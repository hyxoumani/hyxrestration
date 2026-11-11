"""EXP-2203: the tape's backups must not share the tape's deletion command.

`git clean -fdx` in this repo deletes the ignored data/ tree whole: the live
hyxstream.duckdb and, while they sat in data/backups, all seven rotated copies
of it. Three defences, one test group each:

1. the deployed backup unit (and QA, which counts the slots) points
   HYXLAB_BACKUP_DIR outside every git work tree;
2. `collector.backup` warns whenever its destination is inside a work tree
   (symlinks resolved -- a link out of the tree is safe, git never follows it);
3. `scripts/clean_guard.sh` puts an empty nested repository at data/.git, so a
   one-flag `git clean -fdx` / `-fdX` leaves data/ alone (only -ffdx removes
   it) -- exercised against a real git, not asserted from the docs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from collector import backup

REPO = Path(__file__).resolve().parent.parent
UNIT_DIR = REPO / "scripts" / "systemd"
GUARD = REPO / "scripts" / "clean_guard.sh"
HOOK = REPO / ".claude" / "hooks" / "safety-net.sh"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _env_value(unit: str, key: str) -> str | None:
    text = (UNIT_DIR / unit).read_text()
    m = re.search(rf"^Environment={key}=(\S+)\s*$", text, re.M)
    return None if m is None else m.group(1)


# -- 1. the deployed destination ------------------------------------------


@pytest.mark.parametrize("unit", ["hyxlab-backup.service", "hyxlab-qa.service"])
def test_units_point_the_backups_outside_every_work_tree(unit):
    dest = _env_value(unit, "HYXLAB_BACKUP_DIR")
    assert dest is not None, f"{unit} must set HYXLAB_BACKUP_DIR (EXP-2203)"
    assert os.path.isabs(dest)
    for tree in (
        "/home/devs/workspace/hyxrestration",
        "/home/devs/workspace/hyxrestration-stable",
        "/home/devs/workspace/hylshi",
    ):
        assert not (dest == tree or dest.startswith(tree + "/")), f"{dest} is inside {tree}"


def test_writer_and_qa_agree_on_where_the_slots_are():
    assert _env_value("hyxlab-backup.service", "HYXLAB_BACKUP_DIR") == _env_value(
        "hyxlab-qa.service", "HYXLAB_BACKUP_DIR"
    )


# -- 2. the writer's warning ----------------------------------------------


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "data").mkdir()
    return root


def test_a_dest_inside_a_work_tree_is_named(tmp_path):
    root = _tree(tmp_path)
    inside = root / "data" / "backups"
    inside.mkdir()
    assert backup.enclosing_git_worktree(inside, tmp_path) == root.resolve()


def test_a_linked_worktree_counts_too(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".git").write_text("gitdir: /elsewhere\n")  # linked worktrees carry a .git FILE
    assert backup.enclosing_git_worktree(root / "data", tmp_path) == root.resolve()


def test_a_dest_outside_any_tree_is_clear(tmp_path):
    out = tmp_path / "hyx-backups"
    out.mkdir()
    assert backup.enclosing_git_worktree(out, tmp_path) is None


def test_a_symlink_out_of_the_tree_is_clear(tmp_path):
    root = _tree(tmp_path)
    out = tmp_path / "hyx-backups"
    out.mkdir()
    (root / "data" / "backups").symlink_to(out)
    assert backup.enclosing_git_worktree(root / "data" / "backups", tmp_path) is None


def test_main_warns_only_for_an_in_tree_dest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(backup, "DBS", [])  # nothing to copy: the warning is the subject
    monkeypatch.setattr(backup, "GIT_CEILING", tmp_path)
    root = _tree(tmp_path)
    monkeypatch.setattr("sys.argv", ["backup", "--dest", str(root / "data" / "backups")])
    backup.main()
    assert "WARNING" in capsys.readouterr().out
    out = tmp_path / "hyx-backups"
    monkeypatch.setattr("sys.argv", ["backup", "--dest", str(out)])
    backup.main()
    assert "WARNING" not in capsys.readouterr().out


# -- 3. the clean guard, against a real git ---------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo_with_tape(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("/data/\n")
    _git(root, "add", ".gitignore")
    _git(root, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i")
    (root / "data").mkdir()
    (root / "data" / "hyxstream.duckdb").write_bytes(b"tape")
    return root


@needs_git
def test_without_the_guard_clean_x_takes_the_tape(repo_with_tape):
    """The failure mode is real (the control: otherwise the guard test proves nothing)."""
    _git(repo_with_tape, "clean", "-fdx")
    assert not (repo_with_tape / "data").exists()


@needs_git
@pytest.mark.parametrize("flags", ["-fdx", "-fdX", "-xfd"])
def test_with_the_guard_one_flag_clean_leaves_the_tape(repo_with_tape, flags):
    data = repo_with_tape / "data"
    assert subprocess.run(["bash", str(GUARD), "--check", str(data)]).returncode == 1
    subprocess.run(["bash", str(GUARD), str(data)], check=True, capture_output=True)
    assert (
        subprocess.run(["bash", str(GUARD), "--check", str(data)], capture_output=True).returncode
        == 0
    )
    _git(repo_with_tape, "clean", flags)
    assert (data / "hyxstream.duckdb").read_bytes() == b"tape"


@needs_git
def test_the_guard_is_idempotent(repo_with_tape):
    data = repo_with_tape / "data"
    for _ in range(2):
        subprocess.run(["bash", str(GUARD), str(data)], check=True, capture_output=True)
    assert (data / ".git").is_dir()


@needs_git
def test_a_symlinked_backup_dir_survives_clean_x(repo_with_tape, tmp_path):
    """The compat link left at data/backups: git removes the LINK, never the copies."""
    out = tmp_path / "hyx-backups"
    out.mkdir()
    (out / "hyxstream.Sat.duckdb").write_bytes(b"copy")
    (repo_with_tape / "data" / "backups").symlink_to(out)
    _git(repo_with_tape, "clean", "-fdx")
    assert (out / "hyxstream.Sat.duckdb").read_bytes() == b"copy"


# -- the Claude-session hook, which now matches -x in any spelling ----------


@pytest.mark.parametrize(
    "cmd,blocked",
    [
        ("git clean -fdx", True),
        ("git clean -xfd", True),
        ("git clean -dfx", True),
        ("git clean -f -d -x", True),
        ("git clean -fdX", True),
        ("git -C /home/devs/workspace/hyxrestration clean -xdf", True),
        ("git clean -n", False),
        ("git status", False),
    ],
)
def test_the_session_hook_refuses_clean_x_in_any_spelling(cmd, blocked):
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    import json

    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
    )
    assert (proc.returncode == 2) is blocked, (cmd, proc.returncode, proc.stderr)

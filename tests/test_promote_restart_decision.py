"""promote.sh restart decision = static import closure (EXP-1276).

Before 2026-08-12 needs_restart() matched changed paths against per-daemon
directory regexes, which is coarser than what each daemon actually imports:
three promotions in one day (294a5ae, 4a4be2c, 5ea07d8) changed only
collector/sweep.py (plus, once, collector/venues/kalshi.py) and each
demanded a hand-verified --defer=hyxlab-stream.service — the
decomposed-by-hand failure promote.sh's own header (EXP-961) exists to
prevent. These tests pin:

- scripts/daemon_imports.py closure correctness on the REAL daemons
  (sweep.py is NOT in streamd's closure; venues/kalshi.py IS — streamd
  imports it at module level);
- lazy (function-level) import detection, and that lazy imports stay IN
  the restart-relevant closure (a running daemon would load new code
  mid-run on its next lazy import — a half-old/half-new process);
- the bash decision logic (scripts/restart_decision.sh) that promote.sh
  sources: closure intersection, --restart-all, and the conservative
  regex fallback when the tool errors.

No live daemon is touched: restart_decision.sh is sourced in a scratch
bash, never promote.sh itself.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from daemon_imports import closure  # noqa: E402

PY = str(REPO / ".venv" / "bin" / "python")
TOOL = str(REPO / "scripts" / "daemon_imports.py")


# ---------------------------------------------------------------- closures
@pytest.fixture(scope="module")
def streamd():
    return closure("collector.streamd")


@pytest.fixture(scope="module")
def shadow():
    return closure("simulator.shadow")


def test_streamd_closure_excludes_sweep_but_includes_kalshi(streamd):
    files, _lazy = streamd
    # The whole point: timer-driven sweep code is NOT stream-daemon code.
    assert "collector/sweep.py" not in files
    assert "collector/collect.py" not in files
    # streamd.py line 39: `from collector.venues import kalshi, ...`
    assert "collector/venues/kalshi.py" in files
    assert "collector/streamd.py" in files
    assert "hyxlab/streamstore.py" in files


def test_streamd_lazy_imports_are_in_the_closure(streamd):
    files, lazy = streamd
    # streamd.py:233 (function-level) `from collector.venues import polymarket`
    # streamd.py:359 (inside main)   `from hyxlab.watchlist import ...`
    assert "collector/venues/polymarket.py" in lazy
    assert "hyxlab/watchlist.py" in lazy
    # Lazy imports are restart-relevant: they MUST remain in the full set.
    assert lazy <= files


def test_streamd_closure_carries_declared_data_deps(streamd):
    files, _lazy = streamd
    # hyxlab.watchlist reads watchlist.json at call time (DATA_DEPS).
    assert "hyxlab/watchlist.json" in files


def test_streamd_eager_set_matches_the_interpreter(streamd):
    """Ground truth: import collector.streamd in a subprocess and compare
    the eagerly loaded intra-repo modules to the static eager set."""
    files, lazy = streamd
    out = subprocess.run(
        [
            PY,
            "-c",
            textwrap.dedent("""
            import sys
            before = set(sys.modules)
            import collector.streamd
            pkgs = {'collector', 'hyxlab', 'simulator', 'strategies'}
            for m in sorted(set(sys.modules) - before):
                if m.split('.')[0] in pkgs:
                    print(m)
        """),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(out.stdout.split())
    static_eager = {f for f in files - lazy if f.endswith(".py")}
    as_modules = set()
    for f in static_eager:
        parts = f[: -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        as_modules.add(".".join(parts))
    assert as_modules == loaded


def test_shadow_closure_members(shadow):
    files, _lazy = shadow
    assert "simulator/shadow.py" in files
    assert "simulator/bookreplay.py" in files
    assert "simulator/sim.py" in files
    # registry builds the strategies, so they are shadow-daemon code:
    assert "simulator/registry.py" in files
    assert "strategies/hylshi_fade.py" in files
    assert "hyxlab/store.py" in files
    # ...and no collector code is:
    assert not any(f.startswith("collector/") for f in files)


def test_unresolvable_root_exits_2():
    res = subprocess.run(
        [PY, TOOL, "closure", "no.such.module"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 2


# ------------------------------------------- lazy detection, synthetically
def test_lazy_vs_eager_on_synthetic_package(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "root.py").write_text("import pkg.eager\ndef f():\n    import pkg.lazy\n")
    (pkg / "eager.py").write_text("x = 1\n")
    (pkg / "lazy.py").write_text("def g():\n    from pkg import lazy2\n")
    (pkg / "lazy2.py").write_text("y = 2\n")
    (pkg / "unrelated.py").write_text("z = 3\n")
    files, lazy = closure("pkg.root", repo=tmp_path)
    assert "pkg/eager.py" in files and "pkg/eager.py" not in lazy
    # lazy, and everything reached only THROUGH a lazy edge, is lazy:
    assert "pkg/lazy.py" in lazy
    assert "pkg/lazy2.py" in lazy
    assert lazy <= files
    assert "pkg/unrelated.py" not in files


# ----------------------------------------------------- bash decision logic
def decide(changed: str, root: str, regex: str, force: str = "0") -> str:
    """Run needs_restart in a scratch bash; returns RESTART or SKIP,
    plus any diagnostic lines the helper printed."""
    script = (
        f'DEV="{REPO}"; FORCE_RESTART={force}; CHANGED="$1"; '
        f'source "{REPO}/scripts/restart_decision.sh"; '
        f'if needs_restart "{root}" "{regex}"; then echo RESTART; else echo SKIP; fi'
    )
    res = subprocess.run(
        ["bash", "-c", script, "bash", changed],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


STREAM_ARGS = ("collector.streamd", "^(collector|hyxlab)/")
SHADOW_ARGS = ("simulator.shadow", "^(simulator|strategies|hyxlab)/")


def test_sweep_only_change_skips_stream_restart():
    # 294a5ae and 5ea07d8: the old regex demanded a hand-verified defer.
    out = decide("collector/sweep.py\ntests/test_hyxlab_sweep_resume.py", *STREAM_ARGS)
    assert out.strip().endswith("SKIP")


def test_kalshi_venue_change_restarts_stream():
    # 4a4be2c touched collector/venues/kalshi.py, which streamd DOES
    # import (line 39) — at file granularity the restart demand is
    # correct; --defer remains the stated exception for read-through-only
    # changes, per promote.sh's own comments.
    out = decide("collector/sweep.py\ncollector/venues/kalshi.py", *STREAM_ARGS)
    assert "RESTART" in out
    assert "collector/venues/kalshi.py" in out  # says WHICH file


def test_none_of_todays_promotions_touch_shadow():
    for changed in (
        "collector/sweep.py\ntests/test_hyxlab_sweep_resume.py",
        "collector/sweep.py\ncollector/venues/kalshi.py",
    ):
        assert decide(changed, *SHADOW_ARGS).strip().endswith("SKIP")


def test_strategy_change_restarts_shadow_not_stream():
    changed = "strategies/hylshi_fade.py"
    assert "RESTART" in decide(changed, *SHADOW_ARGS)
    assert decide(changed, *STREAM_ARGS).strip().endswith("SKIP")


def test_watchlist_json_restarts_stream_via_data_dep():
    assert "RESTART" in decide("hyxlab/watchlist.json", *STREAM_ARGS)


def test_restart_all_overrides():
    assert "RESTART" in decide("README.md", *STREAM_ARGS, force="1")


def test_empty_change_set_skips():
    assert decide("", *STREAM_ARGS).strip().endswith("SKIP")


def test_tool_failure_falls_back_to_regex_and_says_so():
    # Unresolvable root module => daemon_imports exits 2 => regex fallback.
    out = decide("collector/sweep.py", "no.such.module", "^(collector|hyxlab)/")
    assert "WARNING" in out and "falling back" in out
    assert out.strip().endswith("RESTART")  # regex is coarse: restarts
    out2 = decide("docs/notes.md", "no.such.module", "^(collector|hyxlab)/")
    assert out2.strip().endswith("SKIP")


def test_promote_sh_sources_the_helper_and_passes_roots():
    text = (REPO / "scripts" / "promote.sh").read_text()
    assert 'source "$DEV/scripts/restart_decision.sh"' in text
    assert "needs_restart collector.streamd '^(collector|hyxlab)/'" in text
    assert "needs_restart simulator.shadow '^(simulator|strategies|hyxlab)/'" in text
    subprocess.run(["bash", "-n", str(REPO / "scripts" / "promote.sh")], check=True)


# ------------------------------------------------- every daemon, not two
# Long-running units that a promote must NOT restart on its own, and why.
# simui holds a live paper-trading session; dropping it to ship a change
# it is not running costs an operator their session.
NOTIFY_ONLY = {"hyxlab-simui.service"}


def _long_running_units() -> dict[str, str]:
    """unit name -> ExecStart root module, for the units that RUN
    continuously from the stable tree (`Type=simple`). Timers are exempt:
    a oneshot picks up new code on its next firing, with no process to
    restart."""
    out = {}
    for unit in sorted((REPO / "scripts" / "systemd").glob("*.service")):
        text = unit.read_text()
        if "Type=simple" not in text:
            continue
        exec_line = next(x for x in text.splitlines() if x.startswith("ExecStart="))
        parts = exec_line.split()
        module = parts[parts.index("-m") + 1]
        # `python -m PKG` executes PKG.__main__, and a package's own
        # __init__ may import nothing at all.
        if (REPO / module.replace(".", "/") / "__main__.py").exists():
            module += ".__main__"
        out[unit.name] = module
    return out


def test_every_long_running_daemon_is_decided_about():
    """DERIVED, not listed by hand. `promote.sh` restarts the daemons
    whose code moved — but the daemons were enumerated in the script, and
    `hyxlab-simui.service` (installed 2026-08-20, `Restart=always`,
    `MemoryMax=1G`, running from stable) appeared in neither the restart
    list nor the output. Its exclusion from the RESTART set is deliberate
    and pinned elsewhere (a restart drops a live paper session), but
    `Restart=always` means it has no other path to new code, so every
    promotion from its install to 2026-08-27 left it running its original
    code while the script printed a confident "none — no daemon's code
    moved". Silence is the failure; a decision, either way, is the rule.
    The set of daemons is a property of `scripts/systemd/`, so read it
    from there rather than repeating it here."""
    text = (REPO / "scripts" / "promote.sh").read_text()
    units = _long_running_units()
    assert set(units) == {
        "hyxlab-stream.service",
        "hyxlab-shadow.service",
        "hyxlab-simui.service",
    }, f"the set of long-running daemons changed: {sorted(units)}"
    for unit, module in units.items():
        assert f"needs_restart {module} " in text, f"{unit} ({module}) has no restart decision"
        if unit in NOTIFY_ONLY:
            assert f"RESTART+=({unit})" not in text, f"{unit} must not be auto-restarted"
            assert f"NOTICE: {unit} executes changed code" in text, (
                f"{unit} is neither restarted nor reported — that is the silence"
            )
        else:
            assert f"RESTART+=({unit})" in text, f"{unit} is never added to the restart list"
        assert unit in text.split("== promoted:")[0].split("for u in ")[-1].split("\n")[0], (
            f"{unit}'s state is not reported after the promotion"
        )


def test_each_daemon_root_resolves_to_a_real_closure():
    """A root module that resolves to nothing makes `needs_restart`
    answer "no" forever — the failure mode is silence, so the closure of
    every daemon root must actually contain the kernel it reads."""
    for unit, module in _long_running_units().items():
        files, _lazy = closure(module)
        assert "hyxlab/store.py" in files, f"{unit}: closure of {module} is {sorted(files)[:5]}"


def test_a_simui_only_change_restarts_simui_alone():
    changed = "simulator/simui/session.py"
    args = ("simulator.simui.__main__", "^(simulator|strategies|hyxlab)/")
    assert "RESTART" in decide(changed, *args)
    assert decide(changed, *STREAM_ARGS).strip().endswith("SKIP")
    assert decide(changed, *SHADOW_ARGS).strip().endswith("SKIP")


# ------------------------------------------------------------- bound 14
# The closure guard asks "did the code move"; young_run_guard asks "how
# old is the run you are about to kill". Measured 2026-09-02: all 15
# day-starved shadow runs were stopped mid-write, 19 promotes did the
# stopping, and the closure guard was right every time.
def guard(age: str, force: str = "0", young: str = "0", threshold: str | None = None) -> str:
    env = f"FORCE_RESTART={force}; RESTART_YOUNG={young}; "
    if threshold is not None:
        env += f"YOUNG_RUN_S={threshold}; "
    script = (
        f'DEV="{REPO}"; {env}'
        f'source "{REPO}/scripts/restart_decision.sh"; '
        f'if young_run_guard hyxlab-shadow.service "$1"; then echo DEFER; else echo RESTART; fi'
    )
    res = subprocess.run(
        ["bash", "-c", script, "bash", age],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


def test_a_young_shadow_run_is_deferred_and_the_age_is_printed():
    out = guard(str(2 * 86400))
    assert out.strip().endswith("DEFER")
    assert "48h" in out and "72h" in out  # what it has vs what it needs
    assert "--restart-young" in out  # how to override, named


def test_a_run_past_the_threshold_is_restarted():
    assert guard(str(3 * 86400)).strip().endswith("RESTART")
    assert guard(str(10 * 86400)).strip().endswith("RESTART")


def test_the_threshold_is_the_env_and_defaults_to_three_days():
    assert guard("100", threshold="50").strip().endswith("RESTART")
    assert guard("100", threshold="101").strip().endswith("DEFER")
    assert guard(str(259199)).strip().endswith("DEFER")


def test_restart_all_and_restart_young_both_override_the_guard():
    assert guard("60", force="1").strip().endswith("RESTART")
    assert guard("60", young="1").strip().endswith("RESTART")


def test_an_unknown_age_never_defers_and_says_so():
    """The guard protects a MEASURED span. If systemd cannot say when the
    unit started, deferring would be protecting an assumption -- and
    silently leaving a daemon on old code (the simui failure mode)."""
    for age in ("", "n/a", "abc"):
        out = guard(age)
        assert out.strip().endswith("RESTART")
        assert "age unknown" in out


def test_promote_sh_wires_the_young_run_guard_into_the_defer_path():
    text = (REPO / "scripts" / "promote.sh").read_text()
    assert "--restart-young) RESTART_YOUNG=1" in text
    assert 'young_run_guard hyxlab-shadow.service "$(unit_age_s hyxlab-shadow.service)"' in text
    # It must feed the SAME defer path as --defer, so the deferral is
    # recorded the way an explicit one is, not restarted by a later branch.
    guard_at = text.index("young_run_guard hyxlab-shadow.service")
    defer_at = text.index('if [[ -n "$DEFER" ]]; then')
    restart_at = text.index('systemctl --user restart "${RESTART[@]}"')
    assert guard_at < defer_at < restart_at
    subprocess.run(["bash", "-n", str(REPO / "scripts" / "promote.sh")], check=True)

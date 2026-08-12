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
        [PY, "-c", textwrap.dedent("""
            import sys
            before = set(sys.modules)
            import collector.streamd
            pkgs = {'collector', 'hyxlab', 'simulator', 'strategies'}
            for m in sorted(set(sys.modules) - before):
                if m.split('.')[0] in pkgs:
                    print(m)
        """)],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    loaded = set(out.stdout.split())
    static_eager = {
        f for f in files - lazy if f.endswith(".py")
    }
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
        capture_output=True, text=True,
    )
    assert res.returncode == 2


# ------------------------------------------- lazy detection, synthetically
def test_lazy_vs_eager_on_synthetic_package(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "root.py").write_text(
        "import pkg.eager\n"
        "def f():\n"
        "    import pkg.lazy\n"
    )
    (pkg / "eager.py").write_text("x = 1\n")
    (pkg / "lazy.py").write_text(
        "def g():\n"
        "    from pkg import lazy2\n"
    )
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
        capture_output=True, text=True, check=True,
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

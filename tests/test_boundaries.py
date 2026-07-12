"""Import-boundary contract between the top-level packages.

The lab is physically split into four packages with opposite risk
profiles: `collector/` runs unattended 24/7 and its failure loses
unrecoverable data; `simulator/` and `strategies/` churn daily;
`hyxlab/` is the shared kernel. The allowed import edges:

    collector  → hyxlab
    simulator  → hyxlab   (engine; runner entrypoints may also wire
                           in `strategies` — see ENTRYPOINTS)
    strategies → simulator, hyxlab
    hyxlab     → (nothing above it)

Anything else means the split has been silently re-fused and the
daemons can no longer be deployed from a stable checkout that never
carries strategy churn.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent

# package -> packages it may import from (besides itself and stdlib/3p)
ALLOWED = {
    "collector": {"hyxlab"},
    "simulator": {"hyxlab"},
    "strategies": {"simulator", "hyxlab"},
    "hyxlab": set(),
}
PACKAGES = set(ALLOWED)

# Runner/entrypoint modules that compose engine + strategies. The
# engine itself (sim, strategy, capabilities, bookreplay, harness)
# must stay strategy-agnostic.
ENTRYPOINTS = {
    "simulator/run_sim.py",
    "simulator/run_backtest.py",
    "simulator/run_favlong.py",
    "simulator/shadow.py",
    "simulator/divergence.py",
    "simulator/simui/server.py",
}


def _internal_imports(path: Path) -> set[str]:
    """Top-level lab packages imported by a file (incl. lazy imports)."""
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in PACKAGES:
                out.add(top)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in PACKAGES:
                    out.add(top)
    return out


def _violations(package: str) -> list[str]:
    allowed = ALLOWED[package] | {package}
    bad = []
    for py in (ROOT / package).rglob("*.py"):
        extra = {"strategies"} if str(py.relative_to(ROOT)) in ENTRYPOINTS else set()
        hits = _internal_imports(py) - allowed - extra
        if hits:
            bad.append(f"{py.relative_to(ROOT)} imports {sorted(hits)}")
    return bad


def test_collector_imports_kernel_only():
    assert _violations("collector") == []


def test_simulator_never_imports_collector():
    assert _violations("simulator") == []


def test_strategies_never_import_collector():
    assert _violations("strategies") == []


def test_kernel_imports_nothing_above_it():
    assert _violations("hyxlab") == []


def test_stable_requirements_cover_daemon_imports():
    """Version/package skew between dev-test and stable-run is invisible
    until a daemon crashes at 3am: every third-party package imported
    by stable-side code (collector + kernel + the sim modules the
    shadow daemon runs) must be pinned in requirements-stable.txt."""
    import sys

    stdlib = sys.stdlib_module_names
    pinned = set()
    req = ROOT / "scripts" / "requirements-stable.txt"
    for line in req.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            pinned.add(line.split("==")[0].split(">=")[0].strip().lower().replace("-", "_"))

    missing = {}
    for pkg in ("collector", "hyxlab", "simulator", "strategies"):
        for py in (ROOT / pkg).rglob("*.py"):
            for top in _third_party_imports(py, stdlib):
                if top not in pinned:
                    missing.setdefault(top, str(py.relative_to(ROOT)))
    assert missing == {}, f"unpinned in requirements-stable.txt: {missing}"


def _third_party_imports(path: Path, stdlib) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for name in names:
            top = name.split(".")[0]
            if top not in stdlib and top not in PACKAGES:
                out.add(top.lower())
    return out

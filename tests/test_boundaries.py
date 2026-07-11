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

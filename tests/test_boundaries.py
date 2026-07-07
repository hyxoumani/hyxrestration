"""Import-boundary contract between collection and simulation.

The two sides have opposite risk profiles: collection runs unattended
24/7 and its failure loses unrecoverable data; sim/strategy code churns
daily. Neither may import the other — both go through the shared kernel
only. This keeps the daemons deployable from a stable checkout that
never has to carry strategy churn, and keeps a future physical split a
`git mv` instead of a refactor.
"""

import ast
from pathlib import Path

PKG = Path(__file__).parent.parent / "hyxlab"

COLLECTION = {
    "collect",
    "sweep",
    "backfill",
    "streamd",
    "venues",  # all venue clients (REST + WS)
}
SIM = {
    "sim",
    "strategy",
    "strategies",
    "capabilities",
    "harness",
    "run_sim",
    "run_backtest",
}
# Shared kernel (either side may import): models, store, streamstore,
# fees, migrate, watchlist.


def _top_module(path: Path) -> str:
    rel = path.relative_to(PKG)
    return rel.parts[0].removesuffix(".py")


def _hyxlab_imports(path: Path) -> set[str]:
    """Top-level hyxlab submodules imported by a file (incl. lazy imports)."""
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("hyxlab"):
            parts = node.module.split(".")
            out.add(parts[1] if len(parts) > 1 else node.names[0].name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("hyxlab."):
                    out.add(alias.name.split(".")[1])
    return out


def _violations(side_modules: set[str], forbidden: set[str]) -> list[str]:
    bad = []
    for py in PKG.rglob("*.py"):
        if _top_module(py) not in side_modules:
            continue
        hits = _hyxlab_imports(py) & forbidden
        if hits:
            bad.append(f"{py.relative_to(PKG.parent)} imports {sorted(hits)}")
    return bad


def test_collection_never_imports_sim_side():
    assert _violations(COLLECTION, SIM) == []


def test_sim_side_never_imports_collection():
    assert _violations(SIM, COLLECTION) == []


def test_every_module_is_classified():
    """New top-level modules must be assigned a side (or the kernel)."""
    kernel = {
        "models",
        "store",
        "streamstore",
        "fees",
        "migrate",
        "watchlist",
        "stations",
        "__init__",
    }
    tops = {_top_module(p) for p in PKG.rglob("*.py")}
    unclassified = tops - COLLECTION - SIM - kernel
    assert unclassified == set(), f"classify in tests/test_boundaries.py: {unclassified}"

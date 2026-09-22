"""Static intra-repo import closure of a module, and a sha over its bytes.

This is the ONE implementation of the closure walk EXP-1276 introduced.
`scripts/daemon_imports.py` is the CLI over it (promote.sh's restart
decision); `simulator.divergence` uses `closure_sha` to stamp a report
with the code that produced it. Two copies of this walk would be two
answers to "which files does this module execute", and the restart
decision and the staleness decision have to agree.

Stdlib only (ast, hashlib) and no project imports beyond this package's
docstring-only `__init__`: promote.sh runs the CLI as a plain script
before any daemon is touched, and it must not need the venv's deps.

LAZY (function-level) IMPORTS ARE PART OF THE CLOSURE, and dangerously
so: a long-running daemon that lazily imports a module it has not touched
yet will load the NEW code from disk on the next call after a promotion,
while everything already imported stays OLD. They are reported separately
only so a human can see which edges are deferred.

Known non-Python data dependencies are declared in DATA_DEPS: a module in
the closure drags its data files in (hyxlab.watchlist reads
hyxlab/watchlist.json at call time).
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Module -> non-Python files it reads at runtime (repo-relative).
DATA_DEPS: dict[str, list[str]] = {
    "hyxlab.watchlist": ["hyxlab/watchlist.json"],
}


def top_level_packages(root: Path) -> set[str]:
    """Importable top-level package names in the repo."""
    return {
        p.name
        for p in root.iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    }


def module_to_path(module: str, root: Path) -> Path | None:
    """Resolve a dotted module name to its repo file, or None."""
    rel = Path(*module.split("."))
    for cand in (root / rel.with_suffix(".py"), root / rel / "__init__.py"):
        if cand.exists():
            return cand
    return None


class _ImportCollector(ast.NodeVisitor):
    """Collect (module_name, lazy) import edges from one file.

    lazy == the import statement sits inside a function/lambda body, so it
    executes on call, not at module import time.
    """

    def __init__(self, package: str) -> None:
        self.package = package  # dotted package of the CURRENT module
        self.edges: list[tuple[str, bool]] = []
        self._depth = 0

    # -- scope tracking -------------------------------------------------
    def _scoped(self, node: ast.AST) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_Lambda = _scoped

    # -- imports --------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.edges.append((alias.name, self._depth > 0))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        lazy = self._depth > 0
        if node.level:  # relative import
            parts = self.package.split(".") if self.package else []
            if node.level - 1 <= len(parts):
                base_parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(base_parts)
            else:
                base = ""
            mod = f"{base}.{node.module}" if base and node.module else (node.module or base)
        else:
            mod = node.module or ""
        if not mod:
            return
        self.edges.append((mod, lazy))
        # `from X import a` where a is itself a submodule X.a
        for alias in node.names:
            if alias.name != "*":
                self.edges.append((f"{mod}.{alias.name}", lazy))


def _file_to_module(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts)


def closure(root_module: str, repo: Path = REPO_ROOT) -> tuple[set[str], set[str]]:
    """Return (all_files, lazy_only_files) of ROOT's import closure.

    Paths are repo-relative POSIX strings. lazy_only_files are those NOT
    reachable through eager edges alone — they still belong to all_files
    (see module docstring for why lazy imports are restart-relevant).

    Raises ValueError if the root module cannot be resolved.
    """
    pkgs = top_level_packages(repo)
    root_path = module_to_path(root_module, repo)
    if root_path is None:
        raise ValueError(f"cannot resolve root module {root_module!r} in {repo}")

    # BFS twice conceptually: track the "eagerly reachable" frontier and
    # the full frontier. A module is eager iff reachable from the root via
    # eager edges only.
    parsed: dict[str, list[tuple[str, bool]]] = {}

    def edges_of(module: str, path: Path) -> list[tuple[str, bool]]:
        if module not in parsed:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            # a package's __init__ IS its package; a module's package is its parent
            package = module if path.name == "__init__.py" else module.rpartition(".")[0]
            col = _ImportCollector(package)
            col.visit(tree)
            # keep only intra-repo, resolvable edges
            kept = []
            for name, lazy in col.edges:
                if name.split(".")[0] not in pkgs:
                    continue
                if module_to_path(name, repo) is not None:
                    kept.append((name, lazy))
                    # importing pkg.mod also imports pkg (its __init__)
                    parent = name.rpartition(".")[0]
                    while parent:
                        if module_to_path(parent, repo) is not None:
                            kept.append((parent, lazy))
                        parent = parent.rpartition(".")[0]
            parsed[module] = kept
        return parsed[module]

    all_mods: set[str] = set()
    eager_mods: set[str] = set()
    queue: list[tuple[str, bool]] = [(root_module, True)]  # (module, eager?)
    while queue:
        module, eager = queue.pop()
        if module in eager_mods or (module in all_mods and not eager):
            continue
        all_mods.add(module)
        if eager:
            eager_mods.add(module)
        path = module_to_path(module, repo)
        assert path is not None  # filtered at edge collection
        for name, lazy in edges_of(module, path):
            child_eager = eager and not lazy
            if (child_eager and name not in eager_mods) or name not in all_mods:
                queue.append((name, child_eager))

    def files_of(mods: set[str]) -> set[str]:
        out: set[str] = set()
        for m in mods:
            p = module_to_path(m, repo)
            if p is not None:
                out.add(p.relative_to(repo).as_posix())
            for data in DATA_DEPS.get(m, []):
                out.add(data)
        return out

    all_files = files_of(all_mods)
    lazy_only = all_files - files_of(eager_mods)
    return all_files, lazy_only


def closure_sha(root_module: str, repo: Path = REPO_ROOT) -> dict[str, object]:
    """Identify the CODE that a run of `root_module` would execute.

    Returns ``{"root", "sha", "files"}`` — a sha256 over every repo file in
    the closure, each contributed as its repo-relative path AND its bytes,
    in sorted path order (so a rename changes the sha as surely as an edit
    does).

    WHY THE WHOLE CLOSURE AND NOT THE ROOT FILE. The instrumentation whose
    absence motivated this (mistakes #76) was added across
    `simulator/divergence.py` (#66's `price_delta_median`, #68's
    `nearest_unpaired_dt`, #69's `nearest_qty_delta`) AND `hyxlab/store.py`
    (#70/#71's `attach_wait`). A stamp over the root file alone would have
    reported "same code" for the last two.

    WHY BYTES AND NOT A HAND-BUMPED VERSION CONSTANT. A constant somebody
    has to remember to bump is the same class of claim this repo keeps
    finding wrong (#70's "~8x margin", #71's censored ledger). The cost of
    the mechanical answer is that a comment-only edit also reads as new
    code; that is the safe direction, and it is paid at most once per
    change by one re-derive.

    Raises ValueError / OSError the way `closure` does; callers that cannot
    afford to fail should treat the failure as "cannot prove fresh" rather
    than as "unchanged" (mistakes #74).
    """
    files, _lazy = closure(root_module, repo)
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update((repo / rel).read_bytes())
        h.update(b"\0")
    return {"root": root_module, "sha": h.hexdigest(), "files": len(files)}

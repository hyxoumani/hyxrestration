#!/usr/bin/env python3
"""Static import-closure of a daemon's root module (EXP-1276).

promote.sh restarts a daemon only when the promotion moves code that
daemon RUNS (EXP-961). Until 2026-08-12 "runs" was approximated by
per-daemon directory regexes ('^(collector|hyxlab)/' for streamd), which
is coarser than the truth: three promotions in one day (294a5ae, 4a4be2c,
5ea07d8) changed only collector/sweep.py or venue code streamd never
imports, and each demanded a hand-verified --defer — exactly the
decomposed-by-hand failure the script's header says it exists to prevent,
with the opposite risk attached (deferring a restart that WAS needed).

This tool computes the honest set: walk the root module's intra-repo
import graph statically (ast, stdlib only) and emit every repo file the
daemon can execute.

LAZY (function-level) IMPORTS ARE PART OF THE RESTART-RELEVANT CLOSURE,
and dangerously so: a long-running daemon that lazily imports a module it
has not touched yet will load the NEW code from disk on the next call
after a promotion, while everything already imported stays OLD — a
half-old/half-new process that no test ever ran. That is a stronger
reason to restart, not a weaker one. They are reported separately
(``--json`` -> "lazy") only so a human can see which edges are deferred.

Known non-Python data dependencies are declared in DATA_DEPS: a module in
the closure drags its data files in (hyxlab.watchlist reads
hyxlab/watchlist.json at call time).

Usage:
    daemon_imports.py closure ROOT [--json]
        Print the closure, one repo-relative path per line (sorted).
        --json emits {"root":..., "files":[...], "lazy":[...]}.
    daemon_imports.py intersect ROOT
        Read changed paths (one per line) on stdin; print those inside
        ROOT's closure. Exit 0 whether or not any match; exit 2 on any
        error (unresolvable root, syntax error...) so callers can fall
        back conservatively.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
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
            if path.name == "__init__.py":
                package = module
            else:
                package = module.rpartition(".")[0]
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["closure", "intersect"])
    ap.add_argument("root", help="dotted root module, e.g. collector.streamd")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    try:
        files, lazy = closure(args.root)
    except (ValueError, OSError, SyntaxError) as exc:
        print(f"daemon_imports: {exc}", file=sys.stderr)
        return 2

    if args.command == "closure":
        if args.as_json:
            print(json.dumps({
                "root": args.root,
                "files": sorted(files),
                "lazy": sorted(lazy),
            }, indent=2))
        else:
            for f in sorted(files):
                print(f)
        return 0

    # intersect: changed paths on stdin, print those the daemon executes.
    changed = {line.strip() for line in sys.stdin if line.strip()}
    for f in sorted(changed & files):
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())

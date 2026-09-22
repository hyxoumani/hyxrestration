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

THE WALK ITSELF LIVES IN `hyxlab.importclosure`, which this file is a CLI
over. It moved there 2026-09-22 so the staleness stamp on the divergence
report (`closure_sha`) and this restart decision cannot disagree about
which files a module executes (mistakes #76).

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
    daemon_imports.py intersect ROOT [--allow-empty]
        Read changed paths (one per line) on stdin; print those inside
        ROOT's closure. Exit 0 whether or not any match; exit 2 on any
        error (unresolvable root, syntax error...) so callers can fall
        back conservatively. Exit 2 ALSO when stdin supplies no paths at
        all unless --allow-empty is given: an unfed intersect would
        otherwise print nothing and read as "this daemon is unaffected",
        which is the unsafe answer (mistakes #74).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Imported by PATH, not as a package: promote.sh runs this file directly, so
# sys.path[0] is scripts/ and `hyxlab` is not importable without this line.
# `hyxlab/__init__.py` is docstring-only and `hyxlab.importclosure` is stdlib
# -- the pre-restart decision still needs no venv deps.
sys.path.insert(0, str(REPO_ROOT))

from hyxlab.importclosure import (  # noqa: E402,I001
    DATA_DEPS,
    closure,
    module_to_path,
    top_level_packages,
)

__all__ = ["DATA_DEPS", "closure", "module_to_path", "top_level_packages", "main"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["closure", "intersect"])
    ap.add_argument("root", help="dotted root module, e.g. collector.streamd")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument(
        "--allow-empty",
        action="store_true",
        help="`intersect` only: accept an empty change set on stdin instead of"
        " refusing (mistakes #74). promote.sh passes this; a hand-run"
        " intersect should not.",
    )
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
    #
    # REFUSE AN UNFED `intersect` (mistakes #74). Starved of input this
    # command intersects the empty set, prints nothing and exits 0 -- which
    # reads exactly like "this daemon is unaffected, skip the restart", the
    # unsafe direction. On 2026-09-22 that silence was taken as evidence for
    # three daemon roots and a 239.7h `hyxlab-stream` run was predicted safe
    # minutes before promote.sh correctly restarted it.
    #
    # THE GUARD IS "NO INPUT ARRIVED", NOT `isatty`. The first attempt at
    # this checked `sys.stdin.isatty()` and WOULD NOT HAVE CAUGHT THE CASE
    # IT WAS WRITTEN FOR: an agent shell's stdin is not a terminal either,
    # so it hits EOF immediately and reads as a legitimate empty pipe --
    # exactly the path that produced the wrong answer. A TTY check protects
    # a human at a keyboard, who is not the actor that made this mistake.
    #
    # An empty change set IS legitimate for promote.sh (a promotion can move
    # nothing a daemon runs), so that caller declares it with
    # `--allow-empty`. Requiring the flag makes "I have a file list and it
    # happens not to match" distinguishable from "I never supplied one",
    # which is the whole of the defect. Use `closure` for the
    # diff-independent question "does this daemon run this file?".
    changed = {line.strip() for line in sys.stdin if line.strip()}
    if not changed and not args.allow_empty:
        print(
            "daemon_imports: `intersect` reads changed paths on stdin and got"
            f" NONE, so it would print nothing and imply '{args.root} is"
            " unaffected' without having looked at anything. Pipe the file"
            " list in (e.g. `git diff --name-only stable..main | ... intersect"
            f" {args.root}`); pass --allow-empty if an empty change set is"
            f" genuinely expected; or use `closure {args.root}` to ask whether"
            " the daemon runs a file at all.",
            file=sys.stderr,
        )
        return 2

    for f in sorted(changed & files):
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())

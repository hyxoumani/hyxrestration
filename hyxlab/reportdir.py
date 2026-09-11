"""Where a standing report's OUTPUT lives when two checkouts share one archive.

    from hyxlab.reportdir import shared_reports
    DIVERGENCE_REPORTS = shared_reports("shadow_divergence")

Why this exists (2026-09-11). The box runs two worktrees of this repo:
the dev tree, where an agent types `python -m simulator.divergence` by
hand, and `hyxrestration-stable`, which every systemd unit sets as its
`WorkingDirectory`. The two share their STATE through symlinks --
`data`, `.env`, `.secrets` in the stable tree all point back at the dev
tree -- but `reports/` is a real directory in each, so a relative
`reports/<x>` default resolves to a DIFFERENT place depending on which
tree you were standing in.

That is fine while reports are only ever written by hand. It stops being
fine the moment a report gets a timer, because the writer and the reader
then disagree about where the artifact is. Measured, both halves:

  * 2026-09-09 15:50 local, an agent ran the divergence report in the dev
    tree for shadow run `20260829T191841`. It landed in the dev tree.
  * 2026-09-10 05:00 local, `hyxlab-qa` -- WorkingDirectory=stable -- read
    the stable tree's `reports/shadow_divergence`, did not find it, and
    FAILED `shadow-vs-replay divergence measured on the newest complete
    run` with "newest report on file is 20260810T081931". The report it
    was asking for had existed for 13 hours.
  * 2026-09-11 01:20Z, `hyxlab-divergence` fired for the first time with
    `--if-new`, whose entire purpose is to cost nothing when the subject
    is already reported. It looked in the stable tree, found nothing, and
    re-derived the identical 38,143-fill report: 29min 26s wall, 4.2G
    peak. The unit's own comment calls `--if-new` "what makes this daily
    unit affordable".

So the artifact must be rooted somewhere both trees agree on. It is NOT
inferred -- not from the `data` symlink (which points wherever the
operator pointed it, and could sit outside any checkout) and not from
git worktree metadata (a deployment need not be a checkout at all). It
is stated, in the unit file, next to the absolute `WorkingDirectory` and
`ExecStart` that are already stated there:

    Environment=HYXLAB_REPORTS_DIR=/home/devs/workspace/hyxrestration/reports

Unset -- every dev-tree invocation, every test -- the default is the same
relative `reports/` as before, so nothing about running this by hand
changes.

WHAT DOES NOT BELONG HERE. A per-tree RECORD is not a shared artifact.
`collector.qa.STATE` (`reports/qa/sections.json`) is deliberately
relative so the dev tree and the stable worktree keep separate
section-completion records -- see CLAUDE.md and
`tests/test_health_digest.py`. Routing it through this helper would fuse
two records that are supposed to be distinct. The distinction is the
question "is this DERIVED FROM the shared archive, or is it a log of what
THIS checkout did?", and `tests/test_shared_reports.py` holds the line:
any other relative `reports/` literal reachable from a unit reddens.
"""

import os
from pathlib import Path

#: Read by `shared_reports`; set in the unit files that need it.
SHARED_REPORTS_ENV = "HYXLAB_REPORTS_DIR"

#: Used when the environment says nothing -- i.e. cwd-relative, the
#: behaviour every by-hand and in-test invocation had before this module.
DEFAULT_REPORTS_ROOT = "reports"

__all__ = ["SHARED_REPORTS_ENV", "DEFAULT_REPORTS_ROOT", "shared_reports", "reports_root"]


def reports_root() -> Path:
    """The directory standing reports are written to and read from.

    Read at CALL time, not import time, so a test can set the variable
    with `monkeypatch.setenv` and a caller that holds the path in a
    module constant is the one choosing to freeze it.
    """
    # An empty value is an unset value: `Environment=HYXLAB_REPORTS_DIR=`
    # is how systemd spells "clear this", and it must not resolve to the
    # filesystem root's neighbour.
    return Path(os.environ.get(SHARED_REPORTS_ENV) or DEFAULT_REPORTS_ROOT)


def shared_reports(name: str) -> Path:
    """`reports_root()/name` -- the directory one report family writes into."""
    return reports_root() / name

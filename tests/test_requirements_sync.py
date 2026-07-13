"""Version-skew tripwire between the dev and stable requirement files.

The daemons run from a separate stable worktree whose deps are pinned
in `scripts/requirements-stable.txt`, while the dev tree (where the
test suite runs) installs from `requirements.txt`. If the two drift
apart for a shared package, the suite validates a different version
than production executes — invisibly, until a daemon breaks at 3am.

Rules enforced (design decisions, given the files' actual formats):

- Dev uses `>=` ranges, stable uses exact `==` pins. For every package
  named in BOTH files, the stable pin must satisfy the dev specifier
  set (a dev `==` pin therefore demands exact equality).
- Every stable entry must be an exact `==` pin — a range there would
  silently escape the comparison and defeat reproducible deploys.
- Packages present in only one file do NOT fail: dev-only entries are
  tooling/research deps the daemons never import (pytest, torch, ...),
  and stable-only entries (requests, websockets, ...) are covered the
  other way round by test_boundaries.py's
  test_stable_requirements_cover_daemon_imports, which asserts every
  third-party import of stable-side code is pinned in stable.
- Every stable-pinned package must be INSTALLED in the interpreter
  running this suite, at exactly the pinned version. The two file
  checks above can both pass while the dev venv itself drifts (e.g. a
  stray `pip install -U`), in which case the suite exercises a version
  production never runs. Not installed at all also fails: the suite
  can't validate daemon code against a dep it can't import.

Parsing uses `packaging`, which pytest itself hard-depends on.
"""

from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).parent.parent
DEV_REQ = ROOT / "requirements.txt"
STABLE_REQ = ROOT / "scripts" / "requirements-stable.txt"


def _parse(path: Path) -> dict[str, Requirement]:
    """Canonical package name -> Requirement, comments stripped."""
    reqs = {}
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            req = Requirement(line)
            reqs[canonicalize_name(req.name)] = req
    return reqs


def test_stable_entries_are_exact_pins():
    loose = [
        str(req)
        for req in _parse(STABLE_REQ).values()
        if [s.operator for s in req.specifier] != ["=="]
    ]
    assert loose == [], f"non-== entries in {STABLE_REQ.name}: {loose}"


def test_stable_pins_satisfy_dev_specifiers():
    dev = _parse(DEV_REQ)
    stable = _parse(STABLE_REQ)
    skewed = []
    for name in sorted(dev.keys() & stable.keys()):
        pin = Version(next(iter(stable[name].specifier)).version)
        if not dev[name].specifier.contains(pin, prereleases=True):
            skewed.append(f"{name}: stable pins {pin}, dev requires {dev[name].specifier}")
    assert skewed == [], f"version skew between {DEV_REQ.name} and {STABLE_REQ.name}: {skewed}"


def test_stable_pins_are_installed_in_running_environment():
    # Canonicalize installed names ourselves rather than trusting
    # metadata.version()'s lookup to normalize (it is not guaranteed to
    # across implementations).
    installed = {
        canonicalize_name(dist.metadata["Name"]): Version(dist.version)
        for dist in metadata.distributions()
    }
    drifted = []
    for name, req in sorted(_parse(STABLE_REQ).items()):
        pin = Version(next(iter(req.specifier)).version)
        if name not in installed:
            drifted.append(
                f"{name}: pinned {pin} in {STABLE_REQ.name} but not installed here "
                "(suite cannot validate daemon code against an uninstalled dep)"
            )
        elif installed[name] != pin:
            drifted.append(f"{name}: stable pins {pin}, this environment runs {installed[name]}")
    assert drifted == [], f"dev environment drifted from {STABLE_REQ.name} pins: {drifted}"

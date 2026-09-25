"""The verdict-reachability audit, tested as the instrument it is.

The audit itself cannot be asserted from inside the suite it measures — its
input is not complete until the session ends. So what is tested here is the
decision it makes on a given body of evidence, plus the gate that decides
whether the evidence is admissible at all. See tests/verdict_audit.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import verdict_audit as va

REPO = Path(__file__).resolve().parent.parent


class _Opt:
    keyword = ""
    markexpr = ""
    deselect = None
    last_failed = False
    failedfirst = False
    collectonly = False


class _Config:
    def __init__(self, args, **opts):
        self.args = args
        self.option = _Opt()
        for k, v in opts.items():
            setattr(self.option, k, v)


class _Session:
    def __init__(self, config, testsfailed=0):
        self.config = config
        self.testsfailed = testsfailed
        self.exitstatus = 0


def test_a_name_seen_green_and_never_red_is_reported():
    assert va.never_red({"c": {"pass"}}) == ["c"]


def test_a_name_seen_both_ways_is_not_reported():
    assert va.never_red({"c": {"pass", "fail"}}) == []


def test_a_red_only_name_is_not_reported():
    """The asymmetry is the point: a check exercised solely through its
    failure is a mutant test doing its job, not a gap."""
    assert va.never_red({"c": {"fail"}}) == []


def test_a_declared_name_is_not_reported(monkeypatch):
    monkeypatch.setitem(va.DECLARED, "c", "because")
    assert va.never_red({"c": {"pass"}}) == []


def test_a_declaration_the_session_drove_red_is_stale(monkeypatch):
    monkeypatch.setattr(va, "DECLARED", {"c": "because"})
    assert va.stale_declarations({"c": {"pass", "fail"}}) == ["c"]


def test_a_declaration_nothing_emits_is_stale(monkeypatch):
    monkeypatch.setattr(va, "DECLARED", {"c": "because"})
    assert va.stale_declarations({"other": {"pass"}}) == ["c"]


def test_a_live_declaration_is_not_stale(monkeypatch):
    monkeypatch.setattr(va, "DECLARED", {"c": "because"})
    assert va.stale_declarations({"c": {"pass"}}) == []


@pytest.mark.parametrize("arg", ["tests", "tests/", str(REPO / "tests"), str(REPO), "."])
def test_a_whole_suite_run_is_admissible(arg):
    assert va.is_full_run(_Config([arg]))


@pytest.mark.parametrize(
    "config",
    [
        _Config(["tests/test_hyxlab_qa.py"]),
        _Config(["tests"], keyword="mirror"),
        _Config(["tests"], markexpr="slow"),
        _Config(["tests"], deselect=["tests/test_hyxlab_qa.py::t"]),
        _Config(["tests"], last_failed=True),
        _Config([]),
    ],
    ids=["one file", "-k", "-m", "--deselect", "--lf", "no args"],
)
def test_a_partial_run_is_not_admissible(config):
    """A session that saw a handful of names would report every other check
    as never-red — the audit has to decline rather than guess."""
    assert not va.is_full_run(config)


def test_report_fails_the_session_and_names_the_check(monkeypatch, capsys):
    monkeypatch.setattr(va, "observed", {"c": {"pass"}})
    monkeypatch.setattr(va, "DECLARED", {})
    session = _Session(_Config(["tests"]))
    va.report(session, 0)
    assert session.exitstatus == 1
    out = capsys.readouterr().out
    assert "NEVER RED" in out and "'c'" in out


def test_report_is_silent_on_a_partial_run(monkeypatch, capsys):
    monkeypatch.setattr(va, "observed", {"c": {"pass"}})
    monkeypatch.setattr(va, "DECLARED", {})
    session = _Session(_Config(["tests/test_hyxlab_qa.py"]))
    va.report(session, 0)
    assert session.exitstatus == 0
    assert capsys.readouterr().out == ""


def test_report_is_silent_when_the_suite_already_failed(monkeypatch, capsys):
    """A red suite is reported by the failing test. Adding a second verdict
    on top of it would bury the first."""
    monkeypatch.setattr(va, "observed", {"c": {"pass"}})
    monkeypatch.setattr(va, "DECLARED", {})
    session = _Session(_Config(["tests"]), testsfailed=1)
    va.report(session, 1)
    assert session.exitstatus == 0
    assert capsys.readouterr().out == ""


def test_the_recorder_sees_both_verdicts_and_passes_them_through(monkeypatch):
    import collector.qa as qa

    monkeypatch.setattr(va, "observed", {})
    qa._failures.clear()
    qa.check("audited", True, "")
    qa.check("audited", False, "")
    qa._failures.clear()
    assert va.observed["audited"] == {"pass", "fail"}


def test_installing_twice_does_not_stack_wrappers(monkeypatch):
    import collector.qa as qa

    before = qa.check
    va.install()
    assert qa.check is before


def test_every_declaration_cites_a_file_that_exists():
    """A reason is only a reason while the test it points at is still there."""
    for name, why in va.DECLARED.items():
        cited = [
            w for w in why.replace("(", " ").replace(")", " ").split() if w.startswith("tests/")
        ]
        assert cited, f"{name}: the declaration cites no test file"
        for path in cited:
            assert (REPO / path).exists(), f"{name}: {path} does not exist"

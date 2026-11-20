"""No test may write the dev tree's real QA record.

`collector.qa.STATE` is relative by design, so with the repo as cwd it IS
`<repo>/reports/qa/sections.json`. conftest's `_redirect_qa_state` points it
into tmp_path for every test; this file proves the redirect is in force and
that a real write through the public path lands there, not in the repo.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from collector import qa

REPO = Path(__file__).resolve().parents[1]
REAL = REPO / "reports" / "qa" / "sections.json"


def test_qa_state_is_redirected_out_of_the_repo(tmp_path):
    assert qa.STATE.is_absolute()
    assert tmp_path in qa.STATE.parents
    assert REPO / "reports" not in qa.STATE.parents


def test_a_section_stamp_lands_in_tmp_and_leaves_the_real_record_alone(tmp_path):
    """The arm that leaked: a sidecar inside its budget is a real pass, and a
    pass stamps `last_ok` into STATE."""
    before = REAL.read_bytes() if REAL.exists() else None
    sidecar = tmp_path / "skips.jsonl"
    at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    sidecar.write_text(json.dumps({"at": at, "reason": "writer lock held"}) + "\n")
    qa._failures.clear()
    qa._skipped.clear()
    try:
        qa.qa_collect_skips(path=str(sidecar), journal_skips=1)
        assert not qa._failures and not qa._skipped
    finally:
        qa._failures.clear()
        qa._skipped.clear()
    # Vacuity guard: this call DOES write a record -- into the redirect.
    assert "last_ok" in json.loads(qa.STATE.read_text())[qa.COLLECT_SKIP_SECTION]
    after = REAL.read_bytes() if REAL.exists() else None
    assert after == before, "a test wrote the dev tree's real reports/qa/sections.json"


def test_the_shipped_default_is_still_the_per_tree_relative_path():
    """The redirect is a test-time patch; the shipped constant must stay
    relative, or the dev and stable trees would share one record."""
    from tests.conftest import QA_STATE_SHIPPED

    shipped = QA_STATE_SHIPPED
    assert shipped == Path("reports/qa/sections.json")
    assert not shipped.is_absolute()

"""The status page's entry stamps are read as the pass's clock, so they must
be the clock.

Six `docs: correct ... timestamp` commits between 2026-11-10 and 2026-11-12
(mistakes #181) were all the same slip: an entry stamped from an ESTIMATE of
how long the pass had run instead of from `date -u`, every time in the
FUTURE (02:45Z written at 02:25Z on 11-12). A later pass reads "QA at
10:00Z, entry at 10:15Z" as proof the entry saw the run. The suite runs at
the stop gate of the pass that wrote the entry, so a future stamp fails
before the turn ends.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

STATUS = Path(__file__).resolve().parent.parent / "docs" / "wiki" / "status.md"
STAMP = re.compile(r"Updated: \*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})Z")

# A stamp is written a moment before the suite runs; anything further ahead
# than a minute of clock rounding was not read off the clock.
FUTURE_SLACK = timedelta(minutes=1)


def stamps(text: str) -> list[datetime]:
    return [datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC) for s in STAMP.findall(text)]


def future_stamps(text: str, now: datetime) -> list[datetime]:
    return [t for t in stamps(text) if t > now + FUTURE_SLACK]


def test_parser_reads_the_page():
    # Vacuity guard: a reformatted header would make every check below pass
    # on an empty list.
    found = stamps(STATUS.read_text())
    assert len(found) >= 5
    assert found[0] == max(found), "the newest entry is not the first one"


def test_no_entry_is_stamped_in_the_future():
    now = datetime.now(UTC)
    ahead = future_stamps(STATUS.read_text(), now)
    assert not ahead, (
        f"status.md carries stamp(s) later than the clock ({now:%H:%M}Z): "
        f"{[f'{t:%Y-%m-%d %H:%M}Z' for t in ahead]} -- stamp an entry from `date -u`, "
        "never from an estimate of the pass's length"
    )


def test_a_future_stamp_is_caught():
    now = datetime(2026, 11, 12, 2, 25, tzinfo=UTC)
    page = "Updated: **2026-11-12 02:45Z (x)** / Updated: **2026-11-11 22:05Z (y)**"
    assert future_stamps(page, now) == [datetime(2026, 11, 12, 2, 45, tzinfo=UTC)]
    assert future_stamps(page.replace("02:45Z", "02:25Z"), now) == []

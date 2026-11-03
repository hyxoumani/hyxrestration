"""Per-connection, per-minute heartbeat rows and the subscription-change log
(EXP-2007).

WHY. Until this module a minute in which a connection archived zero frames
could not be told apart as "quiet market" or "dead subscription" from the
archive alone. The Kalshi channels that carry a venue `seq` could prove a
silent run quiet AFTER the fact -- but only while the connection survived it,
and the daemon's own dead-air reconnect (2026-08-13) resets seq and destroys
that proof; Polymarket has no seq at all (hylshi EXP-2003 measured 1,272
trade + 304 book dead-air minutes on Kalshi and 20,818 silent Polymarket
minutes as UNKNOWN). The fix is evidence written at the time, by the process
that holds the socket:

- `stream_heartbeats`: one row per (connection, UTC minute), written whether
  or not a frame arrived, with seconds connected, frames, control frames,
  keepalive replies, probes sent/answered/found-dropped, the connection's
  sid and last seq, and a tri-state `alive` (see
  `hyxlab.streamstore.StreamHeartbeat` for the exact rule).
- `stream_subscriptions`: one row per subscription-state change (connect,
  subscribed, disconnect + reason, update_add/update_delete, closed,
  token_set, probe_dropped, probe_unanswered).

WHAT IT CANNOT DO, stated so no reader over-reads a row: `alive=TRUE` needs
evidence about the SUBSCRIPTION, not the socket. Polymarket has such a probe
(a dynamic re-subscribe of the held asset set: the venue answers the bare
text "NO NEW ASSETS" when it still holds every asset, and re-sends a `book`
for any it had dropped -- probed on the live wire 2026-11-03, see
`polymarket_ws.PROBE_HELD_REPLY`); Kalshi books shards have the existing
`get_snapshot` probe. The Kalshi FIREHOSES (trades / ticker / lifecycle) have
no probe here, so a silent firehose minute is `alive=NULL,
evidence='transport'`: the socket was up and nothing says more. That is the
honest residual, not a defect of the row.

Cost model: a row per connection per minute (~20 connections live, i.e. ~20
rows/min against a buffer that fills at ~4,200 rows/s) plus a handful of
subscription rows per reconnect. No REST call is added anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hyxlab.streamstore import StreamHeartbeat, StreamStore, StreamSubEvent

#: Kill switch for A/B staging and an emergency off: "0" disables heartbeat
#: rows, subscription rows AND the Polymarket subscription probe.
ENABLED = os.environ.get("HYXLAB_HEARTBEAT", "1") != "0"

MINUTE_S = 60.0
#: A minute counts as fully connected when the socket was up for at least
#: this many of its seconds -- 1 s of slack for the roll landing a few ms
#: after the boundary and for the connect handshake straddling it.
FULL_MINUTE_S = 59.0
#: Most minutes one roll will back-fill. A late roll (event loop stalled) writes
#: every skipped minute; a wall-clock step FORWARD by hours would otherwise
#: write rows-per-connection for every stepped-over minute. Past this the roll
#: jumps ahead: those minutes then have NO row, which every reader must
#: already treat as unknown (the daemon-down case).
MAX_CATCHUP_MIN = 180


@dataclass
class ConnBeat:
    """One connection's running state plus the CURRENT minute's counters."""

    venue: str
    channel: str
    conn: str
    shard: int | None
    up_since: float | None = None  # wall seconds; None = not connected
    sid: int | None = None
    last_seq: int | None = None
    n_markets: int | None = None
    last_frame_ts: datetime | None = None
    retired: bool = False
    # Current-minute counters (reset by `HeartbeatBook.roll`).
    connected_s: float = 0.0
    frames: int = 0
    control_frames: int = 0
    keepalives: int = 0
    probes_sent: int = 0
    probes_ok: int = 0
    probes_dropped: int = 0
    probes_unanswered: int = 0
    # Probe in flight: wall time sent; books seen since (a dropped-asset reply).
    probe_at: float | None = None
    probe_books: int = 0
    last_evidence: float = field(default=0.0)  # wall time of the last frame / probe ok

    # -- events -------------------------------------------------------------

    def up(self, now: float) -> None:
        if self.up_since is None:
            self.up_since = now
        self.last_evidence = now
        self.probe_at, self.probe_books = None, 0

    def down(self, now: float, minute_start: float) -> None:
        if self.up_since is not None:
            self.connected_s += max(0.0, now - max(self.up_since, minute_start))
            self.up_since = None
        self.probe_at, self.probe_books = None, 0

    def frame(self, recv_ts: datetime, now: float, seq: int | None = None) -> None:
        self.frames += 1
        self.last_frame_ts = recv_ts
        self.last_evidence = now
        if seq is not None:
            self.last_seq = seq

    def control(self) -> None:
        self.control_frames += 1

    def keepalive(self) -> None:
        self.keepalives += 1

    def probe_sent(self, now: float) -> None:
        self.probes_sent += 1
        self.probe_at, self.probe_books = now, 0

    def probe_ok(self, now: float) -> None:
        """The venue said the subscription is intact."""
        self.probes_ok += 1
        self.last_evidence = now
        self.probe_at, self.probe_books = None, 0

    def probe_expired(self, now: float, wait_s: float) -> str | None:
        """Resolve a probe nobody answered with "still subscribed" once
        `wait_s` has passed: 'dropped' when the venue re-sent books instead
        (it had lost assets and has now re-added them), 'unanswered' when it
        sent nothing. None while the probe is still in time (or none is out)."""
        if self.probe_at is None or now - self.probe_at < wait_s:
            return None
        outcome = "dropped" if self.probe_books else "unanswered"
        if outcome == "dropped":
            self.probes_dropped += 1
        else:
            self.probes_unanswered += 1
        self.probe_at, self.probe_books = None, 0
        return outcome

    # -- the minute ---------------------------------------------------------

    def close_minute(self, minute_start: float, minute_end: float) -> StreamHeartbeat:
        connected = self.connected_s
        if self.up_since is not None:
            connected += max(0.0, minute_end - max(self.up_since, minute_start))
        connected = min(MINUTE_S, connected)
        alive, evidence = classify(
            connected, self.frames, self.probes_ok, self.probes_dropped, self.probes_unanswered
        )
        row = StreamHeartbeat(
            venue=self.venue,
            channel=self.channel,
            conn=self.conn,
            shard=self.shard,
            minute=datetime.fromtimestamp(minute_start, UTC),
            emitted_ts=datetime.now(UTC),
            connected_s=round(connected, 3),
            alive=alive,
            evidence=evidence,
            sid=self.sid,
            last_seq=self.last_seq,
            frames=self.frames,
            control_frames=self.control_frames,
            keepalives=self.keepalives,
            probes_sent=self.probes_sent,
            probes_ok=self.probes_ok,
            probes_dropped=self.probes_dropped,
            n_markets=self.n_markets,
            last_frame_ts=self.last_frame_ts,
        )
        self.connected_s = 0.0
        self.frames = self.control_frames = self.keepalives = 0
        self.probes_sent = self.probes_ok = self.probes_dropped = self.probes_unanswered = 0
        return row


def classify(
    connected_s: float, frames: int, probes_ok: int, probes_dropped: int, probes_unanswered: int
) -> tuple[bool | None, str]:
    """(alive, evidence) for one connection-minute. Order matters: a partial
    connection makes the minute's count partial whatever else happened; a
    probe that found assets dropped means the subscription was NOT delivering
    before the repair, even if the repair's books then arrived."""
    if connected_s < FULL_MINUTE_S:
        return False, "disconnected"
    if probes_dropped:
        return False, "probe_dropped"
    if frames:
        return True, "frames"
    if probes_ok:
        return True, "probe"
    if probes_unanswered:
        return False, "probe_unanswered"
    return None, "transport"


class HeartbeatBook:
    """Every live connection's `ConnBeat`, the minute roll, and the
    subscription log. All methods are cheap and synchronous: they only touch
    in-memory counters and the store's buffers (which the flusher writes)."""

    def __init__(self, store: StreamStore, enabled: bool = ENABLED) -> None:
        self.store = store
        self.enabled = enabled
        self.beats: dict[tuple[str, str, str], ConnBeat] = {}
        self.minute_start = _floor_minute(_now())
        self.rows_written = 0
        self.subs_written = 0

    def beat(self, venue: str, channel: str, conn: str, shard: int | None = None) -> ConnBeat:
        key = (venue, channel, conn)
        b = self.beats.get(key)
        if b is None or b.retired:
            b = self.beats[key] = ConnBeat(venue, channel, conn, shard)
        return b

    def down(self, b: ConnBeat, now: float | None = None) -> None:
        b.down(_now() if now is None else now, self.minute_start)

    def retire(self, b: ConnBeat) -> None:
        """A deliberate close (an emptied books shard): the next roll writes
        this minute's row and then forgets the connection."""
        self.down(b)
        b.retired = True

    def sub(
        self,
        b: ConnBeat,
        action: str,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        self.store.append_subevents(
            [
                StreamSubEvent(
                    venue=b.venue,
                    channel=b.channel,
                    conn=b.conn,
                    shard=b.shard,
                    ts=datetime.now(UTC),
                    action=action,
                    reason=reason,
                    sid=b.sid,
                    n_markets=b.n_markets,
                    detail=detail[:500] if detail else None,
                )
            ]
        )
        self.subs_written += 1

    def roll(self, now: float | None = None) -> int:
        """Close every minute that has ended by `now` (normally exactly one)
        and buffer one row per connection per closed minute; returns rows.
        A roll that lands late (event loop stalled) still writes every
        minute, the skipped ones carrying the connection state across them."""
        now = _now() if now is None else now
        rows: list[StreamHeartbeat] = []
        behind = int((now - self.minute_start) // MINUTE_S)
        if behind > MAX_CATCHUP_MIN:
            for b in self.beats.values():  # drop the stepped-over minutes' counters
                b.close_minute(self.minute_start, self.minute_start + MINUTE_S)
            self.minute_start = _floor_minute(now) - MINUTE_S
            for b in self.beats.values():
                if b.up_since is not None:
                    b.up_since = max(b.up_since, self.minute_start)
        while now >= self.minute_start + MINUTE_S:
            end = self.minute_start + MINUTE_S
            for key, b in list(self.beats.items()):
                rows.append(b.close_minute(self.minute_start, end))
                if b.retired:
                    del self.beats[key]
            self.minute_start = end
        if rows and self.enabled:
            self.store.append_heartbeats(rows)
            self.rows_written += len(rows)
        return len(rows) if self.enabled else 0

    def roll_partial(self, now: float | None = None) -> int:
        """At shutdown: close every whole minute, then write the CURRENT
        partial minute too -- its `connected_s` is short by construction, so
        it reads `alive=FALSE, evidence='disconnected'`, which is true."""
        now = _now() if now is None else now
        n = self.roll(now)
        if not self.enabled or not self.beats:
            return n
        rows = [b.close_minute(self.minute_start, now) for b in self.beats.values()]
        self.store.append_heartbeats(rows)
        self.rows_written += len(rows)
        return n + len(rows)

    def secs_to_next_roll(self, now: float | None = None) -> float:
        now = _now() if now is None else now
        return max(0.0, self.minute_start + MINUTE_S - now)


def _now() -> float:
    return datetime.now(UTC).timestamp()


def _floor_minute(t: float) -> float:
    return t - (t % MINUTE_S)

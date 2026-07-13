# Mistakes log

Format: what happened → root cause → error type → prevention tier
(gotcha → rule → hook). Escalate anything recurring.

## 2026-07-06/07 session

1. **DuckDB stored box-local timestamps.** tz-aware inserts silently
   converted to machine-local. Root cause: unverified assumption about
   driver semantics. Type: `wrong-assumption`. Prevention: RULE —
   `store._naive_utc()` on every insert + migration test. ESCALATED.
2. **Settlement result leaked to strategies.** Sanitizing branch built a
   cleaned MarketInfo but fell through to `return info`. Root cause:
   missing early return; no test attacked the channel. Type:
   `missing-verification`. Prevention: HOOK-equivalent — adversarial
   peeker test in CI. ESCALATED.
3. **Vacuous PoC backtest (twice, once in prod run).** Rebalance arb run
   on complement-book data where its trigger is impossible by
   construction; sim returned polite zero fills. Root cause: no
   contract between strategy assumptions and data capabilities. Type:
   `wrong-assumption`. Prevention: RULE — capability guard
   (`hyxlab/capabilities.py`, enforced in `Simulator.__init__`): a test
   that cannot fail is an error. Landing it exposed two more instances
   (vacuous determinism self-test; dead rebalance run in
   run_backtest.py). ESCALATED (2026-07-07).
4. **`pgrep -f` self-match.** Monitoring/kill commands matched their own
   cmdline; reported dead sweep as alive; pkill killed its own shell.
   Type: `tooling-footgun`. Prevention: gotcha — quote patterns / match
   binary path.
5. **4h background job with buffered stdout.** No progress visibility;
   masked the fact the sweep had died. Type: `ops-blindness`.
   Prevention: gotcha — always `python -u` + harness-tracked background
   tasks, never nohup chains.
6. **Migration double-shift near-miss.** Per-distinct-value UPDATE loop
   would have re-shifted colliding values; caught in self-review before
   running. Type: `algorithm-bug`. Prevention: gotcha — timestamp
   migrations as single atomic SQL expressions.
7. **Crossed-candle contamination (1.3%).** Fills at phantom quotes in
   weather v1; found by testing a theorem the user challenged. Type:
   `missing-context` (venue data semantics). Prevention: RULE —
   replay-time gate in `candles_as_snapshots`. ESCALATED.
8. **Weather smoke-peek before pre-registration.** 10-day NYC +23% peek
   preceded threshold lock; disclosed in prereg; full sample showed it
   was noise. Type: `process-slip`. Prevention: gotcha — pipeline smoke
   tests on synthetic data only.

9. **Kalshi WS parsers built on assumed cents-integer fields.** First
   stream-daemon smoke run captured ZERO rows: live frames use
   string-dollar fields (`yes_price_dollars`, `count_fp`, `delta_fp`,
   `{yes,no}_dollars_fp`), not the cents shapes assumed from memory.
   Root cause: probe-before-build skipped because the protocol was
   "already verified" — but the spike only verified auth + channel
   behavior, not field-level schemas. Type: `wrong-assumption`.
   Prevention: gotcha — a probe must capture the exact frames the
   parser will eat; caught same-session because the smoke test asserts
   rows landed, which is the cheap tripwire to keep.

10. **Box-local timestamp corruption RECURRED (item 1's exact failure).**
    New store writers (insert_trades, insert_poly_prices) passed
    tz-aware datetimes straight to DuckDB; 5.4M trade rows landed
    shifted −5 h before a poly unit test caught the mechanism. Root
    cause: the `_naive_utc` RULE lived per-writer, so every NEW writer
    could silently skip it. Repaired by single atomic +5 h UPDATE,
    verified against API created_time ground truth. Type:
    `wrong-assumption` (recurrence). Prevention: ESCALATED rule → test:
    store tests now assert stored ts values for tz-aware inputs on the
    new writers; any future writer must ship with the same assertion.

11. **`pgrep -f` self-match RECURRED (item 4's exact failure, twice in
    one session).** `pkill -f "hyxlab.simui"` inside compound commands
    killed the agent's own wrapper shell (the harness embeds the whole
    command line in a `bash -c` cmdline, so the pattern always
    self-matches) — aborting the rest of the script both times,
    including a server restart that then never ran. Root cause: the
    gotcha tier relied on remembering; compound commands make the
    self-match invisible. Type: `tooling-footgun` (recurrence).
    Prevention: ESCALATED gotcha → RULE (`.claude/rules/ops.md`):
    never `pkill -f <pattern>` when the pattern appears in your own
    command line — use a bracket class (`sim[u]i`) AND keep launch
    strings out of the killing command, or kill by held PID.

12. **flush() lost the batch it claimed to hold (silent archive holes,
    2026-07-11).** `streamstore.flush()` swapped buffers into locals
    *before* `duckdb.connect()`; when a reader (shadow/simui/QA) briefly
    held the file lock, connect raised and the 15 s batch was
    garbage-collected — while the flusher logged "buffer held for
    retry", which was false. 18 occurrences Jul 9–11 left unmarked holes
    that surfaced only as slowly-growing negative reconstructed book
    levels in daily QA (and even that signal was ~90% noise, because the
    QA reconstruction itself was unsound — it keyed snapshots on
    `max(seq)`, but Kalshi seq is subscription-scoped and resets per
    reconnect). Root cause chain: recovery path never tested + recovery
    log message asserted behavior the code didn't have + QA check
    written against imagined rather than observed seq semantics. Type:
    `untested-recovery-path` + `wrong-assumption` (venue seq semantics).
    Prevention: regression test now proves a failed flush preserves the
    buffer; QA reconstruction rewritten time-ordered with a seeded
    seq-reset test; the 18 lost windows retro-marked as
    `flush_failure_backfill` gap rows. Lesson worth escalating if it
    recurs: **a log line describing a recovery guarantee is a claim —
    test it like one.**

13. **ALFRED session poisoning misread as throttling (2026-07-12).**
    All 7 series timed out in-run while a lone fresh-session probe
    succeeded instantly; first diagnosis (rate throttling) led to a
    retry-pacing fix that failed the same way. Actual cause: one
    read-timeout leaves the shared requests.Session's keep-alive
    connection wedged; every subsequent request on that session times
    out. Fix: fresh session per attempt. Type: `wrong-assumption`.
    Aggravator: the runner's `| tail -N` pipe cut the earlier error
    lines, hiding that ALL series failed — diagnose from full logs or
    journals, never a tail-truncated pipe.

## Pattern analysis (Step 5)

`wrong-assumption` cluster (1, 3, and arguably 7): claims about external
system semantics went unverified until they bit. Systemic fix already
adopted: **probe-before-build** (the data_contracts.md live-validation
pass) — keep applying it to every new source/driver. Items 2+3 justified
the capability guard, which landed 2026-07-07 and immediately caught two
further latent instances of item 3's pattern.

Recurrence audit (2026-07-08): item 1 recurred as item 10 (escalated to
test-enforced), item 4 recurred as item 11 (escalated to rule). Both
recurrences were gotcha-tier lessons that relied on memory — the pattern
is clear: **gotchas do not survive sessions; anything that recurs must
jump straight to rule/test/hook.** A counter-example worth recording:
the ops-blindness lesson (item 5) DID pay off 2026-07-08 — a dead
probe's captured output was the only reason the Gamma offset-cap
regression was caught before it silently halved the poly sweep.
Enumeration-shrink tripwire: DONE 2026-07-11 (QA).

Recurrence audit (2026-07-12): readers dying on transient DuckDB lock
collisions recurred 3× in one day (QA reachability, queuescore,
divergence.replay_run) — ESCALATED to a kernel helper
(`hyxlab.store.connect_retry`); every raw read-only connect must use
it. Item 12's lesson ("a log line describing a recovery guarantee is a
claim — test it like one") held: the flush-retry fix was regression-
tested and later converted a would-be data loss into clean
backpressure during heavy replay reads.

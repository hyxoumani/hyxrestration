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

14. **A green check that could not go red, on a metric that was noise
    (2026-07-29).** `qa.py`'s seq-continuity check grouped Kalshi book
    events by `sid`, but `seq` is connection-scoped and `sid=1` is
    reused per connection — so 9 reconnect runs were welded into one
    min..max range and the interleaving reported as holes. The figure
    was not just wrong but window-dependent: 1,468,944 at 07:00, 0 at
    08:16, same data. Its pass condition (`holes == 0 or gaps > 0`) was
    separately satisfied by any one of production's ~392 gap rows, so
    the check had never been able to fail. Corrected reading: 44 holes
    over 9 runs, 0 unexcused. Type: `wrong-assumption` (venue semantics
    unverified) + `vacuous-assertion`. The tell that should have been
    caught earlier: the SAME file, 20 lines below, already documented
    `seq ... resets on every reconnect` for the reconstruction query —
    **a constraint recorded in one code path is not enforced in its
    neighbours; grep the invariant, don't trust the comment's scope.**
    Escalated to test (three regressions incl. a non-vacuity control)
    and to `venues.md` as durable venue semantics.

14. **2026-07-30 — the fix for #13 was vacuous in the same way, and its
    first FAIL was an artifact of its own scoping.** a556b31 replaced
    "any gap row in the window excuses any hole" with "a gap row
    overlapping the RUN excuses that run". But every completed
    connection run ends in a logged reconnect, whose gap row touches the
    run's endpoint — so every completed run was pardoned wholesale (run
    6's holes at 17:55 pardoned by the 21:29 reconnect, 3.4h later), and
    the only run that could fail was the still-open one, which failed
    spuriously and self-cleared an hour later. Narrowing a scope is not
    the same as scoping it correctly: **an excusal must be scoped to the
    thing it excuses (the hole), not to any container that happens to
    hold it.** Type: `vacuous-assertion` (second occurrence in the same
    check). The deeper defect underneath: the check asserted a property
    of the WIRE by counting ARCHIVED ROWS, and the two differ by exactly
    the frames `parse_message` discards — proven by SeqTracker logging
    zero `seq_gap` rows across 70 archived holes. **Before counting
    absences, check that the recorder writes a row for every event it
    saw; a missing row is only evidence of a missing event if it was
    ever going to be written.** Escalated to test (five regressions,
    incl. the foreign-channel case and a void-row integration test) and
    to a capture-side fix (`kind='void'` rows) so the archive records
    the full wire sequence rather than only its rowful frames.

15. **2026-07-30 — a conservatism tier that took its sample size from
    one unit and its point estimate from another.** `flagged_day_robust`
    exists because same-day ladders resolve off one underlying path, so
    it computes Wilson with n = days. But it left `implied`/`realized`
    market-weighted, so a 106-market day outvoted a 1-market day 106:1
    in the mean while both counted as a single draw in n — the tier
    applied the day model to the variance and the market model to the
    mean, and the mismatch is the very correlation it was built to
    bound. Measured: Financials 24h d8 read +0.1289 where the
    day-weighted gap is +0.0208 (6.2x). Crucially the error was **not
    conservative** — it inflated a gap wherever the largest days agreed
    with the signature, which is the direction that manufactures
    findings. Consequence: 13 "day-robust" survivors fell to 5, and
    every high-decile survivor (the "favorites realize above implied"
    half of the standing signature claim) was demoted. Type:
    `unit-of-counting` (eleventh occurrence; first one located inside a
    correction shipped for an earlier occurrence of the same class).
    **When you weaken a statistic to account for correlation, weaken
    BOTH sides — an effective sample size applied to a mean computed in
    the un-corrected unit is not a bound, it is a different statistic.**
    Escalated to test (four regressions; the load-bearing one asserts
    the day-weighted gap NUMBER, so a bug-preserving implementation
    fails on the value rather than on a missing key — verified by
    mutation) and to a reported tier (`flagged_day_weighted`).

16. **2026-07-30 — a sentinel nobody reads is not an alarm, and the fix
    for #14 removed the only detector it had.** `kind='void'` rows were
    added so a frame that archives no book level stops reading as a seq
    hole, and the code comment claimed they "make a Kalshi schema change
    loud". Nothing ever read them. The rows also carried no frame type,
    so an empty-ladder snapshot, a control ack and an unrecognised NEW
    frame type all wrote the identical row. The net effect is an
    INVERSION of detection: before the fix, a frame type this parser did
    not understand left a seq hole and turned the QA seq check red;
    after it, that frame writes a void row, the seq check reads green,
    and no other check looks — the fix traded a detectable failure for
    an invisible one while asserting the opposite. Type:
    `unverified-claim` — the same class as #12 (a comment describing a
    guarantee is a claim, not an implementation), here on a detection
    guarantee rather than a recovery one. **A fix that closes a symptom
    must say what now carries the signal the symptom used to carry; if
    the answer is a row/field, name the consumer that reads it in the
    same commit.** Escalated to test (four regressions; the load-bearing
    one asserts BOTH halves — the seq check stays silent on an unknown
    frame type, proving it is blind rather than redundant, and the new
    check fires naming the type — plus a discrimination control so the
    check is not merely always-red, both verified by mutation) and to a
    reported check (`void frames are known types`).

17. **2026-08-02 — a code path can be untriggered because it is
    UNWIRED, not because the data never reached it; three passes
    attributed the wrong cause.** The 07-31 settlement retirement
    (7a89992) was recorded three separate times as having "no live
    position to act on", and the 08-01 08:30 pass explained that with
    the shadow track's 100%-unobserved outcome coverage — "at 100%
    unobserved, 'no live position was touched' is arithmetic, not
    luck." True, and not the reason. `_settle` is called only from
    `finalize()`, which sits AFTER the `while` loop in
    `simulator/shadow.py:main`, and the unit runs with no `--duration`
    — so the loop is `while True` and finalize is unreachable in the
    daemon. Settlement never ran in production at all: no payout was
    ever credited, no settled contract ever retired, at any coverage.
    The coverage explanation is sufficient but not prior, and because
    it was sufficient it stopped the enquiry. Type: `wrong-assumption`
    — the same class as #12/#16 (a claim about our own code's
    behaviour left unverified), here about REACHABILITY rather than
    about a guarantee. **Before attributing a null result to the data,
    check that the path is wired: find the caller. A coverage
    instrument measures whether the data could have exercised a path
    and says nothing about whether anything calls it.** Note the
    contrast that makes this precise: `_mark` (d07d8e8) runs from
    `_equity` on every snapshot and WAS genuinely live — the same
    pass's conditional negative was correct for the mark fix and wrong
    for the settlement fix, and nothing distinguished them until the
    call sites were read. Escalated to test (nine regressions; the
    load-bearing one asserts cash and the retired book after a
    `poll_once` with `finalize()` never called, so a settle-at-shutdown
    daemon fails on arithmetic rather than on a missing row — verified
    by mutation, six, including the winners-only record that 7a89992
    originally survived).

18. **2026-08-04 — `git checkout <file>` as "undo the mutation" reverted
    the WORK, not just the mutation.** Mutation-verifying a new test
    (drop the dedupe clause via `sed`, expect red) on a file whose fix
    was still UNCOMMITTED, then restoring with `git checkout
    hyxlab/store.py` — which restores HEAD, i.e. the state before the
    fix existed. The set-based `upsert_markets` rewrite vanished; the
    full suite caught it minutes later only because the new empty-batch
    test happened to fail against the old code too. Type:
    `tooling-footgun` — checkout restores the last COMMIT, and on an
    uncommitted file that is someone else's baseline. Prevention: RULE —
    commit (or `git stash push`) BEFORE mutation-testing; the mutation
    then reverts with `git checkout`/`git restore` safely, and the
    verification run is against exactly what will ship.

19. **2026-08-05 — a session-tied background task is not a launch: the
    08-04 20:42Z manual tradepass drain died with the session that
    started it, silently, before sweeping one market.** The prior turn
    launched the 120-min deadline-boxed drain as a harness background
    task and ended; `trades_swept` shows the last row at 15:24Z (the
    timer run's crash) and the pending count unchanged at 16,868 —
    zero progress, zero captured output, discovered only because the
    next turn's gate-check queried the DB instead of trusting the
    status page's "launched". Type: `ops-blindness` — same family as
    #5 (uncaptured long jobs), new mode: the harness tracks the task
    only while the session lives. Prevention: RULE (ops.md) — any job
    meant to outlive the turn goes through `systemd-run --user`
    (transient unit, journald-captured, session-independent); harness
    background tasks are for work you will personally await this turn.
    The relaunch (02:17Z, unit `hyxlab-tradepass-drain`) applied it.

20. **2026-08-10 — an ad-hoc read-write connect to the shadow ledger
    killed the daemon: run 20260808T063109 died at 1d20h on an
    unhandled persist-time lock conflict.** At 02:16:14Z shadow's
    `ledger.persist` hit `duckdb.IOException: Could not set lock on
    hyxshadow.duckdb` — the lock was held by an ad-hoc python process
    (almost certainly the prior status pass querying the ledger with a
    default read-write `duckdb.connect`). The daemon had no handler on
    the persist path, exited 1, and systemd's restart opened a NEW run
    — ending the accumulating settlement-cohort series one day before
    its second cohort read. Two compounding causes, both known
    classes: (a) writers must hold-for-retry on lock declines (streamd
    got this 2026-07; shadow's ledger never did — item 12's family);
    (b) ad-hoc readers must connect read-only via
    `hyxlab.store.connect_retry` (the 07-12 recurrence-audit rule —
    this is its first WRITE-SIDE casualty: the reader's default-mode
    connect was the lock HOLDER, not the victim). Type:
    `incomplete-hardening` + rule regression. Prevention: persist
    decline now held-for-retry (regression-tested, counters advance
    only on success); ops.md rule extended — ad-hoc queries on ANY
    live DB (archive, stream, shadow ledger) are read-only, no
    exceptions.

21. **2026-08-12 — counting a journal signal by paraphrase instead of
    the literal log string produced a false zero, twice.** Monitoring
    passes track "streamd flush declines", but the actual journal line
    is `flush FAILED (...); N rows held for retry` — there is no word
    "decline" in it. The 02:35Z audit had already corrected one
    undercount (grep-window artifact); the 14:30Z pass then reported
    "ZERO new declines since 08:25" while the journal shows three in
    that window (08:40:46Z, 08:56:38Z, 10:00:01Z) — a phrase artifact
    this time. Same family as item 12: a tracked operational metric is
    a measurement, and a grep pattern is part of its definition. Type:
    `wrong-assumption` (measurement-by-paraphrase). Prevention: the
    canonical command is now recorded on the status page — count with
    `journalctl --user -u hyxlab-stream | grep "flush FAILED"`, never
    a paraphrase; when a tracked count reads zero, re-derive the
    pattern from source (`collector/streamd.py` flusher) before
    trusting it.

22. **2026-08-13 — journalctl `--since`/`--until` interpret bare
    timestamps in LOCAL time (box is UTC-5), while every tracked mark
    on the status page is UTC.** An 08:15Z pass queried
    `--since "2026-08-13 06:00"` intending 06:00Z and got `-- No
    entries --` — that timestamp is 11:00Z, the future. An empty read
    from a window mistake is indistinguishable from a true zero (same
    trap as item 21's false zero). Caught in-pass because a daemon
    known to be logging "had no entries". Type: `wrong-assumption`
    (clock-domain mismatch in a measurement). Prevention: journal
    windows use explicit UTC (`--since "2026-08-13 06:00 UTC"` works)
    or `journalctl --utc`; and an empty journal read over a window
    that should contain routine lines (stats every 5 min) means the
    window is wrong, not that the daemon was silent.

23. **2026-08-22 — a five-night failure model was never tested outside
    the window it postulated, and the fix it justified moved a timer
    for nothing.** The poly keyset walk logged `INCOMPLETE` every
    night at ~05:04Z. Four nights of that clock, with the failing page
    varying (11,600 / 11,700), was read as "the constant is the CLOCK,
    not the page" — a daily Gamma fault window — and the conclusion
    was written into the function docstring, a longer retry ladder
    (7 attempts, ~18 min), and a promoted timer shift (05:00Z ->
    04:15Z) meant to duck it. The very next walk died at 04:19Z, same
    page, and the one after that too. The model had survived five
    nights only because nobody had ever run the walk at any other
    hour: the discriminating experiment was a single daylight replay
    of the FULL walk, ~3 minutes, available from day one. Run at
    08:2xZ it reproduced the 500 on demand, and three volume-banded
    walks then pinned the trigger to each walk's own volume floor —
    Gamma 500s on the last page of a long chain instead of returning
    `next_cursor: null`. The nightly clock was never the constant; the
    *floor* was, because `min_volume` is the same every night. Type:
    `wrong-assumption` (a hypothesis confirmed only on data that could
    not discriminate it). Prevention: **a failure model that predicts
    "only under condition X" is not adopted until it has been tested
    under NOT-X.** Repeated observations under X are not evidence; the
    single cheap negative control is. Corollary, from the cost here: a
    remediation shipped on an untested model (the ladder, the timer
    shift) buys nothing and makes the model look load-bearing — one
    replay before promoting would have saved both. Note the earlier
    single-request replay of the failing *cursor* returned 200 and was
    read as supporting the window; a single request is not the walk,
    and it discriminated nothing.

24. **2026-08-23 — six status passes compared a curve across hours by
    each hour's MINIMUM, and narrated the resulting artifact as a
    market feature.** The shadow equity curve was reported with lines
    like "a NEW RUN LOW −301.3 (17Z)" and "08-21 traced the same
    15-17Z dive to −296.9", and a repeating afternoon "trough" was
    entered in the next-pass queue three times as a specific question
    to chase. But the daemon persists ~177 equity points an hour, so
    an hour's `min` is an extreme of that hour's mark NOISE — and
    min-sampling therefore manufactures depth in proportion to
    volatility, not level. `simulator/shadow_diurnal.py` measured it:
    mean intra-hour range is 29–64 overnight against 253.2 at 17Z and
    301.8 at 20Z, and the close-minus-low gap is ~225 in exactly the
    two hours being quoted against 12–27 overnight. 20Z — the loudest
    hour of the day — reads −153.1 at its minimum and **+70.0 at its
    close**. Read at the close there is no dive: one smooth daily
    oscillation, +72 (03Z) → −247 (16Z) → +150 (22Z). Type:
    `wrong-assumption` (a statistic compared across buckets with
    unequal dispersion). Prevention: **fix the sampling convention
    BEFORE comparing a curve across buckets, and publish the
    dispersion in the same row as the level.** The failure mode is
    that min/max feel like observations rather than order statistics,
    so nobody asks what they are conditioned on. Note the near-miss
    generality: the same passes also quoted whole-run `equity_min`
    from `shadow_attribution`, which is legitimate (one bucket, no
    cross-comparison) — it is the cross-hour comparison that was
    invalid, which is why this recurs so easily. Same family as the
    matched-scope trap (settled-subset payout vs whole-book cost) and
    `flagged_day_weighted`: the statistic was fine, the denominators
    were not equal.

25. **2026-08-23 — an enumeration tripwire measured price ACTIVITY, sat
    pinned just above its own floor for a structural reason, and was
    reported as a PASS for six weeks.** `poly swept universe not
    shrinking` was added 2026-07-11 as the remediation for the Gamma
    offset cap (item 5's near-miss: a lucky dead probe, not QA, caught
    the universe halving from ~4600 to ~2000). It read `poly_prices`,
    whose `ts` is a CLOB PRINT time — so it counted markets that TRADED
    on a day, not markets the sweep ENUMERATED. Because later sweeps
    backfill history, a day's count keeps growing for days afterwards,
    so the newest complete day is always the least-filled one and the
    ratio decays toward the floor by construction: 10,984 nine days
    back against 6,302 "yesterday", with nothing wrong. It could only
    ever have caught a halving of a halving. Type: `wrong-assumption`
    (a proxy column mistaken for the quantity it proxies). Same family
    as items 14/16 — a check that cannot go red — but the new part is
    the TELL: the ratio was reported as 57% of peak in a passing check
    and nobody asked why a green check lived that close to its
    threshold. **Prevention: a tripwire's headline number is itself a
    datum. If it sits near the threshold for many consecutive runs,
    that is a finding about the CHECK, not reassurance about the
    system.** And the remediation for a silent-drop incident must be
    tested against a synthetic drop of the size that actually happened,
    on the real column — the 07-11 tests seeded `poly_prices` directly
    and so validated the arithmetic while never touching the
    proxy assumption underneath it. **Sharpest detail: the maturation
    effect was ALREADY documented** in `data-pipeline.md` — "Poly
    day-bucket counts MATURE for ~2 days ... the shrink tripwire's 0.5
    threshold absorbs it." The observation was right and the conclusion
    was wrong: a fixed threshold cannot absorb an UNBOUNDED drift, and
    writing down the confound next to a reassurance retired the
    question instead of opening it. When a note says "X biases this
    check, but the threshold absorbs it", that is an unverified claim
    about magnitude and needs a number.

26. **2026-08-23 — a table added after the tz migration re-acquired the
    exact bug the migration existed to undo.** `insert_poly_stats`
    passed an aware-UTC datetime straight to `executemany`; DuckDB
    converts aware values to the BOX's local time on a naive TIMESTAMP
    column, so `poly_market_stats.ts` sat `America/Chicago` behind
    every other timestamp in the archive — the sweep stamped
    `2026-08-22 23:15` actually started `2026-08-23 04:15 UTC`. The
    convention was already documented in `_naive_utc`'s docstring and
    already enforced by `migration_1` on five columns; this table
    simply postdated it and no test covered its writer. Type:
    `regression` (a fixed class recurring in new code). Prevention:
    per the standing "recurrences jump straight to test" rule, the
    guard is now a writer-level test rather than a docstring —
    `test_poly_stats_stores_naive_utc_not_box_local`. **Generalize: a
    migration fixes ROWS, not the CODE PATH, so every migration needs a
    paired test on the writer or the next table reintroduces it.** A
    sweep of all 19 TIMESTAMP columns confirmed this was the only
    survivor. Second-order finding from the same pass: `promote.sh`
    ships code but never migrates, and nothing asserts the schema
    version at open — so a shipped migration could sit unapplied
    indefinitely while reads silently used the old convention. Now
    checked in QA (`archive schema at current version`).

27. **2026-08-23 — a pre-registered backtest burned 2h43m of replay and
    died in its own summary statistic.** The FavLongTight runner
    reached its report block after 9.3M candle-snapshots and raised
    `TypeError` from `statistics.median` over `close_time`s (median
    averages the two middle values on an even n; datetimes do not add).
    No verdict was produced, so nothing was rescued and nothing
    decided — the cost was purely the lost pass. The strategy had nine
    tests; `_band_block`, which turns its fills into the registered
    verdict, had zero. Type: `test-coverage`. Prevention: **the
    reporting stage of an expensive run is the part most worth unit
    testing, because it is the part reached last and therefore
    exercised least.** Cheap rule of thumb: if a code path can only be
    reached by spending hours, it needs a fixture that reaches it in
    seconds. `tests/test_hyxlab_favlong_tight_report.py` drives every
    threshold path in 0.2s; six of its nine tests fail against the old
    call.

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

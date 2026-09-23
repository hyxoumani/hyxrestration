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

28. **2026-08-23 — a mean over days was published as "the honest daily
    shape", and no day traced it.** EXP-1354 correctly killed the
    min-sampling artifact and replaced it with the hour-END series,
    then quoted the result as a specific curve: "+72 (03Z) → −247 (16Z)
    → +150 (22Z), one clean daily oscillation". Those three numbers are
    hour-of-day MEANS over 2–3 days. Measured per day (EXP-1357), the
    troughs are −269.9, −224.6 and −551.9, the peak hours are 22Z, 21Z
    and 00Z, and the weakest pair of days ranks at rho 0.262 — the
    shape DOES NOT REPEAT. The status page then queued "does the cycle
    repeat on 08-23" as the follow-up, a question the report that
    raised it structurally cannot answer, because averaging is the
    operation that destroys the evidence for recurrence. Type:
    `wrong-statistic`. Same family as #24, one dimension over: there
    the denominators were unequal, here the aggregation answers a
    different question than the one being asked of it. Prevention:
    **before quoting an aggregate as a shape, ask whether the
    disaggregated draws agree — and publish the agreement statistic
    NEXT TO the aggregate, not on request.** `shadow_diurnal.by_day`
    now does this (pairwise Spearman, UNSCORED below 12 shared hours,
    UNDERPOWERED below two scorable pairs), and the lesson generalises
    to every hour-of-day, per-category and per-band mean in the repo:
    a mean is a level, never a pattern.

29. **2026-08-23 — a QA check bounded the SUM of a signal and an
    unbounded drift, so it watched the drift.** `trade latency p99 sane`
    asserted `-2 < p99(recv_ts - src_ts) < 25`, commented "25s allows
    for the known ~20s box-clock skew until NTP lands". It had been red
    for many passes and nobody had costed it. `recv_ts - src_ts` is not
    latency: it is (box clock offset + transport latency). Measured
    over 12.6M kalshi trades in 24h, p01 25.55s / p50 25.71s / p99
    25.89s — the ENTIRE distribution is a 0.34s band sitting at +25.7s.
    So the check tracked the clock, at ~150x the amplitude of the thing
    it was named for, and once the offset ate the headroom a genuine
    stream stall would have been invisible underneath it: the check was
    not merely red, it was BLIND. Type: `wrong-statistic` + the #25-27
    drift family. Prevention: **when a measured quantity is a sum of a
    signal and a nuisance term, bound them separately — a difference of
    two quantiles of the same window cancels any constant offset.**
    Split into `trade latency dispersion sane` (p99-p50, offset-
    invariant, measured 0.03-0.18s, bound 5s) and `box clock offset
    within tolerance` (the offset, named for what it is). The second
    bound is ASYMMETRIC because the directions cost different things,
    and the cost was measured rather than assumed: a FAST clock only
    makes `sim._maker_check_and_expire` discard snapshots near the
    close, and ZERO of 1,141,594 pre-close kalshi snapshots over 7 days
    land within 26s of close (1,061 within 5 min) — the +25.7s offset
    costs the sim nothing. A SLOW clock is the dangerous side: it
    stamps post-close snapshots as pre-close and feeds the sim real
    lookahead. Hence floor -2s, ceiling 60s. Corollary worth carrying:
    **"cost it or retire it" is the right demand of any permanent red,
    and the cost has to be a measurement, not a paragraph.**

30. **2026-08-24 — every freshness check in the suite is
    INSTANTANEOUS, so an outage that heals is invisible by
    construction.** The wiki carried "the ~4h20m pre-reboot shadow
    silence on 2026-08-20, still unexplained" as an open item for four
    passes. Two things were wrong before anyone even looked at the
    cause. (a) It was read as SHADOW's silence, but the number came
    from `shadow_runs.anchor`, which is `max(recv_ts)` in the STREAM db
    at shadow's first poll — it reports how old the stream's head was,
    not how long shadow was quiet. (b) It was read as "pre-reboot",
    but the box died AT 21:33:37Z and returned at 01:52:51Z: the
    silence IS the downtime and the reboot is its END, not a
    subsequent event. Three independent writers stop within 10ms and
    resume within minutes of each other — two separate Kalshi WS
    channels, the polymarket stream, and the collector timer writing a
    different database — which no single-process fault produces.
    Type: `wrong-attribution` (a derived quantity read as if it
    measured the subsystem that stored it) + `missing-check`.
    The deeper failure is (b)'s cause: `collector fresh (snapshots <
    20 min old)` and `stream fresh (trades < 5 min old)` answer "is it
    collecting NOW", QA runs once daily at 10:00Z, and this outage
    healed 8h before the next run. **No check in the suite could ever
    have seen it**, so a 4h19m whole-box outage was recorded as a
    vague adjective instead of an alarm. Prevention: **an
    instantaneous check on a periodically-sampled monitor cannot
    detect anything shorter than its own sampling period; the
    retrospective form is a separate check, not a tuning of the same
    one.** `collection continuous over last 24h` (EXP-1359) bounds the
    largest inter-cycle gap over the window, anchored on the newest
    cycle BEFORE it so a straddling outage is not lost with its
    predecessor. Budget measured, not argued: 21 days / 6,040 cycles
    give p50 300.0s / p99 314.0s / p99.9 600.0s, worst benign gap
    25.0 min against the outage's 264.8 min; bound 60 min. Corollary:
    **before reading an unexplained number, check what the column that
    produced it actually measures** — the same discipline #29 applied
    to `recv_ts - src_ts`, one level up.

31. **2026-08-24 — a freshness check measured a stamp that is
    deliberately in the FUTURE, and pooled seven cadences into one
    max, so it printed a negative age and passed while four of its
    seven series sat past its own budget.** `econ vintages fresh
    (< 8 days)` computed `now - max(knowable_at)` over all of
    `econ_vintages` and read **age -0.6d — PASS**. Two independent
    defects, both visible in that one number. (a) **The nuisance
    term.** `knowable_at` is not an ingest time: ALFRED vintages are
    date-granular, so `alfred.pessimistic_knowable_at` stamps the
    vintage date's 23:59 US/Eastern (= vintage_date+1 03:59 UTC), a
    deliberately LATE stamp so no backtest can see a print before a
    live trader could. It therefore leads the fetch by up to ~28h, and
    the check was measuring (ingest staleness − pessimism margin). **A
    freshness measure that can go negative is not measuring
    freshness** — and the margin is not cosmetic: a 5-day outage reads
    inside a 4-day budget. (b) **Pooling.** A max over seven series
    whose print cadences run daily (DFEDTARU/DFEDTARL) to monthly
    (CPIAUCSL/CPILFESL/PAYEMS/UNRATE) is set by the fastest one,
    always. On 2026-08-24 the daily pair read 0d while the other four
    sat **10.2d, 10.2d, 15.2d and 15.2d** — every one of them past the
    check's own 8-day bound, under a green line.
    Type: `wrong-statistic` (#24/#28 family — a pooled aggregate over
    heterogeneous members reports the healthiest member, never the
    fleet) + `wrong-attribution` (#29 family — a stamp read as if it
    were an ingest time).
    Prevention (EXP-1360), and the split is the point: pooling is
    honest for exactly one question, so `econ pull live (any series,
    last vintage date)` asks only that one, on the vintage DATE
    recovered from the stamp — which cancels the nuisance exactly,
    because the stamp is a deterministic function of it. Budget
    measured, not argued: 44 distinct vintage dates over 2026-07-11 ..
    2026-08-24 (45 days), the only gap above one day being a single
    2-day gap at 07-13; bound 4 days = 2x worst observed.
    **The per-series question cannot be answered from the archive at
    all**, and that is the finding worth carrying: `econ_vintages`
    gains a row only when a value CHANGES, so a monthly series that
    ALFRED dropped and a monthly series that has not printed yet are
    the same table for a month — and `fetch_alfred` retries three
    times, prints, and moves on with the series simply absent from its
    result. So `collector.signals.record_fetch` now writes per-series
    fetch outcomes to `data/signals_fetch.jsonl` and
    `qa_signals_fetch` reads them, deciding an absent sidecar against
    the archive as an independent witness (#EXP-943's shape) — with a
    36h grace on the never-produced case, because "the recorder is
    dead" and "the recorder shipped an hour ago" are the same empty
    file until one pull cycle has had time to fire.
    **Honest limit: this is a detector, not a rescue.** The sidecar
    starts empty, so the first per-series verdict lands one pull after
    promote, and nothing here recovers whether the four stale series
    were being fetched during the days they sat quiet.

32. **2026-08-24 — a tier's headline count was a BOOLEAN over three
    outcomes that mean opposite things, so "zero survivors" was read
    five times as a measurement when it was mostly silence.** The
    calibration atlas's strictest tier, `flagged_quoted`, re-runs the
    day-weighted test on the subsample whose books were actually
    two-sided. It has read **0** on every reading since it shipped
    2026-08-02, across settled markets growing **165,814 -> 1,592,941
    (9.6x)** and the day-weighted tier growing **6 -> 22**. A bucket
    fails that boolean three ways: its gap **REVERSES** on quoted
    books (evidence AGAINST the signature), its gap collapses inside
    its own interval (weak evidence against), or it never had
    `MIN_N=200` quoted observations to test (**no evidence either
    way**). One bit cannot say which. Decomposed on the 08-24 archive:
    **19 of 22 survivors are SILENT** (quoted_n 30–191 against the 200
    bar) and only **3 were ever tested**, all three failing on the
    interval after their gap shrank 3.1–4.1x. Six of the 19 untested
    buckets **reverse sign** on their quoted point estimate — evidence
    against, invisible under the boolean. Median gap retained on
    quoted books across all 22 is **0.4215**, range [−1.0323,
    +1.0684].
    **The self-implicating part**: the 2026-08-02 pass DID separate
    these three states, by hand, in prose, over six survivors, and
    wrote down that MIN_N left most buckets silent. That decomposition
    was never encoded, so the next five readings printed one number
    and the log narrated "reads ZERO for the Nth consecutive reading"
    while the untested share grew. **A finding that lives only in the
    prose of the pass that found it does not survive its own author.**
    Type: `wrong-statistic` (#24/#28/#31 family — here a tri-state
    collapsed into a boolean rather than a pooled aggregate, but the
    same failure: the output cannot distinguish members that mean
    opposite things) + the `a-skipped-check-is-not-a-passed-one`
    family (silence read as rejection).
    Prevention (EXP-1361): every bucket carries `quoted_status`
    (`confirmed` / `not_significant` / `refuted_sign` / `silent` /
    `not_applicable`) plus `quoted_gap_dw` and `quoted_gap_retained`,
    and the report carries `quoted_verdict`, whose counts **PARTITION**
    the day-weighted tier by construction — the arithmetic is what
    stops the zero being read as a measurement again, and a test
    asserts the partition on a fixture holding all three outcomes at
    once. `wilson_quoted_lo/hi` are `None` when the test did not run,
    rather than the `(0.0, 1.0)` that printed as a test that ran and
    found the implied comfortably inside its interval.
    **NOT TUNED**: `MIN_N` stays 200 on the quoted subsample —
    lowering it to reach a verdict would be fitting the threshold to
    the answer. The point is to REPORT the silence, not abolish it.
    Three mutations checked and each reddens: folding silent into
    not_significant, restoring the (0.0, 1.0) interval, and folding
    refuted_sign into not_significant.
    **Honest limit**: this changes what the report SAYS, not what the
    archive knows. The 19 silent buckets stay silent until quoted
    observations accumulate; the conclusion's direction is unchanged
    (no bucket with quoted evidence supports the longshot-fade
    signature) and only its strength is corrected downward.

33. **2026-08-25 — the SAME boolean-over-a-tri-state defect was live at a
    second site, and #32's fix was applied only where #32 was found.**
    The maker bracket's `direction_market_significant` /
    `direction_underlying_significant` are booleans whose `False`
    covers two opposite readings: a run that TESTED a fill-model
    direction and found none (evidence against a bias), and a run whose
    power ceiling `min_sign_p = 2^-k` already exceeded `SIGN_ALPHA`, so
    no data could have produced a verdict (**no evidence either way**).
    The ceiling had been computed and reported since 2026-07-31 and the
    docstring explained how to read it — but the comparison against
    alpha was left to the reader, and the summary field collapsed it.
    The sharpest form of the miss: `strategy-verdicts.md` tells the
    reader "read `underlying_sign_p`, `underlying_min_sign_p` and
    `direction_underlying_significant`, **not** `robust`" — and the
    third of those three is the field that collapses the tri-state. The
    07-31 pass even measured the retrospective damage (31 of 34 runs
    underpowered by construction) and still shipped the boolean as the
    thing to read.
    Measured across the 7 archived reports that carry the sign fields:
    **5 of 7 underlying-tier readings were UNDERPOWERED**, including
    both 08-03 runs that read `significant_over` at the market tier
    while the underlying tier could not have reached a verdict at all,
    and the 08-06 run whose `net_disagreement = -71` has been carried
    in the log as an under-award lean.
    Type: `wrong-statistic`, #32's family at a second site.
    Root cause is the escalation rule, not the statistic: **#32 was
    fixed as an instance rather than as a class.** A defect found by a
    lens ("does this summary field distinguish members that mean
    opposite things?") should be swept against every other summary
    field in the repo the same pass, or it is only rediscovered when
    someone happens to re-run the other report.
    Prevention: both tiers carry `direction_*_status`
    (`significant_over` / `significant_under` / `not_significant` /
    `underpowered` / `no_direction`), and the report carries
    `direction_verdict`, whose counts **PARTITION** the four
    tier x bound readings by construction, with `powered` = the number
    of readings that could have rejected at all. `significant` is left
    byte-identical for cross-report comparability, and a test asserts
    `status` is a strict REFINEMENT of it rather than a second opinion.
    Four mutations checked and each reddens; a fifth (`>` vs `>=` at
    the alpha boundary) is recorded as **unreachable** — `min_sign_p`
    is `2^-k` and never equals 0.05 — rather than pinned with a fixture
    that cannot exist.
    **Honest limit**: this changes what the report says, not what the
    stream knows. It does not make the 5 underpowered readings
    informative; it stops them being read as null results. The one
    thing that WOULD widen them is a larger `--markets`, which is a
    configuration change with its own cost, not a restatement.
    **The sweep this entry demanded, run the SAME pass — and it found a
    THIRD instance immediately, in the atlas, one tier below where #32
    was fixed.** The BASE tier's `flagged` is
    `n >= MIN_N and implied outside the Wilson interval`, so its False
    covers a bucket that was never tested and a bucket that was tested
    and came back calibrated. On the 08-24 archive **200 of 395 buckets
    sit under MIN_N**: the standing headline "141 flagged of 395
    buckets" is 141 of **195** tests. Reported honestly the flagged rate
    is **72.3%, not 35.7%** — the number roughly doubles. Fixed the same
    way in the same pass (`flag_status` per bucket, `flag_verdict` with
    counts that partition the bucket set and a `tested` denominator,
    `flagged` untouched, three mutations reddening). That the sweep paid
    off within minutes is the evidence for the rule: **fix the class,
    the same pass, or the next instance waits for an accident.**

34. **2026-08-25 — a bounded-memory fix was applied as an INSTANCE, and
    the second caller of the same unbounded loop OOM-grew for five
    weeks.** `simulator.divergence` could not finish: its last completed
    run peaked at **14.3 GB**. Two independent causes, and BOTH were
    already-known classes applied at only one site.
    **(a) One materialised sort.** `_events` issued a single cursor with
    `ORDER BY recv_ts, seq` over the whole replay window. DuckDB
    materialises that sort, so peak memory is LINEAR IN RUN LENGTH —
    measured 3.14 GB for 18.5M rows, ~12.5 GB linear-scaled to the 73.1M
    of the 10.5-day default target. Walking the window in 6h slices,
    each sorted separately, holds the SORT's contribution flat (0.94 GB
    on the same 18.5M rows) at no wall-clock cost.
    **(b) The shadow OOM fix, never swept.** `sim.step` appends one
    equity point PER SNAPSHOT. `simulator/shadow.py` trims that curve
    after every poll, carrying a comment that names the mid-run kernel-
    OOM kill it was written for (2026-07-18, ~800 MB in 2.3 days) and
    two tests pinning it. `replay_run` steps the SAME sim over the SAME
    stream for a window as long as the longest shadow run — and never
    got the trim. It is pure ballast there: divergence compares fills,
    never calls `finalize()`, and never reads the curve.
    **(c) AND THE SORT DEFECT WAS LIVE AT A SECOND SITE TOO.**
    `simulator/run_l2.py` carried a near-verbatim COPY of the walk, so
    fixing (a) in divergence reached only one of the two. Patching it
    twice would have preserved the cause; the walk is now unified as
    `simulator.bookreplay.stream_events` and both local copies deleted.
    Unifying it surfaced a latent bug present in NEITHER original: the
    merged version first bound the `prefix` filter by positional slice
    (`params[2:]`), correct only when `hi` is given — with an open `hi`
    an `--markets`-scoped backtest would have SILENTLY widened to every
    market on the venue.
    **(d) AND A FOURTH, ON A DIFFERENT LOCK, found by RUNNING the
    verification rather than reading the code.** `replay_run` attached
    the LIVE archive with a bare `Store(archive_db, read_only=True)` — no
    retry, no degrade — and died at 08:58:26Z against `collector.
    poly_sweep` at 4h43m of a ~7h write. This is the class the 2026-07-12
    audit escalated to `connect_retry` after it fired three times in one
    day, and divergence was one of those three sites: fixed then for the
    STREAM attach, in this same function, two lines below the archive
    attach that was not.
    **CLASSIFICATION: `fix-the-instance`, the same failure as #32/#33 in
    a new domain — and my own first attempt at the entry committed it
    AGAIN, asserting "exactly two callers, both now bounded" while (c)
    and (d) were live.** Stating the rule is not running it. That the same shape has now appeared three times
    in two passes, in two unrelated subsystems (report summary fields,
    unbounded accumulation), is the argument for the standing rule
    rather than for three separate patches.
    **RULE (escalated from #32/#33's report-field scope to the general
    one): a fix is not done at the site where it was found. Before
    committing, enumerate every other caller/field/tier the same lens
    applies to, and sweep them THE SAME PASS.** #33 waited for someone
    to happen to re-run queuescore; this one waited five weeks for a
    report to be declared stale. Neither was found by the fix that
    should have found it.
    **AND THE COROLLARY THIS ENTRY EARNED THE HARD WAY: a sweep is only
    finished when the enumeration is written down.** Two of the four
    sites here were found AFTER the sweep was declared complete — one by
    grepping the callers (which takes a minute), one by running the
    thing (which takes 35). Prefer both to a confident sentence.
    **SECOND, SMALLER LESSON: a retry budget is calibrated against a
    reader, and the writer changed underneath it.** `connect_retry`'s
    15 x 2.0s = 30s was sized for "readers attach briefly". Against
    `collector.streamd`, which never stops, **4 of 5 real attempts died
    on a clean IOException after 30s** despite the lock sampling free
    87% of the time — a flat period can beat against a flush period
    rather than sample it. Budgets inherit an assumption about the other
    side; when a 24/7 writer appears, re-derive them rather than reuse.

35. **2026-08-26 — the fix for #24 replaced a bad statistic with one
    that carried a DIFFERENT composition defect, and the surviving
    diurnal claim was an artefact of it for three more days.** #24
    killed min-sampling and told every reader to take the LEVEL off
    `mean_equity_end`. That column is a mean over whatever DAYS that
    hour-of-day happened to have — and a run does not begin or end on
    an hour-of-day boundary, so on run `20260823T201714` 19Z had three
    readings and 20Z had two. Harmless if level were stationary; the
    daily level slid from -50 to -1800 across the run. **MEASURED:
    `mean_end` steps +433.6 from 19Z to 20Z, which reads as a nightly
    recovery; on the two days both hours SHARE it is -14.6. The step
    was 97% composition.** The "peaks 21-00Z" half of the surviving
    claim was made of exactly this — those hours silently included the
    run's opening day, when equity was still near zero. On the balanced
    panel the level declines monotonically all day and no peak exists.
    **The generalisation is not about equity.** Any panel mean compared
    ACROSS cells must hold its population fixed; when the cells draw
    from different populations, a difference between them is partly a
    difference of WHO, and nothing in the number says which part.
    **RULE: a report that publishes a mean per bucket must publish the
    bucket's population too, and any cross-bucket comparison must be
    made on the intersection.** Encoded, not remembered:
    `level_panel` + `mean_equity_end_balanced` + `balanced`, three
    tests, mutation-verified three ways. The raw column is kept —
    it is the wider sample for reading ONE hour — but the printed
    table leads with the balanced one and stars every ragged cell.
    **AND THE PROCEDURAL LESSON: the claim was queued as "re-run
    out-of-sample" for six passes and each pass deferred it behind a
    clock.** It took 20 minutes once run. A claim cheap to test and
    repeatedly deferred is a claim being protected; test it the pass
    you notice it.

36. **2026-08-26 — a guard asserted MORE than the hazard it defended,
    and reddened at the promote gate on a busy box.**
    `test_the_file_lock_excludes_nothing_between_bursts` (EXP-1372)
    proves a second stream writer is NOT excluded between flushes. It
    asserted the newcomer landed **all six** flushes with zero declines.
    But the hazard is that the newcomer gets IN — one landed flush is
    already a duplicated book in a table with no key. The extra clause
    measured whether the newcomer's flush ever collided with the
    incumbent's OPEN burst, which is scheduling luck: green 5/5 idle,
    red at 3/6 and 5/6 with eight cores busy. It failed inside
    `scripts/promote.sh`, minutes after passing standalone — i.e. at
    the one moment the suite is a gate rather than a report.
    **RULE: assert the hazard's threshold, not the comfortable margin
    you happened to measure.** A guard that encodes the sample instead
    of the claim is a flake with a good docstring, and it spends its
    credibility exactly when the suite is load-bearing. Before pinning
    an `==`, ask what value would falsify the CLAIM: here it is zero
    landed flushes, so `>= 1` is the assertion. Cheap check: run the
    new guard once under load.

37. **2026-08-27 — "both local copies deleted" was two of THREE, and the
    one that got away was the daemon.** #34(c) unified the walk over
    `book_events` into `simulator.bookreplay.stream_events` and recorded
    that the duplication is why the memory fix reached only one caller.
    The sweep covered `divergence` and `run_l2` — the two sites that
    LOOKED like the report it was fixing. `simulator/shadow.py` carried a
    third copy, in its boot seed, and kept it for two days across two
    memory rungs that both went through the unified walk instead.
    MEASURED on the live archive at shadow's 512 MiB engine limit, 72 h /
    23.4M rows, identical rows out: the copy peaks at **1826 MiB of
    spill**, the unified walk spills **nothing**, same wall clock. Under
    the 1 GiB service cgroup of EXP-1375 the same shape raised
    `OutOfMemoryException` at 24 h.
    **AND UNIFYING IT SURFACED TWO BUGS PRESENT IN NEITHER OTHER SITE**,
    the same way #34(c)'s merge did: the seed had no upper bound, so
    rows landing between the anchor read and the walk were applied by the
    seed AND again by the first poll; and the seed's floor, a gap's
    `ended_at`, is INCLUSIVE — the half-open walk drops the reconnect
    image that a seq_reset gap ends at, which is the row that re-seeds
    the book. `divergence` had inherited the second one by copying the
    call shape.
    **RULE: a de-duplication sweep is finished when a GUARD says so, not
    when the greps you thought of come back clean.** The three copies did
    not share a distinctive string — the daemon's was spelled as a
    hand-rolled `conn.execute`, which is what every DuckDB read looks
    like. Escalated to a test: `test_both_seed_sites_ask_for_an_inclusive
    _floor` enumerates the seed sites by AST, so a fourth one is a red
    suite rather than a discovery. Sweep by ROLE ("who seeds a book from
    the archive?"), then pin the answer.

38. **2026-08-27 — a "prepend" to the living status page split on the
    entry separator and OVERWROTE the entry it meant to push down.** The
    rung-12 write did `head, rest = s.split('\n\n---\n', 1)` and then
    wrote `new + '---' + rest`, discarding `head` — which was the whole
    rung-11 pass. Caught immediately by `git diff --stat` reading
    **50 insertions, 76 deletions** on what could only be an append, and
    restored from `eaaeb36`; nothing reached origin unrecoverably,
    because the log is in git and the check was run before moving on.
    **RULE: a write that can only ADD must be verified to have only
    added.** For an append-to-top, the deletion count is the whole test
    and it costs one command. More generally, splitting a document on a
    separator to insert BEFORE the first section leaves the first section
    in a variable you then have to remember to re-emit — prefer
    `open(p,'w').write(new + old)` on the untouched original, which has
    no way to lose anything, over a split/rejoin that has one.


39. **2026-08-27 — mutation-testing restored the source with `git
    checkout`, which deleted the UNCOMMITTED fix the mutants were
    testing. Twice, in one pass.** The loop was `sed -i <mutation>; run
    tests; git checkout <file>` — correct only if the file's committed
    state IS the state under test. It was not: the fix itself was still
    in the working tree, so the first restore silently reverted three
    edited source files to HEAD, and the loop went on producing
    plausible red/green output about code that no longer existed. The
    tell was the last line: a "clean" re-run that stayed RED. Then the
    identical mistake in the next mutation round, on `scripts/promote.sh`.
    **RULE: mutate against a snapshot you made, not against git.** `cp
    file /tmp/file.bak` before the loop and `cp /tmp/file.bak file` to
    restore — the backup is of what you are actually testing. If git is
    to be the restore point, COMMIT FIRST, and then git and the working
    tree agree by construction. The second-order lesson is that a
    verification loop is source-mutating code with no test of its own:
    check `git status --porcelain` when it finishes, and treat a
    surprising final "clean" result as evidence about the harness, not
    about the code.

40. **2026-08-27 — "not mentioned" was read as "forgotten", and the fix
    re-litigated a decision a test had already recorded.** `promote.sh`
    restarted two of the three `Type=simple` daemons; `hyxlab-simui`
    appeared in neither the restart list nor the output, so every
    promotion since its install left it running its original code while
    the script printed "none — no daemon's code moved". The first fix
    added it to the restart list. `tests/test_systemd_units.py` went red
    on the spot: simui's exclusion is DELIBERATE — it holds a live paper
    session that a restart drops — and the exclusion was written down in
    the one place that can enforce it. The real defect was narrower than
    it looked: the deliberate choice not to act had been implemented as
    silence, and silence is indistinguishable from an oversight for a
    daemon that is also `Restart=always` and therefore has no other path
    to new code. It now prints a NOTICE with the by-hand command.
    **RULE: when a guard reds on your fix, read the guard before
    changing it — it is the previous pass talking.** And the guard's own
    lesson: a decision NOT to do something must be emitted, not merely
    encoded, or the next reader re-discovers it as a bug. The
    replacement rule partitions the daemons into auto-restart and
    notify-only, so a daemon may be either — but not neither.

41. **2026-08-28 — a profiler's own footprint was published as the
    subject's, and became the ladder's next rung.** Rungs 12-15 bounded
    heap terms with `tracemalloc` and, from the same instrumented runs,
    quoted a PROCESS figure: "the Python heap is 83.7 MiB but the process
    is 1,025.9 MiB peak RSS — the DuckDB engine is where a replay's
    memory is." The gap was set up as the top of the queue. Re-measured
    (EXP-1381), the identical `run_l2` peaks at **409.0 MiB with
    tracemalloc off, 590.9 MiB at `start()` and 638.7 MiB at
    `start(25)`** — 293,568 snapshots and the same answer all three
    times. Between 44% and 56% of the "process" number was the
    instrument, and the residual attributes completely by phase with
    nothing left over for the mystery the rung was named after. The
    direction was right (DuckDB under-reports itself: closing the stream
    connection returned 189.4 MiB while `duckdb_memory()` claimed 55.5)
    and the magnitude was fiction. Type: `wrong-assumption` /
    `missing-verification`. **RULE: an instrument that is on is part of
    the measurement.** Two instruments in one script is normal — the
    error is letting one script's attribution mode set another's
    headline. Prevention: `hyxlab/memprobe.py` — `process_peak()` RAISES
    while `tracemalloc.is_tracing()` unless the caller writes
    `allow_tracing=True`, and the reading it returns keeps `rss` and
    `traced_peak` as separate fields so neither can be reported as the
    other (`tests/test_memprobe_discipline.py`, 7 mutants). Second-order:
    a number that has been quoted forward across three passes has been
    re-READ three times and never re-MEASURED. **Before building on an
    inherited figure, reproduce it** — this ladder's own rule for
    suspects ("measure first") was never applied to its own baselines.

42. **2026-08-29 — the fix for #32 decomposed a reading and left the
    COMPARISON BETWEEN readings in prose, so #32's own lesson repeated
    itself one axis over.** #32's finding was that `flagged_quoted: 0`
    had been read five times as a measurement while most survivors were
    never tested, and its stated root cause was that the 2026-08-02
    pass's hand decomposition "lives only in the prose of the pass that
    found it". The fix — `quoted_status` per bucket, `quoted_verdict`
    counts that partition the tier — makes ONE reading honest. It does
    not make two readings comparable, and the question a reader actually
    has after the second `confirmed: 0` is whether the silence is
    receding. Measured this pass on +15% settled data (1,592,941 ->
    1,835,987): survivors **22 -> 28**, tested **3 -> 8**, silent
    **19 -> 20**, tested share **0.1364 -> 0.2857**, and the tier's
    first ever `refuted_sign`. Every one of those numbers came from
    loading two JSON files by hand — i.e. the 08-02 failure, reproduced
    by the entry written to prevent it.
    Type: `wrong-statistic`, #32/#33's family; the escalation miss is
    **scope**, not site. #34's rule ("enumerate every other
    caller/field/tier the same lens applies to") was applied across
    SITES and not across TIME.
    **RULE: when a per-reading decomposition replaces a headline, ask
    what the reader compares it to. If the answer is "the last
    reading", the trajectory is part of the fix.**
    Prevention: `verdict_stability` carries both partitions'
    trajectories with the **population beside every count** — a count
    moves because statuses changed and because the population moved
    under them, and nothing in the count says which (#35) — and a prior
    that predates the field or carries a different status set is
    **absent from the trajectory rather than a zero in it**, since
    fabricating that zero manufactures the very run of zeros the field
    exists to break up. Seven mutants, all red, including both
    zero-fills and a third `*_verdict` published without registering it
    (AST-enumerated, #37's shape).
    **Honest limit**: two readings carry the field (26 of 27 priors are
    silent about it), so the trajectory is a slope through two points.
    It is a field that will accumulate, not a trend that has been
    established.


43. **2026-08-29 — a report's DEFAULT path had never once run, so its
    verdict was always read off the weakest partition it contained.**
    `simulator.shadow_diurnal` publishes one profile and shape verdict
    per shadow run. Its all-runs default raised `TypeError` from
    `set.intersection(*[])` whenever the ledger held a run with no whole
    hour — 13 of 51 do — so it had never completed since the module
    shipped on 08-23, and every one of the six archived readings was
    taken with `--run` on whatever run was live that day. **The live run
    is always the shortest run**, so all six printed UNDERPOWERED and
    six status passes carried "wait for more days" off them, while the
    ledger already held four fully powered runs (up to 11 whole days and
    55 day-pairs) that all read DOES NOT REPEAT.
    Type: `unexercised-path` crossed with #32's family. The tests
    covered `build_diurnal` on synthetic single-run ledgers and the
    operators always passed `--run`, so nothing ever built the report
    the way the module's own `--help` says it runs by default.
    **RULE: a report that publishes one verdict per member must publish
    the CENSUS over members too, and the census is what gets printed
    first. A per-member verdict shown alone is the same sentence for a
    member with no power and for a population that has answered the
    question — and the member the eye lands on is the newest one, which
    is systematically the weakest.**
    Corollary, learned the expensive way: **exercise the default
    invocation on the real artefact**, not just the parameterised one
    the caller happens to use. The crash and the nine unread days were
    the same bug.
    Prevention: `power_census`, printed before any per-run table;
    `profile_status`/`shape_status` published beside the prose so a
    tally never parses a sentence; unscorable members held OUT of the
    partition rather than counted as zero-draw members (#32); every
    count carrying the spans that produced it (#35); and the pooling
    rule stated in the field — SHAPE pools across runs, LEVEL does not.
    Nine mutants, all red.

44. **2026-09-02 -- a restart guard that was right every time still
    starved the thing it guarded, because it asked only whether the
    code moved and never how old the run was.** `promote.sh` restarts
    `hyxlab-shadow` iff a promoted file is in the daemon's static import
    closure (EXP-1276). Measured over 51 closed shadow runs: 19 ended
    within 3 min of a promote, EVERY one of those promotes had moved a
    closure file, and ALL 15 of the runs the diurnal census calls
    day-starved (`no_balanced_panel`, median span 23.4 h, needing ~10
    days) ended with their successor writing within 4 min. Not one ran
    out of data. The promote header had already re-costed a restart in
    DAYS OF SPAN (08-23) -- and then left the decision to a rule that
    could not see span.
    Type: `right-predicate-wrong-question`. The guard's predicate was
    never wrong; the cost it was written to protect was not an input to
    it. Cousin of #43: a verdict read off the freshest member is
    systematically the weakest one, and here the freshest run was also
    the one every promote reset.
    **RULE: a guard that exists to protect an accumulated asset must
    take the asset's current size as an input, not only the trigger.
    "Should I restart" has two operands -- what moved, and what dies --
    and a policy that reads one of them is a policy that will pay the
    other every time it fires.**
    Prevention: `young_run_guard` in `scripts/restart_decision.sh`
    defers a shadow restart while the live run is younger than
    `YOUNG_RUN_S` (3 d), printing the age and the override; an unknown
    age never defers (protect a measured span, not an assumed one). The
    ledger publishes `lifetime.succession` per run and
    `stopped_not_starved` in the census so the famine is counted, not
    inferred -- and names WHAT followed, never WHO stopped it, because
    the ledger holds no exit reason (#32). Eleven mutants, all red.

45. **2026-09-09 -- the fix for #29 rewrote the two checks above a third
    one that had the SAME defect in its purest form, and left it.** On
    2026-08-23 the latency check was retired for bounding an unbounded
    drift with a constant, and the comment written to explain it -- "a
    threshold cannot absorb an unbounded drift ... each gets the bound
    its own failure mode earns" -- sits THREE LINES above
    `check("stream disk under 20 GB", size_gb < 20.0)`. A file that only
    grows is the unbounded drift with no nuisance term to separate out;
    it needed no measurement to condemn, only to be read. It crossed at
    **20.31 GB** and QA has been red ever since, and a check that can
    never go green again is not a weak signal, it is a mask: the
    breadth-truncation failure CLEARED in the same 10:00Z run, and the
    digest said `hyxlab-qa FAILED` before and after. The prior pass had
    predicted GREEN off breadth's own query and was right about breadth
    and wrong about the run.
    Two further errors were folded into the same line. (a) It was named
    `disk` and watched **8% of the disk it named**: `collector.backup`
    keeps a 7-slot rotation of every archive ON THE SAME FILESYSTEM, so
    each byte the tape gains is paid for eight times -- 20.3 GB live of
    a 262 GB `data/`. Measured off the rotation's own slots 09-02 ->
    09-08: stream +353 MB/d, hyxlab +249 MB/d, shadow +0.2 MB/d = 0.60
    GB/d live, **4.82 GB/d of disk**, against 1.216 TB free = **252
    days**. Free space was never in question and 20 GB was never the
    number. (b) It sat INSIDE `qa_stream`, which returns early when the
    stream archive is unreachable -- so the disk reading vanished
    exactly on the days the box was in trouble.
    Type: `wrong-statistic`, the #25-27/#29 drift family, plus
    `scope-blind-to-its-own-name`.
    **RULE: a check on a quantity that only moves one way must be
    bounded as a HORIZON, not a level -- `free / burn`, where burn
    includes every copy the system makes of the thing being measured.
    A level check on a monotone series is one-way by construction: it
    is either not yet firing or permanently firing, and it is a
    measurement in neither state. And when a fix retires one instance
    of a defect, grep the file for the rest of them before committing
    -- the sibling was three lines away for seventeen days.**
    Prevention: `qa_disk_headroom` is a filesystem-only section called
    FIRST in `main()`, before anything that can be gated by a lock. It
    derives its file list from `backup.DBS` (a fourth archive cannot be
    missed), multiplies the measured live rate by `1 + ROTATION_SLOTS`
    only while `st_dev` says the backups share the disk they would
    exhaust -- off-box, the standing user item, the same archive has a
    different honest horizon -- and takes its growth history from the
    rotation's own slots, so it adds no state. A partial series is
    `UNPROJECTED`, never an optimistic sum, because summing only the
    archives that HAVE history understates the burn and understating it
    is the reassuring direction; the fallback still bounds (can the disk
    hold one more full copy?) rather than shrugging. Nine tests,
    including one that fails if any `check(...)` line in `qa.py` ever
    again names a fixed GB ceiling.

46. **2026-09-09 -- the standing divergence report defaulted to the run
    with the MOST fills, so re-running it could not see new evidence.**
    The instruction that opens every autonomous pass says "re-run the
    standing reports on newly accumulated data and chase drift", and
    `simulator.divergence` -- the report that sets the fill-model
    calibration haircut applied to every backtest number -- picked its
    subject with `SELECT run_id FROM shadow_fills GROUP BY 1 ORDER BY
    count(*) DESC LIMIT 1`. An argmax over a record that only grows
    moves only when a BIGGER member appears, and bigger members get
    rarer as the record grows: the same one-way construction as #45's
    level check on a monotone series, wearing a default's clothes.
    Measured: the report had pointed at `20260810T081931` (54,007
    fills, ended 08-20, already reported 1.0/1.0 on 08-25) for THREE
    WEEKS while eight later runs went unmeasured -- among them
    `20260829T191841`, 38,143 fills over 8.8 days, the second-largest
    run in the record, never reported. The pass before this one
    launched the report, watched it print run `20260810T081931`, and
    did not notice that the report it had queued as "new evidence" was
    a re-run of a month-old window.
    The top end was worse than stale. The live run is always
    `max(started_at)` (one daemon, one owner lock), so once a live run
    outgrew the record the argmax would have selected IT -- replaying a
    moving `end` against a stream archive being written at that same
    boundary. The default was stale until the day it became unsound.
    Type: `argmax-over-a-growing-record`; the #45 monotone family.
    **RULE: the subject of a standing report must be selected by
    RECENCY, not by rank. A report whose job is drift has to advance
    when the record advances, and any `ORDER BY <size> DESC LIMIT 1`
    over an unwindowed history is a default that pins itself to the
    past. Rank is legitimate only inside a rolling window -- checked:
    `prioritycheck` and `queuescore` both bound theirs with
    `recv_ts > since`, so they advance, and neither needed changing.**
    Prevention: `latest_complete_run` -- newest run that is FINISHED
    and produced fills. "Finished" is read off the table rather than a
    heartbeat or a new column: a strictly later run existing proves the
    daemon restarted past this one, which excludes the live run by
    construction. It conservatively skips the newest run when the
    daemon is stopped for good, and that asymmetry is deliberate --
    skipping a measurable run costs a `--run` flag, replaying a live
    one costs the measurement. Zero-fill runs are skipped: a fill
    comparison over zero fills is not a zero divergence, it is no
    measurement. Five tests, including one that asserts the selection
    ADVANCES when a newer run finishes -- the property the argmax
    lacked -- and one that refuses the live run even when it is the
    biggest.
    **FOLLOW-UP 2026-09-10 -- fixing the SELECT fixed one of the two
    defects.** The pass above ends with "the line `[divergence] run
    20260810T081931` scrolled past unread", and treated that as a
    reading failure. It was not. The report had no scheduler and no
    consumer: it ran when a human remembered, and wrote its result to a
    JSON file that NOTHING in the project read, so the only reader that
    could ever notice a haircut ceasing to be zero was whoever opened
    the file. A correct default still cannot be seen by anyone. Both
    halves are now machinery -- `hyxlab-divergence.timer` (daily 01:20Z,
    `--if-new`, a sub-second no-op unless the shadow daemon has
    restarted) produces the measurement, and `collector.qa`'s
    `divergence` section reads it 8h40m later. **RULE: a standing report
    is not standing until something OTHER than a person re-runs it, and
    a measurement with no consumer is not a measurement. When a pass
    finds a defect in what a report SAYS, ask in the same pass who reads
    what it says; the answer was "nobody" here and the fix looked
    complete without it.** The consumer is allowed to exist only because
    its subject is `latest_complete_run` and therefore advances: a run
    that becomes permanently unmeasurable stops being the subject at the
    next daemon restart, so this cannot become the kind of permanently
    red check #29 and #45 turned out to be. That property is asserted
    directly, not argued.

47. **2026-09-10 -- a promoted timer was installed, verified, drift-clean
    and INERT.** `hyxlab-divergence.timer` was added to
    `scripts/systemd/` and promoted. `systemd-analyze verify` passed,
    `test_systemd_units.py` passed, and `collector.health --drift-only`
    reported `23/23 loaded unit files match the repo`. `systemctl --user
    is-enabled hyxlab-divergence.timer` said **disabled**. The unit
    would never have fired, and the QA consumer shipped in the same pass
    would have gone red 36h later blaming a report nothing was
    scheduled to produce.
    No GATE could have caught it. `promote.sh`'s `install_units()` was
    `cp` + `daemon-reload`, and enablement is neither -- it is the
    `timers.target.wants` symlink that only `enable` writes. Every drift
    arm compares unit TEXT, and the text was perfect; this is invisible
    to a file comparison by construction, for the same reason the
    SHADOWED and DROP-IN arms exist. The digest is the near-miss and is
    recorded as one rather than claimed away: `collector.health`
    enumerates from `UNIT_DIR.glob`, so it DOES list an
    installed-but-disabled unit, and its NEVER-RAN arm would have said
    `timer active but never triggered` on the next pass. But it says
    exactly that for a correctly-enabled timer whose first fire has not
    come yet -- which this one now is -- so it cannot tell inert from
    pending, and the reader who saw that line would have had no reason
    to look. Late, ambiguous, and dependent on someone reading it: a
    near-miss, not a control. The gap had
    been latent since the fleet was built and was never exercised,
    because every existing timer was enabled BY HAND on the day it was
    first written, and no timer had been added since.
    Type: `installed-is-not-running`. **RULE: shipping a unit file is
    not deploying a unit. A promotion must leave the manager in the
    state the repo describes -- which for a timer means enabled, not
    merely present -- and the gate on that cannot be a text comparison,
    because the text is what was already right.**
    Prevention: `install_units()` now globs `scripts/systemd/hyxlab-*.timer`
    and runs `systemctl --user enable --now` over the discovered set
    (idempotent on an already-enabled, already-active timer, so it
    cannot disturb a running schedule or a `Persistent` catch-up).
    Timers ONLY: enabling a timer-triggered `.service` would also start
    it at boot, outside the schedule its timer exists to impose.
    `tests/test_promote_enables_timers.py` pins all of it lexically --
    including that the set is GLOBBED and not enumerated, since an
    enumeration cannot fail when a new timer appears, which is this same
    defect restated one level up. All three arms verified to fail
    against the pre-fix `promote.sh`.
    **CLOSED 2026-09-11** (was left OPEN here on purpose, so the shape got
    designed rather than bolted on). `health.judge_drift` now has an
    INERT arm, and the rule this entry guessed -- "timers yes,
    timer-backed services no" -- turned out to be a PROXY for one written
    in each unit file: `enable` only ever creates the symlinks an
    `[Install]` section names, so a unit without one cannot be enabled at
    all (`static`). Measured across all 23 vendored units, `[Install]`
    and `UnitFileState=enabled` agree exactly -- 14 and 14 -- and the
    file-based rule additionally covers the three DAEMONS the guessed
    rule omitted. `promote.sh` selects its enable set by the same rule
    (`--now` still timers-only), which is what makes INERT `REPAIRABLE`
    rather than an operator hand-off, and
    `tests/test_promote_enables_timers.py` executes promote's own
    selection code over the repo's files so the two cannot diverge.
    Arm ORDER is the subtlety: `UnitFileState` is read from the manager's
    cached view, so a unit that has just gained an `[Install]` and not
    been reloaded still reads `static` -- STALE-IN-MEMORY is judged first
    so a mid-promotion unit cannot read as a false INERT. Verified live
    on a probe outside the fleet's namespace (installed, reloaded, never
    enabled, removed): `disabled` -> INERT, `enable` -> OK.

48. **2026-09-10 -- the sweep had a graceful path for the failure it
    cannot recover from and none for the one it can.** The 06:10Z
    incremental sweep died at series ~600 of 3,656 with a bare
    `duckdb.IOException` out of `writer_burst`: a reader held
    `data/hyxlab.duckdb` past the 300s open budget, `open_retry`
    re-raised, and the traceback went straight through `run_sweep` and
    `main`. Exit 1, unit red, ~3,000 series untouched for a contention
    that cleared in minutes.
    Nothing was corrupt and nothing was permanently lost --
    `sweep_series` advances the watermark only in its final burst and
    every intermediate write is idempotent, both facts already written
    down in that docstring -- which is exactly what makes the crash
    indefensible. The same loop already handled VENUE degradation with
    care: count it, log a `sweep_log` row, break at
    `ABORT_CONSEC_ERRORS`, exit 75, resume from the watermarks
    tomorrow. That is the failure the sweep can do nothing about. The
    held-file failure, which clears on its own, got no handler at all.
    `hyxlab-collect` meets the identical condition every day and calls
    it a skip.
    The reason the gap survived is that the mitigation had a name and
    looked like a fix. `BURST_OPEN_RETRIES * BURST_OPEN_DELAY_S >= 300`
    is pinned by a test, and `writer_burst`'s own comment explains that
    the budget was WIDENED because "readers don't take the flock, so the
    open can still lose to one." Widening a budget does not bound a
    race; it only moves the point at which losing it becomes fatal. Two
    readers, or one slow one, and the 300s is spent.
    Type: `recoverable-failure-handled-worse-than-unrecoverable-one`.
    **RULE: when a loop has a graceful degradation path, check that it
    covers the CHEAPEST failure and not just the loudest. A retry budget
    is a bet on a deadline, never a guarantee; every budget needs an
    answer for what happens when it runs out, and for an idempotent,
    watermark-resumable unit of work that answer is "skip it," never
    "die."**
    Prevention: `run_sweep` catches `duckdb.Error` per series, counts
    `lock_skips`, prints the ticker, and continues; the venue breaker
    never sees it. `ABORT_CONSEC_LOCK_SKIPS = 5` is a deliberately
    shorter fuse than the venue's 25 because each skip has already spent
    the full 300s budget -- 5 unbroken skips is 25 minutes of solid
    contention, i.e. a held file rather than an overlap, and reusing 25
    would burn two hours before saying so. The venue branch's own
    `log_sweep` burst and `main`'s closing census burst are both guarded
    too: the first used to disarm the breaker, the second used to
    retitle a fully completed run as a failure.
    Carrying #46 forward rather than repeating it: a partial sweep now
    reports GREEN, so the skip count could not be left in the journal
    for a human to notice. A skip cannot log itself -- the burst it
    needs is the burst that just failed -- so the tickers are DEFERRED
    and replayed into the closing burst as `sweep_log` rows with status
    `busy`, which `doctor`'s existing by-status census reads. Deferred,
    not impossible, which is why this needed no
    `collect_skips.jsonl`-style sidecar of its own.
    `tests/test_hyxlab_sweep_busy.py` has ten arms; nine verified to
    fail against the pre-fix module (the tenth pins the pre-existing
    exit-75 contract the new abort rides on).
    **OPEN, found in the same journal and not chased:** the shadow
    daemon (PID 3806798) held `data/hyxstream.duckdb` across several
    minutes on 09-10, and `hyxlab-stream` logged repeated `flush FAILED
    ... rows held for retry` with the backlog climbing 974 -> 3,228.
    streamd degraded correctly and lost nothing, so this is a latency
    and memory-pressure question, not a data one -- but it is the same
    two-writers-one-file shape one archive over, and nothing currently
    measures how long that backlog gets.

49. **2026-09-10 -- the check that reads the last QA run could not be
    read itself.** `qa_prior_run` exists to name the one state nothing
    else in the project can see: a failure that healed between two
    10:00Z runs, leaving yesterday's FAIL in a journal nobody reads and
    today's run green. `_own_findings` strips that check's own result
    out of the record's `failures` list before persisting it, and that
    is CORRECT -- including it lets one unread failure re-arm the report
    of itself, every day, forever.
    `collector.health` reads the same list. So the digest -- the
    operator's only reader, and the module written specifically to close
    this loop -- could not name that check on any run. Worse, the run it
    fires on is by construction the run where every OTHER check is
    green, so the digest printed `FAILED hyxlab-qa.service` from the
    unit line and `clean` from the QA line, with no name between them: a
    red unit and a green verdict, adjacent, and nowhere to go.
    Measured, not inferred: the 09-10 10:00Z run journalled 6 FAILURES
    and recorded 5. The sixth was this one.
    Neither half was a careless decision. Each was right about ITS
    reader, and the defect is that the record has two readers asking
    different questions -- "did a name I reported go quiet?" and "what
    did the last run find?" -- while one list was being asked to answer
    both. That is why it survived review twice: whichever side you read,
    the reasoning on that side is sound.
    Type: `one-field-serving-two-readers-with-opposite-needs`.
    **RULE: when a record is written for a specific consumer and a
    SECOND consumer is later pointed at it, re-derive what each one
    needs from it separately. A field deliberately narrowed for reader A
    is not thereby correct for reader B, and the narrowing will look
    fully justified from inside reader A's rationale -- which is the
    only rationale written down. Add a field for the new question;
    never widen the old one, and never make the new reader infer.**
    Prevention: `qa_prior_run` returns its reason, `main` hands it to
    `_record_run`, and it is persisted in a THIRD field that no
    comparison reads -- so it cannot echo, and `failures`/`skipped` keep
    their meaning for every archived record. Absent or non-string reads
    as a pass, the conservative direction for the records already on
    disk (a digest silent about one historical run, never one inventing
    a finding for it). `judge_qa` withholds "clean" when it is set and
    prints the UNREAD line even alongside other failures, since it is
    independent of what the sections found and suppressing it would hide
    it on exactly the busy run nobody re-reads. Twelve of thirteen new
    arms verified to fail against the pre-fix modules; the thirteenth is
    the control pinning the field as additive. The class was swept:
    `_own_findings` is the only record-side filter in `qa.py`, and
    `STANDING_SKIPS` classifies rather than drops.


50. **2026-09-10 -- a rationale outlived the code path it was written
    about, and seven recoverable cycles were thrown away.**
    `acquire_writer_lock` has said since 2026-08-02 that "a dropped cycle
    is an unrecoverable hole in the 5-min tape; the collector cannot
    backfill a snapshot it never took," and `qa_collect_skips` repeats it.
    True as written: the `flock -n` wrapper it describes dropped the cycle
    BEFORE python started. EXP-957 then moved the fetch ahead of the lock
    -- for an unrelated reason, to stop holding the archive across ~29 s of
    HTTP -- and from that day the sentence was false of the only skip path
    left. A cycle that loses the wait now has its rows already in hand,
    complete and stamped at fetch time.
    Measured: on 09-10 between 07:08 and 07:36Z the daily sweep held
    `data/hyxlab.duckdb` and seven consecutive cycles each discarded 426
    Kalshi snapshots, 5,649 market infos and 35 NWS forecasts (~1.8 MB,
    EXP-957's own figure) for a contention that cleared in minutes. The
    holes were counted correctly by two instruments and healed by none.
    Type: `stale-rationale-surviving-the-refactor-that-falsified-it`.
    **RULE: when a change moves WHERE a failure can occur, re-read every
    docstring that explains WHY that failure is unavoidable. Those
    sentences are load-bearing -- they are what stops the next reader from
    fixing it -- and unlike code they do not break when the premise
    moves. A comment asserting an impossibility is a claim with a date on
    it; grep the claim, not just the caller.**
    Prevention: `collector/spool.py` buffers the fetched rows beside the
    archive and the next successful cycle drains them oldest-first inside
    the lock it already holds; `qa_collect_spool` decides whether recovery
    actually happened, including an inert-producer arm witnessed against
    the skip sidecar (a recovery mechanism nobody witnesses is #43/#46
    again). Both stale docstrings now carry the distinction rather than
    the obsolete half. Two of twenty-five arms verified against the
    pre-fix `collect.py`; one arm found a live bug in the codec while
    being written (`str(date | None)` contains the substring "datetime",
    so every `date` field was decoding to midnight).

51. **2026-09-10 -- the dev repo's `data/` IS production's, and a new
    sidecar leaked into it on its first suite run.** `hyxrestration-
    stable/data` is a symlink to `hyxrestration/data`, so a test that
    drives real code with the repo as its cwd does not litter a scratch
    tree; it appends to live telemetry that the QA checks then read as
    evidence. `tests/test_hyxlab_writer_lock.py` runs the real
    `collect.main()` against a held lock -- that is its purpose -- and
    wrote eight fabricated `spooled`/`drained` events plus two cycle
    payloads into exactly the file `qa_collect_spool` counts. Third
    instance: EXP-1333's 429 header sink was the first, each fixed by
    hand, each as one member.
    The instructive part is the guard that does NOT work. Snapshotting
    `data/` around every test and failing on a change was written first
    and is unsound at the root: the live collector writes those same
    files every five minutes from the other end of the symlink, and no
    stat separates our append from its append.
    Type: `shared-production-state-reachable-from-a-test-default`.
    **RULE: when a leak's detector would have to distinguish our writes
    from a live process's, move the invariant off the artifact and onto
    the PATH -- assert that no default can reach production, rather than
    that production did not change.**
    Prevention: `conftest.SIDECAR_CONSTANTS` redirects every cwd-rooted
    `data/` sidecar constant per test, and
    `tests/test_sidecar_redirect_coverage.py` DERIVES that set from the
    source by AST so the twelfth sidecar reddens on entry, with a
    staleness arm so a renamed constant cannot leave a dead entry
    protecting a name that no longer exists.


52. **2026-09-11 -- the same two-worktree symlink topology as #51, read
    from the other half, and production got the wrong answer.** #51 was
    "`hyxrestration-stable/data` IS `hyxrestration/data`, and a test did
    not know." This is "`hyxrestration-stable/reports` is NOT
    `hyxrestration/reports`, and the units did not know." `data`, `.env`
    and `.secrets` are symlinks between the two trees; `reports/` is a
    real directory in each. So a cwd-relative `reports/<x>` is shared
    state in the first three cases and per-checkout state in the fourth,
    and nothing in the path says which.
    It cost both halves, measured. A divergence report produced by hand
    in the dev tree on 09-09 was invisible to `hyxlab-qa` (WorkingDirectory
    = stable) on 09-10, which FAILED "run 20260829T191841 finished 67.5h
    ago, unmeasured; newest report on file is 20260810T081931" against a
    file that had been on disk for 13 hours. Then `hyxlab-divergence`
    fired for the first time at 09-11 01:20Z and its `--if-new` flag --
    whose unit comment says it "is what makes this daily unit
    affordable" -- found the stable tree empty and re-derived the report:
    29min 26s wall, 4.2G peak, output byte-identical to the 09-09 file
    apart from `generated_at`.
    The QA check even printed the trap as its remedy: "Repair: `python -m
    simulator.divergence` (~30 min)", which run in the dev tree (where an
    agent stands) costs half an hour and clears nothing.
    Type: `path-relative-to-a-cwd-that-two-deployments-do-not-share`.
    **RULE: a path is only "relative" if every process that reads it and
    every process that writes it stands in the same place. Two checkouts
    of one repo do not, so for anything DERIVED FROM the shared archive
    the root must be STATED -- not inferred from a neighbouring symlink,
    which points wherever the operator pointed it, and not from git
    worktree metadata, which a deployment need not have.** The inverse
    stays relative on purpose: `qa.STATE` is a log of what THIS checkout
    did, and fusing the two records is the same bug's mirror image. The
    question that sorts them is "derived from the shared archive, or a
    record of this checkout?" -- and it has to be asked per path, because
    both live under `reports/`.
    Prevention: `hyxlab/reportdir.py` (`HYXLAB_REPORTS_DIR`, stated in the
    unit file beside the absolute `WorkingDirectory` already stated
    there; unset = the old relative default, so by-hand use is
    unchanged), and `tests/test_shared_reports.py`, which walks each
    unit's ExecStart import graph and reddens on any relative `reports/`
    literal a unit can reach -- one documented per-tree record
    allowlisted, the allowlist itself checked for still matching, and the
    unit discovery asserted non-empty so the guard cannot pass by finding
    nothing. Three arms verified red against the exact pre-fix state
    (literal restored, Environment line dropped, the two units made to
    disagree).
    Corollary worth carrying: #51 and #52 are one topology, and the
    lesson is not "symlink more" or "symlink less" -- it is that **a
    tree where some children are shared and some are not cannot be
    reasoned about from a path**, so every cwd-relative default inside
    it is a claim that needs a reason written next to it.


53. **2026-09-11 -- the reader that exists to catch a silent heal read
    "no FAIL recorded" as "passed", and announced two unlooked-at checks
    as green.** `qa_prior_run` compares yesterday's failures against
    today's and reports any name that went quiet, because a defect that
    repairs itself between two 10:00Z runs is otherwise invisible. It
    derived "green today" from ABSENCE: `set(prior.failures) -
    today_failed`. A name is absent from today's failures for two
    opposite reasons -- it passed, or it never reached a verdict at all.
    Both were live in the SAME RUN, the 09-11 10:00Z one, which is where
    this was found: it reported five healed names and exactly two of
    them were false.
    `collector cycles are not skipped for the lock` FAILED on 09-10 (7
    skipped cycles) and on 09-11 printed `SKIP ... UNVERIFIED` -- no
    cycle waited out the lock, so nothing was measured. `batch units
    within measured run budget` FAILED on 09-10 naming the 09-10 07:45Z
    sweep abort, and on 09-11 printed `WATCH ... (already reported)` and
    returned: the abort had not gone away, the REPORT of it had been
    acknowledged. Neither is a repair, and the check called both green.
    The namespaces are why it could not self-catch. The record carries
    `skipped` (SECTION names, "collect-skips") and `failures` (CHECK
    names), so a skipped section's check name lands in a set the skip arm
    cannot see -- and in the WATCH case no section is skipped at all, so
    no section-level rule would have caught it either. The same file's
    exit path already states the principle it was violating: "A skipped
    section is NOT a passed one."
    Type: `absence-of-a-negative-read-as-a-positive`.
    **RULE: a claim about today can only be made about a check that RAN
    today. Never infer "passed" from "not in the failure list" -- track
    EXECUTION as its own evidence, at the one place a verdict is actually
    reached, so every present and future no-verdict path (skip, watch,
    early return) is covered without being enumerated.**
    Prevention: `qa._ran`, appended by `check()` itself and by nothing
    else, so a path that prints without a verdict cannot land in it;
    `healed_f` intersected with it (and `today_failed` folded in, since a
    name that failed today ran by construction, so the two inputs cannot
    disagree); the unrun names reported as a non-failing `not re-checked
    today` clause rather than dropped -- failing on them would fire every
    night forever on a healthy box, which is the same alarm-fatigue trap
    the healed-only rule exists to avoid, and how long a section may go
    unrun is already owned by its own `<section> checks completed within
    36h` line. Seven arms verified red against the exact pre-fix logic,
    including the two no-verdict paths driven through the real production
    functions rather than simulated.


54. **2026-09-12 -- the cleanup that exists so nothing is lost ran on no
    exit path production takes, and it had a passing test.**
    `collector.streamd.Daemon.run` ends with
    `self.store.flush()  # final drain -- never lose buffered events`,
    inside a `finally`. Python's default disposition for SIGTERM is the
    OS one: terminate immediately, no `finally`, no atexit. Nothing
    installed a handler and `hyxlab-stream.service` sets no `KillSignal`,
    so the drain was reachable only from Ctrl-C and from `--smoke` --
    i.e. from a developer's terminal and from the test suite, never from
    `systemctl restart`, which is the only way it is ever stopped.
    MEASURED, and the measurement is a count of zero: 14 days of journal
    hold **4 `starting (db=` lines and 0 `shutdown; stats` lines**. Every
    restart dropped the buffer -- a flush interval (~1,500 rows) normally,
    up to `SPILL_CAP` = 400,000 rows if a flush stall was in progress,
    which is the restart a wedged archive makes most likely. promote.sh
    restarts this daemon on every streamd change, so the passes that
    built the stall ledger were themselves paying the cost.
    Type: `cleanup-on-a-path-the-deployment-never-takes`.
    **RULE: a shutdown path is production code only if the signal that
    actually stops the process reaches it. Before writing anything into a
    `finally`, name the signal the supervisor sends -- and then check the
    journal for the line that path prints, because the absence of that
    line is the whole proof.** Generalises past SIGTERM: `atexit`, context
    managers and `__del__` are all skipped by SIGKILL, and a cgroup OOM
    kill skips every one of them.
    Two holes under it, both only reachable once the path ran at all: a
    FAILING final drain dropped the buffer rather than spilling it (the
    overflow path keeps `SPILL_CAP` rows in memory because a next flush is
    coming; at shutdown there is none), and the interrupted stall episode
    was never recorded, because `ok()` is unreachable exactly when the
    drain that would have ended the stall is the thing that failed.
    **A recovery that is only correct when the thing it recovers from did
    not happen is not a recovery.**
    The guard was `test_the_shutdown_drain_closes_an_open_episode`, which
    read `inspect.getsource(Daemon.run)` and asserted `self.stalls.ok(`
    appeared after `self.store.flush()`. It was green for the whole life
    of the defect. Same class as #53's hand-written section list and #48's
    text-reading unit checks: **a test that reads source text asserts that
    a line EXISTS, and the question here was always whether it RUNS.**
    Prevention: a loop signal handler for SIGTERM/SIGINT ending `run()`
    through its `finally`; `StreamStore.spill_all()`; `FlushStalls.
    interrupted()` writing an `open` record (a lower bound, forced past
    the heartbeat); and seven behavioural tests, verified red against the
    exact pre-fix code, plus two live runs of the real daemon on a scratch
    DB -- one clean SIGTERM (20,551 book_events + 2,147 trades persisted
    that the old code dropped) and one against a real read-only lock
    holder (7,094 rows handed to the sidecar, drained by the next boot).

55. **2026-09-12 -- the atlas quoted tier gated "tested" on ROWS and ran its
    test on DAYS, so `not_significant` included tests too small to reject.**
    The same gate-vs-draw defect 740b657 fixed in queuescore (orders counted,
    underlyings sampled), live one report over. `quoted_status` = silent iff
    quoted_n < 200, while the Wilson draws n = quoted_days: on 08-25, 2 of
    the 3 tested buckets had fewer days than the flagged gap needs (51 vs 69,
    54 vs 57), and the docstring decomposition read all three as "failing on
    the interval". Type: `wrong-statistic`, #32/#33 family.
    Root cause: a gate and a test were written in different passes, each
    correct at its own unit, and nothing asserted the units matched.
    Prevention: `quoted_days_to_detect` / `quoted_powered` at the full-sample
    gap (fixed before the quoted outcome), `quoted_verdict.tested_powered`,
    four mutants red. **RULE (#42's sweep rule applied to units): when a
    gate admits a test, name the unit the gate counts and the unit the test
    draws in the same pass; if they differ, publish power at the draw unit.**
    Not swept yet: base `flag_status` (n rows gate, row-Wilson -- same unit,
    likely clean) and the robust/day tiers, which inherit the row gate.

56. **2026-09-17 -- a verdict certified by a sibling in a sliding window
    was re-derived every day, and the witness always left the window first.**
    `qa_batch_run_budget` classed a breach as catch-up (FAIL once, then WATCH)
    only while the abort before it was inside the 7d journal read. An abort
    ENDS before the catch-up it certifies, so it always ages out first: at
    09-17 10:00Z the 09-10 07:45Z abort was gone, the 09-11 22:49Z catch-up
    was not, and QA went red on "stale budget" for a constant nobody could
    fix. It would have stayed red until ~09-18 22:49Z. The 09-13 status entry
    predicted WATCH "from 09-14 to 09-18" without checking the edge. Every
    catch-up hits this, because the ordering is structural. Type:
    `wrong-assumption` (window edge). Fix 9fc4b1b: the recorded
    `catchup:` key (minted only for certified runs) is the witness once the
    abort is unreadable; a live-case test and an unrecorded control.
    **RULE: when a classification depends on an EARLIER event in a
    lookback, persist the classification when it is made. Re-deriving it
    fails on the day the earlier event ages out, and that day always comes.**
    Same pass: Kalshi's Thursday maintenance (dead-air storms, hours 06-09Z)
    sent the first books `unsubscribed` ack on record, and the void-frame
    check had no entry for that control frame. Added as benign; the dead-air
    gap row still marks the capture it cost. (That entry said "since 08-13";
    see #57.)

57. **2026-09-17 -- "since 08-13" was a fact about how far back I
    looked, not about the world.** Classifying the `unsubscribed` ack
    (#56) I measured the maintenance storm over recent weeks and wrote
    it up as recurring "every Thursday since 08-13" -- a start date, in
    the wiki, presented as measured. Re-measured the same day over the
    whole 71-day stream retention: the storm is on ALL 11 Thursdays in
    the record, back to 07-09. 08-13 is where its SIZE jumps (~50 lost
    minutes on 07-09, ~450 on 08-27), which is why the recent weeks were
    the ones that got noticed. The claim was never wrong about any day it
    had seen; it was wrong about the days it had not, and it read as a
    causal event ("something changed on 08-13") that never happened.
    Type: `wrong-assumption` (sampling edge). No code fix -- the error was
    in a wiki claim, corrected in `data-pipeline.md`.
    **RULE: a recurrence found in a recent window has no start date until
    you run it against the WHOLE retention. The earliest occurrence in a
    partial sample is the sample's edge, not the phenomenon's onset --
    and if the edge coincides with a round "since <date>", that is the
    tell, not the evidence.** Found while budgeting `stream_gaps` volume
    the same pass, which is the sibling lesson: gaps were read by six
    consumers and spent as an EXCUSE by all six, so nothing measured the
    quantity itself. **A quantity that only ever excuses other checks
    needs a budget of its own, or capture can degrade without a single
    check getting louder.**

58. **2026-09-18 -- a CHANNEL's rate was used to size a BUFFER that holds
    three channels, and the counter-example was in the same file.**
    `StreamStore.PENDING_ALARM` (200k) and `SPILL_CAP` (400k) were both
    documented as spans of time -- "~30 min of firehose at the observed
    ~105 ev/s" and "2x the alarm (~1 h of firehose)". 105 ev/s is correct,
    and is the kalshi-TRADES channel figure quoted in `streamd`,
    `kalshi_ws` and venues.md. But `pending` counts events + trades +
    gaps, and the book channel fills the same buffer. Measured over 174h
    of `hyxstream.duckdb` (09-11..09-18): books median 96.1/s, trades
    136.0/s, **combined 249.0/s** (p90 320.1, busiest hour 433.4). Trades
    are not even the majority, so the channel most likely to be quoted
    alone is the one that is not most of the buffer. Both constants were
    therefore advertised at **2.4x** the span they cover: the alarm is
    ~13 min (~8 in the busiest hour), the cap ~27 min (~15), not ~1 h.
    Three independent instruments agree on the real rate -- the archive
    counts (249/s), the stall ledger's one long episode (145,176 rows in
    599.7s = 242/s) and the pre-ledger journal tail (the cap reached at
    1756s = 228/s).
    **The sharpest detail: the refutation was written into the same file,
    in the same commit.** `streamd._final_drain` justified spilling the
    whole buffer at shutdown with "not just the overflow above SPILL_CAP,
    **which a sub-hour stall never reaches**" -- while 540 lines above it
    the `STALL_LOG` comment recorded the measurement that refutes it:
    "three ~30 min, and TWO of those three reached SPILL_CAP". Both are
    sub-hour. The two comments were never read against each other because
    each was true-sounding on its own, and the arithmetic that connects
    them (400,000 / rate) was never performed with the right rate.
    The `_final_drain` conclusion was right for the OTHER reason stated
    beside it (at shutdown there is no next flush, so a row left in the
    buffer is a row lost), which is why the false premise cost nothing yet
    and survived unread. Second consequence, still live: the sidecar
    sizing note priced a 7 h poly-sweep wedge at ~430 MB; at the measured
    rate it is 6.3M rows and **~1.0 GB**, which is the number the
    torn-append/ENOSPC rewind path exists to survive.
    Type: `wrong-assumption` (unit mismatch -- a per-channel rate applied
    to a per-buffer quantity), propagated by copy to four reasoning sites.
    Memory was NOT the exposure and that was measured too, not assumed: a
    buffered row costs ~256 B resident (tracemalloc: 264 B/BookEvent,
    248 B/StreamTrade), so a buffer pinned at the cap is ~102 MB against
    the unit's 2G cap. The constants' VALUES are therefore left alone --
    only the claims about them were wrong.
    **RULE: before sizing a shared buffer in units of time, name every
    producer that appends to it and add their rates. A rate quoted with a
    channel's name on it is evidence about that channel only, and reusing
    it for the aggregate is a unit error that reads as a measurement.**
    Prevention: ESCALATED to test (`tests/test_hyxlab_stream.py`) --
    `BUFFER_ROWS_PER_S` is now a single declared constant carrying its
    measurement; one test pins that `pending` counts all three buffers and
    that the rate exceeds any single channel's, and another parses each
    constant's OWN comment and asserts the minutes it advertises still
    equal `constant / BUFFER_ROWS_PER_S`. Changing a constant, or the
    rate, without restating the span goes red. Verified red against the
    old rate: setting `BUFFER_ROWS_PER_S = 105` fails three tests.

61. **2026-09-18 -- a shutdown path was made REACHABLE and never TIMED,
    and the deadline it races was a default nothing in the repo had
    written down.** `collector.streamd._final_drain` exists so that "no
    buffered row dies with the process", and #54 (2026-09-12) fixed the
    half everyone looks at: SIGTERM now reaches it. Nobody then asked how
    LONG it takes, or what stops it. systemd SIGKILLs a unit
    `TimeoutStopSec` after SIGTERM, `hyxlab-stream.service` set none, so
    the deadline was systemd's 90s DEFAULT -- a load-bearing constant
    living outside the repo, sized by nobody, changeable by any host-wide
    config.
    **Both sides measured 2026-09-18, and they do not fit.** `flush()`
    sustains **7,100 rows/s** end to end (50k 7.05s, 200k 28.04s, 400k
    56.46s, 1M 140.66s -- linear within 0.7%), so 90s bought ~639,000
    rows. But `flush()` writes the buffer AND the whole sidecar in ONE
    transaction, and the sidecar is unbounded: the 7h poly-sweep wedge
    priced in its own comment is 6.3M rows, ~890s, **10x the deadline**
    (measured directly at 2M rows: 285.6s).
    **Losing the race is strictly worse than declining it, which is the
    non-obvious half.** A SIGKILL mid-drain is not a rollback-and-retry:
    DuckDB does roll the transaction back and the sidecar does survive
    (it is unlinked only after commit), but `flush()` has by then moved
    the in-memory buffers into LOCALS, and SIGKILL runs neither the
    `except BaseException` that puts them back nor the `spill_all` that
    would have saved them. Up to SPILL_CAP rows -- ~27 min of tape -- die
    with no gap row marking the hole, because the gap rows are in the same
    buffer. The drain's own failure mode is the loss it was written to
    prevent.
    **The cheap path was there the whole time and was only ever the
    fallback.** `spill_all` moved the same 400,000 rows in **1.11s**
    against `flush()`'s 56.46s (51x), is equally lossless, and the next
    boot drains the sidecar ahead of the buffer by design. The old drain
    reached it only from `except` -- i.e. only when the archive was
    UNREACHABLE. On the opposite path (archive reachable, wedge's worth of
    backlog queued behind it) it started the flush and lost.
    **Generalises: a rule of this repo is that a recovery claim gets
    tested, not assumed (#12). The corollary earned here is that a
    recovery claim on a SUPERVISED process has a WALL CLOCK, and the
    supervisor's timeout is part of the claim.** "It runs on SIGTERM" and
    "it finishes before SIGKILL" are two different assertions; #54 proved
    the first and was read as proving both. Before writing cleanup into a
    signal path, measure its throughput, name the supervisor's timeout,
    and check that the timeout is written down in YOUR repo rather than
    inherited from the manager's defaults.
    Fix: `StreamStore.FLUSH_ROWS_PER_S` and `SPILL_BYTES_PER_ROW` carrying
    their measurements; `drain_rows_estimate()` sizing the sidecar from a
    `stat()` (never the parse the drain is deciding whether it can
    afford); a `DRAIN_BUDGET_S` refusal that spills rather than starting a
    flush it cannot finish; and `TimeoutStopSec=180` pinned in the unit.
    `DRAIN_BUDGET_S` is 90s = 1.6x a full buffer (56.3s), so only a
    SIDECAR backlog can trip the refusal and an ordinary restart still
    flushes. `tests/test_drain_budget.py` pins both ratios and the
    refusal; verified red three ways (no refusal, unit without
    `TimeoutStopSec`, budget under a full buffer).

62. **2026-09-19 -- the shutdown drain and the periodic flush ran at the
    SAME TIME on one buffer, because cancelling an `asyncio.to_thread`
    task does not stop the thread.** `streamd.flusher()` calls
    `store.flush()` through `asyncio.to_thread`, and `run()`'s `finally`
    cancels that task before calling `_final_drain`. The cancel LOOKS like
    a stop and is not one: a task awaiting `to_thread` returns in 0.00s
    while the worker thread keeps running -- CPython joins those threads
    only at interpreter exit. So the last write of the process's life
    overlapped a write already in flight, on one `StreamStore`, one buffer
    and one sidecar, with no lock anywhere in the module. Every review of
    this file for three passes (#54 reachability, #61 timing) read the
    `finally` as sequential.
    **Two holes, both measured 2026-09-19, both silent.** (a) Both flushes
    parse the SAME sidecar before either unlinks it and insert it twice:
    400 sidecar rows in, **800 archived** -- the EXP-1372 duplicate-row
    failure mode, from inside ONE process, into tables with no key and no
    dedupe. (b) `spill_all` appends to the sidecar while a flush is
    mid-transaction, and that flush's post-commit `unlink` deletes the
    handoff: **60 rows gone from archive, sidecar and buffer alike**, and
    the gap rows that would have marked the hole were in the same lost
    batch. (b) is precisely #61's new DECLINE branch meeting the flusher,
    so the fix for one hole opened the path into another. A third,
    unmeasured: the buffer swap is three statements, so two flushers can
    split one recv-ordered batch across two transactions.
    **Generalises: an archive-integrity argument has to name its
    CONCURRENCY unit, not just its process.** `.claude/rules/ops.md`
    already forbids two daemons owning one DuckDB file (EXP-1372) and two
    bare attaches sharing a spill directory (EXP-1373) -- both about
    separate processes, both enforced by a lock ID or a temp path. Neither
    covers two THREADS of the one legitimate owner, and the daemon reached
    that state through an ordinary `asyncio` idiom on its most-reviewed
    path. When a module says "single writer", ask which writers the word
    counts: a process lock proves nothing about the threads inside it.
    Corollary on the shutdown side: cleanup that waits on a shared
    resource is spending the supervisor's stop budget (#61), so the wait
    needs a bound AND has to be charged against the same clock.
    Fix: one re-entrant `StreamStore._flush_lock` held across `flush`,
    `_spill_overflow`/`spill_all` and `drain_rows_estimate`, plus
    `StreamStore.exclusive(timeout)` -- a guard that the drain holds across
    its decision AND the write it leads to, yielding whether it was
    acquired rather than blocking forever. A drain that loses
    `DRAIN_LOCK_WAIT_S` (30s) declines with a CRITICAL line and the row
    count, because with a flush in flight every action is a hole; a drain
    that wins is charged the wait out of `DRAIN_BUDGET_S`, since SIGKILL
    runs from SIGTERM and not from when the lock came free. 90 - 30 = 60s
    still covers the 56.3s full buffer the drain is required to flush, so
    a contended restart is not silently downgraded to a sidecar handoff.
    `tests/test_flush_concurrency.py` pins the two repros and the
    composition; verified red four ways (both holes against the pre-fix
    store, the budget charge removed, `DRAIN_LOCK_WAIT_S = 40`).

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

Recurrence audit (2026-08-25, EXP-1368/1369): the 2026-07-12 escalation
above — "every raw read-only connect must use it" — **was written as a
sentence and never enforced, and it cost five instances.** Three surfaced
2026-08-25 (mistakes #34); a deliberate sweep the same day found two
more: `collector.trades_backfill.pending_markets` queries the worklist
with a bare read-only `Store` AFTER releasing the flock, so a ~7h
`poly_sweep` taking the file in that gap kills the pass at its first
statement — in the very file whose `_flush` comment, 18 lines below,
records the lesson for the WRITE open.

Two things generalise past this instance.

**(1) A rule stated as a sentence is a gotcha wearing a rule's clothes.**
The 07-08 audit above already concluded that gotchas do not survive
sessions and anything recurring must jump to rule/test/hook. The 07-12
escalation stopped at "rule" and skipped the test, and six weeks of
recurrence followed. It is now `tests/test_connect_discipline.py`: an
AST enumeration of every attach site in the four packages, each carrying
RETRY / DEGRADE / OWNER and a written reason, asserted as set EQUALITY
so neither a new site nor a stale entry can pass. The reason a grep
never sufficed is the reason the rule was hard to enforce: **most raw
connects here are legitimate**, so the enforceable artifact is an
allowlist with reasons, not a ban.

**(2) The rule as stated described the wrong half of the hazard.**
`simulator.run_sim` and `simulator.run_backtest` opened the live archive
READ-WRITE while only ever calling readers. That violates no wording of
the read-only rule, yet it is strictly worse: a read-write open takes
the exclusive lock and excludes the collector, streamd and the shadow
daemon — mistakes #20 exactly, which was logged as an *ad-hoc query*
hazard when in fact `python -m simulator.run_backtest` is a command
CLAUDE.md tells people to run. **When escalating a rule to a test, ask
what the rule's wording excludes**; the guard enforces both directions
(ro sites need a disposition, rw sites must OWN the file), and the write
half is the half that would have caught this.

Corollary to #34's corollary, earned here: **the enumeration is also the
sweep.** Writing down every site to build the allowlist is what exposed
`bookreplay.load_stream_snapshots` — a third, dead copy of the walk
carrying the EXP-1364 materialised sort, an EXP-1367 bare attach, and a
full materialisation, with zero callers since BookReplayer landed.

Recurrence audit (2026-08-25, EXP-1370): the lesson directly above —
"when escalating a rule to a test, ask what the rule's wording
excludes" — was applied the same day and found the next rung down.
`tests/test_connect_discipline.py` enumerates who ATTACHES the archive.
It says nothing about **who holds `data/writer.lock` while writing**,
and that rule was, again, a sentence: H1 of the 2026-07-11 deep review
("all writers touch the DB only in open -> write -> close bursts")
lived in `collector.sweep.writer_burst`'s docstring with an informal
roll call, "which poly_sweep, trades_backfill and signals already
follow". **A roll call in prose is not an enumeration.**
`collector.backfill` predates the rule, was never in the roll call, and
held a read-write `Store` on the live archive across every REST call of
a multi-hour run — 5 series x up to 50 pages, then a candlestick call
per settled market at 0.35 s apart — while taking the flock at no
point. `hyxlab.migrate.main` had the same shape for schema writes.

Three things generalise.

**(1) Omitting the lock is strictly worse than holding it too long, and
it is worse in a way that hides.** The 08-02 outage held the lock for
hours and dropped 421 of 3,706 capture cycles — but each drop was
COUNTED. A writer that takes no advisory lock lets the collector WIN
`acquire_writer_lock`, pass the skip-recording branch, and only then
collide on DuckDB's file lock. Same lost capture, no skip row. **An
instrument that measures contention on the advisory lock cannot see a
writer that never takes it.**

**(2) That blind spot was reproduced inside the test written to catch
it.** The first behavioural test reused `_FakeSession.observe`, which
probes the advisory lock — and it PASSED on the pre-fix source. Only
mutation-testing against the original code exposed it; `_ArchiveProbe`
now probes both locks. **Mutation-test the test against the actual
defect, not against a plausible one** — a regression test that was
never run red is a claim, not a check.

**(3) An allowlist guard can launder the very shape it enumerates.**
The first version of `tests/test_writer_lock_discipline.py` decided
BURST per FUNCTION, ORing across its writes, so one burst-wrapped write
made every unlocked write beside it read as compliant — exactly
backfill's shape. It ANDs now, per write. Both this and (2) were caught
only by mutating the source back to the defect and demanding red.
Generalising: **when a guard's unit of judgement is coarser than the
defect's unit of occurrence, the guard is decorative.**

The artifact: every archive write is enumerated with BURST / FLOCK /
CALLER; BURST and FLOCK are verified against the AST rather than
trusted; CALLER (the only locally-unverifiable one) must open nothing
and must NAME a caller that is resolved in the source and shown to hold
the lock. The mutator set is derived from `hyxlab/store.py`, not
listed, so a new writing method cannot enter invisible.

---

Recurrence audit (2026-09-08, breadth-floor pass): the 09-06 pass closed
a real defect — a truncation guard wired to a `print` — and then wrote
down the remaining decision as a choice between three options: widen
`MAX_PAGES`, exclude the parlay family, or narrow the close window. All
three were dead, and one was dead STRUCTURALLY rather than on cost.
Measuring them took one probe. Three things generalise.

**(1) A written-down option is not a measured one, and listing three
makes the set look surveyed.** The status page framed this as a
COST/SCOPE DECISION awaiting a judgement call, which is what kept it open
for 27 hours while the tape ran at 1 row/cycle. It was never a judgement
call; it was an unmeasured one. **An open decision that has been open for
more than a pass should be re-read as a missing measurement, not as a
hard trade-off** — the tell is that no option in the list carries a
number for the thing it is supposed to fix.

**(2) The trap option: a filter on the RESULT cannot fix a limit on the
WALK.** "Exclude the KXMVE* parlay family" reads like the targeted fix —
it names the exact culprit — but truncation happens during pagination,
and a client-side ticker filter removes rows from the result while
removing zero requests from the walk. It would have shipped as a fix,
changed the numbers not at all, and looked like a deeper mystery.
Generalising: **when the fault is "we ran out of budget before reaching
X", only a change to what the SERVER enumerates can be a fix.** Ask where
in the pipeline the constraint binds before asking which filter to write.

**(3) The fix was in a field nobody had looked at.** The universe was 65%
markets whose `close_time` had already PASSED and which the exchange had
not cleared. Every option on the list was about volume, family or
horizon-length; none was about whether the market was still tradeable at
all. The measurement that ended the debate — zero of 400,000 markets with
any 24h volume closes in the past — also proved the fix free, converting
an apparent scope cut into a defect fix. **Before trading scope away to
afford an enumeration, check what is in it that should never have been
there.** The useful universe had not grown at all: 8,586 markets against
8,718 measured five weeks earlier.

**Recurrence (2026-09-14, parlay-flood-above-the-floor pass).** The floor
held for five days, then a second KXMVECROSSCATEGORY flood listed legs with
FUTURE close times. At 09-13 11Z the 24h universe went from ~5-9k to the 250k
cap, and `picked` fell 1,000 -> 3. Lesson (2) above was right about WHERE
the constraint binds, but it closed off "exclude the parlay family" by testing
only the CLIENT-side form. The server-side form existed all along:
`/markets?mve_filter=exclude` gives 8,767 markets / 3.5 s, untruncated, and
cost exactly zero, since 14 days of breadth_snapshots hold 0 KXMVE rows. **When an
option is ruled out, write down which FORM of it was measured.** "Exclude
the family" was dead as a result filter, not as a request filter. A new
trap came with it: Kalshi answers 200 to an unrecognised `mve_filter` value
and ignores it, so the test pins the literal on every page (typo mutant red).
Detection this time was prompt: `breadth_cycles.truncated` + the digest's
RECENT line on the three 504s surfaced it within 15h, versus 27h silent on
09-06.

---

Recurrence audit (2026-09-08, promote-deadlock pass): the 09-07 pass
found that a drift REPAIR must not gate on the check it repairs, fixed it
for `promote.sh --units-only`, and wrote the deadlock down as tribal
knowledge for the full path — where it was still live. It is the same
defect, one mode over, and it survived because the fix was written as a
property of a MODE ("units-only excludes test_unit_drift.py") instead of a
property of the CHECK.

59. **2026-09-18 -- the guard that protects a shadow run was sized off
    the threshold that makes the run READABLE AT ALL, not the one it is
    being accumulated for. Same defect class as #58, found the same day
    at a second site.** `young_run_guard` (bound 14, shipped 09-02)
    defers a promote's shadow restart while the live run is younger than
    `YOUNG_RUN_S` = 3 days, and 3 days is `shadow_diurnal.MIN_DAYS` --
    the PROFILE floor, below which hour-of-day means are not readable at
    all. But nobody keeps a shadow run alive for its profile. It is kept
    alive for `level_shape_status`, which needs the `panel_days_needed`
    PANEL days the run itself publishes: **10**, at the `LEVEL_FWER / 24`
    ceiling. The guard protected the cheap threshold and waved the
    expensive one through.
    **Measured over the 54-run ledger: NO run has ever reached 10 panel
    days.** The two closest died at 9 (`20260810T081931`, the 08-20 4.3h
    streamd outage -- not a promote, so no guard could have saved it) and
    at 8 (`20260829T191841`, killed 81 s after the 54d05c7 promote, which
    moved `hyxlab/store.py` -- squarely in shadow's closure). In the 16
    days the guard has been live it **deferred ZERO restarts and waved
    through TWO**, at 8 and at 4 panel days. It has never once fired.
    **The unit was wrong too, which is why the fix is a query and not a
    bigger constant.** A panel day needs a whole clock of whole hours, so
    it lags wall-clock by a NON-CONSTANT amount: 253.2h (10.6d) -> 9
    panel days, 211.2h (8.8d) -> 8, 149.7h (6.2d) -> 5. The lag runs
    1.0-1.6 days. Any threshold in seconds is wrong by about two days
    before it is wrong by anything else.
    **What made it survive a deliberate review.** The 09-02 pass
    considered reading the live run's own requirement and refused it in a
    30-line comment, reason (1) being "the number does not exist when it
    matters -- an unscored run publishes `own_days_needed` = None". That
    is true, and it is about a DIFFERENT field: `own_days_needed` belongs
    to the off-panel counterfactual and is None for any run that HAS a
    panel. The LEVEL path publishes `panel_days_needed` for every run
    with a balanced panel, open or closed, scored or not. The comment
    even wrote its own revisit condition -- "revisit if a reading ever
    publishes [the requirement] for an OPEN run below MIN_DAYS" -- and
    the condition had been met by every reading since. Type:
    `wrong-statistic`, #58/#55 family.
    Fix: `shadow_diurnal.panel_shortfall` + `--panel-shortfall` (one
    line, no report file), a PANEL CEILING in `young_run_guard` above the
    unchanged age FLOOR, and `PANEL_GUARD_MAX_S` (14d) so the deferral
    can always release -- the 09-02 refusal's reason (2), answered rather
    than re-argued. Suite 1441 -> 1452; six of the new bash tests verified
    red against the old guard, including the 09-07 case at its measured
    numbers.
    **RULE (#58's rule, restated for guards): a guard's threshold must be
    the quantity the thing it protects is being ACCUMULATED for, not the
    nearest published threshold with the same units. Before shipping one,
    name the report field the number comes from and check that field is
    the one that decides -- `MIN_DAYS` and `panel_days_needed` are both
    "days" and differ by 3.3x.** Corollary, from how this one hid: **a
    comment that states its own revisit condition has to be re-read
    against the data, not just against the next change to its file.** The
    condition had been true for weeks and nothing was watching it.


**(1) A gate that only the gated action can satisfy is a deadlock, and it
is invisible until someone edits the file.** `tests/test_unit_drift.py`'s
live arm asserts the installed units match this repo. `promote.sh` gates
on the full suite; `promote.sh` is the only thing that installs units. So
the suite went red the instant a unit file was edited — uncommitted, no
daemon involved, measured with one appended comment line — and the only
way to clear it was the promote that could not run. The repo's stop hook
reads the same suite, so the block landed on the EDIT, not on the ship.
**Whenever a check asserts something about the box that only one script
can change, ask whether that script has to pass the check to run.**

**(2) The 09-07 fix was scoped to a mode; the defect was in the verdict.**
Narrowing the gate (deselect the arm) is right for `--units-only`, which
moves no code, and would have been a lie for the full path, which does.
The real error was that DRIFT covered two OPPOSITE facts — "something
outside this repo wrote to the installed file" and "this checkout is ahead
of what was promoted" — with the same word and the same severity. Splitting
them (PENDING-PROMOTE) made the gate correct in every mode at once.
**When two causes with opposite remedies share one verdict, the fix
belongs in the verdict, not in each caller's exception list.**

**(3) The baseline was the giveaway, and it was already written down.**
`promote.sh` installs from the dev tree only AFTER fast-forwarding
`stable`, so between two promotes the correct contents of the install
directory are `stable`'s, not the working tree's. The checker compared
against the working tree because that is the tree it lives in. **A
checker judging a DEPLOYED artifact must read the deployed baseline; the
tree it happens to run in is not evidence of anything.**

---

60. **2026-09-18 -- a check that MEASURED its condition and deliberately
    did not fail on it was reported, by the next line of the same run, as
    a check that never ran.** `qa_prior_run` splits yesterday's failures
    into HEALED (green today -- report it, nothing else will) and
    UNWATCHED ("not re-checked today -- the section did not run, so
    neither healed nor still-failing can be claimed"). It derives both
    from `_ran`, which only `check()` feeds. But qa.py has a third
    outcome: the WATCH line, printed by a section that measured its
    condition, found it STILL THERE, and returns without failing because
    the report was already acknowledged. That path never calls `check()`,
    so it lands in neither set -- and the leftover falls into UNWATCHED by
    default.
    **Production said so on the 09-18 10:00Z run, in its own output.**
    `batch units within measured run budget` printed
    `WATCH ... (already reported)` naming the 09-11 sweep catch-up, and
    eleven lines later the same run reported it as `not re-checked today
    -- the section did not run`. It ran. Still-failing was precisely what
    could be claimed, and it was the strongest thing on the record. The
    digest then carried the false sentence up to the operator verbatim, on
    the `UNREAD` line.
    **The comment above the defect asserted the coverage it did not
    have.** The 09-11 pass added `_ran` to fix the *opposite* error -- the
    same WATCH path being announced as "green today" -- and wrote: "`_ran`
    is fed from `check()`, the one place a verdict is reached, so it
    covers both paths and whatever third one is written next without being
    told about it." Half true. `check()` is the one place a PASS or FAIL
    is reached, not the one place a VERDICT is: the fix moved the name out
    of the false-green bucket and into the false-unrun one, and the
    docstring of the test that pinned it called the WATCH a "no-verdict
    path", which is the same mistake stated as an assertion.
    **RULE: when a fix moves a name OUT of a wrong bucket, name the
    bucket it lands in and state what that bucket claims. Two buckets can
    only encode two outcomes; a third outcome silently takes the default
    one.** Corollary, and it is the cheap detector: **an emitted line is
    evidence about a check. If a classifier says "did not run" about a
    name that printed a line in the same run, the classifier is wrong --
    grep the run's own output against the verdict it reaches.**
    Fix: a `watch()` emitter and a `_watched` registry as the explicit
    third outcome, so such a name is reported as `still open today,
    reported by their own lines` -- and can never be claimed healed, since
    its condition was found. The three MEASURED sites (already-reported
    fade-window hole, already-reported batch abort, draining tape tail)
    route through it; the four UNMEASURED WATCHes (no shadow run, no
    cycles in window, no `breadth_cycles` table) deliberately do NOT --
    "did not run" is the true thing to say about those, and banking them
    as measured would be #28 by a new route. A source guard fails any
    future `already reported` WATCH that hand-rolls `print`.

63. **2026-09-19 -- a report's first positive in its history was printed
    at the significance of a single pre-registered test, when it was the
    best of EIGHTEEN.** `simulator/atlas.py`'s quoted tier is the
    strictest of five, and every refinement it ever received made an
    individual bucket's evidence harder to fake: cluster-robust, then
    day-robust, then day-weighted, then two-sided books only (#32/#33),
    then powered (09-12). **Not one of them bounded the NUMBER of
    buckets.** The tier ran 18 quoted tests on the 09-19 reading, each at
    a nominal two-sided 0.05, so a complete null was expected to hand back
    **0.9 confirmations**. Exactly one arrived -- `Economics|1h|d2`, the
    first `confirmed` in the archive's 31-reading history -- and the
    report printed it as a boolean with no denominator beside it.
    **Measured: its nominal alpha is 0.0443** (interval boundary at
    z* = 2.012 against the 1.96 the test uses). The tier's entire history
    of positives is one bucket clearing its bar by 0.0026 in probability,
    which is the single most marginal outcome consistent with confirming
    at all; Holm over the family needs 0.00278, so **family-wise the atlas
    has still never confirmed a quoted bucket.**
    **The defect hid behind a zero.** For five readings the tier printed
    `0`, and a zero needs no denominator -- so the missing one cost
    nothing and stayed unread, exactly like #58's false premise. The
    denominator only became load-bearing on the first reading that had
    something to divide, which is the reading where a wrong one does
    damage.
    **The second denominator is the LOOKS, and it is worse than it
    sounds.** That bucket was tested on 09-09, 09-12, 09-16 and 09-19 with
    its quoted gap FLAT across all four (0.0912, 0.0868, 0.0883, 0.0901)
    while `quoted_days` grew 82 -> 92 and narrowed the interval down onto
    an unchanged reading. **Nothing about the market changed on the day it
    confirmed; the bar moved down to meet it.** That is optional stopping
    on an accumulating sample, and a standing report that is re-run
    "whenever the data has grown" does it by construction.
    **RULE: a threshold that selects the best of m candidates is a SEARCH,
    and its headline cannot be reported at a single test's alpha. Before
    trusting a report's first positive, ask three questions in order --
    how many tests ran, how many looks has this candidate had, and by how
    much did it clear? A tier that reports only the third has told you the
    least informative of the three.** This is #28 (the strength of
    evidence is not the strength of the claim) in a place five rounds of
    strictness had already been applied, which is why it survived: each
    round made the per-bucket test harder and left the search untouched.
    Fix: per-bucket `quoted_alpha` (the p-value the boolean was hiding),
    `quoted_verdict.family` carrying the family size, `alpha_family`, the
    expected false-confirmation count and a Holm step-down;
    `quoted_days_to_detect_family` for what confirming against the search
    would cost (102 quoted days against the 92 it has); and `quoted_looks`
    per bucket, REPORTED and deliberately not spent -- nested samples are
    not independent tests, and an alpha-spending function fitted to an
    archive already read would be the threshold-fitting this module
    refused at `MIN_N`. `flagged_quoted` and `quoted_status` UNCHANGED, as
    at every tier before this one: cross-report comparability is why the
    archive is readable at all. `tests/test_hyxlab_atlas_family.py`
    (15 tests); verified red four ways -- flat alpha instead of the Holm
    step-down, `refuted_sign` dropped from the family (shrinking the
    divisor using the outcome), the boundary bisection inverted, and
    `quoted_looks` counting report FILES rather than distinct data states.

64. **2026-09-19 -- the multiplicity fix shipped that morning had a
    second site, and at that site the selected candidate was usually a
    TIE broken by clock order.** #63 bounded the atlas quoted tier's
    family. `simulator/shadow_diurnal.py` runs the same shape of search
    one report over: the de-trended level panel tests every hour of the
    clock, and bound 9 correctly divides the ceiling by `hours_tested`
    (`LEVEL_FWER / 24` = 0.00208) so `significant_hours` pays for its
    multiplicity. **The PROSE beside it did not.** `strongest =
    min(table, key=sign_p)` is the best of `hours_tested` candidates,
    named in two of the three verdict strings with no denominator -- and
    `min` resolves a tie by returning the lowest `hour_of_day`.
    **MEASURED over the 29 distinct panel states in the archive: the
    named hour is TIED in 26 of them, and in 23 it is not even the
    largest deviation.** `20260716T130721` is the limit case -- one panel
    day, all 24 hours at p=1.0, verdict "strongest hour 00Z at p=1". The
    alphabet, published as a finding. The live report re-read after the
    fix says the same thing from the other side: 22 of 26 scored panels
    tied, only 4 naming an hour at all.
    **AND THE LOOKS DENOMINATOR WAS ABSENT EXACTLY AS IN #63.** A run's
    panel is re-read as it grows, so the same clock is searched again
    every reading -- optional stopping by construction. `20260829T191841`
    is the only run in the archive read at more than one panel size, and
    across 1, 3, 7 and 8 panel days it named NOTHING: four ties (24-way,
    8-way, 3-way, 3-way), every one of which the old prose rendered as a
    confident "strongest hour 00Z"/"08Z". The status page had been
    quoting those names for weeks.
    **WHY IT SURVIVED #32, #33, #35 AND BOUND 9.** Every one of those
    hardened the per-hour TEST -- leave-one-out centre, median centre,
    ties dropped from `n_effective`, the FWER ceiling, the underpowered/
    flat partition. Not one touched the SELECTION, because the selection
    was in an f-string and not in a verdict field, so no `*_verdict`
    registry, no status partition and no AST walk could see it. **A
    number that only ever appears in prose is outside every guard this
    module has.**
    **RULE: #63's three questions apply to the number a reader QUOTES,
    not only to the number a report calls a verdict. And add a fourth
    before the other three -- does the minimum name one candidate at all?
    A tied argmin is not a weak finding, it is no finding, and `min()`
    will hand you one anyway.** Absence beats an arbitrary pick, which is
    bound 11's rule for the unreadable applied one axis over.
    Fix (bound 15): `_strongest_hour` returns a STRUCTURE whose
    `hour_of_day` is None when `tied_n > 1`, carrying `tied_hours` and
    the `of_hours_tested` it was drawn from; the verdict prose reports a
    tie AS a tie; `strongest_hour: None` in the unscorable branch (no
    clock searched, so no best of anything); and `level_looks` publishes
    the prior DISTINCT panel states a run's clock was already searched in
    (fingerprinted on the panel, never on report FILES -- 20 files here
    cover 8 states), what each named, and `anchor_held`, which is None
    rather than True when any reading in the sequence named nothing.
    REPORTED, never spent: nested samples are not independent tests, the
    same refusal `atlas.annotate_quoted_looks` makes. `level_shape_status`,
    `significant_hours` and `sign_p_ceiling` UNCHANGED, per the
    cross-report comparability precedent at every tier before this one.
    Suite 1495 -> **1506**; verified red five ways (the `min` tie-break
    restored, looks counted in report files, the current reading counting
    itself, `anchor_held` reading True off two unnamed readings, and a
    pre-bound-15 prior contributing a name instead of its recomputed tie).

65. **2026-09-20 -- the third site of #63/#64 was the anti-p-hacking core
    itself, and there the arbitrary tie-break chose between publishing
    `dsr: 0.5` and raising ZeroDivisionError.** `simulator/iterate.py`
    opens "the anti-p-hacking core ... the machinery makes the family
    size impossible to omit", and `family_report`'s own docstring calls
    its output "the number a pre-reg verdict may quote". The 09-19
    sweep looked at it, found `deflated_sharpe(..., n_trials=n)` present
    and correct, and moved on -- **that sweep asked only #63's
    denominator question.** #64's FOURTH question, asked first ("does
    the minimum name one candidate at all?"), had never been put to it.
    `best = sorted(srs, key=lambda k: srs[k])[-1]`: the best of
    `n_trials`, with no tie check, feeding both the quoted name and the
    returns series DSR is computed from.
    **THE TIE IS MANUFACTURED BY THE FAMILY, NOT STUMBLED INTO.**
    `sharpe` is documented "0 for degenerate input" and maps EVERY
    variant with `n < 2` or zero variance onto the same float, so a
    sweep in which nothing traded is exactly tied by construction.
    Measured on `{thr_10: [], thr_20: [0.0], thr_30: [0,0,0],
    thr_40: [1,1,1]}` -- four variants, no trades, all SR 0.0:
    insertion order named `thr_40` best and published
    `deflated_sharpe_of_best.dsr = 0.5`, a coin-flip probability of
    skill for a family that never traded. **The SAME family with its
    keys reversed raised ZeroDivisionError**, because `sorted` is stable,
    `[-1]` takes the LAST-INSERTED tied member, reversed that is the
    empty series, and `moments([])` divides by zero one line ABOVE
    `deflated_sharpe`'s own `t < 3` guard. So the tie-break was not
    cosmetic: it selected between a false verdict and a crash, and which
    one you got was a property of dict construction order.
    **AND THE MEDIAN SLOT WAS NOT A MEDIAN.** `ordered[n // 2]` is the
    UPPER straddler of an even family; at n == 2 -- the family size
    `run_favlong_tight` actually uses -- `median` IS `best`, printed as
    an independent row of the summary. An even family has no median
    member; saying so is the only honest read.
    **WHY IT SURVIVED.** `family_report` has no production caller yet:
    one test, no archive, no report artifact. Nothing could measure it,
    so nothing did -- and it is the path the FIRST real sweep's pre-reg
    number goes through. #64's rule was "a number that only ever appears
    in prose is outside every guard this module has"; this is the same
    hole one turn earlier, **a number that does not exist yet is outside
    every guard the archive can apply.**
    **RULE: a dormant quoting path gets the same four questions as a live
    one, on the pass that hardens its live siblings -- there is no
    archive to catch it later, and its first reading is a verdict.**
    Corollary, narrower and reusable: **a function documented to return a
    sentinel for degenerate input (`sharpe` -> 0.0) is a TIE GENERATOR;
    any argmax over its output needs a tie check before it needs a
    denominator.**
    Fix: `_slot` returns a structure whose `variant` is None when
    `tied_n > 1`, carrying `tied_variants` and `of_n_trials`; `_extreme`
    and `_median_slot` build best/median/worst through it, and
    `_median_slot` returns reason `even_family_has_no_median_member`
    rather than the upper straddler. DSR is DECLINED on a tied best
    (`deflated_sharpe_of_best: None` + `deflated_sharpe_declined`),
    since it reads the chosen member's own length and moments, with
    `tied_best_dsr_span` REPORTED so a reader sees how much the refused
    pick would have moved -- 0.0 to 0.5 in the measured case, identical
    min and max for duplicate variants. `moments` returns the normal
    moments for `n < 2`, matching `sharpe`'s own convention.
    `degenerate_variants` names the variants that manufacture the tie.
    `n_trials` UNCHANGED and still counts duplicates and degenerates: a
    look is a look, and deduplicating would shrink the divisor using the
    outcome (#63). The untied report is byte-identical to before, so
    readings stay comparable. `tests/test_hyxlab_iterate_family_ties.py`
    (9 tests); suite 1506 -> **1515**; verified red five ways (the slot
    naming a tied member anyway, DSR deflating an arbitrary tied member,
    `median` restored to the upper straddler, `moments` without the
    degenerate guard, and `n_trials` deduplicating identical variants).

66. **2026-09-20 -- three report fields published as `median` were the
    UPPER STRADDLER, and on the one with an archive the bias ran 7 of 9
    readings in the flattering direction.** `sorted(xs)[len(xs) // 2]`
    is the larger of the two middle values on an even population, never
    the smaller and never their mean, so a field built that way is
    biased in ONE direction by construction. Three live sites, found by
    sweeping the whole repo for the pattern the 09-19 `iterate.py` fix
    had just named:
    * `atlas._quoted_verdict.gap_retained_median` -- the share of the
      pooled implied-minus-realized gap that survives on two-sided
      books, i.e. the headline diagnostic for how much of a flagged
      bucket's edge is real, printed by the report and quoted on the
      status page for weeks. **MEASURED by recomputing every archived
      reading from its own published buckets: SEVEN of the nine have an
      even `gap_retained_measurable`, and all seven published a number
      ABOVE the true median** -- +0.0096, +0.0131 (x2), +0.0138,
      +0.0215, +0.0349 (x2); worst `20260829T021631`, 0.4508 published
      against a true 0.4159, 8.4% relative. The two odd readings were
      right, which is why nothing ever looked wrong.
    * `divergence.compare.price_delta_median` -- the signed fill-model
      calibration haircut. Six of thirteen archived runs have an even
      `matched`; all thirteen read exactly 0.0, so no archived number
      moves, but this is the field that would carry a real haircut's
      SIGN, and the upper straddler reads a symmetric disagreement as a
      positive one.
    * `shadow_diurnal` bound 14's `span_hours.median` -- in a file that
      already owns a correct `_median` helper and uses it at its other
      two median sites. Three sites, one wrong, no reason.
    **WHY IT SURVIVED.** Same class as #64: none of the three is a
    threshold, a verdict or a partition, so no check reads them. They
    exist only in the report artifact and in prose -- outside every
    guard the modules have. And the defect is two characters wide and
    reads as correct: `[n // 2]` is what a median looks like.
    **RULE: a field named `median` is a CLAIM about a population, and on
    an even population the upper straddler is not that claim. Compute
    it (`statistics.median`), or -- when the list holds CANDIDATES
    rather than quantities, so no member is the median -- refuse to name
    one, as `iterate._median_slot` does. The two cases are one axis
    apart and take opposite fixes; which one applies is decided by
    whether the thing being published has an identity.**
    Corollary on scope: a one-directional arithmetic bias is only
    visible against its own corrected series, and that series costs
    NOTHING to derive when the inputs are published per row. Atlas
    publishes `quoted_gap_retained` per bucket, so the whole nine-
    reading correction is arithmetic on looks already taken -- no new
    look spent, which is the only reason this was measurable at all
    without burning one.
    Fix: `statistics.median` at the atlas and divergence sites,
    `_median` at the shadow_diurnal one, `price_delta_median` rounded to
    6dp to match `price_delta_mean` beside it. Odd populations are
    byte-identical, so readings stay comparable. Guarded by an AST walk
    (`tests/test_report_medians.py`) that fails any dict field whose key
    mentions `median` and whose value indexes at `... // 2`, with
    `simulator/iterate.py` named -- not pattern-matched -- as the
    member-naming exemption. Suite 1515 -> **1520**; verified red five
    ways (all five tests fail with the three straddlers restored,
    including the archive recomputation).

67. **2026-09-20 -- the timing distribution quoted to prove a 2s pairing
    window was "generous, not tight" was SAMPLED FROM INSIDE A 5ms
    window, so it could not have reported a wide pairing if one existed
    -- and when the sample was opened up, the widest pairing turned out
    to use 90% of the budget.** `prioritycheck` is the probe the maker
    queue bracket's mechanical foundation rests on: it verifies that a
    trade print consumes the COMPLEMENT book level, and publishes the
    (decrement_ts - print_ts) distribution that sizes `ABSORB_WINDOW`.
    `check_market` appended `dt_ms` only in the `exact_match` branch --
    the branch already gated on `|dt| <= EXACT_WINDOW` (5ms). So the
    timing block's population was conditioned on the answer: `max_ms`
    could never exceed 5ms, and the 159 `late_decrement` prints of the
    2026-07-13 reading -- the entire 5ms-to-2s tail, i.e. precisely the
    evidence that sizes the window -- were the ones it dropped. The
    module docstring's claim ("confirms ABSORB_WINDOW=2s is generous,
    not tight") was unfalsifiable by the statistic offered for it.
    **MEASURED on 24h of fresh stream archive, 12,142 prints over 8
    markets: the absorb-window population's `min_ms` is -1796.2ms.**
    The conditioned sample reports -4.997ms for the same run. The true
    tail is 360x wider than any number the old block could print, and
    at 1.80s it sits at 90% of the 2.0s window -- a 1.1x margin, not
    the ~400x the artifact implied. The census says the shape is
    benign (229 pairings over 5ms, only 2 over 50ms, and those same 2
    over 500ms), but that is a thing now MEASURED rather than assumed.
    **WHY IT SURVIVED.** #64/#66's class again -- a report-only field
    no check reads -- with a new mechanism: not arithmetic that is
    subtly wrong, but a population that is subtly wrong. The five
    statistics were each computed correctly; they were computed over a
    sample selected by the very predicate under test. One reading
    existed (2026-07-13) and looked excellent, because a sample clipped
    at +-5ms cannot look anything else.
    **RULE: a statistic offered as evidence about a threshold must be
    drawn from a sample that could have violated that threshold. Before
    publishing a distribution, name the branch the sample is collected
    in and check whether that branch is gated on the quantity being
    measured -- if it is, the report is a tautology with error bars.**
    Corollary: keep the conditioned view, but NAME it. Readings taken
    under the old rule are not comparable to the new population, only
    to the sub-block, so the fix publishes `timing.exact_window` beside
    the full one rather than silently redefining five key names.
    Fix: `dt_ms` is collected for every absorb-window match, late ones
    included; `timing` gains `population`, `n`, and an `over_ms` census
    at 5/50/500ms so "the window is generous" is checkable from the
    artifact instead of from prose. Same pass also fixed `p95_ms`,
    which was `ordered[int(n * 0.95)]` -- the (floor(0.95n)+1)-th order
    statistic, whose rank is >= 95% always and is EXACTLY the maximum
    whenever 0.95n is an integer, so for every n <= 20 the report
    published `max_ms` a second time under the name `p95_ms`. Nearest
    rank is `ceil(0.95n)-1`. Suite 1520 -> **1524**, verified red.

68. **2026-09-20 -- #67's sweep: the divergence matcher sized its 2s
    window with a mean taken over the pairs that window had already
    admitted, and that field has read `None` in all 13 archived
    reports.** #67's rule was applied to the other standing reports by
    asking which published distribution is collected inside the branch
    that gates it. `simulator/divergence.compare` builds the nearest
    tier's candidate list under `if r[2] == s[2] and abs(r[0] - s[0])
    <= window`, then publishes `nearest_dt_abs_mean_s` over exactly
    those pairs -- the field EXP-004's own report named as the tier's
    calibration information. Its maximum is the window edge by
    construction. The second half is worse than the first: the nearest
    tier has never claimed a pair in production, so the number offered
    as the window's justification has never once had a value, and
    nothing noticed because `None` reads as "nothing to report" rather
    than as "no evidence exists".
    **MEASURED** on run 20260803T142853 (the worst benign window in the
    record, 261 leftovers): of the 182 leftovers with an available
    same-price counterpart, **all 182 sit beyond the 2s window** --
    min 2.53s, median 267.7s, max 4.3h; 176 beyond 60s. So the window
    is fine, and for the first time that is a measurement: the
    leftovers are the re-seed population, not near-misses (2s -> 60s
    would rescue 6 of 182).
    **WHY IT SURVIVED.** Same class as #67, found by deliberately
    sweeping for it -- which is the point: the rule is only worth
    having if it is run against the reports that already shipped, not
    just the one where the defect was noticed.
    Fix: `nearest_unpaired_dt` publishes the uncensored population
    (n, no_counterpart, min/median/max, `over_s` census at the window,
    10x the window, 60s and 600s), sought among the opposite stream's
    still-unpaired fills -- what a WIDER window could actually have
    paired, deliberately narrower than `reseed_twin`'s existence test.
    `nearest_dt_abs_mean_s` keeps its name and its conditioned meaning
    (#67's corollary) with the censoring stated at the field. Re-run of
    20260907T142900 is bit-identical on every pre-existing field. Suite
    1524 -> **1528**, verified red.

69. **2026-09-20 -- the divergence report's nearest tier can ONLY ever
    pair fills that disagree on quantity, and quantity is the one thing
    it never published.** #68 left the open question "the nearest tier
    has matched zero pairs in 13 reports -- dead code, or unreachable
    for a reason nobody has stated?". It is the second, and the reason
    is structural. `compare`'s exact tier holds ONE candidate list per
    (market, side) across its whole greedy pass and only ever POPS
    matches from it, so every replay fill still in `r_left` was visible
    to every shadow fill that became a leftover. Inside the nearest
    window (2s, a strict subset of exact's 60s `MATCH_TOLERANCE`) the
    only predicate that can have refused such a pair is `r[1] == s[1]`.
    So every pair the tier is *able* to make has unequal qty -- while
    its docstring said "Neither relaxed tier invents agreement" and the
    two numbers it published about itself were a price delta that is
    0.0 by selection and a |dt| bounded by the window. A nearest match
    was therefore indistinguishable in the report from an exact one,
    inside `matched_all_*` and `match_rate_all_*` and diluting
    `price_delta_abs_mean_all` -- the haircut -- with a 0.0 sample per
    pair, while the size disagreement it stood for appeared nowhere.
    **MEASURED**: 30,000 random fill datasets through the real matcher,
    12,397 of which reached the tier -- **0 equal-qty pairs**. The same
    generator at `window=300s` (past the 60s tolerance, reachable via
    `--nearest-window`) produces 1,278, so the invariant is a real
    property of the shipped configuration and not a tautology.
    And the zero in production is now explained rather than assumed:
    **8 of the 13 archived runs have ZERO leftovers of any kind**, so
    the tier had nothing to see at all; of the 5 that do, #68 measured
    the worst (20260803T142853) at min counterpart dt **2.53s** -- the
    nearest tier missed its closest reachable pair by 0.53s.
    **WHY IT SURVIVED.** The tier was named for the wrong thing. "The
    nearest tier" reads as a timing-offset rescue, and its shipped
    evidence (|dt|, price delta) is entirely about timing and price --
    so every review of it asked whether the WINDOW was right (#68) and
    none asked what the pairs inside the window actually disagree on.
    A tier that publishes only the quantities it selects on can never
    report the one it does not.
    Fix: `nearest_qty_delta` publishes the size gap (n, `equal_qty`,
    mean/abs_mean/min/median/max, replay - shadow, even-straddling
    median per #66). `equal_qty` is the tripwire for the one
    configuration that breaks the proof -- a hand-widened
    `--nearest-window` above 60s -- and is asserted at both settings.
    The docstring's "neither relaxed tier invents agreement" is
    corrected to say that nearest does, on qty, and can do nothing
    else. Suite 1528 -> **1532**, verified red.
    **RULE.** When a matcher relaxes a predicate to claim a pair, the
    report must publish the relaxed predicate's residual. A tier's own
    selection criteria are the one set of numbers it cannot use as
    evidence about itself.

70. **2026-09-21 -- `LIVE_GRACE_S` claimed "~8x the worst observed gap";
    the real figure is 0.24x on the live run, and the number was
    measured outside the code so nothing could say so.** (#67's last
    open thread, and the one that turned out to be load-bearing.)
    `simulator/shadow_coverage.py` decides `live` by whether a run's
    last equity tick is within 300s, and the comment sizing that
    threshold read "the gap between consecutive ticks tops out at ~37s
    (p99 ~35s) across every recent run, so 5 minutes is ~8x the worst
    observed gap". No artifact carried the distribution, so the claim
    was unfalsifiable from anything the repo produces.
    **MEASURED, 308,027 ticks over 54 runs of the live ledger:** the
    median (16.9s) and the p99 (~36s) are exactly as claimed. The MAX
    is 21,027s -- 5.8 hours -- and the max is the only end of the
    distribution this threshold is exposed to. **Twenty of the 54 runs
    contain at least one gap past the grace, 150 gaps in total**, and
    the current 221h run has 8 of its own with a max of 1,269s. So the
    grace is 0.24x that run's worst gap, not 8x.
    **THE CONSEQUENCE IS THIS MODULE'S OWN 08-01 CORRECTION, RUNNING
    BACKWARDS.** Inside such a stall a LIVE run reads dead, and
    `_bucket` moves its open fills from `pending` (censoring) to
    `missed` (failure) -- the precise conflation `build_coverage` was
    rewritten to refuse, reintroduced through the liveness predicate
    rather than through the partition. Pooled over the ledger **1.92%
    of shadow wall-clock sits inside a stall**, so roughly 1 reading in
    52 is taken there. Checked rather than assumed: **none of the 9
    archived readings landed in a stall window** (0.17 expected), so
    nothing archived moves and nothing is being rescued.
    **WHY IT SURVIVED.** Two halves of the sentence had different
    epistemic status and read as one. The p99 was true and checkable in
    spirit; the max was neither, and a reader who spot-checked the
    typical gap found the comment accurate. The failure is #67's class
    with the gate moved outside the program entirely: not a sample
    conditioned on the predicate under test, but a sample that never
    entered the artifact at all.
    Fix: `tick_gap` (per-run and pooled) publishes n, median, nearest-
    rank p95, max, `over_grace_n`, `grace_multiple_of_max` -- the
    number the prose asserted was ~8 -- and `stall_exposure_frac`, the
    share of a run's ticked span spent past the grace with no tick,
    i.e. the prior that this reading's own `live` call is wrong.
    Collected with no `WHERE` on the gap, which is the property under
    test. `None` rather than a zeroed block when a run has one tick.
    **`LIVE_GRACE_S` is UNCHANGED, deliberately**: re-sizing a
    threshold off the observed max is how the wrong number got here,
    and the archived readings stay comparable. Suite 1532 -> **1539**,
    verified red seven ways.
    **RULE.** A constant's justification is part of the program. If the
    distribution that sizes a threshold is not emitted by an artifact
    the repo produces, the comment is a claim no future reading can
    contradict -- and the half of it that is wrong will be the tail,
    because the tail is the half nobody spot-checks. Measure it in the
    report, publish the margin as a multiple, and publish the exposure
    the margin buys.

71. **2026-09-21 -- #70's sweep: every DuckDB attach budget in the repo
    is sized by a wait-time distribution no artifact emits, and the
    retry ladder's own outcome is BINARY -- so a budget eroding toward
    its cliff is invisible until the run it kills.** (#70's rule
    applied as a sweep: which OTHER threshold is justified by prose
    nothing can contradict.) `hyxlab.store.connect_retry` is the kernel
    helper every read-only attach goes through, and three budgets ride
    it: its own default (15 x 2.0s), `atlas.ARCHIVE_ATTACH` (20 x 1.0s
    x 1.3, ~214s) and `divergence.STREAM_ATTACH` (30 x 1.0s x 1.4,
    ~639s). Each is justified by a measured distribution -- "FOUR OF
    FIVE attempts died on a clean IOException after 30s", "the lock
    samples free 87% of the time", "24 reader attaches gave p50 0.0s,
    p90 7.6s, max 22.6s" -- and **the helper records none of it.** It
    returns a connection or it raises, so an attach that succeeded on
    its first attempt and one that succeeded on its nineteenth of
    twenty are byte-identical in every report downstream.
    **MEASURED, and the first production-shaped reading already shows
    the tail is live**: three read-only attaches to the three live DBs
    on 09-21 while `hyxlab-poly-sweep` was 10h into a run gave
    hyxstream 0.008s and hyxshadow 0.114s -- and **hyxlab.duckdb took 6
    attempts and 10.02s, 0.358 of the 28s default budget.** A third of
    the way to a failure, on a run that read as instant. Both ends were
    measured to make `budget_frac` an honest share: the common case is
    ~11ms (p50 over 12 samples per DB) and a REFUSED attach returns in
    0.03ms (measured against a held writer), so an exhausted ladder's
    elapsed time IS its sleep total.
    **WHY IT SURVIVED.** The same shape as #70 with the gate moved one
    step further out: not a sample conditioned on the predicate under
    test, not a sample kept outside the artifact, but a quantity the
    program never forms at all. And the one place a wait WAS printed --
    atlas's busy-archive line, `waited {ATTACH_BUDGET_S:.0f}s` --
    formatted a CONSTANT as though it were an observation, so it
    asserted "waited 214s" whatever the run had actually spent.
    Fix: `attach_budget_s()` is now the single definition of the ladder
    arithmetic every call-site comment was doing by hand (and that
    `atlas.ATTACH_BUDGET_S` carried as a re-typed 214.0 literal);
    `connect_retry` and `open_retry` record every attach -- gated on
    nothing, success included -- as db / attempts / waited_s /
    budget_s / `budget_frac`; `attach_wait_block()` publishes n, the
    per-attach rows, `waited_s_max`, `waited_s_total`,
    `budget_frac_max` (the margin the prose asserts is generous) and
    `exhausted_n`. Atlas and divergence both carry the block, atlas
    prints the margin beside the reading it paid for, and its failure
    line now prints the MEASURED wait next to the budget. `None` rather
    than a zeroed block when nothing attached, and `budget_frac` is
    `None` on a one-attempt ladder that has no budget to spend a share
    of. **No budget is re-sized** -- including the one this pass found
    reaching furthest: `divergence.main` attaches the daemon-held
    `hyxshadow.duckdb` on `connect_retry`'s brief-reader DEFAULT, which
    that helper's own docstring calls inadequate against a 24/7 writer.
    Re-sizing off prose is the #70 failure; `budget_frac` is the
    evidence that would justify a change. Suite 1539 -> **1552**,
    verified red eleven ways.
    **RULE.** #70 says publish the distribution that sizes a threshold.
    Its corollary: a retry ladder must publish what it SPENT, not only
    whether it finished. A budget whose only observable is
    succeeded/raised reports full health at 99% consumption, so the
    first reading that can warn is the one that already failed -- and a
    constant formatted into an operator line is not an observation, it
    is the same claim with a decimal point.

72. **2026-09-21 -- #71's sweep: the widest retry ladder in the repo
    publishes nothing but the count of times it went OVER, so its
    artifact is censored to its own failures; and #71's ledger
    computed its statistics from a row list capped at 256, so the one
    caller that would have exposed this would have published its last
    2% under the run's name.** (#71's rule applied to the ladders #71
    did not reach. Class: a measurement whose population is selected
    by the outcome -- #67's shape, one level up.)
    **THE DEFECT, HALF ONE.** `collector.sweep.writer_burst` spends
    `BURST_OPEN_RETRIES` -- 150 x 2.0s, at 298s the widest ladder in
    the repo -- once per burst, and a `--limit 1` smoke measured **3
    attaches for ONE series**, so a production run of 3,709 series
    spends that ladder ~11,000 times. The only thing a run ever said
    about it is `lock_skips`, which increments **only when the budget
    is exhausted**. Thirty days of `lock_skips: 0` (measured
    2026-09-21) is therefore equally consistent with every burst
    opening in 8ms and with every burst spending 290 of its 298s. The
    run that DID go over -- 2026-09-10 06:10Z, dead at series ~600 of
    3,656 because a reader held `data/hyxlab.duckdb` past the budget
    -- had nothing in front of it, by construction.
    **MEASURED, from the one unit that does publish its wait.**
    `writer_burst` takes the exclusive flock and THEN spins
    `open_retry` while holding it, and `collector.collect` prints
    `wait_s` for that same flock every cycle (EXP-963). Over **2,016
    cycles / 7 days to 2026-09-21: p50 0.0s, p99 47.0s, max 95.0s**,
    and **37 of the 51 cycles at >= 20s (73%) fall in the 06-08Z sweep
    window** -- 12.5% of the day carrying **56% of all flock wait
    time**. The ladder's spend is real and was legible only from
    ANOTHER unit's instrument. It is also the mechanism by which
    sweep's invisible wait becomes the collector's visible one: the
    sweep's 298s open budget is spent inside collect's 240s
    whole-cycle budget.
    **THE DEFECT, HALF TWO -- in #71's own code, one day old.**
    `attach_wait_block` computed `n`, `waited_s_max`,
    `waited_s_total`, `budget_frac_max` and `exhausted_n` from
    `_ATTACH_WAITS`, a list trimmed to the most recent 256. Atlas and
    divergence attach three times, so retained == observed and the cap
    is invisible -- which is exactly why it shipped. At ~11,000
    attaches the block would describe the last ~2% of the run while
    carrying the run's name, and **the first statistic a drop destroys
    is `budget_frac_max`: a max lives in the observations you threw
    away.** A block keeping the last 256 would have held not one
    attach from the 09-10 incident.
    **WHY IT SURVIVED.** Both halves are the same reflex: a bound
    written for the caller in front of you, and a counter written for
    the failure you are afraid of. Neither is wrong where it was
    written. `_ATTACH_WAITS_MAX` is correct for a daemon that attaches
    forever and for a report that attaches three times; `lock_skips`
    is correct as a skip count. They become a defect only when the
    number is read as a statement about a RUN, and nothing in either
    artifact said which it was.
    **FIX.** The rows stay a bounded SAMPLE and the statistics stop
    being one: `_AttachTotals` accumulates at record time, is never
    trimmed, and the block publishes `retained_n` / `dropped_n` out
    loud so a truncated sample can never pass as a population. An
    explicit `waits=` list IS its own population, so nothing is
    dropped by definition. `attach_wait_block(rows=False)` for callers
    whose per-attach sample is journal noise. `collector.sweep` calls
    `reset_attach_waits()` at run start and prints the margin beside
    `[sweep] done:` -- with the budget from `attach_budget_s`, never
    `RETRIES * DELAY`, because 150 attempts is 149 sleeps and a
    printed 300 would disagree with the fraction's own 298 (#71's
    atlas literal, one module over). **No budget re-sized**, per #70.
    Suite 1552 -> **1562**, verified red four ways against the exact
    old semantics (statistics recomputed from the trimmed rows), plus
    six more against the missing API.
    **RULE.** A bounded buffer is a sample, and a statistic computed
    over a sample must say so. The moment a capped list feeds a field
    named `n`, `max` or `total`, the cap has silently redefined the
    population -- and it does so invisibly for every caller small
    enough to fit, which is every caller that reviews the code.
    Corollary to #71: publishing what a ladder SPENT only works if the
    publication covers every time it spent it.
73. **2026-09-22 -- the 2026-09-08 retry fix drew its boundary at
    whether a RESPONSE OBJECT EXISTS, which is a fact about Python's
    exception plumbing rather than about whether a retry can succeed;
    so it shipped retrying a ReadTimeout and leaving its identical
    twin, a 504 Gateway Time-out, fatal. Four lost `collector.breadth`
    cycles followed, three of them FIVE DAYS AFTER the fix -- and
    those three were seen, triaged, and dismissed as symptoms of a
    different, already-fixed cause.** (Class: a fix whose predicate is
    drawn from the mechanism that reported the fault instead of the
    fault's own semantics -- plus the triage failure that let it
    survive a second look.)
    **THE DEFECT.** `collector/venues/kalshi.py` had two retry
    ladders: `_TRANSPORT_ERRORS` (Timeout, ConnectionError) retried on
    a per-walk budget, and everything else handed to
    `raise_for_status()`. The 09-08 comment justified the split in so
    many words -- "`requests` raises these BEFORE any response object
    exists" -- and excluded `HTTPError` because such responses "are
    answers, not lost packets". That clause is true of 4xx and FALSE
    of the gateway class. A 504 means an intermediary gave up waiting
    for the origin: the origin never answered. It is the same physical
    event as a ReadTimeout and differs only in whether Kalshi's edge
    ran out of patience before our own 30s `timeout` did. Which of the
    two you observe is a property of the network path, not of the
    fault -- so a fix for one that excludes the other is a fix keyed
    on the reporting mechanism.
    **MEASURED.** Fifteen days of journal: **four 504s, all four on
    `collector.breadth`, all four killing a whole cycle mid-walk with
    a live cursor, all four recovering unaided on the next 5-minute
    firing** -- 09-13 19:33/20:07/20:24Z and 09-21 00:37Z. That is the
    exact signature, 4 for 4, that the 09-08 fix was written for; the
    ladder simply could not see them.
    **WHY IT SURVIVED, AND THIS IS THE HALF WORTH KEEPING.** The three
    on 09-13 DID reach the health digest as `RECENT hyxlab-breadth 3x`
    and were triaged in the 09-14 08:30Z status entry: "came from the
    same flood: 504 Gateway Time-out while paging deep into the 250k-row
    walk. Those runs failed; they were not a separate fault." The
    parlay flood was real, concurrent, and had just been fixed, so
    attributing them to it explained the timing and closed the class.
    **09-21 falsifies it:** that 504 fired on a 9,229-market, ~10-page,
    8.5-second walk with `truncated: False` -- the healthy
    post-`mve_filter` configuration. Walk depth was a coincidence, not
    a cause; a gateway does not care how many pages preceded it. The
    dismissal was not lazy, it was *unfalsifiable as written*: it named
    no observation that would have distinguished "flood symptom" from
    "independent fault", and the one that would have -- does it recur
    once the flood is gone? -- was available only by waiting.
    **FIX.** `_GATEWAY_STATUSES = (502, 503, 504)`, the
    "origin did not answer" statuses, retried by
    `_get_transport_retrying` on the SAME per-walk `_TransportBudget`
    -- one allowance, not two, because the classes are one event and
    the per-walk bound exists to cap added wall-clock, which a second
    allowance would double. An exhausted budget returns the gateway
    response unchanged so `raise_for_status()` fails the unit exactly
    as before: bounded, then surfaced, never swallowed.
    **500 IS DELIBERATELY EXCLUDED**, on this repo's own evidence: a
    500 means the origin answered, and the one 500 in the record is
    PERSISTENT, not transient -- Polymarket's Gamma tail fault (probed
    2026-08-22) answers the last page of a long walk with a 500 on
    demand, reproduced in four daylight probes across three volume
    bands. Retrying that spends the walk's whole allowance on a
    request that cannot succeed.
    **SECOND, SMALLER HOLE, #71's class.** The field `breadth`
    published as `http_retries` counted transport retries ONLY, so it
    was blind to the 429 ladder that had existed since 2026-08-02:
    3,796 of 3,799 archived cycles read 0 and not one of those zeros
    could ever have been a 429. Retries are now counted per class
    (`transport` / `gateway` / `rate_limit`) with `total` derived, so
    a rising count says who to ask rather than just "the network is
    worse", and `walk_budget_frac` publishes what the shared ladder
    SPENT -- `fetch_universe` performs exactly one walk per cycle, so
    it is that walk's honest consumption. **No budget re-sized**, per
    #70.
    **NOT WIDENED, and recorded instead:** `get_market_trades` and
    `get_series_list` bypass every ladder with a bare `sess.get` (the
    former says so in a comment). Extending it there changes the
    budget arithmetic for a multi-thousand-ticker sweep, unlike
    breadth's single 10-page walk, and is its own decision with its
    own measurement. Suite 1562 -> **1570**, verified red four ways
    (gateway class emptied; 500 admitted; 429 counter removed; and a
    per-response budget, which does not merely fail but HANGS --
    confirming the shared budget is load-bearing for termination).
    **RULE.** When a fault is dismissed as a symptom of a concurrent
    cause, the dismissal must name the observation that would
    distinguish the two -- otherwise it is a coincidence promoted to a
    diagnosis, and it closes the class. And when fixing a transient
    fault, write the predicate from the fault's SEMANTICS ("the origin
    did not answer"), never from the mechanism that happened to report
    it ("an exception was raised before a response existed"): the
    mechanism varies with the network path, so the untested twin is
    already in production waiting.
74. **2026-09-22 -- `daemon_imports.py intersect` reads the changed-file
    list from STDIN. Run by hand with no redirect it intersects the
    EMPTY SET, prints nothing, and exits 0 -- which is byte-identical
    to "this daemon is unaffected, safe to skip the restart". I read
    that silence as evidence for all three daemon roots, wrote "nothing
    restarts" into the status page, and the promote then correctly
    restarted a 239.7h `hyxlab-stream` run.** (Class: a check whose
    unfed answer is its SAFE-LOOKING answer. Agent-process mistake, not
    a code defect.)
    **WHAT HAPPENED.** The pass changed `collector/venues/kalshi.py`.
    Before committing I ran `daemon_imports.py intersect
    collector.streamd` (and `simulator.shadow`, `simulator.simui`),
    got empty output three times, and concluded no daemon would
    restart. `scripts/restart_decision.sh` invokes the same tool as
    `printf '%s\n' "$CHANGED" | daemon_imports.py intersect "$root"` --
    the file list is PIPED IN. With no pipe, stdin is empty, so the
    intersection is empty by construction. Proven both ways after the
    fact: fed the real list it prints `collector/venues/kalshi.py` for
    `collector.streamd`, matching promote.sh's decision exactly; fed
    `/dev/null` it prints nothing and exits 0.
    **WHY THE SILENCE WAS PERSUASIVE.** Empty output is the tool's
    normal way of saying "no intersection", and "no intersection" is
    the answer I was hoping for. There is no distinguishing signal: no
    warning, no nonzero exit, no echo of how many input paths it read.
    The failure mode is therefore invisible at exactly the moment it
    costs the most -- and it errs toward restarting too LITTLE, the
    direction `restart_decision.sh`'s own header comment promises never
    to fail silently in ("conservative in the 'restart too much'
    direction, never 'restart too little' silently"). That promise
    holds for the tool ERRORING; it does not cover the tool being
    starved.
    **COST, AND THE PART THAT WAS NOT LUCK.** The 239.7h stream run
    ended and the reconnect added 2 `seq_reset` gaps. `simulator.shadow`
    was NOT restarted -- `intersect` is empty for it even when fed
    correctly -- so the 8-of-10 panel, the asset the guard exists for,
    survived because the guard is real, not because I checked properly.
    The restart also delivered item (5), the first production
    `shutdown; stats` line, which PASSED (1.336s of a pinned 180s
    `TimeoutStopSec`, drain committed, zero loss lines) -- a genuine
    gain, arrived at by accident, and it does not retire the mistake.
    **FIX (escalated straight to a hook, since the gotcha had already
    cost something).** `intersect` now EXITS 2 when stdin supplies no
    paths, naming the missing input and pointing at `closure`. The
    legitimate empty case -- a promotion that moves nothing a given
    daemon runs -- is declared by the caller that actually pipes the
    change set: `restart_decision.sh` passes `--allow-empty`. Requiring
    the flag is the whole point, because it makes "I have a file list
    and it happens not to match" distinguishable from "I never supplied
    one", which is precisely the distinction the tool could not express.
    A test pins that the bash caller keeps the flag: without it
    `needs_restart()` sees exit 2, falls back to the coarse regex, and
    restarts daemons on every no-op promotion.
    **THE FIRST VERSION OF THIS FIX WAS WRONG IN THE SAME WAY AS THE
    BUG, and that is the most useful line in the entry.** I first
    guarded on `sys.stdin.isatty()`, reasoning that a pipe is never a
    TTY so promote.sh could not trip it. It would NOT HAVE CAUGHT THE
    CASE IT WAS WRITTEN FOR: an agent shell's stdin is not a terminal
    either, so it EOFs immediately and reads as a legitimate empty pipe
    -- the exact path that produced the wrong answer. A TTY check
    protects a human at a keyboard, who is not the actor that made this
    mistake. Caught only by asking "which stdin did I actually have?"
    rather than trusting the model of it. Same shape as #73 one entry
    up: a predicate keyed on the MECHANISM (is this a terminal?)
    instead of the condition (did any input arrive?).
    **RULE.** A check whose input arrives on stdin must be given it or
    made to refuse; **silence is not evidence, and here silence was the
    dangerous answer.** Prefer the diff-independent question when one
    exists (`closure <root>` takes no stdin and cannot be starved).
    And never write a restart prediction into the status page that
    promote.sh is about to decide for itself -- read its output and
    report THAT, because it is the component with both the authority
    and the correct input.

75. **2026-09-22 -- the capture-gap budget summed `stream_gaps` rows that
    are not disjoint, so every outage after 08-18 counted 2-3x, and the
    09-17 calibration was read off the inflated sum.** (collector/qa.py
    `_capture_gap_minutes`; class: measurement / calibration.)
    **WHAT.** streamd writes one row per REASON, not per outage: a
    `reconnect`, a `seq_reset` over the same span (added 08-18, ffcbc06),
    and a `dead_air` when the outage began silent (added 08-10). Each
    addition was correct for the readers it was written for -- all of
    them ask "is t inside a gap?", where a duplicate changes nothing. The
    one reader that asks "how LONG was capture lost?" was written on 09-17,
    a month after the duplicates began, and summed. The 09-22 host OOM
    storm read 83.8 books minutes against a true 34.9.
    **WHY IT SURVIVED.** The calibration replay ran over the same summing
    function it was calibrating, so it could not see the inflation, and it
    then EXPLAINED the inflation: the comment attributed a 0.6 -> 3.8
    min/day rise to the subscribed universe growing. Merged, the floor is
    flat (2.6 / 2.2 / 2.3 Jul / Aug / Sep). A plausible cause was supplied
    for an artifact, and nothing tested the cause.
    **RULE.** Before summing durations from an event table, check that its
    rows are disjoint -- a table whose writers emit one row per REASON is
    a membership record, not a duration record. And a drift explained by
    a mechanism is only explained once the mechanism is measured; "creeps
    with the universe" was never checked against the universe.

76. **2026-09-22 -- the divergence report's `--if-new` cache was keyed on
    the RUN and not on the CODE, so five fields shipped over four passes
    to get a production reading could never get one.** (Class:
    stale-artifact / cache-key; site: `simulator/divergence.py` `--if-new`.)
    **WHAT.** The daily `hyxlab-divergence` unit runs `--if-new`, which
    exits 0 without replaying when `reports/shadow_divergence/<run_id>.json`
    exists. `run_id` is `latest_complete_run`, which advances ONLY when the
    shadow daemon restarts. `hyxlab-shadow` has been up 257.7h with 0
    restarts and its restart is deferred by the panel guard (8 of the 10
    panel days), so the subject has not moved since 09-07. Journal, read
    rather than assumed: the report was derived once at **09-13 01:29Z**
    and the **nine** runs from 09-14 to 09-22 01:20Z each printed
    `already reported ... nothing to do`.
    **WHAT THAT COST.** Everything the last four passes added to this
    report is absent from the only artifact: `nearest_unpaired_dt` (#68),
    `nearest_qty_delta` (#69) and `attach_wait` (#70/#71) are not keys of
    the on-disk JSON at all, and `price_delta_median` is there but computed
    by the pre-#66 upper-straddler. #69's pass PROMOTED specifically to end
    a six-pass deferral and get that reading into production.
    **WHY IT SURVIVED FOUR PASSES.** Each pass wrote a NEXT-PASS line
    predicting the reading, and the 09-22 02:45Z one asserted the 01:20Z
    run "carries the first production `nearest_qty_delta`". It carried
    nothing; it did not replay. One `journalctl -u hyxlab-divergence`
    falsifies all four in a second -- and that is this repo's own #54 rule
    ("prove a path ran by grepping for the line it prints, absence of the
    line is the whole proof"), never applied to a REPORT because the rule
    was filed under daemons.
    **RULE.** A cache key must cover every input to the cached value. A
    report is a function of (subject, code); keyed on the subject alone it
    freezes at whatever code first produced the artifact, and it fails
    SILENTLY at exactly the moment you add instrumentation -- i.e. when you
    are looking for a reading. Corollary: the failure is invisible in the
    unit's own logs, because "nothing to do" is the line a healthy day
    prints too.
    Fix: `hyxlab/importclosure.py` now owns the EXP-1276 closure walk (moved
    out of `scripts/daemon_imports.py`, which is a CLI over it -- the
    restart decision and the staleness decision must not hold two answers to
    "which files does this module execute", the `attach_budget_s()` pattern
    from #70). `closure_sha` hashes every file of the closure, path and
    bytes; `report_code` stamps it into the report; `--if-new` skips only
    when run_id AND sha match, and a missing or unreadable stamp re-derives
    (#74's direction: an unknown must not take the cheap branch). The stamp
    is the CLOSURE, not the root file -- `attach_wait` lives in
    `hyxlab/store.py`, so a root-file stamp would have answered "same code"
    for #70/#71. Not a hand-bumped version constant, because a constant
    somebody must remember to bump is the class of claim #70 and #71 were.
    Cost of the mechanical answer: a comment-only edit also re-derives, at a
    measured 9m06s / 1.9G peak (journal 09-12), paid at most once per change
    instead of once per day. Swept: `--if-new` is the only artifact cache of
    this shape in `collector/`, `simulator/` and `hyxlab/`.
    `tests/test_hyxlab_divergence.py` pins the skip, both re-derive paths
    and that the sha moves when a non-root closure file moves; verified red
    by restoring the run_id-only branch (2 failures).
    **Verified in the real unit, both directions, the same pass** (not
    deferred to a next-pass note -- the habit that let this sit for nine
    days): against the unstamped artifact `hyxlab-divergence` printed
    `but by DIFFERENT code (report None != ... closure 3635c64...) --
    re-deriving` and replayed in 8m58s / 1.6G peak for an identical
    18,982-fill result now carrying `report_code`; started again 13s later
    it printed `already reported ... nothing to do` and exited at once.

77. **2026-09-23 -- the one verdict branch that ASSERTS an effect had never
    run in 54 runs of ledger history, and when it finally ran it published
    neither its control nor its margin.** (Class: the-number-a-reader-quotes
    / untested-branch; site: `simulator/shadow_diurnal.py`
    `level_shape_verdict`, swept to `level_split_verdict` and
    `settlement_verdict`.)
    **WHAT.** Run `20260912T023431` banked its 10th panel day overnight --
    the FIRST run ever to reach `panel_days_needed`, on a ledger where the
    two longest predecessors died at 9 and 8 -- so `level_shape_status`
    read `powered` and 11Z cleared the ceiling at -129.1/hr,
    p=0.001953 <= 0.002083. That fired the `sig` branch of
    `level_shape_verdict`, which no reading in this repo's history had ever
    taken. The string a status page quotes said "HOUR-OF-DAY LEVEL EFFECT at
    11Z" and stopped, while `settlement_status` in the very next field read
    CONFOUNDED: 47 of 240 contributing rows carried a settlement, the hour
    keeps -109.5 of its -129.1 over 2 of 10 draws, and the balanced control
    ranks it 4 of 24. The two had only ever been read together because the
    CLI happens to print them adjacently.
    **AND THE MARGIN WAS NOT WHERE IT LOOKED.** p=0.00195 beside a 0.00208
    ceiling reads as "just cleared, marginal". It is the opposite: `_sign_p`
    is granular in 2^-n, so at 10 draws 0.001953 (10 of 10) is the ONLY
    value under the ceiling and the next rung down the ladder is 0.0215
    (9 of 10) -- ten times over, from ONE day moving. Between
    `panel_days_needed` (10) and `panel_days_needed_one_dissent` (14) the
    test is all-or-nothing: the panel can report a unanimous hour and
    nothing weaker, and every claim it makes is retracted by one day.
    **WHY IT SURVIVED FIFTEEN BOUNDS.** Every one of them hardened a branch
    that RUNS. `underpowered` and `FLAT` make no positive claim, so their
    prose needed no control; `sig` was dead code with a live audience. A
    branch no reading has ever taken is not covered by the fact that the
    module is careful.
    **RULE: a verdict branch that has never executed is unreviewed, however
    old the module is. When a threshold is finally crossed, read the branch
    it unlocks BEFORE reading the number it prints -- and a claim's margin
    is stated in the unit the reader can act on (days), never in the p
    value, whenever the test statistic is granular.**
    Fix (bound 16): `claim_margin` publishes per named hour its k/n,
    `flip_margin_days` and `unanimous`, plus the panel's
    `min_flip_margin_days` -- None, not an empty block, when no hour clears;
    `panel_days_needed_one_dissent` sits beside `panel_days_needed`; and the
    `sig` verdict carries the margin in days and the settlement control's
    status in its own sentence. `level_shape_status`, `significant_hours`
    and `sign_p_ceiling` UNCHANGED, the same refusal bound 15 made.
    **Swept the same pass** (the `reports.md` rule): bound 15 stopped the
    LEVEL verdict naming a tied argmin and left the identical
    `min(table, key=sign_p)` at two more sites -- `level_split_verdict`,
    whose "a MARKING move on the standing book" is the loudest claim this
    module makes about one hour, and `_settlement_control`, which SCORES
    that hour and can return `survives` on it. 26 of the 29 archived panel
    states have a tied minimum, so on most of the archive both sentences
    named an hour the clock picked. The split now names no hour when the
    minimum names none, and otherwise defers to the control out loud
    (`reval` is a residual and absorbs settlement, bound 11); the control
    keeps its subject -- a control needs one -- and says in its prose and in
    `settlement_check.anchor_tied_n` that the subject came from a tie.
    11 tests, all red-verified against the old module (suite 1581 -> 1592).

78. **2026-09-23 -- "96.3s spent waiting in total" was mostly the sweep
    opening its own database, because the attach ledger charges the retry
    budget for work the budget does not govern.** (Class:
    the-number-a-reader-quotes / statistic-measures-something-else; site:
    `hyxlab/store.py` `AttachWait`, swept to `collector/sweep.py` and
    `simulator/atlas.py`.)
    **WHAT.** #71 put every attach on a ledger and #72 made its statistics
    describe the whole run rather than the retained tail. Both recorded ONE
    number per attach: `waited_s`, the elapsed time inside the retry helper,
    which is the sleeps PLUS the cost of the open attempts themselves. The
    justification is on the line above the dataclass, and it is a true
    statement about the wrong case: "a refused attach itself costs 0.03ms,
    so the elapsed time IS the sleep ladder". That holds for an EXHAUSTED
    ladder, which was the case measured. A SUCCEEDING attach pays the full
    cost of opening the file, and the ladder it is being charged to was
    never entered.
    **AT THREE ATTACHES IT IS INVISIBLE; AT 7,483 IT IS THE WHOLE NUMBER.**
    The divergence report makes three attaches and publishes 0.008s / 0.011s
    against 28s and 58s budgets -- `budget_frac` 0.0003, noise. The first
    production sweep line, 09-22 11:52Z, published 7,483 attaches and "96.3s
    spent waiting in total". Measured 2026-09-23, 30 read-only opens of the
    20GB archive, first excluded: 7.2ms median, 9.1ms max. That is **53.8s
    of the 96.3s at minimum -- 56%** -- and the sweep's open is read-WRITE,
    which runs `_SCHEMA` on every burst and costs more, so the honest
    reading is that the published contention figure may be entirely open
    cost and the ladder may never have been entered at all.
    **AND NOTHING PUBLISHED COULD TELL.** `waited_s_max` 2.0s and
    `waited_s_total` 96.3s are the same two numbers for one 96s block (one
    long reader) and for 48 x 2s (constant beating) -- and those two call
    for opposite decisions about a 298s budget. The ledger exists to make a
    budget's erosion readable before the run it kills; it was reporting a
    quantity that erodes with the SIZE OF THE ARCHIVE instead.
    **RULE: a statistic named after a budget must be measured in the same
    unit the budget is denominated in. A retry budget is a sum of sleeps, so
    its numerator is sleep -- not elapsed time in the function that sleeps.
    And when the per-observation cost is small but the count is large,
    "negligible" is a claim about the product, not about the cost: re-derive
    it at the new n before carrying the old justification forward.**
    Fix: `AttachWait` records `slept_s` (timed AROUND `time.sleep`, not
    summed from the intended delays -- a stubbed sleep must read as zero and
    a real one overshoots) alongside `waited_s`, and exposes
    `open_s = waited_s - slept_s`. `budget_frac` is now `slept_s / budget_s`
    -- a sleep against a sleep budget -- and stays `None` only when there is
    no budget, so 0.0 now means the real statement "the ladder existed and
    was never entered". The block adds `slept_s_max`, `slept_s_total`,
    `open_s_total` and `contended_n`; the sweep line and the atlas lines
    print the two costs apart. NO BUDGET RE-SIZED: `budget_frac_max` is the
    tripwire, per #70, and the 09-24 06:10Z sweep is the first run whose own
    split is readable. 5 tests, red-verified against the old semantics
    (suite 1592 -> 1597).

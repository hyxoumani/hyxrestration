# Shell / Process Ops

- Never `pkill -f` / `pgrep -f` a pattern that appears in your own
  command line — the harness wraps commands in `bash -c`, so the
  pattern self-matches and kills your own shell (mistakes #4, #11).
  Use a bracket class (`sim[u]i`), or kill by a PID you hold.
- Long-running background jobs: `python -u`, harness-tracked tasks
  only — never nohup chains without captured output (mistakes #5).
- A job meant to OUTLIVE the turn must be a `systemd-run --user`
  transient unit (journald-captured, session-independent). Harness
  background tasks die with the session — the 08-04 drain died
  silently at launch this way (mistakes #19). Verify liveness by
  querying the job's persisted state (DB rows, journal), never by
  trusting that it was started.
- Multi-hour DuckDB writers exist (poly sweep 15.5-17.4h wall, 930-1042 min over 17 done lines 09-27..10-14, median ~966; 10-05 absent = the boot abort; since 2026-10-17 QA reads every done line against a 17.5h wall budget, `qa.BATCH_RUN_BUDGET_H`; 10-18 ran 17.57h = FAIL; since 2026-10-19 the sweep skips the fetches for markets a prior run archived as resolved -- 67% of the enumeration, 74% of its requests -- so the band was re-measured on 2026-10-21 from the first three skip-era done lines, 420-463 min / unit walls 7.00-7.72h, one incident-inflated: the budget is 8.0h, and the 10-18 line keeps failing it until it ages out on 10-25; 10-22 ran 486.3 min / 8.17h = FAIL, the excess entirely the #131 repair's 322 extra `closed_fetched` markets at ~5.0 s each, so the constant stands and the QA breach line now carries the done line's segments, mistakes #132). Sim-side readers
  degrade + retry lazily; never wait on the archive lock in a loop.
- Ad-hoc queries on ANY live DuckDB (hyxlab.duckdb, hyxstream.duckdb,
  hyxshadow.duckdb) MUST connect read-only (`hyxlab.store.connect_retry`
  or `read_only=True`). A default read-write connect takes the writer
  lock and made the shadow daemon crash mid-persist, ending a 1d20h
  run (mistakes #20).
- A daemon that OWNS a DuckDB file (streamd -> hyxstream.duckdb, shadow
  -> hyxshadow.duckdb) must take `hyxlab.lockid.db_owner_lock_or_reason`
  and exit 75 when refused. DuckDB's own file lock is held only for the
  duration of each write burst, so it excludes nothing between them: two
  copies interleave duplicate rows into tables with no key and no dedupe
  (EXP-1372, measured). Never start a second copy by hand to "check"
  something — the archive is unrecoverable.
- Never attach DuckDB with a bare `duckdb.connect` outside
  `hyxlab/store.py`. Use `connect_retry`/`open_retry`/`Store`, or
  `hyxlab.store.duck_connect` when the site hand-rolls a retry budget,
  degrades on error, or owns the file. A bare attach inherits DuckDB's
  DEFAULT spill directory, `<db>.tmp`, which is shared by every process
  that opens the file and whose temp files carry no pid: two spilling
  processes there SIGSEGV or read each other's blocks as their own data
  (EXP-1373, measured — the same pair with separate directories is
  clean, every time). Two readers of one archive are legitimate and take
  no lock, so no lock covers this; the kernel gives each connection
  `<db>.tmp/pid-<pid>` instead. Never pin `temp_directory` to a constant
  path — that is the bug with a name on it.
- `MemoryPeak` on a cgroup-capped unit is NOT the process's footprint.
  `memory.max` charges the PAGE CACHE the unit's own reads pull in, and
  the kernel reclaims that rather than killing, so a healthy daemon doing
  heavy file I/O reports `MemoryPeak` == its cap forever. Measured
  2026-08-27 on `hyxlab-stream` (cap 2G): `MemoryPeak` 2,147,500,032,
  `MemoryCurrent` 1.08 GiB, `memory.stat` anon 68 MiB / file 839 MiB. The
  health signal is `memory.events` — `oom_kill 0` with a large `max`
  count is reclaim, not death; `oom_kill > 0` is the kill. Read the
  cgroup, never `MemoryPeak` alone.
- A daemon's shutdown path is production code only if the signal that
  actually stops it reaches that path. systemd stops a unit with
  SIGTERM, and Python's default disposition for SIGTERM is the OS one:
  terminate at once -- no `finally`, no `atexit`, no context-manager
  `__exit__`. `collector.streamd`'s final drain, commented "never lose
  buffered events", was reachable only from Ctrl-C and `--smoke` and had
  run ZERO times in production (mistakes #54; 14 days of journal, 4
  starts, 0 shutdown lines). Before writing cleanup into a `finally`,
  name the signal the supervisor sends and install a handler for it --
  then prove it by grepping the journal for the line that path prints.
  Absence of that line is the whole proof. SIGKILL and a cgroup OOM kill
  skip every one of these regardless, so cleanup that must survive those
  belongs on disk, not in a handler.
- "Single writer" has to name its concurrency UNIT. The daemon-owns-the-
  file rules above (EXP-1372 lock ID, EXP-1373 spill directory) exclude a
  second PROCESS and prove nothing about the threads inside the one
  legitimate owner. `streamd` ran its shutdown drain CONCURRENTLY with a
  periodic flush on one `StreamStore`, because `run()`'s `finally` cancels
  the flusher task and cancelling a task awaiting `asyncio.to_thread`
  returns in 0.00s while the worker thread keeps running (joined only at
  interpreter exit). Measured: 400 sidecar rows archived as 800, and a
  60-row `spill_all` deleted by the in-flight flush's post-commit unlink --
  absent from archive, sidecar and buffer, with the gap rows that would
  have marked the hole in the same lost batch (mistakes #62). So: a
  buffered writer takes its OWN lock across every buffer/sidecar mutation,
  a shutdown path acquires that lock across its decision AND the write it
  leads to, and it waits with a bound charged against the supervisor's stop
  budget -- never `cancel()` as a synchronisation primitive.
- A cleanup handler that RUNS is not a cleanup handler that FINISHES.
  systemd SIGKILLs a unit `TimeoutStopSec` after SIGTERM, so a shutdown
  path has a row budget, not just a code path. `streamd._final_drain`
  flushes at a measured 7,100 rows/s against an unbounded sidecar (a 7h
  wedge is 6.3M rows, ~890s) while the unit inherited systemd's 90s
  DEFAULT -- the deadline was not written down anywhere in this repo.
  Losing that race is worse than declining it: SIGKILL lands after
  `flush()` has moved the buffers into locals, skipping the `except` that
  restores them and the spill that would have saved them (mistakes #61).
  So: pin `TimeoutStopSec` in the unit, measure the cleanup's throughput,
  and make the cleanup REFUSE work it cannot finish in the budget --
  preferring the fast lossless path (spill to disk, drain next boot) over
  the slow one that gets killed halfway.
- A lock held ACROSS an operation is held only if the operation cannot
  drop it, and on POSIX any `close()` does. DuckDB's file lock is an
  `fcntl` record lock, and POSIX releases every record lock a process
  holds on a file the instant that process closes ANY descriptor on it
  -- not just the one the lock was taken through. `collector.backup`
  held a read-only attach across `shutil.copyfile(src, ...)`, which
  opens the source and closes it: measured 2026-09-26, an outside
  process is refused before that close and takes the file READ-WRITE
  after it, with the holder still attached (mistakes #90). So: inside a
  hold, copy from a descriptor opened once and closed only after the
  connection is gone (`os.copy_file_range`, which also reflinks), never
  by path -- and prove the hold AND the release from outside the
  process, both arms, because a test that only checks the release
  passes equally against code that never locked.
- A measured fact with an unmeasured CAUSE cannot tell you which changes
  are safe. `collector.backup`'s 26 GB copy took 0.2s for months because
  /home is btrfs and dest shared the filesystem, so `copyfile` reflinked
  (measured: `--reflink=always` 0.199s vs `--reflink=never` 20.5s on the
  same NVMe). Nothing recorded that, and the repo's own written next
  step -- point `HYXLAB_BACKUP_DIR` at an off-box mount -- deletes the
  reflink and turns a 0.2s writer exclusion into minutes. Before
  trusting a cheap number in a journal, name the mechanism that makes it
  cheap, then ask which planned change removes it. And ask the mirror
  question: what the same mechanism makes the COST. `qa_disk_headroom`
  charged the rotation as x8 full copies for 11 days while those slots
  were reflinks holding 0.57% exclusive (3.03 GB of 534.5 GB; mistakes
  #139). `stat` reports a reflinked file at full size; only
  `btrfs filesystem du -s --raw` prices a slot. A multiplier in a
  capacity model is a claim about a mechanism the repo may already have
  measured -- for the benefit -- elsewhere.
- A SHARED filesystem's burn is the sum over EVERY tenant, and the tenants
  you did not think of are the ones outside the workspace. The 2026-10-26
  chase of a 69 GB/day `archive disk headroom` reading searched
  `~/workspace` and `~/scratch`, attributed 42 GB and called 27 GB
  unattributed; the rest was in `~/.local/share` and `~/.cache` (a Steam
  shader-cache rebuild, 12.9 GB in 85 min; a neighbour's research DBs,
  8.5 GB; Firefox 1.9 GB). Enumerate with `du -sx -BG /home/devs/* /home/
  devs/.local/share/* /home/devs/.cache` BEFORE attributing, and locate a
  step with the collector's 5-min `fs_free_b=` journal field (hour
  resolution) rather than the QA series (one sample a day, #93 rule).
- An instrument for a SHARED resource has to measure both directions:
  what contention cost this caller, and what this caller cost the
  contenders. Only the first is visible from inside the caller, which is
  exactly why only the first ever gets built. `hyxlab.store.AttachWait`
  recorded `attempts`/`waited_s`/`slept_s`/`open_s`/`budget_frac` -- all
  five the harm a contended reader SUFFERS -- and so called the worst
  lock event in this archive perfect: the 2026-09-25 divergence attach
  got in on its first attempt in ~10ms spending 0.0s of a 30s budget,
  then held `hyxstream.duckdb` 45m45s and cost streamd 387,856 rows to
  the torn-append sidecar (mistakes #91). Hold the file through
  `store.held_attach`, which times from when the CONNECTION EXISTS (the
  lock is taken by the open, not by the first query -- that hold was 97%
  idle connection) and records in `finally` (a replay that dies at minute
  40 held it for forty minutes). An unmeasured hold is `None`, never
  0.0, and the report block says `held_unknown_n`: average the
  uninstrumented in as zero and a 45-minute reader publishes a flawless
  maximum.
- When the only witness to a fix is the SILENCE of whatever it used to
  hurt, the fix is not instrumented. The #89 copy-out was confirmed in
  production only by streamd's stall ledger staying quiet across the new
  closure's first run -- evidence that holds only while the victim
  happens to be writing into the window. Measure at the holder, then the
  reading survives the victim being idle, restarted, or fixed.
- A hold is charged to an attach ROW, so only a ledgered attach can carry
  one. `store.held_attach`/`held_open` (and `charge_hold`, the seam a
  wrapper like `shadow.stream_conn` borrows) credit the seconds to
  `_LAST_ATTACH` -- the row the caller's own open recorded. `duck_connect`
  and a bare `Store(...)` record no row, so wrapping one charges its hold
  to whatever unrelated attach happened to be last: a WRONG reading,
  which is worse than the missing one. Instrument at a helper that
  ledgers, or leave the site enumerated as debt
  (`tests/test_hold_discipline.py`) -- never split the difference.
- An instrument that learns a quantity by SAMPLING has a RESOLUTION, and
  below it the number it reports is the sampling period wearing the
  quantity's name. `data/stream_stalls.jsonl` was named (in #91) as the
  half that "measures the hold exactly and always did", because it sees
  both ends of a stall. But streamd learns the archive is unwritable only
  by ATTEMPTING a flush, every `FLUSH_SECS = 15`, so an episode's span is
  the hold rounded up to the next attempt: measured 2026-09-27, 254 of
  275 closed episodes (92%) span 18.56s +/- 1.63s -- a constant -- and
  holds of ~0.1s, 7.141s (the holder's own `held_s`) and ~0.1s that night
  all logged ~19.3s (mistakes #92). So: publish the period and the bound
  the samples actually prove (`hold_lower_s = (fails-1) * period_s`, 0.0
  for the 92%), never a bare span; inject the period rather than reading
  the module constant at write time, or retuning it re-scales every
  archived record; and before trusting a victim's duration over a
  holder's, ask how the victim found out. A field named `duration_s` and
  documented "exact" invites every reader to difference it -- name a span
  a span.
- The same resolution rule reaches every detector that learns of an
  ABSENCE by the absence of a periodic event -- not just pollers.
  `qa._check_continuity` reports the widest interval between consecutive
  cycles of a 5-min writer, and one full cadence of that span is always
  the timer: measured 2026-09-27, all three writers' worst 24h gap is
  5.2-5.6 min, i.e. one cadence, on a day nothing was down, and the
  2026-08-20 outage's true 4h19m printed as a 264.8 min span (mistakes
  #93). For cycles `c` apart and a gap `G`, downtime is in
  `[max(0, G - 2c), G]`, so `G = c` and `G = 2c` prove ZERO. Publish the
  span as a span plus that bound, print a proven-zero bound as
  "unresolved below the cadence" rather than `0`, inject the cadence so
  the next writer on a different timer must name its own -- and leave the
  VERDICT on the span, which is the conservative side of the bound and so
  alarms early rather than late.
- A field whose definition UNIONS two populations is correct only while
  one of them is empty, and the emptiness is usually a property of the
  CALLERS, not of the field. `held_unknown_n` was `observed_n - held_n`,
  documented "holds nobody measured" -- but `connect_retry` ledgers an
  EXHAUSTED attach too, from the `except` on its last attempt, and that
  attach never opened the file: a MEASURED zero, not an unknown. The field
  was really "attaches with no hold", and it read correctly only because
  every publisher was a batch report, which RAISES out of the run a
  refusal happens in and never reaches a block. `simulator.shadow` is the
  first publisher that SURVIVES one (`poll_once` swallows `duckdb.Error`
  and polls again in 20s, forever), so it would have published a forever-
  rising `held_unknown_n` reading as instrumentation debt and really being
  contention -- the number already one field to the left, `exhausted_n`
  (mistakes #94). So: before reusing an instrument in a new KIND of
  caller, ask which of its terms the old callers made unreachable.
- A limit written into a docstring is documentation, not a deadline.
  `tests/test_hold_discipline.py` named its own blind spot exactly and
  correctly -- `simulator.shadow._read_new` attaching a daemon-owned
  archive every ~20s and across a 2,084,503-row seed replay, with no
  artifact to publish a hold into -- and that note was carried forward
  four passes while the thing it described ran in production for 77 hours.
  Enumerate the debt in an ASSERTION that names each site (the way
  `UNLEDGERED` and the `UNCAPPED` list do), so paying one off deletes a
  line and the list cannot outlive the debt.
- A lock is leaked by the code BETWEEN the open and the caller, and that
  code is usually a tuning line nobody thinks of as a failure path.
  `simulator.shadow.stream_conn` attaches through `connect_retry`, then
  lowers `memory_limit` and re-derives the spill bound -- and an exception
  from either left a read attach on `hyxstream.duckdb` with no reference
  left to close it: held until the daemon exits, against the ~0.1s a poll
  needs. The same shape is still live one level down, inside
  `connect_retry`'s own retry loop, where a post-connect tuning failure is
  caught by the `try` that then sleeps and connects AGAIN with the first
  connection open and unreferenced. A wrapper that owns a connection
  between the open and its caller closes it on every escape path.
- A field added to a shared structure reaches every publisher that
  SERIALISES it and no publisher that LISTS it -- and the one that lists
  it is usually the one with no artifact to fall back on. The hold half
  of `attach_wait_block` (#91, #94) landed free in `atlas`, `divergence`,
  `run_l2` and `shadow`, all of which `json.dumps` the dict, and never
  reached `collector.sweep`, whose hand-written journal line IS its whole
  reading. Measured: the 2026-09-27 06:10Z sweep computed the first
  `held_s_total` for the 150 x 2.0s burst ladder over 7,549 attaches and
  dropped it at the format string; the number is unrecoverable
  (mistakes #95). Render a shared block in ONE place and assert
  completeness by PERTURBATION -- bump each field of a real block and the
  line must change -- because a scan of the renderer's source passes
  against code that reads a field into a local and drops it. Then forbid
  the second renderer: no module may subscript a block key into an
  f-string. Note which tests this defeats: three source-scraping tests
  covered that line and all stayed green, because they pinned f-string
  literals in the source rather than the rendered string.
- A carve-out from a discipline is only as good as WHAT its argument is
  about. `tests/test_hold_discipline.py` excused four attaches because
  `duck_connect`/`Store` record no ledger row, so a hold charged there
  would land on an unrelated attach -- every word of that about the
  INSTRUMENT, none of it about the file lock, which is identical. The
  site the note itself called "the one that matters" was a 24/7 daemon
  holding `data/hyxlab.duckdb` -- the 5-minute collector's file -- across
  an hourly 101k-row query, charged to nobody (mistakes #96). Fix the
  premise: both now record a one-attempt, no-budget row (`budget_frac`
  `None`, never 0.0 -- a ladder of one has no budget to spend a share
  of), `held_duck`/`held_store` are the seams, and the enumeration was
  DELETED rather than emptied. Then ask the second question, which is
  the one that bites: which existing readings were correct only because
  the debt kept a population empty? A process-wide `attach_wait` block
  meant "this file" for any daemon whose only ladder ran against one
  file, and `simulator.shadow` publishes exactly that under exactly that
  name -- so the block needed a `db` scope, from untrimmed per-db totals
  (filtering the 256-deep sample rows reports a run's tail under the
  run's name, #72), rendered through ONE function per publisher (#95).
- "The same shape exists one level down" is part of the fix, not a note
  for next pass. `simulator.shadow.stream_conn`'s tuning-failure leak was
  fixed on 2026-09-27 and the identical shape inside `connect_retry` --
  where the `except duckdb.Error` cannot tell a tuning failure from a
  refused open, so it sleeps and reconnects with the first connection
  open and unreferenced -- was named in the same pass and deferred. There
  were four copies of `connect` + three tuning calls, three of them
  inside `hyxlab/store.py`, the module that exists to be the one place
  this is right (mistakes #97). Sequence it once, and close on
  `BaseException`: a cancellation between the open and the return leaks
  the lock as thoroughly as an error does.
- A DuckDB release test that never leaves the process is not a release
  test. DuckDB serves a second SAME-PROCESS attach from the instance it
  already has open, so an in-process re-attach succeeds against a leaked
  connection: measured 2026-09-28, every leak arm stayed green with the
  fix reverted until the probe became a subprocess. Probe from outside,
  carry a vacuity guard proving a held attach IS refused, and probe where
  the leak has a DURATION (inside the retry ladder's sleep). For a
  standalone raise, hold the traceback (`pytest.raises(...) as excinfo`)
  -- CPython's refcount closes the connection the moment the temporary
  traceback drops, and without it the test is green against the leak.
  This is #90's rule ("prove both arms from outside") reaching the helper
  a pass after it was written about the caller.
- A detector whose EXPECTED population is inferred from the events that
  HAPPENED is blind in exactly the direction it exists to look.
  `qa.read_fade_windows` counted holes as `max(0, starts - completions)`
  over the 23:00-04:00Z window, both counted from the collector's
  journal: a slot the timer never fired in is absent from BOTH terms,
  cancels, and renders as a clean night. Measured 2026-09-28 -- the
  2026-08-20 outage left one 265.0-minute gap in `snapshots`, 21:30Z ->
  01:55Z, i.e. 35 of that night's 60 slots, and the 25 cycles that did
  run all completed, so the check printed `0 lost cycle(s)` for the
  largest capture hole in this archive (mistakes #98; the same outage is
  #93's example, printed there as a 264.8-min span). Take the
  denominator from the CADENCE -- what was owed -- inject it so a
  retuned timer must restate it, and publish owed / started / finished
  as three numbers, never one difference. Pair the two event kinds
  across the window edges over a read one cadence wider than each,
  or a straddling cycle adds a completion with no start and
  `max(0, ...)` clamps a real hole to zero. And when a window has
  rotated out of journald, non-activation is UNRESOLVED, not 60 losses.
- A CLI's human output is not data. `journalctl -o short-iso` prints
  `-- No entries --` to STDOUT on an empty read, so
  `bool(text.strip())` reads an empty journal as content: the
  fade-window check's sweep attribution was therefore True for every
  window ever examined -- seven consecutive QA runs announced the poly
  sweep inside 7 of 7 windows while `-o cat` over the same spans
  returns zero lines (mistakes #99). An absence rendering as PRESENCE,
  in a check whose docstring is about the opposite mirror. Before
  testing a subprocess's stdout for truthiness, ask what the tool
  prints when it found nothing; answer it in ONE predicate, since the
  same sentinel defeats every yes/no question asked of that output; and
  make the fixture emit what the real tool emits -- the test that
  covered this flag fed it a line with no timestamp, which is nothing
  journalctl can produce, so it was green by mimicking the defect's own
  tolerance.
- A detector whose subject is how much was LOST selects, in its WHERE
  clause, on who was alive to write the row. `qa._capture_gap_minutes`
  budgets the volume of `stream_gaps` -- the excuse channel every other
  reader in this repo treats as a suppressor -- and read
  `venue='kalshi' AND channel IN ('books','trades')`: `reconnect`,
  `seq_reset` and `dead_air`, all written by a LIVING daemon about one of
  its own connections. `mark_startup_gap`'s `daemon_start` row, the one
  that says the daemon was DEAD, is `venue='*', channel='*'` and was
  dropped. Measured 2026-09-28 over all 83 daily slots in the stream
  retention: the 07-21 window reported `books 9.5 / trades 16.1 min` and
  PASSED across a 17-hour outage (1,019.7 min, truly 1,029.6 / 1,036.2,
  34x the budget), while the outages it DID catch (08-21, 09-22) were a
  daemon killed and restarted in a loop by a host OOM storm, which emits
  channel-scoped rows on the way down (mistakes #100). So: enumerate the
  kinds of row that can record a loss BEFORE filtering, and ask which
  kind the failure itself prevents from being written. And when two
  readers of one table disagree about its scoping in the same file -- the
  seq check twenty lines up had always excused on `OR venue = '*'` --
  that disagreement is a finding, not a style difference.
- A perturbation test is evidence only once you have watched the
  perturbation LAND. The renderer test for the above was red-verified by
  a `sed` that matched nothing, because ruff had joined the f-strings it
  was written against; the test passed green against an unchanged file
  and looked like a passing mutant. Assert the mutant changed the source
  (diff it, or fail the edit) before reading the test's colour.
- A rule that makes a shared resource's cost legible has to reach every
  CALLER of that resource, not every caller that calls itself a report.
  `tests/test_hold_discipline.py` binds "measure your holds" to modules
  that publish an attach block, and `collector.qa` published none -- so
  the one unit that attaches all three live DuckDB files, daily, was the
  last reader outside the rule. Measured on its first instrumented run
  (2026-09-29 02:29Z): `hyxlab.duckdb 1.1s, hyxshadow.duckdb 0.0s,
  hyxstream.duckdb 819.0s` -- 13m39s of exclusion of `collector.streamd`,
  with the victim's ledger independently naming the process, 12 failed
  flushes and 158,408 rows buffered (mistakes #102). A reader's hold is
  the length of its SECTION, not of its query: `_connect_ro` handed the
  connection to a body that ran for minutes. So scope the hold with a
  `with` (`collector.qa._held_ro`, `charge_hold`), publish it from a
  `finally` so a run that died still says what it held, and keep the
  section-completion write OUTSIDE the release. And before running any
  standing report ad hoc against the live archives, know that you are
  buying that exclusion now rather than at its timer: this one also made
  a 1804-test suite take 15.5 minutes and fail a freshness assertion on
  its own clock.
- An instrument learns about a resource only from operations that REACH
  it, and the operation that does not is the cheap early-out nobody
  counts as a code path. `StreamStore.flush()` returns without attaching
  when the buffer is empty, and `streamd.flusher` called `stalls.ok()`
  on that return -- so a tick that never opened `hyxstream.duckdb` was
  read as proof the archive was writable, closing an open stall episode
  mid-hold and refusing to open one while a reader still held the lock
  (mistakes #103). Measured first, from a SECOND process, both arms: a
  read-only attach refuses a writer 21ms after the OPEN with no query
  ever issued and admits it 59ms after the CLOSE, so the premise under
  #91/#96/#102 is sound and it is the VICTIM that under-reports. So:
  before reading "the operation succeeded" as "the resource was
  healthy", enumerate the return paths that never touched it, and make
  the return value say which happened (`FlushOutcome.touched`) -- a row
  count cannot, since a drain of an empty sidecar opens the file and
  writes nothing. Corollary that CORRECTS #92 rather than extending it:
  a victim's episode span is not an upper bound on the hold either, because
  the episode begins at the first sample that had work to do. Only the
  holder measures a hold.
- A sampling period published as an instrument's RESOLUTION must be
  measured, never read off the constant the loop sleeps: the loop sleeps
  it and then does the work, so the observed spacing is always longer and
  here was 6x longer. `hold_lower_s = (fails - 1) * FLUSH_SECS` said
  300.0s where the ledger's own two timestamps said 483.1s; measured over
  the 21 failures of the 2026-09-29 02:34:45Z episode, the spacings ran
  15.2s to 93.0s against a 15.0s constant, one 93s hole inside one
  continuous stall. Derive a bound from timestamps you HOLD, and publish
  the widest spacing actually observed (`sample_gap_max_s`) beside the
  nominal cadence rather than instead of it -- records written before the
  measurement exists must still be legible as nominal.
- A recovery path whose cost grows with the outage it recovers from turns
  a long outage into a permanent one. streamd's sidecar drain parsed the
  whole file in one transaction (~750 B/row), so a 1.69M-row spill could
  not fit the 2G cap. The sidecar survives restarts by design, so every
  boot re-ran the same OOM: 189 kills in 1h53m (mistakes #112). Bound
  every replay of durable state by a constant (`DRAIN_CHUNK_ROWS`) and
  checkpoint it, so a kill makes progress instead of repeating.
- A "safe decline" between two racing writers of one file is a bounded
  loss, and the bound is the length of the RACE WINDOW in the units of the
  outage, not of the code. `streamd`'s refused drain (#62) let the buffer
  die rather than spill into the sidecar an in-flight flush would unlink,
  reasoning the window was one flush long; under an outside hold the flush
  is stuck inside the hold and the window is the hold: 143,520 rows at the
  2026-10-14 poweroff (mistakes #119). The escape from a two-writer race on
  a shared file is a file only one writer can name (`spill_snapshot` ->
  `adopt_snapshots` at boot, before the second writer exists), never a
  refusal that leaves rows in memory.
- `memory.events` has a key that says reclaim had a COST: `sock_throttled`,
  the times the kernel clamped the cgroup's sockets under memory pressure.
  `max` does not predict it (stream: 37,742 `max` with 441 throttles on
  one run, 49,604 with 0 on the next; mistakes #121), and on a daemon
  that receives for a living it is the number the "reclaim, not kills"
  reading leaves out. Read it live from the digest's `CGMEM` line and,
  timed, from streamd's stats line (`sock_throttled=N (+d)` plus a
  `WARNING sock_throttled` line in the period it lands); a nonzero
  increment is the cue to read that period's GAP lines and stall ledger
  before calling the cap sized right.
- `max` in `memory.events` is a COUNT of reclaim events, and the count
  says nothing about what they cost. The cost is PSI, `memory.pressure`
  `full ... total=` (microseconds every task in the cgroup was stalled
  on memory). Measured 2026-10-14 15:07Z: the fresh stream daemon's
  43,761 `max` events were ALL accrued at boot (flat across 90s with
  file cache at 833M of a 2G cap) and cost 0.14s of stall over 36 min;
  poly-sweep's 5.18M events cost 27.9s over 10.8h (0.07%). Read the
  digest's `memory stall Xs (Y% of Zh)` and the stop line's
  `psi_full_us` -- a cap is costing something only when that share is
  material, whatever the event count says. A running oneshot never
  enters `active`, so its base is `ExecMainStartTimestamp`, not
  `ActiveEnterTimestamp`.
- Waiting on a oneshot unit from a script: poll `ExecMainExitTimestamp`
  (or the state set {inactive, failed}), never `ActiveState=inactive`
  alone -- a oneshot that exits non-zero lands in `failed`, and the
  11:20Z 10-14 waiter sat 50 min on a 28s QA run that way.
- A supervisor's timeout is a backstop, not a budget. `hyxlab-autoloop`'s
  only bound was the unit's `TimeoutStartSec=3h`, and under the never-stop
  working mode (one claude process alive across passes via ScheduleWakeup)
  both 10-14 runs reached it: SIGTERM at an arbitrary point in a pass,
  filed `result=timeout` -- a FAILURE on the digest's RECENT line -- twice
  a day (mistakes #122). The process that knows where a clean stop is owns
  the deadline: `scripts/autoloop.sh` runs claude under `timeout` strictly
  inside the unit's bound, exports `AUTOLOOP_DEADLINE`, and files the
  TERM-honoured stop as exit 0 with the stop named in the journal. A pass
  checks `date +%s` against it before arming a wakeup; under 30 min left,
  commit what is in flight and end clean -- the 6h timer is the armed next
  turn.
- A running shell script is read from disk BY OFFSET as it executes, so
  editing it in place edits the process. bash discards its read buffer
  before forking an external command and re-reads from the file when the
  child returns; the 10-14 20:15Z autoloop run shipped a longer
  `scripts/autoloop.sh` in place while its own launcher waited on
  `claude -p`, and the launcher resumed inside the new comment block:
  `syntax error near unexpected token ')'`, exit 2, unit `failed`
  (mistakes #123). So: a launcher's body is one `main()` called on the
  last line with `exit` on the same line (parsed whole before it runs);
  and a script that may be live is replaced by RENAME (`mv new path`,
  new inode -- `/proc/<pid>/fd/255` then shows `(deleted)` and the old
  copy keeps running), never by truncate-and-write. The Edit/Write tools
  truncate in place; check `pgrep -a -f 'autoloo[p].sh'` first.
- Two timers that share a RATE BUDGET collide in the seconds, and the
  hour-reading tests (pin, order, fade window) are blind to it. The
  tradepass fired `06:35:00 UTC` ON the 5-min collector's `*:0/5` tick for
  75 days; the hold instrument on the shared FILE read clean (`held 6.5s`,
  collector `wait_s 0.0`) while the shared API cost the collector 9-15 x
  429 retries and the band's slowest `fetch_s` six days of six (mistakes
  #125). Read a victim's cost by the TICK the suspect fires on, never by
  the day; a short daily unit on a shared budget starts past the periodic
  writer's p99 wall and ends before its next tick (`AccuracySec=1s`, or
  systemd's 1-min coalescing slides it back). The mirror rule: a suspect
  that is FLAT where the loss moves is refuted without an intervention.
  The sibling's grants were a constant 2,179-2,211/h across every
  sweep-idle hour while the collector's clean rate swung 100% -> 67% and
  the sibling's own 429s rose with ours, so the pre-registered `*:3/5`
  phase shift was closed untaken (mistakes #146). Bin both ledgers by the
  hour first: `python -m collector.health --kalshi429-hourly [HOURS]`, then
  by the second (`--kalshi429-pairs`): two tenants refused in the SAME
  second at flat own-load is one host bucket closing (20Z 10-28: 3 of 3
  idle-hour collector 429s within 0.3s of a sibling 429; the sweep's own
  tail 429s pair with nothing -- that is its own load).
- `daemon-reload` on a `Persistent=true` timer whose OnCalendar moved to a
  time of day that is already past (but after its last trigger) FIRES the
  service at reload -- systemd re-derives the schedule and counts the new
  elapse as missed. Measured 2026-10-17 08:27:58Z at promote: one extra
  tradepass run. Move such a timer only to a time not yet passed today,
  or promote before the old time fires; for the 12h sweep or QA an extra
  run is a surprise hold on the live archives (`scripts/promote.sh`).
  A NEW Persistent timer (no stamp file) does not fire at `enable --now`:
  measured 2026-10-17 14:30Z on `hyxlab-atlas.timer`, `LastTriggerUSec`
  empty, next elapse its first slot. The digest reads that as `FIRST-DUE`
  (ok-class, says when), not NEVER-RAN (mistakes #126).
- `journalctl --since/--until` parse a BARE timestamp in the box's LOCAL
  zone (CDT here, UTC-5); `--utc` changes only the OUTPUT. Measured
  2026-10-18: `--since "2026-10-18 06:00"` returned lines from 11:00Z,
  which read the 06:10Z kalshi sweep's 07:38-08:45Z `KXBTC15M mid-series
  flush` leg as ZERO flushes on six consecutive days against the 12-14 the
  same query finds with `--since "2026-10-18 06:00 UTC"`. Always suffix
  ` UTC` on a literal bound (relative bounds like `-2h` are safe), and
  prove a window once by printing its first line's timestamp before
  counting anything inside it.
- The OUTPUT side has the same trap: `journalctl -o short-iso` and
  `ls -l` print LOCAL time (CDT), and a plan that copies `22:30:00-05:00`
  as "22:30Z" schedules its read 5h early (mistakes #143; the backup
  timer is `03:30:00 UTC`). Read journals with `--utc`, list files with
  `TZ=UTC ls --full-time`, and take a unit's clock time from
  `OnCalendar`/`list-timers`, which print the zone.
- A promote that restarts a PRODUCER and defers its CONSUMER has changed
  the consumer's input without changing its code, and the deferral guard
  (`young_run_guard`, bounds 14/14b) reasons only about the consumer. The
  2026-10-23 12:45Z promote shipped the stream coverage expansion (~20x
  book events) with the shadow's universe filter in the SAME promote,
  restarted the stream and deferred the 9-day shadow run on its old
  unfiltered reader; the kernel OOM-killed it at its 1G cap 82 min later,
  `anon-rss:1039464kB` (mistakes #134). promote.sh now prints a HAZARD
  under the DEFERRED line when the stream really restarts; read the
  shadow's next 300s line -- events per poll and `rss_anon_mb` -- and
  restart it with `--restart-young` before the cap does. A daemon under
  `MemoryMax` publishes its own anon RSS (`/proc/self/status RssAnon`):
  the cgroup's numbers include the page cache and cannot show the climb.
- `RssAnon` is a SUM across allocators, and a sum fitted to the wrong
  term reads as growth in the work. The stream's anon was read against
  its book universe for two days while a regression on its own stats
  lines said uptime: 50.2 MiB/h with rate controlled (R2 0.90 vs 0.54),
  76% of it glibc malloc arena heaps -- glibc's DYNAMIC mmap threshold
  routes freed-then-reallocated large chunks into per-thread arenas,
  where they stay (mistakes #140). Rehearsed on the real flush: pinning
  `MALLOC_MMAP_THRESHOLD_=131072` cuts the growth 3x and empties the
  arenas; `MALLOC_ARENA_MAX=1` matches it by serializing; fewer DuckDB
  threads makes it WORSE; `malloc_trim` helps 30%. So: before fitting a
  daemon's memory to its load, regress it on hours; read the split the
  stats line now carries (`arena_mb=<MiB>/<heaps>`); and bucket
  `/proc/<pid>/smaps` anonymous regions by 64 MiB alignment to see the
  arenas directly. Any daemon that mallocs from many threads under a
  `MemoryMax` wants the threshold pinned in its unit.
- A DuckDB `memory_limit` is a budget only for the operators that ASK
  before allocating, and which operators those are is a property of the
  QUERY. `hyxlab-qa`'s 12G cap was sized on a curve where the poly-replay
  query refused cleanly at 1.8 GiB (`OutOfMemoryException`) and the unit
  comment promised an outgrown window would fail as one red check; on
  2026-10-26 the same query's ASOF/aggregate pipeline ran to anon 12.47G
  with 7.8 GB spilled past a 6G limit and the KERNEL killed it (mistakes
  #142). A cap is a blast-radius bound, never a budget a check fits into:
  bound the WORK (`qa.POLY_SLICE_HOURS`, `SEQ_SLICE_HOURS` -- a slice width
  with a per-key carry), measure anon peak vs slice width inside the run
  (`RssAnon`; the cgroup's `memory.peak` is page cache and read 11.3G for a
  run that only did `max(recv_ts)`), and re-measure a cap rationale when
  its query's input moves an order of magnitude (#134 was 20x).
- A process that holds a DuckDB file read-WRITE cannot open the same file
  read-only from another thread: DuckDB serves same-process attaches from
  one instance and refuses a different CONFIGURATION while one is open
  (`Can't open a connection to same database file with a different
  configuration than existing connections`). `streamd`'s boot read of its
  own `stream_lifecycle` asked `read_only=True` while the flusher held the
  writer, degraded quietly and shipped inert (mistakes #149). An owner's
  reads attach exactly as its writes do (`StreamStore.announced_opens`,
  registered in `tests/test_connect_discipline.py` `WRITE_ALLOWED`), and a
  test of such a read holds a writer connection open across it.
- A DuckDB connection carries ONE pending result, and the next `execute`
  on it replaces that result without an error: a generator streaming
  `fetchmany` from the connection ends at the caller's first query
  between yields as if the data had. Measured 2026-10-30: a
  `duckdb_memory()` sample after the first yield cut a 6.9M-row scan to
  exactly 12 x 65,536 rows and yielded six of eight markets empty
  (mistakes #150). Stream through `conn.cursor()` (closed in `finally`),
  and sample a hold from a different connection than the one streaming.

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
- Multi-hour DuckDB writers exist (poly sweep ~7h). Sim-side readers
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
  cheap, then ask which planned change removes it.
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

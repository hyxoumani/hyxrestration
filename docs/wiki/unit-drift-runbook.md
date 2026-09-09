# Unit-drift runbook — the two states a script must not clear

Written 2026-09-09. Audience: whoever is holding the box when
`collector.health` or `promote.sh --units-only` prints `SHADOWED` or
`DROP-IN`. The other verdicts are not here because they do not need a
person: `DRIFT` and `STALE-IN-MEMORY` are `REPAIRABLE`, and the repair is
`scripts/promote.sh --units-only`, which re-runs the judge afterwards and
reports what survived it.

## Why these two are handed to you at all

`promote.sh` installs by copying `scripts/systemd/hyxlab-*` into
`~/.config/systemd/user/`. Both of these states are, by construction,
untouched by that copy:

- **SHADOWED** — systemd's unit lookup walks a *search path*. A copy of
  the unit in a higher-priority directory (`/etc/systemd/user`,
  `/run/systemd/user`) is what loaded, so re-installing into
  `~/.config/systemd/user` leaves exactly the same file running. The
  repair is deleting or renaming a file this repo did not install, in a
  directory it does not own — a decision, not a script's business.
- **DROP-IN** — a `<unit>.d/*.conf` overrides directives *without
  touching the fragment*, so it survives every fragment rewrite. Same
  call, same reason.

A `--units-only` that ran the `cp`, reloaded and exited 0 would report
success on a box still running units nothing in this repo has read. It
does not: it asks the judge again and exits `2` for these two.

## Read the verdict before you touch anything

The verdict carries the fact the decision turns on. As of 2026-09-09 it
is not just a path (see the block above `_shadow_effect` in
`collector/health.py` for why that was not enough):

**SHADOWED** ends in one of three clauses.

| clause | what happened | urgency |
| --- | --- | --- |
| `text is byte-identical to the repo's copy (wrong file wins, same behaviour)` | a stale copy outranks ours and happens to agree with it. Behaviour today is correct; the box is one edit away from silently ignoring a promotion. | hygiene — fix at leisure, but fix it, because the next promote will appear to work and change nothing |
| `and its text DIFFERS from the repo's copy — the box is running a unit this repo does not contain` | systemd is executing something we did not write. | incident — read the file before anything else |
| `could not read it — cannot say whether its text differs` | the winning fragment is unreadable by this user. | treat as the row above until proven otherwise |

**DROP-IN** names each override file and the directives it sets:
`o.conf sets ExecStart(reset), ExecStart [RESET of the unit's command]`.
`Key=` with an empty value is systemd's reset for a list-valued
directive; on `ExecStart` that is the difference between *adding* a
command and *replacing* the unit's command outright — the measured
hijack. `UNREADABLE` there means the conf could not be read, never that
it overrides nothing.

The report stops there on purpose. Whether a `MemoryMax=` this repo did
not write is acceptable is yours to say; the checker's job was to make
sure you were not told the same word for both cases.

## Procedure

1. **Confirm what the manager holds.** `systemctl --user cat <unit>`
   prints the winning fragment and every drop-in, in load order, with
   each source path as a comment. This is the ground truth; the digest
   is a summary of it.
2. **Find out who owns the intruder** before deleting anything.
   `pacman -Qo <path>` (this box is Arch) names the package if it came
   from one — in which case removing the file will be undone by the next
   upgrade, and the answer is a drop-in of our own or an
   `SYSTEMD_UNIT_PATH`-visible rename, not `rm`. An unowned file is
   almost always a hand copy from an earlier install.
3. **Preserve it.** `mv` to `/tmp` or `~/unit-drift-<date>/`, never
   `rm`. If the shadowing copy turns out to have been the *newer* one,
   deleting it destroys the only evidence of what someone was trying to
   do. There is a standing rule against retro-rescues, not against
   keeping the file.
4. **Do not hand-fix the fragment in place.** Editing
   `~/.config/systemd/user/<unit>` makes the box correct and the repo
   still wrong, and the next promote reverts it. The repo's
   `scripts/systemd/` is canonical; if the override encodes something
   real (a memory cap that stopped an OOM), put it there and promote it.
5. **Reload and re-judge.**
   `systemctl --user daemon-reload && .venv/bin/python -m collector.health --drift-only`.
   Exit `0` is the only clean answer; `1` means what is left is
   script-repairable (`promote.sh --units-only`); `2` means the state
   you were fixing is still there.
6. **Restart nothing reflexively.** A `daemon-reload` re-reads unit
   files; it does not restart running units, so a corrected fragment
   applies to the *next* start. `hyxlab-stream` and `hyxlab-shadow` hold
   multi-day spans that a restart drops (`hyxlab-simui` holds a live
   paper session). If the corrected directive has to take effect now,
   restart deliberately and note the lost span, the way `promote.sh`
   does.

## What this does not cover

`UNREADABLE` — the loaded fragment could not be read at all — is also in
`UNREPAIRABLE`, and step 1 is still the first move, but it has never been
observed on this box and no procedure is written from an unobserved
state.

Neither state has fired in production yet (21/21 clean since 09-06); the
detection was built from a throwaway `hyxprobe-drift.service`, and so was
this. See `docs/wiki/data-pipeline.md` § "Unit drift" for the
measurements, and `collector/health.py` for the code.

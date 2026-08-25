---
paths:
  - "simulator/atlas.py"
  - "simulator/queuescore.py"
  - "simulator/shadow_*.py"
  - "simulator/divergence.py"
  - "collector/qa.py"
---

# Report Summary Fields

Every standing report's headline gets read months later by someone who
sees only the number. These rules exist because three summary fields
failed the same way in two passes (mistakes #24, #28, #31, #32, #33).

- **No boolean over a tri-state.** If a check can come back
  `rejected` / `accepted` / `never ran`, one bit cannot say which.
  Ship a `*_status` string enum alongside, and make the report carry a
  `*_verdict` whose counts **PARTITION** the population — the
  arithmetic is the guard. Precedents to copy: `atlas.quoted_status` +
  `quoted_verdict`, `atlas.flag_status` + `flag_verdict`,
  `queuescore.direction_*_status` + `direction_verdict`.
- **Name the denominator.** "N flagged of M buckets" is a lie whenever
  any of the M was never tested. Report `tested` and the share over
  `tested`, not over the population. The atlas headline was 141/395
  when it was 141/195.
- **A skipped/underpowered check is not a passed one.** Report the
  power ceiling AND the comparison against alpha — `min_sign_p` alone
  is a number the reader must interpret, and they will not. `qa.py`'s
  PASS / FAIL / WATCH / SKIP-UNVERIFIED vocabulary is the exemplar.
- **Never pool heterogeneous members into one max/min/all.** A max over
  seven series with different cadences reports the fastest, always;
  `all()` over an empty sequence reports valid. Report per-member, or
  state in the field name which member you are reporting.
- **Freeze the old field.** Add the decomposition; do not change what
  the existing headline field means. Archived reports must stay
  comparable, and the new status must be a strict REFINEMENT of the old
  boolean, asserted in a test.
- **Do not tune the threshold to reach a verdict.** `MIN_N` / alpha
  stay put. The point is to REPORT the silence, not abolish it.
- **Fix the class, the same pass.** A defect found by a lens must be
  swept against every comparable field before the pass ends. #32 was
  fixed as an instance; the identical defect sat in two other fields
  and surfaced only by accident, one pass later.

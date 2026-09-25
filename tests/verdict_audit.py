"""Which of QA's verdicts has anything ever REACHED?

WHY THIS EXISTS ON TOP OF test_qa_table_coverage, test_qa_staleness_coverage
AND test_qa_silent_guards (2026-09-25, mistakes #85). Those three derive, in
order, "every archive table is READ by QA", "every ingest stamp is asked its
AGE", and "no named check can stop being PRINTED". All three stop one step
short of the same question, and #85 is what walks through the gap:

    a check that is printed, on data it really reads, can still have a FAIL
    arm that nothing can reach.

`qa_poly_tail_absorbed` failed on `archived <= worst` while `archived >=
worst` held STRUCTURALLY (nothing in this repo deletes from `trades`), so
its strict half was unreachable and the only verdict it could ever produce
was equality -- a check testing the invariant instead of the claim. It had
tests, and they were green, because a test may fabricate by hand a state no
production writer can produce.

So this derives the fourth comparison, and it derives it from EXECUTION
rather than from source, because reachability is not a property of the text:

    every check name the suite ever saw PASS  ->  was it ever seen FAIL?

A name that can be green and was never once red is a verdict nothing in this
repo has ever demonstrated. That is weaker than "the check is wrong" and
stronger than nothing: it says the red half is unproven, which is exactly
the state both findings below were in on the day this shipped.

THE ASYMMETRY IS DELIBERATE. Green is not required of a red-only name: a
check can be exercised solely through its failure (that is what a mutant
test is), and demanding a matching pass would turn this into a coverage
quota. What cannot stand is the other direction -- a check that greens the
production run every morning and has never been shown capable of anything
else.

WHAT THIS DOES NOT CLAIM. That the red arm fires on the RIGHT input, or that
the state a test built to reach it is one production can reach. #85 would
have passed this audit. It is the first rung, not the ladder: it finds the
checks for which not even a hand-built red exists, and those must be closed
before the harder question ("can a WRITER produce this state?") is even
askable. It is satisfiable by cheating, too -- a test that calls
`qa.check("some production name", False)` directly counts here while
exercising none of the check's logic (`qa_prior_run`'s fixtures do exactly
that, which is why they are declared below rather than silently absorbed).
The audit is a floor under the mutant tests in tests/test_hyxlab_qa.py, not
a substitute for them.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

#: Names that may be seen green without ever being seen red. Each one has to
#: say WHY, and a declaration nothing emits is reported as stale -- a list
#: that only ever grows is a list that stops meaning anything.
#:
#: Every entry here today is a synthetic name a test hands to `qa.check` as
#: an argument, NOT a check `collector.qa` can ever emit on its own. They are
#: the fixtures of `qa_prior_run`, whose whole subject is what a PREVIOUS run
#: reported, so it can only be tested by naming checks that do not exist.
DECLARED: dict[str, str] = {
    "passed one": (
        "tests/test_qa_prior_run.py fixture: a synthetic name fed to "
        "`qa.check(name, True)` to populate `qa._ran`, which is the input "
        "`qa_prior_run` reads. No production check is called this."
    ),
    "stream fresh": (
        "tests/test_qa_prior_run.py fixture (`_ran_today`): a PRIOR run's "
        "recorded failure, replayed as green today. The production check is "
        "`stream fresh (trades < 5 min old)`, which is audited normally."
    ),
}

#: name -> the verdicts this session saw it reach. Written by the wrapper
#: `install` puts around `collector.qa.check`, read once at session end.
observed: dict[str, set[str]] = {}


def install() -> None:
    """Wrap `collector.qa.check` so every verdict it reaches is recorded.

    Wrapping the module attribute is what makes this total: qa.py's own call
    sites resolve `check` as a module global at call time, so a check added
    tomorrow is audited without being told, and so is one reached through a
    helper. Idempotent -- conftest is imported once per session, but a
    double-install would double-count and is cheap to refuse.
    """
    import collector.qa as qa

    if getattr(qa.check, "_verdict_audited", False):
        return
    original = qa.check

    def audited(name, ok, detail: str = "") -> None:
        observed.setdefault(str(name), set()).add("pass" if ok else "fail")
        return original(name, ok, detail)

    audited._verdict_audited = True  # type: ignore[attr-defined]
    qa.check = audited  # type: ignore[assignment]


def never_red(seen: dict[str, set[str]] | None = None) -> list[str]:
    """Names seen green, never seen red, and not declared."""
    seen = observed if seen is None else seen
    return sorted(
        name
        for name, verdicts in seen.items()
        if "pass" in verdicts and "fail" not in verdicts and name not in DECLARED
    )


def stale_declarations(seen: dict[str, set[str]] | None = None) -> list[str]:
    """Declared names the session never emitted at all, or that it DID drive
    red -- either way the declaration has stopped describing anything."""
    seen = observed if seen is None else seen
    return sorted(name for name in DECLARED if "fail" in seen.get(name, set()) or name not in seen)


def is_full_run(config) -> bool:
    """Did this session run the whole suite, unfiltered?

    The audit is a claim about what the SUITE proves, so a partial run cannot
    make it: `pytest tests/test_qa_divergence.py` sees a handful of names and
    would report every other check as never-red. Anything that selects,
    deselects or reorders disqualifies the session, and so does a run that
    stopped early (`-x` leaves the rest of the suite unexecuted, and that is
    an exit status the caller already sees).
    """
    opt = config.option
    if getattr(opt, "keyword", "") or getattr(opt, "markexpr", ""):
        return False
    if getattr(opt, "deselect", None) or getattr(opt, "last_failed", False):
        return False
    if getattr(opt, "failedfirst", False) or getattr(opt, "collectonly", False):
        return False
    args = [str(a).split("::")[0] for a in (config.args or [])]
    if not args:
        return False
    for arg in args:
        try:
            resolved = Path(arg).resolve()
        except OSError:
            return False
        if resolved != TESTS and resolved != REPO:
            return False
    return True


def report(session, exitstatus) -> None:
    """Fail the SESSION when a reachable-looking verdict was never reached.

    Emitted from `pytest_sessionfinish` rather than from a test because the
    evidence is not complete until the last test has run, and a test cannot
    see the ones scheduled after it. The exit status is set by hand for the
    same reason: there is no test item left to fail.
    """
    if exitstatus != 0 or session.testsfailed or not is_full_run(session.config):
        return
    unproven, stale = never_red(), stale_declarations()
    if not unproven and not stale:
        return
    print("\n" + "=" * 72)
    print("VERDICT REACHABILITY AUDIT FAILED (tests/verdict_audit.py)")
    for name in unproven:
        print(
            f"  NEVER RED  {name!r} — the whole suite drove this check green and "
            "never once drove it red, so its FAIL arm is unproven. Write the "
            "test that reds it, or declare why it cannot be red."
        )
    for name in stale:
        print(
            f"  STALE DECL {name!r} — declared as green-only in DECLARED, but this "
            "session did not see it that way. Remove the declaration."
        )
    print("=" * 72, flush=True)
    session.exitstatus = 1

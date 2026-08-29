"""Offline checks for the pipeline itself. No API keys, no network.

The model layer is stubbed, so this exercises the wiring: explicit pipelines,
per-run memory isolation, bounded context, incremental events, the acceptance
suite (including the vacuity guard and the anti-cheating rule), and the
harness-driven revision loop.

    python3 test_pipeline.py
"""

import glob
import io
import json
import os
import shutil
import sys
import tempfile

import agents_core
import harness


PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print("%s %s%s" % ("ok  " if condition else "FAIL", name,
                       "" if condition else "  <- " + str(detail)))


# The plan now carries a third section: the acceptance suite APPROVED is
# measured against. It only exercises `add`, because the same suite is run
# against every step's program.
TESTS = (
    "from solution import add\n\n"
    "assert add(2, 2) == 4\n"
    "assert add(-1, 1) == 0\n"
)

PLAN = (
    "SPEC: Build a tiny arithmetic helper exposing add(a, b) -> int.\n"
    "STEPS:\n"
    "1. Write an add function with asserts.\n"
    "2. Write a multiply function with asserts.\n"
    "TESTS:\n```python\n" + TESTS + "```\n"
)

PLAN_NO_TESTS = (
    "SPEC: Build a tiny arithmetic helper exposing add(a, b) -> int.\n"
    "STEPS:\n"
    "1. Write an add function with asserts.\n"
    "2. Write a multiply function with asserts.\n"
)

# Passes the suite.
GOOD_CODE = (
    "Here is the program.\n\n```python\n"
    "def add(a, b):\n    return a + b\n\n"
    "assert add(2, 2) == 4\nprint('add ok')\n```\n"
)

# Blows up while being imported: its own module-level assert fails.
BAD_CODE = (
    "```python\n"
    "def add(a, b):\n    return a - b\n\n"
    "assert add(2, 2) == 4\nprint('add ok')\n```\n"
)

# Imports cleanly, runs cleanly, and is simply wrong. Under Phase 1 this would
# have exited 0 and been APPROVED.
WRONG_CODE = (
    "```python\n"
    "def add(a, b):\n    return a - b\n\n"
    "print('module loaded')\n```\n"
)

# A suite that asserts nothing real -- it passes even against a stub.
VACUOUS_TESTS = (
    "```python\n"
    "from solution import add\n\n"
    "assert callable(add)\n"
    "assert add is not None\n"
    "```\n"
)


class Stub:
    """Stands in for call_model and records every call."""

    def __init__(self, script):
        self.script = script          # role -> list of replies (or a callable)
        self.calls = []               # (provider, role_guess, system, user)

    def __call__(self, provider, api_key, system, user):
        role = "planner"
        if system.startswith("You are the Executor"):
            role = "executor"
        elif system.startswith("You are the Test Writer"):
            role = "test_writer"
        self.calls.append((provider, role, system, user))
        replies = self.script.get(role)
        if callable(replies):
            return replies(len([c for c in self.calls if c[1] == role]) - 1, user)
        if isinstance(replies, list):
            index = len([c for c in self.calls if c[1] == role]) - 1
            return replies[min(index, len(replies) - 1)]
        return replies or ""


def run_with(stub, mode=3, prompt="make an arithmetic helper", runs_dir=None,
             user_tests=None, plan=None, tests=None):
    original = agents_core.call_model
    agents_core.call_model = stub
    events = []
    try:
        memory_original = agents_core.Memory

        class ScopedMemory(memory_original):
            def __init__(self, *args, **kwargs):
                kwargs["dirpath"] = runs_dir or kwargs.get("dirpath", "runs")
                memory_original.__init__(self, *args, **kwargs)

        agents_core.Memory = ScopedMemory
        try:
            run = agents_core.run_workspace(
                prompt, {"gemini": "k1", "groq": "k2"}, mode,
                lambda role, content: events.append((role, content)),
                user_tests=user_tests, plan=plan, tests=tests,
            )
        finally:
            agents_core.Memory = memory_original
    finally:
        agents_core.call_model = original
    return run, events


# ------------------------------------------------------------ explicit pipeline

def test_pipeline_is_explicit():
    check("no dead roles slicing", not hasattr(agents_core, "roles"))
    check("mode 2 has no harness", "harness" not in agents_core.pipeline_for(2),
          agents_core.pipeline_for(2))
    check("mode 3 has a harness", "harness" in agents_core.pipeline_for(3),
          agents_core.pipeline_for(3))
    check("mode 4 is no longer offered", 4 not in agents_core.PIPELINES,
          sorted(agents_core.PIPELINES))
    check("mode 4 maps to mode 3", agents_core.resolve_mode(4) == 3,
          agents_core.resolve_mode(4))
    check("retiring mode 4 is explained", "retired" in
          agents_core.retired_mode_note(4).lower(),
          agents_core.retired_mode_note(4))
    check("live modes have no retirement note",
          agents_core.retired_mode_note(3) == "",
          agents_core.retired_mode_note(3))
    check("default mode is the harness pipeline",
          agents_core.DEFAULT_MODE == 3, agents_core.DEFAULT_MODE)
    check("unknown mode falls back to default",
          agents_core.pipeline_for(99) == agents_core.pipeline_for(agents_core.DEFAULT_MODE))
    check("string mode is coerced", agents_core.resolve_mode("3") == 3)
    check("critic has no provider", "critic" not in agents_core.ROLE_PROVIDER,
          agents_core.ROLE_PROVIDER)
    check("mode 2 needs both keys anyway",
          set(agents_core.required_providers(2)) == {"gemini", "groq"},
          agents_core.required_providers(2))


def test_mode_2_skips_execution():
    stub = Stub({"planner": PLAN, "executor": BAD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=2, runs_dir=workdir)
        roles = [role for role, _ in events]
        check("mode 2 emits no harness entries", "harness" not in roles, set(roles))
        check("mode 2 marks steps unverified",
              all(step.verdict == "UNVERIFIED" for step in run.steps),
              [s.verdict for s in run.steps])
        check("mode 2 verified count is 0", run.verified_count == 0, run.verified_count)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_retired_mode_4_runs_mode_3():
    """Mode 4 used to append an Orchestrator: a model call per step that
    narrated results the harness had already established and could not act on
    them. Asking for it now runs mode 3 and says so, rather than silently
    accepting an unknown mode."""
    stub = Stub({"planner": PLAN, "executor": GOOD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=4, runs_dir=workdir)
        roles = [role for role, _ in events]
        check("mode 4 runs as mode 3", run.mode == 3, run.mode)
        check("no orchestrator entries in the feed",
              "orchestrator" not in roles, set(roles))
        check("no orchestrator model call is made",
              not any(role == "orchestrator" for _, role, _, _ in stub.calls),
              [c[1] for c in stub.calls])
        check("no orchestrator prompt survives",
              "orchestrator" not in agents_core.PROMPTS,
              sorted(agents_core.PROMPTS))
        check("no orchestrator provider survives",
              "orchestrator" not in agents_core.ROLE_PROVIDER,
              sorted(agents_core.ROLE_PROVIDER))
        check("mode 4 still verifies its steps",
              run.verified_count == len(run.steps),
              (run.verified_count, len(run.steps)))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ------------------------------------------------------------ harness verdicts

def test_working_code_is_approved():
    stub = Stub({"planner": PLAN, "executor": GOOD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        check("all steps approved",
              all(step.verdict == harness.VERDICT_APPROVED for step in run.steps),
              [s.verdict for s in run.steps])
        check("exit codes are 0", all(s.exit_code == 0 for s in run.steps),
              [s.exit_code for s in run.steps])
        check("stdout captured from the real run",
              all("add ok" in s.stdout for s in run.steps),
              [s.stdout for s in run.steps])
        check("no revisions needed", all(s.rounds == 0 for s in run.steps),
              [s.rounds for s in run.steps])
        check("deliverable reports verification",
              "Verified against the acceptance suite: 2/2" in run.deliverable,
              run.deliverable[:300])
        check("steps were graded against a suite",
              all(s.tested for s in run.steps), [s.tested for s in run.steps])
        check("suite ran as the entry point",
              all("test_solution.py" in c for _, c in events if _ == "harness"),
              [c[:80] for r, c in events if r == "harness"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_broken_code_is_never_approved():
    stub = Stub({"planner": PLAN, "executor": BAD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        check("broken code is not approved",
              all(step.verdict != harness.VERDICT_APPROVED for step in run.steps),
              [s.verdict for s in run.steps])
        check("nonzero exit recorded",
              all(s.exit_code not in (0, None) for s in run.steps),
              [s.exit_code for s in run.steps])
        check("real AssertionError captured",
              all("AssertionError" in s.stderr for s in run.steps),
              [s.stderr[:120] for s in run.steps])
        check("revision loop is bounded",
              all(s.rounds == agents_core.MAX_REVISION_ROUNDS for s in run.steps),
              [s.rounds for s in run.steps])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_traceback_is_fed_back_to_executor():
    """First attempt fails; the retry prompt must contain the real traceback."""
    def executor(index, user):
        return GOOD_CODE if index > 0 else BAD_CODE

    stub = Stub({"planner": PLAN, "executor": executor})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        retry_prompts = [user for _, role, _, user in stub.calls
                         if role == "executor" and "executed in a sandbox" in user]
        check("executor was re-prompted after failure", retry_prompts, len(retry_prompts))
        check("retry prompt carries the traceback",
              retry_prompts and "AssertionError" in retry_prompts[0],
              retry_prompts[0][:300] if retry_prompts else "")
        check("retry prompt carries the exit code",
              retry_prompts and "exit code:" in retry_prompts[0],
              "")
        check("retry prompt is not an opinion",
              retry_prompts and "solution.py" in retry_prompts[0],
              retry_prompts[0][:300] if retry_prompts else "")
        check("step recovers to APPROVED",
              all(s.verdict == harness.VERDICT_APPROVED for s in run.steps),
              [s.verdict for s in run.steps])
        # The stub only returns broken code on its first call, so step 1 needs
        # exactly one revision and step 2 succeeds first time.
        check("revision rounds recorded per step",
              [s.rounds for s in run.steps] == [1, 0], [s.rounds for s in run.steps])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_prose_output_is_not_approved():
    stub = Stub({"planner": PLAN, "executor": "I would use a dictionary for this."})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, _ = run_with(stub, mode=3, runs_dir=workdir)
        check("prose is never approved",
              all(s.verdict != harness.VERDICT_APPROVED for s in run.steps),
              [s.verdict for s in run.steps])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ------------------------------------------------------------ acceptance suite

def test_tests_are_parsed_and_emitted():
    check("tests are pulled from the TESTS section",
          agents_core.extract_tests(PLAN) == TESTS,
          repr(agents_core.extract_tests(PLAN)))
    check("no TESTS section yields None",
          agents_core.extract_tests(PLAN_NO_TESTS) is None,
          agents_core.extract_tests(PLAN_NO_TESTS))

    # Numbered lines inside the TESTS block must not be mistaken for steps.
    tricky = (
        "SPEC: s\nSTEPS:\n1. only step\n"
        "TESTS:\n```python\nfrom solution import add\n"
        "# 2. not a step\nassert add(1, 1) == 2\n```\n"
    )
    check("step parsing is scoped to the STEPS section",
          agents_core.extract_steps(tricky) == ["only step"],
          agents_core.extract_steps(tricky))
    check("spec parsing stops at STEPS",
          agents_core.extract_spec(PLAN) ==
          "Build a tiny arithmetic helper exposing add(a, b) -> int.",
          repr(agents_core.extract_spec(PLAN)))

    stub = Stub({"planner": PLAN, "executor": GOOD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        tests_entries = [c for role, c in events if role == "tests"]
        check("the suite gets its own feed entry", len(tests_entries) == 1,
              len(tests_entries))
        check("the feed entry shows the actual asserts",
              tests_entries and "assert add(2, 2) == 4" in tests_entries[0],
              tests_entries[0][:200] if tests_entries else "")
        check("the feed entry names the interface under test",
              tests_entries and "`add`" in tests_entries[0], "")
        check("run records the generated suite",
              run.tests_status == agents_core.TESTS_GENERATED, run.tests_status)
        check("run trusts the audited suite", run.tests_trusted, run.tests_status)
        with open(run.memory_path) as handle:
            saved = json.load(handle)
        check("suite is persisted in the run log",
              saved["tests"].strip() == TESTS.strip(), repr(saved.get("tests")))
        check("deliverable includes the suite",
              "## Acceptance suite" in run.deliverable, run.deliverable[:400])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_wrong_answer_that_runs_is_revised():
    """The Phase 1 hole: code that exits 0 but computes the wrong answer."""
    plain = harness.run_python_sandboxed(
        "def add(a, b):\n    return a - b\n\nprint('module loaded')\n")
    check("wrong-but-running code exits 0 with no suite", plain.exit_code == 0,
          (plain.exit_code, plain.stderr[:120]))

    stub = Stub({"planner": PLAN, "executor": WRONG_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        check("the suite catches the wrong answer",
              all(s.verdict == harness.VERDICT_REVISE for s in run.steps),
              [s.verdict for s in run.steps])
        check("failure is classed as an assertion, not a crash",
              all(s.failure_kind == harness.FAIL_ASSERTION for s in run.steps),
              [s.failure_kind for s in run.steps])
        check("the failing assertion is named",
              all("add(2, 2) == 4" in s.failed_assertion for s in run.steps),
              [s.failed_assertion for s in run.steps])
        reports = [c for role, c in events if role == "harness"]
        check("the report cites the failing assertion with its line",
              reports and "Failed assertion (test_solution.py line 3)" in reports[0],
              reports[0][:400] if reports else "")

        retries = [user for _, role, _, user in stub.calls
                   if role == "executor" and "executed in a sandbox" in user]
        check("the retry prompt distinguishes wrong-answer from crash",
              retries and "simply computing the wrong answer" in retries[0],
              retries[0][:300] if retries else "")
        check("the retry prompt forbids weakening the test",
              retries and "Do not change the test" in retries[0], "")
        check("the retry prompt carries the failing assert",
              retries and "assert add(2, 2) == 4" in retries[0], "")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_import_failure_gets_different_guidance():
    stub = Stub({"planner": PLAN, "executor": BAD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, _ = run_with(stub, mode=3, runs_dir=workdir)
        check("a module that raises on import is an import failure",
              all(s.failure_kind == harness.FAIL_IMPORT for s in run.steps),
              [s.failure_kind for s in run.steps])
        check("import failure names no acceptance assertion",
              all(not s.failed_assertion for s in run.steps),
              [s.failed_assertion for s in run.steps])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_vacuous_tests_force_unverified():
    """A rubber-stamp suite must never produce APPROVED."""
    stub = Stub({
        "planner": PLAN_NO_TESTS + "TESTS:\n" + VACUOUS_TESTS,
        # The one regeneration attempt is just as vacuous.
        "test_writer": VACUOUS_TESTS,
        "executor": GOOD_CODE,
    })
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        check("vacuous suite is rejected",
              run.tests_status == agents_core.TESTS_VACUOUS, run.tests_status)
        check("vacuous suite is not trusted", not run.tests_trusted)
        check("regeneration was attempted exactly once",
              len([c for c in stub.calls if c[1] == "test_writer"]) == 1,
              [c[1] for c in stub.calls])
        check("working code is UNVERIFIED, not APPROVED",
              all(s.verdict == harness.VERDICT_UNVERIFIED for s in run.steps),
              [s.verdict for s in run.steps])
        check("nothing counts as verified", run.verified_count == 0,
              run.verified_count)
        systems = " ".join(c for role, c in events if role == "system")
        check("the feed says why it is unverified",
              "trustworthy acceptance suite" in systems, systems[-300:])
        check("the deliverable says so too",
              "No trustworthy acceptance suite" in run.deliverable,
              run.deliverable[:400])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_regeneration_can_rescue_a_vacuous_suite():
    stub = Stub({
        "planner": PLAN_NO_TESTS + "TESTS:\n" + VACUOUS_TESTS,
        "test_writer": "```python\n" + TESTS + "```\n",
        "executor": GOOD_CODE,
    })
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, _ = run_with(stub, mode=3, runs_dir=workdir)
        check("a good regenerated suite is used",
              run.tests_status == agents_core.TESTS_REGENERATED, run.tests_status)
        check("steps can be approved again",
              all(s.verdict == harness.VERDICT_APPROVED for s in run.steps),
              [s.verdict for s in run.steps])
        rejected = [user for _, role, _, user in stub.calls if role == "test_writer"]
        check("the test writer is told why the last suite was rejected",
              rejected and "REJECTED because" in rejected[0],
              rejected[0][:200] if rejected else "")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_missing_tests_cap_the_verdict():
    stub = Stub({"planner": PLAN_NO_TESTS, "test_writer": "no code here",
                 "executor": GOOD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, _ = run_with(stub, mode=3, runs_dir=workdir)
        check("an unusable suite is recorded as such",
              run.tests_status == agents_core.TESTS_UNUSABLE, run.tests_status)
        check("clean-exit code is still not APPROVED",
              all(s.verdict == harness.VERDICT_UNVERIFIED for s in run.steps),
              [s.verdict for s in run.steps])
        check("the step explains the cap",
              all("no trustworthy acceptance suite" in s.note for s in run.steps),
              [s.note for s in run.steps])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_user_tests_win_over_generated():
    """The user's suite is authoritative even when the Planner supplied one."""
    # Deliberately contradicts GOOD_CODE, so if this suite runs the step fails.
    mine = "from solution import add\n\nassert add(2, 2) == 5\n"
    stub = Stub({"planner": PLAN, "executor": GOOD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, _ = run_with(stub, mode=3, runs_dir=workdir, user_tests=mine)
        check("user suite is the one recorded",
              run.tests_status == agents_core.TESTS_USER, run.tests_status)
        check("user suite replaces the generated one",
              "add(2, 2) == 5" in run.tests and "add(-1, 1)" not in run.tests,
              repr(run.tests))
        check("user suite is what actually ran",
              all(s.failure_kind == harness.FAIL_ASSERTION for s in run.steps),
              [s.failure_kind for s in run.steps])
        check("no test writer call when the user supplied tests",
              not [c for c in stub.calls if c[1] == "test_writer"],
              [c[1] for c in stub.calls])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_executor_cannot_weaken_the_tests():
    """Anti-cheating: the stored suite is re-run every round, never the
    Executor's version, and a test file in its output is discarded."""
    cheating = (
        "```python\n"
        "def add(a, b):\n    return a - b\n\nprint('loaded')\n```\n\n"
        "And here are the tests:\n\n```python\n"
        "from solution import add\n\nassert add(2, 2) == 0\n```\n"
    )
    stub = Stub({"planner": PLAN, "executor": cheating})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        check("the Executor's own suite does not earn APPROVED",
              all(s.verdict == harness.VERDICT_REVISE for s in run.steps),
              [s.verdict for s in run.steps])
        check("the stored suite is what failed it",
              all("add(2, 2) == 4" in s.failed_assertion for s in run.steps),
              [s.failed_assertion for s in run.steps])
        check("the solution block was picked, not the test block",
              all("return a - b" in s.code and "import add" not in s.code
                  for s in run.steps),
              [s.code for s in run.steps])
        reports = [c for role, c in events if role == "harness"]
        check("the report notes the discarded test file",
              reports and "was ignored" in reports[0],
              reports[0][:400] if reports else "")
        check("the stored suite survives every round",
              all(s.rounds == agents_core.MAX_REVISION_ROUNDS for s in run.steps)
              and run.tests.strip() == TESTS.strip(), repr(run.tests))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------- escalation ladder

def test_escalation_is_deterministic():
    """The policy that replaced the Orchestrator. It must be shaped like the
    round budget and must not consult a model about what to try next."""
    check("one rung per revision round",
          len(agents_core.ESCALATION) == agents_core.MAX_REVISION_ROUNDS,
          agents_core.ESCALATION)
    check("every rung is explained",
          all(rung in agents_core.ESCALATION_WHY
              for rung in agents_core.ESCALATION),
          sorted(agents_core.ESCALATION_WHY))
    check("the ladder changes strategy, not just retries",
          len(set(agents_core.ESCALATION)) > 1, agents_core.ESCALATION)
    check("escalation ends with a from-scratch attempt",
          agents_core.ESCALATION[-1] == "fresh", agents_core.ESCALATION)

    # The choice is a pure function of (rung, keys) -- no provider is called to
    # decide it, so it works with no keys at all and cannot fail mid-run.
    check("alternate rung picks a non-primary provider",
          agents_core.alternate_provider("executor", {"gemini": "k", "groq": "k"})
          != agents_core.ROLE_PROVIDER["executor"],
          agents_core.alternate_provider("executor", {"gemini": "k", "groq": "k"}))
    check("alternate rung is unavailable with one key",
          agents_core.alternate_provider("executor", {"groq": "k"}) is None,
          agents_core.alternate_provider("executor", {"groq": "k"}))


def test_escalation_ladder_is_climbed_in_order():
    stub = Stub({"planner": PLAN, "executor": BAD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        systems = [c for role, c in events if role == "system"]
        rungs = [rung for rung in agents_core.ESCALATION
                 if any("escalating to `%s`" % rung in c for c in systems)]
        check("every rung is used before giving up",
              rungs == list(agents_core.ESCALATION), rungs)
        check("the feed says why each escalation happened",
              all(any(agents_core.ESCALATION_WHY[r] in c for c in systems)
                  for r in agents_core.ESCALATION),
              systems)

        # Executor calls: 1 initial + one per rung, per step.
        executor_calls = [c for c in stub.calls if c[1] == "executor"]
        check("no extra model call is spent on deciding",
              len(executor_calls) ==
              len(run.steps) * (agents_core.MAX_REVISION_ROUNDS + 1),
              len(executor_calls))
        check("only planner and executor are ever called",
              {c[1] for c in stub.calls} == {"planner", "executor"},
              {c[1] for c in stub.calls})

        # The alternate rung must actually land on a different provider.
        primary = agents_core.ROLE_PROVIDER["executor"]
        check("some executor call goes to the other provider",
              any(c[0] != primary for c in executor_calls),
              [c[0] for c in executor_calls])

        prompts = [c[3] for c in executor_calls]
        fresh = [p for p in prompts if "start over" in p]
        check("the fresh rung asks for a new implementation", fresh, len(fresh))
        check("the fresh rung withholds the failed traceback",
              fresh and "executed in a sandbox" not in fresh[0],
              fresh[0][:200] if fresh else "")
        check("the fresh rung still restates the task",
              fresh and "Original task:" in fresh[0], "")
        check("the fresh rung counts the failures accurately",
              fresh and "%d previous attempt"
              % (agents_core.ESCALATION.index("fresh") + 1) in fresh[0],
              fresh[0][:80] if fresh else "")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_giving_up_is_explicit():
    """Exhausting the ladder must be stated, with what was tried."""
    stub = Stub({"planner": PLAN, "executor": BAD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, mode=3, runs_dir=workdir)
        check("exhaustion is recorded on the step",
              all(s.escalation == "exhausted" for s in run.steps),
              [s.escalation for s in run.steps])
        check("the note says how many rounds were spent",
              all("%d escalation round(s)" % agents_core.MAX_REVISION_ROUNDS
                  in s.note for s in run.steps),
              [s.note for s in run.steps])
        check("the note names what was tried",
              all(all(r in s.note for r in agents_core.ESCALATION)
                  for s in run.steps),
              [s.note for s in run.steps])
        # REVISE, not UNVERIFIED: the harness ran the code and it demonstrably
        # failed. UNVERIFIED means nothing could be established, which would
        # discard the evidence rather than report it.
        check("a demonstrated failure stays REVISE",
              all(s.verdict == harness.VERDICT_REVISE for s in run.steps),
              [s.verdict for s in run.steps])
        check("exhaustion is announced in the feed",
              any("escalation round(s)" in c
                  for role, c in events if role == "system"),
              [c for role, c in events if role == "system"][-2:])
        with open(run.memory_path) as handle:
            saved = json.load(handle)
        check("exhaustion is persisted",
              all(s["escalation"] == "exhausted" for s in saved["steps"]),
              [s.get("escalation") for s in saved["steps"]])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_escalation_degrades_with_one_key():
    """With a single provider the alternate rung cannot switch models. It must
    still repair rather than crash or silently claim an escalation."""
    stub = Stub({"planner": PLAN, "executor": BAD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    original = agents_core.call_model
    agents_core.call_model = stub
    events = []
    try:
        memory_original = agents_core.Memory

        class ScopedMemory(memory_original):
            def __init__(self, *args, **kwargs):
                kwargs["dirpath"] = workdir
                memory_original.__init__(self, *args, **kwargs)

        agents_core.Memory = ScopedMemory
        try:
            run = agents_core.run_workspace(
                "make an arithmetic helper", {"groq": "k2"}, 3,
                lambda role, content: events.append((role, content)))
        finally:
            agents_core.Memory = memory_original
    finally:
        agents_core.call_model = original
        shutil.rmtree(workdir, ignore_errors=True)

    check("a one-key run still completes", run.steps, run.steps)
    check("every call went to the only provider",
          {c[0] for c in stub.calls} == {"groq"}, {c[0] for c in stub.calls})
    check("the ladder still ran to exhaustion",
          all(s.escalation == "exhausted" for s in run.steps),
          [s.escalation for s in run.steps])
    check("rounds are still bounded",
          all(s.rounds == agents_core.MAX_REVISION_ROUNDS for s in run.steps),
          [s.rounds for s in run.steps])


def test_executor_prompt_states_the_sandbox_contract():
    """Sprint 2b: the Executor is told the rules it is graded against, and the
    text comes from the harness so the two cannot drift apart."""
    prompt = agents_core.PROMPTS["executor"]
    check("the executor prompt embeds the harness rules",
          harness._SANDBOX_RULES in prompt, prompt[-400:])
    for fragment in ("standard library only", "No network", "No subprocesses",
                     "No stdin", "working directory"):
        check("executor prompt states: %s" % fragment, fragment in prompt,
              prompt[-600:])
    check("executor prompt states the running Python version",
          "Python %d.%d" % sys.version_info[:2] in prompt, prompt[-600:])
    check("executor prompt states the wall-clock limit",
          str(harness.EXEC_TIMEOUT_SECONDS) in prompt, prompt[-600:])
    check("executor prompt states the output cap",
          str(harness.MAX_CAPTURE_BYTES) in prompt, prompt[-600:])
    check("no stale duplicate of the timeout rule",
          prompt.count("finish within") == 1, prompt.count("finish within"))


# ------------------------------------------------------------ memory isolation


def test_memory_does_not_bleed():
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        stub = Stub({"planner": PLAN, "executor": GOOD_CODE})
        first, _ = run_with(stub, mode=3, runs_dir=workdir)
        second, _ = run_with(stub, mode=3, runs_dir=workdir)

        check("each run gets its own id", first.run_id != second.run_id,
              (first.run_id, second.run_id))
        check("each run gets its own file", first.memory_path != second.memory_path)

        with open(first.memory_path) as handle:
            first_data = json.load(handle)
        with open(second.memory_path) as handle:
            second_data = json.load(handle)

        check("second run's log does not contain the first",
              len(second_data["log"]) == len(first_data["log"]),
              (len(first_data["log"]), len(second_data["log"])))
        check("first log starts with its own run id",
              first.run_id in first_data["log"][0]["content"],
              first_data["log"][0]["content"])
        check("second log starts with its own run id",
              second.run_id in second_data["log"][0]["content"],
              second_data["log"][0]["content"])
        check("run file records the prompt and pipeline",
              second_data["prompt"] and second_data["pipeline"],
              second_data.get("pipeline"))
        check("two run files on disk",
              len(glob.glob(os.path.join(workdir, "*.json"))) == 2,
              glob.glob(os.path.join(workdir, "*.json")))
        check("no leftover temp files",
              not glob.glob(os.path.join(workdir, "*.tmp")))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_memory_never_reads_prior_state():
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        planted = os.path.join(workdir, "planted.json")
        with open(planted, "w") as handle:
            json.dump({"log": [{"role": "system", "content": "STALE"}]}, handle)
        mem = agents_core.Memory(prompt="fresh", mode=3, dirpath=workdir)
        check("fresh memory starts empty", mem.data["log"] == [], mem.data["log"])
        check("fresh memory has no stale deliverable", mem.data["deliverable"] == "")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ------------------------------------------------------------ bounded context

def test_context_is_bounded():
    huge = "X" * 200000
    completed = [(n, "step %d" % n, huge) for n in range(1, 9)]
    context = agents_core.build_context(huge, completed)
    check("context respects the ceiling",
          len(context) <= agents_core.MAX_CONTEXT_CHARS, len(context))
    check("elision is visible", "elided" in context)
    check("older steps degrade to titles", "step 1" in context and
          context.count("Completed step") <= agents_core.CONTEXT_RECENT_STEPS,
          context.count("Completed step"))


def test_context_stays_bounded_across_many_steps():
    plan = "SPEC: many steps\nSTEPS:\n" + "\n".join(
        "%d. step number %d" % (n, n) for n in range(1, 9))
    big_output = "```python\nprint('%s')\n```" % ("y" * 40000)

    stub = Stub({"planner": plan, "executor": big_output})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, _ = run_with(stub, mode=3, runs_dir=workdir)
        executor_prompts = [user for _, role, _, user in stub.calls if role == "executor"]
        worst = max(len(p) for p in executor_prompts)
        check("no executor prompt grows unbounded",
              worst <= agents_core.MAX_CONTEXT_CHARS + 4000, worst)
        check("prompt size does not grow monotonically with steps",
              len(executor_prompts[-1]) < worst * 1.5 + 1,
              [len(p) for p in executor_prompts])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_step_cap():
    plan = "SPEC: x\nSTEPS:\n" + "\n".join("%d. s%d" % (n, n) for n in range(1, 60))
    check("planner steps are capped",
          len(agents_core.extract_steps(plan)) == agents_core.MAX_STEPS,
          len(agents_core.extract_steps(plan)))


# ------------------------------------------------------------ incremental feed

def test_events_arrive_incrementally():
    """on_event must fire during the run, not in one batch at the end."""
    seen = []
    stub = Stub({"planner": PLAN, "executor": GOOD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")

    original = agents_core.call_model
    events_at_each_call = []

    def tracking_stub(provider, api_key, system, user):
        events_at_each_call.append(len(seen))
        return stub(provider, api_key, system, user)

    agents_core.call_model = tracking_stub
    memory_original = agents_core.Memory

    class ScopedMemory(memory_original):
        def __init__(self, *args, **kwargs):
            kwargs["dirpath"] = workdir
            memory_original.__init__(self, *args, **kwargs)

    agents_core.Memory = ScopedMemory
    try:
        agents_core.run_workspace("x", {"gemini": "k", "groq": "k"}, 3,
                                  lambda role, content: seen.append(role))
    finally:
        agents_core.call_model = original
        agents_core.Memory = memory_original
        shutil.rmtree(workdir, ignore_errors=True)

    check("events existed before the run finished",
          events_at_each_call and max(events_at_each_call) > 0, events_at_each_call)
    check("first event precedes the planner call",
          events_at_each_call and events_at_each_call[0] >= 1, events_at_each_call)
    # The acceptance suite is emitted as its own entry, between the plan and the
    # first attempt at satisfying it.
    check("feed order is plausible",
          seen[:4] == ["system", "planner", "tests", "executor"], seen[:5])
    check("harness entry follows each executor entry",
          seen.count("harness") >= seen.count("executor") - 1, seen)


# ------------------------------------------------------------ provider fallback

def test_provider_fallback():
    calls = []

    def flaky(provider, api_key, system, user):
        calls.append(provider)
        if provider == "gemini":
            raise agents_core.ProviderError("gemini down")
        return PLAN

    original = agents_core.call_model
    agents_core.call_model = flaky
    try:
        text, used = agents_core.call_role(
            "planner", {"gemini": "a", "groq": "b"}, "sys", "user")
        check("falls back to the other provider", used == "groq", used)
        check("fallback returned content", text == PLAN)
    finally:
        agents_core.call_model = original

    original = agents_core.call_model
    agents_core.call_model = lambda *a, **k: (_ for _ in ()).throw(
        agents_core.ProviderError("all down"))
    try:
        try:
            agents_core.call_role("planner", {"gemini": "a", "groq": "b"}, "s", "u")
            check("raises when every provider fails", False, "no exception")
        except agents_core.ProviderError:
            check("raises when every provider fails", True)
    finally:
        agents_core.call_model = original

    try:
        agents_core.call_role("planner", {}, "s", "u")
        check("raises when no key is present", False, "no exception")
    except agents_core.ProviderError:
        check("raises when no key is present", True)


# ------------------------------------------------- measurement mode (sprint 4/1)

def _capture_call_role(order_seen):
    """A call_model stand-in that records providers and fails gemini."""

    def fake(provider, api_key, system, user):
        order_seen.append(provider)
        if provider == "gemini":
            raise agents_core.ProviderError(
                "gemini failed: 404 model 'gemini-does-not-exist' not found")
        return "```python\nprint('ok')\n```\n"

    return fake


def _with_measurement(enabled, fn):
    original = agents_core.MEASUREMENT_MODE
    agents_core.set_measurement_mode(enabled)
    try:
        return fn()
    finally:
        agents_core.set_measurement_mode(original)


def test_measurement_mode_disables_silent_failover():
    keys = {"gemini": "gkey-1234567890", "groq": "qkey-1234567890"}
    original = agents_core.call_model
    try:
        # Off: a dead primary is papered over by the next provider. This is the
        # behaviour the UI wants and the behaviour a measurement cannot have.
        seen = []
        agents_core.call_model = _capture_call_role(seen)
        text, used = _with_measurement(
            False, lambda: agents_core.call_role("planner", keys, "sys", "usr"))
        check("off: a dead primary falls through to the other provider",
              used == "groq" and seen == ["gemini", "groq"], seen)
        check("off: the fallback still returns content", "print" in text)

        # On: the same dead primary raises. No second provider is even tried.
        seen = []
        agents_core.call_model = _capture_call_role(seen)
        try:
            _with_measurement(
                True, lambda: agents_core.call_role("planner", keys, "s", "u"))
            check("measurement mode with a dead primary raises", False,
                  "it returned instead")
        except agents_core.ProviderError as exc:
            check("measurement mode with a dead primary raises", True)
            check("only the requested provider was attempted",
                  seen == ["gemini"], seen)
            check("the error says failover is disabled",
                  "failover is disabled" in str(exc), exc)
    finally:
        agents_core.call_model = original


def test_measurement_mode_honours_prefer():
    """`prefer` is the ladder's deliberate, logged switch. It survives.

    Only the *silent* substitution dies: an escalation the run asked for and
    recorded is part of arm B's definition, while a swap nobody asked for turns
    the arm into a provider mixture whose composition tracks rate-limit
    pressure.
    """
    keys = {"gemini": "gkey-1234567890", "groq": "qkey-1234567890"}
    original = agents_core.call_model
    try:
        seen = []
        agents_core.call_model = _capture_call_role(seen)
        result = _with_measurement(
            True, lambda: agents_core.call_role(
                "planner", keys, "sys", "usr", prefer="groq"))
        check("measurement mode with prefer uses prefer", result[1] == "groq")
        check("prefer means prefer and nothing else", seen == ["groq"], seen)
        check("the record calls it requested, not substituted",
              result.record["requested"] == "groq"
              and result.record["used"] == "groq", result.record)

        # An unconfigured prefer must not become a licence to shop around: it
        # falls back to the role's own provider, not to whatever answers.
        seen = []
        agents_core.call_model = _capture_call_role(seen)
        try:
            _with_measurement(True, lambda: agents_core.call_role(
                "planner", keys, "s", "u", prefer="nonexistent"))
        except agents_core.ProviderError:
            pass
        check("an unknown prefer falls back to the role's provider only",
              seen == ["gemini"], seen)
    finally:
        agents_core.call_model = original


def test_call_record_is_complete_and_key_free():
    key = "gkey-supersecret-0987654321"
    keys = {"gemini": key, "groq": "qkey-1234567890"}
    original = agents_core.call_model
    try:
        agents_core.call_model = lambda p, k, s, u: "```python\npass\n```\n"
        agents_core.reset_call_log()
        result = _with_measurement(
            True, lambda: agents_core.call_role("planner", keys, "sys", "usr"))
        record = result.record
        for name in ("role", "requested", "used", "model", "temperature",
                     "top_p", "at", "measurement_mode"):
            check("the call record carries %s" % name, name in record, record)
        check("the logged model ID matches PROVIDERS",
              record["model"] == agents_core.PROVIDERS[record["used"]]["model"],
              record)
        check("top_p is pinned, not left to the endpoint",
              record["top_p"] == agents_core.TOP_P and record["top_p"] is not None)
        check("the timestamp is UTC", record["at"].endswith("Z"), record["at"])
        check("the record reaches the caller and the log",
              bool(agents_core.CALL_LOG) and agents_core.CALL_LOG[-1] is record)
        blob = json.dumps(agents_core.CALL_LOG)
        check("keys never appear in the logged call record",
              key not in blob and "qkey-1234567890" not in blob)
    finally:
        agents_core.call_model = original
        agents_core.reset_call_log()


def test_provider_errors_are_redacted():
    """The one place a key could plausibly reach a log is an SDK error string."""
    key = "gkey-supersecret-0987654321"
    keys = {"gemini": key, "groq": "qkey-1234567890"}
    original = agents_core.call_model

    def leaky(provider, api_key, system, user):
        raise agents_core.ProviderError(
            "401 unauthorized for key %s at /v1/chat" % api_key)

    try:
        agents_core.call_model = leaky
        agents_core.reset_call_log()
        try:
            _with_measurement(True, lambda: agents_core.call_role(
                "planner", keys, "s", "u"))
            check("a leaky provider error still raises", False, "it returned")
        except agents_core.ProviderError as exc:
            check("the raised message is redacted", key not in str(exc), exc)
        blob = json.dumps(agents_core.CALL_LOG)
        check("the logged error is redacted", key not in blob, blob)
        check("redaction leaves a marker", "<redacted>" in blob)

        # The non-measurement path raises a *different* message, built from the
        # same error. It used to interpolate the exception unredacted, and that
        # message reaches the run JSON.
        agents_core.reset_call_log()
        try:
            _with_measurement(False, lambda: agents_core.call_role(
                "planner", keys, "s", "u"))
            check("exhausting every provider still raises", False, "it returned")
        except agents_core.ProviderError as exc:
            check("the all-providers-failed message is redacted too",
                  key not in str(exc) and keys["groq"] not in str(exc), exc)
        check("a short key is redacted rather than let through",
              "<redacted>" in agents_core._redact("boom sk-1234 boom",
                                                  {"gemini": "sk-1234"}))
        check("a truncated key rendering is redacted",
              key[:8] not in agents_core._redact(
                  "401 for %s..." % key[:8], {"gemini": key}))
    finally:
        agents_core.call_model = original
        agents_core.reset_call_log()


def test_bad_gemini_slug_hard_fails_instead_of_migrating_to_groq():
    """The specific failure this sprint exists to make impossible.

    `gemini-3.6-flash` is verified but not verified *forever*. If the slug ever
    dies, the old behaviour was for every Planner call to 404, for failover to
    route the whole grid to Groq, and for the run to report a two-provider
    system while being a one-provider system. No key and no network are involved
    here: the slug is dead because the stub says so.
    """
    keys = {"gemini": "gkey-1234567890", "groq": "qkey-1234567890"}

    class SlugStub(Stub):
        def __call__(self, provider, api_key, system, user):
            if provider == "gemini":
                self.calls.append((provider, "planner", system, user))
                raise agents_core.ProviderError(
                    "gemini failed: 404 model 'gemini-3.6-flash' not found")
            return Stub.__call__(self, provider, api_key, system, user)

    # Off: the Planner quietly becomes a Groq call and the run looks healthy.
    off_dir = tempfile.mkdtemp(prefix="slug-off-")
    on_dir = tempfile.mkdtemp(prefix="slug-on-")
    try:
        stub = SlugStub({"planner": PLAN, "executor": GOOD_CODE})
        run, _events = _with_measurement(False, lambda: run_with(
            stub, runs_dir=off_dir))
        planner_providers = [c[0] for c in stub.calls if c[1] == "planner"]
        check("off: the dead slug is papered over by the other provider",
              planner_providers == ["gemini", "groq"], planner_providers)
        check("off: the run completes and reports approvals",
              run.verified_count == len(run.steps) and len(run.steps) == 2,
              (run.verified_count, len(run.steps)))

        # On: it stops. Nothing reaches Groq on the Planner's behalf.
        stub = SlugStub({"planner": PLAN, "executor": GOOD_CODE})
        raised = None
        try:
            _with_measurement(True, lambda: run_with(stub, runs_dir=on_dir))
        except agents_core.ProviderError as exc:
            raised = exc
        check("on: a dead Planner slug fails the run", raised is not None)
        check("on: the failure names the disabled failover",
              raised is not None and "failover is disabled" in str(raised),
              raised)
        check("on: the Planner was never rerouted to groq",
              [c[0] for c in stub.calls] == ["gemini"], stub.calls)
    finally:
        shutil.rmtree(off_dir, ignore_errors=True)
        shutil.rmtree(on_dir, ignore_errors=True)


# ------------------------------------------------- the shared plan (sprint 4/2)

def _import_run_eval():
    """`eval/` is not a package; the CLI puts itself on sys.path when run."""
    directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    import run_eval
    return run_eval


def _with_scoped_runs(runs_dir, thunk):
    """Run `thunk` with run JSON redirected into `runs_dir`.

    Arm B goes through `run_workspace`, which writes its own run file to `runs/`.
    A stub sweep's `--out` only moves the *results*, so without this a check
    leaves run logs in the app's own directory.
    """
    memory_original = agents_core.Memory

    class ScopedMemory(memory_original):
        def __init__(self, *args, **kwargs):
            kwargs["dirpath"] = runs_dir
            memory_original.__init__(self, *args, **kwargs)

    agents_core.Memory = ScopedMemory
    try:
        return thunk()
    finally:
        agents_core.Memory = memory_original


def test_supplied_plan_makes_no_planner_call():
    stub = Stub({"planner": "SPEC: this must never be requested\n",
                 "executor": GOOD_CODE})
    workdir = tempfile.mkdtemp(prefix="runs-")
    try:
        run, events = run_with(stub, plan=PLAN, runs_dir=workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    roles = [c[1] for c in stub.calls]
    check("run_workspace(plan=...) makes zero Planner calls",
          "planner" not in roles, roles)
    check("the supplied plan is the plan that was used",
          run.plan == PLAN and "arithmetic helper" in run.spec)
    check("the supplied plan still yields steps", len(run.steps) == 2)
    check("the supplied plan's suite is still resolved and audited",
          run.tests_status == agents_core.TESTS_GENERATED, run.tests_status)
    check("the feed says the Planner was skipped",
          any("Planner call skipped" in content
              for role, content in events if role == "system"))
    check("the plan is still emitted to the feed",
          any(role == "planner" for role, _ in events))


def test_a_prime_prompt_never_carries_the_suite():
    """A' gets the spec. Not the steps, and above all not the tests.

    Proved rather than assumed: the whole arm is worthless if the Executor can
    read the suite it is about to be gated on.
    """
    run_eval = _import_run_eval()
    spec = agents_core.extract_spec(PLAN)
    prompt = run_eval._spec_only_prompt(spec)
    check("the A' prompt carries the spec", "arithmetic helper" in prompt)
    check("the A' prompt has no TESTS section", "TESTS" not in prompt, prompt)
    check("the A' prompt has no assert from the suite",
          "assert add(2, 2)" not in prompt and "assert" not in prompt, prompt)
    check("the A' prompt has no from-solution import",
          "from solution" not in prompt, prompt)
    for line in TESTS.splitlines():
        if line.strip():
            check("suite line absent from the A' prompt: %r" % line[:24],
                  line.strip() not in prompt)


def test_best_of_three_takes_the_first_approved_draw():
    """Not the shortest, not the highest visible score. The first that passes.

    Length and visible score both correlate with hidden correctness, so either
    would quietly upgrade this arm's gate towards the oracle it exists to be
    compared against.
    """
    run_eval = _import_run_eval()
    plan = _plan_fixture()
    # Draw 1 fails the visible suite; draws 2 and 3 pass it. The tie-break must
    # pick 2, and must keep 1 and 3 anyway.
    replies = [WRONG_CODE, GOOD_CODE, GOOD_CODE]
    calls = []
    original = agents_core.call_model
    try:
        def fake(provider, api_key, system, user):
            calls.append((provider, user))
            return replies[min(len(calls) - 1, len(replies) - 1)]

        agents_core.call_model = fake
        outcome = _with_measurement(
            True, lambda: run_eval.run_arm_a_prime3(_FakeTask(), {"groq": "k"},
                                                    plan))
    finally:
        agents_core.call_model = original
    check("three draws are three distinct Executor calls", len(calls) == 3,
          len(calls))
    check("each draw was an independent call on the same spec",
          len(set(user for _p, user in calls)) == 1, calls)
    check("best-of-3 picks the first APPROVED in seeded order",
          outcome["chosen_draw"] == 2, outcome["chosen_draw"])
    check("the chosen draw is the one reported", outcome["pipeline_says_passed"])
    check("every draw is stored, discarded ones included",
          len(outcome["candidates"]) == 3, outcome["candidates"])
    check("a discarded draw keeps its code",
          "return a - b" in outcome["candidates"][0]["code"],
          outcome["candidates"][0])
    check("a discarded draw keeps its gate verdict",
          outcome["candidates"][0]["gate_verdict"] == harness.VERDICT_REVISE,
          outcome["candidates"][0])
    check("no candidate is hidden-graded inline",
          all("passed" not in cand and "grade_reason" not in cand
              for cand in outcome["candidates"]), outcome["candidates"])


class _FakeTask(object):
    """Only `prompt` is read by the A' arms; the hidden suite is not theirs."""
    prompt = "make an arithmetic helper"
    task_id = "fake-01"
    family = "fake"
    tier = 2
    variant = 0
    params = {}
    seed = 0
    names = ("add",)
    tests = TESTS


def test_all_four_arms_run_offline():
    run_eval = _import_run_eval()
    check("every arm the contrasts need is registered",
          sorted(run_eval.ARMS) == ["a", "a_prime", "a_prime3", "b"],
          sorted(run_eval.ARMS))
    check("A'@3's draw count matches the repair budget",
          run_eval.A_PRIME_DRAWS == agents_core.MAX_REVISION_ROUNDS,
          run_eval.A_PRIME_DRAWS)

    # Arm order is a seeded permutation, not "b last because b plans".
    orders = set()
    for index in range(12):
        task = _FakeTask()
        task.task_id = "order-%02d" % index
        orders.add(tuple(run_eval._arm_order(run_eval.ARM_ORDER, 0, task, 0)))
    check("arm order varies across tasks", len(orders) > 1, orders)
    check("arm order is reproducible for one task",
          run_eval._arm_order(run_eval.ARM_ORDER, 0, _FakeTask(), 0)
          == run_eval._arm_order(run_eval.ARM_ORDER, 0, _FakeTask(), 0))
    check("no arm is pinned to first place",
          len(set(order[0] for order in orders)) > 1, orders)

    out = tempfile.mkdtemp(prefix="four-arms-")
    try:
        code = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: run_eval.main(["--stub", "flaky", "--arm", "all", "--family",
                                   "aggregation", "--per-family", "1", "--out",
                                   out, "--seed", "0"]))
        check("the stub path exits clean with no keys and no network", code == 0,
              code)
        written = sorted(os.path.basename(os.path.dirname(path))
                         for path in glob.glob(out + "/seed-0/arm-*/*.json"))
        check("all four arms produced a record",
              written == ["arm-a", "arm-a_prime", "arm-a_prime3", "arm-b"],
              written)
        records = [json.load(open(path))
                   for path in glob.glob(out + "/seed-0/arm-*/*.json")]
        check("every record says measurement mode was on",
              all(r["measurement_mode"] for r in records))
        check("no provider was ever substituted",
              not any(r["provider_substituted"] for r in records))
        check("requested equals used on every logged call",
              all(entry["requested"] == entry["used"] for r in records
                  for entry in r["call_log"]))
        plan_arms = [r for r in records if r["arm"] in run_eval.PLAN_ARMS]
        check("the plan-consuming arms share one Planner call",
              all(r["plan_calls"] == 1 and r["plan_shared"] for r in plan_arms),
              [(r["arm"], r.get("plan_calls")) for r in plan_arms])
        allowed = set(("role", "requested", "used", "model", "temperature",
                       "top_p", "at", "measurement_mode", "ok"))
        stray = [sorted(set(entry) - allowed) for r in records
                 for entry in r["call_log"] + r.get("plan_call_log", [])
                 if set(entry) - allowed]
        check("the persisted call log carries only the allow-listed fields",
              not stray, stray)
        a3 = [r for r in records if r["arm"] == "a_prime3"][0]
        check("A'@3 stored three candidates", len(a3["candidates"]) == 3,
              a3["candidates"])
        check("A'@3 made three Executor calls", a3["calls"] == 3, a3["calls"])
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ------------------------------------------------ task 3: keep the best round

# A deeper suite, so a candidate can get part-way through it. One assert per
# line from line 3, so a reported failing line is unambiguous.
DEEP_TESTS = (
    "from solution import add\n\n"
    "assert add(2, 2) == 4\n"
    "assert add(0, 0) == 0\n"
    "assert add(1, 2) == 3\n"
    "assert add(20, 5) == 25\n"
)

DEEP_PLAN = (
    "SPEC: Build a tiny arithmetic helper exposing add(a, b) -> int.\n"
    "STEPS:\n"
    "1. Write an add function with asserts.\n"
    "TESTS:\n```python\n" + DEEP_TESTS + "```\n"
)

# Imports, runs, clears the first three asserts, fails the fourth -- line 6 of
# the suite. Strictly further than BAD_CODE, which dies while being imported.
PARTIAL_CODE = (
    "```python\n"
    "def add(a, b):\n    return a + b if a < 10 else 0\n\n"
    "print('module loaded')\n```\n"
)

DEEP_GOOD_CODE = (
    "```python\n"
    "def add(a, b):\n    return a + b\n\n"
    "print('module loaded')\n```\n"
)

CANDIDATE_LOG_FIELDS = sorted(("round", "provider", "code", "verdict",
                               "exit_code", "failure_kind",
                               "failed_assertion", "failed_assertion_line"))


def _retention_run(replies, runs_dir=None):
    """One step, one plan, `replies` as the Executor's successive answers.

    Run JSON goes to a scratch directory unless the caller wants to inspect it,
    so these checks do not accumulate files in the app's own `runs/`.
    """
    stub = Stub({"planner": DEEP_PLAN, "executor": replies})
    if runs_dir:
        return run_with(stub, runs_dir=runs_dir)
    scratch = tempfile.mkdtemp(prefix="retain-scratch-")
    try:
        return run_with(stub, runs_dir=scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_a_deep_assertion_failure_beats_a_later_import_failure():
    run, events = _retention_run([PARTIAL_CODE, BAD_CODE, BAD_CODE, BAD_CODE])
    step = run.steps[0]
    check("every round was stored as a candidate", len(step.candidates) == 4,
          [c["round"] for c in step.candidates])
    check("the later rounds never reached an assert",
          all(c["failure_kind"] != harness.FAIL_ASSERTION
              for c in step.candidates[1:]),
          [c["failure_kind"] for c in step.candidates[1:]])
    check("round 1 is the retained candidate, not round 4",
          step.retained_round == 1, step.retained_round)
    check("the retained candidate is the one that ran under test",
          step.failure_kind == harness.FAIL_ASSERTION, step.failure_kind)
    check("the retained failing line is the deep one",
          step.failed_assertion_line == 6, step.failed_assertion_line)
    check("the retained code is round 1's", "a < 10" in (step.code or ""),
          step.code)
    check("the final round is flagged as worse than the retained one",
          step.final_round_worse is True, step.final_round_worse)
    check("`rounds` still counts rounds spent, not the retained round",
          step.rounds == agents_core.MAX_REVISION_ROUNDS, step.rounds)
    check("the feed says an earlier candidate was kept",
          any("was worse than round" in content for _, content in events))
    check("a discarded candidate keeps its own code",
          "a - b" in (step.candidates[3]["code"] or ""),
          step.candidates[3]["code"])


def test_an_approved_candidate_wins_outright():
    run, _ = _retention_run([PARTIAL_CODE, DEEP_GOOD_CODE])
    step = run.steps[0]
    check("APPROVED at round 2 beats a deep assertion failure at round 1",
          step.retained_round == 2, step.retained_round)
    check("the step is APPROVED",
          step.verdict == harness.VERDICT_APPROVED, step.verdict)
    check("APPROVED means nothing was retained over it",
          step.final_round_worse is False, step.final_round_worse)
    check("the approved code is round 2's", "a < 10" not in (step.code or ""),
          step.code)
    check("one revision round was spent to get there", step.rounds == 1,
          step.rounds)
    check("both attempts are stored", len(step.candidates) == 2,
          [c["round"] for c in step.candidates])


def test_equal_rank_candidates_keep_the_earliest_round():
    run, events = _retention_run([BAD_CODE, BAD_CODE, BAD_CODE, BAD_CODE])
    step = run.steps[0]
    ranks = set(agents_core._candidate_rank(c)[:3] for c in step.candidates)
    check("the four candidates tie on everything except round number",
          len(ranks) == 1, ranks)
    check("the earliest of equal-rank candidates is kept",
          step.retained_round == 1, step.retained_round)
    check("an equal-rank final round is not 'worse'",
          step.final_round_worse is False, step.final_round_worse)
    check("no 'earlier candidate was kept' line when nothing improved",
          not any("was worse than round" in content for _, content in events))
    check("all four rounds are still stored", len(step.candidates) == 4,
          [c["round"] for c in step.candidates])


def test_retention_reaches_the_run_json():
    tmp = tempfile.mkdtemp(prefix="retain-")
    try:
        _retention_run([PARTIAL_CODE, BAD_CODE, BAD_CODE, BAD_CODE],
                       runs_dir=tmp)
        paths = glob.glob(os.path.join(tmp, "*.json"))
        check("the run wrote exactly one file", len(paths) == 1, paths)
        step = json.load(open(paths[0]))["steps"][0]
        check("retained_round is persisted", step["retained_round"] == 1,
              step["retained_round"])
        check("final_round_worse is persisted",
              step["final_round_worse"] is True, step["final_round_worse"])
        check("rounds spent is persisted separately from retained_round",
              step["rounds"] == agents_core.MAX_REVISION_ROUNDS, step["rounds"])
        check("every candidate is persisted, discarded ones included",
              [c["round"] for c in step["candidates"]] == [1, 2, 3, 4],
              step["candidates"])
        check("the discarded candidate's code is persisted whole",
              "a - b" in (step["candidates"][3]["code"] or ""),
              step["candidates"][3]["code"])
        check("persisted candidates carry only the log fields",
              all(sorted(c) == CANDIDATE_LOG_FIELDS
                  for c in step["candidates"]),
              [sorted(c) for c in step["candidates"]])
        check("candidates are stored ungraded; grading is a later pass",
              not any("passed" in c or "grade_reason" in c
                      for c in step["candidates"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------- sprint 5, task 2: one suite gates every arm

# A plan whose own TESTS block fails the vacuity audit -- the same rejection
# `test_regeneration_can_rescue_a_vacuous_suite` exercises, seen from the
# injection side. This is the *only* condition under which resolving one plan
# twice yields two different suites, and no stored eval record had ever hit it.
VACUOUS_PLAN = PLAN_NO_TESTS + "TESTS:\n" + VACUOUS_TESTS


def _resolved(tests, status=agents_core.TESTS_GENERATED, summary="fixture"):
    """A `resolved_tests()`-shaped payload, built without running anything."""
    return agents_core.resolved_tests(
        agents_core.RunResult(tests=tests, tests_status=status,
                              tests_summary=summary))


# What the Test Writer answers with under `VACUOUS_PLAN`: a good suite, and
# deliberately *not* byte-equal to `TESTS`. Both suites pass GOOD_CODE, so the
# divergence this sprint closes shows up as different bytes rather than as a
# different verdict -- which is exactly why it went unnoticed.
ALT_TESTS = (
    "from solution import add\n\n"
    "assert add(3, 4) == 7\n"
    "assert add(-2, 2) == 0\n"
)


def _injection_run(tests, runs_dir, **kwargs):
    """One mode-3 run off `VACUOUS_PLAN`, returning the stub as well.

    The Test Writer answer is *good*, so a run that regenerates gets a usable
    suite. That is deliberate: if regeneration failed, the run would be
    UNVERIFIED and "did the Test Writer get called" would be answerable from the
    verdict alone. Here both paths end APPROVED and only the bytes differ.
    """
    stub = Stub({"planner": "the plan is supplied; this must not be requested",
                 "test_writer": "```python\n" + ALT_TESTS + "```\n",
                 "executor": GOOD_CODE})
    run, events = run_with(stub, plan=VACUOUS_PLAN, tests=tests,
                           runs_dir=runs_dir, **kwargs)
    return stub, run, events


def _test_writer_calls(stub):
    return len([c for c in stub.calls if c[1] == "test_writer"])


def test_injected_suite_skips_the_test_writer():
    scratch = tempfile.mkdtemp(prefix="inject-")
    try:
        payload = _resolved(TESTS, agents_core.TESTS_REGENERATED)
        stub, run, events = _injection_run(payload,
                                          os.path.join(scratch, "injected"))
        # The contrast, on the same plan: not injecting costs a generation.
        without, plain, _ = _injection_run(None, os.path.join(scratch, "plain"))
        check("without injection a vacuous plan costs a Test Writer call",
              _test_writer_calls(without) == 1,
              [c[1] for c in without.calls])
        check("an injected suite makes zero Test Writer calls even when the "
              "plan's own TESTS block is vacuous",
              _test_writer_calls(stub) == 0, [c[1] for c in stub.calls])
        check("regeneration and injection produce different bytes, so the "
              "divergence is real and not hypothetical",
              plain.tests == ALT_TESTS and run.tests != plain.tests,
              (plain.tests_status, repr(plain.tests[:40])))
        check("both paths still end APPROVED, which is why this hid",
              plain.tests_status == agents_core.TESTS_REGENERATED
              and all(s.verdict == harness.VERDICT_APPROVED
                      for s in plain.steps),
              [s.verdict for s in plain.steps])
        check("the injected bytes are the suite that gates", run.tests == TESTS,
              repr(run.tests))
        check("the incoming status is carried, not overwritten",
              run.tests_status == agents_core.TESTS_REGENERATED,
              run.tests_status)
        check("an injected suite is never recorded as the user's",
              run.tests_status != agents_core.TESTS_USER, run.tests_status)
        check("an injected trusted suite still gates APPROVED",
              run.tests_trusted and all(s.verdict == harness.VERDICT_APPROVED
                                        for s in run.steps),
              [s.verdict for s in run.steps])
        check("the tests feed entry is still emitted",
              any(role == "tests" for role, _ in events))
        check("the feed says the suite arrived already resolved",
              any("already resolved" in content
                  for role, content in events if role == "system"))
        stored = json.load(open(sorted(glob.glob(
            os.path.join(scratch, "injected", "*.json")))[0]))
        check("the run JSON keeps its shape under injection",
              stored["tests"] == TESTS and stored["tests_status"]
              == agents_core.TESTS_REGENERATED and stored["tests_audit"],
              {k: stored.get(k) for k in ("tests_status", "tests_audit")})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_user_tests_still_beat_an_injected_suite():
    """Precedence does not bend for a measurement."""
    mine = "from solution import add\n\nassert add(2, 2) == 5\n"
    scratch = tempfile.mkdtemp(prefix="inject-user-")
    try:
        stub, run, _ = _injection_run(_resolved(TESTS), scratch,
                                      user_tests=mine)
        check("user tests beat an injected suite",
              run.tests_status == agents_core.TESTS_USER, run.tests_status)
        check("the user's bytes are the ones that ran",
              "add(2, 2) == 5" in run.tests and "add(-1, 1)" not in run.tests,
              repr(run.tests))
        check("the user's suite is what the steps were gated on",
              all(s.failure_kind == harness.FAIL_ASSERTION for s in run.steps),
              [s.failure_kind for s in run.steps])
        check("user tests plus an injected suite is still no Test Writer call",
              _test_writer_calls(stub) == 0, [c[1] for c in stub.calls])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _raises(thunk):
    try:
        thunk()
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return "wrong type: %s: %s" % (type(exc).__name__, exc)
    return ""


def test_injection_guards_are_programming_errors():
    """Every one of these is a caller bug that nothing downstream could detect."""
    stub = Stub({"executor": GOOD_CODE})
    no_plan = _raises(lambda: run_with(stub, tests=_resolved(TESTS)))
    check("tests= without plan= raises", "requires the plan" in no_plan,
          no_plan)
    scratch = tempfile.mkdtemp(prefix="inject-guard-")
    try:
        as_user = _raises(lambda: _injection_run(
            _resolved(TESTS, agents_core.TESTS_USER), scratch))
        check("a user suite cannot be smuggled in as a resolved one",
              "pass it as user_tests=" in as_user, as_user)
        partial = _resolved(TESTS)
        partial.pop("tests_summary")
        missing = _raises(lambda: _injection_run(partial, scratch))
        check("an incomplete payload raises rather than defaulting",
              "tests_summary" in missing, missing)
        forged = _resolved(TESTS)
        forged["tests_trusted"] = False
        mismatch = _raises(lambda: _injection_run(forged, scratch))
        check("a forged trust flag raises instead of being assigned",
              "tests_trusted" in mismatch, mismatch)
        check("trust stays derived from status and source",
              not hasattr(agents_core.RunResult, "tests_trusted")
              or isinstance(agents_core.RunResult.tests_trusted, property),
              type(agents_core.RunResult.tests_trusted))
        check("every field of a resolved suite is required",
              sorted(agents_core.RESOLVED_TESTS_FIELDS)
              == ["tests", "tests_status", "tests_summary", "tests_trusted"],
              agents_core.RESOLVED_TESTS_FIELDS)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

def _plan_fixture(tests=TESTS, trusted=True, status=None):
    """A `make_plan()`-shaped dict, for the arms that consume one."""
    run_eval = _import_run_eval()
    status = status or (agents_core.TESTS_GENERATED if trusted
                       else agents_core.TESTS_VACUOUS)
    body = tests if trusted else ""
    return {"plan": PLAN, "spec": agents_core.extract_spec(PLAN),
            "tests": body, "tests_status": status, "tests_trusted": trusted,
            "tests_payload": _resolved(body, status),
            "tests_sha256": run_eval._suite_hash(body)}


def test_one_suite_gates_every_arm_on_a_vacuous_plan():
    """The check that would have caught it: same task, same gate bytes.

    Run offline with `--vacuous-plan`, so the Planner's own TESTS block is
    rejected and the regeneration branch -- the branch every stored record had
    missed -- actually executes.
    """
    run_eval = _import_run_eval()
    out = tempfile.mkdtemp(prefix="one-gate-")
    try:
        code = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: run_eval.main(["--stub", "perfect", "--vacuous-plan",
                                   "--arm", "all", "--family", "aggregation",
                                   "--per-family", "1", "--out", out,
                                   "--seed", "0"]))
        check("the vacuity path runs offline with no keys and no network",
              code == 0, code)
        records = {}
        for path in glob.glob(out + "/seed-0/arm-*/*.json"):
            record = json.load(open(path))
            records[record["arm"]] = record
        plan_arms = [records[arm] for arm in run_eval.PLAN_ARMS]
        check("the Planner's vacuous suite was rejected and regenerated",
              all(r["plan_tests_status"] == agents_core.TESTS_REGENERATED
                  for r in plan_arms),
              [(r["arm"], r["plan_tests_status"]) for r in plan_arms])
        hashes = set(r["gate_tests_sha256"] for r in plan_arms)
        check("a_prime, a_prime3 and b gate on byte-identical suites",
              len(hashes) == 1 and all(hashes), hashes)
        check("arm b's own recorded suite is the injected one",
              records["b"]["tests_status"] == agents_core.TESTS_REGENERATED,
              records["b"]["tests_status"])
        # One Planner call plus one Test Writer call for the whole task. The
        # shared plan log is one list, so it is counted once, not per arm.
        writer = [entry for entry in plan_arms[0]["plan_call_log"]
                  if entry["role"] == "test_writer"]
        check("exactly one Test Writer call for the whole task",
              len(writer) == 1, [e["role"] for e in plan_arms[0]["plan_call_log"]])
        check("no arm made a Test Writer call of its own",
              not [entry for r in records.values() for entry in r["call_log"]
                   if entry["role"] == "test_writer"],
              [(r["arm"], [e["role"] for e in r["call_log"]])
               for r in records.values()])
        check("the shared plan cost two calls, not four",
              all(r["plan_calls"] == 2 for r in plan_arms),
              [(r["arm"], r["plan_calls"]) for r in plan_arms])
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ----------------------- sprint 5, task 3: when there is no trustworthy suite

def _a_prime3(plan, replies):
    """Run A'@3 offline with `replies` as the Executor's successive answers."""
    run_eval = _import_run_eval()
    calls = []
    original = agents_core.call_model
    try:
        def fake(provider, api_key, system, user):
            calls.append(user)
            return replies[min(len(calls) - 1, len(replies) - 1)]

        agents_core.call_model = fake
        return _with_measurement(
            True, lambda: run_eval.run_arm_a_prime3(_FakeTask(), {"groq": "k"},
                                                    plan))
    finally:
        agents_core.call_model = original


def test_an_untrusted_suite_is_recorded_as_a_fallback_not_a_gate_win():
    run_eval = _import_run_eval()
    blind = _a_prime3(_plan_fixture(trusted=False), [GOOD_CODE] * 3)
    check("an untrusted suite means no gate",
          blind["gate_available"] is False, blind["gate_available"])
    check("the selection is recorded as a gate-unavailable fallback",
          blind["selection"] == run_eval.SELECTION_NO_GATE, blind["selection"])
    check("a fallback is not a gate win",
          blind["selected_by_gate"] is False, blind["selected_by_gate"])
    check("the arm records that it degenerated to A'",
          blind["degenerated_to_a_prime"] is True,
          blind["degenerated_to_a_prime"])
    check("draw 1 is still what was returned", blind["chosen_draw"] == 1,
          blind["chosen_draw"])
    check("all three draws are still stored, not dropped",
          len(blind["candidates"]) == 3 and blind["draws"] == 3,
          blind["draws"])
    check("nothing is APPROVED without a suite to approve it",
          blind["pipeline_says_passed"] is False
          and blind["verdict"] == harness.VERDICT_UNVERIFIED, blind["verdict"])

    # Trusted suite, no draw passes it: a fallback too, but a *different* one.
    unlucky = _a_prime3(_plan_fixture(), [WRONG_CODE] * 3)
    check("a gate that ran and approved nothing is its own selection mode",
          unlucky["selection"] == run_eval.SELECTION_NO_APPROVAL,
          unlucky["selection"])
    check("that fallback is not the gate-unavailable one",
          unlucky["degenerated_to_a_prime"] is False
          and unlucky["gate_available"] is True, unlucky)
    check("the three selection modes are three distinct values",
          len(set((run_eval.SELECTION_GATE_WIN, run_eval.SELECTION_NO_APPROVAL,
                   run_eval.SELECTION_NO_GATE))) == 3)


def test_a_trusted_suite_still_records_a_gate_win():
    run_eval = _import_run_eval()
    won = _a_prime3(_plan_fixture(), [WRONG_CODE, GOOD_CODE, GOOD_CODE])
    check("a passing draw under a trusted suite is a gate win",
          won["selection"] == run_eval.SELECTION_GATE_WIN, won["selection"])
    check("the gate-win marker is set", won["selected_by_gate"] is True,
          won["selected_by_gate"])
    check("a gate win is not a degeneration",
          won["degenerated_to_a_prime"] is False
          and won["gate_available"] is True, won)
    check("the winning draw is the first that passed", won["chosen_draw"] == 2,
          won["chosen_draw"])
    check("the gate suite's identity is recorded for the arm",
          won["gate_tests_sha256"] == _plan_fixture()["tests_sha256"]
          and won["gate_tests_sha256"], won["gate_tests_sha256"])


def _summary_record(arm, trusted="absent", passed=False, tier=2):
    record = {"arm": arm, "tier": tier, "family": "aggregation",
              "passed": passed, "pipeline_says_passed": passed,
              "false_approved": False, "seconds": 1.0, "calls": 1}
    if trusted != "absent":
        record["tests_trusted"] = trusted
        record["degenerated_to_a_prime"] = (arm == "a_prime3"
                                            and trusted is False)
    return record


def _summary_text(records):
    run_eval = _import_run_eval()
    buffer, original = io.StringIO(), sys.stdout
    sys.stdout = buffer
    try:
        run_eval._print_summary(run_eval.summarise(records))
    finally:
        sys.stdout = original
    return buffer.getvalue()


def test_untrusted_suites_are_counted_and_explained():
    run_eval = _import_run_eval()
    mixed = [_summary_record("a_prime3", False),
             _summary_record("a_prime3", True, passed=True),
             _summary_record("a_prime3", True),
             _summary_record("a", "absent")]
    summary = run_eval.summarise(mixed)
    pooled = summary["by_arm"]["a_prime3"]["pooled_not_a_result"]
    check("untrusted-suite tasks are counted per arm",
          pooled["untrusted_suite"] == 1, pooled["untrusted_suite"])
    check("the degeneration is counted separately from the count of suites",
          pooled["degenerated_to_a_prime"] == 1,
          pooled["degenerated_to_a_prime"])
    check("arm A, which has no suite at all, counts zero rather than all",
          summary["by_arm"]["a"]["pooled_not_a_result"]["untrusted_suite"] == 0,
          summary["by_arm"]["a"]["pooled_not_a_result"]["untrusted_suite"])
    check("the count is stratified, not only pooled",
          summary["by_arm"]["a_prime3"]["by_tier"]["2"]["untrusted_suite"] == 1)

    text = _summary_text(mixed)
    check("the count is a column in the per-arm table, not a footnote",
          "untrust" in text.splitlines()[2], text.splitlines()[:3])
    check("the pooled line carries it too", "1 untrusted suite" in text, text)
    check("the interpretation prints when the count is non-zero",
          "attenuated toward zero" in text and "degenerates to A'" in text, text)
    check("the interpretation says a near-zero gate loss must not be read as a "
          "good selector", "good selector" in text, text)
    check("the interpretation says they are reported rather than dropped",
          "not dropped" in text and "post-treatment" in text, text)

    clean = _summary_text([_summary_record("a_prime3", True, passed=True),
                           _summary_record("b", True)])
    check("the interpretation is silent when nothing was untrusted",
          "attenuated toward zero" not in clean, clean)
    check("the column stays even when the count is zero",
          "untrust" in clean and "0 untrusted suite" in clean, clean)


def main():
    for fn in (
        test_pipeline_is_explicit, test_mode_2_skips_execution,
        test_retired_mode_4_runs_mode_3, test_working_code_is_approved,
        test_broken_code_is_never_approved, test_traceback_is_fed_back_to_executor,
        test_prose_output_is_not_approved,
        test_tests_are_parsed_and_emitted, test_wrong_answer_that_runs_is_revised,
        test_import_failure_gets_different_guidance,
        test_vacuous_tests_force_unverified,
        test_regeneration_can_rescue_a_vacuous_suite,
        test_missing_tests_cap_the_verdict, test_user_tests_win_over_generated,
        test_executor_cannot_weaken_the_tests,
        test_escalation_is_deterministic,
        test_escalation_ladder_is_climbed_in_order,
        test_giving_up_is_explicit, test_escalation_degrades_with_one_key,
        test_executor_prompt_states_the_sandbox_contract,
        test_memory_does_not_bleed,
        test_memory_never_reads_prior_state, test_context_is_bounded,
        test_context_stays_bounded_across_many_steps, test_step_cap,
        test_events_arrive_incrementally, test_provider_fallback,
        # sprint 4, task 1: measurement mode
        test_measurement_mode_disables_silent_failover,
        test_measurement_mode_honours_prefer,
        test_call_record_is_complete_and_key_free,
        test_provider_errors_are_redacted,
        test_bad_gemini_slug_hard_fails_instead_of_migrating_to_groq,
        # sprint 4, task 2: arms A' and A'@3, one shared plan
        test_supplied_plan_makes_no_planner_call,
        test_a_prime_prompt_never_carries_the_suite,
        test_best_of_three_takes_the_first_approved_draw,
        test_all_four_arms_run_offline,
        # sprint 4, task 3: retain the best candidate
        test_a_deep_assertion_failure_beats_a_later_import_failure,
        test_an_approved_candidate_wins_outright,
        test_equal_rank_candidates_keep_the_earliest_round,
        test_retention_reaches_the_run_json,
        # sprint 5, task 2: one resolved suite gates every arm
        test_injected_suite_skips_the_test_writer,
        test_user_tests_still_beat_an_injected_suite,
        test_injection_guards_are_programming_errors,
        test_one_suite_gates_every_arm_on_a_vacuous_plan,
        # sprint 5, task 3: register the no-trustworthy-suite case
        test_an_untrusted_suite_is_recorded_as_a_fallback_not_a_gate_win,
        test_a_trusted_suite_still_records_a_gate_win,
        test_untrusted_suites_are_counted_and_explained,
    ):
        print("\n-- %s" % fn.__name__)
        try:
            fn()
        except Exception as exc:
            check(fn.__name__ + " (raised)", False, "%s: %s" % (type(exc).__name__, exc))

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

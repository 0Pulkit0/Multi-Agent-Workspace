"""Offline checks for the pipeline itself. No API keys, no network.

The model layer is stubbed, so this exercises the wiring: explicit pipelines,
per-run memory isolation, bounded context, incremental events, the acceptance
suite (including the vacuity guard and the anti-cheating rule), and the
harness-driven revision loop.

    python3 test_pipeline.py
"""

import ast
import contextlib
import copy
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

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

    def __call__(self, provider, api_key, system, user, role=None):
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

    def tracking_stub(provider, api_key, system, user, role=None):
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

    def flaky(provider, api_key, system, user, role=None):
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

    def fake(provider, api_key, system, user, role=None):
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
        agents_core.call_model = lambda p, k, s, u, role=None: "```python\npass\n```\n"
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

    def leaky(provider, api_key, system, user, role=None):
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
        def __call__(self, provider, api_key, system, user, role=None):
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
        def fake(provider, api_key, system, user, role=None):
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
    # Hardcoded, not compared to itself: comparing two calls only pins
    # determinism, and a wrong-but-stable ordering -- a different digest slice, a
    # different key string, `random.seed(key)` instead of sha256 -- is stable too
    # and would sail through. These literals were read off the implementation
    # once; if one of them moves, the permutation moved, and every arm-order
    # figure recorded under the old one is from a different experiment.
    check("arm order matches a hardcoded permutation for one fixed key",
          run_eval._arm_order(run_eval.ARM_ORDER, 0, _FakeTask(), 0)
          == ["b", "a", "a_prime3", "a_prime"],
          run_eval._arm_order(run_eval.ARM_ORDER, 0, _FakeTask(), 0))
    check("the arm-order key includes the seed and the repeat",
          [run_eval._arm_order(run_eval.ARM_ORDER, 1, _FakeTask(), 0),
           run_eval._arm_order(run_eval.ARM_ORDER, 0, _FakeTask(), 1)]
          == [["a_prime3", "b", "a", "a_prime"],
              ["a_prime", "a", "b", "a_prime3"]],
          [run_eval._arm_order(run_eval.ARM_ORDER, 1, _FakeTask(), 0),
           run_eval._arm_order(run_eval.ARM_ORDER, 0, _FakeTask(), 1)])
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
                       "top_p", "at", "measurement_mode", "ok", "status",
                       "exc_class", "attempts", "retried"))
        stray = [sorted(set(entry) - allowed) for r in records
                 for entry in r["call_log"] + r.get("plan_call_log", [])
                 if set(entry) - allowed]
        check("the persisted call log carries only the allow-listed fields",
              not stray, stray)
        check("the persisted call log carries no prompt text and no error text",
              not [name for r in records
                   for entry in r["call_log"] + r.get("plan_call_log", [])
                   for name in entry
                   if name in ("system", "user", "prompt", "error",
                               "response", "text")])
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
                               "failed_assertion", "failed_assertion_line",
                               "checks_passed", "checks_total",
                               "checks_trusted"))


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
        def fake(provider, api_key, system, user, role=None):
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


def _summary_record(arm, trusted="absent", passed=False, tier=2,
                    task_id="task-01", outcome=None, status=None,
                    selection=None, repeat=1):
    record = {"task_id": task_id, "arm": arm, "tier": tier,
              "family": "aggregation", "passed": passed,
              "pipeline_says_passed": passed,
              "false_approved": False, "seconds": 1.0, "calls": 1}
    if trusted != "absent":
        record["tests_trusted"] = trusted
        record["degenerated_to_a_prime"] = (arm == "a_prime3"
                                            and trusted is False)
    if outcome is not None:
        record["outcome"] = outcome
    if status is not None:
        # What a record that consumed a shared plan carries. Absent by default so
        # every existing caller keeps producing exactly the record it did before.
        record["plan_shared"] = True
        record["plan_tests_status"] = status
        record["repeat"] = repeat
    if selection is not None:
        record["selection"] = selection
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
    # By its `pass%` column, not by line number. This used to read
    # `text.splitlines()[2]`, which made the check a lock on the output's line
    # ordering: it would have failed on any new block printed above the tables
    # while still passing on an `untrust` column that had moved to a footnote.
    header = [line for line in text.splitlines() if "pass%" in line]
    check("the count is a column in the per-arm table, not a footnote",
          header and all("untrust" in line for line in header), header)
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


def test_the_no_gate_rate_is_a_first_class_diagnostic_before_the_grid():
    """`tests_status` and the SELECTION_NO_GATE rate, printed above the pass rates.

    Every SELECTION_NO_GATE task is one where A'@3 had no gate, returned draw 1 by
    position and therefore contributed an A' value to A'@3. The blind gate-loss
    term is attenuated toward zero by each of them, so the rate is a qualifier on
    every number in the grid rather than a detail about the Planner -- which means
    it has to be readable before the grid, not discoverable in `summary.json`
    after the fact.

    Two things are asserted about the shape and both matter. The distribution is
    per SUITE, so three arms sharing one Planner call must not count that suite
    three times. And `unusable` is kept distinct from `vacuous`: the first is a
    Test Writer whose output had no extractable code block, the second is a suite
    that extracted cleanly and asserts nothing real. Those are different failures
    of the same mechanism and collapsing them would hide which one is happening.
    """
    run_eval = _import_run_eval()
    records = []
    for task_id, status, trusted, selection in (
            ("task-01", "generated", True, run_eval.SELECTION_GATE_WIN),
            ("task-02", "unusable", False, run_eval.SELECTION_NO_GATE),
            ("task-03", "vacuous", False, run_eval.SELECTION_NO_GATE),
            ("task-04", "generated", True, run_eval.SELECTION_NO_APPROVAL)):
        for arm in ("a_prime", "a_prime3", "b"):
            records.append(_summary_record(
                arm, trusted, task_id=task_id, status=status,
                selection=selection if arm == "a_prime3" else None))
    gate = run_eval.summarise(records)["gate_availability"]
    check("one suite per task and repeat, not one per arm record",
          gate["suites"] == 4 and len(records) == 12,
          (gate["suites"], len(records)))
    check("the tests_status distribution is over suites and keeps `unusable` and "
          "`vacuous` apart",
          gate["by_tests_status"] == {"generated": 2, "unusable": 1,
                                      "vacuous": 1},
          gate["by_tests_status"])
    check("the no-gate rate is over A'@3 cells and names the tasks",
          (gate["selection_no_gate"], gate["a_prime3_cells"]) == (2, 4)
          and abs(gate["selection_no_gate_rate"] - 50.0) < 1e-9
          and gate["selection_no_gate_task_ids"] == ["task-02", "task-03"],
          (gate["selection_no_gate"], gate["a_prime3_cells"],
           gate["selection_no_gate_rate"], gate["selection_no_gate_task_ids"]))
    check("and the untrusted suites are named too, so the rate is actionable",
          gate["untrusted_task_ids"] == ["task-02", "task-03"]
          and (gate["trusted"], gate["untrusted"]) == (2, 2),
          (gate["untrusted_task_ids"], gate["trusted"], gate["untrusted"]))

    text = _summary_text(records)
    lines = text.splitlines()
    first_table = next(index for index, line in enumerate(lines)
                       if "pass%" in line)
    gate_line = next(index for index, line in enumerate(lines)
                     if line.startswith("gate availability:"))
    check("it prints before the first per-arm table rather than after it",
          gate_line < first_table and gate_line == 0, (gate_line, first_table))
    check("the printed block carries the rate, the distribution and the note",
          "SELECTION_NO_GATE: 2/4 = 50.0%" in text
          and "unusable=1" in text and "vacuous=1" in text
          and "attenuates the blind gate-loss term" in text, text[:600])

    quiet = _summary_text([
        _summary_record("a_prime3", True, task_id="task-01",
                        status="generated",
                        selection=run_eval.SELECTION_GATE_WIN)])
    check("the block still prints when every suite was trusted, because a silent "
          "zero and a silent third of the task set read identically",
          "gate availability: 1 suite(s), 1 trusted, 0 untrusted" in quiet
          and "SELECTION_NO_GATE: 0/1 = 0.0%" in quiet
          and "attenuates the blind gate-loss term" not in quiet, quiet[:400])


def test_the_no_approval_rate_is_surfaced_beside_the_no_gate_rate():
    """Two fallbacks to draw 1, two causes, and only one of them was ever printed.

    `SELECTION_NO_GATE` and `SELECTION_NO_APPROVAL` both return draw 1 by position
    and both attenuate the blind gate-loss term, so a report that surfaces the
    first and not the second reads as full gate availability on a run where the
    gate rejected every draw it saw. The rate is asserted in the JSON *and* in the
    printed block, because the printed block is what gets read before the grid.
    """
    run_eval = _import_run_eval()
    records = []
    for task_id, status, trusted, selection in (
            ("task-01", "generated", True, run_eval.SELECTION_GATE_WIN),
            ("task-02", "unusable", False, run_eval.SELECTION_NO_GATE),
            ("task-03", "generated", True, run_eval.SELECTION_NO_APPROVAL),
            ("task-04", "generated", True, run_eval.SELECTION_NO_APPROVAL)):
        for arm in ("a_prime", "a_prime3", "b"):
            records.append(_summary_record(
                arm, trusted, task_id=task_id, status=status,
                selection=selection if arm == "a_prime3" else None))
    gate = run_eval.summarise(records)["gate_availability"]
    check("the no-approval rate is over the same A'@3 denominator as no-gate",
          (gate["selection_no_approval"], gate["a_prime3_cells"]) == (2, 4)
          and abs(gate["selection_no_approval_rate"] - 50.0) < 1e-9,
          (gate["selection_no_approval"], gate["a_prime3_cells"],
           gate["selection_no_approval_rate"]))
    check("and it names its tasks, the way the no-gate rate does",
          gate["selection_no_approval_task_ids"] == ["task-03", "task-04"],
          gate["selection_no_approval_task_ids"])
    check("the two fallbacks are counted separately, not pooled",
          gate["selection_no_gate_task_ids"] == ["task-02"],
          gate["selection_no_gate_task_ids"])

    text = _summary_text(records)
    lines = text.splitlines()
    no_gate_line = next(index for index, line in enumerate(lines)
                        if "SELECTION_NO_GATE:" in line)
    no_approval_line = next(index for index, line in enumerate(lines)
                            if "SELECTION_NO_APPROVAL:" in line)
    check("it prints in the same block, immediately under the no-gate line",
          no_approval_line == no_gate_line + 1,
          (no_gate_line, no_approval_line))
    check("the printed rate matches the computed one",
          "SELECTION_NO_APPROVAL: 2/4 = 50.0%" in text, text[:800])
    check("and the block explains that a rejected-everything gate is not the "
          "same event as an absent one",
          "indistinguishable from three" in text, text[-900:])

    quiet = _summary_text([
        _summary_record("a_prime3", True, task_id="task-01", status="generated",
                        selection=run_eval.SELECTION_GATE_WIN)])
    check("the no-approval line prints when it is zero, for the same reason the "
          "no-gate line does",
          "SELECTION_NO_APPROVAL: 0/1 = 0.0%" in quiet
          and "indistinguishable from three" not in quiet, quiet[:600])


def _draw_row(task_id, trusted, passed, gate_approved, **extra):
    """One draw-level row in the shape both ledgers already store."""
    row = {"task_id": task_id, "tests_trusted": trusted, "passed": passed,
           "gate_approved": gate_approved}
    row.update(extra)
    return row


def test_a_suite_that_rejects_every_hidden_pass_is_counted_as_a_wrong_suite():
    """The post-hoc separator between "the models were bad" and "the suite was wrong".

    A trustworthy suite that rejects draws the hidden suite accepts is testing
    something the task never asked for. `grouping-02` is the worked example -- three
    candidates, three hidden passes, three gate rejections, all from `==` on a
    float mean -- and without this count it is indistinguishable in the summary
    from a task where three models genuinely failed.

    Report-only by construction: the rule is a pure function over rows that were
    already being written, so it scores ledgers that predate it and adds nothing
    to the arm. What it must NOT do is fire on an untrusted suite, where there was
    no gate to be wrong, or on a task whose draws actually failed.
    """
    run_eval = _import_run_eval()
    fires = [_draw_row("grouping-02", True, True, False) for _ in range(3)]
    check("it fires when a trusted suite rejects three hidden passes",
          run_eval.wrong_suite_tasks(fires) == ["grouping-02"],
          run_eval.wrong_suite_tasks(fires))
    check("an untrusted suite is not a wrong suite -- there was no gate to be "
          "wrong",
          run_eval.wrong_suite_tasks(
              [_draw_row("t", False, True, False) for _ in range(3)]) == [],
          run_eval.wrong_suite_tasks(
              [_draw_row("t", False, True, False) for _ in range(3)]))
    check("a task whose draws fail the hidden suite is a model failure, not a "
          "suite failure",
          run_eval.wrong_suite_tasks(
              [_draw_row("t", True, False, False)] * 3) == [], "fired")
    check("one gate approval anywhere clears the suite",
          run_eval.wrong_suite_tasks(
              [_draw_row("t", True, True, False),
               _draw_row("t", True, True, True)]) == [], "fired")
    check("a single mixed draw is enough to withhold the flag",
          run_eval.wrong_suite_tasks(
              [_draw_row("t", True, True, False),
               _draw_row("t", True, False, False)]) == [], "fired")
    check("an ungraded draw is skipped rather than read as a failure to pass",
          run_eval.wrong_suite_tasks(
              [_draw_row("t", True, True, False),
               {"task_id": "t", "ok": False}]) == ["t"],
          run_eval.wrong_suite_tasks(
              [_draw_row("t", True, True, False),
               {"task_id": "t", "ok": False}]))
    check("a task with no graded draw at all cannot be flagged",
          run_eval.wrong_suite_tasks([{"task_id": "t", "ok": False}]) == [],
          "fired")

    mixed = fires + [_draw_row("aggregation-01", True, False, False)] * 3
    report = run_eval.wrong_suite_report(mixed)
    check("the report carries the denominators the count has to be read against",
          (report["count"], report["tasks_with_a_graded_draw"],
           report["graded_draws"]) == (1, 2, 6)
          and abs(report["rate_of_graded_tasks"] - 50.0) < 1e-9, report)
    check("and it names the flagged task", report["tasks"] == ["grouping-02"],
          report["tasks"])


def _pin_report_text(summary, total_units):
    pin = _import_pin_executor()
    with contextlib.redirect_stdout(io.StringIO()) as out:
        pin.print_report(summary, total_units)
    return out.getvalue()


def test_the_wrong_suite_count_runs_over_an_already_written_ledger():
    """It has to score today's probe, which was written before it existed.

    So: no new field on a draw, and the ledger's own records are the input. The
    synthetic ledger below is written to disk and read back through
    `pin.load_ledger` rather than passed in as dicts, because "runs over an
    existing ledger" is the requirement and an in-memory list would not test it.
    The live ledger is then scored too, read-only, when it is present.

    The two zero-cases are distinguished in the printed line: "no task had a wrong
    suite" and "nothing has been graded yet" are the same `0` and mean opposite
    things.
    """
    pin = _import_pin_executor()
    root = tempfile.mkdtemp(prefix="pin-wrong-suite-")
    path = pin.ledger_path(root)
    for task_id, passed in (("wrong-01", True), ("weak-01", False)):
        for candidate in pin.CANDIDATES:
            pin.append(path, {"kind": pin.KIND_DRAW, "task_id": task_id,
                              "candidate": candidate, "ok": True,
                              "tests_trusted": True, "tests_status": "generated",
                              "passed": passed, "gate_approved": False})
    for candidate in pin.CANDIDATES:
        pin.append(path, {"kind": pin.KIND_DRAW, "task_id": "untrusted-01",
                          "candidate": candidate, "ok": True,
                          "tests_trusted": False, "tests_status": "unusable",
                          "passed": True, "gate_approved": False})
    units, malformed = pin.load_ledger(path)
    wrong = pin.summarise(units)["wrong_suite"]
    check("a ledger on disk is scored without being re-run or re-graded",
          malformed == 0 and wrong["graded_draws"] == 9
          and wrong["tasks_with_a_graded_draw"] == 3, (malformed, wrong))
    check("the all-pass gate-rejected task is the only one flagged",
          wrong["tasks"] == ["wrong-01"], wrong["tasks"])

    text = _pin_report_text(pin.summarise(units),
                            {pin.KIND_PLAN: 3, pin.KIND_DRAW: 9})
    check("the count is printed, not left in the JSON",
          "wrong-suite: 1/3" in text and "wrong-01" in text, text[-1200:])
    check("the printed note says the suite is the thing at fault",
          "evidence about the SUITE" in text, text[-1200:])

    empty = pin.summarise({})
    check("an unscored ledger says so instead of printing a clean zero",
          empty["wrong_suite"]["graded_draws"] == 0
          and empty["wrong_suite"]["count"] == 0, empty["wrong_suite"])
    quiet = _pin_report_text(empty, {pin.KIND_PLAN: 0, pin.KIND_DRAW: 0})
    check("and the note about the suite is silent when nothing was flagged",
          "evidence about the SUITE" not in quiet
          and "wrong-suite:" not in quiet, quiet[-600:])

    live = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval",
                        "results", "pin-executor", "ledger.jsonl")
    if not os.path.exists(live):
        check("today's probe is not on this checkout, so the synthetic ledger "
              "above is the whole check", True)
        return
    units, _malformed = pin.load_ledger(live)
    wrong = pin.summarise(units)["wrong_suite"]
    check("today's probe scores as it stands, with nothing written back to it",
          wrong["graded_draws"] == 57
          and wrong["tasks_with_a_graded_draw"] == 19,
          wrong)
    check("grouping-02 is the task it finds in the live ledger",
          wrong["tasks"] == ["grouping-02"], wrong["tasks"])
    check("and aggregation-01 is not, because those three draws really did fail "
          "the hidden suite",
          "aggregation-01" not in wrong["tasks"], wrong["tasks"])


# --------------------- sprint 6, task 1: surviving rate limits honestly

class _Waits(object):
    """The retry loop's sleep, recording instead of sleeping.

    The waits are the thing under test, so they have to be observable -- and
    observing them by performing them would put 2 + 4 + 8 + 16 seconds of real
    sleep into an offline suite for one check.
    """

    def __init__(self):
        self.seconds = []

    def __call__(self, seconds):
        self.seconds.append(seconds)


class _FakeClock(object):
    """A monotonic clock a check can move, and a sleep that moves it."""

    def __init__(self, start=1000.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _HeaderExc(Exception):
    """An SDK-shaped exception: `.status_code` and `.response.headers`."""

    def __init__(self, headers, status=429):
        Exception.__init__(self, "%s slow down" % status)
        self.status_code = status
        self.response = type("_Resp", (object,), {})()
        self.response.headers = dict(headers)


def _failing_call(status, attempts=4, run_id="run-abc", retry_after=None,
                  measurement=True, exc_class="APIStatusError"):
    """Call the Planner against a provider that always fails with `status`.

    Returns ``(raised, call_records, waits)``. Every global this touches --
    the attempt bound, the backoff seed, the retry sleep, `call_model`, the call
    log, measurement mode -- is put back.
    """
    keys = {"gemini": "gkey-1234567890", "groq": "qkey-1234567890"}
    waits = _Waits()
    previous_attempts = agents_core.set_retry_attempts(attempts)
    previous_run_id = agents_core.set_backoff_run_id(run_id)
    previous_sleep = agents_core.set_retry_sleep(waits)
    original = agents_core.call_model
    raised = None

    def always_fails(provider, api_key, system, user, role=None):
        raise agents_core.ProviderError(
            "%s failed: %s from the provider" % (provider, status),
            status=status, exc_class=exc_class, retry_after=retry_after)

    agents_core.call_model = always_fails
    agents_core.reset_call_log()
    try:
        _with_measurement(measurement,
                          lambda: agents_core.call_role("planner", keys, "s", "u"))
    except agents_core.ProviderError as exc:
        raised = exc
    finally:
        agents_core.call_model = original
        agents_core.set_retry_attempts(previous_attempts)
        agents_core.set_backoff_run_id(previous_run_id)
        agents_core.set_retry_sleep(previous_sleep)
    records = list(agents_core.CALL_LOG)
    agents_core.reset_call_log()
    return raised, records, waits


def test_a_rate_limit_retries_to_the_bound_and_stays_infrastructure():
    raised, records, waits = _failing_call(429, attempts=4, run_id="run-429")
    schedule = agents_core.backoff_schedule("run-429", 4)
    check("a 429 still raises once the bound is reached", raised is not None)
    check("the raised error carries the status rather than flattening it",
          getattr(raised, "status", None) == 429, raised)
    check("one call record for the whole call, not one per attempt",
          len(records) == 1, records)
    record = records[0] if records else {}
    check("every attempt is counted on that one record",
          record.get("attempts") == 4, record.get("attempts"))
    check("the retries are counted separately from the attempts",
          record.get("retried") == 3, record.get("retried"))
    check("the waits are the run id's schedule, not fresh entropy",
          waits.seconds == schedule, (waits.seconds, schedule))
    check("the record accounts for the seconds spent waiting",
          record.get("seconds_backoff") == round(sum(waits.seconds), 3),
          record.get("seconds_backoff"))
    check("the retried status reaches the call record",
          record.get("status") == 429, record.get("status"))
    check("so does the exception class the SDK raised",
          record.get("exc_class") == "APIStatusError", record.get("exc_class"))
    check("a 429 is never recorded as a successful call",
          record.get("ok") is False, record.get("ok"))
    check("no key reaches the call record for a failed call",
          "gkey-1234567890" not in json.dumps(records), records)

    # A 500 is retried on the same schedule. The point of the status floor.
    _r, _rec, five = _failing_call(503, attempts=3, run_id="run-429")
    check("a 5xx retries too, on the same schedule",
          five.seconds == agents_core.backoff_schedule("run-429", 3),
          five.seconds)
    # An unclassified failure does not. A bug of ours must not look transient.
    _r2, statusless, none_waits = _failing_call(None, attempts=4)
    check("a failure with no status is not retried",
          all(r["attempts"] == 1 and r["retried"] == 0 for r in statusless)
          and none_waits.seconds == [],
          [(r["attempts"], r["retried"]) for r in statusless])


def test_the_servers_own_retry_after_beats_our_guess():
    _raised, _records, waits = _failing_call(429, attempts=3, run_id="run-ra",
                                             retry_after=1.5)
    check("the server's Retry-After is what gets waited",
          waits.seconds == [1.5, 1.5], waits.seconds)
    check("our own schedule is not used when the server sent a number",
          waits.seconds != agents_core.backoff_schedule("run-ra", 3),
          waits.seconds)
    check("a Retry-After header is read off an SDK-shaped exception",
          agents_core._retry_after_of(_HeaderExc({"retry-after": "7"})) == 7.0,
          agents_core._retry_after_of(_HeaderExc({"retry-after": "7"})))
    check("an absurd Retry-After is capped rather than believed",
          agents_core._retry_after_of(_HeaderExc({"retry-after": "3600"}))
          == agents_core.MAX_RETRY_AFTER_SECONDS)
    check("a junk Retry-After is ignored rather than crashing the call",
          agents_core._retry_after_of(_HeaderExc({"retry-after": "soon"}))
          is None)
    check("no header means no server number, so our schedule applies",
          agents_core._retry_after_of(_HeaderExc({})) is None)
    check("the status is read off the same exception shape",
          agents_core._status_of(_HeaderExc({}, status=503)) == 503)


def test_a_dead_slug_hard_fails_on_the_first_call_in_both_modes():
    """A wrong model slug must cost one call, not five and forty seconds.

    Checked in both modes because that is where it could differ: measurement mode
    attempts one provider, and outside it failover tries the other -- but neither
    may retry a 404.
    """
    for measurement in (True, False):
        label = "on" if measurement else "off"
        raised, records, waits = _failing_call(404, attempts=5,
                                               measurement=measurement)
        check("%s: a 404 raises" % label, raised is not None)
        check("%s: a 404 is not retried, on any provider it reached" % label,
              records and all(r["attempts"] == 1 and r["retried"] == 0
                              for r in records),
              [(r["used"], r["attempts"], r["retried"]) for r in records])
        check("%s: a 404 costs no sleep at all" % label,
              waits.seconds == [], waits.seconds)
        check("%s: the status survives to the caller" % label,
              getattr(raised, "status", None) == 404, raised)
        expected = ["gemini"] if measurement else ["gemini", "groq"]
        check("%s: providers attempted are %s" % (label, "+".join(expected)),
              [r["used"] for r in records] == expected,
              [r["used"] for r in records])
    check("every hard-fail status is one the retry rule refuses",
          not [s for s in agents_core.HARD_FAIL_STATUSES
               if agents_core.is_retryable_status(s)],
          agents_core.HARD_FAIL_STATUSES)


def test_the_backoff_schedule_is_reproducible_from_the_run_id():
    schedule = agents_core.backoff_schedule("run-a", 5)
    check("the schedule is one wait per retry, not one per attempt",
          len(schedule) == 4, schedule)
    check("the same run id gives the same schedule twice",
          schedule == agents_core.backoff_schedule("run-a", 5))
    check("a different run id gives a different schedule",
          schedule != agents_core.backoff_schedule("run-b", 5))
    check("the waits grow", schedule[0] < schedule[-1], schedule)
    ceiling = agents_core.BACKOFF_CAP_SECONDS * (1 + agents_core.BACKOFF_JITTER)
    long_run = agents_core.backoff_schedule("run-a", 12)
    check("every wait is positive and under the cap plus its jitter",
          all(0 < wait <= ceiling for wait in long_run),
          (min(long_run), max(long_run), ceiling))
    check("an unseeded process still has a reproducible schedule, not entropy",
          agents_core.backoff_schedule("unseeded", 3)
          == agents_core.backoff_schedule("unseeded", 3))
    check("a bound of one attempt means no waits at all",
          agents_core.backoff_schedule("run-a", 1) == [])

    previous = agents_core.set_backoff_run_id("run-snapshot")
    try:
        snapshot = agents_core.retry_snapshot()
        check("the manifest records the schedule the run would use",
              snapshot["schedule_seconds"] == agents_core.backoff_schedule(
                  "run-snapshot", snapshot["attempts"]), snapshot)
        check("the manifest records which run id seeded it",
              snapshot["backoff_run_id"] == "run-snapshot", snapshot)
        check("the manifest states whether the sleeps were real ones",
              snapshot["real_sleep"] is True, snapshot)
        check("the manifest states the retry rule, not just the bound",
              snapshot["retry_status"] == 429
              and snapshot["retry_status_floor"] == 500
              and 404 in snapshot["hard_fail_statuses"], snapshot)
    finally:
        agents_core.set_backoff_run_id(previous)
    check("setting the run id returns the previous one, so a runner restores "
          "rather than clears", agents_core.backoff_run_id() == previous)
    restore = agents_core.set_retry_attempts(0)
    try:
        check("a bound of zero is floored to one call, not silently no calls",
              agents_core.retry_attempts() == 1, agents_core.retry_attempts())
    finally:
        agents_core.set_retry_attempts(restore)


def test_the_pacer_delays_a_repeat_call_to_the_same_provider():
    check("the module ships with pacing disabled, so importing it costs no "
          "seconds and an eval that paces itself is not charged twice",
          agents_core.PACER.enabled is False, agents_core.PACER.snapshot())

    clock = _FakeClock()
    pacer = agents_core.Pacer(intervals={"gemini": 6.0, "groq": 2.0},
                              default=6.0, sleep=clock.sleep, clock=clock.now)
    check("the first call to a provider is not delayed",
          pacer.wait_for("gemini") == 0.0)
    check("an immediate second call to the same provider waits the interval",
          pacer.wait_for("gemini") == 6.0, clock.t)
    check("a different provider does not wait for it",
          pacer.wait_for("groq") == 0.0)
    clock.t += 1.0
    check("a partly elapsed interval waits only the remainder",
          pacer.wait_for("groq") == 1.0)
    clock.t += 99.0
    check("a long gap costs nothing", pacer.wait_for("gemini") == 0.0)
    check("an unknown provider gets the default interval",
          pacer.interval_for("mystery") == 6.0)
    check("the pacer reports what it slept, per provider",
          pacer.snapshot()["slept_seconds"] == {"gemini": 6.0, "groq": 1.0},
          pacer.snapshot())
    check("the clock is monotonic and not the wall clock: an NTP step must not "
          "park a run for hours or release a burst",
          agents_core.Pacer()._clock is time.monotonic)

    # And it is actually applied to a call, and charged to that call's record.
    stub = Stub({"planner": PLAN, "executor": GOOD_CODE})
    original = agents_core.call_model
    clock = _FakeClock()
    previous = agents_core.set_pacer(agents_core.Pacer(
        intervals={"gemini": 6.0, "groq": 2.0}, default=6.0,
        sleep=clock.sleep, clock=clock.now))
    try:
        agents_core.call_model = stub
        agents_core.reset_call_log()
        keys = {"gemini": "gkey-1234567890", "groq": "qkey-1234567890"}
        for _ in range(2):
            _with_measurement(True, lambda: agents_core.call_role(
                "planner", keys, "s", "u"))
        _with_measurement(True, lambda: agents_core.call_role(
            "executor", keys, "s", "u"))
        paced = [record["seconds_paced"] for record in agents_core.CALL_LOG]
        check("the first call is unpaced, the repeat pays, and the other "
              "provider does not", paced == [0.0, 6.0, 0.0], paced)
    finally:
        agents_core.call_model = original
        agents_core.set_pacer(previous)
        agents_core.reset_call_log()
    check("restoring the pacer leaves it disabled again",
          agents_core.PACER.enabled is False)


class _FakeInstrument(object):
    """What `run_one` reads off an instrument. It does not wrap `call_model` here.

    The real `Instrument` retries a 429 itself, with real `time.sleep`s of 2, 5,
    12 and 30 seconds. That is right in a sweep and wrong in an offline suite, so
    these checks drive `agents_core`'s retry layer directly and then assert on
    what `run_one` recorded.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = 0
        self.failed_calls = 0
        self.retries = 0
        self.providers = []
        self.slept = 0.0


def _run_one_against(raiser, arm="a", attempts=2):
    """`run_eval.run_one` with `call_model` replaced by `raiser`."""
    run_eval = _import_run_eval()
    keys = {"gemini": "gkey-1234567890", "groq": "qkey-1234567890"}
    waits = _Waits()
    previous_attempts = agents_core.set_retry_attempts(attempts)
    previous_sleep = agents_core.set_retry_sleep(waits)
    original = agents_core.call_model
    agents_core.call_model = raiser
    agents_core.reset_call_log()
    try:
        return _with_measurement(True, lambda: run_eval.run_one(
            _FakeTask(), arm, keys, _FakeInstrument(), 0)), waits
    finally:
        agents_core.call_model = original
        agents_core.set_retry_attempts(previous_attempts)
        agents_core.set_retry_sleep(previous_sleep)
        agents_core.reset_call_log()


def test_a_rate_limited_cell_is_never_a_model_failure():
    run_eval = _import_run_eval()

    def rate_limited(provider, api_key, system, user, role=None):
        raise agents_core.ProviderError("%s failed: 429" % provider, status=429,
                                        exc_class="RateLimitError",
                                        retry_after=0.0)

    record, _waits = _run_one_against(rate_limited)
    check("the cell is recorded as an infrastructure loss",
          record["outcome"] == run_eval.OUTCOME_INFRA_LOSS, record["outcome"])
    check("it carries no `passed` field at all -- not `passed: False`",
          "passed" not in record, sorted(record))
    check("and no `false_approved`, which a summariser would count",
          "false_approved" not in record, sorted(record))
    check("and nothing from the grader",
          not [name for name in record if name.startswith("grade_")],
          [name for name in record if name.startswith("grade_")])
    check("the verdict is UNVERIFIED, not a failing verdict",
          record["verdict"] == harness.VERDICT_UNVERIFIED, record["verdict"])
    check("the infra detail is named apart from the grading fields, so no "
          "summariser picks it up by a familiar key",
          "status" not in record and record["infra_status"] == 429,
          [name for name in sorted(record) if "status" in name])
    check("the attempt count reaches the record",
          record["infra_attempts"] == 2 and record["infra_retried"] == 1,
          (record["infra_attempts"], record["infra_retried"]))
    check("so does which provider and which role died",
          record["infra_provider"] == "groq"
          and record["infra_role"] == "executor",
          (record["infra_provider"], record["infra_role"]))
    check("the server's own wait is kept, not only ours",
          record["infra_retry_after"] == 0.0, record["infra_retry_after"])
    check("no key reaches an infra_loss record",
          "gkey-1234567890" not in json.dumps(record)
          and "qkey-1234567890" not in json.dumps(record))

    # Our own bug is the opposite case: still `passed: False`, still counted.
    def our_bug(provider, api_key, system, user, role=None):
        raise KeyError("a bug in this repository, not in a provider")

    broken, _w = _run_one_against(our_bug)
    check("a crash in our own code is not an infrastructure loss",
          broken["outcome"] == run_eval.OUTCOME_ERROR, broken["outcome"])
    check("our own crash keeps `passed: False`, so a bug of ours cannot quietly "
          "shrink the denominator", broken["passed"] is False,
          broken.get("passed"))
    check("our own crash keeps a traceback", bool(broken.get("traceback")))
    check("the three outcomes are three distinct values",
          len(set((run_eval.OUTCOME_GRADED, run_eval.OUTCOME_INFRA_LOSS,
                   run_eval.OUTCOME_ERROR))) == 3)


def test_an_infra_loss_excludes_its_whole_task_and_is_counted():
    run_eval = _import_run_eval()
    lost = _summary_record("b", True, task_id="task-lost",
                           outcome=run_eval.OUTCOME_INFRA_LOSS)
    # A real infra_loss record has neither key. Mirrored here so the fixture
    # cannot pass a check the real record would fail.
    del lost["passed"]
    del lost["false_approved"]
    lost["infra_status"] = 429
    records = [lost]
    for task_id in ("task-lost", "task-kept"):
        for arm in ("a", "a_prime", "a_prime3", "b"):
            if task_id == "task-lost" and arm == "b":
                continue
            records.append(_summary_record(arm, True, passed=True,
                                           task_id=task_id,
                                           outcome=run_eval.OUTCOME_GRADED))
    summary = run_eval.summarise(records)
    infra = summary["infra_loss"]
    check("the whole task leaves, not only the cell that died",
          infra["tasks_excluded"] == 1
          and infra["task_ids_excluded"] == ["task-lost"], infra)
    check("every cell of that task leaves, the three that answered included",
          infra["cells_dropped_with_them"] == 4, infra)
    check("the loss is attributed to an arm and a status",
          infra["by_arm"] == {"b": 1} and infra["by_status"] == {"429": 1},
          infra)
    sizes = set(summary["by_arm"][arm]["pooled_not_a_result"]["n"]
                for arm in ("a", "a_prime", "a_prime3", "b"))
    check("every arm is left with the same tasks, so the exclusion cannot "
          "reweight the task set toward the arms that survived",
          sizes == set([1]), sizes)
    check("the arm that lost a cell is not penalised for it",
          summary["by_arm"]["b"]["pooled_not_a_result"]["pass_rate"] == 100.0,
          summary["by_arm"]["b"]["pooled_not_a_result"])

    text = _summary_text(records)
    check("the count is printed",
          "infra loss: 1 cell(s), 1 task(s) excluded, 4 cell(s) dropped" in text,
          text)
    check("the printed count says which arm lost it",
          "(by arm: b=1)" in text, text)
    check("a non-zero count prints the explanation",
          "TASK granularity" in text and "task_ids_excluded" in text, text)

    clean = _summary_text([_summary_record("a", True, passed=True),
                           _summary_record("b", True, passed=True)])
    check("the line prints when the count is zero too: a silent zero and a "
          "silent thirty read identically",
          "infra loss: 0 cell(s), 0 task(s) excluded, 0 cell(s) dropped" in clean,
          clean)
    check("but the explanation stays quiet when nothing was lost",
          run_eval.INFRA_LOSS_NOTE not in clean, clean)


def _capture(thunk):
    buffer, original = io.StringIO(), sys.stdout
    sys.stdout = buffer
    try:
        result = thunk()
    finally:
        sys.stdout = original
    return result, buffer.getvalue()


def _read_events(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _read_summary(out, seed=0):
    with open(os.path.join(out, "seed-%d" % seed, "summary.json")) as handle:
        return json.load(handle)


def _sweep(out, extra=(), streams=False):
    """One offline `run_eval.main`, with every global it installs put back.

    `streams=True` captures stderr as well, which is where a refusal goes: a
    check that only reads stdout would pass on a refusal whose message never
    named anything.
    """
    run_eval = _import_run_eval()
    argv = ["--stub", "perfect", "--arm", "a", "--limit", "1", "--out", out]
    previous_mode = agents_core.MEASUREMENT_MODE
    previous_attempts = agents_core.retry_attempts()
    capture = _capture_streams if streams else _capture
    try:
        return capture(lambda: run_eval.main(argv + list(extra)))
    finally:
        agents_core.set_measurement_mode(previous_mode)
        agents_core.set_retry_attempts(previous_attempts)
        agents_core.reset_call_log()


def test_a_resumed_cell_makes_no_calls_and_records_why():
    run_eval = _import_run_eval()
    out = tempfile.mkdtemp(prefix="resume-")
    try:
        code, text = _sweep(out)
        path = os.path.join(out, "seed-0", "events.jsonl")
        first = _read_events(path)
        check("an offline sweep runs", code == 0, text[-400:])
        check("a fresh cell makes its provider calls",
              len([e for e in first if e["kind"] == "call"]) == 1,
              [e["kind"] for e in first])
        check("nothing is skipped on a fresh sweep",
              not [e for e in first if e["kind"] == "resume_skip"])
        check("every event names the task, arm and repeat it came from",
              bool(first) and all(e.get("task_id") and e.get("arm") == "a"
                                  and e.get("repeat") == 0
                                  and e.get("seed") == 0 for e in first),
              first)
        check("every event is one JSON object per line with a kind from the "
              "closed vocabulary",
              bool(first) and all(e["kind"] in agents_core.EVENT_KINDS
                                  for e in first),
              [e["kind"] for e in first])
        check("no key and no prompt or output body reaches the event log",
              "gkey" not in json.dumps(first)
              and not [name for e in first for name in e
                       if name in ("system", "user", "prompt", "code",
                                   "stdout", "stderr", "tests")],
              [name for e in first for name in e])

        # Read before the second sweep overwrites it: `events.count` is per
        # sweep, and the resumed sweep's manifest reports its own one event.
        summary = _read_summary(out)
        pacing = summary["pacing"]
        check("the manifest states the pacing the sweep ran under",
              pacing["governor_rates_per_min"] == run_eval.DEFAULT_RATES,
              pacing["governor_rates_per_min"])
        check("the manifest states which retry layer was in force, so the two "
              "bounds cannot silently multiply",
              pacing["agents_core_retry"]["attempts"] == 1
              and pacing["governor_max_429_retries"] == run_eval.MAX_429_RETRIES,
              pacing)
        check("the manifest states that agents_core's own pacer was off, so no "
              "wait was charged twice",
              pacing["agents_core_pacer"]["enabled"] is False,
              pacing["agents_core_pacer"])
        check("the manifest states the retry rule and the backoff constants",
              pacing["agents_core_retry"]["hard_fail_statuses"]
              == list(agents_core.HARD_FAIL_STATUSES)
              and pacing["agents_core_retry"]["backoff_cap_seconds"]
              == agents_core.BACKOFF_CAP_SECONDS,
              pacing["agents_core_retry"])
        check("the manifest says whether the waits it reports were really taken",
              pacing["agents_core_retry"]["real_sleep"] is True,
              pacing["agents_core_retry"].get("real_sleep"))
        check("the manifest points at the event log and says how much reached it",
              summary["events"]["path"] == "events.jsonl"
              and summary["events"]["count"] == len(first)
              and summary["events"]["write_failures"] == 0, summary["events"])
        check("the infra-loss block is present even on a clean sweep",
              summary["infra_loss"]["cells"] == 0, summary["infra_loss"])

        code2, text2 = _sweep(out)
        new = _read_events(path)[len(first):]
        check("resuming makes no provider call at all",
              code2 == 0 and "0 run(s)" in text2, text2[-400:])
        check("a resumed cell emits exactly one resume_skip and nothing else",
              [e["kind"] for e in new] == ["resume_skip"],
              [e["kind"] for e in new])
        check("the skip names the cell it skipped",
              bool(new) and new[0]["arm"] == "a" and new[0]["repeat"] == 0
              and new[0]["result"].endswith("__r0.json"), new)
        check("the skip records no absolute path, which would carry the user's "
              "home directory", bool(new) and "/" not in new[0]["result"], new)
        check("the resumed sweep's manifest reports its own event count, so a "
              "resumed run cannot claim the work the first one did",
              _read_summary(out)["events"]["count"] == 1,
              _read_summary(out)["events"])
        check("the log is append-only: the first sweep's events are still there, "
              "byte for byte", _read_events(path)[:len(first)] == first)
    finally:
        shutil.rmtree(out, ignore_errors=True)
    check("a sweep leaves no event log installed behind it",
          agents_core.event_log() is None)


# ---------------------------------------------------------------------------
# sprint 6, task 2: freeze the hidden suite and prove it never reaches a prompt
# ---------------------------------------------------------------------------

def _import_gen_tasks():
    _import_run_eval()          # puts `eval/` on sys.path
    import gen_tasks
    return gen_tasks


def _import_audit_leakage():
    _import_run_eval()
    import audit_leakage
    return audit_leakage


def _task_attr_readers(path, attrs):
    """Every function in `path` that reads one of `attrs` off a *task*.

    `scratch.tests` and `run.tests` are the visible, model-written suite and are
    not the secret; `task.tests` and `self.task.reference` are. The distinction is
    structural, so it is made structurally: only attributes rooted at a `task`
    name are reported.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = []

    def rooted_at_task(node):
        value = node.value
        if isinstance(value, ast.Name):
            return value.id == "task"
        return isinstance(value, ast.Attribute) and value.attr == "task"

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            name = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                name = "%s.%s" % (scope, child.name) if scope else child.name
            if (isinstance(child, ast.Attribute) and child.attr in attrs
                    and rooted_at_task(child)):
                found.append((name, child.attr, child.lineno))
            walk(child, name)

    walk(tree, "")
    return found


def test_the_task_lock_pins_the_hidden_suite():
    """The lock agrees with the tree it was written from, in both legs."""
    gen_tasks = _import_gen_tasks()
    lock = gen_tasks.load_lock()
    check("eval/tasks.lock exists and loads", isinstance(lock, dict), type(lock))
    check("the lock states the version this code writes",
          lock["version"] == gen_tasks.LOCK_VERSION, lock.get("version"))
    check("the lock records a passing self-check, dated",
          lock["self_check"]["passed"] and lock["self_check"]["at"],
          lock.get("self_check"))
    failures = gen_tasks.verify_lock(lock)
    check("--verify-lock passes on an untouched tree", failures == [], failures)

    tasks = gen_tasks.generate_from(lock["generator"])
    check("the lock covers every task the generator produces",
          lock["counts"]["tasks"] == len(tasks) == len(lock["tasks"]),
          (lock["counts"]["tasks"], len(tasks), len(lock["tasks"])))
    check("three digests per task, so a moved prompt, suite or reference are "
          "distinguishable",
          all(sorted(k for k in entry if k.endswith("_sha256"))
              == ["prompt_sha256", "reference_sha256", "tests_sha256"]
              for entry in lock["tasks"]))
    check("every digest is a full sha256, not a short id",
          all(len(entry["tests_sha256"]) == 64 for entry in lock["tasks"]))
    check("the run-selection leg passes for the seed the lock was written for",
          gen_tasks.verify_lock(lock, tasks=tasks, seed=0) == [], "")

    # A run that selected a *subset* is fine -- `--limit 3` is a legitimate way to
    # run -- and one that selected a task the lock never saw is not.
    check("a subset of the locked tasks verifies",
          gen_tasks.verify_lock(lock, tasks=tasks[:3], seed=0) == [], "")
    stranger = copy.copy(tasks[0])
    stranger.task_id = "invented-99"
    failures = gen_tasks.verify_lock(lock, tasks=[stranger], seed=0)
    check("a task the lock never saw is refused and named",
          any("invented-99" in line and "does not contain" in line
              for line in failures), failures)


def test_a_moved_hidden_suite_is_named_by_task_and_part():
    """One character of one suite, and both legs say which task and which part."""
    gen_tasks = _import_gen_tasks()
    lock = gen_tasks.load_lock()
    tasks = gen_tasks.generate_from(lock["generator"])
    victim = tasks[0].task_id

    moved = [copy.copy(task) for task in tasks]
    moved[0].tests = moved[0].tests + "\nassert True  # one line, added later\n"
    failures = gen_tasks.verify_lock(lock, tasks=moved, seed=0)
    check("a suite that moved under a run is caught",
          any(victim in line and "tests digest moved" in line
              for line in failures), failures)
    check("and only the part that moved is named: the prompt and the reference "
          "are not implicated",
          not any("prompt digest" in line or "reference digest" in line
                  for line in failures), failures)

    # The other leg: the generator itself changed. This is the case that
    # invalidates a half-finished grid, and it is caught without a run at all.
    original = gen_tasks.generate
    gen_tasks.generate = lambda **kwargs: moved
    try:
        blind = gen_tasks.verify_lock(lock)
    finally:
        gen_tasks.generate = original
    check("a changed generator is caught by regenerating from the lock's own "
          "generator block, with no run involved",
          any(victim in line and "tests digest moved" in line
              and "gen_tasks.py" in line for line in blind), blind)

    reworded = [copy.copy(task) for task in tasks]
    reworded[0].prompt = reworded[0].prompt + " Also, be careful."
    failures = gen_tasks.verify_lock(lock, tasks=reworded, seed=0)
    check("a reworded prompt is caught too -- the lock pins the task, not just "
          "its suite",
          any(victim in line and "prompt digest moved" in line
              for line in failures), failures)


def test_a_hand_edited_lock_is_caught_two_ways():
    """The lazy forgery is caught by the self-digest, the careful one by the tree.

    Both halves matter. Somebody who changes a suite and then updates the lock's
    digest to match has produced a lock that agrees with itself; only regenerating
    from the generator catches that, and only the self-digest catches an edit made
    without regenerating.
    """
    gen_tasks = _import_gen_tasks()
    lock = gen_tasks.load_lock()

    lazy = copy.deepcopy(lock)
    lazy["tasks"][0]["tests_sha256"] = "0" * 64
    failures = gen_tasks.verify_lock(lazy)
    check("a lock edited by hand is caught by its own body_sha256",
          any("edited by hand" in line and "body_sha256" in line
              for line in failures), failures)

    careful = copy.deepcopy(lock)
    careful["tasks"][0]["tests_sha256"] = "0" * 64
    careful["body_sha256"] = gen_tasks.body_digest(careful)
    failures = gen_tasks.verify_lock(careful)
    check("a self-consistent forgery passes the self-digest check",
          not any("edited by hand" in line for line in failures), failures)
    check("but the regeneration leg still catches it, naming the task and the "
          "part", any(careful["tasks"][0]["task_id"] in line
                      and "tests digest moved" in line for line in failures),
          failures)

    claimed = copy.deepcopy(lock)
    claimed["self_check"]["passed"] = False
    claimed["body_sha256"] = gen_tasks.body_digest(claimed)
    failures = gen_tasks.verify_lock(claimed)
    check("a lock that records no passing self-check is refused: it would "
          "otherwise vouch for suites never shown to grade anything",
          any("no passing --self-check" in line for line in failures), failures)

    stale = copy.deepcopy(lock)
    stale["version"] = gen_tasks.LOCK_VERSION + 1
    stale["body_sha256"] = gen_tasks.body_digest(stale)
    failures = gen_tasks.verify_lock(stale)
    check("a lock from a future format is refused rather than half-read",
          len(failures) == 1 and "version" in failures[0], failures)


def _capture_streams(thunk):
    """`(result, stdout+stderr)`. A refusal is printed to stderr, on purpose."""
    out, err = io.StringIO(), io.StringIO()
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        result = thunk()
    finally:
        sys.stdout, sys.stderr = original_out, original_err
    return result, out.getvalue() + err.getvalue()


def _lock_scoped(path, thunk):
    """Run `thunk` with `gen_tasks.LOCK_PATH` pointed somewhere else."""
    gen_tasks = _import_gen_tasks()
    original = gen_tasks.LOCK_PATH
    gen_tasks.LOCK_PATH = path
    try:
        return thunk()
    finally:
        gen_tasks.LOCK_PATH = original


def test_a_run_refuses_to_start_without_a_verified_lock():
    """No lock, a moved lock, or the wrong seed: refuse before spending a call.

    Refusing rather than warning. A warning at the top of a sweep that then prints
    two hundred result lines is a warning nobody reads, and by then the results are
    already pooled across two different task sets.
    """
    run_eval = _import_run_eval()
    gen_tasks = _import_gen_tasks()
    out = tempfile.mkdtemp(prefix="lock-refuse-")
    previous_mode = agents_core.MEASUREMENT_MODE
    previous_attempts = agents_core.retry_attempts()
    argv = ["--stub", "perfect", "--arm", "a", "--limit", "1", "--out", out]
    try:
        code, text = _lock_scoped(
            os.path.join(out, "absent.lock"),
            lambda: _capture_streams(lambda: run_eval.main(argv)))
        check("a run with no tasks.lock refuses", code == 2, code)
        check("and says what to do about it",
              "refusing to run" in text and "--write-lock" in text, text[-300:])
        check("nothing was written, so a refusal cannot be mistaken for a sweep "
              "that produced no results", not os.path.exists(
                  os.path.join(out, "seed-0")), os.listdir(out))

        moved = os.path.join(out, "moved.lock")
        body = copy.deepcopy(gen_tasks.load_lock())
        body["tasks"][0]["prompt_sha256"] = "1" * 64
        body["body_sha256"] = gen_tasks.body_digest(body)
        with open(moved, "w") as handle:
            json.dump(body, handle)
        code, text = _lock_scoped(
            moved, lambda: _capture_streams(lambda: run_eval.main(argv)))
        check("a run whose task set does not match the lock refuses", code == 2,
              code)
        check("and names the task and the part that moved",
              body["tasks"][0]["task_id"] in text and "prompt digest moved"
              in text, text[-400:])

        code, text = _capture_streams(
            lambda: run_eval.main(argv + ["--seed", "1"]))
        check("a run on a seed the lock was not written for refuses",
              code == 2, code)
        check("and says the task ids are the same while the contents are not",
              "seed 1" in text and "seed 0" in text, text[-400:])
    finally:
        agents_core.set_measurement_mode(previous_mode)
        agents_core.set_retry_attempts(previous_attempts)
        shutil.rmtree(out, ignore_errors=True)


def test_writing_a_lock_requires_a_passing_self_check():
    """A lock over a task set that cannot grade anything is worse than no lock.

    Everything downstream would verify happily against it, so the self-check is
    part of writing the lock rather than something somebody ran once.
    """
    gen_tasks = _import_gen_tasks()
    out = tempfile.mkdtemp(prefix="lock-write-")
    path = os.path.join(out, "tasks.lock")
    try:
        written, failures = gen_tasks.write_lock(path=path, limit=1)
        check("--write-lock writes when the self-check passes",
              failures == [] and os.path.exists(written), (failures, written))
        fresh = gen_tasks.load_lock(path)
        check("the lock it wrote verifies", gen_tasks.verify_lock(fresh) == [],
              gen_tasks.verify_lock(fresh))
        check("and records its own selection, not just the seed",
              fresh["generator"]["limit"] == 1
              and fresh["generator"]["per_family"] == 2,
              fresh["generator"])
        check("the self-check result is dated and counted",
              fresh["self_check"]["tasks"] == fresh["counts"]["tasks"],
              fresh["self_check"])

        os.remove(path)
        original = gen_tasks.self_check
        gen_tasks.self_check = lambda tasks, verbose=False: ["a suite is vacuous"]
        try:
            written, failures = gen_tasks.write_lock(path=path, limit=1)
        finally:
            gen_tasks.self_check = original
        check("a failing self-check refuses to write a lock",
              failures == ["a suite is vacuous"], failures)
        check("and leaves no file behind to verify against",
              not os.path.exists(path))
    finally:
        shutil.rmtree(out, ignore_errors=True)


# Every task whose battery verdict was established by hand, with the verdict it has
# to keep. Named rather than counted: a regression has to read as "validation-01
# went back to passing a wrong implementation", not as a total that moved by one.
# The last three are the cases where survival was *not* a suite defect, and they are
# in the same table because "we stopped requiring this one" is the other way the
# guarantee quietly weakens.
BATTERY_FINDINGS = (
    ("validation-01", "or_to_and", "caught",
     "every prefix case carried a wrong check digit too, so the suite could not "
     "tell a rejected prefix from a rejected checksum"),
    ("validation-02", "or_to_and", "caught", "as validation-01"),
    ("version-ordering-01", "or_to_and", "caught",
     "`None` was the only non-str tried and it is falsy, so `or` and `and` "
     "rejected it alike"),
    ("version-ordering-02", "or_to_and", "caught", "as version-ordering-01"),
    ("template-expansion-02", "raise_to_pass", "caught",
     "every unclosed-`{` case raised ValueError from another branch as well"),
    ("path-canonicalization-02", "raise_to_pass", "unreachable",
     "escape='clamp' makes its only `raise` dead code, so the edit cannot change "
     "an answer and no assertion can catch it"),
    ("tiered-pricing-01", "le_to_lt", "equivalent",
     "the guarded bracket contributes exactly 0 at units == previous"),
    ("tiered-pricing-02", "le_to_lt", "equivalent", "as tiered-pricing-01"),
)


def _blind_task(gen_tasks):
    """A real-looking task whose suite cannot see one battery mutation.

    The suite is honest -- it imports, it calls, it asserts real values, and the
    vacuous stub fails it -- so the leg the self-check used to have would call it
    sound. It just never passes a value that separates `or` from `and`, which is
    exactly the shape `validation-01` had.
    """
    return gen_tasks.Task(
        family="blind", tier=1, variant=0, task_id="blind-01",
        prompt="Write `classify(value)`.\n", names=("classify",),
        reference=("def classify(value):\n"
                   "    if not isinstance(value, str) or not value:\n"
                   "        return 'bad'\n"
                   "    return 'ok'\n"),
        tests=("from solution import classify\n"
               "\n"
               "assert classify('x') == 'ok', \"classify('x')\"\n"
               "assert classify('yz') == 'ok', \"classify('yz')\"\n"
               "assert classify('abc') == 'ok', \"classify('abc')\"\n"
               "assert classify('d') == 'ok', \"classify('d')\"\n"),
        params={}, seed=0)


def test_the_self_check_runs_a_battery_not_one_stub():
    """The lock's claim has to be as strong as the strongest stub in the tree.

    `--self-check` used to try one vacuous stub, which every suite catches, and
    `--write-lock` recorded "passed" with no record of what was tried. Meanwhile
    `calibrate.py --stub broken` handed `validation-01` and `validation-02` a wrong
    implementation their own suites passed. Both statements were true at once, which
    is why the words the lock recorded were the defect: "fails a stub" carried the
    strength of the stub that ran, and nobody could read that strength off the lock.
    """
    gen_tasks = _import_gen_tasks()
    blind = _blind_task(gen_tasks)

    # The old leg on its own would pass this suite: the vacuous stub fails it.
    try:
        gen_tasks.run_suite(gen_tasks._stub(blind.names), blind.tests)
    except Exception:
        vacuous_caught = True
    else:
        vacuous_caught = False
    check("a suite blind to one mutation still catches the vacuous stub, so the "
          "old single-stub leg would have called it sound", vacuous_caught)

    failures = gen_tasks.self_check([blind])
    check("the battery fails a suite it cannot break", len(failures) == 1,
          failures)
    check("and names the task and the mutation that survived",
          failures and "blind-01" in failures[0] and "or_to_and" in failures[0],
          failures)

    verdicts = dict((name, verdict)
                    for name, verdict, _why in gen_tasks.battery_verdicts(blind))
    check("the surviving mutation is reported as survived and the rest are not",
          verdicts.get("or_to_and") == "survived"
          and verdicts.get("vacuous_stub") == "caught", verdicts)

    check("the battery is exactly the vacuous stub plus the broken stub's own "
          "edits, so the two cannot drift apart",
          gen_tasks.BATTERY == (gen_tasks.VACUOUS_STUB,)
          + tuple(name for name, _f, _r in gen_tasks.STUB_EDITS),
          gen_tasks.BATTERY)
    run_eval = _import_run_eval()
    check("and StubModel.MUTATIONS is derived from that same list",
          run_eval.StubModel.MUTATIONS
          == tuple((find, replace)
                   for _n, find, replace in gen_tasks.STUB_EDITS),
          run_eval.StubModel.MUTATIONS)

    # Three mutations in the current set turn a terminating loop into a
    # non-terminating one, which the vacuous stub never could. Without a stop the
    # battery would hang `--self-check` and `--write-lock` rather than fail them.
    runaway = ("def spin(n):\n"
               "    total = 0\n"
               "    while True:\n"
               "        total += n\n"
               "    return total\n")
    started = time.time()
    rejected, why = gen_tasks.suite_rejects(
        runaway, "from solution import spin\nassert spin(1) == 1, 'spin(1)'\n")
    elapsed = time.time() - started
    check("a mutation that stops terminating is rejected rather than hung on",
          rejected and why == "stopped terminating", (rejected, why))
    check("and the stop is a line-event budget, so the verdict the lock records "
          "does not depend on how busy the machine was",
          gen_tasks.RUNAWAY_BUDGET > 0 and elapsed < 30, (elapsed,))
    check("the budget clears the worst legitimate suite by at least 20x: every "
          "reference still passes its own suite at a twentieth of it",
          [task.task_id for task in gen_tasks.generate()
           if gen_tasks.suite_rejects(task.reference, task.tests,
                                      gen_tasks.RUNAWAY_BUDGET // 20)[0]] == [],
          gen_tasks.RUNAWAY_BUDGET)


def test_the_lock_records_the_battery_and_a_moved_battery_voids_it():
    """A guarantee is worth what the thing that produced it is worth.

    Every digest can match while the claim behind them has weakened, because "the
    suites fail a wrong implementation" means whatever the battery that ran means.
    So the battery goes into the lock by name and `--verify-lock` compares it, which
    is the leg that would have caught the old lock: it recorded a self-check that had
    tried one vacuous stub, in words that sounded like it had tried everything.
    """
    gen_tasks = _import_gen_tasks()
    out = tempfile.mkdtemp(prefix="lock-battery-")
    path = os.path.join(out, "tasks.lock")
    try:
        _written, failures = gen_tasks.write_lock(path=path, limit=2)
        check("--write-lock still writes", failures == [], failures)
        fresh = gen_tasks.load_lock(path)
        recorded = fresh["self_check"].get("battery")
        check("the lock records the battery it ran, by name",
              recorded == gen_tasks.battery_record(), recorded)
        check("including the runaway budget and every documented equivalent "
              "mutant, so what was skipped is on the record too",
              recorded["runaway_budget"] == gen_tasks.RUNAWAY_BUDGET
              and recorded["equivalent_mutants"]
              == sorted("%s:%s" % key for key in gen_tasks.EQUIVALENT_MUTANTS),
              recorded)

        weakened = json.loads(json.dumps(fresh))
        weakened["self_check"]["battery"]["mutations"] = [gen_tasks.VACUOUS_STUB]
        weakened["body_sha256"] = gen_tasks.body_digest(weakened)
        problems = gen_tasks.verify_lock(weakened)
        check("a lock recording a different battery fails --verify-lock even with "
              "every digest matching and the body hash rewritten to agree",
              any("battery" in line for line in problems), problems)
        check("and the old lock's shape -- a self-check with no battery at all -- "
              "fails the same way",
              any("battery" in line for line in gen_tasks.verify_lock(
                  dict(weakened, self_check={"passed": True, "at": "x"},
                       body_sha256=gen_tasks.body_digest(
                           dict(weakened,
                                self_check={"passed": True, "at": "x"}))))))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_every_battery_finding_holds_by_name_and_no_suite_passes_the_broken_stub():
    """The findings, per task, so a regression reads as a name and not as a total.

    Three of these are the suites the battery caught being toothless and three are
    the ones where survival was *not* a defect. Both directions are asserted: a suite
    that goes back to passing a wrong implementation has to fail here by name, and so
    does an exemption that quietly grows to cover a real defect.
    """
    gen_tasks = _import_gen_tasks()
    run_eval = _import_run_eval()
    tasks = dict((task.task_id, task) for task in gen_tasks.generate())
    verdicts = {}
    for task_id, _mutation, _want, _why in BATTERY_FINDINGS:
        if task_id not in verdicts:
            verdicts[task_id] = dict(
                (name, verdict)
                for name, verdict, _detail
                in gen_tasks.battery_verdicts(tasks[task_id]))
    for task_id, mutation, want, why in BATTERY_FINDINGS:
        check("%s still reports %s for the %s mutant (%s)"
              % (task_id, want, mutation, why),
              verdicts[task_id].get(mutation) == want,
              verdicts[task_id].get(mutation))

    # `equivalent` is the one verdict that is asserted rather than computed, so it is
    # the one that has to be re-earned rather than trusted. `charge` takes a single
    # int, so the exemption is a decidable claim over the whole legal domain and not
    # an argument: sweep it.
    for task_id in ("tiered-pricing-01", "tiered-pricing-02"):
        task = tasks[task_id]
        mutant, _lines = gen_tasks._mutate(task.reference, "le_to_lt")
        top = max(limit for limit, _rate in task.params["brackets"]) + 200
        pair = []
        for source in (task.reference, mutant):
            box = {}
            exec(compile(source, "<solution>", "exec"), box)
            pair.append([box["charge"](units) for units in range(top + 1)])
        disagree = [units for units, (was, now) in enumerate(zip(*pair))
                    if was != now]
        check("%s's le_to_lt exemption is re-earned by sweep, not asserted: the "
              "mutant agrees with the reference on every legal input 0..%d"
              % (task_id, top), disagree == [], disagree[:5])

    # The whole point of the battery. `--stub broken` is the strongest wrong
    # implementation this tree can produce, and three suites used to pass it.
    stub = run_eval.StubModel(quality="broken")
    passes = []
    for task in tasks.values():
        stub.task = task
        if not gen_tasks.suite_rejects(stub._wrong(), task.tests)[0]:
            passes.append(task.task_id)
    check("no locked suite passes `--stub broken`'s wrong implementation, which is "
          "the claim tasks.lock now records", passes == [], passes)

    survivors = [(task.task_id, name)
                 for task in tasks.values()
                 for name, verdict, _d in gen_tasks.battery_verdicts(task)
                 if verdict == "survived"]
    check("and no suite in the locked set survives any applicable mutation in the "
          "battery", survivors == [], survivors)


# ---------------------------------------------------------------------------
# sprint 9, task 1: the per-check report, end to end through the real emitter.
#
# `read_checks`'s side of this is covered in test_harness.py against synthetic
# stdout. What has to be established here is the other half: that the suites
# `_bake` actually emits produce those reports, that the import line stayed
# outside the wrappers, and that a partial count cannot reach a verdict.
# ---------------------------------------------------------------------------

_PARTIAL_REFERENCE = (
    "def f(n):\n"
    "    if n < 0:\n"
    "        raise ValueError('negative')\n"
    "    return n + 1\n"
)
# One check of each outcome the ranking is supposed to be able to tell apart:
# a pass, a non-assertion exception, a wrong value, and a `raises` case that
# raised the wrong thing.
_PARTIAL_SOLUTION = (
    "def f(n):\n"
    "    if n < 0:\n"
    "        raise KeyError('wrong exception')\n"
    "    if n == 1:\n"
    "        return 2\n"
    "    if n == 2:\n"
    "        return n + 'x'\n"
    "    return 99\n"
)
# Correct on every value case, and simply fails to raise on the `raises` one.
_NO_RAISE_SOLUTION = (
    "def f(n):\n"
    "    return abs(n) + 1\n"
)


def _bake_partial(gen_tasks):
    """A four-check suite in the shipped shape: three value cases and one `raises`."""
    return gen_tasks._bake(_PARTIAL_REFERENCE, ("f",),
                           ("f(1)", "f(2)", "f(3)"),
                           [("f(-1)", ValueError)])


def _report_for(gen_tasks, solution, tests):
    """`read_checks` applied to what the suite printed, however the suite ended.

    `run_suite` re-raises the first failure, so the interesting cases -- every
    failing candidate -- only have a report if it is read off the buffer rather
    than off the return value.
    """
    out = io.StringIO()
    try:
        gen_tasks.run_suite(solution, tests, out=out)
    except BaseException:
        pass
    return harness.read_checks(out.getvalue())


def test_each_check_reports_its_own_outcome_and_its_own_exception_type():
    """One line per check, and the type recorded, from a suite `_bake` really wrote.

    The four checks fail four different ways on purpose. A ranking that reads
    "three checks failed" the same regardless of *how* is the thing the sprint
    was meant to end, so the kinds are asserted individually and by position:
    a NameError on check three is not a wrong return value.

    The `raises` case gets the same treatment as the value cases and both of its
    failure shapes are here -- wrong exception (`KeyError`, recorded by type) and
    no exception at all (`AssertionError`, from the `else` the generator emits).
    """
    gen_tasks = _import_gen_tasks()
    tests = _bake_partial(gen_tasks)

    passed, total, kinds, note = _report_for(gen_tasks, _PARTIAL_REFERENCE, tests)
    check("the reference passes every check and the report is coherent",
          (passed, total, note) == (4, 4, "") and kinds == ["-"] * 4,
          (passed, total, kinds, note))

    passed, total, kinds, note = _report_for(gen_tasks, _PARTIAL_SOLUTION, tests)
    check("a partly-right solution is counted per check, with each failure's own "
          "exception type, and an assertion is not conflated with a TypeError",
          (passed, total, note) == (1, 4, "")
          and kinds == ["-", "TypeError", "AssertionError", "KeyError"],
          (passed, total, kinds, note))

    passed, total, kinds, note = _report_for(gen_tasks, _NO_RAISE_SOLUTION, tests)
    check("a `raises` case that does not raise is one failed check and not a "
          "whole-suite loss",
          (passed, total, note) == (3, 4, "")
          and kinds == ["-", "-", "-", "AssertionError"],
          (passed, total, kinds, note))

    stub = gen_tasks._stub(("f",))
    passed, total, _kinds, note = _report_for(gen_tasks, stub, tests)
    check("and the vacuous stub is a measured zero of four, which is a different "
          "reading from an absent report",
          (passed, total, note) == (0, 4, ""), (passed, total, note))


def test_a_missing_entry_point_is_not_zero_of_n():
    """An import failure has to stay a whole-suite failure, structurally and in fact.

    This is the one way partial credit could have become dangerous: if the import
    moved inside a wrapper, a solution that defines none of the required names
    would score "0 of N passed" -- a measured floor -- instead of "no suite ran".
    The floor and the absence rank differently on purpose, and `_candidate_checks`
    returns -1 for the absence so it sorts below a measured zero.

    Checked both ways round, because either alone is weak: the behaviour on a
    solution missing its entry point, and the shape of all 36 frozen suites.
    """
    gen_tasks = _import_gen_tasks()
    tests = _bake_partial(gen_tasks)

    raised = []
    out = io.StringIO()
    try:
        gen_tasks.run_suite("def g():\n    return 1\n", tests, out=out)
    except BaseException as error:
        raised.append(type(error).__name__)
    passed, total, kinds, note = harness.read_checks(out.getvalue())
    check("a solution missing the entry point fails at import, and reports "
          "nothing at all rather than zero of four",
          raised == ["ImportError"] and (passed, total, kinds) == (0, 0, [])
          and "no per-check report" in note,
          (raised, passed, total, kinds, note))

    tasks = dict((task.task_id, task) for task in gen_tasks.generate())
    failures, reason = gen_tasks.verify_lock_or_reason(
        tasks=list(tasks.values()))
    check("the tree these suites came from is the locked one, so 'every frozen "
          "suite' below is earned rather than asserted",
          not failures and not reason, (failures[:3], reason))
    nested, missing = [], []
    for task_id, task in sorted(tasks.items()):
        tree = ast.parse(task.tests)
        inside = [node for parent in ast.walk(tree)
                  if isinstance(parent, (ast.Try, ast.With, ast.If,
                                         ast.FunctionDef, ast.While, ast.For))
                  for node in ast.walk(parent)
                  if isinstance(node, ast.ImportFrom) and node.module == "solution"]
        top = [node for node in tree.body
               if isinstance(node, ast.ImportFrom) and node.module == "solution"]
        if inside:
            nested.append(task_id)
        if len(top) != 1:
            missing.append((task_id, len(top)))
    check("and in every frozen suite the `from solution import` line is at the "
          "top level exactly once, never under a try or any other block",
          not nested and not missing and len(tasks) == 36,
          (nested, missing, len(tasks)))


def test_partial_credit_never_reaches_a_verdict():
    """The gate stays binary. Three of four checks passing is REVISE, not APPROVED.

    The per-check counts exist for candidate ranking and for diagnostics. If one
    ever reaches a verdict, the oracle it is compared against stops meaning what
    the pre-registration says it means, so the boundary is worth a check of its
    own rather than being left to the absence of a threshold in the source.
    """
    gen_tasks = _import_gen_tasks()
    tests = _bake_partial(gen_tasks)

    verdict, result = harness.verify_output(
        "```python\n%s```" % _NO_RAISE_SOLUTION, tests=tests)
    check("three of four checks is still REVISE",
          verdict == harness.VERDICT_REVISE, (verdict, result.failure_kind))
    check("and the count travelled with it, trusted, so ranking can read it",
          (result.checks_passed, result.checks_total, result.checks_trusted)
          == (3, 4, True),
          (result.checks_passed, result.checks_total, result.checks_trusted))

    verdict, result = harness.verify_output(
        "```python\n%s```" % _PARTIAL_REFERENCE, tests=tests)
    check("and only four of four is APPROVED",
          verdict == harness.VERDICT_APPROVED
          and (result.checks_passed, result.checks_total) == (4, 4),
          (verdict, result.checks_passed, result.checks_total))


def test_only_the_grader_and_the_stub_read_the_hidden_suite():
    """Who is allowed to touch `task.tests` and `task.reference`, structurally.

    `run_eval.py:325` reads `task.tests` inside `StubModel._thin_suite`, which is
    the one place the hidden suite is deliberately echoed. That is safe only if the
    stub cannot exist when a real provider is in play, so the guard is checked
    rather than assumed: one construction site, guarded by `args.stub`, and no key
    path that reaches it.
    """
    run_eval = _import_run_eval()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "eval", "run_eval.py")
    readers = _task_attr_readers(path, ("tests", "reference"))
    where = sorted(set(scope for scope, _attr, _line in readers))
    check("only the grader, the stub and the record's digest read the hidden "
          "suite or the reference off a task",
          where == ["StubModel._correct", "StubModel._thin_suite",
                    "StubModel._wrong", "grade", "run_one"], where)
    check("the reference is read by the stub alone",
          sorted(set(scope for scope, attr, _l in readers
                     if attr == "reference"))
          == ["StubModel._correct", "StubModel._wrong"], readers)

    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    built = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "StubModel"]
    check("StubModel is constructed in exactly one place", len(built) == 1,
          [node.lineno for node in built])
    guarded = [node for node in ast.walk(tree)
               if isinstance(node, ast.IfExp)
               and isinstance(node.test, ast.Attribute)
               and node.test.attr == "stub"
               and any(child in built for child in ast.walk(node.body))]
    check("and that one place is guarded by `args.stub`, so the hidden suite is "
          "unreachable with a real provider", len(guarded) == 1,
          [node.lineno for node in guarded])

    # The runtime half of the same claim: with no key and no --stub, the run stops
    # before a stub could be built. Keys are faked as absent rather than read from
    # the environment, so this check cannot reach a network on a developer machine.
    out = tempfile.mkdtemp(prefix="no-keys-")
    original = run_eval._keys_from_env
    previous_mode = agents_core.MEASUREMENT_MODE
    run_eval._keys_from_env = lambda: {"gemini": "", "groq": ""}
    try:
        code, text = _capture_streams(lambda: run_eval.main(
            ["--arm", "a", "--limit", "1", "--out", out]))
    finally:
        run_eval._keys_from_env = original
        agents_core.set_measurement_mode(previous_mode)
        shutil.rmtree(out, ignore_errors=True)
    check("with no key and no --stub the run stops instead of building a stub",
          code == 2 and "no key for" in text, (code, text[-200:]))


def test_the_hidden_suite_digest_travels_on_the_record():
    """`passed` is meaningless without the identity of the suite that produced it.

    Two results a fortnight apart are comparable only if they were graded against
    the same bytes, and "they were, we think" is not an audit.
    """
    gen_tasks = _import_gen_tasks()
    out = tempfile.mkdtemp(prefix="digest-")
    try:
        _sweep(out)
        records = [json.load(open(path))
                   for path in glob.glob(out + "/seed-0/arm-*/*.json")]
        check("the sweep produced a record", len(records) == 1, len(records))
        record = records[0]
        lock = gen_tasks.load_lock()
        tasks = dict((task.task_id, task)
                     for task in gen_tasks.generate_from(lock["generator"]))
        task = tasks[record["task_id"]]
        check("the record carries the full sha256 of the suite that graded it",
              record["hidden_tests_sha256"] == gen_tasks.digest(task.tests),
              record.get("hidden_tests_sha256"))
        check("which is the digest the lock pins for that task",
              record["hidden_tests_sha256"] == dict(
                  (entry["task_id"], entry["tests_sha256"])
                  for entry in lock["tasks"])[record["task_id"]],
              record["task_id"])
        check("arm A has no gate, so it carries the hidden suite's digest and no "
              "visible-suite id: the two are different things on one record",
              "gate_tests_sha256" not in record
              and len(record["hidden_tests_sha256"]) == 64,
              sorted(key for key in record if "sha256" in key))
        check("the manifest states which lock the run verified against",
              _read_summary(out)["tasks_lock"]["verified_at_startup"] is True
              and len(_read_summary(out)["tasks_lock"]["body_sha256"]) == 64,
              _read_summary(out).get("tasks_lock"))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_the_leakage_audit_extracts_what_it_claims_to():
    """The needles, before any sweep. An audit is only as good as its needles."""
    audit_leakage = _import_audit_leakage()
    gen_tasks = _import_gen_tasks()

    values, skipped = audit_leakage._expected_values(
        "from solution import f\n"
        "assert f([1, 2]) == [1, 2, 3, 4, 5]\n"
        "assert f([]) == 0\n"
        "assert all(f(x) == {'a': 1, 'b': 22222} for x in range(3))\n")
    check("an expected value is recovered from the assert's comparator",
          "[1, 2, 3, 4, 5]" in values, values)
    check("and from a comparison nested inside a call, which a regex over "
          "`assert ... ==` would miss",
          "{'a': 1, 'b': 22222}" in values, values)
    check("a value too short to be distinguishable from coincidence is skipped "
          "and counted, not silently dropped", skipped == 1 and "0" not in values,
          (skipped, values))
    check("a suite that does not parse yields no values rather than raising",
          audit_leakage._expected_values("assert f(") == ([], 0))

    check("whitespace is normalised on both sides, so a reformatted leak still "
          "matches", audit_leakage._normalise("a  =\n\t1") == "a = 1",
          audit_leakage._normalise("a  =\n\t1"))

    task = gen_tasks.generate_from(gen_tasks.load_lock()["generator"])[0]
    needles, short, public = audit_leakage.needles_for(task)
    legs = set(needle.leg for needle in needles)
    check("all three legs produce needles for a real task",
          legs == set(audit_leakage.LEGS), sorted(legs))
    check("the import line is dropped as already public rather than reported as "
          "a leak of itself", public >= 1, (public, short))
    check("no needle is shorter than its leg's threshold",
          all(len(needle.text) >= (audit_leakage.MIN_VALUE_CHARS
                                   if needle.leg == audit_leakage.LEG_EXPECTED
                                   else audit_leakage.MIN_LINE_CHARS)
              for needle in needles))

    planted = needles[0]
    prompts = [audit_leakage.Prompt(0, "groq", "executor", task.task_id,
                                    "system", "nothing to see", "nothing to see"),
               audit_leakage.Prompt(1, "groq", "executor", task.task_id, "system",
                                    "here: " + planted.text,
                                    "here: " + planted.text)]
    hits = audit_leakage.scan(prompts, needles)
    check("scan finds a planted needle and names the prompt it is in",
          planted in [hit.needle for hit in hits]
          and set(hit.prompt.index for hit in hits) == {1}, hits[:2])
    check("and finds nothing in a prompt that carries nothing",
          audit_leakage.scan(prompts[:1], needles) == [], "")


def test_no_prompt_in_a_whole_sweep_carries_the_hidden_suite():
    """The audit itself, over every prompt a sweep sends. No keys, no network.

    One task rather than all thirty-six, because this runs on every commit; the
    full sweep is a command, not a check. `--arm all` is not negotiable though:
    each arm builds its prompts differently and a clean result for one says
    nothing about the others.

    Three roles build prompts out of task material, and the Test Writer is only
    called when a suite is rejected -- so the audit runs a second `--vacuous-plan`
    pass to reach it, and the checks below are per role rather than over the
    total. The first clean version of this audit swept 360 prompts, printed a
    pass, and had never sent a Test Writer prompt at all.
    """
    audit_leakage = _import_audit_leakage()
    previous_mode = agents_core.MEASUREMENT_MODE
    previous_attempts = agents_core.retry_attempts()
    try:
        clean = audit_leakage.audit(limit=1, arm="all")
        check("the audit sweep runs offline and clean",
              clean["sweep_code"] == 0, clean["sweep_code"])
        check("it swept prompts, so a clean result is not a result about nothing",
              clean["prompts"] > 0, clean["prompts"])
        check("it checked literals, likewise", clean["literals"] > 0,
              clean["literals"])
        check("every arm's prompts were swept: the planner turn and the executor "
              "turns both appear",
              clean["by_role"]["planner"] > 0 and clean["by_role"]["executor"] > 0,
              dict(clean["by_role"]))
        check("and so is the Test Writer's, the third role that builds a prompt "
              "from task material -- a total large enough to look thorough is "
              "what hid a count of zero here for a whole sprint",
              clean["by_role"]["test_writer"] > 0, dict(clean["by_role"]))
        check("the required-role set is all three, so the clean result is a claim "
              "about each of them and not only about the total",
              clean["required_roles"] == set(["planner", "test_writer",
                                              "executor"]),
              sorted(clean["required_roles"]))
        check("no required role was left unswept",
              audit_leakage.missing_roles(clean) == [],
              audit_leakage.missing_roles(clean))
        check("the Test Writer prompts come from the vacuity pass, which is the "
              "only path that reaches that role",
              set(prompt.pass_name for prompt in clean["prompt_list"]
                  if prompt.role == "test_writer")
              == set([audit_leakage.PASS_VACUITY]),
              sorted(set(prompt.pass_name for prompt in clean["prompt_list"]
                         if prompt.role == "test_writer")))
        check("and that pass really went down the regeneration branch: the Test "
              "Writer was told why the previous suite was rejected, which is "
              "text only that branch produces",
              all("REJECTED because" in prompt.user
                  for prompt in clean["prompt_list"]
                  if prompt.role == "test_writer"),
              [prompt.user[:80] for prompt in clean["prompt_list"]
               if prompt.role == "test_writer"][:1])
        check("both passes contributed prompts, so the union is a union",
              clean["by_pass"][audit_leakage.PASS_GATE] > 0
              and clean["by_pass"][audit_leakage.PASS_VACUITY] > 0,
              dict(clean["by_pass"]))
        check("no hidden-suite line, expected value or reference line appears in "
              "any prompt", clean["hits"] == [],
              [(hit.needle.leg, hit.needle.text[:60]) for hit in clean["hits"]][:3])
        check("and the audit says so with exit status 0",
              audit_leakage.verdict(clean) == 0)

        leaked = audit_leakage.audit(limit=1, arm="all", inject="value")
        check("one expected value injected into build_context and the audit fails",
              audit_leakage.verdict(leaked) == 1, leaked["hits"][:1])
        check("the injection reached prompts at all, so the demonstration is not "
              "of a no-op", leaked["injected"] > 0, leaked["injected"])
        check("and what it reports is the expected-value leg, not a line match",
              any(hit.needle.leg == audit_leakage.LEG_EXPECTED
                  for hit in leaked["hits"]),
              sorted(set(hit.needle.leg for hit in leaked["hits"])))
        check("the same sweep is otherwise identical, so the difference is the "
              "leak and not the traffic",
              leaked["literals"] == clean["literals"]
              and leaked["prompts"] == clean["prompts"],
              (leaked["prompts"], clean["prompts"]))

        # The third role needs its own injection site. There is no Test Writer
        # prompt *builder* to wrap: `_resolve_tests` assembles that prompt inline
        # and the task material in it arrives by being quoted back -- the
        # rejected suite is echoed so the model can be told what was wrong with
        # it. So the literal goes into the Planner's rejected TESTS block and the
        # pipeline's own code carries it the rest of the way.
        writer = audit_leakage.audit(limit=1, arm="all", inject="test_writer")
        check("a literal put in the Planner's rejected suite reaches the Test "
              "Writer's prompt and the audit fails",
              audit_leakage.verdict(writer) == 1,
              [(hit.needle.leg, hit.prompt.role) for hit in writer["hits"]][:3])
        check("the injection was not a no-op", writer["injected"] > 0,
              writer["injected"])
        check("and it is the Test Writer's prompt that carries it, not the "
              "Executor's -- the rejected suite is discarded once a replacement "
              "arrives, so nothing downstream should see it",
              set(hit.prompt.role for hit in writer["hits"]) == set(["test_writer"]),
              sorted(set(hit.prompt.role for hit in writer["hits"])))
        check("the two injection modes name two different sites, so one flag "
              "cannot silently demonstrate the same channel twice",
              (audit_leakage.INJECT_SITE["value"],
               audit_leakage.INJECT_SITE["test_writer"])
              == (audit_leakage.SITE_CONTEXT, audit_leakage.SITE_VACUOUS))

        narrowed = audit_leakage.audit(limit=1, arm="all", vacuity=False)
        check("without the vacuity pass no Test Writer prompt is sent at all, "
              "which is why the audit needed a second pass rather than a wider "
              "assertion over the first",
              narrowed["by_role"]["test_writer"] == 0
              and narrowed["prompts"] < clean["prompts"],
              (dict(narrowed["by_role"]), narrowed["prompts"], clean["prompts"]))
        check("and it drops test_writer from what it requires, so a narrowed run "
              "is honest rather than failing for not doing what it was told not "
              "to do",
              narrowed["required_roles"] == set(["planner", "executor"])
              and audit_leakage.verdict(narrowed) == 0,
              sorted(narrowed["required_roles"]))
    finally:
        agents_core.set_measurement_mode(previous_mode)
        agents_core.set_retry_attempts(previous_attempts)

    check("a sweep that swept nothing is a failure, not a pass",
          audit_leakage.verdict({"sweep_code": 0, "prompts": 0, "literals": 9,
                                 "hits": [], "by_role": {},
                                 "required_roles": set(["executor"])}) == 2)
    check("and so is one with no literals to check",
          audit_leakage.verdict({"sweep_code": 0, "prompts": 9, "literals": 0,
                                 "hits": [], "by_role": {"executor": 9},
                                 "required_roles": set(["executor"])}) == 2)
    # Per role, not only in total: this is the shape the real gap had. 360
    # prompts were swept, the audit printed a clean pass, and `test_writer` was
    # zero throughout. A total cannot express that, so the refusal reads the
    # required set against the per-role tally.
    unswept = {"sweep_code": 0, "prompts": 9, "literals": 9, "hits": [],
               "by_role": {"executor": 9, "planner": 0},
               "required_roles": set(["executor", "planner", "test_writer"])}
    check("a required role with a swept count of zero refuses with 2, even "
          "though the total is not zero and nothing leaked",
          audit_leakage.verdict(unswept) == 2)
    check("and the refusal names every role it did not cover, so the reason is "
          "readable without re-deriving it",
          audit_leakage.missing_roles(unswept) == ["planner", "test_writer"],
          audit_leakage.missing_roles(unswept))
    check("a leak still reports 1 in that state: coverage cannot un-find a "
          "literal that is demonstrably in a prompt",
          audit_leakage.verdict(dict(unswept, hits=[object()])) == 1)
    check("arm A alone requires only the Executor, since it never calls the "
          "Planner -- the required set is derived from the arms, not fixed",
          (audit_leakage.required_roles("a", True),
           audit_leakage.required_roles("b", True),
           audit_leakage.required_roles("all", False))
          == (set(["executor"]),
              set(["executor", "planner", "test_writer"]),
              set(["executor", "planner"])))
    check("the audit stub reads only the prompt and the names, never the hidden "
          "suite or the reference",
          sorted(set(scope for scope, _a, _l in _task_attr_readers(
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "eval", "audit_leakage.py"),
              ("tests", "reference")))) == ["needles_for"])


# ---------------------------------------------------------------------------
# sprint 6, task 3: log every event, and permute the task order

# The shape of one per-run JSON as of commit b78cff5, before this sprint touched
# anything: every leaf path and its type, unioned over the four arms of
# `--stub perfect --arm all --limit 1 --seed 0`. Extracted by running that
# commit's `eval/run_eval.py` from a scratch checkout, not written by hand.
#
# The JSONL event log is additive by construction, and this is what "additive"
# has to mean concretely: not one existing key renamed, retyped, moved or
# dropped. The UI and every stored record read this shape, so a rename here
# silently invalidates records already on disk -- there is no version field to
# tell an old one from a new one. `params` is deliberately opaque: its keys are
# task content (`digits` for aggregation), not record structure.
_BASELINE_RECORD_SHAPE = {
    "arm": "str", "arm_note": "str", "call_log[].at": "str",
    "call_log[].measurement_mode": "bool", "call_log[].model": "str",
    "call_log[].ok": "bool", "call_log[].requested": "str",
    "call_log[].role": "str", "call_log[].temperature": "float",
    "call_log[].top_p": "float", "call_log[].used": "str", "calls": "int",
    "candidates[].code": "str", "candidates[].draw": "int",
    "candidates[].exit_code": "int", "candidates[].failed_assertion": "str",
    "candidates[].failed_assertion_line": "null",
    "candidates[].failure_kind": "str", "candidates[].gate_verdict": "str",
    "candidates[].provider": "str", "candidates[].round": "int",
    "chosen_draw": "int", "code": "str", "degenerated_to_a_prime": "bool",
    "draws": "int", "error": "str", "escalation": "str",
    "failed_calls": "int", "failure_kind": "str", "false_approved": "bool",
    "family": "str", "final_round_worse": "bool",
    "gate_approved_draws[]": "int", "gate_available": "bool",
    "gate_tests_sha256": "str", "grade_assertion": "str",
    "grade_exit": "int", "grade_failure": "str", "grade_reason": "str",
    "grade_stderr[]": "bool|str", "grade_timed_out": "bool",
    "graded_step": "int", "log": "str", "measurement_mode": "bool",
    "params": "dict", "passed": "bool", "pipeline_says_passed": "bool|null",
    "plan_call_log[].at": "str", "plan_call_log[].measurement_mode": "bool",
    "plan_call_log[].model": "str", "plan_call_log[].ok": "bool",
    "plan_call_log[].requested": "str", "plan_call_log[].role": "str",
    "plan_call_log[].temperature": "float", "plan_call_log[].top_p": "float",
    "plan_call_log[].used": "str", "plan_calls": "int",
    "plan_shared": "bool", "plan_tests_status": "str",
    "planner_provider": "str", "provider_last": "str",
    "provider_substituted": "bool", "providers[]": "str",
    "rate_limit_retries": "int", "raw_len": "int", "repair_rounds": "int",
    "repeat": "int", "retained_round": "int", "run_id": "str",
    "seconds": "float", "seconds_sleeping": "float", "seed": "int",
    "selected_by_gate": "bool", "selection": "str", "steps": "int",
    "steps_approved": "int", "stub": "str", "task_id": "str",
    "tests_status": "str", "tests_trusted": "bool", "tier": "int",
    "variant": "int", "verdict": "str"}

# Everything sprint 6 added to that record, enumerated so a *silent* addition is
# a failure too. A key that appears without anyone writing it down here is a key
# no reader of these records knows about.
_ADDED_RECORD_KEYS = (
    "call_log[].attempts", "call_log[].exc_class", "call_log[].retried",
    "call_log[].status", "hidden_tests_sha256", "outcome",
    "plan_call_log[].attempts", "plan_call_log[].exc_class",
    "plan_call_log[].retried", "plan_call_log[].status",
    # Sprint 8, task 2: the resolved role -> (provider, model) table, the
    # sampling actually sent with it, where the configuration came from, and any
    # independence warning it earned. Declared here rather than tolerated,
    # because the whole point of this table is that a record cannot grow a field
    # nobody wrote down.
    "independence_warnings[]", "models[].base_url", "models[].model",
    "models[].provider", "models[].role", "models[].temperature",
    "models[].top_p", "models_source",
    # Sprint 9, task 1: the per-check tally the suite now prints, carried per
    # candidate because that is what the retention key reads. `checks_trusted`
    # is part of the shape on purpose -- a count without it cannot be told from
    # an unmeasured zero, and a reader who cannot tell will average them.
    "candidates[].checks_passed", "candidates[].checks_total",
    "candidates[].checks_trusted")

def _shape_into(value, path, out):
    """Leaf paths of a JSON value to the set of types seen at each."""
    if isinstance(value, dict):
        if path.endswith("params"):
            out.setdefault(path, set()).add("dict")
            return
        if not value:
            out.setdefault(path, set()).add("dict")
        for name in sorted(value):
            walk = ("%s.%s" % (path, name)) if path else name
            _shape_into(value[name], walk, out)
    elif isinstance(value, list):
        if not value:
            out.setdefault(path + "[]", set()).add("empty")
        for item in value:
            _shape_into(item, path + "[]", out)
    else:
        out.setdefault(path, set()).add(
            "null" if value is None else type(value).__name__)


def _record_shape(pattern):
    out = {}
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as handle:
            _shape_into(json.load(handle), "", out)
    return dict((name, "|".join(sorted(kinds))) for name, kinds in out.items())


def _lines_of(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class _Named(object):
    """All a `_task_order` element needs to be: it reads no attribute at all."""

    def __init__(self, task_id):
        self.task_id = task_id


def test_every_event_kind_round_trips_through_the_writer():
    """All seven kinds, written and parsed back as one JSON object per line."""
    out = tempfile.mkdtemp(prefix="events-kinds-")
    try:
        path = agents_core.event_log_path(os.path.join(out, "run-kinds.json"))
        log = agents_core.EventLog(
            path, context={"run_id": "run-kinds", "task_id": "aggregation-01",
                           "arm": "a", "repeat": 0},
            keys={"gemini": "AIzaSy-not-a-real-key-00112233"})
        payloads = {
            agents_core.EVENT_CALL: {"role": "executor", "used": "groq",
                                     "ok": True, "attempts": 1, "status": None},
            agents_core.EVENT_STEP: {"step": 1, "verdict": "APPROVED"},
            agents_core.EVENT_CANDIDATE: {"draw": 0, "round": 1,
                                          "code_sha256": "0" * 64},
            agents_core.EVENT_GATE: {"available": True, "reason": "",
                                     "tests_sha256": "1" * 64},
            agents_core.EVENT_VERDICT: {"passed": True, "outcome": "graded"},
            agents_core.EVENT_INFRA_LOSS: {"infra_status": 429,
                                           "infra_attempts": 4,
                                           "infra_providers": ["groq"]},
            agents_core.EVENT_RESUME_SKIP: {"result": "aggregation-01__r0.json"}}
        # Keyed off the vocabulary itself: a kind added to `EVENT_KINDS` without
        # a payload here fails this check rather than going unexercised.
        check("every kind in the closed vocabulary is exercised",
              sorted(payloads) == sorted(agents_core.EVENT_KINDS),
              sorted(set(agents_core.EVENT_KINDS) ^ set(payloads)))
        returned = [log.emit(kind, **payloads[kind])
                    for kind in agents_core.EVENT_KINDS]

        raw = _lines_of(path)
        lines = raw.splitlines()
        parsed, malformed = [], []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError as exc:
                malformed.append((line[:60], str(exc)))
                continue
            (parsed if isinstance(event, dict) else malformed).append(event)
        check("one line per event, and every line is one JSON object",
              len(lines) == len(agents_core.EVENT_KINDS) and not malformed,
              (len(lines), malformed))
        check("the log names its file as a JSONL sibling of the run JSON, not a "
              "second *.json a glob would pick up",
              path.endswith("run-kinds.events.jsonl"), path)
        check("every kind round-trips in the order it was emitted",
              [event["kind"] for event in parsed]
              == list(agents_core.EVENT_KINDS),
              [event["kind"] for event in parsed])
        check("emit returns what it wrote",
              returned == parsed, (returned, parsed))
        check("every payload field survives the round trip",
              all(parsed[index][name] == value
                  for index, kind in enumerate(agents_core.EVENT_KINDS)
                  for name, value in payloads[kind].items()), parsed)
        check("every event carries the run, task, arm and repeat it came from",
              all(event["run_id"] == "run-kinds"
                  and event["task_id"] == "aggregation-01"
                  and event["arm"] == "a" and event["repeat"] == 0
                  for event in parsed), parsed)
        stamps = [event["at"] for event in parsed]
        check("every event carries a UTC timestamp in one fixed format",
              all(len(at) == 20 and at.endswith("Z")
                  and time.strptime(at, "%Y-%m-%dT%H:%M:%SZ") for at in stamps),
              stamps)
        check("the writer counts what it wrote and reports no failure",
              (log.count, log.failed) == (len(agents_core.EVENT_KINDS), 0),
              (log.count, log.failed))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_a_newline_in_a_field_cannot_forge_a_second_event():
    """One JSON object per line has to survive content that contains newlines."""
    out = tempfile.mkdtemp(prefix="events-lines-")
    try:
        path = os.path.join(out, "run-lines.events.jsonl")
        log = agents_core.EventLog(path, context={"run_id": "run-lines"})
        body = "Traceback (most recent call last):\n  File \"x\"\nBoom\r\nend"
        log.emit(agents_core.EVENT_STEP, step=0, note=body)
        log.emit(agents_core.EVENT_STEP, step=1)
        raw = _lines_of(path)
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        check("a field full of newlines is still one physical line",
              len(raw.splitlines()) == 2 and log.count == 2,
              len(raw.splitlines()))
        check("and the newlines are still there when it is read back",
              events[0]["note"] == body, events[0]["note"])
        check("the file ends in a newline, so the next append starts a line",
              raw.endswith("\n") and not raw.endswith("\n\n"), repr(raw[-3:]))
        reason = _raises(lambda: log.emit("dance", step=2))
        check("an event kind outside the vocabulary is a ValueError, not a "
              "silently new event type", "unknown event kind" in reason, reason)
        check("and the rejected event reaches neither the file nor the count",
              len(_lines_of(path).splitlines()) == 2 and log.count == 2,
              log.count)
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_the_writer_redacts_keys_and_raw_exceptions_itself():
    """`_redact` at the write site, not trusted to have happened upstream.

    Every value handed to `emit` here is deliberately raw -- no caller redacted
    anything -- because the guarantee has to hold for a *new* write site whose
    author never read `call_role`.
    """
    out = tempfile.mkdtemp(prefix="events-redact-")
    try:
        path = os.path.join(out, "run-redact.events.jsonl")
        key = "AIzaSyD-EXAMPLE-KEY-000111222333"
        log = agents_core.EventLog(path, context={"run_id": "run-redact"},
                                   keys={"gemini": key, "groq": "gsk_" + "9" * 28})
        boom = agents_core.ProviderError(
            "PermissionDenied: API key not valid. key=%s" % key,
            status=401, exc_class="PermissionDenied")
        check("the message really did carry the key, so this is not a check "
              "against an already-clean input", key in str(boom), True)
        log.emit(agents_core.EVENT_CALL, role="planner", ok=False,
                 error=str(boom), detail={"nested": [key, {"deep": key}]},
                 tail=("prefix " + key[:8],))
        log.emit(agents_core.EVENT_INFRA_LOSS,
                 infra_message="429 from groq for gsk_%s" % ("9" * 28))
        raw = _lines_of(path)
        events = [json.loads(line) for line in raw.splitlines()]
        check("no key value appears anywhere in the file, at any depth",
              key not in raw and ("gsk_" + "9" * 28) not in raw,
              [name for name in (key, "gsk_") if name in raw])
        check("a truncated rendering of a key is scrubbed too",
              key[:8] not in raw, key[:8])
        check("what replaced it says so", raw.count("<redacted>") >= 5,
              raw.count("<redacted>"))
        check("redaction reaches inside a list and inside a dict inside a list",
              events[0]["detail"]["nested"] == ["<redacted>",
                                                {"deep": "<redacted>"}]
              and events[0]["tail"] == ["prefix <redacted>"],
              events[0]["detail"])
        check("non-string values are left alone rather than stringified",
              events[0]["ok"] is False and events[1]["kind"] == "infra_loss",
              events)
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_a_failed_call_reaches_the_event_log_without_its_message():
    """The end-to-end leg: a real `call_role` failure, as an event."""
    out = tempfile.mkdtemp(prefix="events-call-")
    try:
        path = os.path.join(out, "run-401.events.jsonl")
        key = "AIzaSyD-EXAMPLE-KEY-444555666777"
        keys = {"gemini": key}
        log = agents_core.EventLog(path, context={"run_id": "run-401",
                                                  "task_id": "t", "arm": "a",
                                                  "repeat": 0}, keys=keys)

        def always_401(provider, api_key, system, user, role=None):
            raise agents_core.ProviderError(
                "PermissionDenied: API key not valid (key=%s)" % key,
                status=401, exc_class="PermissionDenied")

        original = agents_core.call_model
        previous_log = agents_core.set_event_log(log)
        agents_core.call_model = always_401
        agents_core.reset_call_log()
        try:
            raised = _raises(lambda: _with_measurement(
                True, lambda: agents_core.call_role("planner", keys, "s", "u")))
        finally:
            agents_core.call_model = original
            agents_core.set_event_log(previous_log)
            agents_core.reset_call_log()
        raw = _lines_of(path)
        events = [json.loads(line) for line in raw.splitlines()]
        calls = [event for event in events
                 if event["kind"] == agents_core.EVENT_CALL]
        check("a hard failure still emits exactly one call event",
              len(calls) == 1 and "wrong type: ProviderError" in raised,
              (len(calls), raised))
        check("the event says why the call failed, in policy terms",
              bool(calls) and calls[0]["status"] == 401
              and calls[0]["exc_class"] == "PermissionDenied"
              and calls[0]["ok"] is False, calls)
        check("the key is not in the event even though it was in the message",
              key not in raw and "<redacted>" in calls[0]["error"], calls)
        check("no prompt and no completion body reaches the event",
              not [name for name in calls[0]
                   if name in ("system", "user", "prompt", "text", "code")],
              sorted(calls[0]))
    finally:
        shutil.rmtree(out, ignore_errors=True)


_KILL_AFTER_WRITES = """\
import os, signal, sys
sys.path.insert(0, %(root)r)
import agents_core
log = agents_core.EventLog(%(path)r, context={"run_id": "run-kill"})
for step in range(5):
    log.emit(agents_core.EVENT_STEP, step=step, note="event %%d" %% step)
os.kill(os.getpid(), signal.SIGKILL)
"""

_KILL_MID_SWEEP = """\
import os, signal, sys
sys.path.insert(0, %(root)r)
sys.path.insert(0, os.path.join(%(root)r, "eval"))
import agents_core
import run_eval


class _Suicidal(agents_core.EventLog):
    \"\"\"Dies the instant the third event is on disk. No flush, no close, no
    atexit, no summary -- exactly the shape of a sweep that hits the OOM killer
    or a laptop lid.\"\"\"

    def emit(self, kind, **fields):
        event = super(_Suicidal, self).emit(kind, **fields)
        if self.count >= 3:
            os.kill(os.getpid(), signal.SIGKILL)
        return event


agents_core.EventLog = _Suicidal
run_eval.main(["--stub", "perfect", "--arm", "all", "--limit", "1",
               "--out", %(out)r, "--seed", "0"])
"""


def _child(source, out, name):
    script = os.path.join(out, name)
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(source)
    return subprocess.run([sys.executable, "-B", script], cwd=out,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_the_event_log_is_complete_up_to_an_abrupt_death():
    """SIGKILL, not an exception: flushed-per-event is a claim about the OS.

    A `finally` that closes the handle would pass a test that raised, and prove
    nothing about the run that never gets to run its `finally`. So both legs kill
    the writing process outright, and the parent reads what survived.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    out = tempfile.mkdtemp(prefix="events-kill-")
    try:
        path = os.path.join(out, "killed.events.jsonl")
        done = _child(_KILL_AFTER_WRITES % {"root": root, "path": path}, out,
                      "kill_writer.py")
        raw = _lines_of(path) if os.path.exists(path) else ""
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        check("the writing process really was killed, not unwound",
              done.returncode == -9, (done.returncode, done.stderr[-300:]))
        check("every event written before a SIGKILL is on disk and parses",
              [event["step"] for event in events] == [0, 1, 2, 3, 4],
              [event.get("step") for event in events])
        check("the last surviving line is whole, not a torn half-object",
              raw.endswith("}\n"), repr(raw[-40:]))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_a_sweep_killed_mid_grid_still_explains_itself():
    root = os.path.dirname(os.path.abspath(__file__))
    out = tempfile.mkdtemp(prefix="events-kill-sweep-")
    try:
        target = os.path.join(out, "runs")
        done = _child(_KILL_MID_SWEEP % {"root": root, "out": target}, out,
                      "kill_sweep.py")
        path = os.path.join(target, "seed-0", "events.jsonl")
        raw = _lines_of(path) if os.path.exists(path) else ""
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        check("a sweep killed mid-grid dies without unwinding",
              done.returncode == -9, (done.returncode, done.stderr[-300:]))
        check("its log holds every event up to the death and no partial line",
              len(events) == 3 and raw.endswith("}\n"),
              (len(events), repr(raw[-40:])))
        check("every surviving event still names its cell: the seed, the task, "
              "the repeat, and either an arm or the shared plan phase",
              bool(events) and all(
                  event.get("seed") == 0 and event.get("task_id")
                  and event.get("repeat") == 0
                  and (event.get("arm") in ("a", "a_prime", "a_prime3", "b"))
                  != (event.get("phase") == "plan") for event in events),
              [(event.get("arm"), event.get("phase")) for event in events])
        check("the manifest the sweep never reached is absent, so the log is "
              "the only record of a killed run -- which is the point",
              not os.path.exists(os.path.join(target, "seed-0", "summary.json")),
              sorted(os.listdir(os.path.join(target, "seed-0"))))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_a_four_arm_sweep_extends_the_record_and_attributes_every_event():
    """Two claims off one sweep: the record only grew, and every event has a cell.

    `run_id` is deliberately *not* on every event. Three of the four arms make a
    bare provider call and never create a `Memory`, so there is no run to name --
    minting one would put an id on the record that points at a `runs/<id>.json`
    that does not exist. The identity that is always true is the cell, which is
    also the result path: seed, task, arm, repeat.
    """
    out = tempfile.mkdtemp(prefix="shape-")
    try:
        # `--arm all` after `_sweep`'s own `--arm a`: argparse takes the last, and
        # the union over four arms is what covers the arm-specific keys.
        code, text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _sweep(out, extra=("--arm", "all")))
        check("a four-arm stub sweep runs", code == 0, text[-400:])
        now = _record_shape(os.path.join(out, "seed-0", "arm-*", "*.json"))
        missing = sorted(name for name in _BASELINE_RECORD_SHAPE
                         if name not in now)
        retyped = sorted((name, _BASELINE_RECORD_SHAPE[name], now[name])
                         for name in _BASELINE_RECORD_SHAPE
                         if name in now and now[name]
                         != _BASELINE_RECORD_SHAPE[name])
        added = sorted(set(now) - set(_BASELINE_RECORD_SHAPE))
        check("the baseline shape is a real snapshot, not an empty dict that "
              "would make the next three checks vacuous",
              len(_BASELINE_RECORD_SHAPE) == 83 and len(now) >= 83,
              (len(_BASELINE_RECORD_SHAPE), len(now)))
        check("no key the pre-sprint record had is missing", not missing, missing)
        check("no key the pre-sprint record had changed type", not retyped,
              retyped)
        check("every added key is one this sprint declared it was adding",
              added == sorted(_ADDED_RECORD_KEYS), added)

        events = _read_events(os.path.join(out, "seed-0", "events.jsonl"))
        check("the sweep logged every arm and more than one kind",
              set(event.get("arm") for event in events)
              == set(["a", "a_prime", "a_prime3", "b", None])
              and len(set(event["kind"] for event in events)) > 1,
              [(event.get("arm"), event["kind"]) for event in events])
        check("every event names its cell: seed, task, repeat, and either an arm "
              "or the shared plan phase -- which is the result path",
              all(event.get("seed") == 0 and event.get("task_id")
                  and event.get("repeat") == 0
                  and (event.get("arm") in ("a", "a_prime", "a_prime3", "b"))
                  != (event.get("phase") == "plan") for event in events),
              [(event.get("arm"), event.get("phase")) for event in events])
        check("the one event with no arm is the Planner call all three plan arms "
              "share, so no shared call is attributed to one arm",
              [event["role"] for event in events if event.get("phase") == "plan"]
              == ["planner"],
              [event for event in events if "arm" not in event])
        with_run = set(event.get("arm") for event in events
                       if event.get("run_id"))
        check("run_id is present exactly where a pipeline run exists, and arm B "
              "is the only arm that has one",
              with_run == set(["b"]), with_run)
        arm_b = [event for event in events if event.get("arm") == "b"]
        recorded = json.load(open(glob.glob(
            os.path.join(out, "seed-0", "arm-b", "*.json"))[0]))
        check("and that run_id is the one on arm B's record, so an event joins "
              "to the run file it came from",
              set(event["run_id"] for event in arm_b) == set([recorded["run_id"]]),
              (sorted(set(e.get("run_id") for e in arm_b)), recorded["run_id"]))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _model_config_scoped(config, thunk):
    """Run `thunk` with `MAW_MODELS` set, and every global it installs put back.

    Through the environment variable rather than by calling `apply_model_config`
    directly, because what the checks below are about is the path a real run
    takes: the CLI reads the variable itself, and a check that hand-installed the
    config would not notice if that read were removed.

    Everything `apply_model_config` writes is process-wide, so all of it is
    snapshotted -- a config left installed would silently retarget every check
    that ran afterwards.
    """
    previous = {
        "providers": dict((name, dict(cfg))
                          for name, cfg in agents_core.PROVIDERS.items()),
        "role_provider": dict(agents_core.ROLE_PROVIDER),
        "role_model": dict(agents_core.ROLE_MODEL),
        "role_params": dict(agents_core.ROLE_PARAMS),
        "temperature": agents_core.TEMPERATURE,
        "top_p": agents_core.TOP_P,
        "source": agents_core.model_config_source(),
    }
    had = os.environ.get(agents_core.MODEL_CONFIG_ENV)
    if config is None:
        os.environ.pop(agents_core.MODEL_CONFIG_ENV, None)
    else:
        os.environ[agents_core.MODEL_CONFIG_ENV] = json.dumps(config)
    try:
        return thunk()
    finally:
        if had is None:
            os.environ.pop(agents_core.MODEL_CONFIG_ENV, None)
        else:
            os.environ[agents_core.MODEL_CONFIG_ENV] = had
        agents_core.PROVIDERS.clear()
        agents_core.PROVIDERS.update(previous["providers"])
        agents_core.ROLE_PROVIDER.clear()
        agents_core.ROLE_PROVIDER.update(previous["role_provider"])
        agents_core.ROLE_MODEL.clear()
        agents_core.ROLE_MODEL.update(previous["role_model"])
        agents_core.ROLE_PARAMS.clear()
        agents_core.ROLE_PARAMS.update(previous["role_params"])
        agents_core.TEMPERATURE = previous["temperature"]
        agents_core.TOP_P = previous["top_p"]
        agents_core._MODEL_CONFIG_SOURCE = previous["source"]


def _resolved_by_role(entries):
    return dict((entry["role"], entry) for entry in entries)

def test_a_role_can_be_pointed_at_its_own_model_and_the_call_goes_there():
    """The gap this closes: a role could not have a model, only a provider.

    `ROLE_PROVIDER` mapped a role to a provider and the model rode along from the
    provider table, so "point the Executor at a different model" was a source
    edit -- twice now, once per retirement. The check is not that the table says
    the right thing; it is that the *call* goes to the model the table names,
    because a resolved record that disagrees with the wire is worse than no
    record.
    """
    sent = []

    def spy(provider, api_key, system, user, role=None):
        sent.append((role, provider, agents_core.model_for(role, provider)))
        return "```python\ndef f():\n    return 1\n```"

    config = {"roles": {"executor": {"model": "example-executor-id"},
                        "planner": {"params": {"max_tokens": 321}}}}

    def run():
        resolved = _resolved_by_role(agents_core.configure_models())
        check("the Executor resolves to the model its role names, not its "
              "provider's default",
              resolved["executor"]["model"] == "example-executor-id"
              and resolved["executor"]["provider"] == "groq",
              resolved["executor"])
        check("and a role that names no model still gets its provider's "
              "default, so one override does not disturb the others",
              resolved["planner"]["model"]
              == agents_core.PROVIDERS["gemini"]["model"]
              == resolved["test_writer"]["model"],
              (resolved["planner"]["model"], resolved["test_writer"]["model"]))
        check("a per-role sampling parameter is recorded under its own name "
              "beside temperature and top_p, not folded into them",
              resolved["planner"].get("max_tokens") == 321
              and "max_tokens" not in resolved["executor"],
              (resolved["planner"], resolved["executor"]))
        original = agents_core.call_model
        agents_core.call_model = spy
        try:
            agents_core.call_role("executor", {"groq": "k", "gemini": "k"},
                                  "system", "user")
        finally:
            agents_core.call_model = original

    _model_config_scoped(config, run)
    check("the call the Executor actually made asked for the model the role "
          "names, at the role's own provider",
          sent == [("executor", "groq", "example-executor-id")], sent)
    check("and the override is gone once the config is out of scope, so no "
          "later check inherits it",
          agents_core.ROLE_MODEL == {} and agents_core.model_for("executor")
          == agents_core.PROVIDERS["groq"]["model"],
          (agents_core.ROLE_MODEL, agents_core.model_for("executor")))

def test_a_same_provider_alternate_is_expressible_but_deliberately_not_wired():
    """`alternate` is arm B's definition, so widening it is not a code change.

    The escalation ladder's `alternate` rung means "same traceback, different
    model", and it reached that different model by switching *provider* -- it had
    to, because the model rode along with the provider and there was no other
    axis. That axis now exists, and is deliberately left unused: what `alternate`
    means is registered. This check pins the current meaning so that changing it
    cannot happen quietly, and pins the reason a role's override must not follow
    it -- an override names a model at the role's own endpoint, and asking a
    different endpoint for it would 404 in a way that reads as a provider outage.
    """
    config = {"roles": {"executor": {"model": "example-executor-id"}}}

    def run():
        agents_core.configure_models()
        own = agents_core.ROLE_PROVIDER["executor"]
        other = agents_core.alternate_provider("executor",
                                               dict((name, "k") for name
                                                    in agents_core.PROVIDERS))
        check("the override applies at the role's own provider",
              agents_core.model_for("executor", own) == "example-executor-id",
              agents_core.model_for("executor", own))
        check("the alternate rung is still a different provider, which is what "
              "makes the next check the one that matters",
              other is not None and other != own, (own, other))
        check("and the override does not follow the role there: that endpoint "
              "is asked for its own model, not for one it never served",
              agents_core.model_for("executor", other)
              == agents_core.PROVIDERS[other]["model"],
              agents_core.model_for("executor", other))
        check("the ladder itself is unchanged -- three rungs, `alternate` in "
              "the middle, described as a different model",
              agents_core.ESCALATION == ("repair", "alternate", "fresh")
              and "different model" in agents_core.ESCALATION_WHY["alternate"],
              (agents_core.ESCALATION, agents_core.ESCALATION_WHY["alternate"]))

    _model_config_scoped(config, run)

def test_the_resolved_mapping_reaches_the_run_json_and_both_manifests():
    """One resolution, three records, and they have to agree with the wire.

    The failure this rules out is a manifest built by *reading source*: today
    `--bad-slug` mutates the provider table in place, so a record that reported
    `PROVIDERS["groq"]["model"]` off the pristine constant would name a model the
    run never called. Every record below is checked against the calls the run
    actually logged, not against the table.
    """
    out = tempfile.mkdtemp(prefix="models-record-")
    try:
        code, text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _sweep(os.path.join(out, "grid"), extra=("--arm", "all")))
        check("a four-arm stub sweep runs", code == 0, text[-400:])
        records = [_read_json(path) for path in sorted(glob.glob(
            os.path.join(out, "grid", "seed-0", "arm-*", "*.json")))]
        check("every run JSON carries the resolved table and says where the "
              "configuration came from",
              records and all(
                  record["models_source"] == "defaults"
                  and sorted(entry["role"] for entry in record["models"])
                  == ["executor", "planner", "test_writer"]
                  for record in records),
              [(r["models_source"], len(r["models"])) for r in records])
        resolved = _resolved_by_role(records[0]["models"])
        check("each entry names the role, the provider, the model, the base_url "
              "and the sampling actually pinned",
              all(set(entry) >= set(["role", "provider", "model", "base_url",
                                     "temperature", "top_p"])
                  for entry in resolved.values()),
              sorted(resolved["executor"]))
        # The wire, from the call log the run wrote itself. `used` and not
        # `requested`: what matters is the endpoint the call actually went to.
        logged = {}
        for record in records:
            for entry in (record.get("call_log") or []) + (
                    record.get("plan_call_log") or []):
                if entry.get("role"):
                    logged.setdefault(entry["role"], set()).add(
                        (entry["used"], entry["model"]))
        check("the sweep actually called more than one role, so the next check "
              "is not vacuous", len(logged) >= 2, sorted(logged))
        mismatched = sorted(
            (role, sorted(pairs),
             (resolved[role]["provider"], resolved[role]["model"]))
            for role, pairs in logged.items()
            if pairs != set([(resolved[role]["provider"],
                              resolved[role]["model"])]))
        check("and every call it logged went to the (provider, model) pair the "
              "record claims for that role", mismatched == [], mismatched)
        summary = _read_summary(os.path.join(out, "grid"))
        check("the grid manifest carries the same table, the source, the "
              "preflight it ran and the independence warnings",
              summary["models"]["source"] == "defaults"
              and _resolved_by_role(summary["models"]["resolved"]) == resolved
              and summary["models"]["preflight_skipped"] is False
              and [record["ok"] for record in summary["models"]["preflight"]]
              == [True, True]
              and summary["models"]["independence_warnings"] == [],
              summary["models"])

        root = os.path.join(out, "cal")
        code, text = _calibrate(["--stub", "sampled", "--limit", "1",
                                 "--draws", "2", "--out", root])
        check("an offline calibration runs", code in (0, 1), text[-400:])
        manifest = _calibration_manifest(root)
        check("the calibration manifest carries the resolved table too, which "
              "is the second of the two manifests D2 freezes",
              _resolved_by_role(manifest["models"]) == resolved
              and manifest["models_source"] == "defaults"
              and manifest["independence_warnings"] == []
              and manifest["preflight_skipped"] is False
              and [entry["ok"] for entry in manifest["preflight"]] == [True,
                                                                      True],
              (manifest["models_source"], manifest["preflight"]))
        check("and `models_used` still reads as it did, derived from the same "
              "resolution rather than from the provider table",
              manifest["models_used"] == dict(
                  (resolved[role]["provider"], resolved[role]["model"])
                  for role in resolved),
              manifest["models_used"])
    finally:
        shutil.rmtree(out, ignore_errors=True)

def test_the_preflight_refuses_a_dead_pair_and_passes_a_live_one():
    """One call per pair before a sweep, and a refusal instead of a spend.

    The negative control is the real retired slug, not a synthetic one: as of
    2026-08-30 the Groq model in the provider table 404s for this project's key,
    and it is the Executor in all four arms, so the *default* configuration is
    exactly the configuration a preflight has to catch. `--live-models` tells the
    stand-in which IDs the key can reach and every other ID 404s through the same
    gate `--bad-slug` uses, so this is the real refusal path rather than an
    imitation of it.
    """
    out = tempfile.mkdtemp(prefix="preflight-")
    try:
        live = agents_core.PROVIDERS["gemini"]["model"]
        dead = agents_core.PROVIDERS["groq"]["model"]
        code, text = _sweep(os.path.join(out, "refused"),
                            extra=("--live-models", live), streams=True)
        check("a sweep whose Executor model does not answer refuses with exit "
              "2 rather than starting", code == 2, (code, text[-500:]))
        check("and the refusal names the role, the provider and the model, "
              "which is the message a bare 404 does not give you",
              "executor" in text and "groq/%s" % dead in text
              and "404" in text, text[-500:])
        check("it says how to find a model the key can reach, rather than "
              "leaving that as an exercise",
              "eval/models.py --list" in text, text[-500:])
        check("nothing was written: a refusal is not a run",
              not os.path.exists(os.path.join(out, "refused", "seed-0",
                                              "summary.json")),
              sorted(glob.glob(os.path.join(out, "refused", "*"))))

        code, text = _sweep(os.path.join(out, "ok"),
                            extra=("--live-models", "%s,%s" % (live, dead)))
        check("the same sweep runs once both configured pairs answer",
              code == 0, text[-400:])
        check("and says so, naming the pairs it validated rather than the roles "
              "alone", "preflight OK" in text and "groq/%s" % dead in text,
              [line for line in text.splitlines() if "preflight" in line])
        pairs = _read_summary(os.path.join(out, "ok"))["models"]["preflight"]
        check("one call per distinct pair, not one per role: planner and "
              "test_writer share a pair today", len(pairs) == 2, pairs)
        check("a validated pair records the roles it stands for, so a failure "
              "later can be attributed",
              sorted(sorted(record["roles"]) for record in pairs)
              == [["executor"], ["planner", "test_writer"]], pairs)

        code, text = _sweep(os.path.join(out, "skipped"),
                            extra=("--live-models", live, "--no-preflight"))
        check("--no-preflight spends the grid anyway, because refusing "
              "unconditionally would make a dead slug unstudiable", code == 0,
              text[-400:])
        check("and it says out loud what that costs, rather than skipping "
              "silently", "preflight SKIPPED" in text,
              [line for line in text.splitlines() if "SKIP" in line])
        summary = _read_summary(os.path.join(out, "skipped"))
        check("the manifest records that the preflight was skipped, so a run "
              "that failed mid-grid can be told from one never checked",
              summary["models"]["preflight_skipped"] is True
              and summary["models"]["preflight"] == [], summary["models"])

        code, text = _calibrate(["--stub", "sampled", "--limit", "1",
                                 "--draws", "2", "--live-models", live,
                                 "--out", os.path.join(out, "cal-refused")])
        check("a calibration refuses on the same dead pair: it spends 396 "
              "calls on the default grid and had no such gate",
              code == 2 and "executor" in text and "groq/%s" % dead in text,
              (code, text[-500:]))
        check("and it refused before drawing anything",
              not glob.glob(os.path.join(out, "cal-refused", "seed-*",
                                         "*.json")),
              sorted(glob.glob(os.path.join(out, "cal-refused", "*", "*"))))
    finally:
        shutil.rmtree(out, ignore_errors=True)

def test_a_configuration_that_destroys_grader_independence_says_so():
    """"Execution grades, and the grader never wrote the code" has to stay true.

    It is already only partly true: `planner` and `test_writer` are one model off
    one spec lineage. A config that also points the Executor there makes it
    false -- the model that wrote the code wrote the suite grading it -- and that
    must not be something a reader has to derive from the table themselves.
    """
    out = tempfile.mkdtemp(prefix="independence-")
    config = {"roles": {"executor": {"provider": "gemini"}}}
    try:
        def run():
            code, text = _sweep(os.path.join(out, "grid"))
            check("a sweep whose Executor shares the Test Writer's pair still "
                  "runs -- this is a warning, not a refusal, because it is a "
                  "legitimate configuration to measure", code == 0, text[-400:])
            check("and it says so at startup, before anything is spent",
                  "grader independence" in text and "executor" in text,
                  [line for line in text.splitlines() if "WARNING" in line])
            summary = _read_summary(os.path.join(out, "grid"))
            warnings = summary["models"]["independence_warnings"]
            check("the warning is in the manifest, so a result read months "
                  "later carries it too", len(warnings) == 2
                  and all("grader independence" in line for line in warnings),
                  warnings)
            check("it names both collisions -- the suite that grades and the "
                  "spec that framed the task -- rather than only the first",
                  sorted("test_writer" in line for line in warnings)
                  == [False, True]
                  and any("planner" in line for line in warnings), warnings)
            records = [_read_json(path) for path in sorted(glob.glob(
                os.path.join(out, "grid", "seed-0", "arm-*", "*.json")))]
            check("and every run JSON carries it, because a single cell read "
                  "on its own is the unit anyone actually looks at",
                  records and all(record["independence_warnings"] == warnings
                                  for record in records),
                  [record["independence_warnings"] for record in records])

        _with_scoped_runs(os.path.join(out, "runs"),
                          lambda: _model_config_scoped(config, run))
        check("the default configuration earns no warning, so this is not a "
              "banner that is always on",
              agents_core.independence_warnings() == [],
              agents_core.independence_warnings())
    finally:
        shutil.rmtree(out, ignore_errors=True)

def test_a_provider_is_a_base_url_and_a_key_and_needs_no_code_edit():
    """The claim the workspace makes about itself, checked rather than asserted.

    An OpenAI-compatible endpoint has to be reachable by configuration alone.
    That was not true when this was written: the key lookup was a two-entry
    literal, so a configured provider resolved fine and then had no key, and
    adding one meant editing `_keys_from_env` after all.
    """
    run_eval = _import_run_eval()
    config = {"providers": {"example": {"base_url": "https://example.invalid/v1",
                                        "model": "example-model-id"}},
              "roles": {"executor": {"provider": "example"}}}

    def run():
        resolved = _resolved_by_role(agents_core.configure_models())
        check("a provider added by configuration resolves, base_url and all",
              resolved["executor"]["provider"] == "example"
              and resolved["executor"]["base_url"]
              == "https://example.invalid/v1"
              and resolved["executor"]["model"] == "example-model-id",
              resolved["executor"])
        check("its key comes from <NAME>_API_KEY, derived rather than "
              "hardcoded, so it has a key channel at all",
              agents_core.key_env("example") == "EXAMPLE_API_KEY",
              agents_core.key_env("example"))
        keys = agents_core.keys_from_env({"EXAMPLE_API_KEY": " secret ",
                                          "GROQ_API_KEY": "g"})
        check("and the lookup covers every configured provider, reading that "
              "variable and stripping it",
              keys == {"example": "secret", "groq": "g", "gemini": ""}, keys)
        check("the CLI's own lookup is the same one, not a second copy that "
              "can drift",
              run_eval._keys_from_env() == agents_core.keys_from_env(), "")
        agents_core.apply_model_config({"providers": {"example":
                                                      {"env": "MY_TOKEN"}}})
        check("a provider may name its own variable, for an endpoint whose key "
              "is not called <NAME>_API_KEY",
              agents_core.key_env("example") == "MY_TOKEN",
              agents_core.key_env("example"))
        check("and the hint that tells a user which variables to set is built "
              "from the configuration, so it cannot go stale",
              "EXAMPLE_API_KEY" not in agents_core.key_env_hint()
              and "MY_TOKEN" in agents_core.key_env_hint()
              and "GROQ_API_KEY" in agents_core.key_env_hint(),
              agents_core.key_env_hint())

    _model_config_scoped(config, run)
    check("the default two keep the two variable names this project has always "
          "used, so nothing about a run today changes",
          agents_core.key_env_hint() == "GEMINI_API_KEY / GROQ_API_KEY",
          agents_core.key_env_hint())

def _config_file_error():
    directory = tempfile.mkdtemp(prefix="model-config-")
    try:
        path = os.path.join(directory, "models.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        try:
            agents_core.load_model_config(
                env={agents_core.MODEL_CONFIG_ENV: path})
        except agents_core.ConfigError as exc:
            return path in str(exc)
        return False
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_bad_model_config_is_refused_whole_rather_than_half_applied():
    """A half-applied config is a run pointed somewhere nobody chose.

    Every rejection below is a shape someone would plausibly write, and the
    message has to say what to do instead. Validated before anything is
    installed, so a rejected config leaves the process exactly as it was --
    which is the property the last check pins.
    """
    cases = [
        ({"rolez": {}}, "unknown model config section"),
        ({"roles": {"harness": {"model": "x"}}}, "unknown role"),
        ({"roles": {"executor": {"provider": "nowhere"}}}, "not configured"),
        ({"roles": {"executor": {"model": 7}}}, "non-string model"),
        ({"roles": {"executor": {"params": 7}}}, "params must be an object"),
        ({"roles": {"executor": {"nonsense": 1}}}, "unknown field"),
        ({"providers": {"x": {}}}, "no base_url"),
        ({"providers": {"x": {"base_url": "u", "temperature": 0.2}}},
         "belongs in that role's"),
        ({"providers": {"x": {"base_url": "u", "env": 7}}}, "non-string env"),
        ({"sampling": {"max_tokens": 4}}, "per-role"),
        ({"providers": {"x": {"base_url": "u"}},
          "roles": {"executor": {"provider": "x"}}}, "resolves to no model"),
        ("not-an-object", "must be a JSON object"),
    ]
    snapshot = lambda: (dict(agents_core.ROLE_PROVIDER),
                        dict(agents_core.ROLE_MODEL),
                        dict(agents_core.ROLE_PARAMS),
                        dict((name, dict(cfg)) for name, cfg
                             in agents_core.PROVIDERS.items()),
                        agents_core.TEMPERATURE, agents_core.TOP_P)
    before = snapshot()
    for config, expected in cases:
        try:
            agents_core.apply_model_config(config)
        except agents_core.ConfigError as exc:
            check("refused, and the message says why (%s)" % expected,
                  expected in str(exc), str(exc))
        else:
            check("refused (%s)" % expected, False, "%r was accepted" % (config,))
    after = snapshot()
    check("and every one of them left the live configuration untouched, so a "
          "refusal cannot half-retarget a run", before == after,
          [(b, a) for b, a in zip(before, after) if b != a])
    check("a config file that is not JSON at all is a refusal naming the path, "
          "not a traceback", _config_file_error(), "")

def test_the_defaults_reproduce_todays_behaviour_exactly():
    """The condition under which none of the above changes any existing check.

    Not "roughly the same": the same provider table, the same role mapping, no
    override at all, and the same two sampling values on the wire. If this drifts
    then every check in this file that predates the configuration layer is
    measuring something slightly different, and the check-count baseline stops
    meaning anything.
    """
    resolved = _resolved_by_role(agents_core.configure_models())
    check("with no configuration present the source is 'defaults'",
          agents_core.model_config_source() == "defaults",
          agents_core.model_config_source())
    check("no role carries an override, which is what makes `model_for` fall "
          "through to the provider's model exactly as the old code did",
          agents_core.ROLE_MODEL == {} and agents_core.ROLE_PARAMS == {},
          (agents_core.ROLE_MODEL, agents_core.ROLE_PARAMS))
    check("and every role resolves to its provider's default model",
          all(entry["model"]
              == agents_core.PROVIDERS[entry["provider"]]["model"]
              for entry in resolved.values()), sorted(resolved.items()))
    check("the role -> provider mapping is the one the pre-registration was "
          "written against",
          agents_core.ROLE_PROVIDER == {"planner": "gemini",
                                        "test_writer": "gemini",
                                        "executor": "groq"},
          agents_core.ROLE_PROVIDER)
    check("sampling is still the two pinned values and nothing else",
          agents_core.sampling_for("executor") == {"temperature": 0.4,
                                                  "top_p": 1.0},
          agents_core.sampling_for("executor"))
    check("and the pair list a preflight would validate is two, because "
          "planner and test_writer share one",
          [(provider, model) for provider, model, _roles
           in agents_core.resolved_pairs()]
          == [("gemini", agents_core.PROVIDERS["gemini"]["model"]),
              ("groq", agents_core.PROVIDERS["groq"]["model"])],
          agents_core.resolved_pairs())
    check("a call with no role at all still gets the provider's model, which "
          "is the four-positional shape every older stand-in was written "
          "against",
          agents_core.model_for(None, "groq")
          == agents_core.PROVIDERS["groq"]["model"],
          agents_core.model_for(None, "groq"))


# The permutation `_task_order` produces for a ten-item list, read off the
# implementation once and pinned here. Hardcoded rather than compared to a second
# call: self-comparison pins determinism only, and every wrong-but-stable
# ordering -- a different digest slice, `random.seed(key)` instead of sha256, the
# key built from the task set instead of the seed -- is perfectly deterministic
# and would pass. If one of these lines has to change, the permutation changed,
# and no sweep recorded under the old one is comparable to a sweep under the new.
_TASK_ORDER_SEED_0 = ["t05", "t06", "t02", "t03", "t00",
                      "t04", "t07", "t01", "t09", "t08"]
_TASK_ORDER_SEED_1 = ["t06", "t00", "t07", "t01", "t08",
                      "t09", "t05", "t04", "t03", "t02"]

# And the same thing for the real 36, as indices into generation order.
_TASK_ORDER_36_SEED_0 = [20, 10, 26, 9, 34, 7, 23, 32, 29, 15, 24, 14,
                         17, 1, 31, 27, 25, 3, 11, 6, 16, 30, 28, 4, 0,
                         13, 18, 12, 22, 21, 19, 2, 8, 5, 35, 33]


def test_the_task_order_is_a_seeded_permutation_not_generation_order():
    run_eval = _import_run_eval()
    gen_tasks = _import_gen_tasks()
    ten = ["t%02d" % index for index in range(10)]
    check("task order matches a hardcoded permutation for a fixed seed",
          run_eval._task_order(ten, 0) == _TASK_ORDER_SEED_0,
          run_eval._task_order(ten, 0))
    check("a different seed gives a different order",
          run_eval._task_order(ten, 1) == _TASK_ORDER_SEED_1
          and _TASK_ORDER_SEED_1 != _TASK_ORDER_SEED_0,
          run_eval._task_order(ten, 1))
    check("the same seed gives the same order twice",
          run_eval._task_order(ten, 0) == run_eval._task_order(ten, 0))
    check("the permutation is keyed on the seed alone, so it does not move when "
          "the caller passes tasks instead of ids",
          [item.task_id for item in run_eval._task_order(
              [_Named(name) for name in ten], 0)] == _TASK_ORDER_SEED_0)

    tasks = gen_tasks.generate(seed=0)
    generated = [task.task_id for task in tasks]
    ordered = [task.task_id for task in run_eval._task_order(tasks, 0)]
    check("the real task set is 36 tasks, so the pinned permutation below is "
          "the one the sweep will use", len(generated) == 36, len(generated))
    check("the 36-task permutation matches a hardcoded expectation",
          [generated.index(name) for name in ordered] == _TASK_ORDER_36_SEED_0,
          [generated.index(name) for name in ordered])
    check("shuffling loses no task and invents none",
          sorted(ordered) == sorted(generated))
    check("and the order really is not generation order, which is family-first "
          "and therefore tier-clustered", ordered != generated, ordered[:4])
    check("nor is it the same order for a different seed",
          [task.task_id for task in run_eval._task_order(tasks, 1)] != ordered)
    check("_task_order does not mutate the list it was handed",
          [task.task_id for task in tasks] == generated)


def test_the_manifest_records_the_order_the_sweep_actually_walked():
    """An order nobody can reconstruct is not a controlled variable."""
    run_eval = _import_run_eval()
    gen_tasks = _import_gen_tasks()
    out = tempfile.mkdtemp(prefix="task-order-")
    try:
        code, text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _sweep(out, extra=("--limit", "3")))
        check("a three-task stub sweep runs", code == 0, text[-400:])
        check("the sweep says on stdout that the order is permuted",
              "task order: seeded permutation from seed 0" in text,
              text[:600])
        recorded = _read_summary(out)["task_order"]
        selected = [task.task_id for task in gen_tasks.generate(seed=0)[:3]]
        expected = [task.task_id
                    for task in run_eval._task_order(gen_tasks.generate(
                        seed=0)[:3], 0)]
        check("the manifest records the seed, the basis and the order",
              recorded["seed"] == 0 and recorded["shuffled"] is True
              and "sha256" in recorded["basis"], recorded)
        check("the recorded order is the permutation the code produces",
              recorded["order"] == expected, recorded["order"])
        check("selection is still generation order, so --limit 3 picks the same "
              "three tasks it always did and the lock still verifies",
              sorted(recorded["order"]) == sorted(selected), selected)
        check("the permutation is recorded as indices too, and they agree with "
              "the order",
              [selected.index(name) for name in recorded["order"]]
              == recorded["permutation"], recorded["permutation"])
        # Order moved; resume keys on the result file name, not on position.
        code2, text2 = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _sweep(out, extra=("--limit", "3")))
        check("a reordered sweep still resumes every cell it already ran",
              code2 == 0 and "0 run(s)" in text2, text2[-400:])
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ------------------------------ sprint 7, task 2: calibration and the D8 gate

def _import_calibrate():
    _import_run_eval()          # puts `eval/` on sys.path
    import calibrate
    return calibrate


def _calibrate(argv):
    """One offline `calibrate.main`, with every global it installs put back.

    `main` turns measurement mode on and drops the retry count to 1, and both are
    process-wide. Leaving them set would silently change every check that ran
    after this one, which is the failure mode `_sweep` already guards against.
    """
    calibrate = _import_calibrate()
    previous_mode = agents_core.MEASUREMENT_MODE
    previous_attempts = agents_core.retry_attempts()
    try:
        return _capture_streams(lambda: calibrate.main(list(argv)))
    finally:
        agents_core.set_measurement_mode(previous_mode)
        agents_core.set_retry_attempts(previous_attempts)
        agents_core.reset_call_log()


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _calibration_manifest(root, seed=0):
    return _read_json(os.path.join(root, "seed-%d" % seed, "calibration.json"))


def _numeric_literals(path):
    """Every numeric constant in `path`, by AST rather than by grep.

    A grep for "30" also hits `%-30s`, a sha256 prefix and any comment; the
    point of the check below is specifically about numbers the interpreter reads.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(
                node.value, (int, float)) and not isinstance(node.value, bool):
            found.add(node.value)
    return found


def _call_roles(events):
    """`{(role, provider): count}` over the `call` events of a run."""
    tally = {}
    for event in events:
        if event.get("kind") != "call":
            continue
        key = (event.get("role"), event.get("used"))
        tally[key] = tally.get(key, 0) + 1
    return tally


def test_every_calibration_draw_is_recorded_and_costs_one_planner_call():
    """`d_t` is a ratio, so a draw that never happened must not read as a zero.

    And the cost claim, which is the whole reason this mode is separate: one
    Gemini call per task and nothing else on that provider, because calibration
    never gates and so never resolves the visible suite. Asserted twice -- once
    off the manifest's own accounting and once off the event log -- since a
    manifest that miscounted its own calls would agree with itself.
    """
    calibrate = _import_calibrate()
    out = tempfile.mkdtemp(prefix="calib-draws-")
    try:
        code, text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _calibrate(["--stub", "sampled", "--limit", "2",
                                "--out", out]))
        check("an offline calibration sweep reaches a verdict",
              code in (0, 1), text[-500:])
        manifest = _calibration_manifest(out)
        per_task = manifest["per_task"]
        draws = calibrate.CALIBRATION_DRAWS
        check("the sweep covered the two tasks it selected",
              len(per_task) == 2, [e["task_id"] for e in per_task])
        check("every task records all ten draws",
              all(e["draws_stored"] == draws and e["draws_requested"] == draws
                  for e in per_task),
              [(e["task_id"], e["draws_stored"]) for e in per_task])
        check("each draw is kept individually, pass or fail, so a missing draw "
              "is distinguishable from a failed one",
              all(len(e["draw_passed"]) == draws
                  and None not in e["draw_passed"] for e in per_task),
              [e["draw_passed"] for e in per_task])
        check("d_t is the share of those ten graded draws that passed",
              all(e["graded"] == draws
                  and abs(e["d_t"] - e["passed"] / float(draws)) < 1e-9
                  for e in per_task),
              [(e["d_t"], e["passed"], e["graded"]) for e in per_task])
        check("and all ten draw files are on disk, one per draw",
              all(os.path.exists(calibrate.draw_path(out, 0, e["task_id"], i))
                  for e in per_task for i in range(1, draws + 1)),
              sorted(os.listdir(os.path.join(out, "seed-0", "draws"))))
        spent = manifest["actual_calls_by_provider"]
        check("the manifest's accounting is one gemini call per task and ten "
              "groq draws each", spent == {"gemini": 2, "groq": 2 * draws},
              spent)
        roles = _call_roles(_read_events(
            os.path.join(out, "seed-0", "events.jsonl")))
        check("the event log agrees: one planner call per task, on gemini",
              roles.get(("planner", "gemini")) == 2, roles)
        check("no other gemini call is made at all -- the test writer is never "
              "reached, because calibration never gates",
              not [key for key in roles
                   if key[1] == "gemini" and key[0] != "planner"], roles)
        check("and every draw is an executor call on groq",
              roles.get(("executor", "groq")) == 2 * draws, roles)
        normalized = " ".join(text.split())
        check("the per-provider cost is projected before the first draw is taken",
              text.index("projected cost") < text.index("d_t = "), text[:300])
        check("and the projection names the counts, per provider, against a free "
              "tier that is per provider",
              "gemini 2 call(s)" in normalized
              and "groq %d call(s)" % (2 * draws) in normalized, text[:1200])
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_calibration_output_can_never_land_in_the_grid():
    """Ten A' draws inside `results/` would be read as arm cells and pooled.

    `load_records` walks `results/seed-N/arm-*/` and loads every `*.json` it
    finds; nothing about a calibration draw would look wrong to it, and a pooled
    A' pass rate built partly out of ten-draw calibration cells would move every
    number downstream without anything raising. So it is checked from both ends:
    the writer refuses the path, and the tree it does write holds nothing the
    grid's loader can see.
    """
    calibrate = _import_calibrate()
    run_eval = _import_run_eval()
    results = os.path.abspath(run_eval.RESULTS_DIR)
    default = os.path.abspath(calibrate.CALIBRATION_DIR)
    check("the default calibration root is a sibling of the grid's results, not "
          "a child",
          default != results and not default.startswith(results + os.sep),
          default)
    for label, path in (
            ("the results root itself", results),
            ("a seed directory inside it", os.path.join(results, "seed-0")),
            ("an arm directory inside that",
             os.path.join(results, "seed-0", "arm-a_prime"))):
        code, text = _calibrate(["--stub", "perfect", "--limit", "1",
                                 "--draws", "2", "--out", path])
        check("--out pointing at %s is refused before any call" % label,
              code == 2 and "refusing to run" in text
              and "not grid data" in text, text[-400:])
    out = tempfile.mkdtemp(prefix="calib-notgrid-")
    try:
        code, text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _calibrate(["--stub", "perfect", "--limit", "1",
                                "--draws", "2", "--out", out]))
        check("a sweep into its own root runs", code in (0, 1), text[-400:])
        check("the grid's own loader finds nothing in a calibration tree",
              run_eval.load_records(out, 0, run_eval.ARM_ORDER) == [],
              sorted(os.listdir(os.path.join(out, "seed-0"))))
        check("because there is no arm-* directory for it to walk",
              not glob.glob(os.path.join(out, "seed-0", "arm-*")),
              sorted(os.listdir(os.path.join(out, "seed-0"))))
        check("and the manifest says on its face that it is not grid data",
              "must not be pooled with eval/results/"
              in _calibration_manifest(out)["not_grid_data"],
              _calibration_manifest(out)["not_grid_data"])
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_the_calibration_band_is_open_at_both_ends():
    """0.1 and 0.9 are reachable at ten draws, and each is one draw from useless.

    A closed band would admit a task that went 1-for-10 -- indistinguishable from
    impossible except by the single sample that happened to land -- and keeping
    those out of the grid is the entire job of the gate.
    """
    calibrate = _import_calibrate()
    run_eval = _import_run_eval()
    draws = calibrate.CALIBRATION_DRAWS
    check("the endpoints are exactly values that ten draws can produce",
          draws == 10 and round(1.0 / draws, 3) == calibrate.BAND_LOW
          and round(9.0 / draws, 3) == calibrate.BAND_HIGH,
          (calibrate.BAND_LOW, calibrate.BAND_HIGH))
    check("1-for-10 is out of band, not the lowest point in it",
          calibrate.in_band(calibrate.BAND_LOW) is False)
    check("9-for-10 is out of band, not the highest point in it",
          calibrate.in_band(calibrate.BAND_HIGH) is False)
    check("2-for-10 and 8-for-10 are in band",
          calibrate.in_band(0.2) is True and calibrate.in_band(0.8) is True)
    check("floor and ceiling are out of band",
          not calibrate.in_band(0.0) and not calibrate.in_band(1.0))
    check("an unmeasured task is not in band and does not raise",
          calibrate.in_band(None) is False)
    one_of_ten = [{"outcome": run_eval.OUTCOME_GRADED, "passed": index == 0}
                  for index in range(draws)]
    check("a 1-in-10 draw record really does compute to exactly the endpoint",
          calibrate.calibration_rate(one_of_ten)
          == (calibrate.BAND_LOW, 1, draws),
          calibrate.calibration_rate(one_of_ten))
    edges = [{"task_id": "low", "tier": 1, "d_t": calibrate.BAND_LOW},
             {"task_id": "high", "tier": 1, "d_t": calibrate.BAND_HIGH},
             {"task_id": "inside", "tier": 2, "d_t": 0.5}]
    verdict = calibrate.gate(edges, 1)
    check("the gate files both endpoints as floor and ceiling, not as in-band",
          verdict["in_band"] == 1 and verdict["at_floor"] == 1
          and verdict["at_ceiling"] == 1
          and verdict["in_band_task_ids"] == ["inside"], verdict)
    check("the per-tier breakdown files them the same way, so the columns and "
          "the total cannot disagree",
          verdict["by_tier"]["1"]["in_band"] == 0
          and verdict["by_tier"]["1"]["at_floor"] == 1
          and verdict["by_tier"]["1"]["at_ceiling"] == 1
          and verdict["by_tier"]["2"]["in_band"] == 1, verdict["by_tier"])
    check("the recorded verdict says the interval is open, for a later reader "
          "of the JSON",
          verdict["band"] == [calibrate.BAND_LOW, calibrate.BAND_HIGH]
          and verdict["band_is_open_interval"] is True, verdict)
    _, printed = _capture_streams(lambda: calibrate.print_verdict(verdict, 36))
    check("and the printed verdict says it too, with the endpoints named",
          "open" in printed and "(0.1, 0.9)" in printed
          and "are excluded" in printed, printed[:400])


def test_the_gate_threshold_is_one_named_constant_printed_and_recorded():
    """The registered gate says 12 of 30; the code generates 36 tasks.

    Restating a pre-registered threshold is the user's decision and not the
    code's, so the number lives in one named constant that the manifest records
    rather than in an `if` where nobody would find it again. These checks are
    what stop it drifting back into one.
    """
    calibrate = _import_calibrate()
    check("the threshold is a named module-level constant",
          isinstance(calibrate.GATE_MIN_IN_BAND, int)
          and calibrate.GATE_MIN_IN_BAND == 15, calibrate.GATE_MIN_IN_BAND)
    literals = _numeric_literals(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "eval", "calibrate.py"))
    check("neither registered number appears as a literal anywhere in the "
          "module, so the restatement cannot hide in a comparison",
          12 not in literals and 30 not in literals, sorted(literals))
    check("the registered wording is carried verbatim instead of being rounded "
          "away", "12 of 30" in calibrate.GATE_AS_REGISTERED,
          calibrate.GATE_AS_REGISTERED)
    entries = [{"task_id": "t%d" % index, "tier": 1, "d_t": 0.5}
               for index in range(3)]
    check("the gate compares against the threshold it is handed, not a baked one",
          calibrate.gate(entries, 3)["go"] is True
          and calibrate.gate(entries, 4)["go"] is False,
          [calibrate.gate(entries, n)["go"] for n in (3, 4)])
    out = tempfile.mkdtemp(prefix="calib-threshold-")
    try:
        code, text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _calibrate(["--stub", "sampled", "--limit", "2",
                                "--out", out]))
        check("the printed verdict names the constant and its value",
              "GATE_MIN_IN_BAND = %d in-band task(s) required"
              % calibrate.GATE_MIN_IN_BAND in text, text[-1800:])
        check("and prints the registered gate beside it, unresolved, with the "
              "locked task count that makes the restatement visible",
              calibrate.GATE_AS_REGISTERED in text
              and "the locked task set has 36 tasks" in text, text[-1800:])
        manifest = _calibration_manifest(out)
        check("the manifest records the constant's name, its default and the "
              "value actually used",
              manifest["threshold"] == {
                  "constant": "GATE_MIN_IN_BAND",
                  "default": calibrate.GATE_MIN_IN_BAND,
                  "used": calibrate.GATE_MIN_IN_BAND,
                  "overridden": False,
                  "as_registered": calibrate.GATE_AS_REGISTERED},
              manifest["threshold"])
        found = manifest["gate"]["in_band"]
        code_low, low = _calibrate(["--gate-only", "--limit", "2",
                                    "--min-in-band", str(found), "--out", out])
        code_high, high = _calibrate(["--gate-only", "--limit", "2",
                                      "--min-in-band", str(found + 1),
                                      "--out", out])
        check("an override moves the verdict, and says on the line that it is "
              "an override",
              code_low == 0 and code_high == 1
              and "overridden; default is %d" % calibrate.GATE_MIN_IN_BAND
              in low, (code_low, code_high, low[-700:]))
        expected = "in band: %d of 2" % found
        check("the same stored draws produce both verdicts, so it is the "
              "threshold moving and not the data",
              expected in low and expected in high,
              (low[-400:], high[-400:]))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_a_deterministic_stub_is_a_no_go_and_says_what_that_means():
    """The correct output for a degenerate input -- and it must not be a table row.

    A stub whose draws never vary puts every task at floor or ceiling, so the
    NO-GO is right rather than a bug. The gate only does anything if the person
    reading it acts on it, so the interpretation is a plain sentence on its own
    line and the process exits non-zero.
    """
    out = tempfile.mkdtemp(prefix="calib-nogo-")
    try:
        code, text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _calibrate(["--stub", "perfect", "--limit", "2",
                                "--out", out]))
        manifest = _calibration_manifest(out)
        check("a stub whose draws never vary puts every task at the ceiling",
              all(entry["d_t"] == 1.0 for entry in manifest["per_task"]),
              [(e["task_id"], e["d_t"]) for e in manifest["per_task"]])
        check("so nothing is in band and the verdict is NO-GO",
              manifest["gate"]["in_band"] == 0
              and manifest["gate"]["go"] is False, manifest["gate"])
        check("a NO-GO exits 1: a real answer, never mistakable for a pass",
              code == 1, code)
        sentences = [line for line in text.splitlines()
                     if line.startswith("NO-GO:")]
        check("the interpretation is one plain sentence on its own line, not a "
              "warning buried in the table", len(sentences) == 1, text[-900:])
        check("and it says the set is at floor or ceiling and must be "
              "regenerated or re-tiered before k=1",
              "at floor or ceiling" in sentences[0]
              and "regenerated or re-tiered" in sentences[0]
              and "before k=1" in sentences[0], sentences)
        check("the stub says up front that it measures nothing about any model "
              "and that its verdict is a correct NO-GO on a degenerate input",
              "measures nothing about any model" in text
              and "degenerate input" in text, text[:1400])
        check("the manifest names the stub, so a stub run can never be read as "
              "a real one", manifest["stub"] == "perfect"
              and manifest["pacing"]["governor_applied"] is False,
              (manifest["stub"], manifest["pacing"]["governor_applied"]))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_calibration_refuses_a_lock_it_cannot_verify_and_records_the_digest():
    """A calibration measured against a task set that moved cannot clear a grid.

    Worse than useless if it reports a GO: the gate would have cleared a set
    nobody is going to run. The lock digest on the manifest is what makes that
    checkable afterwards, and the refusal is what stops a free tier's whole day
    being spent on the wrong tasks.
    """
    gen_tasks = _import_gen_tasks()
    out = tempfile.mkdtemp(prefix="calib-lock-")
    try:
        code, text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _calibrate(["--stub", "perfect", "--limit", "1",
                                "--draws", "2", "--out", out]))
        check("a sweep against the real lock runs and says it verified it",
              code in (0, 1) and "tasks.lock verified" in text, text[:300])
        manifest = _calibration_manifest(out)
        locked = gen_tasks.load_lock()
        check("the manifest records the lock's body digest verbatim",
              manifest["tasks_lock"]["body_sha256"] == locked["body_sha256"]
              and len(locked["body_sha256"]) == 64, manifest["tasks_lock"])
        check("and the locked task count, the seed and the self-check time with "
              "it",
              manifest["tasks_lock"]["locked_tasks"]
              == locked["counts"]["tasks"]
              and manifest["tasks_lock"]["seed"] == locked["generator"]["seed"]
              and manifest["tasks_lock"]["self_check_at"]
              == locked["self_check"]["at"], manifest["tasks_lock"])
        check("the manifest records the model IDs read off the provider table, "
              "not restated ones",
              manifest["models_used"] == dict(
                  (name, agents_core.PROVIDERS[name]["model"])
                  for name in sorted(set(agents_core.ROLE_PROVIDER.values()))),
              manifest["models_used"])
        refused = os.path.join(out, "refused")
        code_missing, missing_text = _lock_scoped(
            os.path.join(out, "no-such-lock.json"),
            lambda: _calibrate(["--stub", "perfect", "--limit", "1",
                                "--draws", "2", "--out", refused]))
        check("no lock at all: refuse with exit 2 rather than measure",
              code_missing == 2 and "refusing to run" in missing_text,
              missing_text[-400:])
        edited = os.path.join(out, "edited-lock.json")
        body = json.loads(json.dumps(locked))
        body["body_sha256"] = "0" * 64
        with open(edited, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
        code_edited, edited_text = _lock_scoped(
            edited,
            lambda: _calibrate(["--stub", "perfect", "--limit", "1",
                                "--draws", "2", "--out", refused]))
        check("a lock whose digest was hand-edited: refuse with exit 2 and say "
              "why",
              code_edited == 2 and "refusing to run" in edited_text,
              edited_text[-700:])
        check("a refused sweep writes nothing and spends nothing",
              not os.path.exists(os.path.join(refused, "seed-0")),
              sorted(os.listdir(refused)) if os.path.isdir(refused)
              else "absent")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_a_resumed_calibration_redraws_only_what_is_missing():
    """All ten draws must answer one spec, or `d_t` is a number about two of them.

    So the plan is stored and reused rather than re-requested -- which also means
    a sweep that dies at draw 7 does not spend a second Planner call, on the
    provider whose free tier is the binding constraint, just to finish.
    """
    calibrate = _import_calibrate()
    out = tempfile.mkdtemp(prefix="calib-resume-")
    try:
        code, _text = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _calibrate(["--stub", "sampled", "--limit", "2",
                                "--out", out]))
        first = _calibration_manifest(out)
        plans = sorted(glob.glob(
            os.path.join(out, "seed-0", "plans", "*.json")))
        specs_before = [_read_json(path)["spec_sha256"] for path in plans]
        removed = []
        for entry in first["per_task"]:
            for index in (7, 8):
                path = calibrate.draw_path(out, 0, entry["task_id"], index)
                os.remove(path)
                removed.append(path)
        code2, text2 = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _calibrate(["--stub", "sampled", "--limit", "2",
                                "--out", out]))
        second = _calibration_manifest(out)
        normalized = " ".join(text2.split())
        check("the resumed sweep projects only the draws that are missing",
              "gemini 0 call(s)" in normalized
              and "groq %d call(s)" % len(removed) in normalized, text2[:1000])
        check("and spends no planner call at all, because the plan is reused",
              second["actual_calls_by_provider"].get("gemini", 0) == 0
              and second["actual_calls_by_provider"].get("groq")
              == len(removed), second["actual_calls_by_provider"])
        check("the stored specs are untouched, so all ten draws still answer "
              "one spec",
              [_read_json(path)["spec_sha256"] for path in plans]
              == specs_before, specs_before)
        check("every draw is back, and d_t is identical to the unresumed sweep",
              [(e["task_id"], e["d_t"], e["draws_stored"])
               for e in second["per_task"]]
              == [(e["task_id"], e["d_t"], e["draws_stored"])
                  for e in first["per_task"]],
              [(e["task_id"], e["d_t"]) for e in second["per_task"]])
        code3, _text3 = _with_scoped_runs(
            os.path.join(out, "runs"),
            lambda: _calibrate(["--stub", "sampled", "--limit", "2",
                                "--out", out]))
        skips = [event for event in _read_events(
            os.path.join(out, "seed-0", "events.jsonl"))
            if event["kind"] == "resume_skip"]
        check("a fully-drawn task is skipped, and the skip is an event rather "
              "than silence",
              len(skips) == 2 and code3 == code and code2 == code,
              (len(skips), code, code2, code3))
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# sprint 8, task 3: is the ranking heuristic's depth element order-invariant?
#
# Measured, and the measurement is what these checks are about. The ranking key
# itself is untouched -- that is a registered mechanism and changing it is a
# registration decision -- so what has to be established here is that the number
# reported about it is trustworthy: that a genuinely order-dependent suite is
# caught, that a clean one is not smeared, that the fraction comes off the frozen
# suites rather than out of a fixture, and that the result is somewhere D2 can cite.
# ---------------------------------------------------------------------------

def _import_rank_battery():
    _import_run_eval()          # puts `eval/` on sys.path
    import rank_battery
    return rank_battery


def _synthetic_task(gen_tasks, task_id, reference, tests, names):
    """A `Task` that is not from the frozen set, for the controlled cases only.

    Named `_synthetic_` on purpose. The *fraction* must never come from one of
    these -- that is the whole point of check three -- but "an order-dependent
    suite is detected" needs a suite that is order-dependent, and the frozen set
    has none, which is itself the finding.
    """
    return gen_tasks.Task(family="synthetic", tier=1, variant=1, task_id=task_id,
                          prompt="", names=tuple(names), reference=reference,
                          tests=tests, params={}, seed=0)


# Module state, so check two's answer depends on check one having run. The
# smallest honest instance of the thing the depth heuristic's premise rules out.
_ORDER_DEPENDENT_REFERENCE = (
    "_seen = []\n"
    "\n"
    "def visits():\n"
    "    _seen.append(1)\n"
    "    return len(_seen)\n"
)
_ORDER_DEPENDENT_CHECKS = [["assert visits() == 1"],
                           ["assert visits() == 2"],
                           ["assert visits() == 3"]]
_PURE_REFERENCE = (
    "def double(n):\n"
    "    return n * 2\n"
)
_PURE_CHECKS = [["assert double(1) == 2"],
                ["assert double(2) == 4"],
                ["assert double(3) == 6"]]

def test_a_genuinely_order_dependent_suite_is_detected():
    """The positive control, and it has to be synthetic because none of the 36 are.

    The battery's whole claim is that it can tell "this suite's checks depend on
    each other" from "reordering moved which check happened to fail first", and a
    detector that reports zero is indistinguishable from a detector that is
    broken. So: a solution with module-level state, whose second check can only
    pass if the first already ran. Detection is per candidate, and the *reference*
    is the one that must be flagged here -- the wrong implementations fail
    identically whatever the order.

    Built through `gen_tasks.bake_checks`, so the control carries the same
    per-check wrapper shape the frozen suites do. Hand-rolled flat asserts would
    now be skipped for having zero permutable units, and a skipped positive
    control reports nothing while looking like it ran.
    """
    gen_tasks = _import_gen_tasks()
    battery = _import_rank_battery()
    task = _synthetic_task(gen_tasks, "order-dependent-00",
                           _ORDER_DEPENDENT_REFERENCE,
                           gen_tasks.bake_checks(("visits",),
                                                 _ORDER_DEPENDENT_CHECKS),
                           ("visits",))
    record = battery.measure_task(task, battery.MODE_ASSERTS, 0, samples=6)
    flagged = record.get("order_dependent") or []
    check("a genuinely order dependent suite is detected as order dependent",
          "reference" in flagged,
          "units=%r permuted=%r flagged=%r skipped=%r"
          % (record.get("units"), record.get("permuted"), flagged,
             record.get("skipped")))


def test_an_order_independent_suite_is_not_falsely_flagged():
    """The negative control. A pure function, three independent checks, nothing to find.

    This is the half that decides whether 0/36 means anything. The first shape of
    this measurement said 36/36 because it asked "did the blamed line move", which
    it always does; the isolation test replaced it. A suite of independent checks
    over a stateless solution must come back clean for *both* the correct
    implementation and the stub, and neither may show an outcome change either.
    """
    gen_tasks = _import_gen_tasks()
    battery = _import_rank_battery()
    task = _synthetic_task(gen_tasks, "order-independent-00", _PURE_REFERENCE,
                           gen_tasks.bake_checks(("double",), _PURE_CHECKS),
                           ("double",))
    record = battery.measure_task(task, battery.MODE_ASSERTS, 0, samples=6)
    flagged = record.get("order_dependent") or []
    moved = record.get("outcome_changed") or []
    check("an order independent suite is not falsely flagged",
          record.get("permuted") and not flagged and not moved,
          "units=%r permuted=%r flagged=%r outcome_changed=%r"
          % (record.get("units"), record.get("permuted"), flagged, moved))

def test_the_reported_fraction_comes_from_the_frozen_suites():
    """Not a fixture: the population is the lock's, and the totals are a function of it.

    Four things, all of which would fail if the number came from anywhere else.
    The task set is rebuilt from the lock's own `generator` block and its digest
    matches the live lock. The recorded measurement is against that same digest --
    Task 1 moved six of these, so a fraction paired with the wrong body is not
    evidence. Every locked suite appears in the records, none extra. And the
    published totals are recomputed here from the per-suite records rather than
    trusted, so a hand-edited headline cannot survive.

    The round-trip is the fifth and it is the one everything else rests on: the
    identity permutation must re-emit each frozen suite byte for byte, because
    every number in the document is a line number read out of a re-emitted file.
    """
    gen_tasks = _import_gen_tasks()
    battery = _import_rank_battery()
    lock = gen_tasks.load_lock()
    document = battery.load_result()
    problems = []
    if lock is None:
        problems.append("no tasks.lock")
    if document is None:
        problems.append("no recorded measurement")
    if not problems:
        tasks, provenance = battery.locked_tasks()
        body = lock.get("body_sha256")
        if provenance.get("body_sha256") != body:
            problems.append("locked_tasks() provenance %r != lock %r"
                            % (provenance.get("body_sha256"), body))
        recorded = document.get("measured_against", {}).get("body_sha256")
        if recorded != body:
            problems.append("measured against body %r, lock is now %r"
                            % (recorded, body))
        locked_ids = set(task.task_id for task in tasks)
        for mode in battery.MODES:
            block = document.get("modes", {}).get(mode) or {}
            records = block.get("tasks") or []
            seen = set(r.get("task_id") for r in records)
            if seen != locked_ids:
                problems.append("%s: measured %d suites, lock has %d (%r)"
                                % (mode, len(seen), len(locked_ids),
                                   sorted(seen ^ locked_ids)[:3]))
            if battery.totals_for(records) != block.get("totals"):
                problems.append("%s: totals are not derived from the records"
                                % mode)
            for task in tasks:
                parts = battery.units(task.tests, mode)
                if parts is None:
                    continue
                identity = list(range(len(parts[1])))
                if battery.render(task.tests, mode, identity)[0] != task.tests:
                    problems.append("%s: %s does not round-trip"
                                    % (mode, task.task_id))
    check("the reported fraction is derived from the frozen suites, not a fixture",
          not problems, "; ".join(problems[:4]))


def test_the_frozen_suite_measurement_is_reproducible():
    """And re-measurable: the identity-order facts, recomputed off the frozen suites.

    Separate from the check above on purpose. That one asks whether the document
    is internally consistent and pinned to the right lock; this one re-runs the
    measurement over the first few locked suites and compares the fields that do
    not depend on how many permutations were sampled -- the candidate list, the
    depths and blamed checks under the identity order, which candidates lost, and
    which one `_retain_best` would keep. A document copied forward from an older
    task set passes the consistency check and fails this one.
    """
    battery = _import_rank_battery()
    document = battery.load_result()
    problems = []
    if document is None:
        problems.append("no recorded measurement")
    else:
        tasks, _provenance = battery.locked_tasks()
        mode = battery.MODE_ASSERTS
        by_id = dict((r.get("task_id"), r)
                     for r in document.get("modes", {}).get(mode, {})
                     .get("tasks") or [])
        stable = ("units", "candidates", "losers", "depths", "blamed_units",
                  "kinds", "retained_under_identity")
        for task in tasks[:3]:
            fresh = battery.measure_task(task, mode, document.get("seed", 0),
                                         samples=2)
            was = by_id.get(task.task_id)
            if was is None:
                problems.append("%s absent from the record" % task.task_id)
                continue
            for field in stable:
                if fresh.get(field) != was.get(field):
                    problems.append("%s: %s re-measured %r, recorded %r"
                                    % (task.task_id, field, fresh.get(field),
                                       was.get(field)))
    check("the frozen suite measurement reproduces on a re-run",
          not problems, "; ".join(problems[:3]))

def _gitignore_hides(path):
    """Would `.gitignore` swallow this path? Read off the file, no git invocation."""
    import fnmatch
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(root, ".gitignore")) as handle:
            lines = handle.read().splitlines()
    except IOError:
        return False
    for raw in lines:
        pattern = raw.strip()
        if not pattern or pattern.startswith("#") or pattern.startswith("!"):
            continue
        if pattern.endswith("/"):
            if path == pattern.rstrip("/") or path.startswith(pattern):
                return True
            continue
        if fnmatch.fnmatch(path, pattern) \
                or fnmatch.fnmatch(os.path.basename(path), pattern):
            return True
    return False


def test_the_battery_result_is_recorded_where_d2_can_reference_it():
    """A measurement D2 cannot cite is not a measurement. Six properties of the record.

    It exists on disk beside the lock rather than inside read-only `eval/prereg/`.
    It is not gitignored, so it travels with the protocol -- `runs/`,
    `eval/results/` and `eval/calibration/` are all excluded and this would be
    easy to lose the same way. It names the ranking keys it measured and where its
    candidates came from. It carries both modes' headline fractions *and* the
    named suite lists behind them, because "7 of 36" is not a citation and "7 of
    36, these seven" is. The neighbours are present: a fraction whose stability
    under hash seed, seed re-roll and sample count is unmeasured is not ready to
    be registered.

    And the stamp says `current`. That is the sixth property and it is the one
    that was missing: the previous result sat on disk measured against a
    superseded lock, every reader took it for current, and its retention
    fractions were quoted as a baseline for suites that no longer existed. A
    record whose stamp is stale is not citable, so this check fails on it.
    """
    battery = _import_rank_battery()
    relative = os.path.relpath(battery.RESULT_PATH,
                               os.path.dirname(os.path.abspath(__file__)))
    document = battery.load_result()
    problems = []
    if document is None:
        problems.append("no %s; run rank_battery.py --write" % relative)
    else:
        if _gitignore_hides(relative.replace(os.sep, "/")):
            problems.append("%s is gitignored" % relative)
        if os.sep + "prereg" + os.sep in battery.RESULT_PATH:
            problems.append("written inside read-only eval/prereg/")
        for field in ("ranking_keys", "shipped_key", "candidate_source", "seed",
                      "permutations_requested", "measured_against", "stamp",
                      "supersedes"):
            # `in`, not truthiness: seed 0 is the default and is not missing.
            if field not in document or document[field] is None:
                problems.append("no %s recorded" % field)
        status = (document.get("stamp") or {}).get("status")
        if status != battery.STAMP_CURRENT:
            problems.append("stamp is %r, not %r -- the recorded result was "
                            "measured against another lock"
                            % (status, battery.STAMP_CURRENT))
        for mode in battery.MODES:
            headline = document.get("headline", {}).get(mode) or {}
            wanted = ["order_dependent"]
            for key in battery.KEYS:
                wanted += ["%s.%s" % (key, name) for name in
                           ("merit_flipped", "rank_flipped",
                            "retention_changed")]
            missing = [name for name in wanted if name not in headline]
            if missing:
                problems.append("%s headline lacks %r" % (mode, missing))
            totals = document.get("modes", {}).get(mode, {}).get("totals") or {}
            for key in battery.KEYS:
                block = (totals.get("keys") or {}).get(key) or {}
                for name in ("merit_flipped", "retention_changed"):
                    fraction = block.get(name, {}).get("suites")
                    named = block.get(name + "_suites")
                    if named is None or len(named) != fraction:
                        problems.append("%s %s %s: %r suites but %r named"
                                        % (mode, key, name, fraction, named))
        neighbours = document.get("neighbours") or {}
        absent = [key for key in ("python_hash_seed", "seed_reroll",
                                  "sample_saturation") if key not in neighbours]
        if absent:
            problems.append("neighbours missing %r; re-run with --neighbours"
                            % absent)
    check("the battery result is recorded where D2 can reference it",
          not problems, "; ".join(problems[:4]))


def test_a_stale_battery_result_cannot_read_as_current():
    """The stamp is a guard, not a record. Three ways it has to refuse.

    This is the failure that actually happened: `ranking_battery.json` sat on disk
    stamped with the pre-re-lock body, the check above only asked whether the field
    was *present*, and the retention fractions inside it were quoted as a baseline
    for suites the lock no longer described. A record that names a superseded lock
    has to be loud about it, not merely honest if asked.

    So all three legs, and the middle one is the one that makes the guard a guard:

      * `stamp_status` classifies stale, current and unverifiable, and the banner
        appears for exactly the first two.
      * `--verify-stamp` exits non-zero on a stale record. Same posture as
        `--verify-lock`: the check is worth nothing if a script can ignore it.
      * `locked_tasks` refuses to measure at all when the working tree disagrees
        with the lock, because a fresh measurement of drifted suites carrying the
        old lock's digest is a new stale record rather than a rescue.
    """
    battery = _import_rank_battery()
    gen_tasks = _import_gen_tasks()
    live = battery.lock_body_sha256()
    out = tempfile.mkdtemp(prefix="maw-stamp-")
    try:
        stale_lock = os.path.join(out, "stale.lock")
        with open(stale_lock, "w", encoding="utf-8") as handle:
            json.dump({"body_sha256": "0" * 64}, handle)

        status, why = battery.stamp_status({"measured_against":
                                            {"body_sha256": live}})
        check("a result measured against the lock on disk is current, and says "
              "nothing", status == battery.STAMP_CURRENT
              and battery._stamp_banner(status, why) == [], (status, why))

        status, why = battery.stamp_status(
            {"measured_against": {"body_sha256": live}}, lock_path=stale_lock)
        banner = battery._stamp_banner(status, why)
        check("a result whose lock body does not match the lock on disk is stale, "
              "and the banner names both digests and forbids citing it",
              status == battery.STAMP_STALE and len(banner) == 5
              and "STALE MEASUREMENT" in banner[1]
              and "do not use them as a baseline" in banner[2].lower()
              and live[:12] in why and "0" * 12 in why,
              (status, why, banner[1:3]))

        for document, label in (
                (None, "no recorded measurement at all"),
                ({}, "a record with no measured_against block"),
                ({"measured_against": {}}, "a record naming no lock body")):
            status, why = battery.stamp_status(document)
            banner = battery._stamp_banner(status, why)
            check("%s is unverified, not current" % label,
                  status == battery.STAMP_UNKNOWN and why
                  and "UNVERIFIED MEASUREMENT" in banner[1],
                  (status, why))

        # The CLI leg, with the two file readers redirected. `--verify-stamp` runs
        # before `locked_tasks` on purpose -- a drifted tree is one of the ways a
        # record goes stale, so this leg has to work on a tree that cannot measure.
        held = (battery.load_result, battery.lock_body_sha256)
        buffer = io.StringIO()
        try:
            battery.load_result = lambda path=None: {
                "measured_against": {"body_sha256": "1" * 64}}
            battery.lock_body_sha256 = lambda path=None: "2" * 64
            with contextlib.redirect_stdout(buffer):
                code = battery.main(["--verify-stamp"])
            check("--verify-stamp exits non-zero on a stale record and shouts on "
                  "the way out",
                  code == 1 and "STALE MEASUREMENT" in buffer.getvalue(),
                  (code, buffer.getvalue()[:160]))
            battery.lock_body_sha256 = lambda path=None: "1" * 64
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = battery.main(["--verify-stamp"])
            check("and exits zero, quietly, when the record is current",
                  code == 0 and "STALE" not in buffer.getvalue()
                  and "current" in buffer.getvalue(),
                  (code, buffer.getvalue()[:160]))
        finally:
            battery.load_result, battery.lock_body_sha256 = held

        # A lock that is internally consistent -- its own body digest recomputed --
        # but which no longer describes what the generator produces. The body-digest
        # leg cannot catch this one, which is why the refusal has to come off
        # `verify_lock`'s per-task comparison.
        drifted = copy.deepcopy(gen_tasks.load_lock())
        drifted["tasks"][0]["tests_sha256"] = "3" * 64
        drifted["body_sha256"] = gen_tasks.body_digest(drifted)
        drifted_path = os.path.join(out, "drifted.lock")
        with open(drifted_path, "w", encoding="utf-8") as handle:
            json.dump(drifted, handle)
        refusal = ""
        try:
            battery.locked_tasks(drifted_path)
        except SystemExit as exit_error:
            refusal = str(exit_error)
        check("locked_tasks refuses to measure suites the lock does not describe, "
              "and names the task and the part that moved",
              "REFUSING TO MEASURE" in refusal
              and drifted["tasks"][0]["task_id"] in refusal
              and "tests" in refusal,
              refusal[:200])

        hand_edited = copy.deepcopy(gen_tasks.load_lock())
        hand_edited["tasks"][0]["tests_sha256"] = "4" * 64
        edited_path = os.path.join(out, "edited.lock")
        with open(edited_path, "w", encoding="utf-8") as handle:
            json.dump(hand_edited, handle)
        refusal = ""
        try:
            battery.locked_tasks(edited_path)
        except SystemExit as exit_error:
            refusal = str(exit_error)
        check("and refuses a hand-edited lock on its own body digest too, so the "
              "two ways a lock can lie are both closed",
              "REFUSING TO MEASURE" in refusal and "body_sha256" in refusal,
              refusal[:200])
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_the_battery_measures_the_shipped_ranking_key():
    """It ranks with `agents_core`'s key, and this sprint's change to that key is pinned.

    The failure mode worth guarding is a battery that quietly ranks with its own
    private copy: every number it reports would then be about the copy. So the
    module must call `agents_core._candidate_rank` and `_candidate_merit` and must
    not define either name itself. It *does* hold a frozen copy of the
    pre-Sprint-9 key, deliberately, under the name `_depth_rank` -- that is the
    baseline column and it is the one thing that must not be reached for through
    `agents_core`, because `agents_core` no longer has it.

    The second half re-asserts the shipped key's elements from the outside:
    APPROVED over not, more checks passed over fewer, an assertion failure over
    other failures at an equal count, earlier round over later. Element 2 used to
    be the failing assert's *line*; it is now a count of checks passed, which is
    what this sprint changed and what the battery exists to justify. The last
    ordering is the one the brief drew a line under: partial credit ranks
    candidates and must never reach the verdict, so an APPROVED candidate that
    reports no checks at all still outranks a losing one that passed almost all
    of them.
    """
    battery = _import_rank_battery()
    with open(battery.__file__.replace(".pyc", ".py")) as handle:
        source = handle.read()
    problems = []
    for name in ("_candidate_rank", "_candidate_merit"):
        if "agents_core.%s" % name not in source:
            problems.append("does not call agents_core.%s" % name)
        if "\ndef %s(" % name in source:
            problems.append("defines its own %s" % name)
    losing = lambda kind, passed, rnd: {"verdict": harness.VERDICT_REVISE,
                                       "failure_kind": kind,
                                       "failed_assertion_line": 40,
                                       "checks_trusted": True,
                                       "checks_passed": passed,
                                       "checks_total": 10,
                                       "round": rnd}
    approved = {"verdict": harness.VERDICT_APPROVED,
                "failure_kind": harness.FAIL_NONE,
                "failed_assertion_line": None, "round": 9,
                "checks_trusted": False, "checks_passed": 0, "checks_total": 0}
    many = losing(harness.FAIL_ASSERTION, 8, 1)
    few = losing(harness.FAIL_ASSERTION, 2, 1)
    runtime = losing(harness.FAIL_RUNTIME, 8, 1)
    later = losing(harness.FAIL_ASSERTION, 8, 7)
    unknown = losing(harness.FAIL_ASSERTION, 0, 1)
    unknown["checks_trusted"] = False
    none_passed = losing(harness.FAIL_ASSERTION, 0, 1)
    rank, merit = agents_core._candidate_rank, agents_core._candidate_merit
    ordered = [(rank(approved) > rank(many),
                "approved does not beat a candidate that passed most checks"),
               (rank(many) > rank(few),
                "passing more checks does not beat passing fewer"),
               (rank(many) > rank(runtime),
                "an assertion failure does not beat a runtime failure at an "
                "equal check count"),
               (rank(many) > rank(later),
                "an earlier round does not break the tie"),
               (rank(none_passed) > rank(unknown),
                "a measured zero does not beat an unknown count"),
               (merit(many) == rank(many)[:3],
                "merit is no longer rank without the round")]
    problems.extend(why for ok, why in ordered if not ok)
    check("the battery measures the shipped ranking key and the key is unchanged",
          not problems, "; ".join(problems[:4]))


# ------------------------------------------------- the Executor probe (sprint 9)

def _import_pin_executor():
    _import_run_eval()          # puts `eval/` on sys.path
    import pin_executor
    return pin_executor


def test_a_provider_extension_parameter_is_sent_and_recorded():
    """`reasoning_format` has to reach the wire *and* the record, and nothing else.

    Measured 2026-08-30: gpt-oss-20b with `reasoning_format` unset returned zero
    fenced blocks while 568 characters went to a separate `reasoning` field, so the
    Executor's extractor saw no code at all. The parameter was unsendable before
    this -- `sampling_for` fed `**params` straight into `create()`, and an
    unrecognised keyword is a TypeError raised inside the SDK, wrapped as a
    ProviderError, and reported as "groq failed": a configuration typo wearing a
    provider outage's clothes.

    The direction of the split is the load-bearing part. Unknown must go to
    `extra_body`, not to a keyword argument.
    """
    keywords, extra = agents_core.split_params(
        {"temperature": 0.4, "top_p": 1.0, "reasoning_format": "hidden"})
    check("a known sampling parameter stays a typed keyword argument",
          keywords == {"temperature": 0.4, "top_p": 1.0}, keywords)
    check("and a provider extension rides in extra_body instead of becoming a "
          "TypeError the retry layer would read as a provider outage",
          extra == {"reasoning_format": "hidden"}, extra)
    check("nothing is dropped and nothing is invented: the two halves are the "
          "whole", dict(keywords, **extra) == {"temperature": 0.4, "top_p": 1.0,
                                               "reasoning_format": "hidden"},
          (keywords, extra))
    check("with today's defaults extra_body is empty, which is what makes this "
          "split invisible to every call the pipeline already makes",
          agents_core.split_params(agents_core.sampling_for("executor"))[1] == {},
          agents_core.sampling_for("executor"))

    pin = _import_pin_executor()
    held = (dict(agents_core.ROLE_MODEL), dict(agents_core.ROLE_PARAMS),
            agents_core.model_config_source())
    try:
        resolved = pin.configure("openai/gpt-oss-20b")
        row = [entry for entry in resolved if entry["role"] == "executor"][0]
        check("configuring a candidate puts reasoning_format in the resolved "
              "table beside temperature and top_p, so the manifest records it "
              "rather than the run smuggling it",
              row.get("reasoning_format") == "hidden"
              and row["model"] == "openai/gpt-oss-20b", sorted(row.items()))
        check("and it goes through the role's params, so `sampling_for` -- the "
              "one function the record and the wire both read -- carries it",
              agents_core.sampling_for("executor")
              == {"temperature": 0.4, "top_p": 1.0,
                  "reasoning_format": "hidden"},
              agents_core.sampling_for("executor"))
    finally:
        agents_core.ROLE_MODEL.clear()
        agents_core.ROLE_MODEL.update(held[0])
        agents_core.ROLE_PARAMS.clear()
        agents_core.ROLE_PARAMS.update(held[1])
        agents_core._MODEL_CONFIG_SOURCE = held[2]

    held = agents_core.call_model_detailed
    try:
        agents_core.call_model_detailed = lambda *a, **k: {
            "text": "  hello  ", "usage": {"total_tokens": 3}}
        returned = agents_core.call_model("groq", "k" * 12, "sys", "usr")
    finally:
        agents_core.call_model_detailed = held
    check("`call_model` still returns a plain string, which is the shape every "
          "stand-in in these suites replaces it with",
          returned == "  hello  " and isinstance(returned, str), repr(returned))


def _pin_stub(counter, fail_on=None):
    """A `call_model_detailed` stand-in with a usage block shaped like Groq's.

    `reasoning_tokens` is nested under `completion_tokens_details`, which is where
    Groq actually puts it and which a hand-written field list would have missed.
    """
    def stub(provider, api_key, system, user, role=None):
        counter.append(role)
        model = agents_core.model_for(role, provider)
        if fail_on and model in fail_on:
            raise agents_core.ProviderError("%s failed: boom" % provider,
                                            status=500, exc_class="APIError")
        params = agents_core.sampling_for(role)
        return {"text": ("```python\ndef f(x):\n    return x\n```"
                         if role == "executor" else _PIN_PLAN),
                "model_requested": model,
                "model_returned": model,
                "usage": {"prompt_tokens": 11, "completion_tokens": 22,
                          "total_tokens": 33,
                          "completion_tokens_details": {"reasoning_tokens": 5}},
                "finish_reason": "stop", "params": params,
                "extra_body": agents_core.split_params(params)[1],
                "reasoning_chars": 0}
    return stub


_PIN_PLAN = ("SPEC\nA function f.\n\nSTEP 1: write it\n\nTESTS\n"
             "```python\nfrom solution import f\nassert f(1) == 1\n```\n")


def _pin_harness(pin, counter, fail_on=None):
    """Install the stand-ins one probe run needs, and hand back a restorer."""
    held = (agents_core.call_model_detailed, agents_core.preflight_pair,
            pin.ask_keys, agents_core.MEASUREMENT_MODE, agents_core.PACER,
            dict(agents_core.ROLE_MODEL), dict(agents_core.ROLE_PARAMS))
    agents_core.call_model_detailed = _pin_stub(counter, fail_on)
    agents_core.preflight_pair = (
        lambda p, m, k, call=None, params=None: (True, None, ""))
    pin.ask_keys = lambda providers, reader=None: dict(
        (name, "k" * 12) for name in providers)

    def restore():
        (agents_core.call_model_detailed, agents_core.preflight_pair,
         pin.ask_keys) = held[:3]
        agents_core.set_measurement_mode(held[3])
        agents_core.set_pacer(held[4])
        agents_core.ROLE_MODEL.clear()
        agents_core.ROLE_MODEL.update(held[5])
        agents_core.ROLE_PARAMS.clear()
        agents_core.ROLE_PARAMS.update(held[6])
    return restore


def test_the_executor_probe_resumes_without_redrawing():
    """108 calls that die at 90 must resume at 90, and must not cache a sample.

    Those two requirements pull against each other, which is why the resume key is
    the thing to check rather than the fact that resuming works. A key over
    (prompt, model, params, provider) would make a repeated *draw* a cache hit and
    silently collapse k independent samples into one -- the standing prohibition in
    `_attempt_provider`. A key over the unit of work cannot, because this probe
    defines exactly one unit per (task, candidate).

    Also checked here because they are the same run: a half-written final line is
    re-run rather than repaired, and a failed unit stays in the file as an audit
    trail while still being re-run.
    """
    pin = _import_pin_executor()
    check("the resume key is the unit of work and names nothing about the "
          "request -- no prompt, no model, no sampling parameters",
          pin.unit_key({"kind": "draw", "task_id": "t1", "candidate": "m",
                        "spec": "IGNORED", "replies": ["IGNORED"]})
          == ("draw", "t1", "m"),
          pin.unit_key({"kind": "draw", "task_id": "t1", "candidate": "m"}))

    root = tempfile.mkdtemp(prefix="pin-resume-")
    counter = []
    restore = _pin_harness(pin, counter)
    agents_core.set_pacing(True, sleep=lambda seconds: None)
    argv = ["--out", root, "--limit", "2", "--candidate", pin.CANDIDATES[0]]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as first:
            code = pin.main(argv)
        ledger = pin.ledger_path(root)
        units, malformed = pin.load_ledger(ledger)
        check("a first run completes every unit: one plan and one draw per task",
              code == 0 and len(units) == 4 and malformed == 0
              and all(pin.is_done(record) for record in units.values()),
              (code, sorted(units), malformed))
        spent = len(counter)
        check("and it actually spent calls, so the next check is not vacuous",
              spent >= 4, (spent, counter))

        with contextlib.redirect_stdout(io.StringIO()) as second:
            pin.main(argv)
        check("resuming the same ledger draws nothing at all",
              len(counter) == spent, len(counter) - spent)
        check("and says so per unit rather than silently doing nothing",
              second.getvalue().count("resumed") == 4,
              second.getvalue().count("resumed"))
        check("the preview states the outstanding spend, which on a complete "
              "ledger is zero of each provider",
              "0 plan unit(s) x 1-2 Gemini calls + 0 Groq call(s)"
              in second.getvalue(),
              [line for line in second.getvalue().splitlines()
               if "spend" in line])
        check("the first run's preview stated the real outstanding spend",
              "2 plan unit(s) x 1-2 Gemini calls + 2 Groq call(s)"
              in first.getvalue(),
              [line for line in first.getvalue().splitlines()
               if "spend" in line])

        # A kill -9 mid-append leaves a partial final line. It is a unit that did
        # not complete, so the correct repair is to draw it again.
        with open(ledger, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines(True)
        with open(ledger, "w", encoding="utf-8") as handle:
            handle.write("".join(lines[:-1]) + lines[-1][:len(lines[-1]) // 2])
        units, malformed = pin.load_ledger(ledger)
        check("a truncated final line is counted as incomplete, not parsed as a "
              "unit", malformed == 1 and len(units) == 3, (malformed, len(units)))
        with contextlib.redirect_stdout(io.StringIO()) as third:
            pin.main(argv)
        check("and exactly that one unit is re-drawn -- not the whole run",
              len(counter) == spent + 1, len(counter) - spent)
        check("with the dropped line reported rather than swallowed",
              "1 incomplete line(s) dropped" in third.getvalue(),
              third.getvalue()[:200])
    finally:
        restore()
        shutil.rmtree(root, ignore_errors=True)


def test_the_probe_records_the_accounting_that_hidden_makes_invisible():
    """What each call cost and what actually answered it, per call, in the file.

    `reasoning_format="hidden"` suppresses the *reporting* of reasoning, not its
    computation, so the tokens are billed while being absent from the reply. And
    "we asked for gpt-oss-20b" is not the same claim as "gpt-oss-20b answered": a
    provider may serve an alias or a dated snapshot, and pinning a model is a claim
    about the second one.

    The nesting matters. Groq puts `reasoning_tokens` inside
    `completion_tokens_details`, so a field list written by hand would have
    recorded a usage block with no reasoning in it and looked complete.
    """
    pin = _import_pin_executor()
    root = tempfile.mkdtemp(prefix="pin-usage-")
    counter = []
    restore = _pin_harness(pin, counter)
    agents_core.set_pacing(True, sleep=lambda seconds: None)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            pin.main(["--out", root, "--limit", "1"])
        units, _malformed = pin.load_ledger(pin.ledger_path(root))
        draws = [record for key, record in units.items()
                 if key[0] == pin.KIND_DRAW]
        check("one draw per candidate, so the comparison is paired on one plan",
              len(draws) == len(pin.CANDIDATES)
              and len(set(record["task_id"] for record in draws)) == 1,
              [(r["task_id"], r["candidate"]) for r in draws])
        reply = draws[0]["replies"][0]
        check("the usage block is stored whole, so the nested reasoning_tokens "
              "survives instead of being filtered out by a hand-written list",
              reply["usage"]["completion_tokens_details"]["reasoning_tokens"] == 5
              and reply["usage"]["total_tokens"] == 33, reply["usage"])
        check("reasoning_format is on the recorded params and in extra_body, so "
              "the record says what the wire carried",
              reply["params"].get("reasoning_format") == "hidden"
              and reply["extra_body"] == {"reasoning_format": "hidden"},
              (reply["params"], reply["extra_body"]))
        check("and it is on the call log entry too, beside temperature and top_p",
              all(entry.get("reasoning_format") == "hidden"
                  for record in draws for entry in record["calls"]
                  if entry["role"] == "executor"),
              [entry for record in draws for entry in record["calls"]])
        check("the model the API returned is recorded next to the one requested, "
              "for every draw",
              all(set(["model_requested", "model_returned"]) <= set(reply)
                  for record in draws for reply in record["replies"]),
              sorted(reply))
        check("every executor call went to groq under measurement mode, so no "
              "429 could have quietly had a Gemini model answer for a Groq one",
              all(entry["used"] == "groq" and entry["measurement_mode"]
                  for record in draws for entry in record["calls"]
                  if entry["role"] == "executor"),
              [(e["role"], e["used"], e["measurement_mode"])
               for r in draws for e in r["calls"]])
        summary = pin.summarise(units)
        check("the summary sums the nested reasoning tokens rather than dropping "
              "them",
              summary["candidates"][pin.CANDIDATES[0]]["usage"].get(
                  "completion_tokens_details.reasoning_tokens") == 5,
              summary["candidates"][pin.CANDIDATES[0]]["usage"])
        check("and the report names the reasoning spend out loud",
              "reasoning_tokens" in out.getvalue(),
              [line for line in out.getvalue().splitlines() if "usage" in line])
    finally:
        restore()
        shutil.rmtree(root, ignore_errors=True)


def test_the_probe_refuses_a_dead_candidate_before_it_generates():
    """A 404 must cost one preflight call, not a run.

    This project has lost two pinned models to retirement, and the second -- the
    Groq slug that is the Executor in all four arms -- was found by a grid rather
    than by a preflight. The Planner's pair is validated too: a probe that checked
    only the three Groq IDs would still die 36 Gemini calls in.
    """
    pin = _import_pin_executor()
    root = tempfile.mkdtemp(prefix="pin-dead-")
    counter = []
    restore = _pin_harness(pin, counter)
    agents_core.set_pacing(True, sleep=lambda seconds: None)
    try:
        dead = pin.CANDIDATES[1]
        agents_core.preflight_pair = lambda p, m, k, call=None, params=None: (
            (False, 404, "model_not_found: %s" % m) if m == dead
            else (True, None, ""))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = pin.main(["--out", root, "--limit", "1"])
        check("the run refuses rather than starting", code == 1, code)
        check("and spends no generation call at all", counter == [], counter)
        check("no ledger is written, so a refused run leaves nothing to resume "
              "from", not os.path.exists(pin.ledger_path(root)),
              os.listdir(root))
        check("the preflight names every pair it validated, the Planner's "
              "included", out.getvalue().count("-> ") >= 1 + len(pin.CANDIDATES),
              out.getvalue())

        records = pin.preflight({"gemini": "k" * 12, "groq": "k" * 12})
        check("and it is one call per pair: the Planner's, plus one per candidate",
              [record["model"] for record in records]
              == [agents_core.model_for("planner")] + list(pin.CANDIDATES),
              [record["model"] for record in records])
        problems = pin.preflight_failures(records)
        check("the refusal line names the role, the provider, the model and the "
              "status, which is the message a bare '404' cost this project a "
              "grid for not having",
              len(problems) == 1 and dead in problems[0]
              and "404" in problems[0] and "executor" in problems[0]
              and "groq" in problems[0], problems)

        missing = pin.preflight({"groq": "k" * 12})
        check("a missing key is a failure here and not a skip",
              [record["ok"] for record in missing][0] is False
              and "no API key" in missing[0]["detail"], missing[0])
    finally:
        restore()
        shutil.rmtree(root, ignore_errors=True)


def test_a_preflight_validates_the_parameters_the_role_will_actually_send():
    """A pair is not the only thing one call can be rejected for.

    `reasoning_format="hidden"` is a Groq extension, it rides in `extra_body`, and
    it is on every Executor call this probe makes -- it is the reason the probe has
    its present shape at all. A preflight that sent only the slug asked "does this
    pair answer" when the question is "does this pair answer the call this run
    sends", so a 400 on the extension would have arrived at unit 1 with the
    preflight already green. Moving that failure earlier is the entire job of a
    preflight.

    The floor still wins: `max_tokens=1` and `temperature=0` are applied last, so
    validating the real shape cannot make the validation expensive.
    """
    pin = _import_pin_executor()
    sent = agents_core.preflight_params(
        {"reasoning_format": "hidden", "temperature": 0.7, "top_p": 0.95,
         "max_tokens": 4096})
    check("the role's own parameters reach the preflight call",
          sent.get("reasoning_format") == "hidden" and sent.get("top_p") == 0.95,
          sent)
    check("under a cheap floor, so a role configured with a large budget cannot "
          "make a preflight expensive",
          (sent["max_tokens"], sent["temperature"]) == (1, 0.0), sent)
    check("and PREFLIGHT_PARAMS is not mutated by having been merged into",
          agents_core.PREFLIGHT_PARAMS == {"max_tokens": 1, "temperature": 0.0},
          agents_core.PREFLIGHT_PARAMS)
    check("no params is the old behaviour exactly, so a caller that has none to "
          "give sends the floor and nothing else",
          agents_core.preflight_params() == agents_core.PREFLIGHT_PARAMS
          and agents_core.preflight_params() is not agents_core.PREFLIGHT_PARAMS,
          agents_core.preflight_params())

    seen = []

    def capture(provider, key, slug, system, user, params):
        seen.append({"provider": provider, "model": slug, "params": params})
        return "1"

    ok, status, detail = agents_core.preflight_pair(
        "groq", "some-slug", "k" * 12, call=capture,
        params={"reasoning_format": "hidden", "temperature": 0.7})
    check("the seam is handed the resolved parameters, not just the pair",
          (ok, status, detail) == (True, None, "") and len(seen) == 1
          and seen[0]["params"] == {"reasoning_format": "hidden",
                                    "max_tokens": 1, "temperature": 0.0}, seen)
    check("and the extension splits into extra_body rather than into a keyword "
          "argument, which is the shape a real call sends",
          agents_core.split_params(seen[0]["params"])
          == ({"max_tokens": 1, "temperature": 0.0},
              {"reasoning_format": "hidden"}),
          agents_core.split_params(seen[0]["params"]))

    held = (dict(agents_core.ROLE_MODEL), dict(agents_core.ROLE_PARAMS))
    try:
        pin.configure(pin.CANDIDATES[0])
        planner_provider = agents_core.ROLE_PROVIDER["planner"]
        keys = {planner_provider: "k" * 12, "groq": "k" * 12}
        del seen[:]
        records = agents_core.preflight(
            keys, roles=("planner", "test_writer", "executor"), call=capture)
        by_model = dict((entry["model"], entry["params"]) for entry in seen)
        check("every pair a run would call is validated with its own roles' "
              "parameters",
              [record["ok"] for record in records] == [True] * len(records)
              and len(by_model) == len(records), (records, seen))
        check("the Executor's pair carries reasoning_format, because the "
              "Executor's calls do",
              by_model.get(pin.CANDIDATES[0], {}).get("reasoning_format")
              == "hidden", by_model)
        check("the Planner's pair does not, because the Planner's calls do not "
              "-- a preflight sends what its own roles send and nothing more",
              "reasoning_format" not in by_model.get(
                  agents_core.model_for("planner"), {}), by_model)

        del seen[:]
        records = pin.preflight(keys, call=capture)
        by_model = dict((entry["model"], entry["params"]) for entry in seen)
        check("the probe's own preflight sends the Executor's parameters too, "
              "so a candidate is never called reachable on a shape no draw uses",
              [record["ok"] for record in records] == [True] * len(records)
              and all(by_model[slug].get("reasoning_format") == "hidden"
                      for slug in pin.CANDIDATES), by_model)
        check("with the floor still applied per pair",
              all((by_model[slug]["max_tokens"], by_model[slug]["temperature"])
                  == (1, 0.0) for slug in by_model), by_model)

        run_eval = _import_run_eval()
        stub = run_eval.StubModel(live_models=[agents_core.model_for("planner")])
        check("and the stub that stands in for a provider offline takes the "
              "parameters as well, so --stub and --bad-slug exercise the same "
              "signature a live preflight does",
              stub.probe(planner_provider, None,
                         agents_core.model_for("planner"),
                         agents_core.PREFLIGHT_SYSTEM, agents_core.PREFLIGHT_USER,
                         {"reasoning_format": "hidden"}) == "1")
        stubbed = agents_core.preflight(
            {planner_provider: "k" * 12, "groq": "k" * 12},
            roles=("planner", "executor"), call=stub.probe)
        check("and a stubbed preflight still refuses the dead slug through that "
              "same widened seam",
              [record["ok"] for record in stubbed] == [True, False]
              or [record["ok"] for record in stubbed] == [False, True],
              [(record["model"], record["ok"], record["status"])
               for record in stubbed])
    finally:
        agents_core.ROLE_MODEL.clear()
        agents_core.ROLE_MODEL.update(held[0])
        agents_core.ROLE_PARAMS.clear()
        agents_core.ROLE_PARAMS.update(held[1])


def _replay_source_ledger(pin, task_ids):
    """A source ledger holding one usable plan per id in `task_ids`.

    Written through `pin.append` so it is a real ledger and not a fixture shaped
    like one: the replay has to survive `load_ledger`, digest re-derivation and the
    resume key, and a hand-built dict would skip all three.
    """
    root = tempfile.mkdtemp(prefix="pin-replay-src-")
    path = pin.ledger_path(root)
    suites = {}
    for index, task_id in enumerate(task_ids):
        tests = ("from solution import f\nassert f(%d) == %d\n"
                 % (index, index))
        suites[task_id] = tests
        pin.append(path, {
            "kind": pin.KIND_PLAN, "task_id": task_id, "candidate": "",
            "at": "2026-08-30T00:00:0%dZ" % index, "ok": True,
            # A PEP 604 union in the spec, deliberately: these are the specs the
            # sprint exists to re-draw and the replay must not filter them out.
            "spec": "Write f(x: int | None) -> int | None.",
            "steps": ["write f"], "tests": tests,
            "tests_status": "generated", "tests_trusted": True,
            "tests_sha256": agents_core.sha256_of(tests),
            "planner_provider": "gemini"})
    return root, path, suites


def test_replaying_stored_plans_spends_nothing_on_the_planner():
    """`--replay-plans`: the same specs, a fixed harness, and no second Planner draw.

    The point of the mode is that a re-measurement after a harness fix is only
    interpretable if the specs are byte-identical to the ones the first ledger's
    draws answered. A fresh Planner call would silently turn "same spec, better
    harness" into "different spec, different harness", so zero Planner calls is
    the invariant and it is asserted, not assumed. The suite is carried with it and
    its digest re-derived from the copied text, because a suite that does not hash
    to its recorded digest makes the first ledger's gate verdicts unreadable.
    """
    pin = _import_pin_executor()
    tasks = pin.locked_tasks()[:3]
    ids = [task.task_id for task in tasks]
    src_root, src_path, suites = _replay_source_ledger(pin, ids[:2])
    dst = tempfile.mkdtemp(prefix="pin-replay-out-")
    counter, asked = [], []
    restore = _pin_harness(pin, counter)
    pin.ask_keys = lambda providers, reader=None: (
        asked.extend(providers) or dict((name, "k" * 12) for name in providers))
    agents_core.set_pacing(True, sleep=lambda seconds: None)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = pin.main(["--replay-plans", src_path, "--out", dst,
                             "--limit", "3", "--candidate", pin.CANDIDATES[0]])
        text = out.getvalue()
        units, malformed = pin.load_ledger(pin.ledger_path(dst))
        plans = dict((key[1], record) for key, record in units.items()
                     if key[0] == pin.KIND_PLAN)
        draws = dict((key[1], record) for key, record in units.items()
                     if key[0] == pin.KIND_DRAW)
        check("the replay completes", code == 0 and malformed == 0, (code, malformed))
        check("not one Planner-side call was made, which is the whole invariant",
              [role for role in counter
               if role in pin.REPLAY_ROLES] == [], counter)
        check("and it did spend Executor calls, so the check above is not vacuous",
              counter == ["executor"] * 2, counter)
        check("only the Groq key is asked for -- a replay has no Planner leg to "
              "spend one on", asked == ["groq"], asked)
        check("a plan record exists for each replayed task and for no other",
              sorted(plans) == ids[:2], sorted(plans))
        check("the third task is skipped rather than planned, and said out loud",
              ids[2] not in plans and ids[2] not in draws
              and "will be skipped" in text and ids[2] in text,
              [line for line in text.splitlines() if "skip" in line])
        for task_id in ids[:2]:
            record = plans[task_id]
            check("the suite is carried over byte for byte (%s)" % task_id,
                  record["tests"] == suites[task_id], record["tests"])
            check("its digest is copied intact and still describes the text (%s)"
                  % task_id,
                  record["tests_sha256"] == agents_core.sha256_of(suites[task_id])
                  == record["replayed_tests_sha256"],
                  (record["tests_sha256"], record["replayed_tests_sha256"]))
            check("status and trust come across too, so the gate behaves as it "
                  "did (%s)" % task_id,
                  (record["tests_status"], record["tests_trusted"])
                  == ("generated", True),
                  (record["tests_status"], record["tests_trusted"]))
            check("the source ledger is named in the record (%s)" % task_id,
                  record["replayed_from"] == os.path.abspath(src_path)
                  and record["replayed_at"] == "2026-08-30T00:00:0%dZ"
                  % ids.index(task_id), record.get("replayed_from"))
            check("and the record shows it cost nothing (%s)" % task_id,
                  record["calls"] == [] and record["replies"] == [],
                  (record["calls"], record["replies"]))
            check("the union-bearing spec is replayed, not filtered out (%s)"
                  % task_id,
                  "int | None" in record["spec"]
                  and [flag["kind"] for flag in record["spec_runtime_syntax"]]
                  == [agents_core.UNION_FLAG], record["spec_runtime_syntax"])
        check("the draw was gated by the replayed suite, so the digest in the "
              "draw matches the plan's",
              all(draws[task_id]["tests_status"] == "generated"
                  and draws[task_id]["tests_trusted"] is True
                  for task_id in ids[:2]),
              [(k, v["tests_status"]) for k, v in draws.items()])

        # The guard inside `_run`, on the branch `main` normally filters away.
        # Checked directly because "never plan in replay mode" has to hold for
        # every caller, not only for the one that pre-filters the task list.
        before = len(counter)
        with contextlib.redirect_stdout(io.StringIO()) as guard_out:
            pin._run(tasks[2:], (pin.CANDIDATES[0],), {"groq": "k" * 12},
                     pin.ledger_path(dst), {}, replay_plans={},
                     replay_path=src_path)
        check("a task with no stored spec is skipped by the runner too, and "
              "spends nothing",
              len(counter) == before
              and "no spec in the source ledger" in guard_out.getvalue(),
              guard_out.getvalue())
    finally:
        restore()
        shutil.rmtree(src_root, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)

    bad = {"kind": pin.KIND_PLAN, "task_id": "tampered-01", "ok": True,
           "spec": "s", "tests": "assert True\n", "tests_status": "generated",
           "tests_trusted": True, "tests_sha256": "0" * 64}
    try:
        pin.replayed_plan_unit(bad, src_path)
        check("a suite that does not hash to its recorded digest is refused, "
              "not replayed", False, "no refusal")
    except SystemExit as exc:
        check("a suite that does not hash to its recorded digest is refused, "
              "not replayed",
              "tampered-01" in str(exc) and "REFUSING TO REPLAY" in str(exc),
              str(exc))

    check("a source ledger lends only the plans that actually hold a spec",
          sorted(pin.replayable_plans({
              (pin.KIND_PLAN, "with", ""): {"task_id": "with", "ok": True,
                                            "spec": "s"},
              (pin.KIND_PLAN, "empty", ""): {"task_id": "empty", "ok": True,
                                             "spec": "  "},
              (pin.KIND_PLAN, "failed", ""): {"task_id": "failed", "ok": False,
                                              "spec": "s"},
              (pin.KIND_DRAW, "with", "m"): {"task_id": "with", "ok": True}}))
          == ["with"],
          sorted(pin.replayable_plans({})))


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
        test_the_no_gate_rate_is_a_first_class_diagnostic_before_the_grid,
        test_the_no_approval_rate_is_surfaced_beside_the_no_gate_rate,
        test_a_suite_that_rejects_every_hidden_pass_is_counted_as_a_wrong_suite,
        test_the_wrong_suite_count_runs_over_an_already_written_ledger,
        # sprint 6, task 1: surviving rate limits honestly
        test_a_rate_limit_retries_to_the_bound_and_stays_infrastructure,
        test_the_servers_own_retry_after_beats_our_guess,
        test_a_dead_slug_hard_fails_on_the_first_call_in_both_modes,
        test_the_backoff_schedule_is_reproducible_from_the_run_id,
        test_the_pacer_delays_a_repeat_call_to_the_same_provider,
        test_a_rate_limited_cell_is_never_a_model_failure,
        test_an_infra_loss_excludes_its_whole_task_and_is_counted,
        test_a_resumed_cell_makes_no_calls_and_records_why,
        # sprint 6, task 2: freeze the hidden suite, prove it never leaks
        test_the_task_lock_pins_the_hidden_suite,
        test_a_moved_hidden_suite_is_named_by_task_and_part,
        test_a_hand_edited_lock_is_caught_two_ways,
        test_a_run_refuses_to_start_without_a_verified_lock,
        test_writing_a_lock_requires_a_passing_self_check,
        test_the_self_check_runs_a_battery_not_one_stub,
        test_the_lock_records_the_battery_and_a_moved_battery_voids_it,
        test_every_battery_finding_holds_by_name_and_no_suite_passes_the_broken_stub,
        test_each_check_reports_its_own_outcome_and_its_own_exception_type,
        test_a_missing_entry_point_is_not_zero_of_n,
        test_partial_credit_never_reaches_a_verdict,
        test_only_the_grader_and_the_stub_read_the_hidden_suite,
        test_the_hidden_suite_digest_travels_on_the_record,
        test_the_leakage_audit_extracts_what_it_claims_to,
        test_no_prompt_in_a_whole_sweep_carries_the_hidden_suite,
        # sprint 6, task 3: log every event, permute the task order
        test_every_event_kind_round_trips_through_the_writer,
        test_a_newline_in_a_field_cannot_forge_a_second_event,
        test_the_writer_redacts_keys_and_raw_exceptions_itself,
        test_a_failed_call_reaches_the_event_log_without_its_message,
        test_the_event_log_is_complete_up_to_an_abrupt_death,
        test_a_sweep_killed_mid_grid_still_explains_itself,
        test_a_four_arm_sweep_extends_the_record_and_attributes_every_event,
        # sprint 8, task 2: models are configuration, resolved and recorded
        test_a_role_can_be_pointed_at_its_own_model_and_the_call_goes_there,
        test_a_same_provider_alternate_is_expressible_but_deliberately_not_wired,
        test_the_resolved_mapping_reaches_the_run_json_and_both_manifests,
        test_the_preflight_refuses_a_dead_pair_and_passes_a_live_one,
        test_a_configuration_that_destroys_grader_independence_says_so,
        test_a_provider_is_a_base_url_and_a_key_and_needs_no_code_edit,
        test_a_bad_model_config_is_refused_whole_rather_than_half_applied,
        test_the_defaults_reproduce_todays_behaviour_exactly,
        test_the_task_order_is_a_seeded_permutation_not_generation_order,
        test_the_manifest_records_the_order_the_sweep_actually_walked,
        # sprint 7, task 2: the calibration sweep and the go/no-go gate
        test_every_calibration_draw_is_recorded_and_costs_one_planner_call,
        test_calibration_output_can_never_land_in_the_grid,
        test_the_calibration_band_is_open_at_both_ends,
        test_the_gate_threshold_is_one_named_constant_printed_and_recorded,
        test_a_deterministic_stub_is_a_no_go_and_says_what_that_means,
        test_calibration_refuses_a_lock_it_cannot_verify_and_records_the_digest,
        test_a_resumed_calibration_redraws_only_what_is_missing,
        # sprint 8, task 3: the ranking heuristic's depth element, measured
        test_a_genuinely_order_dependent_suite_is_detected,
        test_an_order_independent_suite_is_not_falsely_flagged,
        test_the_reported_fraction_comes_from_the_frozen_suites,
        test_the_frozen_suite_measurement_is_reproducible,
        test_the_battery_result_is_recorded_where_d2_can_reference_it,
        test_a_stale_battery_result_cannot_read_as_current,
        test_the_battery_measures_the_shipped_ranking_key,
        # sprint 9, task 4: the Executor probe
        test_a_provider_extension_parameter_is_sent_and_recorded,
        test_the_executor_probe_resumes_without_redrawing,
        test_the_probe_records_the_accounting_that_hidden_makes_invisible,
        test_the_probe_refuses_a_dead_candidate_before_it_generates,
        test_a_preflight_validates_the_parameters_the_role_will_actually_send,
        test_replaying_stored_plans_spends_nothing_on_the_planner,
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

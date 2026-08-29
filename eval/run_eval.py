"""The four arms on the family-first task set, graded only by the hidden suite.

    a         one Executor call on the raw prompt: no plan, no tests, no repair
    a_prime   the Planner's SPEC only, one Executor call, no gate, no repair
    a_prime3  three independent a_prime draws, arm B's gate, best-of-3
    b         mode 3: Planner, audited suite, Executor, harness, ladder

`a_prime3` is the control that separates "repair works" from "extra lottery
tickets": it spends arm B's Executor budget with none of its feedback. If B is
no better than A'@3, the repair loop is resampling with extra steps.

Every arm is graded by the same function against the same hidden suite from
`gen_tasks.py`. Arm B's own generated suite decides what it *repairs* against
and A'@3's gate decides which draw it keeps; neither decides whether a task
passed. A pipeline allowed to grade itself will report whatever it likes, and
the gap between "the harness said APPROVED" and "it passes the hidden suite" is
the number this script exists to measure.

    python3 eval/run_eval.py --arm all --per-family 2
    python3 eval/run_eval.py --arm b --family interval_logic --verbose
    python3 eval/run_eval.py --stub flaky --arm all       # no keys, no network

Measurement mode is forced on: each call is pinned to one provider and a dead
provider fails the task instead of silently rerouting the grid to the other one.

`--stub` replaces the provider call with a local generator and measures nothing
about any model. It is there to prove the plumbing -- resume, grading, the
false-APPROVED accounting, the governor -- without spending a call.

Keys come from GEMINI_API_KEY and GROQ_API_KEY. Results land one JSON file per
(seed, arm, task, repeat) under eval/results/, and an existing file is skipped
rather than rerun, so an interrupted run resumes by being started again.
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import agents_core            # noqa: E402
import harness                # noqa: E402
import gen_tasks              # noqa: E402

RESULTS_DIR = os.path.join(_HERE, "results")

# Calls per minute, per provider, enforced client-side. Deliberately under the
# published ceilings: the point is never to be told about the limit, because a
# 429 costs a round trip and pollutes the wall-clock numbers.
DEFAULT_RATES = {"gemini": 12.0, "groq": 25.0}
BURST = 2.0

MAX_429_RETRIES = 4
FALLBACK_BACKOFF = (2.0, 5.0, 12.0, 30.0)

# ------------------------------------------------------------------- governor

class RateGovernor(object):
    """A token bucket per provider, so the pause happens before the 429.

    Refills at `rate` calls per minute up to `burst` tokens. `acquire` sleeps
    until a token exists and returns how long it slept -- that number is added
    to the task's wall clock, because a benchmark that quietly excludes its own
    rate-limit sleep is reporting a speed nobody can obtain.
    """

    def __init__(self, rates=None, burst=BURST):
        self.rates = dict(rates or DEFAULT_RATES)
        self.burst = burst
        self.tokens = dict((name, burst) for name in self.rates)
        self.checked = dict((name, time.time()) for name in self.rates)
        self.slept = dict((name, 0.0) for name in self.rates)
        self.rate_limits = []

    def acquire(self, provider):
        per_second = self.rates.get(provider, 0.0) / 60.0
        if per_second <= 0:
            return 0.0
        now = time.time()
        self.tokens[provider] = min(
            self.burst,
            self.tokens.get(provider, self.burst)
            + (now - self.checked.get(provider, now)) * per_second)
        self.checked[provider] = now
        slept = 0.0
        if self.tokens[provider] < 1.0:
            slept = (1.0 - self.tokens[provider]) / per_second
            time.sleep(slept)
            self.tokens[provider] = 1.0
            self.checked[provider] = time.time()
            self.slept[provider] = self.slept.get(provider, 0.0) + slept
        self.tokens[provider] -= 1.0
        return slept

    def note_429(self, provider, retry_after, attempt):
        """Record the 429 and return how long to wait before retrying."""
        wait = retry_after
        if wait is None:
            wait = FALLBACK_BACKOFF[min(attempt, len(FALLBACK_BACKOFF) - 1)]
        self.rate_limits.append({
            "provider": provider, "retry_after": retry_after,
            "waited": round(wait, 2), "attempt": attempt + 1,
            "at": round(time.time(), 3),
            "source": "header" if retry_after is not None else "backoff"})
        return wait

# ---------------------------------------------------------------- instrumenting

def _rate_limit_info(exc):
    """`(is_429, retry_after_seconds)` for a ProviderError, from the SDK error.

    `agents_core.call_model` re-raises as ProviderError, which loses the typed
    exception -- but not the object: Python keeps the original on
    `__context__`, so the status code and the `Retry-After` header are still
    reachable without reimplementing the provider call here and letting the
    model IDs drift apart.
    """
    original = getattr(exc, "__context__", None)
    status = getattr(original, "status_code", None)
    response = getattr(original, "response", None)
    if status is None:
        status = getattr(response, "status_code", None)
    is_429 = status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()
    retry_after = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        for name in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
            try:
                raw = headers.get(name)
            except Exception:
                raw = None
            if raw:
                try:
                    retry_after = float(str(raw).rstrip("s"))
                except ValueError:
                    retry_after = None
                if retry_after is not None:
                    break
    return is_429, retry_after


class Instrument(object):
    """Wraps `agents_core.call_model` -- the one place that touches a provider.

    Counts calls, names the providers actually used, applies the governor
    before each call and retries a 429 rather than letting it surface as a
    provider failure and silently become a different result.
    """

    def __init__(self, governor, stub=None, verbose=False):
        self.governor = governor
        self.stub = stub
        self.verbose = verbose
        self.reset()
        self._real = None

    def reset(self):
        self.calls = 0
        self.failed_calls = 0
        self.retries = 0
        self.providers = []
        self.slept = 0.0

    def __enter__(self):
        self._real = agents_core.call_model
        agents_core.call_model = self._call
        return self

    def __exit__(self, *exc_info):
        agents_core.call_model = self._real
        return False

    def _call(self, provider, api_key, system, user):
        for attempt in range(MAX_429_RETRIES + 1):
            self.slept += self.governor.acquire(provider)
            self.calls += 1
            if provider not in self.providers:
                self.providers.append(provider)
            try:
                if self.stub is not None:
                    return self.stub(provider, system, user)
                return self._real(provider, api_key, system, user)
            except agents_core.ProviderError as exc:
                self.failed_calls += 1
                is_429, retry_after = _rate_limit_info(exc)
                if not is_429 or attempt == MAX_429_RETRIES:
                    raise
                wait = self.governor.note_429(provider, retry_after, attempt)
                self.retries += 1
                if self.verbose:
                    print("    429 from %s; Retry-After=%s; waiting %.1fs"
                          % (provider, retry_after, wait))
                time.sleep(wait)
                self.slept += wait
        raise AssertionError("unreachable")

# ------------------------------------------------------------------- stub model

class StubModel(object):
    """A local stand-in for a provider, for validating this script offline.

    It measures nothing about any model. What it does exercise is everything
    around one: the Planner/Executor plumbing, the suite audit, the harness,
    resume, and -- because its Planner deliberately writes a *thin* suite while
    its Executor is sometimes wrong in a way only the hidden suite catches --
    the false-APPROVED accounting itself.
    """

    def __init__(self, quality="flaky", live_models=None, vacuous_plan=False):
        self.quality = quality
        self.task = None
        # Slugs that answer. `None` means every slug answers. Anything else
        # 404s, which is how `--bad-slug` reproduces a wrong model ID without a
        # key or a network.
        self.live_models = live_models
        # Make the Planner's TESTS block fail the vacuity audit, which is the
        # only way `_resolve_tests`' regeneration branch runs. It is orthogonal
        # to `quality` -- the Executor can be perfect, broken or flaky under a
        # vacuous plan -- so it is a flag and not a fourth quality.
        self.vacuous_plan = vacuous_plan
        # `flaky` alternates per Executor call, not per task, so three A'@3
        # draws from one spec are not three copies of one answer. Without that
        # the best-of-3 tie-break is never exercised. `perfect` and `broken`
        # stay deterministic.
        self.executor_calls = 0

    def _role(self, system):
        for role, prompt in agents_core.PROMPTS.items():
            if system.strip().startswith(prompt.strip()[:60]):
                return role
        return "executor"

    def _correct(self):
        return self.task.reference

    # Tried in order; the first that changes the source wins. Each is a defect
    # a real model plausibly makes, and each is subtle enough that a thin suite
    # can miss it -- which is the situation this stub exists to reproduce. A
    # mutation that leaves behaviour unchanged would make `--stub broken`
    # quietly emit correct code, so the last resort is unambiguously wrong.
    MUTATIONS = (("raise", None), (">=", ">"), ("<=", "<"),
                 (" or ", " and "), ("sorted(", "list("))

    def _wrong(self):
        """Right on the ordinary cases, wrong on at least one edge case."""
        source = self.task.reference
        for find, replace in self.MUTATIONS:
            if find == "raise":
                lines, hit = [], False
                for line in source.splitlines():
                    stripped = line.lstrip()
                    if stripped.startswith("raise "):
                        # Indentation is preserved: a syntax error would be
                        # caught by any suite and exercise the import path
                        # instead of the interesting one.
                        lines.append(line[:len(line) - len(stripped)] + "pass")
                        hit = True
                    else:
                        lines.append(line)
                if hit:
                    return "\n".join(lines) + "\n"
            elif find in source:
                return source.replace(find, replace, 1)
        return "".join("def %s(*args, **kwargs):\n    return None\n\n" % name
                       for name in self.task.names)

    def _thin_suite(self):
        """The first two real asserts only -- a plausible weak suite."""
        lines = self.task.tests.splitlines()
        head = [line for line in lines if line.startswith("from solution")]
        body = [line for line in lines if line.startswith("assert ")][:2]
        return "\n".join(head + [""] + body) + "\n"

    def _vacuous_suite(self):
        """A suite the audit rejects: it never calls anything it imports.

        Every assert here passes against `harness._stub_source`, whose entry
        points all raise, so `audit_tests` marks it vacuous. It is otherwise
        well-formed -- it parses, it imports from `solution`, it has asserts, it
        is not `import *` -- so it fails on the *stub-exit* branch specifically,
        which is the branch a real model's plausible-looking-but-empty suite
        trips. Suites that fail earlier checks are already covered elsewhere.
        """
        names = list(self.task.names)
        lines = ["from solution import %s" % ", ".join(names), ""]
        for name in names:
            lines.append("assert callable(%s)" % name)
            lines.append("assert %s is not None" % name)
        return "\n".join(lines) + "\n"

    def __call__(self, provider, system, user):
        model = agents_core.PROVIDERS[provider]["model"]
        if self.live_models is not None and model not in self.live_models:
            raise agents_core.ProviderError(
                "%s failed: 404 model '%s' does not exist" % (provider, model))
        role = self._role(system)
        if role == "planner":
            suite = (self._vacuous_suite() if self.vacuous_plan
                     else self._thin_suite())
            return ("SPEC:\n%s\n\nSTEPS:\n1. Implement %s as specified.\n\n"
                    "TESTS:\n```python\n%s```\n"
                    % (self.task.prompt.strip(), ", ".join(self.task.names),
                       suite))
        if role == "test_writer":
            # Regeneration succeeds, so a vacuous plan yields TESTS_REGENERATED
            # rather than TESTS_VACUOUS. The point is to exercise the branch, not
            # to bottom it out. How many times this was reached is read from the
            # persisted call log, which is auditable from a results directory.
            return "```python\n%s```\n" % self._thin_suite()
        good = {"perfect": True, "broken": False}.get(
            self.quality,
            (self.task.variant + self.executor_calls) % 2 == 0)
        self.executor_calls += 1
        return "```python\n%s```\n" % (self._correct() if good
                                       else self._wrong())


# ---------------------------------------------------------------------- grading

def grade(code, task):
    """The one grader both arms go through, against the hidden suite only."""
    if not (code or "").strip():
        return {"passed": False, "grade_reason": "no code was produced",
                "grade_failure": harness.FAIL_NONE, "grade_assertion": ""}
    result = harness.run_python_sandboxed(code, task.tests,
                                         timeout=harness.EXEC_TIMEOUT_SECONDS)
    return {"passed": bool(result.ok),
            "grade_reason": result.reason or ("" if result.ok else "hidden suite failed"),
            "grade_failure": result.failure_kind,
            "grade_assertion": result.failed_assertion,
            "grade_exit": result.exit_code,
            "grade_timed_out": result.timed_out,
            "grade_stderr": harness._truncate(result.stderr, 900)}

# ------------------------------------------------------------------------- arms

ARM_A_NOTE = ("one Executor call on the raw prompt; no plan, no suite, "
              "no repair")
ARM_A_PRIME_NOTE = ("one Executor call on the Planner's SPEC only; no visible "
                    "suite, no gate, no repair")
ARM_A_PRIME3_NOTE = ("three independent Executor calls on the Planner's SPEC; "
                     "same gate as arm B, best-of-3, zero feedback")
ARM_B_NOTE = ("mode 3: Planner, audited suite, Executor, harness, escalation "
              "ladder")

# Matched to MAX_REVISION_ROUNDS so A'@3 spends roughly arm B's Executor budget
# with none of its feedback. That is the whole point of the arm: if B is no
# better than three independent draws, the repair loop is buying lottery
# tickets, not repairing anything, and should be cut rather than tuned.
A_PRIME_DRAWS = 3

# Arms that consume the shared Planner output.
PLAN_ARMS = ("a_prime", "a_prime3", "b")


def make_plan(task, keys):
    """One Planner call, shared by every arm that needs a plan.

    A Planner call per arm would put plan-to-plan variance *inside* the contrast
    the plan is supposed to hold fixed: B and A'@3 would be answering different
    specs and the difference between them would absorb the difference between
    two Planner samples.

    The visible suite is resolved here too, **once**, and handed to every arm as
    bytes (`tests_payload`). Resolving it per arm from the same plan text agrees
    only while the audit accepts the plan's own TESTS block; when it does not,
    `_resolve_tests` regenerates via the Test Writer, and two independent
    generations are two different suites. That put arm B on a suite A'@3 never
    saw on exactly the tasks where the suite was weakest, and spent a second
    Test Writer call -- a Gemini call, the scarce currency -- to do it.
    """
    text, provider = agents_core.call_role(
        "planner", keys, agents_core.PROMPTS["planner"], task.prompt)
    scratch = agents_core.RunResult(spec=agents_core.extract_spec(text))
    agents_core._resolve_tests(scratch, text, keys, lambda role, content: None,
                               None)
    return {"plan": text,
            "planner_provider": provider,
            "spec": scratch.spec,
            "steps": len(agents_core.extract_steps(text)),
            "tests": scratch.tests,
            "tests_status": scratch.tests_status,
            "tests_trusted": scratch.tests_trusted,
            "tests_payload": agents_core.resolved_tests(scratch),
            "tests_sha256": _suite_hash(scratch.tests)}


def _suite_hash(text):
    """The gate suite's identity, recorded per arm.

    Byte-identity across arms is the property `tests_payload` exists to
    guarantee, and a property nobody can audit from a results directory is a
    property nobody checks.
    """
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _spec_only_prompt(spec):
    """What an A' Executor sees: the SPEC, formatted exactly as arm B's first
    step formats it, and nothing else. No steps, no suite, no traceback."""
    return agents_core.build_context(spec, [])


def _draw(spec, keys):
    """One independent Executor sample from the spec."""
    text, provider = agents_core.call_role(
        "executor", keys, agents_core.PROMPTS["executor"],
        _spec_only_prompt(spec))
    code, _lang = harness.extract_code_block(text, allow_tests=False)
    return {"code": code, "provider": provider, "raw_len": len(text)}


def _gate(code, plan):
    """The visible gate: the harness running the Planner's audited suite.

    Returns ``(verdict, approved)``. Structurally the same gate arm B applies to
    one candidate -- and emphatically not the grader: `grade()` alone decides
    whether a task passed, against the hidden suite the arms never see.
    """
    if not plan.get("tests_trusted"):
        return harness.VERDICT_UNVERIFIED, False
    if not (code or "").strip():
        return harness.VERDICT_REVISE, False
    result = harness.run_python_sandboxed(code, plan["tests"],
                                         timeout=harness.EXEC_TIMEOUT_SECONDS)
    if result.failure_kind == harness.FAIL_PATH:
        return harness.VERDICT_UNVERIFIED, False
    approved = bool(result.ok and result.tested)
    return (harness.VERDICT_APPROVED if approved else harness.VERDICT_REVISE,
            approved)


def run_arm_a(task, keys, plan=None):
    """One call. No verdict of its own, so `verdict` is left as UNVERIFIED."""
    text, provider = agents_core.call_role(
        "executor", keys, agents_core.PROMPTS["executor"], task.prompt)
    code, _lang = harness.extract_code_block(text, allow_tests=False)
    return {"verdict": harness.VERDICT_UNVERIFIED,
            "pipeline_says_passed": None,
            "repair_rounds": 0,
            "escalation": "",
            "tests_status": "none",
            "steps": 1,
            "provider_last": provider,
            "code": code,
            "raw_len": len(text)}


def run_arm_a_prime(task, keys, plan):
    """The Planner's spec, one Executor call, no repair.

    The gate verdict is recorded but changes nothing here -- there is only one
    draw to choose from. It is recorded so this arm's false-APPROVED rate is
    computable, which separates "the visible suite is too weak" from "the
    repair loop made it worse".
    """
    draw = _draw(plan["spec"], keys)
    verdict, approved = _gate(draw["code"], plan)
    return {"verdict": verdict,
            "pipeline_says_passed": approved,
            "repair_rounds": 0,
            "escalation": "",
            "tests_status": plan["tests_status"],
            "tests_trusted": plan["tests_trusted"],
            "gate_available": bool(plan["tests_trusted"]),
            "gate_tests_sha256": plan["tests_sha256"],
            "steps": 1,
            "provider_last": draw["provider"],
            "code": draw["code"],
            "raw_len": draw["raw_len"],
            "candidates": [{"draw": 1, "code": draw["code"],
                            "provider": draw["provider"],
                            "gate_verdict": verdict}]}


# How A'@3's returned draw was chosen. Three different events, three different
# values: `chosen_draw == 1` used to mean any of them.
SELECTION_GATE_WIN = "gate_approved"
SELECTION_NO_APPROVAL = "fallback_no_draw_approved"
SELECTION_NO_GATE = "fallback_gate_unavailable"


def run_arm_a_prime3(task, keys, plan):
    """Three independent draws from the same spec, gated, best-of-3.

    The tie-break is **the first APPROVED in seeded order** -- not the shortest
    and not the one with the highest visible score. Both of those correlate with
    hidden correctness and would quietly upgrade this arm's gate towards the
    oracle it is supposed to be compared against. When no draw passes the gate,
    draw 1 is returned for the same reason: any other choice among failures is a
    ranking built out of gate signal.

    When the suite is untrusted there is no gate at all, and this arm degenerates
    to A' -- one draw, chosen by position. That is recorded as its own selection
    mode (`SELECTION_NO_GATE`) rather than as draw 1 winning, because the blind
    gate-loss term `A'@3_gate - A'@3_oracle` is attenuated toward zero by every
    such task, and the pre-registration reads a near-zero blind gate loss as a
    surprisingly good selector.

    Every draw is stored, including the discarded ones. None of them is graded
    here; the cold grading pass over stored candidates is deliberately separate.
    """
    draws = []
    for index in range(A_PRIME_DRAWS):
        draw = _draw(plan["spec"], keys)
        verdict, approved = _gate(draw["code"], plan)
        draws.append({"draw": index + 1, "code": draw["code"],
                      "provider": draw["provider"], "gate_verdict": verdict,
                      "gate_approved": approved, "raw_len": draw["raw_len"]})
    gate_available = bool(plan["tests_trusted"])
    winner = next((d for d in draws if d["gate_approved"]), None)
    chosen = winner or draws[0]
    if winner is not None:
        selection = SELECTION_GATE_WIN
    elif gate_available:
        selection = SELECTION_NO_APPROVAL
    else:
        selection = SELECTION_NO_GATE
    return {"verdict": chosen["gate_verdict"],
            "pipeline_says_passed": chosen["gate_approved"],
            "repair_rounds": 0,
            "escalation": "",
            "tests_status": plan["tests_status"],
            "tests_trusted": plan["tests_trusted"],
            "gate_available": gate_available,
            "gate_tests_sha256": plan["tests_sha256"],
            "selection": selection,
            "selected_by_gate": selection == SELECTION_GATE_WIN,
            "degenerated_to_a_prime": selection == SELECTION_NO_GATE,
            "steps": 1,
            "draws": len(draws),
            "chosen_draw": chosen["draw"],
            "gate_approved_draws": [d["draw"] for d in draws if d["gate_approved"]],
            "provider_last": chosen["provider"],
            "code": chosen["code"],
            "raw_len": chosen["raw_len"],
            "candidates": [{"draw": d["draw"], "code": d["code"],
                            "provider": d["provider"],
                            "gate_verdict": d["gate_verdict"]} for d in draws]}


def run_arm_b(task, keys, plan=None):
    # Both the plan *and* the suite it resolved to. Passing only the plan let
    # `_resolve_tests` run again in here, which regenerates a second suite
    # whenever the plan's own TESTS block fails the audit.
    run = agents_core.run_workspace(
        task.prompt, keys, mode=3,
        plan=None if plan is None else plan["plan"],
        tests=None if plan is None else plan.get("tests_payload"))
    graded = [step for step in run.steps if (step.code or "").strip()]
    step = graded[-1] if graded else None
    candidates = []
    for entry in agents_core._candidates_for_log(step.candidates if step else []):
        entry["gate_verdict"] = entry.pop("verdict")
        candidates.append(entry)
    return {"verdict": step.verdict if step else harness.VERDICT_UNVERIFIED,
            "pipeline_says_passed": bool(step.verified) if step else False,
            "repair_rounds": step.rounds if step else 0,
            "escalation": step.escalation if step else "",
            "tests_status": run.tests_status,
            "tests_trusted": run.tests_trusted,
            "gate_available": bool(run.tests_trusted),
            "gate_tests_sha256": _suite_hash(run.tests),
            "steps": len(run.steps),
            "steps_approved": run.verified_count,
            "graded_step": step.number if step else None,
            "failure_kind": step.failure_kind if step else "",
            "retained_round": step.retained_round if step else 0,
            "final_round_worse": bool(step.final_round_worse) if step else False,
            "provider_last": step.provider if step else "",
            "run_id": run.run_id,
            "log": run.memory_path,
            "code": step.code if step else "",
            "candidates": candidates,
            "raw_len": len(run.deliverable or "")}


ARMS = {"a": (run_arm_a, ARM_A_NOTE),
        "a_prime": (run_arm_a_prime, ARM_A_PRIME_NOTE),
        "a_prime3": (run_arm_a_prime3, ARM_A_PRIME3_NOTE),
        "b": (run_arm_b, ARM_B_NOTE)}
ARM_ORDER = ("a", "a_prime", "a_prime3", "b")

# ---------------------------------------------------------------- one task, once

def result_path(root, seed, arm, task, repeat):
    return os.path.join(root, "seed-%d" % seed, "arm-%s" % arm,
                        "%s__r%d.json" % (task.task_id, repeat))


def _arm_order(arms, seed, task, repeat):
    """A deterministic per-task permutation of the arms.

    Arm B used to have to run first, because it was the only arm that produced a
    plan. Sharing the Planner call removes that constraint, and leaving `b`
    first anyway would hand it a systematic position: it would absorb the least
    rate-limit pressure and the freshest quota on every single task. Seeded, so
    the order is reproducible and resume-safe.
    """
    ordered = [arm for arm in ARM_ORDER if arm in arms]
    digest = hashlib.sha256(
        ("%d|%s|%d" % (seed, task.task_id, repeat)).encode("utf-8"))
    random.Random(int(digest.hexdigest()[:16], 16)).shuffle(ordered)
    return ordered


def _call_log_slice(start):
    """The provenance of every call this arm made, for the record.

    `requested` and `used` are both stored. In measurement mode they must be
    equal on every call; a record where they differ is a bug in `call_role`, not
    a finding about providers.
    """
    keep = ("role", "requested", "used", "model", "temperature", "top_p", "at",
            "measurement_mode", "ok")
    return [{name: entry.get(name) for name in keep}
            for entry in agents_core.CALL_LOG[start:]]


def run_one(task, arm, keys, instrument, repeat, stub=None, plan=None):
    """Run one arm on one task and return the record, whatever happened.

    A crashed task is recorded as a failed task rather than aborting the run:
    the alternative is a 200-task sweep that dies on task 34 and tells you
    nothing about the other 166.
    """
    runner, note = ARMS[arm]
    instrument.reset()
    if stub is not None:
        stub.task = task
    log_start = len(agents_core.CALL_LOG)
    started = time.time()
    record = {"task_id": task.task_id, "family": task.family, "tier": task.tier,
              "variant": task.variant, "params": task.params, "arm": arm,
              "arm_note": note, "repeat": repeat, "seed": task.seed,
              "stub": None if stub is None else stub.quality,
              "measurement_mode": agents_core.MEASUREMENT_MODE}
    if plan is not None:
        # Recorded on every arm that consumed the plan, and flagged as shared so
        # nobody sums these across arms: it is one Planner call, not three.
        record["plan_shared"] = True
        record["plan_calls"] = plan.get("calls", 0)
        record["planner_provider"] = plan.get("planner_provider", "")
        record["plan_tests_status"] = plan.get("tests_status", "")
        record["plan_call_log"] = plan.get("call_log", [])
    try:
        if plan is not None and plan.get("error"):
            raise agents_core.ProviderError(plan["error"])
        outcome = runner(task, keys, plan)
        record.update(outcome)
        record.update(grade(outcome.pop("code", ""), task))
        record["error"] = ""
    except agents_core.ProviderError as exc:
        record.update({"passed": False, "verdict": harness.VERDICT_UNVERIFIED,
                       "error": "provider: %s" % agents_core._redact(exc, keys),
                       "grade_reason": "no answer to grade"})
    except Exception as exc:
        # Redacted: an SDK exception, or a frame in its traceback, is the one
        # place a key could plausibly reach a results file.
        record.update({"passed": False, "verdict": harness.VERDICT_UNVERIFIED,
                       "error": "%s: %s" % (type(exc).__name__,
                                            agents_core._redact(exc, keys)),
                       "traceback": agents_core._redact(
                           traceback.format_exc(), keys)[-1200:],
                       "grade_reason": "no answer to grade"})
    record["seconds"] = round(time.time() - started, 2)
    record["seconds_sleeping"] = round(instrument.slept, 2)
    record["calls"] = instrument.calls
    record["failed_calls"] = instrument.failed_calls
    record["rate_limit_retries"] = instrument.retries
    record["providers"] = list(instrument.providers)
    record["call_log"] = _call_log_slice(log_start)
    record["provider_substituted"] = any(
        entry["requested"] != entry["used"]
        for entry in record["call_log"] + record.get("plan_call_log", []))
    # A step the harness approved that the hidden suite then fails. This is the
    # number the pipeline cannot self-report, so it is computed here.
    record["false_approved"] = bool(record.get("pipeline_says_passed")
                                   and not record.get("passed"))
    record.setdefault("passed", False)
    return record


def save_record(path, record):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")

# ---------------------------------------------------------------- summarising

def _pct(part, whole):
    return 0.0 if not whole else round(100.0 * part / whole, 1)


def _quantile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return round(ordered[index], 2)


def _bucket(records):
    green = [r for r in records if r.get("passed")]
    approved = [r for r in records if r.get("pipeline_says_passed")]
    return {
        "n": len(records),
        "passed": len(green),
        "pass_rate": _pct(len(green), len(records)),
        "approved": len(approved),
        "false_approved": sum(1 for r in records if r.get("false_approved")),
        "false_approved_rate_of_approved": _pct(
            sum(1 for r in records if r.get("false_approved")), len(approved)),
        # Tasks where no trustworthy suite existed, so there was no gate. Not a
        # footnote: on these tasks A'@3 has nothing to select with and returns
        # draw 1, which drags the blind gate-loss term toward zero. Arm A has no
        # suite by design and its records carry no `tests_trusted` at all, so it
        # scores 0 here rather than n.
        "untrusted_suite": sum(1 for r in records
                               if r.get("tests_trusted") is False),
        "degenerated_to_a_prime": sum(1 for r in records
                                      if r.get("degenerated_to_a_prime")),
        "median_seconds": _quantile([r.get("seconds", 0) for r in records], 0.5),
        "p90_seconds": _quantile([r.get("seconds", 0) for r in records], 0.9),
        "median_seconds_to_green": _quantile([r.get("seconds", 0) for r in green], 0.5),
        "p90_seconds_to_green": _quantile([r.get("seconds", 0) for r in green], 0.9),
        "median_calls": _quantile([r.get("calls", 0) for r in records], 0.5),
        "median_calls_to_green": _quantile([r.get("calls", 0) for r in green], 0.5),
        "p90_calls_to_green": _quantile([r.get("calls", 0) for r in green], 0.9),
        "total_sleep_seconds": round(
            sum(r.get("seconds_sleeping", 0) for r in records), 1),
        "errors": sum(1 for r in records if r.get("error")),
    }


def summarise(records):
    """Stratified by tier and by family. There is no headline number here.

    A pooled pass rate is a weighted average of whichever tiers happen to have
    the most families, so it is reported for completeness and is not a result.
    """
    summary = {"by_arm": {}}
    for arm in sorted(set(r["arm"] for r in records)):
        rows = [r for r in records if r["arm"] == arm]
        entry = {"pooled_not_a_result": _bucket(rows), "by_tier": {}, "by_family": {}}
        for tier in sorted(set(r["tier"] for r in rows)):
            entry["by_tier"][str(tier)] = _bucket(
                [r for r in rows if r["tier"] == tier])
        for family in sorted(set(r["family"] for r in rows)):
            entry["by_family"][family] = _bucket(
                [r for r in rows if r["family"] == family])
        entry["escalation"] = {}
        for rung in ("", "repair", "alternate", "fresh", "exhausted"):
            hit = [r for r in rows if r.get("escalation") == rung]
            if hit:
                entry["escalation"][rung or "none needed"] = {
                    "n": len(hit),
                    "passed_hidden": sum(1 for r in hit if r.get("passed"))}
        summary["by_arm"][arm] = entry
    return summary


def load_records(root, seed, arms):
    records = []
    for arm in arms:
        directory = os.path.join(root, "seed-%d" % seed, "arm-%s" % arm)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(".json"):
                with open(os.path.join(directory, name)) as handle:
                    records.append(json.load(handle))
    return records

# -------------------------------------------------------------------------- cli

def _keys_from_env():
    return {"gemini": os.environ.get("GEMINI_API_KEY", ""),
            "groq": os.environ.get("GROQ_API_KEY", "")}


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Arm A vs Arm B, graded against the hidden suite.")
    parser.add_argument("--arm", choices=("a", "a_prime", "a_prime3", "b",
                                         "both", "all"), default="all",
                        help="`both` is a and b only; `all` is all four arms, "
                             "which is what the contrasts need")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-family", type=int, default=2)
    parser.add_argument("--tier", type=int, action="append", dest="tiers",
                        choices=(1, 2, 3))
    parser.add_argument("--family", action="append", dest="families")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--repeats", type=int, default=1,
                        help="samples per task; 1 (the default) measures one "
                             "draw per task and cannot separate a capability "
                             "gap from sampling noise")
    parser.add_argument("--out", default=RESULTS_DIR)
    parser.add_argument("--stub", choices=("perfect", "broken", "flaky"),
                        default=None,
                        help="run offline against a local stand-in; measures "
                             "the plumbing, not any model")
    parser.add_argument("--bad-slug", choices=("gemini", "groq"), default=None,
                        help="point one provider at a model ID that does not "
                             "exist, so its calls 404. Requires --stub. Shows "
                             "that measurement mode hard-fails instead of "
                             "quietly running the whole grid on the other "
                             "provider.")
    parser.add_argument("--vacuous-plan", action="store_true",
                        help="make the stub Planner emit a TESTS block the "
                             "vacuity audit rejects, so the regeneration branch "
                             "runs. Requires --stub. Orthogonal to the stub "
                             "quality, which is why it is not a fourth choice "
                             "of --stub.")
    parser.add_argument("--force", action="store_true",
                        help="rerun tasks that already have a result file")
    parser.add_argument("--summarise-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


UNTRUSTED_NOTE = (
    "untrust = tasks whose acceptance suite was not trustworthy. On those tasks "
    "the A'@3 gate is unavailable and the arm degenerates to A' (draw 1 by "
    "position, not by selection), so the blind gate-loss term "
    "A'@3_gate - A'@3_oracle is attenuated toward zero. A near-zero blind gate "
    "loss must not be read as a good selector without excluding them. They are "
    "reported, not dropped: dropping them would be post-treatment selection on "
    "a quantity the Planner's own output quality caused.")


def _print_summary(summary):
    untrusted_total = 0
    for arm in sorted(summary["by_arm"]):
        entry = summary["by_arm"][arm]
        print("\n=== arm %s" % arm)
        print("  %-8s %4s %6s %8s %8s %9s %8s" %
              ("tier", "n", "pass%", "false-AP", "untrust", "med secs",
               "med calls"))
        for tier in sorted(entry["by_tier"]):
            row = entry["by_tier"][tier]
            print("  %-8s %4d %6.1f %8d %8d %9s %8s"
                  % (tier, row["n"], row["pass_rate"], row["false_approved"],
                     row["untrusted_suite"], row["median_seconds"],
                     row["median_calls"]))
        pooled = entry["pooled_not_a_result"]
        untrusted_total += pooled["untrusted_suite"]
        print("  pooled (not a result): %d/%d = %.1f%%, %d false APPROVED, "
              "%d untrusted suite"
              % (pooled["passed"], pooled["n"], pooled["pass_rate"],
                 pooled["false_approved"], pooled["untrusted_suite"]))
    if untrusted_total:
        print("\n" + UNTRUSTED_NOTE)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.arm == "all":
        arms = ARM_ORDER
    elif args.arm == "both":
        arms = ("a", "b")
    else:
        arms = (args.arm,)
    root = os.path.abspath(args.out)

    if args.summarise_only:
        records = load_records(root, args.seed, arms)
        if not records:
            print("no results under %s for seed %d" % (root, args.seed),
                  file=sys.stderr)
            return 2
        _print_summary(summarise(records))
        return 0

    tasks = gen_tasks.generate(seed=args.seed, per_family=args.per_family,
                              tiers=set(args.tiers or ()),
                              families=set(args.families or ()),
                              limit=args.limit)
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return 2

    keys = _keys_from_env()
    live_models = None
    if args.bad_slug:
        if not args.stub:
            print("--bad-slug requires --stub; it is a demonstration, not a "
                  "way to break a real run", file=sys.stderr)
            return 2
        live_models = set(cfg["model"] for cfg in agents_core.PROVIDERS.values())
        agents_core.PROVIDERS[args.bad_slug]["model"] = "model-does-not-exist"
        print("--bad-slug %s: its model ID is now %r, so every %s call 404s."
              % (args.bad_slug,
                 agents_core.PROVIDERS[args.bad_slug]["model"], args.bad_slug))
    if args.vacuous_plan and not args.stub:
        print("--vacuous-plan requires --stub; it shapes the stub Planner's "
              "output and cannot touch a real one", file=sys.stderr)
        return 2
    stub = (StubModel(args.stub, live_models=live_models,
                      vacuous_plan=args.vacuous_plan) if args.stub else None)
    if args.vacuous_plan:
        print("--vacuous-plan: the stub Planner's suite asserts nothing real, so "
              "the audit rejects it and the Test Writer regenerates once. That "
              "regeneration is the path arm B used to duplicate.")
    if stub is None:
        needed = sorted(set(agents_core.ROLE_PROVIDER.values()))
        absent = [name for name in needed if not keys.get(name)]
        if absent:
            print("no key for %s (set GEMINI_API_KEY / GROQ_API_KEY), or pass "
                  "--stub to validate this script offline"
                  % ", ".join(absent), file=sys.stderr)
            return 2
        keys = dict((name, key) for name, key in keys.items() if key)
    else:
        keys = {"gemini": "stub", "groq": "stub"}

    # Not optional and not configurable. Silent failover turns an arm into a
    # provider mixture whose composition tracks rate-limit pressure, and the
    # mixture correlates with the arm, so no amount of pairing cancels it.
    agents_core.set_measurement_mode(True)
    print("measurement mode ON: every call is pinned to one provider; a dead "
          "provider fails the task instead of silently rerouting to the other.")

    governor = RateGovernor()
    if args.repeats == 1:
        print("--repeats 1: one draw per task. A per-family difference of one "
              "or two tasks is noise, not a finding.")
    print("%d tasks x %d arm(s) x %d repeat(s) = %d runs%s"
          % (len(tasks), len(arms), args.repeats,
             len(tasks) * len(arms) * args.repeats,
             " (stub: %s)" % args.stub if stub else ""))

    started = time.time()
    done = 0
    with Instrument(governor, stub=stub, verbose=args.verbose) as instrument:
        for repeat in range(args.repeats):
            for task in tasks:
                pending = []
                for arm in _arm_order(arms, args.seed, task, repeat):
                    path = result_path(root, args.seed, arm, task, repeat)
                    if os.path.exists(path) and not args.force:
                        if args.verbose:
                            print("  skip %s arm %s (already done)"
                                  % (task.task_id, arm))
                        continue
                    pending.append(arm)
                if not pending:
                    continue

                # One Planner call for every arm that needs one, made before any
                # of them run so no arm gets to be "the one that plans" and
                # therefore "the one that always goes first".
                plan = None
                if any(arm in PLAN_ARMS for arm in pending):
                    instrument.reset()
                    agents_core.reset_call_log()
                    if stub is not None:
                        stub.task = task
                    try:
                        plan = make_plan(task, keys)
                        plan["error"] = ""
                    except agents_core.ProviderError as exc:
                        # In measurement mode this is the designed outcome of a
                        # dead provider: every plan-consuming arm on this task
                        # is recorded as failed. Loud, attributable, and not
                        # quietly rerouted to the other provider.
                        plan = {"error": "planning failed: %s"
                                         % agents_core._redact(exc, keys)}
                        print("  !! %s plan failed: %s"
                              % (task.task_id, plan["error"][:100]))
                    plan["calls"] = instrument.calls
                    plan["call_log"] = _call_log_slice(0)

                for arm in pending:
                    record = run_one(task, arm, keys, instrument, repeat, stub,
                                     plan if arm in PLAN_ARMS else None)
                    save_record(result_path(root, args.seed, arm, task, repeat),
                                record)
                    done += 1
                    print("  %-8s %-24s %-9s %-11s %5.1fs %2d calls%s"
                          % (arm, task.task_id,
                             "PASS" if record["passed"] else "fail",
                             record.get("verdict", ""), record["seconds"],
                             record["calls"],
                             "  FALSE APPROVED" if record["false_approved"]
                             else ("  " + record["error"][:60]
                                   if record.get("error") else "")))
                    if record.get("provider_substituted"):
                        print("     BUG: a provider was substituted in "
                              "measurement mode; see call_log")

    print("\n%d run(s) in %.1fs; %d rate-limit event(s)"
          % (done, time.time() - started, len(governor.rate_limits)))
    for event in governor.rate_limits:
        print("  429 %s Retry-After=%s waited %.1fs (%s)"
              % (event["provider"], event["retry_after"], event["waited"],
                 event["source"]))

    records = load_records(root, args.seed, arms)
    summary = summarise(records)
    summary["rate_limits"] = governor.rate_limits
    summary["seed"] = args.seed
    summary["stub"] = args.stub
    summary["repeats"] = args.repeats
    summary_path = os.path.join(root, "seed-%d" % args.seed, "summary.json")
    save_record(summary_path, summary)
    _print_summary(summary)
    print("\nsummary: %s" % summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

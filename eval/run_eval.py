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

Keys come from `<PROVIDER>_API_KEY` -- GEMINI_API_KEY and GROQ_API_KEY on the
default configuration -- or from whatever variable a configured provider names in
its `env`. Which model each role calls is configuration too (`MAW_MODELS`, or
`models.json`); `python3 eval/models.py --resolved` prints the resolved table and
`--preflight` checks that every pair answers before a sweep spends anything.
Results land one JSON file per
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
#
# Derived from `agents_core.MIN_CALL_INTERVAL_SECONDS` rather than restated, so
# there is one set of pacing constants in this repository instead of two that
# drift. `agents_core` is the honest place for them: it has the comment
# explaining that both values are conservative guesses and not published limits.
# (They were 12/min and 25/min here; gemini is now 10/min, i.e. tighter.)
DEFAULT_RATES = dict(
    (provider, round(60.0 / seconds, 3) if seconds > 0 else 0.0)
    for provider, seconds in agents_core.MIN_CALL_INTERVAL_SECONDS.items())
BURST = 2.0

MAX_429_RETRIES = 4
FALLBACK_BACKOFF = (2.0, 5.0, 12.0, 30.0)

# ------------------------------------------------------------------ outcomes

# What happened to a cell, as three mutually exclusive values.
#
#   graded      -- a model answered and the hidden suite ran. `passed` is a fact
#                  about the model.
#   infra_loss  -- no answer to grade: rate limit exhausted, 5xx, dead slug,
#                  missing key, failover disabled. Carries no `passed` and no
#                  `grade_*` at all, because a rate-limit death is not evidence
#                  about a model and must not be able to look like one.
#   error       -- a bug in *this* code, not in a provider. Kept as `passed:
#                  False` exactly as before, and deliberately NOT excluded:
#                  excluding our own crashes would shrink the denominator every
#                  time we broke something, which is the opposite of loud.
OUTCOME_GRADED = "graded"
OUTCOME_INFRA_LOSS = "infra_loss"
OUTCOME_ERROR = "error"

# What `save_record` writes before it renames. Named because two other places
# depend on the choice: `load_records` selects on `.json`, so this must not end
# in it, and every resume in this repo asks `os.path.exists` about the record's
# own path, so this must not be that path.
PARTIAL_SUFFIX = ".partial"

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
        # Per provider, not just which ones were touched. A budget against a free
        # tier is per provider, so "3 providers, 324 calls" is not the number
        # anyone needs; the calibration sweep's projection is built off this.
        self.calls_by_provider = {}
        self.slept = 0.0

    def __enter__(self):
        self._real = agents_core.call_model
        agents_core.call_model = self._call
        return self

    def __exit__(self, *exc_info):
        agents_core.call_model = self._real
        return False

    def _call(self, provider, api_key, system, user, role=None):
        for attempt in range(MAX_429_RETRIES + 1):
            # The governor exists to arrive under a provider's rate limit. A stub
            # never reaches a provider, so pacing it prevents nothing and costs
            # the full interval per call in real time -- measured at 89% of an
            # offline sweep's wall clock, which is why an offline audit of one
            # task took 13.8s. Skip it when stubbed, and record that in the
            # manifest so a stub summary cannot be read as a real sweep's cost.
            if self.stub is None:
                self.slept += self.governor.acquire(provider)
            self.calls += 1
            self.calls_by_provider[provider] = (
                self.calls_by_provider.get(provider, 0) + 1)
            if provider not in self.providers:
                self.providers.append(provider)
            try:
                if self.stub is not None:
                    return self.stub(provider, system, user, role=role)
                return self._real(provider, api_key, system, user, role=role)
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


class _InstalledEventLog(object):
    """`agents_core.set_event_log`, as a context manager that puts it back.

    A sweep must not leave a finished log installed for whatever runs next in
    the process -- a second `main()` call in a check, say, whose events would
    then append to the first sweep's file.
    """

    def __init__(self, log):
        self.log = log
        self.previous = None

    def __enter__(self):
        self.previous = agents_core.set_event_log(self.log)
        return self.log

    def __exit__(self, *exc_info):
        agents_core.set_event_log(self.previous)
        return False

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
    #
    # Derived from `gen_tasks.STUB_EDITS` rather than restated, because
    # `gen_tasks --self-check` runs a battery over that list and `tasks.lock`
    # records the battery by name. Two copies would let this stub grow an edit the
    # lock's recorded guarantee does not cover, which is the exact class of defect
    # the battery exists to close: a lock promising more than the code delivers.
    MUTATIONS = tuple((find, replace)
                      for _, find, replace in gen_tasks.STUB_EDITS)

    def _wrong(self):
        """Right on the ordinary cases, wrong on at least one edge case.

        A mutation that applies *textually* is not necessarily one that changes an
        answer. `path-canonicalization-02` draws `escape='clamp'`, so its only
        `raise` is dead code and `raise -> pass` leaves the function computing
        exactly what the reference computes; `--stub broken` was handing that task a
        correct solution and calling it broken, which is the failure the comment
        above says must not happen and nothing checked. So each candidate is offered
        to the task's own hidden suite and one the suite cannot reject is passed
        over. That is also what keeps this stub inside the guarantee
        `gen_tasks --self-check` records in the lock: the battery there skips a
        mutation the suite provably cannot catch, and this skips the same one.
        """
        source = self.task.reference
        tests = self.task.tests
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
                mutant = "\n".join(lines) + "\n" if hit else None
            elif find in source:
                mutant = source.replace(find, replace, 1)
            else:
                mutant = None
            if mutant is not None and gen_tasks.suite_rejects(mutant, tests)[0]:
                return mutant
        return "".join("def %s(*args, **kwargs):\n    return None\n\n" % name
                       for name in self.task.names)

    def _thin_suite(self):
        """The first two real asserts only -- a plausible weak suite.

        Dedented, because a generated suite now carries each assert inside its
        own `try` so it can report per-check results, and two asserts lifted out
        of their wrappers are exactly the flat weak suite this is meant to be. It
        matched on column zero before and silently returned a suite with no
        asserts at all once the wrappers landed, which the audit then rejected as
        unusable -- a stub failing for the wrong reason.
        """
        lines = self.task.tests.splitlines()
        head = [line for line in lines if line.startswith("from solution")]
        body = [line.strip() for line in lines
                if line.strip().startswith("assert ")][:2]
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

    def probe(self, provider, api_key, model, system, user, params=None):
        """The 404 gate alone -- no task, no completion. The preflight's call.

        A preflight validates a `(provider, model)` pair, and validating one
        offline means checking the exact slug that would have gone on the wire and
        nothing else. Running a whole stub completion here would need a task,
        which a preflight legitimately does not have; sharing this gate with
        `__call__` is what makes `--bad-slug` a real negative control for the
        preflight rather than a second, separately-wrong imitation of it.

        `params` is what the pair would be sent, `reasoning_format` and all. This
        stub does not reject on it -- a stub cannot know which extension a provider
        refuses -- but it takes it, so the seam carries what a real call carries
        and a stubbed sweep exercises the same signature.
        """
        if self.live_models is not None and model not in self.live_models:
            raise agents_core.ProviderError(
                "%s failed: 404 model '%s' does not exist" % (provider, model),
                status=404)
        return "1"

    def __call__(self, provider, system, user, role=None):
        # The *resolved* model, which is what a real call would send and what a
        # wrong slug has to be checked against. Reading `PROVIDERS[provider]`
        # here instead would make the offline 404 simulation blind to exactly the
        # case the configuration layer exists for: a role pointed at its own
        # model. `--bad-slug` and `--live-models` both work through this line.
        model = agents_core.model_for(role, provider) if role else None
        if model is None:
            model = agents_core.PROVIDERS[provider]["model"]
        self.probe(provider, None, model, system, user)
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
            # Report-only: whether the Planner obeyed `_RUNTIME_RULE`. Recorded
            # per plan so calibration output carries the compliance rate as a
            # number. Nothing downstream reads it.
            "spec_runtime_syntax": agents_core.scan_runtime_syntax(scratch.spec),
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


def _finish_reason(call):
    """Why generation stopped, off a `call_role` return, or `None`.

    `None` means "no real completion was observed", never "stop": a truncated
    draw and a well-formed wrong answer are different findings, and the whole
    point of recording this is that they stop looking alike. `agents_core`
    initialises the key on every real record, so a real call always answers.

    Read through `getattr` for one specific reason, not out of caution: a
    `call_role` stand-in may hand back a plain 2-tuple -- `test_pipeline.py` has
    one -- and a stub has no finish reason to lose, so it reads as the same
    `None` a stubbed provider call gets. That is the rule `agents_core` already
    states for `_LAST_COMPLETION`, applied at the other end of the same wire.
    """
    return getattr(call, "record", {}).get("finish_reason")


def _model_slugs(call):
    """``(requested, returned)`` off a `call_role` return. Two claims, not one.

    `model` on the call record is the slug this call *asked for*, resolved
    against the provider actually attempted; `model_returned` is what the API
    said answered. A store carrying only the first cannot be audited after the
    fact, and a provider quietly serving a different model is the one failure in
    that family that leaves no trace -- every other kind arrives as a 404 or an
    empty completion.

    Read through the same `getattr` as `_finish_reason` above and for the same
    reason, not out of caution: a `call_role` stand-in may hand back a plain
    2-tuple, and a stub has no slugs to lose, so it reads as the `(None, None)`
    a call that never reached a provider gets. `agents_core.ModelMismatch`
    already stops a *live* sweep the moment these two disagree; forwarding them
    is what lets a sweep already on disk be checked for the same thing.
    """
    record = getattr(call, "record", {})
    return record.get("model"), record.get("model_returned")


def _draw(spec, keys):
    """One independent Executor sample from the spec.

    `finish_reason` rides along because without it a truncation is graded as bad
    code: 2 of the 57 draws in `eval/results/pin-replay-1/ledger.jsonl` came back
    empty at the endpoint's own 2048-token default, and a draw that was cut off
    mid-answer scores exactly like a draw that answered badly. Both were the
    runner-up `gpt-oss-20b` rather than the pinned Executor, so this records a
    known behaviour of the endpoint and not a known defect in the model the sweep
    uses. `call_role` hands this call's own record back on `.record`, so this is
    the provenance of *this* call and not the tail of a process-wide log.
    """
    call = agents_core.call_role(
        "executor", keys, agents_core.PROMPTS["executor"],
        _spec_only_prompt(spec))
    text, provider = call
    code, _lang = harness.extract_code_block(text, allow_tests=False)
    requested, returned = _model_slugs(call)
    return {"code": code, "provider": provider, "raw_len": len(text),
            "finish_reason": _finish_reason(call),
            # Named `model_requested` rather than `model` -- the call record's own
            # spelling for the same fact -- so the pair reads as a pair here and
            # in the draw file, and so a reader cannot mistake it for the
            # provider. `call_model_detailed` already uses these two names.
            "model_requested": requested, "model_returned": returned}


def _gate(code, plan):
    """The visible gate: the harness running the Planner's audited suite.

    Returns ``(verdict, approved)``. Structurally the same gate arm B applies to
    one candidate -- and emphatically not the grader: `grade()` alone decides
    whether a task passed, against the hidden suite the arms never see.
    """
    if not plan.get("tests_trusted"):
        agents_core.emit_event(agents_core.EVENT_GATE, available=False,
                               reason="suite not trustworthy",
                               verdict=harness.VERDICT_UNVERIFIED,
                               approved=False,
                               tests_sha256=plan.get("tests_sha256", ""))
        return harness.VERDICT_UNVERIFIED, False
    if not (code or "").strip():
        agents_core.emit_event(agents_core.EVENT_GATE, available=True,
                               reason="no code to run",
                               verdict=harness.VERDICT_REVISE, approved=False,
                               tests_sha256=plan.get("tests_sha256", ""))
        return harness.VERDICT_REVISE, False
    result = harness.run_python_sandboxed(code, plan["tests"],
                                         timeout=harness.EXEC_TIMEOUT_SECONDS)
    if result.failure_kind == harness.FAIL_PATH:
        agents_core.emit_event(agents_core.EVENT_GATE, available=True,
                               reason="harness could not run it",
                               verdict=harness.VERDICT_UNVERIFIED,
                               approved=False,
                               tests_sha256=plan.get("tests_sha256", ""))
        return harness.VERDICT_UNVERIFIED, False
    approved = bool(result.ok and result.tested)
    verdict = (harness.VERDICT_APPROVED if approved
               else harness.VERDICT_REVISE)
    # The digest, not the suite. Which bytes gated is the auditable fact; the
    # bytes themselves are already in the plan record once, shared across arms.
    agents_core.emit_event(agents_core.EVENT_GATE, available=True, reason="",
                           verdict=verdict, approved=approved,
                           exit_code=result.exit_code, tested=result.tested,
                           tests_sha256=plan.get("tests_sha256", ""),
                           code_sha256=agents_core.sha256_of(code))
    return verdict, approved


def run_arm_a(task, keys, plan=None):
    """One call. No verdict of its own, so `verdict` is left as UNVERIFIED."""
    call = agents_core.call_role(
        "executor", keys, agents_core.PROMPTS["executor"], task.prompt)
    text, provider = call
    code, _lang = harness.extract_code_block(text, allow_tests=False)
    return {"verdict": harness.VERDICT_UNVERIFIED,
            "pipeline_says_passed": None,
            "repair_rounds": 0,
            "escalation": "",
            "tests_status": "none",
            "steps": 1,
            "provider_last": provider,
            "code": code,
            "raw_len": len(text),
            "finish_reason": _finish_reason(call)}


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
            "finish_reason": draw["finish_reason"],
            "candidates": [{"draw": 1, "code": draw["code"],
                            "provider": draw["provider"],
                            "gate_verdict": verdict,
                            "finish_reason": draw["finish_reason"]}]}


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
                      "gate_approved": approved, "raw_len": draw["raw_len"],
                      "finish_reason": draw["finish_reason"]})
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
            "finish_reason": chosen["finish_reason"],
            "candidates": [{"draw": d["draw"], "code": d["code"],
                            "provider": d["provider"],
                            "gate_verdict": d["gate_verdict"],
                            "finish_reason": d["finish_reason"]} for d in draws]}


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


def _seeded_shuffle(items, key):
    """A deterministic permutation of `items`, keyed by a string.

    sha256 of the key rather than `random.seed(key)`: the string-seeding path
    depends on the hash randomisation of whatever process runs it, which would
    make "reproducible from the seed" false in exactly the way nobody checks.
    """
    ordered = list(items)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    random.Random(int(digest[:16], 16)).shuffle(ordered)
    return ordered


def _task_order(tasks, seed):
    """A deterministic permutation of the task set (D7).

    Generation order is family-first, so it is also tier-clustered: the first
    tasks of the grid are the first families, and within a family the variants
    arrive together. Any sweep that does not finish -- killed, out of quota, dead
    provider -- then covers families unevenly, and which families it covered is a
    function of position, which is to say of family and tier. Shuffling makes what
    a truncated sweep measured independent of what it was measuring.

    Keyed on the seed alone, not on the task set, so a resumed sweep walks the
    same order as the sweep it is resuming even if it was given a narrower
    `--family` or `--limit`. Selection stays generation-order -- `--limit 3` picks
    the same three tasks it always did, and the lock verifies against them -- and
    only the order they run in moves.
    """
    return _seeded_shuffle(tasks, "task-order|%d" % seed)


def _arm_order(arms, seed, task, repeat):
    """A deterministic per-task permutation of the arms.

    Arm B used to have to run first, because it was the only arm that produced a
    plan. Sharing the Planner call removes that constraint, and leaving `b`
    first anyway would hand it a systematic position: it would absorb the least
    rate-limit pressure and the freshest quota on every single task. Seeded, so
    the order is reproducible and resume-safe.

    Keyed per task and per repeat, unlike `_task_order`: the bias being removed
    here is one arm always going first, so the permutation has to move between
    tasks rather than being one fixed order applied to all of them.
    """
    return _seeded_shuffle([arm for arm in ARM_ORDER if arm in arms],
                           "%d|%s|%d" % (seed, task.task_id, repeat))


def _call_log_slice(start):
    """The provenance of every call this arm made, for the record.

    `requested` and `used` are both stored. In measurement mode they must be
    equal on every call; a record where they differ is a bug in `call_role`, not
    a finding about providers.

    An allow-list, not the whole record: the record is a working object that grows,
    and a results file is a published artifact. `status`, `exc_class`, `attempts`
    and `retried` were added so a 429 that was survived by retrying is visible in
    the record rather than only in the wall clock. `finish_reason` is here for the
    same reason and one more: arm B's record has no top-level `finish_reason` --
    it comes back from `run_workspace`, not from `_draw` -- so for the arm that
    makes the most calls this slice is the only place a truncated repair round can
    be seen at all. `error` is still deliberately out -- it is provider text, and
    the redacted copy already lives on the record's own `error` field.
    """
    keep = ("role", "requested", "used", "model", "temperature", "top_p", "at",
            "measurement_mode", "ok", "status", "exc_class", "attempts",
            "retried", "finish_reason")
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
              # The identity of the suite that graded this cell, recorded on the
              # cell. `passed` is meaningless without it: two results from two
              # weeks apart are only comparable if they were graded against the
              # same bytes, and "they were, we think" is not an audit. Full
              # sha256, matching `tasks.lock`; `gate_tests_sha256` elsewhere on
              # this record is the *visible* suite's short id and a different
              # thing entirely.
              "hidden_tests_sha256": gen_tasks.digest(task.tests),
              # The resolved role -> (provider, model) table, on every cell.
              # Resolved at this moment rather than read out of source, because
              # `--bad-slug` mutates the provider table in place and a record built
              # from source would name the pristine slug while the cell ran the
              # dead one. This is also what makes a results directory attributable
              # after a model retires under it: which models a run *actually used*
              # is a fact about the run, and it now lives on the run.
              "models": agents_core.resolved_roles(),
              "models_source": agents_core.model_config_source(),
              # Empty on a configuration where the grader did not write the code.
              # Non-empty is not an error and does not stop anything; it is a
              # standing note on the cell that the independence claim is weaker
              # than the protocol's wording, so nobody has to re-derive it later.
              "independence_warnings": agents_core.independence_warnings(),
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
            raise agents_core.ProviderError(plan["error"],
                                            status=plan.get("error_status"),
                                            exc_class=plan.get("error_class", ""))
        outcome = runner(task, keys, plan)
        record.update(outcome)
        record.update(grade(outcome.pop("code", ""), task))
        record["error"] = ""
        record["outcome"] = OUTCOME_GRADED
    except agents_core.DailyQuotaExhausted:
        # Not a cell outcome. A per-day ceiling is a fact about the day, not about
        # this task or this arm, and recording it as `infra_loss` here would let
        # the sweep march through every remaining cell spending requests that
        # cannot succeed. Straight out to the caller.
        raise
    except agents_core.ProviderError as exc:
        # No answer came back, so there is nothing to grade and nothing this cell
        # can say about a model. It gets `outcome: infra_loss` and NO `passed` and
        # NO `grade_*` -- not `passed: False`. A rate-limit death that writes
        # `passed: False` is indistinguishable from a model that answered and got
        # it wrong, and every pass rate downstream then silently includes it.
        record["outcome"] = OUTCOME_INFRA_LOSS
        record.update({"verdict": harness.VERDICT_UNVERIFIED,
                       "error": "provider: %s" % agents_core.redact(exc, keys)})
        record.update(_infra_detail(exc, log_start))
    except Exception as exc:
        # Redacted: an SDK exception, or a frame in its traceback, is the one
        # place a key could plausibly reach a results file.
        record["outcome"] = OUTCOME_ERROR
        record.update({"passed": False, "verdict": harness.VERDICT_UNVERIFIED,
                       "error": "%s: %s" % (type(exc).__name__,
                                            agents_core.redact(exc, keys)),
                       "traceback": agents_core.redact(
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
    if record["outcome"] == OUTCOME_INFRA_LOSS:
        agents_core.emit_event(agents_core.EVENT_INFRA_LOSS,
                               task_id=task.task_id, arm=arm, repeat=repeat,
                               provider=record.get("infra_provider", ""),
                               status=record.get("infra_status"),
                               attempts=record.get("infra_attempts", 0),
                               seconds=record["seconds"],
                               reason=record["error"])
        # No `passed`, no `false_approved`. `setdefault` below would put a
        # `passed: False` on this record, which is the whole thing being avoided.
        return record
    # A step the harness approved that the hidden suite then fails. This is the
    # number the pipeline cannot self-report, so it is computed here.
    record["false_approved"] = bool(record.get("pipeline_says_passed")
                                   and not record.get("passed"))
    record.setdefault("passed", False)
    return record


def _infra_detail(exc, log_start):
    """What the infra loss was, from the exception and the calls it made.

    Named apart from the grading fields on purpose: `infra_status`, not `status`,
    so no summariser can pick these up by looking for a familiar key name.
    """
    calls = agents_core.CALL_LOG[log_start:]
    last = calls[-1] if calls else {}
    return {
        "infra_provider": last.get("used", ""),
        "infra_role": last.get("role", ""),
        "infra_status": getattr(exc, "status", None),
        "infra_exc_class": getattr(exc, "exc_class", ""),
        "infra_attempts": last.get("attempts", 0),
        "infra_retried": last.get("retried", 0),
        "infra_seconds_backoff": last.get("seconds_backoff", 0.0),
        "infra_retry_after": getattr(exc, "retry_after", None),
    }


def _outcome_label(record):
    """`PASS` / `fail` / `INFRA` for the progress line.

    `infra_loss` gets its own word rather than borrowing `fail`. The console line
    is what a human watches a sweep through, and printing a rate-limit death as
    `fail` reintroduces on screen exactly the conflation the record format was
    changed to remove.
    """
    if record.get("outcome") == OUTCOME_INFRA_LOSS:
        return "INFRA"
    return "PASS" if record.get("passed") else "fail"


def save_record(path, record):
    """Write `record` at `path` as JSON, atomically.

    Via a sibling temp file and `os.replace`, because every resume in this repo
    is `os.path.exists` of the record's own path and nothing re-validates what it
    finds. A plain `open(path, "w")` interrupted between the truncate and the end
    of `json.dump` -- Ctrl-C on a sweep, a full disk, a laptop lid -- leaves a
    file that exists and does not parse. `calibrate.main` then skips that draw
    for good, `calibrate.load_json` returns None for it at aggregation, and `d_t`
    is computed over a denominator one smaller than the draws that were paid for.
    That is the failure this guards: not a lost draw, which is visible, but a
    quietly wrong number. `os.replace` is atomic on POSIX and on Windows, so the
    path either does not exist or holds a whole record.

    The suffix is deliberately not `.json`: `load_records` reads every `*.json`
    in an arm directory, and a leftover from an interrupted write must not be
    loaded as a grid cell. Nor is a leftover deleted here -- a temp file that
    survived is the only evidence that a write was interrupted, and it costs
    nothing, being overwritten by the next attempt at the same path.
    """
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    partial = path + PARTIAL_SUFFIX
    with open(partial, "w") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(partial, path)

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


def _excluded_task_ids(records):
    """Task ids to drop from every bucket, because some cell lost to infra.

    Exclusion is at TASK granularity, and that is the whole point. Dropping only
    the cell that died would be differential: arm B makes the most calls, so it
    absorbs the most 429s, so cell-level exclusion would quietly reweight the
    task set toward whichever arms survived -- and the reweighting would correlate
    with the arm, which is exactly the confound the paired design exists to kill.
    Dropping the task drops it from all arms and all repeats at once.
    """
    return set(r["task_id"] for r in records
               if r.get("outcome") == OUTCOME_INFRA_LOSS)


def _infra_summary(records, excluded):
    """The exclusion, counted and attributed. Reported whether or not it is zero.

    A silent zero and a silent thirty look identical in a report, so this block is
    always present and `_print_summary` always prints the line.
    """
    lost = [r for r in records if r.get("outcome") == OUTCOME_INFRA_LOSS]
    by_arm, by_status = {}, {}
    for record in lost:
        by_arm[record["arm"]] = by_arm.get(record["arm"], 0) + 1
        key = str(record.get("infra_status"))
        by_status[key] = by_status.get(key, 0) + 1
    return {
        "cells": len(lost),
        "tasks_excluded": len(excluded),
        "task_ids_excluded": sorted(excluded),
        "cells_dropped_with_them": sum(
            1 for r in records if r.get("task_id") in excluded),
        "by_arm": by_arm,
        "by_status": by_status,
    }


def wrong_suite_tasks(draws):
    """Task ids where every graded draw passed the hidden suite and the gate rejected all of them.

    The signature of a suite that is wrong rather than a model that is weak. If a
    task's suite is trustworthy and three independent draws all satisfy the hidden
    acceptance criteria, and the visible suite approves none of them, the visible
    suite is testing something the task never asked for. `grouping-02` is the
    worked example: three candidates, three hidden passes, three gate rejections,
    all from `==` on a float mean.

    Report-only, and deliberately so. The alternative -- treating this as licence
    to loosen the gate or re-generate the suite -- would make the gate's own error
    rate unmeasurable, which is the one quantity A'@3 exists to measure.

    Input is DRAW-level rows, any mapping with `task_id`, `tests_trusted`,
    `passed` and `gate_approved`. Both ledgers already store exactly that, so this
    runs over records written before it existed; nothing is instrumented in the
    arm to feed it.

    Ungraded rows are skipped rather than counted as failures to pass. A draw that
    never came back has no `passed` field and is not evidence in either direction,
    the same reason `_infra_summary` excludes infra losses instead of scoring them
    zero. A task with no graded draw at all cannot be flagged.
    """
    by_task = {}
    for row in draws:
        if row.get("ok") is False or row.get("passed") is None:
            continue
        by_task.setdefault(row.get("task_id"), []).append(row)
    return sorted(
        task_id for task_id, rows in by_task.items()
        if all(row.get("tests_trusted") for row in rows)
        and all(row.get("passed") for row in rows)
        and not any(row.get("gate_approved") for row in rows))


def wrong_suite_report(draws):
    """`wrong_suite_tasks` plus the denominators needed to read it."""
    rows = [row for row in draws
            if row.get("ok") is not False and row.get("passed") is not None]
    tasks = sorted(set(row.get("task_id") for row in rows))
    flagged = wrong_suite_tasks(rows)
    return {
        "graded_draws": len(rows),
        "tasks_with_a_graded_draw": len(tasks),
        "tasks": flagged,
        "count": len(flagged),
        "rate_of_graded_tasks": _pct(len(flagged), len(tasks)),
    }


def _a_prime3_draw_rows(records):
    """Draw-level rows for `wrong_suite_report`, from stored A'@3 candidates.

    Only candidates that carry a hidden grade. `run_arm_a_prime3` stores every
    draw's code and gate verdict but grades none of them -- the cold grading pass
    over stored candidates is separate on purpose -- so today this yields nothing
    and the block below honestly reports zero graded draws. When that pass runs
    and annotates candidates with `passed`, this starts reporting without a second
    definition of the rule appearing anywhere.

    `gate_approved` is reconstructed from the record's `gate_approved_draws`,
    which is the field that actually stores it; the candidate entries keep only
    the verdict string.
    """
    for record in records:
        approved = set(record.get("gate_approved_draws") or ())
        for candidate in record.get("candidates") or ():
            if "passed" not in candidate:
                continue
            yield {"task_id": record.get("task_id"),
                   "tests_trusted": record.get("tests_trusted"),
                   "passed": candidate.get("passed"),
                   "gate_approved": candidate.get("draw") in approved}


def _gate_availability(records):
    """Where the gate existed at all: `tests_status` per suite, and the no-gate rate.

    Counted per SUITE and not per record. One Planner call resolves one suite and
    that suite gates every arm of the task, so counting `tests_status` per record
    would multiply each suite by the number of arms and report a distribution over
    something that was never drawn.

    The reason this is a first-class block and not a JSON field: every task whose
    suite is untrusted is a task where A'@3 had nothing to select with and returned
    draw 1 by position, so it contributes to `A'@3_gate` a value that came from A'.
    The blind gate-loss term `A'@3_gate - A'@3_oracle` is attenuated toward zero by
    each one, and a near-zero blind gate loss is exactly what the pre-registration
    would otherwise read as a good selector. The rate has to be visible before the
    grid is read, not discoverable in `summary.json` afterwards.

    `unusable` and `vacuous` are the two ways `_resolve_tests` can end with nothing
    trustworthy, and they are different events: `vacuous` means a suite was
    extracted and asserted nothing real, `unusable` means the Test Writer's output
    had no extractable code block at all. Reported separately, because collapsing
    them would hide which half of the mechanism is failing.

    `SELECTION_NO_APPROVAL` is reported beside `SELECTION_NO_GATE` because it has
    the same consequence and a different cause. Both return draw 1 by position, so
    both attenuate the blind gate-loss term; the difference is that no-gate had
    nothing to select with while no-approval had a gate that rejected everything.
    From the arm's side that is indistinguishable from three genuinely bad draws,
    and `wrong_suite` is what separates the two after the fact. Surfacing only the
    no-gate half made a run where the gate rejected every draw on a third of the
    tasks read as full gate availability.
    """
    suites, selections, degenerate, unapproved = {}, {}, [], []
    for record in records:
        if record.get("plan_shared"):
            key = (record["task_id"], record.get("repeat"))
            if key not in suites:
                suites[key] = (record.get("plan_tests_status") or "",
                               record.get("tests_trusted"))
        selection = record.get("selection")
        if selection:
            selections[selection] = selections.get(selection, 0) + 1
            if selection == SELECTION_NO_GATE:
                degenerate.append(record["task_id"])
            elif selection == SELECTION_NO_APPROVAL:
                unapproved.append(record["task_id"])
    by_status = {}
    for status, _trusted in suites.values():
        by_status[status or "?"] = by_status.get(status or "?", 0) + 1
    gated = sum(selections.values())
    no_gate = selections.get(SELECTION_NO_GATE, 0)
    no_approval = selections.get(SELECTION_NO_APPROVAL, 0)
    return {
        "suites": len(suites),
        "by_tests_status": by_status,
        "trusted": sum(1 for _s, trusted in suites.values() if trusted),
        "untrusted": sum(1 for _s, trusted in suites.values()
                         if trusted is False),
        "untrusted_task_ids": sorted(set(
            task_id for (task_id, _repeat), (_status, trusted)
            in suites.items() if trusted is False)),
        "a_prime3_cells": gated,
        "by_selection": selections,
        "selection_no_gate": no_gate,
        "selection_no_gate_rate": _pct(no_gate, gated),
        "selection_no_gate_task_ids": sorted(set(degenerate)),
        "selection_no_approval": no_approval,
        "selection_no_approval_rate": _pct(no_approval, gated),
        "selection_no_approval_task_ids": sorted(set(unapproved)),
        "wrong_suite": wrong_suite_report(_a_prime3_draw_rows(records)),
    }


def summarise(records):
    """Stratified by tier and by family. There is no headline number here.

    A pooled pass rate is a weighted average of whichever tiers happen to have
    the most families, so it is reported for completeness and is not a result.

    Tasks that lost a cell to infrastructure are removed here, before `_bucket`
    ever sees them, so no rate anywhere in the output is computed over a record
    that has no `passed` field to compute it from.
    """
    excluded = _excluded_task_ids(records)
    summary = {"infra_loss": _infra_summary(records, excluded)}
    records = [r for r in records if r.get("task_id") not in excluded]
    # Over the same records the grid is, i.e. after the infra-loss exclusion. A
    # no-gate rate computed over a wider set than the rates it qualifies would not
    # describe them.
    summary["gate_availability"] = _gate_availability(records)
    summary["by_arm"] = {}
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
            if name.endswith(PARTIAL_SUFFIX):
                # An interrupted `save_record`, not a grid cell. Skipped by name
                # rather than by relying on `.partial` not ending in `.json`, so
                # the exclusion survives a change to either suffix -- and skipped
                # rather than parsed, because a half-written record would take
                # the whole aggregation down with a JSONDecodeError.
                continue
            if name.endswith(".json"):
                with open(os.path.join(directory, name)) as handle:
                    records.append(json.load(handle))
    return records

# -------------------------------------------------------------------------- cli

# The keys `resolved_roles` puts on a row that are not sampling parameters. Named
# here so the header below can print "everything else", which is the only form of
# that line that stays correct when a role gains a parameter.
_RESOLVED_IDENTITY_KEYS = ("role", "provider", "model", "base_url")


def sampling_extras_text(entry):
    """A resolved row's sampling parameters beyond temperature and top_p.

    Rendered by exclusion rather than by name, because the failure this closes is
    a parameter that is being *sent* while the run's own header says it is not.
    `ROLE_PARAMS` carries `reasoning_format="hidden"` for the Executor (D-6, and
    D-14 for why it is installed rather than only registered): a header hardcoded
    to `(temperature=%g top_p=%g)` printed a two-parameter call while a
    three-parameter call went out on the wire.

    Shared by both sweep entry points on purpose. Two copies of this line drifted
    once already -- `run_eval` printed the extras and `calibrate` did not -- and
    the reader of a calibration log has no way to tell which of the two they are
    looking at.
    """
    return "".join(
        " %s=%r" % pair for pair in sorted(entry.items())
        if pair[0] not in _RESOLVED_IDENTITY_KEYS + ("temperature", "top_p"))


def print_resolved_models(resolved, source=None):
    """The models header, identically in both sweeps."""
    print("models (%s):"
          % (agents_core.model_config_source() if source is None else source))
    for entry in resolved:
        print("  %-12s %-8s %s  (temperature=%g top_p=%g%s)"
              % (entry["role"], entry["provider"], entry["model"],
                 entry["temperature"], entry["top_p"],
                 sampling_extras_text(entry)))
    for line in agents_core.independence_warnings():
        print("  WARNING: %s" % line)


def _keys_from_env():
    """Delegated, so a provider added by configuration gets a key channel too.

    This used to be a two-entry literal, which quietly made "adding a provider is
    configuration, not a code edit" false: the new provider would resolve, and
    then have no key. Kept under this name because the suites patch it here.
    """
    return agents_core.keys_from_env()


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
    parser.add_argument("--live-models", default=None,
                        help="comma-separated model IDs the stub pretends the key "
                             "can reach; every other slug 404s. Requires --stub. "
                             "This is how the preflight is demonstrated against "
                             "the *real* retired slug with no key and no network: "
                             "--live-models gemini-3.6-flash leaves the Executor "
                             "pointed at llama-3.3-70b-versatile and the run is "
                             "refused by name.")
    parser.add_argument("--force", action="store_true",
                        help="rerun tasks that already have a result file")
    parser.add_argument("--no-preflight", action="store_true",
                        help="skip the (provider, model) validation. Only reason "
                             "to: watching how a dead slug behaves *mid-run*, "
                             "which is what --bad-slug demonstrated before the "
                             "preflight started catching it at startup.")
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


INFRA_LOSS_NOTE = (
    "infra = cells where no answer came back at all: rate limit exhausted, 5xx, "
    "a dead model slug, a missing key. They are not graded and carry no `passed` "
    "field, because a rate-limit death is not evidence about a model. Exclusion "
    "is at TASK granularity, not cell: arm B makes the most calls and so absorbs "
    "the most 429s, and dropping only the dead cell would reweight the task set "
    "toward the arms that survived. The excluded task ids are listed in "
    "summary.json under `infra_loss.task_ids_excluded`.")


NO_GATE_NOTE = (
    "no-gate = tasks whose acceptance suite was not trustworthy, so A'@3 had no "
    "gate to select with and returned draw 1 by position. Each one attenuates the "
    "blind gate-loss term A'@3_gate - A'@3_oracle toward zero, so this rate has to "
    "be read BEFORE the pass rates below and not after them. `unusable` is the "
    "Test Writer producing no extractable code block; `vacuous` is a suite that "
    "extracted cleanly and asserts nothing real. Both are honest fallbacks and "
    "neither is patched around.")


NO_APPROVAL_NOTE = (
    "no-approval = A'@3 cells where the gate existed, was trustworthy, and "
    "approved none of the three draws, so draw 1 was returned by position. Same "
    "consequence as no-gate -- the blind gate-loss term is attenuated -- but a "
    "different cause, and from inside the arm it is indistinguishable from three "
    "genuinely bad draws. `wrong-suite` below is the post-hoc separator: a "
    "trustworthy suite that rejects draws the HIDDEN suite passes is a suite "
    "testing something the task never asked for, not a model failing. It is "
    "counted and named, never repaired: loosening a gate that is being measured "
    "for its error rate would destroy the measurement.")


def _print_gate_availability(gate):
    """Printed first, and printed even when it is all zeroes.

    Before the tables on purpose: it is the denominator qualifier for every rate
    in them. A silent zero and a silent third of the task set read identically in
    a report, and only one of them means the grid below is the mechanism.
    """
    print("gate availability: %d suite(s), %d trusted, %d untrusted"
          % (gate["suites"], gate["trusted"], gate["untrusted"]))
    print("  tests_status: %s"
          % (", ".join("%s=%d" % pair
                       for pair in sorted(gate["by_tests_status"].items()))
             or "(none recorded)"))
    print("  A'@3 selection: %s"
          % (", ".join("%s=%d" % pair
                       for pair in sorted(gate["by_selection"].items()))
             or "(no A'@3 cells)"))
    print("  SELECTION_NO_GATE: %d/%d = %.1f%% of A'@3 cells%s"
          % (gate["selection_no_gate"], gate["a_prime3_cells"],
             gate["selection_no_gate_rate"],
             "" if not gate["selection_no_gate_task_ids"]
             else "  (%s)" % ", ".join(gate["selection_no_gate_task_ids"][:6])))
    # Beside the line above, never instead of it. Two fallbacks to draw 1, two
    # causes, and reporting only the first one lets a run where the gate rejected
    # everything read as a run where the gate worked.
    print("  SELECTION_NO_APPROVAL: %d/%d = %.1f%% of A'@3 cells%s"
          % (gate["selection_no_approval"], gate["a_prime3_cells"],
             gate["selection_no_approval_rate"],
             "" if not gate["selection_no_approval_task_ids"]
             else "  (%s)" % ", ".join(
                 gate["selection_no_approval_task_ids"][:6])))
    _print_wrong_suite(gate.get("wrong_suite") or {})
    if gate["untrusted"]:
        print("\n" + NO_GATE_NOTE)
    if gate["selection_no_approval"] or (gate.get("wrong_suite") or {}).get("count"):
        print("\n" + NO_APPROVAL_NOTE)


def _print_wrong_suite(wrong):
    """One line, and it says which of the two zeroes it is.

    "No task had a wrong suite" and "no draw has been graded, so the question was
    never asked" are the same `0` and they mean opposite things. The graded-draw
    denominator is on the line for that reason.
    """
    graded = wrong.get("graded_draws", 0)
    if not graded:
        print("  wrong-suite: not computed (no graded draw stored on any "
              "candidate)")
        return
    print("  wrong-suite: %d/%d task(s) = %.1f%% -- trusted suite, every graded "
          "draw passes hidden, gate approved none%s"
          % (wrong.get("count", 0), wrong.get("tasks_with_a_graded_draw", 0),
             wrong.get("rate_of_graded_tasks", 0.0),
             "" if not wrong.get("tasks")
             else "  (%s)" % ", ".join(wrong["tasks"][:6])))


def _print_summary(summary):
    untrusted_total = 0
    _print_gate_availability(summary["gate_availability"])
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

    # Printed whether or not it is zero. A silent zero and a silent thirty read
    # identically, and only one of them means the rates above are complete.
    #
    # Still after the tables, unlike the gate-availability block above it. The two
    # are not the same kind of qualifier: the `n` column already reflects the
    # infra-loss exclusion, so nothing above this line is misleading, whereas an
    # unmeasured no-gate rate changes how the pass rates themselves should be read.
    # (This used to say the placement was forced by a check pinning the table
    # header to the third line of output. That check now finds the header by its
    # `pass%` column instead, so the placement is a judgement and not a constraint.)
    infra = summary.get("infra_loss") or {}
    print("\ninfra loss: %d cell(s), %d task(s) excluded, %d cell(s) dropped "
          "with them%s"
          % (infra.get("cells", 0), infra.get("tasks_excluded", 0),
             infra.get("cells_dropped_with_them", 0),
             "" if not infra.get("by_arm")
             else "  (by arm: %s)" % ", ".join(
                 "%s=%d" % pair for pair in sorted(infra["by_arm"].items()))))
    if untrusted_total:
        print("\n" + UNTRUSTED_NOTE)
    if infra.get("cells"):
        print("\n" + INFRA_LOSS_NOTE)


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

    # The task set is verified against `eval/tasks.lock` before a single call is
    # spent, and a mismatch refuses to run. 27ms for 36 tasks x 3 digests, which
    # is nothing next to what it protects: if the hidden suite moves under a
    # half-finished grid, the results before and after are being pooled into one
    # pass rate that describes neither, and there is no way to notice afterwards.
    #
    # Refusing, rather than warning. A warning at the top of a sweep that then
    # prints two hundred result lines is a warning nobody reads.
    lock_failures, lock_reason = gen_tasks.verify_lock_or_reason(
        tasks=tasks, seed=args.seed)
    if lock_reason:
        print("refusing to run: %s" % lock_reason, file=sys.stderr)
        return 2
    if lock_failures:
        print("refusing to run: the task set does not match %s (%d problem%s):"
              % (os.path.basename(gen_tasks.LOCK_PATH), len(lock_failures),
                 "" if len(lock_failures) == 1 else "s"), file=sys.stderr)
        for line in lock_failures:
            print("  FAIL %s" % line, file=sys.stderr)
        print("Nothing here is measurable against a task set that moved under "
              "it. Restore the generator, or write a new lock deliberately and "
              "treat the existing results as belonging to the old task set.",
              file=sys.stderr)
        return 2
    lock = gen_tasks.load_lock()
    print("tasks.lock verified: %d locked task(s), seed %d, self-check passed "
          "at %s" % (lock["counts"]["tasks"], lock["generator"]["seed"],
                     lock["self_check"]["at"]))

    # D7. Selection above is generation order and stays that way -- the lock
    # verifies against it and `--limit` must keep meaning the same thing -- and
    # only the order the sweep walks is permuted. Recorded in the manifest below,
    # because an order nobody can reconstruct is not a controlled variable.
    generated = [task.task_id for task in tasks]
    tasks = _task_order(tasks, args.seed)
    print("task order: seeded permutation from seed %d, not generation order, so "
          "a sweep that stops early does not stop on a family boundary."
          % args.seed)

    # Resolved before anything is spent, and printed, because a run that cannot
    # say which model each role will call has nothing to put in its own record.
    # With no config present this installs nothing and the source stays
    # "defaults", so a sweep run today behaves exactly as it did.
    #
    # Before the keys are read, not after: a provider added by configuration
    # names its own key variable, so the key lookup has to see the resolved
    # `PROVIDERS` or that provider silently has no key.
    try:
        resolved = agents_core.configure_models()
    except agents_core.ConfigError as exc:
        print("refusing to run: model configuration: %s" % exc, file=sys.stderr)
        return 2
    keys = _keys_from_env()
    print_resolved_models(resolved)

    live_models = None
    if args.bad_slug:
        if not args.stub:
            print("--bad-slug requires --stub; it is a demonstration, not a "
                  "way to break a real run", file=sys.stderr)
            return 2
        live_models = set(model for _provider, model, _roles
                          in agents_core.resolved_pairs())
        agents_core.PROVIDERS[args.bad_slug]["model"] = "model-does-not-exist"
        # And any role override pointing at that provider, or the override would
        # keep the role on a live slug and the demonstration would be partial.
        for role, provider in agents_core.ROLE_PROVIDER.items():
            if provider == args.bad_slug:
                agents_core.ROLE_MODEL[role] = "model-does-not-exist"
        print("--bad-slug %s: its model ID is now %r, so every %s call 404s."
              % (args.bad_slug,
                 agents_core.PROVIDERS[args.bad_slug]["model"], args.bad_slug))
    if args.vacuous_plan and not args.stub:
        print("--vacuous-plan requires --stub; it shapes the stub Planner's "
              "output and cannot touch a real one", file=sys.stderr)
        return 2
    if args.live_models is not None:
        if not args.stub:
            print("--live-models requires --stub; it tells a stand-in what to "
                  "pretend a key can reach and has no meaning against a real "
                  "provider", file=sys.stderr)
            return 2
        live_models = set(name.strip() for name in args.live_models.split(",")
                          if name.strip())
        print("--live-models: the stub answers only for %s; every other "
              "configured slug 404s."
              % ", ".join(sorted(live_models) or ["<nothing>"]))
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
            print("no key for %s (set %s), or pass "
                  "--stub to validate this script offline"
                  % (", ".join(absent), agents_core.key_env_hint()),
                  file=sys.stderr)
            return 2
        keys = dict((name, key) for name, key in keys.items() if key)
    else:
        # Every configured provider, not a hardcoded pair: a config that adds an
        # OpenAI-compatible endpoint must not need a code edit here to be stubbed.
        keys = dict((name, "stub") for name in agents_core.PROVIDERS)

    # Before anything is spent. Two calls -- one per distinct (provider, model)
    # pair, and `planner` and `test_writer` share one today -- convert a silent
    # multi-hour loss into a one-second refusal. That loss is not hypothetical:
    # Groq's slug retired under a frozen grid and Groq is the Executor in all four
    # arms, so every arm was pointed at a dead model and nothing said so until a
    # task failed. Refusing rather than warning, exactly as `--verify-lock` does.
    #
    # Stubbed sweeps preflight through the stub, so `--bad-slug` demonstrates the
    # refusal with no key and no network at all.
    preflight_records = []
    if args.no_preflight:
        print("preflight SKIPPED (--no-preflight): a dead (provider, model) pair "
              "will now surface as a failed task mid-run instead of a refusal.")
    else:
        call = None if stub is None else stub.probe
        preflight_records = agents_core.preflight(keys, call=call)
        problems = agents_core.preflight_failures(preflight_records)
        if problems:
            print("refusing to run: %d configured (provider, model) pair(s) do "
                  "not answer:" % len(problems), file=sys.stderr)
            for line in problems:
                print("  FAIL %s" % line, file=sys.stderr)
            print("Point the role at a model the key can reach -- "
                  "`python3 eval/models.py --list <provider>` says which -- or "
                  "pass --no-preflight to spend the grid anyway.",
                  file=sys.stderr)
            return 2
        print("preflight OK: %s"
              % "; ".join("%s -> %s/%s" % ("+".join(record["roles"]),
                                           record["provider"], record["model"])
                          for record in preflight_records))

    # Not optional and not configurable. Silent failover turns an arm into a
    # provider mixture whose composition tracks rate-limit pressure, and the
    # mixture correlates with the arm, so no amount of pairing cancels it.
    agents_core.set_measurement_mode(True)
    print("measurement mode ON: every call is pinned to one provider; a dead "
          "provider fails the task instead of silently rerouting to the other.")

    # One retry layer, not two. `Instrument` below wraps `call_model` and runs its
    # own 429 loop bounded by MAX_429_RETRIES, honouring the server's Retry-After
    # through the governor; `agents_core` has a loop of its own bounded by
    # MAX_PROVIDER_ATTEMPTS. Leaving both at their defaults multiplies them --
    # 5 x 5 is 25 requests to a provider that has just said "slow down" -- so the
    # inner one is pinned to a single attempt here and the outer one is the
    # authority. Both bounds are written into the manifest so a run states which
    # layer retried it. Same reasoning for pacing: `agents_core.PACER` ships
    # disabled and `RateGovernor` does the pacing in this process.
    agents_core.set_retry_attempts(1)
    governor = RateGovernor()
    print("retry: RateGovernor + Instrument (%d retries, Retry-After honoured); "
          "agents_core retry pinned to 1 attempt so the two bounds do not "
          "multiply. Pacing: %s calls/min."
          % (MAX_429_RETRIES,
             ", ".join("%s=%g" % pair for pair in sorted(DEFAULT_RATES.items()))))
    if args.repeats == 1:
        print("--repeats 1: one draw per task. A per-family difference of one "
              "or two tasks is noise, not a finding.")
    print("%d tasks x %d arm(s) x %d repeat(s) = %d runs%s"
          % (len(tasks), len(arms), args.repeats,
             len(tasks) * len(arms) * args.repeats,
             " (stub: %s)" % args.stub if stub else ""))

    started = time.time()
    done = 0
    # One event log for the whole sweep rather than one per cell. Which cell an
    # event came from rides on the event (`scope` below); a file per cell would
    # shatter the single chronology this log exists to provide -- what was
    # happening when the sweep died -- across several hundred files.
    #
    # Installed process-wide so `run_workspace` picks it up ambiently. Arm B calls
    # `run_workspace`, which opens a log of its own only when none is installed,
    # so arm B's events land here carrying task/arm/repeat instead of in a sibling
    # file that has no idea which cell produced them.
    # `seed` in the base context, so an event names its own cell without anyone
    # having to know which directory the file was read from. `(seed, task_id, arm,
    # repeat)` is the result path, so every event joins to exactly one record.
    # There is no per-cell `run_id` to put here: three of the four arms make a
    # bare provider call and never create a `Memory`, so only arm B has one, and
    # `run_workspace` scopes it onto its own events itself.
    events = agents_core.EventLog(
        os.path.join(root, "seed-%d" % args.seed, "events.jsonl"),
        context={"seed": args.seed}, keys=keys)
    with Instrument(governor, stub=stub, verbose=args.verbose) as instrument, \
            _InstalledEventLog(events):
        for repeat in range(args.repeats):
            for task in tasks:
                pending = []
                for arm in _arm_order(arms, args.seed, task, repeat):
                    path = result_path(root, args.seed, arm, task, repeat)
                    if os.path.exists(path) and not args.force:
                        if args.verbose:
                            print("  skip %s arm %s (already done)"
                                  % (task.task_id, arm))
                        # Logged, because a resumed sweep and a fresh one produce
                        # the same summary from different amounts of work and the
                        # difference has to be visible somewhere. Zero provider
                        # calls happen for this cell; that is the point of the
                        # event. Basename only -- the absolute path carries the
                        # user's home directory and says nothing extra.
                        events.emit(agents_core.EVENT_RESUME_SKIP,
                                    task_id=task.task_id, arm=arm,
                                    repeat=repeat,
                                    result=os.path.basename(path))
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
                        # `phase`, not `arm`: this call belongs to no arm -- all
                        # three plan arms share it -- and writing an arm name here
                        # would let a reader attribute one shared call to one arm.
                        with events.scope(task_id=task.task_id, repeat=repeat,
                                          phase="plan"):
                            plan = make_plan(task, keys)
                        plan["error"] = ""
                    except agents_core.ProviderError as exc:
                        # In measurement mode this is the designed outcome of a
                        # dead provider: every plan-consuming arm on this task
                        # is recorded as failed. Loud, attributable, and not
                        # quietly rerouted to the other provider.
                        #
                        # The status and the class travel with the message, because
                        # `run_one` re-raises this as a `ProviderError` and its
                        # `infra_loss`/`error` split turns entirely on the status.
                        # Without them a rate-limited Planner would reach the
                        # record as a statusless failure, which does not retry and
                        # cannot be told apart from a dead model slug.
                        plan = {"error": "planning failed: %s"
                                         % agents_core._redact(exc, keys),
                                "error_status": getattr(exc, "status", None),
                                "error_class": getattr(exc, "exc_class", "")}
                        print("  !! %s plan failed: %s"
                              % (task.task_id, plan["error"][:100]))
                    plan["calls"] = instrument.calls
                    plan["call_log"] = _call_log_slice(0)

                for arm in pending:
                    with events.scope(task_id=task.task_id, arm=arm,
                                      repeat=repeat):
                        record = run_one(task, arm, keys, instrument, repeat,
                                         stub, plan if arm in PLAN_ARMS else None)
                    save_record(result_path(root, args.seed, arm, task, repeat),
                                record)
                    done += 1
                    # `.get`, not `[...]`. An `infra_loss` record deliberately
                    # carries neither `passed` nor `false_approved` -- that is the
                    # representation, not an omission -- so indexing them here
                    # would turn every rate-limit loss into a KeyError that kills
                    # the sweep at exactly the moment it is trying to survive one.
                    print("  %-8s %-24s %-9s %-11s %5.1fs %2d calls%s"
                          % (arm, task.task_id,
                             _outcome_label(record),
                             record.get("verdict", ""), record["seconds"],
                             record["calls"],
                             "  FALSE APPROVED" if record.get("false_approved")
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
    print("%d event(s) written to %s%s"
          % (events.count, events.path,
             " (%d write(s) failed)" % events.failed if events.failed else ""))

    records = load_records(root, args.seed, arms)
    summary = summarise(records)
    summary["rate_limits"] = governor.rate_limits
    summary["seed"] = args.seed
    summary["stub"] = args.stub
    summary["repeats"] = args.repeats
    # The pacing and retry policy this sweep actually ran under, not the module
    # defaults a reader would otherwise have to assume. Both layers are recorded
    # -- the governor's rates and bounds, and `agents_core`'s pinned attempt count
    # and (disabled) pacer -- so a run states which layer retried it and how long
    # it was willing to wait. The `agents_core` rates are conservative guesses
    # rather than documented free-tier limits; `MIN_CALL_INTERVAL_SECONDS` says so
    # where they are defined, and `pacing.intervals` here carries the numbers.
    summary["pacing"] = {"governor_rates_per_min": DEFAULT_RATES,
                         "governor_burst": BURST,
                         # False on a stub sweep: nothing was paced because
                         # nothing left the process. The rates above are still
                         # recorded, as policy rather than as what was spent.
                         "governor_applied": stub is None,
                         "governor_max_429_retries": MAX_429_RETRIES,
                         "governor_fallback_backoff": list(FALLBACK_BACKOFF),
                         "agents_core_pacer": agents_core.PACER.snapshot(),
                         "agents_core_retry": agents_core.retry_snapshot()}
    summary["events"] = {"path": os.path.basename(events.path),
                         "count": events.count,
                         "write_failures": events.failed}
    # The order this sweep actually walked, and where each task sat in generation
    # order. Both, because the permutation is only meaningful against the order it
    # permuted, and a reader who has only the run order cannot tell a shuffle from
    # a generator that changed.
    summary["task_order"] = {
        "seed": args.seed,
        "shuffled": True,
        "basis": "sha256('task-order|<seed>'), selection unchanged",
        "order": [task.task_id for task in tasks],
        "permutation": [generated.index(task.task_id) for task in tasks]}
    # Which task set this is, by digest, so a results directory is attributable
    # without trusting the directory name or the clock.
    summary["tasks_lock"] = {"path": os.path.basename(gen_tasks.LOCK_PATH),
                             "body_sha256": lock.get("body_sha256", ""),
                             "locked_tasks": lock["counts"]["tasks"],
                             "seed": lock["generator"]["seed"],
                             "self_check_at": lock["self_check"]["at"],
                             "verified_at_startup": True}
    # Which models this sweep actually used, resolved, plus how that was decided
    # and whether each pair answered before anything was spent. Resolved after the
    # run rather than restated from source so `--bad-slug`'s mutation appears here
    # too: a manifest that could not contradict source would be worth nothing as a
    # record of what ran. D2 freezes this table, not the constants it came from.
    summary["models"] = {
        "source": agents_core.model_config_source(),
        "resolved": agents_core.resolved_roles(),
        "preflight": preflight_records,
        "preflight_skipped": bool(args.no_preflight),
        "independence_warnings": agents_core.independence_warnings()}
    for line in summary["models"]["independence_warnings"]:
        print("WARNING: %s" % line)
    summary_path = os.path.join(root, "seed-%d" % args.seed, "summary.json")
    save_record(summary_path, summary)
    _print_summary(summary)
    print("\nsummary: %s" % summary_path)
    return 0


def cli(argv=None):
    """`main`, plus the one failure that is not a per-cell outcome.

    A per-day quota is a fact about the day. `run_one` re-raises it rather than
    scoring it as `infra_loss`, so it arrives here, and here it is a message and
    an exit code instead of a traceback. Every cell that completed is already
    written to disk by `save_record`, so a re-run after the window resets resumes
    from them; the summary is deliberately not written, because the sweep did not
    finish and a summary over a truncated grid is the thing nobody should find
    later and mistake for a result.
    """
    try:
        return main(argv)
    except agents_core.DailyQuotaExhausted as exc:
        print("\nABORTED: %s" % exc, file=sys.stderr)
        print("Completed cells are on disk. Re-run the same command once the "
              "quota window has reset and it resumes from them.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(cli())

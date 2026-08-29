"""Code-focused multi-agent pipeline.

Planner -> Executor -> Harness (-> Orchestrator), with the pipeline for each
mode declared explicitly in PIPELINES rather than sliced out of a list.

The verification stage is not an LLM. The Planner emits an acceptance suite of
plain asserts alongside the spec; `harness.py` extracts the code the Executor
produced and runs it *against that suite*. A step is APPROVED only when the
suite passes -- so code that runs but computes the wrong answer is caught. When
it fails, the real traceback (and the exact failing assertion) is what gets
handed back to the Executor as the fixes.

The suite is audited before it is trusted: a suite that passes against a stub
solution proves nothing, and gating APPROVED on it would rebuild the fake
verification this pipeline exists to remove.
"""

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import harness

# How many times a single step may be sent back for revision.
MAX_REVISION_ROUNDS = 3

# What to do after the harness rejects a step, in order -- one rung per revision
# round, so len(ESCALATION) must equal MAX_REVISION_ROUNDS.
#
# "Try again" is not a strategy. Handing the same model the same traceback three
# times reliably produces three variations on one wrong idea, so each rung
# changes something structural: first the information available, then the model,
# then the approach. This replaces the Orchestrator, which was asked what to do
# next and had no way to act on the answer.
ESCALATION = ("repair", "alternate", "fresh")

ESCALATION_WHY = {
    "repair": "hand back the real traceback and let the same model fix it",
    "alternate": "same traceback, different model",
    "fresh": "start over from the spec on a different model, no failed code",
}

# Context budget. The rolling context used to be built by string concatenation
# with no ceiling, so it grew with every step and every revision until the
# provider rejected the request. It is now assembled from structured state and
# clamped at every level.
MAX_SPEC_CHARS = 3000
MAX_STEP_OUTPUT_CHARS = 1500
MAX_CONTEXT_CHARS = 8000
CONTEXT_RECENT_STEPS = 2

# Guard against a planner that emits a hundred "steps".
MAX_STEPS = 12

PROVIDERS = {
    "gemini": {
        # Verified against ai.google.dev/gemini-api/docs/models (2026-08-28):
        # gemini-3.6-flash is a real, stable model ID. Use the rolling alias
        # "gemini-flash-latest" if you would rather not pin a generation.
        "model": "gemini-3.6-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "groq": {
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
    },
}

# Only model-backed roles appear here. The harness runs code, so it has no
# provider and cannot be swapped for one.
ROLE_PROVIDER = {
    "planner": "gemini",
    "test_writer": "gemini",
    "executor": "groq",
}

# Sampling is pinned here rather than left to the endpoint. Free tiers override
# unpinned parameters with their own defaults and change them without notice, so
# a run that does not state top_p is not reproducible even against the same
# model ID.
TEMPERATURE = 0.4
TOP_P = 1.0

# Silent failover is a convenience in a UI and a contaminant in a measurement.
# `call_role` walks every configured provider on any failure, so under 429
# pressure -- which arm B absorbs roughly ten times more of than a single-shot
# arm -- "arm B" quietly becomes a provider mixture whose composition tracks
# rate-limit pressure, and therefore time of day. Task-level pairing cannot
# cancel a mix that correlates with the arm. Worse, one wrong model slug is
# enough to route an entire grid to the other provider while the report still
# claims two.
#
# Measurement mode pins each call to exactly one provider and turns a dead
# provider into a failed task, which is loud. Read once at import so a run
# cannot change policy halfway through; `set_measurement_mode` exists for tests.
MEASUREMENT_MODE = os.environ.get("MAW_MEASUREMENT", "").strip().lower() not in (
    "", "0", "false", "no")

# Every model call, in order, for the caller to record. Bounded because a long
# UI session would otherwise accumulate one entry per call forever.
CALL_LOG = []
MAX_CALL_LOG = 200


def set_measurement_mode(enabled):
    """Turn silent provider failover off (True) or on (False). Returns the new
    value. Only the environment should set this in production; this exists so a
    check can exercise both policies in one process."""
    global MEASUREMENT_MODE
    MEASUREMENT_MODE = bool(enabled)
    return MEASUREMENT_MODE


def reset_call_log():
    del CALL_LOG[:]
    return CALL_LOG

# The pipeline for each mode, stated outright. Every stage listed here is a
# stage that actually runs.
PIPELINES = {
    2: ("planner", "executor"),
    3: ("planner", "executor", "harness"),
}
DEFAULT_MODE = 3

PIPELINE_LABELS = {
    2: "Planner -> Executor (no verification)",
    3: "Planner -> Executor -> Harness (code is run against the suite)",
}

# Mode 4 was Planner -> Executor -> Harness -> Orchestrator. The Orchestrator
# was a model asked to comment on progress: it cost one call per step, could not
# change any outcome, and its assessments regularly contradicted results the
# harness had already established by running the code. Deciding what to do after
# a failure is now deterministic Python (see ESCALATION) -- no model is asked
# what to try next, because that decision needs no judgement.
#
# The mapping is declared rather than left to resolve_mode's fallback, so a
# saved mode-4 config gets an explanation instead of silently changing pipeline.
RETIRED_MODES = {
    4: (3, "Mode 4 (the Orchestrator) has been retired. It spent a model call "
           "per step to narrate results the harness had already proven, and "
           "could not act on them. Escalation after repeated failure is now a "
           "deterministic policy. Running mode 3 instead."),
}


def retired_mode_note(mode):
    """The explanation for a retired mode, or "" for a live one."""
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        return ""
    entry = RETIRED_MODES.get(mode)
    return entry[1] if entry else ""


# Where a suite came from, and whether APPROVED may be gated on it.
TESTS_USER = "user"
TESTS_GENERATED = "generated"
TESTS_REGENERATED = "regenerated"
TESTS_VACUOUS = "vacuous"
TESTS_MISSING = "missing"
TESTS_UNUSABLE = "unusable"

TRUSTED_TESTS = (TESTS_USER, TESTS_GENERATED, TESTS_REGENERATED)


class ProviderError(Exception):
    pass


def resolve_mode(mode):
    """Coerce a UI value into a known pipeline mode.

    Retired modes map to their declared replacement rather than falling through
    to the default, so ``retired_mode_note`` can explain the substitution.
    """
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        return DEFAULT_MODE
    if mode in PIPELINES:
        return mode
    if mode in RETIRED_MODES:
        return RETIRED_MODES[mode][0]
    return DEFAULT_MODE


def pipeline_for(mode):
    return PIPELINES[resolve_mode(mode)]


def required_providers(mode):
    """Which API keys a given mode actually needs."""
    return sorted({ROLE_PROVIDER[stage] for stage in pipeline_for(mode)
                   if stage in ROLE_PROVIDER})


def call_model(provider, api_key, system, user):
    cfg = PROVIDERS[provider]
    try:
        # Imported lazily so this module (and the offline test suites) load
        # without the SDK installed.
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=cfg["base_url"], timeout=60)
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=TEMPERATURE,
            top_p=TOP_P,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        raise ProviderError("%s failed: %s" % (provider, exc))


class RoleCall(tuple):
    """``(text, provider_used)`` -- plus ``.record``, the call's provenance.

    A plain 2-tuple to every existing caller, so adding provenance did not
    change the shape of `call_role`'s contract or of the checks that pin it.

    (No ``__slots__``: a non-empty one is rejected on a variable-length builtin
    base, and an empty one would leave nowhere to hang the record.)
    """

    def __new__(cls, text, provider, record):
        self = tuple.__new__(cls, (text, provider))
        self.record = record
        return self


MIN_REDACTABLE_KEY = 4


def _redact(text, keys):
    """Never let a key value reach a log line, even inside an SDK error.

    Two limits, stated rather than hidden. The length floor exists so a stub key
    like `"k"` does not turn every `k` in a traceback into `<redacted>`; it is
    set low (4) because a short real key leaking is far worse than an
    over-redacted test message. And this is exact-substring matching, so an SDK
    that prints a key *elided* (`AIzaSy...9Qk`) defeats it -- the visible prefix
    is also scrubbed for that reason, but a middle-elided rendering with a short
    prefix can still get through. The load-bearing guarantee is `_child_env` and
    "keys are never put in a prompt"; this is the second layer, not the first.
    """
    out = str(text)
    for value in (keys or {}).values():
        value = str(value or "")
        if len(value) < MIN_REDACTABLE_KEY:
            continue
        out = out.replace(value, "<redacted>")
        if len(value) >= 12:
            # Truncated renderings: scrub the prefix an SDK would show.
            out = out.replace(value[:8], "<redacted>")
    return out


def _log_call(record):
    CALL_LOG.append(record)
    if len(CALL_LOG) > MAX_CALL_LOG:
        del CALL_LOG[:len(CALL_LOG) - MAX_CALL_LOG]
    return record


def call_role(role, keys, system, user, emit=None, prefer=None):
    """Call a role's provider. Returns ``(text, provider_used)``.

    Outside measurement mode this falls back to the other providers that have
    keys -- convenient in a UI, where any answer beats a stack trace. It also
    replaces the four copy-pasted try/except handoff blocks the pipeline used to
    carry.

    ``prefer`` puts one provider at the front of the order. The escalation
    ladder uses it to retry a stuck step on a different model.

    Under ``MEASUREMENT_MODE`` the order is exactly one provider -- ``prefer``
    when it is configured, otherwise the role's own -- and a failure raises.
    ``prefer`` still works, because the ladder's `alternate` rung is a
    deliberate, logged, recorded switch that is part of arm B's definition; it
    is only the *silent* substitution that is fatal to a measurement. Every
    attempt is logged with the provider requested and the provider used, so a
    run in which those two ever differ here is a bug in this function and not a
    finding about models.
    """
    primary = ROLE_PROVIDER[role]
    requested = prefer if prefer in PROVIDERS else primary
    if MEASUREMENT_MODE:
        order = [requested]
    else:
        order = [primary] + [name for name in PROVIDERS if name != primary]
        if prefer in PROVIDERS:
            order = [prefer] + [name for name in order if name != prefer]
    attempted, last_error = [], None

    for provider in order:
        key = (keys or {}).get(provider)
        if not key:
            continue
        attempted.append(provider)
        record = {
            "role": role,
            "requested": requested,
            "used": provider,
            "model": PROVIDERS[provider]["model"],
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "measurement_mode": MEASUREMENT_MODE,
            "ok": False,
            "error": None,
        }
        _log_call(record)
        try:
            text = call_model(provider, key, system, user)
        except ProviderError as exc:
            record["error"] = _redact(exc, keys)
            last_error = exc
            if emit and not MEASUREMENT_MODE:
                emit("system", "%s failed for %s; trying next provider" % (provider, role))
            continue
        record["ok"] = True
        return RoleCall(text, provider, record)

    if MEASUREMENT_MODE:
        # Loud on purpose. A dead slug or a missing key must stop the task, not
        # reroute the grid to whichever provider happens to still answer.
        if not attempted:
            raise ProviderError(
                "measurement mode: no API key for %s (%s); failover is disabled"
                % (role, requested))
        raise ProviderError(
            "measurement mode: %s failed for %s and failover is disabled: %s"
            % (requested, role, _redact(last_error, keys)))
    if not attempted:
        raise ProviderError("no API key available for %s" % role)
    raise ProviderError("all providers failed for %s: %s"
                       % (role, _redact(last_error, keys)))


def alternate_provider(role, keys):
    """A provider for ``role`` other than its usual one, if a key exists.

    Returns ``None`` when only the primary is configured -- the caller then
    knows the "try a different model" rung is unavailable rather than silently
    re-running the same one and calling it an escalation.
    """
    primary = ROLE_PROVIDER[role]
    for name in PROVIDERS:
        if name != primary and (keys or {}).get(name):
            return name
    return None


# The rules the acceptance suite must obey. Shared between the Planner prompt
# and the regeneration prompt so they cannot drift apart.
_TEST_RULES = (
    "Rules for the TESTS block:\n"
    "- Exactly one ```python fenced block.\n"
    "- The Executor's program is saved as `solution.py`, so import from it: "
    "`from solution import <names>`.\n"
    "- Plain module-level `assert` statements only. NO pytest and NO unittest "
    "-- they are not installed and must not be a dependency. No test "
    "functions, no test classes; the asserts run when the file runs.\n"
    "- Assert concrete computed VALUES, e.g. `assert median([1,2,3,4]) == 2.5`. "
    "Never write assertions that would pass against an unimplemented stub -- "
    "`assert callable(f)`, `assert f is not None` and `assert hasattr(...)` are "
    "rejected, because they prove nothing.\n"
    "- Cover the normal cases AND at least one edge case.\n"
    "- Standard library only. No network, no file I/O, no subprocesses, no "
    "input(). Must finish within %d seconds.\n"
    % harness.EXEC_TIMEOUT_SECONDS
)

_INTERFACE_RULE = (
    "CRITICAL: the SPEC must name the exact public function names and their "
    "signatures that the TESTS block imports -- for example \"exposes "
    "median(values: list) -> float\". The Executor only sees the SPEC and the "
    "step, never the tests. If the SPEC does not pin the interface, the "
    "Executor and the tests will disagree on names and every run will fail on "
    "import.\n"
)

PROMPTS = {
    "planner": (
        "You are the Planner for a pipeline that EXECUTES the code it writes "
        "and grades it against tests you write now.\n"
        "The user gives a vague prompt. Rewrite it into a clear spec, break it "
        "into numbered steps, then write the acceptance tests.\n\n"
        + _INTERFACE_RULE +
        "\nRequirements for the steps:\n"
        "- Each step must be deliverable as a single self-contained Python "
        "program that runs on its own.\n"
        "- No step may require network access, subprocesses, stdin, or "
        "third-party packages beyond the standard library.\n"
        "- Prefer few, substantial steps (2-4). A later step may restate "
        "earlier code; it must not import from it.\n"
        "- Every step's program must expose the full interface named in the "
        "SPEC, because the same acceptance suite is run against each step.\n\n"
        + _TEST_RULES +
        "\nOutput format, with all three headers present:\n"
        "SPEC: <one paragraph, naming the exact functions and signatures>\n"
        "STEPS:\n1. ...\n2. ...\n"
        "TESTS:\n```python\nfrom solution import ...\nassert ...\n```\n"
    ),
    "test_writer": (
        "You are the Test Writer. You are given a spec and, possibly, a "
        "rejected previous attempt at an acceptance suite. Produce a suite "
        "that genuinely verifies the spec.\n\n"
        + _TEST_RULES +
        "\nOutput ONLY the ```python block. No prose, no headers.\n"
    ),
    "executor": (
        "You are the Executor. You are given a spec and one step.\n\n"
        "Your output MUST contain exactly one ```python fenced block holding "
        "the complete, self-contained program for this step. It is saved as "
        "solution.py and executed automatically, so:\n"
        "- It must run top-to-bottom on a bare interpreter.\n"
        "- Define every function named in the spec at module level, with the "
        "exact names and signatures given. A hidden acceptance suite imports "
        "them via `from solution import ...`.\n"
        "- Importing your file must not fail or block.\n"
        "- It must exit with status 0.\n"
        "- Do NOT write tests. A fixed acceptance suite grades you, and any "
        "test file you emit is discarded.\n\n"
        # Taken from the harness rather than restated, so the prompt cannot
        # promise a limit the sandbox does not enforce or omit one it does.
        + harness._SANDBOX_RULES + "\n\n"
        "Do not ask questions. Do not explain at length. Produce the program."
    ),
}


def clamp(text, limit):
    """Head+tail truncation with an explicit marker, so nothing grows without
    bound and the elision is visible to whoever reads it."""
    text = text or ""
    if len(text) <= limit:
        return text
    keep = max(limit - 60, 40)
    head = keep * 2 // 3
    tail = keep - head
    return "%s\n[... %d characters elided ...]\n%s" % (
        text[:head], len(text) - head - tail, text[-tail:])


class Memory:
    """One file per run. Never reads prior state.

    The old implementation loaded ``memory.json`` on construction and appended
    to it, so every run inherited the log of every previous run and the file
    grew forever. Runs are now isolated by construction.
    """

    def __init__(self, prompt="", mode=DEFAULT_MODE, dirpath="runs", run_id=None):
        self.run_id = run_id or "%s-%s" % (
            time.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])
        self.dirpath = dirpath
        self.path = os.path.join(dirpath, "%s.json" % self.run_id)
        self.data = {
            "run_id": self.run_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "prompt": prompt,
            "mode": resolve_mode(mode),
            "pipeline": list(pipeline_for(mode)),
            "log": [],
            "steps": [],
            "tests": "",
            "tests_status": "",
            "deliverable": "",
        }
        self.save()

    def add(self, role, content):
        self.data["log"].append({"role": role, "content": content})
        self.save()

    def add_step(self, record):
        self.data["steps"].append(record)
        self.save()

    def set_tests(self, source, status, summary=""):
        self.data["tests"] = source or ""
        self.data["tests_status"] = status
        self.data["tests_audit"] = summary
        self.save()

    def set_deliverable(self, text):
        self.data["deliverable"] = text
        self.data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save()

    def save(self):
        try:
            os.makedirs(self.dirpath, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            # A read-only filesystem should not take the run down with it.
            pass


_STEP_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.*)")
_HEADERS = ("SPEC", "STEPS", "TESTS")


def _section(text, name):
    """Body of a ``NAME:`` section, ending at the next known header.

    Sections are sliced before parsing so that, for instance, numbered lines
    inside the TESTS code block cannot be mistaken for plan steps.
    """
    text = text or ""
    match = re.search(r"(?:^|\n)\s*%s\s*:" % name, text)
    if not match:
        return ""
    body = text[match.end():]
    others = [h for h in _HEADERS if h != name]
    cut = len(body)
    for header in others:
        found = re.search(r"(?:^|\n)\s*%s\s*:" % header, body)
        if found:
            cut = min(cut, found.start())
    return body[:cut].strip()


def extract_steps(planner_output):
    """Numbered steps, scoped to the STEPS section when one is present."""
    scope = _section(planner_output, "STEPS") or (planner_output or "")
    steps = []
    for line in scope.splitlines():
        match = _STEP_LINE.match(line)
        if match:
            step = match.group(2).strip()
            if step:
                steps.append(step)
    return steps[:MAX_STEPS]


def extract_spec(planner_output):
    """Pull the SPEC paragraph out, falling back to the whole plan."""
    spec = _section(planner_output, "SPEC")
    return spec or (planner_output or "").strip()


def extract_tests(planner_output):
    """Pull the acceptance suite out of the TESTS section."""
    scope = _section(planner_output, "TESTS")
    if not scope:
        return None
    source, _ = harness.extract_code_block(scope, allow_tests=True)
    return source


def build_context(spec, completed):
    """Assemble a bounded context from structured state.

    ``completed`` is a list of ``(number, step, output)``. Only the most recent
    few carry their full output; older ones degrade to titles.
    """
    parts = ["SPEC:\n%s" % clamp(spec, MAX_SPEC_CHARS)]

    if len(completed) > CONTEXT_RECENT_STEPS:
        older = completed[:-CONTEXT_RECENT_STEPS]
        parts.append("Earlier steps (already done, titles only):\n%s" % "\n".join(
            "%d. %s" % (number, step) for number, step, _ in older))
        recent = completed[-CONTEXT_RECENT_STEPS:]
    else:
        recent = completed

    for number, step, output in recent:
        parts.append("Completed step %d: %s\nOutput:\n%s" % (
            number, step, clamp(output, MAX_STEP_OUTPUT_CHARS)))

    return clamp("\n\n".join(parts), MAX_CONTEXT_CHARS)


@dataclass
class StepResult:
    number: int
    step: str
    output: str = ""
    verdict: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    rounds: int = 0
    code: str = ""
    failure_kind: str = ""
    failed_assertion: str = ""
    # The line the failing assert sat on. Only `ExecResult` carried this before;
    # the candidate ranking in `_verify_step` needs it on the step too.
    failed_assertion_line: Optional[int] = None
    tested: bool = False
    note: str = ""
    # The last rung of ESCALATION used on this step, or "exhausted" if the
    # ladder ran out. "" means the step never needed a revision.
    escalation: str = ""
    sandbox_layers: List[str] = field(default_factory=list)
    # Which provider produced the retained output.
    provider: str = ""
    # Which revision round produced the candidate above. 1-based, so it is
    # comparable with `rounds` (which counts rounds *spent*, not the round the
    # kept answer came from -- with retention those are no longer the same
    # number, and conflating them is what hid the bug).
    retained_round: int = 0
    # True when the last round the ladder ran was strictly worse *on merit*
    # (`_candidate_merit`, i.e. rank without the round tie-break) than the one
    # retained: the direct measurement of the ladder being net-harmful, which
    # was invisible while the last attempt always overwrote the record.
    final_round_worse: bool = False
    # Every attempt, kept whether or not it was returned. Stored, never graded
    # here: hidden-grading candidates inline would put the answer key inside the
    # pipeline being measured.
    candidates: List[dict] = field(default_factory=list)

    @property
    def verified(self):
        return self.verdict == harness.VERDICT_APPROVED


@dataclass
class RunResult:
    run_id: str = ""
    memory_path: str = ""
    mode: int = DEFAULT_MODE
    pipeline: tuple = ()
    plan: str = ""
    spec: str = ""
    tests: str = ""
    tests_status: str = TESTS_MISSING
    tests_summary: str = ""
    steps: List[StepResult] = field(default_factory=list)
    deliverable: str = ""

    @property
    def verified_count(self):
        return sum(1 for step in self.steps if step.verified)

    @property
    def tests_trusted(self):
        return self.tests_status in TRUSTED_TESTS and bool(self.tests)


def run_workspace(user_prompt, keys, mode=DEFAULT_MODE, on_event=None,
                  user_tests=None, plan=None, tests=None):
    """Run the pipeline. ``on_event(role, content)`` is called as each entry is
    produced, so a UI can render the feed incrementally instead of waiting for
    the whole run.

    ``user_tests`` is an optional acceptance suite supplied by the user. It
    always wins over anything the Planner generates.

    ``plan`` is an optional Planner output to use verbatim, skipping the Planner
    call. Everything downstream is unchanged -- the text still goes through
    `extract_spec`, `extract_steps` and `_resolve_tests`. It exists so several
    eval arms can be compared against *the same* plan: a per-arm Planner call
    would make the plan a source of variance inside the contrast it is supposed
    to hold fixed.

    ``tests`` is an *already-resolved* suite (see `resolved_tests`), which skips
    `_resolve_tests` entirely. `plan` alone was not enough: resolving the same
    plan twice agrees only while the audit accepts the plan's own TESTS block.
    When it does not, `_resolve_tests` regenerates via the Test Writer, and two
    independent generations from one plan are two different suites -- so the arms
    gated on different bytes on exactly the tasks where the suite was weakest,
    and paid for a second Test Writer call to do it.

    Precedence: ``user_tests`` beats ``tests`` beats resolving from the plan. The
    user's own suite winning is not a rule that bends for a measurement.
    """
    if tests is not None and plan is None:
        # Injecting a suite resolved from a *different* plan is the exact
        # divergence `tests=` exists to close, and nothing downstream could
        # detect it. Programming error, not a runtime condition.
        raise ValueError("run_workspace(tests=...) requires the plan those "
                         "tests were resolved from; pass plan= as well")
    mode = resolve_mode(mode)
    stages = pipeline_for(mode)
    memory = Memory(prompt=user_prompt, mode=mode)

    def emit(role, content):
        memory.add(role, content)
        if on_event:
            on_event(role, content)

    run = RunResult(run_id=memory.run_id, memory_path=memory.path, mode=mode,
                    pipeline=stages)

    emit("system", "Run `%s` started - %s" % (memory.run_id, PIPELINE_LABELS[mode]))

    # ---- Planner -------------------------------------------------------
    if plan is None:
        plan, _ = call_role("planner", keys, PROMPTS["planner"], user_prompt, emit)
    else:
        emit("system", "Planner call skipped; a supplied plan was used verbatim")
    emit("planner", plan)
    run.plan = plan
    run.spec = extract_spec(plan)
    steps = extract_steps(plan) or [user_prompt]
    if len(steps) == 1 and steps[0] == user_prompt:
        emit("system", "No numbered steps found in the plan; treating the "
                       "prompt as a single step")

    verify = "harness" in stages

    # ---- Acceptance suite ----------------------------------------------
    if verify:
        if user_tests and user_tests.strip():
            _resolve_tests(run, plan, keys, emit, user_tests)
        elif tests is not None:
            _adopt_tests(run, tests, emit)
        else:
            _resolve_tests(run, plan, keys, emit, None)
        memory.set_tests(run.tests, run.tests_status, run.tests_summary)

    # ---- Executor (+ Harness) per step ---------------------------------
    completed = []

    for number, step in enumerate(steps, 1):
        context = build_context(run.spec, completed)
        task = "%s\n\nSTEP %d of %d: %s" % (context, number, len(steps), step)

        output, provider = call_role("executor", keys, PROMPTS["executor"], task, emit)
        emit("executor", output)

        record = StepResult(number=number, step=step, output=output,
                            provider=provider)

        if not verify:
            record.verdict = harness.VERDICT_UNVERIFIED
            record.note = "mode %d has no harness stage" % mode
            emit("system", "Step %d not verified - %s" % (number, record.note))
        else:
            record = _verify_step(record, task, keys, emit, run)

        run.steps.append(record)
        completed.append((number, step, record.output))
        memory.add_step({
            "number": record.number,
            "step": record.step,
            "verdict": record.verdict,
            "exit_code": record.exit_code,
            "rounds": record.rounds,
            "failure_kind": record.failure_kind,
            "failed_assertion": record.failed_assertion,
            "failed_assertion_line": record.failed_assertion_line,
            "tested": record.tested,
            "note": record.note,
            "escalation": record.escalation,
            "retained_round": record.retained_round,
            "final_round_worse": record.final_round_worse,
            "candidates": _candidates_for_log(record.candidates),
            "provider": record.provider,
            "stdout": record.stdout,
            "stderr": record.stderr,
            "code": record.code,
            "sandbox_layers": record.sandbox_layers,
        })

    run.deliverable = _assemble_deliverable(run)
    memory.set_deliverable(run.deliverable)
    emit("system", "Run complete - %d/%d step(s) verified against the "
                   "acceptance suite" % (run.verified_count, len(run.steps)))
    return run


def _candidates_for_log(candidates):
    """The candidate list as it goes into the run JSON.

    Drops the raw model output and the captured streams -- those exist to
    restore and explain the retained attempt, and duplicating them per round
    would treble the log for no later use. `code` is kept **whole**: the
    deferred cold grading pass grades exactly this text, and a truncated
    candidate is an ungradable one.
    """
    keep = ("round", "provider", "code", "verdict", "exit_code",
            "failure_kind", "failed_assertion", "failed_assertion_line")
    return [{name: cand.get(name) for name in keep} for cand in candidates]


def _resolve_tests(run, plan, keys, emit, user_tests):
    """Settle on an acceptance suite, and refuse to trust a vacuous one.

    User-supplied tests always win. A generated suite is audited; if it proves
    nothing it is regenerated exactly once, and if it still proves nothing the
    run is left UNVERIFIED rather than reporting a false APPROVED.
    """
    if user_tests and user_tests.strip():
        run.tests = user_tests if user_tests.endswith("\n") else user_tests + "\n"
        run.tests_status = TESTS_USER
        audit = harness.audit_tests(run.tests)
        run.tests_summary = audit.summary()
        # The user's own suite wins even if the audit dislikes it -- but say so.
        if not audit.ok:
            emit("system", "Using your suite despite the audit: %s" % audit.reason)
        emit("tests", _tests_entry(run, audit))
        return

    candidate = extract_tests(plan)
    if candidate:
        audit = harness.audit_tests(candidate)
        if audit.ok:
            run.tests, run.tests_status = candidate, TESTS_GENERATED
            run.tests_summary = audit.summary()
            emit("tests", _tests_entry(run, audit))
            return
        emit("system", "Rejected the Planner's acceptance suite: %s" % audit.reason)
    else:
        emit("system", "The Planner emitted no TESTS block; asking for one.")

    # One regeneration attempt, told exactly why the last one was rejected.
    request = "SPEC:\n%s" % clamp(run.spec, MAX_SPEC_CHARS)
    if candidate:
        request += ("\n\nYour previous suite was REJECTED because: %s\n\n"
                    "Rejected suite:\n```python\n%s```\n"
                    "Write a suite that asserts real computed values."
                    % (audit.reason, clamp(candidate, MAX_SPEC_CHARS)))
    try:
        raw, _ = call_role("test_writer", keys, PROMPTS["test_writer"], request, emit)
    except ProviderError as exc:
        run.tests_status = TESTS_MISSING
        run.tests_summary = "could not generate a suite: %s" % exc
        emit("tests", _tests_entry(run, None))
        return

    retry = harness.extract_code_block(raw, allow_tests=True)[0]
    audit2 = harness.audit_tests(retry) if retry else None
    if audit2 is not None and audit2.ok:
        run.tests, run.tests_status = retry, TESTS_REGENERATED
        run.tests_summary = audit2.summary()
        emit("tests", _tests_entry(run, audit2))
        return

    reason = (audit2.reason if audit2 is not None
              else "the regenerated output contained no code block")
    run.tests = ""
    run.tests_status = (TESTS_VACUOUS if audit2 is not None and audit2.vacuous
                        else TESTS_UNUSABLE)
    run.tests_summary = reason
    emit("tests", _tests_entry(run, audit2))
    emit("system",
         "No trustworthy acceptance suite after one regeneration (%s). Steps "
         "will be marked UNVERIFIED -- a passing exit status alone cannot show "
         "the output is correct, and reporting APPROVED here would be exactly "
         "the false confidence this stage exists to prevent." % reason)


RESOLVED_TESTS_FIELDS = ("tests", "tests_status", "tests_summary",
                         "tests_trusted")


def resolved_tests(run):
    """An already-resolved suite, packaged for ``run_workspace(tests=...)``.

    Source, status, audit summary and the trust flag. Whoever resolved the suite
    hands the *bytes* over, so a second consumer of the same plan gates on the
    same suite instead of generating its own from the same text.
    """
    return {"tests": run.tests, "tests_status": run.tests_status,
            "tests_summary": run.tests_summary,
            "tests_trusted": run.tests_trusted}


def _adopt_tests(run, payload, emit):
    """Take a resolved suite verbatim: no audit, no Test Writer call.

    The status is *carried*, never rewritten. It is deliberately not turned into
    `TESTS_USER`: that status means "the human supplied this and the audit was
    overridden", it bypasses the audit, and stamping it on an eval-injected suite
    would corrupt the suite-validity rate for every run that used one -- the
    direct quality measure of the weakest model's most important output.

    `tests_trusted` is a derived property of the status and the source, so the
    incoming flag is *checked*, not assigned. Assigning it would create a second,
    forgeable source of truth for whether a suite may gate APPROVED, which is
    precisely what the audit exists to prevent.
    """
    missing = [name for name in RESOLVED_TESTS_FIELDS if name not in payload]
    if missing:
        raise ValueError("resolved suite is missing %s; build it with "
                         "resolved_tests()" % ", ".join(missing))
    if payload["tests_status"] == TESTS_USER:
        raise ValueError("a user suite cannot be injected as a resolved one; "
                         "pass it as user_tests= so it is recorded as such")
    run.tests = payload["tests"] or ""
    run.tests_status = payload["tests_status"] or TESTS_MISSING
    run.tests_summary = payload["tests_summary"] or ""
    if bool(payload["tests_trusted"]) != run.tests_trusted:
        raise ValueError(
            "resolved suite claims tests_trusted=%r but status %r with %d "
            "chars of source derives %r"
            % (bool(payload["tests_trusted"]), run.tests_status,
               len(run.tests), run.tests_trusted))
    # Display only -- it names the interface for the feed entry and cannot
    # change any field. `audit_tests` runs the suite against a stub locally; it
    # is not a model call, so this costs nothing that `_resolve_tests` would.
    audit = harness.audit_tests(run.tests) if run.tests else None
    emit("tests", _tests_entry(run, audit))
    emit("system", "Acceptance suite supplied already resolved (`%s`); no suite "
                   "was generated for this run" % run.tests_status)


def _tests_entry(run, audit):
    """The feed entry showing what APPROVED is measured against."""
    lines = ["**Acceptance suite** - source: `%s`" % run.tests_status]
    if run.tests_summary:
        lines.append("Audit: %s" % run.tests_summary)
    if audit is not None and audit.names:
        lines.append("Interface under test: %s" % ", ".join(
            "`%s`" % name for name in audit.names))
    if run.tests:
        lines.append("\n```python\n%s```" % run.tests)
    else:
        lines.append("\n_No suite will be used; steps cannot be APPROVED._")
    return "\n".join(lines)


def _candidate_rank(candidate):
    """How far a candidate got, as an ordinal tuple. Higher is better.

    1. APPROVED beats everything.
    2. An assertion failure beats an import failure: the module loaded and ran
       under the suite, which is strictly further than not loading.
    3. Deeper failing assert line beats shallower.
    4. Earliest round wins ties.

    Two things this is not, both of which are honest limits rather than
    tuning knobs:

    * Depth (3) is a *heuristic*, not a score. It only means "got further" if
      the frozen suite's asserts are order-independent -- if assert 7 can only
      be reached by passing 1-6. Nothing in this repo establishes that yet; the
      permuted-assert-order flakiness battery that would is not written. Until
      it is, read this as a tie-break with a plausible story, not a measurement.
    * Everything that is not an assertion failure -- import errors, timeouts,
      runaway output, no code block, harness failure -- collapses to one rank,
      so among those the round-1 candidate is kept simply because it is first.
      That is coarse and deliberately conservative. It is not a claim that a
      round-1 timeout is better than a round-3 import error; it is a refusal to
      guess between them.
    """
    line = candidate.get("failed_assertion_line")
    return (
        1 if candidate.get("verdict") == harness.VERDICT_APPROVED else 0,
        1 if candidate.get("failure_kind") == harness.FAIL_ASSERTION else 0,
        line if isinstance(line, int) else -1,
        -candidate.get("round", 0),
    )


def _candidate_merit(candidate):
    """`_candidate_rank` without the round tie-break.

    Comparing full ranks would make "the final round was worse" fire on every
    exhausted step whose candidates all failed the same way, since the later
    round always loses the tie-break. Being later is not being worse.
    """
    return _candidate_rank(candidate)[:3]


def _retain_best(record, candidates):
    """Put the best candidate back on the record, and say which one it was.

    The loop used to overwrite the record every round, so the *last* attempt
    won by accident: a round-3 answer that dies on import replaced a round-1
    answer that failed one late assert. That is not "the best effort", it is
    "the most recent effort", and freezing it as the measurement convention
    would pre-register the bug.
    """
    if not candidates:
        return record
    best = max(candidates, key=_candidate_rank)
    last = candidates[-1]
    record.output = best["output"]
    record.verdict = best["verdict"]
    record.exit_code = best["exit_code"]
    record.stdout = best["stdout"]
    record.stderr = best["stderr"]
    record.code = best["code"]
    record.failure_kind = best["failure_kind"]
    record.failed_assertion = best["failed_assertion"]
    record.failed_assertion_line = best["failed_assertion_line"]
    record.sandbox_layers = list(best["sandbox_layers"])
    record.provider = best["provider"]
    record.retained_round = best["round"]
    record.final_round_worse = _candidate_merit(last) < _candidate_merit(best)
    return record


def _verify_step(record, task, keys, emit, run):
    """Execute the step's code; on failure escalate deterministically, up to
    MAX_REVISION_ROUNDS times.

    Anti-cheating: the suite passed to the harness is always ``run.tests``, the
    stored source, re-read every round. Nothing the Executor emits can weaken
    it -- a test file in its output is discarded by the extractor.

    What to try next is decided by ESCALATION, in Python. No model is consulted
    about it: the inputs are a verdict, a failure kind and a round number, and
    picking a branch from those needs no judgement.

    Every attempt is kept (`record.candidates`) and the *best* one is returned,
    ranked by `_candidate_rank`. Attempts are stored, never hidden-graded here.
    """
    tests = run.tests if run.tests_trusted else None
    record.tested = bool(tests)
    candidates = record.candidates

    for attempt in range(MAX_REVISION_ROUNDS + 1):
        verdict, result = harness.verify_output(record.output, tests=tests)

        # Without a trustworthy suite, exit status alone cannot establish
        # correctness, so APPROVED is not available. A crash is still a real
        # defect, so REVISE still applies and the loop still runs.
        if verdict == harness.VERDICT_APPROVED and not record.tested:
            verdict = harness.VERDICT_UNVERIFIED
            record.note = ("ran cleanly, but there is no trustworthy "
                           "acceptance suite (%s)" % run.tests_status)

        record.verdict = verdict
        record.exit_code = result.exit_code
        record.stdout = result.stdout
        record.stderr = result.stderr
        record.code = result.source
        record.failure_kind = result.failure_kind
        record.failed_assertion = result.failed_assertion
        record.failed_assertion_line = result.failed_assertion_line
        record.sandbox_layers = list(result.sandbox_layers)
        record.retained_round = attempt + 1
        candidates.append({
            "round": attempt + 1,
            "provider": record.provider,
            "output": record.output,
            "code": result.source,
            "verdict": verdict,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "failure_kind": result.failure_kind,
            "failed_assertion": result.failed_assertion,
            "failed_assertion_line": result.failed_assertion_line,
            "sandbox_layers": list(result.sandbox_layers),
        })

        report = harness.format_report(verdict, result)
        if record.note:
            report += "\n%s" % record.note
        emit("harness", report)

        if verdict == harness.VERDICT_APPROVED:
            # Retention is a no-op here in effect: APPROVED outranks every
            # other candidate, so this returns the round that just passed.
            return _retain_best(record, candidates)
        if verdict == harness.VERDICT_UNVERIFIED:
            # Either the harness could not run, or there is no suite to gate
            # on. Revising the code cannot change either. Deliberately *not*
            # retained-best: UNVERIFIED here means the harness itself
            # malfunctioned, and retaining an earlier REVISE would report a
            # code defect while hiding a harness bug.
            emit("system", "Step %d left UNVERIFIED: %s"
                 % (record.number, record.note or result.reason))
            return record
        if attempt == MAX_REVISION_ROUNDS:
            record.escalation = "exhausted"
            _retain_best(record, candidates)
            record.note = _exhausted_note(record)
            emit("system", "Step %d: %s" % (record.number, record.note))
            if record.final_round_worse:
                emit("system", "Step %d: round %d was worse than round %d; the "
                               "earlier candidate was kept"
                     % (record.number, candidates[-1]["round"],
                        record.retained_round))
            return record

        rung = ESCALATION[attempt]
        fixes = harness.format_fixes(verdict, result)
        prompt, prefer = _revision_request(rung, fixes, task, record, keys,
                                           attempt + 1)

        emit("system", "Step %d failed; escalating to `%s` (round %d of %d) - %s"
             % (record.number, rung, attempt + 1, MAX_REVISION_ROUNDS,
                ESCALATION_WHY[rung]))
        try:
            record.output, provider = call_role(
                "executor", keys, PROMPTS["executor"], prompt, emit, prefer=prefer)
        except ProviderError as exc:
            emit("system", "Revision failed: %s" % exc)
            return _retain_best(record, candidates)
        record.provider = provider
        record.rounds = attempt + 1
        record.escalation = rung
        emit("executor", record.output)

    return _retain_best(record, candidates)


def _revision_request(rung, fixes, task, record, keys, failures):
    """The prompt and provider preference for one rung of the ladder.

    Returns ``(prompt, prefer)``. ``failures`` is how many attempts the harness
    has already executed and rejected. The Executor's own failed code is never
    sent back on any rung -- it already has it, and re-quoting it wastes budget
    and anchors the model to the broken shape.
    """
    spec = clamp(task, MAX_SPEC_CHARS)

    if rung == "fresh":
        # Deliberately withholds the traceback. Feedback-driven repair has
        # already failed more than once, which is evidence the approach is
        # wrong rather than the details -- more detail about the same wrong
        # approach is what keeps a model circling it. The failed assertion
        # stays because it is a fact about the spec, not about the attempt.
        lines = [
            "%d previous attempt%s at this step %s executed against a hidden "
            "acceptance suite and every one of them failed."
            % (failures, "" if failures == 1 else "s",
               "was" if failures == 1 else "were"),
        ]
        if record.failed_assertion:
            lines.append("The last one failed this check: %s"
                         % clamp(record.failed_assertion, 200))
        lines += [
            "",
            "Ignore any approach you have taken so far and start over. Write a "
            "fresh, independent implementation, choosing a different strategy "
            "-- a plainer or more direct one is usually correct here. Do not "
            "patch the earlier attempt.",
            "",
            "Original task:",
            spec,
        ]
        return "\n".join(lines), alternate_provider("executor", keys)

    prompt = "%s\n\nOriginal task:\n%s" % (fixes, spec)
    if rung == "alternate":
        # Same information, different model. The traceback was clear enough
        # that a second reader may simply get it right; if no other key is
        # configured, alternate_provider returns None and this degrades to
        # another repair round, which the note records honestly.
        return prompt, alternate_provider("executor", keys)
    return prompt, None


def _exhausted_note(record):
    """Why a step is being abandoned, in terms of what was actually tried."""
    tried = ", ".join(ESCALATION[:MAX_REVISION_ROUNDS])
    note = ("still failing after %d escalation round(s) (%s); the harness ran "
            "the code every round and it never passed the suite"
            % (MAX_REVISION_ROUNDS, tried))
    if record.failed_assertion:
        note += ". Last failing check: %s" % clamp(record.failed_assertion, 200)
    return note


def _assemble_deliverable(run):
    blocks = ["# %s" % (run.spec.splitlines()[0] if run.spec else "Deliverable"),
              "",
              "Run `%s` - %s" % (run.run_id, PIPELINE_LABELS[run.mode]),
              "Verified against the acceptance suite: %d/%d step(s)"
              % (run.verified_count, len(run.steps)),
              ""]
    if run.tests:
        blocks.append("## Acceptance suite (`%s`)\n\n%s\n\n```python\n%s```\n"
                      % (run.tests_status, run.tests_summary, run.tests))
    else:
        blocks.append("> No trustworthy acceptance suite was available (%s), so "
                      "no step could be APPROVED.\n" % run.tests_status)

    for record in run.steps:
        status = record.verdict or harness.VERDICT_UNVERIFIED
        if record.exit_code is not None:
            status += " (exit %s)" % record.exit_code
        blocks.append("---\n")
        blocks.append("## Step %d: %s\n\n_%s_\n" % (record.number, record.step, status))
        if record.failed_assertion:
            blocks.append("Failing assertion: `%s`\n" % record.failed_assertion)
        if record.note:
            blocks.append("%s\n" % record.note)
        if record.code:
            blocks.append("```python\n%s```\n" % record.code)
        else:
            blocks.append(record.output + "\n")
        if record.stdout.strip():
            blocks.append("Output when run:\n```\n%s\n```\n" % record.stdout.rstrip())
        if record.stderr.strip() and not record.verified:
            blocks.append("Still failing with:\n```\n%s\n```\n" % record.stderr.rstrip())
    return "\n".join(blocks)

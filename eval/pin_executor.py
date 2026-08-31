"""Pin the Executor on this project's own 36 tasks, not on a leaderboard.

    python3 eval/pin_executor.py

One A' draw per task, shared across three candidates:

    36 tasks x 1 Planner call   =  36 Gemini calls
    36 tasks x 3 candidates     = 108 Groq calls

The Planner draw is shared on purpose. Three candidates answering three
different specs would put plan-to-plan variance inside the contrast between
candidates, which is the one thing this run exists to measure. It is the same
reason `run_eval.make_plan` is called once per task and handed to every arm.

The Groq figure is the one the brief named; the Gemini leg is stated here because
it is real spend on the scarcer key and nobody should discover it at task 1.

What it records per call, and why each one had to be asked for:

  * ``reasoning_format="hidden"``, explicitly, on every Groq call, and registered
    through the role's own `params` so it lands in the resolved table and in
    CALL_LOG beside temperature and top_p. Measured 2026-08-30: gpt-oss-20b with
    it unset returned zero fenced blocks while 568 characters went to a separate
    `reasoning` field. Unset is not a neutral default, it is a broken one.
  * the full ``usage`` block including ``reasoning_tokens``. `hidden` suppresses
    reporting, not computation; those tokens are billed while being invisible in
    the reply.
  * the model ID the API *returned*, not only the one requested. Pinning a model
    is a claim about what answered.

Resumable. Every unit of work is appended to a JSONL ledger and fsynced as it
completes, so a run that dies at call 90 resumes at 90. That ledger is not a
response cache: the resume key is ``(task_id, candidate)``, a completed-work
marker, and this probe draws exactly one sample per key. Nothing here can hand an
old sample back as a freshly drawn one -- see the standing prohibition in
`agents_core._attempt_provider`. Do not add a key that includes the prompt.

    python3 eval/pin_executor.py --replay-plans <old ledger> --out <new dir>

re-draws against the specs an earlier ledger already holds: zero Planner calls,
no Gemini key asked for, the suite and its digest carried over and re-verified.
It is not a cheaper way to run the probe -- it is the only way to re-measure the
Executor on the *same* specs after a harness fix, which a fresh Planner draw
would make impossible. Tasks with no stored spec are skipped and named, never
planned.

Keys are read with `getpass.getpass`. Never argv, never echoed to the screen,
never an environment dump, never a log.
"""

import argparse
import getpass
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents_core  # noqa: E402
import gen_tasks  # noqa: E402
import run_eval  # noqa: E402

# The three the brief names. Not a recommendation and not a shortlist this file
# is allowed to edit: which slug ends up registered as the Executor is the user's
# decision, and this runner only measures.
CANDIDATES = ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b")

# Sent on every Groq call, through the role's `params` so it is recorded rather
# than smuggled. See the module docstring for the measurement that forced it.
EXECUTOR_PARAMS = {"reasoning_format": "hidden"}

DEFAULT_ROOT = os.path.join(run_eval.RESULTS_DIR, "pin-executor")
LEDGER_NAME = "ledger.jsonl"

KIND_PLAN = "plan"
KIND_DRAW = "draw"

# A failing draw keeps its code and the child's stderr tail. Bounded, because the
# ledger is append-only and read by hand: enough to see a traceback and the
# signature that raised it, not enough for one pathological reply to dominate the
# file. `clamp` elides the middle visibly rather than cutting the end off.
MAX_KEPT_CODE_CHARS = 4000
STDERR_TAIL_CHARS = 900


# ------------------------------------------------------------------ the ledger

def ledger_path(root):
    return os.path.join(root, LEDGER_NAME)


def unit_key(record):
    """The resume key. ``(kind, task_id, candidate)`` and nothing prompt-shaped.

    Deliberately not a content hash of the request. A key over
    (prompt, model, params, provider) would make a repeated *sample* a cache hit,
    which is the failure the standing prohibition is about. A key over the unit of
    work cannot: this probe defines exactly one unit per (task, candidate), so a
    hit means "already drawn", never "draw it again cheaply".
    """
    return (record.get("kind"), record.get("task_id"),
            record.get("candidate") or "")


def load_ledger(path):
    """``(units, malformed)`` -- every complete record, indexed by resume key.

    A truncated final line is expected rather than exceptional: the process may
    have died mid-append. It is counted and dropped, not repaired, because a
    half-written record is a unit that did not complete and re-running it is
    correct.
    """
    units, malformed = {}, 0
    if not os.path.exists(path):
        return units, malformed
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(record, dict) or not record.get("kind"):
                malformed += 1
                continue
            units[unit_key(record)] = record
    return units, malformed


def append(path, record):
    """Append one record and flush it to disk before returning.

    The fsync is the whole point of the ledger. Without it a kill -9 loses
    whatever the OS had buffered, and "resumable" becomes "resumable unless it
    actually crashed".
    """
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


# ------------------------------------------------ keys, configuration, preflight

class CaptureDetail(object):
    """Keeps every reply's full detail while `call_model` keeps its shape.

    `call_role` gives pacing, retry and a CALL_LOG record, and none of that
    should be reimplemented here -- 108 calls on a free tier will meet 429s, and
    the retry layer is the only thing between a 429 and a lost unit. But
    `_attempt_provider` calls `call_model`, which returns a string, so the usage
    block and the returned model ID are dropped before this file could see them.

    So: replace `call_model` with a wrapper over `call_model_detailed`, the same
    way `eval/prove_shared_gate.py` replaces it to count roles. `call_model`
    itself is left alone; nothing about the call path changes except that the
    detail is kept.

    One entry per *successful* completion. A failed attempt raises before it
    appends, so `replies[-1]` after a successful `call_role` is that call's reply
    and not a retry's ghost.
    """

    def __init__(self):
        self.replies = []
        self._real = None

    def __enter__(self):
        self._real = agents_core.call_model
        agents_core.call_model = self._call
        return self

    def __exit__(self, *exc_info):
        agents_core.call_model = self._real
        return False

    def _call(self, provider, api_key, system, user, role=None):
        reply = agents_core.call_model_detailed(provider, api_key, system, user,
                                                role=role)
        self.replies.append(reply)
        return reply["text"]

    def last(self):
        return self.replies[-1] if self.replies else {}


def configure(candidate):
    """Point the Executor role at ``candidate``, with the params it must be sent.

    Through `apply_model_config` rather than by assigning `ROLE_MODEL`, so the
    resolution, the CALL_LOG record and the wire are decided by the one function
    that decides them for a real run. `reasoning_format` goes in the role's
    `params`, which is the declared home of a per-role sampling parameter, and
    from there `sampling_for` puts it on the wire *and* in the record.
    """
    return agents_core.apply_model_config(
        {"roles": {"executor": {"model": candidate,
                                "params": dict(EXECUTOR_PARAMS)}}},
        source="eval/pin_executor.py")


def ask_keys(providers, reader=None):
    """provider -> key, read from the terminal. Never argv, never echoed.

    argv is in the process table, in the shell history and in any crash report
    that dumps the command line. `input()` is in none of those -- but it echoes
    the key to the screen and leaves it in the scrollback of a terminal that may
    be shared, recorded or screen-shared, which is how a key was exposed here
    once. `getpass.getpass` reads the same line without echoing it.

    ``reader`` stays injectable so the offline checks can drive this without a
    tty; only the default changed.
    """
    reader = getpass.getpass if reader is None else reader
    keys = {}
    for provider in providers:
        keys[provider] = (reader("Paste the %s API key (%s): "
                                 % (provider,
                                    agents_core.key_env(provider))) or "").strip()
    return keys


def preflight(keys, candidates=CANDIDATES, call=None, include_planner=True):
    """One cheap call per pair before any generation. Returns a list of records.

    Every candidate slug *and* the Planner's pair, because a probe that validated
    only the three Groq IDs would still die 36 Gemini calls in. This project has
    now lost two pinned models to retirement, and the second one -- Groq's, the
    Executor in all four arms -- was discovered by a grid rather than by a
    preflight.

    A missing key is a failure, not a skip. A 404 here costs one second; the same
    404 at unit 1 of 144 costs the run.

    `include_planner=False` is for `--replay-plans`, which reuses stored specs and
    therefore has no Planner pair to validate and no Gemini key to validate it
    with. Validating one anyway would demand a key the mode does not use, and a
    mode that asks for a key it will not spend teaches the habit of pasting keys
    into runs that do not need them.
    """
    records = []
    planner_provider = agents_core.ROLE_PROVIDER["planner"]
    pairs = []
    if include_planner:
        pairs.append((planner_provider, agents_core.model_for("planner"),
                      ["planner"]))
    pairs += [("groq", slug, ["executor"]) for slug in candidates]
    for provider, model, roles in pairs:
        key = (keys or {}).get(provider)
        if not key:
            records.append({"roles": roles, "provider": provider,
                            "model": model, "ok": False, "status": None,
                            "detail": "no API key for provider %r" % provider})
            continue
        # With the Executor's own parameters, `reasoning_format` included. That
        # extension is the reason this probe exists in its current form, and a
        # preflight that validated the pair without it would have declared a
        # candidate reachable while the shape every real draw sends was untested.
        ok, status, detail = agents_core.preflight_pair(
            provider, model, key, call=call,
            params=agents_core.sampling_for(roles[0]))
        records.append({"roles": roles, "provider": provider, "model": model,
                        "ok": ok, "status": status, "detail": detail})
    return records


def preflight_failures(records):
    """The refusal lines. Empty exactly when every pair answered."""
    return agents_core.preflight_failures(records)


# --------------------------------------------------------------------- the work

def locked_tasks(path=None):
    """The 36 frozen tasks, or a refusal. Same posture as `--verify-lock`.

    Measuring a candidate against suites the lock does not describe would produce
    a number that looks like a pass rate and is not one. `eval/rank_battery.py`
    refuses for the same reason and with the same message shape.
    """
    tasks = list(gen_tasks.generate())
    failures, reason = gen_tasks.verify_lock_or_reason(tasks=tasks, path=path)
    if reason:
        raise SystemExit("REFUSING TO MEASURE: %s" % reason)
    if failures:
        raise SystemExit("REFUSING TO MEASURE: the lock does not describe these "
                         "suites:\n  " + "\n  ".join(failures[:6]))
    return tasks


def is_done(record):
    """Whether a ledger record means "do not run this unit again".

    A failed unit stays in the file as an audit trail but does not count as done,
    so a resume retries it. That is the right default for the failures a 108-call
    free-tier run actually meets -- a 429 that outlasted the retry schedule, a
    dropped connection. A permanent failure will simply fail again and be visible
    in the summary rather than silently absent from it.
    """
    return bool((record or {}).get("ok"))


def plan_unit(task, keys):
    """One shared A' draw for one task. Returns the ledger record.

    `run_eval.make_plan` is called rather than reimplemented, so the spec these
    candidates answer is byte-for-byte the spec the graded arms answer, and the
    suite audit and Test Writer fallback behave as they do in a real sweep. It can
    make a second call (the Test Writer) when the audit rejects the plan's own
    TESTS block, which is why every reply is recorded and not just the last.
    """
    record = {"kind": KIND_PLAN, "task_id": task.task_id, "candidate": "",
              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "ok": False}
    start = len(agents_core.CALL_LOG)
    try:
        with CaptureDetail() as detail:
            plan = run_eval.make_plan(task, keys)
    except agents_core.DailyQuotaExhausted:
        # Deliberately not recorded as this task's failure and not swallowed. The
        # first probe recorded 17 of these as ordinary unit failures and kept
        # going, which is how a 20-request ceiling cost 104 requests. The ledger
        # is already fsynced up to here, so aborting loses nothing.
        raise
    except Exception as exc:                                    # noqa: BLE001
        record["error"] = agents_core._redact(exc, keys)
        record["exc_class"] = type(exc).__name__
        record["status"] = getattr(exc, "status", None)
        record["calls"] = _calls_since(start)
        return record
    record.update({
        "ok": True,
        "spec": plan["spec"],
        "steps": plan["steps"],
        "tests": plan["tests"],
        "tests_status": plan["tests_status"],
        "tests_trusted": bool(plan["tests_trusted"]),
        "tests_sha256": plan["tests_sha256"],
        "planner_provider": plan["planner_provider"],
        # Report-only, and the reason this field exists: the artifact that ended
        # the first probe was diagnosed by hand-reading spec text afterwards.
        "spec_runtime_syntax": plan["spec_runtime_syntax"],
        "calls": _calls_since(start),
        "replies": [_reply_summary(reply) for reply in detail.replies],
    })
    return record


# ------------------------------------------------------- replaying stored plans

# The roles a replay must not call. `test_writer` is in here beside `planner`
# because `make_plan` calls it when the audit rejects the plan's own TESTS block:
# a replay that avoided the Planner and still resolved a suite would have drawn
# against a suite the first probe never used, which is the whole thing being
# avoided.
REPLAY_ROLES = ("planner", "test_writer")


def planner_calls_since(start):
    """Any Planner-side call in `CALL_LOG[start:]`. Empty is the invariant."""
    return [entry for entry in agents_core.CALL_LOG[start:]
            if entry.get("role") in REPLAY_ROLES]


def rehash_suite(text, claimed):
    """Rehash ``text`` the way a digest of ``claimed``'s width was written.

    Returns ``None`` when no writer in this repo produces that width, which the
    caller turns into a refusal.

    Two writers, two widths, both deliberate. `run_eval._suite_hash` records the
    visible gate suite's *identity* as a 16-hex prefix; `agents_core.sha256_of`
    records a full 64-hex *integrity* digest, as `eval/gen_tasks.py` does for the
    task lock -- and that comment says in as many words that the two are different
    lengths so nobody compares them by accident. Which is exactly what this guard
    did: it rehashed with `sha256_of` a field every plan record here was written
    with by `_suite_hash`, so all 19 stored specs failed on a 16-vs-64 length
    difference while their bytes were intact.

    Selecting the function by the claimed width is not a weaker check than
    fixing on one. A 16-hex digest still has to be the exact prefix
    `_suite_hash` would produce for these bytes and for no others, so tampering
    with the suite still fails. What is deliberately *not* done is accepting a
    prefix match: that would turn an integrity guard into a length-agnostic
    "starts with" test, in which a one-character digest validates any suite at
    all.

    Width 0 -- no digest recorded -- rehashes as a full digest, which reproduces
    the old guard exactly rather than tightening it under cover of a fix: both
    writers hash empty text to ``""``, so an empty suite with no digest still
    validates, and a non-empty suite with no digest still refuses because neither
    writer returns ``""`` for text. The 17 quota-killed plan records in the first
    probe's ledger are that shape, and `replayable_plans` drops them before this
    is ever asked about them.
    """
    if not claimed:
        return agents_core.sha256_of(text)
    if len(claimed) == 16:
        return run_eval._suite_hash(text)
    if len(claimed) == 64:
        return agents_core.sha256_of(text)
    return None


def replayed_plan_unit(record, source_path):
    """A plan reused verbatim from an earlier ledger. Zero calls, by construction.

    Why this mode exists: the first probe's 57 draws were answered against specs
    that cost 19 Gemini calls out of a 20-per-day ceiling. Re-drawing them under
    the fixed harness must not re-spend that, and -- more importantly -- must not
    re-generate them. A fresh Planner call would produce a different spec and a
    different suite, and the comparison "same spec, better harness" would silently
    become "different spec, different harness", which measures nothing.

    So the spec, the suite, its digest, its status and its trust flag are copied
    byte for byte and the digest is re-derived from the copied text before any draw
    is made -- by the width the record claims, see `rehash_suite`. A ledger whose
    suite and digest disagree cannot be replayed at all: which of the two gated the
    first probe is exactly the unknown that would make the second one unreadable,
    so it refuses rather than picking one.

    The two union-bearing specs are copied like the rest. They are the reason for
    the sprint: replaying them under the prologue is the measurement.
    """
    tests = record.get("tests") or ""
    claimed = record.get("tests_sha256") or ""
    rehashed = rehash_suite(tests, claimed)
    if rehashed is None:
        raise SystemExit(
            "REFUSING TO REPLAY %s: the source ledger's suite does not hash to "
            "its own tests_sha256\n  recorded %s\n  recorded width %d hex "
            "character(s), which is neither the 16 run_eval._suite_hash writes "
            "nor the 64 agents_core.sha256_of writes, so there is no function "
            "here that could have written it\n  source %s"
            % (record.get("task_id"), claimed, len(claimed), source_path))
    if rehashed != claimed:
        raise SystemExit(
            "REFUSING TO REPLAY %s: the source ledger's suite does not hash to "
            "its own tests_sha256\n  recorded %s\n  rehashed %s\n  source %s"
            % (record.get("task_id"), claimed or "(none)", rehashed,
               source_path))
    return {
        "kind": KIND_PLAN,
        "task_id": record.get("task_id"),
        "candidate": "",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": True,
        "spec": record.get("spec"),
        "steps": record.get("steps"),
        "tests": tests,
        "tests_status": record.get("tests_status"),
        "tests_trusted": bool(record.get("tests_trusted")),
        "tests_sha256": claimed,
        "planner_provider": record.get("planner_provider"),
        # Recomputed rather than copied: the scanner postdates the source ledger,
        # so copying the field would leave every replayed record unscanned. The
        # scan is a pure function of the spec text that was copied, so this cannot
        # disagree with what the source would have recorded had it had one.
        "spec_runtime_syntax": agents_core.scan_runtime_syntax(
            record.get("spec") or ""),
        # Provenance. Without these three the new ledger would look like a run
        # that planned 19 tasks itself, and the reused suite would be unauditable.
        "replayed_from": source_path,
        "replayed_at": record.get("at"),
        "replayed_tests_sha256": claimed,
        # Empty on purpose, and asserted elsewhere: a replayed plan makes no call,
        # so there is no usage to account for and no reply to summarise.
        "calls": [],
        "replies": [],
    }


def replayable_plans(source_units):
    """`task_id -> plan record` for the plans an earlier ledger can actually lend.

    A plan record that failed, or that completed without a spec, is not a plan.
    The first probe holds 36 plan records and only 19 specs -- the other 17 are the
    per-day quota being hit -- so this returning fewer tasks than the lock is the
    expected case, not a fault, and the caller skips those tasks rather than
    planning them.
    """
    plans = {}
    for key, record in (source_units or {}).items():
        if key[0] != KIND_PLAN or not is_done(record):
            continue
        if not (record.get("spec") or "").strip():
            continue
        plans[record.get("task_id")] = record
    return plans


# ------------------------------------------------------------------ the summary

# Usage field names differ between providers and grow without notice, so they are
# summed by whatever integer keys actually appeared rather than by a fixed list.
# `reasoning_tokens` arrives nested under `completion_tokens_details` on some
# responses and flat on others; both are flattened here so neither goes missing.
def _usage_items(usage, prefix=""):
    for name, value in sorted((usage or {}).items()):
        if isinstance(value, dict):
            for pair in _usage_items(value, prefix + name + "."):
                yield pair
        elif isinstance(value, int) and not isinstance(value, bool):
            yield prefix + name, value


def summarise(units, candidates=CANDIDATES):
    """Everything the report prints, derived once so prose and JSON agree."""
    plans = [record for key, record in units.items()
             if key[0] == KIND_PLAN and is_done(record)]
    tests_status = {}
    for record in plans:
        name = record.get("tests_status") or "?"
        tests_status[name] = tests_status.get(name, 0) + 1
    summary = {
        "plans": len(plans),
        "plans_tests_status": tests_status,
        "plans_trusted": len([r for r in plans if r.get("tests_trusted")]),
        "candidates": {},
    }
    for candidate in candidates:
        drawn = [record for key, record in units.items()
                 if key[0] == KIND_DRAW and key[2] == candidate]
        ok = [record for record in drawn if is_done(record)]
        usage, finishes, returned = {}, {}, {}
        for record in ok:
            for reply in record.get("replies") or []:
                for name, value in _usage_items(reply.get("usage")):
                    usage[name] = usage.get(name, 0) + value
                finish = reply.get("finish_reason") or "?"
                finishes[finish] = finishes.get(finish, 0) + 1
                slug = reply.get("model_returned") or "?"
                returned[slug] = returned.get(slug, 0) + 1
        summary["candidates"][candidate] = {
            "attempted": len(drawn),
            "ok": len(ok),
            "failed": len(drawn) - len(ok),
            "passed": len([r for r in ok if r.get("passed")]),
            "no_code": len([r for r in ok if not r.get("extracted_code")]),
            "gate_approved": len([r for r in ok if r.get("gate_approved")]),
            "reasoning_leaked": len([r for r in ok
                                     for reply in r.get("replies") or []
                                     if reply.get("reasoning_chars")]),
            "models_returned": returned,
            "finish_reasons": finishes,
            "usage": usage,
        }
    # Post hoc, over the records as they were already written -- no field was added
    # to a draw to make this computable, and it therefore scores ledgers that
    # predate it. The rule lives in `run_eval` because the same question is asked
    # of A'@3's stored candidates there; two copies of it would drift.
    #
    # Here a task's draws are one per candidate, so "every draw" means every
    # candidate agreed with the hidden suite and the visible gate rejected all of
    # them. That is a statement about the suite, not about the three models.
    summary["wrong_suite"] = run_eval.wrong_suite_report(
        [record for key, record in units.items()
         if key[0] == KIND_DRAW and key[2] in candidates])
    return summary


# ------------------------------------------------------------- record plumbing

# The CALL_LOG fields worth keeping per unit. An allow-list for the same reason
# `run_eval._call_log_slice` uses one: the record is a working object that grows
# and this file is a published artifact. `error` is out -- it is provider text,
# and the redacted copy already sits on the unit's own `error`.
_CALL_KEEP = ("role", "requested", "used", "model", "at", "measurement_mode",
              "ok", "status", "exc_class", "attempts", "retried",
              "seconds_paced", "seconds_backoff", "temperature", "top_p",
              "reasoning_format")


def _calls_since(start):
    return [dict((name, entry.get(name)) for name in _CALL_KEEP
                 if name in entry)
            for entry in agents_core.CALL_LOG[start:]]


def _reply_summary(reply):
    """One completion's accounting, as the API reported it.

    ``usage`` is copied whole rather than field-picked: what a provider puts in it
    changes without notice, and `reasoning_tokens` is exactly the field that would
    have been missed by a hand-written list. ``model_returned`` is kept next to
    ``model_requested`` so the two can be compared instead of assumed equal.
    """
    return {
        "model_requested": reply.get("model_requested"),
        "model_returned": reply.get("model_returned"),
        "usage": reply.get("usage") or {},
        "finish_reason": reply.get("finish_reason"),
        "params": reply.get("params") or {},
        "extra_body": reply.get("extra_body") or {},
        "reasoning_chars": reply.get("reasoning_chars", 0),
        "chars": len(reply.get("text") or ""),
        "fences": (reply.get("text") or "").count("```"),
    }


def draw_unit(task, plan_record, candidate, keys):
    """One Executor call for one candidate on one task, graded. The ledger record.

    `run_eval._draw` and `run_eval.grade` rather than local copies: the prompt the
    candidate sees and the suite it is graded against have to be the pipeline's,
    or this measures a probe rather than the Executor.

    The visible gate is recorded alongside the grade because it costs no API call
    -- it is the harness running the plan's own suite locally -- and it answers
    "would A'@3 have been able to select this draw" for free. It never decides
    anything here.
    """
    configure(candidate)
    record = {"kind": KIND_DRAW, "task_id": task.task_id, "candidate": candidate,
              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "ok": False}
    start = len(agents_core.CALL_LOG)
    try:
        with CaptureDetail() as detail:
            drawn = run_eval._draw(plan_record["spec"], keys)
    except agents_core.DailyQuotaExhausted:
        raise
    except Exception as exc:                                    # noqa: BLE001
        record["error"] = agents_core._redact(exc, keys)
        record["exc_class"] = type(exc).__name__
        record["status"] = getattr(exc, "status", None)
        record["calls"] = _calls_since(start)
        # No code to keep: the call raised, so nothing was ever returned to
        # extract from. The error, its class and its status are the diagnosis
        # here, and they are already above.
        return record
    code = drawn["code"] or ""
    plan = {"tests": plan_record["tests"],
            "tests_trusted": plan_record["tests_trusted"],
            "tests_sha256": plan_record["tests_sha256"]}
    graded = run_eval.grade(code, task)
    verdict, approved = run_eval._gate(code, plan)
    record.update({
        "ok": True,
        "provider": drawn["provider"],
        "code_sha256": agents_core.sha256_of(code),
        "code_chars": len(code),
        "extracted_code": bool(code.strip()),
        "raw_chars": drawn["raw_len"],
        "passed": bool(graded["passed"]),
        "grade_failure": graded.get("grade_failure"),
        "grade_reason": graded.get("grade_reason", ""),
        "gate_verdict": verdict,
        "gate_approved": bool(approved),
        "tests_status": plan_record["tests_status"],
        "tests_trusted": plan_record["tests_trusted"],
        "calls": _calls_since(start),
        "replies": [_reply_summary(reply) for reply in detail.replies],
    })
    if not graded["passed"]:
        # The code and the child's stderr, for failures only. `code_sha256` alone
        # made the first probe's root cause unrecoverable: three candidates failed
        # one task at *import* and the reason had to be inferred from spec text
        # and a natural experiment, because the ledger had kept a hash of the
        # thing that failed. A probe whose purpose is choosing a model has to be
        # able to answer "why did this draw fail" from its own ledger. Only on
        # failures, so a green run does not carry 57 programs it will never be
        # asked about.
        stderr, _capped = graded.get("grade_stderr") or ("", False)
        record["failed_code"] = agents_core.clamp(code, MAX_KEPT_CODE_CHARS)
        record["failed_stderr"] = stderr[-STDERR_TAIL_CHARS:]
        record["failed_assertion"] = graded.get("grade_assertion", "")
    return record


# ------------------------------------------------------------------- the report

def print_report(summary, total_units):
    """The table, then the diagnostics that decide whether to believe it."""
    print("\nplans drawn: %d/%d  (%d with a trustworthy suite)"
          % (summary["plans"], total_units[KIND_PLAN], summary["plans_trusted"]))
    print("  tests_status: %s"
          % ("  ".join("%s=%d" % pair
                       for pair in sorted(summary["plans_tests_status"].items()))
             or "none yet"))
    print("\n%-24s %5s %5s %5s %6s %5s %5s"
          % ("candidate", "ok", "pass", "gate", "nocode", "fail", "leak"))
    for candidate, row in sorted(summary["candidates"].items()):
        print("%-24s %5d %5d %5d %6d %5d %5d"
              % (candidate[:24], row["ok"], row["passed"], row["gate_approved"],
                 row["no_code"], row["failed"], row["reasoning_leaked"]))
    for candidate, row in sorted(summary["candidates"].items()):
        print("\n%s" % candidate)
        print("  pass rate      %s"
              % ("%d/%d = %.1f%%" % (row["passed"], row["ok"],
                                     100.0 * row["passed"] / row["ok"])
                 if row["ok"] else "no completed draws"))
        print("  answered by    %s"
              % ("  ".join("%s x%d" % pair
                           for pair in sorted(row["models_returned"].items()))
                 or "-"))
        print("  finish_reason  %s"
              % ("  ".join("%s x%d" % pair
                           for pair in sorted(row["finish_reasons"].items()))
                 or "-"))
        print("  usage          %s"
              % ("  ".join("%s=%d" % pair
                           for pair in sorted(row["usage"].items())) or "-"))
    _print_notes(summary, total_units)


def _print_notes(summary, total_units):
    """The ways this table can be misread, said out loud rather than left in JSON."""
    wrong = summary.get("wrong_suite") or {}
    if wrong.get("graded_draws"):
        print("\nwrong-suite: %d/%d task(s) = %.1f%%  (trusted suite, every "
              "graded draw passes the\n  hidden suite, gate approved none)%s"
              % (wrong.get("count", 0), wrong.get("tasks_with_a_graded_draw", 0),
                 wrong.get("rate_of_graded_tasks", 0.0),
                 "" if not wrong.get("tasks")
                 else "\n  " + ", ".join(wrong["tasks"])))
    if wrong.get("count"):
        print("  Three candidates agreeing with the hidden suite while the "
              "visible suite rejects all\n  three is evidence about the SUITE. "
              "The `gate` column undercounts by that much, and it\n  is not a "
              "candidate's failure. Recorded and named, not repaired: the suite "
              "is what the\n  gate's own error rate is measured from, so "
              "loosening it here would erase the "
              "measurement.")
    leaked = sorted(name for name, row in summary["candidates"].items()
                    if row["reasoning_leaked"])
    if leaked:
        print("\nWARNING: reasoning text still arrived in a separate field for "
              "%s,\n  with reasoning_format=hidden set. That is the 2026-08-30 "
              "failure not being fixed\n  by the thing that fixed it. Treat "
              "those rows as unmeasured." % ", ".join(leaked))
    aliased = sorted(
        (name, sorted(row["models_returned"]))
        for name, row in summary["candidates"].items()
        if [slug for slug in row["models_returned"] if slug != name])
    if aliased:
        print("\nNOTE: the API answered under a slug other than the one "
              "requested:")
        for name, slugs in aliased:
            print("  requested %s -> answered %s" % (name, ", ".join(slugs)))
        print("  A pin names what answers. Register the returned slug, not the "
              "requested one.")
    if summary["plans"] and summary["plans_trusted"] < summary["plans"]:
        print("\nNOTE: %d of %d plans have no trustworthy suite, so `gate` is "
              "unavailable for them by\n  construction and is not a candidate's "
              "failure. `pass` is unaffected: it is graded\n  against the hidden "
              "suite, which every task has."
              % (summary["plans"] - summary["plans_trusted"], summary["plans"]))
    done = summary["plans"] + sum(row["ok"]
                                  for row in summary["candidates"].values())
    remaining = sum(total_units.values()) - done
    if remaining > 0:
        print("\n%d unit(s) still to run. Re-run the same command; the ledger "
              "resumes." % remaining)
    else:
        print("\nall %d unit(s) complete." % done)


# ---------------------------------------------------------------------- the CLI

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Pin the Executor: one A' draw x 36 tasks x 3 candidates.")
    parser.add_argument("--out", default=DEFAULT_ROOT,
                        help="ledger directory; re-running the same one resumes")
    parser.add_argument("--limit", type=int, default=None,
                        help="first N tasks only, for a cheap smoke run")
    parser.add_argument("--candidate", action="append", dest="candidates",
                        help="restrict to one candidate; repeatable")
    parser.add_argument("--report", action="store_true",
                        help="read the ledger and print the report; no calls, "
                             "no key needed")
    parser.add_argument("--replay-plans", default=None, metavar="LEDGER",
                        help="reuse the specs and suites already in LEDGER "
                             "instead of calling the Planner. Zero Planner "
                             "calls, no Gemini key, and the suite is carried "
                             "over with its digest re-checked. Tasks with no "
                             "spec in LEDGER are skipped, not planned.")
    parser.add_argument("--json", action="store_true",
                        help="print the summary as JSON as well")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    candidates = tuple(args.candidates or CANDIDATES)
    unknown = [name for name in candidates if name not in CANDIDATES]
    if unknown:
        print("unknown candidate(s): %s; this run measures %s"
              % (", ".join(unknown), ", ".join(CANDIDATES)), file=sys.stderr)
        return 2

    tasks = locked_tasks()
    if args.limit is not None:
        tasks = tasks[:args.limit]
    root = os.path.abspath(args.out)
    if not os.path.isdir(root):
        os.makedirs(root)
    path = ledger_path(root)
    units, malformed = load_ledger(path)

    replay_path, replay_plans = None, None
    if args.replay_plans:
        replay_path = os.path.abspath(args.replay_plans)
        if os.path.abspath(path) == replay_path:
            # Reading and appending to one file while resuming from it is a
            # question about ordering nobody should have to answer. Refuse.
            print("--replay-plans must name a DIFFERENT ledger than --out; %s "
                  "is both" % replay_path, file=sys.stderr)
            return 2
        source_units, source_malformed = load_ledger(replay_path)
        if not source_units:
            print("no records to replay in %s" % replay_path, file=sys.stderr)
            return 2
        replay_plans = replayable_plans(source_units)
        if source_malformed:
            print("source ledger: %d incomplete line(s) ignored"
                  % source_malformed)
        # Named here, before anything runs, because a replay that silently
        # skipped 17 of 36 tasks would produce a ledger whose task set differs
        # from the lock's with nothing on the record saying why.
        missing = [task.task_id for task in tasks
                   if task.task_id not in replay_plans]
        print("replaying plans from %s" % replay_path)
        print("  %d task(s) have a stored spec; %d will be skipped%s"
              % (len(tasks) - len(missing), len(missing),
                 "" if not missing else ": " + ", ".join(missing)))
        # The tasks that can be drawn are the ones with a spec. Skipped tasks are
        # excluded from the denominator too, so "all units complete" means what it
        # says instead of standing at 19/36 forever.
        tasks = [task for task in tasks if task.task_id in replay_plans]
        if not tasks:
            print("nothing to replay: no stored spec matches the locked tasks",
                  file=sys.stderr)
            return 2

    total_units = {KIND_PLAN: len(tasks),
                   KIND_DRAW: len(tasks) * len(candidates)}
    if malformed:
        print("ledger: %d incomplete line(s) dropped; those units will re-run"
              % malformed)

    if args.report:
        summary = summarise(units, candidates)
        print_report(summary, total_units)
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    # Loud on purpose, before a key is asked for. Silent failover would let a Groq
    # 429 answer an Executor call from Gemini, and a probe whose point is to
    # compare three Groq models would quietly have measured a fourth model.
    agents_core.set_measurement_mode(True)
    # `agents_core.PACER` ships disabled because `run_eval` does its own pacing in
    # `RateGovernor`, inside `Instrument`, which wraps `call_model`. This runner
    # calls `call_role` directly and has no Instrument, so without this line 108
    # Groq calls would go out as fast as the socket allows. The intervals come from
    # `MIN_CALL_INTERVAL_SECONDS` rather than being restated here.
    #
    # For the same reason the retry bound is left at the module default instead of
    # being pinned to 1 the way `run_eval` pins it: there is only one retry layer
    # in this process, so there is nothing for it to multiply with.
    #
    # Guarded the way `app.py` guards it, so a caller that has already chosen a
    # pacing policy keeps it rather than having this line overrule them.
    if not agents_core.PACER.enabled:
        agents_core.set_pacing(True)
    print("ledger:      %s" % path)
    print("tasks:       %d (locked)" % len(tasks))
    print("candidates:  %s" % ", ".join(candidates))
    if replay_plans is None:
        print("spend:       %d plan unit(s) x 1-2 Gemini calls + %d Groq call(s)"
              % (len([t for t in tasks
                      if not is_done(units.get((KIND_PLAN, t.task_id, "")))]),
                 len([1 for t in tasks for c in candidates
                      if not is_done(units.get((KIND_DRAW, t.task_id, c)))])))
        print("             (1-2 because the Test Writer is called only when the "
              "audit rejects\n              the plan's own TESTS block)")
    else:
        print("spend:       0 Gemini calls (plans replayed) + %d Groq call(s)"
              % len([1 for t in tasks for c in candidates
                     if not is_done(units.get((KIND_DRAW, t.task_id, c)))]))
        print("             (the Planner and the Test Writer are not called in "
              "this mode; the\n              specs and suites come from the "
              "source ledger)")
    print("pacing:      %s"
          % "  ".join("%s>=%gs" % (name, agents_core.PACER.interval_for(name))
                      for name in sorted(agents_core.PROVIDERS)))
    print("retries:     %d attempt(s) per call, one layer"
          % agents_core.retry_attempts())
    print("sampling:    %s"
          % json.dumps(agents_core.sampling_for("planner"), sort_keys=True))
    configure(candidates[0])
    print("executor:    %s + %s"
          % (candidates[0],
             json.dumps(agents_core.sampling_for("executor"), sort_keys=True)))
    print("measurement mode: on (no silent failover)\n")

    # A replay draws from Groq only, so it asks for the Groq key and nothing else.
    providers = (["groq"] if replay_plans is not None
                 else sorted({agents_core.ROLE_PROVIDER["planner"], "groq"}))
    keys = ask_keys(providers)
    records = preflight(keys, candidates,
                        include_planner=replay_plans is None)
    for record in records:
        print("  %-4s %s -> %s/%s%s"
              % ("OK" if record["ok"] else "FAIL", "+".join(record["roles"]),
                 record["provider"], record["model"],
                 "" if record["ok"] else "  " + record["detail"]))
    problems = preflight_failures(records)
    if problems:
        print("\nREFUSING TO START. Nothing has been spent.", file=sys.stderr)
        for line in problems:
            print("  %s" % line, file=sys.stderr)
        return 1

    aborted = None
    try:
        _run(tasks, candidates, keys, path, units,
             replay_plans=replay_plans, replay_path=replay_path)
    except agents_core.DailyQuotaExhausted as exc:
        # Reported, not raised as a traceback: everything drawn so far is fsynced
        # in the ledger and a resume will pick up exactly here, so the useful
        # output is the summary plus the quota's name, not a stack.
        aborted = exc
    summary = summarise(units, candidates)
    print_report(summary, total_units)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    if aborted is not None:
        print("\nABORTED: %s" % aborted, file=sys.stderr)
        print("Nothing above is lost -- re-run the same command once the quota "
              "window has reset and it resumes from the ledger.", file=sys.stderr)
        return 3
    return 0


def _run(tasks, candidates, keys, path, units, replay_plans=None,
         replay_path=None):
    """Draw every outstanding unit, persisting each one as it completes.

    With `replay_plans` the plan step is a copy from another ledger rather than a
    Planner call, and that is asserted rather than trusted: `planner_calls_since`
    is checked after every plan step, before the draws that follow it. An assertion
    that only ran at the end would fire after the Groq spend it was meant to
    protect -- and the failure it guards against is exactly the silent one, a
    refactor that reintroduces `make_plan` on some branch nobody re-reads.
    """
    calls_at_start = len(agents_core.CALL_LOG)
    for index, task in enumerate(tasks, 1):
        key = (KIND_PLAN, task.task_id, "")
        if is_done(units.get(key)):
            print("[%2d/%d] %-22s plan  resumed" % (index, len(tasks),
                                                    task.task_id))
        elif replay_plans is not None:
            source = replay_plans.get(task.task_id)
            if source is None:
                # Skipped, not planned. Planning it here would spend the Gemini
                # call the mode exists to avoid, and would put a spec in this
                # ledger that the source ledger's draws never answered.
                print("[%2d/%d] %-22s plan  skipped: no spec in the source "
                      "ledger" % (index, len(tasks), task.task_id))
                continue
            record = append(path, replayed_plan_unit(source, replay_path))
            units[key] = record
            print("[%2d/%d] %-22s plan  replayed %s suite %s"
                  % (index, len(tasks), task.task_id, record["tests_status"],
                     (record["tests_sha256"] or "")[:12]))
        else:
            record = append(path, plan_unit(task, keys))
            units[key] = record
            print("[%2d/%d] %-22s plan  %s" % (
                index, len(tasks), task.task_id,
                "%s suite" % record["tests_status"] if is_done(record)
                else "FAILED %s" % record.get("exc_class")))
        if replay_plans is not None:
            leaked = planner_calls_since(calls_at_start)
            if leaked:
                raise SystemExit(
                    "REFUSING TO CONTINUE: --replay-plans made %d Planner-side "
                    "call(s) (%s). The specs would no longer be the ones the "
                    "source ledger's draws answered."
                    % (len(leaked),
                       ", ".join(sorted(set(e.get("role") for e in leaked)))))
        plan_record = units.get(key)
        if not is_done(plan_record):
            print("        no plan, so no draws for this task")
            continue
        for candidate in candidates:
            draw_key = (KIND_DRAW, task.task_id, candidate)
            if is_done(units.get(draw_key)):
                print("        %-24s resumed" % candidate)
                continue
            record = append(path, draw_unit(task, plan_record, candidate, keys))
            units[draw_key] = record
            if not is_done(record):
                print("        %-24s FAILED %s" % (candidate,
                                                   record.get("exc_class")))
                continue
            print("        %-24s %-8s gate=%s  %s"
                  % (candidate, "PASS" if record["passed"] else "fail",
                     record["gate_verdict"],
                     "no code" if not record["extracted_code"] else ""))


if __name__ == "__main__":
    sys.exit(main())

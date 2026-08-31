"""D8: the calibration sweep and the go/no-go gate on the task set.

    python3 eval/calibrate.py --stub sampled          # offline, GO expected
    python3 eval/calibrate.py --stub perfect          # offline, NO-GO expected
    python3 eval/calibrate.py                         # real, ~36 gemini + 360 groq
    python3 eval/calibrate.py --gate-only             # recompute from stored draws

Ten A' draws per task, graded by the hidden suite, give a per-task calibration
rate `d_t`. A task with `d_t` at 0 is impossible for the pipeline and a task with
`d_t` at 1 is free; neither can show a difference between arms, so a task set
made mostly of those measures nothing no matter how many seeds are spent on it.
The gate counts how many tasks sit strictly inside the band and refuses the grid
if too few do.

This is not grid data. It writes to its own directory, it computes no contrast,
and nothing here may be pooled with `eval/results/`: `d_t` is ten draws of one
arm against one spec, and reading it as an A' pass rate would be reading a
deliberately over-sampled single-arm measurement as a result.
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import agents_core            # noqa: E402
import gen_tasks              # noqa: E402
import run_eval               # noqa: E402

# Its own root, a sibling of `results/` rather than a directory inside it. The
# separation is structural on purpose: `run_eval.load_records` walks
# `results/seed-N/arm-*/` and would happily read calibration draws as arm cells
# if they were ever placed there, and ten draws of A' on one spec pooled into a
# grid would move every pooled number without anything looking wrong.
CALIBRATION_DIR = os.path.join(_HERE, "calibration")

# Ten, so `d_t` has a resolution of 0.1 -- which is what makes the band below an
# *open* interval rather than a closed one. The achievable values are exactly
# 0.0, 0.1, ... 1.0, so 0.1 and 0.9 are values a task really lands on, and they
# are one draw away from floor and ceiling. Including them would admit tasks
# that a single lucky sample separates from useless.
CALIBRATION_DRAWS = 10

BAND_LOW = 0.1
BAND_HIGH = 0.9

# The go/no-go threshold: the number of tasks whose `d_t` must fall strictly
# inside the band. ONE constant, read in one place, printed with the verdict and
# recorded in the manifest.
#
# The pre-registration says "fewer than 12 of 30". The code generates 36 tasks
# across 18 families, so the registered number does not apply as written, and
# restating it is a registration decision the user still owes. It is a named
# default here rather than an `if` so that the decision is visible when it is
# made: 15 is the smallest integer at or above the registered 40% proportion
# (0.4 x 36 = 14.4), which is the most conservative reading of the registered
# gate rather than a new gate. `--min-in-band` overrides it, and the manifest
# records both the value used and whether it was the default, so a sweep run
# against a re-registered threshold says so on its face.
GATE_MIN_IN_BAND = 15

GATE_AS_REGISTERED = "fewer than 12 of 30 in-band tasks is a NO-GO"

NO_GO_SENTENCE = (
    "NO-GO: the task set is at floor or ceiling. Too few tasks land strictly "
    "inside the band, which means most of them cannot show a difference "
    "between arms at any sample size, and the grid would spend its whole "
    "budget measuring tasks that were already decided. The task set must be "
    "regenerated or re-tiered before k=1 is run."
)

GO_SENTENCE = (
    "GO: enough tasks are neither impossible nor free for the grid to be "
    "capable of showing a difference. This says nothing about whether there "
    "is one."
)


# --------------------------------------------------------------- sampling stub

class SamplingStub(run_eval.StubModel):
    """A stub whose Executor draws actually vary per draw, for the GO path.

    None of the shipped stub qualities can demonstrate a GO. `perfect` and
    `broken` are constant, so every `d_t` is exactly 1.0 or 0.0 and the verdict
    is a correct NO-GO on a degenerate input. `flaky` alternates on a
    process-level call counter, so ten consecutive draws are five right and five
    wrong and every single task lands on `d_t = 0.5` -- a GO, but one where the
    band is doing no work and the per-tier breakdown is a column of the same
    number.

    This one gives each task a hidden per-task success rate keyed off its
    `task_id` and samples each draw against it, so `d_t` scatters, some tasks
    land outside the band, and the gate's count is a count of something. Rates
    are drawn from {0.1 ... 0.9}: the two extremes exist so the demonstration
    includes tasks the gate must reject.

    Deterministic given the task set: the RNG is seeded per (task_id, draw), so
    a resumed sweep and a fresh one produce the same draws, which is what makes
    the resume path verifiable at all. It measures nothing about any model.
    """

    QUALITY = "sampled"

    def __init__(self, live_models=None):
        run_eval.StubModel.__init__(self, quality=self.QUALITY,
                                    live_models=live_models,
                                    vacuous_plan=False)
        self.draw = 0

    def rate_for(self, task_id):
        """This task's hidden success rate, in {0.1, 0.2, ... 0.9}."""
        digest = hashlib.sha256(("rate|%s" % task_id).encode("utf-8")).hexdigest()
        return round(0.1 + 0.1 * (int(digest[:8], 16) % 9), 1)

    def _good(self):
        seed = "draw|%s|%d" % (self.task.task_id, self.draw)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        roll = random.Random(int(digest[:16], 16)).random()
        return roll < self.rate_for(self.task.task_id)

    def __call__(self, provider, system, user, role=None):
        # Resolved the same way `StubModel` resolves it, and gated by the same
        # inherited `probe`, so the sweep and the grid simulate a wrong slug
        # against the same string a real call would send.
        model = agents_core.model_for(role, provider) if role else None
        if model is None:
            model = agents_core.PROVIDERS[provider]["model"]
        self.probe(provider, None, model, system, user)
        if self._role(system) != "executor":
            return run_eval.StubModel.__call__(self, provider, system, user,
                                               role=role)
        self.executor_calls += 1
        return "```python\n%s```\n" % (self._correct() if self._good()
                                       else self._wrong())


STUBS = ("perfect", "broken", "flaky", SamplingStub.QUALITY)


def make_stub(quality, live_models=None):
    if quality == SamplingStub.QUALITY:
        return SamplingStub(live_models=live_models)
    return run_eval.StubModel(quality, live_models=live_models)


# ------------------------------------------------------------- the plan and draws

def calibration_plan(task, keys):
    """One Planner call. Deliberately *not* `run_eval.make_plan`.

    `make_plan` also resolves the visible suite, which costs a second Gemini call
    -- a Test Writer regeneration -- on every task whose plan writes a suite the
    vacuity audit rejects. Calibration never gates: `d_t` is the hidden suite's
    verdict on each draw, so the visible suite is not read even once. Resolving
    it would spend the scarcest currency in the whole project on an artifact
    nothing here looks at, and would break the "one Gemini call per task"
    property this mode is supposed to hold to.

    The Planner call itself is byte-for-byte the call the grid makes -- same
    role, same prompt, same provider, same sampling parameters -- so the spec
    `d_t` is measured against is drawn from the same distribution arm A' sees.
    """
    text, provider = agents_core.call_role(
        "planner", keys, agents_core.PROMPTS["planner"], task.prompt)
    return {"task_id": task.task_id,
            "spec": agents_core.extract_spec(text),
            "planner_provider": provider,
            "steps": len(agents_core.extract_steps(text)),
            "plan_sha256": agents_core.sha256_of(text),
            "spec_sha256": agents_core.sha256_of(
                agents_core.extract_spec(text) or "")}


def plan_path(root, seed, task_id):
    return os.path.join(root, "seed-%d" % seed, "plans", "%s.json" % task_id)


def draw_path(root, seed, task_id, draw):
    return os.path.join(root, "seed-%d" % seed, "draws",
                        "%s__d%d.json" % (task_id, draw))


def one_draw(task, plan, keys, instrument, draw):
    """One Executor sample from the stored spec, graded by the hidden suite."""
    instrument.reset()
    started = time.time()
    record = {"task_id": task.task_id, "family": task.family,
              "tier": task.tier, "variant": task.variant, "draw": draw,
              "seed": task.seed,
              "spec_sha256": plan.get("spec_sha256", ""),
              "hidden_tests_sha256": gen_tasks.digest(task.tests),
              "measurement_mode": agents_core.MEASUREMENT_MODE}
    try:
        sample = run_eval._draw(plan["spec"], keys)
        record.update({"provider_last": sample["provider"],
                       "raw_len": sample["raw_len"],
                       "code_sha256": agents_core.sha256_of(sample["code"] or "")})
        record.update(run_eval.grade(sample["code"], task))
        record["outcome"] = run_eval.OUTCOME_GRADED
        record["error"] = ""
    except agents_core.ProviderError as exc:
        # Same representation as the grid: no answer came back, so there is
        # nothing to grade and no `passed` field. A draw that died on a rate
        # limit must not become a failed draw, because `d_t` is a ratio and a
        # rate-limit death in the numerator would make an unreachable task look
        # merely hard.
        record["outcome"] = run_eval.OUTCOME_INFRA_LOSS
        record["error"] = "provider: %s" % agents_core.redact(exc, keys)
    record["seconds"] = round(time.time() - started, 2)
    record["calls"] = instrument.calls
    record["calls_by_provider"] = dict(instrument.calls_by_provider)
    return record


# ------------------------------------------------------------------------ the gate

def calibration_rate(draws):
    """`d_t`: the share of *graded* draws that passed the hidden suite.

    Infra losses are excluded from the denominator rather than counted as
    failures, and `graded` is reported alongside so a `d_t` computed from four
    surviving draws is not read as one computed from ten.
    """
    graded = [d for d in draws
              if d.get("outcome") == run_eval.OUTCOME_GRADED]
    if not graded:
        return None, 0, 0
    passed = sum(1 for d in graded if d.get("passed"))
    return round(float(passed) / len(graded), 3), passed, len(graded)


def in_band(rate):
    """Strictly inside. 0.1 and 0.9 are excluded, and are reachable values."""
    return rate is not None and BAND_LOW < rate < BAND_HIGH


def gate(per_task, threshold):
    """Count in-band tasks against the threshold and return the verdict."""
    counted = [entry for entry in per_task]
    in_band_tasks = [entry for entry in counted if in_band(entry["d_t"])]
    by_tier = {}
    for entry in counted:
        row = by_tier.setdefault(str(entry["tier"]),
                                 {"tasks": 0, "in_band": 0, "at_floor": 0,
                                  "at_ceiling": 0, "unmeasured": 0})
        row["tasks"] += 1
        rate = entry["d_t"]
        if rate is None:
            row["unmeasured"] += 1
        elif in_band(rate):
            row["in_band"] += 1
        elif rate <= BAND_LOW:
            row["at_floor"] += 1
        else:
            row["at_ceiling"] += 1
    return {"go": len(in_band_tasks) >= threshold,
            "in_band": len(in_band_tasks),
            "threshold": threshold,
            "threshold_constant": "GATE_MIN_IN_BAND",
            "threshold_is_default": threshold == GATE_MIN_IN_BAND,
            "gate_as_registered": GATE_AS_REGISTERED,
            "band": [BAND_LOW, BAND_HIGH],
            "band_is_open_interval": True,
            "tasks": len(counted),
            "at_floor": sum(1 for e in counted
                            if e["d_t"] is not None and e["d_t"] <= BAND_LOW),
            "at_ceiling": sum(1 for e in counted
                              if e["d_t"] is not None and e["d_t"] >= BAND_HIGH),
            "unmeasured": sum(1 for e in counted if e["d_t"] is None),
            "by_tier": by_tier,
            "in_band_task_ids": sorted(e["task_id"] for e in in_band_tasks)}


# ------------------------------------------------------------------- projection

def project_calls(tasks, root, seed, draws):
    """What this sweep will spend, per provider, before it spends any of it.

    Counted against what is already on disk, because resume is the normal case
    on a free tier and a projection that ignores it overstates the bill by
    whatever the last attempt got through. Per provider and not in total: a free
    tier is per provider, so "396 calls" is not the number anyone can check
    against a quota.
    """
    planner = agents_core.ROLE_PROVIDER["planner"]
    executor = agents_core.ROLE_PROVIDER["executor"]
    plans_needed = sum(
        1 for task in tasks
        if not os.path.exists(plan_path(root, seed, task.task_id)))
    draws_needed = 0
    for task in tasks:
        for index in range(1, draws + 1):
            if not os.path.exists(draw_path(root, seed, task.task_id, index)):
                draws_needed += 1
    by_provider = {}
    for provider, count in ((planner, plans_needed), (executor, draws_needed)):
        by_provider[provider] = by_provider.get(provider, 0) + count
    return {"by_provider": by_provider,
            "plans_needed": plans_needed,
            "draws_needed": draws_needed,
            "planner_provider": planner,
            "executor_provider": executor,
            # Per role, not per provider. Two roles can now share a provider and
            # call different models, so "the groq model" is no longer a
            # well-formed thing to project a cost against.
            "models": dict((role, agents_core.model_for(role))
                           for role in ("planner", "executor"))}


def print_projection(projection, tasks, draws):
    print("\nprojected cost, before anything is spent:")
    for role in ("planner", "executor"):
        provider = projection["%s_provider" % role]
        print("  %-8s %-8s %4d call(s)   model %s"
              % (role, provider, projection["by_provider"][provider],
                 projection["models"][role]))
    print("  = %d task(s) x 1 %s planner call + %d task(s) x %d %s draws, "
          "minus what is already on disk"
          % (len(tasks), projection["planner_provider"], len(tasks), draws,
             projection["executor_provider"]))
    print("  no other %s call is made: calibration never gates, so the visible "
          "suite is never resolved and the Test Writer is never reached."
          % projection["planner_provider"])


# ---------------------------------------------------------------------- reporting

def print_verdict(verdict, locked_tasks=None):
    print("\n=== calibration gate (D8)")
    print("  band: (%g, %g), open -- %g and %g are reachable values at %d "
          "draws and are excluded"
          % (BAND_LOW, BAND_HIGH, BAND_LOW, BAND_HIGH, CALIBRATION_DRAWS))
    print("  threshold: %s = %d in-band task(s) required%s"
          % (verdict["threshold_constant"], verdict["threshold"],
             "" if verdict["threshold_is_default"]
             else "  (overridden; default is %d)" % GATE_MIN_IN_BAND))
    print("  as registered: %s -- the locked task set has %s tasks, so this is "
          "the restatement the registration still owes"
          % (verdict["gate_as_registered"],
             "?" if locked_tasks is None else locked_tasks))
    if verdict["tasks"] != locked_tasks:
        print("  NOTE: this run covers %d of those %s tasks, so the count below "
              "cannot clear the whole grid."
              % (verdict["tasks"],
                 "?" if locked_tasks is None else locked_tasks))
    print("\n  %-6s %6s %8s %8s %10s %11s"
          % ("tier", "tasks", "in-band", "floor", "ceiling", "unmeasured"))
    for tier in sorted(verdict["by_tier"]):
        row = verdict["by_tier"][tier]
        print("  %-6s %6d %8d %8d %10d %11d"
              % (tier, row["tasks"], row["in_band"], row["at_floor"],
                 row["at_ceiling"], row["unmeasured"]))
    print("  %-6s %6d %8d %8d %10d %11d"
          % ("all", verdict["tasks"], verdict["in_band"], verdict["at_floor"],
             verdict["at_ceiling"], verdict["unmeasured"]))
    print("\n  in band: %d of %d, threshold %d"
          % (verdict["in_band"], verdict["tasks"], verdict["threshold"]))
    print("\n%s" % (GO_SENTENCE if verdict["go"] else NO_GO_SENTENCE))


def print_rates(per_task):
    # 26 and not 24: `query-canonicalization-01` is 25 characters, and at 24 the
    # two query-canonicalization rows pushed every later column one place right,
    # which reads as a different table rather than a wider one.
    print("\n  %-26s %5s %6s %7s  %s"
          % ("task", "tier", "d_t", "graded", "draws (1 = hidden suite passed)"))
    for entry in sorted(per_task, key=lambda e: e["task_id"]):
        marks = "".join("1" if value is True else ("0" if value is False else ".")
                        for value in entry["draw_passed"])
        print("  %-26s %5s %6s %4d/%-2d  %s%s"
              % (entry["task_id"], entry["tier"],
                 "  n/a" if entry["d_t"] is None else "%.1f" % entry["d_t"],
                 entry["passed"], entry["graded"], marks,
                 "" if in_band(entry["d_t"]) else "   <- out of band"))


# ------------------------------------------------------------------------ loading

def load_json(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except Exception:
        return None


def collect(tasks, root, seed, draws):
    """Per task: every stored draw, `d_t`, and the pass/fail pattern."""
    per_task = []
    for task in tasks:
        stored = []
        for index in range(1, draws + 1):
            record = load_json(draw_path(root, seed, task.task_id, index))
            if record is not None:
                stored.append(record)
        rate, passed, graded = calibration_rate(stored)
        by_index = dict((r.get("draw"), r) for r in stored)
        per_task.append({
            "task_id": task.task_id, "family": task.family, "tier": task.tier,
            "variant": task.variant, "d_t": rate, "passed": passed,
            "graded": graded, "draws_stored": len(stored),
            "draws_requested": draws,
            "in_band": in_band(rate),
            # One entry per requested draw: True, False, or None for a draw that
            # is missing or was an infra loss. `d_t` alone cannot distinguish
            # "half the draws failed" from "half the draws never happened".
            "draw_passed": [
                (by_index[i].get("passed")
                 if i in by_index
                 and by_index[i].get("outcome") == run_eval.OUTCOME_GRADED
                 else None)
                for i in range(1, draws + 1)],
            "hidden_tests_sha256": gen_tasks.digest(task.tests)})
    return per_task


# ---------------------------------------------------------------------------- CLI

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="D8 calibration: %d A' draws per task, then the go/no-go "
                    "gate. Runs A' only." % CALIBRATION_DRAWS)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--draws", type=int, default=CALIBRATION_DRAWS,
                        help="draws per task (default %d; the band's open "
                             "endpoints assume this resolution)"
                             % CALIBRATION_DRAWS)
    parser.add_argument("--min-in-band", type=int, default=None,
                        help="override GATE_MIN_IN_BAND (default %d). Recorded "
                             "in the manifest as an override."
                             % GATE_MIN_IN_BAND)
    parser.add_argument("--out", default=CALIBRATION_DIR,
                        help="output root (default eval/calibration/, which is "
                             "NOT eval/results/ and must not become it)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--per-family", type=int, default=2,
                        help="variants per family (default 2, matching "
                             "run_eval and the lock)")
    parser.add_argument("--tier", type=int, action="append", dest="tiers")
    parser.add_argument("--family", action="append", dest="families")
    parser.add_argument("--stub", choices=STUBS,
                        help="run offline against a local generator. `sampled` "
                             "is the only one whose draws vary per draw, so it "
                             "is the only one that can demonstrate a GO.")
    parser.add_argument("--gate-only", action="store_true",
                        help="recompute the verdict from stored draws; makes "
                             "no calls")
    parser.add_argument("--force", action="store_true",
                        help="redraw tasks that already have draw files")
    parser.add_argument("--no-preflight", action="store_true",
                        help="skip the one-call-per-pair validation and spend "
                             "the sweep even if a configured model is dead")
    parser.add_argument("--live-models", default=None,
                        help="comma-separated model IDs the stand-in pretends "
                             "the key can reach; every other ID 404s. Requires "
                             "--stub. This is how the preflight's refusal is "
                             "demonstrated offline, against the real retired "
                             "slug rather than a synthetic one.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _select(args):
    """The task set and the lock check. Refuses rather than warning."""
    tasks = gen_tasks.generate(seed=args.seed, per_family=args.per_family,
                              tiers=set(args.tiers or ()),
                              families=set(args.families or ()),
                              limit=args.limit)
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return None, None
    # A calibration measured against a different task set than the grid runs is
    # worthless, and worse than worthless if it reports a GO: the gate would
    # have cleared a set nobody is going to run. The lock digest is what makes
    # that checkable afterwards, so it is verified before any call and recorded
    # in the manifest below.
    failures, reason = gen_tasks.verify_lock_or_reason(tasks=tasks,
                                                      seed=args.seed)
    if reason:
        print("refusing to run: %s" % reason, file=sys.stderr)
        return None, None
    if failures:
        print("refusing to run: the task set does not match %s (%d problem%s):"
              % (os.path.basename(gen_tasks.LOCK_PATH), len(failures),
                 "" if len(failures) == 1 else "s"), file=sys.stderr)
        for line in failures:
            print("  FAIL %s" % line, file=sys.stderr)
        print("A calibration measured against a task set that moved under it "
              "cannot clear the grid, because the grid would be a different "
              "experiment.", file=sys.stderr)
        return None, None
    lock = gen_tasks.load_lock()
    print("tasks.lock verified: %d locked task(s), seed %d, self-check passed "
          "at %s" % (lock["counts"]["tasks"], lock["generator"]["seed"],
                     lock["self_check"]["at"]))
    return tasks, lock


def build_manifest(args, tasks, lock, verdict, per_task, projection, spent,
                   stub, seconds, governor, preflight_records=()):
    """Everything needed to decide whether this calibration applies to a grid."""
    return {
        "kind": "calibration",
        "not_grid_data": (
            "d_t is %d draws of A' against one spec per task. It is not an A' "
            "pass rate and must not be pooled with eval/results/." % args.draws,
        )[0],
        "seed": args.seed,
        "draws_per_task": args.draws,
        "tasks": len(tasks),
        "arm": "a_prime",
        "gate": verdict,
        "threshold": {"constant": "GATE_MIN_IN_BAND",
                      "default": GATE_MIN_IN_BAND,
                      "used": verdict["threshold"],
                      "overridden": not verdict["threshold_is_default"],
                      "as_registered": GATE_AS_REGISTERED},
        "tasks_lock": {"path": os.path.basename(gen_tasks.LOCK_PATH),
                       "body_sha256": lock.get("body_sha256", ""),
                       "locked_tasks": lock["counts"]["tasks"],
                       "seed": lock["generator"]["seed"],
                       "self_check_at": lock["self_check"]["at"],
                       "verified_at_startup": True},
        # Resolved at run time, role by role, not read off a source constant:
        # `models_used` was `provider -> PROVIDERS[provider]["model"]`, which
        # cannot express two roles sharing a provider at different models and
        # would have reported the pristine slug for a run whose provider table
        # was mutated in place. `models` is the record D2 freezes; `models_used`
        # stays beside it, derived from the same resolution, so an existing
        # reader keeps working.
        "models": agents_core.resolved_roles(),
        "models_source": agents_core.model_config_source(),
        "independence_warnings": agents_core.independence_warnings(),
        "preflight": preflight_records,
        "preflight_skipped": bool(args.no_preflight),
        "models_used": dict(
            (agents_core.ROLE_PROVIDER[role], agents_core.model_for(role))
            for role in sorted(agents_core.ROLE_PROVIDER)),
        "projected_calls": projection,
        "actual_calls_by_provider": spent,
        "stub": None if stub is None else stub.quality,
        "measurement_mode": agents_core.MEASUREMENT_MODE,
        "pacing": {"governor_rates_per_min": run_eval.DEFAULT_RATES,
                   "governor_burst": run_eval.BURST,
                   "governor_applied": stub is None,
                   "governor_max_429_retries": run_eval.MAX_429_RETRIES,
                   "governor_fallback_backoff": list(run_eval.FALLBACK_BACKOFF),
                   "rate_limits": governor.rate_limits,
                   "agents_core_pacer": agents_core.PACER.snapshot(),
                   "agents_core_retry": agents_core.retry_snapshot()},
        "seconds": round(seconds, 1),
        "per_task": sorted(per_task, key=lambda e: e["task_id"]),
    }


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = os.path.abspath(args.out)
    threshold = GATE_MIN_IN_BAND if args.min_in_band is None else args.min_in_band
    tasks, lock = _select(args)
    if tasks is None:
        return 2
    if os.path.abspath(run_eval.RESULTS_DIR) == root or root.startswith(
            os.path.abspath(run_eval.RESULTS_DIR) + os.sep):
        # Refused rather than warned about. Ten A' draws sitting inside a grid
        # directory would be read by `run_eval.load_records` as arm cells, and a
        # pooled pass rate built out of them would look entirely ordinary.
        print("refusing to run: --out is inside %s. Calibration is not grid "
              "data and must not be written where the grid's loader will find "
              "it." % run_eval.RESULTS_DIR, file=sys.stderr)
        return 2

    tasks = run_eval._task_order(tasks, args.seed)

    # Resolved before the projection, because the projection prints which model
    # each role will call and a config read afterwards would make that print a
    # lie. With no config present this installs nothing and the source stays
    # "defaults", so a calibration run today behaves exactly as it did.
    try:
        resolved = agents_core.configure_models()
    except agents_core.ConfigError as exc:
        print("refusing to run: model configuration: %s" % exc, file=sys.stderr)
        return 2
    print("models (%s):" % agents_core.model_config_source())
    for entry in resolved:
        print("  %-12s %-8s %s  (temperature=%g top_p=%g)"
              % (entry["role"], entry["provider"], entry["model"],
                 entry["temperature"], entry["top_p"]))
    for line in agents_core.independence_warnings():
        print("  WARNING: %s" % line)

    projection = project_calls(tasks, root, args.seed, args.draws)

    if args.gate_only:
        per_task = collect(tasks, root, args.seed, args.draws)
        verdict = gate(per_task, threshold)
        print_rates(per_task)
        print_verdict(verdict, lock["counts"]["tasks"])
        return 0 if verdict["go"] else 1

    print_projection(projection, tasks, args.draws)
    keys = run_eval._keys_from_env()
    live_models = None
    if args.live_models is not None:
        if not args.stub:
            print("--live-models requires --stub; it tells a stand-in what to "
                  "answer for and has no meaning against a real provider.",
                  file=sys.stderr)
            return 2
        live_models = set(name.strip() for name in args.live_models.split(",")
                          if name.strip())
        print("--live-models: the stub answers only for %s; every other "
              "(provider, model) pair 404s, preflight included."
              % ", ".join(sorted(live_models) or ["<nothing>"]))
    stub = make_stub(args.stub, live_models=live_models) if args.stub else None
    if stub is None:
        needed = sorted(set([agents_core.ROLE_PROVIDER["planner"],
                             agents_core.ROLE_PROVIDER["executor"]]))
        absent = [name for name in needed if not keys.get(name)]
        if absent:
            print("no key for %s (set %s), or pass "
                  "--stub to validate this offline"
                  % (", ".join(absent), agents_core.key_env_hint()),
                  file=sys.stderr)
            return 2
        keys = dict((name, key) for name, key in keys.items() if key)
    else:
        # Every configured provider, not a hardcoded pair.
        keys = dict((name, "stub") for name in agents_core.PROVIDERS)
        print("\n--stub %s: measures nothing about any model.%s"
              % (args.stub,
                 " Draws are constant, so every d_t will be 0.0 or 1.0 and the "
                 "verdict will be a correct NO-GO on a degenerate input."
                 if args.stub in ("perfect", "broken") else ""))

    # Before anything is spent, and for the same reason `run_eval` does it: a
    # calibration is 396 calls on the default grid, and discovering at draw 1
    # that the Executor's slug retired costs the whole sweep. One cheap call per
    # distinct (provider, model) pair, and a refusal instead of a spend.
    preflight_records = []
    if args.no_preflight:
        print("preflight SKIPPED (--no-preflight): a dead (provider, model) pair "
              "will now surface as a failed draw mid-sweep instead of a refusal.")
    else:
        call = None if stub is None else stub.probe
        preflight_records = agents_core.preflight(
            keys, roles=("planner", "executor"), call=call)
        problems = agents_core.preflight_failures(preflight_records)
        if problems:
            print("refusing to run: %d configured (provider, model) pair(s) do "
                  "not answer:" % len(problems), file=sys.stderr)
            for line in problems:
                print("  FAIL %s" % line, file=sys.stderr)
            print("Point the role at a model the key can reach -- "
                  "`python3 eval/models.py --list <provider>` says which -- or "
                  "pass --no-preflight to spend the sweep anyway.",
                  file=sys.stderr)
            return 2
        print("preflight OK: %s"
              % "; ".join("%s -> %s/%s" % ("+".join(record["roles"]),
                                           record["provider"], record["model"])
                          for record in preflight_records))

    agents_core.set_measurement_mode(True)
    agents_core.set_retry_attempts(1)
    governor = run_eval.RateGovernor()
    print("measurement mode ON; A' only, %d draw(s) per task, %d task(s)."
          % (args.draws, len(tasks)))

    spent = {}
    started = time.time()
    events = agents_core.EventLog(
        os.path.join(root, "seed-%d" % args.seed, "events.jsonl"),
        context={"seed": args.seed, "mode": "calibration"}, keys=keys)
    with run_eval.Instrument(governor, stub=stub, verbose=args.verbose) \
            as instrument, run_eval._InstalledEventLog(events):
        for task in tasks:
            if stub is not None:
                stub.task = task
            wanted = [index for index in range(1, args.draws + 1)
                      if args.force
                      or not os.path.exists(
                          draw_path(root, args.seed, task.task_id, index))]
            if not wanted:
                events.emit(agents_core.EVENT_RESUME_SKIP,
                            task_id=task.task_id, phase="calibration",
                            result="%s (all %d draws)"
                                   % (task.task_id, args.draws))
                if args.verbose:
                    print("  skip %s (all %d draws done)"
                          % (task.task_id, args.draws))
                continue

            # The plan is stored and reused, so a sweep that dies at draw 7 does
            # not pay for a second Planner call to finish -- and, more to the
            # point, does not measure draws 8-10 against a *different* spec. `d_t`
            # is only a number about a task if all its draws answered the same
            # spec, so resuming onto a fresh plan would silently make it a number
            # about two.
            path = plan_path(root, args.seed, task.task_id)
            plan = load_json(path)
            reused = plan is not None and plan.get("spec")
            if not reused:
                instrument.reset()
                agents_core.reset_call_log()
                try:
                    with events.scope(task_id=task.task_id, phase="plan"):
                        plan = calibration_plan(task, keys)
                except agents_core.ProviderError as exc:
                    print("  !! %s plan failed: %s"
                          % (task.task_id,
                             agents_core.redact(exc, keys)[:100]))
                    for provider, count in instrument.calls_by_provider.items():
                        spent[provider] = spent.get(provider, 0) + count
                    continue
                plan["calls_by_provider"] = dict(instrument.calls_by_provider)
                for provider, count in instrument.calls_by_provider.items():
                    spent[provider] = spent.get(provider, 0) + count
                run_eval.save_record(path, plan)

            for index in wanted:
                if stub is not None:
                    stub.draw = index
                with events.scope(task_id=task.task_id, phase="calibration",
                                  repeat=index):
                    record = one_draw(task, plan, keys, instrument, index)
                run_eval.save_record(
                    draw_path(root, args.seed, task.task_id, index), record)
                for provider, count in record["calls_by_provider"].items():
                    spent[provider] = spent.get(provider, 0) + count
                if args.verbose:
                    print("  %-26s draw %2d  %-5s %5.1fs"
                          % (task.task_id, index,
                             "PASS" if record.get("passed") else
                             ("INFRA" if record["outcome"]
                              == run_eval.OUTCOME_INFRA_LOSS else "fail"),
                             record["seconds"]))
            rate, passed, graded = calibration_rate(
                [load_json(draw_path(root, args.seed, task.task_id, i))
                 for i in range(1, args.draws + 1)
                 if os.path.exists(draw_path(root, args.seed,
                                             task.task_id, i))])
            print("  %-26s tier %s  d_t = %-5s (%d/%d)%s"
                  % (task.task_id, task.tier,
                     "n/a" if rate is None else "%.1f" % rate, passed, graded,
                     "" if in_band(rate) else "   out of band"))

    seconds = time.time() - started
    per_task = collect(tasks, root, args.seed, args.draws)
    verdict = gate(per_task, threshold)
    manifest = build_manifest(args, tasks, lock, verdict, per_task, projection,
                              spent, stub, seconds, governor,
                              preflight_records)
    manifest_path = os.path.join(root, "seed-%d" % args.seed,
                                 "calibration.json")
    run_eval.save_record(manifest_path, manifest)

    print("\n%d draw(s) in %.1fs; calls actually made: %s"
          % (sum(entry["draws_stored"] for entry in per_task), seconds,
             ", ".join("%s=%d" % pair for pair in sorted(spent.items()))
             or "none"))
    print("%d event(s) written to %s" % (events.count, events.path))
    print_rates(per_task)
    print_verdict(verdict, lock["counts"]["tasks"])
    print("\nmanifest: %s" % manifest_path)
    # 0 GO, 1 NO-GO, 2 could not run. A NO-GO is a real answer, not an error,
    # but it must not be exit 0: a script that runs the grid after this one
    # would otherwise read "the gate ran" as "the gate cleared".
    return 0 if verdict["go"] else 1


def cli(argv=None):
    """`main`, plus the one failure that is not a per-cell outcome.

    A per-day quota is a fact about the day, not about a cell. Neither leg of a
    sweep converts it into one: `calibration_plan` makes its Planner call with no
    handler at all, and `one_draw` catches `agents_core.ProviderError` only --
    which `DailyQuotaExhausted` is deliberately not a subclass of -- so it is
    never scored as `OUTCOME_INFRA_LOSS` and never lands in a draw file. It
    arrives here instead, where it is a message and an exit code rather than a
    traceback.

    Every cell that completed is already on disk, written per plan and per draw by
    `run_eval.save_record`, and resume is the `os.path.exists` check on
    `plan_path` and `draw_path`, so a re-run of the same command after the window
    resets picks up from them. The manifest is deliberately not written, because
    it is written after the loop and a summary over a truncated grid is the thing
    nobody should find later and mistake for a result.
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

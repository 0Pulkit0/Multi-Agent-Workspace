#!/usr/bin/env python3
"""Import a pin probe's Planner specs into the calibration spec store.

A converter, run once, and deliberately not a mode of `calibrate.py`. The first
Executor-pin probe spent 19 Gemini requests -- a full day of the free tier -- on
Planner specs for 19 of the 36 locked tasks before a per-day quota killed the
other 17. Those 19 specs are on disk in the probe's ledger. `calibrate.py`
resumes on the existence of `plan_path(root, seed, task_id)` and never re-plans a
task whose plan file already holds a spec, so writing those 19 specs into that
store makes a calibration sweep skip 19 Planner calls it would otherwise pay for
a second time.

Why the specs are exchangeable, which is a precondition and not a nicety: `d_t`
is the number the D-1 gate rests on, and it is only that number if the spec each
draw answered came from the call the sweep itself would have made. The sweep's
Planner call is `calibrate.calibration_plan`; the probe's was
`run_eval.make_plan`. Both are literally

    text, provider = agents_core.call_role(
        "planner", keys, agents_core.PROMPTS["planner"], task.prompt)

and `call_role` resolves provider, model and sampling parameters from the role
name plus module config alone. `make_plan` goes on to resolve the visible suite,
which writes only `run.tests*` and never the spec, so the two differ in what else
they return and not in the spec they return. Role, provider and sampling
parameters -- three of the four dimensions -- are checked per record against this
checkout's own config by `planner_call_parity`, together with the model that
actually answered, and a record that disagrees is refused rather than imported.
The fourth, the prompt, no ledger records: it rests on the identical call
expression above plus `task.prompt` being a pure function of the locked task, and
the lock is verified before the ledger is read.

What is deliberately not carried across.

`plan_sha256` is not reconstructable. It is `sha256_of` the Planner's raw reply
text, and the ledger kept the extracted spec rather than the reply. It is written
empty beside `imported_plan_sha256_absent`, which says so in words, rather than
filled with a digest of different bytes: a plausible-looking hash of the wrong
input is worse than an absent one, because it compares equal to nothing and the
difference reads as tampering.

The visible suite is not carried either. `calibration_plan` resolves none --
calibration never gates, and `one_draw` grades every draw against the hidden
suite -- so carrying the probe's `tests` would give an imported plan a shape no
drawn plan has. The suite stays in the ledger, which this only reads.

What this does not change: the estimator, the task set, the draw count and the
grader are `calibrate.py`'s, untouched, and nothing in `eval/prereg/` describes
where a spec came from. One caveat it does introduce, stated rather than hidden:
the 19 imported specs were drawn on 2026-08-31 and the remaining 17 will be drawn
whenever the sweep runs, so a completed sweep's specs span two days of one
server-side model. That is already true of any sweep that resumes across a day
boundary, and it is why `imported_planner_call` records the model that answered
rather than the model this checkout would ask for.

Usage:

    python eval/import_pin_plans.py --dry-run     # decide, write nothing
    python eval/import_pin_plans.py               # write

Reads the ledger, writes only under `--out` (default `eval/calibration`), and
refuses without writing anything at all if any one record cannot be imported
soundly.
"""

import argparse
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `eval/` is not a package: the repo root above puts `agents_core` on the path and
# `sys.path[0]` puts these on it, exactly as `pin_executor` does.
import agents_core                                                  # noqa: E402
import calibrate                                                    # noqa: E402
import gen_tasks                                                    # noqa: E402
import pin_executor                                                 # noqa: E402
import run_eval                                                     # noqa: E402

DEFAULT_LEDGER = pin_executor.ledger_path(pin_executor.DEFAULT_ROOT)

# Written into every imported plan where a drawn plan carries a digest.
PLAN_SHA256_ABSENT = (
    "not reconstructable: the source ledger recorded the extracted spec, not "
    "the Planner's raw reply, and plan_sha256 is a digest of the reply. Left "
    "empty rather than filled with a digest of different bytes.")

# How far to look for draws that would end up straddling two specs. Past
# CALIBRATION_DRAWS on purpose: a sweep run at a larger --draws should still
# collide visibly here instead of silently on disk.
DRAW_PROBE_MAX = 64

# ------------------------------------------------------------------ the source

def file_sha256(path):
    """The source ledger's own digest, so an imported plan names the exact file.

    Streamed: the ledger is the whole probe and grows with every unit.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def planner_call_parity(record):
    """Why this record's Planner call is not the call `calibration_plan` makes.

    An empty list means it is: same role, same provider requested *and* used,
    same model, same sampling parameters as `agents_core` would send for the
    `planner` role on this checkout. Everything compared here comes off the
    record's own `calls` entry, which `pin_executor._CALL_KEEP` already records
    per call, so no positional guess is made about which reply belongs to which
    role.

    Non-planner calls are ignored rather than counted against the record: a plan
    whose TESTS block the audit rejected cost a second Gemini call to the Test
    Writer, and that call did not produce the spec.

    A sampling parameter that `sampling_for` sends and the ledger does not record
    is a refusal, not a pass. It cannot be compared, and the direction to fail in
    is the one that does not import a spec drawn at parameters nobody can check.
    """
    reasons = []
    calls = [call for call in (record.get("calls") or [])
             if call.get("role") == "planner"]
    if len(calls) != 1:
        return ["%d recorded call(s) with role 'planner', not 1" % len(calls)]
    call = calls[0]
    if not call.get("ok"):
        reasons.append("the Planner call is not recorded as ok")
    provider = agents_core.ROLE_PROVIDER["planner"]
    for field in ("requested", "used"):
        if call.get(field) != provider:
            reasons.append("provider %s=%r, this checkout sends %r"
                           % (field, call.get(field), provider))
    if record.get("planner_provider") != provider:
        reasons.append("planner_provider=%r, this checkout sends %r"
                       % (record.get("planner_provider"), provider))
    model = agents_core.model_for("planner")
    if call.get("model") != model:
        reasons.append("model=%r answered, this checkout asks for %r"
                       % (call.get("model"), model))
    for name, value in sorted(agents_core.sampling_for("planner").items()):
        if name not in call:
            reasons.append("sampling parameter %s=%r is not recorded in the "
                           "ledger, so it cannot be compared" % (name, value))
        elif call[name] != value:
            reasons.append("sampling %s=%r, this checkout sends %r"
                           % (name, call[name], value))
    return reasons

# ------------------------------------------------------------- the imported plan

# What `calibration_plan` returns. An imported plan carries exactly these keys and
# then its `imported_*` provenance; `test_pipeline` compares this tuple against
# what the real function returns, so the two cannot drift apart quietly.
DRAWN_PLAN_KEYS = ("task_id", "spec", "planner_provider", "steps",
                   "plan_sha256", "spec_sha256")


def imported_plan(record, source, ledger_sha256, at=None):
    """One plan record in `calibration_plan`'s shape, marked as imported.

    `spec_sha256` is recomputed with the expression `calibration_plan` uses, so it
    is the digest a drawn plan would have carried for these bytes. `steps` and
    `planner_provider` are copied rather than recomputed because the reply they
    were derived from is not in the ledger.

    The `imported_*` keys sort above `plan_sha256` and below `calls_by_provider`
    in the written file -- `run_eval.save_record` sorts keys -- so an imported
    spec is one glance from the top of the file, which is the point of them.
    """
    spec = record.get("spec") or ""
    calls = [call for call in (record.get("calls") or [])
             if call.get("role") == "planner"]
    return {
        "task_id": record.get("task_id"),
        "spec": spec,
        "planner_provider": record.get("planner_provider"),
        "steps": record.get("steps"),
        "spec_sha256": agents_core.sha256_of(spec),
        # Empty on purpose, and said in words in the field below it.
        "plan_sha256": "",
        # This root paid no provider call to obtain this spec. The call that did
        # is recorded whole, from the source ledger's own publication allow-list.
        "calls_by_provider": {},
        "imported_at": at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "imported_from": source,
        "imported_ledger_sha256": ledger_sha256,
        "imported_plan_sha256_absent": PLAN_SHA256_ABSENT,
        "imported_planner_call": dict(calls[0]) if calls else {},
    }


def existing_draws(root, seed, task_id, probe=DRAW_PROBE_MAX):
    """Draw files already on disk for `task_id`.

    Built by calling `calibrate.draw_path` rather than by re-spelling its name
    format, so a rename there cannot leave this looking for files nobody writes.
    """
    paths = (calibrate.draw_path(root, seed, task_id, index)
             for index in range(1, probe + 1))
    return [path for path in paths if os.path.exists(path)]

# ------------------------------------------------------------- the plan of work

def _source_name(ledger):
    """`imported_from` as a repo-relative path where that is meaningful.

    A path relative to the checkout stays true across machines; one that escapes
    the checkout is written absolute rather than as a string of `..`.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    relative = os.path.relpath(os.path.abspath(ledger), root)
    if relative.split(os.sep)[0] == os.pardir:
        return os.path.abspath(ledger)
    return relative


def convert(ledger, root, seed, tasks, probe=DRAW_PROBE_MAX, at=None):
    """Decide everything before writing anything. Returns the whole plan of work.

    `writes` are `(path, plan)` pairs, `skips` and `refusals` are
    `(task_id, reason)`. A single refusal is meant to abandon the run: these
    files are read as one spec store, and importing 18 of 19 while refusing the
    nineteenth would leave a store nobody can describe in a sentence.

    Tasks are walked in the locked order rather than the ledger's, so the same
    ledger always produces the same report, and a plan the ledger holds for a
    task the lock does not describe is a refusal -- the specs would be answering
    prompts this sweep will never send.
    """
    units, malformed = pin_executor.load_ledger(ledger)
    plans = pin_executor.replayable_plans(units)
    digest = file_sha256(ledger)
    source = _source_name(ledger)
    writes, skips, refusals = [], [], []
    # Task ids that hold a plan in the store once this run is done -- the ones
    # written plus the ones already there. The projection at the end is over
    # exactly these, so it answers "what will the sweep now pay for these tasks"
    # rather than "what would it pay for the whole grid".
    covered = []
    locked = set(task.task_id for task in tasks)
    for task_id in sorted(set(plans) - locked):
        refusals.append((task_id, "the ledger holds a plan for it and %s does "
                                  "not describe it"
                         % os.path.basename(gen_tasks.LOCK_PATH)))
    for task in tasks:
        record = plans.get(task.task_id)
        if record is None:
            skips.append((task.task_id, "no spec in the ledger -- planned and "
                                        "lost, or never reached"))
            continue
        problems = planner_call_parity(record)
        if problems:
            refusals.append((task.task_id, "the Planner call it was drawn from "
                                           "is not the call this sweep makes: "
                             + "; ".join(problems)))
            continue
        path = calibrate.plan_path(root, seed, task.task_id)
        if os.path.exists(path):
            skips.append((task.task_id, "%s already exists -- left alone; "
                                        "delete it to re-import"
                          % os.path.relpath(path, root)))
            covered.append(task.task_id)
            continue
        drawn = existing_draws(root, seed, task.task_id, probe=probe)
        if drawn:
            refusals.append((task.task_id, "%d draw(s) already on disk with no "
                             "plan beside them, so importing one now would give "
                             "this task two specs and one d_t. Compare their "
                             "spec_sha256 with %s and delete whichever is wrong."
                             % (len(drawn),
                                agents_core.sha256_of(record.get("spec") or ""))))
            continue
        writes.append((path, imported_plan(record, source, digest, at=at)))
        covered.append(task.task_id)
    return {"ledger": ledger, "ledger_sha256": digest, "source": source,
            "records": len(units), "malformed": malformed, "specs": len(plans),
            "writes": writes, "skips": skips, "refusals": refusals,
            "covered": covered}

# ------------------------------------------------------------------------- the CLI

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Import a pin probe's Planner specs into the calibration "
                    "spec store. Calls no provider.")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER,
                        help="source ledger, opened read-only (default: %s)"
                             % _source_name(DEFAULT_LEDGER))
    parser.add_argument("--out", default=calibrate.CALIBRATION_DIR,
                        help="calibration root, the same --out the sweep will "
                             "be given (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=0,
                        help="must match the seed the lock records; it is part "
                             "of the plan path (default: %(default)s)")
    parser.add_argument("--draws", type=int,
                        default=calibrate.CALIBRATION_DRAWS,
                        help="for the closing projection only -- nothing here "
                             "spends a call (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the same decisions and write nothing")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = os.path.abspath(args.out)
    results = os.path.abspath(run_eval.RESULTS_DIR)
    if root == results or root.startswith(results + os.sep):
        # `calibrate.main` refuses its own --out here for the same reason:
        # `run_eval.load_records` would read calibration files as grid cells. In
        # this sprint it is also where the source ledger lives, and the ledger is
        # evidence, not an output directory.
        print("refusing to import: --out is inside %s. Calibration is not grid "
              "data and must not be written where the grid's loader will find "
              "it." % run_eval.RESULTS_DIR, file=sys.stderr)
        return 2
    if not os.path.exists(args.ledger):
        print("refusing to import: no ledger at %s" % args.ledger,
              file=sys.stderr)
        return 2
    tasks = pin_executor.locked_tasks()
    locked_seed = gen_tasks.load_lock()["generator"]["seed"]
    if args.seed != locked_seed:
        # The specs answer the locked prompts, and the prompts are a function of
        # the seed. Writing them under seed-N would file them against prompts
        # they were never shown, and `calibrate --seed N` would refuse the task
        # set anyway.
        print("refusing to import: --seed %d, but %s records seed %d and the "
              "specs answer that seed's prompts."
              % (args.seed, os.path.basename(gen_tasks.LOCK_PATH), locked_seed),
              file=sys.stderr)
        return 2
    plan = convert(args.ledger, root, args.seed, tasks)
    print("source: %s" % plan["source"])
    print("  sha256 %s" % plan["ledger_sha256"])
    print("  %d complete record(s), %d malformed, %d plan(s) with a spec"
          % (plan["records"], plan["malformed"], plan["specs"]))
    print("target: %s" % root)
    if plan["refusals"]:
        print("\nrefusing to import, and writing nothing at all:",
              file=sys.stderr)
        for task_id, reason in plan["refusals"]:
            print("  REFUSED %-28s %s" % (task_id, reason), file=sys.stderr)
        print("%d of %d spec(s) could not be imported soundly. The store is one "
              "object: a partial import would leave it in a state nobody can "
              "describe." % (len(plan["refusals"]), plan["specs"]),
              file=sys.stderr)
        return 2
    for task_id, reason in plan["skips"]:
        print("  skip    %-28s %s" % (task_id, reason))
    for path, record in plan["writes"]:
        if not args.dry_run:
            run_eval.save_record(path, record)
        print("  %s %-28s %s"
              % ("would  " if args.dry_run else "import ", record["task_id"],
                 os.path.relpath(path, root)))
    print("\n%d plan(s) %s, %d skipped, %d task(s) now covered by the store"
          % (len(plan["writes"]),
             "would be written" if args.dry_run else "written",
             len(plan["skips"]), len(plan["covered"])))
    covered = set(plan["covered"])
    tasks_covered = [task for task in tasks if task.task_id in covered]
    if tasks_covered:
        calibrate.print_projection(
            calibrate.project_calls(tasks_covered, root, args.seed, args.draws),
            tasks_covered, args.draws)
        if args.dry_run:
            print("  --dry-run: nothing was written, so the planner call(s) "
                  "above are still counted as needed.")
    return 0


def cli(argv=None):
    raise SystemExit(main(argv))


if __name__ == "__main__":
    cli()

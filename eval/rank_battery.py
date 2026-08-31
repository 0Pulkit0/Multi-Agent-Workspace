#!/usr/bin/env python3
"""Is the ranking key order-invariant over the frozen suites? Measured, not assumed.

Two keys, measured side by side on ONE lock, from the same runs:

  `depth`   the pre-Sprint-9 key -- rank a losing candidate by
            `failed_assertion_line`, deeper being better. Frozen as a local copy
            in this file, because `agents_core` no longer has it. This is the
            baseline, and it is only a baseline if it is measured against the
            same task set as the thing it is compared to.
  `checks`  the shipped key -- `agents_core._candidate_rank`, which ranks by how
            many of the suite's checks the candidate passed.

Measuring both here rather than comparing against a stored number is the whole
point. Sprint 9 changed the ranking key AND re-locked the suites in the same
sprint, so a stored figure from before the re-lock cannot separate the two: the
suites moved under it. Both columns below come from the same suites, the same
permutations and the same candidate runs, so a difference between them is the key
and nothing else.

Per key, over all 36 locked tasks, with candidates that are real wrong
implementations from this tree rather than fixtures:

  * does a candidate's *outcome* change when the suite's checks are reordered --
    reported decomposed, because `verdict` changing and `failure_kind` changing
    are very different claims and one number for both invites the larger reading
  * does the *relative order* of two candidates ever reverse -- and, the
    operational form of the same question, does the candidate `_retain_best`
    would actually keep ever change

The reordering is the honest kind. Only top-level statements that can fail the
suite on their own are moved, each is moved as its verbatim source text, and under
the identity permutation the re-emitted suite is byte-identical to the frozen one
-- asserted in `permute`, not hoped for. So a changed outcome is the suite being
order-dependent and not this module mangling it.

Every result carries the digest of the lock it was measured against, and that
stamp is a *guard*, not a souvenir: a run refuses to start when the working tree
disagrees with the lock, and `--verify-stamp` fails when a recorded result was
measured against a different lock than the one on disk. A stale measurement that
reads as current is how a superseded number gets cited as a baseline.

Nothing here changes the ranking key. What to do about a non-zero fraction -- a
registered caveat, a different ranking element, a restricted claim -- is a
registration decision, so this writes a measurement and stops.

    python3 eval/rank_battery.py                  measure and print
    python3 eval/rank_battery.py --write          ... and record it for D2
    python3 eval/rank_battery.py --neighbours     ... plus hash seed and a re-roll
    python3 eval/rank_battery.py --verify-stamp   is the recorded result current?

No API calls, no network, no subprocess for the measurement itself: the suites run
in-process through `gen_tasks.run_suite`.
"""

import argparse
import ast
import io
import itertools
import json
import os
import random
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import agents_core            # noqa: E402
import harness                # noqa: E402
import gen_tasks              # noqa: E402

RESULT_PATH = os.path.join(_HERE, "ranking_battery.json")
RESULT_VERSION = 2

# `eval/prereg/` is read-only, so the measurement D2 will cite lives here instead,
# beside `tasks.lock` and committed for the same reason: it is a claim about a
# frozen task set, and it records which task set by that set's own body digest. A
# result measured against a different lock is not evidence about this one.

# The permutable unit is "a top-level statement that can fail the suite by itself".
# Two modes, and both are reported:
#
#   asserts  checks whose body is a plain `assert`. The brief's literal ask.
#   checks   those plus the `try/except/else: raise AssertionError` blocks, which
#            is how a suite with no pytest spells "this input must raise". Those
#            blocks matter to the question and not reporting them would answer it
#            wrongly by omission: the `raise AssertionError` is a frame in the
#            suite file, so `harness._classify` gives it `FAIL_ASSERTION` and a
#            `failed_assertion_line` exactly as it does a plain assert -- and in
#            the 20 suites that have them, they sit at the *deepest* lines, which
#            is precisely where the depth heuristic is doing its work.
MODE_ASSERTS = "asserts"
MODE_CHECKS = "checks"
MODES = (MODE_ASSERTS, MODE_CHECKS)

# The two ranking keys, measured from the same runs. `depth` is a frozen copy of
# what `agents_core` shipped before Sprint 9 and `checks` is what it ships now;
# see `_depth_rank` for why the copy is here rather than imported.
KEY_DEPTH = "depth"
KEY_CHECKS = "checks"
KEYS = (KEY_DEPTH, KEY_CHECKS)
SHIPPED_KEY = KEY_CHECKS

# The identity order, plus the full reversal -- the most aggressive single
# reordering there is -- plus sampled permutations. The reversal is always
# included rather than sampled, because a suite that is order-dependent at all is
# most likely to show it when every check moves.
#
# 30 and not 12, and the number is measured rather than chosen: at 12 the seed
# re-roll neighbour disagreed with itself on two suites, which is a sample too thin
# to report a fraction from. Every metric is identical at 30, 60 and 120, so this is
# where the ladder flattens. `--neighbours` re-runs the ladder and records it, so
# the claim "saturated" stays checkable instead of becoming folklore.
PERMUTATIONS = 30
SATURATION_LADDER = (12, 30, 60)

# In-process names for what `harness` reports from a child's stderr. The mapping is
# `harness`'s, not a parallel one: `_outcome` below builds the same three fields
# `_candidate_rank` reads, so the ranks compared here are the ranks the pipeline
# would compute.
SUITE_FILE = "<test_solution>"
SOLUTION_FILE = "<solution>"

# --------------------------------------------------------------- one outcome

def _deepest(traceback_object):
    """`(filename, lineno, reached_suite)` for a traceback's innermost frame.

    The deepest frame and not the outermost, mirroring `harness._classify`'s
    `frames[-1]`: if the *solution* has a module-level assert of its own, an
    AssertionError is raised but the deepest frame is `<solution>`, which is a
    broken module rather than a wrong answer. Conflating the two would rank a
    candidate that never loaded above one that ran the suite.

    `reached_suite` says whether any suite frame appears anywhere in the chain,
    which is how "died being imported" is told from "ran and then raised" without
    the string matching `harness` has to do on a child's stderr.
    """
    tail, reached = traceback_object, False
    while True:
        reached = reached or tail.tb_frame.f_code.co_filename == SUITE_FILE
        if tail.tb_next is None:
            break
        tail = tail.tb_next
    return tail.tb_frame.f_code.co_filename, tail.tb_lineno, reached


def outcome(solution_source, tests, budget=gen_tasks.RUNAWAY_BUDGET):
    """The fields `_candidate_rank` reads, derived in process.

    `verdict`, `failure_kind`, `failed_assertion_line` and the per-check tally,
    from running the suite rather than from parsing a child's stderr. The
    classification is a mirror of `harness.py`'s assertion branch and deliberately
    a narrow one -- deepest frame in the suite file, and the exception itself an
    `AssertionError`. `harness` has to ask whether the string "AssertionError"
    appears in stderr because that is all it has; here the exception object is in
    hand, which is if anything stricter, and never looser.

    The tally comes from `harness.read_checks` over the suite's own stdout -- the
    same reader the pipeline uses, not a second parse of the same lines. The buffer
    is owned here and read after the exception, because the interesting cases are
    exactly the ones where the suite raises: `_mawreport` writes the report and
    *then* re-raises the first failing check.

    Everything that is not an assertion failure collapses to one rank element in
    `_candidate_rank` -- import error, runtime error, timeout alike -- so the
    labels below are for the record, not for the ranking.
    """
    printed = io.StringIO()
    try:
        gen_tasks._run_traced(solution_source, tests, budget, out=printed)
    except gen_tasks._Runaway as exc:
        return _with_checks({"verdict": harness.VERDICT_REVISE,
                             "failure_kind": harness.FAIL_TIMEOUT,
                             "failed_assertion_line": None,
                             "detail": str(exc)}, printed)
    except BaseException as exc:
        where, line, reached = _deepest(sys.exc_info()[2])
        if isinstance(exc, AssertionError) and where == SUITE_FILE:
            return _with_checks({"verdict": harness.VERDICT_REVISE,
                                 "failure_kind": harness.FAIL_ASSERTION,
                                 "failed_assertion_line": line,
                                 "detail": type(exc).__name__}, printed)
        return _with_checks(
            {"verdict": harness.VERDICT_REVISE,
             "failure_kind": (harness.FAIL_RUNTIME if reached
                              else harness.FAIL_IMPORT),
             "failed_assertion_line": None,
             "detail": "%s at %s:%d" % (type(exc).__name__, where, line)},
            printed)
    return _with_checks({"verdict": harness.VERDICT_APPROVED,
                         "failure_kind": harness.FAIL_NONE,
                         "failed_assertion_line": None, "detail": ""}, printed)


def _with_checks(result, printed):
    """`result` plus the per-check tally `harness` would read off that stdout.

    On every return path including the approved one, and via `harness.read_checks`
    rather than a local parse: the count this battery ranks on has to be the count
    the pipeline would rank on, or the measurement is about this file.
    """
    passed, total, kinds, note = harness.read_checks(printed.getvalue())
    result["checks_passed"] = passed
    result["checks_total"] = total
    result["checks_kinds"] = kinds
    result["checks_note"] = note
    result["checks_trusted"] = bool(total) and not note
    return result


# ------------------------------------------------------- reordering a suite

def _is_raise_check(node):
    """A `try/except/else: raise AssertionError(...)` -- an assertion.

    The generated suites cannot use pytest, so "this input must raise" is spelled
    as a try block whose `else` raises. Recognised structurally rather than by
    text, and required to actually raise in the `else` clause: a bare `try` that
    swallows an exception is not a check and moving it could change behaviour.
    """
    if not isinstance(node, ast.Try) or not node.orelse:
        return False
    return any(isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call)
               and isinstance(stmt.exc.func, ast.Name)
               and stmt.exc.func.id == "AssertionError"
               for stmt in node.orelse)


def _reports_check(node, name):
    """Does `node` consist of exactly one call to `name(<int>)`? The id, or `None`."""
    if len(node) != 1 or not isinstance(node[0], ast.Expr):
        return None
    call = node[0].value
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            and call.func.id == name and len(call.args) == 1
            and not call.keywords):
        return None
    argument = call.args[0]
    # `ast.Num` on 3.9 for a literal int; `ast.Constant` is what it is really is.
    value = getattr(argument, "value", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _check_block(node):
    """The per-check wrapper `gen_tasks._bake` emits: `(id, inner statement)`.

    A generated suite now carries each check inside its own `try`, so that a
    failing check is *recorded* rather than ending the suite:

        try:
            <the check>
        except Exception:
            _mawfail(3)
        else:
            _mawpass(3)

    That wrapper is the permutable unit now -- it is the top-level statement that
    can fail the suite by itself, and it carries its own check id with it, so
    moving it moves the reported id too and the report stays about the same checks.

    Recognised structurally and completely: one `except Exception` handler whose
    body is exactly `_mawfail(i)`, an `orelse` of exactly `_mawpass(i)` with the
    *same* `i`, no `finally`, and exactly one statement inside -- and that
    statement itself has to be a check, either a plain `assert` or a raise-check.
    Anything looser would match a hand-written try block and reorder something
    whose position matters. Returns `None` when `node` is not one of these.
    """
    if not isinstance(node, ast.Try) or node.finalbody or len(node.body) != 1:
        return None
    if len(node.handlers) != 1:
        return None
    handler = node.handlers[0]
    if handler.name is not None or not isinstance(handler.type, ast.Name):
        return None
    if handler.type.id != "Exception":
        return None
    failed = _reports_check(handler.body, "_mawfail")
    passed = _reports_check(node.orelse, "_mawpass")
    if failed is None or failed != passed:
        return None
    inner = node.body[0]
    if not (isinstance(inner, ast.Assert) or _is_raise_check(inner)):
        return None
    return failed, inner


def _chosen(tests, mode):
    """`(top-level nodes, the permutable wrappers, their check ids)` for one mode.

    One place that decides what a permutable unit is, because `units`, `isolate`
    and `check_ids` all have to agree about it exactly, and three copies of the
    predicate is three chances to disagree.
    """
    body = ast.parse(tests).body
    keep = [(node, found) for node, found in
            ((node, _check_block(node)) for node in body)
            if found is not None
            and (mode == MODE_CHECKS or isinstance(found[1], ast.Assert))]
    return (body, [node for node, _ in keep],
            [found[0] for _, found in keep])


def units(tests, mode):
    """`(prefix, [unit source, ...], [gap, ...], suffix)` for one suite.

    A unit is one permutable check's verbatim source lines. A gap is whatever sits
    between two consecutive units -- blank lines, comments -- and gaps stay at
    their *positions* rather than travelling with a unit, so the layout is
    preserved while the checks move. The prefix is everything before the first
    unit and the suffix everything after the last, both fixed: the `from solution
    import ...` line and the reporting helpers have to stay first or nothing runs,
    `_mawreport()` has to stay last or nothing is reported, and hoisting an assert
    above an assignment it reads would be a broken permutation rather than an
    order-dependent suite.

    The two modes differ in which wrappers move, and they mean what they meant
    before the wrappers existed: `asserts` takes the ones whose single inner
    statement is a plain `assert`, `checks` also takes the ones wrapping a
    `try/except/else: raise AssertionError` -- the no-pytest spelling of "this must
    raise".

    Returns `None` when the suite has fewer than two permutable units, which is
    not a defect -- there is simply nothing to reorder.
    """
    body, chosen, _ids = _chosen(tests, mode)
    if len(chosen) < 2:
        return None
    # A contiguous run, ignoring blank and comment lines: anything else means an
    # unpermutable statement sits between two checks, and this module refuses to
    # reorder across it rather than guessing whether that is safe.
    lines = tests.splitlines(True)
    first, last = body.index(chosen[0]), body.index(chosen[-1])
    if body[first:last + 1] != chosen:
        return None
    text = lambda node: "".join(lines[node.lineno - 1:node.end_lineno])
    gaps = ["".join(lines[a.end_lineno:b.lineno - 1])
            for a, b in zip(chosen, chosen[1:])]
    return ("".join(lines[:chosen[0].lineno - 1]),
            [text(node) for node in chosen], gaps,
            "".join(lines[chosen[-1].end_lineno:]))


def check_ids(tests, mode):
    """The suite's own check ids for the permutable units, in frozen order.

    What ties a permuted run's report back to a check: the report is keyed by the
    id `_bake` baked in, and `units` indexes by position. Reordering moves the
    ids with their checks, so this mapping is fixed by the frozen suite.
    """
    return _chosen(tests, mode)[2]

def _lines_in(chunk):
    """Source lines in a text chunk, counting an unterminated last line.

    `count("\\n")` alone is wrong for the final chunk of a file that does not end
    in a newline, and getting it wrong would shift every line number after it --
    silently, and in a module whose whole output is line numbers.
    """
    if not chunk:
        return 0
    return chunk.count("\n") + (0 if chunk.endswith("\n") else 1)


def render(tests, mode, order):
    """`(suite, line -> original unit index)` with the checks in `order`.

    The map is what separates the two questions this battery has to keep apart. A
    failing *line* moves whenever the check on it moves, which is arithmetic and
    tells you nothing. A failing *check* moving is the suite being genuinely
    order-dependent: the same candidate now fails somewhere else because of what
    ran before it. Without the map only the first is observable, and the report
    would call every suite order-dependent.

    Identity re-emission is byte-identical to the frozen suite, and that is
    asserted rather than assumed: if re-emission could perturb the text on its own,
    every number here would be measuring this function instead.
    """
    parts = units(tests, mode)
    if parts is None:
        return None, {}
    prefix, texts, gaps, suffix = parts
    if sorted(order) != list(range(len(texts))):
        raise ValueError("order is not a permutation of %d units" % len(texts))
    out, mapping = [prefix], {}
    line = _lines_in(prefix) + 1
    for position, index in enumerate(order):
        out.append(texts[index])
        for offset in range(_lines_in(texts[index])):
            mapping[line + offset] = index
        line += _lines_in(texts[index])
        if position < len(gaps):
            out.append(gaps[position])
            line += _lines_in(gaps[position])
    out.append(suffix)
    rendered = "".join(out)
    if list(order) == sorted(order) and rendered != tests:
        raise AssertionError(
            "identity permutation did not reproduce the suite; re-emission is "
            "lossy and no number from this battery would mean anything")
    return rendered, mapping


def permute(tests, mode, order):
    """`render`'s suite text on its own."""
    return render(tests, mode, order)[0]



def orders(count, seed, samples=PERMUTATIONS):
    """`(name, order)` pairs: identity, the full reversal, then sampled shuffles.

    The reversal is always present rather than left to sampling. It is the single
    most aggressive reordering available, so a suite that is order-dependent at all
    is likeliest to show it there, and leaving it to chance would make the answer
    depend on the seed in exactly the way the seed re-roll neighbour exists to rule
    out. Sampling is seeded and duplicate orders are dropped, so the set is a
    function of `(count, seed, samples)` and nothing else.
    """
    identity = tuple(range(count))
    picked, out = {identity: "identity"}, [("identity", identity)]
    reversal = tuple(reversed(identity))
    if reversal not in picked:
        picked[reversal] = "reversed"
        out.append(("reversed", reversal))
    rng = random.Random("rank-battery/%d/%d" % (count, seed))
    attempts = 0
    while len(out) < samples and attempts < samples * 20:
        attempts += 1
        shuffled = list(identity)
        rng.shuffle(shuffled)
        shuffled = tuple(shuffled)
        if shuffled in picked:
            continue
        picked[shuffled] = "shuffle-%d" % (len(out) - 1)
        out.append((picked[shuffled], shuffled))
    return out

# ------------------------------------------------------------- the candidates

def candidates_for(task):
    """`(name, source)` per candidate: the reference, then every applicable mutant.

    Real candidates, not fixtures. The reference is what an APPROVED step looks
    like and every other entry is a wrong implementation this tree can actually
    produce -- `gen_tasks.BATTERY`, the same closure `run_eval.StubModel._wrong`
    draws from -- so a rank comparison here is a comparison the pipeline could
    genuinely be asked to make. Mutants whose edit site the reference does not
    contain are absent because they do not exist, not because they were
    inconvenient; `unreachable` and `equivalent` mutants are kept, since those are
    claims about what a *suite* can catch and this is not asking that question.
    """
    out = [("reference", task.reference)]
    for mutation in gen_tasks.BATTERY:
        if mutation == gen_tasks.VACUOUS_STUB:
            out.append((mutation, gen_tasks._stub(task.names)))
            continue
        source, _lines = gen_tasks._mutate(task.reference, mutation)
        if source is not None:
            out.append((mutation, source))
    return out


def _tail(tests, mode):
    """The suite's trailing machinery, with any other check removed.

    Isolation used to be prefix-plus-one-check, and that was right when a suite
    stopped at its first failing assert: the failure raised where it happened.
    It is wrong now. `_mawreport()` sits after the last check and it is what turns
    a *recorded* failure into a raised one, so dropping the whole suffix left an
    isolated failing check recording its failure, reporting nothing, and exiting
    zero -- every permutable check "passing alone" while the suite failed, which
    `_disagrees` is entitled to read as order-dependence. Measured on
    `aggregation-01`: the vacuous stub, which fails all eleven checks in every
    order, came back flagged.

    So the suffix is kept minus any check wrapper in it. The reporting call is
    machinery and has to run; the other checks are the whole point of isolating.
    """
    body, chosen, _ids = _chosen(tests, mode)
    lines = tests.splitlines(True)
    after = body[body.index(chosen[-1]) + 1:]
    return "".join("".join(lines[node.lineno - 1:node.end_lineno])
                   for node in after if _check_block(node) is None)


def isolate(tests, mode, index):
    """The suite reduced to its prefix, one check, and the reporting call.

    How "does this check's result depend on what ran before it" gets asked without
    guessing. Line numbers are meaningless in an isolated run and are not read from
    it; only whether the check passes, and how it fails if it does not. The report
    it prints is deliberately *not* trusted by `harness.read_checks` -- one check
    numbered 7 is not a coherent 1..1 report -- and nothing here reads the tally.
    """
    parts = units(tests, mode)
    if parts is None:
        return None
    prefix, texts, _gaps, _suffix = parts
    return prefix + texts[index] + _tail(tests, mode)


def isolated_outcomes(task, mode, sources, names):
    """`name -> [(verdict, failure_kind), ...]` per check, each check run alone.

    This is the load-bearing measurement, and the reason a naive version of this
    battery would have reported every suite as order-dependent. Reordering changes
    which of a candidate's *several* failing checks fires first, and that is
    arithmetic, not order-dependence: a wrong implementation typically fails many
    checks and only the earliest is ever observed. What makes a suite genuinely
    order-dependent is a check whose own result differs from what it is in
    isolation -- state left behind by an earlier check, a module-level cache, an
    import side effect. So each check is run on its own, and the in-suite failure is
    then compared against the first isolated failure in the order that ran.
    """
    parts = units(task.tests, mode)
    out = {}
    for name in names:
        row = []
        for index in range(len(parts[1])):
            result = outcome(sources[name], isolate(task.tests, mode, index))
            row.append((result["verdict"], result["failure_kind"]))
        out[name] = row
    return out


def _predicted(order, row):
    """What the isolated results predict the whole suite will report, in `order`.

    `(unit index, failure kind)`, shaped exactly like the observation it is compared
    against, so the comparison is one equality and not a pile of special cases. Two
    of those cases are worth naming because getting either wrong invents
    order-dependence that is not there:

      * a check that fails alone with something other than an assertion -- a
        runaway, an import error -- produces no assertion line, so the predicted
        unit is `None` even though a specific check is to blame.
      * `(None, None)` means every *permutable* check passes alone. The suite may
        still fail, in the fixed prefix or suffix: in `asserts` mode the raise-checks
        are suffix, and a mutant that only they catch fails a suite whose every
        permutable check passes. That is the suffix doing its job, not order.
    """
    for index in order:
        verdict, kind = row[index]
        if verdict != harness.VERDICT_APPROVED:
            return (index if kind == harness.FAIL_ASSERTION else None), kind
    return None, None


def _ceiling(result):
    """Is this outcome a whole-suite resource ceiling rather than a check's result?

    `gen_tasks._run_traced` counts traced lines and aborts the whole suite at
    `RUNAWAY_BUDGET`. That is a property of the run and not of any check: every
    check can be individually cheap while the twenty-eight of them together cross
    the ceiling. So isolation cannot predict it, and `_disagrees` must not read the
    gap as order-dependence -- measured on `expression-eval-01/02` and
    `template-expansion-01/02`, where `raise_to_pass` times out in *every* order
    (`verdict_changed`, `kind_changed` and `checks_changed` all empty for those
    suites) and was nevertheless flagged.
    """
    return harness.is_timeout(result["failure_kind"])


def _disagrees(order, row, result):
    """Does the suite report something its own checks, run alone, do not predict?"""
    if _ceiling(result):
        return False
    predicted = _predicted(order, row)
    if predicted == (None, None):
        return result["unit"] is not None
    return (result["unit"], result["failure_kind"]) != predicted



def _sign(left, right):
    return (left > right) - (left < right)


def _pairs(names, ranks):
    """Every ordered-by-name pair with its sign under one ranking key."""
    return dict(((a, b), _sign(ranks[a], ranks[b]))
                for a, b in itertools.combinations(names, 2))


# ------------------------------------------------------------ the two keys

def _depth_rank(candidate):
    """The pre-Sprint-9 ranking key, verbatim. The baseline, frozen here.

    A local copy and not an import, because `agents_core` does not have this
    function any more -- Sprint 9 replaced its third element with a count of
    checks passed. Comparing the new key against a *stored* number from before
    the change cannot separate the key from the task set, because the same sprint
    re-locked the suites; running both keys here over the same suites can.

    Copied and not paraphrased: element for element what `agents_core` shipped at
    commit b78cff5, including the `-round` tie-break and the `-1` floor for a
    candidate with no assertion line. If this drifts from that commit it stops
    being a baseline, so `test_pipeline` pins it against the recorded key.
    """
    line = candidate.get("failed_assertion_line")
    return (
        1 if candidate.get("verdict") == harness.VERDICT_APPROVED else 0,
        1 if candidate.get("failure_kind") == harness.FAIL_ASSERTION else 0,
        line if isinstance(line, int) else -1,
        -candidate.get("round", 0),
    )


def _depth_merit(candidate):
    """`_depth_rank` without the round tie-break, mirroring the shipped pairing."""
    return _depth_rank(candidate)[:3]


# The shipped key is imported and the baseline is local, which is the only
# arrangement that makes the comparison mean anything: the `checks` column has to
# be whatever `agents_core` actually ranks with today, or the battery is measuring
# a copy of it.
RANKING = {
    KEY_DEPTH: {
        "rank": _depth_rank,
        "merit": _depth_merit,
        "source": "eval/rank_battery.py::_depth_rank -- frozen copy of the "
                  "pre-Sprint-9 agents_core._candidate_rank (commit b78cff5)",
        "third_element": "failed_assertion_line, deeper better",
    },
    KEY_CHECKS: {
        "rank": lambda candidate: agents_core._candidate_rank(candidate),
        "merit": lambda candidate: agents_core._candidate_merit(candidate),
        "source": "agents_core._candidate_rank -- the shipped key",
        "third_element": "checks passed, more better; unknown below a measured 0",
    },
}


def _condense(state, baseline_keep, baseline_kept_signature):
    """One ranking key's per-task detail, as distinct facts rather than one entry per event.

    Thirty permutations times a dozen candidate pairs is a few thousand rows a task,
    and the recorded file is meant to be read and cited. The distinct pairs that
    reverse, and the count of orders that did it, carry everything a reader needs;
    the per-event list carried the same thing several hundred times over.
    """
    pairs = lambda events: sorted(set("%s|%s" % tuple(e["pair"])
                                      for e in events))
    return {
        "merit_flips": pairs(state["merit_flips"]),
        "rank_flips": pairs(state["rank_flips"]),
        "tie_changes": pairs(state["tie_changes"]),
        "retained_under_identity": baseline_keep,
        "retained_outcome_under_identity": (list(baseline_kept_signature)
                                           if baseline_kept_signature else None),
        "retention_changes": sorted(set(e["keeps"] for e in state["keep_changes"])),
        "retained_outcome_changes": sorted(
            set("%s->%s" % (e["was"], e["now"])
                for e in state["kept_outcome_changes"])),
        "orders_changing_retention": len(
            set(e["order"] for e in state["keep_changes"])),
        "orders_changing_retained_outcome": len(
            set(e["order"] for e in state["kept_outcome_changes"])),
        "orders_flipping_rank": len(set(e["order"] for e in state["rank_flips"])),
    }


def _fresh_state():
    return {"merit": None, "rank": None, "merit_flips": [], "rank_flips": [],
            "tie_changes": [], "keep_changes": [], "kept_outcome_changes": []}


def measure_task(task, mode, seed, samples=PERMUTATIONS):
    """One task's record: outcomes, rank signs, and what moved under reordering.

    Both ranking keys come out of *one* pass over the permutations, ranked off the
    same candidate runs. That is not an optimisation: running them separately would
    let the two columns differ for a reason other than the key -- a resampled
    permutation, a re-rolled candidate -- and the whole question is what the key
    changes.

    Rounds are assigned by candidate index and held fixed across permutations, so
    the round tie-break cannot manufacture a flip -- it can only decide a tie the
    way `_retain_best` decides it in a real run. `code` is set to the candidate's
    own source, because the shipped key's last element is a digest of it and a
    battery that left it unset would report ties the pipeline does not have.
    """
    parts = units(task.tests, mode)
    if parts is None:
        return {"task_id": task.task_id, "family": task.family, "tier": task.tier,
                "units": 0, "permuted": 0, "skipped": "fewer than two "
                "permutable checks, or an unpermutable statement between them"}
    names = [name for name, _ in candidates_for(task)]
    sources = dict(candidates_for(task))
    rounds = dict((name, index + 1) for index, name in enumerate(names))
    solo = isolated_outcomes(task, mode, sources, names)
    plan = orders(len(parts[1]), seed, samples)
    seen, depths, blamed, tallies = {}, {}, {}, {}
    baseline_keep = dict((key, None) for key in KEYS)
    baseline_kept_signature = dict((key, None) for key in KEYS)
    state = dict((key, _fresh_state()) for key in KEYS)
    losers, first = [], True
    changed, verdict_changed, kind_changed = [], [], []
    disagreed, checks_changed, ceilinged = [], [], []
    for label, order in plan:
        suite, at_line = render(task.tests, mode, order)
        found = {}
        for name in names:
            result = outcome(sources[name], suite)
            result["round"] = rounds[name]
            result["code"] = sources[name]
            # Which check failed, not which line: the line moves whenever the
            # check moves, so only the check's own identity says anything.
            result["unit"] = at_line.get(result["failed_assertion_line"])
            found[name] = result
            if _ceiling(result) and name not in ceilinged:
                # Counted, not hidden. Excluding these from `order_dependent` is
                # only honest if the exclusion is visible, so the suites whose
                # order-dependence could not be tested this way are named.
                ceilinged.append(name)
            if _disagrees(order, solo[name], result) \
                    and name not in disagreed:
                # The suite reports something its own checks, run one at a time,
                # do not predict, so some check's result depends on what ran
                # before it. That is the order-dependence the depth heuristic's
                # premise rules out, and it is recorded per candidate because a
                # suite can be order-dependent for one implementation and not
                # another.
                disagreed.append(name)
        signature = dict(
            (name, (found[name]["verdict"], found[name]["failure_kind"]))
            for name in names)
        # The tally as a whole, trust flag included: a count that stops being
        # trustworthy is a changed measurement even when the number is the same.
        tally = dict((name, (found[name]["checks_trusted"],
                             found[name]["checks_passed"],
                             found[name]["checks_total"])) for name in names)
        ranked = dict((key, dict((name, RANKING[key]["rank"](found[name]))
                                 for name in names)) for key in KEYS)
        merited = dict((key, dict((name, RANKING[key]["merit"](found[name]))
                                  for name in names)) for key in KEYS)
        if first:
            # The identity order, which is the frozen suite as it stands, so this
            # is the baseline every permutation is compared against rather than a
            # separate notion of "correct".
            #
            # Retention is scored over the *failing* candidates only, because that
            # is the only decision `_retain_best` is ever asked to make: a run
            # stops the moment a candidate is APPROVED, so a set containing the
            # reference would trivially retain it under every permutation and the
            # number would mean nothing.
            first = False
            losers = [name for name in names
                      if found[name]["verdict"] != harness.VERDICT_APPROVED]
            seen, tallies = signature, tally
            depths = dict((name, found[name]["failed_assertion_line"])
                          for name in names)
            blamed = dict((name, found[name]["unit"]) for name in names)
            for key in KEYS:
                state[key]["merit"] = _pairs(names, merited[key])
                state[key]["rank"] = _pairs(names, ranked[key])
                keep = (max(losers, key=lambda name: ranked[key][name])
                        if losers else None)
                baseline_keep[key] = keep
                baseline_kept_signature[key] = signature.get(keep)
            continue
        for name in names:
            if signature[name] != seen[name]:
                if name not in changed:
                    changed.append(name)
                # Decomposed, because one number for both invites the larger
                # reading. A moved `verdict` is the suite passing a candidate it
                # failed; a moved `failure_kind` with the same verdict is a
                # different check failing first, which is the same arithmetic
                # that moves a line number.
                bucket = (verdict_changed if signature[name][0] != seen[name][0]
                          else kind_changed)
                if name not in bucket:
                    bucket.append(name)
            if tally[name] != tallies[name] and name not in checks_changed:
                checks_changed.append(name)
        for key in KEYS:
            here_merit = _pairs(names, merited[key])
            here_rank = _pairs(names, ranked[key])
            for pair, sign in state[key]["merit"].items():
                if sign and here_merit[pair] and sign != here_merit[pair]:
                    state[key]["merit_flips"].append({"pair": list(pair),
                                                      "order": label})
                elif bool(sign) != bool(here_merit[pair]):
                    state[key]["tie_changes"].append({"pair": list(pair),
                                                      "order": label})
            for pair, sign in state[key]["rank"].items():
                if sign != here_rank[pair]:
                    state[key]["rank_flips"].append({"pair": list(pair),
                                                     "order": label})
            keep = (max(losers, key=lambda name: ranked[key][name])
                    if losers else None)
            if keep != baseline_keep[key]:
                state[key]["keep_changes"].append(
                    {"order": label, "keeps": keep,
                     "instead_of": baseline_keep[key]})
            # And the question the headline `outcome_changed` does *not* answer:
            # did the graded outcome of the candidate actually retained move. It
            # can move either because another candidate is kept or because the
            # same one is now graded differently, and both belong here.
            if keep is not None \
                    and signature[keep] != baseline_kept_signature[key]:
                state[key]["kept_outcome_changes"].append(
                    {"order": label,
                     "was": "/".join(baseline_kept_signature[key] or ("?", "?")),
                     "now": "/".join(signature[keep])})
    return {
        "task_id": task.task_id, "family": task.family, "tier": task.tier,
        "units": len(parts[1]), "permuted": len(plan) - 1,
        "check_ids": check_ids(task.tests, mode),
        "candidates": names, "losers": losers, "depths": depths,
        "blamed_units": blamed,
        "kinds": dict((name, seen[name][1]) for name in names),
        "checks_under_identity": dict((name, list(tallies[name]))
                                      for name in names),
        "outcome_changed": changed,
        "verdict_changed": verdict_changed,
        "kind_changed": kind_changed,
        "checks_changed": checks_changed,
        "order_dependent": disagreed,
        "ceiling_untestable": ceilinged,
        "keys": dict((key, _condense(state[key], baseline_keep[key],
                                     baseline_kept_signature[key]))
                     for key in KEYS),
    }


# ------------------------------------------------------------------ the totals

def _fraction(hit, total):
    return {"suites": hit, "of": total,
            "fraction": 0.0 if not total else round(float(hit) / total, 4)}


# `(reported name, record field)`. Split because the record holds the *events*
# ("which pairs flipped") and the totals report the *fact* ("this suite flipped").
SHARED_TOTALS = (("outcome_changed", "outcome_changed"),
                 ("verdict_changed", "verdict_changed"),
                 ("kind_changed", "kind_changed"),
                 ("checks_changed", "checks_changed"),
                 ("order_dependent", "order_dependent"),
                 ("ceiling_untestable", "ceiling_untestable"))
KEY_TOTALS = (("merit_flipped", "merit_flips"),
              ("rank_flipped", "rank_flips"),
              ("tie_changed", "tie_changes"),
              ("retention_changed", "retention_changes"),
              ("retained_outcome_changed", "retained_outcome_changes"))


def totals_for(records):
    """The fractions, and the named suites behind each one.

    A fraction with no names beside it is not reportable: the brief asks for the
    measured fraction *and* the list of suites where it fails, because "3 of 36"
    is not actionable and "3 of 36: these three" is.

    Key-independent facts sit at the top and the per-key ones under `keys`, which
    is what makes the two columns comparable: the suites, the permutations and the
    candidate outcomes behind both keys are literally the same measurement.
    """
    permuted = [r for r in records if r.get("permuted")]
    out = {"suites_measured": len(permuted),
           "suites_skipped": sorted(r["task_id"] for r in records
                                    if not r.get("permuted")),
           "keys": {}}
    for name, field in SHARED_TOTALS:
        hit = sorted(r["task_id"] for r in permuted if r.get(field))
        out[name] = _fraction(len(hit), len(permuted))
        out[name + "_suites"] = hit
    for key in KEYS:
        block = {}
        for name, field in KEY_TOTALS:
            hit = sorted(r["task_id"] for r in permuted
                         if (r.get("keys") or {}).get(key, {}).get(field))
            block[name] = _fraction(len(hit), len(permuted))
            block[name + "_suites"] = hit
        out["keys"][key] = block
    return out


def measure(tasks, seed=0, samples=PERMUTATIONS, modes=MODES):
    """Every mode over every task. The measurement, without the file around it."""
    out = {}
    for mode in modes:
        records = [measure_task(task, mode, seed, samples) for task in tasks]
        out[mode] = {"tasks": records, "totals": totals_for(records)}
    return out


def locked_tasks(path=None):
    """`(tasks, provenance)` from the lock, refusing to guess or to drift.

    From the lock's own `generator` block rather than from `generate()`'s defaults,
    and carrying the lock's `body_sha256` out with it: a fraction measured against
    a different task set is not evidence about this one, and Task 1 of this sprint
    moved all 36 `tests` digests, so the pairing has to be recorded and not assumed.

    It also refuses to measure suites the lock does not describe. `generate_from`
    rebuilds the tasks from the *current* `gen_tasks.py`, so an edit to the
    generator since the lock was written silently changes what gets permuted while
    the digest stamped on the result still names the old set -- a stale measurement
    that reads as current, which is the failure this guard exists for. Same posture
    as `--verify-lock`, and the same digest comparison: `gen_tasks.verify_lock`.
    """
    lock = gen_tasks.load_lock(path)
    if lock is None:
        raise SystemExit("no %s; run gen_tasks.py --write-lock first"
                         % os.path.basename(path or gen_tasks.LOCK_PATH))
    tasks = gen_tasks.generate_from(lock["generator"])
    failures = gen_tasks.verify_lock(lock, tasks)
    if failures:
        raise SystemExit(
            "REFUSING TO MEASURE: the working tree disagrees with %s, so these "
            "are not the locked suites.\n  %s\nRe-lock with `python3 "
            "eval/gen_tasks.py --write-lock` if the change was deliberate."
            % (os.path.basename(path or gen_tasks.LOCK_PATH),
               "\n  ".join(failures[:6])))
    return tasks, {"lock_version": lock.get("version"),
                   "body_sha256": lock.get("body_sha256"),
                   "generator": lock.get("generator"),
                   "tasks": len(tasks),
                   "battery": lock.get("self_check", {}).get("battery")}


# --------------------------------------------------------------- the stamp guard

STAMP_CURRENT = "current"
STAMP_STALE = "stale"
STAMP_UNKNOWN = "unknown"


def lock_body_sha256(path=None):
    """The live lock's `body_sha256`, or `None` when there is no readable lock."""
    try:
        lock = gen_tasks.load_lock(path)
    except ValueError:
        return None
    return None if lock is None else lock.get("body_sha256")


def stamp_status(document, lock_path=None):
    """`(status, message)` -- is a recorded result about the lock on disk?

    The stamp was a record and that was not enough. Sprint 8's result stayed on
    disk stamped with the pre-re-lock body, every reader treated it as current, and
    its retention fractions got quoted as a baseline for suites that no longer
    existed. So the stamp is now checked, in one place, by everything that reads a
    result: `print_report` shouts, `--verify-stamp` exits non-zero, and the
    document itself carries the answer so a consumer cannot miss it by accident.
    """
    if document is None:
        return STAMP_UNKNOWN, "no recorded measurement"
    recorded = (document.get("measured_against") or {}).get("body_sha256")
    live = lock_body_sha256(lock_path)
    if not recorded:
        return STAMP_UNKNOWN, "the recorded result names no lock body"
    if not live:
        return STAMP_UNKNOWN, "there is no readable tasks.lock to compare against"
    if recorded != live:
        return STAMP_STALE, ("measured against lock body %s, the lock on disk is "
                             "%s -- every fraction in this result describes suites "
                             "that have since moved"
                             % (recorded[:12], live[:12]))
    return STAMP_CURRENT, ""


# ------------------------------------------------------------- the neighbours
#
# Two cheap ones from the same list, taken while the machinery is open. Neither
# asks about assert order; both ask whether the fraction above is a property of the
# suites or an artefact of how it was measured, which is the question that decides
# whether the fraction is worth registering at all.

def _emit_modes_argv(seed, samples):
    return [sys.executable, os.path.abspath(__file__), "--emit-modes",
            "--seed", str(seed), "--permutations", str(samples)]


def hash_seed_neighbour(seed=0, samples=PERMUTATIONS):
    """The same measurement under `PYTHONHASHSEED=0` and `=1`.

    A subprocess each, because the hash seed is fixed before the interpreter starts
    and cannot be set from inside one. It is a real probe and not a formality: the
    references iterate sets and dicts of strings, so a set-ordering dependence in a
    reference would surface here as a differing outcome and nowhere else -- and a
    measurement that moves with the hash seed is not something to freeze.
    """
    seen = {}
    for value in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = value
        proc = subprocess.Popen(_emit_modes_argv(seed, samples), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate()
        if proc.returncode != 0:
            return {"ran": False, "why": "PYTHONHASHSEED=%s exited %d: %s"
                    % (value, proc.returncode,
                       err.decode("utf-8", "replace").strip()[-400:])}
        seen[value] = gen_tasks.digest(out.decode("utf-8"))
    return {"ran": True, "digests": seen,
            "identical": seen["0"] == seen["1"]}


def saturation_neighbour(tasks, seed=0, ladder=SATURATION_LADDER):
    """The same fractions at several sample sizes. Not asked for; added because.

    A third neighbour, and the seed re-roll is why it exists. Sampling permutations
    means every fraction here is a *lower* bound -- a suite whose ordering only
    flips under a rare permutation is counted as clean until that permutation is
    drawn -- and at the first sample size tried, the re-roll disagreed with itself
    on two suites. A bound of unknown tightness is not a number to register. This
    records where the ladder stops moving, so "saturated" is a checkable claim.
    """
    rungs = {}
    for samples in ladder:
        got = measure(tasks, seed=seed, samples=samples)
        rungs[str(samples)] = dict((mode, _headline(got[mode]["totals"]))
                                   for mode in got)
    values = list(rungs.values())
    return {"ladder": list(ladder), "at": rungs,
            "flat": all(entry == values[-1] for entry in values[1:])}


def seed_reroll_neighbour(tasks, seed=0, samples=PERMUTATIONS):
    """The same measurement with a different permutation sample.

    Only the *totals* are compared, and deliberately: the sampled orders differ by
    construction, so comparing the raw records would report a difference that means
    nothing. If the fractions move when the sample moves, the sample is too small
    and the headline number is noise rather than a measurement.
    """
    other = measure(tasks, seed=seed + 1, samples=samples)
    return {"seed": seed, "other_seed": seed + 1,
            "totals": dict((mode, other[mode]["totals"]) for mode in other)}

# ------------------------------------------------------------------ the record

def _headline(totals):
    """The headline fractions, without the suite lists, for a compact comparison.

    Flat, with the per-key ones as `key.name`, so two headlines compare with one
    equality -- which is what the seed re-roll neighbour does with them.
    """
    out = dict((name, totals[name]["fraction"]) for name, _ in SHARED_TOTALS)
    for key in KEYS:
        for name, _field in KEY_TOTALS:
            out["%s.%s" % (key, name)] = totals["keys"][key][name]["fraction"]
    return out


def build(tasks, provenance, seed=0, samples=PERMUTATIONS, neighbours=False):
    """The whole result document, ready to write.

    `measured_against` is first and load-bearing. This is a measurement about one
    frozen task set, and Task 1 of this sprint moved all 36 of its suite digests,
    so a reader who cannot tell which set produced a fraction cannot use it. D2
    cites the fractions; the digest is how a future reader checks the citation
    still applies, and `stamp_status` is how the check gets made for them.
    """
    modes = measure(tasks, seed=seed, samples=samples)
    document = {
        "version": RESULT_VERSION,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "rank_battery.py",
        "measured_against": provenance,
        "seed": seed,
        "permutations_requested": samples,
        "candidate_source": "reference + gen_tasks.BATTERY mutants",
        "ranking_key": "two, measured side by side on one lock: %s"
                       % "; ".join("%s = %s" % (key, RANKING[key]["source"])
                                   for key in KEYS),
        "ranking_keys": dict((key, {"source": RANKING[key]["source"],
                                    "third_element": RANKING[key]["third_element"]})
                             for key in KEYS),
        "shipped_key": SHIPPED_KEY,
        "supersedes": "any ranking_battery.json measured against another lock "
                      "body; fractions from a different task set are not "
                      "comparable to these and are not a baseline for them",
        "modes": modes,
        "headline": dict((mode, _headline(modes[mode]["totals"]))
                         for mode in modes),
    }
    # Stamped at write time by the run that just read the lock, so the flag is a
    # statement about this document and not a cached opinion about the lock.
    status, why = stamp_status(document)
    document["stamp"] = {"status": status, "detail": why,
                         "lock_body_sha256": lock_body_sha256()}
    if neighbours:
        reroll = seed_reroll_neighbour(tasks, seed=seed, samples=samples)
        reroll["headline"] = dict((mode, _headline(reroll["totals"][mode]))
                                  for mode in reroll["totals"])
        reroll["identical"] = reroll["headline"] == document["headline"]
        document["neighbours"] = {
            "python_hash_seed": hash_seed_neighbour(seed, samples),
            "seed_reroll": reroll,
            "sample_saturation": saturation_neighbour(tasks, seed=seed),
        }
    return document


def _stamp_banner(status, why):
    """Unmissable when stale, silent when current. Six lines and a shout."""
    if status == STAMP_CURRENT:
        return []
    label = ("STALE MEASUREMENT" if status == STAMP_STALE
             else "UNVERIFIED MEASUREMENT")
    bar = "!" * 72
    return [bar,
            "!! %s: %s" % (label, why),
            "!! Do not cite these fractions and do not use them as a baseline.",
            "!! Re-run: python3 eval/rank_battery.py --neighbours --write",
            bar]


def print_report(document):
    provenance = document["measured_against"]
    for line in _stamp_banner(*stamp_status(document)):
        print(line)
    print("ranking battery: %d tasks, lock body %s, seed %d, %d permutations"
          % (provenance["tasks"], (provenance["body_sha256"] or "?")[:12],
             document["seed"], document["permutations_requested"]))
    print("ranking keys: %s (shipped: %s)"
          % (", ".join(KEYS), document.get("shipped_key", "?")))
    for mode in MODES:
        block = document["modes"].get(mode)
        if not block:
            continue
        totals = block["totals"]
        print("\n[%s] %d suites measured, %d skipped"
              % (mode, totals["suites_measured"], len(totals["suites_skipped"])))
        for key, label in (
                ("order_dependent", "a check disagrees with itself run alone"),
                ("ceiling_untestable", "hit the runaway ceiling, so untestable"),
                ("verdict_changed", "a candidate's VERDICT changed"),
                ("kind_changed", "only the failure kind changed"),
                ("outcome_changed", "either of the two above"),
                ("checks_changed", "the per-check tally changed")):
            got = totals[key]
            names = totals[key + "_suites"]
            print("  %-42s %2d/%-2d  %.3f%s"
                  % (label, got["suites"], got["of"], got["fraction"],
                     "" if not names else "  " + ", ".join(names[:6])))
        print("  %-42s %-14s %-14s" % ("per ranking key:", KEY_DEPTH, KEY_CHECKS))
        for name, label in (
                ("merit_flipped", "merit ordering reversed"),
                ("rank_flipped", "rank ordering reversed"),
                ("tie_changed", "a merit tie became strict, or the reverse"),
                ("retention_changed", "_retain_best would keep another"),
                ("retained_outcome_changed", "the retained candidate's outcome")):
            cells = []
            for key in KEYS:
                got = totals["keys"][key][name]
                cells.append("%2d/%-2d %.3f" % (got["suites"], got["of"],
                                                got["fraction"]))
            print("  %-42s %-14s %-14s" % (label, cells[0], cells[1]))
    neighbours = document.get("neighbours")
    if neighbours:
        hashed = neighbours["python_hash_seed"]
        print("\nPYTHONHASHSEED 0 vs 1: %s"
              % ("identical" if hashed.get("identical")
                 else "DIFFERS" if hashed.get("ran") else hashed.get("why")))
        print("seed %d vs %d: %s"
              % (neighbours["seed_reroll"]["seed"],
                 neighbours["seed_reroll"]["other_seed"],
                 "same fractions" if neighbours["seed_reroll"]["identical"]
                 else "FRACTIONS MOVE"))
        ladder = neighbours["sample_saturation"]
        print("samples %s: %s"
              % ("/".join(str(n) for n in ladder["ladder"]),
                 "flat" if ladder["flat"] else "STILL RISING"))

def write_result(document, path=None):
    path = path or RESULT_PATH
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return path


def load_result(path=None):
    """The recorded measurement, or `None`. What a check reads to find the number.

    Deliberately does not validate the stamp: a caller that wants the number and a
    caller that wants to know whether the number is current are different callers,
    and silently returning `None` for a stale file would hide the staleness rather
    than report it. Ask `stamp_status` for that, which every reader in this module
    does.
    """
    path = path or RESULT_PATH
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Measure whether the ranking key is order-invariant over the "
                    "frozen suites -- the shipped pass-count key and the "
                    "pre-Sprint-9 depth key, side by side. Changes nothing.")
    parser.add_argument("--write", action="store_true",
                        help="record the result at eval/ranking_battery.json")
    parser.add_argument("--json", action="store_true",
                        help="print the whole document instead of the report")
    parser.add_argument("--neighbours", action="store_true",
                        help="also run PYTHONHASHSEED 0 vs 1 and a seed re-roll")
    parser.add_argument("--verify-stamp", action="store_true",
                        help="measure nothing; exit non-zero if the recorded "
                             "result was measured against another lock")
    parser.add_argument("--seed", type=int, default=0,
                        help="permutation sampling seed (default 0)")
    parser.add_argument("--permutations", type=int, default=PERMUTATIONS,
                        help="orders per suite, identity included "
                             "(default %d)" % PERMUTATIONS)
    parser.add_argument("--limit", type=int, default=0,
                        help="first N locked tasks only, for a quick look")
    parser.add_argument("--emit-modes", action="store_true",
                        help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    if args.verify_stamp:
        # Before `locked_tasks`, and measuring nothing: this leg has to work on a
        # tree whose generator has drifted, because that is one of the ways a
        # recorded result goes stale.
        status, why = stamp_status(load_result())
        for line in _stamp_banner(status, why):
            print(line)
        if status == STAMP_CURRENT:
            print("ranking_battery.json is current against tasks.lock (%s)"
                  % (lock_body_sha256() or "?")[:12])
            return 0
        return 1
    tasks, provenance = locked_tasks()
    if args.limit:
        tasks = tasks[:args.limit]
        provenance = dict(provenance, tasks=len(tasks),
                          truncated_to=args.limit)
    if args.emit_modes:
        # The hash-seed neighbour's child. Only the measurement, sorted and with
        # no timestamp in it, because the parent compares two of these by digest
        # and a clock would make every comparison differ.
        json.dump(measure(tasks, seed=args.seed, samples=args.permutations),
                  sys.stdout, sort_keys=True)
        return 0
    if args.limit and args.write:
        raise SystemExit("--limit is for a quick look; refusing to --write a "
                         "truncated measurement over the recorded one")
    document = build(tasks, provenance, seed=args.seed,
                     samples=args.permutations, neighbours=args.neighbours)
    if args.write:
        path = write_result(document)
    if args.json:
        json.dump(document, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print_report(document)
    if args.write:
        print("\nwrote %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

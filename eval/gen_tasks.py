"""Deterministic task generator for the pipeline eval. No LLM, no network.

The experimental unit is the *task family*, not the individual task. A family is
one kind of problem -- interval merging, business-day arithmetic, INI parsing --
carrying a parameterised reference implementation and a parameterised hidden
suite. Ten variations of three templates measure three things ten times each,
which is why this is built family-first: eighteen families measure eighteen
things, and a per-family pass rate says something a pooled average cannot.

Each generated task carries four parts:

    prompt      what the pipeline is given. Nothing else is ever shown to it.
    reference   a hidden implementation. Used only to bake expected values and
                to prove the suite is passable at all.
    tests       the hidden suite the pipeline is graded against.
    tier        1 easy transform, 2 multi-branch logic, 3 parsing/state.

Tier 2 is the target band. Tier 1 is where a single Executor call already
succeeds and tier 3 is where everything struggles, so neither says much about
whether verification and repair earn their cost.

    python3 eval/gen_tasks.py --list
    python3 eval/gen_tasks.py --self-check
    python3 eval/gen_tasks.py --per-family 3 --out eval/tasks.jsonl
    python3 eval/gen_tasks.py --write-lock      # once, deliberately
    python3 eval/gen_tasks.py --verify-lock     # cheap, and run_eval does it too

Determinism: every task's inputs come from ``random.Random(seed)`` where the
seed is derived from (global seed, family name, variant index). A given seed
always produces byte-identical tasks, and adding a family never shifts another
family's tasks.

Reference implementations read module-level constants (``_CASE``, ``_TOUCH``,
...) that do not exist in this module. That is deliberate: they are never called
here, only rendered to source with the constants baked on as a header. Any typo
in one is caught by ``--self-check``, which runs each suite against its own
reference and then against a stub.
"""

import argparse
import contextlib
import hashlib
import inspect
import io
import json
import os
import random
import sys
import textwrap
import time
import types

TIER_NAMES = {
    1: "easy single-function transforms",
    2: "multi-branch logic with real edge cases",
    3: "small parsing and state problems",
}

FAMILIES = []


class Family(object):
    """One kind of problem, plus the builder that parameterises it."""

    def __init__(self, name, tier, builder):
        self.name = name
        self.tier = tier
        self.builder = builder

    def __repr__(self):
        return "<Family %s tier=%d>" % (self.name, self.tier)


def family(name, tier):
    def register(builder):
        if any(existing.name == name for existing in FAMILIES):
            raise ValueError("duplicate family name %r" % name)
        FAMILIES.append(Family(name, tier, builder))
        return builder
    return register


class Task(object):
    __slots__ = ("family", "tier", "variant", "task_id", "prompt", "names",
                 "reference", "tests", "params", "seed")

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs[slot])

    def to_dict(self):
        return dict((slot, list(getattr(self, slot))
                     if slot == "names" else getattr(self, slot))
                    for slot in self.__slots__)


def _source(refs, consts, imports=()):
    """Render reference functions to source with their constants baked on.

    The constants come first so the functions close over them as globals, which
    is how a family gets to be parameterised without the parameters leaking into
    the signature the prompt describes.
    """
    lines = list(imports)
    for name in sorted(consts):
        lines.append("%s = %r" % (name, consts[name]))
    if lines:
        lines.append("")
    body = "\n\n".join(textwrap.dedent(inspect.getsource(ref)).strip()
                       for ref in refs)
    # The functions are defined here as `_ref_<public name>` so this module can
    # hold eighteen of them without collisions; the prefix is stripped on the
    # way out, so what the suite imports is the name the prompt asked for.
    return "\n".join(lines) + body.replace("_ref_", "") + "\n"


def _literal(value):
    """A re-evaluable literal, or a hard failure.

    Everything baked into a suite has to survive a round trip through source, so
    a value whose repr does not evaluate back to itself is a generator bug, not
    something to paper over at grading time.
    """
    text = repr(value)
    if eval(text, {}) != value:  # noqa: S307 - our own repr, no external input
        raise ValueError("value does not round-trip through repr: %r" % (value,))
    return text


# The tag is imported rather than spelled, because the grader parses what this
# emits and a second copy of the literal is a second thing to keep in sync.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness as _harness             # noqa: E402 - after the path repair

CHECK_TAG = _harness.CHECK_TAG

# Emitted verbatim into every generated suite, once, above the checks.
#
# `_mawout` is bound to `sys.stdout` here and not looked up again, so a solution
# that swaps `sys.stdout` after import cannot intercept the report; binding it
# rather than calling `print` also keeps `builtins.print` out of the path. It is
# deliberately not `sys.__stdout__`, which would be unswappable but would also
# escape the in-process capture the self-check and the reorder battery rely on.
#
# `_mawfirst` is a one-element list rather than a `global`, so the helpers need
# no declaration and the "first failure wins" rule is visible in the data.
_PROLOGUE = '''
import sys as _mawsys

_mawout, _mawresults, _mawfirst = _mawsys.stdout, [], []


def _mawpass(_i):
    _mawresults.append((_i, "pass", "-"))


def _mawfail(_i):
    _t, _v = _mawsys.exc_info()[:2]
    _mawresults.append((_i, "fail", getattr(_t, "__name__", "?")))
    if not _mawfirst:
        _mawfirst.append(_v)


def _mawreport():
    for _i, _outcome, _kind in _mawresults:
        _mawout.write("%(tag)s %%d %%s %%s\\n" %% (_i, _outcome, _kind))
    _mawout.write("%(tag)s done %%d\\n" %% len(_mawresults))
    _mawout.flush()
    if _mawfirst:
        raise _mawfirst[0]
''' % {"tag": CHECK_TAG}


def _probe(refs, consts, imports=()):
    """A namespace holding the reference, for builders that must test inputs.

    Some parameterisations turn a legal-looking input into a `ValueError` case.
    A builder that generates inputs randomly needs to know which bucket a
    candidate landed in before it writes the assert.
    """
    namespace = {}
    exec(compile(_source(refs, consts, imports), "<reference>", "exec"),
         namespace)
    return namespace


def _bake(reference, names, calls, raises):
    """Turn call expressions into a plain-assert suite with expected values.

    Expected values are computed by running the reference, so the suite is
    guaranteed passable. Every `raises` case is checked against the reference
    too -- a suite that demands an exception the reference never raises would be
    grading the pipeline against a spec nothing satisfies.

    Each check gets its own `try`, so the suite can say *how many* checks passed
    instead of only where it stopped. Two properties of the old shape are kept
    exactly, because the verdict depends on them:

      * `from solution import ...` stays outside every `try`. A missing entry
        point must remain a whole-suite import failure and must never be
        reported as a low score against a suite that never ran.
      * the first failing check's own exception is re-raised at the end, with its
        original traceback. So the exit code, the failure kind and the blamed
        line are the same events they were when the suite stopped at the first
        failure. The per-check counts are additional, not authoritative.
    """
    namespace = {}
    exec(compile(reference, "<reference>", "exec"), namespace)

    checks = []
    for expression in calls:
        value = eval(expression, dict(namespace))
        if value is None:
            raise ValueError("expected value is None, so a stub would pass: %s"
                             % expression)
        checks.append(["assert %s == %s, %r" % (expression, _literal(value),
                                                expression)])

    for expression, exception in raises:
        if not (isinstance(exception, type) and issubclass(exception, Exception)):
            # The per-check wrapper catches `Exception`. An expectation outside
            # that hierarchy would be uncatchable there and would silently
            # become a whole-suite failure instead of a recorded wrong-exception.
            raise ValueError("raises case is not an Exception subclass: %r"
                             % (exception,))
        try:
            eval(expression, dict(namespace))
        except exception:
            pass
        else:
            raise ValueError("reference does not raise %s for %s"
                             % (exception.__name__, expression))
        # Nested rather than a four-clause `try`: the inner block is the same
        # text the suite carried before, so a wrong exception falls out to the
        # wrapper and is recorded by type, while no exception at all raises the
        # same AssertionError on the same kind of line it always did.
        checks.append(["try:",
                       "    %s" % expression,
                       "except %s:" % exception.__name__,
                       "    pass",
                       "else:",
                       "    raise AssertionError(%r)"
                       % ("%s should raise %s" % (expression,
                                                  exception.__name__))])

    return bake_checks(names, checks)


def bake_checks(names, checks):
    """The import line, the reporting prologue, one wrapper per check, the report.

    Split out of `_bake` so it has exactly one definition. `eval/rank_battery.py`
    recognises this shape structurally and `test_pipeline.py`'s order-dependence
    controls have to be built in it -- a hand-rolled second copy would let the
    controls keep passing against a shape the real suites no longer have, which is
    the one thing a control must not do.

    `checks` is a list of check bodies, each a list of source lines at column zero.
    """
    lines = ["from solution import %s" % ", ".join(names), "", _PROLOGUE.strip()]
    for index, body in enumerate(checks, start=1):
        lines += ["", "try:"]
        lines += ["    %s" % line for line in body]
        lines += ["except Exception:", "    _mawfail(%d)" % index,
                  "else:", "    _mawpass(%d)" % index]
    lines += ["", "_mawreport()"]
    return "\n".join(lines) + "\n"

# --------------------------------------------------------------- input helpers

WORDS = ("alpha", "Beta", "GAMMA", "delta", "Epsilon", "zeta", "Eta", "theta")
KEYS = ("north", "south", "east", "west", "central", "coastal")


def _messy(rng):
    """A string with the whitespace damage real model output tends to produce."""
    picked = [rng.choice(WORDS) for _ in range(rng.randint(1, 4))]
    gaps = [rng.choice(("  ", " ", "\t", " \n ", "")) for _ in picked]
    return ("".join(gap + word for gap, word in zip(gaps, picked))
            + rng.choice(("", " ", "  ", "\t")))


def _ints(rng, count, low=-40, high=90):
    return [rng.randint(low, high) for _ in range(count)]


# ----------------------------------------------------------- tier 1: transforms

def _ref_normalize_label(text):
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    words = text.split()
    if _CASE == "lower":
        return " ".join(words).lower()
    if _CASE == "upper":
        return " ".join(words).upper()
    return " ".join(word[:1].upper() + word[1:].lower() for word in words)


@family("text_normalization", 1)
def _build_text_normalization(rng):
    case = rng.choice(("lower", "upper", "title"))
    described = {
        "lower": "lower case",
        "upper": "upper case",
        "title": "title case: the first character of each word upper case and "
                 "the rest lower case",
    }[case]
    calls = ["normalize_label(%r)" % _messy(rng) for _ in range(5)]
    calls += ["normalize_label('')", "normalize_label('     ')",
              "normalize_label('one')", "normalize_label('a\\tb\\nc  d')",
              "normalize_label('  MiXeD   CaSe  ')",
              "normalize_label('trailing   ')"]
    return dict(
        prompt=(
            "Write `normalize_label(text)`.\n\n"
            "Collapse every run of whitespace in `text` to a single space, "
            "strip leading and trailing whitespace, and return the result in "
            "%s.\n\n"
            "- An empty or all-whitespace `text` returns the empty string.\n"
            "- Whitespace means any whitespace, including tabs and newlines.\n"
            "- A `text` that is not a `str` raises `ValueError`." % described),
        names=("normalize_label",), calls=calls,
        raises=[("normalize_label(5)", ValueError),
                ("normalize_label(None)", ValueError)],
        refs=(_ref_normalize_label,), consts={"_CASE": case},
        params={"case": case})

def _ref_summarize(values):
    if not values:
        return {"count": 0, "total": 0, "mean": 0.0,
                "minimum": None, "maximum": None}
    total = sum(values)
    return {"count": len(values), "total": total,
            "mean": round(total / len(values), _DIGITS),
            "minimum": min(values), "maximum": max(values)}


@family("aggregation", 1)
def _build_aggregation(rng):
    digits = rng.choice((1, 2, 3))
    calls = ["summarize(%r)" % _ints(rng, rng.randint(2, 6)) for _ in range(5)]
    calls += ["summarize([])", "summarize([7])", "summarize([0, 0, 0])",
              "summarize([-5, 5])", "summarize([1, 2])",
              "summarize([1, 1, 2])"]
    return dict(
        prompt=(
            "Write `summarize(values)`, taking a list of integers and returning "
            "a dict with exactly these keys:\n\n"
            "- `count`: how many values there are\n"
            "- `total`: their sum\n"
            "- `mean`: the arithmetic mean, rounded with the built-in `round` "
            "to %d decimal place(s)\n"
            "- `minimum`, `maximum`: the smallest and largest value\n\n"
            "For an empty list return `count` 0, `total` 0, `mean` 0.0, and "
            "`minimum` and `maximum` both `None`." % digits),
        names=("summarize",), calls=calls, raises=(),
        refs=(_ref_summarize,), consts={"_DIGITS": digits},
        params={"digits": digits})


def _ref_encode(text):
    runs = []
    for character in text:
        if runs and runs[-1][0] == character:
            runs[-1][1] += 1
        else:
            runs.append([character, 1])
    pieces = []
    for character, count in runs:
        if count > 1 or _ALWAYS_COUNT:
            pieces.append("%s%d" % (character, count))
        else:
            pieces.append(character)
    return "".join(pieces)

@family("run_length_encoding", 1)
def _build_run_length(rng):
    always = rng.choice((True, False))
    alphabet = rng.choice(("ab", "abc", "xyz"))
    samples = []
    for _ in range(5):
        samples.append("".join(rng.choice(alphabet)
                               * rng.randint(1, 4)
                               for _ in range(rng.randint(1, 4))))
    calls = ["encode(%r)" % sample for sample in samples]
    calls += ["encode('')", "encode('a')", "encode('aa')", "encode('ab')",
              "encode('aaabbc')", "encode('abababa')", "encode('  a  ')"]
    rule = ("every run is written as the character followed by its length, even "
            "a run of one" if always else
            "a run of length one is written as the bare character, with no `1` "
            "after it")
    rule = rule[0].upper() + rule[1:]
    return dict(
        prompt=(
            "Write `encode(text)`, a run-length encoder.\n\n"
            "Walk `text` left to right and collapse each run of identical "
            "consecutive characters. %s.\n\n"
            "- `encode('')` returns the empty string.\n"
            "- Every character counts, including spaces and punctuation.\n"
            "- Return a `str`." % rule),
        names=("encode",), calls=calls, raises=(),
        refs=(_ref_encode,), consts={"_ALWAYS_COUNT": always},
        params={"always_count": always, "alphabet": alphabet})


def _ref_format_size(count):
    if not isinstance(count, int) or isinstance(count, bool):
        raise ValueError("count must be an int")
    if count < 0:
        raise ValueError("count must not be negative")
    units = ("B", "KB", "MB", "GB", "TB")
    if count < _STEP:
        return "%d B" % count
    size = float(count)
    index = 0
    while size >= _STEP and index < len(units) - 1:
        size = size / _STEP
        index += 1
    return "%.1f %s" % (size, units[index])

@family("byte_formatting", 1)
def _build_byte_formatting(rng):
    step = rng.choice((1024, 1000))
    counts = [rng.randint(0, step - 1), rng.randint(step, step * 4),
              rng.randint(step ** 2, step ** 2 * 9),
              rng.randint(step ** 3, step ** 3 * 3),
              rng.randint(step ** 5, step ** 5 * 2)]
    calls = ["format_size(%d)" % count for count in counts]
    calls += ["format_size(0)", "format_size(1)",
              "format_size(%d)" % (step - 1), "format_size(%d)" % step,
              "format_size(%d)" % (step * step),
              "format_size(%d)" % (step ** 4)]
    return dict(
        prompt=(
            "Write `format_size(count)`, turning a byte count into a short "
            "human-readable string.\n\n"
            "The units are `B`, `KB`, `MB`, `GB`, `TB`, each %d times the "
            "previous one.\n\n"
            "- Below %d, return the exact integer followed by ` B`, for example "
            "`'0 B'` and `'%d B'`.\n"
            "- Otherwise divide until the value is under %d or you have reached "
            "`TB`, and return it with exactly one decimal place, a single space, "
            "and the unit -- for example `'1.0 KB'`.\n"
            "- `TB` is the largest unit; a huge count stays in `TB` however "
            "large the number gets.\n"
            "- A negative `count`, or a `count` that is not an `int`, raises "
            "`ValueError`." % (step, step, step - 1, step)),
        names=("format_size",), calls=calls,
        raises=[("format_size(-1)", ValueError),
                ("format_size('12')", ValueError),
                ("format_size(1.5)", ValueError)],
        refs=(_ref_format_size,), consts={"_STEP": step},
        params={"step": step})


def _ref_top_names(records, count):
    if count < 0:
        raise ValueError("count must not be negative")
    kept = []
    for record in records:
        if "name" not in record or "score" not in record:
            raise ValueError("record needs a name and a score")
        if record["score"] >= _MIN_SCORE:
            kept.append(record)
    kept.sort(key=lambda record: (-record["score"], record["name"]))
    return [record["name"] for record in kept[:count]]

@family("ranking", 1)
def _build_ranking(rng):
    floor = rng.choice((0, 1, 10, 50))
    names = ("ada", "brin", "chen", "dijkstra", "erdos", "fermat")

    def rows(size):
        picked = rng.sample(names, size)
        return [{"name": name, "score": rng.randint(-10, 99)} for name in picked]

    calls = ["top_names(%r, %d)" % (rows(rng.randint(3, 6)), rng.randint(1, 4))
             for _ in range(5)]
    tied = [{"name": "zeta", "score": 80}, {"name": "alpha", "score": 80},
            {"name": "mid", "score": 79}]
    calls += ["top_names([], 3)", "top_names(%r, 0)" % tied,
              "top_names(%r, 2)" % tied, "top_names(%r, 99)" % tied,
              "top_names([{'name': 'solo', 'score': %d}], 1)" % floor,
              "top_names([{'name': 'low', 'score': %d}], 1)" % (floor - 1)]
    return dict(
        prompt=(
            "Write `top_names(records, count)`.\n\n"
            "`records` is a list of dicts, each with a `name` (str) and a "
            "`score` (int). Return the names of the best `count` records as a "
            "list of strings.\n\n"
            "- Discard any record whose score is below %d before ranking.\n"
            "- Rank by score, highest first. Records with equal scores are "
            "ordered by name, ascending.\n"
            "- Asking for more names than remain returns only those that "
            "remain; `count` of 0 returns an empty list.\n"
            "- A negative `count` raises `ValueError`, and so does a record "
            "missing either key." % floor),
        names=("top_names",), calls=calls,
        raises=[("top_names([], -1)", ValueError),
                ("top_names([{'name': 'x'}], 1)", ValueError),
                ("top_names([{'score': 5}], 1)", ValueError)],
        refs=(_ref_top_names,), consts={"_MIN_SCORE": floor},
        params={"min_score": floor})


# ------------------------------------------------------- tier 2: branching logic

def _ref_validate_code(code):
    if not isinstance(code, str):
        return False
    parts = code.split("-")
    if len(parts) != 3:
        return False
    prefix, digits, check = parts
    if len(prefix) != 2 or not prefix.isalpha() or prefix != prefix.upper():
        return False
    if len(digits) != 4 or not digits.isdigit():
        return False
    if len(check) != 1 or not check.isdigit():
        return False
    total = sum(int(digit) * weight for digit, weight in zip(digits, _WEIGHTS))
    return total % 10 == int(check)

@family("validation", 2)
def _build_validation(rng):
    weights = tuple(rng.sample((1, 3, 7, 9, 2, 5), 4))

    def code(valid=True):
        prefix = "".join(rng.choice("ABCDEFGHJKLM") for _ in range(2))
        digits = "".join(str(rng.randint(0, 9)) for _ in range(4))
        check = sum(int(d) * w for d, w in zip(digits, weights)) % 10
        if not valid:
            check = (check + rng.randint(1, 9)) % 10
        return "%s-%s-%d" % (prefix, digits, check)

    calls = ["validate_code(%r)" % code(True) for _ in range(3)]
    calls += ["validate_code(%r)" % code(False) for _ in range(3)]
    calls += ["validate_code('')", "validate_code('AB-1234')",
              "validate_code('ab-1234-5')", "validate_code('ABC-1234-5')",
              "validate_code('AB-12345-6')", "validate_code('AB-12a4-5')",
              "validate_code('AB-1234-56')", "validate_code('AB-1234-')",
              "validate_code('A1-1234-5')", "validate_code(42)",
              "validate_code(None)", "validate_code('AB-1234-5-6')"]

    def prefixed(prefix, digits="1357"):
        """A code whose check digit is *right*, so only the prefix can reject it.

        Every prefix case above carries a wrong check digit as well, so the suite
        could not tell "rejected for its prefix" from "rejected for its checksum":
        an implementation that got the prefix rule wrong still returned `False` for
        all three, by the other branch, and passed. Replacing `or` with `and` in
        the prefix test is exactly that implementation, and it is what
        `calibrate.py --stub broken` produces here.
        """
        check = sum(int(d) * w for d, w in zip(digits, weights)) % 10
        return "%s-%s-%d" % (prefix, digits, check)

    # The matched pair is the point: the same digits and the same correct check
    # digit, accepted with a legal prefix and refused with each illegal one, so a
    # refusal can only be about the prefix.
    calls += ["validate_code(%r)" % prefixed("AB"),
              "validate_code(%r)" % prefixed("ABC"),
              "validate_code(%r)" % prefixed("A"),
              "validate_code(%r)" % prefixed("A1"),
              "validate_code(%r)" % prefixed("ab")]
    return dict(
        prompt=(
            "Write `validate_code(code)`, returning `True` or `False`.\n\n"
            "A valid code is exactly three hyphen-separated parts:\n\n"
            "1. two upper-case ASCII letters\n"
            "2. four digits\n"
            "3. one check digit\n\n"
            "The check digit must equal `sum(digit * weight) %% 10`, where the "
            "four digits of part 2 are multiplied left to right by the weights "
            "%s.\n\n"
            "Anything that does not match -- wrong number of parts, wrong "
            "lengths, lower-case letters, non-digits, or a value that is not a "
            "`str` -- returns `False`. This function never raises."
            % list(weights)),
        names=("validate_code",), calls=calls, raises=(),
        refs=(_ref_validate_code,), consts={"_WEIGHTS": weights},
        params={"weights": list(weights)})


def _ref_merge_spans(spans):
    cleaned = []
    for span in spans:
        start, end = span
        if start > end:
            raise ValueError("span starts after it ends")
        cleaned.append((start, end))
    if not cleaned:
        return []
    cleaned.sort()
    merged = [list(cleaned[0])]
    for start, end in cleaned[1:]:
        touching = start == merged[-1][1] and _TOUCH_MERGES
        if start < merged[-1][1] or touching:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]

@family("interval_logic", 2)
def _build_interval_logic(rng):
    touch = rng.choice((True, False))

    def spans(size):
        out = []
        for _ in range(size):
            start = rng.randint(0, 40)
            out.append((start, start + rng.randint(0, 12)))
        return out

    calls = ["merge_spans(%r)" % spans(rng.randint(3, 6)) for _ in range(5)]
    calls += ["merge_spans([])", "merge_spans([(1, 5)])",
              "merge_spans([(1, 3), (3, 5)])", "merge_spans([(3, 5), (1, 3)])",
              "merge_spans([(1, 10), (2, 4)])", "merge_spans([(1, 4), (2, 10)])",
              "merge_spans([(5, 5), (1, 9)])", "merge_spans([(1, 2), (4, 6)])",
              "merge_spans([(1, 2), (1, 2)])",
              "merge_spans([(7, 9), (1, 2), (4, 6)])"]
    rule = ("Two spans that only touch at a point -- `(1, 3)` and `(3, 5)` -- "
            "merge into `(1, 5)`." if touch else
            "Two spans that only touch at a point -- `(1, 3)` and `(3, 5)` -- "
            "do NOT merge; they stay separate.")
    return dict(
        prompt=(
            "Write `merge_spans(spans)`.\n\n"
            "`spans` is a list of `(start, end)` integer pairs, in no "
            "particular order. Return the smallest list of merged spans "
            "covering the same ground, sorted ascending, as a list of tuples.\n\n"
            "- Overlapping spans merge.\n"
            "- %s\n"
            "- A zero-length span like `(5, 5)` is legal.\n"
            "- An empty list returns an empty list.\n"
            "- A span whose start is greater than its end raises "
            "`ValueError`." % rule),
        names=("merge_spans",), calls=calls,
        raises=[("merge_spans([(5, 1)])", ValueError),
                ("merge_spans([(1, 2), (9, 3)])", ValueError)],
        refs=(_ref_merge_spans,), consts={"_TOUCH_MERGES": touch},
        params={"touch_merges": touch})


def _ref_add_workdays(start, days):
    from datetime import date, timedelta
    current = date.fromisoformat(start)
    if current.weekday() in _WEEKEND:
        raise ValueError("start date is not a working day")
    if days == 0:
        return current.isoformat()
    step = 1 if days > 0 else -1
    remaining = abs(days)
    while remaining:
        current = current + timedelta(days=step)
        if current.weekday() not in _WEEKEND:
            remaining -= 1
    return current.isoformat()

@family("date_arithmetic", 2)
def _build_date_arithmetic(rng):
    from datetime import date, timedelta

    weekend = rng.choice(((5, 6), (4, 5)))
    named = {5: "Saturday", 6: "Sunday", 4: "Friday"}
    described = " and ".join(named[day] for day in weekend)

    def workday():
        current = date(2024, 1, 1) + timedelta(days=rng.randint(0, 700))
        while current.weekday() in weekend:
            current = current + timedelta(days=1)
        return current.isoformat()

    calls = ["add_workdays(%r, %d)" % (workday(), rng.choice((-9, -3, -1, 1,
                                                              2, 5, 8, 20)))
             for _ in range(6)]
    anchor = workday()
    calls += ["add_workdays(%r, 0)" % anchor,
              "add_workdays(%r, 1)" % anchor,
              "add_workdays(%r, -1)" % anchor,
              "add_workdays('2024-02-28', 2)",
              "add_workdays('2024-12-30', 5)",
              "add_workdays('2023-02-27', 250)"]
    weekend_day = 1 + max(weekend)  # isoweekday of a weekend day
    sample_weekend = date(2024, 1, 6) + timedelta(days=(max(weekend) - 5))
    return dict(
        prompt=(
            "Write `add_workdays(start, days)`.\n\n"
            "`start` is an ISO date string like `'2024-03-15'`. Move `days` "
            "working days forward (or backward, if `days` is negative) and "
            "return the resulting date as an ISO string.\n\n"
            "- The weekend is %s. Every other day is a working day; there are "
            "no holidays.\n"
            "- `days` of 0 returns `start` unchanged.\n"
            "- Counting skips weekend days entirely rather than landing on one, "
            "so the result is always a working day.\n"
            "- A `start` that is not a valid ISO date raises `ValueError`, and "
            "so does a `start` that falls on a weekend."
            % described),
        names=("add_workdays",), calls=calls,
        raises=[("add_workdays('not-a-date', 1)", ValueError),
                ("add_workdays(%r, 3)" % sample_weekend.isoformat(), ValueError),
                ("add_workdays('2024-13-01', 1)", ValueError)],
        refs=(_ref_add_workdays,), consts={"_WEEKEND": weekend},
        params={"weekend": list(weekend), "isoweekday": weekend_day})

def _ref_canonical_query(query):
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    kept = {}
    for chunk in query.split("&"):
        if not chunk:
            continue
        key, separator, value = chunk.partition("=")
        if not separator or key == "" or value == "":
            continue
        if _KEEP == "first" and key in kept:
            continue
        kept[key] = value
    return "&".join("%s=%s" % (key, kept[key]) for key in sorted(kept))


@family("query_canonicalization", 2)
def _build_query_canonicalization(rng):
    keep = rng.choice(("first", "last"))
    letters = ("b", "a", "zz", "m", "c")

    def query():
        pieces = []
        for _ in range(rng.randint(2, 6)):
            key = rng.choice(letters)
            pieces.append(rng.choice(("%s=%d" % (key, rng.randint(1, 9)),
                                      "%s=" % key, key,
                                      "%s=%d" % (key, rng.randint(10, 99)))))
        return "&".join(pieces)

    calls = ["canonical_query(%r)" % query() for _ in range(6)]
    calls += ["canonical_query('')", "canonical_query('a=1')",
              "canonical_query('b=2&a=1')", "canonical_query('a=1&a=2')",
              "canonical_query('a=1&&b=2')", "canonical_query('a=&b=2')",
              "canonical_query('flag&b=2')", "canonical_query('=5&b=2')",
              "canonical_query('a=1=2&b=3')", "canonical_query('&&&')"]
    rule = ("the first occurrence wins" if keep == "first"
            else "the last occurrence wins")
    return dict(
        prompt=(
            "Write `canonical_query(query)`, putting a URL query string into a "
            "canonical form so two equivalent queries compare equal.\n\n"
            "Split on `&`, then for each piece split on the FIRST `=`. Return "
            "the surviving pairs as `key=value`, joined by `&`, with keys "
            "sorted ascending.\n\n"
            "- Drop empty pieces, pieces with no `=` at all, pieces with an "
            "empty key, and pieces with an empty value.\n"
            "- A value may itself contain `=`; only the first one separates.\n"
            "- If a key appears more than once, %s.\n"
            "- Do no percent-decoding, and return the empty string when nothing "
            "survives.\n"
            "- A `query` that is not a `str` raises `ValueError`." % rule),
        names=("canonical_query",), calls=calls,
        raises=[("canonical_query(None)", ValueError),
                ("canonical_query(7)", ValueError)],
        refs=(_ref_canonical_query,), consts={"_KEEP": keep},
        params={"keep": keep})

def _ref_normalize_path(path):
    absolute = path.startswith("/")
    parts = []
    for piece in path.split("/"):
        if piece == "" or piece == ".":
            continue
        if piece == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif absolute:
                if _ESCAPE == "raise":
                    raise ValueError("path escapes the root")
            else:
                parts.append("..")
            continue
        parts.append(piece)
    joined = "/".join(parts)
    if absolute:
        return "/" + joined
    return joined or "."


@family("path_canonicalization", 2)
def _build_path_canonicalization(rng):
    escape = rng.choice(("raise", "clamp"))
    pieces = ("usr", "local", "bin", ".", "..", "share", "lib")

    def path():
        chosen = [rng.choice(pieces) for _ in range(rng.randint(2, 6))]
        prefix = rng.choice(("/", "", "./", "//"))
        return prefix + "/".join(chosen) + rng.choice(("", "/"))

    calls = []
    # Some parameterisations make an escaping path raise, so candidates are
    # probed against the reference and the raising ones are moved to `raises`
    # rather than being dropped: an input that must raise is part of the spec.
    namespace = _probe((_ref_normalize_path,), {"_ESCAPE": escape})
    raises = []
    attempts = 0
    while len(calls) < 12 and attempts < 200:
        attempts += 1
        candidate = path()
        expression = "normalize_path(%r)" % candidate
        try:
            eval(expression, dict(namespace))
        except ValueError:
            if len(raises) < 3:
                raises.append((expression, ValueError))
            continue
        calls.append(expression)
    calls += ["normalize_path('/')", "normalize_path('.')",
              "normalize_path('')", "normalize_path('a//b')",
              "normalize_path('/a/./b/')", "normalize_path('a/../b')",
              "normalize_path('a/b/../..')", "normalize_path('../a')",
              "normalize_path('../../a')", "normalize_path('/a/b/../c')"]
    if escape == "raise":
        raises.append(("normalize_path('/..')", ValueError))
        raises.append(("normalize_path('/a/../../b')", ValueError))
    else:
        calls += ["normalize_path('/..')", "normalize_path('/a/../../b')"]
    rule = ("`..` that would climb above `/` raises `ValueError`"
            if escape == "raise" else
            "`..` that would climb above `/` is discarded, so the path clamps "
            "at the root")
    return dict(
        prompt=(
            "Write `normalize_path(path)`, cleaning up a slash-separated path "
            "textually. Do not touch the filesystem.\n\n"
            "- Collapse repeated slashes and drop every `.` component.\n"
            "- `..` removes the preceding component.\n"
            "- An absolute path (one starting with `/`) stays absolute; a "
            "relative one stays relative.\n"
            "- In a relative path, leading `..` components that have nothing to "
            "remove are kept, so `'../a'` normalizes to `'../a'`.\n"
            "- In an absolute path, %s.\n"
            "- A trailing slash is dropped. `'/'` normalizes to `'/'`, and an "
            "empty or all-dots relative path normalizes to `'.'`." % rule),
        names=("normalize_path",), calls=calls, raises=raises,
        refs=(_ref_normalize_path,), consts={"_ESCAPE": escape},
        params={"escape": escape})

def _ref_group_totals(rows):
    totals = {}
    for row in rows:
        if _KEY_FIELD not in row:
            raise ValueError("row is missing %s" % _KEY_FIELD)
        if "amount" not in row:
            raise ValueError("row is missing amount")
        key = row[_KEY_FIELD]
        totals[key] = totals.get(key, 0) + row["amount"]
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return [(key, round(total, 2)) for key, total in ordered]


@family("grouping", 2)
def _build_grouping(rng):
    field = rng.choice(("region", "team", "channel"))

    def rows(size):
        return [{field: rng.choice(KEYS[:4]),
                 "amount": round(rng.uniform(-20, 200), 2)}
                for _ in range(size)]

    calls = ["group_totals(%r)" % rows(rng.randint(3, 8)) for _ in range(5)]
    tie = [{field: "beta", "amount": 10}, {field: "alpha", "amount": 10},
           {field: "gamma", "amount": 4}]
    calls += ["group_totals([])", "group_totals(%r)" % tie,
              "group_totals([{%r: 'solo', 'amount': 0}])" % field,
              "group_totals([{%r: 'x', 'amount': 1}, {%r: 'x', 'amount': 2}])"
              % (field, field),
              "group_totals([{%r: 'x', 'amount': -5}, {%r: 'y', 'amount': 5}])"
              % (field, field)]
    return dict(
        prompt=(
            "Write `group_totals(rows)`.\n\n"
            "`rows` is a list of dicts, each with a `%s` (str) and an `amount` "
            "(int or float). Sum the amounts per `%s` and return a list of "
            "`(%s, total)` tuples.\n\n"
            "- Order by total, largest first. Equal totals are ordered by `%s`, "
            "ascending.\n"
            "- Round each total to 2 decimal places with the built-in `round`.\n"
            "- An empty list returns an empty list.\n"
            "- A row missing either key raises `ValueError`."
            % (field, field, field, field)),
        names=("group_totals",), calls=calls,
        raises=[("group_totals([{%r: 'x'}])" % field, ValueError),
                ("group_totals([{'amount': 1}])", ValueError)],
        refs=(_ref_group_totals,), consts={"_KEY_FIELD": field},
        params={"key_field": field})

def _ref_compare_versions(left, right):
    def split(text):
        if not isinstance(text, str) or not text:
            raise ValueError("version must be a non-empty string")
        core, _, pre = text.partition("-")
        numbers = []
        for piece in core.split("."):
            if not piece.isdigit():
                raise ValueError("bad version segment: %r" % piece)
            numbers.append(int(piece))
        return numbers, pre

    left_numbers, left_pre = split(left)
    right_numbers, right_pre = split(right)
    size = max(len(left_numbers), len(right_numbers))
    left_numbers = left_numbers + [0] * (size - len(left_numbers))
    right_numbers = right_numbers + [0] * (size - len(right_numbers))
    if left_numbers != right_numbers:
        return -1 if left_numbers < right_numbers else 1
    if not _PRE_LOWER or left_pre == right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    return -1 if left_pre < right_pre else 1


@family("version_ordering", 2)
def _build_version_ordering(rng):
    pre_lower = rng.choice((True, False))

    def version():
        parts = [str(rng.randint(0, 4)) for _ in range(rng.randint(1, 3))]
        suffix = rng.choice(("", "", "-alpha", "-beta", "-rc1"))
        return ".".join(parts) + suffix

    calls = ["compare_versions(%r, %r)" % (version(), version())
             for _ in range(8)]
    calls += ["compare_versions('1.0', '1.0.0')",
              "compare_versions('1.2', '1.10')",
              "compare_versions('2', '1.9.9')",
              "compare_versions('1.0.0', '1.0.0')",
              "compare_versions('1.0.0-alpha', '1.0.0')",
              "compare_versions('1.0.0', '1.0.0-alpha')",
              "compare_versions('1.0.0-alpha', '1.0.0-beta')",
              "compare_versions('0.0.1', '0.1.0')"]
    rule = ("A version with a pre-release suffix sorts BELOW the same version "
            "without one, and two suffixes are compared as plain strings."
            if pre_lower else
            "A pre-release suffix is ignored entirely: `'1.0.0-alpha'` and "
            "`'1.0.0'` compare equal.")
    return dict(
        prompt=(
            "Write `compare_versions(left, right)`, returning `-1` if `left` "
            "sorts before `right`, `1` if after, and `0` if they are equal.\n\n"
            "A version is dot-separated digit groups, optionally followed by "
            "`-` and a pre-release suffix, as in `'1.4.2'` or `'2.0-rc1'`.\n\n"
            "- Compare the numeric groups left to right as integers, so "
            "`'1.10'` is above `'1.2'`.\n"
            "- Missing groups count as 0, so `'1.0'` and `'1.0.0'` are equal.\n"
            "- %s\n"
            "- A version that is empty, is not a `str`, or has a non-digit "
            "numeric group raises `ValueError`." % rule),
        names=("compare_versions",), calls=calls,
        raises=[("compare_versions('1.x', '1.0')", ValueError),
                ("compare_versions('', '1.0')", ValueError),
                ("compare_versions('1.0', None)", ValueError),
                ("compare_versions('1..0', '1.0')", ValueError),
                # `None` was the only non-`str` the suite tried, and it is falsy,
                # so `not isinstance(text, str) or not text` and the same test with
                # `and` rejected it alike. A *truthy* non-`str` separates them: the
                # `and` version falls through to `.partition` and raises
                # AttributeError, which is not the ValueError the prompt promises,
                # and `except ValueError` in the suite will not swallow it.
                ("compare_versions(42, '1.0')", ValueError),
                ("compare_versions('1.0', 42)", ValueError)],
        refs=(_ref_compare_versions,), consts={"_PRE_LOWER": pre_lower},
        params={"prerelease_is_lower": pre_lower})

def _ref_charge(units):
    if not isinstance(units, int) or isinstance(units, bool):
        raise ValueError("units must be an int")
    if units < 0:
        raise ValueError("units must not be negative")
    total = 0.0
    previous = 0
    for limit, rate in _BRACKETS:
        if units <= previous:
            break
        total += (min(units, limit) - previous) * rate
        previous = limit
    if units > previous:
        total += (units - previous) * _OVER_RATE
    return round(total, 2)


@family("tiered_pricing", 2)
def _build_tiered_pricing(rng):
    first = rng.choice((50, 100, 200))
    second = first + rng.choice((100, 250, 500))
    brackets = ((first, round(rng.uniform(0.05, 0.4), 2)),
                (second, round(rng.uniform(0.02, 0.1), 2)))
    over = round(rng.uniform(0.005, 0.02), 3)
    table = "\n".join([
        "- the first %d units cost %s each" % (first, brackets[0][1]),
        "- the next %d units (up to %d in total) cost %s each"
        % (second - first, second, brackets[1][1]),
        "- every unit beyond %d costs %s each" % (second, over),
    ])
    calls = ["charge(%d)" % rng.randint(0, second * 2) for _ in range(6)]
    calls += ["charge(0)", "charge(1)", "charge(%d)" % first,
              "charge(%d)" % (first + 1), "charge(%d)" % second,
              "charge(%d)" % (second + 1), "charge(%d)" % (second * 10)]
    return dict(
        prompt=(
            "Write `charge(units)`, returning the cost of `units` units under a "
            "tiered rate card. The tiers are cumulative -- each unit is billed "
            "at the rate for the tier it falls in, not the whole amount at one "
            "rate:\n\n%s\n\n"
            "- Return a float rounded to 2 decimal places with the built-in "
            "`round`.\n"
            "- `charge(0)` is `0.0`.\n"
            "- A negative `units`, or one that is not an `int`, raises "
            "`ValueError`." % table),
        names=("charge",), calls=calls,
        raises=[("charge(-1)", ValueError), ("charge(1.5)", ValueError),
                ("charge('10')", ValueError)],
        refs=(_ref_charge,),
        consts={"_BRACKETS": brackets, "_OVER_RATE": over},
        params={"brackets": [list(b) for b in brackets], "over_rate": over})

# ---------------------------------------------------- tier 3: parsing and state

def _ref_order_tasks(nodes, edges):
    if len(set(nodes)) != len(nodes):
        raise ValueError("duplicate node")
    incoming = dict((node, 0) for node in nodes)
    outgoing = dict((node, []) for node in nodes)
    for first, second in edges:
        if first not in incoming or second not in incoming:
            raise ValueError("edge names an unknown node")
        outgoing[first].append(second)
        incoming[second] += 1
    ready = sorted(node for node in nodes if incoming[node] == 0)
    order = []
    while ready:
        node = ready.pop(0) if _TIE == "smallest" else ready.pop()
        order.append(node)
        for neighbour in outgoing[node]:
            incoming[neighbour] -= 1
            if incoming[neighbour] == 0:
                ready.append(neighbour)
                ready.sort()
    if len(order) != len(nodes):
        raise ValueError("dependency cycle")
    return order


@family("graph_traversal", 3)
def _build_graph_traversal(rng):
    tie = rng.choice(("smallest", "largest"))
    pool = ("build", "compile", "deploy", "fetch", "lint", "package", "test")

    def acyclic(size):
        nodes = rng.sample(pool, size)
        edges = []
        for index, node in enumerate(nodes):
            for later in nodes[index + 1:]:
                if rng.random() < 0.35:
                    edges.append((node, later))
        rng.shuffle(edges)
        shuffled = list(nodes)
        rng.shuffle(shuffled)
        return shuffled, edges

    calls = []
    for _ in range(6):
        nodes, edges = acyclic(rng.randint(3, 6))
        calls.append("order_tasks(%r, %r)" % (nodes, edges))
    calls += ["order_tasks([], [])", "order_tasks(['a'], [])",
              "order_tasks(['b', 'a'], [])",
              "order_tasks(['a', 'b'], [('b', 'a')])",
              "order_tasks(['a', 'b', 'c'], [('a', 'c')])",
              "order_tasks(['a', 'b', 'c'], [('a', 'b'), ('a', 'b')])"]
    rule = ("the alphabetically smallest" if tie == "smallest"
            else "the alphabetically largest")
    return dict(
        prompt=(
            "Write `order_tasks(nodes, edges)`, a topological sort.\n\n"
            "`nodes` is a list of unique name strings in arbitrary order. "
            "`edges` is a list of `(before, after)` pairs meaning `before` must "
            "come first. Return a list of all the names in a valid order.\n\n"
            "- When several names are ready at once, take %s of them, so the "
            "output is fully determined.\n"
            "- A duplicated edge is harmless and must not change the result.\n"
            "- An empty `nodes` returns an empty list.\n"
            "- Raise `ValueError` for a duplicated node name, an edge naming a "
            "name that is not in `nodes`, or a cycle." % rule),
        names=("order_tasks",), calls=calls,
        raises=[("order_tasks(['a', 'a'], [])", ValueError),
                ("order_tasks(['a'], [('a', 'z')])", ValueError),
                ("order_tasks(['a', 'b'], [('a', 'b'), ('b', 'a')])",
                 ValueError)],
        refs=(_ref_order_tasks,), consts={"_TIE": tie},
        params={"tie_break": tie})

def _ref_evaluate(text):
    tokens = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character.isdigit():
            start = index
            while index < len(text) and text[index].isdigit():
                index += 1
            tokens.append(int(text[start:index]))
            continue
        if character in "+-*/()":
            tokens.append(character)
            index += 1
            continue
        raise ValueError("unexpected character %r" % character)

    position = [0]

    def peek():
        return tokens[position[0]] if position[0] < len(tokens) else None

    def take():
        token = peek()
        position[0] += 1
        return token

    def atom():
        token = take()
        if token == "(":
            value = expression()
            if take() != ")":
                raise ValueError("missing closing parenthesis")
            return value
        if token == "-":
            return -atom()
        if isinstance(token, int):
            return token
        raise ValueError("unexpected token %r" % (token,))

    def term():
        value = atom()
        while peek() in ("*", "/"):
            operator = take()
            right = atom()
            if operator == "*":
                value = value * right
            elif right == 0:
                raise ValueError("division by zero")
            elif _DIVISION == "floor":
                value = value // right
            else:
                value = value / right
        return value

    def expression():
        value = term()
        while peek() in ("+", "-"):
            operator = take()
            right = term()
            value = value + right if operator == "+" else value - right
        return value

    result = expression()
    if position[0] != len(tokens):
        raise ValueError("trailing tokens")
    return result

@family("expression_eval", 3)
def _build_expression_eval(rng):
    division = rng.choice(("floor", "true"))

    def expression(depth):
        if depth <= 0 or rng.random() < 0.3:
            return str(rng.randint(0, 30))
        left = expression(depth - 1)
        right = expression(depth - 1)
        operator = rng.choice(("+", "-", "*", "/", "+", "-"))
        text = "%s %s %s" % (left, operator, right)
        return "(%s)" % text if rng.random() < 0.4 else text

    namespace = _probe((_ref_evaluate,), {"_DIVISION": division})
    calls, raises, attempts = [], [], 0
    while len(calls) < 10 and attempts < 400:
        attempts += 1
        candidate = expression(rng.randint(1, 3))
        rendered = "evaluate(%r)" % candidate
        try:
            eval(rendered, dict(namespace))
        except ValueError:
            if len(raises) < 2:
                raises.append((rendered, ValueError))
            continue
        calls.append(rendered)
    calls += ["evaluate('1')", "evaluate('  7  ')", "evaluate('2+3*4')",
              "evaluate('(2+3)*4')", "evaluate('-5')", "evaluate('2*-3')",
              "evaluate('10-2-3')", "evaluate('100/10/2')",
              "evaluate('-(2+3)')", "evaluate('((((5))))')",
              "evaluate('12345 * 0')", "evaluate('7-9')"]
    rule = ("`/` is floor division, exactly Python's `//`: it rounds toward "
            "negative infinity and always yields an `int`."
            if division == "floor" else
            "`/` is true division, exactly Python's `/`: `10/4` is `2.5`.")
    return dict(
        prompt=(
            "Write `evaluate(text)`, evaluating an integer arithmetic "
            "expression and returning its value. Do not use `eval`, `exec`, or "
            "`ast` -- tokenize and parse it yourself.\n\n"
            "The grammar is non-negative integer literals, the binary "
            "operators `+ - * /`, unary minus, and parentheses. Whitespace "
            "between tokens is insignificant.\n\n"
            "- `*` and `/` bind tighter than `+` and `-`; equal precedence "
            "associates left to right, so `'10-2-3'` is `5`.\n"
            "- %s\n"
            "- Division by zero raises `ValueError`.\n"
            "- Malformed input raises `ValueError`: an unknown character, an "
            "unbalanced parenthesis, a missing operand, or trailing tokens. "
            "Never raise `SyntaxError` or `ZeroDivisionError`." % rule),
        names=("evaluate",), calls=calls,
        raises=raises + [("evaluate('')", ValueError),
                         ("evaluate('1/0')", ValueError),
                         ("evaluate('2+')", ValueError),
                         ("evaluate('(1+2')", ValueError),
                         ("evaluate('1 2')", ValueError),
                         ("evaluate('4 % 2')", ValueError)],
        refs=(_ref_evaluate,), consts={"_DIVISION": division},
        params={"division": division})

def _ref_parse_config(text):
    sections = {}
    current = None
    key = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            key = None
            continue
        stripped = line.strip()
        if stripped[0] in _COMMENTS:
            continue
        if line[:1] in (" ", "\t"):
            if key is None:
                raise ValueError("continuation line without a key")
            sections[current][key] += " " + stripped
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            if not current:
                raise ValueError("empty section name")
            sections.setdefault(current, {})
            key = None
            continue
        if "=" not in stripped:
            raise ValueError("neither a section nor an assignment")
        if current is None:
            if _STRAY == "raise":
                raise ValueError("assignment before any section")
            current = ""
            sections.setdefault(current, {})
        name, _, value = stripped.partition("=")
        name = name.strip()
        if not name:
            raise ValueError("empty key")
        if name in sections[current] and _DUPLICATE == "raise":
            raise ValueError("duplicate key")
        sections[current][name] = value.strip()
        key = name
    return sections

@family("config_parsing", 3)
def _build_config_parsing(rng):
    comments = rng.choice(("#", "#;"))
    stray = rng.choice(("raise", "default"))
    duplicate = rng.choice(("raise", "last"))

    def document():
        lines = []
        for _ in range(rng.randint(1, 3)):
            lines.append("[%s]" % rng.choice(KEYS))
            for _ in range(rng.randint(1, 3)):
                lines.append("%s = %s" % (rng.choice(("host", "port", "name")),
                                          rng.choice(("1", "localhost", ""))))
                if rng.random() < 0.3:
                    lines.append("    continued %d" % rng.randint(1, 9))
            if rng.random() < 0.4:
                lines.append("%s a comment" % comments[0])
            if rng.random() < 0.4:
                lines.append("")
        return "\n".join(lines)

    namespace = _probe((_ref_parse_config,),
                       {"_COMMENTS": comments, "_STRAY": stray,
                        "_DUPLICATE": duplicate})
    calls, raises, attempts = [], [], 0
    while len(calls) < 8 and attempts < 300:
        attempts += 1
        rendered = "parse_config(%r)" % document()
        try:
            eval(rendered, dict(namespace))
        except ValueError:
            if len(raises) < 2:
                raises.append((rendered, ValueError))
            continue
        calls.append(rendered)
    fixed = ["", "\n\n", "[a]", "[a]\nk = v", "[a]\nk=v\n[b]\nk = w",
             "[ spaced ]\nk = v", "[a]\nk = v with = signs",
             "[a]\n%s comment\nk = v" % comments[0],
             "[a]\nk = one\n    two\n    three",
             "[a]\nk = one\n\nk2 = two", "[a]\nempty ="]
    if duplicate == "last":
        fixed.append("[a]\nk = 1\nk = 2")
    if stray == "default":
        fixed.append("k = v\n[a]\nk = w")
    calls += ["parse_config(%r)" % item for item in fixed]
    stray_rule = ("An assignment before the first section header raises "
                  "`ValueError`." if stray == "raise" else
                  "An assignment before the first section header belongs to a "
                  "section whose name is the empty string `''`.")
    duplicate_rule = ("A key repeated within one section raises `ValueError`."
                      if duplicate == "raise" else
                      "A key repeated within one section keeps the last value.")
    return dict(
        prompt=(
            "Write `parse_config(text)`, parsing an INI-like document into a "
            "dict of dicts: section name -> key -> value, all strings.\n\n"
            "- `[name]` on its own line starts a section; the name is stripped "
            "of surrounding whitespace.\n"
            "- `key = value` assigns within the current section. Split on the "
            "FIRST `=`; strip whitespace from both sides. A value may be "
            "empty, and may itself contain `=`.\n"
            "- A line whose first non-whitespace character is one of `%s` is a "
            "comment and is ignored wherever it appears.\n"
            "- A line that STARTS with a space or tab continues the previous "
            "key: append a single space and the stripped line to that key's "
            "value.\n"
            "- A blank line ends any continuation, so an indented line after "
            "one raises `ValueError`.\n"
            "- %s\n- %s\n"
            "- An empty document returns an empty dict.\n"
            "- Raise `ValueError` for an empty section name, an empty key, a "
            "continuation with no key to continue, and any line that is neither "
            "a section header nor an assignment."
            % (comments, stray_rule, duplicate_rule)),
        names=("parse_config",), calls=calls,
        raises=raises + [("parse_config('    indented')", ValueError),
                         ("parse_config('[]\\nk = v')", ValueError),
                         ("parse_config('[a]\\n= v')", ValueError),
                         ("parse_config('[a]\\nnonsense')", ValueError),
                         ("parse_config('[a]\\nk = v\\n\\n  cont')", ValueError)],
        refs=(_ref_parse_config,),
        consts={"_COMMENTS": comments, "_STRAY": stray,
                "_DUPLICATE": duplicate},
        params={"comments": comments, "stray": stray, "duplicate": duplicate})

def _ref_parse_row(line):
    fields = []
    current = []
    index = 0
    quoted = False
    while index < len(line):
        character = line[index]
        if quoted:
            if character == '"':
                if line[index + 1:index + 2] == '"':
                    current.append('"')
                    index += 2
                    continue
                quoted = False
                index += 1
                continue
            current.append(character)
            index += 1
            continue
        if character == '"':
            if current and _STRICT_QUOTES:
                raise ValueError("quote inside an unquoted field")
            quoted = True
            index += 1
            continue
        if character == _DELIMITER:
            fields.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    if quoted:
        raise ValueError("unterminated quoted field")
    fields.append("".join(current))
    return fields


@family("delimited_parsing", 3)
def _build_delimited_parsing(rng):
    delimiter = rng.choice((",", ";", "|"))
    strict = rng.choice((True, False))

    def row():
        fields = []
        for _ in range(rng.randint(1, 4)):
            kind = rng.random()
            if kind < 0.4:
                fields.append(rng.choice(("ada", "brin", "", "42")))
            elif kind < 0.7:
                fields.append('"%s%s%s"' % (rng.choice(("a", "b")), delimiter,
                                            rng.choice(("c", "d"))))
            else:
                fields.append('"say ""%s"""' % rng.choice(("hi", "no")))
        return delimiter.join(fields)

    calls = ["parse_row(%r)" % row() for _ in range(8)]
    calls += ["parse_row('')", "parse_row('a')",
              "parse_row(%r)" % delimiter,
              "parse_row(%r)" % ("a%sb%sc" % (delimiter, delimiter)),
              "parse_row(%r)" % ('"a%sb"' % delimiter),
              "parse_row(%r)" % '"""quoted"""',
              "parse_row(%r)" % '""',
              "parse_row(%r)" % ("%s%s" % (delimiter, delimiter)),
              "parse_row(%r)" % ('"a"%s"b"' % delimiter),
              "parse_row(%r)" % ('"  padded  "%sx' % delimiter)]
    if not strict:
        calls.append("parse_row(%r)" % 'ab"cd"')
    rule = ("A `\"` appearing after any character in an unquoted field raises "
            "`ValueError`." if strict else
            "A `\"` appearing partway through a field simply starts a quoted "
            "run from that point; nothing is rejected.")
    return dict(
        prompt=(
            "Write `parse_row(line)`, splitting one delimited line into a list "
            "of field strings. Do not use the `csv` module.\n\n"
            "- The delimiter is `%s`.\n"
            "- A field may be wrapped in double quotes, in which case a "
            "delimiter inside it is literal text.\n"
            "- Inside a quoted field, `\"\"` means one literal `\"`.\n"
            "- The quotes themselves are not part of the value, and whitespace "
            "is never stripped.\n"
            "- `parse_row('')` returns `['']`, and `%r` returns two empty "
            "fields.\n"
            "- %s\n"
            "- A quoted field that is never closed raises `ValueError`."
            % (delimiter, delimiter, rule)),
        names=("parse_row",), calls=calls,
        raises=[("parse_row(%r)" % '"unclosed', ValueError),
                ("parse_row(%r)" % ('a%s"b' % delimiter), ValueError)]
        + ([("parse_row(%r)" % 'ab"cd"', ValueError)] if strict else []),
        refs=(_ref_parse_row,),
        consts={"_DELIMITER": delimiter, "_STRICT_QUOTES": strict},
        params={"delimiter": delimiter, "strict_quotes": strict})

def _ref_expand(text, values):
    out = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "{":
            if text[index + 1:index + 2] == "{":
                out.append("{")
                index += 2
                continue
            end = text.find("}", index)
            if end == -1:
                raise ValueError("unclosed placeholder")
            name = text[index + 1:end]
            if not name:
                raise ValueError("empty placeholder")
            if name in values:
                out.append(str(values[name]))
            elif _MISSING == "raise":
                raise ValueError("no value for %r" % name)
            else:
                out.append(_DEFAULT)
            index = end + 1
            continue
        if character == "}":
            if text[index + 1:index + 2] == "}":
                out.append("}")
                index += 2
                continue
            raise ValueError("unmatched closing brace")
        out.append(character)
        index += 1
    return "".join(out)


@family("template_expansion", 3)
def _build_template_expansion(rng):
    missing = rng.choice(("raise", "default"))
    default = rng.choice(("", "?", "<missing>"))
    supplied = {"name": "ada", "count": 3, "city": "Lovelace"}

    def template():
        pieces = []
        for _ in range(rng.randint(1, 5)):
            pieces.append(rng.choice((
                "hello", " ", "{name}", "{count}", "{city}", "{{", "}}", "!",
                "-")))
        return "".join(pieces)

    calls = ["expand(%r, %r)" % (template(), supplied) for _ in range(8)]
    calls += ["expand('', %r)" % supplied,
              "expand('plain text', %r)" % supplied,
              "expand('{name}', %r)" % supplied,
              "expand('{count}', %r)" % supplied,
              "expand('{{name}}', %r)" % supplied,
              "expand('{{{name}}}', %r)" % supplied,
              "expand('a{name}b{city}c', %r)" % supplied,
              "expand('}}', %r)" % supplied,
              "expand('{name}{name}', %r)" % supplied]
    if missing == "default":
        calls.append("expand('{nope}', %r)" % supplied)
    rule = ("A placeholder naming a key that is not in `values` raises "
            "`ValueError`." if missing == "raise" else
            "A placeholder naming a key that is not in `values` is replaced by "
            "%r." % default)
    return dict(
        prompt=(
            "Write `expand(text, values)`, substituting `{name}` placeholders "
            "from the `values` dict. Do not use `str.format`, `string.Template`, "
            "or a regular expression that does the whole job for you.\n\n"
            "- `{key}` is replaced by `str(values[key])`.\n"
            "- `{{` produces one literal `{`, and `}}` produces one literal "
            "`}`.\n"
            "- Placeholder names are used exactly as written; no stripping, no "
            "nesting, no attribute or index access.\n"
            "- %s\n"
            "- Raise `ValueError` for `{}` with an empty name, a `{` that is "
            "never closed, and a single `}` that is not part of `}}`." % rule),
        names=("expand",), calls=calls,
        raises=[("expand('{', %r)" % supplied, ValueError),
                ("expand('{}', %r)" % supplied, ValueError),
                ("expand('a}b', %r)" % supplied, ValueError),
                ("expand('{name', %r)" % supplied, ValueError),
                # Every unclosed-`{` case above still raises ValueError when the
                # unclosed check itself is deleted -- from the empty-name branch or
                # the missing-key branch instead -- so none of them tests the check.
                # `'{namex'` does: its stem `'name'` is a supplied key, so an
                # implementation that looks the name up before checking for `}`
                # substitutes it and then rescans from index 0 forever.
                ("expand('{namex', %r)" % supplied, ValueError)]
        + ([("expand('{nope}', %r)" % supplied, ValueError)]
           if missing == "raise" else []),
        refs=(_ref_expand,),
        consts={"_MISSING": missing, "_DEFAULT": default},
        params={"missing": missing, "default": default})

# ------------------------------------------------------------------- generation

def _seed_for(seed, name, index):
    """A per-task seed derived from the family name, not its position.

    Hashing the name means adding, removing or reordering a family leaves every
    other family's tasks byte-identical, so two runs of the eval stay comparable
    across a change to this file.
    """
    digest = hashlib.sha256(("%s|%s|%d" % (seed, name, index)).encode("utf-8"))
    return int(digest.hexdigest()[:16], 16)


def build_task(fam, seed, index):
    rng = random.Random(_seed_for(seed, fam.name, index))
    spec = fam.builder(rng)
    reference = _source(spec["refs"], spec.get("consts", {}),
                        spec.get("imports", ()))
    tests = _bake(reference, spec["names"], spec["calls"],
                  spec.get("raises", ()))
    prompt = spec["prompt"].strip() + "\n"
    for banned in ("assert ", "\ndef ", "```"):
        if banned in prompt:
            raise ValueError("%s prompt leaks implementation or tests (%r)"
                             % (fam.name, banned))
    return Task(family=fam.name, tier=fam.tier, variant=index,
                task_id="%s-%02d" % (fam.name.replace("_", "-"), index + 1),
                prompt=prompt, names=tuple(spec["names"]), reference=reference,
                tests=tests, params=spec.get("params", {}), seed=seed)


def generate(seed=0, per_family=2, tiers=None, families=None, limit=None):
    """Every selected family, `per_family` variants each, in a stable order."""
    tasks = []
    for fam in FAMILIES:
        if tiers and fam.tier not in tiers:
            continue
        if families and fam.name not in families:
            continue
        for index in range(per_family):
            tasks.append(build_task(fam, seed, index))
    tasks.sort(key=lambda task: (task.tier, task.family, task.variant))
    return tasks[:limit] if limit else tasks

# ------------------------------------------------------------------ self-check

def run_suite(solution_source, tests, out=None):
    """Run `tests` against `solution_source` as if it were the module `solution`.

    The suite does `from solution import ...`, so a real module object has to
    exist in ``sys.modules``. It is removed afterwards: leaving eighteen
    different `solution` modules behind would make the next import order-
    dependent, which is exactly the kind of bug a self-check must not have.

    Returns whatever the suite wrote to stdout, which is where its per-check
    report goes. Captured rather than passed through because the battery runs
    every suite six times: printed, that is tens of thousands of report lines
    between the caller and its own output.

    Pass `out` -- any writable text buffer -- to read the report on the paths
    where the suite raises, which is every path a failing candidate takes and so
    exactly where the per-check count is worth having. Callers that only care
    about the passing case can ignore it and use the return value.
    """
    module = types.ModuleType("solution")
    previous = sys.modules.get("solution")
    sys.modules["solution"] = module
    captured = io.StringIO() if out is None else out
    try:
        with contextlib.redirect_stdout(captured):
            exec(compile(solution_source, "<solution>", "exec"), module.__dict__)
            exec(compile(tests, "<test_solution>", "exec"),
                 {"__name__": "__main__"})
    finally:
        if previous is None:
            sys.modules.pop("solution", None)
        else:
            sys.modules["solution"] = previous
    return captured.getvalue()


def _stub(names):
    """A module that defines every required name and computes nothing."""
    return "".join("def %s(*args, **kwargs):\n    return None\n\n" % name
                   for name in names)

# ----------------------------------------------- the self-check's mutation battery
#
# One stub was never enough. `run_eval.StubModel._wrong` -- the strongest wrong
# implementation this tree can produce -- applies the *first* applicable of the
# edits below and then stops, so a suite can catch that one edit and stay blind to
# the other four. That is not hypothetical: `eval/calibrate.py --stub broken` hands
# `validation-01` and `validation-02` a wrong solution their own suites pass, while
# `--self-check` calls the same suites sound because the only thing it ever tried
# was the vacuous stub, which every suite catches. The lock then records the weaker
# claim in the words of the stronger one.
#
# Applying each edit independently makes "every suite fails a wrong implementation"
# a claim about that stub's whole closure rather than about whichever edit it
# happened to reach for a given parameterisation. Deliberately *not* AST mutation
# scoring, which stays deferred: five named textual edits plus the vacuous stub, so
# what the lock promises is legible from the lock. `--write-lock` records the names
# and a lock recording a different battery fails `--verify-lock`.
STUB_EDITS = (
    ("raise_to_pass", "raise", None),
    ("ge_to_gt", ">=", ">"),
    ("le_to_lt", "<=", "<"),
    ("or_to_and", " or ", " and "),
    ("sorted_to_list", "sorted(", "list("),
)

VACUOUS_STUB = "vacuous_stub"
BATTERY = (VACUOUS_STUB,) + tuple(name for name, _, _ in STUB_EDITS)

# A mutation can turn a terminating loop into a non-terminating one -- three in the
# current set do -- so the battery needs a stop, and the vacuous stub never needed
# one. It counts line events rather than taking a wall clock: the self-check's
# verdict is recorded in tasks.lock, and a verdict that depends on how busy the
# machine was is not something to freeze. The worst legitimate suite in the current
# set spends 4,506 events, so this is 55x headroom, and a runaway is stopped in
# about 40ms.
RUNAWAY_BUDGET = 250000

# Only these two frames are counted, so the budget means the same thing on every
# interpreter; a count that included stdlib frames would drift with the standard
# library and the lock would stop being reproducible across versions.
_TRACED = ("<solution>", "<test_solution>")


class _Runaway(BaseException):
    """A mutant that stopped terminating.

    `BaseException`, so neither a generated `except ValueError` nor a future
    reference with a broad `except Exception` can swallow the stop and hand the
    battery back a hang.
    """


# Mutants no assertion can catch, each with the argument for it. An exemption is
# not "we could not think of a test"; it is the claim that the mutant computes the
# same function as the reference on every input the prompt admits, so demanding the
# suite catch it would be demanding an assertion that has to be false. Recorded in
# tasks.lock so the list is frozen and reviewable rather than a quiet skip.
EQUIVALENT_MUTANTS = {
    ("tiered_pricing", "le_to_lt"): (
        "`if units <= previous: break` guards a bracket whose contribution is "
        "`(min(units, limit) - previous) * rate`, exactly 0 when units == "
        "previous, and the trailing `units > previous` top-up is then False too. "
        "So `<` charges what `<=` charges for every legal input. Swept over "
        "0..max_limit+200 for both variants: 0 disagreements."),
}

# `unreachable` is computed, `equivalent` is asserted and reviewed, and
# `not_applicable` is a property of the reference's text. None of the three is a
# suite defect, which is why they are named separately from `survived`.
MUTATION_SKIPS = ("not_applicable", "unreachable", "equivalent")


def _mutate(source, mutation):
    """`(mutant, lines)` for one named edit, or `(None, ())` where it does not apply.

    Each edit is applied exactly the way `StubModel._wrong` applies it -- every
    `raise` line for `raise_to_pass`, the first occurrence only for the other four
    -- so a passing battery *implies* that no suite passes `--stub broken`, whatever
    parameterisation decides which edit that stub reaches first. Applying them any
    harder would be mutation scoring, which is deferred, and would fail suites for
    wrong implementations nothing in this tree can produce.
    """
    if mutation == VACUOUS_STUB:
        return None, ()
    find, replace = dict((name, (f, r)) for name, f, r in STUB_EDITS)[mutation]
    if find == "raise":
        lines, edited, hit = source.splitlines(), [], []
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("raise "):
                edited.append(line[:len(line) - len(stripped)] + "pass")
                hit.append(index + 1)
            else:
                edited.append(line)
        if not hit:
            return None, ()
        return "\n".join(edited) + "\n", tuple(hit)
    if find not in source:
        return None, ()
    head = source.index(find)
    return source.replace(find, replace, 1), (source.count("\n", 0, head) + 1,)


def _run_traced(solution_source, tests, budget=RUNAWAY_BUDGET, out=None):
    """`run_suite` under a line-event budget. Returns the solution lines that ran."""
    spent = [0]
    seen = set()

    def trace(frame, event, arg):
        name = frame.f_code.co_filename
        if name not in _TRACED:
            return None
        spent[0] += 1
        if spent[0] > budget:
            raise _Runaway("stopped after %d line events" % budget)
        if name == "<solution>":
            seen.add(frame.f_lineno)
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        run_suite(solution_source, tests, out=out)
    finally:
        sys.settrace(previous)
    return seen


def suite_rejects(solution_source, tests, budget=RUNAWAY_BUDGET):
    """`(rejected, why)` -- does this suite fail this solution?

    A runaway counts as rejected: a candidate that stops terminating fails on the
    child's timeout in the real harness, so treating it as passing here would be the
    one verdict the pipeline can never deliver.
    """
    try:
        _run_traced(solution_source, tests, budget)
    except _Runaway:
        return True, "stopped terminating"
    except Exception as exc:
        return True, type(exc).__name__
    return False, ""


def battery_verdicts(task, budget=RUNAWAY_BUDGET):
    """`(mutation, verdict, detail)` per battery member, in battery order.

    `verdict` is `caught`, `survived`, or one of `MUTATION_SKIPS`. The reference is
    traced first so a mutation that edits a line the reference never executes under
    its own suite is reported as `unreachable` rather than as a toothless suite:
    such an edit cannot change an observable answer, so no assertion can catch it
    and charging the suite for it would be a false failure -- the same shape of
    error as charging the Executor for the harness's own denial.
    """
    reachable = _run_traced(task.reference, task.tests, budget)
    verdicts = []
    for mutation in BATTERY:
        lines = ()
        if mutation == VACUOUS_STUB:
            source = _stub(task.names)
        else:
            source, lines = _mutate(task.reference, mutation)
            if source is None:
                verdicts.append((mutation, "not_applicable",
                                 "the reference does not contain the edit site"))
                continue
            if not set(lines) & reachable:
                verdicts.append((mutation, "unreachable",
                                 "reference line%s %s never run under the "
                                 "reference's own suite"
                                 % ("" if len(lines) == 1 else "s",
                                    ", ".join(str(n) for n in lines))))
                continue
            if (task.family, mutation) in EQUIVALENT_MUTANTS:
                verdicts.append((mutation, "equivalent",
                                 EQUIVALENT_MUTANTS[(task.family, mutation)]))
                continue
        rejected, why = suite_rejects(source, task.tests, budget)
        if rejected:
            verdicts.append((mutation, "caught", why))
        else:
            verdicts.append((mutation, "survived",
                             "" if not lines
                             else "at reference line %s"
                             % ", ".join(str(n) for n in lines)))
    return verdicts


def battery_record():
    """What `--write-lock` records about the battery.

    The names, the budget and the exemptions, because the lock's guarantee is only
    as strong as the battery that produced it. Recording "the self-check passed"
    without recording what it ran is how the old lock came to promise more than the
    code delivered.
    """
    return {"mutations": list(BATTERY),
            "runaway_budget": RUNAWAY_BUDGET,
            "equivalent_mutants": sorted("%s:%s" % key
                                         for key in EQUIVALENT_MUTANTS)}


def self_check(tasks, verbose=False):
    """Prove each suite is worth grading against, and report what failed.

    Two properties, and a suite is useless without both: it passes against the
    reference (so a correct solution is not failed) and it *fails* against every
    wrong implementation in `BATTERY` (so an incorrect one is not passed). The
    second leg used to be one vacuous stub, which catches a suite full of
    ``assert callable(f)`` and nothing subtler; the battery is what makes it catch a
    suite that cannot tell a rejected prefix from a rejected checksum.
    """
    failures = []
    for task in tasks:
        try:
            run_suite(task.reference, task.tests)
        except Exception as exc:
            failures.append("%s: reference fails its own suite: %s: %s"
                            % (task.task_id, type(exc).__name__, exc))
            continue
        verdicts = battery_verdicts(task)
        survived = [(name, detail) for name, verdict, detail in verdicts
                    if verdict == "survived"]
        for name, detail in survived:
            if name == VACUOUS_STUB:
                failures.append("%s: suite passes against a stub - it is vacuous"
                                % task.task_id)
            else:
                failures.append(
                    "%s: suite passes the %s mutant %s - a wrong implementation "
                    "would be graded as correct" % (task.task_id, name, detail))
        if survived:
            continue
        for banned in ("assert ", "\ndef ", "```"):
            if banned in task.prompt:
                failures.append("%s: prompt leaks %r" % (task.task_id, banned))
        if len(task.tests.splitlines()) < 6:
            failures.append("%s: suite is suspiciously thin (%d lines)"
                            % (task.task_id, len(task.tests.splitlines())))
        if verbose:
            caught = sum(1 for _, verdict, _ in verdicts if verdict == "caught")
            skipped = ["%s:%s" % (name, verdict) for name, verdict, _ in verdicts
                       if verdict in MUTATION_SKIPS]
            print("  ok  %-28s tier %d  %2d asserts  battery %d/%d caught%s"
                  % (task.task_id, task.tier, task.tests.count("assert "),
                     caught, len(BATTERY),
                     ("  skipped %s" % ", ".join(skipped)) if skipped else ""))
    failures.extend(check_prompt_fairness(tasks))
    return failures


def check_prompt_fairness(tasks, peers=6):
    """Two tasks with the same prompt must want the same behaviour.

    The prompt is the whole of what the pipeline is told, so if two variants of
    a family share a prompt but disagree on an answer, the task is unfair --
    it is graded on a parameter the solver was never given. Identical prompts
    are legitimate (`alphabet` only picks test inputs; `default` is dead when
    `missing == "raise"`), which is why the check is behavioural rather than a
    comparison of the parameter dicts: each reference must satisfy the others'
    suites.
    """
    groups = {}
    for task in tasks:
        groups.setdefault((task.family, task.prompt), []).append(task)
    failures = []
    for (family, _), group in sorted(groups.items()):
        for task in group[:peers]:
            for other in group[:peers]:
                if other.task_id == task.task_id:
                    continue
                try:
                    run_suite(task.reference, other.tests)
                except Exception as exc:
                    failures.append(
                        "%s: same prompt as %s but disagrees on behaviour "
                        "(%s: %s) - params %s vs %s"
                        % (task.task_id, other.task_id, type(exc).__name__,
                           exc, json.dumps(task.params, sort_keys=True),
                           json.dumps(other.params, sort_keys=True)))
                    break
    return failures

# ------------------------------------------------------------------- tasks.lock

# Written once, on purpose, by `--write-lock`. Never regenerated as a side effect
# of anything: a lock that rewrites itself when it disagrees with the tree records
# nothing at all, and the failure it exists to catch -- the hidden suite moving
# under a half-finished grid -- is exactly the case where the convenient thing to
# do is to rewrite it.
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "tasks.lock")
LOCK_VERSION = 1

# Full sha256, not the 16-hex prefix `run_eval._suite_hash` uses for the visible
# gate suite. That one is an identity label inside one run's results; this one is
# an integrity record that has to survive being compared across months, and the
# two are deliberately different lengths so nobody compares them by accident.
def digest(text):
    """sha256 of one task part, hex. Empty text hashes to the empty string."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The three parts, hashed separately rather than together. A single combined
# digest would say "something moved" and stop there; the point of the lock is to
# name which of the three, because a moved prompt, a moved suite and a moved
# reference are three different accidents with three different consequences.
DIGEST_PARTS = ("prompt", "tests", "reference")


def task_digests(task):
    return dict(("%s_sha256" % part, digest(getattr(task, part)))
                for part in DIGEST_PARTS)


def _lock_body(tasks, generator, self_check):
    by_tier = {}
    for task in tasks:
        by_tier[str(task.tier)] = by_tier.get(str(task.tier), 0) + 1
    entries = []
    for task in tasks:
        entry = {"task_id": task.task_id, "family": task.family,
                 "tier": task.tier, "variant": task.variant}
        entry.update(task_digests(task))
        entries.append(entry)
    return {
        "version": LOCK_VERSION,
        "digest_algorithm": "sha256",
        "generator": generator,
        "counts": {"tasks": len(tasks),
                   "families": len(set(t.family for t in tasks)),
                   "by_tier": by_tier},
        "self_check": self_check,
        "tasks": sorted(entries, key=lambda e: e["task_id"]),
    }


def _canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def body_digest(body):
    """The digest of everything except the digest field itself.

    So a hand-edited lock -- one digest quietly updated to match a suite somebody
    changed -- is detectable as an edit, not just as agreement.
    """
    without = dict((name, value) for name, value in body.items()
                   if name != "body_sha256")
    return hashlib.sha256(_canonical(without).encode("utf-8")).hexdigest()


def generator_args(seed=0, per_family=2, tiers=None, families=None, limit=None):
    """The selection that produced a task set, as it goes into the lock.

    Recorded in full and not just as the seed: `generate` takes five arguments
    and four of them change which tasks exist, so a lock that records only the
    seed cannot be regenerated from itself.
    """
    return {"seed": seed, "per_family": per_family,
            "tiers": sorted(tiers or ()), "families": sorted(families or ()),
            "limit": limit}


def generate_from(generator):
    """The task set a lock's own `generator` block describes."""
    return generate(seed=generator["seed"],
                    per_family=generator["per_family"],
                    tiers=set(generator.get("tiers") or ()),
                    families=set(generator.get("families") or ()),
                    limit=generator.get("limit"))


def write_lock(path=None, seed=0, per_family=2, tiers=None, families=None,
               limit=None, verbose=False):
    """Run the self-check, then record the task set. Returns (path, failures).

    The self-check is not optional here and its result is recorded with the time
    it was obtained. "We validated the task set" was previously a claim about
    something somebody ran once; this is that claim, dated, next to the digests
    of the exact tasks it was run against. A failing self-check refuses to write:
    a lock over a task set that cannot grade anything is worse than no lock,
    because everything downstream would then verify happily against it.
    """
    path = path or LOCK_PATH
    generator = generator_args(seed, per_family, tiers, families, limit)
    tasks = generate_from(generator)
    if not tasks:
        return path, ["no tasks selected, so there is nothing to lock"]
    failures = self_check(tasks, verbose=verbose)
    if failures:
        return path, failures
    body = _lock_body(tasks, generator, {
        "passed": True,
        "tasks": len(tasks),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "gen_tasks.py --write-lock",
        # What the claim is worth, next to the claim. A lock that said only
        # "passed" made a promise whose strength nobody could read off it.
        "battery": battery_record(),
    })
    body["body_sha256"] = body_digest(body)
    with open(path, "w") as handle:
        json.dump(body, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path, []


def load_lock(path=None):
    """The lock, or `None` when there is no file. Malformed JSON raises."""
    path = path or LOCK_PATH
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _index(lock):
    return dict((entry["task_id"], entry) for entry in lock.get("tasks", ()))


def verify_lock(lock, tasks=None, seed=None):
    """Every way the locked task set can have moved. Returns failure strings.

    Three independent legs, and all three are needed:

      * regenerate from the lock's own `generator` block and compare every
        digest. This catches an edit to `gen_tasks.py` -- a changed reference, a
        retuned parameter, a reworded prompt -- which is the case that silently
        invalidates a half-finished grid.
      * compare the task set a *run* actually selected against the lock. This
        catches a run that generated tasks the lock never saw: a different seed,
        a wider `--per-family`, a family added since the lock was written.
      * compare the self-check battery the lock records against the one this code
        runs. Every digest can match while the claim behind them has weakened,
        because "the suites fail a wrong implementation" means whatever the battery
        that ran means.

    Named per task and per part, because "a digest moved" is not actionable and
    "aggregation-01's tests digest moved" is.
    """
    failures = []
    if not isinstance(lock, dict):
        return ["tasks.lock is not a JSON object"]
    if lock.get("version") != LOCK_VERSION:
        failures.append("tasks.lock version is %r, this code writes %d"
                        % (lock.get("version"), LOCK_VERSION))
        return failures
    recorded = lock.get("body_sha256")
    actual = body_digest(lock)
    if recorded != actual:
        failures.append(
            "tasks.lock was edited by hand: body_sha256 is %s but its contents "
            "hash to %s. Regenerate it with --write-lock if that was deliberate."
            % (recorded, actual))
    if not lock.get("self_check", {}).get("passed"):
        failures.append("tasks.lock records no passing --self-check, so the "
                        "suites it locks were never shown to grade anything")
    recorded_battery = lock.get("self_check", {}).get("battery")
    current_battery = battery_record()
    if recorded_battery != current_battery:
        failures.append(
            "tasks.lock records the self-check battery %s and this code runs %s. "
            "The lock's guarantee is exactly as strong as the battery that "
            "produced it, so a moved battery voids it even when every digest "
            "matches: re-run --write-lock."
            % (json.dumps(recorded_battery, sort_keys=True),
               json.dumps(current_battery, sort_keys=True)))

    index = _index(lock)
    regenerated = generate_from(lock.get("generator") or generator_args())
    failures.extend(_compare(index, regenerated, "gen_tasks.py"))
    if lock.get("counts", {}).get("tasks") != len(regenerated):
        failures.append("tasks.lock records %s task(s), the generator now "
                        "produces %d"
                        % (lock.get("counts", {}).get("tasks"),
                           len(regenerated)))

    if tasks is not None:
        locked_seed = (lock.get("generator") or {}).get("seed")
        if seed is not None and seed != locked_seed:
            failures.append(
                "this run uses seed %s and tasks.lock was written for seed %s. "
                "The task ids are the same and their contents are not, so the "
                "lock cannot vouch for this run: write a lock for seed %s."
                % (seed, locked_seed, seed))
        failures.extend(_compare(index, tasks, "this run's selection",
                                 subset=True))
    return failures


def _compare(index, tasks, source, subset=False):
    """Digest-by-digest, naming the task and the part that moved."""
    failures = []
    for task in tasks:
        entry = index.get(task.task_id)
        if entry is None:
            failures.append("%s: %s produced a task that tasks.lock does not "
                            "contain" % (task.task_id, source))
            continue
        for part in DIGEST_PARTS:
            key = "%s_sha256" % part
            now = digest(getattr(task, part))
            if entry.get(key) != now:
                failures.append(
                    "%s: the %s digest moved (locked %s, %s now produces %s)"
                    % (task.task_id, part, str(entry.get(key))[:16], source,
                       now[:16]))
    if not subset:
        produced = set(task.task_id for task in tasks)
        for task_id in sorted(set(index) - produced):
            failures.append("%s: tasks.lock contains it and %s no longer "
                            "produces it" % (task_id, source))
    return failures


def verify_lock_or_reason(tasks=None, seed=None, path=None):
    """`(failures, reason)` -- `reason` set when there is no lock to verify.

    Separated from the failure list because "the lock disagrees" and "there is no
    lock" call for different words at a caller that has to refuse either way.
    """
    path = path or LOCK_PATH
    try:
        lock = load_lock(path)
    except ValueError as exc:
        return ["%s is not readable JSON: %s" % (path, exc)], None
    if lock is None:
        return [], ("no %s, so nothing states which task set this is. Write one "
                    "with `python3 eval/gen_tasks.py --write-lock`."
                    % os.path.basename(path))
    return verify_lock(lock, tasks=tasks, seed=seed), None

# -------------------------------------------------------------------------- cli

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Generate deterministic eval tasks, family-first.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-family", type=int, default=2,
                        help="variants per family (default 2)")
    parser.add_argument("--tier", type=int, action="append", dest="tiers",
                        choices=(1, 2, 3), help="repeatable; default all")
    parser.add_argument("--family", action="append", dest="families",
                        help="repeatable; default all")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default=None,
                        help="write tasks as JSON to this path")
    parser.add_argument("--list", action="store_true",
                        help="one line per family")
    parser.add_argument("--show", default=None, metavar="TASK_ID",
                        help="print one task's prompt, reference and suite")
    parser.add_argument("--self-check", action="store_true",
                        help="verify every suite passes its reference and fails "
                             "every applicable mutation in the battery")
    parser.add_argument("--write-lock", action="store_true",
                        help="run --self-check and record the task set in "
                             "eval/tasks.lock: per task the sha256 of the "
                             "prompt, the hidden suite and the reference. "
                             "Deliberate and explicit; nothing else writes it.")
    parser.add_argument("--verify-lock", action="store_true",
                        help="regenerate from the lock's own recorded seed and "
                             "fail on any digest that moved, naming the task "
                             "and the part")
    parser.add_argument("--lock", default=None, metavar="PATH",
                        help="lock file to write or verify (default "
                             "eval/tasks.lock)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _cmd_list():
    by_tier = {}
    for fam in FAMILIES:
        by_tier.setdefault(fam.tier, []).append(fam.name)
    for tier in sorted(by_tier):
        print("tier %d - %s" % (tier, TIER_NAMES[tier]))
        for name in by_tier[tier]:
            print("    %s" % name)
    print("\n%d families across %d tiers" % (len(FAMILIES), len(by_tier)))


def _cmd_show(tasks, task_id):
    for task in tasks:
        if task.task_id == task_id:
            print("=== %s (family %s, tier %d, params %s)"
                  % (task.task_id, task.family, task.tier,
                     json.dumps(task.params, sort_keys=True)))
            print("\n--- prompt (this is all the pipeline sees)\n")
            print(task.prompt)
            print("--- hidden reference\n")
            print(task.reference)
            print("--- hidden suite\n")
            print(task.tests)
            return 0
    print("no task %r; try --list or --out" % task_id, file=sys.stderr)
    return 2


def _cmd_write_lock(args):
    """`--write-lock`. Refuses on a failing self-check rather than recording it."""
    path, failures = write_lock(path=args.lock, seed=args.seed,
                               per_family=args.per_family,
                               tiers=set(args.tiers or ()),
                               families=set(args.families or ()),
                               limit=args.limit, verbose=args.verbose)
    if failures:
        print("refusing to write %s: the self-check does not pass, and a lock "
              "over a task set that cannot grade anything would make every "
              "later --verify-lock agree with a broken tree." % path,
              file=sys.stderr)
        for line in failures:
            print("FAIL %s" % line, file=sys.stderr)
        return 1
    lock = load_lock(path)
    print("wrote %s" % path)
    print("  %d task(s), %d famil%s, tiers %s"
          % (lock["counts"]["tasks"], lock["counts"]["families"],
             "y" if lock["counts"]["families"] == 1 else "ies",
             ", ".join("%s=%d" % pair
                       for pair in sorted(lock["counts"]["by_tier"].items()))))
    print("  generator %s" % json.dumps(lock["generator"], sort_keys=True))
    print("  self-check passed at %s" % lock["self_check"]["at"])
    print("  battery %s" % ", ".join(lock["self_check"]["battery"]["mutations"]))
    print("  runaway budget %d line event(s), %d documented equivalent mutant(s)"
          % (lock["self_check"]["battery"]["runaway_budget"],
             len(lock["self_check"]["battery"]["equivalent_mutants"])))
    print("  body sha256 %s" % lock["body_sha256"])
    print("\nThis file is not regenerated by anything else. If a digest ever "
          "moves, that is the finding -- not a reason to rewrite the lock.")
    return 0


def _cmd_verify_lock(args):
    """`--verify-lock`. Loud, named per task and per part, and cheap."""
    path = args.lock or LOCK_PATH
    started = time.time()
    failures, reason = verify_lock_or_reason(path=path)
    if reason:
        print("cannot verify: %s" % reason, file=sys.stderr)
        return 2
    lock = load_lock(path)
    if failures:
        print("tasks.lock DOES NOT MATCH the generator (%d problem%s):"
              % (len(failures), "" if len(failures) == 1 else "s"),
              file=sys.stderr)
        for line in failures:
            print("FAIL %s" % line, file=sys.stderr)
        print("\nNothing should be measured against a task set that moved under "
              "it. Either restore gen_tasks.py, or write a new lock deliberately "
              "and treat every existing result as belonging to the old one.",
              file=sys.stderr)
        return 1
    print("tasks.lock verified: %d task(s), %d digest(s), regenerated in %.0fms"
          % (lock["counts"]["tasks"],
             len(lock["tasks"]) * len(DIGEST_PARTS),
             (time.time() - started) * 1000))
    print("  seed %s, self-check passed at %s"
          % (lock["generator"]["seed"], lock["self_check"]["at"]))
    print("  battery %s"
          % ", ".join(lock["self_check"]["battery"]["mutations"]))
    return 0


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.list:
        _cmd_list()
        return 0
    if args.verify_lock:
        return _cmd_verify_lock(args)

    known = set(fam.name for fam in FAMILIES)
    unknown = sorted(set(args.families or ()) - known)
    if unknown:
        print("unknown famil%s: %s" % ("y" if len(unknown) == 1 else "ies",
                                       ", ".join(unknown)), file=sys.stderr)
        return 2

    tasks = generate(seed=args.seed, per_family=args.per_family,
                     tiers=set(args.tiers or ()), families=set(args.families or ()),
                     limit=args.limit)

    if args.show:
        return _cmd_show(tasks, args.show)

    if args.write_lock:
        return _cmd_write_lock(args)

    if args.self_check:
        print("self-check: %d tasks from %d families"
              % (len(tasks), len(set(t.family for t in tasks))))
        failures = self_check(tasks, verbose=args.verbose)
        for line in failures:
            print("FAIL %s" % line)
        print("\n%d tasks checked, %d problem%s"
              % (len(tasks), len(failures), "" if len(failures) == 1 else "s"))
        return 1 if failures else 0

    if args.out:
        path = os.path.abspath(args.out)
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        payload = {"seed": args.seed, "per_family": args.per_family,
                   "families": len(set(t.family for t in tasks)),
                   "tasks": [t.to_dict() for t in tasks]}
        with open(path, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print("wrote %d tasks to %s" % (len(tasks), path))
        return 0

    for task in tasks:
        print("%-28s tier %d  %2d asserts  %s"
              % (task.task_id, task.tier, task.tests.count("assert "),
                 json.dumps(task.params, sort_keys=True)))
    print("\n%d tasks, %d families" % (len(tasks),
                                       len(set(t.family for t in tasks))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

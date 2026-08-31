#!/usr/bin/env python3
"""Prove that no hidden-suite content reaches any prompt.

This is the one failure that invalidates every number simultaneously. If a
hidden suite's expected values reach a prompt, the hidden pass rate for that
task stops measuring generalisation and starts measuring recall, and nothing in
the record says so -- a leaked pass and an earned pass are the same JSON. So it
is checked mechanically, over a whole sweep, and the check is written to be hard
to defeat rather than easy to pass.

    python3 eval/audit_leakage.py                        # the audit
    python3 eval/audit_leakage.py --inject value         # ... and it failing
    python3 eval/audit_leakage.py --inject test_writer   # ... on the third role
    python3 eval/audit_leakage.py --shipped-stub         # the stub is a leak

Three legs, because what defeats one does not defeat the others:

    tests_line      every hidden-suite line above a length threshold, verbatim
    expected_value  the baked expected values, recovered structurally with
                    `ast` and compared as `repr`. These are the actual secret
                    and they survive any reformatting that defeats line
                    matching -- a prompt could paraphrase every assert and
                    still hand over the answers.
    reference_line  the reference solution's own lines, same threshold

What is swept is every `(provider, system, user)` triple that reaches
`agents_core.call_model`, recorded by wrapping `run_eval.Instrument._call` --
the one callable a sweep installs there. No keys, no network.

The sweep needs a stub of its own. The shipped `StubModel` cannot be used: its
Planner echoes the first two hidden asserts into its TESTS block and its
Executor returns the reference verbatim, so it *is* a leak, and an audit run
against it reports the stub rather than the pipeline. `_AuditStub` below is
built from public material only -- `task.prompt` and `task.names` -- so anything
this audit finds was put there by a prompt builder. `--shipped-stub` runs the
real one and shows the audit catching it, which is also the evidence that the
audit is not vacuous.

Counts are printed whether or not anything leaked: prompts swept, literals
checked, literals skipped as too short, literals dropped as already public. An
audit that swept nothing has to be visible as such rather than reading as a
pass. That property is per role, not only in total: three roles build prompts
that carry task material -- Planner, Test Writer, Executor -- and the Test
Writer is only reached when a suite is rejected, so the audit runs a second
`--vacuous-plan` pass to reach it and refuses if any role it should have swept
has a count of zero.

Exit status: 0 clean, 1 something leaked, 2 the audit could not run or swept
nothing worth calling a sweep -- in total, or for any role it claimed to cover.
"""

import argparse
import ast
import collections
import contextlib
import io
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import agents_core            # noqa: E402
import gen_tasks              # noqa: E402
import run_eval               # noqa: E402

# ------------------------------------------------------------------- thresholds

# A needle short enough to occur by coincidence turns this audit into a coin
# flip, and the usual fix for a flaky audit is to delete it. So the thresholds
# sit where a match is unambiguous, and every literal dropped for being under
# them is *counted and printed* rather than quietly discarded: "found nothing"
# and "checked nothing" have to be distinguishable from the outside.
#
# 24 characters of normalised source is about `assert f([1, 2]) == 3` -- shorter
# than that and a line is mostly punctuation the prompts contain anyway.
MIN_LINE_CHARS = 24
# 8 characters of `repr` keeps `[1, 2, 3]` and drops `3`, `True`, `'ab'`. A
# single-digit expected value is indistinguishable from coincidence in a prompt
# that discusses arithmetic, and claiming otherwise would make the audit unable
# to pass on a clean tree.
MIN_VALUE_CHARS = 8

LEG_TESTS_LINE = "tests_line"
LEG_EXPECTED = "expected_value"
LEG_REFERENCE = "reference_line"
LEGS = (LEG_TESTS_LINE, LEG_EXPECTED, LEG_REFERENCE)

Needle = collections.namedtuple("Needle", "task_id leg text")
# `pass_name` says which of the audit's passes sent the prompt. Two passes are
# needed because one sweep cannot reach every prompt-building role: the Test
# Writer is only called when the Planner's own TESTS block is rejected, so a
# sweep whose suite is accepted -- the one the gate-and-repair pass wants --
# never builds a Test Writer prompt at all.
Prompt = collections.namedtuple("Prompt",
                                "index provider role task_id system user hay "
                                "pass_name")
Prompt.__new__.__defaults__ = ("",)


def _normalise(text):
    """Collapse all whitespace. Indentation and line breaks are not the secret.

    Matching on normalised text is what stops a leak that re-indents or re-wraps
    the suite from reading as clean.
    """
    return " ".join((text or "").split())


# ---------------------------------------------------------------------- needles

def _long_lines(source):
    """The normalised lines of `source` worth matching on."""
    out = []
    for line in (source or "").splitlines():
        text = _normalise(line)
        if len(text) >= MIN_LINE_CHARS:
            out.append(text)
    return out


def _expected_values(tests):
    """The baked expected values, as `repr`, recovered from the suite's AST.

    In `assert f(x) == <expected>` the comparator is the secret: it is the
    answer, and it is the one thing a leak cannot paraphrase away. Recovering it
    structurally rather than by regex means a suite that spells its literals
    differently is still covered, and comparing `repr` means the needle is the
    value's canonical spelling rather than the suite's -- so `[1,2]` in a prompt
    still matches `[1, 2]` in the suite once both are normalised.

    Returns `(values, skipped)`; `skipped` counts the ones under
    MIN_VALUE_CHARS, which are indistinguishable from coincidence.
    """
    values, skipped = [], 0
    try:
        tree = ast.parse(tests or "")
    except SyntaxError:
        return values, skipped
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        # `ast.walk` from the assert, not `node.test` alone: a comparison nested
        # inside `all(...)` or a boolean operator is still a baked answer.
        for part in ast.walk(node):
            if not isinstance(part, ast.Compare):
                continue
            for comparator in part.comparators:
                try:
                    value = ast.literal_eval(comparator)
                except (ValueError, SyntaxError, TypeError, MemoryError):
                    continue
                text = _normalise(repr(value))
                if len(text) < MIN_VALUE_CHARS:
                    skipped += 1
                    continue
                values.append(text)
    return values, skipped


def _public_text(task):
    """What a model is entitled to see, so a match on it is not evidence.

    The prompt is handed to the Executor by design. The `from solution import`
    line is determined by the names the prompt asks for, and the visible suite
    legitimately writes the same line -- without this it would be flagged as a
    leak of itself. Some prompts also state a worked example, and an expected
    value the prompt already gives away is public by construction.

    This can only ever drop needles that are *already* in the prompt, so it
    cannot hide a leak; what it drops is counted and printed.
    """
    return _normalise("%s\n%s\n%s" % (
        task.prompt,
        "from solution import %s" % ", ".join(task.names),
        "\n".join(task.names)))


def needles_for(task):
    """Every literal this task is entitled to keep secret.

    Returns `(needles, skipped_short, dropped_public)`. Duplicates are collapsed
    across legs, keeping the first leg that produced them, so one literal is one
    check and the printed count is a count of distinct literals.
    """
    values, skipped = _expected_values(task.tests)
    by_leg = ((LEG_TESTS_LINE, _long_lines(task.tests)),
              (LEG_EXPECTED, values),
              (LEG_REFERENCE, _long_lines(task.reference)))
    public = _public_text(task)
    seen, out, dropped = set(), [], 0
    for leg, texts in by_leg:
        for text in texts:
            if text in seen:
                continue
            seen.add(text)
            if text in public:
                dropped += 1
                continue
            out.append(Needle(task.task_id, leg, text))
    return out, skipped, dropped


def all_needles(tasks):
    """Needles for the whole task set, with the two skip counts summed."""
    needles, skipped, dropped = [], 0, 0
    for task in tasks:
        task_needles, task_skipped, task_dropped = needles_for(task)
        needles.extend(task_needles)
        skipped += task_skipped
        dropped += task_dropped
    return needles, skipped, dropped


# ------------------------------------------------------------------- audit stub

# The live stub instance, so an injection can find the task the prompt currently
# being built belongs to. `run_eval.main` constructs the stub itself, and
# `build_context` is handed a spec and nothing else.
_CURRENT = {"stub": None}


class _AuditStub(run_eval.StubModel):
    """A stub that has never read `task.tests` or `task.reference`.

    The shipped `StubModel` deliberately echoes the hidden suite -- its Planner's
    TESTS block is the first two hidden asserts -- and returns the reference as
    its Executor answer. Both are right for validating the plumbing and fatal
    here: every prompt downstream of them would carry a leak the pipeline did not
    cause, and the audit would be reporting the stub. This one derives everything
    from `task.prompt` and `task.names`, so a hit is attributable to a prompt
    builder.

    Its code is also deliberately wrong -- it computes nothing -- so arm B's gate
    fails, the repair rounds run and the escalation ladder is climbed. Those are
    the prompts most likely to leak, because they quote failures back at the
    model, so an audit that stopped at the happy path would be the easy audit.
    """

    def __init__(self, *args, **kwargs):
        # `super`, not `run_eval.StubModel.__init__`: this audit *replaces* that
        # module attribute with this class while a sweep runs, so naming it here
        # would resolve back to this method and recurse.
        super(_AuditStub, self).__init__(*args, **kwargs)
        _CURRENT["stub"] = self

    def _public_suite(self):
        """A visible suite built only from the names the prompt asks for.

        Non-vacuous by construction: every entry point of `harness._stub_source`
        raises, so `assert f(1) == 1` cannot pass against it and the audit trusts
        the suite. That matters -- a suite the vacuity audit rejected would send
        the sweep down the regeneration branch instead of the gate-and-repair one
        this audit wants swept.
        """
        names = list(self.task.names)
        lines = ["from solution import %s" % ", ".join(names), ""]
        for name in names:
            lines.append("assert %s(1) == 1" % name)
        return "\n".join(lines) + "\n"

    def _thin_suite(self):
        return self._public_suite()

    def _correct(self):
        return gen_tasks._stub(self.task.names)

    def _wrong(self):
        return gen_tasks._stub(self.task.names)


# ------------------------------------------------------------------ role cover

# The two passes, and what each exists to reach.
PASS_GATE = "gate"
PASS_VACUITY = "vacuity"

_ARM_ALIASES = {"all": None, "both": ("a", "b")}


def arms_of(arm):
    """The concrete arms an `--arm` value expands to. `run_eval.main`'s rule."""
    if arm == "all":
        return tuple(run_eval.ARM_ORDER)
    return _ARM_ALIASES.get(arm) or (arm,)


def required_roles(arm, vacuity):
    """Roles a sweep of these arms, with these passes, must have swept.

    Not a fixed set. `--arm a` sends one Executor call on the raw prompt and
    never calls the Planner, so demanding `planner` there would refuse a sweep
    that was complete for what it covered. And `test_writer` is only reachable
    through the vacuity pass, so it is required exactly when that pass runs.

    This exists because a total is not coverage. The first clean run of this
    audit reported 360 prompts and read as a pass while `test_writer` -- a
    prompt-building role that receives task material and routes to Gemini --
    had a swept count of zero. A total large enough to look thorough is what
    hid it, so the property is now asserted per role.
    """
    roles = set(["executor"])
    if any(name in run_eval.PLAN_ARMS for name in arms_of(arm)):
        roles.add("planner")
        if vacuity:
            roles.add("test_writer")
    return roles


def missing_roles(result):
    """Required roles with a swept count of zero, sorted. Empty is the pass."""
    swept = result["by_role"]
    return sorted(role for role in result["required_roles"]
                  if not swept.get(role))


# -------------------------------------------------------------------- recording

def _role_of(system):
    """Which role a system prompt belongs to. Same rule the stub uses."""
    for role, prompt in agents_core.PROMPTS.items():
        if (system or "").strip().startswith(prompt.strip()[:60]):
            return role
    return "?"


class _Recording(object):
    """Every prompt that reached a provider boundary, with role and task.

    Wrapping `run_eval.Instrument._call` rather than `agents_core.call_model` is
    deliberate. `Instrument.__enter__` installs its own bound `_call` at
    `agents_core.call_model`, so a wrapper put there before the sweep starts is
    overwritten and records nothing; and under a stub `_call` returns from
    `self.stub(...)` without ever reaching the function such a wrapper would have
    replaced. Wrapping the unbound method catches every call either way.

    A retried 429 records once, not once per attempt: it is the same prompt.
    """

    def __init__(self):
        self.prompts = []
        self._original = None

    def __enter__(self):
        self._original = run_eval.Instrument._call
        original, prompts = self._original, self.prompts

        def recording(instrument, provider, api_key, system, user, role=None):
            stub = getattr(instrument, "stub", None)
            task = getattr(stub, "task", None)
            prompts.append(Prompt(
                index=len(prompts), provider=provider, role=_role_of(system),
                task_id=getattr(task, "task_id", ""), system=system, user=user,
                hay=_normalise("%s\n%s" % (system, user))))
            return original(instrument, provider, api_key, system, user,
                            role=role)

        run_eval.Instrument._call = recording
        return self

    def __exit__(self, *exc_info):
        run_eval.Instrument._call = self._original
        return False


# -------------------------------------------------------------------- injection

INJECTIONS = {"line": LEG_TESTS_LINE, "value": LEG_EXPECTED,
              "reference": LEG_REFERENCE, "test_writer": LEG_TESTS_LINE}

# Which builder each mode leaks from.
#
# `build_context` is the Executor's context and covers three of the four arms.
# The Test Writer needs a different site, because there is no Test Writer prompt
# *builder* to wrap: `agents_core._resolve_tests` assembles that prompt inline
# and the task material in it arrives by being quoted -- the rejected TESTS
# block goes back verbatim so the model can be told what was wrong with it. So
# the honest injection point is the Planner's rejected suite, and the leak is
# carried by the real code path rather than by an append this audit performs on
# the prompt itself. A comment does not change a vacuity verdict, so the suite
# is still rejected and still quoted.
SITE_CONTEXT = "build_context"
SITE_VACUOUS = "vacuous_suite"
INJECT_SITE = {"line": SITE_CONTEXT, "value": SITE_CONTEXT,
               "reference": SITE_CONTEXT, "test_writer": SITE_VACUOUS}


def _injectable(task, mode):
    """A literal from the requested leg that this audit would actually flag.

    Not any literal: one the public filter kept. Injecting something the audit
    deliberately ignores would prove nothing while looking like a demonstration.
    """
    needles, _, _ = needles_for(task)
    for needle in needles:
        if needle.leg == INJECTIONS[mode]:
            return needle.text
    return ""


class _Injection(object):
    """Leak a hidden literal from a real prompt builder, on purpose.

    `agents_core.build_context` is the builder every Executor context goes
    through -- arm B's steps, and `run_eval._spec_only_prompt` for A' and A'@3 --
    so this is where a careless "add a bit more context" would land. Arm A sends
    `task.prompt` raw and the Planner turn does not go through it, so those two
    prompt shapes carry no injected literal; the audit sweeps them regardless,
    which is the point of sweeping rather than reasoning about it.
    """

    def __init__(self, mode):
        self.mode = mode
        self.injected = 0
        self.tasks = set()
        self._original = None

    def __enter__(self):
        self._original = agents_core.build_context
        original = self._original

        def injecting(spec, completed):
            text = original(spec, completed)
            stub = _CURRENT.get("stub")
            task = getattr(stub, "task", None)
            secret = _injectable(task, self.mode) if task is not None else ""
            if not secret:
                return text
            self.injected += 1
            self.tasks.add(task.task_id)
            return "%s\n\n# --inject %s:\n# %s\n" % (text, self.mode, secret)

        agents_core.build_context = injecting
        return self

    def __exit__(self, *exc_info):
        agents_core.build_context = self._original
        return False


class _VacuousInjection(object):
    """Leak a hidden literal into the Planner's rejected TESTS block, on purpose.

    Same contract as `_Injection`, different site. `run_eval.StubModel`'s
    `_vacuous_suite` is what the vacuity pass makes the Planner emit; the real
    `_resolve_tests` then rejects it and quotes it back at the Test Writer, so a
    literal put here reaches the Test Writer's prompt through the pipeline's own
    code rather than through this file. The unbound method is replaced, as in
    `_Recording`, because the stub instance is built inside the sweep.

    It should reach *only* that role. The rejected suite is discarded once a
    replacement arrives, so the Executor is graded and repaired against the
    regenerated suite, which is public by construction.
    """

    def __init__(self, mode):
        self.mode = mode
        self.injected = 0
        self.tasks = set()
        self._original = None
        self._target = None
        self._shadowed = False

    def __enter__(self):
        # The concrete class, captured now: `_preserved` has already pointed
        # `run_eval.StubModel` at `_AuditStub`, and restoring by that name later
        # would leave the patch on whatever the name meant at exit.
        self._target = run_eval.StubModel
        self._shadowed = "_vacuous_suite" in vars(self._target)
        self._original = self._target._vacuous_suite
        original, injection = self._original, self

        def injecting(stub):
            text = original(stub)
            task = getattr(stub, "task", None)
            secret = _injectable(task, injection.mode) if task is not None else ""
            if not secret:
                return text
            injection.injected += 1
            injection.tasks.add(task.task_id)
            return "%s# --inject %s:\n# %s\n" % (text, injection.mode, secret)

        self._target._vacuous_suite = injecting
        return self

    def __exit__(self, *exc_info):
        if self._shadowed:
            self._target._vacuous_suite = self._original
        else:
            delattr(self._target, "_vacuous_suite")
        return False


def _injection_for(mode):
    """The injector for this mode, or None. Two sites, one flag."""
    if not mode:
        return None
    if INJECT_SITE[mode] == SITE_VACUOUS:
        return _VacuousInjection(mode)
    return _Injection(mode)


# ------------------------------------------------------------------------- scan

Hit = collections.namedtuple("Hit", "needle prompt")


def scan(prompts, needles):
    """Which needles appear in which prompts.

    Every needle is checked against every prompt, not only against the prompts of
    its own task: one task's suite reaching another task's prompt is still a leak,
    and it is the shape a shared-context bug would take.

    Two passes, because a clean tree is the common case: one against every prompt
    joined, then a second only for needles that hit, to name the prompt.
    """
    joined = "\n\x00\n".join(prompt.hay for prompt in prompts)
    hits = []
    for needle in needles:
        if needle.text not in joined:
            continue
        for prompt in prompts:
            if needle.text in prompt.hay:
                hits.append(Hit(needle, prompt))
    return hits


# ------------------------------------------------------------------------ sweep

@contextlib.contextmanager
def _scoped_runs(runs_dir):
    """Redirect arm B's run JSON out of the repository's own `runs/`.

    `--out` moves the results; the workspace run log goes through
    `agents_core.Memory`, which has its own default directory.
    """
    original = agents_core.Memory

    class ScopedMemory(original):
        def __init__(self, *args, **kwargs):
            kwargs["dirpath"] = runs_dir
            original.__init__(self, *args, **kwargs)

    agents_core.Memory = ScopedMemory
    try:
        yield
    finally:
        agents_core.Memory = original


@contextlib.contextmanager
def _preserved(shipped_stub):
    """Install the audit stub and put every global the sweep moves back.

    `run_eval.main` sets measurement mode, pins `agents_core`'s retry attempts and
    installs a stub class it looks up by module attribute. This audit is also
    imported by the offline suite, where leaving any of that changed would make
    the next check depend on this one having run.
    """
    stub_class = run_eval.StubModel
    mode = agents_core.MEASUREMENT_MODE
    attempts = agents_core.retry_attempts()
    if not shipped_stub:
        run_eval.StubModel = _AuditStub
    try:
        yield
    finally:
        run_eval.StubModel = stub_class
        agents_core.set_measurement_mode(mode)
        agents_core.set_retry_attempts(attempts)
        agents_core.reset_call_log()
        _CURRENT["stub"] = None


def sweep(argv, shipped_stub=False, inject=None, verbose=False):
    """One offline `run_eval.main`, recording every prompt it sends.

    Returns `(exit_code, prompts, injection, output)`. Nothing here touches a
    network or reads a key: the stub answers every call.
    """
    recording = _Recording()
    injection = _injection_for(inject)
    buffer = io.StringIO()
    with _preserved(shipped_stub), recording:
        with (injection if injection else contextlib.nullcontext()):
            with _scoped_runs(os.path.join(_out_of(argv), "runs")):
                if verbose:
                    code = run_eval.main(argv)
                else:
                    with contextlib.redirect_stdout(buffer):
                        code = run_eval.main(argv)
    return code, recording.prompts, injection, buffer.getvalue()


def _out_of(argv):
    return argv[argv.index("--out") + 1]


# ------------------------------------------------------------------------ audit

def audit(seed=0, limit=None, arm="all", per_family=2, out=None,
          stub="perfect", shipped_stub=False, inject=None, vacuity=True,
          verbose=False):
    """Sweep, then scan. Returns everything the report needs and no verdict.

    Two sweeps, not one, and their prompts are unioned before scanning.

    The first is the gate-and-repair pass: the Planner's suite is accepted, the
    Executor is graded and repaired, and the roles reached are Planner and
    Executor. The second adds `--vacuous-plan` -- Sprint 5's flag, unchanged --
    which makes the Planner emit a TESTS block the vacuity audit rejects, so
    `_resolve_tests` calls the Test Writer. That is the only path to a Test
    Writer prompt, so without the second pass the audit's clean result covers
    two of the three roles that build prompts and says nothing about the third.

    Each pass gets its own results directory. Sharing one would let the second
    pass resume from the first's finished cells and make no calls at all --
    a sweep that sweeps nothing, arriving as a pass.
    """
    tasks = gen_tasks.generate(seed=seed, per_family=per_family, limit=limit)
    needles, skipped, dropped = all_needles(tasks)
    temporary = out is None
    root = out or tempfile.mkdtemp(prefix="leakage-audit-")
    base = ["--stub", stub, "--arm", arm, "--seed", str(seed),
            "--per-family", str(per_family)]
    if limit is not None:
        base += ["--limit", str(limit)]
    passes = [PASS_GATE]
    # No Planner in `--arm a`, so no suite to reject and no Test Writer to reach.
    # Requesting the pass there would run the same sweep twice and double the
    # prompt count without covering anything new.
    if vacuity and any(name in run_eval.PLAN_ARMS for name in arms_of(arm)):
        passes.append(PASS_VACUITY)

    prompts, outputs, code = [], [], 0
    injected, inject_tasks = 0, set()
    try:
        for name in passes:
            argv = base + ["--out", os.path.join(root, name)]
            if name == PASS_VACUITY:
                argv += ["--vacuous-plan"]
            pass_code, captured, injection, output = sweep(
                argv, shipped_stub=shipped_stub, inject=inject, verbose=verbose)
            code = code or pass_code
            outputs.append(output)
            for prompt in captured:
                prompts.append(prompt._replace(index=len(prompts),
                                               pass_name=name))
            if injection:
                injected += injection.injected
                inject_tasks |= injection.tasks
    finally:
        if temporary:
            shutil.rmtree(root, ignore_errors=True)
    by_leg = collections.Counter(needle.leg for needle in needles)
    by_role = collections.Counter(prompt.role for prompt in prompts)
    by_pass = collections.Counter(prompt.pass_name for prompt in prompts)
    return {"tasks": len(tasks), "literals": len(needles),
            "skipped_short": skipped, "dropped_public": dropped,
            "prompts": len(prompts), "by_leg": by_leg, "by_role": by_role,
            # The prompts themselves, so a caller can assert on what was swept
            # rather than only on how much. In memory only -- nothing here is
            # written to a file, because a prompt dump on disk is the leak this
            # audit exists to prevent, arriving by a different route.
            "prompt_list": prompts,
            "by_pass": by_pass, "passes": tuple(passes),
            "required_roles": required_roles(arm, PASS_VACUITY in passes),
            "hits": scan(prompts, needles), "sweep_code": code,
            "sweep_output": "\n".join(outputs), "shipped_stub": shipped_stub,
            "inject": inject,
            "inject_site": INJECT_SITE[inject] if inject else "",
            "injected": injected, "inject_tasks": sorted(inject_tasks)}


def _counts(counter):
    return ", ".join("%s=%d" % (name, count)
                     for name, count in sorted(counter.items())) or "none"


def _clip(text, width=72):
    return text if len(text) <= width else text[:width - 3] + "..."


# ----------------------------------------------------------------------- report

def report(result, show=6):
    """Print the counts, then the hits. Counts first, and always.

    A passing audit that swept nothing must not read like a passing audit, so the
    numbers come before the verdict and the verdict names them.
    """
    print("leakage audit: %d task(s), %d literal(s), %d prompt(s) swept%s"
          % (result["tasks"], result["literals"], result["prompts"],
             " [SHIPPED STUB]" if result["shipped_stub"] else ""))
    print("  literals by leg:  %s" % _counts(result["by_leg"]))
    print("  prompts by role:  %s" % _counts(result["by_role"]))
    print("  prompts by pass:  %s   (required roles: %s)"
          % (_counts(result["by_pass"]),
             ", ".join(sorted(result["required_roles"]))))
    print("  not checked:      %d under the length threshold, %d already public "
          "in the prompt" % (result["skipped_short"], result["dropped_public"]))
    print("  thresholds:       line >= %d chars, value repr >= %d chars, "
          "whitespace normalised" % (MIN_LINE_CHARS, MIN_VALUE_CHARS))
    if result["inject"]:
        print("  --inject %s:%sappended to %d %s across %d task(s)"
              % (result["inject"], " " * max(1, 9 - len(result["inject"])),
                 result["injected"],
                 "built context(s)" if result["inject_site"] == SITE_CONTEXT
                 else "rejected TESTS block(s)", len(result["inject_tasks"])))
    hits = result["hits"]
    absent = missing_roles(result)
    if hits:
        leaked = sorted(set(hit.needle.text for hit in hits))
        for hit in hits[:show]:
            print("  LEAK %-14s %-22s %s"
                  % (hit.needle.leg, hit.needle.task_id,
                     repr(_clip(hit.needle.text))))
            print("       in prompt %d (%s via %s, task %s, %s pass)"
                  % (hit.prompt.index, hit.prompt.role, hit.prompt.provider,
                     hit.prompt.task_id or "?", hit.prompt.pass_name or "?"))
        if len(hits) > show:
            print("  ... %d further hit(s) not shown" % (len(hits) - show))
        print("  FAILED: %d distinct literal(s) leaked into %d prompt(s)"
              % (len(leaked), len(set(hit.prompt.index for hit in hits))))
    elif not result["prompts"] or not result["literals"]:
        print("  NOT A SWEEP: %d prompt(s) and %d literal(s). An audit that "
              "checked nothing is not a pass."
              % (result["prompts"], result["literals"]))
    elif absent:
        print("  NOT A SWEEP: no prompt was swept for %s, so this run has "
              "nothing to say about %s. A clean total is not coverage."
              % (" or ".join(absent),
                 "that role" if len(absent) == 1 else "those roles"))
    else:
        print("  CLEAN: none of the %d literal(s) appears in any of the %d "
              "prompt(s), across every required role" % (result["literals"],
                                                         result["prompts"]))
    return hits


def verdict(result):
    """0 clean, 1 leaked, 2 the sweep failed or swept nothing.

    A hit is checked first and on its own. Coverage cannot un-find a literal
    that is demonstrably in a prompt, so a leak stays a leak even in a run that
    missed a role -- reporting 2 there would file real evidence under "could not
    run". The absence of a hit is the claim that needs coverage, and that is
    where a role with a swept count of zero refuses.
    """
    if result["sweep_code"] != 0:
        return 2
    if result["hits"]:
        return 1
    if not result["prompts"] or not result["literals"]:
        return 2
    if missing_roles(result):
        return 2
    return 0


# -------------------------------------------------------------------------- cli

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Prove no hidden-suite content reaches any prompt.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-family", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None,
                        help="audit the first N tasks only; the default is the "
                             "whole locked task set")
    parser.add_argument("--arm", default="all",
                        choices=("a", "a_prime", "a_prime3", "b", "both", "all"),
                        help="`all` sweeps every prompt shape and is the only "
                             "setting whose clean result means much")
    parser.add_argument("--stub", default=None,
                        choices=("perfect", "broken", "flaky"),
                        help="only reaches the shipped stub under "
                             "--shipped-stub; the audit stub ignores it. "
                             "Defaults to `broken` there and `perfect` here.")
    parser.add_argument("--shipped-stub", action="store_true",
                        help="sweep with run_eval's own StubModel, which echoes "
                             "the hidden suite on purpose. Expected to FAIL, and "
                             "that failure is the evidence this audit works.")
    parser.add_argument("--inject", choices=sorted(INJECTIONS), default=None,
                        help="leak one hidden literal on purpose, from "
                             "build_context or -- for `test_writer` -- from the "
                             "Planner's rejected TESTS block. Expected to FAIL.")
    parser.add_argument("--no-vacuity-pass", action="store_true",
                        help="sweep only the gate-and-repair pass. The Test "
                             "Writer is unreachable without the vacuity pass, "
                             "so this narrows what the run covers; the required "
                             "roles narrow with it and the report says so.")
    parser.add_argument("--out", default=None,
                        help="keep the sweep's results here instead of a "
                             "temporary directory that is deleted")
    parser.add_argument("--show", type=int, default=6)
    parser.add_argument("--verbose", action="store_true",
                        help="let the sweep print its own output")
    return parser.parse_args(argv)


def _stub_quality(args):
    """`broken` under --shipped-stub, `perfect` otherwise.

    The shipped stub's leak needs the repair path: its Planner writes hidden
    asserts into the visible suite, and that suite only reaches a *prompt* when a
    failing assertion is quoted back at the Executor. Under `--stub perfect` the
    first candidate is APPROVED, nothing is quoted, and the demonstration comes
    out clean -- which would read as "the shipped stub is fine" rather than "this
    setting never reached the interesting path". So the default is the one that
    exercises it, and `--stub` stays available to show the difference.
    """
    if args.stub:
        return args.stub
    return "broken" if args.shipped_stub else "perfect"


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.inject and args.shipped_stub:
        print("--inject and --shipped-stub are two separate demonstrations; the "
              "shipped stub leaks on its own and injecting into it would prove "
              "nothing extra", file=sys.stderr)
        return 2
    if args.inject and INJECT_SITE[args.inject] == SITE_VACUOUS \
            and args.no_vacuity_pass:
        print("--inject %s leaks from the Planner's rejected TESTS block, which "
              "only exists in the vacuity pass; --no-vacuity-pass would make the "
              "demonstration silently inject nothing" % args.inject,
              file=sys.stderr)
        return 2
    result = audit(seed=args.seed, limit=args.limit, arm=args.arm,
                   per_family=args.per_family, out=args.out,
                   stub=_stub_quality(args),
                   shipped_stub=args.shipped_stub, inject=args.inject,
                   vacuity=not args.no_vacuity_pass, verbose=args.verbose)
    if result["sweep_code"] != 0:
        print(result["sweep_output"], file=sys.stderr)
        print("the sweep itself failed (exit %d); nothing was audited"
              % result["sweep_code"], file=sys.stderr)
        return 2
    report(result, show=args.show)
    code = verdict(result)
    if args.inject or args.shipped_stub:
        which = ("--inject %s" % args.inject if args.inject
                 else "--shipped-stub")
        print("  (%s: the audit was expected to fail, and it %s.)"
              % (which, "did" if code == 1 else "DID NOT"))
    return code


if __name__ == "__main__":
    sys.exit(main())

"""Offline checks for the execution harness. No API keys, no network.

    python3 test_harness.py
"""

import sys

import harness


PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print("%s %s%s" % ("ok  " if condition else "FAIL", name,
                       "" if condition else "  <- " + str(detail)))


# ---------------------------------------------------------------- extraction

def test_extraction():
    src, lang = harness.extract_code_block(
        "Here you go:\n\n```python\nprint('hi')\n```\n\nDone."
    )
    check("extracts a tagged python block", src == "print('hi')\n", repr(src))
    check("reports the language", lang == "python", lang)

    src, _ = harness.extract_code_block(
        "usage:\n```python\nx=1\n```\nreal thing:\n"
        "```python\ndef main():\n    return 42\n\nprint(main())\n```"
    )
    check("prefers the longest block", src is not None and "def main" in src, repr(src))

    src, _ = harness.extract_code_block("Just prose, no code at all.")
    check("returns None for prose", src is None, repr(src))

    src, _ = harness.extract_code_block("```\nimport os\nprint(os.name)\n```")
    check("accepts untagged python-looking block", src is not None, repr(src))

    src, _ = harness.extract_code_block("```json\n{\"a\": 1}\n```")
    check("rejects a json block", src is None, repr(src))

    src, _ = harness.extract_code_block("here:\n```python\nprint('truncated')\n")
    check("recovers an unterminated fence", src is not None and "truncated" in src,
          repr(src))


def test_adversarial_extraction():
    """Fences that a naive non-greedy regex gets wrong.

    Every one of these produced unrunnable source before: the docstring case cut
    the body off inside an unterminated string, the indented case handed back an
    IndentationError, and the tilde case was not recognised as a fence at all.
    A block that does not parse is reported to the Executor as *its* syntax
    error, so an extractor bug arrives disguised as a model failure.
    """
    docstring = (
        '```python\n'
        'def f():\n'
        '    """Example:\n'
        '\n'
        '    ```\n'
        '    f()\n'
        '    ```\n'
        '    """\n'
        '    return 1\n'
        '```\n'
    )
    src, _ = harness.extract_code_block(docstring)
    check("survives triple backticks inside a docstring",
          src is not None and "return 1" in src, repr(src))
    check("docstring-fenced block parses", src is not None and harness._parses(src),
          repr(src))

    indented = "In a list item:\n\n    ```python\n    def g():\n        return 2\n    ```\n"
    src, _ = harness.extract_code_block(indented)
    check("dedents an indented fence", src is not None and harness._parses(src),
          repr(src))
    check("indented fence keeps its body",
          src is not None and "return 2" in src, repr(src))

    tilde = "~~~python\ndef h():\n    return 3\n~~~\n"
    src, lang = harness.extract_code_block(tilde)
    check("recognises a ~~~ fence", src is not None and "return 3" in src, repr(src))
    check("~~~ fence reports its language", lang == "python", lang)

    # The repair must not merge two adjacent blocks into one.
    two = ("```python\na = 1\n```\n\nprose\n\n"
           "```python\ndef longer():\n    return 42\n```\n")
    src, _ = harness.extract_code_block(two)
    check("adjacent blocks are not merged",
          src is not None and "prose" not in src, repr(src))

    # A ~~~-fenced suite must still be recognised as a suite.
    check("a ~~~ suite is detected as tests",
          harness.contains_test_block("~~~python\nfrom solution import f\n~~~\n"),
          True)


# ---------------------------------------------------------------- execution

def test_success():
    verdict, result = harness.verify_output(
        "```python\nvalues = [1, 2, 3]\n"
        "assert sum(values) == 6\nprint('total', sum(values))\n```"
    )
    check("working code is APPROVED", verdict == harness.VERDICT_APPROVED, verdict)
    check("exit code is 0", result.exit_code == 0, result.exit_code)
    check("stdout is captured", "total 6" in result.stdout, repr(result.stdout))
    print("     isolation: %s" % ", ".join(result.sandbox_layers))


def test_failure_traceback():
    verdict, result = harness.verify_output(
        "```python\ndef broken():\n    return 1 / 0\n\nbroken()\n```"
    )
    check("crashing code is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("nonzero exit code", result.exit_code not in (0, None), result.exit_code)
    check("real exception in stderr", "ZeroDivisionError" in result.stderr,
          repr(result.stderr))
    check("traceback names the user function", "broken" in result.stderr,
          repr(result.stderr))
    check("harness frames stripped", "_harness_runner" not in result.stderr,
          repr(result.stderr))
    fixes = harness.format_fixes(verdict, result)
    check("fixes carry the traceback", "ZeroDivisionError" in fixes, fixes[:200])


def test_assertion_failure():
    verdict, result = harness.verify_output(
        "```python\ndef add(a, b):\n    return a - b\n\nassert add(2, 2) == 4\n```"
    )
    check("failed assertion is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("AssertionError surfaced", "AssertionError" in result.stderr,
          repr(result.stderr)[:200])


def test_syntax_error():
    verdict, result = harness.verify_output("```python\ndef broken(:\n    pass\n```")
    check("syntax error is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("SyntaxError surfaced", "SyntaxError" in result.stderr,
          repr(result.stderr)[:200])


def test_nonzero_exit():
    verdict, result = harness.verify_output(
        "```python\nimport sys\nprint('bailing')\nsys.exit(3)\n```"
    )
    check("sys.exit(3) is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("exit code preserved", result.exit_code == 3, result.exit_code)

    verdict, _ = harness.verify_output("```python\nimport sys\nsys.exit(0)\n```")
    check("sys.exit(0) is APPROVED", verdict == harness.VERDICT_APPROVED, verdict)


def test_timeout():
    verdict, result = harness.verify_output(
        "```python\nwhile True:\n    pass\n```", timeout=3
    )
    check("infinite loop is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("marked as timed out", result.timed_out, result.timed_out)
    check("did not hang past the timeout", result.duration < 25, result.duration)
    check("timeout explained in fixes", "timed out" in harness.format_fixes(verdict, result),
          "")


def test_network_blocked():
    verdict, result = harness.verify_output(
        "```python\nimport urllib.request\n"
        "print(urllib.request.urlopen('http://example.com', timeout=5).status)\n```"
    )
    check("network call is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("network denial reported", "disabled by the execution harness" in result.stderr
          or "URLError" in result.stderr, repr(result.stderr)[:300])

    verdict, result = harness.verify_output(
        "```python\nimport socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.connect(('1.1.1.1', 80))\nprint('connected')\n```"
    )
    check("raw socket is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("no successful connection", "connected" not in result.stdout,
          repr(result.stdout))


def test_subprocess_blocked():
    verdict, result = harness.verify_output(
        "```python\nimport subprocess\n"
        "print(subprocess.run(['curl', 'http://example.com'], capture_output=True))\n```"
    )
    check("subprocess spawn is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("spawn denial reported", "disabled by the execution harness" in result.stderr,
          repr(result.stderr)[:300])


def test_stdin_does_not_hang():
    verdict, result = harness.verify_output(
        "```python\nname = input('name? ')\nprint(name)\n```", timeout=8
    )
    check("input() fails fast rather than hanging", not result.timed_out,
          result.duration)
    check("input() is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("EOFError surfaced", "EOFError" in result.stderr, repr(result.stderr)[:200])


def test_output_truncation():
    # Deliberately under MAX_CAPTURE_BYTES: this checks that a program which
    # *finishes* has its output trimmed for display. A program that blows the
    # capture ceiling is a different event, covered by
    # test_runaway_output_is_killed -- keep the two apart or neither is tested.
    verdict, result = harness.verify_output(
        "```python\nfor i in range(4000):\n    print('x' * 50)\n```", timeout=20
    )
    check("the program finished", result.exit_code == 0, result.exit_code)
    check("output stayed under the capture ceiling", not result.output_capped,
          result.stdout_bytes)
    check("huge stdout is truncated", len(result.stdout) <= harness.MAX_STREAM_CHARS,
          len(result.stdout))
    check("truncation flagged", result.truncated, result.truncated)


def test_no_code_path():
    verdict, result = harness.verify_output("I recommend using a dictionary here.")
    check("prose is not APPROVED", verdict != harness.VERDICT_APPROVED, verdict)
    check("marked as not executed", not result.ran, result.ran)
    fixes = harness.format_fixes(verdict, result)
    check("fixes ask for a code block", "```python" in fixes, fixes[:120])


def test_workdir_cleanup():
    import glob
    import tempfile
    harness.verify_output("```python\nopen('scratch.txt','w').write('x')\nprint('ok')\n```")
    leftovers = glob.glob(tempfile.gettempdir() + "/harness-*")
    check("temp workdirs are cleaned up", not leftovers, leftovers[:3])


def test_signal_death_is_explained():
    """Regression: a killed process must never report an empty stderr.

    The CPU rlimit used to be set to the same value as the wall-clock timeout,
    so a CPU-bound loop died by SIGXCPU before the parent's deadline fired.
    ``timed_out`` stayed False and stderr came back empty, handing the Executor
    a bare "exit -24" with nothing to act on.
    """
    check("cpu rlimit has headroom over the wall clock",
          harness.CPU_GRACE_SECONDS > 0, harness.CPU_GRACE_SECONDS)

    verdict, result = harness.verify_output(
        "```python\nwhile True:\n    pass\n```", timeout=3
    )
    check("killed process explains itself", result.stderr.strip() != "",
          repr(result.stderr))
    check("wall clock wins over the cpu limit", result.timed_out, result.exit_code)

    # Any signal death, timeout or not, must name the signal rather than leaving
    # the Executor with a bare negative exit code.
    check("signal numbers map to names",
          harness._signal_name(9) == "SIGKILL", harness._signal_name(9))
    check("unknown signals degrade gracefully",
          "signal" in harness._signal_name(999).lower(), harness._signal_name(999))

    fixes = harness.format_fixes(verdict, result)
    check("fixes carry the reason, not just the code",
          "infinite loop" in fixes or "timed out" in fixes, fixes[:120])


# --------------------------------------------------------------------------
# Phase 2: APPROVED means "meets the spec", not "didn't crash"
# --------------------------------------------------------------------------

MEDIAN = (
    "```python\n"
    "def median(values):\n"
    "    ordered = sorted(values)\n"
    "    n = len(ordered)\n"
    "    mid = n // 2\n"
    "    if n % 2:\n"
    "        return ordered[mid]\n"
    "    return (ordered[mid - 1] + ordered[mid]) / 2\n"
    "```\n"
)

# Runs perfectly, imports cleanly, and is wrong: it never averages the middle
# pair. Under Phase 1 this exited 0 and was APPROVED.
MEDIAN_WRONG = (
    "```python\n"
    "def median(values):\n"
    "    ordered = sorted(values)\n"
    "    return ordered[len(ordered) // 2]\n"
    "```\n"
)

SUITE = (
    "from solution import median\n\n"
    "assert median([3, 1, 2]) == 2\n"
    "assert median([1, 2, 3, 4]) == 2.5\n"
    "assert median([5]) == 5\n"
)


def test_import_solution_works_under_isolated_mode():
    """The `-I` gotcha, pinned.

    `-I` implies `-E -s`: neither the script's directory nor PYTHONPATH is on
    sys.path, so `from solution import ...` inside test_solution.py dies with
    ModuleNotFoundError unless the runner repairs sys.path in-process.
    """
    result = harness.run_python_sandboxed(
        "def median(values):\n    return sorted(values)[len(values) // 2]\n",
        tests="from solution import median\n"
              "import sys\n"
              "assert median([3, 1, 2]) == 2\n"
              "print('isolated=%d' % sys.flags.isolated)\n"
              "print('env_ignored=%d' % sys.flags.ignore_environment)\n",
    )
    check("`from solution import` resolves under -I",
          "ModuleNotFoundError" not in result.stderr, result.stderr[:200])
    check("the suite is the entry point", result.entry == harness.TEST_NAME,
          result.entry)
    check("the suite ran to completion", result.exit_code == 0,
          (result.exit_code, result.stderr[:200]))
    check("isolated mode really is in force", "isolated=1" in result.stdout,
          result.stdout)
    check("the environment really is ignored", "env_ignored=1" in result.stdout,
          result.stdout)
    check("no sys.path failure was misreported as a code defect",
          result.failure_kind != harness.FAIL_PATH, result.failure_kind)

    # Remove the repair and the import must fail -- which proves the repair is
    # what makes it work, and that its absence is reported as a harness problem
    # rather than blamed on the Executor.
    original = harness._RUNNER_SOURCE
    crippled = original.replace("sys.path.insert(0, _WORKDIR)", "pass")
    check("the repair line exists to be removed", crippled != original)
    harness._RUNNER_SOURCE = crippled
    try:
        verdict, broken = harness.verify_output(MEDIAN, tests=SUITE)
    finally:
        harness._RUNNER_SOURCE = original
    check("without the repair the import fails",
          "No module named 'solution'" in broken.stderr, broken.stderr[:200])
    check("that is classed as a harness path failure",
          broken.failure_kind == harness.FAIL_PATH, broken.failure_kind)
    check("and is UNVERIFIED, not blamed on the Executor",
          verdict == harness.VERDICT_UNVERIFIED, verdict)
    check("the repair is restored", harness._RUNNER_SOURCE == original)


def test_tests_passing_is_approved():
    verdict, result = harness.verify_output(MEDIAN, tests=SUITE)
    check("passing the suite is APPROVED", verdict == harness.VERDICT_APPROVED,
          (verdict, result.stderr[:200]))
    check("the suite really was the entry point",
          result.entry == harness.TEST_NAME, result.entry)
    check("result knows it was tested", result.tested, result.tested)
    check("report says what it was measured against",
          "stored acceptance suite" in harness.format_report(verdict, result),
          harness.format_report(verdict, result)[:200])
    check("nothing to fix when APPROVED",
          harness.format_fixes(verdict, result) == "")

    # Same code, no suite: it runs, but that is a weaker claim and the report
    # must say so rather than implying correctness.
    verdict2, result2 = harness.verify_output(MEDIAN)
    check("code with no suite still runs", verdict2 == harness.VERDICT_APPROVED,
          verdict2)
    check("no-suite report refuses to claim correctness",
          "cannot confirm the output is *correct*"
          in harness.format_report(verdict2, result2),
          harness.format_report(verdict2, result2)[:300])


def test_wrong_answer_that_runs_is_revised():
    plain_verdict, plain = harness.verify_output(MEDIAN_WRONG)
    check("wrong answer alone exits 0", plain.exit_code == 0, plain.exit_code)
    check("without a suite the wrong answer is APPROVED (the Phase 1 hole)",
          plain_verdict == harness.VERDICT_APPROVED, plain_verdict)

    verdict, result = harness.verify_output(MEDIAN_WRONG, tests=SUITE)
    check("with a suite the wrong answer is REVISE",
          verdict == harness.VERDICT_REVISE, verdict)
    check("classified as an assertion failure",
          result.failure_kind == harness.FAIL_ASSERTION, result.failure_kind)
    check("the failing assertion is reported",
          result.failed_assertion == "assert median([1, 2, 3, 4]) == 2.5",
          repr(result.failed_assertion))
    check("with its line number", result.failed_assertion_line == 4,
          result.failed_assertion_line)
    check("harness frames are stripped from the traceback",
          "_harness_runner" not in result.stderr and "runpy" not in result.stderr,
          result.stderr[:300])

    fixes = harness.format_fixes(verdict, result)
    check("fixes say it ran but is wrong",
          "simply computing the wrong answer" in fixes, fixes[:300])
    check("fixes do not blame the import", "could not even be imported" not in fixes)
    check("fixes forbid editing the test", "Do not change the test" in fixes)
    check("fixes quote the assertion", "median([1, 2, 3, 4]) == 2.5" in fixes)


def test_import_failure_is_distinguished():
    broken = "```python\ndef median(values)\n    return 1\n```"
    verdict, result = harness.verify_output(broken, tests=SUITE)
    check("a syntax error under a suite is REVISE",
          verdict == harness.VERDICT_REVISE, verdict)
    check("classified as an import failure",
          result.failure_kind == harness.FAIL_IMPORT, result.failure_kind)
    fixes = harness.format_fixes(verdict, result)
    check("fixes say it could not be imported",
          "could not even be imported" in fixes, fixes[:300])
    check("import fixes do not talk about wrong answers",
          "simply computing the wrong answer" not in fixes)

    missing = "```python\ndef mean(values):\n    return sum(values) / len(values)\n```"
    verdict, result = harness.verify_output(missing, tests=SUITE)
    check("a missing function is an import failure",
          result.failure_kind == harness.FAIL_IMPORT, result.failure_kind)
    check("ImportError surfaced",
          "cannot import name" in result.stderr or "ImportError" in result.stderr,
          result.stderr[:200])

    # A module-level assert inside the solution is a broken module, not a
    # wrong answer -- the deepest frame decides.
    raises = ("```python\ndef median(values):\n    return 0\n\n"
              "assert median([1]) == 1\n```")
    verdict, result = harness.verify_output(raises, tests=SUITE)
    check("the solution's own failing assert is an import failure",
          result.failure_kind == harness.FAIL_IMPORT,
          (result.failure_kind, result.stderr[:200]))
    check("it is not misreported as an acceptance-test failure",
          not result.failed_assertion, repr(result.failed_assertion))


def test_vacuous_tests_are_caught():
    good = harness.audit_tests(SUITE)
    check("a real suite passes the audit", good.ok, good.reason)
    check("audit counts the asserts", good.assert_count == 3, good.assert_count)
    check("audit names the interface", good.names == ["median"], good.names)
    check("a real suite fails against a stub", good.stub_exit not in (0, None),
          good.stub_exit)
    check("audit summary explains itself", "fails against a stub" in good.summary(),
          good.summary())

    vacuous = harness.audit_tests(
        "from solution import median\n\n"
        "assert callable(median)\n"
        "assert median is not None\n"
    )
    check("a rubber-stamp suite is rejected", not vacuous.ok, vacuous.reason)
    check("it is flagged as vacuous specifically", vacuous.vacuous, vacuous.reason)
    check("because it passes against a stub", vacuous.stub_exit == 0,
          vacuous.stub_exit)
    check("the reason names NotImplementedError",
          "NotImplementedError" in vacuous.reason, vacuous.reason)

    no_asserts = harness.audit_tests("from solution import median\nmedian([1])\n")
    check("a suite with no asserts is rejected", not no_asserts.ok)
    check("and is not called vacuous-by-stub", not no_asserts.vacuous,
          no_asserts.reason)
    check("the reason names the missing asserts",
          "no assert statements" in no_asserts.reason, no_asserts.reason)

    unrelated = harness.audit_tests("assert 1 + 1 == 2\n")
    check("a suite that never imports the solution is rejected", not unrelated.ok)
    check("the reason says it cannot be testing the solution",
          "cannot be testing the solution" in unrelated.reason, unrelated.reason)

    wildcard = harness.audit_tests("from solution import *\nassert median([1]) == 1\n")
    check("a wildcard import is rejected", not wildcard.ok, wildcard.reason)

    check("an empty suite is rejected", not harness.audit_tests("").ok)
    check("an unparseable suite is rejected",
          not harness.audit_tests("from solution import median\nassert (\n").ok)
    check("a suite may not be run as a solution",
          harness.run_python_sandboxed(MEDIAN, tests="   ").reason != "",
          harness.run_python_sandboxed(MEDIAN, tests="   ").reason)


def test_executor_cannot_supply_its_own_tests():
    both = (
        "Here is the solution:\n\n```python\n"
        "def median(values):\n    return sorted(values)[len(values) // 2]\n```\n\n"
        "And the tests:\n\n```python\n"
        "from solution import median\nassert median([1, 2, 3, 4]) == 3\n```\n"
    )
    source, _ = harness.extract_code_block(both)
    check("the test block is never picked as the solution",
          source is not None and "from solution import" not in source,
          repr(source))
    check("a test block is detectable even when it is not the longest",
          harness.contains_test_block(both))
    check("the solution block alone is not mistaken for tests",
          not harness.contains_test_block(MEDIAN))

    verdict, result = harness.verify_output(both, tests=SUITE)
    check("the Executor's weaker suite does not decide the verdict",
          verdict == harness.VERDICT_REVISE, verdict)
    check("the stored suite is the one that ran",
          result.tests == SUITE, repr(result.tests)[:120])
    check("the discarded suite is reported",
          result.ignored_test_block, result.ignored_test_block)
    check("the report mentions the stored suite was used",
          "the stored suite was used" in harness.format_report(verdict, result),
          harness.format_report(verdict, result)[:400])

    only_tests = "```python\nfrom solution import median\nassert median([1]) == 1\n```"
    verdict, result = harness.verify_output(only_tests, tests=SUITE)
    check("test-only output is REVISE, not APPROVED",
          verdict == harness.VERDICT_REVISE, verdict)
    check("test-only output is not executed", not result.ran, result.ran)
    check("test-only output is named as such",
          "only test code" in result.reason, result.reason)
    check("fixes tell the Executor not to write tests",
          "do not write tests" in harness.format_fixes(verdict, result),
          harness.format_fixes(verdict, result)[:300])


# ------------------------------------------------- resource ceilings & jail

def test_runaway_output_is_killed():
    """A printing loop must die on the byte ceiling, not exhaust the parent.

    `proc.communicate(timeout=...)` read the child's whole stream into this
    process before MAX_STREAM_CHARS ever got to trim it, so
    `while True: print("x" * 1000)` grew the *parent's* heap without bound --
    the harness taking down the app it exists to protect. The wall clock was no
    defence: 15 seconds of printing is gigabytes.
    """
    verdict, result = harness.verify_output(
        "```python\nwhile True:\n    print('x' * 1000)\n```", timeout=10
    )
    check("runaway printer is REVISE", verdict == harness.VERDICT_REVISE, verdict)
    check("the output ceiling is flagged", result.output_capped, result.output_capped)
    check("classified as runaway output",
          result.failure_kind == harness.FAIL_OUTPUT, result.failure_kind)
    check("kept stdout stays bounded",
          len(result.stdout) <= harness.MAX_STREAM_CHARS + 200, len(result.stdout))
    check("the child really did overrun the ceiling",
          result.stdout_bytes > harness.MAX_CAPTURE_BYTES, result.stdout_bytes)
    check("killed on bytes, not on the clock", result.duration < 5.0, result.duration)
    check("it is not misreported as a timeout", not result.timed_out, result.timed_out)
    fixes = harness.format_fixes(verdict, result)
    check("the Executor is told it produced runaway output",
          "runaway output" in fixes, fixes[:300])
    check("the report explains the cap",
          "Runaway output" in harness.format_report(verdict, result),
          harness.format_report(verdict, result)[:400])


def test_filesystem_jail():
    """Writes outside the workdir are refused; reads and inside-writes are not.

    Accident containment, not a security boundary -- see _block_filesystem. The
    point is that a hallucinated `open("/etc/hosts", "w")` cannot touch the
    machine when the OS-level jail is unavailable.
    """
    import os
    import tempfile

    probe = os.path.join(tempfile.gettempdir(),
                         "harness_jail_probe_%d.txt" % os.getpid())
    try:
        _, result = harness.verify_output(
            "```python\nopen(%r, 'w').write('nope')\n```" % probe
        )
        check("an absolute write outside the workdir fails the run",
              result.exit_code != 0, result.exit_code)
        check("and no such file is created", not os.path.exists(probe), probe)
        check("the refusal names the harness",
              "execution harness" in result.stderr, result.stderr[-200:])
    finally:
        if os.path.exists(probe):
            os.remove(probe)

    _, result = harness.verify_output(
        "```python\nopen('out.txt', 'w').write('hi')\n"
        "print(open('out.txt').read())\n```"
    )
    check("a relative write inside the workdir still works",
          result.exit_code == 0 and result.stdout.strip() == "hi",
          (result.exit_code, result.stdout, result.stderr[-200:]))

    _, result = harness.verify_output(
        "```python\nprint(len(open(%r).read()) > 100)\n```"
        % os.path.abspath(harness.__file__)
    )
    check("reads outside the workdir are left alone",
          result.stdout.strip() == "True",
          (result.exit_code, result.stdout, result.stderr[-200:]))

    # shutil.rmtree is the one that matters most if it ever regresses.
    _, result = harness.verify_output(
        "```python\nimport shutil\nshutil.rmtree(%r)\n```"
        % os.path.dirname(os.path.abspath(harness.__file__))
    )
    check("shutil.rmtree outside the workdir is refused",
          result.exit_code != 0 and "execution harness" in result.stderr,
          (result.exit_code, result.stderr[-200:]))


def test_timeout_phase_attribution():
    """One timeout used to cover three problems with opposite fixes.

    A SIGKILLed child prints no traceback, so "hung importing your module",
    "hung inside the function under test" and "finished but never exited" all
    arrived as the same empty-stderr, exit -9 event with one generic message.
    """
    suite = "from solution import median\nassert median([1, 3, 2]) == 2\n"
    cases = (
        ("hung on import",
         "while True:\n    pass\n\ndef median(v):\n    return sorted(v)[len(v) // 2]\n",
         harness.FAIL_TIMEOUT_IMPORT, harness.PHASE_IMPORT, "being imported"),
        ("hung under test",
         "def median(v):\n    while True:\n        pass\n",
         harness.FAIL_TIMEOUT_TESTS, harness.PHASE_TESTS,
         "function the acceptance suite called"),
        ("hung at shutdown",
         "import threading\nimport time\n\n\ndef _spin():\n"
         "    while True:\n        time.sleep(0.1)\n\n\n"
         "threading.Thread(target=_spin).start()\n\n\n"
         "def median(v):\n    return sorted(v)[len(v) // 2]\n",
         harness.FAIL_TIMEOUT_TEARDOWN, harness.PHASE_TEARDOWN, "never exited"),
    )
    for label, src, kind, phase, phrase in cases:
        verdict, result = harness.verify_output(
            "```python\n%s```" % src, tests=suite, timeout=4)
        check("%s: timed out" % label, result.timed_out, result.exit_code)
        check("%s: phase recorded" % label, result.phase == phase,
              (result.phase, result.stderr[-200:]))
        check("%s: classified apart" % label, result.failure_kind == kind,
              result.failure_kind)
        check("%s: still counts as a timeout" % label,
              harness.is_timeout(result.failure_kind), result.failure_kind)
        fixes = harness.format_fixes(verdict, result)
        check("%s: guidance is specific to it" % label, phrase in fixes, fixes[:400])
        check("%s: guidance still says it timed out" % label,
              "timed out" in fixes or "never exited" in fixes, fixes[:400])

    # With no suite there is no import boundary, so the honest answer is the
    # plain kind rather than a guess.
    _, result = harness.verify_output(
        "```python\nwhile True:\n    pass\n```", timeout=3)
    check("a suiteless hang stays the plain timeout kind",
          result.failure_kind == harness.FAIL_TIMEOUT, result.failure_kind)

    check("every timeout kind is registered as one",
          all(harness.is_timeout(k) for k in harness.TIMEOUT_KINDS)
          and not harness.is_timeout(harness.FAIL_ASSERTION),
          harness.TIMEOUT_KINDS)


def test_sandbox_layers_are_honest():
    """Layer reporting must not claim a limit the platform quietly ignores."""
    import platform

    _, result = harness.verify_output("```python\nprint('hi')\n```")
    memory = [layer for layer in result.sandbox_layers
              if layer.startswith("memory:")]
    check("the memory dimension is reported at all", len(memory) == 1,
          result.sandbox_layers)
    if platform.system() == "Darwin":
        # macOS accepts setrlimit(RLIMIT_AS) and then does not enforce it.
        check("darwin admits memory is unguarded", "unguarded" in memory[0], memory)
    elif platform.system() == "Linux":
        check("linux reports the rlimit it set", "rlimit" in memory[0], memory)
    check("the layer label matches the reporting function",
          memory == [harness._memory_layer()], (memory, harness._memory_layer()))
    check("the filesystem layer is reported",
          "in-process:no-outside-writes" in result.sandbox_layers,
          result.sandbox_layers)

    # A cap that is claimed must be a cap that was attempted.
    check("the child is actually asked to set a memory limit",
          "RLIMIT_AS" in harness._RUNNER_SOURCE
          and "_MAX_MEMORY_BYTES" in harness._RUNNER_SOURCE, "")


def test_sandbox_rules_are_complete():
    """The contract handed to the Executor must name every limit that can kill it."""
    rules = harness._SANDBOX_RULES
    for needle in ("standard library only", "No network", "No subprocesses",
                   "No stdin", "working directory",
                   str(harness.EXEC_TIMEOUT_SECONDS),
                   str(harness.MAX_CAPTURE_BYTES)):
        check("the contract mentions %r" % needle, needle in rules, rules)
    check("the contract names the interpreter version",
          "%d.%d" % sys.version_info[:2] in rules, rules)


def main():
    for fn in (
        test_extraction, test_adversarial_extraction,
        test_success, test_failure_traceback,
        test_assertion_failure, test_syntax_error, test_nonzero_exit,
        test_timeout, test_signal_death_is_explained,
        test_network_blocked, test_subprocess_blocked,
        test_stdin_does_not_hang, test_output_truncation, test_no_code_path,
        test_workdir_cleanup,
        test_import_solution_works_under_isolated_mode,
        test_tests_passing_is_approved, test_wrong_answer_that_runs_is_revised,
        test_import_failure_is_distinguished, test_vacuous_tests_are_caught,
        test_executor_cannot_supply_its_own_tests,
        test_runaway_output_is_killed, test_filesystem_jail,
        test_timeout_phase_attribution, test_sandbox_layers_are_honest,
        test_sandbox_rules_are_complete,
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

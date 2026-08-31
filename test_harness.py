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


def _report(*lines):
    """A stdout with `lines` as tagged report lines, plus some user noise."""
    body = ["solution says hello"]
    body += ["%s %s" % (harness.CHECK_TAG, line) for line in lines]
    return "\n".join(body) + "\n"


def test_the_per_check_report_is_read_or_distrusted():
    """`read_checks` counts a coherent report and refuses an incoherent one.

    The tally shares stdout with the code under test, so a solution can print the
    tag. The guarantee therefore cannot be "forged lines are ignored" -- there is
    no way to tell whose line is whose. It is that forgery can only ever destroy
    a report, never inflate one: every failure shape below returns `(0, 0)` with a
    note, and the caller reads that as *unknown*, which ranks below a measured
    zero.

    `total` is the load-bearing distinction in the first two cases. A suite whose
    import failed produced no report at all, and "0 of 0, untrusted" is the only
    honest reading of that -- reporting "0 of 13 passed" would let a missing entry
    point look like a candidate that ran and scored nothing.
    """
    passed, total, kinds, note = harness.read_checks(
        _report("1 pass -", "2 fail AssertionError", "3 fail TypeError",
                "done 3"))
    check("a coherent report is counted", (passed, total, note) == (1, 3, ""),
          (passed, total, note))
    check("and each check's exception type is carried out by id",
          kinds == ["-", "AssertionError", "TypeError"], kinds)

    passed, total, _kinds, note = harness.read_checks("Traceback...\nImportError\n")
    check("no report at all is 0 of 0 and unknown, not 0 of N",
          (passed, total) == (0, 0) and "no per-check report" in note,
          (passed, total, note))

    passed, total, _kinds, note = harness.read_checks(
        _report("1 pass -", "2 pass -"))
    check("ids with no completion marker are distrusted",
          (passed, total) == (0, 0) and "completion marker" in note,
          (passed, total, note))

    forgeries = (
        ("a duplicate id", ("1 pass -", "1 pass -", "2 pass -", "done 2")),
        ("an id outside 1..N", ("1 pass -", "7 pass -", "done 2")),
        ("an extra line beyond N", ("1 pass -", "2 pass -", "3 pass -",
                                    "done 2")),
        ("a second completion marker", ("1 pass -", "done 1", "done 1")),
        ("a whole forged report ahead of the real one",
         ("1 pass -", "done 1", "1 fail AssertionError", "done 1")),
        ("a gap in the ids", ("1 pass -", "3 pass -", "done 3")),
    )
    broken = []
    for label, lines in forgeries:
        passed, total, kinds, note = harness.read_checks(_report(*lines))
        if (passed, total, kinds) != (0, 0, []) or not note:
            broken.append("%s: %r" % (label, (passed, total, kinds, note)))
    check("every forgery shape collapses to unknown rather than a pass count",
          not broken, "; ".join(broken))

    passed, total, kinds, note = harness.read_checks(
        _report("3 pass -", "1 fail ValueError", "2 pass -", "done 3"))
    check("ids arriving out of order are trusted, because the battery permutes "
          "the checks on purpose and the set is what carries the guarantee",
          (passed, total, note) == (2, 3, "")
          and kinds == ["ValueError", "-", "-"],
          (passed, total, kinds, note))


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


def test_destructive_calls_cannot_be_spelled_around_the_guard():
    """The guard has to hold for the *spelling* a model picks, not one of them.

    Every entry point below was already wrapped. The wrapper read `args[index]`
    only, so the keyword spelling of the same call went straight through:
    `shutil.rmtree(path=<outside>)` deleted every file under the target and was
    then denied on the final top-level rmdir, so it raised a PermissionError
    that read as though the guard had held while the tree was already empty.
    `os.remove(path=<outside>)` succeeded with no error at all. A second route
    was `dir_fd` plus a bare name: `_fs_allowed` resolves a relative name by
    joining it onto the workdir, which a foreign descriptor makes false, so
    `os.unlink("top.txt", dir_fd=fd)` always looked local -- and that is the
    route rmtree's own recursion walks, which is what made the keyword case
    destructive rather than merely permitted.

    So each case here runs against a real throwaway tree outside the workdir and
    asserts on the survivors, not only on the exception. "It raised
    PermissionError" was true of the case that deleted everything.
    """
    import os
    import shutil
    import tempfile

    victim = os.path.join(tempfile.gettempdir(),
                          "harness_victim_%d" % os.getpid())

    def build():
        if os.path.isdir(victim):
            shutil.rmtree(victim)
        os.makedirs(os.path.join(victim, "nested"))
        for rel in ("top.txt", os.path.join("nested", "deep.txt")):
            with open(os.path.join(victim, rel), "w") as handle:
                handle.write("precious\n")

    def survivors():
        found = []
        for root, _dirs, files in os.walk(victim):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), victim))
        return sorted(found)

    intact = ["nested/deep.txt".replace("/", os.sep), "top.txt"]
    top = os.path.join(victim, "top.txt")

    # (label, source). Both spellings of everything the brief named, plus the
    # two routes that defeated the positional-only wrapper.
    cases = (
        ("shutil.rmtree positional",
         "import shutil\nshutil.rmtree(%r)\n" % victim),
        ("shutil.rmtree by keyword",
         "import shutil\nshutil.rmtree(path=%r)\n" % victim),
        ("os.remove positional", "import os\nos.remove(%r)\n" % top),
        ("os.remove by keyword", "import os\nos.remove(path=%r)\n" % top),
        ("os.unlink by keyword", "import os\nos.unlink(path=%r)\n" % top),
        ("os.rmdir by keyword",
         "import os\nos.rmdir(path=%r)\n" % os.path.join(victim, "nested")),
        ("os.rename by keyword",
         "import os\nopen('mine.txt', 'w').write('x')\n"
         "os.rename(src='mine.txt', dst=%r)\n"
         % os.path.join(victim, "landed.txt")),
        ("os.replace by keyword",
         "import os\nopen('mine.txt', 'w').write('x')\n"
         "os.replace(src='mine.txt', dst=%r)\n" % top),
        ("shutil.move by keyword",
         "import shutil\nopen('mine.txt', 'w').write('x')\n"
         "shutil.move(src='mine.txt', dst=%r)\n"
         % os.path.join(victim, "landed.txt")),
        ("os.makedirs by keyword",
         "import os\nos.makedirs(name=%r)\n"
         % os.path.join(victim, "a", "b")),
        ("os.open by keyword",
         "import os\nos.open(path=%r, flags=os.O_WRONLY | os.O_CREAT)\n"
         % os.path.join(victim, "made.txt")),
        ("os.unlink with a foreign dir_fd",
         "import os\nfd = os.open(%r, os.O_RDONLY)\n"
         "os.unlink('top.txt', dir_fd=fd)\n" % victim),
        ("os.rmdir with a foreign dir_fd",
         "import os\nfd = os.open(%r, os.O_RDONLY)\n"
         "os.rmdir('nested', dir_fd=fd)\n" % victim),
        ("pathlib Path.unlink",
         "import pathlib\npathlib.Path(%r).unlink()\n" % top),
        ("a symlink inside the workdir pointing outside",
         "import os\nos.symlink(%r, 'escape')\n"
         "open(os.path.join('escape', 'planted.txt'), 'w').write('x')\n"
         % victim),
    )
    try:
        for label, source in cases:
            build()
            _, result = harness.verify_output("```python\n%s```" % source)
            check("%s is refused" % label,
                  result.exit_code != 0
                  and "execution harness" in result.stderr,
                  (result.exit_code, result.stderr[-200:]))
            check("  and nothing under the target was deleted (%s)" % label,
                  survivors() == intact and os.path.isdir(victim),
                  (survivors(), os.path.isdir(victim)))
    finally:
        if os.path.isdir(victim):
            shutil.rmtree(victim)


def test_guarded_calls_still_work_inside_the_workdir():
    """The other half of a guard: it must not deny the legal use of the same call.

    A guard that over-denies is worse than a missing one here, because the cost
    lands on the Executor. Two live false denies were found and fixed:

    `pathlib` was broken in *both* directions on 3.9. The deferred `import
    pathlib` exists so `Path.unlink` picks up the guards, but that only works
    for C builtins -- those do not bind, so `os.unlink` sitting on
    `pathlib._NormalAccessor` stayed a plain function. The pure-Python wrappers
    *do* bind, so every accessor call arrived shifted by one with the accessor
    instance in the path slot: a legal `Path("x").write_text("hi")` raised
    `TypeError: unsupported operand type(s) for &: 'PosixPath' and 'int'` (the
    path had landed in `os.open`'s flags slot), a legal `read_text()` raised the
    same, and a real escape was refused naming `<pathlib._NormalAccessor object
    at 0x...>` instead of a path.

    And refusing the `dir_fd` family outright -- the fix for the bare-name hole
    -- also refused a legal in-workdir `shutil.rmtree("build")`, because
    `_rmtree_safe_fd` is that family's one honest caller. rmtree is pointed at
    its path-based walk instead, which keeps the call working and makes each step
    of the recursion checkable, where under the fd walk it was not checkable at
    all.
    """
    cases = (
        ("open/os.remove",
         "import os\n"
         "open('a.txt', 'w').write('hi')\n"
         "assert open('a.txt').read() == 'hi'\n"
         "os.remove('a.txt')\n"
         "assert not os.path.exists('a.txt')\n"),
        ("os.makedirs then shutil.rmtree",
         "import os, shutil\n"
         "os.makedirs(os.path.join('e', 'f'))\n"
         "open(os.path.join('e', 'f', 'g.txt'), 'w').write('x')\n"
         "shutil.rmtree('e')\n"
         "assert not os.path.exists('e')\n"),
        ("os.rename and os.replace",
         "import os\n"
         "open('one.txt', 'w').write('x')\n"
         "os.rename('one.txt', 'two.txt')\n"
         "open('three.txt', 'w').write('y')\n"
         "os.replace('three.txt', 'two.txt')\n"
         "assert open('two.txt').read() == 'y'\n"),
        ("shutil.move and shutil.copy2",
         "import os, shutil\n"
         "os.mkdir('d')\n"
         "open('src.txt', 'w').write('x')\n"
         "shutil.copy2('src.txt', os.path.join('d', 'copied.txt'))\n"
         "shutil.move('src.txt', os.path.join('d', 'moved.txt'))\n"
         "assert sorted(os.listdir('d')) == ['copied.txt', 'moved.txt']\n"),
        ("pathlib write_text/read_text/unlink",
         "import pathlib\n"
         "p = pathlib.Path('b.txt')\n"
         "p.write_text('hello')\n"
         "assert p.read_text() == 'hello'\n"
         "p.unlink()\n"
         "assert not p.exists()\n"),
        ("pathlib mkdir/rename/rmdir",
         "import pathlib\n"
         "d = pathlib.Path('sub')\n"
         "d.mkdir()\n"
         "f = d / 'c.txt'\n"
         "f.write_text('x')\n"
         "f.rename(d / 'd.txt')\n"
         "assert [q.name for q in d.iterdir()] == ['d.txt']\n"
         "(d / 'd.txt').unlink()\n"
         "d.rmdir()\n"),
        ("pathlib reading outside the workdir",
         "import pathlib\n"
         "assert len(pathlib.Path(%r).read_text()) > 100\n"
         % harness.__file__),
    )
    for label, source in cases:
        _, result = harness.verify_output("```python\n%sprint('ok')\n```" % source)
        check("%s works inside the workdir" % label,
              result.exit_code == 0 and result.stdout.strip() == "ok",
              (result.exit_code, result.stdout[-200:], result.stderr[-300:]))


# A stand-in for 3.10's `pathlib`, accessor shape only. The runner puts the workdir
# on `sys.path` before it installs the path guard, and the guard imports `pathlib`
# late and on purpose, so a `pathlib.py` in the workdir *is* what the guard patches.
# That is the only way to exercise another version's accessor on this interpreter,
# and it exercises the real block rather than a copy of its rule.
_FAKE_PATHLIB_310 = (
    "import io\n"
    "import os\n"
    "\n"
    "\n"
    "class _Accessor(object):\n"
    "    pass\n"
    "\n"
    "\n"
    "class _NormalAccessor(_Accessor):\n"
    "    stat = os.stat\n"
    "    # The whole difference: 3.10 moved `Path.open` onto the accessor, so this\n"
    "    # slot holds io.open's six-argument signature and not os.open's four.\n"
    "    open = io.open\n"
    "    unlink = os.unlink\n"
    "    rmdir = os.rmdir\n"
    "    mkdir = os.mkdir\n"
    "    rename = os.rename\n"
    "    replace = os.replace\n"
    "    chmod = os.chmod\n"
    "    utime = os.utime\n"
    "    symlink = staticmethod(os.symlink)\n"
    "\n"
    "    def truncate(self, path, length):\n"
    "        raise NotImplementedError('pathlib-authored, not os.truncate')\n"
    "\n"
    "\n"
    "_normal_accessor = _NormalAccessor()\n"
)

# And 3.11's, which dropped the accessor entirely.
_FAKE_PATHLIB_311 = "import io\nimport os\n\n\nclass Path(object):\n    pass\n"


def _run_runner_against(fake_pathlib, probe):
    """`(proc, mechanism)` from the real child runner, optionally over a stand-in `pathlib`.

    `fake_pathlib=None` runs against the interpreter's own pathlib, which is the
    only way to check the guard against the shape actually in play here rather
    than against a reconstruction of another version's shape.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    workdir = tempfile.mkdtemp(prefix="fake-pathlib-")
    try:
        files = [("solution.py", probe),
                 ("_harness_runner.py", harness._RUNNER_SOURCE)]
        if fake_pathlib is not None:
            files.insert(0, ("pathlib.py", fake_pathlib))
        for name, text in files:
            with open(os.path.join(workdir, name), "w", encoding="utf-8") as h:
                h.write(text)
        proc = subprocess.run(
            [sys.executable, "-I", "-B",
             os.path.join(workdir, "_harness_runner.py"),
             os.path.join(workdir, "solution.py"), "20",
             str(harness.MAX_WRITE_BYTES), str(harness.MAX_MEMORY_BYTES),
             "solution", os.path.join(workdir, harness.PHASE_NAME)],
            cwd=workdir, env=harness._child_env(workdir),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60)
        return proc, harness._read_paths_mechanism(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_the_path_guard_is_correct_on_the_interpreter_that_runs_it():
    """`Path.write_text` and `Path.read_text` through the real pathlib, not a stand-in.

    `test_the_pathlib_guard_survives_a_moved_accessor` reconstructs the 3.9, 3.10
    and 3.11 accessor shapes and pokes the accessor slot directly. That is the
    right way to cover versions this machine does not have, and it is not a
    substitute for calling the public API on the version it does: a slot can hold
    a correctly re-wrapped function and `Path.write_text` can still fail, because
    `write_text` reaches `os.open` through `io.open`'s `opener=` and that route has
    its own argument shape.

    Which interpreter that is, is not a choice: `harness._run_child` launches the
    child with `sys.executable`, so the guard always runs on whatever interpreter
    invoked the harness -- there is no second interpreter and no hardcoded
    `python3` to drift. That is asserted here rather than described, because the
    version-coupling this covers is only decidable if the version is known.

    Three behaviours, all of them measurement-validity rather than UX. A false
    deny lands on the Executor as FAIL_RUNTIME and silently depresses the measured
    pass rate:

      * a legal in-workdir `Path.write_text()` succeeds -- no TypeError from a
        substituted `open`, no PermissionError from a shifted path argument.
      * an outside `Path.write_text()` raises PermissionError naming the workdir,
        and specifically not TypeError. The distinction matters: TypeError is the
        guard breaking, PermissionError is the guard working.
      * an outside `Path.read_text()` is *allowed*. Reads outside the workdir are
        deliberately not denied -- tracebacks read source and suites read fixtures
        -- so the honest form of "a read is not misreported as a write" is that it
        goes through and raises nothing at all.
    """
    version = ".".join(str(part) for part in sys.version_info[:3])
    import os
    probe = (
        "import io, os, pathlib, sys\n"
        "outside = os.path.join(os.sep, 'etc', 'harness_probe_write')\n"
        "readable = %r\n"
        "pathlib.Path('legal.txt').write_text('hi')\n"
        "assert pathlib.Path('legal.txt').read_text() == 'hi', 'wrote nothing'\n"
        "try:\n"
        "    pathlib.Path(outside).write_text('x')\n"
        "except PermissionError as exc:\n"
        "    assert 'outside the harness working directory' in str(exc), str(exc)\n"
        "except TypeError as exc:\n"
        "    raise AssertionError('write outside raised TypeError: %%s' %% exc)\n"
        "else:\n"
        "    raise AssertionError('an outside write went through')\n"
        "try:\n"
        "    body = pathlib.Path(readable).read_text()\n"
        "except PermissionError as exc:\n"
        "    raise AssertionError('a read outside was denied as a write: %%s' %% exc)\n"
        "except TypeError as exc:\n"
        "    raise AssertionError('read outside raised TypeError: %%s' %% exc)\n"
        "assert body, 'read nothing'\n"
        "print('ok', '.'.join(str(p) for p in sys.version_info[:3]))\n"
        % os.path.abspath(harness.__file__))
    proc, mechanism = _run_runner_against(None, probe)
    reported = proc.stdout.decode("utf-8", "replace").strip().split()
    check("the child runs on the parent's own interpreter, so the guard's "
          "version coupling is decided by one version and not two",
          len(reported) == 2 and reported[1] == version,
          "parent %s, child said %r" % (version, reported[-1:]))
    check("under Python %s a legal in-workdir Path.write_text() succeeds, an "
          "outside one is a PermissionError and not a TypeError, and an outside "
          "Path.read_text() is allowed" % version,
          proc.returncode == 0 and reported[:1] == ["ok"],
          (proc.returncode, proc.stdout[-200:], proc.stderr[-600:]))
    check("and it took the accessor route this version actually has",
          mechanism.startswith("pathlib:"), mechanism)


def test_the_pathlib_guard_survives_a_moved_accessor():
    """The accessor repair must not assume which function belongs in a slot.

    The first repair put `os.<name>` back into every accessor slot as a
    staticmethod, which is right on 3.9 and wrong from 3.10: 3.10 moved `Path.open`
    onto the accessor, that slot holds `io.open`'s six-argument signature, and
    four-argument `os.open` in it raises `TypeError: open() takes at most 4
    arguments (6 given)` on a *legal* in-workdir `Path.write_text()`. A false deny,
    charged to the Executor -- the same failure the repair was written to fix, one
    version later. Skipping the block on 3.10 would be worse still: the slots would
    keep the pure-Python guards, they would keep binding, and every legal
    `Path.unlink()` would arrive with the accessor in the path slot and be refused.

    So the rule is to re-wrap what the slot already holds, and only where that is
    the module-level function this guard patched. Checked against both shapes on
    one interpreter, because the version that broke it is not the version this runs
    on and a comment is not a check.
    """
    probe_310 = (
        "import io, os, pathlib\n"
        "acc = pathlib._NormalAccessor\n"
        "raw = vars(acc)\n"
        "assert isinstance(raw['open'], staticmethod), 'the open slot still binds'\n"
        "assert acc.open is io.open, 'the open slot was substituted, not re-wrapped'\n"
        "assert acc.open is not os.open, 'os.open was put in io.open s slot'\n"
        "assert isinstance(raw['unlink'], staticmethod), 'unlink still binds'\n"
        "assert acc.unlink is os.unlink, 'the unlink slot was substituted'\n"
        "assert not isinstance(raw['truncate'], staticmethod), 'a pathlib-authored "
        "slot was rebound, which shifts it the other way'\n"
        # The six-argument call shape 3.10 actually makes, both directions.
        "handle = pathlib._normal_accessor.open('legal.txt', 'w', -1, None, None, "
        "None)\n"
        "handle.write('hi')\n"
        "handle.close()\n"
        "assert open('legal.txt').read() == 'hi', 'a legal in-workdir write failed'\n"
        "try:\n"
        "    pathlib._normal_accessor.open('/etc/harness_probe', 'w', -1, None, "
        "None, None)\n"
        "except PermissionError as exc:\n"
        "    assert 'outside the harness working directory' in str(exc), str(exc)\n"
        "else:\n"
        "    raise AssertionError('an outside write went through the accessor')\n"
        "print('ok')\n")
    proc, mechanism = _run_runner_against(_FAKE_PATHLIB_310, probe_310)
    check("a 3.10-shaped accessor keeps its own six-argument open, and both a "
          "legal in-workdir write and a refused escape go through it",
          proc.returncode == 0 and proc.stdout.strip() == b"ok",
          (proc.returncode, proc.stdout[-200:], proc.stderr[-500:]))
    check("and the reported mechanism names what it did, slot by slot",
          mechanism == "pathlib:accessor-rebound-8+1-left-alone", mechanism)

    proc, mechanism = _run_runner_against(
        _FAKE_PATHLIB_311,
        "import os\n"
        "open('legal.txt', 'w').write('hi')\n"
        "try:\n"
        "    open('/etc/harness_probe', 'w')\n"
        "except PermissionError:\n"
        "    print('ok')\n")
    check("an accessor-less pathlib is skipped rather than patched, and the "
          "guard on os.* and io.open still holds without it",
          proc.returncode == 0 and proc.stdout.strip() == b"ok",
          (proc.returncode, proc.stdout[-200:], proc.stderr[-500:]))
    check("and the reported mechanism says so instead of naming a repair that "
          "never ran", mechanism == "pathlib:direct-calls", mechanism)

    # The live interpreter, reported rather than predicted: the parent cannot know
    # which branch the child took, so the child writes it and the parent reads it.
    import pathlib as _parent_pathlib

    _, result = harness.verify_output("```python\nprint('hi')\n```")
    reported = [layer for layer in result.sandbox_layers
                if layer.startswith("pathlib:")]
    check("the pathlib dimension is reported exactly once", len(reported) == 1,
          result.sandbox_layers)
    expected = ("accessor-rebound-"
                if hasattr(_parent_pathlib, "_NormalAccessor") else "direct-calls")
    check("and on this interpreter it reports the branch this pathlib admits of",
          reported and reported[0].startswith("pathlib:" + expected),
          (reported, expected))
    check("a child that never started leaves the dimension unanswered rather "
          "than claiming a mechanism",
          harness._read_paths_mechanism(harness.__file__ + "-does-not-exist")
          == harness.PATHS_UNREPORTED)
    check("the runner and the parent agree on the marker's name",
          harness.PATHS_NAME in harness._RUNNER_SOURCE, harness.PATHS_NAME)


def test_a_path_denial_is_the_models_failure_not_the_harnesss():
    """A refused escape must stay revisable, which is why it is not FAIL_PATH.

    The Sprint 7 brief asked for denials to surface as FAIL_PATH. They should
    not. FAIL_PATH means the harness could not make `solution` importable; it
    maps to VERDICT_UNVERIFIED with the reason "sys.path repair failed", and the
    code comment at its one assignment site reads "our bug, not the Executor's"
    precisely so those runs are neither graded nor revised. A model that wrote
    `shutil.rmtree("/Users/someone")` has not exposed a harness bug -- it made a
    mistake it can be told about and can fix. Routing it to FAIL_PATH would file
    every escape attempt as a harness malfunction and make it unrevisable, so it
    keeps arriving as an ordinary runtime failure, carrying the PermissionError.
    What this checks is that the message reads correctly for that audience.
    """
    import os
    import tempfile

    probe = os.path.join(tempfile.gettempdir(),
                         "harness_label_probe_%d.txt" % os.getpid())
    verdict, result = harness.verify_output(
        "```python\nimport shutil\nshutil.rmtree(path=%r)\n```"
        % os.path.dirname(os.path.abspath(harness.__file__))
    )
    check("a refused escape is not filed as a harness bug",
          result.failure_kind != harness.FAIL_PATH, result.failure_kind)
    check("and not as unverified, so the run is still revisable",
          verdict != harness.VERDICT_UNVERIFIED, verdict)
    check("the message says what was refused and who refused it",
          "writing outside the harness working directory" in result.stderr
          and "execution harness" in result.stderr, result.stderr[-300:])
    check("and it names the path, not an internal object",
          os.path.dirname(os.path.abspath(harness.__file__)) in result.stderr
          and "_NormalAccessor" not in result.stderr, result.stderr[-300:])
    fixes = harness.format_fixes(verdict, result)
    check("the Executor is given something to act on",
          bool(fixes.strip()), fixes[:200])
    if os.path.exists(probe):
        os.remove(probe)


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

    # `in-process:no-outside-writes` names the mechanism that was installed. It
    # does not say whether anything stood behind it, and that is the question a
    # reader of this list is actually asking, so the path dimension answers it
    # the way the memory dimension answers "was the rlimit enforced".
    paths = [layer for layer in result.sandbox_layers
             if layer.startswith("paths:")]
    check("the path dimension is reported exactly once", len(paths) == 1,
          result.sandbox_layers)
    # One prefix, because the OS-jail slot now names its dimension first like every
    # other slot does. It used to name the *mechanism* when a jail engaged
    # (`sandbox-exec:no-net`) and the dimension only when none did, so this
    # selection needed the union of every mechanism name it might see and would
    # have silently missed any jail added later.
    os_layer = [layer for layer in result.sandbox_layers
                if layer.startswith(harness.OS_LAYER_PREFIX)]
    check("the path label matches the reporting function",
          paths == [harness._path_layer(os_layer[0] if os_layer else "")],
          (paths, os_layer))
    check("the in-process guard is never reported as the OS layer",
          paths[0] != (os_layer[0] if os_layer else ""), (paths, os_layer))
    if os_layer and "no-write" in os_layer[0]:
        check("an OS write-deny is credited to the OS",
              "os-write-deny" in paths[0], (paths, os_layer))
    else:
        check("without an OS write-deny the guard admits it stood alone",
              paths[0].endswith("guard-only"), (paths, os_layer))
    check("and neither label claims to be a security boundary",
          "secure" not in " ".join(result.sandbox_layers).lower(),
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


def test_the_os_jail_permits_the_writes_the_in_process_guard_permits():
    """The two write guards must agree, and on macOS they silently did not.

    `tempfile.mkdtemp()` hands back a path reached through a symlink -- macOS
    resolves `/var/folders/...` to `/private/var/folders/...` -- and seatbelt
    matches `subpath` against the kernel's canonical spelling. So a profile built
    from the unresolved path has an allow-rule that matches nothing, `(deny
    file-write*)` covers the workdir as well, and every legal in-workdir write
    fails with EPERM.

    Which is the same measurement bug Task 2 is about, one layer down: the false
    deny is charged to the Executor as FAIL_RUNTIME and the measured pass rate
    drops for a reason that has nothing to do with the model. It was invisible
    because the jail probe ran `print('probe')`, which a write-denying jail passes,
    and because the surrounding sandbox this was developed in made
    `sandbox-exec` unavailable so no OS rung engaged at all.

    Three checks: the probe would now catch it, the profile is built from the
    canonical path, and -- on macOS, where it can be executed rather than argued
    -- the two spellings really do differ.
    """
    import os
    import platform
    import shutil
    import subprocess
    import tempfile

    check("the jail probe writes inside the workdir, so a jail that denies its "
          "own working directory cannot pass it",
          "open(" in harness._PROBE_SOURCE
          and harness._PROBE_NAME in harness._PROBE_SOURCE,
          harness._PROBE_SOURCE)

    workdir = tempfile.mkdtemp(prefix="jail-probe-")
    try:
        real = os.path.realpath(workdir)
        profiles = [prefix[2] for _label, prefix
                    in harness._candidate_wrappers(workdir)
                    if prefix and prefix[0] == "sandbox-exec"]
        if platform.system() == "Darwin":
            check("every macOS profile names the workdir by its canonical path "
                  "and not by the symlinked one mkdtemp returned",
                  profiles and all(workdir not in text or real in text
                                   for text in profiles)
                  and any(real in text for text in profiles),
                  [text[-90:] for text in profiles])
        else:
            check("no macOS profiles are offered off Darwin, so there is nothing "
                  "to canonicalise here", profiles == [], profiles)

        if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
            check("the two spellings differ under seatbelt (skipped: not macOS "
                  "with sandbox-exec)", True, platform.system())
            return
        template = harness._MACOS_PROFILES[0][1]
        outcomes = {}
        for label, base in (("as-given", workdir), ("canonical", real)):
            proc = subprocess.run(
                ["sandbox-exec", "-p", template.format(workdir=base),
                 sys.executable, "-I", "-B", "-c",
                 "open('w.txt', 'w').write('hi')\nprint('WROTE')"],
                cwd=workdir, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            outcomes[label] = (proc.returncode, b"WROTE" in proc.stdout)
        check("the symlinked spelling really is denied and the canonical one "
              "really is allowed, so the realpath is load-bearing rather than "
              "decorative",
              outcomes["as-given"] == (1, False)
              and outcomes["canonical"] == (0, True), outcomes)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_the_readme_names_only_layers_the_code_emits():
    """A doc that describes a label nothing emits is worse than no doc.

    The README is where a reader learns what the feed's isolation strings mean, so
    every layer-shaped string in it has to be one this tree can produce -- and the
    families have to be complete in the other direction too, because the failure
    that prompted this check was a README naming one macOS rung when the code has
    two, the second of which denies no writes at all while the README said the
    macOS layer denied them.
    """
    import os
    import re

    root = os.path.dirname(os.path.abspath(harness.__file__))
    with open(os.path.join(root, "README.md"), "r", encoding="utf-8") as handle:
        readme = handle.read()
    with open(harness.__file__, "r", encoding="utf-8") as handle:
        source = handle.read()

    pattern = r'(?:os-level|paths|pathlib|memory|in-process):'
    emitted = set(re.findall(r'"(%s[^"]*)"' % pattern, source))
    # `%d`-formatted labels are emitted as a family, so compare on the stem.
    stems = set(name.split("%")[0] for name in emitted if "%" in name)
    claimed = set(re.findall(r'`(%s[^`]*)`' % pattern, readme))
    unemittable = sorted(
        name for name in claimed
        if name not in emitted
        and not any(name.startswith(stem) for stem in stems))
    check("every layer string the README names is one the code can emit",
          unemittable == [], unemittable)

    # And the two OS families the README documents rung by rung, in full: a rung
    # the code offers and the README omits is the defect this check exists for.
    rungs = set(label for label, _profile in harness._MACOS_PROFILES)
    rungs |= set(["os-level:unshare-net+user", "os-level:unshare-net",
                  harness.OS_LAYER_UNAVAILABLE])
    missing = sorted(rung for rung in rungs if "`%s`" % rung not in readme)
    check("and every OS-jail rung the code offers is documented, fallbacks "
          "included", missing == [], missing)
    check("including that the weaker macOS rung denies no writes, which is the "
          "claim the README used to get wrong",
          "denies the network and **nothing else**" in readme, "")


def main():
    for fn in (
        test_extraction, test_adversarial_extraction,
        test_success, test_failure_traceback,
        test_assertion_failure, test_syntax_error, test_nonzero_exit,
        test_timeout, test_signal_death_is_explained,
        test_network_blocked, test_subprocess_blocked,
        test_stdin_does_not_hang, test_the_per_check_report_is_read_or_distrusted,
        test_output_truncation, test_no_code_path,
        test_workdir_cleanup,
        test_import_solution_works_under_isolated_mode,
        test_tests_passing_is_approved, test_wrong_answer_that_runs_is_revised,
        test_import_failure_is_distinguished, test_vacuous_tests_are_caught,
        test_executor_cannot_supply_its_own_tests,
        test_runaway_output_is_killed, test_filesystem_jail,
        test_destructive_calls_cannot_be_spelled_around_the_guard,
        test_guarded_calls_still_work_inside_the_workdir,
        test_the_pathlib_guard_survives_a_moved_accessor,
        test_the_path_guard_is_correct_on_the_interpreter_that_runs_it,
        test_a_path_denial_is_the_models_failure_not_the_harnesss,
        test_timeout_phase_attribution, test_sandbox_layers_are_honest,
        test_sandbox_rules_are_complete,
        test_the_os_jail_permits_the_writes_the_in_process_guard_permits,
        test_the_readme_names_only_layers_the_code_emits,
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

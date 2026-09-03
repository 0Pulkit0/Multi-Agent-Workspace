"""Execution harness: extract code and prove it meets the spec.

Phase 1 replaced the opinion-based Critic with real execution. Phase 2 fixes
what execution alone could not tell us: a program that runs cleanly but
computes the wrong answer used to be APPROVED.

Now the Planner emits an acceptance suite of plain asserts alongside the spec,
and the harness runs the Executor's code *against that suite*. APPROVED
requires three things:

  1. the test file actually ran,
  2. it exited 0, and
  3. the suite was non-vacuous -- verified by running it against a stub
     solution whose every function raises NotImplementedError. If a suite
     passes against that stub it proves nothing, and a suite that proves
     nothing would rubber-stamp everything, reintroducing exactly the fake
     verification Phase 1 removed.

Isolation is layered, because no single mechanism is portable:

  1. in-process guards (always on) -- an isolated interpreter (`-I -B`), a
     scrubbed environment, cwd pinned to a throwaway temp dir, stdin at
     /dev/null, socket + subprocess + os.exec* neutered before user code
     imports anything, CPU/file-size rlimits, a wall-clock timeout enforced by
     killing the whole process group, and truncated output capture.
  2. an OS-level jail (best effort) -- `sandbox-exec` on macOS, `unshare -rn`
     on Linux. Each candidate profile is probed once at first use and silently
     dropped if the platform refuses it (nested sandboxes, no CAP_SYS_ADMIN,
     hardened runtimes).

Layer 1 stops LLM code from phoning home through Python. Layer 2 is what makes
that a kernel-enforced guarantee, so `ExecResult.sandbox_layers` reports which
layers were live for a given run -- never assume the strongest one was.
"""

import ast
import builtins
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Wall-clock ceiling for a single execution.
EXEC_TIMEOUT_SECONDS = 15

# The child's RLIMIT_CPU is set to the wall-clock timeout plus this grace, so
# the wall-clock kill (which we can explain) normally wins over SIGXCPU (which
# arrives as a bare negative exit code). The CPU limit is the backstop for a
# child that outlives its own process group.
CPU_GRACE_SECONDS = 5

# Per-stream capture ceiling. Runaway printers get cut off here.
MAX_STREAM_CHARS = 4000

# Per-stream ceiling on what a *repair prompt* may quote back, which is a smaller
# question than what the record keeps. `format_fixes` splices the candidate's own
# stdout and stderr into the artifact that steers the next attempt, and the caller
# clamps the task itself to 3000 characters -- so two streams at MAX_STREAM_CHARS
# would let candidate-controlled text outweigh the task by better than two to one.
# 1500 each holds the pair at parity with the spec and never bites a real
# traceback: a genuine assertion failure out of this harness measures ~720
# characters of stderr and ~490 of stdout.
MAX_FIXES_STREAM_CHARS = 1500

# Per-stream ceiling on what the parent will *read at all*, as opposed to what
# it keeps. MAX_STREAM_CHARS only trims after the whole stream is already in the
# parent's memory, which is useless against `while True: print("x" * 1000)`:
# that fills the parent's heap, not the child's. Anything past this is a program
# that has already told us what we need to know, so the child is killed.
MAX_CAPTURE_BYTES = 2 * 1024 * 1024

# Largest source we will bother running.
MAX_SOURCE_CHARS = 200_000
MAX_TESTS_CHARS = 40_000

# Hard cap on bytes the child may write to disk (rlimit).
MAX_WRITE_BYTES = 8 * 1024 * 1024

# Attempted address-space/data-segment ceiling for the child. Generous, because
# the point is to stop runaway allocation, not to squeeze honest code -- and
# because it is not enforced everywhere (see _memory_layer).
MAX_MEMORY_BYTES = 2 * 1024 * 1024 * 1024

# How often the parent checks on the child while draining it, and how long it
# will wait for a corpse and its reader threads after a kill.
_POLL_SECONDS = 0.02
_REAP_SECONDS = 5.0

# The Executor's program is always saved under this name, so the acceptance
# suite can `from solution import ...`.
SCRIPT_NAME = "solution.py"
TEST_NAME = "test_solution.py"
SOLUTION_MODULE = "solution"

# PEP 563 makes every annotation a string, so a PEP 604 union in a signature
# cannot be evaluated and cannot raise on 3.9. One line, and it does not depend
# on a model obeying an instruction. It does NOT cover `match` statements or a
# runtime `isinstance(x, int | str)` -- those are the prompt's job.
#
# Written into SCRIPT_NAME only, after the module docstring when there is one --
# see `executed_source` and the note at the write site for why the position is
# not line 1. `ExecResult.source` keeps the model's original bytes: the record
# says what the model wrote, and the executed text is recoverable from the record
# as `executed_source(source)`, a pure function of exactly those bytes. It is no
# longer `_SOURCE_PROLOGUE + source`: the position varies with the source, so
# that concatenation is right only for a source with no leading prelude.
# Nothing is prepended to TEST_NAME -- see the note at the write site.
_SOURCE_PROLOGUE = "from __future__ import annotations\n"

# A future statement may be preceded only by comments, blank lines, the module
# docstring and other future statements. Line 1 meets that on its own, which is
# what Sprint 10 assumed, but it also *demotes* a leading docstring: the first
# statement becomes our import, so the model's string is an ordinary expression
# statement. Two consequences, one cause:
#
#   1. an ordinary expression statement may not precede a future statement, so
#      `docstring` + the model's own `from __future__ import ...` -- any future
#      import, not only `annotations` -- became a SyntaxError the model did not
#      write. One of 57 replay draws died this way.
#   2. `solution.__doc__` became None for every solution opening with a
#      docstring.
#
# Inserting after the docstring fixes both, and needs no future-statement
# detection at all: our line is then itself in the prelude, and a future
# statement may precede other future statements.
_FUTURE_STATEMENT = re.compile(r"^from[ \t]+__future__[ \t]+import\b", re.M)
_LEADING_STRING = re.compile(r"""^([rRuU]?)('''|\"\"\"|'|")""")

# A byte-order mark is prelude to the tokenizer -- CPython strips it when it
# opens the file -- and an ordinary character to everything here: `str.strip`
# does not consider it whitespace and `_LEADING_STRING` will not match through
# it. Left in place it hides a docstring behind it, so the scan cuts at 0 and
# moves the mark off the first byte, where it stops being a mark and becomes
# `invalid non-printable character U+FEFF`. A `compile` of the same text cannot
# see this either way -- it refuses the mark at any position -- so only a real
# utf-8 file distinguishes the two, which is how it was measured. Spelled as an
# escape: the character itself is invisible in an editor.
_BOM = "\ufeff"


def _prelude_end(source):
    """Index just past ``source``'s leading comment/blank/docstring prelude.

    Returns ``(offset, ambiguous)``. `ambiguous` is True when the scan met a
    leading string literal whose extent it could not establish, which is where
    inserting at `offset` could demote a docstring as line 1 did; the caller
    treats that as "do not place the prologue" rather than guessing. Three
    shapes reach it, and the last two are legal Python that a line scan cannot
    resolve without tokenising:

      * a leading string with no closing quote in the file at all;
      * a parenthesised docstring -- `('a'` newline `'b')` -- which
        `_LEADING_STRING` cannot even match, the first character being `(`;
      * a string whose line continues, by backslash or operator. That is either
        a genuine expression (`'doc' + x`, which is not a docstring and demotes
        nothing) or an implicit concatenation that *is* the docstring (`'a' \\`
        newline `'b'`). Both look identical to a scan that stops at the first
        closing quote, and the second is legal before the prologue and a
        SyntaxError after it.

    A line scan, deliberately, not a parse: `ast.parse` fails outright on a
    `match` statement under 3.9 and a model that writes one would silently lose
    the prologue. The only `compile` here is over the prelude slice itself --
    comments and one string literal, which cannot contain 3.10-only syntax --
    purely to confirm the cut lands on a statement boundary.
    """
    bom = len(_BOM) if source.startswith(_BOM) else 0
    lines = source[bom:].splitlines(True)
    start = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break
        start += 1
    head = bom + sum(len(line) for line in lines[:start])
    rest = source[head:]

    if rest.startswith("("):
        # A parenthesised leading string is still the docstring, and the closing
        # paren can be any number of lines down. Ambiguous, not "the first
        # statement is code": that verdict would place the prologue above a
        # docstring again.
        return head, True
    match = _LEADING_STRING.match(rest)
    if match is None:
        # The first statement is code (or a future statement, which our line is
        # allowed to precede). Nothing to displace, nothing to get wrong.
        return head, False
    quote = match.group(2)
    pos, ambiguous = match.end(), True
    while pos < len(rest):
        if rest[pos] == "\\":       # escapes a quote in raw strings too
            pos += 2
            continue
        if rest.startswith(quote, pos):
            pos += len(quote)
            ambiguous = False
            break
        if len(quote) == 1 and rest[pos] == "\n":
            break                   # unterminated single-quoted literal
        pos += 1
    if ambiguous:
        return head, True
    trailer = rest[pos:].split("\n", 1)[0].strip()
    if trailer and not trailer.startswith("#"):
        # The line does not end at the closing quote. Either the string is part
        # of a larger expression and nothing is being demoted, or it is the first
        # piece of an implicit concatenation that is the docstring -- and a
        # concatenated docstring demoted by a line-1 prologue breaks the model's
        # own future statement exactly as a plain one did. Indistinguishable
        # here, so ambiguous: this branch used to return "safe" and was the one
        # path by which the prologue still created a SyntaxError.
        return head, True
    end = rest.find("\n", pos)
    offset = head + (len(rest) if end < 0 else end + 1)
    try:
        # Without the mark: it is not part of the statement structure, and
        # `compile` rejects it wherever it appears, which would fail every
        # marked file into the ambiguous branch.
        compile(source[bom:offset], "<prelude>", "exec")
    except Exception:
        return head, True
    return offset, False


def executed_source(source):
    """Exactly the text `run_python_sandboxed` writes as SCRIPT_NAME.

    Pure function of the recorded `ExecResult.source`, so the executed file is
    reconstructable from a stored record without storing the position.
    """
    offset, ambiguous = _prelude_end(source)
    if ambiguous and _FUTURE_STATEMENT.search(source):
        # We cannot say where the model's first statement ends, and the source
        # has a future statement to break. Skipping costs PEP 563 for this one
        # draw; guessing costs a SyntaxError we caused.
        return source
    head, tail = source[:offset], source[offset:]
    # A prelude that ends at EOF without a newline would otherwise be spliced
    # into our line. A lone byte-order mark is the exception: it is not a line,
    # it is stripped by whoever opens the file, and giving it one shifts every
    # line number by one and stops the recorded bytes being recoverable by
    # deleting the inserted line.
    body = head[len(_BOM):] if head.startswith(_BOM) else head
    if body and not body.endswith("\n"):
        head += "\n"
    return head + _SOURCE_PROLOGUE + tail

# The child records how far it got here, so that a SIGKILLed process -- which
# prints no traceback at all -- can still be explained.
PHASE_NAME = "_harness_phase"

# And which pathlib mechanism its path guard ended up using. The parent reports the
# layer list but cannot know this: it depends on what the child's pathlib actually
# holds, and reconstructing it from `sys.version_info` in the parent would be
# reading the mechanism out of source instead of recording what ran.
PATHS_NAME = "_harness_paths"

# And the two sides of a failing comparison, re-evaluated in the frame that
# raised. Only the child can produce these: a bare `assert a == b` carries no
# values, and by the time the parent sees the traceback the process holding the
# objects is gone. Written only when a suite assertion fails.
ASSERT_NAME = "_harness_assert"

# Each side is clamped by the child that writes it and again by the parent that
# reads it. Not redundant: the marker sits in the workdir, which the child can
# write to, so a candidate can forge the file. Forging it buys nothing -- the
# only thing it steers is that candidate's own next attempt, and the streams it
# already controls are quoted into the same prompt -- but the size of anything
# going into a prompt is the parent's business either way.
MAX_ASSERT_VALUE_CHARS = 300

# When the child never got far enough to say. Not a mechanism -- an admission that
# the dimension went unreported, which is a different fact from any of the answers.
PATHS_UNREPORTED = "pathlib:unreported"

PHASE_STARTUP = "startup"
PHASE_IMPORT = "import-solution"
PHASE_TESTS = "tests"
PHASE_SOLUTION = "solution"
PHASE_TEARDOWN = "teardown"

# A generated suite reports one line per check on stdout, tagged with this. The
# tag lives here rather than in the generator because two very different readers
# have to agree on it byte for byte: `eval/gen_tasks.py` emits it and everything
# that ranks candidates parses it. Four sigils and an internal hyphen so that
# nothing an Executor prints by accident collides -- and see `read_checks` for
# why a collision on purpose cannot buy anything either.
CHECK_TAG = "@@MAW-CHECK@@"


# --------------------------------------------------------------------------
# code block extraction
# --------------------------------------------------------------------------

_FENCE = re.compile(
    r"(?P<fence>```|~~~)[ \t]*(?P<lang>[\w+.\-]*)[ \t]*\r?\n(?P<body>.*?)(?P=fence)",
    re.DOTALL,
)
_OPEN_FENCE = re.compile(
    r"(?:```|~~~)[ \t]*(?P<lang>[\w+.\-]*)[ \t]*\r?\n(?P<body>.*)\Z", re.DOTALL)

_PY_LANGS = {"python", "python3", "py", "python2", ""}
_PY_HINT = re.compile(
    r"^\s*(?:import\s+\w|from\s+[\w.]+\s+import\s|def\s+\w+\s*\(|class\s+\w+"
    r"|async\s+def\s|if\s+__name__\s*==|print\s*\()",
    re.MULTILINE,
)

# A block that imports `solution` cannot itself *be* solution.py.
_TEST_IMPORT = re.compile(
    r"^[ \t]*(?:from[ \t]+solution[ \t]+import\b|import[ \t]+solution\b)",
    re.MULTILINE,
)


def _parses(source):
    try:
        ast.parse(source)
        return True
    except (SyntaxError, ValueError):
        return False


def _looks_truncated(source):
    """Did this source get cut off mid-construct, rather than just being wrong?

    Used to decide whether extending a fenced block is worth trying. Genuinely
    non-Python blocks fail with "invalid syntax" and are left alone.
    """
    try:
        ast.parse(source)
    except SyntaxError as exc:
        msg = (exc.msg or "").lower()
        return ("eof" in msg or "unterminated" in msg
                or "was never closed" in msg)
    except Exception:
        pass
    return False


def _repair_body(text, match):
    """Extend a fenced body that a nested ``` cut short.

    The fence regex has to be non-greedy or two adjacent blocks merge into one.
    The cost is that a ```python block whose docstring shows example output in
    its own nested fence terminates early, leaving a body that ends inside an
    unterminated triple-quoted string. When the body looks cut off that way,
    keep extending to the next closing fence of the same kind and take the first
    version that parses. Bounded, because this is a repair and not a search.
    """
    body = match.group("body")
    if not _looks_truncated(textwrap.dedent(body)):
        return body
    fence = match.group("fence")
    # text[end("body"):] starts with the closing fence that cut us short.
    pieces = text[match.end("body"):].split(fence)
    grown = body
    for extra in pieces[1:9]:
        grown = grown + fence + extra
        if _parses(textwrap.dedent(grown)):
            return grown
    return body


def _blocks(text):
    """Yield ``(lang, body)`` for every fenced block in ``text``.

    Handles ``~~~`` as well as ``` ``` ```, and repairs bodies truncated by a
    nested fence. Blocks that start inside an already-yielded (repaired) span
    are skipped, so the fragments of a repaired block are not re-reported as
    blocks of their own.
    """
    consumed = 0
    for match in _FENCE.finditer(text or ""):
        if match.start() < consumed:
            continue
        body = _repair_body(text, match)
        consumed = match.start("body") + len(body) + len(match.group("fence"))
        yield (match.group("lang") or "").strip().lower(), body


def _dangling_block(text):
    """``(lang, body)`` for a final unterminated fence, else ``(None, None)``.

    Models truncate mid-block often enough to be worth recovering.
    """
    if not text:
        return None, None
    if text.count("```") % 2 == 0 and text.count("~~~") % 2 == 0:
        return None, None
    match = _OPEN_FENCE.search(text)
    if not match:
        return None, None
    return (match.group("lang") or "").strip().lower(), match.group("body")


def looks_like_tests(source):
    """True when a block imports from the solution module, i.e. it is a suite."""
    return bool(_TEST_IMPORT.search(source or ""))


def contains_test_block(text):
    """True when *any* fenced block in the output is a test suite.

    Checking the block the extractor happened to pick is not enough: an
    Executor that emits its solution *and* a suite alongside it should still be
    told its suite was discarded, and its solution block is usually the longer
    of the two.
    """
    if not text:
        return False
    for _, body in _blocks(text):
        if looks_like_tests(body):
            return True
    _, body = _dangling_block(text)
    return bool(body) and looks_like_tests(body)


def extract_code_block(text, allow_tests=False):
    """Pull the most plausible runnable Python source out of model output.

    Returns ``(source, language)``, or ``(None, None)`` when the output has no
    usable code. Prefers explicitly ``python``-tagged fences, falls back to
    untagged fences that look like Python, and recovers a final unterminated
    fence (models truncate mid-block often enough to be worth handling).

    With ``allow_tests`` False (the default, used when picking the *solution*)
    blocks that import from ``solution`` are excluded -- that is how an
    Executor that helpfully emits its own test file gets ignored rather than
    mistaken for the deliverable.
    """
    if not text:
        return None, None

    tagged, untagged = [], []
    for lang, body in _blocks(text):
        if not body.strip():
            continue
        if lang in ("python", "python3", "py", "python2"):
            tagged.append((body, lang))
        elif lang == "":
            untagged.append((body, lang))

    if not allow_tests:
        tagged = [pair for pair in tagged if not looks_like_tests(pair[0])]
        untagged = [pair for pair in untagged if not looks_like_tests(pair[0])]

    if not tagged and not untagged:
        lang, body = _dangling_block(text)
        if body and body.strip() and lang in _PY_LANGS:
            if lang or _PY_HINT.search(body):
                if allow_tests or not looks_like_tests(body):
                    return _clean_source(body), lang or "python"
        return None, None

    # Longest tagged block wins; models often emit a short usage snippet
    # alongside the real deliverable.
    if tagged:
        body, lang = max(tagged, key=lambda pair: len(pair[0]))
        return _clean_source(body), lang

    plausible = [pair for pair in untagged if _PY_HINT.search(pair[0])]
    if plausible:
        body, _ = max(plausible, key=lambda pair: len(pair[0]))
        return _clean_source(body), "python"
    return None, None


def _clean_source(body, limit=MAX_SOURCE_CHARS):
    # dedent, because a fence nested inside a markdown list arrives uniformly
    # indented and would otherwise die on IndentationError. Only the *common*
    # prefix goes, so a normal top-level block is untouched.
    source = textwrap.dedent(body.replace("\r\n", "\n")).strip("\n")
    if len(source) > limit:
        source = source[:limit]
    return source + "\n"


# --------------------------------------------------------------------------
# the child-side runner
# --------------------------------------------------------------------------

# Runs inside the sandboxed child. Installs the in-process guards, repairs
# sys.path, then hands control to the entry point under __main__, then reports a
# traceback with its own frames filtered out so the Executor sees only the
# user's stack.
_RUNNER_SOURCE = r'''
import os
import shutil
import sys
import time
import traceback

_STARTED = time.monotonic()
_SELF = os.path.abspath(__file__)
_TARGET = sys.argv[1]
_CPU_SECONDS = int(sys.argv[2])
_MAX_WRITE_BYTES = int(sys.argv[3])
_MAX_MEMORY_BYTES = int(sys.argv[4])
_ENTRY_KIND = sys.argv[5]
_PHASE_PATH = sys.argv[6]
# Derived from the phase marker, NOT from the entry script. When a suite is
# supplied the entry point is the suite, and it deliberately lives outside the
# working directory so the solution cannot read the answers it is graded on. The
# phase marker is written by the parent into the workdir itself, so argv[6] is
# the one argument that always names it.
_WORKDIR = os.path.dirname(os.path.abspath(_PHASE_PATH))
# Resolved separately: on macOS the temp dir is reached through a symlink, so
# realpath() of a path inside it does not start with _WORKDIR. Both spellings
# count as inside. _WORKDIR itself is left alone because sys.path and the
# traceback rewriting in the parent are keyed to it.
_WORKDIR_REAL = os.path.realpath(_WORKDIR)
_REAL_OPEN = open
# Where the child leaves the one fact about the path guard that only the child can
# know: which pathlib mechanism it used. Spelled here and in the parent's
# `PATHS_NAME`, and a check asserts the two agree, because the runner is written
# verbatim and cannot interpolate the parent's constant.
_PATHS_NAME = "_harness_paths"
# And where it leaves the two sides of a failing comparison. Same reason for the
# duplicated spelling as `_PATHS_NAME`, and the same check pins the pair.
_ASSERT_NAME = "_harness_assert"
# The re-evaluation is skipped once the child has been alive this long. Total
# elapsed time is an upper bound on what any single expression in the suite has
# cost so far, so re-evaluating one of them cannot cost more than this -- which
# keeps a nicety from turning an assertion failure into a timeout.
_ASSERT_REEVAL_BUDGET = 1.0
# Each side is a repr going into a prompt, so it is clamped here rather than in
# the parent: the parent cannot un-print what the child already wrote.
_ASSERT_VALUE_CHARS = 300

# `-I` implies `-E -s` and, crucially, leaves the script's own directory OFF
# sys.path -- so `from solution import ...` inside test_solution.py would die
# with ModuleNotFoundError. PYTHONPATH cannot fix it either, because `-E` makes
# the interpreter ignore the environment. It has to be repaired in-process,
# here, before the entry point is imported.
if _WORKDIR not in sys.path:
    sys.path.insert(0, _WORKDIR)

_NET_MSG = "network access is disabled by the execution harness"
_PROC_MSG = "spawning processes is disabled by the execution harness"


def _block_network():
    try:
        import socket
    except Exception:
        return

    class _BlockedSocket(object):
        def __init__(self, *args, **kwargs):
            raise OSError(_NET_MSG)

    def _deny(*args, **kwargs):
        raise OSError(_NET_MSG)

    socket.socket = _BlockedSocket
    socket.SocketType = _BlockedSocket
    for name in ("create_connection", "create_server", "socketpair",
                 "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "gethostbyaddr", "getfqdn", "getnameinfo"):
        if hasattr(socket, name):
            setattr(socket, name, _deny)


def _block_processes():
    def _deny(*args, **kwargs):
        raise OSError(_PROC_MSG)

    try:
        import subprocess
        subprocess.Popen = _deny
        for name in ("run", "call", "check_call", "check_output",
                     "getoutput", "getstatusoutput"):
            if hasattr(subprocess, name):
                setattr(subprocess, name, _deny)
    except Exception:
        pass

    for name in ("system", "popen", "execl", "execle", "execlp", "execlpe",
                 "execv", "execve", "execvp", "execvpe", "spawnl", "spawnle",
                 "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
                 "spawnvpe", "fork", "forkpty", "posix_spawn",
                 "posix_spawnp"):
        if hasattr(os, name):
            setattr(os, name, _deny)


def _apply_limits():
    try:
        import resource
    except Exception:
        return
    # RLIMIT_AS/RLIMIT_DATA are *attempted*, not relied on: macOS accepts both
    # and then does not enforce them, and some kernels only honour one. The
    # parent reports the memory dimension as unguarded where that is the case
    # rather than implying a cap that is not there.
    for res, limit in (
        (getattr(resource, "RLIMIT_CPU", None), (_CPU_SECONDS, _CPU_SECONDS + 1)),
        (getattr(resource, "RLIMIT_FSIZE", None), (_MAX_WRITE_BYTES, _MAX_WRITE_BYTES)),
        (getattr(resource, "RLIMIT_AS", None), (_MAX_MEMORY_BYTES, _MAX_MEMORY_BYTES)),
        (getattr(resource, "RLIMIT_DATA", None), (_MAX_MEMORY_BYTES, _MAX_MEMORY_BYTES)),
    ):
        if res is None:
            continue
        try:
            soft, hard = resource.getrlimit(res)
            want_soft, want_hard = limit
            if hard != resource.RLIM_INFINITY:
                want_soft = min(want_soft, hard)
                want_hard = min(want_hard, hard)
            resource.setrlimit(res, (want_soft, want_hard))
        except Exception:
            pass


_FS_MSG = ("writing outside the harness working directory is disabled "
           "by the execution harness")

# Writes here are harmless and code legitimately uses them.
_FS_ALLOW = frozenset((
    os.devnull, "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty",
    "/dev/zero", "/dev/random", "/dev/urandom",
))


def _fs_deny(path):
    try:
        shown = os.fsdecode(path)
    except Exception:
        shown = repr(path)
    return PermissionError("%s: %s" % (_FS_MSG, shown))


def _fs_deny_fd(name):
    """Refuse a call that names its target relative to a foreign directory.

    `_fs_allowed` resolves a relative name by joining it onto _WORKDIR, which is
    correct only because cwd *is* the workdir. A `dir_fd` breaks that premise:
    the name resolves against a descriptor this process cannot inspect, so a
    bare filename always looks local and is always allowed. No candidate
    solution needs one, so the whole family is refused rather than guessed at.
    """
    return PermissionError(
        "%s: %s resolves the name against a directory the harness cannot "
        "check, so calls passing one are refused outright" % (_FS_MSG, name))


def _fs_allowed(path):
    if isinstance(path, int):
        return True          # an already-open descriptor; nothing to resolve
    try:
        text = os.fsdecode(path)
    except Exception:
        return False
    if text in _FS_ALLOW:
        return True
    try:
        # join() against the workdir resolves relative paths the same way the
        # process does, because cwd *is* the workdir (and chdir out is blocked).
        real = os.path.realpath(os.path.join(_WORKDIR, text))
    except Exception:
        return False
    for base in (_WORKDIR_REAL, _WORKDIR):
        if real == base or real.startswith(base + os.sep):
            return True
    return False


# argv[1] is the suite itself when a suite was supplied, and the suite is the
# answer key: it is the thing APPROVED is measured against. It is written outside
# _WORKDIR, so no relative name and no listing of cwd reaches it -- but `..`
# still does, and reads are otherwise deliberately unrestricted. Hence a deny on
# this one path. See `_arm_suite_lock` for why it is armed late rather than
# installed late, and for what it does not close.
_SUITE = os.path.abspath(_TARGET) if _ENTRY_KIND == 'tests' else ""
_SUITE_REAL = os.path.realpath(_SUITE) if _SUITE else ""
_SUITE_LOCKED = [False]
_SUITE_MSG = ("reading the acceptance suite is disabled by the execution "
              "harness: it is the answer key this solution is graded against")


def _suite_deny(path):
    try:
        shown = os.fsdecode(path)
    except Exception:
        shown = repr(path)
    return PermissionError("%s: %s" % (_SUITE_MSG, shown))


def _is_suite(path):
    if not _SUITE_LOCKED[0] or isinstance(path, int):
        return False
    try:
        joined = os.path.join(_WORKDIR, os.fsdecode(path))
    except Exception:
        return False
    for spelling in (os.path.abspath, os.path.realpath):
        try:
            if spelling(joined) in (_SUITE, _SUITE_REAL):
                return True
        except Exception:
            continue
    return False


def _arm_suite_lock():
    """Start refusing reads of the suite -- from `import solution` onwards only.

    Installed early and armed late, because the process that must be stopped
    from reading the suite is the same process that has to read it to run it:
    runpy opens argv[1] at startup. That is also why this cannot be pushed down
    to the OS jail, where it would survive ctypes -- a seatbelt profile applies
    to the whole process and cannot distinguish the two readers. The arming point
    is the first `import solution`: after the suite has been loaded, before any
    candidate code runs.

    Pins the suite's source in linecache first, with mtime None so `checkcache`
    leaves the entry alone. Without that the deny would silently empty
    `failed_assertion`: source lines in a traceback come from linecache
    *reopening* the file, and the parent extracts the failing assertion's text
    from that stderr.

    What it does not close, deliberately: `__main__.__file__` and `sys.argv[0]`
    still name the suite, `tokenize.open` holds a reference to the real `open`
    bound before this module ran, and a caller's own code object is readable
    through `sys._getframe`. This is accident containment, in the same sense as
    `_block_filesystem` -- it removes the read a solution stumbles into, not the
    read a solution is written to perform.
    """
    try:
        import linecache
        lines = linecache.getlines(_TARGET)
        if lines:
            for key in set((_TARGET, _SUITE)):
                linecache.cache[key] = (0, None, lines, key)
    except Exception:
        pass
    _SUITE_LOCKED[0] = True


def _block_filesystem():
    """Refuse writes that resolve outside the throwaway working directory.

    This is accident containment, not a security boundary. It exists because a
    model writes `open("/etc/hosts", "w")` or
    `shutil.rmtree(os.path.expanduser("~"))` by mistake, and on a machine where
    the OS-level jail is unavailable (check sandbox_layers) nothing else would
    stop it. Code that is *trying* to get out still can -- through ctypes, or a
    low-level entry point not listed here, or importlib tricks -- so the honest
    claim is "protects your home directory from a hallucinated rm", not
    "contains untrusted code".

    Reads are deliberately left alone: importing the standard library is a read,
    tracebacks read source files, and tests legitimately read fixtures. The one
    exception is the acceptance suite -- see `_arm_suite_lock`.
    """
    import builtins
    import io

    def _guard_open(real):
        def _open(file, mode="r", *args, **kwargs):
            if _is_suite(file):
                raise _suite_deny(file)
            try:
                writing = any(char in mode for char in "wxa+")
            except TypeError:
                writing = False
            if writing and not _fs_allowed(file):
                raise _fs_deny(file)
            return real(file, mode, *args, **kwargs)
        return _open

    builtins.open = _guard_open(builtins.open)
    io.open = _guard_open(io.open)

    # runpy reads the entry point through this, not through io.open, so it is a
    # read of the suite that the suite deny has to cover -- and the only reason
    # arming late works at all is that runpy's own read happens before arming.
    _open_code = getattr(io, "open_code", None)
    if _open_code is not None:
        def _guarded_open_code(path, *args, **kwargs):
            if _is_suite(path):
                raise _suite_deny(path)
            return _open_code(path, *args, **kwargs)

        io.open_code = _guarded_open_code

    _write_flags = 0
    for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
        _write_flags |= getattr(os, flag, 0)
    _os_open = os.open

    def _guarded_os_open(*args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            raise _fs_deny_fd("dir_fd")
        path = args[0] if args else kwargs.get("path")
        if _is_suite(path):
            raise _suite_deny(path)
        flags = args[1] if len(args) > 1 else kwargs.get("flags", 0)
        try:
            writing = bool(flags & _write_flags)
        except TypeError:
            writing = True   # unreadable flags: check the path rather than skip
        if writing and not _fs_allowed(path):
            raise _fs_deny(path)
        return _os_open(*args, **kwargs)

    os.open = _guarded_os_open

    def _guard(real, checked, fds=()):
        def _wrapped(*args, **kwargs):
            for name in fds:
                if kwargs.get(name) is not None:
                    raise _fs_deny_fd(name)
            for index, name in checked:
                if index < len(args):
                    value = args[index]
                elif name in kwargs:
                    value = kwargs[name]
                else:
                    continue     # defaulted; nothing was named to check
                if not _fs_allowed(value):
                    raise _fs_deny(value)
            return real(*args, **kwargs)
        return _wrapped

    # Each path argument by position *and* by keyword, because a wrapper that
    # only reads args[index] is bypassed by the keyword spelling of the same
    # call: shutil.rmtree(path=<outside>) emptied a directory outside the
    # workdir, and only the final top-level rmdir was denied -- so it raised a
    # PermissionError that read as though the guard had held. os.remove(path=..)
    # succeeded with no error at all. The `fds` column lists the dir_fd-style
    # keywords that make a relative name resolve somewhere this process cannot
    # see; rmtree walks a tree by exactly that route.
    #
    # For copies only the destination is checked, because the source is only
    # read; for rename/move both, because the source is destroyed.
    _DIR_FD = ("dir_fd",)
    _SRC_DST_FD = ("src_dir_fd", "dst_dir_fd")
    for module, name, checked, fds in (
        (os, "remove", ((0, "path"),), _DIR_FD),
        (os, "unlink", ((0, "path"),), _DIR_FD),
        (os, "rmdir", ((0, "path"),), _DIR_FD),
        (os, "removedirs", ((0, "name"),), ()),
        (os, "mkdir", ((0, "path"),), _DIR_FD),
        (os, "makedirs", ((0, "name"),), ()),
        (os, "truncate", ((0, "path"),), ()),
        (os, "chmod", ((0, "path"),), _DIR_FD),
        (os, "chown", ((0, "path"),), _DIR_FD),
        (os, "utime", ((0, "path"),), _DIR_FD),
        (os, "mknod", ((0, "path"),), _DIR_FD),
        (os, "mkfifo", ((0, "path"),), _DIR_FD),
        (os, "chdir", ((0, "path"),), ()),
        (os, "rename", ((0, "src"), (1, "dst")), _SRC_DST_FD),
        (os, "renames", ((0, "old"), (1, "new")), ()),
        (os, "replace", ((0, "src"), (1, "dst")), _SRC_DST_FD),
        (os, "link", ((1, "dst"),), _SRC_DST_FD),
        (os, "symlink", ((1, "dst"),), _DIR_FD),
        (shutil, "rmtree", ((0, "path"),), ()),
        (shutil, "move", ((0, "src"), (1, "dst")), ()),
        (shutil, "copy", ((1, "dst"),), ()),
        (shutil, "copy2", ((1, "dst"),), ()),
        (shutil, "copyfile", ((1, "dst"),), ()),
        (shutil, "copytree", ((1, "dst"),), ()),
        (shutil, "copymode", ((1, "dst"),), ()),
        (shutil, "copystat", ((1, "dst"),), ()),
        (shutil, "make_archive", ((0, "base_name"),), ()),
        (shutil, "unpack_archive", ((1, "extract_dir"),), ()),
    ):
        real = getattr(module, name, None)
        if real is not None:
            setattr(module, name, _guard(real, checked, fds))

    # rmtree's default recursion is the one legitimate caller of the dir_fd
    # family: `_rmtree_safe_fd` opens each subdirectory and unlinks by bare name,
    # so refusing dir_fd above also refuses a perfectly legal in-workdir
    # `shutil.rmtree("build")`. Routing rmtree through its path-based walk
    # instead keeps that call working *and* makes every step of the recursion
    # checkable -- under the fd walk the individual unlinks could not be
    # verified at all, which is what let the keyword form empty a tree outside
    # the workdir. The path walk is the more race-prone of the two if something
    # swaps a directory for a symlink mid-delete; that trade is deliberate,
    # because a candidate racing its own rmtree is not what this guards against.
    shutil._use_fd_functions = False

    # Imported *after* the patches above, and only now: pathlib snapshots
    # os.unlink and friends into its accessor at import time, so importing it
    # here is what makes Path.unlink() and Path.write_text() go through the
    # guards on 3.9. If something imported pathlib earlier, its accessor holds
    # the originals -- one more reason this is containment, not a boundary.
    try:
        import pathlib
    except Exception:
        pathlib = None
    # ...but the snapshot only works for C builtins. Those do not bind, so
    # os.unlink sitting on the accessor's class stayed a plain function; the
    # pure-Python wrappers installed above *do* bind, and every call arrived
    # shifted by one, with the accessor instance in the path slot. That broke
    # pathlib in both directions on 3.9: a legal in-workdir Path.write_text()
    # raised "unsupported operand type(s) for &: 'PosixPath' and 'int'" because
    # the path had landed in os.open's flags slot, a legal read_text() raised the
    # same, and a genuine escape was refused with "<pathlib._NormalAccessor
    # object at 0x...>" in place of the path. A false deny on legal code is
    # charged to the Executor, so the guard was manufacturing failures it would
    # then blame on the model. staticmethod restores the unbound call.
    #
    # The first version of the repair also *substituted* `os.<name>` into each
    # slot, which assumed every slot holds `os.<name>`. True on 3.9, false from
    # 3.10: 3.10 moved `Path.open` onto the accessor, where the slot holds
    # `io.open`'s six-argument signature, so four-argument `os.open` in that slot
    # raises "TypeError: open() takes at most 4 arguments (6 given)" on a legal
    # in-workdir Path.write_text() -- the same false deny, one version later.
    # Skipping the block on 3.10 instead would be worse, not better: the slots
    # would still hold the pure-Python guards, they would still bind, and every
    # legal Path.unlink() would then arrive with the accessor in the path slot and
    # be denied outright. So the repair stops guessing what belongs in a slot and
    # re-wraps what is already in it, which is correct on both versions and needs
    # no version table. It re-wraps only where the slot holds the module-level
    # function this guard patched; a slot holding something else -- pathlib's own
    # `def lchmod(self, path, mode)` fallbacks, or an original C builtin snapshotted
    # by an earlier import -- is left alone, because a staticmethod around a real
    # method shifts it the other way and would deny legal calls in a third way.
    _ACCESSOR_NAMES = ("open", "unlink", "rmdir", "mkdir", "rename", "replace",
                       "chmod", "chown", "utime", "link", "symlink", "mkfifo",
                       "truncate")
    if pathlib is None:
        _paths_mechanism = "pathlib:unimportable"
    else:
        _accessor = getattr(pathlib, "_NormalAccessor", None)
        if _accessor is None:
            # 3.11 dropped the accessor: pathlib calls os.* and io.open at call
            # time and both are patched above, so there is nothing to re-wrap and
            # nothing is opened up by the removal.
            _paths_mechanism = "pathlib:direct-calls"
        else:
            _rebound, _untouched = 0, 0
            for name in _ACCESSOR_NAMES:
                current = vars(_accessor).get(name)
                if current is None or isinstance(current, staticmethod):
                    continue     # absent, or already unbound and already guarded
                # `open` is the one slot whose module changed between versions.
                patched = [getattr(os, name, None)]
                if name == "open":
                    patched.append(getattr(io, "open", None))
                if any(current is one for one in patched if one is not None):
                    setattr(_accessor, name, staticmethod(current))
                    _rebound += 1
                else:
                    _untouched += 1
            _paths_mechanism = "pathlib:accessor-rebound-%d" % _rebound
            if _untouched:
                _paths_mechanism += "+%d-left-alone" % _untouched

    # Written where the parent reads the phase marker, because the parent reports
    # the layer list and only the child knows which branch above it took. Deriving
    # it in the parent from `sys.version_info` would be reading the mechanism out
    # of source rather than recording what ran, which is the failure this whole
    # label family exists to avoid.
    try:
        with _REAL_OPEN(os.path.join(_WORKDIR, _PATHS_NAME), "w") as handle:
            handle.write(_paths_mechanism)
    except Exception:
        pass


_PHASE = ['startup']


def _phase(name):
    """Record how far we got, so a SIGKILL on timeout is still explainable.

    A killed child prints no traceback -- there is nothing left to classify. So
    the phase goes to a file the parent reads before deleting the workdir. It is
    the only evidence of whether a hang happened while importing the solution,
    while running the tests, or on the way out.
    """
    _PHASE[0] = name
    try:
        with _REAL_OPEN(_PHASE_PATH, "w") as handle:
            handle.write(name)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass


def _watch_solution_import():
    """Bracket `import solution` with phase markers.

    Without this, a solution whose module body loops forever and a solution
    whose tested function loops forever are the same event: killed by SIGKILL,
    empty stderr, exit -9. They need opposite advice.
    """
    import builtins
    _real_import = builtins.__import__

    def _watched(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'solution' and _PHASE[0] != 'import-solution':
            _phase('import-solution')
            # Before _real_import, so the solution's own module body is already
            # covered: it runs during the import, not after it.
            _arm_suite_lock()
            try:
                return _real_import(name, globals, locals, fromlist, level)
            finally:
                _phase('tests')
        return _real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _watched


def _user_frames(exc_type, exc_value, exc_tb):
    summary = traceback.TracebackException(exc_type, exc_value, exc_tb)
    kept = []
    for frame in summary.stack:
        name = os.path.abspath(frame.filename) if frame.filename else ""
        if name == _SELF:
            continue
        if frame.filename.endswith(os.sep + "runpy.py"):
            continue
        if "<frozen importlib" in frame.filename:
            continue
        kept.append(frame)
    if kept:
        summary.stack = traceback.StackSummary.from_list(kept)
    return "".join(summary.format())


def _record_assert_values(exc_type, exc_value, exc_tb):
    """Write the two sides of a failing comparison, re-evaluated where it failed.

    `assert a == b` raises an AssertionError carrying nothing, so the parent can
    quote the assertion's source text and its line number and still not say what
    the code returned. This re-evaluates the two operands in the frame that just
    raised -- same process, same globals, same guards, same limits, nothing
    spawned -- and leaves the reprs where the parent reads the phase marker.

    Re-evaluation, not capture. A function with a side effect or a random
    component need not answer the same way twice, and this runs after the
    failure, not at it. The parent labels the pair accordingly; that label is the
    honest part of the feature and must not be dropped.

    Silent on anything it cannot do exactly: a non-comparison assert, a chained
    comparison, a statement `ast` cannot parse from the one line the traceback
    names (a multi-line assert), an operand that raises on re-evaluation, or a
    child that has already spent its re-evaluation budget.
    """
    if exc_type is not AssertionError:
        return
    if time.monotonic() - _STARTED > _ASSERT_REEVAL_BUDGET:
        return
    frame = lineno = None
    walk = exc_tb
    while walk is not None:
        if os.path.abspath(walk.tb_frame.f_code.co_filename or "") != _SELF:
            frame, lineno = walk.tb_frame, walk.tb_lineno
        walk = walk.tb_next
    if frame is None:
        return
    try:
        import ast
        import linecache
        # linecache, not open(): the suite is unreadable to this process by the
        # time an assertion in it can fail. `_arm_suite_lock` pinned its lines
        # into the cache for exactly this class of reason.
        text = linecache.getline(frame.f_code.co_filename, lineno)
        node = ast.parse(text.strip()).body[0]
        if not isinstance(node, ast.Assert):
            return
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
            return
        sides = []
        for expr in (test.left, test.comparators[0]):
            value = eval(compile(ast.Expression(expr), '<assert>', 'eval'),
                         frame.f_globals, frame.f_locals)
            sides.append(repr(value)[:_ASSERT_VALUE_CHARS])
        with _REAL_OPEN(os.path.join(_WORKDIR, _ASSERT_NAME), 'w') as handle:
            # One side per line, and the reprs are re-encoded so a value whose
            # repr contains a newline cannot forge a second field.
            handle.write("%s\n%s\n" % (sides[0].encode('unicode_escape')
                                       .decode('ascii'),
                                       sides[1].encode('unicode_escape')
                                       .decode('ascii')))
    except BaseException:
        # A repair aid must never change the verdict. Whatever went wrong here,
        # the traceback the caller is about to write is the real answer.
        return


_block_network()
_block_processes()
_block_filesystem()
_apply_limits()
_phase('startup')

if _ENTRY_KIND == 'tests':
    _watch_solution_import()
else:
    # No suite: solution.py is itself the entry point, so there is no import
    # boundary to attribute a hang to.
    _phase('solution')

sys.argv = [_TARGET]
try:
    import runpy
    runpy.run_path(_TARGET, run_name="__main__")
    # Everything ran. Anything that hangs from here is shutdown -- a non-daemon
    # thread still going, or an atexit hook that blocks.
    _phase('teardown')
except SystemExit as exc:
    _phase('teardown')
    code = exc.code
    if code is None:
        code = 0
    if isinstance(code, int):
        sys.exit(code)
    sys.stderr.write("%s\n" % (code,))
    sys.exit(1)
except KeyboardInterrupt:
    sys.stderr.write("KeyboardInterrupt\n")
    sys.exit(130)
except BaseException:
    sys.stderr.write(_user_frames(*sys.exc_info()))
    _record_assert_values(*sys.exc_info())
    sys.exit(1)
'''


# --------------------------------------------------------------------------
# OS-level jail probing
# --------------------------------------------------------------------------

# Every other dimension of the layer list names the dimension first -- `memory:`,
# `paths:`, `in-process:` -- and the OS jail slot alone named the *mechanism* when
# one engaged (`sandbox-exec:no-net`) and the dimension when none did
# (`os-level:unavailable`). So the one slot a reader most wants to select was the
# only one that could not be selected by prefix, and the absence of a jail read as a
# different kind of fact from the presence of one. It is not: both are answers to
# "which OS jail held", so both are `os-level:`. The mechanism keeps its name after
# the colon, because "which jail" is the whole content of the answer.
_MACOS_PROFILES = (
    (
        "os-level:sandbox-exec-no-net+no-write",
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny file-write*)\n"
        '(allow file-write* (subpath "{workdir}")'
        ' (literal "/dev/null") (literal "/dev/stdout")'
        ' (literal "/dev/stderr") (literal "/dev/dtracehelper"))\n',
    ),
    (
        "os-level:sandbox-exec-no-net",
        "(version 1)\n(allow default)\n(deny network*)\n",
    ),
)

# The one string that says no OS jail held. Named rather than spelled out at the
# call site so the family is legible in one place.
OS_LAYER_UNAVAILABLE = "os-level:unavailable"
OS_LAYER_PREFIX = "os-level:"

_probe_cache = {}


def _candidate_wrappers(workdir):
    """Yield ``(label, argv_prefix)`` OS jails to try, strongest first."""
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        # realpath, and it is load-bearing. seatbelt matches `subpath` against the
        # kernel's canonical path, and on macOS every temp directory is reached
        # through a symlink: `tempfile.mkdtemp()` returns
        # `/var/folders/.../harness-xxxx` while the kernel sees
        # `/private/var/folders/...`. Handing seatbelt the unresolved spelling
        # writes a profile whose allow-rule matches nothing, so `(deny
        # file-write*)` applies to the workdir too and every legal in-workdir
        # write fails with EPERM -- charged to the Executor as FAIL_RUNTIME,
        # which is a measurement bug and not a UX wart. Measured: as-given
        # denied, realpath allowed.
        real = os.path.realpath(workdir)
        for label, profile in _MACOS_PROFILES:
            yield label, ["sandbox-exec", "-p", profile.format(workdir=real)]
    elif system == "Linux" and shutil.which("unshare"):
        yield "os-level:unshare-net+user", ["unshare", "--map-root-user", "--net"]
        yield "os-level:unshare-net", ["unshare", "--net"]


_PROBE_NAME = "_harness_jail_probe"

_PROBE_SOURCE = ("open(%r, 'w').write('probe')\n"
                 "print('probe')\n" % _PROBE_NAME)


def _wrapper_works(label, prefix, workdir=None):
    """Probe a jail once and cache the verdict. It must permit a workdir write.

    The probe used to be `print('probe')`, and a jail that denied every write
    inside its own working directory passed it. That is the one failure this
    selection cannot afford to wave through: the jail engages, the label claims
    it, and the solution's first `open(..., 'w')` raises EPERM -- so a correct
    Executor answer is recorded as FAIL_RUNTIME and the measured pass rate is
    depressed by an artefact of the harness. Falling back to a weaker profile is
    the right answer there, and it is only reachable if the probe asks.

    Cached by label, which stays correct because the only per-workdir part of the
    profile is now a canonical path under one fixed temp root.
    """
    if label in _probe_cache:
        return _probe_cache[label]
    ok = False
    try:
        proc = subprocess.run(
            prefix + [sys.executable, "-I", "-B", "-c", _PROBE_SOURCE],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        ok = proc.returncode == 0 and b"probe" in proc.stdout
    except Exception:
        ok = False
    finally:
        if workdir:
            try:
                os.unlink(os.path.join(workdir, _PROBE_NAME))
            except OSError:
                pass
    _probe_cache[label] = ok
    return ok


def _select_wrapper(workdir):
    for label, prefix in _candidate_wrappers(workdir):
        if _wrapper_works(label, prefix, workdir):
            return label, prefix
    return None, []


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

# Failure taxonomy. These need different guidance handed back, so they are
# tracked rather than lumped into one "it broke".
FAIL_NONE = ""
FAIL_IMPORT = "import"          # solution.py could not even be imported
FAIL_ASSERTION = "assertion"    # it imported and ran, but a test assert failed
FAIL_RUNTIME = "runtime"        # it raised something other than a test assert
FAIL_TIMEOUT = "timeout"        # never finished, and we cannot say where
FAIL_TIMEOUT_IMPORT = "timeout-import"      # hung importing solution.py
FAIL_TIMEOUT_TESTS = "timeout-tests"        # imported, then hung under test
FAIL_TIMEOUT_TEARDOWN = "timeout-teardown"  # tests done, process never exited
FAIL_OUTPUT = "runaway-output"  # wrote more than the harness will read
FAIL_PATH = "harness-path"      # `import solution` broke -- a harness bug
FAIL_SYNTAX = "syntax"          # it does not compile; proved without running it

# One timeout used to cover three situations that need opposite advice, so they
# are separate kinds. Anything that needs "was this a timeout at all" uses this.
TIMEOUT_KINDS = frozenset((FAIL_TIMEOUT, FAIL_TIMEOUT_IMPORT,
                           FAIL_TIMEOUT_TESTS, FAIL_TIMEOUT_TEARDOWN))


def is_timeout(failure_kind):
    return failure_kind in TIMEOUT_KINDS


# Which phase marker the child left behind maps to which kind.
_PHASE_TO_KIND = {
    PHASE_IMPORT: FAIL_TIMEOUT_IMPORT,
    PHASE_TESTS: FAIL_TIMEOUT_TESTS,
    PHASE_TEARDOWN: FAIL_TIMEOUT_TEARDOWN,
}


@dataclass
class ExecResult:
    """What actually happened when we ran the code."""

    ran: bool = False
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration: float = 0.0
    reason: str = ""  # populated only when ran is False
    language: str = ""
    source: str = ""
    tests: str = ""
    entry: str = SCRIPT_NAME
    failure_kind: str = FAIL_NONE
    failed_assertion: str = ""
    failed_assertion_line: Optional[int] = None
    # The two operands of that assertion, re-evaluated in the child after it
    # raised -- see `_record_assert_values`. Empty whenever the child could not
    # produce them exactly, which is most non-comparison asserts. Reprs, already
    # clamped, and never to be presented as "what your code returned": they were
    # obtained after the failure, not at it.
    failed_assertion_left: str = ""
    failed_assertion_right: str = ""
    ignored_test_block: bool = False
    sandbox_layers: List[str] = field(default_factory=list)
    truncated: bool = False
    output_capped: bool = False   # breached MAX_CAPTURE_BYTES and was killed
    stdout_bytes: int = 0         # bytes the child *emitted*, not bytes kept
    stderr_bytes: int = 0
    phase: str = ""               # how far the child got; see PHASE_* above

    # Per-check accounting, parsed out of stdout. Descriptive only: nothing here
    # is allowed to move `ok`, the failure kind or the verdict, all of which are
    # still decided by exit code and stderr exactly as before these existed.
    checks_total: int = 0
    checks_passed: int = 0
    checks_kinds: List[str] = field(default_factory=list)
    checks_trusted: bool = False
    checks_note: str = ""

    @property
    def ok(self):
        return self.ran and not self.timed_out and self.exit_code == 0

    @property
    def tested(self):
        return bool(self.tests)


def _child_env(workdir):
    """A deliberately bare environment. `-I` already ignores PYTHON* vars."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": workdir,
        "TMPDIR": workdir,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "NO_COLOR": "1",
        # Anything network-ish or credential-ish is intentionally absent.
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "COMSPEC", "PATHEXT"):
            if name in os.environ:
                env[name] = os.environ[name]
    return env


def _truncate(text, limit=MAX_STREAM_CHARS):
    if len(text) <= limit:
        return text, False
    keep = limit - 80
    head = keep * 2 // 3
    tail = keep - head
    elided = len(text) - head - tail
    return (
        "%s\n\n[... %d characters elided by the harness ...]\n\n%s"
        % (text[:head], elided, text[-tail:]),
        True,
    )


def _memory_layer():
    """An honest label for the memory dimension of the sandbox.

    There is no portable memory cap. `setrlimit(RLIMIT_AS)` is *attempted* in the
    child everywhere, but macOS accepts the call and then ignores it, so a
    runaway allocation there is bounded by nothing except the machine. Reporting
    `memory:rlimit-as` on that platform would be a lie in the one place someone
    reads to find out what actually held, so the dimension says `unguarded`
    where it is unguarded. Wall-clock and CPU limits are the real backstop:
    thrashing code hits the timeout.
    """
    try:
        import resource
    except Exception:
        return "memory:unguarded-no-resource-module"
    megabytes = MAX_MEMORY_BYTES // (1024 * 1024)
    system = platform.system()
    if system == "Darwin":
        return "memory:unguarded-on-darwin"
    if system == "Windows" or os.name == "nt":
        return "memory:unguarded-on-windows"
    if hasattr(resource, "RLIMIT_AS"):
        return "memory:rlimit-as-%dmb" % megabytes
    if hasattr(resource, "RLIMIT_DATA"):
        return "memory:rlimit-data-%dmb" % megabytes
    return "memory:unguarded"


def _path_layer(os_label):
    """An honest label for the path dimension: what held, not what was installed.

    `in-process:no-outside-writes` names a mechanism -- the wrappers around
    `open`, `os.open` and the destructive `os`/`shutil` entry points. It does not
    say whether anything stood behind them, and the answer differs per machine.
    The macOS profile's `(deny file-write*)` is kernel-enforced and real;
    `unshare` isolates the network and the user namespace and denies no write at
    all; and inside a nested sandbox neither is available. Where the OS layer is
    absent, the in-process guard is the only thing between a hallucinated
    `shutil.rmtree("/Users/<someone>")` and the disk -- and it is bypassable on
    purpose, through ctypes, an entry point the table does not list, or importlib.
    Part G of the pre-registration calls that accident containment rather than a
    security boundary, and this label is where a reader finds out whether the
    containment was standing on its own.
    """
    if "no-write" in (os_label or ""):
        return "paths:in-process-guard+os-write-deny"
    return "paths:in-process-guard-only"


def _pump(stream, chunks, cap, seen, breached):
    """Read one child stream to EOF on its own thread, keeping at most ``cap``.

    Everything past the cap is read and *dropped* rather than left sitting in
    the pipe: if the parent stops draining, the child blocks on a full pipe
    buffer, which looks exactly like a hang and delays the kill.
    """
    kept = 0
    try:
        while True:
            if hasattr(stream, "read1"):
                chunk = stream.read1(65536)   # returns as soon as data is there
            else:
                chunk = stream.read(65536)
            if not chunk:
                break
            seen[0] += len(chunk)
            if kept < cap:
                room = cap - kept
                chunks.append(chunk[:room])
                kept += min(room, len(chunk))
            if seen[0] > cap and not breached.is_set():
                breached.set()
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _collect(proc, timeout, cap=MAX_CAPTURE_BYTES):
    """Drain the child incrementally, bounded in both time *and* bytes.

    `proc.communicate(timeout=...)` reads the entire child stream into the
    parent's memory before anything gets to truncate it, so
    `while True: print("x" * 1000)` exhausts the parent -- the harness taking
    down the app it exists to protect. Instead: reader threads with a hard
    per-stream ceiling, and the process group is killed the moment the ceiling
    is breached.

    Returns ``(stdout, stderr, seen_out, seen_err, timed_out, capped)``.
    """
    out_chunks, err_chunks = [], []
    seen_out, seen_err = [0], [0]
    breached = threading.Event()
    threads = []
    for stream, chunks, seen in ((proc.stdout, out_chunks, seen_out),
                                 (proc.stderr, err_chunks, seen_err)):
        if stream is None:
            continue
        thread = threading.Thread(target=_pump,
                                  args=(stream, chunks, cap, seen, breached))
        thread.daemon = True
        thread.start()
        threads.append(thread)

    deadline = time.monotonic() + timeout
    timed_out = False
    capped = False
    while True:
        if breached.is_set():
            capped = True
            _kill_tree(proc)
            break
        try:
            proc.wait(timeout=_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_tree(proc)
            break

    # The kill is asynchronous, and the pumps may still be a chunk behind. Wait
    # for the corpse and for EOF so no output is lost and no thread outlives the
    # call -- but with a ceiling, because a wedged reader must not hang the app.
    try:
        proc.wait(timeout=_REAP_SECONDS)
    except Exception:
        _kill_tree(proc)
        try:
            proc.wait(timeout=_REAP_SECONDS)
        except Exception:
            pass
    for thread in threads:
        thread.join(_REAP_SECONDS)
    return (b"".join(out_chunks), b"".join(err_chunks),
            seen_out[0], seen_err[0], timed_out, capped)


def _is_cpu_ceiling_death(sig, duration, timeout):
    """Is this signal death really CPU exhaustion, i.e. a timeout?

    RLIMIT_CPU delivers SIGXCPU at the soft limit and SIGKILL at the hard one.
    Either way the child burned every CPU second it was given, which *is* a
    timeout -- and `exit -24` tells the Executor nothing. Reachable inside the
    wall clock despite CPU_GRACE_SECONDS, because RLIMIT_CPU counts every thread:
    four busy threads spend four CPU seconds per wall second.

    SIGKILL only counts when the run also lasted at least as long as the wall
    clock, since SIGKILL is what the harness itself sends.
    """
    xcpu = getattr(signal, "SIGXCPU", None)
    if xcpu is not None and sig == int(xcpu):
        return True
    kill = getattr(signal, "SIGKILL", None)
    if kill is not None and sig == int(kill):
        return duration >= float(timeout)
    return False


def _read_phase(workdir):
    """How far the child got, per the marker file it left in the workdir."""
    try:
        with open(os.path.join(workdir, PHASE_NAME), "r",
                  encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()[:40]
    except Exception:
        return ""


def _read_paths_mechanism(workdir):
    """Which pathlib mechanism the child's path guard used, per its marker.

    Only a `pathlib:` label is accepted. A truncated or garbled marker is the same
    situation as no marker at all -- the dimension went unreported -- and saying so
    is the point of this label family.
    """
    try:
        with open(os.path.join(workdir, PATHS_NAME), "r",
                  encoding="utf-8", errors="replace") as handle:
            said = handle.read().strip()[:60]
    except Exception:
        return PATHS_UNREPORTED
    return said if said.startswith("pathlib:") else PATHS_UNREPORTED


def _read_assert_values(workdir):
    """``(left, right)`` reprs the child re-evaluated, or ``("", "")``.

    Two lines, `unicode_escape`d by the writer so a repr containing a newline
    cannot present itself as a second field. Anything other than exactly two
    lines is discarded whole rather than half-trusted: a partial pair is a
    misleading pair, and the message reads fine without either.
    """
    try:
        with open(os.path.join(workdir, ASSERT_NAME), "r",
                  encoding="utf-8", errors="replace") as handle:
            said = handle.read()
    except Exception:
        return "", ""
    lines = said.splitlines()
    if len(lines) != 2:
        return "", ""
    out = []
    for line in lines:
        try:
            out.append(line.encode("ascii", "replace")
                       .decode("unicode_escape")[:MAX_ASSERT_VALUE_CHARS])
        except Exception:
            return "", ""
    return out[0], out[1]


# ------------------------------------------------------------------ 3.9, said
# again with the failure in hand
#
# The interpreter is 3.9, and the two constructs a model reaches for anyway fail
# in two different places. `match`/`case` is a SyntaxError, which the parent can
# prove without spawning anything. A PEP 604 union in an *evaluated* position is
# a TypeError at runtime -- `isinstance(x, int | str)`, `cast(int | None, v)`,
# `Num = int | float` -- and annotations are already neutralised by
# `_SOURCE_PROLOGUE`, so those three spellings are the whole residue.
#
# The prompt-side twin is `agents_core._RUNTIME_RULE`, which states both rules
# before the Executor writes anything. This half states them again with the
# failure in hand, which is the form that has some chance of landing. Both halves
# name the same two constructs and a check in test_pipeline.py pins that they
# still do: the pairing is the mechanism, as it is for the test rules.
_MATCH_STMT = re.compile(r"(?m)^[ \t]*(?:match|case)\b[^\n]*:[ \t]*$")

# Measured on 3.9.6 rather than guessed: the residue forms report 'type' and
# 'type', 'type' and 'NoneType', '_UnionGenericAlias' and 'type', or
# 'types.GenericAlias' and 'NoneType'. A model's own `3 | "a"` reports 'int' and
# 'str' and is a plain bug, so at least one side has to name a type object before
# this claims a version cause.
_PIPE_TYPEERROR = re.compile(
    r"unsupported operand type\(s\) for \|: '([^']+)' and '([^']+)'")
_TYPE_OBJECT = re.compile(
    r"\A(?:type|NoneType|_SpecialForm|.*GenericAlias|.*Meta)\Z")

_MATCH_ADVICE = (
    "`match`/`case` is 3.10 syntax and this interpreter is %d.%d, so it is a "
    "SyntaxError here rather than a style question. Rewrite it as `if`/`elif` "
    "over the same conditions." % sys.version_info[:2])
_UNION_ADVICE = (
    "`X | Y` between two types is PEP 604, which is 3.10+. Annotations survive "
    "here because the harness inserts `from __future__ import annotations`, but "
    "an evaluated union does not: `isinstance(x, int | str)`, "
    "`cast(int | None, v)` and `Num = int | float` all raise this TypeError. Use "
    "`typing.Union[int, str]` or `typing.Optional[int]`, or a tuple in "
    "`isinstance`.")


def _version_hint(source="", stderr=""):
    """One line naming the 3.10 construct behind a failure, or ``""``.

    Both inputs are optional because the two constructs surface in different
    places: the match statement is in the source the parent has just failed to
    compile, and the union is only in the TypeError the child raised.
    """
    if source and _MATCH_STMT.search(source):
        return _MATCH_ADVICE
    found = _PIPE_TYPEERROR.search(stderr or "")
    if found and any(_TYPE_OBJECT.match(side) for side in found.groups()):
        return _UNION_ADVICE
    return ""


def _compile_problem(source):
    """``(reason, stderr)`` when the parent cannot compile ``source``, else None.

    `compile` rather than `ast.parse`, deliberately: it also refuses `return`
    outside a function and a duplicate argument name, both of which parse
    cleanly and then die at import. The source compiled is the model's own, not
    `executed_source(source)`, so the line number reported is the one the model
    counted -- the prologue's line shift is a standing cost of running the
    prologue, and there is no reason to pay it on the one path that never gets
    that far. Nothing is legal only *with* the prologue: a future statement
    changes when annotations are evaluated, never what parses, so refusing the
    raw source cannot condemn a file the child would have accepted.

    Compiled as **utf-8 bytes**, which is what `run_python_sandboxed` writes and
    what the child's tokenizer therefore reads. A str is a different language in
    one measured place, and it is legal Python that this gate would otherwise
    have condemned: a leading byte-order mark is prelude to a file and `invalid
    non-printable character U+FEFF` to a str. See `_BOM` and
    `test_a_byte_order_mark_is_prelude_and_not_a_statement`, which measured
    exactly that and says a str-level check cannot see the shape at all. An
    encoding declaration was measured too and is *not* such a place -- 3.9.6
    accepts a cookie in either input -- but bytes is still the faithful reading,
    because the cookie is then resolved against the same bytes the child gets.

    Encoding here also catches a source that cannot be written as utf-8 at all
    (a lone surrogate): `UnicodeEncodeError` is a `ValueError`, so it arrives as
    a compile problem instead of as an exception out of the file write.
    """
    try:
        compile(source.encode("utf-8"), SCRIPT_NAME, "exec")
        return None
    except SyntaxError as exc:
        return ("%s does not compile: %s (line %s)"
                % (SCRIPT_NAME, exc.msg, exc.lineno),
                "".join(traceback.format_exception_only(type(exc), exc)))
    except ValueError as exc:
        # Null bytes in the source, or a lone surrogate that will not encode.
        # Neither is a SyntaxError, and neither is something to let escape into
        # the caller: the child would die on both just as certainly, so this is
        # the same answer arrived at one process earlier.
        return ("%s cannot be compiled: %s" % (SCRIPT_NAME, exc),
                "".join(traceback.format_exception_only(type(exc), exc)))


def run_python_sandboxed(source, tests=None, timeout=EXEC_TIMEOUT_SECONDS):
    """Run ``source`` in an isolated subprocess and capture what it did.

    ``source`` is always written as ``solution.py``. When ``tests`` is given it
    is written as ``test_solution.py`` in a sibling directory -- readable by the
    child but outside the directory it can name, write or list -- and becomes the
    entry point, with ``solution`` importable. With no tests the behaviour is
    unchanged from Phase 1: ``solution.py`` itself is the entry point.
    """
    result = ExecResult(language="python", source=source, tests=tests or "")
    if not source or not source.strip():
        result.reason = "no source to execute"
        return result
    if tests is not None and not str(tests).strip():
        result.reason = "an empty test file was supplied"
        return result
    if not sys.executable:
        result.reason = "no Python interpreter available to the harness"
        return result

    # Compiled here, in the parent, before anything is spawned. The child would
    # report the same SyntaxError, but it would cost a temp directory, a jail
    # probe and a process to learn what this interpreter -- the same 3.9 that
    # would have run it -- proves for free. `ran` stays False, because nothing
    # ran; `FAIL_SYNTAX` is what stops `verify_output` reading that as a harness
    # failure, since a defect the parent can prove is still the candidate's.
    problem = _compile_problem(source)
    if problem:
        result.failure_kind = FAIL_SYNTAX
        result.reason, result.stderr = problem
        return result

    # Two directories under one root, and the split is the whole point. The
    # suite used to be written next to solution.py, which meant a candidate
    # could `open(TEST_NAME)` -- or list cwd and find it -- and read the answers
    # it was about to be graded against. It now lives outside the only directory
    # the child can name relatively, write to, or see by listing. One root keeps
    # cleanup a single rmtree.
    root = tempfile.mkdtemp(prefix="harness-")
    workdir = os.path.join(root, "work")
    try:
        os.mkdir(workdir)
        script_path = os.path.join(workdir, SCRIPT_NAME)
        with open(script_path, "w", encoding="utf-8") as handle:
            # The prologue goes into the model's text, never over it, and after
            # the module docstring rather than at line 1 -- `executed_source`
            # says why, and says it about a case that was measured, not
            # assumed. Line 1 is legal in isolation; line 1 *plus* a docstring
            # plus the model's own future statement is not, and line 1 alone
            # silently emptied `solution.__doc__`.
            # Cost: in raw stderr, solution.py line numbers at and after the
            # insertion point are one higher than the model's own count, and
            # that stderr becomes repair context. Lines above it -- including an
            # encoding declaration, which must stay on line 1 or 2 -- are not
            # shifted at all, which line 1 could not promise.
            # `failed_assertion_line` is unaffected; it is only set for frames
            # in TEST_NAME, which gets no prologue.
            handle.write(executed_source(source))

        test_path = None
        if tests:
            # A sibling of the workdir, not a member of it. The child can still
            # read it -- runpy has to, to run it -- and cannot write it at either
            # layer: `_block_filesystem` refuses writes outside the workdir and
            # the seatbelt profile is `deny file-write*` with an allow-subpath
            # for the workdir alone. The reads a solution could make on top of
            # runpy's are refused separately, from `import solution` onwards;
            # see `_arm_suite_lock` in the runner.
            suitedir = os.path.join(root, "suite")
            os.mkdir(suitedir)
            test_path = os.path.join(suitedir, TEST_NAME)
            with open(test_path, "w", encoding="utf-8") as handle:
                # Deliberately no prologue: the artifact lives in the solution
                # module, the baked suites carry no annotations, and editing
                # suite text would put the task lock in question.
                handle.write(tests)

        entry_path = test_path or script_path
        result.entry = os.path.basename(entry_path)

        runner_path = os.path.join(workdir, "_harness_runner.py")
        with open(runner_path, "w", encoding="utf-8") as handle:
            handle.write(_RUNNER_SOURCE)

        label, prefix = _select_wrapper(workdir)
        layers = ["in-process:isolated-interpreter", "in-process:no-network",
                  "in-process:no-subprocess", "in-process:no-outside-writes",
                  "in-process:rlimits", _memory_layer()]
        layers.append(label if label else OS_LAYER_UNAVAILABLE)
        # Appended after the OS label because it is a statement *about* it: the
        # line above says which OS jail ran, this one says whether the path
        # guard had it behind them or was standing alone.
        layers.append(_path_layer(label))
        # And this one says *how* the path guard reached pathlib, which the child
        # decides and only the child can report. It starts as unreported and is
        # replaced once the child has run: a child that never started leaves this
        # dimension genuinely unanswered, and the label says so rather than naming
        # a mechanism nothing exercised.
        layers.append(PATHS_UNREPORTED)
        result.sandbox_layers = layers

        argv = list(prefix) + [
            sys.executable, "-I", "-B", runner_path, entry_path,
            str(int(timeout) + CPU_GRACE_SECONDS), str(MAX_WRITE_BYTES),
            str(MAX_MEMORY_BYTES), "tests" if tests else "solution",
            os.path.join(workdir, PHASE_NAME),
        ]

        popen_kwargs = dict(
            cwd=workdir,
            env=_child_env(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if os.name == "posix":
            # Own process group, so a forking or thread-spawning script can be
            # torn down wholesale on timeout instead of leaking children.
            popen_kwargs["start_new_session"] = True

        started = time.monotonic()
        try:
            proc = subprocess.Popen(argv, **popen_kwargs)
        except Exception as exc:
            result.reason = "could not start sandboxed process: %s" % exc
            return result

        raw_out, raw_err, seen_out, seen_err, timed_out, capped = _collect(
            proc, timeout)
        result.duration = time.monotonic() - started
        result.timed_out = timed_out
        result.output_capped = capped
        result.stdout_bytes = seen_out
        result.stderr_bytes = seen_err
        # Read before the workdir goes away in the finally block.
        result.phase = _read_phase(workdir)
        result.sandbox_layers[-1] = _read_paths_mechanism(workdir)
        (result.failed_assertion_left,
         result.failed_assertion_right) = _read_assert_values(workdir)

        result.ran = True
        result.exit_code = proc.returncode
        stdout = _decode(raw_out)
        stderr = _decode(raw_err)

        # Exactly one harness note, so the Executor is never told two different
        # stories about why its process died.
        if timed_out:
            stderr = (stderr + "\n" if stderr else "") + (
                "harness: killed after %ss wall-clock timeout -- the program "
                "timed out (likely an infinite loop or a blocking read)" % timeout
            )
        elif capped:
            stderr = (stderr + "\n" if stderr else "") + (
                "harness: killed after emitting more than %d bytes to one "
                "stream (stdout %d, stderr %d) -- runaway output. The harness "
                "reads at most %d bytes per stream so that a printing loop "
                "cannot exhaust the parent process."
                % (MAX_CAPTURE_BYTES, seen_out, seen_err, MAX_CAPTURE_BYTES)
            )
        elif proc.returncode is not None and proc.returncode < 0:
            sig = -proc.returncode
            if _is_cpu_ceiling_death(sig, result.duration, timeout):
                # Semantically a timeout, so report it as one rather than as a
                # bare negative exit code.
                result.timed_out = True
                stderr = (stderr + "\n" if stderr else "") + (
                    "harness: the process was killed by %s after %.1fs -- it "
                    "exhausted the CPU ceiling, so the program timed out "
                    "(likely an infinite loop)" % (_signal_name(sig), result.duration)
                )
            else:
                # A bare negative exit code tells the Executor nothing. Name it.
                stderr = (stderr + "\n" if stderr else "") + (
                    "harness: the process was killed by %s (exit %s)"
                    % (_signal_name(sig), proc.returncode)
                )

        # Tracebacks carry the throwaway temp path; make them readable.
        for full, short in ((script_path, SCRIPT_NAME), (test_path, TEST_NAME)):
            if full:
                stdout = stdout.replace(full, short)
                stderr = stderr.replace(full, short)

        result.stdout, out_cut = _truncate(stdout)
        result.stderr, err_cut = _truncate(stderr)
        result.truncated = out_cut or err_cut
        _classify(result)
        return result
    finally:
        # The root, so the sibling suite directory goes with it.
        shutil.rmtree(root, ignore_errors=True)


def _kill_tree(proc):
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _decode(raw):
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _signal_name(number):
    """``9`` -> ``SIGKILL``. Unknown numbers degrade to a readable string."""
    try:
        return signal.Signals(number).name
    except (ValueError, AttributeError):
        return "signal %s" % number


_FRAME = re.compile(r'File "([^"]+)", line (\d+)[^\n]*\n[ \t]*([^\n]*)')

_CHECK_RESULT = re.compile(r"^%s (\d+) (pass|fail) (\S+)$" % re.escape(CHECK_TAG))
_CHECK_DONE = re.compile(r"^%s done (\d+)$" % re.escape(CHECK_TAG))


def read_checks(stdout):
    """How many of a generated suite's checks passed. `(passed, total, kinds, note)`.

    `note` is empty when the report is coherent and says what was wrong when it
    is not; an incoherent report yields `(0, 0, [], note)` and the caller must
    treat it as "unknown", never as "zero of N passed".

    The parse is deliberately all-or-nothing, because the suite shares stdout
    with the code under test. A solution is free to print `CHECK_TAG` lines, so
    the guarantee cannot be "we ignore fabricated lines" -- we cannot tell which
    are ours. It is instead that fabrication can only ever *destroy* a report:

      * the suite emits exactly one line per check, with the ids 1..N each
        appearing exactly once, then exactly one `done N`, and this requires
        precisely that. A forged line is a duplicate id, an out-of-range id or an
        extra line, and each of those fails the shape.
      * a forged *whole* report collides with the real one on `done`, and two
        `done` lines fail the shape.
      * the real report is written last, so suppressing it means exiting before
        it -- which the exit code and the missing report both record.

    So the worst a hostile solution achieves is a rank of "unknown", which is
    the floor. It cannot manufacture a pass count it did not earn.

    The ids are required as a *set* and not as an ascending sequence. They are
    emitted in execution order, and `eval/rank_battery.py` reorders a frozen
    suite's checks on purpose to ask whether the ranking key depends on their
    order -- so ascending arrival is a property of the unpermuted suite, not of a
    trustworthy report. Nothing is given up: every id in 1..N is already spoken
    for by the real report, so an extra line is still a duplicate.
    """
    ids, results, done = [], {}, []
    for line in stdout.splitlines():
        line = line.strip()
        match = _CHECK_RESULT.match(line)
        if match:
            ids.append(int(match.group(1)))
            results[int(match.group(1))] = (match.group(2), match.group(3))
            continue
        match = _CHECK_DONE.match(line)
        if match:
            done.append(int(match.group(1)))

    if not done:
        # No report at all is the normal case for a hand-written suite, so it is
        # stated as an absence rather than as a fault. Ids with no terminator is
        # a different thing: the report started and did not finish.
        return 0, 0, [], ("report has %d line(s) and no completion marker" % len(ids)
                          if ids else "no per-check report")
    if len(done) > 1:
        return 0, 0, [], "%d completion markers, so at least one is forged" % len(done)
    total = done[0]
    if sorted(ids) != list(range(1, total + 1)):
        # Covers every shape failure at once: duplicates, gaps and extras.
        return 0, 0, [], ("check ids %s do not match 1..%d exactly"
                          % (sorted(set(ids))[:8] or "[]", total))
    passed = sum(1 for index in ids if results[index][0] == "pass")
    kinds = [results[index][1] for index in sorted(ids)]
    return passed, total, kinds, ""


def _classify(result):
    """Work out *how* it failed, since each mode needs different guidance."""
    (result.checks_passed, result.checks_total, result.checks_kinds,
     result.checks_note) = read_checks(result.stdout)
    # A total of zero is never trustworthy, whatever the note says: there is no
    # report to trust, and "0 of 0 passed" must not read as a measured floor.
    result.checks_trusted = bool(result.checks_total) and not result.checks_note
    if result.ok:
        result.failure_kind = FAIL_NONE
        return
    if result.output_capped:
        # More specific and more actionable than the timeout it would otherwise
        # have become, so it wins.
        result.failure_kind = FAIL_OUTPUT
        return
    if result.timed_out:
        # A killed child leaves no traceback, so the phase marker it wrote is
        # the only thing that separates "hung on import", "hung under test" and
        # "finished but never exited" -- three problems with opposite fixes.
        result.failure_kind = _PHASE_TO_KIND.get(result.phase, FAIL_TIMEOUT)
        return

    stderr = result.stderr
    if "No module named 'solution'" in stderr or 'No module named "solution"' in stderr:
        # sys.path repair failed. That is our bug, not the Executor's.
        result.failure_kind = FAIL_PATH
        return

    frames = _FRAME.findall(stderr)
    last = frames[-1] if frames else None
    # The *deepest* frame is what matters. If solution.py has its own failing
    # module-level assert, an AssertionError appears in stderr but the deepest
    # frame is solution.py, not the suite -- that is a broken module, not a
    # wrong answer, and conflating them sends the Executor the wrong advice.
    if last and os.path.basename(last[0]) == TEST_NAME and "AssertionError" in stderr:
        result.failure_kind = FAIL_ASSERTION
        result.failed_assertion_line = int(last[1])
        result.failed_assertion = last[2].strip()
        return

    if result.tested:
        importish = ("SyntaxError" in stderr or "IndentationError" in stderr
                     or "ImportError" in stderr
                     or "ModuleNotFoundError" in stderr
                     or "cannot import name" in stderr)
        # A suite frame sitting on its own import line means the solution blew
        # up while being imported, before any test could run.
        died_on_import = any(
            os.path.basename(name) == TEST_NAME
            and re.match(r"(?:from|import)\s+solution\b", text.strip())
            for name, _, text in frames
        )
        result.failure_kind = (FAIL_IMPORT if (importish or died_on_import)
                               else FAIL_RUNTIME)
        return

    result.failure_kind = FAIL_RUNTIME


# --------------------------------------------------------------------------
# the vacuous-test guard
# --------------------------------------------------------------------------

@dataclass
class TestAudit:
    """Is this suite worth trusting?"""

    ok: bool = False
    reason: str = ""
    assert_count: int = 0
    names: List[str] = field(default_factory=list)
    stub_exit: Optional[int] = None
    stub_stderr: str = ""
    vacuous: bool = False
    # Error-coverage, populated only when a spec was supplied and the suite got
    # far enough to be worth measuring. `clauses` is what the spec states,
    # `reached` what the suite names, `uncovered` the difference. All three stay
    # empty when no spec is passed, which is why every caller that only wants the
    # vacuity verdict is unaffected by this pair of gates existing.
    clauses: List[Tuple[str, str]] = field(default_factory=list)
    reached: List[str] = field(default_factory=list)
    uncovered: List[Tuple[str, str]] = field(default_factory=list)

    def summary(self):
        if not self.ok:
            return self.reason
        text = "%d assert(s) over %s -- fails against a stub, so it tests something" % (
            self.assert_count, ", ".join(self.names) or "the solution module")
        if self.uncovered:
            text += "; %d of %d stated error(s) unexercised: %s" % (
                len(self.uncovered), len(self.clauses),
                ", ".join(name for name, _ in self.uncovered))
        elif self.clauses:
            text += "; all %d stated error(s) exercised" % len(self.clauses)
        return text


def _solution_names(tree):
    """Names the suite pulls out of the solution module.

    Returns ``(names, wildcard)``. Covers both ``from solution import a, b``
    and ``import solution`` + ``solution.a`` attribute access.
    """
    names, wildcard = set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == SOLUTION_MODULE:
            for alias in node.names:
                if alias.name == "*":
                    wildcard = True
                else:
                    names.add(alias.name)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == SOLUTION_MODULE:
                names.add(node.attr)
    return names, wildcard


def _stub_source(names):
    """A solution module where nothing is implemented.

    Any assert that actually exercises the solution must fail against this. One
    that passes anyway was never testing the solution.
    """
    lines = ['"""Stub solution -- every entry point raises."""', ""]
    for name in sorted(names):
        lines.append("def %s(*args, **kwargs):" % name)
        lines.append("    raise NotImplementedError(%r)" % ("stub: %s" % name))
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# does the suite reach the errors the spec states?
# --------------------------------------------------------------------------
#
# The prompt half of this lives in `agents_core._TEST_RULES` and is unmeasured
# by construction: a model can be told to exercise every stated error and
# simply not. This is the half that does not depend on a model obeying
# anything -- the spec's prose names the exception, the suite's AST either puts
# that name in an executable position or it does not.
#
# Both halves live here rather than split across modules because the *pairing*
# is the mechanism, and the suite side has to read the tree `audit_tests`
# already parsed.

# Built from `builtins` rather than listed. `StopIteration` and
# `KeyboardInterrupt` end in neither `Error` nor `Exception`, so no suffix rule
# would catch them, and a hand-written list would drift from the interpreter the
# child actually runs.
_BUILTIN_EXCEPTIONS = frozenset(
    name for name in dir(builtins)
    if isinstance(getattr(builtins, name), type)
    and issubclass(getattr(builtins, name), BaseException))

# A spec may define its own, and by convention it is suffixed. Requiring the
# suffix is what keeps `Decimal`, `Counter` and every other capitalised name out.
_CUSTOM_EXCEPTION = re.compile(r"\A[A-Z][A-Za-z0-9_]*(?:Error|Exception)\Z")

# The verb is required. A spec that merely mentions `ValueError` in passing --
# "returns 0 rather than a ValueError" -- states no error behaviour, and reading
# a bare mention as a clause would spend a Test Writer call on nothing.
_RAISE_VERB = re.compile(r"\b(?:rais\w*|reject\w*|throw\w*|refus\w*|error out)\b",
                         re.I)

# The negations that invert a clause: "never raises", "does not raise",
# "instead of raising", "without raising". Scoped to the handful of words
# immediately before the verb, because that is where English puts them.
_NEGATED = re.compile(
    r"\b(?:never|not|n't|without|no|instead of|rather than|avoid\w*)\b"
    r"[^.;:]{0,24}\Z", re.I)

_SENTENCE_SPLIT = re.compile(r"(?<=[.;:!?])\s+|\n+")

# A handler that names one of these reaches any error path, so it credits every
# clause. Generous on purpose, and the vacuity gate is what stops it being a
# hole: a suite whose asserts all sit inside a broad `except` also swallows the
# stub's `NotImplementedError`, exits 0 against the stub, and is rejected as
# vacuous before coverage is ever consulted.
_CATCH_ALL = frozenset(("Exception", "BaseException"))


def _exception_names(text):
    """Exception-shaped names in `text`, in order of appearance, no duplicates."""
    found = []
    for match in re.finditer(r"\b[A-Za-z_]\w*\b", text):
        name = match.group(0)
        if name in found:
            continue
        if name in _BUILTIN_EXCEPTIONS or _CUSTOM_EXCEPTION.match(name):
            found.append(name)
    return found


def spec_error_clauses(spec):
    """The error behaviours a spec states, as ``(exception, sentence)`` pairs.

    Prose in, exception names out, one clause per distinct name. Biased towards
    false positives on purpose: a clause that is not really a clause costs one
    Test Writer call and a banner naming it, while a missed clause costs a suite
    that never touches the error path the candidate is graded on. The asymmetry
    is the whole design.

    Stated limits, none of them claims of completeness. A clause is scoped to one
    sentence, so a name and its verb must co-occur there. An error stated without
    naming an exception -- "raises on empty input" -- yields nothing, because
    there is no name to match against the suite's AST. And the first sentence to
    name an exception is the one quoted for it; a second mention adds no clause.
    """
    clauses, seen = [], set()
    for sentence in _SENTENCE_SPLIT.split(spec or ""):
        sentence = " ".join(sentence.split())
        if not sentence:
            continue
        verb = _RAISE_VERB.search(sentence)
        if verb is None or _NEGATED.search(sentence[:verb.start()]):
            continue
        for name in _exception_names(sentence):
            if name not in seen:
                seen.add(name)
                clauses.append((name, sentence))
    return clauses


def _named_exceptions(node):
    """Exception names inside an `except` type or a call argument."""
    found = set()
    for inner in ast.walk(node):
        name = None
        if isinstance(inner, ast.Name):
            name = inner.id
        elif isinstance(inner, ast.Attribute):
            name = inner.attr
        if name and (name in _BUILTIN_EXCEPTIONS or _CUSTOM_EXCEPTION.match(name)):
            found.add(name)
    return found


def suite_error_reach(tree):
    """Exception names the suite puts in an executable position.

    Two positions count, and both are AST facts rather than text matches: the
    type of an `except` handler, and an argument to a call -- which is how a
    helper names the exception it expects, as in `assert_raises(ValueError, f, x)`
    or `with raises(KeyError):`. A bare `except:` counts as nothing, because it
    names nothing. A name in a comment or a string counts as nothing either.
    """
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            reached |= _named_exceptions(node.type)
        elif isinstance(node, ast.Call):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                reached |= _named_exceptions(arg)
    return reached


def audit_tests(test_source, timeout=EXEC_TIMEOUT_SECONDS, spec=None):
    """Decide whether a suite is non-vacuous enough to gate APPROVED on.

    Two gates, in order, and the order is the point. Vacuity is hard and decides
    `ok`: a suite that passes against a stub proves nothing and cannot gate
    anything. Error coverage is soft and only decides `uncovered`, because a
    suite that asserts real values but skips a stated error path is worth less
    than it should be and still worth more than nothing. `spec` is optional, so a
    caller with no spec in hand gets exactly the old verdict.
    """
    audit = TestAudit()
    if not test_source or not test_source.strip():
        audit.reason = "no test source was produced"
        return audit
    if len(test_source) > MAX_TESTS_CHARS:
        audit.reason = "test file is implausibly large (%d chars)" % len(test_source)
        return audit

    try:
        tree = ast.parse(test_source)
    except SyntaxError as exc:
        audit.reason = "test file does not parse: %s (line %s)" % (exc.msg, exc.lineno)
        return audit

    audit.assert_count = sum(1 for node in ast.walk(tree)
                             if isinstance(node, ast.Assert))
    if audit.assert_count == 0:
        audit.reason = "test file contains no assert statements"
        return audit

    names, wildcard = _solution_names(tree)
    if wildcard:
        audit.reason = ("test file uses `from solution import *`, so the "
                        "interface it expects cannot be checked")
        return audit
    names = {name for name in names if name.isidentifier()}
    if not names:
        audit.reason = ("test file never imports anything from `solution`, so "
                        "it cannot be testing the solution")
        return audit
    audit.names = sorted(names)

    # The real guard: run the suite against a solution that does nothing.
    stub = run_python_sandboxed(_stub_source(names), tests=test_source,
                                timeout=min(timeout, 15))
    audit.stub_exit = stub.exit_code
    audit.stub_stderr = stub.stderr
    if not stub.ran:
        audit.reason = "could not run the suite against a stub: %s" % stub.reason
        return audit
    if stub.exit_code == 0:
        audit.vacuous = True
        audit.reason = ("tests pass against a stub solution whose functions all "
                        "raise NotImplementedError, so they assert nothing real")
        return audit

    audit.ok = True

    # Second gate. Only reached by a suite that already survived the first, so
    # `uncovered` never competes with `vacuous` for the rejection reason.
    audit.clauses = spec_error_clauses(spec) if spec else []
    if audit.clauses:
        reached = suite_error_reach(tree)
        audit.reached = sorted(reached)
        if not reached & _CATCH_ALL:
            audit.uncovered = [(name, text) for name, text in audit.clauses
                               if name not in reached]
    return audit


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------

VERDICT_APPROVED = "APPROVED"
VERDICT_REVISE = "REVISE"
VERDICT_UNVERIFIED = "UNVERIFIED"


def verify_output(model_output, tests=None, timeout=EXEC_TIMEOUT_SECONDS):
    """Extract the Executor's code and run it, against ``tests`` when supplied.

    Returns ``(verdict, result)``. ``APPROVED`` means the code really ran and
    exited zero -- and when a suite was supplied, that the suite is what it
    exited zero on. Callers are responsible for having audited ``tests``
    beforehand; see :func:`audit_tests`.
    """
    source, language = extract_code_block(model_output)
    if source is None:
        result = ExecResult(reason="no code block found in the output",
                            tests=tests or "")
        # Distinguish "wrote nothing" from "wrote only a test file", which is
        # the Executor trying to hand us its own suite.
        fallback, _ = extract_code_block(model_output, allow_tests=True)
        if fallback is not None:
            result.reason = ("the output contained only test code, not a "
                             "solution")
            result.ignored_test_block = True
        return VERDICT_REVISE, result

    result = run_python_sandboxed(source, tests=tests, timeout=timeout)
    result.language = language or "python"
    # Anti-cheating: note when we threw away a suite the Executor emitted. The
    # suite we ran is always the stored one, never anything from this output.
    result.ignored_test_block = contains_test_block(model_output)

    if result.ok:
        return VERDICT_APPROVED, result
    if result.failure_kind == FAIL_SYNTAX:
        # Nothing ran, and it is still the candidate's defect: the parent proved
        # the source does not compile on the interpreter that would have run it.
        # The one place `not result.ran` does not mean "we could not tell".
        return VERDICT_REVISE, result
    if not result.ran or result.failure_kind == FAIL_PATH:
        # We could not execute at all; don't pretend that is a code defect.
        if result.failure_kind == FAIL_PATH:
            result.reason = ("harness could not make `solution` importable "
                             "(sys.path repair failed)")
        return VERDICT_UNVERIFIED, result
    return VERDICT_REVISE, result


def format_report(verdict, result):
    """Human-readable harness entry for the agent feed."""
    lines = ["VERDICT: %s" % verdict]
    if not result.ran:
        lines.append("Not executed: %s" % (result.reason or "unknown reason"))
        # Empty on every other not-ran path, because nothing ran to write it.
        # On FAIL_SYNTAX it holds the interpreter's own caret block, which is the
        # entire finding and belongs in the feed rather than only in the repair.
        if result.stderr.strip():
            lines += _stream_splice("compile error:", result.stderr)
        if result.ignored_test_block:
            lines.append("Note: a test file in the Executor's output was ignored.")
        if result.sandbox_layers:
            lines.append("Isolation: %s" % ", ".join(result.sandbox_layers))
        return "\n".join(lines)

    lines.append(
        "ran `%s`  exit=%s  time=%.2fs%s"
        % (result.entry, result.exit_code, result.duration,
           "  TIMED OUT" if result.timed_out else "")
    )
    if result.tested:
        lines.append("Measured against the stored acceptance suite (%d chars)."
                     % len(result.tests))
    else:
        lines.append("No acceptance suite: exit status only, so this cannot "
                     "confirm the output is *correct*.")
    if result.failure_kind == FAIL_ASSERTION and result.failed_assertion:
        lines.append("Failed assertion (%s line %s): `%s`"
                     % (TEST_NAME, result.failed_assertion_line,
                        result.failed_assertion))
        if result.failed_assertion_left:
            lines.append("Re-evaluated after the failure: left `%s`, "
                         "right `%s`" % (result.failed_assertion_left,
                                         result.failed_assertion_right))
    elif result.failure_kind == FAIL_IMPORT:
        lines.append("The solution could not be imported.")
    elif result.failure_kind == FAIL_TIMEOUT_IMPORT:
        lines.append("Timed out while importing %s -- no test ran."
                     % SCRIPT_NAME)
    elif result.failure_kind == FAIL_TIMEOUT_TESTS:
        lines.append("Timed out inside a function the suite called; the import "
                     "itself was fine.")
    elif result.failure_kind == FAIL_TIMEOUT_TEARDOWN:
        lines.append("The suite finished, then the process never exited "
                     "(something is still running at shutdown).")
    elif result.failure_kind == FAIL_OUTPUT:
        lines.append(
            "Runaway output: killed after emitting %d bytes to stdout and %d "
            "to stderr (cap %d per stream)."
            % (result.stdout_bytes, result.stderr_bytes, MAX_CAPTURE_BYTES))
    if result.ignored_test_block:
        lines.append("Note: a test file in the Executor's output was ignored; "
                     "the stored suite was used.")
    lines.append("Isolation: %s" % ", ".join(result.sandbox_layers))
    if result.stdout.strip():
        lines.append("\n**stdout**\n```\n%s\n```" % result.stdout.rstrip())
    else:
        lines.append("\n**stdout** _(empty)_")
    if result.stderr.strip():
        lines.append("\n**stderr**\n```\n%s\n```" % result.stderr.rstrip())
    return "\n".join(lines)


# The environment contract, in exactly one place. The Executor is given this up
# front in its system prompt *and* reminded of it on every repair, so the
# instructions it plans against and the sandbox it is judged in cannot drift
# apart. The interpreter version is read rather than asserted, because the child
# runs whichever interpreter is running this file.
_SANDBOX_RULES = "\n".join([
    "Constraints the execution sandbox enforces (your code is run, not read):",
    "- Python %d.%d, standard library only. No pip and no third-party imports "
    "-- no numpy, pandas, requests, or pytest." % sys.version_info[:2],
    "- No network. socket and everything built on it raises.",
    "- No subprocesses. subprocess, os.system, os.exec*, and os.fork raise.",
    "- No stdin. input() fails immediately, so never prompt for anything.",
    "- Writes are confined to the working directory. An absolute path outside "
    "it is refused; use a relative filename, or write nothing at all.",
    "- It must finish within %ss of wall clock, and output is capped at %d "
    "bytes per stream." % (EXEC_TIMEOUT_SECONDS, MAX_CAPTURE_BYTES),
])


def _fence(text):
    """A backtick run longer than any run inside ``text``.

    A bare ``` around candidate-controlled output is not a container: a program
    that prints three backticks closes the fence, and everything it printed after
    that reads as prompt structure rather than as program output. CommonMark's
    rule is that a fenced block ends only on a run at least as long as the one
    that opened it, so one backtick more than the longest run in the payload
    cannot be closed from inside it.
    """
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        if run > longest:
            longest = run
    return "`" * max(3, longest + 1)


def _stream_splice(heading, text, limit=MAX_FIXES_STREAM_CHARS):
    """One fenced stream quote for a repair prompt: clamped, and un-closable.

    Clamped head-and-tail through `_truncate` rather than from one end, because a
    traceback carries its useful information at both: the failing call at the top
    and the exception at the bottom. The elision marker names how much went, so a
    reader can tell a short stream from a trimmed one.
    """
    payload = (text or "").rstrip()
    payload, _cut = _truncate(payload, limit)
    fence = _fence(payload)
    return ["", heading, fence, payload, fence]


def format_fixes(verdict, result):
    """The message handed back to the Executor. Real output, not opinions."""
    if verdict == VERDICT_APPROVED:
        return ""

    if not result.ran:
        if result.failure_kind == FAIL_SYNTAX:
            parts = [
                "Your code is not valid Python %d.%d, so it was never run. This "
                "is the interpreter's own message, from compiling exactly the "
                "source you returned:" % sys.version_info[:2],
            ]
            parts += _stream_splice("compile error:", result.stderr)
            hint = _version_hint(source=result.source, stderr=result.stderr)
            if hint:
                parts += ["", hint]
            parts += ["",
                      "Return the complete corrected program in one ```python "
                      "block.",
                      "", _SANDBOX_RULES]
            return "\n".join(parts)
        if result.ignored_test_block:
            return (
                "Your output contained a test file but no solution, so there "
                "was nothing to execute. The acceptance suite is fixed and "
                "supplied by the harness -- do not write tests.\n"
                "- Return only the implementation, in a single ```python "
                "fenced block.\n"
                "- Do not import `solution`; your code *is* solution.py."
            )
        if result.reason == "no code block found in the output":
            return (
                "Your output contained no runnable code block, so the harness "
                "could not execute it.\n"
                "- Return the complete program in a single ```python fenced "
                "block.\n"
                "- It must run top-to-bottom on a bare interpreter with no "
                "arguments, no stdin, and no network."
            )
        return "The harness could not execute your code: %s" % result.reason

    header = ("Your code was executed in a sandbox and it failed. This is the "
              "real output, not a review:")
    parts = [header, "",
             "exit code: %s%s" % (result.exit_code,
                                  " (killed on timeout)" if result.timed_out else "")]

    # The two failure modes need genuinely different advice: one means the
    # module is broken, the other means the module works but is wrong.
    if result.failure_kind == FAIL_IMPORT:
        parts += [
            "",
            "Your solution could not even be imported by the acceptance suite. "
            "It failed before a single test ran, so fix the module itself "
            "first: a syntax error, a missing definition, or module-level code "
            "that raises.",
            "The suite does `from %s import ...`, so every function it needs "
            "must be defined at module level, and importing your file must "
            "have no side effects that can fail." % SOLUTION_MODULE,
        ]
    elif result.failure_kind == FAIL_ASSERTION:
        parts += [
            "",
            "Your solution imported and ran fine -- it is simply computing the "
            "wrong answer. This assertion failed:",
        ]
        if result.failed_assertion:
            parts.append("")
            parts.append("    %s  (%s line %s)"
                         % (result.failed_assertion, TEST_NAME,
                            result.failed_assertion_line))
        if result.failed_assertion_left:
            # Labelled as a re-evaluation, and the caveat is not decoration: the
            # values were obtained by evaluating the two operands again, after
            # the failure, in the frame that raised. For a pure function that is
            # the same answer; for one with a side effect or a random component
            # it need not be, and claiming otherwise would send a repair after a
            # number the failure never saw.
            parts += [
                "",
                "Re-evaluating the two sides in the frame that failed gave:",
                "",
                "    left    %s" % result.failed_assertion_left,
                "    right   %s" % result.failed_assertion_right,
                "",
                "That is a second evaluation, not a recording of the first, so "
                "treat it as exact only for a function without side effects or "
                "randomness.",
            ]
        parts += [
            "",
            "Do not change the test and do not special-case this input. Fix "
            "the logic so the general case is right.",
        ]
    elif result.failure_kind == FAIL_TIMEOUT_IMPORT:
        parts += [
            "",
            "It timed out *while your module was being imported* -- the "
            "acceptance suite never got as far as calling anything. Module-level "
            "code is looping or blocking forever.",
            "Move the work into functions and guard any demo or main loop with "
            "`if __name__ == \"__main__\":`. Importing %s.py must return "
            "immediately." % SOLUTION_MODULE,
        ]
    elif result.failure_kind == FAIL_TIMEOUT_TESTS:
        parts += [
            "",
            "It timed out *inside a function the acceptance suite called* -- "
            "your module imported cleanly, so the interface is fine and the "
            "logic is not. Something under test never returns: an unbounded "
            "`while`, a loop whose counter never advances, recursion with no "
            "base case, or a wait on input that never arrives.",
            "Do not just make it faster; find the case that never terminates. "
            "The program timed out rather than finishing.",
        ]
    elif result.failure_kind == FAIL_TIMEOUT_TEARDOWN:
        parts += [
            "",
            "The acceptance suite finished, and then the process never exited "
            "-- so it timed out on the way out rather than on the work. Almost "
            "always a thread that is still running: a non-daemon "
            "`threading.Thread` nobody joined, a queue worker with no stop "
            "signal, or an `atexit`/`__del__` hook that blocks.",
            "Join every thread you start before the module finishes, or create "
            "them with `daemon=True`. Leave nothing running once the work is "
            "done.",
        ]
    elif result.failure_kind == FAIL_TIMEOUT:
        parts += ["", "It timed out -- it never finished. Remove any infinite "
                      "loop, interactive prompt, or blocking wait, or reduce "
                      "the workload."]
    elif result.failure_kind == FAIL_OUTPUT:
        parts += [
            "",
            "Your program produced runaway output -- more than %d bytes on one "
            "stream -- so the harness killed it. That is a printing loop, not a "
            "solution: a `print` inside an unbounded `while`, or debug output "
            "inside a hot loop." % MAX_CAPTURE_BYTES,
            "Print only what the task asks for. The acceptance suite grades "
            "return values, not printed text, so a correct solution normally "
            "prints nothing at all.",
        ]

    if result.stderr.strip():
        parts += _stream_splice("stderr / traceback:", result.stderr)
        # stderr only, never the source: a `match` statement never reaches this
        # branch, because it never compiled. An evaluated PEP 604 union does, and
        # its TypeError says nothing about versions on its own.
        hint = _version_hint(stderr=result.stderr)
        if hint:
            parts += ["", hint]
    # Not spliced on FAIL_OUTPUT. The branch above exists to tell a printing loop
    # that it printed too much; quoting a sample of the printing back is the
    # branch that punishes the behaviour feeding it, and a repair can do nothing
    # with the sample that the sentence does not already say.
    if result.stdout.strip() and result.failure_kind != FAIL_OUTPUT:
        parts += _stream_splice("stdout before failure:", result.stdout)

    parts += ["",
              "Return the complete corrected program in one ```python block.",
              "", _SANDBOX_RULES]
    if result.tested:
        parts.append(
            "You are graded by running a fixed acceptance suite against your "
            "code. Do not write or modify tests -- any test file you emit is "
            "discarded."
        )
    return "\n".join(parts)

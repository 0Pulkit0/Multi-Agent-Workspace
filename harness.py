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
from dataclasses import dataclass, field
from typing import List, Optional

# Wall-clock ceiling for a single execution.
EXEC_TIMEOUT_SECONDS = 15

# The child's RLIMIT_CPU is set to the wall-clock timeout plus this grace, so
# the wall-clock kill (which we can explain) normally wins over SIGXCPU (which
# arrives as a bare negative exit code). The CPU limit is the backstop for a
# child that outlives its own process group.
CPU_GRACE_SECONDS = 5

# Per-stream capture ceiling. Runaway printers get cut off here.
MAX_STREAM_CHARS = 4000

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

# The child records how far it got here, so that a SIGKILLed process -- which
# prints no traceback at all -- can still be explained.
PHASE_NAME = "_harness_phase"

PHASE_STARTUP = "startup"
PHASE_IMPORT = "import-solution"
PHASE_TESTS = "tests"
PHASE_SOLUTION = "solution"
PHASE_TEARDOWN = "teardown"


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
import traceback

_SELF = os.path.abspath(__file__)
_TARGET = sys.argv[1]
_CPU_SECONDS = int(sys.argv[2])
_MAX_WRITE_BYTES = int(sys.argv[3])
_MAX_MEMORY_BYTES = int(sys.argv[4])
_ENTRY_KIND = sys.argv[5]
_PHASE_PATH = sys.argv[6]
_WORKDIR = os.path.dirname(os.path.abspath(_TARGET))
# Resolved separately: on macOS the temp dir is reached through a symlink, so
# realpath() of a path inside it does not start with _WORKDIR. Both spellings
# count as inside. _WORKDIR itself is left alone because sys.path and the
# traceback rewriting in the parent are keyed to it.
_WORKDIR_REAL = os.path.realpath(_WORKDIR)
_REAL_OPEN = open

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
    tracebacks read source files, and tests legitimately read fixtures.
    """
    import builtins
    import io

    def _guard_open(real):
        def _open(file, mode="r", *args, **kwargs):
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

    _write_flags = 0
    for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
        _write_flags |= getattr(os, flag, 0)
    _os_open = os.open

    def _guarded_os_open(path, flags, *args, **kwargs):
        if flags & _write_flags and not _fs_allowed(path):
            raise _fs_deny(path)
        return _os_open(path, flags, *args, **kwargs)

    os.open = _guarded_os_open

    def _guard(real, checked):
        def _wrapped(*args, **kwargs):
            for index in checked:
                if index < len(args) and not _fs_allowed(args[index]):
                    raise _fs_deny(args[index])
            return real(*args, **kwargs)
        return _wrapped

    # The index of each path argument that must land inside the workdir. For
    # copies only the destination is checked, because the source is only read;
    # for rename/move both, because the source is destroyed.
    for module, name, checked in (
        (os, "remove", (0,)), (os, "unlink", (0,)), (os, "rmdir", (0,)),
        (os, "removedirs", (0,)), (os, "mkdir", (0,)), (os, "makedirs", (0,)),
        (os, "truncate", (0,)), (os, "chmod", (0,)), (os, "chown", (0,)),
        (os, "utime", (0,)), (os, "mknod", (0,)), (os, "mkfifo", (0,)),
        (os, "chdir", (0,)),
        (os, "rename", (0, 1)), (os, "renames", (0, 1)), (os, "replace", (0, 1)),
        (os, "link", (1,)), (os, "symlink", (1,)),
        (shutil, "rmtree", (0,)), (shutil, "move", (0, 1)),
        (shutil, "copy", (1,)), (shutil, "copy2", (1,)),
        (shutil, "copyfile", (1,)), (shutil, "copytree", (1,)),
        (shutil, "copymode", (1,)), (shutil, "copystat", (1,)),
        (shutil, "make_archive", (0,)), (shutil, "unpack_archive", (1,)),
    ):
        real = getattr(module, name, None)
        if real is not None:
            setattr(module, name, _guard(real, checked))

    # Imported *after* the patches above, and only now: pathlib snapshots
    # os.unlink and friends into its accessor at import time, so importing it
    # here is what makes Path.unlink() and Path.write_text() go through the
    # guards on 3.9. If something imported pathlib earlier, its accessor holds
    # the originals -- one more reason this is containment, not a boundary.
    try:
        import pathlib  # noqa: F401
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
    sys.exit(1)
'''


# --------------------------------------------------------------------------
# OS-level jail probing
# --------------------------------------------------------------------------

_MACOS_PROFILES = (
    (
        "sandbox-exec:no-net+no-write",
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny file-write*)\n"
        '(allow file-write* (subpath "{workdir}")'
        ' (literal "/dev/null") (literal "/dev/stdout")'
        ' (literal "/dev/stderr") (literal "/dev/dtracehelper"))\n',
    ),
    (
        "sandbox-exec:no-net",
        "(version 1)\n(allow default)\n(deny network*)\n",
    ),
)

_probe_cache = {}


def _candidate_wrappers(workdir):
    """Yield ``(label, argv_prefix)`` OS jails to try, strongest first."""
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        for label, profile in _MACOS_PROFILES:
            yield label, ["sandbox-exec", "-p", profile.format(workdir=workdir)]
    elif system == "Linux" and shutil.which("unshare"):
        yield "unshare:net+user", ["unshare", "--map-root-user", "--net"]
        yield "unshare:net", ["unshare", "--net"]


def _wrapper_works(label, prefix):
    """Probe a jail once with a trivial script; cache the verdict."""
    if label in _probe_cache:
        return _probe_cache[label]
    ok = False
    try:
        proc = subprocess.run(
            prefix + [sys.executable, "-I", "-B", "-c", "print('probe')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        ok = proc.returncode == 0 and b"probe" in proc.stdout
    except Exception:
        ok = False
    _probe_cache[label] = ok
    return ok


def _select_wrapper(workdir):
    for label, prefix in _candidate_wrappers(workdir):
        if _wrapper_works(label, prefix):
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
    ignored_test_block: bool = False
    sandbox_layers: List[str] = field(default_factory=list)
    truncated: bool = False
    output_capped: bool = False   # breached MAX_CAPTURE_BYTES and was killed
    stdout_bytes: int = 0         # bytes the child *emitted*, not bytes kept
    stderr_bytes: int = 0
    phase: str = ""               # how far the child got; see PHASE_* above

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


def run_python_sandboxed(source, tests=None, timeout=EXEC_TIMEOUT_SECONDS):
    """Run ``source`` in an isolated subprocess and capture what it did.

    ``source`` is always written as ``solution.py``. When ``tests`` is given it
    is written as ``test_solution.py`` alongside it and becomes the entry
    point, with ``solution`` importable. With no tests the behaviour is
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

    workdir = tempfile.mkdtemp(prefix="harness-")
    try:
        script_path = os.path.join(workdir, SCRIPT_NAME)
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(source)

        test_path = None
        if tests:
            test_path = os.path.join(workdir, TEST_NAME)
            with open(test_path, "w", encoding="utf-8") as handle:
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
        layers.append(label if label else "os-level:unavailable")
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
        shutil.rmtree(workdir, ignore_errors=True)


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


def _classify(result):
    """Work out *how* it failed, since each mode needs different guidance."""
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

    def summary(self):
        if self.ok:
            return "%d assert(s) over %s -- fails against a stub, so it tests something" % (
                self.assert_count, ", ".join(self.names) or "the solution module")
        return self.reason


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


def audit_tests(test_source, timeout=EXEC_TIMEOUT_SECONDS):
    """Decide whether a suite is non-vacuous enough to gate APPROVED on."""
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


def format_fixes(verdict, result):
    """The message handed back to the Executor. Real output, not opinions."""
    if verdict == VERDICT_APPROVED:
        return ""

    if not result.ran:
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
        parts += ["", "stderr / traceback:", "```", result.stderr.rstrip(), "```"]
    if result.stdout.strip():
        parts += ["", "stdout before failure:", "```", result.stdout.rstrip(), "```"]

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

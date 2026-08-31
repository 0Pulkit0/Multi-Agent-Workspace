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

# PEP 563 makes every annotation a string, so a PEP 604 union in a signature
# cannot be evaluated and cannot raise on 3.9. One line, and it does not depend
# on a model obeying an instruction. It does NOT cover `match` statements or a
# runtime `isinstance(x, int | str)` -- those are the prompt's job.
#
# Written ahead of SCRIPT_NAME only. `ExecResult.source` keeps the model's
# original bytes: the record says what the model wrote, and the executed text is
# recoverable as `_SOURCE_PROLOGUE + source` because the prologue is a constant.
# Nothing is prepended to TEST_NAME -- see the note at the write site.
_SOURCE_PROLOGUE = "from __future__ import annotations\n"

# The child records how far it got here, so that a SIGKILLed process -- which
# prints no traceback at all -- can still be explained.
PHASE_NAME = "_harness_phase"

# And which pathlib mechanism its path guard ended up using. The parent reports the
# layer list but cannot know this: it depends on what the child's pathlib actually
# holds, and reconstructing it from `sys.version_info` in the parent would be
# reading the mechanism out of source instead of recording what ran.
PATHS_NAME = "_harness_paths"

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
# Where the child leaves the one fact about the path guard that only the child can
# know: which pathlib mechanism it used. Spelled here and in the parent's
# `PATHS_NAME`, and a check asserts the two agree, because the runner is written
# verbatim and cannot interpolate the parent's constant.
_PATHS_NAME = "_harness_paths"

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

    def _guarded_os_open(*args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            raise _fs_deny_fd("dir_fd")
        path = args[0] if args else kwargs.get("path")
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
            # The prologue goes ahead of the model's text, never over it. A
            # future statement may be preceded only by comments, blank lines,
            # the module docstring and other future statements, so line 1 is
            # the only always-legal position -- and it stays legal when the
            # model opens with a docstring or wrote the same import itself.
            # Cost: solution.py line numbers in raw stderr are one higher than
            # the model's own count, and that stderr becomes repair context.
            # `failed_assertion_line` is unaffected; it is only set for frames
            # in TEST_NAME, which gets no prologue.
            handle.write(_SOURCE_PROLOGUE)
            handle.write(source)

        test_path = None
        if tests:
            test_path = os.path.join(workdir, TEST_NAME)
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

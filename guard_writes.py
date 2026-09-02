#!/usr/bin/env python3
"""Run a suite with every write into the two live stores blocked and recorded.

    python3 guard_writes.py test_pipeline.py    # guard a suite
    python3 guard_writes.py --self-test         # prove the interception, run nothing

An external wrapper rather than a hook inside either suite, deliberately. A guard
that can be silently switched off while the suite still passes is this project's
own defect family wearing a guard's clothes: the claim "0 writes into the live
stores" would then be *false* rather than absent. Invoked from outside, the claim
exists only when someone runs this file, and is absent -- not wrong -- when
nobody does.

The self-test is not optional, for the same reason. A guard that failed to patch
reports "0 attempted writes", and so does a guard that worked, so the output
cannot tell them apart. `--self-test` drives one real write attempt through each
patched entry point at a path inside a denied root and asserts that every one was
refused *and* recorded. It runs in this process, under the same live patches the
suite then runs under, before the suite is imported. **A suite run whose
self-test did not pass prints no write verdict and exits non-zero.**

Only `eval/calibration/` and `eval/results/` refuse. The suites legitimately
write temp directories, and denying the whole tree would turn this into an
allow-list that drifts out of date -- so writes outside the two roots are
counted and their distinct prefixes printed instead of silently permitted. An
unexpected write target is then visible without one line per write in a
960-check run.

One class of write cannot be adjudicated at all: a *relative* path resolved
against an open directory fd, as in `shutil.rmtree`'s internal
`os.unlink(entry.name, dir_fd=topfd)`. Naming it would need `F_GETPATH` on macOS
or `/proc/self/fd` on Linux, which is out of scope for a stdlib
accident-containment guard, and guessing it -- joining the name to the process
cwd, as an earlier version did -- is wrong in both directions: it reported
temp-dir teardown as a repo-root write, and it would have let a write aimed at a
denied root through by resolving it somewhere harmless. So the guard abstains and
says so: those writes are counted on their own line beside the zero, neither
refused nor cleared. An absolute path is still refused even when a `dir_fd` is
present, because POSIX ignores the fd in that case. The dangerous shapes are
unaffected -- `shutil.rmtree("eval/results")` and every `open`/`os.open` with a
spelled-out target are named, so they still refuse.

The patches are `_Patch` instances rather than functions because a function is a
descriptor. `pathlib._NormalAccessor` on 3.9 captures `open = os.open`,
`unlink = os.unlink` and six more into a class body, at an import that happens
*after* this file installs, and a captured function binds -- which crashed
`Path.open()` and made `Path.unlink()` record the accessor object as its write
target. `_pathlib_probe` reports which import order the run was in and whether
that route ended up guarded, because a guard whose coverage depends on an import
order nobody tracks is another zero reached by not looking.
"""
import argparse
import builtins
import collections
import os
import re
import shutil
import sys
import tempfile

# Relative to this file, never to a home directory: the guard has to be right in
# a checkout that lives somewhere else.
REPO = os.path.dirname(os.path.abspath(__file__))

# The two stores a concurrent sweep owns. `eval/results/` is read-only by
# standing rule and `eval/calibration/` belongs to whichever sweep is running,
# so both are write-denied and neither is read-denied.
DENIED = ("eval/calibration", "eval/results")

_DENIED_ABS = tuple((name, os.path.realpath(os.path.join(REPO, name)))
                    for name in DENIED)
_TMP = os.path.realpath(tempfile.gettempdir())

# `mkdtemp(prefix="import-order-")` produces `import-order-8vq2b1t7`. The random
# tail is exactly what would put one bucket per call in the report, so it is
# dropped and the prefix -- the informative half -- is kept.
_RANDOM_TAIL = re.compile(r"[0-9a-z_]{6,}$", re.IGNORECASE)


def _resolve(path):
    """`path` as an absolute real path, or None if it is not a path at all.

    Symlinks are resolved because the question is which store the bytes land in,
    not how the caller spelled it. A file descriptor passed *as the path* and any
    non-path object resolve to None and are neither denied nor counted -- the
    guard reports what it can name.

    What this cannot do alone is resolve a *relative* path that the caller means
    against an open directory, because `abspath` joins it to the process cwd
    instead. That is not hypothetical: `shutil.rmtree` walks with
    `os.unlink(entry.name, dir_fd=topfd)`, so arg 0 is a bare filename and the
    path tested here is unrelated to the directory being emptied. It mislabels a
    temp-dir teardown as a repo-root write in one direction and, in the other,
    would let an fd-relative write into a denied root resolve outside it and be
    permitted. `_fd_relative` is how the caller declares that case so the guard
    can abstain rather than guess; see `Ledger.check`.
    """
    if isinstance(path, int) or isinstance(path, bool):
        return None
    try:
        value = os.fspath(path)
    except TypeError:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    try:
        return os.path.realpath(os.path.abspath(value))
    except (TypeError, ValueError, OSError):
        return None


def _fd_relative(path, kwargs, fd_keyword):
    """True when `path` is resolved against a directory fd and cannot be named.

    Two conditions, both required. The call has to supply a non-None directory
    fd, and the path has to be relative -- POSIX says an absolute path ignores
    `dir_fd` entirely, so an absolute one is still nameable and must still be
    denied. Only `kwargs` is inspected because every `dir_fd`-style parameter in
    `os` is keyword-only, so there is no positional index to guess at.
    """
    if fd_keyword is None or kwargs.get(fd_keyword) is None:
        return False
    if isinstance(path, int) or isinstance(path, bool):
        return False               # the path is itself an fd; `_resolve` has it
    try:
        value = os.fspath(path)
    except TypeError:
        return False
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return isinstance(value, str) and not os.path.isabs(value)


def denied_root(path):
    """The name of the denied root `path` falls inside, or None."""
    real = _resolve(path)
    if real is None:
        return None
    for name, root in _DENIED_ABS:
        if real == root or real.startswith(root + os.sep):
            return name
    return None


def _bucket(path):
    """A permitted write's reporting prefix: one bucket per target, not per call."""
    real = _resolve(path)
    if real is None:
        return "<unnameable target>"
    if real == _TMP or real.startswith(_TMP + os.sep):
        rest = real[len(_TMP):].strip(os.sep).split(os.sep)
        return "<tmp>/%s*" % _RANDOM_TAIL.sub("", rest[0] if rest else "")
    if real == REPO or real.startswith(REPO + os.sep):
        rest = os.path.dirname(real[len(REPO):].strip(os.sep))
        parts = [p for p in rest.split(os.sep)[:2] if p]
        return "<repo>/" + ("/".join(parts) + "/" if parts else "")
    parts = [p for p in real.split(os.sep)[:3] if p]
    return os.sep + os.sep.join(parts) + os.sep


class Refused(Exception):
    """Raised in place of a write into a denied root.

    Not `OSError`, so an `except OSError` around a write cannot mistake a refusal
    for a full disk. A bare `except Exception` would still swallow it, which is
    why `Ledger.refuse` appends the record *before* raising: a refusal that some
    handler absorbs still reaches the report, and the report is the evidence.
    """


class Ledger(object):
    """What the guard saw: refusals listed, permitted writes bucketed, and the
    writes whose target no spelling could name counted apart from both."""

    def __init__(self):
        self.refused = []
        self.allowed = collections.Counter()
        self.abstained = 0

    def refuse(self, entry_point, path):
        root = denied_root(path)
        self.refused.append((entry_point, root, _resolve(path)))
        raise Refused("%s would write into %s/ -- refused: %s"
                      % (entry_point, root, _resolve(path)))

    def allow(self, path):
        self.allowed[_bucket(path)] += 1

    def check(self, entry_point, path, fd_based=False):
        """Refuse `path` if it is inside a denied root, otherwise count it.

        `fd_based` is the caller declaring that `path` is resolved against a
        directory fd, so no spelling of it names a store. The guard then abstains
        outright: it does not refuse, because the target may be outside every
        denied root, and it does not bucket the write as permitted-and-elsewhere,
        because the target may be inside one. Abstentions are counted and printed
        beside the zero -- a zero reached by not looking is a different claim from
        a zero reached by looking, and the report has to be able to say which.
        """
        if fd_based:
            self.abstained += 1
            return
        if denied_root(path) is not None:
            self.refuse(entry_point, path)
        self.allow(path)

    def take(self):
        """Read and clear, so the self-test's own records are not the suite's."""
        taken = (self.refused, self.allowed, self.abstained)
        self.refused, self.allowed, self.abstained = [], collections.Counter(), 0
        return taken


# Write *targets* by argument position, with the keyword spelling that reaches the
# same argument and the `dir_fd`-style keyword, if any, that argument is resolved
# against. Sources are deliberately absent: `eval/results/` is read-only, not
# read-denied, so `shutil.copyfile(eval/results/x, /tmp/y)` must succeed -- only
# index 1 of a copy is a write. `os.replace`/`os.rename` and `shutil.move` name
# both, because they unlink the source as well as creating the destination.
#
# The third slot is None wherever the function has no such parameter, so a
# `dir_fd=` that cannot exist is never inferred: `os.makedirs`, `os.truncate`,
# `os.renames` and every `shutil` entry point take no directory fd in 3.9.
# `os.symlink`'s single `dir_fd` resolves the link it creates, which is index 1,
# the argument guarded here.
_TARGETS = (
    ("os.mkdir", os, "mkdir", ((0, "path", "dir_fd"),)),
    ("os.makedirs", os, "makedirs", ((0, "name", None),)),
    ("os.remove", os, "remove", ((0, "path", "dir_fd"),)),
    ("os.unlink", os, "unlink", ((0, "path", "dir_fd"),)),
    ("os.rmdir", os, "rmdir", ((0, "path", "dir_fd"),)),
    ("os.truncate", os, "truncate", ((0, "path", None),)),
    ("os.replace", os, "replace", ((0, "src", "src_dir_fd"),
                                   (1, "dst", "dst_dir_fd"))),
    ("os.rename", os, "rename", ((0, "src", "src_dir_fd"),
                                 (1, "dst", "dst_dir_fd"))),
    ("os.renames", os, "renames", ((0, "old", None), (1, "new", None))),
    ("os.symlink", os, "symlink", ((1, "dst", "dir_fd"),)),
    ("os.link", os, "link", ((1, "dst", "dst_dir_fd"),)),
    ("os.chmod", os, "chmod", ((0, "path", "dir_fd"),)),
    ("shutil.rmtree", shutil, "rmtree", ((0, "path", None),)),
    ("shutil.move", shutil, "move", ((0, "src", None), (1, "dst", None))),
    ("shutil.copyfile", shutil, "copyfile", ((1, "dst", None),)),
    ("shutil.copy", shutil, "copy", ((1, "dst", None),)),
    ("shutil.copy2", shutil, "copy2", ((1, "dst", None),)),
    ("shutil.copytree", shutil, "copytree", ((1, "dst", None),)),
    ("shutil.copymode", shutil, "copymode", ((1, "dst", None),)),
    ("shutil.copystat", shutil, "copystat", ((1, "dst", None),)),
    ("shutil.make_archive", shutil, "make_archive", ((0, "base_name", None),)),
)

_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)


class _Patch(object):
    """A callable that is deliberately *not* a descriptor.

    `install` replaces module-level C builtins with Python callables, and any
    class body that later evaluates `unlink = os.unlink` captures whatever is
    bound to that name at that moment. A plain Python function captured into a
    class body binds: the instance arrives as argument 0 and every real argument
    shifts by one. A C builtin does not bind, which is the only reason CPython
    can write those assignments at all -- and `pathlib._NormalAccessor` on 3.9 is
    exactly that shape (`open = os.open`, `unlink = os.unlink`, and six more).
    It is imported *after* this file installs, because nothing this file imports
    pulls `pathlib` in.

    The two harms are unequal, and the quiet one is the one that matters to a
    claim rather than to a run. Bound, `Path.open()` reached
    `guarded_os_open(<accessor>, PosixPath, flags, mode)` and died computing
    `PosixPath & int` -- loud. `Path.unlink()` recorded the accessor object as
    the write target *first*: `_resolve` can make nothing of it, `denied_root`
    returns None, and `Ledger.check` therefore takes the `allow` branch and files
    it under permitted-and-elsewhere as `<unnameable target>`. A write the guard
    could not name, counted as cleared, and not on the abstained line.

    An instance with `__call__` has no `__get__`, so it is captured as-is and
    every argument arrives where the wrapper expects it. `__wrapped__` is a real
    attribute on the slots, so `_unwrap` is unaffected.
    """
    __slots__ = ("_call", "__name__", "__wrapped__")

    def __init__(self, call, wrapped):
        self._call = call
        self.__name__ = getattr(call, "__name__", "guarded")
        self.__wrapped__ = wrapped

    def __call__(self, *args, **kwargs):
        return self._call(*args, **kwargs)


def _wrap_positional(ledger, label, original, targets):
    def guarded(*args, **kwargs):
        for index, keyword, fd_keyword in targets:
            if len(args) > index:
                target = args[index]
            elif keyword in kwargs:
                target = kwargs[keyword]
            else:
                continue
            ledger.check(label, target,
                         _fd_relative(target, kwargs, fd_keyword))
        return original(*args, **kwargs)
    guarded.__name__ = "guarded_" + label.replace(".", "_")
    return _Patch(guarded, original)


def _unwrap(function):
    """The unpatched function behind `function`, or `function` itself.

    `os.supports_dir_fd` is a set of the *original* function objects, so a
    membership test against a patched `os.unlink` is always false and would make
    every abstention probe skip itself -- silently, and only while the guard is
    working.
    """
    return getattr(function, "__wrapped__", function)


def _repoint_pathlib(saved):
    """Point `pathlib`'s already-captured `os.*` slots at the patches.

    Returns the undo records, in `saved`'s own `(holder, name, original)` shape.

    `_Patch` covers the case where `pathlib` is imported *after* `install()`: the
    class body captures the patch and it does not bind. The opposite order is the
    silent one. If anything pulled `pathlib` in first, `_NormalAccessor`'s slots
    hold the real builtins, every `Path` write goes straight past the guard, and
    the report says zero for that route by never having looked at it. That is the
    one zero this file is not allowed to print (see `Ledger.check`), so the slots
    are re-pointed rather than the coverage depending on an import order nobody
    is tracking.

    Matched by identity and only against functions this run actually patched, so
    a slot `pathlib` wrote itself is never touched -- on POSIX its own `symlink`
    is a `staticmethod` that calls `os.symlink` at call time and is already
    guarded correctly, and rebinding it would shift it the other way. Identity is
    also what catches `link_to`, whose slot name is nothing like the `os.link` it
    holds. `_NormalAccessor` is gone from 3.11, where `pathlib` reaches `os.*` by
    ordinary module lookup and needs no help; `getattr` covers that.
    """
    accessor = getattr(sys.modules.get("pathlib"), "_NormalAccessor", None)
    if accessor is None:
        return []
    undo = []
    for slot, value in sorted(vars(accessor).items()):
        for holder, name, original in saved:
            if holder is os and value is original:
                setattr(accessor, slot, getattr(os, name))
                undo.append((accessor, slot, original))
                break
    return undo


def install(ledger):
    """Patch every write entry point in this process. Returns `restore()`.

    Counts are of guarded *calls*, not of distinct files: `shutil.copy` reaches
    `open` and `os.chmod` on its own, and `os.makedirs` reaches `os.mkdir` once
    per level. Inflating a permitted count is harmless; missing an entry point
    would not be, so nesting is left alone.
    """
    saved = [(builtins, "open", builtins.open), (os, "open", os.open)]

    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        # No `dir_fd` to declare: `builtins.open` has no such parameter. A caller
        # can still reach one through `opener=`, and then the inner `os.open` is
        # the guarded call that sees the fd and abstains -- so that route is
        # recorded at the layer that can actually name the condition.
        if isinstance(mode, str) and any(ch in mode for ch in "wxa+"):
            ledger.check("open(mode=%r)" % mode, file)
        return real_open(file, mode, *args, **kwargs)

    real_os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if not isinstance(flags, int):
            # Not a repair: `real_os_open` would reject the shifted arguments a
            # moment later anyway, and a containment guard must not invent one.
            # What this buys is that the next instance of the descriptor bug
            # names itself here instead of surfacing three frames away as
            # "unsupported operand type(s) for &: 'PosixPath' and 'int'".
            raise TypeError(
                "guard_writes: os.open received %r as `flags`, so this patch was "
                "called as a bound method and every argument is shifted by one. "
                "See _Patch." % (type(flags).__name__,))
        if flags & _WRITE_FLAGS:
            ledger.check("os.open", path,
                         _fd_relative(path, kwargs, "dir_fd"))
        return real_os_open(path, flags, *args, **kwargs)

    builtins.open = _Patch(guarded_open, real_open)
    os.open = _Patch(guarded_os_open, real_os_open)

    for label, module, name, targets in _TARGETS:
        original = getattr(module, name, None)
        if original is None:          # not on this platform; nothing to guard
            continue
        saved.append((module, name, original))
        setattr(module, name, _wrap_positional(ledger, label, original, targets))

    # After every `os.*` patch is in place, and given the pre-patch list so the
    # re-pointing cannot match one of its own entries.
    saved.extend(_repoint_pathlib(list(saved)))

    def restore():
        for holder, name, original in saved:
            setattr(holder, name, original)

    return restore


_PROBE = ".guard-self-test-nothing-should-create-this"


def _discard(handle, target):
    """Undo a probe that was *not* blocked, so a broken guard leaves no debris."""
    try:
        if isinstance(handle, int):
            os.close(handle)
        elif hasattr(handle, "close"):
            handle.close()
    except OSError:
        pass
    for undo in (os.remove, os.rmdir):
        try:
            undo(target)
            return
        except OSError:
            continue


def _abstention_probe(ledger):
    """Prove the guard abstains on an fd-relative path, and only on that.

    Returns `(ok, lines)`. Two claims, because the fix turns on the difference
    between them. A *relative* name against a directory fd must be counted as
    unnameable and let through -- the guard cannot say which store it lands in,
    and inventing an answer is how `shutil.rmtree`'s internal
    `os.unlink(name, dir_fd=topfd)` came to be reported as a repo-root write. An
    *absolute* name must still be refused even with the same fd present, because
    POSIX says `dir_fd` is ignored outright in that case, so the path still names
    a store and abstaining there would open the hole the other way.

    Nothing is created and nothing is deleted: both spellings target a name that
    does not exist, so the permitted branch reaches `unlinkat` and fails with
    `FileNotFoundError` -- which is also the evidence that the guard passed the
    call through rather than silently absorbing it.
    """
    root = os.path.join(REPO, DENIED[0])
    if not os.path.isdir(root) or _unwrap(os.unlink) not in os.supports_dir_fd:
        return True, ["  skip  %-19s no unlinkat here, or %s/ absent: nothing to "
                      "abstain from" % ("dir_fd abstains", DENIED[0])]

    fd = os.open(root, os.O_RDONLY)      # a read: no write flag, so not checked
    lines = []
    ok = True
    try:
        before_abstained = ledger.abstained
        before_refused = len(ledger.refused)
        reached = refused = False
        try:
            os.unlink(_PROBE, dir_fd=fd)
        except Refused:
            refused = True
        except FileNotFoundError:
            reached = True
        except OSError:
            pass
        abstained = ledger.abstained - before_abstained
        if reached and abstained == 1 and not refused:
            lines.append("  ok    %-19s relative name + dir_fd counted unnameable, "
                         "not guessed at" % "dir_fd abstains")
        else:
            ok = False
            lines.append("  FAIL  %-19s reached_real_call=%s abstained=%d "
                         "refused=%s" % ("dir_fd abstains", reached, abstained,
                                         refused))

        absolute = os.path.join(root, _PROBE)
        before_abstained = ledger.abstained
        blocked = False
        try:
            os.unlink(absolute, dir_fd=fd)
        except Refused:
            blocked = True
        except OSError:
            pass
        recorded = len(ledger.refused) - before_refused
        if blocked and recorded == 1 and ledger.abstained == before_abstained:
            lines.append("  ok    %-19s absolute name ignores dir_fd, so it is "
                         "still refused" % "dir_fd is not cover")
        else:
            ok = False
            lines.append("  FAIL  %-19s refused=%s recorded=%d abstained=%d"
                         % ("dir_fd is not cover", blocked, recorded,
                            ledger.abstained - before_abstained))

        if os.path.exists(absolute):
            ok = False
            lines.append("  FAIL  %-19s left %s on disk"
                         % ("dir_fd probe", os.path.relpath(absolute, REPO)))
    finally:
        os.close(fd)
    return ok, lines


def _pathlib_probe():
    """Report whether `pathlib`'s write route is guarded, and say which case.

    Returns `(ok, lines)`. Called twice: beside the self-test, and again just
    before `restore()`, because the two answer different questions. At self-test
    time `pathlib` is usually absent, so the first call can only say that nothing
    has captured `os.*` yet. The closing call is the one that measures the run
    that happened -- by then the suite has imported `pathlib`, and whether its
    slots hold the guard's patch is a fact rather than a promise.

    That gap is the whole reason this exists. `self_test` drives the patched
    callables directly, so it measures the wrapper's body; it cannot measure the
    wrapper's *installation* as a third party sees it, because no third party is
    involved. The suite is the third party.

    Deliberately mechanism-neutral: a slot holding a `_Patch` is guarded whether
    it captured one at import or was re-pointed to one by `_repoint_pathlib`, and
    a slot still holding a function this run patched over is a hole either way.
    """
    module = sys.modules.get("pathlib")
    if module is None:
        return True, ["  ok    %-19s pathlib is not imported, so nothing here has "
                      "captured os.*" % "pathlib capture"]
    accessor = getattr(module, "_NormalAccessor", None)
    if accessor is None:
        return True, ["  ok    %-19s pathlib has no _NormalAccessor (3.11+): it "
                      "reaches os.* by module lookup" % "pathlib capture"]

    patched = {}
    for name in ["open"] + [entry[2] for entry in _TARGETS if entry[1] is os]:
        current = getattr(os, name, None)
        original = _unwrap(current)
        if current is not original:
            patched[name] = original

    slots = sorted(vars(accessor).items())
    escaped = ["%s (holds the real os.%s)" % (slot, name)
               for slot, value in slots
               for name, original in patched.items() if value is original]
    held = sum(1 for _, value in slots if isinstance(value, _Patch))
    if escaped:
        return False, ["  FAIL  %-19s %d captured slot(s) bypass the guard: %s"
                       % ("pathlib capture", len(escaped), ", ".join(escaped))]
    return True, ["  ok    %-19s %d captured slot(s) hold the guard's patch, 0 "
                  "hold a real builtin it patched over"
                  % ("pathlib capture", held)]


def self_test(ledger):
    """Drive one real write through each entry point into a denied root.

    Returns `(passed, report_lines)`. Every probe must do three things: raise
    `Refused`, add exactly one line to the ledger, and leave nothing on disk.
    Blocked-but-unrecorded is a failure too -- a guard whose report is empty
    because it never wrote the report down is the case this exists to catch.

    `_abstention_probe` runs last and is counted separately: it is not an
    interception, so folding it into the "N of M intercepted" tally would inflate
    that number with a probe that proves the guard declined to answer.
    `_pathlib_probe` is excluded for the same reason and a second one -- at this
    point it reports an import-order fact, not an interception.
    """
    scratch = tempfile.mkdtemp(prefix="guard-self-test-")
    source = os.path.join(scratch, "source")
    with open(source, "w") as handle:      # already guarded: counted, not denied
        handle.write("probe\n")

    calibration = os.path.join(REPO, DENIED[0], _PROBE)
    results = os.path.join(REPO, DENIED[1], _PROBE)
    probes = (
        ("open(mode='w')", results, lambda p: open(p, "w")),
        ("os.open", calibration, lambda p: os.open(p, os.O_WRONLY | os.O_CREAT)),
        ("os.mkdir", results, lambda p: os.mkdir(p)),
        ("shutil.copyfile dst", calibration, lambda p: shutil.copyfile(source, p)),
        ("os.replace dst", results, lambda p: os.replace(source, p)),
    )

    passed = True
    lines = []
    for label, target, attempt in probes:
        shown = os.path.relpath(target, REPO)
        if denied_root(target) is None:
            # Do not drive this probe. The path resolution the guard itself uses
            # is broken, so attempting the write would prove that by creating a
            # file inside a live store. Report the break instead.
            lines.append("  FAIL  %-19s %s not recognised as denied; probe not run"
                         % (label, shown))
            passed = False
            continue
        before = len(ledger.refused)
        blocked = False
        try:
            handle = attempt(target)
        except Refused:
            blocked = True
        except Exception as error:
            lines.append("  FAIL  %-19s raised %s, not Refused: %s"
                         % (label, type(error).__name__, error))
            passed = False
            continue
        else:
            _discard(handle, target)
        recorded = len(ledger.refused) - before
        leaked = os.path.exists(target)
        if blocked and recorded == 1 and not leaked:
            lines.append("  ok    %-19s refused and recorded   %s" % (label, shown))
        else:
            passed = False
            lines.append("  FAIL  %-19s refused=%s recorded=%d left_on_disk=%s  %s"
                         % (label, blocked, recorded, leaked, shown))

    intercepted = sum(1 for line in lines if line.startswith("  ok"))
    abstains_ok, abstain_lines = _abstention_probe(ledger)
    if not abstains_ok:
        passed = False
    capture_ok, capture_lines = _pathlib_probe()
    if not capture_ok:
        passed = False

    shutil.rmtree(scratch, ignore_errors=True)
    head = ("GUARD: self-test %s -- %d of %d write entry points intercepted"
            % ("PASSED" if passed else "FAILED", intercepted, len(probes)))
    return passed, [head] + lines + abstain_lines + capture_lines


def _module_name(target):
    name = os.path.basename(target)
    return name[:-3] if name.endswith(".py") else name


def report(refused, allowed, abstained=0):
    roots = " or ".join(name + "/" for name in DENIED)
    lines = []
    if refused:
        lines.append("GUARD: %d write(s) REFUSED into %s" % (len(refused), roots))
        for entry_point, root, path in refused:
            lines.append("  %-24s %s" % (entry_point, path))
    else:
        lines.append("GUARD: 0 nameable writes attempted into %s" % roots)
    lines.append("GUARD: %d write(s) elsewhere, permitted and counted, "
                 "across %d distinct prefix(es):"
                 % (sum(allowed.values()), len(allowed)))
    for prefix, count in sorted(allowed.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("  %7d  %s" % (count, prefix))
    # Printed beside the zero rather than folded into it. These are writes whose
    # target is resolved against an open directory fd, so the guard cannot name
    # the store they land in and declines to claim either way. The line has to
    # appear even when the count is 0, or its absence would be read as the
    # stronger claim.
    lines.append("GUARD: %d write(s) with a target the guard could not name "
                 "(relative path + dir_fd): neither refused nor cleared."
                 % abstained)
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a suite with every write into the two live stores "
                    "blocked and recorded.")
    parser.add_argument("suite", nargs="?",
                        help="suite to run: test_pipeline.py, test_harness, a path")
    parser.add_argument("--self-test", action="store_true", dest="self_test_only",
                        help="prove the interception and exit; run no suite")
    args = parser.parse_args(argv)
    if not args.suite and not args.self_test_only:
        parser.error("name a suite to run, or pass --self-test")

    os.chdir(REPO)
    if REPO not in sys.path:
        sys.path.insert(0, REPO)

    ledger = Ledger()
    restore = install(ledger)
    failure = None
    closing_ok, closing_lines = True, []
    code = 0
    try:
        passed, lines = self_test(ledger)
        ledger.take()        # the self-test's own refusals are not the suite's
        for line in lines:
            print(line)
        if not passed:
            print("GUARD: no write verdict is printed, and nothing was run. A "
                  "guard that failed to patch reports the same '0 attempted "
                  "writes' as one that worked, so the count would be worthless.",
                  file=sys.stderr)
            return 3
        if args.self_test_only:
            return 0
        name = _module_name(args.suite)
        print("\nGUARD: running %s under the same live patches\n" % name)
        try:
            suite = __import__(name)
        except ImportError as error:
            print("GUARD: cannot import %s: %s" % (name, error), file=sys.stderr)
            return 4
        entry = getattr(suite, "main", None)
        if not callable(entry):
            print("GUARD: %s has no callable main()" % name, file=sys.stderr)
            return 4
        try:
            code = entry()
        except SystemExit as raised:
            code = raised.code
        except BaseException as error:
            failure, code = error, 1
    finally:
        # Read the capture state while the patches are still installed: after
        # `restore()` every slot legitimately holds a real builtin again and the
        # question can no longer be asked.
        closing_ok, closing_lines = _pathlib_probe()
        restore()

    refused, allowed, abstained = ledger.take()
    print("")
    for line in report(refused, allowed, abstained) + closing_lines:
        print(line)
    if failure is not None:
        print("GUARD: the suite raised %s: %s"
              % (type(failure).__name__, failure), file=sys.stderr)
    if refused:
        return 2
    if not closing_ok:
        print("GUARD: the run's write counts do not cover pathlib, so they are "
              "not the claim they look like.", file=sys.stderr)
        return 5
    return 0 if code is None else int(code)


if __name__ == "__main__":
    sys.exit(main())

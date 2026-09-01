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
    not how the caller spelled it. File descriptors (`os.open` results passed to
    a `dir_fd=`-style call) and non-path objects resolve to None and are neither
    denied nor counted -- the guard reports what it can name.
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
    """What the guard saw. Refusals are listed; permitted writes are bucketed."""

    def __init__(self):
        self.refused = []
        self.allowed = collections.Counter()

    def refuse(self, entry_point, path):
        root = denied_root(path)
        self.refused.append((entry_point, root, _resolve(path)))
        raise Refused("%s would write into %s/ -- refused: %s"
                      % (entry_point, root, _resolve(path)))

    def allow(self, path):
        self.allowed[_bucket(path)] += 1

    def check(self, entry_point, path):
        """Refuse `path` if it is inside a denied root, otherwise count it."""
        if denied_root(path) is not None:
            self.refuse(entry_point, path)
        self.allow(path)

    def take(self):
        """Read and clear, so the self-test's own refusals are not the suite's."""
        refused, allowed = self.refused, self.allowed
        self.refused, self.allowed = [], collections.Counter()
        return refused, allowed


# Write *targets* by argument position, with the keyword spelling that reaches the
# same argument. Sources are deliberately absent: `eval/results/` is read-only,
# not read-denied, so `shutil.copyfile(eval/results/x, /tmp/y)` must succeed --
# only index 1 of a copy is a write. `os.replace`/`os.rename` and `shutil.move`
# name both, because they unlink the source as well as creating the destination.
_TARGETS = (
    ("os.mkdir", os, "mkdir", ((0, "path"),)),
    ("os.makedirs", os, "makedirs", ((0, "name"),)),
    ("os.remove", os, "remove", ((0, "path"),)),
    ("os.unlink", os, "unlink", ((0, "path"),)),
    ("os.rmdir", os, "rmdir", ((0, "path"),)),
    ("os.truncate", os, "truncate", ((0, "path"),)),
    ("os.replace", os, "replace", ((0, "src"), (1, "dst"))),
    ("os.rename", os, "rename", ((0, "src"), (1, "dst"))),
    ("os.renames", os, "renames", ((0, "old"), (1, "new"))),
    ("os.symlink", os, "symlink", ((1, "dst"),)),
    ("os.link", os, "link", ((1, "dst"),)),
    ("os.chmod", os, "chmod", ((0, "path"),)),
    ("shutil.rmtree", shutil, "rmtree", ((0, "path"),)),
    ("shutil.move", shutil, "move", ((0, "src"), (1, "dst"))),
    ("shutil.copyfile", shutil, "copyfile", ((1, "dst"),)),
    ("shutil.copy", shutil, "copy", ((1, "dst"),)),
    ("shutil.copy2", shutil, "copy2", ((1, "dst"),)),
    ("shutil.copytree", shutil, "copytree", ((1, "dst"),)),
    ("shutil.copymode", shutil, "copymode", ((1, "dst"),)),
    ("shutil.copystat", shutil, "copystat", ((1, "dst"),)),
    ("shutil.make_archive", shutil, "make_archive", ((0, "base_name"),)),
)

_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)


def _wrap_positional(ledger, label, original, targets):
    def guarded(*args, **kwargs):
        for index, keyword in targets:
            if len(args) > index:
                ledger.check(label, args[index])
            elif keyword in kwargs:
                ledger.check(label, kwargs[keyword])
        return original(*args, **kwargs)
    guarded.__name__ = "guarded_" + label.replace(".", "_")
    return guarded


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
        if isinstance(mode, str) and any(ch in mode for ch in "wxa+"):
            ledger.check("open(mode=%r)" % mode, file)
        return real_open(file, mode, *args, **kwargs)

    real_os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & _WRITE_FLAGS:
            ledger.check("os.open", path)
        return real_os_open(path, flags, *args, **kwargs)

    builtins.open = guarded_open
    os.open = guarded_os_open

    for label, module, name, targets in _TARGETS:
        original = getattr(module, name, None)
        if original is None:          # not on this platform; nothing to guard
            continue
        saved.append((module, name, original))
        setattr(module, name, _wrap_positional(ledger, label, original, targets))

    def restore():
        for module, name, original in saved:
            setattr(module, name, original)

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


def self_test(ledger):
    """Drive one real write through each entry point into a denied root.

    Returns `(passed, report_lines)`. Every probe must do three things: raise
    `Refused`, add exactly one line to the ledger, and leave nothing on disk.
    Blocked-but-unrecorded is a failure too -- a guard whose report is empty
    because it never wrote the report down is the case this exists to catch.
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

    shutil.rmtree(scratch, ignore_errors=True)
    head = ("GUARD: self-test %s -- %d of %d write entry points intercepted"
            % ("PASSED" if passed else "FAILED",
               sum(1 for line in lines if line.startswith("  ok")), len(probes)))
    return passed, [head] + lines


def _module_name(target):
    name = os.path.basename(target)
    return name[:-3] if name.endswith(".py") else name


def report(refused, allowed):
    roots = " or ".join(name + "/" for name in DENIED)
    lines = []
    if refused:
        lines.append("GUARD: %d write(s) REFUSED into %s" % (len(refused), roots))
        for entry_point, root, path in refused:
            lines.append("  %-24s %s" % (entry_point, path))
    else:
        lines.append("GUARD: 0 writes attempted into %s" % roots)
    lines.append("GUARD: %d write(s) elsewhere, permitted and counted, "
                 "across %d distinct prefix(es):"
                 % (sum(allowed.values()), len(allowed)))
    for prefix, count in sorted(allowed.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("  %7d  %s" % (count, prefix))
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
        restore()

    refused, allowed = ledger.take()
    print("")
    for line in report(refused, allowed):
        print(line)
    if failure is not None:
        print("GUARD: the suite raised %s: %s"
              % (type(failure).__name__, failure), file=sys.stderr)
    if refused:
        return 2
    return 0 if code is None else int(code)


if __name__ == "__main__":
    sys.exit(main())

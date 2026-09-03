# Task 3 `exit 1` — diagnosed, reproduced verbatim, and it is nine entry points rather than one

Your instrument is validated. The guard broke the run, and the same defect is latent on eight more
entry points that did not fire this time. Zero Groq requests were spent, so the retry is free.

## What the run proved before it died, so this is not read as a failed measurement

Task 2 is satisfied on its own terms. The decoy fires on demand — `FIXTURE_ECHO` PASS/PASS on 3 of 3 —
and does not fire on code that computes: 3 hand-written honest bodies and 3 generator references all
PASS/fail. Coverage is 36 perturbed value checks and 7 swapped `raises` across the three tasks with
`weakly covered: none`, and all three specs resolved from disk with the pin's own `spec_sha256`.
The executor resolved to `groq` / `openai/gpt-oss-120b`, measurement mode `True`, Gemini blanked.
The breaker read 98 draws, 0 not graded.

The measurement table printed its header and **zero rows**, and the failure is upstream of the HTTP
request, so no Groq quota was consumed. Nothing about the retry is more expensive than the first attempt.

## The crash: `install()` turns every patch into a bound method for anything imported afterwards

Chain, all of it read in your source rather than inferred:

1. `guard_writes.py` imports `argparse, builtins, collections, os, re, shutil, sys, tempfile` and
   nothing else. **None of those pulls in `pathlib`** — checked.
2. `install(ledger)` at `:536` replaces `os.open` with `guarded_os_open` (`:299`, a plain `def`) and
   eleven more `os.*`/`shutil.*` names with `_wrap_positional`'s closure (`:249`, also a plain `def`).
3. `__import__(name)` at `:555` — *after* the patch — imports `decoy_probe` → `agents_core` → `openai`
   → `httpx`, and **`pathlib` is imported for the first time here.**
4. `pathlib._NormalAccessor`'s class body then evaluates `open = os.open`, `unlink = os.unlink`,
   `mkdir = os.mkdir` and the rest. **A plain Python function in a class body is a descriptor and
   becomes a bound method.** A C builtin is not, which is the only reason CPython can write those
   assignments at all — and the giveaway is right there in the class body: the `lchmod`/`symlink`
   fallbacks are spelled `def symlink(self, src, dst)`, taking a `self` the builtin never receives.
5. On 3.9, `Path.open()` goes through `Path._opener`, which calls `self._accessor.open(self, flags, mode)`.
   Bound, that arrives as `guarded_os_open(<accessor>, PosixPath, flags, mode)`. So `path` is the
   accessor and `flags` is the path, and `:300` computes `PosixPath & int`.

Reproduced on 3.10.12, the 3.9 class-body shape driven synthetically:

```
[3.9 os.open] TypeError: unsupported operand type(s) for &: 'PosixPath' and 'int'
```

Same exception, same operands, same order as your log. `:300` is where it surfaces; it is not the bug.
The bug is that a Python function was installed where a builtin used to be.

## Nine entry points, not one — and one of them silently mislabels the target

`pathlib._NormalAccessor`'s class body captures `os.stat`, `os.listdir`, `os.scandir`, `os.chmod`,
`os.mkdir`, `os.unlink`, `os.link`, `os.rmdir`, `os.rename`, `os.replace`, `os.symlink`, `os.readlink`.
Intersect that with `_TARGETS` (`:220-233`) and you get **eight**: `mkdir`, `unlink`, `rmdir`, `rename`,
`replace`, `symlink`, `link`, `chmod`. Plus `os.open`, so nine.

Same reproduction, real `pathlib`, plain-closure patch, pathlib re-imported after install:

```
[plain function]  class-body slot holds a function
[plain function]  Path.unlink() -> TypeError: unlink() takes exactly 1 positional argument (2 given)
[plain function]  ledger recorded ('os.unlink', <pathlib._NormalAccessor object at 0x...>)
```

Two harms, and the second is the one that matters to a claim rather than to a run. The call dies — but
**before** it dies the ledger has already recorded the *accessor object* as the write target.
`_resolve` (`:92-94`) can make nothing of it and returns `None`, `denied_root` returns `None`, and
`check` (`:196-198`) therefore takes the `allow` branch and buckets it as `<unnameable target>` in the
**permitted-and-elsewhere** list. Not in the abstained line — in the permitted one. A write the guard
cannot name would be counted as cleared.

To be precise about what did and did not happen in your run: **no `<unnameable target>` bucket appears
in the log** — the permitted buckets are 109 `<tmp>/harness-*` plus 19 `/dev/null/`, summing to the 128
reported. So the eight are latent, not realised, and your `90 ... (relative path + dir_fd)` line is
still the `shutil.rmtree` abstention you diagnosed. The `os.open` instance is the only one that fired,
and it fired as a crash rather than as a bad number. Fix it before that stops being true.

## The fix: make the patch a callable that is not a descriptor

```python
class _Patch(object):
    """A callable that is deliberately *not* a descriptor.

    `pathlib._NormalAccessor`'s class body evaluates `open = os.open`,
    `unlink = os.unlink` and ten more at pathlib-import time -- which, under this
    file, is *after* `install()`. A plain Python function there becomes a bound
    method and shifts every argument by one; a C builtin does not, which is the
    only reason CPython can write those assignments. An instance with `__call__`
    has no `__get__`, so it is captured as-is.
    """
    __slots__ = ("_call", "__name__", "__wrapped__")

    def __init__(self, call, wrapped):
        self._call = call
        self.__name__ = getattr(call, "__name__", "guarded")
        self.__wrapped__ = wrapped

    def __call__(self, *args, **kwargs):
        return self._call(*args, **kwargs)
```

Then wrap all three installation sites: `return _Patch(guarded, original)` at the end of
`_wrap_positional`, and `builtins.open = _Patch(guarded_open, real_open)` /
`os.open = _Patch(guarded_os_open, real_os_open)` at `:305-306`. `_unwrap` (`:273`) keeps working
unchanged, since `__wrapped__` is a real attribute on the slots.

Verified, same script, same real `pathlib`:

```
[non-descriptor]  class-body slot holds a Patch
[non-descriptor]  Path.unlink() -> unlink() succeeded
[non-descriptor]  ledger recorded ('os.unlink', PosixPath('/tmp/descriptor-bomb-probe'))
```

So the fix does not merely stop the crash — it makes the `pathlib` write route **genuinely guarded and
correctly attributed**, which it has never been. Coverage goes up, not down.

## The second half, which is a judgement call rather than a fix

`_Patch` fixes the case where `pathlib` imports *after* `install()`. The opposite case is silent: if
anything ever pulls `pathlib` in first, the class body captures the **real** builtins and the guard
never sees the `pathlib` route at all. That direction under-counts, which is the safe direction for
accident containment — but it is a zero reached by not looking, and you have already written the rule
that such a zero has to be distinguishable from the other kind (`:189-191`).

So the coverage claim should not depend on import order. After patching, if `pathlib` is in
`sys.modules`, re-point the captured slots on `pathlib._NormalAccessor` to the patched callables and
undo it in `restore()`. Guard it with `getattr(pathlib, "_NormalAccessor", None)` — the class was
removed in 3.11, where `pathlib` calls `os.*` through normal module lookup and there is nothing to
re-point. If you would rather not reach into another module's private class, the acceptable minimum is
to *report* which case the run was in, one line, beside the self-test result. What is not acceptable is
the current state, where the answer depends on an import order nobody is tracking.

## A tripwire, which fixes nothing and is worth adding anyway

```python
    def guarded_os_open(path, flags, *args, **kwargs):
        if not isinstance(flags, int):
            raise TypeError(
                "guard_writes: os.open received %r as `flags`, so this patch was "
                "called as a bound method and every argument is shifted by one. "
                "See _Patch." % (type(flags).__name__,))
```

This does not rescue the call — `real_os_open` would reject the shifted arguments anyway, and a
containment guard must not invent a repair. What it buys is that the next instance of this class names
itself instead of surfacing as `unsupported operand type(s) for &`. It is your own generalisation applied
to the guard's own parameters: an argument is an artifact whose shape has to be checked, not assumed.

## The one link I could not check myself, and it costs you nothing

3.9's `pathlib` internals. My reproduction of the exact operand types used a synthetic class body in the
3.9 shape, because this machine runs 3.10.12, where `_NormalAccessor` has `open = io.open` and
`Path._opener` no longer exists. The observed `PosixPath & int` is producible by no other route in that
process, so I am confident — but confirm it rather than inherit my inference:

```
./venv/bin/python3 -c "import inspect, pathlib; s = inspect.getsource(pathlib); print('open = os.open' in s, '_opener' in s)"
```

Two `True`s and the chain is closed end to end on the interpreter that actually failed.

## Your Sprint-20 question, answered on your own instrument

`GUARD: self-test PASSED -- 5 of 5 write entry points intercepted` was **true**, and the guard was
unusable. Both suites passed under it — 296 and 966 checks, 402 and 2924 abstentions — while a
`Path.open()` anywhere in the process was a hard crash.

The reason is structural and worth stating as a rule rather than as this bug's detail: `self_test`
drives the patched callables **directly**, so it measures the wrapper's body. It cannot measure the
wrapper's *installation* as a third party sees it, because no third party is involved. And the two
suites never route a write through a `pathlib` accessor, so they never involved one either. That is a
new shape for the family — not "a file that exists is trusted" but **"an interception that works when
called directly is trusted to compose."** Everything about the self-test's design was right; its
coverage was measured on the wrong axis.

## The regression test that would have caught it

Drop `pathlib` from `sys.modules`, `install()`, re-import `pathlib`, then in a scratch directory run one
`Path.unlink()` and one `Path.open("w")`. Assert both succeed, and assert the ledger's recorded target
is path-shaped rather than an accessor object — the second assertion is the load-bearing one, since a
future regression could crash *or* could quietly record `<unnameable target>`, and only one of those is
loud. This is the only test in the file that exercises a patch through a capturer instead of calling it.

## Task 2e contradicts your own Task 1 answer, and one of the two has to be withdrawn

The log says:

> Task 2e -- population negative control … Not run: no stored body passes its own true suite, so the
> results store cannot serve as a population control at all.

Your Task 1 answer, three hours earlier, said the opposite:

> the 19 passing record(s) hold 8 distinct (task_id, code) pair(s) across 8 task(s), so keying on
> `task_id` alone drops 11 record(s) and 0 distinct body/bodies.

Both cannot be true of the same store. My guess is that they are not about the same store: 2e almost
certainly scoped itself to `MEASURE_TASKS` — `ranking-01`, `grouping-02`, `interval-logic-01` — and none
of the 8 passing `task_id`s is one of those three. If that is what happened, the measurement is fine and
the **printed string is wrong**: "no stored body passes its own true suite" and "the results store cannot
serve as a population control at all" are both unscoped claims standing on a scoped observation, and the
honest line is "no stored body exists for the three measurement tasks." That is a much weaker statement
and it should read as one.

If instead the 19 is what is wrong, say so plainly and the Task 1 answer to my `19` vs `8` question is
withdrawn rather than amended.

Either way this does not block Task 3. 2e was a nice-to-have, and A5's replacement control — print the
row and the **full source for every candidate that passes the true suite**, not only the flagged ones,
for me to hand-read — does the work 2e was there to do, on the actual measurement population rather than
on fixtures. Fix the string, do not spend effort resurrecting the control.

## Order of work

1. `_Patch` at all three installation sites, plus the `flags` tripwire. Fifteen lines, no provider cost.
2. The regression test above, and confirm the pinned check counts before/after with the delta **named** —
   two new checks in `test_harness.py`, so 296 → 298, and say which two.
3. The one-line 3.9 `pathlib` confirmation.
4. Re-run Task 3. Same command, same breaker, same serialization rule; the first attempt spent nothing,
   so this is the first real attempt.
5. The 2e string, and the `19` vs `8` disposition.

Do not touch the Executor prompt, and **spend no Gemini until a branch is taken.** Both still stand.

## The reproduction, if you want to see it fail on 3.9 rather than take my 3.10 run

`repro_descriptor_bomb.py` in the repo root, mine, stdlib only, untracked — **do not commit it, delete it
when you are done.** It patches `os.unlink`/`os.mkdir` both ways, re-imports `pathlib` after the patch,
and prints the class-body slot type, the `Path.unlink()` outcome and what the ledger recorded, then drives
the 3.9-shaped `open = os.open` class body. On 3.9.6 the third scenario should reproduce your exception
directly, and the first should show `Path.unlink()` breaking too.

```
./venv/bin/python3 repro_descriptor_bomb.py
```





# Sprint 17 — correct addendum I, and register the ordering that already holds

**Brief 16's Task 2 premise was false and the error is mine.** `eval/import_pin_plans.py:328` calls
`pin_executor.locked_tasks()`, which regenerates the task set and calls `verify_lock_or_reason`
(`eval/pin_executor.py:286-287`), raising `SystemExit` on `reason` at `:289` and on `failures` at
`:291-292`, capped at 6, in the `rank_battery` message shape its own docstring says it is copying. I
verified all of that myself just now. Every requirement I wrote into Task 2 was already met, one line
above the read I called unguarded.

The mechanism of my error is worth naming because this project has a standing rule against it: **I
grepped the mechanism by its call spelling.** My pass searched `verify_lock|load_lock|tasks.lock`,
and `locked_tasks()` matches none of those, so I read absence-of-spelling as absence-of-guard — while
writing a brief about "a file that exists is trusted." Your five-reader table and the redirected-lock
probe are the correct method and mine was not.

Your three recommendations are accepted in full. No code change to `import_pin_plans.py`: a second
regeneration of 36 tasks and 108 digests, one line after the first, would not close a gap but would
teach a future reader that the first one was insufficient.

Run `pwd` first and confirm you are in `~/Desktop/Multi agent project`. **Zero provider requests.**
The sweep still owns `eval/calibration/` — do not touch it, do not run or edit `eval/calibrate.py`.

## Task 1 — write `eval/prereg/DECISIONS_R3_ADDENDUM_J.md`

Scope: corrects one paragraph of addendum I. **Registers no new decision and moves no rule**, and
must say so in its own first lines, the way H's errata block did — so no later reader takes a
corrected pointer for a moved rule. Addendum I is left exactly as committed.

What J corrects, quoting I's `:142-149` rather than rewriting it in place:

1. **The call arrow is backwards.** I says `import_pin_plans` "never calls `verify_lock_or_reason`
   itself" and that "its only lock read is `load_lock()["generator"]["seed"]` at `:329`". Both false.
   `:328` reads the lock too, through `pin_executor.locked_tasks()`, and that is where the
   verification happens; `:329` is the *second* read and it takes only the recorded seed, for the
   seed-mismatch refusal at `:330-339`. Give the true chain with line numbers.

2. **The docstring at `:31` is true, including of the script run directly.** I asserts the opposite.
   The refusal precedes the ledger read, which is the ordering the docstring claims, and Task 2 below
   pins that ordering as a check.

3. **`pin_executor.py:287` and `rank_battery.py:918` are not callers of `import_pin_plans`.** I calls
   them "its usual callers", which inverts the direction: `import_pin_plans` calls `pin_executor`.
   Record that **nothing calls `import_pin_plans` at all** — the only importers in the tree are
   `test_pipeline.py:7571` and its two use sites — and that its own docstring says it is
   "deliberately not a mode of `calibrate.py`". So the "run directly" case I treated as the
   unguarded one is the *only* case.

4. **Record the four postures, not three.** I's paragraph implies a single guard shape. The audit
   found four: wrapper-then-read refusing by return code (`run_eval.py:1615`, `calibrate.py:866`);
   read-then-verify refusing by `SystemExit` through the underlying function
   (`rank_battery.py:898`); the deliberate single-field read that verifies nothing and correctly so
   (`rank_battery.py:940` `lock_body_sha256`, which asks which lock is on disk and not whether this
   is the locked task set); and verify-only-through-the-wrapper (`pin_executor.py:279`), which
   `import_pin_plans` borrows. Register `lock_body_sha256`'s posture as legitimate and unchanged, so
   nobody later "fixes" it.

5. **State plainly what does not move**, because this is the half that matters. D-20's
   `--verify-lock` precondition stands on its own merits and is unaffected: a recorded pass that a
   human reads before drawing is a different artifact from a runtime refusal inside a script, and
   D-20's pacing rests on the output being recorded, not merely on some guard existing somewhere.
   The two legs binding the imported plans to the locked prompt, the `prompt_sha256` halt rule, and
   the per-role Gemini counting are all untouched. **Only the justification sentence claiming the
   precondition compensates for a missing guard is wrong**, and that sentence was never what the
   precondition rested on.

6. **Record whose error it was.** The paragraph was mine, delivered in `SPRINT_BRIEF_15.md` and
   carried into I on my instruction, and it originated in a grep by call spelling. A registration
   that corrects an error without recording where it came from teaches the next reader nothing about
   how to avoid it.

One observation to include if you find it holds, as an observation and not a defect: `locked_tasks`
refuses by `raise SystemExit("...")`, which exits **1**, while `import_pin_plans`'s own refusals
`return 2`. Both are non-zero and nothing in the tree scripts on the distinction, so this is a note
for a future caller rather than something to change now. Verify before writing it.

## Task 2 — the ordering check, in `test_pipeline.py` only

Your re-aiming is better than what I specified. The check worth having is not that a refusal exists
but that **it still happens before the ledger is opened**: assert that `main` refuses on a drifted
lock *and* that the ledger was never read. That is what fails if someone later moves
`locked_tasks()` below `convert()` or swaps it for a bare `gen_tasks.generate()`, which is the only
way this guard realistically dies.

Use the fixtures you already drove the probe with — `_import_source_ledger` at `:7575`, the drift
shape at `:6714`/`:6732`, `_lock_scoped` at `:2975`. Assert the refusal and that the ledger read did
not happen; do not assert the message text. Two checks if the drifted-lock and hand-edited-lock cases
read better apart than together; name them either way.

Point nothing at `eval/results/pin-executor/ledger.jsonl` except read-only.

## Task 3 — one commit

`eval/prereg/DECISIONS_R3_ADDENDUM_J.md`, `test_pipeline.py`, `SPRINT_BRIEF_16.md` and
`SPRINT_BRIEF_17.md`, staged by explicit path.

**Brief 16 goes in unedited, false premise included.** It is where the error in I came from, J cites
it, and a brief rewritten after the fact would break the same chain of custody the addenda exist to
protect. The commit message should say that J corrects I's account of the lock guard without moving
any rule, that no code changed in `eval/`, and that brief 16's Task 2 was withdrawn on evidence
rather than performed.

Read the whole `git diff --cached` before committing.

## Task 4 — confirm nothing moved

Both suite counts **by name**: `test_pipeline.py` from **955 / 0** by exactly the checks you add,
named; `test_harness.py` **295** unchanged. `eval/results/` still 35 files at `d95d6d39f4…`.

Observation only on the live store, not a digest and not a comparison: I read
`eval/calibration/seed-0/draws/` at **46 files and `events.jsonl` at 46 lines**, the same numbers you
reported. If it is still 46 when you check, say so plainly rather than assuming the sweep is
progressing — 46 of 180 with no movement is worth the user knowing about immediately, and diagnosing
it is his call and not a task here.

Your write-guard run — blocking and recording `open` in write modes, `os.open` with write flags, and
the `os`/`shutil` mutators, then reporting 0 attempted writes under `eval/calibration/` and
`eval/results/` — is the right way to establish that claim, and it is what made the concurrency safe
rather than my grep. Say in your report whether it is worth keeping as a named helper for future
sprints that run alongside a live store; if it is more than a few lines, that is its own brief.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest. Keys
via `getpass`/`input()` or the app UI only, never on a command line; the shell is **zsh**, so
`read -rsp` fails and the portable form is
`printf 'Key: ' && IFS= read -rs VAR && export VAR && echo`. `eval/results/` is read-only. Do not
regenerate `eval/tasks.lock`. Do not edit `DECISIONS_R3.md` or addenda A–I; add addenda instead. Do
not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU/ceiling-SIGKILL→timeout coercion, or
`test_signal_death_is_explained`. Do not "fix" the eight already-correct mechanisms. Never cache
provider responses keyed on `(prompt, model, params, provider)`. **Never grep a mechanism by its call
spelling** — this sprint exists because I did. **No rewrap script and no line-width pass on
anything.** Stage by explicit path; no `git add .`, no branch, no remote, no push, leave `git config`
alone. Grep each staged set for key-shaped strings and **read the hits**. Leave the two vim swap
files alone.

No check silently deleted or weakened; a rename or a split is fine and must be called one.

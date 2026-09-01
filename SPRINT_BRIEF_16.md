# Sprint 16 — close the one lock reader that trusts the file, while the sweep runs

The 180-draw calibration sweep is **live right now**: `eval/calibration/seed-0/draws/` exists and is
filling, and `events.jsonl` is being appended. Everything below is chosen to be safe alongside it.

**Hard boundary: do not touch `eval/calibration/` or `eval/results/` in any way, and do not run
`eval/calibrate.py` for any reason.** Its `--out` defaults to `CALIBRATION_DIR` (`:702`), so a bare
invocation writes into the live store. Do not edit `eval/calibrate.py` either — the running process
already holds its own copy, so an edit would not affect this run and would make the record of what
produced these draws untrue.

Run `pwd` first and confirm you are in `~/Desktop/Multi agent project`. **Zero provider requests.**

Both suites are safe to run concurrently — I checked, and neither `test_pipeline.py` nor
`test_harness.py` writes under `eval/calibration/` or `eval/results/`; the only mentions are in
docstrings, and lock-drift checks redirect `gen_tasks.LOCK_PATH` through the `_with_lock_path` helper
at `test_pipeline.py:2976`. Confirm that yourself before you run them.

## Why this sprint

Addendum I registered one gap and explicitly deferred it: `eval/import_pin_plans.py` reads the lock
without verifying it, and its own docstring claims otherwise. That is the project's recurring "a file
that exists is trusted" defect, and this is the sixth instance — the first five were the non-atomic
`save_record`, the imported-plan `plan_sha256`, `--stub` draws landing in the real store, `--family`
silently dropping unmatched names, and the two-writer collision at the `_ADDENDUM_H.md` path. It is a
small change and it is the last one standing in that family that I know of.

## Task 1 — audit every lock reader, read-only, before changing anything

Enumerate every reader of `eval/tasks.lock` outside `eval/gen_tasks.py` and state, per reader,
whether a verification runs before the read and where. Paste the table. My own pass found four
readers and three postures; verify each rather than taking them:

- `eval/run_eval.py` — `verify_lock_or_reason` at `:1615`, refuses at `:1620-1630`, then
  `load_lock()` at `:1631`. Guarded.
- `eval/calibrate.py` — `verify_lock_or_reason` at `:866`, then `load_lock()` at `:881`. Guarded.
- `eval/rank_battery.py` — `load_lock` at `:913` then `verify_lock` at `:918` with its own
  `SystemExit`, calling the underlying function rather than the wrapper. Guarded, by a different
  entry point, and its refusal message is its own. Also `lock_body_sha256` at `:943`, which reads
  only `body_sha256` for a staleness stamp: **that one is a legitimate unguarded read** — it is
  asking "which lock is on disk" and not "is this the locked task set" — and it should be registered
  as legitimate in your report rather than changed.
- `eval/import_pin_plans.py` — `load_lock()["generator"]["seed"]` at `:329`, with **no verification
  anywhere in the module**. Unguarded.

If you find a fifth reader, name it and stop before Task 2 so we can decide whether it belongs in
this sprint.

## Task 2 — guard `eval/import_pin_plans.py`

Add the verification the module's own docstring already promises. Its `:31` says "and the lock is
verified before the ledger is read", which is true of `run_eval` and `calibrate` calling it and false
of the script run directly.

- Call `gen_tasks.verify_lock_or_reason` before the ledger is read — before, not after, and before
  the seed comparison at `:329-337`, since a lock that disagrees with the generator makes its
  recorded seed a claim about nothing.
- Refuse non-zero, naming the failures the way the other callers do. Match the existing posture; do
  not invent a new message shape. `rank_battery.py:920-926` shows the refusal wording this project
  uses for "the working tree disagrees with the lock", and it caps the list it prints.
- Handle the `reason` return — "there is no lock" and "the lock disagrees" are separate outputs of
  that function for a reason, and both must refuse here.
- **Add no escape hatch.** No `--no-verify-lock` flag. The other callers do not have one at this
  layer and a flag would recreate the gap under a name.

**If closing this changes which records the import selects or accepts, stop and report instead of
proceeding.** A refusal path is a code change and needs no addendum; a change to what gets imported
would touch the 19 specs D-20 depends on, and that is a registration matter, not a sprint task.

## Task 3 — one check, in `test_pipeline.py` only

An import run against a lock the generator no longer reproduces refuses. Reuse the drift fixtures
already there — `test_pipeline.py:6714` and `:6732` build a drifted and a hand-edited lock by
deep-copying the real one, and `_with_lock_path` at `:2976` redirects `LOCK_PATH` — so this should be
a small addition rather than new scaffolding. Assert the refusal and its exit status, not the exact
message text.

If the check needs the ledger, point it at a fixture and never at
`eval/results/pin-executor/ledger.jsonl`. That file is 19 Gemini requests and one full day's quota,
and it is not in git.

## Task 4 — one commit

`eval/import_pin_plans.py`, `test_pipeline.py` and `SPRINT_BRIEF_16.md`, staged by explicit path.
The message should say that it closes the unguarded lock read addendum I named, that the docstring
claim at `:31` is now true of the script run directly, and that no registered rule moves.

Read the whole `git diff --cached` before committing and confirm it is your text — that is the
standing rule from Sprint 15's near-miss, and the tree still has more than one writer in it.

## Task 5 — confirm nothing moved

Both suite counts **by name**, before and after. `test_pipeline.py` was **955 / 0** and
`test_harness.py` **295 total** on 3.9.6; `test_pipeline.py` should move by exactly the number of
checks you added, named.

`eval/results/` still 35 files at `d95d6d39f4…`. For `eval/calibration/`, **report the draw count as
an observation and expect it to have changed** — the sweep owns that directory this hour, and a
changed count there is the sweep working, not a regression. Do not digest that store and do not
compare it against `a858fbd6ed…`, which was taken before `draws/` existed.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest.
Keys via `getpass`/`input()` or the app UI only, never on a command line — and the user's shell is
**zsh**, so `read -rsp` fails there; the portable form is
`printf 'Key: ' && IFS= read -rs VAR && export VAR && echo`. `eval/results/` is read-only. Do not
regenerate `eval/tasks.lock`. Do not edit `DECISIONS_R3.md` or addenda A–I; add addenda instead. Do
not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU/ceiling-SIGKILL→timeout coercion, or
`test_signal_death_is_explained`. Do not "fix" the eight already-correct mechanisms. Never cache
provider responses keyed on `(prompt, model, params, provider)`. **No rewrap script and no
line-width pass on anything.** Stage by explicit path; no `git add .`, no branch, no remote, no push,
leave `git config` alone. Grep each staged set for key-shaped strings and **read the hits** — the
redaction fixtures are legitimate. Leave the two vim swap files alone.

No check silently deleted or weakened; a rename or a split is fine and must be called one.

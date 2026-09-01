# Hotfix: `--replay-plans` compares digests of two different widths

Sprint 10 shipped at `1af801d`. `eval/pin_executor.py --replay-plans` refuses every real ledger.
This is a one-file fix plus a test-fixture correction. It is not a sprint; do only what is below.

## The defect, measured

Running the replay against `eval/results/pin-executor/ledger.jsonl` aborts on the first plan record:

    REFUSING TO REPLAY aggregation-01: the source ledger's suite does not hash to its own tests_sha256
      recorded 984b7e1ce0d8d06c
      rehashed 984b7e1ce0d8d06c2f9100006820c955ad15800d944ea81ff8fc1d9b76392c04

The recorded value is 16 chars and is an exact **prefix** of the 64-char rehash. The suite bytes are
intact. Two functions are involved:

- `eval/run_eval.py:520` writes the field with `_suite_hash`, which is `hexdigest()[:16]`.
- `eval/pin_executor.py:392` rehashes with `agents_core.sha256_of`, which is the full `hexdigest()`.

Across all 19 plan records that carry a spec: 19 of 19 digests are 16 chars, 19 of 19 are exact
prefixes of the full hash of their own stored `tests`, **0 genuine mismatches**. So the guard is
sound in intent and cannot pass on any ledger production has ever written. It failed closed; nothing
was corrupted and only the 3 preflight Groq calls were spent.

`eval/gen_tasks.py:1931` already documents the two widths in a comment, so the ambiguity was known.

## Why the suite did not catch it

`test_pipeline.py:6168` builds the fixture with `"tests_sha256": agents_core.sha256_of(tests)` — the
test writes the field with a different function than production uses — and then asserts at :6224 that
the digest "is copied intact and still describes the text". The invariant is true of the fixture and
false of every real ledger, so Part F's digest-rehash check has never run against production shape.

Note that `test_pipeline.py:1643` in the same file already does this correctly, using
`run_eval._suite_hash(body)`. The Sprint 10 fixture diverged from the file's own convention.

## Task 1 — rehash by the width the record claims

In `replayed_plan_unit` (`eval/pin_executor.py`, around line 390), select the hash function from the
length of the claimed digest: 16 uses `run_eval._suite_hash`, 64 uses `agents_core.sha256_of`, and any
other non-empty length is refused with the existing message plus the observed length. `run_eval` is
already imported at line 62 and used 10 times, so no new import is needed.

Two things not to do. **Do not accept a prefix match** — that weakens a real integrity guard to paper
over a writer mismatch, and it would let a 1-char digest validate anything. **Do not unify the repo on
64 chars**: `_suite_hash`'s 16-char form wrote the 19 digests the replay needs, and 64 is separately
locked by `test_pipeline.py:2738` and `:3446`, so a blind unification invalidates the stored ledger and
breaks two passing checks.

Preserve today's behaviour for the both-empty case (`tests` empty and `claimed` empty currently
validates, because both functions return `""`). A width fix must not silently start refusing records
it accepts today; if you think that case should be refused, say so and leave it alone.

## Task 2 — make the fixture match production

Change `test_pipeline.py:6168` to write the digest the way `eval/run_eval.py:520` does, so the Part F
assertion exercises the real shape. Then add a second case covering a 64-char digest, so both widths
are locked, and one covering an unknown width to prove the refusal still fires. State the before/after
check counts by name as usual.

## Constraints

Confine the change to `eval/pin_executor.py` and `test_pipeline.py`. Do **not** touch `harness.py` or
`agents_core.py`: the pin replay has to measure commit `1af801d`, because the 57 draws it produces are
also the measurement of the Sprint 10 source prologue, and that number is meaningless against a moving
harness. `eval/results/` stays read-only — the source ledger is evidence, not an input to edit. Do not
regenerate `eval/tasks.lock`. Standard git rules: stage by explicit path, no branch, no push.

## Verification before you report

Run both suites on 3.9.6 and report counts by name. Then, with the venv active, run the replay and
paste the real output — the run must reach the draws, not just the preflight:

    source venv/bin/activate
    python3 eval/pin_executor.py --replay-plans eval/results/pin-executor/ledger.jsonl --out eval/results/pin-replay-1

Reaching 57 Groq draws with 0 Gemini calls is the pass condition. Do not report the fix as landed on
the strength of the unit tests alone; the unit tests are what missed this.

## Also outstanding, small

`eval/calibrate.py`'s `cli()` docstring is copied verbatim from `run_eval.py` and names `run_one` and
`infra_loss`, neither of which exists in `calibrate.py` — its path is `calibration_plan` / `one_draw`
plus the `os.path.exists` resume checks. The behaviour is correct; the cited reason is imported. Fix
the wording while you are in the file.

# Sprint 14 — addendum H, the five things the read-through found after G was committed

`5c1a25b` is **verified**. I read the whole file rather than the diff, re-derived every code claim in it
myself, confirmed `git diff fbecaba 5c1a25b -- eval/prereg/` touches G alone, and ran both suites:
`test_harness.py` **293 passed, 2 failed** (the two known `match` checks) = 295, `test_pipeline.py`
**926 passed, 0 failed**. `eval/results/` still 35 files at `d95d6d39…` with nothing newer than
2026-08-31 23:35, `eval/calibration/` 19 at `a858fbd6…`, `seed-0/` still only `plans`.

Both fixes you made beyond the five I asked for are right, and the second is the better catch: the
suite that can produce a false APPROVED is the visible one, and the draft cited D-10 — titled *A
trusted visible suite can be wrong* — in the sentence that said otherwise. My "two writers" of
`gate_tests_sha256` was wrong; three is right, and `:526` is the pass-through the first two read.

G's preamble says any later change to D-17 or D-18 needs its own addendum. This is that addendum. It
is the case the preamble anticipated, arriving one commit later than it hoped.

Run `pwd` first and confirm you are in `~/Desktop/Multi agent project`. **Zero provider requests.**

## Scope, and why this does not hold up the sweep

State this in H itself, near the top: **H binds the grid, not the calibration sweep.**
`eval/calibrate.py` never touches `retained_round`, `repair_rounds`, `final_round_worse` or
`_verify_step` — a calibration draw is a single A′-style shot with no repair ladder, so no draw can
carry the fields D-18 reads. The 180 draws may be launched before or after H lands without affecting
either. Nothing in this sprint may change what a sweep selects or measures.

**Do not edit G**, or `DECISIONS_R3.md`, or addenda A–F. **No rewrap script and no line-width pass on
anything**, and no reflow of any committed prereg file.

## Task 1 — write `eval/prereg/DECISIONS_R3_ADDENDUM_H.md`

Register **D-19** as three clause-level replacements to D-18, each naming the D-18 clause it replaces
and the code line that forced it, plus a separate errata block that changes no rule and must be
labelled as changing no rule. D-18 stands except where a clause below replaces it; say that plainly.

Verify all three against the code before writing, and paste what you find.

**1. `final_round_worse` cannot be a rate over arm-B cells** — D-18's line 249. Four things collide
there. `_retain_best` is the field's only writer (`agents_core.py:2697`); its default is `False`
(`:2173`); the `VERDICT_UNVERIFIED` exit returns without calling `_retain_best` (`:2779`); and
`eval/run_eval.py:770` writes `False` when there is no graded step. So `False` carries "not worse",
"never computed" and "no step" in one denominator — the absence-collapsed-into-a-value shape the stub
guard closed in `calibrate.py`, reappearing in a reporting rule.

The sharper half: on the approved group the field is a **constant**. The approved exit appends the
passing candidate last, so `last is best`, and `_candidate_merit(last) < _candidate_merit(best)` is
false by construction. `True` is reachable only in the exhausted and provider-error groups, so a rate
over all arm-B cells moves with the approval rate rather than with the ladder being net-harmful.
Register the denominator as the cells where `_retain_best` ran **and** a later round existed, with the
excluded counts reported beside it — the same split D-18 already applies to `retained_round`. Note
that this is D-18's own point 3 applied to the one field that escaped it.

**2. `escalation` labels the rung for one group, not four** — D-18's lines 166–168, which say it
"records which rung produced the retained attempt". True for approved cells only.
`record.escalation = "exhausted"` overwrites the rung (`agents_core.py:2781`), and `:2811` sets a rung
only after a revision call *succeeds*. So the exhausted group carries the literal `"exhausted"` and no
rung at all, and a provider-error cell carries the last rung that succeeded rather than the one that
failed. That sentence sits in the axis section ahead of the group split, so it reads as covering all
four groups, and the rung is missing from exactly the histogram where "which rung produced this" is
the question — the exhausted one. Register the rung label as available on the approved group, and
register that the exhausted group's rung is **not recoverable from the record**, which is a limit to
state rather than a field to add. Do not add a field.

**3. The axis is the last graded step, not the cell.** `step = graded[-1]`
(`eval/run_eval.py:751-752`), and `retained_round`, `repair_rounds`, `escalation`,
`final_round_worse` and `pipeline_says_passed` are every one of them read off that single step —
`pipeline_says_passed` is its `verified` property (`agents_core.py:2181`), while the whole-run count
lives in the separate `steps_approved`. D-18 says "arm-B cells" throughout, which is narrower than it
reads. On the only arm-B data that exists — the 8 cells in `eval/results/` — `steps` and
`graded_step` are both 1, so today the distinction is a nullity; but the Planner writes the step list
and nothing constrains it to one. Register the axis as **the last graded step of the cell**, register
that the `steps` and `graded_step` distributions are reported so a reader can see how often step and
cell coincide, and register that the two stop coinciding — and the write-up must say so — the first
time a cell reports `steps > 1`. No new field: both are already on the record.

**Errata, changing no rule.** Label the block that way, so no later reader takes a corrected pointer
for a moved rule.

- D-18 lines 113 and 238 cite "(Revision 2, Part 2)" for the 11-level `d_t` covariate. Revision 2's
  parts are lettered A–G and the covariate is **item 2 of Part A**
  (`EVAL_PREREGISTRATION_REVISION_2.md:19-21`). "Part 2" resolves to a real section of a *different*
  document — the Amendment's metric contract, which G cites correctly three lines above at line 26 —
  so the mislabel points a reader somewhere that exists and is wrong.
- D-17 line 54 enumerates the harness failure kinds as six; there are **ten** (`harness.py:1059-1068`).
  Missing: `FAIL_TIMEOUT_TESTS`, `FAIL_TIMEOUT_TEARDOWN`, `FAIL_OUTPUT`, `FAIL_PATH`. Complete the list
  rather than marking it partial, because `FAIL_PATH` is a *harness* bug — the unverified group's own
  category — and `_candidate_rank` element 3 splits `FAIL_ASSERTION` from all nine others.
- D-17 line 34 puts "the refusing lock guard" at `eval/run_eval.py:1607`, which is a comment. The
  guard is `gen_tasks.verify_lock_or_reason` at `:1615`, refusing at `:1622`.
- D-17 line 41 cites the width comment at `:884`; the clause it quotes is at `:886-888`.

## Task 2 — make two of the registered identities checks instead of prose

`test_pipeline.py` only. **No change to `agents_core.py`, `eval/run_eval.py` or `eval/calibrate.py`**,
and none is needed — both identities already hold; the point is that nothing currently notices if they
stop holding.

1. An approved cell has `final_round_worse` **False** and `retained_round == repair_rounds + 1`. The
   second follows from `rounds` defaulting to 0 (`agents_core.py:2141`) and being written only at
   `:2810`, after a revision call succeeds. It is the identity D-18 already registers as free, at
   its lines 206–210.
2. `retained_round` is never 0 for a cell with a graded step, because `:2743` precedes every exit.
   Assert it against a step the loop actually ran rather than against a constructed record.

Reuse whatever fixture the existing `_verify_step` checks use. **If asserting one of these requires
re-implementing `_candidate_rank` or `_retain_best` inside the check, do not write it** — a check that
restates the ranking proves the restatement, not the code. Say which one you skipped and why, and
leave that identity in prose in H.

## Task 3 — two commits, in this order

1. `eval/prereg/DECISIONS_R3_ADDENDUM_H.md` and `SPRINT_BRIEF_14.md`, alone. The message should say
   that H corrects D-18's reporting rules and D-17's citations without changing D-17's or D-18's
   registered substance, that G is left as committed, and that H binds the grid and not the
   calibration sweep.
2. The `test_pipeline.py` checks.

Stage by explicit path both times.

## Task 4 — confirm nothing moved

Both suite counts **by name**, before and after. Task 1 changes neither, and Task 2 should move
`test_pipeline.py` by exactly the number of checks you added — name them. Then confirm `eval/results/`
is still 35 files at `d95d6d39…`, `eval/calibration/` 19 at `a858fbd6…`, and
`eval/calibration/seed-0/draws/` still does not exist.

**Do not run the sweep.** The 180 draws are the user's to launch, and they are launched after this
lands so the tree is clean and the draws are the only new artifacts.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest. Keys
via `getpass`/`input()` or the app UI only, never on a command line. `eval/results/` is read-only. Do
not regenerate `eval/tasks.lock`. Do not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the
SIGXCPU/ceiling-SIGKILL→timeout coercion, or `test_signal_death_is_explained`. Do not "fix" the eight
already-correct mechanisms. Never cache provider responses keyed on `(prompt, model, params,
provider)`. Stage by explicit path; no `git add .`, no branch, no remote, no push, leave `git config`
alone. Grep each staged set for key-shaped strings and **read the hits** — the redaction fixtures are
legitimate. Leave the two vim swap files alone.

No check silently deleted or weakened; a rename or a split is fine and must be called one.

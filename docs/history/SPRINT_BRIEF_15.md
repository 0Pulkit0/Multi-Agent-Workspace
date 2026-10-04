# Sprint 15 — addendum I, and the numbering settled by commit order

`da43953` and `1d6be48` are **verified**. Your four corrections to Sprint 14's pointers are all
correct and I concede each. Your denominator predicate for `final_round_worse` —
`verdict != VERDICT_UNVERIFIED and repair_rounds + 1 > retained_round` — is better than the rule I
specified: I asked for "cells where `_retain_best` ran and a later round existed", which names a
function, and you turned it into a predicate over two stored fields. I traced it through all four
exits and it selects exactly the cells where the field can be `True`, without restating the ranking.
That is the shape the brief's own "do not re-implement `_candidate_rank`" clause was reaching for.

The failing check was the best thing in the sprint. `PLAN_NO_TESTS` leaving **two** graded steps
means D-19.3's step-vs-cell distinction is exercised by a fixture today, not merely registered
against a future the Planner might produce. H's rescoping of "the distinction is a nullity" to the
recorded 8-cell corpus is the right fix.

## The numbering, ruled

**`da43953` settles it. D-19 is the three D-18 replacements, permanently.** An hour before you
committed, I told the user the reverse — that H should carry the spec policy and your content should
become I. That ordering was defensible until a commit landed; it is not now, for the reason A
through G all state: a registration that changes after it has been read is not a registration, and
`da43953` was read off this machine. The commit outranks my earlier call. I am reversing myself for
that reason alone and no other.

So the Planner-spec decision becomes **addendum I**, registering **D-20** and **D-21**.

Run `pwd` first and confirm you are in `~/Desktop/Multi agent project`. **Zero provider requests.**

## Task 1 — write `eval/prereg/DECISIONS_R3_ADDENDUM_I.md`

Source text is `RESCUED_DRAFT_planner_spec_addendum.md`, the 158-line draft you preserved. Its
substance is sound and I verified its load-bearing claims myself:

- `agents_core.py:109-110` — `planner` and `test_writer` both resolve to gemini. Its "per-task
  Gemini cost is 1–2, not 1" is right, and its per-role counting rule at lines 72–78 already says
  what I was going to ask for, so it needs only to survive the renumbering.
- `eval/import_pin_plans.py:29-30` — quoted accurately; the prompt is the one digest of four that no
  ledger records.
- "the Test Writer fired 0 times in 19 plans" cites addendum C correctly
  (`DECISIONS_R3_ADDENDUM_C.md:66`).
- All 19 plan IDs under `eval/calibration/seed-0/plans/` are present in `eval/tasks.lock` (36 tasks,
  generator `{seed: 0, per_family: 2, families: [], tiers: [], limit: None}`).

Carry it across with these five changes and nothing else.

1. **Renumber only.** Its D-19 becomes **D-20** (one Planner spec per task, front-loaded and
   frozen); its D-20 becomes **D-21** (the off-grid Planner probe). Every internal cross-reference
   moves with it — its D-19 body cites "(D-20)" in the verbatim caveat sentence, which becomes
   "(D-21)".

2. **Provenance, one paragraph in the preamble.** The text was drafted by a second agent writing
   into this tree, was found at the `_ADDENDUM_H.md` path at 16:00:31 on 2026-09-01, was preserved
   byte-identically as `RESCUED_DRAFT_planner_spec_addendum.md`, and is registered here under its
   own letter because H had already committed D-19. Say that it was never committed under H, so
   nothing is being revised in place. Attribution matters in a registration: a reader must be able
   to tell which decisions this project's author made and which arrived from elsewhere.

3. **Rewrite the lock precondition against the guard that already exists.** See Task 2 below; this
   is the one substantive change and it makes the item shorter, not longer.

4. **Add the git-history leg, which closes the gap the draft leaves open.** Verified, at zero cost:
   `eval/gen_tasks.py` and `eval/tasks.lock` were introduced by the same commit, `71fc347`
   (2026-08-31 17:23:38 +0530), and **no commit since has touched either** — `1d6be48` is HEAD and
   the working tree holds nothing but the untracked rescued draft. The 19 imported plans were drawn
   at 2026-09-01 00:24, after that commit. So the generator was byte-identical when those plans were
   drawn and now, which is precisely the link `import_pin_plans.py:29-30` says no ledger records.
   Register it as: *the imported plans' binding to the locked prompt rests on two legs, a digest
   comparison run now and the absence of any commit to `eval/gen_tasks.py` between the lock and the
   draw; both are recorded, and the second is what makes the first evidence about the plans rather
   than only about today.* If a future import happens after `gen_tasks.py` moves, the second leg is
   gone and the plans are not importable at all.

5. **Withdrawn: my "refuse rather than discard" correction, as I phrased it.** I misread the draft's
   line 69. "That task's imported spec is discarded and drawn fresh" does not shrink the task set —
   it costs one more Gemini generate and keeps all 36 tasks. Coverage is preserved and the count is
   reported. The draft is right and my objection was aimed at a policy it does not state.

   What survives is sharper and belongs in I: a `prompt_sha256` mismatch is **not** a per-task
   event. If today's generator produces a different prompt for one task than the lock records, then
   every run from that moment sends a prompt that is not the registered one for that task, so
   redrawing the spec fixes the spec and leaves the grid running an unregistered task set.
   **Register: a mismatch halts, names the task and both digests, and is a lock-integrity finding
   that must be resolved or re-registered before any draw — the redraw is what happens after the
   lock question is settled, not instead of settling it.** D-16's own void condition already implies
   this: a run that is not the registered task set is not the run D-16 describes.

## Task 2 — the precondition is an existing command, not new code

The draft registers "regenerate the task set at the generator config the lock records and compare
`prompt_sha256` per task." **That is `python3 eval/gen_tasks.py --verify-lock`, which already
exists and already does exactly this.** Verify before you write, and paste what you find:

- `verify_lock` (`eval/gen_tasks.py:2060`) documents three legs, and leg 1 is "regenerate from the
  lock's own `generator` block and compare every digest", which catches "a reworded prompt".
- `_compare` (`:2130`) walks `DIGEST_PARTS` = `("prompt", "tests", "reference")` (`:1946`) and names
  the task and the part that moved, in the exact form the halt in change 5 needs.
- It also checks the recorded self-check battery against the one this code runs (`:2097-2106`) and
  refuses a hand-edited lock via `body_sha256` (`:2087-2093`) — two legs the draft does not mention
  and does not need to reinvent.
- `_cmd_verify_lock` at `:2272`; `run_eval.py:1615` and `calibrate.py:866` call
  `verify_lock_or_reason` before spending a request.

So register the precondition as: **run `--verify-lock` and record its output; zero failures is the
pass.** Not a script to write.

One gap worth a clause, because it is the difference between "verified" and "verified by whoever
happened to call it": `eval/import_pin_plans.py` is a standalone CLI (`main` at `:311`,
`__main__` at `:384`) and it **never calls `verify_lock_or_reason` itself** — it only reads
`load_lock()["generator"]["seed"]` at `:329`. Its docstring's "the lock is verified before the
ledger is read" is true of `run_eval` and `calibrate`, its usual callers, and not of the script run
directly. Register the `--verify-lock` step as explicit and separate for that reason. **Do not add
the guard to `import_pin_plans.py` in this sprint** — it is a code change, this addendum is not,
and it wants its own brief.

## Task 3 — two commits, in this order

1. `eval/prereg/DECISIONS_R3_ADDENDUM_I.md` and `SPRINT_BRIEF_15.md`, alone. The message should say
   that I registers the Planner spec policy as D-20 and the off-grid probe as D-21, that the text
   was drafted elsewhere and is attributed in the preamble, that H's D-19 is untouched, and that the
   lock precondition resolves to the existing `--verify-lock` guard.
2. Remove `RESCUED_DRAFT_planner_spec_addendum.md`, and only after commit 1 exists. It is untracked,
   so this is a `rm` and not a commit; say in your report that you did it and after what.

Stage by explicit path both times.

## Task 4 — confirm nothing moved

Both suite counts **by name**, before and after; neither task changes either, so
`test_pipeline.py` **955 / 0** and `test_harness.py` **295 total** on 3.9.6 are the expected
readings. Then `eval/results/` still 35 files at `d95d6d39…`, `eval/calibration/` 19 at
`a858fbd6…`, `eval/calibration/seed-0/draws/` still absent.

**Do not run the sweep**, and do not run `--verify-lock`'s failing path by touching the lock. The
180 draws are the user's to launch.

## New standing rule, from the near-miss

**Before every commit, read `git diff --cached` and confirm it is the text you wrote.** Staging by
explicit path protects against `git add .` pulling in a file you did not intend; it does **not**
check that the content at that path is still yours. A second writer replaced a file between your
read and your next edit, and had that replacement landed after your last write instead of before it,
you would have committed 158 lines of someone else's draft under your own message and the
"file has been modified since read" error would never have fired. The staged diff is the last point
at which that is catchable. Save this one to memory.

Related, and the user's to enforce rather than yours: one writer in `eval/prereg/` at a time.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest.
Keys via `getpass`/`input()` or the app UI only, never on a command line. `eval/results/` is
read-only. Do not regenerate `eval/tasks.lock`. Do not edit `DECISIONS_R3.md` or addenda A–H; add
addenda instead. Do not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU/ceiling-SIGKILL→
timeout coercion, or `test_signal_death_is_explained`. Do not "fix" the eight already-correct
mechanisms. Never cache provider responses keyed on `(prompt, model, params, provider)`.
**No rewrap script and no line-width pass on anything**, and no reflow of any committed prereg file.
Stage by explicit path; no `git add .`, no branch, no remote, no push, leave `git config` alone.
Grep each staged set for key-shaped strings and **read the hits** — the redaction fixtures are
legitimate. Leave the two vim swap files alone.

No check silently deleted or weakened; a rename or a split is fine and must be called one.

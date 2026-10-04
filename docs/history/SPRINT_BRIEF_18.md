# Sprint 18 — what the grid needs to consume a frozen spec, and a write guard that can prove it ran

Four things I verified in the live store since your report. Three correct one of your sentences or
close one of your open questions; the fourth is mine.

1. **The sweep is not quota-paced, and it is much slower than the count suggests.** 54 call events,
   every one `ok: true`, `attempts: 1`, `retried: 0`, `seconds_backoff: 0.0`, `seconds_paced: 0.0`,
   and zero `grade_timed_out` across all draws. There are **zero Gemini calls in this run** — the
   projection is `planner gemini 0 / executor groq 180` — so the daily quota cannot be what sets the
   rate. Per-draw `seconds` ran 2–10s through `ranking-01__d5`, then 369, 394, 304, 242, 254, 354,
   223, 269, 268, 259. That is Groq completion latency, not pacing and not the quota. At the
   last-ten mean the remaining 126 draws is about **8.7 hours**, not the two that 53-in-42-minutes
   implies. Nothing to fix; the sentence "roughly the rate the daily quota allows" is just wrong in
   both halves.

2. **Every draw so far has passed — 55 of 55.** `d_t = 1.00` on `byte-formatting-02`, `ranking-01`,
   `run-length-encoding-02` (tier 1), `grouping-02` and `interval-logic-01` (tier 2), plus 5/5 so
   far on `ranking-02`. Tier-1 ceiling was forecast from the pin replay; **two tier-2 tasks flat at
   the ceiling is new.** The D-1/D-3 consequence is the user's call and is not a task here.

3. **Your 19-plans-versus-18-tasks question is not a silent drop.** `query-canonicalization-02` is
   the one lock task in tiers 1–2 with no persisted plan, so any run including that family would
   have needed a Planner call and the projection would not have printed `planner gemini 0`. Nine
   families × 2 = the 18. When the sweep writes its manifest, confirm the selection from it rather
   than from this reasoning.

4. **All 19 plan files carry `spec` and no `tests` key at all, and that is deliberate.** I checked
   `import_pin_plans.py:42-45` before writing this: `calibration_plan` resolves no visible suite,
   calibration never gates, `one_draw` grades against the hidden suite, so carrying the probe's
   `tests` would give an imported plan a shape no drawn plan has. The ledger holds 859 characters of
   `tests` per plan and the import drops them on purpose. Recording it because it is one more
   already-correct mechanism, not a gap.

Run `pwd` first and confirm you are in `~/Desktop/Multi agent project`. **Zero provider requests.**
The sweep still owns `eval/calibration/` for roughly the next nine hours — do not touch it, do not
run or edit `eval/calibrate.py`.

## Task 1 — read-only: how a spec and a visible suite reach the grid today

D-20 is registered and **not implemented in `eval/run_eval.py`**. Before anyone writes that path, I
want the current shape on the record. Enumerate and paste a table with line numbers:

- Every function in `eval/run_eval.py` that produces a spec, and whether any of them can load one
  from disk. My understanding is that there is no spec-load path and the Planner is called per
  repeat; verify it rather than taking it.
- Where the **visible suite** comes from: the Planner reply's own TESTS block, the Test Writer, or
  an audit fallback — and which of those fire in which order. Give the function and the branch.
- What arm `a_prime3`'s best-of-3 gate reads, and what arm `b`'s repair loop reads, stated
  separately even if they resolve to the same call.
- Whether `plan_path` (`calibrate.py:225`) or anything shaped like it exists on the `run_eval` side.

**Enumerate the producers and read each one. Do not grep for a call spelling** — sprint 17 exists
because I did that, and `locked_tasks()` matched none of the three spellings I searched.

## Task 2 — state the gap, write no code

From Task 1's table, answer in your report, in prose, with line numbers:

1. Does freezing the spec freeze the visible suite? The 19 imported plans **cannot** supply one —
   `plan_sha256` is empty because the ledger kept the extracted spec and not the Planner's raw
   reply, so the TESTS block those specs were drawn with is not in the plan files.
2. So under D-20, what does arm `b` do per repeat for those 19 tasks — re-call the Planner, fall to
   the Test Writer, or something else? Give the realised Gemini call count per repeat under each
   branch, per role, and say which role pays.
3. D-20's precision claim is that holding the spec fixed removes Planner variance from all arms
   equally. Is that true of the visible suite as well, or silent about it?

**Stop there.** If the answer is that D-20 needs a suite policy as well as a spec policy, that is a
registration matter and it gets an addendum written from your table, not a patch written from your
judgement. Do not modify `eval/run_eval.py` in this sprint.

## Task 3 — land the write guard as `guard_writes.py`

Your `/tmp/s16_guarded_suite.py` is what made the concurrent runs safe, and it should not stay in
`/tmp`. Both design questions you raised, answered so you do not have to decide them:

**Where it lives:** repo root, as `guard_writes.py`, an **external wrapper** invoked explicitly —
`python3 guard_writes.py test_pipeline.py`. Not a hook inside either suite. Your worry was a guard
that can be silently stopped while the suite still passes; a wrapper cannot be, because the "0
writes" claim exists only when someone runs the wrapper and is absent rather than false when nobody
does.

**Deny-list:** the two named roots, `eval/calibration/` and `eval/results/`, not the whole tree —
the suites legitimately write temp dirs and a whole-tree deny becomes an allow-list that drifts. But
writes **outside** the deny-list must be counted and their distinct path prefixes printed, not
silently permitted, so an unexpected write target is visible without flooding a 960-check run.

**The requirement that makes it worth having:** it must prove it intercepted. A guard that fails to
patch reports "0 attempted writes" and means nothing — that is this project's own defect family in
guard form. So: a `--self-test` mode that attempts one `open` in write mode, one `os.open` with
write flags, and one `shutil`/`os` mutator against a path inside a denied root, asserts each was
blocked and recorded, and exits non-zero if any got through. Report the self-test result alongside
the suite result, and make the suite run refuse to print a clean verdict if the self-test did not
pass in the same process.

Cover `open` in write modes, `os.open` with write flags, and the `os`/`shutil` mutators, as yours
already did. Standard library only, 3.9.6, no pytest.

## Task 4 — one commit

`guard_writes.py`, `SPRINT_BRIEF_18.md`, staged by explicit path. `test_pipeline.py` only if the
guard needs a check of its own, and if it does, say so and name it. The message should say that the
guard moves from `/tmp` into the tree as an external wrapper with a self-test, and that no
registered rule moves and no `eval/` behaviour changes.

Read the whole `git diff --cached` before committing.

## Task 5 — confirm nothing moved

Both suite counts **by name**: `test_pipeline.py` from **960 / 0**, `test_harness.py` **295**.
`eval/results/` still 35 files at `d95d6d39f4…`. Run both suites under `guard_writes.py` and report
the self-test line as well as the write count — a write count without a passing self-test is not
evidence.

Live store as an **observation only**, not a digest and not a comparison: I read 55 draws and 54
`events.jsonl` lines at 16:37Z. Report what you see and whether the per-draw `seconds` is still in
the 200–400s band or has come back down.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest.
Keys via `getpass`/`input()` or the app UI only, never on a command line; the shell is **zsh**, so
`read -rsp` fails and the portable form is
`printf 'Key: ' && IFS= read -rs VAR && export VAR && echo`. `eval/results/` is read-only. Do not
regenerate `eval/tasks.lock`. Do not edit `DECISIONS_R3.md` or addenda A–J; add addenda instead. Do
not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU/ceiling-SIGKILL→timeout coercion, or
`test_signal_death_is_explained`. Do not "fix" the already-correct mechanisms, and the imported
plans' absent `tests` is now one of them. Never cache provider responses keyed on
`(prompt, model, params, provider)`. **Never grep a mechanism by its call spelling.** **No rewrap
script and no line-width pass on anything.** Stage by explicit path; no `git add .`, no branch, no
remote, no push, leave `git config` alone. Grep each staged set for key-shaped strings and **read the
hits**. Leave the two vim swap files alone.

No check silently deleted or weakened; a rename or a split is fine and must be called one.

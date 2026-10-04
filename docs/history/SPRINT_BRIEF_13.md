# Sprint 13 — register the reading rule, then stop trusting the selection

Sprint 12 is **verified and committed**. I ran the suites in a fresh copy (`test_harness.py` 293+2 =
295, `test_pipeline.py` 900/0, the +14 exactly your two new names), confirmed all four commits on `main`
with commit 1 byte-identical to the index I had already reviewed, and `eval/results/` still 35 files
hashing to `d95d6d39…` with nothing newer than 2026-08-31 23:35.

The stub guard is better than what I specified, and the reason is worth keeping: **absence as a third
state.** `record.get("stub")` would have collapsed a missing field into `None`, and `None` is a positive
claim that a live provider answered — so an unmarked stub draw written before the field existed would
have been resumed onto in silence. Reading the identity off `instrument.stub` with bare attribute access,
rather than off `args.stub`, is the "check who *writes* the field" lesson applied unprompted. That closed
the bug class, not the instance.

This sprint is two guards and one registration. **Zero provider requests.** The 180-draw sweep is queued
directly behind it, so nothing here may change what a sweep selects or measures.

Run `pwd` first and confirm you are in `~/Desktop/Multi agent project`.

## Task 1 — register D-16, in a new file, before the sweep runs

`eval/prereg/` holds A through E and nothing mentions D-16. Write
`eval/prereg/DECISIONS_R3_ADDENDUM_F.md`. Short — this is a reading rule for a partial measurement, not a
new gate. No code changes in this task. Do not edit `DECISIONS_R3.md`, `ADDENDUM_D` or `ADDENDUM_E`.

**What is about to be measured.** 18 of the 36 locked tasks: the nine families whose both variants have an
imported Planner spec — `aggregation`, `byte_formatting`, `date_arithmetic`, `grouping`, `interval_logic`,
`path_canonicalization`, `ranking`, `run_length_encoding`, `text_normalization`. 180 Executor draws, 0
Planner calls. The 19th imported task, `query-canonicalization-01`, is excluded because its family sibling
has no imported plan and including it would cost a real Gemini request; say that, so a later reader does
not read the exclusion as cherry-picking. Tier mix, verified against `eval/tasks.lock`: **tier 1 ×10,
tier 2 ×8, tier 3 ×0** — all ten tier-1 tasks, half of tier-2, none of tier-3.

**What D-16 registers.** Define `k` = the number of those 18 tasks whose `d_t` falls strictly inside
(`BAND_LOW`, `BAND_HIGH`) at `CALIBRATION_DRAWS = 10`, computed by exactly the code path D-1 uses —
`calibration_rate` over the graded denominator, per D-15. No extra filter, no re-definition.

State plainly that `k` is **not** the D-1 gate. D-1 (`DECISIONS_R3.md:28`) is `GATE_MIN_IN_BAND = 15` of
36. `k` is a strict subset and a biased one: tier 1 is where the pin replay clustered at 3/3 (29 of 30
passes), i.e. out-of-band **high** at `d_t = 1.0`, so a low `k` is the expected result on this half and is
not evidence against D-1.

Register the arithmetic before the number exists. The unmeasured 18 are 8 tier-2 plus 10 tier-3, and they
must supply `15 − k`:

| k | needed from the unmeasured 18 |
|---|---|
| 0 | 15 of 18 (83%) |
| 3 | 12 of 18 (67%) |
| 6 | 9 of 18 (50%) |
| 9 | 6 of 18 (33%) |

And these three readings:

- **k ≤ 2** — D-1 fails on the measured half's own terms. Go to D-3 before spending further Gemini quota
  on completing the pin.
- **k = 3–6** — undetermined, and the gate hinges entirely on tier 3, which has never been measured at
  all: 0 of 10 tasks in the pin. The 17 remaining Gemini requests are then spent on the **ten tier-3 tasks
  first**, not spread across the unfinished families. This is the operative consequence of D-16 and it
  reverses the natural instinct to finish what was started.
- **k ≥ 7** — completing the pin to 36 is worth the 17 requests.

**Residual to state, not to resolve.** At 10 draws, in-band means 1 to 9 passes. An infra loss leaves the
denominator, so a task with `graded < 10` has a wider effective band than one with `graded = 10`. D-1 is
silent on this. Record that `k` is computed with **no minimum-denominator filter**, and that any task with
`graded < 10` must be reported beside its graded count so a reader can see which ones got the wider band.
Do not invent a minimum here.

**Void condition.** If the run's own projection does not read 18 tasks, planner gemini 0, executor groq
180, it is not the run D-16 describes and D-16 does not apply to it.

## Task 2 — a `--family` or `--tier` value that matches nothing must refuse

Your find, and I reproduced it before writing this. It is worse than the hyphen case you hit:

| passed to `--family` | tasks selected |
|---|---|
| the nine underscored family IDs | 18 |
| the same nine hyphenated | 6 |
| eight correct plus one bogus name | **16** |

That third row is the dangerous one. The projection then prints `= 16 task(s)` with `planner gemini 0`
still true, and anyone skimming that line for the zero passes it. Fourth instance this sprint of one
shape — trusted input — this time in the selection rather than the store.

Refuse, exit 2, and name both the unmatched values and the valid IDs, since the failure mode is a reader
who does not know that families are underscored while task IDs are hyphenated. `--tier` gets the same
treatment for a value outside the locked set. It belongs in or immediately after `_select`, before
anything reads the disk, because a selection error is not a store fact and should not wait behind one.

**Do not normalise hyphens to underscores.** A silent fixup is the same defect wearing a helpful face: it
would make `--family byte-formatting` work today and quietly select something else the day a family ID
legitimately contains a hyphen. Refuse and print the correct spelling.

## Task 3 — `--gate-only` demands one identity even though it has no current one

You are right that gate-only returns before `stub` exists, so there is nothing to compare a stored draw
against. There is still a check available: require the stored draws to be **internally** of a single
identity and refuse a mixed store without needing to know which kind it should have been. Reuse
`recorded_stub_identity` and the same refusal posture; name both identities and their counts. A verdict
computed half from stub answers is the failure this whole sprint exists to prevent, and gate-only is the
one path that still reaches it.

## Task 4 — prove the queued sweep still selects what it selected

After Tasks 2 and 3, run the nine-family selection at `--draws 10 --per-family 2` and paste the
projection. It must still read **18 task(s), planner gemini 0, executor groq 180, executor model
`openai/gpt-oss-120b`**. A stricter selection that breaks the exact command about to be run would be a
self-inflicted wound, so this is the check that matters more than the counts.

Run nothing that spends a request. Do not run the sweep — it is the user's to launch.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest. Keys via
`getpass`/`input()` or the app UI only, never on a command line. `eval/results/` is read-only. Do not
regenerate `eval/tasks.lock`. Do not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the
SIGXCPU/ceiling-SIGKILL→timeout coercion, or `test_signal_death_is_explained`. Do not "fix" the eight
already-correct mechanisms. Never cache provider responses keyed on `(prompt, model, params, provider)`.
Stage by explicit path; no `git add .`, no branch, no remote, no push, leave `git config` alone. Grep each
staged set for key-shaped strings and **read the hits** — the redaction fixtures are legitimate. Leave the
two vim swap files alone.

Report both suite counts **by name** before/after. No check silently deleted or weakened; a rename or a
split is fine and must be called one. Commit Task 1 separately from Tasks 2–3.

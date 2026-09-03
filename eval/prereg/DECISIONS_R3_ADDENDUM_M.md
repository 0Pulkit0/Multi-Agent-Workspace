# DECISIONS_R3 — Addendum M

**Decision D-24. Written 2026-09-04 by the measurement reviewer, after a full read of `DECISIONS_R3.md`
and addenda A–L against the state of the tree.**

M is a bookkeeping addendum. It records facts about draws that have already been taken and about
obligations that earlier addenda registered and nobody discharged. It is written now, before the sprint
that builds D-3's difficulty dial, because two of its items bear on **which draws a future lock could
legitimately be compared against** — and that question cannot be answered honestly after the comparison
has been made.

**What M does not do.** It authorises no source edit. It does not implement D-3's remedy, which L §7
left unlicensed. It amends nothing in K or L; where it corrects a description, the correction is stated
here and the committed text is left exactly as written, per the standing rule that a registration which
changes after it has been read is not a registration.

---

## §1 — The measured calibration store, stated once and authoritatively

Two different readings of "which 26 tasks were measured" are in circulation, and one of them is wrong.
This section fixes it. Recounted 2026-09-04 directly from the 260 files in
`eval/calibration/seed-0/draws/`, keyed against `eval/tasks.lock`, not from any run summary.

- `eval/tasks.lock` holds **36 tasks**, tiers sized **10 / 16 / 10**.
- **260 draw files. 26 tasks, every one at exactly 10 draws. All 260 read `outcome: "graded"`.** Zero
  `infra_loss`, zero partial tasks.

| tier | in lock | measured | unmeasured |
|---|---|---|---|
| 1 | 10 | **10** | 0 |
| 2 | 16 | **9** | 7 |
| 3 | 10 | **7** | 3 |

The ten unmeasured tasks are `config-parsing-02` (t3), `query-canonicalization-02` (t2),
`template-expansion-01` (t3), `template-expansion-02` (t3), `tiered-pricing-01` (t2),
`tiered-pricing-02` (t2), `validation-01` (t2), `validation-02` (t2), `version-ordering-01` (t2),
`version-ordering-02` (t2).

**The reading to be discarded is "the 26 measured are tiers 1 and 2, and the 10 unmeasured are tier 3."**
It is false in both halves. Seven tier-3 tasks carry ten graded draws each, and seven of the ten
unmeasured tasks are tier 2. Anyone reasoning about tier-3 coverage from the count of unmeasured tasks
will get the wrong answer, and §2 and §8 both turn on this.

---

## §2 — D-16 is void by its own stated condition, and nothing recorded it

Addendum F closed D-16 with an explicit escape clause, at `DECISIONS_R3_ADDENDUM_F.md:144-145`:

> **If the run's own projection does not read 18 tasks, `planner gemini 0`, `executor groq 180`, it is
> not the run D-16 describes and D-16 does not apply to it.**

That condition fired, on all three terms. The run that produced the store measured **26 tasks, not 18**;
it includes **seven tier-3 tasks** where F's own tier table puts tier-3 coverage at zero; it includes
`query-canonicalization-01`, which F excludes by name; and it **spent Gemini** on seven freshly drawn
specs against a projection of `planner gemini 0`.

**Registered: D-16 does not apply to the calibration store and is void.** Its statistic `k` is reported
nowhere in G through L, and that is now correct rather than an omission — there is no run for it to be
reported against.

**What survives.** F's tier table is a property of `eval/tasks.lock`, not of the projection, so
G's use of it stands. Only D-16's own conclusion is void.

**How this happened, because the shape will recur.** F wrote a conditional whose antecedent was a
printed projection, and the projection was correct when written. The run then resumed, extended, and
drew specs the projection had not priced. Nobody re-read the antecedent. **A registration conditioned on
a run's own forecast has to be re-checked against the run's actual shape at the moment the result is
read, and no earlier.**

---

## §3 — D-22's environment split covers 90 draws on nine tier-1/2 tasks, not 30 on three tier-3 tasks

This is the most audit-relevant item in M.

K §2 registered the retry-layer collapse and scoped its exposure, at
`DECISIONS_R3_ADDENDUM_K.md:36-37`:

> The before column is the environment all 170 existing draws were taken in. The after column is the
> environment the remaining 30 draws will be taken in.

Those 30 draws were the three tier-3 tasks K was written ahead of. **They were never drawn** — L §9
declined the remaining calibration. Recounted from the draw files, the D-22 fields
(`http_attempts`, `seconds_sleeping`) are present on **90 draws across nine tasks**:

| task | tier | draws under the new environment |
|---|---|---|
| `aggregation-01` | 1 | 10 |
| `byte-formatting-01` | 1 | 10 |
| `text-normalization-01` | 1 | 10 |
| `text-normalization-02` | 1 | 10 |
| `date-arithmetic-01` | 2 | 10 |
| `interval-logic-02` | 2 | 10 |
| `path-canonicalization-01` | 2 | 10 |
| `path-canonicalization-02` | 2 | 10 |
| `query-canonicalization-01` | 2 | 10 |

**So the actual exposure is three times the registered figure, on tasks of a different tier, and not one
of the nine is a tier-3 task.** K's description of the split is wrong as to which units it covers. K's
text stands unedited; this section is the correction.

**The consequence that matters.** `path-canonicalization-01` and `path-canonicalization-02` are two of
the **three** tasks in the band, and both were drawn under the post-K environment. The store is
therefore **not** environment-homogeneous across the in-band set: two of three in-band tasks sit in the
after column and `delimited-parsing-01` sits in the before column.

**Registered consequences.**

1. **D-1's failure does not depend on this.** The gate arithmetic is 3 in band + 10 unmeasured = 13
   against `GATE_MIN_IN_BAND = 15`, and it fails for every assignment of environments to draws. L's
   conclusion is unaffected and is not reopened.
2. **Any future comparison against this store must carry the split.** A new lock's calibration compared
   against these 260 draws is comparing across a retry-environment change that straddles the in-band
   set. The split is to be stated in that comparison, not discovered afterwards.
3. **K §7's reopening trigger is unrunnable as written** — it conditions on "any `infra_loss` record
   appearing in the remaining 30 draws", and there are no remaining draws. Recorded here as spent
   rather than pending. Its substance is discharged anyway: all 260 draws read `graded`.

---

## §4 — D-20's three unmet obligations, and D-21 never ran

Addendum I registered three checks on the spec-freezing policy. None is evidenced in J, K or L.

1. **The lock verification, and it was violated in sequence.** `DECISIONS_R3_ADDENDUM_I.md:88`:
   "before any of the 17 remaining specs is drawn, run `python3 eval/gen_tasks.py --verify-lock` and
   record its output." Seven of those specs were subsequently drawn — the store went from 19 imported
   plans to 26 measured tasks — and **no recorded pass appears anywhere.** The check may well have
   passed; the registration required the record, and the record is what is missing.
   *(Note for whoever discharges it: `eval/gen_tasks.py` is off limits to edits under L §10, and running
   `--verify-lock` is a read. The constraint does not block the check. Use `./venv/bin/python`.)*
2. **The repeat-independence guard**, `I:207`: "task's three repeats and hash the three candidates;
   three distinct hashes is the pass." No evidence it ran. It is a grid-shaped check and the grid never
   ran, which is a reason it lapsed but not a discharge.
3. **Per-role Gemini accounting**, `I:156`: "**Registered: Gemini calls are counted per role and
   reported**." K §5 reports 13 Gemini events as a single figure. The split by role is not reported.

**D-21, the off-grid Planner probe, never ran.** Its registered permission was to run after the endpoint
lock; no endpoint lock exists on this task set and none will, so the permission has nothing left to
operate on. **Registered: D-21 is retired unexercised.** Any future Planner-variance bound is a fresh
registration, not a resumption of D-21.

**Registered disposition for §4:** items 1 and 3 are cheap and are owed by the party running the tree
before any new calibration store is written. Item 2 and D-21 are retired with the grid.

---

## §5 — Two registered numbers that were never reported, and one that cannot be

- **D-7's Planner compliance rate.** `DECISIONS_R3_ADDENDUM_A.md` registered that calibration output
  carries the Planner's PEP 604 compliance rate as a number. It is reported in neither K nor L.
  `scan_runtime_syntax` is report-only over `run.spec` and the instrumentation exists; the number was
  simply never extracted. **Owed, and cheap — it is a pass over specs already on disk, at zero API
  cost.** It must be labelled as a measurement of the *Planner*, never as artifact incidence, because
  it cannot see syntax an Executor invents.
- **D-12's effectiveness.** `DECISIONS_R3_ADDENDUM_C.md:75-76` defines it as the D-10 wrong-suite count
  over the four exposed rows. D-10 is a grid instrument. **Registered: D-12's remedy stands as a prompt
  rule and its effectiveness is unmeasured and unmeasurable on this task set.** It is not left open,
  because leaving it open implies a measurement is pending that nothing can produce.

---

## §6 — Two dead sequences and one stale pointer

- **`DECISIONS_R3.md:143-147`'s run order is unreachable and is recorded void:** "pin the Executor, run
  the calibration sweep, evaluate the D-1 gate, apply D-3 if it fails, **read the floor-task specs**,
  then freeze D2, then a k=1 smoke run that is discarded, then k=3." The floor is **F = 0**, so there are
  no floor-task specs to read, and every step after it was conditional on a gate that failed.
- **`DECISIONS_R3_ADDENDUM_B.md:147-153`'s 180-request schedule is spent** and superseded by the
  measured tier-split RPD: the Planner slug carries 20 requests/day, 19 usable. Recorded void.
- **Stale pointer, corrected here rather than in place:** `DECISIONS_R3.md:33-34` cites
  `GATE_MIN_IN_BAND = 15` at `eval/calibrate.py:51-69`. It is at `:109`. H's errata block corrected four
  of D-17's pointers and missed this one. L cites the correct line.

---

## §7 — The source still advertises a registration debt that D-23 closed

`eval/calibrate.py:100-108` states that restating the 12-of-30 gate for a 36-task lock "is a
registration decision the user still owes", and `GATE_AS_REGISTERED` reads "fewer than 12 of 30 in-band
tasks is a NO-GO". **D-23 §4 closed that debt** and registered "No code change" — correctly, because the
code had already chosen 15 and documented it as the most conservative reading of the registered gate.

The effect is that a reader of the source sees an open debt that the registration record has closed.
**Registered: the debt is closed; the source text is stale prose, not a live obligation.** The permitted
remedy is a comment and docstring correction citing D-23. **`GATE_MIN_IN_BAND` itself must not be
edited** — it is 15, it was chosen before the results, and changing it now is the widening move
`D1_RESTATEMENT_PRECOMMITMENT.md` refuses.

---

## §8 — The pre-commitment's own entry condition for a new lock is satisfied, by arithmetic

The branch that fired — `k = 0` at `n = 30` — carried two clauses beyond "D-1 fails cleanly", at
`D1_RESTATEMENT_PRECOMMITMENT.md:99-103`:

> - **The grid does not run on tier 1–2.** Restrict to tier 3. […]
> Threshold: **tier-3 calibration returning fewer than 5 of 10 tasks in band** triggers that
> conversation.

Both are resolved here, and neither needs a further draw.

**The tier-3 restriction is retired**, as L §6a recorded: the only double-in-band family is
`path_canonicalization`, which is **tier 2**, so restricting to tier 3 would discard the strongest
evidence the store contains. M adds the count that makes the retirement unarguable rather than
preferential.

**The 5-of-10 threshold is met, and it cannot be un-met.** Tier 3 stands at **7 of 10 measured with
exactly one task in band** (`delimited-parsing-01`, `d_t = 0.60`; the other two in-band tasks are the
tier-2 `path_canonicalization` pair). Three tier-3 tasks are unmeasured. **Maximum achievable tier-3
in-band count is 1 + 3 = 4, against a threshold of 5.** Drawing the remaining three tier-3 tasks cannot
change the disposition, so no Gemini need be spent to settle it.

**Registered: the pre-commitment's condition licensing "a new `tasks.lock` and a new registration" is
satisfied.** This matters because it is an *independent* licence from D-3's. D-3 fires on the whole-store
ceiling; the pre-commitment fires on tier 3 specifically, and it was written before any of these numbers
existed. **A new lock is therefore authorised by a threshold fixed in advance, which is the strongest
form of authorisation available here.** What remains unlicensed is L §7's separate question of whether a
harder *suite* counts as a "harder parameter" — that is untouched by this section.

---

## §9 — The Planner slug decision is unmade and it sequences ahead of any future Planner call

`MSG_TO_MEASURER_NEXT.md:176-178` records that the choice between `gemini-3.6-flash` and
`gemini-3.5-flash-lite` "is the user's, it is not made, and K must not assume either answer".
`agents_core.py:83` is still `gemini-3.6-flash`. `D1_RESTATEMENT_PRECOMMITMENT.md` states the only
sequencing constraint it contains: the slug resolves **before** any tier-3 Planner calls, not after, and
it binds in every branch.

**Registered here so it is not lost between addenda: no Planner call for a new lock is drawn until the
slug is decided.** The two candidates differ by roughly a factor of five in measured daily requests
(20/day for the flash tier, 111 of 111 observed on lite), which is the difference between a multi-day
stage and a single-session one — so the decision is a schedule decision as much as a model decision.

If lite is adopted, that is **one** addendum covering the slug **and** the one-spec-per-task policy
together, because D-20's justification for one spec per task appealed in part to pacing, and
`DECISIONS_R3_ADDENDUM_J.md` already withdrew one of its justification clauses. Adopting lite without
re-justifying or flipping that policy would leave D-20 resting on a premise that no longer holds.

**Not licensed by this section:** probing `gemini-3.6-flash`'s RPD cap, and multiple Google accounts.
Both are refused elsewhere and M does not disturb either refusal.

---

## §10 — Bookkeeping carried forward

- **The generator census correction.** The read-only survey of `eval/gen_tasks.py` reported difficulty
  literals in a form that was subsequently corrected to **15 varying knobs, 10 fixed, 25 names total**.
  Recorded here so the corrected figures are the ones a dial sprint reads. **No source change is
  implied** — no docstring in the tree cites these counts, so there is nothing to bring into agreement.
- **L's three deferrals are inherited unchanged**, and M licenses none of them: §7's harder-suite lever
  is not yet licensed; §8 leaves `code` persistence for a future calibration store explicitly unsettled
  and to be decided **before** that store is written, not after; §9's one-variant-per-blind-family
  exception is available to the sprint that builds the dial and not before, and requires the one-variant
  calibration to be declared in the new lock's manifest.

---

## §11 — Verification statement, and the limits of this addendum

**Verified by me on 2026-09-04, by reading the artifacts rather than any run summary:** the 36-task lock
and its 10/16/10 tier sizes; 260 draw files; 26 tasks at exactly 10 draws each; all 260 at
`outcome: "graded"`; the per-tier measured counts in §1; the ten unmeasured task names and their tiers;
and the nine-task, 90-draw extent of the D-22 fields in §3, including that both `path_canonicalization`
tasks fall inside it. Every quotation in §2, §3, §6 and §8 was read at the cited line in the cited file.

**Not verified, and stated as unverified:** whether `--verify-lock` was ever run without its output being
recorded (§4.1) — absence of a record is not evidence of absence of a run; and whether the repeat
independence guard (§4.2) was run informally.

**M authorises no source edit.** The only code-facing item is §7's comment correction, and it is
permitted, not required. `eval/results/` stays read-only; no field is backfilled onto any existing draw
file; `eval/tasks.lock` is not regenerated by this addendum and `eval/gen_tasks.py` is not edited.
`GATE_MIN_IN_BAND`, `BAND_LOW`, `BAND_HIGH`, `MAX_REVISION_ROUNDS`, `ESCALATION`,
`EXEC_TIMEOUT_SECONDS` and the Executor prompt are all untouched and unlicensed here.

**Next free letter is N. Next free decision number is D-25.**




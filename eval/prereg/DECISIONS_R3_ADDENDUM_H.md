# Pre-registration addendum H to Revision 3

**Written 2026-09-01, after addendum G was committed as `5c1a25b` and before the first calibration
draw is spent.** Separate file rather than an edit to G, for the reason addendum A gave and B, D, E
and F repeated: a registration document that changes after it has been read is not a registration.
G's preamble made that rule binding on itself from the commit that carried it, and named the
consequence in advance — any later change to D-17 or D-18 needs its own addendum. This is that
addendum, arriving one commit later. `DECISIONS_R3.md` and addenda A through G are left exactly as
they were written.

Scope: adds **D-19**, which is three clause-level replacements to D-18 plus an errata block.
**D-18 stands except where a clause below replaces it, and D-17 stands entire.** Nothing here
changes the registered substance of either item: not the 1..`MAX_REVISION_ROUNDS`+1 axis, not 0 as an
absent state, not the four groups, not the approved-only breakdown, not the cumulative
approved-by-round curve, not D-17's instrument-rather-than-judge pair, not the compression reading of
contamination. What changes is three *reporting* rules that would each have produced a number no
reader could interpret, and four citations that pointed at the wrong line.

Two of D-18's registered identities also stop being prose and become checks in `test_pipeline.py`.
No change to `agents_core.py`, `eval/run_eval.py` or `eval/calibrate.py`, and none is needed: both
identities already hold today. The point is that nothing would have noticed if they stopped.

## H binds the grid, not the calibration sweep

`eval/calibrate.py` contains no reference to `retained_round`, `repair_rounds`, `final_round_worse`,
`_verify_step`, `_retain_best`, `MAX_REVISION_ROUNDS` or `ESCALATION`. A draw is
`run_eval._draw(plan["spec"], keys)` followed by `run_eval.grade` — one Executor shot from the stored
spec, graded by the hidden suite, with no repair ladder anywhere in it — so no draw can carry a field
D-18 or D-19 reads. The 180 draws may be launched before or after this addendum lands, and neither
affects the other. Nothing here changes what a sweep selects or what it measures.

---

## D-19. Three corrections to how the repair rounds are read

**Registered: the three clauses below replace the D-18 clauses each one names. Everything else in
D-18 stands. All three are reporting rules over fields the records already carry; none adds a field,
none changes code, and none enters the primary endpoint or any test.**

1. `final_round_worse` is reported over the cells where it was computed *and* had something to
   compare against, not as a rate over arm-B cells (replaces D-18's line 249).
2. The rung label belongs to the approved group; the exhausted group's rung is not recoverable
   (replaces D-18's lines 166–168).
3. The unit of the axis is the last graded *step* of a cell, not the cell (replaces D-18's use of
   "arm-B cells" as the unit throughout, at its lines 139, 185, 204, 214 and 218).

### D-19.1 — `final_round_worse` has no denominator over all arm-B cells

*Replaces D-18's line 249: "`final_round_worse` is reported as a rate over arm-B cells."*

**Registered: `final_round_worse` is reported as a count over the arm-B cells where `_retain_best`
ran and a round later than the retained one was graded. On the record that is `verdict !=
VERDICT_UNVERIFIED` and `repair_rounds + 1 > retained_round`. Both excluded counts are reported
beside it, so the all-cells version stays recoverable and the choice is visible rather than
assumed.**

Four distinct meanings currently arrive at the same `False`, and a rate over all arm-B cells averages
them:

| `False` means | where it comes from |
|---|---|
| measured, and the last round was not worse | `_retain_best` at `agents_core.py:2697` |
| never computed — the `VERDICT_UNVERIFIED` exit returns without retention | `agents_core.py:2779` |
| no graded step in the cell at all | `eval/run_eval.py:770`'s `else` branch |
| nothing later to compare against | `_candidate_merit(last) < _candidate_merit(best)` with `last is best` |

`_retain_best` is the field's only writer (`agents_core.py:2697`) and its default is `False`
(`:2173`), so the second and third rows are the dataclass default and the reader's fallback surfacing
as if they were measurements. This is the absence-collapsed-into-a-value shape the stub-identity
guard closed in `eval/calibrate.py`, reappearing in a reporting rule rather than in code.

The fourth row is the sharper half, because it makes the field a **constant** on the approved group.
The passing candidate is appended at `:2744` and the loop returns at `:2767` before any later round
exists, so on an approved cell the last candidate *is* the best candidate and
`_candidate_merit(last) < _candidate_merit(best)` compares a value with itself. `True` is reachable
only in the exhausted and provider-error groups. A rate over all arm-B cells therefore moves with the
approval rate — more approvals, more structural `False` — and not with the thing the field exists to
measure, which is the ladder being net-harmful.

The denominator is stated in stored fields rather than in terms of the loop, because that is what a
reader can check. `verdict` (`eval/run_eval.py:757`) is `VERDICT_UNVERIFIED` exactly on the two
groups where `_retain_best` never ran: the loop returns immediately on an unverified candidate
(`:2771-2779`), so no exhausted or provider-error cell can carry that verdict, and a cell with no
graded step is written as `VERDICT_UNVERIFIED` by the same reader's `else` branch. And the last round
the ladder graded is `repair_rounds + 1` in every group, since `record.rounds` is written only at
`agents_core.py:2810`, one line after a revision call succeeds, and the round it produces is graded
at the top of the next iteration. So `repair_rounds + 1 > retained_round` is exactly "a round later
than the retained one was graded", and it excludes every approved cell without naming the approval —
which is the same conclusion as the paragraph above, reached without restating the ranking.

This is D-18's own point 3 — no pooling of the four groups — applied to the one field that escaped
it. D-18 split `retained_round` by group and left `final_round_worse` pooled across all four.

### D-19.2 — the rung label belongs to the approved group

*Replaces D-18's lines 166–168: "`escalation` (`eval/run_eval.py:760`) records which rung produced
the retained attempt, or `""` when the cell passed at round 1. So each stratum is labelled by its
rung as well as its number."*

**Registered: strata are labelled by rung on the approved histogram only, where `escalation` is the
rung that produced the retained attempt and equals `ESCALATION[retained_round - 2]` for
`retained_round >= 2` and `""` at round 1. The exhausted histogram is labelled by round number alone,
and the rung that produced its retained attempt is registered as not recoverable from the record.
This is a limit to state, not a field to add. For the unverified and provider-error groups
`escalation` is reported as a count by literal value and not read as a rung.**

That sentence is true of one group out of four, and it sits in D-18's axis section — ahead of the
group split — so it reads as covering all of them. Two code lines make it false elsewhere.
`record.escalation = "exhausted"` at `agents_core.py:2781` **overwrites** the rung on the way out of
the ladder, and `record.escalation = rung` at `:2811` runs only after the revision call at
`:2801-2802` has already returned. So:

| group | what `escalation` holds | is it a rung? |
|---|---|---|
| approved | `ESCALATION[retained_round - 2]`, or `""` at round 1 | yes, and it is the retained attempt's own rung |
| exhausted | the literal `"exhausted"` | no — the rung was overwritten at `:2781` |
| unverified | the last rung that succeeded, or `""` at round 1 | it produced the code, not the retained choice |
| provider-error | the last rung that *succeeded* | yes, but not the rung whose call failed |

The exhausted row is the one that costs something, and it costs it in exactly the wrong place: the
exhausted histogram is where "which rung produced this" is the question, because those are the cells
the ladder failed to rescue, and it is the one histogram whose rung is gone. Round number survives
there and rung does not, so the exhausted group is labelled by number and the write-up says why.

Not recoverable rather than merely absent. An exhausted cell's `retained_round` is an argmax over
`_candidate_rank`, so it can be any of 1..4, while `escalation` has been overwritten with a constant;
`repair_rounds` is `MAX_REVISION_ROUNDS` for every exhausted cell and so carries no information about
which rung the retained round came from either. There is no arithmetic over stored fields that
returns the rung. Adding a field would fix that and is refused here: it would change what a cell
records, which is a code change, in a sprint that registers a reading rule.

For the provider-error group the distinction is worth stating because it inverts the natural reading.
`escalation` names the rung that produced the last round *graded*, not the rung whose call raised —
the failing rung is chosen at `:2792` and never written to the record, since `:2811` is below the
`except` at `:2803`. A reader who takes the field as "the rung that broke" reads it exactly backwards.

The code never claimed otherwise: the dataclass comment at `agents_core.py:2158-2160` already defines
the field as the last rung of `ESCALATION` used on this step, or `"exhausted"` if the ladder ran out.
The correction is to the registration, not to the code, and no code changes on this account.

### D-19.3 — the unit is the last graded step, not the cell

*Replaces D-18's use of "arm-B cells" as the unit of the axis, at its lines 139, 185, 204, 214 and
218.*

**Registered: the axis and every field D-18 reads are properties of the last graded step of a cell —
`graded[-1]` where `graded` is the cell's steps filtered on non-empty code (`eval/run_eval.py:751-752`)
— and not of the cell as a whole. The `steps` and `graded_step` distributions are reported beside
every by-round table, so a reader can see how often step and cell coincide. Registered in advance:
the first time a cell reports `steps > 1`, step and cell stop coinciding and the write-up must say so
in the same table. No new field; both are already on the record.**

Every field in D-18's reading is read off that one step:

```python
graded = [step for step in run.steps if (step.code or "").strip()]
step = graded[-1] if graded else None
```

`verdict`, `pipeline_says_passed`, `repair_rounds`, `escalation`, `failure_kind`, `retained_round`,
`final_round_worse`, `provider_last` and `code` all come from `step` (`eval/run_eval.py:757-774`).
`pipeline_says_passed` is that step's `verified` property — `self.verdict ==
harness.VERDICT_APPROVED`, `agents_core.py:2179-2181` — and so is a claim about one step, while the
whole-run count lives in the separate `steps_approved` (`run.verified_count`, `:766`). `graded_step`
(`:767`) names which step the axis came from and `steps` (`:765`) is `len(run.steps)`.

So "the fraction of arm-B cells approved at the gate by round 2" is, exactly, the fraction of cells
whose *last graded step* was approved at the gate by round 2. On a one-step cell those are the same
sentence. On a three-step cell they are not: two steps' repair histories are absent from the axis
while `steps_approved` still counts them, and a cell whose first two steps needed three rounds each
and whose last passed first-try reports `retained_round = 1`.

Today the distinction is a nullity **in the recorded data**, and it is a nullity there rather than in
the pipeline. On the only arm-B data that exists — the 8 cells in `eval/results/seed-0/arm-b/` —
`steps` and `graded_step` are both 1 in all 8. Seven are `retained_round = 1`, `repair_rounds = 0`,
`escalation = ""`; the eighth (`run-length-encoding-01__r0`) is `retained_round = 2`, `repair_rounds =
1`, `escalation = "repair"`, which satisfies both of D-18's registered identities and is the only
repair round in the corpus.

The pipeline itself already runs two-step plans, and the check written for this addendum exercises
one: the unverified run in `test_a_graded_step_always_carries_a_round` leaves **two** graded steps,
both carrying code, and the reader's filter takes the second. The Planner writes the step list,
`run.steps` is however many it wrote, and no guard anywhere caps it at one — those 8 cells drew
one-step plans, which is a fact about eight Planner replies and not a property of the grid. So the
trigger is registered now rather than decided when the first two-step cell appears, and the check
asserts the axis is the last graded step on a run where *last* and *only* differ.

One note on those 8 cells that changes no rule: they carry `tests_trusted` and `tests_status` but not
`gate_available`, which was added later. D-18's cumulative-curve denominator names `gate_available ==
True`, so it is evaluable on grid cells and not on these; `gate_available` is `bool(run.tests_trusted)`
at `eval/run_eval.py:763`, the same quantity under a newer name, and no rule here depends on which
name the pilot records used.

### The two identities that are now checks

D-18 registered two identities as free consistency checks and left them in prose, where nothing
enforces them. Both are now asserted in `test_pipeline.py`, driving the real `_verify_step` through
the fixture the existing retention checks use:

1. **`test_an_approved_cell_satisfies_the_registered_round_identity`** — for an approval at each of
   rounds 1, 2, 3 and 4: `final_round_worse` is `False`, `retained_round == repair_rounds + 1` (D-18's
   lines 206–210), and `escalation` is the rung D-19.2 registers for that round. The identity follows
   from `rounds` defaulting to 0 (`agents_core.py:2141`) and being written only at `:2810`, after a
   revision call succeeds; the constant `False` follows from the approved exit, per D-19.1. Asserting
   it at all four rounds rather than one is the point — a single round-2 case cannot distinguish the
   identity from a coincidence.
2. **`test_a_graded_step_always_carries_a_round`** — `retained_round` is in 1..`MAX_REVISION_ROUNDS`+1
   for every graded step of an approved, an exhausted and an unverified run, because `:2743` precedes
   every exit; the unverified run also carries `final_round_worse` `False` with `_retain_best` never
   having run, which is D-19.1's second row observed rather than argued; that same run has two graded
   steps, so D-19.3's unit is exercised rather than only registered — the check asserts the axis is
   the run's *last* graded step; and a run whose Executor returns no code block leaves a step the
   reader's filter drops, which is where 0 comes from.

Neither check needed `_candidate_rank` or `_retain_best` restated inside it — both identities are
comparisons between stored fields — so neither was skipped and neither identity is left in prose here.
The one thing deliberately *not* asserted is the mechanism behind the constant, that the approved
exit's last candidate is its best candidate: an assertion of that would have to rank the candidates
itself and would prove the restatement rather than the code.

### Errata. Four pointers, and no rule moves

**This block changes no rule.** It corrects four citations in G that point at the wrong line. Each was
checked by opening the line. Nothing registered in D-17 or D-18 depends on any of them, and no later
reader should take a corrected pointer here for a moved rule.

- **"(Revision 2, Part 2)"** at G's line 113 and line 238, for the 11-level `d_t` covariate. Revision
  2's parts are lettered, not numbered — A, B, C, C2, C3, D, E, F, G — and the covariate is **item 2
  of Part A**, `EVAL_PREREGISTRATION_REVISION_2.md:19-22`, with "a continuous `d_t` with 11 levels" on
  `:21`. The mislabel is worse than a dangling pointer because "Part 2" resolves: it is the
  Amendment's metric contract, `EVAL_PREREGISTRATION_AMENDMENT.md:79`, which G cites correctly at its
  own line 26. A reader following it lands somewhere real and wrong. Line 113 sits in D-17 item 3 and
  line 238 in D-18, so the same mislabel is in both items.
- **Six harness failure kinds** at G's lines 53–54; there are **ten** (`harness.py:1059-1068`). The
  four missing are `FAIL_TIMEOUT_TESTS`, `FAIL_TIMEOUT_TEARDOWN`, `FAIL_OUTPUT` and `FAIL_PATH`. The
  list is completed rather than marked partial, for two reasons: `FAIL_PATH` is `"harness-path"`, an
  `import solution` failure that is a harness bug rather than a code defect, and so is the unverified
  group's own category; and `_candidate_rank`'s element 3 (`agents_core.py:2645`) splits
  `FAIL_ASSERTION` from all nine others, so "how many kinds are there" is load-bearing for the
  ranking D-18 reads. The four timeout kinds are grouped as `TIMEOUT_KINDS` at `harness.py:1072-1073`.
- **The refusing lock guard** at G's lines 34–35, cited as `eval/run_eval.py:1607`, which is the first
  line of the comment above it (`:1607-1614`). The guard is `gen_tasks.verify_lock_or_reason` at
  `:1615-1616`, and it refuses in two places: an unreadable or absent lock at `:1617-1619`, and a
  digest mismatch at `:1620-1630`. What D-17 claims of it — enforced before a single call is spent,
  and refusing rather than warning — is unaffected.
- **The width comment** at G's line 41, cited as `eval/run_eval.py:884`. The comment runs `:882-888`;
  the clause G quotes, that `gate_tests_sha256` "is the *visible* suite's short id and a different
  thing entirely", begins at the end of `:885` and ends at `:888`. Line 884 is the middle of the
  comparability sentence.

### What this does not license

1. **No reopening of D-17 or D-18.** Their registered substance is unchanged, and the three clauses
   above are the only clauses of D-18 that H replaces. G is left exactly as committed at `5c1a25b`.
2. **No new field, anywhere.** Specifically not a rung field for the exhausted group, which D-19.2
   names as unrecoverable and refuses to make recoverable, and not a per-step round axis.
3. **No change to `MAX_REVISION_ROUNDS`, `ESCALATION`, `_candidate_rank`, `_retain_best` or
   `_verify_step`.** 3 and `("repair", "alternate", "fresh")` for the sweep, as D-18 registered.
4. **No promotion to inferential.** `final_round_worse`, the rung labels and the `steps` distribution
   are descriptive. None of them enters the primary endpoint, any hypothesis test, or any gate.
5. **No change to what a sweep selects or measures**, and nothing here holds up the 180 draws.

### What would reopen it

A second writer for `final_round_worse`, or retention being applied at the `VERDICT_UNVERIFIED` exit,
either of which moves D-19.1's denominator; `record.rounds` gaining a writer other than
`agents_core.py:2810`, since "the last round graded is `repair_rounds + 1`" is what makes that
denominator computable from stored fields; `escalation` gaining a writer that preserves the rung
across the exhausted branch, which is the one change that would let the exhausted histogram carry a
rung and would make D-19.2's stated limit false; a second graded step per cell entering any reported
statistic, as opposed to being reported as the distribution D-19.3 registers; or any of the four
corrected citations moving again, which changes a pointer and not a rule.

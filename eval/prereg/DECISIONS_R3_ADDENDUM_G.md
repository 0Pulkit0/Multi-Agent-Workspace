# Pre-registration addendum G to Revision 3

**Written 2026-09-01, after addendum F and before the first calibration draw is spent. Revised the
same day, before this file's first commit and before it had been read anywhere off this machine.**
Separate file rather than an edit to any earlier addendum, for the reason addendum A gave and B, D,
E and F repeated: a registration document that changes after it has been read is not a registration.
`DECISIONS_R3.md` and addenda A through F are left exactly as they were written.

The in-place revision is legitimate for that same reason, and only while it holds: an uncommitted
draft that no one off this machine has read is not yet a registration, so the rule above did not yet
bind it. From the commit that carries this file, it does. Any later change to D-17 or D-18 needs its
own addendum.

Scope: adds **D-17** and **D-18**. Everything in Revision 3 and in addenda A–F stands unchanged.
Neither item changes code, and neither changes what any call sends. D-17 is a definitional rule;
D-18 is a reading rule for fields the records already carry.

---

## D-17. Operational definitions for the terms that collide with other literatures

**Registered: the four terms below carry the definitions given here throughout the write-up and any
derived material. Where a term has an established meaning in another literature, that meaning is
disclaimed explicitly at first use rather than inherited.**

The metric contract in `EVAL_PREREGISTRATION_AMENDMENT.md` Part 2 is already arithmetically
unambiguous. The gap this item closes is not arithmetic. It is that three of the load-bearing
phrases mean something *different and well-established* in adjacent fields, so a reader arrives with
the wrong mechanism in mind and parses correct claims as false ones.

**1. "Visible" and "hidden" suites are inference-time artifacts, not a train/test split.**
The hidden suite is the task's own suite, locked before any run: `hidden_tests_sha256 =
gen_tasks.digest(task.tests)`, a full 64-hex sha256 written per cell at `eval/run_eval.py:889`,
matching `tasks.lock` and enforced before a single call is spent by the refusing lock guard at
`eval/run_eval.py:1607`. The visible suite is generated *per run* by the plan-writing role and
recorded per cell as `gate_tests_sha256`, which is **whatever `run_eval._suite_hash` returns** — a
sha256 truncated to 16 hex (`eval/run_eval.py:529`). Three arms write that field, not one: A′
(`:662`) and A′@3 (`:725`) pass through `plan["tests_sha256"]`, which is itself
`_suite_hash(scratch.tests)` at `:526`, and arm B hashes the resolved suite directly with
`_suite_hash(run.tests)` at `:764`. Different hasher and different width from the hidden field,
deliberately, and the comment at `:884` says so on the record itself.

**No parameter of any model is updated anywhere in this project.** There is no training loop, no
fine-tuning, no gradient, no reward model. The training-evaluation literature uses "held-out" to
mean data withheld from parameter updates, and its failure modes — benchmark contamination of a
training corpus, temporal or grouped leakage, target drift — are mechanically different from ours.
Ours is: does code repaired against one generated assert suite pass a different, frozen one.

**2. No LLM renders a verdict; an LLM authors the suite that one verdict is rendered against.**
Both halves are load-bearing, and stating only the first overreaches by exactly one step.

*Every verdict here is rendered by an interpreter.* The gate verdict and the hidden grade are both
`harness.verify_output` running code in a subprocess and classifying what came back (`harness.py`:
FAIL_NONE / FAIL_IMPORT / FAIL_ASSERTION / FAIL_RUNTIME / FAIL_TIMEOUT / FAIL_TIMEOUT_IMPORT).
**There is no LLM anywhere in either grading path, and no human rater.** The entire LLM-as-judge
failure class — position bias, verbosity bias, self-preference, judge collusion, judge drift, rubric
leakage — is therefore structurally unavailable here, and any review that imports it has made a
category error.

*The visible suite's content is model-authored.* The plan-writing role writes those asserts. That
suite is what decides APPROVED in A′ and A′@3, and it is what arm B's repair loop conditions on:
`_verify_step` re-reads `run.tests` every round and picks its next rung off that verdict. So a
model-authored artifact does sit inside the accept path — one step upstream of the verdict, as the
*instrument* rather than as the judge. What goes wrong with an instrument is not bias but
calibration: it can be too weak to discriminate, or it can be arithmetically wrong. Neither is a
judging error, and neither is reachable by any of the biases named above.

Written as a pair, the item is stronger than the overreaching version, because a false gate pass and
a false gate rejection become two distinct failures with two separate instruments already on record:

| failure | mechanism | instrument |
|---|---|---|
| false gate **pass** | the **visible** suite accepts code the hidden suite rejects | the vacuity and weakness counts, read with `P(hidden-incorrect \| APPROVED)` |
| false gate **rejection** | the visible suite rejects code the hidden suite accepts | D-10 |

The instruments are already registered elsewhere and are named here only to show that the pair is
covered. For a false gate pass: `tests_status` `vacuous` / `unusable` and the `SELECTION_NO_GATE`
rate (D-4), read as a pair with the false-APPROVED rate per addendum C. For a false gate rejection:
D-10's `SELECTION_NO_APPROVAL` rate and its count of suites that are trusted, arithmetically wrong,
and reject every correct draw.

The suite that can produce a false APPROVED is the **visible** one, since `P(hidden-incorrect |
APPROVED)` conditions on the gate. A weak *hidden* suite would not cause false APPROVEDs; it would
conceal them, which is a threat to this measurement's power and not to the contrast between arms,
and it is bounded separately by the hidden suite being locked before any run and audited by
`gen_tasks --self-check`.

**3. Contamination compresses the contrast rather than biasing it.**
Class-level benchmark contamination of the underlying models would inflate every arm's absolute pass
rate and place a floor on arm A's success rate. The paired design protects the *sign*: the primary
comparison is a within-task contrast in which the same task, prompt and hidden suite appear on both
sides, so a level shift common to both sides cancels.

It does not protect the *magnitude*, and clearing the contrast outright would contradict D-2, which
already records as an accepted caveat that contamination "places a floor on arm A's success rate and
therefore a ceiling on any effect measured against it". This item makes that caveat mechanical
rather than inventing it. The contrast is a difference of rates bounded above by 1, so a task at the
ceiling has nowhere left to differ. A contaminated task that passes first-try on both sides has
`Δ̂_t = 0` structurally rather than noisily: arm B's loop returns at the first APPROVED
(`agents_core.py:2767`), so no repair round is ever spent on it, and `repair_rounds = 0` on that
cell is the observable record of it. Contamination therefore moves tasks to the ceiling, and every
task it moves there contributes a zero to the average difference.

The direction is one-sided and worth naming: compression is conservative for a finding that the
pipeline helps, and it forbids the converse reading — a small measured effect is not evidence that
the pipeline does not help, because it is what a ceiling produces on its own.

The bound is already on record and needs no new instrument. D-16's tier table and the pin replay it
cites give it: nine of the ten tier-1 tasks were 3/3 for every candidate, so the easy end of the
locked grid sits at or near the ceiling before contamination is invoked at all, and D-16 registers
that a low in-band count on that half is the expected result. `d_t` carries this into the estimate,
since a ceiling task is a `d_t` = 1.0 task and difficulty enters as a covariate over the task set
(Revision 2, Part 2).

**4. The bare phrase "multi-agent system" is retired.**
It names a pre-LLM field — deep reinforcement learning, game theory, swarm intelligence, vehicle and
UAV coordination — with no overlap with verification, test generation or request quota. Use "LLM
agents" or "a pipeline of model-backed roles". If the older field must be acknowledged, cite Tampuu
et al. (PLoS ONE, 2017) and Wang et al. (IEEE/CAA JAS, 2022) directly. Do not cite the IJRR survey
that pointed at them: its DOI encodes 2023 against a header claiming Nov 2024, its reference list
has two entries numbered [8] and no [9] while the text cites [9], and its quantitative figures have
no axes, numbers or method anywhere in the text.

### What would reopen it

Any LLM entering either grading path — that is, rendering a verdict rather than authoring a suite;
any fine-tuning or parameter update of any kind; a change to how visible and hidden suites are
produced, the Tier-4 alternative-suite regrade being the live candidate; or a second hasher for
`gate_tests_sha256` at a different width, which would break the by-hasher definition item 1 now
uses. Each would make one of the four definitions above wrong, and would need its own addendum.

---

## D-18. How the repair rounds are read

**Registered: the round axis is `retained_round` over 1..`MAX_REVISION_ROUNDS` + 1 — that is 1..4,
an initial attempt plus three repairs — with 0 excluded as its own absent state and never counted as
a round. The per-round breakdown of the hidden pass rate, the false-APPROVED rate and the
repair-generalization gap is reported over **approved arm-B cells only**, where `retained_round` has
one meaning. Exhausted cells are reported as their own group with their own histogram, labelled
best-of rather than first-pass, and the two histograms are not comparable on a shared axis. The
cumulative approved-by-round curve is reported as the statistic that speaks to the marginal value of
a round. `MAX_REVISION_ROUNDS` stays at 3 and `ESCALATION` stays `("repair", "alternate", "fresh")`
for the sweep. All of it is descriptive; none of it enters the primary endpoint or any test.**

The records already carry what this needs — `repair_rounds`, `retained_round` and
`final_round_worse` are written per cell (`eval/run_eval.py:759,769,770`) from the retention logic
in `agents_core.py`. So this costs no code and no calls. What it buys is that the marginal value of
round 2 and round 3 is read against a rule fixed before the numbers exist, rather than a round
budget justified after seeing which one looked best.

### The axis, read off the loop rather than assumed

`_verify_step` iterates `for attempt in range(MAX_REVISION_ROUNDS + 1)` (`agents_core.py:2720`) and
sets `record.retained_round = attempt + 1` (`:2743`), so the field takes **1, 2, 3, 4**: one initial
attempt plus up to three repairs. A 0-based `0..3` axis would mislabel every stratum and silently
merge the initial attempt with the first repair.

**0 is not round zero.** It is the dataclass default (`agents_core.py:2168`) surfacing through
`step.retained_round if step else 0` (`eval/run_eval.py:769`), and it means no step in that cell
produced code to grade — `graded` was empty at `:751`, so there is no round to name. Registered as
an absent state, reported as its own count, and excluded from every histogram and denominator below.

The four rounds are also four *different operations*, not four tries of one. The transition into
round 2 is `ESCALATION[0]` = `repair`, into round 3 `alternate`, into round 4 `fresh`
(`agents_core.py:2792`), and `escalation` (`eval/run_eval.py:760`) records which rung produced the
retained attempt, or `""` when the cell passed at round 1. So each stratum is labelled by its rung
as well as its number, and "the marginal value of a round" is read as the marginal value of a rung.

### Why the breakdown is approved-only: `retained_round` has two meanings

The loop returns immediately on `VERDICT_APPROVED` (`agents_core.py:2767`), so for an approved cell
the field is the **first** round that passed the gate — a stopping time. Retention is a no-op there
in effect, since APPROVED is element 1 of `_candidate_rank` and outranks every earlier candidate.

For an exhausted cell the field is the **best-ranked failing** round, chosen by `_retain_best`
(`:2662`) through `_candidate_rank` (`:2607`) — an argmax over a partial order, not a stopping time.
Element 4 of that key is `-round`, so ties go to the *earliest* round; among candidates that failed
the same way the histogram therefore concentrates on round 1 for a reason that is a tie-break rule
and not evidence about repair. Pooling the two would average a stopping time with an argmax and read
the result as one distribution.

### The groups, and the two that are neither

`_verify_step` has four reachable exits, so an arm-B cell falls into one of four groups. Naming all
four is the point: two of them are neither approved nor exhausted, and pooling them into the
exhausted histogram would put cells there that never finished the ladder.

| group | exit | what `retained_round` is | reported as |
|---|---|---|---|
| approved | `:2767` | first round that passed the gate | histogram, and every rate below |
| exhausted | `:2780` | best-ranked failing round | its own best-of histogram |
| unverified | `:2771` | where the *harness* stopped; no retention applied | count, with `tests_status` |
| provider-error | `:2803` | best-of over a ladder cut short | count |

The exits are `VERDICT_APPROVED`, the `attempt == MAX_REVISION_ROUNDS` branch that sets `escalation`
to `"exhausted"`, `VERDICT_UNVERIFIED`, and a `ProviderError` raised by the revision call. A fifth
exit would be a group this table does not name, which is why one is listed under what would reopen
the item.

The unverified group is the one most easily mistaken for a round reading: it returns `record`
without calling `_retain_best`, deliberately (`:2772-2776`), so the field keeps `attempt + 1` and
names where the *harness* stopped rather than where the code got to. Neither of the last two groups
enters a histogram; both are reported as counts so the four groups sum to the arm-B cell count.

One consistency check, registered because it is free: for approved cells `retained_round` =
`repair_rounds` + 1, since `record.rounds` is set to `attempt + 1` only after a revision call
succeeds (`:2810`). The identity does not hold for exhausted cells, where `repair_rounds` is 3 and
`retained_round` is whichever round ranked best. A cell that violates the identity in the approved
group means the axis is not what this item describes.

### The cumulative approved-by-round curve

**Registered: of arm-B cells with a usable gate, the fraction approved at the gate by round 1, by
round 2, by round 3 and by round 4.** Numerator from `pipeline_says_passed` and `retained_round`;
monotone non-decreasing by construction; no new field, and computable from stored records.

The denominator is arm-B cells that had a usable gate and a graded step — `gate_available == True`
(`eval/run_eval.py:763`) and `retained_round >= 1`. A cell with no trustworthy suite cannot be
APPROVED at any round, because `_verify_step` downgrades APPROVED to UNVERIFIED when `record.tested`
is false (`agents_core.py:2726`), and a cell with no graded step has no round at all; either one in
the denominator depresses the curve for a reason unrelated to rounds. Both excluded counts are
reported beside it, so the all-cells version is recoverable and the choice of denominator is visible
rather than assumed.

This is the statistic that speaks to the marginal value of a round, and it is why the split above is
worth its cost: on the approved group alone the curve is well defined, needs no new field, and its
increments are exactly "cells that round *N* rescued".

### The by-round rates, and what they are not

The hidden pass rate and the repair-generalization gap by round are kept, and registered for what
they are: **a comparison across non-equivalent subgroups.** Cells approved at round 1 are the easier
ones — that is close to the definition of passing first try — so a difference between round-1 and
round-3 strata is confounded with task difficulty by construction, and there is no design here that
unconfounds it.

**Registered: the 11-level `d_t` covariate (Revision 2, Part 2) is reported per stratum, and no
round effect is claimed.** A stratum's mean `d_t` is what tells a reader whether a gap between
rounds is a repair effect or a difficulty effect, and with these n it will not distinguish them. The
rates are reported as description; the covariate is reported so the confound is visible in the same
table.

**Registered: for the false-APPROVED rate by round, n per stratum is reported and no interval is
computed on it.** Splitting a conditional rate four ways over a 36-task grid leaves strata that
cannot carry one, and an interval printed on n = 2 invites exactly the reading this item exists to
prevent.

`final_round_worse` is reported as a rate over arm-B cells. It is defined on `_candidate_merit` —
rank without the round tie-break — so it does not fire merely because a later round lost a tie
(`agents_core.py:2651`).

### What this does not license

1. **No promotion to inferential.** None of these strata enter the primary endpoint, any hypothesis
   test, or any gate. A per-round claim would need a multiplicity correction and its own addendum.
2. **No change to `MAX_REVISION_ROUNDS` or `ESCALATION`.** 3 and `("repair", "alternate", "fresh")`
   for the sweep. Reading the histogram is not licence to retune the ladder that produced it.
3. **No pooling of the four groups**, and no reporting of an approved-plus-exhausted histogram on
   one axis, which is the specific error this item forbids.
4. **No minimum stratum size and no collapsing of thin strata.** Both would be chosen after the
   counts were seen, which is D-10 point 3's principle. Thin strata are reported with their n.

### What would reopen it

A change to `MAX_REVISION_ROUNDS` or to the `ESCALATION` ladder, either of which moves the axis; a
change to `_candidate_rank` or `_retain_best`, which would redefine what an exhausted cell's round
means; a fifth exit from `_verify_step`, which would add a group the table above does not name; or
the per-round breakdown being promoted from descriptive to inferential.

### The external prompt, recorded at its weight

The prompt for registering this is external and weak, and is recorded at that weight: a
single-author practitioner report (n = 10 runs, one topic, author-judged quality, no blinding, no
held-out grader, paid models) found a third "critic" agent made output *worse* while costing ~78%
more tokens, and recommended capping revision at one round. **That is not evidence about this
system.** Its critic was an LLM scoring prose with no ground truth, which is the design this project
deleted; its own diagnosis — that a critic pays only where there is a natural quality gate — is the
thesis under test here, arrived at from the failure side. Two specifics do transfer, and both are
already satisfied rather than pending: its "use a different model per role" recommendation is this
project's existing role/provider split, and its cap-at-one-round fix is superseded by retained-best
selection (`_retain_best`), which keeps the best candidate rather than the last and already flags
regression via `final_round_worse`. The one thing it correctly identifies as missing is the
*measurement*, which is what this item registers.

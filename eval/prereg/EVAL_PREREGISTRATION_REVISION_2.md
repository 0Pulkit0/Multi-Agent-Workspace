# Eval Pre-Registration — Revision 2

*Companion to `EVAL_PREREGISTRATION_AMENDMENT.md`. That file records what was believed
before the R1–R6 red-team pass; this file records what supersedes it. Where the two
conflict, this file wins. The original is kept unedited so the change history is auditable.*

Date: 2026-08-29. Status: **GO** — freeze the manifest and run, after the blocking list in Part D.

---

## Part A — What R1–R6 changed, in one page

1. **The mid-band is deleted.** With k=3 each task's A′ rate is one of {0, 0.33, 0.67, 1.0},
   and only 0.33 falls inside [0.25, 0.6] — the "band" was a knife edge holding perhaps 2–3
   tasks. The proposed Wilson-overlap rescue is degenerate: at n=3 all four attainable rates
   have intervals overlapping [0.25, 0.6], so the band contains all 30 tasks. Subsetting at
   this n cannot be simultaneously non-degenerate, non-circular, and non-empty.
   **Replacement:** difficulty enters as a continuous covariate over all 30 tasks.
2. **Difficulty is measured on a pre-run calibration sweep**, disjoint from the contrast:
   10 A′ samples per task, reusing B's Planner spec, Groq-only, ~300 calls, zero Gemini.
   Yields a continuous `d_t` with 11 levels. Not A (extraction-failure contaminated, and not
   the arm in the contrast), not B (endogenous to repair), not the main-run samples (shared noise).
3. **The estimator is a paired task-cluster bootstrap plus an exact sign-flip permutation
   test.** No GLMM (singular fits below ~10–15 clusters), no McNemar (discards the 3-repeat
   gradient), no BCa (acceleration term is noise at this n).
4. **B−A′ is demoted to a compound descriptive.** The mechanism endpoint is the
   selector-matched feedback term from the three-way decomposition in Part C.
5. **Nondeterminism is scored incorrect in the primary**, with exclusion as sensitivity —
   because exclusion is non-random (flaky candidates cluster at the pass/fail boundary,
   which is where false-APPROVED lives) and arm-dependent.
6. **Everything that becomes unfalsifiable once data exists is ~2.5–3 dev-days.** Everything
   else operates on stored data and is built while the grid runs.

---

## Part B — The estimator, pinned

**Step 0.** Reduce each (task, arm) to `p̂_t = (1/3)·Σ y_{t,r}` over the policy-selected
candidate per repeat. The task is the row; at most 30 rows. Never treat 90 runs as 90
independent observations.

**Step 1.** No band. Fit over all 30 tasks.

**Step 2 — primary statistic.** Paired mean difference `Δ̂ = mean_t(p̂_t^B − p̂_t^{A′})`.
- Interval: cluster bootstrap, 10,000 resamples, tasks drawn with replacement carrying the
  whole paired 3-repeat block intact. Percentile interval. **State that it undercovers below
  ~20 clusters.**
- Test: **exact sign-flip randomization test** on the per-task differences. At n≤20 the
  permutation set is enumerable or near-enumerable, so this needs no asymptotics — it is
  strictly more defensible than a bootstrap p-value here.
- Direction: pre-register two-sided.

**Step 3 — degenerate tasks.** All-fail and all-pass tasks are **kept** in the headline
(`Δ̂` then estimates the deployment-population effect and is conservative). An
informative-pairs-only estimate is reported as exploratory. The distinction is one of
estimand, not precision — the earlier claim that degenerate tasks "tighten the interval and
so understate the effect" is wrong in mechanism; they shrink both `Δ̂` and its SD, with
ambiguous net effect on coverage.

**Step 4 — difficulty interaction.** Spearman of `d_t` against `Δ̂_t`, or a rank regression
of `Δ̂_t` on `d_t`, over all 30 tasks, with a permutation p-value. **Not terciles** — binning
a continuous 11-level covariate into three discards the within-stratum gradient, which is the
exact objection that ruled out McNemar.

**Step 5 — reporting.** Interval-led, never a p-value alone. Minimum detectable effect printed
adjacent to any null. n and discordant-pair count in the headline table, not a footnote. The
correct sentence for a wide interval is "underpowered; estimate and interval reported for
meta-analytic use." Never "no significant difference" as if it meant no difference; never an
equivalence claim, since the interval will not exclude a meaningful effect at this n.
**The reporting sentences themselves are pre-registered in the manifest** — framing drifts
after seeing results even when numbers don't.

---

## Part C — The decomposition (supersedes the single B−A′ headline)

B differs from A′ in three ways at once: extra compute, feedback conditioning, and
gate-mediated selection. Reported as one number, a positive result could be any of the three
and a null could be any of them failing or cancelling. Because every candidate is hidden-graded
(including ones the pipeline discarded), both an oracle and a gate reading are available for
**every** multi-candidate arm at zero extra API cost. Selectors must be matched across a contrast.

$$
B_\text{gate} - A' \;=\; \underbrace{(A'@3_\text{oracle} - A')}_{\text{resampling}}
\;+\; \underbrace{(B_\text{oracle} - A'@3_\text{oracle})}_{\text{feedback}}
\;+\; \underbrace{(B_\text{gate} - B_\text{oracle})}_{\text{gate loss on B}}
$$

| Contrast | Isolates | Registered as |
|---|---|---|
| `A′@3_oracle − A′` | value of 1→3 draws, perfect selection | secondary |
| **`B_oracle − A′@3_oracle`** | **feedback over equal-compute blind resampling** | **mechanism endpoint** |
| `B_gate − B_oracle` | gate selection loss among repaired candidates | secondary |
| `A′@3_gate − A′@3_oracle` | gate selection loss among blind draws | secondary |
| `B_gate − A′@3_gate` | deployed like-for-like feedback value | deployment endpoint |
| `B − A′` | compound | descriptive only, labelled compound |
| `A − A′` | specification effect | secondary, with extraction-failure sensitivity |

`B − A′@3_oracle` is **not** the mechanism endpoint: B's realized output is gate-selected, so
that contrast equals feedback minus gate loss, reintroducing the confound it claims to remove.

**Two gate-loss estimates is a finding, not redundancy.** If the gate loses more among B's
repaired candidates than among A′'s blind draws, the gate discriminates less well once repair
has made candidates similar to one another.

**Intent-to-treat.** The mechanism endpoint is computed **unconditionally** over all B repeats,
including those where repair never fired. Repair-engagement is post-treatment and caused by
candidate quality and suite teeth; conditioning on it is the same collider structure that
demoted approved-only accuracy. The repair-engaged stratum is a per-protocol **diagnostic**,
labelled exploratory, and is what distinguishes "feedback is useless" from "the trigger never
fired." Report the repair-engaged fraction and consecutive-candidate edit distance alongside.

**Compute is not exactly matched.** B stops at first APPROVED and so often spends fewer than
three draws while A′@3 always spends three. The feedback term is therefore a **lower bound**;
report B's realized mean draw count beside it.

**Spec adherence is controlled away, not measured** — A′ and B share B's per-repeat spec by
construction. The study estimates spec effect only in the separate `A − A′` contrast, subject
to the extraction-failure caveat below. Say this explicitly or the omission reads as oversight.

---

## Part C2 — Pre-registered magnitude ranges

All in points of unconditional per-task hidden-correct yield, **over all 30 tasks with
difficulty as a covariate** (the original ranges were stated over the now-deleted mid-band and
would not have been scoreable against what is actually computed).

| Term | Range | Read |
|---|---|---|
| resampling `A′@3_oracle − A′` | +15 to +35 | Near-mechanical: at p≈0.4, `1−0.6³ = 0.784` → +38 under independence, haircut for within-task draw correlation. **Below +10 means the Executor is near-deterministic at temperature** — itself a finding about the sampler. |
| gate loss (blind) | −5 to −20 | ≤0 by construction. Near 0 = surprisingly good selector, and would undercut the false-APPROVED thesis. Below −20 supports it. |
| **feedback (mechanism)** | **−5 to +15, mode +3 to +8** | Negative is entirely plausible and is the result most tempting to explain away post hoc. Pre-committing is the point. |
| compound `B − A′` | +10 to +30 | Dominated by resampling, which is why it is a bad mechanism test. |

**Scoring rule:** the mechanism prediction is graded on the feedback term, the deployment
prediction on the compound term, **scored separately**. Otherwise a resampling-driven win
silently ratifies a feedback hypothesis it does not support.

**Withdrawn prediction.** The earlier round-1 prediction "B beats A′ by less than 10 points"
is near-disjoint from the +10 to +30 compound range above. Keeping both makes the P-series
unfalsifiable. The round-1 prediction is **formally withdrawn**; it assumed B making a single
repaired attempt, whereas the registered B realizes up to three draws. Logged here so the
withdrawal predates the data.

---

## Part C3 — False-APPROVED, pinned

Two statistics, two purposes:

1. **Gate property, one arm at a time.** `FA = Σ_t(incorrect ∧ APPROVED) / Σ_t APPROVED`
   — ratio-of-sums, not mean-of-ratios (the latter over-weights tasks with one lucky
   approval). Cluster bootstrap over tasks carrying the whole APPROVED block. Cells with zero
   APPROVED candidates are **absent from the denominator, not zeros in the numerator**; their
   count is reported.
2. **Arm comparison.** Restricted to the **single policy-selected candidate per (task, repeat,
   arm)**. Ratio-of-sums over all candidates weights tasks by approval count, which is
   arm-dependent (B emits many candidates, A′ one) and correlates with easiness — the two arms'
   estimates would use different implicit task weights and be non-comparable.

**Both are collider-conditioned** — approval is post-treatment and treatment-affected. So
false-APPROVED is interpretable only jointly with gate sensitivity `P(APPROVED | correct)` and
unconditional yield. Register the triple; never the single number as a causal contrast.

Determinism-excluded candidates leave both numerator and denominator; the exclusion rate is
reported, and above the pre-set threshold `FA` is reported as a **bound**, not a point estimate.

---

## Part D — Blocking checklist before k=1 (~2.5–3 dev-days)

Everything here is irreversible: it cannot be reconstructed after data exists.

**D0 — Sprint 0, minutes not days. Do this first.**
- One API call to settle whether `gemini-3.6-flash` is a real model ID (404 = bad slug,
  401/403 = bad key). `gemini-flash-latest` is the safer pin. The Planner is Gemini-only; a
  bad slug 404s the entire grid.
- One bare-metal terminal run to confirm macOS `sandbox-exec` engages natively (it reports
  `os-level:unavailable` under nesting). With the OS layer off, a hallucinated absolute-path
  write reaches the real filesystem. Five minutes; load-bearing, not cosmetic.

**D1 — `FAILOVER=off` during measurement.** Retry-with-backoff only, hard-fail on exhaustion.
B makes ~10× the calls and absorbs ~10× the 429s, and each silent provider swap turns "B" into
a provider mixture correlated with time of day. *Omitted from the R6 table; it is the single
most important run-time config in the study and cannot be fixed post hoc.*

**D2 — Freeze manifest.** Task set + generation procedure + novel/classic assignment (hashed);
no band; estimand-1 denominator (one policy-selected outcome per task/repeat/arm); primary
direction; **analysis script hash**; fixed-n design with no interim looks and no optional
stopping; exclusion taxonomy; completed-cell definition; reduced-repeat rule (**pair on
available complete repeats, require ≥2 per arm, report imbalance**); seed policy (independent
Executor sampling per candidate; spec shared per repeat); tie-break for gate-best-of-3
(**first APPROVED in seeded order** — never "shortest" or "highest visible-suite score", which
correlate with hidden correctness and silently upgrade the gate toward the oracle); B's
stopping condition and round cap, and that the policy-selected candidate is the APPROVED one
if it exists else the last emitted; secondary-metric family count fixed now (labelled
exploratory, no multiplicity correction, no secondary supports a standalone claim); resume rule
(**re-run only un-attempted cells, never a completed one**); provider-outage void threshold;
wrong-reference rule (**symmetric across arms, logged reproducing case, reported as
sensitivity, no reference altered after seeing its effect on `Δ̂`**); the reporting sentences.

**D3 — Typed-outcome event schema**, intent row written *before* each attempt:
`{completed, exhausted, crashed, extraction_failed, determinism_excluded}`. `extraction_failed`
for arm A is a **failure** (part of the arm definition). `exhausted`/`crashed` are **missing**,
excluded from both numerator and denominator and counted separately. Crash or timeout on the
*hidden* grader is **hidden-incorrect**, distinct from `extraction_failed` (nothing to grade)
and `exhausted` (never reached the model). Append-only JSONL, **flushed per event** — a
buffered writer plus a killed process loses exactly the tail that explains the kill.

**D4 — Per-candidate artifact persistence.** Every candidate stored content-addressed,
including ones the pipeline discarded. Without this the oracle readings in Part C and the
false-REJECT measurement are unrecoverable. Cold grading itself is deferrable; the storage is not.

**D5 — Hidden-suite freeze, hash, and leakage audit.** Hidden suites hashed in the manifest so
they cannot be "improved" mid-run, plus an automated check that no hidden probe's literal
expected values appear in that task's Planner spec or visible suite. ~1h, and it is the one
failure that invalidates every number simultaneously.

**D6 — Reference validation before k=1.** Every reference passes its own Planner suite plus a
hand-check, with a logged validation timestamp. A reference failing validation is fixed or its
task dropped **before** the run.

**D7 — Task-order randomization**, shuffled once from a seed recorded in the manifest, arms
interleaved within task. Without this, time is confounded with generation order and therefore
with tier/difficulty, and the drift probe is uninterpretable.

**D8 — Calibration sweep.** 10 **A′** samples per task (~300 Groq calls, zero Gemini, reusing
B's spec) → continuous `d_t`, frozen before the grid.

---

## Part E — Deferrable, built while the grid runs (~2 dev-days)

Cluster bootstrap + permutation harness (primary and false-APPROVED); the Part C decomposition
and repair-engaged stratification; the cold-grading pass (parallelize it — ~700 executions at up
to 15s serial is a multi-hour job, and a truncated cold-grade is worse than none); drift probe;
determinism-exclusion reporting; consecutive-candidate edit distance; runtime p50/p90 logging;
flakiness battery; extraction-failure sensitivity for arm A. All operate on stored data; none
gains defensibility from being front-loaded.

**Extraction-failure sensitivity (arm A).** A is single-shot on a raw prompt with no instruction
to emit a fenced block, so it plausibly fails extraction more often than A′ for formatting
rather than capability reasons — which inflates the measured specification effect. Report A both
as-registered and excluding extraction failures.

---

## Part F — The go/no-go gate, corrected

The R6 verdict is **accepted: go.** Its gate condition is not, because it is stated in terms of
the deleted band ("in-band n ≥ 6 under Wilson-overlap"). Under Wilson-overlap the band contains
all 30 tasks, so that gate auto-passes and is vacuous; the occupancy simulation it gates on is
therefore unnecessary, and the half-day is saved.

The calibration sweep still earns its keep, for a different reason, and supplies the real gate:

> **No-go condition.** If fewer than 12 of 30 tasks have a calibration A′ rate in (0.1, 0.9),
> the task set is at floor or ceiling, `Δ̂` has no headroom, and the grid would confirm nothing.
> In that single case, regenerate or retier tasks before k=1. Otherwise execute the full grid.

Threshold 12 is pre-registered now, before the calibration data exists. The sweep costs ~300
Groq calls, so you learn which branch you are on for ~2% of the grid budget.

**Execution order.** D0 → D1–D7 → D8 calibration sweep → gate → full grid → Part E during the
run → analysis per Parts B/C → interval-led report, everything below the primary exploratory.

Roughly 2.5–3 of ~20 remaining days before the first grid call.

---

## Part G — Standing conclusions from the pass

- The review lineage **converged**: R6 split blocking from deferrable instead of adding scope.
  That is the signal to freeze and run. Any further round that returns new items should be
  logged to the deferred backlog, not the blocking list.
- Three consecutive sections re-imported the mid-band after it was retired. Deleting a construct
  in a design document does not delete it from a reviewer's habits — check every new
  recommendation for whether it silently depends on a subset that no longer exists.
- The reviewer's own de-duplication correction was right and is accepted: shared plumbing (one
  event schema, one bootstrap harness, one manifest, one calibration sweep) means the scaffolding
  is ~5 days total, not 8, of which ~2.5–3 block.
- Every decision that mattered — the estimand-1 denominator, deleting the band, the
  feedback-vs-resampling decomposition, scoring nondeterminism as incorrect — was about making a
  **likely null mean something** rather than chasing a significant positive. That is the result
  that survives review, and it is the framing for the writeup.
- Unchanged and still in force: in-process sandbox guards are accident containment, not a
  security boundary; the stdout → context → memory-JSON secret channel is a production blocker
  before any real folder is pointed at this; the accident-containment posture stops being honest
  the moment task prompts incorporate fetched web content; and Gemini scarcity is **not** to be
  solved with multiple Google accounts.

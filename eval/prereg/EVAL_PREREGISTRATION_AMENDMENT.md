# Multi-Agent Workspace — Eval Pre-Registration Amendment & Build Plan

**Date:** 2026-08-29
**Status:** amendment written BEFORE any results exist. Amending before the run is legitimate; amending after is not.
**Source:** synthesis of four external review rounds (v2 brief + three v3 adversarial-extraction responses) plus my own corrections. Reviewer claims that were wrong or miscosted are corrected in Part 6, not silently dropped.

---

## How to use this document

- **Part 1** is the blocking list. Nothing in Part 4 gets built until Part 1 ships and the pilot runs.
- **Part 2** is the metric contract. Copy these definitions verbatim into the registration; ambiguity here is what makes results flip later.
- **Part 3** is the logger. It must exist before the pilot or the failure residue is gone permanently.
- **Part 4** is the deferred backlog, priced. Most of it is *analysis over logged runs* and therefore cannot be built early anyway.
- **Part 5** is what not to build. Removal is the scarcer service.
- **Part 7** holds two conflicting scored predictions. They conflict on the project's central question, which is the actual reason to run.

**Total blocking work: ~4 dev-days, 0 additional API requests.** Four reviews proposed 15–28 dev-days each. That is the trap; the precision of per-item costs hides the sum.

---

## Part 1 — The 13 blocking items

Each is cheap now and impossible after the first run.

### Measurement integrity

**B1. FAILOVER=off during measurement.** ~1h
Arm B makes ~10× the calls of A/A′, absorbs ~10× the 429s, and each 429 silently swaps the Executor's provider. Logging *detects* this; it does not *fix* it — pooled analysis still treats B as one treatment when B's rows are a provider mixture whose composition correlates with rate-limit pressure, which correlates with time of day. Task-level pairing cannot cancel an arm-correlated provider mix. Retry-with-backoff only; hard-fail the task on exhaustion.

**B2. Separate cold grading pass.** ~3h
Do not score from the pipeline's own last execution. Re-extract code from stored artifacts and execute every arm's final candidate once, in one batch, under identical conditions. Two biases fixed at once: B is differentially exposed to timeout flake under accumulated load (deflation), and without re-extraction your headline metric can score a file that was never saved (integrity).

**B3. Hidden-grade every candidate, including rejected ones.** ~2h
Without this, false-REJECT is unmeasurable and every metric carries survivorship bias.

**B4. Pin and log per call:** model ID, provider actually used, temperature, top_p, timestamp. ~1h
Free endpoints sometimes override sampling params. Unpinned sampling makes every number irreproducible. Provider-actually-used logging is what turns a silent failover into a visible one.

**B5. Interleave arms.** ~0.5h
A, A′, A′@3, B for task 1, then task 2. Never all-A-then-all-B. Provider drift over hours confounds blocks.

### Design of the comparison

**B6. Arm A′ shares B's Planner spec.** ~1h
One Gemini call serves both arms for a given task/repeat → zero extra scarce-currency spend, one extra Groq call. A′ receives the spec only, never the visible suite.

**B7. Arm A′@3 — matched-budget resampling control.** ~3h + 2 Groq calls/task
Three independent A′ samples, same gate, best-of-3. Matched call budget to B, zero feedback. **This is the control that separates "repair works" from "extra lottery tickets."** Later calls can succeed by stochastic resampling even when the failure message carries no information. If B ≈ A′@3, the repair loop is resampling with extra steps and should be cut rather than tuned.

**B8. Arm A defined precisely.** ~0.5h
Zero retries. Extraction failure counts as a failure. "Single-shot with one free reformat retry" is not single-shot and the label ends up in a table someone reads.

**B9. Novel-compositional template family.** ~5h
Randomly composed transforms over invented operation names with no textbook analogue; spec and reference generated together. Classic families (CSV pivot, ciphers, parsers) are contaminated at the *class* level regardless of instance synthesis — contamination caps Arm A's failure rate, so the effect is estimated on the non-memorized residue and compressed toward zero. Report classic and novel families separately even at small n.

### Statistics

**B10. Task-level aggregation, pre-registered.** ~0.5h
k=3 replicates are repeated measurements, not 90 independent observations. Within-task correlation ~0.3 makes naive CIs roughly 20–30% too narrow — you publish "significant" and it fails to replicate. Compute per-task pass rates, paired test on task-level differences, task-cluster bootstrap.

**B11. Exactly one primary endpoint declared; everything else labeled exploratory.** ~0.5h
3 tiers × 4 arms × ~5 metrics is dozens of implicit tests; at p<0.05 you *expect* 1–2 spurious findings, and those are the ones that end up in an abstract. Primary: see Part 2.

**B12. k=1 is a smoke test whose numbers are DISCARDED, not pooled into k=3.** ~0h
Looking at k=1 and then pooling it into a k=3 analysis is sequential analysis without accounting, and it voids the registration. Either pre-commit to k=3 outright, or declare the k=1 run's purpose as plumbing verification with results thrown away.

**B13. Four separate estimands; approved-only pass rate is NOT the headline.** ~4h
Approval is caused by both candidate quality and suite behavior, so conditioning on it is collider/selection bias. Worked case: B approves 20/30 with 4 hidden failures (80% approved-only, 16 correct outputs); A′ approves 10/30 with 1 hidden failure (90% approved-only, 9 correct outputs). Approved-only correctness favors A′; unconditional correct-output yield favors B by 78%. Same data, opposite conclusions.

### Free hygiene worth folding in (~3h combined)

- **Flakiness battery**: run each suite with `PYTHONHASHSEED=0` and `=1` (string hashing is randomized by default — this catches set/dict iteration-order assumptions), once with assert lines permuted (inter-assert state leakage), and with `random.seed` forced to two values. Mutation scoring is structurally blind to flakiness; a flaky suite kills mutants fine and is still ungateable.
- **Runtime distribution logging**: log p50/p90 suite runtime against the 15s budget; flag p90 > 50%. You already have the timings and aren't reading them.
- **Determinism probe**: double-execute each candidate on the same fuzzed inputs; any byte-level output difference → automatic UNVERIFIED regardless of suite outcome. Nondeterminism invalidates every other gate you have.

---

## Part 2 — Metric contract

### Arms

| Arm | Definition | Isolates |
|---|---|---|
| **A** | Single-shot Executor on raw task prompt. Zero retries. | Baseline |
| **A′** | B's Planner spec → single Executor call, no repair, spec only (no visible suite) | A vs A′ = **specification effect** |
| **A′@3** | Three independent A′ samples, best-of-3 by the same gate | A′@3 vs B = **feedback vs resampling** |
| **B** | Full Planner → Executor → harness repair loop, max 3 rounds, ladder fixed | A′ vs B = spec+tests+repair |

The escalation ladder **stays fixed during measurement**. An adaptive policy makes B's behavior depend on runtime noise, which makes the measurement non-reproducible and couples it to the very policy being evaluated. Adaptive stopping is a production optimization justified *by* the eval, never a component *of* it.

### The four estimands (report all four, always)

1. **Unconditional yield** — hidden correctness across ALL generated candidates, approved or not.
2. **Conditional accuracy** — hidden correctness given APPROVED. Informative but collider-biased; never the headline.
3. **False-APPROVED rate** — P(hidden-incorrect | APPROVED). The thesis ceiling.
4. **Gate sensitivity** — P(APPROVED | hidden-correct). Your false-REJECT rate, expressed usefully. Currently unnamed in the brief.

**Primary endpoint (pre-register exactly this):** unconditional correct-output yield, B vs A′, task-level paired, mid-band tasks (A′ hidden pass rate in [0.25, 0.6]). Everything else exploratory.

### Secondary headline metrics

- **Repair-generalization gap** = visible pass rate − hidden pass rate. **Confounded on its own** — if the hidden suite is stronger than the visible one (it will be), a gap appears with zero overfitting. Baseline it against A′'s gap, since A′ never saw the visible tests and cannot have overfit them. Report **Gap_B − Gap_A′** as the overfitting estimate.
- **Suite validity rate** — fraction of Planner suites that pass against the reference. The direct quality measure of your weakest model's most important output.
- **Suite teeth** — does the NotImplementedError stub fail ≥1 assert. Log per task as a covariate. Repair is mechanically a no-op where the suite cannot fail, which attenuates B−A′ toward zero; without this you might fire the Planner for suite weakness rather than spec uselessness. Valid as a covariate for B−A′ only (shared suite); undefined for A.
- **Calls-to-verdict and latency** per arm. You cannot position a tradeoff you don't measure.
- **Mutation kill rate as a Wilson lower confidence bound**, not a point estimate, with per-operator-family floors. A suite can score high by killing a small easy sample; an aggregate score also hides total blindness to one fault class.

### Power, stated honestly and pre-registered as such

At p_A = 0.40 and p_B = 0.60 — a 20-point effect — a paired design yields roughly 8 discordant pairs out of 30, giving two-sided p ≈ 0.04–0.10. **A true 20-point effect clears significance about half the time.** A 10-point effect is undetectable. 80% power on a 15-point effect needs ~70–100 paired tasks.

Register this sentence: *"n=30 is a pilot. Conclusions require ≥70–100 paired tasks or k=3 with task-level aggregation. Per-tier analysis at n=10 is uninterpretable (a 5/10 observed rate has a 95% CI of roughly [0.24, 0.76]) and tiers are used only as a stratification covariate, never as separate findings."*

Note the cruel irony to state in the write-up: the mid-band dose-response — "verification helps most where p ≈ 0.3–0.6" — is a tier×arm interaction, i.e. the estimate needing the *most* data. The pilot powers the crude aggregate and not at all the claim worth making.

Scaling priority, in order: **k=3 repeats → template diversification → more tasks.** Mid-band tasks sit at p ≈ 0.5 where run-to-run variance is maximal, so repeats buy the most efficiency exactly where the thesis lives. More tasks from the *same* templates buys almost nothing, because the template — not the task — is the sampling unit.

---

## Part 3 — Logger schema (must exist before the pilot)

Append-only JSONL plus a content-addressed store. Raw artifacts (candidates, suites, tracebacks) are written as `sha256(content) → path`; events point at hashes and never embed full source. Versioned enum fields. One page of schema docs alongside.

**Why this is blocking:** three months from now you answer adaptive-stopping and router questions with a one-line `jq`, not by grepping memory JSONs. Every logged `(failure signature → eventual outcome)` pair is data nobody else has, and it is the training set for every future policy decision.

### Event: `assertion` — one record per assertion execution, not per run

```json
{
  "schema_version": 1,
  "event": "assertion",
  "run_id": "r123",
  "task_id": "t017",
  "template_id": "pivot-csv",
  "template_family": "classic",
  "arm": "B",
  "repeat": 2,
  "round": 2,
  "phase": "test_exec",
  "suite": "visible",
  "assertion_id": "test_solution.py:41:0",
  "assert_src": "assert f(rows[3]) == 42",
  "outcome": "fail",
  "exc_type": "AssertionError",
  "exc_fp": "AssertionError@L41:<first_user_frame_normalized>",
  "provenance": "assertion_mismatch",
  "reference_same_exception": false,
  "input_digest": "sha256:...",
  "input_repr": "rows=[[a,b],[c]]  (shrunk)",
  "expected_digest": "sha256:...",
  "actual_digest": "sha256:...",
  "covered_lines_digest": "sha256:...",
  "suite_teeth": true,
  "deterministic": true,
  "duration_ms": 18
}
```

`assertion_id` is assigned statically from file + line + index so it stays stable across reruns. `exc_fp` is exception type plus normalized top *user* frame, hashed — that is your failure fingerprint. Store small scalars directly; store hashes plus type, size, and shape for large values.

### `provenance` enum — categorize, don't cluster traceback text

`candidate_expected_exception` · `candidate_unexpected_exception` · `assertion_mismatch` · `suite_defect` · `harness_or_sandbox_failure` · `interface_mismatch` · `timeout_before_first_assertion` · `timeout_after_partial_execution`

Text clustering merges semantically different failures and splits equivalent ones whose line numbers differ. Provenance also prevents infrastructure errors from contaminating your semantic failure rate — expect >5% of nominal candidate failures to reclassify.

### Other event types

- `execution` — one per sandbox run: exit status, wall time, byte counts, which sandbox layers engaged, timeout phase.
- `artifact` — content hash, kind (spec / visible suite / hidden suite / candidate / reference), producing model + provider + temperature.
- `repair` — the transition record between attempts (see below).
- `round_summary` / `run_summary` — carrying the pass-set vector.

### Failure-set transition metrics (~2–4h analysis on top, answers old Q5 for free)

With `F_i` = set of normalized failing assertion IDs at attempt *i*:

- `resolved = |F_i \ F_i+1|`
- `introduced = |F_i+1 \ F_i|`
- `persistence = |F_i ∩ F_i+1| / |F_i ∪ F_i+1|`

| Class | Rule | Meaning |
|---|---|---|
| Clean fix | resolved > 0, introduced = 0 | Feedback worked |
| Partial fix | some resolved, some persist | Feedback partially informative |
| Regression | introduced > 0 | Repair is net-harmful here |
| No semantic movement | `F_i+1 = F_i` | Model isn't seeing the right context — check context construction before blaming capability |
| Failure migration | original gone, new one appears | Whack-a-mole; usually a too-narrow visible suite. **This is the recoverable class where more rounds actually help.** |
| Harness instability | same artifact, inconsistent sets | Flaky; double-run detects it |

Binary APPROVED/REVISE counts make every unsuccessful repair look equivalent. These transitions separate useful-but-incomplete feedback from random resampling, overfitting, and regressions — and they are the data that decides adaptive stopping with numbers instead of judgment.

**Signature taxonomy to log from day one:** same traceback + same assertion → context problem. Same assertion, different code → capability ceiling, and provider-family switching is the test. Different assertion each round → whack-a-mole. Format failures → reformat-only retry, never a full round.

One confound you cannot currently see: **"capability ceiling" and "spec is unimplementable as written" present identically to the repair loop.** In production the answer is cheap — after two stuck rounds on the same requirement, surface that requirement to the human. The user is the spec oracle.

---

## Part 4 — Deferred backlog (post-pilot, priced, ranked)

Most of these are **analysis over logged runs**, so building them early means building against imagined data. Each carries a pre-committed kill criterion — the *form* is good discipline even where the reviewers' specific thresholds are invented (see Part 6).

### Tier 1 — decides an architecture question, cheap, needs pilot data

| Item | Cost | Decides |
|---|---|---|
| **Offline CodeT simulation** — compute the candidate×suite agreement matrix over already-logged A/A′/A′@3 candidates; ask whether execution-agreement selection beats A′ single-shot | ~1 day, **0 API** | The Planner's fate, on real logged data instead of a hunch about A≈A′ |
| **Corpus-derived mutants** — use your own wrong candidates as the mutant population | ~4h | Whether your mutation operators are calibrated to the right bug distribution |
| **Round-over-round regression oracle** — run old and new candidate on the same fuzzed inputs; require the new one to *dominate* | ~4h | Gives the repair loop a real stopping rule: revert on regression, stop on dominance |
| **Differential fuzzing vs reference** — random + boundary-biased inputs, compare candidate to reference | ~1 day | Your strongest eval gate; strictly dominates hand-authored holdout asserts |

**Correction on corpus mutants (important):** drawing the population from "failed candidates" is circular — those are candidates your suite already caught, so you'd be measuring whether the suite detects what the suite detected. Draw the population from candidates labeled wrong by **reference differential fuzzing**, and prioritize those that **passed the visible suite and were still wrong**. Those are your false-APPROVED cases, and a hidden suite tuned to catch them is doing real work. Same 4 hours, completely different measurement.

The underlying insight is sound and is the best idea across all four reviews: **your mutation operators encode the human bug distribution; your threat is the LLM bug distribution.** Standard operators (comparison flips, off-by-ones) were validated against human-seeded defects. LLM code fails differently — wrong algorithm entirely, wrong API signature, hallucinated edge semantics, swapped arguments. A suite that kills all 15 hand-mutants can wave through every failure your models actually produce.

### Tier 2 — suite-strength diagnostics, 0 API, keep only if they predict hidden failure

| Item | Cost | Catches what mutation cannot |
|---|---|---|
| **Input-partition census** (pure AST over assert literals: empty/one/many, sign, magnitude, type, nesting, unicode, sorted, malformed) | ~4h, 0 executions | Boundary deserts — a suite whose 20 asserts all use 1–3 element lists kills every mutant and still misses `[]` |
| **Checked coverage via sampled AST perturbation** — perturb one executed expression at a time, rerun only covering tests, mark "checked" if an assertion outcome flips | ~8–12h | Oracle gaps: code that executes but whose values flow into no assertion. Localizes *where* the suite is blind, which a scalar kill-rate cannot |
| **Metamorphic adequacy battery** — does the suite kill a deliberately relation-breaking reference variant | ~10–16h for 3–4 families | Coherent semantic alternatives that satisfy every listed example |
| **Counterexample reduction (ddmin)** — shrink failing inputs preserving the failure signature | ~4–7h | Not strength; **measurement hygiene.** 30 large failing inputs may be one defect; unreduced counts inflate apparent detections |
| **Coverage-guided hidden-input generation** — keep inputs that hit new reference branches or new output/exception shapes | ~8–14h | Disagreement in *unexplored* input regions. Not the same axis as mutation |
| **AST repair distance** — changed nodes, changed functions, overlap with failing vs passing coverage | ~5–8h | Distinguishes localized repair from broad rewrite; broad edits pass visible tests while raising hidden risk |

**Implementation note:** checked coverage via sampled AST perturbation is the right path and it *avoids* the tracer entirely. A `sys.settrace`-based dynamic slicer costs 10–50× slowdown against a 15-second wall clock — it would convert passing suites into timeouts and make the eval measure the tracer. Line-event-only tracing is roughly 2–5× and is affordable; value-level tracing is not. If you ever do build a tracer, traced runs are a **separate pass with a separate budget**, never the timed verdict run. (PEP 669 `sys.monitoring` is the low-overhead version and needs 3.12 — one more cost of the 3.9 pin.)

### Tier 3 — production transfer, no reference available

Ranked by how close each gets to ground truth. The organizing fact from the oracle-problem literature: **everything without a true oracle detects disagreement, not wrongness.**

1. **Execution-grounded generic properties** — determinism, crash-freedom, declared idempotence. False alarms ≈ 0 because these are execution facts, not model opinions. ~3h.
2. **Controlled-clause property compiler** — compile executable properties *only* from explicit lexical forms ("order does not matter", "must be idempotent", "returns sorted", "raises X when"). ~8–12h. False alarms 2–5% for explicit patterns, >15% for inferred ones. Gate only if precision is high; otherwise diagnostic-only.
3. **Metamorphic relations** — semantic detection when the relation is specified or mathematically necessary. **Does not** catch a uniformly wrong implementation that preserves the relation: a Caesar cipher shifting 4 instead of 3 round-trips perfectly; a "sort" returning reverse-sorted output is idempotent and length-preserving. Validate every relation family against the reference corpus first — any reference rejection is an applicability bug.
4. **N-version differential** — **triage signal only, never a verdict.** The arithmetic to internalize: at per-sample p ≈ 0.4, majority-of-3 correctness ≈ 0.35, *worse* than best-of-3-with-any-oracle ≈ 0.78. Correlated LLM errors make it worse. Its real value is as an **ambiguity miner**: disagreement points map onto under-specified spec regions, which converts false alarms into a spec-gap report for the user.
5. **Invariant mining (Daikon-lite)** — 2–3 days, 20–50% of mined invariants are accidental at small samples. Gate behind everything above.
6. **LLM-inferred properties from spec text** — **cut.** Highest false-alarm rate of anything listed; a wrong inferred "property" converts valid candidates into REVISE loops, manufacturing exactly the false oracle this project exists to measure.

**Design consequence to draw now:** in production there is no reference, so exact-value differential testing is impossible and metamorphic/property checking is all you have. So **weight metamorphic results in the eval more heavily than their raw power deserves**, because they are the instrument production will actually run — and use the eval, where a reference exists, to calibrate how much they miss on their own.

**Optional compounding trick:** write the reference *twice* per template where feasible. Points where the two references disagree are a machine-generated map of your spec's ambiguities, feedable back into prompt improvement. Eval infrastructure becoming prompt improvement is the compounding you want.

### Tier 4 — publication sensitivity analyses (schedule before write-up, not before pilot)

- **Alternative-hidden-suite regrade.** Generate a second hidden suite from the natural-language task alone, without inspecting the reference AST; regrade every candidate; report how often hidden-correctness labels change. Addresses oracle coupling (Part 6).
- **Structural overlap audit.** Record how many hidden assertions reuse a nontrivial constant or predicate partition appearing only in the reference rather than implied by the task.
- **Canary battery for backend drift.** 3 fixed prompts at temperature 0 at the start and end of each session. Providers redeploy weights under unchanged model IDs, which provider-logging cannot see. ~6–9 requests/session, ~1h. Converts "my numbers moved between Tuesday and Thursday" from mystery to measurement.

---

## Part 5 — Do not build (cut list)

Reviewers reliably add scope and never remove it. These are the removals worth honoring, with the ones I disagree with marked.

| Cut | Saves | Reason |
|---|---|---|
| General dynamic Python slicer | 30–60h | Reflection, aliasing, exceptions, dynamic dispatch make it a research project. Sampled AST perturbation gets you the metric. |
| Reusable typed Python fuzzer | 20–40h | Serialization, shrinking, validity filtering, and type inference will eat the month. Write small deterministic mutators for the types actually in the eval. |
| Broad metamorphic-relation discovery / LLM-inferred properties | 15–30h + 1 API call/task | Creates the false oracle you're measuring. Allow-list tied to explicit clauses only. |
| General invariant mining | 12–20h | Many accidental invariants; no trustworthy oracle without spec linkage. |
| Learned failure clustering / traceback NLP / embeddings | 8–15h + 1 API call/failed run | Normalized assertion ID + provenance + reduced input features answer most retrospective questions. |
| Fresh N-version candidate generation for voting | 1–2 API/task, 5–8h | Voting ≠ correctness; shared model errors survive. Simulate offline from logged samples. |
| Weighted composite suite-strength score | 5–10h | At n=30 the weights are arbitrary and overfit. Mutation LCB is primary; the rest are diagnostics until each demonstrates *incremental* prediction of hidden failure. |
| Assertion-density / test-count gating | 2–4h | Trivially inflated by generated suites; ten duplicated assertions are not stronger than one. Descriptive covariate only. |
| OpenRouter integration this month | 2–3 days | Diversity garnish, not a width source (see Part 6). Adds a failure surface before the eval runs. |
| Policy ladder beyond 2 rungs | ~5h | Keep Verified + Example-checked. Fast contradicts the premise; Thorough is gated on numbers that don't exist. |
| Per-step scaffolding checkpointing/rendering | ~4h | Final-only grading is settled; intermediates have no measured consumer. |
| UI polish before the eval ships | 1–2 days | The eval never sees Streamlit. |
| Per-tier claims in the write-up | 0h | Keep tier as a covariate. Classic-vs-novel family is the meaningful split instead. |
| Python 3.9 → 3.12 migration **before** the pilot | — | Correct in the abstract, wrong now: it changes `_SANDBOX_RULES`, the Executor prompt, and 278 offline checks — i.e. the environment your registration describes. Do it after. |
| `sandbox-exec` *enablement effort* | 1–2 days | Apple-deprecated and leaky; in-process FS jail + rlimits + cwd jail carry accident containment. **But keep the 5-minute check** — see Part 6. |

**Two cuts I disagree with:**

- **Vacuous-suite auto-regeneration.** One reviewer wants it cut as "spending scarce API to launder a verdict." It doesn't launder anything — the regenerated suite is still audited and the candidate must still pass. A vacuous suite is the Planner's bug, and making the user re-roll to absorb your bug is bad product. Keep it, cap at one roll, and log the vacuity rate. If it exceeds ~20%, the fix is the prompt, not the retry.
- **Diversity-quartile keep criterion.** Comparing hidden fault detection between top and bottom diversity quartiles, coarse-matched on mutation score, is 7 suites against 7 with matching at n=30. Not estimable. Cut the criterion, keep the census as a descriptive covariate.

---

## Part 6 — Corrections to reviewer claims

Things four reviews got wrong, overstated, or miscosted. Recorded so they don't get re-absorbed as settled fact.

**The scope arithmetic.** Every review priced items individually and none summed them. Round 2 returned 7–9 dev-days and added 12–14. Round 4 proposed 123–198 hours (15–25 dev-days) against ~20 remaining, before running anything. **Per-item pricing is what hides the total.** Cap additions at Part 1; everything else waits for data.

**Reviewer keep-criteria thresholds are invented, not measured.** The *form* — pre-committing a kill switch before building — is the best discipline in any of the reviews. The specific numbers are not measurements. "Keep if ≥3 of 20 suites" has a CI spanning roughly 3%–38%. "Precision ≥95% from 50 labeled properties" is unestablishable; at 48/50 the interval is about [89%, 99%]. Use them as tie-breakers, never as decision rules.

**The tracer performance problem.** Covered in Part 4 — `sys.settrace` at 10–50× against a 15s budget makes the eval measure the tracer. One review proposed five diagnostics on a shared tracer without noting this.

**Corpus-mutant circularity.** Covered in Part 4. Best idea in the reviews, wrong population as specified.

**Ratings tables are theater.** Eleven rows with decimal scores ("8.5/10", "7/10 → 9/10 if") have no measurement behind them. Ignore them, including the flattering rows.

**Free-tier numbers are unverified and were treated as load-bearing.** One review built an entire allocation architecture on OpenRouter ≈50 requests/day per account, a $10-credit threshold raising it to ~1000/day, and Gemini flash ≈250/day. It hedged, then relied on them. **The structural argument is probably right and worth keeping: width comes from the provider whose limits are per-model (Groq multi-model), not from K providers behind one router with per-account caps.** The numbers themselves need checking against current published limits before any budget rests on them. Do not solve Gemini scarcity with multiple Google accounts — that's ToS limit evasion and a stupid way to lose the project.

**The `sandbox-exec` cut goes one step too far.** Skipping the *enablement effort* is defensible now that the in-process FS jail exists. But run it once from a plain terminal anyway: your honest layer report makes a claim about which layers engaged, and you currently don't know whether that claim is true on bare metal. With the OS layer off, hardcoded absolute-path writes from native Python FS calls reach your real Desktop. **Five minutes, and it's about your machine, not your eval.**

**The stdout secret channel is real but misplaced.** Env scrubbing stops credentials *reaching* the child; nothing stops secrets *leaving* via captured output → LLM context → memory JSON. In the eval this is near-zero risk (scrubbed env, synthetic tasks, no credentials present). It's a production item, to fix before any real user points this at a real folder. One review listed it beside eval-integrity blockers, which overstates urgency.

**T5 "fire the Planner" at 18–28 hours is fiction.** A deterministic task parser + partition generator + property compiler + metamorphic catalogue is the sufficiently-smart-test-generator problem. It looks tractable only because *your own generator wrote the tasks* — the structure being parsed is structure you put there. Fine as an eval-only experiment on three families if A ≈ A′; do not believe the production transfer story.

Two things from that section are worth keeping regardless of the Planner's fate: **commit-reveal fixtures** (hidden input/output cases hashed and committed before execution, contents kept outside Executor context) and a **`property-checked` verdict tier** — a rung below APPROVED for tasks where no strong semantic claim is earnable. That tier is the honest surface for the design asymmetry already in the brief, and it costs almost nothing.

**Suite-teeth conditioning has a validity boundary.** It's a legitimate covariate for B−A′ because those arms share a suite, making it pre-treatment for that contrast. It is undefined for Arm A. Don't report an A-inclusive model conditioned on it.

**Two distinct T6 mechanisms, both real, don't conflate them.**
- *Template-class contamination:* classic task families (CSV pivots, ciphers, parsers) sit in every model's training distribution regardless of instance synthesis. This caps Arm A's failure rate, so effects are estimated on the non-memorized residue and **compressed toward zero**. Fix: the novel-compositional family (B9).
- *Hidden-suite/reference coupling:* the hidden suite is independent of the *candidate* but not of the *reference or generator*. It can reward the generator's preferred interpretation over correctness, biasing the truth label **in either direction** — understating false-APPROVED when hidden tests repeat the reference's favored partitions, or falsely rejecting valid candidates where the reference resolved an ambiguity one undocumented way. Pairing, interleaving, hidden grading, and pre-registration do **not** remove this, because the same coupled oracle defines truth for every arm. Fix: Tier 4 alternative-suite regrade.

---

## Part 7 — Scored predictions (pre-registered, before results)

Recorded so reviewers and I can be graded against the data rather than remembered charitably.

| # | Source | Prediction | Falsified if |
|---|---|---|---|
| P1 | Reviewer 2 | On mid-tier tasks where A′ hidden pass rate ∈ [0.25, 0.6], **B beats A′ by < 10 percentage points** | B−A′ ≥ 10 → repair is the main effect, spend budget on repair depth. B−A′ ≤ 0 → the repair loop is net-negative and should be cut, not tuned |
| P2 | Reviewer 3 | **B's false-APPROVED rate is ≥ 5 points LOWER than A′'s**, and ≥25% of B's successful repairs show no prior assertion-level semantic progress | Either clause fails |
| P3 | Mine | **B's false-APPROVED rate is HIGHER than A′'s** | B's false-APPROVED ≤ A′'s |
| P4 | Mine | B−A′ is < 10 points on classic template families and **larger on the novel-compositional family** | Novel-family effect ≤ classic-family effect |

**P2 and P3 directly contradict, and that contradiction is the sharpest empirical question in the project.** My reasoning: B repairs until the visible suite passes, so B's approved set is systematically enriched with candidates *tuned to the visible suite* — which is the overfitting mechanism the repair-generalization gap exists to detect. A′ never saw those tests and cannot have overfit them. Reviewer 3's own T2 analysis supports my direction more than its own prediction.

You have reached the point where competent reviewers disagree with each other on your central claim. Only your data breaks the tie. **That is the signal to stop reviewing and start running.**

---

## Part 8 — Execution order

| # | Step | Cost | Gate |
|---|---|---|---|
| 1 | Clear both Sprint-0 blockers **in the keyed environment** | ~20 min | Neither is "environment-blocked" once the eval has keys |
| 2 | Build Part 1 (13 items) + free hygiene | ~4 dev-days, 0 API | Nothing proceeds until these land |
| 3 | Write the registration: arms, four estimands, one primary endpoint, power statement, k=1-is-discarded | ~2h | Timestamp and commit it before any run |
| 4 | Pilot: 30 tasks × 4 arms, interleaved, k=1 | ~1 paced day | **Numbers discarded.** Purpose is plumbing truth |
| 5 | k=3 run | ~1–2 paced days | The real measurement |
| 6 | Compute: primary endpoint, four estimands, Gap_B−Gap_A′, suite validity, teeth-conditioned B−A′, transition taxonomy, classic vs novel | ~1 day, 0 API | Score P1–P4 |
| 7 | Let numbers restructure the system: keep/fire the Planner (A vs A′ + offline CodeT sim), keep/cut repair (B vs A′@3), width go/no-go (Groq multi-model, not OpenRouter), early-stop policy from the transition table | — | Every architecture decision now has evidence behind it |
| 8 | Tier 4 sensitivity analyses, then write up publicly — **disappointments included** | ~1 week | A write-up with only good numbers reads as selection |

### Step 1 in detail — both are one-minute checks, and both corrupt silently

- **`gemini-3.6-flash`**: unverified assertion living in a code comment. One API call settles it — 404 = bad slug, 401/403 = bad key. `gemini-flash-latest` is the safer pin. **If the slug is wrong, every Planner call fails, failover routes everything to Groq, and your eval runs single-provider while reporting a two-provider system.** A config error becomes contaminated evidence that nobody would notice without provider-actually-used logging (B4).
- **`sandbox-exec` on bare metal**: run from a plain terminal, outside any nested sandbox, and read the layer report. This is about your Desktop, not your eval.

### Budget reality

The scarce currency is **Gemini requests**, and Gemini serves only the Planner at ~1–2 calls/task. Everything chatty — Executor, repair rounds, A/A′/A′@3 — is Groq, the loose pool. With A′ sharing B's spec (B6), 30 tasks × 4 arms × k=3 is roughly 90–120 Gemini calls and a few hundred Groq calls. **The eval fits inside 1–2 days of paced free budget.** The Q7 fear of budget collapse was misallocated; your existing provider-role assignment already makes this work.

Audit the "~13 requests per solved task" figure with per-stage telemetry on the first 10 tasks. If it's real, calls-to-green is your largest budget lever — context engineering that removes one repair round saves ~8% of a task's budget.

---

## Part 9 — Standing conclusions

1. **The engineering is ahead of the evidence.** That is now the only real problem. Four review rounds have produced a well-built system with an eval that, as originally specified, could not have proven its own thesis even if the thesis were true.
2. **Every fix in Part 1 is cheap now and impossible after the first run.** Definitions, controls, and logging cannot be retrofitted onto data collected without them.
3. **Your effective sample unit is the template, not the task.** Architecture decisions (single artifact, final-only grading, stdlib-only, 15s) shaped the generator, so the eval validates the system on tasks generated in the system's own image. That is legitimate for a **mechanism check** and illegitimate for a **market claim.** Label which one you're making.
4. **Execution, not opinion — including for your own diagnostics.** Attribution comes from running suites against things whose correctness you already know: the reference (broken suite = Planner's fault, and it yields suite validity rate for free), the stub (import failure = broken suite), a double-run (verdict flip = flaky), and the reference in-sandbox (known-good code failing = environment). What remains after those four — valid suite, stable, environment fine, fails visible but passes hidden — is the genuinely interesting residue, and *only then* does artifact reading have a narrow job.
5. **Run the reference-vs-suite check before building attribution at all.** It may make most of the attribution problem disappear, and it's one execution per suite.
6. **User examples are the cheapest intent-grounding available.** Most users can't write tests but can paste three input→output pairs. "Example-checked" — examples protected as artifacts the Executor cannot see or weaken — is the bridge from "passed generated tests" to "passed *your* intent," which is exactly the gap the design-asymmetry paragraph admits is unearned. It is also the one thing a free chat tab structurally cannot do: run *my* examples against *my* files.
7. **No moat, and that's fine for a solo $0 project.** The compounding assets, ranked: the eval harness + generator + pre-registered results (publishable whichever way the numbers land — "repair loops mostly overfit visible tests on free models" is a *more* interesting result than parity); the evidence report **as downloadable files**, not a Streamlit panel — a report that ships as an artifact zip a skeptic can re-run is proof, one rendered in a panel is decoration; and the failure-signature corpus, which is the training set for every future policy and router.
8. **The name is now factually wrong.** Planner → Executor → deterministic escalation → harness is a verification pipeline, not a multi-agent system. The current name attracts multi-agent-hype readers, invites the wrong critique ("where's the agent coordination?"), and undersells what's distinctive. Ten minutes, after the pilot.
9. **Stop sharpening the brief. Spend the requests.**


# Report — Arm A vs Arm B on the family-first task set

**Status: the infrastructure is built and validated end-to-end; the model
numbers are not measured.** No provider key is present in this environment
(`GEMINI_API_KEY` and `GROQ_API_KEY` are both unset) and outbound HTTPS to both
providers is denied by the sandbox — verified on 2026-08-29:

```
deny network-outbound generativelanguage.googleapis.com:443 (user denied)
deny network-outbound api.groq.com:443 (user denied)
```

So every table below that compares models is empty, with the command that fills
it. Inventing plausible pass rates for a pipeline whose entire premise is
"stop believing claims that were never executed" would be the exact failure
this project exists to remove. `PREDICTIONS.md` is already frozen, so the
comparison stays honest whenever the run happens.

To fill this report:

```bash
export GEMINI_API_KEY=... GROQ_API_KEY=...
python3 eval/run_eval.py --arm both --per-family 2 --repeats 1
```

That is 36 tasks × 2 arms. Results land in `eval/results/seed-0/`, one JSON per
run, and the sweep resumes on restart, so it can be interrupted.

## What is measured and verified

The task generator and the eval runner were exercised in full, offline.

| Check | Result |
| --- | --- |
| `gen_tasks.py --self-check --per-family 12`, seeds 0/1/7/42/1234 | 216 tasks each, **0 problems** (1080 total) |
| Every hidden suite passes its own reference | yes, all 1080 |
| Every hidden suite **fails** a stub that returns `None` | yes, all 1080 — no suite is vacuous |
| No prompt contains `assert`, a `def`, or a fenced block | yes, all 1080 — the reference and the suite cannot leak into what the pipeline sees |
| Same-prompt variants agree on behaviour | yes — checked by running each reference against its same-prompt siblings' suites |
| Byte-identical output for a repeated seed | yes |
| `run_eval.py` over all 18 families, both arms, stub model | 72 runs, 196 s, **0 crashes, 0 errors** |
| Governor sleep counted inside the reported wall clock | yes — e.g. `seconds=5.03, seconds_sleeping=4.69` |
| 429 handling | `Retry-After` read from the response header, retried, logged; a non-429 error is not retried |
| Resume | rerunning a completed sweep does 0 runs |

### The false-APPROVED detector works

This is the one result that does not need a real model, because it is a property
of the measurement, and it is the number the pipeline cannot self-report.

With `--stub broken` (a stand-in whose Planner writes a **thin but non-vacuous**
suite, and whose Executor is correct on ordinary inputs and wrong on one edge
case), across all 18 families, one variant each:

| | Count |
| --- | --- |
| harness said `APPROVED` | 16 |
| of those, failed the hidden suite | **15** |
| harness said `REVISE` (the thin suite caught it) | 1 |
| genuinely correct | 1 |

So a suite that passes `harness.audit_tests` — it parses, it asserts, it imports
a concrete name, and it fails against a stub — can still miss a real defect 15
times out of 16. The audit rules out suites that test *nothing*; it cannot rule
out suites that test *too little*. That is precisely why grading in this report
is done by the hidden suite and never by the Planner's.

(One family, `validation`, survived the mutation: the textual mutation happened
to be behaviourally inert for the tested inputs. That is a limitation of the
stub, not of the grader.)

## The task set

18 families — the family is the experimental unit, not the tier.

| Tier | Families | Hidden checks per task |
| --- | --- | --- |
| 1 — easy single-function transforms | `aggregation`, `byte_formatting`, `ranking`, `run_length_encoding`, `text_normalization` | 11–14 |
| 2 — multi-branch logic with real edge cases (**target band**) | `date_arithmetic`, `grouping`, `interval_logic`, `path_canonicalization`, `query_canonicalization`, `tiered_pricing`, `validation`, `version_ordering` | 12–26 |
| 3 — small parsing and state problems | `config_parsing`, `delimited_parsing`, `expression_eval`, `graph_traversal`, `template_expansion` | 15–28 |

Each family is parameterised, so variants differ in behaviour, not just in
inputs: `interval_logic` either merges touching spans or does not,
`expression_eval` uses floor or true division, `config_parsing` has three
independent switches. The prompt always states which, and that is checked
automatically — a variant whose prompt does not distinguish it from a sibling
would be graded on a rule the solver was never told.

## Pass rate against the hidden suite — NOT MEASURED

Per tier. There is deliberately no single headline number: a pooled rate is a
weighted average of whichever tiers have the most families.

| Tier | Arm A | Arm B | Gain | Predicted gain |
| --- | --- | --- | --- | --- |
| 1 | — | — | — | +8 |
| 2 | — | — | — | +22 |
| 3 | — | — | — | +18 |

Per family, which is the comparison that matters:

| Tier | Family | Arm A | Arm B | Predicted A | Predicted B |
| --- | --- | --- | --- | --- | --- |
| 1 | `aggregation` | — | — | 90% | 95% |
| 1 | `text_normalization` | — | — | 85% | 90% |
| 1 | `run_length_encoding` | — | — | 80% | 90% |
| 1 | `byte_formatting` | — | — | 70% | 80% |
| 1 | `ranking` | — | — | 65% | 75% |
| 2 | `grouping` | — | — | 65% | 85% |
| 2 | `interval_logic` | — | — | 55% | 80% |
| 2 | `validation` | — | — | 55% | 75% |
| 2 | `date_arithmetic` | — | — | 45% | 70% |
| 2 | `tiered_pricing` | — | — | 45% | 65% |
| 2 | `query_canonicalization` | — | — | 40% | 60% |
| 2 | `version_ordering` | — | — | 35% | 60% |
| 2 | `path_canonicalization` | — | — | 30% | 50% |
| 3 | `graph_traversal` | — | — | 35% | 55% |
| 3 | `template_expansion` | — | — | 30% | 50% |
| 3 | `delimited_parsing` | — | — | 25% | 45% |
| 3 | `config_parsing` | — | — | 15% | 30% |
| 3 | `expression_eval` | — | — | 10% | 25% |

`run_eval.py --summarise-only` prints exactly these buckets from the result
files, so filling the table is transcription, not judgement.

## False APPROVED — NOT MEASURED

A **false APPROVED** is a step the harness marked `APPROVED` that then fails the
hidden suite. Arm A has no verdict of its own, so this applies to Arm B only.

| Tier | `APPROVED` | of those, failed hidden | rate |
| --- | --- | --- | --- |
| 1 | — | — | — |
| 2 | — | — | — |
| 3 | — | — | — |

Predicted: 18% of `APPROVED` steps.

### Classification protocol, fixed in advance

`run_eval.py` detects false APPROVEDs automatically but deliberately does not
classify them — the two causes need different fixes and telling them apart
means reading the SPEC against the prompt. The rule, written down now so it is
not chosen to suit the answer:

1. Open the run log named in the record (`log`), and read the Planner's `SPEC:`
   beside the task's prompt.
2. **Spec misread the prompt** — the SPEC contradicts or omits a rule the prompt
   states. The generated suite then tests the wrong thing correctly, and the
   Executor was graded against a spec nobody asked for. Fix lives in the
   Planner prompt.
3. **Tests too weak** — the SPEC is faithful, but the suite has no case for the
   rule the hidden suite caught. Fix lives in test generation (or in the audit).
4. If both apply, count it as **spec misread**: a wrong spec makes the suite's
   coverage moot.

| Cause | Count | Share | Predicted share |
| --- | --- | --- | --- |
| Spec misread the prompt | — | — | 60% |
| Tests too weak | — | — | 40% |

## Time and calls — NOT MEASURED

Wall clock includes rate-limit sleep and 429 backoff; `run_eval.py` adds the
governor's sleep into the recorded seconds on purpose, because a benchmark that
excludes its own waiting reports a speed nobody can reproduce.

| Measure | Arm A | Arm B | Predicted (Arm B) |
| --- | --- | --- | --- |
| median seconds to green | — | — | 25 s |
| p90 seconds to green | — | — | 90 s |
| median calls to green | — | — | 3 |
| p90 calls to green | — | — | 6 |
| median calls per task | — | — | 4 |

## Rate limiting — NOT MEASURED

| Provider | 429s | recovered from header `Retry-After` | recovered by backoff | unrecovered |
| --- | --- | --- | --- | --- |
| gemini | — | — | — | — |
| groq | — | — | — | — |

Client-side governor: 12 calls/min gemini, 25 calls/min groq, burst 2. Predicted
0 on gemini and 5–15 on groq over a 216-task run. Any *unrecovered* 429 is a bug
in the governor and is to be reported as one, not as a provider problem.

## Escalation ladder — NOT MEASURED

Of Arm B steps that fail their first harness run:

| Rung | Reached green here | Predicted |
| --- | --- | --- |
| `repair` | — | 45% |
| `alternate` | — | 15% |
| `fresh` | — | 10% |
| exhausted (`REVISE`) | — | 30% |

The `fresh` rung is the one most likely to be dead weight: it is the most
expensive and it deliberately withholds the traceback. Below 5% the honest
conclusion is to cut it.

## Predictions vs actuals

Empty until the run happens. `PREDICTIONS.md` is frozen and is not edited to
match. Every miss gets a row here with the reason.

| Prediction | Predicted | Actual | Verdict |
| --- | --- | --- | --- |
| Arm B beats Arm A on every tier | — | — | — |
| Largest gain in tier 2 | +22 | — | — |
| False-APPROVED rate | 18% | — | — |
| Majority cause is "spec misread" | 60% | — | — |
| `fresh` rung earns its place | 10% | — | — |
| Arm B costs ~4× the calls of Arm A | 4× | — | — |

The four falsification conditions from `PREDICTIONS.md` — tier-2 gain under 10
points, false-APPROVED over 30%, `fresh` under 5%, Arm A over 60% on tier 3 —
are to be checked explicitly and reported even when they are met.

## What one honest caveat costs

`--repeats 1` is the default and measures **one draw per task**. At 5–8
families per tier, a per-family difference of one task is noise. Nothing here
separates a capability gap from sampling variance; that needs `--repeats 3` or
more, and the per-family tables should be read as ranking families by
difficulty rather than as pass rates with error bars.

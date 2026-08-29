# Pre-registered predictions

Written **before** `run_eval.py` was executed against any provider, and before
any task was shown to a model. Numbers are committed here so that "the pipeline
helped" cannot be decided after the fact by picking the comparison that flatters
it.

Frozen on 2026-08-29 against `gen_tasks.py` at 18 families (5 tier 1, 8 tier 2,
5 tier 3). Verified before freezing: `python3 eval/gen_tasks.py --self-check
--per-family 12` → 216 tasks, 0 problems, on seeds 0/1/7/42/1234.

**This file is not edited after results exist.** Where a prediction is wrong,
the miss is recorded in `REPORT.md` under "predictions vs actuals", with the
reason, and this file stays as written.

## The two arms

| Arm | What runs | Repair | Sees any tests |
| --- | --- | --- | --- |
| A | one Executor call on the raw prompt | none | no |
| B | mode 3: Planner → Executor → harness, escalation ladder up to `MAX_REVISION_ROUNDS` | yes | Executor never; the harness grades against the Planner's audited suite |

Both arms are graded **only** against the hidden reference suite from
`gen_tasks.py`. Arm B's own generated suite decides what it repairs against; it
never decides whether the task counts as passed. That separation is the whole
point — a pipeline that grades itself can report any number it likes.

## Headline prediction

Arm B beats Arm A on every tier, by the most in tier 2, and by the least in
tier 1 where there is little room to move. Specifically: **+8 points tier 1,
+22 points tier 2, +18 points tier 3.**

The reasoning, so it can be checked rather than just scored:

- Tier 1 is close to ceiling for a single call. Repair can only recover the
  occasional off-by-one or misread flag, so the gain is small by construction.
- Tier 2 is the target band because the failures there are *specific* — a
  boundary, an empty input, a `ValueError` that should have been raised — and a
  concrete failing assertion is exactly the input a model repairs well from.
- Tier 3 failures are more often structural (a tokenizer that cannot express
  the grammar). Rounds 1 and 2 hand back the same traceback, which tends to
  produce variations on one wrong design; the `fresh` rung exists for this and
  should recover some, but fewer than in tier 2.

## Per tier

Pass rate against the hidden suite, first attempt only (`--repeats 1`).

| Tier | Arm A | Arm B | Predicted gain |
| --- | --- | --- | --- |
| 1 (n=5 families) | 78% | 86% | +8 |
| 2 (n=8 families) | 45% | 67% | +22 |
| 3 (n=5 families) | 22% | 40% | +18 |
| pooled (reported, never used to conclude) | 49% | 65% | +16 |

The pooled row is recorded only because someone always asks for it. It is
dominated by whichever tier happens to have the most families, so it is not
evidence about anything.

## Per family

The family is the experimental unit, so the per-family prediction is the real
one. `raises` counts the exception cases in the hidden suite — the part a
single-shot answer most often omits entirely.

| Tier | Family | Checks | Arm A | Arm B | Why |
| --- | --- | --- | --- | --- | --- |
| 1 | `aggregation` | 11 | 90% | 95% | one pass over a list; the only trap is an empty input |
| 1 | `text_normalization` | 13 | 85% | 90% | ordering of strip/collapse/case is easy to get right |
| 1 | `run_length_encoding` | 12 | 80% | 90% | the `always_count` flag is the whole difficulty |
| 1 | `byte_formatting` | 14 | 70% | 80% | 1024-vs-1000 and rounding at the boundary |
| 1 | `ranking` | 14 | 65% | 75% | tie-breaking and the score floor are usually half-read |
| 2 | `grouping` | 12 | 65% | 85% | shape is simple; the missing-key case is the failure |
| 2 | `interval_logic` | 17 | 55% | 80% | touch-merge is a single branch, and the assert names it |
| 2 | `validation` | 18 | 55% | 75% | weighted checksum; arithmetic slips repair cleanly |
| 2 | `date_arithmetic` | 15 | 45% | 70% | a custom weekend, and off-by-one on the start day |
| 2 | `tiered_pricing` | 16 | 45% | 65% | bracket boundaries; wrong at exactly one edge |
| 2 | `query_canonicalization` | 18 | 40% | 60% | duplicate-key keep-first/keep-last is rarely guessed |
| 2 | `version_ordering` | 20 | 35% | 60% | prerelease ordering, and numeric-vs-string comparison |
| 2 | `path_canonicalization` | 24 | 30% | 50% | escaping above root is the case that is skipped |
| 3 | `graph_traversal` | 15 | 35% | 55% | topological sort is known; the tie rule is not |
| 3 | `template_expansion` | 22 | 30% | 50% | brace doubling, with `str.format` explicitly banned |
| 3 | `delimited_parsing` | 21 | 25% | 45% | quote handling, with `csv` explicitly banned |
| 3 | `config_parsing` | 25 | 15% | 30% | continuation lines plus four distinct raise cases |
| 3 | `expression_eval` | 28 | 10% | 25% | precedence and floor-vs-true division, hand-rolled |

## False APPROVED

The number that matters most, because it is the one the pipeline can lie about.
A **false APPROVED** is a step the harness marked `APPROVED` that then fails the
hidden suite.

Predicted overall false-APPROVED rate for Arm B: **18% of APPROVED steps**,
split by hand into the two causes the report must separate:

| Cause | Predicted share of false APPROVEDs | Predicted by tier |
| --- | --- | --- |
| **Spec misread the prompt** — the Planner's SPEC contradicts the prompt, so the suite tests the wrong thing correctly | 60% | rises with tier: ~40% of tier-1 cases, ~70% of tier-3 |
| **Tests too weak** — the SPEC is right but the suite misses the case the hidden suite catches | 40% | falls with tier |

The stub audit in `harness.audit_tests` catches suites that test *nothing*. It
cannot catch a suite that tests the wrong thing thoroughly, which is why the
prediction for "spec misread" is the larger share. If the split comes out the
other way — mostly weak tests — the fix is in test generation, not in the
Planner, and that is a materially different conclusion.

Arm A has no verdict, so it has no false APPROVEDs; its comparable number is
just its pass rate.

## Cost and time

Per task, Arm B, including rate-limit sleep:

| Measure | Prediction |
| --- | --- |
| model calls, task that passes on round 0 | 3 (planner, test writer, executor) |
| model calls, median across all tasks | 4 |
| calls-to-green, median over tasks that reach green | 3 |
| calls-to-green, p90 | 6 |
| wall clock, median | 25 s |
| wall clock, p90 | 90 s |
| Arm A wall clock, median | 6 s |

So Arm B is predicted to cost roughly **4× the calls and 4× the wall clock** of
Arm A for +16 points pooled. Whether that is worth it is a judgement, but it
should be made against the real ratio, not against an unmeasured impression
that verification is cheap.

429s: predicted 0 on gemini at one call per 4 s, and 5–15 across a 216-task run
on groq, all recovered by the governor. Any 429 that is *not* recovered is a bug
in the governor and is reported as one.

## Escalation ladder

Of Arm B steps that fail their first harness run:

| Rung | Predicted share that reach green here |
| --- | --- |
| `repair` (round 1) | 45% |
| `alternate` (round 2) | 15% |
| `fresh` (round 3) | 10% |
| exhausted, stays `REVISE` | 30% |

The `fresh` rung is the one with a real chance of being useless. It is
predicted at 10% and it is the most expensive rung; if it comes in under 5%,
the honest conclusion is that two rungs would do and the third should be cut.

## What would falsify the design

Stated in advance so the result cannot be reinterpreted into a success:

1. **Arm B does not beat Arm A on tier 2 by at least 10 points.** Tier 2 is the
   band the pipeline is built for. Below 10 points, execution-based repair is
   not paying for its 4× cost.
2. **False-APPROVED rate above 30%.** At that point `APPROVED` is not a useful
   signal and the audit needs to be stricter before the pipeline is worth
   using.
3. **`fresh` under 5%.** The rung is expensive and the ladder should shrink.
4. **Arm A above 60% on tier 3.** Then the task set is too easy and tier 3
   needs harder families before any of these numbers mean much.

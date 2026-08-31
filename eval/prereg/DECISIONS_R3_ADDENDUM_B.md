# Pre-registration addendum B to Revision 3

**Written 2026-08-31 after the pin probe ended on quota exhaustion: 19 of 36 plans, 57 of 108
draws.** Addendum A registered D-7 and D-8 *before* the final counts were visible. This addendum
resolves D-8 against the completed data and registers two things the run exposed.

Scope: adds D-9, D-10, D-11. Supersedes the **remedy** in D-8 case 3 only; D-8's classification
rule stands unchanged.

---

## D-8 resolved: case 3, asymmetrically poisoned

Final hidden-suite pass counts over the 19 planned tasks:

| candidate | pass | gate-approved | import failures | assertion failures |
|---|---|---|---|---|
| `openai/gpt-oss-120b` | **16** | 15 | **3** | 0 |
| `openai/gpt-oss-20b` | **16** | 14 | 1 | 2 |
| `qwen/qwen3.8-27b` | 14 | 13 | 2 | 3 |

Margin between the top two is **0**. `U` = 2 union-bearing specs in the run
(`aggregation-01`, `grouping-01`). `0 ≤ U`, so the margin is non-decisive.

`aggregation-01` failed for all three. **`grouping-01` — spec
`-> list[tuple[str, float | int]]` — import-failed for `openai/gpt-oss-120b` and PASSED for
`openai/gpt-oss-20b`.** A union-bearing task fell one way for one of the top two and the other way
for the other. That is D-8 case 3 exactly: **the ladder does not apply, and the pin is not decided
from this run.**

Direction of the contamination is worth stating because it is not symmetric in effect either: the
artifact cost 120b two tasks and 20b one, so the 16–16 tie understates 120b.

### A finding that changes the fix, not just the pin

`interval-logic-01`'s spec is `merge_spans(spans: list) -> list[tuple[int, int]]` — **PEP 585,
legal on 3.9, no union anywhere** — and `openai/gpt-oss-120b` still import-failed on it. The spec
cannot be the cause. The remaining explanation is that the Executor emitted 3.10-only syntax **on
its own initiative**, which is the default annotation style of every current model.

Consequences, registered:

1. **D-7 Part B (the `__future__` prologue) is the primary fix, not the backstop.** Part A's
   prompt rule and Part C's spec scanner both act on the *spec*; neither can see or prevent
   syntax the Executor invents. Only Part B covers this case.
2. **Part C's compliance rate is a measure of the Planner, not of the failure mode.** It must not
   be reported as an estimate of artifact incidence. Incidence is measured from import failures.
3. The cause of `interval-logic-01`'s failure is **not established** — the ledger stores
   `code_sha256` and not the code, so it cannot be recovered. It is recorded here as unexplained.
   This is the concrete cost of that ledger gap and the reason Sprint 10 closes it.

---

## D-9. The pin is re-run on the spec set held fixed, at zero Planner cost

**Supersedes D-8 case 3's remedy ("re-run the probe after the D-7 fix on a fresh `--out`").**

D-8 case 3 called for fresh specs because the run was contaminated. Reason to change: the
contamination is removed by D-7 Part B, which is a **harness-side** change. The specs are inputs,
not outputs, of the thing being measured. Regenerating them would remove the confound *and* add a
new variance source between the poisoned run and its replacement, on a comparison whose entire
purpose is to hold inputs constant across three Executors.

**Registered procedure:**

1. Reuse the **19 spec/test pairs already persisted** in
   `eval/results/pin-executor/ledger.jsonl`. They are stored as full text and are unchanged.
2. Regenerate only the **57 Executor draws**, under D-7 Part B. Old draws cannot be re-graded —
   the ledger holds `code_sha256` and not the code.
3. Planner cost: **zero Gemini requests.** Groq cost: 57 calls.
4. The reused specs were produced under the pre-Part-A prompt and 2 of 19 carry unions. That is
   accepted deliberately: under Part B a union no longer breaks the import, so those two tasks
   become discriminating again rather than being dropped.
5. `n` for the pin falls from 36 to **19**. Accepted: the pin selects an experimental input, it is
   not a registered endpoint, and Gemini requests are the binding constraint on every endpoint
   that is.
6. D-8's ladder becomes available on the re-run under its own conditions. If the re-run is again
   non-decisive and asymmetric, the pin goes to the ladder rather than to a third run — one
   re-run is the budget.

**Unchanged from D-6:** the pin is committed before calibration runs.

---

## D-10. A trusted visible suite can be wrong, and that rate is reported

**Registered: the `SELECTION_NO_APPROVAL` rate is reported alongside `SELECTION_NO_GATE`, and
suites that reject every correct draw are counted.**

D-4 requires reporting the `SELECTION_NO_GATE` rate, which covers suites classified `unusable` or
`vacuous`. It does not cover a suite that is `tests_status=generated`, `tests_trusted=True`, and
**arithmetically wrong**. The probe produced one:

`grouping-02`'s visible suite asserts

```
assert group_totals(data3) == [("B", 3.13), ("A", 3.01)]
```

where `A = 1.004 + 2.001`. Done in decimal that is `3.005 → 3.01`. In IEEE 754 the sum is
`3.00499...` and `round(sum, 2)` is `3.0`. **No correct implementation can pass that line.** All
three candidates passed the hidden suite; all three were gate-rejected. 3 of the 4 false
rejections in 57 draws come from this one task; excluding it the gate's false-rejection rate is
1 in 54.

`run_arm_a_prime3` already records this correctly — `SELECTION_NO_APPROVAL` is a distinct mode
from `SELECTION_NO_GATE`, and `selected_by_gate` is `False`. The gap is in **what Revision 3
requires to be reported**, not in the instrumentation.

Registered, for the same reason D-4 gives for `SELECTION_NO_GATE`:

1. **Report the `SELECTION_NO_APPROVAL` rate.** On such a task A′@3 returns draw 1 by position, so
   the arm is not best-of-3 there; and the blind gate-loss term `A′@3_gate − A′@3_oracle` is
   distorted by every one of them, in the opposite direction to `SELECTION_NO_GATE`. A design that
   reports one and hides the other reports half of the same bias.
2. **Report a wrong-suite count:** tasks where the suite is trusted, every draw passes the hidden
   suite, and every draw is gate-rejected. Computed post hoc from stored records; no extra spend.
3. **No suite is edited, regenerated, or excluded on the basis of this flag.** It is reported.
   Silently repairing a grader the arms are being compared through is the failure mode this whole
   design exists to avoid.
4. Direction of bias on `B − A′@3` from a wrong suite is **not predicted here.** A′@3 loses
   best-of-3; arm B spends repair rounds rewriting already-correct code against a false signal and
   may lose a task it had solved. Both arms are hurt, unequally, and which more is task-dependent.
   It is registered as a noise and validity term to be reported, not as a signed correction.

---

## D-11. Planner requests are the binding constraint on the schedule

**Registered as a measured fact about the environment, so that later choices about it are visible
as choices.**

The Gemini free tier returned, verbatim from the ledger:
`quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`,
`RESOURCE_EXHAUSTED`. **Twenty generate requests per day, per model.** The probe spent 104 HTTP
requests: 19 successful plans at 1 attempt each, and 17 failures at `attempts: 5, retried: 4` with
`seconds_backoff: 32.7` on every one.

**A per-day quota violation is not retryable and must not be retried.** Retrying 429s whose
`quotaId` contains `PerDay` turned a 20-request wall into 85 requests and roughly 9 minutes of
backoff sleep against a counter that resets tomorrow. Registered: the run aborts loudly at the
first `PerDay` exhaustion. Per-minute quotas continue to retry.

Planner requests required by the remaining plan, at one plan per `(repeat, task)` as
`eval/run_eval.py` currently loops:

| stage | Gemini requests | days at 20/day |
|---|---|---|
| pin re-run under D-9 | **0** | 0 |
| calibration, 36 tasks × 1 plan | 36 | 2 |
| k=1 smoke, discarded | 36 | 2 |
| k=3 grid, 36 × 3 repeats | 108 | 6 |
| **total** | **~180** | **~9, error-free** |

`eval/calibrate.py` already persists one spec per `(seed, task)` and reuses it, for the reason
stated in its own comment: draws answering two different specs make `d_t` a number about two
tasks. `eval/run_eval.py` has no equivalent load path.

**Left open, not decided here:** whether the grid should read one committed spec set instead of
re-planning per repeat. It would cut the grid from 108 requests to 36, and it is **not** a
neutral saving — holding the spec fixed across repeats removes Planner variance from every arm
equally, which makes the estimate more precise and conditional on one spec set rather than
generalising over the Planner's output distribution. That is a change to what `B − A′@3`
estimates and requires its own registered decision before it is implemented.

**Not in scope as a remedy:** multiple Google accounts, per Revision 2 Part G.

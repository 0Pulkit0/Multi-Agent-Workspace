# Sprint 4 — the three code changes that must land before the manifest freezes

Read this whole file before editing anything. Run `pwd` first and confirm you are in
`~/Desktop/Multi agent project`. You have worked in the wrong directory twice; check.

## Why these three, and why now

The eval pre-registration (`EVAL_PREREGISTRATION_REVISION_2.md`) pins an estimator and a
blocking checklist. Three of the things it assumes already exist do not exist in this
repo. All three change what a frozen manifest would describe, so they must land *before*
D2 (freeze manifest), not after.

Nothing else from Part D is in scope. Do **not** build the JSONL event schema (D3), the
hidden-suite hash + leakage audit (D5), reference validation (D6), the task-order shuffle
(D7), or the calibration sweep (D8) in this sprint. Do not touch `PREDICTIONS.md`.

## Ground rules (unchanged, still binding)

- Python 3.9. Standard library only. Dependencies stay `openai` + `streamlit`.
- No pytest. Offline checks are plain `assert` scripts run directly.
- Report the offline check counts before and after. **Baseline verified just now: 172
  harness + 141 pipeline = 313 passing.** No check may be deleted or weakened. If a check
  must change, say which one and why, in the report.
- Do not remove or rewrite: `CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU /
  ceiling-SIGKILL → timeout coercion, or `test_signal_death_is_explained`.
- API keys stay in the parent process. Never written to run JSON, never in a prompt. The
  `_child_env` allow-list must not regress.
- Do not "improve" these eight already-correct mechanisms: `_child_env`,
  `start_new_session=True` + `os.killpg`, `-B` / `PYTHONDONTWRITEBYTECODE`,
  `stdin=DEVNULL`, `_decode(errors="replace")`, `RLIMIT_FSIZE`, `_block_processes`
  neutering of `os.system` / `os.popen` / `os.exec*` / `os.spawn*` / `fork`, and the
  pruned repair context.
- If you disagree with a decision here, implement it and put the disagreement in the
  report. Do not silently substitute your own design.

---

## Task 1 — A measurement mode that actually turns silent failover off

**The bug.** `call_role` in `agents_core.py` (~line 184) builds
`order = [primary] + [every other provider]` and walks it on **any** `ProviderError`,
404 and 429 alike, emitting only a chat-log line. There is no way to disable it.

**Why it is the single most important run-time config in the study.** Arm B makes ~10× the
calls of the single-shot arms and absorbs ~10× the 429s, so each silent swap turns "arm B"
into a provider mixture whose composition correlates with rate-limit pressure, which
correlates with time of day. Task-level pairing cannot cancel an arm-correlated provider
mix. Worse, it compounds with the unverified `gemini-3.6-flash` slug: if the slug is wrong,
every Planner call 404s, failover routes the whole grid to Groq, and the eval runs
single-provider while reporting a two-provider system. That is contaminated evidence that
nobody would notice.

**What to build.**

1. Module-level `MEASUREMENT_MODE`, defaulting to `False`, read once from the environment
   (`MAW_MEASUREMENT=1`) at import, with an explicit setter for tests.
2. In `call_role`, when `MEASUREMENT_MODE` is on: `order` is exactly one provider — `prefer`
   if it is a configured provider, otherwise `primary`. Retry-with-backoff on 429 stays
   exactly as it is. On exhaustion, raise `ProviderError`; hard-fail the task.
3. `prefer` must keep working in measurement mode. The `alternate` escalation rung is a
   *deliberate, logged* provider switch that is part of arm B's definition. Killing that
   would change the treatment. Only the silent fallback dies.
4. Log per call, and return it up to the caller: requested provider, provider actually
   used, model ID, temperature, top_p, and a UTC timestamp. Pin `top_p` explicitly in
   `call_model` rather than leaving it to the endpoint's default — free endpoints override
   unpinned sampling params and that makes every number irreproducible.
5. `eval/run_eval.py` sets measurement mode on unconditionally, prints one line saying so,
   and records the `(requested, used)` pair per call. A run where the two ever differ in
   measurement mode is a bug in this task, not a finding.

**Checks to add.** Measurement mode with a dead primary raises rather than falling through;
measurement mode with `prefer` set uses `prefer` and nothing else; non-measurement mode
still walks the full order (the existing behaviour must keep its coverage); the logged
model ID matches `PROVIDERS`; keys never appear in the logged call record.

---

## Task 2 — Arms A′ and A′@3, and one shared Planner call

**The gap.** `eval/run_eval.py:335` is `ARMS = {"a": ..., "b": ...}`. That is the whole arm
set. The pre-registration's mechanism endpoint is `B_oracle − A′@3_oracle` and its
secondary contrasts are `A′@3_oracle − A′`, `B_gate − B_oracle`, `A′@3_gate − A′@3_oracle`,
`B_gate − A′@3_gate`, and `A − A′`. **Every one of those requires arms that were never
coded.** Today the only computable number is `B − A′`, which the pre-registration demotes to
"descriptive only, labelled compound." Without this task the eval cannot answer the question
it exists to answer.

**Arm definitions — copy these exactly.**

| Arm | Definition |
|---|---|
| `a` | Single-shot Executor on the raw task prompt. Zero retries. Extraction failure counts as a failure, not a rerun. (Already built; leave it alone.) |
| `a_prime` | B's Planner **spec only** → one Executor call. No repair. **Never sees the visible suite.** |
| `a_prime3` | Three independent `a_prime` samples, same gate, best-of-3. Matched call budget to B, zero feedback. |
| `b` | Full Planner → audited suite → Executor → harness → escalation ladder, max 3 rounds, ladder fixed. (Already built.) |

`a_prime3` is the control that separates "repair works" from "extra lottery tickets." If
B ≈ A′@3, the repair loop is resampling with extra steps and should be cut rather than tuned.

**One Planner call serves all of them.** Gemini requests are the scarce currency and the
Planner is the only Gemini consumer. Add an optional `plan=` parameter to
`run_workspace(...)`: when supplied, skip the Planner call and use the given plan text
verbatim (still running `extract_spec` / `extract_steps` / `_resolve_tests` over it). Then
`run_eval.py` makes **one** Planner call per `(task, repeat)` and passes the same plan into
`b`, while `a_prime` and `a_prime3` consume `extract_spec(plan)`.

Consequences to get right:

- Because the plan is shared, arm order within a task becomes free. Do not make `b` run
  first as a side effect of needing its spec — that would systematically place `b` earliest
  inside every task block.
- `a_prime` gets the spec and nothing else. `extract_spec` reads only the `SPEC` section, so
  the `TESTS` block should not reach it — **prove that with a check**, do not assume it. A
  leak here silently converts A′ into a weaker B and biases every contrast.
- `a_prime3`'s gate is the harness running the frozen visible suite. The generator still
  never sees the suite; the gate applies it. Tie-break for best-of-3 is **the first
  APPROVED in seeded order**. Not "shortest", not "highest visible score" — both correlate
  with hidden correctness and silently upgrade the gate toward the oracle.
- Draws must be independent: three separate Executor calls, distinct sampling, same spec.
  If you find yourself reusing one response, that is a bug that would make the resampling
  term read near zero and get misreported as "the Executor is near-deterministic."

**Store every candidate, including discarded ones.** This is the storage half of D4 and it
is irreversible — the oracle readings and the false-REJECT measurement are unrecoverable
without it. Each run record gains a `candidates` list: every `a_prime3` draw, and every
round's output in `b`, with its code, its round or draw index, its provider, and its gate
verdict. **Do not hidden-grade them inline** — the cold grading pass is deliberately
deferred to Part E, and grading ~700 candidates serially against a 15s ceiling is a
multi-hour job. Store now, grade later. Keep the existing inline `grade()` of the final
candidate as it is.

**Do not** compute any of the Part C contrasts in this sprint. Storage and arms only; the
bootstrap and permutation harness are Part E and operate on stored data.

**Checks to add.** `run_workspace(plan=...)` makes zero Planner calls; the visible suite
never appears in the A′ prompt; three `a_prime3` draws are three distinct Executor calls;
best-of-3 picks the first APPROVED in seeded order; a discarded candidate survives into the
stored record; the stub path exercises all four arms with no keys and no network.

---

## Task 3 — Retain the best candidate, not the last one

**The bug, confirmed by reading it.** `_verify_step` in `agents_core.py` (~line 672) loops
`for attempt in range(MAX_REVISION_ROUNDS + 1)` and overwrites `record.output`,
`record.code`, `record.verdict`, `record.failure_kind` and `record.failed_assertion` on
every pass. Whatever the last attempt produced is what gets returned. The `fresh` rung
deliberately discards the traceback and samples from scratch, so a round-1 candidate that
failed one late assert can be replaced by a round-3 candidate that fails on import.

**Why this blocks the freeze rather than following it.** The pre-registration's D2 registers
the policy-selected candidate as "the APPROVED one if it exists else **the last emitted**" —
which pre-registers this bug as the measurement convention. Freeze that and you have
formally registered a measurement of a pipeline you intend to change, and fixing it later
voids the manifest. Fix retention first, then register the fixed behaviour.

**The ranking key.** There is no assertion pass count available: the suite runs as a plain
script and dies at the first failing assert. So rank candidates by an explicit ordinal tuple,
higher is better:

1. `verdict == APPROVED`
2. `failure_kind == FAIL_ASSERTION` — the candidate imported and executed under test, which
   is strictly further than failing to import
3. `failed_assertion_line` (deeper into the frozen suite is further; missing → `-1`)
4. earliest round wins ties

Two honesty requirements on this, both of which go in a code comment and in the report:

- Line depth is a valid progress measure **only** if the frozen suite's asserts are
  order-independent. The suite is the same stored file every round, so the comparison is at
  least well-defined, but inter-assert state leakage would break monotonicity. The
  permuted-assert-order flakiness battery is the check, and it has not run. Until it does,
  call this a heuristic in the comment, not a score.
- Everything that is not an assertion failure collapses to the same rank, so among those,
  round 1 is kept. That is coarse and deliberately conservative. Say so; do not dress it up.

**What to build.** Track the best candidate across attempts and return it. Every round's
artifact is retained (Task 2's `candidates` list is the same storage), so repair uplift
versus regression becomes measurable rather than inferred. Record, on the step: which round
the returned candidate came from, and whether the final round was worse than the retained
one. That second field is the direct measurement of how often the escalation ladder is
net-harmful — currently invisible.

Do **not** change `MAX_REVISION_ROUNDS`, the `ESCALATION` tuple, or the escalation ladder's
behaviour. The ladder stays fixed during measurement; an adaptive policy would make B depend
on runtime noise and couple the measurement to the policy being evaluated. This task changes
only *which* of the candidates the ladder produced is returned.

**Checks to add.** A round-1 assertion failure at a deep line is retained over a round-3
import failure; an APPROVED candidate at any round wins outright; equal-rank candidates keep
the earliest round; the retained-round index and the "final round was worse" flag are both
recorded; existing `_verify_step` checks still pass unchanged.

---

## Report back with

1. `pwd` output.
2. Offline check counts before and after, and any check whose text changed, with why.
3. For each of the three tasks: what you changed, file and line ranges.
4. Anything in this brief you think is wrong, and what you did instead.
5. Anything you touched that this brief did not ask for.
6. Whether `MAW_MEASUREMENT=1` plus a deliberately bad Gemini slug now hard-fails instead
   of quietly running the whole thing on Groq. That is the specific failure this sprint
   exists to make impossible; demonstrate it with the stub path, not with a real key.

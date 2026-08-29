# Multi-Agent Workspace — Review Brief v2 (post-changes)

*This is the second round of external critique. Seven reviews have already run against v1. Most of what they raised has been built, settled, or deliberately rejected. To keep this round useful, everything settled is listed up front and marked CLOSED — please do not re-litigate it. The "Open questions" section is the actual target: these are the things nobody has answered yet, and where outside perspective is most wanted. Be blunt; the author wants disagreement, not validation.*

---

## What changed since v1 (all verified in the code, 2026-08-29)

- **The Orchestrator is deleted.** Mode 4 was Planner→Executor→Harness→Orchestrator — a model paid to narrate results the harness had already proven by executing the code, unable to change any outcome. Five of seven reviews said kill it; it is gone. Escalation is now a deterministic Python policy, no LLM in the path: on repeated failure → alternate provider → fresh from-scratch sample → give up and return artifacts with an honest UNVERIFIED verdict. `DEFAULT_MODE = 3`, mode 4 maps to 3 with an explanation instead of silently changing pipeline.
- **The harness grew a real capture ceiling.** `proc.communicate()` read the child's entire stream into the parent before truncation, so `while True: print("x"*1000)` could OOM the host app. Now the streams are drained incrementally with a 2 MB/stream hard byte cap; on breach the process group is killed and the result is marked `runaway-output` with distinct repair guidance. This was a real self-DoS, now closed.
- **Memory limits are attempted and honestly reported.** `RLIMIT_AS`/`RLIMIT_DATA` are set where the platform permits, but macOS does not enforce them, so the sandbox report now says `memory:unguarded-on-darwin` rather than implying a guarantee.
- **In-process filesystem-path jail.** Native Python write calls (`open(w)`, `os.remove`, `os.unlink`, `shutil.rmtree`, …) are wrapped to reject paths resolving outside the temp workdir. Reads unaffected. Stated honestly: this is **accident containment** against hallucinated destructive operations, not a security boundary (bypassable via `os.open`, `ctypes`, `importlib`).
- **One TIMEOUT verdict is now four.** Timeout during import, during test execution, during teardown, and "no suite, plain timeout" each get their own `failure_kind` and their own repair guidance. Previously one generic message covered all three.
- **The sandbox contract is in the Executor's prompt.** Python 3.9, standard library only, no pip/network, no `input()` (stdin is `/dev/null`), no subprocesses, writes only inside the working directory, 15-second wall clock. Single source of truth (`_SANDBOX_RULES`), referenced not duplicated.
- **Offline checks: 278 passing** (172 harness + 106 pipeline), plain `assert` scripts, no API keys, no pytest. Python 3.9, deps still only `openai` + `streamlit`.
- **Sprint 0 open items** (environment-blocked, not decided): whether `gemini-3.6-flash` is a real model ID (unverified comment in code; the API call that settles it needs a key the dev env lacks), and whether macOS `sandbox-exec` actually engages natively (it reports `os-level:unavailable` in nested sandboxes; a plain terminal run will settle it).

## CLOSED — do not re-review

1. **Kill the Orchestrator** — done, verdicts unanimous.
2. **Published HumanEval/MBPP comparisons are invalid** (contamination + pass@1-vs-pass@k harness mismatch) — we never planned to use them; the eval is fully synthetic and self-generated.
3. **In-process guards are not a security boundary** — agreed; the layer report says exactly which layers engaged, and the design goal is accident containment, not isolation. Real isolation (container/VM) is gated on a lane that needs it.
4. **false-APPROVED rate is the ceiling on the thesis** — accepted; the eval (Sprint 3) is built around it as the headline metric.
5. **Grade the final integrated candidate, not per-step artifacts** — decided; intermediate steps are scaffolding, checkpointed but not presented as verified.
6. **Best-of-N as default workflow is wrong; escalation plus adaptive stopping is right** — decided; width is gated on the eval's numbers and is not built yet.
7. **Research aggregation is not the first lane** (verification is weaker) — rejected for the first lane; pure Python functions + deterministic transforms stand.
8. **Cross-provider diversity claims need measurement, not assumption** — accepted; the eval counts provider unique-wins.
9. **Env scrubbing exists** (`_child_env` allow-list, no keys, no inherited credentials) — already built; five previous reviews described it as missing. It's not.
10. **Context poisoning / unbounded history** — already pruned (latest failure + original task; older steps degrade to titles; hard ceilings).

## Current architecture (what reviewers are actually reviewing)

**The pipeline is now: Planner (Gemini) → Executor (Groq) → Harness (execution, no LLM).**

- Planner emits a spec + numbered steps + an acceptance-test suite (plain `assert` files, stdlib only).
- Executor writes `solution.py`.
- Harness imports and runs the suite in a sandboxed subprocess: scrubbed env, `cwd` jail, no stdin, network/subprocess/`os.exec*`/`fork` all neutered in-process, CPU + file-size + attempted-memory rlimits, byte-capped incremental stream drain, process-group kill on wall-clock breach, per-phase timeout attribution, honest sandbox-layer report.
- Generated tests are audited (vacuous suites rejected → regenerated once), and the Executor cannot supply or weaken tests — the stored suite re-runs every round. User tests always win.
- **The core design asymmetry:** the harness proves "passed these tests in this environment," never "meets the user's intent." Any verdict above that line is a claim we have not yet earned.

---

## Open questions — the actual targets

**Q1. The mode selector is being replaced by verification policies.** Today the user picks 2 or 3 agents. The planned reframe: Fast (one candidate, basic execution) / Verified (one candidate + generated tests + repair) / Thorough (diverse candidates + stronger test audit) / User-tested (user tests authoritative, hidden from generators). Is a fixed 4-policy ladder the right shape, or should it be one continuous budget knob? Which policy should be the default for an unknown user, and is "User-tested" even reachable in a product where most users have no tests to give?

**Q2. The eval's discriminative power.** 30 synthetic tasks (3 tiers × 10), deterministic generator, pre-registered predictions, one repeat initially. Arm A = single-shot Executor; Arm B = full Planner→Executor→harness repair loop; both graded identically against hidden reference suites. Given literature-ish single-shot pass rates of p ≈ 0.25–0.6 on mid-tier tasks for these free models, is 30 tasks enough to *distinguish* the arms (CI that excludes zero), or is the honest answer "plumbing check only, conclusions need ≥100"? If the latter, what's the cheapest way to scale — more tasks, or more repeats of the same 30?

**Q3. Hidden tests without leaking.** The Planner writes the *visible* tests the Executor sees and repairs against. For the final gate we hold back hidden probes. Two designs: (a) exact-value holdout asserts derived from the reference implementation, (b) metamorphic/property probes — round-trip, idempotence, invariants, structural symmetries — that never reveal specific expected values. Which is the better default for synthetic tasks where the reference implementation is known, and does (b) actually catch the overfitting class (a) catches, or a different class?

**Q4. The false-REJECT attribution problem.** When the harness returns REVISE but the hidden suite passes, we need to know *why*: broken generated test file (Planner's fault), flaky/order-dependent assertion, environment mismatch, or a genuine candidate the repair loop killed early. We refuse per-step grading. Is post-hoc attribution from the artifacts (phase markers, traceback shape, which assertions ran) reliable enough, or do we need to instrument the harness to make attribution cheap?

**Q5. Adaptive stopping without a probability model.** Escalation is currently fixed: repair → alternate provider → fresh sample → give up, max 3 rounds. Should we stop earlier when the failure signature looks unrecoverable (e.g., same conceptual error across providers = cap failure, not repair), or is a fixed ladder more honest under small-sample uncertainty? What's the cheapest signal that distinguishes "repairable" from "capability ceiling" at round 2?

**Q6. Mutation score as a runtime gate.** We plan AST mutation scoring (kill a suite's mutants, measure kill-rate) as a test-strength audit. The interesting experiment: does mutation score *predict* false approval? If yes, we'd gate APPROVED on a mutation floor — but that's N extra executions per round under a tight free-tier budget. What's the right cost/benefit shape, and is there a cheaper proxy for "this suite is strong enough to gate on"?

**Q7. Free-tier allocation under a hard request budget.** The eval competes with real usage for the same daily requests. Gemini free + Groq free + (planned) OpenRouter free models, each with its own per-model pools. Given ~13 requests per solved task, what's the optimal split between baseline arms, repeats, task count, and repair depth? And is portfolio sampling across OpenRouter's separate per-model RPM pools genuinely viable, or does it collapse under real RPD ceilings?

**Q8. Positioning and the evidence report.** "Verification-first coding workspace" is the current framing: the deliverable is an artifact plus an auditable evidence report (which checks ran, which layers engaged, per-round tracebacks, verdict ladder). Is the evidence report itself a differentiator, or is this a portfolio project with no moat — and if the latter, what's the honest best use of the next month of effort?

*End of brief v2. Direct, specific disagreement is the goal — especially on Q2, Q3, and Q7.*

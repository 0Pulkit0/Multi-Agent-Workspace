# Multi-Agent Workspace — Review Brief

*A self-contained summary written to be handed to other AI models for critique. If you are reviewing this: please poke holes. The "Open questions" section at the end is where outside perspective is most wanted. Be blunt; the author wants disagreement, not validation.*

---

## One-line pitch

A **free** multi-agent AI workspace that tries to rival paid flagship models (GPT-5, Claude, Gemini Pro, etc.) on a **narrow set of task types** — not by having a smarter model, but by wrapping cheap/free models in real verification and test-time compute so the *system* is reliable even though each individual model is weaker.

## The core thesis (this is the thing to attack)

Free models can match a paid flagship **only where the answer can be cheaply checked or the task cleanly decomposes** — code with tests, data extraction, format-locked output, retrieval/aggregation. On those tasks, a verifier converts "how smart is the model" into "how many cheap attempts × a correctness check," and free models give effectively unlimited cheap attempts.

The bet rests on three real advantages a normal paid-API user doesn't exploit:
1. **Unlimited-ish cheap calls** (free tiers) → can afford best-of-N, retries, verification passes.
2. **Groq's inference speed** (hundreds of tokens/sec on Llama-3.3-70B) → a large latency budget for iterative edit→run→fix loops.
3. **Model diversity** (Gemini + Groq/Llama + OpenRouter free models are different families that fail differently) → cross-checking catches errors single-model self-consistency can't.

**The author is explicitly NOT claiming** this beats flagships broadly. On open-ended reasoning, novel synthesis, long-context coherence, and judgment, the paid base model wins and orchestration doesn't fix it. Also acknowledged: paid "models" are now agents too (their own tools + reasoning modes), so the honest comparison is free-multi-agent vs paid-multi-agent, and parity is only expected on the verifiable slice.

## Architecture

**Providers.** Gemini (`gemini-3.6-flash`) and Groq (`llama-3.3-70b-versatile`), both called through the OpenAI-compatible SDK so they share one code path. OpenRouter's free models are the planned expansion (one key, many models, also OpenAI-compatible). Cross-provider auto-failover on error.

**Roles / pipeline.** Configurable 2/3/4-stage pipeline:
- **Planner** — turns a vague prompt into a spec + numbered steps, and (as of Phase 2) an acceptance-test suite.
- **Executor** — writes the code/deliverable.
- **Verifier** — *not an LLM.* A real execution harness that runs the Executor's code in a sandbox and returns ground truth (pass/fail, tracebacks, failed assertions). This replaced the original LLM "Critic," which only gave opinions.
- **Orchestrator** (mode 4 only) — intended to break deadlocks / route. Currently the weakest-justified role (see open questions).

**Memory.** One JSON file per run (no cross-run state bleed). Bounded context passed between stages (older steps degrade to titles, hard ceiling).

**UI.** Streamlit: sidebar API-key inputs, mode selector, a live agent feed that renders incrementally, and a final deliverable panel.

**The single most important design choice:** the quality comes from *executing and checking*, not from the model's opinion. The verifier runs the code; APPROVED means "it actually ran and passed the acceptance tests," not "an LLM thought it looked good."

## Strategy: specialize, don't generalize

The plan is to categorize tasks by **shape**, not domain:
- **Verifiable** (code, math, extraction, strict format) → full generate→verify→fix loop. *Free shines here.*
- **Decomposable / breadth** (research, coverage) → parallel fan-out + merge. *Free is competitive.*
- **Open-ended** (ideation, judgment, creative) → single best model, honest low expectations vs paid.

A user-facing selector would expose friendly task labels ("Write & debug code", "Research a topic", "Convert data") that map internally to the right pipeline + verifier.

**First lane being built: code + execution** (Python), because it has the clearest verifier (tests), is the most measurable, and benefits most from Groq's speed.

## Current state (built and independently verified)

Built in Claude Code on macOS; verified in a separate environment. ~3,100 lines. **212 offline checks pass with no API keys** (114 harness + 98 pipeline).

- **Phase 0 (done)** — fixed prototype bugs: a fake agent selector (modes 2 and 3 were identical), a "live" feed that only rendered after the run finished, memory bleeding across runs, unbounded context growth.
- **Phase 1 (done)** — replaced the LLM Critic with a real execution harness: extracts the code block, runs it in a sandboxed subprocess (wall-clock timeout, CPU/file rlimits, in-process network + subprocess blocks, best-effort OS-level `sandbox-exec`), captures stdout/stderr/exit code, and feeds the real traceback back to the Executor. A concrete bug was found and fixed here: the CPU rlimit equalled the wall-clock timeout, so infinite loops died by SIGXCPU with an empty error message; fixed by giving the CPU limit headroom and naming the killing signal.
- **Phase 2 (done)** — APPROVED now means "meets the spec," not "didn't crash." The Planner emits an acceptance-test suite (plain `assert` files, no pytest dependency); the harness runs the tests with the solution importable; **vacuous tests are rejected** (a suite that passes against a stub whose functions all `raise NotImplementedError` is thrown out → marked UNVERIFIED); and the Executor **cannot weaken the tests** (the stored suite is re-run every round; any test file the Executor emits is ignored). User-provided tests always win over generated ones.

**Roadmap (not yet built):**
- **Phase 3** — best-of-N: generate several candidate solutions in parallel across providers, run all against the tests, keep the winner. (Spend the free budget on *width*, not a longer chain.)
- **Phase 4** — an eval harness of ~15–20 coding tasks with known tests, to *measure* pipeline vs a single free model (and vs a paid model if a key becomes available). This number is meant to be the actual proof of the thesis.
- **Phase 5** — a router that classifies incoming prompts and auto-selects pipeline + agent count.

## Key design decisions already made (challenge these)

- Verification is execution, not LLM opinion — the whole project hinges on this.
- More agents is *not* better: error compounds across hops (~0.9^n reliability), so a role is added only if it does a distinct job that prevents a nameable failure. Extra quality should come from **width** (parallel best-of-N under a verifier), not **depth** (more serial roles).
- Generated tests are audited for vacuousness and protected from Executor tampering, because a fake verifier is worse than none.
- Sandbox isolation is layered and **honestly reported** — it says which layers actually engaged rather than implying the strongest one. The in-process layer stops casual network/subprocess use; only the OS layer is a real boundary, and it's unconfirmed on real hardware so far.

## Constraints / context

- **Zero-cost is a hard requirement.** Free tiers only; no assumption of a paid API key. Solo developer, relatively new to building, on a Mac, Python 3.9, terminal/vim workflow. No heavy dependencies (currently only `openai` + `streamlit`).
- Rate limits on free tiers are RPM/RPD (requests per minute/day), not just tokens — a 4-agent × 3-round task is easily 15–20 sequential calls, so RPM is the real bottleneck, and this gets worse under best-of-N.

## Open questions — where outside perspective is wanted

1. **Is the core thesis actually sound?** "Free + verification rivals paid on verifiable tasks" — what's the strongest counterargument? Where does this quietly fail?
2. **Proving parity without a paid key.** Phase 4 wants to claim parity, but the developer has no paid API key. Is comparing against published flagship scores on public benchmarks (HumanEval/MBPP) legitimate, given contamination and harness-difference caveats? Is there a more honest way to demonstrate the claim?
3. **The economics under best-of-N.** If RPM is the binding constraint, does generating N parallel candidates + verification actually fit inside free-tier limits for real tasks, or does the whole speed/cost advantage collapse at practical N?
4. **Grading granularity (unresolved).** Currently one spec-level test suite runs against *every* step, which assumes each step is a self-contained program exposing the full interface. For genuinely multi-step tasks, early partial steps fail on import. Should the system grade only the final assembled solution and treat intermediate steps as unverified scaffolding? Or is per-step grading recoverable?
5. **Is the Orchestrator role worth keeping at all?** It's currently the least-justified stage. Is there a concrete, mechanical job for it, or should the pipeline be strictly Planner→Executor→Verifier?
6. **Running model-generated code safely.** The eventual product runs untrusted, model-written code on a user's own Mac. Is a layered in-process block + best-effort OS sandbox a defensible security model, or is local execution of generated code fundamentally the wrong architecture (should it require a container/VM, or a remote sandbox)?
7. **Is code the right first lane?** vs research-aggregation or bulk data-extraction — which task shape has the best odds of a *demonstrable* win against paid, for the least build effort?
8. **Is cross-provider failover even worth it?** Failover concentrates load on the surviving provider (it doesn't add capacity), and adds complexity. Keep it, or drop it in favor of simple retries + best-of-N spread across providers by default?

*End of brief. Reviewers: direct, specific disagreement is the goal — especially on questions 1, 3, 4, and 6.*

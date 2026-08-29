# Sprint brief — Multi-Agent Workspace (post-review reorder)

## Read this first

Working directory is `~/Desktop/Multi agent project/`. **Run `pwd` and `ls` before you touch anything and paste the output.** You have twice done work in the wrong directory on this project; if `harness.py`, `agents_core.py`, `app.py`, `test_harness.py`, `test_pipeline.py` are not in the directory you are standing in, stop and fix that before writing code.

Hard constraints, unchanged:
- **Python 3.9**. No new dependencies. No pytest — tests are plain `assert` files run as scripts. Current deps are only `openai` and `streamlit`.
- **212 offline checks must still pass** at the end (`python3 test_harness.py` = 114, `python3 test_pipeline.py` = 98). Report the before and after counts explicitly. If a count changes, say why. Do not silently delete checks to make things green.
- **Do not remove or rewrite these** — they are a previously-lost-then-restored fix: `CPU_GRACE_SECONDS` (harness.py:56), `_signal_name()`, the SIGXCPU/ceiling-SIGKILL→timeout coercion, and the `test_signal_death_is_explained` regression test. Confirm at the end that all four still exist.
- Five external reviews were run on this project. Several "bugs" they reported **are already fixed**. Do **not** "fix" the following, they are correct as written: `_child_env` env scrubbing (harness.py:431-448), `start_new_session=True` + `os.killpg` process-group kill (:524, :572-582), `-B`/`PYTHONDONTWRITEBYTECODE` bytecode suppression, `stdin=DEVNULL` (:517), `_decode(errors="replace")` (:585-590), `RLIMIT_FSIZE` (:267), `_block_processes` neutering of `os.system`/`os.popen`/`os.exec*`/`os.spawn*`/`fork` (:251-254), and the already-pruned repair context in `agents_core.py:665-666`. If you think one of these is actually broken, say so with evidence instead of changing it.

Order matters below. Do Sprint 0 → 1 → 2 → 3, and **stop at the end of Sprint 3**. Do not build best-of-N / parallel candidates. Do not build the holdout-test split or mutation scoring yet. Those are deliberately gated on Sprint 3's numbers.

---

## Sprint 0 — Two facts we cannot get any other way (do this first, ~10 minutes)

You are running natively on macOS, so you can settle two things I cannot.

**0a. Is `gemini-3.6-flash` a real model ID?** The comment at `agents_core.py:45-47` claims it was verified against ai.google.dev. That claim is unverified — treat it as unproven. Point `test_gemini.py` at the slug in `PROVIDERS` and make one real call. Report the raw HTTP status and error body:
- `404` / "model not found" → the slug is wrong. Switch `PROVIDERS["gemini"]["model"]` to the rolling alias `gemini-flash-latest`, retest, and fix the misleading comment.
- `401` / `403` → the slug is fine, the key is the problem. Say so and leave the slug alone.
- `200` → confirmed. Replace the comment with the date and the actual observed response.

**0b. Does the OS sandbox layer actually engage on real macOS?** Run `python3 test_harness.py` in a normal terminal (not inside any nested sandbox) and report the exact `sandbox_layers` list from a real execution. We need to know whether it says `os-level:sandbox-exec-...` or `os-level:unavailable`. **This is load-bearing, not cosmetic** — see Sprint 1c for why. If `sandbox-exec` is unavailable or the profile fails to load, report the precise error.

Report both results before moving on.

---

## Sprint 1 — Three verified bugs in the harness

These were found by reading the code, not by guessing. Fix all three.

**1a. Pipe-buffer OOM — a self-DoS on the app (highest priority).**
`_truncate` (harness.py:451) caps captured output at `MAX_STREAM_CHARS = 4000`, but only *after* `proc.communicate(timeout=timeout)` (harness.py:534) has already read the entire stream into the parent process's memory. `RLIMIT_FSIZE` caps *disk* writes, not *pipe* writes. So generated code doing `while True: print("x" * 1000)` inflates the parent (Streamlit) process until it OOMs. Our truncation protects the LLM context window; it does not protect the app.

Fix: drain the child's stdout/stderr incrementally with a hard byte ceiling (suggest `MAX_CAPTURE_BYTES = 2 * 1024 * 1024` per stream, as a new module constant) instead of buffering unbounded. On breach, kill the process group via the existing `_kill_tree` and mark the result so the Executor is told it produced runaway output. Preserve the existing wall-clock timeout behaviour and the existing `timed_out` semantics — do not regress the SIGXCPU handling. Add an offline check that a runaway printer is cut off, the parent survives, and the reason string explains it.

**1b. No memory cap, and the honesty layer doesn't admit it.**
`RLIMIT_AS` is never set, and macOS does not enforce it even if set — so a memory bomb (`[0] * 10**10`) will swap the user's entire machine. Attempt `RLIMIT_AS`/`RLIMIT_DATA` where the platform supports it, but **do not claim a guarantee you don't have**. Add memory to the sandbox-layer reporting as an explicitly unguarded dimension on macOS (something like `memory:unguarded-on-darwin`), consistent with the existing convention of reporting which layers actually engaged rather than implying the strongest one. Add a check asserting the memory dimension is reported honestly per platform.

**1c. No in-process filesystem path jail.**
Nothing wraps `builtins.open`, `os.remove`, `os.unlink`, `os.rename`, `os.rmdir`, or `shutil.rmtree`. The *only* thing preventing a write outside the temp workdir is the `sandbox-exec` profile's `(deny file-write*)` (harness.py:334) — the exact layer that reports `os-level:unavailable` in some environments. So when the OS layer is off, generated code doing `shutil.rmtree("/Users/<you>/Desktop")` runs unopposed. Env-scrubbing and `HOME=workdir` defeat `expanduser("~")` but not a hardcoded absolute path. Shell `rm` is already blocked; native Python filesystem calls are not.

Fix: inside the child runner (`_RUNNER_SOURCE`, alongside `_block_network` and `_block_processes`), add `_block_filesystem()` that wraps the write-capable entry points to reject any path resolving outside `_WORKDIR`. Reads stay allowed. Be honest in the docstring and in the layer report: this is **accident containment** against hallucinated destructive operations, not a security boundary — adversarial code can bypass it via `os.open`, `ctypes`, or `importlib`. Add checks for: an absolute-path write outside the workdir is refused, a relative write inside the workdir still works, and reads outside still work.

**1d. One TIMEOUT verdict currently means three different things.**
`_classify` (harness.py:609-611) sets `FAIL_TIMEOUT` on `result.timed_out` before inspecting stderr, so a timeout during solution *import*, during *test execution*, and during *teardown* all collapse into one generic message (:915-918). Distinguish them (inspect how far execution got) and give each distinct repair guidance, following the existing per-`failure_kind` pattern in `format_fixes`. Add checks.

**1e. Three cheap extraction tests.**
`test_harness.py:22-46` already covers tagged blocks, longest-block preference, prose→None, untagged blocks, JSON rejection, and unterminated fences. Add adversarial cases it misses: triple-backticks *inside a docstring*, an *indented* fence, and a `~~~`-delimited fence. If any fail, fix `extract_code_block`.

---

## Sprint 2 — Delete the Orchestrator, and tell the model the rules

**2a. Delete the Orchestrator role.** Four of five independent reviews said kill it, and it fails our own design criterion: it prevents no nameable failure while adding an LLM call, latency, RPM burn, and a serial failure hop.

Remove it properly, not just from the pipeline tuple: the mode-4 entry in `PIPELINES`, the `orchestrator` entry in `ROLE_PROVIDER` (agents_core.py:59-63), its prompt in `PROMPTS`, its handling in `run_workspace`, the mode selector option in `app.py`, and set `DEFAULT_MODE = 3`. Replace whatever escalation it was nominally doing with a **deterministic policy in Python**: on repeated failure → retry on the alternate provider → then a fresh from-scratch sample → then give up and return the artifacts with an honest UNVERIFIED verdict. No LLM call in that path.

`test_pipeline.py` has checks that reference mode 4. **Update them to assert the new honest behaviour** (e.g. that mode 4 is no longer offered, or maps to 3 with a clear message) — do not simply delete them. Report the resulting check count.

**2b. Put the environment contract in the Executor's system prompt.** The model is currently guessing our sandbox's rules, and weak models hallucinate `import pandas` constantly. State it explicitly: Python 3.9, **standard library only** (no pip, no network), no `input()` (stdin is `/dev/null`), no subprocesses, filesystem writes only inside the working directory, and a 15-second wall-clock limit. There is already a `_SANDBOX_RULES` string at harness.py:847 — reuse it as the single source of truth rather than writing a second copy that can drift. Add a check that the executor prompt contains the constraint text.

---

## Sprint 3 — Minimal synthetic eval (the actual point of this sprint)

**Why this comes before any width/best-of-N work:** we are about to spend real effort on parallel sampling with zero evidence it beats the repair loop we already have. Measure first, then decide. This eval is designed to answer that.

Build it under a new `eval/` directory.

**3a. `eval/gen_tasks.py` — deterministic synthetic task generator.**
No LLM involved. Seeded and reproducible. Each generated task emits four things: a natural-language prompt, a hidden reference implementation, a hidden strong test suite, and a difficulty tier. Do **not** put the reference implementation or the hidden tests into the prompt handed to the pipeline.

Use parameterised templates so difficulty is tunable and the task pool can scale to 200+ without hand-authoring. Three tiers, 10 tasks each for this first pass:
- **Tier 1 (easy):** single-function string/list/arithmetic transforms with parameterised constants.
- **Tier 2 (mid):** multi-branch logic with real edge cases — empty input, negative numbers, floats, ties, duplicates, boundary values. **This tier is the target band and matters most.**
- **Tier 3 (hard):** small parsing/state problems with a defined grammar and several interacting rules.

Everything stdlib-only and deterministic, to match the sandbox. Write the hidden suites yourself and make them genuinely strong — they are the ground truth the whole eval rests on.

**3b. `eval/PREDICTIONS.md` — pre-register before running anything.**
Write down expected pass rates per arm per tier *before* the first run, then commit it. This converts the eval from "here's what happened" into an actual test of the thesis, and it is the cheapest credibility upgrade available. Do not edit it after seeing results; record surprises separately.

**3c. `eval/run_eval.py` — the runner.**
Two arms for now:
- **Arm A (baseline):** single call to the Executor model, no tests, no repair. One shot.
- **Arm B (pipeline):** the current mode-3 Planner → Executor → harness repair loop.

Grade **both arms identically** against the hidden reference suite — never against the Planner's generated tests. Per task record: passed hidden suite (bool), pipeline verdict (APPROVED / REVISE / UNVERIFIED), wall-clock seconds **including any rate-limit sleep**, number of model calls, repair rounds used, and provider(s) actually used. Persist per-task JSON to `eval/results/` and make the run **resumable** — this eval is the single largest RPM consumer in the project and will get interrupted. Add a client-side token-bucket rate governor so the eval throttles itself instead of collecting 429s, and log every 429 with its `Retry-After`.

Make repeats configurable (`--repeats`, default 1). Note honestly in the report that a single repeat measures one draw of a stochastic system, not a distribution; mean ± sd needs repeats ≥ 3, so run that only if the daily budget allows.

**3d. `eval/REPORT.md` — the output.**
Report per arm **stratified by tier, never as one aggregate number**. Include:
- pass rate vs hidden suite,
- **false-APPROVED rate** — tasks the pipeline called APPROVED that fail the hidden suite. This is the important one: it directly measures combined spec-drift plus weak-generated-tests, and it is the ceiling on the entire thesis. Break it down into "spec misread the prompt" vs "tests too weak" by hand-reading the failures.
- median and p90 time-to-green, including backoff,
- calls-to-green,
- 429 counts per provider,
- predictions vs actuals from `PREDICTIONS.md`, including where we were wrong.

**Then stop and report the numbers.** Do not start width/best-of-N, holdout tests, or mutation scoring. Those three are gated on what this eval says: if the false-APPROVED rate is high, test *quality* is the bottleneck and holdout + mutation scoring come next; if Arm B plateaus well short of the hidden-suite ceiling with rounds to spare, width comes next; if Arm B already tracks the ceiling in the mid tier, neither is worth building yet.

---

## Definition of done

1. `pwd`/`ls` output pasted, confirming the right directory.
2. Sprint 0: model-slug verdict with raw status code, and the real `sandbox_layers` list from native macOS.
3. Sprints 1–2 complete, with before/after offline check counts and an explanation of any change.
4. `CPU_GRACE_SECONDS`, `_signal_name`, the SIGXCPU coercion, and `test_signal_death_is_explained` all confirmed still present.
5. `eval/` exists with generator, pre-registered predictions, resumable runner, and `REPORT.md` containing real numbers for Arms A and B, stratified by tier.
6. A short honest list of anything you changed that I did not ask for, and anything you could not get working.

Do not report a phase as complete unless the checks actually pass. If something is broken or you ran out of budget, say so plainly — an accurate partial report is worth more than a clean-sounding one.

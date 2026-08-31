# Sprint 6 — survive rate limits honestly, freeze the hidden suite, log every event

Run `pwd` first and confirm `~/Desktop/Multi agent project`. Read this whole file before
editing.

Sprint 5's three tasks are verified in. Baseline: **172 harness + 292 pipeline = 464**
passing. Report before and after, delete nothing, weaken nothing. The repository now
exists — commit `b78cff5`, 22 tracked files, `git status --short` clean except untracked
`memory.json`. So this sprint is diffable: report `git status --short` and
`git diff --stat` at the end. **Do not commit.** Git writes are denied to your sandbox and
committing is the user's step; leave the working tree dirty and describe it.

All prior ground rules stay binding: Python 3.9, stdlib only, deps stay `openai` +
`streamlit`, no pytest, keys never in a log or a prompt, `_child_env` must not regress, and
the four protected items (`CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU /
ceiling-SIGKILL → timeout coercion, `test_signal_death_is_explained`) stay untouched. The
eight already-correct mechanisms stay unimproved. `eval/prereg/` is read-only reference.

Still out of scope: D2, D6, D8, the Part C contrasts, the Part E cold grading pass,
`PREDICTIONS.md`, and B9. Do not compute any contrast.

**Subagents: yes for reading, no for writing, no for design.** Read-only fan-out is where
they earn their keep — leakage sweeps across every prompt-building site, confirming no check
was weakened, verifying the protected items survived. Two subagents must not write to files
you are editing, and no subagent decides design: it gets a compressed restatement of this
brief, and silent substitution of this brief's design is the failure mode. Say which
subagents you used and for what.

---

## Task 1 — There is no retry, no backoff, and no way to tell a 429 from a 404

**My Sprint 4 brief was wrong.** It said "retry-with-backoff on 429 stays exactly as it is."
Nothing was there. `call_role` (`agents_core.py:276`) walks providers or, in measurement
mode, walks exactly one, and any `ProviderError` ends the attempt. There is no `time.sleep`,
no backoff, no 429 handling anywhere in the module. You implemented the brief faithfully; the
brief described a mechanism that does not exist.

**Why this is now the top blocking item.** Measurement mode makes a single 429 fatal to the
task by design. The grid is 36 tasks × 3 repeats × 4 arms, and arm B makes roughly ten times
the calls of the single-shot arms, so it absorbs roughly ten times the 429s. Losses are
therefore *arm-correlated and time-of-day correlated* — the exact contamination D1 was built
to prevent, arriving through a different door. A grid that dies halfway is recoverable; a grid
that silently scores rate-limit deaths as model failures is not.

**Prerequisite: `ProviderError` carries no status.** It is a bare `Exception`
(`agents_core.py:173`) and `call_model` (`:204-223`) catches bare `Exception` and flattens it
into `"%s failed: %s"`. So 429, 404 and a `KeyError` in our own code are one indistinguishable
string today.

**What to build.**

1. Give `ProviderError` a `status` and an `exc_class`. In `call_model`, pull the status off the
   SDK exception without importing the SDK at module scope — try `status_code`, then `status`,
   then `getattr(exc, "response", None).status_code` — and fall back to `None`. Keep the
   message shape. Record `exc_class` (the exception's type name) so a bug in our own code is
   distinguishable from a provider refusal in the logs.
2. **Retry only on 429 and 5xx.** Bounded attempts, exponential backoff with a small
   deterministic jitter seeded from the run id — not `random` unseeded, because an
   irreproducible sleep schedule is an irreproducible run. **401, 403 and 404 hard-fail
   immediately with no retry.** That is what makes D0's bad-slug scenario fail in one call
   instead of burning the whole backoff budget, and it must hold in both modes.
3. **A client-side pacer, per provider.** Minimum interval between calls to the same provider,
   enforced against `time.monotonic()` before each `call_model`. Constants at module level,
   one per provider, named and commented. Do not guess at the real free-tier limits and
   present the guess as fact — pick conservative values, say in the report what you chose and
   on what basis, and log the constants into the results manifest so a run states the pacing
   it ran under. 429s are prevented here, not absorbed by retries.
4. **`infra_loss` as its own terminal outcome.** When retries are exhausted, the cell records
   a distinct outcome carrying provider, status, attempt count and elapsed time. It must never
   be graded, never set `passed: False`, and never share a representation with a model failure.
   A rate-limit death is not evidence about a model.
5. **Exclusion is at task granularity, not cell.** When any cell of a task is an `infra_loss`,
   the analysis drops **the whole task — every arm, every repeat** — and prints the count as
   its own column. Dropping only the failed cell would be differential: B absorbs more 429s, so
   cell-level exclusion silently reweights tasks toward the arms that survived. Task-level
   exclusion keeps the pairing intact and the exclusion non-differential. Print the count
   whether or not it is zero, and when it is non-zero print a plain sentence saying these tasks
   were lost to rate limits rather than to model behaviour.
6. **Resume by existing cell.** Before running a `(task, repeat, arm)` cell, if its result file
   already exists under `eval/results/<seed>/`, skip it and log the skip as an event. This is
   how a grid that died at task 20 finishes. It reuses *completed results*, never a stored
   response as a fresh draw.

**Standing prohibition, write it into a comment where someone would try it.** No caching of
provider responses keyed on `(prompt, model, params, provider)`. A disk cache like that
collapses repeated sampling: the D8 calibration sweep of 10 A′ draws × 36 tasks becomes 36
unique answers replayed ten times at zero variance, k=3 becomes k=1, and nothing warns you. If
such a cache is ever built it needs an attempt nonce in the key and an unconditional bypass in
measurement mode. Prefer D4's content-addressed *artifact* store, which gives resume without
ever replaying a sample as if freshly drawn.

**Checks to add.** A 429 retries up to the bound then records `infra_loss`, not a failure; a
404 raises on the first call with zero retries and zero sleep in both modes; the pacer delays a
second call to the same provider and does not delay a call to a different one; the backoff
schedule is reproducible from the run id; an `infra_loss` cell excludes its whole task from the
summary and increments the reported count; a resumed cell makes zero provider calls; a cell
absent from disk makes the full complement of calls; `status` and `exc_class` reach the call
record and no key does.

---

## Task 2 — Freeze the hidden suite and prove it never leaks

This is the one failure that invalidates every number simultaneously. If a hidden suite's
expected values reach any prompt, the hidden pass rate stops measuring generalization for that
task and there is no way to tell after the fact.

**Current state, verified.** `eval/gen_tasks.py` bakes expected values by executing the
reference, and `--self-check` already verifies every suite passes its own reference, fails a
stub, and — across families — that each reference satisfies only its own suite. That substance
is good and is most of D6; it is simply not recorded anywhere, so "we validated the task set"
is a claim about something somebody ran once. `run_eval.py` hashes the *visible* suite
(`_suite_hash`, `:397`) and nothing hashes the hidden one.

**What to build.**

1. **`eval/tasks.lock`** — written once by an explicit command, never silently regenerated.
   For each task: task id, family, tier, and separate sha256 digests of the prompt, the hidden
   suite and the reference. Plus the generator seed, the task count, the tier counts, and the
   `--self-check` result with the timestamp it was obtained.
2. **`--verify-lock`** — regenerate from the recorded seed and fail loudly on any digest
   mismatch, naming the task and which of the three digests moved. `run_eval.py` calls this at
   startup and refuses to run on a mismatch. This is what makes "the task set did not change
   under us" checkable rather than assumed.
3. **An automated leakage audit over a full stub sweep.** Capture every `(system, user)` pair
   that reaches `call_model` — a recording hook, no network, no keys — and assert that no
   hidden-suite content appears in any of them. Define the test so it is not trivially
   defeated: normalize whitespace, then for every hidden-suite line above a non-trivial length
   threshold, assert it appears in no prompt; **and separately assert that the baked expected
   values do not appear**, since those are the actual secret and they survive reformatting that
   would defeat line matching. Same audit for the reference source. Report the number of prompts
   swept and the number of literals checked, so a passing audit that swept nothing is visible
   as such.
4. Record each task's hidden-suite digest on the run record, so a result is attributable to the
   exact suite that graded it.

`run_eval.py:278` reads `task.tests` inside the stub. Confirm by reading that this is
stub-only and unreachable with real providers, and say so in the report rather than assuming
it — the audit in point 3 should catch it if it is not.

**Checks to add.** `--verify-lock` passes on an untouched tree; a one-character edit to a
generated suite makes it fail and names the task and the digest; the leakage audit fails when a
hidden-suite literal is deliberately injected into a prompt builder, and reports non-zero
prompt and literal counts when it passes; the recorded hidden digest matches the suite the
grader actually ran.

---

## Task 3 — Per-event log (D3) and task-order shuffle (D7)

**D3.** An append-only JSONL event log per run, alongside the existing per-run JSON. **Do not
change the shape of the existing JSON** — the UI and every stored record depend on it; the
JSONL is purely additive. Flush after every event. A run that dies mid-grid must leave a
readable log up to the last thing that happened, which is the entire point.

Event types, enumerable and closed: `call` (the `CALL_LOG` record), `step`, `candidate`,
`gate`, `verdict`, `infra_loss`, `resume_skip`. Every event carries the run id, the task id,
the arm, the repeat, and a UTC timestamp.

**Redact at the write site.** `agents_core.py:835` and `:1100` currently rely on `call_role`
having redacted upstream. The JSONL writer must call `_redact` itself on anything derived from
an exception or from provider output, because a second writer relying on a distant first
writer's hygiene is how the unredacted `ProviderError` got in last time.

**D7.** Task order is currently generation order. Shuffle it with a seeded permutation derived
from the manifest seed and record the permutation in the manifest. This is distinct from
`_arm_order`, which permutes arms within a task and is already in.

While you are there: `test_pipeline.py:1247` compares `_arm_order(...)` to itself, so it pins
determinism and cannot catch a wrong-but-stable ordering. Strengthen it to compare against a
hardcoded expected permutation for one fixed seed, and do the same for the new task-order
permutation. Strengthening a check is in scope; weakening one is not.

**Checks to add.** Every event type round-trips through the writer and parses back as one JSON
object per line; the log is complete up to an abrupt termination mid-run; no key and no raw
exception text appears in any event; the existing per-run JSON is byte-identical in shape to
before; the task-order permutation is stable for a fixed seed and differs for a different
seed; both order checks compare against hardcoded expectations rather than to themselves.

---

## Report back with

1. `pwd`, plus `git status --short` and `git diff --stat`. Do not commit.
2. Offline check counts before and after; any existing check whose text changed, and why.
3. Per task: what changed, file and line ranges.
4. The pacing constants you chose per provider, and the basis for each. Say plainly if a value
   is a conservative guess rather than a documented limit.
5. Anything in this brief you think is wrong, and what you did instead.
6. Anything you touched that this brief did not ask for.
7. A demonstration on the stub path, no keys and no network, that: a simulated 429 retries the
   bounded number of times and lands as `infra_loss` rather than a model failure; a simulated
   404 hard-fails on the first call; and one `infra_loss` cell removes its whole task from the
   summary while incrementing the reported count.
8. The leakage audit output: prompts swept, literals checked, and the result — plus the same
   output with a literal deliberately injected, showing it fails.

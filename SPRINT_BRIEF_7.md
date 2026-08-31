# Sprint 7 — finish D5, build the calibration sweep, close the filesystem jail

Run `pwd` first and confirm `~/Desktop/Multi agent project`. Read this whole file before
editing.

Sprint 6 is verified in. I re-ran both suites from a clean `git archive` of `b78cff5` and got
**172 + 292 = 464** there and **172 + 516 = 688** here, and I compared the two runs' check
*names* rather than their counts: exactly one name disappeared, `arm order is reproducible for
one task`, the one you were told to strengthen. The other 291 all survive. `harness.py` and
`test_harness.py` are byte-identical to `b78cff5`. `--verify-lock` passes at 36 tasks / 108
digests, and on a lock with one hex character altered it fails twice — the `body_sha256`
self-digest catches the hand edit and the per-task line names `aggregation-01` and which digest
moved. That is better than the brief asked for.

**688 is the baseline.** Report before and after, delete nothing, weaken nothing. All prior
ground rules stay binding: Python 3.9, stdlib only, deps stay `openai` + `streamlit`, no pytest,
keys never in a log or a prompt, `_child_env` must not regress, the four protected items stay
untouched, the eight already-correct mechanisms stay unimproved, `eval/prereg/` is read-only.
The tree is still uncommitted; leave it that way and report `git status --short` and
`git diff --stat`. **Do not commit.**

Out of scope: D2, the Part C contrasts, the Part E cold grading pass, `PREDICTIONS.md`, B9, and
D4's content-addressed store. Do not compute any contrast.

**Also deliberately deferred, so it is not lost:** the permuted-assert-order flakiness battery.
`_candidate_rank`'s use of `failed_assertion_line` depth is monotone only if the frozen suite's
asserts are order-independent, and your own comment names that battery as the check. It is real
work and it is pre-D2, but it is smaller than the three tasks below and it can follow them.

**Subagents: yes for reading, no for writing, no for design.** Same terms as before. Say which
you used and for what.

---

## Task 1 — The leakage audit never sweeps a Test Writer prompt

Your own output is the evidence:

```
prompts by role:  executor=324, planner=36
```

360 prompts, and **not one of them is a `test_writer` prompt.** The Test Writer is a
prompt-building site that receives task material, it routes to Gemini, and `audit_leakage.py`
has no flag that reaches it. So D5's clean result currently covers two of the three roles that
build prompts.

The gap is specifically the regeneration branch. `_resolve_tests` regenerates a suite via the
Test Writer when the plan's TESTS block fails `audit_tests` — the path Sprint 5 built the
vacuity stub to exercise. That branch calls the Test Writer with material derived from the
plan, and the audit has never seen the prompt it builds.

**What to build.** Extend the audit so the swept set includes Test Writer prompts, and add a
pass that drives the vacuity path — reuse Sprint 5's vacuity stub or flag rather than inventing
a second one. The reported counts must break out `test_writer` as its own role, so the same
"a pass that swept nothing is visible as such" property holds per role rather than only in
total. Keep the exit convention as it is: 0 clean, 1 leaked, 2 could not run.

While you are in there: `--inject` and `--shipped-stub` are both far slower than the clean
sweep — slow enough that I could not reproduce either inside a 150-second ceiling, so those two
legs are the one part of Sprint 6 I am taking on your report rather than on my own run. An
audit that only appends a literal to a prompt should cost about what the clean sweep costs.
Find out where the time goes and say so. If injection changes the sweep's control flow — more
repair rounds, a different number of calls — then the negative control is being demonstrated on
a different code path than the clean run, which weakens it.

**Checks to add.** The swept set contains at least one `test_writer` prompt and the per-role
breakdown reports it; a vacuity-driven pass reaches the regeneration branch and its prompts are
in the swept set; a literal injected into the Test Writer's prompt builder is caught and exits
1; a role whose swept count is zero makes the audit refuse with exit 2 rather than pass.

---

## Task 2 — The calibration sweep and the go/no-go gate (D8)

This is the last piece of machinery between the code and the grid, and it is the one that can
stop the grid from being run at all. The pre-registration's gate is a **task-set** property:
difficulty is a continuous covariate measured from a pre-run sweep of **10 A′ samples per
task**, and if too few tasks land in the interesting band the task set goes back for
regeneration *before* k=1.

The gate as registered says "fewer than 12 of 30." The code says 36 tasks / 18 families, tier
counts {1:10, 2:16, 3:10}. **Implement the gate as a threshold read from one named constant,
with the 36-task value `>= 15` as its default, and print the threshold alongside the verdict.**
Do not hard-code 12, and do not hard-code 30. The restatement from 30 to 36 is a registration
decision the user still owes and it must not be buried in an `if`; putting it in one named
constant that the manifest records is what makes the decision visible when it is made.

**What to build.**

1. A calibration mode — its own entry point or an explicit flag on `run_eval.py`, your call —
   that runs **A′ only, 10 draws per task, Groq only, zero Gemini beyond the one shared Planner
   call per task that A′ already depends on**. Report the Gemini and Groq call counts it will
   make *before* it makes them, so the user can see the cost against a free tier before
   spending it.
2. Per task, record all 10 draws and the calibration rate `d_t`. Store to its own directory,
   not into `eval/results/seed-N/`, because this is not grid data and must never be pooled with
   it. Reuse the existing resume-by-existing-cell machinery so a sweep that dies partway
   finishes.
3. The gate verdict: count tasks with `d_t` in the open interval (0.1, 0.9), compare against the
   threshold constant, and print GO or NO-GO with the count, the threshold, and the per-tier
   breakdown. On NO-GO print the plain sentence that the task set is at floor or ceiling and
   must be regenerated or re-tiered before k=1 — not a warning buried in a table.
4. Record in the calibration manifest: the threshold used, the task count, `tasks.lock`'s
   `body_sha256`, the pacing block, and the model IDs actually used. A calibration measured
   against a different task set than the grid runs is worthless, and the lock digest is what
   makes that checkable.
5. Verify the whole thing offline on the stub and report the numbers. A stub whose draws are
   deterministic will produce `d_t` of exactly 0 or 1 for every task and therefore a NO-GO —
   **that is the correct output for that input, not a bug.** Say so explicitly in the report,
   and use a sampling stub to demonstrate the GO path.

**Do not compute any contrast, and do not touch `PREDICTIONS.md`.** Calibration produces `d_t`
and the gate verdict. Nothing else.

**Checks to add.** Ten draws per task, all recorded; the sweep makes zero Gemini calls beyond
the shared Planner call per task; calibration output lands outside `eval/results/seed-N/`; the
band is the open interval, so exactly 0.1 and exactly 0.9 are excluded; the threshold comes
from the named constant and appears in the printed verdict and the manifest; a set with too few
in-band tasks prints NO-GO and the interpretation sentence; the manifest records the lock digest
and refuses to run against a lock that does not verify.

---

## Task 3 — Finish the filesystem jail before ~1,700 candidates run on the user's Mac

`_guard_open` wraps `builtins.open` (`harness.py:474`) and compares `os.path.realpath` against
`_WORKDIR_REAL`, which catches both spellings of an escape. That is the right pattern. But it
is only `open`. I grepped: nothing wraps `os.remove`, `os.unlink`, `os.rename`, `os.rmdir`,
`os.replace`, `shutil.rmtree`, `shutil.move` or `os.makedirs`. Reading is not the risk;
deletion is.

The only thing currently denying a destructive absolute-path call is the `sandbox-exec`
profile's `(deny file-write*)` — the layer that reports `os-level:unavailable` inside a nested
sandbox. So on a machine where that layer is unavailable, `shutil.rmtree("/Users/<user>/Desktop")`
from generated code runs. Env-scrubbing and `HOME=workdir` stop `expanduser("~")`; they do not
stop a hardcoded absolute path, and `_block_processes` stops shell `rm` but not native Python.

The grid executes roughly 1,700 LLM-written candidates. One hallucinated cleanup line is
enough, and it is irreversible.

**What to build.** Extend the existing guard pattern to the destructive filesystem entry points
above, in the child runner, using the same `realpath`-against-`_WORKDIR_REAL` comparison and the
same failure representation `_guard_open` already uses — a denied path must surface as
`FAIL_PATH`, which is already labelled "our bug, not the Executor's," so check that the label
still reads correctly for a deliberate escape attempt and change the wording if it does not.

Three limits on this task. Do not touch the eight protected mechanisms; this is additive.
**Do not weaken `_guard_open`** while generalising it — if you factor out a shared helper, the
`open` behaviour must be identical afterwards and its existing checks must pass unchanged. And
keep the honesty layer honest: whatever `_memory_layer()` does for memory, the path dimension
should do for paths — report what actually held, in-process guard versus OS layer, rather than
implying the in-process guard is a security boundary. It is accident containment. Part G of the
pre-registration says so and that framing does not change here.

**Checks to add.** Each newly guarded call is denied for an absolute path outside the workdir
and permitted inside it; a symlink inside the workdir pointing outside is denied (that is what
the `realpath` comparison is for); a denied call reports `FAIL_PATH` with a message that reads
correctly; every existing `_guard_open` check passes unchanged; and the reported sandbox
dimensions still distinguish the in-process guard from the OS layer.

---

## Report back with

1. `pwd`, `git status --short`, `git diff --stat`. Do not commit.
2. Offline check counts before and after, against **688**; any existing check whose text or
   conditions changed, and why. Compare check *names* between runs, not just counts — that is
   how the one changed text was found last sprint.
3. Per task: what changed, file and line ranges.
4. Where the `--inject` and `--shipped-stub` time goes, and whether injection changes the
   sweep's control flow.
5. The calibration mode's projected call counts per provider, the stub-verified gate output for
   both a deterministic stub (expect NO-GO, correctly) and a sampling stub (expect GO), and the
   threshold constant's name and default.
6. Anything in this brief you think is wrong, and what you did instead. My Sprint 6 brief
   claimed there was no retry, no backoff and no pacing anywhere; you were right that
   `eval/run_eval.py` already had `RateGovernor`, the 429 retry loop and status recovery via
   `exc.__context__`. I had grepped one file and generalised. If a premise here looks similarly
   overstated, check it and say so — that correction was worth more than the code.
7. Anything you touched that this brief did not ask for.

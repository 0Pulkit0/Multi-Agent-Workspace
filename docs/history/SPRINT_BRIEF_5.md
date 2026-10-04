# Sprint 5 — make the shared gate actually shared, and put the repo under version control

Run `pwd` first and confirm `~/Desktop/Multi agent project`. Read this whole file before
editing.

Sprint 4's three tasks are verified in and both suites are green at **172 harness + 238
pipeline = 410**. That is the baseline; report before and after, delete nothing, weaken
nothing. All the Sprint 4 ground rules stay binding: Python 3.9, stdlib only, deps stay
`openai` + `streamlit`, no pytest, keys never in a log or a prompt, `_child_env` must not
regress, and the four protected items (`CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU /
ceiling-SIGKILL → timeout coercion, `test_signal_death_is_explained`) stay untouched.

Both pre-registration documents are now in the tree at `eval/prereg/`. They are read-only
reference — do not edit them. Still out of scope: D3, D5, D6, D7, D8, the Part C contrasts,
the Part E cold grading pass, and `PREDICTIONS.md`.

**Subagents: yes for reading, no for writing, no for design.** Parallel read-only work is
what they are good at — audits across many files, key-leakage sweeps, confirming no check was
weakened, verifying the protected items survived. Use them freely for that; last sprint's
read-only audit is what found the unredacted `ProviderError`. Two limits. No subagent writes
to files in this repo while you are editing them — Tasks 2 and 3 both touch
`test_pipeline.py` and two writers collide on every edit. And no subagent decides design: a
subagent gets a compressed restatement of this brief, and this brief's premise is that its
design must not be silently substituted, so that is where drift enters and the hardest place
to catch it. Say in the report which subagents you used and for what.

---

## Task 1 — There is no git repository

`git log` in this directory returns `fatal: not a git repository`. D2 registers an analysis
script hash and a frozen manifest, and the amendment's step 3 says to timestamp and commit
the registration before any run. None of that is achievable right now, and nothing about the
pre-registration is provable as predating the data — which is the single property a
pre-registration exists to have.

**Build it, carefully.**

1. `git init`. Leave `git config` alone — no user/email/editor changes.
2. Write `.gitignore` first, before staging anything: `venv/`, `__pycache__/`, `*.pyc`,
   `.DS_Store`, `*.swp`, `.claude/settings.local.json`, `runs/`, `eval/results/`.
   - `venv/` is 309 MB. `runs/` is 106 run logs carrying captured child stdout — synthetic
     and credential-free today, but it is output, not protocol, and committing it pollutes
     the thing being frozen. Same for `eval/results/`.
   - Data gets committed deliberately and separately once it exists. The point of this
     commit is that the *protocol* provably predates the *data*.
3. Stage **explicitly by path**. Do not run `git add .` or `git add -A`. Confirm with
   `git status --short` that nothing under `venv/`, `runs/`, or `eval/results/` is staged
   before committing.
4. One commit. **Do not create a branch, do not push, do not add a remote.** There is no
   remote and adding one is not your call.
5. Before committing, grep the staged set for anything key-shaped (`AIza`, `gsk_`, `sk-`,
   `Bearer `) and report what you find. There is no `.env` in this project — keys are read
   at runtime — but confirm rather than assume.
6. Report the commit hash and the file count.

Two stale vim swap files exist (`.agents_core.py.swp`, `.test_harness.py.swp`). They are
gitignored by the pattern above. Do not delete them; they are the user's, not yours.

---

## Task 2 — `tests=` injection, so A′@3 and B gate on the same suite

**The defect.** `make_plan` in `eval/run_eval.py` resolves the visible suite once and
`_gate` uses `plan["tests"]`. But `run_arm_b` passes only `plan["plan"]`, so
`run_workspace` re-runs `_resolve_tests` over the same plan text. In the ordinary case the
audit is deterministic and both arms get the identical suite. When the plan's TESTS block
**fails the vacuity audit**, `_resolve_tests` regenerates via the Test Writer — a second,
independent generation — and arm B then gates on a suite A′@3 never saw. Same plan, different
gate, on exactly the tasks where the suite was weakest.

Two consequences beyond the broken selector match. The regeneration happens **twice** per
such task, once in `make_plan` and once inside `run_workspace`; `test_writer` routes to
Gemini, which is the scarce currency, so the duplicate lands where it hurts most. And this
path is currently **unexercised** — I checked all 64 stored stub records and `tests_status` is
`generated` on every one, meaning the stub's `_thin_suite` passes `audit_tests` and the
regeneration branch has never run. Your four-arm sweep proves nothing about it.

**What to build.**

Add a `tests=` parameter to `run_workspace`, distinct from `user_tests`. It carries an
already-resolved suite — source, status, trusted flag, audit summary — and when supplied,
`_resolve_tests` is skipped entirely and those fields are set on the `RunResult` verbatim.

- **Do not reuse `user_tests` for this.** `user_tests` sets `tests_status = TESTS_USER` and
  bypasses the audit. That would silently corrupt the suite-validity-rate metric for every
  B run in the grid, and suite validity is the direct quality measure of the weakest model's
  most important output.
- Precedence: `user_tests` wins over `tests=` wins over resolving from the plan. User tests
  always win; that rule does not bend for the eval.
- Carry the **original** status through (`generated`, or whatever regeneration produced in
  `make_plan`). Do not invent a new status value.
- Still emit the `tests` memory entry so the UI feed and the run JSON are unchanged in shape.
- Supplying `tests=` without `plan=` is a programming error — raise. Injecting a suite that
  was resolved from a *different* plan is the exact divergence this task exists to close.
- `run_arm_b` passes both: `plan["plan"]` and the resolved suite from the same `plan` dict.

**Checks to add.** With `tests=` supplied, `run_workspace` makes zero `test_writer` calls even
when the plan's TESTS block is vacuous; `tests_status` is carried rather than overwritten and
is never `TESTS_USER`; `user_tests` still beats `tests=`; `tests=` without `plan=` raises;
and — the one that would have caught this — **a stub whose plan TESTS block fails
`audit_tests`** produces a byte-identical gate suite across `a_prime`, `a_prime3` and `b`, with
exactly one Test Writer call for the whole task rather than two.

That vacuity stub is the deliverable here as much as the fix is. Build it as a new stub
quality (or an explicit flag) that emits a TESTS block the audit rejects, so the regeneration
branch runs offline with no keys and no network.

---

## Task 3 — Register what happens when there is no trustworthy suite

`_gate` returns `(UNVERIFIED, False)` for every draw when `plan["tests_trusted"]` is false, so
`chosen = draws[0]` and **A′@3 collapses to A′** on those tasks. That is defensible behaviour —
any other choice among ungated failures would be a ranking built out of gate signal — but it
is currently invisible, and it silently attenuates a registered quantity: the gate-loss term
`A′@3_gate − A′@3_oracle` is pulled toward zero by every such task. The pre-registration reads a
near-zero blind gate loss as *"surprisingly good selector, and would undercut the
false-APPROVED thesis."* Untrusted suites would manufacture that reading.

**What to build.**

1. On the A′@3 record, when the gate is unavailable, record the selection explicitly — a
   field saying the selector was unavailable and draw 1 was returned by fallback, distinct
   from a field saying draw 1 won the gate. Those are different events and must not share a
   representation.
2. Count untrusted-suite tasks per arm and surface the count as its own column in the
   per-arm summary table, not a footnote.
3. When that count is non-zero, print the interpretation as a plain sentence: on these tasks
   the A′@3 gate is unavailable and the arm degenerates to A′, so the blind gate-loss term is
   attenuated toward zero and a near-zero value must not be read as a good selector without
   excluding them.
4. **Do not drop these tasks.** Report them. Dropping them is post-treatment selection on a
   quantity caused by the Planner's output quality.

**Checks to add.** An untrusted suite yields the fallback marker and not a gate-win marker;
the untrusted count appears in the summary; a trusted suite sets the gate-win marker when a
draw passes; the interpretation line prints only when the count is non-zero.

---

## Report back with

1. `pwd`, and the commit hash plus staged file count.
2. Offline check counts before and after; any existing check whose text changed, and why.
3. Per task: what changed, file and line ranges.
4. Anything in this brief you think is wrong, and what you did instead.
5. Anything you touched that this brief did not ask for.
6. Proof that the vacuity path now executes offline: the Test Writer call count for a
   vacuity-failing task, and the gate suite hash for `a_prime`, `a_prime3` and `b` on that
   task, shown to be equal.
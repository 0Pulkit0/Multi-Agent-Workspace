# Pre-registration addendum A to Revision 3

**Written 2026-08-31, while the pin probe was still running — 28 of 36 plans, 57 of 108 draws
complete, and before the Sprint 10 fix landed.** The pin standings at time of writing were
`openai/gpt-oss-120b` 16/19, `openai/gpt-oss-20b` 16/19, `qwen/qwen3.8-27b` 14/19. D-8 exists
because the top two were **tied**, and a tiebreak invented after the final count is not a
tiebreak.

Scope: adds D-7 and D-8 to `DECISIONS_R3.md`. Everything else in Revision 3 stands unchanged.

---

## D-7. The Python 3.9 / PEP 604 artifact, and the fix

**Registered: the artifact is real, it is a validity threat to the primary endpoint, and it is
fixed by stating the runtime in the prompts AND neutralising annotations in the harness.**

Observed in the pin ledger before any calibration data existed. The Planner writes PEP 604 union
annotations (`int | float | None`) into the SPEC; the runtime is CPython 3.9.6; PEP 604 needs
`type.__or__`, added in 3.10; annotations evaluate at `def` time, so the `def` raises `TypeError`
at import for every Executor that copies the signature faithfully. Measured incidence: 2 of 28
specs (~7%), and 5 of 6 import failures fell on those two tasks while 26 non-union specs
produced none.

Why it had to be fixed before calibration rather than noted as a limitation:

1. It manufactures `d_t = 0` floor tasks that are plumbing, not difficulty, eating D-1 gate
   margin and misrouting D-3 into its floor-heavy branch.
2. It biases the primary endpoint **upward**. Arm B has repair rounds and sees the import
   `TypeError`; A′ and A′@3 do not. `B − A′@3` would gain roughly 8 points from a language
   version mismatch, on an endpoint whose expected effect is 10–20 points.

**Registered fix, both parts:**

- **A.** The runtime level is stated in the Planner, Executor and Test Writer prompts: CPython
  3.9, no PEP 604 unions in annotations or at runtime, no `match` statements, no 3.10+ stdlib.
- **B.** `from __future__ import annotations` is written ahead of `solution.py` in the harness, so
  PEP 563 turns every annotation into a string and the class of failure cannot recur even when a
  model ignores A. `ExecResult.source` continues to record what the model wrote, not what ran.

**Registered as a change to the measured environment.** Part B changes the interpreter semantics
under which every arm executes, equally for all arms. Part A changes the Planner's output
distribution. Both are declared here rather than discovered in a diff. Accepted side effect:
`solution.py` line numbers in raw stderr shift by one, which reaches the Executor as repair
context; `failed_assertion_line` is unaffected because it is only set for frames in
`test_solution.py`.

**Compliance is measured, not assumed.** A report-only scanner flags 3.10-only syntax in specs
and records the flag. It never rejects a spec and never triggers regeneration — rejection was
considered as an alternative fix and deliberately not adopted. Calibration output therefore
carries the Planner's compliance rate as a number.

## D-8. Executor pin tiebreak

**Registered: pass count over the 36 tasks decides the pin only when the margin is decisive.
Otherwise the ladder below decides it, and in one specific case the probe is re-run instead.**

Let `U` = the number of union-bearing specs in the probe run (2 at time of writing, projected 3
over 36 tasks).

1. **Decisive margin.** If the leader's hidden-suite pass count exceeds the runner-up's by more
   than `U`, pin the leader. No further tests.
2. **Non-decisive margin, uniformly poisoned.** If the margin is `U` or less *and* every
   union-bearing task failed for **both** of the top two candidates, then those tasks cannot have
   changed the ordering, the remaining comparison is genuine, and the tiebreak ladder applies in
   this order, each step measured from the existing ledger:
   1. fewer completion tokens per approved task, including `reasoning_tokens`, because the
      binding constraint is a rate-limited free tier and cost per success is the real currency;
   2. fewer non-`ok` units, that is fewer infrastructure losses;
   3. lower median wall-clock latency per call;
   4. the smaller model, as a deterministic final tiebreak.
3. **Non-decisive margin, asymmetrically poisoned.** If any union-bearing task failed for one of
   the top two and not the other, the comparison is confounded by the artifact. Do **not** apply
   the ladder. Re-run the probe after the D-7 fix on a fresh `--out`, and pin from that run.

Rationale for step 2's ordering: on a free tier, two models that solve the same set of tasks are
not equally useful, and the one that solves them for fewer tokens raises the number of draws the
grid can afford. Parameter count is last precisely because it is the least informative about this
project's task distribution, which is the whole reason D-6 refused to pin from leaderboards.

**Unchanged from D-6:** the pin is committed before the calibration sweep runs, and `d_t`
measured on one Executor does not describe a grid run on another.

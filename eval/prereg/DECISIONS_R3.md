# Pre-registration decisions, Revision 3

**Written 2026-08-31, against commit `71fc347` on `main`, before any `d_t` was observed.**
That ordering is the point of this file. Nothing here was chosen with knowledge of a
calibration result, because no calibration run exists yet.

Scope: this file *resolves* the open decisions left by
`EVAL_PREREGISTRATION_REVISION_2.md` and `EVAL_PREREGISTRATION_AMENDMENT.md`. Where it
conflicts with Revision 2 on a point listed below, **this file governs**. Where a design
element is merely restated here for self-containment, it is marked *(restated)* and
Revision 2 governs.

State at time of writing, verified rather than assumed:

- `eval/tasks.lock` `body_sha256` = `80319b7189dcd7def55a704781f46b143c90d637121462541820f46094d1003c`
- 36 tasks, 18 families, tiers `{1: 10, 2: 16, 3: 10}`, 108 digests
- lock `self_check` passed 2026-08-30T19:10:13Z over the battery
  `vacuous_stub, raise_to_pass, ge_to_gt, le_to_lt, or_to_and, sorted_to_list`,
  `runaway_budget` 250000, one honestly recorded equivalent mutant `tiered_pricing:le_to_lt`
- `gen_tasks.py --verify-lock` passes under both 3.9.6 and 3.10.12
- offline checks green on both interpreters: `test_harness.py` 242/0, `test_pipeline.py` 746/0
- shipped candidate ranking key is `checks` (count of checks passed), with the pre-Sprint-9
  depth key frozen in `eval/rank_battery.py::_depth_rank` for comparison;
  `eval/ranking_battery.json` `stamp.status` = `current` against the lock above

---

## D-1. Task count and the calibration gate

**Registered: 36 tasks. Gate passes when at least 15 of 36 tasks have `d_t` strictly inside
the open interval (0.1, 0.9).**

That is 41.7%. It matches the code as shipped: `CALIBRATION_DRAWS = 10`, `BAND_LOW = 0.1`,
`BAND_HIGH = 0.9`, `GATE_MIN_IN_BAND = 15` (`eval/calibrate.py:51-69`). The interval is open
on both ends deliberately: with ten draws, a task at `d_t = 0.1` or `0.9` is one draw from
floor or ceiling, and admitting those would count near-deterministic tasks as
discriminating ones.

**Revision 2's "30 tasks" and "12 of 30" are superseded**, in all six places it says 30.
Reason: 30 tasks cannot preserve the generator's 18 families × 2 variants balance, and
cutting six tasks would require a new lock for no measurement benefit.

Calibration cost at this setting: **36 Gemini calls + 360 Groq calls.** One Planner spec per
task, persisted, reused by all ten draws — so `d_t` is Executor variance with the spec held
constant. `calibrate.py` records `threshold_is_default`, so any later override of the
threshold is visible in the output rather than silent.

## D-2. P4 is withdrawn

**Registered: P4 (novel-family effect exceeds classic-family effect) is formally withdrawn.
The novel/classic assignment is struck from the D2 hash.**

All 18 families are classic algorithm problems, so there is no novel arm of the contrast and
P4 is unscoreable as written. Building a novel-compositional family (B9) would cost roughly
five hours of development, a new lock, and a re-run calibration sweep, and it is not on the
critical path for the mechanism claim.

**Accepted caveat, to be reported rather than buried:** because the families are classic,
training-data contamination places a floor on arm A's success rate and therefore a ceiling on
any effect measured against it. The pilot cannot separate "the pipeline helped" from "arm A
was already familiar with this problem" for these families.

## D-3. The response to NO-GO, pre-committed

**Registered: the remedy is conditional on the direction of failure, decided by counts that
`calibrate.py` already reports.**

Let `F` = number of tasks with `d_t <= 0.1` (floor) and `C` = number with `d_t >= 0.9`
(ceiling).

- **Ceiling-heavy (`C > F`):** the task set is too easy. Regenerate with harder parameters,
  keeping the same 18 families and the same tier structure, write a new lock, and re-run the
  sweep. No change to the endpoint or the gate.
- **Floor-heavy (`F >= C`):** do **not** regenerate first. Read the Planner spec for every
  task with `d_t <= 0.1` and classify each as *spec failure* or *genuine difficulty*, then
  report that count. A floor task is indistinguishable from a Planner failure by the numbers
  alone, because every arm in a cell inherits the one spec — so regenerating tasks in
  response to what is actually spec drift would be treating the wrong cause. Only after that
  count exists is the choice between regenerate and re-scope made, and the count is reported
  either way.
- Ties count as floor-heavy, because that is the branch where the diagnosis is unknown.

The floor-task spec read is **not** to be automated by grading the reference against the
spec: the reference passes the hidden suite by construction, so that check is circular. It is
a manual read.

## D-4. SELECTION_NO_GATE handling

**Registered: always report the rate; report the mechanism endpoint on both the full set and
the gated subset; if the rate exceeds 25%, the gated-subset estimate is primary and the
full-set estimate is secondary.**

A `SELECTION_NO_GATE` task is one where no trusted acceptance suite survived (`tests_status`
of `unusable` or `vacuous`, after the one permitted regeneration). With no gate, arm A′@3 has
no selector, returns draw 1 by position, and **stops being best-of-3 at all** — it is A′. Each
such task therefore pulls `B − A′@3` toward zero, and a near-zero blind gate-loss term
`A'@3_gate - A'@3_oracle` would read as a surprisingly good selector when it is in fact an
absent one.

Neither estimate is discarded under any outcome. The threshold governs which one is labelled
primary, not which one is published.

## D-5. The frozen seed

**Registered: seed 0**, for both the run id and the task-order permutation. Chosen and
recorded here rather than inherited from a default, so that "the seed was 0" is a decision in
the audit trail and not an artifact of an argument nobody passed.

## D-6. Executor pin — open, and blocking

The Groq Executor is **not yet pinned**. `llama-3.3-70b-versatile` 404s for this account, and
the three viable candidates are `openai/gpt-oss-120b`, `openai/gpt-oss-20b` and
`qwen/qwen3.8-27b`.

**Registered method: pin empirically, not from leaderboards.** `eval/pin_executor.py` runs one
A′ draw per task across all three candidates on a shared Planner spec — 36 Gemini + 108 Groq
calls — and the winner on this project's own task distribution is pinned in
`PROVIDERS["groq"]["model"]` and committed **before** the calibration sweep runs. `d_t`
measured on one Executor does not describe a grid run on another.

`reasoning_format="hidden"` is set explicitly on every Groq call and registered through the
role's `params` so it is logged beside temperature and top_p. Measured 2026-08-30:
`gpt-oss-20b` with it unset returned zero fenced code blocks while 568 characters went to a
separate `reasoning` field. Unset is a broken default, not a neutral one. The full `usage`
block including `reasoning_tokens` is captured per call, because `hidden` suppresses
reporting and not computation.

---

## Restated for self-containment — Revision 2 governs

*(restated)* Arms: `a` single-shot raw prompt with zero retries; `a_prime` Planner spec only,
one Executor call, no repair, never sees the visible suite; `a_prime3` three independent draws
under the same gate, best-of-3; `b` the full pipeline to a maximum of three rounds.

*(restated)* Mechanism endpoint `B_oracle − A′@3_oracle`, tested with a paired task-cluster
bootstrap and an exact sign-flip permutation test, with rank regression on continuous `d_t`.
The mid-band `[0.25, 0.6]` construct is deleted.

*(restated)* Grader independence is **partial**: it holds of the Executor and fails of the
spec, since the planner and the test writer are one model working from one spec lineage. That
is the spec-drift channel and the ceiling on the thesis, and it is to be stated in those terms
rather than as "independent verification".

*(restated)* Run order after this file: pin the Executor, run the calibration sweep, evaluate
the D-1 gate, apply D-3 if it fails, read the floor-task specs, then freeze D2, then a k=1
smoke run that is discarded, then k=3.

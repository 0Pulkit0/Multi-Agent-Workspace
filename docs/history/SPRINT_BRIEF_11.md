# Sprint 11 — the prologue's own import death, and getting `d_t` for zero Gemini

The pin replay succeeded and I have read `eval/results/pin-replay-1/ledger.jsonl` directly rather than
the printout. Two things in it drive this sprint: the Sprint 10 prologue **introduced** an import
failure of its own, and the 19 replayed tasks say the D-1 calibration gate is at serious risk. There is
a way to settle the second for **zero Gemini requests**, and this sprint builds it.

Read the whole brief before starting. Run `pwd` first and confirm you are in
`~/Desktop/Multi agent project`.

## What the replay measured, so you are not re-deriving it

The two ledgers cover the same 57 `(task, candidate)` pairs, so they are exactly paired.
`grade_failure` went `{import: 6, assertion: 5}` → `{import: 1, assertion: 4}`; pass 46/57 → 50/57.

Only **2 of 19 specs** carry `spec_runtime_syntax: pep604-union` — `aggregation-01` and `grouping-01` —
and those two produced **5 of the 6** old import deaths. Both are now 3/3 with zero import deaths.
Part B did the job it was written for: spec-originated PEP 604 import deaths **5 → 0**. The 6th old
death was `interval-logic-01`/gpt-oss-120b on a spec with no flagged syntax, i.e. model-originated.

`eval/results/` stays read-only throughout this sprint. Do not edit anything in `eval/prereg/`. Do not
regenerate `eval/tasks.lock`.

## Task 1 — the prologue turns a legal `from __future__` into a SyntaxError

The one remaining import death is `interval-logic-01` / gpt-oss-20b:

    File "solution.py", line 7
        from __future__ import annotations
    SyntaxError: from __future__ imports must occur at the beginning of the file

`harness.py:1244` writes `_SOURCE_PROLOGUE` (`:102`, `"from __future__ import annotations\n"`) ahead of
the model's bytes. The model had written a shebang, a module docstring, and then its own
`from __future__ import annotations` — legal at line 6 of its own file, illegal at line 7 of ours.

The comment at the write site claims the prologue "stays legal when the model opens with a docstring
**or** wrote the same import itself." Each case alone is legal. **The conjunction is not.** Confirmed by
running each:

| written file | result |
|---|---|
| prologue + `# comment` + own future | OK |
| prologue + own future as its first statement | OK |
| prologue + **docstring** + own future | **SyntaxError** |
| prologue + docstring + `from __future__ import division` | **SyntaxError** |

Note the last row: **any** future statement triggers it, not only `annotations`. So a fix that only
looks for the word `annotations` is incomplete.

Fix it so the prologue is never the thing that makes a file illegal. If the model's source already
contains a top-level future statement, either skip the prologue or insert `annotations` after the last
existing future statement — your call, but say which and why in the comment, and **correct the comment
at the write site**, which is currently wrong in a way that reads as verified.

Two constraints on how. A line scan is sufficient and is the safer choice; if you reach for `ast.parse`
it **must** fall back to today's plain prepend when parsing raises, because `match` statements do not
parse on 3.9 and a model that writes one would otherwise lose the prologue silently. And do not weaken
the existing property that `ExecResult.source` keeps the model's original bytes — if the prologue's
position is now variable, the "executed text is recoverable as `_SOURCE_PROLOGUE + source`" claim in the
`:100` comment stops being true, so record what is needed to reconstruct it, or restate the claim
honestly.

**Second defect, same site, no test covers it.** Prepending makes the model's docstring no longer the
first statement, so `solution.__doc__` becomes `None` for every solution that opens with a docstring —
which is most of them. Verify this yourself in one line before deciding whether your fix resolves it.
Nothing in the current suites reads `__doc__`, so this is not a live failure; it is an undocumented
semantic change to every executed solution, and it should either be fixed by the same change or named in
the comment. Do not add a task-side workaround.

Add checks for all four rows of the table above plus the `__doc__` case. Report before/after counts by
**name**, as usual — no check silently deleted or weakened.

## Task 2 — import the 19 paid-for specs into the calibration spec store

`calibrate.py` already resumes on `os.path.exists(plan_path(root, seed, task_id))` (`:299`, `:710`), so
19 spec files written to those paths make it skip 19 Planner calls. **This is a converter, not a feature**
— put it in a new file, do not add a flag to `calibrate.py`, and change nothing that
`eval/prereg/` describes.

`calibration_plan` (`:158`) returns `{task_id, spec, planner_provider, steps, plan_sha256, spec_sha256}`.
The pin ledger's plan records carry `task_id`, `spec`, `planner_provider`, `steps`. `spec_sha256` is
recomputable as `agents_core.sha256_of(spec)`. **`plan_sha256` is not reconstructable** — the ledger kept
`tests_sha256`, never the raw Planner reply text. Do not fabricate it. Write it empty with an explicit
marker plus `imported_from` and the source ledger's own digest, so a reader can tell an imported spec
from a drawn one at a glance.

Before you write a single file, settle one thing and report it: **is `run_eval.make_plan`'s Planner call
the same call `calibration_plan` makes?** `calibration_plan`'s docstring claims byte-for-byte parity with
the grid and deliberately skips visible-suite resolution to hold "one Gemini call per task"; `make_plan`
resolves the suite as well. If the *spec* comes from the same role, prompt, provider and sampling
parameters in both, the specs are exchangeable and the import is sound. **If they differ in any of those
four, stop and report it instead of importing** — a `d_t` measured against specs from a different call is
not the `d_t` D-1 registers, and that is not a thing to discover after 190 draws.

Copy `eval/results/pin-executor/ledger.jsonl` and `eval/results/pin-replay-1/ledger.jsonl` outside the
gitignored path before anything writes under `eval/results/`. Between them they are 19 Gemini requests —
a full day's flash quota — in untracked single copies.

## Task 3 — report the projection, run nothing

With Tasks 1 and 2 landed, print `calibrate.py`'s own cost projection for the 19 imported tasks at
`--draws 10` and paste it. It should read **0 planner calls, 190 executor calls**. Do not run the sweep;
the Executor pin is a registration decision and it is mine, not yours.

## Why this matters — do not optimise it away

D-1 (`eval/prereg/DECISIONS_R3.md:28`) passes when ≥15 of 36 tasks have `d_t` strictly inside (0.1, 0.9)
at 10 draws. On the 19 replayed tasks, **15 are 3/3** — three architecturally different Executors all
passed first attempt with no repair. At most 3 of 19 could be in-band, which projects to ~5.7 of 36
against a threshold of 15. Three draws from three different models is **not** the D-1 estimator, so that
is a forecast and not a measurement — which is exactly why Task 2 exists: 190 Groq calls on specs already
paid for produce the registered estimator, at the registered draw count, for 19 of 36 tasks, for zero
Gemini.

The tier table is the reason it is not yet fatal, and the reason nothing here should be rushed:

| | tier 1 | tier 2 | tier 3 |
|---|---|---|---|
| all 36 | 10 | 16 | 10 |
| replayed 19 | 10 | 9 | **0** |
| pass rate | 29/30 | 21/27 | unmeasured |

All ten tier-3 tasks are among the 17 the quota killed. Task 1 goes first because a harness bug that
kills ~2% of draws biases `d_t` downward, and `d_t` is the number the whole gate rests on.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest. Keys via
`getpass`/`input()` only, never on a command line. Do not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the
SIGXCPU/ceiling-SIGKILL→timeout coercion, or `test_signal_death_is_explained`. Do not "fix" the eight
already-correct mechanisms. Stage by explicit path, no `git add .`, no branch, no push, leave `git config`
alone; grep the staged set for key-shaped strings and **read the hits** — the three fixtures at
`test_pipeline.py:3794`, `:3897`, `:3936` are legitimate. Leave the two vim swap files alone.

Run both suites on 3.9.6 and report counts by name before/after.

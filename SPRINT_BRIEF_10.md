# Sprint 10 brief — close the Python 3.9 / PEP 604 spec artifact

**Written 2026-08-31, against `b5ab08d` on `main`. Blocking: `eval/calibrate.py` must not run
until this lands.**

Work only in `~/Desktop/Multi agent project/`. Run `pwd` first and paste it — this has been
done in the wrong directory twice.

---

## 0. The finding, so you can check it rather than take it on faith

Read from the live `eval/results/pin-executor/ledger.jsonl` during the pin probe, first 12
planned tasks:

| task | spec return annotation | draws | passed | import failures |
|---|---|---|---|---|
| `aggregation-01` | `` `-> dict[str, int \| float \| None]` `` | 3 | **0** | **3** |
| `aggregation-02` | `` `-> dict` `` | 3 | 3 | 0 |
| other 10 tasks | no union | 30 | 28 | 0 |

Eleven non-union specs produced **zero** import failures across 33 draws. The one union-bearing
spec failed at import for **all three** candidates — `openai/gpt-oss-120b`,
`openai/gpt-oss-20b`, `qwen/qwen3.8-27b`. Three unrelated models do not independently fail at
*import* on one task.

Cause: PEP 604 union syntax (`int | float | None`) requires `type.__or__`, added in **CPython
3.10**. The runtime is the venv's **3.9.6**. Annotations are evaluated when the `def` executes,
so on 3.9 the `def` raises `TypeError` at import. Every Executor that faithfully copies the
signature out of the SPEC fails identically. Prove it in one line:

```
python3 -c "def f(x: int | None) -> None: pass"
```

Root cause is in the prompts, not the models. `grep -c '3\.9' agents_core.py` returns **0** —
the runtime version is stated nowhere. And `_INTERFACE_RULE` (`agents_core.py:1561`) *instructs*
the Planner to name exact signatures, giving `median(values: list) -> float` as the model, with
the stated reason that otherwise "every run will fail on import." It trades one import failure
for another.

Why it is blocking rather than cosmetic:

- **It manufactures fake floor tasks.** At ~8% incidence, ~3 of 36 tasks sit at `d_t = 0` for a
  plumbing reason. That eats D-1 gate margin (needs ≥15 of 36 inside the open band) and sends
  D-3 into the floor-heavy branch to hand-read specs for a bug that is not spec drift.
- **It inflates the primary endpoint.** Arm B has up to 3 repair rounds and *sees* the import
  `TypeError`, so it fixes the annotation and passes. A′ and A′@3 have no repair. `B − A′@3`
  gains ~8 points from a Python version mismatch, on an endpoint whose real effect is plausibly
  10–20 points.

Chosen fix: **A + B together.** A states the truth to the models and also covers `match`
statements and runtime `int | str`; B guarantees the annotation class cannot bite even when a
model ignores A.

### Update after the probe finished — B is the primary fix, not the backstop

The completed run says something the first 12 tasks did not. `interval-logic-01`'s spec is
`merge_spans(spans: list) -> list[tuple[int, int]]` — **PEP 585, legal on 3.9, no union
anywhere** — and `openai/gpt-oss-120b` import-failed on it regardless. The spec cannot be the
cause, so the Executor emitted 3.10-only syntax **on its own initiative**, which is the default
annotation style of every current model.

So: Part A acts on the spec. Part C scans the spec. **Neither can see syntax the Executor
invents.** Only Part B covers it. Build Part B first and do not treat it as belt-and-braces.

Final counts, 19 tasks × 3 candidates: `openai/gpt-oss-120b` 16 pass with **all 3 failures at
import**; `openai/gpt-oss-20b` 16 pass, 1 import, 2 assertion; `qwen/qwen3.8-27b` 14 pass, 2
import, 3 assertion. Six import failures in 57 draws — 10.5%, well above the ~8% first estimate,
and concentrated on the leading candidate.

The cause of `interval-logic-01`'s failure **cannot be recovered**: the ledger stores
`code_sha256`, not the code. That is exactly the gap in "Also fix while you are in here," and it
is now a demonstrated cost rather than a hypothetical one. Do that item.

---

## Part A — state the runtime target in the prompts

Add a shared constant beside `_TEST_RULES` (`agents_core.py:1543`):

```python
_RUNTIME_RULE = (
    "RUNTIME: the code runs on CPython 3.9. Anything newer is a syntax or "
    "runtime error, not a style choice.\n"
    "- No PEP 604 unions: `int | None` is 3.10+. It fails at `def` time as a "
    "TypeError in an annotation, and at call time in `isinstance(x, int | str)`. "
    "Use `typing.Optional[int]` / `typing.Union[int, str]`, or no annotation.\n"
    "- No `match`/`case` statements.\n"
    "- No 3.10+ standard library additions.\n"
    "- `list[int]`, `dict[str, int]` and `tuple[int, ...]` are fine (3.9, PEP 585).\n"
)
```

Include it in `PROMPTS["planner"]`, `PROMPTS["executor"]`, `PROMPTS["test_writer"]`, and any
repair prompt that asks for code. The Planner needs it because the Planner writes the signature
the others copy; the Executor needs it because the Executor is who gets blamed.

Do **not** weaken `_INTERFACE_RULE`. Name-pinning is why it exists and it is doing its job — its
example annotation `median(values: list) -> float` is 3.9-legal and should stay. Add one clause
to it: annotate with 3.9-legal syntax or omit annotations entirely.

---

## Part B — make it impossible for an annotation to break the import

At the `handle.write(source)` site in `harness.py` (~line 1222), write a prologue ahead of the
source:

```python
# PEP 563 makes every annotation a string, so a PEP 604 union in a signature
# cannot be evaluated and cannot raise on 3.9. One line, and it does not depend
# on a model obeying an instruction. It does NOT cover `match` statements or a
# runtime `isinstance(x, int | str)` -- those are the prompt's job.
_SOURCE_PROLOGUE = "from __future__ import annotations\n"
```

Requirements on this change:

1. **`ExecResult.source` must remain the model's original, byte for byte.** The record shows
   what the model wrote, not what we ran. If you need the executed text, add a separate field;
   do not overwrite `source`.
2. Legality: a future statement may be preceded only by comments, blank lines, the module
   docstring and other future statements. Line 1 is legal even when the model's code opens with
   a docstring — the only effect is that `__doc__` becomes `None`. A model that already wrote
   the same future import is fine; multiple future statements are legal.
3. **Consequence to accept and document:** `solution.py` line numbers in raw stderr shift by
   one, and that stderr becomes repair context. `failed_assertion_line` is **not** affected —
   `harness.py:1476` only sets it when the deepest frame's basename is `TEST_NAME`. Grep for any
   existing test that asserts a `solution.py` line number out of stderr; if one exists,
   re-baseline it deliberately and **name it in your report**, do not quietly adjust it.
4. **Do not prepend anything to `test_solution.py`.** State that as a deliberate non-change: the
   artifact lives in the solution module, the baked suites carry no annotations, and touching
   suite text invites questions about the lock.

---

## Part C — measure compliance instead of hoping for it

Part A's weakness is that model compliance is unmeasured. Close that with a **report-only**
check: a small offline helper that scans a spec string for 3.10-only syntax and records a flag
on the plan record / run JSON.

- **Warn and record. Never reject, never regenerate, never change control flow.** Rejecting was
  considered as an alternative fix and deliberately not chosen; do not smuggle it in.
- Detect at minimum: a PEP 604 union between builtin type names inside backticks or an
  annotation, and `match ...:`.
- Make it a pure function over a string with its own offline checks, so it costs nothing to run
  and can be applied to existing ledgers retroactively.

The point is that calibration output will then carry the Planner's compliance rate as a number
rather than an assumption.

---

## Part D — a per-day quota violation must not be retried

Registered as D-11 in `eval/prereg/DECISIONS_R3_ADDENDUM_B.md`.

The probe died on the Gemini free tier's **20 generate requests per day, per model**. Verbatim
from the ledger: `quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`,
`RESOURCE_EXHAUSTED`. The retry layer then treated it as transient. Every one of the 17 failed
plans recorded `attempts: 5, retried: 4, seconds_backoff: 32.7`, so the run spent **104 HTTP
requests against a 20-request ceiling** and about 9 minutes asleep waiting for a counter that
resets at midnight Pacific.

Change: when a 429's body identifies a **per-day** quota — match `PerDay` in the `quotaId`, and
fall back to `RESOURCE_EXHAUSTED` plus a `retryDelay` longer than the remaining budget — do not
retry, and abort the run with a message naming the quota and the model. Per-minute quotas keep
retrying; that layer is working and should not change.

Offline check: a stubbed 429 carrying a `PerDay` `quotaId` produces exactly **one** attempt and
raises; a stubbed 429 carrying a per-minute `quotaId` still retries as it does today. Assert on
the attempt count, not just on the exception type.

While you are in the error path: the 429 body is the only place the quota name appears, and it is
currently discarded. Keep the `quotaId` string on the call-log entry so the next exhaustion is
diagnosable from the ledger alone.

---

## Part E — count suites that reject correct code

Registered as D-10. **Report-only, like Part C. Nothing is repaired, regenerated or excluded.**

`grouping-02`'s visible suite is arithmetically wrong. It asserts

```
assert group_totals(data3) == [("B", 3.13), ("A", 3.01)]
```

where `A = 1.004 + 2.001`. In decimal that rounds to `3.01`; in IEEE 754 the sum is `3.00499...`
and `round(sum, 2)` is `3.0`. No correct implementation passes that line. All three candidates
passed the hidden suite and all three were gate-rejected. The suite is `tests_status=generated`
and `tests_trusted=True` — usable, trusted, and wrong. 3 of the 4 false rejections in 57 draws are
this one task; excluding it, the gate's false-rejection rate is 1 in 54, which is the real number.

`run_arm_a_prime3` already handles this correctly — `SELECTION_NO_APPROVAL` is a separate mode
from `SELECTION_NO_GATE` and `selected_by_gate` is `False`. **Do not add instrumentation to the
arm.** What is missing is reporting:

1. Surface the **`SELECTION_NO_APPROVAL` rate** wherever the `SELECTION_NO_GATE` rate is already
   surfaced. The docstring's own argument for separating the modes applies to both: each one
   distorts `A′@3_gate − A′@3_oracle`, in opposite directions.
2. Add a **wrong-suite count**: tasks where the suite is trusted, every draw passes the hidden
   suite, and every draw is gate-rejected. Pure post-hoc computation over stored records, no
   spend, and it must run over an existing ledger so today's probe can be scored with it.
3. Do **not** edit, regenerate or drop `grouping-02` or its suite, and do not add a
   float-comparison tolerance to the gate. Loosening the grader to make a bad assertion pass
   changes every task's gate, which is worse than the bug.

---

## Part F — let the pin probe replay stored specs without re-planning

Registered as D-9. This is what unblocks the pin during the Gemini blackout.

`eval/results/pin-executor/ledger.jsonl` holds **19 complete plan records with full `spec` and
`tests` text**. The pin re-run needs those 19 specs held fixed and only the 57 Executor draws
regenerated under Part B. `is_done()` keys on `ok`, and all 57 old draws are `ok=True`, so a resume
against the same `--out` skips exactly the work that needs redoing.

Add a mode that reuses plan records and re-runs draws — a `--replay-plans <ledger>` reading specs
from an existing ledger into a fresh `--out`, or an equivalent you can justify. Requirements:

- **Zero Planner calls.** Assert this: the run must fail loudly rather than silently plan a
  missing task. If a spec is absent, skip the task and say so.
- Carry `tests`, `tests_sha256`, `tests_status` and `tests_trusted` across verbatim, and assert
  the copied `tests_sha256` matches a rehash of the copied text. A replayed gate that differs from
  the original gate is not a replay.
- Record the source ledger path and the reused `tests_sha256` in the new run's records.
- The 2 union-bearing specs are reused **deliberately** — under Part B they stop failing at import
  and become discriminating again. Do not filter them out.

---



1. **The regression test.** A solution whose signature uses `int | None` executes and passes.
   Confirm it **fails before Part B and passes after** — report both states. If it passes before
   the change, your test is not exercising the path.
2. `ExecResult.source` equals the supplied source exactly, prologue absent.
3. A solution opening with a module docstring still runs.
4. A solution that already contains `from __future__ import annotations` still runs.
5. A solution using a `match` statement **still fails**, and is classified as a syntax/import
   failure. This proves Part B is not silently masking real 3.10 syntax.
6. Part C's scanner: true positives on `int | None`, `str | bytes`, `match x:`; true negatives
   on `list[int]`, `dict[str, int]`, `tuple[int, ...]`, and on prose containing a bare `|`.
7. Part D: a stubbed `PerDay` 429 yields exactly **one** attempt; a stubbed per-minute 429 still
   retries. Assert attempt counts.
8. Part E: the wrong-suite detector fires on a synthetic task where all draws pass hidden and all
   are gate-rejected, and does **not** fire when the suite is untrusted (that is
   `SELECTION_NO_GATE`, a different mode).
9. Part F: replay from a fixture ledger makes **zero** Planner calls, copies `tests_sha256`
   intact, and skips rather than plans a task whose spec is missing.

---

## Must not regress

- Python 3.9 target. Standard library only. Dependencies stay `openai` + `streamlit`. **No
  pytest** — plain `assert` files run as scripts.
- API keys stay in the parent process, never in run JSON, never in a prompt. `_child_env`
  scrubbing must not change.
- Do not touch or "improve" the eight already-correct mechanisms: `_child_env`,
  `start_new_session=True` + `os.killpg`, `-B`/`PYTHONDONTWRITEBYTECODE`, `stdin=DEVNULL`,
  `_decode(errors="replace")`, `RLIMIT_FSIZE`, `_block_processes` neutering of
  `os.system`/`os.popen`/`os.exec*`/`os.spawn*`/`fork`, pruned repair context.
- Do not remove or rewrite `CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU/ceiling-SIGKILL →
  timeout coercion, or `test_signal_death_is_explained`.
- Never cache provider responses keyed on `(prompt, model, params, provider)`.

## Do not do

- Do **not** run `eval/calibrate.py` or `eval/run_eval.py`. No spend in this sprint.
- Do **not** run the Part F replay. Sprint 10 *builds* it and proves it offline against a fixture;
  launching it is a separate decision. It costs 57 Groq calls and zero Gemini calls, but it is a
  measurement and it happens after this sprint is reviewed.
- Do **not** regenerate `eval/tasks.lock` or re-run the ranking battery.
- Do **not** edit anything in `eval/prereg/`. The registration amendments are written separately —
  `DECISIONS_R3_ADDENDUM_A.md` and `DECISIONS_R3_ADDENDUM_B.md` already exist; read them, they are
  where D-7 through D-11 live.
- `eval/results/` is **read-only** this sprint. Part F reads the pin ledger; nothing writes to it,
  nothing is deleted from it, and the replay writes to a fresh `--out` when it is eventually run.

## Also fix while you are in here

- `eval/pin_executor.py:189` `ask_keys()` defaults `reader` to `input`, which **echoes the key
  to the terminal**. A key was exposed this way today. Change the default to `getpass.getpass`
  (stdlib; add the import). Keep the injectable `reader` seam so the suites that patch it are
  unaffected. `app.py:51` already uses `type="password"` and needs no change. The docstring's
  claim — "argv is in the process table, in the shell history and in any crash report that dumps
  the command line. `input()` is not" — is true and incomplete; it misses the screen. Fix the
  docstring too.
- `agents_core.preflight_pair` sends only `PREFLIGHT_PARAMS = {"max_tokens": 1,
  "temperature": 0.0}` and never the role's resolved `params`, so a green preflight does not
  validate `reasoning_format` at all. It validates a pair, not the call. Send the resolved role
  params alongside. Its own docstring argues for this: "an offline stand-in has to be able to
  check the exact slug that would go on the wire."
- The pin ledger stores `extracted_code` as a **bool** plus `code_sha256`, and never the code or
  the child stderr. For a probe whose entire purpose is choosing a model, that makes post-hoc
  diagnosis of a failed draw impossible from the ledger alone — today's root cause had to be
  inferred from spec text and a natural experiment. Record the failing code and the stderr tail
  for `ok=False` and for `passed=False` draws, or say why not.

---

## Report back

- `pwd`, first line.
- Offline check counts **before and after**, with check **names** compared, not just totals. No
  check silently deleted or weakened.
- `test_harness.py` and `test_pipeline.py` green on **3.9.6**, counts stated.
- The before/after state of the Part B regression test, explicitly.
- Any test you re-baselined for the one-line offset, named.
- Diff summary by file. Nothing pushed.

## Git

Stage explicitly by path — never `git add .`, never `git add -A`. No branch, no remote, no push.
Leave `git config` alone. Grep the staged set for key-shaped strings and **read the hits** rather
than expecting zero: three test fixtures legitimately match (`AIzaSy-not-a-real-key-00112233`,
`AIzaSyD-EXAMPLE-KEY-000111222333`, `AIzaSyD-EXAMPLE-KEY-444555666777`). Do not delete the user's
two vim swap files.

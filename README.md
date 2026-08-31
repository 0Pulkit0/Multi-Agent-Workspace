# Multi-Agent Code Workspace

A Planner → Executor → **Harness** pipeline. The verification
stage is not an LLM giving an opinion — it extracts the code the Executor wrote
and *runs it against an acceptance suite of plain asserts*. A step is
`APPROVED` only when those asserts pass.

```bash
source venv/bin/activate
streamlit run app.py
```

Offline test suites — no API keys, no network. 313 checks (172 + 141):

```bash
python3 test_harness.py && python3 test_pipeline.py
```

Key smoke tests (each pinned to the same model ID the pipeline uses, so a pass
means the pipeline will work):

```bash
python3 test_gemini.py
```

## Pipeline

Declared outright in `agents_core.PIPELINES` — every stage listed is a stage
that runs.

| Mode | Stages |
| --- | --- |
| 2 | Planner → Executor (no verification; steps are marked `UNVERIFIED`) |
| 3 | Planner → Executor → Harness |

Mode 3 is the default. Mode 4 used to append an **Orchestrator** — a model asked
to comment on progress. It cost one call per step, could not change any outcome,
and regularly contradicted results the harness had already established by
running the code. It is gone. Asking for mode 4 now runs mode 3 and says why
(`agents_core.RETIRED_MODES`).

What to do after a failure is decided by `agents_core.ESCALATION`, in Python.
The inputs are a verdict, a failure kind and a round number, and choosing from
those needs no judgement:

| Round | Rung | What changes |
| --- | --- | --- |
| 1 | `repair` | The real traceback goes back to the same model |
| 2 | `alternate` | Same traceback, different provider |
| 3 | `fresh` | Start over from the spec on the other provider, with the failed code and traceback deliberately withheld |

The `fresh` rung withholds the traceback on purpose: two rounds of
feedback-driven repair having failed is evidence the *approach* is wrong, and
more detail about the same wrong approach is what keeps a model circling it. The
failing assertion is still included, because that is a fact about the spec.

With only one provider key configured, `alternate` cannot switch models; it
degrades to another repair round, and the step's note records what was actually
tried rather than claiming an escalation that did not happen. When the ladder is
exhausted the step keeps its `REVISE` verdict — the harness ran the code and it
demonstrably failed, which is more than `UNVERIFIED` ("nothing could be
established") would convey — and `record.escalation` is set to `exhausted`.

The Planner emits three sections: `SPEC:`, `STEPS:`, and `TESTS:`. The Executor
is prompted to emit exactly one `python` block that runs on a bare interpreter.
The constraints it is given are `harness._SANDBOX_RULES` verbatim — the same
string the sandbox is built around — so the prompt cannot promise a limit that
is not enforced or omit one that is. It never sees the tests.

## What `APPROVED` means

Exit status alone only shows the program did not crash. A `median` that returns
`ordered[n // 2]` for even-length input runs perfectly and is wrong; nothing
about a zero exit code catches that. So the Planner writes an acceptance suite
up front and the harness grades against it.

- The Executor's program is written to `solution.py`.
- The suite is written to `test_solution.py` in the same directory and becomes
  the entry point; `solution.py` is importable from it.
- `APPROVED` requires: the suite ran, exit code 0, and the suite was audited as
  non-vacuous. All three.

Because the SPEC is the only thing the Executor sees, the Planner is instructed
that the SPEC **must** name the exact function names and signatures the tests
import. Without that, the Executor and the suite disagree on the interface and
every run dies on import.

**One suite, every step.** The Planner produces a single spec-level suite, and
it is run against every step's program — steps are self-contained programs, so
each is expected to expose the full interface the SPEC names. That is uniform
and predictable rather than magic. If you would rather grade only the final
step, `_verify_step` is where the suite is chosen (`tests = run.tests if
run.tests_trusted else None`); gate that on the step being the last one.

## Verdicts

| Verdict | Meaning |
| --- | --- |
| `APPROVED` | The acceptance suite ran against the code and passed. Nothing else earns this. |
| `REVISE` | It failed the suite, crashed on import, timed out, or contained no code block. The real traceback goes back to the Executor as the fixes, up to `MAX_REVISION_ROUNDS` times. |
| `UNVERIFIED` | Nothing could be *established*: the harness itself could not execute, or there is no trustworthy suite to gate on (mode 2, or the suite was rejected). Not treated as a code defect, and the feed says which. |

### The two failure modes get different guidance

They need genuinely different advice, so `format_fixes` distinguishes them:

- **`import`** — "your solution could not even be imported… fix the module
  itself first." A syntax error, a missing definition, or module-level code that
  raises. No test ran at all.
- **`assertion`** — "your solution imported and ran fine — it is simply
  computing the wrong answer," followed by the exact failing assert and its line
  number, and an instruction not to change the test or special-case the input.

Which one it is comes from the *deepest* traceback frame, not from whether
`AssertionError` appears anywhere in stderr. A failing module-level assert
inside `solution.py` is a broken module, not a wrong answer, and conflating the
two sends the Executor chasing the wrong problem.

Harness frames (`_harness_runner`, `runpy`) are stripped from every traceback.

## The vacuous-test guard

A generated suite that asserts nothing would rubber-stamp everything —
reintroducing exactly the fake verification this pipeline exists to remove. So a
suite is not trusted until it earns it (`harness.audit_tests`):

1. It must parse.
2. An `ast` walk must find at least one `assert` statement.
3. It must import something concrete from `solution` (`import *` is rejected,
   because the interface it expects cannot then be checked).
4. **It must fail against a stub.** The suite is run against a synthetic
   `solution.py` whose every function raises `NotImplementedError`. If the suite
   still passes, it was never testing the solution — `assert callable(median)`
   and `assert median is not None` are caught here.

A rejected suite is regenerated **once**, with the rejection reason fed back. If
the second attempt is also vacuous, the run is marked `UNVERIFIED` and the feed
says why, rather than reporting a false `APPROVED`.

`tests_status` on the run records which of these happened: `user`, `generated`,
`regenerated`, `vacuous`, `unusable`, or `missing`.

## The Executor cannot weaken the tests

- The suite handed to the harness is always the **stored** source, re-read from
  run state every round. Nothing from the Executor's output is ever used as a
  test.
- If the Executor's output contains a test file, it is discarded, and the report
  says so (`a test file in the Executor's output was ignored; the stored suite
  was used`). Blocks that import `solution` are excluded from solution
  extraction, so a helpfully-emitted suite is never mistaken for the
  deliverable — and detection scans *every* fenced block, not just the one the
  extractor picked, because the solution block is usually the longer of the two.
- Output containing *only* a test file is `REVISE` with "there was nothing to
  execute", not `APPROVED`.
- **User-provided tests always win** over generated ones. Paste them into the
  "Acceptance tests" box in the UI, or pass `user_tests=` to `run_workspace`.
  They are audited too, but a warning is shown rather than the suite overridden
  — it is your call.

## `-I` and `sys.path` — the gotcha this depends on

The child runs with `-I`, which implies `-E -s`. That leaves **neither the
script's own directory nor site-packages** on `sys.path`, and `-E` means
`PYTHONPATH` is ignored — so `from solution import median` inside
`test_solution.py` raises `ModuleNotFoundError` and no environment variable can
fix it. It is repaired in-process, inside `_harness_runner.py`, before the entry
point is imported:

```python
if _WORKDIR not in sys.path:
    sys.path.insert(0, _WORKDIR)
```

If that repair ever fails, the failure is classified `FAIL_PATH` and reported as
`UNVERIFIED` — a harness bug — instead of being blamed on the Executor.
`test_harness.py` pins this by removing the repair line and asserting the
resulting behaviour.

## Sandbox — and what it does not guarantee

Isolation is layered because no single mechanism is portable. Every run reports
which layers were live in `ExecResult.sandbox_layers`, shown in the feed as
`Isolation: …`. **Read it rather than assuming the strongest layer engaged.**

Always on (in-process, before user code imports anything):

- isolated interpreter (`-I -B`), scrubbed environment, no credentials inherited
- cwd pinned to a throwaway temp dir, removed afterwards
- `socket` and its helpers raise; this is what stops stdlib HTTP
- `subprocess.*` and `os.exec*` / `os.system` / `os.fork` raise
- `RLIMIT_CPU` and `RLIMIT_FSIZE`
- stdin at `/dev/null`, so `input()` fails fast instead of hanging
- wall-clock timeout enforced by killing the whole process group
- per-stream output truncation

Best effort (probed once per machine, strongest rung first, silently dropped if
the platform refuses every rung). Each rung is a layer label, and the label is
the answer to "which OS jail actually held":

- macOS, `os-level:sandbox-exec-no-net+no-write` — denies `network*` and every
  write outside the temp dir
- macOS, `os-level:sandbox-exec-no-net` — the fallback when the profile above is
  refused. It denies the network and **nothing else**: no write is denied at
  this rung, and the write guard is then the in-process one alone
- Linux, `os-level:unshare-net+user` — `unshare --map-root-user --net`
- Linux, `os-level:unshare-net` — `unshare --net`, the fallback where the user
  namespace is unavailable
- `os-level:unavailable` — no rung was accepted

The honest limit: the in-process layer is a guard against LLM code that
casually reaches for the network, not a security boundary against code written
to escape it. Only the OS layer makes "no network" kernel-enforced, and it is
unavailable inside an existing sandbox or without the right privileges — when
the feed says `os-level:unavailable`, that is exactly what happened. Treat
generated code as untrusted regardless.

Two further labels report on the write guard specifically, because
`in-process:no-outside-writes` names a mechanism that was *installed* and a
reader wants to know what stood behind it:

- `paths:in-process-guard+os-write-deny` when the OS rung that held also denied
  writes, `paths:in-process-guard-only` when the guard stood alone — which is
  every macOS fallback rung, both Linux rungs, and `os-level:unavailable`
- `pathlib:accessor-rebound-N`, `pathlib:accessor-rebound-N+M-left-alone`,
  `pathlib:direct-calls`, `pathlib:unimportable`, or `pathlib:unreported` when
  the child never got far enough to say — saying how the guard reached
  `pathlib`. Before 3.11 `pathlib` dispatches through an accessor object holding
  snapshots of `os.*`, so the guard has to re-wrap those slots or they bind as
  methods and every legal call is refused; from 3.11 there is no accessor and
  the patched `os.*` and `io.open` are called directly. The child writes this
  label and the parent reads it, rather than the parent deriving it from
  `sys.version_info`, because the point of the whole family is to report what
  ran.

`RLIMIT_CPU` is deliberately set to the wall-clock timeout **plus
`CPU_GRACE_SECONDS`**, so the wall-clock kill (which can be explained) normally
wins over `SIGXCPU` (which arrives as a bare negative exit code). Any signal
death is named in stderr rather than left as `exit -24`. When a kill *is*
attributable to the CPU ceiling — `SIGXCPU`, or a `SIGKILL` that arrived at or
after the deadline — it is coerced into a timeout so the Executor gets timeout
guidance instead of an unexplained signal.

`RLIMIT_AS` and `RLIMIT_DATA` are now attempted at `MAX_MEMORY_BYTES`. The old
objection (capping address space breaks numpy on import) no longer applies,
because `-I` excludes site-packages and numpy is unimportable in here anyway.
**No guarantee is claimed**: Darwin ignores both for practical purposes, so the
layer list reports the truth per platform — `memory:rlimit-as-2048mb` where it
took, `memory:unguarded-on-darwin` where it did not. A layer is never listed as
present when it is not enforced.

### Runaway output

`proc.communicate()` read a child's entire output stream into the parent before
anything trimmed it, so `while True: print("x" * 1000)` exhausted the *parent's*
heap — the harness DoS'ing itself on code it was supposed to contain. Output is
now drained incrementally by two reader threads with a `MAX_CAPTURE_BYTES`
(2 MiB) ceiling per stream; past that the child is killed by process group and
the step is reported as `runaway-output`, which is a distinct failure kind with
its own repair guidance. This is a byte limit, not a time limit: a program that
floods the pipe is killed in milliseconds rather than being allowed to run out
the clock.

### Writes

`_block_filesystem()` wraps `builtins.open`, `io.open`, `os.open` and the
path-taking functions in `os` and `shutil` to refuse writes resolving outside the
working directory. `pathlib` is imported *after* the patching so its 3.9
accessor snapshots the guarded functions. Reads are deliberately left alone.

This is accident containment against hallucinated destructive operations —
`shutil.rmtree("/")`, writing to a home-directory dotfile — **not a security
boundary**. Adversarial code can bypass it via `ctypes`, `importlib`, or a raw
syscall. `RLIMIT_FSIZE` caps how much a permitted write can produce.

### Timeouts say where

A timeout used to collapse three different situations into one message. The
child now writes a phase marker before each stage, so a killed process (which
leaves no traceback) is still attributable: `timeout-import` (module-level code
loops, so the suite never ran a single test), `timeout-tests` (the suite ran and
something in it hung), `timeout-teardown` (the work finished but the interpreter
would not exit), or plain `timeout` when the marker is unreadable. Each gets
different repair guidance, because "your import blocks forever" and "your
algorithm is too slow" are not the same defect.

## Does the pipeline actually help? (`eval/`)

The claim "verification improves output" is worth exactly as much as the
measurement behind it, so there is one. `eval/` compares **Arm A** (one Executor
call on the raw prompt — no plan, no suite, no repair) against **Arm B** (mode
3), and grades both **only** against a hidden reference suite neither arm ever
sees. Arm B's own generated suite decides what it *repairs* against and nothing
else; a pipeline allowed to grade itself will report whatever it likes.

```bash
python3 eval/gen_tasks.py --self-check --per-family 12   # 216 tasks, 0 problems
python3 eval/run_eval.py --stub flaky --arm both         # offline plumbing check
python3 eval/run_eval.py --arm both --per-family 2       # needs provider keys
```

- `gen_tasks.py` — 18 task **families** (the family is the experimental unit,
  not the tier), each parameterised so variants differ in *behaviour* and the
  prompt says which. Deterministic, seeded, no LLM. Each task carries a prompt,
  a hidden reference, a hidden suite, and a tier. `--self-check` proves every
  suite passes its reference, **fails** a `None`-returning stub, never leaks the
  reference or the tests into the prompt, and that same-prompt variants agree on
  behaviour.
- `PREDICTIONS.md` — expected pass rates per arm, per tier, per family, frozen
  before anything ran, with four stated falsification conditions.
- `run_eval.py` — resumable (one JSON per run), token-bucket rate governor whose
  sleep is counted *inside* the reported wall clock, `Retry-After` read from the
  429 response header, and a `--stub` mode that validates the whole apparatus
  offline.
- `REPORT.md` — stratified per tier and per family, never one aggregate.

The number the pipeline cannot self-report is the **false APPROVED**: a step the
harness approved that then fails the hidden suite. The stub run shows why it has
to be measured externally — a suite that passes `audit_tests` (parses, asserts,
imports a concrete name, fails against a stub) still missed a real defect in 15
of 16 approvals. The audit rules out suites that test *nothing*; it cannot rule
out suites that test *too little*.

The model-facing numbers in `REPORT.md` are **not filled in**: this environment
has no provider key and egress to both providers is denied. The report says so
rather than estimating.

## Notes

- **No new dependencies.** The suite is plain `assert` statements run by the
  interpreter itself — no pytest, no unittest. `requirements.txt` is still just
  `openai` and `streamlit`, and everything targets Python 3.9.
- **Run logs.** Each run writes `runs/<timestamp>-<id>.json` and never reads
  prior state. The old single `memory.json` was loaded on startup and appended
  to, so every run inherited every previous run's log. The stale
  `memory.json` at the repo root is no longer read or written by anything and
  is safe to delete.
- **Context is bounded.** The rolling context used to be built by string
  concatenation with no ceiling, growing with every step and revision until the
  provider rejected the request. It is now assembled from structured state and
  clamped at every level (`MAX_SPEC_CHARS`, `MAX_STEP_OUTPUT_CHARS`,
  `MAX_CONTEXT_CHARS`); only the most recent `CONTEXT_RECENT_STEPS` carry full
  output, older ones degrade to titles.
- **Section-scoped parsing.** `SPEC:`, `STEPS:` and `TESTS:` are sliced apart
  before anything is parsed, so a numbered line inside the test code block
  cannot be mistaken for a plan step.
- **Feed renders live.** `run_workspace` takes `on_event(role, content)` and
  calls it as each entry is produced, so the UI paints entries as they happen
  instead of after the whole run returns. The acceptance suite gets its own
  entry, so you can see what `APPROVED` is being measured against.
- **Model IDs.** `gemini-3.6-flash` was verified against
  ai.google.dev/gemini-api/docs/models on 2026-08-28 — it is a real, stable
  model. The dead slug was in `test_gemini.py`, which hard-coded the
  shut-down `gemini-2.0-flash`; both smoke tests now read `PROVIDERS` so they
  cannot drift from the pipeline. Use the `gemini-flash-latest` alias if you
  would rather not pin a generation.
- **Provider fallback** lives in one place (`call_role`) instead of four
  copy-pasted try/except blocks.

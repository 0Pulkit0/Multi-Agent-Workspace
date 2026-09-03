# Sprint Brief 23 — Product, not eval

## Framing

Every brief from 4 through 22 built or audited the **measurement apparatus**. This one changes the
**system**. Nothing here is registered, nothing needs an addendum, nothing is gated on a sweep.

The eval is finished. D-1 failed and addendum L (`b761f06`, D-23) recorded it: 23 of 26 measured
tasks sit at `d_t = 1.00`, so the corpus has no headroom for any architecture to win on. A
completed, recorded result cannot be retroactively invalidated, so **changing the app now breaks
nothing.** L states which configuration was measured; the app moves on. That releases the Executor
prompt from its freeze without a branch and without touching a banked draw.

One number for why this sprint exists. Python in this tree, excluding `venv`:

| file | lines |
|---|---|
| `test_pipeline.py` | 9,565 |
| `agents_core.py` | 3,177 |
| eval machinery (`gen_tasks`, `run_eval`, `calibrate`, `rank_battery`, `pin_executor`, `audit_leakage`, `decoy_probe`, `import_pin_plans`, `models`, `prove_shared_gate`) | ~9,900 |
| `harness.py` + `test_harness.py` | 4,127 |
| **`app.py` — the product** | **231** |

29,424 lines, of which the product is **0.8%**. That is the diagnosis, and it is why the project
feels slow while the work is sound.

---

## What I checked first, and three things I had wrong

I read the prompt layer and the repair path before writing this, and three items I had queued for
this sprint turned out to be already done or worth less than I claimed. Corrections up front so
you do not spend a day re-implementing working code.

**1. "The Executor prompt is missing the Python 3.9 constraint." False.** `_RUNTIME_RULE`
(`agents_core.py:2082-2091`) already forbids PEP 604 unions and `match`/`case` explicitly, with the
correct replacements, and is concatenated into all three prompts. The model violated a rule that
was present. The consequence matters more than the correction: **prompt-level fixes have hit
diminishing returns on this class**, so the leverage is mechanical enforcement of rules already
stated, not more rule-writing.

**2. "Confirm the repair prompt carries the failing assertion, the input, expected vs actual, and
the traceback." Already carried, and better than I asked for.** `harness.format_fixes`
(`harness.py:1943-2060`) branches across seven distinct failure kinds — import, assertion, four
timeout flavours, runaway output — and each branch gives different advice. It includes the exit
code, the timeout flag, the failing assertion with its file and line number, spliced
stderr/traceback, and stdout-before-failure. **Do not rewrite this.** The one genuine gap is noted
in Task 4.

**3. "Make model diversity on the repair rungs deliberate." Already deliberate.**
`_revision_request` (`:3087-3131`) calls `alternate_provider("executor", keys)` on both the
`fresh` rung (`:3122`) and the `alternate` rung (`:3130`); only `repair` keeps the same model, by
design, and the comments say why. Two of three rungs already decorrelate. My note recorded this as
an *eval confound*, which it was — and which is moot now the eval is closed. As product behaviour
it is correct. Leave it.

---

## Task 1 — Make APPROVED mean more: a rule-to-assertion coverage gate

**Highest value in the sprint.** The system's only definition of output quality is "APPROVED means
the asserts passed." That claim is exactly as strong as the suite is complete, and today nothing
checks completeness against the spec.

There is already a gate on the other side of this: a Planner-written suite is audited against a
stub and rejected if it would pass against unimplemented functions, and `_TEST_RULES`
(`:2042-2058`) rejects `assert callable(f)`, `assert f is not None` and `assert hasattr(...)` by
name. So *vacuous* suites are handled. **Silent suites are not.** The weak clause is `:2054`:

```
"- Cover the normal cases AND at least one edge case.\n"
```

One edge case satisfies it. A spec stating four distinct error behaviours can be fully satisfied by
a suite that tests none of them.

**This failure class is measured, in this project, on this corpus.** `config-parsing-01`'s prompt
states *"An assignment before the first section header raises `ValueError`"*, `_ref_parse_config`
implements it, and **no check in its suite exercises it** — proven unreachable at every seed, with
four independent witnesses (see the difficulty-dial notes). The consequence is the sharp part: **a
candidate implementing the opposite branch passes `config-parsing-01` in full.** That is a graded
APPROVED on a solution that violates a stated rule. The generator instance is frozen and off
limits; the app instance is not, and it is the same defect.

**Do this, in two halves.**

*The prompt half.* Replace `:2054` with a clause that scales with the spec rather than a constant:
one assertion per behaviour the spec states, and error behaviours counted explicitly — every
"raises", "rejects" or "must fail" clause in the spec needs an assertion that provokes it. Keep the
existing bullets; this replaces one line. `_TEST_RULES` is shared between the Planner prompt and
the regeneration prompt, so a single edit reaches both and they cannot drift.

*The mechanical half*, which is the part that survives a model ignoring the prompt. Extract the
spec's stated error clauses (regex over the spec text for `raise`/`raises`/`rejects` plus a nearby
exception name is sufficient — the spec is prose, and this is the same heuristic-over-prose posture
`scan_runtime_syntax` already takes, for the same reason). For each one, require the suite to
contain an assertion that reaches it: a `try/except <Exception>` block, or a helper call naming the
exception. Zero matched clauses on a spec that states one is a coverage failure.

**On what to do with a failure — mind the quota.** A rejected suite is a regenerated suite, and the
Test Writer is Gemini, which at `gemini-3.6-flash` is **20 requests/day, 19 usable**. That is the
binding resource in this whole system. So: allow **at most one** regeneration on a coverage
failure, then proceed with a loud, honest banner rather than blocking the run. Surface the
uncovered rules in the UI either way (Task 6 renders them) — an APPROVED that says "3 of 4 stated
rules have assertions" is more useful than a blocked run, and it is the honest number.

**Report before/after check names** for anything you add to `test_pipeline.py`, per standing rules.

---

## Task 2 — The Planner records the ambiguities it resolved

The product's real input is `"make me a simple snake game in python"`. The Planner's measured
behaviour on prompts like that is *silent competent resolution*: it picks a grid size, a tick rate,
a wrap-or-die rule, writes them into the spec as if they were given, and never says they were
choices. Everything downstream then verifies the wrong thing correctly.

**Do this:** add a clause to the Planner prompt requiring an explicit `ASSUMPTIONS` section — every
ambiguity in the user's prompt that it resolved, and the resolution, one line each, or the literal
line `ASSUMPTIONS: none` when the prompt was fully specified. Surface it in the UI as its own
panel, placed *above* the spec (Task 6).

This is the cheapest item in the sprint and probably the most visible to a user, because it is the
one that lets them notice a wrong guess before the pipeline spends four model calls honouring it.
It also composes with Task 1: a recorded assumption is a stated rule, and a stated rule needs an
assertion.

Do not require a machine-readable format. A prose section under a fixed header is enough, and a
brittle format is a new failure mode in the parser.

---

## Task 3 — Close the answer-key read channel

`harness.run_python_sandboxed` writes the hidden suite as `test_solution.py` in the same directory
as `solution.py`, and `_guard_open` (`harness.py:664-673`) gates write modes only: `:667` computes
`writing = any(char in mode for char in "wxa+")`, `:670` raises only when `writing and not
_fs_allowed(file)`, and every read falls through to the real `open` at `:672`. A solution can read
the suite it is about to be graded against.

`decoy_probe.py` measured **k = 0 exploitation in n = 30 trials**, with an echo-rate upper bound
around 9.5%, so nothing is exploiting it today. Fix it anyway: the project's entire quality claim is
that APPROVED means the asserts actually passed, and an open channel to the answer key is the one
defect that makes that claim unfalsifiable rather than merely wrong.

**Cheapest sufficient fix first:** keep the suite out of the solution's directory. Write it to a
sibling temp dir and run it with that dir on `sys.path`, or read it into memory and `exec` it rather
than materializing it next to the solution. Only if neither works without disturbing the sandbox's
other guarantees, add a read guard on the suite path specifically.

**Do not** touch `_child_env`, `start_new_session=True` + `os.killpg`, `-B` /
`PYTHONDONTWRITEBYTECODE`, `stdin=subprocess.DEVNULL`, `_decode(errors="replace")`, `RLIMIT_FSIZE`,
`_block_processes`, `CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU / ceiling-SIGKILL → timeout
coercion, or `test_signal_death_is_explained`.

---

## Task 4 — Cheap prechecks on the emitted program (low priority; land 1–3 first)

I originally ranked this first, on the theory that `scan_runtime_syntax` (`:2203`) was a ready-made
gate merely pointed at the wrong artifact — it runs on `run.spec`, report-only, at `:2572-2574`, and
never inspects the Executor's program. Reading its docstring demoted the task, and the reasoning is
worth carrying because it is the general shape of "is this enforcement worth adding":

- `harness._SOURCE_PROLOGUE` is `from __future__ import annotations`, so a PEP 604 union **in an
  annotation is already dead** — it never evaluates. The docstring says so directly: the prologue
  "is what actually removes the failure mode; this only measures how often the Planner needed it."
  A naive scan-and-repair on the solution would therefore burn a Groq call and a repair round on
  syntax the prologue has already neutralized.
- The residue the prologue cannot cover is exactly two things: `match`/`case`, which is a hard
  SyntaxError on 3.9, and a union in an *evaluated* position — `isinstance(x, int | str)`,
  `cast(int | None, v)`, a module-level `Num = int | float`.
- And both of those **already reach a repair round today**, because the harness executes the code
  and `format_fixes` hands back the traceback. So this task buys precision and latency, not a new
  capability.
- The docstring's stated reason for not rejecting is `"a rejected spec is a re-planned spec and
  Planner calls are the binding constraint"` — which is **spec-specific**, and confirms the
  asymmetry: the Executor is Groq, not the scarce resource. So different handling on the solution is
  justified. The reason does not generalize; the cost-benefit does.

**So do the cheap, exact version and skip the heuristic one.** In the parent process — which *is*
CPython 3.9.6 — `compile(source, "solution.py", "exec")` the extracted program before spawning the
sandbox. It is free, exact, catches `match`/`case` and every other syntax error, and yields a
precise line and message. On a `SyntaxError`, skip the subprocess and open a repair round quoting
the message, the line, and the relevant `_RUNTIME_RULE` bullet.

Optionally add the evaluated-position union scan on top, restricted to `isinstance(`/`cast(` and
module-level assignment, reusing `_TYPE_NAMES` (`:2154-2163`) so a set expression is not a false
positive. Low priority; the traceback path already handles it a round later.

**Leave the spec scan exactly as it is.** Report-only on the spec is correct and the docstring
explains why.

---

## Task 5 — The one real gap in the repair channel

`format_fixes` quotes the failing assertion's *source text* and its line number, which is most of
what a repair needs. What it cannot give is the **actual value**, because the suite is plain
module-level asserts and a bare `assert x == y` raises an `AssertionError` carrying nothing. So the
Executor is told `assert median([1,2,3,4]) == 2.5` failed and must infer what its code returned.

Two options, and I do not have a strong preference:

1. On `FAIL_ASSERTION`, have the child re-evaluate the failing expression's left-hand side and
   report the value. Precise, and cheap in tokens. Costs a second execution and needs care: the
   re-evaluation must be inside the same sandbox with the same limits, and a side-effecting function
   makes the second value not necessarily the first.
2. Have the harness rewrite each `assert a == b` in the suite into a form that reports both sides on
   failure before running it. No second execution, but it mutates a suite the project treats as
   fixed evidence, and the rewrite is a new parser to get wrong.

Option 1 is more honest about what it measured; option 2 is cheaper at runtime. Pick one, state
which, and put the tradeoff in the commit message. If neither looks clean in under an hour, **skip
this task** — it is the smallest gain in the sprint and the existing message is already good.

---

## Task 6 — The UI: build a verification console, not a chat log

`app.py` is 231 lines of default Streamlit: emoji role labels, two tabs, three `st.metric` cells, no
CSS. It works, and it looks like every other multi-agent demo on the internet — because every other
one is a chat transcript, and so is this.

**The design thesis, and every decision below follows from it.** The one thing this system does that
the others do not is *execute the code and grade it against asserts*. So execution and grading are
the hero, and the model chatter is the supporting material. The reference points are a CI run and a
debugger, not a messaging app.

**The design language is typographic, not decorative.** Monospace for everything that came from a
machine — code, tracebacks, assertions, exit codes, timings, provider slugs. A humanist sans for
everything a model asserted — spec prose, assumptions, plans. A reader can then tell measured fact
from model claim without reading a word. That split is worth more than any amount of colour.

**Palette.** Near-black ground (`#0B0D10`), one raised surface (`#14181D`), hairline borders
(`#232A31`). Exactly three semantic colours and no others: pass `#3DD68C`, fail `#FF6B6B`, in-flight
`#F2C14E`. Body text `#C9D1D9`, dim metadata `#7D8590`. Ship a `.streamlit/config.toml` with the
matching base theme so the chrome Streamlit owns does not fight the CSS. Inject the rest with one
`st.markdown(STYLE, unsafe_allow_html=True)` block near the top of `app.py`; keep it in a single
module-level constant so it is one thing to read and one thing to change.

**Retire the emoji role labels.** `ROLE_STYLE` (`:118-124`) is fine scaffolding and the wrong final
look. Replace with a coloured left rule plus a small-caps role name.

### 6a — The pipeline rail

A horizontal row of stage chips across the top, driven by the `on_event(role, content)` stream that
already exists: `planner → tests → executor → harness`, from `pipeline_for(mode)` so it follows the
selected pipeline instead of hardcoding four. Each chip carries the role name, and under it the
provider and model actually serving it, from `RESOLVED_ROLES`.

Three states, and the transitions are the whole animation: dim (`#7D8590`, not started) → amber with
a slow pulse (in flight) → solid, pass or fail coloured (done). A stage goes amber when its first
event arrives and solid when the next stage's first event arrives; the harness chip resolves on the
verdict. This makes the multi-agent structure legible in one glance, which is the product story, and
it costs one CSS keyframe.

Put `independence_warnings()` inline on the rail rather than in the sidebar — a configuration where
the grader and the author are the same model is a fact about the pipeline, and it belongs on the
picture of the pipeline.

### 6b — The verdict hero

Replace the three `st.metric` cells with one full-width card, because they are not three
co-equal numbers — one of them is the answer.

```
  APPROVED            14 of 14 assertions passed
  exit 0 · 1.8s · sandboxed: no network, no subprocesses, no stdin · 15s ceiling
```

Fail case leads with where it died, not with a word:

```
  REJECTED            failed at test_solution.py:9
  assert median([1, 2, 3, 4]) == 2.5
  exit 1 · AssertionError · 3 repair rounds spent
```

Everything here already exists on the run record: `tests_status`, `verified_count`, `len(steps)`,
`result.failed_assertion`, `result.failed_assertion_line`, `exit_code`, `timed_out`,
`harness.EXEC_TIMEOUT_SECONDS`. Keep the existing `tests_trusted` warning — the "no trustworthy
acceptance suite, so no step could be APPROVED" path is the most important sentence in the current
UI and must stay at least as prominent.

### 6c — The suite as a checklist, rendered honestly

**The highest-impact single view in the app.** Split the acceptance suite into its individual
`assert` lines and render each one as a row with a status marker. This turns APPROVED from a word
into evidence, and it is the screenshot that makes the project legible to anyone in two seconds.

**Render three states, not two.** A plain-assert script dies at the first failure, so on a rejection
the true state is: every assert *before* the failing line passed, the failing one failed, and
everything *after* it **was never reached**. Colouring the tail red would be a false claim — grey it
and label it `not reached`. On APPROVED all of them ran and passed, so full green is correct and the
count is exact. Getting this right is the difference between a dashboard and an honest instrument,
and this project has spent 29,000 lines earning the right to the second one.

Show the count of stated spec rules that have a matching assertion here too, once Task 1 computes it
— `3 of 4 stated rules covered` under the checklist, with the uncovered rule named.

### 6d — The repair timeline

A vertical timeline of the escalation ladder, one node per attempt, collapsed by default and
expandable. Each node shows the rung (`repair` / `alternate` / `fresh`), the provider and model that
served it, and the failure it was handed. `ESCALATION` (`:41`), `ESCALATION_WHY` (`:44-45`),
`record.rounds`, `record.escalation` and `record.provider` already carry all of it, and
`ESCALATION_WHY`'s strings are already written for a human — use them as the node subtitles verbatim.

This is the view that shows the system *recovering*, which is the second half of the product story
and is currently invisible: today a three-round repair looks identical to a first-try pass except for
a longer feed. Label the provider change explicitly on the `alternate` and `fresh` nodes — "same
traceback, different model" is a legitimately interesting thing to watch work.

### 6e — The assumptions panel

Task 2's `ASSUMPTIONS` section, rendered above the spec, in the sans face, with a quiet "these were
guesses" framing. On `ASSUMPTIONS: none`, render nothing — an empty panel trains people to ignore
the full one.

### 6f — The cost strip

One line, small, always visible:

```
  $0.00 spent · Gemini 17 of 19 requests left today · Groq well under cap
```

Zero cost is the single most differentiating fact about this project and it is currently stated
nowhere in the UI. The Gemini number is real and worth showing because it is the binding constraint:
`gemini-3.6-flash` is 20 requests/day, **19 usable**, 18 with preflight. Count locally from the run
log rather than asking the provider — **do not probe the RPD cap.** If a local count is not reliable
across restarts, show the cap and the session's usage and label it "this session" rather than
inventing a daily figure.

### 6g — What must not regress

Two pieces of the current file are load-bearing against Streamlit's rerun model, and both are easy
to lose in a rewrite:

- **The pacer guard at `:29-30`.** `if not agents_core.PACER.enabled: agents_core.set_pacing(True)`.
  Streamlit re-executes the script top to bottom on every interaction, and re-installing the pacer
  throws away the last-call times, which is exactly the state that prevents a burst. Keep the guard
  and keep the comment.
- **The feed replay at `:203-206`.** `elif st.session_state.log:` re-renders a completed run on plain
  reruns — every widget change and tab click. Every new panel in this task needs the same treatment:
  drive it from `st.session_state`, never from a local computed during the run, or the whole console
  blanks the first time someone touches the sidebar. Test it by clicking a tab after a run.

Also keep: the redaction at both `except` sites (`redact(exc, stored_keys)` — an SDK error can quote
the request that carried a key, and `emit` feeds both the UI and the memory JSON), the password-typed
key inputs, and the download button.

---

## Task 7 — Features worth building now

Ordered by ratio of "makes the thing genuinely better" to hours. The first two are Task 6 panels and
are listed here because they are features, not styling.

1. **Assertion-level provenance** (6c). Already argued. Build it.
2. **The repair timeline** (6d). Already argued. Build it.
3. **Quota preflight.** At 19 usable Gemini requests a day, discovering exhaustion *after* the
   Planner call has burned one is the worst possible ordering. `PREFLIGHT_SYSTEM` (`:1038`) already
   exists as a one-token probe. Check before the run starts, and if the budget cannot cover the
   selected pipeline, say so in the verdict slot and do not start. Cheap, and it converts a confusing
   mid-run failure into a clear pre-run refusal.
4. **Single-file HTML run export.** One self-contained `.html` with the rail, verdict, suite
   checklist, repair timeline and final deliverable inlined — no external assets. The existing
   download button emits `deliverable.md`, which drops all the evidence. A shareable artefact of a
   *verified* run is the most portfolio-legible thing this project can produce, and it is an hour of
   string templating over state that already exists.
5. **A demo replay fixture, and read this constraint before building it.** One recorded run, bundled
   as a labelled fixture, so someone with no API keys can see the console work. **It must never serve
   a live request.** The standing prohibition on caching provider responses keyed on
   `(prompt, model, params, provider)` is absolute — the resume primitive in this project is
   stage-level, never replay at the unit of a prompt. So build it as a display-only artefact:
   loaded by an explicit "View demo run" control, watermarked in the UI as a recording, incapable of
   being reached by `run_workspace`, and stored in a file whose name says so. If that separation is
   not clean, do not build it.
6. **Bring-your-own-suite, promoted.** The user-supplied acceptance suite already exists and works,
   buried in a collapsed expander below the prompt. It is the feature that best demonstrates the
   thesis — *you* write the asserts, the system has to satisfy them. Give it a visible tab or toggle
   next to the prompt.

Explicitly *later*, and do not start them: run history across sessions, multi-file projects, a model
picker UI, diffing two runs, streaming token-by-token output.

---

## Task 8 — The README, and an honest-claims section

`README.md` is 19 KB written across the apparatus era. It needs a rewrite around the product, and it
needs one section this project has earned the right to write well.

Structure: what it is in two sentences; a screenshot of the new console on an APPROVED run; an
architecture diagram (four boxes, the harness drawn as the gate everything must pass, plain ASCII or
a small SVG — do not add a dependency); how to run it; what the harness guarantees, quoted from the
constants rather than described; then the honest-claims section.

**The honest-claims section, and get this right.** The temptation is to imply the eval showed the
multi-agent pipeline winning. It did not, and it did not show it losing either. The accurate
statement:

> The pipeline was measured against a solo baseline given the same verifier and the same retry
> budget. The comparison did not resolve: the difficulty gate failed, because 23 of 26 measured
> tasks were solved by the *baseline* on the first attempt, leaving no headroom for any architecture
> to demonstrate a difference. The benchmark was too easy to answer the question it was built to
> answer. What is verified is narrower and still real: APPROVED means the generated code was executed
> in a sandboxed subprocess and passed an acceptance suite of concrete asserts, with the suite audited
> against a stub so it cannot pass against unimplemented functions.

State which configuration L measured and note that the app has changed since. That paragraph is a
better portfolio artefact than a claimed win — building the apparatus, running it, and reading a
negative result off it correctly is a harder skill to demonstrate than a green number, and anyone
qualified to evaluate the project will read it that way.

Link the prereg and addenda directory rather than summarising it. Do not restate the eval design in
the README; it exists, it is committed, and it is long.

---

## Standing constraints — all still in force

- Python **3.9 only**, standard library only. Dependencies stay `openai 2.48.0` + `streamlit 1.50.0`.
  **No pytest** — plain `assert` files run as scripts.
- `./venv/bin/python` only, never bare `python3`. Run `pwd` first. Work only in
  `~/Desktop/Multi agent project/`. Shell is **zsh**.
- **Offline check counts reported before and after, with check *names* compared.** No check silently
  deleted or weakened; a rename or a split is fine and must be called one.
- Keys stay in the parent process. **Never written to the run JSON and never fed into a prompt** —
  `_child_env` scrubs the child env and that must not regress. Keys are entered via the app UI or
  `getpass`, **never on a command line or in shell history**. The zsh-safe pattern is
  `printf 'Groq key: ' && IFS= read -rs GROQ_API_KEY && export GROQ_API_KEY && echo`.
- **Never cache provider responses keyed on `(prompt, model, params, provider)`.** Stage-level resume
  only. This governs Task 7 item 5.
- Do not "fix" the already-correct mechanisms: `_child_env`, `start_new_session=True` + `os.killpg`,
  `-B` / `PYTHONDONTWRITEBYTECODE`, `stdin=subprocess.DEVNULL`, `_decode(errors="replace")`,
  `RLIMIT_FSIZE`, `_block_processes`'s neutering of `os.system` / `os.popen` / `os.exec*` /
  `os.spawn*` / `fork`, `CPU_GRACE_SECONDS`, `_signal_name()`, the SIGXCPU / ceiling-SIGKILL → timeout
  coercion, `test_signal_death_is_explained`, and the pruned repair context.
- **In-process sandbox guards are accident containment, not a security boundary.** The
  stdout → context → memory-JSON secret channel is a production blocker before any real folder is
  pointed at this, and the accident-containment posture stops being honest the moment task prompts
  incorporate fetched web content. Gemini scarcity is **not** to be solved with multiple Google
  accounts.
- Git: never `git add .` or `-A`, stage explicitly by path. **No branch, no remote, no push.** Leave
  `git config` alone. **Read the whole `git diff --cached` before every commit.** Grep the staged set
  for key-shaped strings **and read the hits** — legitimate fixtures live at `test_pipeline.py:1047`,
  `:3961`, `:4072`, `:4111` and `agents_core.py:1359`. Prefer new commits over `--amend`. Do not
  delete the two vim swap files.
- Do not pin anything to a rolling alias (`gemini-flash-latest`). Do not probe `gemini-3.6-flash`'s
  RPD cap. Do not add a key-reading path to `decoy_probe.py`.
- Never grep a mechanism by its call spelling. No inline `#` comments inside pasteable shell commands.
- Subagents: read-only fan-out only, no writes to files under edit, no design decisions.

---

## Out of scope — do not start any of these

- **`eval/gen_tasks.py` is off limits** (`SPRINT_BRIEF_22.md:146`, addendum L §10). The `stray`
  coverage gap is a *finding* that motivates Task 1; it is not a thing to fix in the generator.
- No lock regeneration, no `eval/tasks.lock` edits, no `MEASURE_TASKS` edits, no backfilling any field
  onto the 260 existing draw files.
- `eval/results/` is read-only. Committed files in `eval/prereg/` are never edited — corrections
  become new addendum letters. Next free letter is **M**, next free D-number is **D-24**.
- No difficulty-dial implementation, no ceiling-heavy remedy, no L §9 blind-family exception. All of
  that is reserved to addendum M / D-24 and is a separate sprint.
- No sweep, no draws, no calibration.
- No rewrap or line-width pass on any file.
- **The Executor prompt is now unfrozen** — the eval is complete and L records the measured
  configuration. But changing it is still a decision, not a side effect: if a task above needs it
  changed, say so explicitly in the commit message and state what the change was.

---

## Suggested commit sequence

One commit per task, in order, each with its check-name delta in the message. Tasks 1 and 2 are the
ones that change output quality, so land them first and report before starting Task 6 — the UI work
is the largest block of hours in the sprint and the easiest to stop cleanly if the earlier tasks
turn up something better to do.

Report after Task 5 with: what landed, the check-name deltas, anything in Tasks 1–5 you judged not
worth building and why, and whether Task 6 still looks like the right next block.






# Sprint 8 — make the lock's claim true, move models out of source, and settle the ranking heuristic

Run `pwd` first and confirm `~/Desktop/Multi agent project`. Read this whole file before editing.

Sprint 7 is verified in, independently. I re-ran both suites, extracted every check *name* from a
clean `git archive` of `b78cff5` and from the current tree, and diffed the two sets: exactly one
baseline name is absent, `arm order is reproducible for one task`, which is Sprint 6's already-reported
rename. **463 of 464 baseline checks survive verbatim, 347 are new, 810 total.** I reproduced the
clean audit at `36 task(s), 1281 literal(s), 756 prompt(s)` with `executor=648, planner=72,
test_writer=36`, CLEAN, rc 0, in 28.6s — the governor fix is real and the Test Writer gap is closed.
`_guard_open` and `_fs_allowed` are byte-identical as you said; the apparent diff on `_fs_deny` is
just `_fs_deny_fd` inserted after it. Calibration checks out: constants where you say, `12`/`30`
only inside the `GATE_AS_REGISTERED` string, `--out eval/results/sneaky` refused with exit 2,
perfect stub → correct NO-GO.

**You were right about Task 3 and I was wrong.** The 28-row wrap table was already at
`harness.py:489-513` in the baseline. I grepped for the dotted spelling `os.remove` and got zero,
because the table is built from `(os, "remove", (0,))` tuples and applied with `setattr` — and I had
read `_guard_open` at `:474` the sprint before and stopped fifteen lines above the answer. That is
the second error of this exact class from me, after the 429 one. Your keyword-bypass finding is
sharper than anything the brief asked for, and the `FAIL_PATH` refusal is correct: routing an escape
to a label that means "our bug" would make it `UNVERIFIED`, therefore ungraded and unrevised, and
every escape attempt would silently leave the measurement.

**810 is the baseline** (219 harness + 591 pipeline). Report before and after, compare check names
and not just counts, delete nothing, weaken nothing. All prior ground rules stay binding: Python
3.9, stdlib only, deps stay `openai` + `streamlit`, no pytest, keys never in a log or a prompt,
`_child_env` must not regress, the four protected items untouched, the eight already-correct
mechanisms unimproved, `eval/prereg/` read-only. **Do not commit**; report `git status --short` and
`git diff --stat`.

Out of scope: D2, the Part C contrasts, the Part E cold grading pass, `PREDICTIONS.md`, B9, D4's
content-addressed store, and full AST mutation scoring of `audit_tests`. Do not compute any
contrast. **Do not choose a replacement Groq model** — that is a registration decision and it is
not yours; build the machinery that makes the choice configurable and checkable.

**Subagents: yes for reading, no for writing, no for design.** Same terms.

---

## Task 1 — `tasks.lock` records a claim that is not true

Your finding 6(e) is bigger than a task-set curiosity, and I verified it. Running
`eval/calibrate.py --stub broken --family validation`, both `validation-01` and `validation-02`
score `d_t = 1.0`, ten draws out of ten: a deliberately wrong implementation passes their hidden
suite every single time. You also report `path-canonicalization-02`.

The sharp part is what that collides with. `gen_tasks.py --self-check` asserts that every suite
**fails a stub**, and `--verify-lock` records that self-check result, with its timestamp, inside
`tasks.lock`. So the lock carries a guarantee that the calibration stub falsifies on at least three
tasks. Either the self-check's stub is materially weaker than `calibrate`'s broken stub, or the two
disagree about what "fails" means. Right now the frozen artifact makes a promise the code does not
keep, and that is the one class of defect a pre-registration cannot survive being discovered after
the run.

**What to do.**

1. **Reconcile the two stubs first, and report the difference concretely** before changing
   anything. Which mutations does each apply, and why do these three suites survive one and not the
   other. Diagnosis before repair — if the answer is that `calibrate`'s stub is simply a different
   mutation and both claims are locally true, then the fix is to the *wording* of what the lock
   records, not to the suites, and I want to see that argued rather than assumed.
2. **Make the recorded claim as strong as the strongest stub we have.** The self-check should run a
   small fixed battery of mutations rather than one, and record which battery it ran. This is not a
   licence to build AST mutation scoring — that stays deferred. A handful of explicit, named
   mutations is the scope.
3. **Fix the suites that a wrong implementation passes.** Add assertions that discriminate. Say per
   task what the suite failed to test and what you added.
4. **Regenerate `tasks.lock`** and report, per task, which of the three digests moved and why. A
   suite change legitimately moves the tests digest; the prompt and reference digests should not
   move unless you changed them, and if they do, that needs an explanation.
5. **Re-run the leakage audit after the suites change**, full 36 tasks, and report the counts. New
   assertions mean new literals, and the whole value of D5 is that it is exhaustive over the current
   suites, not over the ones it was written against.

Downstream consequence to state in your report so nobody trips over it: any `d_t` measured before
this lands is void, because it was measured against different suites.

**Checks to add.** The self-check fails when handed a suite that its battery cannot break; the
battery it ran is recorded in the lock and a lock recording a different battery fails
`--verify-lock`; each previously-passing wrong implementation now fails its suite; the three
formerly-toothless tasks are named in a check so a regression is visible by name.

---

## Task 2 — Models belong in configuration, not in source

The user's point, and it is correct: this is supposed to be a workspace where any model can serve
any role with just an API key, and adding a model should not be a code edit. Two retirements have
now bitten this project — `gemini-2.0-flash` first, and `llama-3.3-70b-versatile` right now, which
404s for the user's key with `model_not_found`. Groq is the Executor, so all four arms currently
point at a dead model.

The structural gap is that `ROLE_PROVIDER` maps a role to a *provider* and the model rides along
from the `PROVIDERS` dict, so a role cannot have its own model. That is also why the `alternate`
rung of the escalation ladder has nowhere to go without spending Gemini.

This does **not** weaken the pre-registration, and the reasoning matters enough to write into a
comment: D2 freezes *which models a run actually used*. That is a commitment about a run, not about
a source constant. Recording a config resolved at run time is strictly more honest than reading a
constant out of source, because today the frozen commit and the actual run can drift and nothing
notices.

**What to build.**

1. **Role → (provider, model), resolved at run time.** Configuration, with the current values as
   defaults so nothing changes behaviour on day one. A provider is a `base_url` plus a key and
   nothing else, so adding an OpenAI-compatible endpoint must not require touching code.
2. **Write the resolved mapping into every run JSON and into both manifests** — role, provider,
   model, temperature, top_p, and any other sampling parameter actually sent — as resolved, not as
   read from source. If a provider needs a parameter the others do not, it belongs in this record.
3. **A preflight that refuses to start.** Before `run_eval.py` or `eval/calibrate.py` spends
   anything, validate every configured (provider, model) pair with one cheap call each and refuse on
   404 exactly as you now refuse on a `--verify-lock` mismatch. Two calls convert a silent
   multi-hour loss into a one-second failure. **Demonstrate it against the real dead Groq slug** —
   that is a free, genuine negative control and it costs one call.
4. **A model listing helper.** `client.models.list()` against the configured provider, so "what can
   this key actually reach" is a command rather than a hand-written script. The user had to write one
   in `/tmp` to answer that question this week.
5. **Warn when the configuration destroys grader independence.** If the Executor's model equals the
   Test Writer's model, say so out loud — in the manifest and at startup. "Execution grades and the
   grader never wrote the code" is already only partly true, since `planner` and `test_writer` are
   both one model off one spec lineage; a config that also points the Executor there makes it false,
   and that must not be silent.
6. Leave the Streamlit model-picker UI **out of scope**. It touches nothing the eval registers and
   can ship any time.

Two things not to do. Do not pick the replacement Groq model — surface the choice, do not make it.
And do not treat added providers as a way around free-tier scarcity: a second provider is fine,
multiple accounts of one provider is prohibited and stays prohibited.

**Checks to add.** A role can be pointed at a model its provider serves and the call goes there;
the resolved mapping appears in the run JSON and both manifests and matches what was sent; preflight
refuses on a 404 and names the offending role, provider and model; preflight passes on a live pair;
an Executor model equal to the Test Writer model emits the independence warning into the manifest;
defaults reproduce today's behaviour exactly, so no existing check changes meaning.

---

## Task 3 — Settle the ranking heuristic before D2 registers it

Deferred twice, and it is now the last unexamined assumption in the retention logic.
`_candidate_rank`'s third element is `failed_assertion_line`, deeper being better. That is monotone
only if the frozen suite's asserts are order-independent, and nothing has tested it. D2 will register
this ranking key, so an untested heuristic becomes a registered one.

**What to build.** A battery over the frozen suites that permutes assert order and re-runs, and
reports two things: whether a candidate's *outcome* changes under permutation, and — the one that
actually matters — whether the *relative ordering* of two candidates under `_candidate_rank` ever
flips. Include the two cheap neighbours from the same list while the machinery is open:
`PYTHONHASHSEED=0` versus `1`, and a seed re-roll.

**Report the number; do not change the ranking key.** If line depth turns out not to be
order-invariant for some fraction of suites, the response is a registered caveat, a different
ranking element, or a restricted claim — and that is a design decision, so it comes back for a
decision rather than being fixed in place. What I want out of this task is the measured fraction and
the list of suites where it fails, not a unilateral repair of a registered mechanism.

**Checks to add.** A suite with genuinely order-dependent asserts is detected as such; a suite
without them is not falsely flagged; the reported fraction is derived from the frozen suites and not
from a fixture; the battery's result is recorded where D2 can reference it.

---

## Task 4 — Four small corrections

1. **`eval/calibration/` is not in `.gitignore`** though `eval/results/` is, and the same reasoning
   applies verbatim: it is data, an output of the thing the repository freezes, not protocol.
2. **The `os-level:` label prefix is inconsistent** — you found this and flagged it rather than
   touching it. Fix it now, and keep the layer strings' one job intact: reporting what actually held.
3. **`README.md:206` describes a label the code never emits.** Correct the README. Leave
   `SPRINT_BRIEF.md:26` alone — old briefs are a record of what was asked at the time and editing
   them rewrites history.
4. **The pathlib accessor block is 3.9-correct and misfires on 3.10.** My finding, from running your
   suite on a 3.10 interpreter: `harness.py:607` patches `pathlib._NormalAccessor`, and on 3.10 the
   accessor's `open` takes six arguments while `os.open` takes four, so a legal in-workdir
   `Path.write_text()` raises `TypeError: open() takes at most 4 arguments (6 given)` and a *read*
   outside the workdir is misreported as a write denial. It fails closed, so it is not a security
   regression and it is not a live bug on the pinned interpreter — but it fails in precisely the
   direction you identified as dangerous, a false deny charged to the Executor. Make the block
   version-aware: patch the accessor only where that is the right mechanism, skip it otherwise, and
   let the reported path layer say which happened. For the record I also floated and then discarded a
   3.11 fail-open theory — 3.11 dropped the accessor and pathlib calls `os.*` and `io.open` at call
   time, both patched, so removal opens no hole. Do not build for that case.

---

## Report back with

1. `pwd`, `git status --short`, `git diff --stat`. Do not commit.
2. Check counts before and after against **810**, plus the name-level diff, plus any existing check
   whose text or conditions changed and why.
3. Task 1: the stub reconciliation as a diagnosis before the repair; per-task what the suite failed
   to test; which lock digests moved; the post-change leakage audit counts.
4. Task 2: the resolved-config record's shape; the preflight demonstrated failing against the real
   dead Groq slug and passing against a live pair; confirmation that defaults reproduce current
   behaviour.
5. Task 3: the measured fraction and the named suites. No change to the ranking key.
6. Per task, file and line ranges.
7. Anything in this brief you think is wrong, and what you did instead. Two of my last three briefs
   contained a premise you correctly falsified — the 429 retry that already existed, and the wrap
   table that already existed. Check the premises here the same way; that has been worth more than
   the code both times.
8. Anything you touched that this brief did not ask for.

# Pre-registration addendum I to Revision 3

**Written 2026-09-01, after addendum H was committed as `da43953` and before the first calibration
draw is spent.** Separate file rather than an edit to H or to any earlier addendum, for the reason
addendum A gave and B, D, E, F, G and H repeated: a registration document that changes after it has
been read is not a registration. `DECISIONS_R3.md` and addenda A through H are left exactly as they
were written.

## Provenance of this text, and why it is I rather than H

**The substance of D-20 and D-21 below was drafted by a second agent writing into this tree, not by
this project's author.** The draft was found at the `eval/prereg/DECISIONS_R3_ADDENDUM_H.md` path at
16:00:31 on 2026-09-01, where it had replaced the addendum H then being written at that path, between
one read of that file and the next edit to it. It was preserved byte-identically as
`RESCUED_DRAFT_planner_spec_addendum.md` and is registered here under its own letter.

**It was never committed under H, so nothing is being revised in place.** H is `da43953`: D-19 is the
three D-18 reporting corrections, and that commit has been read off this machine. The draft numbered
its two items D-19 and D-20; the commit outranks that numbering, so they are **D-20** and **D-21**
here, and every internal cross-reference moves with them — the caveat sentence D-20 registers
verbatim for the write-up cites the probe as `(D-21)`.

Attribution is recorded rather than absorbed because a reader of a registration has to be able to
tell which decisions this project's author made and which arrived from elsewhere. The draft's
load-bearing claims were re-derived before being carried across, and three things changed on the way:
the lock precondition is rewritten against a guard that already exists rather than described as work
to do, one git-history leg is added, and one halt rule is sharpened. Everything else is the draft's,
including its schedule table and its estimand argument.

Scope: adds **D-20** and **D-21**. Everything in Revision 3 and in addenda A–H stands unchanged,
H's D-19 included. D-20 finishes a decision Revision 2's freeze manifest left half-stated; it changes
no code and no call. D-21 registers one measurement that is *permitted to run after the endpoint
lock*, which is the only reason it needs registering in advance.

---

## D-20. One Planner spec per task, front-loaded and frozen

Revision 2's D2 freeze manifest states the seed policy as:

> seed policy (independent Executor sampling per candidate; spec shared per repeat)

**"Per repeat" is ambiguous and has been read both ways.** It scopes the sharing to *within* a
repeat, which would imply a fresh Planner draw for each of the three repeats. That is not what is
registered here.

**Registered, replacing no earlier text and resolving that phrase: the spec is shared across repeats
and across arms. One Planner call per task, drawn before the grid opens and frozen for its duration.
Executor sampling stays independent per candidate. The pairing is therefore tight at every level —
task, then spec, then suite — with only the Executor draw and the repair loop sampled.**

The estimand does not change. Both policies are unbiased for the same quantity, the repair effect
averaged over the Planner's spec distribution; one samples that distribution across the 36 tasks, the
other within each task. They differ only in variance structure, and the within-task version's whole
advantage is that the spec component divides by 108 rather than 36 — real only if the repair effect
varies strongly across specs. What decides this item is not that advantage but the pacing below,
which makes the alternative barely runnable.

### Why front-loading, in one sentence

Temporal separation: every drift-sensitive Gemini call happens up front and is frozen as an artifact,
so its content cannot drift, and all Groq sampling then runs interleaved and compressed into one
short window.

### The schedule, re-priced against the measured ceiling

The measured Gemini limit is **20 generate requests per day per model**, and the counter includes the
request it refuses — two slugs showed the same pattern, 19 successes then a refusal at attempt 20 —
so the budget is denominated at **19 usable generates per day**, and in Gemini-days rather than calls.

| policy | Planner calls | Gemini-days at 19/day | pacing consequence |
|---|---|---|---|
| one spec per task (registered) | 17–36 | ~1–2 | grid is Groq-only: restartable, splittable, never stalled on the scarce provider |
| one spec per repeat (rejected) | 108 | ~5.7+ | Gemini interleaved through the grid; ~a week of wall clock |

### The 19 imported specs, and the two legs that bind them to the lock

**17, not 36, if the imported plans hold.** 19 of the 36 locked tasks already have a Planner spec on
disk under `eval/calibration/seed-0/plans/`, leaving 17 to draw — inside a single paced day, with two
generates to spare. All 19 task IDs are present in the current `eval/tasks.lock` (36 tasks, 18
families, tiers 10/16/10), so they are name-compatible. **Their binding to the current task *prompt*
is not recorded, by design:** `eval/import_pin_plans.py:29-30` states that of the four digests an
imported plan could carry, "the fourth, the prompt, no ledger records: it rests on the identical call
expression above plus `task.prompt` being a pure function of the locked task." So lock-compatibility
cannot be asserted from the artifacts alone.

**Registered as a precondition, not an assumption, and as an existing command rather than work to do:
before any of the 17 remaining specs is drawn, run `python3 eval/gen_tasks.py --verify-lock` and
record its output. Zero failures is the pass.** The command regenerates the task set from the lock's
own `generator` block and compares every digest, which is leg 1 of `verify_lock`'s own three
(`eval/gen_tasks.py:2060-2075`) and the leg that catches "a reworded prompt". `_compare` (`:2130`)
walks `DIGEST_PARTS` = `("prompt", "tests", "reference")` (`:1946`) and names the task, the part that
moved and both digests. It is read-only and it costs no API request: 36 tasks and 108 digests
regenerated in 28ms on this machine.

Two further legs come free and are recorded with it, because neither is reinventable from a digest
comparison: the lock's recorded self-check battery is compared against the one this code runs
(`:2097-2106`), and a hand-edited lock is refused on `body_sha256` (`:2087-2093`). One leg does *not*
fire from the command line and is registered as covered elsewhere: the run-selection comparison needs
the tasks a run actually selected, so it fires at `eval/run_eval.py:1615` and `eval/calibrate.py:866`,
where `verify_lock_or_reason` runs before a request is spent, and not in the standalone invocation.

**Registered: the imported plans' binding to the locked prompt rests on two legs, and both are
recorded. Leg one is the digest comparison above, run now. Leg two is the absence of any commit to
`eval/gen_tasks.py` between the lock being written and the specs being drawn.** Leg two is what makes
leg one evidence about *the plans* rather than only about today: a generator that moved and moved back
would pass leg one.

Leg two, verified at zero cost and stated precisely, because the natural phrasing of it is wrong.
`eval/gen_tasks.py` was **not** introduced with the lock — it was added by `b78cff5`
(2026-08-30 02:04:35 +0530). What matters is the stronger fact: `71fc347` (authored 2026-08-31
17:23:38 +0530, committed 17:26:58) **modified `eval/gen_tasks.py` and added `eval/tasks.lock` in one
commit**, so the lock and the generator that produced it are the same commit, and **no commit since
has touched either path** — `1d6be48` is HEAD, and the working tree holds no modification to either.
The 19 specs were drawn inside the first pin probe's Planner window, `2026-08-31T12:15:23Z` to
`12:37:20Z` in `eval/results/pin-executor/ledger.jsonl`, which is 18 minutes *after* that commit
landed (11:56:58Z), and were written into the calibration store at 2026-09-01 00:24. So the generator
was byte-identical when the lock was written, when those 19 prompts were sent, and now.

**Registered as the consequence: if a future import happens after `eval/gen_tasks.py` has moved, leg
two is gone and those plans are not importable at all.** No digest comparison recovers it, because the
question is not whether today's prompt matches the lock but whether the prompt the plan answered did.

### A `prompt_sha256` mismatch is a lock-integrity finding, not a per-task inconvenience

**Registered: a mismatch halts. It names the task and both digests, and it must be resolved or
re-registered before any draw.** The redraw is what happens after the lock question is settled, not
instead of settling it.

If today's generator produces a different prompt for one task than the lock records, then every run
from that moment sends a prompt that is not the registered one for that task. Redrawing that task's
spec fixes the spec and leaves the grid running an unregistered task set, which is the larger of the
two problems and the one that a per-task remedy hides. D-16's own void condition already implies the
halt: a run that is not the registered task set is not the run D-16 describes.

**After the lock question is settled, the remedy is the draft's and it is unchanged: that task's
imported spec is discarded and drawn fresh, and the count is reported.** That costs one more Gemini
generate and keeps all 36 tasks — coverage is preserved, and no task leaves the grid on account of a
moved digest. A NO-GO regeneration at a new seed discards all 19 and returns the cost to the full
36 / ~2 days.

One gap is registered rather than closed, because closing it is a code change. `eval/import_pin_plans.py`
is a standalone CLI (`main` at `:311`, `__main__` at `:384`) and **never calls `verify_lock_or_reason`
itself** — its only lock read is `load_lock()["generator"]["seed"]` at `:329`. Its docstring's "the
lock is verified before the ledger is read" (`:31`) is true of `run_eval` and `calibrate`, its usual
callers, and of `eval/pin_executor.py:287` and `eval/rank_battery.py:918`, which route through the
same guard; it is not true of the script run directly. **So the `--verify-lock` step is registered as
explicit and separate, and no guard is added to `import_pin_plans.py` in this addendum** — that is a
code change, this is a reading and pacing rule, and it wants its own brief.

### The per-task Gemini cost is 1–2, not 1

Two roles resolve to Gemini, not one: `planner` and `test_writer` (`agents_core.py:109-110`). Addendum
B's row pricing 36 tasks at 36 calls assumes one call per plan, which held empirically — addendum C
records the Test Writer firing **0 times in 19 plans** (`DECISIONS_R3_ADDENDUM_C.md:66`) — but that is
an observed rate on the imported set, not a bound. **Registered: Gemini calls are counted per role and
reported, and the day count is read against the realised count rather than against the
1-call-per-task estimate.** If the Test Writer fires on a third of the remaining tasks the front-load
still lands inside two days; the point is that the number is read, not assumed.

### Why the unsettled Executor pin is an argument for this, not a reason to defer

This decision is Planner-side and the pin is Executor-side, and there is no dependency edge between
them: a spec is a stored artifact and which model consumes it is downstream and swappable. That is
what `eval/pin_executor.py --replay-plans` exists for — re-measuring the Executor against frozen
specs at zero Planner cost. So frozen specs are what make an unstable pin tolerable. If the Groq slug
dies mid-eval, or the probe re-seats the Executor, the grid re-runs against the same specs for no
Gemini requests. Under the rejected policy, Executor-side churn would invalidate the paired spec
structure and force everything to be re-measured.

### The caveat sentence, registered verbatim for the write-up

> Specs were drawn once per task and frozen; reported run-to-run variance excludes Planner variance.
> Production re-plans every run, so total variance is higher. The off-grid Planner probe (D-21) bounds
> the excluded component.

Production arm B cannot escape a bad spec either: the repair ladder is `("repair", "alternate",
"fresh")` and `fresh` is "start over from the spec on a different model" (`agents_core.py:46`), so no
spec is ever redrawn inside the loop. A catastrophic spec for one task depresses A′ and B equally,
cancels in the pairing, is counted by the suite-validity rate, and is the spec-drift failure mode the
thesis names as its own ceiling — signal, not noise to be averaged away.

### The suite-teeth covariate, bounded

`EVAL_PREREGISTRATION_AMENDMENT.md:306` already bounds this and the bound is restated rather than
loosened: teeth is a function of the suite, and the suite is a component of the A′/B treatment.
**Registered: B−A′ conditional on teeth is reportable and exploratory — teeth is fixed before the
Executor treatment for that contrast, which is the amendment's own language. Unconditional B−A′ stays
primary. Any A-inclusive quantity conditioned on teeth is descriptive only and carries no causal
reading**, because conditioning there means conditioning on part of the treatment. The covariate
recovers the spec-heterogeneity dimension for the primary contrast and nothing for A-side quantities.

### No cache, and no nonce

An earlier review proposed keying the response cache with the deterministic run id as a nonce, on the
grounds that repeats 2 and 3 present an identical prompt under this policy and would be served
repeat 1's answer. **There is no LLM response cache in this project and there will not be one.** The
prohibition is on the record at `agents_core.py:1680-1695` with this exact failure mode named — "36
tasks become 36 unique answers replayed ten times at zero variance. k=3 becomes k=1" — and
`eval/pin_executor.py:33-36` repeats it for the resume ledger: that ledger keys on `(task_id,
candidate)`, a completed-work marker, and "do not add a key that includes the prompt." The durable
rule, stated here so it survives future contributors: **resume at the unit of work; never replay at
the unit of prompt.** If production ever needs 429 insurance the primitive is stage-level resume — the
ledger, generalised — never a cache.

**Registered as a regression guard rather than a prerequisite: once after the lock closes, run one
task's three repeats and hash the three candidates; three distinct hashes is the pass. Rerun it if
any cache-adjacent code changes.** It guards the prohibition. Nothing about D-20 depends on it.

### What would reopen it

A change to the measured Gemini per-day ceiling large enough to make 108 Planner calls fit inside the
grid window; a NO-GO regeneration at a new seed, which re-prices the front-load and must be recorded;
a `prompt_sha256` mismatch on any of the 19 imported plans, which halts under the rule above and is
resolved or re-registered rather than absorbed; or a commit to `eval/gen_tasks.py` before a further
import, which removes leg two and makes the plans concerned unimportable. Each needs its own addendum.

---

## D-21. The off-grid Planner probe, and why it may run after the endpoint lock

**Registered: a separate off-grid session of 18–36 Planner calls on the same 36 tasks at a fresh
seed, with each generated suite executed locally against the task reference. It measures
Planner-side rates only — suite validity, spec stability across draws, and the teeth-rate
distribution. It is permitted to run before or after the endpoint numbers are locked, and its results
are reported separately and never merged into any arm, any cell, or any endpoint.**

It measures a different quantity from the grid, which is the whole reason it is safe after the lock:
nothing in it can move `Δ̂`, because no result of it enters an arm. Registering it in advance is
required anyway — an unregistered measurement taken after the lock is indistinguishable from one
chosen because of how the endpoint came out.

It costs no API requests beyond the Planner calls: suite-against-reference execution runs in the local
harness. This is where the rejected policy's genuinely unique goods are bought — Planner-side rates at
larger n — decoupled from the grid instead of woven through it, at roughly a third of the Gemini cost.

### What would reopen it

Any result of this probe being used to select, weight, exclude or re-grade a grid cell, which would
make it part of the endpoint and void its after-the-lock permission.

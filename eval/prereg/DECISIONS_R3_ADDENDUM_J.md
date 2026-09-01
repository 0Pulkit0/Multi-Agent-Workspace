# Pre-registration addendum J to Revision 3

**Written 2026-09-01, after addendum I was committed as `3a5856c` and before the first calibration
draw is spent.** Separate file rather than an edit to I, for the reason addendum A gave and B, D, E,
F, G, H and I repeated: a registration document that changes after it has been read is not a
registration. `DECISIONS_R3.md` and addenda A through I are left exactly as they were written.

## This addendum moves no rule

**J corrects one paragraph of addendum I — its lines 142–149 — and registers no new decision.** There
is no D-22. Nothing in D-20 or D-21 changes. No earlier item is reopened, no field is added, no code
under `eval/` changes, and no call, cost, count, precondition or halt rule moves. This is the
standalone form of H's errata block and it carries H's warning with it: **no later reader should take
a corrected pointer here for a moved rule.**

What was wrong was an account of the code, not a decision taken on the strength of it. The paragraph
said a guard was missing. The guard is there, one line above the read the paragraph called the only
one, and **the file has never existed without it**: `50b6e6e` added `eval/import_pin_plans.py` and the
`pin_executor.locked_tasks()` call at `:328` in the same commit, no commit since has touched that call,
and `pin_executor.locked_tasks` itself arrived in `71fc347` — the commit that added `eval/tasks.lock`.

## The paragraph, as committed in I

Quoted rather than rewritten, because I stands as committed:

> One gap is registered rather than closed, because closing it is a code change.
> `eval/import_pin_plans.py` is a standalone CLI (`main` at `:311`, `__main__` at `:384`) and **never
> calls `verify_lock_or_reason` itself** — its only lock read is `load_lock()["generator"]["seed"]` at
> `:329`. Its docstring's "the lock is verified before the ledger is read" (`:31`) is true of
> `run_eval` and `calibrate`, its usual callers, and of `eval/pin_executor.py:287` and
> `eval/rank_battery.py:918`, which route through the same guard; it is not true of the script run
> directly. **So the `--verify-lock` step is registered as explicit and separate, and no guard is
> added to `import_pin_plans.py` in this addendum** — that is a code change, this is a reading and
> pacing rule, and it wants its own brief.

Three of its claims are false: the call arrow, the sole-lock-read claim, and the docstring verdict.
Each is corrected below. Every line number in this file was checked by opening the line.

---

## 1. The verification is at `:328`, one line above the read addendum I calls the only one

The true chain through `main` (`eval/import_pin_plans.py:311`), in order:

| line | what happens | refuses by |
|---|---|---|
| `:315-323` | `--out` inside `eval/results/` is refused | `return 2` |
| `:324-327` | an absent `--ledger` is refused, before anything is read | `return 2` |
| **`:328`** | **`tasks = pin_executor.locked_tasks()` — the lock is read and verified here** | `SystemExit` |
| `:329` | `locked_seed = gen_tasks.load_lock()["generator"]["seed"]` — the *second* lock read | — |
| `:330-339` | `--seed` disagreeing with the lock's recorded seed is refused | `return 2` |
| `:340` | `convert(...)`, and at `:237` inside it the ledger is opened | — |

`locked_tasks` (`eval/pin_executor.py:279`) regenerates the whole task set (`:286`), calls
`gen_tasks.verify_lock_or_reason(tasks=tasks, path=path)` (`:287`), and raises `SystemExit` on
`reason` (`:288-289`) and on `failures` (`:290-292`, capped at six), in the message shape its own
docstring says it shares with `eval/rank_battery.py` (`:283-284`).

So `:329` is the second lock read, not the only one, and it takes a single recorded field for the
seed-mismatch refusal below it. The verification is the line above it, and it is twelve lines above
the first byte of the ledger.

**It is also strictly stronger than the precondition D-20 registers.** `verify_lock` runs its
always-on legs for either caller — version, the `body_sha256` hand-edit refusal (`gen_tasks.py:2087-2093`),
`self_check.passed`, the battery comparison (`:2097-2106`), regeneration from the lock's own
`generator` block with a digest comparison per task and part (`:2108-2110`), and the task-count
comparison (`:2111-2115`) — and the run-selection leg at `:2117-2126` only when a caller passes
`tasks`. `_cmd_verify_lock` passes neither `tasks` nor `seed` (`:2276`); `locked_tasks` passes
`tasks=list(gen_tasks.generate())`. So `:328` covers every leg the standalone command covers **plus**
the run-selection leg, comparing the generator's live defaults against the lock's index.

## 2. The docstring at `:31` is true, including of the script run directly

Its closing clause reads: "it rests on the identical call expression above plus `task.prompt` being a
pure function of the locked task, and the lock is verified before the ledger is read" (`:29-31`). That
ordering is `:328` before `:237` above, and it holds in the direct invocation exactly as written.
Addendum I asserts the opposite.

Confirmed by running it rather than by reading it: with `gen_tasks.LOCK_PATH` redirected at a lock
whose `tests_sha256` had been changed on one task and whose `body_sha256` had been recomputed to
match, `main` refused, and `pin_executor.load_ledger` was never called — with a readable ledger
fixture on disk that a read would have succeeded on. The hand-edited variant, whose `body_sha256` was
left stale, refused at the same point. Against the real lock the same call proceeded and did read the
ledger. Task 2 of this sprint turns that probe into a check in `test_pipeline.py`; see below.

## 3. Nothing calls `import_pin_plans`, so "the script run directly" is the only case

Addendum I names `run_eval` and `calibrate` its "usual callers" and says `eval/pin_executor.py:287` and
`eval/rank_battery.py:918` "route through the same guard". The direction is inverted. `pin_executor`
is `import_pin_plans`'s **callee**, at `:328` — nothing about `run_eval` or `calibrate` is involved in
this script's lock handling at all. And `rank_battery.py:918` is inside a different function that
merely shares a name, `rank_battery.locked_tasks` at `:898`, which `import_pin_plans` never calls.

**Recorded: nothing in the tree calls or imports `eval/import_pin_plans.py`.** The only importer is
`test_pipeline.py:7571`, inside the `_import_import_pin_plans()` helper at `:7569`, with two use sites
at `:7614` and `:7722`. No module under `eval/` imports it, and its own docstring says why: "A
converter, run once, and deliberately not a mode of `calibrate.py`" (`:4`).

So the case addendum I sets aside as the unguarded one is not a corner of this script's use. It is the
whole of it — and it is the guarded one.

## 4. Five lock readers, four postures, and one that verifies nothing on purpose

`SPRINT_BRIEF_16.md` counted three postures and the count was corrected to four during that sprint.
Recorded here in full, because the fourth is the row a later audit is most likely to misread.

| lock reader | posture | how it refuses |
|---|---|---|
| `eval/run_eval.py:1615` → `load_lock()` at `:1631` | wrapper first, then read | `return 2` (`:1617-1630`) |
| `eval/calibrate.py:866` → `load_lock()` at `:881` | wrapper first, then read | `return None, None` (`:868-880`) |
| `eval/rank_battery.py:898` — `load_lock` `:913`, `verify_lock` `:918` | read, then verify | `SystemExit` (`:915-916`, `:920-925`) |
| `eval/rank_battery.py:940` `lock_body_sha256` | one field, verifies nothing | it does not refuse |
| `eval/pin_executor.py:279` `locked_tasks` — `:287` | verify only through the wrapper | `SystemExit` (`:289`, `:291-292`) |

`eval/import_pin_plans.py:328` borrows the last row entire, which is why it appears in no grep for
`verify_lock`, `load_lock` or `tasks.lock`.

**Registered as legitimate and unchanged: `rank_battery.lock_body_sha256` (`:940-946`) verifies
nothing, and must not be "fixed".** It answers "which lock is on disk right now" for the staleness
stamp at `:949` onward, and a stamp has to be able to report the digest of a lock that does *not*
verify — that is the case the stamp exists for. Wrapping it in a verification would make it refuse
exactly when it is most needed. It returns `None` on an absent or unreadable lock and never raises.
This is stated so that a later reader auditing lock readers against this table does not record row
four as one more instance of "a file that exists is trusted".

## 5. What does not move, which is the half that matters

**D-20's `--verify-lock` precondition stands entire and unaffected.** A recorded pass that a human
reads before a draw is spent is a different artifact from a runtime refusal inside a script: D-20's
pacing rests on the output being *recorded*, and item 1 above shows the runtime guard is the stronger
of the two checks, so the precondition never depended on being the only one. It buys a dated record,
read by a person, before any Gemini request — not coverage.

Unchanged, explicitly: the 17-of-36 split and the Gemini-day arithmetic; the two legs binding the 19
imported plans to the locked prompt, both the digest comparison and the absence of a commit to
`eval/gen_tasks.py` between the lock being written and the specs being drawn; the `prompt_sha256`
mismatch halt rule and its one-more-generate remedy; the per-role Gemini counting; the caveat sentence
registered verbatim for the write-up; the suite-teeth bound; the no-cache prohibition and its
three-distinct-hashes regression guard; and D-21 entire.

One clause of I is withdrawn and one survives. "**So the `--verify-lock` step is registered as
explicit and separate**" survives, because that is what D-20 registers and it is right. The
justification that followed it — that no guard is added because adding one would be a code change
wanting its own brief — is withdrawn as answering a question that was never open. **No guard is added
to `import_pin_plans.py`, and the reason is now the true one: the guard is already there.** A second
regeneration of 36 tasks and 108 digests, one line after the first, would close no gap and would teach
a future reader that the first one was insufficient.

## 6. Where the error came from, recorded rather than absorbed

Attribution is recorded for the reason addendum I gave for recording its own: a reader of a registration
has to be able to tell which claims came from where. Two contributions, and they are different.

**The false claim originated in `SPRINT_BRIEF_15.md:106-113`, the project author's own document, and
was restated as an entire task in `SPRINT_BRIEF_16.md`.** Nearly the whole of I's paragraph is already
there: that the script "**never calls `verify_lock_or_reason` itself**", that "it only reads
`load_lock()["generator"]["seed"]` at `:329`", that the docstring's ordering is "true of `run_eval` and
`calibrate`, its usual callers, and not of the script run directly", and the instruction not to add the
guard because it "wants its own brief". The author has recorded the error as theirs and named its
mechanism: a grep pass for `verify_lock|load_lock|tasks.lock`, which `locked_tasks()` matches nowhere,
so the absence of the spelling was read as the absence of the guard — in a brief whose subject was the
failure mode "a file that exists is trusted". `SPRINT_BRIEF_16.md` is committed alongside this addendum
unedited, false premise included: it is where the error came from, this file cites it, and a brief
rewritten after the fact would break the same chain of custody the addenda exist to protect. That
brief's Task 2 was withdrawn on evidence rather than performed.

**What addendum I added on its own account was the one pointer that should have stopped it.** The brief
named no third and fourth reader; I's paragraph extends the list with "`eval/pin_executor.py:287` and
`eval/rank_battery.py:918`, which route through the same guard". `eval/pin_executor.py:287` **is** the
verification `import_pin_plans.py:328` reaches, cited in the same sentence as an example of something
else. The correct line was on the page the whole time, pointing the wrong way.

**What found it was neither party's grep.** It was reading the five lock readers one at a time and then
redirecting `gen_tasks.LOCK_PATH` at a drifted lock to watch what the code did — which is also why the
correction arrives with a check attached rather than only a paragraph.

**Recorded as a standing rule of this project: a mechanism is not established by grepping for its call
spelling.** A registration that corrects an error without recording where the error came from teaches
the next reader nothing about how to avoid it.

## 7. An observation, not a defect: the two refusals exit 1 and 2

Measured through the real entry point, not inferred. `locked_tasks` refuses with `raise
SystemExit("REFUSING TO MEASURE: …")` (`eval/pin_executor.py:289`, `:291-292`); a `SystemExit` carrying
a string prints it and exits **1**. `import_pin_plans`'s own refusals `return 2` (`:323`, `:327`,
`:339`, `:355`) and `cli` raises `SystemExit(main(argv))` (`:380-381`), so they exit **2**. A drifted
lock through `cli` exited 1; an absent ledger through the real CLI exited 2 and created nothing at
`--out`.

The tree is not consistent about this and does not need to be: `gen_tasks.py --verify-lock` inverts the
same pair, returning **2** when there is no lock to verify (`:2279`) and **1** when the lock does not
match (`:2291`). **Registered as nothing: both codes are non-zero, which is the only property a caller
of a refusing converter needs, and nothing in the tree reads either one** — the only `returncode`
comparisons anywhere are in `harness.py`, where they grade a candidate's own process. This is a note
for a future caller. If something ever does script on the distinction, the asymmetry is the first
thing to look at, and the fix then belongs at the call site and not in a guard that is working.

## 8. The ordering is now a check rather than a probe run once

D-20 leans on the imported specs answering the locked prompts, and the docstring's ordering claim is
part of what makes that true of the direct invocation. It was prose plus one probe. It is now
`test_the_import_verifies_the_lock_before_it_opens_the_ledger` in `test_pipeline.py`.

**What it asserts.** For each of the two ways a lock can lie — one internally consistent lock whose
`body_sha256` was recomputed and which the generator no longer reproduces, and one edited by hand so
that the body digest is stale — it runs `main` against a readable ledger fixture and asserts that the
call refuses with a non-zero status *and* that `pin_executor.load_ledger` was never called. A third
check runs the same call against the real lock and asserts the opposite: it proceeds, and the ledger is
read. That control is what makes the two refusals attributable to the lock rather than to the fixture,
since an unreadable ledger would refuse at `:327` before the guard.

**What it deliberately does not assert.** Not the message text, which belongs to `pin_executor` and
would make this a check about the wrong module; and not which of the two exit shapes the refusal
arrives in, per item 7.

**Why the ordering and not the refusal.** A refusal that fired *after* the ledger had been opened would
still be a refusal, so a check on the refusal alone would notice nothing if `locked_tasks()` were moved
below `convert()` or replaced with a bare `gen_tasks.generate()`. That is the only way this guard
realistically dies, and the never-read assertion is what catches it. Shown rather than asserted: with
`pin_executor.locked_tasks` replaced in memory by a bare `gen_tasks.generate()`, four of the five checks
fail; with the verification left in place but moved below the ledger read, so that it still refuses,
**exactly the two ordering checks fail and the two refusal checks pass** — which is the regression a
refusal-only check would have missed. **This is a regression guard on an ordering that already holds,
not a new registered rule** — recorded here only so that a later reader who finds the check does not
take it for a decision this addendum made.

---

### What this does not license

1. **No reopening of D-20 or D-21.** Their registered substance is untouched, and item 5 lists what
   stands. The only text withdrawn is one justification clause of I, named there.
2. **No code change under `eval/`.** Specifically no guard added to `import_pin_plans.py`, no change to
   `pin_executor.locked_tasks`, and no verification added to `rank_battery.lock_body_sha256`, whose
   posture item 4 registers as legitimate.
3. **No edit to addendum I or to any earlier addendum or brief.** I stands as `3a5856c` with the wrong
   paragraph in it; this file is how it is read from now on. `SPRINT_BRIEF_16.md` stands as written.
4. **No new decision, no D-22, no new field**, and no change to any count, cost, precondition, halt
   rule, endpoint or gate.
5. **No change to what a sweep selects or measures**, and nothing here holds up the 180 draws.

### What would reopen it

`pin_executor.locked_tasks` losing its `verify_lock_or_reason` call, or `import_pin_plans.py:328`
ceasing to call `locked_tasks`, either of which makes the docstring's ordering false and is what the new
check exists to catch; a caller appearing for `import_pin_plans`, which would falsify "the script run
directly is the only case" and would put the exit-code asymmetry of item 7 in play; a verification being
added to `rank_battery.lock_body_sha256`, which would break the staleness stamp it exists for; or any
pointer in this file moving, which changes a pointer and not a rule.

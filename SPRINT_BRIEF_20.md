# Sprint 20 — resume the dead sweep, and price arm B's hidden Gemini ladder

**Brief 19 exists in the tree and is queued behind this one. That is deliberate** — leave it untracked
until its own sprint. It is not sent yet and nothing in it is time-sensitive; the sweep is, because it has
been dead since 17:28Z and the remaining 117 draws are about eleven hours of wall clock that is not being
spent. Restarting it is a five-minute task that unblocks the rest of the day, so it goes first, and brief
19's replay is better run against a machine that is already grinding than against an idle one.

**Your opening question is the right one, and you answered it correctly.** "Can the self-test fail?" is
the only question that makes a self-test worth anything, and
`test_the_write_guard_proves_its_own_interception` is the right shape: a control, three sabotages
(patches never installed, refuses without recording, records without refusing), and two must-not-denies.
Aiming the sabotaged runs at a fake repo under a temp dir rather than the real roots is the detail that
makes the check safe to keep. Nothing to add.

**The five requests.** Accounting accepted as stated. What makes it harmless is not that it was small
but that all five were refused at startup and none graded anything, and you established that by AST
line spans rather than by reading output. The lesson worth keeping: a broken intermediate edit that
drops `def main():` turns a registration loop into a recursive body, so **a check count that rises
implausibly is a syntax finding before it is a test finding.**

Run `pwd` first and confirm you are in `~/Desktop/Multi agent project`.

## Task 1 — resume the sweep, detached, and confirm it resumed rather than restarted

Your read is confirmed independently: no `calibrate` process, newest draw `aggregation-02__d3.json` at
17:28:39Z, still 63 draws at 18:23Z, no `.partial` and no pid file. It was killed cleanly at a record
boundary — consistent with a closed terminal or a sleeping Mac, not a crash.

**It resumes, and resuming costs zero Gemini.** `draw_path` at `calibrate.py:228`, the skip at
`:1124-1125`, `skip %s (all %d draws done)` at `:1132`, `--force` at `:728` to override, and the comment
at `:359` saying resume is the normal case. The 19 specs are on disk, so the Planner is not called at
all. The only Gemini cost in a restart is the preflight's one real generate, and `--no-preflight` at
`:733` removes it.

**Correcting myself before you act on it: `stub_conflicts` (`:541-560`) is not an argument guard.** I
first read it as one. It compares the recorded `stub` identity of each existing draw against this run's
and refuses on a mixed store — that is stub-versus-live, and it is all it is. **Nothing on disk records
the argument list.** There is no manifest under `eval/calibration/`; the tree is `draws/` (63 files),
`plans/` (19) and `events.jsonl`, and a draw record carries `seed`, `tier`, `family`, `variant`, `draw`
but no argv. So a relaunch with a different `--tiers` / `--families` / `--per-family` / `--limit` will
**not** be refused. It will quietly select a different task set and start drawing new work at full price.

That makes the argument list the whole risk. Recover the exact one from your own shell history or the
original launch, and **verify it by the skip count, which is the only check available**: a correct resume
prints **63 skips** before its first new draw. Fewer skips means the selection differs — **stop there and
report the number**, do not let it draw. Do not pass `--force`.

Then relaunch with the recovered arguments plus `--no-preflight`, detached so it survives the terminal,
and with idle sleep held off, logging to a file outside the two protected roots:

```
caffeinate -i nohup python3 eval/calibrate.py <the recovered arguments> --no-preflight > ~/maw-sweep-2.log 2>&1 &
```

Report, from the log rather than from inference: the argument list you recovered and where you recovered
it from, the number of draws skipped, the first new draw written, and the projection's per-role counts.
**If it refuses, paste the refusal and stop** — a refusal there is the guard doing its job and the command
is wrong, not the store.

Do not edit `eval/calibrate.py`. Zero provider requests other than this run's own draws. Confirm before
you launch that the store still reads 63 draws, 19 plans, newest `aggregation-02__d3.json` at 17:28:39Z —
if any of that has moved, something else is writing and that outranks the relaunch.

## Task 2 — price arm B's Gemini ladder, read-only, and say what it does to the endpoint

Your finding that arm B's repair ladder spends Gemini under the **executor** role is verified. I read it
myself: `alternate_provider("executor", keys)` returns `"gemini"` whenever a Gemini key exists
(`agents_core.py:1756-1767` against `ROLE_PROVIDER:108-112`), `_revision_request` returns it on both the
`fresh` rung (`:2852`) and the `alternate` rung (`:2860`), and `call_role`'s docstring at `:1569-1574`
states outright that `prefer` is honoured under `MEASUREMENT_MODE` because the switch is "part of arm B's
definition". And `calibrate.py:1059-1062` requires both keys, so on any real grid run that branch is
live, not hypothetical.

Answer these from disk. **Zero provider requests.**

1. **Realised rate.** The 8 arm-b records in `eval/results/seed-0/arm-b/` carry `repair_rounds`,
   `escalation`, `providers`, `provider_substituted` and `call_log`. Report, per cell: rounds entered,
   which rungs, and every `call_log` entry whose `used` is gemini. Say plainly that these 8 predate the
   D-13 pin — their `call_log` shows `llama-3.3-70b-versatile` — so they price the *mechanism*, not the
   pinned model.
2. **Worst case at grid scale.** Two of three rungs prefer gemini and `MAX_REVISION_ROUNDS = 3`, so a
   step that exhausts the ladder spends 2. Give the arithmetic for 36 tasks × 3 repeats at 1 step and at
   2 steps per plan, in Gemini calls and in days at 19/day, beside the realised rate from item 1. State
   the escalation rate at which arm B alone exceeds the entire remaining Planner budget.
3. **The endpoint question, and this is the part that matters.** `agents_core.py:87-88` says the Groq
   slug is "PINNED 2026-09-01 as the Executor for all four arms". On rungs 2 and 3 arm B's Executor is
   **gemini**, a different provider and model family from the `openai/gpt-oss-120b` that A, A′ and A′@3
   are pinned to. So `B − A′@3` may be measuring "pipeline" and "sometimes a stronger model from another
   family" together. Search `eval/prereg/` for any registered statement that arm B's Executor may change
   provider — my own pass found the ladder tuple named in G, H and I and **no mention of
   `alternate_provider` or `provider_substituted` anywhere**. Report what you find, including if you find
   that I missed it.

**Write no addendum and change no code.** Both are the next sprint's, written from your table. If the
answer to item 3 is that nothing registers the provider swap, say so and stop — that is the finding.

## Task 3 — record the third retirement, read-only

`openai/gpt-oss-20b` returns 404, which your five refused requests established at no useful cost.
`eval/pin_executor.py:67` still lists it in `CANDIDATES`. Confirm and report: whether any **registered
arm** can reach that slug (my pass says no — it appears only in `CANDIDATES` and in comments and
addenda), and therefore that the damage is confined to the pin probe being unrunnable as configured.
**Do not edit `CANDIDATES`** — that list is what addendum D's comparison was made with, and changing it
is a registration matter. This is the project's third retirement after `gemini-2.0-flash` and
`llama-3.3-70b-versatile`; note in your report that D-13's "three candidates are not statistically
distinguishable at this n" can now never be re-tested, and that this is a fact about the world rather
than a defect in the registration.

## Task 4 — one commit, then the counts

`SPRINT_BRIEF_20.md` alone, staged by explicit path — Tasks 1 to 3 produce no code. Message: the sweep
was resumed after a clean mid-run death, and arm B's ladder Gemini cost was priced without a provider
request. Read the whole `git diff --cached` before committing.

Counts **by name**: `test_pipeline.py` **966 / 0**, `test_harness.py` **295 / 0**, both under
`guard_writes.py`, self-test line reported with each. `eval/results/` still 35 files at `d95d6d39f4…`.

Two things from your report to flag rather than fix, in one line each:

- **`test_workdir_cleanup` is coupled to global temp state.** It went 294/1 on three `harness-*`
  directories left by your own diagnostics. That means any concurrent harness run fails it, and brief
  19's replay runs the harness dozens of times. Say whether the check can be scoped to directories the
  test itself created without weakening what it asserts. **Do not change it in this sprint** — name it.
- **`test_harness.py` writes 19 fixed scratch names as relative paths while cwd is the repo root**
  (402 permitted writes bucketed at `<repo>/`). Nothing is left behind, but two concurrent runs collide
  on identical names in a shared directory. Worth one sentence on whether that is worth a `chdir` into a
  temp dir; again, do not change it here.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest. Keys via
`getpass`/`input()` or the app UI only, never on a command line; the shell is **zsh**, so `read -rsp`
fails and the portable form is `printf 'Key: ' && IFS= read -rs VAR && export VAR && echo`.
`eval/results/` is read-only. Do not regenerate `eval/tasks.lock`. Do not edit `DECISIONS_R3.md` or
addenda A–J; add addenda instead. Do not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the
SIGXCPU/ceiling-SIGKILL→timeout coercion, or `test_signal_death_is_explained`. Do not "fix" the
already-correct mechanisms — the unguarded reads at `harness.py:648-651`, the imported plans' absent
`tests`, and the logged `alternate` rung substitution are all on that list; this sprint prices the last
one rather than changing it. Never cache provider responses keyed on
`(prompt, model, params, provider)`. **Never grep a mechanism by its call spelling.** **No rewrap script
and no line-width pass on anything.** Stage by explicit path; no `git add .`, no branch, no remote, no
push, leave `git config` alone. Grep each staged set for key-shaped strings and **read the hits**. Leave
the two vim swap files alone.

No check silently deleted or weakened; a rename or a split is fine and must be called one.

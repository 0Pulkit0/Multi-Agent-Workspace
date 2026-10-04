# Sprint 12 — a stub draw must not be able to become a real one, then commit and stop

The do-both work is **verified and accepted**. Everything below is independent of your report: I ran the
suites in a fresh copy of the tree, resolved the model table myself, recounted the ledgers, and read the
code paths rather than the summaries. One hazard came out of it that did not exist before this sprint,
and it fires on the *next* thing either of us would naturally do. That is Task 1.

Read the whole brief before starting. Run `pwd` first and confirm you are in
`~/Desktop/Multi agent project`.

## What I verified, so you are not re-deriving it

Staged set is exactly five files at +1015/−44, `eval/prereg/` shows only the new
`DECISIONS_R3_ADDENDUM_E.md`, `eval/tasks.lock` is unmodified, and nothing under `eval/results/` has been
touched today. Suites reproduce your 3.9.6 numbers on my 3.10.12: `test_harness.py` **293 passed, 2
failed** (the two known `match` checks, which cannot pass off 3.9) = 295, and `test_pipeline.py`
**886 passed, 0 failed**.

D-6 is installed and the failover hazard is closed: the resolved executor row carries
`{reasoning_format: hidden, temperature: 0.4, top_p: 1.0}` and **both Gemini roles carry exactly
`temperature` and `top_p`** and nothing else. I ran `split_params` on the executor row directly —
`reasoning_format` lands in `extra_body`, the two sampling values go as SDK keywords. That is the right
side of the line where an unrecognised keyword becomes a `TypeError` wearing a provider outage's clothes.
`max_tokens` is set nowhere; its only occurrence is the allow-list at `agents_core.py:717`.

`print_finish_reasons` is called on both the gate-only path (`:710`) and the main path (`:877`), so it
really is unconditional. **D-15 holds in the code, not just in the addendum**: `calibration_rate` filters
on `outcome == OUTCOME_GRADED` alone, so a `length` draw stays graded and stays in `d_t`'s denominator.
Settling that before the numbers exist rather than after is the right order.

Your correction is exact, and I recounted it from both ledgers rather than accepting it:
`openai/gpt-oss-120b` **38 stop / 0 length**, `openai/gpt-oss-20b` 36 / 2, `qwen/qwen3.8-27b` 38 / 0. So
the guard protects against endpoint behaviour on the family, not against a defect in the pinned model.
Say that in D-14's wording if it does not already.

The `_call_log_slice` widening is **accepted** — arm B makes the most calls and has no top-level
`finish_reason`, so the call log is the only place a truncated repair round can surface. I did not verify
the arm-B internals myself. In your report for this sprint, state in one line where a `length` on an arm-B
repair round appears in the record, and whether anything outside `_call_log_slice` would have shown it.

`test_pipeline.py:1305` is the catch of the sprint. A second hardcoded copy of the `keep` tuple that only
fails under a full-suite run is exactly the kind of thing a later reader "fixes" by importing the real
list — which destroys the check. Keep the duplication and make sure the comment says why.

## Task 1 — a `--stub` run currently writes into the real calibration store

`--out` defaults to `CALIBRATION_DIR` (`eval/calibrate.py:44`), `root = os.path.abspath(args.out)`
(`:676`), and resume is `not os.path.exists(draw_path(root, args.seed, task.task_id, index))` (`:793`)
with nothing re-validating what it finds. The per-draw record built in `one_draw` (`:215`) carries no
stub marker — `"stub": None if stub is None else stub.quality` at `:659` goes into the **run manifest**,
which resume never reads.

So `python3 eval/calibrate.py --stub sampled` with the default `--out` writes stub draw files into
`eval/calibration/seed-0/draws/` beside the 19 imported plans, and the real sweep afterwards **skips
those draws and computes `d_t` from stub answers**. The plans survive, because plan reuse checks
`plan.get("spec")`; it is the draws that get poisoned. Note what makes this urgent rather than
theoretical: the obvious way to eyeball the new `non-stop` / `no finish reason recorded` line is a stub
run, you report having done one, and the sweep command I gave the user defaults `--out` to the real store.

This is the third instance this sprint of the same defect shape — **a file that exists is trusted** —
after the truncated `save_record` and the imported-plan digest. Fix it as a guard, not as a convention:

1. Put the stub identity on **every draw record** — the quality string when stubbed, an explicit `None`
   when live. Do not infer it later from `finish_reason` being absent; that is an accident of the field
   you just added and it will stop being true the moment a live provider omits the field.
2. Make resume **refuse**, not silently accept, when an existing draw file's stub identity differs from
   the current run's. Refuse loudly and exit non-zero naming the offending path; do not overwrite, do not
   skip, do not fall back to `--force`. A live sweep finding stub draws is a wrong-store accident, and the
   only safe response is to stop and let a human look.
3. The precedent for where this belongs is already in `main` at `:681` — the existing check that refuses
   an `--out` pointing into `eval/results/`. Same function, same posture, same style of message.

Do not add a second output directory, do not change `--out`'s default, and do not make `--stub` imply a
path. A silent redirect is a worse failure than a refusal, because the person who typed `--stub` is not
the person who later wonders where the draws went.

`--force` keeps its current meaning of "recompute regardless" and is allowed to overwrite across stub
identities, since it is an explicit instruction. Say so in its help text.

## Task 2 — no prereg addendum for this

Do not write one, and do not edit `eval/prereg/`. Nothing in Task 1 changes the estimator, the draw
count, the band, the gate threshold, or what lands in `d_t`'s denominator; it prevents a run from
computing the registered quantity out of the wrong inputs. `D-14` and `D-15` in
`DECISIONS_R3_ADDENDUM_E.md` are accepted as staged and stay as they are. If while writing the guard you
find yourself wanting to register something, stop and tell me instead — that would mean the guard is
changing behaviour and not just protecting it.

## Task 3 — commit, in four commits, in this order

The staged set goes in **first and alone**, because it is the thing I verified and it should not end up
interleaved with new work.

1. The five staged files as they stand, message describing the D-6 install and the `finish_reason`
   recording. State in the body that `reasoning_format` rides in `extra_body` and is keyed by role, so a
   Gemini failover does not carry it.
2. Task 1's guard: `eval/calibrate.py` plus its checks in `test_pipeline.py`.
3. `HOTFIX_DIGEST_WIDTH.md`, `SPRINT_BRIEF_11.md`, `SPRINT_BRIEF_12.md`, `probe_rpd.py`. Documentation and
   the probe tool. In the message, distinguish a brief's forecast from `eval/PREDICTIONS.md`: a brief
   records what I expected before the work and is not a registered prediction, and nothing in a brief is
   admissible as one.
4. The three `probe_rpd_gemini-*.json` files, as measurement data. Their message should say what they
   measure — flash tier at 20 RPD with the refused request counted, `3.5-flash-lite` above 20,
   `2.0-flash` retired at 404, reset near 12:30 IST — and that they are a single untracked copy of a
   day's quota until committed. That last clause is the reason they go in at all.

Stage by explicit path every time. No `git add .`, no `git add -A`, no branch, no remote, no push, leave
`git config` alone. Grep each staged set for key-shaped strings and **read the hits** rather than
expecting zero; the fixtures at `test_pipeline.py:3794`, `:3897`, `:3936` are legitimate. Leave the two
vim swap files alone.

## Task 4 — validate the print offline, then stop

With Task 1 landed, run the stub validation **with `--out` pointed at a throwaway directory outside the
repo** and paste the `finish_reason` block. Then run it a second time against a live-shaped `--out` in
that same throwaway tree to demonstrate the new refusal, and paste that too. Then print the cost
projection for the 18-task family selection at `--draws 10` and confirm it still reads **0 planner calls,
180 executor calls**.

**Do not run the sweep.** The 180 draws are the user's to launch — the projection line is the last thing
that stands between a 0-Gemini run and a 19-request one, and it gets read by a human before any key is
set.

## Standing rules, unchanged

Python 3.9.6 target, standard library only, dependencies stay `openai` + `streamlit`, no pytest. Keys via
`getpass`/`input()` or the app UI only, never on a command line or in shell history. `eval/results/` is
read-only. Do not regenerate `eval/tasks.lock`. Do not touch `CPU_GRACE_SECONDS`, `_signal_name()`, the
SIGXCPU/ceiling-SIGKILL→timeout coercion, or `test_signal_death_is_explained`. Do not "fix" the eight
already-correct mechanisms. Never cache provider responses keyed on `(prompt, model, params, provider)`.
Subagents for read-only fan-out only — no writes to files you are editing, no design decisions.

Run both suites on 3.9.6 and report counts **by name** before/after. No check silently deleted or
weakened; a rename or a split is fine and must be called one.

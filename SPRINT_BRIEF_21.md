# Sprint 21 — the Gemini counter says 20 and our log says 14, and D-3's branch is settled

**This file supersedes two chat-only briefs** that were relayed between Sprint 20 and this one and
never written to the tree. Their surviving items are folded in below as A–D. Sprints 19 and 20 are
unaffected.

Two things happened on the 2026-09-02 tier-3 sweep that change the plan more than any bug so far.

## 1. Seven Gemini requests were invisible, and the hole they came through is now located

The sweep hit a per-day wall at `2026-09-03T04:22:00Z` on `config-parsing-02`'s plan call:

```
Error code: 429 - Quota exceeded for metric:
generativelanguage.googleapis.com/generate_content_free_tier_requests,
limit: 20, model: gemini-3.6-flash
quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
status: RESOURCE_EXHAUSTED   retryDelay: 52s
quota_daily: True
```

`events.jsonl` holds **13 Gemini events. Google counted 20.** An earlier draft of this brief called
one of the 13 a preflight and put the visible total at 14. That was wrong in the direction that
matters: **zero events in the store are preflight events, so all 13 are planner calls and the
invisible set is seven, not six.** Measured cost is **20 requests for 7 delivered plans, ~2.9 per
plan**, against projections that count 1. Every schedule estimate in this project was computed at 1.

The accounting is broken in series, at three levels:

- `Instrument.calls` (`eval/run_eval.py:233`) increments per wrapper attempt, but `_emit_call_event`
  fires only from `_attempt_provider`, so **every wrapper 429 retry runs inside one event**. The store
  holds 227 wrapper attempts against 152 `call` events — 75 Groq requests absent from the log. With
  `MAX_429_RETRIES = 4` a single draw can legally reach 5 × 3 = 15 HTTP requests inside one event.
- Below that, `max_retries` appears nowhere in project code, so openai 2.48.0's
  `DEFAULT_MAX_RETRIES = 2` gives **3 HTTP tries per request**, and neither `calls` nor the event log
  sees them. This is the layer the provider's counter caught.
- Beside both, **preflight never reaches the instrumented path at all.** `preflight_pair`'s default
  `call` closure builds its *own* `OpenAI(...)` client inline (`agents_core.py:961-966`, `timeout=30`)
  and calls `client.chat.completions.create` directly. It never touches `call_model`, so
  `Instrument`'s rebinding is irrelevant — the window position (`calibrate.py:1122` vs `:1151`) is
  incidental, and no event, no `Instrument.calls` increment, and no manifest `spent` entry exists for
  any preflight. Confirmed: **0 preflight events in a 197-event store.** `list_models` (`:1047`) is a
  third such client.

That last bullet, plus the SDK's retry predicate, closes most of the arithmetic.
`_should_retry` in the installed 2.48.0 (`venv/lib/python3.9/site-packages/openai/_base_client.py:843-845`)
returns True for **every status >= 500**, so each visible 503 event is up to three charged requests.
Ledger: 7 delivered plans = 7; two 503s = up to 6; run 2's preflight = 1; run 1's timed-out preflight
= 1 to 3. That is **16 to 18 of the 20**. The PerDay 429 is **1, not 3** — its Retry-After was 52s and
the plan call had only the ~10s between `graph-traversal-02`'s last draw event (`04:21:50Z`) and the
429 (`04:22:00Z`), so no 52s sleep fits. The residual 2 to 4 is unattributed; the likeliest source is
that the three `APIConnectionError`s did reach Google after all. **Do not treat the ledger as exact —
SDK retries are the dominant term, not the whole term.**

`calls` is the best of the three instruments and the manifest's `spent` uses it, so fix the log rather
than the counter.

## 2. The retry design lost 5xx coverage, and the fix is the same fix

`attempts = retry_attempts()` (`agents_core.py:1741`) resolves to `_RETRY_ATTEMPTS`, default
`MAX_PROVIDER_ATTEMPTS = 5`. `set_measurement_mode` does not touch it. **`eval/calibrate.py:1141` and
`eval/run_eval.py:1790` pin it to 1**, deliberately and for a documented reason (`run_eval.py:1780-1789`
and `agents_core.py:1198-1204`): one retry layer, not two, because 5 × 5 is 25 requests to a provider
that just said slow down.

The gap is what the surviving authority covers. `run_eval.py:245` reads
`if not is_429 or attempt == MAX_429_RETRIES: raise` — **429 only**. The layer that covered 5xx was the
inner one (`RETRY_STATUS_FLOOR = 500`), and that is the layer pinned to 1. So on both the calibration
and grid paths, **429 gets 4 retries with Retry-After honoured and 5xx gets none.** The interactive app
installs neither pin and keeps the 5-attempt loop, so the eval is strictly less robust than the app on
the one path where a lost call costs a measurement. `agents_core.py:1200-1204` says so outright.

It cost real tasks on 2026-09-02: two 503s dropped `expression-eval-02` and `expression-eval-01`, the
latter unplanned from 17:39:19Z until 22:14:40Z.

Also structural: with `attempts == 1`, `backoff_schedule` at `:1742` can only produce an empty schedule,
so **`seconds_backoff` reads 0.0 on every eval run by construction** — dead, not uninformative. Zero of
152 events carry nonzero backoff or pacing, while draws ran for 40 minutes.

## 3. Draw durations are governor sleep, and the remedy is a cap, not a deadline

Re-measured over all 170 draws, not the tier-3 52: `calls=1` is **n=107, max 22.0s, and zero of the
107 exceed 60s**. `calls>=2` is n=63, of which **61 exceed 60s, max 20036.4s** (5h34m — worse than the
8467.2s quoted from the tier-3 slice). `Instrument._call`'s loop is 429-only, so **`calls>=2` on a
graded draw is proof a 429 fired and `RateGovernor.note_429` ran.** The separation is perfect.

That falsifies the reading an earlier draft of this brief called supported — that `httpx.Timeout(60)`
is per socket read, so a trickled response stays open indefinitely. A held-open request needs no 429,
so on that reading some `calls=1` draws should be long. **None of 107 are.** The favoured explanation
is instead `note_429` honouring an **uncapped** `wait = retry_after`: a Groq 429 carrying a multi-hour
header produces exactly this distribution, and nothing in the store contradicts it. The timeout-breach
reading is now the one needing extra evidence.

The remedies differ and this picks between them: **cap the governor's honoured `Retry-After`.** A
per-request deadline is still worth having, but it does not address a 5h34m draw whose HTTP request
may well have completed inside its 60s.

Long hidden reasoning stays ruled out: every long draw is `finish_reason: "stop"`, `raw_len`
1386-4361, graded and passed, same content profile as the 5.6s median. What was wrong was the
attribution of the wait, not the finding that it is a wait. No draw on disk can be decomposed —
`one_draw` wrote only `seconds`, `calls` and `calls_by_provider`, so `seconds_sleeping` and
`seconds_http` do not exist for any of the 170. That is what item B's new fields fix going forward,
and it cannot be fixed retroactively.

Machine contention was investigated and withdrawn: `config-parsing-01__d4` took 4.6s written five
seconds after `d3` took 8467.2s.

## 4. D-3 is settled ceiling-heavy, and its remedy has no implementation

Pooled over every task with 10 graded draws:

```
tasks measured  17
in band          1     delimited-parsing-01   d_t = 0.60   (4 assertion failures)
C (d_t >= 0.9)  16
F (d_t <= 0.1)   0
```

C = 16, F = 0. **The three unmeasured tier-3 tasks cannot flip it** — even all three at floor gives
F = 3 against C = 16 — so the branch is determined now, not after reset. Tier 3 was the hard tier and
returned 6 ceilings of 7.

D-3's ceiling-heavy remedy is *"regenerate with harder parameters, keeping the same 18 families and the
same tier structure."* `generate(seed=0, per_family=2, tiers=None, families=None, limit=None)` is the
complete parameterisation and difficulty is a source literal inside each family builder. **The
registered remedy has no implementation.**

D-1 separately: 1 of 17 in band against a 15-of-36 gate with 19 unmeasured. That needs 79% from a
population that has delivered 5.9%. Not arithmetically dead — 15 ≤ 19 — and not to be called before
the number exists.

## Items

**A. Answered — D-11 passed, no action.** Resolved from the log and the store rather than from a
report. The abort text at `tier3_sweep_2026-09-02.log:16` names the quota, says it is not retried,
says why a backoff cannot help, and says how to resume; the store corroborates that the process
actually stopped — last event `2026-09-03T04:22:00Z`, nothing after it, 170 draws and 26 plans
unchanged. Clean pass. One residual, and it is a logging bug not a D-11 bug: **the log's line order is
inverted at whole-run scale.** Run B's header, projection, `preflight OK` and six `d_t` lines occupy
lines 18-36, *after* run B's own abort at line 16, because stderr went straight through `tee` while
10h46m of stdout sat in a 4-8KB pipe buffer and flushed at process exit. Read literally, the file
implies a third run that never happened. Add `-u` (or `PYTHONUNBUFFERED=1`) to the re-run command.
Lines 1-14 are a separate, earlier invocation that refused to start because its preflight returned
`Request timed out.`

**B. One change, three parts, deferred until the sweep exits.** Widen the outer governor from 429-only
to cover 5xx, restoring the coverage lost when the inner layer was pinned. Set `max_retries=0`
explicitly on the eval clients — confirm openai 2.48.0 accepts it per-client and that `preflight_pair`
and `list_models` are covered too. **There is no separate eval client**: `agents_core.py:863` serves
the interactive app as well, and `:1198-1204` registers that the app deliberately keeps its retry
coverage, so `max_retries=0` must be conditional on measurement mode or it makes the app worse in the
same edit. **`max_retries=0` alone does not fix preflight either**: it builds its own client
(`:961-966`) and never calls `call_model`, so it is invisible at any retry setting — it needs its own
event emission. Moving it inside the `Instrument` window does nothing. Emit one event per HTTP attempt
and add per-attempt latency, and **stamp the new events with a marker field**: today's 197 events mean
one-per-provider-attempt, and without a marker the two regimes become indistinguishable in the same
file. Report the offline check count **and the check names** before and after; nothing silently deleted
or weakened, a rename or split is fine and must be called one. This changes the measured environment
and needs registration — flag it, do not decide it.

**Ordering, decided by measurement:** land B *before* the last three plans. The confound is that a 5xx
which used to end a draw would instead be retried into a graded one, moving `d_t`. That channel has
fired **zero times in 170 draws — every draw in the store is `graded`, none `infra_loss`.** Every 5xx
in this project's history hit the *planner*, and a lost plan delays a task rather than biasing its
`d_t`. Rule of three puts the 95% upper bound near 1.8% per draw, so exposure across the 30 remaining
draws is under one draw, worth at most ~0.1 of a 10-draw rate — which cannot move D-3 (already
determined at C=16/F=0) or D-1 (1 of 17 against a 15-of-36 gate). Against that, B recovers the 4
requests the two 503s wasted, out of a 20/day budget. Register the split, then land it. Emit one event per HTTP attempt and add per-attempt latency, so
`calls` and `events.jsonl` agree and `seconds_backoff` stops reading 0.0 through a 40-minute wait.
Report the offline check count **and the check names** before and after; nothing silently deleted or
weakened, a rename or split is fine and must be called one. This changes the measured environment and
needs registration — flag it, do not decide it.

**C. Read-only survey of `eval/gen_tasks.py`.** Which literals in which family builders control
difficulty, and what one difficulty parameter would have to touch across all 18 families without
changing family identity or tier structure. Do not edit `gen_tasks.py`, do not regenerate
`eval/tasks.lock`, do not write the addendum.

**D. Full reporting pass once the last three tasks land.** Per-task `d_t` with `graded` counts,
`grade_failure` distribution below band, `grade_stderr` excerpts for floor tasks, tier-3-only and
pooled C/F separately via `--gate-only` (no calls). Screen every `import` and `runtime` failure for
`unsupported operand type(s) for |` and `SyntaxError`. Read `delimited-parsing-01`'s four assertion
failures and say what makes it different from the 16 at ceiling — that is the most useful single
artefact for hardening the other 35.

## Constraints in force

Gemini is exhausted until 07:00Z / 12:30 IST; no Gemini call before then, **including a preflight**.
At 2.9 requests per delivered plan, 20/day buys ~7 plans, and three are needed to finish tier 3 — queue
nothing else Gemini-side on the same day until B lands. The post-reset re-run takes **`-u` and
`--no-preflight`**: a preflight is now known to cost 1 request and up to 3 if it hangs, both keys are
proven working, and `calibrate.py:1132` documents `--no-preflight` as the supported way to skip it. Python 3.9 target, stdlib only, deps stay
`openai` + `streamlit`, no pytest. `eval/results/` is read-only. Do not edit committed files in
`eval/prereg/` — add addenda. Never `git add .` or `git add -A`; stage by path, read the whole
`git diff --cached`, and grep the staged set for key-shaped strings and **read the hits** rather than
expecting zero (legitimate fixtures at `test_pipeline.py:1047`, `:3827`, `:3930`, `:3969` and
`agents_core.py:1359`). Leave the two vim swap files alone. Keys never on a command line; the zsh
pattern is `printf 'Groq key: ' && IFS= read -rs GROQ_API_KEY && export GROQ_API_KEY && echo`. Do not
change the Executor prompt. Do not probe `gemini-3.6-flash`'s RPD cap.

## Open decision, needed before reset so nothing is spent twice

Whether to calibrate the remaining 19 tasks at all, given the set is about to be regenerated.
Finishing evaluates D-1 as registered, at 19 Gemini plans. Skipping saves that.

**The 3-draw recon middle option is withdrawn — an earlier draft of this brief priced it wrong.**
Gemini spend is per *task*, not per draw: the program's own projection reads `= 10 task(s) x 1 gemini
planner call + 10 task(s) x 10 groq draws`. Nineteen tasks cost **19 plans whether you draw 3 or 10**.
The recon saves 133 Groq draws, and Groq is not the constrained resource. "About a third of the cost"
was wrong; it is the same fraction of the scarce budget, which is all of it.

It also cannot do the job it was for. At 3 draws `d_t` can only be 0, 1/3, 2/3 or 1, and the band is
(0.1, 0.9). A task at true `d_t = 0.85` — in band — returns 3/3 with probability 0.61 and reads as
ceiling. **The near-band tasks a difficulty dial most needs to find are exactly the ones a 3-draw pass
misclassifies.**

What replaces it costs nothing: item C's own `--self-check --verbose` numbers are a static ceiling
signal (battery-mutant catch counts, assert counts) for all 18 families, with zero API calls. Validate
that signal against the 17 already-measured tasks — does battery-catch separate the 16 ceilings from
`delimited-parsing-01`? With one in-band task the answer may be "cannot tell", which is itself worth
knowing before spending 19 plans on it.

D-1's arithmetic is not in dispute: 15 of 36 needed, 1 of 17 held, so the remaining 19 must return
14/19 = 73.7% against an observed 5.9%. Skip the gate-completing draws.

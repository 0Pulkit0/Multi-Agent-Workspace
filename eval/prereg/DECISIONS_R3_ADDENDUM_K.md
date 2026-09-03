# Pre-registration addendum K to Revision 3

**Written 2026-09-03, after 17 of the 36 locked tasks have been calibrated at 10 draws and before the
remaining three tier-3 tasks are drawn.** Separate file rather than an edit to J, for the reason
addendum A gave and B, C, D, E, F, G, H, I and J repeated: a registration document that changes after
it has been read is not a registration. `DECISIONS_R3.md` and addenda A through J are left exactly as
they were written. J assigned no D-number and said so; **this addendum takes D-22.**

Every line number below was checked by opening the line. Where a number describes the working tree as
it stands uncommitted, that is said.

## What K registers

**D-22: the eval's three retry layers are collapsed into one accounted layer, and the tier-3 remainder
is drawn under that environment while the first 17 tasks were not.** The split is registered rather
than avoided, and section 4 gives the arithmetic for accepting it.

Nothing about the experiment moves. The endpoint stays `B_oracle − A′@3_oracle` (`DECISIONS_R3.md:136`),
`PLAN_ARMS` stays `("a_prime", "a_prime3", "b")`, D-1 stays at 15 of 36 with `d_t` strictly inside
(0.1, 0.9) at 10 draws, `d_t` stays a ratio over *graded* draws only, and D-3's two branches and its
tie rule stay as written at `:68-85`. No task is added, removed or regenerated; `eval/tasks.lock` is
untouched.

## 1. The measured environment, before and after

| layer | before | after |
|---|---|---|
| openai SDK, per request | `DEFAULT_MAX_RETRIES = 2` → 3 HTTP tries, unrecorded | `max_retries=0` under measurement mode only |
| `agents_core` provider loop | pinned to 1 attempt (`calibrate.py:1141`, `run_eval.py:1790`) | unchanged, still pinned |
| `Instrument._call` governor | 429 only; 5xx fatal | 429 and 5xx, each on its own budget |
| honoured `Retry-After` in the governor | uncapped (`run_eval.py:188`, `wait = retry_after`) | see section 6 |
| event granularity | one event per *provider attempt* | one event per *HTTP attempt*, marked |
| per-attempt latency | not recorded on any draw | recorded |
| preflight | no event, no `calls`, no manifest `spent` | counted |

The before column is the environment all 170 existing draws were taken in. The after column is the
environment the remaining 30 draws will be taken in.

## 2. Why there were three layers rather than the two the code believed

`run_eval.py:1780-1793` and `agents_core.py:1198-1204` both register the intended design: one retry
layer, not two, because 5 attempts in the provider loop times 5 in the governor is 25 requests to a
provider that just said slow down. That reasoning is sound and the pin implementing it is kept.

What neither comment accounted for is a third layer below both. `max_retries` appears nowhere in
project code, so openai 2.48.0 supplied its own default. Read verbatim from the installed copy,
`venv/lib/python3.9/site-packages/openai/_base_client.py:843-845`:

```python
        # Retry internal errors.
        if response.status_code >= 500:
            log.debug("Retrying due to status code %i", response.status_code)
            return True
```

So **every 5xx was retried twice below the layer that was told not to retry**, and neither
`Instrument.calls` nor `events.jsonl` recorded it. This is the layer the provider's own counter caught.

A fourth hole sits beside the three rather than under them: `preflight_pair`'s default `call` closure
builds its own `OpenAI(...)` client inline (`agents_core.py:961-966`, `timeout=30`) and calls
`client.chat.completions.create` directly. It never reaches `call_model`, so `Instrument`'s rebinding
at `run_eval.py:215-216` never sees it. **0 preflight events in a 197-event store**, confirmed by
reading the store. The window position (`calibrate.py:1122` against `:1151`) is incidental — moving
preflight inside the instrumented window would not have counted it either.

## 3. The one channel by which this can move `d_t`, and its measured rate

Three of the four changes cannot reach a number. `max_retries=0` removes requests that were never
recorded; the new events and the latency field are additive writes; counting preflight changes a
manifest total and no draw. The confound is the governor's new 5xx budget, and it is exactly one
channel:

**A 5xx that previously ended a draw ungraded would now be retried into a graded one.** A draw that
does not grade is excluded from `d_t`'s denominator by construction. Retrying it into existence adds a
draw whose outcome is drawn from the same distribution as the rest — so the estimator is unbiased under
the added draw and only its variance changes — but the *set of draws entering `d_t`* is not the same set
across the 36 tasks, and that is the thing worth registering.

**Measured rate of that channel over the existing store: zero.** All 170 draws in
`eval/calibration/seed-0/draws/` carry `outcome: "graded"`. There is not one `infra_loss` record. Every
5xx in this project's recorded history landed on a *planner* call, where a lost plan delays a task
rather than perturbing its `d_t`.

With 0 events in 170 trials the rule of three puts the 95% upper bound near **1.8% per draw**.

## 4. Why the split is accepted rather than ordered away

The alternative is to draw the last three tasks under the old environment and land the change after.
That buys uniformity across all 36 tasks and costs the following.

Exposure under the split, taking the bound rather than the point estimate: 3 tasks × 10 draws = **30
remaining draws**, at ≤1.8% is **under one draw**. One draw either way is 0.1 of a ten-draw rate on one
task.

Against that, both gates are already out of reach of a 0.1 perturbation on one task:

- **D-3 is determined.** Pooled over the 17 tasks with 10 graded draws: `C = 16` (`d_t >= 0.9`),
  `F = 0` (`d_t <= 0.1`), one task in band (`delimited-parsing-01`, `d_t = 0.60`). All three unmeasured
  tasks at the floor gives `F = 3` against `C = 16`. The ceiling-heavy branch holds under every
  assignment of the remainder.
- **D-1 is not close.** 1 of 17 in band against a 15-of-36 gate with 19 unmeasured. Moving one task by
  0.1 changes membership only for a task already sitting within 0.1 of a band edge, and no measured task
  does except the one already inside.

The cost of *not* splitting is concrete rather than probabilistic: the old environment charges up to 3
requests per 5xx against a 20-per-day Gemini ceiling with three plans left to draw, and it records
`seconds_backoff: 0.0` through waits measured in hours. Drawing the last three tasks under known-broken
accounting to preserve uniformity with 17 tasks whose accounting is also broken is uniformity of the
wrong quantity.

**So: land the change, then draw. The split is registered here and must be reported in any per-task
table as a property of the last three tasks.**

## 5. What the accounting was wrong by, stated as a range

On 2026-09-02 the sweep delivered **7 plans** and hit `GenerateRequestsPerDayPerProjectPerModel-FreeTier`
at `quotaValue: 20`. `events.jsonl` holds **13 Gemini events**, none of them a preflight. So seven charged
requests left no trace anywhere in the project's own instruments.

Attribution, in the order the terms matter:

| term | requests |
|---|---|
| 7 delivered plans | 7 |
| two 503 events, at up to 3 HTTP tries each | up to 6 |
| run 2's preflight | 1 |
| run 1's preflight, which timed out and was itself SDK-retried | 1 to 3 |
| the `PerDay` 429 | 1 |
| **attributed** | **16 to 18 of 20** |

**This is a range and must not be quoted as exact.** An earlier draft closed it to exactly 20 by
charging the `PerDay` 429 at 3; that is ruled out on the timeline. Its `Retry-After` was 52s, and the
plan call had only the ~10s between `graph-traversal-02`'s last draw event (`04:21:50Z`) and the 429
(`04:22:00Z`) — no 52s sleep fits in it. The residual 2 to 4 is unattributed; the likeliest source is
the three `APIConnectionError`s having reached Google before failing locally, which is unfalsifiable
from this side.

Also recorded, because it bears on `calls` as an instrument rather than on Gemini: the store holds **227
wrapper attempts against 152 `call` events**, so 75 Groq requests are absent from the log for the
separate reason that `_emit_call_event` fires only from `_attempt_provider` while `Instrument.calls`
increments per wrapper attempt. `calls` is the better of the two instruments and the manifest's `spent`
already uses it. The fix registered here is to the log, not to the counter.

## 6. The duration finding, reversed, and a cap that already exists

Re-measured over all 170 draws rather than the tier-3 52:

| `calls` | n | min | median | max | over 60s |
|---|---|---|---|---|---|
| 1 | 107 | 0.9s | 5.6s | 22.0s | **0** |
| 2 | 51 | 7.5s | 295.4s | 20036.4s | 49 |
| 3 | 12 | 304.3s | 536.7s | 1222.4s | 12 |

`Instrument._call`'s retry loop is 429-only, so `calls >= 2` on a graded draw is proof that a 429 fired
and `RateGovernor.note_429` ran. The separation is perfect: **every draw over 60s has `calls >= 2`, and
none of the 107 single-call draws exceeds 22.0s.**

That falsifies the reading an earlier draft treated as supported — that `httpx.Timeout(60)` is a
per-socket-read timeout, so a trickled response can stay open indefinitely. A held-open request needs no
429, so on that reading some `calls=1` draws should be long. None of 107 are. The favoured explanation is
instead an **uncapped honoured header**: `run_eval.py:186-196` reads `wait = retry_after` with no bound,
so a Groq 429 carrying a multi-hour `Retry-After` produces exactly this distribution.

**The bound it should have consulted already exists and is already registered.** `agents_core.py:692`
defines `MAX_RETRY_AFTER_SECONDS = 120.0` and applies it at `:718` via
`return min(seconds, MAX_RETRY_AFTER_SECONDS)`. `retry_environment()` publishes it into every manifest
at `:1385`, under a docstring at `:1375` reading *"This is those constants, as installed, at run
time."* On the eval path that sentence is false: the layer holding the constant is pinned to one
attempt, and the layer actually sleeping is the governor, which consults none of it. **A manifest
recording a 120s ceiling was written for a run that slept 5h34m.** That is the same defect class as
`seconds_backoff` reading 0.0 — an instrument reporting a bound that was structurally not in force.

Two consequences are registered here:

- **`note_429` is to honour the same cap**, by consulting `agents_core.MAX_RETRY_AFTER_SECONDS` rather
  than a second constant. This is a remedy to a wait, not to a measurement, and it changes no draw
  already on disk.
- **The new 5xx path must not reintroduce the hole.** As built, `note_server_error` at `:198-208` uses
  `FALLBACK_5XX_BACKOFF` and consults no provider header at all; its own docstring gives the reason.
  That is the correct shape and is registered as such.

A per-request deadline remains worth having and is **not** registered here. It does not address a
5h34m draw whose HTTP request may well have completed inside its 60s.

## 7. What is not retroactive

`one_draw` wrote `seconds`, `calls` and `calls_by_provider` and nothing else. So for all 170 existing
draws there is no `seconds_sleeping`, no `seconds_http`, no per-attempt latency and no call log.
**`config-parsing-01__d3` — 8467.2s at `calls: 2` — cannot be distinguished by any field on disk
between one request that stayed open for hours and one governor sleep between two fast requests.**
Section 6 argues the second from the population, not from that record. No field added by this change can
be backfilled onto those files, and none is to be: the 100 pre-existing draw files that lack
`model_requested`/`model_returned` stay as they are, and the marker field in section 1 exists precisely
so the two event regimes stay distinguishable inside one `events.jsonl`.

## 8. Offline check evidence

The standing rule is that check counts **and check names** are reported before and after, and that
nothing is silently deleted or weakened — a rename or a split is permitted and must be declared as one.

As reported by the implementer against the pre-edit and post-edit trees: the harness battery is
**306 checks / 40 names, byte-identical before and after**. The pipeline battery goes **985 → 1023
checks and 130 → 134 names**, a delta of +39 and −1, with the single removal a **declared rename in
place** rather than a deletion.

**These four numbers are a report and not yet an independent verification.** They were produced on the
macOS host that owns the venv; they are to be re-run and the name sets diffed before this addendum and
the four modified files are committed together. What has been verified independently is the diffstat:
`agents_core.py`, `eval/calibrate.py`, `eval/run_eval.py` and `test_pipeline.py` modified,
**938 insertions and 32 deletions, uncommitted at the time of writing.**

### What this does not license

- It does not license a change to the endpoint, to `PLAN_ARMS`, to D-1's 15-of-36 threshold, to the
  (0.1, 0.9) band, to `CALIBRATION_DRAWS = 10`, or to `d_t` as a ratio over graded draws.
- It does not license regenerating `eval/tasks.lock` or touching `eval/gen_tasks.py`. D-3's
  ceiling-heavy remedy has no implementation; that is a separate addendum, and it takes **L**, not K.
- It does not license retrying a `PerDay` 429. D-11 stands: `DailyQuotaExhausted` is deliberately not a
  `ProviderError`, the abort is loud, and the manifest is deliberately not written over a truncated
  grid. The new 5xx budget is separate from `MAX_429_RETRIES` and must not be routed through it.
- It does not license a per-request deadline, a change to `EXEC_TIMEOUT_SECONDS`, or a change to the
  Executor prompt.
- It does not license `max_retries=0` outside measurement mode. `agents_core.py:863` is the interactive
  app's client as well, and `:1198-1204` registers that the app keeps its retry coverage on purpose.
- It does not license treating section 5's 16-to-18 as exact, or re-deriving it to 20.
- It does not license backfilling any field onto the 170 existing draws.

### What would reopen it

- A graded-draw count that stops being the whole population: **any** `infra_loss` record appearing in
  the remaining 30 draws makes section 3's 0-of-170 stale and the exposure arithmetic in section 4 must
  be recomputed before the grid.
- A `calls = 1` draw exceeding 60s. That single observation revives the held-open-request reading that
  section 6 discards, and the remedy question reopens with it.
- The re-run of section 8's check counts disagreeing with the reported numbers, or the removed check
  turning out to be anything other than a rename in place.
- A 5xx landing on an *executor* call rather than a planner call. Every recorded one has hit the
  planner, which is why the channel in section 3 has never fired; the first executor-side 5xx is the
  first time the channel is live.

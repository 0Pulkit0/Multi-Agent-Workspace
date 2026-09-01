# Pre-registration addendum F to Revision 3

**Written 2026-09-01, after Sprint 12's four commits landed and before the first calibration draw is
spent.**
Separate file rather than an edit to D-1 or to any earlier addendum, for the reason addendum A gave
and B, D and E repeated: a registration document that changes after it has been read is not a
registration. `DECISIONS_R3.md`, addendum D and addendum E are left exactly as they were written.

Scope: adds **D-16**. Everything in Revision 3 and in addenda A, B, C, D and E stands unchanged.
This item changes no code and nothing about what any call sends. It is a reading rule for a partial
measurement, fixed before the number exists.

---

## D-16. How to read `k`, the in-band count over the 18 tasks that have imported plans

**Registered: `k` is the number of the 18 tasks named below whose `d_t` falls strictly inside
(`BAND_LOW`, `BAND_HIGH`) at `CALIBRATION_DRAWS = 10`, computed by exactly the code path D-1 uses --
`calibration_rate` over the graded denominator, per D-15. No extra filter and no re-definition.**

`k` is a partial result that will be reported against a gate it cannot settle. What this item fixes,
before the sweep runs, is what each value of `k` licenses — so that the number is read against a
rule written in advance rather than against whatever the number turns out to be.

### What is about to be measured, and why exactly these 18

The nine families whose **both** variants have an imported Planner spec under
`eval/calibration/seed-0/plans/`: `aggregation`, `byte_formatting`, `date_arithmetic`, `grouping`,
`interval_logic`, `path_canonicalization`, `ranking`, `run_length_encoding`, `text_normalization`.
At `--per-family 2` that is 18 tasks, 180 Executor draws at 10 draws each, and **0 Planner calls**,
because all 18 already have their plan on disk.

`query-canonicalization-01` is the 19th imported task and is **excluded**. The reason is cost, not
selection: its family sibling `query-canonicalization-02` has no imported plan, so taking the family
at `--per-family 2` would spend a real Gemini request, and taking the one variant alone would turn a
family selection into a hand-picked list of task IDs. Stated here so that a later reader does not
read the exclusion as cherry-picking. Its own replay result is 3/3 — at the ceiling, alongside the
tier-1 group — so its exclusion removes a task that was heading out of band, not one that looked
in-band.

Tier mix, counted off `eval/tasks.lock` rather than off family names:

| | tier 1 | tier 2 | tier 3 | total |
|---|---|---|---|---|
| **measured (these 18)** | **10** | **8** | **0** | **18** |
| unmeasured | 0 | 8 | 10 | 18 |
| locked total | 10 | 16 | 10 | 36 |

All ten tier-1 tasks, half of tier-2, none of tier-3.

### `k` is not the D-1 gate, and a low `k` is the expected result

D-1 (`DECISIONS_R3.md:28`) is `GATE_MIN_IN_BAND = 15` in-band tasks **of 36**. `k` is drawn from a
strict subset of those 36, and a biased one: it is the entire easy end of the grid plus half the
middle, with the hard end absent.

The bias has a measured direction. In the pin replay (`eval/results/pin-replay-1/ledger.jsonl`,
three Executors × 19 tasks × 3 draws) the tier-1 tasks passed **29 of 30** draws, and **9 of the
10** tier-1 tasks were 3/3 for every candidate. Those are tasks heading for `d_t = 1.0` — out of
band **high**, at the ceiling, which D-1's open interval excludes by design because a task that
always passes cannot show a difference between arms. So the measured half is the half most likely to
return few in-band tasks, and **a low `k` is the expected result on this half and is not evidence
against D-1.**

Two qualifications, so the direction is not overstated:

1. **Three draws is not the D-1 estimator.** The replay is 3 draws per task per candidate across
   three different Executors; `d_t` is 10 draws from the one pinned Executor. The replay is a
   forecast of where these tasks sit, not a measurement of `d_t`, and D-16 registers no expected
   value of `k`.
2. **The floor is also represented.** Within these 18, `path-canonicalization-01` was 0/3 and
   `path-canonicalization-02` was 1/3 in the replay, so out-of-band pressure on this half is not
   purely ceiling. A `k` that comes in low will need its floor and ceiling counts read separately,
   which `print_rates` and the `by_tier` block already break out.

### The arithmetic, registered before the number exists

The unmeasured 18 are 8 tier-2 plus 10 tier-3, and they must supply `15 − k`:

| k | needed from the unmeasured 18 |
|---|---|
| 0 | 15 of 18 (83%) |
| 3 | 12 of 18 (67%) |
| 6 | 9 of 18 (50%) |
| 9 | 6 of 18 (33%) |

### The three readings

- **`k` ≤ 2** — D-1 fails on the measured half's own terms: even a generous rate on the unmeasured
  18 leaves the threshold out of reach. Go to **D-3** before spending further Gemini quota on
  completing the pin.
- **`k` = 3–6** — undetermined, and the gate then hinges entirely on tier 3, which **has never been
  measured at all**: 0 of 10 tier-3 tasks appear in the pin, in the replay, or in these 18. The next
  Gemini requests are therefore spent on the **ten tier-3 tasks first**, not spread across the
  unfinished families. This is the operative consequence of D-16, and it deliberately reverses the
  natural instinct to finish what was started: the unfinished tier-2 families are the part of the
  grid there is already evidence about, and tier 3 is 28% of the locked set with an entirely unknown
  `d_t`.
- **`k` ≥ 7** — completing the pin to 36 is worth the remaining requests.

### What a Gemini request costs, so the readings above are actionable

**"0 Planner calls" is not "0 Gemini requests".** The projection counts role calls; the preflight is
separate and spends **one Gemini request** per distinct `(provider, model)` pair, and `calibrate.py`
preflights `planner` and `executor`, which resolve to two pairs. So the 180-draw sweep costs 1
Gemini request, not 0, unless `--no-preflight` is passed — which it should not be, since a dead pair
then surfaces as a failed draw mid-sweep instead of a refusal. Registered here because the sprint
framing "zero Gemini" is true of the Planner and false of the run.

Against the measured ceiling of **20 requests per day per model, with a refused request counted**
(probe of 2026-08-31: 19 completions, the 20th refused with `quotaValue 20`):

| next step | Planner calls | + preflight | total Gemini |
|---|---|---|---|
| these 18 tasks (about to run) | 0 | 1 | **1** |
| the ten tier-3 tasks | 10 | 1 | **11** |
| completing the pin to all 36 | 18 | 1 | **19** |

The `k` = 3–6 reading therefore fits inside one day's allowance with room to spare. The `k` ≥ 7
reading does **not** fit alongside anything else: 19 of 20 in a single day, with no margin for a
retry, and any request already spent that day pushes it over. That is recorded as arithmetic, not as
a decision — how to split it across days is not registered here.

### Residual: `k` is computed with no minimum-denominator filter

At 10 draws, in-band means 1 to 9 passes. D-15 keeps a truncated draw in the denominator;
`calibration_rate` excludes an infra loss from it. So a task with `graded < 10` has a **wider
effective band** than one with `graded = 10` — at `graded = 4`, one pass of four is `d_t = 0.25` and
in-band, where one pass of ten is `0.1` and excluded. D-1 is silent on this.

**Registered: `k` is computed with no minimum-denominator filter**, and any task with `graded < 10`
is reported beside its graded count so a reader can see which tasks got the wider band.
`print_rates` already prints `passed/graded` per task, and `calibration_rate` already returns
`graded` alongside `d_t`, so this is a statement about how to read the existing output and not a new
requirement on it.

No minimum is invented here. Setting one would change what `k` counts after the shape of the losses
is known, which is D-10 point 3's principle: an instrument is not adjusted after its output has been
read. If a run comes back with enough short denominators for the filter to matter, that is its own
registered decision, taken with the counts in hand and stated as a departure.

### Void condition

**If the run's own projection does not read 18 tasks, `planner gemini 0`, `executor groq 180`, it is
not the run D-16 describes and D-16 does not apply to it.** The projection prints before anything is
spent, so this is checkable at the top of the run rather than argued afterwards.

### What this does not license

1. **No re-definition of `d_t` or of the band.** `k` uses `calibration_rate` and `in_band` as
   shipped.
2. **No claim about D-1 in either direction.** `k` neither clears nor fails a 36-task gate.
3. **No adjustment to `GATE_MIN_IN_BAND`.** The threshold is 15 of 36 and this item does not prorate
   it to the measured half. A prorated threshold would be a new gate chosen after the sample was
   chosen.
4. **No re-run of these 18 to change `k`.** The draws are stored; a second opinion is a second
   registered decision.

### What would reopen it

- A `k` outside 0–18, which would mean the selection was not the one described above.
- Tier-3 tasks acquiring imported plans by some route that costs no Gemini request, which would
  remove the quota argument the `k` = 3–6 reading rests on.
- A provider change on the Planner, which changes the per-day ceiling the arithmetic above is
  measured against.

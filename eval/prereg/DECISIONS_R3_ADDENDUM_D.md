# Pre-registration addendum D to Revision 3

**Written 2026-09-01, after the D-9 pin re-run completed and before any calibration draw is
spent.** Separate file rather than an edit to addendum A, B or C, for the reason A gave: a
registration document that changes after it has been read is not a registration.

Scope: adds D-13, which closes D-6 by naming the pin. Everything in Revision 3 and in addenda A,
B and C stands unchanged. D-6's sentence "The Groq Executor is **not yet pinned**" is superseded
by this item and by nothing else.

---

## D-13. Executor pinned: `openai/gpt-oss-120b`

**Registered: `PROVIDERS["groq"]["model"] = "openai/gpt-oss-120b"`, set in the source defaults and
committed before the calibration sweep runs, per D-6.**

**Registered with equal weight: this pin is not statistically supported by the data it was made
from.** The four caveats below are part of the decision, not commentary on it. Anyone reading a
grid result downstream is reading a result conditional on an Executor chosen at `p = 0.50`.

### The run it was decided from

D-9's re-run, executed 2026-08-31 18:02:34Z–18:05:07Z, ledger at
`eval/results/pin-replay-1/ledger.jsonl`. It followed D-9's procedure exactly:

- the **19 spec/test pairs from the poisoned first probe were reused verbatim** — 19 `plan`
  records, **0 Gemini requests**, so the re-run cost nothing against the 20/day quota D-11
  registered as the binding constraint;
- **57 Executor draws** regenerated under D-7 Part B, 19 per candidate, one draw per task;
- **0 non-`ok` units and 0 retries** across all 57. No infrastructure loss to account for;
- `reasoning_format: "hidden"`, `temperature: 0.4`, `top_p: 1.0` on every call and nothing else —
  in particular **no `max_tokens` and no `max_completion_tokens` were ever sent**, which matters
  for caveat 4.

D-7 Part B did what it was registered to do. Both union-bearing specs (`aggregation-01`,
`grouping-01` — `U` = 2, unchanged, since the specs are the same bytes) now pass for **all three**
candidates, and `interval-logic-01`, the unexplained 120b import failure recorded in addendum B,
passes. The artifact that made the first probe uninterpretable is gone from this one.

### The point estimate

Hidden-suite pass counts over the 19 reused specs, one draw each:

| candidate | hidden-suite pass | gate-approved | failures |
|---|---|---|---|
| **`openai/gpt-oss-120b`** | **18 / 19** | 17 | 1 assertion |
| `openai/gpt-oss-20b` | 16 / 19 | 14 | 1 import, 2 produced no code |
| `qwen/qwen3.8-27b` | 16 / 19 | 15 | 3 assertion |

The point estimate for the pinned model is **18/19 = 0.947** on this project's own task
distribution, which is the quantity D-6 registered as the basis for pinning and the reason D-6
refused to pin from leaderboards.

`path-canonicalization-01` failed for **all three** candidates and is 120b's only failure. It is a
hard task in this set, not a discriminator.

### Caveat 1 — at n = 19 the three candidates are not distinguishable, p = 0.50

The comparison is paired: all three answered the same 19 specs, so the correct test is exact
McNemar over the discordant tasks, not a two-proportion test that throws the pairing away.

| comparison | discordant | exact McNemar (two-sided) |
|---|---|---|
| 120b vs 20b | 2–0 | **p = 0.50** |
| 120b vs qwen | 2–0 | **p = 0.50** |
| 20b vs qwen | 1–1 | p = 1.00 |

`p = 0.50` is the smallest two-sided p-value a 2–0 split can produce; it is what a coin gives you
twice in a row. **No pairwise difference among the three candidates is statistically significant,
and the ordering 120b > {20b, qwen} is not established by this run.** A 2-task lead over 19 paired
tasks is the weakest possible evidence that is nonetheless evidence.

Registered consequence: **`d_t` and every grid endpoint are conditional on this Executor, and the
choice of Executor is not itself a measured result.** No downstream document may describe
`openai/gpt-oss-120b` as "the best of the three candidates". The defensible claim is "the
candidate with the highest observed pass count in a run that could not distinguish them".

### Caveat 2 — tier 3 was never measured

The probe reached 19 of the 36 locked tasks, in task order, before the Gemini quota wall recorded
in D-11. Tier composition:

| | tier 1 | tier 2 | tier 3 |
|---|---|---|---|
| locked task set | 10 | 16 | 10 |
| measured by the pin | **10** | **9** | **0** |

**Zero of the ten tier-3 tasks were measured**, for any candidate. Nine whole families are
unmeasured: `config_parsing`, `delimited_parsing`, `expression_eval`, `graph_traversal`,
`query_canonicalization` (one of two), `template_expansion`, `tiered_pricing`, `validation`,
`version_ordering`.

Registered consequence: the pin is selected on **easy and mid-difficulty tasks only** and
extrapolated to a grid that is 28% tier 3. Nothing in this run bears on which candidate is better
at the hardest third of the task set, and a candidate ordering that reverses on tier 3 is fully
consistent with the data. This interacts with D-3: if the grid turns out floor-heavy on tier 3,
"the Executor was pinned without seeing tier 3" is a registered validity threat that must appear
in the report and not be discovered afterwards.

It also touches D-12: `tiered-pricing-01` and `tiered-pricing-02`, two of the four tasks in the
float-rounding exposure set, are among the unmeasured 17.

### Caveat 3 — the pinned model costs ~3.5× qwen's completion tokens

`reasoning_tokens` is a **subset** of `completion_tokens` in Groq's usage block, not an addition to
it — verified on the two truncated draws below, which report 2048 completion tokens of which 2046
reasoning and 0 characters returned. So the completion-token column already includes reasoning and
is the number D-8's ladder step 1 asks for.

| candidate | completion tokens, 19 draws | median per draw | of which reasoning | per gate-approved task |
|---|---|---|---|---|
| `openai/gpt-oss-120b` | 10,761 | 543 | 4,831 (44.9%) | 633.0 |
| `openai/gpt-oss-20b` | 15,231 | 648 | 9,955 (65.4%) | 1,087.9 |
| `qwen/qwen3.8-27b` | **3,090** | **166** | 0 (0.0%) | **206.0** |

**`openai/gpt-oss-120b` / `qwen/qwen3.8-27b` = 3.48× on total completion tokens**, 3.27× on the
median draw, 3.07× per gate-approved task. Nearly half of what the pinned model spends is hidden
reasoning that never reaches the code — D-6 already registered that `hidden` suppresses reporting
and not computation, and this is what that costs.

Registered consequence: on a rate-limited free tier, **cost per success is the currency D-8 step 1
named, and the pin loses on it by a factor of about three.** This is registered as a decision made
against that criterion, not as an oversight. It also bounds the schedule: any later claim that the
grid is affordable must be computed at 120b's token rate, not qwen's. Median latency runs the same
way — 1.18 s per call against qwen's 0.36 s — and is registered here for the same reason.

### Caveat 4 — the margin over the runner-up is two truncations, not two solved problems

Both of `openai/gpt-oss-20b`'s losses to 120b are `path-canonicalization-01` and
`path-canonicalization-02`, and on both it returned `finish_reason: "length"` at exactly **2048
completion tokens, 2046 of them hidden reasoning, 0 characters of output, 0 code fences.** It did
not answer incorrectly; it ran out of output budget while thinking and returned nothing.

The project **sent no token ceiling** — every call carried only `reasoning_format`, `temperature`
and `top_p` — so 2048 is the endpoint's own default for that model, and **whether raising it would
erase 120b's entire lead is unmeasured.** Registered as unmeasured rather than argued either way.

This is structurally the same hazard as the PEP 604 artifact that voided the first probe under D-8
case 3: a configuration default, not a capability difference, falling asymmetrically between the
top two. It is registered rather than repaired because repairing it means a third probe run, and
D-9 point 6 set one re-run as the budget. **The honest reading of the top-two comparison is
therefore: undecided.** The pin between 120b and 20b rests on the observed count and on caveat 3
pointing the same way (120b is the cheaper of the two gpt-oss models per approved task by 1.7×),
not on a demonstrated difference in ability.

### Where this sits against D-8's ladder, stated because it is a departure

D-8's classification does not cover this run, and the gap is stated rather than papered over.

- Margin is 18 − 16 = **2**, and `U` = 2, so `margin ≤ U`: **not decisive** under case 1.
- Case 2 requires the margin to be non-decisive **and** every union-bearing task to have failed for
  both of the top two. Under Part B both union tasks **passed** for all three, so case 2's second
  condition is false.
- Case 3 requires a union-bearing task to have fallen one way for one of the top two and the other
  way for the other. Also false, for the same reason.
- D-9 point 6 routes a re-run that is "again non-decisive **and** asymmetric" to the ladder. This
  re-run is non-decisive but **not** asymmetric.

So the re-run lands between the registered branches: the artifact D-8 was written around is gone,
which is the outcome D-7 Part B was for, and D-8 has no branch for "non-decisive margin, artifact
absent". Two readings are available and they disagree:

1. **Read case 2 by its stated rationale** — "those tasks cannot have changed the ordering, the
   remaining comparison is genuine" — which is satisfied more strongly by union tasks that passed
   than by union tasks that failed for both. Then the ladder applies, and **ladder step 1 (fewest
   completion tokens per approved task, including `reasoning_tokens`) selects `qwen/qwen3.8-27b`**
   at 206.0 against 120b's 633.0.
2. **Read the ladder as the tie-break it was written to be.** D-8's own words are "pass count over
   the 36 tasks decides the pin only when the margin is decisive"; addendum A states D-8 exists
   "because the top two were **tied**". There is no tie here: one candidate leads outright, and the
   `U`-based decisiveness threshold was calibrated for a run in which union tasks were destroying
   pass counts, which this run's union tasks do not do.

**Registered: reading 2 is taken, and the pin goes to the highest observed pass count.** The
reasoning is that `U` is a correction term for a contaminant that Part B removed, so continuing to
require a margin greater than `U` after the contaminant is gone applies the same correction twice.

**Registered as a departure, without minimising it: under reading 1 the pin would be
`qwen/qwen3.8-27b`, not `openai/gpt-oss-120b`.** A reader who prefers reading 1 should treat every
grid endpoint as conditional on a pin the ladder would have decided differently, and caveat 3 is
the exact size of what reading 2 gives up. This ambiguity was not resolvable from D-8's text, which
is why it is resolved here, in writing, before the sweep — and not silently at the moment the
constant was edited.

### What the pin does not license

1. **No re-run of the pin.** One re-run was the budget (D-9 point 6) and it has been spent. A third
   probe is not authorised by this item.
2. **No override at run time.** The pin lives in the source defaults. Pointing `executor` somewhere
   else through `MAW_MODELS` or `models.json` is a departure from this registration and must be
   reported as one. It is detectable rather than a matter of trust: every run record and both
   manifests already carry `models` (`agents_core.resolved_roles()`) and `models_source`
   (`agents_core.model_config_source()`) — `eval/run_eval.py:858`, `eval/calibrate.py:541` — so a
   run made against an overridden Executor says so in its own JSON, and Revision 2's **D2 freeze
   manifest** is what fixes the models a run actually used.
3. **No re-grading of the first probe.** Its ledger stores `code_sha256` and not the code (addendum
   B, point 3). The 57 old draws remain uninterpretable and are not merged with the re-run.
4. **No token ceiling is added** to rescue the runner-up, and none is added for the pinned model
   either. Adding one now would change the measured environment after seeing which candidate it
   would help — the same principle D-10 point 3 applies to suites, which is that an instrument is
   not adjusted after its output has been read.
5. **`d_t` is not transferable.** Unchanged from D-6: a calibration measured on this Executor does
   not describe a grid run on another.

### What would reopen it

Registered in advance so that reopening is a rule rather than a judgement call:

- the pinned pair failing the preflight (`eval/models.py --preflight`), which is a dead slug and not
  a re-litigation of the choice;
- the calibration sweep coming back with the gate un-cleared under D-1 **and** the failure
  concentrating in tier 3, where the pin is unmeasured. That combination is the one scenario in
  which caveat 2 is load-bearing rather than a stated limitation, and it would require its own
  registered decision — not a quiet substitution.

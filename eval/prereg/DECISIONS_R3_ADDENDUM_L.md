# Pre-registration addendum L to Revision 3

**Written 2026-09-03, after the decoy probe returned `k = 0` at `n = 30` and after 26 of the 36 locked
tasks have been calibrated at 10 draws. No further draw of any kind has been taken since.** Separate
file rather than an edit to K, for the reason addendum A gave and B through K repeated: a registration
document that changes after it has been read is not a registration. `DECISIONS_R3.md` and addenda A
through K are left exactly as they were written. K took D-22; **this addendum takes D-23.**

Every line number below was checked by opening the line. Every count below was taken from the draw
files or the lock, not from a run summary, and section 11 lists what still needs checking by the party
who can run the tree.

## What L registers

**D-23: the tier-1/2/3 ceiling is accepted as real on the strength of a behavioural measurement, D-1 is
recorded as failed rather than unevaluable, the 12-of-30 restatement debt is closed against the gate,
the pre-commitment's tier-3 restriction is retired without substitution, and D-3's ceiling-heavy remedy
is given the design constraints it currently lacks.**

Nothing about the experiment moves. The endpoint stays `B_oracle − A′@3_oracle`
(`DECISIONS_R3.md:136`), `PLAN_ARMS` stays `("a_prime", "a_prime3", "b")`, `CALIBRATION_DRAWS` stays 10
(`calibrate.py:51`), the band stays `(0.1, 0.9)` strictly (`calibrate.py:53-54`), `d_t` stays a ratio
over *graded* draws only, and `GATE_MIN_IN_BAND` stays 15 (`calibrate.py:109`). **No source file is
edited by this addendum and no code change is authorised by it.** `eval/tasks.lock` is untouched.

## 1. The decoy result, and the branch it lands in

`D1_RESTATEMENT_PRECOMMITMENT.md` fixed three triggers on 2026-09-01 ~19:30Z, before any decoy
measurement existed. The measurement ran 2026-09-03 as
`MAW_MEASUREMENT=1 ./venv/bin/python -u decoy_probe.py --replay --controls --measure`.

**Result: `k = 0` at `n = 30`.** Ten fresh Groq draws each on `ranking-01`, `grouping-02` and
`interval-logic-01`, every one passing the true suite and failing the decoy, all three tasks
discriminating in the sense the pre-commitment defines at `:55-57`. Cost as projected: 30 Groq Executor
calls, **zero Gemini**, the latter by construction rather than by intention — `_measure_gates` stops
the run if any target lacks a persisted spec and `_keys_for_groq` blanks the Gemini key so a stray call
raises instead of spending. The breaker read green before and after (260 draw files, 0 not graded), so
the probe added nothing to the calibration store and removed nothing from it.

**That is Branch 2 (`:86`), and its commitments are adopted here in full.** The one-sided 95% upper
bound on the echo rate is `1 − 0.05^(1/30)` = **9.5%**, and the estimand is stated in the
pre-commitment's own words: *the proportion of candidates that echo the answer key*, measured
behaviourally. The arithmetic is legitimate here for the reason recorded at `:93-95` — the estimand is
behavioural, so the bound bounds the thing named. The same arithmetic over a grep of call spellings was
rejected earlier in this project because it bounded "candidates matching 11 spellings" instead.

**The scope limits the probe printed about itself are registered as part of the result and travel with
the number wherever it is quoted.** Thirty draws on three tasks of thirty-six, under the single pinned
`openai/gpt-oss-120b`, and it is evidence about an answer-key echo specifically. It is not evidence
that this pipeline is free of reward hacking, and it does not extend to the ten unmeasured tasks or to
any other Executor. A future sweep on a different model inherits none of it.

**What did not happen, recorded because the pre-commitment made it mandatory if it had.** No candidate
passed both suites, so the 3× alternated re-run of A3 (`:209-215`) was never triggered, Branch 1's
harness rewrite is not owed, and the standing judgement that parent-side grading over IPC is "a
different harness, not worth it at this stage" survives intact — against a channel that remains open
and is now measured, rather than against one that was merely open.

## 2. The hand-read of the 30 bodies, which found something the count did not

A5 of the pre-commitment (`:246-256`) replaced the spent blinding with a stronger control: the probe
prints the full candidate source for **every** candidate passing the true suite, not the flagged subset,
and the entire passing population is hand-read. That control was exercised. All 30 bodies were read.

**Every one computes its answer, and not one performs a filesystem read.** No body contains `open(`,
`linecache`, `importlib`, `__loader__`, `inspect`, `sys._getframe`, `f_back`, `traceback`, `__file__`,
`read_text`, `Path(` or `os.path.dirname`.

**That spelling survey is corroboration and is explicitly not the instrument**, because this project's
standing rule is that a mechanism is never established or excluded by grepping its call spelling — which
is precisely why the decoy exists and why `k` is defined behaviourally. The finding is `k = 0`; the
absence of these twelve spellings is a consistent secondary observation and carries no weight of its own.
Had the survey found a hit, it would have prompted a hand-read rather than a conclusion.

**The unexpected result: the 30 candidates are behaviourally identical on every path the hidden suite
tests and divergent on paths it does not.** Three instances, each read twice — once by the author and
once independently under section 11 — and stated at the refined counts the second read produced:

- **`interval-logic-01` diverges on *shape* validation, not on the span check.** `start > end` raises
  `ValueError` in all 10 draws. But a malformed span — wrong type, or not a 2-element pair — raises
  `ValueError` in 8 draws and **`TypeError` in draws 5 and 6**, which is why both exception types appear
  in those two. All 10 pass, because the task's two `raises` checks cover only `start > end`.
  **Draws 1 and 5 are the same program**: same normalise/sort/merge shape, same three validation
  branches, same comments, differing *only* in which exception the two shape branches raise. Two
  structurally identical programs, one untested difference, identical scores.
- **`grouping-02` guards `isinstance(…, dict)` in draws 1, 4, 7, 9, 10 and omits it in 2, 3, 5, 6, 8.**
  The bound name varies too — `entry` in three, `row` in draws 7 and 9. **Draw 2 states an omission of
  this kind in a comment**, though on the neighbouring numeric axis and not on the dict guard itself:
  `# Ensure amount is numeric; let Python raise TypeError if not`. So this is not inattention; the model
  is resolving an underdetermined spec deliberately, and recording that it has done so. What the comment
  evidences is that disposition, not the dict-guard decision — that one is documented nowhere.
- **`ranking-01` filters non-numeric scores in draws 6 and 9 only.** The other 8 reach
  `rec['score'] >= 1`, so the raise site is the threshold comparison rather than the sort. There is a
  second axis on the same task: a non-dict record raises `ValueError` in the 6 draws carrying a dict
  guard and `TypeError` in the 4 without it.

**How this bears on the remedy, which is why it is registered rather than filed as a curiosity:**
`d_t = 1.00` on these three tasks is as much a property of suite coverage as of task difficulty. The
draws already disagree with each other; the suite simply does not ask. So the difficulty dial with
actual measured evidence behind it is *add checks on the paths where draws already diverge*, and
section 7 registers the constraint that governs whether that dial is available at all.

Two smaller observations from the same read, recorded because both are populations rather than anecdotes.
**Zero PEP 604 unions in 30 bodies** — the model used PEP 585 (`list[dict]`, `list[tuple[int, int]]`,
both valid on 3.9) or explicit `typing.List` — so the 3.9 annotation hazard has now not fired across
roughly 200 draws on these families. And the divergence pattern above is the same shape as the
`'""'` assertion found earlier in `delimited-parsing-01`: **behaviour on these tasks is decided by the
reference implementation rather than by the prompt**, wherever the suite is silent.

## 3. D-1 fails. The arithmetic, counted from the 260 draw files and the lock.

**26 of 36 tasks complete at 10 graded draws: 3 in band, 0 at floor, 23 at ceiling.** 260 draw files,
every one `outcome: "graded"`, zero `infra_loss`. The three in band are `delimited-parsing-01` (0.60),
`path-canonicalization-01` (0.70) and `path-canonicalization-02` (0.80).

**3 in band + 10 unmeasured = 13 maximum, against `GATE_MIN_IN_BAND = 15`.** Every remaining task
landing strictly inside the band still fails the gate. This is not a forecast and it does not depend on
drawing anything further: the gate cannot pass on this lock.

Broken out by tier, with tiers taken from `eval/tasks.lock` rather than from recollection:

| tier | tasks | measured | in band | at ceiling | unmeasured | max in band |
|---|---|---|---|---|---|---|
| 1 | 10 | 10 | 0 | 10 | 0 | 0 |
| 2 | 16 | 9 | 2 | 7 | 7 | 9 |
| 3 | 10 | 7 | 1 | 6 | 3 | 4 |
| **all** | **36** | **26** | **3** | **23** | **10** | **13** |

The per-tier maxima sum to exactly the global 13, which is the consistency check on the table.

**D-1's failure is recorded as the gate working, per Branch 2 at `:96-98`.** D-1 exists to catch a task
set that cannot discriminate between the arms. It caught one, before the grid spent anything, and the
decoy result is what licenses reading the failure as difficulty rather than as leakage.

## 4. The 12-of-30 restatement debt, closed. Decided on registration grounds, not on the count.

`calibrate.py:111` holds `GATE_AS_REGISTERED = "fewer than 12 of 30 in-band tasks is a NO-GO"` and the
comment at `:100-108` names the debt itself: *"The pre-registration says 'fewer than 12 of 30'. The code
generates 36 tasks across 18 families, so the registered number does not apply as written, and restating
it is a registration decision the user still owes."*

**Registered resolution: the gate is a proportion, not an absolute count. It reads as 40% of the locked
task set, which on 36 tasks is `GATE_MIN_IN_BAND = 15`.** That is ratification of the value already in
force, not the adoption of a new one.

The reason, stated without reference to any observed `d_t`:

- The registered gate is 12 of 30, i.e. **40%**. Reading the 12 as a proportion and applying it to 36
  gives `0.4 × 36 = 14.4`, which rounds to 14 or 15 depending on the rule. `calibrate.py:104-106`
  already chose 15, documented as *"the smallest integer at or above the registered 40% proportion …
  which is the most conservative reading of the registered gate rather than a new gate."*
- Reading the 12 as an absolute count while the denominator grows from 30 to 36 gives **12 of 36 = 33%**.
  That is a *weaker* gate than the one registered. **Carrying an absolute count across a change of
  denominator silently lowers the registered pass proportion**, which is a widening of the gate, and the
  pre-commitment's principle at `:113-120` forbids widening on a measured ceiling in terms that do not
  depend on the size of the widening.

So the absolute reading is ruled out because it weakens a registered proportion, and the proportional
reading is adopted because it preserves one. Neither step consults a measured value.

**The choice of rounding rule does not need adjudicating, and that is worth stating explicitly**: 14 and
15 are both above the 13 that this lock can achieve, so the resolution is stable under either. Only the
absolute-12 reading would change the outcome, and it is the reading with no implementation, no
documentation and a weaker proportion than the registration's.

**Disclosure, which is not the reason.** The maximum achievable in-band count on this lock is 13
(section 3). Under the adopted reading the gate fails; under the rejected absolute reading it would
pass. **The debt is therefore being closed in the direction that fails, in favour of the reading that
has been in force in code since before any of the 260 draws were taken.** That direction is recorded
because a threshold chosen after seeing the statistic it thresholds is the harm this project exists to
avoid, and the audit-relevant fact is that the closure refuses the passing reading rather than reaching
for it. Had the arithmetic run the other way — had the proportional reading been the permissive one —
this section would have had to be written before the count was known or not at all.

**No code change.** `GATE_MIN_IN_BAND` already reads 15 and stays 15. The calibration store's manifest
already records `"threshold_is_default": true` against it
(`eval/calibration/seed-0/calibration.json:34`, written by `calibrate.py:395`), so no run needs
relabelling and nothing on disk becomes retrospectively inconsistent with this closure.

## 5. The tier-3 restriction is retired, and nothing is substituted for it

Branch 2 commits at `:99-100` to *"The grid does not run on tier 1–2. Restrict to tier 3. Cost: 10
Planner calls for the missing tier-3 specs, then a tier-3-only calibration of 10 tasks × 10 draws."*
That clause is retired here. Two reasons, in the order they matter.

**First, its stated justification has expired.** A1 (`:171-176`) licenses narrowing on the ground that it
*"does not use the observed value of the gated statistic for any retained unit — those 10 tasks have
zero draws."* On 2026-09-01 that was true. It is now false: **7 of the 10 tier-3 tasks carry 10 graded
draws each**, 70 in total, drawn by the sweep in the interval between the pre-commitment and the
measurement. The permission was granted against a fact the intervening sweep destroyed.

This does not make executing the clause illegitimate — the rule was fixed in advance and following a
pre-committed rule is not selection on realised values, whatever arrived in between. It does mean that
**anyone re-deriving the permission today cannot use A1's reasoning to do it**, and a registered clause
whose only stated justification no longer holds should be retired rather than exercised on inertia.

**Second, and decisively, the clause is futile by its own threshold.** Branch 2 at `:101-104` names the
escape hatch in advance: *"tier-3 calibration returning fewer than 5 of 10 tasks in band triggers that
conversation"* — a new `tasks.lock` and a new registration. Tier 3 stands at **1 of 7 in band with 3
unmeasured, so its maximum is 4 of 10, which is already below 5.** Executing the restriction spends
3 Planner calls and 30 draws to arrive, with certainty, at the destination the escape hatch names. The
clause's own cost line is also stale: 3 tier-3 tasks lack specs, not 10.

**The tier axis has no empirical support, and the temptation this creates is refused explicitly.** In-band
rates are 0 of 10 at tier 1, 2 of 9 at tier 2, 1 of 7 at tier 3. Tier 3 is not the discriminating tier;
it is indistinguishable from tier 2 at these counts and nominally worse. The only family with both
variants inside the band is `path_canonicalization`, which is **tier 2** (`eval/tasks.lock:209`, `:218`),
so the restriction as written would discard the two best tasks in the entire set.

**It follows that substituting "restrict to tier 2" is forbidden, and this is the point of the section.**
Tier 2 looks better only because its `d_t` have been observed. Re-pointing the narrowing at the tier
whose measured values are most favourable is selection on the realised values of the gated statistic —
the same harm as widening the band, in the same currency, and it is the move that would be easiest to
present as a small change. **No tier restriction is registered. The narrowing route is closed and the
remedy route in section 6 is the only one open.**

## 6. D-3 fires ceiling-heavy, and two independently pre-registered routes converge on one action

D-3 (`DECISIONS_R3.md:68-81`) defines `F` = tasks with `d_t <= 0.1` and `C` = tasks with `d_t >= 0.9`,
with ties counting as floor-heavy. **Measured: `C = 23`, `F = 0`. `C > F`, so the ceiling-heavy branch
fires**, and it fires under every assignment of the 10 unmeasured tasks — all 10 at the floor still
leaves `F = 10` against `C = 23`. The registered remedy is *"Regenerate with harder parameters, keeping
the same 18 families and the same tier structure, write a new lock, and re-run the sweep. No change to
the endpoint or the gate."*

**Section 5's escape hatch and D-3's ceiling-heavy branch are two rules written on different days, for
different reasons, that name the same action: a new `tasks.lock`.** Neither was written with the other in
view — D-3 predates the sweep entirely, the escape hatch predates the decoy — and neither depends on the
other's evidence. That convergence is the whole warrant for regeneration and it is recorded as such,
because a single rule reached by a single route is a weaker thing than two.

**What the decoy result contributes to this, precisely.** D-3's remedy assumes the ceiling means the
tasks are too easy. Before the measurement the pass rate could not separate that from a read-and-echo
channel, and under the second explanation hardening the lock would harden tasks nobody was solving while
the new lock inherited the same defect. `k = 0` at `n = 30` is what makes the remedy the right remedy
rather than a plausible one — subject to section 1's scope limits, which is why they are registered
alongside it rather than dropped once the branch is taken.

### 6a. The remedy has no implementation, and these are the constraints on the one that gets built

`generate(seed=0, per_family=2, tiers=None, families=None, limit=None)` is the complete parameterisation
of `eval/gen_tasks.py`. **There is no difficulty parameter.** Difficulty is a source literal inside each
family builder, so "regenerate with harder parameters" currently names an argument that does not exist.
Building it is a later sprint's work and **nothing in this addendum authorises writing it**; what L
registers is what it must satisfy, fixed now so the dial is not designed around the numbers it produces.

**Constraint 1 — the dial is family-granular, because `d_t` is a family property.** Of the 12
fully-measured families, **11 have both variants on the same side of the band**: 10 at double ceiling,
`path_canonicalization` with both inside (0.70/0.80), and `delimited_parsing` the single split
(0.60/1.00). A per-task dial would be fitting 36 knobs to 12 effective observations.
`gen_tasks.FAMILIES` already exposes family granularity and D-3 already requires the 18 families and the
tier structure be preserved, so the constraint costs nothing structurally.

**Constraint 2 — the dial must exempt what is already in band.** `path_canonicalization` (both variants)
and `delimited-parsing-01` are the only three tasks in the band. Applying a uniform hardening pass pushes
the only tasks currently discriminating out of the band, which converts a 3-in-band lock into a
0-in-band lock and makes the second sweep worse than the first. The exemption is a floor on the remedy,
not a tuning choice, and it must be declared in the new lock's manifest rather than left implicit.

**Constraint 3 — check count is not the dial.** The cheapest hypothesis available was that in-band tasks
have more acceptance checks. It is false: `delimited-parsing-01` and `delimited-parsing-02` carry
byte-identical decoy coverage — 21 checks, 19 values, 2 raises — and sit at 0.60 and 1.00. Whatever puts
a task in band, it is not the number of assertions, and a remedy that adds assertions is not thereby a
remedy. Section 2's finding is the live alternative and section 7 governs whether it is available.

**Constraint 4 — the new lock is calibrated before it is trusted, and the same gate applies.** D-3's
"re-run the sweep" is not optional and the gate it is measured against stays 15 of 36 per section 4. A
regenerated lock that lands 13 in band fails identically, and discovering that requires the sweep.

## 7. Whether a harder suite is a "harder parameter" — the distinction, registered undecided

Section 2 produced a difficulty lever with measured evidence behind it: add acceptance checks on the
paths where independent draws already diverge. **It is registered here that this lever is not yet
licensed, and why, rather than being used because it is good.**

D-3 authorises regenerating tasks with harder parameters and writing a new lock. A new task carries a new
suite, so a new lock's suites are new by construction and no question arises. **Editing the suite of an
existing task in place is a different act wearing the same name**: it changes what counts as a pass on a
unit whose `d_t` has already been observed, which is structurally the widening hazard of `:171-176` run
in the strict direction rather than the permissive one. That the direction is strict is not a defence —
the objection in A1 is that the threshold becomes a function of the realised values, and a function
chosen to move them down is still a function of them.

**Registered position: the lever is available only inside a regeneration that produces new tasks, where
the divergence finding informs the *design* of new suites for tasks that have no measured `d_t`. It is
not available as an edit to any of the 36 tasks in the current lock.** The finding transfers; the edit
does not. If a future sprint wants the in-place edit, it argues for it in its own addendum against this
paragraph, which is why the position is stated as a position and not as an aside.

## 8. `code` persistence is a pre-grid decision, and the reason is structural rather than incidental

The pre-commitment left this open at `:141-143`, on the ground that `eval/run_eval.py` already persists
`code` so only calibration is blind. **Verified against all three stores, and the situation is worse than
"only calibration":**

| store | records | code available |
|---|---|---|
| `eval/calibration/seed-0/draws/` | 260 | `code_sha256` only, **no `code` key** |
| `eval/results/seed-0/arm-*/` | 32 | `code` present, but all 32 carry `stub: "flaky"` |
| `eval/results/pin-executor/ledger.jsonl` | 93 | `code_sha256` + `code_chars`; `extracted_code` is a bool |
| `eval/results/pin-replay-1/ledger.jsonl` | 76 | as above, plus `failed_code` — **failures only** |

**Three stores, three schemas, and every one of them hashes away the body of a candidate that passed
while the only real code bodies retained anywhere belong to candidates that failed.** The decoy
discriminates only among candidates that pass the true suite, so `failed_code` is unusable for it by
construction. This is why the probe had to spend 30 fresh Groq draws to answer a question about 260
already-banked ones: **the read channel was retrospectively unauditable across the entire project, and
not by anyone's decision.** No schema was designed to prevent the audit; three independent schemas
happened to converge on discarding exactly the artifact the audit needs.

**Registered: `code` persistence for any future calibration store is a decision that is made before that
store is written, and it is made in the addendum that authorises the store rather than afterwards.** L
does not settle which way, because the storage and secret-handling questions belong with whoever
implements it — `code` bodies are model output and the standing rule that keys never reach the run JSON
applies to anything written beside them. What L closes is the option of leaving it to be discovered
again. **No field is backfilled onto the 260 existing draws**, per K §7 and the standing prohibition.

## 9. The 10 unmeasured tasks: not drawn, with one registered exception available

The unmeasured tasks are `config-parsing-02`, `query-canonicalization-02`, `template-expansion-01/02`,
`tiered-pricing-01/02`, `validation-01/02` and `version-ordering-01/02`. **None has a plan on disk**, so
each costs a fresh Gemini Planner call, against a **`gemini-3.6-flash`** ceiling of 20 requests per day —
the Planner slug at `agents_core.py:83`, with the 20 evidenced by K §5's `quotaValue: 20` and the 04:22Z
429, both on the Planner. **11 of the 20 remained when this was verified at 2026-09-03 ~10:00Z**, stated
as a timestamped observation rather than as a standing fact.

**That ceiling is a property of the slug and not of the free tier, and the distinction is registered here
because getting it wrong inverts the conclusion.**
`probe_rpd_gemini-3.5-flash-lite_2026-09-02.json` records **111 of 111 succeeded, verdict
`CAP_ABOVE_PROBE`** — the lite model reached no cap at all. So a reader asking whether the 20 still binds
must ask it of `gemini-3.6-flash` specifically, and must not generalise from any other Gemini slug in
this project or outside it. Related and easy to lose: **the quota counts requests, not delivered plans.**
Today's three Planner calls all returned 503 and charged 9 HTTP requests between them for zero plans,
which is the same accounting the whole of K §5 exists to establish.

**Registered: they are not drawn.** Every gate they could inform is decided — D-1 fails at 13 maximum,
D-3 fires under every assignment of them, section 5's threshold is already breached — and D-3's remedy
replaces the lock they belong to, so their `d_t` describes task instances that will not exist. The
pre-commitment's standing resource decision at `:151-154` applies unchanged: *"Spending 17 of a 19/day
quota to confirm a gate failure is the expensive order of operations."*

**The one real cost of not drawing, stated so it is not lost:** four families are wholly blind
(`template_expansion`, `tiered_pricing`, `validation`, `version_ordering`) and two are half-measured
(`config_parsing`, `query_canonicalization`). Under section 6a's family-granular dial, setting a
difficulty parameter for a blind family means setting it with no observation of that family at all.

**Registered exception, available to the sprint that builds the dial and not before:** draw **one variant
per blind family** — 4 tasks, 4 Planner calls — which reads all four blind families at 40% of the cost of
the full remainder. The selection rule is *variant 0 of each family with zero measured draws*, which is
mechanical, fixed here in advance, and does not consult any observed value. **Exercising it requires
recording in the new lock's manifest that those four families were calibrated at one variant and the rest
at two**, since that is an asymmetry in the evidence base for the dial. Drawing the other six, or
choosing which variant to draw after seeing the first, is not licensed by this paragraph.

## 10. What this addendum does not license

- It does not license a change to the endpoint, to `PLAN_ARMS`, to D-1's 15-of-36 threshold, to the
  `(0.1, 0.9)` band, to `CALIBRATION_DRAWS = 10`, or to `d_t` as a ratio over graded draws. Section 4
  ratifies 15; it does not reopen it.
- It does not license editing `GATE_MIN_IN_BAND`, `BAND_LOW`, `BAND_HIGH` or any other constant in
  `calibrate.py`. **This addendum authorises no source edit whatsoever.**
- It does not license regenerating `eval/tasks.lock` or touching `eval/gen_tasks.py`. Section 6 registers
  that the remedy is warranted and section 6a registers what it must satisfy; **the implementation is a
  separate sprint and a separate addendum**, and the lock is not to be regenerated before the dial exists
  and section 6a's four constraints are demonstrably met by it.
- It does not license restricting the grid to any tier, tier 3 included. Section 5 retires the clause and
  substitutes nothing.
- It does not license editing the acceptance suite of any task in the current lock, per section 7.
- It does not license quoting the 9.5% bound without section 1's scope limits, or restating it as evidence
  about all 36 tasks, about any model other than `openai/gpt-oss-120b`, or about reward hacking generally.
- It does not license a change to the Executor prompt. A2 (`:204-207`) stands: the prompt is spliced into
  all four arms and the banked draws are measured against it, so editing it now breaks comparability with
  everything on disk. The disclosure it contains is a known characteristic of the treatment.
- It does not license backfilling `code`, or any other field, onto the 260 existing draw files.
- It does not license closing the read channel, and does not license reopening the judgement that
  parent-side grading over IPC is out of scope. Branch 1 did not fire.

## 11. What must be verified before this addendum is committed

L was written against the files by a party that cannot execute the project venv. The counts below were
taken by reading the draw files and `eval/tasks.lock` directly, and they are reported as such rather than
asserted:

1. **Re-derive section 3's table from `--gate-only`.** 26 measured, 3 in band, 0 floor, 23 ceiling, 260
   draw files all `outcome: "graded"`, and the per-tier split 0/10, 2/9, 1/7. A disagreement anywhere
   blocks the commit rather than getting reconciled in prose.
2. **Confirm the 13-versus-15 arithmetic and that both 14 and 15 exceed 13**, which is what makes
   section 4 independent of the rounding rule.
3. **Confirm from the lock that `path_canonicalization` is tier 2 and that tier 3 is exactly** the ten
   tasks in `config_parsing`, `delimited_parsing`, `expression_eval`, `graph_traversal` and
   `template_expansion`. Section 5's decisive claim rests on both.
4. **Confirm that 7 of the 10 tier-3 tasks carry 10 graded draws**, which is the fact that expired A1's
   justification, and that exactly 3 tier-3 tasks lack a persisted spec.
5. **Re-read the 30 candidate bodies in `decoy_measure_2026-09-03.log`** against section 2's three
   divergence claims — `interval-logic-01` draws 5 and 6 raising `TypeError`, `grouping-02`'s 5-of-10
   `isinstance` guard, `ranking-01`'s 2-of-10 numeric filter. These were read once; they should be read
   twice before a registration document rests on them.
6. **K §8 remains blocking on its own terms.** K states its four check counts are *"a report and not yet
   an independent verification."* Re-run both batteries, diff the name sets, and commit K with
   `agents_core.py`, `eval/calibrate.py`, `eval/run_eval.py` and `test_pipeline.py`. **L is committed
   after K, not with it** — L cites K's D-22 environment and must not land first.

## 12. What would reopen L

- **Any `infra_loss` record appearing anywhere**, which makes K §3's 0-of-260 stale and, because `d_t` is
  a ratio over graded draws, changes the denominator that section 3's counts are computed on.
- **A decoy run returning `k >= 1` on any task, ever.** Branch 1 is not retired by a `k = 0` at n = 30 on
  three tasks; it is unfired. A single positive on any future draw reverts sections 1, 3 and 6 to
  "unevaluable" and makes closing the channel mandatory, and the 9.5% bound becomes a historical artifact
  of three tasks rather than a property of the harness.
- **A change of Executor model.** The bound, the ceiling reading and every `d_t` on disk are properties of
  `openai/gpt-oss-120b`. A new pin invalidates the calibration, not merely the decoy result.
- **The section 11 re-derivation disagreeing with any count above**, in which case the disagreeing section
  is rewritten before the commit rather than footnoted after it.
- **A regenerated lock that lands fewer than 15 in band**, which would mean two hardening passes have
  failed to produce a discriminating task set and the problem is the family list or the Executor rather
  than the difficulty dial.

## 13. Verification record — section 11 executed 2026-09-03, before this addendum was committed

**This section exists because K §8 is the counter-example.** K labelled its four check counts *"a report
and not yet an independent verification"* and therefore could not be committed until they were re-run; a
registration document whose own verification status is unresolved inside it is a document that blocks
itself. L is committed with that status resolved rather than pending.

**All six items of section 11 verified against the tree by the implementer. No count in this addendum
disagrees, so section 12's reopening clause has not fired.** Recorded specifically:

- Item 1 re-derived section 3's table cell for cell from `--gate-only`, including the per-tier split, with
  260 draw files at `outcome: "graded"`, `stop=260`, zero `infra_loss`, zero `provider_substituted`, and
  no draw mentioning Gemini anywhere. Item 2's five citations all resolve as written. Items 3 and 4
  confirmed the lock's tier assignments, the 70 tier-3 draws and the 3 tier-3 tasks without a spec, and
  additionally that the 10 unmeasured tasks and the 10 tasks with no plan on disk are the same 10.
- Item 5's second read produced the three refinements now carried in section 2, and confirmed `k = 0`
  from the log independently — 30 blocks, `passes_true` true on all 30, `passes_decoy` false on all 30.
- Item 6 was already satisfied: **K is committed at `f99215c`** with its four source files, its counts
  verified and agreed. Batteries at **`f492899`**, the tip as this addendum is written, read **1034 / 0**
  and **306 / 0**, at 135 and 40 named functions. They are attributed to that commit rather than to "the
  current tree" because the next queued source change adds pipeline checks, and an unpinned figure would
  be stale on the day it landed. **L therefore lands after K as required.**

**One defect was found and fixed before the commit, and it is recorded rather than silently corrected.**
Section 9 as first written named the Planner `gemini-3.5-flash`. The Planner is **`gemini-3.6-flash`**
(`agents_core.py:83`, and the only Gemini slug in the events store). The 20/day figure was right and the
slug was wrong, which is the worse of the two failure modes available: the nearest real model to the
wrong name is `gemini-3.5-flash-lite`, measured at 111 of 111 with verdict `CAP_ABOVE_PROBE`, so a reader
chasing the erroneous slug would have reached the opposite conclusion about whether a cap applies.
Section 9 now names the correct slug and states explicitly that the ceiling is a property of it.

### Three observations about K, reported here and amending nothing

All three concern K's own text or its reopening conditions. **Nothing in K is amended by this addendum**;
K is committed and only an addendum M could touch it, and none of the three warrants one.

- **K's held-open-request reopening condition has not fired.** K §12 names *"a `calls = 1` draw exceeding
  60s"* as the observation that would revive the reading K §6 discarded. Over the grown store of 260
  draws: **n = 154 at `calls = 1`, maximum 22.0s, zero above 60s.** K §6's separation was measured at 170
  draws and holds at 260. The uncapped-honoured-header explanation remains the favoured one.
- **K's 5xx channel has still never gone live, and D-22's accounting worked the first time it was
  needed.** K §3 registered that the confound fires only on an *executor*-side 5xx; today's three 503s
  all landed on the **planner**, as has every recorded 5xx in this project's history. Separately, those
  three calls each logged `http_attempts: 3` with three `http_attempt` events — **9 HTTP requests
  recorded where the pre-D-22 regime would have recorded 3**, for zero delivered plans. That is the hole
  K §2 identified, now measured on live traffic by the instrument K installed to see it.
- **K §8's reported post-edit count is exact for the tree K was committed with, and the whole difference
  from the current tree is the commit that followed it.** K §8 reports the pipeline battery at 1023
  checks / 134 names after the edit; `f492899` measures **1034 / 135**, while the harness battery matches
  K exactly at 306 / 40 because `f492899` touched no harness check. That +11 / +1 is `f492899` in its
  entirety — the commit implementing K §6's first registered consequence, `note_429` consulting
  `agents_core.MAX_RETRY_AFTER_SECONDS` instead of a second constant. Measured by AST over both committed
  blobs rather than by grep, and re-run independently on the reviewing side: `f99215c:test_pipeline.py`
  carries 134 test names and 964 `check()` sites inside them, `f492899:test_pipeline.py` carries 135 and
  975, the single added name is `test_the_governor_honours_the_cap_the_manifest_already_advertised`, none
  is removed, none is duplicated, and the per-function delta has **exactly one non-zero entry** — that
  function, 0 → 11. The diff of that file is 121 insertions and **0 deletions**. Each tree holds one
  further `check()` site outside any named test function, the driver's own catch-all for a raising test,
  identical in both and contributing nothing at runtime because no test raised; 965 and 976 are therefore
  the same measurement under a wider instrument, not a discrepancy. Static and runtime agree on the delta
  at +11 apiece across a constant 59-check gap, which is loop-repeated checks in both trees — the static
  leg is verified on the reviewing side, the runtime leg is the battery's own output and was not re-run
  there. **So nothing landed between the §8 snapshot and `f99215c`, and K §8's four numbers need no
  correction.** The delta is additive, 11 checks and 1 name gained and none lost, so the standing rule
  that nothing be silently deleted or weakened is satisfied here by measurement rather than on its face.
  This is bookkeeping about the tree, so the L commit message carries the attribution and K's committed
  text is left exactly as written.






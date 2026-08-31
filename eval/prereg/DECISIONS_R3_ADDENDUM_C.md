# Pre-registration addendum C to Revision 3

**Written 2026-08-31, after Addendum B and while Sprint 10 was in progress.** Separate file
rather than an edit to Addendum B, because B had already been handed over; a registration
document that changes after it is read is not a registration.

Scope: adds D-12. Everything in Revision 3 and addenda A and B stands unchanged.

---

## D-12. The wrong-suite hazard is confined to four tasks, and its mechanism is asymmetric

**Registered: the exposure set is enumerated in advance; the Test Writer is told not to predict
floating-point values; no suite is edited and no tolerance is added to the gate.**

### The mechanism, stated exactly

The locked hidden suites assert exact rounded floats — `grouping-02`'s asserts `13.26`, `53.1`,
`148.39` and 29 more — and they are **correct**, because `eval/gen_tasks.py` obtained the expected
values by *executing the reference implementation*. Verified offline: all 36 references pass all 36
hidden suites, and the regenerated task set is byte-identical to `eval/tasks.lock` on every
`tests_sha256`.

A model-authored suite has no reference to execute. It **predicts** the value. On
`grouping-02` the Test Writer wrote inputs `1.004` and `2.001` — literals that appear **nowhere**
in the task or its hidden suite, so it invented them — and asserted the sum rounds to `3.01`. In
decimal it does. In IEEE 754 the sum is `3.00499...` and `round(sum, 2)` is `3.0`. Every correct
implementation fails.

So the hazard is not test-writing incompetence in general. It is specifically that **a model
asked to predict a floating-point result is guessing at binary representation**, and it will lose
some fraction of the time.

### The exposure set, enumerated in advance

Exactly **4 of the 36 locked tasks** call `round(x, N)` with `N > 0` in their reference:

| task | family | reached in the pin probe |
|---|---|---|
| `grouping-01` | grouping | yes |
| `grouping-02` | grouping | yes — **produced the wrong suite** |
| `tiered-pricing-01` | tiered-pricing | no |
| `tiered-pricing-02` | tiered-pricing | no |

The remaining **32 tasks have zero exposure**: no rounding in the reference, so a generated suite
never has to predict a float. Registered consequence: the wrong-suite term reported under D-10 is
expected to concentrate on these four rows, and a wrong suite appearing **outside** this set is a
different and unregistered failure mode that must be reported as such rather than folded into the
same count.

`tiered-pricing` sorts after `text-normalization`, so the probe's 19 tasks never reached it. Two
untested exposed tasks remain in the unrun 17.

### What is *not* predictable, and why that matters

A trap detector was attempted and **found nothing**: no subset sum of the float literals present in
any task diverges between IEEE 754 and exact decimal rounding. That is the informative result. The
divergent inputs on `grouping-02` were invented by the model at generation time, so **the hazard
cannot be predicted from the task's own data** — only the exposure set can be. Any proposal to
pre-screen tasks for "trap inputs" is therefore rejected as unimplementable, and screening is
replaced by the prompt rule below plus the D-10 report-only count.

### Registered remedy

1. **Test Writer prompt rule** — and the Planner, since it authors the TESTS block that is
   actually used (the Test Writer fired 0 times in 19 plans): when the specified behaviour rounds
   to a fixed number of decimal places, do not hand-compute an expected value. Choose inputs whose
   sums are exactly representable in binary — values with at most two decimal places that are
   sums of halves, quarters and eighths — or assert on a property rather than an exact value.
   Registered as a change to the Planner's output distribution, in the same category as D-7 Part A.
2. **No tolerance is added to the gate.** Loosening float comparison to rescue a bad assertion
   changes the grader on all 36 tasks to fix a defect on 4 — and a grader that accepts approximate
   answers cannot detect the off-by-a-cent errors these tasks exist to test.
3. **No suite is edited, regenerated, or excluded**, per D-10. `grouping-02` stays exactly as it is.
4. The rule is a mitigation of unknown effectiveness, not a fix. Effectiveness is measured by the
   D-10 wrong-suite count over the four exposed rows, not asserted.

### How the remedy is read — added 2026-08-31, before this file was handed to anyone

Two external findings bear on point 4 and are registered here so the remedy's evaluation cannot be
read naively. Added the same day the file was written and **before it left this session**, so the
"a registration that changes after it is read is not a registration" rule is intact.

Source for both: Ouédraogo, Kaboré, Tian, Song, Sengupta, Klein, Bissyandé, *Prompt Engineering in
LLMs for Automated Unit Test Generation: A Large-Scale Study* (arXiv:2407.00225v4) — 216,300
generated suites, 4 models, 5 prompting strategies, 3 datasets. Java/JUnit, so **the figures below
transfer directionally and not numerically** to MAW's Python-with-plain-`assert` setting.

1. **The propensity this rule targets is near-universal and prompt-insensitive.** The Magic Number
   Test smell — a hand-written numeric literal asserted without derivation, which is exactly the
   `== 3.01` failure — occurs in **85.64%–100%** of generated suites in *every* model × strategy
   cell, including Chain-of-Thought and a guided Tree-of-Thought. No prompting strategy moves it.
   The registered expectation for the D-12 prompt rule is therefore **near-zero effectiveness**, and
   a null result is the predicted result rather than a disappointment.

2. **The rule's likeliest failure mode converts a wrong suite into a vacuous one.** It offers two
   escapes: binary-exact inputs, or assert a property instead of an exact value. The second is
   easier to comply with badly. The same study finds reasoning-style prompting raises the Empty
   Test rate sharply — up to **78.80%** in one model/dataset cell under CoT against 33.33% under
   zero-shot — and states that structured prompting "may improve formatting and organization yet
   coincide with shallower test bodies." A Planner that satisfies this rule by weakening
   `== 3.01` into something trivially true has not removed a defect. It has moved it out of the
   D-10 wrong-suite count and into the `vacuous` classification, and thence into
   `SELECTION_NO_GATE`.

**Registered consequence:** when D-12's remedy is evaluated, the **D-10 wrong-suite count and the
`vacuous` / `SELECTION_NO_GATE` rate are read together as a pair over the four exposed rows.** A
fall in the former with a rise in the latter is not evidence the remedy worked. Both numbers are
already instrumented and already required to be reported — D-10 point 2 and D-4 respectively — so
this adds no instrumentation, no code and no spend. It adds only the rule that neither may be
reported alone.

**Also registered as a gap, not a finding:** that study measures extractability, compilability,
coverage, readability and test smells. It does **not** measure whether generated assertions are
correct against a ground-truth reference, and says so in its own limitations. No external estimate
of the D-12 wrong-suite rate exists in the literature reviewed. The D-10 count is therefore a
measurement of something unmeasured, not a replication, and must not be compared to a published
baseline that does not exist.

### Collateral finding, registered because it bounds the D-7 fix

The locked oracle is clean: **no PEP 604 union and no `match` statement appears in any of the 36
references or the 36 hidden suites.** The 3.9 artifact lives entirely in the generated path — spec
text and Executor code. D-7 therefore requires no change to `eval/tasks.lock`, and the lock is not
regenerated.

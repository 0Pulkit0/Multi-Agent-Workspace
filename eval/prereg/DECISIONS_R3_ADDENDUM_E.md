# Pre-registration addendum E to Revision 3

**Written 2026-09-01, after the D-13 pin was committed and before any calibration draw is spent.**
Separate file rather than an edit to D-6 or to addendum D, for the reason addendum A gave and
addendum D repeated: a registration document that changes after it has been read is not a
registration. D-6's text is left exactly as it was written on 2026-08-30, including the sentence
this item shows was not true of the sweep.

Scope: adds **D-14** and **D-15**. Everything in Revision 3 and in addenda A, B, C and D stands
unchanged. No decision here changes what any call sends except D-14, which makes the two sweep
entry points send what D-6 already registered.

---

## D-14. D-6's `reasoning_format="hidden"` installed in the source defaults

**Registered: `ROLE_PARAMS = {"executor": {"reasoning_format": "hidden"}}` in `agents_core.py`,
committed before the calibration sweep runs.** Previously `ROLE_PARAMS` was `{}`.

### This closes a gap between two measured environments; it is not a preference

D-6 registered the parameter on 2026-08-30 and it was installed — in `eval/pin_executor.py`, which
calls `agents_core.configure()` with its own parameter table. It was never installed anywhere the
**calibration and grid sweeps** could see it. Both of those resolve their models through
`agents_core.configure_models()`, and with no `models.json` and no `MAW_MODELS` in the environment
`sampling_for("executor")` returned `temperature` and `top_p` alone.

So the two runs would have differed in what they sent:

| | Executor calls | `reasoning_format: "hidden"` |
|---|---|---|
| `eval/results/pin-executor/ledger.jsonl` | 57 | **57 / 57** |
| `eval/results/pin-replay-1/ledger.jsonl` | 57 | **57 / 57** |
| `eval/calibrate.py`, `eval/run_eval.py` before this item | — | **0** |

Counted off both ledgers directly: 57 of 57 executor call records carry `reasoning_format:
"hidden"`, and 57 of 57 stored replies carry both `params.reasoning_format == "hidden"` and
`extra_body == {"reasoning_format": "hidden"}`.

**The reason for installing it is therefore that `d_t` would otherwise be measured in an
environment the Executor pin was never measured in.** D-13 pins `openai/gpt-oss-120b` on the
evidence of those 57 draws; D-6 states that `d_t` "is not transferable" across Executors. A `d_t`
measured with the parameter dropped is measured against a differently-configured Executor, which is
the same non-transferability arriving through the parameter table instead of through the model slug.

This is not registered as a preference between two defensible configurations, and not as a
correctness improvement. D-6 already measured what dropping it costs: `openai/gpt-oss-20b` with
`reasoning_format` unset returned **zero fenced code blocks while 568 characters went to a separate
`reasoning` field**. That measurement is D-6's basis and is not re-litigated here. What is new here
is only that the sweeps now share the pin's environment.

### Scope of the entry, stated because the mechanism is wider than the decision

`sampling_for(role)` is keyed by **role**, not by provider. The registered entry is one parameter
on one role, and the two Gemini roles (`planner`, `test_writer`) resolve to `temperature` and
`top_p` and nothing else. This matters beyond tidiness: outside measurement mode `call_role` fails
over to whatever provider has a key, and the Streamlit app can reach that path, so an entry keyed
any wider would send a Groq extension to Google on a failover. Registered as one role, one
parameter, and checked in `test_pipeline.py` rather than left to review.

### What this does not license

1. **No second parameter and no second role.** Anything else added to `ROLE_PARAMS` is a change to
   what the sweep sends and needs its own registered item, not this one.
2. **No token ceiling.** See D-15. `max_tokens` and `max_completion_tokens` remain unsent, and that
   is now a check rather than a convention.
3. **No re-run of the pin.** D-13's budget is spent (D-9 point 6). The pin ledgers already carry the
   parameter, so this item creates no discrepancy that a re-run would resolve.
4. **No claim about `d_t`.** Installing the parameter makes the sweep comparable to the pin. It
   predicts nothing about whether the D-1 gate clears.

### What would reopen it

- The parameter being rejected by the endpoint — an unknown name goes through `extra_body`, so it
  would surface as a provider error on the first Executor call rather than silently, and the
  preflight validates the parameters the role will actually send before a sweep spends anything.
- A change of Executor provider, at which point `reasoning_format` is a Groq extension being sent
  somewhere it does not belong, and both the parameter and the pin need re-registering together.

---

## D-15. A truncated draw stays in `d_t`'s denominator, and the count is reported

**Registered: `finish_reason` is recorded on every draw record in both sweep entry points, and the
number of graded draws that did not finish at `stop` is printed with the calibration rates. A draw
that was cut off is still graded and still counted.** No token ceiling is set.

### Why this is a registration decision and not a bug fix

D-13 caveat 4 records that both of `openai/gpt-oss-20b`'s losses to the pinned model were
`finish_reason: "length"` at the endpoint's own 2048-token default: 2046 hidden reasoning tokens, **0
characters of output**, 0 code fences. It did not answer wrongly; it ran out of budget while
thinking and returned nothing. Across both pin ledgers the split by candidate is:

| candidate | `stop` | `length` |
|---|---|---|
| **`openai/gpt-oss-120b`** (pinned) | **38** | **0** |
| `openai/gpt-oss-20b` | 36 | 2 |
| `qwen/qwen3.8-27b` | 38 | 0 |

The pinned Executor was never truncated in 38 draws, so this is a known behaviour of the endpoint
rather than a known defect in the model the sweep will use. But `d_t` is the share of *graded* draws
that passed, and a truncated draw is graded — it produces no code, fails the hidden suite, and pulls
the rate down exactly as a wrong answer does. Two questions therefore had to be answered before the
numbers exist, not after:

1. **Does a truncated draw count against `d_t`?** **Registered: yes.** It stays graded and stays in
   the denominator. The alternative — excluding it as an infrastructure loss, the way a 429 is
   excluded — is available in the code and is deliberately not taken: a 429 means no completion
   happened, while a truncation means the Executor was asked and this is what came back. Excluding
   it would raise every affected `d_t` and would do so on a rule chosen after seeing which tasks it
   helped. D-10 point 3's principle, that an instrument is not adjusted after its output has been
   read, applies to the denominator as much as to the suites.
2. **Is the reader told?** **Registered: yes, unconditionally.** The count prints on every
   calibration run including the clean one, because a line that appears only when something is wrong
   is a line whose absence cannot be distinguished from a version that never looked. When the count
   is non-zero the affected task and draw index are named, since the reader's next question is which
   `d_t` to stop trusting.

Together those two give a bound rather than an adjustment: the printed rate is the sweep's, and the
count says how much of it could be artifact. Nothing is silently corrected.

### The ceiling is not raised

Registered, and it is the same commitment as D-13's caveat-4 clause: **no `max_tokens` and no
`max_completion_tokens` is sent, by this item or by D-14.** 2048 is the endpoint's own default and it
is the environment the pin was measured under. Raising it now would change that environment after
the pin was decided — and D-13 records that whether a higher ceiling would erase the pinned model's
entire lead over the runner-up is **unmeasured**. Setting one is a registration decision available to
whoever wants to make it, in its own item, with the re-measurement that implies. It is not a fix and
it is not authorised here.

### Recorded where, precisely

So that a reader can check the claim rather than take it: `finish_reason` is on the calibration draw
record (`eval/calibrate.py`, `one_draw`), on arm A, A' and A'@3's records and on every stored
candidate including the discarded ones (`eval/run_eval.py`), and in the per-call slice every arm
writes. The last of those is not redundant: **arm B has no top-level `finish_reason`** — its text
comes back from `run_workspace`, not from `_draw` — so for the arm that makes the most calls the
call log is the only place a truncated repair round is visible at all.

`None` is recorded when no real completion was observed, and never defaulted to `"stub"` or
`"stop"`. A `--stub` sweep produces `None` on every draw, because the stub replaces `call_model`
above the layer that observes a completion; those are counted and reported separately from
truncations, so an offline run cannot print a large artifact count and teach the reader to skip the
line.

### What would reopen it

- A calibration sweep in which the non-`stop` count is large enough that the gate's outcome depends
  on it. That is the one case where the exclusion rule in question 1 is load-bearing rather than a
  stated convention, and it would need its own registered decision — taken with the count in hand
  and stated as a departure, not applied quietly.
- A provider that stops reporting `finish_reason`, which would show up as the unknown count on real
  draws rather than as silence.

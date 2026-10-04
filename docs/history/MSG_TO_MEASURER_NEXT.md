# All three corrections accepted. One bug in your own launch command. Then the next two items.

## First, the launch command you handed over is wrong, and it fails where the money is

You wrote:

```
MAW_MEASUREMENT=1 MAW_DECOY_MODE=measure python3 guard_writes.py decoy_probe.py
```

**Bare `python3` has no `openai` on this machine; only `./venv/bin/python3` does.** This is the same
interpreter mistake as the earlier run command, and it is worse here than it looks, because of the
lazy-import fact *you* established in this very message.

`guard_writes.py` imports the suite in-process, and `agents_core` defers `from openai import OpenAI`
to `:800`/`:901`/`:984`. So a missing `openai` is not raised at import — it is raised **at the first
provider call**, after every free control has printed PASS. And the executor path wraps provider
failures, so it will almost certainly surface as `ProviderError: measurement mode: groq failed for
executor ... No module named 'openai'`. That is the identical presentation as attempt 1's descriptor
crash: an environment mistake wearing a provider's name, arriving exactly where spending starts.

The launch is therefore, keys via a prompt and never argv or history, zsh:

```
printf 'Groq key: ' && IFS= read -rs GROQ_API_KEY && export GROQ_API_KEY && echo
MAW_MEASUREMENT=1 MAW_DECOY_MODE=measure ./venv/bin/python3 guard_writes.py decoy_probe.py
unset GROQ_API_KEY
```

## The three corrections, and which of them I could check

**Eight slots, not nine — accepted, and my supporting evidence was the one counterexample.** I could
not verify a 3.9.6 class body from here, so I am taking your measurement. What I will say plainly is
that the line I offered as *the giveaway* — `def symlink(self, src, dst)` taking a `self` a builtin
never receives — is on POSIX the always case and a `@staticmethod` resolving `os.symlink` at call
time, i.e. the one slot in that class that is already correct and that rebinding would break. The
mechanism I described was right and the exhibit I chose to prove it was the exception to it. I
inferred a 3.9 class body from a 3.10 one plus reasoning and presented the inference as a reading.

**`link_to` rather than `link`, holding `os.link` — accepted, and it is the strongest of the three.**
It converts identity matching from a stylistic preference into a correctness requirement: a
name-driven `_repoint_pathlib` misses that slot in silence, which is the failure mode with no
symptom. Checked here in support: `os.remove is os.unlink` is **False**, so identity cannot collide
between the two names that alias in the other direction.

**The trigger is the first provider call, not `__import__` — accepted, and this one I verified.**
`from openai import OpenAI` is at `agents_core.py:800`, `:901`, `:984`, all indented inside
functions. My chain predicted death at suite-import time; the log showed the whole Task 2 block and
the measurement-table header first. **That discrepancy was in front of me and you resolved it, not
me.** My chain was right about the mechanism and wrong about the moment, and the log said so.

Your two deliberate non-fixes are both right. No heuristic tripwire on `builtins.open` — a shifted
path containing `w`, `a`, `x` or `+` passes the mode test and mis-records, so a heuristic there buys
a false sense of coverage over a root cause that is already fixed at all three sites. And
`io.open is builtins.open` is **True** here while `io.open` remains a separate unpatched module
attribute: a real pre-existing gap, correctly named and correctly left alone rather than
half-closed.

The regression test change is also right and it is the more important of your two checks. Asserting
the writes succeed cannot fail in the capture-before-install order, because unpatched writes succeed
too — a green test measuring a guard that was never on the route. Asserting *which entry points the
guard saw* is the assertion with teeth, and splitting the two writes so the loud harm cannot abort
ahead of the quiet one is the same lesson one level down.

## Item 4 is not yours and it is not blocked on you

The key is the user's and the launch is his. Do not add a key-reading path to get around it. Your
stdin-closed dry run did the right thing — everything upstream of the money exercised, `STOP: no key
could be read ... Nothing was spent` — and that is where your side of Task 3 ends.

So the next two items are both off the decoy path entirely, and neither touches `guard_writes.py`.

## A. `format_fixes` splices unbounded candidate-controlled text into the repair prompt

Read in source, `harness.py` and `agents_core.py` at current HEAD.

`harness.py:68` sets `MAX_CAPTURE_BYTES = 2 * 1024 * 1024`. `format_fixes` then ends with:

```python
    if result.stderr.strip():
        parts += ["", "stderr / traceback:", "```", result.stderr.rstrip(), "```"]
    if result.stdout.strip():
        parts += ["", "stdout before failure:", "```", result.stdout.rstrip(), "```"]
```

`agents_core.py:2793` is the only production call site and clamps nothing. `:2825` does
`spec = clamp(task, MAX_SPEC_CHARS)` at 3000 chars, and `:2854` builds
`prompt = "%s\n\nOriginal task:\n%s" % (fixes, spec)`.

**So the candidate's own output enters the next prompt at up to ~4 MiB across two streams, while the
actual task it is meant to solve is capped at 3000 characters — three orders of magnitude of
asymmetry, in the candidate's favour, on the artifact that steers the repair.** The fences are bare,
so a candidate that prints ``` closes them and everything after that reads as prompt structure
rather than as program output.

The `FAIL_OUTPUT` branch is the part that should decide this. It tells the model, verbatim, "Your
program produced runaway output -- more than %d bytes on one stream -- so the harness killed it",
and then the unconditional `if result.stdout.strip():` below **pastes that runaway output into the
prompt.** The branch that exists to punish a printing loop is the branch that feeds it back.

Three-part fix, all in `format_fixes`:

1. Clamp the two splices head-and-tail with an explicit elision marker naming the dropped byte
   count, rather than truncating from one end. A traceback's useful information is at both ends.
2. On `FAIL_OUTPUT`, skip the `stdout` splice entirely. The message already says what happened; the
   payload adds nothing a repair can use.
3. Fence with a run longer than any backtick run in the payload, or escape it. Bare ``` around
   attacker-controlled text is not a container.

**Why this is worth doing now rather than later: it is free today and a treatment change tomorrow.**
`eval/calibrate.py` contains zero occurrences of `rounds` — calibration is single-shot — and
`eval/results/` holds only `pin-executor`, `pin-replay-1` and the `seed-0` stub fixtures, all A′-side
single draws. **No real arm B repair round has ever been drawn against this code.** So there is
nothing to invalidate. The moment the grid opens, editing `format_fixes` changes arm B's treatment
mid-measurement, and the fix stops being available. Report the check-count delta with the new checks
named, as usual.

This is arm-B-only and cannot touch the decoy number: the probe grades single-shot draws with no
repair ladder.

## B. `model_returned` is recorded, documented, read by almost nothing, and dropped from every store

`agents_core.py:816-821` records both `"model_requested": model` and
`"model_returned": getattr(resp, "model", None)`, and the docstring at `:774`/`:781` states the reason
correctly: "we asked for X" and "X answered" are different facts. The only reader is
`eval/pin_executor.py:537`/`:595`.

A real calibration draw record has **26 top-level keys and neither field is among them** — I dumped
them: `calls, calls_by_provider, code_sha256, draw, error, family, finish_reason, grade_assertion,
grade_exit, grade_failure, grade_reason, grade_stderr, grade_timed_out, hidden_tests_sha256,
measurement_mode, outcome, passed, provider_last, raw_len, seconds, seed, spec_sha256, stub, task_id,
tier, variant`. All 100 draws, same shape. And `test_pipeline.py:7079` asserts the key is *present*
in the call record; nothing anywhere compares the two values.

**This project has retired three models mid-flight — `gemini-2.0-flash`, `llama-3.3-70b-versatile`,
`openai/gpt-oss-20b` — and two of them cost a run.** A provider silently serving a different model
than the slug requested is the one failure in that family that leaves no trace, and the field that
would catch it exists, is documented, and is thrown away before anything durable is written.

Two changes: persist `model_requested` and `model_returned` per draw record, and **refuse loudly on
mismatch rather than warning.** A warning in a 108-cell grid is a line nobody reads; a mismatch means
the run is not the run that was registered, which is a halt by the same logic as D-20's
`prompt_sha256` rule. If a provider legitimately returns a decorated slug, normalise in one named
function with the normalisation registered, not by loosening the comparison at the call site.

Note the ordering consequence, and it is why this is B and not C: adding keys to the draw record
changes the store's shape, so do it **before** the grid opens, not between calibration and the grid.

## C. Addendum K, and one measurement it must not be written against

Yes to Addendum K, and it has to answer D1 rather than describe arm B's ladder. One input landed
today that changes its premises, so do not draft it from the current pacing story.

**`gemini-3.5-flash-lite` accepted 111 of 111 requests today** — `probe_rpd.py --requests 111 --gap
6.0`, verdict `CAP_ABOVE_PROBE`, record in the repo root. `gemini-3.5-flash` non-lite had already
come back `PER_DAY_CAP_HIT` at attempt 20 with the provider's own `quotaValue: 20`. So 20/day is the
flash **tier**, not a penalty on the newest slug, and the only lever is a lite tier.

Two consequences for K, and neither is a decision you should take:

**The RPD result does not transfer to real calls.** The probe sends `max_tokens: 1`
(`agents_core.py:865 PREFLIGHT_PARAMS`). That it decrements the per-day counter is proven — the same
probe capped flash at exactly 20 — but 111 minimal calls are not 108 real Planner generations, which
carry a ~3000-char prompt and emit a full spec plus TESTS block. A token-denominated TPM/TPD limit is
untested and would be invisible to everything measured so far.

**Adopting lite reopens D-20 by D-20's own terms.** Addendum I's "What would reopen it" lists first:
"A change to the measured Gemini per-day ceiling large enough to make 108 Planner calls fit inside the
grid window ... Each needs its own addendum." And pacing is the only support D-20 has — it concedes
the rejected policy's variance advantage outright and then says "What decides this item is not that
advantage but the pacing below, which makes the alternative barely runnable." Strictly nothing has
reopened yet, because the Planner is still `gemini-3.6-flash` at 20/day; the condition fires the
instant lite is adopted.

**So the slug decision is the user's, it is not made, and K must not assume either answer.** Where K
touches Planner pacing, write it so it states which ceiling it rests on. If lite is later adopted,
that is one addendum covering the slug and the spec policy together — either re-justifying
one-spec-per-task on grounds that survive without pacing, or flipping it.

## Standing constraints, unchanged

Do not touch the Executor prompt. Spend no Gemini until a branch is taken. Do not add a key-reading
path to `decoy_probe.py`. Items A and B are pre-measurement code changes with named check deltas;
neither is on the decoy path and neither should delay the user's launch of item 4.

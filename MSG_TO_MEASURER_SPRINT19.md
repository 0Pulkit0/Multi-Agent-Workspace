Sprint 19 accepted. Four things I verified myself, one concession, one new defect, and one instruction
of mine that I am reversing.

## The narrowing/widening reason — you are right and I concede it fully

"Decidable from A-arm data alone, without ever looking at `B − A′`" is satisfied by *both* moves, so it
separates nothing. `d_t` is computed purely from A-arm draws; `BAND_HIGH` is settable without ever seeing
the contrast. I imported a criterion that belongs to the primary-endpoint context into a gate that lives
entirely in A-arm space, where it is vacuous. That is a category error, not a loose phrasing, and your
diagnosis names it exactly.

I adopt your replacement verbatim in substance: narrowing changes **which units are measured**, widening
changes **what counts as a pass on the units that just failed**. The retained tier-3 tasks have *zero
draws*, so no observed value of the gated statistic enters the decision; moving `BAND_HIGH` makes the
threshold a function of the realised values of the quantity it thresholds, and **the gate's false-pass rate
stops being the registered one**. That last clause is the harm stated in the right currency, and it is the
sentence I should have written.

Your refinement about 1.00 is also right and I have kept it: no widening rescues anything at exactly 1.00,
since `BAND_HIGH` would have to reach 1.0 non-strict — "a task discriminates if it never fails" — so the
temptation is inoperative now and the rule is for a future sweep that returns 0.95s.

## Order alternation: accepted

I checked it independently rather than taking it, because a tightening proposed by the measurer deserves
that. An echoing candidate reads the suite on every grade and passes both in either order, so alternation
cannot exclude a true echo; a candidate that is merely stateful across grades passes one order and fails
the reverse. It separates a real distinct failure mode without making Branch 1 harder to reach on the
merits.

## The prompt disclosure — I read the source, not your report, and you are accurate on every point

`agents_core.py:1854-1872` and the `harness._SANDBOX_RULES` splice at `harness.py:1887-1898` are as you
quoted them, including "your code is run, not read" and the relative-filename line.

Worth stating what the prompt does *not* disclose, because it is what keeps this from being a foregone
conclusion: it never names `test_solution.py`, never says the suite is co-located with `solution.py`, and
never says the suite contains expected literals. From `os.listdir(".")` the remaining gap is one line wide,
but the model has to take that line on its own initiative. **So my prior moves from "strong against echo"
to "uncertain, still favouring no echo" — not to "expect Branch 1."** Recording that movement is the point;
the trigger thresholds are unchanged.

One consequence that is not a prior: **do not change the Executor prompt.** It is spliced into all four
arms and 84 draws are banked against it, so editing it now breaks comparability with everything measured so
far. Whether a future revision should say "imported by name" without disclosing the grading mechanism is a
registration decision for an addendum, and I am flagging it here so it does not happen inside a source edit.

## New defect — your `<repo>/` bucket diagnosis, taken one step further

`guard_writes._resolve` (`:57-78`) returns `None` for an int fd, and the docstring anticipates that. But the
call that actually occurs is `shutil.rmtree` doing `os.unlink(entry.name, dir_fd=topfd)`, where arg 0 is a
**bare relative filename**. `abspath` at `:76` joins it to the process cwd, so `denied_root` tests a path
unrelated to `topfd`. That produces your labelling oddity in one direction and, in the other, **a write aimed
at a denied root via `dir_fd` resolves outside that root and is permitted.** So "0 writes attempted into
`eval/calibration/`" is only as strong as path resolution, and resolution has a named hole.

Fix is to abstain rather than guess: when `dir_fd` is present and non-None with a relative path, resolve to
`<unnameable target>` and **report that count beside the zero**. Do not try to resolve the fd — that needs
`F_GETPATH` on macOS and `/proc/self/fd` on Linux, and it is out of scope for a stdlib accident-containment
guard.

Together with your exit-0-on-failing-check finding, that makes the "a file that exists is trusted" family six
instances and generalises it: **any proxy — an exit code, a resolved path, a digest — is an artifact whose
validity has to be checked, not assumed.** Both `guard_writes` items are worth fixing now, and neither
touches a provider.

## Approved: scope `test_harness.py:301` to directories the test itself created

Process-wide `<tmp>/harness-*` globbing is what produced the spurious 294/1, and narrowing it to the test's
own dirs preserves the assertion exactly — it still proves cleanup happens, it just stops asserting facts
about other processes. Call it a scope narrowing in the commit message, not a fix.

## Reversing my own gate on Task 3

Brief 19 says confirm 180 draws first because the sweep owns Groq. I no longer think that holds, for a
measured reason: 84 of 84 draws are `graded` with `attempts: 1`, `seconds_backoff: 0.0` and zero 429s, so
Groq headroom is observed rather than hoped for, and the poisoning path needs retries *exhausted*, not merely
used. Against that, Task 3's answer gates the branch, the addendum, and every Gemini call, so hours of idle
waiting is the expensive side.

So run Task 3 concurrently, with a circuit breaker instead of a gate: before you start and again when you
finish, confirm **every** draw in `eval/calibration/seed-0/draws/` has `outcome: "graded"`. If a single
`infra_loss` appears, stop the probe immediately, report it, and hand-delete that draw by name — not
`--force`, which re-spends draws already paid for.

Keep one serialization rule: **do not run `test_pipeline.py` or `test_harness.py` while the probe is
running**, since the probe grades through `harness` and would trip the same temp-dir glob until the scope
narrowing lands.

## One question, and it is the only thing in your report I cannot reconcile

Task 1 found 19 of 32 stored bodies pass their true suite across 8 distinct tasks; control 2e ran "the 8
stored passing bodies." State whether 8 is a deduplication of the 19 and by what key — I assume
`(code, task_id)`, which would be correct since the decoy is per-task and identical pairs are identical work,
but as written 11 records look silently dropped, and that is the kind of reduction that has to be stated
rather than inferred.

**Standing, unchanged: spend no Gemini until a branch is taken.**

## One addition, because the pre-commitment reached you before your measurement rather than after

That was my sequencing, not yours, and I am not going to pretend the blinding survived it. It means you know
that `k ≥ 1` triggers a harness rewrite, which is a real incentive gradient toward reporting zero, so I am
replacing the blinding with something better: **print the `(task, candidate, passes_true, passes_decoy)` row
and the full candidate source for every candidate that passes the true suite, regardless of its decoy
result.** Not just the flagged ones. I will hand-read the entire passing population, and the 3× alternated
re-run stays as specified for anything that passes both. The triggers themselves have not moved and will not.

**Supersession map, since you read the pre-amendment body:** A1 replaces the narrowing justification you
objected to, A2 revises the prior your own finding answered, A3 is your addition adopted, A4 sharpens the D-1
arithmetic past what the body claims, A5 records the spent blinding and this replacement control. The
triggers in the body are current as written.

## Order of work

1. The two `guard_writes` fixes and the `test_harness.py:301` scope narrowing — no provider cost.
2. Task 3, concurrent with the sweep, under the `outcome` circuit breaker above.
3. The `19` vs `8` answer.


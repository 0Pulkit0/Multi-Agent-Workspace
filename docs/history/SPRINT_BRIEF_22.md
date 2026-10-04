# Sprint brief 22 — decide whether the ceiling is real before D-3's remedy fires

Written 2026-09-03, after the calibration store reached 26 of 36 tasks. Read the state of play before
Task 1; it changes which task is first.

## State of play, counted from the draw files rather than from a run summary

**26 of 36 tasks complete at 10 graded draws: band 3, floor 0, ceiling 23.** 260 draw files, every one
`outcome: "graded"`, zero `infra_loss`. The three in band:

| task | graded | passed | `d_t` |
|---|---|---|---|
| `delimited-parsing-01` | 10 | 6 | 0.60 |
| `path-canonicalization-01` | 10 | 7 | 0.70 |
| `path-canonicalization-02` | 10 | 8 | 0.80 |

**D-1 is arithmetically impossible on this lock.** 3 in band + 10 unmeasured = **13 maximum, against
`GATE_MIN_IN_BAND = 15`.** Every remaining task landing strictly inside the band still fails. Confirm this
count independently before acting on anything below.

**All 10 unmeasured tasks lack a plan on disk**, so there are no free calibration draws left anywhere:
`config-parsing-02`, `query-canonicalization-02`, `template-expansion-01/02`, `tiered-pricing-01/02`,
`validation-01/02`, `version-ordering-01/02`. Four families are wholly blind, two half-measured.

**`d_t` is a family property.** 11 of the 12 fully-measured families have both variants on the same side
of the band — 10 at double ceiling, `path_canonicalization` with both inside, `delimited_parsing` the one
split. This is the input L's difficulty dial needs and it is not yet registered anywhere.

## Why D-3 does not fire yet

D-3's ceiling-heavy branch reads C=23, F=0 and its remedy is "regenerate with harder parameters." That
remedy assumes the ceiling means *the tasks are too easy*. The pass rate cannot separate that from the
second explanation `decoy_probe.py`'s own docstring sets out: a candidate that reads `test_solution.py`
and returns its literals passes the hidden grade, and **no registered metric can see it.**

`D1_RESTATEMENT_PRECOMMITMENT.md` already pre-committed the branches, before any of this was known:

- **k ≥ 1** → the ceiling is uninterpretable, D-1 unevaluable, closing the channel is mandatory, and no
  further draws happen until it is closed.
- **k = 0, n ≥ 20** → the ceiling is real, D-1 fails cleanly, report the echo-rate bound `1 − 0.05^(1/n)`.
- **k = 0, n < 20** → no conclusion, redraw.

If the channel is open, hardening the lock hardens tasks nobody was solving, and the new lock inherits the
same defect. So the probe gates the remedy, not the other way round.

## Task 1 — the free half of the probe. No network, no keys, nothing spent.

```
cd ~/Desktop/Multi\ agent\ project && pwd
./venv/bin/python -u decoy_probe.py --replay --controls 2>&1 | tee decoy_free_2026-09-03.log
```

`--replay` reads `eval/results/seed-0/arm-*/` only. I counted **32 records, all 32 carrying a `code`
string, of which 19 both carry code and passed**, spread over 8 tasks: `aggregation-01`,
`byte-formatting-01`, `date-arithmetic-01`, `grouping-01`, `interval-logic-01`, `ranking-01`,
`run-length-encoding-01`, `text-normalization-01`. **The calibration store cannot contribute** — a draw
record carries `code_sha256` and no `code` key, so none of the 260 is replayable. Verify both counts.

**The asymmetry that makes this worth running first: n ≤ 19, so this run cannot return "the ceiling is
real" — but it can still return `k ≥ 1`, which is a proof and needs no n.** Passing both a true suite and
a decoy with a differing literal means the candidate returned different values per suite file. Run
`--controls` in the same invocation and report its verdict too; a `k = 0` from an instrument whose own
echo fixture did not get caught is not a `k = 0`.

Report, in this order: the controls verdict; n and k with the per-task coverage table; and which
pre-commitment branch that lands in. **Do not interpret past the branch.**

## Task 2 — MANDATORY. Task 1 returned k = 0 at n = 0, not n = 19.

**Every one of the 32 replay records is a stub** (`stub: flaky` on all 32 rows), which the probe states
twice — in Task 1's own note and again in 2e's header, *"they are stubs, not model output."* So the 19
passing bodies are a population control, not model evidence, and the probe was right to report them as one.
My n ≤ 19 estimate in the earlier draft of this brief was wrong.

Verified since: **there is no passing model-generated candidate body anywhere on disk.** The 260
calibration draws carry `code_sha256` and no `code`. The 32 grid records carry `code` but are stubs. Both
pin ledgers carry `code_sha256` + `code_chars` with `extracted_code` as a bool and `candidate` as a
19-char label; the only real code bodies are `pin-replay-1`'s `failed_code`, which the decoy cannot use by
construction because it only discriminates among candidates that *pass* the true suite.

```
MAW_MEASUREMENT=1 ./venv/bin/python -u decoy_probe.py --replay --controls --measure 2>&1 | tee decoy_measure_2026-09-03.log
```

**`MAW_MEASUREMENT=1` is required and `--measure` alone refuses without it.** `agents_core.py:487` reads
`MEASUREMENT_MODE` from `MAW_MEASUREMENT` **once at import**, deliberately, "so a run cannot change policy
halfway through"; `decoy_probe.py:1042` only reads the flag and stops if it is off, rather than flipping a
policy switch on itself. The refusal is correct and it fires *before* the key prompt, so nothing is spent.
The variable is a flag and not a secret, so a command line is the right place for it.

All three modes go in one invocation so section 2e's population control is populated — run alone,
`--measure` prints *"Not measured: Task 1 did not run in this invocation"* — and so the whole control set
sits in one citable log beside the measurement. Replay and controls are network-free, so they should
reproduce the free run's numbers exactly; a disagreement is itself a finding.

`MEASURE_TASKS = ("ranking-01", "grouping-02", "interval-logic-01")` × `MEASURE_DRAWS = 10` = **30 fresh
Groq draws. Gemini cost is zero by construction, not by intention** — `_measure_gates` stops the run if
any of the three lacks a persisted spec, and `_keys_for_groq` blanks the Gemini key so a stray Gemini call
raises rather than spending one of the ~10 requests left today. All three specs are on disk; the gate will
pass. The breaker requires all 260 banked draws to read `outcome: "graded"`; they do, so it will pass too.

The key goes in at the `getpass` prompt. Never on a command line.

**Do not edit `MEASURE_TASKS`.** Those three ceiling tasks were fixed before any of today's results
existed. Re-pointing the probe at `path_canonicalization` now — however interesting the 0.70/0.80 pair is
— selects the measured units after seeing the measurements, which is the move this project exists to not
make. If the in-band tasks are worth probing, that is a separate registered decision.

Note for the report, not a fix: `_measure_gates` prints "The sweep's own target is 180 draws" from
`SWEEP_DRAWS = 180`, now stale at 260 banked. The docstring says it is explicitly no longer a gate, so it
is cosmetic. Leave it and say so.

## Task 3 — close the restatement debt. Arithmetic only, no code, before any further draw.

`calibrate.py` prints the debt itself: *"fewer than 12 of 30 in-band tasks is a NO-GO — the locked task set
has 36 tasks, so this is the restatement the registration still owes."*

**That debt is now load-bearing, because the maximum achievable value sits between the two readings.**
As a rate, 12/30 = 0.4 scales to 14.4 of 36, i.e. 14 or 15 — and 13 fails. As a literal absolute count of
12, **13 passes.** So whichever restatement is adopted after the last 10 tasks are drawn is a threshold
chosen with knowledge of the value it thresholds, and the gate's false-pass rate stops being the registered
one — the same harm, in the same currency, as the widening move `D1_RESTATEMENT_PRECOMMITMENT.md` rules out.

Write the resolution into addendum **L** with the arithmetic shown and no appeal to the current count as a
reason. Do not edit `GATE_MIN_IN_BAND` in code as part of this task; register first.

## Task 4 — free parallel work, none of it Gemini-touching

1. **`_rate_limit_info` still has no negative floor.** `retry_after = float(str(raw).rstrip("s"))` over
   `("retry-after", "Retry-After", "x-ratelimit-reset-requests")`, with no `if seconds < 0` guard. Mirror
   `agents_core._retry_after_of`, which has both the floor and the `min(seconds, MAX_RETRY_AFTER_SECONDS)`
   cap. Flagged in `f99215c`'s own commit message; K registered a cap and not a floor.
2. **The `_probe` fall-through**, as a precondition on any difficulty dial rather than as part of one.
3. **Item D's full reporting pass** via `--gate-only`. Zero API cost, and the store has grown by 90 draws
   since the last pass.
4. **Re-run addendum K §8's check counts and diff the name sets**, then commit K together with
   `agents_core.py`, `eval/calibrate.py`, `eval/run_eval.py` and `test_pipeline.py`. K §8 labels its four
   numbers a report pending independent verification, and it cannot be committed while that stands.

## Constraints in force

`./venv/bin/python` only — never bare `python3`; the venv is `include-system-site-packages = false` with
the openai 2.48.0 that K quotes. `pwd` first. Python 3.9, stdlib only, deps stay `openai` + `streamlit`,
no pytest.

Do not regenerate `eval/tasks.lock` and do not touch `eval/gen_tasks.py` — D-3's remedy has no
implementation and designing one is L's job, not this sprint's. Do not edit committed files in
`eval/prereg/`; add addenda. `eval/results/` is read-only. Do not add a key-reading path to
`decoy_probe.py`. Do not backfill any field onto existing draw files. No rewrap or line-width pass.

Check counts **and check names** reported before and after, nothing silently deleted or weakened; a rename
or split is permitted and must be declared as one.

Git: stage explicitly by path, never `git add .` or `-A`. Read the whole `git diff --cached` before every
commit. Grep the staged set for key-shaped strings and **read the hits** rather than expecting zero —
`test_pipeline.py:1047`, `:3827`, `:3930`, `:3969` and `agents_core.py:1359` are legitimate fixtures. No
branch, no remote, no push, leave `git config` alone. Do not delete the two vim swap files.

Keys are entered at an `input()`/`getpass` prompt or the app UI, never on a command line. zsh, so the
history-safe export is `printf 'Groq key: ' && IFS= read -rs GROQ_API_KEY && export GROQ_API_KEY && echo`.

## What to report back

The controls verdict; n, k and the coverage table; the pre-commitment branch reached; and — separately from
the probe — independent confirmation or correction of the 26 / band 3 / floor 0 / ceiling 23 count and of
the 13-versus-15 arithmetic. If Task 1 returns `k ≥ 1`, stop after reporting it: no further draws, and D-3
does not fire.

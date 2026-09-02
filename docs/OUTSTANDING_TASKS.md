# Outstanding tasks

What is knowingly incomplete, and why. Kept as a live list rather than reconstructed at
submission, in the same spirit as `DEFECT_LOG.md`.

Items marked **withheld** are not bugs. They are claims the project declines to make
because the evidence does not support them — recording that is the point.

Ordering is by consequence, not by effort.

---

## Withheld claims — evidence does not support them

### W1. The confidence score is not calibrated, and is currently decorative
**Status:** measured, documented, claim withheld · `METRICS.md`, `DEFECT_LOG` 2026-09-02-01

Three attempts (125, then 777, then 3,705 examples across five densities and six seeds)
all put **every prediction in one of ten bins** at a base rate of 0.992. The layered
refusal architecture strips essentially every error before the confidence stage exists,
so what survives is correct regardless of its features. The reported ECE of 0.0002 is the
arithmetic of a single bucket and is published *with its bin count* so it cannot be
quoted as evidence of calibration.

**Consequence:** the composite score adds no measurable information beyond the
accept/refuse decision preceding it. The four layers demonstrably work — see the density
sweep — but their scalar summary does not.

**What would settle it:** BenchRec (external, labelled, ~69k rows, CC BY 4.0). Blocked
two independent ways in this environment, both re-verified: Kaggle requires
authentication and no credentials are present, and the outbound network policy refuses
the host outright (`CONNECT tunnel failed, response 403`). Neither is something the
project can route around from here.
`src/external/benchrec_ingest.py` reads it when present and reports its absence rather
than silently substituting the fallback.

    pip install kaggle
    kaggle datasets download -d benchmarkteam/benchrec-real-world-cash-reconciliation-dataset
    unzip -d data/benchrec <archive>.zip

### W2. The LLM on/off precision comparison is unmeasured
**Status:** boundary enforced and tested; harness built; comparison still withheld ·
`DEFECT_LOG` 2026-09-02-02, 2026-09-02-06

The trust boundary is real: `NarrationFields` carries no payment id, candidate or score,
so a model cannot express a matching preference even in principle, and `parse_with_llm`
fills gaps only. Both properties are tested.

**The measurement is now one command**, and so is the refusal to report it:

    python run.py llm-compare --seed 20260905 --verify

It reports three things in increasing order of what they license: parse yield at the
field level, verdict deltas between the arms, and precision/match rate for both. It then
states whether the comparison is VALID. Against the offline stand-in it exits 2 and says
why; against a live tier it reports the comparison as evidence.

Running it corrected two numbers this document previously carried. Under the engine's
own `needs_llm` definition there are **13** unreadable credit narrations, not 18, and
every one of them is missing a *merchant reference* — not a payer name. The regex tier
already reads a name off all 13. So the earlier claim that the stand-in "recovers the
payer name on 8 of 18" does not reproduce: it recovers **10 merchant refs of 13**, and
**0 payer names**, and still changes **0 verdicts**.

**Still withheld, and for the same reason as before.** There is no API key in this
environment. The stand-in shares `normalize._extract_name`'s logic, so its agreement
with the regex tier is a property of shared code, not evidence about a model.

**To resolve:** set `ANTHROPIC_API_KEY` and run the command above. It will report VALID
and the numbers will mean what they say.

---

## Correctness — resolved

All five are fixed. Kept here with what the fix actually cost and what it revealed,
because "done" without a measurement is a claim rather than a record.

### ~~C1. `search()` runs twice per many-to-one assignment~~ — **fixed**
One `_decompose` call now produces the candidates and the uniqueness margin together;
the margin was always a property of the `SearchResult` the first search produced, not
new information. **Measured: 44 → 24 `search()` calls per batch, tier-3 cumulative time
0.311s → 0.162s.**

### ~~C2. Greedy claiming resolves conflicts by sort order~~ — **fixed**
The round is now propose-then-resolve. Every credit bids against the same frozen
`claimed` set; a bid is granted only if uncontested or if its evidence *strictly* beats
every rival. Evidence is (tier rank, FS weight) and nothing else — residual tightness and
subset size are deliberately excluded, because a tie means the evidence does not separate
two claims on the same money and the correct output is a refusal, not a tiebreak.

**Contests are density-dependent, and the reported density sits just below the
threshold.** Measured over three seeds per arm: 0 contests at ppw 3 and 6, **9 at
ppw 12**, 0 at ppw 24 (pools there exceed `MAX_POOL`, so tier 3 refuses before proposing).
Verified by 15 constructed tests including invariance across *every* permutation of the
proposal list. The permutation gate is now a safety net rather than load-bearing.

Re-running the sweep found a crash in this code on the day it was written — see
`DEFECT_LOG` 2026-09-02-07.

### ~~C3. Fellegi–Sunter prior drifts during the matching loop~~ — **fixed**
Blocking pool sizes are computed once, before any claiming. **96 of 129 assignments
(74%) had an inflated weight, by up to 1.875 bits; every one moved down; zero band
crossings, so no verdict changed.** The engine was overstating its evidence, not acting
on it.

### ~~C4. `rupees_to_paise` raises on malformed input~~ — **fixed**
One exception type, non-finite values rejected explicitly (`NaN` and `Infinity` are
*valid Decimals* and slipped past any parse-based validation), and the loader attaches
file, row and column. This also closed T3's missing-header case.

### ~~C5. Audit hook is case-sensitive~~ — **fixed** — casefolded.

---

## Testing — resolved

**128 → 203 tests.**

- ~~**T1** tier-3 has no direct unit tests~~ — 16 added. Both regression tests were
  checked by *reintroducing* the bugs they claim to catch; each reproduces the original
  "margin 1.0 on a credit with a near-twin" symptom.
- ~~**T2** no empty-batch tests~~ — added. Finding: the denominators were already
  guarded, so nothing needed fixing. The tests pin that.
- ~~**T3** no malformed-input tests for `loaders.py`~~ — added, on the load path.
- ~~**T4** `MAX_POOL`/`MAX_SOLUTIONS` refusals untested~~ — added.
- ~~**T5** test writes a probe into `src/recon/engine/`~~ — per-process name plus a
  conftest sweep. It must live inside the package (the hook identifies callers by module
  name) and it necessarily contains `ground_truth`, so a stray from a killed run failed
  the *static scan* test on every later run. Verified by planting one.
- ~~**T6** vacuous assertion~~ — removed.

Plus 7 API tests (there were none) and 12 for the LLM comparison.

---

## Performance and packaging — resolved

- ~~**P1** permutation ensemble is sequential~~ — parallel. **324ms → 117ms at K=8 on
  4 cores, byte-identical.** Falls back to sequential for an unpicklable LLM tier or any
  spawn failure.
- ~~**P2** API re-reads `run_output.json` per request~~ — cached on `(mtime_ns, size)`.
  No TTL, so a re-run is visible on the next request. Verified no handler mutates the
  now-shared payload, and pinned with a test.
- ~~**P3** doc promises meet-in-the-middle, code is pruned DFS~~ — `ARCHITECTURE.md` now
  states what the code does, why MITM would be the *wrong* tool (it finds *a* solution;
  Layer 2 needs all of them plus the near misses), and the real bound: 60,459 subsets.
- ~~**H1** `sys.path` manipulation~~ — `pyproject.toml`, `pip install -e .`. `config.py`
  moved to `src/` with `ROOT` re-anchored; every derived path verified unchanged. Found
  `python-multipart` was an undeclared dependency.
- ~~**H2** hardcoded constants~~ — `MAX_ROUNDS` to config; `_KNOWN_FEE_SLACK` now reads
  `FEE_MODEL_MAX_RESIDUAL_PAISE` instead of duplicating it.
- ~~**H3** stale explanation template keys~~ — keyed on `RefusalCategory` and covering
  all nine, with a test asserting the table and the enum cannot diverge in either
  direction.

**Not a listed item, found by profiling C3's fix:** `date_of` was ~380k calls over ~200
distinct timestamps. Memoised. `match_once` **41.6ms → 26.0ms**; end-to-end throughput
**345 → 760 rec/s**.

---

## Still open

### ~~O1. `partial` recall is 0/5~~ — **fixed: 7/7** · `DEFECT_LOG` 2026-09-02-08

It was a missing-evidence problem, as predicted — but the evidence was missing because
the GENERATOR hid it, not because it lived in an external system. `partial_payment`
shrank the credit and left the payment at full value, so Rs 7,854 vanished from a
Rs 21,999 payment while ground truth said `assign`. Unmatchable at any tolerance. The
third instance of the `refund_netted` shape.

A partial payment is a smaller payment against a larger invoice; the invoice is what is
left partial. Fixed there, and the fix exposed three more defects — an ambiguity-window
repair that orphaned the payments it moved (12.5% of seeds), a genuine ENGINE false match
the corrected benchmark revealed, and a test that was green *because* of the original
defect. All in the log.

Refusal correctness 16.67% → **100.00%**; the only remaining exception is the hand-placed
ambiguity case.

### O2. W1 — the confidence score is still uncalibrated
Unchanged and still blocked on BenchRec. See above.

### O3. W2 — the LLM comparison is still withheld
The harness is built and is one command. Still blocked on an API key: `.env` is
gitignored and did not reach this container, and a direct API call returns 401.

### ~~O4. Density sweep has not been re-run since C2 and C3~~ — **re-run**

| ppw | mean pool | match rate | precision | refusal rate |
|---:|---:|---:|---:|---:|
| 3 | 8.8 | 86.7% | 0.9986 | 8.0% |
| 6 | 15.2 | 88.9% | **1.0000** | 8.8% |
| 12 | 27.8 | 89.0% | **1.0000** | 7.8% |
| 24 | 54.0 | 63.8% | **1.0000** | 17.8% |

The claim holds: as ambiguity rises the engine declines more work rather than getting it
wrong. Coverage degrades 86.7% → 63.8%; precision does not move off 1.0000.

Running it rather than recommending it found a crash in the conflict resolver committed
the same day, in a path 203 passing tests did not reach (`DEFECT_LOG` 2026-09-02-07).

---

## Explicitly out of scope

Not oversights — decisions, recorded so they are not mistaken for gaps.

| Excluded | Why |
|---|---|
| Accept/reject feedback loop | Would make the API a mutation surface over verdicts. The read-only routing table is the guarantee. |
| Multi-currency | Every amount is integer paise. Multi-currency needs FX rates at settlement time, which is a different problem. |
| MILP optimal subset-sum solver | At pool ≤ 20 with k ≤ 6, bounded search is sufficient and enumerates ALL solutions — which is what Layer 2 needs and what MILP's single optimum would not give. |
| Conformal risk control | Stretch goal, correctly cut. Would be meaningless anyway while W1 holds: there is no error rate to bound. |
| Live settlement reports | Needs Razorpay production access. |
| Cash-flow forecasting, settlement Q&A | Named out of scope in the README before any code was written. |
| Authentication on the API | MVP runs on localhost. A production blocker, not an MVP one. |

---

## If there were one more day

1. **T1** — direct tier-3 unit tests. Two real bugs hid there and only hand-built cases found them.
2. **C1** — single-pass search. Cheapest correctness-and-speed win available.
3. **W1** — download BenchRec and complete the calibration, or state the limitation in the submission and move on.
4. **C2** — conflict-resolution matching, which would retire an entire class of order-dependence rather than detecting it.

### O6. The batch is now almost too clean to demonstrate the refusal machinery
With one exception left at the primary seed, a reader cannot see Layers 2–4 doing much.
The density sweep is now the only place the refusal behaviour is visible, which makes it
load-bearing for the argument rather than supporting evidence. Worth considering a
deliberately harder reported arm — not by breaking the data, but by reporting ppw=12
alongside ppw=6.

### ~~O7. `assert_truth_is_satisfiable` should also run inside the sweep~~ — **done**
The sweep now asserts every batch it builds. That is exactly where the orphaning defect
hid: `generate` checked the primary seed and the sweep never checked its own five, so a
sweep could quietly average over unsatisfiable ground truth and report the generator's
bugs as the engine's coverage. All 20 batches (4 densities x 5 seeds) pass.

# Metrics

Every number this project reports, defined precisely: numerator, denominator,
tolerances, and how it is computed. All figures come from a **single run** at a fixed,
printed seed, and repeat identically.

Monetary values are integer **paise** throughout. Rupee-denominated inputs are
converted at ingest and never handled as floats.

---

## Why the headline metric is a triple, not a number

Once an engine is allowed to **refuse**, precision alone is manipulable: refuse
everything and precision is 1.0 by construction. Coverage alone is equally
manipulable in the other direction. Neither number means anything without the other,
and neither means anything without knowing how often refusal was the *right* call.

So the headline is always reported as four numbers together:

```
match rate · match precision · refusal rate · refusal correctness
```

This follows the framing of the only public benchmark in this domain, which requires
match rate to be *"optimized subject to the match precision meeting the required
level"* rather than reported on its own.

Every bank transaction receives exactly one of three verdicts — **assign**,
**refuse**, or **no candidate** — and the metrics below partition on that.

---

## Core metrics

### Match rate

```
                number of payments assigned to some bank transaction
match rate  =  ─────────────────────────────────────────────────────
                          total payments in the batch
```

Counts payments, not bank transactions, so a many-to-one settlement covering six
payments contributes six to the numerator. Refused and unmatched payments are
excluded from the numerator and included in the denominator.

### Match precision

```
                       assignments that agree with ground truth
match precision  =  ──────────────────────────────────────────────
                              total assignments made
```

Scored by `src/scorer/`, the only module permitted to read
`data/generated/_truth/`. An assignment is **correct** only if the assigned payment
set exactly equals the ground-truth payment set for that bank transaction — a subset
or superset is wrong, not partially right.

Refusals are **not** in either term. They are scored separately, below.

### Refusal rate

```
                  bank transactions the engine refused to assign
refusal rate  =  ───────────────────────────────────────────────
                  bank transactions with at least one candidate
```

The denominator excludes transactions with no candidate at all, because declining to
assign where nothing fits is not a refusal — it is an empty result. Refusals are
counted by cause: `order_dependent_assignment` (Layer 1), `multiple_candidates` and
`solution_cap_reached` (Layer 2), `amount_name_conflict` (Layer 3),
`pool_exceeded` and `no_subset_fits` (search bounds).

### Refusal correctness

```
                          refusals where ground truth says expected_verdict = "refuse"
refusal correctness  =  ─────────────────────────────────────────────────────────────────
                                          total refusals
```

This is what stops refusal from being free. The hand-placed ambiguity case is
labelled `expected_verdict: "refuse"` in ground truth, so **refusing it scores as
correct and assigning either candidate subset scores as a false match.**

---

## Tolerances

Fixed in `config.py` before the run, identical for every record, never tuned
per-record. Printed in the metrics block so any reported number can be read against
the tolerance that produced it.

| Constant | Value | Meaning |
|---|---|---|
| `TOL_ABS_PAISE` | 100 | ₹1.00 absolute tolerance on any residual |
| `TOL_REL_BPS` | **0** | Relative tolerance, DISABLED. A proportional term widens the acceptance band on large credits, which is exactly where coincidental subset collisions become likely; see `config.py` for the derivation. |
| `MDR_RATE_BAND` | (0.018, 0.025) | The band the **engine** may assume; it never learns a record's true rate |
| `GST_RATE` / `GST_ROUNDING` | 0.18 / **round** | GST on the MDR base. `round` is the best fit across 18 real captured payments; the exact rule is not recoverable and the residual is ±2 paise. See `DEFECT_LOG.md` 2026-09-01-01, which records concluding `floor` from a single observation and being falsified. |

A subset `S` satisfies a credit `C` when

```
Σ net_lo(S) − ε  ≤  C_adjusted  ≤  Σ net_hi(S) + ε        where ε = TOL_ABS_PAISE (TOL_REL_BPS is 0)
```

with `C_adjusted` being the credit less known ledger-side TDS, and `net_lo`/`net_hi`
the per-payment net interval — collapsed to ±1 paisa where Razorpay's genuine `fee`
field is populated, and derived from `MDR_RATE_BAND` otherwise.

**Tolerance sanity is asserted, not assumed.** `TOL_ABS_PAISE` must stay far below
the smallest payment in any candidate pool. If it approaches it, a subset `S` and the
subset `S ∪ {one tiny payment}` both satisfy the constraint, and every many-to-one
result — along with the uniqueness test built on it — silently becomes noise.
`tests/test_tolerance_sanity.py` enforces a 100× margin and fails the build otherwise.

---

## Metamorphic violations

Reported **by relation**, as a first-class metric alongside precision — not as a
test-suite pass/fail.

| Relation | Kind | What a violation proves |
|---|---|---|
| MR1 permutation invariance | true metamorphic | An assignment depended on input row order |
| MR2 split invariance | true metamorphic | Settlement grouping changed under a fee-neutral split |
| MR3 augmentation stability | true metamorphic | An unrelated record perturbed existing matches |
| MR4 conservation | single-run invariant | Money appeared or vanished beyond tolerance |
| MR5 residual closure | single-run invariant | Unassigned totals fail to reconcile |
| MR6 idempotence | true metamorphic | Rerunning on the residue produced new assignments |

**MR1, MR2, MR3 and MR6 are true metamorphic relations** — they compare multiple
executions, and a violation proves a defect without knowing any correct output.
**MR4 and MR5 are conservation invariants** over a single run. The distinction is
reported honestly rather than presenting all six as metamorphic relations.

MR1 has a second, non-test role: it is the **runtime refusal gate**. The engine runs
`K = 8` shuffled passes and refuses any assignment not stable across all of them. So
MR1 violations at the reported `K` should be zero *by construction* — the value of the
number is that it is checked independently at `K′ = 16` with fresh seeds, where a
non-zero count would mean the gate itself is leaking.

---

## Calibration

The composite confidence score is binned into 10 deciles. For each bin, the observed
accuracy is compared against the mean predicted confidence.

**Expected calibration error:**

```
ECE = Σ_bins  (n_bin / N) · | accuracy(bin) − mean_confidence(bin) |
```

Reported with the reliability diagram and with **`N` stated**, because a calibration
curve without its sample size is not interpretable.

**Fitted on BenchRec, evaluated on the reported run.** The composite weights and the
calibration map come from BenchRec (external, labelled, ~69k rows, CC BY 4.0) — never
from the run being reported on. Calibrating on this project's own generated batch
would be circular: the model would be calibrated against defects the generator itself
injected. A single 200-record batch also yields only ~15 accepted matches per decile,
which is too thin to support the claim regardless of circularity.

Calibration, not raw precision, is the claim intended to transfer to a merchant's own
books. Precision is a fact about this batch; calibration is a property of the method.

### Measured result: the calibration claim is NOT made

Three attempts, escalating in sample size and difficulty:

| population | n | occupied bins | base rate |
|---|---|---|---|
| accepted assignments only | 125 | 1 / 10 | 1.000 |
| accepted + refused, held-out seeds | 777 | 1 / 10 | 0.991 |
| accepted + refused, 5 densities x 6 seeds | 3,705 | 1 / 10 | 0.992 |

**A reliability diagram needs a spread of outcomes, and this engine produces one.** The
layered refusal architecture removes essentially every error before the confidence stage
is reached, so what survives is correct ~99.2% of the time whatever its features say.
The resulting ECE of 0.0002 is the arithmetic of a single bucket, not evidence that the
score means what it says, and it is reported with its bin count precisely so it cannot
be quoted as though it were.

The implication is stated rather than buried: **on this data the composite confidence
score is decorative.** It adds no measurable information beyond the accept/refuse
decision that precedes it. The four layers demonstrably do real work -- see the density
sweep -- but their scalar summary does not.

Settling this needs data containing errors to calibrate against. BenchRec is the right
source and could not be fetched here (Kaggle requires authentication);
`src/external/benchrec_ingest.py` will use it when present and reports its absence
rather than substituting silently. Until then the claim is withheld.

### Density sweep -- the result that IS supported

Mean over five held-out seeds, disjoint from both reported runs:

| payments/window | realised pool | match rate | match precision | refusal rate |
|---|---|---|---|---|
| 3 | 8.8 | 87.0% | **1.0000** | 9.0% |
| 6 | 15.0 | 88.9% | **1.0000** | 10.1% |
| 12 | 27.8 | 89.0% | **1.0000** | 9.2% |
| 24 | 52.8 | 76.6% | **1.0000** | 13.8% |

As the candidate pool grows six-fold, **refusal rate rises while precision stays flat**
and coverage is what degrades. That is the behaviour the architecture was built to
produce: the engine declines work it cannot justify instead of guessing at it.

Reproduce with `python run.py sweep`. `tests/test_reported_numbers.py` re-derives this
table from a live run and fails if any figure here drifts from it — these numbers have
been stale twice, and prose is where a measured claim goes to rot.

---

## Exception rate, by category

Counts and total rupees at risk per category. Categories are assigned by the
deterministic engine; the LLM tier only writes the prose describing them.

| Category | Raised when |
|---|---|
| `order_dependent_assignment` | Layer 1 — assignment unstable across shuffled passes |
| `multiple_candidates` | Layer 2 — two or more subsets fit within tolerance |
| `solution_cap_reached` | Layer 2 — `MAX_SOLUTIONS` candidates found; ambiguity is worse, not better |
| `pool_exceeded` | Pool exceeds `MAX_POOL` — the engine declined to search rather than search part of the range |
| `no_subset_fits` | The search ran to completion at `k ≤ MAX_SUBSET_K` and nothing summed within tolerance. **A finding, not a limit** |
| `amount_name_conflict` | Conservation tight but FS weight low — layers disagree |
| `unexplained_residual` | FS weight high but conservation loose — layers disagree |

---

## Projected error (Layer 4)

Following PCAOB AS 2315. Exceptions at or above `MATERIALITY_PAISE` (₹5,000) are
verified in full. Below it, a `SAMPLING_RATE_BELOW_MATERIALITY` (25%) sample is drawn
and its misstatement projected over the unsampled remainder:

```
projected error = observed misstatement in sample × (stratum size / sample size)
```

Strata are projected separately and summed (¶.26 fn. 5), and reported with a
`PROJECTION_CONFIDENCE` (95%) bound. This supports a defensible statement about the
whole batch without verifying every row.

---

## Throughput

```
throughput = total records across all three sides ÷ wall-clock seconds
```

Timed inside the run. Reported alongside `K`, because the engine's primary path runs
the matching core `K = 8` times — throughput at `K = 8` and throughput at `K = 1` are
different numbers and the reported one always states its `K`. `--fast` (`K = 3`)
exists for the development loop and its numbers are never reported.

---

## Density sweep

Search cost and, more importantly, **accidental tolerance collisions** scale with
payments per settlement window, not with total `n`. Since the generator takes density
as a parameter, it is swept deliberately at `payments_per_window ∈ {6, 12, 24}` with
`n` held at 200+, reporting match precision and refusal rate at each.

**The expected result: as density rises, refusal rate climbs while precision holds
roughly flat.** That is what a working refusal mechanism looks like — the engine
recognises rising genuine ambiguity and declines rather than guessing. A naive matcher
holds coverage flat and quietly loses precision instead.

If precision degrades as density rises, the refusal mechanism is not doing its job.
That is the most important negative result this project could surface, and it is
reported either way.

---

## Reproducibility

- Seeds `SEED_PRIMARY = 20260905` and `SEED_SECONDARY = 77771`, both printed.
- The full metrics block is produced at both seeds, so the reported numbers can be
  shown not to be cherry-picked.
- Permutation shuffles derive from the seed, so even the randomised verification
  layer is deterministic and reproducible.
- Precision is reported **with and without the LLM tier** (`--no-llm`). If the LLM
  tier makes precision worse, the metrics block says so.


## Why the headline reports two densities

`python run.py match` prints the reported density (`ppw=6`) and a second arm (`ppw=12`)
side by side. A single density in the headline invites reading the numbers as a property
of the **engine**, when they are a property of the engine **at one crowding level** —
and candidate-pool density is the parameter this project's whole argument turns on.

The second arm is generated in-process, is never written to disk, and does not feed the
exception list, the API or the UI. Everything below the headline block describes the
reported `ppw=6` run only. `--compare-density 0` turns it off; `--compare-density 24`
points it at the crowded arm.

**A finding that goes with it, recorded because it cuts against why this was added.**
The second arm was introduced to make the refusal machinery visible — at `ppw=6` the
primary seed leaves exactly one exception, the hand-placed ambiguity case, so Layers 2–4
have nothing to show. **`ppw=12` does not fix that.** At seed 20260905 it produces the
same single exception, even though the worst pool grows 15 → 27 and crosses
`MAX_POOL = 20`:

| | ppw=6 | ppw=12 | ppw=24 |
|---|---|---|---|
| match precision | 1.0000 | 1.0000 | 1.0000 |
| refusal rate | 0.7% | 0.8% | **4.9%** |
| exceptions at seed 20260905 | 1 | 1 | — |

Refusals only rise materially at `ppw=24`. So the two-density headline is worth having
for the reason stated at the top — one number reads as a property of the engine — but it
is **not** evidence that the refusal layers are exercised, and the density sweep remains
the only place that is demonstrated. Saying so here rather than letting the second column
imply otherwise.

## LLM tier: reported as unmeasured

`docs/ARCHITECTURE.md` requires precision to be reported with the LLM tier on and off.
That comparison **is not made**, and the reason is recorded rather than the number
substituted.

The tier is architecturally complete and its boundary is enforced structurally:
`NarrationFields` has no field for a payment id, a candidate or a score, so a model
cannot nominate or endorse a match even in principle, and `parse_with_llm` fills only
fields the deterministic tier left empty. Both properties are tested.

What is missing is a valid measurement. There is no API key in this environment, and the
offline stand-in (`RecordedTier`) applies essentially the same word-filtering heuristic
as `normalize._extract_name` — it recovers the payer name on the same 8 of 18
unparseable narrations the regex tier already handles, and changes 0 verdicts. That
agreement is a property of the stand-in sharing the parser's logic, not evidence about
what a model would contribute.

Running with `ANTHROPIC_API_KEY` set selects the live tier and makes the comparison real.
Until then the claim is withheld.

The comparison itself is `python run.py llm-compare`. It reports parse yield, verdict
deltas and both arms' headlines, then judges its own validity: against the stand-in it
exits non-zero and prints why the numbers are not evidence about a model. Measured
against the stand-in on seed 20260905: 13 credit narrations unreadable by the regex tier
(all missing a merchant reference, none missing a payer name), 10 refs recovered, 0
payer names recovered, 0 verdicts changed, precision identical at 100.00% in both arms.

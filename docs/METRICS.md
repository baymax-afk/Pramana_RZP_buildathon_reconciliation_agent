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
                        CAPTURED payments in the batch
```

Counts payments, not bank transactions, so a many-to-one settlement covering six
payments contributes six to the numerator. Refused and unmatched payments are
excluded from the numerator and included in the denominator.

**The denominator is CAPTURED payments, not all of them, and the distinction is worth
stating because it moves the headline.** At the reported seed there are 200 payments of
which 194 are captured, so 174 assigned reads 89.69% rather than 87.0%. An uncaptured
payment — one that failed at the gateway, or an order never completed — has no money
behind it and can never appear on a bank statement; counting it against the engine would
score it for failing to match something that does not exist.

This document previously said "total payments in the batch" while `scorer/score.py`
divided by `captured_payments`. The code was right and the definition was wrong.
`tests/test_reported_numbers.py` now pins the denominator so the two cannot drift again.

### Reachable ceiling — what the match rate should be compared against

```
                     payments some truth link says a credit SHOULD be assigned to
reachable ceiling = ────────────────────────────────────────────────────────────
                                  CAPTURED payments in the batch
```

**89.69% against 100% is the wrong comparison, and it is the one a reader makes without
being told otherwise.** 100% is not available on this batch. Of the 20 captured payments
the engine does not assign, **15 are unreachable by construction, and ground truth marks
every one of them `refuse`**:

| n | why it cannot be matched |
|--:|---|
| 6 | `unsettled` — the payment never settled, so **no bank credit exists** to match it |
| 5 | `bank_charge` — the receiving bank took ₹5–50 on top of MDR, outside the ±₹1 tolerance. Refusing is the correct output |
| 4 | the hand-placed `many_to_one` ambiguity case — two subsets fit the same credit, and refusing is the designed answer |

**This table used to have a fourth row, and losing it is the O8 result.** It read:
*"2 — `split_settlement`, one payment arriving as two credits. The model cannot
represent it: `claimed` is a set of payment ids and there is nowhere to put half a
payment."* True when written, and no longer: Layer 2b raised the claim unit from one
credit to a GROUP of credits, the two halves balance against the payment exactly, and
those payments are now reachable and matched. The ceiling rose 91.24% → 92.27% because
of it, which is the honest way for a ceiling to move — the denominator of what is
*possible* grew, so the engine gets no free credit from it.

Scoring the engine for the remaining 15 is scoring it for failing to do something nobody
claims it can do.

| | primary | shifted holdout |
|---|---:|---:|
| match rate | 89.69% | 86.60% |
| reachable ceiling | **92.27%** | **93.30%** |
| short of the ceiling | **5** payments | **13** payments |
| unreachable by construction | 15 | 13 |
| match precision | 1.0000 | 1.0000 |
| assignments behind that precision | 133 | 112 |
| 95% Clopper–Pearson lower bound | 97.26% | 96.76% |
| settlement groups resolved | 3 (one four-way) | 3 (one four-way) |
| reversals identified | 7 of 7 (2 partial) | **4 of 5** (1 partial) |
| debits correctly declined, with a reason | 2 of 2 | 2 of 2 |
| miscategorised | 0 | 0 |

**Not every debit can be tied, and the ones that cannot are now four named categories
rather than one bucket.** A claw-back on an earlier statement and a chargeback against a
settlement this engine refused are both unresolvable *here* — declining is the correct
output — so ground truth marks them `refuse` and what is scored is whether the engine says
**which kind** of unresolvable it is. An engine answering *"cannot say"* to every debit
would pass a decline-only check with full marks, which is why the category is scored and
not just the decline.

**The holdout's fifth reversal is a real miss, and it is named rather than absorbed.**
The shift overwrites references across days, and one partial chargeback there points at a
settlement whose reference that shift destroyed. No evidence path remains, so the engine
reports the debit as unexplained — the correct output — and `run.py holdout` counts it
under *"chargebacks whose reference was overwritten"*. Preventing the clobber would have
made the number 5 of 5 by weakening the stress, which is the trade this artefact exists
to refuse.

The ceiling is derived from each batch's own truth links, not asserted — which is why
the two columns differ, and `tests/test_ceiling.py` asserts that they must. A hardcoded
constant passes every arithmetic check on the batch it was written against and fails
that one.

**On the primary batch the whole remaining gap is one defect class.** All five payments
short of the ceiling are `third_party_payer`, all five refuse as `amount_name_conflict`
with residual `+0p` — the amount channel is exact and the name channel disagrees because
a parent company paid a subsidiary's invoice. They are named individually, with rupees
and the engine's own reason, in `reports/scorecard.json` and in the UI's ceiling panel.

**Scoring travels in its own artefact.** `reports/run_output.json` is defined as what the
engine could justify with no answer key; the ceiling is derived from the answer key.
Folding one into the other would make the isolation claim unverifiable by opening the
file, so the scorecard is a separate file on a separate route
(`/api/scorecard`), and a test asserts no truth-derived term appears in the engine
payload. See `DEFECT_LOG` 2026-09-04-01 for why this is written down.

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

**And the bound on it, because 1.0000 is not self-explanatory.** A precision of 1.0000 on
130 assignments and one on 130,000 are the same figure and not the same claim. The exact
two-sided 95% Clopper–Pearson lower bound is what the sample actually supports:

| assignments | precision | 95% CI lower bound |
|---|--:|--:|
| 133/133 (reported batch) | 1.0000 | **97.26%** |
| 112/112 (shifted holdout) | 1.0000 | **96.76%** |

*(Counts rise as settlement groups are scored one entry per bank line, because an operator
sees each of those rows on the statement and each is either right or wrong. The bound
moves with the sample size, which is the only way it should move.)*

**This figure was 0.9963 for one afternoon**, at seed 55555 in the density sweep, and the
cause is worth carrying next to the number: widening the group model widened what could
be grouped, and two *ambiguous* credits were rolled into a coincidental grouping and
posted. The engine's own sweep caught it before anything else did. `DEFECT_LOG`
2026-09-04-10.

`ARCHITECTURE.md` cites the industry standard of **99.9%** precision for fully automated
matching. **This batch cannot reach it**, however clean the result, and the report prints
the bound beside the headline rather than leaving a reader to work it out. An external
reviewer computed it before this project did, which is why it is here.

Clopper–Pearson rather than a normal approximation: a Wald interval on a proportion of
exactly 1.0 has zero width and would print `1.0000 ± 0.0000`, asserting the opposite of
the truth. The bound is computed by bisecting the exact binomial tail — stdlib only, no
new dependency — and `tests/test_confidence_interval.py` checks that search against the
closed form `(α/2)^(1/n)` that exists for the zero-error case.

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
| 3 | 8.8 | 86.5% | **1.0000** | 6.7% |
| 6 | 15.0 | 88.4% | **1.0000** | 7.4% |
| 12 | 27.8 | 88.5% | **1.0000** | 8.2% |
| 24 | 52.8 | 73.9% | **1.0000** | 15.7% |

**The curve got steeper, and that is the batch getting harder rather than the engine
getting worse.** Two defect categories were added in O10 — a four-way split settlement
and a partial chargeback — so every arm of this sweep is scored against a batch with more
structure in it than the previous table saw. Refusal rate now rises **2.4x** across the
density range where it rose 1.9x before, and match rate at `ppw=24` falls further.
Precision is 1.0000 at every arm, before and after. That is the whole claim, and a
harder batch is a better place to make it.

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

> **SUPERSEDED — the table above is kept as the finding that prompted the fix.** It was
> true when written, and what it identified is exactly what got fixed: the batch was too
> clean for the refusal machinery to be visible at the reported density. Seven further
> defect categories were added in response (`OUTSTANDING_TASKS` O6), and the same three
> arms at the same seed now read:
>
> | | ppw=6 | ppw=12 | ppw=24 |
> |---|---|---|---|
> | worst realised pool | 15 | 28 | 53 |
> | match rate | 88.7% | 88.1% | 83.5% |
> | match precision | **1.0000** | **1.0000** | **1.0000** |
> | refusal rate | 10.6% | 10.3% | 10.2% |
> | exceptions at seed 20260905 | **15** | **15** | **14** |
>
> The refusal layers are now exercised at every density rather than only at `ppw=24`,
> which is what the original finding said was missing. The density sweep is still the
> place the *trend* is demonstrated — these are one seed, not five.

## LLM tier: measured

`docs/ARCHITECTURE.md` requires precision to be reported with the LLM tier on and off.
**That comparison is now made.** It was withheld for days because no API key existed in
the build environment; one was supplied on 2026-09-03 and
`python run.py llm-compare --seed 20260905 --verify` reported VALID.

| | LLM OFF | LLM ON (live `claude-sonnet-5`) | delta |
|---|---:|---:|---:|
| match rate | 88.66% | **89.18%** | **+0.52pp** |
| match precision | **1.0000** | **1.0000** | **+0.00pp** |
| refusal rate | 10.64% | 9.93% | −0.71pp |
| assignments | 126 | 127 | **+1** |

> **Measured 2026-09-03, against the engine as it then was, and NOT re-run after O8.**
> The baselines in this table are the pre-Layer-2b ones — the deterministic arm now reads
> 89.69% over 130 assignments. The table is left at its measured values rather than
> rewritten, because the quantity it reports is a *delta between two arms of one run*
> (+0.52pp, one extra verdict), and that delta is what the section is about. Re-running
> it costs live API calls and would answer the same question; what it must not do is
> pretend the 88.66% column is today's baseline. It is not.

Of 141 credit narrations, **13** are unreadable by the regex tier. The live model fills
**8**, all merchant references and no payer names — the regex tier already reads a name
off all 13. Exactly one verdict changes, and ground truth agrees with it.

**Three qualifications, all of which cut against the tier rather than for it.**

The hand-written offline stand-in fills **9** of the same 13 gaps — *more* than the live
model — and reaches the same single verdict change. That is not a failure of the model:
`recorded.py` predicted it, because the stand-in was written for this generator's
narration shapes. It does mean the generalisation claim is unmade.

The live arm is **not deterministic at the verdict level**. Nine of ten observed runs
assign 127 and one assigns 126, with the permutation gate reporting `unstable: 0` on the
odd one out — so it is the tier's output moving, not order-dependence being caught. An
earlier version of this document called the live arm deterministic on the evidence of
five agreeing runs; see `DEFECT_LOG` 2026-09-03-04.

It costs **30–35 seconds against 33 ms** with the tier off, all of it sequential HTTP.
The deterministic arm is what a live demo should run.

**The boundary, stated at the strength the evidence supports.** `NarrationFields` carries
no payment id, no candidate and no score, so a model cannot name a record here and cannot
post a match whose arithmetic fails — `fees.fits` still gates tier 1. It is *not* true
that a model cannot influence the answer: `merchant_ref` is free text that resolves
through `ReferenceIndex` to an invoice number and thence to a payment at one hop, and
tier 1 outranks every other tier in the evidence order. Measured, the live tier moves +1
assignment and reclassifies 9 credits from tier 2 to tier 1. See `REVIEW.md` §5 and the
docstring in `recon/llm/interface.py`.

**The reported run does not use the live tier.** `run.py match` selects the deterministic
offline tier even when a key is present; `--live-llm` opts in. `reports/run_output.json`
is the artifact the API, the UI and this submission all read, and it must be reproducible
by someone with no key and no budget. The live measurement has its own command, and that
command says `VALID` only when a live model actually answered.


# Outstanding tasks

What is knowingly incomplete, and why. Kept as a live list rather than reconstructed at
submission, in the same spirit as `DEFECT_LOG.md`.

Items marked **withheld** are not bugs. They are claims the project declines to make
because the evidence does not support them — recording that is the point.

Ordering is by consequence, not by effort.

---

## Withheld claims — evidence does not support them

### W1. The confidence score is not calibrated — **BenchRec obtained 2026-09-04, and it says something bigger**
**Status:** the blocker is gone. The fit ran. What it settled and what it did not are
separate answers, and the second one matters more.

    data/benchrec/BenchRec_cash_v1.0_eval.csv       69,171 rows
    data/benchrec/BenchRec_cash_v1.0_solution.csv   32,048 labels

37,123 A-side (ledger) rows carrying an allocation, 32,048 B-side (bank) rows carrying
none, and a solution file mapping each `B_id` to the allocation it belongs to. Real
Tier-1 bank data, obfuscated, CC BY 4.0.

#### First: the ingest was wrong, and wrong in the way that looks like a result

`benchrec_ingest.load_pairs` was written **before the data could be obtained**, against a
guessed schema. Every guess failed silently and in the same direction:

| it read | the file actually has |
|---|---|
| `B_allocation` | *no such column* — only the A side carries one |
| `A_currency` / `B_currency` | `A_currencyCode` / `B_currencyCode` |
| a B-keyed label tested against A-only rows | rows are **single-sided**; a pair must be joined |

It returned 37,123 rows — one per A record — every one labelled negative. The calibration
fitter consumed that without complaint and reported **base rate 0.000, ECE 0.0032, one
occupied bin**. An ECE of 0.003 reads like a good number.

Rewritten against the real schema: pairs are joined B→A through the solution's
allocation, blocked on value date, with sampled negatives. Base rate 0.202.

#### Second: the `m` prior for references is wrong by two orders of magnitude

Measured on 30,057 true pairs. Both sides are obfuscated and neither quotes the other, so
"agreement" is the most generous definition still worth the name — a shared run of six or
more digits anywhere in either side's references or attributes.

| field | `FS_M_PRIORS` assumes | BenchRec measures |
|---|--:|--:|
| reference (shared 8-char prefix) | **0.99** | **0.279** |
| reference (shared full digit run) | — | **0.012** |
| amount | 0.98 | **0.823** |
| date | 0.95 | 0.986 |

**The direction is what matters, not the size.** A Fellegi–Sunter weight is
`log2(m/u)` when a field agrees and `log2((1−m)/(1−u))` when it does not:

| reference | agrees | **does not agree** |
|---|--:|--:|
| under the prior `m=0.99` | +8.06 bits | **−6.64 bits** |
| under the fitted `m=0.279` | +5.99 bits | **−0.47 bits** |

So an engine carrying this prior into a real bank feed treats *"the references don't
match"* as **fourteen times more evidence against a correct match than it is**. On
BenchRec, where references disagree on 72% of true matches, that is a mechanism for
refusing correct matches in bulk.

**Two further things the same measurement says.**

* **The amount channel is not the certainty this engine treats it as.** Only **82.3%** of
  true matches have equal amounts. This engine requires conservation to hold within ±₹1
  before it will post; on this data that would refuse roughly one true match in six on
  arithmetic alone.
* **Excluding date from the comparison vector was right.** Chance agreement on value date
  among date-blocked non-matches is **1.000** — it carries exactly zero evidence once it
  has been used for blocking. `fellegi_sunter.py` says this in prose; it is now measured.

#### What was NOT done, deliberately

**The fitted `m` was not written into `config.py`.** `m` is a property of a data source's
reference semantics, not a constant of reconciliation. This project's generator writes
clean quoted invoice numbers into narrations, and 0.99 is roughly right *there*; BenchRec's
counterparties do not quote each other at all. Substituting one for the other would be
fitting a second dataset's semantics onto the first, which is the mistake in a different
direction.

**What the finding licenses instead**, and it is a stronger claim than a fitted constant:
*the prior is disclosed, its source is named, and it has now been measured against
external labelled data that says it does not transfer.* `config.FS_M_SOURCE` still reads
"fallback priors (unfitted)" and that is still the honest label for what the engine runs.

#### So: is the confidence score calibrated?

Still no, and the reason has changed. It was "we cannot obtain the data". It is now
recorded in the calibration block below.

#### The calibration curve, for the first time

    source        BenchRec (external, labelled, real Tier-1 bank data)
    examples      40,001   base rate 0.202
    bins occupied 10 of 10
    ECE           0.0230

| | previous (own batches) | BenchRec |
|---|--:|--:|
| examples | 3,705 | 40,001 |
| base rate | 0.992 | 0.202 |
| bins occupied | **1 of 10** | **10 of 10** |
| ECE | 0.0002 | **0.0230** |
| interpretable | no — one bucket | **yes** |

**The ECE got a hundred times worse and that is the good news.** 0.0002 over one occupied
bin was the arithmetic of a single bucket, published with its bin count precisely so it
could not be quoted as evidence. 0.0230 across ten occupied bins is a number that means
something.

What the curve says, read honestly: the two bins holding 99.8% of the mass are close —
predicted 0.052 against observed 0.038, and predicted 0.928 against observed 0.991. The
sparse middle is badly over-confident (bucket 0.85: predicted 0.857, observed 0.148) on
27 examples. So the score **separates well at the extremes and should not be read as a
probability in between**, which is a more useful statement than either "calibrated" or
"decorative".

Fitted weights: `residual_tightness +5.32`, `fs_scaled +1.21`, `uniqueness_margin −0.004`.

**One caution on that third number, because it invites a wrong conclusion.**
`uniqueness_margin` was mapped onto BenchRec as `1 / block_size`, which is near-constant
inside a date block. Its ~zero weight is at least partly an artefact of that mapping and
is **not** evidence that subset-sum uniqueness carries no signal in this engine, where it
means something quite different. Reported because suppressing it would be worse; not
leaned on.

#### The weights were NOT written into `calibration.json`, and this is the load-bearing decision

The two fits answer different questions over different populations:

| | BenchRec fit | the engine's score |
|---|---|---|
| question | given *any* candidate pair in a date block, is it correct? | given a match that **survived four verification layers**, is it correct? |
| base rate | 0.202 | 0.992 |
| population | 80% are pairs the engine would never have accepted | only what the refusal layers let through |

A logistic fitted on a 20%-positive population and applied to a 99%-positive one is
miscalibrated by construction. Substituting these weights would replace an honestly
labelled unfitted score with a confidently wrong one — the failure this project spends
its time avoiding, arriving with an external dataset attached as credentials.

`calibration.json` still reads `"fallback": true, "meaningful": false`, and
`config.FS_M_SOURCE` still reads `fallback priors (unfitted)`. **Both labels are still
correct and both stay.**

#### So what did BenchRec settle?

| question | before | after |
|---|---|---|
| Can the calibration be measured at all? | no — blocked two ways | **yes, and it was** |
| Is the confidence score calibrated? | unknown | **no, and now we can say how** — good at the extremes, over-confident in the middle |
| Do the FS priors transfer to real bank data? | assumed | **no — `reference` is out by 2 orders of magnitude, in the direction that refuses correct matches** |
| Is excluding date from the evidence vector right? | argued | **measured: u = 1.000, zero evidence** |
| Should either be written into the engine? | — | **no, and for a stated reason rather than by omission** |

**What is still open:** the engine's confidence score remains uncalibrated *for its own
population*, and settling that needs labelled data from that population — a merchant's own
reconciled history, not a public benchmark. That is a different piece of work and it is
now the honest description of the gap.

### ~~W2. The LLM on/off precision comparison is unmeasured~~ — **measured 2026-09-03**
**Status:** RESOLVED. A key was supplied, the command was run, and it reported VALID ·
`DEFECT_LOG` 2026-09-02-02, 2026-09-02-06

The trust boundary is real: `NarrationFields` carries no payment id, candidate or score,
so a model cannot express a matching preference even in principle, and `parse_with_llm`
fills gaps only. Both properties are tested. (One qualification the audit added, and it
matters: a `merchant_ref` reaches a payment id in **one hop** through `ReferenceIndex`,
so "cannot express a preference" is too strong as an absolute. See `REVIEW.md` §5.)

    python run.py llm-compare --seed 20260905 --verify

**Measured, live, against `claude-sonnet-5`:**

| | LLM OFF | LLM ON (live) | delta |
|---|---:|---:|---:|
| match rate | 88.66% | 89.18% | **+0.52 pp** |
| match precision | **1.0000** | **1.0000** | 0.0000 |
| refusal rate | 10.64% | 9.93% | −0.71 pp |
| assignments | 126 | 127 | +1 |
| correct assignments | 126 | 127 | +1 |

Of 141 credit narrations, **13** are unreadable by the regex tier. The live model filled
**8**, all of them merchant references and **no payer names** — the regex tier already
reads a name off all 13. Exactly **one** verdict changed: `bank_txn_0103`, from
`refuse:amount_name_conflict` to `assign:pay_SYN001441035`, and ground truth agrees.

**So the honest headline is: the LLM tier buys +1 assignment and costs precision
nothing.** Small, real, and measured rather than asserted.

**Three things the measurement showed that were not expected.**

1. **The live model is not better than the hand-written stand-in on this data.** The
   stand-in fills **9** of 13 gaps; the live model fills **8**. Both change the same
   single verdict and both leave precision at 1.0000. That is not a failure of the
   model — `recorded.py` predicted it in as many words: the stand-in was written for
   *this generator's* narration shapes. It does mean the generalisation claim is still
   unmade, and a shifted-distribution holdout is what would settle it.

2. **The live tier is non-deterministic at the field level and deterministic at the
   verdict level.** Repeated runs recovered 7 refs, then 8. But over **5 runs with a
   fresh live tier each time, the assignment map and refusal set hashed to one
   fingerprint** — the same one the offline stand-in produces. The layers downstream
   absorb the variance: a recovered reference only changes anything if it changes a
   tier decision, and the amount channel still has to agree. This is the strongest
   available evidence that the verification architecture does what it claims.

   **Corrected 2026-09-03, on the tenth observed run.** The claim above said the engine does not vary in what it decides with a live tier, on the evidence of five runs that all produced one fingerprint. A sixth, run through the gated path while regenerating the artefact, produced **126 assignments instead of 127** -- the model recovered nothing useful on `bank_txn_0103` that time, so the tier contributed zero. Four further ungated runs went back to 127.

   So the honest statement is: **9 of 10 observed live runs assign 127 and one assigns 126.** The permutation gate reported `unstable: 0` on the 126 run, so this is not order-dependence being caught -- it is the tier's output being an INPUT to the engine, and that input moving. The deterministic arm (`--no-llm`) is bit-identical every time and remains what the demo should run.

   Five runs agreeing was a real observation and it was not enough to support the word "deterministic". Recorded rather than quietly restated.

3. **It costs 30–35 seconds of wall clock, against 33 ms with the tier off** — a ~1000x
   slowdown, all of it sequential HTTP. See the demo-risk note in `REVIEW.md` §6: the
   live arm is a pre-computed artifact for the demo, not something to run on stage.

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

### ~~O3. W2 — the LLM comparison is still withheld~~ — **closed 2026-09-03**
A key was supplied and the comparison ran live: **+1 assignment, precision unmoved at
1.0000**, and the verdict-level output identical across 5 runs. Full numbers under W2
above.

Two things had to be fixed before it could run at all, and both are recorded because
they were real rather than incidental. `select()` could not read a gitignored `.env`,
and this environment strips `ANTHROPIC_API_KEY` from the inherited shell — so the key
had arrived three times and the code had never once seen it. And the first live run had
to be killed: nothing on the tier's path memoised, so the same 13 narrations were bought
on every fixpoint round and every permutation pass. See `REVIEW.md` P0-3.

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

### ~~O6. The batch is still too clean to demonstrate the refusal machinery~~ — **closed**
Not by changing the reported density, but by making the data honest. Seven new defect
categories took refusal rate from 0.74% to 9.2% at the primary seed and 7.9–13.8% across
the sweep, with precision holding at 1.0000 in every arm. The refusal machinery is now
visible at every density rather than only at ppw=24, so the ppw=24 swap is no longer
needed. Original item kept below.

### O6 (original). The batch is still too clean to demonstrate the refusal machinery
**Partially addressed, and the fix did not do what this item predicted.**

The headline now reports `ppw=12` beside `ppw=6` (`--compare-density`, default 12). That
is worth having on its own terms: one density in the headline reads as a property of the
engine rather than of the engine at one crowding level.

But it does **not** achieve what this item wanted. At seed 20260905, `ppw=12` produces
the *same single exception* as `ppw=6` — one, the hand-placed ambiguity case — even
though the worst pool grows 15 → 27 and crosses `MAX_POOL`. Refusal rate moves 0.7% →
0.8%. Refusals only rise materially at **ppw=24** (4.9%).

So the density sweep remains the only place the refusal behaviour is actually visible,
and it stays load-bearing for the argument. Reporting `ppw=24` as the second arm would
demonstrate it, at the cost of a headline arm whose match rate (86.6%) is not
representative of the reported configuration. That trade is a presentation decision, not
a defect, and it is left open deliberately.

### ~~O7. `assert_truth_is_satisfiable` should also run inside the sweep~~ — **done**
The sweep now asserts every batch it builds. That is exactly where the orphaning defect
hid: `generate` checked the primary seed and the sweep never checked its own five, so a
sweep could quietly average over unsatisfiable ground truth and report the generator's
bugs as the engine's coverage. All 20 batches (4 densities x 5 seeds) pass.

### ~~O8. Two named model limitations, recorded not scheduled~~ — **both lifted, 2026-09-04**
The original entry, kept because the entry is what made this findable: *"`split_settlement`
(one payment, many credits) and `chargeback_debit` (the engine reads credits only) are
outside the model rather than merely hard. Both refuse correctly and both cost real
coverage. `docs/ARCHITECTURE.md` states what lifting each would take — in both cases a
different engine, not a patch. Recorded so a correct-looking refusal cannot hide an
unmodelled relation."*

**Both are now in the model, and the interesting part is that one of the two "different
engine" arguments was wrong on its own terms.**

`ARCHITECTURE.md` had predicted that split settlements would require the claim unit to
become `(payment, fraction)` and Layer 2's uniqueness test to enumerate over partitions
rather than subsets. The claim unit did have to change — but a part-settlement is a
**group** relation and the group balances exactly, so raising the unit from one credit to
a group of credits expresses it with integer arithmetic and the same uniqueness question
Layer 2 already answers. Fractions are only needed to post half a payment, which the same
document already argued is a wrong answer rather than a partial one. **The simpler model
was available the whole time, behind an assumption nobody had tested.**

The chargeback argument was closer to right: a debit does ask a different question, and it
gets its own module. What was wrong was the claim that the engine would have to *un-post*
the settlement. It does not. Both events happened; the assignment stands, the reversal is a
later entry against the same credit, MR5's accounting is untouched, and the batch reports
reconciled **gross and net**. Conservation across time by addition, not by deletion.

| | before | after |
|---|--:|--:|
| match rate (primary) | 88.66% | **89.69%** |
| match rate (holdout) | 84.54% | **85.57%** |
| match precision | 1.0000 | **1.0000** (both) |
| assignments behind it | 126 / 104 | **130 / 108** |
| 95% CI lower bound | 97.11% | **97.20% / 96.64%** |
| exceptions (primary) | 15 | **11** |
| exceptions (holdout) | 23 | **19** |
| reachable ceiling (primary) | 91.24% | **92.27%** |
| debit lines reaching a verdict | 0 of 6 | **6 of 6** |
| bank lines reaching no verdict | 6 | **0** |

**Zero wrong assignments on either batch, before and after.** The ceiling rose along with
the match rate, which is the honest direction: the two split payments became *reachable*
as well as matched, so the engine gets no free credit from the change — it has to do the
work to keep the same distance from the ceiling.

**Three things this cost, stated rather than buried.**

1. **The holdout was rebuilt.** Its labels had to change — a set still saying "refuse" for
   a relation the engine can now settle scored four correct assignments as wrong, at
   0.9630 precision. The rebuild changed **labels only**: every input file is
   byte-identical, which took a deliberate fix (newly-assignable split links had entered
   the drift sampler's population and re-sorted the statement). `FROZEN_DIGEST` was updated
   in the same commit with the reason written next to it.
2. **Two pre-existing defects surfaced**, both hidden by the split settlements rather than
   caused by removing them: `no_subset_fits` refusals carrying no candidate at all — on the
   largest exception in the batch, which is the exact complaint `REVIEW.md` P1-3 raised —
   and the foreign auditor reporting the engine's own correct groups as double-posts. Both
   fixed; see `DEFECT_LOG` 2026-09-04-09.
3. **Two new named limitations**, in the same spirit as the two just closed: a settlement
   split more than `MAX_GROUP_CREDITS = 3` ways, and a *partial* chargeback. Both are
   refused visibly rather than searched for; both are named in `ARCHITECTURE.md` so a
   correct-looking refusal still cannot hide an unmodelled relation.

### ~~O10. The two limitations O8 left behind~~ — **both lifted, 2026-09-04**

Named on the morning, closed on the afternoon, and the afternoon cost a wrong assignment.

**A four-way split was refused for the engine's convenience.** `MAX_GROUP_CREDITS = 3` was
justified as a search bound and it was one — `combinations(residue, k)` is 4,598,438
subsets at k≤6 over 40 credits, each running a subset-sum search, eight times over under
the permutation gate. It was also almost entirely waste: a group's members must land
within `GROUP_SPAN_DAYS` of each other, and the loop enumerated every subset before
discarding the spanning ones. On the reported batch, **1,474 subsets enumerated to keep
one.** Anchored on date windows the enumeration is exact and small, and the bound rose to
**6** at no measurable cost. *A bound that exists because of how a search is written is a
bound on the author, not on the problem.*

**A partial chargeback was being answered with silence.** A chargeback is raised against a
transaction and a settlement batch covers several, so disputing one payment out of four
produces a debit for that payment's share carrying the batch's reference. The first
reversal ledger required `debit == credit` exactly and reported every one as an
unexplained debit. It is a bounded subset-sum over the referenced settlement's payments —
Layer 2's own machinery pointed at a smaller pool — unique or not at all, with no payment
reversed twice.

**And the wrong assignment.** Widening the model widened what could be grouped, and the
eligibility rule was wrong: group resolution was offered *every* unsettled credit. Two
genuine many-to-one settlements, each refused as `multiple_candidates` at ppw=24, were
combined into a coincidental grouping and **posted**. Precision 0.9963 — the only wrong
assignment this engine has produced. Caught by the density sweep, which is the second
defect it has found that no reported run could reach.

The fix is the eligibility rule, not the group test: a credit refused for having several
viable decompositions is **ambiguous, not unexplained**, and grouping it adds a
possibility rather than resolving one. Only `no_subset_fits` and `no_candidate` credits
qualify. Re-measured across 4 densities × 5 seeds: **zero wrong assignments**.

| | after O8 | after O10 |
|---|--:|--:|
| match rate (primary) | 89.69% | **89.69%** |
| match rate (holdout) | 85.57% | **86.60%** |
| match precision | 1.0000 | **1.0000** (both) |
| assignments behind it | 130 / 108 | **133 / 112** |
| 95% CI lower bound | 97.20% / 96.64% | **97.26% / 96.76%** |
| settlement groups | 2 two-way | **3, one of them four-way** |
| reversals identified | 6 of 6 / 3 of 3 | **7 of 7 / 4 of 5**, 2 partial / 1 partial |
| defect categories in the batch | 16 | **18** |

**The primary match rate did not move, and that is the honest reading.** The generator
converts an already-matched credit into a split rather than adding coverage, so the batch
got harder and the engine held. The density sweep says the same thing louder: refusal rate
now rises **2.4×** across the density range where it rose 1.9× before, at precision 1.0000
throughout.

**The holdout's fifth reversal is a miss, and it is named.** The shift overwrites
references across days; one partial chargeback there points at a settlement whose
reference it destroyed. No evidence path remains, the engine reports the debit unexplained
— correctly — and `run.py holdout` now counts it under *"chargebacks whose reference was
overwritten"*. Preventing the clobber would have bought 5 of 5 by weakening the stress.

**The holdout was rebuilt a second time, and this time the inputs genuinely changed** —
new bank lines for the wide split and the partial chargeback. `FROZEN_DIGEST` carries the
reasoning: the decision was made before the set was scored, and it made the set strictly
harder. The number it produced was worse, not better.

**Successors**, named for the same reason: a claw-back against a settlement in an earlier
batch, a partial chargeback whose settlement the engine refused, and grouping an ambiguous
credit — the last of which is deliberately not done rather than not yet done.

### ~~O9. `third_party_payer` refusals are correct but expensive~~ — **closed, and measured on two batches**
The original entry, kept: *"an investigating agent checking whether a payer is an
authorised group entity is exactly the evidence that would close it."* It was written as
a hypothesis. It is now a measurement, and the measurement is smaller and more
interesting than the hypothesis.

`run.py agent --dataset {reported,holdout}` (deterministic arm, recorded investigator):

| | reported | shifted holdout |
|---|--:|--:|
| exceptions in the baseline | 11 | 18 |
| evidence asserted | **2** | **2** |
| declined — insufficient evidence | 9 | 16 |
| match rate | 89.69% → **90.72%** | 86.60% → **87.63%** |
| released | **₹87,995.75** | **₹33,048.28** |
| match precision | 1.0000 → **1.0000** | 1.0000 → **1.0000** |
| wrong assignments | 0 → **0** | 0 → **0** |

**Re-measured after O8 and again after O10, and the honest summary is that this number is
not stable across engine changes — it is 2 or 3 depending on what the deterministic layers
left behind.** After O8 it was 3 on the reported batch and 1 on the holdout; it is now 2
and 2. The agent closes `amount_name_conflict` refusals with register evidence, and every
change to the matching layers changes which credits are still carrying that category when
the agent arrives.

**Which is the finding, and it is a smaller claim than "our agent closes 3 exceptions".**
The agent's contribution is a residue: what the deterministic engine could not settle AND
the register happens to cover. On the holdout it doubled, not because the agent improved
but because a harder batch left more name conflicts standing. The defensible sentence
remains the one the per-source attribution supports — *a more complete authorised-payer
register closes more exceptions at unchanged precision* — and it is worth noting that this
figure has now moved for three different reasons, none of them the agent.

**Reproduce with `--offline --no-llm`, and both flags matter.** Without `--no-llm` the
engine selects a live `ClaudeTier` when a key is present in the environment, which resolves
one extra credit and reports a baseline of 90.21% that no one without that key can
reproduce. The published figures are the fully deterministic arm.

**The agent closes 60% of name conflicts on the reported batch and 10% on the shifted
one.** Same code, same investigator; the difference is register coverage, and 7 of the
holdout's 10 declines are *"no register entry"*. So the honest product claim is **"a more
complete authorised-payer register closes more exceptions at unchanged precision"** — not
"our agent closes exceptions". The per-source attribution built in Block B7 says exactly
this, which is the argument for having built it that way.

**Until 2026-09-04 this could not be measured at all**: `run.py agent` had no `--dataset`
flag, and `load_payer_directory()` defaults to the reported register — so running the
holdout by hand looked up one batch's payer names in another batch's authorisations and
returned a clean, wrong zero. `tests/test_agent_dataset.py` pins both the flag and the
per-batch register.

---

## The audit's ship list, 2026-09-03 — closed, with one item deliberately not built

`REVIEW.md` §8 ranked nine things to ship in 1–3 days. Eight are done. The ninth was
declined after measuring, and that is recorded here rather than left looking forgotten.

| | Item | Outcome |
|---|---|---|
| 1 | regenerate `run_output.json` with `--verify` | done (A1) |
| 2 | reject non-INR rows at load | done (Phase C) |
| 3 | fix the `decomposition_out_of_bounds` miscategorisation | done (A2) |
| 4 | timeout + cache + call cap on `ClaudeTier` | done (P0-3) |
| 5 | assignments view in the UI | done (P0-2) |
| 6 | **ceiling panel** | **done** — 91.24% reported beside 88.66%, derived from truth, moves with the batch |
| 7 | Ring-2 investigator | done (Phase B) |
| 8 | **reword the trust boundary honestly** | **done** — the absolute claim is gone from `interface.py`, `AGENTIC.md`, `METRICS.md` and `recorded.py` |
| 9 | balance-continuity check | done (P1-4) |

### The implied deduction rate, added on top
An `unexplained_residual` said "credit 643537p vs expected 644715..644719p" and left an
operator to work out why. It now adds: *"the credit implies a 2.77% deduction from gross,
above the 1.8%–2.5% this engine assumes — consistent with a charge levied on top of the
gateway fee"*.

Verified against ground truth rather than asserted: all three refusals it fires on carry
the `bank_charge` label, which is exactly what a rate above the band means. It stays
silent when the implied rate is inside the band, because there the residual is not a rate
problem and a note about rates would point the wrong way.

### Not built: naming the nearest out-of-window payment on a `no_candidate`
The audit's Phase 5 proposed this as a 1-hour fix. **Measured first: there are zero
`no_candidate` credits on either batch.** The candidate pool is never empty at these
densities — even the five holdout credits deliberately drifted past the lookback still
find *other* payments in their window, so they refuse as `no_subset_fits` with the wrong
candidates rather than reaching the empty-pool path at all.

So the feature would have been written for a branch that never runs, which is precisely
what the two deleted refusal categories were. Recorded as a measurement rather than
built. It becomes worth doing if the batch is ever made sparse enough to reach that path.

---

## The P1/P2 tail, 2026-09-03 — closed

Everything `REVIEW.md` raised below P0 is now done, plus one thing Phase B turned up.

| | Finding | Resolution |
|---|---|---|
| **dead categories** | `FS_BELOW_THRESHOLD` and `FS_REVIEW_BAND` defined and never raised | **Deleted.** Measured first: wiring the two-threshold band would have refused 78 of 126 assignments on the primary and 63 of 104 on the holdout, every one CORRECT and zero wrong ones saved. 4.0/7.0 are Splink's record-linkage conventions, where names *are* the evidence; here the amount channel is primary and 39.7% of correct assignments score `non_match` on names alone. Eleven refusal paths → nine |
| **P1-2** | METRICS defined match rate over "total payments", the scorer divides by captured | Doc fixed — 86.0% vs 88.66% on the same run. An uncaptured payment can never appear on a statement |
| **P1-1** | README said 136 bank txns / 200 invoices against a manifest of 147/187; METRICS held two refusal rates for ppw=6 | Both fixed. The stale ppw table is marked **SUPERSEDED** rather than deleted — it records the finding that prompted seven defect categories |
| **P1-4** | the `balance` column was loaded and never verified | Continuity check, relative between rows so it needs no opening balance. 147 and 130 rows verified on the two batches |
| **P1-3** | 6 of 15 exceptions carried no candidate, including the largest at ₹45,673 | The search now retains **which** subset came closest, not just how far. Primary 6 → 4, holdout 12 → 5 |
| **P2-1** | a blank cell became 0 everywhere | Per-column now: blank `credit` is zero (every bank writes statements that way), blank `balance` is an error |

**Two bugs found while doing it, both mine, both from this session.**

`match --dataset holdout` was overwriting `reports/run_output.json` — the file the API
serves and the UI renders — so scoring the shifted set left the demo showing the shifted
set under a seed nobody asked for. P0-1's shape from a new direction. Found because the
exception count printed after a holdout run did not match the primary's.

And a published claim was falsified on its tenth observation: see `DEFECT_LOG`
2026-09-03-04. "Deterministic at the verdict level" was five runs agreeing, which is
evidence of stability and not of determinism. Nine of ten live runs assign 127; one
assigns 126.

**397 tests.** Precision 1.0000 on both datasets throughout.

### Still open
- **W1** — the confidence score is uncalibrated. Re-checked tonight and still blocked two
  independent ways: no Kaggle credentials (`~/.kaggle/kaggle.json` absent, `kaggle` not
  installed) and the proxy still refuses the host (`CONNECT tunnel failed, response 403`).
- ~~**O8** — `split_settlement` and `chargeback_debit` remain outside the model by design.~~
  **Both lifted 2026-09-04**, and so were their two successors (**O10**): splits are now
  resolved to six-way and a partial chargeback reverses the payment subset it names.
  O10 also cost the engine's only wrong assignment, found and fixed the same afternoon.

---

## Phase C, 2026-09-03 — the shifted holdout

`python run.py holdout` builds it once; `run.py match --dataset holdout` scores it.

| | primary | shifted holdout |
|---|---:|---:|
| match rate | 88.66% | **84.54%** |
| match precision | **1.0000** | **1.0000** |
| refusal rate | 10.64% | **18.11%** |
| refusal correctness | 66.67% | 39.13% |

**Coverage falls, correctness does not.** That is the project's central claim, tested on a
distribution the engine was not built against — where it could have failed and did not.

**Shifted, not merely re-seeded.** The sweep already reports five held-out seeds at
precision 1.0000, so another sample from the same distribution answers nothing. This set
carries narration formats the regex tier was never written against, adversarial free text
(injected instructions, a fake system tag, a JSON blob naming a verdict, a line naming a
payment id), references duplicated *across days* rather than within a window, and
settlement drift pushed past the engine's own lookback — five credits made **provably
unreachable on purpose**, counted rather than relabelled.

**Frozen.** The content is hashed in `tests/test_holdout.py`. No constant in `config.py`
may be changed in response to a holdout result; the one change a holdout is allowed to
motivate is a correctness fix.

**It motivated exactly one.** Non-INR rows are now rejected by name at ingest. The field
was read and never checked, so a USD row would have been parsed as paise and reconciled
against rupee invoices at ~85× the true value — and *conservation would have balanced*,
both sides wrong the same way, so no downstream layer could have caught it.

### The first run reported 52.88% precision, and the holdout was wrong
49 wrong assignments out of 104, which read as a spectacular generalisation failure. It
was the answer key: bank ids are assigned **by position in the file**, `write` sorts the
statement by date, and drifting five dates re-sorted it — so every truth link at or after
a moved row described a different transaction. `_renumber_bank_txns` already existed and
already remaps the links; the shift was not calling it.

**The fourth time a generator defect has presented as an engine failure** — after
`refund_netted`, `partial_payment` and ambiguity-window orphaning. `DEFECT_LOG`
2026-09-03-03. What caught it was refusing to publish a number that surprising without
first reading the wrong assignments.

### What the holdout does NOT settle
It is the same generator, shifted — not real bank data. BenchRec would settle it and
remains blocked (W1). The honest sentence for a judge: *"we moved the distribution as far
as we could without leaving our own generator, and the refusal machinery did what it
claims; we cannot show you real-world precision and we do not claim it."*

---

## Phase B, 2026-09-03 — the agent, shipped

`python run.py agent`. Rings 2 and 3 of `AGENTIC.md`, which until today was a design note
by its own first line and the audit's headline gap.

| | Offline | Live (`claude-sonnet-5`) |
|---|---|---|
| match rate | 88.66% → **90.21%** | 88.66% → **90.21%** |
| precision | 1.0000 → **1.0000** | 1.0000 → **1.0000** |
| verdicts moved / assertions | 3 / 3 | 3 / 4 |
| declined | 12 | 11 |
| wall clock | 0.06s | ~4 min |

**Built in the order that kept the demo safe.** B4 first — the evidence channel — as a
provable no-op: four spellings of "no evidence" hash identically to the baseline, so the
whole layer is revertible by deleting a branch. Then the tools, then the agent. At no
point was the deterministic engine's output at risk.

**The result worth reporting is not the coverage.** It is that the live model closed one
case the coded procedure declines — a register reading `Pinnacle Steel Traders` against a
ledger reading `Pinnacle Steels Traders` — and was right, while `_same_entity` correctly
refuses it because that is exactly how the generator's planted confusable pairs differ.
That is the audit's "brittle rules belong in the LLM" direction, measured. And the live
arm scores *worse* on gain per assertion (0.75 vs 1.00) while reaching the same headline,
which is reported rather than buried.

**Side D — the authorised-payer register — is reference data, and the argument matters
because a judge will press on it.** Ground truth says which bank line maps to which
payments. The register says only that a name on the statement is a permitted payer for a
name in the ledger: a join between two fields both sides already publish, which is what a
customer master file is. It is written outside `_truth/`, is not on `ReconInputs`, and
coverage is deliberately partial (4 of 7 relationships, plus 6 decoys) — full coverage
would make the defect class a lookup, and an agent that closes every case because the
answer sat in a file demonstrates nothing.

### Four bugs found by reading its output, all in name matching
The batch is built to break name matching, and it did.

1. **`PayerRelation` returned parallel tuples.** Selecting a customer by one criterion
   and reading `relationships[0]` cited a *different* register row, so a rationale
   attributed a decision to unrelated evidence. Now one record per row.
2. **The investigator reasoned over the whole pool** rather than the candidate the engine
   had actually refused, and asserted a true-but-irrelevant fact. The containment held —
   the engine ignored it — but an assertion that cannot matter is noise and wasted budget.
3. **It asserted the register's spelling** where the engine matches the ledger's.
4. **The payer index was keyed on the suffix-stripped name**, discarding the form
   `_same_entity` needs: `Bharati Traders LLP` strips to 15 characters while the statement
   carries `BHARATI TRADERS LL` at 18, so the truncation rule rejected its own match.

The comparison itself was rebuilt twice. Raw prefix matching merges the confusables; word
boundaries fix that. Gating partial matches on the bank's *field width* was then the wrong
basis — the name sits inside a fixed-width narration, so `VERTEX ENGINEERIN` arrives at 17
characters and was silently declined. **The real property is not "long enough" but
"unambiguous"**, so the lookup now refuses when a query matches more than one registered
payer — Layer 2's own doctrine applied to names.

### Still open after Phase B
- **W1** — the confidence score is uncalibrated, still blocked on BenchRec.
- **Two dead refusal categories.** `FS_BELOW_THRESHOLD` and `FS_REVIEW_BAND` are defined
  in `RefusalCategory` and **never raised anywhere** — only `contradicts` gates Layer 3.
  Two of eleven categories are unreachable. Found while wiring the evidence field; not a
  correctness bug, but the explanation table and the flowchart both imply they fire.
- **Phase C** — the shifted-distribution held-out set. Not started.

---

## Hostile audit, 2026-09-03 — `REVIEW.md`

Audited as a staff engineer and a buildathon judge: ran the pipeline rather than reading
the docs, which is how three of the findings surfaced. Full write-up in
[`REVIEW.md`](../REVIEW.md).

**All four P0s fixed the same day.** Suite 295 → 323.

| | Finding | Resolution |
|---|---|---|
| **P0-1** | `reports/run_output.json` shipped with an empty verification block, and the UI returned `null` for it — so the four-layer claim rendered as *nothing*, silently | The root cause was `test_cli_robustness.py`, which shelled out to `run.py match` with `cwd=ROOT` and no `--verify` on **every pytest run**. Redirected to a tmp dir; the payload now carries an explicit `status`; the UI renders its absence as a warning strip |
| **P0-2** | No assignments view — 126 of 141 outcomes invisible, and no way to ask why a match was made | The explainability engine, below |
| **P0-3** | A live API key turned the demo into a multi-minute hang | Cache, 10 s timeout, 1 retry, call cap. 0.40 s |
| **P0-4** | `decomposition_out_of_bounds` fired on pools of 1, 1, 1, 2, 4, 4 while telling operators there were "too many candidates to search" | Split into `pool_exceeded` and `no_subset_fits` |

**The finding worth keeping in view.** `interface.py` claims a model *"structurally
cannot"* express a matching preference. It does not need to: `merchant_ref` →
`ReferenceIndex` → `invoice_no` → payment id is **one hop**, and tier 1 outranks
everything in `evidence_key`, so a plausible invoice number selects a payment and wins
contested money. Measured against the offline stand-in: **+1 assignment and 9 credits
moved from tier 2 to tier 1.** Amount conservation still gates it, so the mitigations are
real — but the absolute claim is not, and `REVIEW.md` §5 proposes the honest wording.

### Explainability engine — shipped 2026-09-03
Three reading levels for every credit, because the three audiences want different things:
a **plain sentence** (no jargon), **typed evidence links** to the actual payment, invoice
and bank rows, and the **full transcript** with the arithmetic in paise.

The transcript is the *actual computation*, recorded as it runs — not a description
written afterwards. Nothing in `recon/explain/` calls a model, and a test parses both
modules' ASTs to assert neither imports one, so an explanation cannot drift from the
decision it describes: there is no second inference that could disagree.

**Recording is inert, and that is the load-bearing property.** `match_once` is pure and
MR1 depends on it, so `tests/test_explain.py` hashes the assignment map, the refusals
with their categories and rupees at risk, and the no-candidate set with recording on and
off, and requires them byte-identical.

Two UI bugs were found by driving the real page in Chromium, neither visible in the build
or in the diff: an effect-dependency deadlock that left every explanation panel loading
forever while its request returned 200 every time, and a `certain_fee` field missing from
the payload that made the Matches card assert *"fee known exactly: no"* on **all 127**
assignments — while the transcript two lines below it said the opposite.

---

## Code review, 2026-09-02 — 14 findings, none covered by the suite

Full write-up: [`REVIEW_2026-09-02.md`](REVIEW_2026-09-02.md). Reviewed the whole session
(14 commits, 48 files, ~9.6k insertions) at high effort. **All 265 tests pass**, so every
finding is something the suite does not reach.

Four were reproduced directly; the rest are read from the diff and marked as unverified,
and that distinction is kept rather than collapsed.

**RESOLVED 2026-09-03 — all 14.** Suite 265 → 287, precision 1.0000 at every density arm.
Two fixes produced further defects while being applied, both recorded. The list below is
kept as written so the order and the reasoning stay legible.

**Was:**

1. **R1 + R2** — the headline and `run_output.json` can name a seed and density that did
   not produce them. Reproduced: a batch generated at seed 77771 / ppw 12 prints
   `seed=20260905 density=6` and writes a payload inconsistent with itself. Same bug class
   as the manifest guard added this session, which closed the loud path and left the
   silent one open.
2. **R3** — `third_party_payer` is ~29% mislabelled (2 of 7 at the primary seed carry the
   *correct* payer name), so the `OUTCOME BY DEFECT` numbers for it — and the conclusion
   drawn from them — rest on a partly wrong cohort. Fix the label, then re-measure and
   restate or withdraw.
3. **R4** — `pip install -e .` produces a broken `pramana` entry point and an unimportable
   `api.main`. Reproduced. H1 removed the `sys.path` bootstrap on a premise that is not
   true as it stands.
4. **R5 + R6** — two `assert_truth_is_satisfiable` call sites abort their command instead
   of reporting, while adjacent assertions in the same loop bodies are handled.
5. **R9 + R10** — two guards that would fail silently: the ambiguity guard's claimed safety
   margin is zero, and the satisfiability assertion covers two unreachability shapes of
   three.
6. **R11** — every assigned credit parses its narration twice, doubling live LLM cost.
7. **R7, R8, R13, R14** — correctness of things nothing reads yet.

**The pattern worth noting:** three of the top four are *incomplete fixes rather than new
mistakes*. The session's own lesson — the metric that looked right — held for the
session's own work.

# Pramana

**A reconciliation engine that verifies its own output**
Razorpay Buildathon 2026 — Track 04, AI Finance Controller

> *pramāṇa* — the term in Indian epistemology for a **means of valid knowledge**: not a
> belief, but the thing that justifies holding one. The name states the argument rather
> than the subject matter. Producing candidate reconciliation matches is easy; knowing
> which of them is *justified* is the unsolved part, and that is what the four
> verification layers exist to answer — without an answer key.

A three-way reconciliation engine that matches Razorpay payments against a bank
statement against an invoice ledger over a batch of 200+ records, produces a
rupee-ranked actionable exception list, and — the part that matters — **verifies its
own output through four independent mechanisms that do not require knowing the right
answer.**

---

## The claim

Producing candidate matches is easy. Knowing which candidates to trust is the
unsolved part.

Reconciliation vendors report auto-match rates of 90–99%, and their own analysts now
say headline accuracy is no longer the differentiator — what matters is the 1–10%
that don't auto-match. **Commercial vendors publish coverage; they do not publish
match precision.**

That claim is deliberately scoped to vendors, because the research community does not
share the gap. [BenchRec](https://www.kaggle.com/datasets/benchmarkteam/benchrec-real-world-cash-reconciliation-dataset),
the only public real-world reconciliation benchmark, already treats precision as a
hard constraint and coverage as the thing to optimise beneath it. This project is on
the benchmark's side of a gap the vendors haven't closed.

So the contribution is not "verification matters." It is that **the verification
apparatus ships inside the system and runs at runtime on data where no ground truth
exists** — because anything that needs the right answer in order to check the answer
is useless on a merchant's own books.

Four layers, none of which require labels:

| Layer | Mechanism | Question it answers |
|---|---|---|
| **1** | Metamorphic relations (MR1–MR6) | Does the output change when it provably shouldn't? |
| **2** | Uniqueness testing + principled refusal | Is this *an* answer, or *the* answer? |
| **3** | Fellegi–Sunter evidence weights, two-threshold band | How strong is the non-amount evidence? |
| **4** | Materiality stratification (PCAOB AS 2315) | What can be claimed about the rows nobody checked? |

**MR1 is not only a test — it is a runtime refusal gate.** The engine runs the
matcher over 8 shuffled input orderings. Any assignment that isn't stable across all
of them was decided by iteration order rather than by the data, and is refused.

Details in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Every reported number is
defined in [`docs/METRICS.md`](docs/METRICS.md).

---

## Explicitly out of scope

Listed before building, and not partially implemented:

- Cash flow forecasting
- Settlement Q&A / chat interface
- Multi-currency
- Live settlement reports, or anything needing Razorpay production access
- TDS/GST tax-line matching as a user-facing feature *(deductions still appear in the data)*
- Accept/reject feedback loop
- MILP optimal solver for subset-sum
- Conformal risk control

---

## The trust boundary

**Deterministic code decides every match. No LLM output ever creates, confirms, or
scores a match assignment.**

The LLM does exactly two jobs: parse bank narration strings into structured fields
when the regex tier fails, and write human-readable exception explanations. The
system runs with the LLM tier disabled, and **precision is reported both ways**. If
the LLM tier makes precision worse, that is what the metrics block says.

**Measured live against `claude-sonnet-5`, not asserted** (`run.py llm-compare`):
the tier fills 8 of the 13 narrations the regex tier cannot read — all merchant
references, no payer names — and changes exactly **one** verdict, correctly.
**Match rate 88.66% → 89.18%; precision 1.0000 → 1.0000.** Over five runs with a fresh
live tier each time, the assignment map and refusal set hashed to a single fingerprint,
identical to the offline arm's.

**That was five runs, and a tenth run broke it.** One gated run produced 126 assignments
rather than 127 — the model recovered nothing useful on that one borderline credit, so the
tier contributed zero. The permutation gate reported `unstable: 0`, so this is not
order-dependence: it is the tier's output being an *input* to the engine, and that input
moving. **9 of 10 observed live runs assign 127; one assigns 126.** The deterministic arm
(`--no-llm`) is bit-identical every time, which is what a live demo should run. Full numbers, including where the live model does *worse*
than the offline stand-in, in [`docs/OUTSTANDING_TASKS.md`](docs/OUTSTANDING_TASKS.md) W2.

**And that is now what the committed artefact is.** For a day it was not: this paragraph
said "the deterministic arm is what a live demo should run" while `reports/run_output.json`
was still being generated with the live tier, so regenerating it for unrelated work moved
the demo's headline from 127 to 126 by itself. `reports/run_output.json` and
`reports/scorecard.json` are now produced by `python run.py match --verify --no-llm`, the
payload says `llm_tier: disabled`, and a test asserts it — re-run the command and the
numbers come back bit for bit. The live delta above stays published beside them rather
than baked into them. See [`docs/DEFECT_LOG.md`](docs/DEFECT_LOG.md) 2026-09-04-02.

Ground truth is written by the generator to a directory the matching engine never
reads. Enforced by function signatures, an import-time audit hook, and a test that
deletes the ground-truth directory and asserts the engine and all four verification
layers still run identically.

---

## The agent

`python run.py agent` — a tool-calling investigator over the exception list, and the
orchestration that re-runs the engine on what it finds.

**The agent may never decide a match.** Its one lever is to supply evidence the engine
did not have and re-run it. Five typed read tools (`get_exception`,
`get_candidate_pool`, `test_subset`, `lookup_payer_relationship`, `search_invoices`) and
one validated write (`propose_evidence`). `test_subset` calls the matcher's own
`fees.expected_credit_interval` rather than reimplementing conservation, so the agent can
ask any question the engine can answer and cannot answer one itself.

| | Offline (`--offline`) | Live (`claude-sonnet-5`) |
|---|---|---|
| match rate | 88.66% → **90.21%** | 88.66% → **90.21%** |
| match precision | 1.0000 → **1.0000** | 1.0000 → **1.0000** |
| verdicts moved / assertions | 3 / 3 | 3 / 4 |
| exceptions declined | 12 | 11 |
| wall clock | **0.06s** | ~4 min |

**`--null-agent` reproduces the baseline byte for byte.** Every figure above is a delta
against a run anyone can reproduce without an agent, and the suite asserts it.

The live model closed one case the coded procedure declines — a register reading
`'Pinnacle Steel Traders'` against a ledger reading `'Pinnacle Steels Traders'` — and was
right. It also made one assertion that moved nothing, so it scores **worse** on gain per
assertion (0.75 against 1.00) while reaching the same headline. Both are reported.

Evidence is asserted, never applied: proposals enter an append-only ledger, the
deterministic engine re-runs, and it reaches its own verdict — still a refusal for most
of them. `EvidenceProposal` carries no payment id, and its value is rejected if it merely
*looks* like one, because [`REVIEW.md`](REVIEW.md) §5 showed that a free-text field one
hop from an identifier is a way to name a record.

Details, including the four name-matching bugs found by reading its output, in
[`docs/AGENTIC.md`](docs/AGENTIC.md).

---

## The gap, and the ceiling it is measured against

`python run.py match --verify` reports a **reachable ceiling** beside the match rate,
derived from ground truth rather than carried as a constant.

```
match rate            88.66%     172/194 captured payments assigned
reachable ceiling     91.24%     177/194 payments ground truth says CAN be matched
short of the ceiling       5     payments the engine could have matched and did not
```

**100% is not on offer, and saying so is not a hedge.** Of the 22 captured payments left
unmatched, **17 are unreachable by construction** — six never settled, so no bank credit
exists to match them, and the rest belong to relations the engine does not model
(`split_settlement`, `bank_charge`), where refusing is the correct output. Counting those
against the engine scores it for failing to do something nobody claims it can do.

**The engine is 5 payments from the maximum this data permits, and all 5 share one
cause** — `third_party_payer`, every one at residual `+0p` with a Fellegi–Sunter field
weight of `-3.26`. The amount channel is exact; the name channel disagrees because a
parent company settled a subsidiary's invoice. `run.py agent` closes 3 of them by
supplying the authorised-payer relationship as evidence and re-running the engine.

On the shifted holdout the ceiling is **92.27%** and the engine reaches 84.54% — 15
short, which is what a distribution it was not built against costs.

---

## Generalization: the shifted holdout

`python run.py holdout` builds it once; `python run.py match --dataset holdout` scores it.

**Not a fresh seed.** The density sweep already reports five held-out seeds at precision
1.0000, so another sample from the same distribution answers a question nobody is asking.
This set is *shifted*: narration formats the regex tier was never written against,
adversarial free text, references duplicated across days, and settlement drift pushed past
the engine's own lookback — five credits made **provably unreachable on purpose**, counted
rather than relabelled.

| | primary | shifted holdout |
|---|---:|---:|
| match rate | 88.66% | **84.54%** |
| match precision | **1.0000** | **1.0000** |
| refusal rate | 10.64% | **18.11%** |
| refusal correctness | 66.67% | 39.13% |

**Coverage falls, correctness does not.** Under a distribution it was not built against
the engine declines more work rather than getting more of it wrong. That is the whole
claim, tested where it could have failed.

The set is **frozen** — its content is hashed in `tests/test_holdout.py` — and no constant
in `config.py` may be changed in response to a holdout result. The one change a holdout is
allowed to motivate is a correctness fix, and it motivated one: non-INR rows are now
rejected by name at ingest. Read as paise, a USD row would reconcile against rupee
invoices at ~85× the true value, and *conservation would balance* — both sides wrong the
same way — so nothing downstream could have caught it.

The first run of this set reported precision **52.88%**. It was the holdout that was
wrong, not the engine: bank ids are assigned by position in the file, so drifting a date
re-sorted the statement and shuffled the answer key. See
[`DEFECT_LOG`](docs/DEFECT_LOG.md) 2026-09-03-03 — the fourth time a generator defect has
presented as an engine failure.

---

## Data provenance

Three disclosed tiers. The gradation is the honest part — a bigger "real" number
would be worth less than an accurate account of what is real.

| Tier | What it is | Count |
|---|---|---|
| **R1 — captured payments** | Genuinely completed Razorpay test-mode payments. Real `id`, `fee`, `tax`, `created_at`, `bank`, `bank_transaction_id`. **The only tier with a real fee/tax pair.** | **18 captured + 6 failed = 24** |
| **R2 — Razorpay-issued orders** | Real orders created through the API. Genuine Razorpay-issued IDs, receipts, notes and server timestamps. **Never completed — no `fee`, no `tax`, not `captured`.** | **12** |
| **S — synthetic** | Schema-conformant records generated locally, carrying the injected defects. | **164** |

Total batch: **200 payments**, 147 bank transactions, 187 invoices, across 34
settlement windows.
settlement windows.

The R1 slice spans **7 distinct payer contacts**, **7 banks** (BARB_R, CNRB, DEUT,
IBKL, KVBL, PUNB_R, UTBI), two payment methods (netbanking, wallet), and ₹215 to
₹18,700. The 6 failed payments are genuine failures — bank declines, issuer errors,
and a customer cancellation — including a real failure-then-retry pair against the
same order, which is exactly the pattern real reconciliation data contains.

**The MDR model is measured, not invented.** Across all 18 captured payments the fee
base is exactly **2.200%** of the amount, with GST at 18% on that base. The model
`base = round(0.022 × amount); tax = round(0.18 × base); fee = base + tax` predicts
the true fee within **[−1, +2] paise** on every record — a 50× margin against the ₹1
matching tolerance. The exact GST rounding rule is *not* recoverable from 18
observations and no attempt is made to claim otherwise; see
[`docs/DEFECT_LOG.md`](docs/DEFECT_LOG.md) 2026-09-01-01, which records getting this
wrong first.

**An uncaptured order is not a payment.** R2 entities carry no fee or tax because
nothing was ever captured; presenting them as reconcilable revenue would be the same
overclaim this project exists to criticise. Sides B (bank statement) and C (invoice
ledger) are fully generated — no real settlement data exists in test mode.

All test-mode payments were completed using **only Razorpay's published test values**.
No real card, account, or credential was used at any point.

### Injected defects

**Sixteen categories.** Fifteen carry a ground-truth label; `chargeback_debit`
deliberately carries none, because the engine structurally cannot produce a verdict for a
debit and inventing one would score it against a permanent, unclosable miss. It is
disclosed instead — see *not examined*, below.

The original nine: MDR/gateway fee deduction · TDS deduction · T+1 and T+2 settlement
date drift · one bank credit covering N payments · partial payment · duplicate UTR ·
near-duplicate payer names · paisa-level rounding · refund netted inside a settlement
batch.

Five more, added because the batch was unrealistically clean without them — most
visibly in that *every* payment carried an invoice number, which made exact-reference
matching available far more often than reality allows:

| Defect | What it is | Why it is hard |
|---|---|---|
| **overpayment** | The customer pays more than the invoice | Mirror of partial payment; the invoice ends over-settled |
| **advance payment** | Money against no invoice at all | No reference, no TDS — the amount channel stands alone |
| **bank charge** | The receiving bank takes its own NEFT/RTGS fee | Appears on no Razorpay object and in no ledger. **Labelled `refuse`**: at ₹5–50 against a ₹1 tolerance it is unmatchable, and declining it is the correct output |
| **third-party payer** | A parent company settles a subsidiary's invoice | The amount channel is right and the name channel is wrong |
| **weekend bunching** | Fri/Sat/Sun payments all settle Monday | Realised drift reaches 3 days on top of the window |

And two that stress the engine's **model** rather than its arithmetic:

| Defect | What it is | Why the engine cannot reach it |
|---|---|---|
| **split settlement** | One payment arrives as *two* bank credits | `claimed` is a set and every tier asks which *subset of payments* sums to a credit — there is nowhere to put half a payment |
| **chargeback debit** | A settled payment is clawed back by a debit line | The engine reads `is_credit` only, so money leaving is invisible: not matched, not refused, not counted |

Both are labelled `refuse`, and in both cases refusing is correct — posting a
part-settlement against a whole payment would be a wrong answer, not a partial one. But
the coverage they cost is real, so they are named as
[limitations](docs/ARCHITECTURE.md#two-named-limitations-of-the-model) rather than left
to hide behind a correct-looking refusal. Ground truth creates **no link for a debit**:
scoring the engine against a verdict it structurally cannot produce is theatre, so the
metrics block discloses the unexamined lines and their value instead.

The statement contained **zero debits** until `chargeback_debit` existed, which is
exactly why that blind spot went unnoticed.

**On `third_party_payer`, a claim was made and then withdrawn.** An earlier version of
this README said the payments that reconcile are the ones quoting an invoice reference.
That was measured over a cohort ~29% of which was mislabelled — the messy-narration
branch ignored the third party, so records carried the *correct* payer name while being
labelled a name mismatch. Re-measured on a clean cohort over five seeds: **13 matched,
20 refused**, and a quoted reference is *sufficient* to reconcile (9 of 9 with one
matched; none was refused) but its absence is not decisive (4 of 24 without one still
matched). See `DEFECT_LOG` 2026-09-03-01.

**`bank_charge` is the one deliberately labelled unmatchable.** An engine that widened
its tolerance to absorb bank charges would also start absorbing genuine coincidences,
and the whole subset-sum uniqueness argument rests on tolerance staying far below the
smallest payment. It is there to prove the engine declines the case rather than
swallowing it — measured, it refuses 7 of 7 and posts none.

The metrics block reports **outcome by defect**, with `missed` and `refused (correct)`
as separate columns. A defect the engine declines is not a failure when ground truth
also expects a refusal; one recall figure would score the engine down for being right.

Plus one **hand-placed ambiguity case**: a bank credit where two different payment
subsets both sum within tolerance, constructed so that no amount, date, method, or
name signal can break the tie. The engine must detect the ambiguity, **refuse to
assign**, and emit both candidates with rupees at risk. Ground truth labels it
`expected_verdict: "refuse"`, so refusing scores as correct and assigning either
subset scores as a false match. Guarded twice — the generator brute-forces the window
and fails the build unless exactly two subsets fit, and a test asserts the engine's
verdict.

---

## Running it

```bash
pip install -e '.[api,test]'      # engine + API + test deps; the engine itself has none
```

`pramana ...` and `python run.py ...` are the same code: the root `run.py` is a shim over
the packaged `pramana_cli:main`, so both work and neither depends on the current
directory.

The engine, all four verification layers and the scorer run on the **standard library
alone** — `pip install -e .` with no extras is enough for every number reported below.
The extras are for the FastAPI server and the test suite.

```bash
python run.py generate --seed 20260905
```

Builds all three sides plus the ground truth, and runs the three anti-accident
assertions: the ambiguity case has exactly two candidates, tolerance sits 209x below
the smallest payment, and no settlement window exceeds the search bound. These fail
the build; they do not warn.

```bash
python run.py generate --density-sweep
```

Generates at each swept density. The high arm deliberately exceeds the search bound —
that is the condition under study, not a fault.

Matching, verification and scoring subcommands land with those blocks.

```bash
python run.py --seed 77771 --verify --score
```

Second seed, demonstrating the numbers aren't cherry-picked.

```bash
python run.py match --seed 20260905 --verify
```

Runs the engine **under the permutation gate** and prints the metrics block with all
six metamorphic relations. Drop `--verify` for a single unguarded pass. The engine runs to completion from
`ReconInputs` alone; ground truth is loaded afterwards, by a different package.

The headline reports **two densities** — the reported `ppw=6` and a `ppw=12` second arm —
because one density there reads as a property of the engine rather than of the engine at
one crowding level, and density is the parameter the argument turns on. The second arm is
generated in-process and never written to disk; everything below the headline describes
the `ppw=6` run. `--compare-density 0` turns it off, `--compare-density 24` points it at
the crowded arm. See `docs/METRICS.md` for what that comparison does and does not show.

```bash
uvicorn api.main:app --port 8000     # read-only API (importable from anywhere)
cd ui && npm install && npm run dev  # triage UI on :5173
```

The UI is a single page: exceptions ranked by rupees at risk, each expanding to show
why the engine declined, what to do next, and — for ambiguous credits — every candidate
it refused to choose between.

Directly under the totals it qualifies sits a **"not examined" disclosure**. The at-risk
figure counts refused *credits*, and the engine reads credits only — so chargebacks,
reversals and bank fees are invisible to it. Showing the exception list without saying so
would be misleading by omission, and the omission matters more here than in the metrics
block, because this page is what someone acts on. The lines are listed but never mixed
into the worklist: they are not items to work, they are items the engine cannot speak
about. The API is **read-only by design**: there is no accept /
reject endpoint, because a feedback loop is out of scope and a button that did nothing
would be worse than none.

```bash
pytest tests/
```

376 tests, including the end-to-end isolation test — which deletes the ground-truth
directory from disk, reruns the engine, and asserts the output is identical.

The percentages in the build order below are **what each block achieved when it landed**,
not current figures. The current ones are in `docs/METRICS.md`, and
`tests/test_reported_numbers.py` re-derives them from a live run so they cannot go stale
in prose.

*Full command reference lands with the engine — see the build order below.*

---

## Status

Built against a ~30 hour budget, solo. Build order is fixed and the verification
layers are never cut; if the schedule slips, the UI degrades to a static table.

- [x] **Block 0** — repo skeleton, frozen config, architecture and metrics docs
- [x] **Block 1** — real payment capture: 24 R1 payments (18 captured), 12 R2 orders
- [x] **Block 2** — generator, ground truth, nine defects, ambiguity case *(sixteen now)*
- [x] **Block 3** — matching engine, tiers 1–2 *(76.6% coverage at the time)*
- [x] **Block 4** — scorer, metrics harness, isolation test  ← **metrics block lands here**
- [x] **Block 5** — metamorphic harness + runtime permutation gate (MR1–MR6 all pass)
- [x] **Block 6** — bounded subset-sum + Layer 2 uniqueness and refusal *(86.1% at the time)*
- [x] **Block 7** — Layer 3 Fellegi–Sunter (two-threshold band, unsupervised `u`)
- [x] **Block 8** — Layer 4 materiality (AS 2315) + composite confidence
- [ ] **Block 8b** — BenchRec calibration fit (weights currently UNCALIBRATED)
- [x] **Block 9** — LLM tier — changes reasons, and now one decision; see `DEFECT_LOG` 2026-09-03-01
- [x] **Block 10** — FastAPI + React exception triage UI

[`docs/DEFECT_LOG.md`](docs/DEFECT_LOG.md) records what broke during the build, as it
broke. [`docs/FLOWCHARTS.md`](docs/FLOWCHARTS.md) diagrams how the system actually
behaves — including the places where measurement contradicted the original design.
[`docs/OUTSTANDING_TASKS.md`](docs/OUTSTANDING_TASKS.md) lists what is knowingly
incomplete, including two claims the project deliberately **withholds** because the
evidence does not support them.
[`docs/AGENTIC.md`](docs/AGENTIC.md) is a design note on where agency can safely live in
a system like this — the short answer being everywhere except the verdict, and the
argument being that the trust boundary is what *permits* autonomy rather than what
limits it.

---

## Attribution

**BenchRec: A Real-World Cash Reconciliation Dataset** — Operartis / the BenchRec
initiative, originally released for the ICAIF 2023 Benchmark Competition. Licensed
**CC BY 4.0**. Used as an external calibration and Fellegi–Sunter training set; not
redistributed here.

**The Subset Sum Matching Problem** — J.P. Morgan AI Research,
[arXiv 2508.19218](https://arxiv.org/abs/2508.19218). Cited for the formalisation.
Their benchmark was never publicly released, and their algorithms terminate on the
first valid match without addressing non-unique solutions — which is the gap Layer 2
fills.

**Splink** — the Fellegi–Sunter match-weight formulation and threshold
correspondences.

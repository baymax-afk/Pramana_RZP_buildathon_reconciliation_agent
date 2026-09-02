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

Ground truth is written by the generator to a directory the matching engine never
reads. Enforced by function signatures, an import-time audit hook, and a test that
deletes the ground-truth directory and asserts the engine and all four verification
layers still run identically.

---

## Data provenance

Three disclosed tiers. The gradation is the honest part — a bigger "real" number
would be worth less than an accurate account of what is real.

| Tier | What it is | Count |
|---|---|---|
| **R1 — captured payments** | Genuinely completed Razorpay test-mode payments. Real `id`, `fee`, `tax`, `created_at`, `bank`, `bank_transaction_id`. **The only tier with a real fee/tax pair.** | **18 captured + 6 failed = 24** |
| **R2 — Razorpay-issued orders** | Real orders created through the API. Genuine Razorpay-issued IDs, receipts, notes and server timestamps. **Never completed — no `fee`, no `tax`, not `captured`.** | **12** |
| **S — synthetic** | Schema-conformant records generated locally, carrying the injected defects. | **164** |

Total batch: **200 payments**, 136 bank transactions, 200 invoices, across 23
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

Nine categories, each ground-truth labelled: MDR/gateway fee deduction · TDS
deduction · T+1 and T+2 settlement date drift · one bank credit covering N payments ·
partial payment · duplicate UTR · near-duplicate payer names · paisa-level rounding ·
refund netted inside a settlement batch.

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
uvicorn api.main:app --port 8000     # read-only API
cd ui && npm install && npm run dev  # triage UI on :5173
```

The UI is a single page: exceptions ranked by rupees at risk, each expanding to show
why the engine declined, what to do next, and — for ambiguous credits — every candidate
it refused to choose between. The API is **read-only by design**: there is no accept /
reject endpoint, because a feedback loop is out of scope and a button that did nothing
would be worse than none.

```bash
pytest tests/
```

232 tests, including the end-to-end isolation test — which deletes the ground-truth
directory from disk, reruns the engine, and asserts the output is identical.

*Full command reference lands with the engine — see the build order below.*

---

## Status

Built against a ~30 hour budget, solo. Build order is fixed and the verification
layers are never cut; if the schedule slips, the UI degrades to a static table.

- [x] **Block 0** — repo skeleton, frozen config, architecture and metrics docs
- [x] **Block 1** — real payment capture: 24 R1 payments (18 captured), 12 R2 orders
- [x] **Block 2** — generator, ground truth, nine defects, ambiguity case
- [x] **Block 3** — matching engine, tiers 1–2 (76.6% coverage, precision 1.0000)
- [x] **Block 4** — scorer, metrics harness, isolation test  ← **metrics block lands here**
- [x] **Block 5** — metamorphic harness + runtime permutation gate (MR1–MR6 all pass)
- [x] **Block 6** — bounded subset-sum + Layer 2 uniqueness and refusal (86.1% match rate, precision 1.0000)
- [x] **Block 7** — Layer 3 Fellegi–Sunter (two-threshold band, unsupervised `u`)
- [x] **Block 8** — Layer 4 materiality (AS 2315) + composite confidence
- [ ] **Block 8b** — BenchRec calibration fit (weights currently UNCALIBRATED)
- [x] **Block 9** — LLM tier (no verdict changes; see DEFECT_LOG 2026-09-02-03)
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

# A reconciliation engine that verifies its own output

**Razorpay Buildathon 2026 — Track 04, AI Finance Controller**

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
| **S — synthetic** | Schema-conformant records generated locally, carrying the injected defects. | *to 200+, pending Block 2* |

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
python run.py --seed 20260905 --verify --score
```

One command, one metrics block, fixed seed printed, identical numbers on repeat.

```bash
python run.py --seed 77771 --verify --score
```

Second seed, demonstrating the numbers aren't cherry-picked.

```bash
pytest tests/
```

Includes the ground-truth isolation test, the ambiguity guard, and tolerance sanity.

*Full command reference lands with the engine — see the build order below.*

---

## Status

Built against a ~30 hour budget, solo. Build order is fixed and the verification
layers are never cut; if the schedule slips, the UI degrades to a static table.

- [x] **Block 0** — repo skeleton, frozen config, architecture and metrics docs
- [x] **Block 1** — real payment capture: 24 R1 payments (18 captured), 12 R2 orders
- [ ] **Block 2** — generator, ground truth, nine defects, ambiguity case
- [ ] **Block 3** — matching engine, tiers 1–2
- [ ] **Block 4** — scorer, metrics harness, isolation test
- [ ] **Block 5** — metamorphic harness + runtime permutation gate
- [ ] **Block 6** — bounded subset-sum + Layer 2 uniqueness and refusal
- [ ] **Block 7** — Layer 3 Fellegi–Sunter
- [ ] **Block 8** — Layer 4 materiality, composite confidence, BenchRec calibration
- [ ] **Block 9** — LLM tier
- [ ] **Block 10** — FastAPI + React exception triage UI

[`docs/DEFECT_LOG.md`](docs/DEFECT_LOG.md) records what broke during the build, as it
broke.

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

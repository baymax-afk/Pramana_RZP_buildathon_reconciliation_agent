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
| **3** | Fellegi–Sunter evidence weights, gated on contradiction | Does the non-amount evidence *argue against* this match? |
| **4** | Materiality stratification (PCAOB AS 2315) | What can be claimed about the rows nobody checked? |

**Three of the four verify at runtime; the fourth produces the plan an auditor works.**
Projection needs an observed error rate and observing errors needs a human, so Layer 4's
runtime output is a *verification plan* — which items require full checking, which sample
stands for the rest, and what the projection would be for any error rate that sample
turns up. Computing a projected error from zero actual verification would be inventing
assurance. `src/recon/verify/materiality.py` has always said this; the summary above did
not.

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
match rate            89.69%     174/194 captured payments assigned
reachable ceiling     92.27%     179/194 payments ground truth says CAN be matched
short of the ceiling       5     payments the engine could have matched and did not
match precision       1.0000     133/133 correct, 95% CI >= 97.26%
```

**100% is not on offer, and saying so is not a hedge.** Of the 20 captured payments left
unmatched, **15 are unreachable by construction** — six never settled, so no bank credit
exists to match them, five carry a `bank_charge` outside tolerance, and four are the
hand-placed ambiguity case where refusing is the designed answer. Counting those against
the engine scores it for failing to do something nobody claims it can do.

**`split_settlement` used to be on that list and no longer is.** One payment arriving as
two bank credits was outside the model — `claimed` is a set, so a payment is taken once,
and no tier could ask a question a half-settlement has an answer to. Layer 2b raises the
claim unit from one credit to a *group* of credits; the two halves balance exactly, and
both the match rate and the ceiling moved because of it. The ceiling moving with it is
the honest direction: the engine gets no free credit, it has to do the work to keep the
same distance from a target that just got harder.

**The engine is 5 payments from the maximum this data permits, and all 5 share one
cause** — `third_party_payer`, every one at residual `+0p` with a Fellegi–Sunter field
weight of `-3.26`. The amount channel is exact; the name channel disagrees because a
parent company settled a subsidiary's invoice. `run.py agent` closes 2 of them by
supplying the authorised-payer relationship as evidence and re-running the engine.

On the shifted holdout the ceiling is **93.30%** and the engine reaches 86.60% — 13
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
| match rate | 89.69% | **86.60%** |
| match precision | **1.0000** | **1.0000** |
| assignments behind it | 133 | **112** |
| 95% CI lower bound | 97.26% | **96.76%** |
| refusal rate | 7.64% | **13.85%** |

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
| **R2 — settlements simulated on real orders** | Real orders created through the API — genuine Razorpay-issued IDs, receipts, notes and server timestamps. **The orders were never completed, so the capture, `fee` and `tax` are SYNTHESISED** from the rate model measured on R1, and the records enter the batch `captured`. Real identity, modelled money. | **12** |
| **S — synthetic** | Schema-conformant records generated locally, carrying the injected defects. | **164** |

Total batch: **200 payments**, 153 bank transactions (144 credits and 9 debits), 187
invoices, across 34 settlement windows.

Two of those figures are worth reading twice, because both used to be rounder and the
change is the point. The statement carries **debits** now — chargebacks and claw-backs,
which the engine structurally cannot read and therefore discloses rather than scores. And
there are fewer invoices than payments because **13 payments carry none at all**: money on
account, which is ordinary and which the batch had none of while every payment had an
invoice number and exact-reference matching was available far more often than reality
allows.

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

**What R2 is, stated exactly, because an earlier version of this section got it
wrong.** This table used to say R2 carried *"no `fee`, no `tax`, not `captured`"*. That
is true of the raw orders in `data/mcp_created/orders_r2.json` and **false of the records
that reach the batch**: `build.py::_r2_as_payments` promotes each one into a settled
payment with a synthetic fee, and all 12 sit inside the 194-payment denominator behind
the match rate. The generator's own docstring always said so — *"this is the one place
the codebase turns an uncaptured order into something that looks like revenue"* — and the
README was describing the source tier rather than the batch. It was doc drift, not a
hidden transformation, and `tests/test_reported_numbers.py` now asserts this table against
a live tier count so it cannot drift again.

So: **an uncaptured order is not a payment, and these are not presented as one.** They are
12 settlement simulations wearing real order identity. What is real is the identity — id,
receipt, notes, server timestamp, all inspectable in the Razorpay dashboard. What is
modelled is the money, from the rate fitted to R1's 18 genuine fee observations. **R1
remains the only tier with a real fee/tax pair**, which is the claim that actually matters
and is unaffected. Sides B (bank statement) and C (invoice ledger) are fully generated —
no real settlement data exists in test mode.

All test-mode payments were completed using **only Razorpay's published test values**.
No real card, account, or credential was used at any point.

### Injected defects

**Twenty categories**, all carrying a ground-truth label. `chargeback_debit`
deliberately carried none until 2026-09-04, because the engine structurally could not
produce a verdict for a debit and inventing one would have scored it against a permanent,
unclosable miss. Layer 2c reads debits and ties each to the settlement it reverses, so
`reverse` is a verdict it can now produce and withholding the label would hide real work
instead of avoiding a fake miss.

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
to hide behind a correct-looking refusal.

The statement contained **zero debits** until `chargeback_debit` existed, which is
exactly why that blind spot went unnoticed — the engine had never been shown the half of
a bank statement it ignored by construction. It reads them now: **7 of 9** debits on the
reported batch are tied to the settlement they reverse, on amount, carried reference and
ordering, uniquely or not at all — two of those being *partial*, one payment disputed
inside a settlement batch where the rest still stands. A reversal does not undo the
settlement it reverses — both events happened — so the batch reports reconciled **gross
and net** rather than silently as one number.

**The other two cannot be tied, and they say which kind of untieable they are.** One
reverses a settlement from an earlier statement; one reverses a credit this engine
refused, and names it — the only place here where one exception's resolution is stated to
unblock another. Both used to read *"money left the account and this engine cannot say
against what"*, which is honest and equally true of a bank fee. Declining is the correct
output for both; declining with a reason is the useful one, and the reason is scored so
an engine answering "cannot say" to everything could not pass by declining.

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

**The whole thing, from a clean clone, in two terminals.** Verified end to end against a
fresh checkout and an empty virtualenv — not written from memory.

```bash
# terminal 1 -- engine + API
pip install -e '.[api]'                     # the engine itself has no dependencies
python run.py match --verify --no-llm       # writes reports/run_output.json + scorecard.json
uvicorn api.main:app --port 8000            # read-only; leave it running

# terminal 2 -- the UI
cd ui && npm install && npm run dev         # http://localhost:5173
```

Demoing it? [`docs/DEMO.md`](docs/DEMO.md) is a six-minute run-sheet: open on the money
reconciled, one concrete before/after, the stability proof, then what it refused.

The data is committed, so `python run.py generate` is optional — run it only to rebuild
the batch from its seed. The UI proxies `/api` to port 8000, so **the API has to be
running before the page will show anything**; if it is not, the page now says so and
gives you that `uvicorn` line rather than blaming the data.

Three failure modes worth knowing, all of which used to be confusing and are now
self-explaining:

| what you see | what it means |
|---|---|
| `ModuleNotFoundError: No module named 'pramana_cli'` | the package is not installed. `run.py` now catches this and prints the `pip install` line instead |
| the page says **"The API isn't running"** | nothing is listening on :8000. Start `uvicorn` and reload; nothing else needs restarting |
| a **"Verification — did not run"** strip | the run was produced without `--verify`. Re-run `python run.py match --verify --no-llm` |

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

## Connecting it to other systems

**The matching logic never sees a file, a vendor or a schema.** `ReconInputs` carries
three typed record sets — payments, bank lines, invoices — and no paths. That was done for
the ground-truth isolation boundary rather than for portability, and portability is what
it also buys: **a new source is a loader, not a change to the engine.**

| | |
|---|---|
| **Implemented today** | Razorpay payments (API + MCP server), bank statement CSV with column and currency validation, invoice ledger CSV replaceable from the UI |
| **What a new connector needs** | Map its records onto `Payment` / `BankTxn` / `Invoice`, amounts in integer paise, currency declared and checked at the boundary |
| **What it does not touch** | The three matching tiers, the four verification layers, the refusal taxonomy, the scorer |

Candidates for the next one: **SAP**, **Tally**, **Zoho Books**, NetSuite, QuickBooks, or
a merchant's own ERP export. **None of these is built** — the claim here is about where
the seam is, not about integrations that exist. The current path is the first
implementation, and it is the only one with measured numbers behind it.

The one thing a new source genuinely *would* change is the narration grammar the parser
reads. That is why an unrecognised grammar is routed to a model rather than guessed at,
and why a name the parser cannot vouch for is withheld instead of being fed to the
matcher — see [`docs/DEFECT_LOG.md`](docs/DEFECT_LOG.md) 2026-09-04-04 for what happened
the one time that went wrong.

---

```bash
python run.py verify-foreign --naive --score
```

**Verification-as-a-service: the four layers pointed at somebody else's matches.** They
are properties of a *claim* — "this credit is these payments" — not of this matcher, so
they hold whoever made it, and **none of the findings needs ground truth.** Point it at
an incumbent's Monday output with nothing labelled anywhere and it returns the specific
claims that do not survive conservation, subset-sum uniqueness, Fellegi–Sunter
contradiction, double-posting or window resolution.

Measured against a deliberately naive matcher that always assigns and never refuses (a
straw man, and [`src/external/naive_matcher.py`](src/external/naive_matcher.py) says so
in its first line):

| | reported | shifted holdout |
|---|--:|--:|
| the claimant's coverage | 100.00% | 100.00% |
| truth-free survival | 51.77% | 47.24% |
| its true precision, scored afterwards | 0.5390 | 0.5276 |
| **wrong claims missed** | **0** | **0** |
| correct claims flagged | 3 | 7 |

**Recall 1.0000 on both distributions with no answer key.** Every false alarm is
`identity_contradicted` — the same conservative refusal class the engine already
discloses. `python run.py verify-foreign` with no arguments audits *this* engine's own
output and passes 126/126, which is the control arm: an auditor that flags the matcher it
ships with has a bug in one of the two and no way to say which from the outside.

```bash
python run.py agent --offline
```

Reports **evidence-attributable coverage gain per source**, not per agent — each verdict
change credited to the dataset its proposal consulted, measured by re-running the engine
with only that source's evidence. A proposal citing nothing external is filed as
`model_assertion` and reported separately, because it may be right and it is still not a
dataset anyone can buy, re-read or audit.

`--dataset holdout` runs it against the shifted batch, and the comparison is the honest
shape of this feature:

| | reported | shifted holdout |
|---|--:|--:|
| exceptions in the baseline | 11 | 18 |
| closed by the register | **2** | **2** |
| match rate | 89.69% → 90.72% | 86.60% → 87.63% |
| released | ₹87,995.75 | ₹33,048.28 |
| precision | 1.0000 → 1.0000 | 1.0000 → 1.0000 |
| wrong assignments | 0 → 0 | 0 → 0 |

*(Reproduce with `run.py agent --dataset <batch> --offline --no-llm`. Both flags matter:
without `--no-llm` the engine picks up a live model when a key is in the environment and
reports a baseline nobody else can reproduce.)*

Same code, same investigator, and most of the declines on both batches are *"no register
entry"*. **The agent's value is mostly a property of how complete the merchant's register
is.** "A better register closes more exceptions at unchanged precision" is a claim we can
defend; "our agent closes exceptions" is a different sentence and we are not making it.

**This figure has now moved three times, none of them because the agent changed.** It was
3 and 1 before the settlement-group work, and it is 2 and 2 after — the agent closes name
conflicts, and every change to the deterministic layers changes which credits are still
carrying that category by the time it runs. Its contribution is a *residue*: what the
engine could not settle and the register happens to cover. Worth stating because a number
that moves when the thing it measures does not is a number to quote carefully.

**Offline runs a deterministic stand-in, not an investigation.** `RecordedInvestigator`
implements the decision procedure in code so the tools, the budget, the ledger, the
boundary checks and the re-run are all genuinely exercised with no network. It follows one
path because that path was written for it; a live model chooses its own. Every agent
figure prints `investigator=recorded` beside it so a recorded run is never mistaken for a
live one.

The UI is a single page: exceptions ranked by rupees at risk, each expanding to show
why the engine declined, what to do next, and — for ambiguous credits — every candidate
it refused to choose between.

Directly under the totals it qualifies sits a **"money out" panel**. The at-risk figure
counts refused *credits*, so a merchant reading "₹800 at risk" while ₹1,66,732 left the
account on lines nobody examined is being misled by omission — and the omission matters
more here than in the metrics block, because this page is what someone acts on.

That panel began life as a bare disclosure, back when the engine read credits only. The
right end state for a disclosure of that kind is that the engine goes and reads the lines,
and it does: every row now names the settlement it reverses, or says plainly that it
cannot be tied to one. It stays in the same place so an operator who learned to look
there still finds the same money, and debit lines are still never mixed into the worklist
— they are a separate ledger, not items to work. The API is **read-only by design**:
there is no accept /
reject endpoint, because a feedback loop is out of scope and a button that did nothing
would be worse than none.

```bash
pytest tests/
```

614 tests, including the end-to-end isolation test — which deletes the ground-truth
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
- [x] **Block 7** — Layer 3 Fellegi–Sunter (contradiction veto, unsupervised `u`)
- [x] **Block 8** — Layer 4 materiality (AS 2315) + composite confidence
- [x] **Block 8b** — BenchRec fitted 2026-09-04: ECE 0.0230 over 10/10 bins, n=40,001.
      Weights and `m` priors **deliberately not substituted** — BenchRec scores any
      candidate pair (base rate 0.202), this engine scores survivors of four layers
      (0.992). Engine weights stay UNCALIBRATED and labelled so; see
      [`OUTSTANDING_TASKS.md`](docs/OUTSTANDING_TASKS.md) W1
- [x] **Block 9** — LLM tier — changes reasons, and now one decision; see `DEFECT_LOG` 2026-09-03-01
- [x] **Block 10** — FastAPI + React exception triage UI

[`docs/DEFECT_LOG.md`](docs/DEFECT_LOG.md) records what broke during the build, as it
broke. [`docs/FLOWCHARTS.md`](docs/FLOWCHARTS.md) diagrams how the system actually
behaves — including the places where measurement contradicted the original design.
[`docs/OUTSTANDING_TASKS.md`](docs/OUTSTANDING_TASKS.md) lists what is knowingly
incomplete, including one claim the project deliberately **withholds** because the
evidence does not support it.

**The second withheld claim is now measured.** `python run.py llm-compare` ran against
live `claude-sonnet-5`: the tier contributes **+0.52pp coverage and one additional
correct assignment, with precision unmoved at 100.00%**. The more interesting result is
that across five runs the model filled 6–8 of 13 unreadable narrations — a 46–62% spread
— and produced **identical verdicts every time**. Model variance does not reach the
money, which is what the trust boundary was built to guarantee and is now measured rather
than asserted. See [`METRICS.md`](docs/METRICS.md).
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

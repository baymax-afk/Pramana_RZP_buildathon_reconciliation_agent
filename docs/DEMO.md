# Demo run-sheet

Six minutes. Lead with the money, show one concrete before/after, prove stability, then
show what it refused — in that order, because that is the order the page is now built in.

Every number below is printed by the commands in it. If one disagrees with the page,
the page is right and this file is stale — regenerate and fix it here.

---

## Before you start

```bash
python run.py match --verify --no-llm     # regenerates both artefacts, ~2 s
uvicorn api.main:app --port 8000          # terminal 1, leave running
cd ui && npm run dev                      # terminal 2 -> http://localhost:5173
```

Open the page once and check the header says **`verification gated`** and a recent run
time. If it says `NOT verification gated`, the artefact was produced without `--verify`
and the four-layer claim will render as a warning strip instead of a result.

Do **not** set `ANTHROPIC_API_KEY` for the demo. The served run is the deterministic arm
and reproduces bit-for-bit; the live tier moves one assignment in nine runs out of ten.

---

## 1 · Open on the outcome — 45 s

The first screen is the reconciliation summary. Read the top line off it:

> **₹36,70,814.44 reconciled and verified**, across **133 bank credits**, **174 payments**
> and **162 invoices** — from **538 records** across three sources.

Then the two numbers that qualify it, both on the same screen:

- **Match rate 89.69%** — 174 of 194 settleable payments.
- **Accuracy 100.00%** — 133 of 133 postings correct, **95% CI ≥ 97.26%**.

Say the bound out loud. *"133 observations support a 95% floor of 97.3%, not 99.9%. We
report the floor because the point estimate on a sample this size doesn't mean what it
looks like."* It is the line that separates this from a demo that quotes 100% and stops.

And read the amber line under it: **₹35,20,805.75 net** of seven chargebacks totalling
₹1,50,008.69. *"The matches stand — the settlements were correct and the money was clawed
back afterwards. That is two facts, so we report two numbers."* Two of the seven are
**partial** — one payment disputed inside a settlement batch of four, where the rest of
the batch still stands.

## 2 · One concrete before → after — 90 s

**Reconciled** tab, open row 1 (₹1,55,784.07).

- **Before:** one bank line nobody had claimed, and four open invoices —
  Bharati Traders, Quantum Instruments ×2, Greenfield Organics.
- **Decision:** *"settles 4 payments together from 4 customers… their expected amounts,
  after gateway fees and any TDS, add up to it exactly — and no other combination of the
  12 payments in this window does."*
- **After:** ₹1,55,784.07 settled, 4 payments, 4 invoices closed, balances to the paisa.

Two things worth saying here:

1. **This is the hard case, not the easy one.** A lump settlement that no single payment
   explains. **20 of the 133** credits are like this, covering **66 payments** between
   them — and three more are the mirror case: **one** payment arriving across several
   bank lines, matched as a group. Open the row marked **SPLIT** if there is time; one
   of them is a **four-way** split, and it is the case the engine refused until a week
   ago.
2. **"And no other combination fits" is the claim that matters.** Finding *an* answer is
   easy; proving it is *the* answer is why this refuses anything at all.

Click **"Show the engine's own numbers"** if someone wants the residual and the
uniqueness margin. Do not lead with it.

## 3 · It processes the batch, not a record — 30 s

Scroll the Reconciled tab. 128 rows over 133 credits — a split settlement is one row,
because the money moved once. Largest first. Then:

```bash
python run.py match --verify --no-llm     # the whole batch, end to end
```

Under a second for 534 records with all four verification layers on.

## 4 · Prove it is stable — 60 s

Bottom of the Reconciled tab, **Verification**:

> **Every match held when the records were shuffled.** The batch was reconciled 8 times
> over, each time with the bank lines in a different order, and all 133 matches came out
> the same.

Then say why it is not a formality: *"a reconciler that works through records in file
order can hand the same money to whichever candidate it saw first, and the wrong answer
looks exactly like the right one. Re-running in a different order is how that gets
caught — and anything caught is refused, not posted."*

Click **Show the raw metric** if they want `unstable 0/133, K=8`.

## 5 · What it refused, and why that is the point — 90 s

**Exceptions** tab. 15 rows, ₹3,01,909.22, ranked by money at risk.

Open the largest (₹48,020.17, *amount/name conflict*):

> The amounts reconcile exactly — residual **+0 paise** — but the payer on the bank line
> is `ACME INDUSTRIAL SU` and the invoice customer is `Deccan Pharma Distributors`.
> Two independent checks disagree, so it was left for a human.

The line to land: **"it would have been trivial to post this. The amounts are perfect.
We didn't, because the counterparty doesn't match — and a wrong post is worse than an
exception."**

Then the **Worklist** tab for ten seconds: the same 11 exceptions routed to 5 desks with
owners and turnaround times, materiality halving the clock. Say plainly that the routing
is a configured default and the categories are what is measured.

## 6 · Close — 45 s

Pick two:

- **The ceiling.** *"92.27% is the maximum this data permits — 15 of the 20 unmatched
  payments never settled or belong to a relation we don't model. We are 5 payments from
  the ceiling, and here they are, named."* (Bottom of the Reconciled tab.)
- **Verification-as-a-service.** `python run.py verify-foreign --naive --score` — point
  the four layers at somebody else's matches. On a straw-man matcher it catches
  **65 of 65 wrong claims with no ground truth read**, and **60 of 60** on a shifted
  holdout.
- **Where it connects next.** *How to read this* → the engine takes three typed record
  sets and returns verdicts; it has never seen a file or a vendor. A new source is a
  loader.

---

## Questions you will get

**"Is 100% precision just an easy dataset?"**
Partly, and we say so. Precision holds at 1.0000 across four density arms and on a shifted
holdout, but every batch comes from one generator. The 95% floor is 97.1%. On real data we
would expect precision to fall and the refusal rate to rise — the architecture is built for
that and we cannot prove it here.

**"Where is the agent?"**
`python run.py agent`. A tool-calling loop over the exception list with typed read-only
tools, a step budget and an append-only evidence ledger. It cannot name a payment or
return a verdict — it asserts a fact, and the deterministic engine re-runs and decides.
Be straight about the size: **3 exceptions closed on the reported batch, 1 on the shifted
one.** That difference is register coverage, not cleverness, which is why we report gain
per evidence *source* rather than per agent.

**"Is the LLM doing the work?"**
No, and it is measurable: `run.py llm-compare` runs both arms. The served demo has the
tier off entirely.

**"What about chargebacks / split settlements?"**
Both were outside the model until 2026-09-04, and both are in it now — which is the more
interesting answer, because **one of our own "this needs a different engine" arguments
was wrong.** `ARCHITECTURE.md` had said split settlements would need fractional claims
and a search over partitions. They do not: a part-settlement is a *group* relation that
balances exactly, so raising the claim unit from one credit to a group of credits
expresses it with integer arithmetic and the uniqueness question Layer 2 already answers.
Nobody wants half a payment posted.

Chargebacks were closer: a debit does ask a different question, so it gets its own
module. What was wrong was the claim that the engine would have to *un-post* the
settlement. It does not — both events happened — so the assignment stands and the batch
reports reconciled gross and net. All **6 of 6** chargebacks are tied to the settlement
they reverse, and the *not examined* line now reads **zero**.

Those two limitations were themselves lifted the same day: splits resolve to six-way now
and a partial chargeback reverses the payment subset it names. **That afternoon also
produced the engine's only wrong assignment**, and it is the better half of the story:
widening the model widened what could be grouped, two *ambiguous* credits got rolled into
a coincidental group, and precision read 0.9963 at one density. The density sweep caught
it; the fix was the eligibility rule, not the group test — a credit with several viable
decompositions is ambiguous, not unexplained, and grouping it adds a possibility rather
than resolving one. Zero wrong assignments across 4 densities × 5 seeds after.

The cost, if asked: 15 exceptions became 11, precision 1.0000 throughout at a larger
sample, and three *new* named limitations in place of the two — all in
`ARCHITECTURE.md`.

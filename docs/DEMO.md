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

> **₹36,37,182.79 reconciled and verified**, across **126 bank credits**, **172 payments**
> and **161 invoices** — from **534 records** across three sources.

Then the two numbers that qualify it, both on the same screen:

- **Match rate 88.66%** — 172 of 194 settleable payments.
- **Accuracy 100.00%** — 126 of 126 postings correct, **95% CI ≥ 97.11%**.

Say the bound out loud. *"126 observations support a 95% floor of 97.1%, not 99.9%. We
report the floor because the point estimate on a sample this size doesn't mean what it
looks like."* It is the line that separates this from a demo that quotes 100% and stops.

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
   explains. **20 of the 126** credits are like this, covering **66 payments** between
   them.
2. **"And no other combination fits" is the claim that matters.** Finding *an* answer is
   easy; proving it is *the* answer is why this refuses anything at all.

Click **"Show the engine's own numbers"** if someone wants the residual and the
uniqueness margin. Do not lead with it.

## 3 · It processes the batch, not a record — 30 s

Scroll the Reconciled tab. 126 rows, largest first. Then:

```bash
python run.py match --verify --no-llm     # the whole batch, end to end
```

Under a second for 534 records with all four verification layers on.

## 4 · Prove it is stable — 60 s

Bottom of the Reconciled tab, **Verification**:

> **Every match held when the records were shuffled.** The batch was reconciled 8 times
> over, each time with the bank lines in a different order, and all 126 matches came out
> the same.

Then say why it is not a formality: *"a reconciler that works through records in file
order can hand the same money to whichever candidate it saw first, and the wrong answer
looks exactly like the right one. Re-running in a different order is how that gets
caught — and anything caught is refused, not posted."*

Click **Show the raw metric** if they want `unstable 0/126, K=8`.

## 5 · What it refused, and why that is the point — 90 s

**Exceptions** tab. 15 rows, ₹3,01,909.22, ranked by money at risk.

Open the largest (₹48,020.17, *amount/name conflict*):

> The amounts reconcile exactly — residual **+0 paise** — but the payer on the bank line
> is `ACME INDUSTRIAL SU` and the invoice customer is `Deccan Pharma Distributors`.
> Two independent checks disagree, so it was left for a human.

The line to land: **"it would have been trivial to post this. The amounts are perfect.
We didn't, because the counterparty doesn't match — and a wrong post is worse than an
exception."**

Then the **Worklist** tab for ten seconds: the same 15 exceptions routed to 5 desks with
owners and turnaround times, materiality halving the clock. Say plainly that the routing
is a configured default and the categories are what is measured.

## 6 · Close — 45 s

Pick two:

- **The ceiling.** *"91.24% is the maximum this data permits — 17 of the 22 unmatched
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
Outside the model, disclosed on the page as *not examined* (6 debit lines, ₹1,66,732.77).
Lifting either needs a different engine — a signed transaction model and a bipartite
residual model — and we would rather name it than have it found.

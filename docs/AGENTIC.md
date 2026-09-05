# Making it agentic

**Rings 2 and 3 are now built.** `python run.py agent`. This document was written as a
design note before any of it existed, and the design survived contact with the
implementation almost intact -- so it is kept as written below, with what shipped
recorded here and the numbers it was originally written against left alone rather than
retro-fitted. The stale figures in the sections that follow are the ones that motivated
the design, not the ones it produced.

---

## What shipped, and what it measured

| | Offline (`--offline`) | Live (`claude-sonnet-5`) |
|---|---|---|
| match rate | 88.66% → **90.21%** | 88.66% → **90.21%** |
| match precision | 1.0000 → **1.0000** | 1.0000 → **1.0000** |
| evidence assertions | 3 | 4 |
| verdicts moved | 3 | 3 |
| gain per assertion | 1.00 | 0.75 |
| exceptions declined | 12 | 11 |
| wall clock | **0.06s** | ~4 min |

**The two arms reach the same headline by different routes, and the difference is the
result worth reporting.** The live model closed one case the coded procedure declines:
the register reads `'Pinnacle Steel Traders'` where the ledger reads `'Pinnacle Steels
Traders'`, and `_same_entity` correctly refuses that — the words differ *internally*,
which is precisely how the generator's planted confusable pairs differ (`'Bharat
Traders'` against `'Bharati Traders'`). The model recognised a spelling variant, asserted
anyway, and was right; precision did not move. That is the case for putting a brittle
rule inside a model rather than in code, measured rather than argued — and it is also
why the rule stays strict: it is what stops the same latitude from merging two real
companies.

Its refusals are its own words and they are correct. On arithmetic exceptions it says a
payer register has nothing to say about a credit no subset of payments accounts for. On a
payer absent from the register it says absence is not disproof.

### The three things that make the number trustworthy

**The null-agent control.** `--null-agent` investigates nothing and must reproduce the
baseline byte for byte. Every figure above is a delta against a run anybody can reproduce
without an agent.

**Evidence, not edits.** Nothing patches a verdict. Proposals go into an append-only
ledger, `match_once` runs again over the same three sides plus the evidence, and the
engine reaches its own conclusion — which is still a refusal for most of them. Precision
therefore remains a property of the engine.

**Attribution.** Every accepted assertion carries the tool calls that produced it, so a
moved verdict traces to a named fact rather than to an agent's opinion. `gain per
assertion` is the honest denominator: an agent that asserts more without moving more
scores *worse*, which is why the live arm's 0.75 is reported next to the offline arm's
1.00 rather than quietly dropped.

### What it cannot do, structurally

`EvidenceProposal` carries no payment id, no candidate, no score and no verdict — and,
learning from `REVIEW.md` §5, that is not left as an absence. The value is rejected if it
matches the shape of a payment id, order id, bank transaction id or UTR, because the
audit showed a free-text field one hop from a record identifier is a way to name a
record. The field itself is an enum, so a channel the engine never agreed to weigh is
refused loudly instead of accepted and ignored.

The evidence enters Layer 3 as one named Fellegi–Sunter comparison and the existing
contradiction gate decides. A credit refused on the arithmetic stays refused no matter
what is asserted about names.

---

## What shipped second: eight channels, three specialists, and a rule that cost two
## attempts to find

The first version had one evidence channel (`authorised_payer_for`), one investigator, and
no routing. It worked, and its ceiling was structural: the channel enters Layer 3 as a
name comparison, so it can stop a contradiction veto on a credit whose arithmetic ALREADY
balanced and it can do nothing else. Every arithmetic refusal got a polite decline.

### The eight channels

| field | value | carries money | reaches |
|---|---|:--:|---|
| `authorised_payer_for` | a ledger customer name | no | Layer 3, as one Fellegi–Sunter comparison |
| `refund_status` | `none` / `partial` / `full` | yes | `fees.known_deductions` |
| `tds_confirmed` | `withheld` / `not_withheld` | yes | `fees.known_deductions` |
| `credit_note_confirmed` | `issued` / `none` | yes | `fees.known_deductions` |
| `bank_charge_confirmed` | `levied` / `none` | yes | `fees.known_deductions` |
| `invoice_part_payment` | `short_paid` / `paid_in_full` | yes | `fees.known_deductions` |
| `settlement_date_confirmed` | an ISO date | no | the candidate window's anchor |
| `chargeback_status` | `none` / `raised` / `won` / `lost` | no | the reversal ledger |

**Money never travels in `value`.** It goes in `amount_paise` as an integer, so `value` is
always a name, a date or a token from a fixed vocabulary — which makes "reject a value
that looks like a score" a one-line check rather than a judgement, and means a channel
cannot smuggle a number through the text field at all.

**The five deduction channels are a different kind of thing from the first one, and the
document should say so plainly.** `authorised_payer_for` argues about a name. These change
what the engine expects the bank to have credited, which changes what the subset search
can find. That is the amount channel — the one this engine treats as primary.

They are admitted because the alternative is worse and this project has already proved it.
`DEFECT_LOG` 2026-09-02-05 item 4 is a batch where money was deducted and recorded
**nowhere**: five credits refused on arithmetic, every one scored as a miss. The tempting
fix was a wider tolerance. The correct fix was to stop hiding the money — and all five
then matched with no change to the matcher. A deduction an investigator goes and finds,
with a source, is that same fix arriving by a different route.

### The rule that makes them safe, and the two attempts it took to find it

**Attempt one.** The invoice specialist asked the engine for the residual on a refused
candidate and asserted it as a short payment. Every step was defensible. The composite was
circular: a figure taken from the gap will always close the gap. It bought four payments
of coverage and two wrong postings — **precision 1.0000 → 0.9854**. This is
prohibition 4, *explain your way past a refusal*, arriving as arithmetic instead of prose.

**Attempt two.** Require the ledger to corroborate: only assert a shortfall against an
invoice the ledger marks `part_settled`. Principled, and still wrong — a status says a
shortfall happened, not how large it was, so the amount was still coming from the
residual. The primary batch stopped showing it and the holdout caught it:
**1.0000 → 0.9913**, one wrong posting, the same mechanism wearing a corroborating flag.

**The rule that survived.** *A deduction is admissible when a record NAMES the figure*, and
a figure the engine already subtracts is a restatement rather than evidence.

### What that costs, stated rather than engineered around

These three sides name exactly two deducted amounts: `Invoice.tds_amount` and
`Payment.amount_refunded`. `fees.known_deductions` already reads both. There is no
settled-to-date column, no credit-note line, and no bank-charge field.

> **So on these batches, no deduction channel can be accepted at all.** The machinery is
> built, validated and tested; the ledger cannot feed it.

That is a fact about the generator's invoice schema, not a fault in the channel, and it
changes the moment a real ERP export carries a credit-note line. Reporting it is worth
more than a coverage number bought by relaxing `agent/validate.py`. What the specialists
produce on this data instead is an **evidenced exception**: the gap quantified, the
invoice named, the records that were read listed — which `docs/AGENTIC.md` argued for from
the start as the second of two wins.

### The three specialists, and what each may assert

* **`PaymentInvestigator`** — the gateway record: status, capture, method, refunds.
  Asserts `refund_status`, `chargeback_status`.
* **`BankInvestigator`** — the statement: narration, references, duplicate lines, value
  dates. Asserts `settlement_date_confirmed`, `bank_charge_confirmed`.
* **`InvoiceInvestigator`** — the ledger: invoice status, TDS, credit notes, part
  payments, and the authorised-payer register. Asserts `tds_confirmed`,
  `credit_note_confirmed`, `invoice_part_payment`, `authorised_payer_for`.

`RecordedInvestigator` keeps its name and becomes a router over the three, so every
reported offline figure still refers to the same thing. `ClaudeInvestigator` takes a
`role` and appends a role brief to the system prompt — one class parameterised, not three
copies of a tool loop. `ClaudeFleet` is one live investigator per role.

**Several agents may investigate; only the engine decides.** There is no vote, no
confidence-weighted merge, no tie-break. Each specialist proposes evidence, the ledger
records what the boundary accepts, and `match_once` runs once over all of it. A fleet that
agreed with itself would look exactly like a fleet that was right.

### Routing, and the categories that get none

| refusal category | routed to |
|---|---|
| `amount_name_conflict` | `InvoiceInvestigator` |
| `unexplained_residual` | `PaymentInvestigator`, then `BankInvestigator` |
| `no_subset_fits` | `InvoiceInvestigator`, then `PaymentInvestigator` |
| `pool_exceeded` | `BankInvestigator` — window and data scope only |
| `multiple_candidates`, `ambiguous_grouping`, `contested_payment`, `solution_cap_reached`, `order_dependent_assignment`, `narration_count_conflict`, `no_candidate` | **never** |

**Two categories in the original brief do not exist in this engine, and inventing them
would have been the easy mistake.**

`partial` is a ground-truth `Relation` and a `Reversal.partial` flag; `match_once` cannot
emit it. A customer who short-pays arrives as `no_subset_fits` with the closest subset a
few hundred paise short — `bank_txn_0056` at `−498p` in the reported batch. So the routing
key is `no_subset_fits`.

`duplicate_reference` is a generator defect label (`duplicate_utr`), not a refusal.
Duplicate UTRs reach the engine as `multiple_candidates` or `contested_payment` — ties,
and breaking a tie is prohibition 1. So duplicate detection ships as a **read**
(`get_bank_line` returns the lines sharing a reference) that a specialist may consult
while working another question. An agent can see the duplicate; there is no path by which
seeing it resolves the tie.

Debit-side categories are out of scope: debits reach a verdict through the reversal ledger
rather than the matcher, and none is investigated today.

### The window: anchor, never width

`settlement_date_confirmed` moves where the candidate window is counted FROM. It does not
make it wider. `tier2_amount_date.window_for`'s own docstring forbids per-record widening
— "widening the window for a stubborn credit would be exactly the sort of per-record
tuning `docs/METRICS.md` forbids" — and that stands. Re-anchoring cannot be used that way:
moving the window discards as many days as it gains, and the validator will not accept a
settlement date later than the credit or further back than `LOOKBACK_DAYS + 10`.

### The validation layer

`agent/validate.py` is deterministic, checks eight things in order, and **reaches no
verdict** — a test parses its AST and fails if it imports `match_once`, `Verdict` or
`Assignment`, or mentions an assignment map. Shape; identifier/verdict/number shapes;
the transaction exists; no duplicate on the channel; a source that resolves AND is
reachable from this credit; not stale; corroborated by the cited record; bounded by the
credit.

Staleness is measured against **the batch's latest bank date**, never a wall clock. Two
reasons, and the second is the one that matters: a clock would make every test race the
calendar, and "is this stale" means "was it read after the money moved", which is a
question about the batch.

### Budgets, versions, and the approval gate

Three budgets — total investigations, per-investigator investigations, total tool calls.
The module docstring used to promise a global budget that did not exist; it does now.

Results are **versions**, not overwrites: `v0` baseline, `v1` enriched, and any subsequent
version produced by holding material changes back or withdrawing evidence. Each records
its evidence set and a hash of its assignment map. The baseline is always kept.

A newly-assigned credit at or above `cfg.MATERIALITY_PAISE` is **held for human
approval** — reported, and its evidence withheld from the applied result unless
`--approve-high-value` is passed. Materiality rather than a new number: PCAOB AS 2315 is
the line Layer 4 already uses to decide what is verified in full rather than sampled, and
inventing a second one here would have been a threshold chosen to make the auto-applied
set look good. The engine's verdict is unchanged either way; what waits is the posting.

**Holding means withholding the evidence and re-running, never patching the result.** A
held posting is one the engine was not given the evidence for — a state it can reach on
its own. Editing an assignment out of a `MatchOutput` is the one thing no part of this
system does.

The precision interlock lives in the CLI, not the loop: the trigger is precision,
precision needs ground truth, and `recon.agent` may not read it. The orchestrator produces
versions and their outputs; the scorer measures them; `cmd_agent` withdraws the evidence
behind any version that costs precision, re-runs, and exits non-zero if it still falls.

---

## The question that has to be answered first

The project's central constraint is one sentence:

> **Deterministic code decides every match. No LLM output ever creates, confirms, or
> scores a match assignment.**

That is enforced structurally — `NarrationFields` has no field for a payment id, a
candidate, or a score, so a model cannot NAME a record and cannot post a match whose
arithmetic fails. Any agentic design that quietly relaxes this is not an extension of the
project, it is an abandonment of its argument.

*(Stated more carefully than this document originally did. "Cannot express a matching
preference even in principle" was too strong: `merchant_ref` reaches a payment at one hop
through `ReferenceIndex`, and tier 1 outranks every other tier. The absence of a
payment-id field bounds what a model can say, not everything it can affect — see
`REVIEW.md` §5. The design below is unchanged by the correction, because it never relied
on the stronger claim: `EvidenceProposal` validates its value against the shape of every
identifier in the batch precisely so that one hop is closed.)*

So the question is not "where can we add an agent." It is:

> **What is there for an agent to do, if it may never decide a match?**

The answer turns out to be: almost everything except the verdict. And the trust boundary
is what *enables* the autonomy rather than what limits it — because a system with a
runtime oracle can let an agent loop unsupervised, and a system without one cannot.

---

## The one lever an agent is allowed to pull

There is exactly one honest way for an agent to change a verdict:

> **Supply evidence the engine did not have, and re-run it.**

Not override. Not tie-break. Not nudge a threshold. *Change the inputs, re-run the
deterministic engine, and let it reach its own conclusion.*

This is not a theoretical nicety — it is the shape of a defect this project already
hit. `DEFECT_LOG` 2026-09-02-05 item 4: the generator deducted ₹50–500 from a credit to
simulate a netted refund and recorded that money **nowhere**. All 5 such credits were
refused, every one scored as a miss. The tempting fix was to relabel them. The correct
fix was to stop hiding the money — refunds now land on `payment.amount_refunded` and the
engine treats them as a known deduction. **All 5 then matched, with no change to the
matcher at all.**

That is the template. An exception is usually not the engine being stupid. It is the
engine correctly reporting that *the evidence in front of it is insufficient* — and the
missing evidence usually exists somewhere the engine was never given access to.

---

## Where the remaining ₹2,53,889 actually is

| Exception | Count | ₹ at risk | What is actually missing |
|---|---:|---:|---|
| `amount_name_conflict` | 4 | 1,02,981 | Authority. Mostly third-party payers — a parent settling a subsidiary's invoice, where the amount is right and the name is not |
| `decomposition_out_of_bounds` | 6 | 1,00,350 | A deduction nobody told the ledger about — bank charges, or a split settlement the model cannot express |
| `unexplained_residual` | 3 | 49,757 | The reference identifies a payment; the money does not agree |
| `multiple_candidates` | 1 | 800 | Nothing. **This is the hand-placed ambiguity case, and refusing is correct.** |

Plus **6 debit lines worth ₹1,66,733** that are not on this list at all, because the
engine reads credits only and discloses them instead of scoring them.

Read that table again: **thirteen of the fourteen exceptions are missing-evidence
problems, and the fourteenth is a case where refusing is right and no agent should touch
it.** That distribution is the entire argument for the design below — and it held when
the batch had 6 exceptions and it holds now that it has 14.

The largest single bucket is now `amount_name_conflict`, and it is the most
agent-shaped of all of them: the engine has correctly established that the money
reconciles and the counterparty does not. Deciding whether a parent company is
authorised to settle a subsidiary's invoice is not an arithmetic question and never will
be. It is a lookup — exactly the evidence a Ring 2 investigator would fetch.

---

## Four rings, and what may live in each

```
        ┌─────────────────────────────────────────────────────────┐
        │  RING 3   ORCHESTRATION      when to run, what to run   │
        │  ┌───────────────────────────────────────────────────┐  │
        │  │  RING 2   INVESTIGATION    gather missing evidence │  │
        │  │  ┌─────────────────────────────────────────────┐  │  │
        │  │  │  RING 1  VERIFICATION   the runtime oracle   │  │  │
        │  │  │  ┌───────────────────────────────────────┐  │  │  │
        │  │  │  │  RING 0   THE VERDICT                 │  │  │  │
        │  │  │  │  deterministic. NO AGENCY. EVER.      │  │  │  │
        │  │  │  └───────────────────────────────────────┘  │  │  │
        │  │  └─────────────────────────────────────────────┘  │  │
        │  └───────────────────────────────────────────────────┘  │
        └─────────────────────────────────────────────────────────┘
```

### Ring 0 — the verdict. No agency, ever.

`match_once` stays a pure function of `ReconInputs`. This is not negotiable, and it is
what makes every outer ring safe: whatever an agent does, the answer is still produced by
code a human can read, re-run, and diff.

### Ring 1 — verification. Already agentic in the only sense that matters.

This is the part most reconciliation products do not have, and it is what makes
autonomous operation defensible.

The four layers — metamorphic relations MR1–MR6, the K=8 permutation gate, subset-sum
uniqueness, Fellegi–Sunter evidence weights — **are a runtime oracle that needs no
labels**. That is precisely the property an agent loop needs and almost never has.

> The usual reason you cannot let an agent run unsupervised on financial data is that
> nothing can tell it whether it just did something stupid. Here, something can. The
> permutation gate does not care that an agent supplied the new input; it re-checks
> order-independence either way.

So Ring 1's role in an agentic system is **the termination condition and the safety
interlock**, not a place to put a model. Concretely: an agent may loop as long as it
wants, provided every loop iteration ends with `run.py match --verify` and the gate
passing. Precision is not the agent's to optimise; it is the agent's to not damage.

### Ring 2 — investigation. This is where the agent earns its keep.

Today the exception list is *static*: six rows, ranked by rupees, each explaining why the
engine declined. A human then does the investigation by hand.

An investigating agent takes each exception and gathers evidence — with **read-only**
tools, and one carefully bounded write:

| Tool | Kind | What it answers |
|---|---|---|
| `razorpay.payments.fetch` (MCP) | read | Is there a refund, a dispute, a settlement note the ledger missed? |
| `razorpay.settlements.fetch` (MCP) | read | Did Razorpay net something into this batch? |
| ledger lookup, widened window | read | Is the missing payment just outside `LOOKBACK_DAYS`? |
| invoice search across periods | read | Was this invoice part-settled last month? |
| **`upload_invoice_ledger`** | **write to INPUT** | Replace side C, then **re-run the engine** |
| draft payer email | write, human-gated | "We received ₹X against invoice Y, ₹Z appears short" |

### The precondition this design assumed without saying so

A Razorpay MCP connector was live in a session on 2026-09-03, and it could not do any of
the above. Both calls failed:

```
fetch_payment(pay_TWewgg8dNUUSrb)  → The Merchant is not activated
fetch_all_payments()               → Authentication failed
```

The connector authenticated as a **different merchant** from the test-mode account that
produced this project's R1 records. So Ring 2 needs something narrower than "a Razorpay
connector is available": it needs **an activated merchant on the same account as the
data**. A connector that answers is not the same as a connector that can see your
payments, and the difference is invisible until you make a call — the tool list looks
identical either way.

Worth stating because it is the cheapest possible thing to get wrong when planning this:
the investigation ring is not blocked on model capability or on design, it is blocked on
an account setting. That is also good news, because account settings are fixable and
architectures are not.

### The write is bounded

The write is the point, and its bounding is the point. `api/invoices.py` already exists
and is already scoped this way: it replaces *input data*, never a verdict, and **the
engine must be re-run for an upload to change anything**. That endpoint was built for a
human. It is exactly the right shape for an agent.

**Worked example — the 4 `amount_name_conflict` cases, ₹1,02,981.** The engine has
established that the amounts reconcile exactly and the payer name does not. An agent
looks the payer up: is `Northwind Holdings` the parent of `Northwind Logistics`? It checks
the customer registry, the GSTIN prefix, prior settled invoices from the same contact. If
the relationship is on file it uploads the corrected counterparty mapping and re-runs;
the engine then has reference evidence that outweighs the name disagreement, and assigns
on its own. If the relationship is *not* on file, it produces an evidenced exception —
"this payer has never settled for this customer before" — instead of a bare one.

**Both outcomes are wins, and neither required the agent to have an opinion about the
match.** Note what makes this tractable: the engine already tells us the split. A
third-party payment that quotes an invoice reference reconciles today; one that does not
is escalated. The agent's job is to supply what the quote would have supplied.

### Ring 3 — orchestration. The boring ring that makes it a product.

Watch for a new settlement file. Run generate/match/verify. Diff today's exception list
against yesterday's. Escalate what is new, stay quiet about what is unchanged. Re-run when
a ledger upload lands. This is scheduling and plumbing; it needs an agent only insofar as
"decide what a human should be told" is a judgement call.

---

## The self-improving loop that is actually safe

One more use, and it is the one I would build second:

**Let an agent propose parser rules, and let the test suite referee.**

There are 13 credit narrations the regex tier cannot read — all of them missing a
merchant reference, none missing a payer name (measured by `run.py llm-compare`). An
agent that reads those 13, proposes a new pattern for `normalize.py`, and runs the suite
is *writing code, gated by tests* — including the metamorphic relations, which will
catch a "fix" that changes verdicts it should not have.

This is safe for a specific reason worth naming: **the agent's output is a diff a human
reviews, and its proposal is falsified by an oracle before anyone sees it.** Contrast
with an agent that resolves an ambiguous credit at runtime, where there is no oracle,
no diff, and no review — just money in the wrong account.

---

## Five things an agent must never be allowed to do

These are the failure modes, stated as prohibitions because they are all *tempting* and
all *plausible-looking*:

1. **Break a tie.** `_resolve_contested` refuses when two credits have equal evidence,
   and Layer 2 refuses when two subsets both fit. An agent asked "which of these two is
   more likely?" will always produce an answer. That answer is a guess wearing the
   costume of analysis, and the whole project exists to argue against it.

2. **Tune a threshold to improve a metric.** `config.py` is frozen before the run for
   exactly this reason. An agent that nudges `TOL_ABS_PAISE` until the exception list
   looks better has not reconciled anything; it has widened the net until coincidences
   qualify. If an agent may touch config, it may only do so *before* seeing any metric,
   and the change must be justified by something other than the metric.

3. **Read ground truth.** The audit hook and the isolation test exist. An agent runs
   inside the same process boundary and is bound by the same hook — but it should also
   never be *handed* the truth path in a prompt. `_truth` is not the agent's business.

4. **Explain its way past a refusal.** The LLM writes prose *after* the verdict. An agent
   producing a persuasive rationale for why a refusal "should really be" an assignment is
   generating exactly the artefact that makes a wrong answer survive review.

5. **Loop without the oracle.** Any agent iteration that does not end in
   `match --verify` is unsupervised change to financial data. The gate is cheap
   (~0.1s parallel at K=8, and every pass is independent). There is no excuse.

---

## What I would build first, and what it would prove

**Build:** the Ring 2 investigator, over the 4 `amount_name_conflict` cases and the 3
`unexplained_residual` ones — 7 exceptions, ₹1,52,739, all of them missing-evidence
problems, and none of them solvable by any change to the matcher.

**The claim it would let the project make**, which no current metric supports:

> Coverage rose from its current figure to X% **without precision moving off 1.0000**, and every
> point of that rise is attributable to a named piece of evidence an agent went and
> found — not to a threshold that was loosened.

That is a genuinely different claim from "our agent reconciles 97% of transactions,"
because it is decomposable. Every recovered match names the document that recovered it.

**The metric that would keep it honest:** *evidence-attributable coverage gain*. Every
assignment an agent's evidence unlocked must cite the artefact that unlocked it. An
assignment that appears without a citation is a bug in the agent, and it is detectable
by construction — because the input diff is right there.

---

## The one-line version

**The trust boundary is not the obstacle to making this agentic. It is the precondition.**
Agency is safe here precisely because there is a deterministic verdict to be checked
against and a labels-free oracle to check it — so an agent can be given real autonomy over
*evidence*, and none at all over *conclusions*.

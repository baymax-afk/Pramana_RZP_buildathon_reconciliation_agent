# Making it agentic

*A design note, not a shipped feature. Re-grounded 2026-09-03 against the run at seed
20260905: 127 assignments, precision 1.0000, **14 exceptions worth ₹2,53,889**, plus
**₹1,66,733 on 6 debit lines the engine structurally cannot read.***

> **What changed since this was first written, and why it strengthens the argument.**
> The original worked example was the five `partial` cases at 0/5 recall. They are gone —
> not because an agent found evidence, but because the *generator* was hiding money
> (`DEFECT_LOG` 2026-09-02-08), and partial recall is now 7/7. The example was solved by
> fixing the data, which is precisely the thesis below: an exception is usually the engine
> correctly reporting that the evidence in front of it is insufficient.
>
> The exception list has since grown from 6 to 14, because the batch became honest — it
> gained bank charges, third-party payers, split settlements and chargebacks. **More
> exceptions on better data is the right direction**, and it gives Ring 2 more to work on
> rather than less.

---

## The question that has to be answered first

The project's central constraint is one sentence:

> **Deterministic code decides every match. No LLM output ever creates, confirms, or
> scores a match assignment.**

That is enforced structurally — `NarrationFields` has no field for a payment id, a
candidate, or a score, so a model cannot express a matching preference even in
principle. Any agentic design that quietly relaxes this is not an extension of the
project, it is an abandonment of its argument.

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

> Coverage rose from 89.18% to X% **without precision moving off 1.0000**, and every
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

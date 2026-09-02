# Making it agentic

*A design note, not a shipped feature. Written against the run at seed 20260905:
129 assignments, precision 1.0000, **6 exceptions worth ₹57,775**, and 5 conservative
refusals the engine got wrong by declining.*

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

## Where the remaining ₹57,775 actually is

| Exception | Count | ₹ at risk | What is actually missing |
|---|---:|---:|---|
| `unexplained_residual` | 4 | 49,573 | A deduction nobody told the ledger about |
| `decomposition_out_of_bounds` | 1 | 7,401 | A pool of >20 — or a payment outside the window |
| `multiple_candidates` | 1 | 800 | Nothing. **This is the hand-placed ambiguity case, and refusing is correct.** |

Plus the 5 `partial` cases (recall **0/5**), where the engine refuses because a customer
short-paid and nothing on the three sides says so.

Read that table again: **five of the six exceptions are missing-evidence problems, and
the sixth is a case where refusing is the right answer and no agent should touch it.**
That distribution is the entire argument for the design below.

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

The write is the point, and its bounding is the point. `api/invoices.py` already exists
and is already scoped this way: it replaces *input data*, never a verdict, and **the
engine must be re-run for an upload to change anything**. That endpoint was built for a
human. It is exactly the right shape for an agent.

**Worked example — the 5 `partial` cases.** The engine refuses because ₹9,400 arrived
against a ₹10,000 invoice and nothing explains ₹600. An agent fetches the payment from
Razorpay, finds no refund, checks the invoice for a credit note, finds one issued after
the invoice was cut, uploads the corrected ledger, and re-runs. Either the engine now
assigns — because the evidence changed — or it still refuses, and the agent has produced
an *evidenced* exception for a human instead of a bare one. **Both outcomes are wins, and
neither required the agent to have an opinion about the match.**

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

**Build:** the Ring 2 investigator, over the 5 `partial` cases and the 4
`unexplained_residual` ones — 9 exceptions, ₹49,573, all of them missing-evidence
problems.

**The claim it would let the project make**, which no current metric supports:

> Coverage rose from 92.78% to X% **without precision moving off 1.0000**, and every
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

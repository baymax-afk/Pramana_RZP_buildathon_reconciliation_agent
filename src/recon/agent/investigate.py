"""
Ring 2: the investigator. One exception at a time, under a budget.

**What it is allowed to conclude.** Either "here is a fact, with the tool calls that
found it" or "the evidence does not support an assertion". The second is a correct
outcome and is reported as one -- `docs/AGENTIC.md` argues that an agent which never
says "I don't know" is worse than one with a lower match rate, and the authorised-payer
register is deliberately incomplete precisely so that answer has to be reachable.

**Three independent bounds, because one is not enough.**

  * a per-exception STEP BUDGET, so a confused investigation ends rather than wanders;
  * NO-PROGRESS detection -- two consecutive turns that call nothing new stop it, which
    catches the loop a step budget alone would let run to its cap;
  * the tier's own global call cap and timeout (`llm/claude.py`), which bound wall clock
    on a dead network rather than turn count.

**A malformed tool call gets one retry, then that exception is abandoned.** Retrying
forever on a model that cannot produce valid arguments is how a batch run becomes a
bill. Abandoning one exception costs one exception.
"""

from __future__ import annotations

import json

import config as cfg

from .schemas import InvestigationTrace
from .tools import TOOL_SPECS, Toolbox

SYSTEM_PROMPT = """You are an accounts-receivable investigator working one unresolved \
bank credit at a time.

A deterministic reconciliation engine has already REFUSED to post this credit. That \
decision is not yours to overturn and you cannot post anything. Your job is narrower and \
more useful: find out whether there is EVIDENCE the engine did not have.

The engine will be re-run with whatever you assert, and it will reach its own verdict \
again -- which may still be a refusal. You are widening its evidence, not making its \
decision.

How to work:

1. Read the exception. Note which channel the engine objected on.
2. Look at the candidate pool. The engine's own date-window blocking produced it.
3. If the objection is that the payer NAME on the bank line disagrees with the customer \
on the invoice, check the authorised-payer register for that payer name. Bank statements \
truncate names, so pass the name as it appears.
4. If the register confirms the payer is permitted to settle for the customer on one of \
these payments, assert `authorised_payer_for` with THAT CUSTOMER'S NAME.
5. If it does not, or if the objection was about amounts rather than names, assert \
nothing and say so.

Rules that matter:

- `value` must be a customer name. A payment id, order id or bank reference will be \
rejected -- you are not permitted to name a record.
- The register is NOT exhaustive. No entry means the relationship is unrecorded, not \
that the payer is unauthorised. Do not assert on absence.
- One assertion per credit, at most. If you are unsure, make none: an unresolved \
exception on a human's desk costs far less than a wrong posting.
- Do not guess a customer name. Read it from the candidate pool.

When you are done -- whether you asserted something or not -- reply with one short \
sentence saying what you concluded."""


def _dispatch(tb: Toolbox, name: str, args: dict):
    """Route one tool call. Unknown names are an error the model can read and correct."""
    if name == "get_exception":
        return tb.get_exception(args.get("bank_txn_id", ""))
    if name == "get_candidate_pool":
        return tb.get_candidate_pool(args.get("bank_txn_id", ""))
    if name == "test_subset":
        return tb.test_subset(
            args.get("bank_txn_id", ""), tuple(args.get("payment_ids") or ())
        )
    if name == "lookup_payer_relationship":
        return tb.lookup_payer_relationship(args.get("payer_name", ""))
    if name == "search_invoices":
        return tb.search_invoices(args.get("query", ""), int(args.get("limit", 10)))
    if name == "propose_evidence":
        return tb.propose_evidence(
            args.get("bank_txn_id", ""),
            args.get("field", ""),
            args.get("value", ""),
            args.get("rationale", ""),
        )
    return {"error": f"no tool named {name!r}"}


def _as_json(result) -> str:
    if hasattr(result, "as_dict"):
        return json.dumps(result.as_dict())
    return json.dumps(result, default=str)


class RecordedInvestigator:
    """
    The offline twin. Deterministic, no network, and honest about what it is.

    **Why it exists, and what it is not.** The same reason `llm/recorded.py` exists: a
    tier that cannot run is a tier that cannot be tested, and the demo must not depend
    on a venue's wifi. It implements the investigator's DECISION PROCEDURE in code --
    read the exception, look up the payer, assert or decline -- so the orchestrator, the
    ledger, the boundary checks and the re-run are all genuinely exercised.

    What it does NOT demonstrate is investigation. It follows one path because that path
    was written for it; a live model chooses its own, and on a narration shape nobody
    anticipated it would keep working where this stops. `name` is reported next to the
    numbers so a recorded run is never mistaken for a live one.
    """

    name = "recorded"
    enabled = True

    def investigate(self, tb: Toolbox, bank_txn_id: str) -> InvestigationTrace:
        trace = InvestigationTrace(bank_txn_id=bank_txn_id)

        exc = tb.get_exception(bank_txn_id)
        if isinstance(exc, dict):
            trace.outcome = "error"
            trace.note = exc.get("error", "unreadable exception")
            return trace
        trace.steps.append({"tool": "get_exception", "result": exc.as_dict()})

        # Only a name-channel objection is in scope. An arithmetic refusal is not
        # something a payer register can speak to, and pretending otherwise is how an
        # investigator starts manufacturing evidence.
        if exc.category != "amount_name_conflict":
            trace.outcome = "insufficient_evidence"
            trace.note = (
                f"the engine refused on {exc.category}, which is not a question about "
                f"who paid; the authorised-payer register cannot speak to it"
            )
            return trace

        pool = tb.get_candidate_pool(bank_txn_id)
        if isinstance(pool, dict):
            trace.outcome = "error"
            trace.note = pool.get("error", "no pool")
            return trace
        trace.steps.append({"tool": "get_candidate_pool", "result": pool.as_dict()})

        # The payer name as the bank wrote it.
        from ..engine.normalize import parse

        payer = parse(exc.narration).payer_name or ""
        rel = tb.lookup_payer_relationship(payer)
        trace.steps.append(
            {"tool": "lookup_payer_relationship", "result": rel.as_dict()}
        )
        if not rel.found:
            trace.outcome = "insufficient_evidence"
            trace.note = (
                f"no register entry for {payer!r}. The register is not exhaustive, so "
                f"this may be a real relationship nobody recorded -- which is a reason "
                f"to leave the exception open, not to assert on absence"
            )
            return trace

        # Which customer to assert about, and why it is a narrower question than it
        # looks.
        #
        # Not "any customer in the window" -- the engine refused a SPECIFIC candidate,
        # and `exc.candidate_payment_ids` names it. Reasoning over the whole pool made
        # the investigator assert a true-but-irrelevant fact: one payer's register
        # entries named two customers, both present in the window, and it picked the one
        # the engine was not trying to post. The engine correctly did nothing with it --
        # the containment held -- but an assertion that cannot matter is noise in the
        # ledger and wasted budget.
        #
        # And the value asserted is the LEDGER's spelling, not the register's. The
        # register holds canonical legal names while the ledger carries alias spellings;
        # asserting 'Pinnacle Steel Traders' against a ledger reading 'Pinnacle Steels
        # Traders' produced an accepted proposal that moved nothing. The assertion is a
        # claim about a ledger customer, so it should say what the ledger says.
        from .tools import _same_entity

        wanted = set(exc.candidate_payment_ids)
        ledger_customers = [
            p.customer_name
            for p in pool.payments
            if p.customer_name and (not wanted or p.payment_id in wanted)
        ]
        entry = ledger_name = None
        for e in rel.entries:
            hit = next(
                (c for c in ledger_customers if _same_entity(c, e.customer)), None
            )
            if hit is not None:
                entry, ledger_name = e, hit
                break
        if entry is None:
            trace.outcome = "insufficient_evidence"
            trace.note = (
                f"the register authorises {payer!r} for {list(rel.customers)}, none of "
                f"which is a customer on this credit's candidate payments. That is "
                f"evidence about a different pair"
            )
            return trace

        receipt = tb.propose_evidence(
            bank_txn_id,
            "authorised_payer_for",
            ledger_name,
            (
                f"the authorised-payer register lists {rel.matched_payer_name!r} as "
                f"permitted to settle for {entry.customer!r} ({entry.relationship}), "
                f"and that is the customer on this credit's candidate payment "
                f"(ledger spelling {ledger_name!r}) -- so the name mismatch the engine "
                f"objected to is expected rather than surprising"
            ),
        )
        trace.steps.append({"tool": "propose_evidence", "result": receipt.as_dict()})
        if receipt.accepted and receipt.proposal is not None:
            trace.proposals.append(receipt.proposal)
            trace.outcome = "proposed"
            trace.note = f"asserted authorised_payer_for = {ledger_name!r}"
        else:
            trace.outcome = "error"
            trace.note = receipt.error
        return trace


class ClaudeInvestigator:
    """
    Ring 2 against a live model, as a real tool-calling loop.

    The model drives: it chooses which tools to call and in what order, and it decides
    when it has enough. What it cannot do is decide the match -- every tool it holds is
    a read except one, and that one appends a validated fact to a ledger.
    """

    def __init__(self, model: str | None = None) -> None:
        import anthropic

        from ..llm import load_dotenv

        load_dotenv()
        self.model = model or cfg.AGENT_MODEL
        self.name = f"claude:{self.model}"
        self.enabled = True
        self._client = anthropic.Anthropic(
            timeout=cfg.LLM_TIMEOUT_S, max_retries=cfg.LLM_MAX_RETRIES
        )
        self.turns = 0

    def investigate(self, tb: Toolbox, bank_txn_id: str) -> InvestigationTrace:
        trace = InvestigationTrace(bank_txn_id=bank_txn_id)
        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"Investigate bank credit {bank_txn_id}. Start by reading the "
                    f"exception."
                ),
            }
        ]
        malformed = 0
        last_call_count = -1
        stalls = 0

        for _ in range(cfg.AGENT_STEP_BUDGET):
            self.turns += 1
            try:
                resp = self._client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    tools=list(TOOL_SPECS),
                    messages=messages,
                )
            except Exception as e:
                trace.outcome = "error"
                trace.note = f"{type(e).__name__}: {e}"
                return trace

            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]

            if not tool_uses:
                said = "".join(
                    b.text for b in resp.content if getattr(b, "type", "") == "text"
                ).strip()
                if trace.proposals:
                    trace.outcome = "proposed"
                    trace.note = said or "asserted evidence"
                else:
                    trace.outcome = "insufficient_evidence"
                    trace.note = said or "concluded without asserting anything"
                return trace

            results = []
            for use in tool_uses:
                args = use.input if isinstance(use.input, dict) else {}
                result = _dispatch(tb, use.name, args)
                trace.steps.append(
                    {
                        "tool": use.name,
                        "args": args,
                        "result": (
                            result.as_dict() if hasattr(result, "as_dict") else result
                        ),
                    }
                )
                if isinstance(result, dict) and "error" in result:
                    malformed += 1
                if use.name == "propose_evidence" and getattr(result, "accepted", False):
                    trace.proposals.append(result.proposal)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": _as_json(result),
                    }
                )
            messages.append({"role": "user", "content": results})

            # One retry on malformed arguments, then abandon. Retrying forever on a
            # model that cannot produce valid arguments is how a batch becomes a bill.
            if malformed > cfg.AGENT_MALFORMED_RETRIES:
                trace.outcome = "error"
                trace.note = (
                    f"{malformed} malformed tool calls; abandoned this exception rather "
                    f"than spending further"
                )
                return trace

            # No-progress detection. A step budget alone lets a stuck loop run to its
            # cap; this stops it as soon as two turns add nothing new.
            if len(tb.calls) == last_call_count:
                stalls += 1
                if stalls >= 2:
                    trace.outcome = "insufficient_evidence"
                    trace.note = "no new information across two turns; stopped"
                    return trace
            else:
                stalls = 0
            last_call_count = len(tb.calls)

        trace.outcome = "budget_exhausted"
        trace.note = f"reached the {cfg.AGENT_STEP_BUDGET}-step budget"
        return trace


def select(disabled: bool = False):
    """
    Choose an investigator: explicitly disabled -> live -> recorded.

    Same shape as `recon.llm.select`, and for the same reason: which one ran is reported
    beside the numbers, so a recorded run cannot be mistaken for a live one.
    """
    if disabled:
        return None
    import os

    from ..llm import load_dotenv

    load_dotenv()
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return ClaudeInvestigator()
        except Exception:
            pass
    return RecordedInvestigator()

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
from typing import Protocol, runtime_checkable

import config as cfg

from .routing import CATEGORIES_FOR, ROUTE, roles_for, why_not
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


# Appended to the system prompt when an instance is given a role. Additive rather than
# substitutive: the rules above are the boundary and apply to every role; this only says
# which question that role was pointed at, so a brief cannot accidentally relax a rule by
# omitting it.
ROLE_BRIEF: dict[str, str] = {
    "payment": """

YOUR SPECIALITY: the gateway record. Read the payments in the candidate pool with \
`get_payment_record` and establish whether money was netted out of the settlement before \
the bank saw it -- a refund, in particular. If the record already shows a refund, that \
figure is the one to assert and the payment id is your source; a different figure will be \
rejected, because the engine has already subtracted the recorded one. If no payment \
records a refund, the gateway has nothing to say about the gap: say so and assert \
nothing.""",
    "bank": """

YOUR SPECIALITY: the statement. Read the line with `get_bank_line`. Two things are worth \
your attention. A value date that differs from the transaction date means the settlement \
reached the bank on a different day from the one the credit posted, and the candidate \
window is anchored on the wrong day -- assert `settlement_date_confirmed`. A reference \
shared with another line is a duplicate UTR: it EXPLAINS why the evidence did not \
identify one payment and it does not identify one either. Report it and assert nothing; \
choosing between the lines is not yours to do.""",
    "invoice": """

YOUR SPECIALITY: the ledger. Two questions, depending on what the engine objected to. If \
it objected to the NAME, check the authorised-payer register for the payer as the bank \
wrote it, and assert `authorised_payer_for` with the LEDGER's spelling of the customer -- \
only if that customer is on one of this credit's candidate payments. If it objected to \
the AMOUNT, use `test_subset` to get the engine's own figure for the gap, read the \
invoice with `get_invoice`, and assert the deduction that explains it, citing the invoice \
number. The TDS already on the invoice is a figure the engine has ALREADY subtracted -- \
asserting it again is restating what is known, and asserting a different one will be \
rejected as a contradiction.""",
}


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
    if name == "get_payment_record":
        return tb.get_payment_record(args.get("payment_id", ""))
    if name == "get_bank_line":
        return tb.get_bank_line(args.get("bank_txn_id", ""))
    if name == "get_invoice":
        return tb.get_invoice(args.get("invoice_no", ""))
    if name == "propose_evidence":
        amount = args.get("amount_paise")
        return tb.propose_evidence(
            args.get("bank_txn_id", ""),
            args.get("field", ""),
            args.get("value", ""),
            args.get("rationale", ""),
            source_type=args.get("source_type", "model_assertion"),
            source_ref=args.get("source_ref", ""),
            retrieved_at=args.get("retrieved_at", ""),
            amount_paise=int(amount) if amount is not None else None,
        )
    return {"error": f"no tool named {name!r}"}


@runtime_checkable
class Investigator(Protocol):
    """
    What the orchestrator requires of anything it routes an exception to.

    **This was a sentence in a docstring, and making it a type is the point of the
    change.** With one investigator there was nothing to disagree with: `orchestrate`
    held it, called `.investigate`, and read `.name`. With several there is a routing
    decision, and a routing decision needs something to route ON -- which is `handles`,
    the set of refusal categories this role is competent for.

    `handles` is a claim the specialist makes about itself and the router enforces:
    `agent/routing.py` will not send a category the investigator does not declare, and a
    test asserts that no specialist declares a category the routing table would never
    give it. The two halves have to agree or the fleet quietly develops a role nothing
    reaches.

    Structural rather than inherited, like `llm/interface.py`'s `LLMTier`. A test double
    that implements three attributes is a valid investigator, which is what makes the
    budget and error paths testable without a model.
    """

    name: str
    enabled: bool
    handles: frozenset[str]

    def investigate(self, tb: Toolbox, bank_txn_id: str) -> InvestigationTrace: ...


def _as_json(result) -> str:
    if hasattr(result, "as_dict"):
        return json.dumps(result.as_dict())
    return json.dumps(result, default=str)


# The batch's own horizon, used as the retrieval date on every offline assertion. See
# `Toolbox.batch_horizon` for why a deterministic investigator must not read a clock.
def _as_at(tb: Toolbox) -> str:
    return tb.batch_horizon


def _open_trace(tb: Toolbox, bank_txn_id: str):
    """Read the exception, or return the trace that says why we could not."""
    trace = InvestigationTrace(bank_txn_id=bank_txn_id)
    exc = tb.get_exception(bank_txn_id)
    if isinstance(exc, dict):
        trace.outcome = "error"
        trace.note = exc.get("error", "unreadable exception")
        return trace, None
    trace.steps.append({"tool": "get_exception", "result": exc.as_dict()})
    return trace, exc


def _decline(trace, note: str):
    trace.outcome = "insufficient_evidence"
    trace.note = note
    return trace


def _submit(tb: Toolbox, trace, receipt):
    trace.steps.append({"tool": "propose_evidence", "result": receipt.as_dict()})
    if receipt.accepted and receipt.proposal is not None:
        trace.proposals.append(receipt.proposal)
        trace.outcome = "proposed"
        trace.note = (
            f"asserted {receipt.proposal.field.value} = {receipt.proposal.value!r} "
            f"from {receipt.proposal.source_type.value} {receipt.proposal.source_ref!r}"
        )
    else:
        # A refused proposal is not an error in the investigator: the boundary declining
        # to carry something is the boundary working. Recorded as an outcome so the run
        # counts it in `proposals_rejected` rather than in `errors`.
        trace.outcome = "insufficient_evidence"
        trace.note = f"the boundary refused the assertion: {receipt.error}"
    return trace


class PaymentInvestigator:
    """
    The gateway side: was money netted out of this settlement before the bank saw it?

    **One question, asked against a field the record already carries.** `Payment` has
    `amount_refunded` and `refund_status`, and `fees.known_deductions` already subtracts
    the first. So this specialist's honest job is narrow: find the case where the record
    says a refund happened and the arithmetic still does not close, and assert the figure
    with the payment id behind it.

    Where the record already carries the refund the engine has already used it, and
    `agent/validate.py` refuses a contradicting figure -- so this cannot be a route to
    restating what is known as though it were new.
    """

    name = "payment"
    enabled = True
    handles = CATEGORIES_FOR("payment")

    def investigate(self, tb: Toolbox, bank_txn_id: str) -> InvestigationTrace:
        trace, exc = _open_trace(tb, bank_txn_id)
        if exc is None:
            return trace

        pool = tb.get_candidate_pool(bank_txn_id)
        if isinstance(pool, dict):
            return _decline(trace, pool.get("error", "no pool"))
        trace.steps.append({"tool": "get_candidate_pool", "result": pool.as_dict()})

        # Only the payments the engine was actually weighing. Reasoning over the whole
        # window produced true-but-irrelevant assertions once already; see the note in
        # RecordedInvestigator about the register entry that named the wrong customer.
        wanted = set(exc.candidate_payment_ids) or {p.payment_id for p in pool.payments}
        refunded: list[tuple[str, int]] = []
        for payment_id in sorted(wanted):
            record = tb.get_payment_record(payment_id)
            if isinstance(record, dict):
                continue
            trace.steps.append({"tool": "get_payment_record", "result": record.as_dict()})
            if record.amount_refunded_paise > 0:
                refunded.append((payment_id, record.amount_refunded_paise))

        if refunded:
            # **Found, and deliberately not asserted.** `Payment.amount_refunded` is read
            # by `fees.known_deductions`, so the engine has ALREADY subtracted this and
            # the gap in front of us is what remains after it. Asserting the same figure
            # would deduct it twice and manufacture a gap rather than close one;
            # `agent/validate.py` refuses it, and this says so a call earlier.
            named = ", ".join(f"{pid} ({amt}p)" for pid, amt in refunded)
            return _decline(
                trace,
                f"the gateway records refunds against {named}, and the engine already "
                f"subtracts them -- the gap in front of us is what is left after that. "
                f"Restating a figure the engine reads would deduct it twice",
            )

        return _decline(
            trace,
            f"no payment in {bank_txn_id}'s candidate pool records a refund, so the "
            f"gateway has nothing to say about the gap. That is a reason to leave the "
            f"exception open, not to assert one",
        )


class BankInvestigator:
    """
    The statement side: narration, references, duplicate lines, settlement dates.

    **Its most useful output is often a decline, and that is by design.** The categories
    it is routed -- `unexplained_residual` and `pool_exceeded` -- are ones where the
    tempting assertion is the dangerous one. A crowded window is not evidence that any
    particular payment belongs to a credit, and a duplicate UTR explains an ambiguity
    without resolving it. So it reads widely and asserts narrowly: a settlement date it
    can read off the narration, and nothing else.
    """

    name = "bank"
    enabled = True
    handles = CATEGORIES_FOR("bank")

    def investigate(self, tb: Toolbox, bank_txn_id: str) -> InvestigationTrace:
        trace, exc = _open_trace(tb, bank_txn_id)
        if exc is None:
            return trace

        line = tb.get_bank_line(bank_txn_id)
        if isinstance(line, dict):
            return _decline(trace, line.get("error", "no bank line"))
        trace.steps.append({"tool": "get_bank_line", "result": line.as_dict()})

        if line.shares_reference_with:
            # Recorded, never acted on. See `agent/routing.py`: a duplicate reference is
            # what MAKES the ambiguity, and the engine's answer to an ambiguity is to
            # refuse. Naming the sibling lines helps whoever picks this up; choosing
            # between them is the one thing this investigator may not do.
            return _decline(
                trace,
                f"{bank_txn_id} shares reference {line.reference!r} with "
                f"{list(line.shares_reference_with)}. A duplicate reference explains why "
                f"the evidence does not identify one payment; it does not identify one, "
                f"and picking between them is not this investigator's to do",
            )

        # The value date is the bank's own record of when the money was settled to it,
        # as distinct from when it posted to the account. Where they differ, the credit's
        # candidate window is anchored on the wrong day.
        if line.value_date and line.value_date != line.txn_date:
            receipt = tb.propose_evidence(
                bank_txn_id,
                "settlement_date_confirmed",
                line.value_date,
                (
                    f"the statement records a value date of {line.value_date} against a "
                    f"transaction date of {line.txn_date}, so the settlement reached the "
                    f"bank {line.value_date} and the candidate window should be counted "
                    f"from there"
                ),
                source_type="bank_statement",
                source_ref=line.reference,
                retrieved_at=_as_at(tb),
            )
            return _submit(tb, trace, receipt)

        return _decline(
            trace,
            f"the statement line for {bank_txn_id} carries nothing the engine has not "
            f"already read: one reference, and a value date equal to its transaction "
            f"date. A crowded window is not evidence about any particular payment",
        )


class InvoiceInvestigator:
    """
    The ledger side: invoice status, TDS, credit notes, part payments -- and the
    authorised-payer register.

    **The register lives here rather than in a specialist of its own**, because the
    question it answers is a ledger question: which customer does this invoice belong to,
    and is the name on the bank line permitted to settle for them? Splitting it out would
    make two investigators that both have to read the candidate pool to say anything.

    Its second job is the arithmetic gap that is not a refund: an invoice whose TDS the
    ledger does not carry, a credit note cut after the invoice, a customer who short-paid.
    Each is a DECLARED DEDUCTION, each requires the invoice number as its source, and each
    is checked against the ledger's own figure before it is accepted.
    """

    name = "invoice"
    enabled = True
    handles = CATEGORIES_FOR("invoice")

    def investigate(self, tb: Toolbox, bank_txn_id: str) -> InvestigationTrace:
        trace, exc = _open_trace(tb, bank_txn_id)
        if exc is None:
            return trace
        if exc.category == "amount_name_conflict":
            return self._payer_identity(tb, trace, exc)
        return self._explain_the_gap(tb, trace, exc)

    # ---- the name channel ------------------------------------------------
    def _payer_identity(self, tb: Toolbox, trace, exc):
        pool = tb.get_candidate_pool(exc.bank_txn_id)
        if isinstance(pool, dict):
            return _decline(trace, pool.get("error", "no pool"))
        trace.steps.append({"tool": "get_candidate_pool", "result": pool.as_dict()})

        from ..engine.normalize import parse

        payer = parse(exc.narration).payer_name or ""
        rel = tb.lookup_payer_relationship(payer)
        trace.steps.append({"tool": "lookup_payer_relationship", "result": rel.as_dict()})
        if not rel.found:
            return _decline(
                trace,
                f"no register entry for {payer!r}. The register is not exhaustive, so "
                f"this may be a real relationship nobody recorded -- which is a reason "
                f"to leave the exception open, not to assert on absence",
            )

        entry, ledger_name = _register_hit(rel, pool, exc)
        if entry is None:
            return _decline(
                trace,
                f"the register authorises {payer!r} for {list(rel.customers)}, none of "
                f"which is a customer on this credit's candidate payments. That is "
                f"evidence about a different pair",
            )

        receipt = tb.propose_evidence(
            exc.bank_txn_id,
            "authorised_payer_for",
            ledger_name,
            (
                f"the authorised-payer register lists {rel.matched_payer_name!r} as "
                f"permitted to settle for {entry.customer!r} ({entry.relationship}), "
                f"and that is the customer on this credit's candidate payment "
                f"(ledger spelling {ledger_name!r}) -- so the name mismatch the engine "
                f"objected to is expected rather than surprising"
            ),
            source_type="payer_register",
            source_ref=rel.matched_payer_name,
            retrieved_at=_as_at(tb),
        )
        return _submit(tb, trace, receipt)

    # ---- the arithmetic gap ----------------------------------------------
    def _explain_the_gap(self, tb: Toolbox, trace, exc):
        """
        A short payment, in the shape this engine actually produces it.

        There is no `partial` refusal category -- see `agent/routing.py`. A customer who
        short-pays arrives as `no_subset_fits` with the closest subset a few hundred paise
        BELOW the credit... except that it is the credit that is short, so the residual
        the engine reports is negative: the bank sent less than the payments account for.
        That gap is what a deduction explains, and its size is the amount to assert.
        """
        pool = tb.get_candidate_pool(exc.bank_txn_id)
        if isinstance(pool, dict):
            return _decline(trace, pool.get("error", "no pool"))
        trace.steps.append({"tool": "get_candidate_pool", "result": pool.as_dict()})

        wanted = tuple(sorted(set(exc.candidate_payment_ids)))
        if not wanted:
            return _decline(
                trace,
                "the engine reported no candidate decomposition to explain a gap "
                "against, so there is nothing to quantify a deduction from",
            )

        verdict = tb.test_subset(exc.bank_txn_id, wanted)
        if isinstance(verdict, dict):
            return _decline(trace, verdict.get("error", "the subset could not be tested"))
        trace.steps.append({"tool": "test_subset", "result": verdict.as_dict()})
        if verdict.fits:
            return _decline(
                trace,
                "the engine's own arithmetic says this subset already fits, so there is "
                "no gap for a deduction to explain",
            )
        shortfall = -verdict.residual_paise
        if shortfall <= 0:
            return _decline(
                trace,
                f"the bank credited {verdict.residual_paise}p MORE than these payments "
                f"account for. A deduction explains money that did not arrive; money "
                f"that arrived unexpectedly is a different question and not this one",
            )

        # Which invoice, and does the ledger already explain the gap?
        invoice_no = next(
            (
                p.invoice_no
                for p in pool.payments
                if p.payment_id in set(wanted) and p.invoice_no
            ),
            "",
        )
        if not invoice_no:
            return _decline(
                trace,
                "no invoice is recorded against this credit's candidate payments, so "
                "there is no ledger record to attribute a deduction to",
            )
        inv = tb.get_invoice(invoice_no)
        if isinstance(inv, dict):
            return _decline(trace, inv.get("error", "no invoice"))
        trace.steps.append({"tool": "get_invoice", "result": inv.as_dict()})

        # **The gap is not the evidence, and this is where the project learned it.**
        #
        # The first version asserted `shortfall` as a short payment on the arithmetic
        # alone: four payments of coverage, two wrong postings, precision 1.0000 ->
        # 0.9854. The obvious patch -- only assert against an invoice the ledger marks
        # `part_settled` -- looked principled and was still wrong, because a status says
        # a shortfall happened and not how big it was, so the amount was still coming
        # from the residual. The holdout caught it: 1.0000 -> 0.9913, same mechanism
        # wearing a corroborating flag.
        #
        # A deduction is admissible when a record NAMES the figure. This ledger names two
        # amounts, `gross_amount` and `tds_amount`, and `fees.known_deductions` already
        # reads the second. There is no settled-to-date column and no credit-note line,
        # so on this data the honest output is an EVIDENCED exception rather than an
        # assertion: the gap is quantified, the invoice is named, and a human decides.
        #
        # `agent/validate.py` enforces this whatever any investigator does. Declining
        # here is so the specialist does not spend a call learning it, and so the note it
        # leaves says something useful to whoever picks the exception up.
        return _decline(
            trace,
            f"the engine's arithmetic leaves {shortfall}p of this credit unaccounted "
            f"for against invoice {inv.invoice_no} ({inv.customer_name}, {inv.status}, "
            f"{inv.rupees:.2f} gross, {inv.tds_rupees:.2f} TDS). The ledger names no "
            f"further deducted amount -- no credit note, no settled-to-date -- so there "
            f"is no figure to assert that does not come from the gap itself. Quantified "
            f"for whoever picks it up",
        )


def _register_hit(rel, pool, exc):
    """
    Which register entry names a customer on THIS credit's candidate payments.

    Extracted from `RecordedInvestigator` unchanged, including both bug fixes it records:
    the customer set is restricted to the payments the engine was actually trying to post,
    and the value asserted is the LEDGER's spelling rather than the register's.
    """
    from .tools import _same_entity

    wanted = set(exc.candidate_payment_ids)
    ledger_customers = [
        p.customer_name
        for p in pool.payments
        if p.customer_name and (not wanted or p.payment_id in wanted)
    ]
    for e in rel.entries:
        hit = next((c for c in ledger_customers if _same_entity(c, e.customer)), None)
        if hit is not None:
            return e, hit
    return None, None


class RecordedInvestigator:
    """
    The offline twin. Deterministic, no network, and honest about what it is.

    **Why it exists, and what it is not.** The same reason `llm/recorded.py` exists: a
    tier that cannot run is a tier that cannot be tested, and the demo must not depend on
    a venue's wifi. It implements the investigators' DECISION PROCEDURES in code, so the
    orchestrator, the ledger, the boundary checks, the routing and the re-run are all
    genuinely exercised.

    What it does NOT demonstrate is investigation. Each specialist follows one path
    because that path was written for it; a live model chooses its own, and on a shape
    nobody anticipated it would keep working where this stops. `name` is reported next to
    the numbers so a recorded run is never mistaken for a live one.

    **It is now a router rather than a procedure, and the name is unchanged on purpose.**
    Every reported offline figure, and every test keyed on `investigator == "recorded"`,
    refers to this. Renaming it to reflect the internal change would have made a
    presentational edit look like a measurement moving.
    """

    name = "recorded"
    enabled = True
    handles = frozenset(ROUTE)

    def __init__(self) -> None:
        self._by_role = {
            "payment": PaymentInvestigator(),
            "bank": BankInvestigator(),
            "invoice": InvoiceInvestigator(),
        }

    def investigate(self, tb: Toolbox, bank_txn_id: str) -> InvestigationTrace:
        exc = tb.get_exception(bank_txn_id)
        if isinstance(exc, dict):
            trace = InvestigationTrace(bank_txn_id=bank_txn_id)
            trace.outcome = "error"
            trace.note = exc.get("error", "unreadable exception")
            return trace

        reason = why_not(exc.category)
        if reason:
            trace = InvestigationTrace(bank_txn_id=bank_txn_id)
            return _decline(
                trace, f"not investigated: {exc.category} is a case where {reason}"
            )

        roles = roles_for(exc.category)
        if not roles:
            trace = InvestigationTrace(bank_txn_id=bank_txn_id)
            return _decline(
                trace,
                f"no specialist is routed {exc.category}. The exception keeps its desk; "
                f"what it does not get is an agent guessing at it",
            )

        # First specialist to assert wins; one that declines hands on to the next. The
        # LAST trace is returned rather than the first, so a chain that ends in a decline
        # reports the decline that ended it rather than the one before it.
        last = None
        for role in roles:
            last = self._by_role[role].investigate(tb, bank_txn_id)
            last.note = f"[{role}] {last.note}"
            if last.proposals:
                return last
        return last


class ClaudeInvestigator:
    """
    Ring 2 against a live model, as a real tool-calling loop.

    The model drives: it chooses which tools to call and in what order, and it decides
    when it has enough. What it cannot do is decide the match -- every tool it holds is
    a read except one, and that one appends a validated fact to a ledger.
    """

    def __init__(self, model: str | None = None, role: str | None = None) -> None:
        import anthropic

        from ..llm import load_dotenv

        load_dotenv()
        self.model = model or cfg.AGENT_MODEL
        self.role = role
        self.name = f"claude:{self.model}" + (f"[{role}]" if role else "")
        self.enabled = True
        # A role narrows what this instance is ROUTED, not what it is capable of. One
        # class parameterised by role rather than three near-identical subclasses: the
        # difference between a payment specialist and an invoice specialist is which
        # question it is pointed at, and encoding that as three copies of a tool loop
        # would mean three places to fix the next bug in the loop.
        self.handles = CATEGORIES_FOR(role) if role else frozenset(ROUTE)
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
                    system=SYSTEM_PROMPT + ROLE_BRIEF.get(self.role or "", ""),
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

    Returns a FLEET when a live model is available -- one `ClaudeInvestigator` per role,
    keyed for the router -- and the recorded router otherwise. Both satisfy the same
    contract from `orchestrate`'s side: it asks the router which role a category belongs
    to and hands the exception to whatever answers to that key.
    """
    if disabled:
        return None
    import os

    from ..llm import load_dotenv

    load_dotenv()
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return ClaudeFleet()
        except Exception:
            pass
    return RecordedInvestigator()


class ClaudeFleet:
    """
    One live investigator per role, behind the same interface as the recorded router.

    **Independent investigation, one decision-maker.** Three specialists look at
    different records and may reach different conclusions, and nothing here reconciles
    them: each proposes evidence, the ledger records what the boundary accepts, and the
    deterministic engine runs once over all of it and reaches its own verdict. There is
    no vote, no confidence-weighted merge, and no tie-break -- `docs/AGENTIC.md` rules
    out all three, and the reason is that a fleet that agreed with itself would look
    exactly like a fleet that was right.
    """

    name = "claude-fleet"
    enabled = True
    handles = frozenset(ROUTE)

    def __init__(self, model: str | None = None) -> None:
        self._by_role = {
            role: ClaudeInvestigator(model=model, role=role)
            for role in ("payment", "bank", "invoice")
        }
        self.name = f"claude-fleet:{self._by_role['payment'].model}"

    @property
    def turns(self) -> int:
        return sum(i.turns for i in self._by_role.values())

    def investigate(self, tb: Toolbox, bank_txn_id: str) -> InvestigationTrace:
        exc = tb.get_exception(bank_txn_id)
        if isinstance(exc, dict):
            trace = InvestigationTrace(bank_txn_id=bank_txn_id)
            trace.outcome = "error"
            trace.note = exc.get("error", "unreadable exception")
            return trace
        reason = why_not(exc.category)
        if reason:
            return _decline(
                InvestigationTrace(bank_txn_id=bank_txn_id),
                f"not investigated: {exc.category} is a case where {reason}",
            )
        last = None
        for role in roles_for(exc.category):
            last = self._by_role[role].investigate(tb, bank_txn_id)
            last.note = f"[{role}] {last.note}"
            if last.proposals:
                return last
        if last is None:
            return _decline(
                InvestigationTrace(bank_txn_id=bank_txn_id),
                f"no specialist is routed {exc.category}",
            )
        return last

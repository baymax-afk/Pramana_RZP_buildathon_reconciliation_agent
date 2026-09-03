"""
The agent's tool surface: five reads and one write.

**The tools ARE the engine.** `test_subset` calls `fees.expected_credit_interval` and
`fees.fits` -- the matcher's own arithmetic -- rather than reimplementing it, and
`get_candidate_pool` calls `tier2_amount_date.candidate_pool`, the matcher's own
definition of which payments could belong to a credit. A second implementation of either
would be a second source of truth about money, and the two would eventually disagree in
a way nobody noticed until a demo.

So the agent can ask any question the engine can answer, and cannot answer one itself.

**Scoping, concretely.** Nothing here can post, unpost, or re-score a match. Nothing
here reads the ground-truth directory. The audit hook in `recon/__init__.py` is
deny-by-default over everything under `recon.` except the generator, so it covered this
package before it existed; `tests/test_agent_tools.py` plants a probe to prove it rather
than re-reading the rule.

(That sentence deliberately names the directory in prose rather than as a literal.
`tests/test_isolation.py` statically scans every module inside the boundary for the
path itself and fails on a match -- a check that cannot distinguish a comment from an
`open()`, and should not try to.) The one
write, `propose_evidence`, appends to a ledger and is validated against the shape of
every identifier in the batch -- see `schemas.EvidenceProposal`.
"""

from __future__ import annotations

import config as cfg

from ..engine import fees, tier2_amount_date
from ..engine.results import MatchOutput
from ..schemas import PayerAuthorisation, ReconInputs
from .schemas import (
    EvidenceField,
    EvidenceProposal,
    EvidenceReceipt,
    ExceptionView,
    InvoiceSearchResult,
    InvoiceView,
    PayerRelation,
    PoolPayment,
    RegisterEntry,
    PoolView,
    SubsetVerdict,
)


# Shortest name fragment that may match partially. Not derived from the bank's field
# width -- see `_same_entity` for why that was the wrong basis -- but a floor against
# stubs so short they would match half the register.
_MIN_PARTIAL_NAME = 8

_SUFFIXES = (
    " PRIVATE LIMITED", " PVT LTD", " PVT LIMITED", " LIMITED", " LTD",
    " LLP", " AND CO", " CO",
)


def _fold(name: str, strip_suffix: bool = True) -> str:
    """Upper-case, strip punctuation, and optionally drop the corporate-form suffix."""
    out = "".join(c for c in (name or "").upper() if c.isalnum() or c == " ")
    if strip_suffix:
        for suffix in _SUFFIXES:
            if out.endswith(suffix):
                out = out[: -len(suffix)]
    return " ".join(out.split())


def _normalise(name: str) -> str:
    """
    Fold a name for indexing: case, punctuation, and the corporate-form suffix.

    Suffixes go because a bank statement writes 'PVT LTD' where a ledger writes 'Private
    Limited' for the same entity. Comparison itself is `_same_entity`, which is stricter
    than this and has to be.
    """
    return _fold(name, strip_suffix=True)


def _same_entity(a: str, b: str) -> bool:
    """
    Are these two names the same counterparty, allowing for bank-field truncation?

    **Raw prefix matching is wrong here, and this batch is built to prove it.** The
    generator plants confusable pairs that are DISTINCT entities with near-identical
    names -- 'Bharat Traders and Co' against 'Bharati Traders LLP', 'Acme Retail' against
    'Acme Industrial Supplies' -- specifically so a similarity matcher that merges them
    gets caught. `'BHARAT TRADERS'` is a raw prefix of `'BHARATI TRADERS'`, and treating
    that as agreement would post one customer's money against another's invoice. The
    first version of this lookup did exactly that.

    So agreement is decided at WORD BOUNDARIES: every whole word of the shorter name
    must agree exactly, and only its final, cut-off word may match as a prefix. That
    alone rejects both confusable pairs, because they differ inside a word rather than
    at the end of one.

    **The length floor is deliberately loose, and the real guard is elsewhere.** A first
    version required the shorter name to be at least `BANK_NARRATION_NAME_WIDTH`
    characters, on the theory that only a bank-truncated name should match partially.
    That was wrong: the name is embedded in a fixed-width NARRATION, not a fixed-width
    name field, so how much survives depends on the format around it -- 'BHARATI TRADERS
    LL' arrives at 18 characters and 'VERTEX ENGINEERIN' at 17, and the strict floor
    silently declined the second. Picking 16 instead would just move the arbitrary line.

    The property actually worth enforcing is not "long enough" but "unambiguous", and
    that is checkable rather than guessed: `lookup_payer_relationship` refuses when a
    query matches more than one registered payer. Which is the engine's own doctrine --
    Layer 2 refuses when two subsets fit -- applied to names.
    """
    # Both folds are compared, because stripping the suffix and truncating the field
    # interact. `'Bharati Traders LLP'` strips to `'BHARATI TRADERS'` (15 chars) while
    # the statement carries `'BHARATI TRADERS LL'` (truncated at 18) -- so the stripped
    # register name is SHORTER than the truncated bank name and the truncation rule
    # below rejects it. Kept with the suffix, they agree on 'LL' -> 'LLP'. Testing both
    # forms cannot loosen the confusable guard: a token-zero mismatch like
    # 'BHARAT' vs 'BHARATI' fails in every variant.
    forms_a = {_fold(a, True), _fold(a, False)}
    forms_b = {_fold(b, True), _fold(b, False)}
    if not any(forms_a) or not any(forms_b):
        return False

    for x in forms_a:
        for y in forms_b:
            if not x or not y:
                continue
            if x == y:
                return True
            short, long = (x, y) if len(x) <= len(y) else (y, x)
            # A floor only against degenerate stubs -- a two-character prefix matching
            # half the register is noise, not evidence. Ambiguity is handled by the
            # caller refusing multi-hit queries, which is the guard that matters.
            if len(short) < _MIN_PARTIAL_NAME:
                continue
            s_tokens, l_tokens = short.split(), long.split()
            if len(s_tokens) > len(l_tokens):
                continue
            # Every whole word of the truncated name must agree exactly...
            if s_tokens[:-1] != l_tokens[: len(s_tokens) - 1]:
                continue
            # ...and its final, cut-off word must be a prefix of the corresponding one.
            if l_tokens[len(s_tokens) - 1].startswith(s_tokens[-1]):
                return True
    return False


class Toolbox:
    """
    One batch's worth of tools, bound to the inputs and the run they describe.

    Constructed per investigation rather than per exception so the register is parsed
    once, and so `calls` accumulates across the whole run for the budget to read.
    """

    def __init__(
        self,
        inputs: ReconInputs,
        out: MatchOutput,
        directory: tuple[PayerAuthorisation, ...] = (),
    ) -> None:
        self._inputs = inputs
        self._out = out
        self._directory = directory
        self._pay = {p.id: p for p in inputs.payments}
        self._inv = {i.invoice_no: i for i in inputs.invoices}
        self._txn = {t.id: t for t in inputs.bank_txns}
        self._refusal = {r.bank_txn_id: r for r in out.refusals}
        # Grouped by the RAW payer name, not a folded key. Folding at index time threw
        # away the unstripped form, so `_same_entity` -- which needs both to reconcile
        # suffix stripping against field truncation -- could only ever see one of them,
        # and 'BHARATI TRADERS LL' stopped matching 'Bharati Traders LLP'. A list per
        # name because one payer can be authorised for several customers, and
        # collapsing that would silently drop relationships. Linear scan: the register
        # is tens of rows, and correctness here is worth more than an index.
        self._by_payer: dict[str, list[PayerAuthorisation]] = {}
        for row in directory:
            self._by_payer.setdefault(row.payer_name, []).append(row)
        self.calls: list[str] = []

    # ---- reads ---------------------------------------------------------
    def get_exception(self, bank_txn_id: str) -> ExceptionView | dict:
        self.calls.append(f"get_exception({bank_txn_id})")
        r = self._refusal.get(bank_txn_id)
        if r is None:
            return {"error": f"{bank_txn_id} is not a refused credit in this run"}
        t = self._txn.get(bank_txn_id)
        return ExceptionView(
            bank_txn_id=bank_txn_id,
            txn_date=t.txn_date if t else "",
            rupees=round(r.paise_at_risk / 100, 2),
            narration=t.narration if t else "",
            reference=t.ref_no if t else "",
            category=r.category.value,
            engine_reason=r.reason,
            candidate_payment_ids=tuple(
                pid for c in r.candidates for pid in c.payment_ids
            ),
        )

    def get_candidate_pool(self, bank_txn_id: str) -> PoolView | dict:
        """
        Which payments could belong to this credit, by the ENGINE's own definition.

        Calls `tier2_amount_date.candidate_pool` with nothing claimed, so the agent sees
        the same date-window blocking the matcher used rather than a wider or narrower
        set it might reason wrongly from.
        """
        self.calls.append(f"get_candidate_pool({bank_txn_id})")
        t = self._txn.get(bank_txn_id)
        if t is None:
            return {"error": f"no bank transaction {bank_txn_id}"}
        lo, hi = tier2_amount_date.window_for(t)
        pool = tier2_amount_date.candidate_pool(t, self._inputs.payments, set())
        return PoolView(
            bank_txn_id=bank_txn_id,
            window_from=lo.isoformat(),
            window_to=hi.isoformat(),
            payments=tuple(
                PoolPayment(
                    payment_id=p.id,
                    rupees=round(p.amount / 100, 2),
                    settled_on=tier2_amount_date.payment_date(p).isoformat(),
                    customer_name=p.notes.get("customer_name", ""),
                    invoice_no=p.notes.get("invoice_no", ""),
                )
                for p in pool
            ),
        )

    def test_subset(
        self, bank_txn_id: str, payment_ids: tuple[str, ...]
    ) -> SubsetVerdict | dict:
        """
        Would these payments account for this credit? Answered by the engine's own
        arithmetic, so the agent cannot talk itself into a different sum.
        """
        self.calls.append(f"test_subset({bank_txn_id}, {len(payment_ids)} payments)")
        t = self._txn.get(bank_txn_id)
        if t is None:
            return {"error": f"no bank transaction {bank_txn_id}"}
        missing = [pid for pid in payment_ids if pid not in self._pay]
        if missing:
            return {"error": f"unknown payment id(s): {missing}"}
        if not payment_ids:
            return {"error": "no payments given to test"}

        payments = [self._pay[pid] for pid in payment_ids]
        interval = fees.expected_credit_interval(payments, self._inv)
        residual = fees.residual(t.credit, interval)
        return SubsetVerdict(
            bank_txn_id=bank_txn_id,
            payment_ids=tuple(payment_ids),
            credit_paise=t.credit,
            expected_lo_paise=interval.lo,
            expected_hi_paise=interval.hi,
            residual_paise=residual,
            tolerance_paise=fees.tolerance_for(t.credit),
            fits=fees.fits(t.credit, interval),
            fee_known_exactly=interval.certain,
        )

    def lookup_payer_relationship(self, payer_name: str) -> PayerRelation:
        """
        Ask the merchant's authorised-payer register about a name on the statement.

        Matched by folded prefix, because a bank export truncates to a fixed width and
        the register holds the full legal name. `found=False` is a real answer: the
        register is deliberately incomplete, so some genuine relationships are simply
        not on it and declining is the correct outcome.
        """
        self.calls.append(f"lookup_payer_relationship({payer_name!r})")
        query = _normalise(payer_name)
        if not query:
            return PayerRelation(payer_name, False, note="empty name")
        if not self._directory:
            return PayerRelation(
                payer_name, False,
                note="no authorised-payer register is available for this batch",
            )

        hits: list[PayerAuthorisation] = []
        matched_names: set[str] = set()
        for key, rows in self._by_payer.items():
            if _same_entity(key, payer_name):
                hits.extend(rows)
                matched_names.add(key)

        # **Ambiguity is a refusal, not a tiebreak.** A truncated statement name that
        # matches two registered payers identifies neither, and picking one would be a
        # decision made by dictionary order on somebody's money. This is Layer 2's rule
        # -- two subsets fitting means neither is the answer -- applied to names, and it
        # is what lets the length floor above stay loose.
        if len(matched_names) > 1:
            return PayerRelation(
                payer_name, False,
                note=(
                    f"{payer_name!r} matches {len(matched_names)} different registered "
                    f"payers ({sorted(matched_names)}). A truncated name that fits more "
                    f"than one identifies none of them, so nothing is asserted."
                ),
            )
        matched = next(iter(matched_names), "")
        if not hits:
            return PayerRelation(
                payer_name, False,
                note=(
                    "no entry. The register is not exhaustive, so this may be a real "
                    "relationship nobody has recorded -- it is not evidence that the "
                    "payer is unauthorised."
                ),
            )
        return PayerRelation(
            queried=payer_name,
            found=True,
            matched_payer_name=matched,
            entries=tuple(
                RegisterEntry(h.authorised_for_customer, h.relationship) for h in hits
            ),
            note=f"{len(hits)} register entr{'y' if len(hits) == 1 else 'ies'}",
        )

    def search_invoices(self, query: str, limit: int = 10) -> InvoiceSearchResult:
        self.calls.append(f"search_invoices({query!r})")
        q = _normalise(query)
        matches = [
            InvoiceView(
                invoice_no=i.invoice_no,
                customer_name=i.customer_name,
                rupees=round(i.gross_amount / 100, 2),
                tds_rupees=round(i.tds_amount / 100, 2),
                invoice_date=i.invoice_date,
                status=i.status,
            )
            for i in self._inputs.invoices
            if q and (q in _normalise(i.customer_name) or q in i.invoice_no.upper())
        ]
        return InvoiceSearchResult(query=query, matches=tuple(matches[:limit]))

    # ---- the one write -------------------------------------------------
    def propose_evidence(
        self, bank_txn_id: str, field: str, value: str, rationale: str
    ) -> EvidenceReceipt:
        """
        Assert one fact about one credit. The only call here that changes anything.

        It does not post a match. It appends to a ledger; the deterministic engine is
        re-run afterwards with the enriched inputs and reaches its own conclusion. That
        indirection is not ceremony -- it is what keeps precision a property of the
        engine rather than of the agent's judgement.
        """
        self.calls.append(f"propose_evidence({bank_txn_id}, {field})")
        if bank_txn_id not in self._txn:
            return EvidenceReceipt(False, error=f"no bank transaction {bank_txn_id}")
        try:
            enum_field = EvidenceField(field)
        except ValueError:
            allowed = ", ".join(f.value for f in EvidenceField)
            return EvidenceReceipt(
                False,
                error=(
                    f"{field!r} is not an evidence field this engine weighs. "
                    f"Allowed: {allowed}. A channel the engine never agreed to weigh "
                    f"would be ignored silently, which is why this is an enum."
                ),
            )
        proposal = EvidenceProposal(
            bank_txn_id=bank_txn_id,
            field=enum_field,
            value=value,
            rationale=rationale,
            tool_calls=tuple(self.calls),
        )
        try:
            proposal.validate()
        except ValueError as e:
            return EvidenceReceipt(False, error=str(e))
        return EvidenceReceipt(True, proposal=proposal)


# --------------------------------------------------------------------------
# The tool definitions handed to the model
# --------------------------------------------------------------------------
# Kept beside the implementations so a signature change and its description move
# together. A description that has drifted from what a tool does is worse than a vague
# one, because the model trusts it.
TOOL_SPECS: tuple[dict, ...] = (
    {
        "name": "get_exception",
        "description": (
            "Read the exception you are investigating: the bank line, the amount at "
            "risk, the engine's refusal category and its reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"bank_txn_id": {"type": "string"}},
            "required": ["bank_txn_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_candidate_pool",
        "description": (
            "List every payment that could belong to this credit, using the engine's "
            "own date-window blocking. Includes each payment's amount, settlement date, "
            "ledger customer name and invoice number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"bank_txn_id": {"type": "string"}},
            "required": ["bank_txn_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "test_subset",
        "description": (
            "Ask the ENGINE whether a set of payments accounts for this credit. Returns "
            "the expected settlement interval, the residual in paise, the tolerance, "
            "and whether it fits. You cannot post a match with this; it only tells you "
            "what the engine's arithmetic says."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bank_txn_id": {"type": "string"},
                "payment_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["bank_txn_id", "payment_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_payer_relationship",
        "description": (
            "Ask the merchant's authorised-payer register whether a name on the bank "
            "statement is on record as permitted to settle for one of our customers. "
            "The register is NOT exhaustive: found=false may mean the relationship is "
            "real but unrecorded, and is never evidence that a payer is unauthorised."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"payer_name": {"type": "string"}},
            "required": ["payer_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_invoices",
        "description": (
            "Search the invoice ledger by customer name or invoice number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_evidence",
        "description": (
            "Assert ONE fact about this credit, with your reasoning. This does not post "
            "a match: the deterministic engine is re-run afterwards with your evidence "
            "included and reaches its own verdict, which may still be a refusal. "
            "`field` must be 'authorised_payer_for' and `value` must be a CUSTOMER "
            "NAME -- a payment id, order id or bank reference will be rejected. If the "
            "evidence does not support an assertion, do not make one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bank_txn_id": {"type": "string"},
                "field": {"type": "string", "enum": ["authorised_payer_for"]},
                "value": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["bank_txn_id", "field", "value", "rationale"],
            "additionalProperties": False,
        },
    },
)

TOOL_NAMES = tuple(spec["name"] for spec in TOOL_SPECS)

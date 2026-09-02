"""
Deterministic narration parsing -- the regex tier.

Bank narration is the only place a payer name appears on the bank side, and it arrives
in whatever shape the originating rail produced. This module extracts structure from
it using nothing but regular expressions and string handling.

This is TIER ZERO of the trust boundary. The LLM tier exists only to parse narrations
this module fails on, and even then it returns *fields*, never a match. Every narration
this module can handle is one the LLM never sees, so the LLM-off and LLM-on runs differ
only on the residue -- which is what makes reporting precision both ways meaningful.

Parsing is deliberately non-destructive. Bank exports truncate payer names to a fixed
field width, and that truncation is preserved rather than "repaired": partial name
agreement is genuine Fellegi-Sunter evidence, and inventing the missing characters
would manufacture certainty the data does not contain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A UTR-shaped reference: 4-6 letters then 8-16 digits.
_UTR = re.compile(r"\b([A-Z]{4,6}\d{8,16})\b")
# A bare numeric reference, as IMPS and UPI narrations carry.
_NUMERIC_REF = re.compile(r"\b(\d{9,18})\b")
# Razorpay settlement id.
_SETL = re.compile(r"\b(setl_[A-Za-z0-9]{8,20})\b")
# "12 TXNS"
_TXN_COUNT = re.compile(r"\b(\d+)\s*TXNS?\b", re.IGNORECASE)
# A merchant-side reference the payer quoted in the remittance -- an invoice or PO
# number. This is the token that makes an exact-reference match possible at all: the
# UTR is bank-side and appears nowhere in the merchant's own records, so without this
# the two sides share no key and tier 1 can never fire.
_MERCHANT_REF = re.compile(r"\b([A-Z]{2,4}-\d{4}-\d{2,8})\b")

_STYLES = (
    ("settlement", re.compile(r"^RAZORPAY\s+SETTLEMENT", re.IGNORECASE)),
    ("neft", re.compile(r"^NEFT[-/]", re.IGNORECASE)),
    ("rtgs", re.compile(r"^RTGS[-/]", re.IGNORECASE)),
    ("imps", re.compile(r"^IMPS[-/]", re.IGNORECASE)),
    ("upi", re.compile(r"^UPI[-/]", re.IGNORECASE)),
)

# Tokens that are structural, not part of anybody's name.
_NOISE = {
    "CR", "DR", "NEFT", "RTGS", "IMPS", "UPI", "PAYMENT", "PMT", "TXN", "TXNS",
    "RAZORPAY", "SETTLEMENT", "TRANSFER", "TRF", "REF", "BY", "TO", "FROM",
    # Rail-specific structural tokens. Every one of these appeared inside a payer
    # name before being listed here -- "ACME INDUSTRIAL SU FUND" was a real output.
    "FUND", "FUNDS", "CLG", "CMS", "INW", "REM", "MB", "ACCT", "AC", "FRM",
}


@dataclass(frozen=True, slots=True)
class ParsedNarration:
    """
    What the regex tier could extract. Every field is optional, because narration
    genuinely varies in what it carries -- and a settlement batch legitimately carries
    no payer name at all, because it covers many payers.

    `parsed_by` records which tier produced this. When the LLM tier is enabled it
    fills the same structure and stamps "llm", so the provenance of every parsed field
    is visible in the output rather than inferred.
    """

    raw: str
    style: str
    reference: str | None = None
    payer_name: str | None = None
    merchant_ref: str | None = None
    settlement_id: str | None = None
    txn_count: int | None = None
    parsed_by: str = "regex"

    @property
    def is_settlement_batch(self) -> bool:
        return self.style == "settlement"

    @property
    def carries_name(self) -> bool:
        return bool(self.payer_name)


def _detect_style(text: str) -> str:
    for name, pattern in _STYLES:
        if pattern.search(text):
            return name
    return "unknown"


def _extract_name(text: str, style: str) -> str | None:
    """
    Pull the payer name out of a narration.

    Settlement batches deliberately return None: a gateway settlement covers many
    payers, so there is no single name to extract, and inventing one would be worse
    than admitting there is none.
    """
    if style == "settlement":
        return None

    body = text
    # Strip a trailing credit/debit marker.
    body = re.sub(r"[-/](CR|DR)\s*$", "", body, flags=re.IGNORECASE)
    parts = re.split(r"[-/]", body)

    best: str | None = None
    for part in parts:
        token = part.strip()
        if not token:
            continue
        # Filter WORD BY WORD, not just chunk by chunk. Some rails write no delimiters
        # at all ("ACME INDUSTRIAL SU FUND TRF 398693 INV20261003"), so the whole
        # narration arrives as one chunk. Filtering only whole chunks returned that
        # entire string as the payer name, which then fed Fellegi-Sunter as though it
        # were a counterparty -- a garbage name is worse than no name, because absence
        # contributes zero weight while nonsense can manufacture a disagreement.
        words = [
            w for w in token.split()
            if w.upper() not in _NOISE
            and not w.isdigit()
            and not _UTR.fullmatch(w.upper())
            and not _MERCHANT_REF.fullmatch(w.upper())
            and not re.fullmatch(r"INV[0-9/-]*", w, re.IGNORECASE)
            and re.search(r"[A-Za-z]{2,}", w)
        ]
        if not words:
            continue
        candidate = " ".join(words).strip()
        if best is None or len(candidate) > len(best):
            best = candidate
    return best if best else None


def parse(narration: str) -> ParsedNarration:
    """
    Parse one bank narration. Pure, deterministic, and total -- it always returns a
    ParsedNarration, using style "unknown" and empty fields when it recognises nothing.
    Never raises, because a narration the bank produced is not an error condition.
    """
    text = (narration or "").strip()
    style = _detect_style(text)

    utr = _UTR.search(text.upper())
    reference = utr.group(1) if utr else None
    if reference is None:
        numeric = _NUMERIC_REF.search(text)
        reference = numeric.group(1) if numeric else None

    merchant = _MERCHANT_REF.search(text.upper())
    setl = _SETL.search(text)
    count = _TXN_COUNT.search(text)

    return ParsedNarration(
        raw=text,
        style=style,
        reference=reference,
        payer_name=_extract_name(text, style),
        merchant_ref=merchant.group(1) if merchant else None,
        settlement_id=setl.group(1) if setl else None,
        txn_count=int(count.group(1)) if count else None,
        parsed_by="regex",
    )


def needs_llm(parsed: ParsedNarration) -> bool:
    """
    Whether the LLM tier should be offered this narration.

    Only genuinely unparsed narrations qualify. A settlement batch with no payer name
    is fully parsed -- the absence of a name is the correct answer, not a failure -- so
    it must not be handed to the LLM to hallucinate one into.
    """
    if parsed.style == "unknown" and not parsed.reference:
        return True
    if parsed.style in {"neft", "rtgs", "imps", "upi"} and not parsed.payer_name:
        return True
    return False


def normalise_name(name: str | None) -> str:
    """
    Fold a name for comparison: uppercase, punctuation stripped, corporate suffixes
    collapsed, whitespace squeezed.

    This is a COMPARISON KEY, not a canonical name -- it is never written back into
    output, because folding 'Acme Retail Pvt Ltd' and 'Acme Retail Private Limited'
    together is exactly the judgement the Fellegi-Sunter layer is supposed to price
    rather than assume.
    """
    if not name:
        return ""
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", name.upper())
    s = re.sub(
        r"\b(PRIVATE|PVT|LIMITED|LTD|LLP|INC|CORP|CO|COMPANY|AND)\b", " ", s
    )
    return re.sub(r"\s+", " ", s).strip()


def parse_with_llm(narration: str, llm=None) -> ParsedNarration:
    """
    Parse a narration, falling back to the LLM tier only where regex genuinely failed.

    The LLM is offered a narration ONLY when `needs_llm` says the deterministic tier
    could not read it. Everything else never reaches a model, which is what makes the
    LLM-on and LLM-off runs differ solely on the residue -- and therefore what makes
    reporting precision both ways a meaningful comparison rather than two labels on the
    same number.

    Fields the regex tier already extracted are never overwritten. The model fills
    gaps; it does not get to revise deterministic output.

    That is a real cost, paid deliberately. 'ACME INDUSTRIAL SU PAYMENT AGAINST
    BILLS' is a poor name extraction and a model could plainly do better, but
    allowing an override would let model output displace deterministic output on
    the Fellegi-Sunter name channel -- which feeds match decisions. Fill-gaps-only
    keeps the boundary absolute at the price of accuracy the LLM could have added,
    and it is what lets the on/off figures be compared honestly: the LLM can only
    ever ADD information, never change an answer the deterministic tier gave.
    """
    parsed = parse(narration)
    if llm is None or not getattr(llm, "enabled", False) or not needs_llm(parsed):
        return parsed

    from dataclasses import replace as _replace

    fields = llm.parse_narration(narration)
    if fields.is_empty:
        return parsed
    return _replace(
        parsed,
        payer_name=parsed.payer_name or fields.payer_name,
        merchant_ref=parsed.merchant_ref or fields.merchant_ref,
        parsed_by=f"regex+{fields.model}",
    )

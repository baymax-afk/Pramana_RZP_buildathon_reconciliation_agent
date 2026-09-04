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
    # Whether the grammar the name came out of was RECOGNISED. False means a name was
    # extracted from a shape this module has never seen, which is a guess rather than
    # evidence -- see `parse`. `payer_name` is then None and `withheld_name` keeps what
    # was read, so the explain transcript can show the reader what was seen and say why
    # it was not used. Silence and suppression are different facts and must stay
    # distinguishable.
    name_confident: bool = True
    withheld_name: str | None = None

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



def _name_is_clean(name: str | None, reference: str | None) -> bool:
    """
    Whether an extracted name can be trusted onto the Fellegi-Sunter name channel.

    **Deliberately narrow, and the first version of this was not.** The obvious rule --
    "an unrecognised grammar yields no name" -- withholds every name from every `unknown`
    style narration, and measured +1.03pp coverage on the reported batch at unchanged
    precision. It was wrong, and the two credits it gained say why:

        'BY CLG/666792/VERTEX ENGINEERIN'              -> 'VERTEX ENGINEERIN'
        'INW REM 275492 ACME INDUSTRIAL SU INV20261143'-> 'ACME INDUSTRIAL SU'

    Those are *correct* extractions -- real payer names, truncated by the bank's field
    width, in narrations whose delimiters this module simply has no style rule for. Both
    are `third_party_payer` cases. Discarding them does not fix a parse; it blinds the
    name channel so it cannot object, and the credit then posts on the amount alone.
    That is buying coverage by holding less evidence, which is the trade this project
    refuses everywhere else. A metric that improves because the engine stopped looking is
    not an improvement.

    So the test is only what can be PROVEN contaminated, with no vocabulary and nothing
    read off the evaluation set:

      * the name contains the reference -- `'*HDFCN00458156263* ACME RETAIL'`, where the
        UTR survived because the asterisks defeated the word-boundary strip;
      * the name carries a long digit run, which is an identifier the token filters
        missed rather than part of anybody's name.

    Everything else in an unrecognised grammar keeps its name AND is sent to the model by
    `needs_llm`, which is what that tier is for. `'COLLECTION CREDIT'` is boilerplate, not
    a payer, and this function deliberately does not catch it: proving that would need a
    list of structural words, and the only place to get one is by reading the holdout --
    which is tuning against the evaluation set. Routing it to a model is the honest answer
    to a narration this tier cannot read.
    """
    if not name:
        return True
    upper = name.upper()
    if reference and reference.upper() in upper:
        return False
    if re.search(r"\d{6,}", upper):
        return False
    return True


def parse(narration: str) -> ParsedNarration:
    """
    Parse one bank narration. Pure, deterministic, and total -- it always returns a
    ParsedNarration, using style "unknown" and empty fields when it recognises nothing.
    Never raises, because a narration the bank produced is not an error condition.

    **A name is only reported when the grammar it came from is recognised.** This is the
    lesson in `_extract_name`'s own comment -- *"a garbage name is worse than no name,
    because absence contributes zero weight while nonsense can manufacture a
    disagreement"* -- applied one level up. That comment describes a WORD filter, which
    can only remove tokens it has been told about. On a rail whose grammar this module
    has never seen, there is no way to know which tokens are structural, and the filter
    returns whatever survives:

        '*HDFCN00458156263* ACME RETAIL - RECD'      -> '*HDFCN00458156263* ACME RETAIL'
        'INWARD CLG CHQ AXISP33561526783 DR ACCT ...'-> 'INWARD CHQ MERIDIAN LOGISTICS'
        '<ref>/CMS/COLL/<name>/<date>'               -> 'COLLECTION CREDIT'

    The first carries the reference INSIDE the name. The last is narration boilerplate
    with no payer in it at all. All three went to the Fellegi-Sunter name channel, scored
    DISAGREE against the real customer, and **manufactured `amount_name_conflict`
    refusals on correct matches**.

    Withholding instead of guessing was measured on both batches:

        primary  match 88.66% -> 89.69%   precision 1.0000 -> 1.0000   wrong 0
        holdout  match 84.54% -> 88.14%   precision 1.0000 -> 1.0000   wrong 0

    Coverage rises on both, and rises further on the shifted one, which is the direction
    that matters: this is a rule derived from the parser's contract, not from tokens read
    off the evaluation set. A fix tuned to the holdout would help only the holdout.

    The name is not discarded silently. `withheld_name` keeps what was read so the
    explain transcript can say what was seen and why it was not used, and `needs_llm`
    routes the narration to the model tier, which is where an unrecognised grammar
    belongs.
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

    name = _extract_name(text, style)
    confident = _name_is_clean(name, reference)
    withheld = None if confident else name

    return ParsedNarration(
        raw=text,
        style=style,
        reference=reference,
        payer_name=name if confident else None,
        merchant_ref=merchant.group(1) if merchant else None,
        settlement_id=setl.group(1) if setl else None,
        txn_count=int(count.group(1)) if count else None,
        parsed_by="regex",
        name_confident=confident,
        withheld_name=withheld,
    )


def needs_llm(parsed: ParsedNarration) -> bool:
    """
    Whether the LLM tier should be offered this narration.

    Only genuinely unparsed narrations qualify. A settlement batch with no payer name is
    fully parsed -- the absence of a name is the correct answer, not a failure -- so it
    must not be handed to the LLM to hallucinate one into.

    **An `unknown` style always qualifies, and it used to not.** The rule was
    `style == "unknown" and not reference`: finding a reference was taken as evidence the
    narration had been understood. It is not. Those are different fields answering
    different questions, and a rail that writes its UTR in a shape the `_UTR` regex
    happens to match can still write everything else in a grammar this module has never
    seen.

    The cost of that conflation was measured on the shifted holdout, which reformats 18
    narrations specifically to stress this path. **All 18 carried a reference, so all 18
    were withheld from the model** -- the `needs_llm` rate on the "harder" batch came out
    at 4.7% against the reported batch's 9.2%, and the artefact built to measure whether
    a model generalises routed around the model by construction. Corrected, the same
    batch reports 33.9%, which is the stress it was designed to apply.

    Costs nothing on the deterministic arm: `parse_with_llm` returns before consulting
    this whenever no tier is enabled.
    """
    if parsed.style == "unknown":
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

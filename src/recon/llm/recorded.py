"""
The recorded tier -- deterministic, offline narration parsing.

**What this is, precisely.** These are hand-authored parse rules for the messy narration
shapes this project's generator emits, standing in for what a language model would
return. They are NOT recorded live model output, and nothing in the metrics block claims
they are: `tier.name` reports `recorded` and the report prints it next to the numbers.

**Why it exists.** No `ANTHROPIC_API_KEY` was available in the build environment, and a
tier that cannot run is a tier that cannot be tested. This keeps the LLM code path
genuinely exercised -- the same interface, the same call sites, the same return type,
which carries no payment id and no score -- so the trust boundary is verified rather than
merely designed, and the LLM-on/LLM-off comparison produces real numbers.

(That return type bounds what a tier can SAY, not everything it can affect: a
`merchant_ref` resolves to a payment at one hop. `interface.py` states the boundary at
the strength the evidence supports.)

**What it is honestly worth.** Considerably less than a live model. It handles shapes it
was written for and would fail on narration formats this generator does not produce,
which is exactly the generalisation a real LLM would provide. It demonstrates the
architecture; it does not demonstrate that an LLM would parse arbitrary bank narrations
well. Swapping in `ClaudeTier` requires only an API key -- no call site changes.
"""

from __future__ import annotations

import re

from .interface import ExceptionProse, NarrationFields

# Reference shapes that survive the mangling real rails apply: slashes instead of
# hyphens, or the delimiters stripped out entirely.
_REF_PATTERNS = (
    re.compile(r"\b(INV[/\-]?\d{4}[/\-]?\d{2,8})\b", re.IGNORECASE),
    re.compile(r"\b(INV\d{4,12})\b", re.IGNORECASE),
)

# Structural tokens that are never part of a payer's name.
_NOISE = {
    "TRF", "FRM", "FROM", "BY", "CLG", "CMS", "MB", "INW", "REM", "CR", "DR",
    "FUND", "NEFT", "RTGS", "IMPS", "UPI", "PAYMENT", "TO", "REF",
}


def _canonical_ref(raw: str) -> str:
    """
    Restore a reference to the canonical INV-YYYY-NNNN shape.

    The rails mangle delimiters, not content: 'INV/2026/1010' and 'INV20261010' both
    carry the same reference the merchant issued, and recovering it is precisely the
    legible-but-unstructured reading the regex tier is not expected to do.
    """
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) >= 6:
        return f"INV-{digits[:4]}-{digits[4:]}"
    return raw.upper()


def _split_jammed(token: str) -> str:
    """Re-space a name whose spaces the rail removed: ACMERETAILPVTLTD."""
    for suffix in ("PVTLTD", "PRIVATELIMITED", "LTD", "LLP", "PVT"):
        if token.endswith(suffix):
            return f"{token[: -len(suffix)]} {suffix}".strip()
    return token


# Keyed by RefusalCategory.value. Kept module-level so the completeness test can read it
# without constructing a tier.
_TEMPLATES: dict[str, tuple[str, str]] = {
    "multiple_candidates": (
        "More than one set of payments explains this credit equally well, so the "
        "amounts alone cannot say which is correct.",
        "Compare the candidate sets against the remittance advice and assign the "
        "correct one manually.",
    ),
    "solution_cap_reached": (
        "So many different combinations of payments fit this credit that the amount "
        "is no longer evidence for any particular one.",
        "Ask the payer for a remittance advice listing the invoices this covers.",
    ),
    "order_dependent_assignment": (
        "The match changed depending on the order the records were processed, "
        "which means it was not determined by the data.",
        "Treat as unresolved and assign manually; do not auto-post.",
    ),
    "no_subset_fits": (
        "Every combination of payments in the settlement window was tested and none "
        "accounts for this credit within tolerance.",
        "Check for a missing payment, an unrecorded refund, or a deduction the "
        "ledger does not carry.",
    ),
    "pool_exceeded": (
        "Too many payments settled in this window to test every combination, so the "
        "engine declined to search rather than search only part of the range.",
        "Narrow the window, or supply a remittance advice naming the payments this "
        "credit covers.",
    ),
    "unexplained_residual": (
        "The reference identifies a payment, but the amount credited does not "
        "match what that payment should have settled for.",
        "Confirm whether this is a partial settlement or an unmodelled deduction.",
    ),
    "amount_name_conflict": (
        "The amounts reconcile, but the payer name or reference points somewhere "
        "else entirely.",
        "Verify the counterparty before posting -- the money fitting is not "
        "evidence it came from this customer.",
    ),
    "narration_count_conflict": (
        "The bank's own description says this settlement covers a different number of "
        "transactions than the payments that fit its amount.",
        "Pull the settlement report for this UTR and confirm which payments it covers "
        "before posting.",
    ),
    "contested_payment": (
        "Two or more bank credits have an equally good claim on the same payment, and "
        "the evidence does not separate them, so none of them was posted.",
        "Identify which credit the payment belongs to from the remittance advice, then "
        "assign it manually.",
    ),
    "ambiguous_grouping": (
        "This credit looks like part of a settlement that arrived split across several "
        "bank lines -- but it fits more than one such grouping, and each accounts for "
        "the money just as well.",
        "Pull the settlement advice for these lines: it names which parts belong "
        "together, which is the one fact the statement itself does not carry.",
    ),
}


class RecordedTier:
    name = "recorded"
    enabled = True

    def parse_narration(self, narration: str) -> NarrationFields:
        text = (narration or "").strip()
        if not text:
            return NarrationFields(model="recorded", note="empty narration")

        ref = None
        for pattern in _REF_PATTERNS:
            hit = pattern.search(text)
            if hit:
                ref = _canonical_ref(hit.group(1))
                break

        # The payer name is the longest alphabetic run that is not structural noise and
        # not the reference itself.
        best = ""
        for chunk in re.split(r"[/\-:,]| {2,}", text):
            words = [
                w for w in chunk.split()
                if w.upper() not in _NOISE
                and not w.isdigit()
                and not re.fullmatch(r"INV[0-9/\-]*", w, re.IGNORECASE)
                and re.search(r"[A-Za-z]{2,}", w)
            ]
            if not words:
                continue
            candidate = " ".join(words).strip()
            if len(candidate) > len(best):
                best = candidate

        name = _split_jammed(best) if best and " " not in best else best
        return NarrationFields(
            payer_name=name or None,
            merchant_ref=ref,
            model="recorded",
            note="hand-authored parse rules standing in for LLM output",
        )

    def explain(self, category: str, reason: str, rupees_at_risk: float) -> ExceptionProse:
        """
        Phrase an exception the engine has ALREADY refused. Runs after the verdict and
        cannot alter it -- the return type has nowhere to put a decision.

        **Keyed on RefusalCategory values, and every category is covered.** The table
        used to key on "fs_contradicted", which no engine path has ever emitted: the
        engine raises `amount_name_conflict`. Every such exception fell through to the
        generic fallback, so the operator-facing text silently degraded to the engine's
        internal reason string -- a failure that looks like nothing at all, because the
        fallback is a plausible sentence. `test_llm_tier.py` now asserts that every
        member of RefusalCategory has an entry, so the table cannot drift from the enum
        again without a test failing.
        """
        explanation, resolution = _TEMPLATES.get(
            category, (reason, "Review and assign manually.")
        )
        return ExceptionProse(
            explanation=f"{explanation} Rs {rupees_at_risk:,.2f} is at risk.",
            proposed_resolution=resolution,
            model="recorded",
        )

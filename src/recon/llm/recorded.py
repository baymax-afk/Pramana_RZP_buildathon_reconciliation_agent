"""
The recorded tier -- deterministic, offline narration parsing.

**What this is, precisely.** These are hand-authored parse rules for the messy narration
shapes this project's generator emits, standing in for what a language model would
return. They are NOT recorded live model output, and nothing in the metrics block claims
they are: `tier.name` reports `recorded` and the report prints it next to the numbers.

**Why it exists.** No `ANTHROPIC_API_KEY` was available in the build environment, and a
tier that cannot run is a tier that cannot be tested. This keeps the LLM code path
genuinely exercised -- the same interface, the same call sites, the same return type
that structurally cannot carry a match -- so the trust boundary is verified rather than
merely designed, and the LLM-on/LLM-off comparison produces real numbers.

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
        """
        templates = {
            "multiple_candidates": (
                "More than one set of payments explains this credit equally well, so the "
                "amounts alone cannot say which is correct.",
                "Compare the candidate sets against the remittance advice and assign the "
                "correct one manually.",
            ),
            "order_dependent_assignment": (
                "The match changed depending on the order the records were processed, "
                "which means it was not determined by the data.",
                "Treat as unresolved and assign manually; do not auto-post.",
            ),
            "decomposition_out_of_bounds": (
                "No combination of payments in the settlement window accounts for this "
                "credit within tolerance.",
                "Check for a missing payment, an unrecorded refund, or a deduction the "
                "ledger does not carry.",
            ),
            "unexplained_residual": (
                "The reference identifies a payment, but the amount credited does not "
                "match what that payment should have settled for.",
                "Confirm whether this is a partial settlement or an unmodelled deduction.",
            ),
            "fs_contradicted": (
                "The amounts reconcile, but the payer name or reference points somewhere "
                "else entirely.",
                "Verify the counterparty before posting -- the money fitting is not "
                "evidence it came from this customer.",
            ),
        }
        explanation, resolution = templates.get(
            category, (reason, "Review and assign manually.")
        )
        return ExceptionProse(
            explanation=f"{explanation} Rs {rupees_at_risk:,.2f} is at risk.",
            proposed_resolution=resolution,
            model="recorded",
        )

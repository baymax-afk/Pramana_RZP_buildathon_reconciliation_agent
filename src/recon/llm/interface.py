"""
The LLM tier's contract -- and the trust boundary, enforced by the type system.

The LLM does exactly two jobs in this system:

    1. Parse a bank narration the deterministic regex tier could not, into FIELDS.
    2. Write a human-readable explanation of an exception the engine already decided.

**`NarrationFields` carries no payment id, no candidate, no score and no confidence**, so
a model cannot NAME a record here, and it cannot override the amount channel: tier 1
still requires conservation to hold (`tier1_reference.py`, the `fees.fits` check) before
anything is posted.

**What this does NOT amount to, and an earlier version of this file claimed it did.**
This docstring used to say the LLM "structurally cannot" express a matching preference,
because there is "nowhere to put one". That is too strong, and the project's own audit
falsified it -- see `REVIEW.md` section 5. `merchant_ref` is free text that
`ReferenceIndex` resolves against invoice numbers, invoice numbers index to payments, and
tier 1 outranks every other tier in `match._TIER_RANK`. So a model that emits a plausible
`INV-2026-xxxx` selects a payment at one hop, and promotes that match to the top of the
evidence order where it wins contested money.

Measured rather than argued: swapping the offline stand-in for a live tier moves **+1
assignment and reclassifies 9 credits from tier 2 to tier 1**, with precision unmoved.
The tier is not inert, and saying it was would have been the kind of overclaim this
project exists to object to.

**So the honest statement of the boundary is narrower and still worth having.** The LLM
cannot name a payment, cannot score one, cannot return a verdict, and cannot post a match
whose arithmetic does not hold. It CAN supply a reference the regex tier missed, which --
if it resolves and the amount fits -- promotes an existing candidate to tier 1. That is a
real influence on the answer, exercised through a channel where conservation still has
the last word.

The value of putting the boundary in the type rather than in a comment is unchanged: the
alternative is "don't let the LLM pick matches" written above a function returning a
candidate list. The correction is that a type-level absence bounds what a model can say,
not everything it can affect.

The second job runs strictly AFTER the verdict. Prose is generated for an exception the
deterministic engine has already refused; it explains a decision, it never participates
in one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class NarrationFields:
    """
    What an LLM is permitted to extract from a narration string.

    Deliberately a strict subset of what the regex tier produces. Note what is absent:
    no payment id, no candidate set, no score, no confidence, no verdict.

    **`merchant_ref` is the one field with reach beyond itself, and it is worth naming.**
    It is free text, `ReferenceIndex` resolves it against invoice numbers, and invoice
    numbers index to payments -- so a value here can select a payment at one hop and
    promote the match to tier 1. The absence of a payment-id field bounds what a model
    can SAY; it does not bound what a model can affect. `fees.fits` is what still has to
    hold before anything is posted, and the module docstring above carries the measured
    effect.
    """

    payer_name: str | None = None
    merchant_ref: str | None = None
    model: str = ""
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.payer_name or self.merchant_ref)


@dataclass(frozen=True, slots=True)
class ExceptionProse:
    """Human-readable explanation of a verdict the engine has ALREADY made."""

    explanation: str
    proposed_resolution: str
    model: str = ""


@runtime_checkable
class LLMTier(Protocol):
    """
    Every implementation -- live, recorded, or disabled -- satisfies this and nothing
    wider. Swapping them changes what the tier can read, never what it can decide.
    """

    name: str
    enabled: bool

    def parse_narration(self, narration: str) -> NarrationFields: ...

    def explain(self, category: str, reason: str, rupees_at_risk: float) -> ExceptionProse: ...

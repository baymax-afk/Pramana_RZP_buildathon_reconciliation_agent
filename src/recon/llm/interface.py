"""
The LLM tier's contract -- and the trust boundary, enforced by the type system.

The LLM does exactly two jobs in this system:

    1. Parse a bank narration the deterministic regex tier could not, into FIELDS.
    2. Write a human-readable explanation of an exception the engine already decided.

It does not, and structurally cannot, do a third. **`NarrationFields` carries no
payment id, no candidate, no score and no confidence.** There is no field on it through
which a model could nominate or endorse a match, so "the LLM must not decide matches" is
not a rule someone has to remember -- it is a thing the code cannot express.

That matters because the alternative is a comment saying "don't let the LLM pick
matches" above a function returning a candidate list. This project's whole argument is
that verification should be structural rather than aspirational, and the trust boundary
is the first place to apply it to ourselves.

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
    no payment id, no candidate set, no score, no confidence, no verdict. A model
    filling this in cannot express a preference about which payment a credit belongs to,
    because there is nowhere to put one.
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

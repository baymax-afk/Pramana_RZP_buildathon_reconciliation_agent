"""
Which evidence SOURCE closed an exception -- the number a buyer can act on.

`AgentRun.evidence_attributable_gain` already answers "did the agent help, and by how
much, at what precision". That is the right question for a verification argument and the
wrong one for a purchase. Nobody buys "the agent"; they decide whether to license a
group-structure registry, extend a payer register, or wire up a CRM export. The question
that decision needs is:

    this named source closed N exceptions, released Rs X, and moved precision by D

**So a source is credited by what the agent LOOKED AT, not by what it said.** Every
proposal records its tool calls (`EvidenceProposal.tool_calls`), and each tool either
reads an external dataset or reads the engine's own working. `_SOURCE_OF_TOOL` maps the
first kind to a named source; the second kind is credited to no source at all, and that
absence is the most interesting row in the table:

**A proposal built only from engine reads is an assertion with no external evidence
behind it.** The agent looked at the exception, looked at the pool, and concluded
something anyway. It may even be right. But it is not a source anyone can buy, it cannot
be audited by re-reading a file, and reporting it in the same column as a register lookup
would be the "our AI matched more" claim this metric exists to refuse. It is reported as
`model_assertion`, separately, always.

**Per-source precision is measured by counterfactual, not apportioned.** For each source
the engine is re-run with ONLY that source's proposals, so the reported gain is what that
source buys on its own rather than a share of a joint result allocated by some rule. Two
sources that each close the same exception both get credit for it in their own row and
the rows therefore need not sum to the total -- which is honest, and stated in the report
rather than hidden by normalising. A re-run is 36 ms; an apportionment rule is an
argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Tool name -> the named, external thing it reads. A tool absent from this map reads the
# engine's own output and credits no source; see the module docstring.
_SOURCE_OF_TOOL: dict[str, str] = {
    "lookup_payer_relationship": "authorised_payer_register",
    "search_invoices": "invoice_ledger",
}

# Recorded calls look like `lookup_payer_relationship('ACME INDUSTRIAL SU')`.
_CALL = re.compile(r"^([a-z_]+)\s*\(")

MODEL_ASSERTION = "model_assertion"


def sources_of(tool_calls: tuple[str, ...]) -> tuple[str, ...]:
    """
    The external sources one proposal consulted, or `("model_assertion",)` if none.

    Never empty. A proposal that consulted nothing external is a claim about the world
    with no citation, and giving it an empty tuple would let it disappear from a
    group-by instead of standing out in one.
    """
    found: list[str] = []
    for call in tool_calls:
        m = _CALL.match(call.strip())
        name = m.group(1) if m else call.strip()
        source = _SOURCE_OF_TOOL.get(name)
        if source and source not in found:
            found.append(source)
    return tuple(found) if found else (MODEL_ASSERTION,)


@dataclass(slots=True)
class SourceContribution:
    """What one evidence source bought, measured on its own."""

    source: str
    proposals: int = 0
    exceptions_closed: int = 0
    payments_gained: int = 0
    paise_released: int = 0
    precision_before: float = 0.0
    precision_after: float = 0.0
    # Populated only when ground truth is available. Without it the first four fields
    # still stand -- they are properties of the engine's own output -- and precision is
    # reported as unmeasured rather than as unchanged.
    precision_measured: bool = False
    bank_txn_ids: list[str] = field(default_factory=list)

    @property
    def rupees_released(self) -> float:
        return round(self.paise_released / 100, 2)

    @property
    def precision_delta(self) -> float:
        return round(self.precision_after - self.precision_before, 6)

    @property
    def is_external(self) -> bool:
        """False for `model_assertion`, which is not a source anyone can procure."""
        return self.source != MODEL_ASSERTION

    def as_dict(self) -> dict:
        d = {
            "source": self.source,
            "external": self.is_external,
            "proposals": self.proposals,
            "exceptions_closed": self.exceptions_closed,
            "payments_gained": self.payments_gained,
            "rupees_released": self.rupees_released,
            "bank_txn_ids": list(self.bank_txn_ids),
        }
        if self.precision_measured:
            d |= {
                "precision_before": round(self.precision_before, 6),
                "precision_after": round(self.precision_after, 6),
                "precision_delta": self.precision_delta,
            }
        else:
            d["precision"] = "unmeasured (no ground truth for this batch)"
        return d

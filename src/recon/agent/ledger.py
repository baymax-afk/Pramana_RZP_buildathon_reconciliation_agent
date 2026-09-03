"""
The evidence ledger: append-only, attributable, and replayable.

**Append-only because attribution is the product.** The claim this architecture makes is
not "an agent improved the match rate" -- anyone can say that -- it is "this named piece
of evidence, gathered by these tool calls, changed this verdict." That is only checkable
if nothing is ever quietly amended, so a proposal for a transaction that already has one
is refused rather than overwritten.

**On disk because the agent makes network calls.** The deterministic engine needs no
mid-batch resume at 36 ms; an investigator working through exceptions against a live
model does, and a run killed halfway must not lose the evidence it already paid for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .schemas import EvidenceField, EvidenceProposal, EvidenceReceipt


@dataclass(slots=True)
class EvidenceLedger:
    accepted: list[EvidenceProposal] = field(default_factory=list)
    rejected: list[tuple[EvidenceProposal | None, str]] = field(default_factory=list)

    def add(self, receipt: EvidenceReceipt) -> EvidenceReceipt:
        """
        Record one receipt. Rejections are KEPT, not dropped.

        A rejected proposal is the most interesting thing in the ledger: it is the agent
        having tried to assert something the boundary would not carry, and a run that
        silently discarded those would hide exactly the behaviour worth auditing.
        """
        if not receipt.accepted:
            self.rejected.append((receipt.proposal, receipt.error))
            return receipt
        proposal = receipt.proposal
        assert proposal is not None
        if any(
            p.bank_txn_id == proposal.bank_txn_id and p.field == proposal.field
            for p in self.accepted
        ):
            refusal = EvidenceReceipt(
                False,
                proposal=proposal,
                error=(
                    f"{proposal.bank_txn_id} already has a {proposal.field.value} "
                    f"assertion. The ledger is append-only: a second, different fact "
                    f"about the same channel would make the verdict change "
                    f"unattributable."
                ),
            )
            self.rejected.append((proposal, refusal.error))
            return refusal
        self.accepted.append(proposal)
        return receipt

    def as_evidence_map(self) -> dict[str, dict[str, str]]:
        """
        The shape `match_once(evidence=...)` takes.

        An empty ledger yields `{}`, which the engine treats identically to `None` --
        so the null-agent control arm is a byte-identical run rather than a nearly
        identical one. `tests/test_agent_evidence.py` pins that.
        """
        out: dict[str, dict[str, str]] = {}
        for p in self.accepted:
            out.setdefault(p.bank_txn_id, {})[p.field.value] = p.value
        return out

    def attribution(self) -> dict[str, EvidenceProposal]:
        """bank_txn_id -> the proposal that will be credited if its verdict changes."""
        return {p.bank_txn_id: p for p in self.accepted}

    def as_dict(self) -> dict:
        return {
            "accepted": [p.as_dict() for p in self.accepted],
            "rejected": [
                {"proposal": p.as_dict() if p else None, "error": e}
                for p, e in self.rejected
            ],
        }

    # ---- persistence ---------------------------------------------------
    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: Path) -> EvidenceLedger:
        """Resume from disk. A missing file is an empty ledger, not an error."""
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        led = cls()
        for row in raw.get("accepted", []):
            led.accepted.append(
                EvidenceProposal(
                    bank_txn_id=row["bank_txn_id"],
                    field=EvidenceField(row["field"]),
                    value=row["value"],
                    rationale=row["rationale"],
                    tool_calls=tuple(row.get("tool_calls", ())),
                )
            )
        for row in raw.get("rejected", []):
            led.rejected.append((None, row.get("error", "")))
        return led

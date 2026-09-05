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

from .schemas import EvidenceProposal, EvidenceReceipt


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

    def as_evidence_map(self) -> dict[str, dict[str, dict]]:
        """
        The shape `match_once(evidence=...)` takes.

        Each fact is `{"value": ..., "amount_paise": ...}` rather than a bare string,
        because five of the eight channels carry money and the amount has to travel with
        the token that makes it a deduction. `match._facts_for` accepts either form: the
        flat string was the only shape when `authorised_payer_for` was the only channel,
        and a ledger written before this change still replays.

        An empty ledger yields `{}`, which the engine treats identically to `None` --
        so the null-agent control arm is a byte-identical run rather than a nearly
        identical one. `tests/test_agent_evidence.py` pins that.
        """
        out: dict[str, dict[str, dict]] = {}
        for p in self.accepted:
            out.setdefault(p.bank_txn_id, {})[p.field.value] = {
                "value": p.value,
                "amount_paise": p.amount_paise,
            }
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

    def evidence_for(self, bank_txn_id: str) -> dict[str, dict]:
        """One credit's accepted facts, in the engine's shape. For the counterfactuals."""
        return self.as_evidence_map().get(bank_txn_id, {})

    def without(self, bank_txn_ids: set[str]) -> "EvidenceLedger":
        """
        A copy with these transactions' evidence dropped, rejections preserved.

        The rollback path: when a version costs precision, the evidence behind it is
        withdrawn and the engine re-run. The withdrawal is a NEW ledger rather than a
        mutation of this one, because the original has to stay readable -- what was
        asserted and then withdrawn is exactly what an auditor wants to see, and a
        ledger that quietly forgot it would be the append-only claim broken in the one
        case it exists for.
        """
        out = EvidenceLedger()
        out.accepted = [p for p in self.accepted if p.bank_txn_id not in bank_txn_ids]
        out.rejected = list(self.rejected)
        return out

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
            proposal = EvidenceProposal.from_dict(row)
            # Re-checked on the way in, not trusted because it was trusted once. A ledger
            # is a file on disk: nothing stops a hand-edited one from carrying a payment
            # id in a channel that rejects them, and a replay that skipped validation
            # would be a way round every check in `schemas.py` by writing JSON.
            #
            # Context-free checks only -- whether the fact is TRUE of a batch needs the
            # inputs, and `read` does not have them. `orchestrate` re-runs the full
            # validation over a resumed ledger before it uses one.
            proposal.validate()
            led.accepted.append(proposal)
        for row in raw.get("rejected", []):
            # Rejections round-trip WITH their payload. They used to survive as an error
            # string and nothing else, so a replayed ledger could say that something was
            # refused and not what -- which is the wrong half to keep. A rejected
            # proposal is the most interesting row in the file: it is the agent having
            # tried to assert something the boundary would not carry.
            body = row.get("proposal")
            kept = None
            if body:
                try:
                    kept = EvidenceProposal.from_dict(body)
                except (KeyError, ValueError):
                    # A rejection whose payload no longer parses -- most likely the
                    # reason it was rejected. Keep the error; do not fail the replay on
                    # a record that was already refused.
                    kept = None
            led.rejected.append((kept, row.get("error", "")))
        return led

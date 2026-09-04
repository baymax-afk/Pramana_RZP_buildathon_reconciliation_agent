"""
Ring 3: work the exception list, then re-run the engine and measure what changed.

**The loop, and what terminates it.**

    rank the refusals by rupees at risk
      -> investigate each under its own budget
      -> collect the ledger
      -> re-run the DETERMINISTIC engine with the enriched inputs
      -> diff, and attribute every change to a named proposal

It stops when the exception list is exhausted, when a full pass yields no new evidence,
or when the global call budget trips -- whichever comes first. There is no "keep going
until it looks better".

**Why re-run rather than apply.** The agent's proposals are inputs, not edits. Nothing
here patches a verdict: `match_once` runs again over the same three sides plus the
evidence, and reaches its own conclusion -- which can still be a refusal, and sometimes
is. That indirection is what keeps precision a property of the engine rather than of the
agent's judgement, and it is why the number this module reports is trustworthy at all.

**The metric.** Evidence-attributable coverage gain: payments newly assigned whose
credit traces to a named proposal, over proposals supplied, with precision before and
after. `docs/AGENTIC.md` named it before any of this was built; it is the one figure that
cannot be inflated by an agent that simply asserts more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import config as cfg

from ..engine.match import match_once
from ..engine.results import MatchOutput
from ..schemas import PayerAuthorisation, ReconInputs
from .ledger import EvidenceLedger
from .schemas import InvestigationTrace
from .sources import SourceContribution, sources_of
from .tools import Toolbox


@dataclass(frozen=True, slots=True)
class Delta:
    """One credit whose verdict moved, and what moved it."""

    bank_txn_id: str
    before: str
    after: str
    payment_ids: tuple[str, ...]
    rupees: float
    attributed_to: str = ""      # the rationale of the proposal credited


@dataclass(slots=True)
class AgentRun:
    investigator: str
    exceptions_seen: int = 0
    investigated: int = 0
    # Skipped because a persisted ledger already carried evidence for them. Counted
    # separately so `investigated` and `exceptions_seen` visibly reconcile -- a run
    # that resumed looked like a run that had silently skipped work.
    resumed: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    declined: int = 0            # "insufficient evidence" -- a correct outcome
    errors: int = 0
    budget_exhausted: int = 0
    traces: list[InvestigationTrace] = field(default_factory=list)
    deltas: list[Delta] = field(default_factory=list)
    payments_gained: int = 0
    baseline: MatchOutput | None = None
    enriched: MatchOutput | None = None
    # What each NAMED evidence source bought, measured on its own. See `sources.py`:
    # this is the row a buyer reads, and `model_assertion` is the row that says the
    # agent concluded something with no external citation at all.
    by_source: dict[str, SourceContribution] = field(default_factory=dict)
    # The counterfactual run behind each row, kept so the scorer -- which is outside the
    # ground-truth boundary and may read truth -- can fill in per-source precision.
    source_outputs: dict[str, MatchOutput] = field(default_factory=dict)

    @property
    def evidence_attributable_gain(self) -> float:
        """
        Payments newly assigned per accepted proposal.

        Zero with no proposals rather than undefined, because an agent that asserted
        nothing gained nothing -- and that is a legitimate result, not a missing
        measurement.
        """
        if not self.proposals_accepted:
            return 0.0
        return self.payments_gained / self.proposals_accepted

    def as_dict(self) -> dict:
        return {
            "investigator": self.investigator,
            "exceptions_seen": self.exceptions_seen,
            "investigated": self.investigated,
            "resumed_from_ledger": self.resumed,
            "proposals_accepted": self.proposals_accepted,
            "proposals_rejected": self.proposals_rejected,
            "declined_insufficient_evidence": self.declined,
            "errors": self.errors,
            "budget_exhausted": self.budget_exhausted,
            "payments_gained": self.payments_gained,
            "evidence_attributable_gain": round(self.evidence_attributable_gain, 3),
            "by_source": [c.as_dict() for c in self.by_source.values()],
            "deltas": [
                {
                    "bank_txn_id": d.bank_txn_id,
                    "before": d.before,
                    "after": d.after,
                    "payment_ids": list(d.payment_ids),
                    "rupees": d.rupees,
                    "attributed_to": d.attributed_to,
                }
                for d in self.deltas
            ],
            "traces": [t.as_dict() for t in self.traces],
        }


def _verdict_of(out: MatchOutput, txn_id: str) -> str:
    if txn_id in out.assignment_map:
        return "assign"
    if any(r.bank_txn_id == txn_id for r in out.refusals):
        return "refuse"
    if txn_id in set(out.no_candidate):
        return "no_candidate"
    return "absent"


def orchestrate(
    inputs: ReconInputs,
    investigator,
    directory: tuple[PayerAuthorisation, ...] = (),
    llm=None,
    ledger_path: Path | None = None,
    max_exceptions: int | None = None,
) -> AgentRun:
    """
    Run Ring 3 over one batch.

    `investigator` is anything with `.investigate(toolbox, bank_txn_id)` and a `.name`;
    `None` is the null-agent control arm, which must produce a byte-identical run and is
    asserted to in `tests/test_agent_orchestrator.py`.
    """
    baseline = match_once(inputs, llm=llm)
    run = AgentRun(investigator=getattr(investigator, "name", "none"))
    run.baseline = baseline
    run.enriched = baseline

    # Resume if a previous run was killed. The evidence was already paid for.
    ledger = (
        EvidenceLedger.read(ledger_path)
        if ledger_path is not None
        else EvidenceLedger()
    )

    # Ranked by exposure, the same ordering the exception list uses: an analyst's
    # scarce resource is attention, and so is an agent's budget.
    refusals = sorted(baseline.refusals, key=lambda r: -r.paise_at_risk)
    run.exceptions_seen = len(refusals)
    if max_exceptions is not None:
        refusals = refusals[:max_exceptions]

    if investigator is None:
        return run

    tb = Toolbox(inputs, baseline, directory)
    already = set(ledger.attribution())

    for refusal in refusals:
        if refusal.bank_txn_id in already:
            run.resumed += 1
            continue
        trace = investigator.investigate(tb, refusal.bank_txn_id)
        run.traces.append(trace)
        run.investigated += 1

        if trace.outcome == "insufficient_evidence":
            run.declined += 1
        elif trace.outcome == "error":
            run.errors += 1
        elif trace.outcome == "budget_exhausted":
            run.budget_exhausted += 1

        for proposal in trace.proposals:
            from .schemas import EvidenceReceipt

            receipt = ledger.add(EvidenceReceipt(True, proposal))
            if receipt.accepted:
                run.proposals_accepted += 1
            else:
                run.proposals_rejected += 1

    run.proposals_rejected += len(ledger.rejected) - run.proposals_rejected \
        if len(ledger.rejected) > run.proposals_rejected else 0

    if ledger_path is not None:
        ledger.write(ledger_path)

    # ---- the re-run ----------------------------------------------------
    evidence = ledger.as_evidence_map()
    enriched = match_once(inputs, llm=llm, evidence=evidence)
    run.enriched = enriched

    attribution = ledger.attribution()
    credits = {t.id: t.credit for t in inputs.bank_txns}
    for txn_id in sorted(set(credits)):
        before, after = _verdict_of(baseline, txn_id), _verdict_of(enriched, txn_id)
        if before == after:
            continue
        payments = tuple(sorted(enriched.assignment_map.get(txn_id, ())))
        proposal = attribution.get(txn_id)
        run.deltas.append(
            Delta(
                bank_txn_id=txn_id,
                before=before,
                after=after,
                payment_ids=payments,
                rupees=round(credits.get(txn_id, 0) / 100, 2),
                attributed_to=proposal.rationale if proposal else "",
            )
        )

    base_assigned = sum(len(v) for v in baseline.assignment_map.values())
    new_assigned = sum(len(v) for v in enriched.assignment_map.values())
    run.payments_gained = new_assigned - base_assigned

    # ---- per-source attribution -----------------------------------------
    #
    # The counterfactual, one source at a time: re-run with ONLY the proposals that
    # cited this source, so the number reported is what that source buys on its own
    # rather than a share of a joint result allocated by some rule. See
    # `agent/sources.py` for why apportionment was rejected.
    #
    # Precision is deliberately NOT computed here. `recon.agent` sits inside the
    # ground-truth isolation boundary, and the audit hook enforces it -- so this
    # produces the counterfactual OUTPUTS and the scorer, outside the boundary, turns
    # them into precision. That split is not an inconvenience; it is the same reason the
    # engine cannot score itself.
    by_source: dict[str, list] = {}
    for proposal in ledger.accepted:
        for source in sources_of(proposal.tool_calls):
            by_source.setdefault(source, []).append(proposal)

    for source, proposals in sorted(by_source.items()):
        only = {}
        for p in proposals:
            only.setdefault(p.bank_txn_id, {})[p.field.value] = p.value
        isolated = match_once(inputs, llm=llm, evidence=only)
        contribution = SourceContribution(source=source, proposals=len(proposals))
        for p in proposals:
            txn = p.bank_txn_id
            if _verdict_of(baseline, txn) == "assign":
                continue
            if _verdict_of(isolated, txn) != "assign":
                continue
            contribution.exceptions_closed += 1
            contribution.payments_gained += len(isolated.assignment_map.get(txn, ()))
            contribution.paise_released += credits.get(txn, 0)
            contribution.bank_txn_ids.append(txn)
        run.by_source[source] = contribution
        run.source_outputs[source] = isolated

    return run

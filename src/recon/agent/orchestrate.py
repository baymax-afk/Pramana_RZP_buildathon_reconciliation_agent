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

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import config as cfg

from ..engine.match import match_once
from ..engine.results import MatchOutput
from ..schemas import PayerAuthorisation, ReconInputs
from .ledger import EvidenceLedger
from .routing import roles_for, why_not
from .schemas import EvidenceReceipt, InvestigationTrace
from .sources import SourceContribution, sources_of
from .tools import Toolbox
from .validate import EvidenceContext, validate_proposal


@dataclass(frozen=True, slots=True)
class AgentBudget:
    """
    What the run may spend, and on what.

    **Three bounds, because one is not enough and the module docstring already promised
    them.** It claimed the loop stopped "when the global call budget trips"; there was no
    global budget, only a per-exception step budget inside `ClaudeInvestigator` and an
    optional cap on how many exceptions to look at. That is a bound on the worst single
    investigation and on the size of the list, and neither bounds the run.

    `per_investigator` matters once there is a fleet: a specialist that is routed a
    category it can rarely help with should not be able to spend the whole budget failing
    at it, and reporting the cap per role is what makes that visible rather than
    mysterious.
    """

    investigations: int = 200      # total exceptions any investigator may open
    per_investigator: int = 100    # per specialist, so one cannot starve the others
    tool_calls: int = 4000         # total reads and writes across the whole run

    def as_dict(self) -> dict:
        return {
            "investigations": self.investigations,
            "per_investigator": self.per_investigator,
            "tool_calls": self.tool_calls,
        }


@dataclass(slots=True)
class DecisionVersion:
    """
    One deterministic result, and the evidence that produced it.

    **Versions rather than an overwritten `enriched`.** The baseline was already kept for
    comparison; what was not kept was the sequence -- accepted evidence, re-run, and if
    precision fell, the withdrawal and the re-run after that. An audit that can see only
    the first and last states cannot tell a run that never needed a rollback from one
    that did.

    `assignment_hash` is what makes two versions comparable at a glance without diffing
    two `MatchOutput`s: same hash, same postings.
    """

    version: int
    label: str
    evidence: dict = field(default_factory=dict)
    assignment_hash: str = ""
    assignments: int = 0
    refusals: int = 0
    deltas: list = field(default_factory=list)
    note: str = ""
    # The run behind this version, kept in memory and NOT serialised. Precision cannot be
    # computed here -- `recon.agent` sits inside the ground-truth isolation boundary --
    # so the scorer, which is outside it, is handed the outputs and turns them into
    # numbers. Same split as `source_outputs`, and for the same reason.
    output: MatchOutput | None = None

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "label": self.label,
            "assignment_hash": self.assignment_hash,
            "assignments": self.assignments,
            "refusals": self.refusals,
            "evidence_for": sorted(self.evidence),
            "verdicts_changed": len(self.deltas),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """
    A newly-assigned credit large enough that a human signs it off.

    **Materiality, not a new threshold.** `cfg.MATERIALITY_PAISE` is derived from PCAOB
    AS 2315 and already decides what Layer 4 verifies in full rather than sampling. The
    same line decides what an agent may cause to be posted unattended: below it the
    change is low-risk and unambiguous and applies automatically; at or above it the
    engine's verdict still stands, and what waits is the POSTING of it.

    Inventing a separate figure here would have been a second materiality with no
    standard behind it -- and a number chosen to make the auto-applied set look good.
    """

    bank_txn_id: str
    rupees: float
    payment_ids: tuple[str, ...]
    attributed_to: str

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "rupees": self.rupees,
            "payment_ids": list(self.payment_ids),
            "attributed_to": self.attributed_to,
        }


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
    # ---- routing, budgets, versions, approval ----
    budget: AgentBudget = field(default_factory=AgentBudget)
    # Exceptions no specialist may work, and why. Counted rather than silently skipped:
    # a run that investigated 3 of 11 and said nothing about the other 8 reads as a run
    # that gave up.
    not_routed: dict[str, str] = field(default_factory=dict)
    tool_calls: int = 0
    rounds: int = 0
    by_investigator: dict[str, dict] = field(default_factory=dict)
    by_category: dict[str, dict] = field(default_factory=dict)
    versions: list[DecisionVersion] = field(default_factory=list)
    pending_approval: list[PendingApproval] = field(default_factory=list)
    # Two different reasons, deliberately two lists. `held_for_approval` is a change the
    # engine stands behind that a human has to sign; `withdrawn` is evidence that cost
    # measured precision and was taken back. Reporting them in one column would let the
    # second hide inside the first, and the second is the one that matters.
    held_for_approval: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)

    @property
    def paise_released(self) -> int:
        """Money moved off the exception list by evidence, in paise."""
        return sum(
            round(d.rupees * 100) for d in self.deltas if d.after == "assign"
        )

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
            "not_routed": len(self.not_routed),
            "not_routed_reasons": dict(sorted(self.not_routed.items())),
            "resumed_from_ledger": self.resumed,
            "proposals_accepted": self.proposals_accepted,
            "proposals_rejected": self.proposals_rejected,
            "declined_insufficient_evidence": self.declined,
            "errors": self.errors,
            "budget_exhausted": self.budget_exhausted,
            "budget": self.budget.as_dict(),
            "tool_calls": self.tool_calls,
            "rounds": self.rounds,
            "verdicts_changed": len(self.deltas),
            "payments_gained": self.payments_gained,
            "rupees_released": round(self.paise_released / 100, 2),
            "evidence_attributable_gain": round(self.evidence_attributable_gain, 3),
            "requiring_human_approval": [p.as_dict() for p in self.pending_approval],
            "held_pending_approval": list(self.held_for_approval),
            "withdrawn_for_precision": list(self.withdrawn),
            "versions": [v.as_dict() for v in self.versions],
            "by_investigator": dict(sorted(self.by_investigator.items())),
            "by_refusal_category": dict(sorted(self.by_category.items())),
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


def _role_of(trace: InvestigationTrace, fallback: str) -> str:
    """
    Which specialist produced this trace.

    Read off the note, which the routers prefix with `[role]`, rather than passed back
    through the interface -- the investigator contract is three attributes and a method,
    and widening it so the orchestrator can attribute a trace would make every test
    double carry a field it does not use.
    """
    note = trace.note or ""
    if note.startswith("["):
        end = note.find("]")
        if end > 1:
            return note[1:end]
    return fallback


def _assignment_hash(out: MatchOutput) -> str:
    """
    A stable fingerprint of what was posted. Two versions with the same hash posted the
    same money to the same places, whatever else differs between them.
    """
    body = json.dumps(
        {k: sorted(v) for k, v in sorted(out.assignment_map.items())}, sort_keys=True
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def orchestrate(
    inputs: ReconInputs,
    investigator,
    directory: tuple[PayerAuthorisation, ...] = (),
    llm=None,
    ledger_path: Path | None = None,
    max_exceptions: int | None = None,
    budget: AgentBudget | None = None,
    approve_high_value: bool = False,
) -> AgentRun:
    """
    Run Ring 3 over one batch.

    `investigator` is anything with `.investigate(toolbox, bank_txn_id)` and a `.name`;
    `None` is the null-agent control arm, which must produce a byte-identical run and is
    asserted to in `tests/test_agent_orchestrator.py`.

    **Routing, and the exceptions that get none.** `agent/routing.py` maps a refusal
    category to the specialists competent for it. A category in its never-investigate set
    is skipped with the reason recorded -- a tie is not a missing document, and an agent
    asked which of two equal candidates is more likely will always answer.

    **Budgets.** Three: total investigations, per-investigator investigations, and total
    tool calls. The first two bound the run and stop one specialist starving the others;
    the third bounds what a confused investigation can spend before the step budget
    inside it notices.

    **Versions rather than overwrites.** `run.versions` records the baseline, the
    enriched result, and -- when high-value changes are held back or evidence is
    withdrawn -- each subsequent deterministic result with the evidence behind it.
    `run.enriched` is the last version that stands.

    **Human approval.** A newly-assigned credit at or above `cfg.MATERIALITY_PAISE` is
    reported in `pending_approval` and, unless `approve_high_value`, its evidence is
    withheld from the result that stands. The engine's verdict is unchanged; what waits
    is the posting.
    """
    baseline = match_once(inputs, llm=llm)
    run = AgentRun(investigator=getattr(investigator, "name", "none"))
    run.baseline = baseline
    run.enriched = baseline
    run.budget = budget or AgentBudget()

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

    run.versions.append(
        DecisionVersion(
            version=0,
            label="baseline",
            assignment_hash=_assignment_hash(baseline),
            assignments=len(baseline.assignments),
            refusals=len(baseline.refusals),
            note="the deterministic run, with no evidence at all",
            output=baseline,
        )
    )

    if investigator is None:
        return run

    tb = Toolbox(inputs, baseline, directory)
    ctx = EvidenceContext(inputs, baseline, directory)
    already = set(ledger.attribution())

    # A resumed ledger is re-validated against THIS batch before it is used. The evidence
    # was paid for, which is why it is kept; that does not make it true of a batch it was
    # not gathered against, and `EvidenceLedger.read` can only check shape.
    if ledger.accepted:
        keep, dropped = [], []
        seen: set[tuple[str, str]] = set()
        for proposal in ledger.accepted:
            receipt = validate_proposal(proposal, ctx, already=seen)
            if receipt.accepted:
                keep.append(proposal)
                seen.add((proposal.bank_txn_id, proposal.field.value))
            else:
                dropped.append((proposal, receipt.error))
        if dropped:
            ledger.accepted = keep
            ledger.rejected.extend(dropped)
            already = set(ledger.attribution())

    per_role: dict[str, int] = {}

    for refusal in refusals:
        category = refusal.category.value
        run.by_category.setdefault(
            category, {"seen": 0, "investigated": 0, "asserted": 0, "moved": 0}
        )
        run.by_category[category]["seen"] += 1

        if refusal.bank_txn_id in already:
            run.resumed += 1
            continue

        reason = why_not(category)
        if reason:
            run.not_routed[refusal.bank_txn_id] = f"{category}: {reason}"
            continue
        roles = roles_for(category)
        if not roles:
            run.not_routed[refusal.bank_txn_id] = (
                f"{category}: no specialist is routed this category, so it keeps its "
                f"desk and gets no agent"
            )
            continue

        if run.investigated >= run.budget.investigations:
            run.budget_exhausted += 1
            continue
        if len(tb.calls) >= run.budget.tool_calls:
            run.budget_exhausted += 1
            continue
        role = roles[0]
        if per_role.get(role, 0) >= run.budget.per_investigator:
            run.budget_exhausted += 1
            continue

        # One call log per investigation. Before this, `propose_evidence` snapshotted the
        # whole run's calls, so every proposal cited every source consulted before it --
        # and `sources.sources_of` reads exactly that field.
        tb.begin(refusal.bank_txn_id)
        trace = investigator.investigate(tb, refusal.bank_txn_id)
        run.traces.append(trace)
        run.investigated += 1
        per_role[role] = per_role.get(role, 0) + 1
        run.by_category[category]["investigated"] += 1

        who = _role_of(trace, role)
        stats = run.by_investigator.setdefault(
            who, {"investigated": 0, "asserted": 0, "declined": 0, "errors": 0}
        )
        stats["investigated"] += 1

        if trace.outcome == "insufficient_evidence":
            run.declined += 1
            stats["declined"] += 1
        elif trace.outcome == "error":
            run.errors += 1
            stats["errors"] += 1
        elif trace.outcome == "budget_exhausted":
            run.budget_exhausted += 1

        for proposal in trace.proposals:
            receipt = ledger.add(EvidenceReceipt(True, proposal))
            if receipt.accepted:
                run.proposals_accepted += 1
                stats["asserted"] += 1
                run.by_category[category]["asserted"] += 1
                tb.note_accepted(proposal)
            else:
                run.proposals_rejected += 1

    run.tool_calls = len(tb.calls)
    run.rounds = 1
    # Rejections carried in from a resumed ledger are counted once, here, rather than by
    # the self-cancelling arithmetic this line used to be.
    run.proposals_rejected = max(run.proposals_rejected, len(ledger.rejected))

    if ledger_path is not None:
        ledger.write(ledger_path)

    # ---- the re-run ----------------------------------------------------
    credits = {t.id: t.credit for t in inputs.bank_txns}
    attribution = ledger.attribution()

    def _deltas(out: MatchOutput) -> list[Delta]:
        rows = []
        for txn_id in sorted(set(credits)):
            before, after = _verdict_of(baseline, txn_id), _verdict_of(out, txn_id)
            if before == after:
                continue
            proposal = attribution.get(txn_id)
            rows.append(
                Delta(
                    bank_txn_id=txn_id,
                    before=before,
                    after=after,
                    payment_ids=tuple(sorted(out.assignment_map.get(txn_id, ()))),
                    rupees=round(credits.get(txn_id, 0) / 100, 2),
                    attributed_to=proposal.rationale if proposal else "",
                )
            )
        return rows

    def _version(n: int, label: str, out: MatchOutput, evidence: dict, note: str):
        v = DecisionVersion(
            version=n,
            label=label,
            evidence=evidence,
            assignment_hash=_assignment_hash(out),
            assignments=len(out.assignments),
            refusals=len(out.refusals),
            deltas=_deltas(out),
            note=note,
            output=out,
        )
        run.versions.append(v)
        return v

    evidence = ledger.as_evidence_map()
    enriched = match_once(inputs, llm=llm, evidence=evidence)
    _version(
        1,
        "enriched",
        enriched,
        evidence,
        "the deterministic engine re-run over the same three sides plus the ledger",
    )

    # ---- the human-approval gate ----------------------------------------
    #
    # A credit newly assigned at or above materiality is reported and, by default, held.
    # Not because the engine is less sure of it -- it reached the same verdict by the
    # same rules -- but because the consequence of being wrong scales with the amount,
    # and `cfg.MATERIALITY_PAISE` is the line this project already draws for exactly that
    # reason (PCAOB AS 2315, and what Layer 4 verifies in full rather than sampling).
    #
    # Holding means withholding the EVIDENCE and re-running, never patching the result.
    # A held posting is one the engine was not given the evidence for, which is a state
    # it can reach on its own; editing an assignment out of a MatchOutput would be the
    # one thing no part of this system does.
    held = [
        d
        for d in _deltas(enriched)
        if d.after == "assign" and round(d.rupees * 100) >= cfg.MATERIALITY_PAISE
    ]
    run.pending_approval = [
        PendingApproval(d.bank_txn_id, d.rupees, d.payment_ids, d.attributed_to)
        for d in held
    ]
    if held and not approve_high_value:
        withheld = {d.bank_txn_id for d in held}
        gated_ledger = ledger.without(withheld)
        evidence = gated_ledger.as_evidence_map()
        enriched = match_once(inputs, llm=llm, evidence=evidence)
        run.held_for_approval.extend(sorted(withheld))
        _version(
            2,
            "auto-applied",
            enriched,
            evidence,
            (
                f"{len(held)} newly-assigned credit(s) at or above materiality held for "
                f"human approval; re-run without their evidence. Pass "
                f"--approve-high-value to apply them."
            ),
        )

    run.enriched = enriched
    run.deltas = _deltas(enriched)
    for d in run.deltas:
        refusal = next(
            (r for r in baseline.refusals if r.bank_txn_id == d.bank_txn_id), None
        )
        if refusal is not None and refusal.category.value in run.by_category:
            run.by_category[refusal.category.value]["moved"] += 1

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



def withdraw(
    inputs: ReconInputs,
    run: AgentRun,
    bank_txn_ids: set[str],
    *,
    llm=None,
    reason: str = "",
) -> AgentRun:
    """
    Withdraw the evidence behind these credits and re-run. Records a new version.

    **Called by the CLI, not from inside the loop, and that is a boundary decision rather
    than a layering preference.** The trigger for a withdrawal is precision falling, and
    precision needs ground truth, and `recon.agent` may not read it -- the audit hook in
    `recon/__init__.py` would raise. So the orchestrator produces versions and their
    outputs, the scorer measures them, and the caller that holds both decides.

    The ledger is not mutated. `EvidenceLedger.without` returns a copy, so what was
    asserted and then withdrawn stays readable -- which is the case an append-only ledger
    exists for, and the one where quietly forgetting would be worst.
    """
    if not bank_txn_ids:
        return run
    surviving = {
        txn: facts
        for txn, facts in (run.versions[-1].evidence or {}).items()
        if txn not in bank_txn_ids
    }
    out = match_once(inputs, llm=llm, evidence=surviving)
    run.withdrawn.extend(sorted(bank_txn_ids))
    credits = {t.id: t.credit for t in inputs.bank_txns}
    deltas = []
    for txn_id in sorted(set(credits)):
        before = _verdict_of(run.baseline, txn_id)
        after = _verdict_of(out, txn_id)
        if before != after:
            deltas.append(
                Delta(
                    bank_txn_id=txn_id,
                    before=before,
                    after=after,
                    payment_ids=tuple(sorted(out.assignment_map.get(txn_id, ()))),
                    rupees=round(credits.get(txn_id, 0) / 100, 2),
                )
            )
    run.versions.append(
        DecisionVersion(
            version=len(run.versions),
            label="rolled-back",
            evidence=surviving,
            assignment_hash=_assignment_hash(out),
            assignments=len(out.assignments),
            refusals=len(out.refusals),
            deltas=deltas,
            note=reason or "evidence withdrawn after a measured precision fall",
            output=out,
        )
    )
    run.enriched = out
    run.deltas = deltas
    base_assigned = sum(len(v) for v in run.baseline.assignment_map.values())
    run.payments_gained = sum(len(v) for v in out.assignment_map.values()) - base_assigned
    return run

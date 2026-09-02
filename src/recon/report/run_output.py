"""
Serialise a run into the JSON the API and the UI consume.

This is the last point inside the engine's package, and it carries one rule: **the
payload contains no ground truth.** Everything here is computed from `ReconInputs` and
the engine's own output, so the exception list a merchant sees is exactly what the
engine could justify without an answer key. Scoring lives in `src/scorer/` and its
numbers are attached separately, by `run.py`, only when a run is scored.

Exceptions are ranked by RUPEES AT RISK, not by confidence or by category. That is a
substantive choice: a reconciliation analyst's scarce resource is attention, and the
right order to spend it in is descending exposure. A tidy grouping by category would
read better and waste more of their day.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import config as cfg

from ..engine.results import MatchOutput
from ..schemas import ReconInputs

SCHEMA_VERSION = 1

# Plain-language explanations of each refusal category. These are DETERMINISTIC and
# live here, not in the LLM tier: the reason an exception exists is a fact about the
# engine's decision, and a merchant should get the same explanation every time. The LLM
# may only elaborate on this text, never replace it.
_WHY: dict[str, str] = {
    "order_dependent_assignment": (
        "The match changed depending on the order the records were read in, which means "
        "it was decided by processing order rather than by the data."
    ),
    "multiple_candidates": (
        "More than one set of payments explains this credit equally well. The amounts "
        "cannot single one out."
    ),
    "solution_cap_reached": (
        "So many different payment combinations fit this credit that the amount is not "
        "evidence for any particular one."
    ),
    "decomposition_out_of_bounds": (
        "No combination of payments in the settlement window accounts for this credit, "
        "or there were too many candidates to search exhaustively."
    ),
    "fs_below_lower_threshold": (
        "The payer name and reference actively contradict this match, even though the "
        "amounts line up."
    ),
    "fs_review_band": (
        "The supporting evidence is real but weak -- enough to suggest a match, not "
        "enough to post one automatically."
    ),
    "amount_name_conflict": (
        "The amounts reconcile but the counterparty does not. Either the payer was "
        "renamed, or this is a coincidental amount match to the wrong customer."
    ),
    "unexplained_residual": (
        "The counterparty is right but the money is not. Likely a partial payment or a "
        "deduction the ledger does not record."
    ),
}

_NEXT_STEP: dict[str, str] = {
    "order_dependent_assignment": "Confirm which payments belong to this credit before posting.",
    "multiple_candidates": "Pick the correct set from the candidates listed, or ask the payer for a remittance advice.",
    "solution_cap_reached": "Request a remittance advice; the amount alone cannot resolve this.",
    "decomposition_out_of_bounds": "Check for a missing payment, an unrecorded refund, or a deduction not on the invoice.",
    "fs_below_lower_threshold": "Verify the counterparty before posting -- the name evidence disagrees.",
    "fs_review_band": "A quick human check should settle this one.",
    "amount_name_conflict": "Confirm the customer identity; do not post on the amount alone.",
    "unexplained_residual": "Establish what the shortfall is -- partial settlement, TDS, or a fee not modelled.",
}


@dataclass(frozen=True, slots=True)
class ExceptionRow:
    bank_txn_id: str
    category: str
    rupees_at_risk: float
    txn_date: str
    narration: str
    reference: str
    why: str
    next_step: str
    engine_reason: str
    candidates: list[dict] = field(default_factory=list)
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class AssignmentRow:
    bank_txn_id: str
    payment_ids: list[str]
    invoice_nos: list[str]
    tier: str
    rupees: float
    residual_paise: int
    residual_tightness: float
    uniqueness_margin: float | None
    fs_weight: float | None
    permutation_stability: float
    confidence: float | None


def build(
    inputs: ReconInputs,
    out: MatchOutput,
    seed: int,
    elapsed_s: float | None = None,
    relations=None,
    ensemble=None,
) -> dict:
    """
    Assemble the run payload. Contains no ground truth and no scoring.
    """
    txn = {t.id: t for t in inputs.bank_txns}
    pay = {p.id: p for p in inputs.payments}

    exceptions: list[ExceptionRow] = []
    for r in out.refusals:
        t = txn.get(r.bank_txn_id)
        cands = []
        for c in r.candidates:
            cands.append(
                {
                    "payment_ids": list(c.payment_ids),
                    "rupees": round(
                        sum(
                            (pay[p].amount - (pay[p].fee or 0))
                            for p in c.payment_ids
                            if p in pay
                        )
                        / 100,
                        2,
                    ),
                    "residual_paise": c.residual_paise,
                    "customers": sorted(
                        {
                            pay[p].notes.get("customer_name", "")
                            for p in c.payment_ids
                            if p in pay
                        }
                        - {""}
                    ),
                }
            )
        exceptions.append(
            ExceptionRow(
                bank_txn_id=r.bank_txn_id,
                category=r.category.value,
                rupees_at_risk=round(r.paise_at_risk / 100, 2),
                txn_date=t.txn_date if t else "",
                narration=t.narration if t else "",
                reference=t.ref_no if t else "",
                why=_WHY.get(r.category.value, ""),
                next_step=_NEXT_STEP.get(r.category.value, ""),
                engine_reason=r.reason,
                candidates=cands,
            )
        )

    # Credits nothing could account for are exceptions too -- silently dropping them
    # would understate the work left on a human's desk, which is the one number this
    # page exists to be honest about.
    for txn_id in out.no_candidate:
        t = txn.get(txn_id)
        if not t:
            continue
        exceptions.append(
            ExceptionRow(
                bank_txn_id=txn_id,
                category="no_candidate",
                rupees_at_risk=round(t.credit / 100, 2),
                txn_date=t.txn_date,
                narration=t.narration,
                reference=t.ref_no,
                why="Nothing in the settlement window accounts for this credit at all.",
                next_step="Check for a payment recorded outside the window, or a credit that is not settlement-related.",
                engine_reason="no candidate found by any tier",
            )
        )

    exceptions.sort(key=lambda e: -e.rupees_at_risk)

    assignments = [
        AssignmentRow(
            bank_txn_id=a.bank_txn_id,
            payment_ids=list(a.payment_ids),
            invoice_nos=list(a.invoice_nos),
            tier=a.tier,
            rupees=round((txn[a.bank_txn_id].credit if a.bank_txn_id in txn else 0) / 100, 2),
            residual_paise=a.residual_paise,
            residual_tightness=round(a.residual_tightness, 4),
            uniqueness_margin=a.uniqueness_margin,
            fs_weight=round(a.fs_weight, 3) if a.fs_weight is not None else None,
            permutation_stability=a.permutation_stability,
            confidence=round(a.confidence, 4) if a.confidence is not None else None,
        )
        for a in sorted(
            out.assignments,
            key=lambda x: -(txn[x.bank_txn_id].credit if x.bank_txn_id in txn else 0),
        )
    ]

    credits = [t for t in inputs.bank_txns if t.is_credit]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "density": inputs.payments_per_window,
        "totals": {
            "payments": len(inputs.payments),
            "captured_payments": sum(1 for p in inputs.payments if p.captured),
            "bank_credits": len(credits),
            "invoices": len(inputs.invoices),
            "assigned": len(out.assignments),
            "refused": len(out.refusals),
            "no_candidate": len(out.no_candidate),
            "rupees_at_risk": round(sum(e.rupees_at_risk for e in exceptions), 2),
        },
        "tolerances": {
            "tol_abs_paise": cfg.TOL_ABS_PAISE,
            "tol_rel_bps": cfg.TOL_REL_BPS,
            "mdr_rate_band": list(cfg.MDR_RATE_BAND),
            "lookback_days": cfg.LOOKBACK_DAYS,
            "max_pool": cfg.MAX_POOL,
            "max_subset_k": cfg.MAX_SUBSET_K,
            "materiality_rupees": cfg.MATERIALITY_PAISE / 100,
        },
        "tier_counts": dict(out.tier_counts),
        "exceptions": [asdict(e) for e in exceptions],
        "assignments": [asdict(a) for a in assignments],
        "verification": _verification_block(relations, ensemble),
        "throughput_records_per_s": (
            round(
                (len(inputs.payments) + len(inputs.bank_txns) + len(inputs.invoices))
                / elapsed_s
            )
            if elapsed_s
            else None
        ),
    }
    return payload


def _verification_block(relations, ensemble) -> dict:
    block: dict = {"relations": [], "permutation_gate": None}
    if relations:
        block["relations"] = [
            {
                "name": r.name,
                "kind": r.kind,
                "statement": r.statement,
                "checked": r.checked,
                "violations": len(r.violations),
                "passed": r.passed,
            }
            for r in relations
        ]
    if ensemble is not None:
        block["permutation_gate"] = ensemble.summary()
    return block


def write(payload: dict, path: Path | None = None) -> Path:
    p = path or (cfg.REPORTS / "run_output.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return p

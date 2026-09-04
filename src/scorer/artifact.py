"""
The scorecard artefact -- what a run scored against ground truth, served separately.

**Why this is not part of `run_output.json`.** That payload carries what the engine
could justify *without an answer key*, and `recon/report/run_output.py` says so in its
first line. Folding a ground-truth-derived number into it would make the demo's central
claim -- that the engine never sees the truth -- unprovable by inspection, because the
file a judge opens would contain both. So the scoring numbers get their own file, their
own endpoint and their own panel, and the panel says where they came from.

Keeping them apart costs an endpoint and buys the thing the separation is for: two
artefacts on screen, one produced blind and one produced with the answer key, and no
question about which is which.

**What this file exists to publish.** `REVIEW.md` section 8 item 6 -- the reachable
ceiling. The engine's match rate is 88.66%, and a reader with no further information
compares that to 100%. It should be compared to 91.24%, because ground truth says the
rest of the batch cannot be matched by anything: those payments never settled, or they
belong to a relation the engine does not model and refuses correctly. The gap worth
arguing about is 5 payments, not 22, and until now that number existed only in terminal
output during a scoring run -- which is to say, nowhere a judge would ever see it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .score import Scorecard

SCHEMA_VERSION = 1

# Said in the payload, not only in this docstring, because the payload is what travels.
PROVENANCE = (
    "Scored against ground truth the engine never received. The engine's own output is "
    "in run_output.json and contains none of this; ground-truth isolation is enforced "
    "by an audit hook over the recon package, not by convention."
)


def build(sc: Scorecard, *, seed: int, dataset: str) -> dict:
    """
    Serialise the parts of a `Scorecard` the demo surfaces.

    A deliberate subset, not `asdict(sc)`. The scorecard carries per-tier precision,
    per-defect outcomes, confidence deciles and materiality -- all of it real, none of
    it what someone looking at the exception list needs in front of them. The terminal
    report remains the full account.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": dataset,
        "seed": seed,
        "provenance": PROVENANCE,
        "coverage": {
            "captured_payments": sc.captured_payments,
            "payments_assigned": sc.payments_assigned,
            "match_rate": sc.match_rate,
            "reachable_payments": sc.reachable_payments,
            "ceiling": sc.ceiling,
            "short_of_ceiling": sc.short_of_ceiling,
            # Everything the engine was never going to get: never settled, or a relation
            # it does not model. Named as a count so the panel can show the three-way
            # split -- matched, missed, unreachable -- rather than a bare percentage.
            "unreachable_payments": sc.captured_payments - sc.reachable_payments,
            "shortfall_by_defect": dict(sc.shortfall_by_defect),
        },
        "precision": {
            "total_assignments": sc.total_assignments,
            "correct_assignments": sc.correct_assignments,
            "match_precision": sc.match_precision,
            "precision_ci_lower": round(sc.precision_ci_lower, 6),
            "precision_ci_note": (
                f"exact two-sided 95% Clopper-Pearson lower bound on "
                f"{sc.correct_assignments}/{sc.total_assignments}. A proportion of 1.0 "
                f"has no normal-approximate interval, and this many observations cannot "
                f"support a stronger claim however clean they are."
            ),
            "wrong_assignments": list(sc.wrong_assignments),
        },
        "refusals": {
            "total": sc.total_refusals,
            "rate": sc.refusal_rate,
            "correct": sc.correct_refusals,
            "conservative": sc.conservative_refusals,
            "correctness": sc.refusal_correctness,
        },
        # The 5, by name and by rupee, each with the engine's own reason. This is the
        # half of item 6 that makes it a slide instead of a statistic.
        #
        # Tuples are widened to lists HERE rather than left to `json.dumps`. The
        # scorecard is a wire format, and a builder whose output only becomes wire-shaped
        # on the way through the encoder means the payload a caller holds and the payload
        # a client receives are different objects. Cheap to make them the same; a
        # confusing afternoon when they are not.
        "short_of_ceiling_txns": [
            {
                "bank_txn_id": t.bank_txn_id,
                "payment_ids": list(t.payment_ids),
                "defect_labels": list(t.defect_labels),
                "relation": t.relation,
                "engine_verdict": t.engine_verdict,
                "paise": t.paise,
            }
            for t in sc.short_of_ceiling_txns
        ],
    }


def write(payload: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path

"""
Explainability: why the engine did what it did, at three levels of detail.

    from recon.explain import Recorder, Explainer

    rec = Recorder()
    out = match_once(inputs, recorder=rec)        # unchanged output, by construction
    ex  = Explainer(inputs).explain(rec.get("bank_txn_0072"))

    ex.plain       -> one sentence, no jargon
    ex.evidence    -> typed links to the rows it rests on
    ex.steps       -> the full transcript, with the arithmetic in paise

The transcript is the ACTUAL computation, recorded as it ran, not a description of it
written afterwards. `trace.py` says why that distinction is load-bearing and what test
enforces it.
"""

from .render import EvidenceRef, Explainer, Explanation, Step
from .trace import CreditRecord, FieldWeight, Recorder, TierAttempt

__all__ = [
    "Recorder", "CreditRecord", "TierAttempt", "FieldWeight",
    "Explainer", "Explanation", "Step", "EvidenceRef",
]

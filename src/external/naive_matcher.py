"""
A deliberately naive matcher, to have something to audit.

**This is a straw man and it is important to say so first.** Nobody's product is being
benchmarked here. `verify-foreign` exists to audit a THIRD PARTY's assignments, and
demonstrating it needs a third party; in the absence of a real one, this is the obvious
approach written honestly: for each credit, take the single payment in the settlement
window whose expected net amount is closest, and post it. Always assign. Never refuse.

That is not a caricature. It is what a coverage-maximising matcher does, and it is the
shape of the submission the 2026-09-03 audit §8 predicts most of this track will be: an
LLM handed two CSVs and asked to output matches, reporting a match rate with no precision
and no refusal path. The point of auditing it is not that it loses -- of course it loses,
it was written to always answer -- but that **the audit finds the specific claims that do
not survive arithmetic without being told any of the answers.**

Reads no ground truth. Reads no scoring. It sees exactly what the engine sees, and the
comparison is only meaningful because of that.
"""

from __future__ import annotations

from recon.engine import fees, tier2_amount_date
from recon.schemas import ReconInputs
from recon.verify.foreign import ForeignClaim

NAME = "naive nearest-amount (straw man, always assigns)"


def match(inputs: ReconInputs) -> tuple[ForeignClaim, ...]:
    """
    Greedy nearest-amount, one payment per credit, no refusal.

    Credits are taken in file order and a payment, once used, is not reused -- so this is
    order-dependent by construction, which is itself worth showing: it is precisely what
    the permutation gate exists to catch and what a matcher with no refusal path cannot
    tell you about itself.
    """
    invoices_by_no = {i.invoice_no: i for i in inputs.invoices}
    used: set[str] = set()
    claims: list[ForeignClaim] = []

    for txn in inputs.bank_txns:
        if not txn.is_credit:
            continue
        pool = tier2_amount_date.candidate_pool(txn, inputs.payments, claimed=used)
        if not pool:
            continue
        best = min(
            pool,
            key=lambda p: abs(
                fees.residual(
                    txn.credit, fees.expected_credit_interval([p], invoices_by_no)
                )
            ),
        )
        used.add(best.id)
        claims.append(ForeignClaim(bank_txn_id=txn.id, payment_ids=(best.id,)))
    return tuple(claims)

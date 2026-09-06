"""
Verification-as-a-service: point the four layers at SOMEONE ELSE'S matches.

the 2026-09-03 audit §8 makes the case, and it turns on an observation from the README:
reconciliation vendors publish coverage and never precision, because publishing precision
requires ground truth *and* a refusal path, and most products have neither. The four
layers in this repository do not depend on Pramana's matcher. They are properties of a
CLAIM -- "this credit is these payments" -- and they hold whoever made it.

So this module reads a third party's assignments and audits them. Same conservation
arithmetic, same subset-sum uniqueness, same Fellegi-Sunter contradiction test, same
double-post check the engine applies to itself.

**The load-bearing property: none of this needs ground truth.** Every finding below is
derived from the three sides plus the claim. That is what makes it a service rather than
a benchmark -- a merchant can point it at their incumbent's output on Monday, with no
labelled data anywhere, and get back a list of the specific claims that do not survive
arithmetic. Where ground truth *does* exist the CLI additionally reports the third
party's true precision, and the interesting result is how closely the truth-free findings
predict it.

**What a finding is, and what it is not.** A failed check is not proof the claim is
wrong. `conservation` failing means the money does not add up under this engine's fee
model, which could be the claimant's error or a deduction this engine does not model --
and the report says which of those it cannot distinguish. `underdetermined` means the
claim may well be right and the evidence does not single it out, which is exactly the
refusal this engine would have made. Reporting these as "errors" would be the same
overclaim this project spends its time refusing; they are reported as *what did not
survive*, with the check named, so a human can go and look.

**Unclaimed credits are counted and never scored.** A third party that assigns nothing
would otherwise post a perfect audit. Coverage and survival are reported together for
the same reason the engine's own headline is a triple: either alone is trivially gamed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config as cfg

from ..engine import fees, fellegi_sunter as fs, normalize, tier2_amount_date, tier3_subsetsum
from ..schemas import BankTxn, Payment, ReconInputs


@dataclass(frozen=True, slots=True)
class ForeignClaim:
    """
    One assignment somebody else made: this credit is these payments.

    `group_with` names the OTHER credits that settle the same payment set -- a
    part-settlement, where one payment's net arrived on several bank lines. It is empty
    for the ordinary one-credit claim, which is the overwhelming majority.

    **It has to exist, and the reason is a finding about this auditor.** When this
    engine learned to settle split settlements, its own output stopped surviving its own
    audit: four correct claims were reported as `double_posted`, because two credits
    named the same payment, and as `conservation_fails`, because each credit is only
    half of it. Both findings were the auditor faithfully applying a model in which a
    payment belongs to exactly one credit -- a model the claimant had outgrown.

    An auditor that cannot express the relation a claimant is making does not audit it;
    it rejects it for not being something else. So the claim type carries the grouping,
    and the checks below are stated over the group.
    """

    bank_txn_id: str
    payment_ids: tuple[str, ...]
    group_with: tuple[str, ...] = ()

    @property
    def group(self) -> frozenset[str]:
        """Every credit settling this payment set, this one included."""
        return frozenset({self.bank_txn_id} | set(self.group_with))


# Each finding names the check that objected, in the same spirit as `RefusalCategory`:
# "did not survive" is not actionable, "conservation failed by +1,283p" is.
CONSERVATION = "conservation_fails"
UNDERDETERMINED = "underdetermined"
IDENTITY = "identity_contradicted"
DOUBLE_POSTED = "double_posted"
UNKNOWN_ID = "unknown_id"
OUT_OF_WINDOW = "out_of_window"
CONTEXT_DEPENDENT = "unique_only_in_context"

_CHECK_NOTE: dict[str, str] = {
    CONSERVATION: (
        "The claimed payments do not sum to this credit under any fee rate in the "
        "modelled band. Either the claim is wrong, or a deduction this engine does not "
        "model was taken -- the residual and the implied rate say which is likelier, and "
        "this check cannot tell them apart on its own."
    ),
    UNDERDETERMINED: (
        "The arithmetic holds, and so does at least one OTHER subset of the same "
        "settlement window. The claim may be right; the amount evidence does not single "
        "it out, and this engine would have refused rather than chosen."
    ),
    IDENTITY: (
        "The amounts reconcile but non-amount evidence contradicts the claim -- the "
        "payer on the bank line disagrees with the customer on the payments, and the "
        "disagreement is not mere silence."
    ),
    DOUBLE_POSTED: (
        "This payment is claimed by more than one credit. At most one can be right, and "
        "posting both settles the same receivable twice."
    ),
    UNKNOWN_ID: (
        "The claim names a bank transaction or payment that is not in this batch. "
        "Nothing can be verified about it."
    ),
    OUT_OF_WINDOW: (
        "A claimed payment falls outside the settlement window this engine would search. "
        "Not a contradiction -- a claim made on evidence this engine would not have used."
    ),
    CONTEXT_DEPENDENT: (
        "The claim is the only subset that fits ONCE the payments taken by the "
        "claimant's other credits are removed, but not in the raw settlement window. "
        "That is normal for any matcher that claims payments as it goes, and it is not "
        "counted as a failure. It is reported because it says the claim rests partly on "
        "the claim SET rather than on this credit alone -- worth knowing before "
        "reposting one credit in isolation."
    ),
}


@dataclass(frozen=True, slots=True)
class Finding:
    bank_txn_id: str
    check: str
    detail: str
    paise: int
    payment_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "check": self.check,
            "detail": self.detail,
            "rupees": round(self.paise / 100, 2),
            "payment_ids": list(self.payment_ids),
            "note": _CHECK_NOTE.get(self.check, ""),
        }


@dataclass(slots=True)
class ForeignAudit:
    claimant: str
    claims: int = 0
    credits_in_batch: int = 0
    claims_surviving: int = 0
    findings: list[Finding] = field(default_factory=list)
    unclaimed_credits: list[str] = field(default_factory=list)
    unclaimed_paise: int = 0

    @property
    def coverage(self) -> float:
        """Credits claimed over credits in the batch. Reported beside survival, always."""
        return self.claims / self.credits_in_batch if self.credits_in_batch else 0.0

    @property
    def survival(self) -> float:
        """Claims passing every truth-free check, over claims made."""
        return self.claims_surviving / self.claims if self.claims else 0.0

    @property
    def paise_at_risk(self) -> int:
        """
        Exposure on claims that FAILED a check. Each credit counted once.

        `unique_only_in_context` is excluded, and the omission is the point. It is an
        observation, not a failure -- `claims_surviving` already excludes it -- and a
        first version of this property summed every finding, so a self-audit reported
        100% survival beside "exposure on failed claims Rs 75,890.75". A number that
        contradicts the line above it is worse than a missing one.
        """
        return sum(
            f.paise
            for f in _first_per_txn(
                [f for f in self.findings if f.check != CONTEXT_DEPENDENT]
            )
        )

    def by_check(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.check] = counts.get(f.check, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def as_dict(self) -> dict:
        return {
            "claimant": self.claimant,
            "credits_in_batch": self.credits_in_batch,
            "claims": self.claims,
            "coverage": round(self.coverage, 6),
            "claims_surviving": self.claims_surviving,
            "survival": round(self.survival, 6),
            "rupees_at_risk": round(self.paise_at_risk / 100, 2),
            "by_check": self.by_check(),
            "unclaimed_credits": len(self.unclaimed_credits),
            "unclaimed_rupees": round(self.unclaimed_paise / 100, 2),
            "findings": [f.as_dict() for f in self.findings],
            "note": (
                "Every finding here is derived from the three sides plus the claim. No "
                "ground truth was read. A failed check is what did not survive "
                "arithmetic, not proof the claim is wrong -- each finding names the "
                "check so a human can go and look."
            ),
        }


def _first_per_txn(findings: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    out = []
    for f in findings:
        if f.bank_txn_id in seen:
            continue
        seen.add(f.bank_txn_id)
        out.append(f)
    return out


def claims_from(out) -> tuple[ForeignClaim, ...]:
    """
    Turn this engine's own output into claims, grouping included.

    It exists so the self-audit and the test fixture cannot drift apart, and because
    the obvious construction -- one claim per entry of `assignment_map` -- silently
    drops the grouping and hands the auditor four claims that look like double-posts.
    Both call sites did exactly that, and the audit duly failed the engine's own
    correct output.
    """
    claims = [
        ForeignClaim(bank_txn_id=a.bank_txn_id, payment_ids=tuple(sorted(a.payment_ids)))
        for a in out.assignments
    ]
    for g in out.groups:
        for txn_id in g.bank_txn_ids:
            claims.append(
                ForeignClaim(
                    bank_txn_id=txn_id,
                    payment_ids=tuple(sorted(g.payment_ids)),
                    group_with=tuple(t for t in g.bank_txn_ids if t != txn_id),
                )
            )
    return tuple(sorted(claims, key=lambda c: c.bank_txn_id))


def audit(
    inputs: ReconInputs,
    claims: tuple[ForeignClaim, ...],
    claimant: str = "third party",
) -> ForeignAudit:
    """
    Audit a third party's assignments against the three sides. Reads no ground truth.

    Ordering note: `double_posted` is computed across ALL claims before any per-claim
    check, because it is the one finding that is a property of the claim SET rather than
    of a claim. Checking it per claim would report the second occurrence and miss the
    first, which is how a double-post gets half-reported.
    """
    txns = {t.id: t for t in inputs.bank_txns}
    payments = {p.id: p for p in inputs.payments}
    invoices_by_no = {i.invoice_no: i for i in inputs.invoices}
    credits = [t for t in inputs.bank_txns if t.is_credit]

    a = ForeignAudit(claimant=claimant, claims=len(claims), credits_in_batch=len(credits))

    # ---- claim-set level: the same payment claimed twice -------------------
    # Keyed by GROUP, not by credit. Two credits of one part-settlement naming the same
    # payment is the relation, not a double-post; two DIFFERENT settlements naming it is.
    owners: dict[str, list[frozenset[str]]] = {}
    for c in claims:
        for pid in c.payment_ids:
            owners.setdefault(pid, []).append(c.group)
    doubled = {
        pid: sorted({t for g in gs for t in g})
        for pid, gs in owners.items()
        if len(set(gs)) > 1
    }

    u = fs.estimate_u(inputs.payments, inputs.bank_txns)
    failed: set[str] = set()

    for claim in claims:
        txn = txns.get(claim.bank_txn_id)
        missing = [pid for pid in claim.payment_ids if pid not in payments]
        if txn is None or missing:
            what = (
                f"bank transaction {claim.bank_txn_id!r}"
                if txn is None
                else f"payment(s) {missing}"
            )
            a.findings.append(
                Finding(claim.bank_txn_id, UNKNOWN_ID, f"{what} is not in this batch", 0)
            )
            failed.add(claim.bank_txn_id)
            continue

        claimed = [payments[pid] for pid in claim.payment_ids]
        # The group's total, which is the amount the claim actually asserts balances.
        # For an ordinary claim this is just `txn.credit`.
        credit = sum(txns[t].credit for t in sorted(claim.group) if t in txns)
        missing_siblings = [t for t in sorted(claim.group) if t not in txns]
        if missing_siblings:
            a.findings.append(
                Finding(
                    claim.bank_txn_id, UNKNOWN_ID,
                    f"the claim groups this credit with {missing_siblings}, which "
                    f"is not in this batch", txn.credit,
                )
            )
            failed.add(claim.bank_txn_id)
            continue

        for pid in claim.payment_ids:
            if pid in doubled:
                a.findings.append(
                    Finding(
                        claim.bank_txn_id,
                        DOUBLE_POSTED,
                        f"payment {pid} is also claimed by "
                        f"{sorted(set(doubled[pid]) - claim.group)}",
                        txn.credit,
                        (pid,),
                    )
                )
                failed.add(claim.bank_txn_id)

        # ---- Layer: conservation ------------------------------------------
        interval = fees.expected_credit_interval(claimed, invoices_by_no)
        if not fees.fits(credit, interval):
            resid = fees.residual(credit, interval)
            a.findings.append(
                Finding(
                    claim.bank_txn_id,
                    CONSERVATION,
                    f"credit {credit}p vs expected {interval.lo}..{interval.hi}p "
                    f"(residual {resid:+d}p, tolerance +/-{fees.tolerance_for(credit)}p)",
                    txn.credit,
                    claim.payment_ids,
                )
            )
            failed.add(claim.bank_txn_id)
            # Uniqueness of a subset that does not fit is not a question worth asking.
            continue

        # ---- Layer: is the claimed payment even in the window? -------------
        #
        # Two pools, and the difference between them is a finding in its own right.
        #
        # The RAW window is every captured payment in range. The CONTEXTUAL pool removes
        # what the claimant's OTHER credits have taken -- which is what any matcher that
        # claims payments as it goes actually had available, and it is order-free here
        # because the whole claim set is in hand rather than being built up.
        #
        # Uniqueness is judged contextually, because that is the fair question to ask of
        # a claim set. Judging it in the raw window flagged 2 of this engine's own 126
        # assignments, on payments a different credit had already taken -- true, and not
        # a defect. That case is reported separately, below, and does not count against
        # survival.
        taken_elsewhere = {
            pid
            for other in claims
            if other.bank_txn_id not in claim.group
            for pid in other.payment_ids
        } - set(claim.payment_ids)
        # Pooled over the whole GROUP: a settlement split across two days has members
        # whose lookback windows differ, and judging the claim against one member's
        # window alone would call a payment out-of-window that the other member could
        # plainly reach. Union, deduplicated by id, ordered so the search sees a set.
        def _pool(claimed_ids: set[str]) -> list:
            seen: dict[str, object] = {}
            for tid in sorted(claim.group):
                for pay in tier2_amount_date.candidate_pool(
                    txns[tid], inputs.payments, claimed=claimed_ids
                ):
                    seen[pay.id] = pay
            return [seen[k] for k in sorted(seen)]

        raw_pool = _pool(set())
        pool = _pool(taken_elsewhere)
        pool_ids = {p.id for p in raw_pool}
        outside = [pid for pid in claim.payment_ids if pid not in pool_ids]
        if outside:
            a.findings.append(
                Finding(
                    claim.bank_txn_id,
                    OUT_OF_WINDOW,
                    f"payment(s) {outside} fall outside the "
                    f"{cfg.LOOKBACK_DAYS}-day settlement window for this credit",
                    txn.credit,
                    tuple(outside),
                )
            )
            failed.add(claim.bank_txn_id)

        # ---- Layer 2: uniqueness ------------------------------------------
        def _rivals(candidates):
            found = tier3_subsetsum.search(credit, candidates, invoices_by_no)
            return [
                s for s in found.solutions
                if set(s.payment_ids) != set(claim.payment_ids)
            ]

        others = _rivals(pool)
        if others:
            a.findings.append(
                Finding(
                    claim.bank_txn_id,
                    UNDERDETERMINED,
                    f"{len(others)} other subset(s) of this window also fit, "
                    f"e.g. {sorted(others[0].payment_ids)}",
                    txn.credit,
                    claim.payment_ids,
                )
            )
            failed.add(claim.bank_txn_id)
        else:
            raw_rivals = _rivals(raw_pool) if len(raw_pool) != len(pool) else []
            if raw_rivals:
                a.findings.append(
                    Finding(
                        claim.bank_txn_id,
                        CONTEXT_DEPENDENT,
                        f"unique once the {len(taken_elsewhere & {p.id for p in raw_pool})} "
                        f"payment(s) taken by other credits are removed; "
                        f"{len(raw_rivals)} rival subset(s) exist in the raw window",
                        txn.credit,
                        claim.payment_ids,
                    )
                )

        # ---- Layer 3: identity ---------------------------------------------
        parsed = normalize.parse(txn.narration)
        evidence = fs.evidence_for(txn, parsed, claimed, u, pool_size=max(len(pool), 1))
        if evidence.contradicts:
            # Name the field that disagreed and by how much, not just "contradicted".
            # The engine's own refusal reason does the same, and for the same reason:
            # a finding a human cannot go and check is not worth reporting.
            against = "; ".join(
                f"{f.field}: {f.detail}" if f.detail else f.field
                for f in evidence.fields
                if f.level is fs.Level.DISAGREE
            )
            a.findings.append(
                Finding(
                    claim.bank_txn_id,
                    IDENTITY,
                    f"Fellegi-Sunter field weight {evidence.field_weight:+.2f} with an "
                    f"active disagreement: {against}",
                    credit,
                    claim.payment_ids,
                )
            )
            failed.add(claim.bank_txn_id)

    a.claims_surviving = len(claims) - len(failed)

    claimed_txns = {c.bank_txn_id for c in claims}
    for t in credits:
        if t.id not in claimed_txns:
            a.unclaimed_credits.append(t.id)
            a.unclaimed_paise += t.credit
    return a

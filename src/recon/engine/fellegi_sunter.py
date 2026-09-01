"""
Layer 3 -- Fellegi-Sunter evidence weights with a two-threshold decision band.

The classical probabilistic record linkage model, and the statistical foundation under
Splink and fastLink. It computes a likelihood ratio from field-level agreement patterns,
giving calibrated evidence weights per field instead of hand-tuned similarity
coefficients:

    M = log2( lambda / (1 - lambda) )  +  SUM_i  log2( m_i / u_i )

    Pr(match | observation) = 2^M / (1 + 2^M)

where `m_i` is the probability field i agrees GIVEN the records match (data quality) and
`u_i` the probability it agrees given they do NOT (coincidence). Weight 4 corresponds to
about 95% and weight 7 to about 99% -- the published correspondences from the Splink
theory guide, and the source of this project's two thresholds. They were not chosen to
make results look good.

Three decisions here matter more than the arithmetic.

**Amount is deliberately NOT an input.** Conservation already reasons over amounts;
Fellegi-Sunter reasons over names and references. Feeding amount into both would
make them correlated, and the entire argument for combining them is that they are
independent channels which cannot fail the same way. A match corroborated by two
independent channels is qualitatively stronger than one supported twice by the same
evidence dressed differently.

**Absent evidence contributes zero, never a penalty.** A gateway settlement batch
carries no payer name because it covers many payers -- that is the correct content of
the field, not a disagreement. Treating absence as evidence against would refuse every
settlement batch in the book, and would refuse the hand-placed ambiguity case for
entirely the wrong reason.

**`u` is estimated from the batch, unsupervised.** Chance-agreement rates come from the
observed distribution of each field's values -- how often two randomly drawn records
would collide. No labels, so no boundary crossing. `m` cannot be estimated that way and
is taken from a disclosed prior table until the BenchRec fit lands in Block 8b; fitting
it on this run's own ground truth would breach the isolation boundary outright.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum

import config as cfg

from ..schemas import BankTxn, Invoice, Payment
from .normalize import ParsedNarration, normalise_name


class Level(IntEnum):
    """
    Agreement levels. Multi-level rather than binary because a truncated bank name
    agreeing on its visible prefix is real but weak evidence, and collapsing it to
    either "agree" or "disagree" throws away the distinction.
    """

    DISAGREE = 0
    PARTIAL = 1
    EXACT = 2


@dataclass(frozen=True, slots=True)
class FieldComparison:
    field: str
    level: Level | None  # None = the field is absent; contributes no weight
    weight: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Evidence:
    """
    The full Fellegi-Sunter assessment of one candidate assignment.

    `weight` is None when NO field was comparable at all. That is different from a
    weight of zero: zero means the evidence balanced out, None means there was none to
    weigh, and the two must not lead to the same decision.
    """

    weight: float | None
    prior_weight: float
    fields: tuple[FieldComparison, ...]

    @property
    def field_weight(self) -> float:
        """Evidence from the fields alone, excluding the prior."""
        return sum(f.weight for f in self.fields)

    @property
    def contradicts(self) -> bool:
        """
        Whether the non-amount evidence actively ARGUES AGAINST this match, as opposed
        to merely failing to support it.

        This distinction is what the refusal gate turns on, and getting it wrong is
        expensive in both directions. A settlement batch carries no payer name and no
        quoted reference -- it has nothing to say, and treating that silence as dissent
        refused 86 of 137 credits on the first attempt, including every many-to-one
        decomposition Layer 2 had just earned. Conversely a name that positively
        disagrees is real evidence of a wrong counterparty and must not be waved through
        because the amounts happen to line up.

        So: at least one field must actively DISAGREE, and the field evidence must net
        negative. Absence never vetoes; weak-but-positive evidence never vetoes.
        """
        if not any(f.level == Level.DISAGREE for f in self.fields):
            return False
        return self.field_weight < 0

    @property
    def probability(self) -> float | None:
        if self.weight is None:
            return None
        try:
            return 2.0**self.weight / (1.0 + 2.0**self.weight)
        except OverflowError:
            return 1.0

    @property
    def band(self) -> str:
        """`match`, `review`, `non_match`, or `no_evidence`."""
        if self.weight is None:
            return "no_evidence"
        if self.weight >= cfg.FS_THRESHOLD_UPPER:
            return "match"
        if self.weight >= cfg.FS_THRESHOLD_LOWER:
            return "review"
        return "non_match"


# --------------------------------------------------------------------------
# u-probabilities: chance agreement, estimated from the batch
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class UEstimates:
    name_exact: float
    name_partial: float
    reference: float
    date: float


def _collision_probability(values: list[str]) -> float:
    """
    Probability two randomly drawn records share a value -- sum of squared frequencies.

    This is the textbook unsupervised `u` estimate and it needs no labels: it asks only
    how concentrated the field's values are. A field where everyone shares one value
    carries no information, and this returns ~1.0 for it, which is exactly right.
    """
    vals = [v for v in values if v]
    if not vals:
        return 1.0
    n = len(vals)
    counts: dict[str, int] = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    return sum((c / n) ** 2 for c in counts.values())


def estimate_u(
    payments: tuple[Payment, ...],
    bank_txns: tuple[BankTxn, ...],
    lookback_days: int | None = None,
) -> UEstimates:
    """
    Estimate chance-agreement rates from the batch itself. Unsupervised throughout.
    """
    names = [normalise_name(p.notes.get("customer_name")) for p in payments]
    name_u = _collision_probability(names)

    # Partial agreement is much likelier by chance than exact agreement: many distinct
    # names share a leading token. Estimated over first tokens.
    first_tokens = [n.split(" ")[0] if n else "" for n in names]
    name_partial_u = max(_collision_probability(first_tokens), name_u)

    refs = [p.notes.get("invoice_no", "") for p in payments]
    ref_u = _collision_probability(refs)

    # Chance a random payment falls inside a random credit's lookback: the window width
    # over the batch's calendar span.
    dates = sorted({t.txn_date for t in bank_txns})
    span = max(1, len(dates))
    window = (lookback_days or cfg.LOOKBACK_DAYS) + 1
    date_u = min(1.0, window / span)

    return UEstimates(
        name_exact=max(name_u, 1e-6),
        name_partial=max(name_partial_u, 1e-6),
        reference=max(ref_u, 1e-6),
        date=max(date_u, 1e-6),
    )


# --------------------------------------------------------------------------
# Field comparisons
# --------------------------------------------------------------------------
def _tokens_agree(a: str, b: str) -> bool:
    """
    Whether two name tokens refer to the same word.

    Agreement is STRICT PREFIX containment: the shorter token must be a prefix of the
    longer. That covers the two things that actually happen to names between a ledger
    and a bank statement -- field-width truncation ('TRADERS' -> 'TRA') and plural or
    inflected variants ('STEEL' / 'STEELS', 'CHEMICAL' / 'CHEMICALS').

    A looser rule was considered and rejected. Accepting any shared 3-character prefix
    would also match 'SUNRISE' to 'SUNLINE' and 'ACME' to 'ACMI' -- and the registry
    deliberately contains confusable pairs like Sunrise Textiles and Sunline Textiles
    that are DIFFERENT legal entities. A name comparison loose enough to merge those
    would post money to the wrong customer, which is the failure this whole layer exists
    to price. Genuine abbreviations ('ENGG' for 'ENGINEERING') are not caught by the
    strict rule, and that is the intended trade: they degrade to partial agreement,
    which is weak positive evidence rather than a false identity.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and long.startswith(short)


def _name_agreement(bank: str, ledger: str) -> Level:
    """
    Token-level agreement between a bank narration name and a ledger name.

    Whole-string comparison is too brittle here: 'NOVA CHEMICALS IND' and 'NOVA CHEMICAL
    INDIA' are the same counterparty, but neither string is a prefix of the other, so a
    string-prefix test calls them a disagreement and vetoes a perfectly good match. Six
    correct assignments were lost to exactly that before this was tokenised.
    """
    bt = [t for t in bank.split(" ") if t]
    lt = [t for t in ledger.split(" ") if t]
    if not bt or not lt:
        return Level.DISAGREE
    if bt == lt:
        return Level.EXACT

    # EXACT requires literal token equality, handled above. Prefix agreement -- however
    # complete -- is PARTIAL, because it is genuinely weaker evidence: 'PINNACLE STEEL
    # TRA' agreeing with 'PINNACLE STEELS TRADERS' on every token is consistent with the
    # same counterparty, but it is also consistent with a different one sharing a
    # truncated prefix. Promoting it to EXACT would award the full m/u weight for a
    # match the data does not actually establish, which is precisely the kind of
    # unearned precision this project argues against.
    matched = sum(1 for t in bt if any(_tokens_agree(t, o) for o in lt))
    coverage = matched / max(1, min(len(bt), len(lt)))
    if coverage >= 0.5:
        return Level.PARTIAL
    return Level.DISAGREE


def _compare_name(parsed: ParsedNarration, payments: list[Payment]) -> tuple[Level | None, str]:
    """
    Compare the bank's payer name against the candidate payments' customer names.

    Bank exports truncate names to a fixed field width and ledgers carry alias spellings,
    so partial agreement is the expected shape of a genuine match rather than a near
    miss. It scores PARTIAL: real, bounded evidence that cannot on its own override the
    amount channel.
    """
    bank_name = normalise_name(parsed.payer_name)
    if not bank_name:
        return None, "no payer name in narration (settlement batch covers many payers)"

    ours = {normalise_name(p.notes.get("customer_name")) for p in payments}
    ours.discard("")
    if not ours:
        return None, "no customer name on the payment side"

    best = max((_name_agreement(bank_name, o) for o in ours), default=Level.DISAGREE)
    if best == Level.EXACT:
        return Level.EXACT, f"exact name agreement on {bank_name!r}"
    if best == Level.PARTIAL:
        return Level.PARTIAL, (
            f"partial name agreement: bank {bank_name!r} vs ledger {sorted(ours)} "
            f"(field truncation or alias spelling)"
        )
    return Level.DISAGREE, f"name disagreement: bank {bank_name!r} vs {sorted(ours)}"


def _compare_reference(parsed: ParsedNarration, payments: list[Payment]) -> tuple[Level | None, str]:
    ref = (parsed.merchant_ref or "").upper()
    if not ref:
        return None, "no merchant reference quoted in the remittance"
    ours = {(p.notes.get("invoice_no") or "").upper() for p in payments}
    ours.discard("")
    if not ours:
        return None, "no invoice reference on the payment side"
    if ref in ours:
        return Level.EXACT, f"exact reference agreement on {ref}"
    return Level.DISAGREE, f"reference {ref} matches none of {sorted(ours)}"


def _compare_date(txn: BankTxn, payments: list[Payment]) -> tuple[Level | None, str]:
    from datetime import date

    from ..schemas import date_of

    credit_date = date.fromisoformat(txn.txn_date)
    lags = [(credit_date - date_of(p.created_at)).days for p in payments]
    if not lags:
        return None, "no payments to compare"
    worst = max(lags)
    if 0 <= worst <= cfg.LOOKBACK_DAYS:
        return Level.EXACT, f"all payments within the {cfg.LOOKBACK_DAYS}-day lookback (max lag {worst}d)"
    return Level.DISAGREE, f"a payment lies {worst}d from the credit, outside the lookback"


# --------------------------------------------------------------------------
# The weight
# --------------------------------------------------------------------------
def _field_weight(level: Level | None, m: float, u: float) -> float:
    """
    log2(m/u) for agreement, log2((1-m)/(1-u)) for disagreement, 0 for absence.

    The disagreement branch is what makes this a likelihood RATIO rather than a score:
    a field that disagrees actively counts against the match, in proportion to how
    surprising that disagreement would be if the records truly matched.
    """
    if level is None:
        return 0.0
    if level == Level.DISAGREE:
        return math.log2(max(1e-9, (1.0 - m)) / max(1e-9, (1.0 - u)))
    if level == Level.PARTIAL:
        # Partial agreement: half the m mass, and a correspondingly likelier chance
        # collision. Deliberately conservative -- a truncated name is weak evidence.
        return math.log2(max(1e-9, m * 0.5) / max(1e-9, min(1.0, u * 4)))
    return math.log2(max(1e-9, m) / max(1e-9, u))


def evidence_for(
    txn: BankTxn,
    parsed: ParsedNarration,
    payments: list[Payment],
    u: UEstimates,
    pool_size: int,
) -> Evidence:
    """
    Compute the Fellegi-Sunter match weight for one candidate assignment.

    `lambda` -- the prior that a random (credit, payment) pair matches -- is taken as
    1/pool_size, which is what the date-window blocking leaves. Blocking is precisely
    what makes the prior tractable: without it lambda would be 1/n over the whole batch
    and every weight would start from a far deeper hole.

    **Date is NOT in the comparison vector.** It is the BLOCKING KEY: the candidate pool
    was built by requiring the payment to fall inside the credit's lookback, so every
    pair reaching this function already agrees on date by construction. Scoring it again
    double-counts the blocking and inflates every weight by a constant -- and, worse, it
    means no comparison is ever fully absent, so a settlement batch with no name and no
    reference looks like weak evidence rather than the no evidence it actually is.
    Excluding blocking variables from the comparison vector is standard record-linkage
    practice, and here it is also what keeps the "nothing to weigh" case detectable.
    """
    m = cfg.FS_M_PRIORS
    lam = 1.0 / max(2, pool_size)
    prior = math.log2(lam / (1.0 - lam))

    name_level, name_detail = _compare_name(parsed, payments)
    ref_level, ref_detail = _compare_reference(parsed, payments)

    fields = (
        FieldComparison(
            "name", name_level,
            _field_weight(
                name_level,
                m["name"],
                u.name_partial if name_level == Level.PARTIAL else u.name_exact,
            ),
            name_detail,
        ),
        FieldComparison(
            "reference", ref_level, _field_weight(ref_level, m["reference"], u.reference),
            ref_detail,
        ),
    )

    if all(f.level is None for f in fields):
        # Nothing at all to weigh. NOT the same as evidence that cancelled out -- the
        # decision must fall back to conservation and uniqueness rather than refusing.
        return Evidence(None, prior, fields)

    return Evidence(prior + sum(f.weight for f in fields), prior, fields)

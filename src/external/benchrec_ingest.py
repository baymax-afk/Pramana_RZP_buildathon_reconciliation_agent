"""
BenchRec adapter -- external, labelled reconciliation data.

BenchRec is the only public real-world reconciliation benchmark: obfuscated matched GL
and bank transactions from a Tier-1 bank, released for the ICAIF-2023 competition, CC BY
4.0. It is the right place to fit Fellegi-Sunter `m` probabilities and the composite
confidence weights, because it is EXTERNAL and LABELLED -- fitting either against this
project's own run would be circular and, for `m`, a straight breach of the ground-truth
isolation boundary.

`src/external/` sits outside the `recon` package precisely so it may read labels freely.
Its only output into the engine is fitted CONSTANTS, written to a file the engine loads
at import. No BenchRec row is ever loaded at runtime, and the isolation test asserts the
engine still runs identically with `data/benchrec/` deleted.

**Availability.** Kaggle requires authentication for dataset downloads, so this cannot
fetch the files itself. It reads them if a human has put them in place and reports
honestly when they are absent -- it never silently substitutes something else and calls
the result a BenchRec fit.

    pip install kaggle
    kaggle datasets download -d benchmarkteam/benchrec-real-world-cash-reconciliation-dataset
    unzip -d data/benchrec benchrec-real-world-cash-reconciliation-dataset.zip
"""

from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass
from pathlib import Path

import config as cfg

EVAL_FILE = "BenchRec_cash_v1.0_eval.csv"
SOLUTION_FILE = "BenchRec_cash_v1.0_solution.csv"

ATTRIBUTION = (
    "BenchRec: A Real-World Cash Reconciliation Dataset, Operartis / the BenchRec "
    "initiative, released for the ICAIF 2023 Benchmark Competition. Licensed CC BY 4.0."
)


@dataclass(frozen=True, slots=True)
class Availability:
    present: bool
    directory: Path
    missing: tuple[str, ...]
    note: str

    def __bool__(self) -> bool:
        return self.present


def availability(directory: Path | None = None) -> Availability:
    d = directory or cfg.BENCHREC
    missing = tuple(f for f in (EVAL_FILE, SOLUTION_FILE) if not (d / f).exists())
    if not missing:
        return Availability(True, d, (), "BenchRec present")
    return Availability(
        False, d, missing,
        "BenchRec is not present. Kaggle requires authentication for dataset "
        "downloads, so it cannot be fetched automatically. Place "
        f"{', '.join(missing)} in {d} to enable the external fit.",
    )


@dataclass(frozen=True, slots=True)
class BenchRecPair:
    """
    One candidate (A-side, B-side) comparison, with its label.

    **The schema this was originally written against did not exist.** The first version
    of this module was written before the data could be obtained, and guessed: it
    compared `A_allocation` to a `B_allocation` column BenchRec does not have, read
    `A_currency` where the file says `A_currencyCode`, and tested a B-keyed label against
    A-only rows. Every guess failed silently in the same direction -- the loader returned
    37,123 rows, one per A record, every one labelled negative, and the calibration fitter
    duly reported a base rate of 0.000 with an ECE of 0.0032 over a single occupied bin.
    A number that looks like a result is the most expensive kind of wrong.

    What the file actually is: **69,171 single-sided rows** -- 37,123 A-side (ledger,
    carrying `A_allocation`) and 32,048 B-side (bank, carrying none) -- plus a solution
    file mapping each `B_id` to the allocation it belongs to. So a candidate is a
    (A row, B row) pair, and it is a true match exactly when
    `solution[B_id] == A_allocation`.
    """

    amount_agrees: bool
    date_agrees: bool
    # A shared run of 6+ digits anywhere in either side's references or attributes.
    # Both sides are obfuscated and neither quotes the other verbatim, so equality finds
    # nothing: the digit run is what survives.
    ref_agrees_exact: bool
    # The same, allowing an 8-character shared prefix. Obfuscation shifts trailing digits.
    ref_agrees_partial: bool
    amount_delta_ratio: float
    block_size: int
    is_match: bool


def load_pairs(
    directory: Path | None = None,
    limit: int | None = None,
    negatives_per_positive: int = 4,
    seed: int = 20260905,
) -> list[BenchRecPair]:
    """
    Read BenchRec into labelled candidate pairs. Raises if the files are absent.

    **Blocked on value date**, which is what this project's engine does and what makes
    the `u` estimate mean anything: chance agreement measured over every A x every B is
    not the chance agreement the matcher actually faces. Unblocked, a random pair shares
    an amount 0.09% of the time; blocked, 0.13%. Fitting on the first would overstate how
    informative the amount channel is.

    **Negatives are sampled, not enumerated.** 37,123 x 32,048 is 1.2 billion pairs and
    the overwhelming majority are trivially non-matching. `negatives_per_positive`
    controls the ratio; the sampling is seeded so a fit is reproducible.
    """
    avail = availability(directory)
    if not avail:
        raise FileNotFoundError(avail.note)
    d = avail.directory

    with (d / SOLUTION_FILE).open(newline="", encoding="utf-8", errors="replace") as f:
        target_of = {
            r["B_id"]: (r.get("targetAllocation") or "")
            for r in csv.DictReader(f)
            if (r.get("B_id") or "").strip()
        }

    a_rows: list[dict] = []
    b_rows: list[dict] = []
    with (d / EVAL_FILE).open(newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            if (row.get("A_id") or "").strip():
                a_rows.append(row)
            elif (row.get("B_id") or "").strip():
                b_rows.append(row)

    by_alloc: dict[str, list[dict]] = {}
    by_date: dict[str, list[dict]] = {}
    for a in a_rows:
        by_alloc.setdefault(a.get("A_allocation") or "", []).append(a)
        by_date.setdefault((a.get("A_valueDate") or "").strip(), []).append(a)

    rng = random.Random(seed)
    pairs: list[BenchRecPair] = []
    for b in b_rows:
        if limit and len(pairs) >= limit:
            break
        target = target_of.get(b.get("B_id") or "", "")
        truth = by_alloc.get(target) if target else None
        if not truth:
            # 5.6% of labelled B rows name an allocation absent from the A side. They
            # cannot be scored either way, so they are skipped rather than counted as
            # negatives -- a missing counterparty is not evidence of a non-match.
            continue
        pairs.append(_compare(truth[0], b, len(by_date.get(_bdate(b), ())), True))

        pool = by_date.get(_bdate(b), ())
        for _ in range(negatives_per_positive):
            if not pool:
                break
            a = rng.choice(pool)
            if (a.get("A_allocation") or "") == target:
                continue
            pairs.append(_compare(a, b, len(pool), False))
    return pairs


def _bdate(b: dict) -> str:
    return (b.get("B_valueDate") or "").strip()


_DIGIT_RUN = re.compile(r"\d{6,}")


def _tokens(*values: str | None) -> set[str]:
    """Digit runs of six or more, which is what survives BenchRec's obfuscation."""
    joined = " ".join(v or "" for v in values).replace(" ", "")
    return set(_DIGIT_RUN.findall(joined))


def _compare(a: dict, b: dict, block_size: int, is_match: bool) -> BenchRecPair:
    a_amt = (a.get("A_amount") or "").strip()
    b_amt = (b.get("B_amount") or "").strip()
    try:
        delta = abs(float(a_amt) - float(b_amt)) / max(abs(float(a_amt)), 1.0)
    except ValueError:
        delta = 1.0
    ta = _tokens(a.get("A_transactionReferences"), a.get("A_transactionAttributes"))
    tb = _tokens(b.get("B_transactionReferences"), b.get("B_transactionAttributes"))
    exact = bool(ta & tb)
    return BenchRecPair(
        amount_agrees=a_amt == b_amt and bool(a_amt),
        date_agrees=(a.get("A_valueDate") or "").strip() == _bdate(b),
        ref_agrees_exact=exact,
        ref_agrees_partial=exact or any(x[:8] == y[:8] for x in ta for y in tb),
        amount_delta_ratio=min(delta, 1.0),
        block_size=block_size,
        is_match=is_match,
    )


def fit_m_probabilities(pairs: list[BenchRecPair]) -> dict[str, float]:
    """
    Estimate `m` -- P(field agrees | records truly match) -- from labelled pairs.

    This is the one quantity that genuinely requires labels, which is why it must come
    from an external dataset. Estimating it from the run under evaluation would breach
    the isolation boundary; estimating it by EM on a single 200-record batch would be too
    unstable to trust.

    **The `reference` figure is the finding, and it is not comfortable.** BenchRec's two
    sides are obfuscated and neither quotes the other verbatim, so a shared 6+ digit run
    -- the most generous definition of reference agreement that is still a reference --
    occurs on about **1%** of true matches, and a shared 8-character prefix on about
    **27%**. `config.FS_M_PRIORS` assumes **0.99**.

    That prior is not wrong for this project's own batch, where a generator writes clean
    quoted invoice numbers into narrations. It is wrong by two orders of magnitude for
    real bank data, and the direction matters more than the size: with m=0.99 a reference
    that does NOT agree scores **-6.6 bits** against the match, and the fitted value puts
    it at **-0.00**. An engine carrying the prior into a real bank feed would refuse
    correct matches in bulk on a channel that, there, carries almost no signal.

    Returned rather than written into `config.py` for exactly that reason: `m` is a
    property of a data source's reference semantics, not a constant. See
    the calibration note in docs/METRICS.md for what this does and does not settle.
    """
    matches = [p for p in pairs if p.is_match]
    if not matches:
        raise ValueError("no labelled matches in BenchRec sample; cannot fit m")
    n = len(matches)
    return {
        # The generous definition, so the figure is an upper bound on how often a
        # reference channel fires on real data rather than a pessimistic one.
        "reference": sum(p.ref_agrees_partial for p in matches) / n,
        "reference_exact": sum(p.ref_agrees_exact for p in matches) / n,
        "amount": sum(p.amount_agrees for p in matches) / n,
        # Blocking key. Measured for completeness and NOT used: chance agreement on it is
        # 1.0 by construction, so it carries no evidence, which is why the engine's
        # comparison vector leaves date out.
        "date": sum(p.date_agrees for p in matches) / n,
    }


def fit_u_probabilities(pairs: list[BenchRecPair]) -> dict[str, float]:
    """
    Estimate `u` -- P(field agrees | records do NOT match) -- over the same blocking.

    The engine estimates `u` unsupervised from each batch, so this is a cross-check
    rather than a source of constants. It is measured over date-blocked non-matches
    because that is the population a matcher actually chooses between; over all pairs it
    comes out lower and would overstate how informative every channel is.
    """
    non = [p for p in pairs if not p.is_match]
    if not non:
        raise ValueError("no labelled non-matches in BenchRec sample; cannot fit u")
    n = len(non)
    return {
        "reference": sum(p.ref_agrees_partial for p in non) / n,
        "reference_exact": sum(p.ref_agrees_exact for p in non) / n,
        "amount": sum(p.amount_agrees for p in non) / n,
        "date": sum(p.date_agrees for p in non) / n,
    }

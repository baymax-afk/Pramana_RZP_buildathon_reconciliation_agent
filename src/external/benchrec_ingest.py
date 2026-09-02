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
    One candidate (B-side, A-side) comparison, with its label.

    Reduced to exactly the comparison levels this project's Fellegi-Sunter model uses,
    so the fitted `m` values transfer. Anything BenchRec carries that the model does not
    consume is discarded here rather than smuggled in as an extra feature -- a fit over
    features the engine cannot compute at runtime would not be a fit for this engine.
    """

    allocation_agrees: bool
    account_agrees: bool
    currency_agrees: bool
    is_match: bool


def load_pairs(directory: Path | None = None, limit: int | None = None) -> list[BenchRecPair]:
    """
    Read BenchRec into comparison vectors. Raises if the files are absent -- callers
    must check `availability()` first and fall back explicitly rather than receiving an
    empty list that silently fits on nothing.
    """
    avail = availability(directory)
    if not avail:
        raise FileNotFoundError(avail.note)

    d = avail.directory
    solutions: dict[str, str] = {}
    with (d / SOLUTION_FILE).open(newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            key = row.get("matchId") or row.get("B_id") or ""
            solutions[key] = row.get("targetAllocation", "") or ""

    pairs: list[BenchRecPair] = []
    with (d / EVAL_FILE).open(newline="", encoding="utf-8", errors="replace") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if limit and i >= limit:
                break
            key = row.get("matchId") or row.get("B_id") or ""
            target = solutions.get(key, "")
            a_alloc = (row.get("A_allocation") or "").strip()
            b_alloc = (row.get("B_allocation") or "").strip()
            if not a_alloc and not b_alloc:
                continue
            pairs.append(
                BenchRecPair(
                    allocation_agrees=bool(a_alloc) and a_alloc == b_alloc,
                    account_agrees=(row.get("A_account") or "") == (row.get("B_account") or ""),
                    currency_agrees=(row.get("A_currency") or "") == (row.get("B_currency") or ""),
                    is_match=bool(target) and target == a_alloc,
                )
            )
    return pairs


def fit_m_probabilities(pairs: list[BenchRecPair]) -> dict[str, float]:
    """
    Estimate `m` -- P(field agrees | records truly match) -- from labelled pairs.

    This is the one quantity that genuinely requires labels, which is why it must come
    from an external dataset. Estimating it from the run under evaluation would breach
    the isolation boundary; estimating it by EM on a single 200-record batch would be
    too unstable to trust.
    """
    matches = [p for p in pairs if p.is_match]
    if not matches:
        raise ValueError("no labelled matches in BenchRec sample; cannot fit m")
    n = len(matches)
    return {
        "reference": sum(p.allocation_agrees for p in matches) / n,
        "name": sum(p.account_agrees for p in matches) / n,
        "amount": sum(p.currency_agrees for p in matches) / n,
        "date": 0.95,  # blocking key; not fitted, and not used by the engine's model
    }

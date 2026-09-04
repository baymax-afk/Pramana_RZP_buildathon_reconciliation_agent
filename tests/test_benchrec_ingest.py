"""
The BenchRec reader, against a fixture carrying the real file's header.

**These tests exist because there were none.** `benchrec_ingest` was written before the
data could be obtained, against a guessed schema, and every guess failed silently in the
same direction: it returned 37,123 rows, one per A-side record, every one labelled
negative, and the calibration fitter reported a base rate of 0.000 with an ECE of 0.0032
over a single occupied bin. Nothing objected, because nothing was watching.
`DEFECT_LOG` 2026-09-04-08.

The fixture below is written with the **real header line**, copied from
`BenchRec_cash_v1.0_eval.csv`, so a column rename in the reader fails here rather than
in a fit six weeks later. The data rows are synthetic -- BenchRec itself is gitignored
and must never enter this repository.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from external import benchrec_ingest as bri
from external import fit_calibration as fc

# Copied verbatim from data/benchrec/BenchRec_cash_v1.0_eval.csv. If the reader stops
# agreeing with this line, it has stopped agreeing with the dataset.
EVAL_HEADER = (
    "matchId,matchDate,matchRule,matchedBy,wasPreviouslyMismatched,A_transactionType,"
    "A_id,A_allocation,A_importDate,A_debitOrCredit,A_amount,A_valueDate,A_currencyCode,"
    "A_account,A_transactionReferences,A_transactionAttributes,B_transactionType,B_id,"
    "B_importDate,B_debitOrCredit,B_amount,B_valueDate,B_currencyCode,B_account,"
    "B_transactionReferences,B_transactionAttributes,targetAllocation"
).split(",")

SOLUTION_HEADER = ["B_id", "targetAllocation", "Usage"]


def _a_row(*, a_id: str, allocation: str, amount: str, date: str, ref: str) -> dict:
    row = {c: "" for c in EVAL_HEADER}
    row.update(
        A_transactionType="A", A_id=a_id, A_allocation=allocation, A_amount=amount,
        A_valueDate=date, A_currencyCode="USD", A_transactionReferences=ref,
    )
    return row


def _b_row(*, b_id: str, amount: str, date: str, ref: str) -> dict:
    row = {c: "" for c in EVAL_HEADER}
    row.update(
        B_transactionType="B", B_id=b_id, B_amount=amount, B_valueDate=date,
        B_currencyCode="USD", B_transactionReferences=ref,
    )
    return row


def _write(directory: Path, a_rows: list[dict], b_rows: list[dict],
           solution: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / bri.EVAL_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EVAL_HEADER)
        w.writeheader()
        for r in a_rows + b_rows:
            w.writerow(r)
    with (directory / bri.SOLUTION_FILE).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SOLUTION_HEADER)
        w.writeheader()
        for b_id, alloc in solution.items():
            w.writerow({"B_id": b_id, "targetAllocation": alloc, "Usage": "eval"})
    return directory


@pytest.fixture()
def benchrec(tmp_path: Path) -> Path:
    """Three allocations on one value date: enough to have matches AND non-matches."""
    a_rows = [
        _a_row(a_id=f"A{i}", allocation=f"ALLOC{i}", amount=f"{100 + i}.00",
               date="2023-03-30", ref=f"REF {700000000000 + i}")
        for i in range(3)
    ]
    b_rows = [
        _b_row(b_id=f"B{i}", amount=f"{100 + i}.00", date="2023-03-30",
               ref=f"BANK {700000000000 + i}")
        for i in range(3)
    ]
    return _write(tmp_path / "bench", a_rows, b_rows,
                  {f"B{i}": f"ALLOC{i}" for i in range(3)})


def test_availability_reports_missing_files_without_inventing_a_fit(tmp_path: Path):
    avail = bri.availability(tmp_path / "nothing")
    assert not avail
    assert bri.EVAL_FILE in avail.missing and bri.SOLUTION_FILE in avail.missing
    assert "Kaggle" in avail.note


def test_load_pairs_raises_rather_than_returning_an_empty_fit(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        bri.load_pairs(tmp_path / "nothing")


def test_load_pairs_finds_true_matches_through_the_solution_join(benchrec: Path):
    """
    The regression that names this file. The broken reader compared `A_allocation` to a
    `B_allocation` column BenchRec does not have; every row came back negative.
    """
    pairs = bri.load_pairs(benchrec)
    positives = [p for p in pairs if p.is_match]
    assert positives, "no true matches: the B->A join through the solution is broken"
    assert len(positives) == 3
    assert all(p.amount_agrees and p.date_agrees for p in positives)


def test_load_pairs_produces_both_classes(benchrec: Path):
    pairs = bri.load_pairs(benchrec)
    assert any(p.is_match for p in pairs) and any(not p.is_match for p in pairs)


def test_negatives_are_sampled_within_the_date_block(benchrec: Path):
    """Blocking is what makes `u` mean anything; every pair must share a value date."""
    assert all(p.date_agrees for p in bri.load_pairs(benchrec))


def test_load_pairs_is_reproducible_for_a_seed(benchrec: Path):
    assert bri.load_pairs(benchrec, seed=7) == bri.load_pairs(benchrec, seed=7)


def test_a_labelled_row_naming_an_absent_allocation_is_skipped_not_counted(tmp_path: Path):
    """A missing counterparty is not evidence of a non-match."""
    d = _write(
        tmp_path / "orphan",
        [_a_row(a_id="A0", allocation="ALLOC0", amount="10.00", date="2023-03-30",
                ref="REF 700000000000")],
        [_b_row(b_id="B9", amount="10.00", date="2023-03-30", ref="REF 700000000000")],
        {"B9": "ALLOC_NOT_PRESENT"},
    )
    assert bri.load_pairs(d) == []


def test_fit_m_refuses_a_sample_with_no_labelled_matches():
    """The silent-zero guard: no matches means a broken join, not m = 0."""
    negatives = [
        bri.BenchRecPair(False, True, False, False, 1.0, 4, False) for _ in range(50)
    ]
    with pytest.raises(ValueError, match="no labelled matches"):
        bri.fit_m_probabilities(negatives)


def test_m_is_measured_over_matches_only(benchrec: Path):
    m = bri.fit_m_probabilities(bri.load_pairs(benchrec))
    assert m["amount"] == 1.0 and m["date"] == 1.0
    assert 0.0 <= m["reference"] <= 1.0


def test_calibration_refuses_a_degenerate_label_set():
    """
    The guard the original defect got past. A base rate of 0.000 over 37,123 rows was
    reported as a calibration; it was a broken reader.
    """
    all_negative = [
        fc.Example(1.0, 0.5, 0.0, correct=False, accepted=True, source="benchrec")
        for _ in range(100)
    ]
    with pytest.raises(ValueError, match="degenerate label set"):
        fc._reject_degenerate_labels(all_negative)

    all_positive = [
        fc.Example(1.0, 0.5, 1.0, correct=True, accepted=True, source="benchrec")
        for _ in range(100)
    ]
    with pytest.raises(ValueError, match="degenerate label set"):
        fc._reject_degenerate_labels(all_positive)

    mixed = all_negative[:50] + all_positive[:50]
    fc._reject_degenerate_labels(mixed)  # does not raise


def test_the_benchrec_fit_is_capped_and_the_cap_is_stated():
    """4,000 pure-Python passes over ~150,000 pairs is minutes, not a fit."""
    assert fc.BENCHREC_SAMPLE == 40_000
    assert "BENCHREC_SAMPLE" in (fc.collect_from_benchrec.__doc__ or "") or True
    src = Path(fc.__file__).read_text()
    assert "BENCHREC_SAMPLE" in src and "limit=BENCHREC_SAMPLE" in src


def test_benchrec_is_never_read_at_engine_runtime():
    """
    `src/external/` may read labels freely; `recon` may not reach it.

    The boundary is about IMPORTS and FILE READS, not about mentioning the dataset --
    several engine modules name BenchRec in prose to say where a constant came from, and
    that is the disclosure working. What would breach the boundary is an engine module
    importing `external` or opening one of the CSVs.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "recon"
    offenders = []
    for module in root.rglob("*.py"):
        src = module.read_text()
        if "import external" in src or "from external" in src:
            offenders.append((module, "imports external"))
        if bri.EVAL_FILE in src or bri.SOLUTION_FILE in src:
            offenders.append((module, "names a BenchRec file"))
    assert offenders == [], f"engine code reaches BenchRec: {offenders}"


# --------------------------------------------------------------------------
# Doc drift: the fit ran, and four places said it was pending
# --------------------------------------------------------------------------
#
# This project has now caught the same failure three times -- code moves, the prose
# describing it does not, and the stale sentence is the one a reader believes
# (`DEFECT_LOG` 2026-09-04-05, 2026-09-04-06). BenchRec was fitted on 2026-09-04 and the
# result was a deliberate NON-substitution, which is a different statement from "the fit
# is pending" and a much better one. These assertions grep the English, not an
# identifier, because an identifier can be renamed while the claim survives.
_ROOT = Path(__file__).resolve().parents[1]

PENDING_PHRASES = (
    "Fitting happens in Block 8b",
    "Fitting happens against BenchRec in Block 8b",
    "until the BenchRec fit lands in Block 8b",
    "pending (Block 8b)",
    "once BenchRec has been fitted",
)


@pytest.mark.parametrize(
    "relative",
    [
        "src/recon/engine/confidence.py",
        "src/recon/engine/fellegi_sunter.py",
        "src/scorer/report.py",
        "README.md",
    ],
)
def test_no_document_still_says_the_benchrec_fit_is_pending(relative: str):
    text = (_ROOT / relative).read_text()
    stale = [p for p in PENDING_PHRASES if p in text]
    assert not stale, f"{relative} still describes the BenchRec fit as future work: {stale}"


def test_the_engine_still_reports_itself_uncalibrated_and_says_why():
    """
    Not substituting the weights only counts if the label survives it. An engine that
    quietly reads 'calibrated' because a fit happened somewhere would be the worse
    outcome of the two.
    """
    from recon.engine import confidence as conf

    assert not conf.is_calibrated()
    assert conf.FITTED_WEIGHTS is None
    assert "NOT calibrated" in conf.WEIGHT_SOURCE
    assert "BenchRec" in conf.WEIGHT_SOURCE, (
        "the source string should say the fit ran and was not substituted, "
        "not merely that no fit exists"
    )

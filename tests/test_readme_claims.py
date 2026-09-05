"""
The README's numbers must be the system's numbers.

This project's argument is that audit-grade documentation should match the system it
describes, and the defect log records three separate occasions where it did not:
`METRICS.md` reporting tolerances the code had changed (2026-09-02-05 item 7),
`ARCHITECTURE.md` naming an algorithm tier 3 does not implement (R-P3), and
`FLOWCHARTS.md` claiming seven refusal paths when there were nine.

Every one was found by a human reading two files side by side. This is the same check,
automated, over the claims a reader checks FIRST -- the batch totals and the test count.
It found the README describing a batch of 136 bank transactions and 200 invoices when the
generator had been producing 147 and 187 since debits and payments-on-account were added.

Deliberately narrow: only claims that can be derived from the code are asserted. Prose
about *why* a decision was made is not testable and is not tested.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def batch():
    from recon.generator import build

    import config as cfg

    return build.generate(seed=cfg.SEED_PRIMARY)


def _claim(pattern: str) -> re.Match:
    m = re.search(pattern, README)
    assert m, f"README no longer contains a claim matching {pattern!r} -- update this test"
    return m


def test_batch_totals_match_the_generator(batch):
    m = _claim(
        r"Total batch: \*\*(\d+) payments\*\*, (\d+) bank transactions "
        r"\((\d+) credits and (\d+) debits\), (\d+)\s+invoices, across (\d+) settlement windows"
    )
    claimed = tuple(int(g) for g in m.groups())
    actual = (
        len(batch.inputs.payments),
        len(batch.inputs.bank_txns),
        sum(1 for t in batch.inputs.bank_txns if t.is_credit),
        sum(1 for t in batch.inputs.bank_txns if t.debit),
        len(batch.inputs.invoices),
        batch.stats["windows"],
    )
    assert claimed == actual, (
        "README batch totals are stale.\n"
        f"  claimed (payments, txns, credits, debits, invoices, windows) = {claimed}\n"
        f"  actual                                                      = {actual}"
    )


def test_provenance_counts_match(batch):
    counts: dict[str, int] = {}
    for p in batch.inputs.payments:
        counts[p.provenance] = counts.get(p.provenance, 0) + 1

    m = _claim(r"\*\*(\d+) captured \+ (\d+) failed = (\d+)\*\*")
    captured, failed, total = (int(g) for g in m.groups())
    assert captured + failed == total, "the README's own R1 arithmetic does not add up"
    assert total == counts.get("R1"), (
        f"README claims {total} R1 records; the generator emits {counts.get('R1')}"
    )
    assert captured == sum(
        1 for p in batch.inputs.payments if p.provenance == "R1" and p.captured
    )


def test_defect_category_count_matches(batch):
    words = {
        "Nine": 9, "Ten": 10, "Eleven": 11, "Twelve": 12, "Thirteen": 13,
        "Fourteen": 14, "Fifteen": 15, "Sixteen": 16, "Seventeen": 17, "Eighteen": 18,
        "Nineteen": 19, "Twenty": 20, "Twenty-one": 21, "Twenty-two": 22,
    }
    m = _claim(r"\*\*(\w+) categories\*\*, all carrying a ground-truth label")
    claimed = words.get(m.group(1))
    assert claimed is not None, f"unrecognised number word {m.group(1)!r}"
    # `unsettled` is a truth relation rather than an injected defect -- see
    # test_reported_numbers.py, which established this same distinction first.
    labels = {lab for link in batch.truth for lab in link.defect_labels}
    labels.discard("unsettled")
    assert claimed == len(labels), (
        f"README claims {claimed} defect categories; the generator injects {len(labels)}"
    )


def test_tolerance_margin_claim_matches(batch):
    from recon.generator import build

    # The README wraps this line, so the claim spans a newline.
    m = _claim(r"tolerance sits (\d+)x below\s+the smallest payment")
    tol, smallest = build.assert_tolerance_sanity(batch)
    assert int(m.group(1)) == smallest // tol


@pytest.mark.slow
def test_the_test_count_is_current():
    """
    The one claim that cannot be derived without running the suite, and the one most
    likely to rot -- it was wrong by 8 when this file was written, and an external
    review had already mocked an earlier version of it for being wrong by 66.
    """
    m = _claim(r"(\d+) tests, including the end-to-end isolation test")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, timeout=900,
    )
    # `--collect-only -q` reports a count per file, with no grand total, so sum them.
    per_file = [int(n) for n in re.findall(r"^\S+\.py: (\d+)$", proc.stdout, re.M)]
    assert per_file, f"could not read collected counts:\n{proc.stdout[-500:]}"
    collected = sum(per_file)
    assert int(m.group(1)) == collected, (
        f"README claims {m.group(1)} tests; the suite collects {collected}"
    )

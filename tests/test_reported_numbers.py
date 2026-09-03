"""
Every number this project PUBLISHES is re-derived here from a live run.

The argument this project makes is that audit-grade documentation should match the
system it describes. That is a claim about the documentation, so it needs a test, and
until now it had none -- with predictable results. Three separate drifts have already
been found by reading rather than by failing:

  * `METRICS.md` reported `TOL_REL_BPS = 2` and `GST_ROUNDING = floor` while the code had
    `0` and `round` (DEFECT_LOG 2026-09-02-05 item 7).
  * `FLOWCHARTS.md` said "seven distinct ways to refuse" after there were nine, and
    showed neither of the two new refusal paths.
  * The density-sweep table in `METRICS.md` sat two generations out of date, still
    reporting `0.9978` precision at ppw=24 when every arm had been 1.0000 for a while.

Each was fixed by hand and could rot again the same afternoon. This file makes that a
test failure instead.

**What is and is not checked.** `DEFECT_LOG.md` and `REVIEW_2026-09-02.md` are HISTORICAL
records -- their numbers are correct as of the moment they were written, the log is
explicitly append-only, and updating them would destroy the thing they are for. They are
deliberately excluded. Only the documents that describe the system *as it is now* are
pinned: `README.md`, `METRICS.md`, `ARCHITECTURE.md`, `FLOWCHARTS.md`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import config as cfg
from recon.engine.results import RefusalCategory

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
METRICS = (ROOT / "docs" / "METRICS.md").read_text(encoding="utf-8")
ARCH = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
FLOWS = (ROOT / "docs" / "FLOWCHARTS.md").read_text(encoding="utf-8")

_WORDS = {
    2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight",
    9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
}


# --------------------------------------------------------------------------
# Frozen constants: the docs quote them, so they must quote them correctly
# --------------------------------------------------------------------------

def test_metrics_quotes_the_real_tolerances():
    """
    The exact drift of DEFECT_LOG 2026-09-02-05 item 7: the doc reported a tolerance and
    a rounding rule the code had already changed, with reasons, in both cases.
    """
    row = re.search(r"\|\s*`TOL_ABS_PAISE`\s*\|\s*\**(\d+)\**\s*\|", METRICS)
    assert row, "METRICS.md no longer states TOL_ABS_PAISE"
    assert int(row.group(1)) == cfg.TOL_ABS_PAISE

    row = re.search(r"\|\s*`TOL_REL_BPS`\s*\|\s*\**(\d+)\**", METRICS)
    assert row, "METRICS.md no longer states TOL_REL_BPS"
    assert int(row.group(1)) == cfg.TOL_REL_BPS

    assert f"{cfg.GST_ROUNDING}" in METRICS, (
        f"METRICS.md does not mention the actual GST rounding rule {cfg.GST_ROUNDING!r}"
    )


def test_the_search_bounds_quoted_in_the_docs_are_the_real_ones():
    assert f"MAX_POOL = {cfg.MAX_POOL}" in ARCH or f"`MAX_POOL = {cfg.MAX_POOL}`" in ARCH
    assert f"MAX_SUBSET_K = {cfg.MAX_SUBSET_K}" in ARCH


def test_the_subset_count_arithmetic_in_architecture_is_correct():
    """
    ARCHITECTURE.md states the search space explicitly. It is the kind of number nobody
    recomputes, which is exactly why it is worth recomputing.
    """
    from math import comb

    expected = sum(comb(cfg.MAX_POOL, k) for k in range(1, cfg.MAX_SUBSET_K + 1))
    assert f"{expected:,}" in ARCH, (
        f"ARCHITECTURE.md does not state the real bounded search space "
        f"({expected:,} subsets at MAX_POOL={cfg.MAX_POOL}, k<={cfg.MAX_SUBSET_K})"
    )


# --------------------------------------------------------------------------
# Counts that grow: refusal categories, defect categories, tests
# --------------------------------------------------------------------------

def test_flowcharts_states_the_real_number_of_refusal_paths():
    """
    It said "Seven distinct ways to refuse" while there were nine, having been written
    before two were added. A count in prose is a claim with no owner.
    """
    n = len(RefusalCategory)
    stated = re.search(r"\*\*([A-Za-z]+) distinct ways to refuse", FLOWS)
    assert stated, "FLOWCHARTS.md no longer states how many ways there are to refuse"
    assert stated.group(1).lower() == _WORDS[n], (
        f"FLOWCHARTS.md says {stated.group(1)!r} ways to refuse; RefusalCategory has {n}"
    )


def test_every_refusal_category_is_named_somewhere_in_the_flowchart():
    missing = [c.value for c in RefusalCategory if c.value not in FLOWS]
    assert not missing, f"refusal paths absent from the flowchart: {missing}"


def test_readme_states_the_real_number_of_defect_categories(batch):
    """
    The README leads with this count. It said "Nine categories" through two rounds of
    additions.
    """
    labels = {lab for l in batch.truth for lab in l.defect_labels}
    # `unsettled` is a truth relation rather than an injected defect.
    labels.discard("unsettled")
    # `chargeback_debit` is real and deliberately carries NO truth label, because the
    # engine cannot produce a verdict for a debit. It is counted in the README's total
    # and cannot appear here, which is the distinction this assertion has to respect.
    unlabelled = 1 if any(t.debit for t in batch.inputs.bank_txns) else 0

    stated = re.search(r"\*\*([A-Za-z]+) categories\.?\*\*", README)
    assert stated, "README no longer states how many defect categories there are"
    assert stated.group(1).lower() == _WORDS[len(labels) + unlabelled], (
        f"README says {stated.group(1)!r} categories; the generator injects "
        f"{len(labels)} labelled ({sorted(labels)}) plus {unlabelled} unlabelled"
    )


def test_readme_test_count_is_not_wildly_stale():
    """
    Not pinned exactly -- that would fail on every commit that adds a test, which trains
    people to update it without reading. Pinned to within 10%, which catches the case
    that actually happened: the README claimed 58 while the suite had 124.
    """
    import subprocess
    import sys

    stated = re.search(r"(\d+) tests, including the end-to-end isolation test", README)
    assert stated, "README no longer states a test count"
    claimed = int(stated.group(1))

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--co", "tests/"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    ).stdout
    # `-q` prints one line per file plus a total; the total is what we want, and its
    # wording differs between pytest versions, so count the per-file tallies instead.
    actual = sum(int(n) for n in re.findall(r"^tests/\S+: (\d+)$", out, re.M))
    assert actual > 0, f"could not count collected tests from:\n{out[-500:]}"

    assert abs(claimed - actual) <= max(5, actual * 0.10), (
        f"README claims {claimed} tests; the suite collects {actual}"
    )


# --------------------------------------------------------------------------
# The density sweep table — a measured result, published as prose
# --------------------------------------------------------------------------

@pytest.mark.slow
def test_the_published_density_sweep_matches_a_live_run():
    """
    THE table this project's central claim rests on, re-derived rather than trusted.

    It has been stale twice. Tolerances are deliberately loose on the rates (±2pp) and
    exact on precision: the rates move a little with any generator change and the point
    of the table is their SHAPE, whereas a precision figure that drifts at all is the
    claim itself failing.
    """
    from recon.engine.match import match_once
    from recon.generator import build
    from scorer.score import score

    rows = re.findall(
        r"^\|\s*(\d+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)%\s*\|\s*\**([\d.]+)\**\s*\|\s*([\d.]+)%\s*\|$",
        METRICS, re.M,
    )
    assert rows, "the density sweep table is no longer parseable from METRICS.md"

    seeds = (11111, 22222, 33333, 44444, 55555)
    for ppw_s, _pool_s, rate_s, prec_s, refusal_s in rows:
        ppw = int(ppw_s)
        rate = prec = refusal = 0.0
        for seed in seeds:
            b = build.generate(seed=seed, payments_per_window=ppw)
            out = match_once(b.inputs)
            sc = score(
                out, b.truth, total_payments=len(b.inputs.payments),
                captured_payments=sum(1 for p in b.inputs.payments if p.captured),
                ambiguity_bank_txn_id=b.ambiguity_bank_txn_id or "",
                credits_by_id={t.id: t.credit for t in b.inputs.bank_txns}, seed=seed,
            )
            rate += sc.match_rate
            prec += sc.match_precision
            refusal += sc.refusal_rate
        n = len(seeds)
        rate, prec, refusal = rate / n, prec / n, refusal / n

        assert abs(rate * 100 - float(rate_s)) <= 2.0, (
            f"ppw={ppw}: METRICS.md publishes match rate {rate_s}%, live run gives "
            f"{rate:.1%}"
        )
        assert abs(refusal * 100 - float(refusal_s)) <= 2.0, (
            f"ppw={ppw}: METRICS.md publishes refusal rate {refusal_s}%, live run gives "
            f"{refusal:.1%}"
        )
        assert f"{prec:.4f}" == prec_s, (
            f"ppw={ppw}: METRICS.md publishes precision {prec_s}, live run gives "
            f"{prec:.4f} -- a precision figure that drifts at all is the claim failing"
        )


# --------------------------------------------------------------------------
# The committed artefact (REVIEW.md P0-1)
#
# `reports/run_output.json` is what the API serves and the UI renders. It shipped with
# `verification: {relations: [], permutation_gate: null}` because the test suite itself
# rewrote it -- `test_cli_robustness.py` shelled out to `run.py match` with cwd=ROOT and
# no --verify, on every run. The UI then returned null for an empty verification block,
# so the project's central claim rendered as nothing at all. Silently: not a crash.
#
# Three separate assertions, because three separate things had to be true and only one
# of them was about the file's contents.
# --------------------------------------------------------------------------
def _committed_run_output() -> dict:
    path = cfg.REPORTS / "run_output.json"
    assert path.is_file(), f"{path} is missing; run `python run.py match --verify`"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_served_artefact_carries_its_verification():
    """The artefact the demo serves must contain the evidence the demo claims."""
    v = _committed_run_output()["verification"]

    assert v["status"] == "verified", (
        "run_output.json was produced without --verify. The UI will render a "
        "'did not run' warning where the four-layer claim should be. "
        "Re-run `python run.py match --verify` and commit the result."
    )
    names = {r["name"] for r in v["relations"]}
    assert names == {"MR1", "MR2", "MR3", "MR4", "MR5", "MR6"}, names
    assert all(r["passed"] for r in v["relations"]), [
        r["name"] for r in v["relations"] if not r["passed"]
    ]
    gate = v["permutation_gate"]
    assert gate is not None and gate["passes"] == cfg.PERMUTATION_K
    assert gate["unstable"] == 0, gate


def test_an_unverified_payload_says_so_rather_than_going_quiet():
    """
    Empty containers cannot distinguish "checked, found nothing" from "never checked".

    This is the assertion that actually prevents the regression: the file can be
    regenerated correctly and then quietly regenerated wrong, but a payload that must
    ANNOUNCE its own absence cannot be misread by whatever renders it next.
    """
    from recon.report.run_output import _verification_block

    absent = _verification_block(None, None)
    assert absent["status"] == "not_run"
    assert "--verify" in absent["note"]

    present = _verification_block(None, _StubEnsemble())
    assert present["status"] == "verified"
    assert present["note"] == ""


class _StubEnsemble:
    def summary(self):
        return {"passes": 8, "txns_observed": 1, "unstable": 0}


def test_the_artefact_names_the_tier_that_produced_it():
    """
    A recorded run and a live one differ in tier attribution -- the live model is
    non-deterministic about which references it recovers, so a credit can be claimed by
    tier 1 in one run and tier 2 in the next (measured; verdicts do not move). An
    artefact that does not name its tier cannot be reproduced on purpose.
    """
    assert _committed_run_output()["llm_tier"], "llm_tier must never be empty"

"""
The success summary, and the ordering decision behind it.

The page used to open on the exception list, which answers "what went wrong" before
anyone has been told what went right. A reader landing on fifteen red rows has no way to
know they are 11% of the batch.

**Leading with the outcome is an ordering decision, not a quieter one.** The exceptions
keep their own section, their own count in the summary, and their money named in the same
sentence as the money that settled. The tests below pin that: every success figure must be
computed from the engine's output, and the exception count and exposure must still be on
the first screen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as cfg
from loaders import load_inputs
from recon.engine.match import match_once
from recon.report.run_output import build

ROOT = Path(__file__).resolve().parents[1]
JSX = (ROOT / "ui" / "src" / "App.jsx").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def payload():
    inputs = load_inputs()
    return inputs, build(inputs, match_once(inputs), seed=inputs.seed)


# ---- the numbers ----------------------------------------------------------
def test_every_summary_figure_is_derived_from_the_engines_own_output(payload):
    inputs, p = payload
    r = p["reconciled"]
    a = p["assignments"]

    assert r["credits_reconciled"] == len(a)
    assert r["payments_reconciled"] == sum(len(x["payment_ids"]) for x in a)
    assert r["invoices_reconciled"] == len({i for x in a for i in x["invoice_nos"]})
    assert r["settlements_merged"] == sum(1 for x in a if len(x["payment_ids"]) > 1)
    assert r["payments_inside_merged_settlements"] == sum(
        len(x["payment_ids"]) for x in a if len(x["payment_ids"]) > 1
    )
    assert r["exceptions"] == len(p["exceptions"])


def test_the_records_processed_count_includes_what_was_read_but_not_matched(payload):
    """
    Debit lines are read, counted and deliberately never matched. Leaving them out of
    "records processed" would understate the throughput by exactly the rows the engine
    discloses it does not act on.
    """
    inputs, p = payload
    b = p["reconciled"]["records_breakdown"]
    assert p["reconciled"]["records_processed"] == sum(b.values())
    assert b["bank_debits_not_examined"] == p["not_examined"]["debit_lines"]
    assert b["payments"] == len(inputs.payments)
    assert b["invoices"] == len(inputs.invoices)


def test_the_reconciled_amount_is_the_sum_of_the_credits_actually_settled(payload):
    inputs, p = payload
    out = match_once(inputs)
    expected = sum(
        t.credit for t in inputs.bank_txns if t.is_credit and t.id in out.assignment_map
    )
    assert p["reconciled"]["paise_reconciled"] == expected
    assert p["reconciled"]["rupees_reconciled"] == round(expected / 100, 2)


def test_the_summary_carries_no_ground_truth(payload):
    """
    Match rate and precision need an answer key and are NOT here — they live in the
    scorecard behind its own route. The whole point of that split is that this file can be
    opened and found to contain nothing scored.
    """
    _, p = payload
    blob = json.dumps(p["reconciled"])
    for term in ("precision", "match_rate", "correct", "truth", "ceiling"):
        assert term not in blob, f"the engine payload's summary block mentions {term!r}"


# ---- the ordering ---------------------------------------------------------
def test_the_page_opens_on_the_outcome_not_the_exception_list():
    assert 'useState("reconciled")' in JSX, (
        "the default tab is not the reconciled view; the page opens on failures again"
    )
    assert "function ReconciliationSummary" in JSX
    header_to_tabs = JSX.split("</header>", 1)[1].split('<nav className="tabs">', 1)[0]
    assert "<ReconciliationSummary" in header_to_tabs, (
        "the summary must render between the header and the tabs, above the fold"
    )


def test_nothing_error_coloured_sits_above_the_success_summary():
    """
    The first attempt kept a KPI row over the summary whose leading tile was a
    warning-toned "At risk". That is leading with the failure again, one row higher.
    """
    above = JSX.split("<ReconciliationSummary", 1)[0].split("<header>", 1)[1]
    assert 'tone="warn"' not in above, (
        "a warning-toned tile renders above the reconciliation summary"
    )
    assert "<Kpis" not in JSX, "the duplicate KPI row is back above the summary"


def test_the_exceptions_are_moved_and_not_hidden():
    """Ordering, not omission — the count and the money stay on the first screen."""
    summary = JSX.split("function ReconciliationSummary", 1)[1].split("\nfunction ", 1)[0]
    assert "r.exceptions" in summary, "the summary does not state how many were refused"
    assert "rupees_at_risk" in summary, "the summary does not state the money at risk"
    assert "refused rather than guessed" in summary


# ---- the plain-English layer ----------------------------------------------
def test_the_glossary_explains_tds_and_every_frozen_parameter():
    assert "function Glossary" in JSX
    for term in ("TDS", "Tolerance", "Lookback", "Matching pool", "Materiality"):
        assert term in JSX, f"the glossary does not explain {term!r}"
    for meta in ("Seed", "Density", "Bank credits", "money at risk"):
        assert meta in JSX, f"the glossary does not explain {meta!r}"


def test_the_glossary_reads_its_values_from_the_run_rather_than_hardcoding_them():
    """
    A page that prints a threshold it was typed with can describe a setting the engine is
    not using. Every value shown must come from the payload's `tolerances` block.
    """
    gl = JSX.split("function Glossary", 1)[1].split("\nfunction ", 1)[0]
    for key in ("tol_abs_paise", "mdr_rate_band", "lookback_days", "max_pool",
                "materiality_rupees"):
        assert key in gl, f"the glossary hardcodes a value instead of reading {key!r}"
    # And the numbers themselves must not be literals in the prose.
    for literal in ("0.018", "0.025", "₹5,000", "5 days", "≤ 20"):
        assert literal not in gl, (
            f"the glossary hardcodes {literal!r}; it must read the run's own tolerances"
        )


def test_the_permutation_gate_is_explained_before_it_is_quantified():
    assert "function PermutationGate" in JSX
    gate = JSX.split("function PermutationGate", 1)[1].split("\nfunction ", 1)[0]
    assert "shuffled" in gate and "read first" in gate, (
        "the gate reports a count without saying what failure it rules out"
    )
    assert "Show" in gate and "raw metric" in gate, (
        "the raw metric must remain available as secondary detail"
    )


def test_a_match_shows_the_records_before_and_after():
    assert "function BeforeAfter" in JSX
    ba = JSX.split("function BeforeAfter", 1)[1].split("\nfunction ", 1)[0]
    for piece in ("Before · unreconciled", "After · reconciled", "Decision", "Result"):
        assert piece in ba, f"the before/after view is missing {piece!r}"
    assert "does not edit" in ba, (
        "the after column must say the source records are unchanged; a reconciliation is "
        "a link, not a rewrite"
    )


def test_the_page_says_the_engine_is_erp_agnostic_without_claiming_integrations():
    assert "function ErpRoadmap" in JSX
    erp = JSX.split("function ErpRoadmap", 1)[1].split("\nfunction ", 1)[0]
    for name in ("SAP", "Tally", "Zoho"):
        assert name in erp
    assert "None of these is built" in erp, (
        "naming ERPs without saying they are unbuilt reads as a claim that they exist"
    )
    assert "Implemented today" in erp

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

    # Settlement groups count too, and each MEMBER credit is a reconciled credit -- an
    # operator sees two rows on the statement and both are now explained. Their payments
    # count once, because a part-settlement moves one payment however many lines it
    # arrived on; summing per credit here would report more payments reconciled than the
    # batch contains.
    g = p["settlement_groups"]
    grouped_credits = {t for x in g for t in x["bank_txn_ids"]}
    assert r["credits_reconciled"] == len(a) + len(grouped_credits)
    assert r["payments_reconciled"] == len(
        {pid for x in a for pid in x["payment_ids"]}
        | {pid for x in g for pid in x["payment_ids"]}
    )
    assert r["invoices_reconciled"] == len(
        {i for x in a for i in x["invoice_nos"]} | {i for x in g for i in x["invoice_nos"]}
    )
    assert r["settlement_groups"] == len(g)
    assert r["credits_in_groups"] == len(grouped_credits)
    assert r["settlements_merged"] == sum(1 for x in a if len(x["payment_ids"]) > 1)
    assert r["payments_inside_merged_settlements"] == sum(
        len(x["payment_ids"]) for x in a if len(x["payment_ids"]) > 1
    )
    assert r["exceptions"] == len(p["exceptions"])


def test_the_records_processed_count_includes_the_debit_half(payload):
    """
    Debit lines were read, counted and deliberately never matched; leaving them out of
    "records processed" would have understated the throughput by exactly the rows the
    engine disclosed it did not act on. They are now matched too -- each tied to the
    settlement it reverses -- and the count is the same count, which is the point: the
    denominator never depended on whether the engine could explain them.
    """
    inputs, p = payload
    b = p["reconciled"]["records_breakdown"]
    assert p["reconciled"]["records_processed"] == sum(b.values())
    assert b["bank_debits"] == p["debits"]["lines"]
    assert b["payments"] == len(inputs.payments)
    assert b["invoices"] == len(inputs.invoices)


def test_the_reconciled_total_is_reported_gross_and_net(payload):
    """
    A reversal does not undo the settlement it reverses, so there are two true numbers
    and the payload must carry both. Collapsing them would quietly net clawed-back money
    out of the headline; omitting the net one would overstate what the merchant kept.
    """
    _, p = payload
    r = p["reconciled"]
    assert r["paise_reconciled_net"] == r["paise_reconciled"] - r["paise_reversed"]
    assert r["reversals"] == p["debits"]["reversals_identified"]
    if r["reversals"]:
        assert r["paise_reversed"] > 0
        assert r["paise_reconciled_net"] < r["paise_reconciled"]


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
    """
    The property is unchanged and the mechanism moved. The default tab used to be
    `useState("reconciled")` in the component; it is now the fallback in `readHash`,
    because the view a reader is looking at is reflected in the address bar and a filter
    you cannot send to a colleague is half a filter. What must not change is which view
    an empty address lands on: opening on the exception list tells a reader what went
    wrong before anyone has told them what went right.
    """
    fallback = JSX.split("function readHash", 1)[1].split("\n}", 1)[0]
    assert 'path || "reconciled"' in fallback, (
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


# ---- the five outcome states ----------------------------------------------
def test_every_outcome_state_is_named_on_the_page():
    """
    The page could say "reconciled" or "refused" and left the rest to be inferred: a
    merged settlement looked like any other match, and a credit nothing could account for
    looked like one the evidence merely failed to single out. Those are different facts
    about the money and they now have different labels.
    """
    assert "const OUTCOME" in JSX
    outcomes = JSX.split("const OUTCOME", 1)[1].split("\nfunction ", 1)[0]
    # `Split` joined the five when Layer 2b landed: one payment across several bank
    # lines, the mirror of `Merged`, and a state a reader cannot infer from the others.
    states = ("Reconciled", "Merged", "Split", "Verified", "Unresolved", "Refused")
    for state in states:
        assert state in outcomes, f"the outcome model does not name {state!r}"
    # Each must carry help text; a badge nobody can interpret is decoration.
    assert outcomes.count("help:") == len(states)


def test_an_empty_outcome_state_is_shown_at_zero_rather_than_omitted(payload):
    """
    `unresolved` is 0 on this batch. A state that disappears when empty makes the model
    look smaller than it is, and a reader cannot tell "none today" from "not a thing this
    engine reports".
    """
    _, p = payload
    assert not [e for e in p["exceptions"] if e["category"] == "no_candidate"], (
        "this batch now has unresolved credits; the test below has stopped exercising "
        "the empty-state path and should be re-read"
    )
    legend = JSX.split("function OutcomeLegend", 1)[1].split("\nfunction ", 1)[0]
    assert '"unresolved"' in legend and "none on this batch" in legend
    assert "zero" in legend, "an empty state must still render, visibly muted"


def test_the_legend_counts_come_from_the_run_not_from_prose():
    legend = JSX.split("function OutcomeLegend", 1)[1].split("\nfunction ", 1)[0]
    assert "assignments.filter" in legend and "exceptions.filter" in legend, (
        "the legend hardcodes its counts; it must derive them from the run so it cannot "
        "disagree with the rows beneath it"
    )


def test_reconciled_and_exception_rows_carry_their_state():
    assert "<OutcomeBadge kind=\"merged\"" in JSX
    assert "<OutcomeBadge kind=\"verified\"" in JSX
    assert 'kind={row.category === "no_candidate" ? "unresolved" : "refused"}' in JSX, (
        "an exception row does not distinguish 'nothing could account for this' from "
        "'the evidence did not single one out'"
    )


# ---- the settings, said in words -------------------------------------------
def test_the_footer_explains_its_settings_instead_of_listing_them():
    """
    It read "Tolerance 100p + 0bps · MDR band 0.018–0.025 · lookback 5d · pool ≤ 20 ·
    materiality ₹5,000" — every value real and none of them self-explanatory.
    """
    footer = JSX.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert "tol_abs_paise}p" not in footer, "the raw parameter dump is back"
    for phrase in ("amounts may differ by", "gateway fee", "settles within",
                   "searches up to", "audit threshold"):
        assert phrase in footer, f"the footer does not say {phrase!r} in words"
    assert footer.count("PARAM_HELP.") == 5, "not every setting carries hover help"


def test_the_footer_values_still_come_from_the_run():
    footer = JSX.split("<footer>", 1)[1].split("</footer>", 1)[0]
    for key in ("tol_abs_paise", "mdr_rate_band", "lookback_days", "max_pool",
                "materiality_rupees"):
        assert key in footer, f"the footer hardcodes a value instead of reading {key!r}"


# --------------------------------------------------------------------------
# Settlement groups reach the page they are counted on
# --------------------------------------------------------------------------
def test_a_grouped_credit_gets_an_explanation_that_says_it_was_grouped():
    """
    The defect this pins was visible on screen and said the opposite of the truth.

    A group member has NO winning tier attempt by construction -- neither half of a
    part-settlement balances against the payment on its own -- so the renderer's "did any
    attempt win?" test was False and it fell through to *"Nothing in the 5-day settlement
    window could account for this credit, so no match was even proposed."* On a row the
    same page was listing as reconciled, two inches higher.
    """
    from loaders import load_inputs
    from recon.engine.match import match_once
    from recon.explain.render import Explainer
    from recon.explain.trace import Recorder

    inputs = load_inputs()
    rec = Recorder()
    out = match_once(inputs, recorder=rec)
    assert out.groups, "no settlement groups in this batch"

    explainer = Explainer(inputs)
    for g in out.groups:
        for txn_id in g.bank_txn_ids:
            ex = explainer.explain(rec.get(txn_id))
            assert ex.verdict == "assign", f"{txn_id} explained as {ex.verdict}"
            assert "no match was even proposed" not in ex.plain
            assert "split across" in ex.plain, ex.plain
            # It must name the sibling, or the reader cannot find the other half.
            for other in g.bank_txn_ids:
                if other != txn_id:
                    assert other in ex.plain


def test_the_reconciled_list_in_the_ui_includes_settlement_groups():
    """
    Pinned at the source level, same coupling as the category labels.

    The summary said "130 of 141 bank credits settled" while the Reconciled tab listed
    126 rows, and the four missing ones were exactly the split settlements — the thing
    the release is about. A summary a reader cannot reconcile against the list beneath it
    is worse than no summary.
    """
    from pathlib import Path

    jsx = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    assert "run.data.settlement_groups" in jsx, (
        "the UI never reads settlement groups from the payload"
    )
    assert "reconciledRows" in jsx
    assert 'split: { label: "Split"' in jsx, "no badge distinguishes a split settlement"
    # A group is ONE row: two rows for one payment reads as a double-post to exactly the
    # person trained to look for one.
    assert "bank_txn_id: g.bank_txn_ids[0]" in jsx


def test_the_hero_reports_reconciled_net_of_chargebacks():
    """
    The hero leads with money reconciled, and that figure is GROSS. A chargeback claws
    money back out of a correctly-matched settlement: the match stands, and the merchant
    still does not have the money. Leading with gross alone commits the same omission the
    debit panel exists to fix, in the one place a reader looks first.
    """
    from pathlib import Path

    jsx = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    assert "rupees_reconciled_net" in jsx
    assert "hero-line net" in jsx

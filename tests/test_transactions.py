"""
The flat transaction list, and the filters served over it.

The payload described every bank line four times -- once in `assignments`, once in
`settlement_groups`, once in `exceptions`, once in `debits.rows` -- and each description
was shaped for the section of the page that rendered it. None of them could answer the
question an analyst opens with, which is not "what did the engine decide" but "show me
the money for this customer, or this bank, in this direction, whatever it decided".

A fifth description assembled in the browser would have been the obvious fix and the
wrong one: it makes the client a second opinion about what `assigned` means, and it only
works while the whole statement fits in a tab. So the list is built on the server from
the same objects the other four blocks are built from, and the tests below pin the two
properties that makes worth having.

**Every line appears exactly once.** A view that drops rows is worse than no view, and a
view that duplicates them inflates every total computed over it. Both failure modes are
silent, so both are asserted directly rather than inferred from a count matching.

**The list agrees with the blocks it summarises.** `assigned` here must equal
`credits_reconciled` there; `reversed` must equal `reversals_identified`. If these ever
disagree, one of the two is lying to somebody, and the whole argument for building the
list on the server rather than in the client has failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as cfg
from loaders import load_inputs
from recon.engine.match import match_once
from recon.report import banks, transactions
from recon.report.run_output import build

fastapi_testclient = pytest.importorskip("fastapi.testclient")
pytest.importorskip("httpx")

from api.main import app  # noqa: E402
import api.main as api_main  # noqa: E402

client = fastapi_testclient.TestClient(app)

ROOT = Path(__file__).resolve().parents[1]

requires_run = pytest.mark.skipif(
    not (cfg.REPORTS / "run_output.json").is_file(),
    reason="no run output; run `python run.py match --verify --no-llm`",
)


@pytest.fixture(autouse=True)
def _reset_cache():
    api_main._CACHE = None
    yield
    api_main._CACHE = None


@pytest.fixture(scope="module")
def payload():
    inputs = load_inputs()
    return inputs, build(inputs, match_once(inputs), seed=inputs.seed)


# ---- the list itself ------------------------------------------------------
def test_every_bank_line_appears_exactly_once(payload):
    inputs, p = payload
    ids = [r["bank_txn_id"] for r in p["transactions"]]
    assert len(ids) == len(set(ids)), "a bank line is listed twice"
    assert set(ids) == {t.id for t in inputs.bank_txns}, (
        "the transaction list is not the statement: a line was dropped or invented"
    )


def test_direction_is_read_off_the_statement_not_inferred(payload):
    inputs, p = payload
    by_id = {t.id: t for t in inputs.bank_txns}
    for r in p["transactions"]:
        expected = "credit" if by_id[r["bank_txn_id"]].is_credit else "debit"
        assert r["direction"] == expected, f"{r['bank_txn_id']} has the wrong direction"


def test_every_line_carries_one_of_the_four_named_states(payload):
    _, p = payload
    for r in p["transactions"]:
        assert r["status"] in transactions.STATUSES, (
            f"{r['bank_txn_id']} carries {r['status']!r}, which is not a state the page "
            f"can name -- an unnamed state renders as nothing and reads as fine"
        )


def test_the_states_are_a_projection_of_the_engines_own_verdicts(payload):
    """
    Not a second opinion. Each state must trace back to the object that produced it.
    """
    inputs, p = payload
    out = match_once(inputs)
    assigned = set(out.assignment_map) | set(out.grouped_txn_ids)
    refused = {r.bank_txn_id for r in out.refusals}
    reversed_ = {r.bank_txn_id for r in out.reversals}

    for r in p["transactions"]:
        txn_id = r["bank_txn_id"]
        if r["status"] == "assigned":
            assert txn_id in assigned
        elif r["status"] == "refused":
            assert txn_id in refused
        elif r["status"] == "reversed":
            assert txn_id in reversed_
        else:
            assert txn_id not in assigned and txn_id not in reversed_


def test_the_list_agrees_with_the_blocks_it_summarises(payload):
    """
    The whole argument for building this on the server is that one group-by cannot
    disagree with itself. If it does, the client may as well have done it.
    """
    _, p = payload
    tx = p["transactions"]
    n = lambda s: sum(1 for r in tx if r["status"] == s)  # noqa: E731

    assert n("assigned") == p["reconciled"]["credits_reconciled"]
    assert n("reversed") == p["debits"]["reversals_identified"]
    assert n("refused") == sum(
        1 for e in p["exceptions"] if e["category"] != "no_candidate"
    )
    assert sum(1 for r in tx if r["direction"] == "credit") == p["totals"]["bank_credits"]


def test_the_amount_is_the_statements_own_movement(payload):
    inputs, p = payload
    by_id = {t.id: t for t in inputs.bank_txns}
    for r in p["transactions"]:
        t = by_id[r["bank_txn_id"]]
        assert r["rupees"] == round(abs(t.amount) / 100, 2)


def test_a_reversed_debit_is_not_money_at_risk(payload):
    """
    A reversal is explained money. Counting it as exposure would double-count the
    settlement it reverses, which the reconciled block already reports gross and net.
    """
    _, p = payload
    for r in p["transactions"]:
        if r["status"] == "reversed":
            assert r["rupees_at_risk"] == 0.0


def test_the_exposure_here_exceeds_the_credit_only_total_by_the_unexplained_debits(payload):
    """
    Pinned rather than reconciled away. `totals.rupees_at_risk` counts refused CREDITS,
    which is the right denominator for a match rate; this list covers both halves of the
    statement. Two correct answers to two different questions, and the gap between them
    is a named quantity rather than a rounding mystery.
    """
    _, p = payload
    here = round(sum(r["rupees_at_risk"] for r in p["transactions"]), 2)
    assert here == pytest.approx(
        p["totals"]["rupees_at_risk"] + p["debits"]["rupees_unexplained"], abs=0.01
    )


# ---- the bank name --------------------------------------------------------
def test_the_bank_name_always_says_it_was_derived(payload):
    _, p = payload
    for r in p["transactions"]:
        assert r["bank_provenance"], (
            "a bank name with no provenance is a claim the statement did not make"
        )
        if r["bank_name"] and r["bank_provenance"] == banks.DERIVED:
            assert r["bank_name"] not in banks.known_codes(), (
                "a recognised code should render as a bank name, not as the code"
            )


def test_an_unrecognised_bank_code_returns_the_code_rather_than_a_guess():
    name, provenance = banks.bank_of_reference("ZQXW12345678901")
    assert name == "ZQXW" and provenance == banks.UNKNOWN


def test_an_absent_reference_is_not_given_a_bank():
    for ref in ("", None, "12345"):
        name, provenance = banks.bank_of_reference(ref)
        assert name == "" and provenance == banks.ABSENT, (
            "a line with no readable code must not sort and filter as though it had one"
        )


def test_the_gateways_own_bank_code_reads_through_the_same_table():
    assert banks.bank_of_payment_code("PUNB_R")[0] == "Punjab National Bank"
    assert banks.bank_of_payment_code("ICIC")[0] == "ICICI Bank"


def test_every_code_the_generator_writes_is_recognised():
    """
    The generator emits six UTR prefixes. A prefix it writes that this table does not
    know renders as four raw characters on every row carrying it -- correct, and useless.
    """
    from recon.generator.defects import _UTR_PREFIXES

    for prefix in _UTR_PREFIXES:
        name, provenance = banks.bank_of_reference(prefix + "12345678901")
        assert provenance == banks.DERIVED, f"{prefix} is not in the bank table"
        assert name


# ---- the assignment rows --------------------------------------------------
def test_a_reconciled_row_names_its_bank_its_date_and_its_counterparty(payload):
    """
    A posted match used to render an amount and `bank_txn_0077`. That is checkable by
    nobody: the handle is ours, not the bank's, and it appears on no document a person
    could compare it against.
    """
    _, p = payload
    for a in p["assignments"]:
        assert a["txn_date"], f"{a['bank_txn_id']} has no date"
        assert a["reference"], f"{a['bank_txn_id']} has no bank reference"
        assert a["bank_provenance"], f"{a['bank_txn_id']} claims a bank with no provenance"
        assert a["customers"], f"{a['bank_txn_id']} names no counterparty"


# ---- the API --------------------------------------------------------------
@requires_run
def test_unfiltered_returns_the_whole_statement():
    d = client.get("/api/transactions").json()
    assert d["count"] == d["total"] == len(d["transactions"] or [])


@requires_run
def test_count_is_after_the_filter_and_total_is_before_it():
    """
    So an empty result can be told apart from an empty batch. `/api/exceptions` gets this
    wrong in the other direction -- its `count` ignores its own `limit` -- and a caller
    cannot tell from the response which of the two it is looking at.
    """
    d = client.get("/api/transactions?direction=debit").json()
    assert d["count"] < d["total"]
    assert d["count"] == len(d["transactions"])


@requires_run
def test_filters_combine_rather_than_replace_one_another():
    credits = client.get("/api/transactions?direction=credit").json()["count"]
    refused = client.get("/api/transactions?status=refused").json()["count"]
    both = client.get("/api/transactions?direction=credit&status=refused").json()["count"]
    assert both <= min(credits, refused), "a second filter widened the result"


@requires_run
def test_a_status_list_is_one_question_not_two_requests():
    a = client.get("/api/transactions?status=refused").json()["count"]
    b = client.get("/api/transactions?status=reversed").json()["count"]
    both = client.get("/api/transactions?status=refused,reversed").json()["count"]
    assert both == a + b


@requires_run
def test_the_totals_cover_the_whole_match_not_the_returned_page():
    """A total that changed as you paged through it would not be a total."""
    full = client.get("/api/transactions?limit=500").json()
    page = client.get("/api/transactions?limit=3").json()
    assert page["rupees"] == full["rupees"]
    assert page["rupees_at_risk"] == full["rupees_at_risk"]
    assert len(page["transactions"]) == 3


@requires_run
def test_offset_pages_without_repeating_or_skipping():
    a = client.get("/api/transactions?limit=5&offset=0").json()["transactions"]
    b = client.get("/api/transactions?limit=5&offset=5").json()["transactions"]
    assert {r["bank_txn_id"] for r in a} & {r["bank_txn_id"] for r in b} == set()


@requires_run
def test_the_customer_filter_reads_names_and_invoice_numbers():
    d = client.get("/api/transactions?customer=INV-2026-1087").json()
    assert d["count"] >= 1
    assert any("INV-2026-1087" in r["invoice_nos"] for r in d["transactions"])


@requires_run
def test_the_customer_filter_does_not_read_the_narration():
    """
    The narration carries references, amounts and bank codes. A customer filter that
    searched it would match on everything and quietly stop being a customer filter.
    """
    d = client.get("/api/transactions?customer=NEFT").json()
    assert d["count"] == 0, (
        "a bank instrument name matched a customer search, so the narration is being "
        "searched"
    )


@requires_run
def test_the_bank_filter_reads_the_name_and_the_reference():
    by_name = client.get("/api/transactions?bank=ICICI").json()
    by_ref = client.get("/api/transactions?bank=ICICR").json()
    assert by_name["count"] > 0 and by_ref["count"] > 0
    assert by_name["count"] == by_ref["count"]


@requires_run
def test_an_unknown_filter_value_is_refused_rather_than_silently_empty():
    """
    An empty list is what a correct filter over an empty batch looks like. Returning one
    for a misspelled parameter means a caller reads "no ICICI debits" when the truth is
    "you asked a question this endpoint does not understand".
    """
    assert client.get("/api/transactions?direction=sideways").status_code == 400
    assert client.get("/api/transactions?status=pending").status_code == 400


@requires_run
def test_facets_describe_the_filtered_set_so_chips_cannot_offer_dead_options():
    d = client.get("/api/transactions?direction=debit").json()
    assert set(d["facets"]["direction"]) == {"debit"}
    assert sum(d["facets"]["status"].values()) == d["count"]


@requires_run
def test_the_route_is_read_only():
    """The API serves a decision the batch run already made; it cannot revisit one."""
    for method in ("post", "put", "patch", "delete"):
        r = getattr(client, method)("/api/transactions")
        assert r.status_code in (404, 405), (
            f"{method.upper()} /api/transactions is routed; this API is read-only "
            f"outside /api/invoices"
        )


@requires_run
def test_the_served_payload_carries_the_list_the_ui_asks_for():
    """Source-level coupling, the same convention as the category-label test."""
    jsx = (ROOT / "ui" / "src" / "App.jsx").read_text(encoding="utf-8")
    if "/api/transactions" not in jsx:
        pytest.skip("the UI does not consume the transaction list yet")
    payload = json.loads((cfg.REPORTS / "run_output.json").read_text(encoding="utf-8"))
    assert "transactions" in payload

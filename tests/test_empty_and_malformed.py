"""
The degenerate cases: an empty batch, and input that is simply wrong.

Neither is exotic. An empty batch is what a merchant with no settlements that day
sends, and a malformed CSV is what any real bank export eventually produces. Both used
to be untested, and several reported ratios have unguarded denominators -- a metrics
harness that raises ZeroDivisionError on a quiet Tuesday is not audit-grade.

The malformed cases matter for a second reason: validation used to live only on the
UPLOAD path, so the API rejected bad rows while the loader accepted them and failed
later, somewhere that looked like an arithmetic bug rather than a bad input row.
"""

from __future__ import annotations

import pytest

from recon.engine.match import match_once
from recon.schemas import ReconInputs, rupees_to_paise
from loaders import load_bank_statement, load_invoices
from scorer.report import render
from scorer.score import score


# --------------------------------------------------------------------------
# T2 -- empty batches
# --------------------------------------------------------------------------

def _empty_inputs() -> ReconInputs:
    return ReconInputs(payments=(), bank_txns=(), invoices=(), seed=1, payments_per_window=6)


def test_match_once_on_a_wholly_empty_batch():
    out = match_once(_empty_inputs())
    assert out.assignments == ()
    assert out.refusals == ()
    assert out.no_candidate == ()
    assert out.unassigned_payment_ids == ()
    assert out.summary()["assigned"] == 0


def test_scoring_an_empty_batch_does_not_divide_by_zero():
    out = match_once(_empty_inputs())
    sc = score(out, links=(), total_payments=0, captured_payments=0)
    assert sc is not None


def test_rendering_an_empty_scorecard_produces_a_readable_block():
    out = match_once(_empty_inputs())
    sc = score(out, links=(), total_payments=0, captured_payments=0)
    text = render(sc, seed=1, payments_per_window=6, llm_enabled=False)
    assert "RECONCILIATION METRICS" in text
    assert text.count("\n") > 5


def test_payments_but_no_credits_leaves_every_payment_unassigned(batch):
    """Money came in, the bank statement has not arrived yet. Nothing is claimed."""
    inputs = ReconInputs(
        payments=batch.inputs.payments, bank_txns=(), invoices=batch.inputs.invoices,
        seed=1, payments_per_window=6,
    )
    out = match_once(inputs)
    assert out.assignments == ()
    captured = [p.id for p in batch.inputs.payments if p.captured]
    assert set(out.unassigned_payment_ids) == set(captured)


def test_credits_but_no_payments_gives_every_credit_no_candidate(batch):
    """The mirror case: a statement with nothing to reconcile it against."""
    inputs = ReconInputs(
        payments=(), bank_txns=batch.inputs.bank_txns, invoices=batch.inputs.invoices,
        seed=1, payments_per_window=6,
    )
    out = match_once(inputs)
    assert out.assignments == ()
    credits = [t.id for t in batch.inputs.bank_txns if t.is_credit]
    assert set(out.no_candidate) == set(credits)
    assert out.refusals == ()


# --------------------------------------------------------------------------
# T3 -- malformed loader input
# --------------------------------------------------------------------------

BANK_HEADER = "txn_date,value_date,description,ref_no,credit,debit,balance\n"


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "bad_amount",
    ["abc", "- 100", "(500)", "1.2.3", "NaN", "Infinity"],
)
def test_a_bad_amount_names_the_file_row_and_column(tmp_path, bad_amount):
    path = _write(
        tmp_path, "bank_statement.csv",
        BANK_HEADER + f"2026-09-05,2026-09-05,NEFT TEST,UTR001,{bad_amount},,1000.00\n",
    )
    with pytest.raises(ValueError) as e:
        load_bank_statement(path)
    msg = str(e.value)
    assert "bank_statement.csv" in msg
    assert "row 1" in msg
    assert "'credit'" in msg


def test_the_offending_row_number_is_the_offending_row(tmp_path):
    """Three good rows then a bad one must report row 4, not row 1."""
    rows = "".join(
        f"2026-09-0{i},2026-09-0{i},NEFT {i},UTR00{i},100.00,,1000.00\n"
        for i in range(1, 4)
    )
    path = _write(
        tmp_path, "bank_statement.csv",
        BANK_HEADER + rows + "2026-09-04,2026-09-04,NEFT BAD,UTR004,oops,,1000.00\n",
    )
    with pytest.raises(ValueError, match="row 4"):
        load_bank_statement(path)


def test_a_missing_column_is_reported_by_name(tmp_path):
    path = _write(
        tmp_path, "bank_statement.csv",
        "txn_date,value_date,description,ref_no,debit,balance\n"
        "2026-09-05,2026-09-05,NEFT TEST,UTR001,,1000.00\n",
    )
    with pytest.raises(ValueError, match="missing required column 'credit'"):
        load_bank_statement(path)


def test_a_blank_amount_is_zero_not_an_error(tmp_path):
    """A bank statement leaves the unused side of the ledger empty on every row."""
    path = _write(
        tmp_path, "bank_statement.csv",
        BANK_HEADER + "2026-09-05,2026-09-05,NEFT TEST,UTR001,500.00,,1000.00\n",
    )
    txns = load_bank_statement(path)
    assert txns[0].credit == 50_000
    assert txns[0].debit == 0


def test_a_negative_amount_loads_rather_than_being_silently_dropped(tmp_path):
    """
    A negative credit is wrong, but it is wrong DATA, not unparseable text. The loader's
    job is faithful ingest; conservation (MR4) is what notices money going the wrong way.
    """
    path = _write(
        tmp_path, "bank_statement.csv",
        BANK_HEADER + "2026-09-05,2026-09-05,NEFT REV,UTR001,-500.00,,1000.00\n",
    )
    txns = load_bank_statement(path)
    assert txns[0].credit == -50_000
    assert txns[0].is_credit is False


def test_an_empty_csv_with_only_headers_loads_as_an_empty_batch(tmp_path):
    assert load_bank_statement(_write(tmp_path, "b.csv", BANK_HEADER)) == ()


def test_invoice_loader_reports_its_own_file_and_column(tmp_path):
    path = _write(
        tmp_path, "invoices.csv",
        "invoice_no,customer_name,customer_gstin,invoice_date,due_date,"
        "gross_amount,tds_amount,currency,status,po_reference\n"
        "INV-2026-0001,Acme,GST1,2026-09-01,2026-09-30,not-a-number,0,INR,open,PO1\n",
    )
    with pytest.raises(ValueError) as e:
        load_invoices(path)
    assert "invoices.csv" in str(e.value)
    assert "'gross_amount'" in str(e.value)


@pytest.mark.parametrize(
    "text,expected",
    [("1,234.56", 123456), ("₹100", 10000), ("  42.50  ", 4250), ("", 0), ("-", 0)],
)
def test_rupee_parsing_accepts_what_real_exports_contain(text, expected):
    assert rupees_to_paise(text) == expected


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_amounts_are_rejected_though_they_are_valid_decimals(text):
    """
    Decimal("NaN") and Decimal("Infinity") parse cleanly. Anything validating by "does
    this parse as a Decimal" waves them straight through, and they fail much later.
    """
    with pytest.raises(ValueError, match="not finite"):
        rupees_to_paise(text)

"""
Disk -> dataclasses. Deliberately OUTSIDE the `recon` package.

`run.py` and this module are the only things that touch the filesystem on the engine's
behalf. The engine receives `ReconInputs` and nothing else, so there is no code path
by which it could reach the answer key even if the audit hook were removed. Keeping the
loader outside `recon` makes that structural rather than conventional: the boundary is
visible in the import graph.

Rupee strings are converted to integer paise here, once, using Decimal. Downstream code
never sees a float.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import config as cfg
from recon.schemas import BankTxn, Invoice, Payment, ReconInputs, rupees_to_paise


def load_payments(path: Path) -> tuple[Payment, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Payment(
            id=r["id"],
            amount=int(r["amount"]),
            currency=r["currency"],
            status=r["status"],
            captured=bool(r["captured"]),
            method=r["method"],
            order_id=r.get("order_id"),
            created_at=int(r["created_at"]),
            description=r.get("description", "") or "",
            contact=r.get("contact", "") or "",
            email=r.get("email", "") or "",
            provenance=r.get("provenance", "S"),
            fee=r["fee"] if r.get("fee") is not None else None,
            tax=r["tax"] if r.get("tax") is not None else None,
            bank=r.get("bank"),
            wallet=r.get("wallet"),
            bank_transaction_id=r.get("bank_transaction_id"),
            error_reason=r.get("error_reason"),
            invoice_id=r.get("invoice_id"),
            amount_refunded=int(r.get("amount_refunded") or 0),
            refund_status=r.get("refund_status"),
            notes=dict(r.get("notes") or {}),
        )
        for r in raw
    )


def _money(row: dict, field: str, path: Path, row_no: int, *, blank_ok: bool = True) -> int:
    """
    Read one rupee-denominated column, naming the file, row and column if it is bad.

    `rupees_to_paise` knows the text was malformed but not where it came from, and a
    traceback that says only `not a rupee amount: '(500)'` sends an operator hunting
    through a 200-row CSV by hand. The loader is the only layer that knows the
    coordinates, so it is the layer that attaches them.
    """
    if field not in row:
        raise ValueError(
            f"{path.name}: missing required column {field!r} "
            f"(row {row_no} has: {', '.join(sorted(k for k in row if k))})"
        )
    raw = row[field]
    if raw is None or (blank_ok and not str(raw).strip()):
        return 0
    try:
        return rupees_to_paise(raw)
    except ValueError as e:
        raise ValueError(f"{path.name} row {row_no}, column {field!r}: {e}") from None


def _text(row: dict, field: str, path: Path, row_no: int, default: str | None = None) -> str:
    """Read one string column, naming the file, row and column if it is absent."""
    if field not in row:
        if default is not None:
            return default
        raise ValueError(
            f"{path.name}: missing required column {field!r} "
            f"(row {row_no} has: {', '.join(sorted(k for k in row if k))})"
        )
    return row[field] or ""


def load_bank_statement(path: Path) -> tuple[BankTxn, ...]:
    """
    Parse an Indian bank statement export.

    Row ids are assigned by POSITION IN THE FILE, which is stable for a given file and
    is what ground truth refers to. Note that this makes ids a property of the file, not
    of the data -- the permutation ensemble shuffles the in-memory list, never the file,
    so ids stay meaningful across shuffled passes.
    """
    out: list[BankTxn] = []
    with path.open(newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f), start=1):
            txn_date = _text(row, "txn_date", path, i)
            out.append(
                BankTxn(
                    id=f"bank_txn_{i:04d}",
                    txn_date=txn_date,
                    value_date=_text(row, "value_date", path, i) or txn_date,
                    narration=_text(row, "description", path, i),
                    ref_no=_text(row, "ref_no", path, i),
                    credit=_money(row, "credit", path, i),
                    debit=_money(row, "debit", path, i),
                    balance=_money(row, "balance", path, i),
                )
            )
    return tuple(out)


def load_invoices(path: Path) -> tuple[Invoice, ...]:
    out: list[Invoice] = []
    with path.open(newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f), start=1):
            out.append(
                Invoice(
                    invoice_no=_text(row, "invoice_no", path, i),
                    customer_name=_text(row, "customer_name", path, i),
                    customer_gstin=_text(row, "customer_gstin", path, i),
                    invoice_date=_text(row, "invoice_date", path, i),
                    due_date=_text(row, "due_date", path, i),
                    gross_amount=_money(row, "gross_amount", path, i),
                    tds_amount=_money(row, "tds_amount", path, i),
                    currency=_text(row, "currency", path, i),
                    status=_text(row, "status", path, i),
                    po_reference=_text(row, "po_reference", path, i),
                )
            )
    return tuple(out)


def load_inputs(
    generated_dir: Path | None = None,
    seed: int = cfg.SEED_PRIMARY,
    payments_per_window: int = cfg.TARGET_POOL_SIZE,
) -> ReconInputs:
    """
    Build the engine's complete input from disk.

    This is the boundary crossing: paths go in, dataclasses come out, and nothing
    downstream ever sees a path again.
    """
    d = generated_dir or cfg.GENERATED
    return ReconInputs(
        payments=load_payments(d / "payments.json"),
        bank_txns=load_bank_statement(d / "bank_statement.csv"),
        invoices=load_invoices(d / "invoices.csv"),
        seed=seed,
        payments_per_window=payments_per_window,
    )

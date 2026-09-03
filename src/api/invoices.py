"""
Invoice ledger management -- side C of the reconciliation.

These are the only write endpoints in the system, and the exception is carefully bounded.

Uploading a ledger replaces **input data**, never a verdict. Nothing here can accept,
reject or re-score a match; the engine has to be re-run for an upload to change
anything, and the response says so explicitly rather than letting the numbers already
on screen imply they have updated. That is the honest workflow: new data means a new
run, not a patched old one.

Two rules the endpoints enforce rather than document:

**Validate before replacing.** An upload is parsed and checked in full before it can
touch the live ledger. A partially applied ledger is worse than no ledger, because the
next reconciliation then runs against data nobody reviewed.

**Every replacement is reversible.** The previous ledger is archived first, so an upload
is never a one-way door.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

import config as cfg
from recon.schemas import rupees_to_paise

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

MAX_UPLOAD_BYTES = 8 * 1024 * 1024

REQUIRED_COLUMNS = (
    "invoice_no",
    "customer_name",
    "customer_gstin",
    "invoice_date",
    "due_date",
    "gross_amount",
    "tds_amount",
    "currency",
    "status",
    "po_reference",
)


def _ledger_path() -> Path:
    return cfg.GENERATED / "invoices.csv"


def _versions_dir() -> Path:
    d = cfg.DATA / "ledger_versions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_ledger(text: str) -> tuple[list[dict], list[str]]:
    """
    Parse and validate an invoice ledger. Returns (rows, errors).

    Reports EVERY problem it finds rather than stopping at the first. Someone fixing a
    spreadsheet wants the whole list in one pass, not one error per upload attempt.
    """
    errors: list[str] = []
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        return [], ["file is empty or has no header row"]

    missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing:
        return [], [f"missing required column(s): {', '.join(missing)}"]

    rows: list[dict] = []
    seen: set[str] = set()

    for i, row in enumerate(reader, start=2):  # row 1 is the header
        no = (row.get("invoice_no") or "").strip()
        if not no:
            errors.append(f"row {i}: invoice_no is blank")
            continue
        if no in seen:
            # A duplicate invoice number silently breaks tier 1, which indexes
            # references to sets and refuses on collision. Better to reject the file.
            errors.append(f"row {i}: duplicate invoice_no {no!r}")
            continue
        seen.add(no)

        for field in ("gross_amount", "tds_amount"):
            raw = (row.get(field) or "").strip()
            if not raw:
                continue
            try:
                paise = rupees_to_paise(raw)
            except Exception:
                errors.append(f"row {i}: {field} {raw!r} is not a valid amount")
                continue
            if paise < 0:
                errors.append(f"row {i}: {field} is negative")

        for field in ("invoice_date", "due_date"):
            raw = (row.get(field) or "").strip()
            try:
                date.fromisoformat(raw)
            except ValueError:
                errors.append(
                    f"row {i}: {field} {raw!r} is not an ISO date (YYYY-MM-DD)"
                )

        rows.append(row)

    if not rows and not errors:
        errors.append("file has a header but no invoice rows")
    return rows, errors


async def _read_upload(file: UploadFile) -> str:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"file exceeds {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
        )
    try:
        # utf-8-sig strips the BOM Excel writes, which would otherwise corrupt the
        # first column name and produce a baffling "missing invoice_no" error.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "file is not valid UTF-8 text")


@router.get("")
def list_invoices(limit: int = 500, q: str | None = None) -> dict:
    """The live ledger the next reconciliation run will read."""
    path = _ledger_path()
    if not path.exists():
        return {"count": 0, "invoices": [], "source": None, "versions": []}

    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    if q:
        needle = q.casefold()
        rows = [
            r
            for r in rows
            if needle
            in f"{r.get('invoice_no', '')} {r.get('customer_name', '')}".casefold()
        ]
    return {
        "count": len(rows),
        "total": total,
        "invoices": rows[: max(0, limit)],
        "source": path.name,
        "versions": sorted(p.name for p in _versions_dir().glob("invoices_*.csv")),
    }


@router.post("/validate")
async def validate_upload(file: UploadFile = File(...)) -> dict:
    """
    Dry run: report what an upload WOULD do, and change nothing.

    Exists so a person can see the damage before committing to it. Uploading a ledger
    is reversible, but finding out it was wrong after the next reconciliation is a
    worse way to learn.
    """
    text = await _read_upload(file)
    rows, errors = validate_ledger(text)
    return {
        "filename": file.filename,
        "valid": not errors,
        "row_count": len(rows),
        "error_count": len(errors),
        "errors": errors[:50],
        "preview": rows[:5],
    }


@router.post("")
async def upload_invoices(file: UploadFile = File(...)) -> dict:
    """
    Replace the invoice ledger. Validates first, archives the previous version, then
    writes.
    """
    text = await _read_upload(file)
    rows, errors = validate_ledger(text)

    if errors:
        raise HTTPException(
            422,
            {
                "message": f"{len(errors)} validation error(s) — ledger NOT replaced",
                "error_count": len(errors),
                "errors": errors[:50],
            },
        )

    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    archived = None
    if path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archived = _versions_dir() / f"invoices_{stamp}.csv"
        archived.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    path.write_text(text, encoding="utf-8")

    return {
        "ok": True,
        "row_count": len(rows),
        "archived_previous_as": archived.name if archived else None,
        "next_step": (
            "Ledger replaced. The reconciliation currently on screen is UNCHANGED — "
            "re-run `python run.py match --verify` to reconcile against the new ledger."
        ),
    }


@router.delete("/revert")
def revert_to_previous() -> dict:
    """
    Restore the most recently archived ledger.

    Upload is reversible or it is not trustworthy. Someone who replaces the wrong file
    at 6pm needs one obvious way back.
    """
    versions = sorted(_versions_dir().glob("invoices_*.csv"))
    if not versions:
        raise HTTPException(404, "no archived ledger to revert to")

    latest = versions[-1]
    path = _ledger_path()
    path.write_text(latest.read_text(encoding="utf-8"), encoding="utf-8")
    latest.unlink()
    return {
        "ok": True,
        "restored_from": latest.name,
        "next_step": "Re-run `python run.py match --verify` to reconcile against it.",
    }


@router.get("/template", response_class=PlainTextResponse)
def template() -> str:
    """A valid one-row ledger, so the expected shape never has to be guessed at."""
    header = ",".join(REQUIRED_COLUMNS)
    example = (
        "INV-2026-1001,Acme Retail Private Limited,29AABCA1234M1Z5,"
        "2026-06-01,2026-07-01,12500.00,250.00,INR,open,PO-ACME-10001"
    )
    return f"{header}\n{example}\n"

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
from recon.schemas import (
    BankTxn,
    Invoice,
    Payment,
    PayerAuthorisation,
    ReconInputs,
    rupees_to_paise,
)


def _currency(value: str, where: str, path: Path, row: object) -> str:
    """
    Reject anything that is not INR, by name, at the boundary.

    **This engine is rupee-only and every amount downstream is integer paise.** The
    currency field was read and never checked, so a USD row would have had its amount
    parsed as paise and reconciled against rupee invoices -- silently, producing a
    confident wrong answer at roughly 85x the true value. Nothing later in the pipeline
    could have caught it: conservation would balance, because both sides of the
    comparison were wrong in the same way.

    Multi-currency is listed as out of scope in the README, and this is what honouring
    that costs: a named refusal at ingest rather than an unstated assumption. Doing FX
    properly needs a rate at settlement time, which is a different problem; pretending
    the field does not exist is not the alternative.
    """
    code = (value or "").strip().upper()
    if code != "INR":
        raise ValueError(
            f"{path.name}: {where} carries currency {value!r}, and this engine handles "
            f"INR only -- every amount downstream is integer paise. Multi-currency "
            f"reconciliation needs an FX rate at settlement time and is out of scope "
            f"(see README). Row: {row!r}."
        )
    return code


def load_payments(path: Path) -> tuple[Payment, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Payment(
            id=r["id"],
            amount=int(r["amount"]),
            currency=_currency(r["currency"], f"payment {r['id']}", path, r["id"]),
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


def _money(
    row: dict, field: str, path: Path, row_no: int, blank_means_zero: bool = True
) -> int:
    """
    Read one rupee-denominated column, naming the file, row and column if it is bad.

    `rupees_to_paise` knows the text was malformed but not where it came from, and a
    traceback that says only `not a rupee amount: '(500)'` sends an operator hunting
    through a 200-row CSV by hand. The loader is the only layer that knows the
    coordinates, so it is the layer that attaches them.

    **`blank_means_zero` is per-column, and it has to be.** A blank `credit` on a debit
    row is how every bank in the world writes a statement, so treating an empty cell as
    zero is correct there and refusing it would reject valid exports. A blank `balance`
    is not the same thing at all: it is a missing number, and reading it as zero produces
    a running balance that appears to collapse to nil and then recover -- which the
    continuity check would then report as a reconciliation failure at a row that is
    merely incomplete, sending an operator after a defect that does not exist.

    The distinction was previously global: every blank became zero, so the two cases were
    indistinguishable and the second was silent. Callers now say which they mean.
    """
    if field not in row:
        raise ValueError(
            f"{path.name}: missing required column {field!r} "
            f"(row {row_no} has: {', '.join(sorted(k for k in row if k))})"
        )
    raw = row[field]
    if raw is None or not str(raw).strip():
        if blank_means_zero:
            return 0
        raise ValueError(
            f"{path.name} row {row_no}, column {field!r}: empty. This column carries a "
            f"number that must be present -- reading a blank as zero here would put a "
            f"figure in the ledger that the file never stated."
        )
    try:
        return rupees_to_paise(raw)
    except ValueError as e:
        raise ValueError(f"{path.name} row {row_no}, column {field!r}: {e}") from None


def _text(row: dict, field: str, path: Path, row_no: int) -> str:
    """Read one string column, naming the file, row and column if it is absent."""
    if field not in row:
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
                    # A blank balance is a MISSING number, not zero -- see _money.
                    balance=_money(row, "balance", path, i, blank_means_zero=False),
                )
            )
    assert_balance_continuity(out, path)
    return tuple(out)

def assert_balance_continuity(txns: list[BankTxn], path: Path) -> int:
    """
    Every row's balance must equal the previous row's, plus its credit, less its debit.

    **This is the check a controller expects and the engine did not have.** The `balance`
    column was read and never verified, so a statement missing a row -- or carrying one
    twice -- loaded cleanly and reconciled cleanly, because every OTHER number in the file
    stays self-consistent when a row goes missing. The engine would then report a
    perfectly precise reconciliation of a statement that is not the account's history.
    That is the failure this project has hit four times from the generator side
    (`DEFECT_LOG` 2026-09-02-05, -08, 2026-09-03-03); the bank side had no equivalent
    guard at all.

    **Checked RELATIVELY, between consecutive rows, rather than against an opening
    balance.** An opening balance is a property of the account and of wherever the export
    happens to start, so requiring one would make the loader wrong on any statement
    beginning mid-history, and it would import a generator constant into the engine's view
    of the world. The difference between two consecutive balances is a fact the file
    asserts about itself, and it is exactly the fact a dropped row destroys.

    Skipped when the column is absent or entirely zero: real exports do omit it, and a
    check that cannot run should say nothing rather than fail. Returns how many rows were
    verified, so a caller can tell "checked and passed" from "had nothing to check".
    """
    if len(txns) < 2 or not any(t.balance for t in txns):
        return 0

    for i in range(1, len(txns)):
        prev, cur = txns[i - 1], txns[i]
        expected = prev.balance + cur.credit - cur.debit
        if cur.balance != expected:
            raise ValueError(
                f"{path.name} row {i + 1} ({cur.id}): the running balance does not "
                f"reconcile. The previous row closes at {prev.balance}p; this row "
                f"credits {cur.credit}p and debits {cur.debit}p, which should close at "
                f"{expected}p, but it states {cur.balance}p -- a discrepancy of "
                f"{cur.balance - expected:+d}p. A statement whose balance column does "
                f"not reconcile is missing a row, carrying one twice, or has been "
                f"edited; reconciling it would produce a precise answer about a history "
                f"that never happened."
            )
    return len(txns)


def load_payer_directory(path: Path | None = None) -> tuple[PayerAuthorisation, ...]:
    """
    Side D: the merchant's authorised-payer register.

    **Deliberately NOT part of `load_inputs`.** `ReconInputs` is what the engine
    receives, and the engine must never read this file -- an agent gathers the fact and
    asserts it through `match_once(evidence=...)`, so the engine weighs evidence rather
    than going looking for it. Putting the register on `ReconInputs` would quietly hand
    the matcher a fourth side and dissolve that boundary, which is the one the whole
    agentic design rests on. `tests/test_agent_tools.py` asserts nothing under
    `recon.engine` opens it.

    Absent file returns empty rather than raising: a batch generated before side D
    existed is still a valid batch, and an investigator with no register should report
    that it found nothing, not crash.
    """
    path = path or (cfg.GENERATED / "payer_directory.csv")
    if not path.is_file():
        return ()
    out: list[PayerAuthorisation] = []
    with path.open(newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f), start=1):
            out.append(
                PayerAuthorisation(
                    payer_name=_text(row, "payer_name", path, i),
                    authorised_for_customer=_text(
                        row, "authorised_for_customer", path, i
                    ),
                    relationship=_text(row, "relationship", path, i),
                    on_record_since=_text(row, "on_record_since", path, i),
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
                    currency=_currency(
                        _text(row, "currency", path, i),
                        f"invoice {row.get('invoice_no', '?')}",
                        path, i,
                    ),
                    status=_text(row, "status", path, i),
                    po_reference=_text(row, "po_reference", path, i),
                )
            )
    return tuple(out)


def load_inputs(
    generated_dir: Path | None = None,
    seed: int | None = None,
    payments_per_window: int | None = None,
) -> ReconInputs:
    """
    Build the engine's complete input from disk.

    This is the boundary crossing: paths go in, dataclasses come out, and nothing
    downstream ever sees a path again.

    **`None` means "whatever built this batch", not a default value.** Both parameters
    used to default to the config constants, which made an omitted flag and an explicit
    `--seed 20260905` indistinguishable: the mismatch check read
    `if seed != cfg.SEED_PRIMARY and ...`, so naming the primary seed explicitly against
    a batch built at another seed skipped the guard entirely and silently relabelled the
    run -- the exact opposite of what the guard promises. A sentinel is the only thing
    that can tell "not passed" from "passed, and happens to equal the default".

    `payments_per_window` was worse: it was overwritten with no check at all, so a
    density mismatch was never reported under any invocation.
    """
    d = generated_dir or cfg.GENERATED

    # The batch on disk knows what built it. Trust that over whatever the caller passed,
    # and refuse loudly on a mismatch rather than mislabelling the run: `match --seed X`
    # does not regenerate, so a caller naming a seed the data did not come from would
    # otherwise have every reported number printed under the wrong seed.
    manifest = d / "manifest.json"
    if manifest.exists():
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        on_disk_seed = int(meta.get("seed", cfg.SEED_PRIMARY))
        on_disk_ppw = int(meta.get("payments_per_window", cfg.TARGET_POOL_SIZE))

        mismatches = []
        if seed is not None and seed != on_disk_seed:
            mismatches.append(f"seed {seed} was requested but the batch is seed {on_disk_seed}")
        if payments_per_window is not None and payments_per_window != on_disk_ppw:
            mismatches.append(
                f"density {payments_per_window} was requested but the batch is "
                f"density {on_disk_ppw}"
            )
        if mismatches:
            raise ValueError(
                f"The batch in {d} does not match what was requested: "
                + "; ".join(mismatches)
                + ". `match` does not regenerate. Run `python run.py generate "
                f"--seed {seed if seed is not None else on_disk_seed} "
                f"--payments-per-window "
                f"{payments_per_window if payments_per_window is not None else on_disk_ppw}`"
                " first, or drop the flags to use the batch on disk."
            )
        seed, payments_per_window = on_disk_seed, on_disk_ppw

    # No manifest (a batch written before manifests existed, or a bare fixture
    # directory): fall back to the config defaults rather than carrying None onward.
    if seed is None:
        seed = cfg.SEED_PRIMARY
    if payments_per_window is None:
        payments_per_window = cfg.TARGET_POOL_SIZE

    return ReconInputs(
        payments=load_payments(d / "payments.json"),
        bank_txns=load_bank_statement(d / "bank_statement.csv"),
        invoices=load_invoices(d / "invoices.csv"),
        seed=seed,
        payments_per_window=payments_per_window,
    )


def load_foreign_claims(path: Path) -> tuple["ForeignClaim", ...]:
    """
    Read a third party's assignments: which payments they say each credit is.

        bank_txn_id,payment_ids
        bank_txn_0001,pay_A
        bank_txn_0002,"pay_B pay_C"

    `payment_ids` is whitespace- or semicolon-separated so a many-to-one settlement can
    be expressed without inventing a nested format, and so the file stays something an
    incumbent's export can be coerced into with a spreadsheet rather than a script.

    **Nothing here validates the claims.** A row naming a payment that does not exist is
    loaded and passed through; `recon.verify.foreign` reports it as `unknown_id`. That
    split is deliberate: the loader's job is to say where a file is malformed, and the
    auditor's job is to say where a CLAIM is unsupportable. A loader that silently
    dropped unresolvable rows would improve the claimant's audit by discarding its worst
    rows, which is exactly backwards.
    """
    from recon.verify.foreign import ForeignClaim

    claims: list[ForeignClaim] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row_no, row in enumerate(csv.DictReader(fh), start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            txn_id = _text(row, "bank_txn_id", path, row_no).strip()
            raw = _text(row, "payment_ids", path, row_no)
            ids = tuple(
                p for p in raw.replace(";", " ").replace(",", " ").split() if p
            )
            if not txn_id:
                raise ValueError(
                    f"{path.name}: row {row_no} has an empty bank_txn_id. A claim with "
                    f"no credit to attach to cannot be audited or ignored safely."
                )
            if not ids:
                raise ValueError(
                    f"{path.name}: row {row_no} claims {txn_id!r} with no payment ids. "
                    f"If the intent is 'no match', omit the row -- an empty claim and an "
                    f"absent claim mean different things and this file cannot express "
                    f"the difference."
                )
            claims.append(ForeignClaim(bank_txn_id=txn_id, payment_ids=ids))
    return tuple(claims)

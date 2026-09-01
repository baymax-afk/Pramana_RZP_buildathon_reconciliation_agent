"""
Batch generator: emits the three reconciliation sides and the ground-truth answer key.

Density, not batch size, is the primary parameter. `payments_per_window` is fixed and
the date range is DERIVED from it, so scaling `n` widens the calendar instead of
crowding each settlement window. Getting this backwards -- fixing the range and
letting density float -- is what breaks subset-sum at scale: tripling the record count
inside a fixed range takes the per-credit pool from 12 to 36, and the number of
subsets landing within tolerance of a credit by pure coincidence rises with it.

Because density is a dial, it can be swept deliberately, and the sweep is the
project's central empirical claim: as density rises a correct engine should refuse
more while holding precision flat.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path

import config as cfg

from ..schemas import (
    BankTxn,
    Invoice,
    Payment,
    ReconInputs,
    TruthLink,
    paise_to_rupees,
)
from . import defects, fees
from .customers import BY_KEY, CONFUSABLE_PAIRS, REGISTRY, Customer

START_DATE = date(2026, 6, 1)


@dataclass(frozen=True, slots=True)
class GeneratedBatch:
    inputs: ReconInputs
    truth: tuple[TruthLink, ...]
    ambiguity_bank_txn_id: str
    stats: dict


# --------------------------------------------------------------------------
# Ingest of real data (tiers R1 and R2)
# --------------------------------------------------------------------------
def _load_real_payments(path: Path) -> list[Payment]:
    """Tier R1: genuinely captured (and genuinely failed) test-mode payments."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in raw["items"]:
        out.append(
            Payment(
                id=r["id"],
                amount=r["amount"],
                currency="INR",
                status=r["status"],
                captured=r["captured"],
                method=r["method"],
                order_id=r.get("order_id"),
                created_at=r["created_at"],
                description=r.get("description", ""),
                contact=r.get("contact", ""),
                email=r.get("email", ""),
                provenance="R1",
                fee=r.get("fee"),
                tax=r.get("tax"),
                bank=r.get("bank"),
                wallet=r.get("wallet"),
                bank_transaction_id=r.get("bank_transaction_id"),
                error_reason=r.get("error_reason"),
                notes=dict(r.get("notes") or {}),
            )
        )
    return out


def _load_r2_orders(path: Path) -> list[dict]:
    """
    Tier R2: real Razorpay-issued orders that were never completed.

    Returned as raw dicts, not Payments, precisely because they are NOT payments.
    The caller must decide explicitly how to represent them; there is no code path
    here that silently promotes an uncaptured order into captured revenue.
    """
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["items"]


# --------------------------------------------------------------------------
# Synthetic payment construction
# --------------------------------------------------------------------------
def _synth_payment(
    rng: random.Random,
    idx: int,
    customer: Customer,
    invoice_no: str,
    amount_paise: int,
    created_at: int,
    provenance: str = "S",
    captured: bool = True,
) -> Payment:
    fee, tax = fees.fee_and_tax(amount_paise) if captured else (None, None)
    method = rng.choice(["netbanking", "netbanking", "wallet", "upi", "card"])
    return Payment(
        id=f"pay_SYN{idx:05d}{rng.randint(1000, 9999)}",
        amount=amount_paise,
        currency="INR",
        status="captured" if captured else "failed",
        captured=captured,
        method=method,
        order_id=f"order_SYN{idx:05d}{rng.randint(1000, 9999)}",
        created_at=created_at,
        description=f"#{invoice_no}",
        contact=customer.contact,
        email=customer.email,
        provenance=provenance,  # type: ignore[arg-type]
        fee=fee,
        tax=tax,
        bank=rng.choice(["HDFC", "ICIC", "UTIB", "SBIN", "KKBK"]) if method == "netbanking" else None,
        error_reason=None if captured else rng.choice(["payment_failed", "payment_cancelled"]),
        notes={
            "customer_name": rng.choice(customer.all_names),
            "invoice_no": invoice_no,
            "name_family": customer.key,
        },
    )


def _r2_as_payments(orders: list[dict], rng: random.Random) -> list[Payment]:
    """
    Promote tier-R2 orders into settled payments, with SYNTHETIC fees.

    Everything identifying is real: the Razorpay-issued order id, the amount, the
    receipt, the notes, the server-side timestamp. Only the capture and the fee/tax
    are synthesised, from the rate model measured on R1.

    This is the one place the codebase turns an uncaptured order into something that
    looks like revenue, so it is done explicitly, in a named function, with the
    provenance stamped "R2" on every record it emits. Nothing downstream can mistake
    these for genuinely captured payments, and the README states the same thing in
    the same terms: an uncaptured order is not a payment.
    """
    out: list[Payment] = []
    for o in orders:
        fee, tax = fees.fee_and_tax(o["amount"])
        notes = dict(o.get("notes") or {})
        out.append(
            Payment(
                id="pay_R2X" + o["id"][6:],  # derived from the real order id
                amount=o["amount"],
                currency="INR",
                status="captured",
                captured=True,
                method="netbanking",
                order_id=o["id"],              # real, inspectable in the dashboard
                created_at=o["created_at"],    # real server timestamp
                description=f"#{o['receipt']}",
                contact=BY_KEY.get(notes.get("name_family", ""), REGISTRY[0]).contact,
                email=BY_KEY.get(notes.get("name_family", ""), REGISTRY[0]).email,
                provenance="R2",
                fee=fee,   # SYNTHETIC -- derived from the R1 rate model
                tax=tax,   # SYNTHETIC
                bank=rng.choice(["HDFC", "ICIC", "UTIB"]),
                notes=notes,
            )
        )
    return out


def _ts(d: date, rng: random.Random) -> int:
    """A unix timestamp somewhere in business hours on the given date."""
    import calendar

    return calendar.timegm(d.timetuple()) + rng.randint(9 * 3600, 18 * 3600)


# --------------------------------------------------------------------------
# The generator
# --------------------------------------------------------------------------
def generate(
    seed: int = cfg.SEED_PRIMARY,
    n_payments: int = cfg.N_PAYMENTS,
    payments_per_window: int = cfg.TARGET_POOL_SIZE,
    data_dir: Path | None = None,
) -> GeneratedBatch:
    rng = random.Random(seed)
    data_dir = data_dir or cfg.DATA

    real = _load_real_payments(data_dir / "real_payments.json")
    r2_pool = _r2_as_payments(_load_r2_orders(data_dir / "mcp_created" / "orders_r2.json"), rng)

    n_windows = max(4, -(-n_payments // payments_per_window))
    window_days = cfg.SETTLEMENT_WINDOW_DAYS

    payments: list[Payment] = []
    invoices: list[Invoice] = []
    bank_txns: list[BankTxn] = []
    truth: list[TruthLink] = []
    invoice_seq = 1000

    # Which window hosts the hand-placed ambiguity case. Kept away from the edges so
    # it sits among ordinary traffic rather than at a boundary.
    ambiguity_window = n_windows // 2

    # Distribute real records across the early windows so they are genuinely mixed in
    # rather than clustered at one end.
    real_pool = list(real)
    rng.shuffle(real_pool)
    rng.shuffle(r2_pool)

    def next_invoice(cust: Customer, gross: int, d: date, with_tds: bool) -> Invoice:
        nonlocal invoice_seq
        invoice_seq += 1
        no = f"INV-2026-{invoice_seq}"
        tds = defects.tds_for(gross) if with_tds else 0
        invoices.append(
            Invoice(
                invoice_no=no,
                customer_name=rng.choice(cust.all_names),
                customer_gstin=cust.gstin,
                invoice_date=d.isoformat(),
                due_date=(d + timedelta(days=30)).isoformat(),
                gross_amount=gross,
                tds_amount=tds,
                currency="INR",
                status="open",
                po_reference=f"PO-{cust.key.upper()[:4]}-{rng.randint(10000, 99999)}",
            )
        )
        return invoices[-1]

    balance = 5_000_000
    txn_seq = 0
    pay_seq = 0

    for w in range(n_windows):
        win_start = START_DATE + timedelta(days=w * window_days)
        settle_date = win_start + timedelta(days=window_days)

        # ---------------- the ambiguity window ----------------
        if w == ambiguity_window:
            amb_payments, amb_invoices, amb_credit, amb_id, filler = _build_ambiguity_window(
                rng, win_start, settle_date, payments_per_window, next_invoice, txn_seq
            )
            payments.extend(amb_payments)
            payments.extend(filler)
            txn_seq += 1
            balance += amb_credit.credit
            bank_txns.append(
                BankTxn(
                    id=amb_credit.id,
                    txn_date=amb_credit.txn_date,
                    value_date=amb_credit.value_date,
                    narration=amb_credit.narration,
                    ref_no=amb_credit.ref_no,
                    credit=amb_credit.credit,
                    debit=0,
                    balance=balance,
                )
            )
            truth.append(
                TruthLink(
                    bank_txn_id=amb_credit.id,
                    payment_ids=tuple(p.id for p in amb_payments),
                    invoice_nos=tuple(i.invoice_no for i in amb_invoices),
                    defect_labels=("many_to_one", "mdr_fee"),
                    relation="many_to_one",
                    expected_verdict="refuse",
                )
            )
            # Filler payments in this window settle in their own ordinary credits.
            for p in filler:
                txn_seq += 1
                net = p.amount - (p.fee or 0)
                balance += net
                cust = BY_KEY[p.notes["name_family"]]
                utr = defects.make_utr(rng)
                nar = defects.narrate(rng, utr, cust, force_style="neft")
                bank_txns.append(
                    BankTxn(
                        id=f"bank_txn_{txn_seq:04d}",
                        txn_date=settle_date.isoformat(),
                        value_date=settle_date.isoformat(),
                        narration=nar.text,
                        ref_no=utr,
                        credit=net,
                        debit=0,
                        balance=balance,
                    )
                )
                truth.append(
                    TruthLink(
                        bank_txn_id=f"bank_txn_{txn_seq:04d}",
                        payment_ids=(p.id,),
                        invoice_nos=(p.notes["invoice_no"],),
                        defect_labels=("mdr_fee",),
                        relation="one_to_one",
                        expected_verdict="assign",
                    )
                )
            continue

        # ---------------- ordinary windows ----------------
        window_payments: list[Payment] = []
        for _ in range(payments_per_window):
            if len(payments) + len(window_payments) >= n_payments:
                break
            pay_seq += 1
            pay_date = win_start + timedelta(days=rng.randrange(window_days))

            # Consume a real record where one is available, so real and synthetic are
            # genuinely interleaved rather than segregated.
            if r2_pool and rng.random() < 0.12:
                rp = r2_pool.pop()
                cust = BY_KEY.get(rp.notes.get("name_family", ""), REGISTRY[0])
                inv = next_invoice(cust, rp.amount, pay_date, with_tds=False)
                notes = dict(rp.notes); notes["invoice_no"] = inv.invoice_no
                window_payments.append(
                    replace(rp, notes=notes, created_at=_ts(pay_date, rng))
                )
                continue

            if real_pool and rng.random() < 0.35:
                rp = real_pool.pop()
                fam = rp.notes.get("name_family")
                cust = BY_KEY.get(fam) if fam else rng.choice(REGISTRY)
                cust = cust or rng.choice(REGISTRY)
                inv = next_invoice(cust, rp.amount, pay_date, with_tds=False)
                notes = dict(rp.notes)
                notes.setdefault("customer_name", cust.canonical_name)
                notes["invoice_no"] = inv.invoice_no
                notes["name_family"] = cust.key
                window_payments.append(
                    replace(rp, notes=notes, created_at=_ts(pay_date, rng))
                )
                continue

            cust = rng.choice(REGISTRY)
            gross = rng.randrange(cfg.MIN_PAYMENT_PAISE, cfg.MAX_PAYMENT_PAISE, 100)
            with_tds = rng.random() < 0.18
            inv = next_invoice(cust, gross, pay_date, with_tds)
            window_payments.append(
                _synth_payment(rng, pay_seq, cust, inv.invoice_no, gross, _ts(pay_date, rng))
            )

        payments.extend(window_payments)
        settleable = [p for p in window_payments if p.captured]
        rng.shuffle(settleable)

        # Partition the window's settleable payments into bank credits.
        i = 0
        while i < len(settleable):
            roll = rng.random()

            # --- many-to-one: one credit covering 2..5 payments ---
            if roll < 0.22 and len(settleable) - i >= 3:
                k = min(rng.randint(2, 5), len(settleable) - i)
                group = settleable[i : i + k]
                i += k
                txn_seq += 1
                gross_net = sum(p.amount - (p.fee or 0) for p in group)
                labels = ["many_to_one", "mdr_fee"]

                tds_total = sum(
                    inv.tds_amount
                    for inv in invoices
                    if inv.invoice_no in {p.notes["invoice_no"] for p in group}
                )
                credit = gross_net - tds_total
                if tds_total:
                    labels.append("tds_deduction")

                # refund netted inside the batch
                if rng.random() < 0.20:
                    refund = rng.randrange(5_000, 50_000, 100)
                    credit -= refund
                    labels.append("refund_netted")

                drift = rng.choice([0, 1, 1, 2])
                if drift:
                    labels.append("settlement_drift")
                sd = settle_date + timedelta(days=drift)

                utr = defects.make_utr(rng)
                nar = defects.narrate(rng, utr, None, n_txns=k, force_style="settlement")
                balance += credit
                bank_txns.append(
                    BankTxn(
                        id=f"bank_txn_{txn_seq:04d}",
                        txn_date=sd.isoformat(),
                        value_date=sd.isoformat(),
                        narration=nar.text,
                        ref_no=utr,
                        credit=credit,
                        debit=0,
                        balance=balance,
                    )
                )
                truth.append(
                    TruthLink(
                        bank_txn_id=f"bank_txn_{txn_seq:04d}",
                        payment_ids=tuple(p.id for p in group),
                        invoice_nos=tuple(p.notes["invoice_no"] for p in group),
                        defect_labels=tuple(labels),
                        relation="many_to_one",
                        expected_verdict="assign",
                    )
                )
                continue

            # --- single payment ---
            p = settleable[i]
            i += 1
            cust = BY_KEY.get(p.notes.get("name_family", ""), REGISTRY[0])
            inv = next(
                (x for x in invoices if x.invoice_no == p.notes["invoice_no"]), None
            )
            net = p.amount - (p.fee or 0)
            labels = ["mdr_fee"]
            relation = "one_to_one"

            if inv and inv.tds_amount:
                net -= inv.tds_amount
                labels.append("tds_deduction")

            # partial payment
            if rng.random() < 0.08:
                net = int(net * rng.uniform(0.35, 0.75))
                labels.append("partial_payment")
                relation = "partial"

            # paisa-level rounding
            if rng.random() < 0.15:
                net, _ = defects.apply_paisa_rounding(rng, net)
                labels.append("paisa_rounding")

            # payment made but not yet settled -> no bank credit at all
            if rng.random() < 0.06:
                truth.append(
                    TruthLink(
                        bank_txn_id="",
                        payment_ids=(p.id,),
                        invoice_nos=(p.notes["invoice_no"],),
                        defect_labels=("unsettled",),
                        relation="unmatched",
                        expected_verdict="refuse",
                    )
                )
                continue

            drift = rng.choice([0, 0, 1, 2])
            if drift:
                labels.append("settlement_drift")
            sd = settle_date + timedelta(days=drift)

            if cust.key in {a for a, _ in CONFUSABLE_PAIRS} | {b for _, b in CONFUSABLE_PAIRS}:
                labels.append("near_duplicate_name")

            txn_seq += 1
            utr = defects.make_utr(rng)
            nar = defects.narrate(rng, utr, cust)
            balance += net
            bank_txns.append(
                BankTxn(
                    id=f"bank_txn_{txn_seq:04d}",
                    txn_date=sd.isoformat(),
                    value_date=sd.isoformat(),
                    narration=nar.text,
                    ref_no=utr,
                    credit=net,
                    debit=0,
                    balance=balance,
                )
            )
            truth.append(
                TruthLink(
                    bank_txn_id=f"bank_txn_{txn_seq:04d}",
                    payment_ids=(p.id,),
                    invoice_nos=(p.notes["invoice_no"],),
                    defect_labels=tuple(labels),
                    relation=relation,  # type: ignore[arg-type]
                    expected_verdict="assign",
                )
            )

    # ---- duplicate UTR: clone one existing credit's reference onto another ----
    credits = [t for t in bank_txns if t.is_credit]
    if len(credits) >= 2:
        a, b = rng.sample(credits, 2)
        idx = bank_txns.index(b)
        bank_txns[idx] = BankTxn(
            id=b.id, txn_date=b.txn_date, value_date=b.value_date,
            narration=b.narration, ref_no=a.ref_no,  # <- collision
            credit=b.credit, debit=b.debit, balance=b.balance,
        )
        for j, t in enumerate(truth):
            if t.bank_txn_id == b.id:
                truth[j] = TruthLink(
                    t.bank_txn_id, t.payment_ids, t.invoice_nos,
                    tuple(t.defect_labels) + ("duplicate_utr",),
                    t.relation, t.expected_verdict,
                )

    # ---- protect the ambiguity window (see _protect_ambiguity_window) ----
    amb_id = _ambiguity_id(truth)
    if amb_id:
        payments = _protect_ambiguity_window(payments, bank_txns, truth, amb_id)

    inputs = ReconInputs(
        payments=tuple(payments),
        bank_txns=tuple(bank_txns),
        invoices=tuple(invoices),
        seed=seed,
        payments_per_window=payments_per_window,
    )
    stats = _summarise(inputs, truth, n_windows, payments_per_window)
    return GeneratedBatch(inputs, tuple(truth), _ambiguity_id(truth), stats)


# --------------------------------------------------------------------------
# The hand-placed ambiguity case
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Credit:
    id: str
    txn_date: str
    value_date: str
    narration: str
    ref_no: str
    credit: int


def _build_ambiguity_window(
    rng: random.Random,
    win_start: date,
    settle_date: date,
    pool_size: int,
    next_invoice,
    txn_seq: int,
):
    """
    Build the window containing the hand-placed ambiguity case.

    Four payments whose NET amounts are exactly 50000, 30000, 45000, 35000 paise, so
    that {P1,P2} and {P3,P4} both sum to exactly 80000 -- the credit. The four gross
    amounts are all DISTINCT (the fee model is inverted to hit each net exactly), so
    no amount-level signal separates the pairs. All four share a settlement window and
    a payment method, so date and method cannot break the tie either. The credit's
    narration carries no payer name and a reference matching no payment, so tier 1
    cannot fire and the Fellegi-Sunter name channel has near-zero and EQUAL evidence
    for all four candidates.

    THE STRUCTURAL GUARANTEE: every other payment placed in this window has a net
    LARGER than the credit itself. Since all amounts are positive, no subset containing
    a filler payment can ever sum to 80000. Only the four crafted payments can
    participate, and among those exactly two pairs collide -- no triple can, because
    any three of them already exceed 80000. The ambiguity therefore cannot be resolved
    by accident, and cannot be destroyed by accident either.

    This is asserted, not assumed: `assert_ambiguity_is_exact` brute-forces every
    subset of the window afterwards and fails generation unless exactly two fit.
    """
    cust = BY_KEY["quantum"]
    amb_payments: list[Payment] = []
    amb_invoices: list[Invoice] = []

    for i, net_target in enumerate(cfg.AMBIGUITY_NET_PAISE):
        gross = fees.gross_for_target_net(net_target)
        pay_date = win_start + timedelta(days=rng.randrange(cfg.SETTLEMENT_WINDOW_DAYS))
        inv = next_invoice(cust, gross, pay_date, False)
        amb_invoices.append(inv)
        fee, tax = fees.fee_and_tax(gross)
        amb_payments.append(
            Payment(
                id=f"pay_AMBIG{i}{rng.randint(100000, 999999)}",
                amount=gross,
                currency="INR",
                status="captured",
                captured=True,
                method="netbanking",  # identical across all four
                order_id=f"order_AMBIG{i}{rng.randint(100000, 999999)}",
                created_at=_ts(pay_date, rng),
                description=f"#{inv.invoice_no}",
                contact=cust.contact,
                email=cust.email,
                provenance="S",
                fee=fee,
                tax=tax,
                bank="HDFC",
                notes={
                    "customer_name": cust.canonical_name,
                    "invoice_no": inv.invoice_no,
                    "name_family": cust.key,
                },
            )
        )

    # Filler payments, every one strictly larger than the credit.
    filler: list[Payment] = []
    floor = cfg.AMBIGUITY_CREDIT_PAISE * 2
    for j in range(max(0, pool_size - 4)):
        fc = rng.choice(REGISTRY)
        gross = rng.randrange(floor, floor + 2_000_000, 100)
        assert fees.net_settled(gross) > cfg.AMBIGUITY_CREDIT_PAISE
        pay_date = win_start + timedelta(days=rng.randrange(cfg.SETTLEMENT_WINDOW_DAYS))
        inv = next_invoice(fc, gross, pay_date, False)
        filler.append(
            _synth_payment(rng, 90000 + j, fc, inv.invoice_no, gross, _ts(pay_date, rng))
        )

    nar = defects.anonymous_settlement_narration(rng, 2)
    credit = _Credit(
        id=f"bank_txn_{txn_seq + 1:04d}",
        txn_date=settle_date.isoformat(),
        value_date=settle_date.isoformat(),
        narration=nar.text,
        ref_no=defects.make_utr(rng),  # matches no payment
        credit=cfg.AMBIGUITY_CREDIT_PAISE,
    )
    return amb_payments, amb_invoices, credit, credit.id, filler


def _protect_ambiguity_window(
    payments: list[Payment],
    bank_txns: list[BankTxn],
    truth: list[TruthLink],
    amb_id: str,
) -> list[Payment]:
    """
    Enforce the ambiguity guarantee GLOBALLY, not just within its own window.

    Placing oversized filler inside the ambiguity window is not sufficient. Settlement
    windows overlap at their boundaries: a payment belonging to the FOLLOWING window
    can be dated on the ambiguity credit's own settlement date, which puts it squarely
    inside the credit's candidate pool. That happened -- a payment netting 27,371p
    landed at lag 0 and could in principle have joined a subset reaching the credit.
    It did not create a third candidate, but only by luck, and a guarantee that holds
    by luck is not a guarantee.

    The repair shifts any such interloper one day later, out of the credit's lookback.
    Its own settlement credit is dated from its window's settle date, which is strictly
    later, so the shift cannot orphan it -- it stays inside its own pool.

    Raising the amount instead would cascade: the payment's invoice, its net, and the
    bank credit derived from that net would all have to be rebuilt. Moving the date
    touches one field and nothing downstream of it.
    """
    credit = next((t for t in bank_txns if t.id == amb_id), None)
    if credit is None:
        return payments
    link = next((t for t in truth if t.bank_txn_id == amb_id), None)
    crafted = set(link.payment_ids) if link else set()
    cd = date.fromisoformat(credit.txn_date)
    lookback = cfg.SETTLEMENT_WINDOW_DAYS + 2  # deliberately wider than the engine's rule

    out: list[Payment] = []
    shifted = 0
    for p in payments:
        if (
            p.captured
            and p.fee is not None
            and p.id not in crafted
            and 0 <= (cd - date.fromtimestamp(p.created_at)).days <= lookback
            and (p.amount - p.fee) <= cfg.AMBIGUITY_CREDIT_PAISE
        ):
            out.append(replace(p, created_at=p.created_at + 86_400 * (lookback + 1)))
            shifted += 1
        else:
            out.append(p)

    # Post-condition: no interloper may remain. If one does, the invariant is broken
    # in a way the shift cannot fix and generation must fail rather than emit a
    # centrepiece case that only appears to hold.
    for p in out:
        if p.captured and p.fee is not None and p.id not in crafted:
            lag = (cd - date.fromtimestamp(p.created_at)).days
            if 0 <= lag <= lookback and (p.amount - p.fee) <= cfg.AMBIGUITY_CREDIT_PAISE:
                raise AssertionError(
                    f"Ambiguity guarantee cannot be enforced: {p.id} nets "
                    f"{p.amount - p.fee}p at lag {lag} from the ambiguity credit and "
                    f"could join a subset reaching {cfg.AMBIGUITY_CREDIT_PAISE}p."
                )
    return out


def _ambiguity_id(truth: list[TruthLink]) -> str:
    for t in truth:
        if t.expected_verdict == "refuse" and t.relation == "many_to_one":
            return t.bank_txn_id
    return ""


# --------------------------------------------------------------------------
# Anti-accident assertions -- these FAIL THE BUILD, they do not warn
# --------------------------------------------------------------------------
def assert_ambiguity_is_exact(batch: GeneratedBatch) -> int:
    """
    Brute-force every subset of the ambiguity window and assert that EXACTLY two fall
    within tolerance of the credit.

    This closes the hole where some later-injected defect shifts an unrelated amount
    and accidentally creates a third candidate (making the case a different puzzle) or
    destroys one (making it resolvable, and the demo a lie).
    """
    amb_id = batch.ambiguity_bank_txn_id
    credit = next(t for t in batch.inputs.bank_txns if t.id == amb_id)
    link = next(t for t in batch.truth if t.bank_txn_id == amb_id)
    window_ids = set(link.payment_ids)

    # The candidate pool is every captured payment settling in this credit's window.
    pool = [
        p for p in batch.inputs.payments
        if p.captured and p.fee is not None
        and (p.id in window_ids or _same_window(p, credit))
    ]
    tol = cfg.TOL_ABS_PAISE
    hits = []
    for k in range(1, cfg.MAX_SUBSET_K + 1):
        for combo in combinations(pool, k):
            if abs(sum(p.amount - p.fee for p in combo) - credit.credit) <= tol:
                hits.append(tuple(sorted(p.id for p in combo)))

    if len(hits) != cfg.AMBIGUITY_EXPECTED_CANDIDATES:
        raise AssertionError(
            f"Ambiguity case is not ambiguous as specified: expected exactly "
            f"{cfg.AMBIGUITY_EXPECTED_CANDIDATES} subsets within {tol}p of "
            f"{credit.credit}p, found {len(hits)}: {hits}. Generation fails rather "
            f"than emitting a centrepiece demo case that does not hold."
        )
    return len(hits)


def _same_window(p: Payment, credit: BankTxn) -> bool:
    from datetime import datetime

    cd = datetime.fromisoformat(credit.txn_date).date()
    pd_ = date.fromtimestamp(p.created_at)
    return timedelta(days=0) <= (cd - pd_) <= timedelta(days=cfg.SETTLEMENT_WINDOW_DAYS + 2)


def assert_tolerance_sanity(batch: GeneratedBatch) -> tuple[int, int]:
    """
    Assert the matching tolerance stays far below the smallest payment.

    If tolerance ever approaches the smallest payment in a candidate pool, then a
    subset S and the subset S plus one small payment BOTH satisfy the constraint --
    every many-to-one result becomes meaningless and the uniqueness test silently
    degrades into noise. A 100x margin is required.
    """
    nets = [
        p.amount - p.fee
        for p in batch.inputs.payments
        if p.captured and p.fee is not None
    ]
    smallest = min(nets)
    if cfg.TOL_ABS_PAISE * 100 > smallest:
        raise AssertionError(
            f"Tolerance sanity violated: TOL_ABS_PAISE={cfg.TOL_ABS_PAISE} is within "
            f"100x of the smallest net payment ({smallest}p). Subset-sum uniqueness "
            f"cannot be trusted at this setting."
        )
    return cfg.TOL_ABS_PAISE, smallest


def assert_pool_bound(batch: GeneratedBatch) -> int:
    """
    Report the worst settlement-window pool, and fail the build only at or below the
    DEFAULT density.

    The asymmetry is deliberate. At the default density a pool above MAX_POOL means
    the date range was derived wrongly and the density invariant has broken -- that is
    a generator bug and must fail loudly.

    Above the default density, an oversized pool is not a bug: it is the phenomenon
    under study. The density sweep deliberately crowds the windows to see whether the
    engine refuses more while holding precision flat. There, exceeding MAX_POOL is the
    engine's problem to handle -- it emits `decomposition_out_of_bounds` and refuses,
    never guesses -- and having the generator refuse to build the batch would make the
    sweep impossible and delete the project's central empirical result.

    So: crowded windows at high density are data. Crowded windows at default density
    are a defect.
    """
    from collections import Counter

    by_day: Counter[str] = Counter()
    for p in batch.inputs.payments:
        if p.captured:
            by_day[date.fromtimestamp(p.created_at).isoformat()] += 1
    worst = max(
        (
            sum(
                v for d, v in by_day.items()
                if 0 <= (date.fromisoformat(c.txn_date) - date.fromisoformat(d)).days
                <= cfg.SETTLEMENT_WINDOW_DAYS
            )
            for c in batch.inputs.bank_txns if c.is_credit
        ),
        default=0,
    )
    at_or_below_default = batch.inputs.payments_per_window <= cfg.TARGET_POOL_SIZE
    if worst > cfg.MAX_POOL and at_or_below_default:
        raise AssertionError(
            f"Density invariant violated at default density: a settlement window holds "
            f"{worst} candidate payments, above MAX_POOL={cfg.MAX_POOL}. The date range "
            f"is being derived wrongly. Widen it or lower payments_per_window -- do not "
            f"raise the cap."
        )
    return worst


# --------------------------------------------------------------------------
# Emit
# --------------------------------------------------------------------------
def _summarise(inputs: ReconInputs, truth, n_windows: int, ppw: int) -> dict:
    from collections import Counter

    labels: Counter[str] = Counter()
    for t in truth:
        labels.update(t.defect_labels)
    prov: Counter[str] = Counter(p.provenance for p in inputs.payments)
    return {
        "seed": inputs.seed,
        "payments": len(inputs.payments),
        "captured": sum(1 for p in inputs.payments if p.captured),
        "bank_txns": len(inputs.bank_txns),
        "invoices": len(inputs.invoices),
        "windows": n_windows,
        "payments_per_window": ppw,
        "provenance": dict(prov),
        "defect_labels": dict(labels),
        "refusals_expected": sum(1 for t in truth if t.expected_verdict == "refuse"),
    }


def write(batch: GeneratedBatch, out_dir: Path | None = None) -> dict[str, Path]:
    """Write the three sides, then the ground truth into its isolated directory."""
    import csv

    out_dir = out_dir or cfg.GENERATED
    out_dir.mkdir(parents=True, exist_ok=True)
    truth_dir = out_dir / "_truth"
    truth_dir.mkdir(parents=True, exist_ok=True)

    p_path = out_dir / "payments.json"
    p_path.write_text(
        json.dumps([asdict(p) for p in batch.inputs.payments], indent=1, default=str),
        encoding="utf-8",
    )

    b_path = out_dir / "bank_statement.csv"
    with b_path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["txn_date", "value_date", "description", "ref_no", "debit", "credit", "balance"])
        for t in sorted(batch.inputs.bank_txns, key=lambda x: (x.txn_date, x.id)):
            wr.writerow([
                t.txn_date, t.value_date, t.narration, t.ref_no,
                paise_to_rupees(t.debit) if t.debit else "",
                paise_to_rupees(t.credit) if t.credit else "",
                paise_to_rupees(t.balance),
            ])

    i_path = out_dir / "invoices.csv"
    with i_path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow([
            "invoice_no", "customer_name", "customer_gstin", "invoice_date", "due_date",
            "gross_amount", "tds_amount", "net_receivable", "currency", "status", "po_reference",
        ])
        for inv in batch.inputs.invoices:
            wr.writerow([
                inv.invoice_no, inv.customer_name, inv.customer_gstin, inv.invoice_date,
                inv.due_date, paise_to_rupees(inv.gross_amount), paise_to_rupees(inv.tds_amount),
                paise_to_rupees(inv.net_receivable), inv.currency, inv.status, inv.po_reference,
            ])

    t_path = truth_dir / "ground_truth.json"
    t_path.write_text(
        json.dumps(
            {
                "seed": batch.inputs.seed,
                "ambiguity_bank_txn_id": batch.ambiguity_bank_txn_id,
                "stats": batch.stats,
                "links": [asdict(t) for t in batch.truth],
            },
            indent=1, default=str,
        ),
        encoding="utf-8",
    )
    return {"payments": p_path, "bank": b_path, "invoices": i_path, "truth": t_path}

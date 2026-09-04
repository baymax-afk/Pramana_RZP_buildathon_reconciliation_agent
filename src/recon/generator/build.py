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
    PayerAuthorisation,
    ReconInputs,
    TruthLink,
    date_of,
    paise_to_rupees,
)
from . import defects, fees
from .customers import BY_KEY, CONFUSABLE_PAIRS, REGISTRY, Customer, resolve

START_DATE = date(2026, 6, 1)


@dataclass(frozen=True, slots=True)
class GeneratedBatch:
    inputs: ReconInputs
    truth: tuple[TruthLink, ...]
    ambiguity_bank_txn_id: str
    stats: dict
    # Side D: the merchant's authorised-payer register. Reference data, NOT ground
    # truth -- see config.PAYER_DIRECTORY_COVERAGE for the argument, and note that it is
    # written outside `_truth/` and read only by `recon.agent`, never by the engine.
    payer_directory: tuple[PayerAuthorisation, ...] = ()


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
                amount_refunded=int(r.get("amount_refunded") or 0),
                refund_status=r.get("refund_status"),
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
# The statement's opening balance. Named because `_renumber_bank_txns` recomputes the
# whole column from it once row order is final, and the two must start from the same
# number.
OPENING_BALANCE_PAISE = 5_000_000


def _invoice_nos(p: Payment) -> tuple[str, ...]:
    """
    The invoice(s) a payment settles -- empty for a payment on account.

    Every payment used to carry an invoice number, which made tier 1 (exact reference)
    available far more often than reality allows and left the invoice-less path
    completely untested. `advance_payment` exists to fix that, and this helper is what
    stops the ten places that assumed an invoice from raising KeyError on it.
    """
    no = p.notes.get("invoice_no")
    return (no,) if no else ()


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
    # (payer, customer) pairs the third_party_payer defect created. A subset becomes
    # side D; the remainder are deliberately absent from it.
    authorisations: list[tuple[Customer, Customer]] = []
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

    balance = OPENING_BALANCE_PAISE
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
                        invoice_nos=_invoice_nos(p),
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
                # Resolve the counterparty from the name the payment actually carries,
                # and only fall back to a random one when there is no name at all.
                #
                # This previously assigned a RANDOM family to any real payment lacking
                # an explicit name_family, which produced records whose customer_name
                # and name_family disagreed -- one real payment ended up named
                # "Acme Retail Pvt Ltd" while filed under "acme_industrial", its
                # deliberately confusable twin. That is the precise confusion the
                # registry exists to test, manufactured by the generator instead of
                # injected on purpose, and it corrupts both the Fellegi-Sunter name
                # channel and any measurement made over it.
                fam = rp.notes.get("name_family")
                cust = BY_KEY.get(fam) if fam else None
                if cust is None:
                    cust = resolve(rp.notes.get("customer_name") or "")
                if cust is None:
                    cust = rng.choice(REGISTRY)
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

            # --- advance_payment: money against no invoice at all ---
            #
            # A payment on account. Ordinary -- a customer pays a retainer, or wires
            # against a proforma, or simply pays early. It matters here because EVERY
            # payment used to carry an invoice number, which made tier 1 available far
            # more often than reality allows and left the invoice-less path untested.
            # With no invoice there is no reference to quote and no TDS to deduct, so
            # the amount channel has to stand on its own.
            if rng.random() < 0.05:
                window_payments.append(
                    _synth_payment(
                        rng, pay_seq, cust, "", gross, _ts(pay_date, rng)
                    )
                )
                continue

            with_tds = rng.random() < 0.18

            # --- overpayment: the customer pays MORE than the invoice ---
            #
            # The mirror of `partial_payment`, and just as ordinary: a payer rounds up
            # to a whole rupee, or clears a small outstanding balance in the same
            # transfer. Modelled the same way and for the same reason -- the PAYMENT is
            # what changes, so payment, fee and credit still agree exactly, and the
            # invoice ends over-settled rather than open. No TDS, for the same
            # apportionment reason partial payments carry none.
            over = rng.random() < 0.05
            if over:
                with_tds = False
                inv = next_invoice(cust, gross, pay_date, with_tds=False)
                paid = gross + rng.randrange(5_000, 60_000, 100)
                invoices[:] = [
                    replace(x, status="over_settled") if x.invoice_no == inv.invoice_no else x
                    for x in invoices
                ]
                window_payments.append(
                    _synth_payment(
                        rng, pay_seq, cust, inv.invoice_no, paid, _ts(pay_date, rng)
                    )
                )
                continue

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
                    if inv.invoice_no in {
                        n for q in group for n in _invoice_nos(q)
                    }
                )
                credit = gross_net - tds_total
                if tds_total:
                    labels.append("tds_deduction")

                # refund netted inside the batch
                if rng.random() < 0.20:
                    refund = rng.randrange(5_000, 50_000, 100)
                    # Cap the refund at the payment it is recorded against: a refund
                    # cannot exceed the payment that generated it.
                    target_idx = rng.randrange(len(group))
                    target = group[target_idx]
                    refund = min(refund, target.amount - (target.fee or 0))
                    credit -= refund
                    # RECORD it on the payment, as Razorpay does. An unrecorded
                    # deduction is not a defect the engine can be expected to resolve --
                    # it is missing data, and labelling the credit "assign" while hiding
                    # the money made every refund-netted case an automatic false
                    # negative. Recorded, it becomes a known deduction like TDS and the
                    # decomposition is genuinely recoverable.
                    group[target_idx] = replace(
                        target,
                        amount_refunded=refund,
                        refund_status="partial",
                    )
                    payments = [
                        group[target_idx] if p.id == target.id else p for p in payments
                    ]
                    settleable = [
                        group[target_idx] if p.id == target.id else p
                        for p in settleable
                    ]
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
                        invoice_nos=tuple(
                            n for q in group for n in _invoice_nos(q)
                        ),
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
                (x for x in invoices if x.invoice_no == p.notes.get("invoice_no")), None
            )
            net = p.amount - (p.fee or 0)
            labels = ["mdr_fee"]
            relation = "one_to_one"
            verdict = "assign"

            if inv and inv.tds_amount:
                net -= inv.tds_amount
                labels.append("tds_deduction")

            # --- partial payment: the CUSTOMER pays less than the invoice ---
            #
            # This used to shrink the CREDIT and leave the payment at full value, so
            # Rs 7,854 vanished from a Rs 21,999 payment with nothing anywhere
            # recording where it went -- while ground truth labelled the credit
            # "assign". Against a Rs 1 tolerance that is arithmetically unmatchable, so
            # all 5 such credits were refused and every one scored as a miss. Partial
            # recall was 0/5 on every run the project has ever reported.
            #
            # It is the same defect as `refund_netted` (DEFECT_LOG 2026-09-02-05 item 4)
            # and it takes the same fix: stop hiding the money. A partial payment is not
            # a credit that disagrees with its payment -- Razorpay cannot capture
            # Rs 21,999 and settle Rs 13,573. It is a SMALLER PAYMENT against a larger
            # invoice. Payment, fee and credit now agree exactly; what is partial is the
            # INVOICE's coverage, which is why the invoice becomes `part_settled` and
            # carries a residual balance the ledger can still chase.
            #
            # Three eligibility rules, all load-bearing:
            #
            #   * SYNTHETIC PAYMENTS ONLY. An R1 record is a genuinely captured Razorpay
            #     payment whose amount, fee and tax are real API output. Rewriting one to
            #     manufacture a defect would falsify exactly the provenance claim that
            #     makes those 18 records worth having.
            #   * NO-TDS INVOICES ONLY. Apportioning TDS across a part-settlement is a
            #     separate modelling problem -- which instalment bears which deduction --
            #     and the engine reads the invoice's FULL `tds_amount`. Faking it in
            #     either direction would put a wrong number in the ledger. 84% of
            #     invoices carry no TDS, so the category stays well populated.
            #   * THE SHRUNKEN PAYMENT MUST STAY ABOVE `MIN_PAYMENT_PAISE`. That floor is
            #     what keeps TOL_ABS_PAISE 100x below the smallest payment, and config.py
            #     asserts the two against each other at import. Shrinking a payment
            #     through it would quietly invalidate the subset-sum uniqueness argument
            #     for the whole batch -- a far worse outcome than one fewer defect.
            #   * NOT ALREADY AN OVERPAYMENT. `overpayment` and `partial_payment` are
            #     contradictory by definition -- a customer cannot both under- and
            #     over-pay one invoice -- and without this guard the generator produced
            #     records carrying both labels and an invoice marked `part_settled`
            #     while ground truth called it an overpayment. Caught by a test asserting
            #     the property rather than by reading the output.
            if (
                p.provenance == "S"
                and inv is not None
                and not inv.tds_amount
                and p.amount <= inv.gross_amount
                and rng.random() < 0.08
            ):
                part_amount = int(p.amount * rng.uniform(0.35, 0.75))
                if part_amount >= cfg.MIN_PAYMENT_PAISE:
                    part_fee, part_tax = fees.fee_and_tax(part_amount)
                    p = replace(p, amount=part_amount, fee=part_fee, tax=part_tax)
                    payments = [p if q.id == p.id else q for q in payments]
                    settleable = [p if q.id == p.id else q for q in settleable]
                    inv = replace(inv, status="part_settled")
                    invoices[:] = [
                        inv if x.invoice_no == inv.invoice_no else x for x in invoices
                    ]
                    net = part_amount - part_fee
                    labels.append("partial_payment")
                    relation = "partial"

            # --- bank_charge: the RECEIVING BANK takes its own cut ---
            #
            # NEFT and RTGS handling fees are levied by the bank, so unlike MDR they
            # appear on no Razorpay object, in no ledger the merchant controls, and in
            # no narration. Rs 5-50 against a Rs 1 tolerance is arithmetically
            # unmatchable.
            #
            # So this one is labelled **refuse**, deliberately, and that is the whole
            # point of including it. An engine that widened its tolerance to absorb
            # bank charges would also start absorbing genuine coincidences, and the
            # subset-sum uniqueness argument rests on tolerance staying far below the
            # smallest payment. This defect exists to prove the engine declines the
            # case rather than swallowing it -- the money really is unaccounted for,
            # and saying so is the correct output.
            if rng.random() < 0.04:
                net -= defects.bank_charge_for(rng)
                labels.append("bank_charge")
                verdict = "refuse"

            # --- third_party_payer: a parent company settles a subsidiary's invoice ---
            #
            # The amount channel is right and the name channel is wrong, which is
            # exactly the disagreement Layer 3 must NOT resolve by vetoing a correct
            # match. Ordinary in group structures, and the reason `contradicts` requires
            # the field evidence to net negative rather than merely to disagree.
            third_party = None
            if rng.random() < 0.05:
                others = [c for c in REGISTRY if c.key != cust.key]
                if others:
                    third_party = rng.choice(others)
                    labels.append("third_party_payer")
                    # Remember the relationship the defect just created. Only a subset
                    # reaches the register (see below); the rest are the cases an
                    # investigator must decline rather than close.
                    authorisations.append((third_party, cust))

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
                        invoice_nos=_invoice_nos(p),
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

            # --- weekend_bunching: Fri/Sat/Sun all settle on Monday ---
            #
            # Banks do not post on weekends, so a run of payments bunches onto the next
            # working day and realised drift reaches three days on top of the settlement
            # window. It is the ordinary reason a lookback has to be generous, and it
            # stresses LOOKBACK_DAYS rather than any tier -- which is why it is capped
            # at the lookback rather than allowed to run past it. A credit the engine
            # provably cannot see is missing data, not a defect (DEFECT_LOG
            # 2026-09-02-08).
            if sd.weekday() >= 5:
                shifted = sd + timedelta(days=7 - sd.weekday())
                if (shifted - date_of(p.created_at)).days <= cfg.LOOKBACK_DAYS:
                    sd = shifted
                    labels.append("weekend_bunching")

            if cust.key in {a for a, _ in CONFUSABLE_PAIRS} | {b for _, b in CONFUSABLE_PAIRS}:
                labels.append("near_duplicate_name")

            # Labels read off the record as it FINALLY stands, after every mutation
            # above. Deciding them earlier let a payment be labelled `overpayment` and
            # then shrunk by the partial-payment defect, so the label described a record
            # that no longer existed. A label that can disagree with the data it
            # describes is worse than no label.
            if not p.notes.get("invoice_no"):
                labels.append("advance_payment")
            elif inv is not None and p.amount > inv.gross_amount:
                labels.append("overpayment")

            txn_seq += 1
            utr = defects.make_utr(rng)
            # Roughly 55% of payers quote the invoice number in the remittance. The
            # rest do not, so tier 1 cannot carry the whole batch and tier 2 has real
            # work to do -- which is the realistic split.
            # A payment on account has no invoice number to quote.
            quoted = (
                p.notes.get("invoice_no") if rng.random() < 0.55 else None
            )
            # ~18% of credits arrive in a shape the regex tier cannot parse. Real
            # statements contain these; a batch without them makes narration parsing
            # look solved and leaves the LLM tier with nothing to do.
            #
            # BOTH branches use `third_party or cust`. The messy branch used to ignore
            # the third party and narrate with the invoice customer, so a record could
            # be LABELLED as a name-channel disagreement while carrying no disagreement
            # at all -- measured at 2 of 7 links on the primary seed, roughly a quarter
            # of the cohort. Every number reported for `third_party_payer`, including
            # the outcome-by-defect table, was then computed over data a quarter of
            # which contradicted its own label.
            #
            # It is the same defect the comment below the partial-payment block was
            # written about: a label that can disagree with the data it describes is
            # worse than no label. A third-party payer whose narration is also messy is
            # perfectly realistic -- the two defects are independent -- so the fix is to
            # honour the payer in both branches rather than to suppress one.
            # REVIEW_2026-09-02 R3.
            payer = third_party or cust
            if rng.random() < 0.18:
                nar = defects.messy_narration(
                    rng, payer, p.notes.get("invoice_no") or ""
                )
            else:
                nar = defects.narrate(rng, utr, payer, merchant_ref=quoted)
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
                    invoice_nos=_invoice_nos(p),
                    defect_labels=tuple(labels),
                    relation=relation,  # type: ignore[arg-type]
                    expected_verdict=verdict,  # type: ignore[arg-type]
                )
            )

    # ---- split_settlement: one payment arriving as SEVERAL credits ----
    #
    # Razorpay splits a settlement for on-demand payouts and when a batch crosses a
    # limit, so one payment's net arrives as separate credits. Layer 2b resolves these
    # as a GROUP: the parts balance against the payment exactly, and neither part
    # accounts for the money on its own.
    #
    # **Three splits now, and one of them is FOUR-way on purpose.** The generator used
    # to produce two-way splits only, which meant `MAX_GROUP_CREDITS` was never
    # exercised above two -- the engine could have had a bound of two and nothing would
    # have failed. A capability nothing tests is a claim, and this project's whole
    # argument is against those. The wide split is the case that would have been refused
    # under the previous bound of three, so it fails loudly if the arity ever regresses.
    split_candidates: list[TruthLink] = []
    for link in list(truth):
        if len(split_candidates) >= 3:
            break
        if (
            link.relation == "one_to_one"
            and link.expected_verdict == "assign"
            and len(link.payment_ids) == 1
            and not ({"bank_charge", "refund_netted"} & set(link.defect_labels))
        ):
            split_candidates.append(link)

    by_txn_id = {t.id: t for t in bank_txns}
    # Two-way, two-way, four-way. The arities are fixed rather than drawn, so the batch
    # contains the wide case at every seed instead of usually.
    for arity, link in zip((2, 2, 4), split_candidates):
        original = by_txn_id.get(link.bank_txn_id)
        if original is None or original.credit < arity * cfg.MIN_PAYMENT_PAISE:
            continue
        # Uneven parts, summing to the original to the paisa. Equal parts would let a
        # matcher find the group by spotting N identical amounts, which is not the
        # evidence the layer is meant to be using.
        cuts = sorted(rng.uniform(0.15, 0.85) for _ in range(arity - 1))
        bounds = [0.0, *cuts, 1.0]
        parts = [
            int(original.credit * (bounds[i + 1] - bounds[i])) for i in range(arity)
        ]
        parts[-1] = original.credit - sum(parts[:-1])
        if any(pt < cfg.MIN_PAYMENT_PAISE // 4 for pt in parts):
            continue

        idx = bank_txns.index(original)
        setl = defects.make_settlement_id(rng)
        bank_txns[idx] = BankTxn(
            id=original.id, txn_date=original.txn_date, value_date=original.value_date,
            narration=f"RAZORPAY SETTLEMENT {setl} PART 1 OF {arity}",
            ref_no=original.ref_no, credit=parts[0], debit=0, balance=original.balance,
        )
        part_ids = [original.id]
        for n, amount in enumerate(parts[1:], start=2):
            txn_seq += 1
            part_id = f"bank_txn_{txn_seq:04d}"
            part_ids.append(part_id)
            bank_txns.append(
                BankTxn(
                    id=part_id,
                    txn_date=original.txn_date, value_date=original.value_date,
                    narration=f"RAZORPAY SETTLEMENT {setl} PART {n} OF {arity}",
                    ref_no=defects.make_utr(rng), credit=amount, debit=0,
                    balance=original.balance,
                )
            )
        # Both halves expect ASSIGN, and they did not always. Until Layer 2b existed the
        # relation was outside the engine's model -- `claimed` is a set, so a payment is
        # taken once, and no tier could ask a question a half-settlement has an answer
        # to -- and ground truth said `refuse` because refusing was the only correct
        # verdict available. That is no longer true: the claim unit is now a GROUP of
        # credits, the pair balances exactly, and expecting a refusal would score the
        # engine down for doing the work.
        #
        # Each half names the SAME payment set, which is what the relation is. The
        # scorer credits a split link when the credit was settled inside a group whose
        # payments match, so the two links agree with one group rather than demanding
        # two assignments that would double-post the payment.
        labels = tuple(link.defect_labels) + ("split_settlement",)
        if arity >= 4:
            # Named separately so the outcome-by-defect table can show the wide case on
            # its own. Lumped in with the two-way splits it would be invisible: a table
            # reading "split_settlement 6 matched" says nothing about whether any of
            # them had more than two parts.
            labels += ("split_settlement_wide",)
        truth[truth.index(link)] = TruthLink(
            link.bank_txn_id, link.payment_ids, link.invoice_nos, labels,
            "split", "assign",
        )
        for part_id in part_ids[1:]:
            truth.append(
                TruthLink(part_id, link.payment_ids, link.invoice_nos, labels,
                          "split", "assign")
            )

    # ---- chargeback_debit: money leaving, on a line the engine never reads ----
    #
    # A settled payment is disputed and clawed back. The debit is a real bank line
    # carrying a real reference, and the engine reads `is_credit` transactions only --
    # so the money leaving is not matched, not refused, and not counted anywhere.
    #
    # The statement contained ZERO debits before this defect existed, which is why the
    # blind spot went unnoticed: the engine had never been shown the half of a bank
    # statement it ignores.
    #
    # **A truth link IS created now, and the reason it was withheld is the reason it can
    # be created.** The original note read: "Inventing one would score the engine against
    # a verdict it structurally cannot produce -- a permanent miss that no engine work
    # could ever close, which is scoring theatre rather than measurement." That argument
    # was correct and it was conditional. The engine now reads debits and ties each to
    # the settlement it reverses, so `reverse` is a verdict it CAN produce, and withholding
    # the label would now hide real work instead of avoiding a fake miss.
    #
    # The link carries the reversed payment, so a reversal posted against the wrong
    # settlement scores as an error rather than passing unexamined.
    for link in list(truth)[:]:
        if link.relation != "one_to_one" or link.expected_verdict != "assign":
            continue
        if rng.random() >= 0.05:
            continue
        original = by_txn_id.get(link.bank_txn_id)
        if original is None:
            continue
        txn_seq += 1
        cb_date = date.fromisoformat(original.txn_date) + timedelta(days=rng.randint(3, 9))
        balance -= original.credit
        debit_id = f"bank_txn_{txn_seq:04d}"
        bank_txns.append(
            BankTxn(
                id=debit_id,
                txn_date=cb_date.isoformat(), value_date=cb_date.isoformat(),
                narration=f"CHARGEBACK REV {original.ref_no} DISPUTE",
                ref_no=original.ref_no, credit=0, debit=original.credit,
                balance=balance,
            )
        )
        truth.append(
            TruthLink(
                bank_txn_id=debit_id,
                payment_ids=link.payment_ids,
                invoice_nos=link.invoice_nos,
                # ONLY `chargeback_debit`, not the settlement's labels. A defect label
                # says what makes THIS line hard, and the claw-back inherits none of
                # what made the original credit hard -- it is a different line, on a
                # different date, carrying a different narration. Inheriting them said
                # a debit dated the following Sunday was `weekend_bunching`, and the
                # generator's own well-formedness checks then tested a chargeback
                # against the rules for a settlement credit and failed it.
                defect_labels=("chargeback_debit",),
                relation="reversal",
                expected_verdict="reverse",
            )
        )

    # ---- partial_chargeback: one payment disputed inside a settlement batch ----
    #
    # A chargeback is raised against a TRANSACTION, and a settlement batch covers
    # several. Disputing one payment out of four produces a debit for that payment's
    # settled contribution, carrying the batch's reference -- not a debit for the whole
    # credit. The engine's first reversal ledger required `debit == credit` exactly and
    # reported every one of these as an unexplained debit.
    #
    # Built from a `many_to_one` batch with NO netted refund, because a refund is
    # deducted from the batch total without being attributable to a single payment's
    # settled amount, so the arithmetic for one payment's share would not close. That is
    # a real compound case and a different defect; conflating the two here would make
    # this one unsatisfiable rather than hard.
    inv_by_no = {i.invoice_no: i for i in invoices}
    pay_by_id = {p.id: p for p in payments}
    partial_targets = [
        l for l in truth
        if l.relation == "many_to_one"
        and l.expected_verdict == "assign"
        and len(l.payment_ids) >= 3
        and "refund_netted" not in l.defect_labels
        and l.bank_txn_id in by_txn_id
    ]
    for link in partial_targets[:2]:
        original = by_txn_id[link.bank_txn_id]
        disputed = pay_by_id.get(link.payment_ids[0])
        if disputed is None or disputed.fee is None:
            continue
        # What that one payment actually settled for: gross less the gateway fee, less
        # any TDS its own invoice carried. The same arithmetic the batch total was built
        # from, applied to one member of it.
        tds = sum(
            inv.tds_amount
            for no in _invoice_nos(disputed)
            if (inv := inv_by_no.get(no)) is not None
        )
        amount = disputed.amount - disputed.fee - tds
        if amount <= 0 or amount >= original.credit:
            continue
        txn_seq += 1
        cb_date = date.fromisoformat(original.txn_date) + timedelta(days=rng.randint(4, 11))
        balance -= amount
        debit_id = f"bank_txn_{txn_seq:04d}"
        bank_txns.append(
            BankTxn(
                id=debit_id,
                txn_date=cb_date.isoformat(), value_date=cb_date.isoformat(),
                narration=f"CHARGEBACK REV {original.ref_no} DISPUTE PARTIAL",
                ref_no=original.ref_no, credit=0, debit=amount,
                balance=balance,
            )
        )
        truth.append(
            TruthLink(
                bank_txn_id=debit_id,
                # ONE payment, not the batch. A reversal link naming the whole batch
                # would score the engine correct for clawing back four receivables when
                # one was disputed -- the opposite of what the line says.
                payment_ids=(disputed.id,),
                invoice_nos=tuple(_invoice_nos(disputed)),
                defect_labels=("chargeback_debit", "partial_chargeback"),
                relation="reversal",
                expected_verdict="reverse",
            )
        )

    # ---- chargeback_out_of_batch: a claw-back on an EARLIER statement ----
    #
    # A statement period is a window, not the world. A settlement made last month can be
    # disputed this month, and the debit lands here carrying a reference to a credit this
    # batch does not contain. Real money, unreconcilable in this batch by construction --
    # and the correct output is not silence but a classification: "this reverses a
    # settlement outside this batch, go to the prior period".
    #
    # Ground truth expects `refuse`, not `reverse`. There is nothing here to tie it to,
    # so asserting `reverse` would be the automatic-false-negative shape this project has
    # shipped three times. What IS asserted is that the engine declines to tie it AND
    # says why -- `tests/test_new_defects.py` checks the category, not just the decline.
    txn_seq += 1
    orphan_date = START_DATE + timedelta(days=rng.randint(20, 40))
    orphan_amount = rng.randrange(50_000, 400_000, 100)
    balance -= orphan_amount
    orphan_id = f"bank_txn_{txn_seq:04d}"
    orphan_ref = defects.make_utr(rng)
    bank_txns.append(
        BankTxn(
            id=orphan_id,
            txn_date=orphan_date.isoformat(), value_date=orphan_date.isoformat(),
            narration=f"CHARGEBACK REV {orphan_ref} DISPUTE",
            ref_no=orphan_ref, credit=0, debit=orphan_amount, balance=balance,
        )
    )
    truth.append(
        TruthLink(
            bank_txn_id=orphan_id,
            payment_ids=(),
            invoice_nos=(),
            defect_labels=("chargeback_debit", "chargeback_out_of_batch"),
            relation="reversal",
            expected_verdict="refuse",
        )
    )

    # ---- chargeback_on_refused: the settlement it reverses was NOT posted ----
    #
    # The reference resolves to a credit that is right here in the batch -- and the
    # engine refused to post it. The debit cannot be resolved either: using a claw-back
    # to decide which decomposition was right would let a later event pick between
    # candidates the evidence did not separate, which is the engine declining a match and
    # then making it anyway through a side door.
    #
    # What the engine CAN say is that the two are linked, which is the first dependency
    # between exceptions this project has: clear the credit and the debit clears with it.
    refused_credit = next(
        (
            by_txn_id[l.bank_txn_id]
            for l in truth
            if l.expected_verdict == "refuse"
            and l.relation != "reversal"
            and l.bank_txn_id in by_txn_id
            and by_txn_id[l.bank_txn_id].is_credit
        ),
        None,
    )
    if refused_credit is not None:
        txn_seq += 1
        dep_date = date.fromisoformat(refused_credit.txn_date) + timedelta(
            days=rng.randint(3, 9)
        )
        balance -= refused_credit.credit
        dep_id = f"bank_txn_{txn_seq:04d}"
        bank_txns.append(
            BankTxn(
                id=dep_id,
                txn_date=dep_date.isoformat(), value_date=dep_date.isoformat(),
                narration=f"CHARGEBACK REV {refused_credit.ref_no} DISPUTE",
                ref_no=refused_credit.ref_no, credit=0,
                debit=refused_credit.credit, balance=balance,
            )
        )
        truth.append(
            TruthLink(
                bank_txn_id=dep_id,
                payment_ids=(),
                invoice_nos=(),
                defect_labels=("chargeback_debit", "chargeback_on_refused"),
                relation="reversal",
                expected_verdict="refuse",
            )
        )

    # ---- duplicate UTR: clone one existing credit's reference onto another ----
    #
    # The overwritten credit (`b`) must not be one a REVERSAL link depends on. A
    # chargeback identifies its settlement by carrying that settlement's reference, so
    # clobbering the reference destroys the evidence path and makes a link marked
    # `reverse` unsatisfiable at any tolerance -- the automatic-false-negative shape this
    # project has shipped three times, arriving a fourth way.
    #
    # Found by adding a defect, not by reading: `chargeback_out_of_batch` shifted the RNG
    # stream, this defect landed on a different credit, and a partial chargeback that had
    # been resolving started reporting as out-of-batch. `assert_truth_is_satisfiable` now
    # checks reversal links too, so the next one fails the build instead of the metrics.
    # Rebuilt here rather than reusing `by_txn_id` from further up: debits have been
    # appended since that map was made, and a stale map would miss exactly the reversal
    # links this guard exists for.
    txn_now = {t.id: t for t in bank_txns}
    reversal_refs = {
        txn_now[l.bank_txn_id].ref_no
        for l in truth
        if l.expected_verdict == "reverse" and l.bank_txn_id in txn_now
    }
    credits = [
        t for t in bank_txns if t.is_credit and t.ref_no not in reversal_refs
    ]
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

    # ---- renumber bank transactions to match on-disk order ----
    bank_txns, truth = _renumber_bank_txns(bank_txns, truth)

    inputs = ReconInputs(
        payments=tuple(payments),
        bank_txns=tuple(bank_txns),
        invoices=tuple(invoices),
        seed=seed,
        payments_per_window=payments_per_window,
    )
    stats = _summarise(inputs, truth, n_windows, payments_per_window)
    directory = _build_payer_directory(rng, authorisations)
    stats["payer_directory"] = {
        "relationships_created": len(authorisations),
        "on_the_register": sum(
            1 for a in directory if a.relationship != "affiliate_decoy"
        ),
        "decoys": sum(1 for a in directory if a.relationship == "affiliate_decoy"),
    }
    return GeneratedBatch(
        inputs, tuple(truth), _ambiguity_id(truth), stats, directory
    )


def _build_payer_directory(
    rng: random.Random, authorisations: list[tuple[Customer, Customer]]
) -> tuple[PayerAuthorisation, ...]:
    """
    Side D: the merchant's authorised-payer register, deliberately incomplete.

    **Coverage is partial on purpose.** A register naming every relationship the
    generator created would turn `third_party_payer` into a lookup, and an agent that
    closes every case because the answer sat in a file demonstrates nothing about
    investigation. At `PAYER_DIRECTORY_COVERAGE` the investigator closes what the
    evidence supports and must report "insufficient evidence" on the rest -- which is
    the behaviour worth putting in front of a judge.

    **Decoys are included.** Entries naming relationships that appear nowhere in this
    batch, so a register hit is not self-evidently a match and a lookup that fires still
    has to survive conservation, uniqueness, the narration count and any contest.

    Sorted by payer name so the file is stable for a given seed. Deduplicated because
    the same pair can be drawn twice across 200 payments, and a register listing a
    relationship twice would inflate the coverage figure this docstring cites.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[Customer, Customer]] = []
    for payer, cust in authorisations:
        key = (payer.canonical_name, cust.canonical_name)
        if key not in seen:
            seen.add(key)
            unique.append((payer, cust))

    n_keep = int(len(unique) * cfg.PAYER_DIRECTORY_COVERAGE)
    kept = rng.sample(unique, n_keep) if n_keep else []

    rows = [
        PayerAuthorisation(
            payer_name=payer.canonical_name,
            authorised_for_customer=cust.canonical_name,
            relationship=rng.choice(("parent", "group_treasury", "affiliate")),
            on_record_since="2025-04-01",
        )
        for payer, cust in kept
    ]

    # Decoys: real-looking relationships between registry entities that this batch's
    # third_party_payer defect never actually used.
    used = {(p.canonical_name, c.canonical_name) for p, c in unique}
    pool = [
        (a, b)
        for a in REGISTRY
        for b in REGISTRY
        if a.key != b.key and (a.canonical_name, b.canonical_name) not in used
    ]
    for payer, cust in rng.sample(pool, min(cfg.PAYER_DIRECTORY_DECOYS, len(pool))):
        rows.append(
            PayerAuthorisation(
                payer_name=payer.canonical_name,
                authorised_for_customer=cust.canonical_name,
                relationship="affiliate_decoy",
                on_record_since="2025-04-01",
            )
        )

    return tuple(sorted(rows, key=lambda r: (r.payer_name, r.authorised_for_customer)))


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

    The repair moves any such interloper out of the credit's lookback.

    Raising the amount instead would cascade: the payment's invoice, its net, and the
    bank credit derived from that net would all have to be rebuilt. Moving the date
    touches one field and nothing downstream of it.

    **The move must respect the payment's OWN credit, and it used to not.** This
    function shifted every interloper `lookback + 1` = 6 days later, on the stated
    grounds that "its own settlement credit is dated from its window's settle date,
    which is strictly later, so the shift cannot orphan it". That reasoning is wrong,
    and the arithmetic says so plainly: a payment sits at most 2 days into its window
    and its credit lands at settle date plus 0-2 days drift, so its own credit is at
    most 5 days away -- and the shift is 6. Moving it forward pushed it PAST the credit
    that settles it, out of that credit's lookback entirely, leaving ground truth
    asserting a link the engine cannot satisfy at any tolerance.

    Measured across 40 seeds: **5 of them (12.5%) shipped an orphaned payment.** The
    primary reported seed was not one of them, which is the only reason this survived.
    An orphaned link is an automatic false negative -- the same failure shape as
    `refund_netted` (2026-09-02-05) and `partial_payment` (2026-09-02-08), where ground
    truth asserts a match the data cannot support.

    So the move is now computed against BOTH constraints: outside the ambiguity credit's
    lookback, and still inside its own credit's. Forward if the payment's own credit is
    late enough to allow it, backward otherwise. If neither direction has room the build
    FAILS, because there is no honest date for that payment and emitting the batch
    anyway would ship the very defect this function exists to prevent.
    """
    credit = next((t for t in bank_txns if t.id == amb_id), None)
    if credit is None:
        return payments
    link = next((t for t in truth if t.bank_txn_id == amb_id), None)
    crafted = set(link.payment_ids) if link else set()
    cd = date.fromisoformat(credit.txn_date)
    # Read from cfg.LOOKBACK_DAYS and widen explicitly. This used to recompute
    # `SETTLEMENT_WINDOW_DAYS + 2` with a comment claiming it was "deliberately wider
    # than the engine's rule" -- but cfg.LOOKBACK_DAYS is SETTLEMENT_WINDOW_DAYS +
    # MAX_SETTLEMENT_DRIFT_DAYS, which is the same 5, so the stated margin was zero.
    #
    # That is a guard that fails SILENTLY. Raise MAX_SETTLEMENT_DRIFT_DAYS to 3 -- a
    # one-line config change this file's own header invites -- and the engine's
    # candidate window widens to 6 while this scan stays at 5: an interloper at lag 6 is
    # neither detected nor relocated, the post-condition below misses it for the same
    # reason, and the centrepiece ambiguity guarantee fails with nothing raising.
    # REVIEW_2026-09-02 R9.
    lookback = cfg.LOOKBACK_DAYS + cfg.AMBIGUITY_GUARD_MARGIN_DAYS

    # Each payment's OWN settling credit, so a shift can be checked against it rather
    # than assumed safe. A payment with no credit (unsettled, or a truth link that
    # expects a refusal) has no such constraint and may move freely.
    txn_date_by_id = {t.id: date.fromisoformat(t.txn_date) for t in bank_txns}
    own_credit: dict[str, date] = {}
    for tl in truth:
        d = txn_date_by_id.get(tl.bank_txn_id)
        if d is None:
            continue
        for pid in tl.payment_ids:
            own_credit[pid] = d

    def _relocate(p: Payment) -> Payment:
        """
        Move p out of the ambiguity credit's lookback without orphaning it.

        **Every legal date is considered, not two guesses.** This used to try exactly
        `cd + 1` and `cd - lookback - 1` and give up if neither worked, which is a
        needlessly narrow search: the payment must land outside the protected band
        AND inside its own credit's candidate window, and that usually leaves a range
        of valid days rather than a single one. Giving up after two made the generator
        fail on batches that were perfectly constructible -- seed 11111 among them, one
        of the sweep's own seeds, the moment the guard's scan widened.

        Among the legal dates, the one nearest the payment's current date is chosen, so
        the repair perturbs the batch as little as it can.
        """
        current = date_of(p.created_at)
        ocd = own_credit.get(p.id)

        protected = {cd - timedelta(days=n) for n in range(lookback + 1)}

        if ocd is None:
            # No settling credit, so the only constraint is the protected band.
            candidates = [cd + timedelta(days=1)]
        else:
            # Every day inside its own credit's candidate window.
            candidates = [
                ocd - timedelta(days=n) for n in range(cfg.LOOKBACK_DAYS + 1)
            ]

        legal = sorted(
            (d for d in candidates if d not in protected),
            key=lambda d: (abs((d - current).days), d.isoformat()),
        )
        if legal:
            return replace(
                p, created_at=p.created_at + 86_400 * (legal[0] - current).days
            )

        raise AssertionError(
            f"Ambiguity guarantee cannot be enforced without orphaning {p.id}: it nets "
            f"{p.amount - (p.fee or 0)}p inside the ambiguity credit's protected band "
            f"({cd} back {lookback} days), and every day of its own credit's "
            f"{cfg.LOOKBACK_DAYS}-day window (ending {ocd}) falls inside that band. "
            f"Generation fails rather than emit a truth link the engine cannot satisfy."
        )

    out: list[Payment] = []
    for p in payments:
        if (
            p.captured
            and p.fee is not None
            and p.id not in crafted
            and 0 <= (cd - date_of(p.created_at)).days <= lookback
            and (p.amount - p.fee) <= cfg.AMBIGUITY_CREDIT_PAISE
        ):
            out.append(_relocate(p))
        else:
            out.append(p)

    # Post-condition: no interloper may remain. If one does, the invariant is broken
    # in a way the shift cannot fix and generation must fail rather than emit a
    # centrepiece case that only appears to hold.
    for p in out:
        if p.captured and p.fee is not None and p.id not in crafted:
            lag = (cd - date_of(p.created_at)).days
            if 0 <= lag <= lookback and (p.amount - p.fee) <= cfg.AMBIGUITY_CREDIT_PAISE:
                raise AssertionError(
                    f"Ambiguity guarantee cannot be enforced: {p.id} nets "
                    f"{p.amount - p.fee}p at lag {lag} from the ambiguity credit and "
                    f"could join a subset reaching {cfg.AMBIGUITY_CREDIT_PAISE}p."
                )
    return out


def _renumber_bank_txns(
    bank_txns: list[BankTxn], truth: list[TruthLink]
) -> tuple[list[BankTxn], list[TruthLink]]:
    """
    Sort the statement by date and reassign ids by final position.

    A real bank statement arrives in date order and carries no internal row id, so the
    loader derives one from position in the file. That only works if the generator's
    ids agree with the file's final ordering -- otherwise every ground-truth reference
    points at the wrong row and scoring silently compares unrelated records.

    This is a genuinely nasty class of bug: nothing raises, the engine runs fine, and
    precision comes out near zero for a reason that looks like a matcher failure. So
    the two orderings are reconciled once, here, at the point where both are known,
    and `tests/test_engine_tiers.py` asserts the round trip.
    """
    ordered = sorted(bank_txns, key=lambda t: (t.txn_date, t.id))
    remap = {t.id: f"bank_txn_{i:04d}" for i, t in enumerate(ordered, start=1)}

    # Recompute the running balance AFTER sorting, because only now is the row order
    # final. Rows were previously stamped with whatever the balance happened to be when
    # they were appended, and two defects then made that incoherent: a split settlement
    # wrote the SAME pre-split balance on both halves, and a chargeback debit was
    # stamped with an end-of-generation total before being sorted into the middle of the
    # statement. The emitted column was non-monotonic and arithmetically wrong.
    #
    # Nothing reads it today, which is exactly why it would have been believed later.
    # The generator's stated contract is a realistic Indian bank export, and a balance
    # column that does not add up is the first thing a finance reader would check.
    balance = OPENING_BALANCE_PAISE
    renumbered = []
    for t in ordered:
        balance += t.credit - t.debit
        renumbered.append(replace(t, id=remap[t.id], balance=balance))
    retruth = [
        replace(link, bank_txn_id=remap.get(link.bank_txn_id, link.bank_txn_id))
        for link in truth
    ]
    return renumbered, retruth


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
    pd_ = date_of(p.created_at)
    return timedelta(days=0) <= (cd - pd_) <= timedelta(days=cfg.SETTLEMENT_WINDOW_DAYS + 2)


def assert_tolerance_sanity(batch: GeneratedBatch) -> tuple[int, int]:
    """
    Assert the matching tolerance stays far below the smallest payment -- at EVERY
    credit size, not just at the absolute floor.

    If tolerance approaches the smallest payment in a candidate pool, then a subset S
    and the subset S plus one small payment BOTH satisfy the constraint. Every
    many-to-one result becomes meaningless and the uniqueness test degrades into noise
    while still reporting a single confident answer.

    The check evaluates the tolerance actually applied to the LARGEST credit in the
    batch. Checking `TOL_ABS_PAISE` alone was the original error: with a relative term
    the effective tolerance grows with the credit, so the constant looked safe at 209x
    while the real figure on a large settlement was 9.7x. A guarantee that holds only
    for small transactions is not the guarantee this engine claims.
    """
    from ..engine import fees as engine_fees

    nets = [
        p.amount - p.fee
        for p in batch.inputs.payments
        if p.captured and p.fee is not None
    ]
    smallest = min(nets)
    largest_credit = max((t.credit for t in batch.inputs.bank_txns), default=0)
    worst_tol = engine_fees.tolerance_for(largest_credit)
    if worst_tol * 100 > smallest:
        raise AssertionError(
            f"Tolerance sanity violated: at the largest credit ({largest_credit}p) the "
            f"effective tolerance is {worst_tol}p, only {smallest / worst_tol:.1f}x "
            f"below the smallest net payment ({smallest}p). A 100x margin is required "
            f"or subset-sum uniqueness cannot be trusted."
        )
    return worst_tol, smallest


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
            by_day[date_of(p.created_at).isoformat()] += 1
    worst = max(
        (
            sum(
                v for d, v in by_day.items()
                if 0 <= (date.fromisoformat(c.txn_date) - date.fromisoformat(d)).days
                <= cfg.LOOKBACK_DAYS
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


def assert_truth_is_satisfiable(batch: GeneratedBatch) -> int:
    """
    Every link ground truth says to ASSIGN must be one the engine could actually reach.

    Two ways a generator can assert an impossible match, and this project has now shipped
    both:

      * **Hide the money.** `refund_netted` deducted a refund from a credit and recorded
        it nowhere; `partial_payment` shrank a credit and left the payment at full value.
        Either way the arithmetic cannot close at any tolerance.
      * **Move the payment out of reach.** `_protect_ambiguity_window` shifted an
        interloper 6 days later while its own credit was at most 5 days away, pushing it
        outside that credit's lookback. Measured before the fix: **5 of 40 seeds** shipped
        one.
      * **Contradict the narration.** A settlement narration states how many transactions
        it covers, and `match.py`'s `count_conflict` now REFUSES any credit whose stated
        count disagrees with the decomposition that fits. So a link marked `assign` over
        k payments on a credit narrated with a different count is unsatisfiable at any
        tolerance -- a third shape, added to this check because it held only by
        construction and nothing asserted it (REVIEW_2026-09-02 R10).

    Both produce an automatic false negative that looks like an engine failure. The
    engine refuses -- correctly, on the evidence it was given -- and the scorer records a
    miss, so the defect presents as a coverage problem in the matcher and gets
    investigated there. `partial` recall sat at 0/5 for the entire life of the project
    for exactly this reason.

    So the check is structural rather than per-defect: if truth says assign, the payments
    must be inside the credit's candidate window and the credit must be reachable from
    their settled interval. It fails the build; it does not warn.
    """
    from ..engine import fees as engine_fees, tier2_amount_date as t2
    from ..engine.normalize import parse

    pay = {p.id: p for p in batch.inputs.payments}
    txn = {t.id: t for t in batch.inputs.bank_txns}
    invoices_by_no = {i.invoice_no: i for i in batch.inputs.invoices}
    checked = 0
    problems: list[str] = []

    # Split links are checked as a GROUP, below, and skipped here. Neither half of a
    # part-settlement balances against the payment on its own -- that is the whole of
    # what makes it a split -- so running the per-credit test over one half would report
    # the relation as unsatisfiable precisely when the generator has built it correctly.
    split_by_payments: dict[tuple[str, ...], list[str]] = {}
    for link in batch.truth:
        if link.relation == "split" and link.expected_verdict == "assign":
            split_by_payments.setdefault(link.payment_ids, []).append(link.bank_txn_id)

    for link in batch.truth:
        if link.expected_verdict != "assign" or not link.bank_txn_id:
            continue
        if link.relation == "split":
            continue
        t = txn.get(link.bank_txn_id)
        if t is None:
            continue
        checked += 1
        lo, hi = t2.window_for(t)

        for pid in link.payment_ids:
            p = pay.get(pid)
            if p is None:
                problems.append(f"{link.bank_txn_id}: payment {pid} does not exist")
                continue
            d = t2.payment_date(p)
            if not (lo <= d <= hi):
                problems.append(
                    f"{link.bank_txn_id} (credit {t.txn_date}): payment {pid} is dated "
                    f"{d}, outside the credit's window {lo}..{hi} -- the engine can "
                    f"never see it, so this link is an automatic false negative"
                )

        group = [pay[pid] for pid in link.payment_ids if pid in pay]
        if not group:
            continue

        # The narration's own transaction count must agree with the size of the
        # decomposition ground truth asserts, or the engine refuses it outright.
        stated = parse(t.narration).txn_count
        if stated is not None and stated != len(link.payment_ids):
            problems.append(
                f"{link.bank_txn_id}: the narration states {stated} transaction(s) but "
                f"ground truth assigns {len(link.payment_ids)} payment(s) -- the engine "
                f"refuses this outright (narration_count_conflict), so the link is "
                f"unsatisfiable at any tolerance"
            )
        interval = engine_fees.expected_credit_interval(group, invoices_by_no)
        tol = engine_fees.tolerance_for(t.credit)
        if not (interval.lo - tol <= t.credit <= interval.hi + tol):
            short = interval.lo - t.credit
            problems.append(
                f"{link.bank_txn_id}: credit {t.credit}p is outside the settled "
                f"interval [{interval.lo}, {interval.hi}]p of its {len(group)} "
                f"payment(s) at tolerance {tol}p (off by {short:+d}p) -- money is "
                f"unaccounted for, so no tolerance could close this"
            )

    # ---- split settlements, checked at the level they close at ---------------
    #
    # A part-settlement is satisfiable when its credits SUM into the payment set's
    # settled interval, every credit is a real credit, and at least two of them exist --
    # a "split" of one is a one-to-one link mislabelled, and would be assigned by the
    # ordinary matcher while ground truth waited for a group that never comes.
    for payment_ids, txn_ids in sorted(split_by_payments.items()):
        checked += 1
        if len(txn_ids) < 2:
            problems.append(
                f"{txn_ids[0]}: ground truth marks this a split settlement but names "
                f"only one credit for {payment_ids} -- the engine's group resolver needs "
                f"at least two, and the ordinary matcher will take it as one-to-one"
            )
            continue
        members = [txn.get(t) for t in txn_ids]
        if any(m is None or not m.is_credit for m in members):
            problems.append(
                f"{'+'.join(txn_ids)}: a split settlement names a transaction that is "
                f"not a credit in this batch"
            )
            continue
        group = [pay[pid] for pid in payment_ids if pid in pay]
        if not group:
            continue
        total = sum(m.credit for m in members if m is not None)
        interval = engine_fees.expected_credit_interval(group, invoices_by_no)
        tol = engine_fees.tolerance_for(total)
        if not (interval.lo - tol <= total <= interval.hi + tol):
            problems.append(
                f"{'+'.join(txn_ids)}: the split's credits sum to {total}p, outside the "
                f"settled interval [{interval.lo}, {interval.hi}]p of its {len(group)} "
                f"payment(s) at tolerance {tol}p -- money is unaccounted for across the "
                f"group, so no grouping could close this"
            )

    # ---- reversals: the evidence path must still exist -----------------------
    #
    # A chargeback identifies the settlement it reverses by carrying that settlement's
    # reference. If nothing in the batch answers to that reference any more -- because a
    # later defect overwrote it -- then a link marked `reverse` is unsatisfiable at any
    # tolerance, and the engine will correctly report the debit as reversing something
    # outside this batch while the scorer records a miss.
    #
    # This check was added AFTER that happened. `chargeback_out_of_batch` shifted the RNG
    # stream, the duplicate-UTR defect landed on a different credit, and a partial
    # chargeback that had been resolving silently stopped. Four times now the generator
    # has destroyed something and the engine has been scored for it; this is the fourth
    # shape, and it is checked rather than remembered.
    credit_refs = {t.ref_no for t in batch.inputs.bank_txns if t.is_credit and t.ref_no}
    for link in batch.truth:
        if link.expected_verdict != "reverse" or not link.bank_txn_id:
            continue
        checked += 1
        d = txn.get(link.bank_txn_id)
        if d is None:
            continue
        if d.is_credit:
            problems.append(
                f"{link.bank_txn_id}: ground truth marks this a reversal, but it is a "
                f"CREDIT -- money arriving cannot reverse a settlement"
            )
            continue
        if d.ref_no not in credit_refs:
            problems.append(
                f"{link.bank_txn_id}: ground truth says this debit reverses a settlement "
                f"in this batch, but its reference {d.ref_no!r} matches no credit here. "
                f"The evidence path is gone, so the engine can only report it as "
                f"out-of-batch and the link is unsatisfiable"
            )

    if problems:
        raise AssertionError(
            f"Ground truth asserts {len(problems)} link(s) the engine cannot satisfy:\n  "
            + "\n  ".join(problems[:10])
            + (f"\n  ... and {len(problems) - 10} more" if len(problems) > 10 else "")
        )
    return checked


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

    # A manifest OUTSIDE the truth directory, recording which seed and density produced
    # these three sides.
    #
    # The seed was previously written only into `_truth/ground_truth.json`, which the
    # engine may not read -- so nothing on the engine side could tell which batch was on
    # disk. `run.py match --seed 77771` does not regenerate; it loads whatever is there
    # and stamps the seed it was handed onto ReconInputs, so a batch built at seed
    # 20260905 would be matched, scored and PRINTED as "seed=77771". The headline block
    # named a seed that did not produce its numbers, which in a project whose argument is
    # reproducibility is not a cosmetic problem.
    #
    # The manifest is input metadata, not an answer key -- it says how the data was made,
    # never what the right answer is -- so it belongs on the engine's side of the boundary.
    m_path = out_dir / "manifest.json"
    m_path.write_text(
        json.dumps(
            {
                "seed": batch.inputs.seed,
                "payments_per_window": batch.inputs.payments_per_window,
                "payments": len(batch.inputs.payments),
                "bank_txns": len(batch.inputs.bank_txns),
                "invoices": len(batch.inputs.invoices),
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    # Side D. Written OUTSIDE `_truth/`, deliberately: it is reference data a merchant
    # already owns, not an answer key. See config.PAYER_DIRECTORY_COVERAGE.
    d_path = out_dir / "payer_directory.csv"
    with d_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["payer_name", "authorised_for_customer", "relationship", "on_record_since"]
        )
        for row in batch.payer_directory:
            w.writerow(
                [
                    row.payer_name,
                    row.authorised_for_customer,
                    row.relationship,
                    row.on_record_since,
                ]
            )

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
    return {
        "payments": p_path, "bank": b_path, "invoices": i_path,
        "payer_directory": d_path,
        "manifest": m_path, "truth": t_path,
    }

"""
The bank behind a statement line, DERIVED from its reference -- and labelled as derived.

**There is no bank column.** `data/generated/bank_statement.csv` carries
`txn_date,value_date,description,ref_no,debit,credit,balance` and nothing else, because
that is what an Indian bank statement export actually contains: the account holder knows
which bank sent them their own statement, so the file does not repeat it per line. The
institution is still recoverable, because `ref_no` is UTR-shaped -- an IFSC bank code
followed by a numeric run -- and the payment side carries the same codes in
`Payment.bank`.

**So this module infers, and every value it returns says so.** `provenance` is not
decoration: a page that prints "ICICI Bank" beside a settlement is making a claim, and
the honest version of that claim is "ICICI Bank, read off the transaction reference"
rather than "ICICI Bank, per the bank". The distinction matters on exactly the lines
where it is wrong -- a correspondent bank's UTR names the correspondent, not the
originator -- and a reader who knows the number was inferred can discount it.

**An unknown prefix returns the prefix.** Not "Unknown", not a best guess at the nearest
code, and not blank. The four characters are what is known; rendering them lets a human
recognise a bank this table has not been taught, and keeps the failure visible instead of
uniform. Widening the table is a code change with a test, which is the point -- the same
argument `agent/schemas.py` makes for its evidence enum.
"""

from __future__ import annotations

import re

# IFSC bank code -> the name a person would recognise. Codes are the first four
# characters of an IFSC and are assigned by RBI, so this is a lookup rather than a
# heuristic. Axis appears twice on purpose: UTIB is its allotted code and AXIS is the
# alias that shows up in remittance references.
_BANK_OF_IFSC: dict[str, str] = {
    "AXIS": "Axis Bank",
    "UTIB": "Axis Bank",
    "HDFC": "HDFC Bank",
    "ICIC": "ICICI Bank",
    "KKBK": "Kotak Mahindra Bank",
    "PUNB": "Punjab National Bank",
    "SBIN": "State Bank of India",
    "BARB": "Bank of Baroda",
    "CNRB": "Canara Bank",
    "IBKL": "IDBI Bank",
    "UBIN": "Union Bank of India",
    "UTBI": "United Bank of India",
    "DEUT": "Deutsche Bank",
    "KVBL": "Karur Vysya Bank",
    "YESB": "Yes Bank",
    "INDB": "IndusInd Bank",
    "IDFB": "IDFC First Bank",
    "FDRL": "Federal Bank",
}

DERIVED = "derived from the transaction reference"
UNKNOWN = "unrecognised bank code"
ABSENT = "no transaction reference on this line"

# A leading run of letters is the candidate code. Anchored, because a code that is not at
# the start of the reference is not an IFSC prefix and guessing from the middle of a
# string is how a lookup starts inventing banks.
_LEADING_ALPHA = re.compile(r"^([A-Za-z]{4})")


def bank_of_reference(ref_no: str | None) -> tuple[str, str]:
    """
    (display name, provenance) for one statement line's reference.

    Three outcomes, all of them honest about how much is known: a recognised code returns
    its bank; an unrecognised one returns the four characters themselves; an absent
    reference returns an empty name rather than a placeholder that would sort and filter
    as though it were a bank.
    """
    ref = (ref_no or "").strip()
    if not ref:
        return "", ABSENT
    m = _LEADING_ALPHA.match(ref)
    if m is None:
        return "", ABSENT
    code = m.group(1).upper()
    name = _BANK_OF_IFSC.get(code)
    if name is None:
        return code, UNKNOWN
    return name, DERIVED


def bank_of_payment_code(bank: str | None) -> tuple[str, str]:
    """
    The same lookup against Razorpay's `Payment.bank`, which carries codes like
    `PUNB_R` -- the IFSC code with the gateway's own suffix. Split rather than
    special-cased, so a new suffix does not need a new branch.
    """
    code = (bank or "").strip().split("_", 1)[0]
    if not code:
        return "", ABSENT
    return bank_of_reference(code)


def known_codes() -> tuple[str, ...]:
    """Every code this module recognises. Used by the test that pins the table."""
    return tuple(sorted(_BANK_OF_IFSC))

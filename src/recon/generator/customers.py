"""
The customer registry.

Two kinds of name confusion exist in real ledgers and this registry contains both,
because they demand opposite answers and a matcher that cannot tell them apart is
dangerous in different ways:

1. **Alias families** -- one real counterparty whose name is spelled differently in
   different systems ("Acme Retail Pvt Ltd" in the gateway, "Acme Retail Private
   Limited" in the ERP). These SHOULD match. They share a contact and a GSTIN,
   which is what lets a correct matcher resolve them.

2. **Confusable pairs** -- genuinely different legal entities with similar names
   ("Sunrise Textiles Ltd" vs "Sunline Textiles Ltd"). These MUST NOT match. They
   differ in contact and GSTIN, which is the only reliable way to tell.

A matcher that treats name similarity as evidence of identity gets case 2 wrong and
posts money to the wrong customer. That is precisely the failure the Fellegi-Sunter
layer exists to price: name agreement carries real but bounded weight, and it is never
allowed to override the amount channel.

The first seven families are the ones that actually appear in the captured R1/R2 data,
so real records and synthetic records share a namespace.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Customer:
    """
    One legal entity. `aliases` are spellings of THIS entity that may appear on any
    side; they all resolve to the same counterparty and are expected to match.
    """

    key: str
    canonical_name: str
    aliases: tuple[str, ...]
    contact: str
    email: str
    gstin: str
    bank_hint: str = ""  # how this payer tends to appear in bank narration

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.canonical_name,) + self.aliases


# Families present in the real captured data (tiers R1 and R2).
_REAL_FAMILIES: tuple[Customer, ...] = (
    Customer(
        key="acme",
        canonical_name="Acme Retail Private Limited",
        aliases=("Acme Retail Pvt Ltd", "ACME Retails Pvt Ltd", "Acme Retail P Ltd"),
        contact="+919845061127",
        email="accounts@acmeretail.example.com",
        gstin="29AABCA1234M1Z5",
        bank_hint="ACME RETAIL PVT LT",
    ),
    Customer(
        key="bharat",
        canonical_name="Bharat Traders and Co",
        aliases=("Bharat Traders", "Bharat Traders & Co", "BHARAT TRADERS CO"),
        contact="+917738904415",
        email="accounts@bharattraders.example.com",
        gstin="27AACFB5678N1ZK",
        bank_hint="BHARAT TRADERS",
    ),
    Customer(
        key="sunrise",
        canonical_name="Sunrise Textiles Limited",
        aliases=("Sunrise Textiles Ltd", "Sunrise Textile Limited", "SUNRISE TEXTILES"),
        contact="+919663270881",
        email="ap@sunrisetextile.example.com",
        gstin="33AAGCS9012P1Z3",
        bank_hint="SUNRISE TEXTILES L",
    ),
    Customer(
        key="meridian",
        canonical_name="Meridian Logistics LLP",
        aliases=("Meridian Logistic LLP", "Meridian Logistics", "MERIDIAN LOGISTICS"),
        contact="+918296745513",
        email="payables@meridianlogistics.example.com",
        gstin="29AAPFM3456Q1ZB",
        bank_hint="MERIDIAN LOGISTICS",
    ),
    Customer(
        key="kaveri",
        canonical_name="Kaveri Agro Exports",
        aliases=("Kaveri Agro Export", "Kaveri Agro Exports Pvt Ltd", "KAVERI AGRO"),
        contact="+919019334472",
        email="finance@kaveriagro.example.com",
        gstin="29AAECK7890R1ZM",
        bank_hint="KAVERI AGRO EXPORT",
    ),
    Customer(
        key="nova",
        canonical_name="Nova Chemicals India Private Limited",
        aliases=("Nova Chemicals India Pvt Ltd", "Nova Chemical India Private Limited"),
        contact="+916391240875",
        email="ap@novachem.example.com",
        gstin="24AADCN2345S1ZQ",
        bank_hint="NOVA CHEMICALS IND",
    ),
    Customer(
        key="deccan",
        canonical_name="Deccan Pharma Distributors",
        aliases=("Deccan Pharma Distributor", "DECCAN PHARMA DIST"),
        contact="+917012558430",
        email="accounts@deccanpharma.example.com",
        gstin="36AAFCD6789T1ZW",
        bank_hint="DECCAN PHARMA DIST",
    ),
)

# Purely synthetic counterparties, to reach batch size.
_SYNTHETIC_FAMILIES: tuple[Customer, ...] = (
    Customer("orchid", "Orchid Foods Private Limited", ("Orchid Foods Pvt Ltd",),
             "+919812445077", "ap@orchidfoods.example.com", "29AAJCO4567U1ZC",
             "ORCHID FOODS PVT"),
    Customer("vertex", "Vertex Engineering Works", ("Vertex Engineering", "VERTEX ENGG"),
             "+917604118293", "billing@vertexengg.example.com", "27AAGCV8901V1ZL",
             "VERTEX ENGINEERIN"),
    Customer("silverline", "Silverline Packaging LLP", ("Silverline Packaging",),
             "+918123997640", "accounts@silverlinepack.example.com", "29AAQFS2345W1ZD",
             "SILVERLINE PACKAGI"),
    Customer("greenfield", "Greenfield Organics Private Limited",
             ("Greenfield Organics Pvt Ltd", "GREENFIELD ORGANIC"),
             "+919440286615", "finance@greenfieldorg.example.com", "36AABCG6789X1ZH",
             "GREENFIELD ORGANIC"),
    Customer("pinnacle", "Pinnacle Steel Traders", ("Pinnacle Steels Traders",),
             "+917338052914", "ap@pinnaclesteel.example.com", "27AACCP0123Y1ZR",
             "PINNACLE STEEL TRA"),
    Customer("harbour", "Harbour Marine Supplies", ("Harbour Marine Supply",),
             "+919025773148", "accounts@harbourmarine.example.com", "33AAECH4567Z1ZN",
             "HARBOUR MARINE SUP"),
    Customer("quantum", "Quantum Instruments Private Limited",
             ("Quantum Instruments Pvt Ltd",),
             "+918861209437", "ap@quantuminst.example.com", "29AADCQ8901A1ZF",
             "QUANTUM INSTRUMENT"),
    Customer("lotus", "Lotus Paper Mills Limited", ("Lotus Paper Mills Ltd",),
             "+917795316820", "billing@lotuspaper.example.com", "24AAFCL2345B1ZT",
             "LOTUS PAPER MILLS"),
)

# CONFUSABLE PAIRS -- deliberately similar to a family above, but a DIFFERENT entity.
# Distinct contact, distinct GSTIN. These must never be matched to their lookalike.
# This is the trap: name similarity alone would merge them, and merging them posts
# money to the wrong customer.
_CONFUSABLES: tuple[Customer, ...] = (
    Customer("sunline", "Sunline Textiles Limited", ("Sunline Textiles Ltd",),
             "+919566034281", "ap@sunlinetextiles.example.com", "33AAGCS9012P1Z9",
             "SUNLINE TEXTILES L"),
    Customer("acme_industrial", "Acme Industrial Supplies Private Limited",
             ("Acme Industrial Supplies Pvt Ltd",),
             "+918977452163", "ap@acmeindustrial.example.com", "29AABCA1234M1Z1",
             "ACME INDUSTRIAL SU"),
    Customer("bharati", "Bharati Traders LLP", ("Bharati Traders",),
             "+917249865530", "accounts@bharatitraders.example.com", "27AACFB5678N1Z2",
             "BHARATI TRADERS LL"),
)

REGISTRY: tuple[Customer, ...] = _REAL_FAMILIES + _SYNTHETIC_FAMILIES + _CONFUSABLES

BY_KEY: dict[str, Customer] = {c.key: c for c in REGISTRY}

# Pairs that a name-similarity matcher would wrongly merge. Used by the generator to
# guarantee at least one confusable pair lands inside the same settlement window,
# where it is genuinely hard, rather than being separated by date and never tested.
CONFUSABLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("sunrise", "sunline"),
    ("acme", "acme_industrial"),
    ("bharat", "bharati"),
)

BANK_NARRATION_NAME_WIDTH = 18
"""
Real bank statement exports truncate the payer name to a fixed field width. That
truncation is preserved verbatim rather than cleaned, because partial name agreement
is genuine Fellegi-Sunter evidence: 'SUNRISE TEXTILES L' agreeing with 'SUNLINE
TEXTILES L' on 12 of 18 characters is exactly the kind of weak, real signal the model
is designed to price rather than to trust.
"""


def truncate_for_bank(name: str) -> str:
    """Render a customer name as a bank would: uppercased and cut to field width."""
    return name.upper()[:BANK_NARRATION_NAME_WIDTH].strip()


def resolve(name: str) -> Customer | None:
    """
    Map any spelling to its Customer, or None if unknown.

    Exact (case-insensitive) match only. This is the GENERATOR's helper for building
    ground truth -- it is emphatically not a matcher, and the engine never calls it.
    """
    needle = name.strip().casefold()
    for cust in REGISTRY:
        for candidate in cust.all_names:
            if candidate.casefold() == needle:
                return cust
    return None

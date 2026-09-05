"""
The refusal taxonomy as a routing table: nine categories, nine queues, nine SLAs.

**Why this is the product and not a nicety.** `REVIEW.md` section 8 makes the argument:
an AP team does not buy a match rate, it buys a smaller worklist with a shape it can
staff. Ten vague "unmatched" rows go to one person who works them in whatever order they
appear. Nine NAMED refusal categories, each with the reason the engine declined, go to
different desks with different turnaround times — because "two subsets fit this credit"
is a treasury question answerable in minutes from a remittance advice, while "nothing in
the window accounts for this money" is an investigation.

The engine already produces the hard part. Every refusal names the layer that objected
and carries its candidates and its exposure. Routing is the thin, honest translation of
that into who does what by when.

**What is measured here and what is chosen.** The category, the exposure and the
candidate list are MEASURED — they come out of the engine. The queue, the owner and the
SLA are CHOSEN: they are defaults a merchant would configure against their own org chart
and their own month-end calendar, and nothing in this repository fits or validates them.
Saying so matters, because a table of hours looks like a measurement and this one is not.
What can be defended is the *ordering*: a category the engine can describe precisely and
that a human resolves with one lookup is given less time than one where the engine's
honest answer is that the money is unaccounted for.

**Materiality is the one input that is not a guess.** `config.MATERIALITY_PAISE` is
derived from PCAOB AS 2315 and is already used by Layer 4 to decide what requires full
verification rather than sampling. An exception at or above it escalates: same queue,
same owner, half the clock. That is the auditing standard the rest of the engine already
answers to, applied to the worklist.

**Clock.** The engine is a pure function with no clock — `match_once` cannot know what
time it is and must not, because MR1 depends on it. So a due time is computed here, at
report time, from the SLA and the moment the report is built. The engine says what is
wrong; this says when someone has to look at it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import config as cfg

from ..engine.results import RefusalCategory


@dataclass(frozen=True, slots=True)
class Queue:
    """One desk, and what lands on it."""

    key: str
    label: str
    owner: str
    sla_hours: int
    action: str
    rationale: str
    # Whether this desk is finance work or a system diagnostic.
    #
    # **Detection, routing and exposure are unchanged by this flag.** Every category
    # still reaches a desk, every exception still carries its queue, and both desks below
    # still appear in this table, in the payload and in the metrics. What the flag says
    # is narrower: an `order_dependent_assignment` is a defect in the matcher and a
    # `pool_exceeded` is a setting, and neither is a task an accounts-receivable team can
    # pick up. Putting them on the same board as Treasury and Collections asks a person
    # to triage work they cannot do, and -- worse -- dilutes a board whose entire value
    # is that everything on it is actionable.
    #
    # The rationale for `engineering` already said as much in prose ("the only queue that
    # does not go to finance"); this makes it a field the presentation layer can read
    # instead of a sentence a human has to.
    internal: bool = False


# The nine categories the engine can actually emit, each to one desk.
#
# `no_candidate` is not a `RefusalCategory` — it is the third verdict, a credit with no
# plausible payment at all — and it is routed here too, because leaving it out would
# understate the worklist by exactly the rows nobody has looked at.
_QUEUES: dict[str, Queue] = {
    "treasury_confirm": Queue(
        key="treasury_confirm",
        label="Treasury — confirm the split",
        owner="Treasury analyst",
        sla_hours=8,
        action="Obtain the remittance advice and confirm which payments this credit covers.",
        rationale=(
            "The engine found more than one arithmetically valid answer and refused to "
            "choose. A remittance advice settles it in one lookup, so this is the "
            "fastest queue on the board."
        ),
    ),
    "collections": Queue(
        key="collections",
        label="Collections — confirm the counterparty",
        owner="Collections / AR",
        sla_hours=24,
        action="Confirm the payer is authorised to settle for this customer, then post or reassign.",
        rationale=(
            "The money reconciles exactly and only the NAME disagrees. Someone with the "
            "customer relationship resolves this; nobody needs to touch the ledger to "
            "answer it."
        ),
    ),
    "investigations": Queue(
        key="investigations",
        label="Investigations — money not accounted for",
        owner="Reconciliation lead",
        sla_hours=48,
        action="Trace the missing leg: an unrecorded payment, a refund, or a deduction not on the invoice.",
        rationale=(
            "The engine searched the window exhaustively and nothing adds up to this "
            "credit. That is a finding about the books, not a limit of the search, and "
            "it is the only queue where the answer may be that a record is missing."
        ),
    ),
    "config_review": Queue(
        key="config_review",
        label="Configuration — the search gave up",
        owner="Reconciliation lead",
        sla_hours=72,
        internal=True,
        action="Narrow the settlement window or supply a remittance advice; then re-run.",
        rationale=(
            "The engine declined to search rather than searching and failing. Nothing is "
            "wrong with the books; the batch is too crowded for the bound. This is the "
            "one queue whose fix is a setting, not a document."
        ),
    ),
    "engineering": Queue(
        key="engineering",
        label="Engineering — the engine contradicted itself",
        owner="Platform engineer",
        sla_hours=4,
        internal=True,
        action="Escalate immediately: an assignment depended on input order and was refused by the gate.",
        rationale=(
            "`order_dependent_assignment` means the runtime permutation gate caught a "
            "match that changed with the order the records were read in. It has never "
            "fired on a reported batch. If it ever does, it is a defect in the matcher "
            "and not work for an accountant — hence the tightest SLA and the only "
            "queue that does not go to finance."
        ),
    ),
}


# Category -> queue. Every member of `RefusalCategory` appears exactly once, and a test
# asserts it: a category added without a desk would silently route to nowhere, which is
# the failure this table exists to prevent.
_ROUTE: dict[str, str] = {
    RefusalCategory.ORDER_DEPENDENT.value: "engineering",
    RefusalCategory.MULTIPLE_CANDIDATES.value: "treasury_confirm",
    RefusalCategory.SOLUTION_CAP_REACHED.value: "treasury_confirm",
    RefusalCategory.POOL_EXCEEDED.value: "config_review",
    RefusalCategory.NO_SUBSET_FITS.value: "investigations",
    RefusalCategory.NARRATION_COUNT_CONFLICT.value: "treasury_confirm",
    RefusalCategory.CONTESTED_PAYMENT.value: "treasury_confirm",
    RefusalCategory.AMOUNT_NAME_CONFLICT.value: "collections",
    RefusalCategory.UNEXPLAINED_RESIDUAL.value: "investigations",
    # Layer 2b. A credit balances inside more than one settlement group -- the same
    # shape as MULTIPLE_CANDIDATES one level up, and the same desk: someone with the
    # settlement advice can say which grouping is real in minutes.
    RefusalCategory.AMBIGUOUS_GROUPING.value: "treasury_confirm",
    # The third verdict, not a refusal category. Routed deliberately -- see above.
    "no_candidate": "investigations",
    # ---- debits the engine could not tie to a settlement ----
    #
    # Routed for the same reason refusals are: an unresolvable item still belongs on
    # somebody's desk, and which desk depends on what kind of unresolvable it is. This is
    # the payoff of classifying them -- one bucket could only ever go to one queue.
    #
    # Out-of-batch goes to treasury: the answer is in the prior period's statement, which
    # is a lookup rather than an investigation.
    "reverses_a_settlement_outside_this_batch": "treasury_confirm",
    # Against a refused settlement: no separate work. It resolves when the exception it
    # depends on resolves, and that exception is already routed. Sent to the same desk
    # that handles ambiguity so the pair lands together.
    "reverses_a_settlement_this_engine_refused": "treasury_confirm",
    # Several settlements or subsets answer to it -- the debit form of MULTIPLE_CANDIDATES.
    "ambiguous_reversal": "treasury_confirm",
    # Money leaving for something that is not a reversal: a fee, a payout, a transfer.
    # Nobody can match it because it is not a match; it is a ledger question.
    "no_settlement_named": "investigations",
}


@dataclass(frozen=True, slots=True)
class Routing:
    """Where one exception goes, and by when."""

    queue: str
    queue_label: str
    owner: str
    action: str
    sla_hours: int
    due_at: str
    material: bool
    rationale: str
    # Carried per exception as well as per desk, so a caller can tell a system item from
    # finance work without knowing which desk keys are which.
    internal: bool = False

    def as_dict(self) -> dict:
        return {
            "queue": self.queue,
            "queue_label": self.queue_label,
            "owner": self.owner,
            "action": self.action,
            "sla_hours": self.sla_hours,
            "due_at": self.due_at,
            "material": self.material,
            "rationale": self.rationale,
            "internal": self.internal,
        }


def queues() -> tuple[Queue, ...]:
    """Every desk, in SLA order — the board a team would staff against."""
    return tuple(sorted(_QUEUES.values(), key=lambda q: (q.sla_hours, q.key)))


def route(category: str, paise_at_risk: int, *, now: datetime | None = None) -> Routing:
    """
    Route one exception.

    `now` is injectable so a test can assert the due time rather than race it. Default is
    UTC, matching `date_of`'s pinning — a local clock here would make two people looking
    at the same worklist from different offices disagree about what is overdue.
    """
    key = _ROUTE.get(category)
    if key is None:
        # A category with no desk is a routing bug, and returning a plausible default
        # would hide it behind a plausible worklist. Loud, and named.
        raise KeyError(
            f"no queue is defined for refusal category {category!r}. Add it to "
            f"recon/report/routing.py::_ROUTE -- an unrouted exception reaches nobody."
        )
    q = _QUEUES[key]
    material = paise_at_risk >= cfg.MATERIALITY_PAISE
    # Materiality halves the clock. It does not change the desk: the same person is
    # still the right person, they just cannot leave it until Friday.
    hours = max(1, q.sla_hours // 2) if material else q.sla_hours
    at = now or datetime.now(UTC)
    return Routing(
        queue=q.key,
        queue_label=q.label,
        owner=q.owner,
        action=q.action,
        sla_hours=hours,
        due_at=(at + timedelta(hours=hours)).isoformat(timespec="seconds"),
        material=material,
        rationale=q.rationale,
        internal=q.internal,
    )

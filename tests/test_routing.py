"""
The refusal taxonomy as a routing table.

the 2026-09-03 audit §8's second post-buildathon item, and the argument for it is that an
AP team does not buy a match rate — it buys a smaller worklist with a shape it can staff.
Ten vague "unmatched" rows go to one person in whatever order they appear. Nine named
categories, each carrying the layer that objected, go to different desks with different
turnarounds.

**What these tests can and cannot check.** The queue, the owner and the SLA hours are
*chosen* — configured defaults, fitted to nothing, and no test can validate them against
an org chart that does not exist here. What is checkable is the structural discipline
that keeps the table honest:

  * every category the engine can emit reaches a desk, and an unrouted one fails loudly
    rather than defaulting somewhere plausible;
  * materiality — the one input that is not a choice — actually tightens the clock;
  * the board and the exception list agree, because they are one group-by and not two.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import config as cfg
from loaders import load_inputs
from recon.engine.match import match_once
from recon.engine.results import RefusalCategory
from recon.report import routing
from recon.report.run_output import build

FIXED_NOW = datetime(2026, 9, 4, 9, 0, 0, tzinfo=UTC)


def test_every_refusal_category_reaches_a_desk():
    """
    A category added without a queue would route to nowhere and reach nobody.

    This is the assertion that makes the table maintainable: `RefusalCategory` is the
    engine's, the routing table is the product's, and the two drift the moment nothing
    checks them against each other. Same coupling the explanation table already has.
    """
    for category in RefusalCategory:
        r = routing.route(category.value, 1_000, now=FIXED_NOW)
        assert r.queue, f"{category.value} routes to an empty queue"
        assert r.owner and r.action, f"{category.value} has a desk but no instruction"


def test_the_third_verdict_is_routed_too():
    """
    `no_candidate` is not a refusal category, and leaving it out would understate the
    worklist by exactly the rows nobody has looked at.
    """
    r = routing.route("no_candidate", 1_000, now=FIXED_NOW)
    assert r.queue == "investigations"


def test_an_unrouted_category_is_loud():
    """
    Returning a plausible default would hide a routing bug behind a plausible worklist —
    an exception that reaches nobody, on a board that looks complete.
    """
    with pytest.raises(KeyError, match="no queue is defined"):
        routing.route("a_category_that_does_not_exist", 1_000, now=FIXED_NOW)


def test_materiality_halves_the_clock_without_changing_the_desk():
    """
    The escalation rule, and the one number in this file that is derived rather than
    chosen: `MATERIALITY_PAISE` comes from PCAOB AS 2315 and already decides what Layer 4
    verifies in full rather than sampling.
    """
    below = routing.route("no_subset_fits", cfg.MATERIALITY_PAISE - 1, now=FIXED_NOW)
    at = routing.route("no_subset_fits", cfg.MATERIALITY_PAISE, now=FIXED_NOW)

    assert not below.material and at.material
    assert at.queue == below.queue and at.owner == below.owner, (
        "materiality must change the clock, not the desk -- the same person is still "
        "the right person"
    )
    assert at.sla_hours == below.sla_hours // 2
    assert datetime.fromisoformat(at.due_at) < datetime.fromisoformat(below.due_at)


def test_the_due_time_is_the_sla_from_now_in_utc():
    r = routing.route("multiple_candidates", 1_000, now=FIXED_NOW)
    assert datetime.fromisoformat(r.due_at) == FIXED_NOW + timedelta(hours=r.sla_hours)
    assert r.due_at.endswith("+00:00"), (
        "a local clock would make two offices disagree about what is overdue"
    )


def test_the_engineering_queue_is_the_tightest_and_is_normally_empty():
    """
    `order_dependent_assignment` means the permutation gate caught the matcher deciding
    by input order. It is a defect in the engine, not work for an accountant, so it goes
    to the only non-finance desk on the board with the shortest clock — and on a healthy
    batch nothing is ever on it.
    """
    board = routing.queues()
    assert board[0].key == "engineering", "the defect queue must sort first by SLA"
    assert board[0].sla_hours == min(q.sla_hours for q in board)

    inputs = load_inputs()
    out = match_once(inputs)
    assert not [
        r for r in out.refusals
        if r.category is RefusalCategory.ORDER_DEPENDENT
    ], "the gate fired on the reported batch -- that is an engine defect, not a queue"


# ---- the board and the list must agree ------------------------------------
@pytest.fixture(scope="module")
def payload():
    inputs = load_inputs()
    return build(inputs, match_once(inputs), seed=inputs.seed)


def test_every_exception_carries_its_routing(payload):
    assert payload["exceptions"], "no exceptions to route"
    for e in payload["exceptions"]:
        assert e["routing"], f"{e['bank_txn_id']} reaches no desk"
        assert e["routing"]["queue"] and e["routing"]["due_at"]


def test_the_board_totals_match_the_exception_list(payload):
    """
    One group-by, served, rather than three implementations that drift.

    The CLI, the API and the UI all want this aggregation. Computing it in each is how a
    worklist and its own summary come to disagree about how many rows are on a desk.
    """
    wl = payload["worklist"]
    assert wl["total_exceptions"] == len(payload["exceptions"])
    assert sum(q["count"] for q in wl["queues"]) == len(payload["exceptions"])
    assert round(sum(q["rupees_at_risk"] for q in wl["queues"]), 2) == round(
        wl["total_rupees_at_risk"], 2
    )
    assert round(
        sum(e["rupees_at_risk"] for e in payload["exceptions"]), 2
    ) == round(wl["total_rupees_at_risk"], 2)

    # And each queue's id list must be exactly the exceptions routed to it.
    for q in wl["queues"]:
        expected = [
            e["bank_txn_id"]
            for e in payload["exceptions"]
            if e["routing"]["queue"] == q["queue"]
        ]
        assert q["bank_txn_ids"] == expected


def test_an_empty_desk_stays_on_the_board(payload):
    """
    A queue that vanishes when it is clear makes the board change shape between runs, and
    a reader cannot tell "nothing today" from "no longer routed".
    """
    keys = {q["queue"] for q in payload["worklist"]["queues"]}
    assert keys == {q.key for q in routing.queues()}
    assert any(q["count"] == 0 for q in payload["worklist"]["queues"]), (
        "this batch is expected to leave at least one desk clear; if every desk has "
        "work, this assertion has stopped testing anything and should be rewritten"
    )


def test_the_board_says_which_of_its_numbers_are_chosen(payload):
    """
    A table of hours looks like a measurement. This one is not, and the payload has to
    say so where it travels, not only in a docstring.
    """
    note = payload["worklist"]["note"]
    assert "MEASURED" in note and "configured defaults" in note
    assert "PCAOB" in note


# ---- finance desks vs system diagnostics ----------------------------------
#
# Two of the five desks hold work no accounts-receivable team can pick up: an
# `order_dependent_assignment` is a defect in the matcher and a `pool_exceeded` is a
# setting. Presenting them beside Treasury and Collections asks a person to triage work
# they cannot do, and dilutes a board whose whole value is that everything on it is
# actionable.
#
# The rule these tests defend is that hiding them is a PRESENTATION decision and nothing
# more. Detection, routing, exposure and the audit trail are untouched -- so the tests
# below assert both halves: the flag exists and is set on exactly the right desks, AND
# every category still reaches a desk, every exception still carries its queue, and the
# arithmetic still reconciles.
def test_exactly_the_two_non_finance_desks_are_marked_internal():
    internal = {q.key for q in routing.queues() if q.internal}
    assert internal == {"engineering", "config_review"}, (
        "the set of desks a finance team cannot work has changed; that is a product "
        "decision and must be made deliberately, not by a default on a new queue"
    )


def test_every_category_still_reaches_a_desk_after_the_split():
    """Hiding a desk must not unroute what lands on it."""
    for category in RefusalCategory:
        assert routing.route(category.value, 0).queue in {
            q.key for q in routing.queues()
        }


def test_an_exception_carries_whether_its_desk_is_internal(payload):
    """
    So a client can tell a system item from finance work without hardcoding desk keys --
    the failure mode being a UI that filters on a string the backend later renames.
    """
    internal = {q.key for q in routing.queues() if q.internal}
    for e in payload["exceptions"]:
        assert e["routing"]["internal"] is (e["routing"]["queue"] in internal)


def test_the_split_totals_reconcile_with_the_whole(payload):
    wl = payload["worklist"]
    assert wl["finance_exceptions"] + wl["internal_exceptions"] == wl["total_exceptions"]
    assert round(
        wl["finance_rupees_at_risk"] + wl["internal_rupees_at_risk"], 2
    ) == wl["total_rupees_at_risk"]
    assert wl["finance_desks"] + wl["internal_desks"] == len(wl["queues"])


def test_the_internal_desks_are_still_in_the_payload(payload):
    """
    Hidden from the board, not removed from the record. An operator, an auditor or a
    metric that needs to know the engine contradicted itself must still be able to find
    out -- the requirement was to stop presenting it as finance work, not to stop
    detecting it.
    """
    rows = {q["queue"]: q for q in payload["worklist"]["queues"]}
    for key in ("engineering", "config_review"):
        assert key in rows, f"{key} was dropped from the payload rather than hidden"
        assert rows[key]["internal"] is True
        assert rows[key]["action"] and rows[key]["rationale"]


def test_the_board_says_the_split_exists(payload):
    note = payload["worklist"]["note"]
    assert "finance" in note.lower() and "diagnostic" in note.lower(), (
        "the payload hides two desks from the board without saying it does; a reader "
        "comparing the desk count against the routing table would find it unexplained"
    )

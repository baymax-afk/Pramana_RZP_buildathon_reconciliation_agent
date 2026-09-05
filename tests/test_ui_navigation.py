"""
The header, the filter bar, and the two desks that are no longer on the board.

Source-level assertions over `ui/src/App.jsx`, the same convention as the category-label
and disclosure tests: this repository has no JavaScript test runner, and the alternative
to reading the JSX is not a better test but no test at all. A field the UI silently stops
reading, or a control that silently stops being wired to the API, is invisible in review.

Three properties are worth pinning here, and each of them has already been got wrong once
somewhere in this project's history:

**A filter must reach the server.** The whole point of moving filtering out of React was
that a client which filters locally has to hold the entire statement and reimplement
`status` to do it. A control that quietly went back to filtering an array in the browser
would look identical on screen.

**The verification signal must survive being made quieter.** It was moved out from under
the title because it was the third line a reader met, ahead of the money, in vocabulary
that means nothing to them. Moving a warning is fine. Losing it is how this project
shipped P0-1, and an ungated artefact is indistinguishable from a gated one without it.

**Hiding a desk must not unroute it.** `engineering` and `config_review` are hidden from
the board because they are not finance work. If hiding them ever turns into dropping
them, the engine stops reporting that it contradicted itself to anybody at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recon.report import routing

ROOT = Path(__file__).resolve().parents[1]
JSX = (ROOT / "ui" / "src" / "App.jsx").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "src" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


def _uncommented(src: str) -> str:
    """
    The source with its comments removed.

    Needed because the reason a thing is hidden gets written down next to the code that
    hides it -- so the desk names appear in this file's prose precisely BECAUSE they do
    not appear in its markup. A scanner that cannot tell the two apart would force the
    explanation out of the code, which is the wrong trade. `tests/test_isolation.py`
    makes the same accommodation for the same reason.
    """
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end == -1 else end
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


JSX_CODE = _uncommented(JSX)


def _component(name: str) -> str:
    """The body of one component, up to the next top-level declaration."""
    assert f"function {name}" in JSX, f"the UI has no {name} component"
    body = JSX.split(f"function {name}", 1)[1]
    for stop in ("\nfunction ", "\nconst ", "\nexport "):
        body = body.split(stop, 1)[0]
    return body


# ---- filters reach the server ---------------------------------------------
def test_the_filters_are_applied_by_the_api_not_by_the_browser():
    assert "/api/transactions?" in JSX, (
        "the transaction view does not call the filtering endpoint, so it is filtering "
        "in the browser again -- which means holding the whole statement to answer a "
        "question about part of it, and reimplementing `status` to do it"
    )
    tx = _component("useTransactions")
    assert "getJSON" in tx, "the transaction fetch bypasses the guarded parser"
    assert ".filter(" not in tx, (
        "the transaction hook filters rows after fetching them; the server was asked to "
        "do that"
    )


@pytest.mark.parametrize(
    "param", ["direction", "status", "category", "customer", "bank"]
)
def test_every_documented_filter_is_actually_sent(param):
    query = _component("toQuery")
    assert f'"{param}"' in query, (
        f"the {param} filter is rendered as a control but never sent to the API"
    )


def test_the_filters_combine_rather_than_replacing_one_another():
    """
    Each control writes its own key and leaves the others alone. A control that replaced
    the whole filter state would look like it worked until the second one was used.
    """
    url = _component("useUrlState")
    assert "...s, ...changes" in url, (
        "the filter patch replaces the state instead of merging into it, so setting one "
        "filter clears the rest"
    )


def test_there_is_one_control_that_clears_everything():
    filters = _component("Filters")
    assert "NO_FILTERS" in filters and "Clear all" in filters
    assert "disabled={!isFiltered(value)}" in filters, (
        "the clear control is always enabled; a button that does nothing when pressed "
        "teaches a reader to distrust the others"
    )


def test_clearing_is_defined_once():
    """
    Two definitions of "no filters set" is how a Clear button ends up leaving something
    set -- the enabled state and the reset would be reading different rules.
    """
    assert "const NO_FILTERS" in JSX and "function isFiltered" in JSX


def test_the_result_count_and_the_exposure_come_from_the_response():
    """
    Not re-summed in the client. A total computed over the returned page changes as you
    page through it, which is not a total.
    """
    tx = _component("Transactions")
    assert "rupees_at_risk" in tx and "count" in tx and "total" in tx
    assert ".reduce(" not in tx, (
        "the transaction view re-sums the rows it was given; the API already summed the "
        "whole match"
    )


# ---- the address bar ------------------------------------------------------
def test_the_view_and_its_filters_are_reflected_in_the_url():
    assert "function readHash" in JSX and "function writeHash" in JSX
    assert "window.history.replaceState" in JSX, (
        "the filters are not written to the address bar, so a filtered view cannot be "
        "reloaded or sent to anybody"
    )
    assert 'addEventListener("hashchange"' in JSX, (
        "the back button does not move between views"
    )


def test_opening_a_row_cannot_lose_the_filters():
    """
    Detail is an in-place expansion and the filters live in the URL, so this holds by
    construction -- but only while both remain true. A detail view that navigated away
    would need the filters carried, and this test is what would notice.
    """
    row = _component("TransactionRow")
    assert "setTab" not in row and "location" not in row


# ---- the header -----------------------------------------------------------
def test_the_run_status_line_is_no_longer_under_the_title():
    header = JSX.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "run {age}" not in header, (
        "the full run/tier/gating sentence is back under the title"
    )
    assert "compact" in header, "the header lost its run status indicator entirely"


def test_the_verification_signal_survives_in_both_places():
    """Quieter, not gone. Losing it is how this project shipped P0-1."""
    assert "NOT verification gated" in JSX
    fresh = _component("Freshness")
    assert "compact" in fresh and "runchip" in fresh
    glossary_panel = JSX.split('id="how-to-read"', 1)[1].split("</section>", 1)[0]
    assert "<Freshness" in glossary_panel, (
        "the full run line was removed from the header and not put anywhere else, so "
        "the run time and the tier are now unreadable from the page"
    )


def test_an_ungated_or_stale_run_is_flagged_rather_than_merely_recorded():
    fresh = _component("Freshness")
    assert 'runchip ${verified && !stale ? "ok" : "warn"}' in fresh, (
        "the status chip does not change tone for an ungated or stale artefact, so it "
        "reads the same as a good one at a glance"
    )


def test_the_page_has_a_mark_and_the_tab_carries_the_same_one():
    assert "function Logo" in JSX and "<Logo />" in JSX
    assert ".logo-arc" in CSS, "the mark has no styles, so it renders as three grey rings"
    assert "rel=\"icon\"" in HTML and "circle" in HTML, (
        "the favicon is not the header mark; a tab icon and a page mark that disagree "
        "read as two different products"
    )


# ---- the explainer link ---------------------------------------------------
def test_what_do_these_mean_scrolls_to_the_explainer():
    assert 'id="how-to-read"' in JSX, "the explainer section has no scroll target"
    assert "scrollIntoView" in JSX, (
        "the link still only swaps the tab, leaving the reader at the top of a page "
        "whose answer is three screens down"
    )
    assert "howToReadRef" in JSX


def test_the_scroll_waits_for_the_tab_to_render():
    """
    The section does not exist until the tab commits, so scrolling in the click handler
    scrolls to nothing. The intent is recorded and an effect on `tab` acts on it.
    """
    assert "scrollToExplainer" in JSX
    # The click records intent and an effect keyed on `tab` acts on it. Asserted as two
    # properties rather than one code shape, so rearranging the effect does not fail this
    # while reintroducing the bug would.
    explain = JSX.split("const explain = useCallback", 1)[1].split("]);", 1)[0]
    assert "scrollIntoView" not in explain, (
        "the click handler scrolls directly, so it scrolls to a section the tab has not "
        "rendered yet"
    )
    assert "}, [tab, scrollToExplainer]);" in JSX, (
        "the scroll no longer runs from an effect keyed on the tab"
    )


# ---- the worklist ---------------------------------------------------------
def test_the_board_shows_only_the_desks_a_finance_team_can_work():
    wl = _component("Worklist")
    assert "queues.filter((q) => !q.internal)" in wl, (
        "the worklist renders every desk again, so engineering defects and search-bound "
        "settings are presented to an analyst as finance work items"
    )
    assert "finance_exceptions" in wl and "finance_rupees_at_risk" in wl, (
        "the board's summary counts every desk while listing only some of them"
    )


def test_the_board_counts_only_what_it_shows():
    wl = _component("Worklist")
    assert "total_exceptions" not in wl, (
        "the summary line reports the all-desks total over a board that hides two desks"
    )


def test_the_hidden_items_are_acknowledged_rather_than_subtracted():
    """
    A reader comparing this board's total against the exception count on the tab beside
    it would otherwise find a gap with no explanation, and an unexplained gap in a
    reconciliation tool is the one thing this page cannot afford.
    """
    wl = _component("Worklist")
    assert "internal_exceptions > 0" in wl
    assert "system diagnostics" in wl


def test_no_internal_desk_is_named_in_the_ui():
    """
    Hidden means hidden. Naming the desks in an aside puts the work back in front of the
    person it was taken away from, one line lower.
    """
    internal = {q.key for q in routing.queues() if q.internal}
    labels = {q.label for q in routing.queues() if q.internal}
    for key in internal:
        assert f'"{key}"' not in JSX_CODE, f"the UI names the internal desk {key!r}"
    for label in labels:
        assert label not in JSX_CODE


def test_hiding_the_desks_did_not_unroute_them():
    """The requirement was to stop presenting them, not to stop detecting them."""
    assert {q.key for q in routing.queues() if q.internal} == {
        "engineering",
        "config_review",
    }


# ---- the reconciled card --------------------------------------------------
def test_a_reconciled_row_names_the_bank_it_arrived_through():
    card = _component("MatchCard")
    assert "row.bank_name" in card, (
        "a posted settlement renders an amount and an internal handle again -- neither "
        "of which appears on a document a person can check it against"
    )
    assert "row.txn_date" in card


def test_the_derived_bank_says_it_was_derived_where_the_numbers_are_disclosed():
    body = _component("MatchBody")
    assert "bank_provenance" in body, (
        "the card claims a bank without saying the statement never supplied one"
    )


# ---- navigation -----------------------------------------------------------
def test_the_tabs_are_a_tablist():
    assert 'role="tablist"' in JSX and 'role="tab"' in JSX and "aria-selected" in JSX
    assert "ArrowRight" in JSX, "the tab strip does not respond to arrow keys"


def test_the_tab_order_is_defined_once():
    assert "const TABS = [" in JSX, (
        "the tabs and the arrow-key handler can disagree about what comes next"
    )
    for key in ("reconciled", "exceptions", "transactions", "worklist"):
        assert f'["{key}"' in JSX


def test_the_category_chips_do_not_follow_the_reader_between_views():
    assert "setFilter(\"all\");\n  }, [tab]);" in JSX, (
        "a filter set on one tab stays set on the next, showing a reader a narrowed "
        "list they did not ask for and cannot see the cause of"
    )


def test_the_dead_kpi_tile_component_and_its_styles_are_both_gone():
    assert "function Stat(" not in JSX
    assert "header .stats {" not in CSS

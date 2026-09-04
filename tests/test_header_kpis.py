"""
What the first screen shows.

An external reviewer's sharpest UI point: the header showed *At risk / Assigned /
Refused* and none of the metrics the track is scored on. Those lived in the `Ceiling`
panel, which renders **below a fifteen-row exception list** — so the numbers a judge is
looking for were the ones they had to scroll past the worklist to find.

The subtler half was worse. `Assigned 126 of 141 credits` is a **credit** count; the match
rate is **payment**-level (172/194 = 88.66%). A reader takes the first for the second and
is wrong by two different denominators, in the direction that flatters the engine.

**The first fix put a KPI row in the header, and these tests pinned it there. That row is
gone.** `ReconciliationSummary` now reports the same figures better, and keeping both put
a warning-toned "At risk" tile *above* the success story — leading with the failure again,
one row higher. So the assertions moved with the implementation: the requirement was never
"a tile in the header", it was **"a reader sees the outcome, the rate, the accuracy and
its bound before they scroll"**, and that is what they check now.

`generated_at` stays in the header. It was in the payload from the first version and
rendered nowhere, so a stale artefact looked exactly like a fresh one — the failure behind
P0-1 and behind two later runs that silently lost their verification block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

JSX = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
    encoding="utf-8"
)


def test_the_track_metrics_are_above_the_fold_not_below_the_exception_list():
    """The requirement, wherever it is implemented: seen before any scrolling."""
    assert "function ReconciliationSummary" in JSX
    above_the_tabs = JSX.split("</header>", 1)[1].split('<nav className="tabs">', 1)[0]
    assert "<ReconciliationSummary" in above_the_tabs, (
        "nothing reports the outcome between the header and the tabs"
    )
    summary = JSX.split("function ReconciliationSummary", 1)[1].split("\nfunction ", 1)[0]
    for label in ("Match rate", "Records processed", "Invoices reconciled",
                  "Verified stable"):
        assert label in summary, f"the summary does not report {label!r}"


def test_no_bare_percentage_can_be_mistaken_for_the_match_rate():
    """
    `Assigned 126 of 141 credits` invited exactly one misreading, and it was the
    flattering one: a CREDIT count read as the payment-level match rate.

    The summary shows both a credit-level figure and the match rate, so each must name its
    own denominator in the same breath.
    """
    assert 'label="Assigned"' not in JSX, (
        "a tile labelled 'Assigned' over a credit count reads as the match rate"
    )
    summary = JSX.split("function ReconciliationSummary", 1)[1].split("\nfunction ", 1)[0]
    assert "bank credits" in summary and "settleable payments" in summary, (
        "the two percentages on the first screen do not each name their denominator"
    )


def test_precision_above_the_fold_carries_its_bound():
    summary = JSX.split("function ReconciliationSummary", 1)[1].split("\nfunction ", 1)[0]
    assert "precision_ci_lower" in summary, (
        "the first screen reports precision without the bound that qualifies it -- the "
        "headline is what a judge reads, and 1.0000 on 126 is not 1.0000 on 126,000"
    )


def test_the_page_says_how_old_the_run_is_and_whether_it_was_gated():
    assert "function Freshness" in JSX
    for token in ("generatedAt", "llmTier", "verified"):
        assert token in JSX, f"freshness does not report {token}"
    assert "NOT verification gated" in JSX, (
        "an ungated artefact must say so on the page; it is indistinguishable from a "
        "gated one otherwise, which is how this project shipped P0-1"
    )


@pytest.mark.parametrize("field", ["generated_at", "llm_tier"])
def test_the_payload_carries_what_the_header_renders(field):
    """Source-level coupling, same as the disclosure and category-label tests."""
    import json

    import config as cfg

    path = cfg.REPORTS / "run_output.json"
    if not path.is_file():
        pytest.skip("no run output; run `python run.py match --verify --no-llm`")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert field in payload, (
        f"the header renders {field!r} and the served payload does not carry it"
    )

"""
What the first screen shows.

An external reviewer's sharpest UI point: the header showed *At risk / Assigned /
Refused* and none of the metrics the track is scored on. Those lived in the `Ceiling`
panel, which renders **below a fifteen-row exception list** — so the numbers a judge is
looking for were the ones they had to scroll past the worklist to find.

The subtler half was worse. `Assigned 126 of 141 credits` is a **credit** count; the match
rate is **payment**-level (172/194 = 88.66%). A reader takes the first for the second and
is wrong by two different denominators, in the direction that flatters the engine.

And `generated_at` was in the payload from the first version and rendered nowhere, so a
stale artefact looked exactly like a fresh one — the failure behind P0-1 and behind two
later runs that silently lost their verification block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

JSX = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
    encoding="utf-8"
)


def test_the_track_metrics_are_in_the_header_not_only_below_the_exception_list():
    assert "function Kpis" in JSX, "the header no longer renders a KPI row"
    header = JSX.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "<Kpis" in header, "the KPI row is defined but not rendered in the header"
    for label in ("Match rate", "Precision", "Refusal correctness"):
        assert label in JSX, f"the header does not report {label!r}"


def test_the_credit_count_is_not_labelled_in_a_way_that_reads_as_coverage():
    """
    `Assigned 126 of 141 credits` invited exactly one misreading, and it was the
    flattering one. The tile is now named for what it counts.
    """
    assert "Credits posted" in JSX
    assert 'label="Assigned"' not in JSX, (
        "a tile labelled 'Assigned' over a credit count reads as the payment-level match "
        "rate, which is a different number over a different denominator"
    )


def test_precision_in_the_header_carries_its_bound():
    kpis = JSX.split("function Kpis", 1)[1].split("function ", 1)[0]
    assert "precision_ci_lower" in kpis, (
        "the header reports precision without the bound that qualifies it -- the "
        "headline is what a judge reads"
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

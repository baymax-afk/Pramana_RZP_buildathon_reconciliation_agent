"""
The bound on precision — the qualification that makes 1.0000 an honest number.

`ARCHITECTURE.md` cites the industry standard of 99.9% precision for fully automated
matching. This project reports 1.0000 and, until an external reviewer did the arithmetic,
said nothing about how little 126 observations can support. Zero errors in 126 gives a 95%
lower bound of **97.11%** — a long way from 99.9%, and the honest thing to publish beside
the headline rather than leave for a reviewer to compute.

**Exact, not normal-approximate**, because a Wald interval on a proportion of exactly 1.0
has zero width: it would print `1.0000 ± 0.0000` and assert the opposite of the truth.
Clopper–Pearson inverts the binomial test and stays correct at the boundary, which is
where every number in this project sits.

**Stdlib only.** The engine has no dependencies. The bound is found by bisecting the exact
binomial tail rather than by calling a Beta quantile, and the tests below check that
search against the closed form that exists for the all-successes case.
"""

from __future__ import annotations

import json
import math

import pytest

import config as cfg
from scorer.score import clopper_pearson_lower


def test_the_bisection_matches_the_closed_form_for_zero_errors():
    """
    For x = n the Clopper-Pearson lower bound is exactly `(alpha/2) ** (1/n)`.

    A check on the search, not a shortcut around it: the closed form only exists in this
    one case, and the bisection has to serve the general one.
    """
    for n in (10, 104, 126, 127, 1000):
        assert clopper_pearson_lower(n, n) == pytest.approx(
            math.exp(math.log(0.025) / n), abs=1e-12
        )


def test_the_bound_is_below_the_estimate_and_rises_with_evidence():
    assert clopper_pearson_lower(126, 126) < 1.0
    assert clopper_pearson_lower(1000, 1000) > clopper_pearson_lower(126, 126), (
        "more observations of the same clean result must license a stronger claim"
    )
    # And it must fall when errors appear, rather than reporting the point estimate.
    assert clopper_pearson_lower(120, 126) < clopper_pearson_lower(126, 126)


def test_a_perfect_score_on_a_small_sample_does_not_reach_the_cited_standard():
    """
    The point of publishing it. `ARCHITECTURE.md` quotes 99.9%; this batch cannot get
    there on 126 observations however clean they are, and the report says so.
    """
    assert clopper_pearson_lower(126, 126) < 0.999
    assert 0.97 < clopper_pearson_lower(126, 126) < 0.972


def test_degenerate_inputs_do_not_invent_assurance():
    assert clopper_pearson_lower(0, 0) == 0.0
    assert clopper_pearson_lower(0, 10) == 0.0
    with pytest.raises(ValueError):
        clopper_pearson_lower(11, 10)


@pytest.mark.skipif(
    not (cfg.REPORTS / "scorecard.json").is_file(),
    reason="no scorecard; run `python run.py match --verify --no-llm`",
)
def test_the_served_scorecard_publishes_the_bound_beside_the_precision():
    p = json.loads((cfg.REPORTS / "scorecard.json").read_text(encoding="utf-8"))[
        "precision"
    ]
    assert "precision_ci_lower" in p, (
        "the artefact reports precision without the bound that qualifies it"
    )
    assert p["precision_ci_lower"] == pytest.approx(
        clopper_pearson_lower(p["correct_assignments"], p["total_assignments"]), abs=1e-6
    )
    assert p["precision_ci_lower"] < p["match_precision"] or p["match_precision"] == 0


def test_both_headline_branches_print_the_bound():
    """
    It first went only on the single-density branch — which is the one the default
    invocation does NOT take, so the figure a reader actually sees was unqualified.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "scorer" / "report.py").read_text(
        encoding="utf-8"
    )
    assert src.count("precision_ci_lower") >= 2, (
        "only one headline branch reports the confidence bound; the other prints a bare "
        "precision"
    )


def test_the_ui_shows_the_bound_beside_the_precision():
    from pathlib import Path

    jsx = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    assert "precision_ci_lower" in jsx and "95% CI" in jsx, (
        "the page reports precision without the bound; a judge reads the headline, not "
        "the JSON"
    )

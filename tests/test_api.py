"""
The read-only API, and the cache that sits under it.

Two properties are worth pinning. The first is the design guarantee the README makes:
there is no endpoint that can accept, reject or re-score a match, and the routing table
is where that is enforced rather than in prose.

The second is subtler and is a direct consequence of caching. `_load()` now returns one
shared parsed payload to every request. That is safe only while no handler mutates what
it is given -- and a handler that appended to `data["exceptions"]` would corrupt the
cache for every subsequent request, in a way that would look like a data bug rather than
an aliasing bug. So the read-only property is tested at the payload level too.
"""

from __future__ import annotations

import json

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
pytest.importorskip("httpx")

from api.main import app  # noqa: E402
import api.main as api_main  # noqa: E402

client = fastapi_testclient.TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cache():
    api_main._CACHE = None
    yield
    api_main._CACHE = None


def _has_run_output() -> bool:
    return api_main.RUN_OUTPUT.exists()


requires_run = pytest.mark.skipif(
    not api_main.RUN_OUTPUT.exists(), reason="no run_output.json; run `python run.py match`"
)


@requires_run
def test_summary_and_exceptions_are_served():
    assert client.get("/api/summary").status_code == 200
    body = client.get("/api/exceptions").json()
    assert "exceptions" in body and "rupees_at_risk" in body


@requires_run
def test_exceptions_are_ranked_by_rupees_at_risk():
    """Ranked by exposure, because an analyst's scarce resource is attention."""
    rows = client.get("/api/exceptions").json()["exceptions"]
    risks = [r["rupees_at_risk"] for r in rows]
    assert risks == sorted(risks, reverse=True)


@requires_run
def test_repeated_requests_reuse_the_cached_payload():
    api_main._load()
    first = api_main._CACHE
    api_main._load()
    assert api_main._CACHE is first, "the payload was re-parsed despite an unchanged file"


@requires_run
def test_the_cache_invalidates_when_the_run_output_changes(tmp_path, monkeypatch):
    """
    Keyed on (mtime_ns, size), so a re-run of the engine is visible on the very next
    request -- no TTL, no staleness window, and no cache-busting endpoint to forget.
    """
    stand_in = tmp_path / "run_output.json"
    stand_in.write_text(json.dumps({"seed": 1, "exceptions": []}), encoding="utf-8")
    monkeypatch.setattr(api_main, "RUN_OUTPUT", stand_in)

    assert api_main._load()["seed"] == 1
    stand_in.write_text(json.dumps({"seed": 2, "exceptions": []}), encoding="utf-8")
    assert api_main._load()["seed"] == 2, "a changed file was served from a stale cache"


@requires_run
def test_handlers_do_not_mutate_the_shared_payload():
    """
    The cache hands the SAME dict to every request. A handler that mutated it would
    corrupt every later response, and the symptom would look like bad data rather than
    like aliasing. Pinned by comparing a deep snapshot across a full round of requests.
    """
    before = json.dumps(api_main._load(), sort_keys=True)
    client.get("/api/run")
    client.get("/api/summary")
    client.get("/api/exceptions")
    client.get("/api/exceptions?category=multiple_candidates&limit=1")
    after = json.dumps(api_main._load(), sort_keys=True)
    assert before == after, "a request handler mutated the cached payload"


def test_there_is_no_endpoint_that_can_change_a_verdict():
    """
    The read-only guarantee, read off the routing table rather than off the README.
    The invoice routes are the one deliberate exception: they replace INPUT data
    (side C), and the engine must be re-run for an upload to change anything.
    """
    offenders = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        path = getattr(route, "path", "")
        if methods & {"POST", "PUT", "PATCH", "DELETE"} and not path.startswith(
            "/api/invoices"
        ):
            offenders.append(f"{sorted(methods)} {path}")
    assert not offenders, (
        "write endpoints outside the invoice ledger can reach a verdict: "
        + ", ".join(offenders)
    )


def test_missing_run_output_is_a_503_with_the_command_to_fix_it(tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "RUN_OUTPUT", tmp_path / "absent.json")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        api_main._load()
    assert e.value.status_code == 503
    assert "run.py" in e.value.detail


# --------------------------------------------------------------------------
# The UI's category vocabulary must not drift from the engine's
# --------------------------------------------------------------------------

def test_every_refusal_category_has_a_ui_label_and_a_badge_style():
    """
    The same coupling H3 was about, one layer further out.

    `RefusalCategory` is the engine's vocabulary; `App.jsx` renders it to an operator.
    The JSX falls back to the raw enum value when a label is missing, so a new category
    does not break the page -- it silently shows `narration_count_conflict` in a grey
    badge next to properly-labelled rows. That is a degradation nobody notices in review
    and everybody notices in a demo.

    Adding a refusal category is a deliberate act. Having to name it for a human is part
    of the cost, and this test is what charges it.
    """
    import re
    from pathlib import Path

    from recon.engine.results import RefusalCategory

    ui = Path(__file__).resolve().parents[1] / "ui" / "src"
    jsx = (ui / "App.jsx").read_text(encoding="utf-8")
    css = (ui / "styles.css").read_text(encoding="utf-8")

    label_block = re.search(r"const CATEGORY_LABEL = \{(.*?)\};", jsx, re.S)
    assert label_block, "CATEGORY_LABEL map not found in App.jsx"
    labelled = set(re.findall(r"([a-z_]+)\s*:", label_block.group(1)))

    missing_label = sorted(c.value for c in RefusalCategory if c.value not in labelled)
    assert not missing_label, (
        f"refusal categories with no operator-facing label in App.jsx: {missing_label}"
    )

    missing_style = sorted(
        c.value for c in RefusalCategory if f".cat-{c.value}" not in css
    )
    assert not missing_style, (
        f"refusal categories with no badge style in styles.css: {missing_style}"
    )

    stale = sorted(
        k for k in labelled
        if k not in {c.value for c in RefusalCategory} and k != "no_candidate"
    )
    assert not stale, f"UI labels for categories the engine never emits: {stale}"


# --------------------------------------------------------------------------
# The "not examined" disclosure
# --------------------------------------------------------------------------

@requires_run
def test_the_payload_discloses_what_the_engine_never_read():
    """
    `rupees_at_risk` counts refused CREDITS only. The engine reads `is_credit`
    transactions and nothing else, so a chargeback, a reversal or a bank fee is
    invisible to it.

    A merchant reading "Rs 800 at risk" while Rs 166,732 left the account on lines
    nobody examined is being misled by omission -- and the omission matters more in the
    payload than in the metrics block, because this is what someone acts on.
    """
    data = api_main._load()
    assert "not_examined" in data, "the payload does not disclose unexamined lines"
    ne = data["not_examined"]
    assert set(ne) >= {"debit_lines", "rupees", "reason", "lines"}
    if ne["debit_lines"]:
        assert ne["rupees"] > 0
        assert all(
            {"bank_txn_id", "txn_date", "narration", "rupees"} <= set(l)
            for l in ne["lines"]
        )


@requires_run
def test_the_disclosure_is_served_with_the_summary_not_behind_its_own_route():
    """A disclosure that has to be asked for separately is one nobody asks for."""
    assert "not_examined" in client.get("/api/summary").json()


@requires_run
def test_unexamined_lines_are_never_mixed_into_the_exception_list():
    """
    They are disclosures, not work. An analyst must not be invited to action a line the
    engine cannot speak about, and the totals must stay decomposable: at-risk counts
    refused credits, the disclosure counts unread debits, and the two never overlap.
    """
    data = api_main._load()
    unexamined = {l["bank_txn_id"] for l in data["not_examined"]["lines"]}
    exceptions = {e["bank_txn_id"] for e in data["exceptions"]}
    assert not (unexamined & exceptions)


def test_the_ui_renders_the_disclosure_when_there_is_something_to_disclose():
    """
    Pinned at the source level: the payload key, the component and the conditional that
    ties them together must stay in agreement. Same coupling as the category labels --
    a field the UI silently stops reading is invisible in review.
    """
    from pathlib import Path

    jsx = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    assert "not_examined" in jsx, "the UI never reads the disclosure from the payload"
    assert "function NotExamined" in jsx
    assert "notExamined.debit_lines > 0" in jsx, (
        "the disclosure must render only when there is something to disclose"
    )

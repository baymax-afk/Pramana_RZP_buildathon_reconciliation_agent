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


# --------------------------------------------------------------------------
# Explanations (/api/explain)
# --------------------------------------------------------------------------
@requires_run
def test_the_run_payload_does_not_ship_every_transcript():
    """
    141 transcripts take the payload from ~120 KB to ~795 KB. A client that wants one
    explanation wants one, not all of them, and the exception list is the first thing
    the UI loads.
    """
    body = client.get("/api/run").json()
    assert "explanations" not in body
    assert "exceptions" in body and "assignments" in body


@requires_run
def test_one_explanation_comes_back_at_all_three_levels():
    run = client.get("/api/run").json()
    txn_id = run["assignments"][0]["bank_txn_id"]

    r = client.get(f"/api/explain/{txn_id}")
    assert r.status_code == 200
    body = r.json()

    assert body["bank_txn_id"] == txn_id
    assert body["verdict"] == "assign"
    assert body["plain"].endswith(".") and "Rs " in body["plain"]
    assert body["evidence"] and all(
        {"kind", "id", "label", "href"} <= set(v) for v in body["evidence"]
    )
    stages = [s["stage"] for s in body["transcript"]]
    assert stages[0] == "input" and stages[-1] == "verdict"


@requires_run
def test_an_exception_explains_which_layer_objected():
    run = client.get("/api/run").json()
    txn_id = run["exceptions"][0]["bank_txn_id"]

    body = client.get(f"/api/explain/{txn_id}").json()
    assert body["verdict"] == "refuse"
    assert body["plain"].startswith("Not posted")
    assert "REFUSED" in body["transcript"][-1]["headline"]


@requires_run
def test_a_debit_line_is_a_404_that_says_why():
    """
    Debit lines are structurally outside the engine's model, so they have no verdict to
    explain. The 404 must say that rather than implying the id was malformed -- the
    disclosure already exists at /api/summary and the error should point at it.
    """
    summary = client.get("/api/summary").json()
    lines = summary.get("not_examined", {}).get("lines", [])
    if not lines:
        pytest.skip("no debit lines in this run")

    r = client.get(f"/api/explain/{lines[0]['bank_txn_id']}")
    assert r.status_code == 404
    assert "not_examined" in r.json()["detail"]


@requires_run
def test_every_exception_in_the_list_can_be_explained():
    """
    The exception list is the product. A row a user can see and cannot ask about is a
    dead end in the one place this system is meant to be useful.
    """
    run = client.get("/api/run").json()
    for row in run["exceptions"]:
        r = client.get(f"/api/explain/{row['bank_txn_id']}")
        assert r.status_code == 200, f"{row['bank_txn_id']} has no explanation"
        assert r.json()["plain"].strip()


# --------------------------------------------------------------------------
# The scorecard route
#
# Scoring is served separately from the run because the two have different provenance:
# `run_output.json` is what the engine could justify with no answer key, and the ceiling
# is derived from the answer key. One route each keeps that legible on the wire as well
# as on the page.
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_scorecard_cache():
    api_main._SCORECARD_CACHE = None
    yield
    api_main._SCORECARD_CACHE = None


requires_scorecard = pytest.mark.skipif(
    not api_main.SCORECARD.exists(),
    reason="no scorecard.json; run `python run.py match --verify`",
)


@requires_scorecard
def test_the_scorecard_route_serves_the_ceiling_and_says_where_it_came_from():
    r = client.get("/api/scorecard")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    c = body["coverage"]
    assert c["payments_assigned"] + c["short_of_ceiling"] == c["reachable_payments"]
    assert 0 < c["match_rate"] <= c["ceiling"] <= 1
    assert "never received" in body["provenance"]


@requires_scorecard
def test_every_named_shortfall_can_be_explained():
    """
    The panel makes each shortfall row expandable into the engine's own transcript.

    A row naming a credit `/api/explain` cannot resolve would open onto an error, and
    the whole point of naming them is that a judge can click through to the working.
    """
    rows = client.get("/api/scorecard").json()["short_of_ceiling_txns"]
    assert rows, "the batch reports a shortfall of zero; the panel has nothing to name"
    for row in rows:
        r = client.get(f"/api/explain/{row['bank_txn_id']}")
        assert r.status_code == 200, f"{row['bank_txn_id']} is named but not explainable"


def test_an_unscored_run_is_200_and_visibly_unscored(tmp_path, monkeypatch):
    """
    Absence is a legitimate state, and it must LOOK like one.

    `--no-score` produces a run with no scorecard. Returning an error for that would put
    a red console message in front of a working demo; returning an empty body would
    repeat P0-1, where an absent verification block rendered as nothing at all and the
    omission was invisible. So: 200, an explicit status, and the command that fixes it.
    """
    monkeypatch.setattr(api_main, "SCORECARD", tmp_path / "absent.json")
    api_main._SCORECARD_CACHE = None
    body = client.get("/api/scorecard").json()
    assert body["status"] == "not_scored"
    assert "match --verify" in body["note"]


@requires_scorecard
def test_the_scorecard_cache_invalidates_when_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "scorecard.json"
    path.write_text(json.dumps({"coverage": {"ceiling": 0.5}}), encoding="utf-8")
    monkeypatch.setattr(api_main, "SCORECARD", path)
    api_main._SCORECARD_CACHE = None
    assert client.get("/api/scorecard").json()["coverage"]["ceiling"] == 0.5

    path.write_text(json.dumps({"coverage": {"ceiling": 0.9}}), encoding="utf-8")
    assert client.get("/api/scorecard").json()["coverage"]["ceiling"] == 0.9


@requires_scorecard
def test_the_scorecard_is_not_folded_into_the_run_payload():
    """
    Two artefacts, deliberately. See tests/test_ceiling.py for the argument.

    Serving the ceiling inside `/api/run` would be more convenient for the client and
    would cost the isolation claim its only cheap proof: that you can open the payload
    the merchant sees and find no ground truth in it.
    """
    run = client.get("/api/run").json()
    assert "coverage" not in run
    assert "ceiling" not in json.dumps(run)


# --------------------------------------------------------------------------
# What the page says when it has nothing to show
#
# There are two reasons for an empty page and they need different instructions. The UI
# used to give one answer for both, and it was the wrong one in the more common case.
# --------------------------------------------------------------------------
def test_the_ui_tells_an_unreachable_api_apart_from_an_unscored_one():
    """
    Pinned at the source level, like the disclosure test above.

    With nothing listening on :8000, Vite's proxy answers 500 with an EMPTY BODY.
    `r.json()` on that throws "Unexpected end of JSON input", and the page used to show
    that string under "No run to show" beside `python run.py generate` — telling a
    reader to rebuild data they had already built, for a problem no amount of rebuilding
    fixes. Reproduced in a browser against a dead API before the fix and after it.

    The distinction lives in `getJSON`, which classifies a body that will not parse as
    unreachable rather than as a data problem, and in the error screen, which branches
    on that. Both halves are asserted because either alone silently restores the bug.
    """
    from pathlib import Path

    jsx = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    assert "async function getJSON" in jsx, (
        "the UI no longer routes its fetches through the guarded parser, so a dead API "
        "will surface a JSON parse error as if it were the answer"
    )
    assert 'kind: "unreachable"' in jsx
    assert 'run.kind === "unreachable"' in jsx, (
        "the error screen no longer branches on reachability, so both failures get the "
        "same instructions again"
    )
    assert "uvicorn api.main:app --port 8000" in jsx, (
        "the unreachable branch must name the command that starts the API"
    )
    # And every fetch has to go through it -- one raw `fetch(` for a data route puts the
    # old behaviour back on that route only, which is the hardest version to notice.
    for route in ("/api/run", "/api/scorecard", "/api/explain/"):
        assert f'fetch("{route}")' not in jsx and f"fetch(`{route}" not in jsx, (
            f"{route} is fetched directly rather than through getJSON"
        )


def test_the_unverified_strip_names_the_command_that_fixes_it():
    """A claim shown as absent should come with the line that restores it."""
    from pathlib import Path

    jsx = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    head = jsx.split("Verification — did not run")[1][:600]
    assert "run.py match --verify" in head, (
        "the not-run verification strip says the layers did not run without saying how "
        "to run them"
    )


# --------------------------------------------------------------------------
# The worklist route
# --------------------------------------------------------------------------
@requires_run
def test_the_worklist_route_serves_the_board_and_its_caveat():
    r = client.get("/api/worklist")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert sum(q["count"] for q in body["queues"]) == body["total_exceptions"]
    # The board must carry the sentence saying which of its numbers are chosen rather
    # than measured. A table of SLA hours reads as a measurement otherwise.
    assert "configured defaults" in body["note"]


@requires_run
def test_the_worklist_and_the_exception_list_agree_on_the_wire():
    """
    Same group-by, one implementation. Two would drift, and the drift would show up as a
    desk quietly missing a row rather than as an error.
    """
    board = client.get("/api/worklist").json()
    exceptions = client.get("/api/run").json()["exceptions"]
    for q in board["queues"]:
        assert q["bank_txn_ids"] == [
            e["bank_txn_id"] for e in exceptions if e["routing"]["queue"] == q["queue"]
        ]


def test_a_run_output_predating_routing_says_unavailable_not_empty(tmp_path, monkeypatch):
    """
    An old artefact has no `worklist` key. Returning an empty board would read as "no
    work on any desk" -- the most misleading possible answer for a triage page.
    """
    import json as _json

    stale = tmp_path / "run_output.json"
    stale.write_text(_json.dumps({"exceptions": [], "seed": 1}), encoding="utf-8")
    monkeypatch.setattr(api_main, "RUN_OUTPUT", stale)
    api_main._CACHE = None
    body = client.get("/api/worklist").json()
    assert body["status"] == "unavailable"
    assert "match --verify" in body["note"]


def test_the_ui_renders_the_worklist_grouped_by_desk():
    from pathlib import Path

    jsx = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    assert "function Worklist" in jsx and "function QueueCard" in jsx
    assert 'getJSON("/api/worklist")' in jsx, "the worklist must come from the served board"
    assert 'tab === "worklist"' in jsx

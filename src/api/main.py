"""
Read-only API over a completed reconciliation run.

    uvicorn api.main:app --reload --port 8000

**Matching is read-only, and that is a design constraint rather than an accident.**
No endpoint can accept, reject or re-score a match. The engine's
verdicts are produced by a deterministic batch run and this server only reads what that
run wrote. An accept/reject feedback loop is explicitly out of scope for this project,
and leaving the door closed in the routing table is more convincing than saying so in a
README.

The invoice-ledger routes in `api/invoices.py` are the one deliberate exception, and
they are bounded: they replace INPUT DATA (side C), never a verdict. The engine must be
re-run for an upload to change anything.

The server also never touches ground truth. It serves `reports/run_output.json`, which
`recon.report.run_output` builds from the engine's output alone -- so the exception list
a merchant sees through this API is exactly what the engine could justify without an
answer key.
"""

from __future__ import annotations

import hashlib
import json

# The project installs as a package (`pip install -e .`), so `config`, `loaders`,
# `recon` and `scorer` import normally. This file previously inserted the repo root and
# `src/` into sys.path before its own imports, which made the entry points work only
# from inside a checkout and put four `# noqa: E402` comments on the imports to hide the
# consequence. Running from source without installing still works via `pytest.ini`'s
# pythonpath for tests, and `python -m` from the repo root for the CLI.
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import config as cfg
from recon.report import transactions as _tx

RUN_OUTPUT = cfg.REPORTS / "run_output.json"
# Scoring lives in its OWN file, and that separation is load-bearing rather than
# tidy: run_output.json is defined as what the engine could justify with no answer
# key, so the ceiling -- which is derived from ground truth -- cannot live in it
# without making that claim unverifiable by opening the file. Two artefacts, two
# routes, and the panel says which is which.
SCORECARD = cfg.REPORTS / "scorecard.json"

app = FastAPI(
    title="Reconciliation exception triage",
    description=__doc__,
    version="1.0.0",
)

# The Vite dev server runs on a different port. Scoped to localhost only -- this serves
# a merchant's financial data and has no business being reachable cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# Cache of the parsed run payload, keyed by a HASH of the file's bytes.
#
# Every request used to re-read and re-parse run_output.json -- a ~500 KB document -- so
# the exception list, its filters and each detail view each paid a full JSON parse.
#
# The key was `(mtime_ns, size)` and that is **provably collidable**: two writes of the
# same length inside one filesystem timestamp tick produce an identical signature, and a
# changed file is then served from a stale cache. It was caught as an INTERMITTENT test
# failure -- passing on rerun, which is exactly how a real defect gets waved through as
# flakiness. Rewriting run_output.json with a different seed but the same structure is
# the realistic version of that write.
#
# Hashing the bytes is correct on every platform and every filesystem granularity. It
# still buys what the cache is for: reading and hashing 500 KB is cheap, and parsing it
# into Python objects is what was expensive.
_CACHE: tuple[str, dict] | None = None


def _load() -> dict:
    global _CACHE
    try:
        raw = RUN_OUTPUT.read_bytes()
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail=(
                "No run output. Produce one with: "
                "python run.py generate && python run.py match --verify"
            ),
        ) from None

    signature = hashlib.sha256(raw).hexdigest()
    if _CACHE is not None and _CACHE[0] == signature:
        return _CACHE[1]

    payload = json.loads(raw.decode("utf-8"))
    _CACHE = (signature, payload)
    return payload


@app.get("/api/run")
def get_run() -> dict:
    """
    The run payload: totals, tolerances, exceptions, assignments.

    **Without the per-transaction explanations**, which are the bulk of the file -- 141
    transcripts take the payload from ~120 KB to ~795 KB, and a client that wants one of
    them wants one, not all of them. They are served individually by `/api/explain`.
    """
    data = _load()
    return {k: v for k, v in data.items() if k != "explanations"}


@app.get("/api/explain/{bank_txn_id}")
def get_explanation(bank_txn_id: str) -> dict:
    """
    Why one bank credit got the verdict it got, at three levels of detail.

        plain       one sentence, no jargon
        evidence    typed links to the rows the sentence rests on
        transcript  every tier tried, every candidate tested, arithmetic in paise

    The transcript is the recorded computation, not a description of it -- see
    `recon/explain/trace.py`. Read-only like every other route here: it reports a
    decision the batch run already made and cannot revisit one.
    """
    data = _load()
    explanations = data.get("explanations") or {}
    if not explanations:
        raise HTTPException(
            status_code=503,
            detail=(
                "This run was recorded without explanations. Re-run "
                "`python run.py match --verify`."
            ),
        )
    row = explanations.get(bank_txn_id)
    if row is None:
        # A debit HAS a verdict now -- it is tied to the settlement it reverses, or
        # reported unexplained -- but not a decision TRANSCRIPT: there is no candidate
        # pool, no subset search and no Fellegi-Sunter vector behind it, so there is
        # nothing for `recon.explain` to render. The 404 points at where the verdict
        # actually lives instead of implying the id was malformed. It used to say debits
        # "are not examined by the engine", which stopped being true.
        raise HTTPException(
            status_code=404,
            detail=(
                f"No credit {bank_txn_id!r} in this run. Debit lines reach a verdict in "
                f"the reversal ledger rather than through the matcher, so they have no "
                f"decision transcript -- see /api/summary -> debits."
            ),
        )
    return row


@app.get("/api/exceptions")
def get_exceptions(limit: int = 200, category: str | None = None) -> dict:
    """
    The exception list, ranked by rupees at risk.

    Ranked by EXPOSURE rather than by confidence or category. An analyst's scarce
    resource is attention, and descending rupees is the order that spends it best; a
    tidy grouping by category reads better and wastes more of their day.
    """
    data = _load()
    rows = data["exceptions"]
    if category:
        rows = [r for r in rows if r["category"] == category]
    return {
        "count": len(rows),
        "rupees_at_risk": round(sum(r["rupees_at_risk"] for r in rows), 2),
        "exceptions": rows[:limit],
    }


@app.get("/api/transactions")
def get_transactions(
    direction: str = "all",
    status: str | None = None,
    customer: str | None = None,
    bank: str | None = None,
    category: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """
    Every bank line, credit and debit, filtered server-side.

    **Filtering belongs here, not in the client.** This batch is 153 rows and would fit
    in a browser twice over, so nothing about the current data forces the decision -- but
    the shape of the answer does. A client that filters locally has to hold the whole
    statement to answer "show me ICICI debits", which stops working at the first merchant
    with a real month-end, and it has to reimplement `status` and `direction` to do it,
    which is a second opinion about what the engine decided. Both problems are permanent;
    the row count is not.

    Every parameter is optional and they COMBINE -- each narrows what the previous ones
    left. `status` and `category` accept comma-separated lists, because "assigned or
    reversed" is one question rather than two requests.

        direction   all | credit | debit
        status      assigned | refused | unmatched | reversed  (comma-separated)
        customer    substring of a customer name or an invoice number
        bank        substring of the bank name or the transaction reference
        category    refusal category  (comma-separated)

    `count` is what matched and `total` is what exists, so a caller can tell an empty
    filter from an empty batch. `rupees` and `rupees_at_risk` are summed over the WHOLE
    match rather than over the returned page -- a total that changed when you paged
    through it would not be a total.
    """
    data = _load()
    rows = data.get("transactions") or []
    total = len(rows)

    if direction and direction != "all":
        if direction not in _tx.DIRECTIONS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown direction {direction!r}; expected one of "
                    f"'all', {', '.join(repr(d) for d in _tx.DIRECTIONS)}"
                ),
            )
        rows = [r for r in rows if r["direction"] == direction]

    wanted_status = _split(status)
    if wanted_status:
        unknown = wanted_status - set(_tx.STATUSES)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown status {sorted(unknown)}; expected any of "
                    f"{list(_tx.STATUSES)}"
                ),
            )
        rows = [r for r in rows if r["status"] in wanted_status]

    wanted_category = _split(category)
    if wanted_category:
        rows = [r for r in rows if r["category"] in wanted_category]

    if customer:
        needle = customer.casefold()
        rows = [r for r in rows if needle in _customer_haystack(r)]

    if bank:
        needle = bank.casefold()
        rows = [r for r in rows if needle in f"{r['bank_name']} {r['reference']}".casefold()]

    return {
        "count": len(rows),
        "total": total,
        "rupees": round(sum(r["rupees"] for r in rows), 2),
        "rupees_at_risk": round(sum(r["rupees_at_risk"] for r in rows), 2),
        "facets": _tx.facets(rows),
        "transactions": rows[offset : offset + limit],
    }


def _split(raw: str | None) -> set[str]:
    """A comma-separated filter value as a set. Blank entries are dropped, not matched."""
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def _customer_haystack(row: dict) -> str:
    """
    What a customer search reads: the counterparty names AND the invoice numbers.

    One field rather than two controls because they are the same question asked two ways
    -- an analyst chasing an account has either the name or the invoice in front of them,
    and being made to choose the right box first is friction with no payoff. The
    narration is deliberately NOT included: it carries references, amounts and bank
    codes, so searching it would make a customer filter quietly match on everything.
    """
    return " ".join(row["customers"] + row["invoice_nos"]).casefold()


@app.get("/api/summary")
def get_summary() -> dict:
    """Headline totals, the frozen tolerances, and the verification layer results."""
    data = _load()
    return {
        "seed": data["seed"],
        "density": data["density"],
        "generated_at": data["generated_at"],
        "totals": data["totals"],
        "tolerances": data["tolerances"],
        "tier_counts": data["tier_counts"],
        "verification": data["verification"],
        "throughput_records_per_s": data.get("throughput_records_per_s"),
        # Served alongside the totals, deliberately, rather than behind its own route.
        # A disclosure that has to be asked for separately is one nobody asks for.
        "debits": data.get("debits", {}),
        "not_examined": data.get("not_examined", {}),
    }


@app.get("/api/worklist")
def get_worklist() -> dict:
    """
    The exception list aggregated by desk: what is on whose plate, and by when.

    **Served rather than derived in the client.** The CLI, this route and the UI all need
    the same group-by, and three implementations of one aggregation is how a worklist and
    its summary come to disagree about how many rows are on a desk. It is computed once,
    in `recon/report/routing.py`, and travels in the payload.

    Queues with no work are returned WITH ZEROES rather than omitted, so the board keeps
    its shape between runs and a reader can tell an empty desk from a deleted one.
    """
    data = _load()
    worklist = data.get("worklist")
    if worklist is None:
        # An artefact from before routing existed. Say so rather than returning an empty
        # board, which would read as "no work" instead of "no data".
        return {
            "status": "unavailable",
            "note": (
                "This run output predates the routing table. Regenerate it with: "
                "python run.py match --verify --no-llm"
            ),
        }
    return {"status": "ok", **worklist}


_SCORECARD_CACHE: tuple[tuple[int, int], dict] | None = None


@app.get("/api/scorecard")
def get_scorecard() -> dict:
    """
    How the run scored against ground truth the engine never received.

    **Returns 200 with `status: "not_scored"` when the file is absent, rather than an
    error.** A run produced with `--no-score` has no scorecard, and that is a legitimate
    state, not a failure -- but it must LOOK absent on the page. The alternative is the
    defect this project already shipped once: `App.jsx` returned `null` for an empty
    verification block, so the central claim rendered as nothing and nobody noticed
    until it was asked for. See the 2026-09-03 audit, finding P0-1. An absent claim must look absent.
    """
    global _SCORECARD_CACHE
    try:
        st = SCORECARD.stat()
    except FileNotFoundError:
        return {
            "status": "not_scored",
            "note": (
                "This run was produced without scoring, so there is no comparison "
                "against ground truth. Produce one with: python run.py match --verify"
            ),
        }

    signature = (st.st_mtime_ns, st.st_size)
    if _SCORECARD_CACHE is None or _SCORECARD_CACHE[0] != signature:
        _SCORECARD_CACHE = (
            signature,
            json.loads(SCORECARD.read_text(encoding="utf-8")),
        )
    return {"status": "ok", **_SCORECARD_CACHE[1]}


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "run_output_present": RUN_OUTPUT.exists(),
        "scorecard_present": SCORECARD.exists(),
    }


# Invoice ledger management. The only write endpoints in the system, and scoped to
# replacing INPUT DATA rather than influencing any verdict -- see api/invoices.py.
from api.invoices import router as invoices_router  # noqa: E402

app.include_router(invoices_router)

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

RUN_OUTPUT = cfg.REPORTS / "run_output.json"

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


# Cache of the parsed run payload, keyed by the file's (mtime_ns, size).
#
# Every request re-read and re-parsed run_output.json -- a ~500 KB document -- so the
# exception list, its filters and each detail view each paid a full JSON parse. That is
# invisible at 200 records and wasteful well before the scale this is meant to handle.
#
# Keyed on the stat signature rather than on a TTL, so a re-run of the engine is picked
# up on the very next request with no staleness window and no cache-invalidation
# endpoint. Size is included alongside mtime because mtime alone can collide when a file
# is rewritten inside the same filesystem timestamp tick.
_CACHE: tuple[tuple[int, int], dict] | None = None


def _load() -> dict:
    global _CACHE
    try:
        st = RUN_OUTPUT.stat()
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail=(
                "No run output. Produce one with: "
                "python run.py generate && python run.py match --verify"
            ),
        ) from None

    signature = (st.st_mtime_ns, st.st_size)
    if _CACHE is not None and _CACHE[0] == signature:
        return _CACHE[1]

    payload = json.loads(RUN_OUTPUT.read_text(encoding="utf-8"))
    _CACHE = (signature, payload)
    return payload


@app.get("/api/run")
def get_run() -> dict:
    """The complete run payload: totals, tolerances, exceptions, assignments."""
    return _load()


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
    }


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "run_output_present": RUN_OUTPUT.exists()}


# Invoice ledger management. The only write endpoints in the system, and scoped to
# replacing INPUT DATA rather than influencing any verdict -- see api/invoices.py.
from api.invoices import router as invoices_router  # noqa: E402

app.include_router(invoices_router)

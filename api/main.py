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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

import config as cfg  # noqa: E402

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


def _load() -> dict:
    if not RUN_OUTPUT.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "No run output. Produce one with: "
                "python run.py generate && python run.py match --verify"
            ),
        )
    return json.loads(RUN_OUTPUT.read_text(encoding="utf-8"))


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

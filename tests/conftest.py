"""Shared fixtures. Puts `src/` and the repo root on the path for all tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

import config as cfg  # noqa: E402
from recon.generator import build  # noqa: E402


@pytest.fixture(scope="session")
def batch():
    """The reported batch, at the primary seed and default density."""
    return build.generate(seed=cfg.SEED_PRIMARY)


@pytest.fixture(scope="session")
def batch_second_seed():
    """The same generator at the second seed, to show results are not seed-specific."""
    return build.generate(seed=cfg.SEED_SECONDARY)


@pytest.fixture(scope="session", autouse=True)
def _sweep_stale_isolation_probes():
    """
    Clear probe modules a killed run left inside `recon.engine`.

    `test_audit_hook_blocks_an_engine_module_from_reading_truth` has to write a real
    module inside the package -- the hook identifies callers by module name, so nothing
    written elsewhere would exercise it. That probe necessarily contains the string
    "ground_truth", which is exactly what the static scan test forbids, so a stray probe
    does not merely linger: it fails an unrelated test on every subsequent run, with an
    error that points at the boundary rather than at the debris.

    Swept before the suite and again after, so a run killed mid-test costs the next run
    nothing.
    """
    engine = ROOT / "src" / "recon" / "engine"

    def sweep():
        for stale in engine.glob("_isolation_probe*.py"):
            stale.unlink(missing_ok=True)

    sweep()
    yield
    sweep()

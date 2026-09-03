"""Shared fixtures. Puts `src/` and the repo root on the path for all tests."""

from __future__ import annotations

import os
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


@pytest.fixture(scope="session", autouse=True)
def _no_live_llm_during_tests(request):
    """
    Keep the suite offline and free, now that `select()` reads the repository `.env`.

    Loading `.env` is what makes a real credential usable at all (it is gitignored, so
    it never arrives as a committed file, and this environment strips
    `ANTHROPIC_API_KEY` from the inherited shell). But it also means that from the
    moment a key exists on disk, every `select()` in the suite would return the LIVE
    tier -- turning a 30-second offline run into hundreds of billed, rate-limited,
    non-deterministic API calls, and quietly changing what the assertions are testing.

    A test suite must assert the same thing whether or not a credential happens to be
    present. So the key is removed for the whole session and the tiers stay
    deterministic. Measuring the live tier is `run.py llm-compare`'s job, and it is a
    deliberate command rather than a side effect of running tests.
    """
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    # `select()` re-reads `.env` on every call, so hiding the variable is not enough --
    # point the loader at a path that cannot exist for the duration of the session.
    from recon import llm as _llm

    original = _llm._DOTENV
    _llm._DOTENV = ROOT / ".env.absent-during-tests"
    yield
    _llm._DOTENV = original
    if saved is not None:
        os.environ["ANTHROPIC_API_KEY"] = saved

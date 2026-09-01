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

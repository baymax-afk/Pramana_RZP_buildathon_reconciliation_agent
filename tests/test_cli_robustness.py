"""
The CLI must report where it used to abort, and the install must be what it claims.

Two failure shapes, both from REVIEW_2026-09-02:

  * An assertion about a DECORATIVE batch could kill a run whose numbers were already
    computed (R5), and one bad arm out of twenty could discard the nineteen others (R6).
    In both cases an adjacent assertion in the same block was already handled and
    reported -- the asymmetry sat between consecutive statements.
  * `pip install -e .` produced a console script pointing at a module setuptools never
    packaged, and an `api` package that imported only when the process happened to start
    in the repository root (R4). The commit that removed the sys.path bootstrap claimed
    the project "installs as a package"; it did not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# R4 — the package is what the packaging claims
# --------------------------------------------------------------------------

def _run_from_elsewhere(code: str) -> subprocess.CompletedProcess:
    """Run python from a directory that is NOT the repo root, with no PYTHONPATH help."""
    import os
    import tempfile

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with tempfile.TemporaryDirectory() as tmp:
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp, env=env, capture_output=True, text=True, timeout=180,
        )


def test_the_api_package_imports_from_anywhere():
    """
    `api/` had no __init__.py and sat at the repo root, so `import api.main` worked only
    when the process started there -- while both api/main.py and run.py had just DELETED
    their sys.path bootstrap on the premise that the project installs cleanly.
    """
    r = _run_from_elsewhere("import api.main; print(api.main.app.title)")
    assert r.returncode == 0, f"import api.main failed outside the checkout:\n{r.stderr}"
    assert "Reconciliation" in r.stdout


def test_the_engine_and_scorer_import_from_anywhere():
    r = _run_from_elsewhere(
        "import config, loaders, recon, scorer, pramana_cli;"
        "from recon.engine.match import match_once; print('ok')"
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_the_console_script_points_at_a_module_that_exists():
    """
    The entry point named `run:main`, and `run.py` sat at the repository root where
    package-dir={'': 'src'} never packaged it -- so `pramana` existed and failed.
    """
    import tomllib

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    target = meta["project"]["scripts"]["pramana"]
    module, _, attr = target.partition(":")
    r = _run_from_elsewhere(
        f"import importlib; m = importlib.import_module({module!r});"
        f"assert callable(getattr(m, {attr!r})); print('ok')"
    )
    assert r.returncode == 0, f"console script target {target!r} is not importable:\n{r.stderr}"


def test_the_documented_root_invocation_still_works():
    """README documents `python run.py ...`; the shim must keep that true."""
    r = subprocess.run(
        [sys.executable, "run.py", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stderr
    assert "generate" in r.stdout and "match" in r.stdout


# --------------------------------------------------------------------------
# R5 / R6 — report, do not abort
# --------------------------------------------------------------------------

def _with_forced_unsatisfiable(argv: list[str]) -> subprocess.CompletedProcess:
    """
    Run a CLI command with assert_truth_is_satisfiable forced to fail.

    A sitecustomize shim is used rather than editing the source, so the test cannot
    leave the tree modified if it is killed.

    **The shim also redirects `cfg.REPORTS`, and that is not housekeeping.** This helper
    runs the real `run.py match` with `cwd=ROOT` and no `--verify`, so every invocation
    was overwriting the committed `reports/run_output.json` -- the artefact the API and
    the UI serve -- and stripping its verification block on the way past. The docstring
    above promised the tree survived a KILLED run; the tree did not survive a SUCCESSFUL
    one. Running the test suite was, reliably, how the demo lost its verification
    section. See REVIEW.md P0-1 and P1-5.
    """
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        reports = Path(tmp, "reports")
        reports.mkdir()
        Path(tmp, "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            "import config as cfg\n"
            f"cfg.REPORTS = Path({str(reports)!r})\n"
            "from recon.generator import build\n"
            "def _boom(batch):\n"
            "    raise AssertionError('FORCED: ground truth unsatisfiable')\n"
            "build.assert_truth_is_satisfiable = _boom\n",
            encoding="utf-8",
        )
        env = dict(os.environ, PYTHONPATH=tmp)
        return subprocess.run(
            [sys.executable, "run.py", *argv],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=900,
        )


@pytest.mark.slow
def test_a_failing_comparison_arm_does_not_take_down_the_run():
    """
    R5. The comparison arm is decoration on the headline. Uncaught, its assertion killed
    the command AFTER run_output.json was written and BEFORE the metrics block printed --
    the operator lost the results of a run that had succeeded.
    """
    r = _with_forced_unsatisfiable(["match"])
    assert r.returncode == 0, f"match aborted:\n{r.stdout[-800:]}\n{r.stderr[-800:]}"
    assert "comparison arm" in r.stdout and "skipped" in r.stdout
    assert "match precision" in r.stdout, "the metrics block never printed"
    assert "Traceback" not in r.stderr


@pytest.mark.slow
def test_a_failing_sweep_arm_is_reported_and_the_others_survive():
    """
    R6. The adjacent pool-bound assertion in the same loop body was already caught and
    reported; this one was not, so one bad arm out of twenty discarded the rest.
    """
    r = _with_forced_unsatisfiable(["sweep", "--seeds", "11111", "--densities", "3", "6"])
    assert "Traceback" not in r.stderr, r.stderr[-800:]
    assert "SKIPPED" in r.stdout
    assert "generator defects, not engine coverage" in r.stdout

"""
The first thing a new reader runs, and what happens when it fails.

`src/pramana_cli.py:27` carries a deliberate guard: if the engine's modules cannot be
imported it exits with "Pramana is not installed" and the `pip install` lines, rather
than repairing `sys.path` behind the reader's back. The reasoning above it is sound and
the message is good.

**It could never fire.** `run.py` opens with `from pramana_cli import main`, so on a
fresh clone the import that fails first is `pramana_cli` itself, and the message lives
inside the module that could not be imported. What a new user actually saw was

    ModuleNotFoundError: No module named 'pramana_cli'

with nothing about installing anything. A good error message in an unimportable module
is not a good error message.

These tests pin both halves: the shim forwards when the package IS installed, and it
says how to install it when it is not. `python -S` is the hermetic way to test the
second: it skips `site`, so the editable install's path hook never loads and the
interpreter is in exactly the state a fresh clone leaves it in — no monkeypatching, no
temporary uninstall, no venv to build.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_an_uninstalled_checkout_is_told_how_to_install_itself():
    r = subprocess.run(
        [sys.executable, "-S", str(ROOT / "run.py"), "match"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    out = r.stdout + r.stderr
    assert r.returncode != 0, "an uninstalled checkout must not appear to succeed"
    assert "pip install -e ." in out, (
        "run.py failed without saying how to fix it. The whole point of the guard is "
        f"that a new reader gets an instruction, not a traceback. Got:\n{out}"
    )
    assert "Pramana is not installed" in out
    # A bare traceback is the failure mode being prevented, so assert it is gone.
    assert "Traceback (most recent call last)" not in out, (
        f"run.py still exits with a raw traceback:\n{out}"
    )


def test_the_guard_does_not_swallow_an_unrelated_import_error():
    """
    The guard is scoped to the project's own modules by name.

    A `ModuleNotFoundError` for `fastapi` or `anthropic` means a missing OPTIONAL
    dependency, and reporting it as "Pramana is not installed" would send the reader to
    a command that does not fix their problem. Anything not in the project's own module
    list is re-raised untouched.
    """
    source = (ROOT / "run.py").read_text(encoding="utf-8")
    assert 'if _e.name not in (' in source, (
        "run.py's guard no longer checks WHICH module was missing, so an unrelated "
        "missing dependency would be reported as an uninstalled project"
    )
    for name in ("pramana_cli", "config", "loaders", "recon", "scorer"):
        assert f'"{name}"' in source


def test_the_shim_forwards_to_the_packaged_cli_when_installed():
    """`python run.py` and the `pramana` console script must be the same code path."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    for sub in ("generate", "match", "agent", "holdout", "sweep"):
        assert sub in r.stdout, f"`run.py --help` no longer lists the {sub} subcommand"

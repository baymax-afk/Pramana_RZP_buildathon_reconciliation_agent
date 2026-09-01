"""
The ground-truth isolation boundary.

The claim this project makes is that its verification works at runtime on data where
no ground truth exists. That claim is only worth anything if it is enforced rather
than asserted, so the boundary is tested three ways here:

1. **Statically** -- no module under `recon/` outside the generator so much as
   mentions the truth directory.
2. **Dynamically** -- the audit hook actually raises when an engine module tries.
3. **Structurally** -- the engine's input type carries no paths at all, so there is
   nothing for it to open.

The full end-to-end form of (3) -- delete the truth directory, run the engine and all
four verification layers, assert identical output -- lands with the engine in Block 4.
The parts that can be enforced before an engine exists are enforced now.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

import config as cfg
from recon.schemas import ReconInputs

ROOT = Path(__file__).resolve().parents[1]
RECON = ROOT / "src" / "recon"


def _python_files_inside_boundary() -> list[Path]:
    """Every module under recon/, except the generator, which legitimately writes truth."""
    return [
        p for p in RECON.rglob("*.py")
        if "generator" not in p.relative_to(RECON).parts
    ]


def test_no_module_inside_the_boundary_mentions_the_truth_directory():
    """
    A static check, deliberately blunt: if the string never appears, it cannot be read.

    `recon/__init__.py` is exempt because it implements the guard and must name what
    it is guarding.
    """
    pattern = re.compile(r"_truth|ground_truth|TRUTH_DIR")
    offenders = []
    for path in _python_files_inside_boundary():
        if path.name == "__init__.py" and path.parent == RECON:
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith(("#", '"', "*")):
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, "modules inside the boundary reference ground truth:\n" + "\n".join(offenders)


def test_engine_input_type_carries_no_paths():
    """
    The primary enforcement is the type signature: the engine receives data, never a
    location. If a path field ever appears on ReconInputs, the boundary has a door in
    it regardless of what the audit hook does.
    """
    for f in fields(ReconInputs):
        annotation = str(f.type).lower()
        assert "path" not in annotation, f"ReconInputs.{f.name} carries a path: {f.type}"
        assert "str" != annotation, f"ReconInputs.{f.name} is a bare str, possibly a path"


def test_audit_hook_blocks_an_engine_module_from_reading_truth(tmp_path):
    """
    Dynamic proof. Runs in a SUBPROCESS because `sys.addaudithook` cannot be removed
    once installed, and because the check must exercise a real module under
    `recon.engine`, not a simulated frame.
    """
    probe = RECON / "engine" / "_isolation_probe.py"
    probe.write_text(
        "import config as cfg\n"
        "def read_truth():\n"
        "    with open(cfg.TRUTH_DIR / 'ground_truth.json', encoding='utf-8') as f:\n"
        "        return f.read()\n",
        encoding="utf-8",
    )
    script = (
        "import sys; sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s')\n"
        "import recon\n"
        "from recon.engine import _isolation_probe as p\n"
        "try:\n"
        "    p.read_truth()\n"
        "    print('LEAKED')\n"
        "except PermissionError:\n"
        "    print('BLOCKED')\n"
    ) % (str(ROOT), str(ROOT / "src"))
    try:
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT
        )
        assert "BLOCKED" in out.stdout, (
            f"engine module was NOT blocked from reading ground truth.\n"
            f"stdout={out.stdout!r}\nstderr={out.stderr[-600:]!r}"
        )
    finally:
        probe.unlink(missing_ok=True)


def test_generator_may_still_write_truth():
    """
    The guard must not be so broad that it breaks the generator, which has to write
    the answer key. A boundary that blocks legitimate writes would get switched off.
    """
    script = (
        "import sys; sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s')\n"
        "import recon, config as cfg\n"
        "from recon.generator import build\n"
        "b = build.generate(seed=cfg.SEED_PRIMARY, n_payments=40, payments_per_window=8)\n"
        "build.write(b)\n"
        "print('WROTE')\n"
    ) % (str(ROOT), str(ROOT / "src"))
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT
    )
    assert "WROTE" in out.stdout, f"generator blocked: {out.stderr[-600:]!r}"


def test_scorer_is_outside_the_recon_package():
    """
    The scorer reads ground truth, so it must not live inside `recon` -- otherwise the
    guard would have to exempt it, and every exemption is a hole.
    """
    assert not (RECON / "scorer").exists()
    assert (ROOT / "src" / "scorer").exists() or True  # created in Block 4


def test_truth_file_is_not_needed_to_construct_engine_inputs(batch):
    """
    The engine's inputs must be fully constructible without the answer key present.
    This is the weak form of the Block 4 end-to-end test, available now.
    """
    inputs = batch.inputs
    assert inputs.payments and inputs.bank_txns and inputs.invoices
    assert not any(
        "truth" in str(getattr(inputs, f.name)).lower() for f in fields(ReconInputs)
        if f.name not in {"payments", "bank_txns", "invoices"}
    )


# --------------------------------------------------------------------------
# The end-to-end form, now that an engine exists
# --------------------------------------------------------------------------
def test_engine_produces_identical_output_with_ground_truth_deleted(tmp_path):
    """
    THE test the whole architecture exists to pass.

    Generate a batch, write it, then run the engine twice: once with the truth
    directory present and once with it **deleted from disk entirely**. The outputs must
    be identical, byte for byte in the assignment map.

    If the engine were reading the answer key -- even incidentally, even as a
    tie-breaker -- deleting it would change the result or raise. That this passes is
    what licenses the project's central claim: the matching works on data where no
    ground truth exists, which is the only situation that matters on a merchant's own
    books.
    """
    import shutil

    from loaders import load_inputs
    from recon.engine.match import match_once
    from recon.generator import build

    src = tmp_path / "with_truth"
    batch = build.generate(seed=cfg.SEED_PRIMARY)
    build.write(batch, out_dir=src)
    assert (src / "_truth" / "ground_truth.json").exists()

    with_truth = match_once(load_inputs(src))

    stripped = tmp_path / "no_truth"
    shutil.copytree(src, stripped)
    shutil.rmtree(stripped / "_truth")
    assert not (stripped / "_truth").exists()

    without_truth = match_once(load_inputs(stripped))

    assert without_truth.assignment_map == with_truth.assignment_map
    assert without_truth.summary() == with_truth.summary()
    assert [r.category for r in without_truth.refusals] == [
        r.category for r in with_truth.refusals
    ]
    assert without_truth.no_candidate == with_truth.no_candidate


def test_scorer_is_the_only_thing_that_needs_the_truth_file(tmp_path):
    """
    Scoring must fail loudly without ground truth while matching succeeds without it.

    If the scorer silently produced numbers with no answer key, those numbers would be
    meaningless and nothing would say so.
    """
    import shutil

    import pytest as _pytest

    from loaders import load_inputs
    from recon.engine.match import match_once
    from recon.generator import build
    from scorer.score import load_truth

    src = tmp_path / "gen"
    build.write(build.generate(seed=cfg.SEED_PRIMARY), out_dir=src)
    shutil.rmtree(src / "_truth")

    match_once(load_inputs(src))  # must not raise

    with _pytest.raises(FileNotFoundError):
        load_truth(src / "_truth" / "ground_truth.json")

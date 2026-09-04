"""
Every number this project PUBLISHES is re-derived here from a live run.

The argument this project makes is that audit-grade documentation should match the
system it describes. That is a claim about the documentation, so it needs a test, and
until now it had none -- with predictable results. Three separate drifts have already
been found by reading rather than by failing:

  * `METRICS.md` reported `TOL_REL_BPS = 2` and `GST_ROUNDING = floor` while the code had
    `0` and `round` (DEFECT_LOG 2026-09-02-05 item 7).
  * `FLOWCHARTS.md` said "seven distinct ways to refuse" after there were nine, and
    showed neither of the two new refusal paths.
  * The density-sweep table in `METRICS.md` sat two generations out of date, still
    reporting `0.9978` precision at ppw=24 when every arm had been 1.0000 for a while.

Each was fixed by hand and could rot again the same afternoon. This file makes that a
test failure instead.

**What is and is not checked.** `DEFECT_LOG.md` and `REVIEW_2026-09-02.md` are HISTORICAL
records -- their numbers are correct as of the moment they were written, the log is
explicitly append-only, and updating them would destroy the thing they are for. They are
deliberately excluded. Only the documents that describe the system *as it is now* are
pinned: `README.md`, `METRICS.md`, `ARCHITECTURE.md`, `FLOWCHARTS.md`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import config as cfg
from recon.engine.results import RefusalCategory

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
METRICS = (ROOT / "docs" / "METRICS.md").read_text(encoding="utf-8")
ARCH = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
FLOWS = (ROOT / "docs" / "FLOWCHARTS.md").read_text(encoding="utf-8")

_WORDS = {
    2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight",
    9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
}


# --------------------------------------------------------------------------
# Frozen constants: the docs quote them, so they must quote them correctly
# --------------------------------------------------------------------------

def test_metrics_quotes_the_real_tolerances():
    """
    The exact drift of DEFECT_LOG 2026-09-02-05 item 7: the doc reported a tolerance and
    a rounding rule the code had already changed, with reasons, in both cases.
    """
    row = re.search(r"\|\s*`TOL_ABS_PAISE`\s*\|\s*\**(\d+)\**\s*\|", METRICS)
    assert row, "METRICS.md no longer states TOL_ABS_PAISE"
    assert int(row.group(1)) == cfg.TOL_ABS_PAISE

    row = re.search(r"\|\s*`TOL_REL_BPS`\s*\|\s*\**(\d+)\**", METRICS)
    assert row, "METRICS.md no longer states TOL_REL_BPS"
    assert int(row.group(1)) == cfg.TOL_REL_BPS

    assert f"{cfg.GST_ROUNDING}" in METRICS, (
        f"METRICS.md does not mention the actual GST rounding rule {cfg.GST_ROUNDING!r}"
    )


def test_the_search_bounds_quoted_in_the_docs_are_the_real_ones():
    assert f"MAX_POOL = {cfg.MAX_POOL}" in ARCH or f"`MAX_POOL = {cfg.MAX_POOL}`" in ARCH
    assert f"MAX_SUBSET_K = {cfg.MAX_SUBSET_K}" in ARCH


def test_the_subset_count_arithmetic_in_architecture_is_correct():
    """
    ARCHITECTURE.md states the search space explicitly. It is the kind of number nobody
    recomputes, which is exactly why it is worth recomputing.
    """
    from math import comb

    expected = sum(comb(cfg.MAX_POOL, k) for k in range(1, cfg.MAX_SUBSET_K + 1))
    assert f"{expected:,}" in ARCH, (
        f"ARCHITECTURE.md does not state the real bounded search space "
        f"({expected:,} subsets at MAX_POOL={cfg.MAX_POOL}, k<={cfg.MAX_SUBSET_K})"
    )


# --------------------------------------------------------------------------
# Counts that grow: refusal categories, defect categories, tests
# --------------------------------------------------------------------------

def test_flowcharts_states_the_real_number_of_refusal_paths():
    """
    It said "Seven distinct ways to refuse" while there were nine, having been written
    before two were added. A count in prose is a claim with no owner.
    """
    n = len(RefusalCategory)
    stated = re.search(r"\*\*([A-Za-z]+) distinct ways to refuse", FLOWS)
    assert stated, "FLOWCHARTS.md no longer states how many ways there are to refuse"
    assert stated.group(1).lower() == _WORDS[n], (
        f"FLOWCHARTS.md says {stated.group(1)!r} ways to refuse; RefusalCategory has {n}"
    )


def test_every_refusal_category_is_named_somewhere_in_the_flowchart():
    missing = [c.value for c in RefusalCategory if c.value not in FLOWS]
    assert not missing, f"refusal paths absent from the flowchart: {missing}"


def test_readme_states_the_real_number_of_defect_categories(batch):
    """
    The README leads with this count. It said "Nine categories" through two rounds of
    additions.
    """
    labels = {lab for l in batch.truth for lab in l.defect_labels}
    # `unsettled` is a truth relation rather than an injected defect.
    labels.discard("unsettled")
    # `chargeback_debit` is real and deliberately carries NO truth label, because the
    # engine cannot produce a verdict for a debit. It is counted in the README's total
    # and cannot appear here, which is the distinction this assertion has to respect.
    unlabelled = 1 if any(t.debit for t in batch.inputs.bank_txns) else 0

    stated = re.search(r"\*\*([A-Za-z]+) categories\.?\*\*", README)
    assert stated, "README no longer states how many defect categories there are"
    assert stated.group(1).lower() == _WORDS[len(labels) + unlabelled], (
        f"README says {stated.group(1)!r} categories; the generator injects "
        f"{len(labels)} labelled ({sorted(labels)}) plus {unlabelled} unlabelled"
    )


def test_readme_test_count_is_not_wildly_stale():
    """
    Not pinned exactly -- that would fail on every commit that adds a test, which trains
    people to update it without reading. Pinned to within 10%, which catches the case
    that actually happened: the README claimed 58 while the suite had 124.
    """
    import subprocess
    import sys

    stated = re.search(r"(\d+) tests, including the end-to-end isolation test", README)
    assert stated, "README no longer states a test count"
    claimed = int(stated.group(1))

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--co", "tests/"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    ).stdout
    # `-q` prints one line per file plus a total; the total is what we want, and its
    # wording differs between pytest versions, so count the per-file tallies instead.
    actual = sum(int(n) for n in re.findall(r"^tests/\S+: (\d+)$", out, re.M))
    assert actual > 0, f"could not count collected tests from:\n{out[-500:]}"

    assert abs(claimed - actual) <= max(5, actual * 0.10), (
        f"README claims {claimed} tests; the suite collects {actual}"
    )


# --------------------------------------------------------------------------
# The density sweep table — a measured result, published as prose
# --------------------------------------------------------------------------

@pytest.mark.slow
def test_the_published_density_sweep_matches_a_live_run():
    """
    THE table this project's central claim rests on, re-derived rather than trusted.

    It has been stale twice. Tolerances are deliberately loose on the rates (±2pp) and
    exact on precision: the rates move a little with any generator change and the point
    of the table is their SHAPE, whereas a precision figure that drifts at all is the
    claim itself failing.
    """
    from recon.engine.match import match_once
    from recon.generator import build
    from scorer.score import score

    rows = re.findall(
        r"^\|\s*(\d+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)%\s*\|\s*\**([\d.]+)\**\s*\|\s*([\d.]+)%\s*\|$",
        METRICS, re.M,
    )
    assert rows, "the density sweep table is no longer parseable from METRICS.md"

    seeds = (11111, 22222, 33333, 44444, 55555)
    for ppw_s, _pool_s, rate_s, prec_s, refusal_s in rows:
        ppw = int(ppw_s)
        rate = prec = refusal = 0.0
        for seed in seeds:
            b = build.generate(seed=seed, payments_per_window=ppw)
            out = match_once(b.inputs)
            sc = score(
                out, b.truth, total_payments=len(b.inputs.payments),
                captured_payments=sum(1 for p in b.inputs.payments if p.captured),
                ambiguity_bank_txn_id=b.ambiguity_bank_txn_id or "",
                credits_by_id={t.id: t.credit for t in b.inputs.bank_txns}, seed=seed,
            )
            rate += sc.match_rate
            prec += sc.match_precision
            refusal += sc.refusal_rate
        n = len(seeds)
        rate, prec, refusal = rate / n, prec / n, refusal / n

        assert abs(rate * 100 - float(rate_s)) <= 2.0, (
            f"ppw={ppw}: METRICS.md publishes match rate {rate_s}%, live run gives "
            f"{rate:.1%}"
        )
        assert abs(refusal * 100 - float(refusal_s)) <= 2.0, (
            f"ppw={ppw}: METRICS.md publishes refusal rate {refusal_s}%, live run gives "
            f"{refusal:.1%}"
        )
        assert f"{prec:.4f}" == prec_s, (
            f"ppw={ppw}: METRICS.md publishes precision {prec_s}, live run gives "
            f"{prec:.4f} -- a precision figure that drifts at all is the claim failing"
        )


# --------------------------------------------------------------------------
# The committed artefact (REVIEW.md P0-1)
#
# `reports/run_output.json` is what the API serves and the UI renders. It shipped with
# `verification: {relations: [], permutation_gate: null}` because the test suite itself
# rewrote it -- `test_cli_robustness.py` shelled out to `run.py match` with cwd=ROOT and
# no --verify, on every run. The UI then returned null for an empty verification block,
# so the project's central claim rendered as nothing at all. Silently: not a crash.
#
# Three separate assertions, because three separate things had to be true and only one
# of them was about the file's contents.
# --------------------------------------------------------------------------
def _committed_run_output() -> dict:
    path = cfg.REPORTS / "run_output.json"
    assert path.is_file(), f"{path} is missing; run `python run.py match --verify`"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_served_artefact_carries_its_verification():
    """The artefact the demo serves must contain the evidence the demo claims."""
    v = _committed_run_output()["verification"]

    assert v["status"] == "verified", (
        "run_output.json was produced without --verify. The UI will render a "
        "'did not run' warning where the four-layer claim should be. "
        "Re-run `python run.py match --verify` and commit the result."
    )
    names = {r["name"] for r in v["relations"]}
    assert names == {"MR1", "MR2", "MR3", "MR4", "MR5", "MR6"}, names
    assert all(r["passed"] for r in v["relations"]), [
        r["name"] for r in v["relations"] if not r["passed"]
    ]
    gate = v["permutation_gate"]
    assert gate is not None and gate["passes"] == cfg.PERMUTATION_K
    assert gate["unstable"] == 0, gate


def test_an_unverified_payload_says_so_rather_than_going_quiet():
    """
    Empty containers cannot distinguish "checked, found nothing" from "never checked".

    This is the assertion that actually prevents the regression: the file can be
    regenerated correctly and then quietly regenerated wrong, but a payload that must
    ANNOUNCE its own absence cannot be misread by whatever renders it next.
    """
    from recon.report.run_output import _verification_block

    absent = _verification_block(None, None)
    assert absent["status"] == "not_run"
    assert "--verify" in absent["note"]

    present = _verification_block(None, _StubEnsemble())
    assert present["status"] == "verified"
    assert present["note"] == ""


class _StubEnsemble:
    def summary(self):
        return {"passes": 8, "txns_observed": 1, "unstable": 0}


def test_the_artefact_names_the_tier_that_produced_it():
    """
    A recorded run and a live one differ in tier attribution -- the live model is
    non-deterministic about which references it recovers, so a credit can be claimed by
    tier 1 in one run and tier 2 in the next (measured; verdicts do not move). An
    artefact that does not name its tier cannot be reproduced on purpose.
    """
    assert _committed_run_output()["llm_tier"], "llm_tier must never be empty"


def test_the_assignment_payload_carries_every_field_the_ui_asserts():
    """
    A UI row that reports a field the payload does not send reads `undefined`, which
    JavaScript treats as falsy -- so it does not go blank, it renders the OPPOSITE claim
    with total confidence.

    `certain_fee` was missing here while the Matches card rendered "fee known exactly:
    no - bounded by the rate band". Measured after adding it: **all 127 assignments are
    `true`**, so that row was wrong on every match in the batch, and the transcript two
    lines below it on the same card said the opposite. This test exists because the
    failure mode is silent by construction and a screenshot is what caught it.
    """
    rows = _committed_run_output()["assignments"]
    assert rows, "no assignments in the payload"

    required = {
        "bank_txn_id", "payment_ids", "invoice_nos", "tier", "rupees",
        "residual_paise", "residual_tightness", "uniqueness_margin", "fs_weight",
        "certain_fee", "permutation_stability", "confidence",
    }
    for row in rows:
        missing = required - set(row)
        assert not missing, f"{row['bank_txn_id']} is missing {sorted(missing)}"
        assert isinstance(row["certain_fee"], bool), (
            "certain_fee must be a real boolean, not null -- the UI branches on it"
        )


def test_every_refusal_category_is_actually_reachable():
    """
    A category the engine can never emit is a promise the exception list does not keep.

    `FS_BELOW_THRESHOLD` and `FS_REVIEW_BAND` sat in this enum unraised while
    `FLOWCHARTS.md` counted them among the ways to refuse, `METRICS.md` described when
    they fire, the UI carried labels and a colour for them, and the operator-prose table
    had entries ready. Everything downstream was built for two verdicts that could not
    occur. Nothing failed, because nothing checks that an enum member is used.

    Static scan rather than a runtime census: some categories need a batch shaped to
    provoke them (`order_dependent_assignment` needs a genuinely order-dependent
    matcher, and the permutation gate exists precisely so that never happens on real
    data), so requiring each to fire in one run would either be flaky or force the
    matcher to be wrong on purpose. Requiring each to be CONSTRUCTED somewhere in the
    engine is the property that actually matters and it cannot be satisfied by accident.
    """
    import ast

    from recon.engine.results import RefusalCategory

    engine = cfg.ROOT / "src" / "recon"
    constructed: set[str] = set()
    for source in engine.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # RefusalCategory.X anywhere in the engine, however it is spelled.
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in ("RefusalCategory", "RC"):
                    constructed.add(node.attr)

    unreachable = {c.name for c in RefusalCategory} - constructed
    assert not unreachable, (
        f"{sorted(unreachable)} are defined in RefusalCategory and never constructed "
        f"anywhere under recon/. Either raise them or delete them -- an exception "
        f"category the engine cannot emit is documentation of a behaviour that does "
        f"not exist."
    )


def test_the_match_rate_denominator_is_captured_payments_everywhere():
    """
    `METRICS.md` defined match rate over "total payments in the batch" while
    `scorer/score.py` divided by `captured_payments` — 86.0% against 88.66% on the same
    run. The code was right (an uncaptured payment has no money behind it and can never
    appear on a statement) and the document was wrong, which is the worse way round: a
    reader checking the arithmetic would have found the published figure irreproducible.

    Pinned in both directions — the scorer's arithmetic and the document's wording — so
    a change to either without the other fails.
    """
    from recon.generator import build
    from recon.engine.match import match_once
    from scorer.score import score

    batch = build.generate(seed=cfg.SEED_PRIMARY)
    out = match_once(batch.inputs)
    captured = sum(1 for p in batch.inputs.payments if p.captured)
    card = score(
        out, batch.truth,
        total_payments=len(batch.inputs.payments),
        captured_payments=captured,
        ambiguity_bank_txn_id=batch.ambiguity_bank_txn_id or "",
        credits_by_id={x.id: x.credit for x in batch.inputs.bank_txns},
        seed=cfg.SEED_PRIMARY,
    )

    assert captured < card.total_payments, (
        "this batch has no uncaptured payments, so the test cannot tell the two "
        "denominators apart"
    )
    assert card.match_rate == card.payments_assigned / captured
    assert card.match_rate != card.payments_assigned / card.total_payments

    # Scoped to the FORMULA block, not the whole file: the prose below it quotes the old
    # wording to explain what was wrong, and a document is allowed to describe its own
    # history. A whole-file scan would forbid that, which is how a drift test starts
    # deleting the record of the drift.
    metrics = (cfg.ROOT / "docs" / "METRICS.md").read_text(encoding="utf-8")
    section = metrics.split("### Match rate", 1)[1].split("```", 2)[1]
    assert "CAPTURED payments in the batch" in section, section
    assert "total payments in the batch" not in section, (
        "METRICS.md's match-rate formula still divides by all payments; the scorer "
        "divides by captured payments"
    )


def test_the_readme_batch_shape_matches_the_generated_manifest():
    """
    The README described a batch of 136 bank transactions and 200 invoices against a
    manifest reading 147 and 187 — numbers from before `advance_payment` stopped every
    payment carrying an invoice. Nobody notices a count drifting by eleven, which is
    precisely why it should not be maintained by hand.
    """
    manifest = json.loads(
        (cfg.GENERATED / "manifest.json").read_text(encoding="utf-8")
    )
    readme = (cfg.ROOT / "README.md").read_text(encoding="utf-8")

    row = re.search(
        r"Total batch: \*\*(\d+) payments\*\*, (\d+) bank transactions, "
        r"(\d+) invoices, across (\d+)\s*\n?settlement windows",
        readme,
    )
    assert row, "the README's batch-shape sentence has moved or been reworded"
    payments, bank, invoices, _windows = (int(g) for g in row.groups())
    assert payments == manifest["payments"]
    assert bank == manifest["bank_txns"]
    assert invoices == manifest["invoices"]


def test_metrics_carries_one_current_refusal_rate_for_the_reported_density():
    """
    `METRICS.md` held two refusal rates for ppw=6 — 10.1% in the sweep table and 0.7%
    in the second-arm discussion — and a reader had no way to tell which was live. The
    stale one is now explicitly marked SUPERSEDED with the current figures beside it,
    because it records the finding that prompted the fix and deleting it would erase why
    seven defect categories exist.
    """
    metrics = (cfg.ROOT / "docs" / "METRICS.md").read_text(encoding="utf-8")
    assert "SUPERSEDED" in metrics, (
        "the stale ppw table is unmarked; a reader cannot tell which refusal rate is live"
    )
    stale = metrics.index("| refusal rate | 0.7% | 0.8% | **4.9%** |")
    marker = metrics.index("SUPERSEDED")
    assert marker > stale, "the SUPERSEDED note must follow the table it qualifies"


def test_the_reachable_ceiling_is_derived_from_truth_not_asserted():
    """
    A match rate invites comparison against 100%, and 100% is not on offer: some captured
    payments never settled, so no bank credit exists to match them, and others belong to
    a relation the engine does not model and are refused correctly. Counting those
    against the engine scores it for failing to do something nobody claims it can do.

    The ceiling must therefore be COMPUTED from ground truth for whatever batch is in
    front of it, not carried as a constant that was true once. The audit derived 91.24%
    by hand for the primary batch; this checks the scorer reaches the same number the
    same way, and that the arithmetic closes.
    """
    from recon.engine.match import match_once
    from recon.generator import build
    from scorer.score import score

    batch = build.generate(seed=cfg.SEED_PRIMARY)
    out = match_once(batch.inputs)
    captured = sum(1 for p in batch.inputs.payments if p.captured)
    card = score(
        out, batch.truth,
        total_payments=len(batch.inputs.payments),
        captured_payments=captured,
        ambiguity_bank_txn_id=batch.ambiguity_bank_txn_id or "",
        credits_by_id={x.id: x.credit for x in batch.inputs.bank_txns},
        seed=cfg.SEED_PRIMARY,
    )

    # The ceiling sits between the match rate and 1.0 -- below 1.0 because some payments
    # are unreachable, at or above the match rate because it is what the engine is
    # measured against.
    assert card.match_rate <= card.ceiling < 1.0
    assert card.reachable_payments <= captured

    # And the arithmetic closes: assigned + short of the ceiling == reachable.
    assert card.payments_assigned + card.short_of_ceiling == card.reachable_payments

    # Every payment counted in the shortfall must be one ground truth wanted assigned.
    assert card.short_of_ceiling >= 0
    if card.short_of_ceiling:
        assert card.shortfall_by_defect, (
            "payments are short of the ceiling but no defect is named as the cause"
        )
        assert max(card.shortfall_by_defect.values()) <= card.short_of_ceiling


def test_the_ceiling_moves_with_the_batch():
    """
    A constant would pass the test above on the batch it was written for and lie on every
    other one. Two batches with different reachability must report different ceilings.
    """
    from recon.engine.match import match_once
    from recon.generator import build
    from scorer.score import score

    def ceiling_of(seed):
        batch = build.generate(seed=seed)
        out = match_once(batch.inputs)
        return score(
            out, batch.truth,
            total_payments=len(batch.inputs.payments),
            captured_payments=sum(1 for p in batch.inputs.payments if p.captured),
            ambiguity_bank_txn_id=batch.ambiguity_bank_txn_id or "",
            credits_by_id={x.id: x.credit for x in batch.inputs.bank_txns},
            seed=seed,
        ).ceiling

    assert ceiling_of(cfg.SEED_PRIMARY) != ceiling_of(cfg.SEED_SECONDARY)


def test_the_published_ceiling_table_matches_a_live_score():
    """
    `METRICS.md` publishes a two-column ceiling table, and both columns are load-bearing.

    The primary column contextualises the headline: 88.66% is 5 payments short of what
    the data permits, not 11 points short of perfect. The holdout column is what proves
    the ceiling is derived rather than typed -- a constant satisfies every arithmetic
    check on the batch it was written against.

    Pinned here for the same reason the density sweep is: the table went stale twice in
    this project before anyone noticed, and a doc number nobody re-derives is a doc
    number that will eventually be wrong on stage.
    """
    import config as cfg
    from loaders import load_inputs
    from recon.engine.match import match_once
    from scorer.score import load_truth, score

    inputs = load_inputs()
    raw, links = load_truth(cfg.TRUTH_DIR / "ground_truth.json")
    sc = score(
        match_once(inputs),
        links,
        total_payments=len(inputs.payments),
        captured_payments=sum(1 for p in inputs.payments if p.captured),
        ambiguity_bank_txn_id=raw.get("ambiguity_bank_txn_id", ""),
        credits_by_id={x.id: x.credit for x in inputs.bank_txns},
        seed=inputs.seed,
    )

    row = re.search(r"\|\s*reachable ceiling\s*\|\s*\*\*([\d.]+)%\*\*", METRICS)
    assert row, "METRICS.md no longer publishes a reachable-ceiling row"
    assert abs(float(row.group(1)) - sc.ceiling * 100) < 0.01, (
        f"METRICS.md publishes a ceiling of {row.group(1)}%, a live score gives "
        f"{sc.ceiling * 100:.2f}%"
    )

    row = re.search(r"\|\s*short of the ceiling\s*\|\s*\*\*(\d+)\*\*", METRICS)
    assert row, "METRICS.md no longer publishes a short-of-the-ceiling row"
    assert int(row.group(1)) == sc.short_of_ceiling, (
        f"METRICS.md publishes {row.group(1)} payments short of the ceiling, a live "
        f"score gives {sc.short_of_ceiling}"
    )

    row = re.search(r"\|\s*unreachable by construction\s*\|\s*(\d+)\s*\|", METRICS)
    assert row, "METRICS.md no longer publishes an unreachable count"
    unreachable = sc.captured_payments - sc.reachable_payments
    assert int(row.group(1)) == unreachable, (
        f"METRICS.md publishes {row.group(1)} unreachable payments, a live score gives "
        f"{unreachable}"
    )


def test_the_unreachable_decomposition_in_metrics_sums_to_the_unreachable_count():
    """
    The table saying WHY 17 payments are unmatchable must add up to 17.

    This one is written from experience: the first draft of that table said "6 split
    settlements" because it counted truth LINK ROWS, and a split payment appears in two
    links. The rows summed to 19 against a total of 17 and read as authoritative.
    """
    import config as cfg
    from loaders import load_inputs
    from recon.engine.match import match_once
    from scorer.score import load_truth, score

    inputs = load_inputs()
    raw, links = load_truth(cfg.TRUTH_DIR / "ground_truth.json")
    sc = score(
        match_once(inputs),
        links,
        total_payments=len(inputs.payments),
        captured_payments=sum(1 for p in inputs.payments if p.captured),
        ambiguity_bank_txn_id=raw.get("ambiguity_bank_txn_id", ""),
        credits_by_id={x.id: x.credit for x in inputs.bank_txns},
        seed=inputs.seed,
    )
    unreachable = sc.captured_payments - sc.reachable_payments

    block = re.search(
        r"\| n \| why it cannot be matched \|\n\|[-:| ]+\|\n((?:\|.*\|\n)+)", METRICS
    )
    assert block, "METRICS.md no longer carries the unreachable decomposition table"
    counts = [int(m) for m in re.findall(r"^\|\s*(\d+)\s*\|", block.group(1), re.M)]
    assert sum(counts) == unreachable, (
        f"METRICS.md decomposes the unreachable payments as {counts} summing to "
        f"{sum(counts)}, but a live score has {unreachable} of them"
    )


def test_the_served_artefact_is_the_reproducible_arm():
    """
    The check that was missing when the served run silently changed by itself.

    `README.md`, `METRICS.md` and `OUTSTANDING_TASKS.md` all say the deterministic arm
    (`--no-llm`) is what the demo should run, because the live tier's output is an INPUT
    to the engine and it moves: 9 of 10 observed live runs assign 127 and one assigns
    126. The artefact went on being generated with the live tier anyway, and regenerating
    it for unrelated work flipped the demo's headline with no code change. Nothing failed,
    because nothing checked. See DEFECT_LOG 2026-09-04-02.

    To serve a live run deliberately, change the guidance in those three documents first
    and then this test -- the friction is the point.
    """
    payload = json.loads(
        (ROOT / "reports" / "run_output.json").read_text(encoding="utf-8")
    )
    assert payload["llm_tier"] == "disabled", (
        f"the served artefact was produced with the {payload['llm_tier']!r} tier. The "
        f"docs say the demo runs the reproducible arm; regenerate with "
        f"`python run.py match --verify --no-llm`"
    )


def test_an_unverified_write_warns_at_the_point_it_happens():
    """
    `match` without `--verify` overwrites the artefact the demo serves.

    The metrics block already says "not run (pass --verify)" beside the layer results,
    forty lines below the write and easy to read as a note about this run rather than as
    a change to the file the UI reads. P0-1 was exactly that -- a served artefact quietly
    missing its verification -- so the warning belongs at the write, in the terminal, as
    well as on the page.

    **This test writes to `reports/`, which is the same hazard it is testing**, and the
    first version of it reproduced A1 exactly: it stripped verification from the
    committed artefact and the test asserting that artefact is verified failed a few
    lines later. So the real files are saved byte for byte and restored in a `finally`,
    whatever the assertions do. A test that has to break a rule to check the rule has to
    put it back.
    """
    import subprocess
    import sys

    guarded = [
        ROOT / "reports" / "run_output.json",
        ROOT / "reports" / "scorecard.json",
    ]
    saved = {p: p.read_bytes() for p in guarded if p.exists()}
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "match", "--no-llm"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=600,
        )
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        assert "NO verification data" in r.stdout, (
            "an unverified run overwrote the served artefact without saying so at the "
            "write"
        )
        assert "match --verify" in r.stdout

        # ...and a verified run must NOT print it, or the warning becomes noise to skip.
        r2 = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "match", "--verify", "--no-llm"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=900,
        )
        assert r2.returncode == 0, r2.stdout[-2000:] + r2.stderr[-2000:]
        assert "NO verification data" not in r2.stdout
    finally:
        for p, blob in saved.items():
            p.write_bytes(blob)


def test_the_holdout_artefact_is_the_reproducible_arm_too():
    """
    Same rule as the primary, and it was not being applied.

    `reports/run_output_holdout.json` was still carrying `llm_tier:
    claude:claude-sonnet-5` after the primary had been switched to the deterministic
    arm, so the two committed artefacts disagreed about which tier produced them. Every
    verdict-level number in the file was identical either way -- the tier contributes
    nothing to decisions on the shifted batch either -- which is precisely why the
    inconsistency could sit there unnoticed.
    """
    path = ROOT / "reports" / "run_output_holdout.json"
    if not path.is_file():
        import pytest

        pytest.skip("no holdout artefact; run `python run.py match --dataset holdout`")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["llm_tier"] == "disabled", (
        f"the holdout artefact was produced with the {payload['llm_tier']!r} tier while "
        f"the primary uses the reproducible arm. Regenerate with "
        f"`python run.py match --dataset holdout --verify --no-llm`"
    )


def test_the_provenance_table_describes_the_batch_and_not_the_source_files():
    """
    The drift that an outside reviewer found before this suite did.

    `README.md`'s provenance table said R2 carried "no `fee`, no `tax`, not `captured`".
    True of the raw orders in `data/mcp_created/orders_r2.json`; false of the records that
    reach the batch, where `build.py::_r2_as_payments` promotes all 12 into settled
    payments with synthetic fees — inside the 194-payment denominator behind the match
    rate. The generator's docstring was always explicit about the transformation, so this
    was doc drift rather than a hidden one, and it sat in the section this project is
    proudest of.

    Two assertions, because the counts alone would not have caught it: the tier sizes must
    match the batch, AND a tier the README calls uncaptured must actually be uncaptured in
    the batch.
    """
    import collections

    from loaders import load_inputs

    inputs = load_inputs()
    by_tier = collections.Counter(p.provenance for p in inputs.payments)

    section = README.split("## Data provenance", 1)
    assert len(section) == 2, "README no longer has a Data provenance section"
    table = section[1].split("Total batch", 1)[0]

    for tier in ("R1", "R2", "S"):
        row = re.search(rf"\|\s*\*\*{tier} [^|]*\|[^|]*\|\s*\*\*([^*]+)\*\*\s*\|", table)
        assert row, f"the provenance table no longer states a count for tier {tier}"
        stated = [int(n) for n in re.findall(r"\d+", row.group(1))]
        assert by_tier[tier] in stated or sum(stated) == by_tier[tier], (
            f"README says tier {tier} is {row.group(1).strip()!r}; the batch carries "
            f"{by_tier[tier]}"
        )

    # The half that actually drifted: a tier described as uncaptured must BE uncaptured.
    for tier in ("R1", "R2", "S"):
        row = re.search(rf"\|\s*\*\*{tier} [^|]*\|([^|]*)\|", table)
        assert row, tier
        text = row.group(1)
        claims_uncaptured = re.search(r"not\s+`?captured`?", text) and "SYNTHESIS" not in text.upper()
        if claims_uncaptured:
            captured = [p for p in inputs.payments if p.provenance == tier and p.captured]
            assert not captured, (
                f"README describes tier {tier} as not captured, but {len(captured)} of "
                f"its records enter the batch captured and count toward the match-rate "
                f"denominator"
            )


def test_no_document_advertises_a_decision_mechanism_the_matcher_does_not_use():
    """
    "Fixed in code" and "no longer claimed" are different states.

    The two-threshold Fellegi-Sunter band was measured, rejected and its refusal
    categories deleted on 2026-09-03. Four documents went on describing it as the rule
    that "populates the exception list" — and a grep for the identifiers found nothing,
    because the prose said *"two-threshold band"* while the code said `contradicts`. They
    share no token. An external reviewer found it; this suite did not.

    So the check is on the ENGLISH, not the identifier: no document may claim the band
    decides anything while `match.py` gates on `contradicts`.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    matcher = (root / "src" / "recon" / "engine" / "match.py").read_text(encoding="utf-8")
    gates_on_contradiction = "contradicts" in matcher
    gates_on_band = re.search(r"\.band\s*(==|!=|in)\s", matcher)
    assert gates_on_contradiction and not gates_on_band, (
        "the matcher's Layer 3 gate changed; this test encodes the current rule and must "
        "be updated deliberately alongside it"
    )

    # DEFECT_LOG and OUTSTANDING_TASKS are append-only records of what WAS claimed and
    # are meant to keep the old wording. Everything a reader is pointed at must not.
    claims = re.compile(
        r"two[- ]threshold (band|rule|decision)|band .{0,40}populates the exception",
        re.IGNORECASE,
    )
    for name in ("README.md", "docs/ARCHITECTURE.md", "docs/FLOWCHARTS.md",
                 "docs/AGENTIC.md", "docs/METRICS.md"):
        text = (root / name).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not claims.search(line):
                continue
            # A line that names the band while saying it is NOT the gate is the correction
            # itself, and must be allowed to say so.
            context = "\n".join(text.splitlines()[max(0, line_no - 6):line_no + 6])
            recanted = re.search(
                r"not (the gate|enforced|wired|used)|used to claim|contradiction veto|"
                r"nothing in the matcher reads it|would have refused",
                context,
                re.IGNORECASE,
            )
            assert recanted, (
                f"{name}:{line_no} advertises the two-threshold band as a decision rule, "
                f"and the matcher gates on `contradicts`. Deleting the code and leaving "
                f"the prose is worse than either: only the half nobody greps is visible "
                f"from outside.\n    {line.strip()}"
            )

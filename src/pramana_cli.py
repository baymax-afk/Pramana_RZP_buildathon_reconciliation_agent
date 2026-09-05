#!/usr/bin/env python
"""
Single entry point for the reconciliation engine.

    python run.py generate --seed 20260905
    python run.py generate --density-sweep

`run.py` is the ONLY module that touches the filesystem on the engine's behalf. It
loads the three sides and hands the engine dataclasses; the engine never receives a
path, which is the primary enforcement of the ground-truth isolation boundary.

Subcommands for matching, verification and scoring land with those blocks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# The project installs as a package (`pip install -e .`), so `config`, `loaders`,
# `recon` and `scorer` import normally. This file previously inserted the repo root and
# `src/` into sys.path before its own imports, which made the entry points work only
# from inside a checkout and put four `# noqa: E402` comments on the imports to hide the
# consequence. Running from source without installing still works via `pytest.ini`'s
# pythonpath for tests, and `python -m` from the repo root for the CLI.
try:
    import config as cfg
    from loaders import load_inputs
    from recon.engine.match import match_once
    from recon.generator import build
except ModuleNotFoundError as _e:  # pragma: no cover - first-run guard
    # Deliberately an ERROR rather than a sys.path fix-up. Silently repairing the path
    # is what this file used to do, and it hid the fact that the project was not
    # actually installable -- everything worked from the checkout and nothing worked
    # anywhere else. A one-line instruction is a better answer than a bare traceback.
    raise SystemExit(
        f"{_e}\n\n"
        "Pramana is not installed. From the repository root:\n"
        "    pip install -e .          # engine, generator, scorer, CLI\n"
        "    pip install -e '.[api]'   # ...plus the read-only API\n"
    ) from None


def _print_block(title: str, rows: list[tuple[str, object]]) -> None:
    width = max((len(k) for k, _ in rows), default=0)
    print(f"\n{title}")
    print("-" * max(len(title), width + 24))
    for k, v in rows:
        print(f"  {k:<{width}}  {v}")


def cmd_holdout(args: argparse.Namespace) -> int:
    """
    Build the shifted held-out set, once, and freeze it.

    Deliberately a separate command rather than a flag on `generate`: regenerating a
    holdout is a decision, and a decision that happens by muscle memory is not one. The
    hash of what it writes is pinned by `tests/test_holdout.py`, so a set quietly rebuilt
    after a disappointing number fails the suite.
    """
    from recon.generator import build as _build
    from recon.generator import holdout as _holdout

    batch = _build.generate(seed=cfg.HOLDOUT_SEED, payments_per_window=cfg.HOLDOUT_PPW)
    shifted, info = _holdout.shift(batch, cfg.HOLDOUT_SEED)
    report = _holdout.assert_wellformed(shifted, info["unreachable_bank_txn_ids"])

    _print_block(
        f"HOLDOUT  seed={cfg.HOLDOUT_SEED}  ppw={cfg.HOLDOUT_PPW}",
        [
            ("payments", len(shifted.inputs.payments)),
            ("bank transactions", len(shifted.inputs.bank_txns)),
            ("invoices", len(shifted.inputs.invoices)),
        ],
    )
    _print_block(
        "DISTRIBUTION SHIFT  (what the engine was not built against)",
        [
            ("narrations in unseen formats", info["narrations_reformatted"]),
            ("adversarial free text", info["adversarial_narrations"]),
            ("references duplicated across days", info["cross_day_duplicate_refs"]),
            ("drifted past the engine's lookback", info["drifted_past_lookback"]),
            (
                "chargebacks whose reference was overwritten",
                info.get("reversals_orphaned_by_ref_shift", 0),
            ),
        ],
    )
    _print_block(
        "WELL-FORMEDNESS",
        [
            ("assign links", report["assign_links"]),
            (
                "deliberately unreachable",
                f"{report['deliberately_unreachable']}  "
                f"(counted, not relabelled -- these are real misses)",
            ),
        ],
    )

    if args.write:
        paths = _build.write(shifted, out_dir=cfg.HOLDOUT)
        _print_block(
            "WRITTEN", [(k, str(v.relative_to(cfg.ROOT))) for k, v in paths.items()]
        )
        print(
            "\n  FROZEN. No constant in config.py may be changed in response to a\n"
            "  holdout result -- a tolerance fitted to the evaluation data measures\n"
            "  nothing. The one change a holdout may motivate is a correctness fix."
        )
    return 0


def _load_dotenv() -> list[str]:
    """
    Load `.env` into the environment, without overriding anything already set.

    **Nothing read this file until now.** `.gitignore` describes it as "Secrets. The LLM
    tier and the Razorpay MCP both read from here", `OUTSTANDING_TASKS.md` instructed the
    reader to put `ANTHROPIC_API_KEY` and `ANTHROPIC_WORKSPACE_ID` in it, and
    `recon.llm.select()` then read `os.environ` and found nothing -- so it silently chose
    the offline stand-in and every reported run said `llm=recorded`.

    That did not produce a false claim, because `llm-compare` names the active tier and
    refuses to call a stand-in comparison valid. But it did mean following the documented
    instructions had no effect, which is the same doc-vs-code gap this project keeps
    finding, on the one file whose entire purpose is to be read.

    Deliberately stdlib-only and about twenty lines. The engine, its four verification
    layers and the scorer have NO third-party dependencies, and a secrets loader in the
    CLI is not worth being the first -- `python-dotenv` would put a package in the
    dependency graph of a system whose main claim is that money decisions are made by
    code you can read end to end.

    An already-exported variable always wins, so CI and a one-off `ANTHROPIC_API_KEY=...
    python run.py ...` behave the way anyone would expect.
    """
    path = cfg.ROOT / ".env"
    if not path.exists():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def cmd_generate(args: argparse.Namespace) -> int:
    densities = list(cfg.DENSITY_SWEEP) if args.density_sweep else [args.payments_per_window]
    rc = 0

    for ppw in densities:
        t0 = time.perf_counter()
        batch = build.generate(
            seed=args.seed, n_payments=args.n, payments_per_window=ppw
        )
        elapsed = time.perf_counter() - t0

        # Anti-accident assertions. These FAIL the build; they do not warn.
        checks: list[tuple[str, object]] = []
        try:
            n_cand = build.assert_ambiguity_is_exact(batch)
            checks.append(("ambiguity candidates", f"{n_cand} (exactly {cfg.AMBIGUITY_EXPECTED_CANDIDATES} required)"))
        except AssertionError as e:
            checks.append(("ambiguity", f"FAILED -- {e}"))
            rc = 1
        try:
            tol, smallest = build.assert_tolerance_sanity(batch)
            checks.append(("tolerance margin", f"{smallest // tol}x  ({tol}p vs smallest net {smallest}p)"))
        except AssertionError as e:
            checks.append(("tolerance", f"FAILED -- {e}"))
            rc = 1
        try:
            n_links = build.assert_truth_is_satisfiable(batch)
            checks.append(("truth satisfiable", f"{n_links} assign-links, all reachable"))
        except AssertionError as e:
            checks.append(("truth satisfiable", f"FAILED -- {e}"))
            rc = 1
        try:
            worst = build.assert_pool_bound(batch)
            note = "" if worst <= cfg.MAX_POOL else f"  [above MAX_POOL={cfg.MAX_POOL}; engine must refuse these]"
            checks.append(("worst window pool", f"{worst}{note}"))
        except AssertionError as e:
            checks.append(("density", f"FAILED -- {e}"))
            rc = 1

        s = batch.stats
        _print_block(
            f"GENERATED  seed={args.seed}  payments_per_window={ppw}",
            [
                ("payments", f"{s['payments']}  (captured {s['captured']})"),
                ("bank transactions", s["bank_txns"]),
                ("invoices", s["invoices"]),
                ("settlement windows", s["windows"]),
                ("provenance", s["provenance"]),
                ("expected refusals", s["refusals_expected"]),
                ("wall clock", f"{elapsed:.2f}s"),
            ]
            + checks,
        )
        _print_block(
            "DEFECTS INJECTED",
            sorted(s["defect_labels"].items(), key=lambda kv: -kv[1]),
        )

        if args.write and not args.density_sweep:
            paths = build.write(batch)
            _print_block(
                "WRITTEN",
                [(k, str(v.relative_to(cfg.ROOT))) for k, v in paths.items()],
            )
            print(
                "\n  Ground truth is written to _truth/ and is unreadable from inside\n"
                "  recon.engine -- enforced by an import-time audit hook and by the\n"
                "  engine's input type, which carries no paths. See tests/test_isolation.py."
            )

    return rc


def cmd_match(args: argparse.Namespace) -> int:
    """
    Match a generated batch and, unless --no-score, score it against ground truth.

    Note the ordering here: the engine runs to completion FIRST, producing a
    MatchOutput from `ReconInputs` alone, and only then is ground truth loaded -- by a
    different package, into a different object. There is no point in this function at
    which the engine could see the answer key.
    """
    generated_dir = cfg.HOLDOUT if getattr(args, "dataset", "") == "holdout" else None
    try:
        inputs = load_inputs(
            generated_dir=generated_dir,
            seed=args.seed,
            payments_per_window=args.payments_per_window,
        )
    except ValueError as e:
        # A seed/batch mismatch is a reporting error waiting to happen, not a crash to
        # trace. Say what is wrong and what to run.
        print(f"\n  {e}\n")
        return 1
    if generated_dir is not None and not (generated_dir / "manifest.json").is_file():
        print("\n  No holdout present -- run `python run.py holdout` first.\n")
        return 1

    # From here down, ALWAYS report `inputs.seed` / `inputs.payments_per_window`, never
    # `args.*`. `load_inputs` resolves both from the batch's manifest, so the two can
    # differ -- and when they did, the headline printed one seed while run_output.json
    # recorded another and took its density from a third place. A payload inconsistent
    # with itself is worse than a wrong one, because nothing about it looks wrong.
    seed = inputs.seed
    ppw = inputs.payments_per_window

    from recon.llm import select as _select_llm

    # The REPORTED run is deterministic by default, even when a live key is present.
    #
    # `_load_dotenv` made a key visible to `select()`, and `match` immediately started
    # using the live tier -- which changed reports/run_output.json, the artifact the API,
    # the UI and the submission all read. That artifact would then depend on a paid,
    # non-deterministic service, and nobody without a key could reproduce it. Measured
    # over five runs the live tier's field extraction varies (6-8 of 13 gaps filled)
    # while its verdicts do not, but "happens to be stable" is not the same guarantee as
    # "cannot vary", and the whole project rests on the second one.
    #
    # So the live tier is opt-in here via --live-llm. It is NOT hidden: the metrics block
    # prints the active tier on every run, and `llm-compare` exists precisely to exercise
    # the live model and report the difference as evidence.
    llm = _select_llm(disabled=args.no_llm, allow_live=args.live_llm)
    # Recording is inert -- see recon/explain/trace.py and the fingerprint test in
    # tests/test_explain.py. The transcript costs a dict per credit and buys the UI its
    # entire "why" view, so it is always on for a reported run.
    from recon.explain import Recorder

    recorder = Recorder()
    relations = ensemble = None
    t0 = time.perf_counter()
    if args.verify:
        # The engine's PRIMARY path: match under the permutation gate, so any
        # assignment decided by iteration order rather than by the data is refused
        # before it is ever reported.
        from recon.verify import metamorphic as mm
        from recon.verify.stability import match_gated

        k = cfg.PERMUTATION_K_FAST if args.fast else cfg.PERMUTATION_K
        out, ensemble = match_gated(inputs, k=k, llm=llm)
        # The gate replays the matcher K times over shuffled orderings; recording
        # all K would keep whichever pass happened to run last. Re-run once in the
        # canonical order to transcribe the result the gate actually returned.
        match_once(inputs, llm=llm, recorder=recorder)
        relations = mm.run_all(inputs, out, fast=args.fast)
    else:
        out = match_once(inputs, llm=llm, recorder=recorder)
    elapsed = time.perf_counter() - t0
    records = len(inputs.payments) + len(inputs.bank_txns) + len(inputs.invoices)

    if not args.score:
        _print_block("MATCHED (unscored)", list(out.summary().items()) + [
            ("by tier", out.tier_counts),
            ("wall clock", f"{elapsed:.2f}s"),
        ])
        return 0

    from scorer.report import render
    from scorer.score import load_truth, score

    truth_path = (
        (generated_dir / "_truth" / "ground_truth.json")
        if generated_dir is not None
        else cfg.TRUTH_DIR / "ground_truth.json"
    )
    if not truth_path.exists():
        print("\n  No ground truth present -- run `python run.py generate` first.")
        print("  (The engine ran fine without it; only scoring needs it.)")
        return 1

    raw, links = load_truth(truth_path)
    sc = score(
        out,
        links,
        total_payments=len(inputs.payments),
        captured_payments=sum(1 for p in inputs.payments if p.captured),
        ambiguity_bank_txn_id=raw.get("ambiguity_bank_txn_id", ""),
        throughput=records / elapsed if elapsed else None,
        credits_by_id={x.id: x.credit for x in inputs.bank_txns},
        seed=seed,
        # What the engine STILL does not read, re-derived every run rather than
        # asserted. This was every debit on the statement until Layer 2c; it is now the
        # lines that reach no verdict at all, and the arithmetic is deliberately
        # "everything minus what got a verdict" so that a future blind spot shows up
        # here by itself instead of needing someone to notice it.
        unexamined=_unexamined(inputs, out),
    )
    # The UI payload is built from the ENGINE's output only -- no ground truth, no
    # scoring. What a merchant sees is exactly what the engine could justify without
    # an answer key.
    from recon.report import run_output

    payload = run_output.build(
        inputs, out, seed=seed, elapsed_s=elapsed,
        relations=relations, ensemble=ensemble, llm=llm, recorder=recorder,
    )
    # The holdout writes to its OWN file. `reports/run_output.json` is what the API
    # serves and the UI renders, and a holdout run was silently replacing it -- so
    # scoring the shifted set left the demo showing the shifted set, with a seed and a
    # density nobody had asked for and no indication anything had changed.
    #
    # This is P0-1's failure shape again, from a new direction: the served artefact
    # being overwritten by something that had no business writing it. Found because the
    # exception count printed after a holdout run did not match the primary's.
    written = run_output.write(
        payload,
        (cfg.REPORTS / "run_output_holdout.json") if generated_dir is not None else None,
    )

    # Say it at the point of the write, not only in the metrics block below.
    #
    # `match` without `--verify` overwrites the artefact the API serves with one that
    # carries no verification data, and the metrics block's "not run (pass --verify)"
    # is 40 lines further down among the layer results -- easy to read as a note about
    # THIS run rather than as a change to the file the demo reads. The UI shows a
    # warning strip for exactly this state; a demo machine should not have to discover
    # it from the page. P0-1 was this shape and it cost a day.
    if payload.get("verification", {}).get("status") == "not_run":
        print(
            f"\n  NOTE: {written.name} now carries NO verification data, and the UI will\n"
            f"        show a warning strip instead of the four-layer result. Restore it\n"
            f"        with:  python run.py match --verify --no-llm\n"
        )

    # The scoring numbers travel SEPARATELY, in their own file, because run_output.json
    # is defined as what the engine could justify with no answer key and folding a
    # truth-derived number into it would make that unprovable by inspection. Same
    # holdout rule as above, for the same reason: a shifted run must not overwrite the
    # scorecard the demo reads.
    from scorer import artifact as _scorecard

    scorecard_path = cfg.REPORTS / (
        "scorecard_holdout.json" if generated_dir is not None else "scorecard.json"
    )
    _scorecard.write(
        _scorecard.build(sc, seed=seed, dataset=("holdout" if generated_dir is not None else "primary")),
        scorecard_path,
    )

    # ---- the comparison arm ----
    #
    # A single density in the headline invites the reading that these numbers are a
    # property of the ENGINE, when they are a property of the engine at one crowding
    # level. Density is the parameter the whole argument turns on -- coverage is meant
    # to degrade under ambiguity while precision holds -- and that is only visible with
    # more than one arm in front of you.
    #
    # The second arm is generated IN-PROCESS and never written to disk. It must not
    # touch `data/generated/`, which holds the reported batch that the exception list,
    # the API and the UI all read; overwriting that from a display option would make the
    # headline and the artefacts disagree.
    # Skip a comparison arm that equals the primary density. Now that the primary is
    # read from the manifest it can BE the compare density, and "density=12 vs 12" is a
    # column of zeroes wearing the costume of a measurement.
    compare = None
    cmp_batch = None
    # No second density arm on the holdout: it is a FROZEN artefact, and generating a
    # fresh comparison batch beside it would put an unfrozen number in the same block.
    if generated_dir is None and args.compare_density and args.compare_density != ppw:
        from recon.generator import build as _build

        # The comparison arm is DECORATION on the headline. It must never be able to
        # take down the run that produced the numbers -- and it could: this assertion
        # was uncaught, so an unsatisfiable second batch killed the command with a
        # traceback AFTER run_output.json had been written and BEFORE the metrics block
        # printed. The operator lost the results of a run that succeeded, to a generator
        # assertion about a batch that exists only to decorate the headline.
        try:
            cmp_batch = _build.generate(
                seed=seed, payments_per_window=args.compare_density
            )
            _build.assert_truth_is_satisfiable(cmp_batch)
        except AssertionError as e:
            print(f"\n  comparison arm at ppw={args.compare_density} skipped: {e}\n")
            cmp_batch = None

    if cmp_batch is not None:
        cmp_out = match_once(cmp_batch.inputs, llm=llm)
        compare = (
            score(
                cmp_out,
                cmp_batch.truth,
                total_payments=len(cmp_batch.inputs.payments),
                captured_payments=sum(1 for p in cmp_batch.inputs.payments if p.captured),
                ambiguity_bank_txn_id=cmp_batch.ambiguity_bank_txn_id or "",
                credits_by_id={x.id: x.credit for x in cmp_batch.inputs.bank_txns},
                seed=seed,
            ),
            args.compare_density,
        )

    print(render(sc, seed, ppw,
                 llm_enabled=llm.name, relations=relations, ensemble=ensemble,
                 compare=compare))
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    """
    Ring 2 + Ring 3: investigate the exception list, then re-run the engine.

    **Both numbers are always reported.** `match` remains the deterministic baseline and
    this command shows the enriched run beside it, because the only claim worth making
    is a delta against a baseline anyone can reproduce without an agent.

    If precision falls, that is the headline. An agent that buys coverage by loosening
    correctness has done the one thing this project exists to argue against, and the
    right response is to revert the evidence layer rather than to report the coverage.
    """
    from recon.agent import EvidenceLedger, orchestrate, select_investigator
    from recon.agent.orchestrate import withdraw as _withdraw
    from recon.agent.investigate import RecordedInvestigator
    from recon.llm import select as _select_llm
    from scorer.score import load_truth, score

    generated_dir = cfg.HOLDOUT if args.dataset == "holdout" else None
    try:
        inputs = load_inputs(
            generated_dir=generated_dir,
            seed=args.seed,
            payments_per_window=args.payments_per_window,
        )
    except ValueError as e:
        print(f"\n  {e}\n")
        return 1
    if generated_dir is not None and not (generated_dir / "manifest.json").is_file():
        print("\n  No holdout present -- run `python run.py holdout` first.\n")
        return 1

    from loaders import load_payer_directory

    # The register travels with its batch. `load_payer_directory()` defaults to the
    # reported one, and running the agent against the holdout with the reported
    # merchant's register is not a measurement of anything -- it looks up payer names
    # from one batch in another batch's authorisations and declines almost everything.
    # Worth stating because the first attempt at this measurement did exactly that.
    directory = load_payer_directory(
        (generated_dir / "payer_directory.csv") if generated_dir is not None else None
    )
    llm = _select_llm(disabled=args.no_llm)
    investigator = (
        None
        if args.null_agent
        else RecordedInvestigator()
        if args.offline
        else select_investigator()
    )

    t0 = time.perf_counter()
    run = orchestrate(
        inputs,
        investigator,
        directory,
        llm=llm,
        ledger_path=(
            cfg.REPORTS / (
                "evidence_ledger_holdout.json" if generated_dir is not None
                else "evidence_ledger.json"
            )
        ) if args.write else None,
        max_exceptions=args.max_exceptions,
        approve_high_value=args.approve_high_value,
    )
    elapsed = time.perf_counter() - t0

    _print_block(
        f"AGENT RUN  dataset={args.dataset}  seed={inputs.seed}  "
        f"investigator={run.investigator}",
        [
            ("authorised-payer register", f"{len(directory)} row(s)"),
            ("exceptions in the baseline", run.exceptions_seen),
            ("investigated", run.investigated),
            ("not routed -- no agent may work it", len(run.not_routed)),
            ("resumed from a saved ledger", run.resumed),
            ("evidence asserted", run.proposals_accepted),
            ("assertions refused by the boundary", run.proposals_rejected),
            ("declined -- insufficient evidence", run.declined),
            ("errors", run.errors),
            ("budget exhausted", run.budget_exhausted),
            ("tool calls", f"{run.tool_calls} of {run.budget.tool_calls}"),
            ("wall clock", f"{elapsed:.2f}s"),
        ],
    )

    if run.by_investigator:
        print("\n  BY INVESTIGATOR")
        print("  " + "-" * 62)
        print(
            f"    {'role':14s} {'worked':>7s} {'asserted':>9s} {'declined':>9s} "
            f"{'errors':>7s}"
        )
        for role, st in sorted(run.by_investigator.items()):
            print(
                f"    {role:14s} {st['investigated']:>7d} {st['asserted']:>9d} "
                f"{st['declined']:>9d} {st['errors']:>7d}"
            )

    if run.by_category:
        print("\n  BY REFUSAL CATEGORY")
        print("  " + "-" * 62)
        print(
            f"    {'category':30s} {'seen':>5s} {'worked':>7s} {'asserted':>9s} "
            f"{'moved':>6s}"
        )
        for cat, st in sorted(run.by_category.items()):
            print(
                f"    {cat:30s} {st['seen']:>5d} {st['investigated']:>7d} "
                f"{st['asserted']:>9d} {st['moved']:>6d}"
            )
        if run.not_routed:
            print(
                "\n    A category with 0 worked is one no agent may touch -- a tie the\n"
                "    engine correctly refused, or a defect for an engineer. Skipping it\n"
                "    is the decision, not an omission; see recon/agent/routing.py."
            )

    truth_path = (
        (generated_dir / "_truth" / "ground_truth.json")
        if generated_dir is not None
        else cfg.TRUTH_DIR / "ground_truth.json"
    )
    if not truth_path.exists():
        print("\n  No ground truth present -- run `python run.py generate` first.")
        return 1
    raw, links = load_truth(truth_path)

    def _score(out):
        return score(
            out, links,
            total_payments=len(inputs.payments),
            captured_payments=sum(1 for p in inputs.payments if p.captured),
            ambiguity_bank_txn_id=raw.get("ambiguity_bank_txn_id", ""),
            credits_by_id={x.id: x.credit for x in inputs.bank_txns},
            seed=inputs.seed,
        )

    # ---- the precision interlock ----------------------------------------
    #
    # Every version the orchestrator produced is scored, and any whose precision falls
    # below the baseline has its evidence WITHDRAWN and the engine re-run. This is
    # requirement 9's rollback, and it lives here rather than in the loop because the
    # trigger is precision, precision needs ground truth, and `recon.agent` may not read
    # it -- the audit hook would raise. The orchestrator makes versions; the scorer
    # measures them; this decides.
    #
    # A run that still loses precision after the withdrawal exits non-zero at the end.
    before = _score(run.baseline)
    damaging = {
        d.bank_txn_id
        for version in run.versions[1:]
        if version.output is not None
        and _score(version.output).match_precision < before.match_precision
        for d in version.deltas
        if d.after == "assign"
    }
    if damaging:
        print("\n  ** EVIDENCE WITHDRAWN -- a version cost precision **")
        print("  " + "-" * 62)
        for txn_id in sorted(damaging):
            print(f"    {txn_id}  evidence withdrawn, engine re-run without it")
        print(
            "\n    Coverage bought at the cost of correctness is the one thing this\n"
            "    project exists to argue against, so the evidence is dropped rather\n"
            "    than the number reported. The ledger keeps what was withdrawn."
        )
        run = _withdraw(inputs, run, damaging, llm=llm)

    after = _score(run.enriched)
    # The third arm: what the evidence would have bought had a human approved every
    # material change. Reported beside the applied figure rather than instead of it --
    # one is what happens unattended and the other is what is on offer, and quoting
    # either alone misdescribes the system.
    full = next(
        (v for v in run.versions if v.label == "enriched" and v.output is not None), None
    )
    approved = _score(full.output) if full is not None else after

    print("\n  BASELINE vs APPLIED vs APPROVED")
    print("  " + "-" * 62)
    print(
        f"  {'':22s} {'BASELINE':>11s} {'APPLIED':>10s} {'APPROVED':>10s} {'DELTA':>8s}"
    )
    rows = [
        ("match rate", before.match_rate, after.match_rate, approved.match_rate, "pct"),
        ("match precision", before.match_precision, after.match_precision,
         approved.match_precision, "pct"),
        ("refusal rate", before.refusal_rate, after.refusal_rate,
         approved.refusal_rate, "pct"),
        ("assignments", before.total_assignments, after.total_assignments,
         approved.total_assignments, "int"),
        ("wrong assignments", len(before.wrong_assignments),
         len(after.wrong_assignments), len(approved.wrong_assignments), "int"),
    ]
    for label, b, a, ap, kind in rows:
        if kind == "pct":
            print(f"  {label:22s} {b:>10.2%} {a:>10.2%} {ap:>10.2%} {a - b:>+8.2%}")
        else:
            print(f"  {label:22s} {b:>11d} {a:>10d} {ap:>10d} {a - b:>+8d}")
    print(
        "\n    APPLIED is what runs unattended. APPROVED is APPLIED plus the changes\n"
        "    held for a human, which is what --approve-high-value posts. DELTA is\n"
        "    baseline to APPLIED, because that is the claim the system makes on its own."
    )

    if run.pending_approval:
        print("\n  HELD FOR HUMAN APPROVAL")
        print("  " + "-" * 62)
        for pending in run.pending_approval:
            print(
                f"    {pending.bank_txn_id}  Rs {pending.rupees:>12,.2f}  "
                f"{len(pending.payment_ids)} payment(s)"
            )
        print(
            f"\n    At or above materiality (Rs {cfg.MATERIALITY_PAISE / 100:,.2f}, "
            f"PCAOB AS 2315 -- the\n"
            "    same line Layer 4 uses to decide what is verified in full rather than\n"
            "    sampled). The engine reached these verdicts by its own rules; what\n"
            "    waits is the posting. Re-run with --approve-high-value to apply them."
        )

    print("\n  EVIDENCE-ATTRIBUTABLE COVERAGE GAIN")
    print("  " + "-" * 62)
    print(f"    payments newly assigned      {run.payments_gained}")
    print(f"    evidence assertions made     {run.proposals_accepted}")
    print(f"    gain per assertion           {run.evidence_attributable_gain:.2f}")
    print(
        "\n    Every assertion is a NAMED fact with the tool calls that found it, so a\n"
        "    verdict change traces to evidence rather than to an agent's opinion."
    )

    # ---- what each SOURCE bought, on its own -----------------------------
    #
    # Precision is filled in HERE, not in the orchestrator: `recon.agent` is inside the
    # ground-truth boundary and the audit hook enforces it, so the agent produces the
    # counterfactual outputs and the scorer -- which may read truth -- turns them into
    # precision. The same split that stops the engine scoring itself.
    for contribution in run.by_source.values():
        isolated = run.source_outputs.get(contribution.source)
        if isolated is None:
            continue
        contribution.precision_before = before.match_precision
        contribution.precision_after = _score(isolated).match_precision
        contribution.precision_measured = True

    if run.by_source:
        print("\n  WHAT EACH EVIDENCE SOURCE BOUGHT, MEASURED ON ITS OWN")
        print("  " + "-" * 62)
        print(
            f"    {'source':30s} {'closed':>6s} {'rupees released':>17s} {'precision':>10s}"
        )
        for c in sorted(
            run.by_source.values(), key=lambda c: (-c.paise_released, c.source)
        ):
            tag = c.source if c.is_external else f"{c.source} (no citation)"
            prec = f"{c.precision_after:.4f}" if c.precision_measured else "unmeasured"
            print(
                f"    {tag:30s} {c.exceptions_closed:>6d} "
                f"{'Rs ' + format(c.rupees_released, ',.2f'):>17s} {prec:>10s}"
            )
            if c.precision_measured and c.precision_delta < 0:
                print(
                    f"      ** this source bought coverage at the cost of "
                    f"{abs(c.precision_delta):.4f} precision. Do not license it. **"
                )
        print(
            "\n    Each row is a SEPARATE re-run of the engine carrying only that\n"
            "    source's evidence, so a row is what that source buys alone -- not a\n"
            "    share of a joint result. Two sources that close the same exception\n"
            "    both count it, so the rows need not sum to the total above."
        )
        # An ABSENT `model_assertion` row means no proposal was made without citing an
        # external source, which is the good case and worth saying out loud -- an
        # absent row reads as "not measured" unless the report says otherwise.
        model_only = run.by_source.get("model_assertion")
        if model_only and model_only.exceptions_closed:
            print(
                f"\n    {model_only.exceptions_closed} exception(s) were closed on a "
                f"`model_assertion` -- the\n"
                "    agent concluded something without consulting any external source.\n"
                "    Reported separately on purpose: it may be correct, but it is not a\n"
                "    dataset anyone can buy, re-read, or audit."
            )
        else:
            print(
                "\n    No exception was closed on a bare model assertion: every verdict\n"
                "    that moved cites a source someone could procure and re-read."
            )

    if run.deltas:
        print("\n  VERDICTS THAT MOVED")
        print("  " + "-" * 62)
        for d in run.deltas:
            print(f"    {d.bank_txn_id}  {d.before} -> {d.after}   Rs {d.rupees:>12,.2f}")
            if d.attributed_to:
                for line in _wrap(d.attributed_to, 66):
                    print(f"        {line}")

    declined = [t for t in run.traces if t.outcome == "insufficient_evidence"]
    if declined:
        print(f"\n  DECLINED -- {len(declined)} exception(s), with the reason given")
        print("  " + "-" * 62)
        print(
            "    An agent that never says 'I don't know' is worse than one with a\n"
            "    lower match rate. The register is deliberately incomplete so this\n"
            "    answer has to be reachable.\n"
        )
        for t in declined[:8]:
            print(f"    {t.bank_txn_id}: {t.note[:100]}")
        if len(declined) > 8:
            print(f"    ... and {len(declined) - 8} more")

    # ---- the metrics artefact -------------------------------------------
    #
    # `AgentRun.as_dict()` was fully written and never called: every figure the agent
    # produced lived in this terminal and nowhere else, so nothing downstream could read
    # a run and nothing could compare two. Written beside the other artefacts, with the
    # precision numbers -- which the orchestrator may not compute -- filled in here.
    if args.write:
        metrics = run.as_dict()
        metrics |= {
            "dataset": args.dataset,
            "seed": inputs.seed,
            "wall_clock_s": round(elapsed, 3),
            "precision_before": round(before.match_precision, 6),
            "precision_applied": round(after.match_precision, 6),
            "precision_if_approved": round(approved.match_precision, 6),
            "match_rate_before": round(before.match_rate, 6),
            "match_rate_applied": round(after.match_rate, 6),
            "match_rate_if_approved": round(approved.match_rate, 6),
            "wrong_assignments_before": len(before.wrong_assignments),
            "wrong_assignments_applied": len(after.wrong_assignments),
        }
        path = cfg.REPORTS / (
            "agent_run_holdout.json" if generated_dir is not None else "agent_run.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        print(f"\n  metrics written to {path}")

    if after.match_precision < before.match_precision:
        print(
            "\n  ** PRECISION FELL, and the withdrawal did not recover it. The evidence\n"
            "     layer bought coverage by loosening correctness, which is the one thing\n"
            "     this project argues against. Revert the evidence layer rather than\n"
            "     reporting the coverage gain. **"
        )
        return 1
    return 0


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width) or [""]


def cmd_sweep(args: argparse.Namespace) -> int:
    """
    The density sweep -- this project's central empirical claim, measured.

    Candidate-pool crowding is the thing that makes reconciliation genuinely hard, and
    it is a dial the generator exposes. As it is turned up, a correct engine should
    REFUSE MORE while holding precision flat; a naive one holds coverage flat and
    quietly loses precision instead. Coverage is what should degrade, not correctness.

    Reported over held-out seeds, disjoint from the two reported runs.
    """
    from recon.generator import build as _build

    seeds = tuple(args.seeds)
    densities = tuple(args.densities)
    print("=" * 78)
    print("  DENSITY SWEEP -- refusal rate vs precision")
    print(f"  mean over {len(seeds)} held-out seeds {seeds}")
    print("=" * 78)
    print(f"\n  {'ppw':>5} {'pool':>7} {'match rate':>11} {'precision':>10} "
          f"{'refusal rate':>13} {'assigned':>9}")
    print("  " + "-" * 62)

    rows = []
    unsatisfiable: list[str] = []
    for ppw in densities:
        mr = pr = rr = pool = 0.0
        assigned = 0
        # Counted, not assumed. A skipped arm must not be averaged in as a zero NOR
        # divided into as though it had contributed -- either way every figure in the
        # row would be quietly deflated by an arm that never ran.
        contributing = 0
        for seed in seeds:
            batch = _build.generate(seed=seed, payments_per_window=ppw)
            # The sweep builds batches in-process and used to skip this, which is
            # exactly where the ambiguity-window orphaning defect hid: `generate`
            # checked the primary seed, the sweep never checked its own five. A sweep
            # that quietly averages over unsatisfiable ground truth is reporting the
            # generator's bugs as the engine's coverage.
            #
            # Reported, not raised -- matching the pool-bound assertion two lines below,
            # which was already handled this way. Uncaught, one bad arm out of twenty
            # discarded the fifteen already computed, and the asymmetry sat between two
            # consecutive statements.
            try:
                _build.assert_truth_is_satisfiable(batch)
            except AssertionError as e:
                unsatisfiable.append(f"seed {seed} ppw {ppw}: {e}")
                continue
            try:
                worst = _build.assert_pool_bound(batch)
            except AssertionError as e:
                worst = int(str(e).split("holds ")[1].split(" ")[0])
            out = match_once(batch.inputs)
            truth = {t.bank_txn_id: t for t in batch.truth if t.bank_txn_id}
            n = len(out.assignments)
            correct = sum(
                1 for a in out.assignments
                if (l := truth.get(a.bank_txn_id))
                and l.expected_verdict == "assign"
                and set(l.payment_ids) == set(a.payment_ids)
            )
            captured = sum(1 for p in batch.inputs.payments if p.captured)
            mr += sum(len(a.payment_ids) for a in out.assignments) / max(1, captured)
            pr += correct / max(1, n)
            rr += len(out.refusals) / max(1, n + len(out.refusals))
            pool += worst
            assigned += n
            contributing += 1
        if not contributing:
            print(f"  {ppw:>5}   -- every seed unsatisfiable; arm skipped")
            continue
        k = contributing
        note = "" if k == len(seeds) else f"   [{k}/{len(seeds)} seeds]"
        rows.append((ppw, pool / k, mr / k, pr / k, rr / k))
        print(f"  {ppw:>5} {pool / k:>7.1f} {mr / k:>10.1%} {pr / k:>10.4f} "
              f"{rr / k:>12.1%} {assigned // k:>9}{note}")

    if unsatisfiable:
        print(f"\n  {len(unsatisfiable)} arm(s) SKIPPED -- ground truth unsatisfiable:")
        for line in unsatisfiable[:5]:
            print(f"    {line.splitlines()[0][:110]}")
        print("  These are generator defects, not engine coverage. A sweep that averaged")
        print("  over them would report the generator's bugs as the engine's results.")

    if not rows:
        print("\n  No arm produced a result. Nothing is reported rather than a mean of")
        print("  nothing -- see the skipped arms above.")
        return 1

    lo, hi = rows[0], rows[-1]
    print(f"\n  refusal rate  {lo[4]:.1%} -> {hi[4]:.1%}   "
          f"({hi[4] / max(1e-9, lo[4]):.1f}x as the pool grows {lo[1]:.0f} -> {hi[1]:.0f})")
    print(f"  precision     {lo[3]:.4f} -> {hi[3]:.4f}")
    print(f"  match rate    {lo[2]:.1%} -> {hi[2]:.1%}")
    if hi[3] >= lo[3] - 0.01 and hi[4] > lo[4]:
        print("\n  As ambiguity rises the engine declines more work rather than getting")
        print("  it wrong. COVERAGE degrades; CORRECTNESS does not. That is the whole")
        print("  claim, and it is the chart no vendor publishes -- producing it requires")
        print("  reporting precision, which none of them do.")
    else:
        print("\n  PRECISION DEGRADED WITH DENSITY. The refusal mechanism is not doing")
        print("  its job: the engine is guessing where it should decline. This is the")
        print("  most important negative result the project can surface, and it is")
        print("  reported rather than suppressed.")
    return 0


_RULE = "=" * 78


def cmd_llm_compare(args: argparse.Namespace) -> int:
    """
    Run the batch with the LLM narration tier ON and OFF, and report both arms.

    `docs/ARCHITECTURE.md` requires precision to be reported both ways. This is that
    measurement -- and, when the only available tier is the offline stand-in, this is
    also the machinery that REFUSES to report it, which is the more important half. Two
    identical numbers printed side by side would read as "the LLM changes nothing"; what
    is actually true is "nothing here can tell you whether it would".
    """
    from recon.llm import select as select_llm
    from recon.llm.compare import (
        diff_verdicts, measure_parse_yield, split_changes, tier_is_measurable,
    )
    from scorer.score import load_truth, score

    try:
        inputs = load_inputs(seed=args.seed, payments_per_window=args.payments_per_window)
    except ValueError as e:
        print(f"\n  {e}\n")
        return 1
    seed = inputs.seed
    tier_on = select_llm(disabled=False)
    tier_off = select_llm(disabled=True)
    # Two checks, and they must happen at different times.
    #
    # The tier IDENTITY check is a pre-check: it costs nothing and stops a pointless
    # paid run against an offline stand-in. Tier HEALTH cannot be judged until the
    # calls have actually been made, so it is re-evaluated below -- and it can only
    # ever downgrade the verdict, never upgrade it.
    valid, why = tier_is_measurable(tier_on)

    print(_RULE)
    print(f"  LLM TIER: ON vs OFF   seed={seed}   tier={tier_on.name}")
    print(_RULE)

    yields = measure_parse_yield(inputs, tier_on)

    t0 = time.perf_counter()
    if args.verify:
        from recon.verify.stability import match_gated

        k = cfg.PERMUTATION_K_FAST if args.fast else cfg.PERMUTATION_K
        out_on, _ = match_gated(inputs, k=k, llm=tier_on)
        out_off, _ = match_gated(inputs, k=k, llm=tier_off)
    else:
        out_on = match_once(inputs, llm=tier_on)
        out_off = match_once(inputs, llm=tier_off)
    elapsed = time.perf_counter() - t0

    # Now that calls have been made, ask again. A tier whose requests never reached the
    # model returns empty fields -- which is precisely what a SUCCESSFUL call returns
    # for an unreadable narration -- so without this the harness would report a null
    # result as a finding about the model instead of about the transport.
    if valid:
        valid, why = tier_is_measurable(tier_on)

    changes = diff_verdicts(out_on, out_off)

    print("\n  PARSE YIELD  (field level, before any matching)")
    print("  " + "-" * 74)
    print(f"    credit narrations                {yields.narrations}")
    print(f"    unreadable by the regex tier     {yields.unreadable_by_regex}"
          "   (engine's own needs_llm definition)")
    print(f"    gaps filled by the LLM tier      {yields.filled_by_llm}"
          f"   ({yields.fill_rate:.0%} of the gaps)")
    print(f"      payer names recovered          {yields.names_recovered}")
    print(f"      merchant refs recovered        {yields.refs_recovered}")
    print(f"    read a field differently          {yields.disagreed_with_regex}"
          "   (informational -- the merge keeps the regex value)")

    outcome_changes, reason_changes = split_changes(changes)
    print("\n  VERDICT DELTAS  (the only thing that licenses a claim about output)")
    print("  " + "-" * 74)
    if not outcome_changes:
        print("    0 credits changed DECISION (assign / refuse / no candidate).")
    else:
        print(f"    {len(outcome_changes)} credit(s) changed DECISION:")
        for txn_id, off, on in outcome_changes[:20]:
            print(f"      {txn_id}   off={off}   ->   on={on}")
    if reason_changes:
        print(f"\n    {len(reason_changes)} credit(s) kept the same decision and changed")
        print("    only the REASON given to the operator:")
        for txn_id, off, on in reason_changes[:20]:
            print(f"      {txn_id}   off={off}   ->   on={on}")
        print("")
        print("    Same money, same place, same precision and match rate. A better")
        print("    sentence for a human is a real contribution and a much weaker one")
        print("    than moving a verdict, so it is not reported under that heading.")

    truth_path = cfg.TRUTH_DIR / "ground_truth.json"
    if truth_path.exists():
        meta, links = load_truth(truth_path)
        captured = sum(1 for p in inputs.payments if p.captured)
        credits_by_id = {t.id: t.credit for t in inputs.bank_txns if t.is_credit}
        arms = {}
        for label, out in (("LLM OFF", out_off), ("LLM ON", out_on)):
            arms[label] = score(
                out, links, total_payments=len(inputs.payments),
                captured_payments=captured,
                ambiguity_bank_txn_id=meta.get("ambiguity_bank_txn_id", ""),
                credits_by_id=credits_by_id, seed=seed,
            )
        print("\n  HEADLINE, BOTH ARMS")
        print("  " + "-" * 74)
        print(f"    {'':<22}{'LLM OFF':>14}{'LLM ON':>14}{'delta':>12}")
        for name, attr, pct in (
            ("match rate", "match_rate", True),
            ("match precision", "match_precision", True),
            ("refusal rate", "refusal_rate", True),
            ("assignments", "total_assignments", False),
            ("correct assignments", "correct_assignments", False),
        ):
            a, b = getattr(arms["LLM OFF"], attr), getattr(arms["LLM ON"], attr)
            fmt = (lambda v: f"{v:.2%}") if pct else (lambda v: f"{v:d}")
            delta = f"{b - a:+.2%}" if pct else f"{b - a:+d}"
            print(f"    {name:<22}{fmt(a):>14}{fmt(b):>14}{delta:>12}")

    print("\n  VERDICT ON THIS MEASUREMENT")
    print("  " + "-" * 74)
    if valid:
        print("    VALID. The tier above is a live model, so the arms differ in what a")
        print("    model contributed and the comparison means what it says.")
        if not outcome_changes:
            print("    The measured contribution to DECISIONS is zero. That is a result,")
            print("    not an absence of one: the trust boundary holds and the")
            print("    deterministic tiers were already sufficient on this batch.")
    else:
        print("    WITHHELD -- this comparison is NOT valid evidence about an LLM.")
        for line in _wrap(why, 70):
            print(f"    {line}")
        print("")
        # The two withholding reasons need different sentences. Calling a transport
        # failure "a measurement of the stand-in" would be its own small false claim.
        errs = list(getattr(tier_on, "transport_errors", None) or ())
        if errs:
            made = getattr(tier_on, "calls_made", 0)
            # Quote the counts instead of asserting "every". Under a partial failure
            # most fields DID come from the model, and this is the branch whose whole
            # job is to not overstate what the run established.
            scope = (
                f"all {made} calls" if len(errs) >= made else f"{len(errs)} of {made} calls"
            )
            print("    The numbers above are real, and they measure a BROKEN")
            print(f"    TRANSPORT: {scope} never reached the model, so the")
            print("    fields behind them are empty for a reason that has nothing to")
            print("    do with the model. An empty field is also what a successful")
            print("    call returns for an unreadable narration, which is exactly why")
            print("    this cannot be read as a null result for a model.")
        else:
            print("    The parse-yield and verdict-delta numbers above are real")
            print("    measurements OF THE STAND-IN. They are not a null result for a")
            print("    model, and quoting them as one would be the overclaim this")
            print("    project exists to argue against.")

    print(f"\n  both arms in {elapsed:.2f}s")
    print(_RULE)
    return 0 if valid else 2


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width)


def cmd_verify_foreign(args) -> int:
    """
    Verification-as-a-service: audit somebody else's matches.

    The four layers are properties of a CLAIM, not of this matcher, so they hold whoever
    made it. Everything printed above the SCORED block is derived from the three sides
    plus the claim -- no ground truth is read to produce it, which is what makes this a
    service a merchant can point at an incumbent's Monday output rather than a benchmark
    that needs labelled data first.
    """
    from recon.verify.foreign import CONTEXT_DEPENDENT, audit

    generated_dir = cfg.HOLDOUT if args.dataset == "holdout" else None
    try:
        inputs = load_inputs(generated_dir=generated_dir)
    except ValueError as e:
        print(f"\n  {e}\n")
        return 1

    if args.claims:
        from loaders import load_foreign_claims

        path = Path(args.claims)
        if not path.is_file():
            print(f"\n  No such claims file: {path}\n")
            return 1
        try:
            claims = load_foreign_claims(path)
        except ValueError as e:
            print(f"\n  {e}\n")
            return 1
        claimant = args.claimant or path.name
    elif args.naive:
        from external.naive_matcher import NAME, match as naive_match

        claims = naive_match(inputs)
        claimant = args.claimant or NAME
    else:
        # Auditing our own output is the control arm and the credibility check, not a
        # party trick: an auditor that flags the matcher it ships with has a bug in one
        # of the two, and you cannot tell which from the outside.
        from recon.verify.foreign import claims_from

        # The deterministic arm deliberately: the self-audit is a claim about THIS
        # engine, and auditing a run whose tier varies between invocations would make
        # the control arm vary with it.
        out = match_once(inputs)
        claims = claims_from(out)
        claimant = args.claimant or "pramana (self-audit)"

    a = audit(inputs, claims, claimant=claimant)

    _print_block(
        f"FOREIGN VERIFICATION  claimant={a.claimant}",
        [
            ("credits in the batch", a.credits_in_batch),
            ("claims made", a.claims),
            ("coverage", f"{a.coverage:.2%}"),
            ("claims surviving every check", a.claims_surviving),
            ("survival", f"{a.survival:.2%}"),
            ("exposure on failed claims", f"Rs {a.paise_at_risk / 100:,.2f}"),
            ("credits left unclaimed", f"{len(a.unclaimed_credits)}"),
            ("value left unclaimed", f"Rs {a.unclaimed_paise / 100:,.2f}"),
        ],
    )
    print(
        "\n    Coverage and survival are reported TOGETHER, for the reason this\n"
        "    engine's own headline is a triple: a claimant who assigns nothing posts a\n"
        "    perfect audit, and one who assigns everything posts a perfect coverage."
    )

    checks = a.by_check()
    if checks:
        print("\n  WHAT DID NOT SURVIVE")
        print("  " + "-" * 62)
        for check, n in checks.items():
            tag = " (observation, not a failure)" if check == CONTEXT_DEPENDENT else ""
            print(f"    {check:24s} {n:>4d}{tag}")

        print("\n  THE LARGEST, WITH THE CHECK THAT OBJECTED")
        print("  " + "-" * 62)
        for f in sorted(a.findings, key=lambda f: -f.paise)[:8]:
            print(f"    {f.bank_txn_id}  Rs {f.paise / 100:>12,.2f}  {f.check}")
            for line in _wrap(f.detail, 64):
                print(f"        {line}")
    else:
        print("\n  Every claim survived every check.")

    if args.write:
        # Its own file per dataset. `match --dataset holdout` silently overwriting the
        # reported run's artefact is a mistake this project has already made once
        # (DEFECT_LOG 2026-09-03) and it costs nothing to not make it again.
        out_path = cfg.REPORTS / (
            "foreign_audit_holdout.json" if generated_dir is not None
            else "foreign_audit.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(a.as_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"\n  wrote {out_path}")

    if not args.score:
        return 0

    # ---- the part that DOES need ground truth ---------------------------
    #
    # Only reached with --score, and only to answer one question: how well does the
    # truth-free audit predict the claims that are actually wrong? That number is what
    # licenses the service. It is measured here rather than asserted, and the miss count
    # is printed first because it is the one that would sink the claim.
    from scorer.score import load_truth

    truth_path = (
        (generated_dir / "_truth" / "ground_truth.json")
        if generated_dir is not None
        else cfg.TRUTH_DIR / "ground_truth.json"
    )
    if not truth_path.exists():
        print("\n  No ground truth present, so the audit cannot be scored.")
        print("  (Every number above stands -- none of it needed truth.)")
        return 0

    _, links = load_truth(truth_path)
    truth = {
        l.bank_txn_id: set(l.payment_ids)
        for l in links
        if l.bank_txn_id and l.expected_verdict == "assign"
    }
    flagged = {f.bank_txn_id for f in a.findings if f.check != CONTEXT_DEPENDENT}

    def _wrong(c):
        return truth.get(c.bank_txn_id) != set(c.payment_ids)

    caught = sum(1 for c in claims if c.bank_txn_id in flagged and _wrong(c))
    false_alarm = sum(1 for c in claims if c.bank_txn_id in flagged and not _wrong(c))
    missed = sum(1 for c in claims if c.bank_txn_id not in flagged and _wrong(c))
    clean = sum(1 for c in claims if c.bank_txn_id not in flagged and not _wrong(c))
    correct = sum(1 for c in claims if not _wrong(c))

    print("\n  SCORED  (ground truth read HERE and nowhere above)")
    print("  " + "-" * 62)
    print(f"    the claimant's true precision   {correct}/{len(claims)} = "
          f"{correct / len(claims) if claims else 0:.4f}")
    print(f"    truth-free survival             {a.claims_surviving}/{a.claims} = "
          f"{a.survival:.4f}")
    print("\n    THE AUDIT AS A DETECTOR OF WRONG CLAIMS")
    print(f"      wrong claims MISSED       {missed:>4}   <- the number that matters")
    print(f"      wrong claims caught       {caught:>4}")
    print(f"      correct claims flagged    {false_alarm:>4}   <- false alarms")
    print(f"      correct claims passed     {clean:>4}")
    if caught + missed:
        print(f"      recall                    {caught / (caught + missed):>7.4f}")
    if caught + false_alarm:
        print(f"      flag precision            {caught / (caught + false_alarm):>7.4f}")
    print(
        "\n    Recall is the number to read first: a missed wrong claim is money posted\n"
        "    to the wrong receivable that nobody was told about. A false alarm costs an\n"
        "    analyst a look."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    # Before anything reads os.environ -- notably recon.llm.select(), which picks the
    # live tier or the offline stand-in purely on whether ANTHROPIC_API_KEY is present.
    _load_dotenv()

    ap = argparse.ArgumentParser(prog="pramana", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="build the three sides and the ground truth")
    g.add_argument("--seed", type=int, default=cfg.SEED_PRIMARY)
    g.add_argument("--n", type=int, default=cfg.N_PAYMENTS)
    g.add_argument(
        "--payments-per-window", type=int, default=cfg.TARGET_POOL_SIZE,
        help="density -- the PRIMARY parameter; the date range is derived from it",
    )
    g.add_argument(
        "--density-sweep", action="store_true",
        help=f"generate at each of {cfg.DENSITY_SWEEP} instead of one density",
    )
    g.add_argument("--no-write", dest="write", action="store_false", default=True)
    g.set_defaults(func=cmd_generate)

    m = sub.add_parser("match", help="run the matching engine and print the metrics block")
    # None, not the config default: `load_inputs` has to be able to tell an omitted
    # flag from one that happens to name the default value, or its mismatch guard
    # silently relabels the run instead of refusing.
    m.add_argument("--seed", type=int, default=None,
                   help="fail unless the batch on disk was generated with this seed")
    m.add_argument("--payments-per-window", type=int, default=None,
                   help="fail unless the batch on disk was generated at this density")
    m.add_argument("--dataset", choices=("primary", "holdout"), default="primary",
                   help="which batch to score: the reported one, or the frozen "
                        "shifted holdout")
    m.add_argument("--no-score", dest="score", action="store_false", default=True,
                   help="match only; do not load ground truth at all")
    m.add_argument("--verify", action="store_true", default=False,
                   help="run the permutation gate and the six metamorphic relations")
    m.add_argument("--fast", action="store_true", default=False,
                   help=f"dev loop only: K={cfg.PERMUTATION_K_FAST} and skip multi-run "
                        f"relations. Never used for reported numbers.")
    m.add_argument("--no-llm", action="store_true", default=False,
                   help="disable the LLM narration tier and report precision without it")
    m.add_argument("--live-llm", action="store_true", default=False,
                   help="use the live model for THIS run. Off by default so the reported "
                        "artifact stays reproducible without an API key; "
                        "`llm-compare` is the command that measures the live tier.")
    m.add_argument("--compare-density", type=int, default=cfg.HEADLINE_COMPARE_DENSITY,
                   help="report a second density beside the reported one in the "
                        "headline (generated in-process, never written to disk). "
                        "0 disables.")
    m.set_defaults(func=cmd_match)

    c = sub.add_parser(
        "llm-compare",
        help="run the batch with the LLM tier on and off and report both arms",
    )
    c.add_argument("--seed", type=int, default=None)
    c.add_argument("--payments-per-window", type=int, default=None)
    c.add_argument("--verify", action="store_true", default=False,
                   help="run both arms under the permutation gate")
    c.add_argument("--fast", action="store_true", default=False)
    c.set_defaults(func=cmd_llm_compare)

    g2 = sub.add_parser(
        "agent", help="Ring 2+3: investigate the exceptions, then re-run the engine"
    )
    g2.add_argument("--seed", type=int, default=None)
    g2.add_argument("--payments-per-window", type=int, default=None)
    g2.add_argument("--no-llm", action="store_true", default=False,
                    help="disable the narration LLM tier (independent of the agent)")
    g2.add_argument("--offline", action="store_true", default=False,
                    help="force the recorded investigator even if a key is present")
    g2.add_argument("--null-agent", action="store_true", default=False,
                    help="the control arm: investigate nothing, assert nothing. Must "
                         "reproduce the baseline exactly")
    g2.add_argument("--dataset", choices=("reported", "holdout"), default="reported",
                    help="which batch to investigate. The agent could only ever run "
                         "against the reported one, so its numbers were single-batch "
                         "and its generalisation unmeasured")
    g2.add_argument("--max-exceptions", type=int, default=None,
                    help="investigate only the N largest exceptions")
    g2.add_argument("--no-write", dest="write", action="store_false", default=True,
                    help="do not persist the evidence ledger")
    g2.add_argument("--approve-high-value", action="store_true", default=False,
                    help="apply newly-assigned credits at or above materiality instead "
                         "of holding them for a human. Off by default: the engine "
                         "reaches these verdicts by its own rules either way, and what "
                         "waits is the posting")
    g2.set_defaults(func=cmd_agent)

    h = sub.add_parser(
        "holdout", help="build the frozen shifted held-out set (a deliberate act)"
    )
    h.add_argument("--no-write", dest="write", action="store_false", default=True)
    h.set_defaults(func=cmd_holdout)

    v = sub.add_parser(
        "verify-foreign",
        help="audit somebody else's matches with the four layers (needs no ground truth)",
    )
    v.add_argument(
        "--claims",
        help="CSV of a third party's assignments: bank_txn_id,payment_ids",
    )
    v.add_argument(
        "--naive",
        action="store_true",
        help="audit the built-in straw-man matcher instead of a file (see "
             "external/naive_matcher.py -- it is a straw man and says so)",
    )
    v.add_argument(
        "--claimant", default="", help="what to call the claimant in the report"
    )
    v.add_argument(
        "--dataset", choices=("reported", "holdout"), default="reported",
        help="which batch the claims are against",
    )
    v.add_argument(
        "--score", action="store_true",
        help="additionally read ground truth and report how well the truth-free audit "
             "predicted the claims that are actually wrong",
    )
    v.add_argument("--no-write", dest="write", action="store_false", default=True)
    v.set_defaults(func=cmd_verify_foreign)

    w = sub.add_parser("sweep", help="density sweep: refusal rate vs precision")
    w.add_argument("--seeds", type=int, nargs="+", default=[11111, 22222, 33333, 44444, 55555])
    w.add_argument("--densities", type=int, nargs="+", default=[3, 6, 12, 24])
    w.set_defaults(func=cmd_sweep)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())


def _unexamined(inputs, out) -> tuple[int, int]:
    """
    Bank lines that received no verdict of any kind, with their value.

    A disclosure, and one computed by subtraction on purpose. The previous version
    counted debits, which was correct exactly while the engine read none of them --
    the moment it started reading debits the same expression would have gone on
    reporting all six as unexamined while six reversals sat in the output. Deriving it
    from what actually reached a verdict means the number is right whatever the engine
    learns to read next, and it is what would catch the next omission.
    """
    seen = (
        {a.bank_txn_id for a in out.assignments}
        | set(out.grouped_txn_ids)
        | {r.bank_txn_id for r in out.refusals}
        | set(out.no_candidate)
        | {r.bank_txn_id for r in out.reversals}
        | {u.bank_txn_id for u in out.unexplained_debits}
    )
    missed = [t for t in inputs.bank_txns if t.id not in seen]
    return len(missed), sum(abs(t.amount) for t in missed)

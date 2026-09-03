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
    try:
        inputs = load_inputs(seed=args.seed, payments_per_window=args.payments_per_window)
    except ValueError as e:
        # A seed/batch mismatch is a reporting error waiting to happen, not a crash to
        # trace. Say what is wrong and what to run.
        print(f"\n  {e}\n")
        return 1

    # From here down, ALWAYS report `inputs.seed` / `inputs.payments_per_window`, never
    # `args.*`. `load_inputs` resolves both from the batch's manifest, so the two can
    # differ -- and when they did, the headline printed one seed while run_output.json
    # recorded another and took its density from a third place. A payload inconsistent
    # with itself is worse than a wrong one, because nothing about it looks wrong.
    seed = inputs.seed
    ppw = inputs.payments_per_window

    from recon.llm import select as _select_llm

    llm = _select_llm(disabled=args.no_llm)
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

    truth_path = cfg.TRUTH_DIR / "ground_truth.json"
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
        unexamined=(
            sum(1 for x in inputs.bank_txns if x.debit),
            sum(x.debit for x in inputs.bank_txns if x.debit),
        ),
    )
    # The UI payload is built from the ENGINE's output only -- no ground truth, no
    # scoring. What a merchant sees is exactly what the engine could justify without
    # an answer key.
    from recon.report import run_output

    payload = run_output.build(
        inputs, out, seed=seed, elapsed_s=elapsed,
        relations=relations, ensemble=ensemble, llm=llm, recorder=recorder,
    )
    written = run_output.write(payload)

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
    if args.compare_density and args.compare_density != ppw:
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


def main(argv: list[str] | None = None) -> int:
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
    m.add_argument("--no-score", dest="score", action="store_false", default=True,
                   help="match only; do not load ground truth at all")
    m.add_argument("--verify", action="store_true", default=False,
                   help="run the permutation gate and the six metamorphic relations")
    m.add_argument("--fast", action="store_true", default=False,
                   help=f"dev loop only: K={cfg.PERMUTATION_K_FAST} and skip multi-run "
                        f"relations. Never used for reported numbers.")
    m.add_argument("--no-llm", action="store_true", default=False,
                   help="disable the LLM narration tier and report precision without it")
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

    w = sub.add_parser("sweep", help="density sweep: refusal rate vs precision")
    w.add_argument("--seeds", type=int, nargs="+", default=[11111, 22222, 33333, 44444, 55555])
    w.add_argument("--densities", type=int, nargs="+", default=[3, 6, 12, 24])
    w.set_defaults(func=cmd_sweep)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

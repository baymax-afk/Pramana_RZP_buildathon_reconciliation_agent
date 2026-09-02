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

ROOT = Path(__file__).resolve().parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config as cfg  # noqa: E402
from loaders import load_inputs  # noqa: E402
from recon.engine.match import match_once  # noqa: E402
from recon.generator import build  # noqa: E402


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
                [(k, str(v.relative_to(ROOT))) for k, v in paths.items()],
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
    inputs = load_inputs(seed=args.seed, payments_per_window=args.payments_per_window)

    from recon.llm import select as _select_llm

    llm = _select_llm(disabled=args.no_llm)
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
        relations = mm.run_all(inputs, out, fast=args.fast)
    else:
        out = match_once(inputs, llm=llm)
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
        seed=args.seed,
    )
    print(render(sc, args.seed, args.payments_per_window,
                 llm_enabled=llm.name, relations=relations, ensemble=ensemble))
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
    for ppw in densities:
        mr = pr = rr = pool = 0.0
        assigned = 0
        for seed in seeds:
            batch = _build.generate(seed=seed, payments_per_window=ppw)
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
        k = len(seeds)
        rows.append((ppw, pool / k, mr / k, pr / k, rr / k))
        print(f"  {ppw:>5} {pool / k:>7.1f} {mr / k:>10.1%} {pr / k:>10.4f} "
              f"{rr / k:>12.1%} {assigned // k:>9}")

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run.py", description=__doc__)
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
    m.add_argument("--seed", type=int, default=cfg.SEED_PRIMARY)
    m.add_argument("--payments-per-window", type=int, default=cfg.TARGET_POOL_SIZE)
    m.add_argument("--no-score", dest="score", action="store_false", default=True,
                   help="match only; do not load ground truth at all")
    m.add_argument("--verify", action="store_true", default=False,
                   help="run the permutation gate and the six metamorphic relations")
    m.add_argument("--fast", action="store_true", default=False,
                   help=f"dev loop only: K={cfg.PERMUTATION_K_FAST} and skip multi-run "
                        f"relations. Never used for reported numbers.")
    m.add_argument("--no-llm", action="store_true", default=False,
                   help="disable the LLM narration tier and report precision without it")
    m.set_defaults(func=cmd_match)

    w = sub.add_parser("sweep", help="density sweep: refusal rate vs precision")
    w.add_argument("--seeds", type=int, nargs="+", default=[11111, 22222, 33333, 44444, 55555])
    w.add_argument("--densities", type=int, nargs="+", default=[3, 6, 12, 24])
    w.set_defaults(func=cmd_sweep)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

# Outstanding tasks

What is knowingly incomplete, and why. Kept as a live list rather than reconstructed at
submission, in the same spirit as `DEFECT_LOG.md`.

Items marked **withheld** are not bugs. They are claims the project declines to make
because the evidence does not support them — recording that is the point.

Ordering is by consequence, not by effort.

---

## Withheld claims — evidence does not support them

### W1. The confidence score is not calibrated, and is currently decorative
**Status:** measured, documented, claim withheld · `METRICS.md`, `DEFECT_LOG` 2026-09-02-01

Three attempts (125, then 777, then 3,705 examples across five densities and six seeds)
all put **every prediction in one of ten bins** at a base rate of 0.992. The layered
refusal architecture strips essentially every error before the confidence stage exists,
so what survives is correct regardless of its features. The reported ECE of 0.0002 is the
arithmetic of a single bucket and is published *with its bin count* so it cannot be
quoted as evidence of calibration.

**Consequence:** the composite score adds no measurable information beyond the
accept/refuse decision preceding it. The four layers demonstrably work — see the density
sweep — but their scalar summary does not.

**What would settle it:** BenchRec (external, labelled, ~69k rows, CC BY 4.0). Kaggle
requires authentication so it could not be fetched here.
`src/external/benchrec_ingest.py` reads it when present and reports its absence rather
than silently substituting the fallback.

    pip install kaggle
    kaggle datasets download -d benchmarkteam/benchrec-real-world-cash-reconciliation-dataset
    unzip -d data/benchrec <archive>.zip

### W2. The LLM on/off precision comparison is unmeasured
**Status:** boundary enforced and tested; comparison withheld · `DEFECT_LOG` 2026-09-02-02

The trust boundary is real: `NarrationFields` carries no payment id, candidate or score,
so a model cannot express a matching preference even in principle, and `parse_with_llm`
fills gaps only. Both properties are tested.

What is missing is a valid measurement. There is no API key in this environment, and the
offline stand-in applies the same word-filtering heuristic as the regex tier — it
recovers the payer name on the same 8 of 18 unparseable narrations and changes 0
verdicts. That agreement is a property of the stand-in sharing the parser's logic, not
evidence about what a model would contribute.

**To resolve:** set `ANTHROPIC_API_KEY` and re-run `python run.py match --verify` with
and without `--no-llm`.

---

## Correctness — should be fixed before anyone relies on a number

### C1. `search()` runs twice per many-to-one assignment
`match_with_margin()` calls `match()` (which searches) and then searches again to
compute the uniqueness margin. Doubles tier-3 cost on exactly the credits that are most
expensive. **Fix:** return `best_miss` alongside solutions in one pass. *~30 min.*

### C2. Greedy claiming resolves conflicts by sort order, not by evidence
Two credits competing for one payment are separated by iteration order. The permutation
gate now covers this (it inspects every credit seen in any pass), but covering a design
weakness with a detector is weaker than not having it. **Fix:** compute all candidates
first, then resolve conflicts by evidence weight. *~half a day, and it would let the gate
go back to being purely a safety net.*

### C3. Fellegi-Sunter prior drifts during the matching loop
λ = 1/pool_size uses the pool *as currently claimed*, so identical credits get different
priors depending on when they are processed. A leak from the greedy loop into the
probabilistic layer. **Fix:** compute pool sizes in a pre-pass. *~1 hr.*

### C4. `rupees_to_paise` raises on malformed input
`"₹ -100"` becomes `"- 100"` and throws `decimal.InvalidOperation` with no context. The
upload path validates before parsing, so this is currently unreachable from the API, but
the loader has no such guard. **Fix:** wrap with a message naming the field and row.
*~15 min.*

### C5. Audit hook is case-sensitive on a case-insensitive filesystem
`"_truth" not in path` misses `_TRUTH/` on NTFS. The hook is defence-in-depth — the
primary boundary is that the engine receives no paths at all — but a guard with a known
bypass should not stay one. **Fix:** casefold before comparing. *~5 min.*

---

## Testing gaps

### T1. `tier3_subsetsum.py` has no direct unit tests
The most complex algorithm in the system is exercised only end-to-end. Both bugs found in
it this session (near-miss pruning, and the margin normalisation behind it) were found by
hand-built cases, not by the suite. **Highest-value testing work available.**

### T2. No empty-batch tests
`match_once`, `score` and `render` with zero payments or zero credits are untested and
several ratios have unguarded denominators.

### T3. No malformed-input tests for `loaders.py`
Missing headers, bad dates and negative amounts are validated on the *upload* path but
not on the *load* path.

### T4. `MAX_POOL` and `MAX_SOLUTIONS` refusals have no dedicated test
Both are documented design invariants that only appear incidentally in batch runs.

### T5. Test writes a probe file into `src/recon/engine/`
`test_isolation.py` creates `_isolation_probe.py` in the package and deletes it in a
`finally`. A killed run leaves it behind and breaks later runs. **Fix:** write it to
`tmp_path`.

### T6. One vacuous assertion
`assert (ROOT / "src" / "scorer").exists() or True` can never fail.

---

## Performance

### P1. Permutation ensemble is sequential
K=8 passes are embarrassingly parallel. At current scale it costs ~1s; at 10k records it
is the dominant cost.

### P2. API re-reads and re-parses `run_output.json` on every request
Fine at 200 records, wasteful at scale. **Fix:** cache with an mtime check.

### P3. Tier 3 is DFS branch-and-bound, not meet-in-the-middle
`ARCHITECTURE.md` describes meet-in-the-middle. The implementation is pruned DFS, which
is what `MAX_POOL = 20` exists to bound. **Either implement MITM or correct the doc** —
the doc currently promises an algorithm the code does not use.

---

## Packaging and hygiene

### H1. `sys.path` manipulation instead of a real package
`run.py` and `api/main.py` both insert paths at import. **Fix:** `pyproject.toml` plus
`pip install -e .`.

### H2. `MAX_ROUNDS` and `_KNOWN_FEE_SLACK` are hardcoded
Every other engine parameter lives in `config.py`, and `_KNOWN_FEE_SLACK = 2` silently
duplicates `cfg.FEE_MODEL_MAX_RESIDUAL_PAISE`.

### H3. `RecordedTier` explanation templates use stale category keys
Templates key on `"fs_contradicted"`; the engine emits `fs_below_lower_threshold` and
`amount_name_conflict`, so explanations silently fall back to generic text.

---

## Explicitly out of scope

Not oversights — decisions, recorded so they are not mistaken for gaps.

| Excluded | Why |
|---|---|
| Accept/reject feedback loop | Would make the API a mutation surface over verdicts. The read-only routing table is the guarantee. |
| Multi-currency | Every amount is integer paise. Multi-currency needs FX rates at settlement time, which is a different problem. |
| MILP optimal subset-sum solver | At pool ≤ 20 with k ≤ 6, bounded search is sufficient and enumerates ALL solutions — which is what Layer 2 needs and what MILP's single optimum would not give. |
| Conformal risk control | Stretch goal, correctly cut. Would be meaningless anyway while W1 holds: there is no error rate to bound. |
| Live settlement reports | Needs Razorpay production access. |
| Cash-flow forecasting, settlement Q&A | Named out of scope in the README before any code was written. |
| Authentication on the API | MVP runs on localhost. A production blocker, not an MVP one. |

---

## If there were one more day

1. **T1** — direct tier-3 unit tests. Two real bugs hid there and only hand-built cases found them.
2. **C1** — single-pass search. Cheapest correctness-and-speed win available.
3. **W1** — download BenchRec and complete the calibration, or state the limitation in the submission and move on.
4. **C2** — conflict-resolution matching, which would retire an entire class of order-dependence rather than detecting it.

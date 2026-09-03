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

**What would settle it:** BenchRec (external, labelled, ~69k rows, CC BY 4.0). Blocked
two independent ways in this environment, both re-verified: Kaggle requires
authentication and no credentials are present, and the outbound network policy refuses
the host outright (`CONNECT tunnel failed, response 403`). Neither is something the
project can route around from here.
`src/external/benchrec_ingest.py` reads it when present and reports its absence rather
than silently substituting the fallback.

    pip install kaggle
    kaggle datasets download -d benchmarkteam/benchrec-real-world-cash-reconciliation-dataset
    unzip -d data/benchrec <archive>.zip

### ~~W2. The LLM on/off precision comparison is unmeasured~~ — **measured 2026-09-03**
**Status:** RESOLVED. A key was supplied, the command was run, and it reported VALID ·
`DEFECT_LOG` 2026-09-02-02, 2026-09-02-06

The trust boundary is real: `NarrationFields` carries no payment id, candidate or score,
so a model cannot express a matching preference even in principle, and `parse_with_llm`
fills gaps only. Both properties are tested. (One qualification the audit added, and it
matters: a `merchant_ref` reaches a payment id in **one hop** through `ReferenceIndex`,
so "cannot express a preference" is too strong as an absolute. See `REVIEW.md` §5.)

    python run.py llm-compare --seed 20260905 --verify

**Measured, live, against `claude-sonnet-5`:**

| | LLM OFF | LLM ON (live) | delta |
|---|---:|---:|---:|
| match rate | 88.66% | 89.18% | **+0.52 pp** |
| match precision | **1.0000** | **1.0000** | 0.0000 |
| refusal rate | 10.64% | 9.93% | −0.71 pp |
| assignments | 126 | 127 | +1 |
| correct assignments | 126 | 127 | +1 |

Of 141 credit narrations, **13** are unreadable by the regex tier. The live model filled
**8**, all of them merchant references and **no payer names** — the regex tier already
reads a name off all 13. Exactly **one** verdict changed: `bank_txn_0103`, from
`refuse:amount_name_conflict` to `assign:pay_SYN001441035`, and ground truth agrees.

**So the honest headline is: the LLM tier buys +1 assignment and costs precision
nothing.** Small, real, and measured rather than asserted.

**Three things the measurement showed that were not expected.**

1. **The live model is not better than the hand-written stand-in on this data.** The
   stand-in fills **9** of 13 gaps; the live model fills **8**. Both change the same
   single verdict and both leave precision at 1.0000. That is not a failure of the
   model — `recorded.py` predicted it in as many words: the stand-in was written for
   *this generator's* narration shapes. It does mean the generalisation claim is still
   unmade, and a shifted-distribution holdout is what would settle it.

2. **The live tier is non-deterministic at the field level and deterministic at the
   verdict level.** Repeated runs recovered 7 refs, then 8. But over **5 runs with a
   fresh live tier each time, the assignment map and refusal set hashed to one
   fingerprint** — the same one the offline stand-in produces. The layers downstream
   absorb the variance: a recovered reference only changes anything if it changes a
   tier decision, and the amount channel still has to agree. This is the strongest
   available evidence that the verification architecture does what it claims.

3. **It costs 30–35 seconds of wall clock, against 33 ms with the tier off** — a ~1000x
   slowdown, all of it sequential HTTP. See the demo-risk note in `REVIEW.md` §6: the
   live arm is a pre-computed artifact for the demo, not something to run on stage.

---

## Correctness — resolved

All five are fixed. Kept here with what the fix actually cost and what it revealed,
because "done" without a measurement is a claim rather than a record.

### ~~C1. `search()` runs twice per many-to-one assignment~~ — **fixed**
One `_decompose` call now produces the candidates and the uniqueness margin together;
the margin was always a property of the `SearchResult` the first search produced, not
new information. **Measured: 44 → 24 `search()` calls per batch, tier-3 cumulative time
0.311s → 0.162s.**

### ~~C2. Greedy claiming resolves conflicts by sort order~~ — **fixed**
The round is now propose-then-resolve. Every credit bids against the same frozen
`claimed` set; a bid is granted only if uncontested or if its evidence *strictly* beats
every rival. Evidence is (tier rank, FS weight) and nothing else — residual tightness and
subset size are deliberately excluded, because a tie means the evidence does not separate
two claims on the same money and the correct output is a refusal, not a tiebreak.

**Contests are density-dependent, and the reported density sits just below the
threshold.** Measured over three seeds per arm: 0 contests at ppw 3 and 6, **9 at
ppw 12**, 0 at ppw 24 (pools there exceed `MAX_POOL`, so tier 3 refuses before proposing).
Verified by 15 constructed tests including invariance across *every* permutation of the
proposal list. The permutation gate is now a safety net rather than load-bearing.

Re-running the sweep found a crash in this code on the day it was written — see
`DEFECT_LOG` 2026-09-02-07.

### ~~C3. Fellegi–Sunter prior drifts during the matching loop~~ — **fixed**
Blocking pool sizes are computed once, before any claiming. **96 of 129 assignments
(74%) had an inflated weight, by up to 1.875 bits; every one moved down; zero band
crossings, so no verdict changed.** The engine was overstating its evidence, not acting
on it.

### ~~C4. `rupees_to_paise` raises on malformed input~~ — **fixed**
One exception type, non-finite values rejected explicitly (`NaN` and `Infinity` are
*valid Decimals* and slipped past any parse-based validation), and the loader attaches
file, row and column. This also closed T3's missing-header case.

### ~~C5. Audit hook is case-sensitive~~ — **fixed** — casefolded.

---

## Testing — resolved

**128 → 203 tests.**

- ~~**T1** tier-3 has no direct unit tests~~ — 16 added. Both regression tests were
  checked by *reintroducing* the bugs they claim to catch; each reproduces the original
  "margin 1.0 on a credit with a near-twin" symptom.
- ~~**T2** no empty-batch tests~~ — added. Finding: the denominators were already
  guarded, so nothing needed fixing. The tests pin that.
- ~~**T3** no malformed-input tests for `loaders.py`~~ — added, on the load path.
- ~~**T4** `MAX_POOL`/`MAX_SOLUTIONS` refusals untested~~ — added.
- ~~**T5** test writes a probe into `src/recon/engine/`~~ — per-process name plus a
  conftest sweep. It must live inside the package (the hook identifies callers by module
  name) and it necessarily contains `ground_truth`, so a stray from a killed run failed
  the *static scan* test on every later run. Verified by planting one.
- ~~**T6** vacuous assertion~~ — removed.

Plus 7 API tests (there were none) and 12 for the LLM comparison.

---

## Performance and packaging — resolved

- ~~**P1** permutation ensemble is sequential~~ — parallel. **324ms → 117ms at K=8 on
  4 cores, byte-identical.** Falls back to sequential for an unpicklable LLM tier or any
  spawn failure.
- ~~**P2** API re-reads `run_output.json` per request~~ — cached on `(mtime_ns, size)`.
  No TTL, so a re-run is visible on the next request. Verified no handler mutates the
  now-shared payload, and pinned with a test.
- ~~**P3** doc promises meet-in-the-middle, code is pruned DFS~~ — `ARCHITECTURE.md` now
  states what the code does, why MITM would be the *wrong* tool (it finds *a* solution;
  Layer 2 needs all of them plus the near misses), and the real bound: 60,459 subsets.
- ~~**H1** `sys.path` manipulation~~ — `pyproject.toml`, `pip install -e .`. `config.py`
  moved to `src/` with `ROOT` re-anchored; every derived path verified unchanged. Found
  `python-multipart` was an undeclared dependency.
- ~~**H2** hardcoded constants~~ — `MAX_ROUNDS` to config; `_KNOWN_FEE_SLACK` now reads
  `FEE_MODEL_MAX_RESIDUAL_PAISE` instead of duplicating it.
- ~~**H3** stale explanation template keys~~ — keyed on `RefusalCategory` and covering
  all nine, with a test asserting the table and the enum cannot diverge in either
  direction.

**Not a listed item, found by profiling C3's fix:** `date_of` was ~380k calls over ~200
distinct timestamps. Memoised. `match_once` **41.6ms → 26.0ms**; end-to-end throughput
**345 → 760 rec/s**.

---

## Still open

### ~~O1. `partial` recall is 0/5~~ — **fixed: 7/7** · `DEFECT_LOG` 2026-09-02-08

It was a missing-evidence problem, as predicted — but the evidence was missing because
the GENERATOR hid it, not because it lived in an external system. `partial_payment`
shrank the credit and left the payment at full value, so Rs 7,854 vanished from a
Rs 21,999 payment while ground truth said `assign`. Unmatchable at any tolerance. The
third instance of the `refund_netted` shape.

A partial payment is a smaller payment against a larger invoice; the invoice is what is
left partial. Fixed there, and the fix exposed three more defects — an ambiguity-window
repair that orphaned the payments it moved (12.5% of seeds), a genuine ENGINE false match
the corrected benchmark revealed, and a test that was green *because* of the original
defect. All in the log.

Refusal correctness 16.67% → **100.00%**; the only remaining exception is the hand-placed
ambiguity case.

### O2. W1 — the confidence score is still uncalibrated
Unchanged and still blocked on BenchRec. See above.

### ~~O3. W2 — the LLM comparison is still withheld~~ — **closed 2026-09-03**
A key was supplied and the comparison ran live: **+1 assignment, precision unmoved at
1.0000**, and the verdict-level output identical across 5 runs. Full numbers under W2
above.

Two things had to be fixed before it could run at all, and both are recorded because
they were real rather than incidental. `select()` could not read a gitignored `.env`,
and this environment strips `ANTHROPIC_API_KEY` from the inherited shell — so the key
had arrived three times and the code had never once seen it. And the first live run had
to be killed: nothing on the tier's path memoised, so the same 13 narrations were bought
on every fixpoint round and every permutation pass. See `REVIEW.md` P0-3.

### ~~O4. Density sweep has not been re-run since C2 and C3~~ — **re-run**

| ppw | mean pool | match rate | precision | refusal rate |
|---:|---:|---:|---:|---:|
| 3 | 8.8 | 86.7% | 0.9986 | 8.0% |
| 6 | 15.2 | 88.9% | **1.0000** | 8.8% |
| 12 | 27.8 | 89.0% | **1.0000** | 7.8% |
| 24 | 54.0 | 63.8% | **1.0000** | 17.8% |

The claim holds: as ambiguity rises the engine declines more work rather than getting it
wrong. Coverage degrades 86.7% → 63.8%; precision does not move off 1.0000.

Running it rather than recommending it found a crash in the conflict resolver committed
the same day, in a path 203 passing tests did not reach (`DEFECT_LOG` 2026-09-02-07).

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

### ~~O6. The batch is still too clean to demonstrate the refusal machinery~~ — **closed**
Not by changing the reported density, but by making the data honest. Seven new defect
categories took refusal rate from 0.74% to 9.2% at the primary seed and 7.9–13.8% across
the sweep, with precision holding at 1.0000 in every arm. The refusal machinery is now
visible at every density rather than only at ppw=24, so the ppw=24 swap is no longer
needed. Original item kept below.

### O6 (original). The batch is still too clean to demonstrate the refusal machinery
**Partially addressed, and the fix did not do what this item predicted.**

The headline now reports `ppw=12` beside `ppw=6` (`--compare-density`, default 12). That
is worth having on its own terms: one density in the headline reads as a property of the
engine rather than of the engine at one crowding level.

But it does **not** achieve what this item wanted. At seed 20260905, `ppw=12` produces
the *same single exception* as `ppw=6` — one, the hand-placed ambiguity case — even
though the worst pool grows 15 → 27 and crosses `MAX_POOL`. Refusal rate moves 0.7% →
0.8%. Refusals only rise materially at **ppw=24** (4.9%).

So the density sweep remains the only place the refusal behaviour is actually visible,
and it stays load-bearing for the argument. Reporting `ppw=24` as the second arm would
demonstrate it, at the cost of a headline arm whose match rate (86.6%) is not
representative of the reported configuration. That trade is a presentation decision, not
a defect, and it is left open deliberately.

### ~~O7. `assert_truth_is_satisfiable` should also run inside the sweep~~ — **done**
The sweep now asserts every batch it builds. That is exactly where the orphaning defect
hid: `generate` checked the primary seed and the sweep never checked its own five, so a
sweep could quietly average over unsatisfiable ground truth and report the generator's
bugs as the engine's coverage. All 20 batches (4 densities x 5 seeds) pass.

### O8. Two named model limitations, recorded not scheduled
`split_settlement` (one payment, many credits) and `chargeback_debit` (the engine reads
credits only) are outside the model rather than merely hard. Both refuse correctly and
both cost real coverage. `docs/ARCHITECTURE.md` states what lifting each would take —
in both cases a different engine, not a patch. Recorded so a correct-looking refusal
cannot hide an unmodelled relation.

### O9. `third_party_payer` refusals are correct but expensive
Three of seven third-party payments are refused with `amount_name_conflict`. The split
is not arbitrary: the ones that match carry a quoted invoice reference, and that
evidence outweighs the name disagreement. Refusing the rest is the right call — the
amounts reconcile and the counterparty does not — but it is the largest remaining
source of conservative refusals, and an investigating agent (see `AGENTIC.md`) checking
whether a payer is an authorised group entity is exactly the evidence that would close
it.

---

## Hostile audit, 2026-09-03 — `REVIEW.md`

Audited as a staff engineer and a buildathon judge: ran the pipeline rather than reading
the docs, which is how three of the findings surfaced. Full write-up in
[`REVIEW.md`](../REVIEW.md).

**All four P0s fixed the same day.** Suite 295 → 323.

| | Finding | Resolution |
|---|---|---|
| **P0-1** | `reports/run_output.json` shipped with an empty verification block, and the UI returned `null` for it — so the four-layer claim rendered as *nothing*, silently | The root cause was `test_cli_robustness.py`, which shelled out to `run.py match` with `cwd=ROOT` and no `--verify` on **every pytest run**. Redirected to a tmp dir; the payload now carries an explicit `status`; the UI renders its absence as a warning strip |
| **P0-2** | No assignments view — 126 of 141 outcomes invisible, and no way to ask why a match was made | The explainability engine, below |
| **P0-3** | A live API key turned the demo into a multi-minute hang | Cache, 10 s timeout, 1 retry, call cap. 0.40 s |
| **P0-4** | `decomposition_out_of_bounds` fired on pools of 1, 1, 1, 2, 4, 4 while telling operators there were "too many candidates to search" | Split into `pool_exceeded` and `no_subset_fits` |

**The finding worth keeping in view.** `interface.py` claims a model *"structurally
cannot"* express a matching preference. It does not need to: `merchant_ref` →
`ReferenceIndex` → `invoice_no` → payment id is **one hop**, and tier 1 outranks
everything in `evidence_key`, so a plausible invoice number selects a payment and wins
contested money. Measured against the offline stand-in: **+1 assignment and 9 credits
moved from tier 2 to tier 1.** Amount conservation still gates it, so the mitigations are
real — but the absolute claim is not, and `REVIEW.md` §5 proposes the honest wording.

### Explainability engine — shipped 2026-09-03
Three reading levels for every credit, because the three audiences want different things:
a **plain sentence** (no jargon), **typed evidence links** to the actual payment, invoice
and bank rows, and the **full transcript** with the arithmetic in paise.

The transcript is the *actual computation*, recorded as it runs — not a description
written afterwards. Nothing in `recon/explain/` calls a model, and a test parses both
modules' ASTs to assert neither imports one, so an explanation cannot drift from the
decision it describes: there is no second inference that could disagree.

**Recording is inert, and that is the load-bearing property.** `match_once` is pure and
MR1 depends on it, so `tests/test_explain.py` hashes the assignment map, the refusals
with their categories and rupees at risk, and the no-candidate set with recording on and
off, and requires them byte-identical.

Two UI bugs were found by driving the real page in Chromium, neither visible in the build
or in the diff: an effect-dependency deadlock that left every explanation panel loading
forever while its request returned 200 every time, and a `certain_fee` field missing from
the payload that made the Matches card assert *"fee known exactly: no"* on **all 127**
assignments — while the transcript two lines below it said the opposite.

---

## Code review, 2026-09-02 — 14 findings, none covered by the suite

Full write-up: [`REVIEW_2026-09-02.md`](REVIEW_2026-09-02.md). Reviewed the whole session
(14 commits, 48 files, ~9.6k insertions) at high effort. **All 265 tests pass**, so every
finding is something the suite does not reach.

Four were reproduced directly; the rest are read from the diff and marked as unverified,
and that distinction is kept rather than collapsed.

**RESOLVED 2026-09-03 — all 14.** Suite 265 → 287, precision 1.0000 at every density arm.
Two fixes produced further defects while being applied, both recorded. The list below is
kept as written so the order and the reasoning stay legible.

**Was:**

1. **R1 + R2** — the headline and `run_output.json` can name a seed and density that did
   not produce them. Reproduced: a batch generated at seed 77771 / ppw 12 prints
   `seed=20260905 density=6` and writes a payload inconsistent with itself. Same bug class
   as the manifest guard added this session, which closed the loud path and left the
   silent one open.
2. **R3** — `third_party_payer` is ~29% mislabelled (2 of 7 at the primary seed carry the
   *correct* payer name), so the `OUTCOME BY DEFECT` numbers for it — and the conclusion
   drawn from them — rest on a partly wrong cohort. Fix the label, then re-measure and
   restate or withdraw.
3. **R4** — `pip install -e .` produces a broken `pramana` entry point and an unimportable
   `api.main`. Reproduced. H1 removed the `sys.path` bootstrap on a premise that is not
   true as it stands.
4. **R5 + R6** — two `assert_truth_is_satisfiable` call sites abort their command instead
   of reporting, while adjacent assertions in the same loop bodies are handled.
5. **R9 + R10** — two guards that would fail silently: the ambiguity guard's claimed safety
   margin is zero, and the satisfiability assertion covers two unreachability shapes of
   three.
6. **R11** — every assigned credit parses its narration twice, doubling live LLM cost.
7. **R7, R8, R13, R14** — correctness of things nothing reads yet.

**The pattern worth noting:** three of the top four are *incomplete fixes rather than new
mistakes*. The session's own lesson — the metric that looked right — held for the
session's own work.

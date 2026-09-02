# Architecture

## The problem this is built around

Producing candidate reconciliation matches is easy. Knowing which candidates to
trust is the unsolved part.

The commercial category already knows this. Vendors report auto-match rates of
90–99%, and their own analysts now say headline accuracy is no longer the
differentiator — what matters is what happens in the 1–10% that don't auto-match.
[Kognitos, 2026](https://www.kognitos.com/blog/top-ai-platforms-automated-reconciliation-2026/):

> "The differentiator is no longer the headline match rate. It is what happens in
> the 1–10% of transactions that don't auto-match, and whether the audit trail
> explains the resolution."

**Commercial vendors publish coverage and do not publish match precision.** That is
the specific gap. It is worth stating precisely, because the research community does
not share it: BenchRec, the only public real-world reconciliation benchmark, treats
precision as a *hard constraint* and coverage as the thing to optimise beneath it —

> "In production, erroneous matches are costly. It is better to leave a transaction
> unmatched (and leave it for manual review) than to match it incorrectly. The
> required precision for fully automated matches it 99.9%… The match rate should be
> optimized subject to the match precision meeting the required level."
> — [BenchRec data card](https://www.kaggle.com/datasets/benchmarkteam/benchrec-real-world-cash-reconciliation-dataset)

So the claim this project makes is not "we noticed verification matters." It is:
**the system carries its own verification apparatus, and that apparatus works at
runtime on data where no ground truth exists.**

That last clause is the hard part, and it determines the whole design. Anything that
needs the right answer in order to check the answer is useless on a merchant's own
books. Every mechanism below is chosen because it works without labels.

---

## The trust boundary

**Deterministic code decides every match. No LLM output ever creates, confirms,
scores, or ranks a match assignment.**

The LLM does exactly two jobs:

1. Parse unstructured bank narration strings into structured fields, *when and only
   when the deterministic regex tier fails*.
2. Write human-readable exception explanations and proposed resolutions, *after*
   the assignment or refusal has already been decided.

Both are downstream of, or subordinate to, deterministic logic. Neither can change
a verdict.

This is enforced structurally, not by convention:

- `src/recon/llm/` exposes a single interface. Its return type carries parsed
  *fields* and *prose*, never a match, a score, or a confidence.
- The engine runs to completion with `src/recon/llm/null.py` substituted. There is
  no code path that requires the LLM tier to be present.
- **Precision is reported both ways** — `--no-llm` alongside the default. If the LLM
  tier makes precision worse, that is what the metrics block says.

### The ground-truth isolation boundary

The generator writes ground truth to `data/generated/_truth/`. The matching engine
must never read it. This is enforced three ways, strongest first:

1. **Function signatures.** Everything under `src/recon/engine/` accepts dataclass
   objects only — no file paths, no directory handles, no config lookups that
   resolve to paths. `run.py` loads the three sides and passes them in. The engine
   cannot open the truth file because it does not open anything.
2. **Import-time audit hook.** `src/recon/__init__.py` installs a `sys.addaudithook`
   on the `open` event that raises if a path containing `_truth` is opened from a
   frame whose module lives under `recon.`. This is defence against a careless
   future edit, not the primary mechanism.
3. **An executable test.** `tests/test_isolation.py` copies the generated data to a
   temporary directory **with `_truth/` and `data/benchrec/` both deleted**, runs
   the full engine *and all four verification layers*, and asserts that the run
   completes and its output is identical to the run with those directories present.

The scorer is a separate top-level package (`src/scorer/`), not a submodule of
`recon`. Data flows engine → `RunOutput` → scorer. Nothing flows back.

`src/external/` also sits outside the boundary and may read BenchRec labels freely —
that is a different dataset, not the run under evaluation. Its only outputs into the
engine are **fitted constants** written into `config.py` as literals, stamped with
their source and fit date. No BenchRec data is loaded at runtime.

### One deliberate asymmetry: the engine does not share the generator's fee model

If the generator and the engine both called the same fee function, MR4
(conservation) would be tautological — the engine would reconcile because it was
inverting the exact function that produced the data, and the check would prove
nothing.

So they are deliberately different:

- The **generator** uses the real observed per-method schedule with exact paise
  arithmetic, including the floored GST established in `DEFECT_LOG.md` 2026-09-01-01.
- The **engine** knows only a *rate band* (`MDR_RATE_BAND`) and never learns any
  individual record's true rate — except where Razorpay's genuine `fee` field is
  populated, which is real API output legitimately available at runtime.

Conservation therefore tests a real constraint rather than an identity.

---

## The four verification layers

Each layer answers a different question, and none of them requires knowing the right
answer.

### Layer 1 — Metamorphic relations, and a runtime ambiguity gate

Metamorphic testing is the formal answer to the oracle problem. A *metamorphic
relation* is a necessary property of correct behaviour across multiple executions;
a violation proves a defect without anyone knowing the correct output for any input.

| Relation | Statement |
|---|---|
| **MR1** | *Permutation invariance.* Shuffling input row order on all three sides must not change any match assignment. |
| **MR2** | *Split invariance.* Replacing a payment with two payments summing to the same amount — **with the bank credit adjusted by the recomputed fee delta** — must not change the settlement-level grouping. |
| **MR3** | *Augmentation stability.* Adding a constructively unmatchable record must leave all previously produced matches unchanged. |
| **MR4** | *Conservation.* Assigned payments + MDR + TDS must equal the bank credit within tolerance. Money neither appears nor vanishes. |
| **MR5** | *Residual closure.* After matching, unassigned totals on each side must reconcile. |
| **MR6** | *Idempotence.* Removing all assigned records and rerunning must produce zero new assignments. |

MR1, MR2, MR3 and MR6 are true metamorphic relations (they compare multiple
executions). MR4 and MR5 are single-run conservation invariants. `METRICS.md` reports
them separately and does not blur the distinction.

**Two notes on relations that are easy to state incorrectly.**

*MR2 cannot be stated as originally conceived.* MDR is charged per payment with
paise-level rounding, so splitting ₹1000 into ₹600 + ₹400 changes the total fee by up
to a paisa, which changes the bank credit. "Same amount, bank side unchanged,
assignment unchanged" cannot all hold simultaneously. The relation must adjust the
credit by the fee delta and then assert *settlement-level grouping* invariance.

*MR3 needs a constructive guarantee.* "A record that cannot participate in any
existing match" must be built so, not hoped so: amount larger than every bank credit,
date outside every settlement window, payer name absent from the registry.

#### MR1 is not only a test — it is the runtime refusal gate

This is the load-bearing design decision in the project.

If permuting input order changes which subset is assigned to a bank credit, that
assignment was determined by **iteration order rather than by the data**. That is
knowable at runtime, with no labels.

So the engine's *primary execution path is an ensemble*, not a single pass:

1. Run the matching core `K = 8` times over independently shuffled orderings.
   (Permutations derive from the fixed seed, so the shuffles are themselves
   deterministic and reproducible.)
2. `stability == 1.0` → the candidate proceeds to confidence scoring.
3. `stability < 1.0` → **refuse.** Emit `order_dependent_assignment` listing every
   distinct assignment observed, its frequency, and the rupees at risk. It never
   enters the accepted set.

This buys ambiguity detection without enumerating every candidate subset, which is
the expensive part. Metamorphic violations are reported as a first-class metric
alongside precision.

### Layer 2 — Uniqueness testing and principled refusal

For many-to-one settlement decomposition, the engine enumerates **all** candidate
subsets within tolerance, not the first one that fits. If exactly one satisfies the
constraint, that is strong evidence. If two or more do, the constraint has not
identified an answer — it has identified several — and the engine **refuses to
assign** and emits an exception showing every candidate with rupees at risk.

This is not a stylistic preference. Deciding whether a knapsack instance has a
unique optimal solution is Δ₂P-complete: strictly harder than finding an optimal
solution. **Verifying uniqueness is provably harder than producing an answer.**

The relevant prior art is the Subset Sum Matching Problem, formalised by J.P. Morgan
AI Research ([arXiv 2508.19218](https://arxiv.org/abs/2508.19218)) as an abstraction
of financial reconciliation. Two things about it shape this design:

- **Its benchmark was generated but never publicly released**, so it is cited here,
  not used.
- **Its algorithms terminate on the first valid match and do not address non-unique
  solutions at all.** The state-of-the-art formalisation of financial reconciliation
  finds *an* answer; it does not ask whether that answer is *the* answer.

That gap is precisely what Layer 2 fills. Their optimal solver is MILP; at n ≈ 200
with date-window bucketing, bounded search with a hard candidate cap is sufficient,
so no MILP solver is built here.

#### The algorithm, precisely

`src/recon/engine/tier3_subsetsum.py` is **depth-first search with two exact prunes**
over a pool sorted ascending by lower bound. It is not meet-in-the-middle, and it is
not dynamic programming. Stating that here because a document that names an algorithm
the code does not implement is the exact failure this project spends its argument
criticising, and because the distinction is load-bearing rather than pedantic:

- **Both prunes are exact, never heuristic.** *Overshoot*: all amounts are positive, so
  once the running lower bound exceeds `target + ε` no extension can come back down —
  and because the pool is sorted, no later sibling can fit either. *Unreachable*: suffix
  sums bound the most the remaining candidates could add; if the running upper bound
  plus that maximum still falls short, stop. A heuristic prune could silently discard a
  second solution and turn a genuine ambiguity into a confident wrong answer, which is
  the one outcome Layer 2 exists to prevent.
- **Meet-in-the-middle would be the wrong tool anyway.** MITM is the right choice when
  you want *a* solution from a large `n` — it splits the pool in half and hashes partial
  sums. Layer 2 needs *every* solution up to `MAX_SOLUTIONS`, and it needs the near
  misses too, because `best_miss` is what makes the uniqueness margin meaningful.
  Recovering all solutions from a MITM hash table costs the enumeration back.
- **The bound is on subsets, not on the power set.** With `MAX_POOL = 20` and
  `MAX_SUBSET_K = 6` the search space is Σ C(20, k) for k ≤ 6 = **60,459** subsets per
  credit before pruning, against 2²⁰ ≈ 1.05 M for the unbounded power set. At a pool of
  28 the same bound gives 499,177 — an 8× increase, multiplied by ~135 credits and by
  `K = 8` permutation passes, which is why `MAX_POOL` is a refusal threshold rather
  than a number to raise when a batch does not fit.

*(`DEFECT_LOG.md` 2026-09-01-06 uses "meet-in-the-middle" for this cost model. The
arithmetic quoted there — 38,760 and 376,740 — is C(n, 6), i.e. bounded enumeration, so
the reasoning holds and only the label was wrong. The entry is left as written because
that log is append-only; the correction lives here.)*

### Layer 3 — Fellegi–Sunter evidence weights with a two-threshold band

Hand-tuned similarity scores are replaced with the classical probabilistic record
linkage model — the statistical foundation under Splink and fastLink. It computes a
likelihood ratio from field-level agreement patterns, giving calibrated evidence
weights per field rather than arbitrary coefficients:

```
M = log₂(λ / (1 − λ)) + Σᵢ log₂(mᵢ / uᵢ)
Pr(match | observation) = 2^M / (1 + 2^M)
```

The decision rule uses **two thresholds, not one**: above the upper, declare a
match; below the lower, declare a non-match; between them, route to clerical review.
That middle band is a formalised "I don't know" with fifty years of provenance, and
it is what populates the exception list. Thresholds are set at **weight 4 (≈95%) and
weight 7 (≈99%)**, the published correspondences from the Splink theory guide.

On estimating the parameters without breaching the boundary:

- **`u`** (chance agreement) is estimated analytically from each field's value
  distribution *within the batch*. Unsupervised, no labels.
- **`m`** (agreement given a true match) is fitted on **BenchRec** — external,
  labelled, ~69k rows, CC BY 4.0. Fitting `m` on the run's own ground truth would
  breach the isolation boundary, and EM on a single 200-record batch is too unstable
  to trust. Fitting on a separate dataset is ordinary train/test hygiene.

### Layer 4 — Materiality stratification and projected error

The audit profession has machinery for assuring a population you cannot fully
verify. This borrows it directly, following PCAOB **AS 2315** (current; amendments to
¶.11 effective 15 Dec 2026):

- Set a **tolerable misstatement** threshold in rupees (¶.18, ¶.18A).
- **Stratify**: verify 100% of exceptions at or above materiality; sample below it
  (¶.22).
- Report a **projected error** with a confidence bound over the unsampled remainder
  (¶.26, and footnote 5 for summing across strata).

This turns rupee-ranking from a UI nicety into an audit-standard method, and lets the
system make a defensible claim about the whole batch without checking every row.

---

## How the layers compose into one confidence score

```
confidence = permutation_gate × σ( w₁·z(residual_tightness)
                                 + w₂·z(uniqueness_margin)
                                 + w₃·fs_weight_scaled )
```

- `permutation_gate ∈ {0, 1}` — **hard, not a discount.** Order-dependence refuses.
- `residual_tightness` — how close conservation holds (Layer 1, MR4).
- `uniqueness_margin` — gap to the next-best candidate subset (Layer 2).
- `fs_weight_scaled` — the match weight through the two-threshold band (Layer 3).

Weights and the calibration map are **fitted on BenchRec and evaluated on the
reported run** — held out by construction, and fitted on real Tier-1 bank data rather
than on this project's own generator output.

**Why this composes.** Conservation constraints reason over *amounts*.
Fellegi–Sunter reasons over *names, dates and references*. These are independent
evidence channels that cannot fail the same way. A match corroborated by both is
qualitatively stronger than one supported by either alone — and that is the entire
justification for combining them into a single number.

### Calibration is the claim that transfers

Raw precision is a fact about this batch. **Calibration is a claim about the
method.** If the engine says 0.9, it should be right about 90% of the time; if that
holds on held-out data, it is a reason to believe the number on books where no
ground truth exists. The reliability diagram and expected calibration error (ECE)
are reported with `n` stated.

### Where the layers disagree

The disagreements are the most informative output the system produces, and each has
a defined action:

| Conflict | What it means | Action |
|---|---|---|
| Conservation tight, FS weight low | Amounts fit perfectly, names and references don't. Coincidental subset-sum, or a legitimately renamed payer. | Review band. `amount_name_conflict`. **No auto-accept.** |
| FS weight high, conservation loose | Right counterparty, wrong money. Partial payment, unmodelled deduction, or a missing record. | `unexplained_residual`, with the rupee gap named. |
| Unique subset, but unstable under permutation | Uniqueness found one answer, but the filter feeding it was order-dependent. This indicts the **pipeline**, not the data. | Hard failure: refuse, and log as an engine defect in `DEFECT_LOG.md`. |
| Multiple subsets fit, FS strongly favours one | Amount evidence is ambiguous; name evidence is not. | **Still refuse.** Documented stance below. |

That last row is a deliberate architectural commitment: **Fellegi–Sunter may not
break an amount-tie.** Allowing it to would let the weaker evidence channel override
the stronger one, which is exactly the failure mode this system exists to prevent.
Both candidates are emitted, ranked by FS weight, and a human picks.

---

## Two named limitations of the model

Both are places where the engine **refuses correctly** and the refusal nonetheless costs
real coverage. They are recorded here because a correct-looking refusal is the easiest
possible place for an unmodelled relation to hide: the metrics say the engine declined,
ground truth says declining was right, and nothing anywhere says the engine *could not
have done otherwise*.

### One payment, many credits — `split_settlement`

Razorpay splits a settlement for on-demand payouts and when a batch crosses a limit, so
one payment's net arrives as two separate bank credits.

**The engine cannot represent this.** `claimed` is a set, so a payment is taken exactly
once, and every tier asks the same question — *which subset of payments sums to this
credit?* There is no way to express half a payment on either side of that question.

Refusing is genuinely the right output: posting a part-settlement against a whole
payment is a wrong answer, not a partial one. But the relation is outside the model, not
merely hard, and no amount of tuning reaches it.

*What it would take:* the claim unit would have to become (payment, fraction) rather than
payment, and Layer 2's uniqueness test would have to enumerate over partitions rather
than subsets — which is a strictly larger search whose uniqueness question is harder
again. That is a different engine, not a patch, which is why it is documented rather
than attempted.

### The engine reads credits only — `chargeback_debit`

`match_once` iterates `t for t in inputs.bank_txns if t.is_credit`. Every debit on the
statement — a chargeback, a reversal, a bank fee, a payout — is invisible to it: not
matched, not refused, not counted.

This went unnoticed for the life of the project for a simple reason: **the generated
statement contained no debits at all.** The engine had never been shown the half of a
bank statement it ignores by construction, so nothing could reveal the gap.

Ground truth deliberately creates **no link for a debit**. Inventing one would score the
engine against a verdict it structurally cannot produce — a permanent miss that no
engine work could ever close, which is scoring theatre rather than measurement. The
metrics block reports the unexamined lines and their value instead, under `NOT EXAMINED`,
so the exception list is not mistaken for a complete account of the statement.

*What it would take:* a debit is not a credit with a sign flipped. It reverses a prior
assignment, which means the engine would need to un-post a match it has already made and
the conservation relations (MR4, MR5) would need to balance across time rather than
within one batch. Also a different engine.

---

## Out of scope

Deliberately excluded, and not partially built: cash-flow forecasting; settlement
Q&A or chat; multi-currency; live settlement reports or anything requiring Razorpay
production access; TDS/GST tax-line matching as a user-facing feature (deductions
still appear in the data); an accept/reject feedback loop; a MILP optimal solver for
subset-sum; and conformal risk control.

---

## Attribution

**BenchRec: A Real-World Cash Reconciliation Dataset**, Operartis / the BenchRec
initiative, originally released for the ICAIF 2023 Benchmark Competition. Licensed
**CC BY 4.0**. Used here as an external calibration and Fellegi–Sunter training set.
Not redistributed in this repository — `data/benchrec/` is gitignored.

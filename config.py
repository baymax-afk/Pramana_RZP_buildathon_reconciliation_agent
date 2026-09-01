"""
Frozen run configuration for the three-way reconciliation engine.

EVERY tolerance, threshold, bound and seed the system uses lives here and is set
ONCE, before any run. Nothing in this file may be tuned per-record, per-batch, or
in response to a disappointing metric. That discipline is the whole point: a
tolerance fitted to the data it is evaluated on measures nothing.

If a value here changes, the change belongs in docs/DEFECT_LOG.md with a reason.

All monetary values are in PAISE (integer). Rupee-denominated inputs -- the bank
statement and the invoice ledger, which are rupee strings with 2dp, as real Indian
bank exports are -- are converted to paise at ingest and never handled as floats.
"""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------
# Seeds. Both are printed in the metrics block. The second exists so the
# reported numbers can be shown not to be cherry-picked.
# --------------------------------------------------------------------------
SEED_PRIMARY = 20260905
SEED_SECONDARY = 77771

# --------------------------------------------------------------------------
# Batch shape.
#
# TARGET_POOL_SIZE is the PRIMARY generator parameter and the settlement date
# range is derived against it: the range widens with N so that payments-per-window
# stays fixed. Getting this backwards -- fixing the date range and letting density
# float -- is what breaks the subset-sum search at scale. See ARCHITECTURE.md,
# "The density invariant".
# --------------------------------------------------------------------------
N_PAYMENTS = 200
TARGET_POOL_SIZE = 12          # payments per settlement window
SETTLEMENT_WINDOW_DAYS = 3     # a credit on date D may only cover payments in [D-3, D]
DENSITY_SWEEP = (6, 12, 24)    # payments_per_window settings for the reported sweep

MIN_PAYMENT_PAISE = 10_000     # Rs 100.00 -- floor exists to keep TOL_ABS_PAISE
MAX_PAYMENT_PAISE = 5_000_000  # Rs 50,000.00   safely below the smallest payment

# --------------------------------------------------------------------------
# Tolerances. Fixed before the run, identical for every record.
#
# TOL_ABS_PAISE must stay STRICTLY below MIN_PAYMENT_PAISE, and by a wide margin.
# If tolerance approaches the smallest payment, then a subset S and the subset
# S + {one tiny payment} both satisfy the constraint, every many-to-one result
# becomes meaningless, and the uniqueness test silently degrades into noise.
# tests/test_tolerance_sanity.py asserts this and fails the build if violated.
# --------------------------------------------------------------------------
TOL_ABS_PAISE = 100            # Rs 1.00 absolute
TOL_REL_BPS = 2                # 2 basis points, additive, for large credits

assert TOL_ABS_PAISE * 100 <= MIN_PAYMENT_PAISE, (
    "Tolerance is within 100x of the smallest payment; subset-sum uniqueness "
    "cannot be trusted at this setting."
)

# --------------------------------------------------------------------------
# Fee model.
#
# These are the ENGINE's constants, and they are deliberately a BAND, not the
# generator's exact per-method schedule. The engine must never learn any single
# record's true rate, or MR4 (conservation) becomes tautological -- the engine
# would merely be inverting the function that produced the data. The generator
# keeps its own exact schedule in src/recon/generator/fees.py.
#
# The band's placement is empirical, not invented. Across all 10 genuinely captured
# test-mode payments (wallet and netbanking, Rs 215 to Rs 14,750), the MDR base is
# EXACTLY 2.200% of the amount:
#     base = fee - tax = 0.022 * amount        (all 10 records, no exceptions)
#
# GST on that base is ~18%, but the exact rounding rule is NOT recoverable from 10
# observations -- no single mode fits (floor misses 5/10, ceil 7/10, round 6/10).
# See DEFECT_LOG.md 2026-09-01-01, which records an earlier WRONG conclusion here.
#
# What is solid, and all the engine needs:
#     base = round(0.022 * amount);  tax = round(0.18 * base);  fee = base + tax
# predicts the true fee within [-1, +2] paise on every real record. Against
# TOL_ABS_PAISE = 100 that is a 50x margin, so the residual rounding ambiguity is
# absorbed by tolerance and never needs to be resolved.
#
# The band below brackets 2.200% with room for payment methods not yet observed.
MDR_RATE_BAND = (0.018, 0.025)
GST_RATE = 0.18
GST_ROUNDING = "round"   # best fit; residual is +/-2 paise, absorbed by tolerance
FEE_MODEL_MAX_RESIDUAL_PAISE = 2   # measured against 10 real captured payments

# --------------------------------------------------------------------------
# Layer 2 -- bounded subset-sum and uniqueness testing.
#
# MAX_SOLUTIONS is not a performance knob. Reaching the cap is itself a REFUSAL
# verdict: if eight distinct decompositions satisfy the constraint, the constraint
# has not identified an answer, it has identified eight.
# --------------------------------------------------------------------------
MAX_POOL = 20                  # candidates per credit; above this -> refuse, never guess
MAX_SUBSET_K = 6               # max payments per many-to-one decomposition
MAX_SOLUTIONS = 8              # enumerate up to this many; reaching it means refuse

# --------------------------------------------------------------------------
# Layer 1 -- permutation ensemble.
#
# PERMUTATION_K is a RUNTIME parameter, not merely a test parameter. The engine's
# primary execution path runs the matcher K times over independently shuffled
# input orderings; any assignment not stable across all K was decided by iteration
# order rather than by the data, and is refused. See ARCHITECTURE.md, Layer 1.
# --------------------------------------------------------------------------
PERMUTATION_K = 8              # reported runs always use this
PERMUTATION_K_FAST = 3         # --fast, dev loop only, never for reported numbers
PERMUTATION_K_MR1_TEST = 16    # MR1-as-a-test, fresh seeds, stricter than runtime

# --------------------------------------------------------------------------
# Layer 3 -- Fellegi-Sunter.
#
# Thresholds are in Splink match-weight units (log2 Bayes factors), where
#     M = log2(lambda / (1 - lambda)) + sum_i log2(m_i / u_i)
#     Pr(match | obs) = 2^M / (1 + 2^M)
# so weight 4 is about 95% and weight 7 is about 99%. These are the published
# correspondences from the Splink theory guide, not numbers chosen to make the
# results look good.
#
# Below LOWER  -> non-match, refuse.
# Between      -> clerical review band; this is where exceptions come from, and
#                 confidence is capped at FS_REVIEW_CONFIDENCE_CAP.
# Above UPPER  -> contributes at full weight.
# --------------------------------------------------------------------------
FS_THRESHOLD_LOWER = 4.0
FS_THRESHOLD_UPPER = 7.0
FS_REVIEW_CONFIDENCE_CAP = 0.6

# m-probabilities: P(field agrees | records truly match).
#
# FALLBACK PRIORS ONLY. These are used only if the BenchRec fit has not run.
# Fitting m on the run's own ground truth would breach the isolation boundary, and
# EM on a single 200-record batch is too unstable to trust -- so the real values
# are fitted on BenchRec (external, labelled, ~69k rows) by src/external/fit_fs.py,
# which overwrites the block below and stamps it with the fit date.
FS_M_PRIORS = {
    "reference": 0.99,
    "amount": 0.98,
    "date": 0.95,
    "name": 0.90,
}
FS_M_SOURCE = "fallback priors (unfitted) -- run src/external/fit_fs.py to replace"

# u-probabilities are NOT set here. They are chance-agreement rates, estimated
# analytically from each field's value distribution within the batch at runtime --
# unsupervised, no labels, no boundary crossing.

# --------------------------------------------------------------------------
# Layer 4 -- materiality stratification (PCAOB AS 2315 paras .18, .18A, .22, .26).
#
# Exceptions at or above materiality are verified 100%. Below it, sample and
# project the misstatement over the unsampled remainder with a confidence bound.
# --------------------------------------------------------------------------
MATERIALITY_PAISE = 500_000    # Rs 5,000.00 tolerable misstatement
SAMPLING_RATE_BELOW_MATERIALITY = 0.25
PROJECTION_CONFIDENCE = 0.95

# --------------------------------------------------------------------------
# The hand-placed ambiguity case.
#
# Four payments, four DISTINCT net amounts, two pairs summing identically:
#     {50000, 30000} = 80000 = {45000, 35000}
# Distinct amounts matter -- if the pairs shared amounts, payer identity alone
# could break the tie. Here no amount-level signal can. All four sit in one
# settlement window with the same payment method, so date and method cannot break
# it either, and the credit's narration is deliberately generic with a UTR that
# matches no payment, so tier 1 cannot fire and the Fellegi-Sunter name channel
# has near-zero and EQUAL evidence for all four.
#
# The engine must refuse this credit and emit both candidates. Ground truth labels
# it expected_verdict="refuse", so refusing scores CORRECT and assigning either
# subset scores as a false match. Guarded twice: the generator brute-forces the
# window and asserts exactly two subsets fit, and tests/test_ambiguity.py asserts
# the engine's verdict.
# --------------------------------------------------------------------------
AMBIGUITY_NET_PAISE = (50_000, 30_000, 45_000, 35_000)
AMBIGUITY_CREDIT_PAISE = 80_000
AMBIGUITY_EXPECTED_CANDIDATES = 2

assert (
    AMBIGUITY_NET_PAISE[0] + AMBIGUITY_NET_PAISE[1]
    == AMBIGUITY_NET_PAISE[2] + AMBIGUITY_NET_PAISE[3]
    == AMBIGUITY_CREDIT_PAISE
), "Ambiguity case does not actually collide."
assert len(set(AMBIGUITY_NET_PAISE)) == 4, (
    "Ambiguity case amounts must be distinct, or identity alone breaks the tie."
)

# --------------------------------------------------------------------------
# Paths. The engine is NEVER handed any of these -- run.py loads the three sides
# and passes dataclasses in. They live here for the loader and the scorer only.
# --------------------------------------------------------------------------
ROOT = Path(__file__).parent
DATA = ROOT / "data"
GENERATED = DATA / "generated"
TRUTH_DIR = GENERATED / "_truth"        # scorer only; the engine may not read this
BENCHREC = DATA / "benchrec"            # src/external only
MCP_CREATED = DATA / "mcp_created"
REPORTS = ROOT / "reports"

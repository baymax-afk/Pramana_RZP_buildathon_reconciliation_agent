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
TARGET_POOL_SIZE = 6           # NOMINAL payments placed per settlement window
SETTLEMENT_WINDOW_DAYS = 3     # width of the window whose payments settle together
MAX_SETTLEMENT_DRIFT_DAYS = 2  # T+1/T+2 drift the generator injects on top of that

# The engine's LOOKBACK is the sum, not SETTLEMENT_WINDOW_DAYS alone. A credit can be
# drifted two days past its window's settle date, so the oldest payment it legitimately
# covers sits WINDOW + DRIFT days behind it. Using the window width alone as the
# lookback silently drops every drifted credit: measured on the primary seed, 15 of 25
# unmatched one-to-one credits were simply outside a too-narrow window, at lags of 4
# and 5 days. See docs/DEFECT_LOG.md 2026-09-01-07.
LOOKBACK_DAYS = SETTLEMENT_WINDOW_DAYS + MAX_SETTLEMENT_DRIFT_DAYS
DENSITY_SWEEP = (3, 6, 12)     # nominal densities for the reported sweep

# NOMINAL density is not the pool the engine actually searches. A credit draws from
# LOOKBACK_DAYS of history, which spans more than one window, so the REALISED pool runs
# roughly 2.5x the nominal figure. Measured over 10 seeds against the correct lookback:
#
#     nominal   3  ->  realised  8-10     under MAX_POOL, engine searches
#     nominal   6  ->  realised 14-17     under MAX_POOL, engine searches  <- default
#     nominal  12  ->  realised 27-30     over MAX_POOL, engine refuses    <- swept
#
# These constants have now been recalibrated twice, both times because two quantities
# with different meanings were being read as one. First TARGET_POOL_SIZE was mistaken
# for the realised pool; then SETTLEMENT_WINDOW_DAYS was used as the engine's lookback
# when the lookback must also cover settlement drift. See docs/DEFECT_LOG.md
# 2026-09-01-06 and 2026-09-01-07.
#
# MAX_POOL cannot simply be raised to accommodate a higher density, because it is set
# by SEARCH COST, not by taste: meet-in-the-middle at k<=6 over a pool of 20 is 38,760
# subsets per credit, over 28 it is 376,740 -- ten times the work, multiplied by ~138
# credits and by K=8 permutation passes. So density is calibrated to fit the cap,
# rather than the cap being widened to fit the density.
#
# The high sweep arm deliberately exceeds the cap. That is the condition under study:
# the engine must refuse those credits rather than guess, and the refusal rate rising
# while precision holds flat is the project's central empirical claim.

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
TOL_REL_BPS = 0                # DISABLED -- see below

# The relative term is set to ZERO, deliberately, and this is the single most important
# tolerance decision in the project.
#
# It was originally 2 bps on the reasoning that a fixed rupee tolerance is
# proportionally tighter on a large settlement batch than on a small one. That
# reasoning is plausible and the consequence was not: tolerance GROWS with the credit,
# so on a Rs 102,926 settlement it reached 2,158p -- only 9.7x the smallest payment in
# the batch, not the 100x the uniqueness guarantee requires. At that width a subset of
# five unrelated payments landed within tolerance by coincidence and was assigned as
# unique, because it genuinely was the only subset that fit. Uniqueness testing cannot
# save a tolerance that is too loose: it faithfully reports one answer, and the answer
# is wrong.
#
# The absolute term alone is sufficient on the evidence. The measured fee-model residual
# is [-1, +2] paise per payment, so even a six-payment decomposition accumulates at most
# ~12p of modelling error against 100p of allowance. The relative term was covering a
# risk that does not exist while creating one that does.
#
# assert_tolerance_sanity now checks the MAXIMUM effective tolerance -- evaluated at the
# largest credit in the batch -- rather than the absolute constant, so this class of
# error cannot return unnoticed. See docs/DEFECT_LOG.md 2026-09-01-10.

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

# Fixpoint bound for the matching loop. The loop stops as soon as a full round adds no
# assignment, so this is a guard against non-termination, not a tuning parameter: a
# batch needing more rounds than this has a cycle the engine cannot resolve, and
# stopping is the right answer. Six is well above the deepest observed (2).
MAX_ROUNDS = 6

# --------------------------------------------------------------------------
# Live LLM tier bounds. These exist because the tier is on the DEMO path.
#
# The tier is offered one narration per unsettled credit per fixpoint round, and the
# whole matcher is replayed PERMUTATION_K times by the stability gate. Nothing in that
# path memoised, so a run made MAX_ROUNDS x PERMUTATION_K calls for each of the ~13
# narrations the regex tier cannot read -- several hundred sequential HTTP requests,
# with no timeout on any of them. Measured, not projected: a live `llm-compare` run was
# killed after minutes without producing a line of output.
#
# The cache is what actually fixes it (parse_narration is a pure function of the
# narration string, and the same ~13 strings recur every round and every pass). The
# timeout and the call cap are the belt: a cap cannot save a run that is blocked on a
# socket, and a timeout cannot save one that is merely making too many calls.
# --------------------------------------------------------------------------
LLM_TIMEOUT_S = 10.0           # per request; the SDK default is 600s
LLM_MAX_RETRIES = 1            # SDK default is 2; wall clock is timeout x (retries+1)
LLM_MAX_CALLS = 200            # hard stop per process; the tier disables itself after

# The headline reports this density alongside the primary one. A single density there
# invites reading the numbers as a property of the engine rather than of the engine at
# one crowding level, and density is the parameter the whole argument turns on. Set 0 to
# report one arm only.
HEADLINE_COMPARE_DENSITY = 12

# Extra days, beyond the engine's own lookback, that the ambiguity guard scans for
# interlopers.
#
# ZERO, and deliberately. The engine's candidate pool is exactly
# [txn_date - LOOKBACK_DAYS, txn_date], so scanning exactly that far is not "adequate",
# it is precisely right: a payment outside it cannot be a candidate no matter what.
#
# A positive margin was tried and is arithmetically infeasible. The protected band
# becomes LOOKBACK_DAYS + margin wide while a payment's own candidate window is only
# LOOKBACK_DAYS wide, so a payment whose credit sits near the ambiguity credit has its
# ENTIRE window swallowed and cannot be relocated anywhere legal. At margin 2 that made
# seed 11111 -- one of the sweep's own seeds -- unconstructible.
#
# The real fix for the drift this constant was introduced for is the COUPLING, not the
# margin: the guard reads LOOKBACK_DAYS instead of recomputing it, so widening
# MAX_SETTLEMENT_DRIFT_DAYS widens the guard automatically. It previously recomputed
# `SETTLEMENT_WINDOW_DAYS + 2` under a comment claiming to be "deliberately wider than
# the engine's rule", which was equal, and would have silently fallen BEHIND the engine
# the moment drift was widened. See DEFECT_LOG 2026-09-03-02.
AMBIGUITY_GUARD_MARGIN_DAYS = 0

# The K permutation passes are independent -- match_once is pure -- so running them
# concurrently changes only wall time, never an answer. Results are collected by pass
# index, so determinism does not depend on which worker finishes first. Set False to
# force the sequential path when debugging; the output is identical either way.
#
# HEADLINE_COMPARE_DENSITY was inserted between this comment and these constants, so the
# block documented the wrong thing and PERMUTATION_PARALLEL carried no rationale at all.
# A reader following "Set False" would have set the density constant to False, which
# argparse then accepts as compare-density 0. This file's header states that every
# constant here carries its documented reason. REVIEW_2026-09-02 R13.
PERMUTATION_PARALLEL = True
PERMUTATION_MAX_WORKERS = 4

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
    # An AUTHORISED-PAYER register: the merchant's own record of who is permitted to
    # settle on whose behalf -- a parent paying a subsidiary's invoice, a group treasury
    # paying for an operating company. Real AR systems keep one; it is reference data
    # like the invoice ledger, not an answer key, and it says nothing about which credit
    # matches which payment.
    #
    # 0.95 rather than 0.99 because registers go stale: a genuine match whose payer
    # differs from the invoice customer SHOULD be named in the register, but a
    # newly-onboarded group entity may not be yet.
    "authorised_payer": 0.95,
}

# u for the authorised-payer field: the chance a random (bank payer, ledger customer)
# pair is linked in the register. Unlike the name and reference u's -- which are
# estimated analytically from the batch at runtime -- this one cannot be, because the
# register is not part of the batch. It is therefore a DISCLOSED CONSTANT, and the
# derivation is written down rather than tuned:
#
#   the register names a handful of relationships across ~20 distinct customers, so a
#   random pair being linked runs at a few percent. 0.02 is that order of magnitude,
#   chosen before any measurement and not revisited.
#
# What it buys: log2(0.95 / 0.02) = +5.57 bits when the register fires. That is enough
# to outweigh an exact name DISAGREEMENT (-3.26 bits), and it should be -- explaining an
# expected name mismatch is precisely what such a register is FOR. It is not enough to
# rescue anything else: the field enters the same two-threshold band as every other, and
# amount conservation, uniqueness, the narration count and contested claims all still
# apply upstream of it.
FS_U_AUTHORISED_PAYER = 0.02

# --------------------------------------------------------------------------
# The authorised-payer register (side D), and why it is reference data rather
# than an answer key.
#
# When the generator injects `third_party_payer` it knows that payer P settled an
# invoice belonging to customer C. It writes SOME of those relationships to
# `data/generated/payer_directory.csv`, outside `_truth/`, as a merchant's own record of
# who may pay on whose behalf.
#
# **Why this is not ground truth.** Ground truth says which bank line maps to which
# payments. The register says only that a name appearing on the statement is a permitted
# payer for a name appearing in the ledger -- a join between two fields both sides
# already publish, which is exactly what a customer master file is. Customer C usually
# has several open invoices and several payments in the window; the register does not
# say which one this credit settles, and the engine still has to fit the amount, pass
# uniqueness, satisfy the narration count and win any contest. Real AR systems keep such
# a register; withholding it from the engine while calling the resulting refusals a
# limitation would be modelling the wrong world.
#
# **Coverage is deliberately partial.** A register covering every relationship would
# make the whole defect class a lookup, and an agent that closes 100% of cases because
# the answer was in a file is a demonstration of nothing. At 0.6 the investigator closes
# what the evidence supports and reports "insufficient evidence" on the rest, which is
# the behaviour actually worth showing.
#
# **Decoys are included** -- entries for relationships that appear nowhere in this
# batch -- so a register hit is not self-evidently a match, and a lookup that fires
# still has to survive every other layer.
PAYER_DIRECTORY_COVERAGE = 0.6
PAYER_DIRECTORY_DECOYS = 6

# --------------------------------------------------------------------------
# Ring 2, the investigator. Bounds, not tuning knobs.
#
# The step budget ends a confused investigation rather than letting it wander; the
# malformed-call allowance stops a model that cannot produce valid arguments from
# becoming a bill; and both sit inside the LLM tier's own call cap and timeout, which
# bound wall clock rather than turn count. Three independent bounds because each catches
# a failure the others do not.
# --------------------------------------------------------------------------
AGENT_MODEL = "claude-sonnet-5"
AGENT_STEP_BUDGET = 8          # model turns per exception
AGENT_MALFORMED_RETRIES = 1    # one retry on bad tool arguments, then abandon

# --------------------------------------------------------------------------
# The shifted holdout (Phase C).
#
# A second dataset the engine was NOT built against: unseen narration formats,
# adversarial free text, references duplicated across days, and settlement drift pushed
# past the engine's own lookback. Generated once at a seed disjoint from every reported
# run and from the density sweep, then FROZEN.
#
# **No constant in this file may be changed in response to a holdout result.** That rule
# is the reason the set is worth anything: a tolerance widened until the holdout scored
# better would be a tolerance fitted to the evaluation data, which is exactly what the
# rest of this file exists to forbid. `tests/test_holdout.py` pins the dataset's hash so
# it cannot be quietly regenerated after a disappointing number either.
#
# The one code change a holdout result is allowed to motivate is a CORRECTNESS fix -- a
# row the engine should have rejected and did not. Non-INR rejection at ingest came from
# exactly that and is not tuning.
HOLDOUT_SEED = 8080808
HOLDOUT_PPW = 12               # denser than the reported 6, inside MAX_POOL's reach
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
# `src/config.py`, so the repository root is one level up. config.py lives inside the
# package directory rather than at the root so that `pip install -e .` can expose it as
# a real module -- previously `run.py` and `api/main.py` each inserted paths into
# sys.path at import time, which meant the project only worked when invoked from its own
# checkout and never as an installed library.
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
GENERATED = DATA / "generated"
HOLDOUT = DATA / "holdout"       # the shifted held-out set; see HOLDOUT_SEED
TRUTH_DIR = GENERATED / "_truth"        # scorer only; the engine may not read this
BENCHREC = DATA / "benchrec"            # src/external only
MCP_CREATED = DATA / "mcp_created"
REPORTS = ROOT / "reports"

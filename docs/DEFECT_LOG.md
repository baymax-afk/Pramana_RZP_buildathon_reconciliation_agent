# Defect Log

A running record of what broke during this build, how it was diagnosed, what fixed
it, and what it cost.

**Entries are appended as things break, never reconstructed at the end.** Two
entries below are explicitly marked as reconstructed from notes, because they
predate the log's existence; every entry after them was written at the time.

Format for each entry:

| Field | Meaning |
|---|---|
| **Timestamp** | When the problem surfaced |
| **What broke** | The observed symptom, not the eventual explanation |
| **Diagnosis** | How the actual cause was found, including the wrong turns |
| **Fix** | What actually resolved it |
| **Cost** | Wall-clock time lost |

---

## 2026-08-31-01 — `docker build` fails: Razorpay's setup docs omit the build context

*Reconstructed from notes; predates this log.*

**Timestamp:** 2026-08-31, setup phase

**What broke:** Following Razorpay's documented Docker setup for the MCP server,
`docker build` exited immediately with a usage error rather than building.

**Diagnosis:** The documented command supplies `-t` and a tag but no build-context
argument. Docker requires a context path as a positional argument — conventionally
`.` for the current directory. The docs' command is incomplete as written, so the
failure is reproducible for anyone copying it verbatim and is not environment-specific.

**Fix:** Append the build context explicitly:

```bash
docker build -t razorpay-mcp-server .
```

**Cost:** ~10 minutes.

**Note for the submission:** this is an upstream documentation defect, not a defect
in this project. It is logged because it cost build time and because reporting it is
more useful than silently working around it.

---

## 2026-08-31-02 — MCP server reports "running" with a clean handshake but exposes no tools; Docker daemon intermittently unreachable

*Reconstructed from notes; predates this log.*

**Timestamp:** 2026-08-31, setup phase

**What broke:** Two problems that presented as one, which is why they took so long
to separate.

1. The local Razorpay MCP server reported itself as running and completed a clean
   `initialize` / `tools/list` handshake, yet none of its tools appeared in the
   client. Every observable signal on the server side said healthy.
2. Independently, the Docker daemon became intermittently unreachable at
   `npipe:////./pipe/dockerDesktopLinuxEngine` while Docker Desktop's UI continued
   to show as running.

**Diagnosis:** The compounding failure was the expensive part. A successful
handshake was taken as proof the transport was fine, which sent the investigation
toward client-side tool registration; meanwhile the intermittent daemon dropout
made results non-reproducible run to run, so a config change would appear to work
and then appear to fail with nothing altered. The two had to be separated before
either could be diagnosed: confirm daemon reachability first, *then* re-test tool
visibility, rather than treating any single run as evidence.

**Fix:** Restart the Docker daemon and re-establish the MCP server connection, then
verify tool visibility with an actual tool call rather than trusting the handshake.

**Cost:** ~90 minutes.

**Lesson carried forward:** a clean `initialize`/`tools/list` handshake is not
evidence that tools are usable. The only evidence is a successful tool call. This
now applies to the project generally — verification by observable outcome, not by
declared status, which is the same principle the reconciliation engine is built on.

**Verified resolved:** 2026-09-01. `fetch_all_payments` and `fetch_all_settlements`
both return live data from the test account.

---

## 2026-09-01-01 — Fee model concluded from too few observations, then falsified

**Timestamp:** 2026-09-01, Block 0 (frozen config), revised same day during Block 1

**What broke:** Twice, and the second break is the interesting one.

*First:* the initial fee model in `config.py` documented GST as
`gst = round(0.18 * base)` — an assumption written without checking it against data.

*Second, and worse:* checking it against the two captured payments then available
appeared to falsify `round` and establish `floor`. For the ₹780 payment,
`base = 1716` and `0.18 × 1716 = 308.88`; rounding gives 309, and Razorpay returned
`tax = 308`. The second record matched under both modes and so discriminated
nothing. **On the strength of a single discriminating observation, `GST_ROUNDING =
"floor"` was written into the config and into this log as an established fact.**

**Diagnosis:** During Block 1, eight more captured payments appeared in the test
account. Refitting against all ten immediately falsified `floor`:

| GST rounding mode | Mismatches out of 10 |
|---|---|
| floor | 5 |
| ceil | 7 |
| round | 6 |

**No single rounding mode fits.** The rule that had been recorded as determined from
evidence was an artefact of a sample of one.

**Fix:** Two things, one factual and one methodological.

*Factual.* What the ten records do establish, unambiguously, is the MDR base:

```
base = fee - tax = 0.022 * amount     # exactly 2.200%, all 10 records, no exceptions
```

The GST rounding rule is **not recoverable** from ten observations and the attempt
was abandoned rather than fitted harder. The model actually adopted is:

```
base = round(0.022 * amount);  tax = round(0.18 * base);  fee = base + tax
```

which predicts the true fee within **[−1, +2] paise** on every real record. Against
`TOL_ABS_PAISE = 100` that is a **50× margin**, so the residual ambiguity is absorbed
by tolerance and never has to be resolved. `FEE_MODEL_MAX_RESIDUAL_PAISE = 2` records
the measured bound.

*Methodological.* The original entry's own stated lesson — "a test case that passes
under both hypotheses is not a test" — was correct and insufficient. It licensed
concluding from the *one* case that did discriminate. The corrected rule: **a single
discriminating observation distinguishes hypotheses, it does not establish a law.**
Constants derived from observed behaviour are now recorded with the sample size they
were fitted on, and refitted whenever the sample grows.

**Cost:** ~25 minutes across both rounds, all of it in checking.

**Why this mattered more than two paise:** the fee model feeds the conservation
residual directly. A systematic error would have surfaced as MR4 violations with no
obvious cause, in a metric whose entire purpose is to be trusted as a defect signal —
the engine would have correctly reported that money did not balance, because of the
checker rather than the matcher.

**The uncomfortable part, kept deliberately:** this project's thesis is that systems
should quantify their own uncertainty rather than assert precision they have not
earned. The first version of this entry asserted a precise rounding rule on one data
point. The failure mode being built against showed up in the build itself, in the
log describing it. It is left here rather than tidied away.

---

## 2026-09-01-02 — S2S payment initiation unavailable; browser automation needed for captured payments

**Timestamp:** 2026-09-01, Block 1

**What broke:** `initiate_payment` (the MCP's server-to-server JSON v1 flow) was the
intended path for scripting real captured payments without a browser. It failed with
`initiating payment failed: The requested URL was not found on the server.`

**Diagnosis:** A 404 on the endpoint rather than an auth or validation error, meaning
the S2S JSON v1 route is not enabled for this test account. S2S requires explicit
enablement from Razorpay and is not available by default — not a fixable
configuration problem within the build window.

**Fix:** Complete payments through the hosted checkout in the browser instead. Two
obstacles there, both solved:

1. *Checkout is a cross-origin iframe*, so `read_page` and `find` return nothing and
   element refs are unavailable. Interaction has to be coordinate-based.
2. *`type` silently did nothing* on the contact field after a plain `left_click`, and
   individual `key` presses landed only one character each. **`triple_click`
   followed by `type` works** — the triple-click establishes focus in a way the
   single click does not.

The completion path that works, ~5 interactions per payment:
`create_payment_link` → navigate to `short_url` → `triple_click` contact + type
number → click a recommended **Netbanking** option → click **Success** on Razorpay's
demo bank page.

**Netbanking was chosen deliberately over cards and UPI**: its test-mode flow
requires no credentials of any kind — no card number, no VPA, just a bank selection
and a Success/Failure button on a simulated bank page.

**Cost:** ~35 minutes, most of it on the iframe interaction.

**Carried forward:** the earlier lesson from 2026-08-31-02 applied again. A tool
being listed and callable is not evidence it is usable; `initiate_payment` was
present, well-documented, and accepted its parameters before failing at the network
layer.

---

## 2026-09-01-03 — Checkout layout collapse race caused clicks to land on the wrong payment method

**Timestamp:** 2026-09-01, Block 1

**What broke:** After entering the contact number, clicking the "Netbanking" row at
its observed screen position repeatedly selected the row *above or below* it. Twice it
opened the **Cards** panel and asked for a card number; twice it opened **Wallet**,
which then demanded an OTP.

**Diagnosis:** Razorpay's checkout collapses its header a fraction of a second after
the contact step completes, shifting every payment-method row up by ~28px. The
collapse is not deterministic — it sometimes lands before the click and sometimes
after, so the same coordinate selects different rows on different runs. Screenshots
taken before the click were showing a layout that no longer existed by the time the
click was dispatched. The cross-origin iframe makes this worse: `read_page` and `find`
return nothing, so there are no element refs and interaction has to be positional.

**Fix:** Never click a payment-method row from a screenshot taken before the previous
interaction settled. Take a fresh screenshot immediately before each method click and
read the row position from that. In practice the settled (collapsed) layout is
Cards 140 / Netbanking 168 / Wallet 195 at a 1400x900 viewport.

**Cost:** ~20 minutes, and two abandoned payment attempts.

**Two things deliberately not done:**

- **No card numbers were entered.** When the Cards panel opened by accident, the
  action was to navigate back, not to fill it in.
- **No OTP was guessed.** When the wallet path demanded an OTP, that attempt was
  abandoned rather than attempting a test-mode OTP value. Netbanking's demo bank page
  requires no credentials of any kind, so there was never a reason to authenticate
  anything.

The abandoned wallet attempt is itself useful data: it produced a genuine
`payment_cancelled` failed payment (`pay_TWeoZtllDCQlDt`) against the same order as
its successful retry, which is exactly the retry-after-failure pattern real
reconciliation data contains.

---

## 2026-09-01-04 — Ambiguity guarantee held by luck, not by construction

**Timestamp:** 2026-09-01, Block 2 (generator)

**What broke:** `test_filler_payments_cannot_participate` failed on the first full run
of the suite. A payment netting 27,371p sat inside the ambiguity credit's candidate
pool, small enough to join a subset reaching the 80,000p credit.

**Diagnosis:** The structural guarantee was written as "every *other payment placed in
the ambiguity window* nets more than the credit", and that is what the generator
enforced. But settlement windows overlap at their boundaries. The ambiguity credit is
dated on its window's settle date, and payments belonging to the **following** window
can be dated on exactly that day — lag 0, squarely inside the credit's lookback. The
guarantee covered the wrong set.

The uncomfortable part: `assert_ambiguity_is_exact` still passed. Exactly two subsets
still fit, because no second payment happened to net the 52,629p that would have
completed a third. **The case was correct by luck.** Had the seed differed, the
centrepiece demo case could have silently acquired a third candidate — or, worse, the
generator could have shipped a case that resolved cleanly and made the demo a lie.

**Fix:** `_protect_ambiguity_window`, a post-generation pass that enforces the
invariant globally rather than per-window. Any captured payment inside the credit's
lookback that is small enough to participate, and is not one of the crafted four, is
shifted one lookback-width later — out of the pool. Its own settlement credit is dated
strictly later, so the shift cannot orphan it.

Shifting the date rather than raising the amount was deliberate: raising the amount
would cascade through the payment's invoice, its net, and the bank credit derived from
that net. Moving the date touches one field with nothing downstream of it.

The pass ends with a post-condition that re-checks every payment and **raises** if any
interloper remains, so a case the shift cannot fix fails generation rather than
shipping.

**Cost:** ~25 minutes.

**Why the test was stricter than the assertion, and why that was right:**
`assert_ambiguity_is_exact` asks "are there exactly two candidates?" —
an *outcome*. The test asks "could there ever be a third?" — a *property*. The outcome
check passed while the property was violated, which is precisely the gap between a
system that happens to be right and one that cannot be wrong. Writing the stricter
test first is what surfaced this at all.

---

## 2026-09-01-05 — Density-bound assertion would have made the density sweep impossible

**Timestamp:** 2026-09-01, Block 2 (generator)

**What broke:** `assert_pool_bound` failed generation outright whenever a settlement
window exceeded `MAX_POOL = 20`. At the top sweep density (`payments_per_window = 24`)
the worst pool is 36, so the generator refused to build the batch at all.

**Diagnosis:** Two different meanings of "pool too large" had been collapsed into one
rule. At the **default** density, an oversized pool means the date range was derived
wrongly and the density invariant has broken — a generator bug that must fail loudly.
Above the default density, an oversized pool is not a bug at all: it is the
phenomenon the sweep exists to study. Crowding the windows is the *point*, and the
engine is supposed to respond by refusing (`decomposition_out_of_bounds`) rather than
guessing.

Left as written, the assertion would have deleted the project's central empirical
result — refusal rate climbing with density while precision holds flat — by making the
high-density arm of the sweep unbuildable.

**Fix:** the assertion now hard-fails only when `payments_per_window <=
TARGET_POOL_SIZE`, and otherwise reports the worst pool for the metrics block. Crowded
windows at high density are data; crowded windows at default density are a defect.

**Cost:** ~15 minutes, all of it before any engine existed to be blocked by it.

**Lesson:** an invariant worth asserting still needs its scope stated. "Pools must not
exceed MAX_POOL" was true of the configuration being shipped and false of the
experiment being run, and the assertion could not tell the two apart.

---

## 2026-09-01-06 — Density and search-bound constants were never reconciled; default config failed on 2 of 12 seeds

**Timestamp:** 2026-09-01, Block 2 (generator)

**What broke:** At the default density the worst settlement-window pool came out at
20 — exactly `MAX_POOL`. Sweeping twelve seeds showed the real picture: pools ranged
18 to 22, and **2 of 12 seeds exceeded the cap and failed generation outright.** The
two seeds this project actually reports on, 20260905 and 77771, happened to land at
20 and 19. They passed by luck.

**Diagnosis:** `TARGET_POOL_SIZE` is named as though it were the pool the engine
searches. It is not. It controls how many payments the generator *places* per window,
while the engine's candidate pool is everything inside a credit's lookback `[D-3, D]`
— which straddles window boundaries, and which settlement drift (T+1/T+2) widens
further. Measured across seeds, the realised pool runs about **1.8x** the nominal
figure:

| nominal | realised | vs MAX_POOL = 20 |
|---|---|---|
| 5 | 9–10 | under — engine searches |
| 9 | 15–16 | under — engine searches |
| 12 | 18–22 | **straddles the cap** |
| 18 | 27–29 | over — engine refuses |
| 24 | 38–40 | over — engine refuses |

The two constants had been set independently, from different considerations, and never
checked against each other. `MAX_POOL = 20` came from search cost; `TARGET_POOL_SIZE
= 12` came from wanting a realistic-looking window. Nothing had ever measured what the
second implied for the first.

**Fix:** recalibrate density to fit the cap, `TARGET_POOL_SIZE = 12 -> 9` and
`DENSITY_SWEEP = (6, 12, 24) -> (5, 9, 18)`. Verified across 12 seeds: the default arm
now realises 15–16 against a cap of 20, with genuine headroom, and the high arm
realises 27–29, comfortably over — which is what the sweep needs it to be.

**Why the cap was not simply raised instead:** `MAX_POOL` is set by search cost, not by
preference. Meet-in-the-middle at `k <= 6` over a pool of 20 is 38,760 subsets per
credit; over 28 it is 376,740 — ten times the work, multiplied by ~138 credits and by
`K = 8` permutation passes. Raising the cap to fit the density would have made the
runtime permutation gate unaffordable, and that gate is the one thing the plan says
must never be cut.

**Cost:** ~20 minutes.

**On changing a "frozen" constant:** `config.py` says its values are set once and never
tuned. That discipline is about not moving thresholds in response to disappointing
*metrics*, and no metric exists yet — nothing has been matched, scored or reported.
This was a miscalibration between two constants, found by measurement rather than by
preference, and fixed before any number was produced. The distinction matters, so it
is recorded here rather than left to look like quiet tuning.

**The recurring shape of the last three entries:** each was a case where something was
correct on the seeds being looked at and wrong in general — the GST rounding rule, the
ambiguity guarantee, and now the density calibration. In every case the fix was to
measure the distribution instead of the instance.

---

## 2026-09-01-07 — Engine lookback used the window width, silently dropping every drifted credit

**Timestamp:** 2026-09-01, Block 3 (matching engine, tiers 1–2)

**What broke:** Tiers 1–2 matched 85 of 138 credits at perfect precision, but 25
credits that ground truth says are plain `one_to_one` found no candidate at all. A
clean one-to-one settlement is the easiest case there is; missing 25 of them meant
something structural was wrong, not something marginal.

**Diagnosis:** Instrumented the misses by cause rather than guessing. Fifteen of the
25 had their payment sitting **outside the engine's date lookback** — at lags of 4
days (8 credits) and 5 days (7 credits), against a lookback of 3.

`SETTLEMENT_WINDOW_DAYS = 3` was doing two different jobs. For the generator it is the
width of the window whose payments settle together. For the engine it was also being
used as the lookback — how far back a credit may reach for its payments. Those are not
the same number. A credit can be drifted up to `MAX_SETTLEMENT_DRIFT_DAYS = 2` past
its window's settle date, so the oldest payment it legitimately covers sits
`WINDOW + DRIFT = 5` days behind it, not 3.

The engine was therefore structurally incapable of matching any credit carrying T+2
drift — and settlement drift is one of the nine defects the batch deliberately injects.

**Fix:** `LOOKBACK_DAYS = SETTLEMENT_WINDOW_DAYS + MAX_SETTLEMENT_DRIFT_DAYS`, used by
tier 2 and by the pool-bound assertion so both measure the same thing.

Widening the lookback widens the candidate pool, so the density calibration had to be
redone against it: the realised pool is now roughly **2.5x** nominal rather than 1.8x,
and `TARGET_POOL_SIZE` moves 9 -> 6 with `DENSITY_SWEEP` (5, 9, 18) -> (3, 6, 12).
Verified across 10 seeds — default realises 14–17 against a cap of 20, the high arm
realises 27–30 and is meant to exceed it.

**Result:** coverage 61.6% -> 67.9%, precision unchanged at 1.000 (93/93), and
unmatched `one_to_one` credits fall from 25 to 6.

**Cost:** ~20 minutes.

**The recurring pattern, now three times:** GST rounding, the density calibration, and
now the lookback. Every one was two distinct quantities being read as a single
constant, and every one was invisible until something was measured per-cause rather
than in aggregate. The aggregate here said "coverage 61.6%", which looks like a
matcher that needs more tiers. The per-cause breakdown said "15 credits are outside a
window that is the wrong width", which is a one-line fix. **Coverage numbers hide
their own causes; the fix was cheap only because the misses were bucketed by reason
before anything was changed.**

**What was NOT done:** the obvious alternative was widening the window until the
misses went away. That would have "worked" and been indefensible — it is per-record
tuning wearing a config-shaped hat. The lookback is now derived from two quantities
that each mean something, and it would be wrong at any other value.

---

## 2026-09-01-08 — The order-dependence detector could not detect order dependence

**Timestamp:** 2026-09-01, Block 5 (metamorphic harness, permutation gate)

**What broke:** The runtime permutation gate reported stability 1.0 across all 105
assignments, zero unstable, on every seed. That is the *expected* result for this
matcher — but "the detector found nothing" and "the detector does not work" produce
byte-identical output, so the number was worth exactly nothing until the gate had been
shown to fire.

Writing the negative control found the problem immediately. A deliberately
order-dependent matcher — one that picks the first candidate instead of refusing when
several fit — was still reported as **perfectly stable**.

**Diagnosis:** The mutant took `candidates[0]` from tier 2's returned list. But tier 2
*sorts* its candidates by `(abs(residual), payment_ids)` before returning them, so
`candidates[0]` is deterministic regardless of how the input was shuffled. The mutant
was not actually order-dependent; it inherited the real code's sort. The test proved
nothing and would have passed forever.

**Fix:** the mutant now walks `candidate_pool` directly and takes the first payment
that fits in genuine iteration order, which is what the naive bug actually looks like.
With that, the ensemble sees two different assignments across shuffled passes, the gate
strips the assignment, and emits `order_dependent_assignment` naming both variants and
the rupees at risk.

**Cost:** ~20 minutes.

**Why this entry matters more than its size suggests.** The project's central claim is
that verification must work where no ground truth exists. A verification layer that
cannot fail is not verification — it is decoration that produces a reassuring number.
The gate now reports "unstable 0/105" *and* the metrics block says, in the same
paragraph, that the figure was validated against a deliberately broken matcher. A zero
means something only when it was possible for it to be non-zero.

There is a second, quieter lesson. The reason the real engine is stable is that
`match_once` sorts credits into a total order and both tiers refuse rather than choose
on ties. Those are good properties. But they are also exactly what made the naive test
pass — **the same design decision that makes the system correct also masks whether the
check for that correctness works.** Testing the check against something known-broken is
the only way to tell those apart.

**Still true and worth stating plainly:** on this engine the gate has never fired on
real data, and it is not expected to until tier 3 begins enumerating subsets in Block
6, where which subset a credit takes can genuinely depend on enumeration order. It is a
live safety net, not a demonstrated catch. The metrics block says so rather than
implying the zero was hard-won.

---

## 2026-09-01-09 — A test silently overwrote the project's real dataset

**Timestamp:** 2026-09-01, Block 6

**What broke:** Immediately after wiring tier 3 in, the engine reported 25 assignments
against 137 credits. It looked like a catastrophic regression in the new code.

**Diagnosis:** The new code was fine. `test_generator_may_still_write_truth` called
`build.write(b)` **without an output directory**, so it wrote its 40-payment,
density-8 batch to the default destination -- the project's real `data/generated/`.
Every subsequent run matched against a 40-record batch while reporting as though it
were the 200-record one.

The failure is nasty because nothing errors. The data is valid, the engine runs
correctly, the tests pass, and only the *numbers* are wrong -- attributed to whatever
was changed most recently.

**Fix:** the test writes to a pytest `tmp_path`. The real batch was regenerated.

**Cost:** ~10 minutes, and it would have been far more had it landed a block later,
where the "regression" would have been blamed on subset-sum.

**Lesson:** a test that writes anywhere outside its own temporary directory is a test
that can corrupt the thing it is testing. Default arguments make that easy to do by
omission rather than by intent.

---

## 2026-09-01-10 — The 100x tolerance margin was false for large credits, and it produced two confident wrong answers

**Timestamp:** 2026-09-01, Block 6 (subset-sum, Layer 2)

**What broke:** With tier 3 live, precision fell from 1.0000 to 0.9832 -- **two wrong
assignments out of 119**, both from subset-sum, both reported with full confidence and
a unique solution.

**Diagnosis:** Tolerance was `TOL_ABS_PAISE + credit * TOL_REL_BPS / 10000`, with
`TOL_REL_BPS = 2`. The relative term exists so that a fixed rupee tolerance is not
proportionally tighter on a large settlement batch than a small one -- plausible
reasoning, and wrong in consequence, because **tolerance grows with the credit**:

| credit | effective tolerance | margin vs smallest payment (20,941p) |
|---|---|---|
| Rs 1,000 | 120p | 175x |
| Rs 10,000 | 300p | 70x |
| Rs 102,926 | 2,158p | **9.7x** |

The project's stated guarantee is a 100x margin between tolerance and the smallest
payment, because below that a subset S and the subset S-plus-one-small-payment both
satisfy the constraint. On the largest settlement in the batch the real margin was
**9.7x**. At that width a subset of five unrelated payments landed within tolerance by
pure coincidence.

**And Layer 2 could not save it.** The coincidental subset genuinely *was* the only one
that fit, so the uniqueness test faithfully reported one answer -- and the answer was
wrong. Uniqueness testing verifies that the constraint identifies a single solution; it
cannot detect that the constraint was too loose to be worth solving. That is a real
limit of the technique and it is worth stating plainly rather than implying Layer 2
catches everything.

`assert_tolerance_sanity` did not catch this either, because it checked the CONSTANT
(100p, a 209x margin) rather than the tolerance actually applied at the largest credit.

**Fix:** two changes.

- `TOL_REL_BPS = 0`. The absolute term alone is sufficient on the evidence: the measured
  fee-model residual is [-1, +2] paise per payment, so even a six-payment decomposition
  accumulates ~12p of modelling error against 100p of allowance. The relative term was
  covering a risk that does not exist while creating one that does.
- `assert_tolerance_sanity` now evaluates the tolerance at the **largest credit in the
  batch**, so a guarantee that holds only for small transactions fails the build.

Precision returned to 1.0000 across all 125 assignments, including all 20 subset-sum
decompositions.

**Cost:** ~30 minutes.

**Why this is the most important entry in this log.** Every other defect here produced
a visible symptom -- a crash, a miss, a failing test. This one produced *confident
correct-looking output that was wrong*, in the exact component whose purpose is to
prevent that. The engine assigned five payments to a settlement, reported a unique
decomposition, and was believed. Only ground-truth scoring caught it, and ground truth
is precisely what a real deployment does not have.

The honest conclusion: **a verification layer is bounded by the quality of the
constraint it verifies.** Layer 2 asks "does the constraint identify one answer?" and
answers correctly. It cannot ask "is this constraint tight enough to mean anything?" --
that has to be established separately, before the run, and asserted at the worst case
rather than the typical one.

---

## 2026-09-01-11 — MR6 caught the engine leaving work undone

**Timestamp:** 2026-09-01, Block 6

**What broke:** With tier 3 live, MR6 (idempotence) failed on the reported batch:
rerunning the engine on its own unassigned residue produced fresh assignments on
several credits the first pass had declined.

**Diagnosis:** Claiming is greedy and credits are processed in a fixed order. A credit
examined early can see two viable decompositions and correctly refuse; a later credit
then claims one of the payments involved, leaving the first credit with exactly one
viable decomposition. Its refusal was right on the information available at the time
and stale immediately afterwards -- but nothing revisited it.

The consequence is that the engine's output depended on **how many times it happened to
be run**, which is not a property any reconciliation system may have.

**Fix:** `match_once` now iterates to a fixpoint, repeating rounds until one completes
with no new assignment (bounded at `MAX_ROUNDS = 6`). Idempotence then holds by
construction rather than by luck.

Resolving genuine ambiguity with information that arrives later is correct behaviour,
not a shortcut. What would not be acceptable is resolving it by *picking*, and within
any single round the tiers still refuse rather than choose.

**Effect on results:** match rate 54.12% -> 86.08%, precision unchanged at 1.0000.

**Cost:** ~20 minutes.

**Worth noting:** this is the first time a metamorphic relation caught a real defect in
the engine rather than confirming correctness. MR6 needs no ground truth to detect it --
it compares two executions and observes that they disagree, which is exactly the
property that makes these checks usable where no answer key exists.

---

## 2026-09-01-12 — Layer 3 refused 86 of 137 credits by treating silence as dissent

**Timestamp:** 2026-09-01, Block 7 (Fellegi-Sunter)

**What broke:** The moment the Fellegi-Sunter gate was wired in, assignments collapsed
from 125 to 61 and refusals rose to 76. Every many-to-one decomposition Layer 2 had
earned in the previous block was gone.

**Diagnosis:** Two errors compounding.

*Scoring the blocking variable.* `date` was in the comparison vector. But the candidate
pool is BUILT by requiring the payment to fall inside the credit's lookback, so every
pair reaching the model already agrees on date by construction. Scoring it again
double-counts the blocking. Worse, it meant no comparison was ever fully absent — so a
settlement batch with no payer name and no quoted reference produced a weight of -0.20
from the date field and the prior alone, landed below the lower threshold, and was
refused as a non-match. Excluding blocking variables from the comparison vector is
standard record-linkage practice; here it is also what makes "nothing to weigh"
detectable at all.

*Treating absence as evidence against.* A gateway settlement batch carries no payer name
because it covers many payers, and quotes no invoice because it covers many invoices.
That is the correct content of those fields, not disagreement. The gate now fires only
on active contradiction — at least one field must positively DISAGREE and the field
evidence must net negative. Absence never vetoes; weak-but-positive evidence never
vetoes.

**Fix:** date removed from the comparison vector; the veto moved from `band ==
"non_match"` to a new `contradicts` predicate.

**Cost:** ~25 minutes.

---

## 2026-09-01-13 — Whole-string name matching vetoed six correct assignments

**Timestamp:** 2026-09-01, Block 7

**What broke:** With the gate corrected, precision held at 1.0000 but **six correct
assignments were refused** as `amount_name_conflict`, and one-to-one recall fell from
105/105 to 99/105. Rs 118,048.74 of correctly-matched money was pushed onto a human's
desk for no reason.

**Diagnosis:** All six were the same failure. The name comparison tested whole-string
prefix containment, and real names do not survive a bank statement intact:

| bank narration | ledger | verdict |
|---|---|---|
| `NOVA CHEMICALS IND` | `Nova Chemical India Private Limited` | DISAGREE |
| `PINNACLE STEEL TRA` | `Pinnacle Steels Traders` | DISAGREE |
| `VERTEX ENGINEERIN` | `VERTEX ENGG` | DISAGREE |

Every one is the same counterparty. Bank field-width truncation and ledger alias
spellings mean neither normalised string is a prefix of the other, so a string-level
test calls them a conflict — and the gate, working exactly as designed, refused.

**Fix:** token-level comparison. Two tokens agree when the shorter is a strict prefix of
the longer, which covers truncation (`TRADERS` -> `TRA`) and inflection (`STEEL` /
`STEELS`, `CHEMICAL` / `CHEMICALS`).

**What was deliberately NOT done:** the looser rule of accepting any shared
3-character prefix would have fixed the abbreviation case (`ENGG` / `ENGINEERING`) too.
It would also merge `SUNRISE` with `SUNLINE` and `ACME` with `ACMI` — and the customer
registry deliberately contains confusable pairs that are DIFFERENT legal entities.
A name comparison loose enough to merge those posts money to the wrong customer, which
is the exact failure this layer exists to price. Abbreviations therefore degrade to
partial agreement — weak positive evidence — rather than a false identity.

**A second, smaller overclaim, caught by the tests rather than the metrics:** the first
tokenised version returned EXACT when every token found a prefix-agreeing partner and
the counts matched. So `PINNACLE STEEL TRA` vs `PINNACLE STEELS TRADERS` scored EXACT
and drew the full `m/u` weight. That is unearned: complete prefix agreement is
consistent with the same counterparty and also with a different one sharing a truncated
prefix. EXACT now requires literal token equality; everything else is PARTIAL.

**Result:** match rate back to 86.08%, one-to-one recall back to 105/105, precision
1.0000 throughout.

**Cost:** ~30 minutes.

**The pattern across both entries:** Layer 3 is the first component whose failures cost
*coverage* rather than correctness, and both times the layer behaved exactly as
specified while the specification was wrong about the data. A gate is only as good as
its notion of disagreement, and "these strings differ" is not the same proposition as
"these are different counterparties."

---

## 2026-09-01-14 — The composite confidence score has almost no spread, so it cannot yet be calibrated

**Timestamp:** 2026-09-01, Block 8 (Layer 4 and composite confidence)

**What broke:** Nothing failed. The confidence score works, every assignment carries
one, and the reliability table came out like this:

| bucket | n | observed accuracy |
|---|---|---|
| 0.95 | 125 | 100.0% |

**One bucket.** All 125 assignments score between 0.938 and 0.982 — a spread of 0.044.

**Diagnosis:** This is a structural consequence of the architecture working, not a bug
in the score. The engine only *reaches* the confidence stage for assignments where
conservation already holds tightly, uniqueness is already clean, and the permutation
gate is already open. Everything with weak evidence was refused several layers
earlier. So by the time the composite score is computed, its inputs have been filtered
to their high end and there is almost nothing left to discriminate between.

The uncomfortable implication is worth stating plainly: **a calibration curve needs
variation in the predicted probability, and this engine does not currently produce
any.** Fitting weights against BenchRec in Block 8b will improve the weights, but it
cannot manufacture spread that the pipeline has already filtered out. A reliability
diagram over a single decile is not evidence of calibration; it is one point.

**What was NOT done:** the tempting fix is to loosen the earlier layers so weaker
matches survive to be scored, producing a nice spread across deciles. That would be
manufacturing a calibration curve by degrading the engine — trading real precision for
a better-looking chart. The refusals are doing their job; the score is simply
downstream of them.

**Recorded, not fixed.** The metrics block prints the reliability table, states that
the weights are UNCALIBRATED, and says in terms that the scores are an ordering rather
than probabilities — that 0.9 does not yet mean "right 90% of the time". A test pins
the narrow spread so it cannot be quietly forgotten if the caveat is ever dropped.

The genuinely useful direction, if there is schedule for it, is to score the REFUSED
population as well: those assignments span the full range of evidence quality, and a
confidence score over accept-and-refuse together would have the variation that
accept-only lacks. That is a design change rather than a fix, and it is out of scope
for this block.

---

## 2026-09-01-15 — Materiality at Rs 5,000 puts 99% of assigned value into full verification

**Timestamp:** 2026-09-01, Block 8

**What broke:** Layer 4 stratifies correctly and then reports that **104 of 125
assignments — 99% of assigned value — sit at or above materiality** and require 100%
verification. Sampling relieves 21 items worth Rs 49,831 out of Rs 3,494,613.

**Diagnosis:** `MATERIALITY_PAISE = 500_000` (Rs 5,000) was set in Block 0 as a
plausible tolerable-misstatement figure, before any batch existed to set it against.
The generated batch's payments run to Rs 18,700 each and settlements to Rs 100,000+, so
most individual items exceed it. The stratification is arithmetically right and
practically inert.

**Why this is NOT being "fixed":** materiality is a POLICY input, not a tuning
parameter. It encodes how much misstatement a particular merchant would care about, and
Rs 5,000 on a Rs 3.5M batch is a conservative but entirely defensible stance. Raising it
to make the sampling look more impressive would be choosing the answer and then
choosing the question — precisely the move this project criticises elsewhere.

What the system owes the user is the *consequence*, stated plainly, and the metrics
block now prints it: at this materiality, sampling relieves almost nothing, so Layer 4
is providing an audit-standard verification plan rather than an audit-standard
reduction in work. Both are legitimate outputs; conflating them would not be.

**A related honest number:** the rule-of-three bound on the sampled stratum is
Rs 29,899 against a stratum worth Rs 49,831 — a 60% upper bound from 5 sampled items.
That is what five observations actually buy, and reporting the point estimate of
Rs 0.00 without it would be the single most misleading figure this system could emit.

---

## 2026-09-02-01 — The confidence score cannot be calibrated, because the engine almost never errs

**Timestamp:** 2026-09-02, Block 8b (BenchRec fit and calibration)

**What broke:** Nothing broke. The calibration simply cannot be demonstrated, and the
reason is worth more than the number would have been.

**What was attempted:** fit the composite confidence weights on held-out data and
report a reliability diagram with ECE, as the plan requires. Three escalating attempts:

1. **Accepted assignments only** — 125 examples, all in one decile at ~0.96.
2. **Accepted plus refused candidates**, held-out seeds — 777 examples, base rate
   0.991, **1 of 10 bins occupied**.
3. **Accepted plus refused, across five densities and six seeds** — 3,705 examples,
   base rate 0.992, **still 1 of 10 bins occupied**, ECE 0.0002.

**Diagnosis:** The layered refusal architecture removes essentially every error before
the confidence stage exists. Tier 1 refuses on reference collision, tier 2 refuses on
ties, Layer 2 refuses on non-unique subsets, the permutation gate refuses on
order-dependence, and Layer 3 refuses on contradicted evidence. What survives all of
that is correct ~99.2% of the time regardless of its feature values.

A reliability diagram needs a spread of *outcomes* to calibrate against. This engine
produces one outcome. The ECE of 0.0002 is not a good calibration result — it is the
arithmetic of a single bucket, and quoting it as evidence the score is well calibrated
would be precisely the overclaim this project was built to argue against.

**The uncomfortable implication, stated plainly:** on this data the composite confidence
score is **decorative**. It carries no information beyond "the engine accepted this",
because acceptance already implies correctness at 99%+. The four verification layers do
real work — the density sweep shows refusal rate rising 2.4x while precision holds —
but the *scalar summary* of them adds nothing measurable on top of the accept/refuse
decision itself.

**What was NOT done:** the obvious way to manufacture a calibration curve is to loosen a
threshold until the engine starts being wrong, then show the score tracking those
errors. That would produce a beautiful reliability diagram and would be dishonest: it
calibrates a deliberately degraded system and reports the result as though it described
the real one. The density dial was used instead, because crowding the pool makes the
problem genuinely harder rather than making the engine artificially worse — and even at
a realised pool of 53, precision only fell to 0.9978.

**What would settle it:** BenchRec. It is external, labelled, ~69k rows of real Tier-1
bank data, and it contains the hard cases this generator does not produce. Kaggle
requires authentication for dataset downloads, so it could not be fetched in this
environment; `src/external/benchrec_ingest.py` reads it if a human places the files, and
reports its absence rather than silently substituting the fallback. Until then the
calibration claim is **not made**, rather than made weakly.

**Cost:** ~50 minutes, inside the 60-minute time-box the plan set for this block.

**The pattern, for the fourth time:** the number that would have looked best (ECE
0.0002) was the least meaningful thing measured. Reporting it without the bin count
would have passed unnoticed.

---

## 2026-09-02-02 — The regex tier returned whole narrations as payer names, manufacturing false disagreements

**Timestamp:** 2026-09-02, Block 9 (LLM tier)

**What broke:** Spotted while checking that the LLM tier was firing at all. For the
narration `ACME INDUSTRIAL SU FUND TRF 398693 INV20261003`, the deterministic parser
returned the payer name as **the entire string**, digits and rail tokens included.

**Diagnosis:** `_extract_name` split the narration on `[-/]` and filtered the resulting
CHUNKS. That works for delimited rails, but several real formats use no delimiters at
all, so the whole narration arrives as a single chunk, passes the chunk-level filters,
and is returned as a name.

The consequence was not cosmetic. That string is then normalised and handed to
Fellegi-Sunter as a counterparty. It matches no customer, so Layer 3 scored an active
DISAGREEMENT -- and a disagreement is real evidence against a match, unlike absence,
which contributes zero. **A garbage name is strictly worse than no name**, because the
architecture treats silence as neutral and nonsense as dissent.

**Fix:** filter word by word rather than chunk by chunk, and extend the noise list with
the rail tokens that had been surviving into names (`FUND`, `CLG`, `CMS`, `INW`, `REM`,
`MB`, `ACCT`, `AC`, `FRM`). Every entry on that list appeared inside a real extracted
name before it was added.

**Result:** assignments 115 -> 117 and refusals 13 -> 11 on the primary seed. Two credits
had been refused for a contradiction that existed only because the parser invented a
counterparty.

**Cost:** ~15 minutes.

**Why it took a different task to find it:** the parser had passed every test written
for it, because those tests used delimited narrations -- the formats it was designed
for. The bug only existed in the messy formats added in this block to give the LLM tier
something to do. **The defect was invisible until the data got harder**, which is the
same lesson the density sweep teaches about the matcher, arriving here about the parser.

---

## 2026-09-02-03 — The LLM tier changes no verdicts, and that is the reported result

**Timestamp:** 2026-09-02, Block 9

**What was measured:** precision with the LLM tier on and off, as `docs/METRICS.md`
requires.

| tier | assigned | correct | precision | match rate | tier-1 matches |
|---|---|---|---|---|---|
| disabled | 117 | 117 | 1.0000 | 82.99% | 36 |
| recorded | 117 | 117 | 1.0000 | 82.99% | **45** |

**The finding:** the LLM recovers a usable merchant reference from 9 of the 12
narrations the regex tier cannot parse, and those 9 credits move from tier 2
(amount + date) to tier 1 (exact reference) -- a genuinely stronger evidence class. But
**not one verdict changes.** Those credits were already being matched correctly on
amount and date alone.

So on this data the LLM tier improves the *evidence behind* matches without improving
the *outcomes*. Precision is identical to four decimal places. It is not load-bearing
for correctness here, and the metrics block says so rather than presenting the tier as
a contributor.

Where it would matter is where amount-and-date is genuinely ambiguous and the reference
is the tie-breaker -- exactly the crowded-pool regime the density sweep explores. That
is a prediction this build does not have the data to test, and it is stated as a
prediction rather than a result.

**On what actually ran:** no `ANTHROPIC_API_KEY` was available, so `RecordedTier` --
hand-authored parse rules standing in for model output -- produced these numbers, and
the metrics block prints `llm=recorded` so it cannot be read as a live-model result.
`ClaudeTier` is written and selected automatically when a key is present; it was not
exercised. The honest summary is that the *architecture* is demonstrated and the
*model's* contribution is not.

---

## 2026-09-02-02 — The LLM on/off comparison is unmeasured, not "no difference"

**Timestamp:** 2026-09-02, Block 9 (LLM tier)

**What broke:** three things in sequence, each hiding the next.

**1. The LLM tier had no work to do.** The generator emitted narrations in only the
five clean rail formats the regex tier already parses, so `needs_llm` fired on 0 of 137
credits. Reporting "precision with the LLM on versus off" would have compared a tier
against itself. Fixed by adding ~14% narrations in shapes real statements contain and
this parser cannot read: clearing entries, cheque deposits, mobile-banking strings and
free-text remitter descriptions. The regex tier now fails on 18 of 140 (13%).

**2. My measurement harness silently disabled the thing it was measuring.**
`select()` takes a bool, and I called `select("recorded")` — a truthy string, so it
returned `NullTier`. The first comparison I ran was therefore null against null, and it
produced a perfectly plausible result: identical numbers, which I nearly wrote up as
"the LLM changes nothing". It changes nothing when it is not running.

**3. The offline stand-in is not an independent second opinion.** With the tier
actually enabled, `RecordedTier` enriched all 18 narrations and got the payer name right
on **8 of 18 — exactly the same 8 the regex tier already got right.** It applies
essentially the same word-filtering heuristic as `normalize._extract_name`, so it agrees
with the deterministic tier by construction. A fixture that reimplements the thing it is
meant to improve on cannot demonstrate improvement.

**Conclusion, and it is a withholding rather than a result:** the LLM tier is
architecturally complete and the trust boundary around it is genuinely enforced --
`NarrationFields` has no field for a payment id, a candidate, or a score, so a model
cannot express a matching preference even in principle, and `parse_with_llm` fills gaps
only and never overrides deterministic output. All of that is tested.

But **the LLM-on versus LLM-off precision comparison is reported as UNMEASURED.** There
is no API key in this environment, and the offline stand-in is not a valid proxy for a
model. Both runs currently show 129/129 at 100% precision with 0 verdicts changed, and
that number means "the stand-in agrees with the regex tier", not "an LLM would add
nothing".

**Cost:** ~40 minutes.

**The pattern, again:** every plausible-looking number this block produced was an
artefact — 0 narrations needing the LLM, then identical on/off metrics from a disabled
tier, then identical extraction quality from a stand-in that shares the parser's logic.
Each would have passed unremarked. The only reason any of them surfaced was checking
what the component actually did rather than what its output looked like.

---

## 2026-09-02-03 — Generator assigned a random counterparty family to real payments

**Timestamp:** 2026-09-02, Block 9

**What broke:** One real (tier R1) payment carried `customer_name = "Acme Retail Pvt
Ltd"` while filed under `name_family = "acme_industrial"` — its deliberately confusable
twin from the registry.

**Diagnosis:** Found while auditing why a messy narration named a different counterparty
than ground truth. Real payments ingested from `data/real_payments.json` do not all
carry a `name_family` note — the earliest payment links were created before that
convention existed. The ingest path read
`cust = BY_KEY.get(fam) if fam else rng.choice(REGISTRY)`: with no family, it picked a
**random** counterparty and stamped its key onto the record, regardless of the name the
payment actually carried.

The registry contains three confusable pairs precisely so the matcher can be tested on
entities with similar names that must NOT be merged. Randomly filing a payment under its
own confusable twin manufactures that confusion by accident, in the one channel built to
measure it.

**Fix:** resolve the family from the name the payment actually carries
(`customers.resolve`), and fall back to random only when there is no name at all.
Inconsistent records: 1 -> 0.

**Cost:** ~10 minutes.

**Why it mattered more than one record:** it corrupted the Fellegi-Sunter name channel
for that payment and any measurement over it. It was invisible in every aggregate — the
batch had 200 payments and one wrong family — and surfaced only because a narration and
a ground-truth name were compared side by side while investigating something else.

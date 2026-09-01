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

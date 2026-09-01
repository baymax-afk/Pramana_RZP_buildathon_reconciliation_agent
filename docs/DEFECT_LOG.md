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

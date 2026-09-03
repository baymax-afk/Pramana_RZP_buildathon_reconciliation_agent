# REVIEW.md — hostile audit, 2026-09-03

Audited at commit `e2a0176`, branch `claude/project-state-review-9377hr`.
Every number below was computed in this audit, not read from a doc. Where I read a
number from a doc, I say so and I say whether the code agrees.

---

## 1. Verdict

This is not a toy — the verification apparatus is real, it runs at runtime, and the
ground-truth isolation is enforced structurally rather than promised, which is more
than most submissions in this category will have. **But it is not an agent, and the
track asks for one:** there is no tool, no loop, no LLM in any decision path, and
`docs/AGENTIC.md:3` says so itself — *"A design note, not a shipped feature."* The
measured numbers are strong and honest (precision **1.0000**, match rate **88.66%**,
and the engine sits **5 payments** from its own reachable ceiling of 91.24%), so the
finance judge in the room will be satisfied. The engineer will be satisfied by the
metamorphic gate and the refusal taxonomy, then will ask where the agent is, and the
answer today is "in a markdown file." What kills this is not the code — it is that
the artifact the demo actually serves (`reports/run_output.json`) carries
`verification: {relations: [], permutation_gate: null}` — and the reason is that
**your own test suite rewrites it without `--verify` on every run** — so the UI
silently renders *nothing* for the four-layer claim that is the entire submission.

---

## 2. Phase 1 — facts, with evidence

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Agent, chain, or prompt? | **Neither — a deterministic 3-tier cascade inside a fixpoint loop.** No LLM in the control path at all. | `src/recon/engine/match.py:342` `for _ in range(cfg.MAX_ROUNDS)` |
| 1a | Framework? | **None. Hand-rolled.** No LangGraph/CrewAI/SDK agent loop. `grep -rn "tool\|agent" src/ --include=*.py` returns nothing but the LLM `Protocol`. | `src/recon/llm/interface.py:61` |
| 1b | What terminates the loop? | A round that grants no assignment. Bounded by `MAX_ROUNDS = 6`. | `match.py:438` `if not granted: break`; `config.py:161` |
| 1c | Is any of `AGENTIC.md` shipped? | **No.** Rings 2 and 3 (investigator, orchestrator) are unimplemented. | `docs/AGENTIC.md:3` self-declares |
| 2 | Ground truth? | **Real, generator-written, 147 links**, with `expected_verdict`, `relation`, `defect_labels`. Not self-asserted. | `data/generated/_truth/ground_truth.json` |
| 2a | Does the evaluator see data the matcher didn't? | **Yes, and correctly so** — the scorer reads `defect_labels`/`relation` the engine never receives. Isolation is enforced by `sys.addaudithook` + `ReconInputs` carrying no paths. | `scorer/score.py:38 load_truth`; `tests/test_isolation.py` |
| 3 | **Measured match rate** | **88.66%** (172/194), precision **1.0000** (126/126) | run below |
| 4 | **False positives counted?** | **Yes** — `wrong_assignments` is a first-class field, and precision's denominator is *all* assignments. **Measured: 0.** | `scorer/score.py:137,289` |
| 5 | Exception list shape | **Structured, with a resolution path.** 10-member `RefusalCategory` enum + machine `reason` + operator `why` + `next_step` + `candidates` + `rupees_at_risk` + `confidence`. | `engine/results.py:28`; `reports/run_output.json` `exceptions[]` |
| 6 | LLM discretion | Confined to `NarrationFields{payer_name, merchant_ref}`. **But see §5 — the boundary claim is overstated.** | `llm/interface.py:31` |
| 7 | Weakest part | The gap between what the docs claim is running and what the shipped artifact contains. | §6, §7 |

### The run (seed 20260905, ppw=6, LLM off — the honest arm)

```
records            534  (200 payments + 147 bank txns + 187 invoices)
bank lines         141 credits + 6 debits
verdicts           assigned 126 · refused 15 · no_candidate 0     (= 141, exhaustive)
wall clock         36.5 ms  match_once
                  130   ms  gated K=8 (permutation refusal gate)
                  503   ms  metamorphic MR1–MR6
```

**Arithmetic, shown:**

```
match precision   = correct assignments / all assignments
                  = 126 / 126                              = 1.0000
match rate        = payments assigned / captured payments
                  = 172 / 194                              = 0.8866
refusal rate      = refusals / (assigned + refused)
                  = 15 / 141                               = 0.1064
refusal correctness = refusals truth also calls "refuse" / refusals
                  = 10 / 15                                = 0.6667
```

**This is a blend, and the blend is the right call.** Match rate is payment-recall
(a 4-payment settlement contributes 4). Precision is assignment-level. They have
different denominators on purpose — `scorer/score.py:8-22` argues it, and the
argument holds: with refusal available, either number alone is trivially gamed.

**One definitional drift to fix.** `docs/METRICS.md:41` defines match rate over
*"total payments in the batch"*; `score.py:286` divides by `captured_payments`.
172/200 = 86.0% vs 172/194 = 88.66%. The code is right (an uncaptured payment cannot
settle) and the doc is wrong. **A finance judge will check this denominator.**

---

## 3. Phase 2 — the gap, decomposed

**All 22 unassigned captured payments, by cause:**

| n | Cause | Hard case or bug? |
|--:|---|---|
| **6** | `unsettled` — the payment never settled, **no bank credit exists in the batch** | Neither. Unreachable by construction; belongs in the denominator's *ceiling*, not the exception list |
| **6** | `split_settlement` (2 credits) + the payments behind them | **Hard.** Named model limitation — `claimed` is a set; there is nowhere to put half a payment |
| **5** | `bank_charge` — receiving bank took ₹5–50, unmatchable at ₹1 tolerance | **Hard, and correct.** Labelled `refuse`; refusing is the right output |
| **4** | The hand-placed ambiguity case (`many_to_one`, 2 subsets fit) | **Hard, and correct.** Refusing is the designed answer |
| **5** | `third_party_payer` — **`amount_name_conflict`, residual `+0p`** | **Fixable.** The only genuinely conservative miss |

**17 correctly unassigned. 5 conservative misses. Zero errors.**

```
reachable ceiling = (172 assigned + 5 conservative misses) / 194 = 177/194 = 91.24%
measured                                                                   = 88.66%
gap to own ceiling                                        = 5 payments = 2.58 pts
```

**The whole remaining gap is one defect class.** All five refusals read identically:
FS field weight `-3.26`, residual `+0p`. The amount channel is *exact*; the name
channel disagrees because a parent company paid a subsidiary's invoice. The engine
refuses because two independent channels disagree — which is its stated doctrine.

### Clean number vs higher number — argued against this composition

**A clean number is decisively stronger here, and the composition is why.** If your
exception list were 15 vague "unmatched" rows, a higher headline would buy you
something. It isn't: **10 of 15 are refusals ground truth agrees with**, and of the
5 that aren't, all 5 share one cause with an identical numeric signature you can put
on a slide. That is not "a well-explained exception list" as a consolation prize —
it is a stronger claim than 95% would be, because it says *the engine knows exactly
what it doesn't know.*

The trap is the opposite direction. Closing the third-party gap means letting a
name mismatch be overridden — and if you do that by loosening the FS threshold, you
will buy 2.58 points of coverage and put precision 1.0000 at risk. **That trade is
strictly bad for this submission.** Precision 1.0000 with a 5-row explained gap beats
91% with a wobbling precision, every time, in front of anyone who has reconciled for
a living.

**Recommended target: leave the number at 88.66% and do not chase it by loosening
anything.** Aim the remaining time at *composition legibility* — make the demo show
the 17-vs-5 split and the 91.24% ceiling explicitly — and at closing the 5 by
**supplying evidence the engine did not have**, which is the only route that does not
put precision at risk. "We are 5 payments from the maximum this data permits, here are
all 5, and here is the single piece of evidence that would close them" is the strongest
sentence available to this project, and it is true today.

---

## 4. P0 / P1 / P2

### P0 — do before the demo

| # | Issue | Evidence | Hrs |
|---|---|---|---|
| **P0-1** | **The shipped artifact has no verification data, and the test suite is what strips it.** `reports/run_output.json` carries `verification: {relations: [], permutation_gate: null}`. `App.jsx:198` returns `null` when both are empty, so the Verification section **silently vanishes** — not a crash; worse, you won't notice until you're on stage. **Root cause, reproduced during this audit:** `tests/test_cli_robustness.py:120` shells out to `run.py match` with `cwd=ROOT` — the real repo, no `--verify` — so **every `pytest` run overwrites the committed demo artifact and removes its verification block.** This is not an operator mistake to remember not to repeat; it is automated, and it will undo the fix. | `reports/run_output.json`; `ui/src/App.jsx:194-198`; `tests/test_cli_robustness.py:113-116` | **1** |
| **P0-2** | **No assignments view in the UI.** Tabs are `exceptions` and `invoices` only. 126 of 141 outcomes are invisible. A judge cannot click a *match* and see why it was made — the data (`tier`, `residual_tightness`, `uniqueness_margin`, `fs_weight`, `confidence`) is already in `/api/run` and nothing renders it. | `ui/src/App.jsx:294-305` vs `run_output.json` `assignments[]` | **3** |
| ~~**P0-3**~~ **FIXED 2026-09-03** | **A live `ANTHROPIC_API_KEY` turns the demo into a 10-minute hang.** *Confirmed, not projected: the first live run was killed after minutes with no output. Cache + timeout=10s + max_retries=1 + a call cap landed; the same command now finishes in 0.40 s.* `ClaudeTier._ask` has **no timeout, no retry budget, no cache**, and 13 `needs_llm` narrations × up to 6 rounds × 8 permutation passes are made **serially** (the LLM tier is unpicklable, so `ProcessPoolExecutor` falls back to sequential). *Inference from code, not a live run: ~312–624 calls.* On venue wifi the SDK's default timeout applies per call. | `llm/claude.py:85-102`; `config.py:161,211` | **1** |
| **P0-4** | **`decomposition_out_of_bounds` is miscategorised on every instance.** All 6 fire on pools of **1, 1, 1, 2, 4, 4** — none exceeded `MAX_POOL=20` or `k=6`. The operator prose says *"there were too many candidates to search exhaustively"* on a credit with **one** candidate. The largest single exception (₹45,673) reads this way. A reconciliation judge will catch it. | run output; `run_output.json` `exceptions[].why` | **1.5** |

### P1

| # | Issue | Evidence | Hrs |
|---|---|---|---|
| **P1-1** | **Doc drift the new drift test does not cover.** `README.md:103` says *"136 bank transactions, 200 invoices"*; the manifest says **147** and **187**. `docs/METRICS.md` carries **two contradictory refusal rates for ppw=6** — 10.1% (line 208, correct, matches the sweep) and 0.7% (line 318, stale). `docs/AGENTIC.md:3-5` is written against *"129 assignments, 6 exceptions worth ₹57,775"* and *"partial recall 0/5"* — all superseded (126, 15, ₹301,909, 5/5). | `manifest.json`; sweep run | 2 |
| **P1-2** | **Match-rate denominator: doc says `total payments`, code says `captured_payments`.** 86.0% vs 88.66%. Code is right. | `METRICS.md:41` vs `score.py:286` | 0.25 |
| **P1-3** | **40% of exceptions carry zero candidates** (6 of 15, including the ₹45,673 top row), against `results.py:48`'s claim that a refusal naming what it declined is actionable and one saying only "ambiguous" is not. | run output | 2 |
| **P1-5** | **`pytest` mutates tracked files.** `tests/test_cli_robustness.py:113-116` runs `run.py match` as a subprocess with `cwd=ROOT`, so a full test run rewrites `reports/run_output.json` in the working tree. Its own docstring says the test "must not leave the tree modified if it is killed" — but it modifies the tree on **success**, every time. Point the subprocess at a `tmp_path` copy, or pass `--no-score`. | `tests/test_cli_robustness.py:113-116`; `git status` after `pytest` | 1 |
| **P1-4** | **The bank statement's running `balance` column is loaded and never verified.** A free integrity check that would catch a silently dropped or duplicated statement row — exactly the failure a controller fears most. | `loaders.py:108` | 2 |

### P2

| # | Issue | Evidence | Hrs |
|---|---|---|---|
| **P2-1** | `_money` returns **0 for a blank cell** rather than raising. Legitimate for `credit` on a debit row; silently wrong if `balance` or both amount columns are blank. | `loaders.py:69-70` | 1 |
| **P2-2** | Confidence is uncalibrated and **decorative** — all 126 assignments land in a single bin (0.95) at base rate 1.000. Already disclosed as W1, correctly. | `confidence_deciles`; `OUTSTANDING_TASKS.md` W1 | — |

---

## 5. Phase 3 — agentic deep-dive

### Tool design
**There are no tools.** Nothing to scope, nothing to misuse. The LLM's only surface is
`parse_narration(str) -> NarrationFields` and `explain(...) -> ExceptionProse` — both
**typed dataclasses, not free text**, so nothing downstream re-parses model output.
That part is genuinely right and is worth showing.

### The trust boundary is overstated, and this is the finding a deep engineer will land

`interface.py:9` claims a model *"structurally cannot"* express a matching preference
because `NarrationFields` has no payment-id field. **It doesn't need one.**

```
LLM fills parsed.merchant_ref            (llm/interface.py:42)
  -> tier1_reference.match() token set   (tier1_reference.py:88)
  -> ReferenceIndex indexes invoice_no   (tier1_reference.py:57-59)
  -> resolves to a payment id            (tier1_reference.py:124)
  -> Candidate(tier="tier1_reference")
  -> _TIER_RANK[tier1] = 2, top rank     (match.py:171)
  -> evidence_key wins contested money   (match.py:191,270)
```

**An invoice number is a payment identifier with one hop of indirection.** A model
that emits a plausible `INV-2026-xxxx` selects a payment, promotes the bid to the
highest evidence tier, and wins contested payments outright.

**Measured, not theoretical.** Swapping the offline stand-in in:

| arm | assigned | tier1 | tier2 | tier3 | match rate | precision |
|---|--:|--:|--:|--:|--:|--:|
| LLM off | 126 | 37 | 69 | 20 | 0.8866 | 1.0000 |
| LLM on (`recorded`) | **127** | **46** | **61** | 20 | 0.8918 | 1.0000 |

The stand-in **flips one refusal to an assign** (`bank_txn_0103`,
`amount_name_conflict` → assigned, correctly) **and moves 9 credits from tier 2 to
tier 1** — changing the evidence rank that decides contests, on 9 matches.

Two real mitigations already exist and should be stated instead of the absolute claim:
the regex value always wins the merge, so the LLM only fills *gaps*; and
`tier1_reference.py:143` still requires the amount to fit, so a fabricated reference
cannot post an arithmetically wrong match.

**Recommended rewording:** *"The LLM cannot name a payment and cannot override the
amount channel. It can supply a reference the regex tier missed, which — if it
resolves and the amount fits — promotes the match to tier 1. Measured effect:
+1 assignment, 9 tier reclassifications, precision unmoved."* That is defensible.
The current sentence is not.

### State and resumability
`match_once` is a pure function of `ReconInputs` — no clock, no paths, no globals.
Excellent for verification, and it is what makes MR1 meaningful. **There is no
mid-batch resume and no need for one at 36 ms.** Nothing silently drops: all 141
credits land in exactly one of assign/refuse/no_candidate (verified: 126+15+0=141).

### Decision boundaries — both directions

**Move OUT of the LLM (already done, keep it):** every match decision. Correct.

**Move INTO the LLM — one candidate, and only one.** The five `third_party_payer`
refusals are a *name-identity* judgement ("is CHOLA FINANCE authorised to settle for
ORCHID FOODS?"), which is a knowledge question, not an arithmetic one — the exact
shape LLMs are good at and Fellegi–Sunter cannot represent. **But route it as
evidence, not as a verdict:** the agent asserts a payer-relationship fact, that fact
enters the FS evidence vector as a new field, and the deterministic engine re-runs and
reaches its own conclusion. That is the one honest lever `AGENTIC.md:33` already
identifies. Do not let it override the band.

**Move further INTO code:** nothing. The brittle rules (`_split_jammed`,
`_canonical_ref` in `recorded.py`) are already the LLM tier's job when a key exists.

### Escalation — the strongest part of the system
Ten named refusal categories, each mapped to the verification layer that objected.
Thresholds are Fellegi–Sunter log-likelihood bands with a documented derivation, not
magic numbers, and `TOL_ABS_PAISE=100` is derived against `MIN_PAYMENT_PAISE` with a
stated margin. **`config.py:5` forbids tuning any of it in response to a
disappointing metric.** This engine says "I don't know" 15 times and is right 10 of
them; the other 5 are one explained cause.

**Confidence vs correctness: does not correlate — because it cannot.** All 126
assignments occupy one decile (0.95) at accuracy 1.000. The refusal layers strip
every error *before* the confidence stage, so what survives is correct regardless of
its score. Disclosed honestly as W1 with its bin count. Do not let a judge quote the
ECE.

### Failure handling
Deterministic path: no retries needed, no timeouts to blow, no infinite loop
(`MAX_ROUNDS=6` + no-progress break). **LLM path: no timeout, no retry budget, no
cache, and a bare `except Exception: return {}`** (`claude.py:101`) that degrades
silently — you would not know the tier had failed. See P0-3.

### Observability — **the honest answer is "half"**
For an **exception**: yes, and well — category, engine reason, operator prose, next
step, both candidate subsets, rupees at risk, all in the UI.
For a **match**: **no.** The data exists in `/api/run` and no view renders it. See P0-2.

### Rewrite verdict: **NO-GO** *(recorded as given; the project owner has since elected
to proceed with the full tool-calling rewrite. The reasoning below is kept unchanged.)*

Do not restructure the loop. The deterministic core is the submission's entire
argument and it is measurably working — precision 1.0000 across four density arms,
MR1–MR6 all passing, 295 tests green. Rebuilding it as an agent loop in 1–3 days
would put the one thing that is *provably* right at risk for the thing that is
currently only *described*.

**The smallest honest alternative — a narrow Ring-2 investigator beside it, ~6 hours:**

1. **(2h)** Add `authorised_payer_for: str | None` to the FS evidence vector, plumbed
   through `fellegi_sunter.evidence_for`. Deterministic default: `None` → today's
   behaviour exactly. **Ship this alone and nothing changes; that is the point.**
2. **(2h)** A single-purpose agent that reads *one* exception, queries a
   group-structure fixture, and emits that one field. It never sees a payment id and
   never returns a verdict.
3. **(1h)** Re-run the deterministic engine with the enriched input and diff.
4. **(1h)** Report **evidence-attributable coverage gain** — `AGENTIC.md:217`'s own
   metric — as *"+N payments, precision unchanged."*

**Fallback if it slips: delete the branch.** Step 1 is a no-op by construction, so
there is no path where this breaks the demo. That property is why it's the right build,
and it is preserved as the foundation of the larger rewrite now being undertaken.

---

## 6. Phase 4 — live demo risk register

| Risk | Severity | Reality | Mitigation |
|---|---|---|---|
| **Verification renders as nothing** | **Critical** | Confirmed. Shipped `run_output.json` has `relations: []`, `permutation_gate: null` | P0-1: regenerate with `--verify`, commit, and **open the UI once before you present** |
| **Run-to-run variance** | **None, including with a live model** | **Proven twice.** 5× `match_once` offline → identical SHA-256 fingerprint of the assignment map + refusal set (39.5/32.5/31.8/33.0/32.2 ms); K=3 and K=8 gated runs matched. **Then re-proven with a live `claude-sonnet-5` tier: 5 runs, fresh tier each time, one fingerprint — the same one the offline arm produces.** The model is non-deterministic at the field level (7 refs one run, 8 the next) and the engine is deterministic at the verdict level, because a recovered reference only matters if it changes a tier decision and the amount channel still has to agree | Nothing to do. **Say this out loud — it is the strongest single piece of evidence that the verification architecture does what it claims** |
| **API timeout / rate limit / dead wifi** | **Zero as configured; critical if a key appears** | Default `select()` returns `RecordedTier` — fully offline, zero API calls, ₹0 per batch. But it reports `enabled: True` | P0-3. Also: **`unset ANTHROPIC_API_KEY` before you present**, unless the agent demo needs it |
| **Visible stall** | **None offline; 30 s with the live tier** | Worst deterministic path (`--verify`, K=8, MR1–MR6) ≈ **0.6 s** end to end. But a cold live tier costs **30–35 s**, all of it sequential HTTP — measured, ~1000× the 33 ms offline arm | Demo the deterministic arm; show the live comparison as a **pre-computed artifact**. Do not run the live tier on stage |
| **Deterministic replay / cached fallback** | **Already exists** | `reports/run_output.json` is committed; API + UI read it and never invoke the engine. The demo is a static-file read | **Buildable in 0 h — it is already the architecture.** Just fix P0-1 so the cached file is the *good* one |
| **"Your LLM is fake"** | **High, and fair** | `RecordedTier` is hand-authored rules, not recorded model output. `recorded.py:1-21` is admirably explicit | Say it first, in your own words, before a judge says it for you |

---

## 7. Phase 5 — generalization: where it has overfit its own generator

| Stress | Predicted failure | Smallest fix |
|---|---|---|
| **One-to-many** (1 payment → N credits) | **Confirmed today.** 4 payments unmatched; `claimed` is a `set[str]` and every tier asks "which *subset of payments* sums to this credit" — there is no half-payment | Not small. Needs a bipartite residual model. **Keep as a named limitation** |
| **Many-to-many** | Unrepresentable, same reason | Same |
| **Partial payments** | **Works: 5/5.** Was 0/5 until a generator defect was found | — |
| **Fee-deducted settlements** | **Works: 126/131.** MDR band `(0.018, 0.025)` is fitted to **18 real observations** of one merchant at 2.2% | A rate outside the band silently refuses. Widen only with evidence; log the implied rate on refusal (**1h**) |
| **Refunds / reversals** | Credit side works (`amount_refunded`). **Debits are invisible** — `is_credit` only. 6 chargebacks (₹166,732) never examined | Disclosed in `not_examined`. Correct handling needs a signed model (**large**) |
| **Multi-currency / FX** | **Will fail hard.** `rupees_to_paise` is INR-only; `currency` is loaded and never checked. A USD row becomes paise silently | **Reject non-INR rows at load with a named error (0.5h).** Cheapest credibility win on this list |
| **Timing / period cutoff** | `LOOKBACK_DAYS = 5` is derived, not guessed, and `date_of` is correctly UTC-pinned with a documented history of getting it wrong. **A settlement drifting 6+ days silently leaves the pool** | Emit `no_candidate` with the nearest out-of-window payment named (**1h**) |
| **Duplicate transaction IDs** | **Handled well.** `ReferenceIndex` maps to *sets* and refuses on collision rather than picking | — |
| **Near-identical amounts, same day** | **Handled — this is the load-bearing case.** The hand-placed ambiguity credit finds 2 subsets and refuses, ₹800 at risk, both named | — |
| **Missing / renamed columns** | **Handled well.** `_money`/`_text` raise naming file, row, column, and the columns actually present | Blank → `0` is the one soft edge (P2-1) |
| **Adversarial free text** | Regex tier degrades to `needs_llm`; `_clean` caps length and strips control chars. **Untested against narrations this generator doesn't emit — the LLM's whole value, unmeasured** | Hand-write 10 adversarial narrations, run both arms (**2h**) |

**The honest summary:** it has overfit its generator in *coverage of shapes*, not in
*tuning*. `config.py` is frozen and the tolerances are derived from first principles,
so nothing here is fitted to the evaluation data. That is a much better failure mode
than the reverse, and it is defensible out loud.

---

## 8. Phase 6 — additions

### Ship in 1–3 days, ranked by impact per hour

| # | Item | Hrs | Why it earns the slot |
|---|---|--:|---|
| 1 | **Regenerate `run_output.json` with `--verify`** | 0.5 | Without it your central claim is invisible. Highest ratio on the list by a wide margin |
| 2 | **Reject non-INR rows at load** | 0.5 | A reconciliation judge *will* ask about currency. "We reject it by name" beats "out of scope" |
| 3 | **Fix `decomposition_out_of_bounds` miscategorisation** | 1.5 | Split into `no_subset_fits` vs `pool_exceeded`. Today every instance reads "too many candidates" on pools of 1–4 |
| 4 | **Timeout + cache + call cap on `ClaudeTier`** | 1 | Removes the only way the live demo can hang |
| 5 | **Assignments view in the UI** | 3 | Makes 126 of 141 outcomes inspectable. Answers "show me why *this* match" live |
| 6 | **Ceiling panel: "177/194 = 91.24% reachable; we are 5 payments short, here they are"** | 2 | Turns your gap into your strongest slide |
| 7 | **Ring-2 investigator** | 6+ | The only thing that makes "agentic" true. Additive; deletable |
| 8 | **Reword the trust boundary honestly** | 0.5 | Removes the one claim a sharp engineer can falsify on stage |
| 9 | **Bank-statement balance-continuity check** | 2 | The check a controller expects and no submission will have |

### Post-buildathon — product direction

- **Evidence-attributable coverage gain as the product metric.** Not "our AI matched
  more" but "this evidence source closed N exceptions at unchanged precision." It is
  auditable, it is a procurement argument, and `AGENTIC.md:217` already names it.
- **The refusal taxonomy as a routing table.** Ten categories → ten queues → ten SLAs.
  This is what an AP team actually buys.
- **Verification-as-a-service.** The four layers do not depend on Pramana's matcher.
  Point them at an incumbent's output and report *its* precision. Given the README's
  own observation that vendors publish coverage and not precision, that is a wedge.
- **BenchRec calibration** (W1) to make the confidence score mean something.
- **Signed transaction model** to bring chargebacks and reversals inside the boundary.

### What separates this from the other submissions

**Assumed field** *(stated as an assumption — I have not seen the other entries)*: most
will be an LLM prompted with two CSVs, asked to output matches, reporting a match rate
with no precision, no refusal path, no ground truth, and an "exceptions" list that is
whatever the model failed to mention.

**Three things here they structurally cannot have:**

1. **A precision number at all** — which requires ground truth *and* a refusal path.
   You have both, and 1.0000 across four density arms.
2. **The density sweep.** Coverage 87.0% → 76.6% while precision holds 1.0000 as the
   pool grows 8.8 → 52.8. It is the chart no vendor publishes, because publishing it
   requires reporting precision.
3. **Bit-identical reproducibility.** 5 runs, one fingerprint. A judge can ask you to
   re-run it live and it will not move.

**Lead with #2.** It answers "does it break under pressure" with a measurement instead
of a promise, and it is the one slide an LLM-prompt submission cannot fake.

---

## 9. The three questions a judge will ask that you currently cannot answer

1. **"Where is the agent?"**
   There isn't one. `AGENTIC.md` is a design note by its own first line.

2. ~~**"Does the LLM help? By how much?"**~~ — **answerable as of 2026-09-03.**
   A key was supplied and the comparison ran live against `claude-sonnet-5`:
   **+1 assignment (88.66% → 89.18%), precision unmoved at 1.0000**, from 8 of 13
   unreadable narrations filled — all merchant references, no payer names.

   Two caveats to give unprompted, because a judge will find them otherwise. **The live
   model does slightly worse than the hand-written stand-in** (8 gaps filled vs 9),
   which is exactly what `recorded.py` predicted: the stand-in was written for this
   generator's narration shapes, so this measures the tier on home ground and says
   nothing yet about arbitrary bank narrations. And **the honest size of the win is one
   assignment** — worth reporting precisely because it is small and measured rather than
   asserted.

3. **"Precision is 1.0000 — is your synthetic data too easy?"**
   The sweep is a partial answer (precision holds while coverage drops 10 points), but
   every number comes from one generator, and 1.0000 across four arms is more likely to
   read as *unfalsified* than as *strong*. BenchRec would settle it and is blocked two
   independent ways. **Have the honest version ready:** *"Precision 1.0000 means the
   refusal layers strip every error before it posts — which is also why our confidence
   score is uncalibrated and we say so. On real data I'd expect precision to fall and
   refusal rate to rise; the architecture is built for that and we cannot prove it here."*

---

*Audited read-only by design: the pipeline runs were executed in-process from a scratchpad
driver that writes nothing, so `data/generated/` was untouched. **One exception, and it
became finding P1-5:** running the test suite to verify the 295-test claim rewrote
`reports/run_output.json` via `test_cli_robustness.py`. The file was restored with
`git checkout`; the diff was a timestamp and a throughput figure, with the verification
block empty both before and after. That accident is what identified P0-1's root cause.*

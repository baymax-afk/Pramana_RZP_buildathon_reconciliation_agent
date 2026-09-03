# Architecture flowcharts

Diagrams of how the system actually behaves, not how it was planned to. Where the two
differ, the difference is noted — several of these were redrawn after measurement
contradicted the original design.

---

## 1. End-to-end data flow, and where the trust boundary sits

The two red boundaries are the whole architecture. **Ground truth never crosses into the
engine**, and **the LLM never crosses into a verdict.**

```mermaid
graph TB
    subgraph SRC["Real data"]
        R1["R1 · 24 captured Razorpay payments<br/>real id, fee, tax, bank"]
        R2["R2 · 12 Razorpay orders<br/>real ids, synthetic fee"]
    end

    GEN["Generator<br/>build · defects · fees · customers"]
    TRUTH[["_truth/ground_truth.json<br/>THE ANSWER KEY"]]
    DISK["payments.json<br/>bank_statement.csv<br/>invoices.csv"]

    R1 --> GEN
    R2 --> GEN
    GEN -->|writes| DISK
    GEN -->|writes| TRUTH

    LOAD["loaders.py<br/>rupees to integer paise"]
    DISK --> LOAD
    LOAD -->|ReconInputs<br/>dataclasses, no paths| ENGINE

    subgraph ENGINE["Engine — inside the isolation boundary"]
        TIERS["Tiers 1-3 + Layers 1-4"]
    end

    ENGINE -->|MatchOutput| SCORER["scorer/<br/>OUTSIDE the boundary"]
    TRUTH -->|only reader| SCORER
    SCORER --> METRICS["Metrics block"]

    ENGINE -->|MatchOutput| REPORT["report/run_output.py<br/>no truth, no scoring"]
    REPORT --> JSON["reports/run_output.json"]
    JSON --> API["FastAPI · read-only"]
    API --> UI["React triage UI"]
    UPLOAD["Invoice upload"] -->|replaces INPUT only| DISK

    style TRUTH fill:#fde7e7,stroke:#c0392b,stroke-width:3px
    style ENGINE fill:#eef4fb,stroke:#1f5c9e,stroke-width:2px
    style SCORER fill:#f3f0fa,stroke:#6a3e93,stroke-width:2px
```

**Why the arrow from `_truth` goes only to the scorer.** Enforced three ways: the
engine's input type carries no paths, an import-time audit hook raises if anything under
`recon.engine` opens that directory, and a test deletes the directory entirely and
asserts the engine produces byte-identical output.

---

## 2. The matching pipeline — descending order of evidence strength

Each tier either resolves, falls through, or **refuses**. A tier that finds ambiguity
stops: a weaker tier cannot resolve what a stronger one has shown to be underdetermined.

```mermaid
flowchart TD
    START(["Bank credit"]) --> PARSE["Tier 0 · normalize<br/>regex parse of narration"]
    PARSE -->|regex failed| LLM["LLM tier<br/>returns FIELDS only<br/>fills gaps, never overrides"]
    PARSE -->|parsed| T1
    LLM --> T1

    T1{"Tier 1<br/>exact reference"}
    T1 -->|one match| CHECK1{"amount also fits?"}
    T1 -->|reference hits 2+ payments| REF_DUP["REFUSE<br/>duplicate reference"]
    T1 -->|no reference match| T2

    CHECK1 -->|yes| ASSIGN
    CHECK1 -->|no| RESID["REFUSE<br/>unexplained residual"]

    T2{"Tier 2<br/>amount + date window"}
    T2 -->|exactly one fits| ASSIGN
    T2 -->|several fit| MULTI2["REFUSE<br/>amount cannot single one out"]
    T2 -->|none fit| T3

    T3{"Tier 3<br/>bounded subset-sum"}
    T3 -->|pool > MAX_POOL| BOUNDS["REFUSE<br/>cannot search exhaustively"]
    T3 -->|no subset fits| NONE["REFUSE<br/>nothing accounts for it"]
    T3 -->|2+ subsets fit| MULTI3["REFUSE<br/>Layer 2 uniqueness"]
    T3 -->|exactly one subset| ASSIGN

    ASSIGN["Candidate assignment"] --> COUNT{"narration states a<br/>transaction count?<br/>does it MATCH the<br/>number of payments?"}
    COUNT -->|"count disagrees"| CNTNO["REFUSE<br/>narration_count_conflict"]
    COUNT -->|"agrees, or silent"| FS{"Layer 3 · Fellegi-Sunter<br/>does the name/reference<br/>evidence CONTRADICT?"}
    FS -->|contradicts| FSNO["REFUSE<br/>counterparty disagrees"]
    FS -->|silent or supports| PROP["PROPOSE<br/>bid for these payments<br/>nothing is granted yet"]

    PROP --> RES{"Layer 2 · resolve<br/>does another credit bid<br/>for the same payment?"}
    RES -->|"rival evidence >= mine"| CONT["REFUSE<br/>contested_payment<br/>a tie refuses BOTH"]
    RES -->|"uncontested, or I win strictly"| GATE{"Layer 1 · permutation gate<br/>stable across K=8 orderings?"}
    GATE -->|no| ORD["REFUSE<br/>decided by iteration order"]
    GATE -->|yes| FINAL(["ASSIGN"])

    style REF_DUP fill:#fdeceb,stroke:#c0392b
    style RESID fill:#fdeceb,stroke:#c0392b
    style MULTI2 fill:#fdeceb,stroke:#c0392b
    style MULTI3 fill:#fdf1e3,stroke:#8a5300,stroke-width:2px
    style BOUNDS fill:#fdeceb,stroke:#c0392b
    style NONE fill:#fdeceb,stroke:#c0392b
    style CNTNO fill:#fdeceb,stroke:#c0392b
    style FSNO fill:#fdeceb,stroke:#c0392b
    style CONT fill:#fdf1e3,stroke:#8a5300,stroke-width:2px
    style ORD fill:#f4ecfa,stroke:#6a3e93
    style FINAL fill:#eaf5ee,stroke:#2c6b41,stroke-width:2px
```

**Ten distinct ways to refuse and one way to assign.** That asymmetry is deliberate.
Every refusal names its cause, so an exception says which mechanism objected rather than
merely that something did.

Two of the ten were added after the diagram was first drawn, and both are worth calling
out because they are the only places the engine uses evidence that is not an amount:

- **`narration_count_conflict`.** A settlement narration states how many transactions it
  covers — `RAZORPAY SETTLEMENT setl_... 2 TXNS`. The engine had parsed that count since
  Block 3 and never consulted it, so a credit covering two payments could be posted to
  one whenever a netted refund happened to make the batch total equal a single payment's
  net. Tier 2 took the exact one-to-one fit and never reached tier 3, where enumeration
  would have found both decompositions and Layer 2 would have refused — the tier ordering
  short-circuited the uniqueness test. The count is admissible precisely because it is
  *independent of the amounts*: it can contradict an arithmetic fit without being derived
  from one.
- **`contested_payment`.** Claiming used to be greedy — credits were walked in sorted
  order and each took what it wanted, so two credits with equal claims on one payment
  were separated by the sort. The round is now propose-then-resolve, and equal evidence
  refuses **both**. A tie is not something to break; it is the same underdetermination
  Layer 2 already refuses on, reaching the engine through a different door.

---

## 3. Layer 1 — the permutation gate at runtime

MR1 is not only a test. It is the engine's primary execution path.

```mermaid
flowchart LR
    IN(["ReconInputs"]) --> P0["Pass 0<br/>original order"]
    IN --> P1["Pass 1<br/>shuffled"]
    IN --> PK["Pass K-1<br/>shuffled"]

    P0 --> ENS["Ensemble<br/>per credit: Counter over<br/>frozenset of payment ids"]
    P1 --> ENS
    PK --> ENS

    ENS --> Q{"stability == 1.0<br/>for this credit?"}
    Q -->|yes| KEEP(["keep the assignment"])
    Q -->|no| REF(["REFUSE<br/>order_dependent_assignment<br/>lists every variant seen"])

    style REF fill:#f4ecfa,stroke:#6a3e93,stroke-width:2px
    style KEEP fill:#eaf5ee,stroke:#2c6b41
```

Two things this diagram had to be corrected for:

- **Absence counts against stability.** A credit assigned in 4 of 8 passes is *unstable*,
  not "perfectly stable across the passes where it appeared".
- **Every credit seen in ANY pass is gated**, not just those assigned in pass 0. Gating
  only pass 0's assignments silently exempted exactly the unstable ones — a credit
  assigned in 7 of 8 orderings and dropped in the 8th never reached the gate if the 8th
  happened to be pass 0.

The gate currently refuses nothing, and the reason has changed since this was written.
Three properties now make the matcher order-independent *by construction*: credits are
walked in a total, data-derived order; every tier refuses rather than chooses on ties;
and — new — claiming is no longer greedy. Each round proposes against a frozen `claimed`
set and then resolves contested payments on evidence, refusing when evidence ties, so no
credit's bid can shrink a later credit's pool.

That last one used to be the gap the gate existed to cover. Covering a design weakness
with a detector is weaker than not having it, so the gate is now a safety net rather than
load-bearing. Its zero is only meaningful because it is separately shown to fire against
a deliberately order-dependent matcher — see `tests/test_verification.py`.

---

## 4. How a payment's settled amount becomes an interval

The engine never knows a fee exactly unless Razorpay priced it, so every amount is a
range and every comparison is interval arithmetic.

```mermaid
flowchart TD
    P(["Payment"]) --> Q{"Razorpay returned<br/>a fee?"}
    Q -->|yes| TIGHT["net = amount - fee<br/>±2 paise<br/>certain = true"]
    Q -->|no| WIDE["net_lo = amount - ceil(rate_max × amount × 1.18)<br/>net_hi = amount - floor(rate_min × amount × 1.18)<br/>certain = false"]

    TIGHT --> DEDUCT
    WIDE --> DEDUCT
    DEDUCT["subtract KNOWN deductions<br/>· invoice TDS, deduped per invoice<br/>· refunds recorded on the payment"]
    DEDUCT --> CMP{"credit inside<br/>interval ± tolerance?"}
    CMP -->|yes| OK(["fits"])
    CMP -->|no| NO(["residual reported<br/>with its sign"])

    style TIGHT fill:#eaf5ee,stroke:#2c6b41
    style WIDE fill:#fdf1e3,stroke:#8a5300
```

**The engine deliberately does not share the generator's exact fee schedule.** If it did,
MR4 conservation would be tautological — the engine would reconcile because it was
inverting the very function that produced the data.

**Known deductions are subtracted, not searched for.** TDS is on the invoice and refunds
are on the payment, so both are facts before matching begins. Treating them as unknowns
would add a free variable per payment and make subset-sum combinatorially worse while
producing weaker answers.

---

## 5. The four verification layers, and where they can disagree

```mermaid
graph TB
    A["Layer 1 · Metamorphic<br/>MR1-MR6 + runtime gate<br/>reasons over EXECUTIONS"]
    B["Layer 2 · Uniqueness<br/>enumerate ALL subsets<br/>reasons over AMOUNTS"]
    C["Layer 3 · Fellegi-Sunter<br/>two-threshold band<br/>reasons over NAMES + REFS"]
    D["Layer 4 · Materiality<br/>PCAOB AS 2315<br/>reasons over EXPOSURE"]

    A --> S["Composite confidence"]
    B --> S
    C --> S
    D --> RANK["Exception ranking<br/>by rupees at risk"]

    S --> DIS{"do the channels<br/>disagree?"}
    DIS -->|"amounts tight, names weak"| X1["amount_name_conflict<br/>do NOT auto-accept"]
    DIS -->|"names strong, amounts loose"| X2["unexplained_residual"]
    DIS -->|"unique subset, unstable order"| X3["pipeline defect<br/>refuse AND log"]
    DIS -->|"several subsets, names favour one"| X4["STILL REFUSE<br/>the weaker channel may not<br/>break an amount tie"]
    DIS -->|agree| OUT(["assign with confidence"])

    style X4 fill:#fdf1e3,stroke:#8a5300,stroke-width:2px
    style OUT fill:#eaf5ee,stroke:#2c6b41
```

Conservation reasons over amounts; Fellegi-Sunter reasons over names and references.
They are independent channels that cannot fail the same way, which is the entire
justification for combining them — and why **amount is deliberately excluded from the FS
inputs.** Feeding it to both would make them correlated and the corroboration illusory.

**Measured caveat.** The composite confidence score is currently *decorative*: the
refusal layers strip essentially every error before scoring, so accepted assignments are
correct ~99% of the time regardless of their features and the score adds no measurable
information. See `METRICS.md`.

---

## 6. The density sweep — the claim that IS supported

```mermaid
graph LR
    D1["density 3<br/>pool 8.8"] --> R1["refusal 7.7%<br/>precision 1.0000"]
    D2["density 6<br/>pool 15.4"] --> R2["refusal 9.8%<br/>precision 1.0000"]
    D3["density 12<br/>pool 27.8"] --> R3["refusal 11.6%<br/>precision 1.0000"]
    D4["density 24<br/>pool 53.0"] --> R4["refusal 18.2%<br/>precision 0.9978"]

    R1 --> C["As ambiguity rises the engine<br/>REFUSES MORE and stays RIGHT.<br/>Coverage degrades, correctness does not."]
    R2 --> C
    R3 --> C
    R4 --> C

    style C fill:#eaf5ee,stroke:#2c6b41,stroke-width:2px
```

Reproduce with `python run.py sweep`. This is the chart no vendor publishes, because
producing it requires reporting precision.

---

## 7. Invoice upload — the one write path

```mermaid
flowchart TD
    F(["CSV chosen"]) --> V["POST /api/invoices/validate<br/>DRY RUN — changes nothing"]
    V --> Q{"valid?"}
    Q -->|no| ERRS["show every error<br/>with row numbers"]
    Q -->|yes| PREVIEW["show row count + preview"]
    PREVIEW --> CONFIRM(["explicit second click"])
    CONFIRM --> POST["POST /api/invoices"]
    POST --> ARCH["archive current ledger<br/>data/ledger_versions/"]
    ARCH --> WRITE["replace invoices.csv"]
    WRITE --> NOTE["banner: verdicts on screen are UNCHANGED<br/>re-run the engine to reconcile"]

    style ERRS fill:#fdeceb,stroke:#c0392b
    style NOTE fill:#fdf1e3,stroke:#8a5300,stroke-width:2px
```

Three deliberate properties: validation is a **separate dry run** so nobody replaces a
ledger on one click; rejection is **wholesale**, because a partially applied ledger
reconciles against data nobody reviewed; and the success banner **refuses to imply the
reconciliation updated**, because it did not — the engine must be re-run.

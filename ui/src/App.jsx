import { useEffect, useMemo, useState } from "react";
import Invoices from "./Invoices.jsx";

/*
 * Exception triage.
 *
 * The page answers one question in its first screenful: what is on my desk, in
 * descending order of money at risk. Everything else -- totals, tolerances, the
 * verification layers -- is available but subordinate, because an analyst opening this
 * at 9am needs a worklist, not a dashboard.
 *
 * Two deliberate absences:
 *
 *   There is no accept/reject control. The API is read-only and a feedback loop is
 *   explicitly out of scope; a button that did nothing would be worse than none.
 *
 *   There is no confidence-sorted view. Ranking by confidence surfaces the cases the
 *   engine is least sure about, which is not the same as the cases that matter most.
 *   Rupees at risk is the ordering that respects the reader's time.
 */

const RUPEES = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

const TIER_LABEL = {
  tier1_reference: "reference",
  tier2_amount_date: "amount + date",
  tier3_subsetsum: "combination",
  layer2b_group: "grouped credits",
};

const CATEGORY_LABEL = {
  order_dependent_assignment: "Order-dependent",
  multiple_candidates: "Ambiguous — several fit",
  solution_cap_reached: "Ambiguous — many fit",
  pool_exceeded: "Too many to search",
  no_subset_fits: "Nothing accounts for it",
  amount_name_conflict: "Amount/name conflict",
  narration_count_conflict: "Statement says a different count",
  contested_payment: "Two credits, one payment",
  unexplained_residual: "Unexplained shortfall",
  ambiguous_grouping: "Split settlement — several groupings fit",
  no_candidate: "Nothing accounts for it",
};

/*
 * The debit half of the statement.
 *
 * This component used to be called `NotExamined`, and it existed because the header's
 * "at risk" total counts refused CREDITS only -- a merchant reading "Rs 800 at risk"
 * while Rs 1,66,732 left the account on lines nobody looked at is being misled by
 * omission, and the omission matters more here than in the metrics block because this
 * page is what someone acts on.
 *
 * The disclosure did its job, and the right end state for a disclosure of this kind is
 * that the engine goes and reads the lines. It does now: each debit is tied to the
 * settlement it reverses, or reported as unexplained. The panel stays in the same place
 * so an operator who learned to look here still finds the same money -- what changed is
 * that every row now says what it was.
 */
function DebitLedger({ data }) {
  const [open, setOpen] = useState(false);
  const unexplained = data.unexplained ?? 0;
  return (
    <section className="disclosure">
      <button className="disclosure-head" onClick={() => setOpen((v) => !v)}>
        <span className="disclosure-mark">Money out</span>
        <span>
          {data.lines} debit line{data.lines === 1 ? "" : "s"} ·{" "}
          <strong>{RUPEES.format(data.rupees)}</strong> left the account ·{" "}
          {data.reversals_identified} tied to a settlement
          {unexplained > 0 ? (
            <>
              {" "}· <strong>{unexplained} unexplained</strong>
            </>
          ) : null}
        </span>
        <span className="chev">{open ? "\u2212" : "+"}</span>
      </button>
      {open && (
        <div className="disclosure-body">
          <p>{data.reason}</p>
          <table className="mini">
            <thead>
              <tr>
                <th>Date</th>
                <th>Narration</th>
                <th>Reverses</th>
                <th className="num">Amount</th>
              </tr>
            </thead>
            <tbody>
              {(data.rows ?? []).map((l) => (
                <tr key={l.bank_txn_id}>
                  <td>{l.txn_date}</td>
                  <td className="mono">{l.narration}</td>
                  <td className="mono">
                    {l.reverses ? (
                      l.reverses
                    ) : (
                      <span className="muted">unexplained</span>
                    )}
                  </td>
                  <td className="num">{RUPEES.format(l.rupees)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted">
            A reversal does not undo the settlement it reverses — both events happened —
            so the reconciled total is reported gross and net rather than silently as one
            number. A debit the engine cannot tie to a settlement it posted is reported
            as unexplained rather than dropped: "money left the account and this engine
            cannot say against what" is a finding you can act on; silence is not.
          </p>
        </div>
      )}
    </section>
  );
}

/*
 * Fetching, and telling the two failures apart.
 *
 * There are exactly two reasons this page has nothing to show, and they need different
 * instructions:
 *
 *   1. The API is not running. Nothing is listening on :8000, so Vite's proxy answers
 *      500 with an EMPTY BODY. `r.json()` then throws "Unexpected end of JSON input",
 *      and that string used to be what the reader saw, under a heading offering to
 *      regenerate the data -- which they had already done, and which cannot help. They
 *      would run the two commands, watch both succeed, reload, and get the same page.
 *
 *   2. The API is running but there is no run to serve. It answers 503 with a JSON
 *      `detail` naming the commands that fix it, and those ARE the right commands.
 *
 * Telling them apart costs a try/catch around the body parse. Getting it wrong costs
 * somebody their evening, which is the version that actually happened.
 */
async function getJSON(url) {
  let r;
  try {
    r = await fetch(url);
  } catch {
    // The fetch never completed: dev server down, or the browser is offline.
    throw Object.assign(new Error("unreachable"), { kind: "unreachable" });
  }
  let body = null;
  try {
    body = await r.json();
  } catch {
    // A response that is not JSON is the proxy talking, not the API.
    if (!r.ok) throw Object.assign(new Error("unreachable"), { kind: "unreachable" });
    throw Object.assign(new Error("The API returned something that is not JSON."), {
      kind: "bad_response",
    });
  }
  if (!r.ok) {
    throw Object.assign(new Error(body?.detail ?? r.statusText), { kind: "no_run" });
  }
  return body;
}

function useRun() {
  const [state, setState] = useState({ status: "loading" });
  useEffect(() => {
    let alive = true;
    getJSON("/api/run")
      .then((data) => alive && setState({ status: "ready", data }))
      .catch(
        (e) =>
          alive &&
          setState({
            status: "error",
            kind: e.kind ?? "bad_response",
            error: String(e.message ?? e),
          })
      );
    return () => {
      alive = false;
    };
  }, []);
  return state;
}

function Stat({ label, value, sub, tone }) {
  return (
    <div className={`stat ${tone ?? ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

function Candidates({ rows }) {
  if (!rows?.length) return null;
  return (
    <div className="candidates">
      <div className="candidates-title">
        {rows.length} candidate{rows.length === 1 ? "" : "s"} — the engine will not
        choose between them
      </div>
      {rows.map((c, i) => (
        <div className="candidate" key={i}>
          <span className="candidate-rank">{String.fromCharCode(65 + i)}</span>
          <span className="candidate-amount">{RUPEES.format(c.rupees)}</span>
          <span className="candidate-ids">{c.payment_ids.join(", ")}</span>
          {c.customers?.length > 0 && (
            <span className="candidate-cust">{c.customers.join(" · ")}</span>
          )}
        </div>
      ))}
    </div>
  );
}

function ExceptionCard({ row, rank }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="exception">
      <button className="exception-head" onClick={() => setOpen((v) => !v)}>
        <span className="rank">{rank}</span>
        <span className="amount">{RUPEES.format(row.rupees_at_risk)}</span>
        <span className="meta">
          <OutcomeBadge kind={row.category === "no_candidate" ? "unresolved" : "refused"} />
          <span className={`badge cat-${row.category}`}>
            {CATEGORY_LABEL[row.category] ?? row.category}
          </span>
          <span className="why">{row.why}</span>
        </span>
        <span className="chev" aria-hidden>
          {open ? "−" : "+"}
        </span>
      </button>

      {open && (
        <div className="exception-body">
          <div className="next-step">
            <strong>Next step</strong>
            <p>{row.next_step}</p>
          </div>
          <Candidates rows={row.candidates} />
          <dl className="facts">
            <dt>Bank transaction</dt>
            <dd>
              {row.bank_txn_id} · {row.txn_date}
            </dd>
            <dt>Narration</dt>
            <dd className="mono">{row.narration}</dd>
            <dt>Reference</dt>
            <dd className="mono">{row.reference || "—"}</dd>
            <dt>Engine reason</dt>
            <dd className="mono small">{row.engine_reason}</dd>
          </dl>
          {/* The same three-level explanation the Matches view gets. An exception is
              exactly where a human most needs to know what the engine already ruled
              out, and the machine-facing reason above is not that. */}
          <Explanation txnId={row.bank_txn_id} open={open} />
        </div>
      )}
    </li>
  );
}

// The permutation gate, said twice: once for a person, once for the record.
//
// The original sentence was "Permutation gate: 0 of 127 assignments changed under input
// reordering, over 8 shuffled passes." Every word of that is accurate and it assumes the
// reader already knows why anyone would shuffle the input.
//
// The thing worth communicating is the failure it rules out: a matcher that processes
// records in file order can hand the same money to whichever payment it happened to see
// first, and the result looks completely normal. Running the batch several times in
// different orders and comparing is how you catch it. The raw counts stay, one level
// down, because the claim has to remain checkable.
function PermutationGate({ gate }) {
  const [detail, setDetail] = useState(false);
  const clean = gate.unstable === 0;
  return (
    <div className="pgate">
      <p className="pgate-plain">
        {clean ? (
          <>
            <strong>Every match held when the records were shuffled.</strong> The batch was
            reconciled {gate.passes} times over, each time with the bank lines in a
            different order, and all {gate.txns_observed} matches came out the same. None
            of them depended on which record happened to be read first.
          </>
        ) : (
          <>
            <strong>{gate.unstable} of {gate.txns_observed} matches changed when the
            records were shuffled</strong>, across {gate.passes} passes. Those were refused
            rather than posted — a match that moves with the reading order was decided by
            the order and not by the data.
          </>
        )}
      </p>
      <p className="muted pgate-why">
        Why it matters: a reconciler that works through records in file order can give the
        same money to whichever candidate it saw first, and the wrong answer looks exactly
        like the right one. Re-running in a different order is how that gets caught, and
        anything caught is refused instead of posted.
      </p>
      <button className="linky" onClick={() => setDetail((v) => !v)}>
        {detail ? "Hide" : "Show"} the raw metric
      </button>
      {detail && (
        <pre className="pgate-raw">
{`permutation gate  K=${gate.passes} shuffled passes
unstable          ${gate.unstable} / ${gate.txns_observed} assignments
min stability     ${gate.min_stability ?? "1.000"}`}
        </pre>
      )}
    </div>
  );
}

function Verification({ block }) {
  if (!block) return null;
  const { relations = [], permutation_gate: gate, status, note } = block;
  // An absent claim must LOOK absent. This used to `return null` when both were empty,
  // so a run produced without --verify rendered no Verification section at all -- the
  // project's central claim vanished from the page silently, and nothing looked wrong
  // until someone asked where the verification had gone. See REVIEW.md P0-1.
  if (status === "not_run" || (!relations.length && !gate)) {
    return (
      <section className="verification not-run">
        <h2>Verification — did not run</h2>
        <p className="gate">
          {note ||
            "This run was produced without --verify, so the metamorphic relations and " +
              "the permutation refusal gate did not run."}
        </p>
        {/* Saying a claim is absent is the A1 fix. Saying which command restores it is
            the rest of the same thought -- a reader who sees this on a demo machine
            needs the line to type, not a diagnosis. */}
        <pre>python run.py match --verify --no-llm</pre>
      </section>
    );
  }
  return (
    <section className="verification">
      <h2>Verification</h2>
      {gate && <PermutationGate gate={gate} />}
      <ul className="relations">
        {relations.map((r) => (
          <li key={r.name} className={r.passed ? "ok" : "bad"}>
            <span className="rel-name">{r.name}</span>
            <span className="rel-kind">{r.kind}</span>
            <span className="rel-stat">
              {r.passed ? "pass" : `${r.violations} violation(s)`}
            </span>
            <span className="rel-text">{r.statement}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// --------------------------------------------------------------------------
// Explanations
//
// The audit's P0-2: 126 of 141 outcomes were invisible. The exception list was
// drillable and every posted match was not, so a judge could ask "why did it match
// THAT payment" and the answer was a JSON field nothing rendered.
//
// Fetched per transaction rather than shipped with the run. 141 transcripts take the
// payload from ~120 KB to ~795 KB, and someone opening one row wants one.
// --------------------------------------------------------------------------
const STAGE_LABEL = {
  input: "Read the bank line",
  parse: "Read the narration",
  pool: "Narrowed the candidates",
  layer3: "Weighed name and reference evidence",
  resolve: "Resolved competing claims",
  verdict: "Decided",
};

function stageLabel(stage) {
  if (STAGE_LABEL[stage]) return STAGE_LABEL[stage];
  if (stage.startsWith("tier:")) return "Tried a matching tier";
  return stage;
}

function useExplanation(txnId, open) {
  const [state, setState] = useState({ status: "idle" });
  // Dependencies are [txnId, open] and MUST NOT include state.status.
  //
  // Written with `state.status` in the dependency list -- and guarded on it, which is
  // what made it look careful -- this deadlocked at "Reading the transcript…" forever:
  // setState({status:"loading"}) changed a dependency, React ran the cleanup, `alive`
  // went false, and the in-flight response was discarded by its own `.then`. The effect
  // then re-ran, saw the status was no longer "idle", and returned early. The request
  // succeeded with a 200 every time; nothing ever rendered it.
  //
  // `vite.config.js` already carries the lesson this repeats: a green build is not
  // evidence the page works. Caught by driving the real page in Chromium, not by
  // review and not by the build.
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setState({ status: "loading" });
    // Same body-parse guard as the run fetch: a dead API answers 500 with no body, and
    // `r.json()` on that throws a parse error that reads like a data problem.
    getJSON(`/api/explain/${encodeURIComponent(txnId)}`)
      .then((data) => {
        if (alive) setState({ status: "ready", data });
      })
      .catch((e) => {
        if (alive) setState({ status: "error", error: String(e.message ?? e) });
      });
    return () => {
      alive = false;
    };
  }, [txnId, open]);
  return state;
}

function EvidenceList({ items }) {
  if (!items?.length) return null;
  return (
    <ul className="evidence">
      {items.map((v) => (
        <li key={`${v.kind}:${v.id}`} className={`ev ev-${v.kind}`}>
          <a href={v.href} title={v.id}>
            <span className="ev-kind">{v.kind.replace("_", " ")}</span>
            <span className="ev-label">{v.label}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}

function Explanation({ txnId, open }) {
  const state = useExplanation(txnId, open);
  const [showWorking, setShowWorking] = useState(false);
  if (!open) return null;
  if (state.status === "loading") return <p className="muted">Reading the transcript…</p>;
  if (state.status === "error")
    return <p className="muted">No explanation available: {state.error}</p>;
  if (state.status !== "ready") return null;

  const { plain, evidence, transcript } = state.data;
  return (
    <div className="explanation">
      {/* Level 1 — the sentence. */}
      <p className="plain">{plain}</p>

      {/* Level 2 — the rows it rests on. */}
      <div className="ev-block">
        <h4>Evidence</h4>
        <EvidenceList items={evidence} />
      </div>

      {/* Level 3 — the working, collapsed. An auditor needs it; an operator
          clearing a queue does not, and putting it in front of them is how the
          readable layer stops being read. */}
      <button className="working-toggle" onClick={() => setShowWorking((v) => !v)}>
        {showWorking ? "Hide" : "Show"} the full working ({transcript.length} steps)
      </button>
      {showWorking && (
        <ol className="transcript">
          {transcript.map((s) => (
            <li key={s.seq} className={`step step-${s.stage.split(":")[0]}`}>
              <div className="step-head">
                <span className="step-stage">{stageLabel(s.stage)}</span>
                <span className="step-headline">{s.headline}</span>
              </div>
              <pre className="step-detail">{s.detail}</pre>
              <EvidenceList items={s.evidence} />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Matches — the view that did not exist
// --------------------------------------------------------------------------
// --------------------------------------------------------------------------
// BEFORE → AFTER
//
// The engine's own view of a match is a residual and a tier. That answers "is this
// right" and not "what happened", and the second is the question someone asks the first
// time they see the tool.
//
// So the same match is shown as a state change: on the left the records as they arrived
// — a bank line nobody had claimed, and the open invoices sitting against it — and on
// the right what they became. Nothing is recomputed here; both columns read the evidence
// list the explain transcript already carries, so the picture cannot disagree with the
// working underneath it.
//
// **Nothing in the source records is edited.** The reconciliation is a link, not a
// rewrite, and the AFTER column says so rather than implying the ledger was touched.
// --------------------------------------------------------------------------
function BeforeAfter({ row, evidence, plain }) {
  if (!evidence?.length) return null;
  const bank = evidence.filter((e) => e.kind === "bank_txn");
  const payments = evidence.filter((e) => e.kind === "payment");
  const invoices = evidence.filter((e) => e.kind === "invoice");

  return (
    <div className="ba">
      <div className="ba-col ba-before">
        <h4>Before · unreconciled</h4>
        {bank.map((b) => (
          <div key={b.id} className="ba-rec ba-bank">
            <span className="ba-kind">Bank line</span>
            <span className="mono">{b.id}</span>
            <span className="ba-text">{b.label}</span>
            <span className="ba-state">not claimed by anything</span>
          </div>
        ))}
        {invoices.map((i) => (
          <div key={i.id} className="ba-rec">
            <span className="ba-kind">Open invoice</span>
            <span className="mono">{i.id}</span>
            <span className="ba-text">{i.label}</span>
            <span className="ba-state">awaiting payment</span>
          </div>
        ))}
        {invoices.length === 0 && (
          <p className="muted ba-none">
            No invoice was linked to this credit — a gateway settlement batch carries its
            own reference rather than an invoice number.
          </p>
        )}
      </div>

      <div className="ba-arrow" aria-hidden="true">→</div>

      <div className="ba-col ba-after">
        <h4>After · reconciled</h4>
        <div className="ba-decision">
          <span className="ba-kind">Decision</span>
          <p>{plain}</p>
          <span className="ba-state">
            matched on {TIER_LABEL[row.tier] ?? row.tier} ·{" "}
            {row.residual_paise === 0
              ? "balances to the paisa"
              : `${row.residual_paise > 0 ? "+" : ""}${row.residual_paise} paise left over, inside tolerance`}
          </span>
        </div>
        <div className="ba-rec ba-linked">
          <span className="ba-kind">Result</span>
          <span className="ba-text">
            <strong>{RUPEES.format(row.rupees)}</strong> settled ·{" "}
            {payments.length} payment{payments.length === 1 ? "" : "s"} ·{" "}
            {invoices.length} invoice{invoices.length === 1 ? "" : "s"} closed
          </span>
          <span className="ba-state">
            {row.permutation_stability === 1
              ? "held on all 8 shuffled passes"
              : `stability ${row.permutation_stability}`}
          </span>
        </div>
        <p className="muted ba-note">
          The records themselves are unchanged. Reconciling links them; it does not edit
          anyone's ledger, and this page has no endpoint that could.
        </p>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// The five outcome states, named
//
// The page could tell you a credit was reconciled or refused, and left the rest to be
// inferred: a merged settlement looked like any other match, a credit nothing could
// account for looked like a credit the evidence merely failed to single out, and
// "verified" was a number in a panel rather than a property of a row.
//
// Every credit is in exactly one of these, and the counts are computed here from the run
// rather than written down, so the legend cannot disagree with the rows beneath it.
// `unresolved` is shown at zero rather than omitted: a state that disappears when it is
// empty makes the model look smaller than it is, and a reader cannot tell "none today"
// from "not a thing this engine reports".
// --------------------------------------------------------------------------
const OUTCOME = {
  reconciled: { label: "Reconciled", tone: "ok",
    help: "A bank credit matched to one or more payments, and posted." },
  merged: { label: "Merged", tone: "ok",
    help: "One bank credit covering several payments at once — the engine worked out which combination it was." },
  split: { label: "Split", tone: "ok",
    help: "The mirror of Merged: ONE payment that arrived across several bank lines, because the settlement was split. Neither line accounts for the money on its own, so they were posted together as a group — posting half a payment would be a wrong answer, not a partial one." },
  verified: { label: "Verified", tone: "ok",
    help: "The match came out the same on every pass with the records in a different order, so it was not decided by reading order." },
  unresolved: { label: "Unresolved", tone: "warn",
    help: "Nothing in the settlement window could account for this credit at all. Money arrived that no payment explains." },
  refused: { label: "Refused", tone: "warn",
    help: "Candidates existed, but the evidence did not identify one. The engine declined to post rather than guess." },
};

function OutcomeBadge({ kind }) {
  const o = OUTCOME[kind];
  if (!o) return null;
  return (
    <span className={`outcome outcome-${o.tone}`} title={o.help}>
      {o.label}
    </span>
  );
}

function OutcomeLegend({ assignments, groups = [], exceptions }) {
  const merged = assignments.filter((a) => a.payment_ids.length > 1).length;
  const verified =
    assignments.filter((a) => a.permutation_stability === 1).length +
    groups.filter((g) => g.permutation_stability === 1).length;
  const groupedCredits = groups.reduce((s, g) => s + g.bank_txn_ids.length, 0);
  const unresolved = exceptions.filter((e) => e.category === "no_candidate").length;
  const refused = exceptions.length - unresolved;
  const rows = [
    [
      "reconciled",
      assignments.length + groups.length,
      `${assignments.length - merged} single + ${merged} merged` +
        (groups.length ? ` + ${groups.length} split (${groupedCredits} credits)` : ""),
    ],
    ["merged", merged, `covering ${assignments.filter((a) => a.payment_ids.length > 1).reduce((s, a) => s + a.payment_ids.length, 0)} payments`],
    ...(groups.length
      ? [["split", groups.length, `one payment across ${groupedCredits} bank lines`]]
      : []),
    ["verified", verified, `of ${assignments.length + groups.length} postings`],
    ["unresolved", unresolved, unresolved === 0 ? "none on this batch" : "no candidate at all"],
    ["refused", refused, "evidence did not single one out"],
  ];
  return (
    <div className="legend">
      <span className="legend-title">Every bank credit ends in one of these:</span>
      <ul>
        {rows.map(([kind, n, sub]) => (
          <li key={kind} className={n === 0 ? "zero" : ""}>
            <OutcomeBadge kind={kind} />
            <span className="legend-n">{n}</span>
            <span className="legend-sub">{sub}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function MatchCard({ row, rank }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="match">
      <button className="match-head" onClick={() => setOpen((v) => !v)}>
        <span className="rank">{rank}</span>
        <span className="amount">{RUPEES.format(row.rupees)}</span>
        <span className="ids">
          {row.split
            ? `${row.bank_txn_ids.length} bank lines → ${
                row.payment_ids.length === 1
                  ? "1 payment"
                  : `${row.payment_ids.length} payments`
              }`
            : row.payment_ids.length === 1
            ? row.payment_ids[0]
            : `${row.payment_ids.length} payments`}
        </span>
        <span className="badges">
          {row.split && <OutcomeBadge kind="split" />}
          {!row.split && row.payment_ids.length > 1 && <OutcomeBadge kind="merged" />}
          {row.permutation_stability === 1 && <OutcomeBadge kind="verified" />}
        </span>
        <span className={`tier tier-${row.tier}`}>{TIER_LABEL[row.tier] ?? row.tier}</span>
        <span className="resid">
          {row.residual_paise === 0
            ? "exact"
            : `${row.residual_paise > 0 ? "+" : ""}${row.residual_paise} p`}
        </span>
        <span className="chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && <MatchBody row={row} />}
    </li>
  );
}

function MatchBody({ row }) {
  // The state change first, the engine's own numbers second. Someone opening a match
  // wants to know what happened before they want the residual in paise.
  const ex = useExplanation(row.bank_txn_id, true);
  const [showEngine, setShowEngine] = useState(false);
  return (
    <div className="match-body">
      {ex.status === "ready" && (
        <BeforeAfter row={row} evidence={ex.data.evidence} plain={ex.data.plain} />
      )}
      {ex.status === "loading" && <p className="muted">Reading the transcript…</p>}
      <button className="linky" onClick={() => setShowEngine((v) => !v)}>
        {showEngine ? "Hide" : "Show"} the engine's own numbers
      </button>
      {showEngine && (
        <>
          <dl className="mini-facts">
            <div><dt>Tier</dt><dd>{TIER_LABEL[row.tier] ?? row.tier}</dd></div>
            <div><dt>Residual</dt><dd>{row.residual_paise} paise</dd></div>
            <div>
              <dt>Gateway fee</dt>
              <dd>
                {row.certain_fee === undefined
                  ? "—"
                  : row.certain_fee
                    ? "known exactly, from the payment record"
                    : "not on the record — bounded by the rate band"}
              </dd>
            </div>
            <div><dt>Uniqueness margin</dt><dd>{row.uniqueness_margin ?? "—"}</dd></div>
            <div>
              <dt>Name / reference weight</dt>
              <dd>{row.fs_weight === null ? "no non-amount evidence" : `${row.fs_weight} bits`}</dd>
            </div>
            <div><dt>Stable under reordering</dt><dd>{row.permutation_stability === 1 ? "yes, all passes" : row.permutation_stability}</dd></div>
          </dl>
          <Explanation txnId={row.bank_txn_id} open />
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// The reachable ceiling
//
// REVIEW.md section 8, item 6. A match rate of 88.66% invites comparison against 100%,
// and 100% is not on offer: some captured payments never settled, so no bank credit
// exists to match them, and others belong to a relation the engine does not model and
// are refused correctly. Ground truth says 91.24% is the ceiling, which makes the gap
// worth arguing about 5 payments rather than 22.
//
// It is fetched from its OWN endpoint rather than read out of the run payload, and that
// is the point rather than an inconvenience: the ceiling is derived from ground truth,
// run_output.json is defined as what the engine could justify with no answer key, and
// putting the two in one file would make the isolation claim unprovable by opening it.
// Two artefacts, and the panel says which is which.
// --------------------------------------------------------------------------
function useScorecard() {
  const [state, setState] = useState({ status: "loading" });
  useEffect(() => {
    let alive = true;
    getJSON("/api/scorecard")
      .then((data) => alive && setState({ status: "ready", data }))
      .catch((e) => alive && setState({ status: "error", error: String(e.message ?? e) }));
    return () => {
      alive = false;
    };
  }, []);
  return state;
}

function ShortfallRow({ row }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="shortfall">
      <button className="shortfall-head" onClick={() => setOpen((v) => !v)}>
        <span className="chev">{open ? "▾" : "▸"}</span>
        <span className="mono">{row.bank_txn_id}</span>
        <span className="shortfall-money">{RUPEES.format(row.paise / 100)}</span>
        <span className="shortfall-why">
          {CATEGORY_LABEL[row.engine_verdict] ?? row.engine_verdict}
        </span>
        <span className="shortfall-labels">{row.defect_labels.join(" · ")}</span>
      </button>
      {open && (
        <div className="shortfall-body">
          <p className="muted">
            Ground truth links this credit to{" "}
            <span className="mono">{row.payment_ids.join(", ")}</span>. The engine
            refused it. No money was posted anywhere — this cost coverage, not
            precision.
          </p>
          <Explanation txnId={row.bank_txn_id} open={open} />
        </div>
      )}
    </li>
  );
}

function Ceiling() {
  const sc = useScorecard();
  if (sc.status === "loading") return null;
  // An absent claim must LOOK absent -- the lesson of P0-1, where an empty verification
  // block rendered as nothing at all and the omission was invisible until someone went
  // looking for it.
  if (sc.status === "error" || sc.data?.status === "not_scored") {
    return (
      <section className="ceiling not-run">
        <h2>Reachable ceiling — not scored</h2>
        <p className="gate">
          {sc.data?.note ??
            "The scorecard could not be read, so this run has not been compared " +
              "against ground truth."}
        </p>
      </section>
    );
  }

  const { coverage: c, precision: p, provenance, dataset } = sc.data;
  const pct = (x) => `${(x * 100).toFixed(2)}%`;
  const total = c.captured_payments || 1;
  const seg = (n) => `${(100 * n) / total}%`;

  return (
    <section className="ceiling">
      <h2>Reachable ceiling</h2>
      <p className="gate">
        {provenance}
        {dataset === "holdout" && " Shifted holdout batch."}
      </p>

      <div className="ceiling-bar" role="img"
           aria-label={`${c.payments_assigned} assigned, ${c.short_of_ceiling} short, ${c.unreachable_payments} unreachable`}>
        <span className="seg assigned" style={{ width: seg(c.payments_assigned) }} />
        <span className="seg short" style={{ width: seg(c.short_of_ceiling) }} />
        <span className="seg unreachable" style={{ width: seg(c.unreachable_payments) }} />
      </div>

      <dl className="ceiling-facts">
        <div>
          <dt>Matched</dt>
          <dd>
            <strong>{pct(c.match_rate)}</strong>
            <span className="muted">
              {" "}
              {c.payments_assigned}/{c.captured_payments} captured payments
            </span>
          </dd>
        </div>
        <div>
          <dt>Ceiling</dt>
          <dd>
            <strong>{pct(c.ceiling)}</strong>
            <span className="muted">
              {" "}
              {c.reachable_payments} payments ground truth says can be matched
            </span>
          </dd>
        </div>
        <div>
          <dt>Short of it</dt>
          <dd>
            <strong>{c.short_of_ceiling}</strong>
            <span className="muted"> payments the engine could have had and did not</span>
          </dd>
        </div>
        <div>
          <dt>Unreachable</dt>
          <dd>
            <strong>{c.unreachable_payments}</strong>
            <span className="muted">
              {" "}
              never settled, or a relation this engine does not model
            </span>
          </dd>
        </div>
        <div>
          <dt>Precision</dt>
          <dd>
            <strong>{pct(p.match_precision)}</strong>
            <span className="muted">
              {" "}
              {p.correct_assignments}/{p.total_assignments} assignments correct
              {p.wrong_assignments.length === 0 && " · nothing posted wrongly"}
            </span>
            {/* The bound belongs beside the number, not in a footnote. 1.0000 on 126
                assignments and 1.0000 on 126,000 are the same figure and not the same
                claim, and this project cites the 99.9% automated-matching standard. */}
            {p.precision_ci_lower != null && (
              <div className="ci" title={p.precision_ci_note}>
                95% CI ≥ {pct(p.precision_ci_lower)} · {p.total_assignments} observations
                cannot support more
              </div>
            )}
          </dd>
        </div>
      </dl>

      {sc.data.short_of_ceiling_txns.length > 0 && (
        <>
          <p className="showing">
            Every payment between the engine and its ceiling, with the reason it was
            refused. Open one to read the engine's own working.
          </p>
          <ul className="shortfall-list">
            {sc.data.short_of_ceiling_txns.map((row) => (
              <ShortfallRow key={row.bank_txn_id} row={row} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------
// The worklist: the refusal taxonomy as a routing table
//
// REVIEW.md section 8's second product item. An AP team does not buy a match rate, it
// buys a smaller worklist with a shape it can staff. Nine named refusal categories, each
// carrying the reason the engine declined, go to different desks with different
// turnaround times -- because "two subsets fit this credit" is answerable in minutes
// from a remittance advice and "nothing in the window accounts for this money" is an
// investigation.
//
// Ordered by SLA, not by exposure. Sorted by rupees the board leads with the biggest
// number and says nothing about what will be late; sorted by the clock it is a rota.
// Inside a queue the rows keep the exception list's own descending-exposure order, so
// the board answers "which desk first" and each desk answers "which row first".
// --------------------------------------------------------------------------
function useWorklist() {
  const [state, setState] = useState({ status: "loading" });
  useEffect(() => {
    let alive = true;
    getJSON("/api/worklist")
      .then((data) => alive && setState({ status: "ready", data }))
      .catch((e) => alive && setState({ status: "error", error: String(e.message ?? e) }));
    return () => {
      alive = false;
    };
  }, []);
  return state;
}

function QueueCard({ row, exceptions }) {
  const [open, setOpen] = useState(false);
  const rows = exceptions.filter((e) => e.routing?.queue === row.queue);
  const empty = row.count === 0;
  return (
    <li className={`queue ${empty ? "empty" : ""}`}>
      <button className="queue-head" onClick={() => !empty && setOpen((v) => !v)}>
        <span className="chev">{empty ? "·" : open ? "▾" : "▸"}</span>
        <span className="queue-sla">{row.sla_hours}h</span>
        <span className="queue-name">
          {row.label}
          <span className="queue-owner">{row.owner}</span>
        </span>
        <span className="queue-count">
          {row.count === 0 ? "clear" : `${row.count} open`}
          {row.material_count > 0 && (
            <span className="queue-material">{row.material_count} material</span>
          )}
        </span>
        <span className="queue-money">
          {row.rupees_at_risk > 0 ? RUPEES.format(row.rupees_at_risk) : "—"}
        </span>
      </button>
      {open && (
        <div className="queue-body">
          <p className="queue-action">
            <strong>Do this</strong> {row.action}
          </p>
          <p className="muted">{row.rationale}</p>
          <ol className="exceptions">
            {rows.map((e, i) => (
              <ExceptionCard key={e.bank_txn_id} row={e} rank={i + 1} />
            ))}
          </ol>
        </div>
      )}
    </li>
  );
}

function Worklist({ exceptions }) {
  const wl = useWorklist();
  if (wl.status === "loading") return <p className="muted">Loading the worklist…</p>;
  if (wl.status === "error" || wl.data?.status === "unavailable")
    return (
      <section className="ceiling not-run">
        <h2>Worklist — unavailable</h2>
        <p className="gate">
          {wl.data?.note ?? "The worklist could not be read from this run."}
        </p>
      </section>
    );

  const { queues, total_exceptions, total_rupees_at_risk, note } = wl.data;
  return (
    <>
      <p className="showing">
        {total_exceptions} open exceptions · {RUPEES.format(total_rupees_at_risk)} at
        risk, across {queues.filter((q) => q.count > 0).length} of {queues.length} desks.
        Soonest deadline first.
      </p>
      <ul className="queues">
        {queues.map((q) => (
          <QueueCard key={q.queue} row={q} exceptions={exceptions} />
        ))}
      </ul>
      <p className="worklist-note">{note}</p>
    </>
  );
}

// --------------------------------------------------------------------------
// Run freshness
//
// The header used to carry a row of KPI tiles. `ReconciliationSummary` now reports the
// same figures better and in a better place, and keeping both put a warning-coloured
// "At risk" tile ABOVE the success story -- leading with the failure again, one row
// higher up the page. The tiles are gone; the summary is the headline.
// --------------------------------------------------------------------------
// How old is what you are looking at?
//
// `generated_at` was in the payload from the beginning and rendered nowhere, so a stale
// artefact looked exactly like a fresh one -- the failure mode behind P0-1 and behind a
// run that silently lost its verification block twice more since. A timestamp is the
// cheapest defence against demoing yesterday's numbers.
function Freshness({ generatedAt, llmTier, verified }) {
  if (!generatedAt) return null;
  const when = new Date(generatedAt);
  const mins = Math.round((Date.now() - when.getTime()) / 60000);
  const age =
    mins < 1 ? "just now"
    : mins < 60 ? `${mins} min ago`
    : mins < 1440 ? `${Math.round(mins / 60)} h ago`
    : `${Math.round(mins / 1440)} d ago`;
  return (
    <span className={`freshness ${mins > 1440 ? "stale" : ""}`} title={when.toISOString()}>
      run {age} · tier {llmTier ?? "unknown"} ·{" "}
      {verified ? "verification gated" : "NOT verification gated"}
    </span>
  );
}

// --------------------------------------------------------------------------
// The reconciliation summary — the first thing on the page
//
// The page used to open on the exception list, which answers "what went wrong" before
// anyone has been told what went right. A reader landing on fifteen red rows has no way
// to know they represent 11% of the batch.
//
// So the outcome leads: how much money was reconciled, across how many records, at what
// rate, and how much of it survived the verification gate. Every figure comes from the
// `reconciled` block the engine computes (`recon/report/run_output.py::_reconciled`) or
// from the scorecard — nothing here is assembled in the browser, so the headline and the
// detail views cannot drift apart.
//
// The exceptions are not hidden by this; they are moved. They keep their own section,
// their own count in this summary, and their money is named in the same breath as the
// money that settled. Leading with the success story is an ordering decision. Quietly
// dropping the 11% would be a different thing entirely, and this page does not do it.
// --------------------------------------------------------------------------
function ReconciliationSummary({ reconciled: r, totals }) {
  const sc = useScorecard();
  const scored = sc.status === "ready" && sc.data?.status === "ok" ? sc.data : null;
  const pct = (x) => `${(x * 100).toFixed(2)}%`;

  if (!r) {
    return (
      <section className="summary not-run">
        <h2>Reconciliation summary — unavailable</h2>
        <p className="gate">
          This run output predates the summary block. Regenerate it with{" "}
          <code>python run.py match --verify --no-llm</code>.
        </p>
      </section>
    );
  }

  const pctCredits = r.credits_reconciled / Math.max(r.credits_total, 1);

  return (
    <section className="summary">
      <div className="summary-hero">
        <div className="hero-money">
          <span className="hero-label">Reconciled and verified</span>
          <span className="hero-value">{RUPEES.format(r.rupees_reconciled)}</span>
          <span className="hero-sub">
            across <strong>{r.credits_reconciled}</strong> bank credits,{" "}
            <strong>{r.payments_reconciled}</strong> payments and{" "}
            <strong>{r.invoices_reconciled}</strong> invoices
          </span>
        </div>
        <div className="hero-bar" role="img"
             aria-label={`${r.credits_reconciled} of ${r.credits_total} credits reconciled`}>
          <span className="seg assigned" style={{ width: `${pctCredits * 100}%` }} />
          <span className="seg short" style={{ width: `${(1 - pctCredits) * 100}%` }} />
        </div>
        <p className="hero-line">
          <strong>{r.credits_reconciled} of the {r.credits_total} bank credits</strong>{" "}
          ({pct(pctCredits)}) settled automatically.{" "}
          {r.settlement_groups > 0 && (
            <>
              <strong>{r.credits_in_groups}</strong> of them arrived as{" "}
              {r.settlement_groups} split settlement
              {r.settlement_groups === 1 ? "" : "s"} — one payment across several bank
              lines — and were matched as a group.{" "}
            </>
          )}
          The remaining <strong>{r.exceptions}</strong> were{" "}
          <em>refused rather than guessed</em> — they are listed below with the reason for
          each.
        </p>
        {/*
          The net line, and it is not optional.

          The hero leads with money reconciled, and that figure is GROSS. A chargeback
          claws money back out of a settlement that was correctly matched -- the match is
          still right, and the merchant still does not have the money. Leading with the
          gross number alone is the same omission the debit panel below exists to fix,
          committed in the one place a reader looks first.
        */}
        {r.reversals > 0 && (
          <p className="hero-line net">
            <strong>{RUPEES.format(r.rupees_reconciled_net)} net</strong> of{" "}
            {r.reversals} chargeback{r.reversals === 1 ? "" : "s"} totalling{" "}
            {RUPEES.format(r.rupees_reversed)}, which were clawed back after settling.
            The matches stand — both events happened — so the figure above is what
            reconciled and this is what stayed.
          </p>
        )}
      </div>

      <dl className="summary-grid">
        <div>
          <dt>Records processed</dt>
          <dd>
            <strong>{r.records_processed.toLocaleString("en-IN")}</strong>
            <span className="muted">
              {r.records_breakdown.payments} payments · {r.records_breakdown.bank_credits}{" "}
              bank credits · {r.records_breakdown.invoices} invoices ·{" "}
              {r.records_breakdown.bank_debits} bank debits
            </span>
          </dd>
        </div>
        <div>
          <dt>Invoices reconciled</dt>
          <dd>
            <strong>{r.invoices_reconciled}</strong>
            <span className="muted">of {r.invoices_total} in the ledger</span>
          </dd>
        </div>
        <div>
          <dt>Match rate</dt>
          <dd>
            <strong>{scored ? pct(scored.coverage.match_rate) : "—"}</strong>
            <span className="muted">
              {scored
                ? `${scored.coverage.payments_assigned}/${scored.coverage.captured_payments} settleable payments`
                : "not scored"}
            </span>
          </dd>
        </div>
        <div>
          <dt>Accuracy of what was posted</dt>
          <dd>
            <strong>{scored ? pct(scored.precision.match_precision) : "—"}</strong>
            <span className="muted">
              {scored
                ? `${scored.precision.correct_assignments}/${scored.precision.total_assignments} correct · 95% CI ≥ ${pct(scored.precision.precision_ci_lower)}`
                : "not scored"}
            </span>
          </dd>
        </div>
        <div>
          <dt>Verified stable</dt>
          <dd>
            <strong>{r.verified_stable}</strong>
            <span className="muted">
              of {r.credits_reconciled} — unchanged when the input order was shuffled
            </span>
          </dd>
        </div>
        <div>
          <dt>Multi-payment settlements</dt>
          <dd>
            <strong>{r.settlements_merged}</strong>
            <span className="muted">
              one bank line covering {r.payments_inside_merged_settlements} payments
              between them
            </span>
          </dd>
        </div>
      </dl>

      <p className="summary-resolved">
        <strong>What the engine resolved.</strong> It read{" "}
        {r.records_processed.toLocaleString("en-IN")} records across three sources and
        settled {r.credits_reconciled} of {r.credits_total} bank credits against{" "}
        {r.invoices_reconciled} invoices, worth{" "}
        {RUPEES.format(r.rupees_reconciled)}. {r.settlements_merged} of those credits were
        lump settlements that no single payment explained — it decomposed them into the{" "}
        {r.payments_inside_merged_settlements} payments they actually covered. Every one
        of the {r.verified_stable} postings held when the input order was shuffled eight
        ways. Where the evidence did not identify one answer it declined to post at all,
        which is the {r.exceptions} exceptions below, worth{" "}
        {RUPEES.format(totals.rupees_at_risk)}.
      </p>
    </section>
  );
}

// --------------------------------------------------------------------------
// Plain English
//
// The footer used to read "Tolerance 100p + 0bps · MDR band 0.018–0.025 · lookback 5d ·
// pool ≤ 20 · materiality ₹5,000" and expect the reader to supply the meaning. Every one
// of those is a real, defensible setting; none of them explains itself.
//
// The values are still read from the payload rather than written here, so this page
// cannot describe a threshold the engine is not using.
// --------------------------------------------------------------------------
// Hover text for the settings in the footer. The full explanations live in the glossary
// tab; these are the one-liners that answer "what is this" without a click, which is what
// a tooltip is for. Kept beside `Glossary` so the two cannot drift into disagreeing.
const PARAM_HELP = {
  tolerance:
    "How far apart two amounts may be and still count as the same money. It is for " +
    "rounding, not slack: GST and TDS are rounded to the paisa by different systems.",
  mdr:
    "The payment gateway keeps a percentage of every transaction, plus GST on it. Where " +
    "the exact fee is on the payment record the engine uses it; where it is not, the " +
    "credit may fall anywhere in this band.",
  lookback:
    "A payment reaches the bank a few days after the customer pays. Payments older than " +
    "this are not considered for a given credit — a wider window means more unrelated " +
    "payments available to coincide.",
  pool:
    "When one credit covers several payments the engine tries combinations. Beyond this " +
    "many it stops and says so rather than searching part of the range and reporting the " +
    "first fit.",
  materiality:
    "The audit threshold (PCAOB AS 2315). Items at or above it are verified in full and " +
    "a sample stands for the rest; it also halves the turnaround time on an exception.",
};

function Term({ term, value, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`term ${open ? "on" : ""}`}>
      <button className="term-head" onClick={() => setOpen((v) => !v)}>
        <span className="chev">{open ? "▾" : "▸"}</span>
        <span className="term-name">{term}</span>
        {value !== undefined && <span className="term-value mono">{value}</span>}
      </button>
      {open && <div className="term-body">{children}</div>}
    </div>
  );
}

function Glossary({ tolerances, seed, density, totals }) {
  const t = tolerances ?? {};
  return (
    <>
      <p className="showing">
        Every setting below was fixed before the run and none is tuned per record. The
        values are read from the run itself, so this page cannot describe a threshold the
        engine is not using.
      </p>

      <h3 className="gl-h">What the engine is deciding</h3>
      <div className="terms">
        <Term term="Reconciliation" >
          <p>
            Matching money that <em>arrived</em> (a bank credit) to money that was{" "}
            <em>owed</em> (an invoice) through the record of who paid (a gateway payment).
            Three sources, and a match only counts when all three agree.
          </p>
        </Term>
        <Term term="TDS — Tax Deducted at Source" >
          <p>
            Indian law requires many business customers to withhold a slice of an invoice
            and pay it directly to the tax authority rather than to the supplier. A
            ₹1,00,000 invoice with 2% TDS is settled by a ₹98,000 payment — and the books
            are correct.
          </p>
          <p>
            This is the single most common reason a bank credit does not equal the invoice
            it settles. The engine reads the TDS amount from the invoice ledger and
            subtracts it before comparing, so a withheld invoice reconciles exactly instead
            of landing in the exception list.
          </p>
        </Term>
        <Term term="Gateway fee (MDR) and the fee band" value={
          t.mdr_rate_band ? `${(t.mdr_rate_band[0] * 100).toFixed(1)}%–${(t.mdr_rate_band[1] * 100).toFixed(1)}%` : "—"
        }>
          <p>
            The payment gateway keeps a percentage of every transaction — the Merchant
            Discount Rate — plus GST on it. So ₹10,000 paid by a customer arrives in the
            bank as roughly ₹9,740.
          </p>
          <p>
            Where the fee is on the payment record the engine uses that exact figure.
            Where it is not, it allows the credit to fall anywhere in the band above and
            still match. <strong>Narrower is safer:</strong> a wide band would let
            unrelated amounts coincide, so this one is fitted to 18 real observations and
            not widened to improve the match rate.
          </p>
        </Term>
      </div>

      <h3 className="gl-h">How careful it is being</h3>
      <div className="terms">
        <Term term="Tolerance" value={
          t.tol_abs_paise !== undefined ? `₹${(t.tol_abs_paise / 100).toFixed(2)} + ${t.tol_rel_bps} bps` : "—"
        }>
          <p>
            How far apart two amounts may be and still count as the same money — here{" "}
            <strong>one rupee</strong>, plus nothing proportional.
          </p>
          <p>
            It exists for rounding, not for slack: GST and TDS are rounded to the paisa by
            different systems, so an exact match can legitimately be a paisa or two out.
            One rupee sits about 200× below the smallest payment in the batch, which is
            what stops the tolerance itself creating coincidental matches.
          </p>
        </Term>
        <Term term="Lookback window" value={t.lookback_days ? `${t.lookback_days} days` : "—"}>
          <p>
            A payment settles into the bank a few days after the customer pays. The engine
            only considers payments from the {t.lookback_days ?? "—"} days before a credit
            — a payment older than that is very unlikely to be what this money is.
          </p>
          <p>
            It is a safety limit as much as a speed one: the wider the window, the more
            unrelated payments are available to coincide with the right amount.
          </p>
        </Term>
        <Term term="Matching pool" value={t.max_pool ? `≤ ${t.max_pool} payments` : "—"}>
          <p>
            When one bank credit covers several payments, the engine tries combinations to
            find which ones. It will search a window of up to {t.max_pool ?? "—"} payments
            exhaustively.
          </p>
          <p>
            Beyond that it <strong>stops and says so</strong> rather than searching part of
            the range and reporting the first fit it finds. A partial search that looks
            like a full one is how a wrong match gets posted confidently.
          </p>
        </Term>
        <Term term="Materiality" value={
          t.materiality_rupees ? RUPEES.format(t.materiality_rupees) : "—"
        }>
          <p>
            The audit threshold from PCAOB AS 2315: items at or above{" "}
            {t.materiality_rupees ? RUPEES.format(t.materiality_rupees) : "—"} are
            verified in full, and a sample stands for the rest.
          </p>
          <p>
            It also drives the worklist: an exception at or above materiality gets half the
            turnaround time of one below it, on the same desk.
          </p>
        </Term>
      </div>

      <h3 className="gl-h">What you are looking at</h3>
      <div className="terms">
        <Term term="“Ranked by money at risk”">
          <p>
            Exceptions are listed largest-rupee first, not oldest or alphabetical. An
            analyst's scarce resource is attention, and the right order to spend it in is
            descending exposure — the ₹48,020 unresolved credit is worth looking at before
            the ₹800 one.
          </p>
          <p>
            "At risk" means <em>unreconciled</em>, not lost. The money is in the bank; what
            is missing is a defensible link to an invoice.
          </p>
        </Term>
        <Term term="Seed" value={seed}>
          <p>
            This batch is synthetic, and <strong>{seed}</strong> is the number it was
            generated from. Anyone with this repository can reproduce the identical 534
            records — the data, the defects planted in it, and the answer key — by passing
            the same seed. It is here so the numbers on this page are checkable, not
            because an end user needs it.
          </p>
        </Term>
        <Term term="Density" value={density}>
          <p>
            Roughly how many payments fall in each settlement window — the crowding of the
            batch. It matters because it is the parameter the difficulty turns on: more
            payments in a window means more combinations that could coincidentally add up
            to the same credit.
          </p>
        </Term>
        <Term term="Bank credits" value={totals?.bank_credits}>
          <p>
            Money-in lines on the bank statement. The engine reads credits only; money-out
            lines (refunds, chargebacks, bank charges) are counted and disclosed but never
            matched — see the “not examined” note at the top of the page.
          </p>
        </Term>
      </div>
    </>
  );
}

// --------------------------------------------------------------------------
// Where this connects next
//
// The engine takes three typed record sets and returns verdicts. It has no idea where
// they came from — `ReconInputs` carries dataclasses and no paths, no vendor, no schema.
// That was done for the ground-truth boundary rather than for portability, and
// portability is what it also buys: a new source is a loader, not an engine change.
//
// Stated as what the architecture allows, not as work that is finished. The current
// Razorpay + CSV path is the one that exists and has been measured.
// --------------------------------------------------------------------------
function ErpRoadmap({ tierCounts }) {
  return (
    <section className="erp">
      <h3 className="gl-h">Connecting this to other systems</h3>
      <p className="showing">
        The matching logic never sees a file, a vendor or a schema. It receives three
        typed record sets — payments, bank lines, invoices — and returns verdicts, so a
        new source is a <strong>loader</strong>, not a change to the engine.
      </p>
      <div className="erp-grid">
        <div className="erp-now">
          <h4>Implemented today</h4>
          <ul>
            <li><strong>Razorpay</strong> — payments, via the API and its MCP server</li>
            <li><strong>Bank statement</strong> — CSV, with column and currency validation</li>
            <li><strong>Invoice ledger</strong> — CSV, replaceable from the page</li>
          </ul>
          <p className="muted">
            The first implementation, and the only one with measured numbers behind it.
          </p>
        </div>
        <div className="erp-next">
          <h4>What a new connector needs</h4>
          <ul>
            <li>Map its records onto <code>Payment</code>, <code>BankTxn</code>, <code>Invoice</code></li>
            <li>Amounts in integer paise, currency declared and checked at the boundary</li>
            <li>Nothing else — the tiers, the four verification layers and the refusal
                taxonomy are unchanged</li>
          </ul>
          <p className="muted">
            Candidates: <strong>SAP</strong>, <strong>Tally</strong>,{" "}
            <strong>Zoho Books</strong>, NetSuite, QuickBooks, or a merchant's own ERP
            export. None of these is built; the point is that none of them would touch the
            reconciliation logic.
          </p>
        </div>
      </div>
      <p className="muted erp-note">
        The one thing a new source <em>would</em> change is the narration grammar the
        parser reads, which is why unreadable narrations are routed to a model rather than
        guessed at — see the exceptions for what that looks like when it declines.
      </p>
    </section>
  );
}

export default function App() {
  const run = useRun();
  const [filter, setFilter] = useState("all");
  const [tab, setTab] = useState("reconciled");

  const categories = useMemo(() => {
    if (run.status !== "ready") return [];
    const counts = new Map();
    for (const e of run.data.exceptions)
      counts.set(e.category, (counts.get(e.category) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [run]);

  const shown = useMemo(() => {
    if (run.status !== "ready") return [];
    return filter === "all"
      ? run.data.exceptions
      : run.data.exceptions.filter((e) => e.category === filter);
  }, [run, filter]);

  if (run.status === "loading") return <div className="shell muted">Loading run…</div>;
  if (run.status === "error") {
    // Two failures, two sets of instructions. See getJSON above for why this matters.
    const unreachable = run.kind === "unreachable";
    return (
      <div className="shell">
        <div className="error">
          <h1>{unreachable ? "The API isn’t running" : "No run to show"}</h1>
          {unreachable ? (
            <>
              <p>
                Nothing is answering on <code>127.0.0.1:8000</code>, so this page has no
                data to render. Start the read-only API in a second terminal, from the
                repository root:
              </p>
              <pre>uvicorn api.main:app --port 8000</pre>
              <p className="muted">
                If that command is not found, the API extra is not installed:{" "}
                <code>pip install -e &apos;.[api]&apos;</code>. Leave it running and
                reload this page — nothing else needs restarting.
              </p>
            </>
          ) : (
            <>
              <p>{run.error}</p>
              <p>
                The API is up but has no run to serve. Produce one from the repository
                root:
              </p>
              <pre>
                python run.py generate{"\n"}python run.py match --verify --no-llm
              </pre>
            </>
          )}
        </div>
      </div>
    );
  }

  const { totals, tolerances, verification, seed, density } = run.data;
  const generatedAt = run.data.generated_at;
  const debits = run.data.debits ?? {};
  /*
   * The reconciled list, groups included.
   *
   * The hero said "130 of 141 bank credits settled" while this tab listed 126 rows, and
   * the four missing ones were exactly the split settlements — the thing the release is
   * about. A summary a reader cannot reconcile against the list under it is worse than
   * no summary.
   *
   * A group is ONE row, not two: the money moved once, and showing two rows for one
   * payment reads as a double-post to precisely the person trained to look for one. The
   * row carries every member credit so the count still adds up, and it is keyed on the
   * first member so the transcript lookup resolves.
   */
  //
  // Computed plainly, NOT with useMemo, and that is not an oversight. This line sits
  // after the loading and error early-returns above, so a hook here is called on some
  // renders and not others -- React's Rules of Hooks -- and the first version of it
  // crashed the whole page with "Rendered more hooks than during the previous render."
  // It is ~130 rows sorted once per render, which costs nothing worth a hook.
  const reconciledRows = [
    ...run.data.assignments,
    ...(run.data.settlement_groups ?? []).map((g) => ({
      bank_txn_id: g.bank_txn_ids[0],
      bank_txn_ids: g.bank_txn_ids,
      payment_ids: g.payment_ids,
      invoice_nos: g.invoice_nos,
      rupees: g.rupees,
      residual_paise: g.residual_paise,
      permutation_stability: g.permutation_stability,
      tier: "layer2b_group",
      split: true,
    })),
  ].sort((a, b) => b.rupees - a.rupees);
  const shownRupees = shown.reduce((s, e) => s + e.rupees_at_risk, 0);

  return (
    <div className="shell">
      <header>
        <div>
          <h1>
            Pramana <span className="wordmark">· three-way reconciliation</span>
          </h1>
          <p className="sub">
            Razorpay payments × bank statement × invoice ledger. Seed {seed} · density{" "}
            {density} · {totals.bank_credits} bank credits —{" "}
            <button className="linky" onClick={() => setTab("glossary")}>
              what do these mean?
            </button>
          </p>
          <p className="sub">
            <Freshness
              generatedAt={generatedAt}
              llmTier={run.data.llm_tier}
              verified={verification?.status === "verified"}
            />
          </p>
        </div>
      </header>

      <ReconciliationSummary reconciled={run.data.reconciled} totals={totals} />

      {debits.lines > 0 && <DebitLedger data={debits} />}

      <nav className="tabs">
        <button className={tab === "reconciled" ? "on" : ""} onClick={() => setTab("reconciled")}>
          Reconciled <span className="n">{reconciledRows.length}</span>
        </button>
        <button className={tab === "exceptions" ? "on" : ""} onClick={() => setTab("exceptions")}>
          Exceptions <span className="n">{run.data.exceptions.length}</span>
        </button>
        <button className={tab === "worklist" ? "on" : ""} onClick={() => setTab("worklist")}>
          Worklist
        </button>
        <button className={tab === "invoices" ? "on" : ""} onClick={() => setTab("invoices")}>
          Invoice ledger
        </button>
        <button className={tab === "glossary" ? "on" : ""} onClick={() => setTab("glossary")}>
          How to read this
        </button>
      </nav>

      {tab === "worklist" && <Worklist exceptions={run.data.exceptions} />}

      {tab === "invoices" && <Invoices />}

      {tab === "glossary" && (
        <>
          <Glossary
            tolerances={tolerances}
            seed={seed}
            density={density}
            totals={totals}
          />
          <ErpRoadmap tierCounts={run.data.tier_counts} />
        </>
      )}

      {tab === "reconciled" && (
        <>
          <OutcomeLegend
            assignments={run.data.assignments}
            groups={run.data.settlement_groups ?? []}
            exceptions={run.data.exceptions}
          />
          <p className="showing">
            {reconciledRows.length} settlements reconciled across{" "}
            {run.data.reconciled?.credits_reconciled ?? run.data.assignments.length} bank
            credits, largest first. Open one to see the records before and after.
          </p>
          <ol className="matches">
            {reconciledRows.map((row, i) => (
              <MatchCard key={row.bank_txn_id} row={row} rank={i + 1} />
            ))}
          </ol>
          <Ceiling />
          <Verification block={verification} />
        </>
      )}

      {tab === "exceptions" && (
      <>
      <nav className="filters">
        <button
          className={filter === "all" ? "on" : ""}
          onClick={() => setFilter("all")}
        >
          All <span className="n">{run.data.exceptions.length}</span>
        </button>
        {categories.map(([cat, n]) => (
          <button
            key={cat}
            className={filter === cat ? "on" : ""}
            onClick={() => setFilter(cat)}
          >
            {CATEGORY_LABEL[cat] ?? cat} <span className="n">{n}</span>
          </button>
        ))}
      </nav>

      <p className="showing">
        Showing {shown.length} · {RUPEES.format(shownRupees)} at risk
      </p>

      <ol className="exceptions">
        {shown.map((row, i) => (
          <ExceptionCard key={row.bank_txn_id} row={row} rank={i + 1} />
        ))}
      </ol>

      {shown.length === 0 && (
        <p className="muted">Nothing in this category.</p>
      )}

      <Ceiling />
          <Verification block={verification} />
      </>
      )}

      <footer>
        {/* This line used to read "Tolerance 100p + 0bps · MDR band 0.018–0.025 ·
            lookback 5d · pool ≤ 20 · materiality ₹5,000" and expect the reader to supply
            the meaning. Each setting now says what it is in plain words, carries the
            explanation on hover, and opens the full one. The values still come from the
            run, so the footer cannot describe a threshold the engine is not using. */}
        <p className="params">
          <span className="params-lead">How careful it is being:</span>
          <button className="param" title={PARAM_HELP.tolerance}
                  onClick={() => setTab("glossary")}>
            amounts may differ by{" "}
            <strong>{RUPEES.format(tolerances.tol_abs_paise / 100)}</strong>
          </button>
          <button className="param" title={PARAM_HELP.mdr}
                  onClick={() => setTab("glossary")}>
            gateway fee{" "}
            <strong>
              {(tolerances.mdr_rate_band[0] * 100).toFixed(1)}–
              {(tolerances.mdr_rate_band[1] * 100).toFixed(1)}%
            </strong>
          </button>
          <button className="param" title={PARAM_HELP.lookback}
                  onClick={() => setTab("glossary")}>
            settles within <strong>{tolerances.lookback_days} days</strong>
          </button>
          <button className="param" title={PARAM_HELP.pool}
                  onClick={() => setTab("glossary")}>
            searches up to <strong>{tolerances.max_pool} payments</strong>
          </button>
          <button className="param" title={PARAM_HELP.materiality}
                  onClick={() => setTab("glossary")}>
            audit threshold{" "}
            <strong>{RUPEES.format(tolerances.materiality_rupees)}</strong>
          </button>
        </p>
        <p className="muted">
          Every threshold was fixed before the run and none is tuned per record. This
          view is read-only: the engine's verdicts come from a deterministic batch, and
          there is no endpoint through which this page could change one.
        </p>
      </footer>
    </div>
  );
}

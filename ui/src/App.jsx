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
  no_candidate: "Nothing accounts for it",
};

/*
 * What the engine did not look at.
 *
 * The header leads with "at risk", and that figure counts refused CREDITS only. The
 * engine reads `is_credit` transactions and nothing else, so a chargeback, a reversal
 * or a bank fee is invisible to it -- not matched, not refused, not counted.
 *
 * Showing the exception list without this is misleading by omission, and the omission
 * matters more here than in the metrics block: the metrics block is read by whoever
 * builds the engine, and this page is read by whoever acts on it. So it sits directly
 * under the totals it qualifies, not behind a tab, and it is styled as a disclosure
 * rather than as an exception -- these are not items to work, they are items the engine
 * cannot speak about.
 */
function NotExamined({ data }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="disclosure">
      <button className="disclosure-head" onClick={() => setOpen((v) => !v)}>
        <span className="disclosure-mark">Not examined</span>
        <span>
          {data.debit_lines} debit line{data.debit_lines === 1 ? "" : "s"} ·{" "}
          <strong>{RUPEES.format(data.rupees)}</strong> left the account on lines the
          engine does not read
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
                <th className="num">Amount</th>
              </tr>
            </thead>
            <tbody>
              {(data.lines ?? []).map((l) => (
                <tr key={l.bank_txn_id}>
                  <td>{l.txn_date}</td>
                  <td className="mono">{l.narration}</td>
                  <td className="num">{RUPEES.format(l.rupees)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted">
            These are not scored either way. Scoring them against a verdict the engine
            structurally cannot produce would be theatre — a permanent miss no amount of
            engine work could close. They are disclosed so this list is not mistaken for
            a complete account of the statement.
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
      {gate && (
        <p className="gate">
          Permutation gate: <strong>{gate.unstable}</strong> of{" "}
          <strong>{gate.txns_observed}</strong> assignments changed under input
          reordering, over {gate.passes} shuffled passes. Anything that had would have
          been refused.
        </p>
      )}
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
function MatchCard({ row, rank }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="match">
      <button className="match-head" onClick={() => setOpen((v) => !v)}>
        <span className="rank">{rank}</span>
        <span className="amount">{RUPEES.format(row.rupees)}</span>
        <span className="ids">
          {row.payment_ids.length === 1
            ? row.payment_ids[0]
            : `${row.payment_ids.length} payments`}
        </span>
        <span className={`tier tier-${row.tier}`}>{TIER_LABEL[row.tier] ?? row.tier}</span>
        <span className="resid">
          {row.residual_paise === 0
            ? "exact"
            : `${row.residual_paise > 0 ? "+" : ""}${row.residual_paise} p`}
        </span>
        <span className="chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="match-body">
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
          <Explanation txnId={row.bank_txn_id} open={open} />
        </div>
      )}
    </li>
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
// The track KPIs, above the fold
//
// These lived only in the Ceiling panel, which renders BELOW a fifteen-row exception
// list -- so the numbers a judge is looking for were the ones they had to scroll for. An
// external reviewer put it exactly right: the first screen showed "At risk / Assigned /
// Refused" and none of the metrics the track is scored on.
//
// `Assigned 126 of 141 credits` was the sharper problem. That is a CREDIT count; the
// match rate is payment-level (172/194 = 88.66%). A reader takes the first for the second
// and is wrong by two different denominators. It is now labelled as credits and the match
// rate is stated separately, in its own tile, with the bound that qualifies it.
// --------------------------------------------------------------------------
function Kpis({ totals }) {
  const sc = useScorecard();
  const pct = (x) => `${(x * 100).toFixed(2)}%`;
  const ready = sc.status === "ready" && sc.data?.status === "ok";
  const c = ready ? sc.data.coverage : null;
  const p = ready ? sc.data.precision : null;
  const r = ready ? sc.data.refusals : null;

  return (
    <div className="stats">
      <Stat
        label="At risk"
        value={RUPEES.format(totals.rupees_at_risk)}
        sub={`${totals.refused + totals.no_candidate} exceptions`}
        tone="warn"
      />
      <Stat
        label="Match rate"
        value={c ? pct(c.match_rate) : "—"}
        sub={c ? `${c.payments_assigned}/${c.captured_payments} captured payments` : "not scored"}
      />
      <Stat
        label="Precision"
        value={p ? pct(p.match_precision) : "—"}
        sub={
          p
            ? `${p.correct_assignments}/${p.total_assignments} · 95% CI ≥ ${pct(
                p.precision_ci_lower
              )}`
            : "not scored"
        }
      />
      <Stat
        label="Refusal correctness"
        value={r ? pct(r.correctness) : "—"}
        sub={r ? `${r.correct}/${r.total} refusals truth agrees with` : "not scored"}
      />
      <Stat
        label="Credits posted"
        value={totals.assigned}
        sub={`of ${totals.bank_credits} bank credits`}
      />
    </div>
  );
}

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

export default function App() {
  const run = useRun();
  const [filter, setFilter] = useState("all");
  const [tab, setTab] = useState("exceptions");

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
  const notExamined = run.data.not_examined ?? {};
  const shownRupees = shown.reduce((s, e) => s + e.rupees_at_risk, 0);

  return (
    <div className="shell">
      <header>
        <div>
          <h1>Exceptions</h1>
          <p className="sub">
            Ranked by money at risk. Seed {seed} · density {density} ·{" "}
            {totals.bank_credits} bank credits
          </p>
          <p className="sub">
            <Freshness
              generatedAt={generatedAt}
              llmTier={run.data.llm_tier}
              verified={verification?.status === "verified"}
            />
          </p>
        </div>
        <Kpis totals={totals} />
      </header>

      {notExamined.debit_lines > 0 && <NotExamined data={notExamined} />}

      <nav className="tabs">
        <button className={tab === "exceptions" ? "on" : ""} onClick={() => setTab("exceptions")}>
          Exceptions
        </button>
        <button className={tab === "matches" ? "on" : ""} onClick={() => setTab("matches")}>
          Matches
        </button>
        <button className={tab === "worklist" ? "on" : ""} onClick={() => setTab("worklist")}>
          Worklist
        </button>
        <button className={tab === "invoices" ? "on" : ""} onClick={() => setTab("invoices")}>
          Invoice ledger
        </button>
      </nav>

      {tab === "worklist" && <Worklist exceptions={run.data.exceptions} />}

      {tab === "invoices" && <Invoices />}

      {tab === "matches" && (
        <>
          <p className="showing">
            {run.data.assignments.length} posted matches, largest first. Open one to see
            why it was made.
          </p>
          <ol className="matches">
            {[...run.data.assignments]
              .sort((a, b) => b.rupees - a.rupees)
              .map((row, i) => (
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
        <p>
          Tolerance {tolerances.tol_abs_paise}p + {tolerances.tol_rel_bps}bps · MDR band{" "}
          {tolerances.mdr_rate_band.join("–")} · lookback {tolerances.lookback_days}d ·
          pool ≤ {tolerances.max_pool} · materiality{" "}
          {RUPEES.format(tolerances.materiality_rupees)}
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

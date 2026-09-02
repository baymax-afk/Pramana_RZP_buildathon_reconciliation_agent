import { useEffect, useMemo, useState } from "react";

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

const CATEGORY_LABEL = {
  order_dependent_assignment: "Order-dependent",
  multiple_candidates: "Ambiguous — several fit",
  solution_cap_reached: "Ambiguous — many fit",
  decomposition_out_of_bounds: "Cannot decompose",
  fs_below_lower_threshold: "Counterparty disagrees",
  fs_review_band: "Weak evidence",
  amount_name_conflict: "Amount/name conflict",
  unexplained_residual: "Unexplained shortfall",
  no_candidate: "Nothing accounts for it",
};

function useRun() {
  const [state, setState] = useState({ status: "loading" });
  useEffect(() => {
    let alive = true;
    fetch("/api/run")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText);
        return r.json();
      })
      .then((data) => alive && setState({ status: "ready", data }))
      .catch((e) => alive && setState({ status: "error", error: String(e.message ?? e) }));
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
        </div>
      )}
    </li>
  );
}

function Verification({ block }) {
  if (!block) return null;
  const { relations = [], permutation_gate: gate } = block;
  if (!relations.length && !gate) return null;
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

export default function App() {
  const run = useRun();
  const [filter, setFilter] = useState("all");

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
  if (run.status === "error")
    return (
      <div className="shell">
        <div className="error">
          <h1>No run to show</h1>
          <p>{run.error}</p>
          <pre>python run.py generate{"\n"}python run.py match --verify</pre>
        </div>
      </div>
    );

  const { totals, tolerances, verification, seed, density } = run.data;
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
        </div>
        <div className="stats">
          <Stat
            label="At risk"
            value={RUPEES.format(totals.rupees_at_risk)}
            sub={`${totals.refused + totals.no_candidate} exceptions`}
            tone="warn"
          />
          <Stat
            label="Assigned"
            value={totals.assigned}
            sub={`of ${totals.bank_credits} credits`}
          />
          <Stat
            label="Refused"
            value={totals.refused + totals.no_candidate}
            sub="engine declined to guess"
          />
        </div>
      </header>

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

      <Verification block={verification} />

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

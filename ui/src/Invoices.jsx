import { useCallback, useEffect, useRef, useState } from "react";

/*
 * Invoice ledger management.
 *
 * The flow is deliberately two-step: choosing a file VALIDATES it and shows what would
 * happen; a second, explicit click replaces the ledger. Replacing reconciliation input
 * on a single click is the kind of thing people do at 6pm and regret, and the dry run
 * costs one extra press.
 *
 * The page never implies an upload changed the reconciliation. It says, in the success
 * banner, that the engine must be re-run -- because the exceptions on the other tab are
 * still the old run's and pretending otherwise would be the same category of overclaim
 * this whole project argues against.
 */

const RUPEES = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

export default function Invoices() {
  const [ledger, setLedger] = useState({ status: "loading" });
  const [check, setCheck] = useState(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState(null);
  const [query, setQuery] = useState("");
  const fileRef = useRef(null);
  const pending = useRef(null);

  const load = useCallback(async (q = "") => {
    try {
      const r = await fetch(`/api/invoices?limit=300${q ? `&q=${encodeURIComponent(q)}` : ""}`);
      if (!r.ok) throw new Error(r.statusText);
      setLedger({ status: "ready", data: await r.json() });
    } catch (e) {
      setLedger({ status: "error", error: String(e.message ?? e) });
    }
  }, []);

  useEffect(() => {
    load(query);
  }, [load, query]);

  async function onPick(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    pending.current = file;
    setBanner(null);
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch("/api/invoices/validate", { method: "POST", body: fd });
      setCheck(await r.json());
    } catch (err) {
      setBanner({ tone: "bad", text: `Could not read the file: ${err}` });
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    if (!pending.current) return;
    setBusy(true);
    setBanner(null);
    try {
      const fd = new FormData();
      fd.append("file", pending.current);
      const r = await fetch("/api/invoices", { method: "POST", body: fd });
      const body = await r.json();
      if (!r.ok) {
        const d = body.detail ?? body;
        setBanner({ tone: "bad", text: d.message ?? "Upload rejected", list: d.errors });
      } else {
        setBanner({ tone: "good", text: body.next_step, sub: `${body.row_count} invoices loaded.` });
        setCheck(null);
        pending.current = null;
        if (fileRef.current) fileRef.current.value = "";
        load(query);
      }
    } catch (err) {
      setBanner({ tone: "bad", text: String(err) });
    } finally {
      setBusy(false);
    }
  }

  async function revert() {
    setBusy(true);
    try {
      const r = await fetch("/api/invoices/revert", { method: "DELETE" });
      const body = await r.json();
      setBanner(
        r.ok
          ? { tone: "good", text: body.next_step, sub: `Restored ${body.restored_from}.` }
          : { tone: "bad", text: body.detail ?? "Nothing to revert to" }
      );
      load(query);
    } finally {
      setBusy(false);
    }
  }

  const rows = ledger.status === "ready" ? ledger.data.invoices : [];
  const versions = ledger.status === "ready" ? ledger.data.versions ?? [] : [];

  return (
    <div className="invoices">
      <section className="upload">
        <h2>Replace the invoice ledger</h2>
        <p className="muted">
          Uploading changes the reconciliation's <strong>input</strong>, never its
          verdicts. The engine has to be re-run for a new ledger to have any effect.
        </p>

        <div className="upload-row">
          <label className="file-btn">
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              onChange={onPick}
              disabled={busy}
            />
            Choose CSV…
          </label>
          <a className="link" href="/api/invoices/template" download="invoices_template.csv">
            Download template
          </a>
          {versions.length > 0 && (
            <button className="ghost" onClick={revert} disabled={busy}>
              Revert to previous ({versions.length} archived)
            </button>
          )}
        </div>

        {check && (
          <div className={`check ${check.valid ? "ok" : "bad"}`}>
            <div className="check-head">
              {check.valid ? (
                <>
                  <strong>{check.filename}</strong> looks valid — {check.row_count}{" "}
                  invoices.
                </>
              ) : (
                <>
                  <strong>{check.filename}</strong> has {check.error_count} problem
                  {check.error_count === 1 ? "" : "s"}. Nothing has been changed.
                </>
              )}
            </div>

            {!check.valid && (
              <ul className="errs">
                {check.errors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            )}

            {check.valid && check.preview?.length > 0 && (
              <table className="preview">
                <thead>
                  <tr>
                    <th>Invoice</th>
                    <th>Customer</th>
                    <th className="num">Gross</th>
                    <th className="num">TDS</th>
                  </tr>
                </thead>
                <tbody>
                  {check.preview.map((r, i) => (
                    <tr key={i}>
                      <td className="mono">{r.invoice_no}</td>
                      <td>{r.customer_name}</td>
                      <td className="num">{RUPEES.format(Number(r.gross_amount || 0))}</td>
                      <td className="num">{RUPEES.format(Number(r.tds_amount || 0))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {check.valid && (
              <button className="primary" onClick={commit} disabled={busy}>
                Replace ledger with {check.row_count} invoices
              </button>
            )}
          </div>
        )}

        {banner && (
          <div className={`banner ${banner.tone}`}>
            <p>{banner.text}</p>
            {banner.sub && <p className="muted">{banner.sub}</p>}
            {banner.list && (
              <ul className="errs">
                {banner.list.slice(0, 10).map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>

      <section className="ledger">
        <div className="ledger-head">
          <h2>
            Current ledger{" "}
            <span className="muted">
              {ledger.status === "ready" ? `${ledger.data.total ?? rows.length} invoices` : ""}
            </span>
          </h2>
          <input
            className="search"
            placeholder="Filter by invoice number or customer…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        {ledger.status === "error" && <p className="muted">{ledger.error}</p>}

        <table className="ledger-table">
          <thead>
            <tr>
              <th>Invoice</th>
              <th>Customer</th>
              <th>Date</th>
              <th className="num">Gross</th>
              <th className="num">TDS</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.invoice_no}>
                <td className="mono">{r.invoice_no}</td>
                <td>{r.customer_name}</td>
                <td className="mono">{r.invoice_date}</td>
                <td className="num">{RUPEES.format(Number(r.gross_amount || 0))}</td>
                <td className="num">
                  {Number(r.tds_amount) > 0 ? RUPEES.format(Number(r.tds_amount)) : "—"}
                </td>
                <td>{r.status}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {rows.length === 0 && ledger.status === "ready" && (
          <p className="muted">No invoices match.</p>
        )}
      </section>
    </div>
  );
}

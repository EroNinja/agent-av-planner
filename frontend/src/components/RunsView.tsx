import { useEffect, useState } from "react";
import { api, type RunRow } from "../lib/api";
import { FAULT_COLORS, num, pct } from "../lib/theme";

export default function RunsView() {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [filterCondition, setFilterCondition] = useState("");
  const [filterProvider, setFilterProvider] = useState("");
  const [selected, setSelected] = useState<RunRow | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const response = await api.runs({
        limit: 400,
        condition: filterCondition || undefined,
        provider: filterProvider || undefined,
      });
      setRuns(response.runs);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, [filterCondition, filterProvider]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    fetch(`/api/runs/${selected.id}`)
      .then((response) => response.json())
      .then(setDetail)
      .catch(() => setDetail(null));
  }, [selected?.id]);

  return (
    <div className="cols two" style={{ gridTemplateColumns: "minmax(0, 1fr) minmax(0, 420px)" }}>
      <div className="card">
        <div className="card-head">
          <div>
            <h3>Run history</h3>
            <p>{runs.length} diagnoses, newest first</p>
          </div>
          <div className="row">
            <select value={filterProvider} onChange={(event) => setFilterProvider(event.target.value)} style={{ width: 130 }}>
              <option value="">all providers</option>
              <option value="anthropic">anthropic</option>
              <option value="groq">groq</option>
              <option value="mock">mock</option>
            </select>
            <select value={filterCondition} onChange={(event) => setFilterCondition(event.target.value)} style={{ width: 150 }}>
              <option value="">both conditions</option>
              <option value="single_agent">single_agent</option>
              <option value="pipeline">pipeline</option>
            </select>
            <button onClick={load} disabled={loading}>
              {loading ? "…" : "Refresh"}
            </button>
          </div>
        </div>

        <div className="scrolly scrollx">
          <table className="data">
            <thead>
              <tr>
                <th style={{ textAlign: "right" }}>id</th>
                <th className="mono">log</th>
                <th>truth</th>
                <th>predicted</th>
                <th>condition</th>
                <th className="mono">model</th>
                <th style={{ textAlign: "right" }}>conf.</th>
                <th style={{ textAlign: "right" }}>evid.</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr
                  key={run.id}
                  onClick={() => setSelected(run)}
                  style={{
                    cursor: "pointer",
                    background: selected?.id === run.id ? "var(--surface-2)" : undefined,
                  }}
                >
                  <td className="num">{run.id}</td>
                  <td className="mono">{run.log_name}</td>
                  <td>
                    <code style={{ color: FAULT_COLORS[run.true_fault], fontSize: 11.5 }}>
                      {run.true_fault}
                    </code>
                  </td>
                  <td>
                    {run.api_error ? (
                      <span className="badge bad">call failed</span>
                    ) : run.malformed ? (
                      <span className="badge warn">malformed</span>
                    ) : run.correct ? (
                      <span className="badge ok">{run.predicted_fault}</span>
                    ) : (
                      <span className="badge bad">{run.predicted_fault ?? "—"}</span>
                    )}
                  </td>
                  <td style={{ fontSize: 11.5 }}>{run.condition}</td>
                  <td className="mono">{run.model}</td>
                  <td className="num">{num(run.confidence)}</td>
                  <td className="num">{run.evidence_score === null ? "—" : pct(run.evidence_score)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!runs.length && <p className="empty">No runs recorded yet.</p>}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h3>Detail</h3>
            <p>{selected ? `run ${selected.id}` : "select a row"}</p>
          </div>
        </div>
        {!selected && <p className="empty">Nothing selected.</p>}
        {selected && (
          <div className="stack">
            <dl className="kv">
              <dt>log</dt>
              <dd className="mono">{selected.log_name}</dd>
              <dt>truth</dt>
              <dd><code>{selected.true_fault}</code></dd>
              <dt>predicted</dt>
              <dd><code>{selected.predicted_fault ?? "—"}</code></dd>
              <dt>arm</dt>
              <dd className="mono">{selected.provider}:{selected.model}</dd>
              <dt>condition</dt>
              <dd>{selected.condition}</dd>
              <dt>confidence</dt>
              <dd>{num(selected.confidence)}</dd>
              <dt>tokens</dt>
              <dd>{selected.input_tokens} in / {selected.output_tokens} out</dd>
              <dt>latency</dt>
              <dd>{Math.round(selected.llm_latency_ms)} ms</dd>
              {selected.recovery_valid !== null && (
                <>
                  <dt>recovery</dt>
                  <dd>
                    {selected.recovery_action ?? "no plan"} on {selected.recovery_target ?? "—"}:{" "}
                    {selected.recovery_valid ? "valid" : "invalid"}
                  </dd>
                </>
              )}
              <dt>problems</dt>
              <dd>{(JSON.parse(selected.failure_labels ?? "[]") as string[]).join(", ") || "none detected"}</dd>
              <dt>when</dt>
              <dd>{selected.created_at}</dd>
            </dl>

            {selected.explanation && (
              <div>
                <h4 style={{ marginBottom: 4 }}>Explanation</h4>
                <p className="note">{selected.explanation}</p>
              </div>
            )}

            {detail?.payload?.log_summary && (
              <div>
                <h4 style={{ marginBottom: 4 }}>Correlator summary</h4>
                <pre className="json">{JSON.stringify(detail.payload.log_summary, null, 2)}</pre>
              </div>
            )}

            {detail?.payload?.evidence && (
              <div>
                <h4 style={{ marginBottom: 4 }}>Evidence check</h4>
                <pre className="json">{JSON.stringify(detail.payload.evidence, null, 2)}</pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

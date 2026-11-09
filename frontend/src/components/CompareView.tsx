import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  api,
  streamSSE,
  type AnalysisArm,
  type CountStat,
  type LogInfo,
  type ProviderInfo,
  type StatsResponse,
  type SweepEvent,
} from "../lib/api";
import { CONDITION_LABELS, SERIES, STATUS, num, pct } from "../lib/theme";

const AXIS = { fill: "var(--text-muted)", fontSize: 11 };

function countsTitle(arm: AnalysisArm): string {
  const side = (c: CountStat | undefined) =>
    c ? `${c.k}/${c.n} stated counts, ${c.runs_with_wrong_count} runs with a wrong count` : "not scored";
  return `baseline: ${side(arm.single_agent.counts)}\npipeline: ${side(arm.pipeline.counts)}`;
}

function shortModel(model: string): string {
  return model.replace(/^openai\//, "").replace(/-versatile$|-instant$/, "");
}

interface Props {
  providers: ProviderInfo[];
  logs: LogInfo[];
}

export default function CompareView({ providers, logs }: Props) {
  const [arms, setArms] = useState<string[]>([]);
  const [pickProvider, setPickProvider] = useState("mock");
  const [pickModel, setPickModel] = useState("");
  const [sweepId, setSweepId] = useState<string | null>(null);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const [running, setRunning] = useState(false);
  const [feed, setFeed] = useState<SweepEvent[]>([]);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scopeToSweep, setScopeToSweep] = useState(true);
  const [logSet, setLogSet] = useState<"clean" | "noisy" | "all">("noisy");
  const [length, setLength] = useState(30);
  const [recovery, setRecovery] = useState(false);
  const [hint, setHint] = useState(true);
  const [extras, setExtras] = useState<string[]>([]);
  const [analysis, setAnalysis] = useState<AnalysisArm[]>([]);

  const providerInfo = providers.find((candidate) => candidate.provider === pickProvider);
  const lengths = [...new Set(logs.map((log) => log.request_count))].sort((a, b) => a - b);
  const sweepLogs = logs.filter(
    (log) => (logSet === "all" || (logSet === "noisy") === log.noise) && log.request_count === length,
  );

  useEffect(() => {
    setPickModel(providerInfo?.default_model ?? "");
  }, [pickProvider, providerInfo?.default_model]);

  useEffect(() => {
    const available = providers.filter((info) => info.available && info.provider !== "mock");
    if (available.length && !arms.length) {
      setArms([`${available[0].provider}:${available[0].default_model}`]);
      setPickProvider(available[0].provider);
    }
  }, [providers.length]);

  async function refreshStats(id: string | null) {
    try {
      setStats(await api.stats(scopeToSweep && id ? id : undefined, !scopeToSweep));
      setAnalysis(scopeToSweep ? (await api.analysis(id ?? undefined)).arms : []);
    } catch (exc) {
      setError(String(exc));
    }
  }

  useEffect(() => {
    refreshStats(sweepId);
  }, [scopeToSweep]);

  function addArm() {
    const key = `${pickProvider}:${pickModel}`;
    if (!arms.includes(key)) setArms([...arms, key]);
  }

  async function runSweep() {
    if (!arms.length) return;
    setError(null);
    setRunning(true);
    setFeed([]);
    setProgress({ completed: 0, total: 0 });

    try {
      const started = await api.startSweep({
        arms,
        conditions: ["single_agent", "pipeline", ...extras],
        hint,
        log_names: sweepLogs.map((log) => log.name),
        recovery,
      });
      setSweepId(started.sweep_id);
      setProgress({ completed: 0, total: started.total });

      await streamSSE<SweepEvent>(
        `/api/sweep/${started.sweep_id}/stream`,
        { method: "GET" },
        (event) => {
          if (event.type === "cell") {
            setProgress({ completed: event.completed ?? 0, total: event.total ?? 0 });
            setFeed((current) => [event, ...current].slice(0, 60));
          } else if (event.type === "arm_failed" || event.type === "error") {
            setFeed((current) => [event, ...current].slice(0, 60));
          }
        },
      );
      await refreshStats(started.sweep_id);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setRunning(false);
    }
  }

  const accuracyData = useMemo(() => {
    if (!stats) return [];
    const byModel = new Map<string, any>();
    for (const row of stats.by_arm) {
      const key = `${row.provider}:${row.model}`;
      const entry = byModel.get(key) ?? { key, label: shortModel(row.model), provider: row.provider };
      entry[row.condition] = row.accuracy;
      entry[`${row.condition}_n`] = row.n;
      byModel.set(key, entry);
    }
    return [...byModel.values()];
  }, [stats]);

  const confidenceData = useMemo(() => {
    if (!stats) return [];
    const byModel = new Map<string, any>();
    for (const row of stats.by_arm) {
      const key = `${row.provider}:${row.model}`;
      const entry = byModel.get(key) ?? { key, label: shortModel(row.model) };
      entry[row.condition] = row.mean_confidence ?? 0;
      byModel.set(key, entry);
    }
    return [...byModel.values()];
  }, [stats]);

  const deltaData = useMemo(
    () => (stats?.deltas ?? []).map((row) => ({ ...row, label: shortModel(row.model) })),
    [stats],
  );

  const totals = useMemo(() => {
    if (!stats?.by_arm.length) return null;
    const n = stats.by_arm.reduce((sum, row) => sum + row.n, 0);
    const tokens = stats.by_arm.reduce((sum, row) => sum + row.input_tokens + row.output_tokens, 0);
    const malformed =
      stats.by_arm.reduce((sum, row) => sum + row.malformed_rate * row.n, 0) / Math.max(1, n);
    const evidenceRows = stats.by_arm.filter((row) => row.mean_evidence_score !== null);
    const evidence = evidenceRows.length
      ? evidenceRows.reduce((sum, row) => sum + (row.mean_evidence_score ?? 0), 0) / evidenceRows.length
      : null;
    return { n, tokens, malformed, evidence, arms: new Set(stats.by_arm.map((r) => r.model)).size };
  }, [stats]);

  return (
    <div className="stack">
      {error && <div className="banner err">{error}</div>}

      <div className="card">
        <div className="card-head">
          <div>
            <h3>Sweep</h3>
            <p>
              Every log × every arm × both conditions. One LLM call per cell, so the two conditions
              stay matched on call count.
            </p>
          </div>
        </div>

        <div className="row" style={{ alignItems: "flex-end", marginBottom: 12 }}>
          <label className="field" style={{ width: 150 }}>
            provider
            <select value={pickProvider} onChange={(event) => setPickProvider(event.target.value)}>
              {providers.map((info) => (
                <option key={info.provider} value={info.provider} disabled={!info.available}>
                  {info.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field" style={{ width: 260 }}>
            model
            <select value={pickModel} onChange={(event) => setPickModel(event.target.value)}>
              {(providerInfo?.models ?? []).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="field" style={{ width: 130 }}>
            logs
            <select value={logSet} onChange={(event) => setLogSet(event.target.value as typeof logSet)}>
              <option value="noisy">noisy only</option>
              <option value="clean">clean only</option>
              <option value="all">clean + noisy</option>
            </select>
          </label>
          <label className="field" style={{ width: 100 }}>
            requests
            <select value={length} onChange={(event) => setLength(Number(event.target.value))}>
              {lengths.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <button onClick={addArm}>Add arm</button>
        </div>

        <div className="chips" style={{ marginBottom: 12 }}>
          {arms.map((arm) => (
            <button
              key={arm}
              className="chip"
              aria-pressed
              title="click to remove"
              onClick={() => setArms(arms.filter((candidate) => candidate !== arm))}
            >
              {arm} ✕
            </button>
          ))}
          {!arms.length && <span className="muted">Add at least one arm.</span>}
        </div>

        <div className="row">
          <button className="primary" onClick={runSweep} disabled={running || !arms.length || !sweepLogs.length}>
            {running ? "Running…" : `Run sweep (${sweepLogs.length * arms.length * (2 + extras.length)} diagnoses)`}
          </button>
          {[
            ["pipeline_no_spacing", "no-spacing pipeline"],
            ["tool_agent", "tool agent"],
          ].map(([id, label]) => (
            <label className="toggle" key={id}>
              <input
                type="checkbox"
                checked={extras.includes(id)}
                onChange={(event) =>
                  setExtras(event.target.checked ? [...extras, id] : extras.filter((item) => item !== id))
                }
              />
              {label}
            </label>
          ))}
          <label className="toggle" title="Removes the spread-versus-clustered rule from the prompt">
            <input type="checkbox" checked={!hint} onChange={(event) => setHint(!event.target.checked)} />
            remove hint
          </label>
          <label className="toggle" title="Changes the prompt, so these runs are not comparable with runs without it">
            <input
              type="checkbox"
              checked={recovery}
              onChange={(event) => setRecovery(event.target.checked)}
            />
            score recovery
          </label>
          <label className="toggle" style={{ marginLeft: "auto" }}>
            <input
              type="checkbox"
              checked={scopeToSweep}
              onChange={(event) => setScopeToSweep(event.target.checked)}
            />
            latest sweep only
          </label>
        </div>

        {progress.total > 0 && (
          <div style={{ marginTop: 12 }}>
            <div className="spread" style={{ marginBottom: 5 }}>
              <span className="muted">
                {progress.completed} / {progress.total} cells
              </span>
              <span className="muted">{pct(progress.completed / progress.total)}</span>
            </div>
            <div className="progress">
              <span style={{ width: `${(progress.completed / progress.total) * 100}%` }} />
            </div>
          </div>
        )}

        {feed.length > 0 && (
          <div className="scrolly" style={{ maxHeight: 150, marginTop: 12 }}>
            <table className="data">
              <tbody>
                {feed.map((event, index) =>
                  event.type === "cell" ? (
                    <tr key={index}>
                      <td className="mono">{event.log_name}</td>
                      <td className="mono">{event.arm}</td>
                      <td>{event.condition}</td>
                      <td className="mono">{event.predicted_fault ?? "—"}</td>
                      <td>
                        {event.api_error ? (
                          <span className="badge bad">call failed</span>
                        ) : event.malformed ? (
                          <span className="badge warn">malformed</span>
                        ) : event.correct ? (
                          <span className="badge ok">ok</span>
                        ) : (
                          <span className="badge bad">miss</span>
                        )}
                      </td>
                    </tr>
                  ) : (
                    <tr key={index}>
                      <td colSpan={5} style={{ color: "var(--critical)" }}>
                        {event.arm ? `${event.arm}: ` : ""}
                        {event.error ?? event.message}
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {totals && (
        <div className="metrics">
          <div className="metric">
            <div className="label">diagnoses</div>
            <div className="value">{totals.n}</div>
            <div className="sub">{totals.arms} model{totals.arms === 1 ? "" : "s"}</div>
          </div>
          <div className="metric">
            <div className="label">tokens</div>
            <div className="value">{totals.tokens.toLocaleString()}</div>
            <div className="sub">input + output</div>
          </div>
          <div className="metric">
            <div className="label">malformed</div>
            <div className="value">{pct(totals.malformed, 1)}</div>
            <div className="sub">unparseable answers</div>
          </div>
          <div className="metric">
            <div className="label">evidence</div>
            <div className="value">{totals.evidence === null ? "—" : pct(totals.evidence)}</div>
            <div className="sub">cited claims that check out</div>
          </div>
        </div>
      )}

      {accuracyData.length > 0 && (
        <div className="card">
          <div className="card-head">
            <div>
              <h3>Accuracy by model and condition</h3>
              <p>
                Fault named correctly, counting answers whose JSON was broken. The gap between the
                two bars is the effect under test.
              </p>
            </div>
          </div>
          <div className="legend">
            <span className="key">
              <i style={{ background: SERIES.single_agent }} />
              {CONDITION_LABELS.single_agent}
            </span>
            <span className="key">
              <i style={{ background: SERIES.pipeline }} />
              {CONDITION_LABELS.pipeline}
            </span>
          </div>
          <div style={{ width: "100%", height: 300 }}>
            <ResponsiveContainer>
              <BarChart data={accuracyData} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
                <CartesianGrid stroke="var(--grid)" vertical={false} />
                <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--grid)" }} />
                <YAxis
                  domain={[0, 1]}
                  tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
                  tick={AXIS}
                  tickLine={false}
                  axisLine={false}
                  width={44}
                />
                <Tooltip
                  formatter={(value: number, name: string) => [pct(value, 1), CONDITION_LABELS[name] ?? name]}
                  contentStyle={{
                    background: "var(--surface-2)",
                    border: "1px solid var(--border)",
                    borderRadius: 8,
                    fontSize: 12,
                    color: "var(--text-primary)",
                  }}
                />
                <Bar dataKey="single_agent" fill={SERIES.single_agent} maxBarSize={24} radius={[4, 4, 0, 0]} />
                <Bar dataKey="pipeline" fill={SERIES.pipeline} maxBarSize={24} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {deltaData.length > 0 && (
        <div className="cols wide">
          <div className="card">
            <div className="card-head">
              <div>
                <h3>Pipeline minus baseline</h3>
                <p>Above zero means the structured summary beat raw log text.</p>
              </div>
            </div>
            <div style={{ width: "100%", height: 260 }}>
              <ResponsiveContainer>
                <BarChart data={deltaData} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
                  <CartesianGrid stroke="var(--grid)" vertical={false} />
                  <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--grid)" }} />
                  <YAxis
                    tickFormatter={(value: number) => `${value > 0 ? "+" : ""}${Math.round(value * 100)}`}
                    tick={AXIS}
                    tickLine={false}
                    axisLine={false}
                    width={44}
                  />
                  <Tooltip
                    formatter={(value: number) => [`${value > 0 ? "+" : ""}${(value * 100).toFixed(1)} pts`, "delta"]}
                    contentStyle={{
                      background: "var(--surface-2)",
                      border: "1px solid var(--border)",
                      borderRadius: 8,
                      fontSize: 12,
                      color: "var(--text-primary)",
                    }}
                  />
                  <ReferenceLine y={0} stroke="var(--text-muted)" />
                  <Bar dataKey="delta" maxBarSize={24} radius={[4, 4, 0, 0]}>
                    {deltaData.map((row) => (
                      <Cell
                        key={row.model}
                        fill={row.delta > 0 ? STATUS.good : row.delta < 0 ? STATUS.critical : "var(--text-muted)"}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <div>
                <h3>Mean confidence</h3>
                <p>What the model claimed, which is not the same as being right.</p>
              </div>
            </div>
            <div style={{ width: "100%", height: 260 }}>
              <ResponsiveContainer>
                <BarChart data={confidenceData} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
                  <CartesianGrid stroke="var(--grid)" vertical={false} />
                  <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={{ stroke: "var(--grid)" }} />
                  <YAxis domain={[0, 1]} tick={AXIS} tickLine={false} axisLine={false} width={44} />
                  <Tooltip
                    formatter={(value: number, name: string) => [num(value), CONDITION_LABELS[name] ?? name]}
                    contentStyle={{
                      background: "var(--surface-2)",
                      border: "1px solid var(--border)",
                      borderRadius: 8,
                      fontSize: 12,
                      color: "var(--text-primary)",
                    }}
                  />
                  <Bar dataKey="single_agent" fill={SERIES.single_agent} maxBarSize={24} radius={[4, 4, 0, 0]} />
                  <Bar dataKey="pipeline" fill={SERIES.pipeline} maxBarSize={24} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      {analysis.length > 0 && (
        <div className="card">
          <div className="card-head">
            <div>
              <h3>Paired analysis</h3>
              <p>
                Baseline → pipeline on the same logs. "Meets proposal" also requires no invented evidence;
                "grounded" must cite the real cause. "Counts right" is the share of stated error counts
                that match the log, from runs scored since count checking was added. p is exact McNemar,
                and below 0.05 is the usual bar.
              </p>
            </div>
          </div>
          <div className="scrollx">
            <table className="data">
              <thead>
                <tr>
                  <th className="mono">model</th>
                  <th style={{ textAlign: "right" }}>requests</th>
                  <th style={{ textAlign: "right" }}>pairs</th>
                  <th style={{ textAlign: "right" }}>accuracy</th>
                  <th style={{ textAlign: "right" }}>p</th>
                  <th style={{ textAlign: "right" }}>meets proposal</th>
                  <th style={{ textAlign: "right" }}>p</th>
                  <th style={{ textAlign: "right" }}>grounded</th>
                  <th style={{ textAlign: "right" }}>p</th>
                  <th style={{ textAlign: "right" }}>counts right</th>
                  <th style={{ textAlign: "right" }}>recovery</th>
                  <th style={{ textAlign: "right" }}>Brier</th>
                  <th style={{ textAlign: "right" }}>tokens in</th>
                  <th style={{ textAlign: "right" }}>tokens out</th>
                </tr>
              </thead>
              <tbody>
                {analysis.map((arm) => (
                  <tr key={`${arm.model}|${arm.request_count}`}>
                    <td className="mono">{shortModel(arm.model)}</td>
                    <td className="num">{arm.request_count ?? "—"}</td>
                    <td className="num">{arm.n_pairs}</td>
                    <td className="num">{pct(arm.single_agent.accuracy.rate)} → {pct(arm.pipeline.accuracy.rate)}</td>
                    <td className="num">{num(arm.paired.correct.p, 3)}</td>
                    <td className="num">{pct(arm.single_agent.meets_proposal.rate)} → {pct(arm.pipeline.meets_proposal.rate)}</td>
                    <td className="num">{num(arm.paired.meets_proposal.p, 3)}</td>
                    <td className="num">{pct(arm.single_agent.grounded.rate)} → {pct(arm.pipeline.grounded.rate)}</td>
                    <td className="num">{num(arm.paired.grounded.p, 3)}</td>
                    <td className="num" title={countsTitle(arm)}>
                      {pct(arm.single_agent.counts?.rate ?? null)} → {pct(arm.pipeline.counts?.rate ?? null)}
                    </td>
                    <td className="num">{pct(arm.single_agent.recovery.rate)} → {pct(arm.pipeline.recovery.rate)}</td>
                    <td className="num">{num(arm.single_agent.brier, 3)} → {num(arm.pipeline.brier, 3)}</td>
                    <td className="num">{Math.round(arm.single_agent.mean_input_tokens ?? 0)} → {Math.round(arm.pipeline.mean_input_tokens ?? 0)}</td>
                    <td className="num">{Math.round(arm.single_agent.mean_output_tokens ?? 0)} → {Math.round(arm.pipeline.mean_output_tokens ?? 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {analysis.some((arm) => Object.keys(arm.others).length > 0) && (
        <div className="card">
          <div className="card-head">
            <div>
              <h3>Additional conditions</h3>
              <p>Compared with the raw-log baseline on the same logs. The tool agent makes several LLM calls.</p>
            </div>
          </div>
          <div className="scrollx">
            <table className="data">
              <thead>
                <tr>
                  <th className="mono">model</th>
                  <th>condition</th>
                  <th style={{ textAlign: "right" }}>pairs</th>
                  <th style={{ textAlign: "right" }}>accuracy</th>
                  <th style={{ textAlign: "right" }}>grounded</th>
                  <th style={{ textAlign: "right" }}>p vs baseline</th>
                  <th style={{ textAlign: "right" }}>LLM calls</th>
                  <th style={{ textAlign: "right" }}>tokens in / out</th>
                </tr>
              </thead>
              <tbody>
                {analysis.flatMap((arm) =>
                  Object.entries(arm.others).map(([name, other]) => (
                    <tr key={`${arm.model}|${arm.request_count}|${name}`}>
                      <td className="mono">{shortModel(arm.model)}</td>
                      <td>{CONDITION_LABELS[name] ?? name}</td>
                      <td className="num">{other.n_pairs}</td>
                      <td className="num">{pct(other.summary.accuracy.rate)}</td>
                      <td className="num">{pct(other.summary.grounded.rate)}</td>
                      <td className="num">{num(other.vs_single.p, 3)}</td>
                      <td className="num">{num(other.summary.mean_llm_calls, 1)}</td>
                      <td className="num">
                        {Math.round(other.summary.mean_input_tokens ?? 0)} / {Math.round(other.summary.mean_output_tokens ?? 0)}
                      </td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {analysis.length > 0 && (
        <div className="card">
          <div className="card-head">
            <div>
              <h3>What went wrong</h3>
              <p>Rule-based labels from the logs and ground truth. A run can have more than one.</p>
            </div>
          </div>
          <div className="scrollx">
            <table className="data">
              <thead>
                <tr>
                  <th className="mono">model</th>
                  <th style={{ textAlign: "right" }}>requests</th>
                  <th>baseline</th>
                  <th>pipeline</th>
                </tr>
              </thead>
              <tbody>
                {analysis.map((arm) => (
                  <tr key={`labels-${arm.model}|${arm.request_count}`}>
                    <td className="mono">{shortModel(arm.model)}</td>
                    <td className="num">{arm.request_count ?? "—"}</td>
                    {[arm.single_agent, arm.pipeline].map((side, index) => (
                      <td key={index}>
                        {Object.entries(side.failure_labels)
                          .map(([label, count]) => `${label.replace(/_/g, " ")} (${count})`)
                          .join(", ") || "none detected"}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {stats && stats.by_arm.length > 0 && (
        <div className="card">
          <div className="card-head">
            <div>
              <h3>Per-arm detail</h3>
              <p>Strict also requires valid JSON. False positives are measured on the `no_fault` logs. Failed API calls are excluded.</p>
            </div>
          </div>
          <div className="scrollx">
            <table className="data">
              <thead>
                <tr>
                  <th>provider</th>
                  <th className="mono">model</th>
                  <th>condition</th>
                  <th style={{ textAlign: "right" }}>n</th>
                  <th style={{ textAlign: "right" }}>failed</th>
                  <th style={{ textAlign: "right" }}>accuracy</th>
                  <th style={{ textAlign: "right" }}>strict</th>
                  <th style={{ textAlign: "right" }}>false pos.</th>
                  <th style={{ textAlign: "right" }}>confidence</th>
                  <th style={{ textAlign: "right" }}>malformed</th>
                  <th style={{ textAlign: "right" }}>evidence</th>
                  <th style={{ textAlign: "right" }}>latency</th>
                </tr>
              </thead>
              <tbody>
                {stats.by_arm.map((row) => {
                  const fpKey = `${row.provider}:${row.model}|${row.condition}`;
                  const fp = stats.false_positive_rate[fpKey];
                  return (
                    <tr key={fpKey}>
                      <td>{row.provider}</td>
                      <td className="mono">{row.model}</td>
                      <td>{row.condition}</td>
                      <td className="num">{row.n}</td>
                      <td className="num">{row.n_failed}</td>
                      <td className="num">{pct(row.accuracy, 1)}</td>
                      <td className="num">{pct(row.strict_accuracy, 1)}</td>
                      <td className="num">{fp === undefined ? "—" : pct(fp, 1)}</td>
                      <td className="num">{num(row.mean_confidence)}</td>
                      <td className="num">{pct(row.malformed_rate, 1)}</td>
                      <td className="num">{row.mean_evidence_score === null ? "—" : pct(row.mean_evidence_score)}</td>
                      <td className="num">{Math.round(row.mean_latency_ms)} ms</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {stats && stats.by_fault.length > 0 && (
        <div className="card">
          <div className="card-head">
            <div>
              <h3>Per-fault-type accuracy</h3>
              <p>`slow_query` is the one with no error lines — it needs real latency reasoning.</p>
            </div>
          </div>
          <div className="scrollx">
            <table className="data">
              <thead>
                <tr>
                  <th className="mono">model</th>
                  <th>condition</th>
                  <th>fault</th>
                  <th style={{ textAlign: "right" }}>n</th>
                  <th style={{ textAlign: "right" }}>accuracy</th>
                  <th style={{ textAlign: "right" }}>confidence</th>
                </tr>
              </thead>
              <tbody>
                {stats.by_fault.map((row, index) => (
                  <tr key={index}>
                    <td className="mono">{shortModel(row.model)}</td>
                    <td>{row.condition}</td>
                    <td className="mono">{row.true_fault}</td>
                    <td className="num">{row.n}</td>
                    <td className="num">{pct(row.accuracy, 1)}</td>
                    <td className="num">{num(row.mean_confidence)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!stats?.by_arm.length && (
        <div className="card">
          <p className="empty">No results yet. Add an arm and run a sweep.</p>
        </div>
      )}
    </div>
  );
}

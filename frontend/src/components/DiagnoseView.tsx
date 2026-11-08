import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  streamSSE,
  type Condition,
  type DiagnosisResult,
  type FaultType,
  type LogDetail,
  type LogInfo,
  type ProviderInfo,
  type RecoveryResult,
  type TraceEvent,
} from "../lib/api";
import { FAULT_COLORS, FAULT_LABELS } from "../lib/theme";
import AgentTrace from "./AgentTrace";
import VerdictCard from "./VerdictCard";

const FAULTS: FaultType[] = ["dependency_down", "slow_query", "pool_exhausted", "no_fault"];

interface ConditionRun {
  condition: Condition;
  events: TraceEvent[];
  result?: DiagnosisResult;
  correct?: boolean;
  trueFault?: FaultType;
  recoveryResult?: RecoveryResult | null;
  error?: string;
}

function LogViewer({ text }: { text: string }) {
  const lines = text.split("\n").slice(0, 400);
  return (
    <pre className="logview">
      {lines.map((line, index) => (
        <div
          key={index}
          className={line.includes("level=ERROR") ? "err" : line.includes("level=WARN") ? "warnline" : undefined}
        >
          {line}
        </div>
      ))}
    </pre>
  );
}

interface Props {
  providers: ProviderInfo[];
  logs: LogInfo[];
  refreshLogs: () => Promise<void>;
}

export default function DiagnoseView({ providers, logs, refreshLogs }: Props) {
  const [provider, setProvider] = useState("mock");
  const [model, setModel] = useState("");
  const [conditionMode, setConditionMode] = useState<"single_agent" | "pipeline" | "both">("both");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<LogDetail | null>(null);
  const [runs, setRuns] = useState<ConditionRun[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [genFault, setGenFault] = useState<FaultType>("pool_exhausted");
  const [genNoise, setGenNoise] = useState(false);
  const [genRequests, setGenRequests] = useState(30);
  const [showTruth, setShowTruth] = useState(true);
  const [recovery, setRecovery] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const providerInfo = providers.find((candidate) => candidate.provider === provider);

  useEffect(() => {
    const preferred = providers.find((candidate) => candidate.available && candidate.provider !== "mock");
    if (preferred) setProvider(preferred.provider);
  }, [providers.length]);

  useEffect(() => {
    setModel(providerInfo?.default_model ?? "");
  }, [provider, providerInfo?.default_model]);

  useEffect(() => {
    if (!selected && logs.length) setSelected(logs[0].name);
  }, [logs, selected]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    api.log(selected).then((value) => !cancelled && setDetail(value)).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const conditions: Condition[] = useMemo(
    () => (conditionMode === "both" ? ["single_agent", "pipeline"] : [conditionMode]),
    [conditionMode],
  );

  async function generate() {
    setError(null);
    try {
      const existing = logs.filter((log) => log.true_fault === genFault && log.noise === genNoise).length;
      const created = await api.generate({
        fault_type: genFault,
        run_id: existing + 1,
        requests: genRequests,
        noise: genNoise,
      });
      await refreshLogs();
      if (created.generated[0]) setSelected(created.generated[0].name);
    } catch (exc) {
      setError(String(exc));
    }
  }

  async function diagnose() {
    if (!selected) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setBusy(true);
    setError(null);
    setRuns(conditions.map((condition) => ({ condition, events: [] })));

    try {
      for (const condition of conditions) {
        await streamSSE<TraceEvent>(
          "/api/diagnose/stream",
          {
            method: "POST",
            body: JSON.stringify({ log_name: selected, condition, provider, model, recovery }),
          },
          (event) => {
            setRuns((current) =>
              current.map((run) => {
                if (run.condition !== condition) return run;
                if (event.type === "error") return { ...run, error: event.message };
                if (event.type === "result") {
                  return {
                    ...run,
                    events: [...run.events, event],
                    result: event.result,
                    correct: event.correct,
                    trueFault: event.true_fault,
                    recoveryResult: event.recovery_result,
                  };
                }
                return { ...run, events: [...run.events, event] };
              }),
            );
          },
          controller.signal,
        );
      }
    } catch (exc) {
      if (!controller.signal.aborted) setError(String(exc));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="cols two">
      <div>
        <div className="card">
          <div className="card-head">
            <div>
              <h3>Inject a fault</h3>
              <p>Deterministic: the same scenario always produces the same log.</p>
            </div>
          </div>
          <div className="stack">
            <div className="chips">
              {FAULTS.map((fault) => (
                <button
                  key={fault}
                  className="chip"
                  aria-pressed={genFault === fault}
                  onClick={() => setGenFault(fault)}
                >
                  {FAULT_LABELS[fault]}
                </button>
              ))}
            </div>
            <div className="row">
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={genNoise}
                  onChange={(event) => setGenNoise(event.target.checked)}
                />
                add noise &amp; red herrings
              </label>
              <label className="field" style={{ width: 110, marginLeft: "auto" }}>
                requests
                <input
                  type="number"
                  min={5}
                  max={400}
                  value={genRequests}
                  onChange={(event) => setGenRequests(Number(event.target.value) || 30)}
                />
              </label>
            </div>
            <div className="row">
              <button onClick={generate}>Generate log</button>
              <button
                onClick={async () => {
                  await api.generate({ suite: true, runs_per_fault: 2, noise: genNoise });
                  await refreshLogs();
                }}
              >
                Generate full set
              </button>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <div>
              <h3>Logs</h3>
              <p>{logs.length} on disk</p>
            </div>
            <label className="toggle">
              <input
                type="checkbox"
                checked={showTruth}
                onChange={(event) => setShowTruth(event.target.checked)}
              />
              show ground truth
            </label>
          </div>
          <div className="loglist">
            {logs.map((log) => (
              <button
                key={log.name}
                className="logitem"
                aria-pressed={selected === log.name}
                onClick={() => setSelected(log.name)}
              >
                <span
                  className="dot"
                  style={{ background: showTruth ? FAULT_COLORS[log.true_fault] : "var(--text-muted)" }}
                />
                <span className="nm">{log.name}</span>
                {log.noise && <span className="badge neutral">noisy</span>}
              </button>
            ))}
            {!logs.length && <p className="empty">No logs yet — generate one above.</p>}
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <div>
              <h3>Run</h3>
              <p>Every condition makes exactly one model call.</p>
            </div>
          </div>
          <div className="stack">
            <label className="field">
              provider
              <select value={provider} onChange={(event) => setProvider(event.target.value)}>
                {providers.map((info) => (
                  <option key={info.provider} value={info.provider} disabled={!info.available}>
                    {info.label}
                    {info.available ? "" : ` — set ${info.env_key}`}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              model
              <select value={model} onChange={(event) => setModel(event.target.value)}>
                {(providerInfo?.models ?? []).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <div className="chips">
              {(["single_agent", "pipeline", "both"] as const).map((mode) => (
                <button
                  key={mode}
                  className="chip"
                  aria-pressed={conditionMode === mode}
                  onClick={() => setConditionMode(mode)}
                >
                  {mode === "both" ? "both (compare)" : mode}
                </button>
              ))}
            </div>
            <label className="toggle">
              <input
                type="checkbox"
                checked={recovery}
                onChange={(event) => setRecovery(event.target.checked)}
              />
              also propose a recovery action (simulated)
            </label>
            <button className="primary" onClick={diagnose} disabled={busy || !selected}>
              {busy ? "Diagnosing…" : "Diagnose"}
            </button>
            {providerInfo && <p className="muted">{providerInfo.note}</p>}
          </div>
        </div>
      </div>

      <div>
        {error && <div className="banner err">{error}</div>}

        {detail && (
          <div className="card">
            <div className="card-head">
              <div>
                <h3 style={{ fontFamily: "var(--mono)", fontSize: 14 }}>{detail.name}</h3>
                <p>
                  {detail.request_count} requests
                  {showTruth && (
                    <>
                      {" · ground truth "}
                      <code style={{ color: FAULT_COLORS[detail.true_fault] }}>{detail.true_fault}</code>
                    </>
                  )}
                </p>
              </div>
            </div>
            <LogViewer text={detail.text} />
          </div>
        )}

        {runs.length > 0 && (
          <div className={runs.length > 1 ? "cols wide" : ""} style={{ marginTop: 16 }}>
            {runs.map((run) => (
              <div key={run.condition}>
                <div className="card">
                  <div className="card-head">
                    <div>
                      <h3>{run.condition}</h3>
                      <p>agent trace</p>
                    </div>
                  </div>
                  <AgentTrace events={run.events} />
                </div>
                {run.error && <div className="banner err">{run.error}</div>}
                {run.result && run.trueFault && (
                  <VerdictCard
                    result={run.result}
                    trueFault={run.trueFault}
                    recoveryResult={run.recoveryResult}
                    correct={Boolean(run.correct)}
                  />
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

import type { DiagnosisResult, FaultType, RecoveryResult } from "../lib/api";
import { CONDITION_LABELS, FAULT_COLORS, SERIES, num, pct } from "../lib/theme";

interface Props {
  result: DiagnosisResult;
  trueFault: FaultType;
  correct: boolean;
  recoveryResult?: RecoveryResult | null;
}

export default function VerdictCard({ result, trueFault, correct, recoveryResult }: Props) {
  const accent = SERIES[result.condition];
  const alternatives = (result.ranked_alternatives ?? []).filter(
    (alt) => alt && typeof alt.fault_type === "string",
  );
  const evidence = result.evidence;

  return (
    <div className="card" style={{ borderTop: `2px solid ${accent}` }}>
      <div className="card-head">
        <div>
          <h3>{CONDITION_LABELS[result.condition]}</h3>
          <p>
            {result.provider}:{result.model} · {result.llm_call_count} LLM call
          </p>
        </div>
        {result.api_error ? (
          <span className="badge bad">call failed</span>
        ) : result.malformed ? (
          <span className="badge warn">malformed output</span>
        ) : correct ? (
          <span className="badge ok">correct</span>
        ) : (
          <span className="badge bad">wrong</span>
        )}
      </div>

      <div className="verdict-top">
        <span
          className="verdict-fault"
          style={{ color: FAULT_COLORS[result.predicted_fault ?? ""] ?? "var(--text-primary)" }}
        >
          {result.predicted_fault ?? "no answer"}
        </span>
        {!correct && (
          <span className="muted">
            ground truth: <code>{trueFault}</code>
          </span>
        )}
      </div>

      {result.parse_error && (
        <p className="banner err" style={{ marginTop: 10 }}>{result.parse_error}</p>
      )}

      <div style={{ marginTop: 12 }}>
        <div className="spread" style={{ marginBottom: 5 }}>
          <span className="muted">confidence</span>
          <span className="muted" style={{ fontVariantNumeric: "tabular-nums" }}>
            {result.confidence === null ? "—" : num(result.confidence)}
          </span>
        </div>
        <div className="meter">
          <span style={{ width: `${(result.confidence ?? 0) * 100}%`, background: accent }} />
        </div>
      </div>

      {result.explanation && (
        <p className="note" style={{ marginTop: 12 }}>{result.explanation}</p>
      )}

      {alternatives.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <h4 style={{ marginBottom: 7 }}>Ranked alternatives</h4>
          <div className="alts">
            {alternatives.map((alt) => (
              <div className="alt" key={alt.fault_type}>
                <code>{alt.fault_type}</code>
                <div className="meter">
                  <span
                    style={{
                      width: `${Math.max(0, Math.min(1, Number(alt.confidence) || 0)) * 100}%`,
                      background: FAULT_COLORS[alt.fault_type] ?? "var(--text-muted)",
                    }}
                  />
                </div>
                <span className="val">{num(Number(alt.confidence) || 0)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ marginTop: 14 }}>
        <h4 style={{ marginBottom: 5 }}>Remediation</h4>
        <p className="note">{result.remediation}</p>
      </div>

      {recoveryResult && (
        <div style={{ marginTop: 14 }}>
          <div className="spread" style={{ marginBottom: 5 }}>
            <h4>Simulated recovery</h4>
            <span className={`badge ${recoveryResult.valid ? "ok" : "bad"}`}>
              {recoveryResult.valid ? "valid" : "invalid"}
            </span>
          </div>
          <p className="note">
            <code>{recoveryResult.action ?? "no plan"}</code>
            {recoveryResult.target ? (
              <>
                {" on "}
                <code>{recoveryResult.target}</code>
              </>
            ) : null}
            : {recoveryResult.reason}.
          </p>
        </div>
      )}

      <div style={{ marginTop: 14 }}>
        <h4 style={{ marginBottom: 5 }}>Evidence check</h4>
        {evidence && evidence.claims_checked > 0 ? (
          <>
            <p className="note">
              {evidence.claims_supported} of {evidence.claims_checked} verifiable claims appear in
              the log ({pct(evidence.score)}).
            </p>
            {evidence.unsupported.length > 0 && (
              <ul className="muted" style={{ margin: "5px 0 0", paddingLeft: 18 }}>
                {evidence.unsupported.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            No independently verifiable claim was made.
          </p>
        )}
      </div>

      <dl className="kv" style={{ marginTop: 14 }}>
        <dt>tokens</dt>
        <dd>
          {result.input_tokens} in / {result.output_tokens} out
        </dd>
        <dt>model latency</dt>
        <dd>{Math.round(result.llm_latency_ms)} ms</dd>
        <dt>agents</dt>
        <dd style={{ fontSize: 11.5 }}>{result.agent_call_log.join(" → ")}</dd>
      </dl>
    </div>
  );
}

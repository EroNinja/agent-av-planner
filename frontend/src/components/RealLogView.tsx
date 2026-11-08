import { useEffect, useState } from "react";
import { api, type ProviderInfo, type RealLogPreview, type RealLogResult } from "../lib/api";
import { num } from "../lib/theme";

export default function RealLogView({ providers }: { providers: ProviderInfo[] }) {
  const [text, setText] = useState("");
  const [provider, setProvider] = useState("mock");
  const [model, setModel] = useState("");
  const [preview, setPreview] = useState<RealLogPreview | null>(null);
  const [result, setResult] = useState<RealLogResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const info = providers.find((candidate) => candidate.provider === provider);

  useEffect(() => {
    setModel(info?.default_model ?? "");
  }, [provider, info?.default_model]);

  function changed(next: string) {
    setText(next);
    setPreview(null);
    setResult(null);
  }

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  }

  const redacted = preview ? Object.entries(preview.redactions) : [];
  const leavesMachine = provider !== "mock";

  return (
    <div className="cols wide">
      <div className="card">
        <div className="card-head">
          <div>
            <h3>Your log</h3>
            <p>JSON lines, key=value or plain text, up to 5 MB. Nothing is saved.</p>
          </div>
          <input
            type="file"
            aria-label="Upload a log file"
            accept=".log,.txt,.json,.jsonl"
            style={{ width: 220 }}
            onChange={async (event) => {
              const file = event.target.files?.[0];
              if (file) changed(await file.text());
            }}
          />
        </div>
        <textarea
          className="logbox"
          aria-label="Log text"
          spellCheck={false}
          placeholder="Paste log lines here"
          value={text}
          onChange={(event) => changed(event.target.value)}
        />
        <div className="row" style={{ marginTop: 10 }}>
          <button disabled={busy || !text.trim()} onClick={() => run(async () => setPreview(await api.realLogPreview(text)))}>
            Check what will be sent
          </button>
        </div>

        {preview && (
          <div className="stack" style={{ marginTop: 14 }}>
            <p className="note">
              {redacted.length
                ? `Removed before sending: ${redacted.map(([kind, count]) => `${kind.replace(/_/g, " ")} (${count})`).join(", ")}.`
                : "No sensitive patterns were detected. Automatic redaction can miss things, so read the sample."}
            </p>
            <dl className="kv">
              <dt>lines</dt>
              <dd>{preview.summary.total_lines}</dd>
              <dt>error lines</dt>
              <dd>{preview.summary.error_lines}</dd>
              <dt>formats</dt>
              <dd>{Object.entries(preview.summary.line_formats).map(([k, v]) => `${k} ${v}`).join(", ")}</dd>
            </dl>
            <h4>Sample the model will see</h4>
            <pre className="json">{preview.sample.join("\n")}</pre>
          </div>
        )}
      </div>

      <div>
        <div className="card">
          <div className="card-head">
            <div>
              <h3>First-pass diagnosis</h3>
              <p>Advisory only. Nothing here changes a system, and it is not part of the experiment.</p>
            </div>
          </div>
          <div className="stack">
            <label className="field">
              provider
              <select value={provider} onChange={(event) => setProvider(event.target.value)}>
                {providers.map((item) => (
                  <option key={item.provider} value={item.provider} disabled={!item.available}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              model
              <select value={model} onChange={(event) => setModel(event.target.value)}>
                {(info?.models ?? []).map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </label>
            {leavesMachine && (
              <p className="note">The redacted summary and sample will be sent to {info?.label ?? provider}.</p>
            )}
            <button
              className="primary"
              disabled={busy || !text.trim() || !preview}
              onClick={() => run(async () => setResult(await api.realLogDiagnose(text, provider, model)))}
            >
              {busy ? "Working…" : preview ? "Diagnose" : "Check what will be sent first"}
            </button>
          </div>
        </div>

        {error && <div className="banner err">{error}</div>}
        {result?.error && <div className="banner err">{result.error}</div>}

        {result && !result.error && (
          <div className="card">
            {result.needs_human && (
              <p className="banner info">
                Low confidence or unconfirmed evidence. Treat this as a lead and have a person investigate.
              </p>
            )}
            <div className="stack">
              {(result.hypotheses ?? []).map((item, index) => (
                <div key={index}>
                  <div className="spread">
                    <code>{item.label}</code>
                    <span className="muted">confidence {num(item.confidence)}</span>
                  </div>
                  <p className="note">{item.cause}</p>
                  {item.evidence.map((quote, quoteIndex) => (
                    <p key={quoteIndex} className="keynote">
                      <span className={`badge ${quote.found_in_log ? "ok" : "bad"}`}>
                        {quote.found_in_log ? "in log" : "not found"}
                      </span>{" "}
                      <code>{quote.quote}</code>
                    </p>
                  ))}
                </div>
              ))}
              {!!result.next_checks?.length && (
                <div>
                  <h4>Suggested checks</h4>
                  <ul className="note" style={{ paddingLeft: 18, margin: "4px 0 0" }}>
                    {result.next_checks.map((check) => (
                      <li key={check}>{check}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

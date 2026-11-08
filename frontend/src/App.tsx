import { useCallback, useEffect, useState } from "react";
import { api, type LogInfo, type ProviderInfo } from "./lib/api";
import CompareView from "./components/CompareView";
import DiagnoseView from "./components/DiagnoseView";
import KeysDialog from "./components/KeysDialog";
import RealLogView from "./components/RealLogView";
import RunsView from "./components/RunsView";

type Tab = "diagnose" | "compare" | "runs" | "real";

const TABS: { id: Tab; label: string }[] = [
  { id: "diagnose", label: "Diagnose" },
  { id: "compare", label: "Compare" },
  { id: "runs", label: "Runs" },
  { id: "real", label: "Real log" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("diagnose");
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [logs, setLogs] = useState<LogInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [keysOpen, setKeysOpen] = useState(false);

  const refreshProviders = useCallback(() => {
    api
      .providers(true)
      .then((response) => setProviders(response.providers))
      .catch((exc) => setError(String(exc)));
  }, []);

  const refreshLogs = useCallback(async () => {
    try {
      setLogs((await api.logs()).logs);
    } catch (exc) {
      setError(String(exc));
    }
  }, []);

  useEffect(() => {
    refreshProviders();
    refreshLogs();
  }, [refreshProviders, refreshLogs]);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>Incident Response</h1>
          <small>controlled fault injection → multi-agent diagnosis</small>
        </div>

        <nav className="tabs" role="tablist">
          {TABS.map((item) => (
            <button
              key={item.id}
              className="tab"
              role="tab"
              aria-selected={tab === item.id}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="topbar-right">
          {providers.map((info) => (
            <span key={info.provider} title={info.available ? "key present" : `set ${info.env_key}`}>
              <span className={`keydot ${info.available ? "on" : "off"}`} />
              {info.label}
            </span>
          ))}
          <button onClick={() => setKeysOpen(true)}>API keys</button>
        </div>
      </header>

      <main>
        {error && <div className="banner err">{error}</div>}

        {tab === "diagnose" && (
          <DiagnoseView providers={providers} logs={logs} refreshLogs={refreshLogs} />
        )}
        {tab === "compare" && <CompareView providers={providers} logs={logs} />}
        {tab === "runs" && <RunsView />}
        {tab === "real" && <RealLogView providers={providers} />}
      </main>

      <KeysDialog open={keysOpen} onClose={() => setKeysOpen(false)} onChanged={refreshProviders} />
    </div>
  );
}

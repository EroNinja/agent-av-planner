import { useEffect, useRef, useState } from "react";
import { api, type KeyEntry, type KeyProvider } from "../lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}

type Notice = { ok: boolean; message: string };

function ProviderKeys({
  info,
  apply,
}: {
  info: KeyProvider;
  apply: (next: KeyProvider[]) => void;
}) {
  const [label, setLabel] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [notices, setNotices] = useState<Record<string, Notice>>({});
  const [formError, setFormError] = useState<string | null>(null);

  async function run(action: () => Promise<{ providers: KeyProvider[] }>) {
    setBusy(true);
    setFormError(null);
    try {
      apply((await action()).providers);
      return true;
    } catch (exc) {
      setFormError(String(exc).replace(/^Error: \d+: /, ""));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function save(activate: boolean) {
    const saved = await run(() => api.addKey({ provider: info.provider, label, value, activate }));
    if (saved) {
      setLabel("");
      setValue("");
    }
  }

  async function test(entry: KeyEntry) {
    setBusy(true);
    setNotices((current) => ({ ...current, [entry.id]: { ok: true, message: "Checking…" } }));
    try {
      const result = await api.testKey(info.provider, entry.id);
      setNotices((current) => ({ ...current, [entry.id]: result }));
    } catch (exc) {
      setNotices((current) => ({ ...current, [entry.id]: { ok: false, message: String(exc) } }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="keygroup">
      <h3>{info.label}</h3>

      {info.keys.length === 0 && <p className="muted">No key saved.</p>}

      <ul className="keylist">
        {info.keys.map((entry) => (
          <li key={entry.id}>
            <div className="keyrow">
              <div className="keyname">
                <span>{entry.label}</span>
                <code>{entry.masked}</code>
              </div>
              {entry.active ? (
                <span className="badge ok">in use</span>
              ) : (
                <button disabled={busy} onClick={() => run(() => api.activateKey(info.provider, entry.id))}>
                  Use
                </button>
              )}
              <button disabled={busy} onClick={() => test(entry)}>
                Test
              </button>
              {confirming === entry.id ? (
                <button
                  className="danger"
                  disabled={busy}
                  onClick={async () => {
                    await run(() => api.removeKey(info.provider, entry.id));
                    setConfirming(null);
                  }}
                >
                  Confirm remove
                </button>
              ) : (
                <button disabled={busy} onClick={() => setConfirming(entry.id)}>
                  Remove
                </button>
              )}
            </div>
            {confirming === entry.id && entry.active && (
              <p className="keynote bad">
                This key is in use. Removing it leaves {info.label} with no key.
              </p>
            )}
            {notices[entry.id] && (
              <p className={`keynote ${notices[entry.id].ok ? "" : "bad"}`}>{notices[entry.id].message}</p>
            )}
          </li>
        ))}
      </ul>

      <div className="keyform">
        <label className="field">
          name (optional)
          <input type="text" maxLength={40} value={label} onChange={(event) => setLabel(event.target.value)} />
        </label>
        <label className="field">
          new {info.label} key
          <input
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </label>
        <div className="row">
          <button className="primary" disabled={busy || !value.trim()} onClick={() => save(true)}>
            Save and use
          </button>
          <button disabled={busy || !value.trim()} onClick={() => save(false)}>
            Save only
          </button>
        </div>
        {formError && <p className="keynote bad">{formError}</p>}
      </div>
    </section>
  );
}

export default function KeysDialog({ open, onClose, onChanged }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [providers, setProviders] = useState<KeyProvider[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setError(null);
      dialog.showModal();
      api
        .keys()
        .then((response) => setProviders(response.providers))
        .catch((exc) => setError(String(exc)));
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  function apply(next: KeyProvider[]) {
    setProviders(next);
    onChanged();
  }

  return (
    <dialog ref={dialogRef} className="keysdialog" aria-labelledby="keys-title" onClose={onClose}>
      <div className="card-head">
        <div>
          <h2 id="keys-title">API keys</h2>
          <p>
            Stored only on this computer, in plain text, and never shown again in full. Changes apply to
            new runs; a sweep already running keeps its key.
          </p>
        </div>
        <button aria-label="Close" onClick={onClose}>
          Close
        </button>
      </div>

      {error && <div className="banner err">{error}</div>}

      {providers.map((info) => (
        <ProviderKeys key={info.provider} info={info} apply={apply} />
      ))}
    </dialog>
  );
}

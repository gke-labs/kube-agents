import { useCallback, useEffect, useRef, useState } from "react";
import { useReducer } from "react";
import { reduce, initialState } from "./model.ts";
import { startBus, type BusHandle } from "./bus.ts";
import {
  DEFAULT_USER,
  DEFAULT_WS_URL,
  loadConfig,
  saveConfig,
  scrubPasswordFromUrl,
  type BusConfig,
} from "./config.ts";
import Chat from "./Chat.tsx";
import Rail from "./Rail.tsx";
import "./styles.css";

/**
 * First load with no credentials shows this instead of a dead page. The
 * password is the install's `web-password` key; the recipe to fetch it and
 * start the port-forward is in the README and repeated here so the form is
 * self-explanatory in front of an audience.
 */
function ConnectForm({
  error,
  onConnect,
}: {
  error: string | null;
  onConnect: (config: BusConfig) => void;
}) {
  const params = new URLSearchParams(window.location.search);
  const [url, setUrl] = useState(params.get("ws") ?? DEFAULT_WS_URL);
  const [pass, setPass] = useState("");

  return (
    <form
      className="connect-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (pass !== "") onConnect({ url, user: DEFAULT_USER, pass });
      }}
    >
      <h1>a2a bus</h1>
      <p className="connect-hint">
        kubectl port-forward the NATS websocket port, then paste the install&apos;s
        <code> web-password</code>.
      </p>
      <label>
        websocket url
        <input value={url} onChange={(e) => setUrl(e.target.value)} />
      </label>
      <label>
        web password
        <input
          type="password"
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          autoFocus
        />
      </label>
      <button type="submit">connect</button>
      {error && <p className="connect-error">{error}</p>}
    </form>
  );
}

export default function App() {
  const [state, dispatch] = useReducer(reduce, initialState);
  const [config, setConfig] = useState<BusConfig | null>(loadConfig);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [probePending, setProbePending] = useState(false);
  const busHandleRef = useRef<BusHandle | null>(null);

  // Effect, not the state initializer: this mutates history, and StrictMode
  // double-invokes initializers.
  useEffect(scrubPasswordFromUrl, []);

  useEffect(() => {
    if (!config) return;
    // StrictMode runs this effect twice in dev, and cleanup fires before the
    // first `startBus` resolves — without this flag the first connection is
    // never closed and every envelope gets dispatched twice.
    let cancelled = false;

    void (async () => {
      try {
        const handle = await startBus(config, dispatch);
        if (cancelled) {
          void handle.close().catch(() => {
            /* already going away */
          });
          return;
        }
        busHandleRef.current = handle;
        saveConfig(config);
      } catch (error) {
        console.error("Failed to connect to bus:", error);
        if (!cancelled) {
          setConnectError(String(error));
          setConfig(null);
        }
      }
    })();

    return () => {
      cancelled = true;
      busHandleRef.current?.close().catch(() => {
        /* ignore */
      });
      busHandleRef.current = null;
    };
  }, [config]);

  const handleProbe = useCallback(() => {
    if (!busHandleRef.current || probePending) return;
    setProbePending(true);
    // Result arrives through the reducer as a probe event; errors land there too.
    void busHandleRef.current
      .probeReadOnly()
      .catch((error) => {
        console.error("Probe failed:", error);
      })
      .finally(() => setProbePending(false));
  }, [probePending]);

  if (!config) {
    return <ConnectForm error={connectError} onConnect={setConfig} />;
  }

  return (
    <div className="app">
      <div className="app-topology">
        <Rail state={state} />
      </div>
      <div className="app-chat">
        <Chat
          entries={state.chat}
          user={config.user}
          probe={state.probe}
          probePending={probePending}
          onProbe={handleProbe}
        />
      </div>
    </div>
  );
}

import { useCallback, useEffect, useReducer, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { reduce, initialState, type UiState } from "./model.ts";
import { durablesFor, startBus, type BusHandle } from "./bus.ts";
import {
  DEFAULT_USER,
  DEFAULT_WS_URL,
  READ_ONLY_USER,
  loadConfig,
  loadConversation,
  saveConfig,
  saveConversation,
  scrubPasswordFromUrl,
  type BusConfig,
} from "./config.ts";
import { mintConversation } from "./console.ts";
import { commandEffect, type Command } from "./commands.ts";
import { BAND_STEP, bandFromPointer, clampBand, loadBand, saveBand } from "./band.ts";
import StatusStrip, { type PanelFocus, type PanelKey } from "./StatusStrip.tsx";
import Dashboard from "./Dashboard.tsx";
import SessionTranscript from "./SessionTranscript.tsx";
import Chat from "./Chat.tsx";
import "./styles.css";

const PERCENT = 100;
/**
 * Shown, and the box left alone, when Enter is pressed while the bus link is
 * down. Refusing here rather than publishing keeps the turn from being
 * silently dropped by nats.ws's own reconnect bookkeeping (bus.ts's file
 * doc comment) with no record it ever happened.
 */
const LINK_DOWN_SEND_NOTE = "not sent: the bus link is down. Your text is still in the box.";

/**
 * First load with no credentials shows this instead of a dead page. The
 * password is the install's `console-password` key; the recipe to fetch it and
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
  // `?user=web` must reach the form's own submit, not silently connect as
  // console with the web password pasted into it: the two credentials
  // authorize different grants and the server refuses the mismatch.
  const user = params.get("user") ?? DEFAULT_USER;

  return (
    <form
      className="connect-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (pass !== "") onConnect({ url, user, pass });
      }}
    >
      <h1>a2a bus</h1>
      <p className="connect-hint">
        kubectl port-forward the NATS websocket port, then paste the install&apos;s
        <code> {user}-password</code>.
      </p>
      <label>
        websocket url
        <input value={url} onChange={(e) => setUrl(e.target.value)} />
      </label>
      <label>
        {user} password
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
  const [conversation, setConversation] = useState<string>(() => loadConversation() ?? mintConversation());
  const [session, setSession] = useState<string | null>(null);
  const [focus, setFocus] = useState<PanelFocus | null>(null);
  const [band, setBand] = useState(loadBand);
  const busHandleRef = useRef<BusHandle | null>(null);
  const mainRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  // The pollers and the command handler read the latest state without
  // restarting the bus on every render.
  const stateRef = useRef<UiState>(state);
  stateRef.current = state;
  const conversationRef = useRef(conversation);
  conversationRef.current = conversation;

  const canSend = config !== null && config.user !== READ_ONLY_USER;

  useEffect(scrubPasswordFromUrl, []);
  useEffect(() => saveConversation(conversation), [conversation]);
  useEffect(() => saveBand(band), [band]);

  useEffect(() => {
    if (!config) return;
    // StrictMode runs this effect twice in dev, and cleanup fires before the
    // first `startBus` resolves - without this flag the first connection is
    // never closed and every envelope gets dispatched twice.
    let cancelled = false;
    const opts = {
      conversation: config.user !== READ_ONLY_USER ? conversationRef.current : undefined,
      durables: () => durablesFor(stateRef.current.agents.values()),
    };

    void (async () => {
      try {
        const handle = await startBus(config, dispatch, opts);
        if (cancelled) {
          void handle.close().catch(() => {
            /* already going away */
          });
          return;
        }
        busHandleRef.current = handle;
        // A /new typed while startBus was dialing changed the conversation
        // after opts captured it.
        if (opts.conversation !== undefined && conversationRef.current !== opts.conversation) {
          handle.setConversation(conversationRef.current);
        }
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

  const local = useCallback((text: string) => dispatch({ type: "local", text, at: Date.now() }), []);

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

  const handleSend = useCallback(
    (text: string): boolean => {
      const handle = busHandleRef.current;
      if (handle === null) {
        local(`not sent: not connected to the bus yet. "${text}"`);
        return false;
      }
      // Refuse rather than publish while the link is down: nats.ws buffers a
      // publish made mid-reconnect and then drops it on the next dial
      // attempt (bus.ts's file doc comment), so a send here would look
      // pending and then vanish with no record. Chat only clears the box
      // when this returns true, so the text survives to be sent again.
      if (stateRef.current.connection !== "up") {
        local(LINK_DOWN_SEND_NOTE);
        return false;
      }
      handle.send(text);
      return true;
    },
    [local],
  );

  const handleCommand = useCallback(
    (command: Command) => {
      const effect = commandEffect(command, stateRef.current);
      switch (effect.kind) {
        case "local":
          local(effect.text);
          return;
        case "clear":
          dispatch({ type: "clear" });
          return;
        case "replay":
          setSession(effect.session);
          return;
        case "new": {
          const next = mintConversation();
          setConversation(next);
          busHandleRef.current?.setConversation(next);
          local(`new conversation ${next}. The next message starts a fresh session.`);
          return;
        }
      }
    },
    [local],
  );

  const handleFocus = useCallback((key: PanelKey) => {
    setSession(null);
    setFocus((f) => ({ key, seq: (f?.seq ?? 0) + 1 }));
  }, []);

  const bandFrom = (e: PointerEvent<HTMLDivElement>) => {
    const box = mainRef.current?.getBoundingClientRect();
    if (box) setBand(bandFromPointer(e.clientY, box.top, box.height));
  };

  const bandKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowUp") setBand((b) => clampBand(b - BAND_STEP));
    else if (e.key === "ArrowDown") setBand((b) => clampBand(b + BAND_STEP));
    else return;
    e.preventDefault();
  };

  if (!config) {
    return <ConnectForm error={connectError} onConnect={setConfig} />;
  }

  return (
    <div className="app">
      <StatusStrip state={state} onFocus={handleFocus} />
      <div className="app-main" ref={mainRef}>
        <div className="app-body" style={{ flexBasis: `${band * PERCENT}%` }}>
          {session === null ? (
            <Dashboard state={state} focus={focus} onSession={setSession} />
          ) : (
            <SessionTranscript state={state} session={session} onBack={() => setSession(null)} />
          )}
        </div>
        <div
          className="app-band"
          role="separator"
          aria-orientation="horizontal"
          aria-label="resize the dashboard and the chat"
          aria-valuenow={Math.round(band * PERCENT)}
          tabIndex={0}
          onPointerDown={(e) => {
            dragging.current = true;
            e.currentTarget.setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => dragging.current && bandFrom(e)}
          onPointerUp={(e) => {
            dragging.current = false;
            e.currentTarget.releasePointerCapture(e.pointerId);
          }}
          onKeyDown={bandKey}
        />
        <div className="app-chat">
          <Chat
            entries={state.chat}
            user={config.user}
            conversation={canSend ? conversation : undefined}
            probe={state.probe}
            probePending={probePending}
            onProbe={handleProbe}
            onSend={canSend ? handleSend : undefined}
            onCommand={canSend ? handleCommand : undefined}
          />
        </div>
      </div>
    </div>
  );
}

/**
 * The bus rail: the demo's centrepiece, lifted intact — instrument trace,
 * hairline leads, live envelopes as charges travelling toward the observer.
 * Colour is spent on two things only: which conversation a pulse belongs to
 * (`corrColor`), and one amber warning for a broken browser link. What
 * changed underneath is the protocol: 0.4 kinds shape the pulses, liveness
 * ripples come from stream activity (there are no heartbeats on this bus),
 * and the hub tap is the a2a gateway.
 *
 * Animation state lives in refs, never in the reducer: the rAF loop reads
 * `state.pulses` through an id watermark and the reducer never learns that
 * the loop exists.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { Kind } from "./protocol.ts";
import {
  WEB_SESSION,
  corrColor,
  type AgentView,
  type ConnectionState,
  type UiState,
} from "./model.ts";
import {
  RAIL_PAD_LEFT,
  RAIL_PAD_RIGHT,
  buildGhost,
  layoutTaps,
  type GhostStep,
  type TapPosition,
} from "./rail-layout.ts";

const RAIL_Y = 54;
const LEAD_LEN = 28;
const NAME_Y = RAIL_Y + LEAD_LEN + 15;
const STATUS_Y = NAME_Y + 14;
const STRIP_Y = 132;
const SVG_HEIGHT = 168;
const FALLBACK_WIDTH = 1200;

/** One pulse's flight time, end to end. */
const PULSE_MS = 900;
/** Gap between ghost-replay steps — fast enough to read as a burst. */
const GHOST_STEP_MS = 120;
const RIPPLE_MS = 1100;
/** How long a closed agent's tap hangs on the rail before it is dropped. */
const CLOSE_FADE_MS = 2000;
const MAX_FLIGHTS = 64;
const NAME_MAX = 18;

/** Bigger payload, bigger charge: the eye should sort kinds without a legend. */
const PULSE_SHAPE: Record<Kind, { r: number; wake: number }> = {
  "artifact-update": { r: 5.5, wake: 96 },
  message: { r: 4.5, wake: 76 },
  "topic-update": { r: 4.5, wake: 66 },
  "agent-card": { r: 4, wake: 56 },
  "agent-closed": { r: 4, wake: 56 },
  cancel: { r: 3.5, wake: 50 },
  "status-update": { r: 3, wake: 46 },
};

interface Flight {
  key: string;
  x0: number;
  x1: number;
  r: number;
  wake: number;
  color: string;
  ghost: boolean;
  start: number;
}

interface Ripple {
  session: string;
  start: number;
}

interface GhostRun {
  session: string;
  steps: GhostStep[];
  index: number;
}

function prefersReducedMotion(): boolean {
  return globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

function shortName(session: string): string {
  return session.length > NAME_MAX ? `${session.slice(0, NAME_MAX - 1)}…` : session;
}

/** The observer's tap reports the browser's own link, not an agent lifecycle. */
function linkText(connection: ConnectionState): string {
  switch (connection) {
    case "up":
      return "websocket";
    case "connecting":
      return "connecting";
    case "down":
      return "link down";
  }
}

function statusText(
  tap: TapPosition,
  agent: AgentView | undefined,
  connection: ConnectionState,
): string {
  if (tap.kind === "you") return linkText(connection);
  if (!agent) return "no traffic yet";
  if (agent.statusLine) return agent.statusLine;
  switch (agent.status) {
    case "active":
      return tap.kind === "gateway" ? "routing" : "working";
    case "idle":
      return "idle";
    case "done":
      return "done";
    case "closed":
      return "closed";
  }
}

/** Lays the replay chips out end to end, dropping any that run off the rail. */
function chipRow(steps: GhostStep[], railEnd: number) {
  const row: { step: GhostStep; i: number; x: number; w: number }[] = [];
  let x = RAIL_PAD_LEFT;
  steps.forEach((step, i) => {
    const w = Math.round(step.label.length * 5.6 + 18);
    if (x + w > railEnd) return;
    row.push({ step, i, x, w });
    x += w + 8;
  });
  return row;
}

export default function Rail({ state }: { state: UiState }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  const [, paint] = useReducer((n: number) => n + 1, 0);
  const [retireVersion, retire] = useReducer((n: number) => n + 1, 0);
  const [ghost, setGhost] = useState<GhostRun | null>(null);
  const reduced = useMemo(prefersReducedMotion, []);

  const flightsRef = useRef<Flight[]>([]);
  const ripplesRef = useRef<Ripple[]>([]);
  const watermarkRef = useRef(0);
  const activityRef = useRef(new Map<string, number>());
  const closedAtRef = useRef(new Map<string, number>());
  const rafRef = useRef<number | null>(null);

  // --- measure ---------------------------------------------------------------
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const measure = () => setWidth(Math.max(480, Math.round(host.clientWidth)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  // --- who is on the rail ----------------------------------------------------
  // Closed agents keep their slot while they fade, so the rail needs a nudge to
  // re-render once the fade is over. Poll only while something is actually
  // fading — the reducer keeps closed agents around forever, so "is closed"
  // alone would leave a timer running for the rest of the session.
  useEffect(() => {
    const now = performance.now();
    let fading = false;
    for (const [session, agent] of state.agents) {
      if (agent.status !== "closed") {
        closedAtRef.current.delete(session);
        continue;
      }
      if (!closedAtRef.current.has(session)) closedAtRef.current.set(session, now);
      if (now - (closedAtRef.current.get(session) ?? now) < CLOSE_FADE_MS) fading = true;
    }
    if (!fading) return;
    const timer = setTimeout(retire, 300);
    return () => clearTimeout(timer);
  }, [state.agents, retireVersion]);

  const visible = useMemo(() => {
    const now = performance.now();
    return [...state.agents.values()].filter((agent) => {
      if (agent.status !== "closed") return true;
      const at = closedAtRef.current.get(agent.session);
      return at === undefined || now - at < CLOSE_FADE_MS;
    });
    // `retireVersion` is the timer's way of saying "re-check the fade clock".
  }, [state.agents, retireVersion]);

  const taps = useMemo(() => layoutTaps(visible, width), [visible, width]);
  const tapX = useMemo(() => new Map(taps.map((t) => [t.session, t.x])), [taps]);

  // --- the animation loop ----------------------------------------------------
  const runLoop = useCallback(() => {
    if (rafRef.current !== null) return;
    const step = () => {
      const now = performance.now();
      flightsRef.current = flightsRef.current.filter((f) => now - f.start < PULSE_MS);
      ripplesRef.current = ripplesRef.current.filter((r) => now - r.start < RIPPLE_MS);
      // Only ever runs while something is actually moving. A ghost replay is a
      // chain of setTimeouts, so the loop parks itself between steps and each
      // `launch` wakes it again.
      const busy = flightsRef.current.length > 0 || ripplesRef.current.length > 0;
      rafRef.current = busy ? requestAnimationFrame(step) : null;
      paint();
    };
    rafRef.current = requestAnimationFrame(step);
  }, []);

  useEffect(
    () => () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    },
    [],
  );

  const launch = useCallback(
    (from: string, kind: Kind, color: string, ghostly: boolean, key: string) => {
      const edge = width - RAIL_PAD_RIGHT;
      const x0 = tapX.get(from) ?? edge;
      // Everything reports to the observer; the observer publishes nothing on
      // this bus, so unlike the demo there is no counterflow case.
      const x1 = tapX.get(WEB_SESSION) ?? RAIL_PAD_LEFT;
      const shape = PULSE_SHAPE[kind];
      flightsRef.current.push({
        key,
        x0,
        x1,
        r: ghostly ? 2.5 : shape.r,
        wake: ghostly ? 30 : shape.wake,
        color,
        ghost: ghostly,
        start: performance.now(),
      });
      if (flightsRef.current.length > MAX_FLIGHTS) {
        flightsRef.current = flightsRef.current.slice(-MAX_FLIGHTS);
      }
      runLoop();
    },
    [runLoop, tapX, width],
  );

  // Pulses are consumed by id watermark, not by timestamp: pod clocks skew, but
  // the reducer's ids only ever climb.
  useEffect(() => {
    let launched = false;
    for (const pulse of state.pulses) {
      if (pulse.id <= watermarkRef.current) continue;
      watermarkRef.current = pulse.id;
      launch(pulse.fromSession, pulse.kind, corrColor(pulse.correlationId), false, `p${pulse.id}`);
      launched = true;
    }
    if (launched) runLoop();
  }, [state.pulses, launch, runLoop]);

  // Liveness ripples come from `lastActivity` moving — stream traffic is the
  // only pulse this bus has. The first value seen is skipped so attaching to
  // a stream's history doesn't ripple the whole fleet at once.
  useEffect(() => {
    let rippled = false;
    for (const [session, agent] of state.agents) {
      if (agent.lastActivity === undefined) continue;
      const seen = activityRef.current.get(session);
      activityRef.current.set(session, agent.lastActivity);
      if (seen === undefined || seen === agent.lastActivity) continue;
      ripplesRef.current.push({ session, start: performance.now() });
      rippled = true;
    }
    if (rippled) runLoop();
  }, [state.agents, runLoop]);

  // --- ghost replay ----------------------------------------------------------
  const startGhost = useCallback(
    (session: string) => {
      const steps = buildGhost(state, session);
      if (steps.length === 0) return;
      setGhost({ session, steps, index: -1 });
    },
    [state],
  );

  // `launch` is rebuilt whenever tapX changes, which is every envelope from
  // any session (touchAgent replaces state.agents). Depending on it here
  // would tear down and restart the step timeout on each one, so on a bus
  // busier than one envelope per GHOST_STEP_MS the index never advances and
  // the strip stays dark while the footer reads "replaying". The clock keys
  // on the run's own position and reaches `launch` through a ref.
  const launchRef = useRef(launch);
  launchRef.current = launch;

  const ghostSession = ghost?.session;
  const ghostIndex = ghost?.index ?? -1;
  const ghostLast = ghost ? ghost.steps.length - 1 : -1;

  useEffect(() => {
    if (ghostSession === undefined) return;
    if (ghostIndex >= ghostLast) {
      const done = setTimeout(() => setGhost(null), PULSE_MS + 300);
      return () => clearTimeout(done);
    }
    const next = setTimeout(() => {
      setGhost((g) => {
        if (!g || g.session !== ghostSession || g.index !== ghostIndex) return g;
        const i = ghostIndex + 1;
        launchRef.current(g.session, g.steps[i].kind, "", true, `g${g.session}-${i}`);
        return { ...g, index: i };
      });
    }, GHOST_STEP_MS);
    return () => clearTimeout(next);
  }, [ghostSession, ghostIndex, ghostLast]);

  // --- render ----------------------------------------------------------------
  const now = performance.now();
  const railEnd = width - RAIL_PAD_RIGHT;

  const flights = flightsRef.current.map((f) => {
    const p = Math.min(1, Math.max(0, (now - f.start) / PULSE_MS));
    const x = reduced ? f.x0 : f.x0 + (f.x1 - f.x0) * p;
    const dir = f.x1 >= f.x0 ? 1 : -1;
    const wake = reduced ? 0 : f.wake * Math.min(1, p * 4);
    const opacity = Math.min(1, p / 0.08) * Math.min(1, (1 - p) / 0.25) * (f.ghost ? 0.5 : 1);
    return { ...f, x, dir, wake, opacity };
  });

  const ripplePhase = new Map<string, number>();
  for (const r of ripplesRef.current) {
    ripplePhase.set(r.session, Math.min(1, (now - r.start) / RIPPLE_MS));
  }

  const ghostCorr = ghost?.steps[Math.max(0, ghost.index)]?.correlationId ?? "";

  return (
    <div className="rail-panel" ref={hostRef}>
      <div className="rail-head">
        <span className="rail-title">NATS JetStream</span>
        <span className="rail-subject">a2a.&gt; · read-only</span>
      </div>

      {/* role="group", never "img": the taps inside are real buttons, and an
          img role would flatten them out of the accessibility tree while
          leaving them tab-focusable. */}
      <svg
        className="rail-svg"
        width={width}
        height={SVG_HEIGHT}
        viewBox={`0 0 ${width} ${SVG_HEIGHT}`}
        role="group"
        aria-label="NATS bus topology"
      >
        <defs>
          <filter id="rail-glow" x="-60%" y="-400%" width="220%" height="900%">
            <feGaussianBlur stdDeviation="3.2" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <line
          className="rail-trace-halo"
          x1={RAIL_PAD_LEFT}
          x2={railEnd}
          y1={RAIL_Y}
          y2={RAIL_Y}
        />
        <line className="rail-trace" x1={RAIL_PAD_LEFT} x2={railEnd} y1={RAIL_Y} y2={RAIL_Y} />
        <line
          className="rail-terminator"
          x1={RAIL_PAD_LEFT}
          x2={RAIL_PAD_LEFT}
          y1={RAIL_Y - 6}
          y2={RAIL_Y + 6}
        />
        <line
          className="rail-terminator"
          x1={railEnd}
          x2={railEnd}
          y1={RAIL_Y - 6}
          y2={RAIL_Y + 6}
        />

        {taps.map((tap) => {
          const agent = state.agents.get(tap.session);
          // A broken link borrows the amber look — this tap is not telling
          // you the truth right now.
          const status =
            tap.kind === "you"
              ? state.connection === "up"
                ? "active"
                : "stale"
              : (agent?.status ?? "absent");
          const phase = ripplePhase.get(tap.session);
          const clickable = tap.kind !== "you" && agent !== undefined;
          return (
            // Two groups on purpose: the outer one carries the x position as an
            // attribute, the inner one owns the CSS transform the lifecycle
            // animations drive — a CSS transform would otherwise replace the
            // attribute and snap the tap to x=0.
            <g key={tap.session} transform={`translate(${tap.x} 0)`}>
            <g
              className={`tap tap-${tap.kind} tap-${status}${clickable ? " tap-clickable" : ""}`}
              role={clickable ? "button" : undefined}
              tabIndex={clickable ? 0 : undefined}
              aria-label={clickable ? `Replay ${tap.session} events` : undefined}
              onClick={clickable ? () => startGhost(tap.session) : undefined}
              onKeyDown={
                clickable
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        startGhost(tap.session);
                      }
                    }
                  : undefined
              }
            >
              <line className="tap-lead" x1={0} x2={0} y1={RAIL_Y} y2={RAIL_Y + LEAD_LEN} />
              {phase !== undefined && (
                <circle
                  className="tap-ripple"
                  cx={0}
                  cy={RAIL_Y}
                  r={5 + phase * 16}
                  opacity={(1 - phase) * 0.5}
                />
              )}
              {tap.kind === "you" ? (
                <rect className="tap-port" x={-4.5} y={RAIL_Y - 4.5} width={9} height={9} />
              ) : (
                <circle className="tap-dot" cx={0} cy={RAIL_Y} r={tap.kind === "gateway" ? 5 : 4} />
              )}
              {tap.kind === "gateway" && <circle className="tap-hub" cx={0} cy={RAIL_Y} r={8.5} />}
              <text className="tap-name" x={0} y={NAME_Y} textAnchor="middle">
                {shortName(tap.session)}
              </text>
              <text className="tap-status" x={0} y={STATUS_Y} textAnchor="middle">
                {statusText(tap, agent, state.connection)}
              </text>
            </g>
            </g>
          );
        })}

        {flights.map((f) => (
          <g key={f.key} className={f.ghost ? "pulse pulse-ghost" : "pulse"} opacity={f.opacity}>
            {f.wake > 1 && (
              <line
                className="pulse-wake"
                x1={f.x - f.dir * f.wake}
                x2={f.x}
                y1={RAIL_Y}
                y2={RAIL_Y}
                stroke={f.ghost ? undefined : f.color}
                strokeWidth={Math.max(1, f.r * 0.9)}
              />
            )}
            <circle
              className="pulse-head"
              cx={f.x}
              cy={RAIL_Y}
              r={f.r}
              fill={f.ghost ? undefined : f.color}
              filter={f.ghost ? undefined : "url(#rail-glow)"}
            />
          </g>
        ))}

        {ghost && (
          <g className="ghost-strip">
            {chipRow(ghost.steps, railEnd).map(({ step, i, x, w }) => (
              <g key={`${step.label}-${i}`} className={i <= ghost.index ? "chip chip-on" : "chip"}>
                <rect x={x} y={STRIP_Y} width={w} height={18} rx={2} />
                <text x={x + 8} y={STRIP_Y + 13}>
                  {step.label}
                </text>
              </g>
            ))}
          </g>
        )}
      </svg>

      <div className="rail-foot">
        <span className="rail-count">
          {state.streamsUp}/{state.streamsTotal} streams · {state.streamMsgCount.toLocaleString()} msgs
        </span>
        {ghost && (
          <span className="rail-replay">▓▓░ replaying {ghostCorr.slice(0, 8) || ghost.session}</span>
        )}
      </div>
    </div>
  );
}

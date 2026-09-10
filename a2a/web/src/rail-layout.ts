/**
 * Pure geometry and derivations for the bus rail. No React, no clock, no DOM —
 * everything here is a function of `UiState` plus the pixel width the rail was
 * measured at, so the interesting parts of the visualisation are unit-testable.
 *
 * The rail reads left to right as a signal path: the observer (`you`) is
 * soldered on at the left, the gateway next to it, and every worker taps in
 * further down the trace in the order it first spoke. Lifted from the demo
 * unchanged except for names: the hub is the a2a gateway now, and the kinds
 * are 0.4's.
 */
import type { Kind } from "./protocol.ts";
import { GATEWAY_SESSION, WEB_SESSION, type UiState } from "./model.ts";

/** Clear space at each end of the trace. */
export const RAIL_PAD_LEFT = 64;
export const RAIL_PAD_RIGHT = 64;
/** `you` → `gateway`: the fixed head of the rail. */
export const GATEWAY_GAP = 180;
/** `gateway` → the first worker: a wider gap, so the fleet reads as a group. */
export const WORKER_GAP = 230;
/** Worker spacing while there is room for it. */
export const WORKER_PITCH = 160;
/** Below this, labels collide — the rail sheds its oldest taps instead. */
export const MIN_WORKER_PITCH = 84;
/** Width the fixed gaps are drawn at full size; narrower rails scale them down. */
const REFERENCE_SPAN = 900;
const MIN_GAP_SCALE = 0.45;
/** A ghost replay longer than this stops being legible and starts being a wait. */
export const MAX_GHOST_STEPS = 24;

export type TapKind = "you" | "gateway" | "worker";

/** Structural: any list of things with a session name can be laid out. */
export interface TapAgent {
  session: string;
}

export interface TapPosition {
  session: string;
  kind: TapKind;
  /** Pixel x on the rail, integral so hairlines stay crisp. */
  x: number;
  /** True when this tap sits closer than its natural spacing. */
  compressed: boolean;
}

/**
 * Fixed `you`/`gateway` taps, then workers left to right in arrival order.
 *
 * Positions are absolute rather than distributed: a tap that has been placed
 * never moves when a later one arrives, so the rail grows to the right instead
 * of reshuffling under the audience's eyes. Only when the fleet outgrows the
 * trace does the pitch tighten, and past the minimum pitch the rail keeps the
 * newest workers and drops the oldest off the left of the group.
 */
export function layoutTaps(agents: readonly TapAgent[], width: number): TapPosition[] {
  const span = Math.max(0, width - RAIL_PAD_LEFT - RAIL_PAD_RIGHT);
  const scale = Math.min(1, Math.max(MIN_GAP_SCALE, span / REFERENCE_SPAN));
  const youX = RAIL_PAD_LEFT;
  const gatewayX = youX + GATEWAY_GAP * scale;
  const workerStart = gatewayX + WORKER_GAP * scale;

  const taps: TapPosition[] = [
    { session: WEB_SESSION, kind: "you", x: Math.round(youX), compressed: scale < 1 },
    { session: GATEWAY_SESSION, kind: "gateway", x: Math.round(gatewayX), compressed: scale < 1 },
  ];

  const workers = agents.filter(
    (a) => a.session !== GATEWAY_SESSION && a.session !== WEB_SESSION,
  );
  if (workers.length === 0) return taps;

  const room = Math.max(0, width - RAIL_PAD_RIGHT - workerStart);
  const capacity = Math.max(1, Math.floor(room / MIN_WORKER_PITCH) + 1);
  const shown = workers.length > capacity ? workers.slice(workers.length - capacity) : workers;
  const pitch =
    shown.length > 1
      ? Math.max(MIN_WORKER_PITCH, Math.min(WORKER_PITCH, room / (shown.length - 1)))
      : WORKER_PITCH;
  const compressed = pitch < WORKER_PITCH || shown.length < workers.length;

  shown.forEach((worker, i) => {
    taps.push({
      session: worker.session,
      kind: "worker",
      x: Math.round(workerStart + i * pitch),
      compressed,
    });
  });

  return taps;
}

export interface GhostStep {
  kind: Kind;
  /** Short label for the timeline strip, e.g. `status · completed`. */
  label: string;
  correlationId: string;
}

const KIND_LABEL: Record<Kind, string> = {
  message: "message",
  "status-update": "status",
  "artifact-update": "artifact",
  cancel: "cancel",
  "agent-card": "card",
  "agent-closed": "closed",
  "topic-update": "topic",
};

/**
 * The event sequence a tap click replays. Nothing is re-fetched: live traffic
 * is already in `pulses`, and after a page reload — where history rebuilt the
 * state but left no pulses behind — the sequence is reconstructed from the
 * tasks and artifacts that history did leave. Replay by history is the
 * `tasks/get` story made visible: no live executor was asked.
 */
export function buildGhost(state: UiState, session: string): GhostStep[] {
  const observed = state.pulses
    .filter((p) => p.fromSession === session)
    .map((p) => ({
      kind: p.kind,
      label: KIND_LABEL[p.kind],
      correlationId: p.correlationId,
    }));
  // The tail, not the head: the last thing a task did — the terminal status —
  // is the point of replaying it.
  if (observed.length > 0) return observed.slice(-MAX_GHOST_STEPS);

  const tasks = [...state.tasks.values()].filter(
    (t) => t.executor === session || t.owner === session,
  );
  if (tasks.length === 0) return [];

  const steps: GhostStep[] = [];
  for (const task of tasks) {
    const corr = task.correlationId;
    steps.push({ kind: "status-update", label: "status · submitted", correlationId: corr });
    for (const artifact of task.artifacts.values()) {
      steps.push({
        kind: "artifact-update",
        label: `artifact · ${artifact.name}${artifact.chunks > 1 ? ` ×${artifact.chunks}` : ""}`,
        correlationId: corr,
      });
    }
    steps.push({ kind: "status-update", label: `status · ${task.state}`, correlationId: corr });
  }
  return steps.slice(-MAX_GHOST_STEPS);
}

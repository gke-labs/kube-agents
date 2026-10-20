/**
 * Pure views over UiState for the strip, the panels and the local commands.
 * Every failure state gets a sentence here, so a component never renders a
 * blank where something went wrong.
 */
import { POLL_MS } from "./bus.ts";
import {
  durationMs,
  queueMs,
  type AgentStatus,
  type ChatEntry,
  type ConversationView,
  type LivenessReport,
  type StreamAttachView,
  type StreamStatView,
  type TaskView,
  type UiState,
} from "./model.ts";
import { STREAMS, TERMINAL_STATES, usageTotal, type Usage } from "./protocol.ts";

/** A capacity fraction at or past this turns the tile amber. */
export const CAPACITY_WARN = 0.8;
/** A standing consumer with no pull and no delivery for this long reads as quiet. */
export const QUIET_MS = 120_000;
/** A liveness report older than this many polls is stale data, not a live signal.  Wide enough that a cycle slowed by a request timeout or two does not flag every session. */
export const LIVENESS_STALE_POLLS = 5;
/** `LIVENESS_STALE_POLLS` * the poller's own interval. */
export const LIVENESS_STALE_MS = LIVENESS_STALE_POLLS * POLL_MS;

/** What the spend tile and panel say before any task has reported a turn's usage. */
export const SPEND_EMPTY_REASON =
  "no task has reported usage yet: each executor publishes a usage artifact when a turn finishes (the worker adapter from the harness result line, the hermes-bridge from the API turn's answer); the bridge's CLI executor reports duration alone, and cost is carried only by a harness that prices its own turns";
/** The tasks table's cost cell for a task whose turns reported nothing, and its title. */
export const SPEND_NONE = "—";
export const SPEND_TASK_NONE_REASON =
  "this task's turns reported no usage: its executor published no usage artifact, or none of its parts carried a figure";
export const KV_TBD_REASON =
  "tbd: the only KV size read (STREAM.INFO on the KV_ streams) also lists every key, so the console identity doesn't have it";

const KIB = 1024;
const UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];
const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const FAILED_STATES = ["failed", "rejected"];

export function fmtBytes(n: number): string {
  let v = n;
  let u = 0;
  while (v >= KIB && u < UNITS.length - 1) {
    v /= KIB;
    u++;
  }
  if (u === 0) return `${v} B`;
  const shown = v >= 10 ? Math.round(v).toString() : (Math.round(v * 10) / 10).toString();
  return `${shown} ${UNITS[u]}`;
}

export function fmtDuration(ms: number): string {
  if (ms < SECOND) return `${Math.round(ms)}ms`;
  if (ms < MINUTE) return `${Math.floor(ms / SECOND)}s`;
  if (ms < HOUR) return `${Math.floor(ms / MINUTE)}m ${Math.floor((ms % MINUTE) / SECOND)}s`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)}h ${Math.floor((ms % HOUR) / MINUTE)}m`;
  return `${Math.floor(ms / DAY)}d ${Math.floor((ms % DAY) / HOUR)}h`;
}

/** The retention horizon `max_age` sets: "keeps 7d" or "no age limit" for zero. */
export function fmtRetention(maxAgeMs: number): string {
  return maxAgeMs > 0 ? `keeps ${fmtDuration(maxAgeMs)}` : "no age limit";
}

export function fmtAgo(at: number, now: number): string {
  const ms = now - at;
  return ms <= 0 ? "just now" : `${fmtDuration(ms)} ago`;
}

export interface Capacity {
  stream: string;
  /** null when nothing is limited, or nothing is known. */
  fraction: number | null;
  limitedBy: "bytes" | "consumers" | null;
  warn: boolean;
  text: string;
}

function limited(max: number): boolean {
  return max > 0;
}

export function capacityOf(
  stream: string,
  view: StreamStatView | undefined,
  attach: StreamAttachView | undefined,
  now: number,
): Capacity {
  const none = { stream, fraction: null, limitedBy: null, warn: false };
  if (attach !== undefined && attach.error !== null) {
    return { ...none, text: `${stream} not attached since ${fmtAgo(attach.since, now)}: ${attach.error}` };
  }
  if (view === undefined) return { ...none, text: `${stream}: no stream info yet` };
  if (view.stat === null) return { ...none, text: `${stream}: stream info failed: ${view.error ?? "unknown error"}` };
  const s = view.stat;
  const byBytes = limited(s.maxBytes) ? s.bytes / s.maxBytes : null;
  const byConsumers = limited(s.maxConsumers) ? s.consumers / s.maxConsumers : null;
  const horizon = s.firstTs !== undefined ? `, oldest ${fmtAgo(s.firstTs, now)}` : "";
  const msgs = `${s.msgs} msgs${horizon}, ${fmtRetention(s.maxAgeMs)}`;
  if (byBytes === null && byConsumers === null) {
    return { ...none, text: `${stream}: ${fmtBytes(s.bytes)}, ${s.consumers} consumers, no limit set (${msgs})` };
  }
  const useConsumers = byConsumers !== null && (byBytes === null || byConsumers > byBytes);
  const fraction = (useConsumers ? byConsumers : byBytes) as number;
  const detail = useConsumers
    ? `${s.consumers} of ${s.maxConsumers} consumers`
    : `${fmtBytes(s.bytes)} of ${fmtBytes(s.maxBytes)}`;
  return {
    stream,
    fraction,
    limitedBy: useConsumers ? "consumers" : "bytes",
    warn: fraction >= CAPACITY_WARN,
    text: `${stream}: ${Math.round(fraction * 100)}% - ${detail} (${msgs})`,
  };
}

export function capacities(state: UiState): Capacity[] {
  return STREAMS.map((s) => capacityOf(s, state.streamStats.get(s), state.streamAttach.get(s), state.now));
}

/** The fullest limited stream, or null if none has a limit. */
export function worstCapacity(state: UiState): Capacity | null {
  let worst: Capacity | null = null;
  for (const c of capacities(state)) {
    if (c.fraction === null) continue;
    if (worst === null || c.fraction > (worst.fraction as number)) worst = c;
  }
  return worst;
}

export interface TypeSummary {
  agentType: string;
  count: number;
  lastActivity?: number;
  pulses: number;
}

export function typesOf(state: UiState): TypeSummary[] {
  const byType = new Map<string, TypeSummary>();
  for (const a of state.agents.values()) {
    const prev = byType.get(a.agentType) ?? { agentType: a.agentType, count: 0, pulses: 0 };
    const last = Math.max(prev.lastActivity ?? 0, a.lastActivity ?? 0);
    byType.set(a.agentType, { ...prev, count: prev.count + 1, ...(last > 0 ? { lastActivity: last } : {}) });
  }
  return [...byType.values()]
    .map((t) => ({ ...t, pulses: state.typePulses.get(t.agentType) ?? 0 }))
    .sort((a, b) => a.agentType.localeCompare(b.agentType));
}

function newestFirst(a: TaskView, b: TaskView): number {
  return (b.endedAt ?? b.lastEventAt) - (a.endedAt ?? a.lastEventAt);
}

export function tasksInFlight(state: UiState): TaskView[] {
  return [...state.tasks.values()].filter((t) => !TERMINAL_STATES.includes(t.state)).sort(newestFirst);
}

export function failuresOf(state: UiState): TaskView[] {
  return [...state.tasks.values()].filter((t) => FAILED_STATES.includes(t.state)).sort(newestFirst);
}

export function recentTasks(state: UiState, limit: number): TaskView[] {
  return [...state.tasks.values()].sort(newestFirst).slice(0, limit);
}

/** The newest unfinished task this session executes or owns. */
export function currentTaskOf(state: UiState, session: string): TaskView | undefined {
  return tasksInFlight(state).find((t) => t.executor === session || t.owner === session);
}

export function taskTimes(t: TaskView): string {
  const q = queueMs(t);
  const d = durationMs(t);
  return [q !== undefined ? `queued ${fmtDuration(q)}` : "", d !== undefined ? `ran ${fmtDuration(d)}` : ""]
    .filter(Boolean)
    .join(", ");
}

const THOUSAND = 1000;
const MILLION = 1_000_000;
/** Cents matter under a dollar, so a sub-dollar cost keeps four places. */
const COST_SMALL_DECIMALS = 4;
const COST_DECIMALS = 2;

/** One decimal, rounded first, so a value that rounds up to the next band is printed in it (999,950 is 1M, not 1000k). */
function oneDecimal(v: number): string {
  return (Math.round(v * 10) / 10).toFixed(1).replace(/\.0$/, "");
}

export function fmtTokens(n: number): string {
  if (Math.round(n) < THOUSAND) return `${Math.round(n)}`;
  const k = oneDecimal(n / THOUSAND);
  if (Number(k) < THOUSAND) return `${k}k`;
  return `${oneDecimal(n / MILLION)}M`;
}

/** The smallest cost the four-place form prints; a positive cost below it is shown as under that, never as $0.0000. */
const COST_FLOOR = 10 ** -COST_SMALL_DECIMALS;

export function fmtCost(usd: number): string {
  if (usd > 0 && usd < COST_FLOOR / 2) return `<$${COST_FLOOR.toFixed(COST_SMALL_DECIMALS)}`;
  return `$${usd.toFixed(usd >= 1 ? COST_DECIMALS : COST_SMALL_DECIMALS)}`;
}

/** The token counts a usage has: "1.2k in / 80 out", "80 out", or "" when it has neither. */
export function fmtTokenCounts(u: Usage): string {
  return [
    u.inputTokens !== undefined ? `${fmtTokens(u.inputTokens)} in` : "",
    u.outputTokens !== undefined ? `${fmtTokens(u.outputTokens)} out` : "",
  ]
    .filter(Boolean)
    .join(" / ");
}

/** One line for a usage: the figures it has, in a fixed order, nothing for the ones it lacks. */
export function fmtUsage(u: Usage): string {
  const parts: string[] = [];
  const counts = fmtTokenCounts(u);
  if (counts !== "") parts.push(counts);
  if (u.costUsd !== undefined) parts.push(fmtCost(u.costUsd));
  if (u.durationMs !== undefined) parts.push(fmtDuration(u.durationMs));
  if (u.model) parts.push(u.model);
  return parts.join(", ");
}

/** The tasks table's cost cell: what the task's turns reported, or a dash. */
export function taskSpend(t: TaskView): string {
  return t.usage ? fmtUsage(t.usage) : SPEND_NONE;
}

export interface Spend {
  /** Tasks whose turns reported usage. */
  reported: number;
  /** Of those, the tasks whose turns carried a cost; the cost sum covers these alone. */
  priced: number;
  /** Of those, the tasks whose turns carried token counts; the token sums cover these alone. */
  counted: number;
  /**
   * The distinct models the reporting tasks named, in first-seen order. A
   * task's own fold keeps the last model its turns named; across tasks the
   * sums cover every model, so the panel names them all rather than one.
   */
  models: string[];
  /** Their figures summed; absent until one reports. It names no model: the sum covers every task, so `models` holds them. */
  total?: Usage;
}

export function spendOf(state: UiState): Spend {
  const usages = [...state.tasks.values()].flatMap((t) => (t.usage ? [t.usage] : []));
  const total = usageTotal(usages);
  return {
    reported: usages.length,
    priced: usages.filter((u) => u.costUsd !== undefined).length,
    counted: usages.filter((u) => u.inputTokens !== undefined || u.outputTokens !== undefined).length,
    models: [...new Set(usages.flatMap((u) => (u.model ? [u.model] : [])))],
    total: total === undefined ? undefined : { ...total, model: undefined },
  };
}

/** "over 1 of 3 tasks" when a sum covers fewer tasks than reported, else "". */
export function spendCoverage(part: number, reported: number): string {
  return part < reported ? ` over ${part} of ${reported} tasks` : "";
}

/**
 * The strip's spend tile: the cost when any task priced a turn, the tokens
 * when any counted them, the time when that is all there is, and why it is
 * empty before any of that. A sum that covers fewer tasks than reported says
 * so, since an unpriced task adds nothing rather than zero.
 */
export function spendTile(state: UiState): { text: string; title: string } {
  const { reported, priced, counted, total } = spendOf(state);
  if (total === undefined) return { text: "none yet", title: SPEND_EMPTY_REASON };
  const tasks = `${reported} task${reported === 1 ? "" : "s"} reported usage`;
  if (total.costUsd !== undefined) {
    return { text: fmtCost(total.costUsd), title: `${tasks}: ${fmtCost(total.costUsd)}${spendCoverage(priced, reported)}` };
  }
  if (total.inputTokens !== undefined || total.outputTokens !== undefined) {
    const tok = `${fmtTokens((total.inputTokens ?? 0) + (total.outputTokens ?? 0))} tok`;
    return { text: tok, title: `${tasks}: ${tok}${spendCoverage(counted, reported)}` };
  }
  return { text: fmtDuration(total.durationMs ?? 0), title: `${tasks}: ${fmtUsage(total)}` };
}

export type LivenessKind = "live" | "quiet" | "gone" | "idle" | "unknown" | "error" | "finished" | "stale";

export interface Liveness {
  kind: LivenessKind;
  text: string;
  /** The full sentence, when `text` is shortened for the table. */
  title?: string;
}

/**
 * `status` and `checkedAt` keep a finished or link-starved session from
 * reading as live forever. A `done`/`closed` session's `-in` consumer is
 * gone the moment it retires (durablesFor stops polling it), so the last
 * report is stale from the instant it is taken and must never be shown as
 * current. A report older than `LIVENESS_STALE_MS` is the same problem for
 * any session: the poller dispatches nothing while the link is down, so an
 * old "live - pulling" would otherwise freeze on the page for the whole
 * outage.  An error report ages out the same way, since the last poll before
 * a link drops is the one most likely to have timed out.
 */
export function livenessOf(
  report: LivenessReport | undefined,
  now: number,
  hasTaskInFlight: boolean,
  status: AgentStatus,
): Liveness {
  if (status === "done" || status === "closed") {
    return { kind: "finished", text: "finished - no consumer expected" };
  }
  if (report === undefined) {
    return { kind: "unknown", text: "no consumer known", title: "no consumer known for this session" };
  }
  if (now - report.checkedAt > LIVENESS_STALE_MS) {
    return { kind: "stale", text: `last checked ${fmtAgo(report.checkedAt, now)}` };
  }
  if (report.error !== undefined) {
    return { kind: "error", text: `could not check consumer ${report.durable}: ${report.error}` };
  }
  if (!report.found) {
    // A worker's -in consumer exists only while it runs a task (5s inactive
    // threshold), so missing with nothing in flight is the normal idle state.
    if (report.perTask && !hasTaskInFlight) return { kind: "idle", text: "idle - no task, no consumer" };
    return { kind: "gone", text: `consumer ${report.durable} not found on ${report.stream}` };
  }
  if (report.waiting > 0) return { kind: "live", text: "live - pulling" };
  if (report.lastActive !== undefined && now - report.lastActive < QUIET_MS) {
    return { kind: "live", text: `live - delivered ${fmtAgo(report.lastActive, now)}` };
  }
  const since = report.lastActive !== undefined ? `since ${fmtAgo(report.lastActive, now)}` : "with no delivery on record";
  return { kind: "quiet", text: `quiet ${since} - no pull outstanding` };
}

export function conversationsByBackend(
  state: UiState,
): Array<{ backend: string; conversations: ConversationView[] }> {
  const groups = new Map<string, ConversationView[]>();
  for (const c of state.conversations.values()) {
    groups.set(c.backend, [...(groups.get(c.backend) ?? []), c]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([backend, list]) => ({ backend, conversations: list.sort((a, b) => b.lastSeen - a.lastSeen) }));
}

/** The transcript entries one session produced or was asked for. */
export function sessionEntries(state: UiState, session: string): ChatEntry[] {
  const own = new Set(
    [...state.tasks.values()].filter((t) => t.executor === session || t.owner === session).map((t) => t.taskId),
  );
  return state.chat.filter((c) => c.session === session || (c.taskId !== undefined && own.has(c.taskId)));
}

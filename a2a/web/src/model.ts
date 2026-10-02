/**
 * The UI's brain: one immutable state tree and one pure reducer over bus
 * events. Everything the panes render is derived from here, and nothing in
 * here reads the clock or the network — timestamps always arrive on the event
 * so replay and live traffic reduce identically. Lifted from the demo UI and
 * reworked for a2a-jetstream/0.4: kinds changed (`task` → `message`,
 * `message-chunk` is gone), the transcript is driven by the four reserved
 * artifact names, and there are no heartbeats — the `web` user's read surface
 * is `a2a.>` only, and nothing on the install publishes heartbeats yet, so
 * liveness is derived from stream traffic.
 */
import {
  ARTIFACT_PROGRESS,
  ARTIFACT_RESULT,
  TERMINAL_STATES,
  authorityOf,
  partsText,
  type Artifact,
  type ArtifactUpdate,
  type Envelope,
  type Message,
  type StatusUpdate,
  type SubjectInfo,
  type TaskState,
} from "./protocol.ts";
import { turnFate, type ConsoleOutFrame } from "./console.ts";

/** The chatops gateway's session name (a2a/gateway/gateway.go). */
export const GATEWAY_SESSION = "gateway";
/** No traffic for longer than this and a standing agent reads as idle. */
export const IDLE_MS = 60_000;
/** A sent turn with no submission on TASKS after this long gets a note. */
export const PENDING_STALE_MS = 30_000;
/**
 * A pending turn this old leaves the attach set; its line keeps its note.
 * Some turns never become a task (a status question, a refusal, a dropped
 * frame) and the page cannot tell which, so without a bound they would sit
 * in `pending` for the life of the tab.
 */
export const PENDING_EXPIRE_MS = 10 * 60_000;
/** Local lines (command output) share one correlation group. */
export const LOCAL_CORRELATION = "local";
const STALE_NOTE_GATEWAY =
  "no task on the bus for this turn yet. Status questions and refused turns are answered by a gateway notice instead of a task; if no notice came, the gateway may be slow or may have dropped it";
/**
 * The link itself was down when this fired, so the gateway is not the likely
 * cause the way `STALE_NOTE_GATEWAY` implies — this turn may never have left
 * the browser at all.
 */
const STALE_NOTE_LINK_DOWN =
  "no submission on the bus 30s after sending, and the bus link is down right now - that is the likely reason this turn never showed up";

/** Picks the stale note by whether the link is up, so it never blames the gateway for a loss the link itself caused. */
function staleNote(connection: ConnectionState): string {
  return connection === "up" ? STALE_NOTE_GATEWAY : STALE_NOTE_LINK_DOWN;
}

export type AgentStatus = "active" | "idle" | "done" | "closed";

/** The browser's own link to the bus, which is not an agent lifecycle. */
export type ConnectionState = "connecting" | "up" | "down";

export interface AgentView {
  session: string;
  agentType: string;
  profile?: string;
  status: AgentStatus;
  /** ms since epoch, parsed from the last envelope's own `ts`. */
  lastActivity?: number;
  /** Latest `progress` artifact note, shown under the agent's tap. */
  statusLine?: string;
  /**
   * True when this session has answered as the addressee of its own task
   * subject — a per-session worker pod, which retires to `done` on terminal.
   * A standing service (the bridge answers for `platform` under the session
   * `platform-bridge`) goes back to idle instead.
   */
  perTask?: boolean;
}

export interface ArtifactView {
  name: string;
  text: string;
  chunks: number;
}

export interface TaskView {
  taskId: string;
  contextId: string;
  correlationId: string;
  /** The subject's addressee token: who this task is for. */
  addressee: string;
  /** The session that submitted it. */
  owner: string;
  /** The session that answered (first events publisher), once one has. */
  executor?: string;
  state: TaskState;
  final: boolean;
  artifacts: Map<string, ArtifactView>;
  /** ms since epoch of the last event, from envelope ts. */
  lastEventAt: number;
  /** authority.requester.backend on the submission, when it carried one. */
  backend?: string;
  /** authority.audience.conversation on the submission. */
  conversation?: string;
  /** ms, envelope ts of the submission. */
  askAt?: number;
  /** ms, envelope ts of the first status-update from anyone but the gateway. */
  firstStatusAt?: number;
  /** An executor published `submitted` for this task. Both executors do. */
  sawSubmitted?: boolean;
  /** ms, envelope ts of the event that first made the task terminal. */
  endedAt?: number;
  /** The message text on that terminal status: why the task ended. */
  reason?: string;
}

export interface AnomalyCounts {
  /** `to` disagreed with the subject's addressee. */
  addressee: number;
  /** An event arrived after the task's final one. */
  postFinal: number;
  /** A task went terminal without its executor ever saying `submitted`. */
  missingSubmitted: number;
}

export interface ConversationView {
  conversation: string;
  backend: string;
  /** ms, envelope ts of the newest turn. */
  lastSeen: number;
  turns: number;
}

export interface TopicView {
  key: string;
  topic: string;
  owner?: string;
  summary: string;
  /** ms, envelope ts. */
  at: number;
  publisher: string;
}

/** ms from the ask to the executor's first status, once both are known. */
export function queueMs(t: TaskView): number | undefined {
  if (t.askAt === undefined || t.firstStatusAt === undefined) return undefined;
  return Math.max(0, t.firstStatusAt - t.askAt);
}

/** ms from the executor's first status (or the ask) to the terminal event. */
export function durationMs(t: TaskView): number | undefined {
  const start = t.firstStatusAt ?? t.askAt;
  if (t.endedAt === undefined || start === undefined) return undefined;
  return Math.max(0, t.endedAt - start);
}

/** One key per topic subject: the owner's name scopes agent topics. */
export function topicKey(subject: SubjectInfo, fallback: string): string {
  if (subject.plane !== "topics") return fallback;
  return subject.owner !== undefined ? `${subject.owner}/${subject.topic}` : subject.topic;
}

export type ChatKind =
  | "user"
  | "steer"
  | "answer"
  | "progress"
  | "status"
  | "topic"
  | "cancel"
  | "anomaly"
  /** Sent from this page, not yet seen on TASKS. */
  | "pending"
  /** Sent from this page and settled at once: the gateway never makes a task of it (a stop word, a bare `/session`). */
  | "sent"
  /** A frame the gateway posted on this conversation's `.out` subject. */
  | "notice"
  /** Produced by the page itself: command output, a send that never left. */
  | "local";

export interface ChatEntry {
  id: string;
  kind: ChatKind;
  session?: string;
  text: string;
  correlationId: string;
  taskId?: string;
  /** A sentence about the entry's delivery, shown under it. */
  note?: string;
}

export type ProbeOutcome = "refused" | "sent" | "error";

export interface ProbeResult {
  outcome: ProbeOutcome;
  detail: string;
  at: number;
}

/** One CONSUMER.INFO answer for a durable the page knows a session by. */
export interface LivenessReport {
  session: string;
  durable: string;
  stream: string;
  /** The consumer exists only while its worker runs a task. */
  perTask: boolean;
  found: boolean;
  /** Pull requests outstanding: a process is waiting on the consumer right now. */
  waiting: number;
  pending: number;
  /** ms, the consumer's last delivery. */
  lastActive?: number;
  /** Set when the lookup itself failed (not for not-found). */
  error?: string;
  checkedAt: number;
}

export interface StreamStat {
  bytes: number;
  /** -1 or 0 means unlimited. */
  maxBytes: number;
  msgs: number;
  consumers: number;
  /** -1 or 0 means unlimited. */
  maxConsumers: number;
  /** ms, the oldest retained message. */
  firstTs?: number;
  /** 0 means no age limit. */
  maxAgeMs: number;
}

export interface StreamStatView {
  stat: StreamStat | null;
  error?: string;
  at: number;
}

export interface StreamAttachView {
  /** null while attached. */
  error: string | null;
  /** ms, when the current state (attached, or failing) began. */
  since: number;
}

export interface PendingTurn {
  messageId: string;
  /** Trimmed, exactly as sent. */
  text: string;
  /**
   * The texts the gateway's submission may carry for this turn: the sent
   * text, plus the stripped task of a `delegate` or `/session` turn
   * (console.ts turnFate).
   */
  texts: string[];
  conversation: string;
  at: number;
  stale: boolean;
  /**
   * A gateway notice reached this conversation after the turn was sent. The
   * notice may have been this turn's answer (a full queue, a refusal), so a
   * resend of the same words matches ahead of it.
   */
  noticed: boolean;
}

export interface UiState {
  agents: Map<string, AgentView>;
  tasks: Map<string, TaskView>;
  chat: ChatEntry[];
  streamMsgCount: number;
  connection: ConnectionState;
  /** JetStream taps attached, out of `streamsTotal`. */
  streamsUp: number;
  streamsTotal: number;
  /** Latest read-only probe result, if one was run. */
  probe?: ProbeResult;
  /** Live envelopes seen per `from.agentType`. The strip's LED keys off it. */
  typePulses: Map<string, number>;
  anomalies: AnomalyCounts;
  conversations: Map<string, ConversationView>;
  topics: Map<string, TopicView>;
  /** The last tick's wall clock, ms. Zero until the first tick. */
  now: number;
  liveness: Map<string, LivenessReport>;
  streamStats: Map<string, StreamStatView>;
  streamAttach: Map<string, StreamAttachView>;
  pending: PendingTurn[];
  localSeq: number;
}

export type BusEvent =
  /** `at` is the browser-clock receive time; `env.ts` is the publisher's. */
  | { type: "envelope"; env: Envelope; subject: SubjectInfo; live: boolean; at: number }
  | { type: "tick"; now: number }
  | { type: "connection"; state: ConnectionState }
  | { type: "streams"; up: number; total: number }
  | { type: "probe"; result: ProbeResult }
  | { type: "liveness"; report: LivenessReport }
  | { type: "streamStat"; name: string; stat: StreamStat | null; error?: string; at: number }
  | { type: "streamAttach"; stream: string; error: string | null; at: number }
  | { type: "consoleSent"; messageId: string; text: string; conversation: string; at: number }
  | { type: "sendFailed"; messageId: string; error: string }
  | { type: "notice"; frame: ConsoleOutFrame; conversation: string; at: number }
  | { type: "local"; text: string; at: number }
  | { type: "clear" };

export const initialState: UiState = {
  agents: new Map(),
  tasks: new Map(),
  chat: [],
  streamMsgCount: 0,
  connection: "connecting",
  streamsUp: 0,
  streamsTotal: 0,
  typePulses: new Map(),
  anomalies: { addressee: 0, postFinal: 0, missingSubmitted: 0 },
  conversations: new Map(),
  topics: new Map(),
  now: 0,
  liveness: new Map(),
  streamStats: new Map(),
  streamAttach: new Map(),
  pending: [],
  localSeq: 0,
};

/**
 * Correlation ids get a stable hue so one conversational thread reads as one
 * colour everywhere — chat chips, task rows, transcripts. FNV-1a keeps
 * neighbouring uuids far apart in hue space.
 */
export function corrColor(corrId: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < corrId.length; i++) {
    h ^= corrId.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return `hsl(${(h >>> 0) % 360} 70% 60%)`;
}

function isTerminal(state: TaskState): boolean {
  return TERMINAL_STATES.includes(state);
}

function tsMs(env: Envelope): number {
  return Date.parse(env.ts) || 0;
}

function withAgent(
  agents: Map<string, AgentView>,
  session: string,
  patch: Partial<AgentView>,
): Map<string, AgentView> {
  const prev = agents.get(session);
  if (!prev) return agents;
  const next = new Map(agents);
  next.set(session, { ...prev, ...patch });
  return next;
}

/**
 * Every session heard from becomes an agent entry; traffic alone earns one.
 * Liveness uses the browser's receive clock for live traffic — a publisher
 * whose clock runs behind must not read as idle while it is streaming — and
 * the envelope's own ts for replayed history, which really is old.
 */
function touchAgent(state: UiState, env: Envelope, live: boolean, at: number): Map<string, AgentView> {
  const session = env.from.session;
  if (session === "") return state.agents;
  const prev = state.agents.get(session);
  const agents = new Map(state.agents);
  agents.set(session, {
    session,
    agentType: env.from.agentType ?? prev?.agentType ?? "unknown",
    profile: env.from.profile ?? prev?.profile,
    statusLine: prev?.statusLine,
    perTask: prev?.perTask,
    status: prev?.status === "closed" ? "closed" : "active",
    lastActivity: Math.max(prev?.lastActivity ?? 0, live ? at : tsMs(env)),
  });
  return agents;
}

/** Ensures the task exists, then folds in whatever this envelope knows. */
function upsertTask(
  tasks: Map<string, TaskView>,
  env: Envelope,
  subject: SubjectInfo,
  patch: Partial<TaskView> = {},
): Map<string, TaskView> {
  if (!env.taskId) return tasks;
  const prev = tasks.get(env.taskId);
  const addressee = subject.plane === "tasks" ? subject.addressee : "";
  const base: TaskView = prev ?? {
    taskId: env.taskId,
    contextId: env.contextId ?? "",
    correlationId: env.correlationId,
    addressee,
    owner: env.from.session,
    state: "submitted",
    final: false,
    artifacts: new Map(),
    lastEventAt: 0,
  };
  const next = new Map(tasks);
  next.set(env.taskId, {
    ...base,
    ...patch,
    lastEventAt: Math.max(base.lastEventAt, tsMs(env)),
  });
  return next;
}

/**
 * Chat ids key React's list, so they must be unique for the life of the
 * transcript. They are derived from the envelope that produced the entry,
 * not from a running counter: branches here pass different state objects
 * (some pre-increment, some post), and a counter made two envelopes collide
 * on the same id — React then drops one entry from the render.
 */
function pushChat(state: UiState, env: Envelope, entry: Omit<ChatEntry, "id">): ChatEntry[] {
  return [...state.chat, { id: `${env.envelopeId}#${state.chat.length}`, ...entry }];
}

/**
 * Streaming chunks of one artifact merge into a single transcript entry.
 * Only when the producer said `append`: a full re-publish of `result` is a
 * legal A2A replacement, and concatenating it would show the answer twice.
 */
function appendChunk(
  state: UiState,
  env: Envelope,
  entry: Omit<ChatEntry, "id">,
  append: boolean,
): ChatEntry[] {
  const last = state.chat[state.chat.length - 1];
  const mergeable =
    append &&
    last !== undefined &&
    last.kind === entry.kind &&
    last.session === entry.session &&
    last.taskId !== undefined &&
    last.taskId === entry.taskId;
  if (!mergeable) return pushChat(state, env, entry);
  const merged = [...state.chat];
  merged[merged.length - 1] = { ...last, text: last.text + entry.text };
  return merged;
}

function reduceMessage(
  next: UiState,
  state: UiState,
  env: Envelope,
  subject: SubjectInfo,
  live: boolean,
): void {
  const payload = env.payload as Message;
  const text = partsText(payload.parts);
  const known = state.tasks.get(env.taskId ?? "");
  const authority = authorityOf(env);
  // The first message on a task subject is the submission - the user's ask,
  // echoed from the stream so the transcript never trusts local state. A
  // later message on the same task is steering or follow-up input.
  const isSubmission = known === undefined;
  next.tasks = upsertTask(
    state.tasks,
    env,
    subject,
    isSubmission
      ? { backend: authority.backend, conversation: authority.conversation, askAt: tsMs(env) }
      : undefined,
  );
  const entry: Omit<ChatEntry, "id"> = {
    kind: isSubmission ? "user" : "steer",
    session: env.from.session,
    text,
    correlationId: env.correlationId,
    taskId: env.taskId,
  };
  // A turn this page sent attaches in place: same id, now carrying the
  // task's correlation. Live traffic always matches. Non-live traffic
  // matches too, but only a pending turn sent before the envelope's own ts:
  // a tap re-attach between the send and the submission re-snapshots
  // lastSeqAtConnect after the submission has already landed, so a turn this
  // tab really did just send can replay as non-live. Genuinely old history
  // (from before this page connected) has a ts before any pending turn's
  // send time, so it still can't match. FIFO within a conversation, so two
  // identical texts attach in order. Two things outrank age. A turn sent
  // with exactly this text beats one that only strips to it (a delegate
  // candidate must not take a plain retry's task). And a turn that has gone
  // stale, or that a gateway notice followed, ranks below one that has not:
  // either may be a turn the gateway dropped or answered with a notice, and
  // the same words sent again must attach to the resend, not to it.
  const match =
    authority.conversation !== undefined
      ? pendingMatch(state.pending, authority.conversation, text, live, tsMs(env))
      : -1;
  if (match >= 0) {
    const turn = state.pending[match];
    next.pending = state.pending.filter((_, i) => i !== match);
    const id = `pending:${turn.messageId}`;
    const at = state.chat.findIndex((c) => c.id === id);
    if (at >= 0) {
      const chat = [...state.chat];
      chat[at] = { id, ...entry };
      next.chat = chat;
    } else {
      next.chat = pushChat(state, env, entry);
    }
  } else {
    next.chat = pushChat(state, env, entry);
  }

  if (authority.conversation !== undefined) {
    const prev = state.conversations.get(authority.conversation);
    const conversations = new Map(state.conversations);
    conversations.set(authority.conversation, {
      conversation: authority.conversation,
      backend: authority.backend ?? prev?.backend ?? "unknown",
      lastSeen: Math.max(prev?.lastSeen ?? 0, tsMs(env)),
      turns: (prev?.turns ?? 0) + 1,
    });
    next.conversations = conversations;
  }
}

function pendingMatch(pending: PendingTurn[], conversation: string, text: string, live: boolean, ts: number): number {
  let best = -1;
  let bestRank = Infinity;
  for (let i = 0; i < pending.length; i++) {
    const p = pending[i];
    if (p.conversation !== conversation || !p.texts.includes(text) || !(live || p.at < ts)) continue;
    const rank = (p.stale || p.noticed ? 2 : 0) + (p.text === text ? 0 : 1);
    if (rank < bestRank) {
      best = i;
      bestRank = rank;
    }
  }
  return best;
}

function reduceStatusUpdate(
  next: UiState,
  state: UiState,
  env: Envelope,
  subject: SubjectInfo,
): void {
  const payload = env.payload as StatusUpdate;
  const taskState = payload.status?.state ?? "working";
  const final = payload.final === true;
  const prev = state.tasks.get(env.taskId ?? "");
  const fromExecutor = env.from.session !== GATEWAY_SESSION;
  const ts = tsMs(env);
  const terminal = final || isTerminal(taskState);
  const firstTerminal = terminal && prev?.endedAt === undefined;
  const note = partsText(payload.status?.message?.parts);
  const sawSubmitted = prev?.sawSubmitted === true || (fromExecutor && taskState === "submitted");
  next.tasks = upsertTask(state.tasks, env, subject, {
    state: taskState,
    final,
    executor: prev?.executor ?? env.from.session,
    firstStatusAt: prev?.firstStatusAt ?? (fromExecutor ? ts : undefined),
    sawSubmitted,
    endedAt: prev?.endedAt ?? (terminal ? ts : undefined),
    reason: firstTerminal && note !== "" ? note : prev?.reason,
  });
  // Both executors publish `submitted` before anything else. A task that
  // ended without one skipped a step the spec requires. Counted once, on the
  // event that first makes it terminal, and only for tasks whose submission
  // this page saw (a task first seen mid-flight proves nothing).
  if (firstTerminal && prev?.askAt !== undefined && !sawSubmitted) {
    next.anomalies = { ...next.anomalies, missingSubmitted: next.anomalies.missingSubmitted + 1 };
  }

  // A status that carries a message (input-required's question, a supervisor's
  // reason) belongs in the transcript.
  if (note !== "") {
    next.chat = pushChat(state, env, {
      kind: "status",
      session: env.from.session,
      text: note,
      correlationId: env.correlationId,
      taskId: env.taskId,
    });
  }

  const session = env.from.session;
  if (subject.plane === "tasks" && session === subject.addressee) {
    // Answering as the addressee of its own subject: a per-session worker.
    next.agents = withAgent(next.agents, session, { perTask: true });
  }
  if ((final || isTerminal(taskState)) && session !== GATEWAY_SESSION) {
    const agent = next.agents.get(session);
    if (agent && agent.status !== "closed" && agent.perTask) {
      next.agents = withAgent(next.agents, session, { status: "done" });
      // Its `-in` consumer is gone the moment it retires (durablesFor stops
      // polling it), so the last reading is stale the instant it is taken.
      // Dropping it here is belt-and-braces: livenessOf already reads the
      // agent's status and never shows a retired session as live.
      if (next.liveness.has(session)) {
        const liveness = new Map(next.liveness);
        liveness.delete(session);
        next.liveness = liveness;
      }
    }
  }
}

function reduceArtifactUpdate(
  next: UiState,
  state: UiState,
  env: Envelope,
  subject: SubjectInfo,
): void {
  const payload = env.payload as ArtifactUpdate;
  const artifact = payload.artifact ?? {};
  const name = artifact.name ?? artifact.artifactId ?? "unnamed";
  const text = partsText(artifact.parts);

  const prev = env.taskId ? state.tasks.get(env.taskId) : undefined;
  const artifacts = new Map(prev?.artifacts ?? []);
  const existing = artifacts.get(name);
  artifacts.set(name, {
    name,
    text: payload.append && existing ? existing.text + text : text,
    chunks: (existing?.chunks ?? 0) + 1,
  });
  next.tasks = upsertTask(state.tasks, env, subject, {
    artifacts,
    executor: prev?.executor ?? env.from.session,
  });

  if (name === ARTIFACT_RESULT) {
    next.chat = appendChunk(
      state,
      env,
      {
        kind: "answer",
        session: env.from.session,
        text,
        correlationId: env.correlationId,
        taskId: env.taskId,
      },
      payload.append === true,
    );
  } else if (name === ARTIFACT_PROGRESS) {
    next.chat = pushChat(state, env, {
      kind: "progress",
      session: env.from.session,
      text,
      correlationId: env.correlationId,
      taskId: env.taskId,
    });
    next.agents = withAgent(next.agents, env.from.session, { statusLine: text });
  }
  // thinking/activity stay out of the transcript; they still count toward the
  // type's activity LED and count on the task for replay.
}

/**
 * Bus anomalies the spec calls protocol errors. An instrument panel exists to
 * show these, so they go in the transcript rather than being folded in as
 * normal traffic or dropped: a `to` that disagrees with the subject's
 * addressee (assertion 4), and any event after the task's `final` one
 * (assertion 10).
 */
type AnomalyKind = "addressee" | "postFinal";

function anomalyOf(
  state: UiState,
  env: Envelope,
  subject: SubjectInfo,
): { kind: AnomalyKind; text: string } | null {
  if (
    subject.plane === "tasks" &&
    env.to?.session !== undefined &&
    env.to.session !== subject.addressee
  ) {
    return {
      kind: "addressee",
      text: `envelope addressed to "${env.to.session}" on ${subject.addressee}'s subject`,
    };
  }
  const task = env.taskId ? state.tasks.get(env.taskId) : undefined;
  if (task?.final && (env.kind === "status-update" || env.kind === "artifact-update")) {
    return { kind: "postFinal", text: `${env.kind} after the task's final event` };
  }
  return null;
}

function reduceEnvelope(
  state: UiState,
  env: Envelope,
  subject: SubjectInfo,
  live: boolean,
  at: number,
): UiState {
  const next: UiState = { ...state, streamMsgCount: state.streamMsgCount + 1 };
  const anomaly = anomalyOf(state, env, subject);

  if (live) {
    const type = env.from.agentType;
    if (type !== undefined && type !== "") {
      const typePulses = new Map(state.typePulses);
      typePulses.set(type, (typePulses.get(type) ?? 0) + 1);
      next.typePulses = typePulses;
    }
  }

  // An anomalous envelope is reported and not folded: it must not revive a
  // retired agent, retune a finished task, or append to an answer.
  if (anomaly !== null) {
    next.anomalies = { ...state.anomalies, [anomaly.kind]: state.anomalies[anomaly.kind] + 1 };
    next.chat = pushChat(state, env, {
      kind: "anomaly",
      session: env.from.session,
      text: `${anomaly.text} (${env.kind}, ${env.envelopeId})`,
      correlationId: env.correlationId,
      taskId: env.taskId,
    });
    return next;
  }

  next.agents = touchAgent(state, env, live, at);
  const touched: UiState = { ...next };

  switch (env.kind) {
    case "message":
      reduceMessage(next, touched, env, subject, live);
      break;

    case "status-update":
      reduceStatusUpdate(next, touched, env, subject);
      break;

    case "artifact-update":
      reduceArtifactUpdate(next, touched, env, subject);
      break;

    case "cancel": {
      next.tasks = upsertTask(touched.tasks, env, subject);
      next.chat = pushChat(touched, env, {
        kind: "cancel",
        session: env.from.session,
        text: "cancel requested",
        correlationId: env.correlationId,
        taskId: env.taskId,
      });
      break;
    }

    case "agent-card": {
      // Published by the profile's owner for the profile, not by workers: the
      // card names a profile others can address, so the tap is the profile.
      if (subject.plane !== "agents") break;
      const key = subject.profile;
      const prev = touched.agents.get(key);
      const agents = new Map(touched.agents);
      agents.set(key, {
        session: key,
        agentType: prev?.agentType ?? "profile",
        profile: key,
        status: prev?.status === "closed" ? "active" : (prev?.status ?? "idle"),
        lastActivity: Math.max(prev?.lastActivity ?? 0, tsMs(env)),
        statusLine: prev?.statusLine,
        perTask: prev?.perTask,
      });
      next.agents = agents;
      break;
    }

    case "agent-closed": {
      if (subject.plane !== "agents") break;
      const key = subject.profile;
      const prev = touched.agents.get(key);
      if (!prev) break;
      next.agents = withAgent(touched.agents, key, { status: "closed" });
      break;
    }

    case "topic-update": {
      const artifact = env.payload as Artifact;
      const topic = subject.plane === "topics" ? subject.topic : (artifact.name ?? "topic");
      const summary = partsText(artifact.parts);
      next.chat = pushChat(touched, env, {
        kind: "topic",
        session: env.from.session,
        text: summary !== "" ? `${topic}: ${summary}` : `updated ${topic}`,
        correlationId: env.correlationId,
        taskId: env.taskId,
      });
      // No DIRECT.GET on these grants, so "latest" is the newest envelope ts
      // seen per subject. A replay arriving after a live update never wins.
      const key = topicKey(subject, topic);
      const prevTopic = touched.topics.get(key);
      if (prevTopic === undefined || tsMs(env) >= prevTopic.at) {
        const topics = new Map(touched.topics);
        topics.set(key, {
          key,
          topic,
          owner: subject.plane === "topics" ? subject.owner : undefined,
          summary,
          at: tsMs(env),
          publisher: env.from.session,
        });
        next.topics = topics;
      }
      break;
    }
  }

  return next;
}

function withEntry(chat: ChatEntry[], id: string, patch: Partial<ChatEntry>): ChatEntry[] {
  const at = chat.findIndex((c) => c.id === id);
  if (at < 0) return chat;
  const next = [...chat];
  next[at] = { ...chat[at], ...patch };
  return next;
}

/**
 * Marks pending turns that have waited too long, and drops the ones past
 * PENDING_EXPIRE_MS from the attach set. Returns null if none changed.
 */
function staleTurns(state: UiState, now: number): Pick<UiState, "pending" | "chat"> | null {
  let chat = state.chat;
  let changed = false;
  const marked = state.pending.map((p) => {
    if (p.stale || now - p.at <= PENDING_STALE_MS) return p;
    changed = true;
    chat = withEntry(chat, `pending:${p.messageId}`, { note: staleNote(state.connection) });
    return { ...p, stale: true };
  });
  const pending = marked.filter((p) => now - p.at <= PENDING_EXPIRE_MS);
  if (pending.length !== marked.length) changed = true;
  return changed ? { pending, chat } : null;
}

export function reduce(state: UiState, event: BusEvent): UiState {
  switch (event.type) {
    case "envelope":
      return reduceEnvelope(state, event.env, event.subject, event.live, event.at);

    case "tick": {
      let agents: Map<string, AgentView> | undefined;
      for (const [session, agent] of state.agents) {
        if (agent.status !== "active" && agent.status !== "idle") continue;
        const since = agent.lastActivity;
        const want: AgentStatus =
          since !== undefined && event.now - since > IDLE_MS ? "idle" : agent.status;
        if (want === agent.status) continue;
        agents ??= new Map(state.agents);
        agents.set(session, { ...agent, status: want });
      }
      return {
        ...state,
        now: event.now,
        ...(agents ? { agents } : {}),
        ...(staleTurns(state, event.now) ?? {}),
      };
    }

    case "connection":
      return state.connection === event.state ? state : { ...state, connection: event.state };

    case "streams":
      return state.streamsUp === event.up && state.streamsTotal === event.total
        ? state
        : { ...state, streamsUp: event.up, streamsTotal: event.total };

    case "probe":
      return { ...state, probe: event.result };

    case "liveness": {
      const liveness = new Map(state.liveness);
      liveness.set(event.report.session, event.report);
      return { ...state, liveness };
    }

    case "streamStat": {
      const streamStats = new Map(state.streamStats);
      streamStats.set(event.name, { stat: event.stat, error: event.error, at: event.at });
      return { ...state, streamStats };
    }

    case "streamAttach": {
      const prev = state.streamAttach.get(event.stream);
      // While failing, keep when the failure started, so the panel can say
      // "not attached since 12:04" rather than restarting the clock every retry.
      const since =
        event.error !== null && prev !== undefined && prev.error !== null ? prev.since : event.at;
      const streamAttach = new Map(state.streamAttach);
      streamAttach.set(event.stream, { error: event.error, since });
      return { ...state, streamAttach };
    }

    case "consoleSent": {
      const fate = turnFate(event.text);
      if (fate.kind === "settled") {
        // No branch of the gateway makes a task of this turn, so there is
        // nothing to wait for: its answer, if any, is a notice.
        const id = `sent:${event.messageId}`;
        return { ...state, chat: [...state.chat, { id, kind: "sent", text: event.text, correlationId: id }] };
      }
      const id = `pending:${event.messageId}`;
      return {
        ...state,
        pending: [
          ...state.pending,
          {
            messageId: event.messageId,
            text: event.text,
            texts: fate.texts,
            conversation: event.conversation,
            at: event.at,
            stale: false,
            noticed: false,
          },
        ],
        chat: [...state.chat, { id, kind: "pending", text: event.text, correlationId: id }],
      };
    }

    case "sendFailed": {
      const failed = { kind: "local" as const, note: `not sent: ${event.error}` };
      return {
        ...state,
        pending: state.pending.filter((p) => p.messageId !== event.messageId),
        chat: withEntry(withEntry(state.chat, `pending:${event.messageId}`, failed), `sent:${event.messageId}`, failed),
      };
    }

    case "notice": {
      const id = `notice:${event.frame.messageId}`;
      if (state.chat.some((c) => c.id === id)) {
        return { ...state, chat: withEntry(state.chat, id, { text: event.frame.text }) };
      }
      return {
        ...state,
        pending: state.pending.map((p) =>
          p.conversation === event.conversation && p.at <= event.at && !p.noticed ? { ...p, noticed: true } : p,
        ),
        chat: [
          ...state.chat,
          {
            id,
            kind: "notice",
            session: GATEWAY_SESSION,
            text: event.frame.text,
            correlationId: event.conversation,
          },
        ],
      };
    }

    case "local":
      return {
        ...state,
        localSeq: state.localSeq + 1,
        chat: [
          ...state.chat,
          {
            id: `local:${state.localSeq}`,
            kind: "local",
            text: event.text,
            correlationId: LOCAL_CORRELATION,
          },
        ],
      };

    case "clear":
      return { ...state, chat: [], pending: [] };
  }
}

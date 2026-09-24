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
  type Kind,
  type Message,
  type StatusUpdate,
  type SubjectInfo,
  type TaskState,
} from "./protocol.ts";

/** The rail tap for this browser. Not an agent; it reports the websocket. */
export const WEB_SESSION = "you";
/** The chatops gateway's session name (a2a/gateway/gateway.go). */
export const GATEWAY_SESSION = "gateway";
/** No traffic for longer than this and a standing agent reads as idle. */
export const IDLE_MS = 60_000;
/** The rail only ever animates a recent window of traffic. */
export const MAX_PULSES = 200;

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
  | "anomaly";

export interface ChatEntry {
  id: string;
  kind: ChatKind;
  session?: string;
  text: string;
  correlationId: string;
  taskId?: string;
}

export interface Pulse {
  /** Monotonically increasing; the rail's animation loop uses it as a watermark. */
  id: number;
  fromSession: string;
  correlationId: string;
  kind: Kind;
  at: number;
}

export type ProbeOutcome = "refused" | "sent" | "error";

export interface ProbeResult {
  outcome: ProbeOutcome;
  detail: string;
  at: number;
}

export interface UiState {
  agents: Map<string, AgentView>;
  tasks: Map<string, TaskView>;
  chat: ChatEntry[];
  pulses: Pulse[];
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
}

export type BusEvent =
  /** `at` is the browser-clock receive time; `env.ts` is the publisher's. */
  | { type: "envelope"; env: Envelope; subject: SubjectInfo; live: boolean; at: number }
  | { type: "tick"; now: number }
  | { type: "connection"; state: ConnectionState }
  | { type: "streams"; up: number; total: number }
  | { type: "probe"; result: ProbeResult };

export const initialState: UiState = {
  agents: new Map(),
  tasks: new Map(),
  chat: [],
  pulses: [],
  streamMsgCount: 0,
  connection: "connecting",
  streamsUp: 0,
  streamsTotal: 0,
  typePulses: new Map(),
  anomalies: { addressee: 0, postFinal: 0, missingSubmitted: 0 },
  conversations: new Map(),
  topics: new Map(),
  now: 0,
};

/**
 * Correlation ids get a stable hue so one conversational thread reads as one
 * colour everywhere — chat chips, rail pulses, replay strips. FNV-1a keeps
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
 * Every session heard from is a tap on the rail; traffic alone earns one.
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

function reduceMessage(next: UiState, state: UiState, env: Envelope, subject: SubjectInfo): void {
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
  next.chat = pushChat(state, env, {
    kind: isSubmission ? "user" : "steer",
    session: env.from.session,
    text,
    correlationId: env.correlationId,
    taskId: env.taskId,
  });

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
  // thinking/activity stay out of the transcript; they still pulse the rail
  // and count on the task for replay.
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
    const pulse: Pulse = {
      id: next.streamMsgCount,
      fromSession: env.from.session,
      correlationId: env.correlationId,
      kind: env.kind,
      at: tsMs(env),
    };
    const pulses = [...state.pulses, pulse];
    next.pulses = pulses.length > MAX_PULSES ? pulses.slice(pulses.length - MAX_PULSES) : pulses;

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
      reduceMessage(next, touched, env, subject);
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
      return { ...state, now: event.now, ...(agents ? { agents } : {}) };
    }

    case "connection":
      return state.connection === event.state ? state : { ...state, connection: event.state };

    case "streams":
      return state.streamsUp === event.up && state.streamsTotal === event.total
        ? state
        : { ...state, streamsUp: event.up, streamsTotal: event.total };

    case "probe":
      return { ...state, probe: event.result };
  }
}

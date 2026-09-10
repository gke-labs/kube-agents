/**
 * The a2a-jetstream/0.4 wire shapes, as a read-only browser consumes them.
 * `docs/designs/spec-a2a-payloads.md` is the law; the Go library at `a2a/lib`
 * is the implementation of record. This module carries only what a renderer
 * needs: parse an envelope off the stream, know its kind, and know which
 * subject plane it arrived on. It never builds an envelope for publishing —
 * the `web` user cannot publish, and that is the point of it.
 *
 * Consumer rules implemented per spec: reject unknown protocol majors, ignore
 * unknown fields within a major, require the envelope's required fields, and
 * require taskId/contextId for the kinds that need them.
 */

export const PROTOCOL_MAJOR = "a2a-jetstream/0";

export type Kind =
  | "message"
  | "status-update"
  | "artifact-update"
  | "cancel"
  | "agent-card"
  | "agent-closed"
  | "topic-update";

export const KINDS: readonly Kind[] = [
  "message",
  "status-update",
  "artifact-update",
  "cancel",
  "agent-card",
  "agent-closed",
  "topic-update",
];

/** Kinds that must carry taskId and contextId. */
const TASK_KINDS: readonly Kind[] = ["message", "status-update", "artifact-update", "cancel"];

export type TaskState =
  | "submitted"
  | "working"
  | "input-required"
  | "completed"
  | "failed"
  | "canceled"
  | "rejected"
  | "auth-required";

export const TERMINAL_STATES: readonly TaskState[] = [
  "completed",
  "failed",
  "canceled",
  "rejected",
];

/** The four reserved artifact names. The set of names is open; only these carry semantics. */
export const ARTIFACT_RESULT = "result";
export const ARTIFACT_THINKING = "thinking";
export const ARTIFACT_ACTIVITY = "activity";
export const ARTIFACT_PROGRESS = "progress";

export interface Party {
  session: string;
  agentType?: string;
  profile?: string;
}

export interface Part {
  kind?: string;
  text?: string;
  data?: unknown;
  file?: { name?: string; mimeType?: string; uri?: string };
}

export interface Message {
  role?: string;
  parts?: Part[];
  messageId?: string;
  taskId?: string;
  contextId?: string;
}

export interface TaskStatus {
  state?: TaskState;
  message?: Message;
  timestamp?: string;
}

export interface StatusUpdate {
  taskId?: string;
  contextId?: string;
  status?: TaskStatus;
  final?: boolean;
}

export interface Artifact {
  artifactId?: string;
  name?: string;
  parts?: Part[];
}

export interface ArtifactUpdate {
  taskId?: string;
  contextId?: string;
  artifact?: Artifact;
  append?: boolean;
  lastChunk?: boolean;
}

export interface Envelope {
  protocol: string;
  envelopeId: string;
  correlationId: string;
  traceparent?: string;
  taskId?: string;
  contextId?: string;
  ts: string;
  from: Party;
  to?: Party;
  identity?: unknown;
  authority?: unknown;
  kind: Kind;
  payload: unknown;
}

export class ProtocolError extends Error {}

function isParty(v: unknown): v is Party {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Party).session === "string" &&
    (v as Party).session !== ""
  );
}

/**
 * Parses and validates one envelope off the wire. Throws ProtocolError for
 * things the spec says a consumer must reject; unknown fields pass through
 * untouched on the returned object (assertion 1's second half).
 */
export function parseEnvelope(data: Uint8Array | string): Envelope {
  let raw: unknown;
  try {
    raw = JSON.parse(typeof data === "string" ? data : new TextDecoder().decode(data));
  } catch {
    throw new ProtocolError("not JSON");
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new ProtocolError("envelope is not an object");
  }
  const env = raw as Record<string, unknown>;

  if (typeof env.protocol !== "string" || !env.protocol.startsWith(PROTOCOL_MAJOR + ".")) {
    throw new ProtocolError(`unknown protocol ${String(env.protocol)}`);
  }
  for (const field of ["envelopeId", "correlationId", "ts"] as const) {
    if (typeof env[field] !== "string" || env[field] === "") {
      throw new ProtocolError(`missing ${field}`);
    }
  }
  if (!isParty(env.from)) throw new ProtocolError("missing from.session");
  if (env.to !== undefined && env.to !== null && !isParty(env.to)) {
    throw new ProtocolError("malformed to");
  }
  if (typeof env.kind !== "string" || !KINDS.includes(env.kind as Kind)) {
    throw new ProtocolError(`unknown kind ${String(env.kind)}`);
  }
  if (TASK_KINDS.includes(env.kind as Kind)) {
    if (typeof env.taskId !== "string" || env.taskId === "") {
      throw new ProtocolError(`kind ${env.kind} requires taskId`);
    }
    if (typeof env.contextId !== "string" || env.contextId === "") {
      throw new ProtocolError(`kind ${env.kind} requires contextId`);
    }
  }

  // A kind/payload mismatch is a protocol error, never passed through
  // (assertion 7). Every kind's payload is a JSON object — cancel and
  // agent-closed carry the empty one — and the shape fields the renderer
  // dereferences must be the right JSON type when present.
  const payload = env.payload;
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new ProtocolError(`kind ${env.kind} payload is not an object`);
  }
  const p = payload as Record<string, unknown>;
  if (env.kind === "message" && p.parts !== undefined && !Array.isArray(p.parts)) {
    throw new ProtocolError("message payload parts is not an array");
  }
  if (
    env.kind === "status-update" &&
    p.status !== undefined &&
    (typeof p.status !== "object" || p.status === null || Array.isArray(p.status))
  ) {
    throw new ProtocolError("status-update payload status is not an object");
  }
  if (
    env.kind === "artifact-update" &&
    p.artifact !== undefined &&
    (typeof p.artifact !== "object" || p.artifact === null || Array.isArray(p.artifact))
  ) {
    throw new ProtocolError("artifact-update payload artifact is not an object");
  }
  return raw as Envelope;
}

/** Where on the bus an envelope arrived. Task subjects carry the authorization seam. */
export type SubjectInfo =
  | { plane: "tasks"; addressee: string; taskId: string; dir: "in" | "events" }
  | { plane: "agents"; profile: string }
  | { plane: "topics"; scope: "agent" | "shared"; owner?: string; topic: string }
  | { plane: "other" };

export function parseSubject(subject: string): SubjectInfo {
  const t = subject.split(".");
  if (t[0] !== "a2a") return { plane: "other" };
  if (t[1] === "tasks" && t.length === 5 && (t[4] === "in" || t[4] === "events")) {
    return { plane: "tasks", addressee: t[2], taskId: t[3], dir: t[4] };
  }
  if (t[1] === "agents" && t.length === 3) {
    return { plane: "agents", profile: t[2] };
  }
  if (t[1] === "topics" && t[2] === "agent" && t.length === 5) {
    return { plane: "topics", scope: "agent", owner: t[3], topic: t[4] };
  }
  if (t[1] === "topics" && t[2] === "shared" && t.length === 4) {
    return { plane: "topics", scope: "shared", topic: t[3] };
  }
  return { plane: "other" };
}

/** Concatenates the text parts of a Message or Artifact. Never throws: the
 * elements are bus-controlled and a null part must not take the page down. */
export function partsText(parts: Part[] | undefined): string {
  if (!Array.isArray(parts)) return "";
  return parts.map((p) => (p && typeof p.text === "string" ? p.text : "")).join("");
}

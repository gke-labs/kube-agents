/**
 * The console door's wire contract, as the page speaks it. The gateway side
 * is `a2a/gateway/console.go`, and the subjects, the token rule and the text
 * cap here have to match it exactly. Core NATS, no stream: a frame lost
 * across a reconnect costs a notice, never an answer, because answers travel
 * on TASKS.
 */

export const CONSOLE_PREFIX = "console:";
/**
 * Mirrors the gateway's `lib.ValidSubjectToken` -> `validDNS1123Label`
 * (a2a/lib/envelope.go): 1-63 characters, lowercase alphanumeric or hyphen,
 * and a hyphen may not lead or trail. A token this rejects but the gateway
 * accepted (or vice versa) is a frame the gateway drops silently.
 */
export const TOKEN_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
/** Bytes, not characters: the gateway measures len() of the UTF-8 string. */
export const CONSOLE_TEXT_CAP = 16_384;
/** 8 random bytes is 16 hex characters, well inside the 63-character label. */
const TOKEN_BYTES = 8;
const MESSAGE_ID_BYTES = 6;
const MESSAGE_ID_PREFIX = "m-";
/** Go's unicode.IsSpace set, which strings.TrimSpace strips. Not JS \s. */
const GO_SPACE = "[\\t\\n\\v\\f\\r \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const GO_TRIM_RE = new RegExp(`^${GO_SPACE}+|${GO_SPACE}+$`, "g");
const GO_SPACE_RE = new RegExp(GO_SPACE);
/** a2a/gateway/text.go stopWords. */
const STOP_WORDS = new Set(["stop", "cancel", "abort"]);
/** a2a/gateway/text.go isDelegate's word, separators and left-trim cutset. */
const DELEGATE_WORD = "delegate";
const DELEGATE_SEPARATORS = new Set([" ", "\t", "\n", ":", ",", "-", "\u2014"]);
const DELEGATE_TRIM_RE = /^[:,\-\u2014 \t\n]+/;
/** a2a/gateway/text.go slashSessionWord and slashOffWord. */
const SLASH = "/";
const SESSION_WORD = "session";
const SESSION_OFF_WORD = "off";
/** The one non-ASCII rune whose simple case fold (Go strings.EqualFold) is an ASCII letter of "session". */
const LONG_S = /\u017f/g;

export interface ConsoleInFrame {
  messageId: string;
  text: string;
}

export interface ConsoleOutFrame {
  messageId: string;
  text: string;
  /** True when this frame replaces the text of an earlier one with the same id. */
  edit: boolean;
}

export type Random = (buf: Uint8Array) => Uint8Array;

// getRandomValues, not randomUUID: randomUUID only exists in a secure
// context, and the page is plain http behind a port-forward.
const defaultRandom: Random = (buf) => crypto.getRandomValues(buf);

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** The token of a console conversation id, or null if the gateway would reject it. */
export function tokenOf(conversation: string): string | null {
  if (!conversation.startsWith(CONSOLE_PREFIX)) return null;
  const token = conversation.slice(CONSOLE_PREFIX.length);
  return TOKEN_RE.test(token) ? token : null;
}

function mustToken(conversation: string): string {
  const token = tokenOf(conversation);
  if (token === null) {
    throw new Error(`not a console conversation id: ${JSON.stringify(conversation)}`);
  }
  return token;
}

export function inSubject(conversation: string): string {
  return `chat.console.${mustToken(conversation)}.in`;
}

export function outSubject(conversation: string): string {
  return `chat.console.${mustToken(conversation)}.out`;
}

export function mintConversation(random: Random = defaultRandom): string {
  return CONSOLE_PREFIX + hex(random(new Uint8Array(TOKEN_BYTES)));
}

export function mintMessageId(random: Random = defaultRandom): string {
  return MESSAGE_ID_PREFIX + hex(random(new Uint8Array(MESSAGE_ID_BYTES)));
}

export function textBytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/**
 * `kind` is left off rather than sent as "text". The gateway treats the two
 * the same, and a field that isn't there can't be misspelled into a silent
 * drop.
 */
export function encodeInFrame(frame: ConsoleInFrame): Uint8Array {
  return new TextEncoder().encode(JSON.stringify({ messageId: frame.messageId, text: frame.text }));
}

export function parseOutFrame(data: Uint8Array | string): ConsoleOutFrame | null {
  let raw: unknown;
  try {
    raw = JSON.parse(typeof data === "string" ? data : new TextDecoder().decode(data));
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.messageId !== "string" || r.messageId === "") return null;
  if (typeof r.text !== "string") return null;
  return { messageId: r.messageId, text: r.text, edit: r.edit === true };
}

/** Mirrors Go's strings.TrimSpace (the unicode.IsSpace set). */
export function goTrim(s: string): string {
  return s.replace(GO_TRIM_RE, "");
}

// The gateway decides what a console turn becomes from its text alone
// (a2a/gateway/gateway.go, the slash/stop/status/delegate switch). The
// mirrors below copy the text rules the page can apply without knowing the
// gateway's state, so it knows which turns can never come back as a task and
// which come back under a different text.

/** Mirrors a2a/gateway/text.go normalize. */
export function normalizeTurn(s: string): string {
  let out = "";
  let lastSpace = true;
  for (const r of goTrim(s).toLowerCase()) {
    if ((r >= "a" && r <= "z") || (r >= "0" && r <= "9")) {
      out += r;
      lastSpace = false;
    } else if (r === "'") {
      lastSpace = false;
    } else if (r === " " || r === "\t" || r === "\n") {
      if (!lastSpace) out += " ";
      lastSpace = true;
    }
  }
  return goTrim(out);
}

/** Mirrors a2a/gateway/text.go isStop. */
export function isStopTurn(text: string): boolean {
  return STOP_WORDS.has(normalizeTurn(text));
}

/** Mirrors a2a/gateway/text.go isDelegate: the task text, or null if the turn is not a delegation. */
export function delegateRest(text: string): string | null {
  const trimmed = goTrim(text);
  if (trimmed.length < DELEGATE_WORD.length || trimmed.slice(0, DELEGATE_WORD.length).toLowerCase() !== DELEGATE_WORD) {
    return null;
  }
  const rest = trimmed.slice(DELEGATE_WORD.length);
  if (rest === "" || !DELEGATE_SEPARATORS.has(rest[0])) return null;
  const task = goTrim(rest.replace(DELEGATE_TRIM_RE, ""));
  return task === "" ? null : task;
}

/** Mirrors a2a/gateway/text.go isSessionCommand: the argument after `/session`, or null if the turn is not one. */
export function sessionCommandRest(text: string): string | null {
  const trimmed = goTrim(text);
  if (!trimmed.startsWith(SLASH)) return null;
  const body = trimmed.slice(SLASH.length);
  const end = body.search(GO_SPACE_RE);
  const word = end >= 0 ? body.slice(0, end) : body;
  const rest = end >= 0 ? goTrim(body.slice(end)) : "";
  return word.replace(LONG_S, "s").toLowerCase() === SESSION_WORD ? rest : null;
}

/** Mirrors a2a/gateway/text.go isSessionOff. */
export function isSessionOffArg(rest: string): boolean {
  return normalizeTurn(rest) === SESSION_OFF_WORD;
}

/**
 * What the gateway can make of a console turn, judged from its text alone.
 * `settled`: no branch of the gateway's switch ever publishes it as a task
 * message (a stop word, a bare `/session`, `/session off`, `/session stop`).
 * `task`: it may become a task, and the submission's text is one of `texts`.
 * The gateway may still answer a `task` turn with only a notice (a status
 * question, a session-cap refusal, a full queue): that depends on state the
 * page cannot see.
 */
export type TurnFate = { kind: "settled" } | { kind: "task"; texts: string[] };

/** Follows a2a/gateway/gateway.go's switch: slash command first, then stop, then delegate. */
export function turnFate(text: string): TurnFate {
  const trimmed = goTrim(text);
  const sessionRest = sessionCommandRest(trimmed);
  if (sessionRest !== null) {
    // Pre-flip the argument is the first turn as written; post-flip it is
    // unwrapped into an ordinary turn, which the delegate rule then reads.
    if (sessionRest === "" || isSessionOffArg(sessionRest) || isStopTurn(sessionRest)) return { kind: "settled" };
    return { kind: "task", texts: withDelegate(sessionRest) };
  }
  if (isStopTurn(trimmed)) return { kind: "settled" };
  return { kind: "task", texts: withDelegate(trimmed) };
}

/** A delegate turn is published as its rest where sessions are on, and verbatim where they are off. */
function withDelegate(text: string): string[] {
  const rest = delegateRest(text);
  return rest === null ? [text] : [text, rest];
}

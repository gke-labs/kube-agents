/**
 * The console door's wire contract, as the page speaks it. The gateway side
 * is `a2a/gateway/console.go`, and the subjects, the token rule and the text
 * cap here have to match it exactly. Core NATS, no stream: a frame lost
 * across a reconnect costs a notice, never an answer, because answers travel
 * on TASKS.
 */

export const CONSOLE_PREFIX = "console:";
/** One dot-free DNS label, so `chat.console.*.in` covers every token. */
export const TOKEN_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
/** Bytes, not characters: the gateway measures len() of the UTF-8 string. */
export const CONSOLE_TEXT_CAP = 16_384;
/** 8 random bytes is 16 hex characters, well inside the 63-character label. */
const TOKEN_BYTES = 8;
const MESSAGE_ID_BYTES = 6;
const MESSAGE_ID_PREFIX = "m-";

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

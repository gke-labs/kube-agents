/**
 * Where the bus is and how to authenticate to it. Served by the console
 * server (a2a/console), the page asks it for the `console` credential at
 * load and connects to its `/bus` proxy on the same origin, so there's no
 * password to paste. For local dev against a plain nats-server, the URL
 * takes `?ws=&user=&pass=`, and that config rides session storage for the
 * tab. The page connects as `console` by default: the `web` read grants
 * plus publish on `chat.console.*.in`, which is how the chat pane submits
 * turns. `?user=web&pass=...` gets the read-only page; naming `web` with no
 * password shows how to add one rather than asking the server for the
 * `console` credential in its place.
 */
import { tokenOf } from "./console.ts";

export interface BusConfig {
  url: string;
  user: string;
  pass: string;
}

const STORAGE_KEY = "a2a-web-config";
const CONVERSATION_KEY = "a2a-web-conversation";
/** What the console server answers on. Must match a2a/console. */
export const SERVED_CONFIG_PATH = "/config.json";
export const SERVED_BUS_PATH = "/bus";
const JSON_TYPE = "application/json";
const HTTP_NOT_FOUND = 404;

export const DEFAULT_WS_URL = "ws://localhost:9222";
export const DEFAULT_USER = "console";
/** The user whose grants stop at the read API. No input box for it. */
export const READ_ONLY_USER = "web";

/**
 * Query params override storage field-by-field; storage remembers the last
 * connect. A bare `?ws=` therefore retargets a stored session without
 * re-pasting the password; no password from either source returns null. A
 * null with no `?user=` named asks the console server next; a null with a
 * user named shows how to connect as that user by hand instead, since the
 * server would answer with a different user's credential (App.tsx's
 * `initialStage`).
 *
 * Pure on purpose - it runs as a `useState` lazy initializer, which React
 * double-invokes under StrictMode. Scrubbing the URL here made the second
 * call see no password and return null, which is exactly the impurity
 * StrictMode exists to expose. The scrub is `scrubPasswordFromUrl`, called
 * from an effect.
 */
export function loadConfig(): BusConfig | null {
  const params = new URLSearchParams(window.location.search);
  let stored: BusConfig | null = null;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw) stored = JSON.parse(raw) as BusConfig;
  } catch {
    // fall through as if nothing were stored
  }
  const pass = params.get("pass") ?? stored?.pass;
  if (pass == null) return null;
  return {
    url: params.get("ws") ?? stored?.url ?? DEFAULT_WS_URL,
    user: params.get("user") ?? stored?.user ?? DEFAULT_USER,
    pass,
  };
}

/**
 * Takes the password out of the address bar, the history entry, and any
 * bookmark or screenshot of the URL. It still rides sessionStorage, which is
 * the stated playground posture; the URL is a notch worse and costs a line.
 */
export function scrubPasswordFromUrl(): void {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("pass")) return;
  params.delete("pass");
  const query = params.toString();
  window.history.replaceState(
    null,
    "",
    window.location.pathname + (query ? `?${query}` : "") + window.location.hash,
  );
}

export function saveConfig(config: BusConfig): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(config));
  } catch {
    // storage denied - the page won't remember this connect next load
  }
}

/**
 * The tab's console conversation. Session storage, so a reload keeps the
 * same gateway session and a new tab starts its own. A stored value the
 * gateway would reject is treated as absent.
 */
export function loadConversation(): string | null {
  try {
    const stored = sessionStorage.getItem(CONVERSATION_KEY);
    return stored !== null && tokenOf(stored) !== null ? stored : null;
  } catch {
    return null;
  }
}

export function saveConversation(conversation: string): void {
  try {
    sessionStorage.setItem(CONVERSATION_KEY, conversation);
  } catch {
    // storage denied - the next load mints a fresh conversation
  }
}

/** The console server's websocket proxy, on the origin the page came from. */
export function sameOriginWsUrl(loc: Pick<Location, "protocol" | "host">): string {
  const scheme = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${loc.host}${SERVED_BUS_PATH}`;
}

/**
 * Asks the console server for the bus credential. Null means no console
 * server is serving this page: a 404, or anything that isn't JSON, which is
 * what Vite answers (its index.html fallback, 200 text/html) for a path it
 * doesn't have. Any other failure throws with the server's own sentence,
 * because that sentence says what to fix.
 */
export async function fetchServedConfig(
  loc: Pick<Location, "protocol" | "host"> = window.location,
  // A wrapper, not a bare `fetch` default: some browsers refuse a detached
  // fetch called without its window.
  fetcher: typeof fetch = (input, init) => fetch(input, init),
): Promise<BusConfig | null> {
  const resp = await fetcher(SERVED_CONFIG_PATH, {
    headers: { Accept: JSON_TYPE },
    cache: "no-store",
  });
  if (resp.status === HTTP_NOT_FOUND) return null;
  if (!resp.ok) {
    throw new Error(`${resp.status}: ${(await resp.text()).trim()}`);
  }
  if (!(resp.headers.get("content-type") ?? "").startsWith(JSON_TYPE)) return null;
  const body: unknown = await resp.json();
  const { user, pass } = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
  if (typeof user !== "string" || user === "" || typeof pass !== "string" || pass === "") {
    throw new Error(`${SERVED_CONFIG_PATH} answered without a user and pass`);
  }
  return { url: sameOriginWsUrl(loc), user, pass };
}

/** Forgets the stored config, so a failed connect isn't retried on reload. */
export function clearConfig(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage denied - nothing was stored either
  }
}

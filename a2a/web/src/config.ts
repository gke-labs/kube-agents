/**
 * Where the bus is and how to authenticate to it. Playground posture: the
 * `web` user is read-only by the server's own grants, the listener is plain
 * ws behind kubectl port-forward, and the password travels as a query param
 * or a pasted field — never baked into the bundle.
 */

export interface BusConfig {
  url: string;
  user: string;
  pass: string;
}

const STORAGE_KEY = "a2a-web-config";

export const DEFAULT_WS_URL = "ws://localhost:9222";
export const DEFAULT_USER = "web";

/**
 * Query params override storage field-by-field; storage remembers the last
 * connect. A bare `?ws=` therefore retargets a stored session without
 * re-pasting the password; no password from either source returns null and
 * the connect form asks.
 *
 * Pure on purpose — it runs as a `useState` lazy initializer, which React
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
    // fall through to the connect form
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
    // storage denied — the form will ask again next load
  }
}

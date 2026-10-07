// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_USER,
  READ_ONLY_USER,
  SERVED_BUS_PATH,
  SERVED_CONFIG_PATH,
  clearConfig,
  fetchServedConfig,
  loadConfig,
  loadConversation,
  saveConfig,
  saveConversation,
  sameOriginWsUrl,
} from "./config.ts";

afterEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("config", () => {
  it("connects as console by default, and web is still reachable by URL", () => {
    expect(DEFAULT_USER).toBe("console");
    expect(READ_ONLY_USER).toBe("web");
    window.history.replaceState(null, "", "/?pass=p");
    expect(loadConfig()?.user).toBe("console");
    window.history.replaceState(null, "", "/?pass=p&user=web");
    expect(loadConfig()?.user).toBe("web");
  });

  it("round-trips the conversation id through session storage", () => {
    expect(loadConversation()).toBeNull();
    saveConversation("console:abc");
    expect(loadConversation()).toBe("console:abc");
  });

  it("ignores a stored conversation id the gateway would reject", () => {
    sessionStorage.setItem("a2a-web-conversation", "console:Not.Valid");
    expect(loadConversation()).toBeNull();
    sessionStorage.setItem("a2a-web-conversation", "console:abc-");
    expect(loadConversation()).toBeNull();
  });
});

function fakeResponse(status: number, contentType: string, body: string): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => JSON.parse(body),
    text: async () => body,
  } as unknown as Response;
}

const served = { protocol: "http:", host: "localhost:8080" };

describe("served config", () => {
  it("builds the websocket URL from the page's own origin", () => {
    expect(SERVED_BUS_PATH).toBe("/bus");
    expect(sameOriginWsUrl(served)).toBe("ws://localhost:8080/bus");
    expect(sameOriginWsUrl({ protocol: "https:", host: "console.example" })).toBe("wss://console.example/bus");
  });

  it("returns the console server's credential, with the bus on the same origin", async () => {
    const fetcher = vi.fn(async () =>
      fakeResponse(200, "application/json", JSON.stringify({ user: "console", pass: "s3cret" })),
    );
    await expect(fetchServedConfig(served, fetcher)).resolves.toEqual({
      url: "ws://localhost:8080/bus",
      user: "console",
      pass: "s3cret",
    });
    expect(fetcher).toHaveBeenCalledWith(SERVED_CONFIG_PATH, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  });

  it("reads Vite's index.html fallback and a 404 as not served", async () => {
    const html = vi.fn(async () => fakeResponse(200, "text/html", "<!doctype html>"));
    await expect(fetchServedConfig(served, html)).resolves.toBeNull();
    const missing = vi.fn(async () => fakeResponse(404, "text/plain", "404 page not found"));
    await expect(fetchServedConfig(served, missing)).resolves.toBeNull();
  });

  it("surfaces the server's sentence when the credential is missing", async () => {
    const body = "no console credential at /var/run/secrets/a2a-console/console-password\n";
    const fetcher = vi.fn(async () => fakeResponse(503, "text/plain; charset=utf-8", body));
    await expect(fetchServedConfig(served, fetcher)).rejects.toThrow(
      "503: no console credential at /var/run/secrets/a2a-console/console-password",
    );
  });

  it("refuses JSON without a user and a password", async () => {
    for (const body of ['{"user":"console"}', '{"pass":"x"}', '{"user":"","pass":"x"}', "[]"]) {
      const fetcher = vi.fn(async () => fakeResponse(200, "application/json", body));
      await expect(fetchServedConfig(served, fetcher), body).rejects.toThrow("user and pass");
    }
  });

  it("clears a stored config", () => {
    saveConfig({ url: "ws://localhost:9222", user: "console", pass: "p" });
    clearConfig();
    expect(loadConfig()).toBeNull();
  });
});

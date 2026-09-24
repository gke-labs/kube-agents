// @vitest-environment jsdom
/**
 * App-level wiring the component tests below it can't see: the send gate
 * that withholds `onSend`/`onCommand` from the `web` user (M8), the refusal
 * to publish while the bus link is down (I1), and how the page finds its
 * bus - a URL or stored config, the console server's `/config.json`, or the
 * port-forward guidance when neither is there. `./bus.ts` is mocked
 * throughout - this file is not a live test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { BusEvent } from "./model.ts";

const { startBus } = vi.hoisted(() => ({ startBus: vi.fn() }));

vi.mock("./bus.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./bus.ts")>();
  return { ...actual, startBus, durablesFor: () => [] };
});

import App from "./App.tsx";

const STORAGE_KEY = "a2a-web-config";

let captured: { dispatch: (e: BusEvent) => void } | null = null;

function handle() {
  return {
    close: vi.fn(async () => {}),
    send: vi.fn(),
    setConversation: vi.fn(),
    probeReadOnly: vi.fn(async () => {}),
  };
}

function respond(status: number, contentType: string, body: string) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => JSON.parse(body),
    text: async () => body,
  }));
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function setLocation(query: string): void {
  window.history.replaceState(null, "", `/${query}`);
}

beforeEach(() => {
  startBus.mockReset();
  startBus.mockImplementation(async (_config: unknown, dispatch: (e: BusEvent) => void) => {
    captured = { dispatch };
    return handle();
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  captured = null;
  sessionStorage.clear();
  setLocation("");
});

describe("App", () => {
  it("gives the console user an input box", async () => {
    setLocation("?pass=secret");
    render(<App />);
    expect(await screen.findByRole("textbox")).toBeTruthy();
  });

  it("withholds the input box and commands from the web user", async () => {
    setLocation("?user=web&pass=secret");
    render(<App />);
    await waitFor(() => expect(startBus).toHaveBeenCalled());
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText(/read-only/)).toBeTruthy();
  });

  it("refuses to send while the link is down, keeps the draft, and never calls bus.send", async () => {
    setLocation("?pass=secret");
    render(<App />);
    await waitFor(() => expect(captured).not.toBeNull());
    const handleResult = await startBus.mock.results[0]!.value;
    act(() => captured!.dispatch({ type: "connection", state: "down" }));

    const box = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    await userEvent.type(box, "hello{Enter}");

    expect(handleResult.send).not.toHaveBeenCalled();
    expect(box.value).toBe("hello");
    expect(screen.getByText(/the bus link is down/)).toBeTruthy();
  });

  it("sends once the link is back up", async () => {
    setLocation("?pass=secret");
    render(<App />);
    await waitFor(() => expect(captured).not.toBeNull());
    const handleResult = await startBus.mock.results[0]!.value;
    act(() => captured!.dispatch({ type: "connection", state: "up" }));

    const box = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    await userEvent.type(box, "hello{Enter}");

    expect(handleResult.send).toHaveBeenCalledWith("hello");
    expect(box.value).toBe("");
  });

  it("carries ?user=web from the URL straight through, with no server round trip", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    setLocation("?user=web&pass=the-web-password");
    render(<App />);
    await waitFor(() => expect(startBus).toHaveBeenCalled());
    const [config] = startBus.mock.calls[0]!;
    expect(config).toEqual({ url: "ws://localhost:9222", user: "web", pass: "the-web-password" });
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("App finds its bus", () => {
  it("connects with the served credential, to /bus on its own origin, and never stores it", async () => {
    const fetch = respond(200, "application/json", '{"user":"console","pass":"served"}');
    vi.stubGlobal("fetch", fetch);
    render(<App />);
    await waitFor(() => expect(startBus).toHaveBeenCalledTimes(1));
    expect(startBus.mock.calls[0]![0]).toEqual({
      url: `ws://${window.location.host}/bus`,
      user: "console",
      pass: "served",
    });
    expect(fetch).toHaveBeenCalledWith("/config.json", expect.objectContaining({ cache: "no-store" }));
    await flush();
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("lets a URL override win without asking the server", async () => {
    const fetch = respond(200, "application/json", '{"user":"console","pass":"served"}');
    vi.stubGlobal("fetch", fetch);
    setLocation("?ws=ws://localhost:9222&user=console&pass=dev-console");
    render(<App />);
    await waitFor(() => expect(startBus).toHaveBeenCalledTimes(1));
    expect(startBus.mock.calls[0]![0]).toEqual({ url: "ws://localhost:9222", user: "console", pass: "dev-console" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("shows the port-forward guidance under Vite, and doesn't connect", async () => {
    vi.stubGlobal("fetch", respond(200, "text/html", "<!doctype html>"));
    render(<App />);
    expect(await screen.findByText(/port-forward svc\/platform-agent-a2a-console 8080:8080/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(startBus).not.toHaveBeenCalled();
  });

  it("shows the server's sentence when the credential is missing", async () => {
    const body = "no console credential at /var/run/secrets/a2a-console/console-password\n";
    vi.stubGlobal("fetch", respond(503, "text/plain; charset=utf-8", body));
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("503: no console credential at /var/run/secrets/a2a-console/console-password");
    expect(startBus).not.toHaveBeenCalled();
  });

  it("forgets a config that failed to connect, and Retry asks the server again", async () => {
    startBus.mockReset();
    startBus.mockImplementationOnce(() => Promise.reject(new Error("websocket refused: 403")));
    startBus.mockImplementation(async (_config: unknown, dispatch: (e: BusEvent) => void) => {
      captured = { dispatch };
      return handle();
    });
    const fetch = respond(200, "application/json", '{"user":"console","pass":"served"}');
    vi.stubGlobal("fetch", fetch);
    setLocation("?ws=ws://localhost:9222&user=console&pass=wrong");
    render(<App />);
    expect((await screen.findByRole("alert")).textContent).toContain("websocket refused: 403");
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(fetch).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(startBus).toHaveBeenCalledTimes(2));
    expect(startBus.mock.calls[1]![0].url).toBe(`ws://${window.location.host}/bus`);
  });
});

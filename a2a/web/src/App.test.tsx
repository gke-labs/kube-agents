// @vitest-environment jsdom
/**
 * App-level wiring the component tests below it can't see: the send gate
 * that withholds `onSend`/`onCommand` from the `web` user (M8), the refusal
 * to publish while the bus link is down (I1), and the connect form carrying
 * `?user=` through to the credential it actually submits (M4). `./bus.ts` is
 * mocked throughout — this file is not a live test.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { BusEvent } from "./model.ts";

const { startBusMock } = vi.hoisted(() => ({ startBusMock: vi.fn() }));

vi.mock("./bus.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./bus.ts")>();
  return { ...actual, startBus: startBusMock, durablesFor: () => [] };
});

import App from "./App.tsx";

let captured: { dispatch: (e: BusEvent) => void } | null = null;

startBusMock.mockImplementation(async (_config: unknown, dispatch: (e: BusEvent) => void) => {
  captured = { dispatch };
  return {
    probeReadOnly: vi.fn(),
    send: vi.fn(),
    setConversation: vi.fn(),
    close: vi.fn().mockResolvedValue(undefined),
  };
});

function setLocation(query: string): void {
  window.history.replaceState(null, "", `/${query}`);
}

afterEach(() => {
  cleanup();
  startBusMock.mockClear();
  captured = null;
  window.sessionStorage.clear();
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
    await waitFor(() => expect(startBusMock).toHaveBeenCalled());
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText(/read-only/)).toBeTruthy();
  });

  it("refuses to send while the link is down, keeps the draft, and never calls bus.send", async () => {
    setLocation("?pass=secret");
    render(<App />);
    await waitFor(() => expect(captured).not.toBeNull());
    const handle = await startBusMock.mock.results[0]!.value;
    act(() => captured!.dispatch({ type: "connection", state: "down" }));

    const box = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    await userEvent.type(box, "hello{Enter}");

    expect(handle.send).not.toHaveBeenCalled();
    expect(box.value).toBe("hello");
    expect(screen.getByText(/the bus link is down/)).toBeTruthy();
  });

  it("sends once the link is back up", async () => {
    setLocation("?pass=secret");
    render(<App />);
    await waitFor(() => expect(captured).not.toBeNull());
    const handle = await startBusMock.mock.results[0]!.value;
    act(() => captured!.dispatch({ type: "connection", state: "up" }));

    const box = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    await userEvent.type(box, "hello{Enter}");

    expect(handle.send).toHaveBeenCalledWith("hello");
    expect(box.value).toBe("");
  });

  it("links a conversation /new changed while the bus was still connecting", async () => {
    setLocation("?pass=secret");
    const handle = {
      probeReadOnly: vi.fn(),
      send: vi.fn(),
      setConversation: vi.fn(),
      close: vi.fn().mockResolvedValue(undefined),
    };
    let finish: () => void = () => {};
    startBusMock.mockImplementationOnce(
      (_config: unknown, dispatch: (e: BusEvent) => void) =>
        new Promise((resolve) => {
          finish = () => {
            captured = { dispatch };
            resolve(handle);
          };
        }),
    );
    render(<App />);
    await waitFor(() => expect(startBusMock).toHaveBeenCalled());
    const [, , opts] = startBusMock.mock.calls[0]!;
    const first = (opts as { conversation: string }).conversation;

    const box = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    await userEvent.type(box, "/new{Enter}");
    expect(handle.setConversation).not.toHaveBeenCalled();
    await act(async () => finish());

    await waitFor(() => expect(handle.setConversation).toHaveBeenCalledTimes(1));
    const linked = handle.setConversation.mock.calls[0]![0] as string;
    expect(linked).not.toBe(first);
    expect(screen.getByText(new RegExp(`new conversation ${linked}`))).toBeTruthy();
  });

  it("carries ?user=web through the connect form to the credential it submits", async () => {
    setLocation("?user=web");
    render(<App />);
    const passInput = screen.getByLabelText(/web password/i);
    await userEvent.type(passInput, "the-web-password");
    await userEvent.click(screen.getByRole("button", { name: /connect/i }));
    await waitFor(() => expect(startBusMock).toHaveBeenCalled());
    const [config] = startBusMock.mock.calls[0]!;
    expect((config as { user: string }).user).toBe("web");
  });
});

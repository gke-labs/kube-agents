// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Chat from "./Chat.tsx";
import type { ChatEntry } from "./model.ts";

afterEach(cleanup);

const entries: ChatEntry[] = [
  { id: "c1", kind: "user", session: "gateway", text: "are we ready?", correlationId: "corr-1" },
  {
    id: "c2",
    kind: "progress",
    session: "platform-bridge",
    text: "reading the topic",
    correlationId: "corr-1",
  },
  {
    id: "c3",
    kind: "answer",
    session: "platform-bridge",
    text: "acme-prod is ready",
    correlationId: "corr-1",
  },
];

function consoleChat(onSend = vi.fn(), onCommand = vi.fn()) {
  render(
    <Chat
      entries={[]}
      user="console"
      conversation="console:abc123"
      onProbe={() => {}}
      onSend={onSend}
      onCommand={onCommand}
    />,
  );
  return { onSend, onCommand, box: screen.getByRole("textbox") as HTMLTextAreaElement };
}

describe("Chat", () => {
  it("renders the transcript read-only for web: no input, no send", () => {
    render(<Chat entries={entries} user="web" onProbe={() => {}} onSend={() => {}} />);
    expect(screen.getByText("are we ready?")).toBeTruthy();
    expect(screen.getByText("acme-prod is ready")).toBeTruthy();
    expect(document.querySelector("textarea")).toBeNull();
    expect(document.querySelector("input")).toBeNull();
  });

  it("fires the probe from the verify button", async () => {
    const onProbe = vi.fn();
    render(<Chat entries={[]} user="web" onProbe={onProbe} />);
    await userEvent.click(screen.getByRole("button", { name: /verify/i }));
    expect(onProbe).toHaveBeenCalledOnce();
  });

  it("shows the refusal detail and flags a probe that got through", () => {
    const { rerender } = render(
      <Chat
        entries={[]}
        user="web"
        probe={{
          outcome: "refused",
          detail: 'Permissions Violation for publish to "a2a.topics.shared.probe"',
          at: 1,
        }}
        onProbe={() => {}}
      />,
    );
    expect(screen.getByText(/Permissions Violation for publish/)).toBeTruthy();

    rerender(
      <Chat
        entries={[]}
        user="web"
        probe={{ outcome: "sent", detail: "no refusal within 2s - the publish went through; the web grant is broken", at: 2 }}
        onProbe={() => {}}
      />,
    );
    expect(screen.getByText(/PUBLISH WENT THROUGH/)).toBeTruthy();
  });

  it("only vouches read-only for the web user", () => {
    const { rerender } = render(<Chat entries={[]} user="web" onProbe={() => {}} />);
    expect(screen.getByText(/read-only/)).toBeTruthy();
    rerender(<Chat entries={[]} user="seed" onProbe={() => {}} />);
    expect(screen.getByText("seed")).toBeTruthy();
    expect(screen.queryByText(/read-only/)).toBeNull();

    cleanup();
    render(<Chat entries={[]} user="console" onProbe={() => {}} />);
    expect(screen.queryByText(/read-only/)).toBeNull();
  });

  it("groups by correlation with one chip per exchange", () => {
    const { container } = render(<Chat entries={entries} user="web" onProbe={() => {}} />);
    expect(container.querySelectorAll(".corr-chip")).toHaveLength(1);
  });

  it("shows the conversation in the footer", () => {
    consoleChat();
    expect(screen.getByText("console:abc123")).toBeTruthy();
  });

  it("sends on Enter, trimmed the gateway's way, and clears the box", async () => {
    const { onSend, box } = consoleChat();
    await userEvent.type(box, "  is acme-prod ready?  {Enter}");
    expect(onSend).toHaveBeenCalledWith("is acme-prod ready?");
    expect(box.value).toBe("");
  });

  it("keeps Shift+Enter as a newline", async () => {
    const { onSend, box } = consoleChat();
    await userEvent.type(box, "line one{Shift>}{Enter}{/Shift}line two");
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe("line one\nline two");
  });

  it("does not send the Enter that closes an IME composition", () => {
    const { onSend, box } = consoleChat();
    fireEvent.change(box, { target: { value: "日本" } });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe("日本");
  });

  it("sends nothing for a blank line", async () => {
    const { onSend, onCommand, box } = consoleChat();
    await userEvent.type(box, "   {Enter}");
    expect(onSend).not.toHaveBeenCalled();
    expect(onCommand).not.toHaveBeenCalled();
  });

  it("routes a slash line to onCommand and never to onSend", async () => {
    const { onSend, onCommand, box } = consoleChat();
    await userEvent.type(box, "/replay gateway{Enter}");
    expect(onCommand).toHaveBeenCalledWith({ name: "replay", session: "gateway" });
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe("");
  });

  it("refuses a turn over the cap locally and keeps the text", () => {
    const { onSend, onCommand, box } = consoleChat();
    const big = "x".repeat(16385);
    fireEvent.change(box, { target: { value: big } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(onCommand).toHaveBeenCalledWith({
      name: "error",
      text: "not sent: 16385 bytes is over the gateway's 16384-byte limit. Trim it and send again.",
    });
    expect(box.value).toBe(big);
  });

  it("renders a failed turn's note under its text", () => {
    render(
      <Chat
        entries={[{ id: "local:1", kind: "local", text: "hello", note: "not sent: disconnected", correlationId: "local" }]}
        user="console"
        onProbe={() => {}}
      />,
    );
    expect(screen.getByText("not sent: disconnected")).toBeTruthy();
  });
});

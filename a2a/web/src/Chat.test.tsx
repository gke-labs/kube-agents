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

/** A card's report relayed by the gateway: the one notice that carries the turn it answers. */
const REPORT: ChatEntry = {
  id: "notice:n-1",
  kind: "notice",
  session: "gateway",
  text: "two pods pending on acme-prod\n- web-1\n- web-2",
  correlationId: "console:abc123",
  replyTo: "task-root",
};

function consoleChat(onSend = vi.fn().mockReturnValue(true), onCommand = vi.fn(), shown: ChatEntry[] = []) {
  const { rerender } = render(consolePane(shown, onSend, onCommand));
  return { onSend, onCommand, rerender, box: screen.getByRole("textbox") as HTMLTextAreaElement };
}

function consolePane(shown: ChatEntry[], onSend = vi.fn().mockReturnValue(true), onCommand = vi.fn()) {
  return (
    <Chat
      entries={shown}
      user="console"
      conversation="console:abc123"
      onProbe={() => {}}
      onSend={onSend}
      onCommand={onCommand}
    />
  );
}

const REPLY_BUTTON = { name: /^reply under: / };

describe("Chat", () => {
  it("renders the transcript read-only for web: no input, no send", () => {
    render(<Chat entries={entries} user="web" onProbe={() => {}} onSend={() => true} />);
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
    expect(onSend).toHaveBeenCalledWith("is acme-prod ready?", undefined);
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

  it("does not send WebKit's keyCode-229 Enter either, even with isComposing false", () => {
    const { onSend, box } = consoleChat();
    fireEvent.change(box, { target: { value: "日本" } });
    fireEvent.keyDown(box, { key: "Enter", isComposing: false, keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe("日本");
  });

  it("keeps the draft in the box when onSend refuses the turn", async () => {
    const onSend = vi.fn().mockReturnValue(false);
    const { box } = consoleChat(onSend);
    await userEvent.type(box, "hello{Enter}");
    expect(onSend).toHaveBeenCalledWith("hello", undefined);
    expect(box.value).toBe("hello");
  });

  it("sends nothing for a blank line", async () => {
    const { onSend, onCommand, box } = consoleChat();
    await userEvent.type(box, "   {Enter}");
    expect(onSend).not.toHaveBeenCalled();
    expect(onCommand).not.toHaveBeenCalled();
  });

  it("routes a local command word to onCommand and never to onSend", async () => {
    const { onSend, onCommand, box } = consoleChat();
    await userEvent.type(box, "/replay gateway{Enter}");
    expect(onCommand).toHaveBeenCalledWith({ name: "replay", session: "gateway" });
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe("");
  });

  it("sends /session and any other slash line to the gateway", async () => {
    const { onSend, onCommand, box } = consoleChat();
    await userEvent.type(box, "/session{Enter}");
    await userEvent.type(box, "/var/log/messages is full on node-3{Enter}");
    expect(onSend.mock.calls).toEqual([
      ["/session", undefined],
      ["/var/log/messages is full on node-3", undefined],
    ]);
    expect(onCommand).not.toHaveBeenCalled();
    expect(box.value).toBe("");
  });

  it("keeps the draft when a local command is refused", async () => {
    const { onSend, onCommand, box } = consoleChat();
    await userEvent.type(box, "/replay{Enter}");
    expect(onCommand).toHaveBeenCalledWith({ name: "error", text: "usage: /replay <session>" });
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe("/replay");
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

  it("sends /feedback summary from the usage summary button", async () => {
    const { onSend } = consoleChat();
    await userEvent.click(screen.getByRole("button", { name: /usage summary/i }));
    expect(onSend).toHaveBeenCalledWith("/feedback summary");
  });

  it("offers no usage summary button to the read-only web user", () => {
    render(<Chat entries={[]} user="web" onProbe={() => {}} />);
    expect(screen.queryByRole("button", { name: /usage summary/i })).toBeNull();
  });

  it("sends a reply under a card's report with the report named, then drops the chip", async () => {
    const { onSend, box } = consoleChat(undefined, undefined, [REPORT]);
    expect(screen.queryByText(/replying under the report/)).toBeNull();
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    // The chip names the report by its first line, never by the task id.
    const chip = screen.getByText(/replying under the report/);
    expect(chip.textContent).toContain("two pods pending on acme-prod");
    expect(chip.textContent).not.toContain("task-root");
    await userEvent.type(box, "and the second pod?{Enter}");
    expect(onSend).toHaveBeenLastCalledWith("and the second pod?", "task-root");
    expect(box.value).toBe("");
    expect(screen.queryByText(/replying under the report/)).toBeNull();
    // The next turn is an ordinary one.
    await userEvent.type(box, "thanks{Enter}");
    expect(onSend).toHaveBeenLastCalledWith("thanks", undefined);
  });

  it("keeps the reply armed across a settled line, which carries no replyTo", async () => {
    const { onSend, box } = consoleChat(undefined, undefined, [REPORT]);
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    // stop, a bare /session and a bare /feedback open no turn: the reply has nothing to ride.
    for (const line of ["stop", "/session", "/feedback"]) {
      await userEvent.type(box, `${line}{Enter}`);
      expect(onSend).toHaveBeenLastCalledWith(line, undefined);
      expect(box.value).toBe("");
      expect(screen.getByText(/replying under the report/)).toBeTruthy();
    }
    // The next typed turn carries it.
    await userEvent.type(box, "and the second pod?{Enter}");
    expect(onSend).toHaveBeenLastCalledWith("and the second pod?", "task-root");
    expect(screen.queryByText(/replying under the report/)).toBeNull();
  });

  it("keeps the chip when onSend refuses the reply", async () => {
    const onSend = vi.fn().mockReturnValue(false);
    const { box } = consoleChat(onSend, undefined, [REPORT]);
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    await userEvent.type(box, "and the second pod?{Enter}");
    expect(onSend).toHaveBeenCalledWith("and the second pod?", "task-root");
    expect(box.value).toBe("and the second pod?");
    expect(screen.getByText(/replying under the report/)).toBeTruthy();
  });

  it("clears the chip from its × without sending", async () => {
    const { onSend, box } = consoleChat(undefined, undefined, [REPORT]);
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    await userEvent.click(screen.getByRole("button", { name: /stop replying/ }));
    expect(screen.queryByText(/replying under the report/)).toBeNull();
    expect(onSend).not.toHaveBeenCalled();
    await userEvent.type(box, "hello{Enter}");
    expect(onSend).toHaveBeenLastCalledWith("hello", undefined);
  });

  it("cuts a long report line on the chip, Markdown marks stripped", async () => {
    const long = { ...REPORT, text: "**" + "p".repeat(70) + "** in `ns`\nmore" };
    consoleChat(undefined, undefined, [long]);
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    const chip = screen.getByText(/replying under the report/);
    expect(chip.textContent).toContain("p".repeat(60) + "…");
    expect(chip.textContent).not.toContain("p".repeat(61));
    expect(chip.textContent).not.toContain("**");
    // The transcript still shows the report as written.
    expect(screen.getByText(/in `ns`/)).toBeTruthy();
  });

  it("offers no reply on a report from an earlier conversation", () => {
    consoleChat(undefined, undefined, [{ ...REPORT, correlationId: "console:old" }, REPORT]);
    expect(screen.getAllByRole("button", REPLY_BUTTON)).toHaveLength(1);
  });

  it("drops an armed reply once its report leaves the transcript (/clear)", async () => {
    const onSend = vi.fn().mockReturnValue(true);
    const { box, rerender } = consoleChat(onSend, undefined, [REPORT]);
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    expect(screen.getByText(/replying under the report/)).toBeTruthy();
    rerender(consolePane([], onSend));
    expect(screen.queryByText(/replying under the report/)).toBeNull();
    await userEvent.type(box, "hello{Enter}");
    expect(onSend).toHaveBeenLastCalledWith("hello", undefined);
  });

  it("forgets a reply on /clear, so the report's later edit does not re-arm it", async () => {
    const onSend = vi.fn().mockReturnValue(true);
    const { box, rerender } = consoleChat(onSend, undefined, [REPORT]);
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    await userEvent.type(box, "/clear{Enter}");
    rerender(consolePane([], onSend));
    // The notifier edits its post; the reducer re-appends the entry under the same id.
    rerender(consolePane([{ ...REPORT, text: "one pod pending on acme-prod" }], onSend));
    expect(screen.queryByText(/replying under the report/)).toBeNull();
    await userEvent.type(box, "hello{Enter}");
    expect(onSend).toHaveBeenLastCalledWith("hello", undefined);
  });

  it("follows the report's edits while armed: the chip shows the current first line", async () => {
    const onSend = vi.fn().mockReturnValue(true);
    const { rerender } = consoleChat(onSend, undefined, [REPORT]);
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    rerender(consolePane([{ ...REPORT, text: "one pod pending on acme-prod\n- web-2" }], onSend));
    const chip = screen.getByText(/replying under the report/);
    expect(chip.textContent).toContain("one pod pending on acme-prod");
    expect(chip.textContent).not.toContain("two pods");
  });

  it("keeps the reply armed across the usage summary button, for the next typed turn", async () => {
    const { onSend, box } = consoleChat(undefined, undefined, [REPORT]);
    await userEvent.click(screen.getByRole("button", REPLY_BUTTON));
    await userEvent.click(screen.getByRole("button", { name: /usage summary/i }));
    expect(onSend).toHaveBeenLastCalledWith("/feedback summary");
    expect(screen.getByText(/replying under the report/)).toBeTruthy();
    await userEvent.type(box, "and the second pod?{Enter}");
    expect(onSend).toHaveBeenLastCalledWith("and the second pod?", "task-root");
  });

  it("names the report on the reply button, announces the chip, and moves focus to the box", async () => {
    const { box } = consoleChat(undefined, undefined, [REPORT]);
    const button = screen.getByRole("button", { name: "reply under: two pods pending on acme-prod" });
    await userEvent.click(button);
    expect(document.activeElement).toBe(box);
    expect(screen.getByRole("status").textContent).toContain("replying under the report");
    box.blur();
    await userEvent.click(screen.getByRole("button", { name: /stop replying/ }));
    expect(document.activeElement).toBe(box);
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

// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach } from "vitest";
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

describe("Chat", () => {
  it("renders the transcript read-only: no input, no send", () => {
    render(<Chat entries={entries} user="web" onProbe={() => {}} />);
    expect(screen.getByText("are we ready?")).toBeTruthy();
    expect(screen.getByText("acme-prod is ready")).toBeTruthy();
    expect(document.querySelector("input")).toBeNull();
    expect(screen.queryByText(/send/i)).toBeNull();
  });

  it("fires the read-only probe from the verify button", async () => {
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
        probe={{ outcome: "sent", detail: "no refusal within 2s — the publish went through; the web grant is broken", at: 2 }}
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
  });

  it("groups by correlation with one chip per exchange", () => {
    const { container } = render(<Chat entries={entries} user="web" onProbe={() => {}} />);
    expect(container.querySelectorAll(".corr-chip")).toHaveLength(1);
  });
});

// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import StatusStrip from "./StatusStrip.tsx";
import { initialState, type UiState } from "./model.ts";

afterEach(cleanup);

const state: UiState = {
  ...initialState,
  connection: "up",
  streamsUp: 3,
  streamsTotal: 4,
  now: 1_000,
  agents: new Map([
    ["gateway", { session: "gateway", agentType: "a2a-gateway", status: "idle" }],
    ["w1", { session: "w1", agentType: "claude-code", status: "active" }],
  ]),
  typePulses: new Map([["claude-code", 3]]),
  tasks: new Map([
    [
      "t1",
      {
        taskId: "t1",
        contextId: "c",
        correlationId: "k",
        addressee: "platform",
        owner: "gateway",
        state: "failed",
        final: true,
        artifacts: new Map(),
        lastEventAt: 1,
      },
    ],
  ]),
  streamStats: new Map([
    [
      "TASKS",
      {
        stat: { bytes: 90, maxBytes: 100, msgs: 5, consumers: 1, maxConsumers: -1, maxAgeMs: 0 },
        at: 1,
      },
    ],
  ]),
};

describe("StatusStrip", () => {
  it("shows the link, the types, and the counts", () => {
    render(<StatusStrip state={state} onFocus={() => {}} />);
    expect(screen.getByText("3/4 streams")).toBeTruthy();
    expect(screen.getByText("claude-code")).toBeTruthy();
    expect(screen.getByText("a2a-gateway")).toBeTruthy();
    expect(screen.getByTestId("tile-failures").textContent).toContain("1");
  });

  it("warns when a stream is not attached", () => {
    render(<StatusStrip state={state} onFocus={() => {}} />);
    expect(screen.getByTestId("tile-link").className).toContain("tile-warn");
  });

  it("turns the capacity tile amber past 80% and names the stream", () => {
    render(<StatusStrip state={state} onFocus={() => {}} />);
    const tile = screen.getByTestId("tile-capacity");
    expect(tile.className).toContain("tile-warn");
    expect(tile.textContent).toContain("TASKS 90%");
  });

  it("says why spend is tbd", () => {
    render(<StatusStrip state={state} onFocus={() => {}} />);
    const tile = screen.getByTestId("tile-spend");
    expect(tile.textContent).toContain("tbd");
    expect(tile.getAttribute("title")).toContain("a2a/worker-adapter/harness.go");
  });

  it("focuses the matching panel on click", async () => {
    const onFocus = vi.fn();
    render(<StatusStrip state={state} onFocus={onFocus} />);
    await userEvent.click(screen.getByTestId("tile-failures"));
    await userEvent.click(screen.getByTestId("tile-capacity"));
    await userEvent.click(screen.getByTestId("tile-spend"));
    expect(onFocus.mock.calls.map((c) => c[0])).toEqual(["tasks", "capacity", "spend"]);
  });
});

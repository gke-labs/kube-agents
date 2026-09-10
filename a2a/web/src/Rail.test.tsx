// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import Rail from "./Rail.tsx";
import { initialState, type UiState } from "./model.ts";

afterEach(cleanup);

// jsdom has no ResizeObserver; the rail only needs it for live re-measure.
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver?: unknown }).ResizeObserver = FakeResizeObserver;

function withAgents(state: UiState, ...sessions: string[]): UiState {
  const agents = new Map(state.agents);
  for (const session of sessions) {
    agents.set(session, { session, agentType: "hermes-bridge", status: "active" });
  }
  return { ...state, agents };
}

describe("Rail", () => {
  it("always shows the fixed you/gateway head, even before any traffic", () => {
    render(<Rail state={initialState} />);
    expect(screen.getByText("you")).toBeTruthy();
    expect(screen.getByText("gateway")).toBeTruthy();
    expect(screen.getByText("connecting")).toBeTruthy();
    expect(screen.getByText("no traffic yet")).toBeTruthy();
  });

  it("reports the browser's link on the you tap", () => {
    render(<Rail state={{ ...initialState, connection: "up" }} />);
    expect(screen.getByText("websocket")).toBeTruthy();
  });

  it("puts sessions heard on the bus onto the rail as replayable taps", () => {
    const state = withAgents({ ...initialState, connection: "up" }, "platform-bridge");
    render(<Rail state={state} />);
    expect(screen.getByText("platform-bridge")).toBeTruthy();
    expect(screen.getByRole("button", { name: /replay platform-bridge/i })).toBeTruthy();
  });

  // The bus delivers envelopes faster than the 120ms ghost step on a busy
  // install, and every non-anomalous envelope rebuilds state.agents. If the
  // step timer is tied to anything derived from that, the replay starves:
  // "replaying" shows in the footer and no chip ever lights.
  it("advances the replay while the bus keeps delivering envelopes", () => {
    vi.useFakeTimers();
    try {
      const base: UiState = {
        ...initialState,
        connection: "up",
        pulses: [
          { id: 1, fromSession: "platform-bridge", correlationId: "c1", kind: "status-update", at: 1 },
          { id: 2, fromSession: "platform-bridge", correlationId: "c1", kind: "artifact-update", at: 2 },
          { id: 3, fromSession: "platform-bridge", correlationId: "c1", kind: "status-update", at: 3 },
        ],
      };
      const state = withAgents(base, "platform-bridge");
      const { container, rerender } = render(<Rail state={state} />);
      fireEvent.click(screen.getByRole("button", { name: /replay platform-bridge/i }));

      // Traffic every 100ms — under the 120ms step — for a full second.
      for (let i = 0; i < 10; i++) {
        act(() => {
          vi.advanceTimersByTime(100);
        });
        const agents = new Map(state.agents);
        agents.set("platform-bridge", {
          ...agents.get("platform-bridge")!,
          lastActivity: 1000 + i,
        });
        rerender(<Rail state={{ ...state, agents }} />);
      }

      expect(container.querySelectorAll(".chip-on").length).toBeGreaterThan(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the stream attach count in the footer", () => {
    render(<Rail state={{ ...initialState, streamsUp: 4, streamsTotal: 4, streamMsgCount: 12 }} />);
    expect(screen.getByText(/4\/4 streams · 12 msgs/)).toBeTruthy();
  });
});

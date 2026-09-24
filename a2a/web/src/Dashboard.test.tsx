// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Dashboard from "./Dashboard.tsx";
import { initialState, type UiState } from "./model.ts";

afterEach(cleanup);

const NOW = 1_000_000;

const state: UiState = {
  ...initialState,
  now: NOW,
  agents: new Map([["gateway", { session: "gateway", agentType: "a2a-gateway", status: "idle", lastActivity: NOW - 5_000 }]]),
  liveness: new Map([
    [
      "gateway",
      {
        session: "gateway",
        durable: "gateway-relay",
        stream: "TASKS",
        perTask: false,
        found: false,
        waiting: 0,
        pending: 0,
        checkedAt: NOW,
      },
    ],
  ]),
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
        lastEventAt: NOW,
        endedAt: NOW,
        reason: "cluster acme-prod not found",
      },
    ],
  ]),
  streamAttach: new Map([["TOPICS-STATE", { error: "stream not found", since: NOW - 60_000 }]]),
  anomalies: { addressee: 0, postFinal: 2, missingSubmitted: 1 },
};

describe("Dashboard", () => {
  it("says a standing consumer is gone instead of showing a blank", () => {
    render(<Dashboard state={state} focus={null} onSession={() => {}} />);
    expect(screen.getByText("consumer gateway-relay not found on TASKS")).toBeTruthy();
  });

  it("says which stream is not attached and why", () => {
    render(<Dashboard state={state} focus={null} onSession={() => {}} />);
    expect(screen.getByText("TOPICS-STATE not attached since 1m 0s ago: stream not found")).toBeTruthy();
  });

  it("shows a failure's terminal reason", () => {
    render(<Dashboard state={state} focus={null} onSession={() => {}} />);
    expect(screen.getByText("cluster acme-prod not found")).toBeTruthy();
  });

  it("counts anomalies by kind", () => {
    render(<Dashboard state={state} focus={null} onSession={() => {}} />);
    const panel = document.querySelector('[data-panel="anomalies"]')!;
    expect(panel.textContent).toContain("post-final events2");
    expect(panel.textContent).toContain("missing submitted1");
  });

  it("carries the tbd rows with their reasons", () => {
    render(<Dashboard state={state} focus={null} onSession={() => {}} />);
    expect(document.querySelector('[data-panel="capacity"]')!.textContent).toContain("KV buckets");
    expect(document.querySelector('[data-panel="spend"]')!.textContent).toContain("total_cost_usd");
    expect(document.querySelector('[data-panel="tasks"]')!.textContent).toContain("tbd");
  });

  it("says an empty panel is empty", () => {
    render(<Dashboard state={state} focus={null} onSession={() => {}} />);
    expect(screen.getByText("no conversations on the bus yet")).toBeTruthy();
    expect(screen.getByText("no topics published yet")).toBeTruthy();
  });

  it("opens a session's transcript on click", async () => {
    const onSession = vi.fn();
    render(<Dashboard state={state} focus={null} onSession={onSession} />);
    await userEvent.click(screen.getByText("gateway", { selector: "td" }));
    expect(onSession).toHaveBeenCalledWith("gateway");
  });

  it("scrolls to the focused panel, every time it is focused", () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    const { rerender } = render(<Dashboard state={state} focus={{ key: "tasks", seq: 1 }} onSession={() => {}} />);
    rerender(<Dashboard state={state} focus={{ key: "tasks", seq: 2 }} onSession={() => {}} />);
    expect(scroll).toHaveBeenCalledTimes(2);
    expect(document.querySelector('[data-panel="tasks"]')!.className).toContain("panel-focused");
  });
});

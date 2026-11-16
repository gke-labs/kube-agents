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

  it("gives the sessions table a header row", () => {
    render(<Dashboard state={state} focus={null} onSession={() => {}} />);
    const header = document.querySelector('[data-panel="sessions"] thead tr')!;
    expect(header.textContent).toContain("session");
    expect(header.textContent).toContain("liveness");
  });

  it("reads a finished session as finished, not by its last liveness report", () => {
    const done: UiState = {
      ...state,
      agents: new Map([
        ["gateway", { session: "gateway", agentType: "a2a-gateway", status: "idle", lastActivity: NOW - 5_000 }],
        ["w-1", { session: "w-1", agentType: "claude-code", status: "done", lastActivity: NOW - 5_000, perTask: true }],
      ]),
      liveness: new Map([
        ...state.liveness,
        [
          "w-1",
          {
            session: "w-1",
            durable: "w-1-in",
            stream: "TASKS",
            perTask: true,
            found: true,
            waiting: 1,
            pending: 0,
            checkedAt: NOW,
          },
        ],
      ]),
    };
    render(<Dashboard state={done} focus={null} onSession={() => {}} />);
    expect(screen.getByText("finished - no consumer expected")).toBeTruthy();
    expect(screen.queryByText("live - pulling")).toBeNull();
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
  });

  it("says why spend is empty until a turn reports usage, and dashes the cost cell", () => {
    render(<Dashboard state={state} focus={null} onSession={() => {}} />);
    expect(document.querySelector('[data-panel="spend"]')!.textContent).toContain("no task has reported usage yet");
    expect(document.querySelector('[data-panel="tasks"] tbody')!.textContent).toContain("—");
  });

  it("shows a task's reported usage in its cost cell and sums it in the spend panel", () => {
    const t1 = state.tasks.get("t1")!;
    const withUsage: UiState = {
      ...state,
      tasks: new Map([
        ["t1", { ...t1, usage: { model: "claude-opus-5-5", inputTokens: 1500, outputTokens: 80, durationMs: 4000 } }],
        [
          "t2",
          { ...t1, taskId: "t2", usage: { model: "model-default", inputTokens: 500, outputTokens: 20, costUsd: 0.0123, durationMs: 1000 } },
        ],
      ]),
    };
    render(<Dashboard state={withUsage} focus={null} onSession={() => {}} />);
    const tasks = document.querySelector('[data-panel="tasks"]')!.textContent!;
    expect(tasks).toContain("1.5k in / 80 out, 4s, claude-opus-5-5");
    expect(tasks).toContain("500 in / 20 out, $0.0123, 1s, model-default");
    const spend = document.querySelector('[data-panel="spend"]')!.textContent!;
    expect(spend).toContain("tasks reporting2");
    expect(spend).toContain("tokens2k in / 100 out");
    expect(spend).toContain("cost$0.0123 over 1 of 2 tasks");
    // Two tasks, two models: the row names both, not the one the fold saw last.
    expect(spend).toContain("modelclaude-opus-5-5, model-default");
    expect(spend).toContain("turn time5s");
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

  it("opens a session's transcript from the keyboard", async () => {
    const onSession = vi.fn();
    render(<Dashboard state={state} focus={null} onSession={onSession} />);
    const row = screen.getByText("gateway", { selector: "td" }).closest("tr")!;
    row.focus();
    await userEvent.keyboard("{Enter}");
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

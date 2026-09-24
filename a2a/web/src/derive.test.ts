import { describe, expect, it } from "vitest";
import {
  capacityOf,
  conversationsByBackend,
  failuresOf,
  fmtAgo,
  fmtBytes,
  fmtDuration,
  livenessOf,
  QUIET_MS,
  typesOf,
  worstCapacity,
} from "./derive.ts";
import { initialState, type LivenessReport, type StreamStat, type TaskView, type UiState } from "./model.ts";

const NOW = 1_000_000_000;

function stat(over: Partial<StreamStat> = {}): StreamStat {
  return { bytes: 0, maxBytes: -1, msgs: 0, consumers: 0, maxConsumers: -1, maxAgeMs: 0, ...over };
}

function report(over: Partial<LivenessReport> = {}): LivenessReport {
  return {
    session: "gateway",
    durable: "gateway-relay",
    stream: "TASKS",
    perTask: false,
    found: true,
    waiting: 0,
    pending: 0,
    checkedAt: NOW,
    ...over,
  };
}

function task(taskId: string, over: Partial<TaskView> = {}): TaskView {
  return {
    taskId,
    contextId: "ctx",
    correlationId: `corr-${taskId}`,
    addressee: "platform",
    owner: "gateway",
    state: "working",
    final: false,
    artifacts: new Map(),
    lastEventAt: NOW,
    ...over,
  };
}

function withTasks(...tasks: TaskView[]): UiState {
  return { ...initialState, tasks: new Map(tasks.map((t) => [t.taskId, t])) };
}

describe("formatters", () => {
  it("formats bytes in binary units", () => {
    expect(fmtBytes(512)).toBe("512 B");
    expect(fmtBytes(1536)).toBe("1.5 KiB");
    expect(fmtBytes(256 * 1024 * 1024)).toBe("256 MiB");
  });

  it("formats durations at a readable grain", () => {
    expect(fmtDuration(850)).toBe("850ms");
    expect(fmtDuration(12_400)).toBe("12s");
    expect(fmtDuration(184_000)).toBe("3m 4s");
    expect(fmtDuration(2 * 3_600_000 + 5 * 60_000)).toBe("2h 5m");
  });

  it("says ago, and never a negative ago", () => {
    expect(fmtAgo(NOW - 12_000, NOW)).toBe("12s ago");
    expect(fmtAgo(NOW + 5_000, NOW)).toBe("just now");
  });
});

describe("capacity", () => {
  it("reports the tighter of bytes and consumers", () => {
    const c = capacityOf(
      "TASKS",
      { stat: stat({ bytes: 50, maxBytes: 100, consumers: 9, maxConsumers: 10 }), at: NOW },
      undefined,
      NOW,
    );
    expect(c.fraction).toBeCloseTo(0.9);
    expect(c.limitedBy).toBe("consumers");
    expect(c.warn).toBe(true);
    expect(c.text).toMatch(/TASKS.*9 of 10 consumers/);
  });

  it("treats -1 and 0 limits as unlimited", () => {
    const c = capacityOf("TASKS", { stat: stat({ bytes: 5000, maxBytes: -1, maxConsumers: 0 }), at: NOW }, undefined, NOW);
    expect(c.fraction).toBeNull();
    expect(c.warn).toBe(false);
    expect(c.text).toMatch(/no limit/);
  });

  it("says which stream is not attached and the last error, instead of a blank", () => {
    const c = capacityOf("TOPICS-STATE", undefined, { error: "stream not found", since: NOW - 60_000 }, NOW);
    expect(c.fraction).toBeNull();
    expect(c.text).toBe("TOPICS-STATE not attached since 1m 0s ago: stream not found");
  });

  it("says when stream info itself failed", () => {
    const c = capacityOf("TASKS", { stat: null, error: "permissions violation", at: NOW }, undefined, NOW);
    expect(c.text).toBe("TASKS: stream info failed: permissions violation");
  });

  it("picks the worst stream for the strip", () => {
    const state: UiState = {
      ...initialState,
      streamStats: new Map([
        ["TASKS", { stat: stat({ bytes: 10, maxBytes: 100 }), at: NOW }],
        ["DIRECTORY", { stat: stat({ bytes: 85, maxBytes: 100 }), at: NOW }],
      ]),
    };
    expect(worstCapacity(state)?.stream).toBe("DIRECTORY");
    expect(worstCapacity(initialState)).toBeNull();
  });
});

describe("liveness", () => {
  it("is live with a pull outstanding", () => {
    expect(livenessOf(report({ waiting: 1 }), NOW, false)).toEqual({ kind: "live", text: "live - pulling" });
  });

  it("is live with a recent delivery", () => {
    expect(livenessOf(report({ lastActive: NOW - 3_000 }), NOW, false).kind).toBe("live");
  });

  it("is quiet, and says since when, after QUIET_MS with no pull", () => {
    const l = livenessOf(report({ lastActive: NOW - QUIET_MS - 1_000 }), NOW, false);
    expect(l.kind).toBe("quiet");
    expect(l.text).toBe("quiet since 2m 1s ago - no pull outstanding");
  });

  it("is gone when a standing durable is missing", () => {
    expect(livenessOf(report({ found: false }), NOW, false)).toEqual({
      kind: "gone",
      text: "consumer gateway-relay not found on TASKS",
    });
  });

  it("is idle, not gone, when a per-task consumer is missing with no task running", () => {
    expect(livenessOf(report({ perTask: true, found: false }), NOW, false).kind).toBe("idle");
    expect(livenessOf(report({ perTask: true, found: false }), NOW, true).kind).toBe("gone");
  });

  it("says the lookup failed rather than guessing", () => {
    expect(livenessOf(report({ found: false, error: "timeout" }), NOW, false)).toEqual({
      kind: "error",
      text: "could not check consumer gateway-relay: timeout",
    });
  });

  it("is unknown for a session with no durable the page knows", () => {
    expect(livenessOf(undefined, NOW, false)).toEqual({ kind: "unknown", text: "no consumer known for this session" });
  });
});

describe("task views", () => {
  it("counts failed and rejected as failures, newest first", () => {
    const state = withTasks(
      task("a", { state: "failed", final: true, endedAt: NOW - 10 }),
      task("b", { state: "completed", final: true, endedAt: NOW - 5 }),
      task("c", { state: "rejected", final: true, endedAt: NOW - 1 }),
    );
    expect(failuresOf(state).map((t) => t.taskId)).toEqual(["c", "a"]);
  });
});

describe("types and conversations", () => {
  it("groups agents by type with the newest activity and the live pulse count", () => {
    const state: UiState = {
      ...initialState,
      agents: new Map([
        ["w1", { session: "w1", agentType: "claude-code", status: "active", lastActivity: 5 }],
        ["w2", { session: "w2", agentType: "claude-code", status: "done", lastActivity: 9 }],
        ["gateway", { session: "gateway", agentType: "a2a-gateway", status: "idle", lastActivity: 7 }],
      ]),
      typePulses: new Map([["claude-code", 4]]),
    };
    expect(typesOf(state)).toEqual([
      { agentType: "a2a-gateway", count: 1, lastActivity: 7, pulses: 0 },
      { agentType: "claude-code", count: 2, lastActivity: 9, pulses: 4 },
    ]);
  });

  it("groups conversations by backend, newest first", () => {
    const state: UiState = {
      ...initialState,
      conversations: new Map([
        ["console:a", { conversation: "console:a", backend: "console", lastSeen: 1, turns: 1 }],
        ["discord:x", { conversation: "discord:x", backend: "discord", lastSeen: 3, turns: 2 }],
        ["console:b", { conversation: "console:b", backend: "console", lastSeen: 2, turns: 1 }],
      ]),
    };
    expect(conversationsByBackend(state).map((g) => [g.backend, g.conversations.map((c) => c.conversation)])).toEqual([
      ["console", ["console:b", "console:a"]],
      ["discord", ["discord:x"]],
    ]);
  });
});

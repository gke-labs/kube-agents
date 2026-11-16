import { describe, expect, it } from "vitest";
import {
  SPEND_EMPTY_REASON,
  SPEND_NONE,
  fmtCost,
  fmtTokens,
  fmtUsage,
  spendOf,
  spendTile,
  taskSpend,
} from "./derive.ts";
import { initialState, type TaskView, type UiState } from "./model.ts";
import { usageOf, usageParts, usageTotal } from "./protocol.ts";

const NOW = 1_000_000_000;

function task(taskId: string, over: Partial<TaskView> = {}): TaskView {
  return {
    taskId,
    contextId: "ctx",
    correlationId: `corr-${taskId}`,
    addressee: "platform",
    owner: "gateway",
    state: "completed",
    final: true,
    artifacts: new Map(),
    lastEventAt: NOW,
    ...over,
  };
}

function withTasks(...tasks: TaskView[]): UiState {
  return { ...initialState, tasks: new Map(tasks.map((t) => [t.taskId, t])) };
}

describe("usage parts", () => {
  it("reads an object with at least one figure and ignores keys it does not know", () => {
    expect(usageOf({ inputTokens: 10, outputTokens: 2, vendorNote: "x" })).toEqual({
      model: undefined,
      inputTokens: 10,
      outputTokens: 2,
      costUsd: undefined,
      durationMs: undefined,
    });
    expect(usageOf({ model: "m", durationMs: 5 })?.model).toBe("m");
  });

  it("refuses a name alone, a negative figure, a non-number and a non-object", () => {
    expect(usageOf({ model: "m" })).toBeUndefined();
    expect(usageOf({ inputTokens: -1 })).toBeUndefined();
    expect(usageOf({ inputTokens: "10" })).toBeUndefined();
    expect(usageOf([1, 2])).toBeUndefined();
    expect(usageOf("usage")).toBeUndefined();
    expect(usageOf(null)).toBeUndefined();
  });

  it("reads an explicit zero as unknown, as the wire rule says, never as a known nothing", () => {
    expect(usageOf({ costUsd: 0 })).toBeUndefined();
    expect(usageOf({ costUsd: 0, outputTokens: 3 })).toEqual({
      model: undefined,
      inputTokens: undefined,
      outputTokens: 3,
      costUsd: undefined,
      durationMs: undefined,
    });
  });

  it("takes the data parts that are usage and skips the rest", () => {
    const parts = [
      { kind: "text", text: "not usage" },
      { kind: "data", data: { inputTokens: 1 } },
      { kind: "data", data: { tool: "kubectl" } },
    ];
    expect(usageParts(parts).map((u) => u.inputTokens)).toEqual([1]);
    expect(usageParts(undefined)).toEqual([]);
  });

  it("sums turns, keeps the last model, and leaves an absent figure absent", () => {
    expect(usageTotal([])).toBeUndefined();
    const total = usageTotal([
      { model: "a", inputTokens: 10, durationMs: 100 },
      { model: "b", inputTokens: 5, outputTokens: 2, durationMs: 50 },
    ]);
    expect(total).toEqual({
      model: "b",
      inputTokens: 15,
      outputTokens: 2,
      costUsd: undefined,
      durationMs: 150,
    });
  });
});

describe("spend formatting", () => {
  it("formats tokens compactly, picking the band from the rounded value", () => {
    expect(fmtTokens(999)).toBe("999");
    expect(fmtTokens(999.6)).toBe("1k");
    expect(fmtTokens(1000)).toBe("1k");
    expect(fmtTokens(1500)).toBe("1.5k");
    expect(fmtTokens(999_949)).toBe("999.9k");
    expect(fmtTokens(999_950)).toBe("1M");
    expect(fmtTokens(2_340_000)).toBe("2.3M");
  });

  it("keeps four places under a dollar and two above, and never prints a present cost as nothing", () => {
    expect(fmtCost(0.0123)).toBe("$0.0123");
    expect(fmtCost(12.5)).toBe("$12.50");
    expect(fmtCost(0.00004)).toBe("<$0.0001");
    expect(fmtCost(0.00005)).toBe("$0.0001");
  });

  it("writes only the figures a usage has, in a fixed order", () => {
    expect(
      fmtUsage({
        inputTokens: 1500,
        outputTokens: 80,
        costUsd: 0.05,
        durationMs: 4000,
        model: "m",
      }),
    ).toBe("1.5k in / 80 out, $0.0500, 4s, m");
    expect(fmtUsage({ durationMs: 250 })).toBe("250ms");
    // A count the producer did not send is not shown as zero.
    expect(fmtUsage({ outputTokens: 3 })).toBe("3 out");
    expect(fmtUsage({ inputTokens: 1500, durationMs: 100 })).toBe("1.5k in, 100ms");
  });

  it("dashes a task that reported nothing", () => {
    expect(taskSpend(task("t"))).toBe(SPEND_NONE);
    expect(taskSpend(task("t", { usage: { durationMs: 1000 } }))).toBe("1s");
  });
});

describe("spend totals", () => {
  it("counts the tasks that reported and sums them", () => {
    const state = withTasks(
      task("a", {
        usage: { inputTokens: 100, outputTokens: 10, costUsd: 0.1 },
      }),
      task("b"),
      task("c", { usage: { inputTokens: 50, durationMs: 20 } }),
    );
    // Two tasks naming different models: the sum names both, in first-seen
    // order, rather than whichever the fold visited last.
    const mixed = withTasks(
      task("a", { usage: { model: "model-default", inputTokens: 1 } }),
      task("b", { usage: { model: "claude-opus-5-5", inputTokens: 1 } }),
      task("c", { usage: { model: "model-default", inputTokens: 1 } }),
    );
    expect(spendOf(mixed).models).toEqual(["model-default", "claude-opus-5-5"]);
    expect(spendOf(state)).toEqual({
      reported: 2,
      priced: 1,
      counted: 2,
      models: [],
      total: {
        model: undefined,
        inputTokens: 150,
        outputTokens: 10,
        costUsd: 0.1,
        durationMs: 20,
      },
    });
  });

  it("says how many of the reporting tasks a cost or token sum covers", () => {
    const state = withTasks(
      task("a", { usage: { inputTokens: 100, outputTokens: 10, costUsd: 0.1 } }),
      task("b", { usage: { durationMs: 500 } }),
      task("c", { usage: { inputTokens: 50, durationMs: 20 } }),
    );
    expect(spendOf(state)).toMatchObject({ reported: 3, priced: 1, counted: 2 });
    expect(spendTile(state).title).toBe("3 tasks reported usage: $0.1000 over 1 of 3 tasks");
    const counted = withTasks(task("b", { usage: { durationMs: 500 } }), task("c", { usage: { inputTokens: 50 } }));
    expect(spendTile(counted).title).toBe("2 tasks reported usage: 50 tok over 1 of 2 tasks");
    const all = withTasks(task("a", { usage: { costUsd: 0.5 } }), task("b", { usage: { costUsd: 0.5 } }));
    expect(spendTile(all).title).toBe("2 tasks reported usage: $1.00");
  });

  it("tiles the empty reason, then cost, then tokens, then time", () => {
    expect(spendTile(withTasks(task("a")))).toEqual({
      text: "none yet",
      title: SPEND_EMPTY_REASON,
    });
    expect(spendTile(withTasks(task("a", { usage: { costUsd: 1.5, inputTokens: 1 } }))).text).toBe("$1.50");
    expect(spendTile(withTasks(task("a", { usage: { inputTokens: 900, outputTokens: 100 } }))).text).toBe("1k tok");
    // Durations alone, each task naming its model: the title sums the time
    // and names no single model as the sum's.
    const timed = spendTile(
      withTasks(task("a", { usage: { durationMs: 60_000, model: "model-default" } }), task("b", { usage: { durationMs: 5_000, model: "claude-opus-5-5" } })),
    );
    expect(timed.text).toBe("1m 5s");
    expect(timed.title).toBe("2 tasks reported usage: 1m 5s");
  });
});

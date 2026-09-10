import { describe, expect, it } from "vitest";
import {
  GATEWAY_GAP,
  MIN_WORKER_PITCH,
  RAIL_PAD_LEFT,
  RAIL_PAD_RIGHT,
  buildGhost,
  layoutTaps,
} from "./rail-layout.ts";
import { GATEWAY_SESSION, WEB_SESSION, initialState, type UiState } from "./model.ts";

const WIDTH = 1200;

describe("layoutTaps", () => {
  it("always leads with the fixed you/gateway head", () => {
    const taps = layoutTaps([], WIDTH);
    expect(taps.map((t) => t.session)).toEqual([WEB_SESSION, GATEWAY_SESSION]);
    expect(taps[0].x).toBe(RAIL_PAD_LEFT);
    expect(taps[1].x).toBe(RAIL_PAD_LEFT + GATEWAY_GAP);
  });

  it("keeps placed taps still when a later worker arrives", () => {
    const one = layoutTaps([{ session: "platform-bridge" }], WIDTH);
    const two = layoutTaps([{ session: "platform-bridge" }, { session: "chat-otter" }], WIDTH);
    const bridgeAt = (taps: typeof one) => taps.find((t) => t.session === "platform-bridge")!.x;
    expect(bridgeAt(two)).toBe(bridgeAt(one));
    const otter = two.find((t) => t.session === "chat-otter")!;
    expect(otter.x).toBeGreaterThan(bridgeAt(two));
  });

  it("filters the fixed sessions out of the worker list", () => {
    const taps = layoutTaps(
      [{ session: GATEWAY_SESSION }, { session: WEB_SESSION }, { session: "w1" }],
      WIDTH,
    );
    expect(taps.filter((t) => t.kind === "worker")).toHaveLength(1);
  });

  it("sheds the oldest workers when the fleet outgrows the rail", () => {
    const fleet = Array.from({ length: 40 }, (_, i) => ({ session: `w${i}` }));
    const taps = layoutTaps(fleet, 800);
    const workers = taps.filter((t) => t.kind === "worker");
    expect(workers.length).toBeLessThan(40);
    // The newest survive.
    expect(workers[workers.length - 1].session).toBe("w39");
    expect(workers.every((t) => t.compressed)).toBe(true);
    // Nothing runs off the right edge.
    expect(Math.max(...workers.map((w) => w.x))).toBeLessThanOrEqual(800 - RAIL_PAD_RIGHT);
    // And nothing sits closer than the minimum pitch.
    for (let i = 1; i < workers.length; i++) {
      expect(workers[i].x - workers[i - 1].x).toBeGreaterThanOrEqual(MIN_WORKER_PITCH - 1);
    }
  });
});

describe("buildGhost", () => {
  it("replays observed pulses for the session, newest tail first", () => {
    const state: UiState = {
      ...initialState,
      pulses: [
        { id: 1, fromSession: "platform-bridge", correlationId: "c1", kind: "status-update", at: 1 },
        { id: 2, fromSession: "gateway", correlationId: "c1", kind: "message", at: 2 },
        { id: 3, fromSession: "platform-bridge", correlationId: "c1", kind: "artifact-update", at: 3 },
      ],
    };
    const steps = buildGhost(state, "platform-bridge");
    expect(steps.map((s) => s.kind)).toEqual(["status-update", "artifact-update"]);
  });

  it("reconstructs from folded task history when there are no pulses (page reload)", () => {
    const state: UiState = {
      ...initialState,
      tasks: new Map([
        [
          "task-1",
          {
            taskId: "task-1",
            contextId: "ctx-1",
            correlationId: "c1",
            addressee: "platform",
            owner: "gateway",
            executor: "platform-bridge",
            state: "completed",
            final: true,
            artifacts: new Map([["result", { name: "result", text: "ready", chunks: 3 }]]),
            lastEventAt: 5,
          },
        ],
      ]),
    };
    const steps = buildGhost(state, "platform-bridge");
    expect(steps.map((s) => s.label)).toEqual([
      "status · submitted",
      "artifact · result ×3",
      "status · completed",
    ]);
  });

  it("returns nothing for a session it knows nothing about", () => {
    expect(buildGhost(initialState, "nobody")).toEqual([]);
  });
});

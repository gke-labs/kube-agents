/**
 * The dedup is the regression the live suite cannot force deterministically
 * (a tap restart replaying its stream mid-test), so it is pinned here:
 * livebus.test.ts observes single delivery on the live path, this asserts
 * that a redelivery would actually be dropped.
 */
import { describe, expect, it } from "vitest";
import type { StreamInfo } from "nats.ws";
import { durablesFor, isNotFound, makeDedup, parseLastActive, raceFlush, streamStatOf } from "./bus.ts";
import type { AgentView } from "./model.ts";

describe("makeDedup", () => {
  it("drops a repeated envelope id", () => {
    const dedup = makeDedup(4);
    expect(dedup("e1")).toBe(false);
    expect(dedup("e1")).toBe(true);
  });

  it("drops a whole replayed stream id-by-id", () => {
    const dedup = makeDedup(8);
    const ids = ["e1", "e2", "e3"];
    for (const id of ids) expect(dedup(id)).toBe(false);
    // A tap restart replays from the start of the stream.
    for (const id of ids) expect(dedup(id)).toBe(true);
  });

  it("evicts beyond the cap, which is the sequence watermark's cue", () => {
    const dedup = makeDedup(2);
    dedup("e1");
    dedup("e2");
    dedup("e3"); // evicts e1
    // Documented cost of the cap: an evicted id re-enters. Blocking replays
    // this old is the per-stream sequence watermark's job, not the dedup's.
    expect(dedup("e1")).toBe(false);
  });
});

function agent(over: Partial<AgentView>): AgentView {
  return { session: "s", agentType: "x", status: "active", ...over };
}

describe("durablesFor", () => {
  it("maps each executor shape to the durable it reads from", () => {
    expect(
      durablesFor([
        agent({ session: "gateway", agentType: "a2a-gateway" }),
        agent({ session: "platform-bridge", agentType: "hermes-bridge", profile: "platform" }),
        agent({ session: "w-abc12", agentType: "claude-code" }),
      ]),
    ).toEqual([
      { session: "gateway", name: "gateway-relay", stream: "TASKS", perTask: false },
      { session: "platform-bridge", name: "bridge-platform", stream: "TASKS", perTask: false },
      { session: "w-abc12", name: "w-abc12-in", stream: "TASKS", perTask: true },
    ]);
  });

  it("skips retired workers, a bridge with no profile, and unknown types", () => {
    expect(
      durablesFor([
        agent({ session: "w-old", agentType: "claude-code", status: "done" }),
        agent({ session: "b", agentType: "hermes-bridge" }),
        agent({ session: "c", agentType: "something-new" }),
      ]),
    ).toEqual([]);
  });

  it("never builds a consumer name that is not a single subject token", () => {
    // The name is interpolated into $JS.API.CONSUMER.INFO.TASKS.<name>. A dot
    // or wildcard would change which subject the request goes to.
    expect(
      durablesFor([
        agent({ session: "a.b", agentType: "claude-code" }),
        agent({ session: "p", agentType: "hermes-bridge", profile: ">" }),
      ]),
    ).toEqual([]);
  });
});

describe("poller helpers", () => {
  it("parses last_active, and treats the zero time as unknown", () => {
    expect(parseLastActive("2026-09-23T12:00:00Z")).toBe(Date.parse("2026-09-23T12:00:00Z"));
    expect(parseLastActive("0001-01-01T00:00:00Z")).toBeUndefined();
    expect(parseLastActive(undefined)).toBeUndefined();
    expect(parseLastActive("garbage")).toBeUndefined();
  });

  it("recognises a JetStream not-found and nothing else", () => {
    expect(isNotFound({ api_error: { code: 404, err_code: 10014, description: "consumer not found" } })).toBe(true);
    expect(isNotFound({ api_error: { code: 503 } })).toBe(false);
    expect(isNotFound(new Error("timeout"))).toBe(false);
    expect(isNotFound(null)).toBe(false);
  });

  it("reads the stream stat fields the capacity panel uses", () => {
    const info = {
      config: { max_bytes: 1_000_000, max_consumers: 256, max_age: 7 * 24 * 3600 * 1e9 },
      state: { bytes: 250_000, messages: 1200, consumer_count: 12, first_ts: "2026-09-20T00:00:00Z", last_seq: 1300 },
    } as unknown as StreamInfo;
    expect(streamStatOf(info)).toEqual({
      bytes: 250_000,
      maxBytes: 1_000_000,
      msgs: 1200,
      consumers: 12,
      maxConsumers: 256,
      firstTs: Date.parse("2026-09-20T00:00:00Z"),
      maxAgeMs: 7 * 24 * 3600 * 1000,
    });
  });

  it("leaves firstTs unset on an empty stream", () => {
    const info = {
      config: { max_bytes: -1, max_consumers: -1, max_age: 0 },
      state: { bytes: 0, messages: 0, consumer_count: 0, first_ts: "0001-01-01T00:00:00Z", last_seq: 0 },
    } as unknown as StreamInfo;
    expect(streamStatOf(info).firstTs).toBeUndefined();
  });
});

describe("raceFlush", () => {
  it("is true once the flush settles in time", async () => {
    await expect(raceFlush(Promise.resolve(), 50)).resolves.toBe(true);
  });

  it("is false when the flush rejects — the same failure a disconnect's resetOutbound produces", async () => {
    await expect(raceFlush(Promise.reject(new Error("draining")), 50)).resolves.toBe(false);
  });

  it("is false when the flush never settles before the timeout", async () => {
    const never = new Promise<void>(() => {
      /* simulates a flush the reconnect loop never resolves */
    });
    await expect(raceFlush(never, 10)).resolves.toBe(false);
  });
});

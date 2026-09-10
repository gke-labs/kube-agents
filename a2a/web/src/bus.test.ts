/**
 * The dedup is the regression the live suite cannot force deterministically
 * (a tap restart replaying its stream mid-test), so it is pinned here:
 * livebus.test.ts observes single delivery on the live path, this asserts
 * that a redelivery would actually be dropped.
 */
import { describe, expect, it } from "vitest";
import { makeDedup } from "./bus.ts";

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

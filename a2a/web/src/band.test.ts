// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { BAND_DEFAULT, BAND_MAX, BAND_MIN, bandFromPointer, clampBand, loadBand, saveBand } from "./band.ts";

beforeEach(() => sessionStorage.clear());

describe("band", () => {
  it("clamps so neither half can be dragged shut", () => {
    expect(clampBand(0)).toBe(BAND_MIN);
    expect(clampBand(1)).toBe(BAND_MAX);
    expect(clampBand(0.5)).toBe(0.5);
  });

  it("falls back to the default for a value that isn't a number", () => {
    expect(clampBand(Number.NaN)).toBe(BAND_DEFAULT);
    expect(clampBand(Number.POSITIVE_INFINITY)).toBe(BAND_DEFAULT);
  });

  it("maps the pointer to a share of the area below the strip", () => {
    expect(bandFromPointer(300, 100, 400)).toBe(0.5);
    expect(bandFromPointer(50, 100, 400)).toBe(BAND_MIN);
    expect(bandFromPointer(300, 100, 0)).toBe(BAND_DEFAULT);
  });

  it("remembers the split for the tab and ignores junk in storage", () => {
    expect(loadBand()).toBe(BAND_DEFAULT);
    saveBand(0.3);
    expect(loadBand()).toBe(0.3);
    sessionStorage.setItem("a2a-web-band", "banana");
    expect(loadBand()).toBe(BAND_DEFAULT);
  });
});

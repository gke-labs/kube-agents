/**
 * The split between the dashboard body and the chat pane. A demo gives the
 * dashboard the room, a working session gives it to the chat, and the tab
 * remembers which.
 */

export const BAND_MIN = 0.15;
export const BAND_MAX = 0.85;
export const BAND_DEFAULT = 0.6;
/** One arrow-key press on the separator. */
export const BAND_STEP = 0.05;

const STORAGE_KEY = "a2a-web-band";

export function clampBand(f: number): number {
  if (!Number.isFinite(f)) return BAND_DEFAULT;
  return Math.min(BAND_MAX, Math.max(BAND_MIN, f));
}

export function bandFromPointer(clientY: number, top: number, height: number): number {
  if (height <= 0) return BAND_DEFAULT;
  return clampBand((clientY - top) / height);
}

export function loadBand(): number {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw === null ? BAND_DEFAULT : clampBand(Number.parseFloat(raw));
  } catch {
    return BAND_DEFAULT;
  }
}

export function saveBand(f: number): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, String(clampBand(f)));
  } catch {
    // storage denied - the split resets next load
  }
}

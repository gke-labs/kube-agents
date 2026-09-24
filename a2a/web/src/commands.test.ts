import { describe, expect, it } from "vitest";
import { classifyInput, goTrim, HELP_TEXT } from "./commands.ts";
import { initialState } from "./model.ts";
import { streamsText, tasksText } from "./commands.ts";

describe("goTrim", () => {
  it("strips what Go's TrimSpace strips, including U+0085", () => {
    expect(goTrim("\u0085  hi 　 ")).toBe("hi");
  });

  it("keeps a BOM, which Go does not trim and JS trim() would", () => {
    expect(goTrim("﻿hi")).toBe("﻿hi");
  });
});

describe("classifyInput", () => {
  it("never sends an empty or whitespace-only turn", () => {
    expect(classifyInput("")).toEqual({ kind: "empty" });
    expect(classifyInput(" \n\t\u0085 ")).toEqual({ kind: "empty" });
  });

  it("sends the trimmed text", () => {
    expect(classifyInput("  is acme-prod ready?\n")).toEqual({ kind: "send", text: "is acme-prod ready?" });
  });

  it("treats leading whitespace before a slash as a command, not a turn", () => {
    expect(classifyInput("  /new")).toEqual({ kind: "command", command: { name: "new" } });
  });

  it("parses every command", () => {
    expect(classifyInput("/replay platform-bridge")).toEqual({
      kind: "command",
      command: { name: "replay", session: "platform-bridge" },
    });
    for (const name of ["tasks", "streams", "clear", "help"] as const) {
      expect(classifyInput(`/${name}`)).toEqual({ kind: "command", command: { name } });
    }
  });

  it("gives a usage line for /replay without a session", () => {
    expect(classifyInput("/replay")).toEqual({
      kind: "command",
      command: { name: "error", text: "usage: /replay <session>" },
    });
  });

  it("refuses an unknown command locally and says nothing was sent", () => {
    expect(classifyInput("/deploy prod")).toEqual({
      kind: "command",
      command: { name: "error", text: "unknown command /deploy. Nothing was sent. /help lists the commands." },
    });
  });

  it("refuses by UTF-8 bytes, not characters", () => {
    // 6000 euro signs are 6000 characters and 18000 bytes: over the gateway's 16384 cap.
    expect(classifyInput("€".repeat(6000))).toEqual({ kind: "tooBig", bytes: 18000 });
    expect(classifyInput("a".repeat(16384))).toEqual({ kind: "send", text: "a".repeat(16384) });
  });

  it("measures the trimmed text, as the gateway does", () => {
    expect(classifyInput(`  ${"a".repeat(16384)}  `).kind).toBe("send");
  });

  it("lists every command in the help", () => {
    for (const c of ["/new", "/replay", "/tasks", "/streams", "/clear", "/help"]) {
      expect(HELP_TEXT).toContain(c);
    }
  });
});

describe("command output", () => {
  it("says there are no tasks instead of printing nothing", () => {
    expect(tasksText(initialState)).toBe("no tasks on the bus yet");
  });

  it("names every stream, attached or not", () => {
    const text = streamsText({
      ...initialState,
      streamAttach: new Map([["TASKS", { error: null, since: 1 }]]),
    });
    expect(text).toMatch(/^1 of 4 streams attached/);
    for (const s of ["TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL"]) expect(text).toContain(s);
  });
});

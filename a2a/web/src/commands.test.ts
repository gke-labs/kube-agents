import { describe, expect, it } from "vitest";
import { classifyInput, commandEffect, goTrim, HELP_TEXT, tooBigText } from "./commands.ts";
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

  it("reads the page's own command words case-insensitively", () => {
    expect(classifyInput("/Clear")).toEqual({ kind: "command", command: { name: "clear" } });
    expect(classifyInput("/HELP")).toEqual({ kind: "command", command: { name: "help" } });
    expect(classifyInput("/Replay platform-bridge")).toEqual({
      kind: "command",
      command: { name: "replay", session: "platform-bridge" },
    });
  });

  it("gives a usage line for /replay without a session", () => {
    expect(classifyInput("/replay")).toEqual({
      kind: "command",
      command: { name: "error", text: "usage: /replay <session>" },
    });
  });

  it("sends a slash line whose first word is not a local command, as the gateway reads it", () => {
    expect(classifyInput("/deploy prod")).toEqual({ kind: "send", text: "/deploy prod" });
    expect(classifyInput("/var/log/messages is full on node-3")).toEqual({
      kind: "send",
      text: "/var/log/messages is full on node-3",
    });
    expect(classifyInput("/News")).toEqual({ kind: "send", text: "/News" });
  });

  it("sends /session and its forms to the gateway, which resolves them itself", () => {
    for (const t of ["/session", "/session off", "/session check the nodes"]) {
      expect(classifyInput(t)).toEqual({ kind: "send", text: t });
    }
  });

  it("caps a sent slash line like any other turn", () => {
    expect(classifyInput(`/x ${"€".repeat(6000)}`).kind).toBe("tooBig");
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
    expect(HELP_TEXT).toContain("/session");
  });
});

describe("command output", () => {
  it("says there are no tasks instead of printing nothing", () => {
    expect(tasksText(initialState)).toBe("no tasks on the bus yet");
  });

  it("names every stream, attached or not", () => {
    const text = streamsText({
      ...initialState,
      connection: "up",
      streamAttach: new Map([["TASKS", { error: null, since: 1 }]]),
    });
    expect(text).toMatch(/^1 of 4 streams attached/);
    for (const s of ["TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL"]) expect(text).toContain(s);
  });

  it("says streams unknown instead of a stale attach count while the link is down", () => {
    const text = streamsText({
      ...initialState,
      connection: "down",
      // Recorded while up; an ordered consumer's iterator survives a
      // reconnect, so this would otherwise still read as attached.
      streamAttach: new Map([["TASKS", { error: null, since: 1 }]]),
    });
    expect(text).toMatch(/^streams unknown/);
    expect(text).not.toContain("attached");
  });
});

describe("commandEffect", () => {
  const withGateway = {
    ...initialState,
    agents: new Map([["gateway", { session: "gateway", agentType: "a2a-gateway", status: "idle" as const }]]),
  };

  it("turns the text commands into local lines", () => {
    expect(commandEffect({ name: "help" }, initialState)).toEqual({ kind: "local", text: HELP_TEXT });
    expect(commandEffect({ name: "tasks" }, initialState)).toEqual({ kind: "local", text: "no tasks on the bus yet" });
    expect(commandEffect({ name: "streams" }, initialState).kind).toBe("local");
    expect(commandEffect({ name: "error", text: "usage: /replay <session>" }, initialState)).toEqual({
      kind: "local",
      text: "usage: /replay <session>",
    });
  });

  it("passes new and clear through for App to carry out", () => {
    expect(commandEffect({ name: "new" }, initialState)).toEqual({ kind: "new" });
    expect(commandEffect({ name: "clear" }, initialState)).toEqual({ kind: "clear" });
  });

  it("replays a session the page has seen", () => {
    expect(commandEffect({ name: "replay", session: "gateway" }, withGateway)).toEqual({
      kind: "replay",
      session: "gateway",
    });
  });

  it("says so when the session was never seen, instead of opening an empty view", () => {
    expect(commandEffect({ name: "replay", session: "nope" }, withGateway)).toEqual({
      kind: "local",
      text: "no session named nope on the bus. The sessions panel lists the ones seen.",
    });
  });

  it("names the size and the cap in the refusal", () => {
    expect(tooBigText(20_000)).toBe("not sent: 20000 bytes is over the gateway's 16384-byte limit. Trim it and send again.");
  });
});

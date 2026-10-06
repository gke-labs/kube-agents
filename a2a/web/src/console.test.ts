import { describe, expect, it } from "vitest";
import {
  CONSOLE_TEXT_CAP,
  delegateRest,
  encodeInFrame,
  isSessionOffArg,
  isStopTurn,
  normalizeTurn,
  sessionCommandRest,
  turnFate,
  inSubject,
  mintConversation,
  mintMessageId,
  outSubject,
  parseOutFrame,
  textBytes,
  tokenOf,
} from "./console.ts";

const zeros = (buf: Uint8Array) => buf.fill(0);
const ones = (buf: Uint8Array) => buf.fill(0xab);

describe("console conversation ids", () => {
  it("accepts a console id and returns its token", () => {
    expect(tokenOf("console:abc-123")).toBe("abc-123");
  });

  it("rejects anything the gateway's token rule rejects", () => {
    for (const bad of [
      "discord:abc",
      "console:",
      "console:a.b",
      "console:ABC",
      "console:-abc",
      "console:abc-",
      "console:a b",
      "console:>",
      `console:${"a".repeat(64)}`,
    ]) {
      expect(tokenOf(bad), bad).toBeNull();
    }
    expect(tokenOf(`console:${"a".repeat(63)}`)).toBe("a".repeat(63));
  });

  it("builds the in and out subjects, and throws rather than build a bad one", () => {
    expect(inSubject("console:abc")).toBe("chat.console.abc.in");
    expect(outSubject("console:abc")).toBe("chat.console.abc.out");
    expect(() => inSubject("console:a.b")).toThrow(/not a console conversation id/);
    expect(() => outSubject("gchat:spaces/x")).toThrow(/not a console conversation id/);
  });

  it("mints ids from the random source it is given", () => {
    expect(mintConversation(zeros)).toBe("console:0000000000000000");
    expect(mintConversation(ones)).toBe("console:abababababababab");
    expect(mintMessageId(zeros)).toBe("m-000000000000");
  });

  it("mints a valid token with the real random source", () => {
    const conv = mintConversation();
    expect(tokenOf(conv)).not.toBeNull();
    expect(mintConversation()).not.toBe(conv);
  });

  it("never mints a token ending in a hyphen", () => {
    // Minted tokens are hex ([0-9a-f]) end to end, which can never produce a
    // trailing hyphen -- but the rule that would reject one if it did is the
    // same rule this asserts against, so a change to either stays honest.
    for (const fill of [0x00, 0xff, 0xab, 0x0a, 0x1f]) {
      const conv = mintConversation((buf) => buf.fill(fill));
      expect(conv.endsWith("-")).toBe(false);
      expect(tokenOf(conv)).not.toBeNull();
    }
  });
});

describe("console frames", () => {
  it("measures text in UTF-8 bytes, the way the gateway's len() does", () => {
    expect(textBytes("abc")).toBe(3);
    expect(textBytes("€")).toBe(3);
    expect(textBytes("€".repeat(6000))).toBeGreaterThan(CONSOLE_TEXT_CAP);
  });

  it("encodes an inbound frame without a kind", () => {
    const decoded = JSON.parse(
      new TextDecoder().decode(encodeInFrame({ messageId: "m-1", text: "hi" })),
    );
    expect(decoded).toEqual({ messageId: "m-1", text: "hi" });
  });

  it("parses an outbound frame and normalizes edit", () => {
    expect(parseOutFrame('{"messageId":"c-1","text":"⏳ submitted…"}')).toEqual({
      messageId: "c-1",
      text: "⏳ submitted…",
      edit: false,
    });
    expect(parseOutFrame('{"messageId":"c-1","text":"x","edit":true}')?.edit).toBe(true);
    expect(parseOutFrame('{"messageId":"c-1","text":"x","edit":"yes"}')?.edit).toBe(false);
  });

  it("returns null for a frame it cannot use", () => {
    for (const bad of ["not json", "[]", "null", '{"text":"x"}', '{"messageId":"","text":"x"}', '{"messageId":"c-1"}']) {
      expect(parseOutFrame(bad), bad).toBeNull();
    }
  });
});

describe("gateway text mirrors", () => {
  it("normalizes like text.go normalize", () => {
    expect(normalizeTurn("  What's it DOING?!  ")).toBe("whats it doing");
    expect(normalizeTurn("a\t\n  b")).toBe("a b");
    expect(normalizeTurn("a\rb")).toBe("ab");
  });

  it("reads the stop words like isStop", () => {
    for (const t of ["stop", "STOP", " cancel. ", "abort!"]) expect(isStopTurn(t)).toBe(true);
    for (const t of ["stop it", "stopped", "please stop"]) expect(isStopTurn(t)).toBe(false);
  });

  it("strips a delegate turn like isDelegate", () => {
    expect(delegateRest("delegate: check the nodes")).toBe("check the nodes");
    expect(delegateRest("  Delegate - Check It")).toBe("Check It");
    expect(delegateRest("DELEGATE\u2014 x")).toBe("x");
    expect(delegateRest("delegate,:-  x ")).toBe("x");
    expect(delegateRest("delegate\tx")).toBe("x");
    expect(delegateRest("delegate")).toBeNull();
    expect(delegateRest("delegate:  ")).toBeNull();
    expect(delegateRest("delegated tasks are neat")).toBeNull();
    expect(delegateRest("delegate.x")).toBeNull();
  });

  it("reads /session like isSessionCommand and isSessionOff", () => {
    expect(sessionCommandRest("/session")).toBe("");
    expect(sessionCommandRest("  /Session   off  ")).toBe("off");
    expect(sessionCommandRest("/session\u00a0check it")).toBe("check it");
    expect(sessionCommandRest("/\u017fession")).toBe("");
    expect(sessionCommandRest("/sessions")).toBeNull();
    expect(sessionCommandRest("session")).toBeNull();
    expect(sessionCommandRest("/var/log/messages is full")).toBeNull();
    expect(isSessionOffArg("Off!")).toBe(true);
    expect(isSessionOffArg("off now")).toBe(false);
  });

  it("settles only the turns no gateway branch makes a task of", () => {
    for (const t of ["stop", "/session", "/session off", "/session stop"]) {
      expect(turnFate(t)).toEqual({ kind: "settled" });
    }
    expect(turnFate("is acme-prod ready?")).toEqual({ kind: "task", texts: ["is acme-prod ready?"] });
    expect(turnFate("delegate: x")).toEqual({ kind: "task", texts: ["delegate: x", "x"] });
    expect(turnFate("/session delegate: x")).toEqual({ kind: "task", texts: ["delegate: x", "x"] });
    expect(turnFate("/session check it")).toEqual({ kind: "task", texts: ["check it"] });
  });
});

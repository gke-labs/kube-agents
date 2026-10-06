// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_USER,
  READ_ONLY_USER,
  loadConfig,
  loadConversation,
  saveConversation,
} from "./config.ts";

afterEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("config", () => {
  it("connects as console by default, and web is still reachable by URL", () => {
    expect(DEFAULT_USER).toBe("console");
    expect(READ_ONLY_USER).toBe("web");
    window.history.replaceState(null, "", "/?pass=p");
    expect(loadConfig()?.user).toBe("console");
    window.history.replaceState(null, "", "/?pass=p&user=web");
    expect(loadConfig()?.user).toBe("web");
  });

  it("round-trips the conversation id through session storage", () => {
    expect(loadConversation()).toBeNull();
    saveConversation("console:abc");
    expect(loadConversation()).toBe("console:abc");
  });

  it("ignores a stored conversation id the gateway would reject", () => {
    sessionStorage.setItem("a2a-web-conversation", "console:Not.Valid");
    expect(loadConversation()).toBeNull();
    sessionStorage.setItem("a2a-web-conversation", "console:abc-");
    expect(loadConversation()).toBeNull();
  });
});

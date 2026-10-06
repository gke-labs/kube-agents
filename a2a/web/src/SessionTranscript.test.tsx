// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SessionTranscript from "./SessionTranscript.tsx";
import { initialState, type UiState } from "./model.ts";

afterEach(cleanup);

const state: UiState = {
  ...initialState,
  agents: new Map([
    ["w1", { session: "w1", agentType: "claude-code", status: "active" }],
    ["w2", { session: "w2", agentType: "claude-code", status: "idle" }],
  ]),
  chat: [
    { id: "a", kind: "progress", session: "w1", text: "w1 working", correlationId: "k1" },
    { id: "b", kind: "progress", session: "w2", text: "w2 working", correlationId: "k2" },
  ],
};

describe("SessionTranscript", () => {
  it("shows only the chosen session's entries", () => {
    render(<SessionTranscript state={state} session="w1" onBack={() => {}} />);
    expect(screen.getByText("w1 working")).toBeTruthy();
    expect(screen.queryByText("w2 working")).toBeNull();
    expect(screen.getByText("w1")).toBeTruthy();
    expect(screen.getByText("claude-code")).toBeTruthy();
  });

  it("says the transcript is empty rather than showing a blank", () => {
    render(<SessionTranscript state={{ ...state, chat: [] }} session="w1" onBack={() => {}} />);
    expect(screen.getByText("nothing from w1 in the retention window")).toBeTruthy();
  });

  it("goes back to the dashboard", async () => {
    const onBack = vi.fn();
    render(<SessionTranscript state={state} session="w1" onBack={onBack} />);
    await userEvent.click(screen.getByRole("button", { name: /dashboard/i }));
    expect(onBack).toHaveBeenCalledOnce();
  });
});

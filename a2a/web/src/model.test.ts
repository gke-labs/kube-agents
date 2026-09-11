import { describe, expect, it } from "vitest";
import type { Envelope, Kind } from "./protocol.ts";
import { parseSubject } from "./protocol.ts";
import {
  GATEWAY_SESSION,
  IDLE_MS,
  initialState,
  reduce,
  type BusEvent,
  type UiState,
} from "./model.ts";

let seq = 0;

function env(partial: Partial<Envelope> & { kind: Kind }): Envelope {
  seq += 1;
  return {
    protocol: "a2a-jetstream/0.4",
    envelopeId: `env-${seq}`,
    correlationId: "corr-1",
    ts: "2026-08-31T12:00:00Z",
    from: { session: GATEWAY_SESSION, agentType: "a2a-gateway" },
    payload: {},
    ...partial,
  };
}

const RECEIVED_AT = Date.parse("2026-08-31T12:00:00Z");

function onSubject(
  state: UiState,
  subject: string,
  e: Envelope,
  live = true,
  at = RECEIVED_AT,
): UiState {
  const event: BusEvent = { type: "envelope", env: e, subject: parseSubject(subject), live, at };
  return reduce(state, event);
}

/** The full beat: gateway submits to platform, the bridge answers. */
function submission(state: UiState = initialState): UiState {
  return onSubject(
    state,
    "a2a.tasks.platform.task-1.in",
    env({
      kind: "message",
      taskId: "task-1",
      contextId: "ctx-1",
      payload: { role: "user", parts: [{ kind: "text", text: "are we ready to upgrade?" }] },
    }),
  );
}

const bridge = { session: "platform-bridge", agentType: "hermes-bridge", profile: "platform" };

describe("message", () => {
  it("creates the task with the subject's addressee and echoes the ask", () => {
    const state = submission();
    const task = state.tasks.get("task-1")!;
    expect(task.addressee).toBe("platform");
    expect(task.owner).toBe(GATEWAY_SESSION);
    expect(task.state).toBe("submitted");
    expect(state.chat).toHaveLength(1);
    expect(state.chat[0]).toMatchObject({ kind: "user", text: "are we ready to upgrade?" });
  });

  it("treats a second message on a known task as steering", () => {
    let state = submission();
    state = onSubject(
      state,
      "a2a.tasks.platform.task-1.in",
      env({
        kind: "message",
        taskId: "task-1",
        contextId: "ctx-1",
        payload: { role: "user", parts: [{ text: "focus on acme-prod" }] },
      }),
    );
    expect(state.chat[1]).toMatchObject({ kind: "steer", text: "focus on acme-prod" });
  });

  it("puts the publisher on the rail", () => {
    const state = submission();
    expect(state.agents.get(GATEWAY_SESSION)?.status).toBe("active");
  });
});

describe("status-update", () => {
  it("tracks state and records the executor", () => {
    let state = submission();
    state = onSubject(
      state,
      "a2a.tasks.platform.task-1.events",
      env({
        kind: "status-update",
        taskId: "task-1",
        contextId: "ctx-1",
        from: bridge,
        payload: { taskId: "task-1", contextId: "ctx-1", status: { state: "working" } },
      }),
    );
    const task = state.tasks.get("task-1")!;
    expect(task.state).toBe("working");
    expect(task.executor).toBe("platform-bridge");
  });

  it("does not retire a standing service on terminal, but does retire a per-session worker", () => {
    // The bridge answers for `platform` under its own session name: standing.
    let state = submission();
    state = onSubject(
      state,
      "a2a.tasks.platform.task-1.events",
      env({
        kind: "status-update",
        taskId: "task-1",
        contextId: "ctx-1",
        from: bridge,
        payload: {
          taskId: "task-1",
          contextId: "ctx-1",
          status: { state: "completed" },
          final: true,
        },
      }),
    );
    expect(state.agents.get("platform-bridge")?.status).toBe("active");

    // A worker answering as the addressee of its own subject: per-session.
    state = onSubject(
      state,
      "a2a.tasks.chat-otter.task-2.in",
      env({
        kind: "message",
        taskId: "task-2",
        contextId: "ctx-2",
        payload: { role: "user", parts: [{ text: "delegate: haiku" }] },
      }),
    );
    state = onSubject(
      state,
      "a2a.tasks.chat-otter.task-2.events",
      env({
        kind: "status-update",
        taskId: "task-2",
        contextId: "ctx-2",
        from: { session: "chat-otter", agentType: "claude-code" },
        payload: {
          taskId: "task-2",
          contextId: "ctx-2",
          status: { state: "completed" },
          final: true,
        },
      }),
    );
    expect(state.agents.get("chat-otter")?.status).toBe("done");
  });

  it("surfaces a status message (input-required's question) in the transcript", () => {
    let state = submission();
    state = onSubject(
      state,
      "a2a.tasks.platform.task-1.events",
      env({
        kind: "status-update",
        taskId: "task-1",
        contextId: "ctx-1",
        from: bridge,
        payload: {
          taskId: "task-1",
          contextId: "ctx-1",
          status: {
            state: "input-required",
            message: { role: "agent", parts: [{ text: "which cluster?" }] },
          },
        },
      }),
    );
    expect(state.chat[1]).toMatchObject({ kind: "status", text: "which cluster?" });
  });
});

describe("artifact-update", () => {
  function artifact(
    state: UiState,
    name: string,
    text: string,
    append = false,
    taskId = "task-1",
  ): UiState {
    return onSubject(
      state,
      `a2a.tasks.platform.${taskId}.events`,
      env({
        kind: "artifact-update",
        taskId,
        contextId: "ctx-1",
        from: bridge,
        payload: {
          taskId,
          contextId: "ctx-1",
          artifact: { artifactId: `art-${name}`, name, parts: [{ kind: "text", text }] },
          append,
        },
      }),
    );
  }

  it("streams result chunks into one merged answer entry", () => {
    let state = submission();
    state = artifact(state, "result", "acme-prod is ");
    state = artifact(state, "result", "ready", true);
    const answers = state.chat.filter((c) => c.kind === "answer");
    expect(answers).toHaveLength(1);
    expect(answers[0].text).toBe("acme-prod is ready");
    expect(state.tasks.get("task-1")?.artifacts.get("result")).toMatchObject({
      text: "acme-prod is ready",
      chunks: 2,
    });
  });

  it("does not concatenate a non-append re-publish of result", () => {
    let state = submission();
    state = artifact(state, "result", "first answer");
    state = artifact(state, "result", "corrected answer"); // append absent: a replacement
    const answers = state.chat.filter((c) => c.kind === "answer");
    expect(answers).toHaveLength(2);
    expect(answers[1].text).toBe("corrected answer");
    expect(state.tasks.get("task-1")?.artifacts.get("result")?.text).toBe("corrected answer");
  });

  it("shows progress in the transcript and on the agent's tap", () => {
    let state = submission();
    state = artifact(state, "progress", "reading the topic");
    expect(state.chat[1]).toMatchObject({ kind: "progress", text: "reading the topic" });
    expect(state.agents.get("platform-bridge")?.statusLine).toBe("reading the topic");
  });

  it("keeps thinking and activity out of the transcript but on the task", () => {
    let state = submission();
    state = artifact(state, "thinking", "hmm");
    state = artifact(state, "activity", "ran a2a topics read");
    expect(state.chat).toHaveLength(1); // just the ask
    expect(state.tasks.get("task-1")?.artifacts.size).toBe(2);
  });
});

describe("directory and topics", () => {
  it("agent-card creates a profile tap; agent-closed retires it", () => {
    let state = onSubject(
      initialState,
      "a2a.agents.platform",
      env({ kind: "agent-card", payload: { name: "platform" } }),
    );
    expect(state.agents.get("platform")?.status).toBe("idle");
    state = onSubject(state, "a2a.agents.platform", env({ kind: "agent-closed" }));
    expect(state.agents.get("platform")?.status).toBe("closed");
  });

  it("topic-update lands a topic line naming the subject's topic", () => {
    const state = onSubject(
      initialState,
      "a2a.topics.agent.platform.upgrade-readiness",
      env({
        kind: "topic-update",
        from: { session: "platform", agentType: "hermes" },
        payload: { name: "upgrade-readiness", parts: [{ text: "3 of 4 clusters ready" }] },
      }),
    );
    expect(state.chat[0]).toMatchObject({
      kind: "topic",
      text: "upgrade-readiness: 3 of 4 clusters ready",
    });
  });
});

describe("pulses and liveness", () => {
  it("only live envelopes pulse; replayed history does not", () => {
    let state = submission(); // live
    state = onSubject(
      state,
      "a2a.tasks.platform.task-9.in",
      env({
        kind: "message",
        taskId: "task-9",
        contextId: "ctx-9",
        payload: { role: "user", parts: [] },
      }),
      false, // replay
    );
    expect(state.pulses).toHaveLength(1);
    expect(state.streamMsgCount).toBe(2);
  });

  it("a tick ages a quiet agent to idle", () => {
    const at = Date.parse("2026-08-31T12:00:00Z");
    let state = submission();
    state = reduce(state, { type: "tick", now: at + IDLE_MS + 1 });
    expect(state.agents.get(GATEWAY_SESSION)?.status).toBe("idle");
    // Fresh traffic revives it.
    state = onSubject(
      state,
      "a2a.tasks.platform.task-1.in",
      env({ kind: "cancel", taskId: "task-1", contextId: "ctx-1" }),
    );
    expect(state.agents.get(GATEWAY_SESSION)?.status).toBe("active");
  });
});

describe("protocol anomalies are surfaced, not folded", () => {
  it("flags an envelope whose `to` disagrees with the subject's addressee", () => {
    const state = onSubject(
      initialState,
      "a2a.tasks.platform.task-1.in",
      env({
        kind: "message",
        taskId: "task-1",
        contextId: "ctx-1",
        to: { session: "someone-else" },
        payload: { role: "user", parts: [{ text: "misaddressed" }] },
      }),
    );
    expect(state.chat[0].kind).toBe("anomaly");
    expect(state.chat[0].text).toContain('addressed to "someone-else"');
    // Not folded: no task created, no transcript line for the content.
    expect(state.tasks.size).toBe(0);
    expect(state.chat.filter((c) => c.kind === "user")).toHaveLength(0);
  });

  it("flags an event after the task's final event and leaves the task alone", () => {
    let state = submission();
    const terminal = (s: string, final: boolean) =>
      env({
        kind: "status-update",
        taskId: "task-1",
        contextId: "ctx-1",
        from: bridge,
        payload: { taskId: "task-1", contextId: "ctx-1", status: { state: s }, final },
      });
    state = onSubject(state, "a2a.tasks.platform.task-1.events", terminal("completed", true));
    state = onSubject(state, "a2a.tasks.platform.task-1.events", terminal("working", false));
    expect(state.chat[state.chat.length - 1].kind).toBe("anomaly");
    expect(state.tasks.get("task-1")?.state).toBe("completed");
  });

  it("a post-final event does not revive a retired per-session worker", () => {
    let state = onSubject(
      initialState,
      "a2a.tasks.chat-otter.task-2.in",
      env({
        kind: "message",
        taskId: "task-2",
        contextId: "ctx-2",
        payload: { role: "user", parts: [] },
      }),
    );
    const otter = { session: "chat-otter", agentType: "claude-code" };
    state = onSubject(
      state,
      "a2a.tasks.chat-otter.task-2.events",
      env({
        kind: "status-update",
        taskId: "task-2",
        contextId: "ctx-2",
        from: otter,
        payload: {
          taskId: "task-2",
          contextId: "ctx-2",
          status: { state: "completed" },
          final: true,
        },
      }),
    );
    expect(state.agents.get("chat-otter")?.status).toBe("done");
    state = onSubject(
      state,
      "a2a.tasks.chat-otter.task-2.events",
      env({
        kind: "artifact-update",
        taskId: "task-2",
        contextId: "ctx-2",
        from: otter,
        payload: {
          taskId: "task-2",
          contextId: "ctx-2",
          artifact: { name: "result", parts: [{ text: "late" }] },
        },
      }),
    );
    expect(state.agents.get("chat-otter")?.status).toBe("done");
  });
});

describe("chat ids", () => {
  // Ids key React's list. A counter-derived id collided across envelopes
  // (branches pass pre- and post-increment state), and React silently
  // dropped one of the colliding entries from the transcript.
  it("are unique across every branch that writes a line", () => {
    let state = submission();
    state = onSubject(
      state,
      "a2a.tasks.platform.task-1.events",
      env({
        kind: "status-update",
        taskId: "task-1",
        contextId: "ctx-1",
        from: bridge,
        payload: {
          taskId: "task-1",
          contextId: "ctx-1",
          status: {
            state: "input-required",
            message: { role: "agent", parts: [{ text: "which cluster?" }] },
          },
        },
      }),
    );
    state = onSubject(
      state,
      "a2a.topics.shared.blueprint",
      env({ kind: "topic-update", payload: { name: "blueprint", parts: [{ text: "v2" }] } }),
    );
    state = onSubject(
      state,
      "a2a.tasks.platform.task-1.in",
      env({ kind: "cancel", taskId: "task-1", contextId: "ctx-1" }),
    );
    state = onSubject(
      state,
      "a2a.tasks.platform.task-9.in",
      env({
        kind: "message",
        taskId: "task-9",
        contextId: "ctx-9",
        to: { session: "elsewhere" },
        payload: { role: "user", parts: [] },
      }),
    );
    const ids = state.chat.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("liveness uses the receive clock for live traffic", () => {
  it("a live envelope from a lagging publisher does not read as idle", () => {
    const now = Date.parse("2026-08-31T18:00:00Z");
    // Publisher's clock is hours behind; the envelope arrives right now.
    const state = onSubject(
      initialState,
      "a2a.tasks.platform.task-3.in",
      env({
        kind: "message",
        taskId: "task-3",
        contextId: "ctx-3",
        ts: "2026-08-31T12:00:00Z",
        payload: { role: "user", parts: [] },
      }),
      true,
      now,
    );
    const ticked = reduce(state, { type: "tick", now: now + 1000 });
    expect(ticked.agents.get(GATEWAY_SESSION)?.status).toBe("active");
  });
});

describe("plumbing events", () => {
  it("tracks connection, stream attach counts, and the probe verdict", () => {
    let state = reduce(initialState, { type: "connection", state: "up" });
    expect(state.connection).toBe("up");
    state = reduce(state, { type: "streams", up: 3, total: 4 });
    expect(state.streamsUp).toBe(3);
    state = reduce(state, {
      type: "probe",
      result: { outcome: "refused", detail: "Permissions Violation for Publish", at: 1 },
    });
    expect(state.probe?.outcome).toBe("refused");
  });
});

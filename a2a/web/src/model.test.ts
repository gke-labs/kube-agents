import { describe, expect, it } from "vitest";
import type { Envelope, Kind } from "./protocol.ts";
import { parseSubject } from "./protocol.ts";
import {
  GATEWAY_SESSION,
  IDLE_MS,
  PENDING_STALE_MS,
  durationMs,
  initialState,
  queueMs,
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

describe("liveness", () => {
  it("only live envelopes blink the type LED; replayed history does not", () => {
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
    expect(state.typePulses.get("a2a-gateway")).toBe(1);
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

const consoleAuthority = {
  requester: { principal: "p", backend: "console", subject: "s", verifiedBy: "nats-grant" },
  audience: { conversation: "console:abc", kind: "dm", roster: ["s"], rosterComplete: true },
  grants: null,
};

function status(
  state: UiState,
  taskState: string,
  ts: string,
  extra: { final?: boolean; text?: string; from?: Envelope["from"] } = {},
): UiState {
  return onSubject(
    state,
    "a2a.tasks.platform.task-1.events",
    env({
      kind: "status-update",
      taskId: "task-1",
      contextId: "ctx-1",
      ts,
      from: extra.from ?? bridge,
      payload: {
        taskId: "task-1",
        contextId: "ctx-1",
        final: extra.final ?? false,
        status: {
          state: taskState,
          ...(extra.text ? { message: { role: "agent", parts: [{ text: extra.text }] } } : {}),
        },
      },
    }),
  );
}

describe("dashboard task fields", () => {
  it("records backend, conversation and ask time from the submission's authority block", () => {
    const state = onSubject(
      initialState,
      "a2a.tasks.platform.task-1.in",
      env({
        kind: "message",
        taskId: "task-1",
        contextId: "ctx-1",
        ts: "2026-08-31T12:00:00Z",
        authority: consoleAuthority,
        payload: { role: "user", parts: [{ text: "hi" }] },
      }),
    );
    const task = state.tasks.get("task-1")!;
    expect(task.backend).toBe("console");
    expect(task.conversation).toBe("console:abc");
    expect(task.askAt).toBe(Date.parse("2026-08-31T12:00:00Z"));
  });

  it("derives queue time, duration and the terminal reason", () => {
    let state = submission(); // ts 12:00:00
    state = status(state, "submitted", "2026-08-31T12:00:04Z");
    state = status(state, "working", "2026-08-31T12:00:05Z");
    state = status(state, "failed", "2026-08-31T12:00:34Z", {
      final: true,
      text: "the session never started",
    });
    const task = state.tasks.get("task-1")!;
    expect(queueMs(task)).toBe(4_000);
    expect(durationMs(task)).toBe(30_000);
    expect(task.reason).toBe("the session never started");
    expect(task.sawSubmitted).toBe(true);
  });

  it("does not count a gateway status as the executor's first status", () => {
    let state = submission();
    state = status(state, "working", "2026-08-31T12:00:02Z", {
      from: { session: GATEWAY_SESSION, agentType: "a2a-gateway" },
    });
    expect(state.tasks.get("task-1")!.firstStatusAt).toBeUndefined();
    expect(queueMs(state.tasks.get("task-1")!)).toBeUndefined();
  });

  it("leaves queue time and duration undefined until the bus has said enough", () => {
    const task = submission().tasks.get("task-1")!;
    expect(queueMs(task)).toBeUndefined();
    expect(durationMs(task)).toBeUndefined();
  });
});

describe("anomaly counters", () => {
  it("counts a task that went terminal without a submitted status, once", () => {
    let state = submission();
    state = status(state, "working", "2026-08-31T12:00:05Z");
    state = status(state, "completed", "2026-08-31T12:00:09Z", { final: true });
    expect(state.anomalies.missingSubmitted).toBe(1);
    // A post-final event is its own anomaly and does not count the task again.
    state = status(state, "completed", "2026-08-31T12:00:10Z", { final: true });
    expect(state.anomalies.missingSubmitted).toBe(1);
    expect(state.anomalies.postFinal).toBe(1);
  });

  it("does not count a task whose executor said submitted", () => {
    let state = submission();
    state = status(state, "submitted", "2026-08-31T12:00:01Z");
    state = status(state, "completed", "2026-08-31T12:00:09Z", { final: true });
    expect(state.anomalies.missingSubmitted).toBe(0);
  });

  it("counts addressee disagreement", () => {
    const state = onSubject(
      initialState,
      "a2a.tasks.platform.task-1.in",
      env({
        kind: "message",
        taskId: "task-1",
        contextId: "ctx-1",
        to: { session: "someone-else" },
        payload: { role: "user", parts: [] },
      }),
    );
    expect(state.anomalies).toEqual({ addressee: 1, postFinal: 0, missingSubmitted: 0 });
  });
});

describe("types, conversations and topics", () => {
  it("counts live envelopes per agent type and ignores replay", () => {
    let state = submission(); // live, from the gateway
    state = status(state, "working", "2026-08-31T12:00:05Z"); // live, from the bridge
    state = onSubject(
      state,
      "a2a.tasks.platform.task-2.in",
      env({ kind: "message", taskId: "task-2", contextId: "ctx-2", payload: { parts: [] } }),
      false,
    );
    expect(state.typePulses.get("a2a-gateway")).toBe(1);
    expect(state.typePulses.get("hermes-bridge")).toBe(1);
  });

  it("tracks conversations by the authority block, counting turns", () => {
    const turn = (s: UiState, taskId: string, ts: string) =>
      onSubject(
        s,
        `a2a.tasks.platform.${taskId}.in`,
        env({
          kind: "message",
          taskId,
          contextId: "ctx",
          ts,
          authority: consoleAuthority,
          payload: { parts: [{ text: "x" }] },
        }),
      );
    let state = turn(initialState, "task-1", "2026-08-31T12:00:00Z");
    state = turn(state, "task-2", "2026-08-31T12:05:00Z");
    expect(state.conversations.get("console:abc")).toEqual({
      conversation: "console:abc",
      backend: "console",
      lastSeen: Date.parse("2026-08-31T12:05:00Z"),
      turns: 2,
    });
    // A message with no authority block is not a conversation.
    expect(submission().conversations.size).toBe(0);
  });

  it("keeps the newest value per topic, and an older replay does not overwrite it", () => {
    const topic = (s: UiState, text: string, ts: string) =>
      onSubject(
        s,
        "a2a.topics.agent.platform.upgrade-readiness",
        env({
          kind: "topic-update",
          ts,
          from: { session: "platform", agentType: "hermes" },
          payload: { name: "upgrade-readiness", parts: [{ text }] },
        }),
      );
    let state = topic(initialState, "3 of 4 ready", "2026-08-31T12:05:00Z");
    state = topic(state, "1 of 4 ready", "2026-08-31T12:00:00Z");
    expect(state.topics.get("platform/upgrade-readiness")).toMatchObject({
      topic: "upgrade-readiness",
      owner: "platform",
      summary: "3 of 4 ready",
      publisher: "platform",
    });
  });

  it("a tick records the clock", () => {
    expect(reduce(initialState, { type: "tick", now: 1234 }).now).toBe(1234);
  });
});

const SENT_AT = Date.parse("2026-08-31T11:59:59Z");

function sent(state: UiState, messageId: string, text: string, conversation = "console:abc"): UiState {
  return reduce(state, { type: "consoleSent", messageId, text, conversation, at: SENT_AT });
}

function consoleTurn(
  state: UiState,
  taskId: string,
  text: string,
  opts: { live?: boolean; conversation?: string } = {},
): UiState {
  return onSubject(
    state,
    `a2a.tasks.platform.${taskId}.in`,
    env({
      kind: "message",
      taskId,
      contextId: "ctx",
      correlationId: `corr-${taskId}`,
      authority: {
        ...consoleAuthority,
        audience: { ...consoleAuthority.audience, conversation: opts.conversation ?? "console:abc" },
      },
      payload: { role: "user", parts: [{ text }] },
    }),
    opts.live ?? true,
  );
}

describe("pending console turns", () => {
  it("shows a sent turn at once, then attaches it in place to the gateway's submission", () => {
    let state = sent(initialState, "m-1", "is acme-prod ready?");
    expect(state.chat).toEqual([
      {
        id: "pending:m-1",
        kind: "pending",
        text: "is acme-prod ready?",
        correlationId: "pending:m-1",
      },
    ]);
    state = consoleTurn(state, "task-1", "is acme-prod ready?");
    expect(state.chat).toHaveLength(1);
    expect(state.chat[0]).toMatchObject({
      id: "pending:m-1",
      kind: "user",
      session: GATEWAY_SESSION,
      text: "is acme-prod ready?",
      correlationId: "corr-task-1",
      taskId: "task-1",
    });
    expect(state.pending).toEqual([]);
    expect(state.tasks.get("task-1")?.backend).toBe("console");
  });

  it("does not attach replayed history or another conversation's turn", () => {
    let state = sent(initialState, "m-1", "hi");
    state = consoleTurn(state, "task-1", "hi", { live: false });
    state = consoleTurn(state, "task-2", "hi", { conversation: "console:other" });
    expect(state.pending).toHaveLength(1);
    expect(state.chat.map((c) => c.kind)).toEqual(["pending", "user", "user"]);
  });

  it("attaches two identical texts in the order they were sent", () => {
    let state = sent(initialState, "m-1", "status?");
    state = sent(state, "m-2", "status?");
    state = consoleTurn(state, "task-1", "status?");
    state = consoleTurn(state, "task-2", "status?");
    expect(state.chat.map((c) => [c.id, c.taskId])).toEqual([
      ["pending:m-1", "task-1"],
      ["pending:m-2", "task-2"],
    ]);
    expect(state.pending).toEqual([]);
  });

  it("attaches a follow-up on a running task as a steer", () => {
    let state = consoleTurn(initialState, "task-1", "first");
    state = sent(state, "m-2", "also check staging");
    state = consoleTurn(state, "task-1", "also check staging");
    expect(state.chat[1]).toMatchObject({ id: "pending:m-2", kind: "steer", taskId: "task-1" });
  });

  it("notes a turn with no submission after 30s, and a late submission still attaches", () => {
    let state = sent(initialState, "m-1", "hello?");
    state = reduce(state, { type: "tick", now: SENT_AT + PENDING_STALE_MS - 1 });
    expect(state.chat[0].note).toBeUndefined();
    state = reduce(state, { type: "tick", now: SENT_AT + PENDING_STALE_MS + 1 });
    expect(state.chat[0].note).toMatch(/no submission on the bus 30s after sending/);
    expect(state.pending[0].stale).toBe(true);
    // A second tick does not rewrite the entry again.
    const again = reduce(state, { type: "tick", now: SENT_AT + PENDING_STALE_MS + 5_000 });
    expect(again.chat[0]).toBe(state.chat[0]);
    state = consoleTurn(again, "task-1", "hello?");
    expect(state.chat[0]).toMatchObject({ kind: "user", taskId: "task-1" });
    expect(state.chat[0].note).toBeUndefined();
  });

  it("says a send failed, and never attaches it later", () => {
    let state = sent(initialState, "m-1", "hi");
    state = reduce(state, { type: "sendFailed", messageId: "m-1", error: "Permissions Violation" });
    expect(state.chat[0]).toMatchObject({ kind: "local", text: "hi", note: "not sent: Permissions Violation" });
    expect(state.pending).toEqual([]);
    state = consoleTurn(state, "task-1", "hi");
    expect(state.chat).toHaveLength(2);
  });
});

describe("console notices and local lines", () => {
  it("renders a notice as a gateway line and replaces it on edit", () => {
    let state = reduce(initialState, {
      type: "notice",
      frame: { messageId: "c-1", text: "⏳ submitted…", edit: false },
      conversation: "console:abc",
      at: 1,
    });
    expect(state.chat[0]).toEqual({
      id: "notice:c-1",
      kind: "notice",
      session: GATEWAY_SESSION,
      text: "⏳ submitted…",
      correlationId: "console:abc",
    });
    state = reduce(state, {
      type: "notice",
      frame: { messageId: "c-1", text: "⚙️ working", edit: true },
      conversation: "console:abc",
      at: 2,
    });
    expect(state.chat).toHaveLength(1);
    expect(state.chat[0].text).toBe("⚙️ working");
  });

  it("gives local lines unique ids, and clear empties the transcript and pending turns", () => {
    let state = reduce(initialState, { type: "local", text: "one", at: 1 });
    state = reduce(state, { type: "local", text: "two", at: 2 });
    expect(state.chat.map((c) => c.id)).toEqual(["local:0", "local:1"]);
    state = sent(state, "m-1", "hi");
    state = reduce(state, { type: "clear" });
    expect(state.chat).toEqual([]);
    expect(state.pending).toEqual([]);
    state = reduce(state, { type: "local", text: "three", at: 3 });
    expect(state.chat[0].id).toBe("local:2");
  });
});

describe("poller events", () => {
  it("keeps the first failure time while a stream stays unattached", () => {
    let state = reduce(initialState, { type: "streamAttach", stream: "TASKS", error: "not found", at: 10 });
    state = reduce(state, { type: "streamAttach", stream: "TASKS", error: "timeout", at: 20 });
    expect(state.streamAttach.get("TASKS")).toEqual({ error: "timeout", since: 10 });
    state = reduce(state, { type: "streamAttach", stream: "TASKS", error: null, at: 30 });
    expect(state.streamAttach.get("TASKS")).toEqual({ error: null, since: 30 });
  });

  it("stores liveness reports by session and stream stats by name", () => {
    let state = reduce(initialState, {
      type: "liveness",
      report: {
        session: "gateway",
        durable: "gateway-relay",
        stream: "TASKS",
        perTask: false,
        found: true,
        waiting: 1,
        pending: 0,
        checkedAt: 5,
      },
    });
    expect(state.liveness.get("gateway")?.waiting).toBe(1);
    state = reduce(state, { type: "streamStat", name: "TASKS", stat: null, error: "denied", at: 6 });
    expect(state.streamStats.get("TASKS")).toEqual({ stat: null, error: "denied", at: 6 });
  });
});

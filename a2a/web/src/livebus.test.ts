/**
 * Live validation: drives the real bus layer — startBus, the
 * four ordered-consumer taps, envelope dedup, and the read-only probe — over
 * an actual websocket against an actual nats-server running the web user's
 * real grant list. Gated on A2A_WS_URL so the unit suite stays hermetic.
 *
 *   nats-server -c dev/nats.conf &
 *   node dev/seed.mjs
 *   A2A_WS_URL=ws://localhost:9222 A2A_WEB_PASS=dev-web npm test -- livebus
 *
 * Against the install: port-forward svc/platform-agent-a2a-nats 9222, set
 * A2A_WEB_PASS from the creds Secret's web-password, same command.
 *
 * The console case sends a turn as the `console` user. Locally a fake
 * gateway answers it (it needs the dev `gateway` user):
 *
 *   A2A_WS_URL=ws://localhost:9222 npm test -- livebus
 *
 * Against the install, set A2A_CONSOLE_PASS from the creds Secret's
 * console-password and A2A_FAKE_GATEWAY=0 so the real gateway answers.
 * That sends one real turn to the agent.
 */
import { describe, expect, it } from "vitest";
import { connect } from "nats.ws";
import { startBus } from "./bus.ts";
import { mintConversation, outSubject } from "./console.ts";
import { initialState, reduce, type BusEvent, type UiState } from "./model.ts";

// This file runs under node only; the tsconfig stays browser-shaped.
declare const process: { env: Record<string, string | undefined> };

const url = process.env.A2A_WS_URL;
const pass = process.env.A2A_WEB_PASS ?? "dev-web";
const seedPass = process.env.A2A_SEED_PASS ?? "dev-seed";
const canSeed = process.env.A2A_SKIP_SEED !== "1";
const consolePass = process.env.A2A_CONSOLE_PASS ?? "dev-console";
const gatewayPass = process.env.A2A_GATEWAY_PASS ?? "dev-gateway";
const fakeGateway = process.env.A2A_FAKE_GATEWAY !== "0";
/** The subject the gateway reads console turns from, every conversation. */
const CONSOLE_IN_WILDCARD = "chat.console.*.in";
const LIVE_TEXT = "live console test - reply with anything";

const suite = url ? describe : describe.skip;

function until<T>(pick: () => T | undefined, ms = 15_000): Promise<T> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const poll = () => {
      const got = pick();
      if (got !== undefined) return resolve(got);
      if (Date.now() - started > ms) return reject(new Error("timed out waiting"));
      setTimeout(poll, 100);
    };
    poll();
  });
}

suite("live bus (A2A_WS_URL set)", () => {
  it("attaches all four taps, replays history, sees live traffic once, and is refused publish", async () => {
    const events: BusEvent[] = [];
    const handle = await startBus({ url: url!, user: "web", pass }, (e) => events.push(e));
    try {
      // All four streams attach.
      await until(() =>
        events.find((e) => e.type === "streams" && e.up === 4 && e.total === 4),
      );

      // Seeded history replays as non-live envelopes.
      const history = await until(() =>
        events.find((e) => e.type === "envelope" && e.env.kind === "message" && !e.live),
      );
      expect(history).toBeDefined();

      if (canSeed) {
        // A live publish (as the seed user) arrives exactly once, live.
        const seedNc = await connect({
          servers: url!,
          user: "seed",
          pass: seedPass,
          inboxPrefix: "_INBOX.seed",
        });
        const envelopeId = `env-live-${Date.now()}`;
        const taskId = `task-live-${Date.now()}`;
        const env = {
          protocol: "a2a-jetstream/0.4",
          envelopeId,
          correlationId: "corr-livetest",
          taskId,
          contextId: "ctx-livetest",
          ts: new Date().toISOString(),
          from: { session: "gateway", agentType: "a2a-gateway" },
          to: { session: "platform" },
          identity: null,
          authority: null,
          kind: "message",
          payload: { role: "user", parts: [{ kind: "text", text: "live test ask" }] },
        };
        await seedNc
          .jetstream()
          .publish(`a2a.tasks.platform.${taskId}.in`, new TextEncoder().encode(JSON.stringify(env)));
        await seedNc.drain();

        const arrived = await until(() =>
          events.find(
            (e) => e.type === "envelope" && e.env.envelopeId === envelopeId && e.live,
          ),
        );
        expect(arrived).toBeDefined();
        // One delivery observed. This cannot force the redelivery case (a
        // tap restart mid-test); makeDedup's unit test in bus.test.ts pins
        // that a repeat would be dropped.
        expect(
          events.filter((e) => e.type === "envelope" && e.env.envelopeId === envelopeId),
        ).toHaveLength(1);
      }

      // The point of the exercise: the web user cannot publish. The server
      // refuses; the probe reports the refusal verbatim.
      const verdict = await handle.probeReadOnly();
      expect(verdict.outcome).toBe("refused");
      expect(verdict.detail.toLowerCase()).toContain("permissions violation");
    } finally {
      await handle.close();
    }
  }, 60_000);

  it("console: sends a turn, sees the notice and the submission, attaches, polls, and is refused a2a publish", async () => {
    const conversation = mintConversation();
    const events: BusEvent[] = [];
    let state: UiState = initialState;
    const dispatch = (e: BusEvent) => {
      events.push(e);
      state = reduce(state, e);
    };

    // The fake does what the gateway does on the wire for a first turn:
    // a notice on .out, then a submission on TASKS whose authority names
    // the console backend and the conversation.
    const gw = fakeGateway
      ? await connect({ servers: url!, user: "gateway", pass: gatewayPass, inboxPrefix: "_INBOX.gateway" })
      : null;
    if (gw) {
      const js = gw.jetstream();
      gw.subscribe(CONSOLE_IN_WILDCARD, {
        callback: (err, msg) => {
          if (err) return;
          const frame = JSON.parse(new TextDecoder().decode(msg.data)) as { messageId: string; text: string };
          const conv = `console:${msg.subject.split(".")[2]}`;
          const taskId = `task-console-${Date.now()}`;
          gw.publish(
            outSubject(conv),
            new TextEncoder().encode(JSON.stringify({ messageId: `n-${frame.messageId}`, text: "⏳ submitted…", edit: false })),
          );
          const env = {
            protocol: "a2a-jetstream/0.4",
            envelopeId: `env-${taskId}`,
            correlationId: `corr-${taskId}`,
            taskId,
            contextId: `ctx-${taskId}`,
            ts: new Date().toISOString(),
            from: { session: "gateway", agentType: "a2a-gateway" },
            to: { session: "platform" },
            identity: null,
            authority: {
              requester: { principal: "p", backend: "console", subject: "s", verifiedBy: "nats-grant" },
              audience: { conversation: conv, kind: "dm", roster: [], rosterComplete: true },
              grants: null,
            },
            kind: "message",
            payload: { role: "user", parts: [{ kind: "text", text: frame.text }] },
          };
          void js.publish(`a2a.tasks.platform.${taskId}.in`, new TextEncoder().encode(JSON.stringify(env)));
        },
      });
      await gw.flush();
    }

    const ghost = { session: "ghost", name: "ghost-in", stream: "TASKS" as const, perTask: true };
    const handle = await startBus({ url: url!, user: "console", pass: consolePass }, dispatch, {
      conversation,
      durables: () => [ghost],
    });
    try {
      await until(() => events.find((e) => e.type === "streams" && e.up === 4 && e.total === 4));

      handle.send(LIVE_TEXT);
      const sent = await until(() => events.find((e) => e.type === "consoleSent"));
      if (sent.type !== "consoleSent") throw new Error("unreachable");

      // The gateway's notice came back on this conversation's .out.
      const notice = await until(() => events.find((e) => e.type === "notice"), 30_000);
      expect(notice.type === "notice" && notice.conversation).toBe(conversation);

      // The pending line attached in place to the submission.
      const attached = await until(
        () => state.chat.find((c) => c.id === `pending:${sent.messageId}` && c.kind !== "pending"),
        30_000,
      );
      expect(attached.kind).toBe("user");
      expect(attached.taskId).toBeDefined();
      expect(state.tasks.get(attached.taskId!)?.backend).toBe("console");
      expect(state.pending.find((p) => p.messageId === sent.messageId)).toBeUndefined();

      // The pollers answer: a durable that doesn't exist is not-found, not an error.
      const ghostReport = await until(() => state.liveness.get("ghost"));
      expect(ghostReport.found).toBe(false);
      expect(ghostReport.error).toBeUndefined();
      const tasksStat = await until(() => state.streamStats.get("TASKS"));
      expect(tasksStat.stat).not.toBeNull();

      // The console credential writes its own inbound subject and nothing on a2a.>.
      const verdict = await handle.probeReadOnly();
      expect(verdict.outcome).toBe("refused");
    } finally {
      await handle.close();
      await gw?.drain();
    }
  }, 90_000);
});

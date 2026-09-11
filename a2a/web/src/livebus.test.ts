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
 */
import { describe, expect, it } from "vitest";
import { connect } from "nats.ws";
import { startBus } from "./bus.ts";
import type { BusEvent } from "./model.ts";

// This file runs under node only; the tsconfig stays browser-shaped.
declare const process: { env: Record<string, string | undefined> };

const url = process.env.A2A_WS_URL;
const pass = process.env.A2A_WEB_PASS ?? "dev-web";
const seedPass = process.env.A2A_SEED_PASS ?? "dev-seed";
const canSeed = process.env.A2A_SKIP_SEED !== "1";

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
});

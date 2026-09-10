/**
 * The headless rail: the live event-sequence assertion. Attaches to the
 * real bus the way the page does (startBus, same taps, same web user), waits
 * for a NEW task to run somewhere on the install, and asserts the sequence
 * the rail would draw — submission first, working before terminal, exactly
 * one final, nothing after it, and a non-empty result artifact — then folds
 * every event through the real reducer and asserts the UI model agrees.
 *
 * It does not create the task: the web user cannot publish, which is the
 * point of the web user. Drive traffic from the other side while this waits:
 * ask the agent something in Discord, or run the worker adapter's live test
 * (a2a/worker-adapter/live_test.go) over a 4222 port-forward. Both executor
 * shapes pass — a per-session worker (retires to done) and a standing
 * service answering for a profile (does not).
 *
 *   A2A_WS_URL=ws://127.0.0.1:9222 A2A_WEB_PASS=... npm test -- livesequence
 *
 * A2A_SEQ_WAIT_MS bounds each leg's wait (default 300s).
 */
import { describe, expect, it } from "vitest";
import { startBus } from "./bus.ts";
import { initialState, reduce, type BusEvent, type UiState } from "./model.ts";
import {
  ARTIFACT_RESULT,
  TERMINAL_STATES,
  type ArtifactUpdate,
  type StatusUpdate,
} from "./protocol.ts";

declare const process: { env: Record<string, string | undefined> };

const url = process.env.A2A_WS_URL;
const pass = process.env.A2A_WEB_PASS ?? "dev-web";
const waitMs = Number(process.env.A2A_SEQ_WAIT_MS ?? 300_000);
/** How long after the final event we keep listening for stragglers (assertion 10). */
const QUIET_MS = 3_000;

const suite = url ? describe : describe.skip;

type EnvelopeEvent = Extract<BusEvent, { type: "envelope" }>;

function until<T>(pick: () => T | undefined, ms: number, what: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const poll = () => {
      const got = pick();
      if (got !== undefined) return resolve(got);
      if (Date.now() - started > ms)
        return reject(new Error(`timed out waiting for ${what} after ${ms}ms`));
      setTimeout(poll, 200);
    };
    poll();
  });
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

suite("live event sequence (A2A_WS_URL set; needs a task driven on the install)", () => {
  it("observes a full task lifecycle in order and the reducer folds it to a finished task", async () => {
    const events: BusEvent[] = [];
    const seen = new Set<string>(); // every taskId sighted, history or live
    let candidate: string | undefined; // first task whose FIRST sighting is a live submission
    const handle = await startBus({ url: url!, user: "web", pass }, (e) => {
      events.push(e);
      if (e.type !== "envelope" || !e.env.taskId) return;
      if (!seen.has(e.env.taskId)) {
        if (candidate === undefined && e.live && e.env.kind === "message") {
          candidate = e.env.taskId;
        }
        seen.add(e.env.taskId);
      }
    });

    try {
      await until(
        () => events.find((e) => e.type === "streams" && e.up === 4 && e.total === 4),
        30_000,
        "all four taps",
      );

      // A brand-new task, seen from its submission. A task first sighted
      // mid-flight (live status with no prior message) is deliberately not
      // eligible: the sequence claim starts at the submission.
      const taskId = await until(() => candidate, waitMs, "a new live task (drive one: Discord ask or the worker adapter's live test)");
      const ofTask = () =>
        events.filter(
          (e): e is EnvelopeEvent => e.type === "envelope" && e.env.taskId === taskId,
        );

      const finalEvent = await until(
        () =>
          ofTask().find(
            (e) =>
              e.env.kind === "status-update" &&
              (e.env.payload as StatusUpdate).final === true,
          ),
        waitMs,
        "the task's final status",
      );
      await sleep(QUIET_MS); // stragglers would violate assertion 10; give them a window

      const seq = ofTask();

      // 1. The first thing the rail heard about this task is the submission.
      expect(seq[0]!.env.kind).toBe("message");
      expect(seq[0]!.live).toBe(true);

      // 2. Status lifecycle arrives in order: working before the terminal
      //    state, submitted (if published) before working.
      const states = seq
        .filter((e) => e.env.kind === "status-update")
        .map((e) => (e.env.payload as StatusUpdate).status?.state ?? "working");
      const iSubmitted = states.indexOf("submitted");
      const iWorking = states.indexOf("working");
      const iTerminal = states.findIndex((s) => TERMINAL_STATES.includes(s));
      expect(iWorking, `no working state in ${JSON.stringify(states)}`).toBeGreaterThanOrEqual(0);
      expect(iTerminal, `no terminal state in ${JSON.stringify(states)}`).toBeGreaterThan(iWorking);
      if (iSubmitted !== -1) expect(iSubmitted).toBeLessThan(iWorking);

      // 3. Exactly one final, it is terminal, and it is the task's last event.
      const finals = seq.filter(
        (e) => e.env.kind === "status-update" && (e.env.payload as StatusUpdate).final === true,
      );
      expect(finals).toHaveLength(1);
      expect(TERMINAL_STATES).toContain(
        (finalEvent.env.payload as StatusUpdate).status?.state,
      );
      expect(seq[seq.length - 1]!.env.envelopeId).toBe(finalEvent.env.envelopeId);

      // 4. A result artifact arrived before the final, with text.
      const resultChunks = seq.filter(
        (e) =>
          e.env.kind === "artifact-update" &&
          ((e.env.payload as ArtifactUpdate).artifact?.name ?? "") === ARTIFACT_RESULT,
      );
      expect(resultChunks.length).toBeGreaterThanOrEqual(1);

      // 5. The UI model agrees: fold every event through the real reducer.
      let state: UiState = initialState;
      for (const e of events) state = reduce(state, e);
      const task = state.tasks.get(taskId);
      expect(task).toBeDefined();
      expect(task!.final).toBe(true);
      expect(TERMINAL_STATES).toContain(task!.state);
      expect(task!.artifacts.get(ARTIFACT_RESULT)?.text ?? "").not.toBe("");
      expect(task!.executor).toBeDefined();
      // A per-session worker (executor is the subject's addressee) retires
      // its tap to done; a standing service does not — both are correct.
      const executorView = state.agents.get(task!.executor!);
      if (executorView?.perTask) expect(executorView.status).toBe("done");
    } finally {
      await handle.close();
    }
  }, 900_000);
});

// Dev seeder: provisions the four streams on the local dev bus and replays a
// plausible beat-3 exchange so the UI has something to show — one completed
// task in history, then (with --live) a second task streamed with real gaps
// so the rail's pulses and the transcript's chunk-merging can be watched.
//
// Run:  nats-server -c dev/nats.conf   (in another terminal)
//       node dev/seed.mjs --live
import { connect } from "nats.ws";

const url = process.env.A2A_WS_URL ?? "ws://localhost:9222";
const live = process.argv.includes("--live");

const enc = (v) => new TextEncoder().encode(JSON.stringify(v));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let n = 0;
function envelope({ kind, payload, taskId, contextId, from, to, correlationId }) {
  n += 1;
  return {
    protocol: "a2a-jetstream/0.4",
    envelopeId: `env-seed-${Date.now()}-${n}`,
    correlationId,
    taskId,
    contextId,
    ts: new Date().toISOString(),
    from,
    to,
    identity: null,
    authority: null,
    kind,
    payload,
  };
}

const gateway = { session: "gateway", agentType: "a2a-gateway" };
const bridge = { session: "platform-bridge", agentType: "hermes-bridge", profile: "platform" };

async function playTask(js, { taskId, corr, ask, notes, answer, gap }) {
  const inSubj = `a2a.tasks.platform.${taskId}.in`;
  const evSubj = `a2a.tasks.platform.${taskId}.events`;
  const ctx = `ctx-${taskId}`;
  const base = { taskId, contextId: ctx, correlationId: corr };

  const pub = async (subj, env) => {
    await js.publish(subj, enc(env));
    if (gap) await sleep(gap);
  };

  await pub(inSubj, envelope({
    ...base, kind: "message", from: gateway, to: { session: "platform" },
    payload: { role: "user", parts: [{ kind: "text", text: ask }], messageId: `msg-${taskId}`, taskId, contextId: ctx },
  }));
  await pub(evSubj, envelope({
    ...base, kind: "status-update", from: bridge,
    payload: { taskId, contextId: ctx, status: { state: "submitted", timestamp: new Date().toISOString() }, final: false },
  }));
  await pub(evSubj, envelope({
    ...base, kind: "status-update", from: bridge,
    payload: { taskId, contextId: ctx, status: { state: "working", timestamp: new Date().toISOString() }, final: false },
  }));
  for (const note of notes) {
    await pub(evSubj, envelope({
      ...base, kind: "artifact-update", from: bridge,
      payload: { taskId, contextId: ctx, artifact: { artifactId: `art-${taskId}-progress`, name: "progress", parts: [{ kind: "text", text: note }] } },
    }));
  }
  const chunks = answer.match(/.{1,28}/g) ?? [answer];
  for (let i = 0; i < chunks.length; i++) {
    await pub(evSubj, envelope({
      ...base, kind: "artifact-update", from: bridge,
      payload: { taskId, contextId: ctx, artifact: { artifactId: `art-${taskId}-result`, name: "result", parts: [{ kind: "text", text: chunks[i] }] }, append: i > 0, lastChunk: i === chunks.length - 1 },
    }));
  }
  await pub(evSubj, envelope({
    ...base, kind: "status-update", from: bridge,
    payload: { taskId, contextId: ctx, status: { state: "completed", timestamp: new Date().toISOString() }, final: true },
  }));
}

const nc = await connect({ servers: url, user: "seed", pass: "dev-seed", inboxPrefix: "_INBOX.seed" });
const jsm = await nc.jetstreamManager();
const js = nc.jetstream();

const streams = [
  { name: "TASKS", subjects: ["a2a.tasks.>"] },
  { name: "DIRECTORY", subjects: ["a2a.agents.>"], max_msgs_per_subject: 1 },
  // shared.probe is provisioned but has no writer in any grant — see dev/nats.conf.
  { name: "TOPICS-STATE", subjects: ["a2a.topics.agent.platform.upgrade-readiness", "a2a.topics.shared.blueprint", "a2a.topics.shared.probe"], max_msgs_per_subject: 8 },
  { name: "TOPICS-JOURNAL", subjects: ["a2a.topics.shared.annotations"] },
];
for (const s of streams) {
  try { await jsm.streams.info(s.name); } catch { await jsm.streams.add(s); }
}
console.log("streams provisioned");

// History: a card, one finished exchange, one topic write.
await js.publish("a2a.agents.platform", enc(envelope({
  kind: "agent-card", from: gateway, correlationId: "corr-directory",
  payload: { name: "platform", description: "the platform agent, via the hermes bridge" },
})));
await playTask(js, {
  taskId: "task-history-1", corr: "corr-hist-1",
  ask: "are we ready to upgrade acme-prod?",
  notes: ["reading upgrade-readiness"],
  answer: "acme-prod: 3 of 4 clusters ready; acme-prod-4 blocked on PDB budget.",
});
await js.publish("a2a.topics.agent.platform.upgrade-readiness", enc(envelope({
  kind: "topic-update", from: { session: "platform", agentType: "hermes" },
  correlationId: "corr-hist-1", taskId: "task-history-1", contextId: "ctx-task-history-1",
  payload: { artifactId: "topic-ur", name: "upgrade-readiness", parts: [{ kind: "text", text: "3 of 4 clusters ready" }] },
})));
console.log("history seeded");

if (live) {
  console.log("live phase: a fresh task every 20s, ctrl-c to stop");
  let i = 0;
  for (;;) {
    await sleep(5000);
    i += 1;
    await playTask(js, {
      taskId: `task-live-${i}`, corr: `corr-live-${i}`,
      ask: `what changed in the fleet in the last ${i * 10} minutes?`,
      notes: ["scanning events", "correlating"],
      answer: "no drift detected; two nodes recycled by the autoscaler, all workloads rescheduled clean.",
      gap: 900,
    });
    await sleep(15000);
  }
}

await nc.drain();

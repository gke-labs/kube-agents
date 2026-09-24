/**
 * The browser's connection to the bus: one websocket, one JetStream tap per
 * provisioned stream, two pollers, a wall-clock tick, and - for the console
 * user - the conversation link.
 *
 * The page connects as `console`. Its grants are the web user's JetStream
 * read surface (STREAM.INFO and CONSUMER.INFO on the four streams included -
 * `web` already holds both, so the pollers below work under either user)
 * plus publish on `chat.console.*.in` and subscribe on `chat.console.*.out`
 * (dev/nats.conf mirrors the operator render). It still cannot publish
 * anywhere on `a2a.>`, and the verify button demonstrates that live. The
 * gateway turns a console frame into a task, so every turn the page sends
 * comes back on TASKS like anyone else's.
 *
 * Mechanics the grants dictate: the inbox prefix must be `_INBOX.<user>` or
 * every JS API reply is unsubscribable; consumers are ephemeral ordered
 * *pull* consumers, because the grant enumerates
 * `$JS.API.CONSUMER.CREATE.<stream>.>` and `MSG.NEXT.<stream>.*` per stream
 * and nothing wider; and there are four streams, not one, so there are four
 * attach loops that each retry independently. A fresh install grows its
 * streams one provisioning Job at a time and the page lights up as they
 * appear.
 *
 * Connecting as `web` still works for the read-only view: no `.out`
 * subscription, no input box, and the pollers work exactly the same - only
 * the console link is unavailable.
 */
import { connect, millis, type NatsConnection, type StreamInfo, type Subscription } from "nats.ws";
import {
  STREAMS,
  parseEnvelope,
  parseSubject,
  type Envelope,
  type StreamName,
} from "./protocol.ts";
import { encodeInFrame, inSubject, mintMessageId, outSubject, parseOutFrame } from "./console.ts";
import type { BusConfig } from "./config.ts";
import type { AgentView, BusEvent, ProbeResult, StreamStat } from "./model.ts";

export { STREAMS };

const TICK_MS = 5_000;
/** How long to wait before trying to attach to a missing stream again. */
const STREAM_RETRY_MS = 3_000;
/** How long the probe waits for the server's refusal before giving up. */
const PROBE_WAIT_MS = 2_000;
/**
 * A provisioned, real subject, so a refusal proves authorization rather than
 * a typo — and one with NO writer in any user's grant, so that on the day the
 * web grant is wrong the probe's write lands nowhere instead of putting junk
 * in a state-class topic the fleet reads. Provisioned into TOPICS-STATE by
 * the operator (platformagent_a2a_manifests.go); the writerless property is
 * asserted there by `TestProbeTopicIsProvisionedAndWriterless`, not here.
 *
 * Note for installs provisioned before this subject existed: the provision
 * script is `info || add`, so their TOPICS-STATE will not list this subject
 * until someone runs `nats stream edit`. The probe still behaves correctly —
 * the refusal is
 * enforced at connect time by the grant, not by stream membership — the only
 * thing not yet true on such an install is where a broken-grant write would
 * land.
 */
const PROBE_SUBJECT = "a2a.topics.shared.probe";
/** Redelivery and tap restarts both repeat envelopes; this caps the dedup set. */
const DEDUP_MAX = 8_192;
/** How often the STREAM.INFO and CONSUMER.INFO pollers run. */
const POLL_MS = 5_000;
/** Prefix every console subject shares; a refusal under it is a send failure. */
const CONSOLE_SUBJECT_PREFIX = "chat.console.";
/** A consumer name must be one subject token, or the INFO request goes elsewhere. */
const CONSUMER_NAME_RE = /^[A-Za-z0-9_-]+$/;
/** The gateway's relay durable (a2a/gateway/gateway.go relayDurable). */
const GATEWAY_RELAY = "gateway-relay";
/** Go's zero time.Time, which NATS reports for "never". */
const ZERO_TIME_MS = Date.parse("0001-01-01T00:00:00Z");

export interface Durable {
  session: string;
  name: string;
  stream: StreamName;
  /** Exists only while the session runs a task. */
  perTask: boolean;
}

/**
 * The durables the page can find a session by. Nothing publishes heartbeats,
 * so a consumer with a pull outstanding is the only liveness signal on the
 * bus. The names come from the executors themselves: the gateway's relay,
 * the bridge's `bridge-<profile>`, and a worker's `<session>-in`
 * (lib.SessionConsumerName), which exists only while it runs a task.
 */
export function durablesFor(agents: Iterable<AgentView>): Durable[] {
  const out: Durable[] = [];
  for (const a of agents) {
    let d: Durable | null = null;
    if (a.agentType === "a2a-gateway") {
      d = { session: a.session, name: GATEWAY_RELAY, stream: "TASKS", perTask: false };
    } else if (a.agentType === "hermes-bridge" && a.profile) {
      d = { session: a.session, name: `bridge-${a.profile}`, stream: "TASKS", perTask: false };
    } else if (a.agentType === "claude-code" && a.status !== "done" && a.status !== "closed") {
      d = { session: a.session, name: `${a.session}-in`, stream: "TASKS", perTask: true };
    }
    if (d !== null && CONSUMER_NAME_RE.test(d.name)) out.push(d);
  }
  return out;
}

/**
 * `delivered.last_active` arrives as an RFC3339 string. nats.ws types it as
 * Nanos, which is wrong, hence `unknown` here. Go's zero time means never.
 */
export function parseLastActive(v: unknown): number | undefined {
  if (typeof v !== "string") return undefined;
  const ms = Date.parse(v);
  return Number.isFinite(ms) && ms > ZERO_TIME_MS && ms > 0 ? ms : undefined;
}

export function isNotFound(err: unknown): boolean {
  return (err as { api_error?: { code?: number } } | null)?.api_error?.code === 404;
}

export function streamStatOf(info: StreamInfo): StreamStat {
  const firstTs = parseLastActive(info.state.first_ts);
  return {
    bytes: info.state.bytes,
    maxBytes: info.config.max_bytes,
    msgs: info.state.messages,
    consumers: info.state.consumer_count,
    maxConsumers: info.config.max_consumers,
    ...(firstTs !== undefined ? { firstTs } : {}),
    maxAgeMs: millis(info.config.max_age),
  };
}

export interface BusOptions {
  /** The console conversation to link. Omit for the read-only view. */
  conversation?: string;
  /** Read at each poll: the durables to check, from the current state. */
  durables?: () => Durable[];
}

/**
 * One dedup set across all taps: a tap restart replays its stream from the
 * start, and the reducer must see each envelope once (assertion 5's analog).
 * Exported for the unit test — the live suite cannot force a tap restart
 * deterministically. The cap keeps a kiosk session bounded, at the cost that
 * an id evicted after `max` newer ones can re-enter; the per-stream sequence
 * watermark in startBus is what blocks replays that old.
 */
export function makeDedup(max: number): (id: string) => boolean {
  const seen = new Set<string>();
  const seenOrder: string[] = [];
  return (id: string): boolean => {
    if (seen.has(id)) return true;
    seen.add(id);
    seenOrder.push(id);
    if (seenOrder.length > max) {
      const evict = seenOrder.splice(0, seenOrder.length - max);
      for (const e of evict) seen.delete(e);
    }
    return false;
  };
}

export interface BusHandle {
  /**
   * Attempts a publish the `web` user must not be allowed to make and
   * reports what the server did. `refused` is the correct answer; `sent`
   * means the grant is broken and the UI says so just as loudly.
   *
   * Serialized: a second call while one is in flight joins the first rather
   * than starting a race. Two racing probes could otherwise resolve out of
   * order and leave the false "the grant is broken" as the last word, which
   * is the one lie this button exists to prevent.
   */
  probeReadOnly(): Promise<ProbeResult>;
  /**
   * Publishes one console turn. Dispatches `consoleSent` first, so the
   * transcript shows it at once, then `sendFailed` if the publish throws or
   * the server refuses it. The caller has already trimmed and size-checked
   * the text (commands.ts).
   */
  send(text: string): void;
  /** Switches the `.out` subscription to another conversation (/new). */
  setConversation(conversation: string): void;
  close(): Promise<void>;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export async function startBus(
  config: BusConfig,
  dispatch: (e: BusEvent) => void,
  opts: BusOptions = {},
): Promise<BusHandle> {
  // No waitOnFirstConnect: a wrong password or absent port-forward should
  // reject out to the connect form immediately, not hang the page. Once up,
  // an unlimited reconnect budget means a NATS restart mid-demo heals itself.
  const nc: NatsConnection = await connect({
    servers: config.url,
    user: config.user,
    pass: config.pass,
    name: "a2a-web",
    inboxPrefix: `_INBOX.${config.user}`,
    maxReconnectAttempts: -1,
  });
  let closed = false;
  let conversation = opts.conversation;
  let outSub: Subscription | null = null;
  /** The last turn published; a refusal on a console subject belongs to it. */
  let lastSent: string | null = null;

  // Permission violations arrive async on the status stream. The probe waits
  // on them; anything else that trips one (it shouldn't) surfaces the same way.
  // A property, not a let: the status loop below closes over it before the
  // probe ever assigns it, and TS narrows a captured let to its initial null.
  const violation: { notify: ((message: string) => void) | null } = { notify: null };

  dispatch({ type: "connection", state: "up" });
  void (async () => {
    for await (const s of nc.status()) {
      if (s.type === "disconnect") dispatch({ type: "connection", state: "down" });
      if (s.type === "reconnect") dispatch({ type: "connection", state: "up" });
      if (s.type === "error") {
        // A publish violation surfaces as data "PERMISSIONS_VIOLATION" with a
        // permissionContext naming the operation and subject. Only the probe's
        // own subject counts for the probe: nats.ws's ordered-consumer reset
        // publishes CONSUMER.DELETE, which these grants deny by design, and
        // that refusal must never be shown as the probe's evidence.
        const perm = (s as { permissionContext?: { operation: string; subject: string } })
          .permissionContext;
        if (perm?.operation === "publish" && perm.subject === PROBE_SUBJECT) {
          violation.notify?.(`Permissions Violation for publish to "${perm.subject}"`);
        }
        if (perm?.operation === "publish" && perm.subject.startsWith(CONSOLE_SUBJECT_PREFIX) && lastSent) {
          dispatch({
            type: "sendFailed",
            messageId: lastSent,
            error: `the server refused the publish to ${perm.subject} - this credential has no console grant`,
          });
          lastSent = null;
        }
        if (perm?.operation === "subscription" && perm.subject.startsWith(CONSOLE_SUBJECT_PREFIX)) {
          dispatch({
            type: "local",
            text: `the server refused the subscription to ${perm.subject}, so gateway notices for this conversation won't show here`,
            at: Date.now(),
          });
        }
      }
    }
  })().catch(() => {
    /* status iterator ends with the connection */
  });
  void nc.closed().then(() => {
    if (!closed) dispatch({ type: "connection", state: "down" });
  });

  const tick = () => dispatch({ type: "tick", now: Date.now() });
  tick();
  const timer = setInterval(tick, TICK_MS);

  const dedup = makeDedup(DEDUP_MAX);

  const js = nc.jetstream();
  const up = new Set<string>();
  const reportStreams = () =>
    dispatch({ type: "streams", up: up.size, total: STREAMS.length });
  reportStreams();

  // One stopper slot per stream, replaced on re-attach: pushing a closure per
  // attempt would grow without bound in a kiosk session left up for days.
  const stoppers = new Map<string, () => void>();
  /**
   * Highest stream sequence handed to the reducer per stream. A re-attach
   * replays the stream from the start, and the envelopeId set is capped, so
   * on a long-running view the id set alone would let evicted envelopes back
   * in — duplicating transcript lines and re-appending artifact chunks. The
   * sequence is monotonic per stream and cannot be evicted.
   */
  const delivered = new Map<string, number>();

  for (const stream of STREAMS) {
    void (async () => {
      let complained = false;
      while (!closed) {
        try {
          // Snapshot where the stream ends *before* consuming: everything at
          // or below this sequence is history replaying into the UI,
          // everything above it is happening now and earns a pulse.
          const jsm = await nc.jetstreamManager();
          const info = await jsm.streams.info(stream);
          const lastSeqAtConnect = info.state.last_seq;

          // A stream deleted and re-provisioned restarts its sequences at 1.
          // Our watermark would then swallow the whole new stream in silence,
          // so a regression resets it — the tap reports the new stream rather
          // than sitting live-but-empty.
          const mark = delivered.get(stream) ?? 0;
          if (lastSeqAtConnect < mark) {
            console.warn(`${stream} sequence regressed (${mark} → ${lastSeqAtConnect}); re-reading`);
            delivered.set(stream, 0);
          }

          // Ephemeral ordered pull consumer — the exact surface the web
          // user's grants describe. It self-heals across server restarts.
          const consumer = await js.consumers.get(stream);
          const messages = await consumer.consume();
          if (closed) {
            void messages.close();
            return;
          }
          stoppers.set(stream, () => void messages.close());
          up.add(stream);
          reportStreams();
          complained = false;
          dispatch({ type: "streamAttach", stream, error: null, at: Date.now() });
          for await (const m of messages) {
            if (m.seq <= (delivered.get(stream) ?? 0)) continue;
            delivered.set(stream, m.seq);
            let env: Envelope;
            try {
              env = parseEnvelope(m.data);
            } catch {
              continue; // not ours, or malformed — surface nothing, skip
            }
            if (dedup(env.envelopeId)) continue;
            dispatch({
              type: "envelope",
              env,
              subject: parseSubject(m.subject),
              live: m.seq > lastSeqAtConnect,
              at: Date.now(),
            });
          }
          // Iterator ended: closed() path or a consume the server tore down.
          up.delete(stream);
          reportStreams();
          if (closed) return;
          dispatch({ type: "streamAttach", stream, error: "the tap closed; re-attaching", at: Date.now() });
        } catch (error) {
          if (closed) return;
          up.delete(stream);
          reportStreams();
          dispatch({ type: "streamAttach", stream, error: String(error), at: Date.now() });
          if (!complained) {
            console.warn(`Waiting for the ${stream} stream (retrying):`, error);
            complained = true;
          }
        }
        await sleep(STREAM_RETRY_MS);
      }
    })();
  }

  // STREAM.INFO and CONSUMER.INFO, every POLL_MS. Each answer (or failure) is
  // one event, so a refused or missing lookup renders as a sentence.
  void (async () => {
    while (!closed) {
      try {
        const jsm = await nc.jetstreamManager();
        for (const stream of STREAMS) {
          const at = Date.now();
          try {
            const info = await jsm.streams.info(stream);
            dispatch({ type: "streamStat", name: stream, stat: streamStatOf(info), at });
          } catch (error) {
            dispatch({ type: "streamStat", name: stream, stat: null, error: String(error), at });
          }
        }
        for (const d of opts.durables?.() ?? []) {
          const base = { session: d.session, durable: d.name, stream: d.stream, perTask: d.perTask };
          const checkedAt = Date.now();
          try {
            const ci = await jsm.consumers.info(d.stream, d.name);
            const lastActive = parseLastActive(ci.delivered.last_active);
            dispatch({
              type: "liveness",
              report: {
                ...base,
                found: true,
                waiting: ci.num_waiting,
                pending: ci.num_pending,
                ...(lastActive !== undefined ? { lastActive } : {}),
                checkedAt,
              },
            });
          } catch (error) {
            dispatch({
              type: "liveness",
              report: {
                ...base,
                found: false,
                waiting: 0,
                pending: 0,
                ...(isNotFound(error) ? {} : { error: String(error) }),
                checkedAt,
              },
            });
          }
        }
      } catch {
        // No JetStream manager (disconnected): the next pass retries. The
        // attach loops already report the outage per stream.
      }
      await sleep(POLL_MS);
    }
  })();

  function linkConversation(): void {
    outSub?.unsubscribe();
    outSub = null;
    if (conversation === undefined) return;
    const linked = conversation;
    outSub = nc.subscribe(outSubject(linked), {
      callback: (err, m) => {
        if (err) return;
        const frame = parseOutFrame(m.data);
        if (frame !== null) dispatch({ type: "notice", frame, conversation: linked, at: Date.now() });
      },
    });
  }
  linkConversation();

  let probeInFlight: Promise<ProbeResult> | null = null;

  async function runProbe(): Promise<ProbeResult> {
    const refusal = new Promise<string>((resolve) => {
      violation.notify = resolve;
    });
    const at = Date.now();
    try {
      nc.publish(
        PROBE_SUBJECT,
        new TextEncoder().encode(JSON.stringify({ probe: "a2a-web read-only check" })),
      );
      // The flush is raced too: clicked during a reconnect it would otherwise
      // hang with the UI saying nothing at all.
      const flushed = await Promise.race([
        nc.flush().then(() => true),
        sleep(PROBE_WAIT_MS).then(() => false),
      ]);
      if (!flushed) {
        violation.notify = null;
        return {
          outcome: "error",
          detail: "the link did not flush — not connected, so the server never saw the publish",
          at,
        };
      }
    } catch (error) {
      violation.notify = null;
      return { outcome: "error", detail: String(error), at };
    }
    const verdict = await Promise.race([
      refusal.then((detail): ProbeResult => ({ outcome: "refused", detail, at })),
      sleep(PROBE_WAIT_MS).then(
        (): ProbeResult => ({
          outcome: "sent",
          detail: `no refusal within ${PROBE_WAIT_MS / 1000}s - the publish went through; the ${config.user} grant is broken`,
          at,
        }),
      ),
    ]);
    violation.notify = null;
    return verdict;
  }

  return {
    probeReadOnly(): Promise<ProbeResult> {
      // Serialized: a second click joins the probe already running rather
      // than racing it. Two in flight could resolve out of order and leave
      // the false "grant is broken" as the last word.
      if (probeInFlight) return probeInFlight;
      probeInFlight = runProbe()
        .then((verdict) => {
          dispatch({ type: "probe", result: verdict });
          return verdict;
        })
        .finally(() => {
          probeInFlight = null;
        });
      return probeInFlight;
    },
    send(text: string): void {
      if (conversation === undefined) throw new Error("send needs a conversation; this is the read-only view");
      const messageId = mintMessageId();
      dispatch({ type: "consoleSent", messageId, text, conversation, at: Date.now() });
      lastSent = messageId;
      try {
        nc.publish(inSubject(conversation), encodeInFrame({ messageId, text }));
      } catch (error) {
        lastSent = null;
        dispatch({ type: "sendFailed", messageId, error: String(error) });
      }
    },
    setConversation(next: string): void {
      conversation = next;
      linkConversation();
    },
    async close() {
      closed = true;
      clearInterval(timer);
      outSub?.unsubscribe();
      for (const stop of stoppers.values()) stop();
      await nc.close();
    },
  };
}

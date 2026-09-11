/**
 * The browser's connection to the bus: one websocket as the read-only `web`
 * user, one JetStream tap per provisioned stream, a wall-clock tick, and the
 * connection's own status. There is no publish path here beyond the probe —
 * the `web` user's grants stop at the JetStream read API, and the probe
 * exists to demonstrate exactly that, live, instead of asserting it.
 *
 * Mechanics the grants dictate (see spec-nats-deployment.md, web read
 * surface): the inbox prefix must be `_INBOX.web` or every JS API reply is
 * unsubscribable; consumers are ephemeral ordered *pull* consumers, because
 * the grant enumerates `$JS.API.CONSUMER.CREATE.<stream>.>` and
 * `MSG.NEXT.<stream>.*` per stream and nothing wider — precisely that
 * surface; and there are four streams, not one, so the
 * demo's single `a2a.>` tap becomes four attach loops that each retry
 * independently — a fresh install grows its streams one provisioning Job at
 * a time and the UI must light up as they appear, not reject.
 */
import { connect, type NatsConnection } from "nats.ws";
import { parseEnvelope, parseSubject, type Envelope } from "./protocol.ts";
import type { BusConfig } from "./config.ts";
import type { BusEvent, ProbeResult } from "./model.ts";

/** The four streams W6's provisioning Job creates; names are the contract. */
export const STREAMS = ["TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL"] as const;

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
  close(): Promise<void>;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export async function startBus(
  config: BusConfig,
  dispatch: (e: BusEvent) => void,
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
        // own subject counts: nats.ws's ordered-consumer reset publishes
        // CONSUMER.DELETE, which these grants deny by design, and that
        // refusal must never be shown as the probe's evidence.
        const perm = (s as { permissionContext?: { operation: string; subject: string } })
          .permissionContext;
        if (perm?.operation === "publish" && perm.subject === PROBE_SUBJECT) {
          violation.notify?.(`Permissions Violation for publish to "${perm.subject}"`);
        }
      }
    }
  })().catch(() => {
    /* status iterator ends with the connection */
  });
  void nc.closed().then(() => {
    if (!closed) dispatch({ type: "connection", state: "down" });
  });

  const timer = setInterval(() => dispatch({ type: "tick", now: Date.now() }), TICK_MS);

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
        } catch (error) {
          if (closed) return;
          up.delete(stream);
          reportStreams();
          if (!complained) {
            console.warn(`Waiting for the ${stream} stream (retrying):`, error);
            complained = true;
          }
        }
        await sleep(STREAM_RETRY_MS);
      }
    })();
  }

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
          detail: `no refusal within ${PROBE_WAIT_MS / 1000}s — the publish went through; the web grant is broken`,
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
    async close() {
      closed = true;
      clearInterval(timer);
      for (const stop of stoppers.values()) stop();
      await nc.close();
    },
  };
}

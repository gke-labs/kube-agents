# a2a/web — the bus, watched from a browser

The demo's web UI (`a2a-stream-demo/web`, an external playground repo),
lifted and adapted to
`a2a-jetstream/0.4`. The rail and the visual design came across intact; the
protocol underneath changed completely: addressee-scoped task subjects, the
0.4 envelope, the four reserved artifact names, four provisioned streams
instead of one, and — the point — a **read-only** connection. This page holds
the `web` NATS user's credential, whose grants stop at the JetStream read
API. It cannot publish, and the probe bar in the footer demonstrates that
live instead of asserting it.

PLAYGROUND POSTURE, stated out loud: static `web` credential from the creds
Secret, plain ws, no TLS, no ingress — the listener is ClusterIP and
`kubectl port-forward` is the only transport. That is an enforced
property, not a convention: the NATS pod's ingress policy refuses
in-cluster 9222 outright, and the port-forward still works because it
enters from the node, which NetworkPolicy does not govern. Production
terminates TLS in front of the bus or keeps the listener off
(spec-nats-deployment.md, web read surface).

## Against the install

```sh
# 1. the password
kubectl -n kubeagents-system get secret platform-agent-a2a-nats-creds \
  -o jsonpath='{.data.web-password}' | base64 -d

# 2. the transport
kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-nats 9222:9222

# 3. the page
npm install && npm run dev
# open http://localhost:5173 and paste the password into the connect form,
# or pass it in the URL: /?ws=ws://localhost:9222&user=web&pass=...
```

Ask the agent something through its chat front door; the rail lights,
events tick, and clicking a tap replays that session's events from the
stream — no live executor asked.

## Serve it from localhost:5173, not another port

The bus's websocket listener renders
`allowed_origins: ["http://localhost:5173", "http://127.0.0.1:5173"]`, so a
page served from any other origin is refused at the handshake with a 403 —
the connect form with a `WebSocket ... 403` in the console, not a hang.
Vite is pinned with `strictPort`, so it fails loudly rather than drifting to
5174 and leaving that failure to be diagnosed live.

Use `http://localhost:5173`. The `127.0.0.1` entry covers the same page
opened as `http://127.0.0.1:5173` — vite's localhost bind serves loopback
under either name, and the browser asserts whichever origin is in the
address bar. Keep `server.host` unset; it would put the dev server on
every interface.

Origin is browser-asserted and non-browser clients omit it entirely, so the
allow-list is defense in depth. The boundary is the grant list.

## Local dev (no cluster)

Node 22+ (`nats.ws` uses the global `WebSocket`, so the seeder and the live
suites fail on 20 with a bare ReferenceError; `engines` in package.json says
the same).

`dev/nats.conf` mirrors the operator's rendered config on the points that
matter: the ws listener and the `web` user's exact grant list. Mirrored from
the operator's render in
`k8s-operator/internal/controller/platformagent_a2a_manifests.go` —
re-mirror if that render moves, and re-run the live suite against it.

```sh
nats-server -c dev/nats.conf     # terminal 1
node dev/seed.mjs --live         # terminal 2: history + a task every ~20s
npm run dev                      # terminal 3, password dev-web
```

## Tests

```sh
npm test          # unit: protocol, reducer, rail geometry, components
npx tsc --noEmit  # strict, browser-shaped
# live, against a real server over real ws:
A2A_WS_URL=ws://localhost:9222 A2A_WEB_PASS=dev-web npm test -- livebus
```

The live suite drives the real bus layer — all four taps attach, seeded
history replays as non-live, a fresh publish arrives exactly once (the
redelivery half of that claim is `makeDedup`'s unit test in `bus.test.ts`;
a live tap restart cannot be forced deterministically), and the read-only
probe comes back `refused`. Against the
install, set `A2A_SKIP_SEED=1` (no seed user in hand) and the same suite
checks everything but the live-publish leg.

`livesequence` is the headless rail: it attaches the same way the page
does, waits for a NEW task to run somewhere on the install, and asserts
the event sequence the rail would draw — submission first, `working`
before terminal, exactly one `final` with nothing after it, a non-empty
`result` — then folds everything through the real reducer and asserts the
UI model agrees. It cannot create the task (the web user cannot publish;
that is the point), so drive one from the other side while it waits: ask
the agent in chat, or run the worker adapter's live test over a 4222
port-forward.

```sh
A2A_WS_URL=ws://localhost:9222 A2A_WEB_PASS=... npm test -- livesequence
```

## Shape notes, for whoever touches this next

- **The grants dictate the client.** `_INBOX.web` must be the inbox prefix
  or every JS API reply is unsubscribable; consumers are ephemeral ordered
  _pull_ consumers because the grant enumerates
  `$JS.API.CONSUMER.CREATE.<stream>.>` and `MSG.NEXT.<stream>.*` per stream
  and nothing wider. Four attach loops, one per
  stream (`TASKS`/`DIRECTORY`/`TOPICS-STATE`/`TOPICS-JOURNAL`), each
  retrying independently so a fresh install lights up as provisioning runs.
- **No heartbeats.** Nothing on the install publishes `agents.hb.>` yet and
  the `web` user couldn't subscribe it anyway (`a2a.>` only). Rail liveness
  derives from stream traffic; `active` decays to `idle` after 60s quiet.
- **Retirement is data-driven.** A session that answers as the addressee of
  its own task subject (the session worker pods) retires to `done` on
  terminal; a standing service answering for a profile under its own name
  (the bridge:
  addressee `platform`, session `platform-bridge`) does not.
- **The transcript is the reserved artifact names.** `result` chunks merge
  into one answer entry, `progress` lines the transcript and the tap's
  status, `thinking`/`activity` stay off the transcript but count in replay.

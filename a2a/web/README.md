# a2a/web - the console

A status dashboard and a chat pane over the a2a bus. The top strip and the dashboard panels show what the bus knows: sessions and their liveness, recent tasks and why they failed, conversations by backend, blackboard topics, stream capacity and protocol anomalies. The chat pane at the bottom talks to the chatops gateway through its console door, the same way a Discord or Google Chat user would, `/session` included. Only the page's own commands stay local.

The page connects to the bus's websocket listener as the `console` NATS user. That user has the read grants the old `web` view had, plus one publish (`chat.console.*.in`) and one subscribe (`chat.console.*.out`). It can't write anywhere on `a2a.>`, and the verify button in the footer shows the server refusing it. Answers never come back on the console subjects. They arrive through the TASKS stream like every other backend's.

## Posture

Port-forward only. The websocket listener is ClusterIP, the NATS pod's ingress policy refuses in-cluster 9222, and the port-forward works because it enters from the node, which NetworkPolicy doesn't govern. There is one shared `console` principal, no TLS, no ingress and no per-user identity. kubectl RBAC on the namespace is the authentication.

We expect this to move to in-cluster ingress with per-user identity, which needs the NATS account split. The frame format, the reducer and the page shouldn't change when it does.

## Against the install

```sh
# 1. the password
kubectl -n kubeagents-system get secret platform-agent-a2a-nats-creds \
  -o jsonpath='{.data.console-password}' | base64 -d

# 2. the transport
kubectl -n kubeagents-system port-forward svc/platform-agent-a2a-nats 9222:9222

# 3. the page
npm install --legacy-peer-deps && npm run dev
# open http://localhost:5173 and paste the password into the connect form,
# or pass it in the URL: /?ws=ws://localhost:9222&user=console&pass=...
```

Type into the chat pane. A turn shows as pending and attaches in place once its submission shows up on TASKS; a `delegate` turn attaches to the stripped task the gateway submits. A stop word, a bare `/session`, `/session off` and `/session stop` never become a task, so they show as sent at once and the gateway answers with a notice. A turn with no task after 30s gets a note saying so: status questions and refused turns get a gateway notice instead of a task, and if no notice came, the gateway may be slow or may have dropped it. After 10 minutes a pending turn stops waiting. `/help` lists the local commands, which are never published; any other slash line is sent.

The read-only view still works with `user=web` and the install's `web-password`. It has no input box.

## Serve it from localhost:5173, not another port

The bus's websocket listener renders `allowed_origins: ["http://localhost:5173", "http://127.0.0.1:5173"]`, so a page served from any other origin is refused at the handshake with a 403. Vite is pinned with `strictPort`, so it fails loudly rather than drifting to 5174. Keep `server.host` unset; it would put the dev server on every interface.

Origin is browser-asserted and non-browser clients omit it, so the allow-list is a second fence. The boundary is the grant list.

## Local dev (no cluster)

Node 22+ (`nats.ws` uses the global `WebSocket`). Install with `--legacy-peer-deps` until `@vitejs/plugin-react`'s peer range covers vite 8.

`dev/nats.conf` mirrors the operator's rendered config on the points that matter: the ws listener and the exact grant lists for `web` and `console` (from `webIdentity()` and `consoleIdentity()` in `k8s-operator/internal/controller/platformagent_a2a_identities.go`). Re-mirror if those lists move, and re-run the live suite against it. It also carries a dev `gateway` user, so a local gateway binary or the live suite's fake gateway can answer console turns.

```sh
nats-server -c dev/nats.conf     # terminal 1
node dev/seed.mjs --live         # terminal 2: history + a task every ~20s
npm run dev                      # terminal 3, user console, password dev-console
```

With no gateway running, a sent turn stays pending and gets the 30s note.

## Tests

```sh
npm test          # unit: protocol, reducer, derived views, commands, components
npx tsc --noEmit  # strict, browser-shaped
# live, against a real server over real ws:
A2A_WS_URL=ws://localhost:9222 npm test -- livebus
```

The live suite has two cases. The read-only one: all four taps attach, seeded history replays as non-live, a fresh publish arrives exactly once, and the `web` probe is refused. The console one: a sent turn gets a notice and a submission, the pending line attaches, a durable that doesn't exist reads as not-found, TASKS stream info comes back, and the `console` probe is refused. Locally a fake gateway answers the turn.

Against the install, set `A2A_SKIP_SEED=1` (no seed user in hand), `A2A_WEB_PASS` and `A2A_CONSOLE_PASS` from the creds Secret, and `A2A_FAKE_GATEWAY=0` so the real gateway answers. That sends one real turn to the agent.

`livesequence` watches a new task run somewhere on the install and asserts the event sequence the page renders: submission first, `working` before terminal, exactly one `final` with nothing after it, a non-empty `result`. Drive the task from chat while it waits.

```sh
A2A_WS_URL=ws://localhost:9222 A2A_WEB_PASS=... npm test -- livesequence
```

## Shape notes, for whoever touches this next

- **The grants dictate the client.** `_INBOX.<user>` must be the inbox prefix or every JS API reply is unsubscribable. Consumers are ephemeral ordered pull consumers because the grant enumerates `$JS.API.CONSUMER.CREATE.<stream>.>` and `MSG.NEXT.<stream>.*` per stream and nothing wider. Four attach loops, one per stream, each retrying on its own so a fresh install lights up as provisioning runs. A stream that won't attach says which one and the last error.
- **Liveness is CONSUMER.INFO.** Nothing publishes heartbeats. The page polls the durables it can name (`gateway-relay`, `bridge-<profile>`, `<session>-in`) every 5s. A pull outstanding or a recent delivery means a live process. A worker's `-in` consumer only exists while it runs a task, so a missing one with nothing in flight reads as idle, not gone.
- **Capacity is STREAM.INFO.** Bytes against `max_bytes` and consumers against `max_consumers`, whichever is fuller. KV sizes aren't shown: the only read that returns them also lists every key.
- **Spend is tbd on purpose.** The worker adapter drops `usage`, `total_cost_usd` and `duration_ms` from the harness result line (`a2a/worker-adapter/harness.go`), so none of it reaches the bus. The tiles and the cost column say so.
- **The trim and the cap match the gateway's.** The input trims with Go's `strings.TrimSpace` set, not JS `trim()`, and refuses over 16384 bytes before publishing, so the page never shows a turn as sent that the gateway would drop.
- **Retirement is data-driven.** A session that answers as the addressee of its own task subject (the session worker pods) retires to `done` on terminal. A standing service answering for a profile under its own name (the bridge: addressee `platform`, session `platform-bridge`) does not.
- **The transcript is the reserved artifact names.** `result` chunks merge into one answer entry, `progress` lines go in the transcript, and `thinking`/`activity` stay off it but count toward the type's activity LED.

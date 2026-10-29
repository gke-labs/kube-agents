# The agent API door: an allowlist kube-agents owns

- **Author:** @bnaylor
- **Date:** 2026-10-29
- **Status:** draft for review (#4175). Nothing here is built.
- **Companions:** the persona format and adapters
  ([`a2a/lib/persona/README.md`](../../a2a/lib/persona/README.md), #3656), which this extends
  with an inbound API descriptor; [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md)
  (AgentProfiles, #3654); the PlatformAgent API section of
  [`credential-isolation-design.md`](../credential-isolation-design.md#platformagent-api), which
  describes the door as it is today.

Line references are to `main` at `b04752be`. `CP` is
`agents/platform/scripts/credential_proxy.py` and `PM` is
`k8s-operator/internal/controller/platformagent_manifests.go`.

## 1. Problem

The agent API door is the `agent-api-auth` sidecar in the agent's gateway pod: the credential
proxy image with `CREDENTIAL_PROXY_ROLE=api-proxy` (PM:4154-4345), serving `AgentAPIProxyHandler`
(CP:2644-2893) on `:8643`. The agent's Service maps its `api` port 8642 to it (PM:6072-6088).

It does two things:

- **It authenticates one shared key.** It compares `Authorization: Bearer <key>` in constant time
  against `API_SERVER_EXTERNAL_KEY` (CP:2682-2687), which the operator fills from the
  `API_SERVER_KEY` entry of the `apiServerSecretRef` Secret (PM:4321-4338). That Secret entry is
  the real key; the `API_SERVER_KEY` the agent container itself sees is the non-secret sentinel. Every holder of that key is the same caller
  to the door.
- **It forwards everything else.** GET, POST, PUT, PATCH and DELETE on any path go to the Hermes
  API server on `127.0.0.1:8642`, re-signed with the in-pod non-secret sentinel (CP:2666-2679,
  CP:2716-2736). The one exception is a request that names an `a2a-*` session, or a child of one,
  in a header, a path segment or a top-level body key (CP:2770-2882). Those get 403.

So what a key holder can do is whatever Hermes' API server exposes. At the pinned Hermes, that
includes:

- listing, reading, renaming, flagging, ending, forking and deleting any non-`a2a-*` session;
- runs with approval, steer and stop;
- cron jobs and their CRUD;
- artifacts, browser control and room-member grants (§3.3).

That is Hermes' admin API by design. With `AgentProfile` making the harness pluggable, though,
the door's reach becomes whatever the next harness exposes. kube-agents should decide what a
caller can do through `:8643`, not inherit it from a harness.

## 2. Goals and non-goals

**Goals**

1. Deny by default at `:8643`. A request passes only if it is one of a small set of
   kube-agents-defined operations that the caller's class is allowed.
2. Know which class is calling, from its credential, not from anything the caller says about
   itself.
3. Make the operation-to-route mapping part of the harness contract, so that adding a harness can
   map kube-agents' operations onto its routes but can never add an operation.
4. Record each session's origin when it starts, so that session reads can be scoped to the
   sessions a class may see.
5. Roll out without breaking today's callers, with a test per class that it is refused everything
   outside its list.

**Non-goals**

- In-pod loopback callers on `127.0.0.1:8642` (§3.2). They share the agent pod's trust boundary,
  and anyone with `pods/exec` on the agent container already has the whole API. This doc does not
  change that; §9 lists it as an open question.
- Per-user authorization behind a class. The web console's own users, for example, are its
  concern; the door sees the console as one caller.
- The a2a bus. A2A traffic reaches the agent over NATS and the hermes-bridge, not through this
  door.

## 3. Inventory, from the code

### 3.1 Who calls `:8643`

The issue names four caller classes: the gateway, the bridge, the web console and the bench. The
code says two of those don't cross the door:

- **The a2a gateway never calls the agent API.** Its HTTP clients go to the broker's Chat relay,
  Slack and Google tokeninfo. It reaches the agent over NATS (`a2a/gateway/gchat.go`,
  `slack_sessions.go`, `a2adoor_google.go`). Its inject door on `:8099` is a server, with its own
  token (`a2a/gateway/inject.go:22-57`).
- **The hermes-bridge goes around the door.** It posts to `127.0.0.1:8642/v1/chat/completions` in
  the agent pod with the non-secret sentinel and `a2a-<contextId>` session headers
  (`a2a/hermes-bridge/api.go:56,473-482`). The door's `a2a-*` refusal exists to keep `:8643` key
  holders out of those sessions (CP:2835-2842).

The callers that do cross the door fall into three classes:

| Class       | Who                                                                                                                                           | How it reaches `:8643`                                                                                                                     |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| **console** | the web console (`a2a/cmd/web-console`)                                                                                                       | the Service, `HERMES_URL=http://<agent>.<ns>.svc.cluster.local:8642` (`charts/kube-agents/templates/web-console.yaml:94-95`)               |
| **eval**    | the bench's api transport (`bench/kube_agents_bench/harness.py`), driven by `hack/ci-eval-pr.sh` and `bench/hack/run-gitops-pilot.sh`         | a port-forward to the Service (`harness.py:181,529-532`), or an exec relay to `agent-api-auth:8643` under gVisor (`bench/README.md:60-63`) |
| **ops**     | install and CI smoke checks: `hack/ci-deploy.sh`, `hack/rollback-roundtrip.sh`, `hack/kind-up.sh` and the e2e suite (`tests/e2e/conftest.py`) | the Service, or the e2e exec relay                                                                                                         |

The bench's inject transport goes to the gateway's `:8099`, and its board, worker-session and
triage reads use `kubectl exec` and sqlite, so none of those cross the door
(`inject_transport.py:122-125`, `harness.py:54-58`).

### 3.2 Loopback callers

These use the non-secret sentinel on `127.0.0.1:8642` and never cross the door:

- the operator's readiness probe (PM:5180-5192);
- `leader_elect.py`;
- the k8s-event-watcher (`k8s-operator/cmd/k8s-event-watcher/gateway.go`);
- the Session KV server, which starts sessions and turns (`session_kv_server.py:258,2498,3682`);
- the admin console's `agent_chat.py`, which runs inside the agent container through exec and uses
  `/v1/runs`;
- the kanban notifier, the pubsub-platform adapter and a scenario script.

The marker is a constant on purpose: the API server binds loopback only (`API_SERVER_HOST=127.0.0.1`,
PM:2776-2777; the rationale is at PM:1065-1080), so anything that can reach it is already inside
the pod.

### 3.3 Routes each class uses today

| Route                                 | Method | console | eval | ops | Kind                    |
| ------------------------------------- | ------ | :-----: | :--: | :-: | ----------------------- |
| `/health`, `/health/detailed`         | GET    |    ✓    |      |     | health                  |
| `/v1/models`                          | GET    |         |      |  ✓  | health                  |
| `/api/sessions` (list)                | GET    |    ✓    |      |  ✓  | session admin: list     |
| `/api/sessions/{id}`                  | GET    |    ✓    |  ✓   |     | session admin: read     |
| `/api/sessions/{id}/messages`         | GET    |    ✓    |      |     | session admin: read     |
| `/api/sessions` (create)              | POST   |    ✓    |      |     | session create          |
| `/api/sessions/{id}/chat`, `…/stream` | POST   |    ✓    |      |     | turn                    |
| `/v1/responses`                       | POST   |         |  ✓   |  ✓  | turn (and status polls) |

Sources:

- **console:** `web-console/main.go:603,640,649-650,801,925,947,1072,1126,1145,1149`,
  `channels.go:54`, `activity.go:256,279,442,591`, `stream.go:263`, `environment.go:422`. The
  console already confines itself to its own `web-console-*` sessions plus event-triage and
  scheduled sessions (`main.go:41-46`).
- **eval:** `harness.py:775-777` (token counts from the session row) and
  `harness.py:1826,1879-1890,1977` (turns, and status polls with `conversation` = the run id).
- **ops:** `ci-deploy.sh:2600`, `rollback-roundtrip.sh:851`, `kind-up.sh:161`,
  `tests/e2e/conftest.py:475,526` (an `/api/sessions?limit=1` liveness probe) and
  `tests/e2e/test_agent_api_health.py:20`.

**Nobody uses, through the door:**

- `PATCH` or `DELETE` on a session, fork, or model lock;
- `/v1/chat/completions`, `/v1/runs`, or `/v1/responses/{id}`;
- `/api/jobs`, artifacts, browser control or room-member grants.

Of the session-admin operations, only list and read are in use.

### 3.4 The surface behind the door

At the pinned Hermes (`v2026.9.14`, `tags.env`), the route table is
`gateway/platforms/api_server.py:1523-1571`, plus the runs and room-grant modules. Every route is
mirrored under `/p/{profile}`. kube-agents' image patches add no routes. Grouped:

- health and metadata: `/health`, `/v1/health`, `/health/detailed`, `/v1/models`,
  `/api/model/options`, `/v1/capabilities`, `/v1/skills`, `/v1/toolsets`;
- sessions: list, create, get, `PATCH` (title, end reason, pinned, archived, hidden, unread),
  `DELETE`, messages, fork, model lock, chat and chat stream;
- turns: `/v1/chat/completions`, `/v1/responses`, and `GET`/`DELETE` on `/v1/responses/{id}`;
- runs: create, get, events (SSE), approval, steer, stop;
- jobs: list, create, get, `PATCH`, `DELETE`, pause, resume, run;
- other: browser control (one route is a WebSocket, which the door can't carry today), artifacts,
  room-member invitations and grants, platform events, and cron fire.

Today every one of these is open to a key holder, apart from the `a2a-*` refusal.

## 4. Design

### 4.1 Operations, not routes

kube-agents defines a closed set of **operations**: the things a caller may ask the agent for,
named independently of any harness. The first set covers today's use and nothing more:

| Operation        | What it means                                                              |
| ---------------- | -------------------------------------------------------------------------- |
| `health`         | liveness and readiness of the agent API                                    |
| `models`         | list the models the agent offers                                           |
| `turn.respond`   | run a turn with a stateless or chained request (`/v1/responses` on Hermes) |
| `session.create` | start a named session the caller will hold turns in                        |
| `session.turn`   | run a turn in a session (plain or streamed)                                |
| `session.read`   | read one session's row and its messages                                    |
| `session.list`   | list sessions                                                              |

No operation exists for rename, flags, end, fork, delete, model lock, runs, jobs, artifacts,
browser control or grants (§4.5). Adding an operation is a change to kube-agents: to this doc and
to the operation enum in code, reviewed as such.

### 4.2 The harness API descriptor

Each harness that serves an inbound API ships an **API descriptor**: a table that maps each
operation it supports onto routes. One entry per route:

- the method;
- a path template, with `{session}` as the only placeholder;
- every request field the harness reads a session id from, in the harness's own order of
  precedence: the path placeholder, named headers, named top-level body keys;
- the top-level body keys, query parameters and harness headers the route accepts, with bounds
  where they matter (a list's `limit`, for example). Anything else is refused. The lists come from
  what the callers in §3.3 actually send, not from everything the harness's route would take, and
  the header list governs only the harness's own namespace (`X-Hermes-*` on Hermes); standard HTTP
  headers such as `Authorization`, `Content-Type`, `Accept` and `User-Agent` pass. This matters as much as the
  route: on Hermes, an allowed turn or create route also takes fields that set a session's system
  prompt, its model lock, its source or its memory scope, and none of today's callers send them;
- where the harness reports the session a request ran in, if it does (Hermes sends
  `X-Hermes-Session-Id` on a `/v1/responses` answer; the bench already reads it,
  `bench/kube_agents_bench/harness.py:717`);
- whether the response streams.

A field named under `session`, `chain` or `name` is also listed in `body` or `query`: the
descriptor states every accepted field once in the lists, and the other keys only say what the door
does with some of them.

For Hermes, for example:

```yaml
apiVersion: harnessapi.kube-agents.x-k8s.io/v1alpha1
harness: hermes
operations:
  health:
    - { method: GET, path: /health }
    - { method: GET, path: /health/detailed }
  models:
    - { method: GET, path: /v1/models }
  turn.respond:
    - method: POST
      path: /v1/responses
      name: { body: conversation } # namespaced per class by the door
      reports_session: { header: X-Hermes-Session-Id }
      body: [model, input, conversation]
      stream: true
  session.create:
    - method: POST
      path: /api/sessions
      session: { body: session_id }
      body: [session_id, title]
  session.turn:
    - { method: POST, path: "/api/sessions/{session}/chat", body: [message] }
    - method: POST
      path: "/api/sessions/{session}/chat/stream"
      body: [message]
      stream: true
  session.read:
    - { method: GET, path: "/api/sessions/{session}" }
    - method: GET
      path: "/api/sessions/{session}/messages"
      query:
        { order: [latest, oldest], limit: { max: 500 }, offset: { min: 0 } }
  session.list:
    - method: GET
      path: /api/sessions
      query: { limit: { max: 200 }, source: any }
```

The descriptor's rules are what keep a harness from widening the door:

- Its operation keys must come from kube-agents' enum. An unknown key fails validation.
- Paths are literal apart from `{session}`. There are no wildcards and no prefix matches.
- The door matches the request's method and path exactly, and it forwards the same path it
  matched. Today it decodes and normalises for the `a2a-*` check (CP:2854-2866) but forwards the
  raw path (CP:2738), and the harness decodes once, so the two could see different paths. The door
  refuses any request whose raw path isn't already in canonical form, rather than trying to
  forward a rewritten one.
- Headers, body keys and query parameters outside the route's lists are refused, not stripped, so
  a caller learns at once that it sent something the door doesn't carry.
- A route the descriptor doesn't name is a 404 from the door, whatever the harness serves behind
  it.
- A harness with no descriptor gets a door that serves `health` and nothing else.

**Where it lives.** Hermes isn't a persona harness: it is configured through
`PlatformAgent.spec.harness.hermes` (`k8s-operator/api/v1alpha1/common_types.go:375-400`), and the
persona `Adapter` only renders a worker's files and arguments (`a2a/lib/persona/persona.go:151-172`).
So the descriptor is a sibling of the adapter, not an `Output` field:

- a new package, `a2a/lib/harnessapi`, with the operation enum, the descriptor type and its
  validation;
- one descriptor per harness, checked into the repo next to that harness's adapter or
  configuration;
- the operator renders the descriptor for the configured harness into the door's config, as a
  ConfigMap mounted read-only.

The descriptor is part of the harness, so a harness PR that changes routes changes the descriptor
in the same diff, and the door's tests (§7) run against it. It is never taken from the harness
image at runtime, so an image upgrade can't widen the door.

### 4.3 A credential per class

The door needs to know which class is calling, and a shared key can't tell it. Each class gets its
own key:

- The chart and the installer create one key per class (`console`, `eval`, `ops`), each in its
  own Secret. One Secret holding all three would make them only as separate as the RBAC on that
  Secret: anyone who can read it holds every class.
- The door loads them as a map from key to class. At load it refuses to start if two keys are
  equal, or if a class key equals the legacy key. It keeps the constant-time compare: it compares
  against every key and takes the match, so timing doesn't reveal which class a wrong key was
  closest to.
- The web console mounts only the console key, through a `secretKeyRef` as it mounts the shared
  key today. The bench and the CI scripts read the eval key with `kubectl get secret`, and the
  install and smoke scripts read the ops key the same way (§3.1).
- The policy is a table from class to the operations it may use, in the door's config:

| Operation        | console | eval | ops |
| ---------------- | :-----: | :--: | :-: |
| `health`         |    ✓    |  ✓   |  ✓  |
| `models`         |         |      |  ✓  |
| `turn.respond`   |         |  ✓   |  ✓  |
| `session.create` |    ✓    |      |     |
| `session.turn`   |    ✓    |      |     |
| `session.read`   |    ✓    |  ✓   |     |
| `session.list`   |    ✓    |      |  ✓  |

ops only needs `session.list` for the e2e liveness probe (`?limit=1`). Moving that probe to
`health` would drop it.

Per-class keys also make the door's audit useful. Today the door writes only the stdlib access
line and has no caller identity (CP:2889-2893). With classes, each request gets one structured
audit record: class, operation, session, decision and status.

**Why keys and not a client certificate or a token review.** The callers are a Deployment (the
console), CI jobs outside the cluster, and port-forwards from a developer's machine. A bearer key
per class works for all three. A Kubernetes token review would work for the console but not for a
port-forward from a laptop. Keys can move to projected tokens later without changing the policy
model.

### 4.4 Session origin

Scoping `session.read` and `session.list` needs to know which class a session belongs to. Today
nothing records that durably:

- Session ids carry it only by convention: `a2a-*` for the bridge, `web-console-*` for the console.
- Sessions created through `/v1/responses` get whatever id the chain or a fresh uuid produces.
- A session continued after context compression gets a new row. Nothing stamps the original
  starter onto it.

Other parts of the platform need the same fact. Memory scoping is one: whether a session is
attended or unattended decides where its writes may go, and some unattended sessions start through
the same API server as attended ones. Today that is inferred, and inference fails open.

So this design adds a **positive origin marker per session**, written by whoever starts the
session:

- **The record:** `session id → origin`. Origin is one of a small enum: `console`, `eval`, `ops`,
  `a2a`, `triage` (a session opened for a cluster event) and `scheduled` (a session a schedule
  opened). The record lives with the session registry that already
  exists in the pod, the Session KV server, and is written once. A continuation after compression
  inherits its parent's origin through `parent_session_id`, the same walk the door already does for
  `a2a-*` children (CP:2789-2825).
- **Through the door:** the door writes the origin itself, as the caller's class, and only for a
  session it saw created. The caller doesn't declare its origin; its key does.
  - On `session.create`, the caller supplies the id (the console does). Before forwarding, the
    door writes a `pending` record for that id with the caller's class. If the harness answers that
    it created the session, the door promotes the record, and only then releases the response, so
    a turn the caller sends next finds it. If the harness refuses, or answers that the session
    already existed, the door drops the `pending` record. If the door can't write the record, it
    refuses the create. A `pending` record left by a door restart is promoted on the next read if
    the session exists and has no messages from anyone else, and dropped otherwise.
  - On `turn.respond`, the door records the response ids it returned, and to which class, in the
    same registry as the origins (the Session KV server's per-session metadata), so a door restart
    loses nothing. It namespaces `conversation` names per class, so two classes can't land in one chain by
    choosing the same name. A turn whose (namespaced) conversation the class doesn't hold yet, or a
    turn with no conversation, starts a new session, which the door records from the session id the
    harness reports (`reports_session`). No caller sends `previous_response_id` today, so the
    example doesn't accept it. If one is added, the door accepts it only if that id was returned to
    the same class.
  - The door never stamps an origin onto a session that already exists. An existing session with no
    record stays unreadable, rather than becoming the first caller's.
- **Around the door:** the in-pod starters write their origin when they start a session: the
  bridge writes `a2a`, the Session KV server writes `triage` for the sessions it opens for cluster
  events, and the harness's scheduler writes `scheduled`. The event watcher itself starts no
  session; it only checks the agent API is up (§3.2). These
  are in-pod writers inside one trust boundary, so a declared origin is good enough for them.
- **No record means no read.** A session with no origin record is readable by no class through the
  door. That fails closed where today's inference fails open.
- **When the record can't be read.** The door caches origins it has found (they never change once
  written), never caches a miss, and refuses an origin-scoped request if the Session KV server
  can't answer for an uncached session.
  `health`, `models` and `turn.respond` on a new chain don't need a lookup.

The door's read rules then become:

| Class   | `session.read` may see              | `session.list` returns                 |
| ------- | ----------------------------------- | -------------------------------------- |
| console | `console`, `triage` and `scheduled` | the same origins, filtered by the door |
| eval    | `eval`                              | (not allowed)                          |
| ops     | (not allowed)                       | its own, `limit` capped                |

`session.turn` follows the same table: the console may post into its own sessions and into triage
and scheduled sessions, because it replies in the thread a cluster event or a schedule opened
(`a2a/cmd/web-console/main.go:37-40`). eval and ops don't use `session.turn`.

`session.list` is the one operation whose response the door has to filter, not just admit or
refuse: Hermes' list takes no origin filter, and today's list through the door also returns `a2a-*`
rows. The door buffers the list response up to a bound (1 MiB), drops rows whose origin the class
may not see, and returns the rest. An unparseable or oversized response is refused. Filtering
changes two things a caller sees:

- **Fewer rows than `limit`, and `has_more` is no longer exact.** The door sets `has_more` to true
  whenever it dropped a row or the harness said true, so a caller that pages keeps paging.
- **The console's recent-sessions rail shrinks.** Today it lists every kind of session by title,
  Slack and Google Chat ones included (`a2a/cmd/web-console/main.go:622-640`). Under this table it
  shows console, triage and scheduled sessions only. Whether the console should keep seeing chat
  sessions' titles is a product call (§9).

A harness whose list can filter server-side can say so in its descriptor later, and the door then
passes the filter through instead of buffering.

### 4.5 Session administration

From §3.3, the only session-admin operations anyone uses through the door are list and read. The
rest get no operation:

- **Rename, flags and end** (`PATCH`): unused. The console shows titles but doesn't set them.
- **Fork:** unused.
- **Delete:** unused. Session retention is the agent's own housekeeping, not a remote caller's.
- **Model lock:** unused. A profile's model belongs to the AgentProfile.
- **Runs, jobs, artifacts, browser control, grants:** unused through the door. The admin console's
  `/v1/runs` use is in-pod over exec (§3.2), which this design doesn't change.

If a caller later needs one, it's added as an operation, with its own row in the class table and
its own test.

## 5. How this fits the harness contract

Today the harness contract has one part: the persona adapter, which renders what a worker is told
and which tools it may use (#3656). This design adds a second part: what a harness lets callers ask
of it from outside.

- **The adapter answers** "what may the agent do?": its tools.
- **The API descriptor answers** "what may be asked of the agent?": its operations.
- **Authority stays on kube-agents' side for both.** Tiers are on the AgentProfile, never in the
  persona. The class table is in the door's config, never in the descriptor. A harness can only
  say how it serves an operation, never which callers get it.

**AgentProfile workers** (#3720 and #3842 are the first two personas) run in their own namespace
and are reached over the bus, not through `:8643` (`agentprofile_namespace.go:174`). They need no
door class today. If a profile ever serves an inbound API, it ships a descriptor and gets its own
door, with the same enum and the same rules.

## 6. Rollout

Each phase is a separate PR, and each can be reverted on its own.

The agent API is documented for users, not only for our own callers: `docs/site/src/content/docs/deploy/kustomize.md:47`
says chat integrations call it, and the operator advertises it in `status.endpoint`
(`platformagent_controller.go:4282`). So every refusing phase ships behind a CR switch that
defaults to the old behaviour for one release, with a release note, and the audit record is what
tells an operator whether anything of theirs would be refused.

1. **Observe.**
   - Add the operation enum, the Hermes descriptor, and a door that matches each request to an
     operation and logs a structured audit record (operation or `unmatched`, and the decision it
     _would_ make) without refusing anything new.
   - Run it on our lanes and ship it in a release, so installs in the field produce the same
     record.
   - Any `unmatched` line from a real caller is a gap in §3.3, so fix the inventory before going
     further.
2. **Deny the unused surface.**
   - With the shared key still in place, refuse every route that maps to no operation: `PATCH` and
     `DELETE` on sessions, fork, model lock, runs, jobs, artifacts, browser control and grants.
     Refuse request fields outside each route's lists (§4.2).
   - No caller in §3.3 uses them, so nothing of ours should break. Phase 1's records are the
     evidence.
3. **Per-class keys.**
   - The chart and installer create the three keys.
   - The web console, the bench and CI scripts, and the install and smoke scripts each switch to
     their own.
   - The shared key stays valid as class `legacy`, allowed the union of today's operations, and is
     deprecated.
   - The web console mounts the console key's Secret. Today the chart gives the web console and
     the CR's `apiServerSecretRef` the same Secret, from one value (`web-console.yaml:32,187-191`,
     `platform-agent-cr.yaml:124-126`). A CR applied outside the chart with its own
     `apiServerSecretRef` would leave the console on the chart's Secret, so the console key should
     be named in one place that both read.
4. **Record origins.**
   - The door records origins for its classes, and the in-pod starters (the bridge, the Session KV
     server and the scheduler) record theirs.
   - Backfill existing sessions once: `a2a-*` and `web-console-*` from the id convention, triage
     and scheduled sessions from their own id patterns (`a2a/cmd/web-console/activity.go:152-155`),
     and the rest from the Session KV server's records.
   - Nothing is enforced yet. The audit record notes, for each read, whether the origin rule would
     have refused it.
5. **Enforce origins,** a release after phase 4, once the audit shows no refusals of sessions a
   class should see: origin-scoped `session.read`, `session.list` and `session.turn`.
6. **Retire the shared key.** Once the audit shows no `legacy` traffic for a release, stop
   accepting it.

## 7. Tests

**Per class, at the door** (unit tests against a fake upstream, in `test_credential_proxy.py`):

- For each class, iterate every route and method in §3.4, including the `/p/{profile}` mirrors.
- Assert that exactly the routes the class's operations map to reach the upstream, and every other
  request gets the door's refusal, never the upstream's answer.
- Run the same table for a request with no key, a wrong key, and the legacy key in phase 3.
- For each allowed route, a request with an extra header, body key or query parameter, or a
  non-canonical path (double-encoded, `..`, trailing slash), is refused.

**Origin scoping:**

- console reads and posts into a `triage` session.
- eval reads its own sessions and no others.
- A session with no origin record is refused to every class.
- `session.list` drops `a2a-*` rows.
- A continuation inherits its parent's origin.
- A `session.create` the harness answers with "already exists" records nothing, and a
  `previous_response_id` returned to another class is refused.
- With the Session KV server down, an uncached origin-scoped read is refused.
- A create whose `pending` record can't be written is refused. A create interrupted after the
  harness's answer leaves a `pending` record that the next read resolves.
- A `previous_response_id` returned before a door restart is still accepted for the same class.

**The descriptor:**

- Validation refuses an unknown operation, a wildcard path, and a placeholder other than
  `{session}`.
- A conformance check holds each harness's descriptor to a golden file, so a route change shows up
  in review.

**Callers against their class:**

- A contract test lists each caller's routes, query parameters, body keys and harness headers, from
  the call sites in §3.3, and asserts every one is in a route its class allows. A caller that starts using a new route fails here before
  it fails in a cluster.
- In e2e, the console and bench happy paths run with their class keys, and one request per class
  outside its list is refused.

**Mutations:**

- Dropping a class's operation turns its happy-path test red.
- Adding an operation to the wrong class, or widening a path to a prefix, turns the refusal table
  red.

## 8. What this doesn't fix

- **In-pod callers keep the whole API.** The bridge, the Session KV server, the admin console's
  exec path and the other loopback callers (§3.2) still reach every Hermes route with the loopback
  marker. That's the pod's trust boundary, and `pods/exec` already crosses it.
- **The NetworkPolicy stays broad.** Ingress to `8642`/`8643` is from every pod in the agent's
  namespace (PM:6586-6618). The key, not the network, is what guards the door. Narrowing ingress to
  the web console's pods is cheap defence in depth (port-forwards don't go through NetworkPolicy
  anyway), and is a separate change.

## 9. Open questions

1. **Loopback scope.** Should the descriptor also govern in-pod callers, for example through a
   second door on loopback that the in-pod callers use with their own classes? That would bring the
   bridge and the Session KV server under the same table, at the cost of a hop for every turn.
2. **`/p/{profile}` routes.** The door re-signs them with the non-secret sentinel, and Hermes checks a
   profile-scoped key for a named profile. Do profile routes work through the door at all today?
   If not, the descriptor can leave them out.
3. **Runs and jobs reachability.** Today `/api/jobs` and `/v1/runs` are open to a key holder,
   and phase 2 closes them through the door. Before that, can a job or run reach an `a2a-*` session
   by a path the door doesn't check? If so, phase 2 should go first.
4. **Stream idle timeout.** The door's upstream socket timeout is 300 s (CP:2734-2736). Does a
   quiet `chat/stream` or `turn.respond` stream hit it? The descriptor's `stream` flag could carry
   a per-route idle timeout.
5. **ops and `session.list`.** Moving the e2e liveness probe to `health` would leave ops with no
   session operation at all. Is that worth doing in phase 2?
6. **A harness that doesn't report its session.** Hermes does (`X-Hermes-Session-Id`). For a
   harness that doesn't, should the descriptor refuse to offer `turn.respond`, or should the door
   assign the session itself?
7. **Chat sessions in the console's rail.** Should the console keep listing Slack and Google Chat
   sessions by title (§4.4)? If yes, `session.list` for console gains those origins, read-only.
8. **Origin enum ownership.** The origin values are shared with memory scoping. Should the enum
   live in `a2a/lib/harnessapi` or with the Session KV server's schema?

## 10. Follow-ups to file once this is reviewed

One issue per rollout phase (§6), plus:

- naming the console key's Secret in one place that the web console and the CR both read (§6, phase 3);
- NetworkPolicy ingress narrowing (§8);
- whichever open questions (§9) are decided as work.

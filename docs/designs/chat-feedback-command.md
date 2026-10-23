# `/feedback`: file a kube-agents report from chat

> **STATUS — design of record; implemented.** Written 2026-10-19 with the change that built it.
> The behaviour it designs works under `spec.mode: next`, where a chat message reaches the
> A2A gateway and the bridge runs it as a Planning Agent turn
> ([`spec-chatops-gateway.md`](spec-chatops-gateway.md)), and under `today` for a message
> that reaches the Planning Agent as text. The form it files to is
> [`scripts/feedback_form/README.md`](../../scripts/feedback_form/README.md). An earlier
> attempt at the agent half, gke-labs/kube-agents#1415, was closed unmerged; its persona text,
> filing script and tests are reused here.

## Intent

A person using a running install finds a problem with kube-agents itself and wants to
report it without leaving the chat. Today the personas tell them where reports go (the
tracker, and the public feedback form for an account that cannot open an issue), and
nothing more. A typed `/feedback` gets "Unknown command" from the Hermes gateway under
`today` and, under `next`, a model's guess at what the person meant.

After this change, under `next`:

- `/feedback` on its own tells the person where reports go and how to have one filed.
- `/feedback <text>` has the Planning Agent write the exact report, show it, and, once the
  person says yes, hand it to the platform worker, which files it through the public form.
  The filed report is a public issue on `gke-labs/kube-agents`.
- An install whose broker cannot reach the form gets the composed report back with the two
  links, so the person can file it themselves.

## Goals

- The person confirms the exact public text before anything is filed, every time.
- One report per confirmation. A retry or a lost answer never files the same text twice (the
  incident #1415 records: #1345 and #1346), and a second card for the same text within a day
  finds the first filing on the tracker. Two confirmations of one text inside the form
  pipeline's minute of latency can both reach the form: the broker's replicas share no lock,
  and the one-card-per-yes persona is what makes that rare.
- A failed filing is never reported as filed, and never leaves the person without a path.
- The filing works on a stock install, where the sandbox has no egress but the broker.
- The gateway's own answer is deterministic and needs no model, like `/session`.
- Red-to-green evidence for the agent behaviour, per
  [`eval_driven_development.md`](../../.agents/rules/eval_driven_development.md).

## Non-goals

- **The Hermes chat gateway's slash parsing under `today`.** It answers an unknown slash with
  "Unknown command", and this change adds no `legacy_slash_commands` rewrite for `/feedback`.
  Everything else here is mode-agnostic: an ask in words ("file this for me") takes the same
  path in either mode, and the bench harness's api transport reaches the Planning Agent
  without that gateway, so one case grades both lanes.
- **The session route.** A conversation on `/session` is answered by the session persona.
  The gateway's bare `/feedback` answer is the same on every route; `/feedback <text>` there
  is an ordinary turn.
- **A switch on the `PlatformAgent`.** Decided 2026-10-19: no field marks an install as unable
  to post. The worker tries, and the skill's failure path is the fallback.
- **Filing to GitHub directly.** The form needs no credential and works when the install's
  GitHub credential path is degraded. The tracker is read, unauthenticated, to find the
  filed issue; it is never written.
- **Rating an answer.** 👍/👎 on a next-mode answer is gke-labs/kube-agents#3423.

## Anti-goals

- A report filed that the person did not see word for word.
- A report carrying a cluster name, project id, log excerpt, or Secret value the person did
  not type themselves.
- A `/feedback <text>` that the gateway rewrites into other words before the agent sees it.
  The gateway holds no model; it passes the text through as typed.
- A new outbound path from the sandbox. The broker stays the sandbox's only way out.

## Constraints

- The gateway resolves a slash command before status, stop and steer, and it owns one today
  (`/session`). A second one follows the same shape: `a2a/lib/chattext`, the chat message
  validator's `IsCommand`, and the chatops page's command table all read one definition.
- Under `next` the bridge's `api` executor runs a platform-routed task as a turn under the
  Hermes `default` profile, the Planning Agent (`a2a/hermes-bridge/api.go`; the Platform Agent
  only under the experimental `platformFrontDoor`). Every task of one conversation is a turn
  in one Hermes session, so a question asked in one turn is in context when the answer
  arrives as the next (`a2a/docs/hermes-bridge.md`, "`api`: a turn in the conversation's
  session"). The Planning Agent has the kanban, router and memory tools and nothing that runs
  a script; the platform worker runs scripts, in the sandbox pod.
- The sandbox's NetworkPolicy is deny-all egress except DNS and the credential broker
  (`k8s-operator/internal/controller/shell_sandbox_manifests.go`,
  `buildShellSandboxNetworkPolicy`). On an enforcing cluster, which the stock install is,
  nothing in the sandbox reaches `gke-labs.github.io`, `docs.google.com` or
  `api.github.com`. The broker pod's egress is open, and it already serves the sandbox's
  forge verbs under `/v1/vcs/` for the `shell` caller role.
- The form's questions are matched by label (`scripts/feedback_form/Code.gs`, `QUESTIONS`),
  and the filed issue is found by exact title, which the pipeline truncates at 120
  characters. A test pins both to `Code.gs`.
- The agent material names the docs-site short link only, never a Google Forms URL
  (`tests/test_feedback_reference.py`), so a recreated form needs no agent release.
- Agent behaviour changes need a case: red against `main`, green three times on the
  branch, registered in `hack/eval/nightly-cases.txt`.

## The parts, as decided

### 1. The gateway answers a bare `/feedback`

`chattext.IsFeedbackCommand(text) (rest string, ok bool)` mirrors `IsSessionCommand`: a
leading `/` after trimming, the word `feedback` case-insensitively, then end of text or
whitespace; `rest` is the original text after the word, trimmed. On Slack the bot mention is
stripped first, as for `/session`, so the form there is `@bot /feedback`.

- `rest == ""`: the gateway posts one fixed message and does nothing else, whatever the
  conversation's route or task state. The message carries the tracker link
  (`https://github.com/gke-labs/kube-agents/issues`), the form's short link
  (`https://gke-labs.github.io/kube-agents/feedback`), that the form needs no account and
  that a submission becomes a public issue, what a report needs (a one-line summary, the
  kind, what happened), and the one-line instruction: send `/feedback <what happened>` and
  the agent writes the report up and asks before it files. The link strings are constants in
  `a2a/lib/chattext` next to the matcher, and a Go test pins them to the same two links the
  Python test pins in the personas.
- `rest != ""`: the gateway does not act. The text, `/feedback` prefix included, continues
  down the ordinary path: a steer if a task is running, else a new task. The agent sees what
  the person typed.

`IsCommand` reports `true` for the bare form only, so a chat message's button may not say
`/feedback`, and may say `/feedback the report posted twice`.

### 2. The Planning Agent composes and confirms; the platform worker files

`agents/chat/SOUL.md` §3 gains a bullet and a quick-reference row, taken from #1415 and
reworded: `/feedback <text>`, and any ask in words to file a kube-agents report, goes to
`platform`, but never straight through.

1. The Planning Agent composes the exact submission from what the person said: a one-line
   summary of at most 120 characters (the issue title); the kind, one of Bug, Feature request,
   Question or Other; what happened; optionally what was expected, and the version and
   environment. Cluster names, project ids, resource names, log excerpts and anything read
   from a Secret stay out unless the person typed them.
2. It shows the submission as it will be filed, says that it becomes a public issue on
   `gke-labs/kube-agents`, asks for a yes, and ends the turn. This is the one place the
   front door replies with more than an ack: the text is the person's own words arranged
   for the form, not knowledge, and §1.6 already requires a confirmation before anything
   destructive. The person's next message is the answer.
3. On a yes it files one card for `platform` whose body carries the confirmed text verbatim
   and the line `user-confirmed: yes`, and acks as for any card. On a change it shows the new
   text and asks again. On a no it files nothing.
4. The platform worker runs the skill with the card's text. `agents/platform/SOUL.md`'s
   "Reporting a Problem with kube-agents Itself" bullet keeps its answer for the question
   "how do I report"; its last sentence changes from "you do not submit the form by hand" to
   naming the skill as the one path that files, on a confirmed card only. A card without the
   confirmation line, or one that asks the worker to compose or improve the text, is
   completed asking for the confirmation, and files nothing.
5. The worker reports the skill's status in the card result: the issue URL on `SUBMITTED` or
   `ALREADY_SUBMITTED`; on `BLOCKED` or `UNCONFIRMED`, that nothing was filed (or that the
   filing is unconfirmed), the composed text, and the two links the status line carries.
   Under `next` the result posts into the thread through the gateway's relay; under `today`
   the front door relays it.

Under `platformFrontDoor` the Platform Agent faces the person and does both halves; the
bullets are written so that whoever faces the person composes and confirms, and whoever
files needs the confirmation.

### 3. The broker verb: `POST /v1/feedback/submit`

The wire path #1415 put in the sandbox script moves into the credential broker
(`agents/platform/scripts/credential_proxy.py`), which runs off-pod with egress. The route
is in `ROUTE_ROLES` for the `shell` caller role only, beside `/v1/vcs/`. It takes the JSON
payload below, and:

- validates it (required fields, the kind, the 120-character title cap, a 60,000-character
  cap over the three text fields so the tracker can hold the body, the token shapes in
  `credential_shapes.py`, which the broker's own log redaction reads too, and
  `user_confirmed: true`), answering `400` with a `BLOCKED` body on a refusal, so the two
  copies of the rules cannot drift: the sandbox script validates nothing the broker does not;
- reads the tracker once for a recent `external-feedback` issue with this exact title and
  the same "what happened" text (the tracker is shared by every install, so a title alone
  is not a report) and answers `ALREADY_SUBMITTED` if there is one;
- resolves the current form from the short link, maps fields by question label, POSTs once,
  and polls the tracker for the issue by exact title;
- answers one JSON object: `SUBMITTED` with `issue_url`; `ALREADY_SUBMITTED` with
  `issue_url`; `UNCONFIRMED` with `issue_url: null` and a `detail`; or `BLOCKED` with a
  `reason`. Every object carries `fallback` with the tracker and short-link URLs.

The broker reaches three hosts for this: the short link's, the form host the short link
names (pinned by the same `docs.google.com/forms/d/e/<id>/viewform` pattern as #1415), and
`api.github.com` unauthenticated. It holds no state across calls and uses no credential. The
route is counted in the broker's request metrics under the `/v1/feedback` endpoint label,
and audit-logged like the forge verbs. A full broker answers `503`, as the exec and forge
routes do, so the client reads it as nothing sent on a first attempt and as a recheck that
could not look on a rerun. The tracker is read before the POST and
after it; a tracker that cannot be read before the POST is a refusal (a blind duplicate guard
is no guard), and one that cannot be read after a confirmed POST leaves `SUBMITTED` with a
null `issue_url` and a `detail`, which the client's recheck resolves; thirty minutes after
the form confirmed, a recheck with no issue answers `UNCONFIRMED` naming the pipeline. Unit
tests beside
`test_credential_proxy.py` drive the route with a fake wire: the wire and validation cases
#1415 wrote for the sandbox script, moved to the broker, plus the `Code.gs` contract tests.

### 4. The skill: `agents/platform/skills/kube-agents-feedback/`

`SKILL.md` from #1415 with the confirmation gate, redaction rule, single run from the card
workspace and the four statuses as written there; the "filing" step calls the script, which
calls the broker. `scripts/file_feedback.py` becomes the client:

- the same payload file as #1415's: `title`, `kind`, `happened`, `expected`, `environment`,
  `user_confirmed`;
- the state marker, written in the card workspace before the broker call and finalised
  after it, so a rerun after a lost response asks the broker to recheck only: it reads the
  tracker and never posts, and answers what the tracker shows instead of filing again (the
  broker's pre-POST tracker read is the second half of that guard, for a fresh workspace);
- one `POST` to `$CREDENTIAL_PROXY_URL/v1/feedback/submit` with the Bearer token
  `credential_proxy_client.py` already builds for the forge verbs;
- the broker's JSON object printed as the script's one output line, unchanged, so the
  persona reads one shape wherever the refusal came from; on a first attempt a broker that
  cannot be reached or answers something else is `BLOCKED` with the reason and the
  `fallback` links, and on a rerun it is `UNCONFIRMED`, since the earlier request went out.

Constants at the top of the file per `.agents/rules/core_engineering.md`. Client tests in
`scripts/test_file_feedback.py` (the marker, the single call, the output shape, the
unreachable broker); the existing Makefile glob runs them.

### 5. The case

`bench/tasks/chat-feedback-composes-and-asks-first/task.yaml`, domain `chat-and-routing`,
`owner: bradhoekstra`, no fixtures, `deployer: noop`. The prompt is a `/feedback <text>`
describing a made-up kube-agents defect that names no cluster. It is graded on the Planning
Agent's reply, which both lanes carry as the answer text: `report_contains` for the composed
submission (a summary line, the kind, a "what happened" section), the public-issue warning,
and a closing question; and the safeguards that no card was filed (a `none`-wrapped
`tool_called` on `kanban_create`) and that no issue URL or claim naming a filed issue is in
the reply. The run ends on the question, so the case
grades the pause and the text, not the filing. Red on `main` because the Planning Agent
there either files a card at once and acks in one line, or answers that there is no such
command; neither reply carries the composed submission. Registered in
`hack/eval/nightly-cases.txt`; never on the blocking roster in this pull request.

The filing turn is not in the case: a case that files a public issue on every nightly run is
not acceptable. The one live run that files is the pull request's manual validation, and
its issue is closed afterwards.

### 6. Documentation and tests

- `docs/site/src/content/docs/concepts/chatops.md`: the "What carries over" row reads two
  commands; a `/feedback` section beside `/session` with the gateway's answer and the
  agent's flow, and what a `BLOCKED` filing looks like.
- `docs/site/src/content/docs/reference/security-and-iam.md` and
  `reference/credential-isolation.md`: the feedback verb joins the list of what the sandbox
  reaches through the broker, with the three hosts and "no credential".
- `docs/designs/spec-chatops-gateway.md`: a line under "Sessions by default" naming the
  second gateway-owned command and pointing here.
- `scripts/feedback_form/README.md`, "Retiring it": the skill, the broker route, the chat
  bullet and the gateway constants join the list of places the link is published.
- `tests/test_feedback_reference.py`: the platform bullet names the skill; the chat table
  has the `/feedback` row and the chat bullet carries `user-confirmed: yes`, as `SKILL.md`
  does; the broker's and the gateway's link constants equal the persona's.
- `make docs-generate` refreshes the skills catalog; `make docs-check` passes.

### 7. `/feedback summary` (added 2026-10-21, issue #3428)

The one `/feedback <text>` the gateway answers itself: `chattext.IsFeedbackSummaryCommand`
is true when the text after the word is exactly `summary`, and `IsCommand` reports it, so a
chat message's button may not carry it either. The dispatch case sits beside the bare form,
ahead of `/session`, status, stop and steer, and changes nothing about the record's task
state. `a2a/gateway/feedback_summary.go` renders the install's usage and feedback summary
deterministically, with the environment, usage and feedback sections `scripts/feedback-summary.sh`
prints from a workstation (epic #3426's share surface; the script adds the model, the performance
figures and recent task rows): environment (tag, mode, Kubernetes version, install age,
readiness, active interfaces), the `status.usage` counters, a roll-up of the gateway's own
task records, and the feedback template. That roll-up is labelled `chat turns`, not `tasks`:
the script's `tasks` line counts kanban cards, and two pastes from one install must not
disagree under one label. The roll-up splits by what a `TaskRef` holds: the
record's `ActiveTask` names the running task, `Canceled` marks a published cancel, and the
rest ended (the ordinary relay path writes no end marker; `TerminalObserved` is the heal
path's dedupe flag, not an outcome), so its buckets are `ended`, `cancelled`, `active` where
the script's kanban read tells `done` from `failed`. Its window is fourteen days clamped to
`SessionTTL`, because the reaper deletes a record, tasks and all, once idle past it, and the
label prints the window it used, never under one day. The records are read as the busy count reads them, through one
KV watch stream rather than a Get per record, under a ten-second budget inside the turn; past
that the line reads `unavailable`. It reads the
`PlatformAgent` resource through the gateway's ServiceAccount, which the operator's Role
grants one `get` on, pinned by `resourceNames` to this agent's resource; the server version
needs no grant. Tokens, spend and latency are not the gateway's to see and the block says
where they are. Before posting, the block is audited as the script audits its own: every
string leaf of the spec's naming subtrees plus the resource's name and namespace, as whole
tokens, with booleans, numbers, short strings and the template's own vocabulary skipped; a
hit posts a fixed refusal instead, and a resource that cannot be read posts a fixed
unavailability notice. No error text reaches chat. The web console classifies the turn as
settled, like the bare form. No model is in the loop, so the eval loop does not apply.

## Calls made in the build

- `/feedback summary` is answered by the gateway rather than by a platform card: a block a
  customer pastes must be a function of the sources alone, which a model-composed answer
  cannot promise, and the gateway is the one component that both sees the resource and
  posts into the conversation.
- The bare-`/feedback` branch is the first case of the gateway's dispatch switch, ahead of
  `/session`, status, stop and steer, gated on a message with no programmatic intent.
- The sandbox client keeps one marker per payload hash in the task workspace. A broker that
  could not be reached, refused the payload, or answered `503` leaves no marker (nothing was
  sent); a connection that broke after the request went out keeps it and exits 2, and the
  rerun asks the broker to recheck only.
- The web console (`a2a/web`) mirrors the bare `/feedback` as a settled turn, as it does the
  `/session` forms, so the page does not wait on a task the gateway never submits.
- The sandbox image's script allowlist (`deploy/sandbox/Dockerfile`) stages
  `feedback_broker.py` beside the client that imports it; `test_sandbox_delivery.py` is the
  gate that found the omission.

## Live validation

The pull request that introduced this document records the runs: the bare command and the
compose-confirm-file flow on an install under `spec.mode: next` with Google Chat on the A2A
gateway, the same confirmed text dispatched twice, and the case red against `main` and green
three times against the branch's build through the harness's api transport. The one real
filing's issue is closed with a comment naming that validation.

# Slack live check

A by-hand live check of the A2A gateway's Slack backend on a `spec.mode: next` install. Two Slack user accounts talk to the gateway's bot. A person types each of their turns in Slack, and the harness checks the typed message and judges the gateway's reply. Each check prints a named PASS or FAIL with its evidence. It runs as a Kubernetes Job on the target cluster, so no workstation ever holds the user tokens. It is never run in CI and is not part of the `tests/e2e/` release gate.

| File                         | Runs where             | What it does                                                                               |
| ---------------------------- | ---------------------- | ------------------------------------------------------------------------------------------ |
| `harness.py`                 | in the Job's pod       | Reads the user tokens, asks for each turn, polls for the typed turn and the bot's replies. |
| `launch.py`                  | your workstation       | Renders and applies the Job, streams its log, deletes it; runs the kubectl-side checks.    |
| `manifests/*.yaml.template`  | applied by `launch.py` | The run namespace, its ServiceAccount and NetworkPolicy, and the per-run Job.              |
| `../slack-live-check-run.sh` | your workstation       | The entry point: `exec`s `launch.py` with your arguments.                                  |

Offline tests: `tests/test_slack_live_check_harness.py` and `tests/test_slack_live_check_launch.py`, run by `make test-python`.

## Why a person types the turns

Slack attributes a message posted with a user token (`xoxp-`) through an app to that app: the message carries the app's `bot_id` and `app_id`, even though its `user` is the person. `inbound()` in `a2a/gateway/slack.go` drops any message with a `bot_id` as bot traffic, so no scripted post can ever be a turn. A live run on 2026-10-07 found this: a post made with the listed user's token through the `ka-test-sender` app came back with `bot_id` and `app_id` set, and the preflight failed on it. So the harness posts nothing. It can only verify turns a person types.

For each turn the harness prints one `TYPE` line: the time it waits, the user to type as, where to type, and the exact text, which ends with a nonce for that turn (`slc-<check>-<6 hex>`). It then polls that conversation with the user's token, which only reads, for a message containing the nonce. That message must:

- come from the user the line names;
- carry no `bot_id` and no `app_id` (a message a person types in Slack has neither; one that has them was posted through an app, and the check FAILs saying so);
- have a subtype the gateway takes as a turn (`""`, `thread_broadcast` or `file_share`) and a `user`;
- for a mention, carry the bot's mention as Slack renders it (`<@U…>`, or the older `<@U…|name>`, both of which the gateway takes as a mention), which it does only when you type `@` and pick the bot from Slack's list.

If no message carrying the nonce arrives within `--type-timeout` (default 300 s), the check FAILs. Once the typed message is in, the harness judges the gateway's replies after it, as below. The `TYPE` line goes through the same redactor as every other line.

The harness starts with a **preflight** for each user token it will use. It reads the token's identity (`auth.test`, then `users.info`) and fails if the account is a bot user, is deactivated, or is in a different workspace from the listed user. A failed preflight stops the run even under `--keep-going`. Nothing is posted: whether a message came from a person is a property of each message, so it is checked on each typed turn.

## What the checks prove

| Check           | Where it runs | Passes when                                                                                                                                                                                                                                                                                                                                                                      |
| --------------- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dm`            | Job           | The listed user types a DM to the bot and a bot reply that is not the refusal notice arrives in the DM.                                                                                                                                                                                                                                                                          |
| `mention`       | Job           | The listed user types a mention of the bot in `--channel` and the reply is threaded under the mention.                                                                                                                                                                                                                                                                           |
| `thread`        | Job           | Once the mention's task has settled, the listed user types a reply in that thread with no mention and the bot replies in the same thread with a new turn, not a steer. Needs `mention` in the same run, or a `--thread-ts` the gateway started a task in (one holding its status line); any other thread FAILs at once, since the gateway takes an unmentioned reply only there. |
| `unlisted`      | Job           | The unlisted user types a DM to the bot (or a mention of it, `--unlisted-via mention`) and gets the `⛔ I can't verify who you are on slack (id …)` notice naming their member id.                                                                                                                                                                                               |
| `restart`       | launcher, Job | After `--restart-cmd` (or `--after-restart`), the gateway rolls out and logs `slack connected`, then a second Job runs `dm` under this name, with its own `TYPE` line.                                                                                                                                                                                                           |
| `legacy-socket` | launcher      | The broker's legacy Slack relay is not armed and the gateway holds the Slack pair (below).                                                                                                                                                                                                                                                                                       |
| `home`          | Job           | A bot post appears in `--home-channel` after `--home-since` (default: the run's start) within `--home-timeout`, matching `--home-match` if given.                                                                                                                                                                                                                                |

By default a reply is the task's status line: the gateway posts it as `⏳ submitted…` before the task can produce anything and edits that one message as the task runs. `--wait-answer` waits for the answer itself. The gateway posts the agent's result as a new message and only then edits the status line to `✅ *completed*`, so once the line reads completed the answer is the last bot message after it, whatever its own first character. A status line that ends `❌ *failed*`, `🛑 *canceled*` or `🚫 *rejected*` fails the check. Bot messages ahead of the status line are the gateway's own: a listed turn answered with a steer outcome (`✏️ steering sent — …`, or `⚠️ could not send that to the running task; …`) fails either way, since it started no task, and so does a turn the gateway answers with a `not started:` notice in place of a task (`🚦 not started: … (cap N)` at the session cap, or `⚠️ not started: …`). Any other bot message there (a `⚠️` warning, a `🔎` status card) is not the reply: by default a listed turn with no status line after it fails at the timeout and quotes the last bot message. `unlisted` and `unlisted-repeat` count any bot message as a reply. `thread` waits for the mention's status line to end before it replies. When a run selects both `dm` and `restart`, the launcher runs `dm` in a Job of its own with `--wait-answer`, so its task has finished before the restart's DM reaches the same conversation; the other checks keep the reading they were asked for. Each wait for the bot is a bounded poll (`--reply-timeout`, default 180 s, every `--poll-interval`, default 5 s) that starts at the typed message.

The refusal notice is sent once per sender per gateway process (`verifySender` in `a2a/gateway/gateway.go`). A second `unlisted` run against the same gateway therefore sees silence, which fails with that explanation; restart the gateway between runs, or pass `--refusal-silence-ok`. `--unlisted-repeat` asks, with a second `TYPE` line, for a second message from the unlisted user after the notice, and passes only if nothing comes back within `--quiet-window`. With `--unlisted-via mention` the second message mentions the bot too, since an unmentioned reply in a thread the gateway never started a task in is not a turn at all.

`legacy-socket` reads, from the PlatformAgent's namespace:

1. the `envoy-credential-proxy` container of the `<agent>-credential-proxy` Deployment, which fails the check if it carries `SLACK_BOT_TOKEN` or `SLACK_APP_TOKEN`, or any `envFrom`. The operator renders that pair on the broker only for the legacy consumer (`legacySlackConsumer` in `k8s-operator/internal/controller/platformagent_a2a_manifests.go`, used in `buildCredentialProxyEnv`), and `serve()` in `agents/platform/scripts/credential_proxy.py` starts `SlackRelay` only when both are non-empty;
2. that container's log since it started, which fails the check if it contains `Slack relay enabled` or `Slack relay initialization failed`, the two lines the relay's start-up thread logs once armed;
3. the `gateway` container of `<agent>-a2a-gateway` as the positive control: it must carry the pair and have logged `slack connected` (`SlackAdapter.Run` in `a2a/gateway/slack.go`).

`--expect-principal` looks up the gateway's `ingress` log line (`startTask` in `a2a/gateway/gateway.go`) for each passing listed turn by `backendMessageId` (the message's `ts`) and compares its `principal` with the value given. `{listed}` expands to the listed user's member id, for a principal derived from it; for a mapped sender, pass the identity the principal map gives them.

## Setup

You need:

- A Slack workspace you admin, with the gateway's app installed and its bot invited to `#ka-test`. The issue that owns this run (gke-labs/kube-agents#2098) carries the app manifest.
- Two Slack user accounts a person can sign in to and type as, and a user token (`xoxp-`) for each in Secret Manager in `bnaylor-kagents-dev`: `slack-test-user-listed`, whose member id is on `spec.integration.slack.allowedUsers` and in the `a2a-slack-principal-map` Secret (the gateway admits a Slack sender only when both name them), and `slack-test-user-unlisted`, whose id is not. The harness only reads with them, so each needs just the user scopes `im:write` (to find the DM with the bot), `im:history`, `channels:history`, `groups:history`, `channels:read` and `users:read`; `chat:write` is no longer needed. `groups:read` is optional: the harness needs it only to look up a private channel by name. Without it, a name lookup searches public channels only, and a private channel needs its `C` or `G` id. The listed user must be a member of `#ka-test`.
- For the reply checks (`dm`, `mention`, `thread`, `restart`), an install whose platform agent runs the Hermes bridge executor: the `hermes-bridge` sidecar declared in the PlatformAgent's `spec.deployment.sidecars` (`a2a/docs/hermes-bridge.md`). It is what consumes the gateway's `platform` tasks. Without it a turn still gets its status line, but its task does not complete, so `--wait-answer`, `thread` (which waits for the mention's task to settle) and a `dm` run alongside `restart` cannot pass. Check before you run: `kubectl --context "$CTX" -n kubeagents-system get platformagent <agent> -o jsonpath='{.spec.deployment.sidecars[*].name}'` must list `hermes-bridge`, and `kubectl --context "$CTX" -n kubeagents-system logs deployment/<agent>-gateway -c hermes-bridge | grep 'hermes bridge consuming'` must find the line the bridge logs once it is consuming tasks. An install without it gets the sidecar from the merge patch `render_mode_next_sidecar_patch` in `hack/ci-deploy.sh` renders (its comment lists the arguments, and the `EVAL_MODE_NEXT` step after it shows the call and the `kubectl patch platformagent`). The `a2a-next-dev-3` baseline lacked the sidecar, and the live session of 2026-10-07 added it this way for its run. `unlisted` and `legacy-socket` do not need it.
- One bot, one socket. Slack spreads an app's events across every Socket Mode connection it has open, so exactly one gateway may hold the bot's app token at a time. Whoever administers the Slack app and the dev project (the cluster manager, which moves the bot between installs with its own `slack-bot.sh`, kept outside this repository) decides which install holds it; check before you run, and never point a second install at the same app.

The Google side belongs to the same administrator. The GSA `ka-slack-test-runner@bnaylor-kagents-dev.iam.gserviceaccount.com` holds `roles/secretmanager.secretAccessor` on exactly those two secrets. The one binding the run needs on top of that lets the run's Kubernetes ServiceAccount act as the GSA through Workload Identity:

```bash
gcloud iam service-accounts add-iam-policy-binding ka-slack-test-runner@bnaylor-kagents-dev.iam.gserviceaccount.com --project=bnaylor-kagents-dev --role=roles/iam.workloadIdentityUser --member='serviceAccount:bnaylor-kagents-dev.svc.id.goog[slack-test/slack-test-runner]'
```

`--render` prints that line for whatever `--project`, `--gsa`, `--namespace` and `--service-account` you pass. The launcher never runs gcloud.

Workload Identity names the pod by namespace and ServiceAccount across every cluster in the project's workload pool, not by cluster. Anyone who can create `slack-test/slack-test-runner` and a pod in it on any cluster in `bnaylor-kagents-dev` can therefore read both user tokens, which carry the users' message history scopes. Keep namespace creation on the project's clusters to its administrators, or give the GSA a project of its own.

The cluster side is the launcher's. Before its first Job it applies `manifests/setup.yaml.template`, idempotently: the `slack-test` namespace, the `slack-test-runner` ServiceAccount annotated `iam.gke.io/gcp-service-account: ka-slack-test-runner@bnaylor-kagents-dev.iam.gserviceaccount.com`, and a NetworkPolicy. It deletes the namespace when the run ends, but not while another run's Job is still running in it: it leaves the namespace to that run. `--cleanup` deletes it on its own after an interrupted run. Running it says no run is live, so it first deletes this tool's Jobs still running there (an interrupted run's, whose launcher no longer watches them) and names them, then the namespace. Both refuse a namespace that exists without the `app.kubernetes.io/name=slack-live-check` label, since the setup fences every pod in it and the cleanup deletes it. `--namespace`, `--service-account` and `--gsa` override the three names.

The pod reads the two secrets over the Secret Manager REST API with the token the GKE metadata server issues under Workload Identity. That needs only Workload Identity on the cluster; the Secret Manager CSI add-on would add a cluster add-on and a `SecretProviderClass` for the same result. A secret whose value has whitespace or a control character inside it is refused by name before it is used.

The Job reaches the gateway only through Slack, never in-cluster. The NetworkPolicy admits nothing and lets the pod reach only DNS, the metadata server, and TCP 443 outside the private ranges, which is where `slack.com` and the Google APIs are. NetworkPolicy cannot name a host, so that last rule is as narrow as it gets without an FQDN policy.

## Running it

Take the shared cluster's lease first if you have one: the launcher creates and deletes the `slack-test` namespace and, with `--restart-cmd`, runs your restart. Never run two launchers (or a run and a `--cleanup`) against the same cluster and namespace at once; one run's end-of-run delete can take the namespace out from under the other. Sign in to Slack as both test users (two browsers, or a browser and the app), with the bot's DM and `#ka-test` to hand. Then, in this order:

```bash
CTX=<kube-context>
BOT=<your bot's name>  # as Slack shows it in this workspace; or pass --bot-user-id <its member id> instead
# 1. No Slack traffic: the broker holds no socket, the gateway does.
hack/slack-live-check-run.sh --context "$CTX" --checks legacy-socket
# 2. The turns. dm, mention and thread as the listed user, then the refusal as the unlisted one.
hack/slack-live-check-run.sh --context "$CTX" --checks dm,mention,thread,unlisted -- --bot-name "$BOT" --wait-answer --unlisted-repeat
# 3. Restart the gateway and DM again. The command runs without a shell; pin its context yourself.
hack/slack-live-check-run.sh --context "$CTX" --checks restart \
  --restart-cmd "kubectl --context $CTX -n kubeagents-system rollout restart deployment/<agent>-a2a-gateway" \
  -- --bot-name "$BOT"
```

The launcher streams the pod's log to your terminal while the Job runs, reading it again every 5 s, so each `TYPE` line shows up within a few seconds of the harness printing it. When one appears, switch to the user it names, go where it says, and type (or paste) its text after the colon exactly as printed, nonce included. For a mention, type `@` and pick the bot from Slack's list rather than pasting `@<name>` as plain text. The harness waits up to `--type-timeout` for the message, then for the bot. A session looks like this (ids, names and timestamps are illustrative; the `EVIDENCE` lines are cut):

```text
SETUP namespace slack-test, ServiceAccount slack-test-runner -> ka-slack-test-runner@bnaylor-kagents-dev.iam.gserviceaccount.com
JOB slack-live-check-20261008150000-1a2b in slack-test: checks=dm,mention,thread image=…/agent-sandbox:…
JOB slack-live-check-20261008150000-1a2b: its log follows as the harness prints it. Type each TYPE line's text in Slack, as the user it names and where it says, before its wait runs out
RUN 20261008150000-1a2b: team=T0TEAM001 listed=U0LISTED1 unlisted=- bot=U0BOTKAGE channel=C0KATEST1 checks=dm,mention,thread
PASS preflight-listed: U0LISTED1 (@lisa) is a person's account in team T0TEAM001
TYPE within 300s as @lisa (U0LISTED1) in your DM with @kage (D0DMKAGE1): Reply with the single word PONG. slc-dm-3f9a1c
PASS dm: answer in D0DMKAGE1 at ts=1791500012.000300 (sent ts=1791500009.000200)
TYPE within 300s as @lisa (U0LISTED1) in #ka-test (C0KATEST1), top level, picking @kage from Slack's list: @kage Reply with the single word PONG. slc-mention-7b20e4
PASS mention: answer in thread 1791500031.000400 at ts=1791500036.000600 (sent ts=1791500031.000400)
TYPE within 300s as @lisa (U0LISTED1) in the thread under your mention in #ka-test (C0KATEST1), thread ts=1791500031.000400, without mentioning the bot: Once more, please: reply with the single word PONG. slc-thread-c51d90
PASS thread: answer in thread 1791500031.000400 at ts=1791500058.000900 (sent ts=1791500052.000700)
SUMMARY pass=4 fail=0 passed=preflight-listed,dm,mention,thread
CLEANUP namespace slack-test deleted
OVERALL pass=4 fail=0
```

A typed message that carries a `bot_id` or `app_id`, comes from another user, or misses its mention FAILs that check at once and names why. One that never arrives FAILs at `--type-timeout`.

The run stops at the first FAIL unless `--keep-going` is given, and ends with an `OVERALL` line. Inside a wait, a transient read error (Slack's 429 or 5xx, an unreachable host, a timed-out read) is read again until the wait runs out; if the last read failed, the FAIL names its error. Any other Slack or network error inside a check is that check's FAIL, not the end of the run, and so is a kubectl error inside one of the launcher's own checks (`legacy-socket`, `restart`, `--expect-principal`). Every check the run asked for needs a PASS or FAIL line, so one the Job's log does not answer (the log could not be read, or the harness stopped first) is a FAIL. The exit status is 0 only when every check passed and the namespace was deleted, or left to another run's live Job as Setup describes; a failed delete exits 2. Flags after `--` go to `harness.py` (`python3 hack/slack-live-check/harness.py --help` lists them, among them `--channel`, `--bot-user-id`, `--bot-name`, `--prompt`, `--type-timeout`, `--reply-timeout`, `--listed-secret` and `--unlisted-secret`). Any run with a check that runs in the Job needs `--bot-name` or `--bot-user-id`. There is no default bot name, because each workspace names its bot, and both the harness and the launcher refuse the run before it starts without one. `--channel` and `--home-channel` take a channel name or a `C`/`G` id; a `D` (DM) id is refused, since in a DM every message is a turn and `mention` and `thread` could not be told from `dm`. The launcher forwards only the flags it lists in `FORWARDABLE_HARNESS_FLAGS`. It refuses `--checks`, `--run-id`, `--project` and `--keep-going`, which are its own, and the harness's hidden endpoint overrides, which would send the tokens to another host. `--render` prints the manifests, touches nothing, and needs no bot name.

The Job runs in the install's own `agent-sandbox` image, read from the `<agent>-shell` StatefulSet (`--image` overrides it): it already has a `python3` and holds no credentials, so no new image enters the inventory. The harness is shipped in a ConfigMap. The pod runs as a non-root user with a read-only root filesystem, no capabilities and no mounted Kubernetes token, and has `backoffLimit: 0` so a failure never asks for the turns again. Its `activeDeadlineSeconds` is the sum of the selected checks' waits (`--type-timeout` for each typed turn, plus the reply waits) plus a setup allowance; `--job-timeout` sets a floor. The launcher deletes the Job, the ConfigMap and then the namespace when the run ends. If it dies first, `ttlSecondsAfterFinished` collects the Job once it ends, and `hack/slack-live-check-run.sh --context "$CTX" --cleanup` removes the namespace and everything in it, a Job still running included.

The harness keeps the tokens in memory only. They are never in the Job spec, the environment or a file, and every line it or the launcher prints goes through a redactor that cuts both the values it has read and anything shaped like a Slack or Google access token.

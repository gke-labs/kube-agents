---
title: Upgrade
description: Moving an existing install to a newer release with upgrade.sh — its modes, how the target version is resolved, and what a run refuses before any of the new release is applied.
---

`upgrade.sh` is the Day-2 engine for an install `install.sh` created. It re-applies the same
Terraform composition and the same Helm chart at a newer revision, re-rendering the install from
the configuration it already has. A release copy of the script carries the version it upgrades to,
so the ordinary upgrade names no version at all.

## Before you start

Record what the install runs now, so you can tell afterwards what moved:

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.spec.mode}{"\n"}'
helm history kube-agents -n kubeagents-system
```

The third command prints the install's `spec.mode`; an empty line means `today`.

Read the release notes for every release between the one you run and the one you are moving to.

The upgrade refuses to run without the install's own configuration, because a full upgrade
re-renders the `PlatformAgent` resource from it: a file written from memory re-renders the install
with whatever it forgets. `KUBE_AGENTS_INSTALL_ENV` names the file outright, which is how an
ephemeral CI runner supplies one. Otherwise the script looks for `install.env` in the checkout it
is running from, then in the directory you run it from, and — when run from outside a checkout,
such as the piped release one-liner — last in the install checkout the installer left in
`$HOME/kube-agents`, so standing in one install's directory upgrades that install, not whichever
one the checkout in `$HOME` belongs to.

`--upgrade-mode=full`, the default, additionally needs the `terraform` CLI on `PATH`.

## Run the upgrade

Substitute `<RELEASE_VERSION>` with the release tag you are moving to, from
[GitHub Releases](https://github.com/gke-labs/kube-agents/releases):

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/upgrade.sh | bash -s -- \
  --non-interactive \
  --gcp-project-id="my-gcp-project" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1"
```

The release-pinned script upgrades to its own version, so you pass no tag. It reuses the install
checkout in `$HOME/kube-agents`, moving it to the release you asked for, and reads the `install.env`
in it. A checkout with uncommitted changes is left alone and the run stops rather than upgrading
from sources that do not match the release. Only a release copy is flagless: a copy built from
`main` carries no version and asks for one, so name a release tag in the URL rather than `main`.

The release bundle is the other supported source, and the one to use on a machine with no install
checkout:

```bash
curl -fsSL https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz | tar -xz
cd kube-agents-<RELEASE_VERSION>
cp /path/to/your/install/install.env .
./upgrade.sh --non-interactive --gcp-project-id="my-gcp-project"
```

## Upgrade modes

- `--upgrade-mode=harness` re-tags the Platform Agent image, the sandbox image it reaches over
  ssh, and every plugin image the release records — all are built from the same revision —
  through one `helm upgrade` that re-applies the values the release recorded over the chart's
  defaults. Before any of the new release is applied it names and stops on any recorded key the
  chart's values schema refuses as undeclared, since Helm would refuse the whole upgrade over it. On an upgrade
  the usual cause is a setting the new chart renamed or removed: take that upgrade through
  `--upgrade-mode=full`. On a rollback to a release that predates the key, pass
  `--drop-undeclared-values` to drop and name each one instead; a later release that declares the
  key renders it from that chart's default until a full-mode run there renders `install.env` onto
  the chart again.
- `--upgrade-mode=operator` applies the chart's CRDs with `kubectl` first — Helm never touches
  `crds/` on an upgrade — then re-tags the operator image the same way.
- `--upgrade-mode=full`, the default, applies the CRDs and then runs a full `terraform apply`
  through the install engine: every image tag moves, and every setting in `install.env` is
  re-rendered. It is the only mode that applies `PLATFORM_AGENT_MODE`, so it is the one that
  [switches `spec.mode`](#switching-specmode).

The operator moves before the harness because the operator owns the resources the harness runs as.
Every mode needs the `kube-agents` Helm release to exist in the target namespace; an install
without one predates the Terraform and Helm engine, and has to be re-installed to adopt it.

## Choosing what the run targets

A release copy of the script carries the version it upgrades to, which is what makes the one-liner
above flagless. That baked version is the run's target from the moment it starts, before any flag
is read, so two of the flags below behave differently depending on which copy you are holding. One
copy is the exception: `upgrade.sh` in a checkout of a release line (`release/<X.Y>`) that has moved
past its latest release still carries that release's version but is not that release, so run from
there, with the release's tag and full history fetched, it drops the baked default, says which line
and commit it is on, and asks for `--image-tag` the way a copy with no baked version does; a clone that
lacks the tag, or whose shallow history stops short of the release, is refused and told which fetch
to run.

- `--image-tag` names a revision to move to instead: a release tag or a full commit SHA. It
  overrides the baked version, and it exists for development and CI/CD testing — a candidate
  commit, or a release the script does not itself carry. It is not part of upgrading to a published
  release. Mutable refs such as `latest`, `main` and `HEAD` are rejected, so the scripts and the
  container images always name the same revision.
- `--keep-image-tag` upgrades everything except the images, leaving them on the tag the install
  already serves. It refuses `--image-tag`, because the two ask for opposite things — and since a
  release copy already carries a version, it refuses this flag as well. Run it from a checkout,
  which is where the scheduled reconciles that need it run.
- `--plan` changes nothing and reports what an upgrade would do. From a release copy it plans at
  that release: the question it answers is whether moving to that version would change anything.
  From a copy with no baked version and no `--image-tag`, it plans at the tag the install's
  Terraform state records, so the report is composition drift rather than image lag. Either way it
  exits 0 when the install is in sync, 2 when there are changes, and 1 when the plan itself failed.

Given no tag and no flag, a copy of the script built from `main` has no version to default to, and
asks for one.

## Previewing

`--dry-run` and `--plan` are both previews and are deliberately not the same one. `--dry-run`
answers offline, from configuration alone, and never contacts the install. `--plan` answers from
the install's real Terraform state, so it needs credentials, and it is the only one of the two that
can report drift. The two are refused together.

Neither moves the install checkout in `$HOME/kube-agents` or edits `install.env`: a preview that
needs sources the checkout does not have reads them from a temporary copy instead, so the checkout
is still on the release the install runs when the preview is over. When a run is configured from
`$(pwd)/install.env` or `KUBE_AGENTS_INSTALL_ENV` outside `$HOME/kube-agents`, it fetches a
temporary copy as well rather than touching `$HOME/kube-agents`. When `--plan` reuses the
install's own checkout because it is already on the target release (or runs from inside it),
`--plan` refreshes the generated `terraform/examples/full-install/terraform.tfvars` and
`.terraform/` working directory there so Terraform can plan against the live backend; `--dry-run`
exits before Terraform runs and writes nothing.

Neither is refused by an edited checkout either. A preview of what an uncommitted change would
apply is the one report that answers "what have I edited here", so both previews warn and continue
where a real upgrade stops. When the configuration the run loaded records a different install than
the flags name, only `--dry-run` warns and continues — `--plan` refuses alongside a real upgrade
because planning writes `terraform.tfvars` and reconfigures `.terraform/` in the working sources
(see [Naming the install](#naming-the-install)).

What a preview will not do is report on a run that could not happen: with no `install.env` anywhere
it stops with the same refusal a real upgrade gives, rather than printing a plan you could not
execute.

## Naming the install

The lookup order above is what keeps the configuration and the target install in step, because
standing in an install's directory upgrades that install. The piped one-liner has nowhere to stand,
so on a machine with two installs it loads `$HOME/kube-agents/install.env` whichever cluster the
flags name — and a full upgrade re-renders the `PlatformAgent` resource from that file, so the
mismatch writes one install's chat space, allowed users, model provider and namespace into the
other.

So the run compares them. When `--gcp-project-id`, `--gke-cluster-name` or `--gcp-region` names
something the loaded `install.env` records differently, both a real upgrade and `--plan` refuse,
while `--dry-run` reports the mismatch and goes on. Point `KUBE_AGENTS_INSTALL_ENV` at the
`install.env` of the install you are upgrading, run from its checkout, or drop the flag that
disagrees.

## Checking the result

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}'
kubectl get pods -n kubeagents-system
helm history kube-agents -n kubeagents-system
```

Both images end in `:<RELEASE_VERSION>`, the `Ready` condition reads `True`, the gateway pod is
`Running`, and the newest Helm revision is `deployed`. `kubeagents-system` is the default
namespace; an install that set `NAMESPACE` in `install.env` uses that one. `platform-agent` is the
chart's default `platformAgent.name` value.

The run also writes a machine-readable report to `/tmp/kube-agents-upgrade-report.json`.

### Slack

On an install with Slack enabled, check that Slack still answers. Which process holds the Slack
connection depends on `spec.mode` (the third command under [Before you start](#before-you-start)):

- **Empty or `today`**, or `next` with Google Chat also enabled: the credential broker.

  ```bash
  kubectl logs deploy/platform-agent-credential-proxy -n kubeagents-system \
    | grep -E 'Slack relay enabled|Slack relay initialization failed|Slack bot token authentication failed'
  ```

  The pattern matches exactly three lines. `Slack relay enabled workspaces=1` as the last line is
  the healthy reading: the number counts the bot tokens Slack accepted, one per workspace.
  `Slack bot token authentication failed` means Slack refused one of the bot tokens; it gives the
  error type and Slack's error, not which token. `Slack relay initialization failed; retrying`
  means no bot token was accepted or the Socket Mode connection failed, and the broker tries again
  every 30 seconds. No output has no reading of its own (on a broker that has run for a while the
  startup lines may have rotated out of the log): go by the DM test below.

- **`next`**: the A2A gateway.

  ```bash
  kubectl logs deploy/platform-agent-a2a-gateway -c gateway -n kubeagents-system \
    | grep -E 'slack connected|chat backend stopped'
  ```

  The last line contains `"msg":"slack connected"` and names the bot user and the workspace
  (`team`). The gateway logs it once it has joined the bus and Slack has accepted the bot token,
  and then opens the Socket Mode connection. A `chat backend stopped` line after it means that
  connection failed. One with no `slack connected` before it means the gateway's first call to
  Slack failed: its `err` reads `invalid_auth` when Slack refused the bot token, and anything else
  (a DNS error or a timeout, say) is the gateway not reaching Slack. The gateway retries either
  way. No output means the gateway has not tried Slack yet: it first joins the bus, retrying for up
  to 45 seconds, and exits if that fails. Wait a minute and run the command again. If the pod's
  restart count is climbing, `kubectl logs ... --previous` shows why the last run exited.

Either way, DM the app a question such as `what clusters can you see?` and wait for the answer (an
AI agent asks a person to do this): the log line does not prove that messages reach the agent. If
nothing comes back, [Slack app setup → Verify](/kube-agents/install/slack-app/#verify) has the next
checks.

## Switching `spec.mode`

`PLATFORM_AGENT_MODE` in `install.env` sets the install's `spec.mode`: `today`, or `next`, an
unsupported development stack. A file with no such line means `today`. Of `upgrade.sh`'s modes only
full applies the key; a re-run of `install.sh` applies it too. Only the `install.sh` and
`upgrade.sh` of releases after 0.9.0 read it, so upgrade to such a release first.
[`scripts/installer/README.md`](https://github.com/gke-labs/kube-agents/blob/main/scripts/installer/README.md)
has the rules for the key. This section covers what a switch does to Slack, in order.

With Google Chat also enabled, Google Chat takes the A2A gateway and Slack stays on the credential
broker under `next`, as under `today`. Then none of the Slack steps below apply: switch, check Slack
as in [Checking the result](#slack), and leave the Slack app as it is.

### Before the switch

1. Check that Slack answers now, as in [Checking the result](#slack).
2. Check the Slack settings `next` depends on:
   - The bot token is one `xoxb-` token, not a comma-separated list: the A2A gateway takes a
     single workspace's token
     ([Get the tokens](/kube-agents/install/slack-app/#get-the-tokens)). This counts the
     `xoxb-` tokens in the installer's Secret without printing them, and should print `1`:

     ```bash
     kubectl get secret platform-agent-secrets -n kubeagents-system \
       -o jsonpath='{.data.SLACK_BOT_TOKEN}' | base64 -d | tr ',' '\n' | grep -c '^xoxb-'
     ```

     `0` means nothing was read: check that the `PlatformAgent`'s
     `spec.integration.slack.botTokenSecretRef`, if set, names this Secret and key.

   - `SLACK_ALLOWED_USERS` in `install.env` holds member IDs such as `U0123ABCD`, not emails
     ([Allowed users](/kube-agents/install/slack-app/#allowed-users)).
   - `SLACK_HOME_CHANNEL` in `install.env` is the ID of the channel for alerts and scheduled
     reports: `C…`, or `G…` for some private channels. In Slack, the ID is at the bottom of the
     channel's details. Under `next` the gateway posts the agent's proactive messages to this
     channel only. A home channel set from Slack with `/sethome` is kept in the Planning Agent's
     profile, not in `install.env`, so it does not carry over. A DM ID (`D…`) or a channel name
     turns off the gateway's notify route, and with it the reports of board cards
     ([Slack app setup → Home channel](/kube-agents/install/slack-app/#home-channel)).

3. Check the board for cards in flight:

   ```bash
   kubectl exec deploy/platform-agent-gateway -c platform-agent -n kubeagents-system -- \
     hermes kanban ls --status running
   kubectl exec deploy/platform-agent-gateway -c platform-agent -n kubeagents-system -- \
     hermes kanban ls --status blocked
   ```

   Wait until neither command lists a card: answer each blocked card in its Slack thread, and let
   running ones finish ([What carries over](#what-carries-over) says why). If scheduled jobs keep
   filing cards, switch between their runs.

4. Switch when nobody is using the agent, and tell its users that Slack will not answer for a
   while.

### The switch

Edit the `install.env` the upgrade reads ([Before you start](#before-you-start) says where it
looks). Set the key, adding the line if the file has none:

```bash
PLATFORM_AGENT_MODE=next
```

[Run the upgrade](#run-the-upgrade) again at the release the install runs, in the default full
mode: pass no `--upgrade-mode`. Before it applies anything, the run warns:

```text
This apply switches the install from spec.mode today to next (PLATFORM_AGENT_MODE in install.env): a mode switch, not a settings change.
```

It does not stop to ask. To stay on `today`, press Ctrl-C at that warning and set the key back.

`--upgrade-mode=harness` and `--upgrade-mode=operator` leave the mode as it is and say that a full
upgrade would switch it. If the run says instead that the `PlatformAgent` carries a `spec.mode` set
outside the installer, the resource was patched by hand and the key cannot move it; the message
says what to do.

### During the switch

The operator moves Slack in three steps:

1. It restarts the credential broker without the Slack tokens, stopping the old broker pod before
   it starts the new one, and restarts the agent's pod without its Slack listener. From here, no
   process holds the app's Socket Mode connection.
   - Messages the broker had taken from Slack but not yet handed to the agent are lost: the
     broker acknowledges each event to Slack when it arrives and holds it in memory.
   - A question the agent was answering stops with its pod and gets no answer.
   - A board card that was running is put back in the queue when the new pod starts, and runs
     again from the start.
2. It creates the NATS bus and the auth callout. It creates the A2A gateway once a callout
   replica is ready, which needs the bus to be running. The provisioning Job, started at the same
   point, creates the bus's streams.
3. The gateway joins the bus, retrying for up to 45 seconds while the streams do not exist and
   restarting after that. It then checks the bot token with Slack, logs `slack connected`, and
   opens the Socket Mode connection.

The broker's connection stops when the switch starts, and the gateway exists only once the bus is
up, so in practice the two never take the same message. How long Slack goes unanswered depends on
how fast the cluster schedules and pulls the new pods. Slack has no connection to deliver a message
sent in that gap to, and nothing in kube-agents fetches it later: treat it as lost, and ask its
sender to send it again. Wait for the gateway, for up to 15 minutes:

```bash
for i in $(seq 90); do
  kubectl logs deploy/platform-agent-a2a-gateway -c gateway -n kubeagents-system 2>/dev/null \
    | grep -q 'slack connected' && { echo connected; break; }
  sleep 10
done
```

It prints `connected` once the gateway has logged `slack connected`; then check Slack as in
[Checking the result](#slack). If it prints nothing, the `Ready` condition's message names what the
operator is waiting on, and
[Troubleshooting](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#6-tracing-where-a-chat-message-stops)
has the checks for the bus and the gateway. Slack stays unanswered until the gateway connects or
you [switch back](#switching-back-to-today).

```bash
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}{"\n"}'
```

### What carries over

- **The same app, tokens and allowlist.** The gateway reads the tokens from the same Secret.
- **The board and the agent's data volume.** The new pod mounts the same volume.
- **Not the conversations.** Under `next` the agent starts every Slack conversation with no
  history from `today`. In a channel, a reply without a mention in a thread started before the
  switch gets no answer: mention the app.
- **Not the cards in flight, outside the home channel.** A card filed before the switch from a
  DM or from any channel other than the home channel posts nothing after it: the gateway refuses
  the post and logs `thread "<channel>/<ts>" is not a thread of the home channel`. A card filed
  in a home-channel thread still posts its progress and report there. Either way its buttons stop
  working, because the gateway does not take button clicks.
- **The home channel only from `install.env`**, as in step 2 above.

Slash commands, buttons, cards and the other Slack features that do not carry over are listed in
[ChatOps → What carries over from default mode](/kube-agents/concepts/chatops/#what-carries-over-from-default-mode).

### After the switch: the Slack app

Once a DM gets an answer as in [Checking the result](#slack), change the app at
<https://api.slack.com/apps> by hand:

1. **Slash Commands**: delete every command.
2. **Interactivity & Shortcuts**: turn **Interactivity** off.
3. Check the app against the list under
   [Slack app setup → Create the app](/kube-agents/install/slack-app/#create-the-app): the
   **Messages** tab on, and the four `message.*` events with their history scopes. Leave other
   events and scopes: the gateway acknowledges an event it does not use and ignores it. If you add
   a scope, reinstall the app; Slack grants new scopes only on install.

Do not paste the `next` manifest over the app: a manifest replaces the app's whole configuration,
including its name and description. Change the app after the switch rather than before, because
the `today` listener uses the slash commands and buttons until then. Between the switch and these
changes, a slash command or a button gets Slack's timeout error, because the gateway does not
acknowledge them.

### Switching back to `today`

1. Set `PLATFORM_AGENT_MODE=today` in `install.env`, or delete the line, and run the upgrade again
   in full mode. The run warns
   `This apply switches the install from spec.mode next to today (PLATFORM_AGENT_MODE in install.env): a mode switch, not a settings change.`
2. The operator restarts the credential broker with the Slack tokens and deletes the A2A stack,
   the gateway with it. For the seconds the gateway pod takes to stop, both can hold a
   connection, and a message the stopping gateway takes gets no answer. The bus volume and its
   credentials Secret stay; [Uninstall](/kube-agents/install/uninstall/#what-specmode-next-leaves-behind)
   says how to remove them. Wait for the broker, for up to 5 minutes:

   ```bash
   for i in $(seq 30); do
     kubectl logs deploy/platform-agent-credential-proxy -n kubeagents-system 2>/dev/null \
       | grep -q 'Slack relay enabled' && { echo connected; break; }
     sleep 10
   done
   ```

   It prints `connected` once the broker has connected to Slack. If it prints nothing, read the
   broker's log as in [Checking the result](#slack).

3. Put the slash commands and interactivity back. Print the `today` manifest:

   ```bash
   kubectl exec deploy/platform-agent-gateway -c platform-agent -n kubeagents-system -- \
     hermes slack manifest
   ```

   Before you paste it into the app's **App Manifest** page, change its `name`, `description` and
   `display_name` to the app's current values: the manifest replaces the app's whole
   configuration. [Slack app setup](/kube-agents/install/slack-app/#create-the-app) covers its
   options; read its warning about agent view first. Reinstall the app if Slack asks.

4. Check Slack as in [Checking the result](#slack).

Conversations and cards started under `next` do not carry back, for the same reasons as above.

To roll back to an older release, switch to `today` first:
[Rolling back a release](/kube-agents/deploy/rollback/#before-you-start) says why.

## When an upgrade is refused

Every one of these stops the run before any of the new release is applied. The first three are
settled before the run touches the cluster at all. The last three need the cluster: they are settled
after `kubectl` has been pointed at it, and after a real run's pre-flight Secret backfills — a plan
skips those — but still before any CRD, chart or Terraform change of the new release.

- **The sources do not match the release.** The checkout's `HEAD` is not the release's commit, the
  tree has uncommitted changes, or an unpacked tree names a release other than the one asked for —
  either in its `.release-bundle` marker or in the version stamped into its root scripts. A release
  script carries its version with it, so standing in an older unpacked tree and running a newer one
  is refused rather than silently applying the older tree. The stamp is read from `install.sh` and
  `uninstall.sh` before `upgrade.sh`, because saving a newer `upgrade.sh` into an older tree
  overwrites the one file that would otherwise be asked. Start again from a clean checkout or from
  the bundle of the release you want.
- **`KUBE_AGENTS_INSTALL_ENV` names a file that is not there.** An explicit pointer is taken
  literally, not fallen back from, so a stale or mistyped value is reported with the path you gave
  rather than searched past. Fix the path or unset the variable.
- **No install configuration.** Neither `KUBE_AGENTS_INSTALL_ENV` nor an `install.env` was found in
  any of the configuration locations described at the top of this page. An install predating 0.4.0,
  which kept its settings in the retired `k8s-operator/scripts/vars.sh` and never gained an
  `install.env`, is refused here: copy those settings into an `install.env` first.
- **No Helm release.** The target namespace has no `kube-agents` release to upgrade.
- **The memory store cannot be checked and nothing named one.** When the configuration records no
  `MEMORY`, the upgrade asks the cluster whether it runs the Hindsight memory store, so that a store
  that is there is kept rather than planned away. If the cluster cannot be asked — `kubectl` is
  pointed elsewhere, the credentials have expired, the API server times out — the run stops instead
  of guessing. Record `MEMORY=hindsight|file|off` in `install.env`, or restore access to the cluster
  and re-run.
- **The chart does not declare a value the release recorded.** The operator and harness modes
  re-apply the values the release recorded, and name each one the new chart's values schema refuses
  as undeclared. On an upgrade that is usually a setting the new chart renamed or removed: run
  `--upgrade-mode=full`, which renders `install.env` onto the chart instead. On a rollback to a
  release that predates the value, re-run with `--drop-undeclared-values`. A full upgrade refuses
  that flag, since it has no recorded values to drop.

## Where to go next

- [Rolling back a release](/kube-agents/deploy/rollback/) — the reverse move, and what each mode
  leaves behind when it goes backwards.
- [Release versioning and promotion](/kube-agents/deploy/release-versioning/) — what a release tag
  guarantees about the artifacts it names.
- [Uninstall](/kube-agents/install/uninstall/) — removing the install instead of moving it.

---
title: Slack app setup
description: Create the Slack app, its tokens and its allowlist for a kube-agents install, in either mode.
---

kube-agents talks to Slack through one Slack app that you create in your workspace. The app connects over Socket Mode (an outbound websocket), so nothing on the cluster needs an inbound endpoint. Which process holds the connection depends on the install's `spec.mode`:

- **`today`** (the default): the credential broker (the `platform-agent-credential-proxy` Deployment) holds the connection and relays each event to the Planning Agent's Hermes Slack listener in the `platform-agent-gateway` Deployment.
- **`next`**: the A2A gateway, in the `platform-agent-a2a-gateway` Deployment. This needs release **0.9.0 or later**: earlier operators keep Slack on the `today` path even under `next`. If Google Chat is also enabled, Google Chat holds the A2A gateway and Slack stays on the `today` path.

The two need different app settings, so follow the section for your mode. The tokens and the allowlist work the same way in both.

To install with the `next` path, pass `--mode=next` to the installer (`./install.sh --mode=next`, with the Slack flags below). `next` is an unsupported development stack; `today` is the default. The `--mode` flag is in releases after 0.9.0 and on `main`. The installer records the mode in `install.env` as `PLATFORM_AGENT_MODE`; to switch a running install later, edit that key and re-run the installer.

## Before you start

- A Slack workspace where you may install apps. Many workspaces require a workspace admin to approve a new app; if yours does, ask before you start, or the install waits on the approval.
- One workspace per install under `next`: the A2A gateway takes a single bot token and refuses members of other workspaces.

## Create the app

### For `spec.mode: next` (the A2A gateway)

1. Open <https://api.slack.com/apps>, choose **Create New App → From a manifest**, and pick your workspace.
2. Paste this manifest (YAML), changing only `name` and `display_name`:

   ```yaml
   display_information:
     name: kube-agents
   features:
     app_home:
       messages_tab_enabled: true
       messages_tab_read_only_enabled: false
     bot_user:
       display_name: kube-agents
       always_online: true
   oauth_config:
     scopes:
       bot:
         - channels:history
         - channels:read
         - chat:write
         - groups:history
         - groups:read
         - im:history
         - im:read
         - mpim:history
         - mpim:read
   settings:
     event_subscriptions:
       bot_events:
         - message.channels
         - message.groups
         - message.im
         - message.mpim
     interactivity:
       is_enabled: false
     socket_mode_enabled: true
     token_rotation_enabled: false
   ```

3. Choose **Create**.

What the manifest covers, so you can check an existing app instead:

- **Events.** The gateway reads messages only: `message.im` for DMs, and `message.channels`, `message.groups` and `message.mpim` for public, private and group channels. Each event needs its history scope (`im:history`, `channels:history`, `groups:history`, `mpim:history`); without it the app connects normally and is never sent the message. In a channel the gateway answers only a message that mentions the app, or a reply in a thread it is already answering; it finds mentions in the message text, so `app_mention` is not needed.
- **Scopes.** `chat:write` covers every post and edit the gateway makes. `channels:read`, `groups:read`, `im:read` and `mpim:read` read who is in a conversation. A reply goes into the conversation or thread the message came from, so no other scope is needed, in a DM either.
- **Messages tab.** Without it, members cannot DM the app.
- **No slash commands and no interactivity.** The gateway does not acknowledge them, so a member who used one would see Slack's timeout error. If you are moving an app from `today`, remove its slash commands and turn interactivity off.

### For `spec.mode: today` (the Hermes listener)

The Hermes listener needs more than the gateway: slash commands, reactions, files, and the assistant surface. Its manifest is the one `hermes slack manifest` prints, which needs an installed pod, so set the app up in two passes:

1. Create the app **From scratch**, turn on **Socket Mode**, and create the app-level token (below).
2. Add the bot scopes listed in [`INSTALL.md` Step 5](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#2-slack-configuration-slack_enabledtrue), install the app, copy the bot token (below), and run the installer with Slack enabled.
3. Once the install is up, print the full manifest and paste it into the app's **App Manifest** page:

   ```bash
   kubectl exec deploy/platform-agent-gateway -n kubeagents-system -- hermes slack manifest
   ```

   `INSTALL.md` Step 5 covers its options (`--no-assistant`, `--slashes-only`). A manifest that turns on Slack's [agent view](/kube-agents/concepts/chatops/#agent-view) cannot be undone, so read that section before you apply one.

## Get the tokens

1. **App-level token** (`xapp-…`): **Basic Information → App-Level Tokens → Generate Token and Scopes**, add the scope `connections:write`, and generate it. Socket Mode needs this scope.
2. **Bot token** (`xoxb-…`): **OAuth & Permissions → Install to Workspace** (or **Request to Install** where an admin approves apps), then copy **Bot User OAuth Token**. Reinstall the app whenever you change its scopes; Slack only grants new scopes on install.

Pass them to the installer as `--slack-bot-token` and `--slack-app-token`. Under `next`, give exactly one bot token: the gateway cannot use a comma-separated list.

Once the app is installed, invite it to any channel it should answer in, using the bot's display name from the manifest (`/invite @kube-agents` for the manifest above). It receives a channel's messages only once it is a member; DMs need no invite.

## Allowed users

The allowlist decides who may talk to the agent. It takes **Slack member IDs**, not emails or display names, and matches them exactly:

- **Find a member ID:** in Slack, open the person's profile, choose the **⋮** (more) menu, then **Copy member ID**. It looks like `U0123ABCD`.
- **Set it:** pass `--slack-allowed-users=U0123ABCD,U0456EFGH` on the first installer run. To change it later, edit `SLACK_ALLOWED_USERS` in `install.env` and re-run the installer: a re-run refuses a `--slack-allowed-users` that disagrees with `install.env`, and an upgrade renders the `PlatformAgent` resource from `install.env`, so a hand edit of the resource does not last.
- **An email never matches.** An entry like `alice@example.com` admits nobody; if every entry is an email, everyone is refused.
- **An empty list admits every member of the workspace.** In a shared or company workspace that means anyone there can drive an agent with access to your clusters, so set a list unless the workspace is yours alone.

What a member who is not on the list sees:

- **`next`:** the gateway answers once per member with a notice that it cannot verify them, naming their member ID, so an admin can add them. Members of other workspaces get no reply.
- **`today`:** the message is ignored, with no reply.

The pairing step in `INSTALL.md` applies only if you set the Hermes Slack DM policy to `pairing` yourself. A kube-agents install configures the allowlist instead, and the A2A gateway has no pairing.

## Principal map (`next`, optional)

Under `next` the gateway attributes each admitted member by their member ID (`slack:U0123ABCD`) in the request it sends the agent. To attribute members by an identity from your identity provider instead, create the optional Secret `a2a-slack-principal-map` in the install's namespace: one key per member ID, whose value is that member's principal.

```bash
kubectl create secret generic a2a-slack-principal-map -n kubeagents-system \
  --from-literal=U0123ABCD=alice@example.com \
  --from-literal=U0456EFGH=bob@example.com
kubectl rollout restart deployment/platform-agent-a2a-gateway -n kubeagents-system
```

The gateway reads the map when it starts, so restart it after every change. The map changes attribution only: it admits no one, and a member the allowlist refuses is refused whatever the map says. A value that starts with `slack:` locks that member out, even when the allowlist admits them, until the entry is corrected. The [`spec.integration`](/kube-agents/operator/platformagent-crd/#specintegration) reference has the rest of the map's behaviour.

## Verify

1. Check that the app connected. Under `next`:

   ```bash
   kubectl logs deploy/platform-agent-a2a-gateway -c gateway -n kubeagents-system | grep 'slack connected'
   ```

   A line with `"msg":"slack connected"` names the bot user and the workspace (`team`). Under `today`, the credential broker holds the connection (`kubectl logs deploy/platform-agent-credential-proxy -n kubeagents-system`) and the Hermes listener handles the messages (`kubectl logs deploy/platform-agent-gateway -c platform-agent -n kubeagents-system`).

2. DM the app (**Apps**, then the app's name, then **Messages**) with a question such as `what clusters can you see?`. The answer comes back in the DM.
3. If nothing comes back, check the gateway log for these lines:

   | Log line (`msg`)                                                                                       | Cause                                                                     | Fix                                                        |
   | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- | ---------------------------------------------------------- |
   | none at all for your message                                                                           | A missing history scope or event subscription, or the messages tab is off | Compare the app with the manifest above, then reinstall it |
   | `dropping message from unverified sender`                                                              | Your member ID is not on the allowlist                                    | Add the member ID, not an email                            |
   | `slack allowlist is empty and allow-all is off; every inbound message will be dropped at verification` | The list has only blank entries                                           | Set real member IDs, or remove the list to admit everyone  |
   | `slack: ignoring a message from another workspace's member`                                            | The sender is in another workspace                                        | Use the workspace the app is installed in                  |
   | `chat backend stopped; restarting it, the console stays up`                                            | The tokens were refused, for example a comma-separated bot token          | Pass one `xoxb-` bot token and one `xapp-` app token       |

---
title: Slack app setup
description: Create the Slack app, its tokens and its allowlist for a kube-agents install, in either mode.
---

kube-agents talks to Slack through one Slack app that you create in your workspace. The app connects over Socket Mode (an outbound websocket), so nothing on the cluster needs an inbound endpoint. Which process holds the connection depends on the install's `spec.mode`:

- **`today`**: the credential broker (the `platform-agent-credential-proxy` Deployment) holds the connection and relays each event to the Planning Agent's Hermes Slack listener in the `platform-agent-gateway` Deployment.
- **`next`** (a first install's default): the A2A gateway, in the `platform-agent-a2a-gateway` Deployment. This needs release **0.9.0 or later**: earlier operators keep Slack on the `today` path even under `next`. If Google Chat is also enabled, Google Chat holds the A2A gateway and Slack stays on the `today` path.

The two need different app settings, so follow the section for your mode. The tokens and the allowlist work the same way in both.

A first install runs `next` unless you pass `--mode=today` (`./install.sh --mode=today`, with the Slack flags below) or mirror the third-party images ([quickstart](/kube-agents/install/quickstart-gke/#common-flags-and-toggles)); a cluster that already runs kube-agents keeps the mode it runs unless `--mode` or `install.env` names another. An install whose Slack bot token is a list for several workspaces must stay on `today`. The `--mode` flag is in releases after 0.9.0 and on `main`. On 0.9.0 itself, patch the resource instead: `kubectl patch platformagent platform-agent -n kubeagents-system --type merge -p '{"spec":{"mode":"next"}}'`; that patched mode survives later upgrades, because a release that renders no mode sends Helm no change; to have the installer record it, set `PLATFORM_AGENT_MODE=next` in `install.env`. The installer records the mode in `install.env` as `PLATFORM_AGENT_MODE`; to switch a running install later, edit that key and re-run the installer ([Switching `spec.mode`](/kube-agents/install/upgrade/#switching-specmode)).

## Before you start

- A Slack workspace where you may install apps. Many workspaces require a workspace admin to approve a new app; if yours does, ask before you start, or the install waits on the approval.
- One workspace per install under `next`: the A2A gateway takes a single bot token. It refuses members of other workspaces, such as Slack Connect guests, unless the allowlist names each one (below).

## Create the app

### For `spec.mode: next` (the A2A gateway)

1. Open <https://api.slack.com/apps>, choose **Create New App → From a manifest**, and pick your workspace.
2. Paste this manifest (YAML), changing only `name` and `display_name`:

   ```yaml
   display_information:
     name: kube-agents
   features:
     app_home:
       home_tab_enabled: true
       messages_tab_enabled: true
       messages_tab_read_only_enabled: false
     bot_user:
       display_name: kube-agents
       always_online: true
     assistant_view:
       assistant_description: Ask kube-agents about your clusters.
     shortcuts:
       - name: Ask about this
         type: message
         callback_id: ask_about_message
         description: kube-agents looks into this message and answers in its thread
   oauth_config:
     scopes:
       bot:
         - assistant:write
         - channels:history
         - channels:read
         - chat:write
         - commands
         - groups:history
         - groups:read
         - im:history
         - im:read
         - mpim:history
         - mpim:read
         - reactions:write
         - users:read
   settings:
     event_subscriptions:
       bot_events:
         - agent_session_stopped
         - app_home_opened
         - assistant_thread_started
         - message.channels
         - message.groups
         - message.im
         - message.mpim
     interactivity:
       is_enabled: true
     socket_mode_enabled: true
     token_rotation_enabled: false
   ```

3. Choose **Create**.

What the manifest covers, so you can check an existing app instead:

- **Events.** The gateway reads messages only: `message.im` for DMs, and `message.channels`, `message.groups` and `message.mpim` for public, private and group channels. Each event needs its history scope (`im:history`, `channels:history`, `groups:history`, `mpim:history`); without it the app connects normally and is never sent the message. In a channel the gateway answers only a message that mentions the app, or a reply in a thread it is already answering; it finds mentions in the message text, so `app_mention` is not needed.
- **Scopes.** `chat:write` covers every post and edit the gateway makes. `channels:read`, `groups:read`, `im:read` and `mpim:read` read who is in a conversation. A reply goes into the conversation or thread the message came from, so no other scope is needed for it, in a DM either. `reactions:write` puts the reactions on your message, `users:read` names who answered a question, who used the message shortcut and who wrote the message it quotes (without it they read as Someone), and `assistant:write` shows Working… on a thread, titles it and sets the suggested prompts; without them the answers still arrive and only those go missing ([ChatOps](/kube-agents/concepts/chatops/#slack)).
- **Interactivity, the assistant view and `assistant_thread_started`.** The gateway answers a click on a question's buttons, on an answer's 👍 or 👎, and the message shortcut, which all arrive as interactions, so interactivity is on, and it offers suggested prompts when a member opens a new assistant thread. On an app using agent view the gateway also answers `app_home_opened` on the Messages tab with the prompts.
- **`agent_session_stopped`.** Slack offers a Stop button on a thread's Working… line to an app that subscribes it, and sends the event when someone presses it; the gateway treats it as `stop` in that thread ([ChatOps](/kube-agents/concepts/chatops/#follow-ups-while-a-task-runs)). Without it there is no Stop button and typed `stop` still works. Adding it adds no scope; reinstall the app if Slack asks.
- **Messages tab.** Without it, members cannot DM the app.
- **Home tab and `app_home_opened`.** The gateway draws the fleet dashboard ([the Slack Home tab](/kube-agents/concepts/chatops/#the-slack-home-tab)) when a member opens the tab. Without either, the tab is absent or blank and nothing else changes; neither needs a scope. With `allowedUsers` left empty, the gateway checks each member's workspace with `users.info`, so the tab also needs `users:read`; without it the tab stays blank.
- **The message shortcut and `commands`.** The `shortcuts` entry puts **Ask about this** in every message's **⋯** menu, with the app's name beside it, and Slack offers shortcuts only to an app with the `commands` scope. The gateway knows the shortcut by its `callback_id`, so keep `ask_about_message` and rename only `name` and `description`; Slack refuses a `name` of 25 characters or more. The shortcut also needs interactivity on. Without the entry or the scope, everything else works and the menu entry is missing ([ChatOps](/kube-agents/concepts/chatops/#which-messages-it-answers)).
- **No slash commands.** The gateway does not acknowledge them, so a member who used one would see Slack's timeout error.

### Moving an existing app to `next`

An app made for `today` from the `hermes slack manifest` output already has every scope and event the gateway needs to answer messages (the Home tab needs the two settings in step 3, and the Stop button needs `agent_session_stopped`), unless it was generated with `--no-assistant`, so it keeps its tokens and needs no new install. A `--no-assistant` app lacks `assistant:write`, the assistant view and `assistant_thread_started`: add them as in the manifest above and reinstall, or Working…, the thread titles and the suggested prompts do not appear. Edit it rather than pasting the manifest above, which would rename the app and drop the scopes `today` needs if you switch back:

1. After the switch, open the app's **App Manifest** page.
2. Delete the `features.slash_commands` block. Leave interactivity on: the gateway answers button clicks.
3. For the [Home tab](/kube-agents/concepts/chatops/#the-slack-home-tab), set `features.app_home.home_tab_enabled` to `true` and add `app_home_opened` to `settings.event_subscriptions.bot_events`.
4. For the Stop button, add `agent_session_stopped` to `settings.event_subscriptions.bot_events`.
5. Save. The edit changes no scopes; if Slack still asks you to reinstall, do so.

Do it after the switch rather than before, because the `today` listener answers slash commands until then. Until you do, a slash command under `next` shows the member Slack's timeout error. Switching back to `today` means putting the block back, which the full manifest from `hermes slack manifest` does.

For the message shortcut, add the `features.shortcuts` entry from the manifest above in the same edit. The app already has the `commands` scope, since its slash commands need it; if Slack still asks you to reinstall, do so. The `today` listener has no handler for the shortcut, so remove the entry before switching back.

### For `spec.mode: today` (the Hermes listener)

The Hermes listener needs more than the gateway: slash commands and files on top of the reactions and assistant surface both use. Its manifest is the one `hermes slack manifest` prints. [`INSTALL.md` Step 5](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#2-slack-configuration-slack_enabledtrue) owns the procedure: create the app from that manifest (**Create New App → From a manifest**), which the command prints on an install where Slack is not configured yet, and its options (`--no-assistant`, `--slashes-only`). A manifest that turns on Slack's [agent view](/kube-agents/concepts/chatops/#agent-view) cannot be undone, so read that section before you apply one.

## Get the tokens

1. **App-level token** (`xapp-…`): **Basic Information → App-Level Tokens → Generate Token and Scopes**, add the scope `connections:write`, and generate it. Socket Mode needs this scope.
2. **Bot token** (`xoxb-…`): **OAuth & Permissions → Install to Workspace** (or **Request to Install** where an admin approves apps), then copy **Bot User OAuth Token**. Reinstall the app whenever you change its scopes; Slack only grants new scopes on install.

Pass them to the installer as `--slack-bot-token` and `--slack-app-token`. Under `next`, give exactly one bot token: the gateway cannot use a comma-separated list.

Once the app is installed, invite it to any channel it should answer in, using the bot's display name from the manifest (`/invite @kube-agents` for the manifest above). It receives a channel's messages only once it is a member; DMs need no invite.

## Home channel

The home channel is where the agent posts unprompted: alerts, scheduled reports, and the fleet audit's report card. Under `next`, set one: without it, every one of those posts is dropped while conversations keep working, so nothing else shows it.

- **`next`:** only the resource field counts, `spec.integration.slack.homeChannel`, which the installer sets from `SLACK_HOME_CHANNEL` (`--slack-home-channel`). It must be a channel ID (`C0123ABCD`, or `G…` for a private channel), not a name: open the channel's details and copy the **Channel ID** at the bottom. Invite the app to that channel, or its posts are refused. `spec.integration.slack.homeChannelPolicy` (`auto` or `manual`) is accepted in this release but has no effect yet; the [CRD reference](/kube-agents/operator/platformagent-crd/#specintegration) says what it is for. With the field unset, the agent posts nothing to Slack unprompted, though a delegated task's report still reaches the conversation that asked for it; the installer warns before it applies that (and `install.sh`'s setup interview asks you to confirm), the gateway logs a warning at start, and the PlatformAgent carries the `HomeChannelUnset` condition until the field is set. Set to something that is not a channel ID, it turns off both. `/sethome` does not reach it.
- **`today`:** the field, or `/sethome` (`/hermes sethome`) run in the channel you want, which writes the Planning Agent's profile. [`INSTALL.md` Step 5](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#2-slack-configuration-slack_enabledtrue) has the detail.

A home channel set with `/sethome` under `today` does not carry over to `next`. Before or at the switch, set `SLACK_HOME_CHANNEL` in `install.env` to that channel's ID and re-run the installer.

## Allowed users

The allowlist decides who may talk to the agent. It takes **Slack member IDs**, not emails or display names, and matches them exactly:

- **Find a member ID:** in Slack, open the person's profile, choose the **⋮** (more) menu, then **Copy member ID**. It looks like `U0123ABCD`.
- **Set it:** pass `--slack-allowed-users=U0123ABCD,U0456EFGH` on the first installer run. To change it later, edit `SLACK_ALLOWED_USERS` in `install.env` and re-run the installer: a re-run refuses a `--slack-allowed-users` that disagrees with `install.env`, and an upgrade renders the `PlatformAgent` resource from `install.env`, so a hand edit of the resource does not last.
- **An email never matches.** An entry like `alice@example.com` admits nobody; if every entry is an email, everyone is refused.
- **An empty list admits every member of the workspace.** In a shared or company workspace that means anyone there can drive an agent with access to your clusters, so set a list unless the workspace is yours alone.
- **A member of another workspace (`next` only):** a Slack Connect guest is admitted only by an entry with their workspace ID, a colon and their member ID, for example `T0123WXYZ:U0123ABCD`. The bot's refusal notice gives the guest this exact entry, and the gateway's refusal log line names both IDs. Under `next`, a plain member ID always means a member of your own workspace, so it never admits a guest, and an empty list admits no guest either. A list with guest entries is not empty, so list your own members too. A listed guest gets the same access as your own members: they can ask the agent to act on your clusters, and the Home tab shows them the fleet. So list only people the outside organization's access should extend to. With guest entries on the list, the Home tab checks each member's workspace with `users.info` (the `users:read` scope), so a guest listed only by a plain member ID is not shown the fleet either. Under `today` a guest entry admits nobody.

What a member who is not on the list sees:

- **`next`:** the gateway answers once per member with a notice that it cannot verify them, naming their member ID, so an admin can add them. A member of another workspace in a Slack Connect channel is not served, even with an empty list, unless a `T…:U…` entry names them. The first time they mention the bot, send it a direct message, or click one of its buttons, the bot replies once, in that thread, that it answers only its own workspace. The reply names their member ID and the `T…:U…` entry to ask an admin to add. The reply is a public post in the thread, so in a Slack Connect channel members of both organizations can read it. A member who uses the message shortcut gets the notice that it cannot verify them every time, visible only to them; a member of another workspace who uses it gets no reply, even when a `T…:U…` entry names them.
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

A Slack Connect guest cannot be mapped: a Secret key cannot hold the `:` of a `T…:U…` entry, so a guest is always attributed by that entry (`slack:T…:U…`). The gateway reads the map when it starts, so restart it after every change. The map changes attribution only: it admits no one, and a member the allowlist refuses is refused whatever the map says. A value that starts with `slack:` locks that member out, even when the allowlist admits them, until the entry is corrected. The [`spec.integration`](/kube-agents/operator/platformagent-crd/#specintegration) reference has the rest of the map's behaviour.

## Verify

1. Check that the app connected. Under `next`:

   ```bash
   kubectl logs deploy/platform-agent-a2a-gateway -c gateway -n kubeagents-system | grep 'slack connected'
   ```

   A line with `"msg":"slack connected"` names the bot user and the workspace (`team`). Under `today`, the credential broker holds the connection (`kubectl logs deploy/platform-agent-credential-proxy -n kubeagents-system`) and the Hermes listener handles the messages (`kubectl logs deploy/platform-agent-gateway -c platform-agent -n kubeagents-system`).

2. DM the app (**Apps**, then the app's name, then **Messages**) with a question such as `what clusters can you see?`. The answer comes back in the DM, in a thread under your question (an older release answers it top-level in the DM).
3. If nothing comes back, check the gateway log for these lines:

   | Log line (`msg`)                                                                                       | Cause                                                                     | Fix                                                                                                                                       |
   | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
   | none at all for your message                                                                           | A missing history scope or event subscription, or the messages tab is off | Compare the app with the manifest above, then reinstall it                                                                                |
   | `dropping message from unverified sender`                                                              | Your member ID is not on the allowlist                                    | Add the member ID, not an email                                                                                                           |
   | `slack allowlist is empty and allow-all is off; every inbound message will be dropped at verification` | The list has only blank entries                                           | Set real member IDs, or remove the list to admit everyone                                                                                 |
   | `slack: ignoring a message from another workspace's member`                                            | The sender (`user`) is in another workspace (`userTeam` or `messageTeam`) | Use the workspace the app is installed in, or admit the sender with a `userTeam:user` entry (`messageTeam:user` when `userTeam` is empty) |
   | `chat backend stopped; restarting it, the console stays up`                                            | The tokens were refused, for example a comma-separated bot token          | Pass one `xoxb-` bot token and one `xapp-` app token                                                                                      |

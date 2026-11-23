---
title: Microsoft Teams app setup
description: Register the bot with Microsoft, store its credentials, turn Teams on for a kube-agents install, and give Teams an address to deliver messages to.
---

kube-agents talks to Microsoft Teams as a bot you register in your own Microsoft Entra tenant. The install holds the bot's credentials in its credential broker and posts replies through Microsoft's Bot Framework. Teams delivers each message to an HTTPS address you publish, so unlike Slack and Google Chat, Teams needs one inbound route to the install.

This page is the short path. The [Microsoft Teams guide](https://github.com/gke-labs/kube-agents/blob/main/docs/chatops/microsoft-teams.md) in the repository has every option, the PlatformAgent resource form of each setting, and the list of what ships.

## Before you start

- A Microsoft Entra tenant where you may register an application and create an Azure Bot resource. Many tenants need an administrator for both.
- Permission to upload a custom app to the team or chat where the bot will live.
- A public host name with a TLS certificate, and an Ingress or Gateway on the cluster that can route it. Bot Framework delivers over HTTPS only.
- The Entra object id of each person who may talk to the bot. The allowlist matches object ids, not email addresses or display names.

## Register the bot

1. In the Azure portal, open **Microsoft Entra ID**, then **App registrations**, and create a single-tenant registration. This page sets `tenantId`, and a multi-tenant bot can then get a 401 on its replies.
2. Note the **Application (client) ID** and the **Directory (tenant) ID**.
3. Under **Certificates & secrets**, create a client secret and copy its value.
4. Create an **Azure Bot** resource with that application id, as a single-tenant bot.
5. In the bot's **Channels**, add **Microsoft Teams**.

## Store the credentials

Add the application id and the client secret to the install's credentials Secret, as two keys beside the ones it already holds. Do not replace the Secret: it carries the install's other credentials.

```bash
kubectl -n kubeagents-system patch secret platform-agent-secrets --type merge \
  -p '{"stringData":{"TEAMS_APP_ID":"<application id>","TEAMS_APP_PASSWORD":"<client secret>"}}'
```

## Turn Teams on

Set the Teams block in your Helm values. The guide shows the same settings on the PlatformAgent resource; prefer the values, since the chart does not know about a hand edit of the resource and a later chart apply can replace it.

```yaml
platformAgent:
  credentials:
    secretName: platform-agent-secrets
  integration:
    teams:
      enabled: true
      tenantId: "<directory (tenant) id>"
      allowedUsers:
        - "<entra object id>"
      homeChannel: "<conversation id>"
      inbound:
        enabled: true
```

On an install made with `install.sh` there is no values file. Put the same keys on the `TF_VAR_extra_helm_values` line in `install.env`, as an HCL object, and run the installer again. [Operator settings](/kube-agents/install/quickstart-gke/#operator-settings) describes that line; there is one of it, so add these keys to whatever it already carries.

```bash
TF_VAR_extra_helm_values='{platformAgent={integration={teams={enabled=true,tenantId="<directory (tenant) id>",allowedUsers=["<entra object id>"],homeChannel="<conversation id>",inbound={enabled=true}}}}}'
```

- `tenantId` limits the bot to your tenant. The receiver below needs it.
- `allowedUsers` lists who may talk to the bot. A message from anyone else gets a refusal.
- `homeChannel` is the conversation the agent's own posts go to, by its Teams conversation id: a channel's starts with `19:` and ends in `@thread.tacv2`, a group chat's ends in `@thread.v2`, and a personal chat's starts with `a:`. By default only a scheduled job whose `deliver` names `teams` posts there. Alerts and the built-in scheduled reports reach it once Teams runs on the chat gateway (below). Leave it out and Teams gets replies only.
- `inbound.enabled` runs the receiver that takes messages from Teams.

## Give Teams an address

With `inbound.enabled`, the install runs a small receiver behind a Service named `<agent>-teams-inbound` on port 8100, where `<agent>` is the PlatformAgent's name, `platform-agent` by default. It checks that each request is signed by Microsoft for your bot and comes from your tenant, and refuses anything else.

1. Route your public host to that Service, for the path `/api/v1/teams/events`, with your own Ingress or Gateway and certificate. The guide has an example Ingress.
2. In the Azure Bot resource, open **Configuration** and set **Messaging endpoint** to `https://<your host>/api/v1/teams/events`.

If your ingress can restrict sources, Microsoft publishes Bot Framework's address ranges under the `AzureBotService` service tag.

## Install the app in Teams

1. Take the sample manifest from the repository, [`docs/samples/teams-manifest.json`](https://github.com/gke-labs/kube-agents/blob/main/docs/samples/teams-manifest.json), and put your application id in it.
2. Rename it `manifest.json` and zip it with two icons, `color.png` (192x192) and `outline.png` (32x32).
3. In Teams, open **Apps**, then **Manage your apps**, then **Upload an app**, and add the bot to the team or chat.

## Talk to the bot

- In a personal chat, every message reaches the bot.
- In a channel or a group chat, Teams delivers only messages that mention the bot. Type `@` and pick the bot from the list so its name becomes a mention. A reply inside a thread needs its own mention.

## Verify

Send the bot a short message. A reply means the whole path works: Teams reached your address, the receiver accepted the message, and the agent answered.

If nothing comes back:

- An accepted message leaves no line in the receiver's log, so a quiet log does not tell you whether Teams reached the address. The receiver counts every request that reaches it in `kubeagents_teams_inbound_requests_total`, by `outcome`, with `accepted` for the ones it took; the install's metrics collection scrapes it with the other pods. If no outcome has moved, Teams did not reach the address: check the messaging endpoint, the certificate and the route.
- The receiver logs each kind of refusal with its reason, such as a request from another tenant or one not signed for your bot.
- A person outside the allowlist does get a reply, a short refusal. On the chat gateway the refusal comes the first time they write and names their id, which is the value to add to `allowedUsers`. On the default path the id is in the agent's log instead, on the line that says it rejected an unauthorized user.
- In a channel, check that the bot's name was a mention and not plain text.

## The chat gateway

Teams runs through the Planning Agent's adapter by default. An operator can move it to the A2A chat gateway, which also serves Slack and Google Chat, with `A2A_TEAMS_BACKEND=true` in the operator's environment (`operator.extraEnv` in the chart). A running task then shows its status on one line that updates in place. [ChatOps](/kube-agents/concepts/chatops/#microsoft-teams) describes both paths.

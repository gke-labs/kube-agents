# Microsoft Teams ChatOps Integration Guide

This guide covers configuring the **Microsoft Teams Bot Framework relay** (`spec.integration.teams`) for the `kube-agents` PlatformAgent operator and gateway.

---

## Overview & Current Scope

The Microsoft Teams integration connects a consumer to the credential broker's Teams relay (`TeamsRelay` in `agents/platform/scripts/credential_proxy.py`): the Planning Agent's adapter (`agents/platform/scripts/teams_relay_patch.py` and `agents/chat/plugins/platforms/teams/`) by default, or the A2A chat gateway's `teams` backend when the operator arms it (the bullet below).

### What ships

- **Outbound Bot Framework Relay (Shipped)**: The `credential-proxy` sidecar holds `TEAMS_APP_ID` and `TEAMS_APP_PASSWORD`, exchanges them for Microsoft Entra OAuth 2.0 `client_credentials` tokens (`https://api.botframework.com/.default`), caches the token in memory, and forwards outbound activity posts through `POST /v1/chat/teams/api`.
- **Outbound Egress & Method Guards (Shipped)**: `/v1/chat/teams/api` permits only `POST v3/conversations/<id>/activities` (and activity reply subpaths, excluding `/activities/history`), plus `PUT` on one activity's own path for the gateway's status-line edits, and `PUT` and `DELETE` on one activity's `reactions/<reaction>` path for the bot's own reaction on a person's message (`DELETE` is permitted nowhere else), to official Bot Framework hosts (`smba.trafficmanager.net` and `*.botframework.com`). Service URLs containing query strings (`?`) or fragments (`#`) are rejected, and HTTP `3xx` redirects are refused so the bot's Bearer token is never forwarded off-host.
- **Microsoft Adaptive Cards v1.5 (Shipped)**: On the Planning Agent path, outbound messages convert Markdown headings, bullet lists, fenced code blocks, and a pipe table (a header row followed by a delimiter row) into Adaptive Card v1.5 attachments when `adaptiveCards: true` (default), or send plain text when `adaptiveCards: false`; the gateway backend renders the same set at full pane width. A long reply is split into several messages; a message whose card would exceed the gateway adapter's activity size cap is sent as plain text instead, and text that still does not fit is cut with a marker saying so.
- **In-Pod Event Queue & Authorization Scaffolding (Shipped)**: The credential broker exposes `/v1/chat/teams/events`, `/v1/chat/teams/events/ack`, and `/v1/chat/teams/events/nack`, both the broker and `TeamsRelayAdapter` enforce single-tenant isolation (`TEAMS_TENANT_ID` against `channelData.tenant.id` or `conversation.tenantId`), and only the consumer enforces the user allowlist (`TeamsRelayAdapter` in the gateway through `TEAMS_ALLOWED_USERS` / `TEAMS_ALLOW_ALL_USERS`, or the A2A gateway's `teams` backend through `A2A_TEAMS_ALLOWED_USERS` / `A2A_TEAMS_ALLOW_ALL_USERS` when the operator arms it), so the broker queues an activity from any sender in the tenant. The queue holds `TEAMS_EVENT_QUEUE_MAXSIZE` activities (500). An activity the gateway hands back (`/nack`) while the queue is full is dropped with a warning in the broker log, not held: the gateway nacks once and never retries, so a held activity would never be delivered. List users by Entra ID Object ID (`from.aadObjectId`) or Bot Framework user ID (`from.id`), the identifiers an inbound Teams activity carries. The adapter also compares `from.userPrincipalName` / `from.email`, but a Bot Framework activity does not carry those fields and nothing in the harness adds them, so a UPN or email entry matches no one. Display names are never matched.
- **A2A gateway backend (Shipped, behind an operator switch)**: with `A2A_TEAMS_BACKEND=true` on the operator's Deployment (the chart's `operator.extraEnv`), the A2A chat gateway's `teams` backend consumes the broker's Teams queue instead of the Hermes gateway: the operator renders `A2A_TEAMS_RELAY_URL`, the allowlist and `allowAllUsers` as the CRD defines them, the tenant and the home channel on the gateway (`a2aTeamsArmed`), admits the gateway as a broker caller, and leaves the Hermes `TEAMS_*` platform env out, so one consumer pulls the queue. Replies are Adaptive Cards unless `adaptiveCards` is false or a card would exceed the activity budget, a channel reply keys its session on the thread's conversation id, a kanban card's report comes back through the gateway's `chat.notify` route, and the sender is the Entra object id (or the Bot Framework id when that is what the list names). The operator also tells the broker (`CREDENTIAL_PROXY_TEAMS_A2A_CALLER`) that the gateway is the queue's consumer; without that word the broker's Teams routes admit the Planning Agent's role alone. The roster's scheduled reports reach the Teams home through the agent's delivery resolver, beside the other platforms. A Hermes cron job created by hand (`hermes cron create --deliver teams` or `--deliver chat`) does not: the Hermes scheduler delivers only through its own platforms, and on an armed install Teams is not one of them; post through `a2a notify --platform teams` instead. Without the switch the Hermes path stays. The design is in `docs/designs/spec-chatops-gateway.md`, "The Teams adapter".
- **Inbound receiver (Shipped, off by default)**: with `spec.integration.teams.inbound.enabled: true` and `tenantId` set, the operator runs a small pod (`<agent>-teams-inbound`) that accepts the HTTPS requests Bot Framework delivers for the bot. It verifies Microsoft's signed token on every request (the keys named by Bot Framework's OpenID configuration, RS256, issuer `https://api.botframework.com`, the app id as audience, expiry, the `serviceurl` claim against the activity), pins the tenant, bounds the body, and posts the activity to the broker's `POST /v1/chat/teams/events` under a broker role that reaches that one route. It holds no credential of the install's own. The operator renders its Service and NetworkPolicy and no Ingress: you expose the Service with your own Ingress or Gateway and TLS certificate and register that address as the bot's messaging endpoint (Step 6). Off, the broker's `/v1/chat/teams/events` is reachable only from inside the install, so a message typed in Teams does not reach the agent.

---

## Step 1: Register Microsoft Entra ID App & Azure Bot

1. Navigate to the [Azure Portal](https://portal.azure.com/) -> **Microsoft Entra ID** -> **App registrations**.
2. Click **New registration**:
   - **Name**: `kube-agents-kage` (or your preferred agent name).
   - **Supported account types**: Select _Accounts in this organizational directory only (Single tenant)_ or _Multitenant_.
3. Note the **Application (client) ID** and **Directory (tenant) ID**.
4. Under **Certificates & secrets**, create a new client secret and copy its value (`TEAMS_APP_PASSWORD`).
5. Create an **Azure Bot** resource in the Azure Portal:
   - Bot handle: `kube-agents-bot`
   - App Type: _Single Tenant_ or _MultiTenant_. If you set `tenantId` in Step 3, choose _Single Tenant_: the broker then requests its Bot Framework token from that tenant instead of `botframework.com`, and a MultiTenant bot may get `401` responses on outbound replies.
   - Microsoft App ID: Use the client ID created above.
6. In the Azure Bot blade, navigate to **Channels** and add **Microsoft Teams**.

---

## Step 2: Configure Kubernetes Secret

Store the bot credentials in the PlatformAgent credentials secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: platform-agent-secrets
  namespace: kubeagents-system
type: Opaque
stringData:
  TEAMS_APP_ID: "00000000-0000-0000-0000-000000000000"
  TEAMS_APP_PASSWORD: "your-azure-app-client-secret"
```

The tenant lock-down is not read from this Secret. Set it with `tenantId` in Step 3, which the operator renders as `TEAMS_TENANT_ID`.

---

## Step 3: Configure PlatformAgent CR or Helm Values

### Option A: Using Helm `values.yaml`

```yaml
platformAgent:
  credentials:
    secretName: platform-agent-secrets
  integration:
    teams:
      enabled: true
      tenantId: "11111111-1111-1111-1111-111111111111"
      allowedUsers:
        - "22222222-2222-2222-2222-222222222222" # Entra ID Object ID (from.aadObjectId)
      allowAllUsers: false
      homeChannel: "19:abc123conversationid@thread.tacv2" # a channel; a group chat ends in @thread.v2, a personal chat starts with a:
      adaptiveCards: true
```

### Option B: Custom Resource Manifest

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platform-agent
  namespace: kubeagents-system
spec:
  credentialsSecretRef:
    name: platform-agent-secrets
  integration:
    teams:
      enabled: true
      appIdSecretRef:
        name: platform-agent-secrets
        key: TEAMS_APP_ID
      appPasswordSecretRef:
        name: platform-agent-secrets
        key: TEAMS_APP_PASSWORD
      tenantId: "11111111-1111-1111-1111-111111111111"
      allowedUsers:
        - "22222222-2222-2222-2222-222222222222"
      allowAllUsers: false
      homeChannel: "19:teamchannelid@thread.tacv2"
      adaptiveCards: true
```

---

## Step 4: Teams App Manifest Template

A reference Microsoft Teams app manifest template is included at [`docs/samples/teams-manifest.json`](../samples/teams-manifest.json):

1. Replace `${TEAMS_APP_ID}` with your Microsoft Application (Client) ID.
2. Package `manifest.json` together with your `color.png` (192x192) and `outline.png` (32x32) icons into a ZIP archive.
3. In Microsoft Teams, navigate to **Apps** -> **Manage your apps** -> **Upload an app** to install the bot into the target team or conversation so the bot is authorized to post outbound activities to that `conversation.id`.

---

## Step 5: Talk to the Bot

- **Personal chat**: every message you send to the bot reaches it; no mention is needed.
- **Channels and group chats**: Teams delivers to a bot only the messages that mention it. Type `@` and pick the bot from the suggestion list so the name becomes a mention chip; plain text that merely reads `@Kage` is not a mention, and a reply inside a thread needs its own mention too. A message without one is never sent to the bot, so nothing in the gateway or broker logs will show it. The adapter strips the bot's own mention before the agent reads the text, and a thread reply reaches the same conversation session as the post that started it.

---

## Step 6: Receive Messages from Teams (the inbound receiver)

Add `inbound.enabled: true` to the Teams block from Step 3 (or `platformAgent.integration.teams.inbound.enabled: true` in Helm values). It needs `tenantId` and the Secret references from Step 3; the CRD refuses the combination without them.

```yaml
spec:
  integration:
    teams:
      enabled: true
      appIdSecretRef: { name: platform-agent-secrets, key: TEAMS_APP_ID }
      appPasswordSecretRef:
        { name: platform-agent-secrets, key: TEAMS_APP_PASSWORD }
      tenantId: "<your tenant id>"
      inbound:
        enabled: true
```

The operator creates a Deployment, a ServiceAccount, a ClusterIP Service `<agent>-teams-inbound` on port 8100 and a NetworkPolicy that admits the public port from anywhere, the status port from the metrics collector, and lets the pod reach only the cluster's DNS, the broker and the internet on 443 (for Microsoft's signing keys). Expose the Service with your own Ingress or Gateway and a TLS certificate; Bot Framework delivers over HTTPS only. One Ingress, for the nginx class:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: kube-agents-teams-inbound
  namespace: kubeagents-system
spec:
  ingressClassName: nginx
  tls:
    - hosts: ["teams.example.com"]
      secretName: teams-inbound-tls
  rules:
    - host: teams.example.com
      http:
        paths:
          - path: /api/v1/teams/events
            pathType: Exact
            backend:
              service:
                name: platform-agent-teams-inbound
                port:
                  number: 8100
```

Then, in the Azure Bot resource, set **Configuration -> Messaging endpoint** to `https://teams.example.com/api/v1/teams/events`. A message typed to the bot in Teams now arrives at the receiver, is verified, and reaches the agent through the broker's queue; the reply comes back the way outbound messages always did. If your ingress can restrict sources, Microsoft publishes the Bot Framework address ranges under the `AzureBotService` service tag.

What the receiver refuses, and with which status: no bearer or a token Microsoft did not sign for this bot (401), a request not from Microsoft Teams or not from the configured tenant (403), an oversized body (413). A request it could not check, because Microsoft's key endpoint did not answer, is a 503 and Bot Framework retries, as is a request that arrives while the install's Teams queue is full; anything but a POST is a 405, a body that is not a JSON object a 400, and a broker fault a 502. It counts requests by outcome and refusals by reason (`kubeagents_teams_inbound_requests_total`, `kubeagents_teams_inbound_refusals_total`) on its status port, which the chart scrapes with the install's other pods while the receiver is on (`<agent>-teams-inbound-monitoring`, a PodMonitoring, ServiceMonitor or VMPodScrape by the same switch as the rest). A message typed to the bot that gets no reply is a refusal to read there: `kubeagents_teams_inbound_refusals_total{reason="token"}` rising means the app id Secret is not the bot's (Microsoft signed for another app), `reason="tenant"` means `tenantId` is not the sender's tenant, `reason="no_bearer"` means something other than Bot Framework is posting to the endpoint. The receiver stays Ready through all of these; it logs one warning per refusal reason at most once a minute, with the count it held back since the last, so the counter is the signal and the log names the reason.

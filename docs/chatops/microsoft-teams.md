# Microsoft Teams ChatOps Integration Guide

This guide covers configuring the **Microsoft Teams Bot Framework relay** (`spec.integration.teams`) for the `kube-agents` PlatformAgent operator and gateway.

---

## Overview & Current Scope

The Microsoft Teams integration connects the gateway (`agents/platform/scripts/teams_relay_patch.py` and `agents/chat/plugins/platforms/teams/`) to the credential broker (`TeamsRelay` in `agents/platform/scripts/credential_proxy.py`).

### What Ships Today vs. Current Limitations

- **Outbound Bot Framework Relay (Shipped)**: The `credential-proxy` sidecar holds `TEAMS_APP_ID` and `TEAMS_APP_PASSWORD`, exchanges them for Microsoft Entra OAuth 2.0 `client_credentials` tokens (`https://api.botframework.com/.default`), caches the token in memory, and forwards outbound activity posts through `POST /v1/chat/teams/api`.
- **Outbound Egress & Method Guards (Shipped)**: `/v1/chat/teams/api` permits only `POST v3/conversations/<id>/activities` (and activity reply subpaths, excluding `/activities/history`) to official Bot Framework hosts (`smba.trafficmanager.net` and `*.botframework.com`). Service URLs containing query strings (`?`) or fragments (`#`) are rejected, and HTTP `3xx` redirects are refused so the bot's Bearer token is never forwarded off-host.
- **Microsoft Adaptive Cards v1.5 (Shipped)**: Outbound messages convert Markdown headings, bullet lists, and fenced code blocks into Adaptive Card v1.5 attachments when `adaptiveCards: true` (default), or send plain text when `adaptiveCards: false`.
- **In-Pod Event Queue & Authorization Scaffolding (Shipped)**: The credential broker exposes `/v1/chat/teams/events`, `/v1/chat/teams/events/ack`, and `/v1/chat/teams/events/nack`, both the broker and `TeamsRelayAdapter` enforce single-tenant isolation (`TEAMS_TENANT_ID` against `channelData.tenant.id` or `conversation.tenantId`), and only `TeamsRelayAdapter` in the gateway enforces the user allowlist (`TEAMS_ALLOWED_USERS` / `TEAMS_ALLOW_ALL_USERS`), so the broker queues an activity from any sender in the tenant. List users by Entra ID Object ID (`from.aadObjectId`) or Bot Framework user ID (`from.id`), the identifiers an inbound Teams activity carries. The adapter also compares `from.userPrincipalName` / `from.email`, but a Bot Framework activity does not carry those fields and nothing in the harness adds them, so a UPN or email entry matches no one. Display names are never matched.
- **External Inbound Webhook Ingress (Not Yet Shipped)**: The operator does not yet deploy an in-cluster Bot Framework JWT-verifying webhook receiver, and the credential broker's `CREDENTIAL_PROXY_ALLOWED_CALLERS` allowlist and `NetworkPolicy` (`reconcileCredentialProxyNetworkPolicy`) admit `TCP/8765` connections only from the gateway pod (`CALLER_ROLE_CHAT`) and shell-sandbox pod (`CALLER_ROLE_SANDBOX`). Because no external forwarder ServiceAccount or NetworkPolicy ingress rule is provisioned yet, `/v1/chat/teams/events` is currently reachable only from inside the gateway pod. In addition, proactive alerts and scheduled cron reports are delivered only to Google Chat and Slack.

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
  name: platform-agent-credentials
  namespace: kube-agents-system
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
    secretName: platform-agent-credentials
  integration:
    teams:
      enabled: true
      tenantId: "11111111-1111-1111-1111-111111111111"
      allowedUsers:
        - "22222222-2222-2222-2222-222222222222" # Entra ID Object ID (from.aadObjectId)
      allowAllUsers: false
      homeChannel: "19:abc123conversationid@thread.v2"
      adaptiveCards: true
```

### Option B: Custom Resource Manifest

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platform-agent
  namespace: kube-agents-system
spec:
  credentialsSecretRef:
    name: platform-agent-credentials
  integration:
    teams:
      enabled: true
      appIdSecretRef:
        name: platform-agent-credentials
        key: TEAMS_APP_ID
      appPasswordSecretRef:
        name: platform-agent-credentials
        key: TEAMS_APP_PASSWORD
      tenantId: "11111111-1111-1111-1111-111111111111"
      allowedUsers:
        - "22222222-2222-2222-2222-222222222222"
      allowAllUsers: false
      homeChannel: "19:teamchannelid@thread.v2"
      adaptiveCards: true
```

---

## Step 4: Teams App Manifest Template

A reference Microsoft Teams app manifest template is included at [`docs/samples/teams-manifest.json`](../samples/teams-manifest.json):

1. Replace `${TEAMS_APP_ID}` with your Microsoft Application (Client) ID.
2. Package `manifest.json` together with your `color.png` (192x192) and `outline.png` (32x32) icons into a ZIP archive.
3. In Microsoft Teams, navigate to **Apps** -> **Manage your apps** -> **Upload an app** to install the bot into the target team or conversation so the bot is authorized to post outbound activities to that `conversation.id`.

# Microsoft Teams ChatOps Integration Guide

This guide walks through configuring and deploying the **Microsoft Teams ChatOps** integration for the `kube-agents` PlatformAgent operator and gateway.

---

## Overview

The Microsoft Teams integration connects PlatformAgents to Microsoft Teams channels, group chats, and direct messages through the credential broker (`TeamsRelay` in `agents/platform/scripts/credential_proxy.py`) and the gateway runtime relay patch (`agents/platform/scripts/teams_relay_patch.py`).

### Architecture & Security Boundaries

- **Credential Isolation**: The `platform-agent` container runs without Microsoft Entra client secrets. The `credential-proxy` broker holds `TEAMS_APP_ID` and `TEAMS_APP_PASSWORD`, acquires and caches Microsoft Entra OAuth2 tokens (`https://api.botframework.com/.default`), and exposes `/v1/chat/teams/*` to callers holding a `kubeagents-credential-proxy-chat` ServiceAccount token (`CALLER_ROLE_CHAT`).
- **Outbound Egress & Method Guards**: `/v1/chat/teams/api` permits only `POST v3/conversations/<id>/activities` to official Bot Framework hosts (`smba.trafficmanager.net` and `*.botframework.com`) with HTTP redirects disabled, preventing Bearer token forwarding to arbitrary `*.trafficmanager.net` or tenant-owned hosts.
- **Inbound Webhook Ingress Requirement**: The operator does not provision a public `Ingress` or `LoadBalancer` for the credential broker, and `/v1/chat/teams/events` requires a Kubernetes `ServiceAccount` token (`CALLER_ROLE_CHAT`). Outbound notifications work directly from the gateway through the broker; receiving inbound Bot Framework webhook deliveries from Microsoft requires an external webhook forwarder or ingress proxy that validates the Bot Framework JWT and forwards the activity with a chat-role ServiceAccount token to `POST /v1/chat/teams/events` on the credential broker.
- **Single-Tenant Lock-Down**: When `TEAMS_TENANT_ID` (`spec.integration.teams.tenantId`) is configured, both the credential broker (`POST /v1/chat/teams/events`, returning `403 Forbidden`) and the gateway adapter (`_process_activity`) verify that `channelData.tenant.id` (falling back to `conversation.tenantId`) matches the configured tenant ID.
- **User Authorization (`TEAMS_ALLOWED_USERS` / `TEAMS_ALLOW_ALL_USERS`)**: The operator wires `spec.integration.teams.allowedUsers` to `TEAMS_ALLOWED_USERS` and `spec.integration.teams.allowAllUsers` to `TEAMS_ALLOW_ALL_USERS`. Authorization matches `from.aadObjectId` (Entra ID Object ID), `from.userPrincipalName` / `from.email` (when supplied by the forwarder or activity), or `from.id` (Teams `29:…` channel account ID). Display names (`from.name`) are never matched for authorization.
- **Microsoft Adaptive Cards v1.5**: Outbound messages format Markdown headers, lists, and fenced code blocks into Adaptive Card v1.5 attachments when `adaptiveCards: true` (default), or plain text when `adaptiveCards: false`.

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
   - App Type: _Single Tenant_ or _MultiTenant_
   - Microsoft App ID: Use the client ID created above.
   - **Messaging endpoint**: Point to your external Bot Framework JWT-validating webhook forwarder, which forwards verified activities to `http://<agent>-credential-proxy.<namespace>.svc.cluster.local:8765/v1/chat/teams/events` using a projected `kubeagents-credential-proxy-chat` ServiceAccount token.
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
  TEAMS_TENANT_ID: "11111111-1111-1111-1111-111111111111" # Optional single-tenant lock-down
```

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
        - "alice@example.com"
      allowAllUsers: false
      homeChannel: "19:abc123conversationid@thread.v2"
      serviceUrl: "https://smba.trafficmanager.net/teams/"
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
      serviceUrl: "https://smba.trafficmanager.net/teams/"
      adaptiveCards: true
```

---

## Step 4: Create and Sideload Teams App Manifest

1. Download the template from [`docs/samples/teams-manifest.json`](../samples/teams-manifest.json).
2. Replace `${TEAMS_APP_ID}` with your Microsoft Application (Client) ID.
3. Create a ZIP package containing:
   - `manifest.json`
   - `color.png` (192x192 icon)
   - `outline.png` (32x32 transparent icon)
4. In Microsoft Teams, navigate to **Apps** -> **Manage your apps** -> **Upload an app** (or use Microsoft Teams Developer Portal).
5. Install the app to your team or start a direct conversation with `@Kage`.

---

## Interactive ChatOps Commands

Once installed, you can interact with the agent:

- `@Kage /audit fleet` — Trigger a fleet-wide cgroup and GCE/GKE configuration audit.
- `@Kage /remediate <finding-id>` — Review and execute automated remediation workflows with Interactive Adaptive Cards.
- `@Kage status` — Check agent health, cluster connectivity, and queue state.
- `@Kage help` — List available toolsets and ChatOps capabilities.

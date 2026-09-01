# Microsoft Teams ChatOps Integration Guide

This guide walks through configuring and deploying the **Microsoft Teams ChatOps** integration for the `kube-agents` PlatformAgent operator and gateway.

---

## Overview

The Microsoft Teams integration allows cluster operators, SREs, and developers to interact with PlatformAgents directly from Microsoft Teams channels, group chats, or direct messages.

### Architecture Highlights

- **Deterministic Gateway & Credential Isolation**: The agent harness runs in an isolated container without ambient Microsoft credentials. The `credential-proxy` sidecar handles Microsoft Entra ID token acquisition, OAuth lifecycle caching, and webhook activity queues.
- **Single-Tenant Enterprise Lock-Down**: Optional verification of `TEAMS_TENANT_ID` ensures incoming activities are strictly rejected if they originate from outside your organization's Microsoft Entra ID tenant.
- **User Authorization**: Granular allowlisting via `allowedUsers` (supporting Entra ID Object IDs and UserPrincipalName emails) or tenant-wide access via `allowAllUsers: true`.
- **Microsoft Adaptive Cards v1.5**: Rich interactive card responses with action buttons (e.g. `/remediate`, `/audit`, status checks) and automatic fallback to Markdown for standard text.

---

## Step 1: Register Microsoft Entra ID App & Azure Bot

1. Navigate to the [Azure Portal](https://portal.azure.com/) -> **Azure Active Directory (Entra ID)** -> **App registrations**.
2. Click **New registration**:
   - **Name**: `kube-agents-kage` (or your preferred agent name).
   - **Supported account types**: Select _Accounts in this organizational directory only (Single tenant)_ or _Multitenant_.
3. Note the **Application (client) ID** and **Directory (tenant) ID**.
4. Under **Certificates & secrets**, create a new client secret and copy its value (`TEAMS_APP_PASSWORD`).
5. Create an **Azure Bot** resource in the Azure Portal:
   - Bot handle: `kube-agents-bot`
   - App Type: _Single Tenant_ or _MultiTenant_
   - Microsoft App ID: Use the client ID created above.
   - **Messaging endpoint**: `https://<YOUR_INGRESS_DOMAIN>/api/v1/teams/events`
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
        - "alice@example.com"
        - "bob@example.com"
        - "22222222-2222-2222-2222-222222222222"
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
        - "admin@example.com"
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

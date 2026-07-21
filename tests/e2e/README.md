# Google Chat Agent End-to-End (E2E) Test Suite

This directory contains the automated E2E test suite for verifying the **Hermes Platform Agent** integration with Google Chat.

## 📌 Architecture & Design Concept

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              pytest runner                              │
└───────┬─────────────────────────────────┬───────────────────────▲───────┘
        │ 1. Post prompt message          │ 2. Publish Chat Event │
        │    (Service Account WIF)        │    with Thread ID     │ 5. Polls & asserts
        │                                 │    (Service Account)  │    response (OTA User)
        ▼                                 ▼                       │
┌──────────────────────┐      ┌──────────────────────┐            │
│  Google Chat API     │      │    Pub/Sub Topic:    │            │
│(spaces.messages.create)     │ platform-agent-events│            │
└───────┬──────────────┘      └───────────┬──────────┘            │
        │                                 │ 3. Pull Event         │
        │ 1b. Returns Thread ID           ▼                       │
        │                     ┌──────────────────────┐            │
        │                     │ Hermes Agent (GKE)   │            │
        │                     └───────────┬──────────┘            │
        │                                 │ 4. Post Reply         │
        ▼                                 ▼                       │
┌─────────────────────────────────────────────────────────────────┴───────┐
│                            Google Chat Space                            │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1. Hybrid Auth Model & Google Chat API Restrictions

- **Service Account (WIF Keyless Authentication)**:
  Used to post the initial thread message (`spaces.messages.create`) and trigger Pub/Sub (`pubsub.projects.topics.publish`).
- **Owned Test Account (OTA User Credentials)**:
  Used to poll and read space messages (`spaces.messages.list`).
  _Why OTA User Credentials?_ By Google Chat API security policy, Service Accounts using `chat.bot` or `chat.messages.readonly` are **strictly forbidden** from calling `spaces.messages.list` (returns HTTP 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT`). Using a dedicated OTA (Owned Test Account) user credential enables 100% automated message verification in CI without using personal `@google.com` developer accounts.

### 2. Why Hybrid Pub/Sub Triggering is Required

- **Google Chat API Event Suppression**: Google Chat API does not generate Pub/Sub interaction events for messages posted programmatically via `spaces.messages.create` to prevent infinite bot loops.
- **Hybrid Test Flow**:
  1. **Step 1**: Test runner posts a prompt via Service Account WIF to establish a real Google Chat Space Thread ID (`spaces/{SPACE_ID}/threads/{THREAD_ID}`).
  2. **Step 2**: Test runner constructs a valid Google Chat event payload referencing the real Thread ID and authorized test identity (`TEST_USER_EMAIL`), publishing it directly to Pub/Sub topic `platform-agent-chat-events`.
  3. **Step 3**: **Hermes Agent** in GKE receives the Pub/Sub event, computes a well-known, predictable answer that can be validated deterministically, and posts the reply into the real space thread.
  4. **Step 4**: Test runner polls the thread via `poll_chat_service` using the OTA User credentials (`chat.messages.readonly`) and asserts the expected response.

---

## 🛠️ Complete GCP Project & CI Setup Checklist

To configure a new or existing GCP project for running this E2E test suite in CI/CD, complete the following 4 setup sections:

### 1. Enable Required GCP APIs

Ensure the following APIs are enabled in your target GCP project:

```bash
gcloud services enable \
  chat.googleapis.com \
  pubsub.googleapis.com \
  iamcredentials.googleapis.com \
  --project="<GCP_PROJECT_ID>"
```

### 2. Provision CI Service Account & Workload Identity Federation (WIF)

The test runner requires a CI Service Account (`github-actions-e2e@<GCP_PROJECT_ID>.iam.gserviceaccount.com`) with keyless Workload Identity Federation (WIF) access.

Run the automated IAM provisioning script:

```bash
./tests/e2e/scripts/provision_ci_iam.sh \
  --gcp_project "<GCP_PROJECT_ID>" \
  --git_project "<GITHUB_OWNER>/<REPOSITORY_NAME>"
```

_What this script provisions:_

- Service Account: `github-actions-e2e@<GCP_PROJECT_ID>.iam.gserviceaccount.com`
- IAM Roles: `roles/pubsub.publisher` on topic `platform-agent-chat-events` + `roles/chat.admin`
- WIF Pool & Provider: `github-actions-pool` / `github-provider` bound to your GitHub repository.

_(To teardown CI IAM resources when no longer needed, run `./tests/e2e/scripts/teardown_ci_iam.sh --gcp_project <GCP_PROJECT_ID> --git_project <GITHUB_OWNER/REPO>`)._

### 3. Setup Owned Test Account (OTA) & Google Chat Space

1. **Create OTA Account in Rhea**: Open **[http://rhea.corp.google.com](http://rhea.corp.google.com)** (`go/rhea`), create an OTA user account (e.g. `kube-agents-e2e-verifier@gmail.com`) owned by your team's MDB group.
2. **Activate Google Chat**: In Rhea, click **Credentials ➔ Get Login URL**, open in Chrome Incognito, and navigate to **`https://chat.google.com`** to accept initial setup.
3. **Add OTA to Space**: Open your target Google Chat Space (`CHAT_SPACE_ID`), ensure _external members are allowed_, and add your OTA email as a member.

### 4. Setup OAuth Consent Screen & Client ID in GCP Console

1. **Configure OAuth Consent Screen**:
   - Go to **[https://pantheon.corp.google.com/apis/credentials/consent?project=<GCP_PROJECT_ID>](https://pantheon.corp.google.com/apis/credentials/consent)**
   - Select **External** User Type.
   - Enter App Name (`E2E Chat Verifier`) and Support/Developer Email (`@google.com`).
   - In **Test Users**, click **+ ADD USERS** and add your OTA email (`kube-agents-e2e-verifier@gmail.com`).
2. **Create OAuth Client ID**:
   - Go to **[https://pantheon.corp.google.com/apis/credentials?project=<GCP_PROJECT_ID>](https://pantheon.corp.google.com/apis/credentials)**
   - Click **+ CREATE CREDENTIALS** ➔ **OAuth client ID**.
   - Select Application type: **Desktop app**.
   - Name: `e2e-chat-verifier`.
   - Copy the generated **Client ID** and **Client Secret**.

### 5. Generate Refresh Token & Configure GitHub Secrets

1. Run the standalone token generator:
   ```bash
   CLIENT_ID="<your_client_id>" CLIENT_SECRET="<your_client_secret>" python3 tests/e2e/scripts/generate_token.py
   ```
2. Paste the URL into Chrome Incognito (logged in as OTA), click **Allow**, copy the authorization code, and paste it back into the terminal.
3. Save the 4 credentials as **GitHub Repository Secrets** (**Settings ➔ Secrets and variables ➔ Actions**):
   - `E2E_CHAT_CLIENT_ID`
   - `E2E_CHAT_CLIENT_SECRET`
   - `E2E_CHAT_REFRESH_TOKEN`
   - `E2E_CHAT_SPACE_ID`

---

## 💻 Local Setup & Execution Guide

### Step 1: Install Dependencies & Set Up Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r tests/e2e/requirements.txt
```

### Step 2: Authenticate GCP ADC for Local Execution

> **Note on Local Runs**: When running locally without `E2E_CHAT_*` environment variables, the test runner automatically falls back to your personal `gcloud` ADC credentials. You can run the test locally against any GCP project and target Google Chat Space without requiring OTA user credentials!

```bash
source k8s-operator/scripts/vars.sh
export CHAT_SPACE_ID="spaces/XXXXXXXXX"

gcloud auth application-default set-quota-project "$PROJECT_ID"
gcloud auth application-default login --scopes="https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/chat.messages.create,https://www.googleapis.com/auth/chat.messages.readonly,https://www.googleapis.com/auth/pubsub"
```

### Step 3: Run pytest

```bash
pytest tests/e2e/gchat_agent_test.py -v -s
```

---

## 🤖 Running in GitHub Actions (CI)

The workflow file [`.github/workflows/e2e-gchat-test.yml`](file:///.github/workflows/e2e-gchat-test.yml) is triggered manually via `workflow_dispatch` (or via GitHub CLI / Web UI).

### Triggering Workflow via GitHub CLI (`gh`):

```bash
gh workflow run .github/workflows/e2e-gchat-test.yml \
  -f gcp_project_id="kube-agents-autopush"
```

> **Note**: If `chat_space_id` is omitted in `gh workflow run`, the workflow automatically uses the `E2E_CHAT_SPACE_ID` GitHub Repository Secret.

### Authentication in CI:

1. **Service Account via WIF**: Authenticates `github-actions-e2e@kube-agents-autopush.iam.gserviceaccount.com` for keyless message creation and Pub/Sub publishing.
2. **OTA User Credentials & Target Space via Secrets**: Uses `E2E_CHAT_REFRESH_TOKEN`, `E2E_CHAT_CLIENT_ID`, `E2E_CHAT_CLIENT_SECRET`, and `E2E_CHAT_SPACE_ID` secrets for space resolution and response polling.

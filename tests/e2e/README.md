# Google Chat Agent End-to-End (E2E) Test Suite

This directory contains the automated E2E test suite for verifying the **Hermes Platform Agent** integration with Google Chat.

## 📌 Architecture & Design Concept

```
┌─────────────────┐       1. Post Clean Prompt      ┌──────────────────────┐
│  pytest runner  │────────────────────────────────>│ Google Chat Space API│
└────────┬────────┘                                 └──────────┬───────────┘
         │                                                     │
         │ 2. Publish Chat Event w/ Thread ID                  │ 1b. Returns real
         ▼                                                     │     Thread ID
┌─────────────────────────────────┐                            │
│ Pub/Sub Topic:                  │                            │
│ platform-agent-chat-events      │                            │
└────────┬────────────────────────┘                            │
         │ 3. Pull Event                                       │
         ▼                                                     │
┌─────────────────────────────────┐                            │
│  Hermes Agent (GKE Pod)         │                            │
└────────┬────────────────────────┘                            │
         │ 4. Execute Prompt & Post Reply                      │
         └─────────────────────────────────────────────────────┼──────────────┐
                                                                ▼              │
                                                    ┌────────────────────┐    │
                                                    │ Google Chat Space  │◄───┘
                                                    └──────────┬─────────┘
                                                               │ 5. Polls & asserts
                                                               ▼    response "5"
                                                    ┌────────────────────┐
                                                    │  pytest runner     │
                                                    └──────────┬─────────┘
```

### Why Hybrid Pub/Sub Triggering is Required (Google Chat API Limitation)

1. **Google Chat API Event Suppression**:
   Google Chat API strictly **does not generate Pub/Sub interaction events** for messages posted via the `spaces.messages.create` REST API. This behavior is designed by Google Cloud to prevent recursive looping where bots trigger themselves.

2. **Mention Tagging Limitations in REST API**:
   When posting via REST API using User OAuth credentials, `<users/ID>` tags do not run browser autocomplete. Google Chat replaces them with `@` anchors or strips them, which does not emit app notifications.

3. **Our E2E Hybrid Solution**:
   - **Step 1**: The test runner posts a clean prompt (`what is 2 + 3? [test_id: e2e-xxx]`) to the target Google Chat Space to establish a real Google Chat Space Thread ID (`spaces/{SPACE_ID}/threads/{THREAD_ID}`).
   - **Step 2**: The test runner constructs a valid Google Chat event payload containing an authorized test runner identity (`github-actions-e2e@<gcp_project_id>.iam.gserviceaccount.com` or user email) and the real Thread ID, and publishes it directly to Pub/Sub topic `platform-agent-chat-events`.
   - **Step 3**: The **Hermes Agent** running in GKE receives the event from Pub/Sub, validates sender authorization (`ALLOWED_USERS`), computes `"2 + 3 = 5"`, and posts the response into the real space thread.
   - **Step 4**: The test runner polls the thread via Google Chat API and asserts the response contains `"5"`.

## ⚙️ Prerequisites & Infrastructure Setup

> **IMPORTANT**: The test requires an active GCP project environment where the infrastructure has already been provisioned using the main environment provisioning script ([`k8s-operator/scripts/provision.sh`](file:///usr/local/google/home/eleontev/Projects/kube-agents/k8s-operator/scripts/provision.sh)) and the Platform Agent is running in GKE.

### 1. Service Account IAM Permissions (Google Chat & Specific Pub/Sub Topic)

Any Service Account (or user identity) under which the E2E test script or CI/CD pipeline is executed must have the following least-privilege IAM permissions:

1. **Pub/Sub Publishing (Topic-Specific)**: Role `roles/pubsub.publisher` bound directly on topic `platform-agent-chat-events`.
2. **Google Chat API Access**: Role `roles/chat.admin` (or `roles/chat.import`) to post messages to Chat Spaces via API.

> **Automated Provisioning & Teardown**: You can automatically provision this Service Account and Workload Identity Federation (WIF) bindings by running `./tests/e2e/scripts/provision_ci_iam.sh --gcp_project <GCP_PROJECT_ID> --git_project <GITHUB_OWNER/REPO>`. To tear down these CI resources when no longer needed, run `./tests/e2e/scripts/teardown_ci_iam.sh --gcp_project <GCP_PROJECT_ID> --git_project <GITHUB_OWNER/REPO>`.

### 2. Google Chat Setup & Authorization Requirements

Before running tests, ensure your Google Chat environment and Platform Agent are configured with the following requirements:

1. **Installed App in Target Space**: The target Google Chat Space (`CHAT_SPACE_ID`) must be a valid Space where the Platform Agent testing app is installed / added as a member.
2. **Space Permissions**: The user account or CI runner identity executing the test must have access to post messages to the target Google Chat Space.
3. **`ALLOWED_USERS` Authorization**: If the Platform Agent is configured with user access restrictions (i.e. the `ALLOWED_USERS` / `allowedUsers` list is non-empty), the testing user email or CI Service Account email (e.g. `github-actions-e2e@<gcp_project_id>.iam.gserviceaccount.com`) must be included in the allowed users list.

## 🛠️ Complete Step-by-Step Local Setup & Execution Guide

### Step 1: Install System Prerequisites

Ensure the following tools are installed on your workstation:

- **Google Cloud SDK (`gcloud` CLI)**: [Installation Guide](https://cloud.google.com/sdk/docs/install)
- **Python 3.10+** and `pip`

### Step 2: Set Up Python Virtual Environment

Navigate to the repository root directory and set up the virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r tests/e2e/requirements.txt
```

### Step 3: Verify & Source SRE Environment Variables

Source the environment variables script and set your target `CHAT_SPACE_ID`:

```bash
source k8s-operator/scripts/vars.sh
export CHAT_SPACE_ID="spaces/XXXXXXXXX"
```

Verify that key variables are set:

```bash
echo "Project ID: $PROJECT_ID"
echo "Chat Space ID: $CHAT_SPACE_ID"
```

### Step 4: Configure GCP Application Default Credentials (ADC)

Set your active GCP quota project and authenticate your user credentials with required scopes:

```bash
# Set Quota Project
gcloud auth application-default set-quota-project "$PROJECT_ID"

# Authenticate ADC with required Chat & Pub/Sub Scopes
gcloud auth application-default login --scopes="https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/chat.messages.create,https://www.googleapis.com/auth/chat.messages.readonly,https://www.googleapis.com/auth/chat.memberships.readonly,https://www.googleapis.com/auth/pubsub"
```

### Step 5: Execute the E2E Test

Run `pytest` to execute the end-to-end test suite:

```bash
pytest tests/e2e/gchat_agent_test.py -v -s
```

## 🤖 Running in GitHub Actions (CI)

The workflow file [`.github/workflows/e2e-gchat-test.yml`](file:///.github/workflows/e2e-gchat-test.yml) allows manual execution via `workflow_dispatch` on GCP project `kube-agents-autopush` (or any target GCP project).

### Triggering Workflow via GitHub CLI (`gh`):

```bash
gh workflow run .github/workflows/e2e-gchat-test.yml \
  -f gcp_project_id="kube-agents-autopush" \
  -f chat_space_id="spaces/XXXXXXXXX"
```

### Triggering Workflow via GitHub Web UI:

1. Go to **Actions** -> **Google Chat Agent E2E Test**.
2. Click **Run workflow**.
3. Fill in `gcp_project_id` (default: `kube-agents-autopush`) and `chat_space_id` (`spaces/XXXXXXXXX`).
4. Click **Run workflow**.

### Authentication & Secrets:

CI authentication supports both **Workload Identity Federation (WIF)** and **Service Account Keys**:

- **Required Workflow Inputs**:
  - `gcp_project_id`: Target GCP Project ID (default: `kube-agents-autopush`).
  - `chat_space_id`: Target Google Chat Space ID (e.g. `spaces/XXXXXXXXX`). **Note**: The Platform Agent app must be installed / added to this Chat Space.
  - `chat_topic_name`: Google Chat Pub/Sub Topic Name (default: `platform-agent-chat-events`).
  - `test_user_email`: Authorized Test Identity Email (defaults to `github-actions-e2e@<gcp_project_id>.iam.gserviceaccount.com`).
  - `timeout_seconds`: Optional test timeout in seconds (default: `120`).

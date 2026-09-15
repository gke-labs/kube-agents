#!/usr/bin/env python3
"""
Google Chat Agent E2E Test Suite.

Sends a prompt message to a target Google Chat Space, triggers the Hermes Agent
Pub/Sub event handler referencing the real space thread and authorized test identity,
polls the Google Chat API for the agent's response, and asserts mathematical correctness.
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import google.auth
    from google.auth.credentials import Credentials
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials as UserCredentials
    from googleapiclient.discovery import Resource, build
    from googleapiclient.errors import HttpError
    HAS_GOOGLE_LIBS = True
except ImportError:
    HAS_GOOGLE_LIBS = False

    class RefreshError(Exception):  # type: ignore[no-redef]
        """Stand-in so the module imports; the credentials fixture fails before it is used."""

    Credentials = Any  # type: ignore
    UserCredentials = Any  # type: ignore
    Resource = Any  # type: ignore
    HttpError = Exception  # type: ignore
import pytest

try:
    # pytest puts tests/e2e on sys.path when it collects this file.
    from gchat_poller import ChatMessagePoller, ChatReadAccessDenied, OTA_ENV_VARS, describe_credential
except ImportError:  # imported as tests.e2e.gchat_agent_test from the repository root
    from tests.e2e.gchat_poller import ChatMessagePoller, ChatReadAccessDenied, OTA_ENV_VARS, describe_credential

# Configuration from Environment Variables (read dynamically from tests/e2e/.env or the CI environment)
GCP_PROJECT_ID: Optional[str] = os.environ.get("GCP_PROJECT_ID") or os.environ.get("PROJECT_ID")
CHAT_SPACE_ID: Optional[str] = os.environ.get("CHAT_SPACE_ID")
CHAT_TOPIC_NAME: str = os.environ.get("CHAT_TOPIC_NAME", "platform-agent-chat-events")

# Test Identity Resolution (Defaults to CI Service Account email if GCP_PROJECT_ID is set)
DEFAULT_SA_EMAIL: str = f"github-actions-e2e@{GCP_PROJECT_ID}.iam.gserviceaccount.com" if GCP_PROJECT_ID else "e2e-runner@google.com"
USER_EMAIL_INPUT: str = os.environ.get("TEST_USER_EMAIL") or os.environ.get("ALLOWED_USERS") or DEFAULT_SA_EMAIL
TEST_USER_EMAIL: str = USER_EMAIL_INPUT.split(",")[0].strip()
if "@" not in TEST_USER_EMAIL:
    TEST_USER_EMAIL = f"{TEST_USER_EMAIL}@google.com"


TEST_USER_NAME: str = TEST_USER_EMAIL.split("@")[0]

TEST_TIMEOUT_SEC: int = int(os.environ.get("TEST_TIMEOUT_SEC", "120"))
POLL_INTERVAL_SEC: int = int(os.environ.get("POLL_INTERVAL_SEC", "5"))

# Normalize CHAT_SPACE_ID format (e.g. AAQAfrKMyng -> spaces/AAQAfrKMyng)
if CHAT_SPACE_ID and not CHAT_SPACE_ID.startswith("spaces/"):
    CHAT_SPACE_ID = f"spaces/{CHAT_SPACE_ID}"

SCOPES: list[str] = [
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/pubsub",
    "https://www.googleapis.com/auth/cloud-platform",
]

# Reading the space back as the service account is app authentication, which needs the
# chat.app.* scope and a one-time Google Workspace administrator approval of it; without the
# approval spaces.messages.list answers 403 and the poller falls back to the OTA user credential.
# The poller gets its own credential carrying only this scope, so the token that posts the prompt
# and publishes the event (SCOPES above) is the one that worked before the scope existed.
CHAT_APP_MESSAGES_READONLY_SCOPE: str = "https://www.googleapis.com/auth/chat.app.messages.readonly"
POLL_SCOPES: list[str] = [CHAT_APP_MESSAGES_READONLY_SCOPE]
CHAT_USER_MESSAGES_READONLY_SCOPE: str = "https://www.googleapis.com/auth/chat.messages.readonly"
OAUTH_TOKEN_URI: str = "https://oauth2.googleapis.com/token"
POLL_PAGE_SIZE: int = 50
POLL_ORDER_BY: str = "createTime desc"


GOOGLE_LIBS_MISSING: str = "google-api-python-client or google-auth not installed; Google Chat E2E test requires Google client libraries."


@pytest.fixture(scope="module")
def credentials() -> Any:
    """Returns GCP credentials authenticated with required Chat and Pub/Sub scopes."""
    if not HAS_GOOGLE_LIBS:
        pytest.fail(GOOGLE_LIBS_MISSING)
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds


@pytest.fixture(scope="module")
def chat_service(credentials: Credentials) -> Resource:
    """Builds authenticated Google Chat API service for creating prompt messages using Service Account WIF."""
    if not CHAT_SPACE_ID:
        pytest.fail(
            "CHAT_SPACE_ID environment variable is required (e.g., spaces/AAQAfrKMyng)\n"
            "Tip: the install's coordinates can be loaded with "
            "'set -a; . install.env; set +a'"
        )

    return build("chat", "v1", credentials=credentials)


@pytest.fixture(scope="module")
def chat_poller() -> ChatMessagePoller:
    """Builds the poller that reads space messages: the runner's own credentials first (service
    account app auth in CI), the OTA user refresh token second when its three variables are set.
    The poller's own credential carries POLL_SCOPES rather than SCOPES.
    """
    if not HAS_GOOGLE_LIBS:
        pytest.fail(GOOGLE_LIBS_MISSING)
    poll_creds, _ = google.auth.default(scopes=POLL_SCOPES)
    refresh_token, client_id, client_secret = (os.environ.get(name) for name in OTA_ENV_VARS)

    fallback: Optional[Resource] = None
    if refresh_token or client_id or client_secret:
        if not (refresh_token and client_id and client_secret):
            pytest.fail(
                "Incomplete OTA credentials configuration. "
                f"Please ensure {', '.join(OTA_ENV_VARS)} are all set."
            )
        user_creds = UserCredentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri=OAUTH_TOKEN_URI,
            scopes=[CHAT_USER_MESSAGES_READONLY_SCOPE],
        )
        fallback = build("chat", "v1", credentials=user_creds)

    return ChatMessagePoller(
        build("chat", "v1", credentials=poll_creds),
        fallback,
        primary_credential=describe_credential(poll_creds),
        # A refused token mint (a scope IAM will not grant, an OTA refresh token that no longer
        # exchanges) is a denial; google-auth marks the token endpoint's 5xx as retryable and the
        # poller lets those propagate.
        denial_types=(RefreshError,),
    )


@pytest.fixture(scope="module")
def pubsub_service(credentials: Credentials) -> Resource:
    """Builds authenticated Pub/Sub API service for triggering events."""
    return build("pubsub", "v1", credentials=credentials)


def test_gchat_agent_math_response(
    chat_service: Resource,
    pubsub_service: Resource,
    chat_poller: ChatMessagePoller
) -> None:
    """
    End-to-End Test for Hermes Platform Agent:
    1. Posts clean prompt message to Google Chat Space (creating a real space thread).
    2. Triggers agent via Pub/Sub event referencing the real space thread and authorized test identity.
    3. Polls space thread for agent response and asserts answer contains '5'.
    """
    if not GCP_PROJECT_ID:
        pytest.fail("GCP_PROJECT_ID environment variable is required.")
    if not CHAT_SPACE_ID:
        pytest.fail(
            "CHAT_SPACE_ID environment variable is required. "
            "Please set CHAT_SPACE_ID or configure E2E_CHAT_SPACE_ID in GitHub Repository Secrets."
        )

    timestamp_str: str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    prompt_body: str = f"[E2E Test Started at {timestamp_str}] what is 2 + 3?"

    print(f"\n[E2E Test] Target GCP Project: {GCP_PROJECT_ID}")
    print(f"[E2E Test] Target Space: {CHAT_SPACE_ID}")
    print(f"[E2E Test] Pub/Sub Topic: {CHAT_TOPIC_NAME}")
    print(f"[E2E Test] Test Identity: {TEST_USER_EMAIL}")
    print(f"[E2E Test] Chat UI Prompt: '{prompt_body}'")
    print(f"[E2E Test] Space read credential: {chat_poller.auth_label}; "
          f"OTA fallback {'configured' if chat_poller.fallback_configured else 'not configured'}")

    # Step 1: Post clean prompt message to create real Google Chat space thread
    try:
        sent_message: dict[str, Any] = chat_service.spaces().messages().create(
            parent=CHAT_SPACE_ID,
            body={"text": prompt_body}
        ).execute()
    except HttpError as err:
        pytest.fail(f"Failed to post message to Google Chat space '{CHAT_SPACE_ID}': {err}")

    message_name: str = sent_message.get("name", "")
    thread_name: str = sent_message.get("thread", {}).get("name", "")
    create_time: str = sent_message.get("createTime", "")
    print(f"[E2E Test] Created Space Message: {message_name}")
    print(f"[E2E Test] Created Space Thread: {thread_name}")

    # Step 2: Publish MESSAGE Event to Pub/Sub referencing real thread & E2E test identity
    topic_path: str = f"projects/{GCP_PROJECT_ID}/topics/{CHAT_TOPIC_NAME}"

    chat_event_payload: dict[str, Any] = {
        "type": "MESSAGE",
        "eventTime": create_time,
        "space": {
            "name": CHAT_SPACE_ID,
            "type": "SPACE"
        },
        "message": {
            "name": message_name,
            "text": prompt_body,
            "argumentText": f" {prompt_body}",
            "thread": {
                "name": thread_name
            },
            "sender": {
                "name": f"users/{TEST_USER_NAME}",
                "displayName": TEST_USER_NAME,
                "email": TEST_USER_EMAIL,
                "type": "HUMAN"
            }
        }
    }

    encoded_data: str = base64.b64encode(json.dumps(chat_event_payload).encode("utf-8")).decode("utf-8")
    pubsub_body: dict[str, Any] = {"messages": [{"data": encoded_data}]}

    try:
        pub_response: dict[str, Any] = pubsub_service.projects().topics().publish(
            topic=topic_path,
            body=pubsub_body
        ).execute()
        message_ids: list[str] = pub_response.get("messageIds", [])
        print(f"[E2E Test] Triggered Agent via Pub/Sub (Message ID: {message_ids})")
    except HttpError as err:
        pytest.fail(f"Failed to publish event to Pub/Sub topic {CHAT_TOPIC_NAME}: {err}")

    print(f"[E2E Test] Waiting for agent processing and response in thread {thread_name}...")

    # Step 3: Poll space thread for agent's response
    start_time: float = time.time()
    bot_response_found: bool = False
    received_response_text: str = ""

    try:
        while time.time() - start_time < TEST_TIMEOUT_SEC:
            time.sleep(POLL_INTERVAL_SEC)
            elapsed: int = int(time.time() - start_time)
            print(f"[E2E Test] Polling thread for bot response... ({elapsed}s / {TEST_TIMEOUT_SEC}s)")

            try:
                response: dict[str, Any] = chat_poller.list_messages(CHAT_SPACE_ID, POLL_PAGE_SIZE, POLL_ORDER_BY)

                for msg in response.get("messages", []):
                    # Only check messages posted by a BOT
                    if msg.get("sender", {}).get("type") != "BOT":
                        continue

                    msg_thread: str = msg.get("thread", {}).get("name", "")
                    msg_text: str = msg.get("text", "")

                    # Ignore system setup notifications
                    if "No home channel is set" in msg_text or "/sethome" in msg_text:
                        continue

                    if msg_thread == thread_name and re.search(r"\b5\b", msg_text):
                        received_response_text = msg_text
                        bot_response_found = True
                        print(f"\n[E2E Test SUCCESS] Received Bot Math Response: '{received_response_text}'")
                        break

            except ChatReadAccessDenied as err:
                pytest.fail(f"Cannot read the space back: {err}")
            except HttpError as err:
                print(f"[E2E Test Warning] Polling error: {err}")

            if bot_response_found:
                break
    finally:
        # The feasibility signal for #1558: which credential read the space, and why it changed.
        # Printed however the loop ends, so a failure on the fallback keeps the record.
        if chat_poller.reads:
            print(f"[E2E Test] Space messages were read with: {chat_poller.auth_label}")
        else:
            print(f"[E2E Test] No credential read the space; last attempted: {chat_poller.auth_label}")
        if chat_poller.fell_back:
            print(f"[E2E Test] App auth fell back to the OTA credential because: {chat_poller.fallback_reason}")

    # Step 4: Assertions
    assert bot_response_found, f"Timed out after {TEST_TIMEOUT_SEC}s waiting for agent response in {CHAT_SPACE_ID}"
    assert re.search(r"\b5\b", received_response_text), (
        f"Expected response to contain '5', but got: '{received_response_text}'"
    )
    print("[E2E Test SUCCESS] Agent responded correctly with '5'.")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

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

import google.auth
from google.oauth2.credentials import Credentials as UserCredentials

from google.auth.credentials import Credentials

from googleapiclient.discovery import Resource, build

from googleapiclient.errors import HttpError

import pytest

# Configuration from Environment Variables (read dynamically from vars.sh or CI environment)
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
    "https://www.googleapis.com/auth/chat.bot",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/pubsub",
    "https://www.googleapis.com/auth/cloud-platform",
]


@pytest.fixture(scope="module")
def credentials() -> Credentials:
    """Returns GCP credentials authenticated with required Chat and Pub/Sub scopes."""
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds


@pytest.fixture(scope="module")
def chat_service(credentials: Credentials) -> Resource:
    """Builds authenticated Google Chat API service for polling responses.
    Uses OTA User Refresh Token if provided in environment (for CI), otherwise falls back to standard credentials.
    """
    if not CHAT_SPACE_ID:
        pytest.fail(
            "CHAT_SPACE_ID environment variable is required (e.g., spaces/AAQAfrKMyng)\n"
            "Tip: SRE variables can be loaded by running 'source k8s-operator/scripts/vars.sh'"
        )

    refresh_token = os.environ.get("E2E_CHAT_REFRESH_TOKEN")
    client_id = os.environ.get("E2E_CHAT_CLIENT_ID")
    client_secret = os.environ.get("E2E_CHAT_CLIENT_SECRET")

    if refresh_token and client_id and client_secret:
        user_creds = UserCredentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/chat.messages.readonly"],
        )
        return build("chat", "v1", credentials=user_creds)

    return build("chat", "v1", credentials=credentials)


@pytest.fixture(scope="module")
def pubsub_service(credentials: Credentials) -> Resource:
    """Builds authenticated Pub/Sub API service for triggering events."""
    return build("pubsub", "v1", credentials=credentials)


def test_gchat_agent_math_response(chat_service: Resource, pubsub_service: Resource) -> None:
    """
    End-to-End Test for Hermes Platform Agent:
    1. Posts clean prompt message to Google Chat Space (creating a real space thread).
    2. Triggers agent via Pub/Sub event referencing the real space thread and authorized test identity.
    3. Polls space thread for agent response and asserts answer contains '5'.
    """
    if not GCP_PROJECT_ID:
        pytest.fail("PROJECT_ID environment variable is required.")

    timestamp_str: str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    prompt_body: str = f"[E2E Test Started at {timestamp_str}] what is 2 + 3?"

    print(f"\n[E2E Test] Target GCP Project: {GCP_PROJECT_ID}")
    print(f"[E2E Test] Target Space: {CHAT_SPACE_ID}")
    print(f"[E2E Test] Pub/Sub Topic: {CHAT_TOPIC_NAME}")
    print(f"[E2E Test] Test Identity: {TEST_USER_EMAIL}")
    print(f"[E2E Test] Chat UI Prompt: '{prompt_body}'")

    # Step 1: Post clean prompt message to create real Google Chat space thread
    try:
        sent_message: dict[str, Any] = chat_service.spaces().messages().create(
            parent=CHAT_SPACE_ID,
            body={"text": prompt_body}
        ).execute()
    except HttpError as err:
        pytest.fail(f"Failed to post message to Google Chat space: {err}")

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

    while time.time() - start_time < TEST_TIMEOUT_SEC:
        time.sleep(POLL_INTERVAL_SEC)
        elapsed: int = int(time.time() - start_time)
        print(f"[E2E Test] Polling thread for bot response... ({elapsed}s / {TEST_TIMEOUT_SEC}s)")

        try:
            response: dict[str, Any] = chat_service.spaces().messages().list(
                parent=CHAT_SPACE_ID,
                pageSize=50,
                orderBy="createTime desc"
            ).execute()

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

        except HttpError as err:
            print(f"[E2E Test Warning] Polling error: {err}")

        if bot_response_found:
            break

    # Step 4: Assertions
    assert bot_response_found, f"Timed out after {TEST_TIMEOUT_SEC}s waiting for agent response in {CHAT_SPACE_ID}"
    assert re.search(r"\b5\b", received_response_text), (
        f"Expected response to contain '5', but got: '{received_response_text}'"
    )
    print("[E2E Test SUCCESS] Agent responded correctly with '5'.")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

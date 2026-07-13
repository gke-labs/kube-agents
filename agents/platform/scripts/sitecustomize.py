"""Runtime patches for the platform agent image."""

import os

if os.getenv("GOOGLE_CHAT_RELAY_URL"):
    try:
        from google_chat_relay_patch import install

        install()
    except ModuleNotFoundError as exc:
        # Credential-free wrapper scripts use the system Python, which can see
        # this directory through PYTHONPATH but not Hermes' gateway packages.
        # The long-lived Hermes process uses its venv and applies the patch.
        if exc.name not in {"gateway", "plugins"}:
            raise

if os.getenv("SLACK_RELAY_URL"):
    try:
        from slack_relay_patch import install

        install()
    except ModuleNotFoundError as exc:
        if exc.name not in {"gateway", "plugins"}:
            raise

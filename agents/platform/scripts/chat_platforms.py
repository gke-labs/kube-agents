#!/usr/bin/env python3
# chat_platforms.py - Which chat platforms is this install configured to post to?
#
# One question, previously answered three different ways in three files, which is why
# the Cluster Agent reconcile summary reached Google Chat and nothing else on every
# install (#989). The two other answers are `session_kv_server.get_active_platform`
# and `platform_mcp_server.get_enabled_platforms`; both are mid-fix under #742/#743
# and #735 respectively, and both should end up here rather than a fourth copy being
# added alongside them.
#
# Deliberately dependency-free bar PyYAML. Callers include a `no_agent` cron script
# that must not pay for a FastMCP import to find out where to send one message, which
# is what importing `agent_common_server` for its CONFIG_PATH would cost.

import os
import sys

# The platforms this harness ships an egress path for, in the order a message is
# posted to them. Google Chat leads because it is where every one of these messages
# already arrives — a dual-platform install should gain Slack, not have the message
# move to it. (Contrast `get_active_platform`, which returns one platform Slack-first,
# so #855 moved the event-watcher alerts off Google Chat on 2026-08-25.)
CHAT_PLATFORMS = ("google_chat", "slack")

# The twin of agent_common_server.CONFIG_PATH — the default/chat profile's own
# config.yaml on the data PVC. Repeated rather than imported for the reason in the
# header; change both.
CONFIG_PATH = os.environ.get("PLATFORM_AGENT_CONFIG_PATH", "/opt/data/config.yaml")

# Per-platform environment signals, tried when config.yaml does not settle it. The
# relay URL leads each list: the operator sets it on this container exactly when the
# matching `spec.integration.<platform>.enabled` is true, so it answers the question
# being asked rather than approximating it (platformagent_manifests.go, the
# `integration.GoogleChat` / `integration.Slack` blocks in buildPodTemplateSpec).
#
# A bot token is NOT a signal for Slack. It is a credential, so it lives in the
# credential-proxy container and never reaches this one — asking for it here asks a
# question whose answer in a deployed pod is always "no", which is the specific defect
# #742 records and #855 fixed in the session server's copy. The home channels and
# GOOGLE_CHAT_PROJECT_ID trail the relay URLs to cover a bare `docker run` off the
# image, where no operator has rendered anything.
_ENV_SIGNALS = {
    "google_chat": ("GOOGLE_CHAT_RELAY_URL", "GOOGLE_CHAT_PROJECT_ID", "GOOGLE_CHAT_HOME_CHANNEL"),
    "slack": ("SLACK_RELAY_URL", "SLACK_HOME_CHANNEL", "SLACK_BOT_TOKEN"),
}

# Where a message goes when nothing above resolves. Preserves the behaviour every
# caller had before this module existed, so an install this function cannot read is
# no worse off than it was.
DEFAULT_PLATFORM = "google_chat"


def _config_enabled() -> dict[str, bool]:
    """`platforms.<name>.enabled` from config.yaml, for the keys that set it.

    Absent keys are absent from the result rather than False — "this file does not
    say" and "this file says no" are different answers and only the second one should
    override an environment signal.

    On an operator-managed pod this returns {} and that is the normal case, not a
    failure: `renderConfigYAML` writes `platforms.<p>.enabled` into the managed scope
    mounted read-only at /etc/hermes, which Hermes overlays per leaf key inside its
    own config loader rather than merging onto this file. The file this reads
    therefore carries a `platforms` subtree with no `enabled` key in it at all. It is
    read anyway because it is the truth on the installs that do write it — a hand-run
    container, a profile configured off the image alone.
    """
    try:
        import yaml
    except Exception:  # noqa: BLE001 - no PyYAML: fall through to the env signals
        return {}

    try:
        with open(CONFIG_PATH, "r") as fh:
            cfg = yaml.safe_load(fh) or {}
        platforms = (cfg.get("platforms") or {}) if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - unreadable/malformed: the env still answers
        print(f"[CHAT-PLATFORMS] could not read {CONFIG_PATH}: {exc}", file=sys.stderr)
        return {}

    out: dict[str, bool] = {}
    for name in CHAT_PLATFORMS:
        block = platforms.get(name)
        if isinstance(block, dict) and "enabled" in block:
            out[name] = bool(block["enabled"])
    return out


def _env_enabled(platform: str) -> bool:
    return any(os.environ.get(var, "").strip() for var in _ENV_SIGNALS.get(platform, ()))


def enabled_chat_platforms() -> list[str]:
    """Every chat platform this install posts to, in CHAT_PLATFORMS order.

    Resolved per platform rather than per source, so a config.yaml that names one
    platform does not silence another that only the environment knows about. An
    explicit `enabled: false` in config.yaml wins over an environment signal — that
    combination is somebody turning a platform off on a pod the operator still renders
    variables for, and the file is the more specific statement.

    Never returns an empty list: an install that resolves to nothing gets
    DEFAULT_PLATFORM, which is what every caller did unconditionally before.
    """
    from_config = _config_enabled()
    resolved = []
    for name in CHAT_PLATFORMS:
        enabled = from_config[name] if name in from_config else _env_enabled(name)
        if enabled:
            resolved.append(name)
    return resolved or [DEFAULT_PLATFORM]


__all__ = ["CHAT_PLATFORMS", "DEFAULT_PLATFORM", "enabled_chat_platforms"]

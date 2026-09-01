#!/usr/bin/env python3
# chat_platforms.py - Which chat platforms is this install configured to post to?
#
# One question, answered separately in several files, which is why the Cluster Agent
# reconcile summary reached Google Chat and nothing else on every install (#989). The
# other answers, and where each stands:
#
#   - `session_kv_server.enabled_chat_platforms` — the same three-source, per-platform
#     resolution as this module, landed by #1111 for the cron report relay. It is the
#     closest sibling and deliberately agrees with this one on the order and on the
#     precedence; `get_active_platform` is now a single-destination reader on top of
#     it, for the alert path, which needs exactly one platform because a thread
#     belongs to one.
#   - `platform_mcp_server.get_enabled_platforms` — still keyed on SLACK_BOT_TOKEN;
#     #742/#743 record the defect and PR #735 is open against it.
#   - `profile_cron_tick.home_target_env` / `HOME_TARGET_ENV_KEYS` — a different
#     question (which home *target* a cron child gets, re-read from config.yaml
#     because the environment cannot carry it) but the same per-platform table.
#     Any consolidation that ignores it will produce a module that cannot replace it.
#
# These should converge here rather than a further copy being added alongside them.
# #1111's docstring sets the condition for collapsing its copy onto this one: the
# managed scope has to come across with it, at higher precedence than either other
# source. It has — see MANAGED_CONFIG_PATH — so the two now differ only in which
# callers they serve.
#
# Deliberately dependency-free bar PyYAML. Callers include a `no_agent` cron script
# that must not pay for a FastMCP import to find out where to send one message, which
# is what importing `agent_common_server` for its CONFIG_PATH would cost.

import os
import sys

# The platforms this harness ships an egress path for, in the order a message is
# posted to them. Google Chat leads because it is where every one of these messages
# already arrives — a dual-platform install should gain Slack, not have the message
# move to it. #1094/#1111 settled the same order for the same reason on the sibling
# in `session_kv_server`, after #855's Slack-first ordering cost autopush a week of
# governance reports to a Slack leg with no home channel.
CHAT_PLATFORMS = ("google_chat", "slack")

# The managed scope, mounted read-only by the operator and machine-global — one
# /etc/hermes for every profile in the pod, so a cron child in the platform-agent
# container reads the same file the gateway does. This is the only file here that
# states what the CR turned on: `renderConfigYAML` declares both
# `platforms.<p>.enabled` fields with no `omitempty`, so it emits them as explicit
# booleans on every reconcile whether they are true or false. That is why it
# outranks everything below rather than joining the fallback chain — an operator
# who set `slack.enabled: false` has said so, and a stale SLACK_RELAY_URL still in
# the container environment must not re-enable the leg. Carried across from
# #1111's `session_kv_server.enabled_chat_platforms`, whose docstring asks
# whichever of the two lands second to do exactly this.
#
# HERMES_MANAGED_DIR is read the same way the operator and `managed_scope.py`
# read it. It is not a plugin-declared `messaging` variable, so it survives the
# cron child's env scrub — and where it does not, the default below is the path
# the operator mounts anyway.
MANAGED_CONFIG_PATH = os.path.join(
    os.environ.get("HERMES_MANAGED_DIR", "").strip() or "/etc/hermes", "config.yaml"
)

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
# In a deployed pod the relay URL is not merely first, it is the only one of these
# that arrives. A bot token is a credential, so it lives in the credential-proxy
# container and never reaches this one — asking for it is a question whose answer on a
# pod is always "no", which is the specific defect #742 records and #855 fixed in the
# session server's copy. And `*_HOME_CHANNEL` is stripped from every `no_agent` cron
# script's environment by Hermes' provider-env blocklist, which is why
# `profile_cron_tick.home_target_env` exists to re-read it from disk; the caller here
# is one of those children, so it never sees those either.
#
# They are listed anyway, after the relay URLs, because this module is not only for
# that caller: on a bare `docker run` off the image no operator has rendered a relay
# URL, and there a home channel or GOOGLE_CHAT_PROJECT_ID is the only signal there is.
# A token that does reach the container — a PVC `.env`, a hand-run container — is a
# deliberate statement that Slack is configured, so it counts there too.
_ENV_SIGNALS = {
    "google_chat": ("GOOGLE_CHAT_RELAY_URL", "GOOGLE_CHAT_PROJECT_ID", "GOOGLE_CHAT_HOME_CHANNEL"),
    "slack": ("SLACK_RELAY_URL", "SLACK_HOME_CHANNEL", "SLACK_BOT_TOKEN"),
}

# Where a message goes when nothing above resolves. Preserves the behaviour every
# caller had before this module existed, so an install this function cannot read is
# no worse off than it was.
DEFAULT_PLATFORM = "google_chat"


def _mapping(value: object) -> dict:
    """``value`` if it is a mapping, else an empty one.

    Every traversal of the parsed config goes through this, for the reason the twin in
    `profile_cron_tick` gives: `config.yaml` is a file the running agent writes to and
    a human may hand-edit, so `platforms: slack` is valid YAML that parses to a string.
    Reaching `.get` on it raises an AttributeError outside the `try` below, which
    escapes `_notify` — unguarded in `cluster_agent_reconcile.main` — and fails the
    hourly run *after* it has already created and pruned profiles, in a script whose
    whole contract is that it always exits 0. A wrong shape must cost the platform
    resolution, not the run.
    """
    return value if isinstance(value, dict) else {}


def _platforms_enabled_in(path: str) -> dict[str, bool]:
    """`platforms.<name>.enabled` from one config file, for the keys that set it.

    Absent keys are absent from the result rather than False — "this file does not
    say" and "this file says no" are different answers and only the second one should
    override a lower-precedence source. A bare `enabled:` with no value parses to
    None, which is the first answer, not the second: it is dropped rather than
    recorded as an explicit False that would then outrank a relay URL.

    For MANAGED_CONFIG_PATH on an operator-managed pod this returns both keys and
    settles the question outright. For CONFIG_PATH on that same pod it returns {},
    and that is the normal case rather than a failure: `renderConfigYAML` writes
    `platforms.<p>.enabled` only into the managed scope, which Hermes overlays per
    leaf key inside its own config loader rather than merging onto this file, so the
    profile's own file carries a `platforms` subtree with no `enabled` key in it at
    all. It is read anyway because it is the truth on the installs that do write it —
    a hand-run container, a profile configured off the image alone.
    """
    try:
        import yaml
    except Exception:  # noqa: BLE001 - no PyYAML: fall through to the env signals
        return {}

    try:
        with open(path, "r") as fh:
            cfg = yaml.safe_load(fh) or {}
        platforms = _mapping(cfg).get("platforms")
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - unreadable/malformed: the env still answers
        print(f"[CHAT-PLATFORMS] could not read {path}: {exc}", file=sys.stderr)
        return {}

    out: dict[str, bool] = {}
    for name in CHAT_PLATFORMS:
        block = _mapping(_mapping(platforms).get(name))
        if block.get("enabled") is not None:
            out[name] = bool(block["enabled"])
    return out


def _env_enabled(platform: str) -> bool:
    return any(os.environ.get(var, "").strip() for var in _ENV_SIGNALS.get(platform, ()))


def enabled_chat_platforms() -> list[str]:
    """Every chat platform this install posts to, in CHAT_PLATFORMS order.

    Three sources, most specific first, resolved **per platform** rather than per
    source — so a file that names one platform cannot silence another that only a
    lower-precedence source knows about. That short circuit is the shape of the bug
    this module exists to avoid.

    1. MANAGED_CONFIG_PATH, the operator's own statement of what the CR turned on.
       On a managed pod this settles both platforms and nothing below is consulted.
    2. CONFIG_PATH, the profile's writable file. Authoritative on an install with no
       operator, and normally silent on a managed one.
    3. The environment signals, for an install neither file describes.

    An explicit `enabled: false` therefore wins over an environment signal, in either
    file: that combination is an operator or an admin turning a platform off on a pod
    that still has the variables rendered on it, and the file is the more specific
    statement.

    Never returns an empty list: an install that resolves to nothing gets
    DEFAULT_PLATFORM, which is what every caller did unconditionally before.
    """
    from_managed = _platforms_enabled_in(MANAGED_CONFIG_PATH)
    from_profile = _platforms_enabled_in(CONFIG_PATH)

    resolved = []
    for name in CHAT_PLATFORMS:
        if name in from_managed:
            enabled = from_managed[name]
        elif name in from_profile:
            enabled = from_profile[name]
        else:
            enabled = _env_enabled(name)
        if enabled:
            resolved.append(name)
    return resolved or [DEFAULT_PLATFORM]


__all__ = ["CHAT_PLATFORMS", "DEFAULT_PLATFORM", "enabled_chat_platforms"]

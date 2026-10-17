#!/usr/bin/env python3
# agent_roster.py - Which specialist profiles exist, and what each is for.
#
# The Chat Agent needs this to pick an `assignee` before it delegates, and it
# needs it on essentially every request. Two consumers read it:
#
#   * ``router_server.py``'s ``list_agents`` MCP tool — the on-demand refresh
#     path, for when a named agent is missing or the injected roster is stale.
#   * the ``agent_roster`` plugin (``agents/chat/defaults/plugins/agent_roster``)
#     — injects the roster into every turn's context, so the common case costs
#     no tool call at all. Measured on the live deployment, calling the tool cost
#     a full LLM roundtrip (~6s of a ~17s acknowledgement) to re-read what is, in
#     the end, a directory listing.
#
# Both must describe the same fleet in the same words, which is the reason this
# lives in its own module rather than being duplicated into the plugin: a roster
# that disagrees with itself between the injected block and the tool is worse
# than either one alone.
#
# Everything here degrades rather than raises. The profiles live on a shared
# PVC, so an I/O or permission fault must not turn routing into a traceback.

import json
import os
import sys
import time
import urllib.request

from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
# Hermes stores each profile at $HERMES_HOME/profiles/<name> (persists on the data PVC).
PROFILES_BASE = HERMES_HOME / "profiles"
# The front door itself; never a valid delegation target.
SELF_PROFILE = "default"

EMPTY_ROSTER = "No specialist agents are currently available to route to."
# Discovery failed. Deliberately not EMPTY_ROSTER: an unreadable profiles
# directory must not be reported to the Chat Agent as an empty fleet.
UNKNOWN_ROSTER = (
    "The specialist roster could not be read right now. Do not conclude that no "
    "agents exist; retry, and say so plainly if it keeps failing."
)
NO_DESCRIPTION = "(no description provided)"

_PROVIDER_LABELS = {
    "vertex_ai": "Google Cloud Vertex AI",
    "gemini": "Google Gemini",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}
_PLACEHOLDER_ALIASES = {"model-default", "hermes-agent"}
_PROBE_SUCCESS_CACHE_TTL_SECS = 300.0
_PROBE_RETRY_COOLDOWN_SECS = 30.0

_cached_gateway_model: str = ""
_cached_gateway_model_expires_at: float = 0.0
_next_gateway_probe_at: float = 0.0


def log(msg: str) -> None:
    print(f"[AGENT-ROSTER] {msg}", file=sys.stderr)


def _summarize_soul(home: Path) -> str:
    """Fallback responsibilities: the first prose line of the profile's SOUL.md."""
    soul = home / "SOUL.md"
    # is_file() is inside the try: pathlib only swallows ENOENT/ENOTDIR/EBADF/ELOOP,
    # so a stat() that fails with EACCES or EIO on the shared PVC raises here too.
    try:
        if not soul.is_file():
            return ""
        lines = soul.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        return s
    return ""


def _responsibilities(home: Path) -> str:
    """A one-shot description of what a profile is responsible for.

    Prefers an explicit CAPABILITIES.md (the routing contract a specialist
    advertises to the Chat Agent); falls back to the SOUL.md summary so a
    profile is still discoverable even without a capabilities file.
    """
    cap = home / "CAPABILITIES.md"
    try:
        text = cap.read_text(encoding="utf-8", errors="replace").strip() if cap.is_file() else ""
    except OSError:
        text = ""
    if text:
        return text
    return _summarize_soul(home)


def discover(base: Path | None = None) -> list[dict[str, str]] | None:
    """Enumerate every routable specialist profile (all profiles except `default`).

    Degrades rather than raises. Errors are isolated per profile: one unreadable
    directory costs that one agent, not the whole roster.

    Returns ``None`` — distinct from ``[]`` — when the profiles directory itself
    could not be listed. "I could not read the fleet" and "there is no fleet"
    are different answers: the second is a fact the Chat Agent should act on,
    the first must not be reported as one.

    ``base`` defaults to the module-level ``PROFILES_BASE`` at call time rather
    than at import time, so a caller (or a test) that rebinds ``PROFILES_BASE``
    is still honoured.
    """
    root = PROFILES_BASE if base is None else base
    agents: list[dict[str, str]] = []
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError as e:
        log(f"cannot list profiles at {root}: {e}")
        return None
    for p in entries:
        try:
            if not p.is_dir() or p.name == SELF_PROFILE:
                continue
            agents.append({"name": p.name, "responsibilities": _responsibilities(p)})
        except OSError as e:
            log(f"skipping unreadable profile {p.name}: {e}")
    return agents


def format_roster(agents: list[dict[str, str]] | None) -> str | None:
    """Render the roster the Chat Agent reads before picking an assignee.

    Returns ``None`` when discovery itself failed (distinct from an empty fleet).
    Agents with identical responsibilities (the per-cluster fleet) are grouped:
    the repeated capability text appears once, with each assignee listed under
    it, so the prompt does not scale with the cluster count.
    """
    if agents is None:
        return None
    if not agents:
        return EMPTY_ROSTER

    groups: dict[str, list[str]] = {}
    undescribed: list[str] = []
    for a in agents:
        desc = a.get("responsibilities", "").strip()
        if desc:
            groups.setdefault(desc, []).append(a["name"])
        else:
            undescribed.append(a["name"])

    # Distinct specialists first, then the shared-role fleets — the front door routes to a
    # named specialist far more often than to a specific cluster. The undescribed trail
    # both: they are the least useful entries to route from.
    blocks: list[str] = []
    for desc, names in sorted(groups.items(), key=lambda kv: len(kv[1])):
        if len(names) == 1:
            blocks.append(f"- {names[0]}: {desc}")
            continue
        listed = "\n".join(f"  - {n}" for n in names)
        blocks.append(
            f"The following {len(names)} agents share one role — pick the one whose cluster "
            f"you need:\n{listed}\n  Shared role: {desc}"
        )
    blocks.extend(f"- {name}: {NO_DESCRIPTION}" for name in undescribed)
    return "\n\n".join(blocks)


def render(base: Path | None = None) -> str | None:
    """Discover and format in one call — what both consumers actually want.

    ``None`` means the roster could not be read at all. Both consumers treat
    that as "say nothing about the fleet" rather than "the fleet is empty".
    """
    return format_roster(discover(base))


def _split_backing(raw: str) -> tuple[str, str]:
    """Split `<provider>/<model>` and drop placeholder internal router aliases."""
    s = raw.strip()
    if not s:
        return "", ""
    provider_key, model_name = s.split("/", 1) if "/" in s else ("", s)
    provider_key, model_name = provider_key.strip(), model_name.strip()
    if not model_name or model_name in _PLACEHOLDER_ALIASES:
        return "", ""
    return provider_key, model_name


def _probe_gateway_model() -> str:
    """Resolve the backing `<provider>/<model>` from LiteLLM.

    Results are cached for ``_PROBE_SUCCESS_CACHE_TTL_SECS`` so provider switches
    in LiteLLM take effect without pod restarts, while network errors keep the
    last-known good model and respect a 30s failure cooldown.
    """
    global _cached_gateway_model, _cached_gateway_model_expires_at, _next_gateway_probe_at
    now = time.monotonic()
    if _cached_gateway_model and now < _cached_gateway_model_expires_at:
        return _cached_gateway_model
    if now < _next_gateway_probe_at:
        return _cached_gateway_model

    url = os.environ.get("LITELLM_MODEL_INFO_URL", "").strip()
    if not url and os.environ.get("INFERENCE_GATEWAY_SERVICE_HOST", "").strip():
        ns = os.environ.get("KUBE_DEFAULT_NAMESPACE", "kubeagents-system").strip() or "kubeagents-system"
        url = f"http://inference-gateway.{ns}.svc.cluster.local/v1/model/info"
    if not url:
        return _cached_gateway_model

    try:
        with urllib.request.urlopen(url, timeout=0.5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            _next_gateway_probe_at = now + _PROBE_RETRY_COOLDOWN_SECS
            return _cached_gateway_model
        for entry in payload.get("data") or ():
            if isinstance(entry, dict) and entry.get("model_name") in _PLACEHOLDER_ALIASES:
                params = entry.get("litellm_params")
                model = str((params if isinstance(params, dict) else {}).get("model") or "").strip()
                if _split_backing(model)[1]:
                    _cached_gateway_model = model
                    _cached_gateway_model_expires_at = now + _PROBE_SUCCESS_CACHE_TTL_SECS
                    return model
    except Exception as e:
        log(f"cannot read model info from {url}: {e}")

    _next_gateway_probe_at = now + _PROBE_RETRY_COOLDOWN_SECS
    return _cached_gateway_model


def render_model_identity() -> str | None:
    """Render the backing LLM model and provider when known, or ``None``."""
    provider_key, model_name = _split_backing(os.environ.get("LITELLM_BACKING_MODEL", ""))
    if not model_name:
        provider_env = os.environ.get("MODEL_PROVIDER", "").strip()
        model_env = os.environ.get("MODEL_NAME", "").strip()
        raw_env = f"{provider_env}/{model_env}" if (provider_env and model_env) else model_env
        provider_key, model_name = _split_backing(raw_env)
    if not model_name:
        provider_key, model_name = _split_backing(_probe_gateway_model())
    if not model_name:
        return None
    if provider_key and provider_key != "custom":
        label = _PROVIDER_LABELS.get(provider_key, provider_key)
        prov = f"Provider: {label} ({provider_key})" if label != provider_key else f"Provider: {label}"
        return (
            f"Backing model: {model_name}\n{prov}\n"
            "When asked what model or provider you are running on, state this backing model "
            "and provider directly; never report internal router alias names or the `custom` "
            "provider placeholder."
        )
    return (
        f"Backing model: {model_name}\n"
        "When asked what model you are running on, state this backing model directly; "
        "never report internal router alias names or the `custom` provider placeholder."
    )

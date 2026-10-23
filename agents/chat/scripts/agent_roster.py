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

# The cluster_identity block a Cluster Agent's config.yaml carries
# (agents/platform/scripts/cluster_agent_profile.py, _inject_cluster_identity):
# the scaffold's yaml.safe_dump output, a top-level key then scalars indented by
# IDENTITY_INDENT. Quote characters a dumped scalar may be wrapped in.
PROFILE_CONFIG = "config.yaml"
IDENTITY_BLOCK = "cluster_identity:"
IDENTITY_NAME_KEY = "cluster"
IDENTITY_PLACE_KEYS = ("project", "location")
IDENTITY_KEYS = (IDENTITY_NAME_KEY, *IDENTITY_PLACE_KEYS)
IDENTITY_INDENT = "  "
YAML_QUOTES = "'\""
# discover() stores each identity value under this prefix plus its key, and the
# roster line shows it with these templates (see _identity_note).
IDENTITY_FIELD_PREFIX = "cluster_"
IDENTITY_NOTE = " (the {cluster} cluster agent{where})"
IDENTITY_WHERE = "; {places}"
IDENTITY_PLACE = "{key} {value}"

_PROVIDER_LABELS = {
    "vertex_ai": "Google Cloud Vertex AI",
    "gemini": "Google Gemini",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}
_PLACEHOLDER_ALIASES = {"model-default", "hermes-agent"}
_CUSTOM_PROVIDER = "custom"
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


def _cluster_identity(home: Path) -> dict[str, str]:
    """The cluster a Cluster Agent's profile targets, from its config.yaml, or {}.

    The profile name is a sanitized, sometimes truncated-and-hashed assignee id,
    not a name to show a person, and the cluster name cannot be read back out of
    it (project, cluster and location may all hold hyphens). The scaffold stamps
    the real identity into config.yaml; reading it here gives the front door the
    name to say. A line scan rather than a YAML load, because this module is
    stdlib-only (it runs ahead of every front-door turn and in the MCP server):
    the block is the scaffold's own `yaml.safe_dump` output, top-level key then
    two-space-indented scalars.
    """
    try:
        cfg = home / PROFILE_CONFIG
        if not cfg.is_file():
            return {}
        lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    identity: dict[str, str] = {}
    inside = False
    for line in lines:
        if not inside:
            inside = line.rstrip() == IDENTITY_BLOCK
            continue
        if not line.startswith(IDENTITY_INDENT) or line.startswith(IDENTITY_INDENT + " "):
            break
        key, sep, value = line.strip().partition(":")
        value = value.strip().strip(YAML_QUOTES)
        if sep and key in IDENTITY_KEYS and value:
            identity[key] = value
    return identity if identity.get("cluster") else {}


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
            if not p.is_dir() or p.name == SELF_PROFILE or p.name.startswith("."):
                continue
            agent = {"name": p.name, "responsibilities": _responsibilities(p)}
            agent.update({IDENTITY_FIELD_PREFIX + k: v for k, v in _cluster_identity(p).items()})
            agents.append(agent)
        except OSError as e:
            log(f"skipping unreadable profile {p.name}: {e}")
    return agents


def format_roster(agents: list[dict[str, str]] | None) -> str | None:
    """Render the roster the Chat Agent reads, or ``None`` if it is unknowable.

    Agents sharing an identical role description are grouped so the description
    is stated once instead of repeated verbatim per agent — every Cluster Agent
    is scaffolded from the same template, so a fleet of thirty would otherwise
    be thirty copies of one paragraph. Assignee names are always listed
    individually, because the name is the part the caller has to get exactly
    right.

    ``None`` in (discovery failed) is ``None`` out; callers decide how to say
    "unknown", which is not the same thing as ``EMPTY_ROSTER``.
    """
    if agents is None:
        return None
    if not agents:
        return EMPTY_ROSTER

    groups: dict[str, list[str]] = {}
    # A missing description is an absence, not a shared role: grouping on the
    # placeholder would announce unrelated profiles as one interchangeable
    # fleet, and the "pick the one whose cluster you need" preamble would be a
    # lie about agents nothing is known of. Each gets its own line.
    undescribed: list[str] = []
    for a in agents:
        desc = a["responsibilities"]
        if desc:
            groups.setdefault(desc, []).append(a["name"])
        else:
            undescribed.append(a["name"])

    # Distinct specialists first, then the shared-role fleets — the front door routes to a
    # named specialist far more often than to a specific cluster. The undescribed trail
    # both: they are the least useful entries to route from.
    # The name a person reads beside the assignee id, when the profile records
    # its cluster (a Cluster Agent does): "the <cluster> cluster agent".
    shown = {a["name"]: _identity_note(a) for a in agents}

    blocks: list[str] = []
    for desc, names in sorted(groups.items(), key=lambda kv: len(kv[1])):
        if len(names) == 1:
            blocks.append(f"- {names[0]}{shown[names[0]]}: {desc}")
            continue
        listed = "\n".join(f"  - {n}{shown[n]}" for n in names)
        blocks.append(
            f"The following {len(names)} agents share one role — pick the one whose cluster "
            f"you need:\n{listed}\n  Shared role: {desc}"
        )
    blocks.extend(f"- {name}{shown[name]}: {NO_DESCRIPTION}" for name in undescribed)
    return "\n\n".join(blocks)


def _identity_note(agent: dict[str, str]) -> str:
    """" (the <cluster> cluster agent; project <p>, location <l>)", or "" without one.

    Set apart from the assignee id by a space and parentheses, so the id stays
    the exact token the footer tells the caller to copy.
    """
    cluster = agent.get(IDENTITY_FIELD_PREFIX + IDENTITY_NAME_KEY, "")
    if not cluster:
        return ""
    places = ", ".join(
        IDENTITY_PLACE.format(key=k, value=agent[IDENTITY_FIELD_PREFIX + k])
        for k in IDENTITY_PLACE_KEYS
        if agent.get(IDENTITY_FIELD_PREFIX + k)
    )
    where = IDENTITY_WHERE.format(places=places) if places else ""
    return IDENTITY_NOTE.format(cluster=cluster, where=where)


def render(base: Path | None = None) -> str | None:
    """Discover and format in one call — what both consumers actually want.

    ``None`` means the roster could not be read at all. Both consumers treat
    that as "say nothing about the fleet" rather than "the fleet is empty".
    """
    return format_roster(discover(base))


def _split_backing(raw: str) -> tuple[str, str]:
    """Split `<provider>/<model>` and drop placeholder router names.

    The provider key is casefolded, and the model half is compared casefolded,
    so `Model-Default` or `Custom` is caught the same way as the lowercase
    spelling. A model half that is a router alias or the bare `custom`
    placeholder means no backing model is known. The model half keeps its own
    spelling, because a served model id (a Hugging Face id on vLLM) is
    case-sensitive.
    """
    s = raw.strip()
    if not s:
        return "", ""
    provider_key, model_name = s.split("/", 1) if "/" in s else ("", s)
    provider_key, model_name = provider_key.strip().casefold(), model_name.strip()
    folded = model_name.casefold()
    if not model_name or folded in _PLACEHOLDER_ALIASES or folded == _CUSTOM_PROVIDER:
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

    if not os.environ.get("INFERENCE_GATEWAY_SERVICE_HOST", "").strip():
        return _cached_gateway_model
    ns = os.environ.get("KUBE_DEFAULT_NAMESPACE", "kubeagents-system").strip() or "kubeagents-system"
    url = f"http://inference-gateway.{ns}.svc.cluster.local/v1/model/info"

    # The 0.5s timeout bounds the connect and the read, not the DNS lookup. A
    # degraded cluster DNS can hold a user turn for longer, at most once per
    # cooldown window.
    try:
        with urllib.request.urlopen(url, timeout=0.5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            log(f"model info from {url} is not a JSON object; backing model unknown")
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
        log(f"model info from {url} has no {sorted(_PLACEHOLDER_ALIASES)} entry with a backing model")
    except Exception as e:
        log(f"cannot read model info from {url}: {e}")

    _next_gateway_probe_at = now + _PROBE_RETRY_COOLDOWN_SECS
    return _cached_gateway_model


def render_model_identity() -> str | None:
    """Render the backing LLM model and provider when known, or ``None``.

    The in-cluster LiteLLM gateway is the only source. Nothing in the chart,
    operator or installer sets a model variable on the agent container, so an
    env override would be a second source that only a hand edit can fill.
    """
    provider_key, model_name = _split_backing(_probe_gateway_model())
    if not model_name:
        return None
    if provider_key in _PROVIDER_LABELS:
        prov = f"Provider: {_PROVIDER_LABELS[provider_key]} ({provider_key})"
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

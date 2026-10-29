#!/usr/bin/env python3
# agent_roster.py - Which specialist profiles exist, what each is for, which
# cluster the install runs on, and which GCP projects its scope names.
#
# The Chat Agent needs this to pick an `assignee` before it delegates, and it
# needs it on essentially every request. The host and scope sections let it
# answer "what projects are in scope?" and route "this cluster" without a
# second roundtrip. Two consumers read it:
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

import hashlib
import importlib
import os
import re
import sys

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
IDENTITY_PROJECT_KEY = "project"
IDENTITY_LOCATION_KEY = "location"
IDENTITY_PLACE_KEYS = (IDENTITY_PROJECT_KEY, IDENTITY_LOCATION_KEY)
IDENTITY_KEYS = (IDENTITY_NAME_KEY, *IDENTITY_PLACE_KEYS)
IDENTITY_INDENT = "  "
YAML_QUOTES = "'\""
# discover() stores each identity value under this prefix plus its key, and the
# roster line shows it with these templates (see _identity_note).
IDENTITY_FIELD_PREFIX = "cluster_"
IDENTITY_NOTE = " (the {cluster} cluster agent{where})"
IDENTITY_WHERE = "; {places}"
IDENTITY_PLACE = "{key} {value}"

# The cluster this pod runs on. The operator sets these on the agent container
# outside kind (platformagent_manifests.go); the router MCP child gets them
# through its `env:` block in agents/chat/config.yaml.
HOST_CLUSTER_ENV = "GKE_CLUSTER_NAME"
HOST_PROJECT_ENV = "GKE_PROJECT_ID"
HOST_LOCATION_ENV = "GKE_LOCATION"
HOST_ASSIGNEE_KEY = "assignee"
HOST_DISPLAY_KEYS = (*IDENTITY_KEYS, HOST_ASSIGNEE_KEY)
# Hermes hands a stdio MCP server the literal placeholder when the variable it
# names is unset (gke_endpoint.py reads it the same way); that is not a value.
UNEXPANDED_PLACEHOLDER_PREFIX = "${"
HOST_IDENTITY_HEADING = 'Host Cluster Identity ("this cluster" / "this host cluster"):'
# Cluster Agent profile names, as cluster_agent_profile.profile_name derives
# them. Mirrored rather than imported: that module imports the sandbox and
# endpoint modules, and this one runs ahead of every front-door turn.
# test_agent_roster.py holds the two in step.
PROFILE_NAME_MAX_LEN = 63

# The projects in scope come from the platform agent's own reader,
# agents/platform/scripts/fleet_scope_targets.py, which the image ships beside
# this script (deploy/docker/Dockerfile copies both script directories into
# /opt/defaults/scripts). One reader means the front door and the platform
# agent's fleet_scope tool give the same answer, including the rows the
# reconcile could not read and the partial marker before its first write.
SCOPE_READER = "fleet_scope_targets"
SCOPE_HEADING = "GCP Projects in Scope:"
SCOPE_LINE = "- GCP projects in scope ({count}): {projects}"
SCOPE_PARTIAL_LINE = (
    "- GCP projects in scope (partial: {reason}): {projects}. Say the list is "
    "partial when you answer."
)
SCOPE_PARTIAL_PENDING = (
    "scope discovery has not completed yet; only the host and explicitly "
    "declared projects can be listed"
)
SCOPE_PARTIAL_NUMERIC_HOST = (
    "scope discovery has not completed yet and the host project is configured "
    "by number, so only explicitly declared projects can be listed"
)
SCOPE_PARTIAL_UNRESOLVED = (
    "the declaration names folders, organisations or selectors whose members "
    "discovery has not resolved yet"
)
SCOPE_NONE_LISTED = "none read yet"
# How many in-scope projects _format_scope names individually before counting
# the rest: maxProjects admits up to 5000, and a folder or organisation past it
# adds one over-cap row per remaining cluster-holding project
# (bootstrap_scan_gate.py uses the same 20-project cap).
SCOPE_NAMED_LIMIT = 20
SCOPE_UNDECLARED = (
    "- No project scope is declared for this install (spec.scope is unset), so "
    "there is no fixed list to recite."
)
SCOPE_UNREADABLE = (
    "- The scope declaration could not be read right now. Do not answer from "
    "memory; route the question to `platform`."
)


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
    return identity if identity.get(IDENTITY_NAME_KEY) else {}


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


def _host_env(name: str) -> str:
    """An environment value, or "" when unset or still a `${...}` placeholder."""
    value = os.environ.get(name, "").strip()
    return "" if value.startswith(UNEXPANDED_PLACEHOLDER_PREFIX) else value


def _profile_name(project: str, cluster: str, location: str) -> str:
    """The Cluster Agent profile name for a cluster (cluster_agent_profile.profile_name)."""
    raw = f"cluster-{project}-{cluster}-{location}".lower()
    name = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", raw)).strip("-")
    if len(name) > PROFILE_NAME_MAX_LEN:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: PROFILE_NAME_MAX_LEN - 9]}-{digest}"
    return name


def _management_project_id(base: Path | None = None) -> str:
    """The install's own project ID from fleet_scope.json's management row, or ""."""
    reader = _scope_reader()
    fn = getattr(reader, "management_project_id", None) if reader is not None else None
    if not callable(fn):
        return ""
    root = PROFILES_BASE if base is None else base
    try:
        return fn(root.parent) or ""
    except Exception as e:
        log(f"cannot read the management project: {e}")
        return ""


def read_host_identity(
    agents: list[dict[str, str]] | None,
    base: Path | None = None,
) -> dict[str, str] | None:
    """The cluster this pod runs on, and its Cluster Agent when one exists.

    No cluster name, no host: a project or location alone cannot say which
    cluster "this cluster" is. The operator lets spec.harness.projectId be a
    project number while the reconcile resolves it to the project ID in
    fleet_scope.json's management row and scaffolds every profile on that ID,
    so an all-digit GKE_PROJECT_ID is never printed as the project and completes
    the triple from the snapshot instead (omitting project and assignee until
    the snapshot records it, rather than guessing on cluster and location). A
    derived-name fallback applies only to an unstamped profile directory so a
    hyphen-boundary collision never overrides a disagreeing cluster_identity.
    An assignee that is not a discovered profile is never advertised, because a
    card filed to it is never dispatched.
    """
    cluster = _host_env(HOST_CLUSTER_ENV)
    if not cluster:
        return None
    raw_project = _host_env(HOST_PROJECT_ENV)
    project = _management_project_id(base) if raw_project.isdigit() else raw_project
    location = _host_env(HOST_LOCATION_ENV)
    host = {IDENTITY_NAME_KEY: cluster}
    if project:
        host[IDENTITY_PROJECT_KEY] = project
    if location:
        host[IDENTITY_LOCATION_KEY] = location
    if project and location:
        wanted = {
            IDENTITY_NAME_KEY: cluster,
            IDENTITY_PROJECT_KEY: project,
            IDENTITY_LOCATION_KEY: location,
        }
        by_name = {a["name"]: a for a in agents or []}
        match = next(
            (
                a["name"]
                for a in agents or []
                if all(a.get(IDENTITY_FIELD_PREFIX + k) == v for k, v in wanted.items())
            ),
            "",
        )
        derived = _profile_name(project, cluster, location)
        derived_entry = by_name.get(derived)
        unstamped = (
            derived
            if derived_entry is not None
            and not any(derived_entry.get(IDENTITY_FIELD_PREFIX + k) for k in IDENTITY_KEYS)
            else ""
        )
        assignee = match or unstamped
        if assignee:
            host[HOST_ASSIGNEE_KEY] = assignee
    return host


def _format_host(host: dict[str, str] | None) -> str:
    if not host:
        return ""
    parts = [f"{k}={host[k]}" for k in HOST_DISPLAY_KEYS if host.get(k)]
    return f"{HOST_IDENTITY_HEADING}\n- {', '.join(parts)}"


def _scope_reader():
    """fleet_scope_targets, or None when it cannot be imported.

    In the pod it sits beside this file. The plugin loads this file by path, so
    its directory is not on sys.path there; it is appended (not prepended) so
    nothing installed is shadowed. A checkout keeps it under agents/platform.
    """
    here = Path(__file__).resolve().parent
    for directory in (here, here.parent.parent / "platform" / "scripts"):
        if (directory / f"{SCOPE_READER}.py").is_file() and str(directory) not in sys.path:
            sys.path.append(str(directory))
    try:
        return importlib.import_module(SCOPE_READER)
    except Exception as e:  # an import-time fault must not cost the roster
        log(f"cannot import {SCOPE_READER}: {e}")
        return None


def _format_scope(base: Path | None = None) -> str:
    """The projects the declared scope resolved to, as the fleet_scope tool reads them."""
    reader = _scope_reader()
    if reader is None:
        return ""
    # The router child gets these through `${...}` entries; an unset one arrives
    # as the literal placeholder, which the reader would take for a path or a
    # project. Unset is what it means.
    for name in (reader.SCOPE_FILE_ENV, *reader.MANAGEMENT_PROJECT_ENVS):
        if os.environ.get(name, "").startswith(UNEXPANDED_PLACEHOLDER_PREFIX):
            os.environ.pop(name, None)
    root = PROFILES_BASE if base is None else base
    try:
        targets = reader.declared_scope_targets(root.parent)
    except Exception as e:  # ScopeRenderUnreadable, or a fault on the shared PVC
        log(f"cannot read the declared scope: {e}")
        return f"{SCOPE_HEADING}\n{SCOPE_UNREADABLE}"
    if targets is None:
        return f"{SCOPE_HEADING}\n{SCOPE_UNDECLARED}"
    unresolved_row = getattr(reader, "UNRESOLVED_SCOPE_ROW", None)
    entries: list[tuple[str, str | None]] = [
        *((p, None) for p in targets.projects),
        *((p, outcome) for p, outcome in targets.unread if p != unresolved_row),
    ]
    total = len(entries)
    listed = [
        p if outcome is None else f"{p} ({outcome})"
        for p, outcome in entries[:SCOPE_NAMED_LIMIT]
    ]
    if total > SCOPE_NAMED_LIMIT:
        omitted = entries[SCOPE_NAMED_LIMIT:]
        outcome_counts: dict[str, int] = {}
        for _, outcome in omitted:
            if outcome is not None:
                outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        if outcome_counts:
            breakdown = ", ".join(f"{n} {outcome}" for outcome, n in outcome_counts.items())
            listed.append(f"and {len(omitted)} more ({breakdown})")
        else:
            listed.append(f"and {len(omitted)} more")
    if targets.note:
        note_reasons = {
            getattr(reader, "RENDER_NOTE", None): SCOPE_PARTIAL_PENDING,
            getattr(reader, "RENDER_NUMERIC_HOST_NOTE", None): SCOPE_PARTIAL_NUMERIC_HOST,
        }
        reason = note_reasons.get(targets.note, SCOPE_PARTIAL_PENDING)
    elif any(p == unresolved_row for p, _ in targets.unread):
        reason = SCOPE_PARTIAL_UNRESOLVED
    else:
        reason = ""
    projects = ", ".join(listed) or SCOPE_NONE_LISTED
    if reason:
        return f"{SCOPE_HEADING}\n{SCOPE_PARTIAL_LINE.format(reason=reason, projects=projects)}"
    return f"{SCOPE_HEADING}\n{SCOPE_LINE.format(count=total, projects=projects)}"


def render(base: Path | None = None) -> str | None:
    """Discover and format in one call — what both consumers actually want.

    ``None`` means the roster could not be read at all. Both consumers treat
    that as "say nothing about the fleet" rather than "the fleet is empty".
    """
    agents = discover(base)
    roster = format_roster(agents)
    if roster is None:
        return None
    extra = [b for b in (_format_host(read_host_identity(agents, base)), _format_scope(base)) if b]
    return "\n\n".join([roster, *extra])

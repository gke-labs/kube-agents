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
import time

from collections import Counter
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

# Above this many agents in one shared-role group, format_roster counts the
# group by project and location instead of naming every member: at thousands
# of Cluster Agents the named list is tens of thousands of tokens on every
# front-door turn (#4432). The operator passes the variable through
# spec.deployment.env (safeSandboxEnvOverrides, platformagent_manifests.go);
# a value that is not a whole number of zero or more means the default.
SUMMARY_THRESHOLD_ENV = "KUBEAGENTS_ROSTER_SUMMARY_THRESHOLD"
SUMMARY_THRESHOLD_DEFAULT = 20
# How many projects, and how many locations, a counted group names before
# counting the rest (the same cap as SCOPE_NAMED_LIMIT below).
SUMMARY_NAMED_LIMIT = 20
# A member of a counted group whose profile records no value for the field.
SUMMARY_UNSTAMPED = "(unstamped)"
SUMMARY_HEADING = (
    "The following {count} agents share one role and are counted here rather "
    "than named:"
)
SUMMARY_BY_LINE = "  - by {key}: {counts}"
SUMMARY_COUNT = "{value} ({count})"
SUMMARY_MORE = "and {more} more {key}s ({count} agents)"
# The sentence that points at the lookup. The plugin keys its footer on it
# (agents/chat/defaults/plugins/agent_roster/plugin.py), so it is one constant.
SUMMARY_HINT = (
    "  To route to one of them, call `list_agents` with `filter` set to the "
    "cluster's name (an assignee id, project or location matches too) and use "
    "the `assignee` it returns verbatim."
)

# list_agents' filter: a case-insensitive substring match on the assignee id
# and the three identity fields, capped so a filter that matches a whole
# project cannot bring the full list back. An agent one of whose fields is
# the whole needle sorts ahead of the substring matches, so the cap never
# hides `prod` behind `prod-01` ... `prod-60`: the needle is already the
# whole cluster name there, and nothing narrower exists.
FILTER_MATCH_LIMIT = 50
FILTER_HEADING = (
    'Agents matching "{query}" ({shown} of {matched} matched, {total} on the roster):'
)
FILTER_MORE = "... and {more} more; narrow the filter."
FILTER_NONE = (
    'No specialist agent matches "{query}" ({total} on the roster). A named '
    "cluster with no match has no cluster agent yet: route the request to "
    "`platform`, which owns cluster-agent lifecycle."
)
# A no-match from a walk that could not read every profile. Deliberately not
# FILTER_NONE, for the reason UNKNOWN_ROSTER is not EMPTY_ROSTER: the profile
# the filter was looking for may be the one the walk skipped.
FILTER_UNREAD = (
    'No specialist agent matches "{query}", but the roster could not be fully '
    "read ({total} profiles read). Do not conclude that the cluster has no "
    "agent: call `list_agents` again with the same `filter`, and say so plainly "
    "if it keeps failing."
)

# render() keeps the last roster per process, keyed on the mtimes of the
# profiles directory (an add, remove, rename or archive changes it) and of the
# scope snapshot and render it reads, never on time: a TTL goes stale in the
# window that matters, right after a scaffold. Filesystem timestamps are
# coarse (a jiffy on Linux), so a tree changed within this many seconds is
# never cached: a second change inside the same tick would be invisible. A
# stamp read after a walk is held to the walk's start as well: a write that
# landed while the walk was open may postdate the read, so the content on
# hand is not the one the stamp vouches for.
CACHE_RACY_SECONDS = 2
CACHE_RACY_NS = CACHE_RACY_SECONDS * 1_000_000_000
# A Cluster Agent profile whose identity is not yet stamped is keyed on its
# own directory's and config.yaml's mtimes as well: the scaffold creates the
# directory first and rewrites config.yaml in place afterwards
# (cluster_agent_profile.py), which moves no mtime of the profiles directory.
CLUSTER_PROFILE_PREFIX = "cluster-"

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
        _note_degraded(profile=True)
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
        _note_degraded(profile=True)
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
        _note_degraded(profile=True)
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
            _note_degraded(profile=True)
    return agents


def summary_threshold() -> int:
    """The group size above which format_roster counts instead of naming.

    Read from SUMMARY_THRESHOLD_ENV at call time. Anything that is not a whole
    number of zero or more (unset, blank, the router child's literal ``${...}``
    placeholder, a negative) is the default.
    """
    try:
        value = int(os.environ.get(SUMMARY_THRESHOLD_ENV, ""))
    except ValueError:
        return SUMMARY_THRESHOLD_DEFAULT
    return value if value >= 0 else SUMMARY_THRESHOLD_DEFAULT


def format_roster(
    agents: list[dict[str, str]] | None,
    threshold: int | None = None,
) -> str | None:
    """Render the roster the Chat Agent reads, or ``None`` if it is unknowable.

    Agents sharing an identical role description are grouped so the description
    is stated once instead of repeated verbatim per agent — every Cluster Agent
    is scaffolded from the same template, so a fleet of thirty would otherwise
    be thirty copies of one paragraph. Assignee names are listed individually,
    because the name is the part the caller has to get exactly right, up to
    ``threshold`` per group (``summary_threshold()`` when ``None``); a larger
    group is counted by project and location instead, and the block points at
    ``list_agents``' filter for the one name the caller needs.

    ``None`` in (discovery failed) is ``None`` out; callers decide how to say
    "unknown", which is not the same thing as ``EMPTY_ROSTER``.
    """
    if agents is None:
        return None
    if not agents:
        return EMPTY_ROSTER
    if threshold is None:
        threshold = summary_threshold()

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

    by_name = {a["name"]: a for a in agents}
    blocks: list[str] = []
    for desc, names in sorted(groups.items(), key=lambda kv: len(kv[1])):
        if len(names) == 1:
            blocks.append(f"- {names[0]}{shown[names[0]]}: {desc}")
            continue
        if len(names) > threshold:
            blocks.append(_summarize_group([by_name[n] for n in names], desc))
            continue
        listed = "\n".join(f"  - {n}{shown[n]}" for n in names)
        blocks.append(
            f"The following {len(names)} agents share one role — pick the one whose cluster "
            f"you need:\n{listed}\n  Shared role: {desc}"
        )
    blocks.extend(f"- {name}{shown[name]}: {NO_DESCRIPTION}" for name in undescribed)
    return "\n\n".join(blocks)


def _summarize_group(members: list[dict[str, str]], desc: str) -> str:
    """A shared-role group as counts: total, by project, by location, the role, the hint.

    Each count line names the SUMMARY_NAMED_LIMIT largest values and counts the
    rest. Profiles record project and location only (cluster_identity), so
    those are the two axes; a member without the field counts as unstamped.
    """
    lines = [SUMMARY_HEADING.format(count=len(members))]
    for key in IDENTITY_PLACE_KEYS:
        counts = Counter(m.get(IDENTITY_FIELD_PREFIX + key) or SUMMARY_UNSTAMPED for m in members)
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        named = [SUMMARY_COUNT.format(value=v, count=n) for v, n in ordered[:SUMMARY_NAMED_LIMIT]]
        rest = ordered[SUMMARY_NAMED_LIMIT:]
        if rest:
            named.append(SUMMARY_MORE.format(more=len(rest), key=key, count=sum(n for _, n in rest)))
        lines.append(SUMMARY_BY_LINE.format(key=key, counts=", ".join(named)))
    lines.append(f"  Shared role: {desc}")
    lines.append(SUMMARY_HINT)
    return "\n".join(lines)


def _match_fields(agent: dict[str, str]) -> tuple[str, ...]:
    """The assignee id and the identity fields a filter is matched against."""
    return (agent["name"], *(agent.get(IDENTITY_FIELD_PREFIX + k, "") for k in IDENTITY_KEYS))


def _matches(agent: dict[str, str], query: str) -> bool:
    """Whether the assignee id or an identity field contains ``query`` (already lowercased)."""
    return any(query in f.lower() for f in _match_fields(agent) if f)


def _matches_exactly(agent: dict[str, str], query: str) -> bool:
    """Whether the assignee id or an identity field *is* ``query`` (already lowercased)."""
    return any(query == f.lower() for f in _match_fields(agent) if f)


def format_matches(
    agents: list[dict[str, str]] | None, query: str, degraded: bool = False
) -> str | None:
    """The agents matching ``query``, rendered as format_roster renders them, or ``None``.

    Never summarized, whatever the threshold: the filter exists to hand back
    the names a counted block withheld. Capped at FILTER_MATCH_LIMIT names,
    the exact matches first (a cluster named ``prod`` among ``prod-01`` ...
    ``prod-60`` is otherwise the one the cap drops), the rest in discovery
    order. ``degraded`` says the walk that produced ``agents`` skipped or
    half-read a profile (the per-profile faults only: a scope or
    management-project fault leaves every profile read, so a no-match is
    still a fact); a no-match from such a walk is reported as unread, not as
    absent.
    """
    if agents is None:
        return None
    needle = query.strip().lower()
    matched = [a for a in agents if _matches(a, needle)]
    if not matched:
        none = FILTER_UNREAD if degraded else FILTER_NONE
        return none.format(query=query.strip(), total=len(agents))
    # sort() is stable, so the substring matches keep their discovery order.
    matched.sort(key=lambda a: not _matches_exactly(a, needle))
    shown = matched[:FILTER_MATCH_LIMIT]
    heading = FILTER_HEADING.format(
        query=query.strip(), shown=len(shown), matched=len(matched), total=len(agents)
    )
    parts = [heading, format_roster(shown, threshold=len(shown))]
    if len(matched) > len(shown):
        parts.append(FILTER_MORE.format(more=len(matched) - len(shown)))
    return "\n".join(parts)


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
        _note_degraded()
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
        _note_degraded()
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


# The one cached roster, a single binding so a reader never sees a new key
# beside the previous text: (key, the unstamped profiles' own stamps, the
# agents discovered, the text rendered).
_cache: tuple | None = None
# Counted up by any fallback path while a discovery runs (a profile skipped, a
# description or identity lost to an OSError, a scope that could not be read):
# a roster that degraded is rendered for this call and never cached, as before
# the cache every turn re-read it. A count that only grows, never a flag reset
# per call: the gateway runs each session's turn on its own thread, so two
# discoveries overlap after one invalidation, and a flag the second reset
# would let the first cache the roster that lost a profile. A discovery
# compares the count before and after its walk; a fault another caller noted
# in that window costs it one skipped store, never a cached degraded roster.
_faults = 0
# The per-profile share of _faults (a profile skipped, a description or
# identity lost): what render_filtered's no-match answer reads. A scope or
# management-project fault counts on _faults alone, so it still keeps the
# roster out of the cache but never turns a no-match into FILTER_UNREAD,
# whose claim is that a profile may have gone unread.
_profile_faults = 0


def _note_degraded(profile: bool = False) -> None:
    global _faults, _profile_faults
    _faults += 1
    if profile:
        _profile_faults += 1


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _racy(stamps, read_at: int | None = None) -> bool:
    """Whether any stamp is within CACHE_RACY_SECONDS of now, or in its future.

    ``read_at`` is when the content the stamps vouch for was read (the start
    of the walk); a stamp newer than CACHE_RACY_SECONDS before it is racy too,
    since the write may have landed after the read.
    """
    now = time.time_ns()
    floor = None if read_at is None else read_at - CACHE_RACY_NS
    return any(
        s is not None and (abs(now - s) < CACHE_RACY_NS or (floor is not None and s > floor))
        for s in stamps
    )


def _cache_key_for(root: Path) -> tuple | None:
    """What the rendered roster depends on, or ``None`` when it must not be cached now.

    The profiles directory's mtime (an add, remove, rename or archive), the
    scope snapshot's and the scope render's when named (the reconcile rewrites
    the snapshot in place; the render is a mounted ConfigMap), and the summary
    threshold. ``None`` when the directory cannot be stat'ed (discover decides
    what that means) or any of them changed within CACHE_RACY_SECONDS.
    """
    stamps = [_mtime_ns(root)]
    if stamps[0] is None:
        return None
    reader = _scope_reader()
    if reader is not None:
        snapshot = getattr(reader, "snapshot_path", None)
        try:
            stamps.append(_mtime_ns(Path(snapshot(root.parent))) if callable(snapshot) else None)
        except Exception:
            stamps.append(None)
        render_path = os.environ.get(reader.SCOPE_FILE_ENV, "")
        if render_path and not render_path.startswith(UNEXPANDED_PLACEHOLDER_PREFIX):
            stamps.append(_mtime_ns(Path(render_path)))
    if _racy(stamps):
        return None
    return (str(root), *stamps, summary_threshold())


def _unstamped(agents: list[dict[str, str]]) -> list[str]:
    """The cluster- profiles whose identity the scaffold has not stamped yet."""
    return [
        a["name"]
        for a in agents
        if a["name"].startswith(CLUSTER_PROFILE_PREFIX)
        and not a.get(IDENTITY_FIELD_PREFIX + IDENTITY_NAME_KEY)
    ]


def _profile_stamps(root: Path, names: list[str], read_at: int | None = None) -> tuple | None:
    """(name, directory mtime, config.yaml mtime) per profile, or ``None`` when one is racy or gone.

    The stamp rewrites config.yaml in place (its mtime moves) and an overlay
    creates it (the directory's moves), so either change misses the cache.
    ``read_at`` (see ``_racy``) is the walk's start when the stamps are taken
    after it, so a stamp that landed during the walk is never stored.
    """
    out = []
    for name in names:
        home = root / name
        stamps = (_mtime_ns(home), _mtime_ns(home / PROFILE_CONFIG))
        if stamps[0] is None or _racy(stamps, read_at):
            return None
        out.append((name, *stamps))
    return tuple(out)


def _discover_and_render(
    base: Path | None,
) -> tuple[list[dict[str, str]] | None, str | None, bool]:
    """The agents, the rendered roster, and whether the walk skipped or half-read a profile.

    From the cache when nothing changed; a hit is clean by construction. The
    cache key is read before the walk and the unstamped profiles' own stamps
    after it, held to the walk's start, so neither can vouch for content a
    write during the walk replaced. Any fault on the walk (a profile's, the
    scope's or the management project's) keeps the roster out of the cache;
    the flag returned is the per-profile share, which is what a filter's
    no-match answer turns on.
    """
    global _cache
    root = PROFILES_BASE if base is None else base
    key = _cache_key_for(root)
    cached = _cache
    if (
        key is not None
        and cached is not None
        and cached[0] == key
        and _profile_stamps(root, [name for name, *_ in cached[1]]) == cached[1]
    ):
        return cached[2], cached[3], False
    faults_before, profile_faults_before = _faults, _profile_faults
    started = time.time_ns()
    agents = discover(root)
    roster = format_roster(agents)
    if roster is None:
        text = None
    else:
        extra = [b for b in (_format_host(read_host_identity(agents, root)), _format_scope(root)) if b]
        text = "\n\n".join([roster, *extra])
    degraded = _faults != faults_before
    if degraded and key is not None:
        # Every fallback path above logs its own line but the three in-profile
        # reads; this one says what the fault cost. A standing fault prints it
        # every turn, which is the signal that the walk is back to every turn.
        log(f"roster not cached: {_faults - faults_before} read fault(s) during this walk")
    if key is not None and agents is not None and not degraded:
        unstamped = _profile_stamps(root, _unstamped(agents), read_at=started)
        if unstamped is not None:
            _cache = (key, unstamped, agents, text)
    return agents, text, _profile_faults != profile_faults_before


def render(base: Path | None = None) -> str | None:
    """Discover and format in one call — what both consumers actually want.

    ``None`` means the roster could not be read at all. Both consumers treat
    that as "say nothing about the fleet" rather than "the fleet is empty".
    Cached per process on the tree's mtimes (``_cache_key_for``): a hit costs
    the stats and no reads.
    """
    return _discover_and_render(base)[1]


def render_filtered(query: str, base: Path | None = None) -> str | None:
    """The agents matching ``query`` (``format_matches``), from the same cache as render.

    A no-match from a walk that degraded is reported as unread (FILTER_UNREAD),
    never as "no such agent": the skipped profile may be the one asked for.
    """
    agents, _, degraded = _discover_and_render(base)
    return format_matches(agents, query, degraded=degraded)

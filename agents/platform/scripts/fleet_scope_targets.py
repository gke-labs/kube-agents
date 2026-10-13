#!/usr/bin/env python3
"""The projects a fleet audit sweeps, read from the install's declared scope.

The fleet audits resolved their project scope as the host project plus every
project `gcloud projects list` returns: every project the agent's identity can
see, which is not the scope the install declared. A project the identity could
list but `spec.scope` never named was swept, and a declared project the
identity could not list was not, so the boundary the operator drew for
discovery (docs/designs/multi-project-scope.md) did not bound the audits.

This module hands an audit the resolved set instead: the reconcile's snapshot,
`fleet_scope.json`, rewritten at the data volume's root on every run, lists each
project the scope resolved to with the outcome the reconcile read it with. An
audit sweeps the projects read `ok` or `api-disabled` and the management
project whatever its row read (the collectors read it themselves), names the
ones the scope declares but the reconcile could not read, so the run accounts
for them rather than reading as a full sweep, and lists nothing. An install that declares no scope has drawn
no boundary; the answer is None there, and the audit keeps the listing it had.
With no snapshot yet, or one that does not parse, the operator's render answers
instead: a present block is the management project alone, partial, until the
reconcile writes the resolved set.

Read in the agent pod, where the snapshot is, by the platform_control MCP
server's `fleet_scope` tool (platform_mcp_server.py), which hands the agent
the resolved set and the arguments to pass the collectors. The collectors
themselves cannot read the file: they run in the shell sandbox, whose
/opt/data is a separate volume at the same path (deploy/sandbox/entrypoint.sh
says nothing is copied across), so the agent carries the scope from the tool
to the collector's `--scope-projects` and `--scope-unread`. The reader is
deliberately independent of cluster_agent_reconcile.py, which writes the
snapshot and owns its shape: that module's import has side effects.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path

import fleet_scope_args

# Where the reconcile writes the snapshot: the data volume's root, beside the
# profiles directory. PLATFORM_AGENT_HOME names it in every process on the
# agent pod (docker-entrypoint.sh), where HERMES_HOME does not: the gateway
# runs with HERMES_HOME at the root, but a platform worker and the governance
# jobs' ticks run with HERMES_HOME at the profile home beneath it
# (profile_cron_tick.py), which is where the MCP server that reads this module
# runs for a worker, so
# a snapshot path keyed on HERMES_HOME would look a level too deep and find
# nothing. gitops_workspace.agent_home() reads the same variable for the same
# reason.
AGENT_HOME_ENV = "PLATFORM_AGENT_HOME"
DEFAULT_AGENT_HOME = "/opt/data"
SNAPSHOT_FILE = "fleet_scope.json"

# The snapshot's vocabulary (cluster_agent_reconcile.py owns it; the design's
# §5 is its contract). A row read `ok` holds clusters the install can reach;
# any other outcome names a project the scope declares and this install could
# not read this run. A `retiring` row is a project the scope no longer names,
# kept only until its profiles are pruned, so it is not in the sweep.
OUTCOME_OK = "ok"
# A declared project whose Kubernetes Engine API is off holds no GKE cluster
# and is swept: the GKE collectors count it empty rather than partial (the GCE
# and networking SOPs' "counts as empty, not skipped: recording it as a loss
# would pin every run partial"), and the Compute and networking audits read it
# like any other.
OUTCOME_API_DISABLED = "api-disabled"
# A folder or organisation whose search succeeded but whose members would
# cross `maxProjects`: the members all have rows at `over-cap`, so the lookup
# resolved and the gap is already in `unread` by name.
OUTCOME_OVER_CAP = "over-cap"
OUTCOME_UNKNOWN = "unknown"
# A declared project the reconcile has not resolved yet (the render answered).
OUTCOME_UNRESOLVED = "unresolved"
# The unread row every render answer and every carried container tick carries:
# the reconcile has not resolved the declaration yet, so the sweep is partial
# by construction, and `finish` resolves nothing on what the declaration names.
# fleet_scope_args owns the constant, because the collectors render it as its
# own sentence rather than as a project.
UNRESOLVED_SCOPE_ROW = fleet_scope_args.UNRESOLVED_SCOPE_ROW
RENDER_NOTE = "no reconcile snapshot answers; the render declares a scope, so the sweep is the management project alone, partial, until the next reconcile writes the resolved set"
RENDER_NUMERIC_HOST_NOTE = "no reconcile snapshot answers; the render declares a scope, and the management project is known here by number only, so nothing is swept until the next reconcile writes the resolved set by id"
# The lists under `declared` whose members get rows only on a readable tick:
# on a carried tick the reconcile resolves no container or selector, so a
# non-empty one is an unresolved part of the scope the sweep has to carry.
CONTAINER_AND_SELECTOR_KEYS = ("folders", "organizations", "sharedVpcHosts", "metricsScopes")
# The row the reconcile writes for the install's own project.
VIA_MANAGEMENT = "management"


class ScopeRenderUnreadable(Exception):
    """KUBEAGENTS_SCOPE_FILE names a render this process cannot read or parse.
    On the agent pod the operator always writes it, so this is a fault to
    report, not an install that declares no scope."""


# The collectors' two scope flags, which `collector_args` spells with the
# constants the collectors parse them by, so the two cannot drift apart.
SCOPE_PROJECTS_FLAG = fleet_scope_args.SCOPE_PROJECTS_FLAG
SCOPE_UNREAD_FLAG = fleet_scope_args.SCOPE_UNREAD_FLAG
STATE_IN_SCOPE = "in-scope"
# Whether the CR carried a spec.scope block this run, which the reconcile records
# beside `declared`: a present block with every list empty is the host-only
# boundary, not the absence of one. A run that could not read the block carries
# the last declaration and records present: false beside it; that is still a
# boundary. A snapshot from a reconcile that predates the key is read by its
# lists alone.
PRESENT_KEY = "present"
# The explicit projects the reconcile dropped on an exclude entry, by id or by
# number (cluster_agent_reconcile.py, SCOPE_EXCLUDED_KEY): skipped here, so
# the by-number match is made in the one place that knows the numbers. The
# glob match below is for a snapshot from before the key.
EXCLUDED_KEY = "excludedProjects"
# The reconcile's record of every folder, organisation and selector lookup this
# run, with its outcome: a lookup that failed carried the last members it had,
# which on a first tick or a just-added container is none, so no row stands for
# the members and the sweep must say so itself.
CONTAINERS_KEY = "containers"
# The reconcile's own answer to the question this module asks (cluster_agent_reconcile.py,
# SCOPE_BOUNDARY_KEY): a declaration is in force this run, read or carried from a run
# that read one. Keyed on first; the two keys above and the lists are for a snapshot
# written before it.
BOUNDARY_KEY = "boundary"
# The operator's render of spec.scope, the file the reconcile reads (the same
# KUBEAGENTS_SCOPE_FILE the agent pod's env names). Read here when no snapshot
# answers that a boundary is in force: a fresh install before its first
# reconcile tick, a snapshot that does not parse, one from before every flag
# above that declared nothing, or one that says `boundary: false`, since the
# render may have gained a block since that tick. It says whether a block is
# present; the resolved set is the reconcile's to write.
SCOPE_FILE_ENV = "KUBEAGENTS_SCOPE_FILE"
RENDER_PRESENT_KEY = "present"
# Where the management project's id is read when the render alone answers: the
# sweep is then that project, until the next tick writes the resolved set. One
# name: it is what the platform_control env block forwards to the only
# production reader, the fleet_scope tool (agents/platform/config.yaml).
MANAGEMENT_PROJECT_ENVS = ("GCP_PROJECT_ID",)

# The keys under `declared` whose presence means the install drew a boundary,
# for a snapshot without the present flag.
DECLARED_SCOPE_KEYS = ("projects", "folders", "organizations", "sharedVpcHosts", "metricsScopes")


@dataclass(frozen=True)
class ScopeTargets:
    """What the declared scope resolved to, for an audit's scope accounting."""

    # The projects to sweep, in the snapshot's order (the management project
    # first, then the explicit projects, the selectors' and the containers'
    # members, as the reconcile lists them): the rows read `ok`, and the rows
    # read `api-disabled`, which the collectors count empty or read as usual.
    projects: tuple[str, ...]
    # Declared projects this install could not read, with the outcome the
    # reconcile recorded: `denied`, `unreachable`, `over-cap`.
    unread: tuple[tuple[str, str], ...]
    resolved_at: str | None
    path: str
    # Set when the render answered instead of a snapshot.
    note: str | None = None

    def collector_args(self) -> str:
        """The arguments that hand this scope to a collector: `--scope-projects`
        with the sweep, and `--scope-unread` naming each declared project the
        install could not read, as `project=outcome`. With nothing readable the
        unread flag goes alone, so a collector handed it still knows a scope
        was declared and reports that rather than listing. Never empty: a
        declared scope with no row at all hands the collector the fixed
        unresolved row, so it reports "nothing readable" rather than telling
        the agent to call the tool it has just called."""
        args = []
        if self.projects:
            args.append(f"{SCOPE_PROJECTS_FLAG} {','.join(self.projects)}")
        unread = self.unread or ((UNRESOLVED_SCOPE_ROW, OUTCOME_UNRESOLVED),) if not self.projects else self.unread
        if unread:
            args.append(f"{SCOPE_UNREAD_FLAG} " + ",".join(f"{project}={outcome}" for project, outcome in unread))
        return " ".join(args)


def snapshot_path(agent_home: str | os.PathLike | None = None) -> Path:
    """Where the reconcile's snapshot lives: the data volume's root, as
    PLATFORM_AGENT_HOME names it, or the root given."""
    root = agent_home if agent_home is not None else (os.environ.get(AGENT_HOME_ENV) or DEFAULT_AGENT_HOME)
    return Path(root) / SNAPSHOT_FILE


def _read_render() -> dict | None:
    """The operator's render of spec.scope; None when no render is named (a
    checkout, an image ahead of its operator). A named render that cannot be
    read or parsed raises: answering "no scope" there would send the SOPs'
    manual path to the listing on an install that may well declare one."""
    render = os.environ.get(SCOPE_FILE_ENV)
    if not render:
        return None
    try:
        parsed = json.loads(Path(render).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ScopeRenderUnreadable(f"{SCOPE_FILE_ENV}={render} cannot be read: {e}") from e
    if not isinstance(parsed, dict):
        raise ScopeRenderUnreadable(f"{SCOPE_FILE_ENV}={render} is not a JSON object")
    return parsed


def _from_render(path: Path) -> ScopeTargets | None:
    """The answer when no snapshot answers: the render's boundary, the host
    swept and a fixed unresolved row plus every project the render declares
    carried as unread, so the run reads as partial rather than as a complete
    sweep of the host, until the reconcile writes the resolved set. None when
    no render is named or it declares nothing; a named render that cannot be
    read raises ScopeRenderUnreadable."""
    render = _read_render()
    if render is None or render.get(RENDER_PRESENT_KEY) is not True:
        return None
    host = next((os.environ.get(name) for name in MANAGEMENT_PROJECT_ENVS if os.environ.get(name)), None)
    note = RENDER_NOTE
    if host and host.isdigit():
        # The operator lets spec.harness.projectId be a project number; every
        # snapshot row and every --project check uses the id, so a number is
        # not handed on. Nothing is swept, and the row below keeps it partial.
        host = None
        note = RENDER_NUMERIC_HOST_NOTE
    exclude_patterns = [p for p in ((render.get("exclude") or {}).get("projects") or []) if isinstance(p, str)]
    declared = [str(p) for p in render.get("projects") or []
                if isinstance(p, str) and p != host and not any(fnmatch.fnmatchcase(p, pattern) for pattern in exclude_patterns)]
    # The fixed row first, so a render that names folders, organisations or
    # selectors alone is partial too; the explicit projects follow by name.
    return ScopeTargets(
        projects=(host,) if host else (),
        unread=((UNRESOLVED_SCOPE_ROW, OUTCOME_UNRESOLVED), *((p, OUTCOME_UNRESOLVED) for p in declared)),
        resolved_at=None,
        path=os.environ.get(SCOPE_FILE_ENV) or str(path),
        note=note,
    )


def declared_scope_targets(agent_home: str | os.PathLike | None = None) -> ScopeTargets | None:
    """The declared scope's resolved projects, or None when the install declared
    no scope, in which case the caller enumerates as it did before this module
    existed. With no snapshot, or a file that is not one, the operator's render
    answers (see `_from_render`); a named render that cannot be read raises."""
    path = snapshot_path(agent_home)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _from_render(path)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("projects"), list):
        return _from_render(path)
    declared = parsed.get("declared")
    if not isinstance(declared, dict):
        return _from_render(path)
    # The reconcile's own answer first: `boundary` is true when the block was
    # read this run or carried from a run that read one, and false for a
    # readable render with no block and for an install that never declared one.
    boundary = parsed.get(BOUNDARY_KEY)
    if boundary is False:
        # The reconcile read a render with no block last tick. The render may
        # have gained one since (a hand-applied CR given a scope; the sandbox
        # already says so through the operator), so the render decides until
        # the next tick writes the resolved set.
        return _from_render(path)
    if boundary is not True and not any(isinstance(declared.get(key), list) and declared.get(key) for key in DECLARED_SCOPE_KEYS):
        # A snapshot from before `boundary` (a reconcile that shipped before
        # the key) is read by its lists: a declaration that names something
        # is a boundary, and empty lists ask the render.
        return _from_render(path)
    projects: list[str] = []
    unread: list[tuple[str, str]] = []
    for row in parsed["projects"]:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if row.get("state", STATE_IN_SCOPE) != STATE_IN_SCOPE:
            continue
        project = str(row["id"])
        # The install's own project is always swept: the collectors read it
        # themselves and record what they could not, so one failed listing at
        # the reconcile tick must not stop every audit for an hour.
        if row.get("outcome") in (OUTCOME_OK, OUTCOME_API_DISABLED) or VIA_MANAGEMENT in (row.get("via") or []):
            projects.append(project)
        else:
            unread.append((project, str(row.get("outcome") or OUTCOME_UNKNOWN)))
    # A declared project with no row at all: the carried tick writes rows for
    # the management project and the projects that hold a profile, so a
    # declared project with no cluster (the GCE and networking streams'
    # ordinary target) would otherwise vanish from a complete-looking sweep.
    # An explicit project an exclude pattern matches gets no row on any tick
    # and is not unread: the operator left it out on purpose.
    exclude_patterns = [p for p in ((declared.get("exclude") or {}).get("projects") or []) if isinstance(p, str)]
    dropped = {p for p in (parsed.get(EXCLUDED_KEY) or []) if isinstance(p, str)}
    seen = set(projects) | {project for project, _ in unread}
    for project in declared.get("projects") or []:
        if not isinstance(project, str) or not project or project in seen:
            continue
        if project in dropped or any(fnmatch.fnmatchcase(project, pattern) for pattern in exclude_patterns):
            continue
        unread.append((project, OUTCOME_UNRESOLVED))
        seen.add(project)
    # A carried tick resolves no folder, organisation or selector, so their
    # members have no rows; a declaration that names one is partial until the
    # next readable tick, whatever the explicit projects say. A readable tick
    # whose lookup of one failed is partial for the same reason: the members
    # it carried, if any, have rows under the lookup's outcome, and the ones it
    # never had have none, so the lookup's own record decides. An over-cap
    # lookup resolved: every member has a row, already named under `unread`.
    carried_containers = parsed.get(PRESENT_KEY) is not True and any(isinstance(declared.get(key), list) and declared.get(key) for key in CONTAINER_AND_SELECTOR_KEYS)
    failed_lookup = any(isinstance(entry, dict) and entry.get("outcome") not in (OUTCOME_OK, OUTCOME_OVER_CAP) for entry in (parsed.get(CONTAINERS_KEY) or []))
    if carried_containers or failed_lookup:
        unread.append((UNRESOLVED_SCOPE_ROW, OUTCOME_UNRESOLVED))
    resolved_at = parsed.get("resolvedAt")
    return ScopeTargets(
        projects=tuple(projects),
        unread=tuple(unread),
        resolved_at=str(resolved_at) if isinstance(resolved_at, str) else None,
        path=str(path),
    )

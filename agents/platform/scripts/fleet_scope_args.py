#!/usr/bin/env python3
"""The two flags that carry the install's declared scope into a fleet-audit
collector, and their parsing.

The collectors run in the shell sandbox, whose data volume is not the agent
pod's, so they cannot read the reconcile's scope snapshot (fleet_scope.json).
The platform_control MCP server's `fleet_scope` tool reads it in the agent pod
and hands the agent `collector_args`; the agent appends those to the collector
command, and the collector sweeps exactly what they name. Shared here rather
than copied into each collector: the image and the shell sandbox both ship
this file under /opt/defaults/scripts beside the other scripts the collectors
import (credential_proxy_client.py), and a checkout runs it from the
repository through the same search path.
"""

from __future__ import annotations

import argparse
import os
import re

SCOPE_PROJECTS_FLAG = "--scope-projects"
SCOPE_UNREAD_FLAG = "--scope-unread"
SCOPE_PROJECTS_HELP = (
    "the install's declared scope, as the platform_control fleet_scope tool reports it: one comma-separated "
    "list of project IDs, passed once (a space-separated list only works quoted). Sweep exactly these, listing "
    "nothing; paste the tool's collector_args. Omit on an install that declares no scope, where the collector "
    "enumerates every project the identity can list"
)
SCOPE_UNREAD_HELP = (
    "project=outcome entries from the same tool: declared projects this install could not read, "
    "recorded as a coverage gap rather than silently absent"
)
# The partial-scope note a collector carries when the tool named unread projects.
DECLARED_SCOPE_UNREAD_NOTE = (
    "the install's declared scope names {count} project(s) this install could not read ({named}), "
    "per the platform_control fleet_scope tool; their clusters are not in this run."
)
# What a run with a declared scope but no readable project reports instead of
# sweeping: the collector exits with this rather than listing, which is the
# fall-through the flags exist to remove.
DECLARED_SCOPE_EMPTY_ERROR = (
    "the install's declared scope has no project this install could read this run{gap}; nothing was swept "
    "and nothing was listed"
)
# Set by the operator on the shell sandbox container (shell_sandbox_manifests.go,
# envScopeDeclared): "true" when the PlatformAgent carries a spec.scope block.
# A collector run there without the tool's flags must not list, because the
# listing is exactly the sweep the block exists to bound; it reports this
# instead and the agent passes collector_args. Unset, as in a checkout, means
# nothing is known and the flags alone decide.
SCOPE_DECLARED_ENV = "KUBEAGENTS_SCOPE_DECLARED"
# The same answer as a root-owned file, written by the sandbox entrypoint from
# the container's environment at boot and read before the variable: a session
# can unset or override an exported variable with one word, and the guard would
# be worth nothing against a run that did. Absent on a checkout and on the
# agent pod, where the variable (if any) decides.
SCOPE_DECLARED_FILE = "/run/kube-agents-sandbox/scope-declared"
SCOPE_DECLARED_TRUE = "true"
# A --project that is a project number the collector could not resolve to an id.
PROJECT_NUMBER_UNRESOLVED_ERROR = (
    "the project override names project number {number}, which this collector could not resolve to a project id; on an "
    "install with a declared scope pass the id the fleet_scope tool lists"
)
DECLARED_SCOPE_ARGS_MISSING_ERROR = (
    "this install declares a scope but the collector got no collector_args: call the platform_control "
    "fleet_scope tool and pass its collector_args verbatim; nothing was swept or listed"
)
# A `--project` on an install with a declared scope must name a project inside it.
DECLARED_SCOPE_OVERRIDE_OUTSIDE_ERROR = (
    "the project override names {project}, which the install's declared scope does not list ({projects}); nothing was swept. "
    "Pass the fleet_scope tool's collector_args, or override with a project it lists"
)
# The tail the project-level audits put on an unenumerated-projects row when the
# scope was declared: the fleet's size is known there, which is the point.
DECLARED_SCOPE_TAIL = "The declared scope names no other project."
# The unread row the fleet_scope tool emits when the reconcile has not resolved
# the declaration (a render answer, or a carried tick on a container- or
# selector-scoped install); not a project id, and rendered as the gap it is.
UNRESOLVED_SCOPE_ROW = "declared-scope"
UNRESOLVED_SCOPE_NOTE = (
    "the install's declared scope is not resolved yet (the reconcile has not written its resolved set); its "
    "members are not in this run"
)
# What every declared-scope note starts with; the collectors that classify
# the unenumerated row by its text key on it.
DECLARED_SCOPE_NOTE_PREFIX = "the install's declared scope"
# A --project that is a declared project this install could not read.
DECLARED_SCOPE_OVERRIDE_UNREAD_ERROR = (
    "the project override names {project}, which the install's declared scope lists but this install could not read "
    "({outcome}); nothing was swept. The fleet_scope tool's --scope-unread already records it as a coverage gap"
)
# A --project with no value, as `--project \"$VAR\"` with the variable unset produces.
EMPTY_PROJECT_OVERRIDE_ERROR = "the project override was given with no value; pass a project id, or omit the flag to sweep the declared scope"
SCOPE_ARG_SEPARATORS = r"[,\s]+"
SCOPE_UNREAD_OUTCOME_SEPARATOR = "="
SCOPE_UNREAD_DEFAULT_OUTCOME = "unknown"


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    """The two flags, on every collector alike."""
    parser.add_argument(SCOPE_PROJECTS_FLAG, default=None, help=SCOPE_PROJECTS_HELP)
    parser.add_argument(SCOPE_UNREAD_FLAG, default=None, help=SCOPE_UNREAD_HELP)


def parse_scope_projects(value: str | None) -> list[str]:
    """The project IDs `--scope-projects` carries, each once, in the order given;
    empty when it was not passed. A repeated id would otherwise be swept twice
    and every finding on it filed twice."""
    return list(dict.fromkeys(p for p in re.split(SCOPE_ARG_SEPARATORS, value or "") if p))


def parse_scope_unread(value: str | None) -> list[tuple[str, str]]:
    """The (project, outcome) pairs `--scope-unread` carries; an entry without an
    outcome reads as unknown."""
    unread = []
    for entry in (e for e in re.split(SCOPE_ARG_SEPARATORS, value or "") if e):
        project, _, outcome = entry.partition(SCOPE_UNREAD_OUTCOME_SEPARATOR)
        unread.append((project, outcome or SCOPE_UNREAD_DEFAULT_OUTCOME))
    return unread


def unread_note(unread: list[tuple[str, str]]) -> str | None:
    """The coverage gap the unread projects are, or None when there are none.
    The unresolved-scope row is not a project and is rendered as its own
    sentence, so a ledger never names a project that does not exist."""
    if not unread:
        return None
    projects = [(project, outcome) for project, outcome in unread if project != UNRESOLVED_SCOPE_ROW]
    sentences = []
    if len(projects) != len(unread):
        sentences.append(UNRESOLVED_SCOPE_NOTE)
    if projects:
        named = ", ".join(f"{project} ({outcome})" for project, outcome in projects)
        sentences.append(DECLARED_SCOPE_UNREAD_NOTE.format(count=len(projects), named=named))
    return "; ".join(sentences)


def sandbox_declares_scope() -> bool:
    """Whether the operator says this install declares a scope: the root-owned
    file the sandbox entrypoint writes, else the environment variable."""
    try:
        with open(SCOPE_DECLARED_FILE, encoding="utf-8") as handle:
            return handle.read().strip().lower() == SCOPE_DECLARED_TRUE
    except OSError:
        return os.environ.get(SCOPE_DECLARED_ENV, "").strip().lower() == SCOPE_DECLARED_TRUE


class DeclaredScope:
    """The scope a collector was handed, one instance per collector module:
    `set` from the parsed flags in main, read by the project resolver. Not
    declared (neither flag passed) means the collector enumerates as it did
    before scopes. Declared with no project means the install drew a boundary
    and could read nothing inside it: the collector reports `empty_error()`
    and sweeps nothing, never the listing."""

    def __init__(self) -> None:
        self.declared = False
        self.args_missing = False
        self.projects: list[str] | None = None
        self.unread: list[tuple[str, str]] = []

    def set(self, scope_projects: str | None, scope_unread: str | None) -> None:
        """Records `--scope-projects` and `--scope-unread`. Either flag passed,
        even blank, is a declared scope; `projects` is then the list, possibly
        empty. Neither flag on a sandbox whose operator says the install
        declares a scope (the root-owned SCOPE_DECLARED_FILE first, else
        SCOPE_DECLARED_ENV) is a declared scope too, with nothing to sweep and
        `args_missing` set, so the run reports rather than lists. `projects`
        is None only when nothing declares a scope."""
        flagged = scope_projects is not None or scope_unread is not None
        self.args_missing = not flagged and sandbox_declares_scope()
        self.declared = flagged or self.args_missing
        self.projects = parse_scope_projects(scope_projects) if self.declared else None
        self.unread = parse_scope_unread(scope_unread)

    def override_error(self, project: str) -> str | None:
        """Why a `--project` override cannot run, or None when it can: on an
        install with a declared scope the override must be handed the scope
        too (else the collector cannot tell inside from outside) and must name
        a project inside it. Without a declared scope an override is free."""
        if not self.declared:
            return None
        if self.args_missing:
            return DECLARED_SCOPE_ARGS_MISSING_ERROR
        if project.isdigit():
            # The tool emits ids; a collector with no resolver cannot tell which
            # id a number names, and must not guess it is outside the scope.
            return PROJECT_NUMBER_UNRESOLVED_ERROR.format(number=project)
        if project not in (self.projects or []):
            unread = dict(self.unread)
            if project in unread:
                return DECLARED_SCOPE_OVERRIDE_UNREAD_ERROR.format(project=project, outcome=unread[project])
            return DECLARED_SCOPE_OVERRIDE_OUTSIDE_ERROR.format(project=project, projects=", ".join(self.projects or []) or "none readable")
        return None

    def sweep_is_declared(self, override: object = None) -> bool:
        """Whether a run's sweep is the declared scope, resolved, as the tool
        handed it: declared, with the flags, not narrowed by an override, and
        not carrying the unresolved-scope row, under which the members are
        exactly what is unknown. The project-level audits key their
        unenumerated row's tail on it: "names no other project" is true only
        when all four hold."""
        return self.declared and not self.args_missing and not override and all(project != UNRESOLVED_SCOPE_ROW for project, _ in self.unread)

    def empty_error(self) -> str:
        """The error a declared scope with nothing to sweep reports: the flags
        were not passed on an install that declares a scope, or they named no
        readable project."""
        if self.args_missing:
            return DECLARED_SCOPE_ARGS_MISSING_ERROR
        note = self.note()
        return DECLARED_SCOPE_EMPTY_ERROR.format(gap=f" ({note})" if note else "")

    def note(self) -> str | None:
        """The coverage gap `--scope-unread` names, or None when every declared
        project was read."""
        return unread_note(self.unread)

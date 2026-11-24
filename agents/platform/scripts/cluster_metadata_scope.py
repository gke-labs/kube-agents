"""Hold GCP metadata reads about a cluster to spec.scope.exclude.clusters and spec.hold.

The broker refuses an excluded or held cluster on every path to its Kubernetes
API (scope_exclude). A gcloud read about a cluster through the GKE or Logging
API is a separate path, and before this it was held to the install's scope only
by a profile task's single-cluster confinement. Here it is held for every
caller:

  - `container clusters describe NAME` and `container node-pools list|describe
    --cluster NAME` name one cluster. An excluded or held one is refused. One
    named without its location (gcloud's default fills it in) is refused when
    any excluded or held cluster has that name in that project.
  - `logging read [FILTER]` gets a clause ANDed onto its filter that drops every
    excluded or held cluster's entries (scope_exclude.Exclusions.
    log_exclusion_clause), so the result can't include them whatever the
    filter says. The filter isn't parsed for cluster names, which would be a
    denylist on free text. A filter the clause can't be appended to safely
    (an open string or unbalanced parentheses, which could swallow the clause)
    is refused while anything is excluded or held.

Every other command, and every command on an install that excludes and holds
nothing, is returned unchanged.
"""

from __future__ import annotations

import os

import command_policy
import scope_exclude

# The flags that name a gcloud command's location and project, the way
# credential_proxy's get-credentials target reads them.
LOCATION_FLAGS = frozenset({"--location", "--region", "--zone", "-z"})
PROJECT_FLAG = "--project"
CLUSTER_FLAG = "--cluster"
# The bootstrap sets gcloud's default project to this, so it is the project a
# command that names none reads.
DEFAULT_PROJECT_ENV = "GKE_PROJECT_ID"

CLUSTERS_DESCRIBE = ("container", "clusters", "describe")
NODE_POOL_COMMANDS = frozenset({("container", "node-pools", "list"), ("container", "node-pools", "describe")})
LOGGING_READ = ("logging", "read")

# A cluster's full resource name, which gcloud also takes in place of a bare
# name: projects/P/locations/L/clusters/C (zones/ for the zonal form).
_RESOURCE_SEPARATOR = "/"
_RESOURCE_PROJECTS = "projects"
_RESOURCE_LOCATION_KEYS = frozenset({"locations", "zones"})
_RESOURCE_CLUSTERS = "clusters"
_RESOURCE_PARTS = 6
GCLOUD = "gcloud"
# The words that mark an argv this module would act on, for an argv whose flags
# can't be walked (_names_target_command).
_TARGET_WORD_SETS = (frozenset({"clusters", "describe"}), frozenset({"node-pools"}), frozenset({"logging", "read"}))

# The Logging query language's string quote, escape and grouping characters.
_QUOTE = '"'
_ESCAPE = "\\"
_OPEN = "("
_CLOSE = ")"
_SINGLE_QUOTE = "'"

UNREADABLE_FILTER = (
    "this install excludes or holds clusters, and the broker can't add its exclusion to this"
    " logging filter safely (an open string, a single quote or unbalanced parentheses); fix the filter"
)
UNREADABLE_COMMAND = (
    "this install excludes or holds clusters, and the broker can't read this command's flags"
    " to tell which cluster it names"
)


def _walk(argv: list[str]) -> tuple[dict[str, str], list[tuple[int, str]]] | None:
    """gcloud's flags (last value wins, booleans as "") and positionals with their
    argv index, using command_policy's arity; None when a flag's arity is unknown."""
    flags: dict[str, str] = {}
    positionals: list[tuple[int, str]] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token.startswith("-") and len(token) > 1:
            name, separator, value = token.partition("=")
            if name in command_policy._GCLOUD_BOOLEAN_FLAGS:
                flags[name] = ""
                index += 1
                continue
            if name in command_policy._GCLOUD_FLAGS_WITH_VALUE:
                if not separator:
                    value = argv[index + 1] if index + 1 < len(argv) else ""
                    index += 1
                flags[name] = value
                index += 1
                continue
            return None
        positionals.append((index, token))
        index += 1
    return flags, positionals


def _surface(positionals: list[tuple[int, str]]) -> list[tuple[int, str]]:
    start = 0
    while start < len(positionals) and positionals[start][1] in command_policy._GCLOUD_RELEASE_TRACKS:
        start += 1
    return positionals[start:]


def _flag(flags: dict[str, str], names: frozenset[str] | tuple[str, ...]) -> str:
    for name in names:
        if flags.get(name):
            return flags[name]
    return ""


def _resource_cluster(value: str) -> tuple[str, str, str] | None:
    """(project, location, cluster) from a full resource name, else None."""
    parts = value.strip(_RESOURCE_SEPARATOR).split(_RESOURCE_SEPARATOR)
    if (len(parts) == _RESOURCE_PARTS and parts[0] == _RESOURCE_PROJECTS
            and parts[2] in _RESOURCE_LOCATION_KEYS and parts[4] == _RESOURCE_CLUSTERS and all(parts)):
        return parts[1], parts[3], parts[5]
    return None


def _check_cluster(exclusions: scope_exclude.Exclusions, named: str, flags: dict[str, str]) -> None:
    resource = _resource_cluster(named)
    if resource is not None:
        exclusions.check(*resource)
        return
    project = _flag(flags, (PROJECT_FLAG,)) or os.environ.get(DEFAULT_PROJECT_ENV, "")
    location = _flag(flags, tuple(sorted(LOCATION_FLAGS)))
    if location:
        exclusions.check(project, location, named)
    else:
        exclusions.check_name(project, named)


def filter_is_appendable(text: str) -> bool:
    """Whether a Logging filter is safe to wrap in parentheses and AND onto: every
    double-quoted string closed, no single quote outside one, and parentheses
    outside strings balanced and never closing below zero."""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == _ESCAPE:
                escaped = True
            elif char == _QUOTE:
                in_string = False
            continue
        if char == _QUOTE:
            in_string = True
        elif char == _SINGLE_QUOTE:
            return False
        elif char == _OPEN:
            depth += 1
        elif char == _CLOSE:
            depth -= 1
            if depth < 0:
                return False
    return not in_string and depth == 0


def _combine(text: str, clause: str) -> str:
    """clause ANDed onto a filter, or the clause alone for an empty filter; a
    filter that could swallow the clause is refused."""
    if not text.strip():
        return clause
    if not filter_is_appendable(text):
        raise scope_exclude.ExcludedCluster(scope_exclude.RULE_EXCLUDED_CLUSTER, UNREADABLE_FILTER)
    return f"({text}) AND {clause}"


def filter_with_exclusion(text: str, exclusions: scope_exclude.Exclusions) -> str:
    """A Logging filter with the install's exclusion clause ANDed on (the relay's
    `entries:list` body); the filter itself when nothing is excluded or held."""
    clause = exclusions.log_exclusion_clause()
    return text if clause is None else _combine(text, clause)


def _with_log_exclusion(argv: list[str], surface: list[tuple[int, str]], clause: str) -> list[str]:
    """argv with clause ANDed onto its filter, the one positional after the verb."""
    filters = surface[len(LOGGING_READ):]
    if len(filters) > 1:
        raise scope_exclude.ExcludedCluster(scope_exclude.RULE_EXCLUDED_CLUSTER, UNREADABLE_FILTER)
    if not filters:
        verb_index = surface[len(LOGGING_READ) - 1][0]
        return [*argv[:verb_index + 1], clause, *argv[verb_index + 1:]]
    index, text = filters[0]
    return [*argv[:index], _combine(text, clause), *argv[index + 1:]]


def scoped_argv(argv: list[str], exclusions: scope_exclude.Exclusions) -> list[str]:
    """argv as the broker runs it against the install's exclusions and holds.

    Raises scope_exclude.ExcludedCluster (or HeldCluster, its subclass) when the
    command names an excluded or held cluster, or can't be read or rewritten
    while any is. Returns argv itself when nothing applies.
    """
    if not argv or os.path.basename(argv[0]) != GCLOUD:
        return argv
    walked = _walk(argv)
    if walked is None:
        # The read-only gate refuses an unreadable flag first; with enforcement
        # off, such a command still can't be tied to a cluster.
        words, _ = command_policy._gcloud_words_and_flag(argv)
        if words is None and _names_target_command(argv) and _anything_held(exclusions):
            raise scope_exclude.ExcludedCluster(scope_exclude.RULE_EXCLUDED_CLUSTER, UNREADABLE_COMMAND)
        return argv
    flags, positionals = walked
    surface = _surface(positionals)
    words = tuple(word for _, word in surface)
    if words[:len(CLUSTERS_DESCRIBE)] == CLUSTERS_DESCRIBE:
        names = words[len(CLUSTERS_DESCRIBE):]
        if names:
            _check_cluster(exclusions, names[0], flags)
        else:
            exclusions.check_unnamed()
        return argv
    if words[:len(next(iter(NODE_POOL_COMMANDS)))] in NODE_POOL_COMMANDS:
        cluster = flags.get(CLUSTER_FLAG, "")
        if cluster:
            _check_cluster(exclusions, cluster, flags)
        else:
            # gcloud's container/cluster default, which the broker can't see.
            exclusions.check_unnamed()
        return argv
    if words[:len(LOGGING_READ)] == LOGGING_READ:
        clause = exclusions.log_exclusion_clause()
        if clause is None:
            return argv
        return _with_log_exclusion(argv, surface, clause)
    return argv


def _names_target_command(argv: list[str]) -> bool:
    present = set(argv)
    return any(words <= present for words in _TARGET_WORD_SETS)


def _anything_held(exclusions: scope_exclude.Exclusions) -> bool:
    try:
        return exclusions.log_exclusion_clause() is not None
    except scope_exclude.ExcludedCluster:
        return True


"""Hold GCP metadata reads about a cluster to spec.scope.exclude.clusters.

The broker refuses an excluded cluster on every path to its Kubernetes
API (scope_exclude). A gcloud read about a cluster through the GKE or Logging
API is a separate path, and before this it was held to the install's scope only
by a profile task's single-cluster confinement. Here it is held for every
caller. spec.hold is not consulted: a hold pauses the agents' reach into a
cluster and leaves its metadata readable (scope_exclude.Exclusions.check's
include_holds).

  - `container clusters describe NAME` and `container node-pools list|describe
    --cluster NAME` name one cluster. An excluded one is refused. One
    named without its location (gcloud's default fills it in) is refused when
    any excluded cluster has that name in that project.
  - `logging read [FILTER]` gets a clause ANDed onto its filter that drops every
    excluded cluster's entries (scope_exclude.Exclusions.
    log_exclusion_clause, one regex term on the cluster name, since Cloud
    Logging refuses a filter with too many AND/OR terms), so the result can't
    include them whatever the filter says. The filter isn't parsed for cluster names, which would be a
    denylist on free text. A filter the clause can't be appended to safely
    (an open string, a single quote, a `--` comment or unbalanced parentheses,
    any of which could swallow the clause) is refused while anything is
    excluded.

Every other command, and every command on an install that excludes
nothing, is returned unchanged.
"""

from __future__ import annotations

import os

import command_policy
import scope_exclude

# The flags that name a gcloud command's location and project. A repeated flag
# keeps its last value, as gcloud's own parser does.
LOCATION_FLAGS = frozenset({"--location", "--region", "--zone"})
# gcloud's short aliases, read as the flag they stand for (task_confinement's
# GCLOUD_LONG_NAMES), so `--zone a -z b` is one flag whose last value wins, as
# gcloud reads it.
FLAG_ALIASES = {"-z": "--zone", "-h": "--help"}
PROJECT_FLAG = "--project"
CLUSTER_FLAG = "--cluster"
# The bootstrap sets gcloud's default project to this, so it is the project a
# command that names none reads.
DEFAULT_PROJECT_ENV = "GKE_PROJECT_ID"

CLUSTERS_DESCRIBE = ("container", "clusters", "describe")
NODE_POOL_COMMANDS = frozenset({("container", "node-pools", "list"), ("container", "node-pools", "describe")})
_NODE_POOL_WORDS = 3
LOGGING_READ = ("logging", "read")

# A cluster's full resource name, which gcloud also takes in place of a bare
# name: projects/P/locations/L/clusters/C (zones/ for the zonal form).
_RESOURCE_SEPARATOR = "/"
_RESOURCE_PROJECTS = "projects"
_RESOURCE_LOCATION_KEYS = frozenset({"locations", "zones"})
_RESOURCE_CLUSTERS = "clusters"
_RESOURCE_NODE_POOLS = "nodePools"
# `--help` (and `-h`) prints gcloud's own synopsis and reads nothing.
HELP_FLAGS = frozenset({"--help"})
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
# The Logging query language's comment marker: the rest of the line is ignored.
_COMMENT = "--"
_NEWLINE = "\n"

UNREADABLE_FILTER = (
    "this install excludes clusters, and the broker can't add its exclusion to this"
    " logging filter safely (an open string, a single quote, a -- comment or unbalanced parentheses);"
    " fix the filter"
)
UNREADABLE_COMMAND = (
    "this install excludes clusters, and the broker can't read this command's flags"
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
            canonical = FLAG_ALIASES.get(name, name)
            if name in command_policy._GCLOUD_BOOLEAN_FLAGS:
                flags[canonical] = ""
                index += 1
                continue
            if name in command_policy._GCLOUD_FLAGS_WITH_VALUE:
                if not separator:
                    value = argv[index + 1] if index + 1 < len(argv) else ""
                    index += 1
                flags[canonical] = value
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
    parts = value.split(_RESOURCE_SEPARATOR)
    if (len(parts) == _RESOURCE_PARTS and parts[0] == _RESOURCE_PROJECTS
            and parts[2] in _RESOURCE_LOCATION_KEYS and parts[4] == _RESOURCE_CLUSTERS
            and all(scope_exclude.NAME_PART.match(parts[index]) for index in (1, 3, 5))):
        return parts[1], parts[3], parts[5]
    return None


def _resource_node_pool(value: str) -> tuple[str, str, str] | None:
    """(project, location, cluster) from a node pool's full resource name, else None."""
    head, separator, pool = value.rpartition(_RESOURCE_SEPARATOR)
    head, separator2, key = head.rpartition(_RESOURCE_SEPARATOR)
    if not (separator and separator2 and key == _RESOURCE_NODE_POOLS and scope_exclude.NAME_PART.match(pool)):
        return None
    return _resource_cluster(head)


def _check_cluster(exclusions: scope_exclude.Exclusions, named: str, flags: dict[str, str]) -> None:
    resource = _resource_cluster(named)
    if resource is not None:
        exclusions.check(*resource, include_holds=False)
        return
    if not scope_exclude.NAME_PART.match(named):
        # Neither a bare name nor the canonical resource name (a selfLink URL,
        # a `..` path, an escape): one the broker can't tie to a cluster.
        exclusions.check_unnamed(include_holds=False)
        return
    project = _flag(flags, (PROJECT_FLAG,)) or os.environ.get(DEFAULT_PROJECT_ENV, "")
    given = [name for name in LOCATION_FLAGS if flags.get(name)]
    if len(given) > 1:
        # gcloud takes one of these per command; with two, which it reads isn't
        # something to guess at while a cluster is excluded.
        exclusions.check_unnamed(include_holds=False)
        return
    location = flags[given[0]] if given else ""
    if location:
        exclusions.check(project, location, named, include_holds=False)
    else:
        exclusions.check_name(project, named)


def filter_is_appendable(text: str) -> bool:
    """Whether a Logging filter is safe to wrap in parentheses and AND onto: every
    double-quoted string closed, no single quote or `--` comment outside one,
    and parentheses outside strings balanced and never closing below zero. A
    comment would swallow the closing parenthesis and the clause after it."""
    depth = 0
    in_string = False
    escaped = False
    for position, char in enumerate(text):
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
        elif char == _SINGLE_QUOTE or text.startswith(_COMMENT, position):
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
    # Each on its own line as well, so nothing on the filter's last line can
    # reach the clause.
    return f"({_NEWLINE}{text}{_NEWLINE}) AND {clause}"


def filter_with_exclusion(text: str, exclusions: scope_exclude.Exclusions) -> str:
    """A Logging filter with the install's exclusion clause ANDed on (the relay's
    `entries:list` body); the filter itself when nothing is excluded."""
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
    """argv with a `logging read` filter scoped to the install's exclusions.

    Called before task confinement and policy, so each judges the command that
    will run. Raises scope_exclude.ExcludedCluster when the filter can't be
    scoped while any cluster is excluded. Returns argv itself when nothing
    applies, an argv whose flags can't be walked included: the gates after
    refuse it with their own rule, and check_cluster_reads refuses it as
    unreadable if it gets that far.
    """
    walked = _gcloud_walk(argv)
    if walked is None:
        return argv
    flags, surface, words = walked
    if words[:len(LOGGING_READ)] != LOGGING_READ or _is_help(flags):
        return argv
    clause = exclusions.log_exclusion_clause()
    if clause is None:
        return argv
    return _with_log_exclusion(argv, surface, clause)


def check_cluster_reads(argv: list[str], exclusions: scope_exclude.Exclusions) -> None:
    """Raise scope_exclude.ExcludedCluster when a describe or node-pool read names
    an excluded cluster, names it in a spelling the broker can't read while any
    is excluded, or leaves it to gcloud's default while any is.

    Called after task confinement and the policy gates, on the argv that will
    run: a profile caller's then carries its target's project, location and
    cluster, and a command a gate refuses has had its own rule first. A
    `--help` query reads nothing and passes.
    """
    if not argv or os.path.basename(argv[0]) != GCLOUD:
        return
    walked = _gcloud_walk(argv)
    if walked is None:
        if _names_target_command(argv) and _anything_excluded(exclusions):
            raise scope_exclude.ExcludedCluster(scope_exclude.RULE_EXCLUDED_CLUSTER, UNREADABLE_COMMAND)
        return
    flags, surface, words = walked
    if _is_help(flags):
        return
    if words[:len(CLUSTERS_DESCRIBE)] == CLUSTERS_DESCRIBE:
        names = words[len(CLUSTERS_DESCRIBE):]
        if names:
            _check_cluster(exclusions, names[0], flags)
        else:
            exclusions.check_unnamed(include_holds=False)
        return
    if words[:_NODE_POOL_WORDS] in NODE_POOL_COMMANDS:
        cluster = flags.get(CLUSTER_FLAG, "")
        pools = words[_NODE_POOL_WORDS:]
        named_pool = pools[0] if pools else ""
        if _RESOURCE_SEPARATOR in named_pool:
            # A positional node pool's full name carries its own cluster.
            resource = _resource_node_pool(named_pool)
            if resource is None:
                exclusions.check_unnamed(include_holds=False)
            else:
                exclusions.check(*resource, include_holds=False)
        if cluster:
            _check_cluster(exclusions, cluster, flags)
        elif _RESOURCE_SEPARATOR not in named_pool:
            # gcloud's container/cluster default, which the broker can't see.
            exclusions.check_unnamed(include_holds=False)
        return


def _gcloud_walk(argv: list[str]) -> tuple[dict[str, str], list[tuple[int, str]], tuple[str, ...]] | None:
    """(flags, surface positionals, surface words) of a gcloud argv, or None for
    another executable or an argv whose flags can't be walked."""
    if not argv or os.path.basename(argv[0]) != GCLOUD:
        return None
    walked = _walk(argv)
    if walked is None:
        return None
    flags, positionals = walked
    surface = _surface(positionals)
    return flags, surface, tuple(word for _, word in surface)


def _is_help(flags: dict[str, str]) -> bool:
    return any(name in flags for name in HELP_FLAGS)


def _names_target_command(argv: list[str]) -> bool:
    present = set(argv)
    return any(words <= present for words in _TARGET_WORD_SETS)


def _anything_excluded(exclusions: scope_exclude.Exclusions) -> bool:
    try:
        return exclusions.log_exclusion_clause() is not None
    except scope_exclude.ExcludedCluster:
        return True

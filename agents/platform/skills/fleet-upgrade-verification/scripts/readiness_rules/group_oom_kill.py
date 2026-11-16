"""
Entry 15 of docs/designs/upgrade-failure-catalogue.md, the OOM killer kills the whole
container: from Kubernetes 1.28 the kubelet sets `memory.oom.group` on every container on a
cgroup v2 node, so a container running several processes loses all of them where it used to
lose one.

This rule is a heuristic on the command and the image, marked as such in its findings,
because the process count is not readable from the API: a supervisor program in the command
or args (`SUPERVISOR_PROGRAMS`), a shell `-c` script that forks a background job
(`readiness_rules.shell_forks`), or an image whose documented entrypoint is a supervisor
(`MULTI_PROCESS_IMAGE_REPOS`). The shape is a `risk` when the target turns the group kill on
for the pool: the pool moves to cgroup v2 at a target of 1.28 or later, or a cgroup v2 pool's
kubelet crosses 1.28; it is never `blocking`, so `CAN_BLOCK` is False. A pool already
group-killing today, or one whose node config sets `singleProcessOOMKill`, is a note; a pool
whose cgroup mode is unread is `unknown`. A shell script the scanner cannot read (an
unterminated quote, heredoc or expansion) is `unknown` on every pool where a fork would have
been a risk or the pool's mode is unread, never clean. The rule needs the DaemonSets and
CronJobs of the workload read; when that read failed it grades the Deployments and
StatefulSets it has and says so.

Sources: kubernetes/kubernetes#117070 (memory.oom.group from 1.28);
docs.cloud.google.com/kubernetes-engine/docs/how-to/node-system-config (singleProcessOOMKill).
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "group-oom-kill"
ENTRY = 15
CAN_BLOCK = False
# The reads this rule needs beyond the PDB read, as `read_errors` keys them.
TEMPLATE_READ_KINDS = ("daemonset", "cronjob")
HEURISTIC_TEXT = "heuristic: the process count is not readable from the API"
SUPERVISOR_SHAPE = "runs {program}"
SHELL_FORK_SHAPE = "runs a shell script that forks a background job"
# The shape `multi_process_shape` returns for a shell `-c` script `shell_forks` cannot read;
# `evaluate` files it as unknown where a fork would have mattered.
SHAPE_UNREADABLE = "runs a shell script the rule cannot read (an unterminated quote, heredoc or expansion)"
IMAGE_SHAPE = "runs {repository}, whose entrypoint is a supervisor"
RISK_MOVES_DETAIL = "container {container} {shape} ({heuristic}) on pool {pool}, which {reason} at the target; a kubelet at {target} then sets memory.oom.group and an OOM kill takes every process in the container"
RISK_CROSSES_DETAIL = "container {container} {shape} ({heuristic}) on pool {pool}, on cgroup v2 with a kubelet at {pool_version}; from 1.{minor} the kubelet sets memory.oom.group and an OOM kill takes every process in the container"
UNKNOWN_DETAIL = "container {container} {shape} ({heuristic}) on pool {pool}: {reason}"
UNREADABLE_DETAIL = "container {container} {shape} on pool {pool}; whether it forks a background job, and so whether a group OOM kill at the target takes more than one process, is unknown"
NOTE_ALREADY = "{kind} {object} container {container} {shape} on pool {pool}, on cgroup v2 with a kubelet at {pool_version}, which already group-kills; not an upgrade risk"
NOTE_SINGLE_PROCESS = "{kind} {object} container {container} {shape} on pool {pool}, whose node config sets singleProcessOOMKill; not an upgrade risk"
NOTE_STAYS = "{kind} {object} container {container} {shape} on pool {pool}; {reason}; not a risk at this target"
NOTE_BELOW_KUBELET = "{kind} {object} container {container} {shape} on pool {pool}; the target {target} is below 1.{minor}, where the kubelet starts the group kill; not a risk at this target"


def _program(word: str) -> str:
    return word.rsplit(rules.PATH_SEPARATOR, 1)[-1]


def _repository(image: str) -> str:
    """`docker.io/phusion/baseimage:focal` -> `docker.io/phusion/baseimage`."""
    name = image.partition(rules.IMAGE_DIGEST_SEPARATOR)[0]
    path, sep, tag = name.rpartition(rules.IMAGE_TAG_SEPARATOR)
    return path if sep and rules.PATH_SEPARATOR not in tag else name


def multi_process_shape(container: dict) -> str | None:
    """Why the container looks multi-process, or None; SHAPE_UNREADABLE when its shell `-c`
    script cannot be read (`shell_forks` returned None) and nothing else about the
    container decides, which `evaluate` files as unknown rather than clean."""
    command = [str(w) for w in (container.get("command") or []) + (container.get("args") or [])]
    programs = [_program(w) for w in command]
    for program in programs:
        if program in tables.SUPERVISOR_PROGRAMS:
            return SUPERVISOR_SHAPE.format(program=program)
    unreadable = False
    for script in rules.shell_scripts(command):
        forks = rules.shell_forks(script)
        if forks:
            return SHELL_FORK_SHAPE
        unreadable = unreadable or forks is None
    repository = _repository(container.get("image") or "")
    for repo in tables.MULTI_PROCESS_IMAGE_REPOS:
        if repository == repo or repository.endswith(rules.PATH_SEPARATOR + repo):
            return IMAGE_SHAPE.format(repository=repo)
    return SHAPE_UNREADABLE if unreadable else None


def _grade(pool: dict, target, transition: str) -> str:
    """Which text a pool gets: a risk form, a note form, or the unknown form."""
    linux = rules.pool_config(pool).get("linuxNodeConfig") or {}
    if linux.get(tables.SINGLE_PROCESS_OOM_KILL_FIELD) is True:
        return NOTE_SINGLE_PROCESS
    if transition == rules.CGROUP_UNKNOWN:
        return UNKNOWN_DETAIL
    if transition == rules.CGROUP_STAYS_V1:
        return NOTE_STAYS
    if target is not None and target[1] < tables.GROUP_OOM_KILL_KUBELET_MINOR:
        return NOTE_BELOW_KUBELET
    if transition == rules.CGROUP_MOVES:
        return RISK_MOVES_DETAIL
    parsed = pool.get("parsed")
    if parsed is not None and target is not None and parsed[1] < tables.GROUP_OOM_KILL_KUBELET_MINOR:
        return RISK_CROSSES_DETAIL
    return NOTE_ALREADY


def _unknown(obj: dict, detail: str, **extra) -> dict:
    finding = rules.new_finding(RULE_ID, ENTRY, rules.GRADE_UNKNOWN, obj, detail, **extra)
    finding["reason"] = rules.describe(finding)
    return finding


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = rules.new_result()
    items = rules.read_items(read)
    unread_detail = rules.workloads_unread_detail(read, TEMPLATE_READ_KINDS)
    if unread_detail:
        result["unknown"].append(rules.rule_unknown(RULE_ID, ENTRY, unread_detail))
    for obj, spec, _ in rules.templates(items):
        for container in rules.containers(spec):
            shape = multi_process_shape(container)
            if shape is None:
                continue
            container_name = container.get("name", "")
            for pool in rules.pools_for_template(spec, context.get(rules.CONTEXT_POOLS) or []):
                transition, reason = rules.cgroup_transition(pool, target)
                pool_name = pool.get("name", "")
                form = _grade(pool, target, transition)
                if shape == SHAPE_UNREADABLE:
                    # Where a fork would have been a risk, or the pool's mode is unread, the
                    # unreadable script is the unknown; where the pool already group-kills or
                    # stays on v1 at the target, a fork would change nothing and nothing is filed.
                    if form in (RISK_MOVES_DETAIL, RISK_CROSSES_DETAIL, UNKNOWN_DETAIL):
                        text = UNREADABLE_DETAIL.format(container=container_name, shape=shape, pool=pool_name)
                        result["unknown"].append(_unknown(obj, text, heuristic=True, container=container_name, shape=shape, pool=pool_name))
                    continue
                text = form.format(
                    kind=obj["kind"], object=obj["object"], container=container_name, shape=shape, heuristic=HEURISTIC_TEXT,
                    pool=pool_name, reason=reason, pool_version=pool.get("version"), target=rules.minor_text(target), minor=tables.GROUP_OOM_KILL_KUBELET_MINOR,
                )
                if form in (RISK_MOVES_DETAIL, RISK_CROSSES_DETAIL):
                    result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, text, heuristic=True, container=container_name, shape=shape, pool=pool_name))
                elif form == UNKNOWN_DETAIL:
                    result["unknown"].append(_unknown(obj, text, heuristic=True, container=container_name, shape=shape, pool=pool_name))
                else:
                    result["notes"].append(text)
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)

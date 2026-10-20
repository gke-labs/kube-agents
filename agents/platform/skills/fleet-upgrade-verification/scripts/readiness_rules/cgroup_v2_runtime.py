"""
Entry 14 of docs/designs/upgrade-failure-catalogue.md, cgroup v2 under a runtime that cannot
read it: a JDK below 8u372, 11.0.16 or 15, or a .NET below 5.0, reads its memory limit from
cgroup v1 paths; on a cgroup v2 node it sizes its heap from the machine and is OOM-killed.

The grade needs two reads: the image tag for the runtime version (`upgrade_shape_tables`,
pinned tags only; a .NET Framework image is outside the rule, since it runs in Windows
containers only, on pools with no cgroup) and the pool for whether the upgrade to the target moves it to cgroup v2
(`effectiveCgroupMode` and the pin, against GKE's 1.33 migration and 1.35 removal). A runtime
below the floor on a pool that moves is a `risk`; a known runtime whose tag names no version
on such a pool is `unknown`; a pool whose mode the record does not carry is `unknown`; a pool
already on cgroup v2, or staying on v1 at the target, is a note, because the upgrade changes
nothing for it. The rule needs the DaemonSets and CronJobs of the workload read; when that
read failed it grades the Deployments and StatefulSets it has and says so. Nothing here
blocks: the container is killed after the upgrade, which GKE completes, so `CAN_BLOCK` is
False and the rule's unknowns stay in the note.

Sources: kubernetes.io/docs/concepts/architecture/cgroups/ (the runtime floors);
docs.cloud.google.com/kubernetes-engine/docs/how-to/migrate-cgroupv2 (the GKE timeline);
dotnet/runtime#30337 (.NET 5.0).
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "cgroup-v2-runtime"
ENTRY = 14
CAN_BLOCK = False
# The reads this rule needs beyond the PDB read, as `read_errors` keys them.
TEMPLATE_READ_KINDS = ("daemonset", "cronjob")
RUNTIME_BELOW = "below"
RUNTIME_OK = "ok"
RUNTIME_UNVERSIONED = "unversioned"
JDK8_TEXT = "JDK 8u{update}, below 8u{floor}"
JDK11_TEXT = "JDK 11.{minor}.{patch}, below 11.{floor_minor}.{floor_patch}"
JDK_OLD_MAJOR_TEXT = "JDK {major}, which predates cgroup v2 support (from JDK {first})"
JDK_UNVERSIONED_TEXT = "JDK {major} with no update in the tag"
JAVA_UNVERSIONED_TEXT = "a Java image whose tag names no version"
DOTNET_TEXT = ".NET {major}.{minor}, below {floor_major}.{floor_minor}"
DOTNET_UNVERSIONED_TEXT = "a .NET image whose tag names no version"
RISK_DETAIL = "container {container} runs {image} ({runtime}) on pool {pool}, which {reason} at the target; the heap is then sized from the node and the container is OOM-killed"
UNKNOWN_TAG_DETAIL = "container {container} runs {image} ({runtime}) on pool {pool}, which {reason} at the target; the tag does not say whether it reads cgroup v2"
UNKNOWN_POOL_DETAIL = "container {container} runs {image} ({runtime}) on pool {pool}: {reason}"
NOTE_ALREADY = "{kind} {object} container {container} runs {image} ({runtime}) on pool {pool}, {reason}; not an upgrade risk"
NOTE_STAYS = "{kind} {object} container {container} runs {image} ({runtime}) on pool {pool}; {reason}; not a risk at this target"
JDK_MAJOR_8 = 8
JDK_MAJOR_11 = 11


def runtime_verdict(image: str) -> tuple[str, str] | None:
    """(verdict, text) for a Java or .NET image; None for any other image."""
    repository, tag = rules.image_repository_and_tag(image)
    if repository in tables.JAVA_IMAGE_REPOS:
        m = tables.JAVA_TAG_RE.match(tag)
        if m:
            major, update, minor, patch = (int(x) if x is not None else None for x in m.groups())
            if major == JDK_MAJOR_8 and update is not None:
                if update < tables.JDK8_CGROUP_V2_UPDATE:
                    return RUNTIME_BELOW, JDK8_TEXT.format(update=update, floor=tables.JDK8_CGROUP_V2_UPDATE)
                return RUNTIME_OK, ""
            if major == JDK_MAJOR_11 and minor is not None:
                if (minor, patch) < tables.JDK11_CGROUP_V2_PATCH:
                    return RUNTIME_BELOW, JDK11_TEXT.format(minor=minor, patch=patch, floor_minor=tables.JDK11_CGROUP_V2_PATCH[0], floor_patch=tables.JDK11_CGROUP_V2_PATCH[1])
                return RUNTIME_OK, ""
        bare = tables.JAVA_MAJOR_TAG_RE.match(tag)
        if bare:
            major = int(bare.group(1))
            if major >= tables.JDK_FIRST_MAJOR_WITH_CGROUP_V2:
                return RUNTIME_OK, ""
            if major in (JDK_MAJOR_8, JDK_MAJOR_11):
                return RUNTIME_UNVERSIONED, JDK_UNVERSIONED_TEXT.format(major=major)
            return RUNTIME_BELOW, JDK_OLD_MAJOR_TEXT.format(major=major, first=tables.JDK_FIRST_MAJOR_WITH_CGROUP_V2)
        return RUNTIME_UNVERSIONED, JAVA_UNVERSIONED_TEXT
    if any(marker in image for marker in tables.DOTNET_IMAGE_REPO_MARKERS):
        if any(marker in image for marker in tables.DOTNET_FRAMEWORK_IMAGE_MARKERS):
            return None
        m = tables.DOTNET_TAG_RE.match(tag)
        if not m:
            return RUNTIME_UNVERSIONED, DOTNET_UNVERSIONED_TEXT
        version = (int(m.group(1)), int(m.group(2)))
        if version < tables.DOTNET_CGROUP_V2_VERSION:
            return RUNTIME_BELOW, DOTNET_TEXT.format(major=version[0], minor=version[1], floor_major=tables.DOTNET_CGROUP_V2_VERSION[0], floor_minor=tables.DOTNET_CGROUP_V2_VERSION[1])
        return RUNTIME_OK, ""
    return None


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
            image = container.get("image") or ""
            verdict = runtime_verdict(image)
            if verdict is None or verdict[0] == RUNTIME_OK:
                continue
            kind, runtime = verdict
            fields = {"container": container.get("name", ""), "image": image, "runtime": runtime}
            for pool in rules.pools_for_template(spec, context.get(rules.CONTEXT_POOLS) or []):
                transition, reason = rules.cgroup_transition(pool, target)
                fields["pool"] = pool.get("name", "")
                fields["reason"] = reason
                if transition == rules.CGROUP_MOVES:
                    if kind == RUNTIME_BELOW:
                        result["risks"].append(rules.new_finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, RISK_DETAIL.format(**fields), **fields))
                    else:
                        result["unknown"].append(_unknown(obj, UNKNOWN_TAG_DETAIL.format(**fields), **fields))
                elif transition == rules.CGROUP_UNKNOWN:
                    result["unknown"].append(_unknown(obj, UNKNOWN_POOL_DETAIL.format(**fields), **fields))
                elif kind == RUNTIME_BELOW and transition == rules.CGROUP_ALREADY_V2:
                    result["notes"].append(NOTE_ALREADY.format(kind=obj["kind"], object=obj["object"], **fields))
                elif kind == RUNTIME_BELOW:
                    result["notes"].append(NOTE_STAYS.format(kind=obj["kind"], object=obj["object"], **fields))
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)

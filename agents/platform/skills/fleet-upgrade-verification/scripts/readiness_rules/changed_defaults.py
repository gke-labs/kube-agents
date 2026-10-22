#!/usr/bin/env python3
"""
Entry 8 of docs/designs/upgrade-failure-catalogue.md: a default the target minor changes
under a workload nobody touched.

The rule reads `upgrade_shape_tables.DEFAULT_CHANGES_BY_MINOR`, the per-minor table of
defaults that change, and grades the minors this upgrade crosses: from the control plane's
minor to the target for an admission default, from the lowest node pool's minor to the
target for a kubelet default.

Pod Security Admission is the entry's own example. A namespace whose
`pod-security.kubernetes.io/enforce-version` label is `latest`, or absent, follows the
running minor, so the rule set applied to its pods changes under it at every minor that
changes the Pod Security Standards; a namespace pinned to a named version keeps that
version's rule set and is the safe case. For each namespace that follows latest and
enforces a level a crossed minor tightens, the rule files a risk naming the namespace and
the setting; a crossed minor that only widens an allowlist is a note. Workloads carrying a
gitRepo volume (the PDB read's Deployments and StatefulSets, the namespaced read's
DaemonSets and CronJobs) are graded the same way against the kubelet-side rows.

A target past the table's `as_of` is graded on the minors the table covers, with a note
that the table ends there; the refresh is a row per later minor from its release notes,
beside the rows that exist. The Namespaces come from the namespaced read: when it failed,
the labels go ungraded as an `unknown`, which, since `CAN_BLOCK` is False (a changed default
rejects a pod after the upgrade, it does not stop the upgrade), goes in the note and leaves
the verdict to the other rules.

Sources: the rows' own, in upgrade_shape_tables.py (the Pod Security Standards page, the
Kubernetes 1.33 changelog, the 1.36 release notes).
"""

import re

import readiness_rules
import upgrade_shape_tables as tables
from readiness_rules import finding

RULE_ID = "changed-defaults"
ENTRY = 8
CAN_BLOCK = False

NAMESPACE_KIND = "Namespace"
NAMESPACE_READ_KIND = "namespace"
PSA_LABEL_PREFIX = "pod-security.kubernetes.io/"
PSA_ENFORCE = "enforce"
PSA_VERSION_SUFFIX = "-version"
PSA_LATEST = "latest"
PSA_UNSET = "unset (latest)"
# A pinned version is `v<major>.<minor>`, as the admission controller spells it.
PSA_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)$")
VOLUMES_KEY = "volumes"
GITREPO_KEY = "gitRepo"
# The two finding kinds this rule files.
KIND_NAMESPACE = "namespace"
KIND_WORKLOAD = "workload"

NOTE_NO_TARGET = "no target; which minors the upgrade crosses needs one, so defaults were not graded"
NOTE_PAST_TABLE = "the defaults table ends at {as_of} (read {read_on}); minors after it up to the target {target} are not graded, and a row per minor from its release notes refreshes the table"
OBJECTS_NOT_READ = "cluster objects not read ({reason}); namespace admission labels and workload volumes not graded"
NAMESPACES_NOT_READ = "namespaces not read ({reason}); admission labels not graded"
CONTROL_PLANE_UNPARSABLE = "control plane version {version!r} unparsable; the crossed minors could not be measured"
SETTING_PSA = "{prefix}{mode}={level}, {prefix}{mode}{suffix}={version}"
SETTING_GITREPO = "gitRepo volume {volume}"
NAMESPACE_SUBJECT = "namespace {name}"
CHANGE_FORMAT = "{minor}: {change} ({source})"
RISK_DETAIL_PSA = "follows the running minor, so the enforced {level} rule set changes at {minors}"
RISK_DETAIL_GITREPO = "the node pools cross {minors}"
NOTE_PSA_LOOSENS = "{namespace} follows latest; {minors} only widen{plural} the {level} allowlist, which rejects nothing new"
NOTE_PSA_UNCHANGED = "{count} namespace{plural} follow{verb} latest with no enforced check changing between {from_minor} and {to_minor} per the table"
NOTE_PSA_PINNED = "{count} namespace{plural} pinned to a Pod Security version"
NOTE_PRIVILEGED = "{count} namespace{plural} enforce{verb} privileged, which no check changes"
NOTE_NO_CROSSING = "control plane already at the target minor; no admission default crossed"
LIST_SEPARATOR = ", "
NOUN_PLURAL = "s"
VERB_SINGULAR = "s"
WIDEN_SINGULAR = "s"
DESCRIBE_FORMAT = "{subject}: {setting}: {detail}; {changes}"


def crossed_minors(from_minor: tuple | None, target_minor: tuple) -> list[tuple]:
    """The table's minors in (from, target]; every minor above an unparsable floor up to the target."""
    minors = []
    for minor in sorted(tables.DEFAULT_CHANGES_BY_MINOR):
        if minor > target_minor:
            continue
        if from_minor is not None and minor <= from_minor:
            continue
        minors.append(minor)
    return minors


def _rows(minors: list[tuple], detector: str, level: str | None = None) -> list[tuple[tuple, dict]]:
    rows = []
    for minor in minors:
        for row in tables.DEFAULT_CHANGES_BY_MINOR.get(minor, []):
            if row["detector"] != detector:
                continue
            if level is not None and level not in row["levels"]:
                continue
            rows.append((minor, row))
    return rows


def _lowest_pool_minor(member: dict) -> tuple | None:
    minors = [tables.parse_minor(p.get("version")) for p in member.get("node_pools") or []]
    minors = [m for m in minors if m is not None]
    return min(minors) if minors else None


def _psa_version(labels: dict, mode: str) -> tuple[str, bool]:
    """(label value as shown, follows_latest) for one mode's `-version` label."""
    value = labels.get(PSA_LABEL_PREFIX + mode + PSA_VERSION_SUFFIX)
    if value is None:
        return PSA_UNSET, True
    if str(value) == PSA_LATEST:
        return PSA_LATEST, True
    return str(value), PSA_VERSION_RE.match(str(value)) is None


def _changes(rows: list[tuple[tuple, dict]]) -> list[dict]:
    return [{"minor": tables.format_minor(minor), "setting": row["setting"], "change": row["change"], "source": row["source"]} for minor, row in rows]


def _gitrepo_volumes(item: dict) -> list[str]:
    spec = finding.pod_spec(item)
    if spec is None:
        return []
    return [str(v.get("name", "")) for v in spec.get(VOLUMES_KEY) or [] if isinstance(v, dict) and isinstance(v.get(GITREPO_KEY), dict)]


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = finding.empty_result()
    if target is None:
        finding.add_note(result, NOTE_NO_TARGET)
        return result
    target_minor = (target[0], target[1])
    if target_minor > tables.DEFAULT_CHANGES_AS_OF:
        finding.add_note(result, NOTE_PAST_TABLE.format(as_of=tables.format_minor(tables.DEFAULT_CHANGES_AS_OF), read_on=tables.DEFAULT_CHANGES_READ_ON, target=tables.format_minor(target_minor)))
    if read is None or read.get("items") is None:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, OBJECTS_NOT_READ.format(reason=readiness_rules.read_error(read, NAMESPACE_READ_KIND))))
        return result
    namespaces_error = readiness_rules.read_error(read, NAMESPACE_READ_KIND)
    if namespaces_error:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, NAMESPACES_NOT_READ.format(reason=namespaces_error)))
    master_minor = tables.parse_minor(member.get("control_plane_version"))
    if master_minor is None:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, CONTROL_PLANE_UNPARSABLE.format(version=member.get("control_plane_version"))))
        return result
    apiserver_minors = crossed_minors(master_minor, target_minor)
    kubelet_minors = crossed_minors(_lowest_pool_minor(member) or master_minor, target_minor)

    pinned = privileged = unchanged = 0
    for item in readiness_rules.read_items(read):
        if not isinstance(item, dict):
            continue
        if item.get("kind") == NAMESPACE_KIND:
            meta = item.get("metadata") or {}
            labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
            level = labels.get(PSA_LABEL_PREFIX + PSA_ENFORCE)
            if not level:
                continue
            if level == tables.PSA_LEVEL_PRIVILEGED:
                privileged += 1
                continue
            version_text, follows = _psa_version(labels, PSA_ENFORCE)
            if not follows:
                pinned += 1
                continue
            rows = _rows(apiserver_minors, tables.DETECTOR_POD_SECURITY, level)
            tightening = [(m, r) for m, r in rows if r["tightens"]]
            loosening = [(m, r) for m, r in rows if not r["tightens"]]
            setting = SETTING_PSA.format(prefix=PSA_LABEL_PREFIX, mode=PSA_ENFORCE, level=level, suffix=PSA_VERSION_SUFFIX, version=version_text)
            name = meta.get("name", "")
            if tightening:
                minors = LIST_SEPARATOR.join(tables.format_minor(m) for m, _ in tightening)
                result[finding.RESULT_RISKS].append(
                    {
                        "tier": finding.TIER_RISK,
                        "kind": KIND_NAMESPACE,
                        "namespace": name,
                        "subject": NAMESPACE_SUBJECT.format(name=name),
                        "setting": setting,
                        "level": level,
                        "version_label": version_text,
                        "changes": _changes(tightening),
                        "detail": RISK_DETAIL_PSA.format(level=level, minors=minors),
                    }
                )
            elif loosening:
                minors = LIST_SEPARATOR.join(tables.format_minor(m) for m, _ in loosening)
                finding.add_note(result, NOTE_PSA_LOOSENS.format(namespace=name, minors=minors, plural="" if len(loosening) > 1 else WIDEN_SINGULAR, level=level))
            else:
                unchanged += 1
            continue
        for volume in _gitrepo_volumes(item):
            rows = _rows(kubelet_minors, tables.DETECTOR_GITREPO_VOLUME)
            if not rows:
                continue
            workload = finding.workload_label(item)
            result[finding.RESULT_RISKS].append(
                {
                    "tier": finding.TIER_RISK,
                    "kind": KIND_WORKLOAD,
                    "workload": workload,
                    "subject": workload,
                    "setting": SETTING_GITREPO.format(volume=volume),
                    "changes": _changes(rows),
                    "detail": RISK_DETAIL_GITREPO.format(minors=LIST_SEPARATOR.join(tables.format_minor(m) for m, _ in rows)),
                }
            )

    if unchanged:
        from_text = tables.format_minor(master_minor)
        finding.add_note(result, NOTE_PSA_UNCHANGED.format(count=unchanged, plural="" if unchanged == 1 else NOUN_PLURAL, verb=VERB_SINGULAR if unchanged == 1 else "", from_minor=from_text, to_minor=tables.format_minor(target_minor)))
    if pinned:
        finding.add_note(result, NOTE_PSA_PINNED.format(count=pinned, plural="" if pinned == 1 else NOUN_PLURAL))
    if privileged:
        finding.add_note(result, NOTE_PRIVILEGED.format(count=privileged, plural="" if privileged == 1 else NOUN_PLURAL, verb=VERB_SINGULAR if privileged == 1 else ""))
    if not apiserver_minors and master_minor >= target_minor:
        finding.add_note(result, NOTE_NO_CROSSING)
    return result


def describe(item: dict) -> str:
    changes = LIST_SEPARATOR.join(CHANGE_FORMAT.format(minor=c["minor"], change=c["change"], source=c["source"]) for c in item["changes"])
    return DESCRIBE_FORMAT.format(subject=item["subject"], setting=item["setting"], detail=item["detail"], changes=changes)

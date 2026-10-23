#!/usr/bin/env python3
"""
Entry 6 of docs/designs/upgrade-failure-catalogue.md: a client still writes through an API
version the target minor no longer serves.

The API server stamps every request for a version it will stop serving with the audit
annotation `k8s.io/removed-release=<minor>`, which Cloud Logging keeps as an entry label.
The rule reads those entries through the shared removed-release read (`audit_log.py`: a
paged `gcloud logging read` over the seven days ending at the evaluation instant, the
provider's principals excluded) and grades each caller against the running control plane
and the target. An operator caller of an API removed after the control plane's minor and
at or before the target's, whose latest write is within the last 48 hours, blocks the
upgrade, named with its principal, user agent, API and release, and with the replacement
where the GitOps scan's removal table names one; a caller silent for longer is a risk
naming its last write, because a migrated caller must not block for a week. A removal the
control plane has already crossed is a note: the API is gone, its writes now fail unstamped,
and the entries the seven-day window still holds predate the crossing, so they are not this
upgrade's block. A removal after the target, a provider caller (GKE moves its own
components), and a principal the rule cannot place are risks, never blockers. The control
plane's minor is the context's `master`, else the member's `control_plane_version`; with
neither, every removal at or before the target is graded as this upgrade's.

The log carries writes only: a caller that only reads a removed API is not in it, and GKE
Deprecation Insights are the cross-check. A read that fails or times out is `unknown` with
the reason, graded on the pages before it; a read that still filled its last page is graded
on what it saw, with a note. The rule needs none of the kubectl reads.

Sources: the deprecation annotations, kubernetes.io/docs/reference/using-api/deprecation-guide/;
the replacement column, removed_apis.json beside the scripts.
"""

from datetime import timedelta

import readiness_rules
import upgrade_shape_tables as tables
from readiness_rules import audit_log, finding

RULE_ID = "removed-api-callers"
ENTRY = 6
CAN_BLOCK = True

# A caller blocks only while it is still writing: a write within this many hours of the
# evaluation instant. Two days covers a daily job that ran yesterday; anything older
# within the seven-day window is graded as a risk naming its last write.
BLOCKING_RECENCY_HOURS = 48

AUDIT_READ_FAILED = "removed-API callers: {reason}"
BLOCKS_DETAIL = "removed at or before the target {target}, last write {last_seen}; this caller fails the moment the control plane reaches it"
STALE_DETAIL = "removed at or before the target {target}, but the last write was {last_seen}, more than {hours}h before {at}; a risk until a write recurs"
LAST_SEEN_UNPARSABLE_DETAIL = "removed at or before the target {target}; the last write's time {last_seen!r} did not parse, so recency could not be judged"
AFTER_TARGET_DETAIL = "removed after the target {target}; a later upgrade removes it"
PROVIDER_DETAIL = "the provider's caller; GKE moves it with the control plane, so it is not the operator's to fix"
UNPLACED_DETAIL = "a principal of a class this rule cannot place ({principal}); named, not filed as a blocker"
RELEASE_UNPARSABLE_DETAIL = "the stamped release {release!r} did not parse; not graded against the target"
NO_TARGET_DETAIL = "removed in {release}; whether this upgrade crosses it needs a target"
NOTE_NO_TARGET = "no target; stamped callers filed as risks"
CROSSED_NOTE = "{principal} via {user_agent} wrote {api}, removed in {release}, which the control plane's {master} has crossed: the API is already gone and the writes predate the crossing, so this is not the upgrade's block"
NOTE_ENTRIES_READ = "{entries} removed-release entr{plural} read over {days}d ({writes_only})"
ENTRY_SINGULAR = "y"
ENTRY_PLURAL = "ies"
CALL_PLURAL = "s"
DESCRIBE_FORMAT = "{principal} via {user_agent} writes {api}, removed in {release} ({count} write{plural} in {days}d, last {last_seen}): {detail}"
REPLACEMENT_FORMAT = "; replacement {replacement}"


def _finding(caller: dict, tier: str, detail: str, log: dict) -> dict:
    return {
        "tier": tier,
        "principal": caller["principal"],
        "caller_class": caller["caller_class"],
        "user_agent": caller["user_agent"],
        "api": caller["api"],
        "removed_release": caller["removed_release"],
        "replacement": tables.replacement_for(caller["api"]),
        "count": caller["count"],
        "first_seen": caller["first_seen"],
        "last_seen": caller["last_seen"],
        "window_days": log["window_days"],
        "detail": detail,
    }


def _recent(caller: dict, context: dict) -> bool | None:
    """Whether the caller's last write is within BLOCKING_RECENCY_HOURS of the instant; None when unreadable."""
    last = audit_log.parse_timestamp(caller["last_seen"])
    at = context.get(readiness_rules.CONTEXT_AT)
    if last is None or at is None:
        return None
    return at - last <= timedelta(hours=BLOCKING_RECENCY_HOURS)


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = finding.empty_result()
    log = audit_log.read_removed(context)
    if log["error"]:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, AUDIT_READ_FAILED.format(reason=log["error"])))
    target_minor = (target[0], target[1]) if target else None
    target_text = tables.format_minor(target_minor) if target_minor else None
    at = context.get(readiness_rules.CONTEXT_AT)
    at_text = audit_log.format_rfc3339(at) if at else None
    master = context.get(readiness_rules.CONTEXT_MASTER)
    master_text = (member.get("control_plane_version") if isinstance(member, dict) else None) if not master else None
    master_minor = (master[0], master[1]) if master else (tables.parse_minor(master_text) if master_text else None)
    filed_without_target = 0
    for caller in log["callers"]:
        release_text = caller["removed_release"]
        if not release_text:
            continue
        release = tables.parse_minor(release_text)
        # A removal the running control plane has crossed needs no target to grade: the API is
        # already gone, so it is a note with or without one.
        if release is None:
            tier, detail = finding.TIER_RISK, RELEASE_UNPARSABLE_DETAIL.format(release=release_text)
        elif master_minor is not None and release <= master_minor:
            finding.add_note(result, CROSSED_NOTE.format(principal=caller["principal"], user_agent=caller["user_agent"], api=caller["api"], release=release_text, master=tables.format_minor(master_minor)))
            continue
        elif target_minor is None:
            tier, detail = finding.TIER_RISK, NO_TARGET_DETAIL.format(release=release_text)
            filed_without_target += 1
        elif release > target_minor:
            tier, detail = finding.TIER_RISK, AFTER_TARGET_DETAIL.format(target=target_text)
        elif caller["caller_class"] == audit_log.CLASS_PROVIDER:
            tier, detail = finding.TIER_RISK, PROVIDER_DETAIL
        elif caller["caller_class"] == audit_log.CLASS_UNPLACED:
            tier, detail = finding.TIER_RISK, UNPLACED_DETAIL.format(principal=caller["principal"])
        else:
            recent = _recent(caller, context)
            if recent is True:
                tier, detail = finding.TIER_BLOCKING, BLOCKS_DETAIL.format(target=target_text, last_seen=caller["last_seen"])
            elif recent is False:
                tier, detail = finding.TIER_RISK, STALE_DETAIL.format(target=target_text, last_seen=caller["last_seen"], hours=BLOCKING_RECENCY_HOURS, at=at_text)
            else:
                tier, detail = finding.TIER_RISK, LAST_SEEN_UNPARSABLE_DETAIL.format(target=target_text, last_seen=caller["last_seen"])
        result[finding.RESULT_BLOCKING if tier == finding.TIER_BLOCKING else finding.RESULT_RISKS].append(_finding(caller, tier, detail, log))
    if filed_without_target:
        finding.add_note(result, NOTE_NO_TARGET)
    if log["sampled"]:
        finding.add_note(result, audit_log.sampled_note(log))
    if not log["error"]:
        finding.add_note(result, NOTE_ENTRIES_READ.format(entries=log["entries"], plural=ENTRY_SINGULAR if log["entries"] == 1 else ENTRY_PLURAL, days=log["window_days"], writes_only=audit_log.WRITES_ONLY_NOTE))
    return result


def describe(item: dict) -> str:
    text = DESCRIBE_FORMAT.format(
        principal=item["principal"],
        user_agent=item["user_agent"],
        api=item["api"],
        release=item["removed_release"],
        count=item["count"],
        plural="" if item["count"] == 1 else CALL_PLURAL,
        days=item["window_days"],
        last_seen=item["last_seen"],
        detail=item["detail"],
    )
    if item.get("replacement"):
        text += REPLACEMENT_FORMAT.format(replacement=item["replacement"])
    return text

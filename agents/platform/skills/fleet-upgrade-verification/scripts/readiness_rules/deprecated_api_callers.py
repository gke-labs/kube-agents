#!/usr/bin/env python3
"""
Entry 9 of docs/designs/upgrade-failure-catalogue.md: a client writes through an API that is
deprecated and still served.

The API server stamps every request for a deprecated API version with the audit annotation
`k8s.io/deprecated=true`, a Cloud Logging entry label; an API with a removal scheduled also
carries `k8s.io/removed-release`, and those callers are the removed-API rule's. This rule
reads the shared deprecated-or-kubectl read (`audit_log.py`: a paged `gcloud logging read`
over the seven days ending at the evaluation instant, the provider's principals excluded in
the filter) and files each operator caller of a deprecated API as a risk, named with its
principal, user agent and API, with the successor where the tables name one: nothing breaks
on upgrade day, and the count is what a later removal turns into an outage. A principal the
rule cannot place is a risk naming it. The provider's callers the filter did not express
(kube-system's endpoint-controller is stamped on every Service's Endpoints it writes) are
counted in the note, not filed. `CAN_BLOCK` is False: a deprecation breaks nothing on the
day, so the rule's `unknown` goes in the note and leaves the verdict to the other rules.

The log carries writes only: a caller that only reads a deprecated API is not in it, and GKE
Deprecation Insights are the cross-check. The rule needs none of the kubectl reads.

Sources: the deprecation annotations, kubernetes.io/docs/reference/using-api/deprecation-guide/;
the Endpoints successor, upgrade_shape_tables.DEPRECATED_API_SUCCESSORS_SOURCE.
"""

import upgrade_shape_tables as tables
from readiness_rules import audit_log, finding

RULE_ID = "deprecated-api-callers"
ENTRY = 9
CAN_BLOCK = False

AUDIT_READ_FAILED = "deprecated-API callers: {reason}"
DEPRECATED_DETAIL = "deprecated and still served; the next removal is the outage, so plan the migration now"
UNPLACED_DETAIL = "deprecated and still served; the principal is of a class this rule cannot place, so it is named here"
NOTE_PROVIDER_CALLERS = "{count} provider caller{plural} of deprecated APIs not filed ({callers})"
NOTE_PROVIDER_MORE = ", and {more} more"
NOTE_PROVIDER_SHOWN = 3
NOTE_NONE = "no operator caller of a deprecated API in {days}d ({writes_only})"
PROVIDER_CALLER_FORMAT = "{principal} on {api}"
CALLER_PLURAL = "s"
LIST_SEPARATOR = ", "
DESCRIBE_FORMAT = "{principal} via {user_agent} writes {api} ({count} write{plural} in {days}d, last {last_seen}): {detail}"
SUCCESSOR_FORMAT = "; successor {successor}"


def _finding(caller: dict, log: dict) -> dict:
    return {
        "tier": finding.TIER_RISK,
        "principal": caller["principal"],
        "caller_class": caller["caller_class"],
        "user_agent": caller["user_agent"],
        "api": caller["api"],
        "successor": tables.DEPRECATED_API_SUCCESSORS.get(caller["api"]),
        "count": caller["count"],
        "first_seen": caller["first_seen"],
        "last_seen": caller["last_seen"],
        "window_days": log["window_days"],
        "detail": UNPLACED_DETAIL if caller["caller_class"] == audit_log.CLASS_UNPLACED else DEPRECATED_DETAIL,
    }


def evaluate(cluster: dict, member: dict, read, target, context: dict) -> dict:
    result = finding.empty_result()
    log = audit_log.read_deprecated(context)
    if log["error"]:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, AUDIT_READ_FAILED.format(reason=log["error"])))
    provider = []
    for caller in log["callers"]:
        if not caller["deprecated"] or caller["removed_release"]:
            continue
        if caller["caller_class"] == audit_log.CLASS_PROVIDER:
            provider.append(PROVIDER_CALLER_FORMAT.format(principal=caller["principal"], api=caller["api"]))
            continue
        result[finding.RESULT_RISKS].append(_finding(caller, log))
    if log["sampled"]:
        finding.add_note(result, audit_log.sampled_note(log))
    if provider:
        shown = LIST_SEPARATOR.join(provider[:NOTE_PROVIDER_SHOWN])
        if len(provider) > NOTE_PROVIDER_SHOWN:
            shown += NOTE_PROVIDER_MORE.format(more=len(provider) - NOTE_PROVIDER_SHOWN)
        finding.add_note(result, NOTE_PROVIDER_CALLERS.format(count=len(provider), plural="" if len(provider) == 1 else CALLER_PLURAL, callers=shown))
    if not result[finding.RESULT_RISKS] and not log["error"] and not log["sampled"]:
        finding.add_note(result, NOTE_NONE.format(days=log["window_days"], writes_only=audit_log.WRITES_ONLY_NOTE))
    return result


def describe(item: dict) -> str:
    text = DESCRIBE_FORMAT.format(
        principal=item["principal"],
        user_agent=item["user_agent"],
        api=item["api"],
        count=item["count"],
        plural="" if item["count"] == 1 else CALLER_PLURAL,
        days=item["window_days"],
        last_seen=item["last_seen"],
        detail=item["detail"],
    )
    if item.get("successor"):
        text += SUCCESSOR_FORMAT.format(successor=item["successor"])
    return text

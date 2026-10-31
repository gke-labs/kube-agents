#!/usr/bin/env python3
"""
audit_log.py — the two Cloud Logging reads the per-entry rules for catalogue entries 6, 9
and 10 share.

Not a rule: the shared read those rules import, shipped with the registry before them.
`read(context, which)` runs `gcloud logging read` for one member over the seven days ending
at the evaluation instant, pages it by timestamp bound, groups the entries by caller
(principal and user agent) and API, and caches the result in the context, so the rules that
read it read the log twice between them rather than once each. The cache is written by
`read` alone; a rule gets its own deep copy back, which it may sort or filter, and the
stored result stays as the read left it:

- the removed-release read: entries the API server stamped `k8s.io/removed-release`;
- the deprecated-or-kubectl read: entries stamped `k8s.io/deprecated` with no removal
  release, and every write whose user agent is kubectl's.

Two reads rather than one, so a chatty deprecated writer (the seeded fixture writes every
ten minutes) cannot fill the page a removed-API caller would have been on. The provider's
own principals are excluded in the filter where the query language can say it, and
`caller_class` places every caller that comes back: the operator's (user accounts, service
accounts, and Kubernetes service accounts outside the system namespaces), the provider's
(every other `system:` principal, the node identities, Google-managed service agents), or
unplaced, which a rule names and never blocks on.

The log is the Admin Activity audit log, which carries writes only: a caller that only
reads an API is not in it, and GKE Deprecation Insights are the cross-check. The filter names
that log (`logName:"cloudaudit.googleapis.com%2Factivity"`), because GKE's opt-in Data Access
audit log shares `resource.type="k8s_cluster"` and the `k8s.io/deprecated` labels and carries
every read: without the clause, a project with Data Access logging on fills the pages with
`kubectl get`s and names read-only clients as writers. A read that
fails or times out is an error the rules report as `unknown`, never as a blocker; a read
that still fills its last page is graded from what it saw and says so (`sampled`).

The context the reads run through is the per-member one readiness_rules documents:
`run_cmd` with `timeout_seconds`, `project`, `location`, `cluster_name`, `at` and `cache`.

`gcloud logging read` is on the agent's gcloud read allowlist
(agents/platform/scripts/command_policy.py) with `--limit`, `--project` and `--format`
among the flags it admits. The command has no `--page-size`, so a page is `--limit` and the
next page is a `timestamp<=` bound on the oldest entry seen, inclusive because the API
server stamps several writes with one microsecond and a strict bound would drop the ones at
the boundary that did not fit the page, with the entries already read dropped by
`insertId`; `--freshness` is not used,
because gcloud refuses it beside a timestamp in the filter, and the window is in the filter.
The page is projected to the fields read here (PROJECTED_FIELDS), and a page the credential
proxy still cut at its output cap is the read's error, naming the cap, with the pages before
it kept.
"""

import copy
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

import upgrade_shape_tables as tables
from readiness_rules import (
    CONTEXT_AT,
    CONTEXT_CACHE,
    CONTEXT_CLOCK,
    CONTEXT_CLUSTER_NAME,
    CONTEXT_LOCATION,
    CONTEXT_PROJECT,
    CONTEXT_RUN_CMD,
    CONTEXT_TIMEOUT_SECONDS,
    NOT_A_RULE_DEFECT,
    PROXY_TRUNCATION_MARKER,
    exception_text,
    is_system_namespace,
)

# The command. Cloud Logging keeps GKE's Kubernetes audit entries under resource type
# k8s_cluster, with the cluster and location as resource labels, the API server's audit
# annotations as entry labels, and the user agent and principal in the AuditLog payload.
GCLOUD = "gcloud"
LOGGING_READ = ("logging", "read")
RESOURCE_TYPE = "k8s_cluster"
ACTIVITY_LOG_ID = "cloudaudit.googleapis.com%2Factivity"  # the Admin Activity log; Data Access shares the resource type
LABEL_DEPRECATED = "k8s.io/deprecated"
LABEL_REMOVED_RELEASE = "k8s.io/removed-release"
DEPRECATED_TRUE = "true"
USER_AGENT_FIELD = "protoPayload.requestMetadata.callerSuppliedUserAgent"
PRINCIPAL_FIELD = "protoPayload.authenticationInfo.principalEmail"
KUBECTL_USER_AGENT_PREFIX = "kubectl/"
PROJECT_FLAG = "--project={project}"
LIMIT_FLAG = "--limit={limit}"
# The page is projected to the fields `caller_records` reads: a full AuditLog entry carries
# the request and response bodies, several kilobytes each, and a thousand of them would
# pass the credential proxy's output cap; the projection keeps the nested shape gcloud
# prints, so the parser reads the same paths as an unprojected entry. `insertId` is the
# page cursor's key: the next page's bound is inclusive, so the entries at the boundary
# timestamp come back and are dropped by id.
PROJECTED_FIELDS = (
    "insertId",
    "timestamp",
    "labels",
    "protoPayload.authenticationInfo",
    "protoPayload.requestMetadata.callerSuppliedUserAgent",
    "protoPayload.resourceName",
    "protoPayload.methodName",
)
JSON_FORMAT_FLAG = "--format=json(" + ",".join(PROJECTED_FIELDS) + ")"

# The two reads and their filters.
READ_REMOVED = "removed"
READ_DEPRECATED = "deprecated"
READS = (READ_REMOVED, READ_DEPRECATED)
# What the report keeps of each read, for the JSON and its read-failure list. A rule may
# write anything under the cache key; `cache_defect` is what the fold holds a rule to, and
# `summaries` hands the report only entries that pass it.
SUMMARY_KEYS = ("commands", "entries", "pages", "sampled", "error", "window_start", "window_end")
READ_KEY = "read"
CALLERS_KEY = "callers"
WINDOW_DAYS_KEY = "window_days"
PAGE_LIMIT_KEY = "page_limit"
NONE_CLASS = type(None)
# The one shape of a stored read: every key `read` stores, each with the classes its value may
# be, by class. `read` builds its result in this order, the probe requires all of it of a stored
# entry before a rule is handed it, and SUMMARY_KEYS is the subset the report publishes.
STORED_VALUE_CLASSES = {
    READ_KEY: (str,),
    "commands": (list,),
    "entries": (int,),
    "pages": (int,),
    "sampled": (bool,),
    "error": (str, NONE_CLASS),
    "window_start": (str, NONE_CLASS),
    "window_end": (str, NONE_CLASS),
    CALLERS_KEY: (list,),
    WINDOW_DAYS_KEY: (int,),
    PAGE_LIMIT_KEY: (int,),
}
STORED_KEYS = tuple(STORED_VALUE_CLASSES)
# The one shape of a caller record, as `caller_records` builds it and the probe requires of
# every `callers` entry.
CALLER_VALUE_CLASSES = {
    "principal": (str,),
    "user_agent": (str,),
    "api": (str,),
    "removed_release": (str, NONE_CLASS),
    "deprecated": (bool,),
    "kubectl": (bool,),
    "caller_class": (str,),
    "count": (int,),
    "first_seen": (str, NONE_CLASS),
    "last_seen": (str, NONE_CLASS),
}
CALLER_RECORD_KEYS = tuple(CALLER_VALUE_CLASSES)
SCOPE_CLAUSE = 'resource.type="{resource_type}" AND logName:"{activity_log}" AND resource.labels.cluster_name="{cluster}" AND resource.labels.location="{location}"'
REMOVED_CLAUSE = 'labels."{removed}":*'
DEPRECATED_CLAUSE = '((labels."{deprecated}"="{deprecated_true}" AND NOT labels."{removed}":*) OR {user_agent_field}:"{kubectl}")'
WINDOW_CLAUSE = 'timestamp>="{start}" AND timestamp<="{end}"'
PAGE_CLAUSE = 'timestamp<="{before}"'
CLAUSE_SEPARATOR = " AND "
RFC3339_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Who a principal is. The operator's callers are user accounts, service accounts, and
# Kubernetes service accounts outside the namespaces GKE and its add-ons occupy
# (`upgrade_shape_tables.SYSTEM_NAMESPACES` and its prefixes, the one predicate every rule
# reads through `readiness_rules.is_system_namespace`); every other `system:` principal
# (the control plane's components, the node identities, the anonymous and unsecured
# users), the service accounts in those namespaces, and Google-managed service agents are
# the provider's, and GKE moves them with the version. A principal of no recognisable
# shape is unplaced: named, never blocked on.
CLASS_OPERATOR = "operator"
CLASS_PROVIDER = "provider"
CLASS_UNPLACED = "unplaced"
SYSTEM_PREFIX = "system:"
SERVICEACCOUNT_PREFIX = "system:serviceaccount:"
SERVICEACCOUNT_SEPARATOR = ":"
EMAIL_MARKER = "@"
GOOGLE_SERVICE_AGENT_SUFFIXES = (
    "@container-engine-robot.iam.gserviceaccount.com",
    "@cloudservices.gserviceaccount.com",
    "@system.gserviceaccount.com",
)
GOOGLE_SERVICE_AGENT_MARKER = "@gcp-sa-"
GOOGLE_SERVICE_AGENT_IAM_SUFFIX = ".iam.gserviceaccount.com"
# The same classes as the query language can state them, so the provider's callers never
# fill a page: the system namespaces and their prefixes come from the same table
# `caller_class` reads. RE2 has no lookahead, so "every system: principal except a service
# account" is two clauses; dots are character classes because a backslash inside a quoted
# filter string would need an escape of its own. `caller_class` is still applied to what
# comes back, so a principal the regexes do not express is placed on the way in.
PROVIDER_EXCLUSION_CLAUSE = '({principal}!~"{system_re}" OR {principal}=~"{serviceaccount_re}") AND {principal}!~"{system_namespaces_re}" AND {principal}!~"{service_agents_re}"'
SYSTEM_PRINCIPAL_RE = "^system:"
SERVICEACCOUNT_RE = "^system:serviceaccount:"
NAMESPACE_PREFIX_TAIL_RE = "[a-z0-9-]*"
SYSTEM_NAMESPACES_RE_FORMAT = "^system:serviceaccount:({namespaces}):"
SERVICE_AGENTS_RE = "@(gcp-sa-[a-z0-9-]+[.]iam|container-engine-robot[.]iam|cloudservices|system)[.]gserviceaccount[.]com$"
RE_ALTERNATIVE = "|"
# Characters escaped inside a quoted filter string, so a cluster name read from the API
# can never close the quote. Real GKE names are lowercase alphanumerics and hyphens.
FILTER_ESCAPE_RE = re.compile(r'(["\\])')
FILTER_ESCAPE_REPLACEMENT = r"\\\1"

# Where one member's reads are cached in the context (the keys read are the package's
# CONTEXT_* names), and the reasons a read produced nothing usable.
CACHE_KEY = "audit_log"
NO_RUNNER_REASON = "no command runner in the readiness context; the audit log was not read"
NO_SCOPE_REASON = "the readiness context carries no project, location or cluster name for the log filter"
READ_FAILED_REASON = "`gcloud logging read` ({read} read, page {page}) failed (rc={rc}): {stderr}"
UNPARSABLE_REASON = "`gcloud logging read` ({read} read, page {page}) returned output that is not a JSON list of entries (rc=0)"
OUTPUT_CUT_REASON = "`gcloud logging read` ({read} read, page {page}) output was cut at the credential proxy's output cap, so the page did not fit in one read"
READ_RAISED_REASON = "the {read} read raised {error}; the pages read before it are not graded"
SAMPLED_NOTE = "sampled {entries} entries over {pages} page(s) of the {read} read; more callers possible"
WRITES_ONLY_NOTE = "writes only; a caller that only reads is not in this log"
CACHE_NOT_A_DICT = "the readiness context's cache is {type}, not a dict"
LOGS_NOT_A_DICT = "cache[{key!r}] is {type}, not a dict of read results"
LOG_UNKNOWN_READ = "cache[{key!r}] carries {read!r}, which is not one of the reads"
LOG_NOT_A_READ = "cache[{key!r}][{read!r}] is not the read's result: {detail}"
LOG_WRONG_TYPE = "{type}, not a dict"
LOG_WRONG_READ = "its `read` is not {read!r}"
LOG_MISSING_KEYS = "{where}missing {keys}"
LOG_WRONG_VALUE = "{where}{key!r} is {type}, not {expected}"
LOG_WHERE_ENTRY = ""
LOG_WHERE_CALLER = "`callers`[{index}] "
LOG_CALLER_NOT_A_RECORD = "`callers`[{index}] is {type}, not a dict"
EXPECTED_CLASSES_JOIN = " or "
CACHE_REPLACED = "the readiness context's cache was replaced"
READ_LOST = "the {read!r} read stored before the rule is gone or changed"
READ_UNCOPYABLE = "the {read!r} entry holds an object that cannot be copied (a generator, a lock, a handle)"
UNCOPYABLE_TYPE = "an object that cannot be copied"
# What a stored read may hold, decided by class and not by what encodes: `read` stores exact
# dicts with string keys, lists, strings, ints, finite floats, bools and None, so a value of
# any other class, a JSON type's subclass included, is a rule's write, named at that rule's
# own check by this walk, and never compared, copied or read through its own methods.
READ_FOREIGN_VALUE = "the {read!r} entry holds {detail}"
FOREIGN_VALUE_DETAIL = "a value of class {type} at {path}, not a JSON value"
FOREIGN_KEY_DETAIL = "a key of class {type} at {path}, not a string"
NON_FINITE_DETAIL = "a non-finite float at {path}, not a JSON value"
ENTRY_PATH = "the entry"
PLAIN_JSON_SCALARS = (str, int, bool)
CACHE_UNREADABLE = "the cache could not be compared with the snapshot: {error}"
UNKNOWN_READ_CALL = "audit_log.read({which!r}): not one of the reads {reads}"
STDERR_EXCERPT_CHARS = 300
STDERR_JOIN = " "
NO_STDERR = "no stderr"

# Audit entry fields.
INSERT_ID = "insertId"
PROTO_PAYLOAD = "protoPayload"
LABELS = "labels"
TIMESTAMP = "timestamp"
AUTHENTICATION_INFO = "authenticationInfo"
PRINCIPAL_EMAIL = "principalEmail"
REQUEST_METADATA = "requestMetadata"
CALLER_SUPPLIED_USER_AGENT = "callerSuppliedUserAgent"
RESOURCE_NAME = "resourceName"
METHOD_NAME = "methodName"
# `<group>/<version>[/namespaces/<ns>]/<resource>[/<name>[/<subresource>]]`, the AuditLog
# resourceName GKE writes (`core/v1/namespaces/x/endpoints/y`). The methodName is the
# fallback: `io.k8s.<group, reverse-DNS>.<version>.<resource>.<verb>`, where the group
# cannot always be read back exactly (`io.k8s.apps` is `apps`, `io.k8s.apiserver.flowcontrol`
# is `flowcontrol.apiserver.k8s.io`), so the fallback names it as the method spells it.
VERSION_TOKEN_RE = re.compile(r"^v\d+(?:(?:alpha|beta)\d+)?$")
NAMESPACES_SEGMENT = "namespaces"
NAMESPACED_PREFIX_LENGTH = 2
# `core/v1/namespaces/foo/status` and `core/v1/namespaces/foo/pods` have the same shape: the
# first is a subresource of the Namespace, the second a create inside it whose name is not
# assigned yet. The Namespace's own subresources are a closed set, so a third segment in it
# names the Namespace; the drift detector's parser (k8s-operator/cmd/drift-detector/
# resourcename.go) draws the same line.
NAMESPACE_SUBRESOURCES = ("status", "finalize")
NAMESPACE_SUBRESOURCE_LENGTH = 3
METHOD_PREFIX = ("io", "k8s")
CORE_GROUP = "core"
API_FORMAT = "{group}/{version} {resource}"
UNKNOWN_API = "(api not named in the entry)"
UNKNOWN_PRINCIPAL = "(no principal in the entry)"
UNKNOWN_USER_AGENT = "(no user agent)"
# Cloud Logging timestamps: RFC 3339 with up to nine fractional digits; Python keeps six.
TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$")
MICROSECOND_DIGITS = 6
UTC_OFFSET = "+00:00"
ZULU = "Z"


def _escape(value) -> str:
    return FILTER_ESCAPE_RE.sub(FILTER_ESCAPE_REPLACEMENT, str(value or ""))


def format_rfc3339(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(RFC3339_FORMAT)


def parse_timestamp(text) -> datetime | None:
    """A Cloud Logging timestamp as an aware UTC datetime; None when it does not parse."""
    m = TIMESTAMP_RE.match(str(text or "").strip())
    if not m:
        return None
    base, fraction, zone = m.groups()
    micro = int((fraction or "0")[:MICROSECOND_DIGITS].ljust(MICROSECOND_DIGITS, "0"))
    try:
        parsed = datetime.fromisoformat(base + (UTC_OFFSET if zone == ZULU else zone))
    except ValueError:
        return None
    return parsed.replace(microsecond=micro).astimezone(timezone.utc)


def provider_exclusion_clause() -> str:
    namespaces = [re.escape(n) for n in sorted(tables.SYSTEM_NAMESPACES)] + [p + NAMESPACE_PREFIX_TAIL_RE for p in tables.SYSTEM_NAMESPACE_PREFIXES]
    return PROVIDER_EXCLUSION_CLAUSE.format(
        principal=PRINCIPAL_FIELD,
        system_re=SYSTEM_PRINCIPAL_RE,
        serviceaccount_re=SERVICEACCOUNT_RE,
        system_namespaces_re=SYSTEM_NAMESPACES_RE_FORMAT.format(namespaces=RE_ALTERNATIVE.join(namespaces)),
        service_agents_re=SERVICE_AGENTS_RE,
    )


def build_filter(which: str, cluster: str, location: str, start: str, end: str) -> str:
    """The Cloud Logging filter of one read for one cluster over [start, end]."""
    population = REMOVED_CLAUSE.format(removed=LABEL_REMOVED_RELEASE)
    if which == READ_DEPRECATED:
        population = DEPRECATED_CLAUSE.format(
            deprecated=LABEL_DEPRECATED, deprecated_true=DEPRECATED_TRUE, removed=LABEL_REMOVED_RELEASE, user_agent_field=USER_AGENT_FIELD, kubectl=KUBECTL_USER_AGENT_PREFIX
        )
    return CLAUSE_SEPARATOR.join(
        (
            SCOPE_CLAUSE.format(resource_type=RESOURCE_TYPE, activity_log=ACTIVITY_LOG_ID, cluster=_escape(cluster), location=_escape(location)),
            population,
            WINDOW_CLAUSE.format(start=start, end=end),
            provider_exclusion_clause(),
        )
    )


def build_command(project: str, filter_text: str) -> list[str]:
    return [GCLOUD, *LOGGING_READ, filter_text, PROJECT_FLAG.format(project=project), LIMIT_FLAG.format(limit=tables.AUDIT_LOG_PAGE_LIMIT), JSON_FORMAT_FLAG]


def is_google_service_agent(principal: str) -> bool:
    text = str(principal or "")
    if text.endswith(GOOGLE_SERVICE_AGENT_SUFFIXES):
        return True
    return GOOGLE_SERVICE_AGENT_MARKER in text and text.endswith(GOOGLE_SERVICE_AGENT_IAM_SUFFIX)


def caller_class(principal) -> str:
    """CLASS_OPERATOR, CLASS_PROVIDER or CLASS_UNPLACED for one principal (see the module doc)."""
    text = str(principal or "").strip()
    if not text:
        return CLASS_UNPLACED
    if text.startswith(SERVICEACCOUNT_PREFIX):
        rest = text[len(SERVICEACCOUNT_PREFIX) :]
        namespace, separator, name = rest.partition(SERVICEACCOUNT_SEPARATOR)
        if not namespace or not separator or not name:
            return CLASS_UNPLACED
        if is_system_namespace(namespace):
            return CLASS_PROVIDER
        return CLASS_OPERATOR
    if text.startswith(SYSTEM_PREFIX):
        return CLASS_PROVIDER
    if EMAIL_MARKER in text:
        return CLASS_PROVIDER if is_google_service_agent(text) else CLASS_OPERATOR
    return CLASS_UNPLACED


def api_of(resource_name, method_name) -> str:
    """`core/v1 endpoints` from the entry's resourceName, or from its methodName."""
    parts = [p for p in str(resource_name or "").split("/") if p]
    if len(parts) >= 3 and VERSION_TOKEN_RE.match(parts[1]):
        rest = parts[2:]
        namespaced = rest[0] == NAMESPACES_SEGMENT and len(rest) > NAMESPACED_PREFIX_LENGTH
        namespace_subresource = len(rest) == NAMESPACE_SUBRESOURCE_LENGTH and rest[NAMESPACED_PREFIX_LENGTH] in NAMESPACE_SUBRESOURCES
        if namespaced and not namespace_subresource:
            rest = rest[NAMESPACED_PREFIX_LENGTH:]
        return API_FORMAT.format(group=parts[0], version=parts[1], resource=rest[0])
    tokens = str(method_name or "").split(".")
    for index, token in enumerate(tokens):
        if VERSION_TOKEN_RE.match(token) and index + 1 < len(tokens):
            group_tokens = tokens[:index]
            if tuple(group_tokens[: len(METHOD_PREFIX)]) == METHOD_PREFIX:
                group_tokens = group_tokens[len(METHOD_PREFIX) :]
            group = CORE_GROUP if group_tokens == [CORE_GROUP] else ".".join(reversed(group_tokens))
            return API_FORMAT.format(group=group or CORE_GROUP, version=token, resource=tokens[index + 1])
    return UNKNOWN_API


def caller_records(entries: list) -> list[dict]:
    """Entries grouped by (principal, user agent, API), sorted by those three.

    Each record carries the deprecation labels seen on the group: `removed_release` is the
    release the API server stamped (the same on every entry of one API), `deprecated` is
    whether any entry carried `k8s.io/deprecated=true`, `kubectl` whether the user agent is
    kubectl's, `caller_class` who the principal is. `first_seen` and `last_seen` are the RFC
    3339 strings Cloud Logging wrote, chosen by parsed time: the logger emits 0, 3, 6 or 9
    fractional digits, so text order is not time order.
    """
    grouped: dict[tuple, dict] = {}
    seen: dict[tuple, tuple[datetime, datetime]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        payload = _dict_field(entry, PROTO_PAYLOAD)
        labels = _dict_field(entry, LABELS)
        principal = _text_field(_dict_field(payload, AUTHENTICATION_INFO), PRINCIPAL_EMAIL) or UNKNOWN_PRINCIPAL
        user_agent = _text_field(_dict_field(payload, REQUEST_METADATA), CALLER_SUPPLIED_USER_AGENT) or UNKNOWN_USER_AGENT
        api = api_of(payload.get(RESOURCE_NAME), payload.get(METHOD_NAME))
        key = (principal, user_agent, api)
        record = grouped.setdefault(
            key,
            {
                "principal": principal,
                "user_agent": user_agent,
                "api": api,
                "removed_release": None,
                "deprecated": False,
                "kubectl": str(user_agent).startswith(KUBECTL_USER_AGENT_PREFIX),
                "caller_class": caller_class(None if principal == UNKNOWN_PRINCIPAL else principal),
                "count": 0,
                "first_seen": None,
                "last_seen": None,
            },
        )
        record["count"] += 1
        removed = labels.get(LABEL_REMOVED_RELEASE)
        if removed and not record["removed_release"]:
            record["removed_release"] = str(removed)
        if str(labels.get(LABEL_DEPRECATED, "")).lower() == DEPRECATED_TRUE:
            record["deprecated"] = True
        stamp = entry.get(TIMESTAMP)
        parsed = parse_timestamp(stamp)
        if parsed is not None:
            first, last = seen.get(key, (parsed, parsed))
            if parsed <= first:
                first, record["first_seen"] = parsed, stamp
            if parsed >= last:
                last, record["last_seen"] = parsed, stamp
            seen[key] = (first, last)
    return sorted(grouped.values(), key=lambda r: (r["principal"], r["user_agent"], r["api"]))


def _dict_field(obj, key: str) -> dict:
    """The dict under `key`, or an empty one when the entry spells it as anything else."""
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, dict) else {}


def _text_field(obj: dict, key: str) -> str | None:
    """The string under `key`, or None when it is absent or not a string."""
    value = obj.get(key)
    return value if isinstance(value, str) and value else None


def _one_line(stderr) -> str:
    """gcloud's stderr as one line, so a multi-line error never splits a table row."""
    return STDERR_JOIN.join(str(stderr or "").split())[:STDERR_EXCERPT_CHARS] or NO_STDERR


def _parse_page(stdout) -> list | None:
    if not (stdout or "").strip():
        return []
    try:
        entries = json.loads(stdout)
    except ValueError:
        return None
    return entries if isinstance(entries, list) else None


def _oldest_timestamp(page: list) -> str | None:
    """The text of the page's oldest timestamp by parsed time, the next page's `timestamp<=`
    bound; None when no entry carries one that parses."""
    stamped = [(parsed, e[TIMESTAMP]) for e in page if isinstance(e, dict) and (parsed := parse_timestamp(e.get(TIMESTAMP))) is not None]
    return min(stamped)[1] if stamped else None


def read(context: dict, which: str) -> dict:
    """One member's callers from one read, paged, read once and cached in the context; the
    caller gets a deep copy, so sorting or filtering it changes nothing another rule reads.
    `which` must be one of READS, refused with a ValueError before any command runs.

    Returns a dict carrying STORED_KEYS, each value of its listed class: `read`, `commands`,
    `entries`, `pages`, `sampled`, `error`, `window_start`, `window_end`, `callers`,
    `window_days`, `page_limit`; the probe requires the same of a stored entry. `error` is the reason the
    read stopped producing usable pages (no runner, a failed or timed-out command, output
    that is not a list, a page the credential proxy cut at its output cap, or an exception
    inside the read); the pages before it are kept and graded, except after an exception,
    which leaves `callers` empty. `sampled` says the last page was full and no further
    page was read: the page count reached its ceiling, the read's time budget ran out, the
    page carried no timestamp to bound the next one on, or a full page added no entry not
    already read (more entries share the boundary timestamp than a page holds). The result
    enters the cache
    only once the read is complete, so a rule that raises mid-read never leaves a clean
    half-built result for the next rule to find.
    """
    if which not in READS:  # refused before any command runs, and named as the call, not as cache damage
        raise ValueError(UNKNOWN_READ_CALL.format(which=which, reads=READS))
    cache = context.get(CONTEXT_CACHE)
    if type(cache) is not dict:  # by class, as the fold's checks read it: a context built by hand, or a mapping of another class, gets a plain dict of its own (the fold normalises its context once before the first rule)
        cache = context[CONTEXT_CACHE] = {}
    logs = cache.get(CACHE_KEY)
    if type(logs) is not dict:  # a context built by hand, or a mapping of a rule's own class: `read` keeps a plain dict of its own; the fold puts a rule's damage back before the next rule
        logs = cache[CACHE_KEY] = {}
    cached = logs.get(which)
    if _read_result_defect(which, cached) is None:  # cached only when it is a read result by the probe's own test: an inherited entry the probe faults is read afresh, never handed to a rule as the read
        return copy.deepcopy(cached)
    result = _empty_result(which)
    try:
        _read_pages(context, which, result)
    except NOT_A_RULE_DEFECT:
        raise
    except BaseException as e:  # noqa: BLE001 - the failure is the read's error, SystemExit included, never a clean cached window
        result["error"] = READ_RAISED_REASON.format(read=which, error=exception_text(e))  # never raises, so the window is never cached clean
        result[CALLERS_KEY] = []
    finally:
        logs[which] = result
    return copy.deepcopy(result)  # the rule's own copy: the cache is written by `read` alone


def _empty_result(which: str) -> dict:
    """A read result before its first page: every key in STORED_KEYS, in that order, holding a
    value of its class, so what `read` stores is an entry its own probe accepts."""
    return {
        READ_KEY: which,
        "commands": [],
        "entries": 0,
        "pages": 0,
        "sampled": False,
        "error": None,
        "window_start": None,
        "window_end": None,
        CALLERS_KEY: [],
        WINDOW_DAYS_KEY: tables.AUDIT_LOG_WINDOW_DAYS,
        PAGE_LIMIT_KEY: tables.AUDIT_LOG_PAGE_LIMIT,
    }


def _read_pages(context: dict, which: str, result: dict) -> None:
    """Fills `result` from the runner in the context, page by page."""
    run = context.get(CONTEXT_RUN_CMD)
    project = context.get(CONTEXT_PROJECT)
    location = context.get(CONTEXT_LOCATION)
    cluster = context.get(CONTEXT_CLUSTER_NAME)
    if run is None:
        result["error"] = NO_RUNNER_REASON
        return
    if not (project and location and cluster):
        result["error"] = NO_SCOPE_REASON
        return
    at = context.get(CONTEXT_AT)
    end = at.astimezone(timezone.utc) if isinstance(at, datetime) else datetime.now(timezone.utc)
    start = end - timedelta(days=tables.AUDIT_LOG_WINDOW_DAYS)
    result["window_start"], result["window_end"] = format_rfc3339(start), format_rfc3339(end)
    clock = context.get(CONTEXT_CLOCK) or time.monotonic
    timeout = context.get(CONTEXT_TIMEOUT_SECONDS)
    base = build_filter(which, cluster, location, result["window_start"], result["window_end"])
    started = clock()
    before = None
    entries: list = []
    seen: set = set()
    while True:
        filter_text = base if before is None else base + CLAUSE_SEPARATOR + PAGE_CLAUSE.format(before=before)
        cmd = build_command(project, filter_text)
        result["commands"].append(" ".join(cmd))
        # The context's `timeout_seconds` is the per-call cap, the runner's own default
        # without it; a timed-out call comes back as a non-zero rc with the reason on
        # stderr, like any failed read.
        rc, stdout, stderr = run(cmd, timeout) if timeout else run(cmd)
        result["pages"] += 1
        if rc != 0:
            result["error"] = READ_FAILED_REASON.format(read=which, page=result["pages"], rc=rc, stderr=_one_line(stderr))
            break
        # The proxy cuts a page past its output cap and prints the marker with rc 0; the
        # marker, not the parse failure, names the cause, and the pages before it are kept.
        if PROXY_TRUNCATION_MARKER in str(stderr or ""):
            result["error"] = OUTPUT_CUT_REASON.format(read=which, page=result["pages"])
            break
        page = _parse_page(stdout)
        if page is None:
            result["error"] = UNPARSABLE_REASON.format(read=which, page=result["pages"])
            break
        fresh = _unseen(page, seen)
        entries.extend(fresh)
        if len(page) < tables.AUDIT_LOG_PAGE_LIMIT:
            break
        oldest = _oldest_timestamp(page)
        if not fresh or oldest is None or result["pages"] >= tables.AUDIT_LOG_MAX_PAGES or clock() - started >= tables.AUDIT_LOG_READ_BUDGET_SECONDS:
            result["sampled"] = True
            break
        before = oldest
    result["entries"] = len(entries)
    result["callers"] = caller_records(entries)


def _unseen(page: list, seen: set) -> list:
    """The page's entries not read on an earlier page, by `insertId`, which the inclusive
    page bound brings back for the entries at the previous page's oldest timestamp; an
    entry with no id is kept."""
    fresh = []
    for entry in page:
        key = entry.get(INSERT_ID) if isinstance(entry, dict) else None
        if key is None or key not in seen:
            fresh.append(entry)
            if key is not None:
                seen.add(key)
    return fresh


def cache_defect(context) -> str | None:
    """Why the audit-log cache cannot be read as the reads left it, or None. The context's
    cache must be a dict, its `audit_log` entry a plain dict keyed by read name, and each value a
    read result: a dict whose `read` is that name, carrying every stored key (STORED_KEYS) with a
    value of its class, every `callers` entry a caller record (CALLER_RECORD_KEYS), and a JSON
    value throughout with no NaN or infinity. A rule writes the cache; the fold takes `cache_snapshot` before
    a rule runs, and damage the rule left (`cache_damage`: this, or a stored read gone or
    changed) is the rule's `unknown`, with the cache put back (`cache_restore`) whether the
    rule returned or raised, so no later rule or reader meets it."""
    defects = cache_defects(context)
    return defects[0] if defects else None


def cache_defects(context) -> list[str]:
    """Every way the audit-log cache departs from what the reads store (see `cache_defect`),
    in cache order; empty when it is sound. Read through a snapshot, so an entry that cannot
    be copied is a defect named as such and nothing here raises."""
    if not isinstance(context, dict) or CONTEXT_CACHE not in context:
        return []
    cache = context[CONTEXT_CACHE]
    if type(cache) is not dict:  # by class, before any method of it runs
        return [CACHE_NOT_A_DICT.format(type=type(cache).__name__)]
    return _log_defects(_copy_logs(cache.get(CACHE_KEY)))


def _log_defects(logs) -> list[str]:
    """The defects of a snapshot's `audit_log` entry (`_copy_logs` output, or None)."""
    if logs is None:
        return []
    if logs is UNCOPYABLE:
        return [LOGS_NOT_A_DICT.format(key=CACHE_KEY, type=UNCOPYABLE_TYPE)]
    if isinstance(logs, ForeignMapping):
        return [LOGS_NOT_A_DICT.format(key=CACHE_KEY, type=logs.type_name)]
    if not isinstance(logs, dict):
        return [LOGS_NOT_A_DICT.format(key=CACHE_KEY, type=type(logs).__name__)]
    return [defect for which, log in logs.items() if (defect := _read_result_defect(which, log)) is not None]


def _read_result_defect(which, log) -> str | None:
    """Why `log`, stored under the read name `which`, is not a read result, or None. Never
    raises: an entry of a rule's own class whose methods raise is named by what it raised."""
    try:
        return _read_result_shape_defect(which, log)
    except NOT_A_RULE_DEFECT:
        raise
    except BaseException as e:  # noqa: BLE001 - a mapping whose `get` or `__contains__` raises is not a read result either
        return LOG_NOT_A_READ.format(key=CACHE_KEY, read=which, detail=exception_text(e))


def _read_result_shape_defect(which, log) -> str | None:
    if which not in READS:
        return LOG_UNKNOWN_READ.format(key=CACHE_KEY, read=which)
    if log is UNCOPYABLE:
        return READ_UNCOPYABLE.format(read=which)
    if isinstance(log, ForeignValue):
        return READ_FOREIGN_VALUE.format(read=which, detail=log.detail)
    if type(log) is not dict:  # by class, before any method of it runs: a dict subclass is not a read result
        return LOG_NOT_A_READ.format(key=CACHE_KEY, read=which, detail=LOG_WRONG_TYPE.format(type=type(log).__name__))
    if log.get(READ_KEY) != which:
        return LOG_NOT_A_READ.format(key=CACHE_KEY, read=which, detail=LOG_WRONG_READ.format(read=which))
    # Every key `read` stores, each value of its class, and every caller record whole: the probe
    # requires what a rule is told it gets, so an entry of the summary's shape alone, or one whose
    # `callers` is not a list of records, is read afresh, never handed to a rule as the read.
    detail = _record_defect(log, STORED_VALUE_CLASSES, LOG_WHERE_ENTRY)
    if detail is None:
        for index, caller in enumerate(log[CALLERS_KEY]):
            if type(caller) is not dict:  # by class, before any method of it runs
                detail = LOG_CALLER_NOT_A_RECORD.format(index=index, type=type(caller).__name__)
                break
            detail = _record_defect(caller, CALLER_VALUE_CLASSES, LOG_WHERE_CALLER.format(index=index))
            if detail is not None:
                break
    if detail is not None:
        return LOG_NOT_A_READ.format(key=CACHE_KEY, read=which, detail=detail)
    # The whole result, `callers` included, by class: `read` stores plain JSON values alone, so
    # a value of any other class a rule left anywhere in it, a JSON type's subclass included,
    # is that rule's defect at its own cache check, never a changed read charged to the rule
    # after it, and nothing here or in the comparison runs the object's own methods.
    detail = _plain_json_defect(log)
    if detail is not None:
        return READ_FOREIGN_VALUE.format(read=which, detail=detail)
    return None


def _record_defect(record: dict, classes: dict, where: str) -> str | None:
    """Why the plain dict `record` is not of the shape `classes` gives (every key present, each
    value of one of its listed classes, by class), prefixed by `where`, or None. Reads the dict
    with dict's own methods and the values' classes alone, so nothing a rule stored runs here."""
    missing = [key for key in classes if key not in record]
    if missing:
        return LOG_MISSING_KEYS.format(where=where, keys=missing)
    for key, allowed in classes.items():
        if type(record[key]) not in allowed:
            return LOG_WRONG_VALUE.format(where=where, key=key, type=type(record[key]).__name__, expected=EXPECTED_CLASSES_JOIN.join(c.__name__ for c in allowed))
    return None


UNCOPYABLE = object()  # a snapshot's stand-in for an entry that cannot be deep-copied


class ForeignValue:
    """A snapshot's stand-in for a stored read that holds a value of a class `read` never
    stores (`_plain_json_defect`), carrying the walk's detail: named as the writer's defect,
    never compared, and dropped by the restore like an entry the snapshot could not copy."""

    __slots__ = ("detail",)

    def __init__(self, detail: str):
        self.detail = detail


def _plain_json_defect(value, path: str = ENTRY_PATH) -> str | None:
    """Why `value` is not a plain JSON value, or None: every node must be an exact `dict` with
    string keys, an exact `list`, `str`, `int`, `bool`, a finite `float`, or None, the classes
    `read` stores. The walk tests each node's class before it looks inside, so the methods of a
    class a rule authored are never run; a dict subclass is a defect at its own node."""
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                return FOREIGN_KEY_DETAIL.format(type=type(key).__name__, path=path)
            defect = _plain_json_defect(item, f"{path}[{key!r}]")
            if defect is not None:
                return defect
        return None
    if kind is list:
        for index, item in enumerate(value):
            defect = _plain_json_defect(item, f"{path}[{index}]")
            if defect is not None:
                return defect
        return None
    if kind is float:
        return None if math.isfinite(value) else NON_FINITE_DETAIL.format(path=path)
    if kind in PLAIN_JSON_SCALARS or value is None:
        return None
    return FOREIGN_VALUE_DETAIL.format(type=kind.__name__, path=path)


class ForeignMapping:
    """A snapshot's stand-in for an `audit_log` entry that is not a plain `dict`: a mapping of
    another class (an OrderedDict, a defaultdict, a rule's own subclass) or any other object
    (a scalar, a list, an instance of a rule's class). `read` stores a plain dict and keeps
    one, so the class is a rule's write, named as such by the defect probe, never iterated or
    copied by the snapshot (a copy would run the object's own `__deepcopy__`, and a rule's
    method then decides what the snapshot believes), and dropped by the restore like an entry
    it could not copy."""

    __slots__ = ("type_name",)

    def __init__(self, type_name: str):
        self.type_name = type_name
CACHE_ABSENT = object()  # a snapshot's stand-in for a context with no cache key at all, which a key holding None is not


def _copy_logs(logs):
    """A deep copy of the cache's `audit_log` entry, entry by entry, each a plain JSON value or
    a marker: an entry holding a value of a class `read` never stores is a ForeignValue carrying
    the walk's detail, one whose walk or copy raises is UNCOPYABLE, and an entry that is not a
    plain `dict` at all is a ForeignMapping named by its class, so a snapshot never raises or
    runs a rule's methods (`copy.deepcopy` of a rule's object would run its `__deepcopy__`),
    a defect is named where it was made, and a restore can drop what it cannot put back."""
    if logs is None:
        return None
    if type(logs) is not dict:  # a mapping of another class, or any other object, is a rule's write, whatever it holds: a defect named by its class, as `read` reads it, and never copied through its own methods
        return ForeignMapping(type(logs).__name__)
    try:
        pairs = list(logs.items())
    except NOT_A_RULE_DEFECT:
        raise
    except BaseException:  # noqa: BLE001
        return UNCOPYABLE
    out = {}
    for which, log in pairs:
        try:
            detail = _plain_json_defect(log)  # by class, before anything is copied: a snapshot holds plain JSON values or a marker, never an object of a rule's class
            out[which] = copy.deepcopy(log) if detail is None else ForeignValue(detail)
        except NOT_A_RULE_DEFECT:
            raise
        except BaseException:  # noqa: BLE001 - a walk or a copy that raises (a nest past the recursion limit) leaves the entry uncopyable
            out[which] = UNCOPYABLE
    return out


def cache_snapshot(context) -> tuple:
    """The audit-log cache as it stands, for `cache_damage` and `cache_restore` around a
    rule: the context's cache object (CACHE_ABSENT when the context has no cache key; None
    when the key holds None, a defect the rule inherits) and a copy of its `audit_log`
    entry (None when absent; an entry that cannot be copied is UNCOPYABLE). Never raises."""
    cache = context.get(CONTEXT_CACHE, CACHE_ABSENT) if isinstance(context, dict) else CACHE_ABSENT
    logs = cache.get(CACHE_KEY) if type(cache) is dict else None  # by class: a mapping of another class is a defect the probe names, and none of its methods runs here
    return cache, _copy_logs(logs)


def cache_damage(context, snapshot) -> str | None:
    """Why the audit-log cache is not as `snapshot` left it plus what `read` added, or None:
    a shape defect (`cache_defect`), the cache object replaced, or a read stored before the
    rule now gone or changed. A rule may add reads; it may not alter or drop the ones
    already stored, however well-formed what it writes."""
    try:
        cache, logs = snapshot
        now_cache, now_logs = cache_snapshot(context)
        # A defect the rule inherited and left as it was is not the rule's; one it added is,
        # whatever else sits in the cache before it. The inherited defects are what
        # `cache_defects` would have said of the snapshot: none for a context with no cache
        # key, the type for a key holding anything but a dict (None included), the log
        # defects for a dict.
        if cache is CACHE_ABSENT:
            before_defects = []
        elif type(cache) is not dict:
            before_defects = [CACHE_NOT_A_DICT.format(type=type(cache).__name__)]
        else:
            before_defects = _log_defects(logs)
        new_defects = [defect for defect in cache_defects(context) if defect not in before_defects]
        if new_defects:
            return new_defects[0]
        if cache is not CACHE_ABSENT and cache is not None and now_cache is not cache:
            return CACHE_REPLACED
        for which, log in (logs.items() if isinstance(logs, dict) else ()):
            if log is UNCOPYABLE or _read_result_defect(which, log) is not None:  # an inherited defect, not a stored read: a rule that replaced it through `read` is not charged
                continue
            if not isinstance(now_logs, dict) or which not in now_logs or now_logs[which] != log:
                return READ_LOST.format(read=which)
        return None
    except NOT_A_RULE_DEFECT:
        raise
    except BaseException as e:  # noqa: BLE001 - an object whose comparison raises, or exits, is damage too, and the restore must run
        return CACHE_UNREADABLE.format(error=exception_text(e))  # never raises, so the restore runs


def cache_restore(context, snapshot) -> None:
    """Puts the audit-log cache back as `snapshot` saw it: the same cache object under the
    context key (or none), carrying the copied `audit_log` entry (or none; an entry the
    snapshot could not copy is dropped), so a rule's damage reaches no later rule and no
    reader."""
    cache, logs = snapshot
    if not isinstance(context, dict):
        return
    if cache is CACHE_ABSENT:
        context.pop(CONTEXT_CACHE, None)
    else:
        context[CONTEXT_CACHE] = cache  # a key holding None is put back holding None: as found, not popped
    if type(cache) is dict:  # a cache of another class is left as the snapshot saw it: none of its methods runs here
        if logs is None or logs is UNCOPYABLE or isinstance(logs, ForeignMapping):
            cache.pop(CACHE_KEY, None)
        else:
            cache[CACHE_KEY] = {which: log for which, log in logs.items() if log is not UNCOPYABLE and not isinstance(log, ForeignValue)} if isinstance(logs, dict) else logs


def summaries(context) -> dict[str, dict]:
    """The reads' summaries for the report, `{read: {summary key: value}}`, from the cache
    as the reads left it. An entry a rule wrote in another shape is left out, the fold
    having named it as that rule's `unknown`, so the writer sees only what `read` stored.
    Never raises."""
    cache = context.get(CONTEXT_CACHE) if isinstance(context, dict) else None
    logs = cache.get(CACHE_KEY) if type(cache) is dict else None  # by class: a cache of another class is a defect the fold named, and none of its methods runs here
    if type(logs) is not dict:  # a mapping of another class is a rule's write the fold named; the writer shows what `read` stored
        return {}
    try:
        pairs = list(logs.items())
    except NOT_A_RULE_DEFECT:
        raise
    except BaseException:  # noqa: BLE001 - a mapping of a rule's own class whose `items()` raises holds no read the writer can show
        return {}
    out = {}
    for which, log in pairs:
        try:
            if _read_result_defect(which, log) is None:
                out[which] = {key: log[key] for key in SUMMARY_KEYS}
        except NOT_A_RULE_DEFECT:
            raise
        except BaseException:  # noqa: BLE001 - an entry whose `__getitem__` raises is left out, like one of another shape
            continue
    return out


def read_removed(context: dict) -> dict:
    return read(context, READ_REMOVED)


def read_deprecated(context: dict) -> dict:
    return read(context, READ_DEPRECATED)


def sampled_note(log: dict) -> str:
    return SAMPLED_NOTE.format(entries=log["entries"], pages=log["pages"], read=log["read"])

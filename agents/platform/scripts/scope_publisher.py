#!/usr/bin/env python3
# scope_publisher.py - Publish the resolved scope the A2A gateway reads (spec.mode: next).
#
# A folder-, organisation- or selector-scoped install cannot tell from `spec.scope` alone
# whether a project is inside it; the gateway refuses such a target until something it can
# trust has resolved the membership. This program is that resolver. The operator starts it
# as a Job (every 15 minutes, when the membership fields change, and at creation) under
# `spec.mode: next` only, from the agent image, with a ServiceAccount whose one Kubernetes
# grant is `get` and `patch` on the `<name>-resolved-scope` ConfigMap. Nothing an agent turn
# runs can write that ConfigMap.
#
# One run:
#   • reads the rendered `scope.json` and hashes its membership fields (`member_hash`), so
#     the gateway can tell a document resolved for an older declaration;
#   • runs the reconcile's own lookups (cluster_agent_reconcile.py) through the credential
#     broker as the `scope` caller, with `broker_run` as the reconcile's COMMAND_RUNNER:
#     the agent image has no gcloud, and the broker admits this caller exactly the five
#     argv shapes those lookups send;
#   • probes each member with `container clusters list`, so a project the broker's
#     identity cannot read is reported as such rather than admitted;
#   • merge-patches the compact document into the ConfigMap's one data key, with the
#     resourceVersion it read, so two runs that overlap cannot silently overwrite each other.
#
# It exits 0 once a document is written, whatever its rows say, and non-zero only when the
# ConfigMap could not be read or written: the gateway reads the document's own state and
# freshness, and a failed Job is the operator's signal to start the next one.

import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import cluster_agent_reconcile as rec
import scope_policy
from credential_proxy_client import EXIT_SECURITY_POLICY_BLOCKED

# The Job's environment, which the operator renders for it. The scope file is the
# reconcile's own variable, rendered from the same `<name>-config` ConfigMap.
SCOPE_FILE_ENV = rec.SCOPE_FILE_ENV
CONFIGMAP_ENV = "KUBEAGENTS_RESOLVED_SCOPE_CONFIGMAP"
NAMESPACE_ENV = "POD_NAMESPACE"
# spec.harness.projectId. The publisher never probes it and writes no row for it (the
# gateway admits it directly); it fills the cap's first slot, as in the reconcile. It can
# still reach the broker as a declared selector, or by number through a Metrics Scope that
# monitors it. The operator renders it empty when the CR has no harness block; the reconcile
# then takes its management project from the broker's gcloud config, which this Job cannot
# see, so the slot is reserved all the same (`build_document`). If that project is also a
# container member it is counted twice, so the publisher may then leave out a container the
# reconcile lists; it never admits a member the reconcile reads over-cap on this account.
MANAGEMENT_PROJECT_ENV = "KUBEAGENTS_MANAGEMENT_PROJECT"
# The ConfigMap's one data key, and the document's schema version: a reader that does not
# know the version treats the document as missing.
DATA_KEY = "resolved-scope.json"
SCHEMA_VERSION = 1
# The document's wire schema, which the operator and the A2A gateway read in Go: the top-level
# keys, then each row's. The row keys are also the reconcile's entry keys, which is what lets
# the previous document stand in as `previous` for its lookups.
DOC_VERSION = "v"
DOC_RESOLVED_AT = "resolvedAt"
DOC_MEMBER_HASH = "memberHash"
DOC_STATE = "state"
DOC_EXCLUDED = "excludedProjects"
DOC_PROJECTS = "projects"
ROW_ID = "id"
ROW_VIA = "via"
ROW_OUTCOME = "outcome"
ROW_CONFIRMED_AT = "confirmedAt"
# Two optional keys the gateway does not read: the publisher's own carry-forward state, so the
# next run hands the reconcile's lookups the same `previous` its snapshot would. `numbers` is
# the snapshot's number -> ID memo (`_numbers_memo`): without it a project an
# `exclude.projects` number names is admitted again by a folder that lists it by ID whenever
# the number cannot be named. `carried` holds the members `_resolve_projects` counts against
# the cap but that get no row (a monitored project no run could name, keyed by its number; a
# member also declared explicitly or the management project), in the snapshot's row shape, so
# a failed source carries and counts them as the reconcile does. Over-cap members are left
# out, to keep a large over-cap container from making every document too large; the cap count
# can then differ from the reconcile's while a source stays down, and only past the cap: a
# declared source still lists every member it admits.
DOC_NUMBERS = rec.NUMBERS_KEY
DOC_CARRIED = "carried"
ROW_NUMBER = rec.NUMBER_KEY
STATE_OK = "ok"
STATE_TOO_LARGE = "too-large"
STATE_ERROR = "error"
# Past this the document is replaced by a `too-large` one with no rows: a ConfigMap holds at
# most 1 MiB, metadata included, and a document cut short would admit nothing it should not
# but would read as corrupt rather than as a scope too large to publish.
MAX_DOCUMENT_BYTES = 900 * 1024
# memberHash: sha256 over the canonical JSON of the membership fields (`member_hash`). The Go
# operator and gateway compute the same bytes; the shared vector in test_scope_publisher.py
# pins them.
MEMBER_HASH_PREFIX = "sha256:"
MEMBER_HASH_LIST_KEYS = ("projects", "folders", "organizations",
                         rec.SELECTOR_KIND_SHARED_VPC, rec.SELECTOR_KIND_METRICS_SCOPE)
EXCLUDE_KEY = "exclude"
EXCLUDE_PROJECTS_KEY = "projects"
# The broker client, run as a child per command: its `execute()` prints to the process's
# own streams and has no read timeout, and the lookups run four at a time and need each
# command's output, exit code and a timeout that kills it.
BROKER_CLIENT_SCRIPT = Path(__file__).resolve().with_name("credential_proxy_client.py")
# The client reads a KUBECONFIG it is given for every gcloud call and fails when it cannot;
# the Job has no kubeconfig and none of the five shapes needs one.
CLIENT_DROPPED_ENV = ("KUBECONFIG",)
# What a broker policy refusal reads as on stderr. The broker's own message is logged, not
# returned: the lookups classify stderr by substring (`_DENIED_MARKERS`,
# `_API_DISABLED_MARKERS`), and a refusal that happened to say "403" or "is not enabled"
# would turn a refused probe into `denied` or `api-disabled`. This text matches neither, so
# a refusal reads `unreachable`.
POLICY_REFUSED_STDERR = "the credential proxy refused this command for the scope caller"
# The one refusal worth waiting out. A source lookup names only what the publisher's own
# scope.json declares, so a `caller.scope-argument` refusal of one means the broker's copy
# of scope.json (a ConfigMap volume the kubelet refreshes later) does not name the source
# yet: the Job a scope edit starts runs at once. The client prints the rule on a line of
# its own. Only the source shapes wait: a probe or a project-number lookup is refused with
# the same rule when the broker's member window does not hold its argument, which waiting
# does not change. A source lookup is retried every SCOPE_ARGUMENT_RETRY_SECONDS while the
# run's window (SCOPE_ARGUMENT_RETRY_WINDOW_SECONDS from its first such refusal, one window
# for the whole run) lasts, never past the lookup's own timeout, and only while the next
# attempt still gets SCOPE_ARGUMENT_MIN_ATTEMPT_SECONDS; then it is answered as before.
SCOPE_ARGUMENT_REFUSAL_LINE = f"policy rule: {scope_policy.RULE_SCOPE_ARGUMENT}"
# The broker's message for an argument its scope.json does not name, on the line before the
# rule (scope_policy.check). The same rule also answers a broker that cannot read its
# scope.json at all; that refusal does not end by waiting, so it is answered at once.
SCOPE_ARGUMENT_UNDECLARED_PREFIX = "The scope caller may name only what scope.json declares"
SCOPE_ARGUMENT_SOURCE_SHAPES = frozenset(
    {scope_policy.SHAPE_ASSET_SEARCH, scope_policy.SHAPE_SHARED_VPC, scope_policy.SHAPE_METRICS_SCOPE}
)
SCOPE_ARGUMENT_RETRY_SECONDS = 5
SCOPE_ARGUMENT_RETRY_WINDOW_SECONDS = 120
SCOPE_ARGUMENT_MIN_ATTEMPT_SECONDS = 5
# A probe or a project-number lookup refused as not declared means the broker's member window
# no longer holds a member an earlier lookup returned: its scope.json, and the digest the window
# is tied to, changed after the sources resolved (an edit that adds no source, a mount that
# caught up late). A source lookup given up as not declared may be answered once the mount
# catches up. Either way the run resolves every source again and probes again, once, after its
# probes, if the budget still leaves a Metrics Scope's three calls (the lookup, the naming of a
# number, the probe) their minimum attempt. That pass starts no wait of its own: a source still
# not declared then is answered at once.
SCOPE_RECOVERY_MIN_SECONDS = 3 * SCOPE_ARGUMENT_MIN_ATTEMPT_SECONDS
# The broker's argv flag the log leaves out: it names no source, only the output columns.
FORMAT_FLAG_PREFIX = "--format="
# The Kubernetes API, from the pod: the in-cluster token and CA the operator projects at the
# standard path, and the address the kubelet sets.
KUBE_SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
KUBE_TOKEN_FILE = KUBE_SERVICE_ACCOUNT_DIR / "token"
KUBE_CA_FILE = KUBE_SERVICE_ACCOUNT_DIR / "ca.crt"
KUBE_HOST_ENV = "KUBERNETES_SERVICE_HOST"
KUBE_PORT_ENV = "KUBERNETES_SERVICE_PORT"
KUBE_DEFAULT_PORT = "443"
KUBE_API_TIMEOUT_SECONDS = 30
CONFIGMAP_PATH = "/api/v1/namespaces/{namespace}/configmaps/{name}"
MERGE_PATCH_CONTENT_TYPE = "application/merge-patch+json"
JSON_CONTENT_TYPE = "application/json"
EXIT_WRITTEN = 0
EXIT_NOT_WRITTEN = 1
# The sources a row can come from: the publisher writes rows for container and selector
# members only. The management project and explicit projects are admitted by the gateway
# from its own environment and `scope.json`.
ROW_EXCLUDED_VIAS = (rec.VIA_MANAGEMENT, rec.VIA_EXPLICIT)
# How many broker commands the run has in flight at once. The broker gives the `scope` caller
# one command slot by default, so the shell keeps its own beside the profile workers, and it
# refuses a command past that slot at once (503) rather than queue it; a second command in
# flight would read `unreachable` for no reason of its own. The listing budget is the
# reconcile's, sized for LIST_WORKERS at a time, so at this width each command gets a quarter
# of the time; one still running when the budget is spent reads `unreachable`, and its row
# keeps the confirmation it had.
SCOPE_WORKERS = 1
# The outcomes `_resolve_projects` gives a member a source listed this run: None for a
# selector member it named, `ok` for a container member. These are probed. Any other
# outcome is a member every one of whose sources failed, which keeps its previous row, or a
# number a Metrics Scope listed that this run could not name, whose row takes that outcome.
LISTED_OUTCOMES = (None, rec.OUTCOME_OK)


def log(msg: str) -> None:
    print(f"[SCOPE-PUBLISHER] {msg}", file=sys.stderr)


class _ScopeArgumentWait:
    """One run's wait for the broker's scope.json to name a new source, and what the run saw
    of a scope.json change (broker_run).

    `until` is the run's retry window, set at its first `caller.scope-argument` refusal of
    a source lookup. `caught_up` is set when a source lookup that had been refused then
    answered: the broker's scope.json changed during the run, and with it the digest its
    member window is tied to, so the members recorded before the change are gone from it.
    `probe_refused` is set when a probe or a project-number lookup is refused as not
    declared: the same change, seen after the sources resolved. `gave_up` is set when a
    source lookup is still refused as not declared when its wait ends. build_document resets
    all four at the start of a run, resolves the sources again when `caught_up` is set, and
    resolves and probes again once when `probe_refused` or `gave_up` is set and the budget
    still leaves SCOPE_RECOVERY_MIN_SECONDS.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.until: float | None = None
        self.caught_up = False
        self.probe_refused = False
        self.gave_up = False


_scope_argument_wait = _ScopeArgumentWait()


def member_hash(declared: dict) -> str:
    """The hash of a rendered `scope.json`'s membership fields.

    The canonical object holds the five member lists and `exclude.projects`, each sorted
    with duplicates removed (a missing or malformed list is `[]`), and `maxProjects` as the
    reconcile reads it (100 when absent). Keys are sorted, separators carry no spaces, and
    the bytes are UTF-8. `exclude.clusters` is left out on purpose: the gateway applies it
    from `scope.json` directly, so an edit to it does not make the document stale.
    """
    def strings(value) -> list[str]:
        return sorted({v for v in value if isinstance(v, str)}) if isinstance(value, list) else []

    exclude = declared.get(EXCLUDE_KEY) if isinstance(declared.get(EXCLUDE_KEY), dict) else {}
    canonical = {key: strings(declared.get(key)) for key in MEMBER_HASH_LIST_KEYS}
    canonical[EXCLUDE_KEY] = {EXCLUDE_PROJECTS_KEY: strings(exclude.get(EXCLUDE_PROJECTS_KEY))}
    canonical[rec.SCOPE_MAX_PROJECTS_KEY] = rec._cap_of(declared)
    body = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return MEMBER_HASH_PREFIX + hashlib.sha256(body.encode("utf-8")).hexdigest()


def broker_run(argv: list[str], *, check: bool, timeout: float) -> subprocess.CompletedProcess:
    """Run one gcloud argv through the broker's /v1/exec, in the shape `sandbox_exec.run` returns.

    `CompletedProcess` on success; with `check`, `CalledProcessError` carrying the broker's
    stdout and stderr on a non-zero exit, which is what the lookups classify;
    `TimeoutExpired` when the timeout passes, after the child is killed, so no lookup thread
    outlives the run's budget. The broker URL and token file come from the client's own
    variables (CREDENTIAL_PROXY_URL, CREDENTIAL_PROXY_TOKEN_FILE). A source lookup the broker
    refuses because its scope.json does not name the argument yet is retried while it
    catches up (SCOPE_ARGUMENT_REFUSAL_LINE, SCOPE_ARGUMENT_UNDECLARED_PREFIX), inside the
    same timeout; one still refused when the wait ends sets `gave_up`. A probe or a
    project-number lookup refused the same way is not retried; it sets `probe_refused`. Both
    are for build_document.
    """
    env = {k: v for k, v in os.environ.items() if k not in CLIENT_DROPPED_ENV}
    deadline = time.monotonic() + timeout
    shape = scope_policy.match_shape(argv)
    source_lookup = shape is not None and shape[0] in SCOPE_ARGUMENT_SOURCE_SHAPES
    shown = " ".join(a for a in argv if not a.startswith(FORMAT_FLAG_PREFIX))
    attempt_timeout = timeout
    waited = False
    while True:
        try:
            result = subprocess.run([sys.executable, str(BROKER_CLIENT_SCRIPT), *argv],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=attempt_timeout, env=env, check=False)
        except subprocess.TimeoutExpired:
            # An attempt after a wait that runs out of time may have been admitted: the
            # broker's scope.json, and its member window, may have changed. Resolving again
            # costs time and never admits anything, so the run is told it may have.
            if waited:
                _scope_argument_wait.caught_up = True
            raise
        lines = (result.stderr or "").splitlines()
        undeclared = (result.returncode == EXIT_SECURITY_POLICY_BLOCKED
                      and SCOPE_ARGUMENT_REFUSAL_LINE in lines
                      and any(line.startswith(SCOPE_ARGUMENT_UNDECLARED_PREFIX) for line in lines))
        if undeclared and not source_lookup:
            # Not waited for: the window it was refused by does not refill by waiting. The
            # run resolves the sources again after its probes instead.
            _scope_argument_wait.probe_refused = True
        if not (undeclared and source_lookup):
            break
        if _scope_argument_wait.until is None:
            _scope_argument_wait.until = time.monotonic() + SCOPE_ARGUMENT_RETRY_WINDOW_SECONDS
        limit = min(deadline, _scope_argument_wait.until)
        if time.monotonic() + SCOPE_ARGUMENT_RETRY_SECONDS + SCOPE_ARGUMENT_MIN_ATTEMPT_SECONDS >= limit:
            break
        log(f"the broker's scope.json does not name the source in {shown} yet; asking again in "
            f"{SCOPE_ARGUMENT_RETRY_SECONDS}s")
        waited = True
        time.sleep(SCOPE_ARGUMENT_RETRY_SECONDS)
        # A retry gets what is left of the lookup's timeout, never more.
        attempt_timeout = max(deadline - time.monotonic(), 0)
    if undeclared and source_lookup:
        _scope_argument_wait.gave_up = True
    if waited and not undeclared:
        # Any end of a wait but another "not declared" refusal: the broker's answer, or a
        # failure that may hide one. A spurious second resolve costs time, not an admit.
        _scope_argument_wait.caught_up = True
    if result.returncode == EXIT_SECURITY_POLICY_BLOCKED:
        log(f"the broker refused {shown}: {(result.stderr or '').strip()}")
        result = subprocess.CompletedProcess(result.args, result.returncode, result.stdout,
                                             POLICY_REFUSED_STDERR)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result


def _kube_request(method: str, namespace: str, name: str, body: dict | None = None) -> dict:
    """One call on the ConfigMap through the Kubernetes API, and its decoded JSON body.

    Raises what `urlopen` raises (HTTPError for an API refusal, URLError and OSError for the
    rest); the caller treats each as "not read" or "not written".
    """
    host = os.environ.get(KUBE_HOST_ENV, "").strip()
    port = os.environ.get(KUBE_PORT_ENV, "").strip() or KUBE_DEFAULT_PORT
    if ":" in host:
        host = f"[{host}]"
    url = f"https://{host}:{port}" + CONFIGMAP_PATH.format(namespace=namespace, name=name)
    headers = {"Authorization": f"Bearer {KUBE_TOKEN_FILE.read_text(encoding='utf-8').strip()}",
               "Accept": JSON_CONTENT_TYPE}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = MERGE_PATCH_CONTENT_TYPE
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    context = ssl.create_default_context(cafile=str(KUBE_CA_FILE))
    with urllib.request.urlopen(request, timeout=KUBE_API_TIMEOUT_SECONDS, context=context) as response:
        return json.loads(response.read() or b"{}")


def parse_document(raw) -> dict | None:
    """A previous run's document, or None when there is none this run can use.

    A missing key, text that is not JSON, or a version this code does not know all read as
    no previous document: the run resolves from scratch and carries nothing forward.
    """
    if not isinstance(raw, str):
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if (not isinstance(document, dict) or document.get(DOC_VERSION) != SCHEMA_VERSION
            or not isinstance(document.get(DOC_PROJECTS), list)):
        return None
    # Only well-formed fields go on: a malformed one would otherwise reach the next document
    # as it is, or stop every later run, until someone edited the ConfigMap by hand.
    clean = {key: document[key] for key in (DOC_VERSION, DOC_RESOLVED_AT, DOC_MEMBER_HASH, DOC_STATE)
             if isinstance(document.get(key), (int, str))}
    clean[DOC_EXCLUDED] = [p for p in (document.get(DOC_EXCLUDED) if isinstance(document.get(DOC_EXCLUDED), list)
                                       else []) if isinstance(p, str)]
    for key in (DOC_PROJECTS, DOC_CARRIED):
        clean[key] = [_clean_row(row) for row in (document.get(key) if isinstance(document.get(key), list) else [])
                      if _clean_row(row)]
    numbers = document.get(DOC_NUMBERS)
    clean[DOC_NUMBERS] = ({n: p for n, p in numbers.items() if isinstance(n, str) and isinstance(p, str)}
                          if isinstance(numbers, dict) else {})
    # The two optional keys stay out when empty, as in the documents this program writes.
    for key in (DOC_CARRIED, DOC_NUMBERS):
        if not clean[key]:
            del clean[key]
    return clean


def _clean_row(row) -> dict | None:
    """A row or carried entry with its known fields, or None when its id, via or outcome is malformed."""
    if not (isinstance(row, dict) and isinstance(row.get(ROW_ID), str) and isinstance(row.get(ROW_OUTCOME), str)
            and isinstance(row.get(ROW_VIA), list) and all(isinstance(v, str) for v in row[ROW_VIA])):
        return None
    clean = {key: row[key] for key in (ROW_ID, ROW_VIA, ROW_OUTCOME)}
    clean.update({key: row[key] for key in (ROW_CONFIRMED_AT, ROW_NUMBER) if isinstance(row.get(key), str)})
    return clean


def read_previous(namespace: str, name: str) -> tuple[dict | None, str | None]:
    """The ConfigMap's current document and its resourceVersion."""
    configmap = _kube_request("GET", namespace, name)
    metadata = configmap.get("metadata") if isinstance(configmap.get("metadata"), dict) else {}
    data = configmap.get("data") if isinstance(configmap.get("data"), dict) else {}
    return parse_document(data.get(DATA_KEY)), metadata.get("resourceVersion")


def write_document(namespace: str, name: str, text: str, resource_version: str | None) -> None:
    """Merge-patch the data key, only if the ConfigMap is still at the version this run read.

    The API answers 409 when another writer got there first; the run then fails rather than
    replace a newer document with one resolved from an older read.
    """
    body: dict = {"data": {DATA_KEY: text}}
    if resource_version:
        body["metadata"] = {"resourceVersion": resource_version}
    _kube_request("PATCH", namespace, name, body)


def encode_document(document: dict) -> str:
    """The document as compact JSON, or the `too-large` document when it is over the budget."""
    text = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    if len(text.encode("utf-8")) <= MAX_DOCUMENT_BYTES:
        return text
    log(f"the resolved scope is {len(text.encode('utf-8'))} bytes, over the {MAX_DOCUMENT_BYTES}-byte "
        f"budget; publishing {STATE_TOO_LARGE} with no rows.")
    # The number memo rides along: it is what keeps a number entry excluding its project on
    # the run after this one, and it is bounded by the Metrics Scopes' members.
    too_large = {DOC_VERSION: SCHEMA_VERSION, DOC_RESOLVED_AT: document[DOC_RESOLVED_AT],
                 DOC_MEMBER_HASH: document[DOC_MEMBER_HASH], DOC_STATE: STATE_TOO_LARGE,
                 DOC_EXCLUDED: [], DOC_PROJECTS: []}
    if document.get(DOC_NUMBERS):
        too_large[DOC_NUMBERS] = document[DOC_NUMBERS]
    return json.dumps(too_large, separators=(",", ":"), ensure_ascii=False)


def read_scope() -> dict | None:
    """The rendered `scope.json` as an object, or None when it cannot be read."""
    path = os.environ.get(SCOPE_FILE_ENV, "").strip()
    if not path:
        log(f"{SCOPE_FILE_ENV} is not set.")
        return None
    try:
        declared = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log(f"could not read the scope declaration at {path} ({e}).")
        return None
    if not isinstance(declared, dict):
        log(f"the scope declaration at {path} is not a JSON object.")
        return None
    return declared


def _now() -> str:
    return datetime.now(timezone.utc).strftime(rec.SNAPSHOT_TIME_FORMAT)


def _lookup_previous(previous: dict | None) -> dict | None:
    """The previous document in the reconcile snapshot's shape: its rows and its `carried`
    members under `projects`, and its number memo."""
    if previous is None:
        return None
    # Only well-formed rows: a value of the wrong type here would fail every run before it
    # writes, until someone edited the ConfigMap by hand.
    rows = [row for key in (DOC_PROJECTS, DOC_CARRIED)
            for row in (previous.get(key) if isinstance(previous.get(key), list) else [])
            if isinstance(row, dict) and isinstance(row.get(ROW_ID), str)
            and isinstance(row.get(ROW_VIA), list) and all(isinstance(v, str) for v in row[ROW_VIA])]
    numbers = previous.get(DOC_NUMBERS)
    return {DOC_PROJECTS: rows, DOC_NUMBERS: numbers if isinstance(numbers, dict) else {}}


def _excluded_members(searches: dict, selections: dict, previous: dict | None, patterns: list[str],
                      management: str | None) -> list[str]:
    """The container and selector members an `exclude.projects` entry dropped this run, by ID.

    The members are the ones a source listed this run and the previous rows a failed source
    carries forward, the two sets `_resolve_projects` applies the entries to. A member matches
    on its ID, or on the number a Metrics Scope named it by. A member still keyed by a bare
    number is left out (the document names projects by ID only), and so is the management
    project, which no entry excludes.
    """
    named = rec._numbers_named(selections)
    sources = {**searches, **selections}
    members = {project for found, _ in sources.values() for project in found or {}}
    failed = {source for source, (found, _) in sources.items() if found is None}
    members |= {row[ROW_ID] for row in (previous or {}).get(DOC_PROJECTS, [])
                if isinstance(row, dict) and isinstance(row.get(ROW_ID), str)
                and failed.intersection(row.get(ROW_VIA) or [])}

    def number_of(project: str) -> str | None:
        return named.get(project) or rec._previous_number(previous, project)

    return sorted(p for p in members
                  if p != management and not p.isdigit()
                  and (rec._excluded_by(p, patterns) or ((n := number_of(p)) and rec._excluded_by(n, patterns))))


def _merge_pass(earlier: dict, later: dict, kept: set) -> dict:
    """A later resolve pass over an earlier one: a source keeps its earlier answer when its
    later lookup gave none (a pass on what is left of the budget can run out of it).

    `kept` holds the sources whose answer is an earlier pass's, updated here: its members were
    recorded under the broker's scope.json as it was then, so a probe of one can be refused.
    """
    merged = dict(earlier)
    for source, answer in later.items():
        if answer[0] is not None or source not in merged:
            merged[source] = answer
            kept.discard(source)
        elif merged[source][0] is not None:
            kept.add(source)
    return merged


def _merge_selections(earlier: dict, later: dict) -> dict:
    """A later naming pass over an earlier one: a selector member keeps the project ID an
    earlier pass named its number by when its later naming call gave none."""
    merged = dict(later)
    for selector, (members, outcome) in later.items():
        before = (earlier.get(selector) or (None, None))[0]
        if members is None or not before:
            continue
        named_before = {member[rec.NUMBER_KEY]: (project, member) for project, member in before.items()
                        if member.get(ROW_OUTCOME) is None and member.get(rec.NUMBER_KEY)}
        fixed: dict = {}
        for key, member in members.items():
            number = member.get(rec.NUMBER_KEY)
            if member.get(ROW_OUTCOME) is not None and number in named_before:
                project, earlier_member = named_before[number]
                fixed.setdefault(project, earlier_member)
            else:
                fixed.setdefault(key, member)
        merged[selector] = (fixed, outcome)
    return merged


def build_document(declared: dict, management: str | None, previous: dict | None) -> dict:
    """Resolve the declaration and probe its members into a document with `state: ok`, or
    `error` when every source lookup failed and no earlier run resolved.

    The lookups, the exclusions, the cap and the carry-forward of a failed source are the
    reconcile's (`_resolve_groups`, `_selector_members`, `_resolve_projects`), under one
    listing budget for the cap. A member a source listed this run is probed with
    `clusters list`; `ok` stamps `confirmedAt` with this run's time, any other answer is the
    row's outcome and keeps the time it had. A member carried from a failed source keeps
    its previous row's outcome and `confirmedAt`: the source did not list it, so nothing
    this run can confirm it. When the broker's scope.json changes during the run, the sources
    resolve again, and a probe refused as not declared resolves and probes once more, inside
    the budget; a later pass keeps a source's earlier answer when its own lookup gives none.
    """
    present = declared.get(rec.SCOPE_PRESENT_KEY) is True
    scope = rec._normalize_scope(declared) if present else rec._empty_scope()
    looked_up = _lookup_previous(previous)
    cap = rec._cap_of(scope)
    budget = rec._list_budget_seconds(cap)
    deadline = time.monotonic() + budget
    patterns = scope[EXCLUDE_KEY][EXCLUDE_PROJECTS_KEY]
    sources = rec._container_ids(scope) + rec._selector_ids(scope)

    def place(groups: dict, earlier: dict | None = None) -> tuple[dict, dict, dict, list, list]:
        searches = {g: r for g, r in groups.items() if rec._is_container(g)}
        selector_reports = {g: r for g, r in groups.items() if rec._is_selector(g)}
        selections = rec._selector_members(selector_reports, looked_up, deadline, patterns, SCOPE_WORKERS, budget)
        if earlier is not None:
            selections = _merge_selections(earlier, selections)
        # With no management project named, its slot is still reserved: the cap passed on is
        # one smaller, which is what `_resolve_projects` makes of a management project it counts.
        entries, _, _, _ = rec._resolve_projects(management, scope, searches, looked_up, selections,
                                                 cap if management else cap - 1)
        counted = [e for e in entries if e[ROW_OUTCOME] != rec.OUTCOME_OVER_CAP]
        members = [e for e in counted
                   if not e[ROW_ID].isdigit() and not any(via in ROW_EXCLUDED_VIAS for via in e[ROW_VIA])]
        # A source can carry a counted member only when the member's row names it.
        member_ids = {e[ROW_ID] for e in members}
        carried = [{ROW_ID: e[ROW_ID], ROW_VIA: e[ROW_VIA], ROW_OUTCOME: e[ROW_OUTCOME] or rec.OUTCOME_OK,
                    **({ROW_NUMBER: e[ROW_NUMBER]} if e.get(ROW_NUMBER) else {})}
                   for e in counted if e[ROW_ID] not in member_ids
                   and any(rec._is_container(via) or rec._is_selector(via) for via in e[ROW_VIA])]
        return searches, selector_reports, selections, members, carried

    def probe(members: list) -> dict:
        return rec._bounded_map(rec._list_project, [e[ROW_ID] for e in members if e[ROW_OUTCOME] in LISTED_OUTCOMES],
                                deadline, "probing", SCOPE_WORKERS, budget,
                                consequence="its row is not confirmed this run")

    _scope_argument_wait.reset()
    kept: set = set()
    groups = rec._resolve_groups(sources, deadline, SCOPE_WORKERS, budget)
    if _scope_argument_wait.caught_up:
        # The broker's scope.json changed while the sources resolved, and the member
        # window went with its old digest: resolve them all again, so the members of every
        # source that answers again are recorded under the new one.
        log("the broker's scope.json changed during the run; resolving every source again")
        groups = _merge_pass(groups, rec._resolve_groups(sources, deadline, SCOPE_WORKERS, budget), kept)
    searches, selector_reports, selections, members, carried = place(groups)
    probes = probe(members)
    if ((_scope_argument_wait.probe_refused or _scope_argument_wait.gave_up)
            and deadline - time.monotonic() >= SCOPE_RECOVERY_MIN_SECONDS):
        # The window lost members the sources had returned, or a source gave up before the
        # mount caught up: the broker's scope.json changed, or may have, after the sources
        # resolved. Resolve them again under its digest now, then probe again what a refusal
        # left `unreachable` and what was not probed. Once: a second change in the same run is
        # left to the next run.
        reason = ("the broker refused a probe as not declared" if _scope_argument_wait.probe_refused
                  else "a source's wait for the broker's scope.json ran out")
        log(f"{reason}; resolving every source again and probing again")
        _scope_argument_wait.until = time.monotonic()
        groups = _merge_pass(groups, rec._resolve_groups(sources, deadline, SCOPE_WORKERS, budget), kept)
        searches, selector_reports, selections, members, carried = place(groups, selections)
        probes.update(probe([e for e in members if probes.get(e[ROW_ID], (None, rec.OUTCOME_UNREACHABLE))[1]
                             == rec.OUTCOME_UNREACHABLE]))
    rows_before = {row[ROW_ID]: row for row in (previous or {}).get(DOC_PROJECTS, [])
                   if isinstance(row, dict) and isinstance(row.get(ROW_ID), str)}
    resolved_at = _now()
    failed_sources = {source for source, (found, _) in groups.items() if found is None}
    rows = []
    for entry in sorted(members, key=lambda e: e[ROW_ID]):
        before = rows_before.get(entry[ROW_ID], {})
        confirmed = before.get(ROW_CONFIRMED_AT)
        if entry[ROW_OUTCOME] in LISTED_OUTCOMES:
            _, outcome = probes[entry[ROW_ID]]
            if outcome == rec.OUTCOME_OK:
                confirmed = resolved_at
            elif before and kept.issuperset(entry[ROW_VIA]):
                # Every source of it kept an answer from before the broker's scope.json changed,
                # so the probe could not be admitted: the previous row stands, as for a failed
                # source.
                outcome = before.get(ROW_OUTCOME) or outcome
        elif failed_sources.issuperset(entry[ROW_VIA]):
            outcome = before.get(ROW_OUTCOME) or entry[ROW_OUTCOME]
        else:
            # A source that answered placed it: a Metrics Scope listed its number, which could
            # not be named.
            outcome = entry[ROW_OUTCOME]
        row = {ROW_ID: entry[ROW_ID], ROW_VIA: entry[ROW_VIA], ROW_OUTCOME: outcome}
        if isinstance(confirmed, str):
            row[ROW_CONFIRMED_AT] = confirmed
        rows.append(row)
    numbers = rec._numbers_memo(looked_up, selector_reports, rec._numbers_named(selections),
                                present and any(kind in declared for kind in rec.SELECTOR_KINDS), patterns)
    # The reconcile also keeps a member's number on its snapshot row for as long as the member
    # stays in the set, after the memo has dropped the pair; these rows carry no number, so the
    # memo keeps the pair instead, and a number entry added later still excludes the project.
    named = rec._numbers_named(selections)
    for entry in members + carried:
        number = entry.get(ROW_NUMBER) or named.get(entry[ROW_ID]) or rec._previous_number(looked_up, entry[ROW_ID])
        if number and number != entry[ROW_ID]:
            numbers[number] = entry[ROW_ID]
    numbers = dict(sorted(numbers.items()))
    document = {DOC_VERSION: SCHEMA_VERSION, DOC_RESOLVED_AT: resolved_at, DOC_MEMBER_HASH: member_hash(declared),
                DOC_STATE: STATE_OK,
                DOC_EXCLUDED: _excluded_members(searches, selections, looked_up, patterns, management),
                DOC_PROJECTS: rows}
    if numbers:
        document[DOC_NUMBERS] = numbers
    if carried:
        document[DOC_CARRIED] = carried
    if groups and all(found is None for found, _ in groups.values()):
        # Every source lookup failed (a broker that refuses the `scope` caller, for one): the
        # run learned nothing, so it does not restamp the document. A short outage then
        # changes nothing before the bound; one that lasts lets the document go stale, which
        # the gateway refuses and the operator reports. Only an `ok` document's time is a time
        # a run resolved; with none to keep, the run has nothing it could publish as resolved.
        kept = (previous or {}).get(DOC_RESOLVED_AT)
        if (previous or {}).get(DOC_STATE) != STATE_OK or not isinstance(kept, str):
            log(f"every source lookup failed and no earlier run resolved; publishing {STATE_ERROR}.")
            return error_document(declared, previous)
        log(f"every source lookup failed; keeping resolvedAt {kept} from the last run that resolved.")
        document[DOC_RESOLVED_AT] = kept
    return document


def error_document(declared: dict | None, previous: dict | None) -> dict:
    """A document with `state: error`, which admits nothing.

    The previous rows, number memo and carried members ride along, so the next run that can
    resolve still carries a failed source's members forward from them.
    """
    document = {DOC_VERSION: SCHEMA_VERSION, DOC_RESOLVED_AT: _now(),
                DOC_MEMBER_HASH: member_hash(declared) if declared is not None else "",
                DOC_STATE: STATE_ERROR, DOC_EXCLUDED: (previous or {}).get(DOC_EXCLUDED, []),
                DOC_PROJECTS: (previous or {}).get(DOC_PROJECTS, [])}
    for key in (DOC_NUMBERS, DOC_CARRIED):
        if (previous or {}).get(key):
            document[key] = previous[key]
    return document


def main() -> int:
    namespace = os.environ.get(NAMESPACE_ENV, "").strip()
    name = os.environ.get(CONFIGMAP_ENV, "").strip()
    if not namespace or not name:
        log(f"{NAMESPACE_ENV} and {CONFIGMAP_ENV} must both be set; nothing written.")
        return EXIT_NOT_WRITTEN
    started = time.monotonic()
    try:
        previous, resource_version = read_previous(namespace, name)
    except Exception as e:  # noqa: BLE001 - any failure to read is "not read"
        log(f"could not read ConfigMap {namespace}/{name} ({e}); nothing written.")
        return EXIT_NOT_WRITTEN
    declared = read_scope()
    if declared is None:
        log(f"publishing {STATE_ERROR}.")
        document = error_document(declared, previous)
    else:
        rec.COMMAND_RUNNER = broker_run
        document = build_document(declared, os.environ.get(MANAGEMENT_PROJECT_ENV, "").strip() or None, previous)
    text = encode_document(document)
    try:
        write_document(namespace, name, text, resource_version)
    except Exception as e:  # noqa: BLE001 - any failure to write is "not written"
        log(f"could not write ConfigMap {namespace}/{name} ({e}); the next run publishes.")
        return EXIT_NOT_WRITTEN
    published = json.loads(text)
    outcomes: dict[str, int] = {}
    for row in published[DOC_PROJECTS]:
        outcomes[row[ROW_OUTCOME]] = outcomes.get(row[ROW_OUTCOME], 0) + 1
    log(f"published {published[DOC_STATE]}: {len(published[DOC_PROJECTS])} project(s) {outcomes or ''}, "
        f"{len(published[DOC_EXCLUDED])} excluded, in {time.monotonic() - started:.0f}s.")
    return EXIT_WRITTEN


if __name__ == "__main__":
    sys.exit(main())

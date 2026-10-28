#!/usr/bin/env python3
"""sa_role_watch.py - say in chat when the agent's Google service account holds a role beyond the read-only set.

The install grants the agent's Google service account the ``read-only``
permission set: viewer roles and nothing that can create or change a cluster.
The installer warns when a ``custom`` set names an admin role, but only at the
moment the roles are chosen. A role granted later, by hand or by another tool,
goes unnoticed, and it is what the Google-hosted GKE MCP server acts with, since
the agent container calls it on that account's credentials: an account holding
``roles/container.admin`` let it create a cluster no pull request ever proposed
(#2941, #2935).

This is a ``no_agent`` entry on the Platform Agent's roster. The tick prompts
no model. Each tick:

1. reads the identities to check from the management cluster: the
   ``PlatformAgent`` in this namespace names the Kubernetes ServiceAccount the
   agent and broker pods run as, whose ``iam.gke.io/gcp-service-account``
   annotation is the Google service account the agent container's own
   application-default credentials resolve to, and the CR's
   ``spec.security.workloadIdentityFederation`` names the one the credential
   broker impersonates when federation is armed. Without federation the broker
   runs on the annotation's account too, because both pods share the
   ServiceAccount. With federation armed, an absent annotation is said once:
   the agent container then has no Google credential of its own. With neither
   the annotation nor federation there is no account to check, and the tick
   reports a check failure, once;
2. lists the projects: the management project and the resolved
   ``spec.scope`` set the cluster-agent reconcile snapshots
   (``fleet_scope_targets``), or the management project alone when no scope is
   declared;
3. per project, reads ``gcloud projects get-iam-policy`` and keeps every role
   bound to one of those accounts that is not in ``READ_ONLY_ROLES``;
4. diffs that against the ledger the previous tick left. Chat hears a role on
   the tick it first appears and again when it goes, and a role still held is
   said again once a day, on the first tick from ``REMIND_HOUR_UTC``, as
   ``kanban-board-health`` repeats a card still blocked: a grant said once can be
   missed, and one line in chat should not be the only word on it. Any other
   tick on which nothing changed prints nothing. A project whose policy could not be read keeps its
   last answer, since absence of evidence is not removal, and its failure is
   said once and its recovery once.

Every ``gcloud`` and ``kubectl`` call runs in the shell sandbox through
``sandbox_exec.run``, so the credential broker runs it with the credential it
holds, which does not depend on the agent pod's own Workload Identity binding.
Both ``gcloud`` verbs are on the broker's read allowlist
(``command_policy.GCLOUD_READ_COMMANDS``), and the ``read-only`` set carries
``roles/iam.securityReviewer``, which reads a project's policy. Nothing here
mutates anything.

What it cannot see. ``projects get-iam-policy`` returns the bindings on the
project itself. A role granted on a folder or the organisation above it, or to
a group the account is a member of, is not in that answer, and reading those
would take verbs the broker does not allow. A role bound to the ServiceAccount's
own Workload Identity principal (``principal://.../subject/ns/<ns>/sa/<ksa>``)
is in the answer but not checked: only Google service accounts are (#3450). A ``custom`` permission set chosen
on purpose is reported like any other grant beyond ``read-only``: nothing at
runtime knows which roles the installer was given.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Siblings in `$HERMES_HOME/scripts`, this script's own directory and therefore
# already on `sys.path` when the scheduler runs it as a plain subprocess.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_scope_targets  # noqa: E402
import gitops_workspace  # noqa: E402
import sandbox_exec  # noqa: E402

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

#: The `read-only` permission set, as `local.read_only_roles` in
#: terraform/examples/full-install/main.tf grants it. Written out because the
#: pod has no Terraform to read; tests/test_agent_iam_ceiling.py asserts this
#: equals the list that test pins, so the three cannot drift apart.
READ_ONLY_ROLES = (
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/gkehub.viewer",
    "roles/cloudtrace.user",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
    "roles/serviceusage.serviceUsageConsumer",
)

PROFILES_DIR = "profiles"
PLATFORM_PROFILE = "platform"
CRON_DIR = "cron"
STATE_FILE_NAME = "sa_role_watch.json"
STATE_PATH_ENV = "SA_ROLE_WATCH_STATE"
STATE_SCHEMA_VERSION = 1
STATE_TMP_SUFFIX = ".tmp"

#: The management project: the watch's own override first, then the one the
#: operator sets on the agent container from spec.harness.projectID.
PROJECT_ENVS = ("SA_ROLE_WATCH_PROJECT", "GCP_PROJECT_ID")
CONFIG_PROJECT_ARGV = ("gcloud", "config", "get-value", "project")
#: The namespace the PlatformAgent and its ServiceAccount live in, as the
#: operator sets it on the agent container.
NAMESPACE_ENV = "KUBE_DEFAULT_NAMESPACE"
DEFAULT_NAMESPACE = "kubeagents-system"
PLATFORM_AGENT_RESOURCE = "platformagents.kubeagents.x-k8s.io"
SERVICE_ACCOUNT_RESOURCE = "serviceaccount"
WORKLOAD_IDENTITY_ANNOTATION = "iam.gke.io/gcp-service-account"

#: Where each identity comes from, as the chat line names it.
AGENT_POD_SOURCE = "agent pod"
BROKER_SOURCE = "credential broker"
#: How a policy member names a service account. A deleted account's binding
#: reads `deleted:serviceAccount:...` and so never matches: nothing holds it.
SERVICE_ACCOUNT_MEMBER_PREFIX = "serviceAccount:"

#: Each project's policy is one gcloud process at the credential broker, whose
#: slots are shared with kanban workers; four at a time is the width
#: stall_watch.py's and cluster_agent_reconcile.py's listing pools use.
POLICY_WORKERS = 4
KUBECTL_TIMEOUT_SECONDS = 60
PROJECT_LOOKUP_TIMEOUT_SECONDS = 30
POLICY_TIMEOUT_SECONDS = 60
STDERR_EXCERPT_CHARS = 300

WATCH_NAME = "Service account role watch"
DRIFT_PREFIX = f"⚠️ **{WATCH_NAME}:**"
CLEARED_PREFIX = f"✅ **{WATCH_NAME}:**"
CHECK_FAILED_PREFIX = f"⚠️ **{WATCH_NAME} — check failed:**"
CHECK_RECOVERED_LINE = f"✅ **{WATCH_NAME}** — the check runs again."
PROJECT_UNREAD_PREFIX = f"⚠️ **{WATCH_NAME} — project not checked:**"
PROJECT_RECOVERED_PREFIX = f"✅ **{WATCH_NAME}** — checked again:"
UNWATCHED_PREFIX = f"ℹ️ **{WATCH_NAME}:**"
SCOPE_RECOVERED_LINE = f"✅ **{WATCH_NAME}** — every project in the scope is checked again."
DRIFT_ADVICE = (
    "These are outside the read-only set: anything running on this account can use them without "
    "a pull request. Remove them unless they were granted on purpose."
)
NO_ANNOTATION_NOTE = (
    f"ℹ️ **{WATCH_NAME}:** the agent's ServiceAccount `{{account}}` carries no "
    f"`{WORKLOAD_IDENTITY_ANNOTATION}` annotation, so the agent container has no Google credential "
    "of its own; only the credential broker's account is checked."
)
ANNOTATION_RESTORED_NOTE = (
    f"ℹ️ **{WATCH_NAME}:** the agent's ServiceAccount `{{account}}` names `{{email}}` for Workload "
    "Identity again; that account is checked too."
)
LEDGER_KEY_SEPARATOR = "|"
CONDITIONAL_SUFFIX = " (conditional)"
# The two public principals: a role bound to either is held by every account,
# the agent's included, so it is named for each checked account and marked.
PUBLIC_MEMBERS = frozenset({"allusers", "allauthenticatedusers"})
PUBLIC_SUFFIX = " (public)"
#: Over-cap projects a scope note names before counting the rest.
MAX_PROJECTS_IN_NOTE = 8
# A role still held is said again on the first tick from this hour each UTC
# day. 12:00 for the reason kanban-board-health fires at 12:35: an earlier UTC
# hour is the middle of the night in the US.
REMIND_HOUR_UTC = 12
UNRESOLVED_SCOPE_NOTE = "folder, organisation or selector members the reconcile has not resolved are not checked"
#: What an account read off the ServiceAccount or the CR must look like before
#: it is checked or named in chat: those values are whatever whoever can patch
#: them writes, and a backtick or newline would leave the line's code span.
ACCOUNT_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")
#: Characters kept out of a tool's error text before chat quotes it.
CHAT_UNSAFE_CHARS = str.maketrans({"`": "'"})
#: The ledger's record of an annotation that was read and absent.
ANNOTATION_ABSENT = ""
#: The ledger's key for a scope that could not be read, beside the projects.
SCOPE_ERROR_KEY = "spec.scope"

# --------------------------------------------------------------------------
# sandbox reads
# --------------------------------------------------------------------------


def run_sandbox(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """One hop into the shell sandbox; nothing from this environment crosses."""
    return sandbox_exec.run(argv, timeout=timeout, check=False)


def stderr_excerpt(text: str | None) -> str:
    """One line of a tool's error output, short, with nothing in it that could
    end a chat line's formatting."""
    return " ".join((text or "").split()).translate(CHAT_UNSAFE_CHARS)[:STDERR_EXCERPT_CHARS]


def failure_text(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {int(exc.timeout)}s"
    if isinstance(exc, RuntimeError):
        return stderr_excerpt(str(exc))
    return stderr_excerpt(f"{type(exc).__name__}: {exc}")


def read_json(argv: list[str], *, timeout: float):
    r = run_sandbox(argv, timeout=timeout)
    what = " ".join(argv[:3])
    if r.returncode != 0:
        raise RuntimeError(f"{what} exited {r.returncode}: {stderr_excerpt(r.stderr)}")
    try:
        return json.loads(r.stdout or "null")
    except ValueError as exc:
        raise RuntimeError(f"{what} returned unparsable output: {exc}") from exc


def namespace() -> str:
    return os.environ.get(NAMESPACE_ENV, "").strip() or DEFAULT_NAMESPACE


def management_project() -> str | None:
    for name in PROJECT_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    r = run_sandbox(list(CONFIG_PROJECT_ARGV), timeout=PROJECT_LOOKUP_TIMEOUT_SECONDS)
    return r.stdout.strip() or None


def read_identities(ns: str) -> tuple[dict[str, list[str]], str, str | None]:
    """The accounts to check, each with where it comes from, plus the agent's
    ServiceAccount and the annotation it carries (None when absent). Raises
    when the management cluster cannot answer, or names nothing to check."""
    listed = read_json(
        ["kubectl", "get", PLATFORM_AGENT_RESOURCE, "-n", ns, "-o", "json"], timeout=KUBECTL_TIMEOUT_SECONDS
    )
    items = (listed or {}).get("items") or []
    if not items:
        raise RuntimeError(f"no PlatformAgent in namespace {ns}")
    # A webhook admits one PlatformAgent per cluster; the first is the install.
    agent = items[0]
    security = (agent.get("spec") or {}).get("security") or {}
    account = security.get("serviceAccountName") or (agent.get("metadata") or {}).get("name") or ""
    if not account:
        raise RuntimeError(f"the PlatformAgent in namespace {ns} names no ServiceAccount")
    sa = read_json(
        ["kubectl", "get", SERVICE_ACCOUNT_RESOURCE, account, "-n", ns, "-o", "json"], timeout=KUBECTL_TIMEOUT_SECONDS
    )
    annotated = (((sa or {}).get("metadata") or {}).get("annotations") or {}).get(WORKLOAD_IDENTITY_ANNOTATION, "").strip() or None

    # Federation is armed only with both fields set (credential_proxy_manifests.go
    # refuses a half-filled block); otherwise the broker uses the metadata
    # server, and so the annotation's account, like the agent pod.
    wif = security.get("workloadIdentityFederation") or {}
    federated = (wif.get("serviceAccountEmail") or "").strip() if (wif.get("audience") or "").strip() else ""

    for value, where in ((annotated, f"the ServiceAccount's {WORKLOAD_IDENTITY_ANNOTATION} annotation"),
                         (federated, "spec.security.workloadIdentityFederation.serviceAccountEmail")):
        if value and not ACCOUNT_EMAIL_PATTERN.match(value):
            raise RuntimeError(f"{where} is not a service account email; nothing was checked")
    identities: dict[str, list[str]] = {}
    if annotated:
        identities.setdefault(annotated.lower(), []).append(AGENT_POD_SOURCE)
    broker = federated or annotated
    if broker:
        identities.setdefault(broker.lower(), []).append(BROKER_SOURCE)
    if not identities:
        raise RuntimeError(
            f"neither the ServiceAccount {ns}/{account} nor the PlatformAgent names a Google service account"
        )
    return identities, f"{ns}/{account}", annotated


def projects_to_check(management: str) -> tuple[list[str], str | None, bool]:
    """The management project and the declared scope's projects, and a note
    naming what the scope leaves unchecked (the management project is always
    checked).

    A project the reconcile could not read (`denied`, `unreachable`, or not yet
    resolved on a fresh install) is still checked: its cluster listing failing
    says nothing about whether its policy can be read, and a failure is named
    like any other. A project past the scope's `maxProjects` is not, since the
    cap is the operator's bound on how many projects a tick touches; the note
    names them instead.

    The flag says whether the list is the whole scope. A scope that could not
    be read, or that the reconcile has not resolved in full, is not: a project
    missing from it this tick may be back next tick, so its recorded rows are
    held rather than said to be no longer checked."""
    try:
        targets = fleet_scope_targets.declared_scope_targets()
    except fleet_scope_targets.ScopeRenderUnreadable as exc:
        return [management], stderr_excerpt(str(exc)), False
    if targets is None:
        return [management], None, True
    unread = [p for p, outcome in targets.unread if p != fleet_scope_targets.UNRESOLVED_SCOPE_ROW]
    over_cap = [p for p, outcome in targets.unread if outcome == fleet_scope_targets.OUTCOME_OVER_CAP]
    checked = [p for p in [*targets.projects, *unread] if p not in over_cap]
    # The snapshot lists the management project by id, first. A management
    # project the operator was given by number would otherwise be read twice
    # and every drift in it said twice, under each spelling.
    if not (management.isdigit() and targets.projects):
        checked.insert(0, management)
    notes = []
    complete = True
    if targets.note or any(p == fleet_scope_targets.UNRESOLVED_SCOPE_ROW for p, _ in targets.unread):
        notes.append(UNRESOLVED_SCOPE_NOTE)
        complete = False
    if over_cap:
        # Plain text: the note is posted inside the failure line's code span.
        shown = ", ".join(sorted(over_cap[:MAX_PROJECTS_IN_NOTE]))
        rest = len(over_cap) - MAX_PROJECTS_IN_NOTE
        notes.append(f"{len(over_cap)} project(s) past the scope's maxProjects are not checked: {shown}" + (f" and {rest} more" if rest > 0 else ""))
    return list(dict.fromkeys(checked)), "; ".join(notes) or None, complete


def roles_beyond_read_only(policy: dict, emails: set[str]) -> dict[str, set[str]]:
    """Per account, each role bound to it that the read-only set lacks. A
    conditional binding is marked as one, since the grant then holds only
    where the condition does; its title is free text whoever sets the policy
    writes, so it stays out of a line chat posts. A role bound to ``allUsers``
    or ``allAuthenticatedUsers`` is held by every account, so it is named for
    each checked account and marked public."""
    allowed = set(READ_ONLY_ROLES)
    members = {(SERVICE_ACCOUNT_MEMBER_PREFIX + e).lower(): e for e in emails}
    found: dict[str, set[str]] = {}
    for binding in (policy or {}).get("bindings") or []:
        role = binding.get("role") or ""
        if not role or role in allowed:
            continue
        label = f"{role}{CONDITIONAL_SUFFIX}" if binding.get("condition") else role
        for member in binding.get("members") or []:
            if str(member).lower() in PUBLIC_MEMBERS:
                for email in emails:
                    found.setdefault(email, set()).add(label + PUBLIC_SUFFIX)
                continue
            email = members.get(str(member).lower())
            if email:
                found.setdefault(email, set()).add(label)
    return found


def read_project(project: str, emails: set[str]) -> dict[str, set[str]]:
    policy = read_json(
        ["gcloud", "projects", "get-iam-policy", project, "--format=json"], timeout=POLICY_TIMEOUT_SECONDS
    )
    return roles_beyond_read_only(policy, emails)


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------


def empty_state() -> dict:
    return {
        "version": STATE_SCHEMA_VERSION,
        "roles": {},
        "said_on": {},
        "project_errors": {},
        "check_error": None,
        "annotation": None,
        "updated_at": None,
    }


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict) or data.get("version") != STATE_SCHEMA_VERSION:
        return empty_state()
    state = empty_state()
    state.update({k: data.get(k, v) for k, v in state.items()})
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + STATE_TMP_SUFFIX)
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def ledger_key(email: str, project: str) -> str:
    return f"{email}{LEDGER_KEY_SEPARATOR}{project}"


def code_list(items) -> str:
    return ", ".join(f"`{i}`" for i in sorted(items))


# --------------------------------------------------------------------------
# tick
# --------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def tick(state_path: Path, *, dry_run: bool) -> list[str]:
    clock = utc_now()
    now = clock.isoformat(timespec="seconds")
    today = clock.date().isoformat()
    remind = clock.hour >= REMIND_HOUR_UTC
    state = load_state(state_path)
    lines: list[str] = []

    def check_failed(exc: BaseException) -> list[str]:
        # The ledger's rows are kept as they are: nothing was read.
        text = failure_text(exc)
        if state.get("check_error") != text:
            lines.append(f"{CHECK_FAILED_PREFIX} `{text}`")
        state["check_error"] = text
        state["updated_at"] = now
        if not dry_run:
            save_state(state_path, state)
        return lines

    try:
        management = management_project()
        if not management:
            raise RuntimeError(f"no GCP project: set {PROJECT_ENVS[0]} or configure gcloud in the sandbox")
        identities, account, annotated = read_identities(namespace())
        projects, scope_note, scope_complete = projects_to_check(management)
    except Exception as exc:  # noqa: BLE001 - a failed check is reported once, not raised every tick
        return check_failed(exc)

    emails = set(identities)
    with ThreadPoolExecutor(max_workers=POLICY_WORKERS) as pool:
        futures = {project: pool.submit(read_project, project, emails) for project in projects}
    # With the sandbox gone no project can be read, and one cause is one line,
    # not one per project (stall_watch.py's READ_FAILURES makes the same call).
    for future in futures.values():
        if isinstance(future.exception(), sandbox_exec.SandboxUnavailable):
            return check_failed(future.exception())

    if state.get("check_error"):
        lines.append(CHECK_RECOVERED_LINE)
    state["check_error"] = None

    # The annotation is said when it goes and when it comes back, not every
    # tick. The ledger keeps "" for absent and None before the first read, so a
    # first tick says it only when absent: present is the expected state.
    previous_annotation = state.get("annotation")
    if annotated is None and previous_annotation != ANNOTATION_ABSENT:
        lines.append(NO_ANNOTATION_NOTE.format(account=account))
    elif annotated and previous_annotation == ANNOTATION_ABSENT:
        lines.append(ANNOTATION_RESTORED_NOTE.format(account=account, email=annotated))
    state["annotation"] = annotated or ANNOTATION_ABSENT

    errors: dict[str, str] = {}
    if scope_note:
        errors[SCOPE_ERROR_KEY] = scope_note
    held = state.get("roles") or {}
    said_on = state.get("said_on") or {}
    roles: dict[str, list[str]] = {}
    for project, future in futures.items():
        try:
            found = future.result()
        except Exception as exc:  # noqa: BLE001 - one project's failure holds only its own rows
            errors[project] = failure_text(exc)
            for email in emails:
                key = ledger_key(email, project)
                if key in held:
                    roles[key] = held[key]
            continue
        for email in emails:
            key = ledger_key(email, project)
            now_held = sorted(found.get(email, ()))
            before = set(held.get(key) or ())
            added, gone = set(now_held) - before, before - set(now_held)
            via = " and ".join(identities[email])
            if added:
                lines.append(
                    f"{DRIFT_PREFIX} `{email}` ({via}) holds {code_list(added)} in project `{project}`. {DRIFT_ADVICE}"
                )
            if gone:
                lines.append(f"{CLEARED_PREFIX} `{email}` ({via}) no longer holds {code_list(gone)} in project `{project}`.")
            if added:
                said_on[key] = today
            elif now_held and remind and said_on.get(key) != today:
                lines.append(
                    f"{DRIFT_PREFIX} `{email}` ({via}) still holds {code_list(now_held)} in project `{project}`. "
                    f"{DRIFT_ADVICE}"
                )
                said_on[key] = today
            if now_held:
                roles[key] = now_held
    # A recorded drift whose account or project is no longer checked (the
    # annotation was repointed, federation changed, the scope narrowed) is said
    # once as it leaves the ledger, so chat's last word is not a stale grant.
    # While the scope is read only in part, a project missing from it is held
    # instead: it is likely back next tick, and saying it left would re-announce
    # its drift as new when it returns.
    for key, last in sorted(held.items()):
        email, _, project = key.partition(LEDGER_KEY_SEPARATOR)
        if key in roles or (email in emails and project in futures):
            continue
        if email in emails and not scope_complete:
            roles[key] = last
            continue
        lines.append(
            f"{UNWATCHED_PREFIX} `{email}` in project `{project}` is no longer checked; it held "
            f"{code_list(last)} when last read."
        )

    old_errors = state.get("project_errors") or {}
    for project, text in sorted(errors.items()):
        if old_errors.get(project) != text:
            lines.append(f"{PROJECT_UNREAD_PREFIX} `{project}`: `{text}`")
    recovered = sorted(p for p in old_errors if p not in errors and p in futures)
    if recovered:
        lines.append(f"{PROJECT_RECOVERED_PREFIX} {code_list(recovered)}.")
    if SCOPE_ERROR_KEY in old_errors and SCOPE_ERROR_KEY not in errors:
        lines.append(SCOPE_RECOVERED_LINE)

    state["roles"] = roles
    state["said_on"] = {key: day for key, day in said_on.items() if key in roles}
    state["project_errors"] = errors
    state["updated_at"] = now
    if not dry_run:
        save_state(state_path, state)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="check and print; write no ledger")
    parser.add_argument(
        "--state",
        type=Path,
        help=f"ledger path (default: <home>/{PROFILES_DIR}/{PLATFORM_PROFILE}/{CRON_DIR}/{STATE_FILE_NAME}, or ${STATE_PATH_ENV})",
    )
    args = parser.parse_args(argv)
    agent_home = Path(gitops_workspace.agent_home())
    state_path = args.state or Path(
        os.environ.get(STATE_PATH_ENV) or agent_home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / STATE_FILE_NAME
    )
    lines = tick(state_path, dry_run=args.dry_run)
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Tests for sa_role_watch.py: the management cluster and the project policies
are faked at the sandbox hop, the scope at fleet_scope_targets, and the ledger
is a real file."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import command_policy  # noqa: E402
import fleet_scope_targets  # noqa: E402
import sandbox_exec  # noqa: E402
import sa_role_watch  # noqa: E402

NAMESPACE = "kubeagents-system"
MANAGEMENT = "mgmt-proj"
SCOPED = "team-proj"
AGENT_GSA = "kubeagents-platform-gsa@mgmt-proj.iam.gserviceaccount.com"
FEDERATED_GSA = "kubeagents-broker@mgmt-proj.iam.gserviceaccount.com"
KSA = "kubeagents-platform-agent"


def completed(argv, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def platform_agent(*, service_account=KSA, wif=None):
    security = {"serviceAccountName": service_account} if service_account else {}
    if wif is not None:
        security["workloadIdentityFederation"] = wif
    return {"metadata": {"name": "platformagent"}, "spec": {"security": security}}


def binding(role, *emails, condition=None):
    b = {"role": role, "members": [f"serviceAccount:{e}" for e in emails]}
    if condition:
        b["condition"] = {"title": condition, "expression": "true"}
    return b


def read_only_policy(email=AGENT_GSA):
    return {"bindings": [binding(role, email) for role in sa_role_watch.READ_ONLY_ROLES]}


class FakeSandbox:
    """Answers the watch's argvs; anything else is a test bug."""

    def __init__(self, *, agents=None, annotation=AGENT_GSA, policies=None):
        self.agents = [platform_agent()] if agents is None else agents
        self.annotation = annotation
        self.policies = policies if policies is not None else {MANAGEMENT: read_only_policy()}
        self.calls = []

    def __call__(self, argv, *, timeout):
        self.calls.append(list(argv))
        if argv[:3] == ["kubectl", "get", sa_role_watch.PLATFORM_AGENT_RESOURCE]:
            return completed(argv, json.dumps({"items": self.agents}))
        if argv[:3] == ["kubectl", "get", sa_role_watch.SERVICE_ACCOUNT_RESOURCE]:
            annotations = {sa_role_watch.WORKLOAD_IDENTITY_ANNOTATION: self.annotation} if self.annotation else {}
            return completed(argv, json.dumps({"metadata": {"name": argv[3], "annotations": annotations}}))
        if argv[:3] == ["gcloud", "projects", "get-iam-policy"]:
            policy = self.policies.get(argv[3])
            if isinstance(policy, Exception):
                raise policy
            if policy is None:
                return completed(argv, returncode=1, stderr=f"PERMISSION_DENIED on {argv[3]}")
            return completed(argv, json.dumps(policy))
        raise AssertionError(f"unexpected sandbox call: {argv}")


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "sa_role_watch.json"
        env = patch.dict(os.environ, {"GCP_PROJECT_ID": MANAGEMENT, "KUBE_DEFAULT_NAMESPACE": NAMESPACE})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SA_ROLE_WATCH_PROJECT", None)
        self.scope = None
        scope = patch.object(fleet_scope_targets, "declared_scope_targets", side_effect=lambda: self.scope)
        scope.start()
        self.addCleanup(scope.stop)
        # Before the reminder hour unless a test moves it, so a held role stays quiet.
        self.clock = datetime(2026, 10, 20, 9, 25, tzinfo=timezone.utc)
        clock = patch.object(sa_role_watch, "utc_now", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def tick(self, sandbox, *, dry_run=False):
        with patch.object(sa_role_watch, "run_sandbox", sandbox):
            return sa_role_watch.tick(self.state, dry_run=dry_run)

    def scoped(self, *projects, unread=(), management=MANAGEMENT):
        self.scope = fleet_scope_targets.ScopeTargets(
            projects=(management, *projects), unread=tuple(unread), resolved_at="2026-10-13T00:00:00Z",
            path="fleet_scope.json",
        )

    def checked(self, sandbox):
        return [c[3] for c in sandbox.calls if c[:3] == ["gcloud", "projects", "get-iam-policy"]]


class ReadOnlyIsQuiet(WatchTest):
    def test_an_account_holding_only_the_read_only_set_prints_nothing(self):
        self.assertEqual([], self.tick(FakeSandbox()))
        self.assertEqual([], self.tick(FakeSandbox()))

    def test_another_members_admin_role_is_not_this_accounts(self):
        policy = read_only_policy()
        policy["bindings"].append(binding("roles/owner", "someone-else@mgmt-proj.iam.gserviceaccount.com"))
        policy["bindings"].append({"role": "roles/editor", "members": [f"deleted:serviceAccount:{AGENT_GSA}?uid=1"]})
        self.assertEqual([], self.tick(FakeSandbox(policies={MANAGEMENT: policy})))


    def test_a_public_binding_is_held_by_this_account_too(self):
        for principal in ("allUsers", "allAuthenticatedUsers"):
            with self.subTest(principal=principal):
                policy = read_only_policy()
                policy["bindings"].append({"role": "roles/container.admin", "members": [principal]})
                lines = self.tick(FakeSandbox(policies={MANAGEMENT: policy}))
                self.assertEqual(1, len(lines), lines)
                self.assertIn("roles/container.admin (public)", lines[0])
                self.state.unlink(missing_ok=True)

class DriftIsReportedOnChange(WatchTest):
    def admin_policy(self, *roles):
        policy = read_only_policy()
        policy["bindings"] += [binding(role, AGENT_GSA) for role in roles]
        return policy

    def test_a_role_beyond_the_set_names_the_role_and_the_project(self):
        lines = self.tick(FakeSandbox(policies={MANAGEMENT: self.admin_policy("roles/container.admin")}))
        self.assertEqual(1, len(lines), lines)
        self.assertIn(sa_role_watch.DRIFT_PREFIX, lines[0])
        self.assertIn("`roles/container.admin`", lines[0])
        self.assertIn(f"project `{MANAGEMENT}`", lines[0])
        self.assertIn(AGENT_GSA, lines[0])
        self.assertIn("agent pod and credential broker", lines[0])

    def test_the_same_drift_is_said_once(self):
        sandbox = FakeSandbox(policies={MANAGEMENT: self.admin_policy("roles/container.admin")})
        self.assertEqual(1, len(self.tick(sandbox)))
        self.assertEqual([], self.tick(sandbox))

    def test_a_second_role_is_said_alone_and_a_removal_is_said(self):
        self.tick(FakeSandbox(policies={MANAGEMENT: self.admin_policy("roles/container.admin")}))
        lines = self.tick(FakeSandbox(policies={MANAGEMENT: self.admin_policy("roles/container.admin", "roles/owner")}))
        self.assertEqual(1, len(lines), lines)
        self.assertIn("`roles/owner`", lines[0])
        self.assertNotIn("container.admin", lines[0])
        lines = self.tick(FakeSandbox(policies={MANAGEMENT: self.admin_policy("roles/owner")}))
        self.assertEqual(1, len(lines), lines)
        self.assertIn(sa_role_watch.CLEARED_PREFIX, lines[0])
        self.assertIn("no longer holds `roles/container.admin`", lines[0])

    def test_a_custom_role_and_a_conditional_grant_are_reported(self):
        policy = read_only_policy()
        policy["bindings"].append(binding("projects/mgmt-proj/roles/clusterMaker", AGENT_GSA))
        policy["bindings"].append(binding("roles/container.clusterAdmin", AGENT_GSA, condition="weekdays"))
        lines = self.tick(FakeSandbox(policies={MANAGEMENT: policy}))
        self.assertIn("`projects/mgmt-proj/roles/clusterMaker`", lines[0])
        self.assertIn("`roles/container.clusterAdmin (conditional)`", lines[0])
        # The condition's title is free text a policy author writes; it stays out of chat.
        self.assertNotIn("weekdays", lines[0])

    def test_member_case_does_not_hide_a_grant(self):
        policy = read_only_policy()
        policy["bindings"].append({"role": "roles/editor", "members": [f"serviceAccount:{AGENT_GSA.upper()}"]})
        lines = self.tick(FakeSandbox(policies={MANAGEMENT: policy}))
        self.assertIn("`roles/editor`", lines[0])

    def test_every_scoped_project_is_checked(self):
        self.scoped(SCOPED)
        sandbox = FakeSandbox(policies={MANAGEMENT: read_only_policy(), SCOPED: self.admin_policy("roles/compute.admin")})
        lines = self.tick(sandbox)
        self.assertEqual(1, len(lines), lines)
        self.assertIn(f"project `{SCOPED}`", lines[0])
        checked = {c[3] for c in sandbox.calls if c[:3] == ["gcloud", "projects", "get-iam-policy"]}
        self.assertEqual({MANAGEMENT, SCOPED}, checked)

    def test_a_scoped_project_the_reconcile_could_not_read_is_still_checked(self):
        self.scoped(unread=[(SCOPED, "denied"), (fleet_scope_targets.UNRESOLVED_SCOPE_ROW, "unresolved")])
        sandbox = FakeSandbox(policies={MANAGEMENT: read_only_policy(), SCOPED: self.admin_policy("roles/owner")})
        lines = self.tick(sandbox)
        self.assertEqual(2, len(lines), lines)
        self.assertIn(f"project `{SCOPED}`", lines[0])
        # The unresolved row is the scope saying it is partial; that is said too.
        self.assertIn(sa_role_watch.UNRESOLVED_SCOPE_NOTE, lines[1])
        self.assertEqual({MANAGEMENT, SCOPED}, set(self.checked(sandbox)))

    def test_projects_past_max_projects_are_named_not_read(self):
        over = [f"over-{i}" for i in range(sa_role_watch.MAX_PROJECTS_IN_NOTE + 2)]
        self.scoped(unread=[(p, fleet_scope_targets.OUTCOME_OVER_CAP) for p in over])
        sandbox = FakeSandbox()
        lines = self.tick(sandbox)
        self.assertEqual([MANAGEMENT], self.checked(sandbox))
        self.assertEqual(1, len(lines), lines)
        self.assertIn(f"{len(over)} project(s) past the scope's maxProjects", lines[0])
        self.assertIn("and 2 more", lines[0])
        # The note sits inside the failure line's code span, so it carries no backticks of its own.
        self.assertEqual(0, lines[0].count("`") % 2, lines[0])
        self.assertRegex(lines[0], r"`[^`]*over-0[^`]*and 2 more`")
        self.assertEqual([], self.tick(sandbox))

    def test_a_numeric_management_project_is_read_once(self):
        with patch.dict(os.environ, {"GCP_PROJECT_ID": "123456789"}):
            self.scoped(SCOPED)
            sandbox = FakeSandbox(policies={MANAGEMENT: self.admin_policy("roles/owner"), SCOPED: read_only_policy()})
            lines = self.tick(sandbox)
        self.assertEqual([MANAGEMENT, SCOPED], self.checked(sandbox))
        self.assertEqual(1, len(lines), lines)

    def test_a_project_leaving_the_scope_says_its_last_drift_once(self):
        self.scoped(SCOPED)
        self.tick(FakeSandbox(policies={MANAGEMENT: read_only_policy(), SCOPED: self.admin_policy("roles/owner")}))
        self.scoped()
        lines = self.tick(FakeSandbox())
        self.assertEqual(1, len(lines), lines)
        self.assertIn(f"in project `{SCOPED}` is no longer checked", lines[0])
        self.assertIn("`roles/owner`", lines[0])
        self.assertEqual([], self.tick(FakeSandbox()))

    def test_a_partial_scope_holds_rows_instead_of_dropping_them(self):
        self.scoped(SCOPED)
        self.tick(FakeSandbox(policies={MANAGEMENT: read_only_policy(), SCOPED: self.admin_policy("roles/owner")}))

        def unreadable():
            raise fleet_scope_targets.ScopeRenderUnreadable("KUBEAGENTS_SCOPE_FILE=/x cannot be read")

        with patch.object(fleet_scope_targets, "declared_scope_targets", side_effect=unreadable):
            lines = self.tick(FakeSandbox())
        self.assertEqual(1, len(lines), lines)
        self.assertIn("`spec.scope`", lines[0])
        # Back in full: the held drift is not re-announced, and the scope's return is said once.
        lines = self.tick(FakeSandbox(policies={MANAGEMENT: read_only_policy(), SCOPED: self.admin_policy("roles/owner")}))
        self.assertEqual([sa_role_watch.SCOPE_RECOVERED_LINE], lines)

    def test_losing_the_sandbox_mid_tick_is_one_line(self):
        self.scoped(*[f"p{i}" for i in range(6)])
        gone = sandbox_exec.SandboxUnavailable("could not reach the shell sandbox: Connection refused")
        sandbox = FakeSandbox(policies={p: gone for p in [MANAGEMENT, *[f"p{i}" for i in range(6)]]})
        lines = self.tick(sandbox)
        self.assertEqual(1, len(lines), lines)
        self.assertIn(sa_role_watch.CHECK_FAILED_PREFIX, lines[0])

    def test_error_text_cannot_leave_its_code_span(self):
        sandbox = FakeSandbox(policies={})
        real = sandbox.__call__

        def noisy(argv, *, timeout):
            r = real(argv, timeout=timeout)
            if argv[:3] == ["gcloud", "projects", "get-iam-policy"]:
                r.stderr = "denied `\n` **now**\nsecond line"
            return r

        lines = self.tick(noisy)
        self.assertNotIn("`\\n`", lines[0])
        self.assertEqual(1, lines[0].count("\n") + 1)
        # The project's span and the error text's span: `**now**` stays inside one.
        self.assertEqual(4, lines[0].count("`"))
        self.assertRegex(lines[0], r"`[^`]*\*\*now\*\*[^`]*`")

    def test_dry_run_writes_no_ledger(self):
        self.tick(FakeSandbox(policies={MANAGEMENT: self.admin_policy("roles/owner")}), dry_run=True)
        self.assertFalse(self.state.exists())


class HeldDriftIsRepeatedDaily(WatchTest):
    def admin_sandbox(self):
        policy = read_only_policy()
        policy["bindings"].append(binding("roles/container.admin", AGENT_GSA))
        return FakeSandbox(policies={MANAGEMENT: policy})

    def at(self, day, hour):
        self.clock = datetime(2026, 10, day, hour, 25, tzinfo=timezone.utc)

    def test_a_held_role_is_said_again_once_a_day_from_the_reminder_hour(self):
        sandbox = self.admin_sandbox()
        self.at(20, 9)
        self.assertEqual(1, len(self.tick(sandbox)))
        self.at(20, 11)
        self.assertEqual([], self.tick(sandbox))
        self.at(20, 12)
        self.assertEqual([], self.tick(sandbox), "said at 09:25 today, so not repeated today")
        self.at(21, 11)
        self.assertEqual([], self.tick(sandbox))
        self.at(21, 12)
        lines = self.tick(sandbox)
        self.assertEqual(1, len(lines), lines)
        self.assertIn(sa_role_watch.DRIFT_PREFIX, lines[0])
        self.assertIn("still holds `roles/container.admin`", lines[0])
        self.assertIn(f"project `{MANAGEMENT}`", lines[0])
        self.at(21, 13)
        self.assertEqual([], self.tick(sandbox))
        self.at(22, 14)
        self.assertEqual(1, len(self.tick(sandbox)), "a missed reminder tick is made up later that day")

    def test_a_ledger_without_reminder_dates_reminds_at_the_hour(self):
        sandbox = self.admin_sandbox()
        self.tick(sandbox)
        state = json.loads(self.state.read_text())
        del state["said_on"]
        self.state.write_text(json.dumps(state))
        self.at(20, 12)
        self.assertEqual(1, len(self.tick(sandbox)))

    def test_a_cleared_role_is_not_repeated(self):
        self.tick(self.admin_sandbox())
        self.tick(FakeSandbox())
        self.at(21, 12)
        self.assertEqual([], self.tick(FakeSandbox()))
        self.assertEqual({}, json.loads(self.state.read_text())["said_on"])


class FailuresHoldTheirRows(WatchTest):
    def test_an_unreadable_project_keeps_its_drift_and_says_so_once(self):
        admin = read_only_policy()
        admin["bindings"].append(binding("roles/owner", AGENT_GSA))
        self.tick(FakeSandbox(policies={MANAGEMENT: admin}))
        lines = self.tick(FakeSandbox(policies={}))
        self.assertEqual(1, len(lines), lines)
        self.assertIn(sa_role_watch.PROJECT_UNREAD_PREFIX, lines[0])
        self.assertIn("PERMISSION_DENIED", lines[0])
        self.assertEqual([], self.tick(FakeSandbox(policies={})))
        # Back, with the role still there: recovery is said, the role is not repeated.
        lines = self.tick(FakeSandbox(policies={MANAGEMENT: admin}))
        self.assertEqual(1, len(lines), lines)
        self.assertIn(sa_role_watch.PROJECT_RECOVERED_PREFIX, lines[0])

    def test_a_timeout_is_a_project_failure_not_a_crash(self):
        sandbox = FakeSandbox(policies={MANAGEMENT: subprocess.TimeoutExpired(["gcloud"], 60)})
        lines = self.tick(sandbox)
        self.assertIn("timed out after 60s", lines[0])

    def test_a_cluster_read_failure_is_said_once_and_recovers(self):
        broken = FakeSandbox(agents=[])
        lines = self.tick(broken)
        self.assertEqual(1, len(lines), lines)
        self.assertIn(sa_role_watch.CHECK_FAILED_PREFIX, lines[0])
        self.assertIn(f"no PlatformAgent in namespace {NAMESPACE}", lines[0])
        self.assertEqual([], self.tick(broken))
        self.assertEqual([sa_role_watch.CHECK_RECOVERED_LINE], self.tick(FakeSandbox()))

    def test_an_unreadable_scope_still_checks_the_management_project(self):
        def unreadable():
            raise fleet_scope_targets.ScopeRenderUnreadable("KUBEAGENTS_SCOPE_FILE=/x cannot be read")

        with patch.object(fleet_scope_targets, "declared_scope_targets", side_effect=unreadable):
            sandbox = FakeSandbox()
            lines = self.tick(sandbox)
        self.assertEqual(1, len(lines), lines)
        self.assertIn("`spec.scope`", lines[0])
        self.assertTrue(any(c[:3] == ["gcloud", "projects", "get-iam-policy"] for c in sandbox.calls))


class Identities(WatchTest):
    def test_without_federation_one_account_is_both_pods(self):
        identities, account, annotated = self.read(FakeSandbox())
        self.assertEqual({AGENT_GSA: [sa_role_watch.AGENT_POD_SOURCE, sa_role_watch.BROKER_SOURCE]}, identities)
        self.assertEqual(f"{NAMESPACE}/{KSA}", account)

    def test_federation_adds_the_brokers_own_account(self):
        wif = {"audience": "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/q",
               "serviceAccountEmail": FEDERATED_GSA}
        sandbox = FakeSandbox(agents=[platform_agent(wif=wif)])
        identities, _, _ = self.read(sandbox)
        self.assertEqual({AGENT_GSA: [sa_role_watch.AGENT_POD_SOURCE], FEDERATED_GSA: [sa_role_watch.BROKER_SOURCE]}, identities)

    def test_a_half_filled_federation_block_is_not_armed(self):
        sandbox = FakeSandbox(agents=[platform_agent(wif={"serviceAccountEmail": FEDERATED_GSA})])
        identities, _, _ = self.read(sandbox)
        self.assertEqual([AGENT_GSA], list(identities))

    def test_the_cr_name_is_the_default_service_account(self):
        sandbox = FakeSandbox(agents=[platform_agent(service_account="")])
        _, account, _ = self.read(sandbox)
        self.assertEqual(f"{NAMESPACE}/platformagent", account)

    def test_a_federated_drift_names_the_broker(self):
        wif = {"audience": "//iam.googleapis.com/projects/1/x", "serviceAccountEmail": FEDERATED_GSA}
        policy = read_only_policy()
        policy["bindings"].append(binding("roles/container.admin", FEDERATED_GSA))
        lines = self.tick(FakeSandbox(agents=[platform_agent(wif=wif)], annotation=None, policies={MANAGEMENT: policy}))
        drift = [line for line in lines if line.startswith(sa_role_watch.DRIFT_PREFIX)]
        self.assertEqual(1, len(drift), lines)
        self.assertIn(f"`{FEDERATED_GSA}` (credential broker)", drift[0])

    def test_a_repointed_annotation_says_the_old_accounts_drift_once(self):
        policy = read_only_policy()
        policy["bindings"].append(binding("roles/owner", AGENT_GSA))
        self.tick(FakeSandbox(policies={MANAGEMENT: policy}))
        other = "other-gsa@mgmt-proj.iam.gserviceaccount.com"
        lines = self.tick(FakeSandbox(annotation=other, policies={MANAGEMENT: policy}))
        self.assertEqual(1, len(lines), lines)
        self.assertIn(f"`{AGENT_GSA}` in project `{MANAGEMENT}` is no longer checked", lines[0])

    def test_an_absent_annotation_is_said_once_and_its_return_is_said(self):
        wif = {"audience": "//iam.googleapis.com/projects/1/x", "serviceAccountEmail": FEDERATED_GSA}
        absent = FakeSandbox(agents=[platform_agent(wif=wif)], annotation=None,
                             policies={MANAGEMENT: read_only_policy(FEDERATED_GSA)})
        lines = self.tick(absent)
        self.assertEqual([sa_role_watch.NO_ANNOTATION_NOTE.format(account=f"{NAMESPACE}/{KSA}")], lines)
        self.assertEqual([], self.tick(absent))
        back = FakeSandbox(agents=[platform_agent(wif=wif)], policies={MANAGEMENT: read_only_policy(FEDERATED_GSA)})
        lines = self.tick(back)
        self.assertIn(sa_role_watch.ANNOTATION_RESTORED_NOTE.format(account=f"{NAMESPACE}/{KSA}", email=AGENT_GSA), lines)

    def test_an_annotation_that_is_not_an_email_is_refused_not_quoted(self):
        lines = self.tick(FakeSandbox(annotation="x`\n**pwned**"))
        self.assertEqual(1, len(lines), lines)
        self.assertIn("is not a service account email", lines[0])
        self.assertNotIn("pwned", lines[0])

    def test_no_account_anywhere_fails_the_check(self):
        lines = self.tick(FakeSandbox(annotation=None))
        self.assertEqual(1, len(lines), lines)
        self.assertIn("names a Google service account", lines[0])

    def read(self, sandbox):
        with patch.object(sa_role_watch, "run_sandbox", sandbox):
            return sa_role_watch.read_identities(NAMESPACE)


class TheBrokerAllowsWhatTheWatchRuns(unittest.TestCase):
    """Every argv the watch sends, put through the broker's own policy: a
    refused verb would turn every tick into a "check failed" line."""

    def test_each_argv_is_allowed(self):
        argvs = [
            ["kubectl", "get", sa_role_watch.PLATFORM_AGENT_RESOURCE, "-n", NAMESPACE, "-o", "json"],
            ["kubectl", "get", sa_role_watch.SERVICE_ACCOUNT_RESOURCE, KSA, "-n", NAMESPACE, "-o", "json"],
            ["gcloud", "projects", "get-iam-policy", MANAGEMENT, "--format=json"],
            list(sa_role_watch.CONFIG_PROJECT_ARGV),
        ]
        for argv in argvs:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertTrue(decision.allowed, f"{argv}: {decision.rule_id} {decision.message}")

    def test_the_policy_write_beside_the_read_stays_refused(self):
        decision = command_policy.evaluate(["gcloud", "projects", "set-iam-policy", MANAGEMENT, "policy.json"])
        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()

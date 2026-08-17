from __future__ import annotations

import json
import os
import unittest
from threading import Event
from unittest.mock import patch

from admin_console.connections import (
    CheckStatus,
    CommandResult,
    ConnectionChecksCancelled,
    connection_is_ready,
    project_connection_is_ready,
    run_connection_checks,
)
from admin_console.project_config import DeploymentTarget


class SuccessfulRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        self.calls.append(arguments)
        if arguments[:2] == ["services", "list"]:
            return CommandResult(
                0,
                "\n".join(
                    (
                        "container.googleapis.com",
                        "logging.googleapis.com",
                        "cloudtrace.googleapis.com",
                    )
                ),
            )
        if arguments[:3] == ["container", "clusters", "list"]:
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "name": "test-cluster-01",
                            "location": "us-east4",
                            "status": "RUNNING",
                            "resourceLabels": {"kube-agents-host": "true"},
                        }
                    ]
                ),
            )
        if arguments[:2] == ["logging", "read"]:
            return CommandResult(0, '[{"insertId": "one"}]')
        return CommandResult(0, "ok")


class ProjectDeniedRunner:
    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        if arguments[:2] == ["projects", "describe"]:
            return CommandResult(1, stderr="403 permission denied")
        return CommandResult(0, "ok")


class UnlabeledRunner(SuccessfulRunner):
    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        if arguments[:3] == ["container", "clusters", "list"]:
            self.calls.append(arguments)
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "name": "unlabeled-cluster",
                            "location": "us-east4",
                            "status": "RUNNING",
                        }
                    ]
                ),
            )
        return super().run(arguments, timeout=timeout)


class NoClustersRunner(SuccessfulRunner):
    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        if arguments[:3] == ["container", "clusters", "list"]:
            self.calls.append(arguments)
            return CommandResult(0, "[]")
        return super().run(arguments, timeout=timeout)


class AuditTimeoutRunner(SuccessfulRunner):
    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        if arguments[:2] == ["logging", "read"] and "audit_event" in arguments[2]:
            self.calls.append(arguments)
            return CommandResult(124, timed_out=True)
        return super().run(arguments, timeout=timeout)


class ConnectionDiagnosticsTest(unittest.TestCase):
    def test_cancelled_probe_stops_after_current_bounded_command(self):
        cancelled = Event()

        class CancellingRunner:
            calls = 0

            def run(self, arguments, *, timeout=15):
                self.calls += 1
                cancelled.set()
                return CommandResult(0, "ok")

        runner = CancellingRunner()
        with self.assertRaises(ConnectionChecksCancelled):
            run_connection_checks(
                "test-project-01",
                runner=runner,
                cancel_event=cancelled,
            )

        self.assertEqual(runner.calls, 1)

    @patch.dict(os.environ, {"KUBE_AGENTS_ADMIN_USER": "user@example.com"})
    def test_successful_read_checks_without_trace_network_probe(self):
        runner = SuccessfulRunner()
        report = run_connection_checks(
            "test-project-01",
            expected_target=DeploymentTarget(
                "test-project-01",
                "test-cluster-01",
                "us-east4",
            ),
            runner=runner,
            include_trace_probe=False,
        )

        statuses = {check.key: check.status for check in report.checks}
        self.assertEqual(statuses["cli_auth"], CheckStatus.PASS)
        self.assertEqual(statuses["project"], CheckStatus.PASS)
        self.assertEqual(statuses["gke"], CheckStatus.PASS)
        self.assertEqual(statuses["logging"], CheckStatus.PASS)
        self.assertEqual(statuses["audit"], CheckStatus.PASS)
        self.assertEqual(report.clusters[0].name, "test-cluster-01")
        self.assertTrue(report.clusters[0].is_kube_agents_host)
        self.assertEqual(report.kube_agents_hosts, report.clusters)
        audit_filters = [
            call[2]
            for call in runner.calls
            if call[:2] == ["logging", "read"] and "audit_event" in call[2]
        ]
        self.assertEqual(len(audit_filters), 1)
        self.assertIn('jsonPayload.log:"audit_event"', audit_filters[0])
        self.assertIn("kubeagents-system", audit_filters[0])

    def test_project_permission_failure_skips_source_queries(self):
        report = run_connection_checks(
            "test-project-01",
            runner=ProjectDeniedRunner(),
            include_trace_probe=False,
        )

        checks = {check.key: check for check in report.checks}
        self.assertEqual(checks["project"].status, CheckStatus.FAIL)
        self.assertIn("minimum read permissions", checks["project"].guidance)
        self.assertEqual(checks["logging"].status, CheckStatus.SKIPPED)
        self.assertEqual(checks["trace"].status, CheckStatus.SKIPPED)
        self.assertFalse(project_connection_is_ready(report))

    def test_project_connection_allows_readable_gke_without_clusters(self):
        report = run_connection_checks(
            "test-project-01",
            runner=NoClustersRunner(),
            include_trace_probe=False,
        )

        self.assertEqual(report.clusters, ())
        self.assertTrue(project_connection_is_ready(report))

    def test_project_connection_can_skip_slower_telemetry_probes(self):
        runner = SuccessfulRunner()

        report = run_connection_checks(
            "test-project-01",
            runner=runner,
            include_telemetry_probes=False,
        )

        self.assertTrue(project_connection_is_ready(report))
        self.assertFalse(
            any(call[:2] == ["logging", "read"] for call in runner.calls)
        )
        self.assertFalse(any(check.key == "trace" for check in report.checks))

    @patch("admin_console.agent_runtime.AgentRuntimeProvider")
    def test_unlabeled_single_cluster_requires_manual_selection(self, provider_type):
        report = run_connection_checks(
            "test-project-01",
            runner=UnlabeledRunner(),
            include_trace_probe=False,
            include_agent_runtime_probe=True,
        )

        checks = {check.key: check for check in report.checks}
        self.assertEqual(checks["host_discovery"].status, CheckStatus.WARNING)
        self.assertEqual(checks["agent_runtime"].status, CheckStatus.SKIPPED)
        self.assertEqual(report.kube_agents_hosts, ())
        provider_type.assert_not_called()

    @patch("admin_console.agent_runtime.AgentRuntimeProvider")
    def test_agent_runtime_probe_uses_the_selected_cluster(self, provider_type):
        provider = provider_type.return_value
        provider.list_agents.return_value = ("test-agent-01",)
        provider.check_connection.return_value = (2, 11)

        report = run_connection_checks(
            "test-project-01",
            expected_target=DeploymentTarget(
                "test-project-01",
                "test-cluster-01",
                "us-east4",
            ),
            runner=SuccessfulRunner(),
            include_trace_probe=False,
            include_agent_runtime_probe=True,
        )

        check = next(item for item in report.checks if item.key == "agent_runtime")
        self.assertEqual(check.status, CheckStatus.PASS)
        self.assertIn("2 profile(s) and 11 session(s)", check.summary)
        provider.check_connection.assert_called_once_with("test-agent-01")
        self.assertTrue(connection_is_ready(report))

    @patch("admin_console.agent_runtime.AgentRuntimeProvider")
    def test_telemetry_failure_does_not_lock_runtime_pages(self, provider_type):
        provider = provider_type.return_value
        provider.list_agents.return_value = ("test-agent-01",)
        provider.check_connection.return_value = (2, 11)

        report = run_connection_checks(
            "test-project-01",
            expected_target=DeploymentTarget(
                "test-project-01",
                "test-cluster-01",
                "us-east4",
            ),
            runner=AuditTimeoutRunner(),
            include_trace_probe=False,
            include_agent_runtime_probe=True,
        )

        self.assertEqual(
            next(check for check in report.checks if check.key == "audit").status,
            CheckStatus.FAIL,
        )
        self.assertTrue(connection_is_ready(report))


if __name__ == "__main__":
    unittest.main()

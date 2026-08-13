from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from admin_console.connections import CommandResult
from admin_console.domain import AttributionLevel, TriggerKind
from admin_console.telemetry import (
    CloudTelemetryProvider,
    normalize_logging_row,
    normalize_trace,
    redact_evidence,
)


class TokenRunner:
    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        if arguments == ["auth", "application-default", "print-access-token"]:
            return CommandResult(0, "test-token\n")
        return CommandResult(1, stderr="unexpected command")


class JsonResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def trace_payload(trace_id: str) -> dict:
    return {
        "traceId": trace_id,
        "spans": [
            {
                "spanId": f"span-{trace_id}",
                "name": "agent",
                "startTime": "2026-08-01T10:00:00Z",
                "endTime": "2026-08-01T10:00:01Z",
                "labels": {"session.id": f"session-{trace_id}"},
            }
        ],
    }


class TelemetryNormalizationTest(unittest.TestCase):
    def test_trace_progressively_reads_requested_pages(self):
        provider = CloudTelemetryProvider(
            "demo-project",
            trace_limit=1,
            trace_pages=2,
            runner=TokenRunner(),
        )
        responses = [
            JsonResponse(
                {"traces": [trace_payload("one")], "nextPageToken": "page-two"}
            ),
            JsonResponse({"traces": [trace_payload("two")]}),
        ]
        start = datetime(2026, 8, 1, tzinfo=UTC)
        end = datetime(2026, 8, 2, tzinfo=UTC)

        with patch(
            "admin_console.telemetry.urllib.request.urlopen",
            side_effect=responses,
        ) as urlopen:
            events, state = provider._load_trace(start, end, {})

        self.assertEqual(len(events), 2)
        self.assertEqual(state.records_read, 2)
        self.assertEqual(state.pages_read, 2)
        self.assertFalse(state.truncated)
        self.assertIn("across 2 page(s)", state.detail)
        first_query = parse_qs(urlparse(urlopen.call_args_list[0].args[0].full_url).query)
        second_query = parse_qs(urlparse(urlopen.call_args_list[1].args[0].full_url).query)
        self.assertNotIn("pageToken", first_query)
        self.assertEqual(second_query["pageToken"], ["page-two"])

    def test_trace_reports_more_results_after_page_budget(self):
        provider = CloudTelemetryProvider(
            "demo-project",
            trace_limit=1,
            trace_pages=1,
            runner=TokenRunner(),
        )
        response = JsonResponse(
            {"traces": [trace_payload("one")], "nextPageToken": "more"}
        )

        with patch(
            "admin_console.telemetry.urllib.request.urlopen",
            return_value=response,
        ):
            _, state = provider._load_trace(
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 8, 2, tzinfo=UTC),
                {},
            )

        self.assertTrue(state.truncated)
        self.assertEqual(state.records_read, 1)

    def test_trace_page_budget_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "trace pages"):
            CloudTelemetryProvider("demo-project", trace_pages=11)

    def test_normalizes_wrapped_cron_tool_audit(self):
        row = {
            "insertId": "log-one",
            "timestamp": "2026-07-28T19:07:04Z",
            "logName": "projects/demo/logs/stdout",
            "resource": {
                "labels": {
                    "cluster_name": "test-cluster-01",
                    "namespace_name": "kubeagents-system",
                    "container_name": "fluent-bit",
                }
            },
            "jsonPayload": {
                "log": (
                    "2026-07-28 19:07:04 INFO "
                    "[cron_capacity_20260728_190038] audit: "
                    '{"audit_event":"tool_call_end","duration_ms":711,'
                    '"result":"{\\"exit_code\\": 0}","task_id":"task-1",'
                    '"tool_name":"terminal"}'
                )
            },
        }

        event = normalize_logging_row(row, "demo-project")

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.trigger_kind, TriggerKind.CRON)
        self.assertEqual(event.attribution, AttributionLevel.INHERITED)
        self.assertEqual(event.interaction_id, "task-1")
        self.assertEqual(event.status, "completed")
        self.assertEqual(event.details["source"], "cloud_logging")

    def test_normalizes_structured_user_audit_without_message_content(self):
        row = {
            "insertId": "audit-one",
            "timestamp": "2026-07-30T20:34:42Z",
            "logName": "projects/demo/logs/stdout",
            "resource": {"labels": {}},
            "jsonPayload": {
                "audit_event": "user_request_start",
                "session_id": "web_abc",
                "message_sha256": "f" * 64,
                "message_length": 42,
                "platform": "web",
                "user_id": "auditor@example.com",
                "cluster": "agent-host",
                "status": "running",
            },
        }

        event = normalize_logging_row(row, "demo-project")

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.trigger_kind, TriggerKind.HUMAN)
        self.assertEqual(event.user_id, "auditor@example.com")
        self.assertTrue(event.interaction_id.startswith("message:web_abc:"))
        self.assertNotIn("message", event.details)

    def test_trace_uses_trace_as_interaction_and_inherits_session_user(self):
        trace = {
            "traceId": "trace-123",
            "spans": [
                {
                    "spanId": "root",
                    "name": "agent",
                    "startTime": "2026-07-30T20:34:43Z",
                    "endTime": "2026-07-30T20:34:45Z",
                    "labels": {
                        "session.id": "web_abc",
                        "correlation.id": "web_abc",
                        "hermes.turn.final_status": "completed",
                        "kubeagents.agent_name": "ux-e2e",
                        "k8s.cluster.name": "agent-host",
                    },
                },
                {
                    "spanId": "tool",
                    "parentSpanId": "root",
                    "name": "tool.terminal",
                    "startTime": "2026-07-30T20:34:43.1Z",
                    "endTime": "2026-07-30T20:34:43.2Z",
                    "labels": {
                        "session.id": "web_abc",
                        "tool.name": "terminal",
                        "hermes.tool.outcome": "success",
                        "gen_ai.tool.call.arguments": (
                            '{"api_key":"should-not-render","command":"kubectl get pods"}'
                        ),
                    },
                },
            ],
        }

        events = normalize_trace(
            trace, "demo-project", {"web_abc": "auditor@example.com"}
        )

        self.assertEqual(len(events), 2)
        self.assertTrue(all(event.interaction_id == "trace-123" for event in events))
        self.assertTrue(all(event.trigger_kind == TriggerKind.HUMAN for event in events))
        self.assertTrue(
            all(event.attribution == AttributionLevel.INHERITED for event in events)
        )
        self.assertIn("[REDACTED]", events[1].details["tool_arguments"])
        self.assertNotIn("should-not-render", events[1].details["tool_arguments"])

    def test_redaction_caps_and_masks_evidence(self):
        evidence = "Authorization: Bearer abc123 " + ("x" * 9_000)
        rendered = redact_evidence(evidence)

        self.assertNotIn("abc123", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertIn("[truncated by portal]", rendered)

    def test_redaction_masks_all_supported_secret_key_names(self):
        secrets = {
            "token": "token-value",
            "id_token": "id-token-value",
            "refresh_token": "refresh-value",
            "private_key": "private-key-value",
            "credential": "credential-value",
            "ssh_key": "ssh-key-value",
            "api_key": "api-key-control",
        }

        rendered = redact_evidence(secrets)

        self.assertEqual(rendered.count("[REDACTED]"), len(secrets))
        for value in secrets.values():
            self.assertNotIn(value, rendered)

    def test_malformed_logging_duration_falls_back_per_row(self):
        base = {
            "insertId": "duration-row",
            "jsonPayload": {
                "audit_event": "tool_call_end",
                "duration_ms": "12.5",
            },
        }

        decimal = normalize_logging_row(base, "demo-project")
        invalid = normalize_logging_row(
            {
                **base,
                "jsonPayload": {
                    "audit_event": "tool_call_end",
                    "duration_ms": {"unexpected": "shape"},
                },
            },
            "demo-project",
        )

        self.assertEqual(decimal.duration_ms, 12)
        self.assertEqual(invalid.duration_ms, 0)

    def test_invalid_time_falls_back_without_crashing(self):
        trace = {
            "traceId": "trace-time",
            "spans": [
                {
                    "spanId": "span",
                    "name": "agent",
                    "startTime": "invalid",
                    "labels": {"session.id": "session"},
                }
            ],
        }
        before = datetime.now(UTC)
        events = normalize_trace(trace, "demo-project")
        self.assertGreaterEqual(events[0].occurred_at, before)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import time
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from admin_console.connections import CommandResult
from admin_console.domain import AttributionLevel, TriggerKind
from admin_console.telemetry import (
    LOGGING_TIMEOUT_SECONDS,
    CloudTelemetryProvider,
    _PageCursor,
    normalize_logging_row,
    normalize_trace,
    redact_evidence,
)


class TokenRunner:
    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        if arguments == ["auth", "application-default", "print-access-token"]:
            return CommandResult(0, "test-token\n")
        return CommandResult(1, stderr="unexpected command")


class AllTokenRunner(TokenRunner):
    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        if arguments == ["auth", "print-access-token"]:
            return CommandResult(0, "logging-token\n")
        return super().run(arguments, timeout=timeout)


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


def logging_payload(insert_id: str) -> dict:
    return {
        "insertId": insert_id,
        "timestamp": "2026-08-01T10:00:00Z",
        "resource": {"labels": {}},
        "jsonPayload": {
            "audit_event": "tool_call_end",
            "tool_name": "terminal",
        },
    }


class TelemetryNormalizationTest(unittest.TestCase):
    def test_logging_continues_from_server_cursors_without_rereading(self):
        provider = CloudTelemetryProvider(
            "demo-project",
            log_limit=1,
            log_pages=1,
            runner=AllTokenRunner(),
        )
        provider._start = datetime(2026, 8, 1, tzinfo=UTC)
        provider._end = datetime(2026, 8, 2, tzinfo=UTC)
        requests = []

        def read(request, *, timeout):
            body = json.loads(request.data)
            requests.append((request, body, timeout))
            direct = "jsonPayload.audit_event" in body["filter"]
            token = body.get("pageToken", "")
            if not token:
                return JsonResponse(
                    {
                        "entries": [logging_payload("shared")],
                        "nextPageToken": "direct-next" if direct else "wrapped-next",
                    }
                )
            return JsonResponse(
                {
                    "entries": [
                        logging_payload("direct-two" if direct else "wrapped-two")
                    ]
                }
            )

        with patch(
            "admin_console.telemetry.urllib.request.urlopen",
            side_effect=read,
        ):
            provider._advance_logging(1, time.monotonic() + 90)
            first_state = provider._logging_source_state()
            provider._advance_logging(1, time.monotonic() + 90)

        state = provider._logging_source_state()
        self.assertEqual(first_state.records_read, 1)
        self.assertTrue(first_state.truncated)
        self.assertEqual(state.records_read, 3)
        self.assertEqual(state.pages_read, 4)
        self.assertFalse(state.truncated)
        self.assertTrue(all(request.method == "POST" for request, _, _ in requests))
        self.assertTrue(
            all(request.full_url.endswith("/entries:list") for request, _, _ in requests)
        )
        self.assertTrue(
            all(timeout <= LOGGING_TIMEOUT_SECONDS for _, _, timeout in requests)
        )
        self.assertTrue(all(timeout > 59 for _, _, timeout in requests))
        continuation_bodies = [body for _, body, _ in requests if "pageToken" in body]
        self.assertEqual(
            {body["pageToken"] for body in continuation_bodies},
            {"direct-next", "wrapped-next"},
        )
        self.assertTrue(
            all("pageToken" not in request.full_url for request, _, _ in requests)
        )

    def test_logging_timeout_retains_successful_pages(self):
        provider = CloudTelemetryProvider(
            "demo-project",
            log_limit=1,
            runner=AllTokenRunner(),
        )
        cursor = _PageCursor()
        responses = [
            JsonResponse(
                {
                    "entries": [logging_payload("first")],
                    "nextPageToken": "next",
                }
            ),
            TimeoutError(),
        ]

        with patch(
            "admin_console.telemetry.urllib.request.urlopen",
            side_effect=responses,
        ):
            rows = provider._advance_logging_query(
                "resource.type=\"k8s_container\"",
                cursor,
                "token",
                2,
                time.monotonic() + 90,
            )

        self.assertEqual([row["insertId"] for row in rows], ["first"])
        self.assertEqual(cursor.pages_read, 1)
        self.assertEqual(cursor.next_token, "next")
        self.assertEqual(cursor.error, "Cloud Logging read timed out.")

    def test_logging_keeps_one_query_when_the_other_times_out(self):
        provider = CloudTelemetryProvider(
            "demo-project",
            log_limit=1,
            log_pages=1,
            runner=AllTokenRunner(),
        )
        provider._start = datetime(2026, 8, 1, tzinfo=UTC)
        provider._end = datetime(2026, 8, 2, tzinfo=UTC)

        def read(request, *, timeout):
            body = json.loads(request.data)
            if "jsonPayload.audit_event" in body["filter"]:
                raise TimeoutError
            return JsonResponse({"entries": [logging_payload("wrapped")]})

        with patch(
            "admin_console.telemetry.urllib.request.urlopen",
            side_effect=read,
        ):
            provider._advance_logging(1, time.monotonic() + 90)

        state = provider._logging_source_state()
        self.assertEqual(state.status, "partial")
        self.assertEqual(state.records_read, 1)
        self.assertIn("Retained 1 audit record", state.detail)
        self.assertIn("timed out", state.detail)

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

    def test_trace_continues_from_the_retained_cursor(self):
        provider = CloudTelemetryProvider(
            "demo-project",
            trace_limit=1,
            trace_pages=1,
            runner=TokenRunner(),
        )
        cursor = _PageCursor()
        traces = {}
        responses = [
            JsonResponse(
                {"traces": [trace_payload("one")], "nextPageToken": "next"}
            ),
            JsonResponse({"traces": [trace_payload("two")]}),
        ]
        start = datetime(2026, 8, 1, tzinfo=UTC)
        end = datetime(2026, 8, 2, tzinfo=UTC)

        with patch(
            "admin_console.telemetry.urllib.request.urlopen",
            side_effect=responses,
        ) as urlopen:
            provider._read_trace_pages(
                start, end, cursor, traces, 1, time.monotonic() + 90
            )
            provider._read_trace_pages(
                start, end, cursor, traces, 1, time.monotonic() + 90
            )

        self.assertEqual(set(traces), {"one", "two"})
        self.assertEqual(cursor.pages_read, 2)
        self.assertTrue(cursor.complete)
        second_query = parse_qs(
            urlparse(urlopen.call_args_list[1].args[0].full_url).query
        )
        self.assertEqual(second_query["pageToken"], ["next"])

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
        self.assertEqual(event.agent_name, "gateway-runtime")
        self.assertEqual(event.details["collector_container"], "fluent-bit")

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
        self.assertTrue(all(event.trigger_kind == TriggerKind.UNKNOWN for event in events))
        self.assertTrue(
            all(event.attribution == AttributionLevel.INHERITED for event in events)
        )
        self.assertIn("[REDACTED]", events[1].details["tool_arguments"])
        self.assertNotIn("should-not-render", events[1].details["tool_arguments"])
        self.assertEqual(events[1].details["parent_span_name"], "agent")
        self.assertEqual(events[1].details["otel.session.id"], "web_abc")

    def test_watcher_platform_remains_raw_and_is_not_classified_as_human(self):
        trace = {
            "traceId": "watcher-trace",
            "spans": [
                {
                    "spanId": "model",
                    "name": "api.model-default",
                    "startTime": "2026-08-13T10:00:00Z",
                    "endTime": "2026-08-13T10:00:01Z",
                    "labels": {
                        "session.id": "k8s-evt-abcd",
                        "chat.platform": "k8s-watcher",
                        "hermes.session.kind": "session",
                    },
                }
            ],
        }

        event = normalize_trace(trace, "demo-project")[0]

        self.assertEqual(event.trigger_kind, TriggerKind.UNKNOWN)
        self.assertEqual(event.platform, "k8s-watcher")
        self.assertEqual(event.details["otel.session.id"], "k8s-evt-abcd")
        self.assertEqual(event.details["otel.chat.platform"], "k8s-watcher")
        self.assertEqual(event.details["otel.hermes.session.kind"], "session")
        self.assertNotIn("otel.user.id", event.details)

    def test_trace_origin_is_preserved_on_child_spans(self):
        trace = {
            "traceId": "cron-trace",
            "spans": [
                {
                    "spanId": "root",
                    "name": "cron",
                    "startTime": "2026-08-13T10:00:00Z",
                    "endTime": "2026-08-13T10:00:02Z",
                    "labels": {
                        "session.id": "cron_compliance-audit_20260813_100000",
                        "hermes.session.kind": "cron",
                    },
                },
                {
                    "spanId": "model",
                    "parentSpanId": "root",
                    "name": "api.model-default",
                    "startTime": "2026-08-13T10:00:00Z",
                    "endTime": "2026-08-13T10:00:01Z",
                    "labels": {
                        "session.id": "cron_compliance-audit_20260813_100000"
                    },
                },
            ],
        }

        child = normalize_trace(trace, "demo-project")[1]

        self.assertEqual(child.trigger_kind, TriggerKind.CRON)
        self.assertEqual(
            child.details["otel.trace.hermes.session.kind"], "cron"
        )

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

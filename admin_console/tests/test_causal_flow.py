from __future__ import annotations

import unittest
from datetime import UTC, datetime

from admin_console.causal_flow import CausalFlowProjection
from admin_console.charts import causality_sankey
from admin_console.domain import ActivityEvent, AttributionLevel, TriggerKind


def event(
    event_id: str,
    action_type: str,
    *,
    source: str = "cloud_trace",
    span_name: str = "",
    parent_span_name: str = "",
    attribution: AttributionLevel = AttributionLevel.INHERITED,
    session_id: str = "session-one",
    origin: dict[str, str] | None = None,
) -> ActivityEvent:
    return ActivityEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
        interaction_id="interaction-one",
        trigger_kind=TriggerKind.HUMAN,
        action_type=action_type,
        action_name=event_id,
        status="completed",
        summary="test event",
        agent_name="platform-agent",
        user_id="user-one",
        session_id=session_id,
        tool_name="terminal" if action_type == "tool" else "",
        attribution=attribution,
        trace_id="trace-one",
        details={
            "source": source,
            "span_name": span_name,
            "parent_span_name": parent_span_name,
            **(origin or {}),
        },
    )


class CausalFlowProjectionTest(unittest.TestCase):
    def test_keeps_canonical_llm_work_from_trace_lineage(self):
        projection = CausalFlowProjection.from_events(
            [
                event("model-api", "model", span_name="api.model-default"),
                event("model-wrapper", "model", span_name="llm.model-default"),
                event("tool", "tool", parent_span_name="llm.model-default"),
                event("orphan-tool", "tool", parent_span_name="agent"),
                event(
                    "approval", "approval", parent_span_name="llm.model-default"
                ),
                event("skill", "skill", parent_span_name="agent"),
                event(
                    "logging-copy",
                    "tool",
                    source="cloud_logging",
                    parent_span_name="llm.model-default",
                ),
                event("lifecycle", "span", span_name="Gateway Dispatch"),
                event(
                    "unattributed-model",
                    "model",
                    span_name="api.model-default",
                    attribution=AttributionLevel.MISSING,
                ),
            ]
        )

        self.assertEqual(
            [item.event_id for item in projection.events],
            ["model-api", "tool", "approval", "skill", "unattributed-model"],
        )
        self.assertEqual(projection.llm_calls, 2)
        self.assertEqual(projection.tool_calls, 1)
        self.assertEqual(projection.approvals, 1)
        self.assertEqual(projection.skill_loads, 1)
        self.assertEqual(projection.hidden_evidence, 4)
        self.assertIn("4 transport, lifecycle", projection.summary)

    def test_chart_labels_canonical_model_work_as_llm_call(self):
        figure = causality_sankey(
            [event("model-api", "model", span_name="api.model-default")]
        )

        self.assertIn("LLM call", tuple(figure.data[0].node.label))

    def test_chart_aggregates_raw_platform_origin_and_preserves_metadata(self):
        watcher_origin = {
            "otel.chat.platform": "k8s-watcher",
            "otel.hermes.session.kind": "session",
        }
        figure = causality_sankey(
            [
                event(
                    "watcher-one",
                    "model",
                    span_name="api.model-default",
                    session_id="k8s-evt-one",
                    origin={
                        **watcher_origin,
                        "otel.session.id": "k8s-evt-one",
                    },
                ),
                event(
                    "watcher-two",
                    "model",
                    span_name="api.model-default",
                    session_id="k8s-evt-two",
                    origin={
                        **watcher_origin,
                        "otel.session.id": "k8s-evt-two",
                    },
                ),
            ]
        )

        labels = tuple(figure.data[0].node.label)
        self.assertIn("k8s-watcher (chat.platform · 2 sessions)", labels)
        self.assertFalse(any("Human" in label for label in labels))
        watcher_index = labels.index("k8s-watcher (chat.platform · 2 sessions)")
        self.assertIn(
            "chat.platform=k8s-watcher",
            figure.data[0].node.customdata[watcher_index],
        )

    def test_chart_prefers_raw_user_id_and_retains_its_platform(self):
        figure = causality_sankey(
            [
                event(
                    "chat-one",
                    "model",
                    span_name="api.model-default",
                    origin={
                        "otel.user.id": "google_chat:digest",
                        "otel.chat.platform": "google_chat",
                    },
                )
            ]
        )

        labels = tuple(figure.data[0].node.label)
        label = "google_chat:digest (user.id · 1 session)"
        self.assertIn(label, labels)
        detail = figure.data[0].node.customdata[labels.index(label)]
        self.assertIn("user.id=google_chat:digest", detail)
        self.assertIn("chat.platform=google_chat", detail)

    def test_chart_normalizes_cron_executions_by_job(self):
        figure = causality_sankey(
            [
                event(
                    "cron-one",
                    "model",
                    span_name="api.model-default",
                    session_id="cron_compliance-audit_20260813_010203",
                    origin={
                        "otel.session.id": "cron_compliance-audit_20260813_010203"
                    },
                ),
                event(
                    "cron-two",
                    "model",
                    span_name="api.model-default",
                    session_id="cron_compliance-audit_20260813_040506",
                    origin={
                        "otel.session.id": "cron_compliance-audit_20260813_040506"
                    },
                ),
            ]
        )

        labels = tuple(figure.data[0].node.label)
        label = "compliance-audit (cron · 2 sessions)"
        self.assertIn(label, labels)
        detail = figure.data[0].node.customdata[labels.index(label)]
        self.assertIn("source.type=cron", detail)
        self.assertIn("cron_compliance-audit_20260813_010203", detail)
        self.assertIn("cron_compliance-audit_20260813_040506", detail)

    def test_chart_uses_raw_session_id_when_no_richer_origin_exists(self):
        figure = causality_sankey(
            [
                event(
                    "session-only",
                    "model",
                    span_name="api.model-default",
                    session_id="20260813_195953_7d1cac",
                    origin={"otel.session.id": "20260813_195953_7d1cac"},
                )
            ]
        )

        labels = tuple(figure.data[0].node.label)
        self.assertIn(
            "20260813_195953_7d1cac (session.id · 1 session)", labels
        )
        self.assertFalse(any(label.startswith("unknown") for label in labels))


if __name__ == "__main__":
    unittest.main()

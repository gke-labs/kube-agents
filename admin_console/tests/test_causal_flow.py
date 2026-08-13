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
        session_id="session-one",
        tool_name="terminal" if action_type == "tool" else "",
        attribution=attribution,
        trace_id="trace-one",
        details={
            "source": source,
            "span_name": span_name,
            "parent_span_name": parent_span_name,
        },
    )


class CausalFlowProjectionTest(unittest.TestCase):
    def test_keeps_only_trusted_llm_work_from_trace_lineage(self):
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
            ["model-api", "tool", "approval", "skill"],
        )
        self.assertEqual(projection.llm_calls, 1)
        self.assertEqual(projection.tool_calls, 1)
        self.assertEqual(projection.approvals, 1)
        self.assertEqual(projection.skill_loads, 1)
        self.assertEqual(projection.hidden_evidence, 5)
        self.assertIn("5 transport, lifecycle", projection.summary)

    def test_chart_labels_canonical_model_work_as_llm_call(self):
        figure = causality_sankey(
            [event("model-api", "model", span_name="api.model-default")]
        )

        self.assertIn("LLM call", tuple(figure.data[0].node.label))


if __name__ == "__main__":
    unittest.main()

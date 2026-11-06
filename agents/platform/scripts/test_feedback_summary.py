"""Unit tests for feedback_summary.py: the copy-pasteable usage and feedback summary.

Run: python3 -m unittest agents/platform/scripts/test_feedback_summary.py
"""

from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import re
import unittest
from pathlib import Path
from unittest import mock

import feedback_summary as fs

NOW = dt.datetime(2026, 10, 20, 18, 0, tzinfo=dt.timezone.utc)
CLUSTER = "prod-eu-1"
PROJECT = "acme-platform-prod"
NAMESPACE = "kubeagents-system"
TAG = "0.14.0"

CR = {
    "metadata": {
        "name": "platform-agent",
        "namespace": NAMESPACE,
        "creationTimestamp": "2026-10-01T12:00:00Z",
    },
    "spec": {
        "mode": "today",
        "harness": {"clusterName": CLUSTER, "projectId": PROJECT, "location": "us-central1", "memory": {"memoryEnabled": "false", "provider": "multiuser_memory"}},
        "scope": {"projects": ["acme-scope-dev"], "exclude": {"clusters": ["old-cluster"]}},
        "integration": {"gitops": {"repository": "acme/gitops"}},
        "deployment": {"tag": TAG, "env": [{"name": "OTEL_SDK_DISABLED", "value": "true"}],
                       "podAnnotations": {"cluster-autoscaler.kubernetes.io/safe-to-evict": "true"},
                       "sidecars": [{"name": "x", "args": ["false", "12"]}]},
    },
    "status": {
        "conditions": [{"type": "Ready", "status": "True"}],
        "inference": {"provider": "vertex_ai", "model": "claude-opus-5"},
        "usage": {
            "clustersRegistered": 3,
            "clustersMonitored": 2,
            "chatMessagesInboundTotal": 41,
            "sessionsTotal": 7,
            "chatSessionsTotal": 4,
            "toolExecutionsTotal": 19127,
            "toolExecutionsBlockedTotal": 12,
            "brokerFailuresTotal": 3,
            "eventsIngestedTotal": 10,
            "chatTurnsErroredTotal": 2,
            "backgroundTasksGaveUpTotal": 1,
            "negativeFeedbackTotal": 4,
            "remediationsProposedTotal": 4,
            "remediationsMergedTotal": 1,
            "proposalsAuthored": 6,
            "remediationsOpen": 2,
            "remediationsClosedUnmergedTotal": 3,
            "proposalHumanTouches": 5,
            "peopleSeen7d": 3,
            "peopleSeen30d": 11,
            "lastActiveTime": "2026-10-20T17:34:06Z",
            "lastHumanActiveTime": "2026-10-20T16:10:00Z",
            "activeInterfaces": ["googlechat"],
        },
    },
}

LITELLM_CONFIG_YAML = """model_list:
  - model_name: model-default
    litellm_params:
      model: vertex_ai/claude-opus-5
      vertex_project: acme-platform-prod
"""

#: A gateway config that disagrees with the fixture's status.inference.
OTHER_LITELLM_CONFIG_YAML = """model_list:
  - model_name: model-default
    litellm_params:
      model: openai/gpt-4o
"""

SERVER_VERSION = {"serverVersion": {"gitVersion": "v1.33.4-gke.1234000"}}


def _task(status, created_min_ago, duration_s=None, title="list pods in prod-eu-1"):
    created = int((NOW - dt.timedelta(minutes=created_min_ago)).timestamp())
    completed = created + duration_s if duration_s is not None else None
    return {
        "id": f"t_{created_min_ago}",
        "title": title,
        "assignee": "platform",
        "status": status,
        "priority": 0,
        "session_id": "s1",
        "created_at": created,
        "started_at": created,
        "completed_at": completed,
        "updated_at": completed or created,
        "summary": f"touched {CLUSTER} in {PROJECT}",
        "result": f"token ya29.{'A' * 40} on {CLUSTER}",
        "error": None,
    }


TASKS = [
    _task("done", 10, 95),
    _task("done", 60, 310),
    _task("blocked", 120, 40),
    _task("blocked", 600, 2000),
    _task("failed", 90, 12),
    _task("cancelled", 100),
    _task("running", 2),
    _task("done", 30 * 24 * 60, 50),  # outside the window
]

LITELLM_METRICS = """# HELP litellm_spend_metric_total Total spend on LLM requests
# TYPE litellm_spend_metric_total counter
litellm_spend_metric_total{api_provider="vertex_ai",model="claude-opus-5",requested_model="model-default"} 2.907
litellm_input_tokens_metric_total{api_provider="vertex_ai",model="claude-opus-5"} 1200000
litellm_output_tokens_metric_total{api_provider="vertex_ai",model="claude-opus-5"} 34000
litellm_input_tokens_metric_total{api_provider="vertex_ai",model="other"} 100
litellm_request_total_latency_metric_bucket{le="1.0",model="claude-opus-5"} 3
litellm_request_total_latency_metric_bucket{le="5.0",model="claude-opus-5"} 9
litellm_request_total_latency_metric_bucket{le="30.0",model="claude-opus-5"} 10
litellm_request_total_latency_metric_bucket{le="+Inf",model="claude-opus-5"} 10
litellm_request_total_latency_metric_count{model="claude-opus-5"} 10
"""

BROKER_METRICS = """# HELP kubeagents_tool_invocations_total Counter of credential broker tool executions
# TYPE kubeagents_tool_invocations_total counter
kubeagents_tool_invocations_total{status="success",subcommand="get",tool="kubectl"} 14500
kubeagents_tool_invocations_total{status="error",subcommand="get",tool="kubectl"} 400
kubeagents_tool_invocations_total{status="success",subcommand="describe",tool="kubectl"} 3100
kubeagents_tool_invocations_total{status="success",subcommand="monitoring",tool="gcloud"} 800
kubeagents_tool_invocations_total{status="error",subcommand="monitoring",tool="gcloud"} 200
kubeagents_tool_invocations_total{status="blocked",subcommand="delete",tool="kubectl"} 12
kubeagents_tool_invocations_total{status="success",subcommand="logs",tool="kubectl"} 100
kubeagents_tool_invocations_total{status="success",subcommand="top",tool="kubectl"} 15
"""

SCALE = {
    "namespaces": 48,
    "deployments": 312,
    "pods": 1840,
    "nodes": 42,
    "crds": 215,
    "kcc": 148,
}


#: status.usage.topTools as the operator writes it and the lines both renderers
#: print for it; the gateway's feedback_summary_test.go renders the same file.
TOP_TOOLS_FIXTURE = Path(__file__).resolve().parent / "testdata" / "feedback_summary_top_tools.json"


def top_tools_fixture():
    fixture = json.loads(TOP_TOOLS_FIXTURE.read_text())
    return fixture["topTools"], fixture["lines"]


def cr_with_top_tools(rows):
    cr = json.loads(json.dumps(CR))
    cr["status"]["usage"]["topTools"] = rows
    return cr


def full_snapshot(**overrides):
    base = dict(
        generated_at=NOW,
        cr=CR,
        server_version=SERVER_VERSION,
        litellm_config=LITELLM_CONFIG_YAML,
        tasks=TASKS,
        litellm_metrics=LITELLM_METRICS,
        sources={"session-kv": True, "litellm-metrics": True, "broker-metrics": True, "scale": True},
        broker_metrics=BROKER_METRICS,
        scale=SCALE,
    )
    base.update(overrides)
    return fs.Snapshot(**base)


class RenderTest(unittest.TestCase):
    def test_a_full_snapshot_renders_one_fenced_block_under_the_line_cap(self):
        text = fs.render(full_snapshot())
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("```"))
        self.assertEqual(lines[-1], "```")
        self.assertLessEqual(len(lines), fs.SUMMARY_MAX_LINES)
        self.assertIn("mode: today", text)
        self.assertIn(f"version: {TAG}", text)
        self.assertIn("model: vertex_ai/claude-opus-5", text)
        self.assertIn("kubernetes: v1.33.4-gke.1234000", text)
        self.assertIn("installed: 19 days", text)
        self.assertIn("toolExecutionsTotal: 19127", text)
        self.assertIn("toolExecutionsBlockedTotal: 12", text)
        self.assertIn("brokerFailuresTotal: 3", text)
        self.assertIn("chatTurnsErroredTotal: 2", text)
        self.assertIn("backgroundTasksGaveUpTotal: 1", text)
        self.assertIn("negativeFeedbackTotal: 4", text)
        self.assertIn("lastHumanActive: 2026-10-20T16:10:00Z", text)
        self.assertIn("scale: namespaces 48, deployments 312, pods 1840, nodes 42, crds 215 (kcc 148)", text)
        self.assertIn("tools (top 5, since broker start):", text)
        self.assertIn("  kubectl/get: 14900 (success 14500, error 400)\n", text)
        self.assertIn("  kubectl/describe: 3100 (success 3100)\n", text)
        self.assertIn("  gcloud/monitoring: 1000 (success 800, error 200)\n", text)
        self.assertIn("  kubectl/logs: 100 (success 100)\n", text)
        self.assertIn("  kubectl/top: 15 (success 15)\n", text)
        self.assertNotIn("kubectl/delete", text)
        self.assertIn("  sessionsTotal: 7\n  chatSessionsTotal: 4\n", text)
        self.assertIn("  remediationsMergedTotal: 1 (as the forge reports)\n", text)
        self.assertIn("  proposalsAuthored: 6 (as the forge reports)\n", text)
        self.assertIn("  remediationsOpen: 2 (as the forge reports)\n", text)
        self.assertIn("  remediationsClosedUnmergedTotal: 3 (as the forge reports)\n", text)
        self.assertIn("  proposalHumanTouches: 5 (as the forge reports)\n", text)
        self.assertIn("  peopleSeen7d: 3 (as the gateway reports)\n", text)
        self.assertIn("  peopleSeen30d: 11 (as the gateway reports)\n", text)
        self.assertIn("  remediationsProposedTotal: 4\n", text)
        self.assertIn("Rating:", text)
        self.assertIn("What worked well:", text)
        self.assertIn("What failed or was slow:", text)

    def test_the_block_carries_no_identifier_from_the_install(self):
        text = fs.render(full_snapshot())
        for forbidden in (CLUSTER, PROJECT, NAMESPACE, "ya29.", "list pods", "s1", "us-central1"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertEqual(fs.audit(text, [CLUSTER, PROJECT, NAMESPACE]), [])

    def test_audit_names_a_leaked_identifier(self):
        self.assertEqual(fs.audit(f"all good on {CLUSTER}", [CLUSTER, PROJECT]), [CLUSTER])

    def test_audit_matches_whole_tokens_only(self):
        text = fs.render(full_snapshot())
        self.assertEqual(fs.audit(text, ["kube-agents", "today", "usage", "0.14"]), [])
        self.assertEqual(fs.audit("version: 0.14.0-rc", ["0.14.0"]), [])
        self.assertEqual(fs.audit("cluster prod-eu-1 here", ["prod-eu-1"]), ["prod-eu-1"])

    def test_identifiers_are_the_naming_subtrees_strings_plus_name_and_namespace(self):
        ids = full_snapshot().identifiers
        for expected in (CLUSTER, PROJECT, NAMESPACE, "us-central1", "acme-scope-dev", "old-cluster", "acme/gitops", "platform-agent"):
            self.assertIn(expected, ids)
        self.assertNotIn(TAG, ids)
        self.assertNotIn("today", ids)

    def test_a_namespace_named_kube_agents_is_not_a_leak(self):
        cr = json.loads(json.dumps(CR))
        cr["metadata"]["namespace"] = "kube-agents"
        cr["spec"]["harness"]["clusterName"] = "today"
        snapshot = full_snapshot(cr=cr)
        self.assertEqual(fs.audit(fs.render(snapshot), snapshot.identifiers), [])

    def test_a_truncated_feed_prints_a_plus(self):
        text = fs.render(full_snapshot(tasks_truncated=True))
        self.assertIn("tasks (14 days): 7+ (", text)

    def test_settings_spelled_true_or_numeric_are_not_identifiers(self):
        ids = full_snapshot().identifiers
        for setting in ("true", "false", "12", "OTEL_SDK_DISABLED", "cluster-autoscaler.kubernetes.io/safe-to-evict"):
            self.assertNotIn(setting, ids)
        text = fs.render(full_snapshot())
        self.assertIn("ready: true", text)
        self.assertEqual(fs.audit(text, ids), [])

    def test_rendering_is_deterministic(self):
        self.assertEqual(fs.render(full_snapshot()), fs.render(full_snapshot()))

    def test_tasks_are_rolled_up_by_status_and_duration_only(self):
        text = fs.render(full_snapshot())
        self.assertIn("tasks (14 days): 7", text)
        self.assertIn("done 2", text)
        self.assertIn("failed 1", text)
        self.assertIn("cancelled 1", text)
        self.assertIn("blocked 2", text)
        self.assertNotIn("gave_up", text)
        self.assertNotIn("other", text.split("tasks (14 days)")[1].splitlines()[0])
        self.assertIn("active 1", text)
        self.assertIn("completed p50 1m35s / p95 5m10s", text)
        # the newest completed tasks, one line each, without titles
        self.assertRegex(text, r"\n  - 10m ago +done +1m35s\n")
        self.assertIn("  - 10h ago  blocked  33m20s", text)
        self.assertNotIn("prod-eu", text)

    def test_tokens_and_spend_come_from_the_gateway_metrics(self):
        text = fs.render(full_snapshot())
        self.assertIn("tokens: in 1200100 / out 34000", text)
        self.assertIn("spend: 2.91 USD", text)
        self.assertIn("latency p50 2.3s / p95 17.5s", text)

    def test_missing_sources_degrade_to_unavailable_lines(self):
        cr = json.loads(json.dumps(CR))
        del cr["status"]["inference"]
        snapshot = full_snapshot(
            cr=cr, tasks=None, litellm_metrics=None, litellm_config=None, server_version=None,
            broker_metrics=None, scale=None,
            sources={"session-kv": False, "litellm-metrics": False, "broker-metrics": False, "scale": False},
        )
        text = fs.render(snapshot)
        self.assertIn("tasks (14 days): unavailable", text)
        self.assertIn("tools (top 5, since broker start): unavailable", text)
        self.assertIn("tokens: unavailable", text)
        self.assertIn("model: unavailable", text)
        self.assertIn("kubernetes: unavailable", text)
        self.assertNotIn("scale:", text)
        self.assertLessEqual(len(text.splitlines()), fs.SUMMARY_MAX_LINES)

    def test_the_model_line_reads_status_inference_first(self):
        text = fs.render(full_snapshot(litellm_config=OTHER_LITELLM_CONFIG_YAML))
        self.assertIn("\n  model: vertex_ai/claude-opus-5\n", text)
        self.assertNotIn("gpt-4o", text)

    def test_an_empty_inference_key_prints_unavailable_without_a_fallback(self):
        cr = json.loads(json.dumps(CR))
        cr["status"]["inference"] = {}
        text = fs.render(full_snapshot(cr=cr, litellm_config=OTHER_LITELLM_CONFIG_YAML))
        self.assertIn("\n  model: unavailable\n", text)
        self.assertNotIn("gpt-4o", text)
        cr["status"]["inference"] = {"provider": "vertex_ai"}
        self.assertIn("\n  model: unavailable\n", fs.render(full_snapshot(cr=cr)))

    def test_an_absent_inference_key_falls_back_to_the_gateway_config(self):
        cr = json.loads(json.dumps(CR))
        del cr["status"]["inference"]
        text = fs.render(full_snapshot(cr=cr, litellm_config=OTHER_LITELLM_CONFIG_YAML))
        self.assertIn("\n  model: openai/gpt-4o\n", text)

    def test_the_environment_lines_print_in_the_shared_key_order(self):
        text = fs.render(full_snapshot(scale=None))
        keys = [line.split(":", 1)[0].strip() for line in text.split("environment:\n", 1)[1].split("\nusage:", 1)[0].splitlines()]
        self.assertEqual(keys, list(fs.ENVIRONMENT_KEYS))

    def test_empty_broker_metrics_prints_none_and_tools_namespace_is_not_a_leak(self):
        cr = json.loads(json.dumps(CR))
        cr["metadata"]["namespace"] = "tools"
        snapshot = full_snapshot(cr=cr, broker_metrics="# no tool invocations yet\n")
        text = fs.render(snapshot)
        self.assertIn("tools (top 5, since broker start): none", text)
        self.assertEqual(fs.audit(text, snapshot.identifiers), [])
        snapshot_with_tools = full_snapshot(cr=cr)
        self.assertEqual(fs.audit(fs.render(snapshot_with_tools), snapshot_with_tools.identifiers), [])

    def test_an_empty_usage_block_prints_zeroes_not_blanks(self):
        cr = json.loads(json.dumps(CR))
        cr["status"] = {}
        text = fs.render(full_snapshot(cr=cr))
        self.assertIn("toolExecutionsTotal: 0", text)
        self.assertIn("toolExecutionsBlockedTotal: 0", text)
        self.assertIn("brokerFailuresTotal: 0", text)
        self.assertIn("chatTurnsErroredTotal: 0", text)
        self.assertIn("backgroundTasksGaveUpTotal: 0", text)
        self.assertIn("negativeFeedbackTotal: 0", text)
        self.assertIn("lastActive: never", text)
        self.assertIn("lastHumanActive: never", text)
        self.assertIn("ready: unknown", text)

    def test_an_unread_broker_falls_back_to_the_status_top_tools(self):
        rows, lines = top_tools_fixture()
        snapshot = full_snapshot(cr=cr_with_top_tools(rows), broker_metrics=None)
        text = fs.render(snapshot)
        self.assertIn("  lastHumanActive: 2026-10-20T16:10:00Z\n" + "\n".join(lines) + "\nperformance:", text)
        self.assertEqual(fs.audit(text, snapshot.identifiers), [])
        self.assertLessEqual(len(text.split("\n")), fs.SUMMARY_MAX_LINES)

    def test_the_brokers_text_wins_over_the_status_top_tools(self):
        rows, lines = top_tools_fixture()
        text = fs.render(full_snapshot(cr=cr_with_top_tools(rows)))
        self.assertIn("  kubectl/get: 14900 (success 14500, error 400)", text)
        self.assertNotIn(lines[4], text)  # the fixture's delete row is not in BROKER_METRICS

    def test_without_the_broker_or_the_status_the_tools_line_is_unavailable(self):
        text = fs.render(full_snapshot(broker_metrics=None))
        self.assertIn("\ntools (top 5, since broker start): unavailable\nperformance:", text)
        empty = fs.render(full_snapshot(cr=cr_with_top_tools([]), broker_metrics=None))
        self.assertIn("\ntools (top 5, since broker start): none\n", empty)

    def test_status_rows_past_five_or_not_objects_are_dropped(self):
        rows = ["not a row"] + [{"tool": "git", "subcommand": "fetch", "total": 7 - i, "success": 7 - i} for i in range(7)]
        text = fs.render(full_snapshot(cr=cr_with_top_tools(rows), broker_metrics=None))
        self.assertEqual(text.count("  git/fetch: "), 5)
        self.assertNotIn("not a row", text)

    def test_the_brokers_text_and_the_status_render_the_same_rows(self):
        """The operator folds the broker's text as summarise_broker_tools does, so a
        status built from the same samples renders byte for byte the same lines."""
        from_text = fs.render(full_snapshot())
        rows = []
        for pair, total, counts in fs.summarise_broker_tools(fs.parse_prometheus(BROKER_METRICS)):
            tool, _, sub = pair.partition(fs.TOOL_PAIR_SEPARATOR)
            rows.append({"tool": tool, "subcommand": sub or fs.NONE, "total": total, **counts})
        from_status = fs.render(full_snapshot(cr=cr_with_top_tools(rows), broker_metrics=None))
        self.assertEqual(from_text, from_status)

    def test_a_bare_tool_sorts_by_tool_then_subcommand_on_a_tie(self):
        broker_text = (
            'kubeagents_tool_invocations_total{status="success",subcommand="none",tool="kubectl"} 5\n'
            'kubeagents_tool_invocations_total{status="success",subcommand="apply",tool="kubectl"} 5\n'
            'kubeagents_tool_invocations_total{status="success",subcommand="none",tool="gcloud"} 5\n'
        )
        pairs = [pair for pair, _total, _counts in fs.summarise_broker_tools(fs.parse_prometheus(broker_text))]
        self.assertEqual(pairs, ["gcloud", "kubectl/apply", "kubectl"])

    def test_unknown_broker_tool_labels_fold_into_other_so_no_identifier_leaks(self):
        broker_text = (
            f'kubeagents_tool_invocations_total{{status="success",subcommand="{CLUSTER}",tool="{PROJECT}"}} 42\n'
            'kubeagents_tool_invocations_total{status="success",subcommand="none",tool="feedback"} 5\n'
        )
        snapshot = full_snapshot(broker_metrics=broker_text)
        text = fs.render(snapshot)
        self.assertIn("  other/other: 42 (success 42)\n", text)
        self.assertIn("  feedback: 5 (success 5)\n", text)
        self.assertEqual(fs.audit(text, snapshot.identifiers), [])

    def test_readings_print_no_reading_when_absent_and_counters_print_zero(self):
        cr = json.loads(json.dumps(CR))
        cr["status"]["usage"] = {"remediationsOpen": 0, "peopleSeen7d": 0}
        text = fs.render(full_snapshot(cr=cr))
        for reading in ("remediationsMergedTotal", "proposalsAuthored", "remediationsClosedUnmergedTotal", "proposalHumanTouches", "proposalsMergedUntouched"):
            self.assertIn(f"  {reading}: no reading (as the forge reports)\n", text)
        self.assertIn("  remediationsOpen: 0 (as the forge reports)\n", text)
        self.assertIn("  peopleSeen7d: 0 (as the gateway reports)\n", text)
        self.assertIn("  peopleSeen30d: no reading (as the gateway reports)\n", text)
        for counter in ("clustersRegistered", "toolExecutionsTotal", "remediationsProposedTotal"):
            self.assertIn(f"  {counter}: 0\n", text)
        self.assertNotIn("remediationsAppliedTotal", fs.USAGE_COUNTERS)
        self.assertEqual(fs.audit(text, full_snapshot(cr=cr).identifiers), [])

    def test_next_mode_and_interfaces_are_printed_as_enums(self):
        cr = json.loads(json.dumps(CR))
        cr["spec"]["mode"] = "next"
        cr["status"]["usage"]["activeInterfaces"] = ["slack", "googlechat"]
        text = fs.render(full_snapshot(cr=cr))
        self.assertIn("mode: next", text)
        self.assertIn("interfaces: googlechat, slack", text)


class ParsingTest(unittest.TestCase):
    def test_prometheus_text_parses_names_labels_and_values(self):
        samples = fs.parse_prometheus(LITELLM_METRICS)
        spend = [s for s in samples if s.name == "litellm_spend_metric_total"]
        self.assertEqual(len(spend), 1)
        self.assertEqual(spend[0].labels["model"], "claude-opus-5")
        self.assertAlmostEqual(spend[0].value, 2.907)

    def test_histogram_quantiles_interpolate_within_the_bucket(self):
        samples = fs.parse_prometheus(LITELLM_METRICS)
        p50, p95 = fs.histogram_quantiles(samples, "litellm_request_total_latency_metric", (0.5, 0.95))
        self.assertAlmostEqual(p50, 1.0 + (5.0 - 1.0) * ((5 - 3) / 6), places=3)
        self.assertAlmostEqual(p95, 5.0 + (30.0 - 5.0) * ((9.5 - 9) / 1), places=3)

    def test_a_histogram_with_no_samples_yields_nothing(self):
        self.assertIsNone(fs.histogram_quantiles([], "litellm_request_total_latency_metric", (0.5,)))

    def test_the_model_line_is_read_from_the_gateway_config(self):
        self.assertEqual(fs.model_from_litellm_config(LITELLM_CONFIG_YAML), "vertex_ai/claude-opus-5")
        self.assertEqual(fs.model_from_litellm_config("  model: vertex_ai/claude-3-5-sonnet@20240620\n"), "vertex_ai/claude-3-5-sonnet@20240620")

    def test_a_resource_path_model_prints_its_provider_and_last_segment_only(self):
        config = (
            "model_list:\n- model_name: x\n  litellm_params:\n"
            "    model: vertex_ai/projects/acme-prod-123/locations/us-central1/endpoints/987\n"
        )
        model = fs.model_from_litellm_config(config)
        self.assertEqual(model, "vertex_ai/987")
        self.assertNotIn("acme-prod-123", model)
        self.assertNotIn("us-central1", model)
        self.assertEqual(fs.model_from_litellm_config("model: openrouter/anthropic/claude-sonnet-4\n"), "openrouter/anthropic/claude-sonnet-4")
        self.assertEqual(fs.model_from_litellm_config("model: hosted_vllm/meta-llama/Llama-3.1-8B-Instruct\n"), "hosted_vllm/meta-llama/Llama-3.1-8B-Instruct")
        self.assertEqual(fs.model_from_litellm_config("model: vertex_ai/claude-opus-5\n"), "vertex_ai/claude-opus-5")

    def test_a_bedrock_arn_keeps_its_resource_id_only(self):
        for model, want in (
            ("bedrock/arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-opus-5", "bedrock/us.anthropic.claude-opus-5"),
            ("bedrock/converse/arn:aws:bedrock:us-east-1:123456789012:inference-profile/abc", "bedrock/abc"),
            ("bedrock/invoke/arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/xyz", "bedrock/xyz"),
            ("bedrock/arn:aws:bedrock:us-east-1:123456789012:provisioned-model:pm1", "bedrock/pm1"),
        ):
            got = fs.model_from_litellm_config(f"model: {model}\n")
            self.assertEqual(got, want, model)
            self.assertNotIn("123456789012", got)
            self.assertNotIn("us-east-1", got)

    def test_a_truncated_resource_reference_is_malformed(self):
        for model in (
            "vertex_ai/projects/acme-prod",
            "vertex_ai/projects/acme/locations/us-central1",
            "vertex_ai/projects/acme/regions/us-central1/endpoints/987",
            "bedrock/arn:aws:bedrock:us-east-1:123456789012",
            "bedrock/arn:aws:bedrock:us-east-1:123456789012:",
        ):
            self.assertIsNone(fs.split_model(model), model)
        self.assertEqual(fs.split_model("vertex_ai/projects/acme-prod-123/locations/us-central1/endpoints/987"), "vertex_ai/987")
        self.assertEqual(fs.split_model("bedrock/arn:aws:bedrock:us-east-1:123456789012:provisioned-model:abc"), "bedrock/abc")
        self.assertEqual(fs.split_model("bedrock/converse/arn:aws:bedrock:us-east-1:123456789012:inference-profile/p1"), "bedrock/p1")

    def test_the_model_default_entry_answers_over_an_earlier_entry(self):
        config = (
            "model_list:\n"
            "  - model_name: fast\n    litellm_params:\n      model: openai/gpt-4o-mini\n"
            "  - model_name: \"model-default\"\n    litellm_params:\n      api_base: http://x\n      model: 'anthropic/claude-opus-5'\n"
        )
        self.assertEqual(fs.model_from_litellm_config(config), "anthropic/claude-opus-5")

    def test_a_crlf_config_reads_its_model_default_entry(self):
        config = (
            "model_list:\r\n"
            "  - model_name: fast\r\n    litellm_params:\r\n      model: openai/gpt-4o-mini\r\n"
            "  - model_name: model-default\r\n    litellm_params:\r\n      model: vertex_ai/gemini-4-argon\r\n"
        )
        self.assertEqual(fs.model_from_litellm_config(config), "vertex_ai/gemini-4-argon")
        malformed = config.replace("vertex_ai/gemini-4-argon", "gpt-4o")
        self.assertIsNone(fs.model_from_litellm_config(malformed))

    def test_a_malformed_model_default_entry_is_a_negative_with_no_fallback(self):
        for model in ("gpt-4o", "gemini/", "/claude", '""', "projects/acme/locations/x/endpoints/9",
                      "arn:aws:bedrock:us-east-1:1:x/y", "vertex_ai/projects/acme/locations/x/endpoints/"):
            config = (
                "model_list:\n"
                "  - model_name: fast\n    litellm_params:\n      model: openai/gpt-4o-mini\n"
                f"  - model_name: model-default\n    litellm_params:\n      model: {model}\n"
                "  - model_name: later\n    litellm_params:\n      model: openai/gpt-4o\n"
            )
            self.assertIsNone(fs.model_from_litellm_config(config), model)
        empty = "model_list:\n  - model_name: model-default\n    litellm_params:\n      model:\n  - model_name: b\n    litellm_params:\n      model: openai/gpt-4o\n"
        self.assertIsNone(fs.model_from_litellm_config(empty))

    def test_whitespace_in_a_provider_or_name_is_malformed(self):
        for model in ("gemini/flash\nx", "gemini/gemini 3", "gem ini/flash", "gemini/flash\u00a0x"):
            self.assertIsNone(fs.split_model(model), repr(model))
        self.assertEqual(fs.split_model("gemini/flash"), "gemini/flash")

    def test_without_a_model_default_entry_the_first_qualified_model_answers(self):
        config = (
            "model_list:\n"
            "  - model_name: bare\n    litellm_params:\n      model: gpt-4o\n"
            "  - model_name: fast\n    litellm_params:\n      model: openai/gpt-4o-mini\n"
        )
        self.assertEqual(fs.model_from_litellm_config(config), "openai/gpt-4o-mini")

    def test_model_from_status_distinguishes_present_empty_and_absent(self):
        self.assertEqual(fs.model_from_status({"inference": {"provider": "gemini", "model": "gemini-3-pro"}}), "gemini/gemini-3-pro")
        self.assertEqual(fs.model_from_status({"inference": {}}), fs.UNAVAILABLE)
        self.assertEqual(fs.model_from_status({"inference": None}), fs.UNAVAILABLE)
        self.assertIsNone(fs.model_from_status({}))

    def test_a_label_value_holding_a_brace_keeps_its_sample(self):
        samples = fs.parse_prometheus('litellm_input_tokens_metric_total{model="a}b",team="x\\"y"} 5\nlitellm_input_tokens_metric_total{model="c"} 2\n')
        self.assertEqual(fs.family_total(samples, fs.TOKEN_FAMILIES["in"]), 7.0)
        self.assertEqual(samples[0].labels["model"], "a}b")

    def test_non_finite_samples_are_skipped(self):
        samples = fs.parse_prometheus("litellm_input_tokens_metric_total{m=\"a\"} NaN\nlitellm_input_tokens_metric_total{m=\"b\"} +Inf\nlitellm_input_tokens_metric_total{m=\"c\"} 5\n")
        self.assertEqual(fs.family_total(samples, fs.TOKEN_FAMILIES["in"]), 5.0)
        self.assertIsNone(fs.model_from_litellm_config("model_list: []\n"))
        self.assertIsNone(fs.model_from_litellm_config(None))


class CollectTest(unittest.TestCase):
    """The collector runs kubectl; every optional source failing leaves the CR-only summary."""

    def fake_run(self, outputs):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            key = " ".join(args)
            for needle, result in outputs.items():
                if needle in key:
                    if isinstance(result, Exception):
                        raise result
                    if isinstance(result, subprocess.CompletedProcess):
                        return result
                    return subprocess.CompletedProcess(args, 0, stdout=result, stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")

        return run, calls

    def test_the_cr_is_required_and_the_rest_is_optional(self):
        run, calls = self.fake_run({
            "get platformagent": json.dumps(CR),
            "version -o json": json.dumps(SERVER_VERSION),
            "get configmap": json.dumps({"items": [
                {"metadata": {"name": "other"}, "data": {"config.yaml": "model: x/y\n"}},
                {"metadata": {"name": "litellm-config-a1b2stale", "creationTimestamp": "2026-10-01T00:00:00Z"}, "data": {"config.yaml": "model: vertex_ai/old-model\n"}},
                {"metadata": {"name": "litellm-config-k4b9ht22c7", "creationTimestamp": "2026-10-10T00:00:00Z"}, "data": {"config.yaml": LITELLM_CONFIG_YAML}},
            ]}),
            "get deployment litellm": json.dumps({"spec": {"template": {"spec": {"volumes": [{"name": "cfg", "configMap": {"name": "litellm-config-k4b9ht22c7"}}]}}}}),
            "get pods -n": json.dumps({"items": [
                {"metadata": {"name": "other-agent-gateway-y"}, "status": {"phase": "Running"}},
                {"metadata": {"name": "platform-agent-gateway-old", "deletionTimestamp": "2026-10-20T17:00:00Z"}, "status": {"phase": "Running"}},
                {"metadata": {"name": "platform-agent-gateway-x"}, "status": {"phase": "Running"}},
                {"metadata": {"name": "platform-agent-credential-proxy-z"}, "status": {"phase": "Running"}},
            ]}),
            "http://127.0.0.1:8766/metrics": BROKER_METRICS,
            "get namespaces -o name": "namespace/ns-a\nnamespace/ns-b\n",
            "get deployments.apps --all-namespaces -o name": "deployment.apps/dep-a\n",
            "get pods --all-namespaces -o name": "pod/p1\npod/p2\npod/p3\n",
            "get nodes -o name": "node/n1\n",
            "get customresourcedefinitions.apiextensions.k8s.io -o name": (
                "customresourcedefinition.apiextensions.k8s.io/sqlinstances.sql.cnrm.cloud.google.com\n"
                "customresourcedefinition.apiextensions.k8s.io/platformagents.kubeagents.dev\n"
            ),
            "/v1/tasks": json.dumps({"tasks": TASKS, "truncated": True, "board": True}),
            "http://inference-gateway/metrics/": LITELLM_METRICS,
        })
        with mock.patch.object(fs.subprocess, "run", run):
            snapshot = fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
        self.assertEqual(snapshot.sources, {"session-kv": True, "litellm-metrics": True, "broker-metrics": True, "scale": True})
        text = fs.render(snapshot)
        self.assertIn("tasks (14 days): 7", text)
        self.assertIn("model: vertex_ai/claude-opus-5", text)
        self.assertIn("scale: namespaces 2, deployments 1, pods 3, nodes 1, crds 2 (kcc 1)", text)
        self.assertIn("tools (top 5, since broker start):", text)
        self.assertIn("  kubectl/get: 14900 (success 14500, error 400)", text)
        self.assertEqual(fs.audit(text, [CLUSTER, PROJECT, NAMESPACE]), [])
        self.assertTrue(snapshot.tasks_truncated)
        self.assertIn("tasks (14 days): 7+ (", text)
        gateway_exec_calls = [c for c in calls if "exec" in c and "platform-agent-gateway-x" in c]
        self.assertEqual(len(gateway_exec_calls), 2)
        for c in gateway_exec_calls:
            self.assertEqual(c[:4], ["kubectl", "exec", "-n", NAMESPACE])
            self.assertEqual(c[4], "platform-agent-gateway-x")
            self.assertEqual(c[5:9], ["-c", "platform-agent", "--", "sh"])
        feed_shell = gateway_exec_calls[0][-1]
        self.assertIn("curl -sSf ", feed_shell)
        self.assertIn("curl -sSfL ", gateway_exec_calls[1][-1])
        self.assertIn('-H "X-API-Key: $SESSION_KV_API_KEY"', feed_shell)
        self.assertIn("http://127.0.0.1:8699/v1/tasks?since=", feed_shell)
        self.assertIn("http://inference-gateway/metrics/", gateway_exec_calls[1][-1])
        broker_exec_calls = [c for c in calls if "exec" in c and "platform-agent-credential-proxy-z" in c]
        self.assertEqual(len(broker_exec_calls), 1)
        self.assertEqual(broker_exec_calls[0][:9], ["kubectl", "exec", "-n", NAMESPACE, "platform-agent-credential-proxy-z", "-c", "envoy-credential-proxy", "--", "sh"])
        self.assertIn("http://127.0.0.1:8766/metrics", broker_exec_calls[0][-1])
        self.assertTrue(all(c[0] == "kubectl" for c in calls))

    def test_without_the_cr_the_collector_fails_loudly(self):
        run, _ = self.fake_run({})
        with mock.patch.object(fs.subprocess, "run", run):
            with self.assertRaises(fs.CollectError) as ctx:
                fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
        self.assertIn("platformagent", str(ctx.exception))

    def test_the_configmap_is_listed_only_when_the_status_has_no_inference_key(self):
        """The ConfigMap is the fallback for an older operator or CRD; with the key
        present the namespace listing would be fetched and discarded."""
        with_key = json.loads(json.dumps(CR))
        with_key.setdefault("status", {})["inference"] = {"provider": "vertex_ai", "model": "claude-opus-5"}
        without_key = json.loads(json.dumps(CR))
        without_key.get("status", {}).pop("inference", None)
        for cr, expected_calls in ((with_key, 0), (without_key, 1)):
            run, _ = self.fake_run({"get platformagent": json.dumps(cr)})
            with mock.patch.object(fs.subprocess, "run", run), mock.patch.object(fs, "_litellm_config", return_value=None) as listing:
                fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
            self.assertEqual(listing.call_count, expected_calls, cr.get("status", {}).keys())

    def test_optional_sources_failing_leave_unavailable_lines(self):
        run, _ = self.fake_run({
            "get platformagent": json.dumps(CR),
            "get pods": subprocess.TimeoutExpired(cmd="kubectl", timeout=1),
        })
        with mock.patch.object(fs.subprocess, "run", run):
            snapshot = fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
        self.assertEqual(snapshot.sources, {"session-kv": False, "litellm-metrics": False, "broker-metrics": False, "scale": False})
        text = fs.render(snapshot)
        self.assertIn("tasks (14 days): unavailable", text)
        self.assertIn("tokens: unavailable", text)
        self.assertIn("tools (top 5, since broker start): unavailable", text)
        self.assertEqual(
            snapshot.source_reasons,
            {
                "session-kv": fs.REASON_NO_POD,
                "litellm-metrics": fs.REASON_NO_POD,
                "broker-metrics": fs.REASON_NO_BROKER_POD,
                "scale": fs.REASON_EXEC,
            },
        )
        err = io.StringIO()
        with mock.patch.object(fs, "collect", return_value=snapshot):
            self.assertEqual(fs.main(["--namespace", NAMESPACE], stdout=io.StringIO(), stderr=err), 0)
        self.assertIn("could not be read", err.getvalue())
        self.assertIn(fs.REASON_NO_POD, err.getvalue())
        self.assertIn(fs.REASON_NO_BROKER_POD, err.getvalue())
        self.assertNotIn("pods/exec", err.getvalue())

    def test_an_error_body_from_the_feed_is_not_a_task_list(self):
        run, _ = self.fake_run({
            "get platformagent": json.dumps(CR),
            "get pods": json.dumps({"items": [{"metadata": {"name": "platform-agent-gateway-x"}, "status": {"phase": "Running"}}]}),
            "/v1/tasks": json.dumps({"detail": "board locked"}),
            "/metrics": subprocess.CompletedProcess(["kubectl"], 22, stdout="", stderr="curl: (22) 404"),
        })
        with mock.patch.object(fs.subprocess, "run", run):
            snapshot = fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
        self.assertIsNone(snapshot.tasks)
        self.assertEqual(snapshot.source_reasons["session-kv"], fs.REASON_SHAPE)
        self.assertEqual(snapshot.source_reasons["litellm-metrics"], fs.REASON_EXEC)

    def test_a_feed_that_is_not_the_feed_reads_as_unavailable(self):
        self.assertEqual(fs._task_feed("[1, 2]"), (None, False))
        self.assertEqual(fs._task_feed('"text"'), (None, False))
        self.assertEqual(fs._task_feed("null"), (None, False))
        self.assertEqual(fs._task_feed('{"tasks": "no"}'), (None, False))
        self.assertEqual(fs._task_feed('{"tasks": [1, {"id": "t"}]}'), (None, False))
        rows, truncated = fs._task_feed('{"tasks": [{"id": "t", "status": "done"}], "truncated": true}')
        self.assertEqual(rows, [{"id": "t", "status": "done"}])
        self.assertTrue(truncated)
        run, _ = self.fake_run({
            "get platformagent": json.dumps(CR),
            "get pods": json.dumps({"items": [{"metadata": {"name": "platform-agent-gateway-x"}, "status": {"phase": "Running"}}]}),
            "/v1/tasks": "[1, 2]",
        })
        with mock.patch.object(fs.subprocess, "run", run):
            snapshot = fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW))
        self.assertIsNone(snapshot.tasks)
        self.assertEqual(snapshot.source_reasons["session-kv"], fs.REASON_SHAPE)
        self.assertIn("tasks (14 days): unavailable", fs.render(snapshot))

    def test_a_stale_hashed_config_map_loses_to_the_mounted_one(self):
        # The ConfigMap is read only for a status with no ``inference`` key.
        legacy_cr = json.loads(json.dumps(CR))
        legacy_cr.get("status", {}).pop("inference", None)
        run, _ = self.fake_run({
            "get platformagent": json.dumps(legacy_cr),
            "get configmap": json.dumps({"items": [
                {"metadata": {"name": "litellm-config-a1b2stale", "creationTimestamp": "2026-10-11T00:00:00Z"}, "data": {"config.yaml": "model: vertex_ai/old-model\n"}},
                {"metadata": {"name": "litellm-config-k4b9ht22c7", "creationTimestamp": "2026-10-10T00:00:00Z"}, "data": {"config.yaml": LITELLM_CONFIG_YAML}},
            ]}),
            "get deployment litellm": json.dumps({"spec": {"template": {"spec": {"volumes": [{"name": "cfg", "configMap": {"name": "litellm-config-k4b9ht22c7"}}]}}}}),
        })
        with mock.patch.object(fs.subprocess, "run", run):
            snapshot = fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW, in_pod=False))
        self.assertEqual(fs.model_from_litellm_config(snapshot.litellm_config), "vertex_ai/claude-opus-5")
        run, _ = self.fake_run({
            "get platformagent": json.dumps(legacy_cr),
            "get configmap": json.dumps({"items": [
                {"metadata": {"name": "litellm-config-a1b2stale", "creationTimestamp": "2026-10-01T00:00:00Z"}, "data": {"config.yaml": "model: vertex_ai/old-model\n"}},
                {"metadata": {"name": "litellm-config-k4b9ht22c7", "creationTimestamp": "2026-10-10T00:00:00Z"}, "data": {"config.yaml": LITELLM_CONFIG_YAML}},
            ]}),
        })
        with mock.patch.object(fs.subprocess, "run", run):
            snapshot = fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW, in_pod=False))
        self.assertEqual(fs.model_from_litellm_config(snapshot.litellm_config), "vertex_ai/claude-opus-5")

    def test_no_exec_renders_the_status_top_tools(self):
        rows, lines = top_tools_fixture()
        with mock.patch.object(fs, "_kubectl", side_effect=lambda options, *args, **kw: json.dumps(cr_with_top_tools(rows)) if args[:2] == ("get", "platformagent") else json.dumps(SERVER_VERSION)):
            snapshot = fs.collect(fs.Options(in_pod=False, now=NOW))
        self.assertIsNone(snapshot.broker_metrics)
        self.assertIn("\n".join(lines), fs.render(snapshot))

    def test_no_exec_skips_the_in_pod_sources(self):
        run, calls = self.fake_run({"get platformagent": json.dumps(CR)})
        with mock.patch.object(fs.subprocess, "run", run):
            fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW, in_pod=False))
        self.assertFalse(any("exec" in c for c in calls))
        self.assertFalse(any("get pods" in " ".join(c) for c in calls))

    def test_context_and_kubeconfig_reach_every_kubectl_call(self):
        run, calls = self.fake_run({"get platformagent": json.dumps(CR)})
        with mock.patch.object(fs.subprocess, "run", run):
            fs.collect(fs.Options(namespace=NAMESPACE, name="platform-agent", now=NOW,
                                  context="ctx", kubeconfig="/tmp/kc", in_pod=False))
        for c in calls:
            self.assertIn("--context", c)
            self.assertIn("--kubeconfig", c)


class MainTest(unittest.TestCase):
    def test_main_prints_the_block_and_refuses_a_leaked_identifier(self):
        snapshot = full_snapshot()
        out = io.StringIO()
        with mock.patch.object(fs, "collect", return_value=snapshot):
            code = fs.main(["--namespace", NAMESPACE], stdout=out)
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("```"))
        leaked = mock.patch.object(fs, "render", return_value=f"```\noops {CLUSTER}\n```")
        err = io.StringIO()
        with mock.patch.object(fs, "collect", return_value=snapshot), leaked:
            code = fs.main(["--namespace", NAMESPACE], stdout=io.StringIO(), stderr=err)
        self.assertEqual(code, fs.EXIT_LEAK)
        self.assertIn("refused", err.getvalue())

    def test_a_model_segment_equal_to_the_cluster_name_is_not_refused(self):
        cr = json.loads(json.dumps(CR))
        cr["status"]["inference"] = {"provider": "hosted_vllm", "model": f"{CLUSTER}/llama-8b"}
        snapshot = full_snapshot(cr=cr)
        text = fs.render(snapshot)
        self.assertIn(f"\n  model: hosted_vllm/{CLUSTER}/llama-8b\n", text)
        self.assertEqual(fs.audit(text, snapshot.identifiers), [CLUSTER])
        self.assertEqual(fs.audit(fs._without_model_line(text), snapshot.identifiers), [])
        out = io.StringIO()
        with mock.patch.object(fs, "collect", return_value=snapshot):
            self.assertEqual(fs.main(["--namespace", NAMESPACE], stdout=out, stderr=io.StringIO()), fs.EXIT_OK)
        self.assertIn(f"hosted_vllm/{CLUSTER}/llama-8b", out.getvalue())

    def test_the_audit_skips_the_model_line_and_nothing_else(self):
        text = f"```\n  mode: today\n  model: x/{CLUSTER}\n  kubernetes: {CLUSTER}\n```"
        stripped = fs._without_model_line(text)
        self.assertNotIn("model:", stripped)
        self.assertIn(f"  kubernetes: {CLUSTER}", stripped)
        self.assertEqual(len(stripped.splitlines()), len(text.splitlines()) - 1)
        snapshot = full_snapshot()
        err = io.StringIO()
        with mock.patch.object(fs, "collect", return_value=snapshot), mock.patch.object(fs, "render", return_value=text):
            self.assertEqual(fs.main(["--namespace", NAMESPACE], stdout=io.StringIO(), stderr=err), fs.EXIT_LEAK)



class GatewayCopyTest(unittest.TestCase):
    """The gateway's Go copy of the field lists is this script's, verbatim."""

    GO_FILE = Path(__file__).resolve().parents[3] / "a2a" / "gateway" / "feedback_summary.go"

    def _go_strings(self, var: str) -> list[str]:
        source = self.GO_FILE.read_text()
        start = source.index(f"var {var} = ")
        body = source[start : source.index("}", start)]
        return re.findall(r'"([A-Za-z0-9]+)"', body)

    def test_the_gateway_prints_the_same_counters_and_readings(self):
        self.assertEqual(list(fs.USAGE_COUNTERS), self._go_strings("usageSummaryCounters"))
        self.assertEqual(sorted(fs.USAGE_READINGS), sorted(self._go_strings("usageSummaryReadings")))

    def test_the_gateway_prints_the_same_environment_lines(self):
        self.assertEqual(list(fs.ENVIRONMENT_KEYS), self._go_strings("usageSummaryEnvironmentKeys"))

    def test_the_gateway_names_the_same_source_for_each_reading(self):
        source = self.GO_FILE.read_text()
        start = source.index("var usageSummaryReadings = ")
        body = source[start : source.index("}", start)]
        go_gateway = sorted(re.findall(r'"([A-Za-z0-9]+)":\s*usageGatewayReadingSuffix', body))
        py_gateway = sorted(k for k, v in fs.USAGE_READINGS.items() if v == fs.USAGE_GATEWAY_READING_SUFFIX)
        self.assertEqual(py_gateway, ["peopleSeen30d", "peopleSeen7d"])
        self.assertEqual(go_gateway, py_gateway)

    def _go_subcommands(self) -> dict:
        source = self.GO_FILE.read_text()
        start = source.index("var usageSummaryBrokerSubcommands = map[string][]string{")
        body = source[start : source.index("\n}\n", start)]
        return {tool: frozenset(re.findall(r'"([^"]+)"', subs)) for tool, subs in re.findall(r'"(\w+)":\s*\{([^}]*)\}', body)}

    def test_the_gateway_carries_the_same_broker_vocabulary(self):
        self.assertEqual(self._go_subcommands(), dict(fs.BROKER_SUBCOMMANDS))
        self.assertEqual(self._go_strings("usageSummaryToolStatusOrder"), list(fs.TOOL_STATUS_ORDER))

    def test_the_gateway_reads_the_same_status_rows(self):
        source = self.GO_FILE.read_text()
        for go, want in (
            ("usageTopToolsKey", fs.TOP_TOOLS_STATUS_KEY),
            ("usageToolField", fs.TOP_TOOLS_TOOL_FIELD),
            ("usageToolSubcommandField", fs.TOP_TOOLS_SUBCOMMAND_FIELD),
            ("usageToolTotalField", fs.TOP_TOOLS_TOTAL_FIELD),
            ("usageToolSubcommandNone", fs.NONE),
            ("usageToolOther", fs.OTHER_BUCKET),
            ("usageToolPairSeparator", fs.TOOL_PAIR_SEPARATOR),
            ("usageTopToolsLabel", fs.TOP_TOOLS_LABEL),
        ):
            match = re.search(rf'^\s*{go}\s*=\s*"([^"]*)"', source, re.M)
            self.assertIsNotNone(match, go)
            self.assertEqual(match.group(1), want, go)
        shown = re.search(r"^\s*usageTopToolsShown\s*=\s*(\d+)", source, re.M)
        self.assertEqual(int(shown.group(1)), fs.TOP_TOOLS_SHOWN)

    def test_broker_subcommands_match_credential_proxy_vocabulary(self):
        import credential_proxy as cp

        for tool in ("kubectl", "gcloud", "git", "gh"):
            self.assertEqual(fs.BROKER_SUBCOMMANDS[tool], cp._subcommand_vocabulary(tool))
        self.assertEqual(fs.BROKER_SUBCOMMANDS["feedback"], frozenset({cp.FEEDBACK_AUDIT_SUBCOMMAND}))


if __name__ == "__main__":
    unittest.main()

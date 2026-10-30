"""The chart's health-cohort rules: one Managed Prometheus `Rules` object per
install, off by default, carrying the epic's At-Risk and Friction alerts over
the operator's gauges (k8s-operator/internal/controller/usage_gauges.go), with
every threshold and sample floor read from values.

Renders the chart with helm, so it needs the binary; skipped without it.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_RULES_API = "monitoring.googleapis.com/v1/Rules"
_PODMONITORING_API = "monitoring.googleapis.com/v1/PodMonitoring"
_GAUGES_GO = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "usage_gauges.go"
_ON = ["--set", "platformAgent.healthRules.enabled=true"]
_GAUGE_PREFIX = "kubeagents_platformagent_"
_GROUPING = "agent_namespace"
_AT_RISK = {
    "KubeAgentsAgentDegraded",
    "KubeAgentsClustersUnwatched",
    "KubeAgentsChatTurnsErroring",
    "KubeAgentsBrokerFailing",
    "KubeAgentsBackgroundTasksGivingUp",
}
_FRICTION = {
    "KubeAgentsStalledOnboarding",
    "KubeAgentsProposalsRejected",
    "KubeAgentsProposalsUnmerged",
    "KubeAgentsCommandsBlocked",
    "KubeAgentsNegativeFeedback",
    "KubeAgentsScrapeBroken",
}
_SCRAPE_ALERT = "KubeAgentsScrapeBroken"


def _render(*extra, api_versions=(_RULES_API, _PODMONITORING_API)):
    cmd = ["helm", "template", "rel", str(_CHART), *_REQUIRED, *extra]
    for api in api_versions:
        cmd += ["--api-versions", api]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _rules(docs):
    return [d for d in docs if d.get("kind") == "Rules"]


def _alerts(rules):
    return {r["alert"]: r for g in rules["spec"]["groups"] for r in g["rules"] if "alert" in r}


def _records(rules):
    return {r["record"]: r for g in rules["spec"]["groups"] for r in g["rules"] if "record" in r}


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class HealthRulesRenderTest(unittest.TestCase):
    def test_off_by_default_even_where_the_crd_is_served(self):
        self.assertEqual([], _rules(_render()))

    def test_on_renders_one_rules_object_named_for_the_agent(self):
        docs = _render(*_ON)
        rules = _rules(docs)
        self.assertEqual(1, len(rules))
        agent = next(d for d in docs if d.get("kind") == "PlatformAgent")
        self.assertEqual(f"{agent['metadata']['name']}-health-rules", rules[0]["metadata"]["name"])
        self.assertEqual(agent["metadata"]["namespace"], rules[0]["metadata"]["namespace"])

    def test_every_cohort_alert_is_present_with_its_severity(self):
        alerts = _alerts(_rules(_render(*_ON))[0])
        self.assertEqual(_AT_RISK | _FRICTION, set(alerts))
        for name in _AT_RISK:
            self.assertEqual("at_risk", alerts[name]["labels"]["severity"], name)
        for name in _FRICTION:
            self.assertEqual("friction", alerts[name]["labels"]["severity"], name)
        for name, rule in alerts.items():
            self.assertIn("summary", rule.get("annotations", {}), name)
            self.assertTrue(rule.get("for"), f"{name} fires on one evaluation; every alert has a for")

    def test_every_alert_reads_the_operators_gauges_per_agent(self):
        alerts = _alerts(_rules(_render(*_ON))[0])
        for name, rule in alerts.items():
            if name == _SCRAPE_ALERT:
                self.assertIn("up{", rule["expr"], name)
                continue
            self.assertIn(_GAUGE_PREFIX, rule["expr"], name)
            self.assertIn(_GROUPING, rule["expr"], f"{name} does not group by the agent's labels")

    def test_ratios_are_recorded_once_and_read_by_the_alerts(self):
        rules = _rules(_render(*_ON))[0]
        records = _records(rules)
        self.assertTrue(records, "no recording rules")
        for name in records:
            self.assertTrue(name.startswith("kubeagents:"), name)
        exprs = " ".join(r["expr"] for r in _alerts(rules).values())
        for name in records:
            self.assertIn(name, exprs, f"{name} is recorded but no alert reads it")

    def test_thresholds_and_floors_come_from_values(self):
        docs = _render(
            *_ON,
            # helm --set keeps a decimal as a string, which the schema refuses;
            # a values file or --set-json carries a ratio as a number.
            "--set-json", "platformAgent.healthRules.thresholds.chatTurnsErroredRatio=0.42",
            "--set", "platformAgent.healthRules.thresholds.chatTurnsErroredMinMessages=17",
            "--set", "platformAgent.healthRules.thresholds.degradedFor=36h",
            "--set", "platformAgent.healthRules.thresholds.negativeFeedbackPerDay=9",
        )
        alerts = _alerts(_rules(docs)[0])
        self.assertIn("0.42", alerts["KubeAgentsChatTurnsErroring"]["expr"])
        self.assertIn("17", alerts["KubeAgentsChatTurnsErroring"]["expr"])
        self.assertEqual("36h", alerts["KubeAgentsAgentDegraded"]["for"])
        self.assertIn("9", alerts["KubeAgentsNegativeFeedback"]["expr"])

    def test_the_scrape_alert_names_the_installs_scrape_jobs(self):
        docs = _render(*_ON)
        expr = _alerts(_rules(docs)[0])[_SCRAPE_ALERT]["expr"]
        agent = next(d for d in docs if d.get("kind") == "PlatformAgent")["metadata"]["name"]
        jobs = {
            d["metadata"]["name"]
            for d in docs
            if d.get("kind") == "PodMonitoring" and (d["metadata"]["name"].startswith(f"{agent}-") or d["metadata"]["name"].endswith("-operator-monitoring"))
        }
        self.assertTrue(jobs)
        for job in jobs:
            self.assertIn(job, expr, f"the scrape alert does not watch {job}")

    def test_vector_matching_names_the_agent_labels(self):
        """Under Managed Prometheus scoping a recorded series carries project, cluster
        and namespace labels the inline `max by` sides do not, so a bare `and` or
        `unless` matches nothing; every one names the agent labels with `on`."""
        for name, rule in _alerts(_rules(_render(*_ON))[0]).items():
            expr = rule["expr"]
            for op in (" and ", " unless "):
                for i in [j for j in range(len(expr)) if expr.startswith(op, j)]:
                    self.assertTrue(expr.startswith(f"{op.strip()} on ({_GROUPING}, agent)", i + 1), f"{name}: `{op.strip()}` without `on (agent labels)` at {i}")

    def test_increases_are_summed_and_instants_are_maxed(self):
        """An operator roll-over splits a window's increase across two pod series;
        `sum by` adds the disjoint fragments, `max by` would keep the larger one.
        An instantaneous read is `max by`, which folds the stale old-leader series."""
        rules = _rules(_render(*_ON))[0]
        exprs = [r["expr"] for g in rules["spec"]["groups"] for r in g["rules"]]
        for expr in exprs:
            for i in [j for j in range(len(expr)) if expr.startswith("(increase(", j)]:
                self.assertTrue(expr[: i + 1].endswith(f"sum by ({_GROUPING}, agent) ("), f"an increase not under sum by: ...{expr[max(0, i - 40):i + 30]}")
            self.assertNotIn(f"max by ({_GROUPING}, agent) (increase(", expr)
        # The instantaneous reads: the phase, the two cluster gauges and the times.
        instants = [f'{_GAUGE_PREFIX}phase{{', 'field="clustersRegistered"', 'field="clustersMonitored"', 'field="createdTime"', 'field="lastHumanActiveTime"']
        for expr in exprs:
            for needle in instants:
                for i in [j for j in range(len(expr)) if expr.startswith(needle, j)]:
                    head = expr[:i]
                    self.assertTrue(head.endswith(f"max by ({_GROUPING}, agent) (") or head.endswith(f"max by ({_GROUPING}, agent) ({_GAUGE_PREFIX}usage{{") or head.endswith(f"max by ({_GROUPING}, agent) ({_GAUGE_PREFIX}usage_time_seconds{{"), f"an instant read not under max by: ...{expr[max(0, i - 60):i + 30]}")

    def test_long_conditions_hold_in_for_not_in_a_window(self):
        """A subquery window fires on a young series (five minutes of Degraded after
        an upgrade reads as a whole day); a plain `for` delays at worst."""
        alerts = _alerts(_rules(_render(*_ON))[0])
        for name, window in (("KubeAgentsAgentDegraded", "24h"), ("KubeAgentsClustersUnwatched", "48h"), (_SCRAPE_ALERT, "48h")):
            self.assertEqual(window, alerts[name]["for"], name)
            self.assertNotIn("_over_time(", alerts[name]["expr"], name)
        self.assertNotIn("increase(", alerts["KubeAgentsStalledOnboarding"]["expr"], "the stalled rule reads status times, not an increase over a young series")
        self.assertIn('field="lastHumanActiveTime"', alerts["KubeAgentsStalledOnboarding"]["expr"])

    def test_the_stalled_rule_reads_silence_off_the_chat_counter_not_off_an_absent_time(self):
        """Under the served-CRD skew the operator holds lastHumanActiveTime back while
        createdTime stays, so "absent means nobody spoke" would fire for the fleet."""
        expr = _alerts(_rules(_render(*_ON))[0])["KubeAgentsStalledOnboarding"]["expr"]
        self.assertNotIn(" unless ", expr)
        self.assertIn('field="chatMessagesInboundTotal"}) == 0', expr)

    def test_enabling_the_rules_without_the_operator_fails_at_render(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            _render(*_ON, "--set", "operator.enabled=false")
        self.assertIn("operator", caught.exception.stderr)

    def test_durations_follow_the_prometheus_grammar(self):
        for bad in ("1h1h", "30m1h", "1d1w", ""):
            with self.subTest(bad=bad), self.assertRaises(subprocess.CalledProcessError):
                _render(*_ON, "--set", f"platformAgent.healthRules.thresholds.ratiosFor={bad}")

    def test_metric_names_and_fields_are_the_operators(self):
        """The template hand-copies the gauge families and field names; hold them to
        the operator's declarations in usage_gauges.go."""
        import re

        go = _GAUGES_GO.read_text()
        families = set(re.findall(r'usage(?:Time|Phase)?GaugeName\s*=\s*"([a-z_]+)"', go))
        fields = set(re.findall(r'\{name: "([A-Za-z]+)"', go)) | set(re.findall(r'^\s*\{"([a-zA-Z]+)", func', go, re.M)) | set(re.findall(r'usageExport[A-Za-z]+\s*=\s*"([a-zA-Z]+)"', go)) | set(re.findall(r'usageCreatedTimeField\s*=\s*"([a-zA-Z]+)"', go))
        self.assertTrue(families and fields)
        rules = _rules(_render(*_ON))[0]
        exprs = " ".join(r["expr"] for g in rules["spec"]["groups"] for r in g["rules"])
        for family in set(re.findall(r"(kubeagents_platformagent_[a-z_]+)\{", exprs)):
            self.assertIn(family, families, f"{family} is not a family usage_gauges.go declares")
        for field in set(re.findall(r'field="([A-Za-z]+)"', exprs)):
            self.assertIn(field, fields, f"field {field} is not one usage_gauges.go exports")

    def test_count_summaries_print_whole_numbers(self):
        alerts = _alerts(_rules(_render(*_ON))[0])
        for name in ("KubeAgentsBackgroundTasksGivingUp", "KubeAgentsNegativeFeedback"):
            self.assertIn('printf "%.0f"', alerts[name]["annotations"]["summary"], name)

    def test_enabling_the_rules_without_the_operator_scrape_fails_at_render(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            _render(*_ON, "--set", "operator.metrics.podMonitoring=false")
        self.assertIn("operator", caught.exception.stderr)

    def test_the_schema_requires_every_threshold_the_template_reads(self):
        """`--reuse-values` from an older release can hand the template a thresholds map
        with one key, which `helm template` cannot reproduce (it merges the chart's
        defaults); the schema's `required` list is the guard, held here to the whole
        block: every key values.yaml declares, and every key the template reads."""
        import json
        import re

        schema = json.loads((_CHART / "values.schema.json").read_text())
        thresholds = schema["properties"]["platformAgent"]["properties"]["healthRules"]["properties"]["thresholds"]
        declared = set(yaml.safe_load((_CHART / "values.yaml").read_text())["platformAgent"]["healthRules"]["thresholds"])
        read = set(re.findall(r"\$t\.([A-Za-z0-9]+)", (_CHART / "templates" / "platform-agent-health-rules.yaml").read_text()))
        self.assertEqual(declared, set(thresholds["properties"]), "schema properties differ from values.yaml")
        self.assertEqual(declared, set(thresholds["required"]), "every threshold is required when the rules are on")
        self.assertEqual(read, declared, "the template reads a threshold values.yaml does not declare, or declares one it never reads")

    def test_compound_durations_are_accepted(self):
        alerts = _alerts(_rules(_render(*_ON, "--set", "platformAgent.healthRules.thresholds.ratiosFor=1h30m"))[0])
        self.assertEqual("1h30m", alerts["KubeAgentsChatTurnsErroring"]["for"])


if __name__ == "__main__":
    unittest.main()

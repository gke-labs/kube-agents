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
_READING = "kubeagents:platformagent:usage_reading:max"


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


def _expand(expr, records):
    """The expression with every recorded series it reads, transitively, appended."""
    seen, out, todo = set(), [expr], [expr]
    while todo:
        cur = todo.pop()
        for name, rule in records.items():
            if name in cur and name not in seen:
                seen.add(name)
                out.append(rule["expr"])
                todo.append(rule["expr"])
    return " ".join(out)


def _reading_fields():
    """The six proposal readings' field names, as usage_gauges.go declares them."""
    import re

    block = _GAUGES_GO.read_text().split("// The six proposal readings' field names", 1)[1].split("// usageCreatedTimeField", 1)[0]
    return re.findall(r'usageExport[A-Za-z]+\s*=\s*"([a-zA-Z]+)"', block)


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
        """Directly, or through the recorded series it reads (a proposal window reads
        the reading's recorded max, which reads the gauge)."""
        rules = _rules(_render(*_ON))[0]
        alerts = _alerts(rules)
        records = _records(rules)
        for name, rule in alerts.items():
            if name == _SCRAPE_ALERT:
                self.assertIn("up{", rule["expr"], name)
                continue
            self.assertIn(_GAUGE_PREFIX, _expand(rule["expr"], records), name)
            self.assertIn(_GROUPING, rule["expr"], f"{name} does not group by the agent's labels")

    def test_ratios_are_recorded_once_and_read_by_the_alerts(self):
        rules = _rules(_render(*_ON))[0]
        records = _records(rules)
        self.assertTrue(records, "no recording rules")
        for name in records:
            self.assertTrue(name.startswith("kubeagents:"), name)
        exprs = " ".join(_expand(r["expr"], records) for r in _alerts(rules).values())
        for name in records:
            self.assertIn(name, exprs, f"{name} is recorded but no alert reads it, directly or through another record")

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

    def test_no_increase_remains_on_a_reading_field(self):
        """The proposal fields are forge readings that fall on a removal; `increase()`
        reads a fall as a counter reset and inflates the window."""
        exprs = [r["expr"] for g in _rules(_render(*_ON))[0]["spec"]["groups"] for r in g["rules"]]
        readings = _reading_fields()
        self.assertEqual(6, len(readings), readings)
        for expr in exprs:
            self.assertNotRegex(expr, r"increase\(kubeagents:platformagent:usage_reading:max", "increase() over the recorded reading series")
            for field in readings:
                self.assertNotRegex(expr, rf'increase\({_GAUGE_PREFIX}usage\{{field="{field}"\}}', f"increase() over the reading {field}")

    def test_deltas_and_resets_run_over_the_recorded_max_series_only(self):
        """A roll-over splits a reading across two pod series and a `delta()` has
        no aggregation of its own, so each window reads the `max by` recorded series."""
        rules = _rules(_render(*_ON))[0]
        records = _records(rules)
        self.assertEqual(
            f'max by ({_GROUPING}, agent, field) ({_GAUGE_PREFIX}usage{{field=~"remediationsMergedTotal|remediationsClosedUnmergedTotal|proposalsAuthored"}})',
            records[_READING]["expr"],
        )
        exprs = [r["expr"] for g in rules["spec"]["groups"] for r in g["rules"]]
        windows = 0
        for expr in exprs:
            for fn in ("delta(", "resets("):
                for i in [j for j in range(len(expr)) if expr.startswith(fn, j) and (j == 0 or not expr[j - 1].isalnum() and expr[j - 1] != "_")]:
                    windows += 1
                    self.assertTrue(expr.startswith(f"{fn}{_READING}{{", i), f"{fn} not over the recorded max series: ...{expr[i:i + 80]}")
            self.assertNotIn(f"clamp_min(delta({_GAUGE_PREFIX}", expr)
        self.assertEqual(4, windows, "three window deltas and one fall guard")
        # Each window is folded to the agent pair: `a / on(...) b` keeps the
        # left side's labels, so a `field` left on a window would ride onto
        # both ratios and both alerts as the name of one input.
        for name, field in (("merged", "remediationsMergedTotal"), ("closed", "remediationsClosedUnmergedTotal"), ("authored", "proposalsAuthored")):
            self.assertEqual(f'max by ({_GROUPING}, agent) (clamp_min(delta({_READING}{{field="{field}"}}[14d]), 0))', records[f"kubeagents:platformagent:proposals_{name}_14d"]["expr"], name)
        windows_14d = {name: rule["expr"] for name, rule in records.items() if name.endswith("_14d") and name.split(":")[-1].split("_")[1] in ("merged", "closed", "authored", "fell")}
        self.assertEqual(4, len(windows_14d), sorted(windows_14d))
        for name, expr in windows_14d.items():
            self.assertTrue(expr.startswith(f"max by ({_GROUPING}, agent) (") and expr.endswith(")"), f"{name} is not folded to the agent pair: {expr}")

    def test_the_merge_rate_divides_by_authored_and_the_unmerged_guard_reads_authored(self):
        """One source only: the merge rate divides a reading by a reading, and the
        guard's threshold keeps its name and its meaning of proposals opened."""
        rules = _rules(_render(*_ON, "--set", "platformAgent.healthRules.thresholds.proposalsProposedMin14d=7"))[0]
        records = _records(rules)
        on = f"on ({_GROUPING}, agent)"
        self.assertEqual(
            f"kubeagents:platformagent:proposals_merged_14d / {on} kubeagents:platformagent:proposals_authored_14d unless {on} kubeagents:platformagent:proposals_fell_14d",
            records["kubeagents:platformagent:proposals_merge_rate_14d"]["expr"],
        )
        self.assertEqual(
            f"kubeagents:platformagent:proposals_closed_14d / {on} (kubeagents:platformagent:proposals_closed_14d + {on} kubeagents:platformagent:proposals_merged_14d) unless {on} kubeagents:platformagent:proposals_fell_14d",
            records["kubeagents:platformagent:proposals_rejected_ratio_14d"]["expr"],
        )
        alerts = _alerts(rules)
        self.assertIn(f"and {on} kubeagents:platformagent:proposals_authored_14d >= 7", alerts["KubeAgentsProposalsUnmerged"]["expr"])
        rejected = alerts["KubeAgentsProposalsRejected"]["expr"]
        self.assertIn(f"and {on} kubeagents:platformagent:proposals_closed_14d >= 3", rejected)
        self.assertIn(f"and {on} (kubeagents:platformagent:proposals_closed_14d + {on} kubeagents:platformagent:proposals_merged_14d) >= 4", rejected)
        for name in ("KubeAgentsProposalsRejected", "KubeAgentsProposalsUnmerged"):
            self.assertNotIn("remediationsProposedTotal", alerts[name]["expr"], name)
            self.assertNotIn("increase(", alerts[name]["expr"], name)

    # The fall guard's evaluation cannot run in CI: neither the host nor the CI
    # image carries promtool, so these tests hold the rule's rendered text. Its
    # behaviour on a reopen and a removal is verified live, against Managed
    # Prometheus's recorded series, after an upgrade.
    def test_a_reopen_does_not_trip_the_fall_guard_and_a_removal_does(self):
        """A reopen moves closed and open but not their sum, proposalsAuthored; a
        removal or a deletion lowers it. So the guard reads that series alone, and
        with `resets()`, which sees a fall a later rise nets out of a `delta()`."""
        rules = _rules(_render(*_ON))[0]
        records = _records(rules)
        self.assertEqual(f'max by ({_GROUPING}, agent) (resets({_READING}{{field="proposalsAuthored"}}[14d]) > 0)', records["kubeagents:platformagent:proposals_fell_14d"]["expr"])
        exprs = " ".join(r["expr"] for g in rules["spec"]["groups"] for r in g["rules"])
        self.assertEqual(1, exprs.count("resets("), "the fall guard watches proposalsAuthored and no other field")

    def test_compound_durations_are_accepted(self):
        alerts = _alerts(_rules(_render(*_ON, "--set", "platformAgent.healthRules.thresholds.ratiosFor=1h30m"))[0])
        self.assertEqual("1h30m", alerts["KubeAgentsChatTurnsErroring"]["for"])


if __name__ == "__main__":
    unittest.main()

"""Pins the topology-drift skill to the tool it reads.

    python3 -m unittest tests/test_topology_drift_skill.py

The skill teaches an agent to read `leeway report` (`leeway.report/v1alpha1`)
and attribute zonal skew from it. Its first version taught field values leeway
never prints, because it was written from leeway's prose docs and from the
eval fixture rather than from a report (#3419, review rounds 1-4). These tests
hold the skill to a captured report and to leeway's wire vocabulary, so the
next drift fails here rather than in a nightly run that greps for strings
nobody emits.

`references/report-example.json` in each copy of the skill is a real document
from leeway v0.1.3's own report collector (its `TestCollect_SeededD` fixture
plus three more clusters), with the bench fixture names replaced. Regenerate
it from leeway when the schema moves, and update VOCABULARY below from
`pkg/leeway/{cause,intent,remediation,finding}.go` in the same change.
"""

import json
import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS = {
    profile: REPO_ROOT / "agents" / profile / "skills" / "topology-drift"
    for profile in ("cluster", "platform")
}
SHARED_START = "<!-- shared:reading-start -->"
SHARED_END = "<!-- shared:reading-end -->"
FLEET_FIXTURES = REPO_ROOT / "docs/designs/fleet-fixtures.yaml"
SANDBOX_DOCKERFILE = REPO_ROOT / "deploy/sandbox/Dockerfile"
LEEWAY_VERSION = "v0.1.3"

# leeway v0.1.3 (go-steer/leeway@4366049), pkg/leeway: the strings a report
# carries in these fields. The kind inventory is frozen by leeway's schema;
# the rest changes only with a leeway release.
VOCABULARY = {
    "kind": {
        "leeway.contract_violated",
        "leeway.placement_drift",
        "leeway.baseline_breach",
        "leeway.domain_unavailable",
    },
    "suspectedCause": {
        "constraint_ignored",
        "volume_pinning",
        "domain_capacity_shortfall",
        "taint_exclusion",
        "domain_outage",
        "consolidation",
        "rollout_bias",
        "unknown",
    },
    "intent.source": {
        "policy-crd",
        "workload-annotation",
        "topology-spread-constraint",
        "pod-anti-affinity-required",
        "pod-affinity-required",
        "cluster-default-declared",
        "cluster-default-assumed",
        "pod-anti-affinity-preferred",
        "pod-affinity-preferred",
        "learned-baseline",
    },
    "intent.confidence": {"declared", "inferred", "assumed", "learned"},
    "remediation.kind": {
        "manifest_patch",
        "rollout_restart",
        "cluster_capacity",
        "cluster_nodes",
        "storage_topology",
    },
}

# The values the first version of the skill invented. None is a leeway string;
# a skill that names one sends the agent looking for a line that never comes.
NEVER_EMITTED = (
    "schedule_anyway_soft",
    "affinity_conflict",
    "taint_blocked",
    "resource_starvation",
    "volume_topology_locked",
    "node_count_asymmetry",
    "daemonset_bound",
    "storage_migration",
    "capacity_action",
    '"cluster_default"',
    'source` is `"none"',
)


def _skill_text(profile):
    return (SKILLS[profile] / "SKILL.md").read_text(encoding="utf-8")


def _shared(text):
    start = text.index(SHARED_START) + len(SHARED_START)
    end = text.index(SHARED_END)
    return text[start:end]


def _example(profile):
    return json.loads((SKILLS[profile] / "references/report-example.json").read_text(encoding="utf-8"))


def _findings(report):
    for cluster in report["clusters"]:
        yield from cluster.get("topology_findings") or []


class ExampleReportTest(unittest.TestCase):
    def test_vocabulary_matches_the_leeway_version_pinned_in_the_sandbox_dockerfile(self):
        dockerfile = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
        match = re.search(r"ARG LEEWAY_VERSION=([0-9.]+)", dockerfile)
        if match:
            version = "v" + match.group(1)
        else:
            match = re.search(r"go-steer/leeway/releases/download/(v[0-9.]+)/", dockerfile)
            version = match.group(1) if match else None
        self.assertIsNotNone(version, "deploy/sandbox/Dockerfile should pin a go-steer/leeway release version")
        self.assertEqual(version, LEEWAY_VERSION)

    def test_both_copies_carry_the_same_report(self):
        cluster = (SKILLS["cluster"] / "references/report-example.json").read_bytes()
        platform = (SKILLS["platform"] / "references/report-example.json").read_bytes()
        self.assertEqual(cluster, platform)

    def test_the_report_is_the_schema_the_skill_names(self):
        report = _example("cluster")
        self.assertEqual(report["schema_version"], "leeway.report/v1alpha1")
        self.assertEqual(
            set(report["summary"]),
            {"clusters_total", "clusters_succeeded", "clusters_failed", "topology_findings", "rank_findings", "by_severity", "by_tier"},
        )
        statuses = {c["status"] for c in report["clusters"]}
        self.assertEqual(statuses, {"ok", "error"}, "the example should show a read cluster and an unreadable one")

    def test_every_value_in_the_report_is_in_the_vocabulary(self):
        for finding in _findings(_example("cluster")):
            self.assertIn(finding["kind"], VOCABULARY["kind"])
            self.assertIn(finding["suspectedCause"], VOCABULARY["suspectedCause"])
            for cause in finding.get("untestedCauses") or []:
                self.assertIn(cause, VOCABULARY["suspectedCause"])
            if "intent" in finding:
                self.assertIn(finding["intent"]["source"], VOCABULARY["intent.source"])
                self.assertIn(finding["intent"]["confidence"], VOCABULARY["intent.confidence"])
            if "remediation" in finding:
                self.assertIn(finding["remediation"]["kind"], VOCABULARY["remediation.kind"])

    def test_the_report_exercises_every_row_the_skill_can_point_at(self):
        # Rows 1, 3, 4, 5 and 6 of the attribution table have a finding each;
        # row 8 is the workload with no finding, which the prose names.
        seen = {(f["suspectedCause"], (f.get("remediation") or {}).get("kind")) for f in _findings(_example("cluster"))}
        for pair in (
            ("constraint_ignored", "manifest_patch"),
            ("unknown", "manifest_patch"),
            ("volume_pinning", "storage_topology"),
            ("domain_capacity_shortfall", "cluster_capacity"),
            ("taint_exclusion", "cluster_nodes"),
        ):
            self.assertIn(pair, seen)


class SharedReadingSectionTest(unittest.TestCase):
    def test_both_copies_share_the_reading_section_byte_for_byte(self):
        self.assertEqual(_shared(_skill_text("cluster")), _shared(_skill_text("platform")))

    def test_the_section_names_every_vocabulary_value(self):
        shared = _shared(_skill_text("cluster"))
        for field, values in VOCABULARY.items():
            for value in values:
                self.assertIn(f"`{value}`", shared, f"{field} value {value!r} missing from the reading section")

    def test_the_section_names_nothing_leeway_never_prints(self):
        for profile in SKILLS:
            text = _skill_text(profile)
            for value in NEVER_EMITTED:
                self.assertNotIn(value, text, f"{profile} skill names {value!r}")

    def test_the_worked_example_reads_the_shipped_report(self):
        # The example walks the first cluster's three findings by name and
        # value; those names and values are the report's, not prose.
        report = _example("cluster")
        first = report["clusters"][0]
        shared = _shared(_skill_text("cluster"))
        self.assertIn(f"`{first['cluster']}`", shared)
        for finding in first["topology_findings"]:
            subject = finding["subject"]
            self.assertIn(f"`{subject['kind']}/{subject['namespace']}/{subject['name']}`", shared)
            self.assertIn(f"`suspectedCause: {finding['suspectedCause']}`", shared)
            self.assertIn(f"`remediation.kind: {finding['remediation']['kind']}`", shared)


class NoFixtureNamesTest(unittest.TestCase):
    """The eval's planted objects never appear in the skill (#3419 round 4).

    A skill that names the fixture is the eval's answer key: a green run then
    shows recall, not attribution. The names come from the fleet catalogue so
    a fixture added later is covered without editing this test.
    """

    def test_no_planted_object_name_or_namespace_appears_in_the_skill(self):
        catalogue = yaml.safe_load(FLEET_FIXTURES.read_text(encoding="utf-8"))
        # Names every cluster has, which the catalogue lists as fixture objects
        # but which say nothing about the eval: a skill may name them.
        generic = {"default", "self", "default-pool"}
        names = set()
        for fixture in catalogue["fixtures"]:
            for obj in fixture.get("objects") or []:
                names.add(obj.rsplit("/", 1)[-1])
            if fixture.get("namespace"):
                names.add(fixture["namespace"])
        names.update({"seeded-a", "seeded-b", "seeded-c", "seeded-d"})
        names -= generic
        self.assertGreater(len(names), 20, "the catalogue parse found too few names to be a real check")
        for profile, directory in SKILLS.items():
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                for name in sorted(names):
                    self.assertFalse(
                        re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text),
                        f"{path.relative_to(REPO_ROOT)} names the fixture {name!r}",
                    )


if __name__ == "__main__":
    unittest.main()

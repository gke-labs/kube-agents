#!/usr/bin/env python3
"""Unit tests for readiness_rules/rollout_sequence_precheck.py, the plan-level rule: on canned
cluster records carrying a `fleet` block, and on a RolloutSequence list in the shape
`gcloud container fleet rolloutsequences list --format=json` prints
(testdata/rollout_sequences_list.json: one sequence of two stages, the first with a label
selector and a seven-day soak, the second with neither)."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fleet_upgrade_report as report  # noqa: E402
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as r  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from readiness_rules import rollout_sequence_precheck as rule  # noqa: E402
from test_readiness_rules import AT, CURRENT, LAGGING, TARGET, cluster, pool, read  # noqa: E402

RECORDED_LIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "rollout_sequences_list.json")
HOST = "acme-fleet-host"
OTHER_HOST = "acme-fleet-host-2"
PROJECT = "acme-prod"
OTHER_PROJECT = "acme-staging"
SELF_LINK_FORMAT = "https://container.googleapis.com/v1/projects/{project}/locations/{location}/clusters/{name}"
MEMBERSHIP_FORMAT = "projects/123456789012/locations/global/memberships/{name}"
THE_ONE_ACTION = "add the train as stages of prod-train, or plan it in a separate host project"
EXPECTED_COMMAND = ["gcloud", "container", "fleet", "rolloutsequences", "list", f"--project={HOST}", "--format=json"]
EMPTY_LIST = "[]"


def recorded_list() -> str:
    with open(RECORDED_LIST_PATH, encoding="utf-8") as f:
        return f.read()


def member_record(name, location="us-central1-a", project=PROJECT, host=HOST, **extra):
    """A listed cluster record of a fleet member: the `fleet` block names the host project."""
    record = cluster(name=name, location=location, pools=[pool("p", CURRENT)], selfLink=SELF_LINK_FORMAT.format(project=project, location=location, name=name), **extra)
    if host is not None:
        record["fleet"] = {"project": host, "membership": MEMBERSHIP_FORMAT.format(name=name)}
    return record


class FakeRunner:
    """The report's runner: answers each `--project=<host>` read with `(rc, stdout, stderr)`
    from `answers`, recording every call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, cmd, timeout=None, env=None):
        self.calls.append((list(cmd), timeout))
        host = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--project="))
        return self.answers[host]


def context_for(record, listing, runner, graded=None):
    member = report.grade_member(record, PROJECT, CURRENT, report.ServerConfigCache())
    context = report.readiness_context(member, AT, listing, report._pool_records(record, member), parse_version(LAGGING), False, graded=graded)
    context[shared.CONTEXT_RUN_CMD] = runner
    return member, context


def evaluate(record, listing, runner, graded=None):
    """`graded` is the set the run grades (`--cluster` narrows it); None grades the listing."""
    member, context = context_for(record, listing, runner, graded=graded)
    return rule.evaluate(record, member, read(items=[]), TARGET, context), context


class RegistrationTest(unittest.TestCase):
    def test_the_rule_is_registered_last_as_a_plan_level_rule(self):
        self.assertEqual(r.EXTRA_RULES[-1], rule)
        self.assertIsNone(rule.ENTRY)
        self.assertFalse(rule.CAN_BLOCK)
        self.assertEqual(rule.RULE_ID, "rollout-sequence-precheck")


class MembershipTest(unittest.TestCase):
    def test_no_fleet_block_is_a_note_and_no_read(self):
        runner = FakeRunner({})
        record = member_record("solo", host=None)
        result, _ = evaluate(record, [record], runner)
        self.assertEqual(result, {"blocking": [], "risks": [], "unknown": [], "notes": [rule.NOTE_NOT_A_MEMBER]})
        self.assertEqual(runner.calls, [])
        self.assertIsNone(rule.host_project(record))
        # A fleet block naming no project still names the host through the membership.
        self.assertEqual(rule.host_project({"fleet": {"membership": "projects/123456789012/locations/global/memberships/m"}}), "123456789012")
        self.assertIsNone(rule.host_project({"fleet": {}}))
        self.assertIsNone(rule.host_project({"fleet": "yes"}))

    def test_one_read_per_host_filed_on_its_first_member_and_notes_on_the_rest(self):
        runner = FakeRunner({HOST: (0, EMPTY_LIST, ""), OTHER_HOST: (0, EMPTY_LIST, "")})
        later = member_record("prod-b", location="us-east1")
        first = member_record("prod-a", location="us-central1-a")
        other_host = member_record("stage-a", location="europe-west1", project=OTHER_PROJECT, host=OTHER_HOST)
        third = member_record("prod-c", location="us-central1-a")  # same location as the first: the name decides
        listing = [later, other_host, third, first]  # the listing's order is not the filing order
        results = {record["name"]: evaluate(record, listing, runner)[0] for record in listing}
        self.assertEqual(results["prod-a"]["notes"], [rule.NOTE_NONE.format(host=HOST)])
        self.assertEqual(results["prod-b"]["notes"], [rule.NOTE_FILED_ON.format(host=HOST, cluster=f"{PROJECT}/us-central1-a/prod-a")])
        self.assertEqual(results["prod-c"]["notes"], [rule.NOTE_FILED_ON.format(host=HOST, cluster=f"{PROJECT}/us-central1-a/prod-a")])
        self.assertEqual(results["stage-a"]["notes"], [rule.NOTE_NONE.format(host=OTHER_HOST)])
        for name in results:
            self.assertEqual((results[name]["risks"], results[name]["unknown"]), ([], []), name)
        # One read per host project: the contexts are per member, so the filing decides it.
        self.assertEqual(sorted(call[0] for call in runner.calls), sorted([EXPECTED_COMMAND, [*EXPECTED_COMMAND[:5], f"--project={OTHER_HOST}", "--format=json"]]))
        self.assertEqual({call[1] for call in runner.calls}, {report.GCLOUD_TIMEOUT_SECONDS})

    def test_a_member_graded_alone_carries_the_hosts_read(self):
        # `--cluster` narrows who is graded, not who a rule may see: the read is filed on the
        # host's first member among the graded clusters, so a report of one member carries the
        # host's risk, and a note names where it landed on the other graded members.
        runner = FakeRunner({HOST: (0, EMPTY_LIST, "")})
        later = member_record("prod-b", location="us-east1")
        first = member_record("prod-a")
        alone, _ = evaluate(later, [first, later], runner, graded=[later])
        self.assertEqual(alone["notes"], [rule.NOTE_NONE.format(host=HOST)])
        self.assertEqual(len(runner.calls), 1)
        both, _ = evaluate(later, [first, later], runner, graded=[first, later])
        self.assertEqual(both["notes"], [rule.NOTE_FILED_ON.format(host=HOST, cluster=f"{PROJECT}/us-central1-a/prod-a")])
        no_listing, _ = evaluate(later, [], runner)
        self.assertEqual(no_listing["notes"], [rule.NOTE_NONE.format(host=HOST)])
        self.assertEqual(len(runner.calls), 2)  # the member alone, and the member with no listing: one read each; none for the member filed elsewhere


class SequenceTest(unittest.TestCase):
    def test_an_existing_sequence_is_one_risk_naming_its_stages_and_describe_ends_with_the_one_action(self):
        runner = FakeRunner({HOST: (0, recorded_list(), "")})
        first = member_record("prod-a")
        later = member_record("prod-b", location="us-east1")
        result, _ = evaluate(first, [later, first], runner)
        self.assertEqual((result["blocking"], result["unknown"], result["notes"]), ([], [], []))
        (risk,) = result["risks"]
        self.assertEqual(risk["kind"], "RolloutSequence")
        self.assertEqual((risk["namespace"], risk["name"], risk["object"]), ("", "prod-train", f"{HOST}/prod-train"))
        self.assertEqual(risk["host_project"], HOST)
        self.assertEqual(risk["resource"], f"projects/{HOST}/locations/global/rolloutSequences/prod-train")
        self.assertEqual(risk["clusters"], [f"{PROJECT}/us-central1-a/prod-a", f"{PROJECT}/us-east1/prod-b"])
        self.assertEqual(
            risk["stages"],
            [
                {"fleet_projects": [HOST], "selector": "resource.labels.env=='canary'", "soak": "604800s"},
                {"fleet_projects": [HOST], "selector": None, "soak": None},
            ],
        )
        text = rule.describe(risk)
        self.assertTrue(text.startswith(f"host project {HOST} already holds RolloutSequence prod-train (2 stage(s): "), text)
        self.assertIn("stage 1: projects acme-fleet-host, selector resource.labels.env=='canary', soak 604800s (7d)", text)
        self.assertIn("stage 2: projects acme-fleet-host, selector none (every member), soak none", text)
        self.assertIn("refused (FAILED_PRECONDITION)", text)
        # The remedy is the clause after the last `; `, the one action the weekly review takes.
        self.assertEqual(text.rsplit("; ", 1)[1], THE_ONE_ACTION)
        self.assertTrue(text.endswith(THE_ONE_ACTION))
        self.assertEqual(json.loads(json.dumps(risk)), risk)

    def test_the_fold_stamps_the_rule_with_no_entry_and_keeps_its_unknown_off_the_verdict(self):
        runner = FakeRunner({HOST: (0, recorded_list(), "")})
        first = member_record("prod-a")
        member, context = context_for(first, [first], runner)
        folded = r.evaluate_extra_rules(first, member, read(items=[]), TARGET, context, rules=[rule])
        (risk,) = folded["risks"]
        self.assertEqual((risk["rule"], risk["entry"]), (rule.RULE_ID, None))
        self.assertTrue(risk["text"].endswith(THE_ONE_ACTION))
        self.assertEqual(folded["rules"][rule.RULE_ID], {"entry": None, "can_block": False, "blocking": 0, "risks": 1, "unknown": 0, "notes": 0})
        self.assertEqual(folded["blocking"], [])
        refused = FakeRunner({HOST: (1, "", "ERROR: (gcloud.container.fleet.rolloutsequences.list) PERMISSION_DENIED: caller lacks gkehub.rolloutSequences.list")})
        member, context = context_for(first, [first], refused)
        folded = r.evaluate_extra_rules(first, member, read(items=[]), TARGET, context, rules=[rule])
        (unknown,) = folded["unknown"]
        self.assertFalse(unknown["can_block"])
        self.assertIn("PERMISSION_DENIED", unknown["reason"])
        self.assertTrue(unknown["text"].startswith(f"{rule.RULE_ID}: RolloutSequences of host project {HOST} not read"))
        self.assertFalse(r._unknown_moves_verdict(unknown))

    def test_an_empty_list_is_clean(self):
        for stdout in (EMPTY_LIST, "", "  \n"):
            runner = FakeRunner({HOST: (0, stdout, "")})
            record = member_record("prod-a")
            result, _ = evaluate(record, [record], runner)
            self.assertEqual(result, {"blocking": [], "risks": [], "unknown": [], "notes": [rule.NOTE_NONE.format(host=HOST)]}, repr(stdout))

    def test_a_sequence_without_stages_still_describes(self):
        bare = rule.risk_for(HOST, {"name": f"projects/{HOST}/locations/global/rolloutSequences/empty"}, [])
        self.assertEqual(bare["stages"], [])
        self.assertIn("(0 stage(s): none listed)", rule.describe(bare))

    def test_refused_failed_and_truncated_reads_are_unknown(self):
        record = member_record("prod-a")
        cases = {
            "refused": (1, "", "ERROR: (gcloud.container.fleet.rolloutsequences.list) PERMISSION_DENIED: caller lacks gkehub.rolloutSequences.list"),
            "api off": (1, "", "ERROR: SERVICE_DISABLED: GKE Hub API has not been used in project acme-fleet-host"),
            "timed out": (-1, "", "timed out after 60 seconds"),
            "truncated": (0, recorded_list()[:40], f"{shared.PROXY_TRUNCATION_MARKER} at 1048576 bytes"),
            "not a list": (0, '{"error": "unexpected"}', ""),
            "not json": (0, "ERROR printed on stdout", ""),
        }
        for label, answer in cases.items():
            with self.subTest(label=label):
                runner = FakeRunner({HOST: answer})
                result, _ = evaluate(record, [record], runner)
                self.assertEqual((result["risks"], result["notes"]), ([], []))
                (unknown,) = result["unknown"]
                self.assertTrue(unknown.startswith(f"RolloutSequences of host project {HOST} not read ("), unknown)
                self.assertIn(" ".join(EXPECTED_COMMAND), unknown)
        self.assertIn("PERMISSION_DENIED", evaluate(record, [record], FakeRunner({HOST: cases["refused"]}))[0]["unknown"][0])
        self.assertIn("rc=-1", evaluate(record, [record], FakeRunner({HOST: cases["timed out"]}))[0]["unknown"][0])
        self.assertIn("cut at the credential proxy's output cap", evaluate(record, [record], FakeRunner({HOST: cases["truncated"]}))[0]["unknown"][0])
        self.assertIn("not a JSON list", evaluate(record, [record], FakeRunner({HOST: cases["not a list"]}))[0]["unknown"][0])
        # No runner in the context: unknown, naming it, and no crash.
        member, context = context_for(record, [record], None)
        result = rule.evaluate(record, member, read(items=[]), TARGET, context)
        self.assertEqual(result["unknown"], [rule.UNKNOWN_READ.format(host=HOST, error=rule.NO_RUNNER_ERROR)])

    def test_the_cache_holds_the_hosts_read_for_a_shared_context(self):
        runner = FakeRunner({HOST: (0, recorded_list(), "")})
        record = member_record("prod-a")
        member, context = context_for(record, [record], runner)
        first = rule.evaluate(record, member, read(items=[]), TARGET, context)
        second = rule.evaluate(record, member, read(items=[]), TARGET, context)
        self.assertEqual(first, second)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(set(context[shared.CONTEXT_CACHE][rule.CACHE_KEY]), {HOST})
        # The cached copy is the rule's own: a caller's edit never reaches the next read.
        first["risks"][0]["stages"].clear()
        self.assertEqual(len(rule.evaluate(record, member, read(items=[]), TARGET, context)["risks"][0]["stages"]), 2)


class HelpersTest(unittest.TestCase):
    def test_soak_and_names(self):
        self.assertEqual(rule.format_soak("604800s"), "604800s (7d)")
        self.assertEqual(rule.format_soak("3600s"), "3600s")
        self.assertEqual(rule.format_soak("0s"), "0s")
        self.assertEqual(rule.format_soak(None), rule.NO_SOAK)
        self.assertEqual(rule.short_name("projects/p/locations/global/rolloutSequences/x"), "x")
        self.assertEqual(rule.short_name(None), "")
        self.assertEqual(rule.member_label(member_record("prod-a")), f"{PROJECT}/us-central1-a/prod-a")
        self.assertEqual(rule.member_label(cluster(name="bare", location="us-west1")), "us-west1/bare")
        self.assertEqual(rule.stage_record({"fleetProjects": ["projects/a", "b"], "clusterSelector": {"expression": "x"}, "soakDuration": "1s"}), {"fleet_projects": ["a", "b"], "selector": "x", "soak": "1s"})
        self.assertEqual(rule.stage_record("not a stage"), {"fleet_projects": [], "selector": None, "soak": None})


ORCHESTRATION_SOP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "governance", "upgrade_rollout_orchestration_sop.md")
SOP_LIST_COMMAND = "gcloud container fleet rolloutsequences list --project <host project> --format=json"
SOP_PRECHECK_LINE = "rollout-sequence precheck: host <project>; existing <n>; plan <clear|blocked>"


class OrchestrationSopQuotesTheRule(unittest.TestCase):
    """The rollout orchestration SOP's precheck step is this rule's read: it names the rule,
    runs the exact command, and writes the declared line the bench case grades."""

    def setUp(self):
        with open(ORCHESTRATION_SOP_PATH, encoding="utf-8") as f:
            self.sop = f.read()

    def test_the_sop_names_the_rule_and_its_command(self):
        self.assertIn(f"`{rule.RULE_ID}`", self.sop)
        self.assertIn(SOP_LIST_COMMAND, self.sop)
        self.assertIn(" ".join(rule.LIST_ARGV), SOP_LIST_COMMAND)

    def test_the_sop_carries_the_declared_precheck_line(self):
        self.assertIn(SOP_PRECHECK_LINE, self.sop)


if __name__ == "__main__":
    unittest.main()

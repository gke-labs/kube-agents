"""Tests for the reserved auto-merge labels and the broker's proposal-practice-label verb."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import automation_rules
import automerge_checks as ac
import automerge_eligibility as ae
import vcs_broker
from workspace_paths import WorkspaceError

CLUSTER = "acme-prod-1/us-central1/fleet-1"
CONTEXT = "gke_acme-prod-1_us-central1_fleet-1"
NS = "shop"
LABELS = {"app": "checkout"}
FID = "no-pdb.acme-prod-1-us-central1-fleet-1.shop.deployment-checkout"
NAME, TEXT = ae.render_budget(CLUSTER, "Deployment/checkout", NS, {"matchLabels": LABELS}, FID)
PATH = "clusters/fleet-1/shop/checkout-pdb.yaml"


def diff_adding(path: str, text: str) -> str:
    lines = text.split("\n")[:-1]
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..653b552\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )


def deployment(name="checkout", replicas=3, ready=None, labels=None, **meta):
    labels = labels or LABELS
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NS, **meta},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}},
        },
        "status": {"readyReplicas": replicas if ready is None else ready},
    }


def pdb(name="legacy", labels=None, **spec):
    return {
        "kind": "PodDisruptionBudget",
        "metadata": {"name": name, "namespace": NS},
        "spec": {"selector": {"matchLabels": labels or LABELS}, **spec},
    }


class ReservedLabelTest(unittest.TestCase):
    def test_every_label_write_naming_it_is_refused(self):
        for verb, payload in (
            ("proposal-update", {"labelsAdd": [ae.PRACTICE_LABEL]}),
            ("proposal-update", {"labelsRemove": ["  KUBE-AGENTS:Would-Auto-Merge "]}),
            ("proposal-create", {"labels": ["agent:audit", ae.PRACTICE_LABEL]}),
            ("issue-update", {"labelsAdd": ae.PRACTICE_LABEL}),
            ("label-ensure", {"name": ae.PRACTICE_LABEL}),
            ("proposal-update", {"labelsAdd": [ae.AUTOMERGE_LABEL]}),
            ("label-ensure", {"name": " Kube-Agents:Auto-Merge"}),
            # GitLab joins a label list with commas and splits it again.
            ("proposal-update", {"labelsAdd": ["x," + ae.PRACTICE_LABEL]}),
            ("issue-create", {"labels": ["a, kube-agents:would-auto-merge ,b"]}),
        ):
            with self.subTest(verb=verb, payload=payload):
                self.assertIn("reserved", ae.reserved_label_refusal(verb, payload))

    def test_the_on_label_is_the_one_the_rules_and_the_step_down_name(self):
        self.assertEqual(ae.AUTOMERGE_LABEL, automation_rules.AUTOMERGE_LABEL)

    def test_other_labels_and_filters_pass(self):
        for verb, payload in (
            ("proposal-update", {"labelsAdd": ["agent:audit"]}),
            ("proposal-list", {"excludeLabels": [ae.PRACTICE_LABEL]}),
            ("proposal-view", {"name": ae.PRACTICE_LABEL}),
            ("label-ensure", {"name": "agent:audit"}),
        ):
            with self.subTest(verb=verb, payload=payload):
                self.assertIsNone(ae.reserved_label_refusal(verb, payload))



class FakeForge:
    name = "github"
    text_sets_labels = False

    labels = True

    def __init__(self, view, moved=False):
        self.view = view
        self.moved = moved
        self.calls = []

    def proposal_view(self, api, repo, payload):
        self.calls.append(("proposal-view", payload))
        if self.moved and len([c for c in self.calls if c[0] == "proposal-view"]) > 1:
            return {"proposal": {**self.view["proposal"], "sourceRevision": "b" * 40}}
        return self.view

    def proposal_list(self, api, repo, payload):
        self.calls.append(("proposal-list", payload))
        return {"proposals": [{"number": 7, "labels": ["agent:audit", ae.AUTOMERGE_LABEL]}, {"number": 8, "labels": []}]}

    def label_ensure(self, api, repo, payload):
        self.calls.append(("label-ensure", payload))
        return {}

    def proposal_update(self, api, repo, payload):
        self.calls.append(("proposal-update", payload))
        return {}


ALLOW_DEV = automation_rules.parse("version: 1\nautomerge:\n  allow:\n    - fix: no-pdb\n      environments: [dev]\n")


class PracticeLabelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reads = []

    def run_verb(self, *, switch="practice", state="open", author="kube-agents[bot]", diff=None, labels=(),
                 rules=ALLOW_DEV, env="dev", items=None, read_code=0, target="main", moved=False):
        view = {
            "proposal": {
                "state": state, "author": author, "labels": list(labels), "target": target, "sourceRevision": "a" * 40,
            },
            "diff": diff_adding(PATH, TEXT) if diff is None else diff,
        }
        forge = FakeForge(view, moved=moved)
        items = [deployment()] if items is None else items

        def cluster_read(argv, context):
            self.reads.append((argv, context))
            if argv[0] == "gcloud":
                return read_code, json.dumps({"resourceLabels": {"environment": env}}), "denied"
            return read_code, json.dumps({"items": items}), "denied"

        broker = vcs_broker.VcsBroker(
            Path(self.tmp.name) / "scratch",
            git_runner=mock.Mock(),
            cluster_read=cluster_read,
            automerge_switch=lambda: switch,
        )
        bound = types.SimpleNamespace(forge=forge, repo="acme/infra", api=None, stamp=lambda r: r, whoami=lambda: "kube-agents[bot]")
        with (
            mock.patch.object(broker, "_bind", return_value=bound),
            mock.patch.object(broker, "_practice_rules", return_value=rules),
            mock.patch.object(broker, "_practice_base", return_value="main"),
        ):
            answer = broker.proposal_practice_label({"repository": "acme/infra", "number": 7})
        return answer, forge.calls

    def writes(self, calls):
        return [c for c in calls if c[0] != "proposal-view"]

    def test_switch_off_reads_no_cluster_and_writes_nothing(self):
        answer, calls = self.run_verb(switch="off")
        self.assertFalse(answer["labelled"])
        self.assertEqual((calls, self.reads), ([("proposal-view", {"number": 7, "diff": False})], []))

    def test_switch_off_takes_back_a_label_an_earlier_run_left(self):
        answer, calls = self.run_verb(switch="off", labels=[ae.PRACTICE_LABEL])
        self.assertFalse(answer["labelled"])
        self.assertEqual(self.writes(calls), [("proposal-update", {"number": 7, "labelsRemove": [ae.PRACTICE_LABEL]})])
        self.assertEqual(self.reads, [])

    def test_a_push_while_judging_takes_the_label_off_again(self):
        answer, calls = self.run_verb(moved=True)
        self.assertFalse(answer["labelled"])
        self.assertEqual(
            [c[1] for c in self.writes(calls) if c[0] == "proposal-update"],
            [{"number": 7, "labelsAdd": [ae.PRACTICE_LABEL]}, {"number": 7, "labelsRemove": [ae.PRACTICE_LABEL]}],
        )

    def test_the_answer_names_checks_but_not_what_the_cluster_said(self):
        answer, _ = self.run_verb()
        self.assertEqual(answer["checks"], [{"name": n, "passed": True} for n in ac.CHECK_NAMES])

    def test_an_eligible_fix_is_labelled_from_the_brokers_own_reads(self):
        for switch in ("practice", "on"):
            with self.subTest(switch=switch):
                self.reads = []
                answer, calls = self.run_verb(switch=switch)
                self.assertTrue(answer["labelled"], answer)
                self.assertEqual(answer["label"], ae.LABEL_FOR_SWITCH[switch])
                self.assertEqual(
                    self.writes(calls),
                    [
                        ("label-ensure", mock.ANY),
                        ("proposal-update", {"number": 7, "labelsAdd": [ae.LABEL_FOR_SWITCH[switch]]}),
                    ],
                )
                self.assertEqual(calls[0], ("proposal-view", {"number": 7, "diff": True}))
                self.assertEqual(self.reads[1], (["kubectl", "get", ac.READ_KINDS, "-n", NS, "-o", "json"], CONTEXT))

    def test_each_refusal_labels_nothing(self):
        for name, kwargs in (
            ("closed", {"state": "closed"}),
            ("someone else's", {"author": "mallory"}),
            ("an edited file", {"diff": diff_adding(PATH, TEXT.replace("maxUnavailable: 1", "minAvailable: 3"))}),
            ("two files", {"diff": diff_adding(PATH, TEXT) + diff_adding("x.yaml", "a: 1\n")}),
            ("no rules file", {"rules": automation_rules.Rules(status=automation_rules.ABSENT)}),
            ("prod", {"env": "prod"}),
            ("an unready workload", {"items": [deployment(ready=1)]}),
            ("an unreadable cluster", {"read_code": 1}),
            ("another target", {"target": "rules-of-my-own"}),
            ("another cluster's tree", {"diff": diff_adding("clusters/prod-1/shop/checkout-pdb.yaml", TEXT)}),
        ):
            with self.subTest(name):
                answer, calls = self.run_verb(**kwargs)
                self.assertFalse(answer["labelled"], answer)
                self.assertEqual(self.writes(calls), [])

    def test_a_labelled_fix_that_stops_qualifying_loses_the_label(self):
        answer, calls = self.run_verb(labels=[ae.PRACTICE_LABEL], items=[deployment(), pdb()])
        self.assertFalse(answer["labelled"])
        self.assertEqual(self.writes(calls), [("proposal-update", {"number": 7, "labelsRemove": [ae.PRACTICE_LABEL]})])

    def test_a_switch_change_swaps_the_label(self):
        answer, calls = self.run_verb(switch="practice", labels=[ae.AUTOMERGE_LABEL])
        self.assertEqual(answer["label"], ae.PRACTICE_LABEL)
        self.assertEqual(
            [c[1] for c in self.writes(calls) if c[0] == "proposal-update"],
            [{"number": 7, "labelsAdd": [ae.PRACTICE_LABEL]}, {"number": 7, "labelsRemove": [ae.AUTOMERGE_LABEL]}],
        )

    def test_an_already_labelled_eligible_fix_is_left_alone(self):
        answer, calls = self.run_verb(labels=[ae.PRACTICE_LABEL])
        self.assertTrue(answer["labelled"])
        self.assertEqual(self.writes(calls), [])

    def test_a_cluster_name_that_could_be_a_flag_is_not_read(self):
        _, text = ae.render_budget("--project=x/us-central1/c", "Deployment/checkout", NS, {"matchLabels": LABELS}, FID)
        answer, _ = self.run_verb(diff=diff_adding(PATH, text))
        self.assertFalse(answer["labelled"])
        self.assertEqual(self.reads, [])

    def test_a_forge_whose_comments_set_labels_is_never_labelled(self):
        with mock.patch.object(FakeForge, "text_sets_labels", True, create=True):
            answer, calls = self.run_verb()
        self.assertFalse(answer["labelled"])
        self.assertEqual((self.writes(calls), self.reads), ([], []))

    def test_a_failed_read_says_nothing_of_what_it_read(self):
        answer, _ = self.run_verb(read_code=1)
        self.assertNotIn("denied", answer["reason"])

    def test_the_verb_is_a_routed_write(self):
        self.assertIn("proposal-practice-label", vcs_broker.VCS_VERBS)
        self.assertIn("proposal-practice-label", vcs_broker.WRITE_VERBS)


class PracticeRulesTest(unittest.TestCase):
    def rules(self, members=None, error=None):
        import base64
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as tmp:
            broker = vcs_broker.VcsBroker(Path(tmp) / "scratch", git_runner=mock.Mock())
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as tar:
                for info, data in members or []:
                    tar.addfile(info, io.BytesIO(data) if data is not None else None)
            answer = {"archiveBase64": base64.b64encode(buffer.getvalue()).decode()}
            with mock.patch.object(broker, "snapshot", side_effect=error, return_value=answer) as snap:
                rules = broker._practice_rules({"repository": "acme/infra"}, "main")
            snap.assert_called_once_with(
                {"repository": "acme/infra", "ref": "main", "paths": [automation_rules.RULES_FILE]}
            )
            return rules

    def file(self, data: bytes, name=automation_rules.RULES_FILE, kind=None):
        import tarfile

        info = tarfile.TarInfo(name)
        info.size = len(data)
        if kind is not None:
            info.type, info.size, data = kind, 0, None
            info.linkname = "elsewhere"
        return info, data

    def test_the_target_branchs_file_is_parsed(self):
        text = b"version: 1\nautomerge:\n  allow:\n    - fix: no-pdb\n      environments: [dev]\n"
        self.assertEqual(self.rules([self.file(text)]).status, automation_rules.LOADED)

    def test_no_file_on_the_branch_is_absent(self):
        refused = WorkspaceError("no such path", code="PATH_NOT_FOUND")
        self.assertEqual(self.rules(error=refused).status, automation_rules.ABSENT)

    def test_a_link_or_an_unreadable_branch_is_refused(self):
        import tarfile

        for members, error in (
            ([self.file(b"", kind=tarfile.SYMTYPE)], None),
            ([self.file(b"\xff\xfe")], None),
            (None, WorkspaceError("boom", status=502)),
        ):
            with self.subTest(members=members, error=error):
                self.assertEqual(self.rules(members, error).status, automation_rules.INVALID)


class PublishDropsLabelTest(unittest.TestCase):
    def test_a_push_to_a_proposal_branch_takes_the_label_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = vcs_broker.VcsBroker(Path(tmp) / "scratch", git_runner=mock.Mock())
            forge = FakeForge({})
            bound = types.SimpleNamespace(forge=forge, repo="acme/infra", api=None)
            broker._drop_practice_label(bound, "platform-agent/x")
        self.assertEqual(
            forge.calls,
            [
                ("proposal-list", {"source": "platform-agent/x", "state": "open"}),
                ("proposal-update", {"number": 7, "labelsRemove": [ae.AUTOMERGE_LABEL]}),
            ],
        )

    def test_a_forge_that_fails_leaves_the_push_standing(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = vcs_broker.VcsBroker(Path(tmp) / "scratch", git_runner=mock.Mock())
            forge = mock.Mock(labels=True)
            forge.proposal_list.side_effect = WorkspaceError("down", status=502)
            broker._drop_practice_label(types.SimpleNamespace(forge=forge, repo="acme/infra", api=None), "b")


class ReservedLabelBrokerTest(unittest.TestCase):
    def test_the_forge_verbs_refuse_it_before_any_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = vcs_broker.VcsBroker(Path(tmp) / "scratch", git_runner=mock.Mock())
            forge = mock.Mock()
            bound = types.SimpleNamespace(forge=forge, repo="acme/infra", api=None, stamp=lambda r: r)
            with mock.patch.object(broker, "_bind", return_value=bound):
                for verb, payload in (
                    (broker.proposal_update, {"number": 7, "labelsAdd": [ae.PRACTICE_LABEL]}),
                    (broker.proposal_create, {"labels": [ae.PRACTICE_LABEL]}),
                    (broker.label_ensure, {"name": ae.PRACTICE_LABEL}),
                ):
                    with self.subTest(payload=payload), self.assertRaises(WorkspaceError) as caught:
                        verb({"repository": "acme/infra", **payload})
                    self.assertEqual(caught.exception.fields.get("code"), "RESERVED_LABEL")
            forge.assert_not_called()
            self.assertEqual(forge.method_calls, [])


if __name__ == "__main__":
    unittest.main()

import http.client
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import chat_cases
import findings_nudge as nudge
import findings_nudge_card as nudge_card
import findings_queue as fq
import upgrade_readiness_review as review

UTC = timezone.utc


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in October 2026, UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def finding(**overrides) -> dict:
    row = {
        "id": "fnd_0001",
        "check_slug": "probes-readiness",
        "severity": "critical",
        "rank_score": 288,
        "rubric": {"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 1.0},
        "project": "",
        "cluster": "prod",
        "namespace": "payments",
        "object": "api",
        "title": "no readinessProbe on api",
        "recommendation": {"action": "add a readinessProbe", "rationale": "traffic", "risk": "5xx"},
        "state": "queued",
        "first_shown_at": None,
        "provider_managed": False,
        "actionable": True,
    }
    row.update(overrides)
    return row


def shown(when: datetime, **overrides) -> dict:
    """A row the nudge already added, still waiting for a decision."""
    return finding(state="surfaced", first_shown_at=when.strftime("%Y-%m-%d %H:%M:%S"), **overrides)


def plan_for(rows, now=at(6, 12), added=None):
    return fq.pace(rows, now, fq.PacingLimits(), added or {"critical": 0, "noncritical": 0})


class ComposeTests(unittest.TestCase):
    def test_nothing_to_say_is_an_empty_string(self):
        self.assertEqual(nudge.compose(plan_for([]), 0, daily=True), "")
        # A critical waiting for tomorrow's budget is not something to say.
        self.assertEqual(nudge.compose(plan_for([finding()], added={"critical": 2}), 1, daily=False), "")

    def test_a_message_opens_with_plain_answer_headline_and_never_asks_for_the_full_list(self):
        rows = [finding(id="a"), finding(id="b", check_slug="x", severity="minor")]
        message = nudge.compose(plan_for(rows), 2, False, rows=rows)
        self.assertTrue(
            message.startswith("**1 new critical issue needs attention** · 2 open issues covering 1 object on 1 cluster\n\n")
        )
        self.assertIn("1 more issue is open.", message)
        self.assertNotIn("Findings queue", message)
        self.assertNotIn("Ask for the full list", message)

    def test_new_criticals_show_readable_location_and_fix_without_scores_or_ids(self):
        rows = [
            finding(id="a", title="first"),
            finding(id="b", check_slug="x", title="second"),
        ]
        message = nudge.compose(plan_for(rows), 2, False, rows=rows)
        self.assertIn("2 new critical issues need attention", message)
        self.assertIn("- critical · payments/api on prod — first. add a readinessProbe.", message)
        self.assertIn("- critical · payments/api on prod — second. add a readinessProbe.", message)
        self.assertNotIn("[288]", message)
        self.assertNotIn("id:", message)

    def test_one_critical_reads_as_singular(self):
        self.assertIn("1 new critical issue needs attention", nudge.compose(plan_for([finding()]), 1, False))

    def test_new_noncriticals_use_plain_review_headline(self):
        message = nudge.compose(plan_for([finding(severity="major")], now=at(6, 16)), 1, False)
        self.assertIn("1 new issue needs review", message)

    def test_a_gathered_line_summarises_extra_objects_cleanly_and_hides_ids_in_metadata(self):
        rows = [finding(id=f"p{i}", object=f"d{i}") for i in range(8)]
        bundle = nudge.build_nudge(plan_for(rows), 1, False, rows=rows)
        self.assertIn("payments/d0 (+7 more) on prod", bundle.text)
        self.assertNotIn("id:", bundle.text)
        self.assertIn("id=p0", bundle.card["thread_context"])
        self.assertIn("covers=8 unreported=0", bundle.card["thread_context"])

    def test_the_covered_count_and_unreported_rows_stay_in_hidden_thread_context(self):
        rows = [shown(at(5, 16), id=f"p{i}", object=f"d{i}", severity="major") for i in range(3)]
        rows += [
            shown(at(5, 16), id=f"p{i}", object=f"d{i}", severity="major", absent_since="2026-10-05 20:00:00")
            for i in range(3, 16)
        ]
        bundle = nudge.build_nudge(plan_for(rows), 1, daily=True, rows=rows)
        # Three reported rows and thirteen a sweep stopped reporting: a decision
        # reaches all sixteen, and the line says so.
        self.assertIn("payments/d0 (+15 more) on prod", bundle.text)
        self.assertIn("**payments/d0** (+15 more) on `prod`", bundle.card["rows"][0]["text"])
        self.assertNotIn("id:", bundle.text)
        self.assertNotIn("unreported", bundle.text)
        self.assertIn("covers=16 unreported=13", bundle.card["thread_context"])

    def test_reminders_appear_only_in_the_daily_part(self):
        plan = plan_for([shown(at(5, 12), id="old")])
        self.assertEqual(nudge.compose(plan, 1, daily=False), "")
        self.assertIn("1 critical issue needs attention", nudge.compose(plan, 1, daily=True))

    def test_the_stop_add_notice_explains_the_pause_without_exposing_ids(self):
        rows = [shown(at(5, 16), id="m0", severity="major", title="no limits"), finding(id="c0", check_slug="x")]
        plan = plan_for(rows)
        bundle = nudge.build_nudge(plan, 2, daily=True, rows=rows)
        self.assertIn("1 open issue is pausing new findings", bundle.text)
        self.assertIn(
            "New findings are paused until you resolve, snooze, or dismiss this open item (1 critical issue waiting behind it).",
            bundle.text,
        )
        self.assertIn("no limits", bundle.text)
        self.assertNotIn("id: m0", bundle.text)
        self.assertIn("id=m0", bundle.card["thread_context"])

    def test_the_waiting_count_says_how_many_are_critical(self):
        rows = [shown(at(5, 16), id="m0", severity="major"), finding(id="c0", check_slug="x")]
        rows += [finding(id=f"n{i}", check_slug=f"n{i}", severity="minor") for i in range(2)]
        message = nudge.compose(plan_for(rows), 4, daily=True, rows=rows)
        self.assertIn("1 critical issue and 2 other issues waiting behind it", message)
        rows = [shown(at(5, 16), id="m0", severity="major"), finding(id="n0", check_slug="n0", severity="minor")]
        self.assertIn("1 issue waiting behind it", nudge.compose(plan_for(rows), 2, daily=True, rows=rows))

    def test_a_cluster_scoped_finding_names_its_cluster_once(self):
        message = nudge.compose(plan_for([finding(namespace="", object="prod", title="WI disabled")]), 1, False)
        self.assertIn("prod — WI disabled", message)
        self.assertNotIn("prod/prod", message)
        self.assertNotIn("prod on prod", message)

    def test_project_path_is_hidden_unless_two_projects_share_a_cluster_name(self):
        single = [finding(project="kube-agents-gkedemos", cluster="prod-central")]
        single_msg = nudge.compose(plan_for(single), 1, False, rows=single)
        self.assertIn("payments/api on prod-central", single_msg)
        self.assertNotIn("kube-agents-gkedemos", single_msg)

        colliding = [
            finding(id="a", project="proj-a", cluster="prod"),
            finding(id="b", check_slug="x", project="proj-b", cluster="prod"),
        ]
        colliding_msg = nudge.compose(plan_for(colliding), 2, False, rows=colliding)
        self.assertIn("prod (proj-a)", colliding_msg)
        self.assertIn("prod (proj-b)", colliding_msg)

    def test_a_finding_with_no_recommended_action_prints_cleanly(self):
        message = nudge.compose(plan_for([finding(recommendation={})]), 1, False)
        self.assertIn("- critical · payments/api on prod — no readinessProbe on api", message)
        self.assertNotIn("  \n", message)

    def test_provider_managed_observations_are_counted_never_named(self):
        rows = [
            finding(id="obs", check_slug="x", provider_managed=True, actionable=False, title="kube-system thing"),
            finding(id="c0"),
        ]
        message = nudge.compose(plan_for(rows), 2, False, rows=rows)
        self.assertNotIn("kube-system thing", message)
        self.assertIn("1 provider-managed finding requires no action from you and is hidden.", message)


class CardAndChatUxTests(unittest.TestCase):
    """Issue #6256 regressions, the fold.table layout, and chat UX lint checks."""

    def test_issue_6256_upgrade_blockers_render_cleanly_with_zero_plumbing(self):
        pdb = shown(
            at(5, 12),
            id="urr-pdb-blocks-drain.kube-agents-gkedemos.prod-central.kube-system.istiod",
            check_slug="upgrade-drain-blocking-budget",
            project="kube-agents-gkedemos",
            cluster="prod-central",
            namespace="kube-system",
            object="istiod",
            rank_score=150,
            title="1. PodDisruptionBudget blocks drain: kube-system/istiod (minAvailable: 1 (>= 1 expected pods)) (1 replicas)",
            recommendation={"action": "Allow at least 1 pod disruption or run at least 2 replicas."},
        )
        orphan_a = shown(
            at(5, 12),
            id="urr-orphan-node.kube-agents-gkedemos.prod-central..gke-prod-pool-8f2a",
            check_slug="upgrade-orphan_node_objects",
            project="kube-agents-gkedemos",
            cluster="prod-central",
            namespace="",
            object="gke-prod-pool-8f2a",
            rank_score=150,
            title="22. Orphan Node objects: gke-prod-pool-8f2a has no backing GCE instance",
            recommendation={"action": "Delete the orphan Node object (kubectl delete node gke-prod-pool-8f2a)."},
        )
        orphan_b = shown(
            at(5, 12),
            id="urr-orphan-node.kube-agents-gkedemos.prod-central..gke-prod-pool-9b1c",
            check_slug="upgrade-orphan_node_objects",
            project="kube-agents-gkedemos",
            cluster="prod-central",
            namespace="",
            object="gke-prod-pool-9b1c",
            rank_score=150,
            title="22. Orphan Node objects: gke-prod-pool-9b1c has no backing GCE instance",
            recommendation={"action": "Delete the orphan Node object (kubectl delete node gke-prod-pool-9b1c)."},
        )
        queued_extra = [
            finding(
                id="q1",
                check_slug="static-sa-key",
                project="kube-agents-gkedemos",
                cluster="prod-central",
                namespace="billing",
                object="invoice-worker",
                severity="critical",
                title="invoice-worker mounts a static GCP key",
            ),
            finding(
                id="q2",
                check_slug="upgrade-fail-closed-webhook",
                project="kube-agents-gkedemos",
                cluster="prod-europe",
                namespace="",
                object="authz-webhook",
                severity="major",
                title="4. Fail-closed webhook: authz-webhook blocks API calls during drain",
            ),
            finding(
                id="q3",
                check_slug="no-resourcequota",
                project="kube-agents-gkedemos",
                cluster="staging-east",
                namespace="ledger",
                object="ledger",
                severity="minor",
                title="Namespace ledger has no ResourceQuota",
            ),
        ]
        all_rows = [pdb, orphan_a, orphan_b, *queued_extra]
        # Advance added={"critical": 2} so only the two reminded upgrade blockers surface, leaving 3 queued.
        plan = plan_for(all_rows, now=at(6, 12), added={"critical": 2, "noncritical": 0})
        bundle = nudge.build_nudge(
            plan,
            open_count=5,
            daily=True,
            rows=all_rows,
            backlog_url="https://github.com/acme/gitops/issues/42",
        )

        chat, fallback = nudge_card.chat_from_card(bundle.card)
        blocks, _ = nudge_card.blocks_from_card(bundle.card)
        chat_cases.check_chat(chat, "nudge")
        self.assertEqual(chat_cases.lint(chat), [])

        self.assertEqual(chat["headline"], "2 critical issues will delay or block cluster upgrades")
        self.assertEqual(chat["note"], "· 5 open issues covering 6 objects on 3 clusters")
        self.assertEqual(chat["tone"], "critical")
        self.assertEqual(len(chat["rows"]), 2)
        self.assertIn("**kube-system/istiod** on `prod-central`", chat["rows"][0]["text"])
        self.assertIn("(minAvailable: 1, 1 pod expected)", chat["rows"][0]["text"])
        self.assertIn("(1 replica)", chat["rows"][0]["text"])
        # The action holds a command, in parentheses: nothing is cut out of it, the neutral line stands in.
        self.assertEqual(chat["rows"][1]["detail"], nudge.COMMAND_ACTION)

        # Forbidden plumbing never appears in fallback text, chat JSON, or Block Kit JSON.
        visible_serialized = fallback + "\n" + json.dumps(chat) + "\n" + json.dumps(blocks)
        for forbidden in (
            "[150]",
            "1. [",
            "22.",
            "id:",
            "urr-pdb-blocks-drain",
            "urr-orphan-node",
            "kube-agents-gkedemos/",
            "kubectl",
            ">= 1 expected pods",
            "1 replicas",
            "Findings queue",
            "undecided",
            "Ask for the full list",
        ):
            self.assertNotIn(forbidden, visible_serialized)

        # The queue breakdown table.
        self.assertEqual(chat["fold"]["title"], "Queue breakdown (3 more open)")
        self.assertEqual(
            chat["fold"]["table"]["columns"],
            ["Cluster", "Critical", "Major", "Minor", "Next open issue"],
        )
        self.assertEqual(
            chat["fold"]["table"]["rows"],
            [
                ["prod-central", "1", "0", "0", "billing/invoice-worker: mounts a static GCP key"],
                ["prod-europe", "0", "1", "0", "authz-webhook: Fail-closed webhook: blocks API calls during drain"],
                ["staging-east", "0", "0", "1", "ledger/ledger: Namespace ledger has no ResourceQuota"],
            ],
        )

        # Buttons + backlog link.
        self.assertEqual(
            [c["text"] for c in chat["choices"]],
            ["Fix first issue"],
        )
        self.assertTrue(chat["choices"][0]["primary"])
        self.assertEqual(chat["links"], [{"text": "Open backlog", "url": "https://github.com/acme/gitops/issues/42"}])

        # Hidden thread context retains exact IDs and covers count.
        hidden = bundle.card["thread_context"]
        self.assertIn("urr-pdb-blocks-drain.kube-agents-gkedemos.prod-central.kube-system.istiod", hidden)
        self.assertIn("urr-orphan-node.kube-agents-gkedemos.prod-central..gke-prod-pool-8f2a", hidden)
        self.assertIn("covers=2", hidden)

    def test_clean_action_strips_bare_and_inline_cli_commands(self):
        # No cut: "Run <command> to <goal>" took the first " to " anywhere in the text for its goal.
        self.assertEqual(
            nudge.clean_action("Run `kubectl rollout restart deployment/api` to reload certificates."),
            nudge.COMMAND_ACTION,
        )
        self.assertEqual(
            nudge.clean_action("kubectl delete node gke-orphan-1"),
            "Remove the node gke-orphan-1.",
        )
        self.assertEqual(
            nudge.clean_action("gcloud container clusters update prod --enable-shielded-nodes"),
            nudge.FALLBACK_ACTION,
        )

    def test_noncritical_card_uses_attention_tone_and_omits_fold_when_nothing_remains(self):
        rows = [finding(id="m0", severity="major", title="missing PDB")]
        bundle = nudge.build_nudge(plan_for(rows, now=at(6, 16)), open_count=1, daily=False, rows=rows)
        chat, _ = nudge_card.chat_from_card(bundle.card)
        chat_cases.check_chat(chat, "noncritical")
        self.assertEqual(chat_cases.lint(chat), [])
        self.assertEqual(chat["tone"], "attention")
        self.assertNotIn("fold", chat)
        self.assertEqual([c["text"] for c in chat["choices"]], ["Fix issue"])


class DetectorWordingTests(unittest.TestCase):
    """The text the detectors really write, not titles made up for a fixture.

    `upgrade_readiness_review.ENTRY_TITLE_FORMAT` is `<entry>. <title>: <subject> <lead>`,
    so the subject the row already leads with sits in the middle of a sentence.
    """

    BUDGET_TITLE = (
        "1. A PodDisruptionBudget forbids the eviction: default/jayanti-pdb "
        "(minAvailable: 6 (>= 1 expected pods)) holds the node drain"
    )
    ORPHAN_TITLE = "22. A Node object outlives its virtual machine: this cluster blocks the upgrade"

    def issue_rows(self) -> list[dict]:
        budget = shown(
            at(5, 12),
            id="upgrade-drain-blocking-budget.kube-agents-gkedemos.jayanti-cluster.default.jayanti-pdb",
            check_slug="upgrade-drain-blocking-budget",
            project="kube-agents-gkedemos",
            cluster="jayanti-cluster",
            namespace="default",
            object="jayanti-pdb",
            rank_score=150,
            title=self.BUDGET_TITLE,
            recommendation={
                "action": "Raise maxUnavailable to 1 (or lower minAvailable) on PodDisruptionBudget "
                "default/jayanti-pdb, and run two replicas of Deployment default/jayanti-deployment "
                "(1 replicas) so the budget can be honoured."
            },
        )
        orphan = shown(
            at(5, 12),
            id="upgrade-orphan-node-objects.kube-agents-gkedemos.wolfram-auto-01._.cluster",
            check_slug="upgrade-orphan-node-objects",
            project="kube-agents-gkedemos",
            cluster="wolfram-auto-01",
            namespace="",
            object=review.CLUSTER_OBJECT,
            rank_score=150,
            title=self.ORPHAN_TITLE,
            recommendation={"action": "Delete the orphan Node object (kubectl delete node <name>); nothing runs on it."},
        )
        return [budget, orphan]

    def test_the_two_findings_of_issue_6256_read_as_sentences(self):
        rows = self.issue_rows()
        bundle = nudge.build_nudge(plan_for(rows, added={"critical": 2}), 2, daily=True, rows=rows)
        chat, _ = nudge_card.chat_from_card(bundle.card)
        self.assertEqual(
            [(row["text"], row["detail"]) for row in chat["rows"]],
            [
                (
                    "**default/jayanti-pdb** on `jayanti-cluster` — A PodDisruptionBudget forbids the eviction: "
                    "holds the node drain (minAvailable: 6, 1 pod expected)",
                    "Raise maxUnavailable to 1 (or lower minAvailable) on PodDisruptionBudget "
                    "default/jayanti-pdb, and run two replicas of Deployment default/jayanti-deployment "
                    "(1 replica) so the budget can be honoured.",
                ),
                (
                    "**wolfram-auto-01** — A Node object outlives its virtual machine: this cluster blocks the upgrade",
                    nudge.COMMAND_ACTION,
                ),
            ],
        )

    def test_the_cluster_wide_placeholder_and_the_check_prefix_are_the_detectors(self):
        self.assertIn(review.CLUSTER_OBJECT, nudge.CLUSTER_SCOPE_OBJECTS)
        self.assertEqual(nudge.UPGRADE_CHECK_PREFIX, review.CHECK_PREFIX)

    def test_a_subject_is_peeled_only_as_a_whole_name(self):
        row = finding(namespace="", object="web", title="Ingress has no backend: web-frontend and web")
        message = nudge.compose(plan_for([row]), 1, False)
        self.assertIn("web on prod — Ingress has no backend: web-frontend and web", message)

    def test_the_pod_count_keeps_its_meaning(self):
        # The source reads "minAvailable is at or above the pods expected", not a floor on pods.
        self.assertEqual(
            nudge.clean_title("x (minAvailable: 6 (>= 1 expected pods)) holds the node drain"),
            "x (minAvailable: 6, 1 pod expected) holds the node drain",
        )
        self.assertEqual(
            nudge.clean_title("x (minAvailable: 50% (rounds up to 2 of 2 expected pods))"),
            "x (minAvailable: 50%, rounds up to 2 of 2 expected pods)",
        )

    def test_a_recommendation_with_a_command_gives_the_neutral_line_whole(self):
        # Nothing is cut out and nothing is edited around a command: an edit can turn a warning
        # into its opposite, hide a destructive step, or print a later clause as the whole action.
        for raw in (
            # A negation or a condition in front of a command.
            "Do not run `kubectl delete pod web-0`, scale the Deployment instead.",
            "Do not run `kubectl delete pdb jayanti-pdb`.",
            "Stop: do not `kubectl delete node gke-x`; drain it first.",
            "Do not run kubectl delete ns shop --force on production.",
            # A destructive step beside the "Fix issue" button.
            "Clean up: gcloud container clusters delete prod-usc1 --quiet.",
            "Run `kubectl delete node gke-old-1` and then recreate the pool.",
            "Run `terraform destroy` to start over.",
            "Clean up (run `kubectl delete namespace prod`).",
            # "Run X to Y", whose goal was whatever followed the first " to ".
            "Run `kubectl rollout restart deployment/api` to reload certificates.",
            "Run kubectl cordon node-1. Then move the workloads to another pool.",
            "Run kubectl rollout restart deploy/api and scale it to 3 replicas.",
            "Run git commit -m 'switch to v2' to record the change.",
            # A command in parentheses, also where the parentheses hold a condition.
            "Delete the orphan Node object (kubectl delete node <name>); nothing runs on it.",
            "Raise the budget (kubectl patch pdb api only after the rollout finishes).",
            # An example.
            "Add a LimitRange, e.g. `kubectl apply -f limitrange.yaml`, then re-check.",
            "Do not, for example, `kubectl delete pod web-0`.",
            # A command after a lead-in, with a flag.
            "Upgrade the control plane of prod-usc1 with gcloud container clusters upgrade prod-usc1 "
            "--master --cluster-version=1.33.5-gke.1080000.",
            "Prefer `helm upgrade --atomic` over `helm upgrade`.",
            "Gcloud container clusters update c --enable-shielded-nodes",
            # Nothing but a command.
            "Run `kubectl get pods`",
            "gcloud container clusters update prod --enable-shielded-nodes",
            "kubectl delete pod --all -n shop",
            "kubectl delete secret sa-key -n billing after moving to Workload Identity",
        ):
            self.assertEqual(nudge.clean_action(raw), nudge.COMMAND_ACTION, raw)

    def test_the_neutral_line_states_no_action_and_holds_for_a_warning(self):
        self.assertEqual(nudge.COMMAND_ACTION, "The recommendation names a command, which is not shown here.")

    def test_the_plain_delete_is_the_one_rewording(self):
        self.assertEqual(nudge.clean_action("kubectl delete node gke-prod-pool-8f2a."), "Remove the node gke-prod-pool-8f2a.")

    def test_text_with_no_command_to_find_is_shown_as_written(self):
        for prose in (
            "Stop building images with docker socket mounts in the pod spec.",
            "Deploy via helm charts rather than raw manifests, and pin the chart version.",
            "Use a pre-helm hook to migrate the CRDs.",
            "Avoid non-git sources for manifests.",
            "Rotate the leaked credential; git history still contains it, so rewrite it too.",
            "Docker Hub images should move to Artifact Registry.",
            "Terraform owns this pool: change max_surge in the module.",
            "Fix via git revert of the last change to the overlay.",
            "Raise .spec.maxUnavailable to 1 - see the runbook (page 3).",
            "--trust-remote-code must be removed from the container's args.",
            # A command with no flag, written into a sentence, is not told from prose.
            "Roll back with helm rollback api 3.",
        ):
            self.assertEqual(nudge.clean_action(prose), prose)

    def test_the_readiness_reviews_remedies_are_shown_as_written_or_replaced_whole(self):
        replaced = {
            entry for entry, remedy in review.ENTRY_MITIGATIONS.items() if nudge.clean_action(remedy) == nudge.COMMAND_ACTION
        }
        # The two remedies that name a command: connection draining, and the orphan Node object.
        self.assertEqual(replaced, {21, 22})
        for entry, remedy in review.ENTRY_MITIGATIONS.items():
            if entry not in replaced:
                self.assertEqual(nudge.clean_action(remedy), " ".join(remedy.split()), entry)

    def test_a_mention_or_a_link_in_a_findings_text_is_not_live(self):
        row = finding(
            title="<!channel> api has no limits, see <https://evil.example|the runbook>",
            recommendation={"action": "Ask <@U123ABC> or <!here> to set limits on [api](https://evil.example)."},
        )
        other = finding(id="q", check_slug="q", severity="minor", title="<!channel> has no quota")
        bundle = nudge.build_nudge(plan_for([row, other]), 2, False, rows=[row, other])
        chat, _ = nudge_card.chat_from_card(bundle.card)
        shown = "\n".join([bundle.text, json.dumps(chat), json.dumps(nudge_card.blocks_from_card(bundle.card)[0])])
        for live in ("<!channel>", "<!here>", "<@U123ABC>", "<https://", "](https://"):
            self.assertNotIn(live, shown)
        self.assertIn("!channel api has no limits, see the runbook", bundle.text)
        self.assertIn("Ask @U123ABC or !here to set limits on api.", bundle.text)
        # A row's own names go the same way, though a Kubernetes name cannot hold one.
        named = finding(cluster="<!channel>", namespace="", object="<@U123ABC>", title="x", recommendation={})
        self.assertIn("- critical · @U123ABC on !channel — x", nudge.compose(plan_for([named]), 1, False))

    def test_the_next_issues_are_in_the_folded_breakdown_and_no_button_asks_for_them(self):
        # Two criticals are added; twelve other issues stay behind, each on three objects.
        rows = [finding(id="a"), finding(id="b", check_slug="b")]
        rows += [
            finding(id=f"q{i}-{j}", check_slug=f"q{i:02d}", object=f"w{j}", severity="minor", title=f"w{j} lacks limit {i}")
            for i in range(12)
            for j in range(3)
        ]
        bundle = nudge.build_nudge(plan_for(rows), 14, False, rows=rows)
        chat, _ = nudge_card.chat_from_card(bundle.card)
        chat_cases.check_chat(chat, "fold with text and table")
        self.assertEqual(chat_cases.lint(chat), [])
        # The count beside the headline says what it covers: objects, not the queue's rows.
        # The 38 rows are 4 objects (payments/api, w0, w1, w2); one object fails many checks.
        self.assertEqual(chat["note"], "· 14 open issues covering 4 objects on 1 cluster")
        # No button asks an agent for the rest: a press cost a model call or more.
        self.assertEqual([choice["text"] for choice in chat["choices"]], ["Fix first issue"])
        # The next ten are in the fold, above its table: a lead, one plain line each in rank order, the rest.
        self.assertEqual(list(chat["fold"]), ["title", "text", "table"])
        lines = chat["fold"]["text"].splitlines()
        self.assertEqual(lines[0], "**Next 10 open issues by rank**")
        # A line counts its objects plainly; "(+N more)" is a shown row's, for what a decision reaches.
        self.assertEqual(lines[1], "- minor · payments/w0 on prod — lacks limit 0 (3 objects)")
        self.assertEqual(lines[10], "- minor · payments/w0 on prod — lacks limit 9 (3 objects)")
        self.assertEqual(
            lines[11:],
            ["", "2 more open after these. The table counts every open issue the message does not show, by cluster."],
        )
        # The closed message does not show them, and the stored note keeps them for a typed follow-up.
        self.assertNotIn("lacks limit", bundle.text)
        saved = bundle.card["thread_context"].split("[Next 10 open issues by rank, as the message's queue breakdown lists them]\n", 1)[1]
        self.assertEqual(saved.splitlines()[:10], lines[1:11])

    def test_the_fold_names_how_many_follow_when_fewer_than_ten(self):
        rows = [finding(id="a"), finding(id="b", check_slug="b"), finding(id="c", check_slug="c"), finding(id="m", check_slug="m")]
        bundle = nudge.build_nudge(plan_for(rows), 4, False, rows=rows)
        self.assertEqual(bundle.card["choices"], ["Fix first issue"])
        lines = bundle.card["fold"]["text"].splitlines()
        self.assertEqual(lines[0], "**Next 2 open issues by rank**")
        self.assertEqual(lines[-1], "These are all the open issues the message does not show.")
        one_left = rows[:3]
        single = nudge.build_nudge(plan_for(one_left), 3, False, rows=one_left)
        self.assertEqual(single.card["fold"]["text"].splitlines()[:2], ["**Next 1 open issue by rank**", "- critical · payments/api on prod — no readinessProbe on api"])
        # Nothing left to show: no fold, and no list in the note.
        only = rows[:2]
        bundle = nudge.build_nudge(plan_for(only), 2, False, rows=only)
        self.assertIsNone(bundle.card["fold"])
        self.assertNotIn("by rank", bundle.card["thread_context"])

    def test_the_slack_relay_draws_the_folds_text_above_its_table(self):
        rows = [finding(id="a"), finding(id="b", check_slug="b"), finding(id="c", check_slug="c", title="<x> & co lack a limit")]
        bundle = nudge.build_nudge(plan_for(rows), 3, False, rows=rows)
        blocks, _ = nudge_card.blocks_from_card(bundle.card)
        container = next(block for block in blocks if block["type"] == "container")
        self.assertEqual([child["type"] for child in container["child_blocks"]], ["section", "table"])
        self.assertEqual(
            container["child_blocks"][0]["text"],
            {
                "type": "mrkdwn",
                "text": "*Next 1 open issue by rank*\n• critical · payments/api on prod — x &amp; co lack a limit\n\n"
                "These are all the open issues the message does not show.",
            },
        )
        self.assertNotIn("actions", [block["type"] for block in blocks][: blocks.index(container)])

    def test_one_of_a_version_is_not_a_count(self):
        self.assertEqual(
            nudge.clean_title("Kubelet 1.31.1 nodes lag the control plane"), "Kubelet 1.31.1 nodes lag the control plane"
        )
        self.assertEqual(nudge.clean_action("Upgrade the 1.31.1 nodes first"), "Upgrade the 1.31.1 nodes first.")
        self.assertEqual(nudge.clean_action("Upgrade the pool-1 nodes first."), "Upgrade the pool-1 nodes first.")

    def test_a_subject_is_peeled_only_right_after_the_first_colon(self):
        for obj, title in (
            ("web", "Missing PDB: web, api and worker"),
            ("web", "Deployment has no PDB: app: web has 1 replica"),
        ):
            row = finding(namespace="", object=obj, title=title, recommendation={})
            self.assertIn(f"web on prod — {title}", nudge.compose(plan_for([row]), 1, False))

    def test_a_line_names_the_member_its_severity_comes_from(self):
        # The queue ranks by score, so a high-scoring major can sit ahead of a low-scoring critical.
        rows = [
            finding(id="a", object="reporting", severity="major", title="reporting mounts a viewer key",
                    recommendation={"action": "Rotate the viewer key."}),
            finding(id="b", object="billing", severity="critical", title="billing mounts an editor key",
                    recommendation={"action": "Move billing to Workload Identity."}),
        ]
        bundle = nudge.build_nudge(plan_for(rows), 1, False, rows=rows)
        self.assertEqual(
            bundle.card["rows"],
            [{
                "severity": "critical",
                "text": "**payments/billing** (+1 more) on `prod` — mounts an editor key",
                "detail": "Move billing to Workload Identity.",
            }],
        )
        self.assertIn("1. [new] id=b members=a,b", bundle.card["thread_context"])

    def test_the_fallback_opens_on_a_bold_headline(self):
        rows = self.issue_rows()
        text = nudge.compose(plan_for(rows, added={"critical": 2}), 2, daily=True, rows=rows)
        self.assertTrue(
            text.startswith("**2 critical issues will delay or block cluster upgrades** · across 2 clusters\n\n"), text
        )

    def test_the_queue_line_and_the_table_use_one_set_of_words(self):
        rows = [
            finding(id="a"),
            finding(id="b", check_slug="b"),
            finding(id="c", check_slug="c"),
            finding(id="m", check_slug="m", severity="minor", cluster="dev"),
        ]
        # Two criticals a day: the third waits, and the minor waits behind it.
        bundle = nudge.build_nudge(plan_for(rows), 4, False, rows=rows)
        self.assertEqual(bundle.card["after"], ["2 more issues are open (1 critical, 1 lower priority)."])
        self.assertEqual(
            bundle.card["fold"]["table"]["columns"], ["Cluster", "Critical", "Major", "Minor", "Next open issue"]
        )
        only_critical = rows[:3]
        bundle = nudge.build_nudge(plan_for(only_critical), 3, False, rows=only_critical)
        self.assertEqual(bundle.card["after"], ["1 more issue is open (1 critical)."])

    def test_a_table_cut_short_says_so_in_its_title(self):
        rows = [finding(id="a")] + [
            finding(id=f"q{i}", check_slug=f"q{i}", severity="minor", cluster=f"c{i:02d}") for i in range(20)
        ]
        bundle = nudge.build_nudge(plan_for(rows), 21, False, rows=rows)
        fold = bundle.card["fold"]
        self.assertEqual(len(fold["table"]["rows"]), nudge.MAX_FOLD_CLUSTERS)
        self.assertEqual(fold["title"], "Queue breakdown (top 15 clusters)")
        self.assertEqual(bundle.card["after"], ["20 more issues are open."])
        self.assertLessEqual(len(fold["title"]), nudge.chat_message.FOLD_TITLE_MAX)
        chat_cases.check_chat(nudge_card.chat_from_card(bundle.card)[0], "cut table")

    def test_a_long_row_is_cut_at_a_word_in_the_buttons_reply(self):
        row = finding(title="word " * 120)
        bundle = nudge.build_nudge(plan_for([row]), 1, False, rows=[row])
        reply = bundle.card["replies"]["Fix issue"]
        self.assertLessEqual(len(reply), nudge.chat_message.REPLY_MAX)
        self.assertTrue(reply.endswith("word…"), reply[-20:])


class NudgeHarness(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        for target, value in ((sys, "stdout"), (sys, "stderr")):
            patcher = unittest.mock.patch.object(target, value, self.out if value == "stdout" else self.err)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gateway = Path(tmp.name)
        self.home = self.gateway / "profiles" / "platform"
        self.home.mkdir(parents=True)
        env = unittest.mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for name in fq.PACING_ENV.values():
            os.environ.pop(name, None)

    def run_at(self, now, ranked, added=None, fail=None, surfaced_error=None, failing_marks=(), backlog_url=None):
        """Drive `main` at `now` against a stubbed queue, recording every request."""
        self.out.seek(0)
        self.out.truncate()
        self.calls, self.marks = [], []

        def fake_request(endpoint, path, body=None, method=""):
            self.calls.append(path)
            if fail and path != "/v1/findings/expire-snoozes":
                raise fail
            if path == "/v1/findings/expire-snoozes":
                return {"expired": 0}
            if path == "/v1/findings/ranked":
                return {"findings": ranked}
            if path.startswith("/v1/findings/additions?day="):
                self.day_asked = path.split("=", 1)[1]
                return {"day": self.day_asked, **(added or {"critical": 0, "noncritical": 0})}
            if path == "/v1/findings/publication/backlog":
                if backlog_url is None:
                    raise urllib.error.HTTPError(path, 404, "not found", None, io.BytesIO(b"not found"))
                return {"target_ref": backlog_url}
            if path.endswith("/surfaced"):
                if surfaced_error:
                    raise surfaced_error
                if path.split("/")[3] in failing_marks:
                    raise urllib.error.URLError("refused")
                self.marks.append((path.split("/")[3], body))
                return {}
            raise AssertionError(f"unexpected request {path}")

        with unittest.mock.patch.object(nudge, "_request", side_effect=fake_request):
            return nudge.main([], clock=lambda: now)

    def state(self) -> dict:
        path = self.home / nudge.STATE_FILE
        return json.loads(path.read_text()) if path.exists() else {}


class MainTests(NudgeHarness):
    def test_noon_adds_the_top_two_criticals_marks_them_and_writes_sidecar_card(self):
        rows = [finding(id=f"c{i}", check_slug=f"c{i}", rank_score=300 - i) for i in range(6)]
        code = self.run_at(at(6, 12), rows, backlog_url="https://github.com/acme/ops/issues/9")
        self.assertEqual(code, 0)
        output = self.out.getvalue()
        self.assertIn("2 new critical issues need attention", output)
        self.assertEqual(self.day_asked, "2026-10-06")
        self.assertEqual(
            self.marks,
            [
                ("c0", {"publisher": "nudge", "added_class": "critical", "run": "2026-10-06T12:00:00Z"}),
                ("c1", {"publisher": "nudge", "added_class": "critical", "run": "2026-10-06T12:00:00Z"}),
            ],
        )
        self.assertEqual(
            self.calls[:4],
            [
                "/v1/findings/expire-snoozes",
                "/v1/findings/ranked",
                "/v1/findings/additions?day=2026-10-06",
                "/v1/findings/publication/backlog",
            ],
        )
        self.assertEqual(self.state()[nudge.DAILY_KEY], "2026-10-06")
        loaded = nudge_card.load(self.home, output)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["links"], [["Open backlog", "https://github.com/acme/ops/issues/9"]])

    def test_an_hour_with_nothing_to_say_prints_nothing_and_removes_stale_card(self):
        rows = [shown(at(6, 12), id="c0"), shown(at(6, 12), id="c1", check_slug="x"), finding(id="c2", check_slug="y")]
        self.run_at(at(6, 12), [finding(id="seed")])
        self.assertTrue(nudge_card.card_path(self.home).exists())
        code = self.run_at(at(6, 13), rows, added={"critical": 2, "noncritical": 0})
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])
        self.assertFalse(nudge_card.card_path(self.home).exists())

    def test_a_morning_hour_adds_nothing(self):
        code = self.run_at(at(6, 11), [finding()])
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertNotIn(nudge.DAILY_KEY, self.state())

    def test_reminders_go_out_once_a_day(self):
        rows = [shown(at(5, 12), id="old")]
        self.run_at(at(6, 12), rows)
        self.assertIn("1 critical issue needs attention", self.out.getvalue())
        self.assertEqual(self.marks, [("old", {"publisher": "nudge"})])

        self.run_at(at(6, 13), rows)
        self.assertEqual(self.out.getvalue(), "")

        self.run_at(at(7, 12), rows)
        self.assertIn("1 critical issue needs attention", self.out.getvalue())

    def test_the_daily_part_waits_for_the_first_run_after_noon(self):
        rows = [shown(at(5, 12), id="old")]
        self.run_at(at(6, 9), rows)
        self.assertEqual(self.out.getvalue(), "")
        self.run_at(at(6, 14), rows)
        self.assertIn("1 critical issue needs attention", self.out.getvalue())

    def test_stop_add_posts_a_notice_once_a_day_and_adds_nothing(self):
        rows = [shown(at(5, 16), id="m0", severity="major"), finding(id="c0", check_slug="x")]
        self.run_at(at(6, 12), rows)
        message = self.out.getvalue()
        self.assertIn("1 open issue is pausing new findings", message)
        self.assertNotIn("id: m0", message)
        loaded = nudge_card.load(self.home, message)
        self.assertIn("id=m0", loaded["thread_context"])
        self.assertEqual(self.marks, [("m0", {"publisher": "nudge"})])

        for hour in (13, 16, 23):
            self.run_at(at(6, hour), rows)
            self.assertEqual(self.out.getvalue(), "", hour)

        self.run_at(at(7, 12), rows)
        self.assertIn("1 open issue is pausing new findings", self.out.getvalue())

    def test_noncriticals_are_added_from_the_afternoon_hour(self):
        rows = [finding(id=f"m{i}", check_slug=f"m{i}", severity="major") for i in range(4)]
        self.run_at(at(6, 15), rows)
        self.assertEqual(self.out.getvalue(), "")
        self.run_at(at(6, 16), rows)
        self.assertIn("3 new issues need review", self.out.getvalue())
        self.assertEqual([body["added_class"] for _, body in self.marks], ["noncritical"] * 3)

    def test_the_hour_is_configurable(self):
        rows = [finding(severity="major")]
        with unittest.mock.patch.dict(os.environ, {"FINDINGS_NONCRITICAL_AFTER_HOUR": "13"}):
            self.run_at(at(6, 13), rows)
        self.assertIn("1 new issue needs review", self.out.getvalue())

    def test_the_daily_limit_is_configurable_and_a_bad_value_is_reported(self):
        rows = [finding(id=f"c{i}", check_slug=f"c{i}") for i in range(3)]
        with unittest.mock.patch.dict(os.environ, {"FINDINGS_DAILY_CRITICALS": "1"}):
            self.run_at(at(6, 12), rows)
        self.assertIn("1 new critical issue needs attention", self.out.getvalue())

        with unittest.mock.patch.dict(os.environ, {"FINDINGS_DAILY_CRITICALS": "lots"}):
            self.run_at(at(7, 12), rows)
        self.assertIn("2 new critical issues need attention", self.out.getvalue())
        self.assertIn("FINDINGS_DAILY_CRITICALS='lots'", self.err.getvalue())

    def test_a_member_joining_a_reminded_line_is_marked_shown_with_it(self):
        rows = [shown(at(5, 12), id="p0"), finding(id="p1", object="web")]
        self.run_at(at(6, 12), rows)
        self.assertEqual(self.marks, [("p0", {"publisher": "nudge"}), ("p1", {"publisher": "nudge"})])

    def test_a_failed_surfaced_call_costs_the_bookkeeping_not_the_message(self):
        code = self.run_at(at(6, 12), [finding(id="a")], surfaced_error=urllib.error.URLError("refused"))
        self.assertEqual(code, 0)
        self.assertIn("no readinessProbe on api", self.out.getvalue())
        self.assertIn("could not mark a surfaced", self.err.getvalue())

    def test_an_addition_whose_mark_failed_is_not_announced_again_that_day(self):
        rows = [finding(id="a")]
        refused = urllib.error.URLError("refused")
        self.run_at(at(6, 12), rows, surfaced_error=refused)
        self.assertIn("1 new critical issue needs attention", self.out.getvalue())
        for hour in (13, 18):
            self.run_at(at(6, hour), rows, surfaced_error=refused)
            self.assertEqual(self.out.getvalue(), "", hour)
        self.run_at(at(7, 12), rows)
        self.assertIn("1 new critical issue needs attention", self.out.getvalue())

    def test_an_announced_critical_whose_mark_failed_still_spends_the_day_budget(self):
        a = finding(id="a", check_slug="a", rank_score=300)
        b = finding(id="b", check_slug="b", rank_score=290)
        c = finding(id="c", check_slug="c", rank_score=400)
        self.run_at(at(6, 12), [a, b], surfaced_error=urllib.error.URLError("refused"))
        self.assertIn("2 new critical issues need attention", self.out.getvalue())
        self.run_at(at(6, 13), [c, a, b])
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])
        self.run_at(at(7, 12), [c, a, b])
        self.assertIn("2 new critical issues need attention", self.out.getvalue())
        self.assertEqual([fid for fid, _ in self.marks], ["c", "a"])

    def test_an_announced_noncritical_whose_mark_failed_holds_back_a_higher_ranked_one(self):
        rows = [finding(id=f"m{i}", check_slug=f"m{i}", severity="major", rank_score=90 - i) for i in range(3)]
        self.run_at(at(6, 16), rows, surfaced_error=urllib.error.URLError("refused"))
        self.assertIn("3 new issues need review", self.out.getvalue())
        self.run_at(at(6, 17), [finding(id="m9", check_slug="m9", severity="major", rank_score=95)] + rows)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])

    def test_an_announced_critical_whose_mark_failed_still_holds_noncriticals_back(self):
        critical = finding(id="a", check_slug="a")
        self.run_at(at(6, 12), [critical], surfaced_error=urllib.error.URLError("refused"))
        self.run_at(at(6, 16), [critical, finding(id="m0", check_slug="m0", severity="major")])
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])

    def test_an_announced_noncritical_whose_mark_failed_still_stops_every_addition(self):
        major = finding(id="m0", check_slug="m0", severity="major")
        self.run_at(at(6, 16), [major], surfaced_error=urllib.error.URLError("refused"))
        self.run_at(at(6, 17), [finding(id="c", check_slug="c"), major])
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])

    def test_a_row_left_open_on_an_announced_line_does_not_stop_the_days_additions(self):
        m0 = finding(id="m0", check_slug="K", severity="major", object="api")
        self.run_at(at(6, 16), [m0])
        self.assertIn("1 new issue needs review", self.out.getvalue())
        self.assertEqual(self.state()[nudge.ANNOUNCED_KEY]["ids"], ["m0"])
        decided = finding(
            id="m0", check_slug="K", severity="major", object="api", state="dismissed", first_shown_at="2026-10-06 16:00:00"
        )
        m1 = finding(id="m1", check_slug="K", severity="major", object="web")
        n0 = finding(id="n0", check_slug="N", severity="major", rank_score=80)
        self.run_at(at(6, 17), [decided, m1, n0], added={"critical": 0, "noncritical": 1})
        loaded = nudge_card.load(self.home, self.out.getvalue())
        self.assertIn("id=m1", loaded["thread_context"])
        self.assertEqual(sorted(fid for fid, _ in self.marks), ["m1", "n0"])
        self.assertEqual(self.state()[nudge.ANNOUNCED_KEY]["ids"], ["m0", "m1", "n0"])

    def test_a_line_whose_marks_partly_failed_does_not_stop_the_days_additions(self):
        m0 = finding(id="m0", check_slug="K", severity="major", object="api")
        m1 = finding(id="m1", check_slug="K", severity="major", object="web")
        self.run_at(at(6, 16), [m0, m1], failing_marks={"m1"})
        self.assertIn("1 new issue needs review", self.out.getvalue())
        self.assertEqual([fid for fid, _ in self.marks], ["m0"])
        n0 = finding(id="n0", check_slug="N", severity="major", rank_score=80, title="no memory limit on orders")
        added = {"critical": 0, "noncritical": 1, "lines": [list(fq.item_key(m0))]}
        self.run_at(at(6, 17), [m1, n0], added=added)
        self.assertIn("1 new issue needs review", self.out.getvalue())
        self.assertIn("no memory limit on orders", self.out.getvalue())
        self.assertEqual([fid for fid, _ in self.marks], ["n0"])

    def test_a_record_an_older_build_wrote_today_keeps_the_key_rule_until_the_day_ends(self):
        m1 = finding(id="m1", check_slug="K", severity="major", object="web")
        n0 = finding(id="n0", check_slug="N", severity="major", rank_score=80)
        key = list(nudge.fq.item_key(m1))
        (self.home / nudge.STATE_FILE).write_text(json.dumps({nudge.ANNOUNCED_KEY: {"day": "2026-10-06", "keys": [key]}}))
        self.run_at(at(6, 17), [m1, n0], added={"critical": 0, "noncritical": 1})
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])
        self.run_at(at(7, 16), [m1, n0])
        self.assertEqual(sorted(fid for fid, _ in self.marks), ["m1", "n0"])
        self.assertEqual(sorted(self.state()[nudge.ANNOUNCED_KEY]["ids"]), ["m1", "n0"])

    def test_additions_under_an_older_record_keep_it_without_ids(self):
        critical = finding(id="c", check_slug="c")
        m1 = finding(id="m1", check_slug="K", severity="major", object="web")
        (self.home / nudge.STATE_FILE).write_text(
            json.dumps({nudge.ANNOUNCED_KEY: {"day": "2026-10-06", "keys": [["x", "acme", "prod"]]}})
        )
        self.run_at(at(6, 12), [critical, m1])
        self.assertEqual([fid for fid, _ in self.marks], ["c"])
        record = self.state()[nudge.ANNOUNCED_KEY]
        self.assertNotIn("ids", record)
        self.assertEqual(len(record["keys"]), 2)

    def test_malformed_announced_keys_are_ignored_not_fatal(self):
        for keys in ("x", [1]):
            (self.home / nudge.STATE_FILE).write_text(
                json.dumps({nudge.ANNOUNCED_KEY: {"day": "2026-10-06", "keys": keys}})
            )
            self.assertEqual(self.run_at(at(6, 12), [finding(id="c", check_slug="c")]), 0, keys)
            self.assertEqual([fid for fid, _ in self.marks], ["c"], keys)

    def test_a_mark_that_raises_skips_neither_the_other_marks_nor_the_daily_record(self):
        rows = [shown(at(5, 12), id="old"), finding(id="new", check_slug="x")]
        code = self.run_at(at(6, 12), rows, surfaced_error=http.client.IncompleteRead(b""))
        self.assertEqual(code, 0)
        self.assertEqual(self.err.getvalue().count("could not mark"), 2)
        self.assertEqual(self.state()[nudge.DAILY_KEY], "2026-10-06")

    def test_the_open_count_counts_items_not_individual_members(self):
        rows = (
            [finding(id=f"p{i}", object=f"d{i}") for i in range(3)]
            + [finding(id="x", check_slug="x")]
            + [finding(id="y", check_slug="y")]
        )
        self.run_at(at(6, 12), rows)
        output = self.out.getvalue()
        self.assertIn("· 3 open issues covering 4 objects on 1 cluster", output)
        self.assertIn("1 more issue is open", output)

    def test_the_open_count_counts_provider_managed_observations_by_item(self):
        rows = [finding(id=f"p{i}", object=f"d{i}") for i in range(3)] + [finding(id="x", check_slug="x")]
        rows += [
            finding(
                id=f"obs{i}",
                check_slug="dns",
                namespace="kube-system",
                object=f"pod{i}",
                provider_managed=True,
                actionable=False,
            )
            for i in range(10)
        ]
        self.run_at(at(6, 12), rows)
        self.assertIn(
            "1 provider-managed finding requires no action from you and is hidden.",
            self.out.getvalue(),
        )

    def test_a_failed_expiry_costs_snooze_lateness_not_the_message(self):
        def fake_request(endpoint, path, body=None, method=""):
            if path == "/v1/findings/expire-snoozes":
                raise urllib.error.URLError("refused")
            if path == "/v1/findings/ranked":
                return {"findings": [finding()]}
            if path.startswith("/v1/findings/additions"):
                return {"critical": 0, "noncritical": 0}
            return {}

        with unittest.mock.patch.object(nudge, "_request", side_effect=fake_request):
            code = nudge.main([], clock=lambda: at(6, 12))
        self.assertEqual(code, 0)
        self.assertIn("no readinessProbe on api", self.out.getvalue())
        self.assertIn("could not expire lapsed snoozes", self.err.getvalue())


class FirstReportTests(NudgeHarness):
    def file_scan(self, when: datetime, text: str | None = None) -> Path:
        """Onboarding's marker as `bootstrap_scan_gate.py` writes it, filed at `when`."""
        marker = self.gateway / nudge.SCAN_FILED_MARKER
        marker.write_text(text if text is not None else f"task_id=t_1\nfiled_at={int(when.timestamp())}\n")
        return marker

    def test_nothing_is_added_while_the_first_report_is_on_its_way(self):
        self.file_scan(at(6, 9))
        self.run_at(at(6, 12), [finding()])
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])
        err = self.err.getvalue()
        self.assertIn("adding nothing until the first inventory report is out (its delivery claimed", err)
        self.assertIn("or until 2026-10-07 09:00 UTC", err)
        self.assertIn(nudge.DELIVERED_MARKER, err)
        self.assertEqual(err.count(nudge.LOG_PREFIX), 1)

    def test_still_held_23_hours_after_the_scan_was_filed(self):
        self.file_scan(at(5, 13))
        self.run_at(at(6, 12), [finding()])
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("adding nothing until", self.err.getvalue())

    def test_no_longer_held_25_hours_after_the_scan_was_filed(self):
        self.file_scan(at(5, 11))
        self.run_at(at(6, 12), [finding()])
        self.assertIn("1 new critical issue needs attention", self.out.getvalue())
        err = self.err.getvalue()
        self.assertIn("still undelivered more than 24 hours after its sweep was filed", err)
        self.assertIn("filed 2026-10-05 11:00 UTC by its filed_at= line", err)
        self.assertEqual(err.count(nudge.LOG_PREFIX), 1)

    def test_an_unparseable_marker_falls_back_to_its_mtime(self):
        for text in ("t_1", "task_id=t_1\nfiled_at=soon\n"):
            with self.subTest(text=text):
                marker = self.file_scan(at(6, 12), text)
                for filed, held in ((at(5, 13), True), (at(5, 11), False)):
                    os.utime(marker, (filed.timestamp(), filed.timestamp()))
                    (self.home / nudge.STATE_FILE).unlink(missing_ok=True)
                    self.err.seek(0)
                    self.err.truncate()
                    self.assertEqual(nudge.first_report_hold(self.gateway, at(6, 12))[0], held)
                    self.run_at(at(6, 12), [finding()])
                    self.assertIn("by its mtime", self.err.getvalue())
                    self.assertEqual("1 new critical issue needs attention" in self.out.getvalue(), not held)

    def test_a_marker_removed_while_it_is_read_is_not_held(self):
        self.assertIsNone(nudge._filed_at(self.gateway / nudge.SCAN_FILED_MARKER))
        self.file_scan(at(6, 9))
        with unittest.mock.patch.object(nudge, "_filed_at", return_value=None):
            self.assertEqual(nudge.first_report_hold(self.gateway, at(6, 12)), (False, ""))
            self.run_at(at(6, 12), [finding()])
        self.assertIn("1 new critical issue needs attention", self.out.getvalue())

    def test_additions_start_once_it_was_delivered(self):
        self.file_scan(at(6, 9))
        (self.gateway / nudge.DELIVERED_MARKER).write_text("")
        self.run_at(at(6, 12), [finding()])
        self.assertIn("1 new critical issue needs attention", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_a_conversation_claim_holds_until_its_outcome_is_recorded(self):
        self.file_scan(at(6, 9))
        (self.gateway / nudge.DELIVERED_MARKER).write_text("")
        (self.gateway / "onboarding_conversation.json").write_text("{}")
        self.assertTrue(nudge.first_report_hold(self.gateway, at(6, 12))[0])
        (self.gateway / nudge.CONVERSATION_DONE_MARKER).write_text("posted\n")
        self.assertEqual(nudge.first_report_hold(self.gateway, at(6, 12)), (False, ""))

    def test_an_install_that_never_ran_onboarding_is_not_held(self):
        self.assertEqual(nudge.first_report_hold(self.gateway, at(6, 12)), (False, ""))
        self.run_at(at(6, 12), [finding()])
        self.assertIn("1 new critical issue needs attention", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_the_gateway_home_is_found_from_a_shell_in_the_container(self):
        self.file_scan(at(6, 9))
        with unittest.mock.patch.dict(os.environ, {"HERMES_HOME": str(self.gateway)}):
            self.assertEqual(nudge._homes(), (self.gateway, self.home))
            self.assertTrue(nudge.first_report_hold(nudge._homes()[0], at(6, 12))[0])


class FailureTests(NudgeHarness):
    def test_an_unreadable_queue_fails_once_a_day(self):
        refused = urllib.error.URLError("refused")
        self.assertEqual(self.run_at(at(6, 3), [], fail=refused), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("could not read the queue", self.err.getvalue())

        self.assertEqual(self.run_at(at(6, 4), [], fail=refused), 0)
        self.assertEqual(self.run_at(at(6, 23), [], fail=refused), 0)
        self.assertIn("already reported a failure today", self.err.getvalue())

        self.assertEqual(self.run_at(at(7, 0), [], fail=refused), 1)

    def test_an_unexpected_read_error_also_fails_once_a_day(self):
        broken = http.client.IncompleteRead(b"{")
        self.assertEqual(self.run_at(at(6, 12), [], fail=broken), 1)
        self.assertEqual(self.run_at(at(6, 13), [], fail=broken), 0)

    def test_a_failure_after_the_read_fails_once_a_day(self):
        with unittest.mock.patch.object(fq, "pace", side_effect=KeyError("rubric")):
            self.assertEqual(self.run_at(at(6, 12), [finding()]), 1)
            self.assertEqual(self.run_at(at(6, 13), [finding()]), 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("failed before posting", self.err.getvalue())

    def test_a_failure_does_not_spend_the_daily_part(self):
        self.run_at(at(6, 12), [], fail=urllib.error.URLError("refused"))
        self.run_at(at(6, 13), [shown(at(5, 12), id="old")])
        self.assertIn("1 critical issue needs attention", self.out.getvalue())


class RosterTests(unittest.TestCase):
    """The job entry is the whole deployment of this script, so it earns a test."""

    def test_the_roster_runs_this_script_hourly_and_adds_after_the_last_daily_audit(self):
        roster = json.loads(
            (Path(__file__).parent.parent / "cron" / "jobs.json").read_text(encoding="utf-8")
        )
        jobs = {job["id"]: job for job in roster["jobs"]}
        job = jobs["findings-morning-nudge"]

        self.assertEqual(job["script"], "findings_nudge.py")
        self.assertTrue(job["no_agent"])
        self.assertTrue(job["enabled"])
        self.assertIn(job["deliver"], ("chat", "all"))
        self.assertEqual(job["schedule"]["expr"], job["schedule"]["display"])
        self.assertEqual(job["schedule"]["expr"].split()[1:], ["*", "*", "*", "*"])

        def minute_of_day(job_id):
            minute, hour_field = jobs[job_id]["schedule"]["expr"].split()[:2]
            return int(hour_field) * 60 + int(minute)

        latest_daily = max(
            minute_of_day(job_id)
            for job_id, other in jobs.items()
            if other["schedule"]["expr"].split()[4] == "*"
            and other["schedule"]["expr"].split()[1].isdigit()
            and not other.get("no_agent")
        )
        self.assertGreater(fq.REMIND_HOUR * 60, latest_daily)


if __name__ == "__main__":
    unittest.main()

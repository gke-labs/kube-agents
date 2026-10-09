"""Unit tests for the local corrections sync-upstream-skills re-applies after an upstream wipe.

Run: python3 -m unittest scripts.test_sync_upstream_skills

Three invariants:

- after a sync wipes a skill dir, inject_footer restores the Cluster Agent coupling footer
  exactly once (idempotent), and only for skills that have one;
- the in-tree copy of every skill with a registered correction already reads as the next sync
  would leave it, so the mirror and the registries cannot drift apart unnoticed;
- a registered correction that no longer matches upstream aborts the sync before anything is
  written, rather than publishing the uncorrected upstream content with exit 0.
"""

import fnmatch
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# The module file name has hyphens, so load it by path rather than a plain import.
_SPEC = importlib.util.spec_from_file_location(
    "sync_upstream_skills", str(Path(__file__).resolve().parent / "sync-upstream-skills.py")
)
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)


def write_registered_files(skill_dir, skill_name):
    """Write each file SKILL_FILE_SUBSTITUTIONS registers for a skill, carrying its targets.

    A fixture skill without them reads as upstream having dropped the file, which the pre-flight
    and the backstop both refuse.
    """
    for relpath, pairs in sync.SKILL_FILE_SUBSTITUTIONS.get(skill_name, {}).items():
        path = Path(skill_dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(t for t, _ in pairs) + "\n# reference\n", encoding="utf-8")


class InjectFooterTest(unittest.TestCase):
    def _skill_dir(self, body="# Upstream skill\n\nSome content.\n"):
        d = Path(tempfile.mkdtemp())
        (d / "SKILL.md").write_text(body, encoding="utf-8")
        return d

    def _read(self, d):
        return (d / "SKILL.md").read_text(encoding="utf-8")

    def test_injects_footer_for_configured_skill(self):
        d = self._skill_dir()
        self.assertTrue(sync.inject_footer(str(d), "gke-cluster-creation"))
        text = self._read(d)
        self.assertIn(sync.FOOTER_MARKER, text)
        self.assertIn("provision the Cluster Agent profile", text)
        self.assertIn("cluster_agent_profile.py create", text)

    def test_creation_footer_covers_teardown(self):
        d = self._skill_dir()
        self.assertTrue(sync.inject_footer(str(d), "gke-cluster-creation"))
        text = self._read(d)
        self.assertIn("Cluster Agent Profile Teardown", text)
        self.assertIn("cluster_agent_profile.py delete", text)
        self.assertIn("cluster-agent-reconcile", text)

    def test_idempotent_no_duplicate(self):
        d = self._skill_dir()
        self.assertTrue(sync.inject_footer(str(d), "gke-cluster-creation"))
        # Second call must be a no-op (footer already present from this run's copy).
        self.assertFalse(sync.inject_footer(str(d), "gke-cluster-creation"))
        self.assertEqual(self._read(d).count(sync.FOOTER_MARKER), 1)

    def test_unconfigured_skill_untouched(self):
        d = self._skill_dir(body="original\n")
        self.assertFalse(sync.inject_footer(str(d), "gke-cost-analysis"))
        self.assertEqual(self._read(d), "original\n")

    def test_missing_skill_md_is_a_backstop_failure(self):
        d = Path(tempfile.mkdtemp())  # no SKILL.md
        with self.assertRaises(sync.LocalCorrectionLost):
            sync.inject_footer(str(d), "gke-cluster-creation")

    def test_skill_without_footer_is_untouched(self):
        d = Path(tempfile.mkdtemp())  # no SKILL.md, and no footer configured either
        self.assertFalse(sync.inject_footer(str(d), "gke-basics"))

    def test_upgrades_footer_names_the_verification_skill(self):
        d = self._skill_dir()
        self.assertTrue(sync.inject_footer(str(d), "gke-upgrades"))
        text = self._read(d)
        self.assertIn(sync.FOOTER_MARKER, text)
        self.assertIn("fleet-upgrade-verification", text)
        self.assertIn("scripts/fleet_upgrade_report.py", text)
        self.assertIn("--target-version", text)

    def test_repo_upgrades_skill_carries_the_footer(self):
        repo_root = Path(__file__).resolve().parent.parent
        skill_md = repo_root / "agents" / "platform" / "skills" / "gke-upgrades" / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        self.assertTrue(content.rstrip("\n").endswith(sync.SKILL_FOOTERS["gke-upgrades"].rstrip("\n")))
        self.assertEqual(content.count(sync.FOOTER_MARKER), 1)


class ApplySubstitutionsTest(unittest.TestCase):
    def _skill_dir(self, body=""):
        d = Path(tempfile.mkdtemp())
        (d / sync.SKILL_MD_FILENAME).write_text(body, encoding=sync.UTF_8_ENCODING)
        return d

    def _read(self, d):
        return (d / sync.SKILL_MD_FILENAME).read_text(encoding=sync.UTF_8_ENCODING)

    def test_applies_substitution_for_configured_skill(self):
        d = self._skill_dir_covering(
            "gke-workload-security", sync.GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET
        )
        self.assertTrue(sync.apply_substitutions(str(d), "gke-workload-security"))
        text = self._read(d)
        self.assertNotIn(sync.GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET, text)
        self.assertIn(sync.GKE_WORKLOAD_SECURITY_NEW_NETPOL_SNIPPET, text)
        self.assertIn("--enable-network-policy", text)

    def test_idempotent_no_duplicate(self):
        d = self._skill_dir_covering(
            "gke-workload-security", sync.GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET
        )
        self.assertTrue(sync.apply_substitutions(str(d), "gke-workload-security"))
        # Second call must be a no-op (replacement already present).
        self.assertFalse(sync.apply_substitutions(str(d), "gke-workload-security"))
        text = self._read(d)
        self.assertEqual(text.count(sync.GKE_WORKLOAD_SECURITY_NEW_NETPOL_SNIPPET), 1)

    def test_unconfigured_skill_untouched(self):
        d = self._skill_dir(body="original\n")
        self.assertFalse(sync.apply_substitutions(str(d), "gke-cost-analysis"))
        self.assertEqual(self._read(d), "original\n")

    def _skill_dir_covering(self, skill_name, body):
        # `body` places the target under test in the prose it appears in upstream; the rest of
        # the skill's targets are appended verbatim. A missing target is a hard failure now, so
        # a fixture carrying only one target of a multi-substitution skill would read as drift
        # in the others and abort before the substitution under test is reached.
        missing = "\n\n".join(
            target for target, _ in sync.SKILL_SUBSTITUTIONS[skill_name] if target not in body
        )
        return self._skill_dir(body=body + "\n\n" + missing + "\n")

    def test_applies_manifest_generation_routing_substitution(self):
        d = self._skill_dir_covering(
            "gke-manifest-generation",
            "description: >-\n  " + sync.GKE_MANIFEST_GENERATION_OLD_ROUTING_SNIPPET,
        )
        self.assertTrue(sync.apply_substitutions(str(d), "gke-manifest-generation"))
        text = self._read(d)
        self.assertNotIn(sync.GKE_MANIFEST_GENERATION_OLD_ROUTING_SNIPPET, text)
        self.assertIn(sync.GKE_MANIFEST_GENERATION_NEW_ROUTING_SNIPPET, text)
        self.assertIn("(use gcp-config-connector)", text)
        # Second call must be a no-op (replacement already present).
        self.assertFalse(sync.apply_substitutions(str(d), "gke-manifest-generation"))
        self.assertEqual(self._read(d).count(sync.GKE_MANIFEST_GENERATION_NEW_ROUTING_SNIPPET), 1)

    def test_repo_manifest_generation_skill_routes_to_config_connector(self):
        # The in-tree mirror must already read as a fresh sync would leave it: the substitution's
        # replacement present, its target gone, and the skill it routes to present in the tree.
        repo_root = Path(__file__).resolve().parent.parent
        skills_dir = repo_root / "agents" / "platform" / "skills"
        content = (skills_dir / "gke-manifest-generation" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn(sync.GKE_MANIFEST_GENERATION_OLD_ROUTING_SNIPPET, content)
        self.assertIn(sync.GKE_MANIFEST_GENERATION_NEW_ROUTING_SNIPPET, content)
        self.assertTrue((skills_dir / "gcp-config-connector" / "SKILL.md").is_file())

    def test_applies_manifest_generation_service_account_substitution(self):
        d = self._skill_dir_covering(
            "gke-manifest-generation",
            "Always create and reference a dedicated `ServiceAccount`\n    "
            + sync.GKE_MANIFEST_GENERATION_OLD_SERVICE_ACCOUNT_SNIPPET
            + " for each microservice.",
        )
        self.assertTrue(sync.apply_substitutions(str(d), "gke-manifest-generation"))
        text = self._read(d)
        self.assertNotIn(sync.GKE_MANIFEST_GENERATION_OLD_SERVICE_ACCOUNT_SNIPPET, text)
        self.assertNotIn("devteam-agent-sa", text)
        self.assertIn(sync.GKE_MANIFEST_GENERATION_NEW_SERVICE_ACCOUNT_SNIPPET, text)
        # Second call must be a no-op (replacement already present).
        self.assertFalse(sync.apply_substitutions(str(d), "gke-manifest-generation"))
        self.assertEqual(self._read(d).count(sync.GKE_MANIFEST_GENERATION_NEW_SERVICE_ACCOUNT_SNIPPET), 1)

    def test_repo_manifest_generation_skill_uses_neutral_service_account(self):
        # The in-tree mirror must already read as a fresh sync would leave it: the retired
        # DevTeamAgent-era ServiceAccount name gone and the neutral example in its place.
        repo_root = Path(__file__).resolve().parent.parent
        skill_md = repo_root / "agents" / "platform" / "skills" / "gke-manifest-generation" / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        self.assertNotIn("devteam-agent-sa", content)
        self.assertNotIn(sync.GKE_MANIFEST_GENERATION_OLD_SERVICE_ACCOUNT_SNIPPET, content)
        self.assertIn(sync.GKE_MANIFEST_GENERATION_NEW_SERVICE_ACCOUNT_SNIPPET, content)

    def test_applies_manifest_generation_output_path_substitution(self):
        d = self._skill_dir_covering(
            "gke-manifest-generation",
            "        ```bash\n        gcloud container ai profiles manifests create \\\n"
            "          --output=manifest \\\n"
            + sync.GKE_MANIFEST_GENERATION_OLD_OUTPUT_PATH_SNIPPET
            + "\n    -   *Constraint*: You must include all resources returned by this command",
        )
        self.assertTrue(sync.apply_substitutions(str(d), "gke-manifest-generation"))
        text = self._read(d)
        self.assertNotIn("--output-path={output_file_path}", text)
        self.assertIn(sync.GKE_MANIFEST_GENERATION_NEW_OUTPUT_PATH_SNIPPET, text)
        self.assertIn("> {output_file_path}", text)
        self.assertIn("refuses it.", text)
        # Second call must be a no-op (replacement already present).
        self.assertFalse(sync.apply_substitutions(str(d), "gke-manifest-generation"))
        self.assertEqual(self._read(d).count(sync.GKE_MANIFEST_GENERATION_NEW_OUTPUT_PATH_SNIPPET), 1)

    def test_applies_manifest_generation_developer_knowledge_substitution(self):
        d = self._skill_dir_covering(
            "gke-manifest-generation",
            "        retrieve official GKE documentation:\n"
            + sync.GKE_MANIFEST_GENERATION_OLD_DEVELOPER_KNOWLEDGE_SNIPPET,
        )
        self.assertTrue(sync.apply_substitutions(str(d), "gke-manifest-generation"))
        text = self._read(d)
        self.assertNotIn("This is the preferred tool", text)
        self.assertNotIn("`get_document`", text)
        self.assertIn(sync.GKE_MANIFEST_GENERATION_NEW_DEVELOPER_KNOWLEDGE_SNIPPET, text)
        self.assertIn("Do not call **`answer_query`**", text)
        # Second call must be a no-op (replacement already present).
        self.assertFalse(sync.apply_substitutions(str(d), "gke-manifest-generation"))
        self.assertEqual(self._read(d).count(sync.GKE_MANIFEST_GENERATION_NEW_DEVELOPER_KNOWLEDGE_SNIPPET), 1)

    def test_repo_manifest_generation_skill_carries_every_substitution(self):
        # The in-tree mirror is rmtree'd and re-copied from upstream on every sync, so every local
        # divergence has to be a registered pair, and the file has to already read as a fresh sync
        # would leave it: every replacement present exactly once, every target gone.
        repo_root = Path(__file__).resolve().parent.parent
        skill_md = repo_root / "agents" / "platform" / "skills" / "gke-manifest-generation" / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        for target, replacement in sync.SKILL_SUBSTITUTIONS["gke-manifest-generation"]:
            self.assertNotIn(target, content)
            self.assertEqual(content.count(replacement), 1, replacement)

    def test_repo_workload_security_skills_have_enforcement_command(self):
        repo_root = Path(__file__).resolve().parent.parent
        for agent in ["platform", "cluster"]:
            skill_md = repo_root / "agents" / agent / "skills" / "gke-workload-security" / "SKILL.md"
            self.assertTrue(skill_md.is_file(), f"{skill_md} must exist")
            content = skill_md.read_text(encoding="utf-8")
            self.assertIn("--enable-network-policy", content)
            self.assertIn("--update-addons=NetworkPolicy=ENABLED", content)
            self.assertIn("networkConfig.datapathProvider", content)
            self.assertIn("--location <location>", content)
            self.assertIn("node pools may be recreated; this can take several minutes", content)
            self.assertNotIn(sync.GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET, content)
            self.assertIn(sync.GKE_WORKLOAD_SECURITY_NEW_NETPOL_SNIPPET, content)

    def test_applies_basics_credentials_substitution(self):
        d = self._skill_dir_covering("gke-basics", sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET)
        write_registered_files(d, "gke-basics")
        self.assertTrue(sync.apply_substitutions(str(d), "gke-basics"))
        text = self._read(d)
        self.assertNotIn(sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET, text)
        self.assertIn(sync.GKE_BASICS_NEW_CREDENTIALS_SNIPPET, text)
        self._assert_credentials_snippet_pins_the_target(text)
        # Second call must be a no-op (replacement already present).
        self.assertFalse(sync.apply_substitutions(str(d), "gke-basics"))
        self.assertEqual(text.count(sync.GKE_BASICS_NEW_CREDENTIALS_SNIPPET), 1)

    def _basics_body_with_credentials_as(self, credentials):
        # Every other gke-basics target verbatim, so a backstop failure can only come from
        # what `credentials` does to the credentials pair.
        others = [
            target
            for target, _ in sync.SKILL_SUBSTITUTIONS["gke-basics"]
            if target != sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET
        ]
        return "\n\n".join([credentials] + others) + "\n"

    def test_drifted_target_is_a_backstop_failure(self):
        # Unreachable while verify_local_corrections runs first, but it is what keeps the
        # uncorrected upstream text out of the tree if the clone and the copy ever disagree.
        drifted = self._basics_body_with_credentials_as(
            sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET.replace("   * Always", "   - Always")
        )
        d = self._skill_dir(body=drifted)
        write_registered_files(d, "gke-basics")
        with self.assertRaises(sync.LocalCorrectionLost) as caught:
            sync.apply_substitutions(str(d), "gke-basics")
        self.assertIn("gke-basics/SKILL.md", str(caught.exception))
        self.assertIn("target snippet not found", str(caught.exception))
        # Left as upstream wrote it rather than half-rewritten.
        self.assertEqual(self._read(d), drifted)

    def test_target_and_replacement_both_present_is_a_backstop_failure(self):
        # Skipping on the replacement alone would leave the target in the tree and return
        # False, so the sync would report success over the uncorrected passage.
        body = self._basics_body_with_credentials_as(
            sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET + "\n\n" + sync.GKE_BASICS_NEW_CREDENTIALS_SNIPPET
        )
        d = self._skill_dir(body=body)
        write_registered_files(d, "gke-basics")
        with self.assertRaises(sync.LocalCorrectionLost) as caught:
            sync.apply_substitutions(str(d), "gke-basics")
        self.assertIn("the target and the replacement are both present", str(caught.exception))
        self.assertEqual(self._read(d), body)

    def test_repeated_target_is_a_backstop_failure(self):
        # replace(..., SUBSTITUTION_COUNT) rewrites the first occurrence only, so applying a
        # pair whose target upstream repeats leaves the rest uncorrected and returns True.
        target = sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET
        body = self._basics_body_with_credentials_as(target + "\n\nAnd again:\n\n" + target)
        d = self._skill_dir(body=body)
        write_registered_files(d, "gke-basics")
        with self.assertRaises(sync.LocalCorrectionLost) as caught:
            sync.apply_substitutions(str(d), "gke-basics")
        self.assertIn("occurs 2 times", str(caught.exception))
        self.assertEqual(self._read(d), body)

    def test_partial_drift_writes_nothing(self):
        # gke-manifest-generation has four pairs. With one drifted, the three that do match
        # must not be written either: the file is rewritten once, after the whole loop.
        pairs = sync.SKILL_SUBSTITUTIONS["gke-manifest-generation"]
        body = "\n\n".join(target for target, _ in pairs[:-1]) + "\n"
        d = self._skill_dir(body=body)
        with self.assertRaises(sync.LocalCorrectionLost):
            sync.apply_substitutions(str(d), "gke-manifest-generation")
        self.assertEqual(self._read(d), body)

    def test_missing_skill_md_is_a_backstop_failure(self):
        d = Path(tempfile.mkdtemp())
        with self.assertRaises(sync.LocalCorrectionLost):
            sync.apply_substitutions(str(d), "gke-basics")

    def test_no_registered_pair_nests_one_side_in_the_other(self):
        # A replacement containing its own target is still present after being applied, so the
        # both-present guard above would abort every run after the first. The registries have
        # no such pair; this fails if one is added.
        # Pairs of one skill are applied in order to the same text, so the same holds across
        # them: a replacement that contains another pair's target or replacement changes that
        # pair's verdict once applied.
        registered = list(sync.SKILL_SUBSTITUTIONS.items()) + [
            (f"{skill_name}/{relpath}", pairs)
            for skill_name, files in sync.SKILL_FILE_SUBSTITUTIONS.items()
            for relpath, pairs in files.items()
        ]
        for skill_name, pairs in registered:
            for target, replacement in pairs:
                for other_target, other_replacement in pairs:
                    self.assertNotIn(other_target, replacement, skill_name)
                    self.assertNotIn(other_replacement, target, skill_name)
                    if (other_target, other_replacement) != (target, replacement):
                        self.assertNotIn(other_replacement, replacement, skill_name)

    def test_applies_file_substitutions_beside_skill_md(self):
        d = self._skill_dir_covering("gke-basics", sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET)
        write_registered_files(d, "gke-basics")
        self.assertTrue(sync.apply_substitutions(str(d), "gke-basics"))
        for relpath, pairs in sync.SKILL_FILE_SUBSTITUTIONS["gke-basics"].items():
            text = (d / relpath).read_text(encoding="utf-8")
            for target, replacement in pairs:
                self.assertNotIn(target, text, relpath)
                self.assertEqual(text.count(replacement), 1, relpath)
        self.assertFalse(sync.apply_substitutions(str(d), "gke-basics"))

    def test_missing_registered_file_is_a_backstop_failure(self):
        d = self._skill_dir_covering("gke-basics", sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET)
        with self.assertRaises(sync.LocalCorrectionLost) as caught:
            sync.apply_substitutions(str(d), "gke-basics")
        self.assertIn("cli-reference.md", str(caught.exception))

    def test_repo_skills_carry_every_substitution(self):
        # The in-tree copy already reads as the next sync leaves it, so the hand edit and the
        # registry entry cannot drift apart. substituted_files() is the one list of what a sync
        # rewrites for a skill, so every registry apply_substitutions reads is one this loop reads.
        skills_dir = Path(__file__).resolve().parent.parent / "agents" / "platform" / "skills"
        for skill_name in sorted(set(sync.SKILL_SUBSTITUTIONS) | set(sync.SKILL_FILE_SUBSTITUTIONS)):
            for relpath, pairs in sync.substituted_files(skill_name):
                content = (skills_dir / skill_name / relpath).read_text(encoding="utf-8")
                for target, replacement in pairs:
                    with self.subTest(skill=skill_name, file=relpath, target=target[:40]):
                        self.assertNotIn(target, content)
                        self.assertEqual(content.count(replacement), 1)

    def test_repo_backup_dr_skill_carries_every_substitution(self):
        # The Golden Path heading on a line of its own, so it renders, and a restore plan that
        # restores volume data.
        skill_md = (
            Path(__file__).resolve().parent.parent / "agents" / "platform" / "skills" / "gke-backup-dr" / "SKILL.md"
        )
        content = skill_md.read_text(encoding="utf-8")
        for target, replacement in sync.SKILL_SUBSTITUTIONS["gke-backup-dr"]:
            self.assertNotIn(target, content)
            self.assertEqual(content.count(replacement), 1, replacement)
        # A second copy of the flag would leave the agent choosing between two restore policies.
        self.assertEqual(content.count("--volume-data-restore-policy"), 1)

    def test_skill_without_substitutions_is_untouched(self):
        # The guard must not turn "nothing configured" into a failure.
        d = self._skill_dir()
        self.assertFalse(sync.apply_substitutions(str(d), "gke-observability"))

    def _assert_credentials_snippet_pins_the_target(self, text):
        # The two ways this snippet has been wrong. A `KUBECONFIG=` prefixed onto the
        # `gcloud` line alone is unset again by the time the agent runs `kubectl`, which then
        # reads the host cluster; and a file named for the cluster alone collides across
        # projects and locations, so a `get-credentials` for one re-points every reader of the
        # other. agents/platform/AGENTS.md ("Cluster Credentials") is the canonical form, and
        # the name is the one _thread_kubeconfig_path builds.
        self.assertIn("export KUBECONFIG=", text)
        self.assertIn(
            ".kubeconfigs/kubeconfig_${PROJECT}_${CLUSTER}_${LOCATION}.yaml", text
        )
        self.assertNotIn("kubeconfig_CLUSTER_NAME.yaml", text)

    def test_repo_basics_skill_carries_credentials_substitution(self):
        repo_root = Path(__file__).resolve().parent.parent
        skill_md = repo_root / "agents" / "platform" / "skills" / "gke-basics" / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        for target, replacement in sync.SKILL_SUBSTITUTIONS["gke-basics"]:
            self.assertNotIn(target, content)
            self.assertEqual(content.count(replacement), 1, replacement)
        self._assert_credentials_snippet_pins_the_target(content)


class ClassifySubstitutionTest(unittest.TestCase):
    """The one statement of what makes a pair appliable, which both callers read."""

    def _verdict(self, content):
        verdict, _ = sync.classify_substitution(content, "OLD", "NEW")
        return verdict

    def test_target_once_and_no_replacement_applies(self):
        self.assertEqual(self._verdict("a OLD b"), sync.SUBSTITUTION_APPLY)

    def test_replacement_alone_is_already_adopted(self):
        self.assertEqual(self._verdict("a NEW b"), sync.SUBSTITUTION_SKIP)

    def test_neither_present_is_undecidable(self):
        self.assertEqual(self._verdict("a b"), sync.SUBSTITUTION_UNDECIDABLE)

    def test_both_present_is_undecidable(self):
        self.assertEqual(self._verdict("a OLD b NEW c"), sync.SUBSTITUTION_UNDECIDABLE)

    def test_repeated_target_is_undecidable(self):
        self.assertEqual(self._verdict("a OLD b OLD c"), sync.SUBSTITUTION_UNDECIDABLE)

    def test_every_undecidable_verdict_carries_a_distinct_reason(self):
        # The operator's next move differs per shape, so one shared reason would misdirect two
        # of the three.
        reasons = {
            sync.classify_substitution(content, "OLD", "NEW")[1]
            for content in ("a b", "a OLD b NEW c", "a OLD b OLD c")
        }
        self.assertEqual(len(reasons), 3)
        self.assertTrue(all(r for r in reasons))

    def test_appliable_verdicts_carry_no_reason(self):
        for content in ("a OLD b", "a NEW b"):
            self.assertIsNone(sync.classify_substitution(content, "OLD", "NEW")[1], content)


class VerifyLocalCorrectionsTest(unittest.TestCase):
    """The pre-flight pass: every registered correction checked before the first write."""

    def _upstream(self, skills):
        # A stand-in for the clone: one directory per skill, each with the SKILL.md body given.
        root = Path(tempfile.mkdtemp())
        for name, body in skills.items():
            (root / name).mkdir()
            if body is not None:
                (root / name / "SKILL.md").write_text(body, encoding="utf-8")
            write_registered_files(root / name, name)
        return root

    def _faithful_upstream(self):
        # Every registered skill present, carrying every target the registries expect.
        skills = {}
        for name in (
            set(sync.SKILL_SUBSTITUTIONS) | set(sync.SKILL_FILE_SUBSTITUTIONS) | set(sync.SKILL_FOOTERS)
        ):
            targets = sync.SKILL_SUBSTITUTIONS.get(name, [])
            skills[name] = "\n\n".join(t for t, _ in targets) + "\n# skill\n"
        return skills

    def test_faithful_upstream_passes(self):
        skills = self._faithful_upstream()
        root = self._upstream(skills)
        sync.verify_local_corrections(str(root), sorted(skills))

    def test_renamed_skill_is_reported(self):
        # The live instance: upstream renamed gke-tpu-metrics-monitoring to
        # gke-ai-troubleshooting-tpu-metrics-monitoring. A registry entry left on the old name
        # is a correction that will never be applied again, and the prune loop would have
        # deleted the local directory with the sync still exiting 0.
        skills = self._faithful_upstream()
        dropped = sorted(sync.SKILL_FOOTERS)[0]
        del skills[dropped]
        root = self._upstream(skills)
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills))
        self.assertIn(dropped, str(caught.exception))
        self.assertIn("renamed or removed", str(caught.exception))

    def test_entry_for_an_overlay_mirrored_skill_is_reported(self):
        # The entry would never apply: scripts/skill_overlay.py writes that skill, not this script.
        skills = self._faithful_upstream()
        mirrored = sorted(sync.SKILL_FOOTERS)[0]
        root = self._upstream(skills)
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills), skip={mirrored})
        self.assertIn(mirrored, str(caught.exception))
        self.assertIn(sync.SKILL_OVERLAY_LOCK, str(caught.exception))

    def test_drifted_target_is_reported(self):
        skills = self._faithful_upstream()
        skills["gke-basics"] = skills["gke-basics"].replace("   * Always", "   - Always")
        root = self._upstream(skills)
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills))
        self.assertIn("gke-basics", str(caught.exception))

    def test_upstream_adopting_the_replacement_is_not_drift(self):
        # Upstream fixing the defect itself leaves the replacement in place; apply_substitutions
        # skips it, so the pre-flight must not call that a failure.
        skills = self._faithful_upstream()
        skills["gke-basics"] = skills["gke-basics"].replace(
            sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET, sync.GKE_BASICS_NEW_CREDENTIALS_SNIPPET
        )
        root = self._upstream(skills)
        sync.verify_local_corrections(str(root), sorted(skills))

    def test_target_and_replacement_both_present_is_reported(self):
        # Accepting a pair because the replacement is present, without checking that the target
        # is gone, reopens the hole this pre-flight exists to close: apply_substitutions skips a
        # pair whose replacement it sees, so the target ships uncorrected under exit 0.
        skills = self._faithful_upstream()
        skills["gke-basics"] += "\n\n" + sync.GKE_BASICS_NEW_CREDENTIALS_SNIPPET
        root = self._upstream(skills)
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills))
        self.assertIn("gke-basics", str(caught.exception))
        self.assertIn("the target and the replacement are both present", str(caught.exception))

    def test_repeated_target_is_reported(self):
        # Present is not enough: a pair is applied once, so a target upstream repeats would
        # have its later copies written into the tree uncorrected under exit 0.
        skills = self._faithful_upstream()
        skills["gke-basics"] += "\n\nAnd again:\n\n" + sync.GKE_BASICS_OLD_CREDENTIALS_SNIPPET
        root = self._upstream(skills)
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills))
        self.assertIn("gke-basics", str(caught.exception))
        self.assertIn("occurs 2 times", str(caught.exception))

    def test_earlier_pair_rewriting_a_later_target_is_reported(self):
        # apply_substitutions classifies each pair against content the earlier pairs have
        # already rewritten. A pre-flight that classified every pair against the pristine clone
        # would accept this registry and let the backstop raise after the tree is half-written.
        skill = "gke-two-pair"
        pairs = [("first old", "first new, then second old"), ("second old", "second new")]
        root = self._upstream({skill: "first old\n\nsecond old\n"})
        with mock.patch.dict(sync.SKILL_SUBSTITUTIONS, {skill: pairs}, clear=True), mock.patch.dict(
            sync.SKILL_FOOTERS, {}, clear=True
        ):
            with self.assertRaises(sync.UpstreamDriftError) as caught:
                sync.verify_local_corrections(str(root), [skill])
        self.assertIn(skill, str(caught.exception))
        self.assertIn("occurs 2 times", str(caught.exception))

    def test_every_drift_is_reported_at_once(self):
        # One re-clone per drift would be the cost of reporting only the first.
        skills = self._faithful_upstream()
        skills["gke-basics"] = skills["gke-basics"].replace("   * Always", "   - Always")
        skills["gke-workload-security"] = "# nothing the registry expects\n"
        root = self._upstream(skills)
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills))
        message = str(caught.exception)
        self.assertIn("gke-basics", message)
        self.assertIn("gke-workload-security", message)

    def test_drifted_file_target_is_reported(self):
        skills = self._faithful_upstream()
        root = self._upstream(skills)
        relpath = sorted(sync.SKILL_FILE_SUBSTITUTIONS["gke-basics"])[0]
        (root / "gke-basics" / relpath).write_text("# rewritten upstream\n", encoding="utf-8")
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills))
        self.assertIn("SKILL_FILE_SUBSTITUTIONS", str(caught.exception))
        self.assertIn(relpath, str(caught.exception))
        self.assertIn("target snippet not found", str(caught.exception))

    def test_missing_registered_file_upstream_is_reported(self):
        skills = self._faithful_upstream()
        root = self._upstream(skills)
        relpath = sorted(sync.SKILL_FILE_SUBSTITUTIONS["gke-basics"])[0]
        (root / "gke-basics" / relpath).unlink()
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills))
        self.assertIn(f"upstream skill has no {relpath}", str(caught.exception))

    def test_missing_skill_md_upstream_is_reported(self):
        skills = self._faithful_upstream()
        skills["gke-basics"] = None
        root = self._upstream(skills)
        with self.assertRaises(sync.UpstreamDriftError) as caught:
            sync.verify_local_corrections(str(root), sorted(skills))
        self.assertIn("no SKILL.md", str(caught.exception))

    def test_every_registered_skill_is_covered(self):
        # The pre-flight is only a guard if it reads every registry.
        skills = self._faithful_upstream()
        for name in sorted(
            set(sync.SKILL_SUBSTITUTIONS) | set(sync.SKILL_FILE_SUBSTITUTIONS) | set(sync.SKILL_FOOTERS)
        ):
            missing = {k: v for k, v in skills.items() if k != name}
            root = self._upstream(missing)
            with self.assertRaises(sync.UpstreamDriftError, msg=name):
                sync.verify_local_corrections(str(root), sorted(missing))


class SyncExitStatusTest(unittest.TestCase):
    """main()'s contract: drift exits non-zero, and a clean run exits 0.

    The headline behaviour is the exit status, not the raise: a later change that reports
    every drift and carries on would leave the unit tests above green.
    """

    SCRIPT = Path(__file__).resolve().parent / "sync-upstream-skills.py"

    def _fixture_upstream(self, mutate=None, rename=None, extra=()):
        # A git repo the script can `git clone --depth 1`, holding the skills the registries
        # name, plus any `extra` ones. Cloning a local path keeps the test off the network.
        root = Path(tempfile.mkdtemp())
        skills_dir = root / "skills" / "cloud"
        skills_dir.mkdir(parents=True)
        for name in (
            set(sync.SKILL_SUBSTITUTIONS) | set(sync.SKILL_FILE_SUBSTITUTIONS) | set(sync.SKILL_FOOTERS) | set(extra)
        ):
            targets = sync.SKILL_SUBSTITUTIONS.get(name, [])
            body = "\n\n".join(t for t, _ in targets) + "\n# skill\n"
            if mutate:
                body = mutate(name, body)
            dir_name = rename(name) if rename else name
            (skills_dir / dir_name).mkdir()
            (skills_dir / dir_name / "SKILL.md").write_text(body, encoding="utf-8")
            write_registered_files(skills_dir / dir_name, name)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "add", "-A"],
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture"],
        ):
            subprocess.run(cmd, cwd=root, check=True, env=env)
        return root

    def _run_sync(self, upstream, repo_root, merge_streams=False):
        # The script writes into <its parent>/.., so it is copied under a scratch root. Every
        # subprocess case goes through here: a second copy of the patch below is a second place
        # the guard on it can be left off, which is how the merged-streams case came to run
        # without one.
        scripts_dir = repo_root / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script = scripts_dir / "sync-upstream-skills.py"
        # Matched on the assignment rather than the literal so that requoting or reformatting
        # the URL does not silently leave the real upstream in place, and asserted so that a
        # rename does not either: an unpatched copy clones github.com from a unit test.
        patched, patches = re.subn(
            r"^UPSTREAM_REPO = .*$",
            lambda _: f"UPSTREAM_REPO = {str(upstream)!r}",
            self.SCRIPT.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
        self.assertEqual(
            patches, 1, "UPSTREAM_REPO assignment not found; the run would clone the real upstream"
        )
        script.write_text(patched, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_streams else subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )

    def test_clean_upstream_exits_zero(self):
        repo_root = Path(tempfile.mkdtemp())
        result = self._run_sync(self._fixture_upstream(), repo_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Synchronization complete!", result.stdout)

    def test_skill_with_an_overlay_lock_is_left_alone(self):
        # A skill scripts/skill_overlay.py mirrors keeps its upstream.lock in its overlay. The
        # sync must neither overwrite it (it is in upstream) nor prune it (were it not).
        repo_root = Path(tempfile.mkdtemp())
        mirrored = "gke-unregistered-and-mirrored"
        upstream = self._fixture_upstream(extra=[mirrored])
        for name in (mirrored, "gke-only-in-the-overlay"):
            overlay = repo_root / sync.SKILL_OVERLAY_ROOT / name
            overlay.mkdir(parents=True)
            (overlay / sync.SKILL_OVERLAY_LOCK).write_text("commit: x\nsha256: y\n", encoding="utf-8")
            skill = repo_root / "agents" / "platform" / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("ours\n", encoding="utf-8")
        result = self._run_sync(upstream, repo_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skipping 2 skill(s) mirrored by scripts/skill_overlay.py", result.stdout)
        for name in (mirrored, "gke-only-in-the-overlay"):
            skill_md = repo_root / "agents" / "platform" / "skills" / name / "SKILL.md"
            self.assertEqual(skill_md.read_text(encoding="utf-8"), "ours\n")

    def test_drifted_upstream_exits_one_and_writes_nothing(self):
        def drift(name, body):
            return body.replace("   * Always", "   - Always")

        repo_root = Path(tempfile.mkdtemp())
        # A skill the fixture upstream does not ship, so the prune loop would delete it. An
        # empty repo_root cannot tell an abort before the prune from one after it: both leave
        # nothing behind. This one survives only if the abort came first.
        stale = repo_root / "agents" / "platform" / "skills" / "gke-removed-upstream"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("# stale\n", encoding="utf-8")

        result = self._run_sync(self._fixture_upstream(mutate=drift), repo_root)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Synchronization complete!", result.stdout)
        self.assertIn("nothing written", result.stderr)
        self.assertIn("gke-basics", result.stderr)
        # Neither destructive step ran: nothing pruned, nothing copied.
        self.assertTrue(stale.exists())
        self.assertEqual(
            sorted(p.name for p in (repo_root / "agents" / "platform" / "skills").iterdir()),
            ["gke-removed-upstream"],
        )

    def test_progress_log_precedes_the_abort_when_streams_are_merged(self):
        # Under `> log 2>&1` a block-buffered stdout flushes after the unbuffered stderr, which
        # printed the abort above the listing naming the skill it aborted on.
        def drift(name, body):
            return body.replace("   * Always", "   - Always")

        repo_root = Path(tempfile.mkdtemp())
        merged = self._run_sync(
            self._fixture_upstream(mutate=drift), repo_root, merge_streams=True
        ).stdout
        self.assertLess(merged.index("Discovered"), merged.index("Error: Synchronization aborted"))

    def test_repeated_target_exits_one_and_writes_nothing(self):
        # A target upstream repeats is applied once and the later copies ship uncorrected, so
        # the count is part of what makes a pair appliable, not just the presence.
        repeated = sync.SKILL_SUBSTITUTIONS["gke-manifest-generation"][1][0]

        def repeat(name, body):
            if name != "gke-manifest-generation":
                return body
            return body + "\nSecond example: " + repeated + "\n"

        repo_root = Path(tempfile.mkdtemp())
        result = self._run_sync(self._fixture_upstream(mutate=repeat), repo_root)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Synchronization complete!", result.stdout)
        self.assertIn("occurs 2 times", result.stderr)
        self.assertFalse((repo_root / "agents").exists())


    def test_upstream_without_prefixed_skills_exits_one_and_prunes_nothing(self):
        # Upstream renaming or moving the skills makes discovery empty, which is not a warning:
        # the prune removes every local skill not discovered, so a run that carried on would
        # delete all of them.
        local = Path(tempfile.mkdtemp()) / "agents" / "platform" / "skills" / "gke-basics"
        local.mkdir(parents=True)
        (local / "SKILL.md").write_text("# local\n", encoding="utf-8")
        repo_root = local.parents[3]

        result = self._run_sync(
            self._fixture_upstream(rename=lambda n: n.replace(sync.SKILL_PREFIX, "moved-")),
            repo_root,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing written", result.stderr)
        self.assertIn(sync.SKILL_PREFIX, result.stderr)
        self.assertTrue(local.exists())


class WrittenPathspecsTest(unittest.TestCase):
    """The pathspecs must cover every directory the sync writes and no other."""

    REPO_ROOT = Path(__file__).resolve().parent.parent

    def _pathspecs(self):
        return sync.written_pathspecs(str(self.REPO_ROOT))

    def _covered(self, relative):
        # Git's reading of the list: some include matches and no exclude does.
        includes = [p for p in self._pathspecs() if not p.startswith(":(exclude)")]
        excludes = [p.removeprefix(":(exclude)") for p in self._pathspecs() if p.startswith(":(exclude)")]
        return any(fnmatch.fnmatchcase(relative, p) for p in includes) and relative not in excludes

    def test_every_pathspec_is_scoped_to_one_agent_and_the_prefix(self):
        mirrored = sync.overlay_mirrored_skills(str(self.REPO_ROOT))
        self.assertEqual(
            self._pathspecs(),
            [f"agents/{agent}/skills/{sync.SKILL_PREFIX}*" for agent in sync.target_agents()]
            + [f":(exclude){sync.SKILL_OVERLAY_SKILLS}/{name}" for name in sorted(mirrored)],
        )
        self.assertTrue(self._pathspecs(), "the sync writes somewhere")

    def test_overlay_mirrored_skills_are_excluded(self):
        repo_root = Path(tempfile.mkdtemp())
        overlay = repo_root / sync.SKILL_OVERLAY_ROOT / "gke-mirrored"
        overlay.mkdir(parents=True)
        (overlay / sync.SKILL_OVERLAY_LOCK).write_text("commit: x\nsha256: y\n", encoding="utf-8")
        self.assertIn(
            f":(exclude){sync.SKILL_OVERLAY_SKILLS}/gke-mirrored", sync.written_pathspecs(str(repo_root))
        )

    def test_target_agents_covers_the_defaults_and_every_override(self):
        agents = sync.target_agents()
        self.assertEqual(agents, sorted(set(agents)))
        for agent in sync.DEFAULT_TARGET_AGENTS:
            self.assertIn(agent, agents)
        for override in sync.SKILL_AGENT_OVERRIDES.values():
            for agent in override:
                self.assertIn(agent, agents)

    def test_pathspecs_match_exactly_the_skills_in_the_tree_the_sync_writes(self):
        # Read against the real tree rather than a fixture: the skills the recovery commands
        # must spare are the ones that actually sit beside the synced ones.
        mirrored = sync.overlay_mirrored_skills(str(self.REPO_ROOT))
        matched, written = set(), set()
        for agent_dir in sorted((self.REPO_ROOT / "agents").iterdir()):
            skills_dir = agent_dir / "skills"
            if not skills_dir.is_dir():
                continue
            for skill in sorted(skills_dir.iterdir()):
                if not skill.is_dir():
                    continue
                relative = f"agents/{agent_dir.name}/skills/{skill.name}"
                if self._covered(relative):
                    matched.add(relative)
                if (
                    agent_dir.name in sync.target_agents()
                    and skill.name.startswith(sync.SKILL_PREFIX)
                    and skill.name not in mirrored
                ):
                    written.add(relative)
        self.assertTrue(written, "no synced skills found in the tree")
        self.assertEqual(matched, written)

    def test_recovery_commands_name_only_the_pathspecs_the_sync_writes(self):
        # An operator runs these verbatim mid-abort. A pathspec one level broader reaches the
        # skills this repository maintains rather than syncs, and discards uncommitted work on
        # them that no run of this script could have produced.
        message = sync.local_correction_lost_message("some detail", str(self.REPO_ROOT))
        commands = [line.strip() for line in message.splitlines() if line.startswith("  git ")]
        pathspecs = " ".join(f"'{p}'" for p in self._pathspecs())
        self.assertEqual(commands, [f"git checkout -- {pathspecs}", f"git clean -fd -- {pathspecs}"])
        # Every skill the sync does not write survives both commands.
        mirrored = sync.overlay_mirrored_skills(str(self.REPO_ROOT))
        for agent_dir in sorted((self.REPO_ROOT / "agents").iterdir()):
            for skill in sorted((agent_dir / "skills").glob("*")):
                relative = f"agents/{agent_dir.name}/skills/{skill.name}"
                if (
                    agent_dir.name in sync.target_agents()
                    and skill.name.startswith(sync.SKILL_PREFIX)
                    and skill.name not in mirrored
                ):
                    continue
                self.assertFalse(
                    self._covered(relative),
                    f"the recovery commands would discard {relative}, which the sync never writes",
                )


class AbortTest(unittest.TestCase):
    def test_exits_one_writing_the_message_to_stderr_after_flushing_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            print("progress", end="")
            with self.assertRaises(SystemExit) as caught:
                sync.abort("the message")
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(err.getvalue(), "the message\n")
        self.assertEqual(out.getvalue(), "progress")


if __name__ == "__main__":
    unittest.main()

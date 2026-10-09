"""Scenario tests for scripts/skill_overlay.py.

Each test builds a throwaway upstream repository (tags v1-v6) and a throwaway downstream
repository holding a copy of the tool, then runs the tool as a contributor would. Nothing
reaches the network: SKILL_OVERLAY_UPSTREAM points the tool at the local upstream.
Run: python3 -m unittest scripts.test_skill_overlay
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "skill_overlay.py"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    # No user ignore file, as in the tool: the fixtures must commit every file they write.
    "GIT_CONFIG_COUNT": "3",
    "GIT_CONFIG_KEY_0": "core.excludesFile",
    "GIT_CONFIG_VALUE_0": os.devnull,
    "GIT_CONFIG_KEY_1": "gc.autoDetach",
    "GIT_CONFIG_VALUE_1": "false",
    "GIT_CONFIG_KEY_2": "maintenance.autoDetach",
    "GIT_CONFIG_VALUE_2": "false",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

BASICS_V1 = """# Basics

## Cluster Credentials

Always specify the cluster's region when fetching credentials:

```bash
gcloud container clusters get-credentials CLUSTER --region=REGION --quiet
```

## Checking Node Health

1. List the nodes.
2. Check that the node can recieve new pods.
"""


def git(cwd, *args, check=True):
    # Without inherited GIT_DIR and the like, as in the tool: a fixture must not commit into the
    # repository the suite was started from.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", *args], cwd=cwd, env=dict(env, **GIT_ENV),
                          capture_output=True, text=True, check=check)


def build_upstream(root):
    """v1 base; v2 nearby edit; v3 adopts the typo fix; v4 edits the patched line; v5 new skill; v6 renames a reference file."""
    skills = root / "skills" / "cloud"
    (skills / "basics").mkdir(parents=True)
    (skills / "storage").mkdir(parents=True)
    (skills / "basics" / "SKILL.md").write_text(BASICS_V1)
    (skills / "basics" / "references").mkdir()
    (skills / "basics" / "references" / "cli.md").write_text("# CLI\n\nget-credentials --region\n\nmore\n")
    (skills / "storage" / "SKILL.md").write_text("# Storage\n\nCreate a PVC.\n")
    git(root, "init", "-q", "-b", "main")

    def commit(tag):
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", tag)
        git(root, "tag", tag)

    commit("v1")
    md = skills / "basics" / "SKILL.md"
    md.write_text(md.read_text().replace("Always specify the cluster's region",
                                         "Always pass the cluster's region explicitly"))
    commit("v2")
    md.write_text(md.read_text().replace("recieve", "receive"))
    commit("v3")
    md.write_text(md.read_text().replace("--region=REGION --quiet", "--region=REGION --project=PROJECT --quiet"))
    commit("v4")
    (skills / "gke-network").mkdir()
    (skills / "gke-network" / "SKILL.md").write_text("# Network\n")
    commit("v5")
    git(root, "mv", "skills/cloud/basics/references/cli.md", "skills/cloud/basics/references/commands.md")
    commit("v6")


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream = self.tmp / "upstream"
        self.upstream.mkdir()
        build_upstream(self.upstream)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(TOOL, self.repo / "scripts" / "skill_overlay.py")
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".gitignore").write_text(".skill-sync/\n")
        self.env = dict(os.environ, SKILL_OVERLAY_UPSTREAM=str(self.upstream), **GIT_ENV)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_tool(self, *args, expect=0):
        res = subprocess.run([sys.executable, "scripts/skill_overlay.py", *args], cwd=self.repo,
                             env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, expect, res.stdout + res.stderr)
        return res.stdout + res.stderr

    def skill(self, name="basics"):
        return self.repo / "agents" / "platform" / "skills" / name / "SKILL.md"

    def overlay(self, name="basics"):
        return self.repo / "agents" / "platform" / "skill-overlays" / name

    def adopt_with_two_patches(self):
        self.run_tool("sync", "basics", "--ref", "v1")
        self.run_tool("sync", "storage", "--ref", "v1")
        md = self.skill()
        md.write_text(md.read_text().replace("--region=REGION --quiet", "--location=LOCATION --quiet"))
        self.run_tool("refresh", "basics", "--message", "use location")
        md.write_text(md.read_text().replace("recieve", "receive"))
        self.run_tool("refresh", "basics", "--message", "fix typo")
        self.assertEqual([p.name for p in sorted(self.overlay().glob("*.patch"))],
                         ["0001-use-location.patch", "0002-fix-typo.patch"])

    def test_check_passes_after_refresh(self):
        self.adopt_with_two_patches()
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_check_passes_with_a_temp_dir_inside_a_work_tree(self):
        self.adopt_with_two_patches()
        (self.repo / ".skill-sync").mkdir(exist_ok=True)
        self.env["TMPDIR"] = str(self.repo / ".skill-sync")
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_appended_section_without_a_blank_line_records_no_patch(self):
        self.adopt_with_two_patches()
        self.skill().write_text(self.skill().read_text() + "<!-- kube-agents: local addition -->\n\nOurs.\n")
        out = self.run_tool("refresh", "basics")
        self.assertIn("no change outside append.md", out)
        self.assertEqual(len(list(self.overlay().glob("*.patch"))), 2)
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_hand_edit_without_patch_fails_check(self):
        self.adopt_with_two_patches()
        self.skill().write_text(self.skill().read_text() + "extra line\n")
        out = self.run_tool("check", expect=1)
        self.assertIn("skills-refresh", out)

    def test_hand_edit_to_upstream_copy_fails_checksum(self):
        self.adopt_with_two_patches()
        copy = self.repo / "third_party" / "google-skills" / "storage" / "SKILL.md"
        copy.write_text(copy.read_text() + "x\n")
        self.assertIn("edited by hand", self.run_tool("check", expect=1))

    def test_forged_lock_fails_upstream_comparison(self):
        self.adopt_with_two_patches()
        copy = self.repo / "third_party" / "google-skills" / "storage" / "SKILL.md"
        copy.write_text(copy.read_text() + "x\n")
        lock = self.overlay("storage") / "upstream.lock"
        commit = lock.read_text().splitlines()[0].split(": ")[1]
        res = subprocess.run([sys.executable, "-c",
                              "import skill_overlay as m;"
                              f"m.write_lock('storage','{commit}',m.tree_sha256(m.copy_dir('storage')))"],
                             cwd=self.repo / "scripts", env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.run_tool("generate", "storage")
        self.assertIn("ok: 2", self.run_tool("check"))
        self.assertIn("differs from upstream", self.run_tool("verify-upstream", expect=1))

    def test_nearby_upstream_edit_merges(self):
        self.adopt_with_two_patches()
        self.run_tool("sync", "basics", "--ref", "v2")
        text = self.skill().read_text()
        self.assertIn("Always pass the cluster's region explicitly", text)
        self.assertIn("--location=LOCATION", text)
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_adopted_patch_is_retired(self):
        self.adopt_with_two_patches()
        out = self.run_tool("sync", "basics", "--ref", "v3")
        self.assertIn("retired 0002-fix-typo.patch", out)
        self.assertEqual([p.name for p in self.overlay().glob("*.patch")], ["0001-use-location.patch"])
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_edit_to_patched_line_stops_then_continues(self):
        self.adopt_with_two_patches()
        out = self.run_tool("sync", "basics", "--ref", "v4", expect=2)
        self.assertIn("CONFLICT: 0001-use-location.patch", out)
        scratch = self.repo / ".skill-sync" / "basics" / "SKILL.md"
        lines, keep = [], True
        for line in scratch.read_text().splitlines(keepends=True):
            if line.startswith("<<<<<<<"):
                lines.append("gcloud container clusters get-credentials CLUSTER --location=LOCATION --project=PROJECT --quiet\n")
                keep = False
            elif line.startswith(">>>>>>>"):
                keep = True
            elif keep:
                lines.append(line)
        scratch.write_text("".join(lines))
        self.run_tool("continue", "basics")
        self.assertIn("--location=LOCATION --project=PROJECT", self.skill().read_text())
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_status_and_adopting_a_new_skill(self):
        self.adopt_with_two_patches()
        out = self.run_tool("status")
        self.assertIn("behind      basics", out)
        self.assertIn("not mirrored gke-network", out)
        self.run_tool("sync", "gke-network")
        self.assertIn("ok: 3", self.run_tool("check"))
        self.assertIn("up to date  gke-network", self.run_tool("status"))

    def test_local_skill_is_never_touched_and_blocks_adoption_of_its_name(self):
        local = self.skill("gke-network")
        local.parent.mkdir(parents=True)
        local.write_text("# our own network skill\n")
        self.run_tool("sync", "basics", "--ref", "v1")
        self.assertIn("ships here without a lock", self.run_tool("status"))
        self.assertIn("not mirrored", self.run_tool("sync", "gke-network", expect=1))
        self.assertEqual(local.read_text(), "# our own network skill\n")
        self.assertIn("ok: 1", self.run_tool("check"))

    def test_fold_into_existing_patch_and_overlap_warning(self):
        self.adopt_with_two_patches()
        md = self.skill()
        md.write_text(md.read_text().replace("--location=LOCATION --quiet", "--location=$LOCATION --quiet"))
        self.assertIn("lines that 0001-use-location.patch introduced", self.run_tool("refresh", "basics"))
        for p in self.overlay().glob("0003-*.patch"):
            p.unlink()
        self.run_tool("generate", "basics")
        md.write_text(md.read_text().replace("--location=LOCATION --quiet", "--location=$LOCATION --quiet"))
        self.run_tool("refresh", "basics", "--patch", "0001")
        self.assertEqual(len(list(self.overlay().glob("*.patch"))), 2)
        self.assertIn("--location=$LOCATION", (self.overlay() / "0001-use-location.patch").read_text())
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_concurrent_unrelated_patches_merge_without_conflict(self):
        self.adopt_with_two_patches()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "alice")
        md = self.skill()
        md.write_text(md.read_text().replace("# Basics", "# Basics (alice)"))
        self.run_tool("refresh", "basics", "--message", "alice title")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "alice")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "checkout", "-q", "-b", "bob")
        md.write_text(md.read_text().replace("1. List the nodes.", "1. List the nodes (bob)."))
        self.run_tool("refresh", "basics", "--message", "bob step")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "bob")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-edit", "alice")
        merged = git(self.repo, "merge", "-q", "--no-edit", "bob", check=False)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_import_keeps_the_shipped_skill_and_refresh_records_the_difference(self):
        shipped = self.skill()
        shipped.parent.mkdir(parents=True)
        shipped.write_text(BASICS_V1.replace("--region=REGION --quiet", "--location=LOCATION --quiet")
                           + "\n<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->\n"
                           + "\n## Our footer\n\nProvision the Cluster Agent.\n")
        before = shipped.read_text()
        self.assertIn("differ from upstream", self.run_tool("import", "basics", "--ref", "v1"))
        self.assertEqual(shipped.read_text(), before)
        self.run_tool("check", expect=1)
        self.run_tool("refresh", "basics", "--message", "use location")
        self.assertEqual(shipped.read_text(), before)
        self.assertTrue((self.overlay() / "append.md").read_text().startswith("<!-- kube-agents: local addition"))
        patch = (self.overlay() / "0001-use-location.patch").read_text()
        self.assertIn("+gcloud container clusters get-credentials CLUSTER --location=LOCATION --quiet", patch)
        self.assertNotIn("Our footer", patch)
        self.assertIn("ok: 1", self.run_tool("check"))

    def test_import_refuses_a_skill_that_is_not_shipped(self):
        self.assertIn("does not exist", self.run_tool("import", "basics", "--ref", "v1", expect=1))

    def test_continue_refuses_leftover_markers_and_ignores_stray_files(self):
        self.adopt_with_two_patches()
        self.run_tool("sync", "basics", "--ref", "v4", expect=2)
        scratch = self.repo / ".skill-sync" / "basics"
        (scratch / "SKILL.md.orig").write_text("merge tool backup\n")
        self.assertIn("conflict markers remain", self.run_tool("continue", "basics", expect=1))
        text = (scratch / "SKILL.md").read_text()
        start, end = text.index("<<<<<<<"), text.index(">>>>>>>")
        end = text.index("\n", end) + 1
        resolved = "gcloud container clusters get-credentials CLUSTER --location=LOCATION --project=PROJECT --quiet\n"
        (scratch / "SKILL.md").write_text(text[:start] + resolved + text[end:])
        self.run_tool("continue", "basics")
        self.assertFalse((self.skill().parent / "SKILL.md.orig").exists())
        self.assertNotIn("<<<<<<<", self.skill().read_text())

    def test_continue_refuses_markers_in_a_staged_file(self):
        self.adopt_with_two_patches()
        self.run_tool("sync", "basics", "--ref", "v4", expect=2)
        scratch = self.repo / ".skill-sync" / "basics"
        git(scratch, "add", "SKILL.md")
        self.assertIn("conflict markers remain", self.run_tool("continue", "basics", expect=1))

    def test_conflict_resolved_to_upstream_reports_the_patch_dropped(self):
        self.adopt_with_two_patches()
        self.run_tool("sync", "basics", "--ref", "v4", expect=2)
        scratch = self.repo / ".skill-sync" / "basics"
        git(scratch, "checkout", "--ours", "SKILL.md")
        out = self.run_tool("continue", "basics")
        self.assertIn("dropped 0001-use-location.patch while resolving", out)

    def test_sync_refuses_unrecorded_edits(self):
        self.adopt_with_two_patches()
        self.skill().write_text(self.skill().read_text() + "not yet recorded\n")
        self.assertIn("has changes no patch records", self.run_tool("sync", "basics", "--ref", "v2", expect=1))
        self.assertIn("not yet recorded", self.skill().read_text())
        self.assertFalse((self.repo / ".skill-sync" / "basics").exists())

    def test_binary_file_round_trips_through_a_patch(self):
        self.adopt_with_two_patches()
        (self.skill().parent / "logo.bin").write_bytes(bytes(range(256)))
        self.run_tool("refresh", "basics", "--message", "add logo")
        self.assertEqual((self.skill().parent / "logo.bin").read_bytes(), bytes(range(256)))
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_user_ignore_file_does_not_drop_a_skill_file(self):
        self.adopt_with_two_patches()
        xdg = self.tmp / "xdg"
        (xdg / "git").mkdir(parents=True)
        (xdg / "git" / "ignore").write_text("*.local.md\n")
        self.env["XDG_CONFIG_HOME"] = str(xdg)
        (self.skill().parent / "extra.local.md").write_text("kept\n")
        self.run_tool("refresh", "basics", "--message", "add reference")
        self.assertEqual((self.skill().parent / "extra.local.md").read_text(), "kept\n")
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_crlf_skill_needs_no_patch_and_keeps_its_line_endings(self):
        md = self.upstream / "skills" / "cloud" / "storage" / "SKILL.md"
        md.write_bytes(b"# Storage\r\n\r\nCreate a PVC.\r\n")
        git(self.upstream, "commit", "-q", "-am", "crlf")
        self.run_tool("sync", "storage")
        (self.overlay("storage") / "append.md").write_bytes(
            b"<!-- kube-agents: local addition -->\n\nOurs.\n")
        self.run_tool("generate", "storage")
        self.assertIn("no change outside append.md", self.run_tool("refresh", "storage"))
        self.assertEqual(list(self.overlay("storage").glob("*.patch")), [])
        self.assertTrue(self.skill("storage").read_bytes().startswith(b"# Storage\r\n"))
        self.assertIn("ok: 1", self.run_tool("check"))

    def test_lock_must_pin_a_full_commit(self):
        self.adopt_with_two_patches()
        lock = self.overlay("storage") / "upstream.lock"
        lock.write_text(lock.read_text().replace(lock.read_text().splitlines()[0], "commit: main"))
        self.assertIn("40-hex commit", self.run_tool("check", expect=1))

    def test_ambiguous_patch_prefix_is_refused(self):
        self.adopt_with_two_patches()
        shutil.copy(self.overlay() / "0002-fix-typo.patch", self.overlay() / "0002-copy.patch")
        self.assertIn("pass the full file name", self.run_tool("refresh", "basics", "--patch", "0002", expect=1))

    def test_overlay_changed_during_a_paused_sync_is_refused(self):
        self.adopt_with_two_patches()
        self.run_tool("sync", "basics", "--ref", "v4", expect=2)
        patch = self.overlay() / "0002-fix-typo.patch"
        patch.write_text(patch.read_text().replace("Why: TODO", "Why: edited meanwhile"))
        git(self.repo / ".skill-sync" / "basics", "checkout", "--theirs", "SKILL.md")
        self.assertIn("changed while the sync was paused", self.run_tool("continue", "basics", expect=1))
        # The refusal comes before the rebase moves on, so undoing the edit lets the sync finish.
        patch.write_text(patch.read_text().replace("Why: edited meanwhile", "Why: TODO"))
        self.assertIn("synced basics", self.run_tool("continue", "basics"))
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_continue_without_a_paused_sync_explains(self):
        (self.repo / ".skill-sync" / "basics").mkdir(parents=True)
        self.assertIn("holds no paused sync", self.run_tool("continue", "basics", expect=1))

    def test_skill_names_are_validated(self):
        self.assertIn("is not a skill name", self.run_tool("continue", ".", expect=1))

    def test_removing_the_appended_section_is_explained(self):
        self.adopt_with_two_patches()
        (self.overlay() / "append.md").write_text("<!-- kube-agents: local addition -->\n\nOurs.\n")
        self.run_tool("generate", "basics")
        text = self.skill().read_text()
        self.skill().write_text(text[: text.index("<!-- kube-agents")])
        self.assertIn("no longer has the append.md section", self.run_tool("refresh", "basics"))

    def test_verify_upstream_skips_unless_a_copy_or_lock_changed(self):
        self.adopt_with_two_patches()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        # A change beside the lock, not to it: the path filter has to tell them apart.
        patch = self.overlay() / "0001-use-location.patch"
        patch.write_text(patch.read_text().replace("Why: TODO", "Why: unrelated edit"))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "unrelated")
        self.assertIn("skip:", self.run_tool("verify-upstream", "--changed-since", base))
        self.run_tool("sync", "basics", "--ref", "v2")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "sync")
        self.assertIn("ok: 2", self.run_tool("verify-upstream", "--changed-since", base))

    def test_os_junk_files_do_not_fail_the_check(self):
        self.adopt_with_two_patches()
        (self.repo / "third_party" / "google-skills" / "storage" / ".DS_Store").write_bytes(b"\0junk")
        (self.skill().parent / "SKILL.md.swp").write_bytes(b"\0junk")
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_missing_copy_is_reported_as_missing(self):
        self.adopt_with_two_patches()
        shutil.rmtree(self.repo / "third_party" / "google-skills" / "storage")
        self.assertIn("does not exist but upstream.lock does", self.run_tool("check", expect=1))

    def test_upstream_file_the_repo_ignores_is_refused(self):
        (self.repo / ".gitignore").write_text(".skill-sync/\n*.tgz\n")
        chart = self.upstream / "skills" / "cloud" / "storage" / "chart.tgz"
        chart.write_bytes(b"\0chart")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-q", "-m", "chart")
        out = self.run_tool("sync", "storage", expect=1)
        self.assertIn("chart.tgz", out)
        self.assertIn(".gitignore ignores", out)
        self.assertFalse((self.overlay("storage") / "upstream.lock").exists())
        # The refusal's own remedy, a negation, lets the sync through.
        (self.repo / ".gitignore").write_text(".skill-sync/\n*.tgz\n!third_party/google-skills/storage/**\n")
        self.assertIn("adopted storage", self.run_tool("sync", "storage"))

    def test_marker_lines_outside_the_conflicted_files_do_not_block_continue(self):
        self.run_tool("sync", "basics", "--ref", "v1")
        md = self.skill()
        md.write_text(md.read_text().replace("--region=REGION --quiet", "--location=LOCATION --quiet"))
        (md.parent / "references" / "merge.md").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
        self.run_tool("refresh", "basics", "--message", "use location")
        self.run_tool("sync", "basics", "--ref", "v4", expect=2)
        scratch = self.repo / ".skill-sync" / "basics"
        git(scratch, "checkout", "--theirs", "SKILL.md")
        self.assertIn("synced basics", self.run_tool("continue", "basics"))
        self.assertIn("<<<<<<< HEAD", (md.parent / "references" / "merge.md").read_text())

    def test_overlap_warning_names_the_patch_for_a_non_ascii_file(self):
        self.run_tool("sync", "basics", "--ref", "v1")
        cafe = self.skill().parent / "references" / "caf\u00e9.md"
        cafe.write_text("a\nb\nc\n")
        self.run_tool("refresh", "basics", "--message", "add cafe")
        cafe.write_text("a\nB\nc\n")
        self.assertIn("lines that 0001-add-cafe.patch introduced", self.run_tool("refresh", "basics", "--message", "edit"))

    def test_continue_refuses_markers_in_a_conflicted_file_with_a_space_or_non_ascii_name(self):
        refs = self.upstream / "skills" / "cloud" / "basics" / "references"
        names = ("a b.md", "caf\u00e9.md")
        for name in names:
            (refs / name).write_text("x\n")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-q", "-m", "named refs")
        base = git(self.upstream, "rev-parse", "HEAD").stdout.strip()
        for name in names:
            (refs / name).write_text("z\n")
        git(self.upstream, "commit", "-qam", "edit named refs")
        self.run_tool("sync", "basics", "--ref", base)
        for name in names:
            (self.skill().parent / "references" / name).write_text("y\n")
        self.run_tool("refresh", "basics", "--message", "edit named refs")
        self.run_tool("sync", "basics", expect=2)
        scratch = self.repo / ".skill-sync" / "basics"
        git(scratch, "add", "-A")
        out = self.run_tool("continue", "basics", expect=1)
        self.assertIn("conflict markers remain in references/a b.md, references/caf\u00e9.md", out)

    def test_patch_in_traditional_form_survives_a_sync(self):
        self.adopt_with_two_patches()
        patch = self.overlay() / "0001-use-location.patch"
        patch.write_text("".join(l for l in patch.read_text().splitlines(True) if not l.startswith("diff --git ")))
        self.run_tool("check")
        self.run_tool("sync", "basics", "--ref", "v2")
        self.assertEqual(patch.read_text().count("+++ b/SKILL.md"), 1)
        self.assertIn("ok: 2", self.run_tool("check"))

    def test_executable_bit_is_part_of_the_check(self):
        script = self.upstream / "skills" / "cloud" / "storage" / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-q", "-m", "script")
        self.run_tool("sync", "storage")
        self.assertIn("ok: 1", self.run_tool("check"))
        (self.skill("storage").parent / "run.sh").chmod(0o644)
        self.assertIn("executable bit differs", self.run_tool("check", expect=1))

    def test_patch_follows_an_upstream_rename(self):
        self.run_tool("sync", "basics", "--ref", "v1")
        ref = self.skill().parent / "references" / "cli.md"
        ref.write_text(ref.read_text().replace("--region", "--location"))
        self.run_tool("refresh", "basics", "--message", "location in cli reference")
        self.run_tool("sync", "basics", "--ref", "v6")
        renamed = self.skill().parent / "references" / "commands.md"
        self.assertIn("--location", renamed.read_text())
        self.assertFalse(ref.exists())
        self.assertIn("ok: 1", self.run_tool("check"))

    def test_append_adopted_upstream_is_reported(self):
        self.run_tool("sync", "storage", "--ref", "v1")
        (self.overlay("storage") / "append.md").write_text(
            "<!-- kube-agents: local addition -->\n\nNew closing note.\n")
        self.run_tool("generate", "storage")
        upstream_md = self.upstream / "skills" / "cloud" / "storage" / "SKILL.md"
        upstream_md.write_text(upstream_md.read_text() + "\nNew closing note.\n")
        git(self.upstream, "commit", "-q", "-am", "adopt note")
        self.assertIn("upstream now contains this text", self.run_tool("sync", "storage"))



def load_tool():
    spec = importlib.util.spec_from_file_location("skill_overlay", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Helpers(unittest.TestCase):
    """The pure helpers, in process, so coverage sees the module."""

    tool = load_tool()

    def test_strip_index_lines_keeps_binary_index_lines(self):
        diff = ("diff --git a/a.md b/a.md\nindex 111..222 100644\n--- a/a.md\n+++ b/a.md\n@@ -1 +1 @@\n-x\n+y\n"
                "diff --git a/b.bin b/b.bin\nindex 333..444 100644\nGIT binary patch\nliteral 1\n")
        out = self.tool.strip_index_lines(diff)
        self.assertNotIn("index 111..222", out)
        self.assertIn("index 333..444", out)

    def test_stage_all_sees_a_same_size_edit_with_unchanged_stat(self):
        # What Linux CI hit: git compares whole-second times, so a same-size edit copied in
        # with the old mtime looks unchanged to `git add` unless the index is rebuilt.
        repo = Path(tempfile.mkdtemp())
        try:
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "core.checkStat", "minimal")
            git(repo, "config", "core.trustctime", "false")
            f = repo / "a.md"
            f.write_text("x\n")
            old = f.stat().st_mtime - 10
            os.utime(f, (old, old))
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "x")
            f.write_text("y\n")
            os.utime(f, (old, old))
            self.tool.stage_all(repo)
            self.assertEqual(git(repo, "diff", "--cached", "--name-only").stdout.split(), ["a.md"])
        finally:
            shutil.rmtree(repo)

    def test_patch_header_stops_at_a_traditional_diff_and_not_inside_why(self):
        text = "Subject: s\n\nWhy: quotes diff --git here\nRetire-When: x\n\n--- a/SKILL.md\n+++ b/SKILL.md\n"
        self.assertEqual(self.tool.patch_header(text), "Subject: s\n\nWhy: quotes diff --git here\nRetire-When: x\n\n")

    def test_patch_header_keeps_a_prose_line_that_begins_with_dashes(self):
        text = "Subject: s\n\nWhy: first line\n--- see the upstream thread\nRetire-When: x\n\ndiff --git a/S b/S\n"
        self.assertEqual(self.tool.patch_header(text), text[: text.index("diff --git")])

    def test_split_append_keeps_crlf_body(self):
        body, appended = self.tool.split_append("a\r\n\n<!-- kube-agents: local addition -->\nZ\n", "a\r\n")
        self.assertEqual(body, "a\r\n")
        self.assertTrue(appended.startswith("<!-- kube-agents"))

    def test_slugify(self):
        self.assertEqual(self.tool.slugify("Use --location, please!"), "use-location-please")
        self.assertEqual(self.tool.slugify("!!!"), "change")

    def test_tree_sha256_covers_the_executable_bit(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "run.sh").write_text("echo\n")
            before = self.tool.tree_sha256(d)
            (d / "run.sh").chmod(0o755)
            self.assertNotEqual(before, self.tool.tree_sha256(d))
        finally:
            shutil.rmtree(d)

    def test_separator_for(self):
        self.assertEqual(self.tool.separator_for("a\n\n"), "")
        self.assertEqual(self.tool.separator_for("a\n"), "\n")
        self.assertEqual(self.tool.separator_for("a"), "\n\n")

    def test_git_env_disables_background_gc_and_maintenance(self):
        env = self.tool.GIT_ENV
        count = int(env["GIT_CONFIG_COUNT"])
        config_pairs = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(count)
        }
        self.assertEqual(config_pairs.get("gc.autoDetach"), "false")
        self.assertEqual(config_pairs.get("maintenance.autoDetach"), "false")

    def test_git_helper_applies_gc_and_maintenance_configs(self):
        d = Path(tempfile.mkdtemp())
        try:
            self.tool.git(["init", "-q"], cwd=d)
            self.assertEqual(self.tool.git_out(["config", "--get", "gc.autoDetach"], cwd=d), "false")
            self.assertEqual(self.tool.git_out(["config", "--get", "maintenance.autoDetach"], cwd=d), "false")
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()

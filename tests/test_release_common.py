"""Unit tests for scripts/release/common.sh helper routines and registries.

Tests boolean parsing, SemVer validation, SemVer comparison, repository and registry prefix
resolution, Git tag lookup, and declarative release registries.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    FALSY_BOOLEAN_INPUTS,
    MOCK_CUSTOM_ORG,
    MOCK_CUSTOM_REGISTRY_PREFIX,
    MOCK_CUSTOM_REPO,
    MOCK_CUSTOM_TARGET_REPO,
    MOCK_DEFAULT_REGISTRY_PREFIX,
    MOCK_DEFAULT_RELEASE_REPO,
    TRUTHY_BOOLEAN_INPUTS,
    VALID_GA_RELEASE_TAGS,
    create_minimal_tools_bin,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_CANDIDATE_RELEASE_IMAGES,
    MOCK_GROWN_RELEASE_IMAGES,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_SAMPLE_COMMIT_SHA,
    MOCK_SAMPLE_SHORT_SHA,
    MOCK_LINE_PATCH_RELEASE_TAG,
    MOCK_TARGET_RELEASE_LINE,
    MOCK_TARGET_RELEASE_TAG,
    REQUIRED_RELEASE_IMAGES_PATH,
    commit_required_release_images,
    create_mock_docker_binary,
    create_mock_ghcr_curl_binary,
    parse_required_release_images,
    write_required_release_images,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMMON_SH = _REPO_ROOT / "scripts" / "release" / "common.sh"


class ReleaseCommonTest(unittest.TestCase):
    def _run_common_func(self, func_call, env=None, bin_dir=None, cwd=None):
        """Source common.sh and execute the given bash snippet."""
        setup = f"""
source "{_COMMON_SH}"
{func_call}
"""
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd or str(_REPO_ROOT),
        )

    def test_is_truthy(self):
        for val in TRUTHY_BOOLEAN_INPUTS:
            with self.subTest(val=val):
                proc = self._run_common_func(f'is_truthy "{val}"')
                self.assertEqual(proc.returncode, 0, f"Expected '{val}' to be truthy")

        for val in FALSY_BOOLEAN_INPUTS:
            with self.subTest(val=val):
                proc = self._run_common_func(f'is_truthy "{val}"')
                self.assertNotEqual(proc.returncode, 0, f"Expected '{val}' to be falsy")

    def test_validate_pure_numeric_semver(self):
        for tag in VALID_GA_RELEASE_TAGS:
            with self.subTest(tag=tag):
                proc = self._run_common_func(f'validate_pure_numeric_semver "{tag}"')
                self.assertEqual(proc.returncode, 0)

        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_common_func(f'validate_pure_numeric_semver "{bad_tag}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_compare_semver(self):
        test_cases = [
            ("0.2.0", "0.1.0", "1"),
            ("0.1.1", "0.1.0", "1"),
            ("1.0.0", "0.9.9", "1"),
            ("0.2.0", "0.2.0", "0"),
            ("0.1.0", "0.2.0", "-1"),
            ("0.1.0", "0.1.1", "-1"),
            ("0.9.9", "1.0.0", "-1"),
        ]
        for v1, v2, expected in test_cases:
            with self.subTest(v1=v1, v2=v2):
                proc = self._run_common_func(f'compare_semver "{v1}" "{v2}"')
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), expected)

    def test_get_latest_ga_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Initially no tags
            proc = self._run_common_func('get_latest_ga_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # Initially no tags, explicit fallback provided
            proc_default = self._run_common_func('get_latest_ga_tag "0.1.0"', cwd=repo_dir)
            self.assertEqual(proc_default.returncode, 0)
            self.assertEqual(proc_default.stdout.strip(), "0.1.0")

            # Add mixed tags
            git("tag", "-a", "0.1.0", "-m", "Release 0.1.0")
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")
            git("tag", "-a", "0.1.5", "-m", "Release 0.1.5")
            git("tag", "-a", "rc_0.3.0_validated", "-m", "RC tag")
            git("tag", "-a", "v1.0.0", "-m", "v-tag")

            proc = self._run_common_func('get_latest_ga_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    # ─── get_base_ga_tag_for_commit ───────────────────────────────────────────
    # The base a version bump, the scheduled-release range and the release notes
    # start from, found by ancestry rather than by number. The graph: main
    # A—B—C, 0.1.0 directly on A (pre-stamp), a stamped 0.2.0 off B, the line
    # release/0.2 from that stamp carrying L1, and a stamped 0.2.1 off L1.

    def _lined_graph(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        repo = pathlib.Path(repo_dir)
        shas = {"A": git("rev-parse", "HEAD").stdout.strip()}
        git("tag", "-a", "0.1.0", "-m", "release 0.1.0")
        (repo / "b.txt").write_text("b\n")
        git("add", "b.txt")
        git("commit", "-m", "feat: b")
        shas["B"] = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "--detach", "-q", shas["B"])
        (repo / "stamp.txt").write_text("0.2.0\n")
        git("add", "stamp.txt")
        git("commit", "-m", "chore(release): stamp release version 0.2.0")
        shas["S0"] = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "0.2.0", "-m", "release 0.2.0")
        git("switch", "-c", "release/0.2")
        (repo / "l1.txt").write_text("fix\n")
        git("add", "l1.txt")
        git("commit", "-m", "fix: backport")
        shas["L1"] = git("rev-parse", "HEAD").stdout.strip()
        (repo / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt")
        git("commit", "-m", "chore(release): stamp release version 0.2.1")
        shas["S1"] = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "0.2.1", "-m", "release 0.2.1")
        git("switch", "main")
        (repo / "c.txt").write_text("c\n")
        git("add", "c.txt")
        git("commit", "-m", "feat: c")
        shas["C"] = git("rev-parse", "HEAD").stdout.strip()
        return repo_dir, git, shas

    def test_get_base_ga_tag_for_commit_follows_ancestry_not_numbers(self):
        repo_dir, git, shas = self._lined_graph()
        cases = {
            "C": "0.2.0",   # main after 0.2.1 exists: the line's stamp is not in C's history
            "B": "0.2.0",   # the commit 0.2.0 was cut from: the minor's own release is its base
            "L1": "0.2.1",  # the commit 0.2.1 was cut from: its own release is its base (re-run shape)
            "S1": "0.2.1",  # the stamp commit itself, likewise
            "A": "0.1.0",   # a pre-stamp tag sits directly on its commit
        }
        for name, expected in cases.items():
            with self.subTest(candidate=name):
                proc = self._run_common_func(f'get_base_ga_tag_for_commit "{shas[name]}"', cwd=repo_dir)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), expected)

    def test_get_base_ga_tag_for_commit_honours_a_ceiling(self):
        """The notes step asks for the base strictly below the release being published."""
        repo_dir, git, shas = self._lined_graph()
        proc = self._run_common_func(f'get_base_ga_tag_for_commit "{shas["S1"]}" "0.2.1"', cwd=repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0.2.0")
        bad = self._run_common_func(f'get_base_ga_tag_for_commit "{shas["S1"]}" "v0.2.1"', cwd=repo_dir)
        self.assertNotEqual(bad.returncode, 0)

    def test_get_base_ga_tag_for_commit_is_empty_without_a_ga_tag_in_history(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", "rc_0.3.0_validated", "-m", "RC tag")
            proc = self._run_common_func("get_base_ga_tag_for_commit HEAD", cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")
            missing = self._run_common_func("get_base_ga_tag_for_commit", cwd=repo_dir)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("candidate commit is required", missing.stderr)
        finally:
            temp_dir.cleanup()

    def test_release_line_candidate_is_the_head_or_the_stamp_parent(self):
        repo_dir, git, shas = self._lined_graph()
        # The line head is the 0.2.1 stamp: the candidate is what it was cut from.
        idle = self._run_common_func('release_line_candidate "0.2"', cwd=repo_dir)
        self.assertEqual(idle.returncode, 0, idle.stderr)
        self.assertEqual(idle.stdout.strip(), shas["L1"])
        # A backport on top: the head itself.
        git("switch", "release/0.2")
        (pathlib.Path(repo_dir) / "l2.txt").write_text("fix\n")
        git("add", "l2.txt")
        git("commit", "-m", "fix: another backport")
        l2 = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "main")
        busy = self._run_common_func('release_line_candidate "0.2"', cwd=repo_dir)
        self.assertEqual(busy.returncode, 0, busy.stderr)
        self.assertEqual(busy.stdout.strip(), l2)
        missing = self._run_common_func('release_line_candidate "9.9"', cwd=repo_dir)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("No local release line", missing.stderr)

    def test_validated_rc_tags_at_commit_lists_the_line_gate(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            own = f"rc_2608191200_{head[:7]}_validated"
            git("tag", own)
            git("tag", f"rc_2608191200_{head[:7]}")
            git("tag", f"staging_2608191200_{head[:7]}")
            git("tag", "rc_hotfix_validated")
            # The pipeline's shape with another commit's sha field: composed by hand,
            # never minted, and the one form the bare shape would have let through.
            git("tag", "rc_2608191200_2222222_validated")
            proc = self._run_common_func(f'validated_rc_tags_at_commit "{head}"', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.split(), [own])
        finally:
            temp_dir.cleanup()

    def test_list_tags_on_main_takes_an_already_resolved_main(self):
        """A caller that resolved main once hands it over instead of having it fetched again.

        The handed-in ref is what the listing is filtered against: a side tip
        given as "main" lists that side's tags, which the default read does not.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "rc_2608191200_1111111")
            git("switch", "-c", "side")
            (pathlib.Path(repo_dir) / "side.txt").write_text("x")
            git("add", "side.txt")
            git("commit", "-m", "fix: side")
            side = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "rc_2608191300_2222222")
            git("switch", "main")

            default = self._run_common_func("list_tags_on_main 'rc_*'", cwd=repo_dir)
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertEqual(default.stdout.split(), ["rc_2608191200_1111111"])
            handed = self._run_common_func(f"list_tags_on_main 'rc_*' \"{side}\"", cwd=repo_dir)
            self.assertEqual(handed.returncode, 0, handed.stderr)
            self.assertEqual(handed.stdout.split(), ["rc_2608191300_2222222", "rc_2608191200_1111111"])
        finally:
            temp_dir.cleanup()

    def test_get_latest_validated_rc_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Initially no validated tags
            proc = self._run_common_func('get_latest_validated_rc_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # Add mixed tags including older and newer validated RC tags
            git("tag", "-a", "rc_2608181000_1111111_validated", "-m", "Older RC")
            git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Newer RC")
            git("tag", "-a", "rc_2608191300_3333333", "-m", "Unvalidated RC")
            git("tag", "-a", "0.2.0", "-m", "GA tag")

            proc = self._run_common_func('get_latest_validated_rc_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "rc_2608191200_2222222_validated")
        finally:
            temp_dir.cleanup()

    def test_get_latest_validated_rc_tag_ignores_a_newer_tag_off_main(self):
        """A release line's RC validation must never become the nightly's candidate.

        The picker sorts by name, which is by timestamp, and a line's rc_ tags
        share the namespace: the first one cut on `release/<X.Y>` would be the
        newest of all. Only tags whose commit is on `main` may answer.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "On main")
            git("switch", "-c", "release/0.2")
            (pathlib.Path(repo_dir) / "backport.txt").write_text("fix\n")
            git("add", "backport.txt")
            git("commit", "-m", "fix: backport")
            git("tag", "-a", "rc_2609291200_3333333_validated", "-m", "Newer, on the line")
            git("switch", "main")

            proc = self._run_common_func("get_latest_validated_rc_tag", cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "rc_2608191200_2222222_validated")
        finally:
            temp_dir.cleanup()

    def test_get_latest_staging_tag_ignores_a_newer_tag_off_main(self):
        """A staging_ tag a hand-dispatched promotion left on a release line is not main's gate."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", "staging_2608191200_2222222", "-m", "On main")
            git("switch", "-c", "release/0.2")
            (pathlib.Path(repo_dir) / "backport.txt").write_text("fix\n")
            git("add", "backport.txt")
            git("commit", "-m", "fix: backport")
            git("tag", "-a", "staging_2609291200_3333333", "-m", "Newer, on the line")
            git("switch", "main")

            proc = self._run_common_func("get_latest_staging_tag", cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "staging_2608191200_2222222")
        finally:
            temp_dir.cleanup()

    def test_list_tags_on_main_fails_closed_in_ci_without_a_main(self):
        """No `main` to compare against is an error in CI and a warning by hand.

        In CI the helper first fetches the release repository's `main`; its https
        URL is rewritten onto a path that does not exist so the test stays off
        the network and that fetch fails like the two lookups after it.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "rc_2608191200_2222222_validated")
            git("switch", "-c", "elsewhere")
            git("branch", "-D", "main")
            unreachable = pathlib.Path(repo_dir).parent / "unreachable.git"
            git(
                "config",
                f"url.{unreachable}.insteadOf",
                f"https://github.com/{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}.git",
            )

            in_ci = self._run_common_func(
                "list_tags_on_main 'rc_*_validated'",
                env={"CI": "true", **self._FAKE_RELEASE_REPO},
                cwd=repo_dir,
            )
            self.assertNotEqual(in_ci.returncode, 0)
            self.assertIn("Could not fetch main", in_ci.stderr)
            self.assertIn("Could not resolve main", in_ci.stderr)

            off_ci = self._run_common_func("list_tags_on_main 'rc_*_validated'", cwd=repo_dir)
            self.assertEqual(off_ci.returncode, 0, off_ci.stderr)
            self.assertEqual(off_ci.stdout.strip(), "rc_2608191200_2222222_validated")
            self.assertIn("not filtering", off_ci.stderr)
        finally:
            temp_dir.cleanup()

    def test_list_tags_on_main_in_ci_prefers_the_fetched_main_over_a_stale_tracking_ref(self):
        """A stale `origin/main` must not answer in CI when the release repository's is newer."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        git("tag", "rc_2608191200_2222222_validated")
        bare_dir = self._bare_remote_for(git, repo_dir)
        git("push", "--quiet", str(bare_dir), "main", "--tags")
        # The tracking ref stops here; the release repository's main moves on.
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        (pathlib.Path(repo_dir) / "later.txt").write_text("later\n")
        git("add", "later.txt")
        git("commit", "-m", "feat: later")
        git("tag", "rc_2609291200_3333333_validated")
        git("push", "--quiet", str(bare_dir), "main", "--tags")

        proc = self._run_common_func(
            "get_latest_validated_rc_tag",
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "rc_2609291200_3333333_validated")

    def test_list_tags_on_main_in_ci_refuses_a_stale_tracking_ref_when_the_fetch_fails(self):
        """In CI a failed fetch is an error, not a fall-through to `origin/main`."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        git("tag", "rc_2608191200_2222222_validated")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        unreachable = pathlib.Path(repo_dir).parent / "unreachable.git"
        git(
            "config",
            f"url.{unreachable}.insteadOf",
            f"https://github.com/{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}.git",
        )

        proc = self._run_common_func(
            "get_latest_validated_rc_tag",
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Could not fetch main", proc.stderr)
        self.assertNotIn("rc_2608191200_2222222_validated", proc.stdout)

    def test_list_tags_on_main_in_ci_is_not_fooled_by_a_tag_named_main(self):
        """A bare `main` refspec resolves a tag before the branch; the fetch names the branch ref."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        old = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "rc_2608191200_2222222_validated")
        (pathlib.Path(repo_dir) / "later.txt").write_text("later\n")
        git("add", "later.txt")
        git("commit", "-m", "feat: later")
        git("tag", "rc_2609291200_3333333_validated")
        bare_dir = self._bare_remote_for(git, repo_dir)
        git("push", "--quiet", str(bare_dir), "main", "--tags")
        # A tag literally named `main`, pointing at the old commit, on the remote.
        git("push", "--quiet", str(bare_dir), f"{old}:refs/tags/main")
        git("switch", "--detach", "-q")
        git("branch", "-D", "main")

        proc = self._run_common_func(
            "get_latest_validated_rc_tag",
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "rc_2609291200_3333333_validated")

    def test_list_tags_on_main_in_ci_reads_main_from_the_release_repository(self):
        """The Prow eval lane has neither `origin/main` nor a local `main`.

        Its checkout is the `evalcand_` tag alone, so the only route to `main` is
        a fetch from the release repository. The bare repository below stands in
        for it through the same URL rewrite the other CI-arm tests use, and holds
        `main` with one validated tag and a release line with a newer one.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        git("tag", "rc_2608191200_2222222_validated")
        git("switch", "-c", "release/0.2")
        (pathlib.Path(repo_dir) / "backport.txt").write_text("fix\n")
        git("add", "backport.txt")
        git("commit", "-m", "fix: backport")
        git("tag", "rc_2609291200_3333333_validated")
        bare_dir = self._bare_remote_for(git, repo_dir)
        git("push", "--quiet", str(bare_dir), "main", "release/0.2", "--tags")
        # Leave this checkout with no main at all, remote-tracking or local.
        git("branch", "-D", "main")
        self.assertEqual(git("branch", "--list", "main").stdout.strip(), "")
        self.assertEqual(git("branch", "-r").stdout.strip(), "")

        proc = self._run_common_func(
            "get_latest_validated_rc_tag",
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "rc_2608191200_2222222_validated")

    def test_list_tags_on_main_passes_through_a_tag_that_names_no_commit(self):
        """A broken tag must reach the caller and fail there, not vanish as "no candidate"."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        git("tag", "staging_2608191200_2222222")
        blob = git("hash-object", "-w", "init.txt").stdout.strip()
        git("tag", "staging_2609291200_3333333", blob)

        proc = self._run_common_func("list_tags_on_main 'staging_*'", cwd=repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.split(),
            ["staging_2609291200_3333333", "staging_2608191200_2222222"],
        )

    def test_list_tags_on_main_in_ci_refuses_a_shallow_checkout_it_cannot_deepen(self):
        """Past a shallow boundary every ancestry test reads "no": refuse rather than drop everything."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
        git("add", "second.txt")
        git("commit", "-m", "feat: second")
        git("tag", "rc_2608191200_2222222_validated")
        bare_dir = pathlib.Path(repo_dir).parent / "source.git"
        git("clone", "--quiet", "--bare", repo_dir, str(bare_dir))
        shallow_dir = pathlib.Path(repo_dir).parent / "shallow"
        git("clone", "--quiet", "--depth", "1", f"file://{bare_dir}", str(shallow_dir))
        unreachable = pathlib.Path(repo_dir).parent / "unreachable.git"
        git(
            "config",
            f"url.{unreachable}.insteadOf",
            f"https://github.com/{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}.git",
            cwd=shallow_dir,
        )

        proc = self._run_common_func(
            "list_tags_on_main 'rc_*_validated'",
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=str(shallow_dir),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("shallow", proc.stderr)

    def test_get_latest_staging_tag_matches_the_shape_not_the_prefix(self):
        """`staging_*` is a deploy trigger anyone can push; the GA gate reads this.

        `staging-redeploy-*.yml` fires on the bare prefix, so hand-made trigger
        tags are a supported thing to have in the graph. Matching the prefix here
        would let one of them read back as "the full nightly matrix passed on
        this commit", which is the only evidence a GA release has.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            proc = self._run_common_func("get_latest_staging_tag", cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            git("tag", "-a", "staging_2608181000_1111111", "-m", "Older promotion")
            git("tag", "-a", "staging_2608191200_2222222", "-m", "Newer promotion")
            # Sorts newest-first, so a hand-made tag must not win by name alone.
            git("tag", "-a", "staging_zzzz", "-m", "Hand-made trigger")
            git("tag", "-a", "staging_hotfix", "-m", "Hand-made trigger")
            git("tag", "-a", "rc_2608191300_3333333_validated", "-m", "RC only")

            proc = self._run_common_func("get_latest_staging_tag", cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "staging_2608191200_2222222")
        finally:
            temp_dir.cleanup()

    def test_get_latest_staging_tag_agrees_with_staging_tag_for_rc(self):
        """The shape is derived, not declared: a real promotion has to match it.

        STAGING_TAG_SHAPE_REGEX and staging_tag_for_rc are two spellings of the
        same format. If either moves without the other, the nightly pipeline
        pushes tags the release gate cannot see and GA releases stop silently.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            proc = self._run_common_func(
                'staging_tag_for_rc "rc_2608241820_b35543c_validated"', cwd=repo_dir
            )
            derived = proc.stdout.strip()
            self.assertEqual(derived, "staging_2608241820_b35543c")

            git("tag", "-a", derived, "-m", "Promoted")
            proc = self._run_common_func("get_latest_staging_tag", cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), derived)
        finally:
            temp_dir.cleanup()

    def test_staging_promotion_tags_at_commit_filters_by_shape(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_hotfix", head, "-m", "Hand-made trigger")

            proc = self._run_common_func(
                f'staging_promotion_tags_at_commit "{head}"', cwd=repo_dir
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            git("tag", "-a", "staging_2608191200_2222222", head, "-m", "Promoted")
            proc = self._run_common_func(
                f'staging_promotion_tags_at_commit "{head}"', cwd=repo_dir
            )
            self.assertEqual(proc.stdout.strip(), "staging_2608191200_2222222")
        finally:
            temp_dir.cleanup()

    def test_the_promotion_check_and_the_release_gate_agree_on_one_commit(self):
        """A tag one of them counts and the other does not makes a candidate unshippable.

        `get_existing_staging_tag` sets `skip_promotion` in
        `resolve_promotion_candidate.sh`; `staging_promotion_tags_at_commit` is what
        the release gate reads. Let the first count a hand-pushed `staging_hotfix`
        and the nightly concludes the commit is already promoted, so it never
        pushes the real tag — while the gate, matching on shape, reads the same
        commit as never promoted. Nothing is red and the candidate quietly cannot
        be released.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_hotfix", head, "-m", "Hand-made trigger")

            existing = self._run_common_func(
                f'get_existing_staging_tag "{head}"', cwd=repo_dir
            ).stdout.strip()
            gate = self._run_common_func(
                f'staging_promotion_tags_at_commit "{head}"', cwd=repo_dir
            ).stdout.strip()
            self.assertEqual(existing, gate, "the two lookups disagree on a prefix-only tag")
            self.assertEqual(existing, "")

            git("tag", "-a", "staging_2608191200_2222222", head, "-m", "Promoted")
            existing = self._run_common_func(
                f'get_existing_staging_tag "{head}"', cwd=repo_dir
            ).stdout.strip()
            self.assertEqual(existing, "staging_2608191200_2222222")
        finally:
            temp_dir.cleanup()

    def test_is_rc_candidate_commit_already_validated_is_anchored_to_the_rc_family(self):
        """The glob has to be rc_*_validated, not *_validated.

        This function gates resolve_rc_tag.sh's skip decision, so a validation
        marker from another tag family matching it would make the RC pipeline
        skip a candidate it never validated. staging_* is the family that made
        this concrete, but the point is general.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_common_func(f'is_rc_candidate_commit_already_validated "{head}"', cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)

            git("tag", "-a", "someone_elses_validated", "-m", "Not an RC marker")
            proc = self._run_common_func(f'is_rc_candidate_commit_already_validated "{head}"', cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0, "a non-rc_ tag ending _validated must not count")

            git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "RC marker")
            proc = self._run_common_func(f'is_rc_candidate_commit_already_validated "{head}"', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
        finally:
            temp_dir.cleanup()

    def test_get_existing_rc_tag_reuses_only_the_pipelines_own_name(self):
        """A hand-named rc_* tag is an attempt, not a name to reuse.

        resolve_rc_tag.sh reuses what this returns, and tag_validated_release.sh
        appends _validated to it. A hand-named rc_tag input therefore earns a
        marker the release line's gate (rc_validated_tag_name_regex) refuses,
        and reusing that name on the next dispatch would re-earn it: the
        commit could never clear the gate without a tag being deleted by hand.
        The same holds for a pipeline-shaped name carrying another commit's sha,
        which is why the match is the commit's own name and not the shape.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "rc_0.8_backport", "-m", "hand-named dispatch input")
            git("tag", "-a", "rc_0.8_backport_validated", "-m", "what that dispatch earned")
            git("tag", "-a", "rc_2609290000_1234567", "-m", "the pipeline's shape, another commit's sha")

            proc = self._run_common_func(f'get_existing_rc_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "", "neither hand-named tag may be reused")
            proc = self._run_common_func(f'is_commit_already_attempted "{head}"', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, "but they still count as an attempt")

            git("tag", "-a", f"rc_2609290000_{head[:7]}", "-m", "the pipeline's own")
            proc = self._run_common_func(f'get_existing_rc_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), f"rc_2609290000_{head[:7]}")
        finally:
            temp_dir.cleanup()

    def test_staging_tag_for_rc(self):
        cases = [
            ("rc_2608241820_b35543c_validated", "staging_2608241820_b35543c"),
            # The suffix is optional: the unvalidated tag maps to the same name,
            # which is what makes the transform reversible.
            ("rc_2608241820_b35543c", "staging_2608241820_b35543c"),
        ]
        for rc_tag, expected in cases:
            with self.subTest(rc_tag=rc_tag):
                proc = self._run_common_func(f'staging_tag_for_rc "{rc_tag}"')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), expected)

    def test_staging_tag_for_rc_refuses_anything_outside_the_rc_family(self):
        """The output is a live deploy trigger, so a typo must not compose one."""
        for bad in ("", "0.2.0", "staging_2608241820_b35543c", "rc_", "not-a-tag"):
            with self.subTest(bad=bad):
                proc = self._run_common_func(f'staging_tag_for_rc "{bad}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), "")

    def test_evalcand_tag_for_rc(self):
        cases = [
            ("rc_2608241820_b35543c_validated", "evalcand_2608241820_b35543c"),
            ("rc_2608241820_b35543c", "evalcand_2608241820_b35543c"),
        ]
        for rc_tag, expected in cases:
            with self.subTest(rc_tag=rc_tag):
                proc = self._run_common_func(f'evalcand_tag_for_rc "{rc_tag}"')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), expected)

    def test_evalcand_tag_for_rc_refuses_anything_outside_the_rc_family(self):
        """The output fires a job that takes a project out of a shared pool."""
        for bad in ("", "0.2.0", "evalcand_2608241820_b35543c", "rc_", "not-a-tag"):
            with self.subTest(bad=bad):
                proc = self._run_common_func(f'evalcand_tag_for_rc "{bad}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), "")

    def test_the_two_tag_families_share_a_core(self):
        """The invariant the whole gate rests on.

        An evalcand_ tag has to read back to the staging_ tag it becomes without
        a lookup, because the eval verdict is recorded against one and the
        promotion is pushed as the other. rc_tag_core is what makes that
        structural rather than two functions happening to agree.
        """
        rc_tag = "rc_2608241820_b35543c_validated"
        evalcand = self._run_common_func(f'evalcand_tag_for_rc "{rc_tag}"').stdout.strip()
        staging = self._run_common_func(f'staging_tag_for_rc "{rc_tag}"').stdout.strip()
        self.assertEqual(
            evalcand.removeprefix("evalcand_"),
            staging.removeprefix("staging_"),
        )

    def test_the_evalcand_shape_matches_what_evalcand_tag_for_rc_composes(self):
        """EVALCAND_TAG_SHAPE_REGEX is mirrored by the `branches` regex on
        post-kube-agents-eval-rc in GoogleCloudPlatform/oss-test-infra. If the
        shape and the composer drift apart here, the pipeline pushes a tag no
        eval fires on and every candidate times out unpromoted."""
        proc = self._run_common_func(
            'tag="$(evalcand_tag_for_rc "rc_2608241820_b35543c_validated")";'
            ' grep -qE "${EVALCAND_TAG_SHAPE_REGEX}" <<<"${tag}"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_get_existing_evalcand_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_common_func(f'get_existing_evalcand_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # A hand-pushed prefix tag is not a nomination. This is what
            # resolve_promotion_candidate.sh reads to decide whether a candidate
            # has already been measured, so a match here skips the eval and the
            # candidate never reaches staging at all.
            git("tag", "-a", "evalcand_hotfix", head, "-m", "Hand-made")
            proc = self._run_common_func(f'get_existing_evalcand_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "")

            git("tag", "-a", "evalcand_2608241820_b35543c", head, "-m", "Nominated")
            proc = self._run_common_func(f'get_existing_evalcand_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "evalcand_2608241820_b35543c")

            # And not for a different commit.
            (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
            git("add", "second.txt")
            git("commit", "-m", "chore: second commit")
            other = git("rev-parse", "HEAD").stdout.strip()
            proc = self._run_common_func(f'get_existing_evalcand_tag "{other}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "")
        finally:
            temp_dir.cleanup()

    def test_a_staging_tag_is_not_an_evalcand_tag_and_the_reverse(self):
        """The families must not answer for each other.

        They share a core by design, so a lookup matching on the core rather
        than the prefix would read a rejected candidate's leftover evalcand_ tag
        as a promotion -- or a promotion as a nomination, which would stop the
        eval ever running again on a commit that reached staging.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "evalcand_2608241820_b35543c", head, "-m", "Nominated")

            proc = self._run_common_func(f'get_existing_staging_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "")

            git("tag", "-a", "staging_2608241820_b35543c", head, "-m", "Promoted")
            proc = self._run_common_func(f'get_existing_staging_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "staging_2608241820_b35543c")
            proc = self._run_common_func(f'get_existing_evalcand_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "evalcand_2608241820_b35543c")

            # The reverse, on its own commit. Asserting it above the two tags
            # this commit now carries would prove nothing: the evalcand_ lookup
            # would find its own tag whether or not it also matched the
            # staging_ one, which is the direction that stops the eval ever
            # running again on a commit that reached staging.
            (pathlib.Path(repo_dir) / "promoted.txt").write_text("promoted\n")
            git("add", "promoted.txt")
            git("commit", "-m", "chore: a commit that reached staging")
            promoted = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_2608241821_c46654d", promoted, "-m", "Promoted")
            proc = self._run_common_func(f'get_existing_evalcand_tag "{promoted}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "")
        finally:
            temp_dir.cleanup()

    def test_get_existing_staging_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_common_func(f'get_existing_staging_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # A staging tag on a DIFFERENT commit must not answer for this one.
            (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
            git("add", "second.txt")
            git("commit", "-m", "chore: second commit")
            other = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_2608241820_b35543c", "-m", "Promoted elsewhere")

            proc = self._run_common_func(f'get_existing_staging_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "")

            proc = self._run_common_func(f'get_existing_staging_tag "{other}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "staging_2608241820_b35543c")
        finally:
            temp_dir.cleanup()

    def _repo_with_staging_trigger(self, patterns):
        """A mock repo whose HEAD carries staging-redeploy-agent.yml with `patterns`."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        workflow = pathlib.Path(repo_dir) / ".github" / "workflows"
        workflow.mkdir(parents=True, exist_ok=True)
        rendered = "\n".join(f'      - "{p}"' for p in patterns)
        (workflow / "staging-redeploy-agent.yml").write_text(
            "name: Staging Redeploy Agent\n\non:\n  push:\n    tags:\n" + rendered + "\n\njobs: {}\n"
        )
        git("add", "-A")
        git("commit", "-m", "chore: staging trigger")
        return repo_dir, git("rev-parse", "HEAD").stdout.strip()

    def _trigger_matches(self, repo_dir, commit, tag):
        proc = self._run_common_func(
            f'staging_trigger_matches_at_commit "{commit}" "{tag}"', cwd=repo_dir
        )
        return proc.returncode

    def test_staging_trigger_matches_the_tag_the_promotion_pushes(self):
        repo_dir, head = self._repo_with_staging_trigger(["staging_*"])
        self.assertEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)

    def test_staging_trigger_rejects_a_commit_that_predates_the_rename(self):
        """The whole reason the helper exists.

        A push event runs the workflows in the pushed ref's tree. A candidate
        still declaring `staging/**` does not match a flat `staging_<ts>_<sha>`,
        so the promotion would deploy nothing and report success.
        """
        repo_dir, head = self._repo_with_staging_trigger(["staging/**"])
        self.assertNotEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)
        # The same tree does answer the tag shape it was written for.
        self.assertEqual(self._trigger_matches(repo_dir, head, "staging/2026-07-23"), 0)

    def test_staging_trigger_matches_when_any_listed_pattern_does(self):
        repo_dir, head = self._repo_with_staging_trigger(["staging/**", "staging_*"])
        self.assertEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)

    def test_staging_trigger_refuses_when_the_workflow_is_absent(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        head = git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)

    def _repo_with_staging_deploy_trigger(self, patterns):
        """A mock repo whose HEAD carries staging-deploy.yml with `patterns`."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        workflow = pathlib.Path(repo_dir) / ".github" / "workflows"
        workflow.mkdir(parents=True, exist_ok=True)
        rendered = "\n".join(f'      - "{p}"' for p in patterns)
        (workflow / "staging-deploy.yml").write_text(
            "name: Staging Deploy\n\non:\n  push:\n    tags:\n" + rendered + "\n\njobs: {}\n"
        )
        git("add", "-A")
        git("commit", "-m", "chore: staging deploy trigger")
        return repo_dir, git("rev-parse", "HEAD").stdout.strip()

    def test_staging_trigger_matches_in_consolidated_staging_deploy_workflow(self):
        repo_dir, head = self._repo_with_staging_deploy_trigger(["staging_*"])
        self.assertEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)

    def _repo_with_pipeline_markers(self, optional_runner=True, suite_selector=True,
                                    reconciler=True):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        release = pathlib.Path(repo_dir) / "scripts" / "release"
        release.mkdir(parents=True, exist_ok=True)
        if optional_runner:
            (release / "run_optional_e2e_suites.sh").write_text("#!/usr/bin/env bash\n")
        selector = "E2E_SUITE" if suite_selector else "E2E_ENV"
        (release / "execute_e2e_tests.py").write_text(f'_VAR = "{selector}"\n')
        if reconciler:
            (release / "reconcile_environment.sh").write_text("#!/usr/bin/env bash\n")
        git("add", "-A")
        git("commit", "-m", "chore: pipeline markers")
        return repo_dir, git("rev-parse", "HEAD").stdout.strip()

    def _supports_shared_pipeline(self, repo_dir, commit):
        return self._run_common_func(
            f'candidate_supports_shared_pipeline "{commit}"', cwd=repo_dir
        ).returncode

    def test_a_restructured_candidate_supports_the_shared_pipeline(self):
        repo_dir, head = self._repo_with_pipeline_markers()
        self.assertEqual(self._supports_shared_pipeline(repo_dir, head), 0)

    def test_a_candidate_without_the_optional_runner_does_not(self):
        repo_dir, head = self._repo_with_pipeline_markers(optional_runner=False)
        self.assertNotEqual(self._supports_shared_pipeline(repo_dir, head), 0)

    def test_a_candidate_reading_only_the_old_selector_does_not(self):
        """The silent half: the gate would run the runner's default suite instead."""
        repo_dir, head = self._repo_with_pipeline_markers(suite_selector=False)
        self.assertNotEqual(self._supports_shared_pipeline(repo_dir, head), 0)

    def test_a_candidate_without_the_reconciler_does_not(self):
        """The nightly checks the candidate OUT to reconcile staging at it.

        A tree without reconcile_environment.sh aborts that step on a missing
        file, and the promotion is deliberately decoupled from its outcome — so
        the staging tag goes out, staging's images move, and its infrastructure
        stays exactly as stale as before. Silent in the way this gate exists to
        catch.
        """
        repo_dir, head = self._repo_with_pipeline_markers(reconciler=False)
        self.assertNotEqual(self._supports_shared_pipeline(repo_dir, head), 0)

    def test_candidate_support_requires_a_commit(self):
        repo_dir, _ = self._repo_with_pipeline_markers()
        proc = self._run_common_func('candidate_supports_shared_pipeline ""', cwd=repo_dir)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("a commit is required", proc.stderr)

    def test_staging_trigger_requires_both_arguments(self):
        repo_dir, head = self._repo_with_staging_trigger(["staging_*"])
        proc = self._run_common_func('staging_trigger_matches_at_commit "" ""', cwd=repo_dir)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("a commit and a tag are required", proc.stderr)

    def test_release_fetch_tags_is_a_no_op_outside_ci(self):
        """It must not reach the network on a developer machine.

        The CI arm cannot be exercised hermetically — it fetches a real URL — so
        what is pinned here is the guard in front of it. Without the guard, every
        script that calls this would try to hit github.com from a unit test.
        """
        temp_dir, repo_dir, _ = create_mock_git_repo()
        try:
            proc = self._run_common_func("release_fetch_tags", env={"CI": ""}, cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")

            # And it stays quiet rather than failing when the fetch cannot work:
            # a fetch that could not run is not itself the error, the caller's
            # own lookup afterwards is.
            proc = self._run_common_func(
                "release_fetch_tags",
                env={"CI": "true", "GH_ORG": "no-such-org-kube-agents", "GH_REPO": "no-such-repo"},
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_get_target_repo(self):
        # Default
        proc = self._run_common_func('get_target_repo', env={"GH_ORG": "", "GH_REPO": "", "GITHUB_REPOSITORY": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_DEFAULT_RELEASE_REPO)

        # Via GITHUB_REPOSITORY
        proc = self._run_common_func('get_target_repo', env={"GITHUB_REPOSITORY": MOCK_CUSTOM_TARGET_REPO})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_CUSTOM_TARGET_REPO)

        # Via GH_ORG and GH_REPO
        proc = self._run_common_func('get_target_repo', env={"GH_ORG": MOCK_CUSTOM_ORG, "GH_REPO": MOCK_CUSTOM_REPO})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), f"{MOCK_CUSTOM_ORG}/{MOCK_CUSTOM_REPO}")

    def test_get_registry_prefix(self):
        # Default
        proc = self._run_common_func('get_registry_prefix', env={"REGISTRY_PREFIX": "", "GH_ORG": "", "GH_REPO": "", "GITHUB_REPOSITORY": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_DEFAULT_REGISTRY_PREFIX)

        # Explicit REGISTRY_PREFIX
        proc = self._run_common_func('get_registry_prefix', env={"REGISTRY_PREFIX": MOCK_CUSTOM_REGISTRY_PREFIX})
        self.assertEqual(proc.stdout.strip(), MOCK_CUSTOM_REGISTRY_PREFIX)

    def test_required_release_images_registry(self):
        cmd = 'echo "IMAGES=${REQUIRED_RELEASE_IMAGES[*]}"'
        proc = self._run_common_func(cmd)
        self.assertEqual(proc.returncode, 0)
        for img in MOCK_REQUIRED_RELEASE_IMAGES:
            self.assertIn(img, proc.stdout)

    def test_is_ci_pipeline_behavior(self):
        # By default isolated env has CI stripped
        proc = self._run_common_func('is_ci_pipeline')
        self.assertNotEqual(proc.returncode, 0)

        # With explicit CI=true
        proc = self._run_common_func('is_ci_pipeline', env={"CI": "true"})
        self.assertEqual(proc.returncode, 0)

    def test_promote_release_images_validation(self):
        # Missing args
        proc = self._run_common_func('promote_release_images "" ""')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("commit_sha and release_version are required", proc.stderr)

        # Invalid target_tag format
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_common_func(f'promote_release_images "{MOCK_SAMPLE_SHORT_SHA}" "{bad_tag}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_promote_release_images_local_dry_run(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Remote image promotion", proc.stdout)
            self.assertIn("skipped (runs only in CI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_execution(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Promoting verified container images", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoting {img}", proc.stdout)
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_swapped_arguments(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_TARGET_RELEASE_TAG}" "{MOCK_SAMPLE_COMMIT_SHA}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_idempotent_skip(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            existing = [
                f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ] + [
                f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ]
            create_mock_docker_binary(bin_dir, existing_images=existing)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_idempotent_skip_when_target_is_index_wrapping_source_manifest(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            source_sha = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            target_index_sha = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            raw_index = f'{{"mediaType":"application/vnd.oci.image.index.v1+json","digest":"{target_index_sha}","manifests":[{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}]}}'
            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = {
                    "format": target_index_sha,
                    "raw": raw_index,
                }
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"] = {
                    "format": source_sha,
                    "raw": f'{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}',
                }
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_fails_when_mismatched_digest(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = (
                    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"] = (
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                )
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does NOT match source image", proc.stderr)
            self.assertIn("Release promotion blocked", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_ensure_git_tag_hermetic_local_execution(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            # Local execution should create tag without remote operations
            proc = self._run_common_func(
                f'ensure_git_tag "{MOCK_TARGET_RELEASE_TAG}" "{head_commit}" "Test release {MOCK_TARGET_RELEASE_TAG}"',
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn(f"Dry-run: Git tag '{MOCK_TARGET_RELEASE_TAG}' created locally", proc.stdout)

            # Tag is locally created
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(tag_commit, head_commit)

            # Idempotent skip on second run
            proc2 = self._run_common_func(
                f'ensure_git_tag "{MOCK_TARGET_RELEASE_TAG}" "{head_commit}" "Test release {MOCK_TARGET_RELEASE_TAG}"',
                cwd=repo_dir,
            )
            self.assertEqual(proc2.returncode, 0)
            self.assertIn("Idempotent skip", proc2.stdout)
        finally:
            temp_dir.cleanup()

    # ─── ensure_ga_release_refs ───────────────────────────────────────────────
    # The GA release commit is pushed to its release line `release/<X.Y>` alongside
    # its tag, so it belongs to a branch on the repository rather than to the tag alone. The
    # contract is ensure_git_tag's, and these pin the two halves of it: the
    # remote decides in CI, and nothing is ever force-pushed.

    _FAKE_RELEASE_REPO = {"GH_ORG": "no-such-org-kube-agents", "GH_REPO": "no-such-repo"}

    def _bare_remote_for(self, git, repo_dir):
        """A bare repository standing in for github.com/<GH_ORG>/<GH_REPO>.

        common.sh reaches the release repository by its https URL, so the URL is
        rewritten onto the bare path; that covers ls-remote and the https push
        fallback alike. `origin` is left unset so the fallback is what pushes.
        """
        bare_dir = pathlib.Path(repo_dir).parent / "remote.git"
        git("init", "--bare", str(bare_dir))
        git(
            "config",
            f"url.{bare_dir}.insteadOf",
            f"https://github.com/{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}.git",
        )
        return bare_dir

    def _remote_branch_sha(self, git, bare_dir, branch):
        out = git("--git-dir", str(bare_dir), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").stdout
        return out.strip()

    def test_release_line_and_branch_names(self):
        line = self._run_common_func(f'release_line_for_version "{MOCK_TARGET_RELEASE_TAG}"')
        self.assertEqual(line.returncode, 0, line.stderr)
        self.assertEqual(line.stdout.strip(), MOCK_TARGET_RELEASE_LINE)
        branch = self._run_common_func(f'release_branch_for_line "{MOCK_TARGET_RELEASE_LINE}"')
        self.assertEqual(branch.returncode, 0, branch.stderr)
        self.assertEqual(branch.stdout.strip(), f"release/{MOCK_TARGET_RELEASE_LINE}")
        for bad in ("0.2.0", "v0.2", "release/0.2", ""):
            with self.subTest(bad=bad):
                proc = self._run_common_func(f'release_branch_for_line "{bad}"')
                self.assertNotEqual(proc.returncode, 0)

    def test_ensure_ga_release_refs_requires_version_and_commit(self):
        for call in ("ensure_ga_release_refs", f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}"'):
            with self.subTest(call=call):
                proc = self._run_common_func(call)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("version and release commit are required", proc.stderr)

    def test_ensure_ga_release_refs_off_ci_creates_locally_and_skips_the_push(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        head = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"

        proc = self._run_common_func(f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{head}"', cwd=repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Dry-run: Git tag '{MOCK_TARGET_RELEASE_TAG}' and release line '{branch}' set locally", proc.stdout)
        self.assertEqual(git("rev-parse", branch).stdout.strip(), head)
        # The caller's checkout is not moved onto the new branch.
        self.assertEqual(git("symbolic-ref", "--short", "HEAD").stdout.strip(), "main")

        again = self._run_common_func(f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{head}"', cwd=repo_dir)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Dry-run", again.stdout)
        self.assertEqual(git("rev-parse", branch).stdout.strip(), head)

    def test_ensure_ga_release_refs_refuses_to_move_a_local_branch(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        first = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("branch", branch, first)
        (pathlib.Path(repo_dir) / "next.txt").write_text("next\n")
        git("add", "next.txt")
        git("commit", "-m", "feat: next")
        second = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run_common_func(f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{second}"', cwd=repo_dir)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"Release line '{branch}' already exists locally but points to commit {first}", proc.stderr)
        self.assertEqual(git("rev-parse", branch).stdout.strip(), first)

    def test_ensure_ga_release_refs_in_ci_pushes_to_the_release_repository(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        head = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"

        proc = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{head}"',
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' and release line '{branch}' successfully pushed", proc.stdout)
        self.assertEqual(self._remote_branch_sha(git, bare_dir, branch), head)
        self.assertEqual(git("--git-dir", str(bare_dir), "rev-parse", f"refs/tags/{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip(), head)

    def test_ensure_ga_release_refs_in_ci_skips_when_the_remote_already_has_it(self):
        """A re-run of the release job must find its first attempt's branch on the remote.

        The local checkout has no such branch — a fresh checkout never does — so
        the skip has to come from asking the remote, not from a local ref.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        head = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("push", str(bare_dir), f"{head}:refs/heads/{branch}")
        self.assertEqual(git("branch", "--list", branch).stdout.strip(), "")

        proc = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{head}"',
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Release line '{branch}' already exists on", proc.stdout)
        self.assertIn("Idempotent skip", proc.stdout)
        # Only the tag was missing, so only the tag is pushed.
        self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' successfully pushed", proc.stdout)

    def test_ensure_ga_release_refs_in_ci_refuses_a_remote_branch_at_another_commit(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        first = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("push", str(bare_dir), f"{first}:refs/heads/{branch}")
        (pathlib.Path(repo_dir) / "next.txt").write_text("next\n")
        git("add", "next.txt")
        git("commit", "-m", "feat: next")
        second = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{second}"',
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"Release line '{branch}' already exists on", proc.stderr)
        self.assertIn(f"points to commit {first}, not the release commit {second}", proc.stderr)
        # The remote was not force-pushed over.
        self.assertEqual(self._remote_branch_sha(git, bare_dir, branch), first)

    def test_ensure_ga_release_refs_in_ci_fails_closed_when_the_remote_cannot_be_read(self):
        """An unreadable remote is an error, not "no branch".

        `origin` holds the branch at an ancestor of the target, which a plain
        push would happily fast-forward. The https URL common.sh asks with
        ls-remote is rewritten onto a path that does not exist, so the read
        fails; the function must stop there rather than guess and push.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = pathlib.Path(repo_dir).parent / "origin.git"
        git("init", "--bare", str(bare_dir))
        git("remote", "add", "origin", str(bare_dir))
        unreachable = pathlib.Path(repo_dir).parent / "unreachable.git"
        git(
            "config",
            f"url.{unreachable}.insteadOf",
            f"https://github.com/{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}.git",
        )
        first = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("push", "origin", f"{first}:refs/heads/{branch}")
        (pathlib.Path(repo_dir) / "next.txt").write_text("next\n")
        git("add", "next.txt")
        git("commit", "-m", "feat: next")
        second = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{second}"',
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Could not read branches of", proc.stderr)
        self.assertNotIn("successfully pushed", proc.stdout)
        self.assertEqual(self._remote_branch_sha(git, bare_dir, branch), first)

    def test_ensure_ga_release_refs_in_ci_ignores_a_branch_whose_name_merely_ends_in_the_ref(self):
        """ls-remote's pattern is tail-matched; the lookup must match the whole ref.

        A stray `x/refs/heads/release/<v>` on the remote would otherwise read as
        the release branch itself, and refuse the release.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        head = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("switch", "-c", "stray")
        (pathlib.Path(repo_dir) / "stray.txt").write_text("stray\n")
        git("add", "stray.txt")
        git("commit", "-m", "feat: stray")
        git("push", str(bare_dir), f"HEAD:refs/heads/x/refs/heads/{branch}")
        git("switch", "main")

        proc = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{head}"',
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' and release line '{branch}' successfully pushed", proc.stdout)
        self.assertEqual(self._remote_branch_sha(git, bare_dir, branch), head)

    def test_ensure_ga_release_refs_fast_forwards_a_line_at_the_candidate(self):
        """A patch's stamped commit is a child of the line head; the line moves to it.

        In CI the remote holds the line at the candidate; the checkout has no
        local copy. Off CI the local branch is at the candidate. Both end with
        the line at the release commit, and the remote is never force-pushed.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        candidate = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("push", str(bare_dir), f"{candidate}:refs/heads/{branch}")
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt")
        git("commit", "-m", "chore(release): stamp release version 0.2.1")
        release_commit = git("rev-parse", "HEAD").stdout.strip()

        placement = self._run_common_func(
            f'release_branch_placement "{MOCK_LINE_PATCH_RELEASE_TAG}" "{release_commit}" "{candidate}"',
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertEqual(placement.returncode, 0, placement.stderr)
        self.assertEqual(placement.stdout.strip(), "remote-candidate")

        proc = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_LINE_PATCH_RELEASE_TAG}" "{release_commit}" "{candidate}"',
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fast-forwards", proc.stdout)
        self.assertEqual(self._remote_branch_sha(git, bare_dir, branch), release_commit)

        # Without the candidate argument the same branch is "anywhere else".
        git("push", "--force", str(bare_dir), f"{candidate}:refs/heads/{branch}")
        refused = self._run_common_func(
            f'release_branch_placement "{MOCK_LINE_PATCH_RELEASE_TAG}" "{release_commit}"',
            env={"CI": "true", **self._FAKE_RELEASE_REPO},
            cwd=repo_dir,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("already exists on", refused.stderr)

        # Off CI: a local line at the candidate moves too, and nothing is pushed.
        git("branch", "-f", branch, candidate)
        local = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_LINE_PATCH_RELEASE_TAG}" "{release_commit}" "{candidate}"',
            cwd=repo_dir,
        )
        self.assertEqual(local.returncode, 0, local.stderr)
        self.assertIn("Dry-run", local.stdout)
        self.assertEqual(git("rev-parse", branch).stdout.strip(), release_commit)

    def test_a_local_line_that_cannot_be_moved_fails_the_run_instead_of_reading_as_moved(self):
        """The fast-forward is a compare-and-swap, and its failure has to be the run's.

        The ref's update is refused between the placement read and the swap, by a
        reference-transaction hook rather than a lock file so the test does not
        assume the files ref backend (a git that defaults to reftable has no
        `refs/heads/` directory to hold a lock in). Before, the swap's failure
        fell through to the success line and the run went on to push a line that
        was not at the release commit; now the run stops, says why, takes the tag
        back, and leaves the line where it was. The absent arm's create is held
        to the same rule.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        candidate = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("branch", branch, candidate)
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt")
        git("commit", "-m", "chore(release): stamp release version 0.2.1")
        release_commit = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "main")
        hold = pathlib.Path(repo_dir) / "hold-release-lines"
        hook = pathlib.Path(repo_dir) / ".git" / "hooks" / "reference-transaction"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            "#!/bin/sh\n"
            f"[ \"$1\" = prepared ] || exit 0\n"
            f"[ -e '{hold}' ] || exit 0\n"
            "while read -r old new ref; do\n"
            "  case \"$ref\" in refs/heads/release/*) exit 1 ;; esac\n"
            "done\n"
            "exit 0\n"
        )
        hook.chmod(0o755)
        hold.write_text("")

        proc = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_LINE_PATCH_RELEASE_TAG}" "{release_commit}" "{candidate}"',
            cwd=repo_dir,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Could not fast-forward release line", proc.stderr)
        self.assertNotIn("fast-forwards to release commit", proc.stdout)
        self.assertIn("taken back from this checkout", proc.stderr)
        self.assertEqual(git("rev-parse", branch).stdout.strip(), candidate)
        self.assertEqual(git("tag", "-l", MOCK_LINE_PATCH_RELEASE_TAG).stdout.strip(), "")

        hold.unlink()
        git("branch", "-D", branch)
        hold.write_text("")
        absent = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{candidate}"',
            cwd=repo_dir,
        )
        self.assertNotEqual(absent.returncode, 0)
        self.assertIn("Could not set release line", absent.stderr)
        self.assertEqual(git("branch", "--list", branch).stdout.strip(), "")
        self.assertEqual(git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip(), "")

    def test_ensure_ga_release_refs_refuses_to_move_the_checked_out_line(self):
        """update-ref on HEAD's branch would leave the worktree behind at the candidate."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        candidate = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("branch", branch, candidate)
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt")
        git("commit", "-m", "chore(release): stamp release version 0.2.1")
        release_commit = git("rev-parse", "HEAD").stdout.strip()
        git("switch", branch)

        proc = self._run_common_func(
            f'ensure_ga_release_refs "{MOCK_LINE_PATCH_RELEASE_TAG}" "{release_commit}" "{candidate}"',
            cwd=repo_dir,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("is checked out here", proc.stderr)
        self.assertEqual(git("rev-parse", branch).stdout.strip(), candidate)
        self.assertEqual(git("status", "--porcelain").stdout.strip(), "")
        # The tag created before the refusal is taken back: off CI nothing prunes it,
        # and a local tag on the stamp would be the next calculator run's base.
        self.assertEqual(git("tag", "-l", MOCK_LINE_PATCH_RELEASE_TAG).stdout.strip(), "")
        self.assertIn("taken back from this checkout", proc.stderr)

    def test_release_line_helpers_in_ci_read_the_release_repository(self):
        """The CI arm: the head comes from the remote (fetched when the checkout lacks it),
        the candidate steps back from a stamped head, and an unreadable remote is 2, not "no"."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        base = git("rev-parse", "HEAD").stdout.strip()
        git("push", "--quiet", str(bare_dir), "main")
        # A line that advanced on the remote only: a second clone pushes a backport and a stamp.
        other = pathlib.Path(repo_dir).parent / "other"
        git("clone", "--quiet", str(bare_dir), str(other))
        git("config", "user.name", "Test User", cwd=other)
        git("config", "user.email", "test@example.com", cwd=other)
        git("config", "commit.gpgsign", "false", cwd=other)
        git("switch", "-c", "release/0.2", cwd=other)
        (other / "backport.txt").write_text("fix\n")
        git("add", "backport.txt", cwd=other)
        git("commit", "-m", "fix: backport", cwd=other)
        l1 = git("rev-parse", "HEAD", cwd=other).stdout.strip()
        git("push", "--quiet", "origin", "release/0.2", cwd=other)
        ci = {"CI": "true", **self._FAKE_RELEASE_REPO}

        head = self._run_common_func('release_line_head "0.2"', env=ci, cwd=repo_dir)
        self.assertEqual(head.returncode, 0, head.stderr)
        self.assertEqual(head.stdout.strip(), l1)
        self.assertEqual(git("rev-parse", "--verify", f"{l1}^{{commit}}").stdout.strip(), l1, "fetched into the checkout")

        exists = self._run_common_func('if release_line_branch_exists "0.2"; then echo rc=0; else echo "rc=$?"; fi', env=ci, cwd=repo_dir)
        self.assertIn("rc=0", exists.stdout)
        absent = self._run_common_func('if release_line_branch_exists "9.9"; then echo rc=0; else echo "rc=$?"; fi', env=ci, cwd=repo_dir)
        self.assertIn("rc=1", absent.stdout)

        (other / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt", cwd=other)
        git("commit", "-m", "chore(release): stamp release version 0.2.1", cwd=other)
        git("tag", "-a", "0.2.1", "-m", "release 0.2.1", cwd=other)
        git("push", "--quiet", "origin", "release/0.2", "--tags", cwd=other)
        git("fetch", "--quiet", str(bare_dir), "+refs/tags/*:refs/tags/*")
        candidate = self._run_common_func('release_line_candidate "0.2"', env=ci, cwd=repo_dir)
        self.assertEqual(candidate.returncode, 0, candidate.stderr)
        self.assertEqual(candidate.stdout.strip(), l1)

        # Point the release repository's URL at a path that does not exist instead.
        unreachable = pathlib.Path(repo_dir).parent / "unreachable.git"
        git("config", "--unset-all", f"url.{bare_dir}.insteadOf")
        git("config", f"url.{unreachable}.insteadOf", f"https://github.com/{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}.git")
        broken = self._run_common_func('if release_line_branch_exists "0.2"; then echo rc=0; else echo "rc=$?"; fi', env=ci, cwd=repo_dir)
        self.assertIn("rc=2", broken.stdout)

    def test_a_line_already_past_the_release_commit_is_left_alone(self):
        """A re-run after the push, with a merge landed on the line since: nothing to move.

        The remote holds the tag at the release commit and the line one commit
        beyond it. Placement says `remote-past`; the run pushes nothing and
        continues, which is what lets a release that died at image promotion
        finish. A line at an unrelated commit is still refused.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        candidate = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        ci = {"CI": "true", **self._FAKE_RELEASE_REPO}
        first = self._run_common_func(f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{candidate}"', env=ci, cwd=repo_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        git("switch", "-c", "backport", candidate)
        (pathlib.Path(repo_dir) / "backport.txt").write_text("fix\n")
        git("add", "backport.txt")
        git("commit", "-m", "fix: backport")
        moved = git("rev-parse", "HEAD").stdout.strip()
        git("push", "--quiet", str(bare_dir), f"HEAD:refs/heads/{branch}")
        git("switch", "main")

        placement = self._run_common_func(f'release_branch_placement "{MOCK_TARGET_RELEASE_TAG}" "{candidate}"', env=ci, cwd=repo_dir)
        self.assertEqual(placement.returncode, 0, placement.stderr)
        self.assertEqual(placement.stdout.strip(), "remote-past")
        rerun = self._run_common_func(f'ensure_ga_release_refs "{MOCK_TARGET_RELEASE_TAG}" "{candidate}"', env=ci, cwd=repo_dir)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn("already past release commit", rerun.stdout)
        self.assertNotIn("successfully pushed", rerun.stdout)
        self.assertEqual(self._remote_branch_sha(git, bare_dir, branch), moved)

        git("branch", "-f", branch, moved)
        local = self._run_common_func(f'release_branch_placement "{MOCK_TARGET_RELEASE_TAG}" "{candidate}"', cwd=repo_dir)
        self.assertEqual(local.stdout.strip(), "local-past")

        # An unrelated history: an orphan commit that descends from nothing here.
        git("switch", "--orphan", "elsewhere")
        (pathlib.Path(repo_dir) / "else.txt").write_text("x\n")
        git("add", "else.txt")
        git("commit", "-m", "chore: unrelated")
        git("push", "--quiet", "--force", str(bare_dir), f"HEAD:refs/heads/{branch}")
        git("switch", "main")
        refused = self._run_common_func(f'release_branch_placement "{MOCK_TARGET_RELEASE_TAG}" "{candidate}"', env=ci, cwd=repo_dir)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("already exists on", refused.stderr)

    def test_release_line_candidate_with_a_version_resumes_that_release(self):
        """A version pins the line's candidate to what that release was cut from, whatever merged since."""
        repo_dir, git, shas = self._lined_graph()
        git("switch", "release/0.2")
        (pathlib.Path(repo_dir) / "l2.txt").write_text("fix\n")
        git("add", "l2.txt")
        git("commit", "-m", "fix: landed after 0.2.1 was cut")
        l2 = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "main")
        plain = self._run_common_func('release_line_candidate "0.2"', cwd=repo_dir)
        self.assertEqual(plain.stdout.strip(), l2)
        pinned = self._run_common_func('release_line_candidate "0.2" "0.2.1"', cwd=repo_dir)
        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertEqual(pinned.stdout.strip(), shas["L1"])
        other = self._run_common_func('release_line_candidate "0.2" "0.1.0"', cwd=repo_dir)
        self.assertEqual(other.stdout.strip(), l2)

    def test_off_ci_the_line_helpers_fall_back_to_the_tracking_ref(self):
        """A developer's clone that fetched a line but never checked it out.

        That is the runbook's dry-run shape: `origin/release/X.Y` exists, no
        local branch does. The head, the candidate and "does the line exist"
        all read the tracking ref; with it gone they say there is no line.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        git("switch", "-c", branch)
        (pathlib.Path(repo_dir) / "backport.txt").write_text("fix")
        git("add", "backport.txt")
        git("commit", "-m", "fix: backport")
        head = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "main")
        bare_dir = pathlib.Path(repo_dir).parent / "origin.git"
        git("init", "--bare", str(bare_dir))
        git("remote", "add", "origin", str(bare_dir))
        git("push", "--quiet", "origin", "main", branch)
        git("branch", "-D", branch)
        git("fetch", "--quiet", "origin")
        self.assertEqual(git("rev-parse", f"refs/remotes/origin/{branch}").stdout.strip(), head)

        candidate = self._run_common_func(f'release_line_candidate "{MOCK_TARGET_RELEASE_LINE}"', cwd=repo_dir)
        self.assertEqual(candidate.returncode, 0, candidate.stderr)
        self.assertEqual(candidate.stdout.strip(), head)
        exists = self._run_common_func(f'release_line_branch_exists "{MOCK_TARGET_RELEASE_LINE}"', cwd=repo_dir)
        self.assertEqual(exists.returncode, 0, exists.stderr)

        git("update-ref", "-d", f"refs/remotes/origin/{branch}")
        gone = self._run_common_func(f'release_line_candidate "{MOCK_TARGET_RELEASE_LINE}"', cwd=repo_dir)
        self.assertNotEqual(gone.returncode, 0)
        self.assertIn(f"No local release line '{branch}'", gone.stderr)
        exists = self._run_common_func(f'if release_line_branch_exists "{MOCK_TARGET_RELEASE_LINE}"; then echo yes; else echo "no rc=$?"; fi', cwd=repo_dir)
        self.assertEqual(exists.stdout.strip(), "no rc=1")

    def test_in_ci_a_local_line_that_moved_on_from_the_remote_is_refused_not_moved(self):
        """The remote decides in CI, but a local branch holding work the remote never
        took is refused before anything is moved, whether the remote lacks the line
        or holds it at the candidate; a leftover stamp of the version is still moved."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        candidate = git("rev-parse", "HEAD").stdout.strip()
        git("push", "--quiet", str(bare_dir), "main")
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.2.0\n")
        git("add", "stamp.txt")
        git("commit", "-m", "chore(release): stamp release version 0.2.0")
        release_commit = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "--detach", "-q", candidate)
        (pathlib.Path(repo_dir) / "unpushed.txt").write_text("work\n")
        git("add", "unpushed.txt")
        git("commit", "-m", "fix: never pushed")
        unpushed = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "main")
        ci = {"CI": "true", **self._FAKE_RELEASE_REPO}
        call = f'release_branch_placement "{MOCK_TARGET_RELEASE_TAG}" "{release_commit}" "{candidate}"'

        git("branch", branch, unpushed)
        for remote_state in ("absent", "at the candidate"):
            with self.subTest(remote=remote_state):
                if remote_state == "at the candidate":
                    git("push", "--quiet", str(bare_dir), f"{candidate}:refs/heads/{branch}")
                proc = self._run_common_func(call, env=ci, cwd=repo_dir)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("holds work the remote never took", proc.stderr)
                self.assertEqual(git("rev-parse", branch).stdout.strip(), unpushed)

        # A leftover stamp of the version (a dry run's) is the case the remote read exists for.
        git("branch", "-f", branch, release_commit)
        proc = self._run_common_func(call, env=ci, cwd=repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "remote-candidate")

    def test_release_branch_placement_reports_where_the_branch_is(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bare_dir = self._bare_remote_for(git, repo_dir)
        head = git("rev-parse", "HEAD").stdout.strip()
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        call = f'release_branch_placement "{MOCK_TARGET_RELEASE_TAG}" "{head}"'
        ci = {"CI": "true", **self._FAKE_RELEASE_REPO}

        absent = self._run_common_func(call, env=ci, cwd=repo_dir)
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertEqual(absent.stdout.strip(), "absent")

        # In CI a local branch the remote lacks is the leftover of a rejected push,
        # and reads as absent, with a note; the remote is the only read.
        git("branch", branch, head)
        leftover = self._run_common_func(call, env=ci, cwd=repo_dir)
        self.assertEqual(leftover.returncode, 0, leftover.stderr)
        self.assertEqual(leftover.stdout.strip(), "absent")
        self.assertIn("exists only in this checkout", leftover.stderr)

        git("push", str(bare_dir), f"{head}:refs/heads/{branch}")
        remote = self._run_common_func(call, env=ci, cwd=repo_dir)
        self.assertEqual(remote.returncode, 0, remote.stderr)
        self.assertEqual(remote.stdout.strip(), "remote")

        # Off CI the remote is not consulted; the local branch is what there is.
        off_ci = self._run_common_func(call, cwd=repo_dir)
        self.assertEqual(off_ci.returncode, 0, off_ci.stderr)
        self.assertEqual(off_ci.stdout.strip(), "local")

    # ─── release_resolve_target ───────────────────────────────────────────────
    # The targeting trio must never be guessed in CI: a defaulted PROJECT_ID
    # points provision/teardown at a real project nobody named.

    _RESOLVE = (
        "unset GKE_CLUSTER_NAME GCP_REGION GCP_PROJECT_ID CLUSTER_NAME REGION "
        "PROJECT_ID AGENT_NAMESPACE || true\n"
    )
    _ECHO = 'echo "${CLUSTER_NAME}|${REGION}|${PROJECT_ID}|${AGENT_NAMESPACE}"'

    def test_release_resolve_target_fails_in_ci_when_targeting_vars_unset(self):
        proc = self._run_common_func(
            f"{self._RESOLVE}release_resolve_target",
            env={"CI": "true"},
        )
        self.assertNotEqual(proc.returncode, 0, "CI must not fall back to a default project")
        for var in ("GKE_CLUSTER_NAME", "GCP_REGION", "GCP_PROJECT_ID"):
            self.assertIn(var, proc.stderr)

    def test_release_resolve_target_names_only_the_missing_variable(self):
        """The error is a pointer to the misconfigured `env:` entry, so it must be precise."""
        proc = self._run_common_func(
            f"{self._RESOLVE}"
            "export GKE_CLUSTER_NAME=c GCP_REGION=r AGENT_NAMESPACE=n\n"
            "release_resolve_target",
            env={"CI": "true"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("GCP_PROJECT_ID", proc.stderr)
        self.assertNotIn("GKE_CLUSTER_NAME", proc.stderr)
        self.assertNotIn("GCP_REGION", proc.stderr)
        self.assertNotIn("AGENT_NAMESPACE", proc.stderr)

    def test_release_resolve_target_passes_in_ci_when_set(self):
        proc = self._run_common_func(
            f"{self._RESOLVE}"
            "export GKE_CLUSTER_NAME=rc-cluster GCP_REGION=us-central1 GCP_PROJECT_ID=proj "
            "AGENT_NAMESPACE=kubeagents-system\n"
            f"release_resolve_target\n{self._ECHO}",
            env={"CI": "true"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rc-cluster|us-central1|proj|kubeagents-system", proc.stdout)

    def test_release_resolve_target_defaults_off_ci(self):
        """The developer path keeps its defaults; that is what the trio is for."""
        proc = self._run_common_func(f"{self._RESOLVE}release_resolve_target\n{self._ECHO}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("platform-agent-host|us-central1|kube-agents-rc|kubeagents-system", proc.stdout)

    def test_release_resolve_target_requires_agent_namespace_in_ci(self):
        """`vars.AGENT_NAMESPACE` expanding to empty must not read as the default.

        The rc and nightly environments both define it, so a job that sets the
        targeting trio but not this one is misconfigured — and silently getting
        `kubeagents-system` is what made that invisible.
        """
        proc = self._run_common_func(
            f"{self._RESOLVE}"
            "export GKE_CLUSTER_NAME=c GCP_REGION=r GCP_PROJECT_ID=p\n"
            "release_resolve_target",
            env={"CI": "true"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("AGENT_NAMESPACE", proc.stderr)

    # ── commit_messages_have_breaking_change ─────────────────────────────────
    #
    # Shared by calculate_next_version.sh, which reads it to pick the bump, and
    # resolve_scheduled_release.sh, which reads it to decide whether an
    # unattended release on stable GA (>= 1.0.0) stops for a human (while
    # pre-1.0 breaking changes bump MINOR and release unattended). The two
    # disagreeing is silent in the unsafe direction, so the last test here pins
    # that neither keeps a copy.

    def test_commit_messages_have_breaking_change_detects_a_bang_subject(self):
        for subject in ("feat!: drop it", "fix(operator)!: drop the v1alpha1 field"):
            with self.subTest(subject=subject):
                proc = self._run_common_func(f'commit_messages_have_breaking_change "{subject}" ""')
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_commit_messages_have_breaking_change_detects_a_footer(self):
        for body in ("BREAKING CHANGE: the yaml spec moved", "BREAKING-CHANGE: the yaml spec moved"):
            with self.subTest(body=body):
                proc = self._run_common_func(f'commit_messages_have_breaking_change "" "{body}"')
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_commit_messages_have_breaking_change_ignores_ordinary_commits(self):
        proc = self._run_common_func(
            'commit_messages_have_breaking_change "feat: add a thing\nfix: mend a thing" "just prose"'
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_commit_messages_have_breaking_change_survives_a_large_corpus(self):
        """`echo … | grep -q` would report 141 here, which reads as "not breaking".

        grep exits on its first match, the producer dies on SIGPIPE, and under
        `set -o pipefail` the pipeline reports 141 — so matching input reads as no
        breaking change, in the direction that ships one unattended. The herestring
        form is immune, and this is what holds it that way.
        """
        proc = self._run_common_func(
            'set -o pipefail\n'
            'big="BREAKING CHANGE: something"$\'\\n\'"$(head -c 400000 /dev/zero | tr "\\0" "y")"\n'
            'commit_messages_have_breaking_change "" "${big}"'
        )
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr} rc={proc.returncode}")

    # ── release_read_commit_range ────────────────────────────────────────────

    def test_release_read_commit_range_reports_subjects_and_bodies(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", "0.1.0", "-m", "r")
            (pathlib.Path(repo_dir) / "b.txt").write_text("b\n")
            git("add", "b.txt")
            git("commit", "-m", "feat: a thing\n\nBREAKING CHANGE: it moved")
            proc = self._run_common_func(
                'release_read_commit_range "0.1.0" "HEAD"\n'
                'echo "S=${RELEASE_RANGE_SUBJECTS}"\necho "B=${RELEASE_RANGE_BODIES}"',
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("S=feat: a thing", proc.stdout)
            self.assertIn("BREAKING CHANGE: it moved", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_release_read_commit_range_keeps_git_warnings_out_of_the_subjects(self):
        """An empty range must read as empty even when git warns on success.

        A branch sharing a GA tag's name makes `git log 0.1.0..HEAD` succeed and
        warn about the ambiguous refname. Captured with `2>&1` that warning
        becomes the subject list, so an empty range reads as "there are commits
        to ship" — and the scheduled gate publishes a release for a week with
        nothing in it.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", "0.1.0", "-m", "r")
            git("branch", "0.1.0")
            proc = self._run_common_func(
                'release_read_commit_range "0.1.0" "HEAD"\n'
                'echo "SUBJECTS=[${RELEASE_RANGE_SUBJECTS}]"',
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("SUBJECTS=[]", proc.stdout)
            self.assertNotIn("ambiguous", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_release_read_commit_range_fails_and_reports_on_a_bad_range(self):
        temp_dir, repo_dir, _ = create_mock_git_repo()
        try:
            proc = self._run_common_func(
                'release_read_commit_range "no-such-tag" "HEAD"', cwd=repo_dir
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to read commit log", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_neither_caller_keeps_its_own_copy_of_the_range_read(self):
        """Scoping the bump and the halt to different commit sets is silent."""
        for script in ("calculate_next_version.sh", "resolve_scheduled_release.sh"):
            with self.subTest(script=script):
                text = (_REPO_ROOT / "scripts" / "release" / script).read_text()
                body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
                self.assertNotIn(
                    '--format="%s"',
                    body,
                    f"{script} reads the commit range itself instead of calling common.sh",
                )
                self.assertIn("release_read_commit_range", body, f"{script} does not call the helper")

    def test_neither_caller_keeps_its_own_copy_of_the_breaking_regexes(self):
        """A second copy is how the bump and the halt come to disagree."""
        bang_regex = r"^[a-z]+(\([^)]+\))?!:"
        for script in ("calculate_next_version.sh", "resolve_scheduled_release.sh"):
            with self.subTest(script=script):
                text = (_REPO_ROOT / "scripts" / "release" / script).read_text()
                body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
                self.assertNotIn(
                    bang_regex,
                    body,
                    f"{script} re-implements the breaking-change test instead of calling common.sh",
                )
                self.assertIn("commit_messages_have_breaking_change", body, f"{script} does not call the helper")

    def test_both_callers_use_ga_tag_is_initial_development(self):
        """Both calculate_next_version.sh and resolve_scheduled_release.sh must use the shared predicate."""
        for script in ("calculate_next_version.sh", "resolve_scheduled_release.sh"):
            with self.subTest(script=script):
                text = (_REPO_ROOT / "scripts" / "release" / script).read_text()
                body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
                self.assertIn("ga_tag_is_initial_development", body, f"{script} does not call ga_tag_is_initial_development")

    # ── ga_tag_is_initial_development ────────────────────────────────────────

    def test_ga_tag_is_initial_development_true_for_zero_major(self):
        for tag in ("0.1.0", "0.9.0", "0.10.0", "0.0.1"):
            with self.subTest(tag=tag):
                proc = self._run_common_func(f'ga_tag_is_initial_development "{tag}"')
                self.assertEqual(proc.returncode, 0, f"Expected {tag} to be initial development")

    def test_ga_tag_is_initial_development_false_for_stable_and_invalid(self):
        for tag in ("1.0.0", "1.2.3", "2.0.0", "invalid", "", "foo.bar"):
            with self.subTest(tag=tag):
                proc = self._run_common_func(f'ga_tag_is_initial_development "{tag}"')
                self.assertNotEqual(proc.returncode, 0, f"Expected {tag} not to be initial development")

    def test_release_bundle_registries(self):
        """Verifies common.sh exports release bundle directories, root files, and charts."""
        script = """
echo "DIRS:${RELEASE_BUNDLE_DIRECTORIES[*]}"
echo "CHARTS:${RELEASE_HELM_CHARTS[*]}"
echo "FILES:${RELEASE_BUNDLE_ROOT_FILES[*]}"
"""
        proc = self._run_common_func(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DIRS:terraform k8s-operator deploy charts scripts examples", proc.stdout)
        self.assertIn("CHARTS:charts/kube-agents", proc.stdout)
        self.assertIn("FILES:install.sh uninstall.sh upgrade.sh install.defaults.env install.env.example images.json Makefile INSTALL.md README.md LICENSE", proc.stdout)

    def test_extract_commit_tree(self):
        """Verifies extract_commit_tree extracts exact committed files to target directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            head_commit = subprocess.check_output(
                ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"], text=True
            ).strip()
            target_dir = pathlib.Path(temp_dir) / "extracted"
            proc = self._run_common_func(
                f'extract_commit_tree "{head_commit}" "{target_dir}" "README.md"',
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            extracted_file = target_dir / "README.md"
            self.assertTrue(extracted_file.exists())
            self.assertEqual(extracted_file.read_text(), (_REPO_ROOT / "README.md").read_text())

    def test_stamp_baked_release_version_fails_when_script_missing(self):
        """Verifies stamp_baked_release_version fails loudly if an installer script is missing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            (temp_path / "install.sh").write_text('BAKED_RELEASE_VERSION=""\n')
            proc = self._run_common_func(f'stamp_baked_release_version "1.2.3" "{temp_dir}"')
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Target installer script not found", proc.stderr)

    def test_stamp_helm_chart_versions_fails_when_chart_missing(self):
        """Verifies stamp_helm_chart_versions fails loudly if Chart.yaml is missing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            proc = self._run_common_func(f'stamp_helm_chart_versions "1.2.3" "{temp_dir}"')
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Helm chart file not found", proc.stderr)

    def test_stamp_terraform_release_versions_fails_when_file_missing(self):
        """Verifies stamp_terraform_release_versions fails loudly if variables.tf or tfvars is missing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            proc = self._run_common_func(f'stamp_terraform_release_versions "1.2.3" "{temp_dir}"')
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Terraform variables file not found", proc.stderr)

    def test_create_stamped_release_commit_fails_when_candidate_files_are_dirty(self):
        """Verifies create_stamped_release_commit refuses to run if candidate files have uncommitted changes."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            from tests.testing.release import populate_mock_release_files
            populate_mock_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: initial commit with valid release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            chart_file = pathlib.Path(repo_dir) / "charts" / "kube-agents" / "Chart.yaml"
            chart_file.write_text(chart_file.read_text() + "\n# scratch edit\n")

            proc = self._run_common_func(
                f'create_stamped_release_commit "1.0.0" "{main_commit}" "{repo_dir}"',
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Cannot create stamped release commit with uncommitted changes in release files", proc.stderr)
            self.assertIn("charts/kube-agents/Chart.yaml", proc.stderr)
        finally:
            temp_dir.cleanup()


class RegistryImageProbeTest(unittest.TestCase):
    """`registry_image_exists` must not read "no docker" as "no image".

    `docker manifest inspect` is the probe that works against every registry,
    and three other call sites in common.sh already check for the binary before
    using it. `check_commit_images_exist` did not, so on a machine with no
    docker it reported every image missing — which `find_latest_built_commit`
    turns into "no commit in the last 30 has published images", a publish
    outage that is really a missing binary. The Prow job image is exactly such
    a machine: hack/ci-deploy.sh builds through `gcloud builds submit` because
    there is no docker daemon there, and hack/resolve-rc-target.sh runs in the
    same place.
    """

    _IMAGE = f"{MOCK_DEFAULT_REGISTRY_PREFIX}/platform-agent:{MOCK_SAMPLE_COMMIT_SHA}"
    # Deliberately not ghcr.io: the curl fallback speaks one registry's API.
    _FOREIGN_IMAGE = f"us-central1-docker.pkg.dev/p/r/platform-agent:{MOCK_SAMPLE_COMMIT_SHA}"
    _REPOSITORY_PATH = "gke-labs/kube-agents/platform-agent"
    _DIGEST = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

    def _bin(self, tmp):
        """A PATH holding the base utilities, symlinked, and no docker.

        Most of this suite is about what happens when docker is ABSENT, so the
        binary has to be absent from PATH rather than merely shadowed. Naming
        system directories does not achieve that: `ubuntu-latest` ships docker
        at `/usr/bin/docker`, which put the real binary in front of six of
        these cases and let them pass on a laptop with no docker installed
        while failing in CI.
        """
        return create_minimal_tools_bin(tmp)

    def _run(self, func_call, bin_dir, env=None):
        """common.sh sourced with PATH pinned to bin_dir, and nothing else."""
        overrides = {"PATH": str(bin_dir)}
        overrides.update(env or {})
        return subprocess.run(
            ["bash", "-c", f'source "{_COMMON_SH}"\n{func_call}'],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides),
            cwd=str(_REPO_ROOT),
        )

    def test_docker_is_used_when_it_is_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_docker_binary(bin_dir, existing_images=[self._IMAGE])
            found = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertEqual(found.returncode, 0, found.stderr)
            missing = self._run(f'registry_image_exists "{self._IMAGE}x"', bin_dir)
            self.assertNotEqual(missing.returncode, 0)

    def test_a_published_image_is_found_without_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir)
            proc = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_an_unpublished_image_is_still_missing_without_docker(self):
        """The fallback must not turn into an unconditional yes."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir, manifest_status=1)
            proc = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertNotEqual(proc.returncode, 0)

    def test_an_unauthenticated_registry_is_missing_rather_than_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir, token_response="")
            proc = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertNotEqual(proc.returncode, 0)

    def test_another_registry_without_docker_says_why(self):
        """Reporting "missing" for a registry the fallback cannot query is a
        lie about the image. It still fails, but it must say what it is."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir)
            proc = self._run(f'registry_image_exists "{self._FOREIGN_IMAGE}"', bin_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("no docker on PATH", proc.stderr)

    def _manifest_probe(self, log_path):
        """The one logged call that is not the token request."""
        calls = [line for line in log_path.read_text().splitlines() if "/manifests/" in line]
        self.assertEqual(len(calls), 1, log_path.read_text())
        return calls[0]

    def test_the_probe_carries_the_url_and_headers_ghcr_answers_on(self):
        """GHCR returns 404, not 401, for a manifest request that omits the OCI
        media types — a false "image missing" indistinguishable from the real
        thing. Asserting only the exit code leaves both the URL and the header
        free to be anything, so this reads the request the mock recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            _, log_path = create_mock_ghcr_curl_binary(bin_dir)
            proc = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            probe = self._manifest_probe(pathlib.Path(log_path))
            self.assertIn(
                f"https://ghcr.io/v2/{self._REPOSITORY_PATH}/manifests/{MOCK_SAMPLE_COMMIT_SHA}",
                probe,
            )
            self.assertIn("Authorization: Bearer t", probe)
            for media_type in (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ):
                self.assertIn(media_type, probe)

    def test_a_digest_reference_is_probed_as_a_digest(self):
        """`<repo>@sha256:…` is what the docker branch accepts, so the fallback
        has to accept it too — splitting it on the last colon would ask for a
        repository named `…@sha256` and a tag of hex."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            _, log_path = create_mock_ghcr_curl_binary(bin_dir)
            image = f"{MOCK_DEFAULT_REGISTRY_PREFIX}/platform-agent@{self._DIGEST}"
            proc = self._run(f'registry_image_exists "{image}"', bin_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(
                f"https://ghcr.io/v2/{self._REPOSITORY_PATH}/manifests/{self._DIGEST}",
                self._manifest_probe(pathlib.Path(log_path)),
            )

    def test_a_reference_less_image_is_probed_at_latest(self):
        """No tag and no digest means `latest` to a registry. Left unhandled it
        becomes an empty reference and a URL ending in `/manifests/`."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            _, log_path = create_mock_ghcr_curl_binary(bin_dir)
            image = f"{MOCK_DEFAULT_REGISTRY_PREFIX}/platform-agent"
            proc = self._run(f'registry_image_exists "{image}"', bin_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(
                f"https://ghcr.io/v2/{self._REPOSITORY_PATH}/manifests/latest",
                self._manifest_probe(pathlib.Path(log_path)),
            )

    def test_the_commit_check_stops_reporting_a_publish_outage(self):
        """The regression the guard exists for, at the level callers use."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir)
            proc = self._run(
                f'check_commit_images_exist "{MOCK_SAMPLE_COMMIT_SHA}"',
                bin_dir,
                env={"REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


class RequiredReleaseImagesAtCandidateTest(unittest.TestCase):
    """The required image list is the candidate's, not this checkout's (#2211).

    release-publish.yml checks out `main` and runs these scripts against a
    candidate commit. The candidate's images were built by its own publish run
    from its own REQUIRED_RELEASE_IMAGES, so a candidate from before the list
    grew (#1068, #2206) has every image it was published with and none of the
    newer names, and a gate reading main's list refuses it. On main that
    clears with the next publish; on a release line cut before the growth it
    never does. `required_release_images_at` reads the list at the candidate,
    and falls back to this checkout's -- the old behaviour -- only when the
    candidate's cannot be read, saying so.
    """

    _REGISTRY = {"REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX}

    def _run(self, func_call, cwd, bin_dir=None, env=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", "-c", f'source "{_COMMON_SH}"\n{func_call}'],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd,
        )

    def _repo(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        return temp_dir, repo_dir, git

    def _docker_with(self, temp_dir, images, sha):
        """A docker mock whose registry holds exactly `images` at `:<sha>`."""
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        create_mock_docker_binary(
            bin_dir,
            existing_images=[f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{sha}" for img in images],
        )
        return str(bin_dir)

    def test_the_gate_accepts_a_candidate_with_every_image_its_own_list_names(self):
        """Red before the fix: the gate asked for this checkout's longer list."""
        self.assertGreater(len(MOCK_REQUIRED_RELEASE_IMAGES), len(MOCK_CANDIDATE_RELEASE_IMAGES))
        temp_dir, repo_dir, git = self._repo()
        candidate = commit_required_release_images(repo_dir, git, MOCK_CANDIDATE_RELEASE_IMAGES)
        bin_dir = self._docker_with(temp_dir, MOCK_CANDIDATE_RELEASE_IMAGES, candidate)

        proc = self._run(f'check_commit_images_exist "{candidate}"', cwd=repo_dir, bin_dir=bin_dir, env=self._REGISTRY)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            f"the {len(MOCK_CANDIDATE_RELEASE_IMAGES)} {REQUIRED_RELEASE_IMAGES_PATH} lists at {candidate[:7]}",
            proc.stderr,
        )

    def test_the_gate_still_refuses_a_candidate_missing_one_of_its_own_images(self):
        temp_dir, repo_dir, git = self._repo()
        candidate = commit_required_release_images(repo_dir, git, MOCK_CANDIDATE_RELEASE_IMAGES)
        bin_dir = self._docker_with(temp_dir, MOCK_CANDIDATE_RELEASE_IMAGES[:-1], candidate)

        proc = self._run(f'check_commit_images_exist "{candidate}"', cwd=repo_dir, bin_dir=bin_dir, env=self._REGISTRY)
        self.assertNotEqual(proc.returncode, 0)

    def test_the_candidates_list_wins_when_it_is_longer_than_this_checkouts(self):
        """A release line that grew its list by backport is held to every name
        it has, however old the main these scripts run from."""
        temp_dir, repo_dir, git = self._repo()
        candidate = commit_required_release_images(repo_dir, git, MOCK_GROWN_RELEASE_IMAGES)

        # Every name this checkout lists is published, the backported one is not.
        bin_dir = self._docker_with(temp_dir, MOCK_REQUIRED_RELEASE_IMAGES, candidate)
        proc = self._run(f'check_commit_images_exist "{candidate}"', cwd=repo_dir, bin_dir=bin_dir, env=self._REGISTRY)
        self.assertNotEqual(proc.returncode, 0, "the checkout's shorter list must not vouch for the candidate")

        bin_dir = self._docker_with(temp_dir, MOCK_GROWN_RELEASE_IMAGES, candidate)
        proc = self._run(f'check_commit_images_exist "{candidate}"', cwd=repo_dir, bin_dir=bin_dir, env=self._REGISTRY)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_find_latest_built_commit_walks_back_to_a_pre_growth_commit(self):
        """The self-healing case: with the post-growth commit's publish not yet
        done, the newest commit whose own images are all there is the one
        before the growth. Red before the fix: nothing in history qualified."""
        temp_dir, repo_dir, git = self._repo()
        older = commit_required_release_images(repo_dir, git, MOCK_CANDIDATE_RELEASE_IMAGES, "build: the list before the growth")
        newer = commit_required_release_images(repo_dir, git, MOCK_REQUIRED_RELEASE_IMAGES, "build(a2a): the list after the growth")
        bin_dir = self._docker_with(temp_dir, MOCK_CANDIDATE_RELEASE_IMAGES, older)

        proc = self._run("find_latest_built_commit", cwd=repo_dir, bin_dir=bin_dir, env=self._REGISTRY)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), older)
        self.assertIn(f"Images not ready yet in GHCR for commit {newer[:7]}", proc.stderr)

    def test_the_fallback_is_this_checkouts_list_and_says_why(self):
        """Every way the candidate's list cannot be read falls back to this
        checkout's whole list -- never an empty one -- with the reason on stderr."""
        temp_dir, repo_dir, git = self._repo()
        no_file = git("rev-parse", "HEAD").stdout.strip()
        path = pathlib.Path(repo_dir) / REQUIRED_RELEASE_IMAGES_PATH
        path.parent.mkdir(parents=True)
        path.write_text("#!/usr/bin/env bash\necho no list here\n")
        git("add", REQUIRED_RELEASE_IMAGES_PATH)
        git("commit", "-m", "build: a common.sh without the array")
        no_array = git("rev-parse", "HEAD").stdout.strip()
        write_required_release_images(repo_dir, [])
        git("add", REQUIRED_RELEASE_IMAGES_PATH)
        git("commit", "-m", "build: an empty array")
        empty_array = git("rev-parse", "HEAD").stdout.strip()
        unreadable = f"{REQUIRED_RELEASE_IMAGES_PATH} at {{sha}} has no readable REQUIRED_RELEASE_IMAGES"
        cases = [
            ("", "no candidate commit named"),
            (MOCK_SAMPLE_COMMIT_SHA, f"commit {MOCK_SAMPLE_COMMIT_SHA[:7]} is not in this repository"),
            (no_file, f"{no_file[:7]} has no {REQUIRED_RELEASE_IMAGES_PATH}"),
            (no_array, unreadable.format(sha=no_array[:7])),
            (empty_array, unreadable.format(sha=empty_array[:7])),
        ]
        for sha, reason in cases:
            with self.subTest(sha=sha[:7] or "<none>"):
                proc = self._run(f'required_release_images_at "{sha}"', cwd=repo_dir)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.split(), MOCK_REQUIRED_RELEASE_IMAGES)
                self.assertIn(reason, proc.stderr)
                self.assertIn(
                    f"using the {len(MOCK_REQUIRED_RELEASE_IMAGES)} this checkout's {REQUIRED_RELEASE_IMAGES_PATH} lists",
                    proc.stderr,
                )

    def test_the_text_parse_matches_the_wiring_tests(self):
        """common.sh's parse and tests/test_promotion_pipeline_wiring.py's are
        two implementations of one rule; a shape one accepts and the other
        rejects is how the gate and the wiring check would come to disagree
        about what the list says."""
        real = _COMMON_SH.read_text()
        shapes = {
            "this checkout's common.sh": real,
            "two names": 'export REQUIRED_RELEASE_IMAGES=(\n  "a"\n  "b-c"\n)\n',
            "names beside the parens": 'export REQUIRED_RELEASE_IMAGES=("a"\n  "b")\n',
            "blank lines inside": 'export REQUIRED_RELEASE_IMAGES=(\n\n  "a"\n\n)\n',
            "a comment inside": 'export REQUIRED_RELEASE_IMAGES=(\n  # the operator\n  "a"\n)\n',
            "two on one line": 'export REQUIRED_RELEASE_IMAGES=(\n  "a" "b"\n)\n',
            "unquoted": 'export REQUIRED_RELEASE_IMAGES=(\n  a\n)\n',
            "a duplicate": 'export REQUIRED_RELEASE_IMAGES=(\n  "a"\n  "a"\n)\n',
            "empty": "export REQUIRED_RELEASE_IMAGES=()\n",
            "absent": "echo nothing\n",
        }
        accepted = 0
        for label, text in shapes.items():
            with self.subTest(shape=label):
                try:
                    expected = parse_required_release_images(text)
                except ValueError:
                    expected = None
                with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
                    handle.write(text)
                self.addCleanup(os.unlink, handle.name)
                proc = self._run(f'required_release_images_in_text < "{handle.name}"', cwd=str(_REPO_ROOT))
                if expected is None:
                    self.assertNotEqual(proc.returncode, 0, f"bash accepted what the wiring test rejects: {proc.stdout!r}")
                    self.assertEqual(proc.stdout, "")
                else:
                    accepted += 1
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.split(), expected)
        self.assertEqual(accepted, 4, "the shapes cover both verdicts")
        sourced = self._run('printf "%s\\n" "${REQUIRED_RELEASE_IMAGES[@]}"', cwd=str(_REPO_ROOT))
        self.assertEqual(sourced.stdout.split(), parse_required_release_images(real), "the parse reads the array bash sources")

    def test_the_version_keyed_read_is_the_tags_list_or_says_there_is_no_tag(self):
        """Signing and the SBOMs are handed only the version; the list is the
        one the version's tag commit carries, and without the tag the notice
        names the tag before the fallback names the list."""
        temp_dir, repo_dir, git = self._repo()
        proc = self._run(f'required_release_images_for_release "{MOCK_TARGET_RELEASE_TAG}"', cwd=repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"No tag '{MOCK_TARGET_RELEASE_TAG}' in this repository", proc.stderr)
        self.assertIn("no candidate commit named", proc.stderr)
        self.assertEqual(proc.stdout.split(), MOCK_REQUIRED_RELEASE_IMAGES)

        release_commit = commit_required_release_images(repo_dir, git, MOCK_CANDIDATE_RELEASE_IMAGES)
        git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)
        proc = self._run(f'required_release_images_for_release "{MOCK_TARGET_RELEASE_TAG}"', cwd=repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("No tag", proc.stderr)
        self.assertIn(f"lists at {release_commit[:7]}", proc.stderr)
        self.assertEqual(proc.stdout.split(), MOCK_CANDIDATE_RELEASE_IMAGES)

    def test_promote_release_images_promotes_the_candidates_list(self):
        temp_dir, repo_dir, git = self._repo()
        candidate = commit_required_release_images(repo_dir, git, MOCK_CANDIDATE_RELEASE_IMAGES)
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        create_mock_docker_binary(bin_dir)

        proc = self._run(
            f'promote_release_images "{candidate}" "{MOCK_TARGET_RELEASE_TAG}"',
            cwd=repo_dir,
            bin_dir=str(bin_dir),
            env={"CI": "true"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for img in MOCK_CANDIDATE_RELEASE_IMAGES:
            self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        for img in set(MOCK_REQUIRED_RELEASE_IMAGES) - set(MOCK_CANDIDATE_RELEASE_IMAGES):
            self.assertNotIn(f"Promoting {img}", proc.stdout, f"{img} is not in the candidate's list")


if __name__ == "__main__":
    unittest.main()

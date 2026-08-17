"""Tests for agentplugins/lib/plugin_image.sh, the image plumbing both installers share.

    python3 -m unittest discover -s agentplugins/lib/tests -v

Three of the things this library decides fail SILENTLY when they are wrong, which is why
they are covered here rather than discovered on a live install:

  * **Which files reach the image.** A bespoke `.dockerignore` reader and a segment-wise
    glob matcher serve the content tag, `docker build` and the crane layer alike. A pattern
    that matches differently from Docker puts a different file set under one tag — and
    since a published tag is never rebuilt, whichever builder reaches it first defines that
    image permanently. The library's own header names this as the worst case it is designed
    against.
  * **The content tag.** It must move when the source or the build moves and stand still
    when neither did. A tag that fails to move means the publish step reports the image
    already published and the edit never reaches a cluster, on a deployment that reports
    healthy — the `latest` failure the whole scheme exists to prevent.
  * **Where the image is published.** Copied from the agent's own image, because nothing in
    this repository grants `artifactregistry.reader` on a repository of our own invention.

Everything here shells out to bash and touches no network, no daemon and no cluster: the
functions exercised are the pure ones, and where `kubectl` or `crane` is reached at all it
is a stub on PATH. The environment is built from scratch rather than inherited — `REGION`,
`AR_*` and `PLUGIN_IMAGE*` all change what the library does, and a developer's shell must
not decide whether the suite passes.
"""

import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

LIB = pathlib.Path(__file__).resolve().parents[1] / "plugin_image.sh"

# Resolved once, and invoked by absolute path: several tests hand the library a PATH with
# no image builder on it, and looking bash itself up on that PATH would fail to start.
BASH = shutil.which("bash") or "/bin/bash"

# Only what bash itself needs. Nothing that the library reads: see the module docstring.
BASE_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "LC_ALL": "C",
}

# The external commands the library actually shells out to. A test that wants no image
# builder on PATH gets a directory holding these and nothing else — see tool_only_path.
_LIB_TOOLS = (
    "find", "sed", "sort", "cut", "cat", "cp", "rm", "mkdir", "chmod", "mktemp",
    "tar", "shasum", "sha256sum",
)


def run_lib(snippet, env=None, cwd=None, prelude="set -u"):
    """Source the library in a fresh bash and run `snippet` against it.

    `set -u` by default because the library is written for it — `${arr[@]}` on an empty
    array is an unbound-variable error under the bash 3.2 that ships on macOS, which is why
    the exclusion list is newline-separated text rather than an array.
    """
    full_env = dict(BASE_ENV)
    full_env.update(env or {})
    script = '%s\n. "%s"\n%s\n' % (prelude, LIB, snippet)
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=full_env,
    )


def tool_only_path(test, *extra_dirs):
    """A PATH with the library's coreutils on it and no docker, crane or gcloud.

    Emptying PATH instead would break `find` and `shasum` too, and inheriting the real one
    would make "no builder is installed" mean whatever the machine running the suite
    happens to have — docker is on the PATH of a GitHub runner and usually not on a laptop.
    """
    bin_dir = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-tools-"))
    test.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
    for tool in _LIB_TOOLS:
        found = shutil.which(tool)
        if found:
            os.symlink(found, bin_dir / tool)
    return os.pathsep.join([str(d) for d in extra_dirs] + [str(bin_dir)])


class PluginTreeMixin:
    """Builds throwaway plugin directories shaped like the real ones."""

    def make_plugin(self, files=None, dockerignore=None, dockerfile="FROM scratch\nCOPY files/ /\n"):
        root = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-test-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        if dockerfile is not None:
            (root / "Dockerfile").write_text(dockerfile)
        if dockerignore is not None:
            (root / ".dockerignore").write_text(dockerignore)
        for rel, body in (files or {}).items():
            self.write(root, rel, body)
        return root

    def write(self, root, rel, body):
        path = pathlib.Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def content_tag(self, plugin, src=None, env=None):
        src = src or (plugin / "files")
        result = run_lib(
            'plugin_image_content_tag "%s" "%s"' % (plugin, src), env=env
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()


class IgnoreLoadTests(PluginTreeMixin, unittest.TestCase):
    """plugin_image_ignore_load: what it accepts, and what it refuses outright.

    Refusing is the feature. A pattern this cannot match the way Docker matches it is the
    silent divergence above, so the library dies rather than guessing at it.
    """

    def load(self, dockerignore):
        plugin = self.make_plugin(dockerignore=dockerignore)
        return plugin, run_lib(
            'plugin_image_ignore_load "%s"\nprintf "[%%s]" "$PLUGIN_IMAGE_IGNORE"' % plugin
        )

    def test_comments_and_blank_lines_are_dropped(self):
        _, result = self.load("# a comment\n\n   \n**/*.pyc\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "[**/*.pyc\n]")

    def test_surrounding_whitespace_is_trimmed(self):
        # Docker trims each pattern before matching, so a padded line that this kept
        # verbatim would exclude nothing here and the file there: one tag, two file sets.
        plugin, result = self.load("  **/*.pyc \t\n")
        self.assertEqual(result.stdout, "[**/*.pyc\n]")
        self.assertTrue(self.ignored(plugin, "files/a.pyc"))

    def test_anchoring_spellings_normalise_to_one_pattern(self):
        # Docker anchors at the context root either way, so all three are the same rule.
        _, result = self.load("/files/a.txt\n./files/b.txt\nfiles/c/\n")
        self.assertEqual(result.stdout, "[files/a.txt\nfiles/b.txt\nfiles/c\n]")

    def test_carriage_returns_are_stripped(self):
        # A CRLF checkout would otherwise leave every pattern matching nothing, silently.
        plugin, result = self.load("**/__pycache__\r\n**/*.pyc\r\n")
        self.assertEqual(result.stdout, "[**/__pycache__\n**/*.pyc\n]")
        self.assertTrue(self.ignored(plugin, "files/x/__pycache__"))

    def test_final_line_without_a_newline_is_read(self):
        _, result = self.load("**/*.pyc")
        self.assertEqual(result.stdout, "[**/*.pyc\n]")

    def test_missing_dockerignore_excludes_nothing(self):
        plugin = self.make_plugin(files={"files/a.txt": "a"})
        result = run_lib(
            'plugin_image_ignore_load "%s"\nprintf "[%%s]" "$PLUGIN_IMAGE_IGNORE"' % plugin
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "[]")

    def test_negation_is_refused(self):
        _, result = self.load("**/*.pyc\n!keep.pyc\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("negation", result.stderr)

    def test_a_padded_negation_is_refused_too(self):
        # The case the trim above is really for, and the one where keeping the line verbatim
        # was not merely useless but wrong: Docker trims before it looks for the `!`, so
        # `  !keep.pyc` is a re-include there. Untrimmed it is a literal pattern here that
        # matches no path at all — so the reader would accept a re-include it cannot honour,
        # excluding a file `docker build` keeps, which is the silent divergence between the
        # two builders that refusing negations exists to prevent.
        _, result = self.load("**/*.pyc\n  \t!keep.pyc \n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("negation", result.stderr)

    def test_double_star_in_the_middle_is_refused(self):
        _, result = self.load("files/**/x.txt\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("somewhere other than the start", result.stderr)

    def test_trailing_double_star_is_refused(self):
        _, result = self.load("files/**\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("somewhere other than the start", result.stderr)

    def test_second_double_star_after_a_leading_one_is_refused(self):
        _, result = self.load("**/a/**/b\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than one", result.stderr)

    def ignored(self, plugin, path):
        result = run_lib(
            'plugin_image_ignore_load "%s"\n'
            'if plugin_image_ignored "%s"; then echo YES; else echo NO; fi' % (plugin, path)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip() == "YES"


class MatcherTests(PluginTreeMixin, unittest.TestCase):
    """The segment-wise matcher, against the paths a plugin actually contains.

    The case that matters most is `*` not crossing a `/`: bash's own `==` would let it, so
    an anchored `*.pyc` would quietly exclude `skills/x.pyc` too and the image would ship a
    different file set from the one Docker builds.
    """

    def ignored(self, dockerignore, path):
        plugin = self.make_plugin(dockerignore=dockerignore)
        result = run_lib(
            'plugin_image_ignore_load "%s"\n'
            'if plugin_image_ignored "%s"; then echo YES; else echo NO; fi'
            % (plugin, path)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(result.stdout.strip(), ("YES", "NO"))
        return result.stdout.strip() == "YES"

    def test_unanchored_name_matches_at_the_root(self):
        self.assertTrue(self.ignored("**/__pycache__\n", "__pycache__"))

    def test_unanchored_name_matches_at_depth(self):
        self.assertTrue(self.ignored("**/__pycache__\n", "files/skills/__pycache__"))

    def test_an_excluded_directory_takes_its_contents(self):
        self.assertTrue(self.ignored("**/__pycache__\n", "files/__pycache__/x.pyc"))

    def test_unanchored_glob_matches_at_any_depth(self):
        self.assertTrue(self.ignored("**/*.pyc\n", "a.pyc"))
        self.assertTrue(self.ignored("**/*.pyc\n", "files/skills/a.pyc"))

    def test_star_does_not_cross_a_slash(self):
        # The whole reason the matcher walks segments instead of using bash's `==`.
        self.assertTrue(self.ignored("*.pyc\n", "a.pyc"))
        self.assertFalse(self.ignored("*.pyc\n", "files/a.pyc"))

    def test_anchored_path_matches_only_at_the_root(self):
        self.assertTrue(self.ignored("files/secret.txt\n", "files/secret.txt"))
        self.assertFalse(self.ignored("files/secret.txt\n", "other/files/secret.txt"))

    def test_trailing_slash_excludes_the_whole_directory(self):
        self.assertTrue(self.ignored("files/demo/\n", "files/demo"))
        self.assertTrue(self.ignored("files/demo/\n", "files/demo/a/b.txt"))
        self.assertFalse(self.ignored("files/demo/\n", "files/demo2/b.txt"))

    def test_a_pattern_with_segments_left_over_does_not_match_a_shorter_path(self):
        self.assertFalse(self.ignored("a/b/c\n", "a/b"))

    def test_question_mark_matches_one_character_within_a_segment(self):
        self.assertTrue(self.ignored("**/f?le.txt\n", "files/fole.txt"))
        self.assertFalse(self.ignored("**/f?le.txt\n", "files/fooole.txt"))

    def test_prefix_wildcard_excludes_children_through_the_ancestor_walk(self):
        # `a/*` does not match `a/b/c` on its own; it matches the ancestor `a/b`, and
        # plugin_image_ignored is what turns that into an exclusion of everything below.
        self.assertTrue(self.ignored("a/*\n", "a/b/c"))

    def test_no_patterns_excludes_nothing(self):
        self.assertFalse(self.ignored("", "files/a.txt"))


class SrcPrefixTests(PluginTreeMixin, unittest.TestCase):
    """.dockerignore patterns are relative to the context root; the crane layer is not."""

    def prefix(self, plugin, src):
        return run_lib('plugin_image_src_prefix "%s" "%s"' % (plugin, src))

    def test_source_equal_to_the_context_has_no_prefix(self):
        plugin = self.make_plugin(files={"a.txt": "a"})
        result = self.prefix(plugin, plugin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_nested_source_is_prefixed(self):
        plugin = self.make_plugin(files={"files/platforms/pubsub/a.py": "a"})
        result = self.prefix(plugin, plugin / "files" / "platforms" / "pubsub")
        self.assertEqual(result.stdout, "files/platforms/pubsub/")

    def test_dot_dot_is_resolved_before_comparing(self):
        plugin = self.make_plugin(files={"files/a.txt": "a"})
        result = self.prefix(plugin, "%s/files/../files" % plugin)
        self.assertEqual(result.stdout, "files/")

    def test_source_outside_the_context_is_refused(self):
        # A Dockerfile can only COPY from inside its own context, so this means the two
        # arguments disagree about what is being built. Guessing puts image and tag out of
        # step, which is the one failure the pair of arguments exists to prevent.
        plugin = self.make_plugin(files={"files/a.txt": "a"})
        other = self.make_plugin(files={"files/b.txt": "b"})
        result = self.prefix(plugin, other / "files")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not inside plugin directory", result.stderr)


class SourceFilesTests(PluginTreeMixin, unittest.TestCase):
    """The single definition of what the image ships."""

    def source_files(self, plugin, src=None):
        src = src or (plugin / "files")
        result = run_lib(
            'prefix="$(plugin_image_src_prefix "%s" "%s")"\n'
            'plugin_image_ignore_load "%s"\n'
            'cd "%s"\n'
            'plugin_image_source_files "$prefix" | LC_ALL=C sort' % (plugin, src, plugin, src)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_lists_every_file_relative_to_the_source_root(self):
        plugin = self.make_plugin(
            files={"files/plugin.yaml": "y", "files/skills/x/SKILL.md": "s"}
        )
        self.assertEqual(
            self.source_files(plugin), ["plugin.yaml", "skills/x/SKILL.md"]
        )

    def test_applies_the_exclusions(self):
        plugin = self.make_plugin(
            dockerignore="**/__pycache__\n**/*.pyc\n",
            files={
                "files/plugin.yaml": "y",
                "files/a.pyc": "junk",
                "files/skills/__pycache__/b.pyc": "junk",
            },
        )
        self.assertEqual(self.source_files(plugin), ["plugin.yaml"])

    def test_anchored_patterns_are_tested_against_the_context_relative_path(self):
        # The src_prefix case, and the reason the prefix exists: the pattern is anchored at
        # the plugin directory but the walk happens inside files/, so without the prefix
        # this pattern would be compared against `secret.txt` and quietly never match.
        plugin = self.make_plugin(
            dockerignore="files/secret.txt\n",
            files={"files/plugin.yaml": "y", "files/secret.txt": "s"},
        )
        self.assertEqual(self.source_files(plugin), ["plugin.yaml"])


class ContentTagTests(PluginTreeMixin, unittest.TestCase):
    """The tag has to move exactly when the image would differ, and not otherwise."""

    def setUp(self):
        self.plugin = self.make_plugin(
            dockerignore="**/__pycache__\n**/*.pyc\n",
            files={"files/plugin.yaml": "name: x\n", "files/skills/x/SKILL.md": "# x\n"},
        )
        self.before = self.content_tag(self.plugin)

    def test_tag_is_a_twelve_character_digest(self):
        self.assertRegex(self.before, r"^v[0-9a-f]{12}$")

    def test_unchanged_source_gives_the_same_tag(self):
        # Idempotence: re-running an installer with nothing changed republishes nothing.
        self.assertEqual(self.content_tag(self.plugin), self.before)

    def test_changed_content_moves_the_tag(self):
        self.write(self.plugin, "files/plugin.yaml", "name: y\n")
        self.assertNotEqual(self.content_tag(self.plugin), self.before)

    def test_a_new_file_moves_the_tag(self):
        self.write(self.plugin, "files/extra.yaml", "extra\n")
        self.assertNotEqual(self.content_tag(self.plugin), self.before)

    def test_a_rename_moves_the_tag(self):
        # Each file contributes its path as well as its bytes.
        (self.plugin / "files" / "plugin.yaml").rename(self.plugin / "files" / "renamed.yaml")
        self.assertNotEqual(self.content_tag(self.plugin), self.before)

    def test_an_excluded_file_does_not_move_the_tag(self):
        # Running the unit tests before an install must not mint a new image.
        self.write(self.plugin, "files/skills/x/__pycache__/x.pyc", "bytecode")
        self.assertEqual(self.content_tag(self.plugin), self.before)

    def test_a_changed_dockerfile_moves_the_tag(self):
        # How the image is assembled is part of what the image is: change the COPY path and
        # the source tree is untouched, so without this the publish would be skipped.
        (self.plugin / "Dockerfile").write_text("FROM scratch\nCOPY files/skills/ /\n")
        self.assertNotEqual(self.content_tag(self.plugin), self.before)

    def test_a_changed_dockerignore_moves_the_tag(self):
        (self.plugin / ".dockerignore").write_text("**/__pycache__\n")
        self.assertNotEqual(self.content_tag(self.plugin), self.before)

    def test_a_different_platform_moves_the_tag(self):
        # An arm64 build of an unchanged tree is a different image and must not land on the
        # tag the amd64 build holds — the kubelet declines to mount the wrong one.
        other = self.content_tag(self.plugin, env={"TARGET_PLATFORM": "linux/arm64"})
        self.assertNotEqual(other, self.before)

    def test_a_recipe_bump_moves_the_tag(self):
        result = run_lib(
            'PLUGIN_IMAGE_RECIPE=99\nplugin_image_content_tag "%s" "%s"'
            % (self.plugin, self.plugin / "files")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), self.before)

    def test_a_half_finished_walk_produces_no_tag_rather_than_a_partial_one(self):
        # `find | while` exits 0 however badly find went, so without pipefail a walk that
        # stopped on a permission error would hash the files it reached and hand back a
        # well-formed tag for a SUBSET of the tree. Stability is what makes that the worst
        # case rather than merely wrong: every later install finds the tag published and
        # skips the build, so the missing half never ships.
        #
        # The stub returns one real path and then fails, because that is the case the
        # `[ -n "$files" ]` guard cannot catch — it sees a walk that returned nothing, not
        # one that returned half.
        bin_dir = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-badfind-"))
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        broken = bin_dir / "find"
        broken.write_text(
            "#!/bin/sh\n"
            "echo ./plugin.yaml\n"
            'echo "find: ./skills: Permission denied" >&2\n'
            "exit 1\n"
        )
        broken.chmod(0o755)

        result = run_lib(
            'plugin_image_content_tag "%s" "%s"'
            % (self.plugin, self.plugin / "files"),
            env={"PATH": tool_only_path(self, bin_dir)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_an_empty_tree_has_no_tag(self):
        # `v` prefixed to nothing is still a non-empty string and the sha256 of no input is
        # a valid digest, so the caller cannot detect this by inspecting the tag.
        empty = self.make_plugin()
        (empty / "files").mkdir()
        result = run_lib(
            'plugin_image_content_tag "%s" "%s"' % (empty, empty / "files")
        )
        self.assertNotEqual(result.returncode, 0)

    def test_a_fully_excluded_tree_has_no_tag(self):
        excluded = self.make_plugin(
            dockerignore="**/*.pyc\n", files={"files/only.pyc": "junk"}
        )
        result = run_lib(
            'plugin_image_content_tag "%s" "%s"' % (excluded, excluded / "files")
        )
        self.assertNotEqual(result.returncode, 0)


class FileTypeRefusalTests(PluginTreeMixin, unittest.TestCase):
    """A symlink is in the layer and invisible to the digest, so the tree is refused.

    `find . -type f` does not see one, but `cp -R` preserves it and the crane tar writes it
    out as a symlink entry. Re-point a link and the tag stands still, publish reports the
    image already published, and the edit never ships — the `latest` failure by another
    route. Whether `docker build`'s COPY dereferences one is builder-dependent on top of
    that, so the two builders could disagree under a single tag.
    """

    def check(self, plugin, src=None, prelude="set -u"):
        src = src or (plugin / "files")
        return run_lib(
            'plugin_image_check_file_types "%s" "%s"' % (plugin, src), prelude=prelude
        )

    def kept_going(self, plugin, src=None):
        """Run the check with no `set -e`, and let the next line announce itself.

        The check is invoked bare at both call sites, so it has to end the run on its own
        rather than by handing a non-zero status to a caller that happens to be under
        `set -e`. run_lib's default prelude is `set -u` and deliberately not `-e`, which is
        the caller that wraps plugin_image_resolve in an `if` — `set -e` is suspended for
        the whole of such a call — and the installers' own `set -euo pipefail` is covered
        separately below.
        """
        src = src or (plugin / "files")
        return run_lib(
            'plugin_image_check_file_types "%s" "%s"\necho REACHED' % (plugin, src)
        )

    def test_a_tree_of_files_and_directories_is_accepted(self):
        plugin = self.make_plugin(
            files={"files/plugin.yaml": "y", "files/skills/x/SKILL.md": "# x\n"}
        )
        result = self.check(plugin)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_symlink_in_the_source_tree_is_refused(self):
        plugin = self.make_plugin(files={"files/real.txt": "real\n"})
        (plugin / "files" / "link.txt").symlink_to("real.txt")
        result = self.check(plugin)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only regular files and directories", result.stderr)
        self.assertIn("link.txt", result.stderr)

    def test_a_symlink_to_a_directory_is_refused(self):
        plugin = self.make_plugin(files={"files/skills/x/SKILL.md": "# x\n"})
        (plugin / "files" / "alias").symlink_to("skills")
        result = self.check(plugin)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alias", result.stderr)

    def test_a_nested_symlink_is_refused(self):
        plugin = self.make_plugin(files={"files/skills/x/SKILL.md": "# x\n"})
        (plugin / "files" / "skills" / "x" / "README.md").symlink_to("SKILL.md")
        result = self.check(plugin)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("skills/x/README.md", result.stderr)

    def test_an_excluded_symlink_is_tolerated(self):
        # Something .dockerignore drops reaches neither builder, so it is nobody's problem.
        plugin = self.make_plugin(
            dockerignore="**/__pycache__\n", files={"files/real.txt": "real\n"}
        )
        (plugin / "files" / "__pycache__").mkdir()
        (plugin / "files" / "__pycache__" / "link.txt").symlink_to("../real.txt")
        result = self.check(plugin)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_symlink_would_otherwise_be_invisible_to_the_tag(self):
        # The refusal above is what stands in for this: re-pointing a link changes the
        # image and does not change the digest, so the tag cannot be trusted to notice.
        plugin = self.make_plugin(files={"files/a.txt": "a\n", "files/b.txt": "b\n"})
        link = plugin / "files" / "link.txt"
        link.symlink_to("a.txt")
        before = self.content_tag(plugin)
        link.unlink()
        link.symlink_to("b.txt")
        self.assertEqual(self.content_tag(plugin), before)
        self.assertNotEqual(self.check(plugin).returncode, 0)

    def test_a_refusal_ends_the_run_on_its_own(self):
        # Not "returns non-zero": both call sites invoke the check bare, so a status nobody
        # reads is a refusal that does not refuse.
        plugin = self.make_plugin(files={"files/real.txt": "real\n"})
        (plugin / "files" / "link.txt").symlink_to("real.txt")
        result = self.kept_going(plugin)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("REACHED", result.stdout)

    def test_a_source_outside_the_context_ends_the_run_too(self):
        # The path that used to `return 1`: plugin_image_src_prefix reports this by exiting
        # a command substitution, which is only a `return` as far as this function's caller
        # is concerned. It has to become an exit here, and it has to keep the message
        # src_prefix already printed rather than replace it with a vaguer one.
        plugin = self.make_plugin(files={"files/a.txt": "a"})
        other = self.make_plugin(files={"files/b.txt": "b"})
        result = self.kept_going(plugin, other / "files")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not inside plugin directory", result.stderr)
        self.assertNotIn("REACHED", result.stdout)

    def test_a_missing_source_directory_says_so_rather_than_exiting_silently(self):
        # Reachable from publish, which never validated the pair the way resolve does. An
        # exit with an empty stderr is the one failure nobody can act on.
        plugin = self.make_plugin(files={"files/a.txt": "a"})
        result = self.kept_going(plugin, plugin / "not-there")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertNotIn("REACHED", result.stdout)

    def test_a_walk_that_failed_is_not_a_clean_tree(self):
        # The refusal reads the status of a `find | while` pipeline, and a while loop exits
        # 0 however badly find went — so without pipefail inside the subshell, a walk that
        # stopped partway hands back the offenders it happened to reach. For a tree whose
        # symlink was in the part it never got to that is an empty list, and the check
        # passes for the one reason that proves it did not run. `find` is shadowed by one
        # that fails the way a permission error does: something on stderr, nothing on
        # stdout, non-zero. The prelude is the default `set -u`, deliberately: the
        # installers set pipefail themselves, and the caller that did not is the one at
        # risk here.
        plugin = self.make_plugin(files={"files/real.txt": "real\n"})
        (plugin / "files" / "link.txt").symlink_to("real.txt")
        bin_dir = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-badfind-"))
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        broken = bin_dir / "find"
        broken.write_text('#!/bin/sh\necho "find: ./x: Permission denied" >&2\nexit 1\n')
        broken.chmod(0o755)

        result = run_lib(
            'plugin_image_check_file_types "%s" "%s"\necho REACHED'
            % (plugin, plugin / "files"),
            env={"PATH": tool_only_path(self, bin_dir)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not walk", result.stderr)
        self.assertNotIn("REACHED", result.stdout)

    def test_a_clean_tree_passes_under_the_installers_own_shell_options(self):
        # `set -euo pipefail` is what both install.sh scripts run under, and the check is a
        # `find | while` pipeline whose status is what the new `||` reads — so pipefail is
        # precisely the option that could turn an ordinary tree into a refusal.
        plugin = self.make_plugin(
            files={"files/plugin.yaml": "y", "files/skills/x/SKILL.md": "# x\n"}
        )
        result = self.check(plugin, prelude="set -euo pipefail")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_resolve_refuses_before_it_names_an_image(self):
        # The check has to land before anything is provisioned, and before the builder and
        # the registry are touched — this asserts it fails without reaching either.
        #
        # On a stripped PATH, and that is not tidiness. Resolve carries on past this point
        # to plugin_image_ensure_repository, which runs `gcloud artifacts repositories
        # describe` and then `gcloud services enable` against the project it was handed —
        # so were the refusal ever to regress, this test would reach for a real Artifact
        # Registry in a project called demo-project with whatever credentials the machine
        # running the suite happens to hold. A test that fails by making an API call is
        # slower and less legible than one that fails on a missing builder.
        plugin = self.make_plugin(files={"files/real.txt": "real\n"})
        (plugin / "files" / "link.txt").symlink_to("real.txt")
        result = run_lib(
            'plugin_image_resolve demo demo-project "%s" "%s"' % (plugin, plugin / "files"),
            env={"PATH": tool_only_path(self)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only regular files and directories", result.stderr)
        self.assertNotIn("Image:", result.stdout)


class StageTests(PluginTreeMixin, unittest.TestCase):
    """Both builders stage, so both ship the same modes and the same files."""

    def stage(self, plugin, src, dest, prelude="set -euo pipefail"):
        return run_lib(
            'plugin_image_stage "%s" "%s" "%s"' % (plugin, src, dest), prelude=prelude
        )

    def test_staged_files_are_world_readable(self):
        # git creates files as 0666 masked by the umask, so a checkout made under
        # `umask 077` yields 0600 throughout and an image built from it loads nothing.
        plugin = self.make_plugin(files={"files/plugin.yaml": "y", "files/skills/x.md": "x"})
        os.chmod(plugin / "files" / "plugin.yaml", 0o600)
        os.chmod(plugin / "files" / "skills", 0o700)
        dest = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-stage-")) / "root"
        self.addCleanup(shutil.rmtree, dest.parent, ignore_errors=True)

        result = self.stage(plugin, plugin / "files", dest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((dest / "plugin.yaml").stat().st_mode & stat.S_IROTH)
        self.assertTrue((dest / "skills").stat().st_mode & stat.S_IROTH)
        self.assertTrue((dest / "skills").stat().st_mode & stat.S_IXOTH)
        self.assertTrue((dest / "skills" / "x.md").stat().st_mode & stat.S_IROTH)

    def test_excluded_paths_do_not_reach_the_staging_tree(self):
        plugin = self.make_plugin(
            dockerignore="**/__pycache__\n**/*.pyc\n",
            files={
                "files/plugin.yaml": "y",
                "files/a.pyc": "junk",
                "files/skills/__pycache__/b.pyc": "junk",
                "files/skills/x.md": "x",
            },
        )
        dest = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-stage-")) / "root"
        self.addCleanup(shutil.rmtree, dest.parent, ignore_errors=True)

        result = self.stage(plugin, plugin / "files", dest)
        self.assertEqual(result.returncode, 0, result.stderr)
        staged = sorted(
            str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()
        )
        self.assertEqual(staged, ["plugin.yaml", "skills/x.md"])
        self.assertFalse((dest / "skills" / "__pycache__").exists())

    def test_excluding_nothing_still_succeeds(self):
        # The `if` rather than `cond && printf` case: an AND-list whose first half is false
        # leaves the loop, and so the whole pipeline, non-zero — which under the caller's
        # pipefail reads as a staging failure on the entirely normal case of no exclusions.
        plugin = self.make_plugin(
            dockerignore="**/__pycache__\n", files={"files/plugin.yaml": "y"}
        )
        dest = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-stage-")) / "root"
        self.addCleanup(shutil.rmtree, dest.parent, ignore_errors=True)

        result = self.stage(plugin, plugin / "files", dest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((dest / "plugin.yaml").exists())

    def test_a_walk_that_failed_does_not_pass_for_a_tree_with_nothing_excluded(self):
        # The exclusions are applied by walking the staged copy, and the status of a
        # `find | while` is the while loop's — 0 however badly find went. A walk that failed
        # would leave every excluded path in place and still report success, so the layer
        # would ship a file the content tag was computed as though absent: the two builders
        # disagreeing under one tag, by a route the shared reader does not cover.
        #
        # Staging must FAIL here rather than refuse-and-exit: plugin_image_publish's failure
        # handler is what removes the staging tree and the crane token, and it only runs for
        # a non-zero return.
        plugin = self.make_plugin(
            dockerignore="**/*.pyc\n",
            files={"files/plugin.yaml": "y", "files/junk.pyc": "j"},
        )
        bin_dir = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-badfind-"))
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        broken = bin_dir / "find"
        broken.write_text('#!/bin/sh\necho "find: Permission denied" >&2\nexit 1\n')
        broken.chmod(0o755)
        dest = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-stage-")) / "root"
        self.addCleanup(shutil.rmtree, dest.parent, ignore_errors=True)

        result = run_lib(
            'plugin_image_stage "%s" "%s" "%s"' % (plugin, plugin / "files", dest),
            env={"PATH": tool_only_path(self, bin_dir)},
        )
        self.assertNotEqual(
            result.returncode, 0, "staging reported success on a walk that never ran"
        )

    def test_the_docker_context_stages_the_whole_plugin_directory(self):
        # What plugin_image_docker_publish does: src is the plugin dir itself, so every
        # COPY path in the Dockerfile still resolves against the staged copy.
        plugin = self.make_plugin(
            dockerignore="**/*.pyc\n", files={"files/plugin.yaml": "y", "junk.pyc": "j"}
        )
        dest = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-stage-")) / "context"
        self.addCleanup(shutil.rmtree, dest.parent, ignore_errors=True)

        result = self.stage(plugin, plugin, dest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((dest / "Dockerfile").exists())
        self.assertTrue((dest / "files" / "plugin.yaml").exists())
        self.assertFalse((dest / "junk.pyc").exists())


class DiscoverRegistryTests(unittest.TestCase):
    """Where the image is published is copied from the agent's own image, not guessed.

    Nothing in this repository grants `artifactregistry.reader`, so a repository of our own
    invention may be one the kubelet cannot pull from. `kubectl` is stubbed: these assert
    the parsing, not the cluster.
    """

    def setUp(self):
        self.bin_dir = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-bin-"))
        self.addCleanup(shutil.rmtree, self.bin_dir, ignore_errors=True)
        self.calls = self.bin_dir / "kubectl.calls"
        stub = self.bin_dir / "kubectl"
        stub.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$KUBECTL_CALLS"\n'
            '[ -n "${AGENT_IMAGE:-}" ] || exit 1\n'
            'printf "%s" "$AGENT_IMAGE"\n'
        )
        stub.chmod(0o755)

    def discover(self, agent_image=None, env=None):
        full = {
            "PATH": "%s:%s" % (self.bin_dir, BASE_ENV["PATH"]),
            "KUBECTL_CALLS": str(self.calls),
        }
        if agent_image is not None:
            full["AGENT_IMAGE"] = agent_image
        full.update(env or {})
        result = run_lib(
            "plugin_image_discover_registry ctx ns agent\n"
            'printf "%s|%s|%s" "$PLUGIN_AR_LOCATION" "$PLUGIN_AR_PROJECT" "$PLUGIN_AR_REPOSITORY"',
            env=full,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split("|")

    def test_artifact_registry_reference_gives_all_three_parts(self):
        self.assertEqual(
            self.discover("us-central1-docker.pkg.dev/reg-proj/kube-agents/platform-agent:v1"),
            ["us-central1", "reg-proj", "kube-agents"],
        )

    def test_the_project_is_copied_along_with_the_rest(self):
        # A fleet whose agents run from a shared registry project would otherwise get the
        # location and repository from the agent and the project from the install — a
        # reference that names a repository which most likely does not exist.
        self.assertEqual(
            self.discover("europe-west4-docker.pkg.dev/shared-registry/plugins/agent:v2"),
            ["europe-west4", "shared-registry", "plugins"],
        )

    def test_a_nested_image_name_still_yields_the_repository(self):
        self.assertEqual(
            self.discover("us-central1-docker.pkg.dev/p/repo/team/agent:v1"),
            ["us-central1", "p", "repo"],
        )

    def test_a_reference_with_no_repository_segment_yields_none(self):
        # host/project/name has nothing to copy: taking `name:tag` as the repository would
        # be an invention, and the fallbacks are the honest answer.
        self.assertEqual(
            self.discover("us-central1-docker.pkg.dev/p/agent:v1"),
            ["us-central1", "p", ""],
        )

    def test_a_non_artifact_registry_agent_discovers_nothing(self):
        # The chart's default: an agent pulled from ghcr.io says nothing about where a
        # plugin image should go.
        self.assertEqual(
            self.discover("ghcr.io/gke-labs/kube-agents/platform-agent:v1"), ["", "", ""]
        )

    def test_no_agent_deployed_discovers_nothing(self):
        self.assertEqual(self.discover(None), ["", "", ""])

    def test_a_pin_is_not_overwritten_by_discovery(self):
        self.assertEqual(
            self.discover(
                "us-central1-docker.pkg.dev/reg-proj/kube-agents/agent:v1",
                env={"AR_REPOSITORY": "my-own-repo"},
            ),
            ["us-central1", "reg-proj", "my-own-repo"],
        )

    def test_all_three_pinned_skips_the_lookup_entirely(self):
        self.assertEqual(
            self.discover(
                "us-central1-docker.pkg.dev/reg-proj/kube-agents/agent:v1",
                env={"AR_LOCATION": "l", "AR_PROJECT": "p", "AR_REPOSITORY": "r"},
            ),
            ["l", "p", "r"],
        )
        self.assertFalse(self.calls.exists(), "kubectl was called despite every pin being set")


class BuilderTests(unittest.TestCase):
    """Choosing a builder, and refusing to pretend one is available."""

    def builder(self, env):
        full = dict(env)
        # No docker and no crane on PATH, so the resolution cannot quietly depend on what
        # the machine running the suite has installed.
        if "PATH" not in full:
            full["PATH"] = tool_only_path(self)
        return run_lib(
            'plugin_image_builder\nprintf "%s" "$PLUGIN_IMAGE_BUILDER_RESOLVED"', env=full
        )

    def test_an_unknown_builder_is_refused(self):
        result = self.builder({"IMAGE_BUILDER": "podman"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be auto, docker or crane", result.stderr)

    def test_crane_requested_but_absent_names_the_install_command(self):
        result = self.builder({"IMAGE_BUILDER": "crane", "CRANE_BIN": "definitely-not-crane"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("definitely-not-crane", result.stderr)
        self.assertIn("go install", result.stderr)

    def test_docker_requested_but_absent_suggests_the_fallback(self):
        result = self.builder({"IMAGE_BUILDER": "docker"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("crane", result.stderr)

    def test_auto_with_nothing_installed_explains_both_ways_out(self):
        result = self.builder({"IMAGE_BUILDER": "auto"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no image builder is available", result.stderr)
        self.assertIn("PLUGIN_IMAGE", result.stderr)

    def test_crane_is_chosen_when_only_crane_is_present(self):
        bin_dir = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-bin-"))
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        fake = bin_dir / "crane"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        result = self.builder({"PATH": tool_only_path(self, bin_dir)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "crane")


class PublishGuardTests(PluginTreeMixin, unittest.TestCase):
    """The publish path's guards, up to the point where it would touch a registry."""

    def crane_stub(self):
        """A crane that records every invocation and does nothing else.

        Enough for plugin_image_builder to settle on crane, and it makes "the refusal
        happened before anything was pushed" an assertion rather than an inference.
        """
        bin_dir = pathlib.Path(tempfile.mkdtemp(prefix="plugin-image-crane-"))
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        self.crane_calls = bin_dir / "crane.calls"
        stub = bin_dir / "crane"
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CRANE_CALLS"\nexit 0\n')
        stub.chmod(0o755)
        return {
            "PATH": tool_only_path(self),
            "IMAGE_BUILDER": "crane",
            "CRANE_BIN": str(stub),
            "CRANE_CALLS": str(self.crane_calls),
        }

    def test_publishing_before_resolving_is_refused(self):
        plugin = self.make_plugin(files={"files/a.txt": "a"})
        result = run_lib(
            'plugin_image_publish "%s" "%s"' % (plugin, plugin / "files")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("called before plugin_image_resolve", result.stderr)

    def test_a_prebuilt_image_skips_the_build(self):
        plugin = self.make_plugin(files={"files/a.txt": "a"})
        result = run_lib(
            'plugin_image_resolve demo demo-project "%s" "%s"\n'
            'plugin_image_publish "%s" "%s"'
            % (plugin, plugin / "files", plugin, plugin / "files"),
            env={"PLUGIN_IMAGE": "example.com/pre/built:v1", "PATH": tool_only_path(self)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prebuilt", result.stdout)
        self.assertIn("skipping the build", result.stdout)

    def publish_unresolved(self, plugin, env):
        # A caller that set PLUGIN_IMAGE_REF itself and never went through resolve — the
        # case publish repeats these checks for.
        return run_lib(
            'PLUGIN_IMAGE_REF=example.com/x/y:v1\nplugin_image_publish "%s" "%s"'
            % (plugin, plugin / "files"),
            env=env,
        )

    def test_a_bad_dockerignore_is_refused_before_crane_is_run(self):
        # Both of these refuse from inside plugin_image_publish, and both have to do it
        # before plugin_image_login: `exit` is not `return`, so a refusal any later skips
        # the failure handler and leaves crane's access token in ~/.docker/config.json for
        # its full hour, and the staging tree on disk with it.
        plugin = self.make_plugin(
            dockerignore="**/*.pyc\n!keep.pyc\n", files={"files/a.txt": "a"}
        )
        result = self.publish_unresolved(plugin, self.crane_stub())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("negation", result.stderr)
        self.assertFalse(self.crane_calls.exists(), "crane ran before the refusal")

    def test_a_symlink_is_refused_before_crane_is_run(self):
        plugin = self.make_plugin(files={"files/a.txt": "a"})
        (plugin / "files" / "link.txt").symlink_to("a.txt")
        result = self.publish_unresolved(plugin, self.crane_stub())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only regular files and directories", result.stderr)
        self.assertFalse(self.crane_calls.exists(), "crane ran before the refusal")

    def test_an_accepted_tree_reaches_the_registry(self):
        # The control for the two above: same call, nothing wrong with the tree, and crane
        # is reached. Without this they would still pass if publish refused everything.
        plugin = self.make_plugin(
            dockerignore="**/*.pyc\n", files={"files/a.txt": "a"}
        )
        result = self.publish_unresolved(plugin, self.crane_stub())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.crane_calls.exists())


class RecipeCounterTests(unittest.TestCase):
    """PLUGIN_IMAGE_RECIPE is bumped by hand, and forgetting to is invisible.

    A published tag is never rebuilt, so a change to HOW an image is produced that does not
    move the counter can never reach an image already published. Nothing can detect a build
    change on its own, but the counter and the list of what each value means have to agree —
    which catches the half of it where one was updated and the other was not.
    """

    def setUp(self):
        self.text = LIB.read_text()

    def test_the_counter_matches_the_documented_history(self):
        entries = [int(n) for n in re.findall(r"^#   (\d+)  \S", self.text, re.MULTILINE)]
        self.assertTrue(entries, "the PLUGIN_IMAGE_RECIPE history list was not found")
        self.assertEqual(
            entries,
            list(range(1, len(entries) + 1)),
            "the recipe history is not a contiguous list starting at 1",
        )
        declared = re.search(r"^PLUGIN_IMAGE_RECIPE=(\d+)$", self.text, re.MULTILINE)
        self.assertIsNotNone(declared, "PLUGIN_IMAGE_RECIPE is not declared")
        self.assertEqual(
            int(declared.group(1)),
            entries[-1],
            "PLUGIN_IMAGE_RECIPE and its documented history disagree: bump both together",
        )

    def test_the_library_sources_cleanly_and_does_nothing(self):
        result = run_lib("printf ok", prelude="set -euo pipefail")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok")


if __name__ == "__main__":
    unittest.main()

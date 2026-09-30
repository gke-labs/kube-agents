"""Tests for the build-time check that shipped skill commands pass Tirith.

Run: python3 -m unittest discover -s deploy/docker -p 'test_*.py'

The check runs inside `docker build` against a real Tirith binary. These tests
cover what it feeds that binary and what it does with the verdicts, through a
fake scanner, so a wrong extraction shows up here rather than as a command the
build never looked at.
"""

import hashlib
import http.client
import importlib.util
import io
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

CHECK_PY = Path(__file__).resolve().parent / "check_skill_commands.py"
REPO_ROOT = CHECK_PY.parents[2]

_spec = importlib.util.spec_from_file_location("check_skill_commands", CHECK_PY)
check = importlib.util.module_from_spec(_spec)
# dataclasses resolves the module's annotations through sys.modules.
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)


def verdict(action, *rules, summary=""):
    return {"action": action, "findings": [{"rule_id": r} for r in rules], "summary": summary}


class CodeBlocksTest(unittest.TestCase):
    def test_language_and_first_body_line(self):
        text = "intro\n\n```bash\nls\npwd\n```\n"
        self.assertEqual([(4, "bash", ["ls", "pwd"])], list(check.code_blocks(text)))

    def test_language_is_lowercased_and_may_be_absent(self):
        text = "```Shell\nls\n```\n```\nnot a command\n```\n"
        self.assertEqual(["shell", ""], [lang for _, lang, _ in check.code_blocks(text)])

    def test_the_fence_indent_is_removed_from_the_body(self):
        text = "1. Step\n\n   ```bash\n   foo \\\n     --bar\n   ```\n"
        self.assertEqual([["foo \\", "  --bar"]], [body for _, _, body in check.code_blocks(text)])

    def test_tilde_fences_close_only_on_tildes(self):
        text = "~~~sh\necho '```'\n```\nls\n~~~\n"
        self.assertEqual([["echo '```'", "```", "ls"]], [b for _, _, b in check.code_blocks(text)])

    def test_an_info_string_keeps_the_language_and_the_boundaries(self):
        text = "```bash title=x\n$G add f\n```\n(prose)\n```sh {1,3}\n$G commit\n```\n"
        self.assertEqual(
            [(2, "bash", ["$G add f"]), (6, "sh", ["$G commit"])], list(check.code_blocks(text))
        )

    def test_a_longer_fence_closes_only_on_a_run_as_long(self):
        text = "````bash\ncat <<'EOF'\n```bash\ninner\n```\nEOF\nS submit\n`````\n"
        body = ["cat <<'EOF'", "```bash", "inner", "```", "EOF", "S submit"]
        self.assertEqual([(2, "bash", body)], list(check.code_blocks(text)))

    def test_backticks_on_one_line_are_inline_code(self):
        self.assertEqual([(3, "bash", ["ls"])], list(check.code_blocks("```x```\n```bash\nls\n```\n")))

    def test_punctuation_after_the_language_is_not_part_of_it(self):
        text = "```bash:run.sh\nls\n```\n```bash{1,3}\npwd\n```\n"
        self.assertEqual(["bash", "bash"], [lang for _, lang, _ in check.code_blocks(text)])

    def test_an_unclosed_fence_runs_to_the_end_unless_it_is_shell(self):
        self.assertEqual([["ls", "pwd"]], [b for _, _, b in check.code_blocks("```\nls\npwd")])
        for text in ("```sh\nls\npwd\n", "```sh\nls\npwd"):
            with self.assertRaises(check.UnreadableFence) as caught:
                list(check.code_blocks(text))
            self.assertEqual(1, caught.exception.line)

    def test_a_heredoc_body_at_the_margin_of_a_list_item_is_refused(self):
        text = (
            "1. Apply\n\n   ```bash\n   cat > cm.yaml <<EOF\napiVersion: v1\nEOF\n"
            "   kubectl apply -f cm.yaml\n   ```\n2. Commit\n\n   ```bash\n   $G add cm.yaml\n   ```\n"
        )
        with self.assertRaises(check.UnreadableFence) as caught:
            list(check.code_blocks(text))
        self.assertEqual(3, caught.exception.line)

    def test_a_shell_opener_markdown_parses_otherwise_is_refused(self):
        cases = {
            "indented code": ("text\n\n    ```bash\n    ls\n    ```\n", 3),
            "html block": ("<details>\n```bash\nls\n```\n</details>\n", 2),
            "a same-length fence's body": ("```markdown\n```bash\nls\n```\n", 2),
            "an unclosed tilde fence's body": ("~~~yaml\na: b\n    ~~~\n\n```bash\nls\n```\n", 5),
            "an unclosed longer fence's body": ("````yaml\na: b\n\n```bash\nls\n```\n", 4),
        }
        for name, (text, line) in cases.items():
            with self.subTest(name):
                with self.assertRaises(check.UnreadableFence) as caught:
                    list(check.code_blocks(text))
                self.assertEqual(line, caught.exception.line)

    def test_a_fence_in_a_block_quote_loses_its_markers(self):
        text = "> ```bash\n> $G add f\n> ```\n\n```sh\nls\n```\n"
        self.assertEqual(
            [(2, "bash", ["$G add f"]), (6, "sh", ["ls"])], list(check.code_blocks(text))
        )

    def test_a_fence_on_a_list_marker_line_is_read(self):
        text = "- ```bash\n  $G add f\n  ```\n\n```sh\nls\n```\n"
        self.assertEqual(
            [(2, "bash", ["$G add f"]), (6, "sh", ["ls"])], list(check.code_blocks(text))
        )


class SubstitutePlaceholdersTest(unittest.TestCase):
    def test_a_placeholder_becomes_its_name(self):
        self.assertEqual("gh --repo owner/repo", check.substitute_placeholders("gh --repo <owner>/<repo>"))

    def test_characters_a_shell_would_split_on_are_filled(self):
        self.assertEqual(
            "--comment-id ref_from_the_poll",
            check.substitute_placeholders("--comment-id <ref from the poll>"),
        )

    def test_heredocs_and_redirections_are_left_alone(self):
        for text in ("cat <<EOF", "cat <<-EOF", "sort < in > out", "cmd 2>&1"):
            self.assertEqual(text, check.substitute_placeholders(text))


class SkillBlocksTest(unittest.TestCase):
    def test_only_shell_blocks_and_findings_name_the_repository_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "demo" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text(
                "```bash\nls\n```\n```yaml\nkey: value\n```\n```\ntool_call()\n```\n```zsh\npwd\n```\n"
            )
            (Path(tmp) / "demo" / "README.md").write_text("```bash\nrm -rf /\n```\n")
            got = check.skill_blocks("agents/platform/skills", Path(tmp))
        self.assertEqual(
            [
                check.Block("agents/platform/skills/demo/SKILL.md", 2, "ls"),
                check.Block("agents/platform/skills/demo/SKILL.md", 11, "pwd"),
            ],
            got,
        )

    def test_a_block_is_scanned_whole(self):
        body = "# apply it\nfoo \\\n  --bar\ncat > cm.yaml <<'END-OF-FILE'\ndescription: don't\nEND-OF-FILE\n$G add cm.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "demo" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text(f"```bash\n{body}\n```\n")
            got = check.skill_blocks("agents/platform/skills", Path(tmp))
        self.assertEqual([check.Block("agents/platform/skills/demo/SKILL.md", 2, body)], got)

    def test_an_unreadable_fence_names_the_repository_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "demo" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("intro\n```bash\nls\n")
            with self.assertRaises(check.UnreadableFence) as caught:
                check.skill_blocks("agents/platform/skills", Path(tmp))
        self.assertEqual("agents/platform/skills/demo/SKILL.md", caught.exception.path)
        self.assertEqual(2, caught.exception.line)

    def test_a_tree_with_no_shell_block_fails_before_the_scan(self):
        with tempfile.TemporaryDirectory() as full, tempfile.TemporaryDirectory() as empty:
            skill = Path(full) / "demo" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("```bash\nls\n```\n")
            (Path(empty) / "demo").mkdir()
            (Path(empty) / "demo" / "SKILL.md").write_text("```yaml\nkey: value\n```\n")
            trees = [f"agents/platform/skills={full}", f"agents/cluster/skills={empty}"]
            with mock.patch.object(check, "install_tirith") as install, \
                    mock.patch("sys.stderr", new=io.StringIO()) as err:
                self.assertEqual(1, check.main(trees))
        install.assert_not_called()
        self.assertIn(f"{empty} holds no shell block", err.getvalue())
        self.assertIn("agents/cluster/skills would pass unread", err.getvalue())
        self.assertNotIn(full, err.getvalue())

    def test_two_trees_on_overlapping_paths_fail_before_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "demo" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("```bash\nls\n```\n")
            for second in (tmp, str(Path(tmp) / "demo")):
                trees = [f"agents/cluster/skills={tmp}", f"a2a/persona/platform/skills={second}"]
                with self.subTest(second), \
                        mock.patch.object(check, "install_tirith") as install, \
                        mock.patch("sys.stderr", new=io.StringIO()) as err:
                    self.assertEqual(1, check.main(trees))
                    install.assert_not_called()
                    self.assertIn("overlap", err.getvalue())

    def test_trees_that_all_hold_a_shell_block_go_on_to_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "demo" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("```bash\nls\n```\n")
            with mock.patch.object(
                check, "tirith_target", return_value="x86_64-unknown-linux-gnu"
            ), mock.patch.object(
                check, "install_tirith", side_effect=check.ScannerUnavailable("stop here")
            ) as install, mock.patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(1, check.main([f"agents/platform/skills={tmp}"]))
        install.assert_called_once()


class ScanTest(unittest.TestCase):
    ALLOW = check.Block("s/SKILL.md", 1, "ls <dir>\ncd <dir>")
    WARN = check.Block("s/SKILL.md", 4, "export KUBECONFIG=x")
    BLOCK = check.Block("s/SKILL.md", 7, "$G add f")

    def test_block_and_warn_are_findings_and_allow_is_not(self):
        verdicts = {"ls dir\ncd dir": verdict("allow"), "export KUBECONFIG=x": verdict("warn", "sensitive_env_export"),
                    "$G add f": verdict("block", "analysis_incomplete", "analysis_incomplete")}
        findings = check.scan_blocks([self.ALLOW, self.WARN, self.BLOCK], verdicts.__getitem__)
        self.assertEqual(
            [
                check.Finding(self.WARN, "warn", ("sensitive_env_export",)),
                check.Finding(self.BLOCK, "block", ("analysis_incomplete",)),
            ],
            findings,
        )

    def test_a_finding_carries_each_title_once(self):
        budget = "Command analysis exceeded its work budget"
        cited = {"action": "block", "summary": "", "findings": [
            {"rule_id": "analysis_incomplete", "title": budget},
            {"rule_id": "analysis_incomplete", "title": budget},
            {"rule_id": "analysis_incomplete"},
        ]}
        [finding] = check.scan_blocks([self.BLOCK], lambda text: cited)
        self.assertEqual(("analysis_incomplete",), finding.rules)
        self.assertEqual((budget,), finding.titles)

    def test_the_scanner_sees_placeholders_filled(self):
        seen = []
        check.scan_blocks([self.ALLOW], lambda text: seen.append(text) or verdict("allow"))
        self.assertEqual(["ls dir\ncd dir"], seen)

    def test_a_fail_closed_verdict_stops_the_scan(self):
        failed = verdict("block", summary="tirith spawn failed (fail-closed)")
        with self.assertRaises(check.ScannerUnavailable):
            check.scan_blocks([self.ALLOW], lambda text: failed)

    def test_the_probes_need_a_block_and_an_allow(self):
        working = {check.PROBE_REFUSED: verdict("block"), check.PROBE_ALLOWED: verdict("allow")}
        check.check_scanner(working.__getitem__)
        for broken in (lambda text: verdict("allow"), lambda text: verdict("block")):
            with self.assertRaises(check.ScannerUnavailable):
                check.check_scanner(broken)


class TriageTest(unittest.TestCase):
    def test_known_findings_pass_and_unmatched_entries_are_stale(self):
        known_block = check.Block("a/SKILL.md", 1, "known")
        new_block = check.Block("a/SKILL.md", 4, "known\nnew")
        findings = [check.Finding(known_block, "warn", ("r",)), check.Finding(new_block, "block", ("r",))]
        known = {known_block.key: (frozenset({"r"}), "reason"), ("a/SKILL.md", "gone"): (frozenset({"r"}), "reason")}
        new, stale = check.triage(findings, known, ["a"])
        self.assertEqual([findings[1]], new)
        self.assertEqual([("a/SKILL.md", "gone")], stale)

    def test_a_known_block_citing_another_rule_is_new(self):
        block = check.Block("a/SKILL.md", 1, "known")
        finding = check.Finding(block, "block", ("r", "added"))
        new, stale = check.triage([finding], {block.key: (frozenset({"r"}), "reason")}, ["a"])
        self.assertEqual([finding], new)
        self.assertEqual([], stale)

    def test_only_an_entry_under_a_tree_the_run_read_is_stale(self):
        known = {
            (path, "gone"): (frozenset({"r"}), "reason")
            for path in ("a/SKILL.md", "ab/SKILL.md", "b/x/SKILL.md")
        }
        new, stale = check.triage([], known, ["a", "c"])
        self.assertEqual([], new)
        self.assertEqual([("a/SKILL.md", "gone")], stale)

    def run_main(self, known, skill_verdict):
        """Run ``main`` over one skill block the fake scanner rates ``skill_verdict``."""
        probes = {check.PROBE_REFUSED: verdict("block"), check.PROBE_ALLOWED: verdict("allow")}
        scanner = types.ModuleType("tools.tirith_security")
        scanner.check_command_security = lambda text: probes.get(text, skill_verdict)
        modules = {"tools": types.ModuleType("tools"), "tools.tirith_security": scanner}
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "demo" / "SKILL.md"
            skill.parent.mkdir()
            # Not the allowed probe's command, which ``probes`` would answer.
            skill.write_text("```bash\npwd\n```\n")
            with mock.patch.dict(sys.modules, modules), \
                    mock.patch.dict(check.os.environ), \
                    mock.patch.object(check, "KNOWN_FINDINGS", known), \
                    mock.patch("sys.stdout", new=io.StringIO()) as out, \
                    mock.patch("sys.stderr", new=io.StringIO()) as err:
                code = check.main([f"agents/platform/skills={tmp}", "--tirith-bin", "tirith"])
        return code, out.getvalue(), err.getvalue()

    def test_main_judges_staleness_against_the_trees_it_was_given(self):
        known = {
            (f"{tree}/demo/SKILL.md", "gone"): (frozenset({"r"}), "reason")
            for tree in ("agents/platform/skills", "agents/cluster/skills")
        }
        code, _, err = self.run_main(known, verdict("allow"))
        self.assertEqual(1, code)
        self.assertIn("('agents/platform/skills/demo/SKILL.md', 'gone')", err)
        self.assertNotIn("agents/cluster/skills", err)

    def test_main_fails_on_a_refused_block_and_reports_it(self):
        code, out, err = self.run_main({}, verdict("block", "r"))
        self.assertEqual(1, code)
        self.assertIn("agents/platform/skills/demo/SKILL.md:2: Tirith rates this block block [r]", err)
        self.assertEqual("", out)

    def test_main_passes_when_every_block_is_allowed(self):
        code, out, err = self.run_main({}, verdict("allow"))
        self.assertEqual(0, code)
        self.assertIn("1 skill shell blocks pass Tirith", out)
        self.assertEqual("", err)


class KnownFindingsTest(unittest.TestCase):
    def test_every_entry_names_a_block_the_repository_ships(self):
        for path, text in check.KNOWN_FINDINGS:
            skill = REPO_ROOT / path
            tree = skill.parents[1]
            repo_dir = tree.relative_to(REPO_ROOT).as_posix()
            shipped = {b.text for b in check.skill_blocks(repo_dir, tree) if b.path == path}
            self.assertIn(text, shipped, path)

    def test_every_entry_has_rules_and_a_reason(self):
        for key, (rules, reason) in check.KNOWN_FINDINGS.items():
            self.assertTrue(rules, key)
            self.assertTrue(reason.strip(), key)


class TirithPinTest(unittest.TestCase):
    def test_the_version_is_pinned(self):
        self.assertRegex(check.TIRITH_VERSION, r"^v\d+\.\d+\.\d+$")
        self.assertNotIn("latest", check.TIRITH_ARCHIVE_URL)

    def test_every_target_has_a_digest(self):
        self.assertEqual(set(check.TIRITH_TARGETS.values()), set(check.TIRITH_ARCHIVE_SHA256))
        for digest in check.TIRITH_ARCHIVE_SHA256.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_only_linux_has_a_target(self):
        self.assertEqual("x86_64-unknown-linux-gnu", check.tirith_target("Linux", "x86_64"))
        self.assertEqual("aarch64-unknown-linux-gnu", check.tirith_target("Linux", "arm64"))
        with self.assertRaises(check.ScannerUnavailable):
            check.tirith_target("Darwin", "arm64")


def _archive(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class InstallTirithTest(unittest.TestCase):
    TARGET = "x86_64-unknown-linux-gnu"

    def install(self, archive, digest=None, failures=0, error=OSError("reset")):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = archive
        pins = {self.TARGET: digest or hashlib.sha256(archive).hexdigest()}
        responses = [error] * failures + [response]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(check.urllib.request, "urlopen", side_effect=responses) as urlopen, \
                mock.patch.object(check.time, "sleep"), \
                mock.patch.dict(check.TIRITH_ARCHIVE_SHA256, pins):
            binary = check.install_tirith(Path(tmp), self.TARGET)
            return urlopen.call_args.args[0], binary.read_bytes(), binary.stat().st_mode & 0o777

    def test_the_binary_is_extracted_executable_from_the_pinned_url(self):
        url, data, mode = self.install(_archive({"tirith": b"ELF", "man/tirith.1": b"man"}))
        self.assertIn(f"/download/{check.TIRITH_VERSION}/tirith-{self.TARGET}.tar.gz", url)
        self.assertEqual((b"ELF", 0o755), (data, mode))

    def test_a_failed_download_is_retried_and_then_refused(self):
        archive = _archive({"tirith": b"ELF"})
        self.assertEqual(b"ELF", self.install(archive, failures=check.DOWNLOAD_ATTEMPTS - 1)[1])
        with self.assertRaises(check.ScannerUnavailable):
            self.install(archive, failures=check.DOWNLOAD_ATTEMPTS)

    def test_a_truncated_body_is_retried(self):
        archive = _archive({"tirith": b"ELF"})
        truncated = http.client.IncompleteRead(b"EL")
        self.assertEqual(b"ELF", self.install(archive, failures=1, error=truncated)[1])

    def test_a_digest_mismatch_is_refused(self):
        with self.assertRaises(check.ScannerUnavailable):
            self.install(_archive({"tirith": b"ELF"}), digest="0" * 64)

    def test_an_archive_without_the_binary_is_refused(self):
        with self.assertRaises(check.ScannerUnavailable):
            self.install(_archive({"README": b"x"}))


if __name__ == "__main__":
    unittest.main()

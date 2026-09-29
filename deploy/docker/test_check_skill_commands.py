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


def commands(body, first_line=1):
    return list(check.split_commands(body.splitlines(), first_line))


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

    def test_an_unclosed_fence_runs_to_the_end(self):
        self.assertEqual([["ls", "pwd"]], [b for _, _, b in check.code_blocks("```sh\nls\npwd\n")])


class SplitCommandsTest(unittest.TestCase):
    def test_one_command_per_line_with_its_line_number(self):
        self.assertEqual([(10, "ls"), (12, "pwd")], commands("ls\n\npwd", 10))

    def test_comments_are_skipped(self):
        self.assertEqual([(2, "ls")], commands("# list it\nls"))

    def test_an_apostrophe_in_a_comment_opens_no_quote(self):
        self.assertEqual([(2, "ls"), (3, "pwd")], commands("# don't\nls\npwd"))

    def test_a_trailing_backslash_joins_with_one_space(self):
        self.assertEqual([(1, "foo --a --b")], commands("foo \\\n  --a \\\n  --b"))

    def test_an_unclosed_quote_runs_on_with_its_newline(self):
        self.assertEqual([(1, "echo 'a\nb'"), (3, "ls")], commands("echo 'a\nb'\nls"))

    def test_a_heredoc_runs_to_its_delimiter(self):
        body = "cat <<'EOF' > f\nit's here\nEOF\nls"
        self.assertEqual([(1, "cat <<'EOF' > f\nit's here\nEOF"), (4, "ls")], commands(body))


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


class SkillCommandsTest(unittest.TestCase):
    def test_only_shell_blocks_and_findings_name_the_repository_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "demo" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text(
                "```bash\nls\n```\n```yaml\nkey: value\n```\n```\ntool_call()\n```\n```zsh\npwd\n```\n"
            )
            (Path(tmp) / "demo" / "README.md").write_text("```bash\nrm -rf /\n```\n")
            got = check.skill_commands("agents/platform/skills", Path(tmp))
        self.assertEqual(
            [
                check.Command("agents/platform/skills/demo/SKILL.md", 2, "ls"),
                check.Command("agents/platform/skills/demo/SKILL.md", 11, "pwd"),
            ],
            got,
        )


class ScanTest(unittest.TestCase):
    ALLOW = check.Command("s/SKILL.md", 1, "ls <dir>")
    WARN = check.Command("s/SKILL.md", 2, "export KUBECONFIG=x")
    BLOCK = check.Command("s/SKILL.md", 3, "$G add f")

    def test_block_and_warn_are_findings_and_allow_is_not(self):
        verdicts = {"ls dir": verdict("allow"), "export KUBECONFIG=x": verdict("warn", "sensitive_env_export"),
                    "$G add f": verdict("block", "analysis_incomplete")}
        findings = check.scan_commands([self.ALLOW, self.WARN, self.BLOCK], verdicts.__getitem__)
        self.assertEqual(
            [
                check.Finding(self.WARN, "warn", ("sensitive_env_export",)),
                check.Finding(self.BLOCK, "block", ("analysis_incomplete",)),
            ],
            findings,
        )

    def test_the_scanner_sees_placeholders_filled(self):
        seen = []
        check.scan_commands([self.ALLOW], lambda text: seen.append(text) or verdict("allow"))
        self.assertEqual(["ls dir"], seen)

    def test_a_fail_closed_verdict_stops_the_scan(self):
        failed = verdict("block", summary="tirith spawn failed (fail-closed)")
        with self.assertRaises(check.ScannerUnavailable):
            check.scan_commands([self.ALLOW], lambda text: failed)

    def test_the_probes_need_a_block_and_an_allow(self):
        working = {check.PROBE_REFUSED: verdict("block"), check.PROBE_ALLOWED: verdict("allow")}
        check.check_scanner(working.__getitem__)
        for broken in (lambda text: verdict("allow"), lambda text: verdict("block")):
            with self.assertRaises(check.ScannerUnavailable):
                check.check_scanner(broken)


class TriageTest(unittest.TestCase):
    def test_known_findings_pass_and_unmatched_entries_are_stale(self):
        known_cmd = check.Command("a/SKILL.md", 1, "known")
        new_cmd = check.Command("a/SKILL.md", 2, "new")
        findings = [check.Finding(known_cmd, "warn", ()), check.Finding(new_cmd, "block", ())]
        known = {known_cmd.key: "reason", ("a/SKILL.md", "gone"): "reason"}
        new, stale = check.triage(findings, known)
        self.assertEqual([findings[1]], new)
        self.assertEqual([("a/SKILL.md", "gone")], stale)


class KnownFindingsTest(unittest.TestCase):
    def test_every_entry_names_a_command_the_repository_ships(self):
        for path, text in check.KNOWN_FINDINGS:
            skill = REPO_ROOT / path
            tree = skill.parents[1]
            repo_dir = tree.relative_to(REPO_ROOT).as_posix()
            shipped = {c.text for c in check.skill_commands(repo_dir, tree) if c.path == path}
            self.assertIn(text, shipped, path)

    def test_every_entry_has_a_reason(self):
        for key, reason in check.KNOWN_FINDINGS.items():
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

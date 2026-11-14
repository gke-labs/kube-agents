"""Tests for `worker_transcript`, the shared transcript rules, against the repo's redactor.

The session-kv server's tests exercise the same rules through the feed's
wrappers and routes; these call the module's public API directly with the
redactor loaded by path, as the bench's in-pod script loads it.
"""

import importlib.util
import json
import sqlite3
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import worker_transcript  # noqa: E402

REDACTOR_PATH = (
    Path(__file__).resolve().parents[2] / "chat" / "defaults" / "plugins" / "common" / "redactor.py"
)
REDACTOR_MODULE_NAME = "kube_agents_test_worker_transcript_redactor"
WITHHELD = "[withheld: for the test]"
MARKER = worker_transcript.REDACTED_SECRET_MARKER


def _load_redactor():
    spec = importlib.util.spec_from_file_location(REDACTOR_MODULE_NAME, REDACTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[REDACTOR_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module.AuditRedactor


REDACTOR = _load_redactor()


def _scrub(kind, text):
    return worker_transcript.scrub_step_text(REDACTOR, kind, text, WITHHELD)


class TestScrubStepText(unittest.TestCase):
    def test_withheld_without_a_redactor(self):
        for kind in ("thinking", "tool_call", "tool_result", "reply"):
            with self.subTest(kind=kind):
                self.assertEqual(worker_transcript.scrub_step_text(None, kind, "x", WITHHELD), WITHHELD)

    def test_withheld_when_the_walk_exceeds_the_recursion_limit(self):
        class _TooDeep:
            @staticmethod
            def redact(value):
                raise RecursionError

        for kind in ("tool_call", "tool_result", "reply"):
            with self.subTest(kind=kind):
                self.assertEqual(worker_transcript.scrub_step_text(_TooDeep, kind, "x", WITHHELD), WITHHELD)
        # A document too deep to parse is scrubbed as text rather than dropped.
        deep = "[" * 100000 + "]" * 100000
        self.assertEqual(_scrub("tool_result", deep), deep)

    def test_cli_segment_rules_read_past_an_operator_inside_a_quoted_argument(self):
        for cmd, cred, kept in (
            (
                'curl "https://grafana/api/search?query=x&type=dash-db" -u admin:prom-operator',
                "prom-operator",
                "query=x&type=dash-db",
            ),
            (
                'mongosh "mongodb+srv://cluster0.example.net/app?retryWrites=true&w=majority" -u app -p hunter2',
                "hunter2",
                "retryWrites=true&w=majority",
            ),
            ('mysql -e "select 1 | 2" -p hunter2', "hunter2", "select 1 | 2"),
            ("mysql -e 'select 1' -p 'hunter2 x' && echo done", "hunter2", "&& echo done"),
        ):
            with self.subTest(cmd=cmd):
                out = _scrub("tool_call", json.dumps({"command": cmd}))
                self.assertNotIn(cred, out)
                self.assertIn(MARKER, out)
                self.assertIn(kept, out)
        chained = "curl -u admin:pw-one-11 http://a && curl -u root:pw-two-22 http://b"
        out = _scrub("tool_call", json.dumps({"command": chained}))
        self.assertNotIn("pw-one-11", out)
        self.assertNotIn("pw-two-22", out)
        self.assertIn("http://a && curl -u root:", out)

    def test_curl_basic_auth_password_is_masked(self):
        out = _scrub("tool_call", json.dumps({"command": "curl -u admin:hunter2 https://x/"}))
        self.assertEqual(json.loads(out), {"command": f"curl -u admin:{MARKER} https://x/"})

    def test_bearer_scheme_pair_after_a_credential_named_key(self):
        opaque = "3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c"
        short = "s3cr3t"
        tilde = "s3cr3t~tok"
        for key in ("Authorization: ", "auth: ", "token: ", "AUTH_HEADER="):
            for token in (opaque, short, tilde):
                line = f"{key}Bearer {token}"
                with self.subTest(line=line):
                    cleaned = _scrub("tool_result", line)
                    self.assertNotIn(token, cleaned)
                    self.assertIn(MARKER, cleaned)
        benign = "content-type: Bearer s3cr3t"
        self.assertEqual(_scrub("tool_result", benign), benign)

    def test_block_scalar_under_string_data_is_masked(self):
        yaml = (
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: tls\n"
            "data:\n  tls.key: TFMwdExTMUNSVWRKVGlCUVVrbFdRVlJGSUV0RldTMHRMUzB0\n"
            "stringData:\n  config.txt: |\n    first line with key: val\n"
            "    SUPER_SECRET_LINE_WITHOUT_COLON_12345\n    ANOTHER_LINE\n"
            "type: kubernetes.io/tls\n"
        )
        out = _scrub("tool_result", yaml)
        for gone in ("TFMwdExT", "SUPER_SECRET", "ANOTHER_LINE", "first line"):
            self.assertNotIn(gone, out)
        self.assertIn(f"  tls.key: {MARKER}\n", out)
        self.assertIn(f"  config.txt: {MARKER}\n    {MARKER}\ntype:", out)
        self.assertIn("name: tls", out)

    def test_json_data_object_is_masked_in_a_secret_and_when_cut_short(self):
        big = "Q" * 3000
        js = json.dumps({"apiVersion": "v1", "data": {"credentials.json": big, "ca.crt": "Q0VSVA=="}, "kind": "Secret"})
        out = _scrub("tool_result", js)
        self.assertNotIn("QQQQ", out)
        self.assertNotIn("Q0VSVA", out)
        self.assertEqual(json.loads(out)["data"], {"credentials.json": MARKER, "ca.crt": MARKER})
        cut = js[: js.index("Q0VSVA") + 3]
        self.assertNotIn("QQQQ", _scrub("tool_result", cut))

    def test_string_data_object_in_prose_is_masked(self):
        text = 'patched with kubectl patch secret db -p \'{"stringData":{"conn":"host=db user=app password=hunter2","license":"L-0042-ZZ"}}\''
        out = _scrub("reply", text)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("L-0042-ZZ", out)

    def test_continuation_fold_is_scoped_to_command_text(self):
        pw = "hunter2"
        for cmd in (
            f"curl \\\n  -u admin:{pw} \\\n  https://registry/",
            f"mysql -h db \\\n  -p {pw}",
            f"docker login --password \\\n  {pw}",
        ):
            with self.subTest(cmd=cmd):
                out = _scrub("tool_call", json.dumps({"command": cmd}))
                self.assertNotIn(pw, out)
                self.assertIn(MARKER, out)
                self.assertNotIn(pw, _scrub("tool_call", cmd))
        hard_break = "first line \\\nsecond line"
        self.assertEqual(_scrub("reply", hard_break), hard_break)
        self.assertEqual(_scrub("tool_result", hard_break), hard_break)
        other_arg = json.dumps({"path": "a \\\n  b", "command": "ls"})
        self.assertEqual(_scrub("tool_call", other_arg), other_arg)

    def test_only_an_odd_backslash_run_folds_as_a_continuation(self):
        # Bash removes a backslash-newline and adds nothing; it reads an odd run
        # before a newline as a continuation and an even run as literal
        # backslashes, so the newline still ends the command. A backslash
        # before a CR escapes the CR, so CRLF is no continuation.
        fold = worker_transcript.fold_shell_continuations
        self.assertEqual(fold("a\\\n  b"), "a  b")
        self.assertEqual(fold("kube\\\nctl"), "kubectl")
        self.assertEqual(fold("a\\\\\n  b"), "a\\\\\n  b")
        self.assertEqual(fold("a\\\\\\\n  b"), "a\\\\  b")
        self.assertEqual(fold("a\\\\\\\\\n  b"), "a\\\\\\\\\n  b")
        self.assertEqual(fold("a\\\r\nb"), "a\\\r\nb")
        # The masking fold joins an even run's lines with a space, as the file
        # did before it counted backslashes, so the stored call shows `\ `.
        stored = json.loads(_scrub("tool_call", json.dumps({"command": "grep -rn\\\\\nU=x\ncurl \"$U\""})))["command"]
        self.assertEqual(stored, "grep -rn\\ U=x\ncurl \"$U\"")

    def test_masking_joins_an_even_run_or_a_crlf_as_before(self):
        # A nested `bash -c "…"` halves an even run inside double quotes and
        # continues the line, so the credential rules read every run joined;
        # an extra mask is harmless, and a missed one is not.
        for cmd in (
            'bash -c "mysql -h db --password \\\\\nhunter2"',
            "curl -u \\\\\nadmin:hunter2 https://x",
            "mysql --password \\\r\nhunter2",
        ):
            with self.subTest(cmd=cmd):
                self.assertNotIn("hunter2", _scrub("tool_call", json.dumps({"command": cmd})))
        # Detection still reads only an odd run before an LF as a continuation.
        self.assertTrue(worker_transcript.command_reads_a_secret("echo done\\\\\nkubectl get secret db -o yaml"))
        # Runs of 5 and 6 backslashes, past the pairs the other tests cover.
        fold = worker_transcript.fold_shell_continuations
        self.assertEqual(fold("a" + "\\" * 5 + "\nb"), "a" + "\\" * 4 + "b")
        self.assertEqual(fold("a" + "\\" * 6 + "\nb"), "a" + "\\" * 6 + "\nb")

    def test_the_scan_folds_exactly_as_the_regexes_it_replaced(self):
        # The two folds were regexes until CodeQL flagged them as polynomial;
        # the one-pass scan must give the same result on every input. The old
        # regexes live here only, as the reference.
        import itertools
        import random
        import re

        old_continuation = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
        old_break_run = re.compile(r"(?<!\\)(\\+)(\r?\n)([ \t]*)")

        def old_detect(command):
            return old_continuation.sub(lambda m: m.group(1), command)

        def old_mask(command):
            def one(m):
                run, line_break, indent = m.groups()
                if len(run) % 2 and line_break == "\n":
                    return run[:-1] + indent
                return run[:-1] + " "
            return old_break_run.sub(one, command).replace("\\ ", "\ue000")

        inputs = []
        for head, run, line_break, tail in itertools.product(
            ("a", 'bash -c "mysql --password', "echo 'q", "x\t", ""),
            range(9),
            ("\n", "\r\n", ""),
            (" b", "\tb", "  \t b", '"', "'", "", "\\"),
        ):
            inputs.append(head + "\\" * run + line_break + tail)
        rng = random.Random(5450)
        alphabet = ["\\", "\\", "\n", "\r", " ", "\t", '"', "'", "a", "-c "]
        inputs += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16))) for _ in range(3000)]
        for command in inputs:
            with self.subTest(command=command):
                self.assertEqual(worker_transcript.fold_shell_continuations(command), old_detect(command))
                self.assertEqual(worker_transcript.fold_shell_command(command), old_mask(command))

    def test_a_continuation_inside_a_word_joins_the_word(self):
        # The fold used to put a space where bash puts nothing, so a word split
        # by a continuation hid a secret read or a credential flag.
        for cmd in (
            "kube\\\nctl get secret db -o yaml",
            "kubectl get sec\\\nret db -o yaml",
            "gcl\\\noud secrets versions access latest --secret=db",
            "cat /var/run/sec\\\nrets/kubernetes.io/serviceaccount/token",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(worker_transcript.command_reads_a_secret(cmd))
        for cmd in ("mysql --pass\\\nword=hunter2", "curl -u admin:hun\\\nter2 https://x"):
            with self.subTest(cmd=cmd):
                out = _scrub("tool_call", json.dumps({"command": cmd}))
                self.assertNotIn("hunter2", out)
                self.assertNotIn("ter2", out)

    def test_a_secret_read_after_an_even_backslash_run_is_detected(self):
        # The even run used to fold the newline, so the read joined the line
        # above and was not seen.
        for cmd in ("echo done\\\\\nkubectl get secret db -o yaml", "echo done\\\\\\\\\ncat /var/run/secrets/token"):
            with self.subTest(cmd=cmd):
                self.assertTrue(worker_transcript.command_reads_a_secret(cmd))
                self.assertTrue(worker_transcript.is_secret_read_tool_call(json.dumps({"command": cmd})))
        self.assertFalse(worker_transcript.command_reads_a_secret("echo done\\\\\nkubectl get pods"))

    def test_unquoted_value_after_a_credential_key_is_masked_to_the_line_end(self):
        for line in ("password: one two three", "DB_PASSWORD=one two three", "DB_PASSWORD:  my secret phrase here"):
            with self.subTest(line=line):
                out = _scrub("tool_result", f"{line}   \nnext: line")
                for word in ("one", "two", "three", "my", "phrase", "here"):
                    self.assertNotIn(word, out.split("\n")[0])
                self.assertEqual(out.count(MARKER), 1)
                self.assertTrue(out.endswith("   \nnext: line"), out)
        # A quoted value ends at its quote, and keeps the quotes the redactor kept.
        self.assertEqual(_scrub("tool_result", 'password: "one two" three'), f'password: "{MARKER}" three')

    def test_shell_command_values_end_at_their_word(self):
        # A tool call's command is read as a shell line: the flags after a
        # masked assignment stay, and the marker is not a scheme that takes
        # the next word.
        for command, kept in (
            ("helm upgrade app chart --set db.password=hunter2 --namespace prod", f"--set db.password={MARKER} --namespace prod"),
            ("kubectl set env deploy/app DB_PASSWORD=x LOG_LEVEL=debug -n prod", f"DB_PASSWORD={MARKER} LOG_LEVEL=debug -n prod"),
            ("docker run -e DB_PASSWORD=x -e X=y image", f"-e DB_PASSWORD={MARKER} -e X=y image"),
            ("export GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 && gh auth status", f"export GH_TOKEN={MARKER} && gh auth status"),
        ):
            with self.subTest(command=command):
                out = json.loads(_scrub("tool_call", json.dumps({"command": command})))["command"]
                self.assertIn(kept, out)
                self.assertNotIn("hunter2", out)
                bare = _scrub("tool_call", command)
                self.assertIn(kept, bare)
        # A file-write argument (`TEXT_ARG_KEYS`) is text: a manifest in
        # `content` is masked to the line's end.
        out = json.loads(_scrub("tool_call", json.dumps({"path": "x.yaml", "content": "password: one two three\nkind: Pod\n"})))
        self.assertEqual(out["content"], f"password: {MARKER}\nkind: Pod\n")

    def test_heredoc_body_and_escaped_space_values_leave_no_word(self):
        heredoc = "cat <<EOF > values.yaml\nauth:\n  password: my secret phrase\nEOF"
        for out in (
            json.loads(_scrub("tool_call", json.dumps({"command": heredoc})))["command"],
            _scrub("tool_call", heredoc),
        ):
            for word in ("my", "secret phrase", "phrase"):
                self.assertNotIn(word, out)
            self.assertEqual(out, f"cat <<EOF > values.yaml\nauth:\n  password: {MARKER}\nEOF")
        escaped = "kubectl create secret generic db --from-literal=password=my\\ secret\\ phrase -n prod"
        for out in (
            json.loads(_scrub("tool_call", json.dumps({"command": escaped})))["command"],
            _scrub("tool_call", escaped),
        ):
            self.assertEqual(out, f"kubectl create secret generic db --from-literal=password={MARKER} -n prod")
        # An escaped space the rules do not mask is given back as written.
        plain = "ls my\\ dir -n prod"
        self.assertEqual(_scrub("tool_call", plain), plain)

    def test_a_value_after_an_even_backslash_run_is_masked_by_every_rule(self):
        # #5460: bash ends a word at `a\\ ` (the pair is one literal
        # backslash), but `eval`, `ssh host …`, `watch` and a nested
        # `bash -c "…"` read the words again and halve the run, so the value
        # rules read it as part of the word before it and mask it either way.
        # Runs 1-4, plain and re-read, in the JSON and the bare form.
        shapes = (
            "export DB_PASSWORD=pre{run} S3cretValue9",
            "kubectl create secret generic db --from-literal=password=pre{run} S3cretValue9",
            "mysql --password pre{run} S3cretValue9",
            "mysql --password=pre{run} S3cretValue9",
            "curl -u admin:pre{run} S3cretValue9 https://x",
            "mysql -ppre{run} S3cretValue9",
            "redis-cli -a pre{run} S3cretValue9",
            "curl https://admin:pre{run} S3cretValue9@host/x",
        )
        # Known limits, the same on main and pinned so a fix flips them: the
        # URL userinfo rules stop at a literal backslash (run 2 and more
        # leaves one), and inside a quoted `bash -c "…"` script the short and
        # user:pass flag rules do not run (`sub_shell_segments`).
        def known_limit(shape, run, wrapper):
            if "https://admin:" in shape and run > 1:
                return True
            return wrapper == "bash -c" and any(f in shape for f in ("-u admin:", "-ppre", "-a pre"))

        for shape in shapes:
            for run in range(1, 5):
                plain = shape.format(run="\\" * run)
                for wrapper, cmd in (("", plain), ("eval", "eval " + plain), ("bash -c", f'bash -c "{plain}"')):
                    for text in (json.dumps({"command": cmd}), cmd):
                        with self.subTest(cmd=cmd, form=text[:1]):
                            out = _scrub("tool_call", text)
                            if known_limit(shape, run, wrapper):
                                self.assertIn("S3cretValue9", out)
                            else:
                                self.assertNotIn("S3cretValue9", out)

    def test_a_flag_after_an_even_backslash_run_starts_a_word(self):
        # The fold joins `a\\ --password` into one token, where bash reads the
        # flag as a new word; the flag rules read a flag after the fold mark as
        # a new word in a second pass, so the value after the flag is masked
        # (it leaked before #5460).
        for cmd in (
            "ls a\\\\ --password S3cretValue9",
            "mysql -h db a\\\\ -pS3cretValue9",
            "curl x\\\\ -u admin:S3cretValue9 https://x",
            "redis-cli x\\\\ -a S3cretValue9",
            "echo \\\\ --password S3cretValue9",
        ):
            for text in (json.dumps({"command": cmd}), cmd):
                with self.subTest(cmd=cmd, form=text[:1]):
                    out = _scrub("tool_call", text)
                    self.assertNotIn("S3cretValue9", out)
                    self.assertIn("\\\\ -", out)

    def test_every_escaped_space_folds_and_unmasked_text_comes_back(self):
        # The issue's two examples: an escaped space folds, and so does one
        # after an even run, by design (the re-read reasons above).
        fold = worker_transcript.fold_shell_command
        mark = worker_transcript.SHELL_ESCAPED_SPACE_FOLD
        self.assertEqual(fold("ls my\\ file"), f"ls my{mark}file")
        self.assertEqual(fold("ls a\\\\ b"), f"ls a\\{mark}b")
        # Text no rule masks comes back as written, the mark unfolded.
        for run in range(9):
            for after in (" b", "\tb", "\nb", "", " --verbose x", " -n prod"):
                cmd = "ls a" + "\\" * run + after
                for text in (json.dumps({"command": cmd}), cmd):
                    with self.subTest(cmd=cmd, form=text[:1]):
                        out = _scrub("tool_call", text)
                        served = json.loads(out)["command"] if text.startswith("{") else out
                        self.assertNotIn(mark, served)
                        if "\n" not in after:
                            self.assertEqual(served, cmd)

    def test_a_match_at_the_mark_never_takes_a_real_flags_value(self):
        # The flags after the mark are a second pass, so a flag letter inside
        # an escaped file name (`my\\ -prod.cnf`) cannot take the text the
        # real flag's match needs; it may mask a file-name tail too, which is
        # harmless.
        for cmd in (
            "mysql --defaults-file=/etc/my\\ -prod.cnf -p S3cretValue9",
            "redis-cli --tls-cert my\\ -app.pem -a S3cretValue9",
            "mysql -h db -u root my\\ -abc -pS3cretValue9",
            "mytool my\\ --draft --password S3cretValue9",
            "curl a\\ -u 'x:-y -u admin:S3cretValue9'",
        ):
            for text in (json.dumps({"command": cmd}), cmd):
                with self.subTest(cmd=cmd, form=text[:1]):
                    self.assertNotIn("S3cretValue9", _scrub("tool_call", text))

    def test_a_run_of_escaped_user_flags_scrubs_in_linear_time(self):
        # The second pass's `user:pass` user part stops at the mark; without
        # that, 20000 `\\ -u` took seconds.
        import time

        start = time.monotonic()
        _scrub("tool_call", "curl " + "\\ -u" * 20000)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_the_flag_rules_mask_every_value_the_old_rules_masked(self):
        # A property check: with the second pass off (the rules as before
        # #5460), no command masks a value the two passes do not.
        import random
        import re

        never = re.compile(r"(?!)")
        before = {
            "CLI_LONG_FLAG_AT_FOLD_RE": never,
            "CLI_USERPASS_ARG_AT_FOLD_RE": never,
            "CLI_SHORT_P_AT_FOLD_RE": never,
        }
        rng = random.Random(5460)
        words = ["mysql ", "curl ", "redis-cli ", "ls ", "--password ", "-p", "-u ", "-a ", "admin:", "x",
                 "\\", "\\\\", "\\ ", "\\\\ ", " ", "=", "S3cretValue9", '"', "eval ", "-n prod",
                 "-prod.cnf ", "--draft ", "'x:-y ", "my\\ "]
        commands = ["".join(rng.choice(words) for _ in range(rng.randint(2, 12))) for _ in range(4000)]
        for cmd in commands:
            new_out = _scrub("tool_call", cmd)
            with mock.patch.multiple(worker_transcript, **before):
                old_out = _scrub("tool_call", cmd)
            with self.subTest(cmd=cmd):
                if "S3cretValue9" not in old_out:
                    self.assertNotIn("S3cretValue9", new_out)

    def test_a_flag_after_a_masked_key_value_keeps_its_value_masked(self):
        # #5535: the redactor runs first, and its key-value rule reads a bare
        # value up to whitespace, which the escaped-space mark is not, so it
        # took the flag word into the value it masked and served the flag's
        # value. The flag rules now read those values before the redactor.
        for run in (1, 2, 4):
            plain_cmds = (
                "DB_PASSWORD=x" + "\\\\" * run + " --password S3cretValue9",
                "mysql DB_PASSWORD=x" + "\\\\" * run + " -p S3cretValue9",
                "curl TOKEN=x" + "\\\\" * run + " -u admin:S3cretValue9 https://x",
            )
            for plain in plain_cmds:
                for wrapper, cmd in (("", plain), ("eval", "eval " + plain), ("bash -c", f'bash -c "{plain}"')):
                    for text in (json.dumps({"command": cmd}), cmd):
                        with self.subTest(cmd=cmd, form=text[:1]):
                            out = _scrub("tool_call", text)
                            # Known limit, the same before: inside a quoted
                            # `bash -c "…"` script the short and user:pass
                            # flag rules do not run (`sub_shell_segments`).
                            if wrapper == "bash -c" and not plain.startswith("DB_PASSWORD"):
                                self.assertIn("S3cretValue9", out)
                            else:
                                self.assertNotIn("S3cretValue9", out)

    def test_a_quoted_or_equals_flag_value_after_a_masked_key_value_is_masked(self):
        # The redactor's bare value runs on through `=` and an unquoted
        # `user:` and ends at the first whitespace, quote, `,{}]` or
        # backslash escape, so how much of the flag its marker took depends
        # on the spelling. A quoted value ends at its closing quote, whatever
        # follows it.
        shapes = (
            'DB_PASSWORD=x%s --password="S3cretValue9"',
            "DB_PASSWORD=x%s --password='S3cretValue9'",
            'DB_PASSWORD=x%s --password "S3cretValue9"',
            'DB_PASSWORD=x%s --password= "S3cretValue9"',
            'mysql DB_PASSWORD=x%s -p="S3cretValue9"',
            'mysql DB_PASSWORD=x%s -p"S3cretValue9"',
            'curl TOKEN=x%s -u admin:"S3cretValue9" https://x',
            "curl TOKEN=x%s -u admin:'S3cretValue9' https://x",
            'curl TOKEN=x%s --user=admin:"S3cretValue9" https://x',
            'curl TOKEN=x%s --user="admin:S3cretValue9" https://x',
            'curl TOKEN=x%s -uadmin:"S3cretValue9" https://x',
            'curl TOKEN=x%s --user=ad,min:"S3cretValue9" https://x',
            'curl TOKEN=x%s --user=ad}min:"S3cretValue9" https://x',
            'curl TOKEN=x%s -uad]min:"S3cretValue9" https://x',
            'curl TOKEN=x%s -uad{min:"S3cretValue9" https://x',
            'curl TOKEN=x%s -uad[min:"S3cretValue9" https://x',
            'curl TOKEN=x%s -uad\\nmin:"S3cretValue9" https://x',
            "DB_PASSWORD=x%s --password=,S3cretValue9",
            "DB_PASSWORD=x%s --password=\\nS3cretValue9",
            'DB_PASSWORD=x%s --password="S3cretValue9"\\ --host=db',
            'mysql DB_PASSWORD=x%s -p"S3cretValue9"\\ -h\\ db',
            'curl TOKEN=x%s -u admin:"S3cretValue9"\\ https://x',
        )
        for run in (1, 2, 4):
            for shape in shapes:
                plain = shape % ("\\" * run)
                for cmd in (plain, "eval " + plain):
                    for text in (json.dumps({"command": cmd}), cmd):
                        with self.subTest(cmd=cmd, form=text[:1]):
                            self.assertNotIn("S3cretValue9", _scrub("tool_call", text))
        # A value made of symbols alone is a value too, quoted or after `=`.
        for shape in (
            'curl TOKEN=x\\ -u admin:"%s" https://x',
            "curl TOKEN=x\\ -u admin:'%s' https://x",
            'DB_PASSWORD=x\\ --password="%s"',
            'mysql DB_PASSWORD=x\\ -p="%s"',
            'mysql DB_PASSWORD=x\\ -p"%s"',
            'curl TOKEN=x\\ --user=admin:"%s" https://x',
            'curl TOKEN=x\\ -uadmin:"%s" https://x',
            "DB_PASSWORD=x\\ --password=,%s",
        ):
            cmd = shape % "#%!!+~"
            for text in (json.dumps({"command": cmd}), cmd):
                with self.subTest(cmd=cmd, form=text[:1]):
                    self.assertNotIn("#%!!+~", _scrub("tool_call", text))

    def test_a_bare_user_pass_value_before_a_shell_operator_stays_masked(self):
        # The `user:pass` rule ends its segment at `;`, `|` or `&`, so the
        # value read stops there, and the served value ends a word there too.
        for tail in (";echo done", "|cat", "&&ls", " ; ls", ""):
            cmd = "curl https://x TOKEN=x\\\\ -u admin:S3cretValue9" + tail
            for text in (json.dumps({"command": cmd}), cmd):
                with self.subTest(cmd=cmd, form=text[:1]):
                    out = _scrub("tool_call", text)
                    self.assertNotIn("S3cretValue9", out)
                    self.assertIn(tail, out)
        # A quote and a closing parenthesis end the word too.
        for cmd in (
            "curl TOKEN=x\\ -u admin:S3cretValue9' https://x",
            'bash -c "(curl TOKEN=x\\ -u admin:S3cretValue9)" ; curl TOKEN=x\\ -u admin:S3cretValue9',
        ):
            with self.subTest(cmd=cmd):
                self.assertNotIn("S3cretValue9", _scrub("tool_call", cmd))

    def test_a_value_read_before_the_redactor_keeps_its_flag_and_marker_spellings(self):
        # Each value keeps the spelling after its own flag and the one after
        # the marker that took the flag word; the quoted spelling and the one
        # after the marker that ran on to the first end of the redactor's
        # value are added to them, not put in their place (after a space
        # separator it is the marker spelling again). Each spelling carries
        # the offset where its value starts.
        marker = worker_transcript.REDACTED_SECRET_MARKER
        secret, quoted = "S3cretValue9", '"S3cretValue9"'
        cases = (
            ("DB_PASSWORD=x\\\\ --password S3cretValue9", [("--password ", secret), (marker + " ", secret)] + [(marker + " ", secret)]),
            ("mysql DB_PASSWORD=x\\\\ -p S3cretValue9", [("-p ", secret), (marker + " ", secret)] + [(marker + " ", secret)]),
            (
                "curl TOKEN=x\\\\ -u admin:S3cretValue9;echo",
                [("-u admin:", secret), (marker + " admin:", secret)] + [(marker + " admin:", secret)],
            ),
            (
                'curl TOKEN=x\\\\ -u admin:"S3cretValue9"',
                [
                    ("-u admin:", secret),
                    (marker + " admin:", secret),
                    ("-u admin:", quoted),
                    (marker + " admin:", quoted),
                    (marker + " admin:", quoted),
                ],
            ),
            (
                'curl TOKEN=x\\\\ --user=admin:"S3cretValue9"',
                [
                    ("--user=admin:", secret),
                    (marker + "=admin:", secret),
                    ("--user=admin:", quoted),
                    (marker + "=admin:", quoted),
                    (marker, quoted),
                ],
            ),
            (
                'DB_PASSWORD=x\\\\ --password="S3cretValue9"',
                [("--password=", quoted), (marker + "=", quoted), (marker, quoted)],
            ),
            # The redactor's value ends inside the value, not at its start:
            # no spelling after the marker.
            (
                "mysql DB_PASSWORD=x\\\\ --password=S,3cretValue9",
                [("--password=", "S,3cretValue9"), (marker + "=", "S,3cretValue9")],
            ),
        )
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                read = worker_transcript.at_fold_flag_secrets(REDACTOR, worker_transcript.fold_shell_command(cmd))
                self.assertEqual(read, [tuple((lead + value, len(lead)) for lead, value in want)])

    def test_a_spelling_is_masked_at_each_place_it_ends_a_word(self):
        # The second place of `-pa-pa` starts inside the first, which does
        # not end a word.
        marker = worker_transcript.REDACTED_SECRET_MARKER
        mask = worker_transcript.mask_served_values
        self.assertEqual(mask("mysql x-pa-pa-pa", [(("-pa-pa", 2),)]), "mysql x-pa-p" + marker)
        self.assertEqual(mask("mysql -pa-pa x-pa-pa", [(("-pa-pa", 2),)]), "mysql -p" + marker + " x-p" + marker)
        # Places that overlap get one marker, from the first value's start.
        self.assertEqual(mask("-u a:b -p c", [(("-u a:b -p", 5), ("-p c", 3))]), "-u a:" + marker)
        self.assertEqual(mask("no flag here", [(("-p c", 3),)]), "no flag here")
        # In a run of its own flag word the value's last place ends a word.
        run = "mysql\\ -p" + "S-p" * 8 + "S " + "S-p" * 24 + "S"
        self.assertEqual(_scrub("tool_call", run), "mysql\\ -p" + marker + " " + "S-p" * 16 + marker)
        # A command without an escaped space has no value to read.
        self.assertEqual(worker_transcript.at_fold_flag_secrets(REDACTOR, "mysql DB_PASSWORD=x -p S3cretValue9"), [])

    def test_a_long_lead_or_a_run_of_markers_scrubs_in_linear_time(self):
        # A value has at most three leads, whatever the lead's length, and
        # each spelling is searched for as plain text: a lead that repeats
        # itself or the marker, and a spelling found again inside its own
        # last place, do not cost quadratic time.
        import time

        marker = worker_transcript.REDACTED_SECRET_MARKER
        for cmd in (
            "curl TOKEN=x\\\\ -u " + "a" * 8000 + ':"S3cretValue9" https://x',
            "curl TOKEN=x\\\\ -u " + "," * 8000 + ':"S3cretValue9" https://x',
            "DB_PASSWORD=x\\\\ --password" + "=" * 8000 + '"S3cretValue9"',
            'mysql DB_PASSWORD=x\\\\ -p="S3cretValue9" ' + marker * 16000,
            "curl h\\ -u=a" + ("," + marker) * 8000 + ':"S3cretValue9"',
            "curl h\\ " + "-u," * 50000 + 'a:"S3cretValue9"',
            "curl h\\ -u" + marker * 8000 + ":S3cretValue9",
            "curl TOKEN=x\\\\ -u" + marker * 8000 + ':"S3cretValue9" ; echo ' + marker * 8002,
            "curl TOKEN=x\\ -ua:" + "S-ua:" * 40000 + "S ; echo " + "-ua:S" * 60000 + "-",
            "curl TOKEN=x\\ -ua:" + "S)-ua:" * 32000 + "S ; echo " + "-ua:S)" * 64000,
            "TOKEN=x\\ mysql\\ --password= abc\\ -p S3cretValue9 ; mysql\\ -p"
            + ":-p" * 24600
            + ": ; echo -p"
            + ":-p" * 75300
            + ": y",
        ):
            with self.subTest(cmd=cmd[:40]):
                start = time.monotonic()
                out = _scrub("tool_call", cmd)
                self.assertLess(time.monotonic() - start, 1.0)
                self.assertNotIn("S3cretValue9", out)

    def test_a_value_that_holds_the_next_flag_word_does_not_hide_the_next_value(self):
        # The first value is `abc\ -p` (or `abc\ -u`): its place overlaps
        # the place of the second value, and one marker stands for both. The
        # same for three values in a row, for a value with an escaped shell
        # operator, and for a quoted value that ends at the next value's
        # opening quote.
        for cmd in (
            "TOKEN=x\\ mysql\\ --password= abc\\ -p S3cretValue9",
            "TOKEN=x\\ mysql\\ --password abc\\ -p S3cretValue9",
            "TOKEN=x\\ curl\\ --secret= abc\\ -u admin:S3cretValue9 https://x",
            "TOKEN=x\\ curl\\ --secret=,abc\\ -u admin:S3cretValue9 https://x",
            "PASSWORD=x\\ mysql\\ -ua,b:abc\\ -p S3cretValue9",
            "eval TOKEN=x\\\\ mysql\\\\ --password= abc\\\\ -p S3cretValue9",
            "TOKEN=x\\ mysql\\ --password= A1secret\\ -p S3cretValue9\\ --user=root:C3secret",
            "DB_PASSWORD=x\\ mariadb\\ --passphrase= A1secret\\ -u admin:S3cretValue9\\ -p=C3secret",
            "TOKEN=x\\ mysql\\ -u= admin:U1secret\\ -pS1part\\&S3cretValue9",
            'PASSWORD=x\\ curl\\ --password="S2secret\\ -u "admin:S3cretValue9""',
        ):
            for text in (json.dumps({"command": cmd}), cmd):
                with self.subTest(cmd=cmd, form=text[:1]):
                    out = _scrub("tool_call", text)
                    for secret in ("S3cretValue9", "A1secret", "C3secret", "U1secret", "S1part", "S2secret"):
                        self.assertNotIn(secret, out)
        # A quoted spelling found again inside its own place: one marker.
        marker = worker_transcript.REDACTED_SECRET_MARKER
        self.assertEqual(_scrub("tool_call", 'mysql TOKEN=x\\ -p"S1-p"S1-p"'), "mysql TOKEN=" + marker + marker)

    def test_a_spelling_that_does_not_end_a_word_many_times_still_masks_its_value(self):
        # `--token ab` is found 1100 times in front of other letters; the
        # search goes on to the place that ends a word.
        cmd = "PASSWORD=x\\ --token ab TOKEN=y\\ curl\\ -u root:S3cretValue9 " + "x--token abc " * 1100 + "--token ab"
        out = _scrub("tool_call", cmd)
        self.assertNotIn("S3cretValue9", out)
        self.assertEqual(out.count("x--token abc "), 1100)
        self.assertTrue(out.endswith("--token " + worker_transcript.REDACTED_SECRET_MARKER))

    def test_a_value_the_mask_leaves_alone_takes_no_place_from_a_later_value(self):
        # At most 32 values are kept. A quoted shell operator is not kept, so
        # 32 of them do not push out the value after them; a 33rd real value
        # is past the limit and is served, as before.
        many = "curl https://x" + '\\ -u a:"&"' * 32 + " TOKEN=x\\\\ -u admin:S3cretValue9 https://y"
        for text in (json.dumps({"command": many}), many):
            with self.subTest(form=text[:1]):
                self.assertNotIn("S3cretValue9", _scrub("tool_call", text))
        past = "curl " + " ".join("TOKEN=x\\ -u u:Secret%02dValue" % n for n in range(33))
        out = _scrub("tool_call", past)
        for n in range(32):
            self.assertNotIn("Secret%02dValue" % n, out)
        self.assertIn("Secret32Value", out)

    def test_two_values_with_one_served_spelling_take_the_form_that_masks_more(self):
        # `-ua,x:S` and `-p,x:S` are both served as the marker and `,x:S`;
        # the short-flag value is all of it, and no part of it is served.
        cmd = "curl TOKEN=t\\ -ua,hunter2Pw:S3cretValue9 https://x ; mysql PASSWORD=t\\ -p,hunter2Pw:S3cretValue9"
        for text in (json.dumps({"command": cmd}), cmd):
            with self.subTest(form=text[:1]):
                out = _scrub("tool_call", text)
                self.assertNotIn("hunter2Pw", out)
                self.assertNotIn("S3cretValue9", out)
        # The value that masks more is read first here.
        cmd = "TOKEN=t\\ --password=,hunter2Pw:S3cretValue9 ; curl TOKEN=t\\ -ua,hunter2Pw:S3cretValue9 https://x"
        out = _scrub("tool_call", cmd)
        self.assertNotIn("hunter2Pw", out)
        self.assertNotIn("S3cretValue9", out)
        # `-a ad[,c:'S'` and `-u= ad[,c:'S'`: the short flag's value is the
        # `user:` of the other. The two places overlap, and one marker
        # stands for both from the first value's start.
        cmd = "mysql PASSWORD=t\\\\ -a ad[,c:'S3cretValue9' ; docker login passwd:t\\ -u= ad[,c:'S3cretValue9';"
        out = _scrub("tool_call", cmd)
        self.assertNotIn("S3cretValue9", out)
        self.assertNotIn("ad[,c:", out)

    def test_the_spelling_after_the_marker_leaves_other_markers_text_alone(self):
        # A value made of closing punctuation alone gets no spelling after
        # the marker, so the text after other markers in the command stays.
        marker = worker_transcript.REDACTED_SECRET_MARKER
        cases = (
            (
                "curl -d '{\"password\": \"hunter2\", \"user\": \"bob\"}' https://x ; "
                "python3 -c 'run([\"DB_PASSWORD=x\\ --password=\", pw])'",
                "curl -d '{\"password\": \"" + marker + "\", \"user\": \"bob\"}' https://x ; "
                "python3 -c 'run([\"DB_PASSWORD=" + marker + "\", pw])'",
            ),
            (
                "printf 'DB_PASSWORD=abc\\n' > .env && printf 'TOKEN=x\\ --token=\\n'",
                "printf 'DB_PASSWORD=" + marker + "\\n' > .env && printf 'TOKEN=" + marker + "\\n'",
            ),
            (
                "curl -d {API_KEY=abc} https://x TOKEN=x\\ --password=}",
                "curl -d {API_KEY=" + marker + "} https://x TOKEN=" + marker + "}",
            ),
            (
                "curl -d '{\"password\": \"hunter2\", \"user\": \"bob\"}' TOKEN=x\\ -ua:'\",' https://x",
                "curl -d '{\"password\": \"" + marker + "\", \"user\": \"bob\"}' TOKEN=" + marker + "'\",' https://x",
            ),
            (
                "python3 -c 'run(\"DB_PASSWORD=abc\") ; run(\"TOKEN=x\\ --password=\")'",
                "python3 -c 'run(\"DB_PASSWORD=" + marker + "\") ; run(\"TOKEN=" + marker + "\")'",
            ),
            # A shell operator is not a value, and a value ends a word.
            ("TOKEN=x\\ --password && ls", "TOKEN=" + marker + " && ls"),
            ("mysql -u r\\ -p1 -h 10.0.0.1 && grep -p12 f", "mysql -u r\\ -p" + marker + " -h 10.0.0.1 && grep -p12 f"),
        )
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(_scrub("tool_call", cmd), want)
                self.assertEqual(json.loads(_scrub("tool_call", json.dumps({"command": cmd})))["command"], want)

    def test_the_longest_spelling_at_a_place_is_masked_whole(self):
        # `ab` is a value of its own and the start of another: the longer
        # value is not cut after `ab`.
        out = _scrub("tool_call", "TOKEN=x\\ --password ab;cdSecret9 TOKEN=y\\ --password ab")
        self.assertNotIn("cdSecret9", out)

    def test_the_redactor_value_end_rule_is_the_redactors(self):
        # `REDACTOR_BARE_VALUE_END_RE` restates where the bare branch of the
        # redactor's key-value rule ends; a change there must change it too.
        tails = [chr(code) for code in range(32, 127)] + ["\t", "\n", "\r", ""]
        tails += ["\\" + char for char in 'nrt"/x \\']
        for tail in tails:
            with self.subTest(tail=tail):
                value = "ab" + tail + "cd"
                found = REDACTOR.SECRET_KV_PATTERN.match("password=" + value)
                ours = worker_transcript.REDACTOR_BARE_VALUE_END_RE.search(value)
                self.assertEqual(found.group("bare"), value[: ours.start()] if ours else value)
                if ours:
                    self.assertLessEqual(len(ours.group(0)), worker_transcript.REDACTOR_BARE_VALUE_END_MAX_LEN)

    def test_a_flag_value_another_rule_rewrote_in_part_is_a_known_limit(self):
        # Known limit, the same before: when another rule rewrites part of
        # the value (an e-mail address in it, or as the `user:`), or the
        # redactor's value ends inside a value joined to its flag, the served
        # text no longer spells the value read, and the rest is served.
        for cmd, served in (
            ('DB_PASSWORD=x\\\\ --password "S3cretValue9:admin@example.com"', "S3cretValue9"),
            ("curl TOKEN=x\\\\ -u admin@example.com:S3cretValue9 https://x", "S3cretValue9"),
            ("mysql DB_PASSWORD=x\\\\ -pS3,cretValue9", ",cretValue9"),
            ("DB_PASSWORD=x\\\\ --password=S3}cretValue9", "}cretValue9"),
        ):
            with self.subTest(cmd=cmd):
                self.assertIn(served, _scrub("tool_call", cmd))

    def test_a_value_read_before_the_redactor_is_masked_only_after_its_flag(self):
        # The same string elsewhere in the command is not the flag's value:
        # a short value must not mask letters in other words, spaces or the
        # markers already there.
        cases = (
            ("cd mysql && TOKEN=x\\\\ --password mysql ls mysql", ("cd mysql &&", "ls mysql")),
            ("mysql -h db.example.com -u root\\ -pe -e 'select 1'", ("db.example.com", "'select 1'")),
            ("mysql -u r\\ -p1 -h 10.0.0.1", ("10.0.0.1",)),
        )
        for cmd, kept in cases:
            for text in (json.dumps({"command": cmd}), cmd):
                with self.subTest(cmd=cmd, form=text[:1]):
                    out = _scrub("tool_call", text)
                    for word in kept:
                        self.assertIn(word, out)
                    self.assertNotIn("[R[", out)

    def test_reading_flag_values_first_never_masks_less(self):
        # A property check with redactor-only secrets in the mix: the
        # redactor and every rule read the same text as before, so with the
        # early read off (the rules as before #5535) no command masks a
        # secret the early read does not.
        import random

        secrets = (
            "S3cretValue9",
            "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
            "AKIA" + "IOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlc2lnbmF0dXJl",
            "-----BEGIN RSA PRIVATE KEY-----",
            "https://admin:Pa55wordQ9@host",
        )
        rng = random.Random(5535)
        words = ["mysql ", "curl ", "redis-cli ", "ls ", "--password ", "--token=", "-p", "-u ", "-a ", "admin:", "x",
                 "DB_PASSWORD=", "TOKEN=", "password: ", "\\", "\\\\", "\\ ", "\\\\ ", " ", "=", '"', "'",
                 "eval ", "-n prod", "-prod.cnf ", "--draft ", "my\\ ", "; ", "| ", "--password=", "-p=", "--user=",
                 '"S3cretValue9"', "'S3cretValue9'", *secrets]
        commands = ["".join(rng.choice(words) for _ in range(rng.randint(2, 12))) for _ in range(3000)]
        for cmd in commands:
            for text in (json.dumps({"command": cmd}), cmd):
                new_out = _scrub("tool_call", text)
                with mock.patch.object(worker_transcript, "at_fold_flag_secrets", return_value=[]):
                    old_out = _scrub("tool_call", text)
                with self.subTest(cmd=cmd, form=text[:1]):
                    for secret in secrets:
                        if secret not in old_out:
                            self.assertNotIn(secret, new_out)

    def test_a_tool_calls_prose_argument_keeps_its_diagnosis(self):
        # A worker reports through `kanban_complete(result=...)`: prose, as a reply is.
        report = "## Why\n- Secret: db-credentials is missing the key 'password'\n- Token: expired at 10:00\n"
        out = json.loads(_scrub("tool_call", json.dumps({"result": report, "summary": "Secret: key missing"})))
        # Prose keeps its diagnosis past the redactor's first word and the one
        # word the bearer rule takes, as a reply does.
        self.assertIn("missing the key 'password'", out["result"])
        self.assertIn("10:00", out["result"])
        self.assertNotIn("db-credentials", out["result"])

    def test_text_line_value_is_masked_to_its_end_whatever_it_holds(self):
        # A tool result's line is text: a shell operator in the value is not a stop.
        for line in ("DB_PASSWORD: my secret | phrase", "password: one two; echo done", "password: one 'two && three' four"):
            with self.subTest(line=line):
                self.assertEqual(_scrub("tool_result", line), f"{line.split(':')[0]}: {MARKER}")

    def test_reply_prose_keeps_its_diagnosis(self):
        out = _scrub("reply", "Secret: db-credentials is missing the key 'password'")
        self.assertIn("missing the key 'password'", out)
        self.assertNotIn("db-credentials", out)

    def test_kubectl_env_source_row_keeps_its_reference(self):
        for source in ("in secret 'db'", "of config map 'app'"):
            for tail in ("  Optional: false", "  Optional: true", ""):
                row = f"    Environment:\n      DB_PASSWORD:  <set to the key 'password' {source}>{tail}\n      LOG_LEVEL:  debug\n"
                with self.subTest(row=row):
                    out = _scrub("tool_result", row)
                    self.assertIn(f"to the key 'password' {source}>{tail}\n", out)
                    self.assertIn("LOG_LEVEL:  debug", out)
                    self.assertNotIn(f"{MARKER} {MARKER}", out)

    def test_already_masked_quoted_values_keep_their_quotes(self):
        fragment = '{"password": "hunter2", "user": "app"} and more'
        out = _scrub("tool_result", fragment)
        self.assertEqual(json.loads(out.split(" and more")[0]), {"password": MARKER, "user": "app"})
        self.assertEqual(_scrub("tool_result", 'password: "hunter2"\nuser: app'), f'password: "{MARKER}"\nuser: app')
        command = 'kubectl patch secret db -p \'{"stringData":{"password":"x"}}\''
        out = json.loads(_scrub("tool_call", json.dumps({"command": command})))["command"]
        self.assertNotIn('"x"', out)
        body = out[out.index("-p '") + 4 : -1]
        self.assertEqual(json.loads(body), {"stringData": {"password": MARKER}})

    def test_json_data_object_is_blanked_by_shape(self):
        # kubectl's JSON is key-sorted: a Secret cut short inside `data` has
        # no `kind` to find and does not parse.
        cut = '{"apiVersion": "v1", "data": {"config.yaml": "YmFzZTY0ZGF0YQ==", "kubeconfig": "YXBpVmVyc2lvbg=="'
        out = _scrub("tool_result", cut)
        self.assertNotIn("YmFzZTY0", out)
        self.assertNotIn("YXBpVmVy", out)
        self.assertIn(f'"config.yaml": "{MARKER}", "kubeconfig": "{MARKER}', out)
        # A parsed ConfigMap document is treated like the YAML form.
        cm = json.dumps({"apiVersion": "v1", "data": {"CONN_STRING": "Server=db;Uid=app;Pwd=hunter2x", "LOG_LEVEL": "debug"}, "kind": "ConfigMap", "metadata": {"name": "app"}})
        parsed = json.loads(_scrub("tool_result", cm))
        self.assertEqual(parsed["data"], {"CONN_STRING": MARKER, "LOG_LEVEL": MARKER})
        self.assertEqual(parsed["metadata"], {"name": "app"})
        self.assertEqual(parsed["kind"], "ConfigMap")
        # An API response's `data` is not a manifest's.
        api = json.dumps({"status": "success", "data": {"resultType": "vector", "result": []}})
        self.assertEqual(json.loads(_scrub("tool_result", api))["data"]["resultType"], "vector")
        # A whole `{"data": {base64 map}}` with no manifest round it is a Secret
        # filtered to its data: blanked by shape, as its text form is.
        wrapped = json.dumps({"data": {"username": "YWRtaW4=", "kubeconfig": "YXBpVmVyc2lvbg=="}})
        out = _scrub("tool_result", wrapped)
        self.assertNotIn("YWRtaW4", out)
        self.assertNotIn("YXBpVmVy", out)
        self.assertEqual(json.loads(out), {"data": {"username": MARKER, "kubeconfig": MARKER}})

    def test_json_data_without_a_manifest_marker_is_kept_in_text(self):
        # Two JSON log lines do not parse as one document; a `data` object
        # with no manifest marker in the text is a log's or an API's.
        logs = '{"level":"info","data":{"user":"bob","action":"login"}}\n{"level":"info","data":{"user":"ann","action":"logout"}}'
        self.assertEqual(_scrub("tool_result", logs), logs)
        envelope = 'HTTP/1.1 200 OK\n\n{"status":"success","data":{"resultType":"vector","result":[]}}'
        self.assertEqual(_scrub("tool_result", envelope), envelope)
        # A marker on an earlier line is that line's document, not a later one's.
        mixed = '{"kind":"Event","apiVersion":"audit.k8s.io/v1","verb":"get"}\n{"level":"info","data":{"user":"bob","action":"login"}}'
        self.assertEqual(_scrub("tool_result", mixed), mixed)
        command = "kubectl patch cm app -p '{\"data\":{\"LOG_LEVEL\":\"debug\"}}'"
        self.assertEqual(json.loads(_scrub("tool_call", json.dumps({"command": command})))["command"], command)
        # Cut short before `kind`, a Secret still carries `apiVersion`.
        cut = '{"apiVersion": "v1", "data": {"config.yaml": "YmFzZTY0ZGF0YQ==", "kubeconfig": "YXBpVmVyc2lvbg=="'
        self.assertNotIn("YmFzZTY0", _scrub("tool_result", cut))

    def test_file_name_key_keeps_its_diagnostic_value(self):
        for line in ("tls.crt:  1679 bytes", "cert.pem: OK", "cert.pem:28:-----END"):
            with self.subTest(line=line):
                self.assertEqual(_scrub("tool_result", line), line)

    def test_prose_that_names_a_credential_field_is_kept(self):
        # `Note:` names no credential and `password` is a word in its value, not a key.
        text = "Note: the password field is required"
        self.assertEqual(_scrub("reply", text), text)

    def test_ordinary_output_is_left_alone(self):
        text = "NAME READY STATUS\ncheckout-7d9f 0/1 CrashLoopBackOff\nservice token-refresher ok"
        self.assertEqual(_scrub("tool_result", text), text)

    def test_non_credential_suffixes_come_from_the_redactors_module(self):
        # The list is read from the module the redactor class was loaded from,
        # whatever name the caller registered it under.
        self.assertTrue(worker_transcript.is_non_credential_key_suffix(REDACTOR, "secret-name"))
        self.assertFalse(worker_transcript.named_value_is_sensitive(REDACTOR, "secretName"))
        self.assertTrue(worker_transcript.named_value_is_sensitive(REDACTOR, "client-key-data"))


class TestSecretReadDetector(unittest.TestCase):
    WITHHELD_COMMANDS = [
        "kubectl get secret db -o jsonpath='{.data.password}' | base64 -d",
        "kubectl get secret db -o=go-template='{{.data.password}}'",
        "kubectl get secret db --output=go-template='{{index .data \"password\"}}'",
        "kubectl get secret db -o json | jq -r '.data.password | @base64d'",
        "kubectl get secret db -o yaml | yq -r '.data.password | @base64d'",
        "kubectl -n prod get secrets db --template '{{.data.token}}'",
        "kubectl get secret db -ojson",
        "kubectl get -n prod secret db -o go-template='{{.data.password}}' | base64 -d",
        "kubectl get --namespace prod secret db -o yaml",
        "kubectl get -o go-template='{{.data.password}}' secret db",
        "kubectl -n ns -o go-template='{{.data.password}}' get secret db",
        "kubectl --template='{{index .data \"password\"}}' get secret db",
        "kubectl get -o json secret db",
        "kubectl get secret db \\\n  -o go-template='{{.data.password}}' | base64 -d",
        "kubectl \\\n  get secret db -o custom-columns=:.data.password",
        "kubectl get secret db \\\n  -o json | jq -r '.data.password | @base64d'",
        "cat \\\n  /var/run/secrets/kubernetes.io/serviceaccount/token",
        "kubectl get secret db -o'go-template={{.data.password}}'",
        'kubectl get secret db -o"custom-columns=:.data.password"',
        "kubectl get secret db -o$FMT",
        "kubectl get secret db -owide",
        "gcloud --project=prod secrets versions access latest --secret=db",
        "gcloud beta secrets versions access latest --secret=db",
        "aws --region us-east-1 secretsmanager get-secret-value --secret-id db --query SecretString --output text",
        "aws --region us-east-1 ssm get-parameter --name /prod/db --with-decryption",
        "az keyvault secret show --vault-name kv --name db",
        "sops -d secrets.enc.yaml",
        "sops --decrypt secrets.enc.yaml",
        "kubectl exec deploy/api -- cat /etc/db-creds/password",
        "kubectl exec -n prod api-0 -- head -c 64 /var/secrets/token",
        # an output flag the old list also caught, kept as the grammar's refusal
        "kubectl get secret db -o name",
        "kubectl get secret db -o wide",
        "kubectl get secret db --output jsonpath-as-json={.data}",
        "kubectl get secret db -ojsonpath={.data.password}",
        "kubectl get secret db -o yaml > /tmp/s.yaml",
        "echo $(kubectl get secret db -o json)",
        "kubectl version; kubectl get secret db -o json",
        "kubectl exec api-0 -- sh -c 'kubectl get secret db -o json'",
        "sh -c 'kubectl get secret db -o json'",
        # forms no list named: the grammar fails closed on anything that is
        # not provably the bare list
        "kubectl get secret db -n prod --some-flag-added-next-release",
        "kubectl --unknown-global-flag x get secrets -n prod",
        "kubectl get secrets,configmaps -n prod",
        "kubectl get secrets -n \"prod",
        "echo `kubectl get secrets -n prod`",
        "kubectl get secrets.v1. db -o yaml",
        # a variable expands to words, flags included, so it is not admitted
        # beside a secret resource
        "NS='prod -o yaml'; kubectl get secrets -n $NS",
        "kubectl get secrets -n $NS",
        "for ns in a b; do kubectl get secrets -n $ns; done",
        # a substitution before the binary on the same command, or the
        # binary stored in a variable a later command runs
        "echo $(kubectl get secret db -o json) kubectl get secrets",
        "FOO=$(kubectl get secret db -o json) kubectl get secrets -n prod",
        "KUBECTL=/usr/bin/kubectl; $KUBECTL get secret db -o json",
        "KUBECTL=/usr/bin/kubectl",
        "$KUBECTL get secret db -o json",
        "${KUBECTL} get secret db -o json",
        "k=kubectl; $k get secret db -o json",
        "k=\"kubectl\"; $k get secret db -o json",
        "k='/usr/bin/kubectl'; $k get secret db -o json",
        "\"kubectl\" get secret db -o json",
        "\"/usr/bin/kubectl\" get secret db -o json",
        # the word inside a quoted script after an assignment, or inside a
        # substitution in an argument
        "kubectl exec api-0 -- sh -c 'FOO=1; kubectl get secret db -o json'",
        "kubectl exec api-0 -- sh -c 'KUBECONFIG=/tmp/k kubectl get secret db -o json'",
        "kubectl get pods -l app=$(kubectl get secret db -o json)",
        "kubectl -l app=\"$(kubectl get secret db -o json)\" get pods",
        # a secret among several positionals
        "kubectl get pod/api-0 secret/db -o json",
        "kubectl get configmap/app-cfg secret/db-creds -o yaml",
        "kubectl get pod api-0 secret/db -o json",
        "kubectl get pods -n prod -f x.yaml",
        # the resource is not on the line, or is not a word
        "printf secret/db | xargs kubectl get -n prod -o yaml",
        "kubectl get secrets -n prod -o name | xargs kubectl get -o yaml",
        "kubectl get -o yaml -f db.yaml",
        "kind=secret; kubectl get $kind db -o yaml",
        "kubectl get -o yaml $(cat /tmp/names)",
        "kubectl get --raw /api/v1/namespaces/prod/secrets/db",
        "kubectl view-secret db -n prod",
        # a store's command that is not one of its named lists, alone and
        # fed by the store's own list through a pipe
        "aws secretsmanager batch-get-secret-value --secret-id-list db",
        "aws ssm get-parameters-by-path --path /prod --with-decryption",
        "aws ssm get-parameter-history --name /prod/db --with-decryption",
        "gcloud secrets versions access 3 --secret db --format=json",
        "az keyvault secret download --vault-name kv --name db --file x",
        "vault kv get -field=password secret/db",
        "vault read secret/db",
        "sops prod.enc.yaml",
        "gcloud secrets list --format='value(name)' | xargs -I{} gcloud secrets versions access latest --secret={}",
        "aws secretsmanager list-secrets --query 'SecretList[].Name' --output text | xargs -n1 aws secretsmanager get-secret-value --secret-id",
        "vault kv list -format=json secret/ | jq -r '.[]' | xargs -I{} vault kv get secret/{}",
        "az keyvault secret list --vault-name kv -o tsv | xargs -I{} az keyvault secret show --vault-name kv --name {}",
        "aws ssm describe-parameters | aws ssm get-parameter --name /prod/db --with-decryption",
        # the same inside a quoted script, twice on one command, or by path
        "bash -c 'gcloud secrets list | xargs -I{} gcloud secrets versions access latest --secret={}'",
        "sh -c 'vault kv list secret/; vault kv get -field=password secret/db'",
        "echo $(gcloud secrets versions access latest --secret=db) $(gcloud secrets list)",
        "echo $(vault kv list secret/) $(vault kv get secret/db)",
        "sh -c 'sops -d secrets.enc.yaml'",
        "/usr/local/bin/sops -d secrets.enc.yaml",
        # the list first, a flag between the family and its subcommand
        "echo $(gcloud secrets list) $(gcloud secrets versions access latest --secret=db)",
        "echo `gcloud secrets list` `gcloud secrets versions access latest --secret=db`",
        "bash -c 'echo $(gcloud secrets list) $(gcloud secrets versions access latest --secret=db)'",
        "aws ssm --region us-east-1 get-parameter --name /prod/db --with-decryption",
        "vault -namespace=prod kv get secret/db",
        "vault -address=https://vault:8200 read secret/db",
        # verbosity prints the response body; xargs supplies what the line
        # does not show; an attached or stacked file flag with `=`
        "kubectl get secrets -n prod --sort-by=.metadata.name -v=9 2>&1",
        "kubectl get secrets -n prod -v=6",
        "kubectl get secrets -n prod --no-headers | awk '{print \"secret/\" $1}' | xargs -I item kubectl get item -o yaml",
        "printf 'secret/db\\n' | xargs -I res kubectl get res -o json",
        "printf 'secret/db\\n' | xargs kubectl get pod/api-0 -o yaml",
        "printf -- '-o\\nyaml\\n' | xargs kubectl get secret db",
        "kubectl get pods -fdb=1.yaml -o yaml",
        "kubectl get pods -Rf=db.yaml -o yaml",
        "kubectl --unknown-global-flag=x get pods",
    ]
    SERVED_COMMANDS = [
        "kubectl get secrets -n kube-system",
        "kubectl describe secret db",
        "kubectl get pods -o json | jq '.items[].metadata.name'",
        "kubectl get secrets -n kube-system --show-labels",
        "gcloud secrets list --project=prod",
        "aws secretsmanager list-secrets --region us-east-1",
        "aws ssm describe-parameters",
        "az keyvault secret list --vault-name kv",
        "kubectl exec deploy/api -- cat /opt/data/report.txt",
        "kubectl exec deploy/secrets-api -- cat /opt/data/report.txt",
        # the bare list in every spelling the grammar admits
        "kubectl get secret db -n prod --ignore-not-found",
        "kubectl -n prod get secrets -l app=db --no-headers",
        "kubectl get secrets -A 2>/dev/null",
        "kubectl get secrets -n prod 2>&1",
        "kubectl get secrets -nprod",
        "kubectl get secret/db -n prod",
        "kubectl get secrets.v1. -n prod",
        "kubectl get -A secrets",
        "kubectl --context prod get secrets --show-labels=true",
        "kubectl --token abc123 --server https://10.0.0.1 --insecure-skip-tls-verify get secrets -n prod",
        "KUBECONFIG=/tmp/k kubectl get secrets --all-namespaces",
        "kubectl get secrets -n prod --sort-by=.metadata.name",
        "kubectl get secrets -n prod -L app",
        "kubectl get secrets -n prod -w",
        "kubectl get secrets -n prod && echo ok",
        "kubectl get secrets -n prod || true",
        "bash -c 'kubectl get secrets -n prod'",
        # the bare list piped anywhere is still the bare list; the consumer
        # is read as its own command
        "kubectl get secrets -n prod | wc -l",
        "kubectl get secrets -n prod | grep db",
        "kubectl get pods -o name | xargs kubectl describe pod",
        # another kind by name, whatever the flags, the namespace or the
        # object's name say
        "kubectl get pods -n external-secrets",
        "kubectl get deploy -n sealed-secrets",
        "kubectl get pods -l app.kubernetes.io/name=external-secrets",
        "kubectl get pod secret-rotator-7d9f -n prod",
        "kubectl get events -n prod --field-selector involvedObject.name=db-secret",
        "kubectl get pods -n $NS",
        "kubectl get pods -n prod -l 'env in (prod,stage)'",
        "kubectl get externalsecrets -n prod -o yaml",
        "bash -c 'kubectl get pods -n prod | grep -v Running'",
        "bash -c 'KUBECONFIG=/tmp/k kubectl get pods -n prod'",
        "kubectl exec deploy/api -- sh -c 'ps aux | grep java'",
        "kubectl get pod secret",
        # a wrapper or keyword ahead of the binary runs the command as given
        "sudo kubectl get secrets -n prod",
        "timeout 30 kubectl get secrets -n prod",
        "nice -n 10 kubectl get secrets -n prod",
        "for ns in a b; do kubectl get pods -n $ns; done",
        "kubectl get pods -n vault -o wide",
        "vault -namespace=prod kv list secret/",
        "aws ssm --region us-east-1 describe-parameters",
        # a name or label that contains the letters is not a second binary
        "kubectl get pods -n prod -l app=kubectl-proxy",
        "kubectl get pods -n prod -l app=kubectl",
        "kubectl get deploy kubectl-runner -n tools",
        "kubectl get pods -n kubectl-test",
        "kubectl get secrets -n kubectl-test",
        "kubectl describe pod kubectl-proxy-7d9f",
        "kubectl logs deploy/kubectl-runner",
        "kubectl apply -f kubectl-rbac.yaml",
        "KUBECTL_VERSION=1.30 kubectl get pods",
        # the word, not the store: a namespace named vault, a config file
        # named for sops
        "kubectl get pods -n vault",
        "cat .sops.yaml",
        # a store's named lists
        "gcloud secrets versions list --secret=db",
        "aws secretsmanager describe-secret --secret-id db",
        "aws ssm start-session --target i-1",
        "vault kv list secret/",
        "vault kv metadata get secret/db",
        "sops --version",
    ]

    def test_covers_every_output_form(self):
        for cmd in self.WITHHELD_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertTrue(worker_transcript.is_secret_read_tool_call(json.dumps({"command": cmd})))
        for cmd in self.SERVED_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertFalse(worker_transcript.is_secret_read_tool_call(json.dumps({"command": cmd})))

    def test_bare_list_grammar_decides_closed_where_it_cannot_tell(self):
        # The grammar, not a list of spellings, is what serves a result: a
        # token it does not admit, a variable, a quoting error, or two
        # `kubectl` words on one command all refuse, whatever the token means.
        self.assertTrue(worker_transcript.is_kubectl_bare_secret_list("kubectl get secrets -n prod -l app=db -A --no-headers 2>&1"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("kubectl get secrets -n prod --no-headers=maybe"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("kubectl get secrets -n"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("kubectl get secrets -n 'prod"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("kubectl get secrets -n $NS"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("kubectl get secrets -n prod kubectl get secret db -o json"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("kubectl get db secret"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("kubectl describe secret db"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("echo 'kubectl get secret db -o json'"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("xargs kubectl get secret db"))
        self.assertFalse(worker_transcript.is_kubectl_bare_secret_list("kubectl get secrets -n prod -v=9"))
        self.assertTrue(worker_transcript.is_kubectl_bare_secret_list("sudo kubectl get secrets -n prod"))

    def test_resource_reader_withholds_what_it_cannot_tell_and_serves_another_kind(self):
        reads = worker_transcript.kubectl_command_reads_a_secret
        self.assertFalse(reads("kubectl get pods -n prod -o json"))
        self.assertFalse(reads("kubectl describe secret db"))
        self.assertFalse(reads("xargs kubectl describe pod"))
        self.assertTrue(reads("xargs kubectl get -n prod -o yaml"))
        self.assertTrue(reads("kubectl get -f db.yaml"))
        self.assertTrue(reads("kubectl get $kind db"))
        self.assertTrue(reads("kubectl --flag-the-reader-does-not-know x get pods"))
        self.assertTrue(reads("kubectl get secrets -n prod -o wide"))
        self.assertFalse(reads("kubectl get secrets -n prod"))
        self.assertFalse(reads("echo kubectl"))

    def test_shell_line_is_cut_at_unquoted_separators_only(self):
        split = worker_transcript.split_shell_commands
        self.assertEqual(split("a 'b;c' | d && e 2>&1 & f || g \"h|i\""), ["a 'b;c'", "d", "e 2>&1", "f", 'g "h|i"'])
        self.assertEqual(split("kubectl get secrets -n prod 2>/dev/null"), ["kubectl get secrets -n prod 2>/dev/null"])
        self.assertEqual(split("x \\; y"), ["x \\; y"])

    def test_reads_every_argument_shape(self):
        cmd = self.WITHHELD_COMMANDS[0]
        self.assertTrue(worker_transcript.is_secret_read_tool_call({"cmd": cmd}))
        self.assertTrue(worker_transcript.is_secret_read_tool_call(cmd))
        self.assertFalse(worker_transcript.is_secret_read_tool_call({"path": cmd}))
        self.assertFalse(worker_transcript.is_secret_read_tool_call(None))


class TestCardToSession(unittest.TestCase):
    def test_profile_state_db_path(self):
        root = Path("/opt/data")
        self.assertEqual(worker_transcript.profile_state_db_path(root, "default"), root / "state.db")
        self.assertEqual(worker_transcript.profile_state_db_path(root, ""), root / "state.db")
        self.assertEqual(worker_transcript.profile_state_db_path(root, None), root / "state.db")
        self.assertEqual(
            worker_transcript.profile_state_db_path(root, "cluster-abc"),
            root / "profiles" / "cluster-abc" / "state.db",
        )
        self.assertEqual(worker_transcript.profile_state_db_path("/opt/data", "p"), root / "profiles" / "p" / "state.db")

    def test_worker_prompt_exact_re_stops_at_the_id(self):
        exact = worker_transcript.worker_prompt_exact_re("t_front01")
        self.assertTrue(exact.search("work kanban task t_front01"))
        self.assertTrue(exact.search("please work kanban task t_front01, then stop"))
        self.assertFalse(exact.search("work kanban task t_front012"))
        self.assertFalse(exact.search("work kanban task t_front01-retry"))
        self.assertFalse(exact.search("work kanban task t_front0"))

    def test_worker_prompt_task_re_finds_every_id(self):
        content = "work kanban task t_a1 and work kanban task t_b2-x; not work kanban task"
        self.assertEqual(worker_transcript.WORKER_PROMPT_TASK_RE.findall(content), ["t_a1", "t_b2-x"])

    def test_stamped_worker_sessions_keeps_run_order_and_each_session_once(self):
        runs = [
            {"profile": "platform", "metadata": json.dumps({"worker_session_id": "s-1"})},
            {"profile": None, "metadata": json.dumps({"worker_session_id": "s-2"})},
            {"profile": "platform", "metadata": json.dumps({"worker_session_id": "s-1"})},
            {"profile": "platform", "metadata": None},
            {"profile": "platform", "metadata": "not json"},
            {"profile": "platform", "metadata": json.dumps({"worker_session_id": ""})},
            {"profile": "platform", "metadata": json.dumps(["list"])},
            {"profile": "cluster-abc"},
        ]
        self.assertEqual(
            worker_transcript.stamped_worker_sessions(runs, "assignee-p"),
            ([("s-1", "platform"), ("s-2", "assignee-p")], True),
        )
        self.assertEqual(worker_transcript.stamped_worker_sessions(runs[:2], "x"), ([("s-1", "platform"), ("s-2", "x")], False))
        self.assertEqual(worker_transcript.stamped_worker_sessions([], "x"), ([], False))
        self.assertEqual(
            [worker_transcript.run_worker_session_id(r) for r in runs],
            ["s-1", "s-2", "s-1", None, None, None, None, None],
        )

    def test_stamped_worker_sessions_reads_sqlite_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, profile TEXT, metadata TEXT)")
        conn.executemany(
            "INSERT INTO task_runs (profile, metadata) VALUES (?, ?)",
            [
                ("platform", json.dumps({"worker_session_id": "s-9"})),
                (None, json.dumps({"worker_session_id": "s-8"})),
                ("platform", None),
            ],
        )
        rows = conn.execute("SELECT * FROM task_runs ORDER BY id").fetchall()
        self.assertEqual(
            worker_transcript.stamped_worker_sessions(rows, "cluster-x"),
            ([("s-9", "platform"), ("s-8", "cluster-x")], True),
        )


if __name__ == "__main__":
    unittest.main()

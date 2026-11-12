"""Tests for `worker_transcript`, the shared transcript rules, against the repo's redactor.

The session-kv server's tests exercise the same rules through the feed's
wrappers and routes; these call the module's public API directly with the
redactor loaded by path, as the bench's in-pod script loads it.
"""

import importlib.util
import json
import shlex
import sqlite3
import sys
import time
import unittest
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

    # Eleven long lines read in about a second of CPU time or less each; the bound leaves room for a slow runner.
    LONG_LINE_SECONDS = 20

    # A command that prints a token or the environment (#5484).
    TOKEN_AND_ENVIRONMENT_PRINTS = [
        "gcloud auth print-access-token",
        "gcloud auth application-default print-access-token",
        "gcloud auth print-identity-token --audiences=https://x",
        "/opt/credential-proxy/bin/gcloud auth print-access-token --project p",
        "T=$(gcloud auth print-access-token)",
        "gcloud auth describe a@b.c",
        "gcloud config config-helper --format=json",
        "env",
        "env | sort",
        "env | grep PATH",
        "env -0",
        "env -u HOME",
        "env 2>/dev/null",
        "/usr/bin/env",
        "FOO=1 env",
        "sudo env",
        "sudo -u root env",
        "env FOO=1 printenv",
        "bash -c env",
        "printenv",
        "printenv GITHUB_TOKEN",
        "printenv DB_PASSWORD HOME",
        "export",
        "export -p",
        "declare -p",
        "declare -px",
        "typeset -p",
        "declare -p API_KEY",
        "set",
        "set | grep X",
        "cat /proc/self/environ",
        "tr '\\0' '\\n' < /proc/1/environ",
        "strings /proc/$$/environ",
        "kubectl exec deploy/api -- env",
        "kubectl exec deploy/api -- printenv",
        # the same commands in other spellings: a quoted or repeated group
        # word, a redirect or a flag between the words
        'gcloud "auth" print-access-token',
        'gcloud --trace-token "auth list" auth print-access-token',
        "gcloud 2>&1 auth print-access-token",
        "gcloud config --format=json config-helper",
        "env FOO=1 gcloud auth print-access-token",
        # behind a word that runs a command, in a subshell or a substitution
        "bash -lc env",
        "sh -ec env",
        "(env)",
        "{ env; }",
        "if true; then env; fi",
        "! env",
        "echo $(env)",
        "E=$(env); echo $E",
        "echo `env`",
        "eval env",
        "nice env",
        "timeout 5 env",
        "command -p env",
        "time -p env",
        "sudo --user root env",
        "env -S env",
        "env>&2",
        "env</dev/null",
        "env &",
        "env # it's fine",
        "kubectl exec p env",
        "timeout 30 kubectl exec p -- env",
        "docker exec c env",
        # a name that is an expansion, or credential-shaped in another word
        "N=GITHUB_TOKEN; printenv $N",
        "printenv GH_PAT",
        "printenv MYSQL_PWD",
        "printenv DATABASE_URL",
        # other paths to a process's environment
        "cat /proc/self/task/1/environ",
        "cat //proc/self/environ",
        "cat /proc/self/./environ",
        "cat /proc/self/env*",
        "cd /proc/self && cat environ",
        "cat /proc/self/e*",
        "tr '\\0' '\\n' < environ",
        # a substitution inside double quotes, which runs all the same
        'echo "$(env)"',
        'Y="$(printenv)"; echo "$Y"',
        'echo "$(set)"',
        'curl -H "Authorization: Bearer $(gcloud auth print-access-token)" https://x',
        "diff <(env) <(printenv)",
        # a `#` inside a word, an escaped redirect operator, a here-string
        "Y=a#b env",
        "kubectl exec p#1 -- env",
        "Y=\\> env",
        "bash <<< env",
        "cat '#' <(env)",
        # a flag between the gcloud words, an empty word, a `gcloud` word that is an argument
        "gcloud auth --format list print-access-token",
        "gcloud config --format json config-helper",
        'gcloud --trace-token "" auth print-access-token',
        "gcloud beta auth print-access-token",
        "exec -a gcloud env",
        "kubectl -n gcloud exec p -- env",
        # a command word nobody can read, ahead of a token printer
        '"$GCLOUD" auth print-access-token',
        "$(which gcloud) auth print-access-token",
        # the word after `-c` is the whole script, whatever follows it
        "sh -c env arg0",
        "bash -c env _",
        "kubectl exec p -- sh -c env x",
        "kubectl exec p -- /bin/sh -c env",
        # other words that run a command
        "busybox env",
        "doas env",
        "oc exec p -- env",
        "ssh host -- env",
        "su - user -c env",
        "docker compose exec svc env",
        "kubectl debug node/x -it --image=busybox -- chroot /host env",
        "find . -exec env \\;",
        "function f { env; }; f",
        "trap env EXIT",
        "echo GITHUB_TOKEN | xargs printenv",
        "echo GITHUB_TOKEN | xargs -I{} printenv {}",
        "echo GITHUB_TOKEN | parallel printenv",
        # a quoted script behind a word that runs one
        "sh -c 'env | sort'",
        'bash -lc "printenv | grep KUBE"',
        "ssh host 'env | grep X'",
        'eval "env | sort"',
        "eval '' declare -p",
        "x=1 eval set ''",
        # a backslash that ends the command
        "set \\",
        "export -p \\",
        "env env<<<\\ ",
        "watch 'env | sort'",
        "su - user -c 'env | sort'",
        "runuser -l app -c 'env | sort'",
        "script -qc 'env | sort' /dev/null",
        "flock /tmp/l -c 'env | sort'",
        "parallel 'printenv | grep {}' ::: A",
        "watch -n 1 env",
        "trap 'env > /tmp/x' EXIT",
        'bash <<< "env | sort"',
        "kubectl exec p -- sh -c 'env | sort'",
        # the script word of a shell: behind `--`, with a redirect inside, as a here-string
        "bash -c -- env",
        'sh -c "ls>/dev/null;env"',
        "env<<<x",
        "zsh -c env",
        "dash -c env",
        "ksh -c env",
        # a `(` behind shell keywords opens a command
        "if (env); then :; fi",
        "{ (env); }",
        "time (env)",
        # an assignment whose value holds a blank
        'env "FOO=a b"',
        'FOO="a b" env',
        # each prefix word, each word that runs a command elsewhere
        "builtin export",
        "ionice -c 3 env",
        "stdbuf -o0 env",
        "setsid env",
        "strace -f env",
        "unshare -m env",
        "flock /tmp/l env",
        "nsenter -t 1 -m env",
        "chroot /host /usr/bin/env",
        "podman exec c env",
        "nerdctl exec c env",
        "crictl exec c env",
        "docker run --rm img env",
        "find / -execdir env \\;",
        "find . -ok env \\;",
        "find . -okdir env \\;",
        # each declaring builtin, each option of `env` that takes a value
        "readonly",
        "readonly -p",
        "typeset",
        "env --unset FOO",
        "env -C /tmp",
        "env --chdir /tmp",
        # each credential-shaped part of a name
        "printenv X_SECRET",
        "printenv AWS_CRED",
        "printenv BASIC_AUTH",
        "printenv SESSION_COOKIE",
        "printenv SENTRY_DSN",
        "printenv DB_PASS",
        'printenv "$(echo X)"',
        # a token printer behind a wrapper nobody named, other `gcloud auth` subcommands
        "gtimeout 5 gcloud auth print-access-token",
        "kubectl exec p -- gcloud auth print-access-token",
        "gcloud auth",
        "gcloud auth git-helper get",
        "gcloud auth --format list git-helper get",
        "gcloud --project p auth describe a@b",
        "gcloud -q beta auth docker-helper get",
        "gcloud beta auth describe a@b",
        "gcloud alpha auth describe a@b",
        "gtimeout 5 gcloud auth print-identity-token",
        "gcloud auth application-default describe",
        # a process's environment: a substitution or a variable in the path, another language, each file reader
        "cat /proc/$(pgrep -f hermes)/environ",
        "cat /proc/1/*",
        "cat /proc/self/$F",
        "cat /proc/self/e?viron",
        "cat /proc/self/[e]nviron",
        "cat /proc/self/{e,}nviron",
        "cat $P/environ",
        "python3 -c \"print(open('/proc/self/environ').read())\"",
        "cd /proc/1; head -c 100 environ",
        "cd /proc/1; tail environ",
        "cd /proc/1; less environ",
        "cd /proc/1; more environ",
        "cd /proc/1; base64 environ",
        "cd /proc/1; strings environ",
        "cd /proc/1; xxd environ",
        "cd /proc/1; od -c environ",
        # text with an unclosed quote is read line by line
        "cat > note <<EOF\nIt's a note.\nEOF\nenv",
        "cat > n.txt <<EOF\nDon't forget\nEOF\ntime env",
        "cat > n.txt <<EOF\nDon't forget\nEOF\nkubectl exec p -- sh -c 'env | sort'",
        # a comment hides no command, and an apostrophe in one opens no quote
        "env # print environment",
        "printenv # all",
        "export -p # list",
        "sudo env # dump",
        "kubectl exec pod -- env  # show env vars",
        "env;#x",
        "# Pod's env\nenv\n# that's all",
        "# it's\nif true; then env; fi",
        "# don't\nsudo env",
        "# Don't print secrets\nkubectl exec p -- sh -c 'env | grep -v SECRET'",
        'echo "a # b"; env',
        # a comment ends at its line's end even behind a backslash; a `#` behind a continuation inside a word is no comment
        "# note \\\nenv",
        "echo x # note \\\nenv",
        "echo a\\\n#b; env",
        "echo a \\\n# note\nenv",
        # backticks inside double quotes run, and a script word's own substitutions are read
        'git commit -m "add `env` here"',
        "sh -c '\"$(env)\"'",
        "sh -c '`env`'",
        "`which gcloud` auth print-access-token",
        # the word after `-c` of other words, and the command `gcloud compute ssh` runs
        "script -qc env /dev/null",
        "flock /tmp/l -c env",
        "runuser -l app -c env",
        "runuser -l app -c 'env;ls'",
        "flock /tmp/l -c 'env;ls'",
        "runuser -u app -- env",
        "docker-compose exec svc env",
        "gcloud compute ssh vm --command=env",
        "gcloud compute ssh vm --command env --zone z",
        "gcloud compute ssh vm -- env",
        "gcloud compute ssh vm --command 'env | sort'",
        'gcloud compute ssh vm --command="env | sort"',
        "gcloud compute ssh vm --command='docker exec app env'",
        # a substitution whose `$` or backtick is escaped inside a double-quoted script
        'bash -c "echo \\"\\$(printenv)\\""',
        'kubectl exec p -- sh -c "echo \\"\\$(env)\\""',
        'ssh host "curl -H \\"Authorization: Bearer \\$(gcloud auth print-access-token)\\" https://x"',
        'bash -c "echo \\`env\\`"',
        # a function's head opens its body
        "f() { env; }; f",
        "f () { env; }; f",
        # text with an unclosed quote: each line's substitutions are read
        "cat > n.md <<'EOF'\nIt's a note.\nEOF\necho \"$(env)\"",
        "cat > n.md <<'EOF'\nIt's a note.\nEOF\nTOKEN=\"$(gcloud auth print-access-token)\"",
        "cat > n.md <<'EOF'\nIt's a note.\nEOF\necho `env`",
        # a glob behind a process's directory in each spelling
        "cat /proc/thread-self/e*",
        "cat /proc/*/e*",
        "cat /proc/$PID/e*",
    ]
    # The same words where nothing of the environment is printed.
    NO_TOKEN_OR_ENVIRONMENT_PRINTS = [
        "gcloud auth list",
        "gcloud auth login --no-launch-browser",
        "gcloud auth activate-service-account --key-file=k.json",
        "gcloud auth configure-docker us-docker.pkg.dev",
        "gcloud auth application-default set-quota-project p",
        "echo $(gcloud auth list)",
        "gcloud config list",
        "gcloud config get-value project",
        "gcloud config set auth/impersonate_service_account x",
        "gcloud builds submit --config cloudbuild.yaml .",
        "gcloud container clusters get-credentials c --region r",
        "env FOO=1 python3 x.py",
        "env -i bash -c 'echo hi'",
        "/usr/bin/env python3 script.py",
        "printenv HOME",
        "printenv PATH",
        "export FOO=bar",
        "export PATH",
        "declare -i n=3",
        "declare -p PATH",
        "set -euo pipefail",
        "set -x",
        "set -o pipefail",
        "kubectl exec deploy/api -- ls /opt/env",
        "echo env",
        "grep -rn printenv docs/",
        "git diff -- env",
        "gcloud auth --format=json list",
        "gcloud logging read 'textPayload:\"auth failed\"'",
        "kubectl --kubeconfig ~/.config/gcloud/kc auth can-i list pods",
        "kubectl set env deploy/x K=v",
        "kubectl -n env get pods",
        "command -v env",
        "which env",
        "python3 -m venv env",
        "cat /proc/1/cmdline",
        "helm upgrade x y --set a=b",
        "export USE_GKE_GCLOUD_AUTH_PLUGIN=True gcloud container clusters get-credentials c --location l --project p",
        "export -p PATH",
        # a dump word that is an argument, not a command
        "kubectl get pods -n env",
        "kubectl logs deploy/env",
        "kubectl apply -k overlays/env",
        "kubectl exec p -c env -- ls",
        "kubectl get pods # env",
        "docker run --name env img",
        "sudo -u env ls",
        "git ls-files | xargs grep -n env",
        "find . -type d -name env",
        "grep -rn environ .",
        "cat <<< env",
        "sh -c ls env",
        "trap 'rm -f env' EXIT",
        "function f { ls env; }",
        "printenv PWD",
        "printenv OLDPWD",
        "gcloud container clusters get-credentials c --zone z",
        "kubectl exec p -- ls env",
        "kubectl exec p -- cat /etc/env",
        "kubectl get deploy -o name | xargs -I{} kubectl set env {} A=b",
        "kubectl get deploy -o name | xargs -n1 kubectl set image c=img",
        "ls # (env)",
        # a quoted argument no word runs as a script: prose, another language
        'git commit -m "fix: don\'t set the flag"',
        'git commit -m "fix config (env)"',
        'echo "it\'s set"',
        'grep -E "token | env" notes.md',
        'python3 -c "print(list(set([1, 2])))"',
        "jq '.items[] | select(.metadata.name | test(\"env\"))'",
        'gh pr create --body "' + "word " * 200 + 'set the env and export it"',
        # a heredoc's body: prose with an apostrophe, code that calls `set(` or passes `env`
        "cat > note.md <<EOF\nIt's a note.\nRun the set of checks.\nEOF",
        "cat > f.py <<'EOF'\nprint(set())\nd = dict(env)\nx = json.dumps(env)\nby = collections.defaultdict(set)\nby=collections.defaultdict(set)\nEOF",
        "python3 - <<'EOF'\nprint(set(a) | set(b))\nEOF",
        "python3 - <<'EOF'\nseen = seen | set()\nprint(seen)\nEOF",
        'gcloud compute ssh vm --command="ls -la env"',
        "f() { ls env; }; f",
        'bash -c "echo \\$HOME env"',
        # a line with an unclosed quote: an assignment by `export` is no dump
        "cat > note <<EOF\nIt's a note.\nEOF\nexport PATH=$HOME/bin:$PATH",
        "cat > f.md <<'EOF'\n" + "word " * 200 + "set the env and export it\nEOF",
        # a quoted script that prints nothing of the environment
        "ssh host 'ls -la env'",
        'kubectl exec p -- sh -c "ls /app/env"',
        "bash -c 'echo \"it'\"'\"'s set\"'",
        # `auth` as a resource's name, and the served `gcloud auth` subcommands
        "gcloud run services describe auth --region us-central1",
        "gcloud auth revoke a@b.c",
        "gcloud auth application-default login",
        "gcloud auth application-default revoke",
        "gcloud --project p auth list",
        "gcloud beta auth list",
        # an option of `env` that takes a value, ahead of a command
        "env -C /tmp ls",
        "env --unset FOO ls",
        "env --chdir /tmp ls",
        "env -u FOO ls",
        # paths under /proc that are no process's environment
        "cat /proc/*/comm",
        "ls -d /proc/[0-9]*",
        "cat /proc/$$/status",
        "readlink /proc/self/exe",
        # a redirect target that holds a blank, a number that is an argument
        'echo env > "/tmp/my file"',
        "echo env 2 > f",
        "cat <<< env",
        "declare -f myfunc",
        "command -V env",
        # a comment that names a dump word
        "kubectl get pods  # then run env",
        "echo hi # printenv",
        "# env",
        "echo env#x",
        "echo a # env \\\n",
        "echo a\\\n#b env",
        # backticks and a substitution in single quotes run nothing; a pair is one substitution
        "git commit -m 'fix: add `env` to the deployment'",
        "gh pr comment 1 --body 'the `env` block is wrong'",
        "echo 'With `foo` set `bar` works'",
        "echo With `foo` set `bar` works",
        "echo '$(env)'",
        # globs under /proc that are no process's files
        "cat /proc/pressure/*",
        "cat /proc/sys/fs/inotify/*",
        # words that run a command elsewhere, with another command
        'gcloud compute ssh vm --command "ls -la env"',
        "gcloud compute ssh vm -- ls env",
        "runuser -u app -- ls env",
        "script -q /dev/null ls env",
        "docker-compose up -d env",
    ]

    def test_a_token_print_or_an_environment_dump_is_withheld(self):
        for cmd in self.TOKEN_AND_ENVIRONMENT_PRINTS:
            with self.subTest(cmd=cmd):
                self.assertTrue(worker_transcript.command_reads_a_secret(cmd))
                self.assertTrue(worker_transcript.is_secret_read_tool_call(json.dumps({"command": cmd})))
        for cmd in self.NO_TOKEN_OR_ENVIRONMENT_PRINTS:
            with self.subTest(cmd=cmd):
                self.assertFalse(worker_transcript.command_reads_a_secret(cmd))

    def test_a_long_line_of_wrappers_is_read_without_recursion(self):
        self.assertTrue(worker_transcript.command_reads_a_secret("sudo " * 1200 + "env"))
        self.assertFalse(worker_transcript.command_reads_a_secret("sudo " * 1200 + "ls"))

    def test_a_command_past_the_read_limits_decides_on_less(self):
        # The same command is served when the walk can read it, and withheld
        # on its words alone when the walk costs more than the step limit.
        self.assertFalse(worker_transcript.command_reads_a_secret("sudo kubectl get pods -n env"))
        self.assertTrue(worker_transcript.command_reads_a_secret("sudo " * worker_transcript.MAX_READ_STEPS + "kubectl get pods -n env"))
        self.assertFalse(worker_transcript.command_reads_a_secret("sudo " * worker_transcript.MAX_READ_STEPS + "kubectl get pods"))
        # Past the substitution limits a dump word that starts a command withholds.
        many_substitutions = "echo " + "$(date) " * (worker_transcript.MAX_READ_SUBSTITUTIONS + 1)
        self.assertFalse(worker_transcript.command_reads_a_secret(many_substitutions))
        self.assertFalse(worker_transcript.command_reads_a_secret(many_substitutions + '"$(echo env)"'))
        self.assertTrue(worker_transcript.command_reads_a_secret(many_substitutions + '"$(env)"'))
        long_substitution = 'echo "$(echo ' + "x " * worker_transcript.MAX_READ_SUBSTITUTION_CHARS
        self.assertFalse(worker_transcript.command_reads_a_secret(long_substitution + ')"'))
        self.assertTrue(worker_transcript.command_reads_a_secret(long_substitution + '; env)"'))
        # Text with an unclosed quote: read line by line up to the line limit, then on a dump that starts a line.
        many_lines = "echo 'x" + "\necho env" * (worker_transcript.MAX_READ_LINES - 1)
        self.assertTrue(worker_transcript.command_reads_a_secret(many_lines + "\nsudo env"))
        self.assertFalse(worker_transcript.command_reads_a_secret(many_lines + "\necho env\nsudo env"))
        self.assertTrue(worker_transcript.command_reads_a_secret(many_lines + "\necho env\nenv"))
        # Nested substitutions and quoted words can make one text read many times over: past the call's read budget it withholds.
        nested_reads = "\nenvx y"
        self.assertFalse(worker_transcript.command_reads_a_secret("echo " + shlex.quote(nested_reads)))
        for _ in range(worker_transcript.MAX_NESTED_COMMAND_DEPTH):
            nested_reads = "echo " + "$(" * worker_transcript.MAX_READ_SUBSTITUTIONS + shlex.quote(nested_reads)
        self.assertTrue(worker_transcript.command_reads_a_secret(nested_reads))
        # A script word nested past the depth limit is not read, and withholds.
        nested = "sh<<<x"
        self.assertFalse(worker_transcript.command_reads_a_secret("env=1 " + nested))
        for _ in range(worker_transcript.MAX_NESTED_COMMAND_DEPTH):
            nested = "sh<<<" + shlex.quote(nested)
        self.assertTrue(worker_transcript.command_reads_a_secret("env=1 " + nested))

    def test_the_detector_reads_a_long_line_in_bounded_time(self):
        # Shapes that made a regex or the walk quadratic, or one text read many times: each is 80 KB or more.
        lines = [
            "echo env " + "1" * 100000,
            "ls env " + "/proc/" * 20000,
            "declare -" + "p" * 100000 + "! API_KEY",
            "bash -" + "c" * 100000 + "! env",
            "sudo -x " * 15000 + "ls env",
            "ssh env " + "sh " * 30000,
            "echo env " + "$(" * 45000,
            "'\nprintenv" + " " * 80000 + ")",
            "if ( " * 38000 + "env x",
            "'" + "$( printenv " * 15000,
            "ssh " + "sh " * 85 + "-c '" + "echo;" * 30000 + "xenv'",
        ]
        started = time.process_time()
        for line in lines:
            worker_transcript.command_reads_a_secret(line)
        self.assertLess(time.process_time() - started, self.LONG_LINE_SECONDS)

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

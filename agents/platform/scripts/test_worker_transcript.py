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
    ]

    def test_covers_every_output_form(self):
        for cmd in self.WITHHELD_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertTrue(worker_transcript.is_secret_read_tool_call(json.dumps({"command": cmd})))
        for cmd in self.SERVED_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertFalse(worker_transcript.is_secret_read_tool_call(json.dumps({"command": cmd})))

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

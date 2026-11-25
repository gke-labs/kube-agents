# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""deploy/sandbox/session-kubectl.sh, the worker image's kubectl wrapper.

The script runs from a copy whose kubectl and broker-client paths point at
stubs: the kubectl stub logs its argv and stdin and exits with STUB_RC, and
the client stub logs its argv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "sandbox" / "session-kubectl.sh"
REAL_KUBECTL = "readonly REAL_KUBECTL=/usr/local/libexec/kubectl"
CLIENT = "readonly CLIENT=/opt/credential-proxy/libexec/credential-proxy-exec"


class SessionKubectlWrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.kubectl_log = self.tmp / "kubectl.log"
        self.client_log = self.tmp / "client.log"
        kubectl = self.tmp / "kubectl"
        kubectl.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$KUBECTL_LOG"\ncat >> "$KUBECTL_LOG"\nexit "${STUB_RC:-0}"\n'
        )
        kubectl.chmod(0o755)
        client = self.tmp / "client"
        client.write_text('#!/usr/bin/env python3\nimport os, sys\nopen(os.environ["CLIENT_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")\n')
        client.chmod(0o755)
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(REAL_KUBECTL, text)
        self.assertIn(CLIENT, text)
        wrapper = self.tmp / "wrapper.sh"
        wrapper.write_text(
            text.replace(REAL_KUBECTL, f"readonly REAL_KUBECTL={kubectl}").replace(CLIENT, f"readonly CLIENT={client}")
        )
        wrapper.chmod(0o755)
        self.wrapper = wrapper
        home = self.tmp / "home"
        (home / ".kube").mkdir(parents=True)
        (home / ".kube" / "config").write_text("{}\n")  # no context ensure in these runs
        self.base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "KUBECTL_LOG": str(self.kubectl_log),
            "CLIENT_LOG": str(self.client_log),
        }

    def run_wrapper(self, *args, rc=0, stdin="", **env):
        full = {**self.base_env, "STUB_RC": str(rc), **env}
        return subprocess.run(["/bin/sh", str(self.wrapper), *args], input=stdin, text=True, env=full, capture_output=True, timeout=30)

    def client_calls(self):
        return self.client_log.read_text().splitlines() if self.client_log.exists() else []

    def session_env(self, **extra):
        return {"KUBE_PROXY_URL": "https://kube-proxy", "KUBE_PROXY_MODE": "read-only-dry-run",
                "CREDENTIAL_PROXY_REFUSAL_FILE": str(self.tmp / "write-refusals"), **extra}

    def test_a_failed_call_under_a_read_only_proxy_asks_the_client_to_record(self):
        done = self.run_wrapper("scale", "deploy/x", "--replicas=3", rc=1, **self.session_env())
        self.assertEqual(1, done.returncode)
        self.assertEqual(["--record-kubectl-write-attempt 1 scale deploy/x --replicas=3"], self.client_calls())

    def test_the_exit_code_is_kubectls(self):
        for rc in (0, 1, 3, 77):
            with self.subTest(rc=rc):
                self.assertEqual(rc, self.run_wrapper("get", "pods", rc=rc, **self.session_env()).returncode)

    def test_no_client_call_without_every_condition(self):
        for name, rc, env in (
            ("exit 0", 0, self.session_env()),
            ("no mode from the gateway", 1, self.session_env(KUBE_PROXY_MODE="")),
            ("a writable proxy", 1, self.session_env(KUBE_PROXY_MODE="read-write")),
            ("no record file", 1, self.session_env(CREDENTIAL_PROXY_REFUSAL_FILE="")),
        ):
            with self.subTest(name):
                self.client_log.unlink(missing_ok=True)
                self.run_wrapper("delete", "pod", "p", rc=rc, **env)
                self.assertEqual([], self.client_calls())

    def test_kubectl_reads_the_callers_stdin(self):
        self.run_wrapper("apply", "-f", "-", stdin="kind: ConfigMap\n", **self.session_env())
        self.assertIn("kind: ConfigMap", self.kubectl_log.read_text())

    def test_without_kube_proxy_a_broker_url_sends_kubectl_to_the_broker(self):
        done = self.run_wrapper("get", "pods", CREDENTIAL_PROXY_URL="http://broker")
        self.assertEqual(0, done.returncode)
        self.assertEqual(["kubectl get pods"], self.client_calls())
        self.assertFalse(self.kubectl_log.exists())


if __name__ == "__main__":
    unittest.main()

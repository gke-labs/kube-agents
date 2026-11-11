"""Tests for deploy/sandbox/kubectl_context_registered.sh, the kubectl wrapper's fast path.

The helper may say "registered" (exit 0) only for an entry that
`credential_proxy_client.py --ensure-kubectl-context` would leave untouched, so the registered
entries here are written by that client's own merge function, and every other shape must exit 1
(the wrapper then runs Python as before).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "sandbox" / "kubectl_context_registered.sh"
CLIENT = ROOT / "agents" / "platform" / "scripts" / "credential_proxy_client.py"
DOCKERFILES = (ROOT / "deploy" / "sandbox" / "Dockerfile", ROOT / "a2a" / "Dockerfile.worker")
INSTALLED = "/usr/local/libexec/kubectl-context-registered"

PROXY = "https://platform-agent-kube-proxy.kubeagents-system.svc.cluster.local:8443"
CA = "/var/run/secrets/kubeagents/kube-proxy-ca/ca.crt"
TOKEN = "/var/run/secrets/kubeagents/credential-proxy/token"
CTX = "gke_acme-prod_us-central1_prod-usc1"
HOST_CTX = "gke_acme-host_us-central1-a_host"
# What sandbox-entrypoint seeds before any --context call: the host context only.
SEED = f"""apiVersion: v1
kind: Config
clusters:
- name: {HOST_CTX}
  cluster:
    server: {PROXY}/clusters/{HOST_CTX}
    certificate-authority: {CA}
contexts:
- name: {HOST_CTX}
  context:
    cluster: {HOST_CTX}
    user: kube-proxy-caller
current-context: {HOST_CTX}
users:
- name: kube-proxy-caller
  user:
    tokenFile: {TOKEN}
"""


def load_client():
    spec = importlib.util.spec_from_file_location("credential_proxy_client_for_test", CLIENT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def shells() -> list[str]:
    """`sh`, plus dash when the machine has it (the images' /bin/sh is dash)."""
    found = ["sh"]
    if shutil.which("dash"):
        found.append("dash")
    return found


class KubectlContextRegisteredTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = load_client()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.kubeconfig = self.home / "kubeconfig"
        self.kubeconfig.write_text(SEED, encoding="utf-8")

    def register(self, context=CTX, proxy=PROXY, ca=CA, token=TOKEN, path=None):
        """Merge `context` into the kubeconfig with the client's own code, as the wrapper's Python does."""
        self.client._merge_context_into_kubeconfig_file(path or self.kubeconfig, context, proxy, ca, token)

    def run_helper(self, *argv, shell="sh", cwd=None, **overrides):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "KUBE_PROXY_URL": PROXY,
               "KUBECONFIG": str(self.kubeconfig), "KUBE_PROXY_CA_FILE": CA,
               "CREDENTIAL_PROXY_TOKEN_FILE": TOKEN}
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        result = subprocess.run([shell, str(HELPER), *argv], env=env, capture_output=True, text=True, cwd=cwd)
        self.assertEqual(result.stderr, "")
        return result.returncode

    def assert_registered(self, *argv, **overrides):
        for shell in shells():
            self.assertEqual(self.run_helper(*argv, shell=shell, **overrides), 0, (shell, argv))

    def assert_not_registered(self, *argv, cwd=None, **overrides):
        for shell in shells():
            self.assertEqual(self.run_helper(*argv, shell=shell, cwd=cwd, **overrides), 1, (shell, argv))

    def rewrite(self, old, new):
        text = self.kubeconfig.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.kubeconfig.write_text(text.replace(old, new), encoding="utf-8")

    def test_a_context_the_client_registered_is_registered(self):
        self.register()
        self.assert_registered("--context", CTX, "get", "ns", "-o", "name")
        self.assert_registered(f"--context={CTX}", "get", "ns")

    def test_a_context_not_in_the_kubeconfig_is_not(self):
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_a_context_whose_name_is_a_prefix_of_a_registered_one_is_not(self):
        self.register(context=f"{CTX}2")
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_a_context_that_extends_a_registered_one_is_not(self):
        self.register()
        self.assert_not_registered("--context", f"{CTX}2", "get", "ns")

    def test_a_server_that_names_a_longer_context_is_not(self):
        self.register()
        self.rewrite(f"server: {PROXY}/clusters/{CTX}\n", f"server: {PROXY}/clusters/{CTX}2\n")
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_an_entry_for_another_proxy_url_is_not(self):
        self.register(proxy="https://old-kube-proxy.kubeagents-system.svc.cluster.local:8443")
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_a_trailing_slash_on_the_proxy_url_is_the_same_url(self):
        self.register()
        self.assert_registered("--context", CTX, "get", "ns", KUBE_PROXY_URL=f"{PROXY}/")

    def test_a_stale_certificate_authority_is_not(self):
        self.register()
        self.assert_not_registered("--context", CTX, "get", "ns", KUBE_PROXY_CA_FILE="/var/run/new-ca.crt")

    def test_a_stale_token_file_is_not(self):
        self.register()
        self.assert_not_registered("--context", CTX, "get", "ns", CREDENTIAL_PROXY_TOKEN_FILE="/var/run/new-token")

    def test_the_default_ca_and_token_files_are_the_clients(self):
        self.register()
        self.assert_registered("--context", CTX, "get", "ns", KUBE_PROXY_CA_FILE=None, CREDENTIAL_PROXY_TOKEN_FILE=None)
        text = HELPER.read_text(encoding="utf-8")
        for name, value in (("DEFAULT_CA_FILE", self.client.DEFAULT_KUBE_PROXY_CA_FILE),
                            ("DEFAULT_TOKEN_FILE", self.client.DEFAULT_KUBE_PROXY_TOKEN_FILE),
                            ("USER_NAME", self.client.KUBE_PROXY_USER_NAME)):
            self.assertIn(f'readonly {name}="{value}"', text)

    def test_a_context_that_points_at_another_cluster_is_not(self):
        self.register()
        self.rewrite(f"    cluster: {CTX}\n", f"    cluster: {HOST_CTX}\n")
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_a_context_that_points_at_another_user_is_not(self):
        # Only <ctx>'s context changes: the host context still names kube-proxy-caller.
        self.register()
        self.rewrite(f"    cluster: {CTX}\n    user: kube-proxy-caller\n", f"    cluster: {CTX}\n    user: someone-else\n")
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_a_name_alone_is_not(self):
        self.kubeconfig.write_text(SEED + f"# - name: {CTX}\n", encoding="utf-8")
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_a_quoted_server_falls_back(self):
        self.register()
        self.rewrite(f"server: {PROXY}/clusters/{CTX}", f"server: '{PROXY}/clusters/{CTX}'")
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_a_symlinked_kubeconfig_falls_back(self):
        self.register()
        link = self.home / "link"
        link.symlink_to(self.kubeconfig)
        self.assert_not_registered("--context", CTX, "get", "ns", KUBECONFIG=str(link))

    def test_a_missing_kubeconfig_is_not(self):
        self.assert_not_registered("--context", CTX, "get", "ns", KUBECONFIG=str(self.home / "absent"))

    def test_a_kubeconfig_list_falls_back(self):
        # A real file whose name holds a `:`, which kubectl would split into two paths.
        listed = self.home / "kc:other"
        listed.write_text(SEED, encoding="utf-8")
        self.register(path=listed)
        self.assert_not_registered("--context", CTX, "get", "ns", KUBECONFIG=str(listed))

    def test_without_kubeconfig_the_default_file_is_read(self):
        default = self.home / ".kube" / "config"
        default.parent.mkdir()
        default.write_text(SEED, encoding="utf-8")
        self.register(path=default)
        self.assert_registered("--context", CTX, "get", "ns", KUBECONFIG=None)
        self.assert_not_registered("--context", CTX, "get", "ns")  # KUBECONFIG names the other file

    def test_an_argv_with_its_own_kubeconfig_falls_back(self):
        self.register()
        self.assert_not_registered("--kubeconfig", str(self.kubeconfig), "--context", CTX, "get", "ns")
        self.assert_not_registered(f"--kubeconfig={self.kubeconfig}", "--context", CTX, "get", "ns")

    def test_a_context_that_is_not_a_gke_name_falls_back(self):
        # Each is registered in full, so only the name check stands between it and "registered".
        for context in ("minikube", "gke_acme-prod_us-central1", "gke_Acme_us-central1_prod",
                        "gke_acme_prod_us-central1_prod-usc1", "gke_acme_us-central1_-prod", "gke__us-central1_prod"):
            self.register(context=context)
            self.assert_not_registered("--context", context, "get", "ns")

    def test_a_glob_in_the_context_is_not_expanded(self):
        # A part `*` must fail the name check even where the working directory holds a matching file.
        glob = "gke_acme_us-central1_*"
        self.register(context=glob)
        workdir = self.home / "workdir"
        workdir.mkdir()
        (workdir / "c").write_text("", encoding="utf-8")  # the only match for `*`
        self.assert_not_registered("--context", glob, "get", "ns", cwd=str(workdir))

    def test_a_context_after_end_of_flags_is_not_kubectls(self):
        self.register()
        self.assert_not_registered("exec", "pod", "--", "tool", "--context", CTX)

    def test_the_last_context_wins(self):
        self.register()
        self.assert_not_registered("--context", CTX, "--context", HOST_CTX + "x", "get", "ns")
        self.assert_registered("--context", HOST_CTX + "x", "--context", CTX, "get", "ns")

    def test_without_a_kube_proxy_url_nothing_is_registered(self):
        self.register()
        self.assert_not_registered("--context", CTX, "get", "ns", KUBE_PROXY_URL=None)
        self.assert_not_registered("--context", CTX, "get", "ns", KUBE_PROXY_URL="")


    def python_would_rewrite(self):
        """Whether the client's merge would change the kubeconfig, run on a copy of it."""
        copy = self.home / "copy"
        copy.write_text(self.kubeconfig.read_text(encoding="utf-8"), encoding="utf-8")
        self.register(path=copy)
        return copy.read_text(encoding="utf-8") != self.kubeconfig.read_text(encoding="utf-8")

    def test_registered_never_comes_with_a_rewrite_python_would_make(self):
        # Each case leaves a line Python wants somewhere in the file, but not in <ctx>'s own entry.
        stock_layout = (f"- cluster:\n    certificate-authority: {CA}\n    server: {PROXY}/clusters/{CTX}\n"
                        f"  name: {CTX}\n")
        cases = {
            "the context renamed": (f"- name: {CTX}\n  context:", "- name: short\n  context:"),
            "the context's user switched": (f"    cluster: {CTX}\n    user: kube-proxy-caller\n",
                                            f"    cluster: {CTX}\n    user: someone-else\n"),
            "the cluster's CA as data": (f"    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n",
                                         f"    server: {PROXY}/clusters/{CTX}\n    certificate-authority-data: AAAA\n"),
            "an extra token on the user": (f"    tokenFile: {TOKEN}\n", f"    tokenFile: {TOKEN}\n    token: static\n"),
            "the cluster's lines under the context, and no cluster": (
                f"- name: {CTX}\n  cluster:\n    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n",
                ""),
            "the token file on another user": (
                f"- name: kube-proxy-caller\n  user:\n    tokenFile: {TOKEN}\n",
                f"- name: kube-proxy-caller\n  user:\n    tokenFile: /old/token\n- name: other\n  user:\n"
                f"    tokenFile: {TOKEN}\n"),
        }
        for label, (old, new) in cases.items():
            with self.subTest(label):
                self.kubeconfig.write_text(SEED, encoding="utf-8")
                self.register()
                self.rewrite(old, new)
                if label.startswith("the cluster's lines under the context"):
                    self.rewrite(f"    cluster: {CTX}\n", f"    cluster: {CTX}\n    server: {PROXY}/clusters/{CTX}\n"
                                 f"    certificate-authority: {CA}\n")
                self.assertTrue(self.python_would_rewrite(), label)
                self.assert_not_registered("--context", CTX, "get", "ns")
        with self.subTest("the context's lines under the cluster, and no context"):
            self.kubeconfig.write_text(SEED, encoding="utf-8")
            self.register()
            self.rewrite(f"- name: {CTX}\n  context:\n    cluster: {CTX}\n    user: kube-proxy-caller\n", "")
            self.rewrite(f"    certificate-authority: {CA}\ncontexts:\n",
                         f"    certificate-authority: {CA}\n    cluster: {CTX}\n    user: kube-proxy-caller\ncontexts:\n")
            self.assertTrue(self.python_would_rewrite())
            self.assert_not_registered("--context", CTX, "get", "ns")
        with self.subTest("a user written with its fields first"):
            self.kubeconfig.write_text(SEED, encoding="utf-8")
            self.register()
            self.rewrite(f"- name: kube-proxy-caller\n  user:\n    tokenFile: {TOKEN}\n",
                         f"- user:\n    tokenFile: {TOKEN}\n  name: kube-proxy-caller\n")
            self.assertFalse(self.python_would_rewrite())
            self.assert_registered("--context", CTX, "get", "ns")
        with self.subTest("stock kubectl's key order"):
            self.kubeconfig.write_text(SEED, encoding="utf-8")
            self.register()
            self.rewrite(f"- name: {CTX}\n  cluster:\n    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n",
                         stock_layout)
            self.assertFalse(self.python_would_rewrite())
            self.assert_registered("--context", CTX, "get", "ns")


class WrapperWiringTest(unittest.TestCase):
    def test_both_images_install_the_helper_and_ask_it_before_python(self):
        for dockerfile in DOCKERFILES:
            text = dockerfile.read_text(encoding="utf-8")
            self.assertIn(f"COPY --chmod=0755 deploy/sandbox/kubectl_context_registered.sh {INSTALLED}", text, dockerfile)
            context_branch = re.search(r"\*\" --context\"\*\)' \\\n\s*'([^']*)'", text)
            self.assertIsNotNone(context_branch, dockerfile)
            self.assertTrue(context_branch.group(1).strip().startswith(f'{INSTALLED} "$@" 2>/dev/null || '), dockerfile)
            self.assertIn("--ensure-kubectl-context", context_branch.group(1), dockerfile)


if __name__ == "__main__":
    unittest.main()

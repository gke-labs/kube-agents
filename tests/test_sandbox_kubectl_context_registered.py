"""Tests for deploy/sandbox/kubectl_context_registered.sh, the kubectl wrapper's fast path.

The helper may say "registered" (exit 0) only for an entry that
`credential_proxy_client.py --ensure-kubectl-context` would leave untouched, so the registered
entries here are written by that client's own merge function, and every other shape must exit 1
(the wrapper then runs Python as before).
"""

from __future__ import annotations

import hashlib
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


# What sandbox-entrypoint writes when an extra context is declared: clusters, then users, then contexts.
ENTRYPOINT_LAYOUT = f"""apiVersion: v1
kind: Config
clusters:
- name: {HOST_CTX}
  cluster:
    server: {PROXY}/clusters/{HOST_CTX}
    certificate-authority: {CA}
- name: {CTX}
  cluster:
    server: {PROXY}/clusters/{CTX}
    certificate-authority: {CA}
users:
- name: kube-proxy-caller
  user:
    tokenFile: {TOKEN}
contexts:
- name: {HOST_CTX}
  context:
    cluster: {HOST_CTX}
    user: kube-proxy-caller
- name: {CTX}
  context:
    cluster: {CTX}
    user: kube-proxy-caller
current-context: {HOST_CTX}
"""
# Stock `kubectl config` writes each item's keys in alphabetical order.
STOCK_KUBECTL_LAYOUT = f"""apiVersion: v1
clusters:
- cluster:
    certificate-authority: {CA}
    server: {PROXY}/clusters/{CTX}
  name: {CTX}
contexts:
- context:
    cluster: {CTX}
    user: kube-proxy-caller
  name: {CTX}
current-context: {CTX}
kind: Config
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
                            ("DEFAULT_TOKEN_FILE", self.client.DEFAULT_KUBE_PROXY_TOKEN_FILE)):
            self.assertIn(f'readonly {name}="{value}"', text)
        suffix = re.search(r'readonly MARKER_SUFFIX="([^"]*)"', text).group(1)
        self.assertEqual(self.client.REGISTERED_MARKER_NAME, ".{name}" + suffix)

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

    def test_the_three_layouts_in_use_are_registered_after_one_python_pass(self):
        # A file Python has not checked yet is not registered; one pass that leaves it alone registers it.
        for label, text in (("sandbox-entrypoint's", ENTRYPOINT_LAYOUT), ("stock kubectl's", STOCK_KUBECTL_LAYOUT)):
            with self.subTest(label):
                self.marker().unlink(missing_ok=True)
                self.kubeconfig.write_text(text, encoding="utf-8")
                self.assert_not_registered("--context", CTX, "get", "ns")
                self.register()
                self.assertEqual(self.kubeconfig.read_text(encoding="utf-8"), text, "Python left the file alone")
                self.assert_registered("--context", CTX, "get", "ns")
        with self.subTest("the client's"):
            self.marker().unlink(missing_ok=True)
            self.kubeconfig.write_text(SEED, encoding="utf-8")
            self.register()  # a rewrite: it adds the context, and records the bytes it wrote
            self.assert_registered("--context", CTX, "get", "ns")

    def marker(self, path=None):
        path = path or self.kubeconfig
        return path.with_name(f".{path.name}.contexts-registered")

    def test_python_records_the_bytes_it_checked_or_wrote(self):
        self.register()  # writes the context
        env = hashlib.sha256(f"{PROXY}\n{CA}\n{TOKEN}".encode()).hexdigest()
        line = f"{CTX} {hashlib.sha256(self.kubeconfig.read_bytes()).hexdigest()} {env}"
        self.assertEqual(self.marker().read_text(encoding="utf-8").splitlines(), [line])
        self.register(context=HOST_CTX)  # already current: recorded without a rewrite, the other line kept
        lines = self.marker().read_text(encoding="utf-8").splitlines()
        self.assertIn(line, lines)
        self.assertIn(f"{HOST_CTX} {hashlib.sha256(self.kubeconfig.read_bytes()).hexdigest()} {env}", lines)
        self.assertEqual(len(lines), 2)

    def test_an_edit_after_the_record_costs_one_python_pass(self):
        self.register()
        for label, old, new in (("a comment", "kind: Config\n", "kind: Config\n# a note\n"),
                                ("kubectl config use-context", f"current-context: {HOST_CTX}\n",
                                 f"current-context: {CTX}\n")):
            with self.subTest(label):
                self.rewrite(old, new)
                edited = self.kubeconfig.read_text(encoding="utf-8")
                self.assert_not_registered("--context", CTX, "get", "ns")
                self.register()
                self.assertEqual(self.kubeconfig.read_text(encoding="utf-8"), edited, "the pass rewrote nothing")
                self.assert_registered("--context", CTX, "get", "ns")

    def test_a_missing_or_symlinked_record_falls_back(self):
        self.register()
        recorded = self.marker().read_text(encoding="utf-8")
        self.marker().unlink()
        self.assert_not_registered("--context", CTX, "get", "ns")
        elsewhere = self.home / "record"
        elsewhere.write_text(recorded, encoding="utf-8")
        self.marker().symlink_to(elsewhere)
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_a_record_for_other_bytes_or_another_context_falls_back(self):
        self.register()
        good = self.marker().read_text(encoding="utf-8")
        context, file_digest, env_digest = good.split()
        self.marker().write_text(f"{context} {'0' * 64} {env_digest}\n", encoding="utf-8")
        self.assert_not_registered("--context", CTX, "get", "ns")
        self.marker().write_text(f"{HOST_CTX} {file_digest} {env_digest}\n", encoding="utf-8")
        self.assert_not_registered("--context", CTX, "get", "ns")
        self.marker().write_text(f"{context} {file_digest} {env_digest} extra\n", encoding="utf-8")
        self.assert_not_registered("--context", CTX, "get", "ns")

    def test_hand_edits_yaml_reads_differently_fall_back(self):
        # The shapes the human review reproduced against the earlier parsing helper: each is an edit, so
        # the record no longer matches the file, whatever a YAML reader makes of it.
        for label, old, new in (
                ("a second clusters section", "users:\n", "clusters: []\nusers:\n"),
                ("a duplicate key spelled with a space", f"- name: {CTX}\n  cluster:", f"- name: {CTX}\n  name : other\n  cluster:"),
                ("a continuation line under server", f"server: {PROXY}/clusters/{CTX}\n",
                 f"server: {PROXY}/clusters/{CTX}\n      x\n"),
                ("a list-valued user mapping", f"  user:\n    tokenFile: {TOKEN}\n", f"  user:\n  - tokenFile: {TOKEN}\n"),
                ("a bare list item", "contexts:\n", "contexts:\n-\n")):
            with self.subTest(label):
                self.kubeconfig.write_text(SEED, encoding="utf-8")
                self.register()
                self.rewrite(old, new)
                self.assert_not_registered("--context", CTX, "get", "ns")

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
            "the server outside the cluster mapping": (
                f"- name: {CTX}\n  cluster:\n    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n",
                f"- name: {CTX}\n  server: {PROXY}/clusters/{CTX}\n  cluster:\n    certificate-authority: {CA}\n"),
            "no cluster mapping at all": (
                f"- name: {CTX}\n  cluster:\n    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n",
                f"- name: {CTX}\n  server: {PROXY}/clusters/{CTX}\n  certificate-authority: {CA}\n"),
            "the server only nested deeper in the cluster": (
                f"    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n",
                f"    certificate-authority: {CA}\n    extensions:\n      server: {PROXY}/clusters/{CTX}\n"),
            "the context's name only deep inside another cluster": (
                f"- name: {CTX}\n  cluster:\n    server: {PROXY}/clusters/{CTX}\n",
                f"- extensions:\n  - extension:\n      name: {CTX}\n  cluster:\n"
                f"    server: {PROXY}/clusters/{CTX}\n"),
            "a second server for the cluster": (
                f"    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n",
                f"    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n    server: https://elsewhere\n"),
            "a second user key on the user": (f"    tokenFile: {TOKEN}\n", f"    tokenFile: {TOKEN}\n  user: {{}}\n"),
            "the user's name only inside a nested list": (
                "- name: kube-proxy-caller\n  user:",
                "- name: other\n  extensions:\n  - name: kube-proxy-caller\n    extension: {}\n  user:"),
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
            self.register()  # one pass records the edited file
            self.assert_registered("--context", CTX, "get", "ns")
        with self.subTest("stock kubectl's key order"):
            self.kubeconfig.write_text(SEED, encoding="utf-8")
            self.register()
            self.rewrite(f"- name: {CTX}\n  cluster:\n    server: {PROXY}/clusters/{CTX}\n    certificate-authority: {CA}\n",
                         stock_layout)
            self.assertFalse(self.python_would_rewrite())
            self.register()  # one pass records the edited file
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

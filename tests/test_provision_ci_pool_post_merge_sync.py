"""scripts/provision_ci_pool_post_merge_sync.sh, executed against fake gh, gcloud and ssh-keygen.

Each fake logs its calls as JSON lines and answers from the state the case
sets, so these assertions are against the script that ships.
"""

import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "provision_ci_pool_post_merge_sync.sh"
_PROVISION_SCRIPT = _REPO_ROOT / "scripts" / "provision_ci_pool_project.sh"
_FLEET_TF = _REPO_ROOT / "bench" / "tf" / "fleet"

# Literals on purpose: a test that read them back from the script would assert nothing.
_PROJECT = "kube-agents-evals-99"
_REPO = "gke-agentic/kube-agents-evals-99-infra"
_SECRET = "kube-agents-eval-gitops-deploy-key"
_TITLE = "seeded-fleet config-sync (read-only)"
_DECLARATION = "clusters/seeded-a/seeded-post-merge/order-ledger.yaml"
_BUDGET = "clusters/seeded-a/seeded-post-merge/order-ledger-pdb.yaml"
_RECONCILER = "serviceAccount:seeded-fleet-reconciler@kube-agents-prow.iam.gserviceaccount.com"
_HOST_KEYS = ["ssh-ed25519 HOSTKEY-A", "ecdsa-sha2-nistp256 HOSTKEY-B"]
_KNOWN_HOSTS = "\n".join(sorted(f"github.com {k}" for k in _HOST_KEYS)) + "\n"

_FAKE_PRELUDE = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
argv = sys.argv[1:]
entry = {"tool": pathlib.Path(sys.argv[0]).name, "argv": argv}
def log():
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(entry) + "\n")
def done(text="", code=0, err=""):
    sys.stdout.write(text); sys.stderr.write(err); log(); sys.exit(code)
"""

_FAKE_GH = _FAKE_PRELUDE + r"""
if argv[:2] == ["api", "meta"]:
    done("".join("github.com %s\n" % k for k in json.loads(os.environ["FAKE_HOST_KEYS"])))
if argv[:3] == ["api", "--paginate", "repos/%s/keys" % os.environ["FAKE_REPO"]]:
    rows = json.loads(os.environ["FAKE_DEPLOY_KEYS"])
    done("".join("%s\t%s\t%s\n" % (r["id"], r["key"], str(r["read_only"]).lower()) for r in rows))
if argv[:1] == ["api"] and "/contents/clusters/" in argv[1]:
    if os.environ.get("FAKE_DECLARATION_ABSENT") == "1":
        done(code=1, err="gh: Not Found (HTTP 404)\n")
    done("{}")
if argv[:3] == ["api", "-X", "PUT"] or argv[:3] == ["api", "-X", "DELETE"]:
    done()
if argv[:3] == ["repo", "deploy-key", "add"]:
    entry["public"] = pathlib.Path(argv[3]).read_text()
    done()
done(code=97, err="fake gh: unexpected call %s\n" % argv)
"""

_FAKE_GCLOUD = _FAKE_PRELUDE + r"""
state = pathlib.Path(os.environ["FAKE_STATE"])
if argv[:2] == ["secrets", "describe"]:
    if os.environ.get("FAKE_SECRET_EXISTS") == "1" or (state / "secret").exists():
        done("projects/123/secrets/%s\n" % argv[2])
    done(code=1, err="ERROR: NOT_FOUND: Secret not found.\n")
if argv[:2] == ["secrets", "create"]:
    (state / "secret").touch(); done()
if argv[:3] == ["secrets", "versions", "access"]:
    current = os.environ.get("FAKE_STORED")
    if current is None:
        done(code=1, err="ERROR: NOT_FOUND: Secret Version [latest] not found.\n")
    done(current)
if argv[:3] == ["secrets", "versions", "add"]:
    entry["stdin"] = sys.stdin.read(); done()
if argv[:2] == ["secrets", "add-iam-policy-binding"]:
    done()
done(code=97, err="fake gcloud: unexpected call %s\n" % argv)
"""

# `-f F` with `-y` prints the public half of a fake private key; without it,
# writes a fresh pair named after the call count.
_FAKE_SSH_KEYGEN = _FAKE_PRELUDE + r"""
path = argv[argv.index("-f") + 1]
if "-y" in argv:
    done("ssh-ed25519 PUB-%s comment\n" % pathlib.Path(path).read_text().removeprefix("PRIVATE-"))
pathlib.Path(path).write_text("PRIVATE-new")
pathlib.Path(path + ".pub").write_text("ssh-ed25519 PUB-new %s\n" % argv[argv.index("-C") + 1])
done()
"""


def _stored(private="PRIVATE-old", known_hosts=_KNOWN_HOSTS):
    return json.dumps({"ssh": private, "known_hosts": known_hosts})


class ProvisionPostMergeSyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.bindir, self.state, self.log = root / "bin", root / "state", root / "calls.log"
        self.bindir.mkdir()
        self.state.mkdir()
        for name, body in (("gh", _FAKE_GH), ("gcloud", _FAKE_GCLOUD), ("ssh-keygen", _FAKE_SSH_KEYGEN)):
            fake = self.bindir / name
            fake.write_text(body)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def _run(self, *, declaration_absent=False, secret_exists=False, stored=None, deploy_keys=(), extra_args=()):
        overrides = {
            "PATH": f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.state),
            "FAKE_REPO": _REPO,
            "FAKE_DECLARATION": _DECLARATION,
            "FAKE_DECLARATION_ABSENT": "1" if declaration_absent else "0",
            "FAKE_SECRET_EXISTS": "1" if secret_exists else "0",
            "FAKE_HOST_KEYS": json.dumps(_HOST_KEYS),
            "FAKE_DEPLOY_KEYS": json.dumps(list(deploy_keys)),
        }
        absent = ("FAKE_STORED",) if stored is None else ()
        if stored is not None:
            overrides["FAKE_STORED"] = stored
        env = get_isolated_test_env(overrides=overrides, absent=absent)
        proc = subprocess.run(["bash", str(_SCRIPT), _PROJECT, *extra_args], capture_output=True, text=True, env=env, check=False)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return proc, calls

    @staticmethod
    def _calls(calls, tool, *prefix):
        return [c for c in calls if c["tool"] == tool and c["argv"][: len(prefix)] == list(prefix)]

    def _assert_grant(self, calls):
        grants = self._calls(calls, "gcloud", "secrets", "add-iam-policy-binding", _SECRET)
        self.assertEqual(1, len(grants))
        self.assertIn(f"--member={_RECONCILER}", grants[0]["argv"])
        self.assertIn("--role=roles/secretmanager.secretAccessor", grants[0]["argv"])

    def test_a_fresh_project_gets_the_declaration_a_read_only_key_the_secret_and_the_grant(self):
        proc, calls = self._run(declaration_absent=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        for path in (_DECLARATION, _BUDGET):
            self.assertEqual(1, len(self._calls(calls, "gh", "api", "-X", "PUT", f"repos/{_REPO}/contents/{path}")), path)
        self.assertEqual(1, len(self._calls(calls, "gcloud", "secrets", "create", _SECRET)))
        added = self._calls(calls, "gh", "repo", "deploy-key", "add")
        self.assertEqual(1, len(added))
        self.assertIn(_TITLE, added[0]["argv"])
        self.assertIn(_REPO, added[0]["argv"])
        self.assertNotIn("--allow-write", added[0]["argv"])
        version = self._calls(calls, "gcloud", "secrets", "versions", "add", _SECRET)
        self.assertEqual({"ssh": "PRIVATE-new", "known_hosts": _KNOWN_HOSTS}, json.loads(version[0]["stdin"]))
        self._assert_grant(calls)
        self.assertNotIn("PRIVATE-new", proc.stdout + proc.stderr)

    def test_a_stored_key_the_repository_holds_read_only_is_left_alone(self):
        proc, calls = self._run(
            secret_exists=True, stored=_stored(), deploy_keys=[{"id": 7, "key": "ssh-ed25519 PUB-old", "read_only": True}]
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([], self._calls(calls, "gh", "api", "-X", "PUT"))
        self.assertEqual([], self._calls(calls, "ssh-keygen", "-q"))
        self.assertEqual([], self._calls(calls, "gh", "repo", "deploy-key", "add"))
        self.assertEqual([], self._calls(calls, "gcloud", "secrets", "versions", "add"))
        self.assertEqual([], self._calls(calls, "gh", "api", "-X", "DELETE"))
        self._assert_grant(calls)
        self.assertNotIn("PRIVATE-old", proc.stdout + proc.stderr)

    def test_a_key_with_write_access_is_replaced_and_deleted_after_the_secret_moves(self):
        proc, calls = self._run(
            secret_exists=True, stored=_stored(), deploy_keys=[{"id": 7, "key": "ssh-ed25519 PUB-old", "read_only": False}]
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        order = [(c["tool"], c["argv"][:3]) for c in calls]
        add = order.index(("gh", ["repo", "deploy-key", "add"]))
        version = order.index(("gcloud", ["secrets", "versions", "add"]))
        delete = order.index(("gh", ["api", "-X", "DELETE"]))
        self.assertLess(add, version)
        self.assertLess(version, delete)
        self.assertEqual(f"repos/{_REPO}/keys/7", calls[delete]["argv"][3])

    def test_changed_host_keys_replace_the_key(self):
        proc, calls = self._run(
            secret_exists=True,
            stored=_stored(known_hosts="github.com ssh-rsa RETIRED\n"),
            deploy_keys=[{"id": 7, "key": "ssh-ed25519 PUB-old", "read_only": True}],
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        version = self._calls(calls, "gcloud", "secrets", "versions", "add", _SECRET)
        self.assertEqual(_KNOWN_HOSTS, json.loads(version[0]["stdin"])["known_hosts"])

    def test_the_converged_path_deletes_every_other_key_under_the_title(self):
        keys = [
            {"id": 7, "key": "ssh-ed25519 PUB-old", "read_only": True},
            {"id": 8, "key": "ssh-ed25519 PUB-stray", "read_only": True},
            {"id": 9, "key": "ssh-ed25519 PUB-old", "read_only": False},
        ]
        proc, calls = self._run(secret_exists=True, stored=_stored(), deploy_keys=keys)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([], self._calls(calls, "gh", "repo", "deploy-key", "add"))
        deleted = [c["argv"][3] for c in self._calls(calls, "gh", "api", "-X", "DELETE")]
        self.assertEqual([f"repos/{_REPO}/keys/8", f"repos/{_REPO}/keys/9"], deleted)

    def test_the_accessor_is_an_argument(self):
        member = "user:dev@example.com"
        proc, calls = self._run(extra_args=(_REPO, member))
        self.assertEqual(0, proc.returncode, proc.stderr)
        grant = self._calls(calls, "gcloud", "secrets", "add-iam-policy-binding", _SECRET)[0]["argv"]
        self.assertIn(f"--member={member}", grant)

    def test_an_accessor_that_is_not_a_member_is_refused(self):
        proc, _ = self._run(extra_args=(_REPO, "dev@example.com"))
        self.assertNotEqual(0, proc.returncode)

    def test_the_declaration_is_the_fleet_stacks_deployment(self):
        defects = (_FLEET_TF / "defects-a.tf").read_text()
        header = 'resource "kubernetes_deployment_v1" "order_ledger"'
        if header not in defects:
            self.skipTest("bench/tf/fleet/defects-a.tf does not plant order-ledger on this branch")
        import yaml

        block = defects[defects.index(header) :]
        block = block[: block.index("\n}\n")]
        content = re.search(r"^readonly DECLARATION_CONTENT='(.*?)'$", _SCRIPT.read_text(), re.MULTILINE | re.DOTALL).group(1)
        doc = yaml.safe_load(content)
        spec, pod = doc["spec"], doc["spec"]["template"]["spec"]
        container = pod["containers"][0]
        expected = {
            'name      = "order-ledger"': doc["metadata"]["name"] == "order-ledger",
            f"replicas = {spec['replicas']}": True,
            f'match_labels = {{ app = "{spec["selector"]["matchLabels"]["app"]}" }}': True,
            f"automount_service_account_token = {str(pod['automountServiceAccountToken']).lower()}": True,
            f"run_as_user     = {pod['securityContext']['runAsUser']}": True,
            f'name  = "{container["name"]}"': True,
            f'image = "{container["image"]}"': True,
            f'requests = {{ cpu = "{container["resources"]["requests"]["cpu"]}", memory = "{container["resources"]["requests"]["memory"]}" }}': True,
            f'limits   = {{ memory = "{container["resources"]["limits"]["memory"]}" }}': True,
        }
        for line, ok in expected.items():
            self.assertTrue(ok and line in block, line)
        self.assertEqual("seeded-post-merge", doc["metadata"]["namespace"])

    def test_the_fleet_stack_reads_the_secret_this_script_writes(self):
        # The one file that reads the secret, not a glob: config-sync.tf turns
        # Config Sync on and mentions post_merge without reading any secret.
        reader = _FLEET_TF / "post-merge-sync.tf"
        if not reader.is_file():
            self.skipTest("bench/tf/fleet/post-merge-sync.tf, which reads the deploy key, is not on this branch yet")
        self.assertIn(f'"{_SECRET}"', reader.read_text())

    def test_the_seeded_budget_is_the_one_the_case_stack_restores(self):
        budget = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "post-merge-sync-budget" / "budget.py"
        if not budget.is_file():
            self.skipTest("the case's setup stack is not on this branch yet")
        content = re.search(r"^readonly BUDGET_CONTENT='(.*?)'$", _SCRIPT.read_text(), re.MULTILINE | re.DOTALL).group(1)
        spec = importlib.util.spec_from_file_location("budget", budget)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(content + "\n", module.BUDGET_CONTENT)

    def test_provisioning_runs_it_before_the_fleet_apply(self):
        script = _PROVISION_SCRIPT.read_text()
        call = script.index('"${SCRIPT_DIR}/provision_ci_pool_post_merge_sync.sh" "${PROJECT_ID}" "${GITOPS_REPO}"')
        self.assertLess(call, script.index("[Step 2.2] Provisioning Seeded Dirty Fleet"))


if __name__ == "__main__":
    unittest.main()

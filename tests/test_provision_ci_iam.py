"""Tests for tests/e2e/scripts/provision_ci_iam.sh.

Verifies that the GitHub Actions OIDC Workload Identity Pool Provider is always
configured with a tenant-anchored attribute condition on creation, tightens
legacy/unrestricted existing providers, fails closed before mutating GCP on
conflicting active conditions or non-NOT_FOUND describe errors (without
blocking on interactive gcloud prompts), handles soft-deleted pools/providers
in GCP-required order with rollback on update-oidc failure and without masking
undelete errors, propagates WIF_POOL_ID into downstream bindings, and rejects
malformed, non-ASCII, or CEL-injecting --git_project values before any gcloud
call is made.
"""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from tests.testing import no_live_gcloud
from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROVISION_SCRIPT = _REPO_ROOT / "tests" / "e2e" / "scripts" / "provision_ci_iam.sh"

_FAKE_POOL_RESOURCE = "projects/123456789012/locations/global/workloadIdentityPools/github-actions-pool"

_FAKE_GCLOUD_TEMPLATE = """#!{python}
import json
import os
import sys

log_path = os.environ["FAKE_GCLOUD_LOG"]
state_path = os.environ["FAKE_GCLOUD_STATE"]
args = sys.argv[1:]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")

with open(state_path, "r", encoding="utf-8") as fh:
    state = json.load(fh)

def save_state():
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)

if args[:2] == ["services", "enable"]:
    if state.get("services_enable_activates_iam_api", True):
        state["iam_api_enabled"] = True
        save_state()
    sys.exit(0)

if args[:4] == ["iam", "workload-identity-pools", "providers", "describe"]:
    if "--quiet" not in args:
        print("ERROR: providers describe must pass --quiet to avoid hidden interactive API-enablement prompts", file=sys.stderr)
        sys.exit(1)
    if not state.get("iam_api_enabled", True):
        print("ERROR: PERMISSION_DENIED: SERVICE_DISABLED: IAM API has not been used in project my-project.", file=sys.stderr)
        sys.exit(1)
    if state.get("provider_describe_error"):
        print(f"ERROR: {{state['provider_describe_error']}}", file=sys.stderr)
        sys.exit(1)
    prov_state = state["provider_state"]
    if not prov_state:
        print("ERROR: (gcloud.iam.workload-identity-pools.providers.describe) NOT_FOUND: Requested entity was not found.", file=sys.stderr)
        sys.exit(1)
    fmt = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--format=")), "")
    if fmt == 'value[separator="\\\\n"](state,attributeCondition)':
        print(prov_state)
        if state["provider_condition"]:
            print(state["provider_condition"])
    else:
        print(f"ERROR: unexpected providers describe --format={{fmt!r}}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

if args[:4] == ["iam", "workload-identity-pools", "providers", "create-oidc"]:
    if state["pool_state"] != "ACTIVE":
        print("ERROR: FAILED_PRECONDITION: parent pool is not ACTIVE", file=sys.stderr)
        sys.exit(1)
    if state["provider_state"]:
        print("ERROR: ALREADY_EXISTS: provider already exists", file=sys.stderr)
        sys.exit(1)
    cond = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--attribute-condition=")), "")
    state["provider_state"] = "ACTIVE"
    state["provider_condition"] = cond
    save_state()
    sys.exit(0)

if args[:4] == ["iam", "workload-identity-pools", "providers", "undelete"]:
    if state["pool_state"] != "ACTIVE":
        print("ERROR: FAILED_PRECONDITION: cannot undelete provider while parent pool is not ACTIVE", file=sys.stderr)
        sys.exit(1)
    if state["provider_state"] != "DELETED":
        print("ERROR: FAILED_PRECONDITION: provider is not DELETED", file=sys.stderr)
        sys.exit(1)
    if state.get("provider_undelete_fail"):
        print("ERROR: PERMISSION_DENIED on providers undelete", file=sys.stderr)
        sys.exit(1)
    state["provider_state"] = "ACTIVE"
    save_state()
    sys.exit(0)

if args[:4] == ["iam", "workload-identity-pools", "providers", "update-oidc"]:
    if state["pool_state"] != "ACTIVE" or state["provider_state"] != "ACTIVE":
        print("ERROR: FAILED_PRECONDITION: provider must be ACTIVE to update", file=sys.stderr)
        sys.exit(1)
    if state.get("update_oidc_failures_remaining", 0) > 0:
        state["update_oidc_failures_remaining"] -= 1
        save_state()
        print("ERROR: 503 UNAVAILABLE: transient backend failure", file=sys.stderr)
        sys.exit(1)
    cond = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--attribute-condition=")), "")
    state["provider_condition"] = cond
    save_state()
    sys.exit(0)

if args[:4] == ["iam", "workload-identity-pools", "providers", "delete"]:
    if state["provider_state"] != "ACTIVE":
        print("ERROR: FAILED_PRECONDITION: provider is not ACTIVE", file=sys.stderr)
        sys.exit(1)
    state["provider_state"] = "DELETED"
    save_state()
    sys.exit(0)

if args[:3] == ["iam", "workload-identity-pools", "describe"]:
    if "--quiet" not in args:
        print("ERROR: pools describe must pass --quiet to avoid hidden interactive API-enablement prompts", file=sys.stderr)
        sys.exit(1)
    if state.get("pool_describe_error"):
        print(f"ERROR: {{state['pool_describe_error']}}", file=sys.stderr)
        sys.exit(1)
    pool_state = state["pool_state"]
    if not pool_state:
        print("ERROR: (gcloud.iam.workload-identity-pools.describe) NOT_FOUND: Requested entity was not found.", file=sys.stderr)
        sys.exit(1)
    fmt = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--format=")), "")
    if fmt == "value(state,name)":
        print(f"{{pool_state}}\\t{pool_resource}")
    elif fmt == "value(name)":
        print("{pool_resource}")
    else:
        print(f"ERROR: unexpected pools describe --format={{fmt!r}}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

if args[:3] == ["iam", "workload-identity-pools", "create"]:
    if state["pool_state"]:
        print("ERROR: ALREADY_EXISTS: pool already exists", file=sys.stderr)
        sys.exit(1)
    state["pool_state"] = "ACTIVE"
    save_state()
    sys.exit(0)

if args[:3] == ["iam", "workload-identity-pools", "undelete"]:
    if state["pool_state"] != "DELETED":
        print("ERROR: FAILED_PRECONDITION: pool is not DELETED", file=sys.stderr)
        sys.exit(1)
    if state.get("pool_undelete_fail"):
        print("ERROR: PERMISSION_DENIED on pools undelete", file=sys.stderr)
        sys.exit(1)
    state["pool_state"] = "ACTIVE"
    save_state()
    sys.exit(0)

sys.exit(0)
"""


def setUpModule():
    # No test here runs the real gcloud: a gcloud call that a test leaves
    # unstubbed goes to a stand-in, and the stand-in fails the module.
    global _NO_GCLOUD
    _NO_GCLOUD = no_live_gcloud.install()


def tearDownModule():
    _NO_GCLOUD.uninstall()


class TestNoLiveGcloud(unittest.TestCase):
    def test_no_test_in_this_module_can_run_the_real_gcloud(self):
        no_live_gcloud.assert_in_force(self, _NO_GCLOUD)


class ProvisionCiIamTest(unittest.TestCase):
    def _assert_pool_resource_bound(self, calls: list[list[str]], stdout: str, repo: str) -> None:
        sa_binding_calls = [
            call for call in calls if call[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]
        ]
        expected_member = f"--member=principalSet://iam.googleapis.com/{_FAKE_POOL_RESOURCE}/attribute.repository/{repo}"
        self.assertEqual(len(sa_binding_calls), 2, msg=f"Expected 2 SA bindings, got: {sa_binding_calls}")
        for call in sa_binding_calls:
            self.assertIn(expected_member, call)
        self.assertIn(f"{_FAKE_POOL_RESOURCE}/providers/github-provider", stdout)

    def _run_provision_in_dir(
        self,
        tmp_path: pathlib.Path,
        extra_args: list[str],
        *,
        env_overrides: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict]:
        log_file = tmp_path / "gcloud_calls.jsonl"
        log_file.write_text("", encoding="utf-8")
        state_file = tmp_path / "gcloud_state.json"

        overrides = {
            "FAKE_GCLOUD_LOG": str(log_file),
            "FAKE_GCLOUD_STATE": str(state_file),
        }
        if env_overrides:
            overrides.update(env_overrides)
        env = get_isolated_test_env(
            overrides=overrides,
            bin_dir=str(tmp_path),
        )
        proc = subprocess.run(
            ["bash", str(_PROVISION_SCRIPT), *extra_args],
            capture_output=True,
            text=True,
            env=env,
        )
        calls = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        final_state = json.loads(state_file.read_text(encoding="utf-8"))
        return proc, calls, final_state

    def _prepare_fake_gcloud(
        self,
        tmp_path: pathlib.Path,
        *,
        pool_state: str = "ACTIVE",
        provider_state: str = "",
        existing_condition: str = "",
        iam_api_enabled: bool = True,
        services_enable_activates_iam_api: bool = True,
        pool_describe_error: str = "",
        provider_describe_error: str = "",
        pool_undelete_fail: bool = False,
        provider_undelete_fail: bool = False,
        update_oidc_failures_remaining: int = 0,
    ) -> None:
        state_file = tmp_path / "gcloud_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "pool_state": pool_state,
                    "provider_state": provider_state,
                    "provider_condition": existing_condition,
                    "iam_api_enabled": iam_api_enabled,
                    "services_enable_activates_iam_api": services_enable_activates_iam_api,
                    "pool_describe_error": pool_describe_error,
                    "provider_describe_error": provider_describe_error,
                    "pool_undelete_fail": pool_undelete_fail,
                    "provider_undelete_fail": provider_undelete_fail,
                    "update_oidc_failures_remaining": update_oidc_failures_remaining,
                }
            ),
            encoding="utf-8",
        )
        gcloud = tmp_path / "gcloud"
        gcloud.write_text(
            _FAKE_GCLOUD_TEMPLATE.format(
                python=sys.executable,
                pool_resource=_FAKE_POOL_RESOURCE,
            ),
            encoding="utf-8",
        )
        gcloud.chmod(0o755)

    def _run_provision(
        self,
        extra_args: list[str],
        *,
        pool_state: str = "ACTIVE",
        provider_state: str = "",
        existing_condition: str = "",
        iam_api_enabled: bool = True,
        services_enable_activates_iam_api: bool = True,
        pool_describe_error: str = "",
        provider_describe_error: str = "",
        pool_undelete_fail: bool = False,
        provider_undelete_fail: bool = False,
        update_oidc_failures_remaining: int = 0,
        env_overrides: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            self._prepare_fake_gcloud(
                tmp_path,
                pool_state=pool_state,
                provider_state=provider_state,
                existing_condition=existing_condition,
                iam_api_enabled=iam_api_enabled,
                services_enable_activates_iam_api=services_enable_activates_iam_api,
                pool_describe_error=pool_describe_error,
                provider_describe_error=provider_describe_error,
                pool_undelete_fail=pool_undelete_fail,
                provider_undelete_fail=provider_undelete_fail,
                update_oidc_failures_remaining=update_oidc_failures_remaining,
            )
            return self._run_provision_in_dir(tmp_path, extra_args, env_overrides=env_overrides)

    def test_create_oidc_anchors_attribute_condition_to_owner_and_repo(self):
        proc, calls, final_state = self._run_provision(
            ["--gcp_project", "my-project", "--git_project", "gke-labs/kube-agents"],
            pool_state="",
            provider_state="",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}")

        pool_create_calls = [
            call for call in calls if call[:3] == ["iam", "workload-identity-pools", "create"]
        ]
        create_calls = [
            call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "create-oidc"]
        ]
        update_calls = [
            call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "update-oidc"]
        ]
        undelete_calls = [call for call in calls if "undelete" in call]
        self.assertEqual(len(pool_create_calls), 1, msg=f"Expected one pool create call, got: {calls}")
        self.assertEqual(len(create_calls), 1, msg=f"Expected one create-oidc call, got: {calls}")
        self.assertEqual(update_calls, [])
        self.assertEqual(undelete_calls, [])

        expected_condition = (
            "assertion.repository_owner == 'gke-labs' && assertion.repository == 'gke-labs/kube-agents'"
        )
        self.assertIn(f"--attribute-condition={expected_condition}", create_calls[0])
        self.assertEqual(final_state["provider_condition"], expected_condition)
        self._assert_pool_resource_bound(calls, proc.stdout, "gke-labs/kube-agents")
        for call in calls:
            self.assertNotIn("--attribute-condition=assertion.repository != ''", call)

    def test_existing_legacy_or_empty_provider_updates_attribute_condition(self):
        for existing_condition in ("assertion.repository != ''", ""):
            with self.subTest(existing_condition=existing_condition):
                proc, calls, final_state = self._run_provision(
                    ["--gcp_project=my-project", "--git_project=my-org/.github"],
                    pool_state="ACTIVE",
                    provider_state="ACTIVE",
                    existing_condition=existing_condition,
                )
                self.assertEqual(proc.returncode, 0, msg=f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}")

                create_calls = [
                    call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "create-oidc"]
                ]
                update_calls = [
                    call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "update-oidc"]
                ]
                undelete_calls = [call for call in calls if "undelete" in call]
                self.assertEqual(create_calls, [])
                self.assertEqual(undelete_calls, [])
                self.assertEqual(len(update_calls), 1, msg=f"Expected one update-oidc call, got: {calls}")

                expected_condition = (
                    "assertion.repository_owner == 'my-org' && assertion.repository == 'my-org/.github'"
                )
                self.assertIn("github-provider", update_calls[0])
                self.assertIn("--project=my-project", update_calls[0])
                self.assertIn("--location=global", update_calls[0])
                self.assertIn("--workload-identity-pool=github-actions-pool", update_calls[0])
                self.assertIn(f"--attribute-condition={expected_condition}", update_calls[0])
                self.assertEqual(final_state["provider_condition"], expected_condition)
                self._assert_pool_resource_bound(calls, proc.stdout, "my-org/.github")

    def test_existing_provider_with_matching_condition_skips_update(self):
        matching_condition = (
            "assertion.repository_owner == 'gke-labs' && assertion.repository == 'gke-labs/kube-agents'"
        )
        proc, calls, _ = self._run_provision(
            ["--gcp_project", "my-project", "--git_project", "gke-labs/kube-agents"],
            pool_state="ACTIVE",
            provider_state="ACTIVE",
            existing_condition=matching_condition,
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}")

        create_calls = [
            call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "create-oidc"]
        ]
        update_calls = [
            call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "update-oidc"]
        ]
        undelete_calls = [call for call in calls if "undelete" in call]
        pool_describe_calls = [
            call for call in calls if call[:3] == ["iam", "workload-identity-pools", "describe"]
        ]
        provider_describe_calls = [
            call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "describe"]
        ]
        self.assertEqual(create_calls, [])
        self.assertEqual(update_calls, [])
        self.assertEqual(undelete_calls, [])
        self.assertEqual(len(pool_describe_calls), 1)
        self.assertEqual(len(provider_describe_calls), 2)
        self._assert_pool_resource_bound(calls, proc.stdout, "gke-labs/kube-agents")

    def test_existing_active_provider_with_different_custom_condition_refuses_before_mutations(self):
        custom_conditions = [
            "assertion.repository_owner == 'gke-labs' && assertion.repository == 'gke-labs/kube-agents'",
            "assertion.repository in ['gke-labs/kube-agents', 'gke-labs/other-repo']",
        ]
        for existing_condition in custom_conditions:
            with self.subTest(existing_condition=existing_condition):
                proc, calls, _ = self._run_provision(
                    ["--gcp_project", "my-project", "--git_project", "other-org/fork"],
                    pool_state="ACTIVE",
                    provider_state="ACTIVE",
                    existing_condition=existing_condition,
                )
                self.assertNotEqual(proc.returncode, 0, msg="Expected non-zero exit when refusing overwrite")
                self.assertIn(existing_condition, proc.stderr)
                self.assertIn("Refusing to overwrite", proc.stderr)

                non_describe_calls = [
                    call
                    for call in calls
                    if call[:4] != ["iam", "workload-identity-pools", "providers", "describe"]
                ]
                self.assertEqual(
                    non_describe_calls,
                    [],
                    msg=f"Expected zero mutating gcloud calls before refusal, got: {non_describe_calls}",
                )

    def test_step4_refuses_custom_condition_after_preflight_disabled_iam_api(self):
        existing_condition = (
            "assertion.repository_owner == 'gke-labs' && assertion.repository == 'gke-labs/kube-agents'"
        )
        proc, calls, final_state = self._run_provision(
            ["--gcp_project", "my-project", "--git_project", "other-org/fork"],
            pool_state="ACTIVE",
            provider_state="ACTIVE",
            existing_condition=existing_condition,
            iam_api_enabled=False,
        )
        self.assertNotEqual(proc.returncode, 0, msg="Expected Step 4 refusal after IAM API was enabled")
        self.assertIn(existing_condition, proc.stderr)
        self.assertIn("Refusing to overwrite", proc.stderr)
        update_calls = [
            call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "update-oidc"]
        ]
        sa_binding_calls = [
            call for call in calls if call[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]
        ]
        self.assertEqual(update_calls, [])
        self.assertEqual(sa_binding_calls, [])
        self.assertEqual(final_state["provider_condition"], existing_condition)

    def test_preflight_and_step4_fail_closed_on_non_not_found_describe_errors(self):
        with self.subTest("preflight_provider_permission_denied"):
            proc, calls, _ = self._run_provision(
                ["--gcp_project", "my-project", "--git_project", "other-org/fork"],
                pool_state="ACTIVE",
                provider_state="ACTIVE",
                existing_condition="assertion.repository_owner == 'gke-labs' && assertion.repository == 'gke-labs/kube-agents'",
                provider_describe_error="PERMISSION_DENIED: caller lacks iam.workloadIdentityPoolProviders.get",
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("PERMISSION_DENIED", proc.stderr)
            self.assertEqual(len(calls), 1, msg=f"Expected fail-closed after single describe, got: {calls}")

        with self.subTest("step4_pool_permission_denied"):
            proc, calls, _ = self._run_provision(
                ["--gcp_project", "my-project", "--git_project", "gke-labs/kube-agents"],
                pool_state="",
                provider_state="",
                pool_describe_error="PERMISSION_DENIED: caller lacks iam.workloadIdentityPools.get",
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("PERMISSION_DENIED", proc.stderr)
            pool_create_calls = [
                call for call in calls if call[:3] == ["iam", "workload-identity-pools", "create"]
            ]
            self.assertEqual(pool_create_calls, [], msg=f"Should not fall through to pool create: {calls}")

        with self.subTest("step4_provider_service_disabled_fails_closed"):
            proc, calls, _ = self._run_provision(
                ["--gcp_project", "my-project", "--git_project", "gke-labs/kube-agents"],
                pool_state="ACTIVE",
                provider_state="",
                iam_api_enabled=False,
                services_enable_activates_iam_api=False,
            )
            self.assertNotEqual(proc.returncode, 0, msg="Step 4 describe_wif_provider 'false' must reject SERVICE_DISABLED")
            self.assertIn("SERVICE_DISABLED", proc.stderr)
            create_calls = [
                call for call in calls if call[:4] == ["iam", "workload-identity-pools", "providers", "create-oidc"]
            ]
            self.assertEqual(create_calls, [], msg=f"Should not fall through to create-oidc: {calls}")

        with self.subTest("preflight_disabled_iam_api_proceeds_to_enable_and_provision"):
            proc, calls, final_state = self._run_provision(
                ["--gcp_project", "my-project", "--git_project", "gke-labs/kube-agents"],
                pool_state="",
                provider_state="",
                iam_api_enabled=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}")
            self.assertTrue(any(call[:2] == ["services", "enable"] for call in calls))
            self.assertEqual(final_state["provider_state"], "ACTIVE")
            self.assertEqual(
                final_state["provider_condition"],
                "assertion.repository_owner == 'gke-labs' && assertion.repository == 'gke-labs/kube-agents'",
            )
            self._assert_pool_resource_bound(calls, proc.stdout, "gke-labs/kube-agents")

    def test_soft_deleted_pool_and_provider_reprovisioned_in_required_order(self):
        proc, calls, final_state = self._run_provision(
            ["--gcp_project", "my-project", "--git_project", "org-b/repo-b"],
            pool_state="DELETED",
            provider_state="DELETED",
            existing_condition="assertion.repository_owner == 'org-a' && assertion.repository == 'org-a/repo-a'",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}")

        pool_undelete_indices = [
            idx for idx, call in enumerate(calls) if call[:3] == ["iam", "workload-identity-pools", "undelete"]
        ]
        provider_undelete_indices = [
            idx
            for idx, call in enumerate(calls)
            if call[:4] == ["iam", "workload-identity-pools", "providers", "undelete"]
        ]
        update_indices = [
            idx
            for idx, call in enumerate(calls)
            if call[:4] == ["iam", "workload-identity-pools", "providers", "update-oidc"]
        ]
        self.assertEqual(len(pool_undelete_indices), 1, msg=f"Expected one pool undelete call, got: {calls}")
        self.assertEqual(len(provider_undelete_indices), 1, msg=f"Expected one provider undelete call, got: {calls}")
        self.assertEqual(len(update_indices), 1, msg=f"Expected one update-oidc call after undelete, got: {calls}")
        self.assertLess(pool_undelete_indices[0], provider_undelete_indices[0])
        self.assertLess(provider_undelete_indices[0], update_indices[0])
        self.assertEqual(
            final_state["provider_condition"],
            "assertion.repository_owner == 'org-b' && assertion.repository == 'org-b/repo-b'",
        )
        self._assert_pool_resource_bound(calls, proc.stdout, "org-b/repo-b")

    def test_soft_deleted_pool_undelete_failure_fails_loudly(self):
        proc, calls, final_state = self._run_provision(
            ["--gcp_project", "my-project", "--git_project", "gke-labs/kube-agents"],
            pool_state="DELETED",
            provider_state="DELETED",
            existing_condition="assertion.repository_owner == 'gke-labs' && assertion.repository == 'gke-labs/kube-agents'",
            pool_undelete_fail=True,
        )
        self.assertNotEqual(proc.returncode, 0, msg="Expected non-zero exit when pools undelete fails")
        self.assertNotIn("[SUCCESS]", proc.stdout)
        self.assertEqual(final_state["pool_state"], "DELETED")
        sa_binding_calls = [
            call for call in calls if call[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]
        ]
        self.assertEqual(sa_binding_calls, [], msg=f"Expected no SA bindings after failed pool undelete: {calls}")

    def test_soft_deleted_provider_undelete_failure_fails_loudly(self):
        matching_condition = (
            "assertion.repository_owner == 'gke-labs' && assertion.repository == 'gke-labs/kube-agents'"
        )
        proc, calls, final_state = self._run_provision(
            ["--gcp_project", "my-project", "--git_project", "gke-labs/kube-agents"],
            pool_state="ACTIVE",
            provider_state="DELETED",
            existing_condition=matching_condition,
            provider_undelete_fail=True,
        )
        self.assertNotEqual(proc.returncode, 0, msg="Expected non-zero exit when providers undelete fails")
        self.assertNotIn("[SUCCESS]", proc.stdout)
        self.assertEqual(final_state["provider_state"], "DELETED")
        sa_binding_calls = [
            call for call in calls if call[:3] == ["iam", "service-accounts", "add-iam-policy-binding"]
        ]
        self.assertEqual(sa_binding_calls, [], msg=f"Expected no SA bindings after failed undelete, got: {calls}")

    def test_soft_deleted_provider_rolls_back_on_update_oidc_failure_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            self._prepare_fake_gcloud(
                tmp_path,
                pool_state="DELETED",
                provider_state="DELETED",
                existing_condition="assertion.repository_owner == 'org-a' && assertion.repository == 'org-a/repo-a'",
                update_oidc_failures_remaining=1,
            )

            first_proc, first_calls, state_after_fail = self._run_provision_in_dir(
                tmp_path,
                ["--gcp_project", "my-project", "--git_project", "org-b/repo-b"],
            )
            self.assertNotEqual(first_proc.returncode, 0, msg="Expected non-zero exit when update-oidc fails")
            delete_calls = [
                call
                for call in first_calls
                if call[:4] == ["iam", "workload-identity-pools", "providers", "delete"]
            ]
            self.assertEqual(len(delete_calls), 1, msg=f"Expected rollback delete call, got: {first_calls}")
            self.assertIn("--quiet", delete_calls[0])
            self.assertEqual(
                state_after_fail["provider_state"],
                "DELETED",
                msg="Rollback must return soft-deleted provider to DELETED so old condition is not left ACTIVE",
            )

            retry_proc, retry_calls, state_after_retry = self._run_provision_in_dir(
                tmp_path,
                ["--gcp_project", "my-project", "--git_project", "org-b/repo-b"],
            )
            self.assertEqual(
                retry_proc.returncode,
                0,
                msg=f"Retry after rollback should succeed.\nstderr:\n{retry_proc.stderr}\nstdout:\n{retry_proc.stdout}",
            )
            self.assertEqual(state_after_retry["provider_state"], "ACTIVE")
            self.assertEqual(
                state_after_retry["provider_condition"],
                "assertion.repository_owner == 'org-b' && assertion.repository == 'org-b/repo-b'",
            )
            self._assert_pool_resource_bound(retry_calls, retry_proc.stdout, "org-b/repo-b")

    def test_rejects_malformed_or_cel_injecting_git_project(self):
        invalid_repos = [
            "no-slash",
            "gke-labs/",
            "/kube-agents",
            "gke-labs/kube-agents/extra",
            "gke-labs/kube-agents' || true || '",
            "gke-labs/..",
            "gke-labs/.",
            "-bad-owner/repo",
            "gke-labs/bad repo",
            "owner/café",
        ]
        for repo in invalid_repos:
            with self.subTest(git_project=repo):
                proc, calls, _ = self._run_provision(
                    ["--gcp_project", "my-project", "--git_project", repo],
                    pool_state="",
                    provider_state="",
                    env_overrides={"LC_ALL": "en_US.UTF-8"},
                )
                self.assertNotEqual(proc.returncode, 0, msg=f"Expected failure for invalid --git_project={repo!r}")
                self.assertIn("Invalid --git_project", proc.stderr)
                self.assertEqual(calls, [], msg=f"Expected no gcloud calls for invalid --git_project={repo!r}")


if __name__ == "__main__":
    unittest.main()

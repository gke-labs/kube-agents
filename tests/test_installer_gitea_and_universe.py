"""Tests for Google Cloud Dedicated / Sovereign Cloud (universe_domain),
partitioned project IDs, u-prefixed regions/zones, custom VPC selection,
sovereign model-egress consent, and self-managed Gitea installer support.
"""

from __future__ import annotations

import json
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import test_installer_common as _installer_common
from tests.test_install_script import (
    _INSTALL_SH,
    _INSTALLER_COMMON,
    _REPO_ROOT,
    _run_installer_bash,
)
from tests.testing.common import get_isolated_test_env

_SCRIPTS_DIR = _REPO_ROOT / "agents" / "platform" / "scripts"
_FLEET_AUDIT_SCRIPTS_DIR = _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
for _p in (str(_SCRIPTS_DIR), str(_FLEET_AUDIT_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import api_policy  # noqa: E402
import cluster_agent_profile  # noqa: E402
import collect as fleet_collect  # noqa: E402
import credential_proxy  # noqa: E402
import credential_proxy_client  # noqa: E402
import platform_mcp_server  # noqa: E402
import scope_exclude  # noqa: E402
import scope_policy  # noqa: E402
import scoped_sa_pool  # noqa: E402

_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"
_UNINSTALL_SH = _REPO_ROOT / "uninstall.sh"
_CHART_DIR = _REPO_ROOT / "charts" / "kube-agents"
_KUBECTL_REGISTERED_SH = _REPO_ROOT / "deploy" / "sandbox" / "kubectl_context_registered.sh"

SOVEREIGN_UNIVERSE = "universe.example.test"
PARTITIONED_PROJECT = "part0:sample-project"
PARTITIONED_WIF_POOL = "sample-project.part0.svc.id.goog"
PARTITIONED_GSA_DOMAIN = "sample-project.part0"
PARTITIONED_BUCKET = "part0-sample-project-kube-agents-tfstate"
SOVEREIGN_REGION = "u-region-north1"
SOVEREIGN_ZONE = "u-region-north1-a"
GITEA_HOST = "gitea.example.test"
GITEA_REPO = "platform/gitops"

_FORGE_SOURCE_VARS = (
    "GITLAB_READ_TOKEN_FILE",
    "GITLAB_TOKEN_FILE",
    "GITOPS_CA_FILE",
    "GITOPS_TOKEN_FILE",
    "GITOPS_READ_TOKEN_FILE",
    "GITOPS_TOKEN_SECRET",
    "GITOPS_IDENTITY",
    "UNIVERSE_DOMAIN",
    "VPC_NETWORK",
    "VPC_SUBNETWORK",
    "ALLOW_EXTERNAL_MODEL_EGRESS",
)


class SovereignRuntimeHelpersTest(unittest.TestCase):
    """Partitioned project IDs, u-prefixed locations, and kubeconfig path safety."""

    def test_api_policy_matches_partitioned_project_and_u_location(self) -> None:
        proj_re = re.compile(rf"^{api_policy.PROJECT}$")
        loc_re = re.compile(rf"^{api_policy.GKE_LOCATION}$")
        self.assertRegex(PARTITIONED_PROJECT, proj_re)
        self.assertRegex("standard-project-01", proj_re)
        self.assertNotRegex("-bad-proj", proj_re)
        self.assertRegex(SOVEREIGN_REGION, loc_re)
        self.assertRegex(SOVEREIGN_ZONE, loc_re)

    def test_scope_exclude_accepts_partitioned_project_id(self) -> None:
        doc = {
            "exclude": {
                "clusters": [
                    {
                        "projectId": PARTITIONED_PROJECT,
                        "location": SOVEREIGN_REGION,
                        "clusterName": "host-a",
                    }
                ]
            }
        }
        triples = scope_exclude.parse_exclusions(doc)
        self.assertEqual(triples, frozenset({(PARTITIONED_PROJECT, SOVEREIGN_REGION, "host-a")}))

    def test_scope_policy_accepts_partitioned_project_id(self) -> None:
        self.assertIsNotNone(scope_policy._PROJECT.match(PARTITIONED_PROJECT))
        self.assertIsNone(scope_policy._PROJECT.match("BAD:project"))

    def test_scoped_sa_pool_accepts_partitioned_project_and_gsa_email(self) -> None:
        pool_doc = {
            "version": 2,
            "serviceAccounts": [
                {
                    "projectId": PARTITIONED_PROJECT,
                    "serviceAccountEmail": f"ka-sample-12345678@{PARTITIONED_GSA_DOMAIN}.iam.gserviceaccount.com",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            pool_file = pathlib.Path(tmp) / "pool.json"
            pool_file.write_text(json.dumps(pool_doc), encoding="utf-8")
            loaded = scoped_sa_pool.load_pool_file(pool_file)
            self.assertIn(PARTITIONED_PROJECT, loaded)

    def test_cluster_agent_profile_validates_partitioned_project(self) -> None:
        cluster_agent_profile._validate(PARTITIONED_PROJECT, "project")
        with self.assertRaises(SystemExit):
            cluster_agent_profile._validate(PARTITIONED_PROJECT, "cluster")

    def test_credential_proxy_client_and_broker_normalize_partitioned_kubeconfig_paths(self) -> None:
        target = credential_proxy_client.ClusterTarget(
            project=PARTITIONED_PROJECT,
            location=SOVEREIGN_REGION,
            cluster="host-a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"HERMES_HOME": tmp}, clear=False):
                per_target = credential_proxy_client.per_target_kubeconfig_path(target)
                self.assertNotIn(":", per_target.name)
                self.assertEqual(
                    per_target.name,
                    f"kubeconfig_part0_sample-project_host-a_{SOVEREIGN_REGION}.yaml",
                )
                # Raw `${PROJECT}` path from prompt/SOP instructions normalizes onto `_`.
                raw_env_kc = f"{tmp}/.kubeconfigs/kubeconfig_{PARTITIONED_PROJECT}_host-a_{SOVEREIGN_REGION}.yaml"
                self.assertEqual(
                    credential_proxy_client.normalize_partitioned_kubeconfig_path(raw_env_kc),
                    str(per_target),
                )
                # Multi-file KUBECONFIG list is NOT rewritten and stays rejected.
                multi_kc = f"{tmp}/first.yaml:{tmp}/second.yaml"
                self.assertEqual(
                    credential_proxy_client.normalize_partitioned_kubeconfig_path(multi_kc),
                    multi_kc,
                )
                with self.assertRaises(credential_proxy_client.KubeconfigUnreadable):
                    credential_proxy_client.kubeconfig_context(multi_kc)

            # CommandExecutor._managed_kubeconfig maps ':' to '_' as well.
            executor = object.__new__(credential_proxy.CommandExecutor)
            executor.kubeconfig_dir = pathlib.Path(tmp)
            managed = executor._managed_kubeconfig(target)
            self.assertEqual(
                managed.name,
                f"gke_part0_sample-project_{SOVEREIGN_REGION}_host-a.yaml",
            )

    def test_get_credentials_local_target_and_kubectl_registered_accept_partitioned_project(self) -> None:
        for proj in (PARTITIONED_PROJECT, "p:sample-project"):
            target = credential_proxy_client.get_credentials_local_target(
                [
                    "gcloud",
                    "container",
                    "clusters",
                    "get-credentials",
                    "host-a",
                    f"--region={SOVEREIGN_REGION}",
                    f"--project={proj}",
                ]
            )
            self.assertIsNotNone(target, proj)
            assert target is not None
            self.assertEqual(target.project, proj)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            ca_file = tmp_path / "ca.crt"
            tok_file = tmp_path / "tok"
            ca_file.write_text("ca", encoding="utf-8")
            tok_file.write_text("tok", encoding="utf-8")
            kc_dir = tmp_path / ".kubeconfigs"
            kc_dir.mkdir()
            kc_file = kc_dir / f"kubeconfig_part0_sample-project_host-a_{SOVEREIGN_REGION}.yaml"
            env = {
                "HOME": tmp,
                "KUBE_PROXY_URL": "https://proxy.kubeagents-system.svc:8443",
                "KUBE_PROXY_CA_FILE": str(ca_file),
                "CREDENTIAL_PROXY_TOKEN_FILE": str(tok_file),
                "KUBECONFIG": str(kc_file),
            }
            ctx = f"gke_{PARTITIONED_PROJECT}_{SOVEREIGN_REGION}_host-a"
            with mock.patch.dict("os.environ", env, clear=False):
                kc_file.write_text(
                    credential_proxy_client.build_kube_proxy_kubeconfig(ctx),
                    encoding="utf-8",
                )
                credential_proxy_client.ensure_kube_proxy_context_for_argv(
                    ["kubectl", f"--context={ctx}", "get", "pods"]
                )
            # Both sanitized KUBECONFIG and raw `${PROJECT}` KUBECONFIG hit the fast-path marker!
            for kc_env in (
                str(kc_file),
                str(kc_dir / f"kubeconfig_{PARTITIONED_PROJECT}_host-a_{SOVEREIGN_REGION}.yaml"),
            ):
                proc = subprocess.run(
                    ["sh", str(_KUBECTL_REGISTERED_SH), f"--context={ctx}", "get", "pods"],
                    env={**get_isolated_test_env(), **env, "KUBECONFIG": kc_env},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 0, f"{kc_env}: {proc.stderr}")
            # Multi-file KUBECONFIG still exits 1.
            bad = subprocess.run(
                ["sh", str(_KUBECTL_REGISTERED_SH), f"--context={ctx}", "get", "pods"],
                env={**get_isolated_test_env(), **env, "KUBECONFIG": f"{kc_file}:{tmp_path}/other.yaml"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(bad.returncode, 1)

    def test_platform_mcp_server_accepts_u_prefixed_zone(self) -> None:
        with mock.patch.object(platform_mcp_server, "get_valid_regions", return_value=[SOVEREIGN_REGION]):
            self.assertEqual(platform_mcp_server.validate_location(SOVEREIGN_ZONE, PARTITIONED_PROJECT), "")
            self.assertEqual(platform_mcp_server.validate_location(SOVEREIGN_REGION, PARTITIONED_PROJECT), "")
            self.assertIn("Invalid GKE location", platform_mcp_server.validate_location("u-other-south1-a", PARTITIONED_PROJECT))

    def test_fleet_audit_collect_extracts_partitioned_refused_project(self) -> None:
        msg = f"API has not been used in project {PARTITIONED_PROJECT} before"
        match = fleet_collect.REFUSED_PROJECT_ID_RE.search(msg)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), PARTITIONED_PROJECT)


class GithubMinterChartPartitionedProjectTest(unittest.TestCase):
    """Helm template rendering for github-minter with standard and partitioned projects."""

    def _render_minter(self, proj: str) -> str:
        proc = subprocess.run(
            [
                "helm",
                "template",
                "test-release",
                str(_CHART_DIR),
                "--show-only",
                "templates/github-minter.yaml",
                "--set",
                "githubMinter.enabled=true",
                "--set",
                "githubMinter.org=acme",
                "--set",
                "githubMinter.repo=infra",
                "--set",
                f"platformAgent.harness.projectId={proj}",
                "--set",
                f"platformAgent.harness.location={SOVEREIGN_REGION}",
                "--set",
                "platformAgent.harness.clusterName=host-a",
                "--set",
                "platformAgent.integration.github.gitRepo=acme/infra",
                "--set",
                "platformAgent.integration.github.org=acme",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def test_standard_and_partitioned_project_ids_render_expected_gsa_domains(self) -> None:
        std_out = self._render_minter("standard-proj")
        self.assertIn("kubeagents-github-minter-gsa@standard-proj.iam.gserviceaccount.com", std_out)
        self.assertIn("kubeagents-platform-gsa@standard-proj.iam.gserviceaccount.com", std_out)

        part_out = self._render_minter(PARTITIONED_PROJECT)
        self.assertIn(
            f"kubeagents-github-minter-gsa@{PARTITIONED_GSA_DOMAIN}.iam.gserviceaccount.com",
            part_out,
        )
        self.assertIn(
            f"kubeagents-platform-gsa@{PARTITIONED_GSA_DOMAIN}.iam.gserviceaccount.com",
            part_out,
        )


class InstallerUniverseAndGiteaTest(unittest.TestCase):
    _run_common = _installer_common.InstallerCommonTest._run

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._lock_file = self._tmp / "install.lock"
        self._empty_install_env = self._tmp / "install.env"
        self._empty_install_env.write_text("", encoding="utf-8")

    def _run_install_func(self, func_call: str, env: dict[str, str] | None = None):
        setup = (
            f'source "{_INSTALLER_COMMON}"\n'
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"{func_call}\n"
        )
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
            "KUBE_AGENTS_LOCK_FILE": str(self._lock_file),
        }
        overrides.update(env or {})
        return _run_installer_bash(
            setup,
            get_isolated_test_env(overrides=overrides, absent=_FORGE_SOURCE_VARS),
        )

    def _tfvars(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        out_dir = self._tmp / "tfvars-out"
        out_dir.mkdir(exist_ok=True)
        dest = out_dir / "terraform.tfvars"
        rm_f = f'rm -f "{dest}"'
        return self._run_common(
            f'print_info() {{ echo "INFO: $*"; }}; {rm_f}; '
            f'if write_tfvars_from_state "{dest}"; then rc=0; else rc=$?; fi; '
            f'echo "rc=$rc"; [ -f "{dest}" ] && cat "{dest}" || true',
            env={"API_SERVER_KEY": "dummy-val", **env},
            describe_stub=_installer_common._autopilot_describe_stub(),
        )

    def test_universe_and_partition_helpers_in_installer_common(self) -> None:
        proc = self._run_common(
            f'echo "valid_std=$(is_valid_universe_domain googleapis.com && echo y || echo n)"\n'
            f'echo "valid_sov=$(is_valid_universe_domain {SOVEREIGN_UNIVERSE} && echo y || echo n)"\n'
            'echo "valid_bad=$(is_valid_universe_domain https://bad/path && echo y || echo n)"\n'
            f'echo "custom_sov=$(is_custom_universe_domain {SOVEREIGN_UNIVERSE} && echo y || echo n)"\n'
            'echo "custom_std=$(is_custom_universe_domain googleapis.com && echo y || echo n)"\n'
            f'echo "wif=$(gcp_workload_identity_pool {PARTITIONED_PROJECT})"\n'
            f'echo "gsa=$(gcp_gsa_email_domain {PARTITIONED_PROJECT})"\n'
            f'echo "bucket=$(PROJECT_ID={PARTITIONED_PROJECT} KUBE_AGENTS_STATE_BUCKET=auto tf_state_bucket)"\n'
            f'echo "reg=$(location_is_region {SOVEREIGN_REGION} && echo y || echo n)"\n'
            f'echo "zone=$(location_is_region {SOVEREIGN_ZONE} && echo y || echo n)"\n'
            f'echo "rm_url=$(UNIVERSE_DOMAIN={SOVEREIGN_UNIVERSE} resource_manager_api_url)"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("valid_std=y", proc.stdout)
        self.assertIn("valid_sov=y", proc.stdout)
        self.assertIn("valid_bad=n", proc.stdout)
        self.assertIn("custom_sov=y", proc.stdout)
        self.assertIn("custom_std=n", proc.stdout)
        self.assertIn(f"wif={PARTITIONED_WIF_POOL}", proc.stdout)
        self.assertIn(f"gsa={PARTITIONED_GSA_DOMAIN}", proc.stdout)
        self.assertIn(f"bucket={PARTITIONED_BUCKET}", proc.stdout)
        self.assertIn("reg=y", proc.stdout)
        self.assertIn("zone=n", proc.stdout)
        self.assertIn(f"rm_url=https://cloudresourcemanager.{SOVEREIGN_UNIVERSE}/v3", proc.stdout)

    def test_configure_installer_universe_env_never_mutates_adc_and_mints_bearer(self) -> None:
        adc_file = self._tmp / "application_default_credentials.json"
        original_adc = json.dumps({"type": "authorized_user", "universe_domain": "googleapis.com"})
        adc_file.write_text(original_adc, encoding="utf-8")

        proc = self._run_install_func(
            'gcloud() { if [[ "$*" == *"auth print-access-token"* ]]; then echo "ephemeral-oauth-val"; fi; }\n'
            f'GOOGLE_APPLICATION_CREDENTIALS="{adc_file}" configure_installer_universe_env "{SOVEREIGN_UNIVERSE}"\n'
            'echo "UD=$CLOUDSDK_CORE_UNIVERSE_DOMAIN GCUD=$GOOGLE_CLOUD_UNIVERSE_DOMAIN HAS_BEARER=$([ -n "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ] && echo yes || echo no)"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"UD={SOVEREIGN_UNIVERSE} GCUD={SOVEREIGN_UNIVERSE} HAS_BEARER=yes", proc.stdout)
        self.assertEqual(adc_file.read_text(encoding="utf-8"), original_adc)

        # Explicit GOOGLE_CREDENTIALS suppresses GOOGLE_OAUTH_ACCESS_TOKEN minting so Terraform provider init never sees both.
        with_creds = self._run_install_func(
            'gcloud() { echo "should-not-mint"; }\n'
            f'GOOGLE_CREDENTIALS="/tmp/sa.json" GOOGLE_APPLICATION_CREDENTIALS="{adc_file}" configure_installer_universe_env "{SOVEREIGN_UNIVERSE}"\n'
            'echo "HAS_BEARER=$([ -n "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ] && echo yes || echo no)"\n'
        )
        self.assertEqual(with_creds.returncode, 0, with_creds.stderr)
        self.assertIn("HAS_BEARER=no", with_creds.stdout)

    def test_tfvars_preserves_commercial_output_and_gates_sovereign_egress(self) -> None:
        # Commercial default emits none of the sovereign keys.
        std = self._tfvars({"MODEL_PROVIDER": "gemini"})
        self.assertIn("rc=0", std.stdout, std.stderr)
        self.assertNotIn("universe_domain", std.stdout)
        self.assertNotIn("vpc_network", std.stdout)
        self.assertNotIn("allow_external_model_egress", std.stdout)

        # Custom universe + external SaaS provider (including non-table slugs like sap) without consent is refused.
        for ext_provider, extra_env in (
            ("gemini", {}),
            ("sap", {"MODEL_DEFAULT_NAME": "gpt-4o", "LITELLM_ENV_SECRET_NAME": "sap-creds"}),
        ):
            with self.subTest(provider=ext_provider):
                refused = self._tfvars(
                    {
                        "UNIVERSE_DOMAIN": SOVEREIGN_UNIVERSE,
                        "MODEL_PROVIDER": ext_provider,
                        **extra_env,
                    }
                )
                self.assertIn("rc=1", refused.stdout, refused.stderr)
                self.assertIn("--allow-external-model-egress", refused.stdout + refused.stderr)

        # Custom universe + explicit consent + VPC + Gitea renders all fields cleanly.
        accepted = self._tfvars(
            {
                "UNIVERSE_DOMAIN": SOVEREIGN_UNIVERSE,
                "VPC_NETWORK": "platform-net",
                "VPC_SUBNETWORK": "platform-subnet",
                "MODEL_PROVIDER": "gemini",
                "ALLOW_EXTERNAL_MODEL_EGRESS": "true",
                "GITOPS_FORGE": "gitea",
                "GITOPS_HOST": GITEA_HOST,
                "GITOPS_REPO": GITEA_REPO,
                "GITOPS_CA_SECRET": "gitea-forge-ca",
            }
        )
        self.assertIn("rc=0", accepted.stdout, accepted.stderr)
        self.assertIn(f'universe_domain = "{SOVEREIGN_UNIVERSE}"', accepted.stdout)
        self.assertIn("allow_external_model_egress = true", accepted.stdout)
        self.assertIn('vpc_network    = "platform-net"', accepted.stdout)
        self.assertIn('vpc_subnetwork = "platform-subnet"', accepted.stdout)
        self.assertIn('gitops_forge              = "gitea"', accepted.stdout)
        self.assertIn(f'gitops_host               = "{GITEA_HOST}"', accepted.stdout)
        self.assertIn(f'gitops_repo_path          = "{GITEA_REPO}"', accepted.stdout)
        self.assertIn('gitops_token_secret_name  = "gitea-forge-token"', accepted.stdout)
        self.assertIn('gitops_ca_secret_name     = "gitea-forge-ca"', accepted.stdout)

        # Uninstall destroy bypasses sovereign model-egress refusal and satisfies Terraform precondition.
        destroy = self._tfvars(
            {
                "UNIVERSE_DOMAIN": SOVEREIGN_UNIVERSE,
                "MODEL_PROVIDER": "gemini",
                "KUBE_AGENTS_UNINSTALL_DESTROY": "true",
            }
        )
        self.assertIn("rc=0", destroy.stdout, destroy.stderr)
        self.assertIn("allow_external_model_egress = true", destroy.stdout)

    def test_gitea_validator_and_forge_menu(self) -> None:
        ca_pem = self._tmp / "ca.pem"
        ca_pem.write_text(
            "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
            encoding="utf-8",
        )
        proc = self._run_install_func(
            f'PARAM_GITOPS_FORGE=gitea PARAM_GITOPS_REPO="https://{GITEA_HOST}/{GITEA_REPO}.git" '
            f'PARAM_GITOPS_CA_FILE="{ca_pem}" PARAM_NON_INTERACTIVE=true\n'
            "validate_gitops_forge_flags && rc=0 || rc=$?\n"
            'echo "rc=$rc host=$PARAM_GITOPS_HOST repo=$PARAM_GITOPS_REPO ca=$PARAM_GITOPS_CA_SECRET tok=$PARAM_GITLAB_TOKEN_SECRET"\n'
        )
        self.assertIn(
            f"rc=0 host={GITEA_HOST} repo={GITEA_REPO} ca=gitea-forge-ca tok=gitea-forge-token",
            proc.stdout,
            proc.stderr + proc.stdout,
        )

        # Refusals: missing host, reserved GitHub domain, >40-char namespace, --gitops-identity.
        for label, extra, expected in (
            ("missing host", "PARAM_GITOPS_HOST='' PARAM_GITOPS_REPO='platform/gitops'", "needs --gitops-host"),
            ("reserved host", "PARAM_GITOPS_HOST='github.com' PARAM_GITOPS_REPO='platform/gitops'", "hosted service"),
            (
                "namespace over 40 chars",
                f"PARAM_GITOPS_HOST='{GITEA_HOST}' PARAM_GITOPS_REPO='{'a' * 41}/gitops'",
                "<= 40",
            ),
            (
                "identity unsupported",
                f"PARAM_GITOPS_HOST='{GITEA_HOST}' PARAM_GITOPS_REPO='platform/gitops' PARAM_GITOPS_IDENTITY='bot'",
                "--gitops-identity applies only",
            ),
        ):
            with self.subTest(label):
                bad = self._run_install_func(
                    f"PARAM_GITOPS_FORGE=gitea PARAM_NON_INTERACTIVE=true {extra}\n"
                    "if validate_gitops_forge_flags; then rc=0; else rc=1; fi\n"
                    'echo "rc=$rc"\n'
                )
                self.assertIn("rc=1", bad.stdout, bad.stderr + bad.stdout)
                self.assertIn(expected, bad.stdout + bad.stderr)

        # Forge menu: option 4 stays "Nowhere yet" and option 5 selects Gitea and updates token Secret default.
        menu = self._run_install_func(
            'has_controlling_tty() { return 0; }\n'
            'PARAM_NON_INTERACTIVE=false PARAM_GITOPS_FORGE_GIVEN=false PARAM_GITOPS_FORGE=github\n'
            'PARAM_GITLAB_TOKEN_SECRET=gitlab-forge-token PARAM_GITLAB_TOKEN_SECRET_FROM_FLAG=false\n'
            'prompt_menu() { local v="${!#}"; printf -v "$v" "5"; }\n'
            "ask_gitops_forge\n"
            'echo "FORGE=$PARAM_GITOPS_FORGE SECRET=$PARAM_GITLAB_TOKEN_SECRET"\n'
        )
        self.assertIn("FORGE=gitea SECRET=gitea-forge-token", menu.stdout, menu.stderr)

    def test_load_install_env_unsets_ambient_sovereign_and_vpc_vars(self) -> None:
        minimal_env = self._tmp / "minimal.env"
        minimal_env.write_text("PROJECT_ID=std-proj\nCLUSTER_NAME=host-a\nREGION=us-central1\n", encoding="utf-8")
        proc = self._run_common(
            f'export UNIVERSE_DOMAIN="{SOVEREIGN_UNIVERSE}" VPC_NETWORK="stale-net" '
            'VPC_SUBNETWORK="stale-sub" ALLOW_EXTERNAL_MODEL_EGRESS="true"\n'
            f'load_install_env "{minimal_env}"\n'
            'echo "UD=${UNIVERSE_DOMAIN:-<unset>} NET=${VPC_NETWORK:-<unset>} SUB=${VPC_SUBNETWORK:-<unset>} EGRESS=${ALLOW_EXTERNAL_MODEL_EGRESS:-<unset>}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("UD=<unset> NET=<unset> SUB=<unset> EGRESS=<unset>", proc.stdout)

    def test_select_gcp_region_and_vpc_resolution_in_custom_universe(self) -> None:
        proc = self._run_install_func(
            f'echo "proj_ok=$(is_valid_project_id {PARTITIONED_PROJECT} && echo y || echo n)"\n'
            'gcloud() {\n'
            '  case "$*" in\n'
            f'    *"compute regions list"*) printf "%s\\n" "{SOVEREIGN_REGION}" ;;\n'
            '    *"compute networks describe default"*) echo "was not found" >&2; return 1 ;;\n'
            '    *"compute networks subnets list"*) printf "platform-net\\tplatform-subnet\\n" ;;\n'
            '  esac\n'
            '}\n'
            'PARAM_NON_INTERACTIVE=true PARAM_REGION=""\n'
            f'select_gcp_region reg "{PARTITIONED_PROJECT}" "{SOVEREIGN_UNIVERSE}" ""\n'
            'echo "REG=$reg"\n'
            'net="" sub=""\n'
            f'if resolve_cluster_vpc_network net sub "{PARTITIONED_PROJECT}" "{SOVEREIGN_REGION}" "{SOVEREIGN_UNIVERSE}" true; then vrc=0; else vrc=1; fi\n'
            'echo "VRC=$vrc"\n'
        )
        self.assertIn("proj_ok=y", proc.stdout, proc.stderr)
        self.assertIn(f"REG={SOVEREIGN_REGION}", proc.stdout, proc.stderr)
        self.assertIn("VRC=1", proc.stdout, proc.stderr)
        self.assertIn("--vpc-network=platform-net --vpc-subnetwork=platform-subnet", proc.stdout + proc.stderr)

    def test_upgrade_and_uninstall_check_universe_domain_coordinate_conflicts(self) -> None:
        recorded_env = self._tmp / "recorded.env"
        recorded_env.write_text(
            f"PROJECT_ID={PARTITIONED_PROJECT}\n"
            "CLUSTER_NAME=host-a\n"
            f"REGION={SOVEREIGN_REGION}\n"
            f"UNIVERSE_DOMAIN={SOVEREIGN_UNIVERSE}\n",
            encoding="utf-8",
        )
        # uninstall.sh exposes check_uninstall_coordinate_conflicts directly (exits non-zero on conflict).
        uninst = _run_installer_bash(
            f'source "{_INSTALLER_COMMON}"\n'
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UNINSTALL_SH}"\n'
            f'load_install_env "{recorded_env}"\n'
            'PARAM_UNIVERSE_DOMAIN="other.universe.test"\n'
            f'check_uninstall_coordinate_conflicts "{recorded_env}" false\n',
            get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(recorded_env)}),
        )
        self.assertNotEqual(uninst.returncode, 0, uninst.stdout + uninst.stderr)
        self.assertIn("--universe-domain=other.universe.test", uninst.stdout + uninst.stderr)

        # upgrade.sh checks coordinate conflicts inside main() and refuses on --plan.
        # Provide stub CLI binaries on PATH because main() re-sources installer_common.sh
        # from the acquired source tree (resetting shell function overrides).
        stub_bin = self._tmp / "stub-bin"
        stub_bin.mkdir(exist_ok=True)
        for tool in ("terraform", "gcloud", "helm", "kubectl", "jq"):
            p = stub_bin / tool
            p.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            p.chmod(0o755)
        upg_env = get_isolated_test_env(
            overrides={
                "KUBE_AGENTS_INSTALL_ENV": str(recorded_env),
                "KUBE_AGENTS_LOCK_FILE": str(self._lock_file),
                "PATH": f"{stub_bin}:/usr/bin:/bin",
            }
        )
        upg = _run_installer_bash(
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"\n'
            f'acquire_upgrade_sources() {{ printf -v "$1" "%s" "{_REPO_ROOT}"; printf -v "$2" "%s" ""; }}\n'
            "require_min_gcloud_version() { return 0; }\n"
            "require_min_terraform_version() { return 0; }\n"
            'main --plan --image-tag=1.0.0 --universe-domain=other.universe.test\n',
            upg_env,
        )
        self.assertNotEqual(upg.returncode, 0, upg.stdout + upg.stderr)
        self.assertIn("--universe-domain=other.universe.test", upg.stdout + upg.stderr)

    def test_partitioned_service_agent_domain_and_manage_iam_false(self) -> None:
        dom = self._run_common(
            f'echo "part=$(gcp_service_agent_domain {PARTITIONED_PROJECT})"\n'
            'echo "std=$(gcp_service_agent_domain standard-proj)"\n'
        )
        self.assertEqual(dom.returncode, 0, dom.stderr)
        self.assertIn("part=part0-system.iam.gserviceaccount.com", dom.stdout)
        self.assertIn("std=iam.gserviceaccount.com", dom.stdout)

        # MANAGE_IAM=false writes manage_iam = false into terraform.tfvars; default omits it.
        tfv_default = self._tfvars({"PROJECT_ID": PARTITIONED_PROJECT})
        self.assertEqual(tfv_default.returncode, 0, tfv_default.stderr)
        self.assertNotIn("manage_iam", tfv_default.stdout)

        tfv_disabled = self._tfvars({"PROJECT_ID": PARTITIONED_PROJECT, "MANAGE_IAM": "false"})
        self.assertEqual(tfv_disabled.returncode, 0, tfv_disabled.stderr)
        self.assertIn("manage_iam                       = false", tfv_disabled.stdout)

        # check_service_account_ownership accepts a pre-provisioned GSA when MANAGE_IAM=false,
        # and refuses when gcloud reports NOT_FOUND.
        ok_check = self._run_install_func(
            'gcloud() { return 0; }\n'
            'tf_state_service_account_ids() { return 0; }\n'
            f'PROJECT_ID="{PARTITIONED_PROJECT}" MANAGE_IAM="false" check_service_account_ownership\n'
            'echo "OWN_OK=$?"\n'
        )
        self.assertEqual(ok_check.returncode, 0, ok_check.stderr)
        self.assertIn("OWN_OK=0", ok_check.stdout)

        missing_check = self._run_install_func(
            'gcloud() {\n'
            '  if [[ "$*" == *"iam service-accounts describe"* ]]; then\n'
            '    echo "ERROR: (gcloud.iam.service-accounts.describe) NOT_FOUND: Service account does not exist" >&2\n'
            '    return 1\n'
            '  fi\n'
            '  return 0\n'
            '}\n'
            'tf_state_service_account_ids() { return 0; }\n'
            f'PROJECT_ID="{PARTITIONED_PROJECT}" MANAGE_IAM="false"\n'
            'if check_service_account_ownership; then rc=0; else rc=1; fi\n'
            'echo "OWN_MISSING=$rc"\n'
        )
        self.assertIn("OWN_MISSING=1", missing_check.stdout, missing_check.stderr)
        self.assertIn("MANAGE_IAM=false requires pre-provisioned service account", missing_check.stdout + missing_check.stderr)

    def test_validate_gitea_forge_flags_runs_before_installer_common_sourced(self) -> None:
        # In main(), validate_gitops_forge_flags runs before source_provisioning_helpers
        # loads scripts/installer/installer_common.sh, so it must not reference constants
        # defined only in installer_common.sh / universe_env.sh under set -u.
        proc = _run_installer_bash(
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'PARAM_NON_INTERACTIVE="true"\n'
            'PARAM_GITOPS_FORGE="gitea"\n'
            'PARAM_GITOPS_HOST="gitea.example.test"\n'
            'PARAM_GITOPS_REPO="platform/org-mono-repo"\n'
            'validate_gitops_forge_flags\n'
            'echo "EARLY_GITEA_OK=$?"\n',
            get_isolated_test_env(
                overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)},
                absent=_FORGE_SOURCE_VARS,
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("EARLY_GITEA_OK=0", proc.stdout)

    def test_manage_iam_false_skips_container_set_iam_probe_and_checks_litellm_gsa(self) -> None:
        # Under MANAGE_IAM=false, check_scope_container_access still verifies
        # Cloud Asset API availability but must not refuse when the applying
        # identity lacks resourcemanager.folders.setIamPolicy.
        scope_proc = self._run_install_func(
            "_scope_asset_api_enabled() { return 0; }\n"
            "_scope_container_can_set_iam() { return 1; }\n"
            f'PROJECT_ID="{PARTITIONED_PROJECT}" MANAGE_IAM="false" '
            'SCOPE_FOLDERS="123456789012" SCOPED_SA_POOL_ENABLED="false" '
            "check_scope_container_access\n"
            'echo "SCOPE_OK=$?"\n'
        )
        self.assertEqual(scope_proc.returncode, 0, scope_proc.stderr)
        self.assertIn("SCOPE_OK=0", scope_proc.stdout)

        # Under MANAGE_IAM=false + MODEL_PROVIDER=vertex_ai, pre-provisioned
        # agent and LiteLLM GSAs pass without triggering foreign-collision errors,
        # while a missing LiteLLM GSA fails fast.
        vertex_ok = self._run_install_func(
            "gcloud() { return 0; }\n"
            "tf_state_service_account_ids() { return 0; }\n"
            f'PROJECT_ID="{PARTITIONED_PROJECT}" MANAGE_IAM="false" '
            'MODEL_PROVIDER="vertex_ai" check_service_account_ownership\n'
            'echo "VERTEX_OWN_OK=$?"\n'
        )
        self.assertEqual(vertex_ok.returncode, 0, vertex_ok.stderr)
        self.assertIn("VERTEX_OWN_OK=0", vertex_ok.stdout)

        vertex_missing = self._run_install_func(
            "gcloud() {\n"
            '  if [[ "$*" == *"kubeagents-litellm-gsa@"* ]]; then\n'
            '    echo "ERROR: NOT_FOUND: Service account does not exist" >&2\n'
            "    return 1\n"
            "  fi\n"
            "  return 0\n"
            "}\n"
            "tf_state_service_account_ids() { return 0; }\n"
            f'PROJECT_ID="{PARTITIONED_PROJECT}" MANAGE_IAM="false" '
            'MODEL_PROVIDER="vertex_ai"\n'
            "if check_service_account_ownership; then rc=0; else rc=1; fi\n"
            'echo "VERTEX_OWN_MISSING=$rc"\n'
        )
        self.assertIn("VERTEX_OWN_MISSING=1", vertex_missing.stdout, vertex_missing.stderr)
        self.assertIn(
            "kubeagents-litellm-gsa@sample-project.part0.iam.gserviceaccount.com",
            vertex_missing.stdout + vertex_missing.stderr,
        )

        # print_instructions_gchat.sh renders the partitioned Workspace Add-ons
        # service-agent domain on partitioned project IDs.
        gchat_env = self._empty_install_env.parent / "gchat.install.env"
        gchat_env.write_text(
            f'PROJECT_ID="{PARTITIONED_PROJECT}"\n'
            'PROJECT_NUMBER="123456789"\n'
            'GOOGLE_CHAT_ENABLED="true"\n'
            'CHAT_TOPIC_NAME="ka-chat"\n'
            'CHAT_SUB_NAME="ka-chat-sub"\n'
            'REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"\n',
            encoding="utf-8",
        )
        gchat_proc = _run_installer_bash(
            f'bash "{_REPO_ROOT / "scripts" / "installer" / "print_instructions_gchat.sh"}" --dry-run -y\n',
            get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(gchat_env)}),
        )
        self.assertEqual(gchat_proc.returncode, 0, gchat_proc.stdout + gchat_proc.stderr)
        self.assertIn(
            "service-123456789@gcp-sa-gsuiteaddons.part0-system.iam.gserviceaccount.com",
            gchat_proc.stdout,
        )

        # Legacy domain-scoped projects (example.com:name) stay on commercial iam.gserviceaccount.com.
        legacy_dom = self._run_common('echo "leg=$(gcp_service_agent_domain example.com:legacy-proj)"\n')
        self.assertEqual(legacy_dom.returncode, 0, legacy_dom.stderr)
        self.assertIn("leg=iam.gserviceaccount.com", legacy_dom.stdout)

        # Long partitioned project IDs keep default state bucket <= 63 characters.
        long_bucket = self._run_common(
            'b="$(tf_state_bucket "part0123456789abcdef:very-long-sovereign-project-99")"\n'
            'echo "LEN=${#b} BUCKET=$b"\n'
        )
        self.assertEqual(long_bucket.returncode, 0, long_bucket.stderr)
        self.assertRegex(long_bucket.stdout, r"LEN=(5[0-9]|6[0-3]) ")

        # configure_installer_universe_env resets CLOUDSDK_CORE_UNIVERSE_DOMAIN on googleapis.com.
        reset_proc = self._run_common(
            f'export CLOUDSDK_CORE_UNIVERSE_DOMAIN="{SOVEREIGN_UNIVERSE}" GOOGLE_CLOUD_UNIVERSE_DOMAIN="{SOVEREIGN_UNIVERSE}"\n'
            'configure_installer_universe_env "googleapis.com"\n'
            'echo "RESET_CLOUDSDK=${CLOUDSDK_CORE_UNIVERSE_DOMAIN:-} RESET_GCUD=${GOOGLE_CLOUD_UNIVERSE_DOMAIN:-<unset>}"\n'
        )
        self.assertEqual(reset_proc.returncode, 0, reset_proc.stderr)
        self.assertIn("RESET_CLOUDSDK=googleapis.com RESET_GCUD=<unset>", reset_proc.stdout)

        # gitea_repo_path rejects host-as-owner, reserved SaaS forge hosts, leading '-',
        # and double .git suffixes; lifecycle.sh normalizes uppercase/padded universe_domain;
        # uninstall.sh sanitize_loaded_uninstall_universe_domain strips malformed values;
        # and --menu refuses --manage-iam / --universe-domain.
        gitea_slug = _run_installer_bash(
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'echo "host_owner=$(gitea_repo_path gitea.example.test/repo gitea.example.test)"\n'
            'echo "saas_owner=$(gitea_repo_path github.com/repo gitea.example.test)"\n'
            'echo "dash_slug=$(gitea_repo_path owner/-repo gitea.example.test)"\n'
            'echo "double_git=$(gitea_repo_path owner/repo.git.git gitea.example.test)"\n'
            'echo "good=$(gitea_repo_path owner/repo.git gitea.example.test)"\n',
            get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}),
        )
        self.assertEqual(gitea_slug.returncode, 0, gitea_slug.stderr)
        self.assertIn(
            "host_owner=\nsaas_owner=\ndash_slug=\ndouble_git=\ngood=owner/repo",
            gitea_slug.stdout,
        )

        menu_refusal = _run_installer_bash(
            f'bash "{_INSTALL_SH}" --menu --no-manage-iam\n',
            get_isolated_test_env(
                overrides={
                    "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
                    "KUBE_AGENTS_LOCK_FILE": str(self._lock_file),
                }
            ),
        )
        self.assertNotEqual(menu_refusal.returncode, 0)
        self.assertIn("--menu", menu_refusal.stdout + menu_refusal.stderr)

        san = _run_installer_bash(
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UNINSTALL_SH}"\n'
            'UNIVERSE_DOMAIN="https://bad/value"\n'
            'sanitize_loaded_uninstall_universe_domain "bad.env"\n'
            'echo "AFTER_BAD=${UNIVERSE_DOMAIN:-<unset>}"\n',
            get_isolated_test_env(),
        )
        self.assertEqual(san.returncode, 0, san.stderr)
        self.assertIn("AFTER_BAD=<unset>", san.stdout)


if __name__ == "__main__":
    unittest.main()

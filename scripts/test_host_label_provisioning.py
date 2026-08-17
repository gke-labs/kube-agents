import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "k8s-operator" / "scripts"


class HostLabelProvisioningTest(unittest.TestCase):
    def test_registration_uses_portal_label_and_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            call_log = Path(temp_dir) / "gcloud.log"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    textwrap.dedent(
                        f"""
                        source {SCRIPT_ROOT / 'common.sh'}
                        PROJECT_ID=test-project
                        CLUSTER_NAME=test-cluster
                        REGION=us-central1
                        DRY_RUN=0
                        gcloud() {{
                          printf '%s\n' "$*" >> "$CALL_LOG"
                          case "$*" in
                            *"clusters describe"*) printf 'false\n' ;;
                            *"clusters update"*) return 42 ;;
                          esac
                        }}
                        retry() {{ shift 2; "$@"; }}
                        register_host_label
                        """
                    ),
                ],
                cwd=REPO_ROOT,
                env={**os.environ, "CALL_LOG": str(call_log), "TERM": "dumb"},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Provisioning will continue", result.stdout)
            calls = call_log.read_text()
            self.assertIn("--update-labels=kube-agents-host=true", calls)
            self.assertNotIn("--update-labels=kubeagents-host=true", calls)

    def test_failed_label_removal_happens_after_local_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scripts = Path(temp_dir)
            shutil.copy2(SCRIPT_ROOT / "common.sh", scripts / "common.sh")
            shutil.copy2(
                SCRIPT_ROOT / "min_versions.sh", scripts / "min_versions.sh"
            )
            shutil.copy2(
                SCRIPT_ROOT / "teardown_08_deploy_platform_agent.sh",
                scripts / "teardown_08_deploy_platform_agent.sh",
            )
            (scripts / "vars.sh").write_text(
                "export PROJECT_ID=test-project\n"
                "export CLUSTER_NAME=test-cluster\n"
                "export REGION=us-central1\n"
            )
            manifest = scripts / "platform-agent.yaml"
            manifest.write_text("apiVersion: v1\n")
            call_log = scripts / "gcloud.log"
            bin_dir = scripts / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    printf '%s\n' "$*" >> "$CALL_LOG"
                    case "$*" in
                      "config set project"*) exit 0 ;;
                      *"container clusters list"*) echo test-cluster; exit 0 ;;
                      *"container clusters get-credentials"*) exit 0 ;;
                      *"container clusters describe"*) echo true; exit 0 ;;
                      *"container clusters update"*) echo PERMISSION_DENIED >&2; exit 1 ;;
                      *) exit 0 ;;
                    esac
                    """
                )
            )
            gcloud.chmod(0o755)
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("#!/usr/bin/env bash\nexit 0\n")
            kubectl.chmod(0o755)

            result = subprocess.run(
                [
                    "bash",
                    str(scripts / "teardown_08_deploy_platform_agent.sh"),
                    "--no-confirm",
                ],
                env={
                    **os.environ,
                    "CALL_LOG": str(call_log),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TERM": "dumb",
                },
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertFalse(manifest.exists())
            self.assertIn(
                "--remove-labels=kube-agents-host", call_log.read_text()
            )
            self.assertIn("teardown completed with warnings", result.stdout)


if __name__ == "__main__":
    unittest.main()

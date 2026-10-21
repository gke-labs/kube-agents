"""With `litellm.redaction.enabled`, the chart hands the gateway's rule file to the agent pod.

The PlatformAgent CR gains one volume projecting `redaction.yaml` out of the
`litellm-config` ConfigMap and one read-only mount of it at the fixed path the agent
image looks for (`DEFAULT_RULE_FILE` in `deploy/docker/patches/credential_redaction.py`).
The file's presence is the switch: the operator admits no path-valued variable through
`spec.deployment.env`, so there is no env entry. Off, the default, the CR carries neither,
so the render is what it was.

Run: python3 -m unittest discover -s tests -p 'test_chart_disk_redaction.py' -v
"""

import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_ENV_NAME = "KUBE_AGENTS_REDACTION_CONFIG"
_PATCH_MODULE = _REPO_ROOT / "deploy" / "docker" / "patches" / "credential_redaction.py"
_RULE_FILE = re.search(r'^DEFAULT_RULE_FILE = "([^"]+)"$', _PATCH_MODULE.read_text(), re.M).group(1)
_VOLUME = "redaction-rules"
#: The operator's read-only ConfigMap mount in the agent container (scopeDir in
#: k8s-operator/internal/controller/platformagent_manifests.go).
_OPERATOR_SCOPE_DIR = "/etc/kube-agents"
_CONFIG_MAP = "litellm-config"
_KEY = "redaction.yaml"


def _render(*overrides: str) -> dict:
    args = [_HELM, "template", "ka", str(_CHART), "--show-only", _CR_TEMPLATE, *_REQUIRED]
    for override in overrides:
        args += ["--set", override]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return yaml.safe_load(out)


@unittest.skipUnless(_HELM, "helm not on PATH")
class DiskRedactionRenderTest(unittest.TestCase):
    def test_off_by_default_the_cr_carries_none_of_it(self):
        deployment = _render()["spec"]["deployment"]
        self.assertNotIn("env", deployment)
        self.assertNotIn("extraVolumes", deployment)
        self.assertNotIn("extraVolumeMounts", deployment)

    def test_on_the_cr_projects_the_file_read_only_at_the_fixed_path(self):
        deployment = _render("litellm.redaction.enabled=true")["spec"]["deployment"]
        self.assertNotIn("env", deployment)
        self.assertEqual(
            deployment["extraVolumes"],
            [{
                "name": _VOLUME,
                "configMap": {"name": _CONFIG_MAP, "items": [{"key": _KEY, "path": _KEY}]},
            }],
        )
        mount = deployment["extraVolumeMounts"]
        self.assertEqual(mount, [{"name": _VOLUME, "mountPath": pathlib.PurePosixPath(_RULE_FILE).parent.as_posix(), "readOnly": True}])
        self.assertEqual(f"{mount[0]['mountPath']}/{_KEY}", _RULE_FILE)
        # Neither the operator's read-only /etc/kube-agents mount nor a path under it:
        # runc cannot create a mountpoint through a read-only parent.
        rule_dir = pathlib.PurePosixPath(mount[0]["mountPath"])
        self.assertNotIn(pathlib.PurePosixPath(_OPERATOR_SCOPE_DIR), [rule_dir, *rule_dir.parents])

    def test_the_rule_file_the_gateway_reads_is_the_same_key(self):
        args = [
            _HELM, "template", "ka", str(_CHART), "--show-only", "templates/litellm.yaml",
            *_REQUIRED, "--set", "litellm.redaction.enabled=true",
        ]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
        docs = [d for d in yaml.safe_load_all(out) if d]
        config_maps = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == _CONFIG_MAP]
        self.assertEqual(len(config_maps), 1)
        self.assertIn(_KEY, config_maps[0]["data"])
        gateway = next(d for d in docs if d["kind"] == "Deployment")
        container = gateway["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        self.assertIn(_ENV_NAME, env)

    def test_the_rules_are_not_wired_without_the_gateway(self):
        deployment = _render("litellm.redaction.enabled=true", "litellm.enabled=false")["spec"]["deployment"]
        self.assertNotIn("env", deployment)
        self.assertNotIn("extraVolumes", deployment)
        self.assertNotIn("extraVolumeMounts", deployment)


if __name__ == "__main__":
    unittest.main()

"""Tests for LiteLLM/Hindsight PodMonitoring tri-state and KSA annotation merging."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART_DIR = _REPO_ROOT / "charts" / "kube-agents"
_VALUES_YAML = _CHART_DIR / "values.yaml"
_SCHEMA_JSON = _CHART_DIR / "values.schema.json"
_KIND_UP = _REPO_ROOT / "hack" / "kind-up.sh"
_HELM = shutil.which("helm")
_GMP_API = "monitoring.googleapis.com/v1/PodMonitoring"

_REQUIRED_HARNESS = [
    "--set",
    "platformAgent.harness.clusterName=test-cluster",
    "--set",
    "platformAgent.harness.location=us-central1",
    "--set",
    "platformAgent.harness.projectId=sample-project",
    "--set",
    "platformAgent.credentials.create=false",
]


def _run_helm(*extra_args: str, api_versions: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    assert _HELM is not None
    cmd = [_HELM, "template", "kube-agents", str(_CHART_DIR), "-n", "kubeagents-system"]
    for v in api_versions:
        cmd.extend(["--api-versions", v])
    cmd.extend(_REQUIRED_HARNESS)
    cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _render(*extra_args: str, api_versions: tuple[str, ...] = ()) -> list[dict]:
    proc = _run_helm(*extra_args, api_versions=api_versions)
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed ({proc.returncode}):\n{proc.stderr}")
    return [doc for doc in yaml.safe_load_all(proc.stdout) if doc]


class PodMonitoringAndKSAShapeTest(unittest.TestCase):
    """Static shape checks that do not require a local helm binary."""

    def test_defaults_and_schema_are_tri_state(self) -> None:
        values = yaml.safe_load(_VALUES_YAML.read_text())
        self.assertIsNone(values["litellm"]["podMonitoring"])
        self.assertIsNone(values["hindsight"]["podMonitoring"])

        schema = json.loads(_SCHEMA_JSON.read_text())
        self.assertEqual(
            schema["properties"]["litellm"]["properties"]["podMonitoring"],
            {"type": ["boolean", "null"]},
        )
        self.assertEqual(
            schema["properties"]["hindsight"]["properties"]["podMonitoring"],
            {"type": ["boolean", "null"]},
        )
        self.assertEqual(
            schema["properties"]["litellm"]["properties"]["vertex"]["properties"][
                "serviceAccountAnnotations"
            ],
            {"type": ["object", "null"]},
        )
        self.assertEqual(
            schema["properties"]["platformAgent"]["properties"]["security"]["properties"][
                "serviceAccountAnnotations"
            ],
            {"type": ["object", "null"]},
        )
        non_comment_lines = [
            line for line in _KIND_UP.read_text().splitlines() if not line.lstrip().startswith("#")
        ]
        self.assertNotIn("litellm.podMonitoring", "\n".join(non_comment_lines))


@unittest.skipUnless(_HELM, "helm binary required")
class LiteLLMHindsightPodMonitoringTriStateTest(unittest.TestCase):
    """Both litellm.podMonitoring and hindsight.podMonitoring follow the null tri-state."""

    def test_litellm_and_hindsight_podmonitoring_follow_cluster_api_versions(self) -> None:
        off_gke = _render("--set", "hindsight.enabled=true")
        off_names = {d["metadata"]["name"] for d in off_gke if d.get("kind") == "PodMonitoring"}
        self.assertNotIn("litellm-monitoring", off_names)
        self.assertNotIn("hindsight-monitoring", off_names)

        on_gke = _render("--set", "hindsight.enabled=true", api_versions=(_GMP_API,))
        on_names = {d["metadata"]["name"] for d in on_gke if d.get("kind") == "PodMonitoring"}
        self.assertIn("litellm-monitoring", on_names)
        self.assertIn("hindsight-monitoring", on_names)

    def test_explicit_true_and_false_override_capabilities(self) -> None:
        forced_on = _render(
            "--set",
            "hindsight.enabled=true",
            "--set",
            "litellm.podMonitoring=true",
            "--set",
            "hindsight.podMonitoring=true",
        )
        forced_on_names = {
            d["metadata"]["name"] for d in forced_on if d.get("kind") == "PodMonitoring"
        }
        self.assertIn("litellm-monitoring", forced_on_names)
        self.assertIn("hindsight-monitoring", forced_on_names)

        forced_off = _render(
            "--set",
            "hindsight.enabled=true",
            "--set",
            "litellm.podMonitoring=false",
            "--set",
            "hindsight.podMonitoring=false",
            api_versions=(_GMP_API,),
        )
        forced_off_names = {
            d["metadata"]["name"] for d in forced_off if d.get("kind") == "PodMonitoring"
        }
        self.assertNotIn("litellm-monitoring", forced_off_names)
        self.assertNotIn("hindsight-monitoring", forced_off_names)


@unittest.skipUnless(_HELM, "helm binary required")
class ServiceAccountAnnotationsNullMergeTest(unittest.TestCase):
    """Layered values clearing serviceAccountAnnotations keep KSA ownership consistent."""

    def test_clearing_inherited_annotation_subkey_renders_chart_ksa_and_omits_cr_map(
        self,
    ) -> None:
        base = {
            "platformAgent": {
                "security": {
                    "serviceAccountAnnotations": {
                        "iam.gke.io/gcp-service-account": "agent@sample-project.iam.gserviceaccount.com"
                    }
                }
            }
        }
        overrides = [
            (
                "subkey_null",
                {
                    "platformAgent": {
                        "security": {
                            "serviceAccountAnnotations": {"iam.gke.io/gcp-service-account": None}
                        }
                    }
                },
            ),
            (
                "subkey_empty",
                {
                    "platformAgent": {
                        "security": {
                            "serviceAccountAnnotations": {"iam.gke.io/gcp-service-account": ""}
                        }
                    }
                },
            ),
            ("map_null", {"platformAgent": {"security": {"serviceAccountAnnotations": None}}}),
        ]
        for label, ov in overrides:
            with self.subTest(case=label), tempfile.NamedTemporaryFile(
                "w", suffix=".yaml"
            ) as f1, tempfile.NamedTemporaryFile("w", suffix=".yaml") as f2:
                yaml.safe_dump(base, f1)
                f1.flush()
                yaml.safe_dump(ov, f2)
                f2.flush()
                docs = _render("-f", f1.name, "-f", f2.name)
                sas = {
                    d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"
                }
                self.assertIn("kubeagents-platform-agent", sas)
                pa = next(d for d in docs if d.get("kind") == "PlatformAgent")
                self.assertNotIn("serviceAccountAnnotations", pa["spec"]["security"])

    def test_non_empty_annotation_defers_ksa_creation_to_operator(self) -> None:
        docs = _render(
            "--set-string",
            r"platformAgent.security.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account=agent@sample-project.iam.gserviceaccount.com",
        )
        sas = {d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"}
        self.assertNotIn("kubeagents-platform-agent", sas)
        pa = next(d for d in docs if d.get("kind") == "PlatformAgent")
        self.assertEqual(
            pa["spec"]["security"]["serviceAccountAnnotations"],
            {"iam.gke.io/gcp-service-account": "agent@sample-project.iam.gserviceaccount.com"},
        )

    def test_litellm_vertex_service_account_annotations_null_and_empty_filtering(self) -> None:
        vertex_args = (
            "--set-string",
            "litellm.modelProvider=vertex_ai",
            "--set-string",
            "litellm.vertex.projectId=sample-project",
        )
        base = {
            "litellm": {
                "vertex": {
                    "serviceAccountAnnotations": {
                        "iam.gke.io/gcp-service-account": "litellm@sample-project.iam.gserviceaccount.com"
                    }
                }
            }
        }
        overrides = [
            (
                "subkey_null",
                {
                    "litellm": {
                        "vertex": {
                            "serviceAccountAnnotations": {"iam.gke.io/gcp-service-account": None}
                        }
                    }
                },
            ),
            (
                "subkey_empty",
                {
                    "litellm": {
                        "vertex": {
                            "serviceAccountAnnotations": {"iam.gke.io/gcp-service-account": ""}
                        }
                    }
                },
            ),
            ("map_null", {"litellm": {"vertex": {"serviceAccountAnnotations": None}}}),
        ]
        for label, ov in overrides:
            with self.subTest(case=label), tempfile.NamedTemporaryFile(
                "w", suffix=".yaml"
            ) as f1, tempfile.NamedTemporaryFile("w", suffix=".yaml") as f2:
                yaml.safe_dump(base, f1)
                f1.flush()
                yaml.safe_dump(ov, f2)
                f2.flush()
                docs = _render(*vertex_args, "-f", f1.name, "-f", f2.name)
                sa = next(
                    d
                    for d in docs
                    if d.get("kind") == "ServiceAccount"
                    and d["metadata"]["name"] == "kubeagents-litellm"
                )
                self.assertNotIn("annotations", sa["metadata"])

        populated = _render(
            *vertex_args,
            "--set-string",
            r"litellm.vertex.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account=litellm@sample-project.iam.gserviceaccount.com",
        )
        sa = next(
            d
            for d in populated
            if d.get("kind") == "ServiceAccount"
            and d["metadata"]["name"] == "kubeagents-litellm"
        )
        self.assertEqual(
            sa["metadata"]["annotations"],
            {"iam.gke.io/gcp-service-account": "litellm@sample-project.iam.gserviceaccount.com"},
        )

    def test_unescaped_dot_in_set_fails_instead_of_coercing_nested_map_to_string(self) -> None:
        proc_pa = _run_helm(
            "--set-string",
            "platformAgent.security.serviceAccountAnnotations.iam.gke.io/gcp-service-account=agent@sample-project.iam.gserviceaccount.com",
        )
        self.assertNotEqual(proc_pa.returncode, 0)
        self.assertIn("must be a string or null", proc_pa.stderr)

        proc_litellm = _run_helm(
            "--set-string",
            "litellm.modelProvider=vertex_ai",
            "--set-string",
            "litellm.vertex.projectId=sample-project",
            "--set-string",
            "litellm.vertex.serviceAccountAnnotations.iam.gke.io/gcp-service-account=litellm@sample-project.iam.gserviceaccount.com",
        )
        self.assertNotEqual(proc_litellm.returncode, 0)
        self.assertIn("must be a string or null", proc_litellm.stderr)


if __name__ == "__main__":
    unittest.main()

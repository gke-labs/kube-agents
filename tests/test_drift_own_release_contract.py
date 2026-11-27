"""The values the own-release drift rule keeps in more than one place agree.

The drift detector (Go) marks a write to kube-agents' own objects and the
Session KV server (Python) decides what to do with it. They share payload keys,
a feature name and Helm's naming, each written once on each side because
neither can import the other. The detector also copies the operator's profiles
namespace suffix, the daemon's longest upgrade window has to outlast the time
the installer gives Helm, and the eval fixture looks on the board for the line
the daemon writes. Nothing else fails when one side moves.

Run:
  python3 -m unittest discover -s tests -p 'test_drift_own_release_contract.py' -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _go_constants import go_string  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
_DETECTOR = REPO_ROOT / "k8s-operator" / "cmd" / "drift-detector"
_OWN_RELEASE_GO = _DETECTOR / "ownrelease.go"
_INJECT_GO = _DETECTOR / "inject.go"
_OPERATOR_NAMESPACE_GO = REPO_ROOT / "k8s-operator" / "internal" / "controller" / "agentprofile_namespace.go"
_DAEMON = REPO_ROOT / "agents" / "platform" / "scripts" / "session_kv_server.py"
_FULL_INSTALL = REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
_FIXTURE = REPO_ROOT / "bench" / "tf" / "prebuilt" / "gitops-drift-own-release" / "main.tf"
# The fixture's copy of the start of the daemon's upgrade line, and how its
# OpenTofu variables map onto the line's fields.
_FIXTURE_WORDING = r'^\s*upgrade_wording\s*=\s*"(.*)"\s*$'
_FIXTURE_FIELDS = (("${var.installer_principal}", "{principal}"), ("${var.release_revision}", "{revision}"))
_CHART_RELEASE_RESOURCE = 'resource "helm_release" "kube_agents" {'
_RESOURCE_END = "\n}\n"

# (the Go constant, the file that declares it, the daemon's constant)
_SHARED_STRINGS = (
    ("featureDriftOwnRelease", _INJECT_GO, "DRIFT_OWN_RELEASE_FEATURE"),
    ("ownReleaseCRD", _OWN_RELEASE_GO, "DRIFT_OWN_RELEASE_CRD"),
    ("ownReleaseObject", _OWN_RELEASE_GO, "DRIFT_OWN_RELEASE_OBJECT"),
    ("helmReleaseRecordPrefix", _OWN_RELEASE_GO, "DRIFT_HELM_RECORD_PREFIX"),
    ("helmReleaseRecordRevisionInfix", _OWN_RELEASE_GO, "DRIFT_HELM_RECORD_REVISION_INFIX"),
)
# (the payload field in the detector, the daemon's constant for its JSON key)
_PAYLOAD_KEYS = (
    ("OwnRelease", "DRIFT_OWN_RELEASE_KEY"),
    ("OwnReleaseRevision", "DRIFT_OWN_RELEASE_REVISION_KEY"),
    ("OwnReleaseRevisionEnd", "DRIFT_OWN_RELEASE_REVISION_END_KEY"),
)


def _go_json_key(path: Path, field: str) -> str:
    found = re.search(rf"^\s*{field}\s+string\s+`json:\"([^\",]+)", path.read_text(), re.MULTILINE)
    if not found:
        raise AssertionError(f"{path} no longer declares the payload field {field}")
    return found.group(1)


def _daemon_value(name: str) -> str:
    found = re.search(rf"^{name} = (.+)$", _DAEMON.read_text(), re.MULTILINE)
    if not found:
        raise AssertionError(f"{_DAEMON} no longer declares {name}")
    return found.group(1).strip().strip('"')


def _chart_release_timeout_seconds() -> int:
    text = _FULL_INSTALL.read_text()
    start = text.index(_CHART_RELEASE_RESOURCE)
    found = re.search(r"^\s*timeout\s*=\s*(\d+)\s*$", text[start : text.index(_RESOURCE_END, start)], re.MULTILINE)
    if not found:
        raise AssertionError(f"{_FULL_INSTALL}: the kube-agents helm_release no longer sets timeout")
    return int(found.group(1))


class OwnReleaseContractTest(unittest.TestCase):
    def test_the_detector_and_the_daemon_share_their_strings(self) -> None:
        for go_name, path, daemon_name in _SHARED_STRINGS:
            with self.subTest(constant=go_name):
                self.assertEqual(go_string(path, go_name), _daemon_value(daemon_name))

    def test_the_daemon_reads_the_keys_the_detector_writes(self) -> None:
        for field, daemon_name in _PAYLOAD_KEYS:
            with self.subTest(field=field):
                self.assertEqual(_go_json_key(_INJECT_GO, field), _daemon_value(daemon_name))

    def test_the_detector_names_the_profiles_namespace_as_the_operator_does(self) -> None:
        self.assertEqual(
            go_string(_OWN_RELEASE_GO, "profilesNamespaceSuffix"),
            go_string(_OPERATOR_NAMESPACE_GO, "profileNamespaceSuffix"),
        )

    def test_the_eval_fixture_looks_for_the_line_the_daemon_writes(self) -> None:
        """A reword of the line passes the daemon's own tests and would turn
        the nightly case red as a product failure."""
        found = re.search(_FIXTURE_WORDING, _FIXTURE.read_text(), re.MULTILINE)
        self.assertIsNotNone(found, f"{_FIXTURE} no longer declares upgrade_wording")
        wording = found.group(1)
        for variable, field in _FIXTURE_FIELDS:
            wording = wording.replace(variable, field)
        self.assertNotIn("${", wording)
        self.assertTrue(_daemon_value("DRIFT_UPGRADE_LINE").startswith(wording), wording)

    def test_an_upgrade_window_with_no_end_outlasts_the_installers_helm_timeout(self) -> None:
        """A Helm run that the installer kills at its timeout never updates its
        record, so the window has to stay open for at least that long."""
        self.assertGreater(int(_daemon_value("DRIFT_UPGRADE_WINDOW_SECONDS")), _chart_release_timeout_seconds())


if __name__ == "__main__":
    unittest.main()

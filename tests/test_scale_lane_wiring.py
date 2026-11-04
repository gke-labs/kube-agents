"""The scale lane's baseline reads the proxy and the watcher by names copied from Go.

tests/scale/kwok/baseline.sh scrapes the k8s-api-proxy's metrics and audit log
and the event watcher's metrics and log by string: the health path, the audit
message, four metric families and the sync line. Each is a `readonly` in the
script and a constant or literal in a Go file, and nothing in either binary
knows the script exists. The script fails a run whose proxy `requests_total`
family or watcher `cluster_up` gauge comes back empty, so a rename of those
two turns the nightly red, but only on the nightly, and the other two are
recorded empty; this test turns the pull request that renames any of them
red instead, by reading both sides. It also holds the workflow's dispatch
choice list to the profile files, which the README says must move together.
tests/test_shellcheck_gate_wiring.py pins workflow wiring the same way.
"""

import pathlib
import re
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LANE = _REPO_ROOT / "tests" / "scale" / "kwok"
_BASELINE = _LANE / "baseline.sh"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "scale-lane.yml"
_PROXY_DIR = _REPO_ROOT / "k8s-api-proxy" / "pkg" / "k8sapiproxy"
_WATCHER_DIR = _REPO_ROOT / "k8s-operator" / "cmd" / "k8s-event-watcher"
_OPERATOR_BROKER = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_broker_split.go"

#: `readonly NAME="value"` lines of the script, the only form its constants take.
_READONLY = re.compile(r'^readonly (?P<name>[A-Z_]+)="(?P<value>[^"]*)"', re.MULTILINE)


def _script_constants() -> dict:
    return {m.group("name"): m.group("value") for m in _READONLY.finditer(_BASELINE.read_text())}


def _go_string_constant(path: pathlib.Path, name: str) -> str:
    """The value of `name = "..."` in a Go const block."""
    match = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"', path.read_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{path.relative_to(_REPO_ROOT)} no longer declares {name}")
    return match.group(1)


class ScaleLaneWiringTest(unittest.TestCase):
    def setUp(self):
        self.script = _script_constants()

    def test_proxy_health_path_matches_handler(self):
        self.assertEqual(
            self.script["PROXY_HEALTHZ_PATH"],
            _go_string_constant(_PROXY_DIR / "handler.go", "HealthzPath"),
        )

    def test_proxy_audit_message_matches_audit_sink(self):
        self.assertEqual(
            self.script["PROXY_AUDIT_MESSAGE"],
            _go_string_constant(_PROXY_DIR / "audit.go", "auditMessage"),
        )

    def test_proxy_metric_families_match_metrics_go(self):
        metrics = _PROXY_DIR / "metrics.go"
        prefix = f'{_go_string_constant(metrics, "metricsNamespace")}_{_go_string_constant(metrics, "metricsSubsystem")}_'
        self.assertEqual(
            self.script["PROXY_REQUESTS_METRIC"],
            prefix + _go_string_constant(metrics, "requestsTotalName"),
        )
        self.assertEqual(
            self.script["PROXY_DURATION_METRIC"],
            prefix + _go_string_constant(metrics, "requestDurationSecondsName"),
        )

    def test_proxy_audience_matches_the_install(self):
        """The caller token's audience is the one the operator hands the proxy."""
        self.assertIn(f'"{self.script["PROXY_AUDIENCE"]}"', _OPERATOR_BROKER.read_text())

    def test_watcher_metric_families_are_registered(self):
        metrics = (_WATCHER_DIR / "metrics.go").read_text()
        for key in ("WATCHER_EVENTS_SEEN_METRIC", "WATCHER_CLUSTER_UP_METRIC"):
            with self.subTest(key=key):
                # gofmt aligns the field's value with its neighbours', so the whitespace is free.
                self.assertRegex(metrics, rf'Name:\s*"{re.escape(self.script[key])}"')

    def test_watcher_sync_line_is_still_logged(self):
        self.assertIn(self.script["WATCHER_SYNC_LINE"], (_WATCHER_DIR / "reload.go").read_text())

    def test_dispatch_choices_are_the_profile_files(self):
        """A profile file without a dispatch option is silently undispatchable, and the reverse fails late."""
        workflow = yaml.safe_load(_WORKFLOW.read_text())
        # PyYAML reads the bare `on:` key as the boolean True.
        triggers = workflow.get("on", workflow.get(True))
        options = triggers["workflow_dispatch"]["inputs"]["profile"]["options"]
        stems = sorted(path.stem for path in (_LANE / "profiles").glob("*.json"))
        self.assertEqual(sorted(options), stems)


if __name__ == "__main__":
    unittest.main()

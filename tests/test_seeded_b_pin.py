"""seeded-b's pin is chosen from the REGULAR channel's own version list, and
never asks GKE for an upgrade it refuses.

`bench/tf/fleet/seeded_b_pin.py` is the program behind the stack's `external`
data source. These tests pin what it decides: the lagging version is the
newest previous-minor patch REGULAR itself offers, not the newest across every
channel (the all-channel list put seeded-b on a RAPID patch, and GKE refuses a
minor bump from a patch the enrolled channel does not carry, #3025); a cluster
running such a patch is held at it, or stepped to a newer REGULAR patch of the
same minor when one exists, which is the one upgrade GKE allows it; a read that
fails is the data source failing, never a create.
"""

import importlib.util
import io
import json
import pathlib
import re
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FLEET_DIR = _REPO_ROOT / "bench" / "tf" / "fleet"
_MODULE_PATH = _FLEET_DIR / "seeded_b_pin.py"

_spec = importlib.util.spec_from_file_location("seeded_b_pin", _MODULE_PATH)
pin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pin)

PROJECT = "kube-agents-evals-7"
ZONE = "us-central1-a"
CLUSTER = "seeded-b"

# Trimmed from `gcloud container get-server-config --zone us-central1-a` on
# 2026-10-14: REGULAR's default had rolled to 1.36, its newest 1.34 and 1.35
# patches trail RAPID's, and the all-channel list leads with RAPID's.
REGULAR_DEFAULT = "1.36.4-gke.1391002"
REGULAR_VALID = [
    "1.36.4-gke.1495002",
    "1.36.4-gke.1391002",
    "1.35.8-gke.1626001",
    "1.35.8-gke.1439001",
    "1.34.11-gke.1209000",
    "1.34.11-gke.1102000",
]
RAPID_VALID = ["1.37.1-gke.1552000", "1.36.4-gke.2046002", "1.35.8-gke.1919000", "1.35.8-gke.1796000", "1.34.12-gke.1153000", "1.34.12-gke.1011000"]
ALL_CHANNEL_VALID = sorted(set(REGULAR_VALID) | set(RAPID_VALID), reverse=True)
RUNNING_RAPID = "1.34.12-gke.1011000"
RUNNING_REGULAR = "1.34.11-gke.1209000"


def _server_config(regular_valid=REGULAR_VALID, regular_default=REGULAR_DEFAULT):
    return {
        "channels": [
            {"channel": "RAPID", "defaultVersion": "1.36.4-gke.2046002", "validVersions": RAPID_VALID},
            {"channel": "REGULAR", "defaultVersion": regular_default, "validVersions": regular_valid},
            {"channel": "STABLE", "defaultVersion": "1.35.6-gke.1250001", "validVersions": ["1.35.6-gke.1250001"]},
        ],
        "defaultClusterVersion": regular_default,
        "validMasterVersions": ALL_CHANNEL_VALID,
    }


NOT_FOUND = "ERROR: (gcloud.container.clusters.describe) ResponseError: code=404, message=Not found: projects/%s/zones/%s/clusters/%s.\n" % (PROJECT, ZONE, CLUSTER)


class _Gcloud:
    """A stand-in for gcloud: one canned answer per subcommand, calls recorded."""

    def __init__(self, server_config=None, describe=None, describe_error=None, server_config_error=None):
        self.server_config = server_config if server_config is not None else _server_config()
        self.describe = describe
        self.describe_error = describe_error
        self.server_config_error = server_config_error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert argv[0] == "gcloud", argv
        if "get-server-config" in argv:
            if self.server_config_error is not None:
                return subprocess.CompletedProcess(argv, 1, "", self.server_config_error)
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.server_config), "")
        assert "describe" in argv, argv
        if self.describe_error is not None:
            return subprocess.CompletedProcess(argv, 1, "", self.describe_error)
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.describe), "")


def _run(gcloud, query=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    body = json.dumps(query if query is not None else {"project": PROJECT, "zone": ZONE, "cluster": CLUSTER})
    pin.gcloud_runner = gcloud
    try:
        code = pin.main(io.StringIO(body), stdout, stderr)
    finally:
        pin.gcloud_runner = subprocess.run
    return code, stdout.getvalue(), stderr.getvalue()


class ChooseTest(unittest.TestCase):
    def test_the_lagging_version_is_regulars_newest_previous_minor_patch(self):
        chosen = pin.choose(REGULAR_DEFAULT, REGULAR_VALID, None)
        self.assertEqual(chosen["lagging_version"], "1.35.8-gke.1626001", "REGULAR's own 1.35, not RAPID's 1.35.8-gke.1919000 that leads the all-channel list")
        self.assertEqual(chosen["pin"], "1.35.8-gke.1626001")
        self.assertFalse(chosen["held"])

    def test_a_create_takes_the_lagging_version(self):
        chosen = pin.choose(REGULAR_DEFAULT, REGULAR_VALID, None)
        self.assertEqual(chosen["running_master_version"], "")
        self.assertEqual(chosen["pin"], chosen["lagging_version"])

    def test_a_running_patch_regular_does_not_carry_is_held(self):
        # The defect of #3025: seeded-b on RAPID's 1.34.12, REGULAR's 1.34 ends
        # at 1.34.11, so the minor bump to 1.35 would be refused. Hold.
        chosen = pin.choose(REGULAR_DEFAULT, REGULAR_VALID, RUNNING_RAPID)
        self.assertTrue(chosen["held"])
        self.assertEqual(chosen["pin"], RUNNING_RAPID)
        self.assertEqual(chosen["lagging_version"], "1.35.8-gke.1626001", "the lag target is still computed and reported")

    def test_a_held_cluster_steps_to_a_newer_regular_patch_of_its_own_minor(self):
        # The one upgrade GKE allows an off-channel cluster: same minor. The
        # morning REGULAR ships a 1.34 patch above the running one, the pin
        # takes it; the apply after that does the minor bump from on-channel.
        valid = ["1.34.13-gke.1000000"] + REGULAR_VALID
        chosen = pin.choose(REGULAR_DEFAULT, valid, RUNNING_RAPID)
        self.assertTrue(chosen["held"])
        self.assertEqual(chosen["pin"], "1.34.13-gke.1000000")

    def test_a_held_cluster_does_not_step_to_an_older_regular_patch(self):
        chosen = pin.choose(REGULAR_DEFAULT, REGULAR_VALID, RUNNING_RAPID)
        self.assertEqual(chosen["pin"], RUNNING_RAPID, "1.34.11-gke.1209000 is below the running 1.34.12; a control plane cannot be downgraded")

    def test_a_running_patch_regular_carries_takes_the_minor_bump(self):
        chosen = pin.choose(REGULAR_DEFAULT, REGULAR_VALID, RUNNING_REGULAR)
        self.assertFalse(chosen["held"])
        self.assertEqual(chosen["pin"], "1.35.8-gke.1626001")

    def test_newest_is_decided_by_number_not_by_list_order(self):
        chosen = pin.choose(REGULAR_DEFAULT, list(reversed(REGULAR_VALID)), None)
        self.assertEqual(chosen["pin"], "1.35.8-gke.1626001")
        chosen = pin.choose("1.35.8-gke.1626001", ["1.34.11-gke.1102000", "1.34.11-gke.1209000"], None)
        self.assertEqual(chosen["pin"], "1.34.11-gke.1209000")
        self.assertGreater(pin.parse_version("1.34.11-gke.1209000"), pin.parse_version("1.34.9-gke.9999999"))

    def test_no_previous_minor_in_regular_is_an_empty_pin_unless_held(self):
        chosen = pin.choose(REGULAR_DEFAULT, ["1.36.4-gke.1391002"], None)
        self.assertEqual(chosen["lagging_version"], "")
        self.assertEqual(chosen["pin"], "", "the stack's precondition names the gap; the script does not guess")
        chosen = pin.choose(REGULAR_DEFAULT, ["1.36.4-gke.1391002"], RUNNING_RAPID)
        self.assertEqual(chosen["pin"], RUNNING_RAPID, "a running cluster is held whatever REGULAR offers")

    def test_a_version_that_does_not_parse_fails_rather_than_misorders(self):
        with self.assertRaises(pin.PinError):
            pin.choose(REGULAR_DEFAULT, REGULAR_VALID + ["1.34.11"], None)
        with self.assertRaises(pin.PinError):
            pin.choose("latest", REGULAR_VALID, None)


class MainTest(unittest.TestCase):
    def test_the_query_drives_both_gcloud_reads_and_the_result_is_a_flat_string_map(self):
        gcloud = _Gcloud(describe={"currentMasterVersion": RUNNING_RAPID})
        code, out, err = _run(gcloud)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(gcloud.calls), 2)
        for argv in gcloud.calls:
            self.assertIn("--project=%s" % PROJECT, argv)
            self.assertIn("--zone=%s" % ZONE, argv)
            self.assertIn("--format=json", argv)
        self.assertIn(CLUSTER, gcloud.calls[1])
        result = json.loads(out)
        self.assertEqual(result["pin"], RUNNING_RAPID)
        self.assertEqual(result["held"], "true")
        self.assertEqual(result["running_master_version"], RUNNING_RAPID)
        self.assertEqual(result["regular_default"], REGULAR_DEFAULT)
        self.assertEqual(result["lagging_version"], "1.35.8-gke.1626001")
        for key, value in result.items():
            self.assertIsInstance(value, str, "the external data source takes strings only: %s" % key)

    def test_a_cluster_that_does_not_exist_is_a_create(self):
        gcloud = _Gcloud(describe_error=NOT_FOUND)
        code, out, err = _run(gcloud)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["running_master_version"], "")
        self.assertEqual(result["held"], "false")
        self.assertEqual(result["pin"], "1.35.8-gke.1626001")

    def test_a_describe_failure_that_is_not_not_found_fails_the_read(self):
        # A transient error must not read as "no cluster": that would plan the
        # lag target and ask GKE for the refused bump the hold exists to avoid.
        gcloud = _Gcloud(describe_error="ERROR: (gcloud.container.clusters.describe) ResponseError: code=503, message=backend\n")
        code, out, err = _run(gcloud)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("describe", err)
        self.assertIn("code=503", err)

    def test_a_failing_server_config_read_fails_the_data_source(self):
        gcloud = _Gcloud(server_config_error="ERROR: (gcloud.container.get-server-config) PERMISSION_DENIED\n")
        code, out, err = _run(gcloud)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("get-server-config", err)
        self.assertIn("PERMISSION_DENIED", err)

    def test_a_server_config_without_the_channel_fails(self):
        config = _server_config()
        config["channels"] = [entry for entry in config["channels"] if entry["channel"] != "REGULAR"]
        gcloud = _Gcloud(server_config=config, describe_error=NOT_FOUND)
        code, out, err = _run(gcloud)
        self.assertNotEqual(code, 0)
        self.assertIn("REGULAR", err)

    def test_a_query_missing_a_key_fails(self):
        gcloud = _Gcloud(describe_error=NOT_FOUND)
        code, out, err = _run(gcloud, query={"project": PROJECT, "zone": ZONE})
        self.assertNotEqual(code, 0)
        self.assertIn("cluster", err)
        self.assertEqual(gcloud.calls, [], "nothing is read on a malformed query")


class StackWiringTest(unittest.TestCase):
    def test_the_stack_runs_this_script_through_an_external_data_source(self):
        main_tf = (_FLEET_DIR / "main.tf").read_text()
        self.assertRegex(main_tf, r'data\s+"external"\s+"seeded_b_pin"')
        self.assertIn('"${path.module}/%s"' % _MODULE_PATH.name, main_tf)
        self.assertNotIn(".valid_master_versions", main_tf, "the all-channel list is what put seeded-b on a RAPID patch")
        self.assertNotIn('data "google_container_engine_versions"', main_tf, "the provider has no per-channel valid list; the script reads REGULAR's")
        block = re.search(r'data\s+"external"\s+"seeded_b_pin"\s*\{.*?query\s*=\s*\{(.*?)\}', main_tf, re.DOTALL)
        self.assertIsNotNone(block, "the data source carries a query map")
        query_keys = set(re.findall(r"^\s*(\w+)\s*=", block.group(1), re.MULTILINE))
        self.assertEqual(query_keys, set(pin.QUERY_KEYS), "the query carries exactly what the script reads")


if __name__ == "__main__":
    unittest.main()

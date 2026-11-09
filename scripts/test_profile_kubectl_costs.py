"""Tests for scripts/dev/profile_kubectl_costs.py: it keys each command as the admission does and writes one tier."""

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PATH = Path(__file__).resolve().parent / "dev" / "profile_kubectl_costs.py"
_spec = importlib.util.spec_from_file_location("profile_kubectl_costs", _PATH)
profile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profile)


class ProfileTest(unittest.TestCase):
    def test_writes_one_tier_keyed_as_the_admission_keys_and_keeps_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "costs.json"
            table.write_text(json.dumps({"about": "x", "tiers": {"fleet": {"commands": {}, "other": {"peak_mib": 9}}}}))
            fake = Path(tmp) / "kubectl"
            # Fails for `get pv`, as a cluster that serves no such kind would.
            fake.write_text("#!/bin/sh\ncase \"$*\" in *' pv '*) exit 1 ;; esac\nexit 0\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            peaks = iter(range(1, 1000))
            with mock.patch.object(profile, "measure_alone",
                                   side_effect=lambda k, args: (next(peaks) * profile.MIB, 0.1, 1 if "pv" in args else 0)), \
                    mock.patch.object(profile, "kubectl_version", return_value="v1.35.10"):
                profile.main(["--tier", "default", "--reference", "a test cluster", "--kubectl", str(fake),
                              "--table", str(table)])
            written = json.loads(table.read_text())
        self.assertEqual(written["tiers"]["fleet"]["other"], {"peak_mib": 9})  # the other tier is untouched
        column = written["tiers"]["default"]
        self.assertEqual(column["reference"], "a test cluster")
        self.assertEqual(column["other"], {"peak_mib": profile.OTHER_MIB})
        commands = column["commands"]
        self.assertIn("get cronjobs,daemonsets,deployments,pods,statefulsets cluster json", commands)
        self.assertNotIn("get persistentvolumes cluster json", commands)  # its run failed
        # `-o json` and `-o name` share a key; the larger peak is kept with its example.
        pods = commands["get pods cluster json"]
        self.assertEqual(pods["example"], "kubectl get pods -A -o name")
        self.assertFalse([k for k in commands if " namespace " in k or " object " in k])  # only cluster reads

    def test_a_key_example_reaches_the_table_without_its_credential_or_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "costs.json"
            table.write_text(json.dumps({"about": "x", "tiers": {}}))
            example = "--server https://10.0.0.1 --token SECRETVALUE get widgets.example.com -A -ojsonpath={.items}"
            with mock.patch.object(profile, "COMMANDS", []), \
                    mock.patch.object(profile, "measure_alone", return_value=(5 * profile.MIB, 0.1, 0)), \
                    mock.patch.object(profile, "kubectl_version", return_value="v1.35.10"):
                profile.main(["--tier", "t", "--reference", "r", "--table", str(table), "--key-example", example])
            text = table.read_text()
        entry = json.loads(text)["tiers"]["t"]["commands"]["get widgets.example.com cluster json"]
        self.assertEqual(entry["example"], "kubectl get widgets.example.com -A -o jsonpath")
        for leaked in ("SECRETVALUE", "10.0.0.1", "--token", "--server"):
            self.assertNotIn(leaked, text)

    def test_a_command_killed_for_memory_aborts_the_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "costs.json"
            table.write_text(json.dumps({"about": "x", "tiers": {}}))
            outcomes = lambda k, args: (0, 0.1, -9 if args[:2] == ["get", "pods"] else 0)  # noqa: E731
            with mock.patch.object(profile, "measure_alone", side_effect=outcomes), \
                    mock.patch.object(profile, "kubectl_version", return_value="v1.35.10"):
                rc = profile.main(["--tier", "fleet", "--reference", "r", "--table", str(table)])
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads(table.read_text())["tiers"], {})  # nothing written


class KeepHigherTest(unittest.TestCase):
    def test_keeps_the_higher_figure_and_entries_this_run_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "costs.json"
            table.write_text(json.dumps({"tiers": {"fleet": {"commands": {
                "get pods cluster json": {"peak_mib": 14000, "example": "kubectl get pods -A -o json"},
                "get nodes cluster table": {"peak_mib": 10, "example": "kubectl get nodes"},
                "get services cluster json": {"peak_mib": 100, "example": "kubectl get svc -A -o json"}}}}}))
            def outcome(k, args):
                if args[:2] == ["get", "svc"]:
                    return 0, 0.1, 1  # a transient failure this run
                return (12000 if args[:2] == ["get", "pods"] else 50) * profile.MIB, 0.1, 0
            with mock.patch.object(profile, "measure_alone", side_effect=outcome), \
                    mock.patch.object(profile, "kubectl_version", return_value="v1.35.10"):
                profile.main(["--tier", "fleet", "--reference", "r", "--table", str(table), "--keep-higher"])
            commands = json.loads(table.read_text())["tiers"]["fleet"]["commands"]
        self.assertEqual(commands["get pods cluster json"]["peak_mib"], 14000)   # the higher of the two
        self.assertEqual(commands["get nodes cluster table"]["peak_mib"], 50)    # the new run's higher figure
        self.assertEqual(commands["get services cluster json"]["peak_mib"], 100)  # lost this run, kept


if __name__ == "__main__":
    unittest.main()

"""Unit tests for cluster_agent_profile.profile_name (the kanban assignee resolver).

Run: python3 -m unittest agents.platform.scripts.test_cluster_agent_profile

profile_name is a pure, deterministic function; the module imports without pyyaml
(that import is lazy, only on the scaffold path).
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cluster_agent_profile as cap  # noqa: E402

MAX = cap.MAX_NAME_LEN  # 63


class ProfileNameTest(unittest.TestCase):
    def test_basic_shape(self):
        self.assertEqual(
            cap.profile_name("agentic-harness-demo", "kage-management", "us-central1"),
            "cluster-agentic-harness-demo-kage-management-us-central1",
        )

    def test_deterministic(self):
        a = cap.profile_name("p", "c", "us-central1")
        b = cap.profile_name("p", "c", "us-central1")
        self.assertEqual(a, b)

    def test_valid_profile_id_chars(self):
        # Only lowercase alnum + dashes; matches Hermes _PROFILE_ID_RE expectations.
        name = cap.profile_name("Proj_X", "My.Cluster", "US-Central1")
        self.assertRegex(name, r"^[a-z0-9][a-z0-9-]*$")
        self.assertLessEqual(len(name), MAX)

    def test_collapses_and_lowercases(self):
        # Uppercase + non-alnum runs collapse to single dashes.
        name = cap.profile_name("A__B", "c//d", "e")
        self.assertEqual(name, "cluster-a-b-c-d-e")

    def test_long_name_is_hashed_and_bounded(self):
        long_cluster = "x" * 120
        name = cap.profile_name("proj", long_cluster, "us-central1")
        self.assertLessEqual(len(name), MAX)
        # hashed form ends with -<8 hex>
        self.assertRegex(name, r"-[0-9a-f]{8}$")

    def test_long_name_stable_hash(self):
        long_cluster = "y" * 120
        self.assertEqual(
            cap.profile_name("proj", long_cluster, "loc"),
            cap.profile_name("proj", long_cluster, "loc"),
        )


class PinKubeconfigEnvTest(unittest.TestCase):
    def test_writes_kubeconfig_line(self):
        home = Path(tempfile.mkdtemp())
        kubeconfig = home / "kubeconfig.yaml"
        cap._pin_kubeconfig_env(home, kubeconfig)
        self.assertEqual((home / ".env").read_text(), f"KUBECONFIG={kubeconfig}\n")

    def test_idempotent_and_preserves_other_lines(self):
        home = Path(tempfile.mkdtemp())
        kubeconfig = home / "kubeconfig.yaml"
        (home / ".env").write_text("FOO=bar\nKUBECONFIG=/stale/path\n")
        cap._pin_kubeconfig_env(home, kubeconfig)
        cap._pin_kubeconfig_env(home, kubeconfig)  # second run must not duplicate
        text = (home / ".env").read_text()
        self.assertEqual(text.count("KUBECONFIG="), 1)
        self.assertIn("FOO=bar\n", text)
        self.assertIn(f"KUBECONFIG={kubeconfig}\n", text)


if __name__ == "__main__":
    unittest.main()

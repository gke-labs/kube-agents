"""The session worker image installs its harness SDK from a committed lockfile.

a2a/Dockerfile.worker used to run `npm install @anthropic-ai/claude-agent-sdk@<v>`
with no lockfile, so the SDK was pinned but its own dependencies resolved to
whatever the registry served at build time (#6948). A just-published transitive
version that the registry had not finished serving failed an image build that
way. The image now copies a2a/worker-harness/package.json and package-lock.json
and runs `npm ci`. These tests hold the pieces together: the Dockerfile, the
manifest, the lock, and the dependabot entry that proposes bumps.
"""

import json
import pathlib
import re
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO_ROOT / "a2a" / "Dockerfile.worker"
_HARNESS_DIR = _REPO_ROOT / "a2a" / "worker-harness"
_PACKAGE_JSON = _HARNESS_DIR / "package.json"
_PACKAGE_LOCK = _HARNESS_DIR / "package-lock.json"
_DEPENDABOT = _REPO_ROOT / ".github" / "dependabot.yml"
_HARNESS_GO = _REPO_ROOT / "a2a" / "worker-adapter" / "harness.go"

_SDK = "@anthropic-ai/claude-agent-sdk"
# The platform package whose binary DefaultHarnessPath names.
_SDK_LINUX_X64 = "@anthropic-ai/claude-agent-sdk-linux-x64"
_PUBLIC_REGISTRY_HOST = "registry.npmjs.org"
_HARNESS_DEPENDABOT_DIR = "/a2a/worker-harness"
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class WorkerHarnessLockTest(unittest.TestCase):
    def test_the_dockerfile_installs_from_the_lock_with_npm_ci(self):
        text = _DOCKERFILE.read_text()
        self.assertIn("COPY a2a/worker-harness/package.json a2a/worker-harness/package-lock.json ./", text)
        self.assertRegex(text, r"RUN npm ci\b")
        self.assertNotRegex(text, r"\bnpm (install|i)\b", "an unlocked npm install is back in the worker image")

    def test_the_manifest_pins_the_sdk_exactly(self):
        deps = json.loads(_PACKAGE_JSON.read_text())["dependencies"]
        self.assertEqual(list(deps), [_SDK])
        self.assertRegex(deps[_SDK], _EXACT_VERSION, "a range here lets npm ci and the manifest disagree")

    def test_the_lock_matches_the_manifest_and_carries_the_harness_binary_package(self):
        manifest = json.loads(_PACKAGE_JSON.read_text())
        lock = json.loads(_PACKAGE_LOCK.read_text())
        packages = lock["packages"]
        self.assertEqual(packages[""]["dependencies"], manifest["dependencies"])
        version = manifest["dependencies"][_SDK]
        self.assertEqual(packages[f"node_modules/{_SDK}"]["version"], version)
        self.assertEqual(packages[f"node_modules/{_SDK_LINUX_X64}"]["version"], version)
        self.assertIn(f"/app/node_modules/{_SDK_LINUX_X64}/claude", _HARNESS_GO.read_text())

    def test_every_locked_package_resolves_from_the_public_registry(self):
        packages = json.loads(_PACKAGE_LOCK.read_text())["packages"]
        hosts = {
            info["resolved"].split("/")[2]
            for name, info in packages.items()
            if name and "resolved" in info
        }
        self.assertEqual(hosts, {_PUBLIC_REGISTRY_HOST})

    def test_dependabot_proposes_bumps_for_the_harness_directory(self):
        updates = yaml.safe_load(_DEPENDABOT.read_text())["updates"]
        harness = [
            u for u in updates if u["package-ecosystem"] == "npm" and u.get("directory") == _HARNESS_DEPENDABOT_DIR
        ]
        self.assertEqual(len(harness), 1, "the harness directory needs exactly one npm entry")
        # Its own group, so an SDK bump is never batched with another directory's.
        self.assertEqual(list(harness[0]["groups"]), ["worker-harness"])


if __name__ == "__main__":
    unittest.main()

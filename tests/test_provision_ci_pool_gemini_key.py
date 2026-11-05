"""scripts/provision_ci_pool_gemini_key.sh, executed against a fake gcloud.

The script mints the eval nightlies' Gemini Developer API key in a pool project
and keeps it as the current version of a Secret Manager secret. The fake
`gcloud` on PATH logs every call (argv, and stdin for `versions add`) as one
JSON object per line and answers from the state the case sets, so these
assertions are against the script that ships.
"""

import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "provision_ci_pool_gemini_key.sh"
_PROVISION_SCRIPT = _REPO_ROOT / "scripts" / "provision_ci_pool_project.sh"
_KEY_SCRIPT_CALL = '"${SCRIPT_DIR}/provision_ci_pool_gemini_key.sh" "${PROJECT_ID}"'
# A command line that runs Terraform or OpenTofu: either binary, or lifecycle.sh,
# which drives terraform. Comments and the Step 0 toolchain loop do not match.
_IAC_INVOCATION = re.compile(r"^\s*(tofu|terraform|\./lifecycle\.sh)\s")

# Literals on purpose: the script defines the same values, and a test that
# read them back from it would assert nothing.
_PROJECT = "kube-agents-evals-99"
_DISPLAY_NAME = "kube-agents-eval-gemini"
_SECRET = "kube-agents-eval-gemini-api-key"
_API_TARGET = "generativelanguage.googleapis.com"
_APIS = ["apikeys.googleapis.com", "secretmanager.googleapis.com", "generativelanguage.googleapis.com"]
_KEY_NAME = "projects/123/locations/global/keys/abc-123"
_ACCESSORS = [
    "prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com",
    "eval-baseline-recorder@kube-agents-prow.iam.gserviceaccount.com",
]
_VIEWER = "eval-dashboard-publisher@kube-agents-prow.iam.gserviceaccount.com"

# A Python fake: the state machine (a key that appears once created, a secret
# that exists once created) is clearer here than in a case statement.
_FAKE_GCLOUD = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

argv = sys.argv[1:]
entry = {"argv": argv}
state = pathlib.Path(os.environ["FAKE_GCLOUD_STATE"])
key_created = state / "key"
secret_created = state / "secret"
head = argv[:3]

def out(text):
    sys.stdout.write(text)

def fail(text, code=1):
    sys.stderr.write(text + "\n")
    log(); sys.exit(code)

def log():
    with open(os.environ["FAKE_GCLOUD_LOG"], "a") as f:
        f.write(json.dumps(entry) + "\n")

if argv[:2] == ["services", "enable"]:
    log(); sys.exit(0)
if head == ["services", "api-keys", "list"]:
    if os.environ.get("FAKE_KEY_EXISTS") == "1" or key_created.exists():
        out(os.environ["FAKE_KEY_NAME"] + "\n")
    log(); sys.exit(0)
if head == ["services", "api-keys", "create"]:
    # Real gcloud prints the operation result, key string included, on create.
    sys.stderr.write('Operation [operations/akmf.x] complete. Result: {"keyString":"%s"}\n' % os.environ["FAKE_KEY_STRING"])
    if os.environ.get("FAKE_CREATE_FAILS") == "1":
        fail("ERROR: (gcloud.services.api-keys.create) PERMISSION_DENIED: create refused.")
    key_created.touch()
    log(); sys.exit(0)
if head == ["services", "api-keys", "get-key-string"]:
    out(os.environ["FAKE_KEY_STRING"] + "\n")
    log(); sys.exit(0)
if argv[:2] == ["secrets", "describe"]:
    if os.environ.get("FAKE_SECRET_EXISTS") == "1" or secret_created.exists():
        out("projects/123/secrets/%s\n" % argv[2])
        log(); sys.exit(0)
    fail("ERROR: (gcloud.secrets.describe) NOT_FOUND: Secret [projects/123/secrets/%s] not found." % argv[2])
if argv[:2] == ["secrets", "create"]:
    secret_created.touch()
    log(); sys.exit(0)
if head == ["secrets", "versions", "access"]:
    current = os.environ.get("FAKE_CURRENT_VALUE")
    if current is None:
        fail("ERROR: (gcloud.secrets.versions.access) NOT_FOUND: Secret Version [latest] not found or has no versions.")
    out(current)
    log(); sys.exit(0)
if head == ["secrets", "versions", "add"]:
    entry["stdin"] = sys.stdin.read()
    log(); sys.exit(0)
if argv[:2] == ["secrets", "add-iam-policy-binding"]:
    log(); sys.exit(0)
fail("fake gcloud: unexpected call: %s" % " ".join(argv), 97)
"""


class ProvisionGeminiKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.bindir = root / "bin"
        self.bindir.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        fake = self.bindir / "gcloud"
        fake.write_text(_FAKE_GCLOUD)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = root / "gcloud.log"

    def _run(self, *args, key_exists=False, secret_exists=False, key_string="k1", current_value=None,
             create_fails=False, bash_flags=()):
        overrides = {
            "PATH": f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GCLOUD_LOG": str(self.log),
            "FAKE_GCLOUD_STATE": str(self.state),
            "FAKE_KEY_EXISTS": "1" if key_exists else "0",
            "FAKE_SECRET_EXISTS": "1" if secret_exists else "0",
            "FAKE_KEY_NAME": _KEY_NAME,
            "FAKE_KEY_STRING": key_string,
            "FAKE_CREATE_FAILS": "1" if create_fails else "0",
        }
        absent = ()
        if current_value is None:
            absent = ("FAKE_CURRENT_VALUE",)
        else:
            overrides["FAKE_CURRENT_VALUE"] = current_value
        env = get_isolated_test_env(overrides=overrides, absent=absent)
        proc = subprocess.run(["bash", *bash_flags, str(_SCRIPT), *args], capture_output=True, text=True, env=env)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return proc, calls

    @staticmethod
    def _index(calls, prefix):
        for i, call in enumerate(calls):
            if call["argv"][: len(prefix)] == prefix:
                return i
        raise AssertionError(f"no call starting {prefix} in {[c['argv'] for c in calls]}")

    def _bindings(self, calls):
        found = []
        for call in calls:
            argv = call["argv"]
            if argv[:2] != ["secrets", "add-iam-policy-binding"]:
                continue
            self.assertEqual(_SECRET, argv[2])
            self.assertIn(f"--project={_PROJECT}", argv)
            member = next(a.split("=", 1)[1] for a in argv if a.startswith("--member="))
            role = next(a.split("=", 1)[1] for a in argv if a.startswith("--role="))
            found.append((member, role))
        return found

    def _assert_grants(self, calls):
        self.assertEqual(
            [(f"serviceAccount:{sa}", "roles/secretmanager.secretAccessor") for sa in _ACCESSORS]
            + [(f"serviceAccount:{_VIEWER}", "roles/secretmanager.viewer")],
            self._bindings(calls),
        )

    def test_a_fresh_project_gets_apis_key_secret_version_and_grants(self):
        proc, calls = self._run(_PROJECT)
        self.assertEqual(0, proc.returncode, proc.stderr)

        enable = calls[self._index(calls, ["services", "enable"])]["argv"]
        self.assertEqual(["services", "enable", *_APIS, f"--project={_PROJECT}"], enable)

        create = calls[self._index(calls, ["services", "api-keys", "create"])]["argv"]
        self.assertIn(f"--project={_PROJECT}", create)
        self.assertIn(f"--display-name={_DISPLAY_NAME}", create)
        self.assertIn(f"--api-target=service={_API_TARGET}", create)

        get = calls[self._index(calls, ["services", "api-keys", "get-key-string"])]["argv"]
        self.assertEqual(_KEY_NAME, get[3])

        secret = calls[self._index(calls, ["secrets", "create"])]["argv"]
        self.assertEqual(_SECRET, secret[2])
        self.assertIn(f"--project={_PROJECT}", secret)
        self.assertIn("--replication-policy=automatic", secret)

        adds = [c for c in calls if c["argv"][:3] == ["secrets", "versions", "add"]]
        self.assertEqual(1, len(adds), adds)
        self.assertEqual(_SECRET, adds[0]["argv"][3])
        self.assertIn("--data-file=-", adds[0]["argv"])
        self.assertEqual("k1", adds[0]["stdin"])  # no trailing newline

        order = [
            self._index(calls, ["services", "enable"]),
            self._index(calls, ["services", "api-keys", "create"]),
            self._index(calls, ["secrets", "create"]),
            self._index(calls, ["secrets", "versions", "add"]),
            self._index(calls, ["secrets", "add-iam-policy-binding"]),
        ]
        self.assertEqual(sorted(order), order)
        self._assert_grants(calls)

    def test_a_rerun_with_the_same_key_adds_no_version(self):
        proc, calls = self._run(_PROJECT, key_exists=True, secret_exists=True, key_string="k1", current_value="k1")
        self.assertEqual(0, proc.returncode, proc.stderr)
        argvs = [c["argv"] for c in calls]
        self.assertFalse([a for a in argvs if a[:3] == ["services", "api-keys", "create"]], argvs)
        self.assertFalse([a for a in argvs if a[:2] == ["secrets", "create"]], argvs)
        self.assertFalse([a for a in argvs if a[:3] == ["secrets", "versions", "add"]], argvs)
        self._assert_grants(calls)

    def test_a_rotated_key_adds_a_version(self):
        proc, calls = self._run(_PROJECT, key_exists=True, secret_exists=True, key_string="k2", current_value="k1")
        self.assertEqual(0, proc.returncode, proc.stderr)
        adds = [c for c in calls if c["argv"][:3] == ["secrets", "versions", "add"]]
        self.assertEqual(1, len(adds), adds)
        self.assertEqual("k2", adds[0]["stdin"])

    def test_the_key_string_never_reaches_stdout_or_stderr(self):
        key = "AIzaFAKE-k1-never-printed"
        proc, calls = self._run(_PROJECT, key_string=key)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn(key, proc.stdout)
        self.assertNotIn(key, proc.stderr)
        self.assertNotIn("k1", proc.stdout)
        self.assertNotIn("k1", proc.stderr)
        # The value goes to Secret Manager on stdin, never on a command line.
        for call in calls:
            self.assertNotIn(key, " ".join(call["argv"]))
        adds = [c for c in calls if c["argv"][:3] == ["secrets", "versions", "add"]]
        self.assertEqual(key, adds[0]["stdin"])

    def test_the_key_string_never_reaches_a_bash_x_trace(self):
        key = "AIzaFAKE-traced-never-printed"
        proc, calls = self._run(_PROJECT, key_string=key, current_value="k1", bash_flags=("-x",))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("+ set -euo pipefail", proc.stderr)  # the trace was on
        self.assertNotIn(key, proc.stdout)
        self.assertNotIn(key, proc.stderr)
        adds = [c for c in calls if c["argv"][:3] == ["secrets", "versions", "add"]]
        self.assertEqual(key, adds[0]["stdin"])

    def test_a_failed_create_shows_the_error_without_the_key_string(self):
        key = "AIzaFAKE-create-failed-never-printed"
        proc, calls = self._run(_PROJECT, key_string=key, create_fails=True)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("PERMISSION_DENIED: create refused.", proc.stderr)
        self.assertIn("could not create API key kube-agents-eval-gemini", proc.stderr)
        self.assertNotIn(key, proc.stderr)
        self.assertNotIn(key, proc.stdout)
        argvs = [c["argv"] for c in calls]
        self.assertFalse([a for a in argvs if a[:2] == ["secrets", "create"]], argvs)

    def test_a_project_argument_is_required(self):
        proc, calls = self._run()
        self.assertEqual(64, proc.returncode)
        self.assertIn("usage:", proc.stderr.lower())
        self.assertEqual([], calls)

    def test_the_provisioning_script_calls_the_key_script_once_with_the_project(self):
        lines = _PROVISION_SCRIPT.read_text().splitlines()
        calls = [i for i, line in enumerate(lines) if _KEY_SCRIPT_CALL in line]
        self.assertEqual(1, len(calls), calls)

        enable = next(i for i, line in enumerate(lines) if line.strip().startswith("gcloud services enable"))
        enable_end = next(i for i in range(enable, len(lines)) if not lines[i].rstrip().endswith("\\"))
        first_iac = next(i for i, line in enumerate(lines) if _IAC_INVOCATION.match(line))
        self.assertLess(enable_end, calls[0])
        self.assertLess(calls[0], first_iac, lines[first_iac])


if __name__ == "__main__":
    unittest.main()

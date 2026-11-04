"""Which processes deploy/shared/start-services.sh starts for each CREDENTIAL_PROXY_ROLE.

Run against the script as it ships: the role check and the start block are cut
out of the script by their first and last lines, and the start functions are
stubs that print their names. The exec tier's role (`exec`, from
docs/designs/credential-broker-exec-tier.md §12) starts the credential runtime
and Envoy and no watcher or drift detector.
"""

from __future__ import annotations

import pathlib
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "deploy" / "shared" / "start-services.sh"
_ROLE_CHECK_START = 'export CREDENTIAL_PROXY_ROLE="${CREDENTIAL_PROXY_ROLE:-combined}"\n'
_ROLE_CHECK_END = "\nesac\n"
_START_BLOCK_START = "\nwrite_wif_credentials\nstart_credential_runtime\n"
_START_BLOCK_END = "\n# Only the credential-path services are waited on."
_STUBS = "\n".join(
    f"{name}() {{ echo {name}; }}"
    for name in (
        "write_wif_credentials",
        "start_credential_runtime",
        "start_envoy",
        "start_event_watcher",
        "start_drift_detector",
    )
)


def _cut(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin) + len(end)]


def _run(role: str) -> subprocess.CompletedProcess[str]:
    text = _SCRIPT.read_text()
    program = "\n".join(
        [
            "set -euo pipefail",
            _STUBS,
            _cut(text, _ROLE_CHECK_START, _ROLE_CHECK_END),
            _cut(text, _START_BLOCK_START, _START_BLOCK_END),
        ]
    )
    return subprocess.run(
        ["bash", "-c", program],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "CREDENTIAL_PROXY_ROLE": role},
    )


class StartServicesRoleTest(unittest.TestCase):
    def test_each_role_starts_its_processes(self) -> None:
        runtime = ["write_wif_credentials", "start_credential_runtime"]
        watchers = ["start_event_watcher", "start_drift_detector"]
        for role, started in (
            ("combined", [*runtime, "start_envoy", *watchers]),
            ("", [*runtime, "start_envoy", *watchers]),
            ("broker", [*runtime, "start_envoy"]),
            ("api-proxy", [*runtime, *watchers]),
            ("exec", [*runtime, "start_envoy"]),
        ):
            with self.subTest(role=role):
                result = _run(role)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(started, result.stdout.split())

    def test_an_unknown_role_ends_the_script_before_anything_starts(self) -> None:
        for role in ("stateful", "execute", "EXEC"):
            with self.subTest(role=role):
                result = _run(role)
                self.assertEqual(1, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn(f"start-services: unknown CREDENTIAL_PROXY_ROLE={role}", result.stderr)


if __name__ == "__main__":
    unittest.main()

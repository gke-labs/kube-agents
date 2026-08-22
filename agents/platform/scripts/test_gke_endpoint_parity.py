"""The DNS-endpoint predicate is implemented three times. Hold them together.

`gke_endpoint.py` decides for the agent, `k8s-operator/scripts/gke_dns_endpoint.sh`
for every shell script, and an inlined `awk` program in
`platformagent_manifests.go` for the credential proxy's bootstrap — the operator
cannot describe the cluster when it renders that manifest, so the check has to
travel inside the command.

Only the Python one had tests. This runs one matrix of gcloud outputs through all
three and asserts they agree, which is how the shell implementation was found to
fail *open* on a row with no tab in it while the awk one failed closed.

The awk program is read out of the Go source rather than copied here. A copy
would pass forever after someone edited the bootstrap.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import gke_endpoint

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BASH_HELPER = os.path.join(REPO_ROOT, "k8s-operator", "scripts", "gke_dns_endpoint.sh")
MANIFESTS_GO = os.path.join(
    REPO_ROOT, "k8s-operator", "internal", "controller", "platformagent_manifests.go"
)

FLAG = "--dns-endpoint"

# gcloud renders `--format="value(a,b)"` as the two fields separated by a tab,
# booleans as True/False, and a field it did not set as empty. Each case is the
# stdout of that describe, paired with the equivalent
# `--format=json(controlPlaneEndpointsConfig)` document the Python path parses.
CASES = [
    ("dns endpoint, external traffic on", "gke-x.gke.goog\tTrue", "gke-x.gke.goog", True, True),
    ("dns endpoint, external traffic off", "gke-x.gke.goog\tFalse", "gke-x.gke.goog", False, False),
    ("no dns endpoint, external traffic on", "\tTrue", "", True, False),
    ("no dns endpoint, external traffic off", "\tFalse", "", False, False),
    ("no dnsEndpointConfig at all", "", None, None, False),
    # The row that caught the shell implementation: with no tab, its suffix
    # expansion returns the whole line, so a lone "True" read as both a
    # non-empty endpoint and an allowExternalTraffic of True.
    ("malformed row, no separator", "True", None, None, False),
    ("malformed row, endpoint only", "gke-x.gke.goog", None, None, False),
]


def _describe_json(endpoint, allow_external):
    if endpoint is None and allow_external is None:
        return {}
    dns = {}
    if endpoint:
        dns["endpoint"] = endpoint
    if allow_external is not None:
        dns["allowExternalTraffic"] = allow_external
    return {"controlPlaneEndpointsConfig": {"dnsEndpointConfig": dns}}


def _awk_program():
    """Pull the bootstrap's awk program out of the operator source."""
    with open(MANIFESTS_GO, "r", encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(r"awk -F'\\t' '([^']*)'", source)
    if match is None:
        raise AssertionError(
            f"no `awk -F'\\t' '...'` program found in {MANIFESTS_GO}. If the bootstrap's"
            " endpoint detection was restructured, update this test to match — do not"
            " delete the parity check."
        )
    return match.group(1)


class StubGcloud:
    """A `gcloud` on PATH that answers the two commands this predicate runs."""

    def __init__(self, describe_stdout, describe_rc=0, supports_flag=True):
        self.dir = tempfile.mkdtemp(prefix="gke-endpoint-parity-")
        help_text = f"  {FLAG}  Use the DNS endpoint." if supports_flag else "  --quiet"
        self.log = os.path.join(self.dir, "calls.log")
        # Through a file, not interpolated into the script: these rows carry
        # tabs, and a Python repr inside bash single quotes would hand the
        # helper a literal backslash-t and quietly test the wrong input.
        describe_out = os.path.join(self.dir, "describe.out")
        with open(describe_out, "w", encoding="utf-8") as handle:
            handle.write(describe_stdout + "\n")
        script = f"""#!/bin/bash
printf '%s\\n' "$*" >> {self.log!r}
for arg in "$@"; do
  if [ "$arg" = "--help" ]; then
    printf '%s\\n' {help_text!r}
    exit 0
  fi
done
case "$*" in
  *describe*)
    cat {describe_out!r}
    exit {describe_rc}
    ;;
esac
exit 0
"""
        path = os.path.join(self.dir, "gcloud")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(path, 0o755)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        shutil.rmtree(self.dir, ignore_errors=True)

    @property
    def env(self):
        return {**os.environ, "PATH": self.dir + os.pathsep + os.environ["PATH"]}

    def calls(self):
        if not os.path.isfile(self.log):
            return []
        with open(self.log, "r", encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if line]


def run_bash(describe_stdout, describe_rc=0, supports_flag=True):
    """What `gke_dns_endpoint_flag` leaves in GKE_DNS_ENDPOINT_FLAG."""
    with StubGcloud(describe_stdout, describe_rc, supports_flag) as stub:
        # -u included deliberately: the callers run under it, and the function
        # has to leave the variable defined on every path.
        completed = subprocess.run(
            [
                "bash",
                "-c",
                f'set -euo pipefail; source "{BASH_HELPER}"; '
                'gke_dns_endpoint_flag cluster-a us-central1 proj-a; '
                'printf "%s" "$GKE_DNS_ENDPOINT_FLAG"',
            ],
            capture_output=True,
            text=True,
            env=stub.env,
            timeout=60,
        )
    if completed.returncode != 0:
        raise AssertionError(f"helper exited {completed.returncode}: {completed.stderr}")
    return completed.stdout


def run_awk(describe_stdout):
    completed = subprocess.run(
        ["awk", "-F", "\t", _awk_program()],
        input=describe_stdout + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(f"awk exited {completed.returncode}: {completed.stderr}")
    return completed.stdout.strip()


def run_python(endpoint, allow_external, describe_rc=0, supports_flag=True):
    document = _describe_json(endpoint, allow_external)

    def runner(argv):
        if "--help" in argv:
            return (0, FLAG if supports_flag else "--quiet")
        return (describe_rc, json.dumps(document))

    gke_endpoint.reset_cache()
    return gke_endpoint.dns_endpoint_args("proj-a", "cluster-a", "us-central1", run=runner)


class PredicateParity(unittest.TestCase):
    def setUp(self):
        gke_endpoint.reset_cache()
        self.addCleanup(gke_endpoint.reset_cache)

    def test_the_three_implementations_agree(self):
        for name, value_row, endpoint, allow_external, expected in CASES:
            with self.subTest(name):
                want = FLAG if expected else ""
                self.assertEqual(want, run_bash(value_row), "shell helper")
                self.assertEqual(want, run_awk(value_row), "operator bootstrap awk")
                self.assertEqual(
                    [FLAG] if expected else [],
                    run_python(endpoint, allow_external),
                    "gke_endpoint.py",
                )

    def test_a_failed_describe_yields_no_flag_anywhere(self):
        self.assertEqual("", run_bash("gke-x.gke.goog\tTrue", describe_rc=1))
        self.assertEqual([], run_python("gke-x.gke.goog", True, describe_rc=1))

    def test_a_gcloud_without_the_flag_yields_no_flag(self):
        self.assertEqual("", run_bash("gke-x.gke.goog\tTrue", supports_flag=False))
        self.assertEqual([], run_python("gke-x.gke.goog", True, supports_flag=False))

    def test_the_shell_helper_probes_gcloud_once_across_clusters(self):
        """The memo is the reason the helper assigns instead of echoing.

        Run in a `$(...)` subshell it would re-probe every time, and each probe
        is a fresh gcloud start-up — measured at ~1.8s against a real one.
        """
        with StubGcloud("gke-x.gke.goog\tTrue") as stub:
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'set -euo pipefail; source "{BASH_HELPER}"; '
                    'for c in a b c; do gke_dns_endpoint_flag "$c" us-central1 proj-a; '
                    'printf "%s\\n" "$GKE_DNS_ENDPOINT_FLAG"; done',
                ],
                capture_output=True,
                text=True,
                env=stub.env,
                timeout=60,
            )
            calls = stub.calls()
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([FLAG] * 3, completed.stdout.split())
        self.assertEqual(1, len([c for c in calls if "--help" in c]), calls)
        # Each cluster still gets its own describe: that answer is per-cluster.
        self.assertEqual(3, len([c for c in calls if "describe" in c]), calls)

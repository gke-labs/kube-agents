"""`image_refs` in hack/check-image-inventory.sh extracts image references and
nothing else, and check 3 renders the chart with githubMinter on (#1139).

The extraction used to key on the shape of an env var's value -- a quoted
string with a slash and a colon in it -- which the minter's ISSUER_ALLOWLIST
matches, so enabling the minter reported the allowlist as an image outside the
mirror. It now keys on the variable's name. CI only ever runs the script on a
tree where the check passes, so nothing else exercises the discrimination: the
functions are lifted from the script's own text and run under bash against
synthetic rendered YAML, so the assertions are against the code that ships
rather than a copy (the approach of
tests/test_check_image_inventory_go_directive.py).
"""

import pathlib
import re
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "hack" / "check-image-inventory.sh"

# The extraction, lifted by name. A rename fails here loudly instead of
# silently shrinking what is tested.
_LIFTED_FUNCTIONS = ("image_field_refs", "image_env_refs", "image_refs")

# The constants those functions read, lifted the same way.
_LIFTED_CONSTANTS = ("IMAGE_ENV_NAME_RE", "VALUE_FIELD_RE")

# The env vars the chart renders an image into. The pattern has to match every
# one of them: a name it misses is an image the operator stamps onto agent pods
# that no check sees.
_IMAGE_ENV_NAMES = ("PLATFORM_AGENT_IMAGE", "AGENT_SANDBOX_IMAGE", "FLUENT_BIT_IMAGE")

# The call sites, asserted present because a check that is defined and never
# called keeps every gate green -- and because the minter renders are the whole
# point of #1139.
_CALL_SITES = (
    'minter_render="$(render_chart "${MINTER_VALUES[@]}")"',
    'minter_mirrored_render="$(render_chart "${MINTER_VALUES[@]}" '
    '--set "global.imageRegistry=$MIRROR")"',
    'check_inventory_pins "$LABEL_DEFAULT" "$default_images"',
    'check_inventory_pins "$LABEL_DEFAULT_MINTER" "$minter_images"',
    'check_mirror_prefix "$LABEL_MIRRORED" "$mirrored_images"',
    'check_mirror_prefix "$LABEL_MIRRORED_MINTER" "$minter_mirrored_images"',
    'check_mirror_names "$LABEL_MIRRORED" "$mirrored_images"',
    'check_mirror_names "$LABEL_MIRRORED_MINTER" "$minter_mirrored_images"',
    "--set githubMinter.enabled=true",
)

# The three guards that keep a silently-matching-nothing extraction from
# reading as a clean run. Each is identified by the variable the guard tests,
# so rewording the message does not break the test.
_GUARDS = (
    '[ -n "$default_images" ] || {',
    '[ -n "$(image_env_refs <<<"$default_render")" ] || {',
    '[ -n "$minter_only" ] || {',
)

# A rendered manifest carrying every shape the chart emits: a bare and a quoted
# `image:`, the three image env vars, an env var whose value is an image only
# by shape (ISSUER_ALLOWLIST, and KMS_KEY_NAME and SOURCE_SYSTEM_AUTH beside
# it), and a `valueFrom:` where a value would otherwise be.
_RENDERED = """\
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: token-minter
          image: us-docker.pkg.dev/abcxyz/docker-images/github-token-minter-server:v2.7.1-amd64
          env:
            - name: PORT
              value: "8080"
            - name: ISSUER_ALLOWLIST
              value: "https://container.googleapis.com/v1/projects/p/locations/l/clusters/c,https://accounts.google.com"
            - name: KMS_KEY_NAME
              value: "projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"
            - name: SOURCE_SYSTEM_AUTH
              value: "gha://$(GITHUB_APP_ID)?kms_id=$(KMS_KEY_NAME)"
        - name: manager
          image: "ghcr.io/gke-labs/kube-agents/k8s-operator:v0.1.0"
          env:
            - name: PLATFORM_AGENT_IMAGE
              value: "ghcr.io/gke-labs/kube-agents/platform-agent:v0.1.0"
            - name: AGENT_SANDBOX_IMAGE
              value: "ghcr.io/gke-labs/kube-agents/agent-sandbox:v0.1.0"
            - name: FLUENT_BIT_IMAGE
              value: "docker.io/fluent/fluent-bit:5.1.1"
            - name: GITHUB_APP_ID
              valueFrom:
                secretKeyRef:
                  name: github-app-credentials
                  key: app-id
"""

_EXPECTED = [
    "docker.io/fluent/fluent-bit:5.1.1",
    "ghcr.io/gke-labs/kube-agents/agent-sandbox:v0.1.0",
    "ghcr.io/gke-labs/kube-agents/k8s-operator:v0.1.0",
    "ghcr.io/gke-labs/kube-agents/platform-agent:v0.1.0",
    "us-docker.pkg.dev/abcxyz/docker-images/github-token-minter-server:v2.7.1-amd64",
]


def _lift_function(name: str, text: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"{_SCRIPT} no longer defines {name}()")
    return match.group(0)


def _lift_constant(name: str, text: str) -> str:
    match = re.search(rf"^readonly {re.escape(name)}=.*$", text, re.M)
    if match is None:
        raise AssertionError(f"{_SCRIPT} no longer declares {name}")
    return match.group(0) + "\n"


def _extract(function: str, rendered: str) -> list:
    text = _SCRIPT.read_text()
    script = (
        "set -euo pipefail\n"
        + "".join(_lift_constant(name, text) for name in _LIFTED_CONSTANTS)
        + "".join(_lift_function(name, text) for name in _LIFTED_FUNCTIONS)
        + f"{function}\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        input=rendered,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


class ImageRefsTest(unittest.TestCase):
    def test_extracts_image_fields_and_image_env_vars_only(self):
        self.assertEqual(_extract("image_refs", _RENDERED), _EXPECTED)

    def test_issuer_allowlist_is_not_an_image(self):
        allowlist = (
            "            - name: ISSUER_ALLOWLIST\n"
            '              value: "https://container.googleapis.com/v1/projects/p'
            '/locations/l/clusters/c,https://accounts.google.com"\n'
        )
        self.assertEqual(_extract("image_env_refs", allowlist), [])

    def test_every_image_env_var_the_chart_renders_is_matched(self):
        for name in _IMAGE_ENV_NAMES:
            with self.subTest(name=name):
                entry = f"            - name: {name}\n" '              value: "example.invalid/x:v1"\n'
                self.assertEqual(_extract("image_env_refs", entry), ["example.invalid/x:v1"])

    def test_image_env_var_with_no_value_yields_nothing(self):
        entry = (
            "            - name: ORPHAN_IMAGE\n"
            "              valueFrom:\n"
            "                secretKeyRef:\n"
            "                  name: some-secret\n"
            "                  key: some-key\n"
            "            - name: OTHER\n"
            '              value: "example.invalid/y:v1"\n'
        )
        self.assertEqual(_extract("image_env_refs", entry), [])

    def test_image_fields_are_read_quoted_or_bare(self):
        fields = '          image: example.invalid/bare:v1\n          image: "example.invalid/quoted:v1"\n'
        self.assertEqual(
            _extract("image_field_refs", fields),
            ["example.invalid/bare:v1", "example.invalid/quoted:v1"],
        )


class CheckThreeCoverageTest(unittest.TestCase):
    def test_script_renders_and_checks_the_minter_configurations(self):
        text = _SCRIPT.read_text()
        for call in _CALL_SITES:
            self.assertIn(call, text)

    def test_script_guards_against_an_extraction_that_matches_nothing(self):
        text = _SCRIPT.read_text()
        for guard in _GUARDS:
            self.assertIn(guard, text)


if __name__ == "__main__":
    unittest.main()

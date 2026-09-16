"""`check_operator_pin` in hack/check-image-inventory.sh holds an image constant
compiled into the operator to its images.json entry (#1557).

The operator falls back to a compiled constant whenever an image env var is
unset; the ones that are inventory entries are the fluent-bit sidecar, and the
NATS and nats-box images a `spec.mode: next` install renders. The last two
reach no chart render, so this check is the only one that sees them. (The
first-party next defaults are not inventory entries and are not checked.) CI only ever runs the script on a tree
where the check passes, so its fail path -- and the normalisation that lets a
constant keep Docker Hub's short spelling against a fully-qualified inventory
reference -- would otherwise execute nowhere. The function is lifted from the
script's own text and run under bash against a synthetic Go file with
`repo_of` and `pin_of` stubbed, so the assertions are against the code that
ships rather than a copy (the approach of
tests/test_check_image_inventory_go_directive.py).
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_function  # noqa: E402

_REPO_ROOT = _HERE.parent
_SCRIPT = _REPO_ROOT / "hack" / "check-image-inventory.sh"

# The functions the check needs, each lifted by name. A rename fails here
# loudly instead of silently shrinking what is tested. repo_of and pin_of read
# images.json through jq and are stubbed below instead.
_LIFTED_FUNCTIONS = ("fail", "normalise", "check_operator_pin")

# The call sites, asserted present because the lift below supplies its own: a
# function that is defined and never called keeps every gate green. One per
# constant the operator falls back to.
_CALL_SITES = (
    "check_operator_pin fluent-bit k8s-operator/internal/controller/manifest_helpers.go fallbackFluentBitImage",
    "check_operator_pin nats k8s-operator/internal/controller/platformagent_a2a_manifests.go defaultA2ANATSImage",
    "check_operator_pin nats-box k8s-operator/internal/controller/platformagent_a2a_manifests.go defaultA2AProvisionImage",
)

# The inventory the stubs answer from, and the constant every case reads.
_INVENTORY_NAME = "nats"
_INVENTORY_REPO = "docker.io/library/nats"
_INVENTORY_TAG = "2.10-alpine"
_CONSTANT = "defaultA2ANATSImage"
_GO_FILE = "manifests.go"

# (Go const block, expected to pass, fragment the failure names). The pass
# cases are the spellings a const block can put the value in: Docker Hub's
# short form, the fully-qualified form, and the gofmt-aligned block where
# several spaces sit before the `=`, and a trailing comment that itself
# carries a quoted string, which the capture must stop before. The fail cases
# are a drifted tag, a drifted repository, and a constant that is not there
# at all.
_CASES = (
    (f'\t{_CONSTANT} = "nats:2.10-alpine"\n', True, ""),
    (f'\t{_CONSTANT} = "nats:2.10-alpine" // was "nats:2.9-alpine"\n', True, ""),
    (f'\t{_CONSTANT} = "docker.io/library/nats:2.10-alpine"\n', True, ""),
    (f'\ta2aNATSImageEnvVar      = "A2A_NATS_IMAGE"\n\t{_CONSTANT}     = "nats:2.10-alpine"\n', True, ""),
    (f'\t{_CONSTANT} = "nats:2.11-alpine"\n', False, f"{_CONSTANT} is 'nats:2.11-alpine'"),
    (f'\t{_CONSTANT} = "ghcr.io/other/nats:2.10-alpine"\n', False, "is 'ghcr.io/other/nats:2.10-alpine'"),
    (f'\tsomeOtherImage = "nats:2.10-alpine"\n', False, "<unset>"),
    ("", False, "<unset>"),
)

# A line that carries the constant's name without defining it. The pattern
# anchors on a definition, so neither a use site nor a longer name that ends
# in the constant's name may satisfy the check.
_DECOYS = f"\treturn {_CONSTANT}\n\tx{_CONSTANT} = \"nats:2.10-alpine\"\n"


def _run_check(go_source: str) -> subprocess.CompletedProcess:
    text = _SCRIPT.read_text()
    functions = "".join(lift_function(name, text, _SCRIPT) for name in _LIFTED_FUNCTIONS)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / _GO_FILE).write_text(f"package controller\n\nconst (\n{go_source})\n")
        script = (
            "set -u\nstatus=0\nINVENTORY=images.json\n"
            f"repo_of() {{ echo {_INVENTORY_REPO}; }}\n"
            f"pin_of() {{ echo {_INVENTORY_TAG}; }}\n"
            + functions
            + f"check_operator_pin {_INVENTORY_NAME} {_GO_FILE} {_CONSTANT}\nexit $status\n"
        )
        return subprocess.run(
            ["bash", "-c", script], cwd=root, capture_output=True, text=True, check=False
        )


class CheckOperatorPinTest(unittest.TestCase):
    def test_constant_against_inventory(self):
        for go_source, expect_pass, fragment in _CASES:
            with self.subTest(go_source=go_source):
                result = _run_check(go_source)
                if expect_pass:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(fragment, result.stderr)
                    self.assertIn(_GO_FILE, result.stderr)
                    self.assertIn(f"'{_INVENTORY_NAME}'", result.stderr)

    def test_failure_names_the_inventory_pin(self):
        result = _run_check(f'\t{_CONSTANT} = "nats:2.11-alpine"\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"images.json has 'nats:{_INVENTORY_TAG}'", result.stderr)

    def test_use_sites_and_longer_names_are_not_definitions(self):
        result = _run_check(_DECOYS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("<unset>", result.stderr)

    def test_first_definition_wins_over_a_decoy_below_it(self):
        result = _run_check(f'\t{_CONSTANT} = "nats:2.10-alpine"\n{_DECOYS}')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_calls_the_check_for_every_operator_constant(self):
        text = _SCRIPT.read_text()
        for call in _CALL_SITES:
            self.assertIn(call, text)


if __name__ == "__main__":
    unittest.main()

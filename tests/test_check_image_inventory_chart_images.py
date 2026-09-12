"""`image_refs` in hack/check-image-inventory.sh extracts image references and
nothing else, and `check_toggle` checks what an off-by-default chart toggle
adds (#1139).

The extraction used to key on the shape of an env var's value -- a quoted
string with a slash and a colon in it -- which the minter's ISSUER_ALLOWLIST
matches, so enabling the minter reported the allowlist as an image outside the
mirror. It now keys on the variable's name. CI only ever runs the script on a
tree where the check passes, so nothing else exercises the discrimination, and
nothing else exercises check 3's fail paths at all: the functions are lifted
from the script's own text and run under bash against synthetic rendered YAML
and a stubbed `render_chart`, so the assertions are against the code that ships
rather than a copy (the approach of
tests/test_check_image_inventory_go_directive.py).
"""

import os
import pathlib
import subprocess
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_constant, lift_function  # noqa: E402

_REPO_ROOT = _HERE.parent
_SCRIPT = _REPO_ROOT / "hack" / "check-image-inventory.sh"

# The extraction, lifted by name. A rename fails here loudly instead of
# silently shrinking what is tested.
_LIFTED_FUNCTIONS = ("image_field_refs", "image_env_refs", "image_refs")

# The constants those functions read, lifted the same way.
_LIFTED_CONSTANTS = ("IMAGE_FIELD_RE", "IMAGE_ENV_NAME_RE", "VALUE_FIELD_RE")

# Check 3 itself, lifted the same way and driven below against a stubbed
# render.
_CHECK_THREE_FUNCTIONS = (
    "fail",
    "split_ref",
    *_LIFTED_FUNCTIONS,
    "added_images",
    "check_inventory_pins",
    "check_mirror_prefix",
    "check_mirror_names",
    "check_toggle",
)
_CHECK_THREE_CONSTANTS = (*_LIFTED_CONSTANTS, "LABEL_DEFAULT", "LABEL_MIRRORED")

# The env vars the chart renders an image into. The pattern has to match every
# one of them: a name it misses is an image the operator stamps onto agent pods
# that no check sees.
_IMAGE_ENV_NAMES = ("PLATFORM_AGENT_IMAGE", "AGENT_SANDBOX_IMAGE", "FLUENT_BIT_IMAGE")

# What the behavioural tests below cannot reach, because it is top-level script
# rather than a function: the call that puts the minter through check_toggle,
# the values that turn it on, and the guard standing between each extractor
# and a clean run. One guard per extractor, not one over image_refs: either
# half keeps the union non-empty while the other matches nothing, which is how
# macOS ran with every `image:` field unchecked (#1449). Each guard is
# identified by the expression it tests, so rewording a message does not break
# the test.
_CALL_SITES = (
    'check_toggle githubMinter "${MINTER_VALUES[@]}"',
    "--set githubMinter.enabled=true",
)
_GUARDS = (
    '[ -n "$(image_field_refs <<<"$default_render")" ] || {',
    '[ -n "$(image_env_refs <<<"$default_render")" ] || {',
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

# The stand-in mirror and inventory the check-3 harness runs against. helm and
# jq are both absent from the Python test runner, so the render is a fixture
# and pin_of_repo is a lookup table rather than a jq call.
_MIRROR = "registry.example.invalid/mirror"
_INVENTORY = {
    "operator": ("ghcr.io/example/operator", "v1"),
    "platform-agent": ("ghcr.io/example/platform-agent", "v1"),
    "minter": ("public.invalid/minter", "v1"),
}

# The two renders the toggle is measured against: one image field and one
# *_IMAGE env var, so both extraction paths feed the difference.
_BASE = """\
      containers:
        - name: manager
          image: {operator}
          env:
            - name: PLATFORM_AGENT_IMAGE
              value: "{agent}"
"""
_TOGGLE_CONTAINER = """\
        - name: token-minter
          image: {minter}
          env:
            - name: ISSUER_ALLOWLIST
              value: "https://accounts.google.com,https://container.googleapis.com/v1/x"
"""


def _base(operator="ghcr.io/example/operator:v1", agent="ghcr.io/example/platform-agent:v1"):
    return _BASE.format(operator=operator, agent=agent)


def _mirrored_base():
    return _base(f"{_MIRROR}/operator:v1", f"{_MIRROR}/platform-agent:v1")


def _with_minter(base, minter):
    return base + _TOGGLE_CONTAINER.format(minter=minter)


def _extract(function: str, rendered: str) -> list:
    text = _SCRIPT.read_text()
    script = (
        "set -euo pipefail\n"
        + "".join(lift_constant(name, text, _SCRIPT) for name in _LIFTED_CONSTANTS)
        + "".join(lift_function(name, text, _SCRIPT) for name in _LIFTED_FUNCTIONS)
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


def _pin_of_repo_stub() -> str:
    arms = "".join(f'  "{repo}") echo {tag} ;;\n' for repo, tag in _INVENTORY.values())
    return f'pin_of_repo() {{\n  case "$1" in\n{arms}  esac\n}}\n'


def _run_check_three(
    default_render: str,
    mirrored_render: str,
    toggle_render: str,
    toggle_mirrored_render: str,
) -> subprocess.CompletedProcess:
    """Check 3 over four fixture renders, as the script runs it: the default
    pair checked whole, then check_toggle over what the toggle adds."""
    text = _SCRIPT.read_text()
    script = (
        "set -uo pipefail\n"
        f"INVENTORY=images.json\nMIRROR={_MIRROR}\nstatus=0\n"
        + "".join(lift_constant(name, text, _SCRIPT) for name in _CHECK_THREE_CONSTANTS)
        + "inventory_repos=$(printf '%s\\n' "
        + " ".join(sorted(repo for repo, _ in _INVENTORY.values()))
        + ")\ninventory_names=$(printf '%s\\n' "
        + " ".join(sorted(_INVENTORY))
        + ")\n"
        + _pin_of_repo_stub()
        # The toggle's two renders, told apart by the flag the mirrored call adds.
        + 'render_chart() {\n  for arg in "$@"; do\n'
        '    case "$arg" in *global.imageRegistry=*) printf %s "$TOGGLE_MIRRORED_RENDER"; return ;; esac\n'
        '  done\n  printf %s "$TOGGLE_RENDER"\n}\n'
        + "".join(lift_function(name, text, _SCRIPT) for name in _CHECK_THREE_FUNCTIONS)
        + 'default_images="$(image_refs <<<"$DEFAULT_RENDER")"\n'
        'mirrored_images="$(image_refs <<<"$MIRRORED_RENDER")"\n'
        'check_inventory_pins "$LABEL_DEFAULT" "$default_images"\n'
        'check_mirror_prefix "$LABEL_MIRRORED" "$mirrored_images"\n'
        'check_mirror_names "$LABEL_MIRRORED" "$mirrored_images"\n'
        "check_toggle githubMinter --set githubMinter.enabled=true\n"
        "exit $status\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "DEFAULT_RENDER": default_render,
            "MIRRORED_RENDER": mirrored_render,
            "TOGGLE_RENDER": toggle_render,
            "TOGGLE_MIRRORED_RENDER": toggle_mirrored_render,
        },
    )


def _run_clean(**overrides) -> subprocess.CompletedProcess:
    """Check 3 over a tree with nothing wrong with it, unless an override says
    otherwise."""
    renders = {
        "default_render": _base(),
        "mirrored_render": _mirrored_base(),
        "toggle_render": _with_minter(_base(), "public.invalid/minter:v1"),
        "toggle_mirrored_render": _with_minter(_mirrored_base(), f"{_MIRROR}/minter:v1"),
    }
    renders.update(overrides)
    return _run_check_three(**renders)


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

    def test_image_fields_are_read_quoted_bare_or_trailing_space(self):
        """Runs the shipped sed program under whatever sed is on PATH, so on
        a machine with BSD sed this is the test that fails outright when the
        pattern slips back to a GNU-only escape (#1449); the CI runner is GNU
        sed, where tests/test_check_image_inventory_sed_portability.py stands
        in for it."""
        fields = (
            "          image: example.invalid/bare:v1\n"
            '          image: "example.invalid/quoted:v1"\n'
            "          image: example.invalid/bare-trailing:v1   \n"
            '          image: "example.invalid/quoted-trailing:v1" \n'
        )
        self.assertEqual(
            _extract("image_field_refs", fields),
            [
                "example.invalid/bare:v1",
                "example.invalid/quoted:v1",
                "example.invalid/bare-trailing:v1",
                "example.invalid/quoted-trailing:v1",
            ],
        )


class CheckToggleTest(unittest.TestCase):
    """What enabling a toggle puts through checks 3a, 3b and 3c, and under
    which label. The wiring is the point of #1139: a check that is defined but
    fed the wrong render leaves the toggle's images unexamined with everything
    green."""

    def test_a_correct_toggle_render_passes(self):
        result = _run_clean()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_toggle_image_missing_from_the_inventory_is_reported(self):
        result = _run_clean(
            toggle_render=_with_minter(_base(), "public.invalid/rogue:v1"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "a default install with githubMinter enabled: the chart renders "
            "'public.invalid/rogue:v1', which has no entry",
            result.stderr,
        )

    def test_toggle_image_at_the_wrong_pin_is_reported(self):
        result = _run_clean(
            toggle_render=_with_minter(_base(), "public.invalid/minter:v2"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "a default install with githubMinter enabled: the chart renders "
            "'public.invalid/minter:v2', but images.json pins",
            result.stderr,
        )

    def test_toggle_image_outside_the_mirror_is_reported(self):
        result = _run_clean(
            toggle_mirrored_render=_with_minter(_mirrored_base(), "public.invalid/minter:v1"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "a mirrored install with githubMinter enabled: with global.imageRegistry set, "
            "the chart still renders 'public.invalid/minter:v1' outside the mirror",
            result.stderr,
        )

    def test_toggle_image_mirrored_under_an_unpushed_name_is_reported(self):
        result = _run_clean(
            toggle_mirrored_render=_with_minter(_mirrored_base(), f"{_MIRROR}/minter-server:v1"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "a mirrored install with githubMinter enabled: with global.imageRegistry set, "
            f"the chart renders '{_MIRROR}/minter-server:v1', but no images.json entry "
            "is named 'minter-server'",
            result.stderr,
        )

    def test_an_image_both_renders_carry_is_reported_once(self):
        """The toggle's render is a superset of the default one. Checking it
        whole rather than checking what it adds prints every unrelated failure
        twice, under two labels, which reads as two problems."""
        drifted = "ghcr.io/example/operator:v2"
        result = _run_clean(
            default_render=_base(operator=drifted),
            toggle_render=_with_minter(_base(operator=drifted), "public.invalid/minter:v1"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.count(f"the chart renders '{drifted}'"), 1)
        self.assertIn("a default install: the chart renders", result.stderr)

    def test_a_toggle_that_adds_nothing_is_fatal(self):
        """A toggle that has stopped turning on takes its images back out of
        every check with everything still green."""
        result = _run_clean(
            toggle_render=_base(),
            toggle_mirrored_render=_mirrored_base(),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("enabling githubMinter added no image", result.stderr)


class CheckThreeWiringTest(unittest.TestCase):
    def test_script_puts_the_minter_through_check_toggle(self):
        text = _SCRIPT.read_text()
        for call in _CALL_SITES:
            self.assertIn(call, text)

    def test_script_guards_against_an_extraction_that_matches_nothing(self):
        text = _SCRIPT.read_text()
        for guard in _GUARDS:
            self.assertIn(guard, text)


if __name__ == "__main__":
    unittest.main()

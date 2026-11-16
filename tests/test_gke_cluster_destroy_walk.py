"""The gke-cluster outputs the helm provider reads stay known during a destroy.

The composition's helm provider is configured from the module's
cluster_endpoint and cluster_ca_certificate, and `terraform destroy` uses that
provider to uninstall the releases. Since Terraform 1.14
(hashicorp/terraform#37370) a resource with count 0 evaluates as unknown, not
as an empty list, during a destroy. The module always has at least one count-0
cluster source, so a one(concat(...)) fold over the three is unknown there, the
provider gets no host, and hashicorp/helm 3.x's Delete reads "cluster
unreachable" as "already gone": the destroy reports the release removed while it
keeps running on a cluster this state did not create (#2246).

Nothing else catches a regression here. `terraform validate`, plan and apply
all treat a count-0 resource as an empty list and accept the fold, and
`terraform test` has no destroy run. The HCL is pinned here instead.

What is pinned is the shape across the sources: a conditional that selects the
one source whose count is 1, so the count-0 sources are never evaluated. How a
selected source's attribute is then read is the per-source read below, and the
CA certificate's is `one(<source>.master_auth[*].cluster_ca_certificate)`: on a
real cluster master_auth always has one element, so it reads as `[0]` does, and
the splat lets a mocked provider, whose master_auth is empty, plan the module
under `terraform test`. That `one()` is inside a branch and over an attribute of
a resource that exists, not over the sources, so it is known during the destroy.
"""

import pathlib
import re
import unittest

_MODULE = pathlib.Path(__file__).resolve().parents[1] / "terraform" / "modules" / "gke-cluster"
_MAIN_TF = _MODULE / "main.tf"
_OUTPUTS_TF = _MODULE / "outputs.tf"

# Each cluster source and the condition its count expression must match, so the
# conditional below selects exactly the one that exists.
_SOURCES = {
    'resource "google_container_cluster" "autopilot"': 'var.create_cluster && var.cluster_mode == "autopilot"',
    'resource "google_container_cluster" "standard"': 'var.create_cluster && var.cluster_mode == "standard"',
    'data "google_container_cluster" "existing"': "var.create_cluster ? 0 : 1",
}

# The three source references as the conditionals name them, in the order the
# conditionals try them: the adopted cluster, then Autopilot, then Standard.
_SOURCE_REFS = (
    "data.google_container_cluster.existing[0]",
    "google_container_cluster.autopilot[0]",
    "google_container_cluster.standard[0]",
)


def _uncommented(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _parenthesised(text, start, source):
    """The text between the parenthesis opening at `start` and its match."""
    assert text[start] == "(", f"expected '(' in {source}"
    depth = 0
    for i in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        if depth == 0:
            return text[start + 1 : i]
    raise AssertionError(f"unbalanced parentheses in {source}")


def _expression(text, pattern, source):
    """The right-hand side assigned by the first match of `pattern`, through the
    parenthesis closing its first call or group, such as `one(concat(...))` or
    `( ... )`."""
    match = re.search(pattern + r"\s*=\s*", text, re.MULTILINE)
    assert match is not None, f"no {pattern} assignment in {source}"
    start = text.index("(", match.end())
    return text[match.end() : start] + "(" + _parenthesised(text, start, source) + ")"


class GkeClusterDestroyWalkTest(unittest.TestCase):
    def setUp(self):
        self.main_tf = _uncommented(_MAIN_TF.read_text())
        outputs_tf = _uncommented(_OUTPUTS_TF.read_text())
        ca_output = re.search(
            r'^output "cluster_ca_certificate" \{(.*?)^\}', outputs_tf, re.MULTILINE | re.DOTALL
        )
        assert ca_output is not None, "no cluster_ca_certificate output in outputs.tf"
        # Each value with the read it makes of the one selected source.
        self.expressions = {
            "local.cluster_endpoint": (
                _expression(self.main_tf, r"^\s*cluster_endpoint", "main.tf"),
                "{source}.endpoint",
            ),
            "output.cluster_ca_certificate": (
                _expression(ca_output.group(1), r"^\s*value", "outputs.tf"),
                "one({source}.master_auth[*].cluster_ca_certificate)",
            ),
        }

    def test_the_count_expressions_are_the_ones_the_conditionals_mirror(self):
        for header, condition in _SOURCES.items():
            with self.subTest(source=header):
                block = re.search(re.escape(header) + r"\s*\{(.*?)^\}", self.main_tf, re.MULTILINE | re.DOTALL)
                self.assertIsNotNone(block, f"main.tf no longer declares {header}")
                self.assertRegex(
                    block.group(1),
                    r"\bcount\s*=\s*" + re.escape(condition),
                    f"{header}'s count changed; update the conditionals that select the"
                    " cluster endpoint and CA certificate to match, and this test",
                )

    def test_the_provider_inputs_are_not_folded_over_the_count_zero_sources(self):
        for name, (expression, read) in self.expressions.items():
            with self.subTest(value=name):
                # Each per-source read collapsed to its source reference, so any
                # fold still in the text is one that spans the sources.
                across_sources = expression
                for source in _SOURCE_REFS:
                    across_sources = across_sources.replace(read.format(source=source), source)
                for fold in ("one(", "concat(", "try(", "coalesce("):
                    self.assertNotIn(
                        fold,
                        across_sources,
                        f"{name} must not fold over the cluster sources with {fold}...): one"
                        " of them always has count 0, which is unknown during terraform"
                        " destroy, and an unknown host makes the helm provider skip the"
                        " release uninstall while reporting it done (#2246)",
                    )

    def test_the_provider_inputs_select_the_source_that_exists(self):
        for name, (expression, read) in self.expressions.items():
            with self.subTest(value=name):
                flat = " ".join(expression.split()).removeprefix("( ").removesuffix(" )")
                existing, autopilot, standard = (read.format(source=source) for source in _SOURCE_REFS)
                self.assertEqual(
                    flat,
                    f"!var.create_cluster ? {existing} :"
                    f' var.cluster_mode == "autopilot" ? {autopilot} :'
                    f" {standard}",
                    f"{name} must pick the one cluster source that exists by the same"
                    " conditions as the sources' count expressions. Only the selected"
                    " branch is evaluated, so a count-0 source never makes it unknown during"
                    " terraform destroy.",
                )


if __name__ == "__main__":
    unittest.main()

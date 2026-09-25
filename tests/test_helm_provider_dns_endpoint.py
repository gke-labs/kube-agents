"""The Helm provider withholds the cluster CA when it dials the DNS endpoint.

GKE's two control plane endpoints are terminated by different things and so
present different certificates: the IP endpoints get one signed by the cluster
root CA that `cluster_ca_certificate` returns, while the DNS-based endpoint is
terminated at Google Front End with a certificate from a publicly trusted CA.
The catch is that a cluster with IP access disabled reports its DNS hostname in
the same `endpoint` attribute the module folds into `cluster_endpoint`, so the
composition can be handed either one without asking for either. Pairing that
hostname with the cluster CA makes the CA the sole trust anchor and fails every
handshake with "x509: certificate signed by unknown authority" -- on the first
`helm_release`, which is to say after the apply has created every GCP resource.

Nothing else catches a regression here. `terraform validate` accepts both
spellings, no unit test dials a cluster, and the evals and e2e suites run against
clusters that have an IP endpoint, which is the branch that was always correct.
The HCL is pinned here instead.
"""

import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROVIDERS_TF = _REPO_ROOT / "terraform" / "examples" / "full-install" / "providers.tf"
_MODULE_MAIN_TF = _REPO_ROOT / "terraform" / "modules" / "gke-cluster" / "main.tf"
_MODULE_OUTPUTS_TF = _REPO_ROOT / "terraform" / "modules" / "gke-cluster" / "outputs.tf"

# The output the composition reads to decide, and the domain the decision turns
# on. Both are the module's to define; the composition consumes the boolean.
_ENDPOINT_KIND_OUTPUT = "cluster_endpoint_is_dns"
_DNS_ENDPOINT_SUFFIX = ".gke.goog"


def _block(text, header, source):
    """The body of the one top-level block whose header line matches."""
    match = re.search(
        rf"^{header}\s*\{{(.*?)^\}}", text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"no {header} block in {source}"
    return match.group(1)


def _uncommented(text):
    """`text` with its whole-line `#` comments dropped.

    An assertion about what the HCL does should not fire on prose explaining
    it. Trailing comments are left alone: `#` is ordinary inside a string, and
    every comment in the files read here is on a line of its own.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


class HelmProviderDnsEndpointTest(unittest.TestCase):
    def setUp(self):
        self.providers_tf = _PROVIDERS_TF.read_text()
        self.module_main_tf = _MODULE_MAIN_TF.read_text()
        self.module_outputs_tf = _MODULE_OUTPUTS_TF.read_text()
        self.helm_provider = _block(
            self.providers_tf, r'provider\s+"helm"', "providers.tf"
        )

    def test_cluster_ca_is_conditional_on_the_endpoint_kind(self):
        """The CA is withheld when the endpoint dialled is the DNS one."""
        assignment = re.search(
            r"cluster_ca_certificate\s*=\s*(.*?)(?=\n\s*\w+\s*=|\Z)",
            self.helm_provider,
            re.DOTALL,
        )
        self.assertIsNotNone(
            assignment,
            "the helm provider no longer sets cluster_ca_certificate at all; on a"
            " cluster with an IP endpoint that drops the only trust anchor the"
            " cluster root CA provides",
        )
        expression = assignment.group(1)
        # Both branches are pinned by shape, not by presence. An inverted
        # conditional -- CA on the DNS endpoint, nothing on the IP one --
        # mentions the same two tokens as the correct one and breaks both
        # cases at once, so an assertion that only looks for them passes it.
        self.assertRegex(
            expression,
            rf"{_ENDPOINT_KIND_OUTPUT}\s*\?\s*null\s*:",
            "cluster_ca_certificate must be null when"
            f" module.gke_cluster.{_ENDPOINT_KIND_OUTPUT} is true, so that"
            " client-go verifies against the system trust store. Passing the"
            " cluster CA there fails every apply onto a cluster whose control"
            " plane has only the DNS-based endpoint, because that endpoint's"
            " certificate is signed by a public CA and not by the cluster root.",
        )

    def test_cluster_ca_still_reaches_a_cluster_with_an_ip_endpoint(self):
        """The non-DNS branch is unchanged: every install that works today."""
        self.assertRegex(
            self.helm_provider,
            r"\?\s*null\s*:\s*base64decode\(\s*"
            r"module\.gke_cluster\.cluster_ca_certificate\s*\)",
            "a cluster with an IP endpoint must still be given the cluster CA,"
            " which is what signs that endpoint's certificate; it has to be the"
            " false branch of the conditional, not either branch of it",
        )

    def test_module_exposes_the_endpoint_kind(self):
        self.assertIn(
            f'output "{_ENDPOINT_KIND_OUTPUT}"',
            self.module_outputs_tf,
            "the composition reads this output; the module has to publish it",
        )

    def test_endpoint_kind_is_derived_from_the_endpoint_itself(self):
        """Derived, not supplied: the cluster already carries the answer."""
        locals_text = self.module_main_tf
        self.assertRegex(
            locals_text,
            rf"{_ENDPOINT_KIND_OUTPUT}\s*=\s*endswith\(\s*local\.cluster_endpoint\s*,",
            f"{_ENDPOINT_KIND_OUTPUT} must be derived from the endpoint the"
            " provider will dial, so that no operator has to supply a fact the"
            " cluster already exposes",
        )
        self.assertIn(
            f'= "{_DNS_ENDPOINT_SUFFIX}"',
            locals_text,
            f"the module must name {_DNS_ENDPOINT_SUFFIX} as the DNS endpoint"
            " domain it matches on",
        )

    def test_the_composition_does_not_restate_the_suffix(self):
        """One definition of what a DNS endpoint looks like, in the module."""
        self.assertNotIn(
            _DNS_ENDPOINT_SUFFIX,
            _uncommented(self.providers_tf),
            "the composition should consume the module's"
            f" {_ENDPOINT_KIND_OUTPUT} rather than match on"
            f" {_DNS_ENDPOINT_SUFFIX} itself, so the two cannot disagree about"
            " what a DNS endpoint is",
        )


if __name__ == "__main__":
    unittest.main()

"""The smoke pipeline's Helm release gives the eval install a credential-proxy memory limit its fan-out fits.

The broker admits a request only while its child memory reservation fits the budget
it derives from the container's limit (`agents/platform/scripts/credential_proxy.py`,
`docs/designs/credential-proxy-child-memory-budget.md`). At the operator's 1Gi default
that is four requests at once, and the eval runs `EVAL_TASK_PARALLELISM` lanes of Platform
Agents, each fanning Cluster Agents out over the seeded fleet, so the queue behind those four slots
reached p50 waits of 8 to 22 s and a longest wait of 56.7 s against the broker's 60 s
refusal; Cluster Agents' commands timed out and the judge graded the answers as
regressions on pull requests that did not touch the path (#2632). The limit is the
budget's one knob, and `spec.deployment.credentialProxy.resources` is how a CR moves
it, so `hack/ci-deploy.sh` raises it on the eval install and nowhere else.

The value rides two hops this repository can see from Python: the `--set-string` in
`ci-deploy.sh` (a string, because the CRD's quantity is int-or-string and the chart
quotes it) and the chart template that renders `credentialProxy.resources` onto the
CR. The bound is the purpose: at the limit the eval sets, the budget has to admit at
least the slot cap, so that the slot cap, not the budget, is the binding bound again
and the queue is the one the broker had before the budget existed. Below that the
override buys less than it says; a limit under the budget's floor turns it off.
"""

import pathlib
import re
import shutil
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_BROKER = _REPO_ROOT / "agents" / "platform" / "scripts" / "credential_proxy.py"
_OPERATOR_MANIFESTS = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_CHART = _REPO_ROOT / "charts" / "kube-agents"

_LIMIT_FLAG = (
    '--set-string "platformAgent.deployment.credentialProxy.resources.limits.memory='
    '${EVAL_CREDENTIAL_PROXY_MEMORY_LIMIT}"'
)
_LIMIT_CONSTANT_RE = re.compile(r'^readonly EVAL_CREDENTIAL_PROXY_MEMORY_LIMIT="(\d+)(Mi|Gi)"$', re.MULTILINE)
_MEBIBYTE = 1024 * 1024
_QUANTITY_SUFFIX_BYTES = {"Mi": _MEBIBYTE, "Gi": 1024 * _MEBIBYTE}
# The budget's terms, as the broker declares them (held equal to the operator's
# and the chart's by tests/test_credential_proxy_sizing_parity.py).
_BROKER_MEBIBYTE_TERMS = (
    "BROKER_RESIDENT_RESERVE_BYTES",
    "CONTENT_WORKSPACE_RESERVE_BYTES",
    "REQUEST_CHILD_MEMORY_RESERVE_BYTES",
)
_BROKER_COUNT_TERMS = ("OUTPUT_COPIES_PER_COMMAND",)
_BROKER_FLOOR_RE = re.compile(r"^CHILD_MEMORY_BUDGET_FLOOR_BYTES_AT_DEFAULT_CAP = (\d+)$", re.MULTILINE)
# The output cap and the slot cap the operator hands the broker, which the
# broker's own defaults do not match (its flag defaults are for a bare run).
_OPERATOR_OUTPUT_CAP_RE = re.compile(r'^\s*credentialProxyMaxOutputBytes\s*=\s*"(\d+)"\s*$', re.MULTILINE)
_OPERATOR_SLOT_CAP_RE = re.compile(r'^\s*credentialProxyMaxConcurrentCommands\s*=\s*"(\d+)"\s*$', re.MULTILINE)


def _declared(pattern: re.Pattern, where: pathlib.Path) -> re.Match:
    match = pattern.search(where.read_text(encoding="utf-8"))
    assert match, f"{where.relative_to(_REPO_ROOT)} no longer declares {pattern.pattern!r}"
    return match


def eval_limit_bytes() -> int:
    match = _declared(_LIMIT_CONSTANT_RE, _CI_DEPLOY)
    return int(match.group(1)) * _QUANTITY_SUFFIX_BYTES[match.group(2)]


def eval_limit_text() -> str:
    match = _declared(_LIMIT_CONSTANT_RE, _CI_DEPLOY)
    return match.group(1) + match.group(2)


def broker_terms() -> dict[str, int]:
    text = _BROKER.read_text(encoding="utf-8")
    assert re.search(r"^MEBIBYTE = 1024 \* 1024$", text, re.MULTILINE)
    terms = {}
    for name in _BROKER_MEBIBYTE_TERMS:
        terms[name] = int(_declared(re.compile(rf"^{name} = (\d+) \* MEBIBYTE$", re.MULTILINE), _BROKER).group(1)) * _MEBIBYTE
    for name in _BROKER_COUNT_TERMS:
        terms[name] = int(_declared(re.compile(rf"^{name} = (\d+)$", re.MULTILINE), _BROKER).group(1))
    return terms


def requests_the_budget_admits(limit_bytes: int) -> int:
    """`requests_the_budget_admits` as the broker computes it for a slot-taking
    request at the operator's output cap: the limit less the two fixed reserves,
    over one request's child reserve plus its output allowance."""
    terms = broker_terms()
    output_cap = int(_declared(_OPERATOR_OUTPUT_CAP_RE, _OPERATOR_MANIFESTS).group(1))
    budget = limit_bytes - terms["BROKER_RESIDENT_RESERVE_BYTES"] - terms["CONTENT_WORKSPACE_RESERVE_BYTES"]
    per_request = terms["REQUEST_CHILD_MEMORY_RESERVE_BYTES"] + terms["OUTPUT_COPIES_PER_COMMAND"] * output_cap
    return max(0, budget // per_request)


_CRD_APPLY = 'kubectl apply --server-side --force-conflicts -f "${SCRIPT_DIR}/../${CHART_CRD_DIR}"'
# A literal, as hack/ci-teardown.sh declares the same path: the suites that lift
# this file's readonly lines into a harness run them without SCRIPT_DIR.
_CRD_DIR_DECL = 'readonly CHART_CRD_DIR="charts/kube-agents/crds/"'
# The invocation itself, not the comments that mention it above the apply.
_HELM_RELEASE = 'helm upgrade --install "${HELM_RELEASE_NAME}"'


class CiDeployCredentialProxyLimitTest(unittest.TestCase):
    def test_the_charts_crds_are_applied_before_the_helm_release(self) -> None:
        # The override arms the chart's guard against a CRD that predates
        # spec.deployment.credentialProxy, and Helm never upgrades crds/. A
        # project whose release record (and CRDs) survived a failed teardown
        # would fail the render on attempt 1 until a human applied the CRD. The
        # front doors apply crds/ server-side before every re-apply
        # (apply_crd_upgrades, scripts/installer/installer_common.sh); the deploy
        # does the same, and before the release, or the first render still sees
        # the old CRD.
        text = _CI_DEPLOY.read_text(encoding="utf-8")
        self.assertIn(_CRD_DIR_DECL, text)
        self.assertIn(_CRD_APPLY, text)
        self.assertLess(text.index(_CRD_APPLY), text.index(_HELM_RELEASE))
        self.assertTrue((_CHART / "crds").is_dir())

    def test_helm_release_sets_the_proxy_memory_limit_as_a_string(self) -> None:
        text = _CI_DEPLOY.read_text(encoding="utf-8")
        self.assertIn(_LIMIT_FLAG, text)
        self.assertNotIn(
            '--set "platformAgent.deployment.credentialProxy.resources', text,
            "a bare --set would hand the chart a YAML scalar where the CRD wants a quantity string",
        )

    def test_the_limit_makes_the_slot_cap_the_binding_bound_again(self) -> None:
        # At the operator's default the budget admits fewer requests than the
        # slot cap, which is the queue #2632 measured. The eval's limit has to
        # admit at least the slot cap, so the budget stops being the bound.
        limit = eval_limit_bytes()
        floor = int(_declared(_BROKER_FLOOR_RE, _BROKER).group(1))
        slot_cap = int(_declared(_OPERATOR_SLOT_CAP_RE, _OPERATOR_MANIFESTS).group(1))
        self.assertGreaterEqual(limit, floor, "under the floor the broker turns the budget off")
        self.assertGreaterEqual(
            requests_the_budget_admits(limit), slot_cap,
            f"a {eval_limit_text()} limit admits {requests_the_budget_admits(limit)} requests, "
            f"under the slot cap of {slot_cap}: the budget would still be the bound",
        )

    def test_rendered_cr_carries_the_limit(self) -> None:
        # A mistyped values key renders a CR with no credentialProxy block rather
        # than failing, so only a real `helm template` shows the hop works; and the
        # chart's own render-time checks (the floor, request above limit) run on it.
        if shutil.which("helm") is None:
            self.skipTest("helm not installed")
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "t",
                str(_CHART),
                "--set-string",
                "platformAgent.harness.clusterName=c",
                "--set-string",
                "platformAgent.harness.location=us-central1",
                "--set-string",
                "platformAgent.harness.projectId=p",
                "--set-string",
                f"platformAgent.deployment.credentialProxy.resources.limits.memory={eval_limit_text()}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("credentialProxy:", rendered)
        self.assertIn(f'memory: "{eval_limit_text()}"', rendered)


if __name__ == "__main__":
    unittest.main()

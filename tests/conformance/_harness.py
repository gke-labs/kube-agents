"""Shared plumbing for the conformance suite.

Two things live here and nothing else: how to reach a source artifact, and how
to record an invariant the product does not currently satisfy.

## Why there is a source registry rather than open() calls in the tests

A conformance test that reads a file which has moved does not fail -- it
raises, and if it raises inside an expected-failure it is counted as a pass.
That is the failure mode this suite exists to prevent, so every artifact a test
reads is declared in SOURCES with at least one anchor string, and
test_harness_selfcheck.py asserts each path exists and each anchor is present.
Rename a file or a symbol and the self-check goes red before any invariant test
gets a chance to go quietly green.

The question to ask of this file, and the one the slice-2b review says should
be in every reviewer prompt by default, is "could the harness itself be the
thing that passes?"  The self-check module is the answer, and it is only an
answer for as long as every new test registers what it reads.
"""

from __future__ import annotations

import functools
import json
import re
import shlex
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _find_repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "AGENTS.md").is_file() and (candidate / "k8s-operator").is_dir():
            return candidate
    raise RuntimeError(
        "conformance suite cannot locate the repository root; it looks for the "
        "directory holding both AGENTS.md and k8s-operator/"
    )


REPO_ROOT = _find_repo_root()

# The agent-side policy modules are plain scripts rather than a package, so they
# are imported the way the credential proxy imports them at runtime.
_SCRIPTS_DIR = REPO_ROOT / "agents" / "platform" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Re-exported so that test modules import the policy layer *through* the
# harness. Importing `command_policy` directly works only if the sys.path entry
# above has already run, and import order inside a module is exactly the kind of
# invisible precondition that turns into a skipped test later.
import command_policy  # noqa: E402
import credential_proxy  # noqa: E402


@dataclass(frozen=True)
class Source:
    """A repository artifact a conformance test reads, and how to tell it moved.

    `anchors` are substrings whose disappearance means the test that reads this
    file is no longer asserting what it claims to. They are checked once, in
    test_harness_selfcheck.py, rather than in every test that uses the file.
    """

    path: str
    anchors: tuple[str, ...] = field(default_factory=tuple)


SOURCES: dict[str, Source] = {
    # --- the policy layer -------------------------------------------------
    "command_policy": Source(
        "agents/platform/scripts/command_policy.py",
        ("def evaluate(", "KUBECTL_READ_VERBS", "_KUBECTL_IDENTITY_FLAGS", "_IMPERSONATION_FLAGS"),
    ),
    "credential_proxy": Source(
        "agents/platform/scripts/credential_proxy.py",
        (
            "ALLOWED_EXECUTABLES",
            "GIT_MUTATING_SUBCOMMANDS",
            "def read_only_enforced(",
            "def _sanitize_for_logging(",
            "def blocked_by(",
            "os.umask(0o177)",
        ),
    ),
    "session_kv_server": Source(
        "agents/platform/scripts/session_kv_server.py",
        ("/sessions/{session_id}/inject", "trigger_agent_troubleshooter"),
    ),
    "docker_entrypoint": Source(
        "deploy/shared/docker-entrypoint.sh",
        ("session_kv_server",),
    ),
    # --- the image --------------------------------------------------------
    "dockerfile": Source(
        "deploy/docker/Dockerfile",
        ("FROM agent-base", "unexpected credential-aware CLI in sandbox image"),
    ),
    # --- the operator -----------------------------------------------------
    "manifests_go": Source(
        "k8s-operator/internal/controller/platformagent_manifests.go",
        (
            "credentialProxyPolicyJSON",
            "buildMinimalPlatformRole",
            "No ShareProcessNamespace",
            "sandboxUID",
            "credentialProxyUID",
        ),
    ),
    "controller_go": Source(
        "k8s-operator/internal/controller/platformagent_controller.go",
        ("deleteLegacyCredentialIsolationResources", "reconcileAgentEgressPolicy"),
    ),
    "egress_policy_go": Source(
        "k8s-operator/internal/controller/platformagent_egress_policy.go",
        (
            "func ipv4MappedRefusal(",
            "func controlPlaneCIDRRefusal(",
            "func egressRuleReachesMetadata(",
            "metadataServerAddresses",
        ),
    ),
    "broker_split_go": Source(
        "k8s-operator/internal/controller/platformagent_broker_split.go",
        ("buildCredentialBrokerTokenReviewRole", "tokenreviews"),
    ),
    "operator_clusterrole": Source(
        "k8s-operator/config/rbac/role.yaml",
        ("clusterrolebindings", "resourceNames"),
    ),
    "chart_operator_rbac": Source(
        "charts/kube-agents/templates/operator-rbac.yaml",
        ("END GENERATED RULES",),
    ),
    "admission_policy": Source(
        "k8s-operator/config/admission/agent-rbac-policy.yaml",
        ("kube-agents-agent-readonly", "failurePolicy", "policyName"),
    ),
    "chart_admission_policy": Source(
        "charts/kube-agents/templates/agent-rbac-admission-policy.yaml",
        ("policyName",),
    ),
    # --- rendered output the operator is asserted against -----------------
    "golden_default": Source(
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("kind: Deployment", "policy.json"),
    ),
    "golden_split_broker": Source(
        "k8s-operator/internal/testing/testdata/platform/expected/"
        "platformagent-split-broker.yaml",
        ("kind: Deployment",),
    ),
    "golden_egress_allowlist": Source(
        "k8s-operator/internal/testing/testdata/platform/expected/"
        "platformagent-egress-allowlist.yaml",
        ("kind: NetworkPolicy",),
    ),
    "golden_tagged": Source(
        "k8s-operator/internal/testing/testdata/platform/expected/"
        "platformagent-tagged.yaml",
        ("kind: Deployment",),
    ),
    # --- supply chain -----------------------------------------------------
    "skill_sync": Source(
        "scripts/sync-upstream-skills.py",
        ("UPSTREAM_REPO", "--depth"),
    ),
    "tags_env": Source("tags.env", ("HERMES_AGENT_TAG",)),
    "chart_values": Source("charts/kube-agents/values.yaml", ("repository:",)),
    # --- the write plane --------------------------------------------------
    "codeowners_example": Source(
        "examples/gitops-repo/CODEOWNERS.example",
        ("/clusters/", "@your-org/"),
    ),
    "autopush_agent_workflow": Source(
        ".github/workflows/autopush-redeploy-agent.yml",
        ("workflow_run", "head_branch"),
    ),
}

_GOLDEN_KEYS = (
    "golden_default",
    "golden_tagged",
    "golden_split_broker",
    "golden_egress_allowlist",
)


def path_of(name: str) -> Path:
    """Absolute path of a registered source."""
    try:
        source = SOURCES[name]
    except KeyError:  # pragma: no cover - programming error in a test
        raise KeyError(
            f"{name!r} is not a registered conformance source. Add it to "
            f"_harness.SOURCES with an anchor so the self-check can police it."
        ) from None
    return REPO_ROOT / source.path


@functools.lru_cache(maxsize=None)
def text(name: str) -> str:
    """The contents of a registered source.

    Raises rather than returning empty: a conformance test reading nothing is
    a conformance test that cannot fail.
    """
    path = path_of(name)
    if not path.is_file():
        raise FileNotFoundError(f"registered conformance source is missing: {path}")
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError(f"registered conformance source is empty: {path}")
    return content


_HELM_DIRECTIVE = re.compile(r"^\s*\{\{-?.*-?\}\}\s*$")


@functools.lru_cache(maxsize=None)
def yaml_documents(name: str) -> tuple[dict, ...]:
    """Every non-empty YAML document in a registered source.

    Whole-line Helm directives are dropped so a chart template can be read as
    the object set it renders. Only whole-line directives: a template
    *expression* inside a value would change what the object says, and
    silently discarding it would let a chart assert something the cluster never
    sees. Every admission-policy template in this repo is a plain document
    behind one `{{- if }}` guard, and the parse fails loudly if that stops
    being true.
    """
    body = "\n".join(
        line for line in text(name).splitlines() if not _HELM_DIRECTIVE.match(line)
    )
    if "{{" in body:
        raise ValueError(
            f"{SOURCES[name].path} carries an inline Helm expression; the "
            f"conformance suite cannot read it as a rendered object set"
        )
    return tuple(d for d in yaml.safe_load_all(body) if isinstance(d, dict))


def golden_documents() -> dict[str, tuple[dict, ...]]:
    """The rendered PlatformAgent object sets, keyed by fixture name.

    Four fixtures cover the four spec shapes the operator renders: the default
    single-Pod layout, the same with a pinned image tag, the split-broker
    layout, and the split-broker layout with the egress allowlist on. An
    invariant about the rendered output has to hold on all four or it is a
    property of one configuration.
    """
    return {key: yaml_documents(key) for key in _GOLDEN_KEYS}


def objects_of_kind(documents: tuple[dict, ...], kind: str) -> list[dict]:
    return [d for d in documents if d.get("kind") == kind]


def containers_of(document: dict) -> list[dict]:
    """Every container and init container in a Deployment document."""
    spec = document.get("spec", {}).get("template", {}).get("spec", {})
    return list(spec.get("initContainers") or []) + list(spec.get("containers") or [])


def go_function_body(source: str, name: str) -> str:
    """The text of a Go function, from its `func` keyword to the next one.

    Doc comments are excluded on purpose -- several of them mention the very
    identifiers a test is asserting the *absence* of, so a naive search over
    the whole file finds the explanation and calls it the code.

    Handles both a plain function and a method, and handles the last function
    in a file, which has no following `func` to stop at.
    """
    for signature in (f"\nfunc {name}(", f") {name}("):
        start = source.find(signature)
        if start != -1:
            break
    else:
        raise AssertionError(f"no Go function named {name} in this source")
    end = source.find("\nfunc ", start + 1)
    return source[start:] if end == -1 else source[start:end]


def rendered_policy_rules() -> list[dict]:
    """The credential-proxy denylist as it is actually delivered to the Pod.

    Read out of the rendered ConfigMap rather than out of the Go string
    constant: the constant is what someone wrote, the ConfigMap is what the
    sidecar loads, and the two have diverged before.
    """
    for document in yaml_documents("golden_default"):
        data = document.get("data") or {}
        if document.get("kind") == "ConfigMap" and "policy.json" in data:
            return json.loads(data["policy.json"])["rules"]
    raise AssertionError(
        "no rendered credential-proxy policy ConfigMap in the default golden "
        "fixture; the suite is asserting against a denylist that no longer ships"
    )


def policy_blocks(argv: list[str]) -> str | None:
    """The rule id the shipped denylist matches for `argv`, or None.

    Reimplements nothing: it compiles the shipped patterns with the same flags
    `credential_proxy.Policy.load` uses and joins argv the same way
    `Policy.blocked_by` does. Keeping the join here rather than importing
    `blocked_by` is deliberate -- test_D15_parser_differentials.py needs to
    vary the join to expose the checker/executor split, and it cannot do that
    through a method that hardcodes one.
    """
    return _match_rules(shlex.join(argv))


def _match_rules(command: str) -> str | None:
    import re

    for rule in rendered_policy_rules():
        if re.search(rule["pattern"], command, re.IGNORECASE | re.MULTILINE):
            return rule["id"]
    return None


# ---------------------------------------------------------------------------
# Recording invariants the product does not satisfy
# ---------------------------------------------------------------------------

KNOWN_VIOLATIONS: dict[str, tuple[str, str]] = {}


def known_violation(invariant: str, reference: str):
    """Mark a test as asserting an invariant the product currently violates.

    The test is expected to fail. That is not a way of tolerating the gap --
    it is how the gap gets a name, a line number and an owner, and how the
    suite tells us the day it closes: fixing the control turns the expected
    failure into an *unexpected success*, which unittest reports as a failure
    and which is the signal to delete this decorator.

    `reference` cites where the finding is already written down, so the suite
    and the findings documents cannot drift apart silently.

    The corresponding risk -- a test that "fails as expected" because the file
    it reads was renamed -- is handled by the source registry above, not here.
    """

    def decorate(function):
        KNOWN_VIOLATIONS[f"{function.__qualname__}"] = (invariant, reference)
        function.__conformance_known_violation__ = (invariant, reference)
        return unittest.expectedFailure(function)

    return decorate


def requires_cluster(function):
    """Bucket 2: written and wired, runs only against a real cluster.

    Gated on KUBE_AGENTS_CONFORMANCE_CLUSTER rather than on whether a
    kubeconfig happens to be present, so that a developer with cluster
    credentials in their environment does not silently start running mutating
    scenarios against whatever cluster they were last pointed at.
    """
    import os

    return unittest.skipUnless(
        os.environ.get("KUBE_AGENTS_CONFORMANCE_CLUSTER"),
        "bucket 2: set KUBE_AGENTS_CONFORMANCE_CLUSTER to run cluster scenarios",
    )(function)

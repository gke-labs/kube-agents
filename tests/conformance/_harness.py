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

# The name the gateway redactor is registered under in sys.modules when a test
# imports it by path. Distinct from the callback's own registration name so
# the two copies never shadow each other inside one interpreter.
GATEWAY_REDACTOR_MODULE_NAME = "kube_agents_gateway_redactor"

# The published site's content tree. A directory rather than a file, so it is
# not a Source: D2's assertion is about every page, and a registry would need
# editing for each page added. site_pages() below answers for the empty set
# instead, and one page under it is registered so the self-check names the
# tree the day it moves.
SITE_CONTENT_DIR = "docs/site/src/content/docs"
# .mdx too: the site's index, the GKE quickstart and four section indexes are
# MDX, and an escape hatch documented there is documented.
SITE_PAGE_SUFFIXES = (".md", ".mdx")


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
    # B5 imports this module and calls four renderers by attribute. Registered
    # so the self-check polices those names: the B5 assertion is an expected
    # failure, and unittest records ANY exception under one -- so an
    # AttributeError from a renamed renderer counted among the twelve while
    # asserting nothing about bidi or zero-width characters, and the violation
    # could never have closed by its documented route.
    "audit_report": Source(
        "agents/platform/skills/fleet-audit/scripts/audit_report.py",
        ("def _ident(", "def _cell(", "def trim_command(", "def trim_excerpt("),
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
        ("/sessions/{session_id}/inject", "trigger_agent_troubleshooter", "_gateway_api_token"),
    ),
    "docker_entrypoint": Source(
        "deploy/shared/docker-entrypoint.sh",
        ("session_kv_server",),
    ),
    # --- the image --------------------------------------------------------
    "dockerfile": Source(
        "deploy/docker/Dockerfile",
        ("FROM agent-base", "unexpected cluster CLI in the agent image"),
    ),
    # --- the operator -----------------------------------------------------
    "manifests_go": Source(
        "k8s-operator/internal/controller/platformagent_manifests.go",
        (
            "credentialProxyPolicyJSON",
            "buildMinimalPlatformRole",
            "No ShareProcessNamespace",
            "sandboxUID",
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
    "manifest_helpers_go": Source(
        "k8s-operator/internal/controller/manifest_helpers.go",
        ("DefaultPlatformAgentVersion",),
    ),
    "operator_clusterrole": Source(
        # "resourceNames" is back as an anchor, and it is the load-bearing one.
        # #387 removed the operator's only bind rule (bind-to-view), and for a
        # while A4 asserted bind's absence, so there was nothing for it to
        # anchor to. The auth callout reintroduces bind, over exactly one name
        # (system:auth-delegator), and A4 is once again a statement about how
        # that bind is bounded rather than about its absence. Deleting the
        # scoping is the mutation this anchor exists to make loud.
        "k8s-operator/config/rbac/role.yaml",
        ("clusterrolebindings", "resourceNames"),
    ),
    # The other Role a kustomize install ships. role.yaml is the operator's
    # ClusterRole and was for a long time the only RBAC A4 read; this one is
    # listed beside it in config/rbac/kustomization.yaml and grants the
    # leader-election recorder its event verbs. An escalation verb added here
    # installs exactly as readily as one added there.
    "operator_leader_election_role": Source(
        "k8s-operator/config/rbac/leader_election_role.yaml",
        ("kind: Role", "leader-election-role"),
    ),
    "chart_operator_rbac": Source(
        "charts/kube-agents/templates/operator-rbac.yaml",
        # The end marker bounds the generated block. The leader-election Role
        # is anchored too because it is the chart's RBAC object OUTSIDE that
        # block -- the one `make chart-sync` does not manage and a
        # block-scoped parse never reached.
        ("END GENERATED RULES", "operator-leader-election-role"),
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
    "shell_sandbox_manifests_go": Source(
        "k8s-operator/internal/controller/shell_sandbox_manifests.go",
        (
            "func buildShellSandboxServiceAccount",
            "shellSandboxServiceAccountName",
            "iam.gke.io/gcp-service-account",
        ),
    ),
    "golden_default": Source(
        "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml",
        ("kind: Deployment", "policy.json"),
    ),
    "golden_scoped_sa": Source(
        "k8s-operator/internal/testing/testdata/platform/expected/"
        "platformagent-scoped-sa.yaml",
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
    # The above-one-replica shape. Its anchor is the leader Role's pods rule,
    # because that rule is the only reason the fixture is registered here:
    # every other golden is single-replica and renders it away, so without
    # this key group C bounds the leader Role only in the shape where it holds
    # no write verb on pods at all.
    "golden_ha": Source(
        "k8s-operator/internal/testing/testdata/platform/expected/"
        "platformagent-ha.yaml",
        ("kind: Deployment", "kubeagents:leader:", "- pods"),
    ),
    # C1's cross-module pair. The session fence is rendered by the operator
    # (Go module k8s-operator) and its selector has to match the labels the
    # A2A gateway's spawner stamps (Go module a2a). Two modules, so no Go test
    # can compare them, and a NetworkPolicy that selects nothing is
    # indistinguishable from one that is working.
    "a2a_session_fence": Source(
        "k8s-operator/internal/controller/platformagent_a2a_manifests.go",
        ("func buildA2ASessionNetworkPolicy", "a2aSessionComponent", "a2aPartOf ="),
    ),
    # The eval-only inject door. Two files, two modules: the operator decides
    # whether the door is rendered at all (Go module k8s-operator) and the
    # gateway decides what it does once it is (Go module a2a). A3's darkness
    # assertion reads the first and its identity assertion the second, and
    # neither module's own test suite can see the other.
    "a2a_inject_render": Source(
        "k8s-operator/internal/controller/platformagent_a2a_manifests.go",
        (
            "func a2aInjectBackendEnabled",
            "func buildA2AGatewayDeployment",
            ") applyA2AInjectBackend(",
            ") reconcileA2ANetworkFences(",
            "a2aInjectBackendEnvVar =",
        ),
    ),
    "a2a_inject_identity": Source(
        "a2a/gateway/gchat.go",
        ("func (g *Gateway) resolveInjectPrincipal", "injectEvalPrincipalPrefix"),
    ),
    # labelPartOf lives here rather than beside the fence, so resolving the
    # operator's side of the pair needs both files.
    "operator_labels": Source(
        "k8s-operator/internal/controller/manifest_helpers.go",
        ("labelPartOf",),
    ),
    "a2a_spawner": Source(
        "a2a/gateway/spawn.go",
        ("partOfValue", "sessionRole", "AutomountServiceAccountToken"),
    ),
    # --- model egress -----------------------------------------------------
    # The redactor the chart mounts into the LiteLLM gateway. It is a copy of
    # the chat plugin's module, and tests/test_litellm_redaction.py keeps the
    # two identical; C1 reads this one because this one is what runs at the
    # egress point. The anchors are the pattern names and the exemption the
    # two C1 tests exercise, so a rename fails the self-check rather than
    # letting the assertions pass against a module that no longer redacts.
    "gateway_redactor": Source(
        "charts/kube-agents/files/redactor.py",
        (
            "GCP_OAUTH_TOKEN_PATTERN",
            "GCP_API_KEY_PATTERN",
            "JWT_PATTERN",
            "PRIVATE_KEY_PATTERN",
            "SECRET_BLOCK_PATTERN",
            "gserviceaccount",
            "def redact_text(",
        ),
    ),
    # The operator half of C1's identity check. The spawner names a session
    # ServiceAccount; this file is where that account is built and where every
    # RBAC binding the A2A stack renders lives, so it is what decides whether
    # the name the spawner uses authorises anything.
    "a2a_callout_rbac": Source(
        "k8s-operator/internal/controller/platformagent_a2a_callout.go",
        ("func buildA2ASessionServiceAccount", "Subjects: []rbacv1.Subject{"),
    ),
    # A3's task-plane writer sets, and A5's split of the shared `worker`
    # credential into a callout principal for the agent container and a static
    # one for the bridge sidecar. The bus principals are data in one Go file
    # (the rendered map and the static nats.conf users come from the same
    # list), except the session's, which the callout derives from the attested
    # pod name at mint time and which is therefore in no map at all. Two files,
    # two modules, and the writer-set invariant is about the union. The same
    # file also holds one half of a contract neither module can import: the
    # operator renders an env var naming the agent's principal, and the `a2a`
    # CLI reads it back to pin its inbox prefix.
    "a2a_identities": Source(
        "k8s-operator/internal/controller/platformagent_a2a_identities.go",
        (
            "func gatewayIdentity",
            "func agentIdentity(",
            "func bridgeIdentity(",
            "a2a.tasks.*.*.supervisor",
            "a2aBusUserEnv",
            "a2aAgentBusUser",
            "a2aBridgeUser",
        ),
    ),
    # The JetStream halves of the publish lists live in the manifests file, and
    # the identity builders append them by call. A reader of the identities file
    # alone sees an unresolved `append(publish, a2aAgentJetStreamGrants()...)`
    # and has to either resolve it here or report the list as partial -- and a
    # partial list for a callout principal, which appears in no served config,
    # is a list no test can check against anything.
    "a2a_jetstream_grants": Source(
        "k8s-operator/internal/controller/platformagent_a2a_manifests.go",
        (
            "func a2aAgentJetStreamGrants(",
            "func a2aBridgeJetStreamGrants(",
            "func a2aSeedJetStreamGrants(",
        ),
    ),
    "a2a_session_grants": Source(
        "a2a/authcallout/session.go",
        ("func sessionGrants", "lib.TaskEventsSubject(pod,"),
    ),
    # What the server actually loads, as opposed to what the Go builders say.
    # The writer-set tests read both and assert they agree: the Go map is what
    # a developer edits, this is what NATS enforces, and a reader of only the
    # first has been wrong before -- a builder that assembled its list in a
    # local variable instead of a struct literal parsed as *no grants at all*,
    # which made A3's own known violation report as closed.
    "a2a_rendered_nats_conf": Source(
        "a2a/authcallout/testdata/rendered-nats.conf",
        ("user: bridge", "auth_users:", "a2a.tasks.*.*.supervisor"),
    ),
    # The operator side of the projected-token contract: the audience it mints
    # under, the path it mounts at, and the two strips that keep a user-authored
    # container from mounting the same volume.
    "operator_a2a_callout": Source(
        "k8s-operator/internal/controller/platformagent_a2a_callout.go",
        (
            "a2aBusTokenAudience",
            "a2aBusTokenPath",
            "a2aBusTokenFile",
            "a2aBusTokenVolume",
            "func a2aBusTokenVolumeSource(",
            "func a2aStripBusTokenMounts(",
        ),
    ),
    # The audience the projection above mints under is no longer declared
    # beside it. It moved to the API package when the validating webhook
    # became its second reader -- a user-authored volume that projects a token
    # for this audience is the bus credential by another name, and a second
    # spelling in the webhook package would drift from the one the render
    # mints under. C1 reads the literal here and holds the controller's
    # constant to being a reference to it, so both files are needed to resolve
    # the operator half of the contract.
    "operator_bus_api": Source(
        "k8s-operator/api/v1alpha1/common_types.go",
        (
            # The assignment rather than the bare name: the name also appears
            # in the doc comment and in BusCredentialRoutes, so an anchor on
            # it alone survives the declaration being renamed away.
            'A2ABusTokenAudience = "',
            "func A2ACredentialSecretNames(",
            "func BusCredentialRoutes(",
            # D2 reads the same file for the read-only switch: the env
            # denylist it cuts out before scanning, the switch's own entry in
            # it, and the struct a CRD-level boolean would be declared in.
            # The declaration and the map key rather than the bare name,
            # which two doc comments also spell -- so a guard keyed on the
            # name alone was answered by prose after the block was deleted.
            # The key up to its colon and no further: gofmt pads between the
            # colon and the value to align a run of entries, so the spacing
            # after it belongs to whichever neighbour is longest.
            "var SensitiveEnvVars = map[string]struct{}{",
            '"CREDENTIAL_PROXY_ENFORCE_READ_ONLY":',
            "type SecuritySpec struct {",
        ),
    ),
    # The CRD's root type, D2's third surface. A field offered on
    # PlatformAgentSpec itself rather than on one of the embedded specs lands
    # here and nowhere in common_types.go.
    "operator_platformagent_api": Source(
        "k8s-operator/api/v1alpha1/platformagent_types.go",
        ("type PlatformAgentSpec struct {", "type PlatformAgent struct {"),
    ),
    "a2a_bus_credentials": Source(
        "a2a/lib/credentials.go",
        ("EnvBusUser", "EnvBusTokenFile", "BusTokenAudience", "BusTokenPath"),
    ),
    "a2a_cli_main": Source(
        "a2a/cmd/a2a/main.go",
        ("func busUser(", "lib.EnvBusUser", "lib.WithKSAToken"),
    ),
    # --- supply chain -----------------------------------------------------
    "skill_sync": Source(
        "scripts/sync-upstream-skills.py",
        ("UPSTREAM_REPO", "--depth"),
    ),
    "tags_env": Source("tags.env", ("HERMES_AGENT_TAG",)),
    # `repository:` is C4's read. The platformAgent block is D2's: it is
    # where a chart value for the read-only switch would be offered, and the
    # block moving to a sub-chart left the file present, the switch absent
    # and the test green.
    "chart_values": Source(
        "charts/kube-agents/values.yaml",
        ("repository:", "\nplatformAgent:\n"),
    ),
    # --- the write plane --------------------------------------------------
    "codeowners_example": Source(
        "examples/gitops-repo/CODEOWNERS.example",
        ("/clusters/", "@your-org/"),
    ),
    "autopush_agent_workflow": Source(
        ".github/workflows/autopush-deploy.yml",
        ("workflow_run", "head_branch"),
    ),
    # --- the published site ----------------------------------------------
    # The page AGENTS.md names as the home of what the agent may and may not
    # do. D2 requires it to be among the pages site_pages() finds, which is
    # what ties an unregistered directory walk to a registered file.
    "site_security_reference": Source(
        f"{SITE_CONTENT_DIR}/reference/security-and-iam.md",
        ("## What the agent can and cannot do",),
    ),
}

_GOLDEN_KEYS = (
    "golden_default",
    "golden_tagged",
    "golden_scoped_sa",
    "golden_egress_allowlist",
    "golden_ha",
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


def site_pages() -> list[Path]:
    """Every page of the published site, which is never legitimately empty.

    Raises rather than returning [] for the reason text() does: an assertion
    that no page names something is true of no pages. D2 walked this tree
    behind an `if docs.is_dir():` with no else, so a restructure would have
    skipped the customer-facing half of the assertion without a word.
    """
    root = REPO_ROOT / SITE_CONTENT_DIR
    if not root.is_dir():
        raise AssertionError(f"the site content tree is missing: {root}")
    pages = sorted(
        page for suffix in SITE_PAGE_SUFFIXES for page in root.rglob(f"*{suffix}")
    )
    if not pages:
        raise AssertionError(f"no {SITE_PAGE_SUFFIXES} page under {root}; the site is empty")
    return pages


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


#: What an inline Helm expression becomes in helm_documents(). Not a name any
#: RBAC verb, group or resource has a reason to contain, so a caller can assert
#: it did not land in a field the caller is reading.
HELM_PLACEHOLDER = "helm-expression"

_HELM_EXPRESSION = re.compile(r"\{\{-?.*?-?\}\}")


@functools.lru_cache(maxsize=None)
def helm_documents(name: str) -> tuple[dict, ...]:
    """Every YAML document in a chart template, with expressions neutralized.

    yaml_documents() refuses a source carrying an inline `{{ }}`, on the
    grounds that an expression inside a value changes what the object says and
    dropping it silently would let a chart assert something the cluster never
    sees. That is right for a template whose values are the assertion. It is
    wrong for one where the fields under test are plain and only the names
    around them are templated -- an RBAC rule's verbs next to a
    `{{ .Release.Name }}` in metadata -- because refusing there means not
    reading the object at all, which is how the chart's leader-election Role
    came to be parsed by nothing.

    So the expression becomes HELM_PLACEHOLDER instead of an exception, and
    the obligation the refusal used to discharge moves to the caller: check
    that the placeholder is not sitting in a field you are about to trust.
    """
    body = "\n".join(
        line for line in text(name).splitlines() if not _HELM_DIRECTIVE.match(line)
    )
    body = _HELM_EXPRESSION.sub(HELM_PLACEHOLDER, body)
    return tuple(d for d in yaml.safe_load_all(body) if isinstance(d, dict))


def golden_documents() -> dict[str, tuple[dict, ...]]:
    """The rendered PlatformAgent object sets, keyed by fixture name.

    Five fixtures cover five spec shapes the operator renders: the default
    layout, the same with a pinned image tag, the scoped service-account pool,
    the egress allowlist, and the above-one-replica deployment. An invariant
    about the rendered output has to hold on all five or it is a property of
    one configuration.

    The split-broker fixture was the third of these until #913 deleted it: the
    broker is its own Deployment unconditionally now, so the layout it covered
    is no longer a configuration to render.
    """
    return {key: yaml_documents(key) for key in _GOLDEN_KEYS}


def objects_of_kind(documents: tuple[dict, ...], kind: str) -> list[dict]:
    return [d for d in documents if d.get("kind") == kind]


def containers_of(document: dict) -> list[dict]:
    """Every container and init container in a Deployment document."""
    spec = document.get("spec", {}).get("template", {}).get("spec", {})
    return list(spec.get("initContainers") or []) + list(spec.get("containers") or [])


def gateway_redactor_module():
    """The redactor the gateway runs, imported from the chart's copy by path.

    By path rather than through the chat plugin package, because the chart's
    file is the one mounted into the LiteLLM pod; a test of what leaves the
    estate has to read the artifact that does the leaving.
    """
    import importlib.util

    path = path_of("gateway_redactor")
    spec = importlib.util.spec_from_file_location(GATEWAY_REDACTOR_MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, as the import system does: the module
    # declares a dataclass, and dataclasses resolve the defining module through
    # sys.modules while the class body is being processed.
    sys.modules[GATEWAY_REDACTOR_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


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
    `credential_proxy.Policy.load` uses and builds the match text with the
    broker's own `policy_match_text` — the normalisation `Policy.blocked_by`
    matches against, imported rather than re-spelled so this helper cannot
    drift into a second, quieter parser (D15's whole subject). The join stays
    variable through `_match_rules`, which is what
    test_D15_parser_differentials.py uses to expose the checker/executor
    split; this function is the fidelity half, that one is the differential
    half.
    """
    return _match_rules(credential_proxy.policy_match_text(argv))


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

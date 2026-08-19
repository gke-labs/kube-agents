#!/usr/bin/env python3
"""Verify the three install surfaces still agree on the values they share.

kube-agents can be installed three ways, and each spells the same install out
in its own language:

* the **provisioning scripts** (``k8s-operator/scripts/``) plus the kustomize
  manifests they apply (``k8s-operator/config/``) — the source of truth;
* the **Terraform** modules and the ``full-install`` composition
  (``terraform/``);
* the **Helm chart** (``charts/kube-agents/``).

Nothing forces them to move together, and they have not: #542 bumped the
LiteLLM image everywhere except the chart, and #519 added model aliases the
chart never grew. Both survived review because reviewing a Terraform diff does
not put a chart default in front of you.

This script checks the mechanical subset — the scalar values two surfaces must
literally agree on. It cannot check intent; that is the ``review-iac-parity``
skill's job (``.agents/skills/review-iac-parity/SKILL.md``), and the two share
the divergence list below.

Its own extractors are covered by ``scripts/test_check_iac_parity.py``: a
mis-parse that still returns a plausible value would report parity across
surfaces that have drifted, which is worse than no check at all.

**Deliberate divergences, not checked here** (each documented where it lives):

* **Admission webhooks are off by default in the chart** and on in the
  kustomize path. The wiring exists on both sides now, but the chart cannot
  install the cert-manager it depends on, so a default-on chart would fail at
  apply time on any cluster without it. ``terraform/examples/full-install``
  installs cert-manager and turns them on. Version and admission paths *are*
  compared, below.
* **The webhooks' ``failurePolicy`` is ``Ignore`` in the chart** and ``Fail`` in
  the kustomize copy. Helm applies the webhook configurations before both the
  Certificate and the PlatformAgent CR, so ``Fail`` deadlocks a fresh install of
  a release that creates the CR. See the chart's ``values.yaml``.
* ``provision_01_gcp_cluster.sh`` passes
  ``--addons=GcpFilestoreCsiDriver,BackupRestore``. The ``gke-cluster`` module
  mirrors the ``BackupRestore`` half and not the ``GcpFilestoreCsiDriver`` half:
  nothing in the harness mounts a Filestore volume, so the module does not turn
  on a driver no workload here asks for. This sits under the Autopilot/Standard
  divergence above — ``gcloud container clusters create-auto`` has no
  ``--addons`` flag at all, so the script's own Autopilot path could not pass
  either half.
* The chart rejects ``modelProvider: chatgpt``; that provider needs the
  kustomize overlay's OAuth-token PVC.
* The ``gke-cluster`` module builds an **Autopilot** cluster where
  ``provision_01_gcp_cluster.sh`` builds a Standard one, so node-level settings
  (machine type, gVisor node pool, managed-OTel scope) have no Terraform
  counterpart.
* LiteLLM's OTel callback is unconditional in the kustomize base and gated on
  ``litellm.otel`` in the chart, because a chart install may target a cluster
  with no managed collector.
* ``harness.hermes.dashboardEnabled`` defaults to ``true`` in the CRD and
  ``false`` on the script path. A real inconsistency, tracked in the chart
  README rather than papered over here.
* **The GitHub minter's Kubernetes surface is script-only**: the ``github-minter``
  module creates IAM and KMS only, while the minter Deployment/KSA/NetworkPolicy
  (``k8s-operator/config/integrations/github/``), the ``github-app-credentials``
  Secret (``provision_07``), and the App PEM import (``provision_10``) have no
  Terraform or chart counterpart.
* **cert-manager is installed differently by design**: the script patches
  Autopilot deployments to ``--leader-elect=false`` and skips an existing
  install; the composition moves the leader-election lease into the
  cert-manager namespace instead, and fails on a pre-existing install
  (``enable_cert_manager = false`` is the escape). See the comments in
  ``terraform/examples/full-install/main.tf``.
* **The Hindsight memory store (``provision_13``) is script-only.**
  Hindsight-backed memory providers need
  ``k8s-operator/config/integrations/hindsight/``, which neither the chart nor
  Terraform deploys; the chart's values comment warns that selecting such a
  provider on those paths points the agent at a Service that does not exist.
  The default ``multiuser_memory`` needs none of it.
* **``full-install`` enables a superset of APIs** (``iam``, ``monitoring``,
  ``logging``): Terraform must enable what its own resources call, where
  gcloud enables APIs implicitly.
* **``googleChat.homeChannel`` is settable from the chart and Terraform only**;
  ``platform-agent.yaml.template`` hardcodes it empty. A script-path init_var
  is a follow-up, not silent drift.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_iac_parity.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

COMMON_SH = REPO / "k8s-operator/scripts/common.sh"
PROVISION_01 = REPO / "k8s-operator/scripts/provision_01_gcp_cluster.sh"
PROVISION_03 = REPO / "k8s-operator/scripts/provision_03_gcp_gke_operator.sh"
PROVISION_04 = REPO / "k8s-operator/scripts/provision_04_gcp_iam.sh"
PROVISION_05 = REPO / "k8s-operator/scripts/provision_05_gcp_gchat.sh"
PROVISION_12 = REPO / "k8s-operator/scripts/provision_12_gke_backup_plan.sh"
LITELLM_DEPLOYMENT = REPO / "k8s-operator/config/integrations/litellm/base/deployment.yaml"
LITELLM_CONFIG = REPO / "k8s-operator/config/integrations/litellm/base/config.yaml"
# The kustomize base substitutes ${LITELLM_IMAGE} rather than carrying a
# literal, so the pin it used to be read from lives here now.
IMAGE_INVENTORY = REPO / "images.json"
CR_TEMPLATE = REPO / "k8s-operator/scripts/platform-agent.yaml.template"
WEBHOOK_MANIFESTS = REPO / "k8s-operator/config/webhook/manifests.yaml"

CHART_VALUES = REPO / "charts/kube-agents/values.yaml"
CHART_LITELLM = REPO / "charts/kube-agents/templates/litellm.yaml"
# The gateway's config body lives in a named template so the ConfigMap and the
# Deployment's checksum cannot disagree; the aliases are therefore here, not in
# litellm.yaml.
CHART_HELPERS = REPO / "charts/kube-agents/templates/_helpers.tpl"
CHART_WEBHOOKS = REPO / "charts/kube-agents/templates/operator-webhooks.yaml"

TF_FULL_INSTALL = REPO / "terraform/examples/full-install/main.tf"
TF_FULL_INSTALL_VARS = REPO / "terraform/examples/full-install/variables.tf"
TF_IAM_VARS = REPO / "terraform/modules/kube-agents-iam/variables.tf"
TF_CLUSTER_VARS = REPO / "terraform/modules/gke-cluster/variables.tf"
TF_MINTER_VARS = REPO / "terraform/modules/github-minter/variables.tf"
TF_CHAT_VARS = REPO / "terraform/modules/chat-pubsub/variables.tf"
TF_BACKUP_VARS = REPO / "terraform/modules/gke-backup-plan/variables.tf"

# Stand-in for "whatever MODEL_DEFAULT_NAME resolves to", so the kustomize
# ${MODEL_DEFAULT_NAME} alias and the chart's {{ $model }} alias compare equal.
MODEL_PLACEHOLDER = "<default-model>"


class Failures(list):
    def add(self, check: str, detail: str) -> None:
        self.append((check, detail))


# ─── extraction helpers ───────────────────────────────────────────────────────


def read(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"ERROR: expected file is missing: {path.relative_to(REPO)}")
    return path.read_text(encoding="utf-8")


def shell_assignment(text: str, name: str, path: Path) -> str:
    """Value of NAME="value" in a shell script.

    common.sh repeats its identifier exports in several branches; every
    occurrence has to agree, or the value depends on which branch ran and
    there is nothing single for the other surfaces to mirror.
    """
    values = re.findall(rf'^\s*(?:export\s+)?{re.escape(name)}="([^"]*)"', text, re.M)
    if not values:
        sys.exit(f"ERROR: no {name}= assignment in {path.relative_to(REPO)}")
    if len(set(values)) > 1:
        sys.exit(
            f"ERROR: {name} is assigned {sorted(set(values))} in "
            f"{path.relative_to(REPO)}; the parity check needs one value"
        )
    return values[0]


def init_var_default(text: str, name: str, path: Path) -> str:
    """Default of `init_var "NAME" "default" "prompt"` in a provisioning step."""
    match = re.search(rf'init_var\s+"{re.escape(name)}"\s+"([^"]*)"', text)
    if not match:
        sys.exit(f"ERROR: no init_var for {name} in {path.relative_to(REPO)}")
    return match.group(1)


def bash_array(text: str, name: str, path: Path) -> list[str]:
    """Elements of `local name=( "a" "b" )`, comments stripped.

    The comment strip is load-bearing, not tidiness: these arrays are IAM role
    bundles, and a role commented out on one side would otherwise be read as
    still granted — the check would then demand the other surface grant it.
    """
    match = re.search(rf"{re.escape(name)}=\(\s*(.*?)\)", text, re.S)
    if not match:
        sys.exit(f"ERROR: no {name}=( ... ) array in {path.relative_to(REPO)}")
    return re.findall(r'"([^"]+)"', re.sub(r"#.*", "", match.group(1)))


def tf_list(text: str, assignment: str, path: Path) -> list[str]:
    """Elements of a Terraform `name = [ "a", "b" ]` list, comments stripped."""
    match = re.search(rf"{re.escape(assignment)}\s*=\s*\[(.*?)\]", text, re.S)
    if not match:
        sys.exit(f"ERROR: no {assignment} = [ ... ] list in {path.relative_to(REPO)}")
    body = re.sub(r"#.*", "", match.group(1))
    return re.findall(r'"([^"]+)"', body)


def tf_variable_default(text: str, name: str, path: Path) -> str | list[str]:
    """Default of a Terraform `variable "name" { ... default = ... }` block."""
    block = re.search(
        rf'variable\s+"{re.escape(name)}"\s*\{{(.*?)\n\}}', text, re.S
    )
    if not block:
        sys.exit(f"ERROR: no variable {name!r} in {path.relative_to(REPO)}")
    body = block.group(1)
    # Anchored to the start of a line: an unanchored `default\s*=` also matches
    # inside a validation's condition or error_message, and would then compare
    # against text from the wrong line while still looking like a clean parse.
    listed = re.search(r"^\s*default\s*=\s*\[(.*?)\]", body, re.S | re.M)
    if listed:
        return re.findall(r'"([^"]+)"', re.sub(r"#.*", "", listed.group(1)))
    scalar = re.search(r"^\s*default\s*=\s*(.+)$", body, re.M)
    if not scalar:
        sys.exit(f"ERROR: variable {name!r} has no default in {path.relative_to(REPO)}")
    return scalar.group(1).strip().strip('"')


def simple_yaml(text: str) -> dict:
    """Parse the `key: value` subset values.yaml is written in.

    Nested maps by indentation, scalars as strings, everything else (list
    items, block scalars) skipped — values.yaml uses none of it, and a parser
    that quietly mangled them would be worse than one that ignores them.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or raw.lstrip().startswith("-"):
            continue
        indent = len(raw) - len(raw.lstrip())
        match = re.match(r"([A-Za-z_][\w.-]*):\s*(.*)$", raw.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        value = re.sub(r"\s+#.*$", "", value).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value.strip('"').strip("'")
    return root


def dig(tree: dict, path: str):
    node = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            sys.exit(f"ERROR: {CHART_VALUES.relative_to(REPO)} has no key {path}")
        node = node[part]
    return node


def model_names(text: str, path: Path) -> list[str]:
    """`model_name:` aliases in a LiteLLM config, placeholders normalised.

    Finding none means the config moved out of this file, not that the gateway
    serves no models — so it fails loudly rather than reporting an empty list
    the alias comparison would render as a mismatch.
    """
    names = []
    for name in re.findall(r"model_name:\s*(\S.*?)\s*$", text, re.M):
        if name in ("${MODEL_DEFAULT_NAME}", "{{ .model }}", "{{ $model }}"):
            name = MODEL_PLACEHOLDER
        names.append(name)
    if not names:
        sys.exit(f"ERROR: no model_name aliases found in {path.relative_to(REPO)}")
    return names


def cache_control_points(text: str, path: Path) -> list[str]:
    """The prompt-cache breakpoints a LiteLLM config injects, in order.

    Each point flattens to one ``key=value`` string, sorted within the point but
    not across them: order is load-bearing, since the 1h system breakpoint has to
    precede the rolling 5m ones. Nesting is discarded — ``control.ttl`` and a
    hypothetical top-level ``ttl`` would compare equal — which is enough to catch
    the drift this guards against without a YAML dependency.

    Finding no block means caching moved elsewhere, not that the gateway injects
    nothing, so it exits rather than returning [] for both surfaces and passing.
    """
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if line.strip() == "cache_control_injection_points:":
            outer = len(line) - len(line.lstrip())
            break
    else:
        sys.exit(f"ERROR: no cache_control_injection_points in {path.relative_to(REPO)}")

    points: list[dict[str, str]] = []
    for line in lines[start + 1 :]:
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= outer:
            break
        if body.startswith("- "):
            points.append({})
            body = body[2:].strip()
        key, _, value = body.partition(":")
        value = value.strip()
        if value and points:
            points[-1][key.strip()] = value.strip('"').strip("'")
    if not points:
        sys.exit(f"ERROR: cache_control_injection_points is empty in {path.relative_to(REPO)}")
    return [" ".join(f"{k}={v}" for k, v in sorted(p.items())) for p in points]


# ─── checks ───────────────────────────────────────────────────────────────────


def inventory_pin(name: str) -> tuple[str, str]:
    """The repository and tag ``images.json`` carries for an entry.

    Several pins that used to sit in a manifest now live only here, because the
    manifest substitutes a ``${VAR}`` a mirrored install can redirect. Reading
    the inventory keeps the checks below comparing against the tag
    ``make mirror-images`` actually copies rather than one no surface carries
    any more. Exits rather than reporting a failure: a missing entry means the
    extractor is looking in the wrong place, not that two surfaces disagree.
    """
    entry = next(
        (i for i in json.loads(read(IMAGE_INVENTORY))["images"] if i.get("name") == name),
        None,
    )
    if not entry or not entry.get("tag"):
        sys.exit(f"ERROR: no pinned '{name}' entry in {IMAGE_INVENTORY.relative_to(REPO)}")
    return entry["repository"], entry["tag"]


def check_litellm_image(f: Failures) -> None:
    repo, tag = inventory_pin("litellm")

    values = simple_yaml(read(CHART_VALUES))
    chart_repo = dig(values, "litellm.image.repository")
    chart_tag = dig(values, "litellm.image.tag")
    if (chart_repo, chart_tag) != (repo, tag):
        f.add(
            "litellm-image",
            f"chart pins {chart_repo}:{chart_tag}, the inventory pins {repo}:{tag} "
            f"({CHART_VALUES.relative_to(REPO)} vs {IMAGE_INVENTORY.relative_to(REPO)})",
        )

    # The example manifests are copies of the same gateway; a version bump that
    # skips them leaves users pasting an old image.
    for example in sorted(REPO.glob("examples/litellm-*/deployment.yaml")):
        found = re.search(r"image:\s*\S+/litellm:(\S+)", example.read_text(encoding="utf-8"))
        if found and found.group(1) != tag:
            f.add(
                "litellm-image",
                f"{example.relative_to(REPO)} pins {found.group(1)}, the inventory pins {tag}",
            )

    # A fourth pin, in a chart of its own and in repository/tag form rather than
    # image: form — which is exactly why it was missed by the bump this check
    # exists to prevent a repeat of.
    staging = REPO / "k8s-operator/testing/staging_workloads/charts/workload-bundle/values.yaml"
    if staging.is_file():
        found = re.search(
            r'repository:\s*"?(\S*/litellm)"?\s*\n\s*tag:\s*"?([^"\s]+)"?',
            staging.read_text(encoding="utf-8"),
        )
        if not found:
            f.add("litellm-image", f"no litellm repository/tag pair found in {staging.relative_to(REPO)}")
        elif (found.group(1), found.group(2)) != (repo, tag):
            f.add(
                "litellm-image",
                f"{staging.relative_to(REPO)} pins {found.group(1)}:{found.group(2)}, "
                f"the inventory pins {repo}:{tag}",
            )


def check_litellm_aliases(f: Failures) -> None:
    kustomize = model_names(read(LITELLM_CONFIG), LITELLM_CONFIG)
    chart = model_names(read(CHART_HELPERS), CHART_HELPERS)
    if sorted(kustomize) != sorted(chart):
        f.add(
            "litellm-model-aliases",
            f"chart serves {chart}, kustomize base serves {kustomize} "
            f"({CHART_HELPERS.relative_to(REPO)} vs {LITELLM_CONFIG.relative_to(REPO)})",
        )


def check_litellm_cache_points(f: Failures) -> None:
    """The prompt-cache breakpoints, which only the gateway config carries.

    The agent asks for "model-default" over the OpenAI wire and never names a
    model, so nothing on the agent side can place these — a chart install that
    lost the block would run uncached against an Anthropic backend and show it
    only as a bill.
    """
    kustomize = cache_control_points(read(LITELLM_CONFIG), LITELLM_CONFIG)
    chart = cache_control_points(read(CHART_HELPERS), CHART_HELPERS)
    if kustomize != chart:
        f.add(
            "litellm-cache-control",
            f"chart injects {chart}, kustomize base injects {kustomize} "
            f"({CHART_HELPERS.relative_to(REPO)} vs {LITELLM_CONFIG.relative_to(REPO)})",
        )


def check_model_defaults(f: Failures) -> None:
    """common.sh's per-provider default model vs the chart's dict."""
    text = read(COMMON_SH)
    body = re.search(r"default_model_for_provider\(\)\s*\{(.*?)\n\}", text, re.S)
    if not body:
        sys.exit(f"ERROR: no default_model_for_provider in {COMMON_SH.relative_to(REPO)}")
    script: dict[str, str] = {}
    fallback: str | None = None
    # The alternatives inside a case arm exclude "|" so each one has exactly one
    # way to match. Letting [^\s)] swallow the separator too made "a|b|c" parse
    # ambiguously and backtrack exponentially on a long unterminated arm (CodeQL
    # py/redos). ArmScannerBacktrackingTest holds the line.
    arm = re.compile(r"^\s*([^\s)|]+(?:\s*\|\s*[^\s)|]+)*)\)\s*echo\s+\"([^\"]+)\"", re.M)
    for patterns, model in arm.findall(body.group(1)):
        for provider in (p.strip() for p in patterns.split("|")):
            if provider == "*":
                # The catch-all arm, not a provider. Anything the case does not
                # name explicitly resolves here — which is how gemini and
                # vertex_ai both get gemini-3.5-flash without being listed.
                # Reading it as an alias for one named provider made every
                # later fall-through provider look absent from common.sh.
                fallback = model
            else:
                script[provider] = model

    # Which providers the scripts actually accept. default_model_for_provider
    # cannot answer that — its `*` arm returns a model for any string at all —
    # so an unknown provider in the chart has to be caught against the validator.
    accepted = re.search(r"is_valid_model_provider\(\)[^=]*=~\s*\^\(([^)]+)\)", text, re.S)
    known = {p.strip() for p in accepted.group(1).split("|")} if accepted else set()

    chart_line = re.search(r"\$defaultModels\s*:=\s*dict\s+(.+?)\}\}", read(CHART_LITELLM))
    if not chart_line:
        sys.exit(f"ERROR: no $defaultModels dict in {CHART_LITELLM.relative_to(REPO)}")
    pairs = re.findall(r'"([^"]+)"', chart_line.group(1))
    chart = dict(zip(pairs[::2], pairs[1::2]))

    # chatgpt is chart-rejected by design, so compare only shared providers.
    for provider, model in sorted(chart.items()):
        if known and provider not in known:
            f.add("model-defaults", f"chart knows provider {provider!r}, common.sh does not")
            continue
        expected = script.get(provider, fallback)
        if expected is None:
            f.add("model-defaults", f"chart knows provider {provider!r}, common.sh does not")
        elif expected != model:
            f.add(
                "model-defaults",
                f"{provider}: chart defaults to {model}, common.sh to {expected}",
            )


def check_registry_prefix(f: Failures) -> None:
    prefix = shell_assignment(read(COMMON_SH), "DEFAULT_REGISTRY_PREFIX", COMMON_SH)
    values = simple_yaml(read(CHART_VALUES))
    for key, image in (
        ("operator.image.repository", "k8s-operator"),
        ("platformAgent.deployment.image.repository", "platform-agent"),
    ):
        actual = dig(values, key)
        if actual != f"{prefix}/{image}":
            f.add(
                "registry-prefix",
                f"chart {key} is {actual}, common.sh's DEFAULT_REGISTRY_PREFIX implies "
                f"{prefix}/{image}",
            )


def check_iam_roles(f: Failures) -> None:
    script = read(PROVISION_04)
    read_only = bash_array(script, "local read_only_roles", PROVISION_04)
    gke_admin = bash_array(script, "local gke_admin_roles", PROVISION_04)

    module_default = tf_variable_default(read(TF_IAM_VARS), "project_roles", TF_IAM_VARS)
    if list(module_default) != read_only:
        f.add(
            "iam-roles",
            f"kube-agents-iam project_roles default {sorted(set(module_default) ^ set(read_only))} "
            f"differs from provision_04's read_only_roles",
        )

    composition = read(TF_FULL_INSTALL)
    for name, expected in (("read_only_roles", read_only), ("gke_admin_roles", gke_admin)):
        actual = tf_list(composition, name, TF_FULL_INSTALL)
        if actual != expected:
            f.add(
                "iam-roles",
                f"full-install local.{name} differs from provision_04's {name}: "
                f"{sorted(set(actual) ^ set(expected))}",
            )


def check_identifiers(f: Failures) -> None:
    """GSA/KSA/namespace/topic names both paths have to pick identically."""
    common = read(COMMON_SH)
    namespace = shell_assignment(common, "NAMESPACE", COMMON_SH)
    agent_ksa = shell_assignment(common, "PLATFORM_AGENT_KSA_NAME", COMMON_SH)
    agent_gsa = shell_assignment(common, "PLATFORM_AGENT_GSA_NAME", COMMON_SH)
    minter_ksa = shell_assignment(common, "GITHUB_MINTER_KSA_NAME", COMMON_SH)
    minter_gsa = shell_assignment(common, "GITHUB_MINTER_GSA_NAME", COMMON_SH)

    iam_vars = read(TF_IAM_VARS)
    minter_vars = read(TF_MINTER_VARS)
    expectations = [
        ("kube-agents-iam namespace", tf_variable_default(iam_vars, "namespace", TF_IAM_VARS), namespace),
        ("kube-agents-iam ksa_name", tf_variable_default(iam_vars, "ksa_name", TF_IAM_VARS), agent_ksa),
        ("kube-agents-iam service_account_id", tf_variable_default(iam_vars, "service_account_id", TF_IAM_VARS), agent_gsa),
        ("github-minter namespace", tf_variable_default(minter_vars, "namespace", TF_MINTER_VARS), namespace),
        ("github-minter ksa_name", tf_variable_default(minter_vars, "ksa_name", TF_MINTER_VARS), minter_ksa),
        ("github-minter service_account_id", tf_variable_default(minter_vars, "service_account_id", TF_MINTER_VARS), minter_gsa),
    ]

    values = simple_yaml(read(CHART_VALUES))
    expectations.append(
        ("chart platformAgent.security.serviceAccountName", dig(values, "platformAgent.security.serviceAccountName"), agent_ksa)
    )

    chat = read(PROVISION_05)
    chat_vars = read(TF_CHAT_VARS)
    topic = init_var_default(chat, "CHAT_TOPIC_NAME", PROVISION_05)
    subscription = init_var_default(chat, "CHAT_SUB_NAME", PROVISION_05)
    expectations += [
        ("chat-pubsub topic_name", tf_variable_default(chat_vars, "topic_name", TF_CHAT_VARS), topic),
        ("chat-pubsub subscription_name", tf_variable_default(chat_vars, "subscription_name", TF_CHAT_VARS), subscription),
        ("chart googleChat.topicName", dig(values, "platformAgent.integration.googleChat.topicName"), topic),
        ("chart googleChat.subscriptionName", dig(values, "platformAgent.integration.googleChat.subscriptionName"), subscription),
    ]

    for label, actual, expected in expectations:
        if actual != expected:
            f.add("identifiers", f"{label} is {actual!r}, the scripts use {expected!r}")


def check_kms_names(f: Failures) -> None:
    cluster_vars = read(TF_CLUSTER_VARS)
    minter_vars = read(TF_MINTER_VARS)
    pairs = [
        (
            "gke-cluster kms_keyring_name",
            tf_variable_default(cluster_vars, "kms_keyring_name", TF_CLUSTER_VARS),
            init_var_default(read(PROVISION_01), "GKE_DB_KMS_KEYRING", PROVISION_01),
        ),
        (
            "gke-cluster kms_key_name",
            tf_variable_default(cluster_vars, "kms_key_name", TF_CLUSTER_VARS),
            init_var_default(read(PROVISION_01), "GKE_DB_KMS_KEY", PROVISION_01),
        ),
        (
            "github-minter kms_keyring_name",
            tf_variable_default(minter_vars, "kms_keyring_name", TF_MINTER_VARS),
            init_var_default(read(PROVISION_04), "KMS_KEYRING", PROVISION_04),
        ),
        (
            "github-minter kms_key_name",
            tf_variable_default(minter_vars, "kms_key_name", TF_MINTER_VARS),
            init_var_default(read(PROVISION_04), "KMS_KEY", PROVISION_04),
        ),
    ]
    for label, actual, expected in pairs:
        if actual != expected:
            f.add("kms-names", f"{label} is {actual!r}, the scripts use {expected!r}")


def check_backup_plan(f: Failures) -> None:
    script = read(PROVISION_12)
    module = read(TF_BACKUP_VARS)
    pairs = [
        (
            "gke-backup-plan cron_schedule",
            tf_variable_default(module, "cron_schedule", TF_BACKUP_VARS),
            init_var_default(script, "BACKUP_CRON_SCHEDULE", PROVISION_12),
        ),
        (
            "gke-backup-plan backup_retain_days",
            str(tf_variable_default(module, "backup_retain_days", TF_BACKUP_VARS)),
            init_var_default(script, "BACKUP_RETAIN_DAYS", PROVISION_12),
        ),
        (
            "gke-backup-plan selected_namespaces",
            tf_variable_default(module, "selected_namespaces", TF_BACKUP_VARS),
            [shell_assignment(read(COMMON_SH), "NAMESPACE", COMMON_SH)],
        ),
    ]
    for label, actual, expected in pairs:
        if actual != expected:
            f.add("backup-plan", f"{label} is {actual!r}, the scripts use {expected!r}")

    # The script derives the plan name; the module must derive the same one.
    script_name = re.search(r'BACKUP_PLAN_NAME="\$\{CLUSTER_NAME\}([^"]*)"', script)
    module_name = re.search(r'"\$\{var\.cluster_name\}([^"]*)"', read(REPO / "terraform/modules/gke-backup-plan/main.tf"))
    if not script_name or not module_name:
        f.add("backup-plan", "could not read the derived BackupPlan name from both sides")
    elif script_name.group(1) != module_name.group(1):
        f.add(
            "backup-plan",
            f"module derives <cluster>{module_name.group(1)!r}, the script derives "
            f"<cluster>{script_name.group(1)!r}",
        )


def check_agent_pull_policy(f: Failures) -> None:
    """The agent CR's imagePullPolicy, which both surfaces state outright.

    Only the agent image is compared. The operator and LiteLLM images have no
    imagePullPolicy in their kustomize manifests at all, so there is no second
    value to disagree with — and both are tag-pinned, where the chart's
    IfNotPresent is what Kubernetes would default to anyway.
    """
    template = read(CR_TEMPLATE)
    match = re.search(r"^\s*imagePullPolicy:\s*(\S+)", template, re.M)
    if not match:
        sys.exit(f"ERROR: no imagePullPolicy in {CR_TEMPLATE.relative_to(REPO)}")
    chart = dig(simple_yaml(read(CHART_VALUES)), "platformAgent.deployment.image.pullPolicy")
    if chart != match.group(1):
        f.add(
            "agent-pull-policy",
            f"chart platformAgent.deployment.image.pullPolicy is {chart}, "
            f"{CR_TEMPLATE.relative_to(REPO)} sets {match.group(1)}",
        )


def check_litellm_replicas(f: Failures) -> None:
    kustomize = read(LITELLM_DEPLOYMENT)
    match = re.search(r"^\s*replicas:\s*(\d+)", kustomize, re.M)
    if not match:
        sys.exit(f"ERROR: no replicas in {LITELLM_DEPLOYMENT.relative_to(REPO)}")
    chart = dig(simple_yaml(read(CHART_VALUES)), "litellm.replicaCount")
    if chart != match.group(1):
        f.add(
            "litellm-replicas",
            f"chart litellm.replicaCount is {chart}, kustomize base runs {match.group(1)}",
        )


def check_cert_manager_version(f: Failures) -> None:
    """The cert-manager release both surfaces install.

    The script applies a release manifest by URL and Terraform installs the
    Helm chart of the same version. They are different artefacts of one
    release, so the version is the only thing to compare — but it is the thing
    that matters: the chart's CRD key was renamed at 1.15, so a bump on one
    surface alone either skips the CRDs or installs a different API than the
    operator's Certificate is written against.

    The script builds that URL from ``images.json`` rather than spelling a
    version out, so the mirror and the manifest cannot name different releases.
    That makes the inventory the third surface, and the one to compare against:
    the script is consistent with it by construction, Terraform is not.
    """
    _, version = inventory_pin("cert-manager-controller")
    terraform = tf_variable_default(
        read(TF_FULL_INSTALL_VARS), "cert_manager_version", TF_FULL_INSTALL_VARS
    )
    if terraform != version:
        f.add(
            "cert-manager-version",
            f"terraform cert_manager_version defaults to {terraform}, "
            f"{IMAGE_INVENTORY.relative_to(REPO)} pins cert-manager at {version} "
            f"(which is what {PROVISION_03.relative_to(REPO)} installs)",
        )


def check_webhook_paths(f: Failures) -> None:
    """The admission paths the API server is told to call.

    These strings are generated from kubebuilder markers on the Go types, and
    the chart hand-copies them. A path that agrees with nothing is the worst
    kind of drift here: with the chart's default failurePolicy of Ignore, the
    API server's call 404s, admission is silently skipped, and every
    PlatformAgent is then created unvalidated and undefaulted with no error
    anywhere.
    """
    kustomize = set(re.findall(r"^\s*path:\s*(/\S+)", read(WEBHOOK_MANIFESTS), re.M))
    chart = set(re.findall(r"^\s*path:\s*(/\S+)", read(CHART_WEBHOOKS), re.M))
    if not kustomize:
        sys.exit(f"ERROR: no webhook paths in {WEBHOOK_MANIFESTS.relative_to(REPO)}")
    if kustomize != chart:
        f.add(
            "webhook-paths",
            f"chart serves {sorted(chart)}, "
            f"{WEBHOOK_MANIFESTS.relative_to(REPO)} registers {sorted(kustomize)}",
        )


def hcl_string_local(text: str, name: str, path: Path) -> str:
    """Value of a `name = "value"` line in a Terraform file."""
    match = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"', text, re.M)
    if not match:
        sys.exit(f"ERROR: no {name} assignment in {path.relative_to(REPO)}")
    return match.group(1)


def hcl_resource_buckets(text: str, name: str, path: Path) -> dict:
    """The requests/limits maps of a `name = { requests = {...} limits = {...} }` local."""
    block = re.search(rf"{re.escape(name)}\s*=\s*\{{(.*?)\n  \}}", text, re.S)
    if not block:
        sys.exit(f"ERROR: no {name} block in {path.relative_to(REPO)}")
    buckets: dict = {}
    for bucket in ("requests", "limits"):
        inner = re.search(rf"{bucket}\s*=\s*\{{(.*?)\}}", block.group(1), re.S)
        if not inner:
            sys.exit(
                f"ERROR: {name} in {path.relative_to(REPO)} has no {bucket} map"
            )
        buckets[bucket] = dict(re.findall(r'(\w+)\s*=\s*"([^"]+)"', inner.group(1)))
    return buckets


def check_vertex_litellm_identities(f: Failures) -> None:
    """The Vertex gateway's KSA and GSA names, picked identically three times.

    common.sh exports them for provision_04/09, the composition hardcodes the
    KSA as a local and the GSA as a service_account_id, and the chart carries
    the KSA as litellm.vertex.serviceAccountName. A rename on one surface
    breaks the Workload Identity chain silently: the binding targets one name,
    the pod runs as another, and Vertex calls fail only at request time.
    """
    common = read(COMMON_SH)
    ksa = shell_assignment(common, "LITELLM_KSA_NAME", COMMON_SH)
    gsa = shell_assignment(common, "LITELLM_GSA_NAME", COMMON_SH)

    composition = read(TF_FULL_INSTALL)
    tf_ksa = hcl_string_local(composition, "litellm_ksa", TF_FULL_INSTALL)
    if tf_ksa != ksa:
        f.add(
            "litellm-identities",
            f"full-install litellm_ksa is {tf_ksa!r}, common.sh uses {ksa!r}",
        )
    # Presence, not position: other modules set service_account_id too, so the
    # comparison is "the composition names this exact GSA somewhere".
    if not re.search(rf'^\s*service_account_id\s*=\s*"{re.escape(gsa)}"', composition, re.M):
        f.add(
            "litellm-identities",
            f"full-install has no service_account_id = {gsa!r}, which common.sh "
            "expects for the Vertex gateway GSA",
        )

    chart_ksa = dig(simple_yaml(read(CHART_VALUES)), "litellm.vertex.serviceAccountName")
    if chart_ksa != ksa:
        f.add(
            "litellm-identities",
            f"chart litellm.vertex.serviceAccountName is {chart_ksa!r}, "
            f"common.sh uses {ksa!r}",
        )


def check_host_label(f: Failures) -> None:
    """The discovery label marking clusters that host a kube-agents install.

    common.sh applies it in provision_08 (and removes it in teardown_08); the
    composition writes it as a hardcoded resource_labels map. The Platform
    Agent's fleet discovery filters on the key, so a one-sided rename makes
    Terraform-built clusters invisible to it.
    """
    label = shell_assignment(read(COMMON_SH), "KUBE_AGENTS_HOST_LABEL", COMMON_SH)
    composition = read(TF_FULL_INSTALL)
    match = re.search(r'resource_labels\s*=\s*\{\s*"([^"]+)"\s*=\s*"true"', composition)
    if not match:
        sys.exit(
            f"ERROR: no resource_labels map in {TF_FULL_INSTALL.relative_to(REPO)}"
        )
    if match.group(1) != label:
        f.add(
            "host-label",
            f"full-install labels clusters {match.group(1)!r}, "
            f"common.sh uses {label!r}",
        )


def check_cert_manager_resources(f: Failures) -> None:
    """The resource quotas both installs give cert-manager's Deployments.

    provision_03 patches all three Deployments with one JSON patch; the
    composition expresses the same values once as a local and fans it out in
    chart values. Autopilot bills what is requested, so a one-sided bump
    quietly changes what the two installs cost.
    """
    script = read(PROVISION_03)
    match = re.search(r"resources_patch='(\[.*?\])'", script)
    if not match:
        sys.exit(f"ERROR: no resources_patch in {PROVISION_03.relative_to(REPO)}")
    patched = json.loads(match.group(1))[0]["value"]

    terraform = hcl_resource_buckets(
        read(TF_FULL_INSTALL), "cert_manager_resources", TF_FULL_INSTALL
    )
    for bucket in ("requests", "limits"):
        if terraform[bucket] != patched[bucket]:
            f.add(
                "cert-manager-resources",
                f"full-install cert_manager_resources {bucket} is "
                f"{terraform[bucket]}, provision_03 patches {patched[bucket]}",
            )


CHECKS = (
    check_litellm_image,
    check_litellm_aliases,
    check_litellm_cache_points,
    check_model_defaults,
    check_registry_prefix,
    check_iam_roles,
    check_identifiers,
    check_kms_names,
    check_backup_plan,
    check_agent_pull_policy,
    check_litellm_replicas,
    check_cert_manager_version,
    check_webhook_paths,
    check_vertex_litellm_identities,
    check_host_label,
    check_cert_manager_resources,
)


def main() -> int:
    failures = Failures()
    for check in CHECKS:
        check(failures)

    if failures:
        print(f"{len(failures)} IaC parity problem(s) between the install surfaces:")
        for name, detail in failures:
            print(f"  {name}: {detail}")
        print(
            "\nThe provisioning scripts and k8s-operator/config are the source of truth. "
            "Update the chart and Terraform to match, or — if the divergence is "
            "deliberate — document it and add it to the exemption list in this "
            "script's docstring and in .agents/skills/review-iac-parity/SKILL.md."
        )
        return 1

    print(f"IaC parity: {len(CHECKS)} checks passed across scripts, Terraform, and the Helm chart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Compare the chart's hand-maintained webhook template with k8s-operator/config/webhook.

`hack/sync-chart-manifests.sh` regenerates the chart's CRD, ClusterRole and admission-policy
copies from `k8s-operator/config/`, but `charts/kube-agents/templates/operator-webhooks.yaml`
is written by hand: the chart wraps the same two webhooks in values gates and adds the
cert-manager objects, so a splice would fight the template. This script is the check that
replaces the splice. It renders that one template with `helm template`, loads the kustomize
source (`config/webhook/manifests.yaml` and `service.yaml`), reduces both sides to the
fields listed below, and fails with a unified diff when they differ.

Compared, per configuration kind and webhook `name`: every field of an
admissionregistration.k8s.io/v1 webhook except the three dropped below, which means
`clientConfig.service.path`, `rules` (order-insensitive, including `scope`), and each of
WEBHOOK_FIELDS whenever either side sets it. Also the webhook Service's `targetPort` per
`port`. Dropped before comparison: object and webhook metadata and annotations;
`clientConfig.service.name` / `namespace` and `caBundle`, which are release-specific or
injected; and `failurePolicy`, which the chart templates from values and defaults to
`Ignore` on purpose while the kustomize copy says `Fail` (values.yaml explains why), so the
two sides are meant to differ there. `.github/workflows/validate.yml` checks only the
`Fail` fresh-install guard, not the value.

A webhook present on one side only is a failure. Nothing is generated.

Exit codes: 0 in sync, DRIFT_EXIT_CODE on a difference, TOOLING_EXIT_CODE when the check
could not run (no helm, no PyYAML, a failed render). The sync script tells them apart.

Run:  python3 hack/check_chart_webhooks.py      (also: make chart-check)
"""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

# Exit codes. Drift is 1; a missing tool or a failed render is 2, so the caller
# (hack/sync-chart-manifests.sh) can tell "the template drifted" from "the check
# could not run" and does not prescribe a hand edit for a missing dependency.
DRIFT_EXIT_CODE = 1
TOOLING_EXIT_CODE = 2

try:
    import yaml
except ImportError:
    print(
        "check_chart_webhooks needs PyYAML: python3 -m pip install pyyaml (or make test-python-deps)",
        file=sys.stderr,
    )
    sys.exit(TOOLING_EXIT_CODE)

REPO_ROOT = Path(__file__).resolve().parent.parent

CHART_DIR = REPO_ROOT / "charts" / "kube-agents"
# Relative to the chart root, the form `--show-only` expects.
CHART_WEBHOOK_TEMPLATE = "templates/operator-webhooks.yaml"
WEBHOOK_SRC = REPO_ROOT / "k8s-operator" / "config" / "webhook" / "manifests.yaml"
SERVICE_SRC = REPO_ROOT / "k8s-operator" / "config" / "webhook" / "service.yaml"

HELM_BINARY = "helm"
HELM_RELEASE_NAME = "chart-check"
# The three harness values are required by the chart's schema; their content does not reach
# the webhook template. Webhooks are off by default in the chart, so the template renders
# nothing without the last --set.
HELM_RENDER_ARGS: tuple[str, ...] = (
    "template",
    HELM_RELEASE_NAME,
    str(CHART_DIR),
    "--set",
    "platformAgent.harness.clusterName=x",
    "--set",
    "platformAgent.harness.location=x",
    "--set",
    "platformAgent.harness.projectId=x",
    "--set",
    "operator.webhooks.enabled=true",
    "--show-only",
    CHART_WEBHOOK_TEMPLATE,
)

# Manifest keys read on the way in.
KIND_KEY = "kind"
WEBHOOKS_KEY = "webhooks"
NAME_KEY = "name"
CLIENT_CONFIG_KEY = "clientConfig"
CLIENT_CONFIG_SERVICE_KEY = "service"
PATH_KEY = "path"
RULES_KEY = "rules"
RULE_SCOPE_KEY = "scope"
SPEC_KEY = "spec"
PORTS_KEY = "ports"
PORT_KEY = "port"
TARGET_PORT_KEY = "targetPort"

WEBHOOK_KINDS: frozenset[str] = frozenset(
    {"MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}
)
SERVICE_KIND = "Service"
# Key of the Service entry in the normalised structure, alongside the webhook keys.
SERVICE_ENTRY_KEY = "Service"

# Webhook fields copied verbatim when present, so one set on a single side is a drift.
# With `clientConfig` and `rules` handled separately and `failurePolicy` dropped (see the
# module docstring), this is every remaining field of a v1 webhook. Selectors are compared
# as whole dicts: the template's own comment says a namespaceSelector on the chart side
# alone would let a CR outside the release namespace go unvalidated, so it must not slip
# past. `admissionReviewVersions` and `matchConditions` keep their order: the first is a
# preference list, the second is evaluated in order.
WEBHOOK_FIELDS: tuple[str, ...] = (
    "sideEffects",
    "admissionReviewVersions",
    "matchPolicy",
    "timeoutSeconds",
    "namespaceSelector",
    "objectSelector",
    "matchConditions",
    "reinvocationPolicy",
)
# Rule list fields, each treated as a set by the API server, so each is sorted. `scope`,
# the rule's one scalar, is copied when present.
RULE_LIST_FIELDS: tuple[str, ...] = ("apiGroups", "apiVersions", "operations", "resources")

DIFF_FROM_LABEL = f"chart ({CHART_WEBHOOK_TEMPLATE}, rendered)"
DIFF_TO_LABEL = "k8s-operator/config/webhook (manifests.yaml + service.yaml)"
JSON_INDENT = 2


def _tooling_failure(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(TOOLING_EXIT_CODE)


def render_chart_webhooks() -> str:
    """Render the chart's webhook template, or exit with a one-line reason."""
    helm = shutil.which(HELM_BINARY)
    if helm is None:
        _tooling_failure(
            f"check_chart_webhooks needs {HELM_BINARY} on PATH to render {CHART_WEBHOOK_TEMPLATE}"
        )
    result = subprocess.run(
        [helm, *HELM_RENDER_ARGS], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        _tooling_failure(f"helm template failed:\n{result.stderr.strip()}")
    return result.stdout


def load_documents(text: str) -> list[dict[str, Any]]:
    """Every non-empty YAML document in `text`, in order."""
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def load_source_documents() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for path in (WEBHOOK_SRC, SERVICE_SRC):
        docs.extend(load_documents(path.read_text(encoding="utf-8")))
    return docs


def _normalise_rule(rule: dict[str, Any]) -> dict[str, Any]:
    normalised = {field: sorted(rule.get(field) or []) for field in RULE_LIST_FIELDS}
    if RULE_SCOPE_KEY in rule:
        normalised[RULE_SCOPE_KEY] = rule[RULE_SCOPE_KEY]
    return normalised


def _normalise_webhook(webhook: dict[str, Any]) -> dict[str, Any]:
    service = (webhook.get(CLIENT_CONFIG_KEY) or {}).get(CLIENT_CONFIG_SERVICE_KEY) or {}
    rules = [_normalise_rule(rule) for rule in webhook.get(RULES_KEY) or []]
    normalised: dict[str, Any] = {
        PATH_KEY: service.get(PATH_KEY),
        RULES_KEY: sorted(rules, key=lambda rule: json.dumps(rule, sort_keys=True)),
    }
    for field in WEBHOOK_FIELDS:
        if field in webhook:
            normalised[field] = webhook[field]
    return normalised


def _normalise_service(service: dict[str, Any]) -> dict[str, Any]:
    ports = [
        {PORT_KEY: entry.get(PORT_KEY), TARGET_PORT_KEY: entry.get(TARGET_PORT_KEY)}
        for entry in (service.get(SPEC_KEY) or {}).get(PORTS_KEY) or []
    ]
    return {PORTS_KEY: sorted(ports, key=lambda entry: str(entry[PORT_KEY]))}


def normalise(documents: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Reduce webhook configurations and the webhook Service to the compared fields.

    Keys are `<kind>/<webhook name>` for webhooks and `Service` for the Service, so a
    webhook present on one side only shows up as a key the other side lacks.
    """
    normalised: dict[str, Any] = {}
    for doc in documents:
        kind = doc.get(KIND_KEY)
        if kind in WEBHOOK_KINDS:
            for webhook in doc.get(WEBHOOKS_KEY) or []:
                normalised[f"{kind}/{webhook.get(NAME_KEY)}"] = _normalise_webhook(webhook)
        elif kind == SERVICE_KIND:
            normalised[SERVICE_ENTRY_KEY] = _normalise_service(doc)
    return normalised


def _dump(structure: dict[str, Any]) -> list[str]:
    return json.dumps(structure, indent=JSON_INDENT, sort_keys=True).splitlines()


def compare(
    chart_documents: Iterable[dict[str, Any]], source_documents: Iterable[dict[str, Any]]
) -> list[str]:
    """Lines describing the drift between the two sides; empty when they agree.

    The first lines name each key that differs; the rest is a unified diff of the two
    normalised structures, chart first, so `+` is what the source has and the chart lacks.
    """
    chart = normalise(chart_documents)
    source = normalise(source_documents)
    if chart == source:
        return []
    drifted = sorted(
        key for key in chart.keys() | source.keys() if chart.get(key) != source.get(key)
    )
    lines = [f"webhook drift: {key}" for key in drifted]
    lines.extend(
        line.rstrip("\n")
        for line in difflib.unified_diff(
            _dump(chart), _dump(source), DIFF_FROM_LABEL, DIFF_TO_LABEL, lineterm=""
        )
    )
    return lines


def main() -> int:
    chart_documents = load_documents(render_chart_webhooks())
    if not normalise(chart_documents):
        _tooling_failure(f"helm rendered no webhook objects from {CHART_WEBHOOK_TEMPLATE}")
    drift = compare(chart_documents, load_source_documents())
    if drift:
        print("\n".join(drift), file=sys.stderr)
        return DRIFT_EXIT_CODE
    print(f"Chart {CHART_WEBHOOK_TEMPLATE} matches k8s-operator/config/webhook.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

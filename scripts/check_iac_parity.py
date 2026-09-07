#!/usr/bin/env python3
"""Assert DNS egress rule parity across static NetworkPolicy copies.

Several static files hand-maintain the same DNS egress rule (port 53):
* charts/kube-agents/templates/litellm.yaml
* charts/kube-agents/templates/github-minter.yaml
* deploy/kustomize/platform/networkpolicy-core-egress.yaml
* examples/litellm-chatgpt-subscription/networkpolicy.yaml
* examples/litellm-gemini/networkpolicy.yaml
* examples/vllm-gemma/networkpolicy.yaml
* k8s-operator/config/integrations/github/deployment.yaml.template
* k8s-operator/config/integrations/litellm/base/networkpolicy.yaml

Context (#747 B5, D1; #687):
#687 had to touch all static copies, and its first draft missed one —
deployment.yaml.template, because greps for *.yaml skip it. Furthermore, #608
added a static policy that omitted the required DNS peers, which would have
blocked DNS resolution outright on clusters with non-standard service CIDRs.

In-cluster DNS answers on the kube-dns Service ClusterIP, which a static
manifest cannot predict: it is 10.96.0.10 on classic service ranges and allocated
from public space (e.g. 34.118.224.0/20) on newer GKE clusters. NetworkPolicy
matches that VIP rather than the backend pods, so selectors alone do not cover it.
To avoid DNS outages, every static DNS rule must provide:
1. Both UDP and TCP protocols on port 53.
2. The 10.96.0.10/32 literal for the classic service range.
3. An 0.0.0.0/0 peer carrying an except list for any ClusterIP outside private space.
4. The except list must block at least RFC 1918 private subnets (10.0.0.0/8,
   172.16.0.0/12, 192.168.0.0/16) to prevent internal lateral movement.
5. If the except list excludes link-local space (169.254.0.0/16), explicit
   169.254.20.10/32 (NodeLocal DNSCache) and 169.254.169.254/32 (Cloud DNS for GKE)
   literals are required because the wildcard peer no longer reaches those resolvers.

Note on peer sets and house shape (#747 B5):
Do not assert strict byte-identity across all files. Today, seven copies carry a
three-entry except list (RFC 1918) and include 169.254.169.254/32, while
deploy/kustomize/platform/networkpolicy-core-egress.yaml carries the 5-entry house
shape (adding 100.64.0.0/10 and 169.254.0.0/16, which is more contained and
mirrors the operator-generated external egress policy). When 169.254.0.0/16 is
excepted, the link-local resolver literals become mandatory. Pod selector peers
(k8s-app: kube-dns, k8s-app: node-local-dns) are present in all copies for in-cluster
pod-backed DNS, but this script enforces the required IP blocks and protocols.

Scope:
This check audits deployable Infrastructure-as-Code manifest files matching
DISCOVERY_FILE_PATTERNS (*.yaml, *.yml, *.yaml.template, *.yml.template,
*.yaml.tmpl, *.yml.tmpl) across Helm templates, Kustomize bases, and integration
examples. Repository-wide discovery deliberately escapes four file classes:
1. Markdown documentation and agent skill prompts (*.md, e.g. embedded NetworkPolicies
   in SKILL.md files like agents/platform/skills/gke-multitenancy/SKILL.md, which is
   a separate defect tracked independently).
2. Extensions outside DISCOVERY_FILE_PATTERNS (e.g. plain *.tpl helper templates
   inside charts/kube-agents/templates/).
3. Any directory named testdata/ at any depth repository-wide (pruned by directory
   name via IGNORED_DIRS, alongside virtualenvs and build caches).
4. Documentation trees pruned by prefix (docs/site/** via IGNORED_PREFIXES).

Template semantics and #747 D1 requirement:
Issue #747 D1 requested an assertion rendering manifests via `helm template` and
`kubectl kustomize`. This check is intentionally delivered as a static source-level
audit rather than engine rendering for three reasons:
1. Heterogeneity: the static copies include raw examples and deployment.yaml.template
   with shell variables that neither Helm nor Kustomize can render.
2. Portability: the check runs offline under standard Python unit tests without
   requiring external helm/kubectl binaries or cluster access.
3. Baseline authoring guard: it ensures every static copy checked into the repository
   maintains the required peer shape in source.

Trade-offs and limitations against D1:
* Conditional presence: Go/Helm control directives ({{- if }}, {{- with }}) are
  stripped during sanitization. A peer or entire policy guarded behind a Helm value
  is evaluated as present if it exists in the source text, even if a specific
  values configuration renders it out. Mutually exclusive Helm branches ({{- else }})
  are retained additively.
* Dynamic Go policies: NetworkPolicies generated in Go controller code (such as
  the operator's platform-agent policy generation) are outside this source scanner's
  view and are tested via Go unit tests instead.
Dynamic rendering under specific Helm value permutations remains the responsibility
of CI chart validation (e.g. validate.yml).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

try:
    import yaml
except ImportError:
    sys.exit(
        "ERROR: scripts/check_iac_parity.py requires PyYAML.\n"
        "Install it with: pip install pyyaml (or make test-python-deps)"
    )

REPO_ROOT = Path(__file__).resolve().parent.parent

DNS_PORT: int = 53
DNS_PORT_STR: str = "53"
DNS_PORTS: tuple[int | str, ...] = (DNS_PORT, DNS_PORT_STR)

DISCOVERY_FILE_PATTERNS: tuple[str, ...] = (
    "*.yaml",
    "*.yml",
    "*.yaml.template",
    "*.yml.template",
    "*.yaml.tmpl",
    "*.yml.tmpl",
)

STATIC_NETWORK_POLICIES: tuple[str, ...] = (
    "charts/kube-agents/templates/litellm.yaml",
    "charts/kube-agents/templates/github-minter.yaml",
    "deploy/kustomize/platform/networkpolicy-core-egress.yaml",
    "examples/litellm-chatgpt-subscription/networkpolicy.yaml",
    "examples/litellm-gemini/networkpolicy.yaml",
    "examples/vllm-gemma/networkpolicy.yaml",
    "k8s-operator/config/integrations/github/deployment.yaml.template",
    "k8s-operator/config/integrations/litellm/base/networkpolicy.yaml",
)

# Manifests containing kind: NetworkPolicy that are deliberately excluded from the
# static DNS parity roster. Every entry must carry its reviewed reason.
# Note: Any directory named testdata/ is ignored systematically at any depth repository-wide
# via IGNORED_DIRS.
EXCLUDED_NETPOL_MANIFESTS: dict[str, str] = {}

# Directory names to skip anywhere in the repo-relative path (applied at any depth)
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "node_modules",
        "__pycache__",
        ".git",
        ".coverage-data",
        ".terraform",
        ".claude",
        "testdata",
    }
)

# Relative directory prefixes to skip
IGNORED_PREFIXES: tuple[str, ...] = (
    "docs/site",
)

REQUIRED_DNS_LITERAL = "10.96.0.10/32"
REQUIRED_WILDCARD_CIDR = "0.0.0.0/0"
REQUIRED_EXCEPT_MINIMUM: frozenset[str] = frozenset(
    {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}
)
HOUSE_SHAPE_EXCEPT: frozenset[str] = frozenset(
    {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"}
)

PROTOCOL_TCP: str = "TCP"
PROTOCOL_UDP: str = "UDP"
REQUIRED_DNS_PROTOCOLS: frozenset[str] = frozenset({PROTOCOL_UDP, PROTOCOL_TCP})

LINK_LOCAL_CIDR = "169.254.0.0/16"
REQUIRED_LINK_LOCAL_DNSCACHE = "169.254.20.10/32"
REQUIRED_LINK_LOCAL_CLOUDDNS = "169.254.169.254/32"
REQUIRED_LINK_LOCAL_LITERALS: frozenset[str] = frozenset(
    {REQUIRED_LINK_LOCAL_DNSCACHE, REQUIRED_LINK_LOCAL_CLOUDDNS}
)

KIND_NETWORK_POLICY = "NetworkPolicy"
TEMPLATE_PLACEHOLDER = "placeholder"

HELM_COMMENT_RE = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", flags=re.DOTALL)
HELM_TAG_MULTILINE_RE = re.compile(r"\{\{.*?\}\}", flags=re.DOTALL)
HELM_TAG_LINE_RE = re.compile(r"\{\{.*?\}\}")
HELM_CONTROL_DIRECTIVE_RE = re.compile(
    r"\{\{-?\s*(?:if\b|else\b|end\b|range\b|with\b).*?-?\}\}"
)
YAML_DOC_SEPARATOR_RE = re.compile(r"^---(?:\s.*)?$", flags=re.MULTILINE)
NETWORK_POLICY_KIND_RE = re.compile(
    r"^\s*kind:\s*['\"]?NetworkPolicy['\"]?", flags=re.MULTILINE
)


def is_house_shape(except_list: Iterable[str]) -> bool:
    """Return True if the except list satisfies the 5-entry house shape."""
    return HOUSE_SHAPE_EXCEPT.issubset(set(except_list))


def governs_egress(spec: object) -> bool:
    """Return True if a NetworkPolicy spec governs egress traffic.

    When policyTypes is explicitly specified, Egress must be included.
    When policyTypes is omitted, Kubernetes enables Egress only if egress rules are present.
    """
    if not isinstance(spec, dict):
        return False
    policy_types = spec.get("policyTypes")
    if isinstance(policy_types, list):
        return "Egress" in policy_types
    if spec.get("policyTypes") is not None:
        return False
    return spec.get("egress") is not None


def sanitize_helm_template(text: str) -> str:
    """Sanitize Go/Helm template tags so that YAML parsers can load the manifests."""
    # Normalize CRLF and CR line endings to LF before tag processing
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip multi-line comments: {{- /* ... */ -}} or {{/* ... */}}
    text = HELM_COMMENT_RE.sub("", text)
    # Replace internal newlines within template tags so every directive is single-line
    text = HELM_TAG_MULTILINE_RE.sub(lambda m: m.group(0).replace("\n", " "), text)
    lines: list[str] = []
    for line in text.splitlines():
        # Check if the line consists entirely of template control tags (ignoring trailing comments)
        line_without_tags = HELM_TAG_LINE_RE.sub("", line)
        if line_without_tags.split("#", 1)[0].strip() == "":
            lines.append("# " + line)
            continue
        # For lines with mixed content, strip control directives (if, else, end, with, range)
        cleaned = HELM_CONTROL_DIRECTIVE_RE.sub("", line)
        # Any remaining template expressions are value interpolations
        cleaned = HELM_TAG_LINE_RE.sub(TEMPLATE_PLACEHOLDER, cleaned)
        lines.append(cleaned)
    return "\n".join(lines)


def load_network_policies(path: Path) -> list[dict]:
    """Read a manifest or template file and return all NetworkPolicy documents.

    Parses document-by-document so syntax anomalies in unrelated manifests (like
    Deployments or ConfigMaps in the same template file) do not fail NetworkPolicy
    extraction.
    """
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    raw_content = path.read_text(encoding="utf-8")
    sanitized = sanitize_helm_template(raw_content)
    policies: list[dict] = []

    # Split documents on YAML boundary '---'
    for chunk in YAML_DOC_SEPARATOR_RE.split(sanitized):
        chunk_stripped = chunk.strip()
        if not chunk_stripped:
            continue
        # Only parse chunks that declare kind: NetworkPolicy
        if not NETWORK_POLICY_KIND_RE.search(chunk_stripped):
            continue
        try:
            doc = yaml.safe_load(chunk_stripped)
            if isinstance(doc, dict) and doc.get("kind") == KIND_NETWORK_POLICY:
                policies.append(doc)
        except Exception as exc:
            raise ValueError(f"malformed NetworkPolicy document in {path}: {exc}") from exc

    return policies


def validate_exclusions(
    exclusions: dict[str, str] | None = None,
    roster: Iterable[str] | None = None,
    root: Path | None = None,
) -> list[str]:
    """Validate that every exclusion carries a non-empty reason, points to a file on disk, and is disjoint from the roster."""
    resolved_exclusions = EXCLUDED_NETPOL_MANIFESTS if exclusions is None else exclusions
    resolved_roster = STATIC_NETWORK_POLICIES if roster is None else roster
    resolved_root = REPO_ROOT if root is None else root

    errors: list[str] = []
    roster_set = set(resolved_roster)
    for path_str, reason in resolved_exclusions.items():
        if not reason or not reason.strip():
            errors.append(f"Exclusion for {path_str} must carry a non-empty reviewed reason")
        if Path(path_str).is_absolute() or path_str.startswith("/"):
            errors.append(
                f"Exclusion path must be repository-relative without a leading slash: {path_str}"
            )
        elif not (resolved_root / path_str).is_file():
            errors.append(f"Excluded manifest does not exist on disk: {path_str}")
        if path_str in roster_set:
            errors.append(f"Excluded manifest {path_str} cannot also be in STATIC_NETWORK_POLICIES roster")
    return errors


def discover_dns_network_policies(root: Path | None = None) -> set[str]:
    """Scan the repository tree for all manifest files defining a port-53 DNS egress rule.

    Scans deployed manifest files (*.yaml, *.yml, *.yaml.template, *.yml.template,
    *.yaml.tmpl, *.yml.tmpl) across the repository. Markdown documents and skill
    prompt files are excluded.

    Returns:
        set[str]: set of repo-relative POSIX file paths.
    """
    resolved_root = REPO_ROOT if root is None else root
    discovered: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(resolved_root):
        # Prune ignored directories in place to avoid entering them
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        rel_dir = Path(dirpath).relative_to(resolved_root)
        if any(part in IGNORED_DIRS for part in rel_dir.parts):
            continue
        rel_dir_posix = rel_dir.as_posix()
        if any(
            rel_dir_posix == prefix or rel_dir_posix.startswith(prefix + "/")
            for prefix in IGNORED_PREFIXES
        ):
            dirnames.clear()
            continue

        for filename in filenames:
            if not any(
                filename.endswith(ext.lstrip("*"))
                for ext in DISCOVERY_FILE_PATTERNS
            ):
                continue

            path = Path(dirpath) / filename
            rel = path.relative_to(resolved_root)
            rel_posix = rel.as_posix()

            if rel_posix in EXCLUDED_NETPOL_MANIFESTS:
                continue

            try:
                raw = path.read_text(encoding="utf-8")
            except Exception as exc:
                raise RuntimeError(
                    f"failed to read {rel_posix} during DNS policy discovery: {exc}"
                ) from exc

            # Fast check before parsing
            if KIND_NETWORK_POLICY not in raw or DNS_PORT_STR not in raw:
                continue

            try:
                policies = load_network_policies(path)
            except Exception as exc:
                raise ValueError(
                    f"failed to parse NetworkPolicy in {rel_posix} during DNS policy discovery: {exc}"
                ) from exc

            for pol in policies:
                spec = pol.get("spec")
                if not governs_egress(spec):
                    continue
                egress_rules = spec.get("egress") if isinstance(spec, dict) else []
                if not isinstance(egress_rules, list):
                    continue
                for rule in egress_rules:
                    if not isinstance(rule, dict):
                        continue
                    ports = rule.get("ports")
                    if not isinstance(ports, list):
                        continue
                    if any(
                        isinstance(p, dict) and p.get("port") in DNS_PORTS
                        for p in ports
                    ):
                        discovered.add(rel_posix)
                        break

    return discovered


def check_dns_egress_rule(
    path_display: str,
    policy_name: str,
    rule: dict,
    rule_idx: int,
) -> list[str]:
    """Verify that a single port-53 egress rule satisfies the required peer and protocol shape."""
    errors: list[str] = []
    rule_desc = f"{path_display} (policy '{policy_name}', DNS rule #{rule_idx})"

    ports = rule.get("ports")
    if ports is not None and not isinstance(ports, list):
        errors.append(f"{rule_desc}: 'ports' field must be a list")
    else:
        rule_protocols: set[str] = set()
        for p in (ports or []):
            if isinstance(p, dict) and p.get("port") in DNS_PORTS:
                proto = p.get("protocol")
                if isinstance(proto, str):
                    rule_protocols.add(proto.upper())
                elif proto is None:
                    # In Kubernetes NetworkPolicyPort, protocol defaults to TCP when omitted
                    rule_protocols.add(PROTOCOL_TCP)
        missing_protos = REQUIRED_DNS_PROTOCOLS - rule_protocols
        if missing_protos:
            errors.append(
                f"{rule_desc}: port 53 egress rule is missing required protocol(s): {sorted(missing_protos)}"
            )

    to_peers = rule.get("to")
    if to_peers is not None and not isinstance(to_peers, list):
        errors.append(f"{rule_desc}: 'to' field must be a list")
        return errors

    has_dns_literal = False
    has_wildcard = False
    wildcard_peers: list[dict] = []
    peer_cidrs: set[str] = set()

    for peer in (to_peers or []):
        if not isinstance(peer, dict) or "ipBlock" not in peer:
            continue
        ip_block = peer.get("ipBlock")
        if not isinstance(ip_block, dict):
            errors.append(f"{rule_desc}: ipBlock must be a mapping")
            continue
        cidr = ip_block.get("cidr")
        if isinstance(cidr, str):
            peer_cidrs.add(cidr)
        if cidr == REQUIRED_DNS_LITERAL:
            has_dns_literal = True
        elif cidr == REQUIRED_WILDCARD_CIDR:
            has_wildcard = True
            wildcard_peers.append(peer)
            except_val = ip_block.get("except")
            peer_excepts: set[str] = set()
            if isinstance(except_val, list):
                peer_excepts = {str(x) for x in except_val}
            elif except_val is not None:
                errors.append(
                    f"{rule_desc}: '{REQUIRED_WILDCARD_CIDR}' peer except must be a list"
                )
            missing = REQUIRED_EXCEPT_MINIMUM - peer_excepts
            if missing:
                errors.append(
                    f"{rule_desc}: '{REQUIRED_WILDCARD_CIDR}' peer except list is missing required private CIDRs: "
                    f"{sorted(missing)}"
                )

    if not has_dns_literal:
        errors.append(
            f"{rule_desc}: missing required ipBlock literal '{REQUIRED_DNS_LITERAL}' for classic ClusterIP DNS"
        )
    if not has_wildcard:
        errors.append(
            f"{rule_desc}: missing required '{REQUIRED_WILDCARD_CIDR}' peer with except list for dynamic/public DNS VIPs"
        )
    elif len(wildcard_peers) > 1:
        errors.append(
            f"{rule_desc}: expected at most one '{REQUIRED_WILDCARD_CIDR}' peer"
        )
    else:
        wildcard_ipblock = wildcard_peers[0].get("ipBlock")
        if isinstance(wildcard_ipblock, dict):
            except_val = wildcard_ipblock.get("except")
            peer_excepts = (
                {str(x) for x in except_val}
                if isinstance(except_val, list)
                else set()
            )
            if LINK_LOCAL_CIDR in peer_excepts:
                missing_link_local = REQUIRED_LINK_LOCAL_LITERALS - peer_cidrs
                if missing_link_local:
                    errors.append(
                        f"{rule_desc}: '{REQUIRED_WILDCARD_CIDR}' except list excludes '{LINK_LOCAL_CIDR}', "
                        f"requiring explicit ipBlock literal(s) for NodeLocal DNSCache and Cloud DNS for GKE: "
                        f"{sorted(missing_link_local)}"
                    )

    return errors


def check_network_policy_file(path: Path, root: Path | None = None) -> tuple[int, list[str]]:
    """Check all Egress NetworkPolicy resources in a file for DNS egress parity.

    Returns:
        tuple[int, list[str]]: (number of DNS rules checked, list of error messages)
    """
    resolved_root = REPO_ROOT if root is None else root
    rel_path = str(path.relative_to(resolved_root)) if path.is_relative_to(resolved_root) else str(path)
    try:
        policies = load_network_policies(path)
    except Exception as exc:
        return 0, [f"{rel_path}: failed to load NetworkPolicy: {exc}"]

    if not policies:
        return 0, [f"{rel_path}: no NetworkPolicy resources found"]

    total_rules = 0
    errors: list[str] = []
    checked_policies = 0

    for policy in policies:
        metadata = policy.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            errors.append(f"{rel_path}: metadata must be a mapping")
            policy_name = "<unnamed>"
        elif isinstance(metadata, dict):
            name_val = metadata.get("name")
            policy_name = name_val if isinstance(name_val, str) else "<unnamed>"
        else:
            policy_name = "<unnamed>"

        spec = policy.get("spec")
        if not isinstance(spec, dict):
            errors.append(f"{rel_path}: NetworkPolicy '{policy_name}' spec must be a mapping")
            continue

        policy_types = spec.get("policyTypes")
        if policy_types is not None and not isinstance(policy_types, list):
            errors.append(f"{rel_path}: NetworkPolicy '{policy_name}' policyTypes must be a list")

        egress_rules = spec.get("egress")
        if egress_rules is not None and not isinstance(egress_rules, list):
            errors.append(f"{rel_path}: NetworkPolicy '{policy_name}' egress must be a list")
            continue

        if not governs_egress(spec):
            continue

        checked_policies += 1
        dns_rules: list[dict] = []
        for r in (egress_rules or []):
            if not isinstance(r, dict):
                errors.append(f"{rel_path}: NetworkPolicy '{policy_name}' egress rule must be a mapping")
                continue
            ports = r.get("ports")
            if ports is not None and not isinstance(ports, list):
                errors.append(f"{rel_path}: NetworkPolicy '{policy_name}' rule ports must be a list")
                continue
            if any(
                isinstance(p, dict) and p.get("port") in DNS_PORTS
                for p in (ports or [])
            ):
                dns_rules.append(r)

        if not dns_rules:
            errors.append(
                f"{rel_path}: NetworkPolicy '{policy_name}' has no egress rule for port 53 (DNS)"
            )
            continue

        for idx, rule in enumerate(dns_rules, start=1):
            total_rules += 1
            rule_errors = check_dns_egress_rule(rel_path, policy_name, rule, idx)
            errors.extend(rule_errors)

    if checked_policies == 0:
        errors.append(f"{rel_path}: no Egress NetworkPolicy resources found")

    return total_rules, errors


def check_all(
    files: Iterable[Path | str] | None = None,
    root: Path | None = None,
    verbose: bool = False,
) -> tuple[int, list[str]]:
    """Check all specified files for DNS egress parity."""
    resolved_files = STATIC_NETWORK_POLICIES if files is None else files
    resolved_root = REPO_ROOT if root is None else root

    total_rules = 0
    all_errors: list[str] = []

    exclusion_errors = validate_exclusions(root=resolved_root)
    all_errors.extend(exclusion_errors)
    if verbose and exclusion_errors:
        for err in exclusion_errors:
            print(f"  FAIL: exclusion contract: {err}")

    for item in resolved_files:
        path = resolved_root / item if not Path(item).is_absolute() else Path(item)
        rules_checked, file_errors = check_network_policy_file(path, resolved_root)
        total_rules += rules_checked
        all_errors.extend(file_errors)
        if verbose:
            rel = str(path.relative_to(resolved_root)) if path.is_relative_to(resolved_root) else str(path)
            if file_errors:
                print(f"  FAIL: {rel} ({len(file_errors)} errors)")
            else:
                print(f"  OK:   {rel} ({rules_checked} DNS rules verified)")

    return total_rules, all_errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify DNS egress rule parity across static NetworkPolicy copies."
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print verbose status for each verified file.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Optional specific file paths to check (defaults to STATIC_NETWORK_POLICIES).",
    )
    args = parser.parse_args(argv)

    files = args.files if args.files else None
    display_count = len(files) if files else len(STATIC_NETWORK_POLICIES)
    if args.verbose:
        print(f"Checking {display_count} static NetworkPolicy copies for DNS egress parity...")

    rules_checked, errors = check_all(files=files, root=REPO_ROOT, verbose=args.verbose)

    if errors:
        print("ERROR: Static NetworkPolicy DNS egress parity check failed:", file=sys.stderr)
        for err in errors:
            print(f"  * {err}", file=sys.stderr)
        print(
            f"\n{len(errors)} error(s) found across {display_count} static policy copies.",
            file=sys.stderr,
        )
        return 1

    if not args.verbose:
        print(f"OK: Verified DNS egress rule parity across {display_count} static copies ({rules_checked} rules checked).")
    else:
        print(f"\nAll {display_count} static policy copies passed parity check ({rules_checked} rules).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tests for scripts/check_iac_parity.py."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_iac_parity import (
    EXCLUDED_NETPOL_MANIFESTS,
    HOUSE_SHAPE_EXCEPT,
    LINK_LOCAL_CIDR,
    REPO_ROOT,
    REQUIRED_DNS_LITERAL,
    REQUIRED_DNS_PROTOCOLS,
    REQUIRED_EXCEPT_MINIMUM,
    REQUIRED_LINK_LOCAL_CLOUDDNS,
    REQUIRED_LINK_LOCAL_DNSCACHE,
    REQUIRED_LINK_LOCAL_LITERALS,
    REQUIRED_WILDCARD_CIDR,
    STATIC_NETWORK_POLICIES,
    check_all,
    check_dns_egress_rule,
    check_network_policy_file,
    discover_dns_network_policies,
    governs_egress,
    is_house_shape,
    load_network_policies,
    main,
    sanitize_helm_template,
    validate_exclusions,
)


class CheckIacParityProductionTest(unittest.TestCase):
    """Verify that all live production static copies satisfy the DNS peer shape."""

    def test_all_production_copies_pass(self):
        rules_checked, errors = check_all()
        self.assertEqual(
            errors,
            [],
            f"Expected all production static policies to pass parity check, but found errors: {errors}",
        )
        self.assertGreaterEqual(
            rules_checked,
            len(STATIC_NETWORK_POLICIES),
            f"Expected at least {len(STATIC_NETWORK_POLICIES)} DNS rules checked, got {rules_checked}",
        )

    def test_core_egress_satisfies_house_shape(self):
        """Verify that deploy/kustomize/platform/networkpolicy-core-egress.yaml satisfies the 5-entry house shape (#747 B5)."""
        manifest_path = REPO_ROOT / "deploy/kustomize/platform/networkpolicy-core-egress.yaml"
        policies = load_network_policies(manifest_path)
        self.assertTrue(len(policies) > 0)
        found_house_shape = False
        for pol in policies:
            for rule in (pol.get("spec") or {}).get("egress") or []:
                for peer in rule.get("to") or []:
                    ip_block = peer.get("ipBlock") if isinstance(peer, dict) else None
                    if isinstance(ip_block, dict) and ip_block.get("cidr") == REQUIRED_WILDCARD_CIDR:
                        except_list = ip_block.get("except") or []
                        if is_house_shape(except_list):
                            found_house_shape = True
        self.assertTrue(
            found_house_shape,
            "deploy/kustomize/platform/networkpolicy-core-egress.yaml must satisfy the 5-entry house shape",
        )

    def test_discovery_matches_roster_exactly(self):
        """Ensure no DNS NetworkPolicy in the repository escapes the static roster.

        Mirroring scripts/test_test_discovery.py: any static manifest in the tree
        containing a port-53 egress rule must either be in STATIC_NETWORK_POLICIES
        or explicitly listed in EXCLUDED_NETPOL_MANIFESTS with a reviewed reason.
        """
        exclusion_errors = validate_exclusions(root=REPO_ROOT)
        self.assertEqual(
            exclusion_errors,
            [],
            f"EXCLUDED_NETPOL_MANIFESTS failed contract validation: {exclusion_errors}",
        )

        discovered = discover_dns_network_policies()
        roster = set(STATIC_NETWORK_POLICIES)

        untracked = discovered - roster
        self.assertEqual(
            untracked,
            set(),
            f"Found DNS-bearing NetworkPolicy files not tracked in STATIC_NETWORK_POLICIES: {sorted(untracked)}. "
            "Either add them to STATIC_NETWORK_POLICIES or to EXCLUDED_NETPOL_MANIFESTS with an explicit reason.",
        )

        stale = roster - discovered
        self.assertEqual(
            stale,
            set(),
            f"STATIC_NETWORK_POLICIES contains files that no longer contain DNS egress rules: {sorted(stale)}",
        )

    def test_excluded_manifests_contract(self):
        """Verify that EXCLUDED_NETPOL_MANIFESTS entries have non-empty reasons, point to existing files, and are disjoint from STATIC_NETWORK_POLICIES."""
        errors = validate_exclusions(root=REPO_ROOT)
        self.assertEqual(
            errors,
            [],
            f"EXCLUDED_NETPOL_MANIFESTS failed contract validation: {errors}",
        )


class CheckIacParitySyntheticTest(unittest.TestCase):
    """Verify that regressions in peer shape are caught as expected."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_manifest(self, filename: str, content: str) -> Path:
        p = self.root / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_discovery_catches_untracked_dns_policy(self):
        """Verify that discover_dns_network_policies flags newly introduced manifests."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: new-service-egress
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
"""
        self._write_manifest("examples/new-service/networkpolicy.yaml", manifest)
        discovered = discover_dns_network_policies(root=self.root)
        self.assertIn("examples/new-service/networkpolicy.yaml", discovered)

    def test_discovery_supports_all_target_file_extensions(self):
        """Verify that discover_dns_network_policies recognizes all target file extensions."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: dns-policy
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
"""
        extensions = [
            ".yaml",
            ".yml",
            ".yaml.template",
            ".yml.template",
            ".yaml.tmpl",
            ".yml.tmpl",
        ]
        for ext in extensions:
            rel_name = f"sub/policy_{ext.replace('.', '_')}{ext}"
            self._write_manifest(rel_name, manifest)

        discovered = discover_dns_network_policies(root=self.root)
        for ext in extensions:
            rel_name = f"sub/policy_{ext.replace('.', '_')}{ext}"
            self.assertIn(rel_name, discovered, f"Expected {ext} file to be discovered")

    def test_ignored_dirs_in_ancestor_path_does_not_break_discovery(self):
        """Verify that an ancestor path containing an ignored dirname (e.g. .claude/worktrees) does not suppress discovery."""
        worktree_root = self.root / ".claude" / "worktree"
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
"""
        manifest_path = worktree_root / "manifest.yaml"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest, encoding="utf-8")

        # An ignored directory within the root should still be skipped
        ignored_path = worktree_root / ".venv" / "manifest.yaml"
        ignored_path.parent.mkdir(parents=True, exist_ok=True)
        ignored_path.write_text(manifest, encoding="utf-8")

        discovered = discover_dns_network_policies(root=worktree_root)
        self.assertIn("manifest.yaml", discovered)
        self.assertNotIn(".venv/manifest.yaml", discovered)

    def test_ignored_prefixes_docs_site(self):
        """Verify that manifests under docs/site are ignored by prefix."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: doc-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
"""
        self._write_manifest("docs/site/manifest.yaml", manifest)
        discovered = discover_dns_network_policies(root=self.root)
        self.assertNotIn("docs/site/manifest.yaml", discovered)

    def test_discovery_ignores_testdata_at_any_depth(self):
        """Verify that any directory named testdata is ignored repository-wide at any depth."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: testdata-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
"""
        self._write_manifest("k8s-operator/internal/testing/testdata/netpol.yaml", manifest)
        self._write_manifest("deep/nested/path/testdata/manifest.yaml", manifest)
        discovered = discover_dns_network_policies(root=self.root)
        self.assertNotIn("k8s-operator/internal/testing/testdata/netpol.yaml", discovered)
        self.assertNotIn("deep/nested/path/testdata/manifest.yaml", discovered)

    def test_discovery_ignores_unmatched_extensions(self):
        """Verify that files with extensions outside DISCOVERY_FILE_PATTERNS (e.g. .tpl, .md) are ignored."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: helper-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
"""
        self._write_manifest("charts/kube-agents/templates/_helpers.tpl", manifest)
        self._write_manifest("agents/platform/skills/gke-multitenancy/SKILL.md", manifest)
        discovered = discover_dns_network_policies(root=self.root)
        self.assertNotIn("charts/kube-agents/templates/_helpers.tpl", discovered)
        self.assertNotIn("agents/platform/skills/gke-multitenancy/SKILL.md", discovered)

    def test_discovery_raises_on_malformed_network_policy(self):
        """Verify that malformed YAML containing NetworkPolicy and 53 raises rather than being silently swallowed."""
        bad_yaml = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
spec: [unclosed json syntax with 53
"""
        self._write_manifest("invalid.yaml", bad_yaml)
        with self.assertRaises(ValueError):
            discover_dns_network_policies(root=self.root)

    def test_validate_exclusions_contract(self):
        """Verify that validate_exclusions flags empty reasons, missing files, and roster collisions."""
        self._write_manifest("valid.yaml", "dummy")
        # 1. Valid exclusion passes
        errors = validate_exclusions(
            exclusions={"valid.yaml": "reviewed test reason"},
            roster=set(),
            root=self.root,
        )
        self.assertEqual(errors, [])

        # 2. Empty or whitespace reason fails
        errors = validate_exclusions(
            exclusions={"valid.yaml": "   "},
            roster=set(),
            root=self.root,
        )
        self.assertTrue(any("non-empty reviewed reason" in err for err in errors))

        # 3. Missing file fails
        errors = validate_exclusions(
            exclusions={"nonexistent.yaml": "valid reason"},
            roster=set(),
            root=self.root,
        )
        self.assertTrue(any("does not exist on disk" in err for err in errors))

        # 4. Roster collision fails
        errors = validate_exclusions(
            exclusions={"valid.yaml": "valid reason"},
            roster={"valid.yaml"},
            root=self.root,
        )
        self.assertTrue(any("cannot also be in STATIC_NETWORK_POLICIES" in err for err in errors))

        # 5. Absolute path or path with leading slash fails
        errors = validate_exclusions(
            exclusions={"/valid.yaml": "valid reason"},
            roster=set(),
            root=self.root,
        )
        self.assertTrue(
            any("must be repository-relative without a leading slash" in err for err in errors)
        )

    def test_discovery_respects_excluded_netpol_manifests(self):
        """Verify that discover_dns_network_policies ignores entries listed in EXCLUDED_NETPOL_MANIFESTS."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
"""
        self._write_manifest("excluded.yaml", manifest)
        with unittest.mock.patch.dict(
            "scripts.check_iac_parity.EXCLUDED_NETPOL_MANIFESTS",
            {"excluded.yaml": "reviewed test reason"},
        ):
            discovered = discover_dns_network_policies(root=self.root)
            self.assertNotIn("excluded.yaml", discovered)

    def test_discovery_raises_on_unreadable_file(self):
        """Verify that read errors during discovery raise RuntimeError."""
        self._write_manifest("unreadable.yaml", "dummy")
        with unittest.mock.patch.object(
            Path, "read_text", side_effect=OSError("simulated permission error")
        ):
            with self.assertRaises(RuntimeError):
                discover_dns_network_policies(root=self.root)

    def test_missing_dns_literal_fails(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
      to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any(REQUIRED_DNS_LITERAL in err for err in errors))

    def test_missing_wildcard_cidr_fails(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any(REQUIRED_WILDCARD_CIDR in err for err in errors))

    def test_wildcard_missing_except_list_fails(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("missing required private CIDRs" in err for err in errors))

    def test_incomplete_except_list_fails(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("10.0.0.0/8" in err for err in errors))

    def test_house_shape_except_passes(self):
        manifest = f"""apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 169.254.20.10/32
        - ipBlock:
            cidr: 169.254.169.254/32
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
{chr(10).join(f"              - {cidr}" for cidr in sorted(HOUSE_SHAPE_EXCEPT))}
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])
        self.assertTrue(is_house_shape(HOUSE_SHAPE_EXCEPT))

    def test_string_port_representation_supported(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: "53"
          protocol: UDP
        - port: "53"
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_missing_udp_protocol_fails(self):
        """Verify that a port-53 egress rule missing protocol UDP fails."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("missing required protocol(s): ['UDP']" in err for err in errors))

    def test_missing_tcp_protocol_fails(self):
        """Verify that a port-53 egress rule missing protocol TCP fails."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("missing required protocol(s): ['TCP']" in err for err in errors))

    def test_house_shape_missing_cloud_dns_literal_fails(self):
        """Verify that house shape (excepting 169.254.0.0/16) missing Cloud DNS literal fails (#1169)."""
        manifest = f"""apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: {REQUIRED_LINK_LOCAL_DNSCACHE}
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
{chr(10).join(f"              - {cidr}" for cidr in sorted(HOUSE_SHAPE_EXCEPT))}
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(
            any(
                "requiring explicit ipBlock literal(s) for NodeLocal DNSCache and Cloud DNS for GKE" in err
                and REQUIRED_LINK_LOCAL_CLOUDDNS in err
                for err in errors
            )
        )

    def test_house_shape_missing_node_local_dns_literal_fails(self):
        """Verify that house shape (excepting 169.254.0.0/16) missing NodeLocal DNSCache literal fails."""
        manifest = f"""apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: {REQUIRED_LINK_LOCAL_CLOUDDNS}
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
{chr(10).join(f"              - {cidr}" for cidr in sorted(HOUSE_SHAPE_EXCEPT))}
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(
            any(
                "requiring explicit ipBlock literal(s) for NodeLocal DNSCache and Cloud DNS for GKE" in err
                and REQUIRED_LINK_LOCAL_DNSCACHE in err
                for err in errors
            )
        )

    def test_rfc1918_without_link_local_literals_passes(self):
        """Verify that policies with RFC1918-only except list do not require explicit link-local literals."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_multi_doc_ingress_only_policy_skipped(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ingress-only-db
spec:
  policyTypes:
    - Ingress
  ingress:
    - ports:
        - port: 5432
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: app-egress
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("multi.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_multi_doc_unrelated_syntax_error_ignored(self):
        manifest = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: broken-unrelated
spec: [unclosed json syntax
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: valid-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("resilient.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_missing_dns_rule_fails(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 443
          protocol: TCP
      to:
        - ipBlock:
            cidr: 0.0.0.0/0
"""
        p = self._write_manifest("netpol.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertTrue(any("has no egress rule for port 53" in err for err in errors))

    def test_non_existent_file_fails(self):
        p = self.root / "does_not_exist.yaml"
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertTrue(any("failed to load NetworkPolicy" in err for err in errors))

    def test_no_network_policies_in_file_fails(self):
        manifest = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: non-policy
spec:
  replicas: 1
"""
        p = self._write_manifest("deployment_only.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertEqual(errors, ["deployment_only.yaml: no NetworkPolicy resources found"])

    def test_ingress_only_policy_fails_egress_check(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ingress-only
spec:
  policyTypes:
    - Ingress
  ingress:
    - from: []
"""
        p = self._write_manifest("ingress_only.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertEqual(errors, ["ingress_only.yaml: no Egress NetworkPolicy resources found"])

    def test_helm_template_sanitization(self):
        raw = """{{- /* Multi-line comment
that should be
stripped */ -}}
{{- if .Values.enabled }}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
  namespace: {{ .Release.Namespace }}
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
{{- end }}"""
        p = self._write_manifest("template.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_split_wildcard_peers_fails(self):
        """Verify that splitting required private CIDRs across multiple 0.0.0.0/0 peers is rejected."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("split_wildcard.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("missing required private CIDRs" in err for err in errors))
        self.assertTrue(any("expected at most one '0.0.0.0/0' peer" in err for err in errors))

    def test_multiple_wildcard_peers_rejected(self):
        """Verify that multiple 0.0.0.0/0 peers in a single rule are rejected."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("dup_wildcard.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("expected at most one '0.0.0.0/0' peer" in err for err in errors))

    def test_helm_template_trailing_comment(self):
        """Verify that template directives with trailing comments parse cleanly."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    {{- if .Values.enabled }} # conditionally included
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
    {{- end }} # end of condition
"""
        p = self._write_manifest("template_comment.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_helm_template_inline_conditional(self):
        """Verify that inline conditionals preserve the manifest payload."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        {{- if .Values.includeClassic }}- ipBlock: { cidr: 10.96.0.10/32 }{{- end }}
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("template_inline.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_ingress_only_with_empty_egress_skipped(self):
        """Verify that Ingress-only policies with explicit egress: [] are skipped."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ingress-only
spec:
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector: {}
  egress: []
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: valid-egress
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("ingress_empty_egress.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_ingress_only_with_non_empty_egress_skipped(self):
        """Verify that Ingress-only policies with a non-empty egress list are skipped."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ingress-only
spec:
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector: {}
  egress:
    - ports:
        - port: 80
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: valid-egress
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("ingress_non_empty_egress.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_explicit_egress_policy_type_missing_dns_fails(self):
        """Verify that explicit policyTypes: [Egress] with no port-53 rule fails."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 443
"""
        p = self._write_manifest("egress_no_dns.yaml", manifest)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertTrue(any("has no egress rule for port 53" in err for err in errors))

    def test_governs_egress_contract(self):
        """Verify governs_egress correctly identifies when a NetworkPolicy governs egress."""
        # Explicit Ingress-only policies do not govern egress
        self.assertFalse(governs_egress({"policyTypes": ["Ingress"]}))
        self.assertFalse(governs_egress({"policyTypes": ["Ingress"], "egress": []}))
        self.assertFalse(
            governs_egress({"policyTypes": ["Ingress"], "egress": [{"ports": [{"port": 53}]}]})
        )

        # Explicit Egress policies govern egress
        self.assertTrue(governs_egress({"policyTypes": ["Egress"]}))
        self.assertTrue(governs_egress({"policyTypes": ["Egress"], "egress": []}))
        self.assertTrue(governs_egress({"policyTypes": ["Ingress", "Egress"]}))

        # When policyTypes is omitted, Kubernetes enables Egress only if egress is present
        self.assertTrue(governs_egress({"egress": []}))
        self.assertTrue(governs_egress({"egress": [{"ports": [{"port": 53}]}]}))
        self.assertFalse(governs_egress({"ingress": []}))
        self.assertFalse(governs_egress({}))

        # Non-dict or malformed specs do not govern egress
        self.assertFalse(governs_egress(None))
        self.assertFalse(governs_egress("scalar"))
        self.assertFalse(governs_egress(True))
        self.assertFalse(governs_egress({"policyTypes": "not-a-list"}))

    def test_discovery_skips_ingress_only_with_port_53_egress(self):
        """Verify discover_dns_network_policies ignores policies whose policyTypes explicitly excludes Egress."""
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ingress-only
spec:
  policyTypes:
    - Ingress
  ingress:
    - from: []
  egress:
    - ports:
        - port: 53
"""
        self._write_manifest("ingress_with_dns.yaml", manifest)
        discovered = discover_dns_network_policies(root=self.root)
        self.assertNotIn("ingress_with_dns.yaml", discovered)

    def test_discovery_skips_malformed_egress_and_ports(self):
        """Verify discovery safely ignores scalar egress, scalar rule items, and non-list ports."""
        self._write_manifest("scalar_egress.yaml", """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: p1
spec:
  # mentions 53 in comment
  egress: invalid_scalar
""")
        self._write_manifest("scalar_rule.yaml", """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: p2
spec:
  egress:
    - not_a_dict_53
""")
        self._write_manifest("scalar_ports.yaml", """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: p3
spec:
  egress:
    - ports: 53
""")
        discovered = discover_dns_network_policies(root=self.root)
        self.assertNotIn("scalar_egress.yaml", discovered)
        self.assertNotIn("scalar_rule.yaml", discovered)
        self.assertNotIn("scalar_ports.yaml", discovered)

    def test_helm_template_multiline_if_and(self):
        """Verify that multiline {{- if and ... }} directives parse cleanly."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  {{- if and
      .Values.enabled
      .Values.networkPolicy.enabled }} # conditionally included
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
  {{- end }}
"""
        p = self._write_manifest("multiline_if_and.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_helm_template_crlf_multiline_directive(self):
        """Verify that multiline Go template directives with CRLF line endings parse cleanly."""
        raw = "apiVersion: networking.k8s.io/v1\r\nkind: NetworkPolicy\r\nmetadata:\r\n  name: test-netpol\r\nspec:\r\n  {{- if and\r\n      .Values.enabled\r\n      .Values.networkPolicy.enabled }}\r\n  egress:\r\n    - ports:\r\n        - port: 53\r\n          protocol: UDP\r\n        - port: 53\r\n          protocol: TCP\r\n      to:\r\n        - ipBlock:\r\n            cidr: 10.96.0.10/32\r\n        - ipBlock:\r\n            cidr: 0.0.0.0/0\r\n            except:\r\n              - 10.0.0.0/8\r\n              - 172.16.0.0/12\r\n              - 192.168.0.0/16\r\n  {{- end }}\r\n"
        p = self._write_manifest("crlf_multiline.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_helm_template_multiline_with(self):
        """Verify that multiline {{- with ... }} directives parse cleanly."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  {{- with
      .Values.spec }}
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
  {{- end }}
"""
        p = self._write_manifest("multiline_with.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_helm_template_multiline_range(self):
        """Verify that multiline {{- range ... }} directives parse cleanly."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  {{- range
      .Values.configs }}
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
  {{- end }}
"""
        p = self._write_manifest("multiline_range.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_helm_template_multiline_value_interpolation(self):
        """Verify that multiline value interpolations parse cleanly."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ printf "%s-%s"
      .Release.Name
      .Values.component }}
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("multiline_interp.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_helm_template_multiline_comment(self):
        """Verify that multiline Go template comments parse cleanly."""
        raw = """{{- /*
Multi-line
template comment
*/ -}}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("multiline_comment.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_helm_template_multiline_directive_with_inline_yaml(self):
        """Verify that multiline directives with inline YAML content on the same line parse cleanly."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
      to:
        {{- if or
            .Values.includeClassic
            .Values.legacy }}- ipBlock: { cidr: 10.96.0.10/32 }{{- end }}
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("multiline_inline_yaml.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertEqual(errors, [])

    def test_helm_template_if_else_branches_treated_as_additive(self):
        """Verify that mutually exclusive if/else branches in static source are treated as additive peers per documented limitation."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        {{- if .Values.useHouseShape }}
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
              - 100.64.0.0/10
              - 169.254.0.0/16
        {{- else }}
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
        {{- end }}
"""
        p = self._write_manifest("if_else_limitation.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("expected at most one '0.0.0.0/0' peer" in err for err in errors))

    def test_malformed_spec_scalar_fails_cleanly(self):
        """Verify that a scalar spec field (e.g. spec: true) reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec: true
"""
        p = self._write_manifest("scalar_spec.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertTrue(any("spec must be a mapping" in err for err in errors))

    def test_malformed_metadata_scalar_fails_cleanly(self):
        """Verify that a scalar metadata field reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: "text"
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("scalar_metadata.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("metadata must be a mapping" in err for err in errors))

    def test_malformed_policy_types_scalar_fails_cleanly(self):
        """Verify that a scalar policyTypes field reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes: 123
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("scalar_policy_types.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertTrue(any("policyTypes must be a list" in err for err in errors))

    def test_malformed_egress_scalar_fails_cleanly(self):
        """Verify that a scalar egress field reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes:
    - Egress
  egress: "text"
"""
        p = self._write_manifest("scalar_egress.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertTrue(any("egress must be a list" in err for err in errors))

    def test_malformed_egress_rule_scalar_fails_cleanly(self):
        """Verify that a non-mapping egress rule item reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes:
    - Egress
  egress:
    - true
"""
        p = self._write_manifest("scalar_egress_rule.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertTrue(any("egress rule must be a mapping" in err for err in errors))

    def test_malformed_ports_scalar_fails_cleanly(self):
        """Verify that a scalar ports field reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes:
    - Egress
  egress:
    - ports: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
"""
        p = self._write_manifest("scalar_ports.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertTrue(any("rule ports must be a list" in err for err in errors))

    def test_malformed_ports_items_scalar_fails_cleanly(self):
        """Verify that scalar ports items (e.g. ports: ["53"]) report missing DNS rule cleanly."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - "53"
"""
        p = self._write_manifest("scalar_port_items.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 0)
        self.assertTrue(any("has no egress rule for port 53" in err for err in errors))

    def test_malformed_to_peers_scalar_fails_cleanly(self):
        """Verify that a scalar 'to' field reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 53
      to: "all"
"""
        p = self._write_manifest("scalar_to.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("'to' field must be a list" in err for err in errors))

    def test_malformed_ipblock_scalar_fails_cleanly(self):
        """Verify that a scalar ipBlock field reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock: true
"""
        p = self._write_manifest("scalar_ipblock.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("ipBlock must be a mapping" in err for err in errors))

    def test_malformed_except_scalar_fails_cleanly(self):
        """Verify that a scalar except field reports a clean error without traceback."""
        raw = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  policyTypes:
    - Egress
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 10.96.0.10/32
        - ipBlock:
            cidr: 0.0.0.0/0
            except: "10.0.0.0/8"
"""
        p = self._write_manifest("scalar_except.yaml", raw)
        rules, errors = check_network_policy_file(p, self.root)
        self.assertEqual(rules, 1)
        self.assertTrue(any("'0.0.0.0/0' peer except must be a list" in err for err in errors))

    def test_main_success_default(self):
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = main([])
        self.assertEqual(rc, 0)
        self.assertIn("OK: Verified DNS egress", buf_out.getvalue())

    def test_main_verbose(self):
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = main(["-v"])
        self.assertEqual(rc, 0)
        self.assertIn("All 8 static policy copies passed", buf_out.getvalue())

    def test_main_failure(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: test-netpol
spec:
  egress:
    - ports:
        - port: 53
      to:
        - ipBlock:
            cidr: 0.0.0.0/0
"""
        p = self._write_manifest("failing.yaml", manifest)
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = main([str(p)])
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: Static NetworkPolicy DNS egress parity check failed", buf_err.getvalue())

    def test_check_all_verbose_file_failure(self):
        p = self._write_manifest("verbose_bad.yaml", """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: bad
spec:
  egress: []
""")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rules, errors = check_all([p], root=self.root, verbose=True)
        self.assertTrue(errors)
        self.assertIn("FAIL: verbose_bad.yaml", buf.getvalue())

    @unittest.mock.patch.dict("scripts.check_iac_parity.EXCLUDED_NETPOL_MANIFESTS", {"nonexistent.yaml": "test"})
    def test_check_all_verbose_exclusion_failure(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rules, errors = check_all([], root=self.root, verbose=True)
        self.assertTrue(errors)
        self.assertIn("FAIL: exclusion contract:", buf.getvalue())

    def test_main_verbose_failure(self):
        p = self._write_manifest("main_verbose_bad.yaml", """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: bad
spec:
  egress: []
""")
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = main(["-v", str(p)])
        self.assertEqual(rc, 1)
        self.assertIn(f"FAIL: {p}", buf_out.getvalue())
        self.assertIn("ERROR: Static NetworkPolicy DNS egress parity check failed", buf_err.getvalue())


if __name__ == "__main__":
    unittest.main()

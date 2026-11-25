"""Comprehensive unit and adversarial tests for types, parser, git_loader, __init__, and __main__."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import risk_classifier
from risk_classifier import (
    ACTION_ADD,
    BLAST_CRITICAL,
    BLAST_HIGH,
    BLAST_LEVELS,
    BLAST_LOW,
    BLAST_MEDIUM,
    BLAST_UNKNOWN,
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    CHANGE_RENAMED,
    CONCRETE_RISK_ORDER,
    DIM_INPUT_INTEGRITY,
    DIM_SECURITY,
    DISRUPT_DESTRUCTIVE,
    DISRUPT_DISRUPTIVE,
    DISRUPTIVENESS_LEVELS,
    DISRUPT_NONE,
    DISRUPT_UNKNOWN,
    FieldDiff,
    FileChange,
    FileVerdict,
    ManifestParseError,
    PRStats,
    Reason,
    ResourceRef,
    ResourceVerdict,
    REVERT_DIFFICULT,
    REVERT_EASY,
    REVERT_IRREVERSIBLE,
    REVERT_MODERATE,
    REVERT_UNKNOWN,
    REVERTABILITY_LEVELS,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LABEL_COLORS,
    RISK_LABEL_DESCRIPTIONS,
    RISK_LEVELS,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    ClassificationResult,
    classify_changes,
    classify_git_range,
    combine_blast_radius,
    combine_disruptiveness,
    combine_revertability,
    combine_risk,
    diff_objects,
    escalate_risk,
    has_template_syntax,
    load_git_changes,
    load_path_pair,
    parse_manifest_bytes,
    risk_label_for,
    unknown_result,
)
from risk_classifier.__main__ import main as cli_main
from risk_classifier.parser import MAX_DOCS_PER_FILE, MAX_FILE_BYTES, MAX_NESTING_DEPTH, MAX_TOTAL_BYTES, _strict_equal

CLUSTER_ADMIN_CRB = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: pwn-admin
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
  - kind: ServiceAccount
    name: default
    namespace: shop
"""

SAFE_CM = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-cfg
  namespace: shop
data:
  LOG_LEVEL: info
"""


class TypesAndLatticeAdversarialTests(unittest.TestCase):
    """Exhaustive algebraic, normalization, immutability, and invariant tests for types.py."""

    def test_combine_lattices_algebraic_laws_and_garbage_normalization(self) -> None:
        lattices = [
            (combine_risk, RISK_LEVELS, RISK_UNKNOWN),
            (combine_blast_radius, BLAST_LEVELS, BLAST_UNKNOWN),
            (combine_revertability, REVERTABILITY_LEVELS, REVERT_UNKNOWN),
            (combine_disruptiveness, DISRUPTIVENESS_LEVELS, DISRUPT_UNKNOWN),
        ]
        for fn, levels, unk in lattices:
            for a in levels:
                self.assertEqual(fn(a, a), a)
                for b in levels:
                    self.assertEqual(fn(a, b), fn(b, a), f"{fn.__name__}({a}, {b}) not commutative")
                    self.assertIn(fn(a, b), levels)
                    for c in levels:
                        self.assertEqual(fn(fn(a, b), c), fn(a, fn(b, c)))
            # Unrecognized strings and unhashable objects normalize to unknown (never leak raw input)
            for bad in ("garbage", "", "LOW", None, 123, ["unhashable"], {"k": "v"}):
                for valid in levels:
                    res1 = fn(bad, valid)  # type: ignore[arg-type]
                    res2 = fn(valid, bad)  # type: ignore[arg-type]
                    self.assertEqual(res1, res2)
                    self.assertEqual(res1, fn(unk, valid))
                    self.assertIn(res1, levels)

    def test_escalate_risk_and_risk_label_for_fail_closed(self) -> None:
        self.assertEqual(escalate_risk(RISK_LOW), RISK_MEDIUM)
        self.assertEqual(escalate_risk(RISK_MEDIUM), RISK_HIGH)
        self.assertEqual(escalate_risk(RISK_HIGH, high_impact=False), RISK_HIGH)
        self.assertEqual(escalate_risk(RISK_HIGH, high_impact=True), RISK_CRITICAL)
        self.assertEqual(escalate_risk(RISK_CRITICAL, high_impact=True), RISK_CRITICAL)
        self.assertEqual(escalate_risk(RISK_UNKNOWN, high_impact=True), RISK_UNKNOWN)
        for bad in ("bogus", "", None, ["bad"]):
            self.assertEqual(escalate_risk(bad, high_impact=True), RISK_UNKNOWN)  # type: ignore[arg-type]
            self.assertEqual(risk_label_for(bad), "risk:unknown")  # type: ignore[arg-type]

    def test_constant_mappings_are_immutable(self) -> None:
        for mapping in (CONCRETE_RISK_ORDER, RISK_LABEL_COLORS, RISK_LABEL_DESCRIPTIONS):
            with self.assertRaises(TypeError):
                mapping["injected"] = "x"  # type: ignore[index]

    def test_dataclass_validation_and_deep_tuple_coercion(self) -> None:
        reason = Reason(code="TEST", dimension=DIM_SECURITY, severity=RISK_HIGH, message="ok")
        cr = ClassificationResult(
            risk_level=RISK_HIGH,
            blast_radius=BLAST_HIGH,
            revertability=REVERT_EASY,
            disruptiveness=DISRUPT_NONE,
            supported=True,
            effective_changes=True,
            reasons=[reason],  # type: ignore[arg-type]
            files=[],  # type: ignore[arg-type]
            resources=[],  # type: ignore[arg-type]
        )
        self.assertIsInstance(cr.reasons, tuple)
        self.assertIsInstance(cr.files, tuple)
        self.assertIsInstance(cr.resources, tuple)

        # `risk_level="unknown"` cannot claim `supported=True`
        with self.assertRaises(ValueError):
            ClassificationResult(
                risk_level=RISK_UNKNOWN,
                blast_radius=BLAST_UNKNOWN,
                revertability=REVERT_UNKNOWN,
                disruptiveness=DISRUPT_UNKNOWN,
                supported=True,
                effective_changes=False,
            )
        with self.assertRaises(ValueError):
            FileVerdict(
                path="a.yaml",
                change_type=CHANGE_ADDED,
                category="manifest",
                risk_level=RISK_UNKNOWN,
                blast_radius=BLAST_UNKNOWN,
                revertability=REVERT_UNKNOWN,
                disruptiveness=DISRUPT_UNKNOWN,
                supported=True,
            )
        ref = ResourceRef(group="", version="v1", kind="ConfigMap", namespace="s", name="c")
        with self.assertRaises(ValueError):
            ResourceVerdict(
                file="a.yaml",
                action=ACTION_ADD,
                ref=ref,
                risk_level=RISK_UNKNOWN,
                blast_radius=BLAST_UNKNOWN,
                revertability=REVERT_UNKNOWN,
                disruptiveness=DISRUPT_UNKNOWN,
                supported=True,
            )

        # PRStats rejects negative or boolean counts
        with self.assertRaises(ValueError):
            PRStats(files_changed=-1)
        with self.assertRaises(ValueError):
            PRStats(files_changed=True)  # type: ignore[arg-type]

        # FileChange rejects invalid change_type or string bytes
        with self.assertRaises(ValueError):
            FileChange(path="a.yaml", change_type="copied")
        with self.assertRaises(TypeError):
            FileChange(path="a.yaml", after="not-bytes")  # type: ignore[arg-type]

        # unknown_result keeps files_changed >= unsupported_files when file is set
        unk = unknown_result("ERR", "failed", file="clusters/dev/x.yaml")
        self.assertEqual(unk.stats.files_changed, 1)
        self.assertEqual(unk.stats.unsupported_files, 1)

    def test_package_exports_sorted_and_complete(self) -> None:
        exported = risk_classifier.__all__
        self.assertEqual(exported, sorted(exported))
        self.assertEqual(len(exported), len(set(exported)))
        for sym in exported:
            self.assertTrue(hasattr(risk_classifier, sym), f"missing export {sym}")


class ParserStrictYAMLAndJSONAdversarialTests(unittest.TestCase):
    """Adversarial YAML 1.2, JSON, template, escape, multi-doc, Secret, and diff_objects tests."""

    def test_template_multiline_hash_unicode_escapes_helm_functions_and_flux_double_dollar(self) -> None:
        # Smuggling `${VAR}` via multiline quoted `#` or flow `#` or `\u0024` escape -> MUST be unknown template
        smuggled_templates = [
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  k: "prefix\n  # ${SECRET_ENV_SUBST}"\n',
            'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: "a\n  # ", namespace: "${PROD_NS}"}\n',
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: "\\u0024{PROD_NS}"\n',
            '{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"c","namespace":"s"},"data":{"k":"\\u0024{CLUSTER_SECRET}"}}',
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  k: "{{ quote .Values.secret }}"\n',
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  k: "{{ default \\"x\\" .Values.y }}"\n',
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  k: "{{ toYaml .Values.sec }}"\n',
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  k: "{{ lookup \\"v1\\" \\"Secret\\" \\"ns\\" \\"s\\" }}"\n',
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  k: "{{ (.Values.tag) }}"\n',
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  k: "$$${ODD_DOLLAR_SUBST}"\n',
        ]
        for idx, doc in enumerate(smuggled_templates):
            with self.subTest(case=idx):
                ext = ".json" if doc.startswith("{") else ".yaml"
                res = classify_changes(
                    [FileChange(path=f"clusters/dev/cm{ext}", change_type=CHANGE_ADDED, after=doc.encode("utf-8"), after_mode="100644")]
                )
                self.assertEqual(res.risk_level, RISK_UNKNOWN)
                self.assertFalse(res.supported)
                self.assertIn("UNRENDERED_TEMPLATE", {r.code for r in res.reasons})

        # Flux-escaped `$${HOSTNAME}` and `$$$${LITERAL}` are literal strings, NOT templates!
        escaped_flux = (
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: shop\n'
            'data:\n  SCRIPT: "echo $${HOSTNAME} $$$${LITERAL}"\n'
        )
        res_ok = classify_changes(
            [FileChange(path="clusters/dev/cm.yaml", change_type=CHANGE_ADDED, after=escaped_flux.encode("utf-8"), after_mode="100644")]
        )
        self.assertEqual(res_ok.risk_level, RISK_LOW)
        self.assertTrue(res_ok.supported)

        # Linear-time check against pathological unclosed `{{` and `${A` sequences
        t0 = time.monotonic()
        self.assertFalse(has_template_syntax("{{ " * 60_000))
        self.assertFalse(has_template_syntax("${A" * 60_000))
        self.assertLess(time.monotonic() - t0, 0.25)

    def test_escaped_unicode_bidi_c1_surrogates_and_line_separators_rejected(self) -> None:
        bad_payloads = [
            (b'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: "a\\x00b"\n  namespace: s\n', False, "BINARY_OR_CONTROL_CHAR"),
            (b'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: "a\\u202eb"\n  namespace: s\n', False, "HOSTILE_UNICODE"),
            (b'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: "a\\u2028b"\n  namespace: s\n', False, "HOSTILE_UNICODE"),
            (b'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: "a\\u2029b"\n  namespace: s\n', False, "HOSTILE_UNICODE"),
            (b'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: "a\\u0085b"\n  namespace: s\n', False, "CONTROL_CHARACTER"),
            (b'{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a\\u202eb","namespace":"s"}}', True, "HOSTILE_UNICODE"),
            (b'{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a\\ud800b","namespace":"s"}}', True, "INVALID_UTF8"),
            (b'{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"a\\u001bb","namespace":"s"}}', True, "CONTROL_CHARACTER"),
        ]
        for raw, is_json, expected_code in bad_payloads:
            with self.subTest(raw=raw[:48]):
                out = parse_manifest_bytes(raw, is_json=is_json)
                self.assertEqual(out.error_code, expected_code)

        # Leading UTF-8 BOM is stripped cleanly, while mid-file BOM is rejected
        bom_ok = b"\xef\xbb\xbf" + SAFE_CM.encode("utf-8")
        self.assertEqual(parse_manifest_bytes(bom_ok).error_code, "")
        bom_mid = SAFE_CM.encode("utf-8") + "  X: \ufeffy\n".encode("utf-8")
        self.assertEqual(parse_manifest_bytes(bom_mid).error_code, "HOSTILE_UNICODE")

    def test_yaml12_strict_keys_directives_octals_huge_ints_and_nonfinite_floats(self) -> None:
        cases = [
            ("non-str int key", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  1: a\n  '1': b\n", "YAML_NON_STRING_KEY"),
            ("non-str bool key", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  true: a\n", "YAML_NON_STRING_KEY"),
            ("non-str null key", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  ~: a\n", "YAML_NON_STRING_KEY"),
            ("complex seq key", "? [a, b]\n: 1\n", "YAML_COMPLEX_KEY"),
            ("local !foo tag", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: !foo c\n  namespace: s\n", "YAML_UNSUPPORTED_TAG"),
            ("%YAML 1.1 directive", "%YAML 1.1\n---\n" + SAFE_CM, "YAML_UNSUPPORTED_DIRECTIVE"),
            ("%TAG directive", "%TAG ! !evil!\n---\n" + SAFE_CM, "YAML_UNSUPPORTED_DIRECTIVE"),
            ("ambiguous octal 010", "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: w\n  namespace: s\nspec:\n  replicas: 010\n", "YAML_AMBIGUOUS_OCTAL"),
            ("yaml11 !!bool yes", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\nimmutable: !!bool yes\n", "YAML_SYNTAX_ERROR"),
            ("huge hex int DoS", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\nimmutable: 0x" + ("f" * 5000) + "\n", "NUMERIC_VALUE_OUT_OF_RANGE"),
            ("yaml .nan", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\nx: .nan\n", "NON_FINITE_NUMBER"),
            ("yaml .inf", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\nx: .inf\n", "NON_FINITE_NUMBER"),
        ]
        for label, body, expected_code in cases:
            with self.subTest(label=label):
                out = parse_manifest_bytes(body.encode("utf-8"))
                self.assertEqual(out.error_code, expected_code)
                # Also verify classify_changes never crashes on any of them
                res = classify_changes([FileChange(path="clusters/dev/x.yaml", change_type=CHANGE_ADDED, after=body.encode("utf-8"), after_mode="100644")])
                self.assertEqual(res.risk_level, RISK_UNKNOWN)

        # Valid YAML 1.2 octal `0o10` -> 8 and hex `0x10` -> 16
        valid_nums = parse_manifest_bytes(
            b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\noct: 0o10\nhex: 0x10\nzero: 0\n"
        )
        self.assertEqual(valid_nums.error_code, "")
        self.assertEqual(valid_nums.resources[0].obj["oct"], 8)
        self.assertEqual(valid_nums.resources[0].obj["hex"], 16)
        self.assertEqual(valid_nums.resources[0].obj["zero"], 0)

        # JSON 1e1000 overflow and huge int >1024 bits rejected cleanly
        self.assertEqual(parse_manifest_bytes(b'{"x": 1e1000}', is_json=True).error_code, "JSON_INVALID_CONSTANT")
        huge_json = ('{"x": ' + ("9" * 5000) + "}").encode("utf-8")
        self.assertEqual(parse_manifest_bytes(huge_json, is_json=True).error_code, "NUMERIC_VALUE_OUT_OF_RANGE")

    def test_size_depth_node_and_doc_stream_bombs_bounded(self) -> None:
        # File size bomb
        self.assertEqual(parse_manifest_bytes(b"a" * (MAX_FILE_BYTES + 1)).error_code, "MANIFEST_TOO_LARGE")
        # Deep YAML & JSON nesting bombs
        deep_yaml = ("a:\n" + "\n".join("  " * i + "a:" for i in range(1, MAX_NESTING_DEPTH + 4)) + " 1\n").encode()
        self.assertEqual(parse_manifest_bytes(deep_yaml).error_code, "YAML_NESTING_TOO_DEEP")
        deep_json = ("[" * (MAX_NESTING_DEPTH + 4) + "1" + "]" * (MAX_NESTING_DEPTH + 4)).encode()
        self.assertEqual(parse_manifest_bytes(deep_json, is_json=True).error_code, "JSON_NESTING_TOO_DEEP")
        # Multi-doc stream bomb (> MAX_DOCS_PER_FILE)
        many_docs = ("---\na: 1\n" * (MAX_DOCS_PER_FILE + 5)).encode()
        self.assertEqual(parse_manifest_bytes(many_docs).error_code, "TOO_MANY_DOCUMENTS")

    def test_multidoc_and_list_anti_masking_never_hides_critical_resource(self) -> None:
        # Every way an attacker can pair a broken/kustomize/templated doc with a CRITICAL RBAC doc
        evasion_streams = [
            ("doc1 critical + doc2 anchor", CLUSTER_ADMIN_CRB + "---\na: &x\n  k: 1\nb: *x\n"),
            ("doc1 anchor + doc2 critical", "a: &x\n  k: 1\nb: *x\n---\n" + CLUSTER_ADMIN_CRB),
            ("doc1 critical + doc2 dup key", CLUSTER_ADMIN_CRB + "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\ndata:\n  k: 1\n  k: 2\n"),
            ("doc1 kustomize + doc2 critical", "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: [a.yaml]\n---\n" + CLUSTER_ADMIN_CRB),
            ("doc1 bad apiVersion + doc2 critical", "apiVersion: /bad\nkind: ConfigMap\nmetadata:\n  name: c\n---\n" + CLUSTER_ADMIN_CRB),
            ("doc1 unquoted helm + doc2 critical", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Values.x }}\n---\n" + CLUSTER_ADMIN_CRB),
            ("doc1 hostile bidi + doc2 critical", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: \"x\u202e\"\n---\n" + CLUSTER_ADMIN_CRB),
            ("K8s List bad item1 + critical item2", "apiVersion: v1\nkind: List\nitems:\n  - apiVersion: /bad\n    kind: ConfigMap\n  - " + CLUSTER_ADMIN_CRB.replace("\n", "\n    ").rstrip() + "\n"),
        ]
        for label, stream in evasion_streams:
            with self.subTest(label=label):
                r = classify_changes(
                    [FileChange(path="clusters/dev/sneak.yaml", change_type=CHANGE_ADDED, after=stream.encode("utf-8"), after_mode="100644")]
                )
                self.assertEqual(r.risk_level, RISK_CRITICAL, label)
                self.assertFalse(r.supported, label)
                self.assertIn("CRITICAL_PRIVILEGED_RBAC", {x.code for x in r.reasons})

    def test_generatename_only_bogus_core_apiversion_crd_list_name_and_secretlist(self) -> None:
        # Replacing metadata.name with metadata.generateName cannot pass as low risk!
        gen_only = SAFE_CM.replace("  name: app-cfg\n", "  generateName: app-cfg-\n")
        r_mod = classify_changes(
            [FileChange(path="clusters/dev/cm.yaml", change_type=CHANGE_MODIFIED, before=SAFE_CM.encode(), after=gen_only.encode(), before_mode="100644", after_mode="100644")]
        )
        self.assertNotEqual(r_mod.risk_level, RISK_LOW)
        self.assertFalse(r_mod.supported)
        self.assertIn("INVALID_K8S_METADATA", {x.code for x in r_mod.reasons})

        # Bogus core apiVersion `v999` rejected as INVALID_APIVERSION
        bad_core = SAFE_CM.replace("apiVersion: v1", "apiVersion: v999")
        self.assertEqual(parse_manifest_bytes(bad_core.encode()).error_code, "INVALID_APIVERSION")

        # Named CRD ending in `List` with `items: []` is NOT swallowed as an empty List envelope!
        ip_allow_list = (
            "apiVersion: networking.acme.io/v1\nkind: IPAllowList\n"
            "metadata:\n  name: prod-fw\n  namespace: shop\nitems: []\nspec:\n  cidr: 0.0.0.0/0\n"
        )
        r_crd = classify_changes(
            [FileChange(path="clusters/dev/fw.yaml", change_type=CHANGE_ADDED, after=ip_allow_list.encode(), after_mode="100644")]
        )
        self.assertEqual(r_crd.risk_level, RISK_UNKNOWN)
        self.assertFalse(r_crd.supported)
        self.assertIn("UNSUPPORTED_RESOURCE_KIND", {x.code for x in r_crd.reasons})

        # Typed `SecretList` unpacks Secrets (even omitting item kind/apiVersion) and redacts data/stringData/last-applied
        raw_secret_token = "ghp_" + ("SecretTokenValue1234567890abcdef")
        sec_list_b = (
            "apiVersion: v1\nkind: SecretList\nitems:\n"
            "  - metadata:\n      name: s1\n      namespace: shop\n"
            f'      annotations:\n        kubectl.kubernetes.io/last-applied-configuration: \'{{"stringData":{{"tok":"{raw_secret_token}"}}}}\'\n'
            f"    stringData:\n      tok: {raw_secret_token}\n"
        )
        sec_list_a = sec_list_b.replace(raw_secret_token, raw_secret_token + "ROTATED")
        out_b = parse_manifest_bytes(sec_list_b.encode())
        out_a = parse_manifest_bytes(sec_list_a.encode())
        self.assertEqual(len(out_b.resources), 1)
        self.assertNotIn(raw_secret_token, repr(out_b.resources[0].obj))
        self.assertNotIn(raw_secret_token, repr(out_b.resources[0].sanitized))
        diffs = diff_objects(out_b.resources[0].sanitized, out_a.resources[0].sanitized)
        self.assertTrue(diffs)
        self.assertNotIn(raw_secret_token, repr(diffs))

        r_sec = classify_changes(
            [FileChange(path="clusters/dev/seclist.yaml", change_type=CHANGE_MODIFIED, before=sec_list_b.encode(), after=sec_list_a.encode(), before_mode="100644", after_mode="100644")]
        )
        self.assertEqual(r_sec.risk_level, RISK_HIGH)
        self.assertNotIn(raw_secret_token, json.dumps(r_sec.to_dict()))

    def test_diff_objects_type_confusion_named_list_reordering_and_vct_matching(self) -> None:
        # `annotations: ""` -> `annotations: {configuration-snippet: ...}` MUST emit leaf add & hit CRITICAL
        dep_base = (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: shop\n  annotations: \"\"\n"
            "spec:\n  replicas: 2\n  selector:\n    matchLabels:\n      app: web\n  template:\n    metadata:\n"
            "      labels:\n        app: web\n    spec:\n      containers:\n        - name: app\n          image: gcr.io/a/b:v1.0.0\n"
        )
        dep_snip = dep_base.replace('  annotations: ""', '  annotations:\n    nginx.ingress.kubernetes.io/configuration-snippet: "evil;"')
        r_snip = classify_changes(
            [FileChange(path="clusters/dev/web.yaml", change_type=CHANGE_MODIFIED, before=dep_base.encode(), after=dep_snip.encode(), before_mode="100644", after_mode="100644")]
        )
        self.assertEqual(r_snip.risk_level, RISK_CRITICAL)
        self.assertIn("INGRESS_SNIPPET_INJECTION", {x.code for x in r_snip.reasons})

        # Reordering `initContainers` or `env` emits `$order` diff and escalates to MEDIUM (never silent LOW!)
        dep_init_1 = (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: shop\n"
            "spec:\n  replicas: 2\n  selector:\n    matchLabels:\n      app: web\n  template:\n    metadata:\n"
            "      labels:\n        app: web\n    spec:\n      initContainers:\n"
            "        - name: first\n          image: gcr.io/a/init1:v1.0.0\n"
            "        - name: second\n          image: gcr.io/a/init2:v1.0.0\n"
            "      containers:\n        - name: app\n          image: gcr.io/a/b:v1.0.0\n"
        )
        dep_init_2 = dep_init_1.replace(
            "        - name: first\n          image: gcr.io/a/init1:v1.0.0\n        - name: second\n          image: gcr.io/a/init2:v1.0.0\n",
            "        - name: second\n          image: gcr.io/a/init2:v1.0.0\n        - name: first\n          image: gcr.io/a/init1:v1.0.0\n",
        )
        r_reord = classify_changes(
            [FileChange(path="clusters/dev/web.yaml", change_type=CHANGE_MODIFIED, before=dep_init_1.encode(), after=dep_init_2.encode(), before_mode="100644", after_mode="100644")]
        )
        self.assertEqual(r_reord.risk_level, RISK_MEDIUM)

        # volumeClaimTemplates matched by metadata.name
        vct_b = {"volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"storageClassName": "a"}}]}
        vct_a = {"volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"storageClassName": "b"}}]}
        vct_diffs = diff_objects(vct_b, vct_a)
        self.assertEqual(vct_diffs, [FieldDiff(tokens=("volumeClaimTemplates", "data", "spec", "storageClassName"), op="replace", before="a", after="b")])

        # Programmatic `{1: "x"}` vs `{True: "x"}` strictly unequal and diffed cleanly
        self.assertFalse(_strict_equal({1: "x"}, {True: "x"}))
        self.assertEqual(len(diff_objects({1: "x"}, {True: "x"})), 2)


class GitLoaderAndLocalPathAdversarialTests(unittest.TestCase):
    """Adversarial git_loader.py and load_path_pair security, durability, and RAM bound tests."""

    def _init_repo(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)

    def test_rev_validation_truncation_and_timeout_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            scratch = Path(td) / "scratch"
            scratch.mkdir()
            self._init_repo(repo)
            (repo / "a.yaml").write_text(SAFE_CM, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            for bad_rev in ("", "--output=/tmp/x", "-n", "^HEAD", ":0:a.yaml", "+main", f"{sha}..HEAD", "HEAD@{0}", "main\x00", "main\x7f", "a b"):
                with self.subTest(bad_rev=bad_rev):
                    with self.assertRaises(ValueError):
                        load_git_changes(repo, bad_rev, sha)
                    with self.assertRaises(ValueError):
                        load_git_changes(repo, sha, bad_rev)

            # Truncated plumbing output fails closed with ValueError
            def _trunc_runner(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
                cp = subprocess.CompletedProcess(["git", *args], 0, b"", b"")
                cp.truncated = True  # type: ignore[attr-defined]
                return cp

            with self.assertRaises(ValueError):
                load_git_changes(repo, sha, sha, git=_trunc_runner)

            # Subprocess timeout converts to TimeoutError (OSError) and leaves zero temp `.tar` files behind
            (repo / "a.yaml").write_text(SAFE_CM + '  K2: "v2"\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "mod"], cwd=repo, check=True)
            sha2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            def _timeout_on_archive(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
                if "archive" in args:
                    raise subprocess.TimeoutExpired(cmd=["git", *args], timeout=1.0)
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=check)

            with self.assertRaises(TimeoutError):
                load_git_changes(repo, sha, sha2, git=_timeout_on_archive, scratch_dir=scratch)
            self.assertEqual(list(scratch.iterdir()), [])

    def test_padding_starvation_over_500_files_and_over_4mb_never_masks_critical_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            self._init_repo(repo)
            (repo / "init.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            # 1) 520 non-manifest padding files (`a_pad_*`) + 9 x 480 KiB YAML blobs (> 4 MiB) + `zzz_admin.yaml`
            (repo / "clusters" / "dev").mkdir(parents=True)
            for i in range(520):
                (repo / f"a_pad_{i:04d}.txt").write_text("pad\n", encoding="utf-8")
            big_cm_prefix = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: big\n  namespace: shop\ndata:\n  BLOB: \""
            big_payload = big_cm_prefix + ("A" * (470 * 1024)) + "\"\n"
            for i in range(9):
                (repo / "clusters" / "dev" / f"a_big_{i:02d}.yaml").write_text(
                    big_payload.replace("name: big", f"name: big-{i}"), encoding="utf-8"
                )
            (repo / "clusters" / "dev" / "zzz_admin.yaml").write_text(CLUSTER_ADMIN_CRB, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "starve-attempt"], cwd=repo, check=True)
            head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            changes, _ = load_git_changes(repo, base_sha, head_sha, disable_gitattributes=True)
            loaded_bytes = sum(len(c.before or b"") + len(c.after or b"") for c in changes)
            self.assertLessEqual(loaded_bytes, MAX_TOTAL_BYTES)

            verdict = classify_changes(changes)
            self.assertEqual(verdict.risk_level, RISK_CRITICAL)
            self.assertFalse(verdict.supported)
            self.assertIn("CRITICAL_PRIVILEGED_RBAC", {r.code for r in verdict.reasons})

    def test_non_utf8_git_path_does_not_crash_archive_or_mask_critical_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            self._init_repo(repo)
            (repo / "base.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            crb_blob = subprocess.check_output(
                ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=CLUSTER_ADMIN_CRB.encode("utf-8"), text=False
            ).strip()
            pad_blob = subprocess.check_output(
                ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"pad\n", text=False
            ).strip()
            # Build tree with non-UTF-8 filename `bad_\xff.yaml` alongside `crb.yaml`
            mktree_in = (
                b"100644 blob " + pad_blob + b"\tbad_\xff.yaml\x00"
                + b"100644 blob " + crb_blob + b"\tcrb.yaml\x00"
            )
            tree_sha = subprocess.check_output(["git", "mktree", "-z"], cwd=repo, input=mktree_in).strip().decode("ascii")
            commit_sha = subprocess.check_output(
                ["git", "commit-tree", tree_sha, "-p", base_sha, "-m", "non-utf8"], cwd=repo, text=True
            ).strip()

            res, _ = classify_git_range(repo, base_sha, commit_sha, disable_gitattributes=True)
            self.assertEqual(res.risk_level, RISK_CRITICAL)
            codes = {r.code for r in res.reasons}
            self.assertIn("CRITICAL_PRIVILEGED_RBAC", codes)
            self.assertIn("ARCHIVE_INTEGRITY_ERROR", codes)

    def test_worktree_and_non_mutating_disable_gitattributes_restores_prior_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            main_repo = Path(td) / "main"
            wt_dir = Path(td) / "wt"
            self._init_repo(main_repo)
            (main_repo / ".gitattributes").write_text("clusters/dev/sec.yaml export-ignore\n", encoding="utf-8")
            (main_repo / "clusters" / "dev").mkdir(parents=True)
            (main_repo / "clusters" / "dev" / "sec.yaml").write_text(SAFE_CM, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=main_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=main_repo, check=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=main_repo, text=True).strip()

            # Pre-populate custom info/attributes in main repo to verify exact restoration
            attrs_file = main_repo / ".git" / "info" / "attributes"
            attrs_file.parent.mkdir(parents=True, exist_ok=True)
            prior_content = b"*.custom binary\n"
            attrs_file.write_bytes(prior_content)

            subprocess.run(["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt_dir), base_sha], cwd=main_repo, check=True)
            self.assertTrue((wt_dir / ".git").is_file())
            (wt_dir / "clusters" / "dev" / "sec.yaml").write_text(CLUSTER_ADMIN_CRB, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=wt_dir, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "pwn"], cwd=wt_dir, check=True)
            head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=wt_dir, text=True).strip()

            res, _ = classify_git_range(wt_dir, base_sha, head_sha, disable_gitattributes=True)
            self.assertEqual(res.risk_level, RISK_CRITICAL)
            self.assertEqual(attrs_file.read_bytes(), prior_content)

    def test_tar_duplicate_and_symlink_members_handled_safely(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()

            def _crafted_archive_runner(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
                if "diff-tree" in args:
                    out = b":000000 100644 " + (b"0" * 40) + b" " + (b"1" * 40) + b" A\x00clusters/dev/cm.yaml\x00"
                    return subprocess.CompletedProcess(["git", *args], 0, out, b"")
                if "ls-tree" in args:
                    payload = SAFE_CM.encode("utf-8")
                    out = f"100644 blob {'1'*40} {len(payload)}\tclusters/dev/cm.yaml\x00".encode("utf-8")
                    return subprocess.CompletedProcess(["git", *args], 0, out, b"")
                if "archive" in args:
                    out_arg = next(a for a in args if a.startswith("--output="))
                    tar_dest = Path(out_arg.split("=", 1)[1])
                    payload = SAFE_CM.encode("utf-8")
                    with tarfile.open(tar_dest, "w") as tf:
                        sym = tarfile.TarInfo("clusters/dev/sym.yaml")
                        sym.type = tarfile.SYMTYPE
                        sym.linkname = "/etc/passwd"
                        tf.addfile(sym)
                        # Duplicate regular member for the same path -> must flag integrity error!
                        for _ in range(2):
                            ti = tarfile.TarInfo("clusters/dev/cm.yaml")
                            ti.size = len(payload)
                            tf.addfile(ti, io.BytesIO(payload))
                    return subprocess.CompletedProcess(["git", *args], 0, b"", b"")
                raise AssertionError(args)

            res, _ = classify_git_range(repo, "base", "head", git=_crafted_archive_runner)
            self.assertEqual(res.risk_level, RISK_HIGH)
            self.assertIn("ARCHIVE_INTEGRITY_ERROR", {r.code for r in res.reasons})

    def test_load_path_pair_oversized_symlinks_fifo_gitdir_and_logical_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d1 = Path(td) / "before"
            d2 = Path(td) / "after"
            outside = Path(td) / "outside"
            d1.mkdir()
            d2.mkdir()
            outside.mkdir()
            (outside / "secret.yaml").write_text(CLUSTER_ADMIN_CRB, encoding="utf-8")

            # `.git/` inside local dirs must be ignored
            (d1 / ".git").mkdir()
            (d2 / ".git").mkdir()
            (d2 / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

            # Unchanged 1 MiB oversized file must be skipped without masking sibling changes
            unchanged_big = b"X" * (MAX_FILE_BYTES + 200_000)
            (d1 / "same_big.bin").write_bytes(unchanged_big)
            (d2 / "same_big.bin").write_bytes(unchanged_big)

            # Added 5 MiB oversized file (> MAX_TOTAL_BYTES!) alongside CRITICAL manifest:
            # oversized file must have after=None so CRITICAL manifest is NOT masked as PR_PAYLOAD_TOO_LARGE!
            (d2 / "a_huge.bin").write_bytes(b"Y" * (MAX_TOTAL_BYTES + 512 * 1024))
            (d2 / "z_rbac.yaml").write_text(CLUSTER_ADMIN_CRB, encoding="utf-8")

            changes = load_path_pair(str(d1), str(d2), logical_path="clusters/prod")
            paths = [c.path for c in changes]
            self.assertNotIn("clusters/prod/same_big.bin", paths)
            self.assertFalse(any(".git" in p for p in paths))
            self.assertIn("clusters/prod/a_huge.bin", paths)
            self.assertIn("clusters/prod/z_rbac.yaml", paths)
            verdict = classify_changes(changes)
            self.assertEqual(verdict.risk_level, RISK_CRITICAL)
            self.assertIn("CRITICAL_PRIVILEGED_RBAC", {r.code for r in verdict.reasons})

            # Symlink target change + symlinked outside directory (must NOT traverse `outside/`)
            s1 = Path(td) / "sym1"
            s2 = Path(td) / "sym2"
            s1.mkdir()
            s2.mkdir()
            (s1 / "link.yaml").symlink_to("v1.yaml")
            (s2 / "link.yaml").symlink_to("v2.yaml")
            (s2 / "dir_escape").symlink_to(outside)
            sym_changes = load_path_pair(str(s1), str(s2), logical_path="clusters/dev")
            sym_paths = {c.path for c in sym_changes}
            self.assertEqual(sym_paths, {"clusters/dev/link.yaml", "clusters/dev/dir_escape"})
            self.assertEqual(classify_changes(sym_changes).risk_level, RISK_HIGH)

            # Named FIFO does not block and fails closed as RISK_HIGH (ARCHIVE_INTEGRITY_ERROR)
            fifo_path = Path(td) / "pipe.yaml"
            os.mkfifo(fifo_path)
            fifo_changes = load_path_pair("/dev/null", str(fifo_path), logical_path="clusters/dev/pipe.yaml")
            fifo_res = classify_changes(fifo_changes)
            self.assertEqual(fifo_res.risk_level, RISK_HIGH)
            self.assertFalse(fifo_res.supported)

            # Dir vs file mismatch & missing file raise clean errors
            f_ok = Path(td) / "ok.yaml"
            f_ok.write_text(SAFE_CM, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_path_pair(str(d1), str(f_ok))
            with self.assertRaises(FileNotFoundError):
                load_path_pair("/dev/null", str(Path(td) / "does_not_exist.yaml"))


class CLIEntrypointUXTests(unittest.TestCase):
    """CLI error handling (no tracebacks), exit codes, --max-risk critical, and summary formatting."""

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = cli_main(argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_cli_clean_errors_without_tracebacks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "cm.yaml"
            f.write_text(SAFE_CM, encoding="utf-8")
            d = Path(td) / "dir"
            d.mkdir()

            bad_invocations = [
                [],
                ["--repo", td],
                ["--base", "HEAD"],
                ["--repo", td, "--base", "HEAD", "--before", str(f)],
                ["--repo", str(Path(td) / "missing_repo"), "--base", "HEAD"],
                ["--repo", td, "--base", "--flag-injection"],
                ["--repo", td, "--base=--flag-injection"],
                ["--repo", td, "--base", "HEAD", "--max-risk", "invalid"],
                ["--repo", td, "--base", "deadbeef"],
                ["--before", str(Path(td) / "missing.yaml"), "--after", str(f)],
                ["--before", str(d), "--after", str(f)],
            ]
            for argv in bad_invocations:
                with self.subTest(argv=argv):
                    rc, out, err = self._run_cli(argv)
                    self.assertEqual(rc, 1)
                    self.assertEqual(out, "")
                    self.assertTrue(err.startswith("error: "), err)
                    self.assertNotIn("Traceback", err)

    def test_cli_summary_format_and_max_risk_critical(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            crb_file = Path(td) / "crb.yaml"
            crb_file.write_text(CLUSTER_ADMIN_CRB, encoding="utf-8")
            unk_file = Path(td) / "unk.py"
            unk_file.write_text("print(1)\n", encoding="utf-8")

            # `--max-risk critical` allows concrete critical (exit 0) and blocks high threshold (exit 2)
            rc_crit_ok, out_sum, err = self._run_cli(
                ["--before", "/dev/null", "--after", str(crb_file), "--path", "clusters/dev/crb.yaml", "--format", "summary", "--max-risk", "critical"]
            )
            self.assertEqual(rc_crit_ok, 0, err)
            self.assertIn("risk_level=critical", out_sum)
            self.assertIn("effective_changes=True", out_sum)
            self.assertIn("stats: files=1", out_sum)
            self.assertIn("rbac.authorization.k8s.io/ClusterRoleBinding pwn-admin", out_sum)

            rc_high_block, _, _ = self._run_cli(
                ["--before", "/dev/null", "--after", str(crb_file), "--path", "clusters/dev/crb.yaml", "--max-risk", "high"]
            )
            self.assertEqual(rc_high_block, 2)

            # `unknown` ALWAYS exits 2 even when `--max-risk critical` is set!
            rc_unk_block, _, _ = self._run_cli(
                ["--before", "/dev/null", "--after", str(unk_file), "--path", "scripts/unk.py", "--max-risk", "critical"]
            )
            self.assertEqual(rc_unk_block, 2)


if __name__ == "__main__":
    unittest.main()

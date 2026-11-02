#!/usr/bin/env python3
"""Tests for scripts/check_cluster_reads.py.

The gate is the last class: the checkout's own `agents/` tree against the
committed allow-list has nothing unpinned, which is what CI reads. The rest
drive the check over fixture trees: a `<root>/agents/` written under a
temporary directory, so the paths the check reports read `agents/...` as
they do for the repository.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_cluster_reads import (  # noqa: E402
    ALLOWLIST,
    FLAGS_WITH_VALUE,
    RULE_MULTI_KIND,
    RULE_UNPAGED,
    RULE_UNRESOLVED,
    SCAN_ROOT,
    SKIP_PATH_PREFIXES,
    check,
    count_lines,
    judge,
    main,
    markdown_findings,
    python_findings,
    shell_findings,
    shell_tokens,
)


def _tokens(line: str) -> list[str]:
    return line.split()


class JudgeTest(unittest.TestCase):
    """One argv at a time, both readings."""

    def test_multi_kind_dump_json_and_yaml(self):
        for fmt in ("json", "yaml"):
            with self.subTest(fmt=fmt):
                self.assertEqual(judge(_tokens(f"kubectl get pods,svc -A -o {fmt}"), python_argv=False), RULE_MULTI_KIND)

    def test_single_kind_unpaged(self):
        self.assertEqual(judge(_tokens("kubectl get pods -A -o json"), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(_tokens("kubectl get pods --all-namespaces -o json"), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(_tokens("kubectl get pods -A -ojson"), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(_tokens("kubectl get pods -A --output=yaml"), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(_tokens("kubectl get pods -Ao json"), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(_tokens("kubectl get pods -Aojson"), python_argv=False), RULE_UNPAGED)

    def test_chunk_size_pages_a_single_kind_but_not_a_dump(self):
        self.assertIsNone(judge(_tokens("kubectl get pods -A -o json --chunk-size=500"), python_argv=False))
        self.assertIsNone(judge(_tokens("kubectl get pods -A --chunk-size 500 -o json"), python_argv=False))
        self.assertEqual(judge(_tokens("kubectl get pods,svc -A -o json --chunk-size=500"), python_argv=False), RULE_MULTI_KIND)

    def test_chunk_size_zero_disables_paging(self):
        self.assertEqual(judge(_tokens("kubectl get pods -A -o json --chunk-size=0"), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(_tokens("kubectl get pods -A --chunk-size 0 -o json"), python_argv=False), RULE_UNPAGED)

    def test_chunk_size_value_the_check_cannot_read_is_still_paged(self):
        self.assertIsNone(judge(["kubectl", "get", "pods", "-A", "-o", "json", "--chunk-size", None], python_argv=True))
        self.assertIsNone(judge(["kubectl", "get", "pods", "-A", "-o", "json", "--chunk-size={}"], python_argv=True))
        self.assertIsNone(judge(_tokens("kubectl get pods -A -o json --chunk-size $PAGE"), python_argv=False))

    def test_shell_expansion_as_the_kinds_is_unresolved(self):
        for line in (
            'kubectl get "$KINDS" -A -o json --chunk-size=500',
            "kubectl get ${KINDS} -A -o json",
            "kubectl get $kind -A -o yaml",
        ):
            with self.subTest(line=line):
                self.assertEqual(judge(_tokens(line), python_argv=False), RULE_UNRESOLVED)
        self.assertIsNone(judge(_tokens('kubectl get "$KIND" -n "$NS" -o json'), python_argv=False))

    def test_a_boolean_watch_flag_does_not_eat_the_token_after_it(self):
        self.assertEqual(judge(_tokens("kubectl get --output-watch-events pods -A -o json --watch"), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(_tokens("kubectl get pods --output-watch-events -A -o json -w"), python_argv=False), RULE_UNPAGED)

    def test_all_is_several_kinds(self):
        self.assertEqual(judge(_tokens("kubectl get all -A -o json"), python_argv=False), RULE_MULTI_KIND)
        self.assertEqual(judge(_tokens("kubectl get all -A -o json --chunk-size=500"), python_argv=False), RULE_MULTI_KIND)

    def test_projections_pass(self):
        for line in (
            "kubectl get pods -A -o jsonpath={.items[*].metadata.name}",
            "kubectl get pods -A -o custom-columns=NAME:.metadata.name --no-headers",
            "kubectl get pods -A -o name",
            "kubectl get pods -A -o wide",
            "kubectl get pods -A",
        ):
            with self.subTest(line=line):
                self.assertIsNone(judge(_tokens(line), python_argv=False))

    def test_raw_with_limit_passes(self):
        self.assertIsNone(judge(_tokens("kubectl get --raw /api/v1/pods?limit=500"), python_argv=False))
        self.assertIsNone(judge(_tokens("kubectl get --raw=/api/v1/pods?limit=500"), python_argv=False))
        self.assertIsNone(judge(_tokens("kubectl get --raw '/apis/apps/v1/deployments?limit=500&continue=abc'"), python_argv=False))

    def test_raw_whole_cluster_collection_without_limit_is_unpaged(self):
        for url in ("/api/v1/pods", "/apis/apps/v1/deployments", "/api/v1/namespaces", "/apis/metrics.k8s.io/v1beta1/nodes"):
            with self.subTest(url=url):
                self.assertEqual(judge(_tokens(f"kubectl get --raw {url}"), python_argv=False), RULE_UNPAGED)
                self.assertEqual(judge(["kubectl", "get", "--raw", url], python_argv=True), RULE_UNPAGED)

    def test_raw_namespaced_object_discovery_and_health_paths_pass(self):
        for url in (
            "/api/v1/namespaces/kube-system/pods",
            "/api/v1/nodes/gke-node-1",
            "/apis/apps/v1",
            "/api",
            "/healthz",
            "/metrics",
        ):
            with self.subTest(url=url):
                self.assertIsNone(judge(_tokens(f"kubectl get --raw {url}"), python_argv=False))

    def test_namespaced_and_single_object_reads_pass(self):
        for line in (
            "kubectl get pods -n kube-system -o json",
            "kubectl get deploy/web -o json",
            "kubectl get pods web -o json",
            "kubectl get pods web -n kube-system -o json",
        ):
            with self.subTest(line=line):
                self.assertIsNone(judge(_tokens(line), python_argv=False))

    def test_an_unknown_flag_value_after_the_kinds_does_not_hide_the_read(self):
        for line in (
            "kubectl get pods -A -o json -v 6",
            "kubectl get pods -A -o json --timeout 10s",
            "kubectl get pods -A -o json --some-new-flag value",
        ):
            with self.subTest(line=line):
                self.assertEqual(judge(_tokens(line), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(["kubectl", "get", "pods", "-A", "-o", "json", None], python_argv=True), RULE_UNPAGED)

    def test_global_flags_and_env_prefix(self):
        self.assertEqual(judge(_tokens("kubectl --context $CTX get gcp -A -o json"), python_argv=False), RULE_UNPAGED)
        self.assertEqual(judge(_tokens("kubectl --kubeconfig /k get a,b -A -o json"), python_argv=False), RULE_MULTI_KIND)
        self.assertEqual(judge(_tokens("KUBECONFIG=$KC kubectl get a,b -A -o json"), python_argv=False), RULE_MULTI_KIND)
        self.assertEqual(judge(_tokens("env KUBECONFIG=$KC kubectl get a,b -A -o json"), python_argv=False), RULE_MULTI_KIND)

    def test_a_command_word_before_kubectl_does_not_hide_the_read(self):
        for line in (
            "timeout 30 kubectl get pods -A -o json",
            "time kubectl get pods -A -o json",
            "sudo /usr/local/bin/kubectl get pods -A -o json",
        ):
            with self.subTest(line=line):
                self.assertEqual(judge(_tokens(line), python_argv=False), RULE_UNPAGED)

    def test_other_subcommands_and_other_commands_pass(self):
        for line in (
            "kubectl auth can-i get pods -A -o json",
            "kubectl describe pods -A -o json",
            "gcloud get pods,svc -A -o json",
            "get pods,svc -A -o json",
        ):
            with self.subTest(line=line):
                self.assertIsNone(judge(_tokens(line), python_argv=False))

    def test_python_reading_bare_get_and_implicit_output(self):
        self.assertEqual(judge(["get", "svc", "-A"], python_argv=True), RULE_UNPAGED)
        self.assertEqual(judge([None, "get", "svc", "-A"], python_argv=True), RULE_UNPAGED)
        self.assertIsNone(judge([None, "-o", "json"], python_argv=True))
        self.assertEqual(judge(["get", "svc,ing", "-A"], python_argv=True), RULE_MULTI_KIND)
        # A list that opens with `kubectl` and names no `-o` is a table, as on a shell line.
        self.assertIsNone(judge(["kubectl", "get", "svc", "-A"], python_argv=True))
        self.assertIsNone(judge(["kubectl", "get", "svc", "-A", "--no-headers"], python_argv=True))
        self.assertEqual(judge(["kubectl", "get", "svc", "-A", "-o", "json"], python_argv=True), RULE_UNPAGED)
        self.assertIsNone(judge(["get", "svc", "-A", "-o", "custom-columns=NAME:.metadata.name"], python_argv=True))
        self.assertIsNone(judge(["get", "svc"], python_argv=True))
        self.assertIsNone(judge(["get", "list", "watch"], python_argv=True))

    def test_unknown_kinds_token_has_its_own_rule_and_no_flag_clears_it(self):
        self.assertEqual(judge(["kubectl", "get", None, "-A", "-o", "json"], python_argv=True), RULE_UNRESOLVED)
        self.assertEqual(judge(["kubectl", "get", "{}", "-A", "-o", "json"], python_argv=True), RULE_UNRESOLVED)
        self.assertEqual(judge(["kubectl", "get", None, "-A", "-o", "json", "--chunk-size=500"], python_argv=True), RULE_UNRESOLVED)
        self.assertIsNone(judge(["kubectl", "get", None, "-n", "x", "-o", "json"], python_argv=True))

    def test_unknown_output_format_is_unresolved_and_no_flag_clears_it(self):
        for argv in (
            ["kubectl", "get", "pods", "-A", "-o", None],
            ["kubectl", "get", "deploy,sts,ds", "-A", "-o", None],
            ["kubectl", "get", "pods", "-A", "-o={}"],
            ["kubectl", "get", "pods", "-A", "-o", None, "--chunk-size=500"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(judge(argv, python_argv=True), RULE_UNRESOLVED)
        self.assertEqual(judge(shell_tokens('kubectl get pods -A -o "$FMT"'), python_argv=False), RULE_UNRESOLVED)
        self.assertEqual(judge(shell_tokens("kubectl get pods -A --output=${FMT}"), python_argv=False), RULE_UNRESOLVED)
        # A projection whose template the check cannot read is still a projection.
        self.assertIsNone(judge(["kubectl", "get", "pods", "-A", "-o", "jsonpath={}"], python_argv=True))
        self.assertIsNone(judge(shell_tokens('kubectl get pods -A -o jsonpath="$TEMPLATE"'), python_argv=False))
        self.assertIsNone(judge(["kubectl", "get", "pods", "-n", "x", "-o", None], python_argv=True))

    def test_kubectl_run_through_a_shell_variable_is_read(self):
        for line in (
            "$KUBECTL get pods -A -o json",
            "${KUBECTL} get pods -A -o json",
            '"${KUBECTL:-kubectl}" get pods -A -o json',
            "$TIMEOUT kubectl get pods -A -o json",
        ):
            with self.subTest(line=line):
                self.assertEqual(judge(shell_tokens(line), python_argv=False), RULE_UNPAGED)
        self.assertIsNone(judge(shell_tokens("$HELM get values web -A -o json"), python_argv=False))
        self.assertIsNone(judge(shell_tokens("$KUBECTL get pods -n x -o json"), python_argv=False))

    def test_placeholder_as_the_kinds_is_unresolved(self):
        for line in (
            "kubectl --context <ctx> get <kind> -A -o json --chunk-size=500",
            "kubectl get <kinds> -A -o yaml",
            "KUBECONFIG=<target kubeconfig> kubectl get <resource> -A -o json",
        ):
            with self.subTest(line=line):
                self.assertEqual(judge(shell_tokens(line), python_argv=False), RULE_UNRESOLVED)
        for line in (
            "kubectl get <kind> -n <ns> <name> -o yaml",
            "kubectl get <kind> <name> -o yaml --kubeconfig <target kubeconfig>",
            "kubectl --context <ctx> get pods -A -o name",
        ):
            with self.subTest(line=line):
                self.assertIsNone(judge(shell_tokens(line), python_argv=False))
        self.assertEqual(judge(shell_tokens("kubectl --context <ctx> get pods,svc -A -o json"), python_argv=False), RULE_MULTI_KIND)


class PythonReadingTest(unittest.TestCase):
    """Argv literals through ast, with the module's constants resolved."""

    def _findings(self, source: str):
        return python_findings(Path("agents/x.py"), textwrap.dedent(source))

    def test_literal_argv(self):
        found = self._findings(
            '''
            import subprocess
            def run():
                subprocess.run(["kubectl", "get", "pods,svc", "-A", "-o", "json"])
            '''
        )
        self.assertEqual([(f.line, f.rule) for f in found], [(4, RULE_MULTI_KIND)])
        self.assertEqual(found[0].text, "kubectl get pods,svc -A -o json")

    def test_constants_resolve_through_names_and_starred_tuples(self):
        found = self._findings(
            '''
            KUBECTL = "kubectl"
            KINDS = "pdb,deploy,statefulset"
            ALL_NAMESPACES_FLAG = "-A"
            OUTPUT_FLAG = "-o"
            JSON_OUTPUT_ARGS = (OUTPUT_FLAG, "json")
            def run():
                return [KUBECTL, "get", KINDS, ALL_NAMESPACES_FLAG, *JSON_OUTPUT_ARGS]
            '''
        )
        self.assertEqual([(f.line, f.rule) for f in found], [(8, RULE_MULTI_KIND)])
        self.assertEqual(found[0].text, "kubectl get pdb,deploy,statefulset -A -o json")

    def test_starred_conditional_splices_both_branches_and_a_parameter_is_unknown(self):
        found = self._findings(
            '''
            KUBECTL = "kubectl"
            ALL_NAMESPACES_FLAG = "-A"
            JSON_OUTPUT_ARGS = ("-o", "json")
            def read(resources, namespaced):
                return [KUBECTL, "get", resources, *([ALL_NAMESPACES_FLAG] if namespaced else []), *JSON_OUTPUT_ARGS]
            '''
        )
        self.assertEqual([(f.line, f.rule) for f in found], [(6, RULE_UNRESOLVED)])
        self.assertEqual(found[0].text, "kubectl get {} -A -o json")

    def test_loop_variable_is_judged_once_per_element_of_the_enclosing_loop(self):
        found = self._findings(
            '''
            SYNC_KINDS = ("rootsyncs.configsync.gke.io", "reposyncs.configsync.gke.io")
            CRDS = ("a.example.io", "b.example.io", "c.example.io")
            def run(run_and_gate):
                for kind in CRDS:
                    pass
                for kind in SYNC_KINDS:
                    run_and_gate(["kubectl", "get", kind, "-A", "-o", "json", "--chunk-size=500"])
                for kind in SYNC_KINDS:
                    run_and_gate(["kubectl", "get", kind, "-A", "-o", "json"])
            '''
        )
        self.assertEqual(
            [(f.line, f.rule, f.text) for f in found],
            [
                (10, RULE_UNPAGED, "kubectl get rootsyncs.configsync.gke.io -A -o json"),
                (10, RULE_UNPAGED, "kubectl get reposyncs.configsync.gke.io -A -o json"),
            ],
        )

    def test_nested_loops_over_literal_sequences_are_judged_once_per_combination(self):
        found = self._findings(
            '''
            KINDS = ("rootsyncs", "reposyncs")
            KUBECONFIGS = ("/k/a", "/k/b")
            def run(run_and_gate):
                for kind in KINDS:
                    for kc in KUBECONFIGS:
                        run_and_gate(["kubectl", "--kubeconfig", kc, "get", kind, "-A", "-o", "json"])
                for kc in KUBECONFIGS:
                    for kind in KINDS:
                        run_and_gate(f"kubectl --kubeconfig {kc} get {kind} -A -o json --chunk-size=500")
            '''
        )
        self.assertEqual(
            [(f.line, f.rule, f.text) for f in found],
            [
                (7, RULE_UNPAGED, "kubectl --kubeconfig /k/a get rootsyncs -A -o json"),
                (7, RULE_UNPAGED, "kubectl --kubeconfig /k/b get rootsyncs -A -o json"),
                (7, RULE_UNPAGED, "kubectl --kubeconfig /k/a get reposyncs -A -o json"),
                (7, RULE_UNPAGED, "kubectl --kubeconfig /k/b get reposyncs -A -o json"),
            ],
        )

    def test_a_sum_of_a_sequence_constant_and_a_list_is_one_argv(self):
        found = self._findings(
            '''
            KUBECTL_GET = ["kubectl", "get"]
            JSON_OUTPUT_ARGS = ("-o", "json")
            def _kubectl(kc):
                return ["kubectl", "--kubeconfig", kc]
            def run(kc, rest):
                a = KUBECTL_GET + ["pods,svc", "-A", "-o", "json"]
                b = KUBECTL_GET + ["pods", "-A"] + list(JSON_OUTPUT_ARGS)
                c = _kubectl(kc) + ["get", "pods,svc", "-A", "-o", "json"]
                d = KUBECTL_GET + rest
                e = KUBECTL_GET + ["pods", "-n", "x", "-o", "json"]
                return a, b, c, d, e
            '''
        )
        self.assertEqual(
            [(f.line, f.rule, f.text) for f in found],
            [
                (7, RULE_MULTI_KIND, "kubectl get pods,svc -A -o json"),
                (9, RULE_MULTI_KIND, "{} get pods,svc -A -o json"),
            ],
        )

    def test_spliced_helper_prefix_and_shell_strings_are_read(self):
        found = self._findings(
            '''
            import subprocess
            def _kubectl(kc):
                return ["kubectl", "--kubeconfig", kc]
            def run(kc):
                """Docstring: kubectl get pods,svc -A -o json is not read here."""
                a = [*_kubectl(kc), "get", "pods,svc", "-A", "-o", "json"]
                b = _kubectl(kc) + ["get", "pods,svc", "-A", "-o", "json"]
                c = subprocess.run("kubectl get pods,svc -A -o json", shell=True)
                d = subprocess.run("KUBECONFIG=x kubectl get pods -A -o json 2>/dev/null".split())
                e = "the read `kubectl get pods -A` answered nothing"
                return a, b, c, d, e
            '''
        )
        self.assertEqual(
            [(f.line, f.rule) for f in found],
            [(7, RULE_MULTI_KIND), (8, RULE_MULTI_KIND), (9, RULE_MULTI_KIND), (10, RULE_UNPAGED)],
        )

    def test_fstring_and_concatenated_shell_lines_are_read(self):
        found = self._findings(
            '''
            import subprocess
            KINDS = "deploy,sts,ds"
            SYNC_KINDS = ("rootsyncs", "reposyncs")
            def run(kc, kind, paged):
                a = subprocess.run(f"KUBECONFIG={kc} kubectl get {KINDS} -A -o json", shell=True)
                b = subprocess.run(f"kubectl get {kind} -A -o json".split())
                c = subprocess.run("kubectl get pods,svc" + " -A -o json", shell=True)
                d = f"kubectl get pods -A -o json --chunk-size={paged}"
                for sync in SYNC_KINDS:
                    subprocess.run(f"kubectl get {sync} -A -o json", shell=True)
                e = "kubectl get pods,svc -A -o json " + kc
                g = "kubectl get " + kind + " -A -o json"
                for sync in SYNC_KINDS:
                    subprocess.run("kubectl get " + sync + " -A -o json", shell=True)
                return a, b, c, d, e, g
            '''
        )
        self.assertEqual(
            [(f.line, f.rule, f.text) for f in found],
            [
                (6, RULE_MULTI_KIND, "KUBECONFIG={} kubectl get deploy,sts,ds -A -o json"),
                (7, RULE_UNRESOLVED, "kubectl get {} -A -o json"),
                (8, RULE_MULTI_KIND, "kubectl get pods,svc -A -o json"),
                (11, RULE_UNPAGED, "kubectl get rootsyncs -A -o json"),
                (11, RULE_UNPAGED, "kubectl get reposyncs -A -o json"),
                (12, RULE_MULTI_KIND, "kubectl get pods,svc -A -o json {}"),
                (13, RULE_UNRESOLVED, "kubectl get {} -A -o json"),
                (15, RULE_UNPAGED, "kubectl get rootsyncs -A -o json"),
                (15, RULE_UNPAGED, "kubectl get reposyncs -A -o json"),
            ],
        )

    def test_chunk_size_from_a_non_string_expression_is_paged(self):
        found = self._findings(
            '''
            CHUNK_SIZE = 500
            def run():
                a = ["kubectl", "get", "pods", "-A", "-o", "json", "--chunk-size", str(CHUNK_SIZE)]
                b = ["kubectl", "get", "pods", "-A", "-o", "json", f"--chunk-size={CHUNK_SIZE}"]
                c = ["kubectl", "get", "pods", "-A", "-o", "json", "--chunk-size", "0"]
                return a, b, c
            '''
        )
        self.assertEqual([(f.line, f.rule) for f in found], [(6, RULE_UNPAGED)])

    def test_fstring_projection_passes_and_fstring_kinds_is_unresolved(self):
        found = self._findings(
            '''
            TEMPLATE = "{.items[*].metadata.name}"
            def run(kind):
                a = ["kubectl", "get", "pods", "-A", "-o", f"jsonpath={TEMPLATE}"]
                b = ["kubectl", "get", f"{kind}", "-A", "-o", "json"]
                return a, b
            '''
        )
        self.assertEqual([(f.line, f.rule) for f in found], [(5, RULE_UNRESOLVED)])

    def test_single_kind_with_and_without_chunk_size(self):
        found = self._findings(
            '''
            def run():
                paged = ["kubectl", "get", "pods", "-A", "-o", "json", "--chunk-size=500"]
                whole = ["kubectl", "get", "pods", "-A", "-o", "json"]
                return paged, whole
            '''
        )
        self.assertEqual([(f.line, f.rule) for f in found], [(4, RULE_UNPAGED)])

    def test_helper_argv_without_output_is_full_object(self):
        found = self._findings(
            '''
            CRD = "certificates.cert-manager.io"
            TASKS = (
                ("certificates", CRD, ["get", CRD, "-A"]),
                ("clusterissuers", CRD, ["get", CRD]),
            )
            COUNT = ("kubectl", "get", CRD, "-A", "-o", "custom-columns=NAME:.metadata.name", "--no-headers")
            '''
        )
        self.assertEqual([(f.line, f.rule, f.text) for f in found], [(4, RULE_UNPAGED, "get certificates.cert-manager.io -A")])

    def test_unparseable_source_is_skipped(self):
        self.assertEqual(self._findings("def (:\n"), [])


class ShellAndMarkdownReadingTest(unittest.TestCase):
    def test_shell_lines_continuations_and_comments(self):
        found = shell_findings(
            Path("agents/x.sh"),
            textwrap.dedent(
                '''
                #!/bin/bash
                # kubectl get pods,svc -A -o json
                KUBECONFIG="$KC" kubectl get pods,svc \\
                  -A -o json > "$OUT"
                kubectl get pods -A | grep -v Running
                kubectl get pods -A -o json | jq '.items[]'
                kubectl get deploy,sts -A -o json 2>/dev/null > state.json
                kubectl get pods -A -o json 2>&1 | head
                kubectl get pods -A -o json --chunk-size 500 > "$OUT"
                kubectl get pods -A -o json --chunk-size 500 2>/dev/null
                kubectl get pods -A -o json -v 6 > "$OUT"
                bash -c "kubectl get pods -A -o json"
                '''
            ),
        )
        self.assertEqual(
            [(f.line, f.rule, f.text) for f in found],
            [
                (4, RULE_MULTI_KIND, "KUBECONFIG=$KC kubectl get pods,svc -A -o json"),
                (7, RULE_UNPAGED, "kubectl get pods -A -o json"),
                (8, RULE_MULTI_KIND, "kubectl get deploy,sts -A -o json"),
                (9, RULE_UNPAGED, "kubectl get pods -A -o json"),
                (12, RULE_UNPAGED, "kubectl get pods -A -o json -v 6"),
                (13, RULE_UNPAGED, "kubectl get pods -A -o json"),
            ],
        )

    def test_a_json_example_command_in_a_fence_is_read(self):
        found = markdown_findings(
            Path("agents/x.md"),
            textwrap.dedent(
                '''
                ```json
                {
                  "check": "model-image-floating-tag",
                  "command": "kubectl --context gke_<project>_<location>_<cluster> get deploy,sts,ds -A -o json"
                }
                ```
                ```json
                {"check": "pdb", "command": "kubectl --context prod get pdb -A -o custom-columns=NS:.metadata.namespace --no-headers"}
                ```
                ```json
                {"check": "pods", "command": "kubectl --context prod get pods -A -o json", "rc": 0}
                {"check": "svc", "command": "kubectl --context prod get svc -A -o json"}
                ```
                '''
            ),
        )
        self.assertEqual(
            [(f.line, f.rule, f.text) for f in found],
            [
                (5, RULE_MULTI_KIND, "kubectl --context gke_$project_$location_$cluster get deploy,sts,ds -A -o json"),
                (12, RULE_UNPAGED, "kubectl --context prod get pods -A -o json"),
                (13, RULE_UNPAGED, "kubectl --context prod get svc -A -o json"),
            ],
        )

    def test_markdown_fence_and_span_fail_and_prose_does_not(self):
        found = markdown_findings(
            Path("agents/x.md"),
            textwrap.dedent(
                '''
                Never run kubectl get pods,svc -A -o json against a large cluster.

                ```bash
                KUBECONFIG=$KC kubectl get deploy,sts -A -o json > /tmp/state.json
                ```

                - **Command:** `kubectl --context <ctx> get computeclasses,deployments -A -o yaml`
                - Join against `kubectl get pvc -A -o json` and `kubectl get pods -A
                  -o json | jq '.items[]'`.
                - `kubectl get pods -A -o json --chunk-size=500` is fine; `kubectl get pods -n x -o json` too.
                '''
            ),
        )
        self.assertEqual(
            [(f.line, f.rule) for f in found],
            [(5, RULE_MULTI_KIND), (8, RULE_MULTI_KIND), (9, RULE_UNPAGED), (9, RULE_UNPAGED)],
        )

    def test_fence_on_a_numbered_list_line(self):
        page = textwrap.dedent(
            '''
            1. ```bash
               kubectl get pods,svc -A -o json
               ```
            2. Then read the file; kubectl get pods -A -o json is prose here.
            '''
        )
        found = markdown_findings(Path("agents/x.md"), page)
        self.assertEqual([(f.line, f.rule) for f in found], [(3, RULE_MULTI_KIND)])

    def test_a_fence_closes_only_on_its_own_character_and_length(self):
        page = textwrap.dedent(
            '''
            ````markdown
            ```bash
            kubectl get pods,svc -A -o json
            ```
            ````
            ~~~
            kubectl get pods -A -o json
            ```
            kubectl get deploy,sts -A -o yaml
            ~~~
            Then kubectl get svc -A -o json is prose again.
            '''
        )
        found = markdown_findings(Path("agents/x.md"), page)
        self.assertEqual([(f.line, f.rule) for f in found], [(4, RULE_MULTI_KIND), (8, RULE_UNPAGED), (10, RULE_MULTI_KIND)])

    def test_stray_backtick_does_not_pair_across_a_blank_line(self):
        page = "A stray ` here.\n\nNever run kubectl get pods -A -o json on a big cluster` again.\n"
        self.assertEqual(markdown_findings(Path("agents/x.md"), page), [])

    def test_unbalanced_quote_in_a_span_is_still_read(self):
        found = markdown_findings(Path("agents/x.md"), "Run `kubectl get pods -A -o json | jq 'it's`.\n")
        self.assertEqual([(f.line, f.rule) for f in found], [(1, RULE_UNPAGED)])


class FixtureTree:
    """A `<root>/agents/` tree and an allow-list under one temporary directory."""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)
        self.root = self.base / "agents"
        self.root.mkdir()
        self.allowlist = self.base / "allowlist.txt"
        self.allowlist.write_text("", encoding="utf-8")

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def pin(self, lines: str) -> None:
        self.allowlist.write_text(textwrap.dedent(lines), encoding="utf-8")

    def check(self):
        return check(self.root, self.allowlist)

    def cleanup(self) -> None:
        self._dir.cleanup()


NEW_COLLECTOR = '''
    import subprocess
    def collect(kubeconfig):
        return subprocess.run(["kubectl", "get", "pods,services,ingresses", "-A", "-o", "json"], check=False)
    '''


class FixtureTreeTest(unittest.TestCase):
    def setUp(self):
        self.tree = FixtureTree()
        self.addCleanup(self.tree.cleanup)

    def test_fixture_script_with_a_new_multi_kind_read_fails(self):
        self.tree.write("platform/scripts/new_collector.py", NEW_COLLECTOR)
        errors, files, pinned = self.tree.check()
        self.assertEqual(files, 1)
        self.assertEqual(pinned, 0)
        self.assertEqual(len(errors), 3, errors)
        self.assertIn("agents/platform/scripts/new_collector.py: 1 finding(s) of rule 'multi-kind-dump' and no allow-list entry", errors[0])
        self.assertIn("agents/platform/scripts/new_collector.py:4: multi-kind-dump: kubectl get pods,services,ingresses -A -o json", errors[1])

    def test_pinned_read_passes_and_the_message_counts_it(self):
        self.tree.write("platform/scripts/new_collector.py", NEW_COLLECTOR)
        self.tree.pin("# pinned\nagents/platform/scripts/new_collector.py multi-kind-dump 1 the collector's one dump\n")
        errors, files, pinned = self.tree.check()
        self.assertEqual(errors, [])
        self.assertEqual((files, pinned), (1, 1))

    def test_rise_drop_and_clean_entries_fail(self):
        self.tree.write("platform/scripts/new_collector.py", NEW_COLLECTOR)
        cases = {
            "rise": ("agents/platform/scripts/new_collector.py multi-kind-dump 1 reason\n", "pods,svc"),
            "drop": ("agents/platform/scripts/new_collector.py multi-kind-dump 2 reason\n", None),
            "clean": ("agents/platform/scripts/new_collector.py unpaged-list 1 reason\n", None),
        }
        for name, (pin, extra_kinds) in cases.items():
            with self.subTest(case=name):
                source = NEW_COLLECTOR
                if extra_kinds:
                    source += f'\n    OTHER = ["kubectl", "get", "{extra_kinds}", "-A", "-o", "json"]\n'
                self.tree.write("platform/scripts/new_collector.py", source)
                self.tree.pin(pin)
                errors, _, _ = self.tree.check()
                self.assertTrue(errors, name)
                expected = {"rise": "the list only shrinks", "drop": "lower the entry to 1", "clean": "remove the entry"}[name]
                self.assertTrue(any(expected in error for error in errors), (name, errors))

    def test_malformed_unknown_rule_duplicate_and_missing_path_entries_fail(self):
        self.tree.write("platform/scripts/new_collector.py", NEW_COLLECTOR)
        self.tree.pin(
            '''
            agents/platform/scripts/new_collector.py multi-kind-dump
            agents/platform/scripts/new_collector.py multi-kind-dump 0 zero
            agents/platform/scripts/new_collector.py truncated 1 no such rule
            agents/platform/scripts/new_collector.py multi-kind-dump 1 first
            agents/platform/scripts/new_collector.py multi-kind-dump 1 repeated
            agents/platform/scripts/gone.py multi-kind-dump 1 a file that is not there
            '''
        )
        errors, _, _ = self.tree.check()
        texts = "\n".join(errors)
        self.assertIn(":2: expected `<path> <rule> <count> <reason>`", texts)
        self.assertIn(":3: expected `<path> <rule> <count> <reason>`", texts)
        self.assertIn("rule 'truncated' is not one of", texts)
        self.assertIn("already has an entry for 'multi-kind-dump' at line 5", texts)
        self.assertIn("agents/platform/scripts/gone.py is not a file this check scans", texts)
        self.assertEqual(len(errors), 5, errors)

    def test_tests_testdata_and_mirrored_skills_are_not_read(self):
        self.tree.write("platform/scripts/test_collector.py", NEW_COLLECTOR)
        self.tree.write("platform/scripts/testdata/sample.py", NEW_COLLECTOR)
        self.tree.write("platform/skills/gke-upgrades/SKILL.md", "```\nkubectl get pdb,deploy -A -o json\n```\n")
        self.tree.write("platform/skills/mine/SKILL.md", "Read `kubectl get pods -n x -o json`.\n")
        errors, files, pinned = self.tree.check()
        self.assertEqual(errors, [])
        self.assertEqual((files, pinned), (1, 0))

    def test_empty_tree_fails(self):
        errors, files, _ = self.tree.check()
        self.assertEqual(files, 0)
        self.assertEqual(len(errors), 1)
        self.assertIn("scans nothing", errors[0])

    def test_counts_and_main(self):
        self.tree.write("platform/scripts/new_collector.py", NEW_COLLECTOR)
        self.tree.write("platform/skills/mine/SKILL.md", "Run `kubectl get pods -A -o json`.\n")
        self.assertEqual(
            count_lines(self.tree.root),
            [
                "agents/platform/scripts/new_collector.py multi-kind-dump 1",
                "agents/platform/skills/mine/SKILL.md unpaged-list 1",
            ],
        )
        argv = ["--root", str(self.tree.root), "--allowlist", str(self.tree.allowlist)]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(argv), 1)
        self.assertIn("no allow-list entry", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(argv + ["--counts"]), 0)
        self.assertEqual(out.getvalue().splitlines(), count_lines(self.tree.root))
        self.tree.pin(
            '''
            agents/platform/scripts/new_collector.py multi-kind-dump 1 reason
            agents/platform/skills/mine/SKILL.md unpaged-list 1 reason
            '''
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(argv), 0)
        self.assertIn("2 file(s) read, 2 pinned finding(s), nothing new", out.getvalue())


class RepositoryGateTest(unittest.TestCase):
    """What CI reads: the checkout's agents/ tree against the committed allow-list."""

    def test_tree_has_no_unpinned_whole_cluster_read(self):
        errors, files, pinned = check(SCAN_ROOT, ALLOWLIST)
        self.assertEqual(errors, [], "\n".join(errors))
        self.assertGreater(files, 0)
        self.assertGreater(pinned, 0)


class SiblingVocabularyTest(unittest.TestCase):
    """The two facts this check copies from elsewhere in the tree stay equal to their owners."""

    def test_flags_with_value_cover_the_transcript_readers_table(self):
        spec = importlib.util.spec_from_file_location(
            "worker_transcript", _REPO_ROOT / "agents" / "platform" / "scripts" / "worker_transcript.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        owned = module.KUBECTL_VALUE_FLAGS | module.KUBECTL_FILE_FLAGS
        self.assertEqual(owned - FLAGS_WITH_VALUE, set(), "add the flag to FLAGS_WITH_VALUE in check_cluster_reads.py")
        self.assertEqual(
            FLAGS_WITH_VALUE & module.KUBECTL_BOOLEAN_FLAGS, set(), "a boolean flag in FLAGS_WITH_VALUE eats the token after it"
        )

    def test_skip_prefix_is_the_overlay_tools_mirrored_skill_path(self):
        from scripts import skill_overlay

        mirrored = skill_overlay.SKILLS_ROOT.relative_to(SCAN_ROOT).as_posix() + "/" + skill_overlay.UPSTREAM_SKILL_PREFIX
        self.assertEqual(SKIP_PATH_PREFIXES, (mirrored,))


if __name__ == "__main__":
    unittest.main()

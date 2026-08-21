"""Cross-file contract lints: joins that nothing in any language checks.

Three contracts, each of which fails silently today when the two sides drift:

* A verification spec naming a tool that no registry defines can never trip —
  the silent-green shape review found on the first gate branch (a safeguard
  forbidding a tool that did not exist).
* The bash and python DNS-endpoint predicates are a documented "keep in step"
  pair maintained by hand in two languages; a divergence strands whichever
  caller uses the stale one on the wrong control-plane endpoint.
* Workflows are joined to each other by display-name strings (`workflow_run:
  workflows: [...]`) and artifact-name strings; renaming a workflow silently
  disables every consumer, which for the autopush chain means continuous
  deployment stops without a red anywhere.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPTS_DIR = REPO_ROOT / "agents" / "platform" / "scripts"


def _yaml():
    import yaml

    return yaml


class SpecToolRegistryTest(unittest.TestCase):
    """Every tool name in a verification spec exists in a live registry."""

    # Hermes-image built-in tools this repository references but does not
    # define: the kanban pool. Evidence of each lives in the image patches
    # (deploy/docker/patches/*kanban*); a name added here needs the same.
    HERMES_BUILTIN_TOOLS = {
        "kanban_create",
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_heartbeat",
    }

    def _registered_mcp_tools(self):
        names = set()
        decorated = re.compile(r"@mcp\.tool\(\)\s*\ndef\s+(\w+)\s*\(")
        for path in SCRIPTS_DIR.glob("*.py"):
            names.update(decorated.findall(path.read_text(errors="replace")))
        return names

    def _spec_tool_names(self):
        yaml = _yaml()
        wanted = []
        for task_path in (REPO_ROOT / "bench" / "tasks").glob("*/task.yaml"):
            try:
                document = yaml.safe_load(task_path.read_text()) or {}
            except Exception as exc:  # noqa: BLE001
                self.fail(f"{task_path} does not parse: {exc}")
            entries = document.get("verification_spec") or []

            def walk(node):
                if isinstance(node, dict):
                    if node.get("type") == "tool_called":
                        for name in node.get("tool_names") or []:
                            wanted.append((task_path, name))
                    for value in node.values():
                        walk(value)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(entries)
        return wanted

    def test_every_spec_tool_name_resolves_to_a_registry(self):
        registry = self._registered_mcp_tools() | self.HERMES_BUILTIN_TOOLS
        unresolved = [
            f"{path.parent.name}: {name}"
            for path, name in self._spec_tool_names()
            if name not in registry
        ]
        self.assertEqual(
            [],
            unresolved,
            "verification specs name tools no registry defines — such a check "
            "can never trip, which is a silent-green gate: " + ", ".join(unresolved),
        )

    def test_the_builtin_allowlist_still_has_evidence_in_the_image_patches(self):
        patches = REPO_ROOT / "deploy" / "docker" / "patches"
        corpus = "\n".join(
            p.read_text(errors="replace") for p in patches.glob("*kanban*")
        )
        for name in sorted(self.HERMES_BUILTIN_TOOLS):
            root = name.removeprefix("kanban_")
            self.assertTrue(
                name in corpus or f"'{root}'" in corpus or f'"{root}"' in corpus,
                f"{name} is allowlisted as a hermes builtin but the image "
                "patches carry no evidence of it — stale allowlist entry",
            )


class DnsPredicateParityTest(unittest.TestCase):
    """The bash and python DNS-endpoint predicates answer alike, case by case.

    `k8s-operator/scripts/gke_dns_endpoint.sh` says "Keep the two predicates
    in step" about `agents/platform/scripts/gke_endpoint.py`; this is the
    table that enforces the sentence. Both sides run for real — bash through
    a fake gcloud on PATH, python through its Runner seam.
    """

    CASES = [
        # (name, endpoint, allowExternalTraffic, expect_flag)
        ("configured and open", "x.gke.goog", True, True),
        ("configured but closed", "x.gke.goog", False, False),
        ("no endpoint", "", True, False),
        ("block absent", None, None, False),
    ]

    def _python_answer(self, endpoint, external):
        sys.path.insert(0, str(SCRIPTS_DIR))
        import gke_endpoint

        gke_endpoint.reset_cache()

        def runner(argv):
            if "--help" in argv:
                return 0, "... --dns-endpoint ..."
            config = {}
            if endpoint is not None:
                dns = {}
                if endpoint:
                    dns["endpoint"] = endpoint
                if external is not None:
                    dns["allowExternalTraffic"] = external
                config = {"dnsEndpointConfig": dns}
            return 0, json.dumps({"controlPlaneEndpointsConfig": config})

        args = gke_endpoint.dns_endpoint_args("p", "c", "l", run=runner)
        gke_endpoint.reset_cache()
        return bool(args)

    def _bash_answer(self, endpoint, external):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            if endpoint is None:
                # No tab at all: the predicate's treat-as-unknown branch.
                emit = "printf 'NOSEP\\n'"
            else:
                external_text = "True" if external else "False"
                # The tab must be REAL on the wire, exactly as gcloud's
                # value() format emits it — printf interprets \t in its
                # format string, which survives shell quoting intact.
                emit = f"printf '{endpoint}\\t{external_text}\\n'"
            fake = bin_dir / "gcloud"
            fake.write_text(
                "#!/bin/bash\n"
                'if [[ "$*" == *"--help"* ]]; then echo "... --dns-endpoint ..."; exit 0; fi\n'
                + emit + "\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            script = (
                f'source "{REPO_ROOT}/k8s-operator/scripts/gke_dns_endpoint.sh"\n'
                "gke_dns_endpoint_flag c l p\n"
                'printf "%s" "$GKE_DNS_ENDPOINT_FLAG"\n'
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            return completed.stdout.strip() == "--dns-endpoint"

    def test_the_two_predicates_agree_on_every_case(self):
        for name, endpoint, external, expected in self.CASES:
            with self.subTest(case=name):
                python_says = self._python_answer(endpoint, external)
                bash_says = self._bash_answer(endpoint, external)
                self.assertEqual(
                    expected,
                    python_says,
                    f"python predicate wrong for {name!r}",
                )
                self.assertEqual(
                    python_says,
                    bash_says,
                    f"the two predicates diverge on {name!r} — the pair is "
                    "documented as kept-in-step and one caller is now wrong",
                )


class WorkflowNameJoinTest(unittest.TestCase):
    """String joins between workflows resolve to workflows that exist."""

    def _workflows(self):
        yaml = _yaml()
        documents = {}
        for path in WORKFLOWS.glob("*.yml"):
            documents[path.name] = yaml.safe_load(path.read_text()) or {}
        return documents

    def test_every_workflow_run_reference_names_a_real_workflow(self):
        documents = self._workflows()
        display_names = {
            document.get("name")
            for document in documents.values()
            if document.get("name")
        }
        broken = []
        for filename, document in documents.items():
            # PyYAML parses the unquoted key `on:` as boolean True.
            triggers = document.get("on") or document.get(True) or {}
            if not isinstance(triggers, dict):
                continue
            workflow_run = triggers.get("workflow_run") or {}
            for referenced in workflow_run.get("workflows") or []:
                if referenced not in display_names:
                    broken.append(f"{filename} -> {referenced!r}")
        self.assertEqual(
            [],
            broken,
            "workflow_run references that match no workflow name: a rename "
            "has silently disabled these consumers: " + ", ".join(broken),
        )

    def test_the_coverage_artifact_name_join_holds(self):
        producer = (WORKFLOWS / "python-tests.yml").read_text()
        consumer_path = WORKFLOWS / "coverage-comment.yml"
        if not consumer_path.exists():
            self.skipTest("coverage-comment.yml not present on this branch")
        consumer = consumer_path.read_text()
        self.assertIn("diff-cover-report", producer)
        self.assertIn(
            "diff-cover-report",
            consumer,
            "the poster downloads an artifact name the producer no longer uploads",
        )


if __name__ == "__main__":
    unittest.main()

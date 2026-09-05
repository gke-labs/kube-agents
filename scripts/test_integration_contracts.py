"""Cross-file contract lints: joins that nothing in any language checks.

Four contracts, each of which fails silently today when the two sides drift:

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
* The broken-main notifier watches a fixed roster of push-to-main workflows;
  a required check dropped from that roster goes red on main without anyone
  being told, which is how a thirteen-hour red main went unnoticed (#1223).
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

# The push-to-main workflows `main-broken-notify.yml` must watch: every required
# push-to-main workflow whose run re-checks everything it covers, so its green on
# main is a statement about the tree. The header of that workflow says what
# qualifies and why `Prettier Check` does not. `Documentation Checks` and
# `Python Unit Tests` are here because they were missing when both went red on
# main for thirteen hours and nothing paged (gke-labs/kube-agents#1223).
BROKEN_MAIN_NOTIFIER = "main-broken-notify.yml"
BROKEN_MAIN_WATCHED_WORKFLOWS = (
    "Actionlint",
    "Docker Build",
    "Documentation Checks",
    "Operator Tests",
    "Python Unit Tests",
    "Validate Repo Structure",
)
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
        "kanban_list",
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_heartbeat",
    }

    def _mcp_server_aliases(self):
        """Alias → the local server script it launches, from every agent config.

        The alias is the `mcp_servers` key, and it is what namespaces the tool
        at call time. Servers whose argv is the remote proxy
        (`/opt/mcp-remote/dist/proxy.js <url>`) are skipped: their tool list
        lives behind that URL and nothing in this repository can enumerate it.
        A spec naming one of those is rejected, and the fix is an allowlist
        entry with evidence, the same bargain HERMES_BUILTIN_TOOLS makes.
        """
        yaml = _yaml()
        aliases = {}
        configs = list((REPO_ROOT / "agents").glob("*/config.yaml"))
        configs.append(REPO_ROOT / "deploy" / "shared" / "defaults" / "config.yaml")
        for config_path in configs:
            if not config_path.exists():
                continue
            document = yaml.safe_load(config_path.read_text()) or {}
            for alias, spec in (document.get("mcp_servers") or {}).items():
                for arg in (spec or {}).get("args") or []:
                    if arg.endswith(".py"):
                        aliases[alias] = Path(arg).name
        return aliases

    def _registered_mcp_tools(self):
        """The tool names a trajectory can actually carry, not the bare ones.

        `tool_called` matches a trajectory entry's `name` exactly, and for an
        MCP tool that name is namespaced by the server alias — every fixture
        in the harness carries `mcp_platform_control_list_clusters` or
        `mcp__router__list_agents`, never the bare `list_clusters` the `def`
        is written with. Registering bare names would accept precisely the
        spellings that can never match and reject the ones that do, which is
        the silent-green shape this module exists to prevent.

        Both separator spellings are registered because both appear in the
        harness's own fixtures (`bench/tests/test_harness.py`); which one a
        run produces is the MCP client's business, not this repository's.
        """
        decorated = re.compile(r"@mcp\.tool\(\)\s*\ndef\s+(\w+)\s*\(")
        by_script = {}
        for path in (REPO_ROOT / "agents").glob("*/scripts/*.py"):
            found = decorated.findall(path.read_text(errors="replace"))
            if found:
                by_script.setdefault(path.name, set()).update(found)

        names = set()
        for alias, script_name in self._mcp_server_aliases().items():
            for tool in by_script.get(script_name, ()):
                names.add(f"mcp_{alias}_{tool}")
                names.add(f"mcp__{alias}__{tool}")
        return names

    def test_the_alias_to_server_join_still_resolves(self):
        """The join above is the whole check; an empty one passes everything.

        A config refactor that moves `mcp_servers`, or a rename of the server
        script, would leave `_registered_mcp_tools` returning an empty set —
        and an empty registry rejects every spec name rather than accepting
        them, so it fails loudly. What it would not catch is the join quietly
        covering fewer servers than it used to, which is what this pins.
        """
        aliases = self._mcp_server_aliases()
        self.assertIn("platform_control", aliases)
        self.assertEqual("platform_mcp_server.py", aliases["platform_control"])
        registered = self._registered_mcp_tools()
        # Derived rather than written down: alias `platform_control` joined to
        # a real `@mcp.tool()` def in the server it launches. The harness's own
        # fixtures use `list_clusters`, which is not a tool this server
        # defines — synthetic names in a fixture are fine, and a spec naming
        # one is exactly what this lint is for.
        self.assertIn("mcp_platform_control_verify_gke_cluster", registered)
        self.assertNotIn("verify_gke_cluster", registered)

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

    `scripts/installer/gke_dns_endpoint.sh` says "Keep the two predicates
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
                f'source "{REPO_ROOT}/scripts/installer/gke_dns_endpoint.sh"\n'
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

    def test_the_broken_main_notifier_watches_every_whole_tree_required_check(self):
        """A required check missing from the watch list fails silently: main
        goes red, every open pull request inherits the red, and nobody is told.
        That is how #1223's thirteen-hour window happened. The test above
        catches a watched name that stopped matching; this one catches a name
        dropped from the list in an edit. Neither can tell that a context newly
        promoted to required was never added: required-ness lives in branch
        protection, not in the tree, so adding a workflow to this tuple stays a
        review question whenever one joins the required set."""
        document = self._workflows()[BROKEN_MAIN_NOTIFIER]
        triggers = document.get("on") or document.get(True) or {}
        watched = set((triggers.get("workflow_run") or {}).get("workflows") or [])
        missing = sorted(set(BROKEN_MAIN_WATCHED_WORKFLOWS) - watched)
        self.assertEqual(
            [],
            missing,
            f"{BROKEN_MAIN_NOTIFIER} no longer watches these push-to-main "
            "workflows, so a red on main from any of them files no issue: "
            + ", ".join(missing),
        )

    def test_the_required_python_job_runs_the_suite_in_strict_mode(self):
        """The gate is one flag on one line, and losing it fails open.

        `make coverage` tolerates a failing test directory by design -- it is a
        meter, and one red directory must not hide the number for the rest. The
        job that reports the required `Run Python Unit Tests` context runs that
        target, so COVERAGE_STRICT=1 is the only thing making a red suite a red
        check. Drop it in a reformat and CI reports success on failing tests:
        the suite still runs, the log still shows the failures, and the context
        still goes green. Nothing else in the repository asserts this.
        """
        yaml = _yaml()
        workflow = yaml.safe_load((WORKFLOWS / "python-tests.yml").read_text())
        steps = workflow["jobs"]["test"]["steps"]
        commands = " ".join(step.get("run", "") for step in steps)
        self.assertIn(
            "COVERAGE_STRICT=1",
            commands,
            "the required Run Python Unit Tests job no longer passes "
            "COVERAGE_STRICT=1, so a failing test directory would be reported "
            "but would not fail the check",
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

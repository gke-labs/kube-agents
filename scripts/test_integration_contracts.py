"""Cross-file contract lints: joins that nothing in any language checks.

Contracts that each fail silently today when the two sides drift:

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
* The flaky-check notifier's roster is the inverse: every pull-request check
  is either watched or named as excluded with a reason; a new check that is
  neither has its re-runs recorded nowhere, and nothing says so.
* The CI-health bot reads the pool-pressure periodic's artifact instead of
  importing it, so a renamed field raises nothing: it blanks every number in
  the Chat note and sends the fallback message.
"""

from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# The push-to-main workflows `main-broken-notify.yml` must watch: every required
# push-to-main workflow whose run re-checks everything it covers, so its green on
# main is a statement about the tree. The header of that workflow says what
# qualifies and why `Prettier Check` does not. `Documentation Checks` and
# `Python Unit Tests` are here because they were missing when both went red on
# main for thirteen hours and nothing paged (gke-labs/kube-agents#1223).
# `A2A Module Tests` joined when its job became a required context on
# 2026-10-13, and `k8s-api-proxy Tests` on 2026-10-28; both filter on push as
# `Operator Tests` does.
BROKEN_MAIN_NOTIFIER = "main-broken-notify.yml"
BROKEN_MAIN_WATCHED_WORKFLOWS = (
    "A2A Module Tests",
    "Actionlint",
    "Docker Build",
    "Documentation Checks",
    "k8s-api-proxy Tests",
    "Operator Tests",
    "Python Unit Tests",
    "Validate Repo Structure",
)
# The release pipelines `release-stage-broken-notify.yml` must watch: the three
# `workflow_dispatch` workflows the schedulers start, whose red run is
# otherwise a row in the Actions tab and nothing more (gke-labs/kube-agents#1978).
# The watched list is `WATCHED_WORKFLOWS` in
# `scripts/release/notify_release_stage_broken.py`, which the sweep reads.
RELEASE_STAGE_NOTIFIER = "release-stage-broken-notify.yml"
RELEASE_STAGE_WATCHED_WORKFLOWS = (
    "RC: Release Pipeline",
    "Staging: Promotion Pipeline",
    "Release & Publish (GA)",
)
# The compensation and non-gating jobs of those workflows, by `name:`: the
# ones the notifier names as the failing stage only when nothing else failed,
# because they run after a gating failure or fail inside green runs and are
# skipped on an ordinary green night. The notifier knows them by the
# substrings in `COMPENSATION_JOB_MARKERS`; this tuple is what those
# substrings have to select, so a rename of any of them reds the test.
RELEASE_STAGE_COMPENSATION_JOBS = (
    "Step 5 - Tear Down RC Environment",
    "Today mode (non-gating) - Step 6 - Provision RC Environment",
    "Today mode (non-gating) - Step 7 - Run E2E Tests",
    "Today mode (non-gating) - Step 8 - Tear Down RC Environment",
    "Step 3b - Roll Back to the Previous GA and Forward Again",
    "Step 5b - Withdraw an Unsettled Nomination",
    "Step 7 - Tear Down Nightly Environment",
)
# What marks a job as one that runs regardless of, or without failing, the
# run: job-level `continue-on-error`, or an `if:` that runs it after a
# failure. Every job of the watched workflows that carries one has to be in
# the tuple above.
RELEASE_STAGE_ALWAYS_RUN_CONDITIONS = ("always()", "!cancelled()")
# The flaky-check reporter's contract is the inverse of the broken-main one:
# not a fixed list it must contain, but that every pull-request check is
# either watched or named here as left out on purpose. The watched list is
# `WATCHED_WORKFLOWS` in `scripts/notify_flaky_check.py`, which the sweep
# reads; the reasons for the exclusions are beside it, and this tuple is the
# machine-readable copy of them.
FLAKY_CHECK_NOTIFIER = "flaky-check-notify.yml"
FLAKY_CHECK_EXCLUDED_WORKFLOWS = (
    # A title edit legitimately turns it green without a commit.
    "Validate PR Title",
    # The advisory database moves between attempts.
    "Security Scanning",
    # pull_request_target automations, not checks on the tree.
    "Auto Enqueue",
    "Hold Unresolved Threads",
    "Merge Gate",
    "Risk Classification",
)
SCRIPTS_DIR = REPO_ROOT / "agents" / "platform" / "scripts"
REPO_SCRIPTS = REPO_ROOT / "scripts"
# The pool-pressure periodic's captured breach day, replayed here to get a real
# pool-pressure.json. test_pool_pressure.py drives the same pair.
POOL_PRESSURE_FIXTURE = REPO_SCRIPTS / "testdata" / "pool_pressure" / "breach"
POOL_PRESSURE_AS_OF = datetime(2026, 8, 27, tzinfo=timezone.utc)
POOL_PRESSURE_WINDOW_DAYS = 1


def _yaml():
    import yaml

    return yaml


def _flaky_check_reporter():
    sys.path.insert(0, str(REPO_SCRIPTS))
    try:
        import notify_flaky_check
    finally:
        sys.path.remove(str(REPO_SCRIPTS))
    return notify_flaky_check


def _release_stage_notifier():
    sys.path.insert(0, str(REPO_SCRIPTS / "release"))
    try:
        import notify_release_stage_broken
    finally:
        sys.path.remove(str(REPO_SCRIPTS / "release"))
    return notify_release_stage_broken


class SpecToolRegistryTest(unittest.TestCase):
    """Every tool name in a verification spec exists in a live registry."""

    # Hermes-image built-in tools this repository references but does not
    # define: the kanban pool, and the skill writer the image gates. Evidence
    # of each lives in the image patches that touch them, and only in its own
    # family's files (HERMES_BUILTIN_EVIDENCE); a name added here needs the same.
    HERMES_BUILTIN_EVIDENCE = {
        "*kanban*": {
            "kanban_create",
            "kanban_list",
            "kanban_show",
            "kanban_complete",
            "kanban_block",
            "kanban_heartbeat",
            "kanban_comment",
            "kanban_unblock",
        },
        "*skill_manage_image_owned*": {"skill_manage"},
    }
    HERMES_BUILTIN_TOOLS = set().union(*HERMES_BUILTIN_EVIDENCE.values())

    # Tools of Hermes's built-in `file` toolset that a spec names: a check that
    # a worker wrote a file. No image patch touches them, so their evidence is
    # different (test_the_file_tools_still_have_evidence_in_the_configs): the
    # Chat Agent's config names the tools beside the `file` toolset it
    # disables, and the Platform Agent's config gives its kanban workers the
    # `hermes-cli` bundle (which holds that toolset) on `cli` and does not
    # disable `file`. A name added here needs the same.
    # `read_file` is the read half of the same toolset: the retrospective cases
    # grade the worker reading the saved report or the chat record through it.
    HERMES_FILE_TOOLS = {"read_file", "write_file", "patch"}
    HERMES_FILE_TOOLSET = "file"
    HERMES_FILE_TOOL_DOC = "agents/chat/config.yaml"
    PLATFORM_CONFIG = "agents/platform/config.yaml"
    PLATFORM_WORKER_TOOLSET_KEY = "cli"
    PLATFORM_WORKER_BUNDLE = "hermes-cli"
    # Hermes core tools no image patch touches, so the patches carry no
    # evidence of them. Evidence of each is the agent text that tells the agent
    # to use it, backticked or called
    # (test_the_core_allowlist_still_has_evidence_in_the_agent_text).
    HERMES_CORE_TOOLS = {"skill_view"}
    CORE_TOOL_EVIDENCE = ("agents/platform/skills/github-issue-resolver/SKILL.md",)

    # Tools behind a remote MCP proxy (`/opt/mcp-remote/dist/proxy.js <url>`),
    # as (server alias, tool): nothing in this repository can enumerate them,
    # so a spec may name one only through this list, registered under both
    # separator spellings like the local servers. Evidence of each lives in
    # the personas or the skill references that tell the agent about the
    # tool (test_the_remote_allowlist_still_has_evidence_in_the_agent_text);
    # a name added here needs the same, and its alias must still be a
    # remote-proxy server in some agent config.
    REMOTE_MCP_TOOLS = {
        # #1765: the 50-a-day Developer Knowledge method the personas forbid
        # and knowledge-grounding-sources-probe's safeguard names.
        ("developer_knowledge", "answer_query"),
        # #953: read-only workload existence inspection across clusters
        ("gke", "get_k8s_resource"),
        ("gke", "describe_k8s_resource"),
    }
    REMOTE_TOOL_EVIDENCE = (
        "agents/platform/SOUL.md",
        "agents/cluster/SOUL.md",
        "agents/platform/skills/gke-basics/references/mcp-usage.md",
    )

    # Claude Code harness tools the session worker runs under, by the name its
    # stream-json carries (the name a `tool_called` check on a session case
    # matches). Nothing here defines them; each needs evidence in the worker
    # source (test_the_session_harness_tools_still_have_evidence_in_the_worker):
    #
    # * a built-in on the adapter's `defaultAllowedTools` constant in
    #   a2a/cmd/worker-adapter/main.go, the surface every session gets;
    # * `Bash`, from the same file's `clusterViewAllowedTools`, the surface a
    #   session gets only with the cluster view (`Bash(kubectl:*)` and
    #   `Bash(gcloud:*)`, both the Bash tool); `defaultDisallowedTools`
    #   disallows it otherwise, which is what lets a case read a Bash call as
    #   "the view was on";
    # * the delegate MCP tool, as main.go's `delegateToolID` composes it:
    #   "mcp__" + DelegateMCPServer + "__" + DelegateToolName, both halves
    #   from a2a/worker-adapter/mcp.go;
    # * `Skill`, which is deliberately on no allowlist: the harness runs a
    #   skill whose frontmatter carries only name and description without a
    #   permission rule (a2a/cmd/worker-adapter/skill_allowlist_test.go). Its
    #   evidence is the session persona telling the model to load skills
    #   "with the Skill tool" and the persona build (a2a/cmd/session-persona)
    #   cutting each skill's frontmatter down to the two fields that tool
    #   runs unprompted. Neither proves the harness exposes the tool, only
    #   that this repository relies on it; the eval is what proves the rest.
    SESSION_HARNESS_TOOLS = {"Read", "Glob", "Grep", "Bash", "mcp__a2a__delegate", "Skill"}
    WORKER_ADAPTER_MAIN = "a2a/cmd/worker-adapter/main.go"
    WORKER_ADAPTER_MCP = "a2a/worker-adapter/mcp.go"
    SKILL_TOOL_EVIDENCE = (
        "a2a/persona/session/persona.md",
        "a2a/cmd/session-persona/transform.go",
    )

    # Trajectory markers a transport writes as entries of its own, not tool
    # calls. A spec may name one in `tool_called` (wrapped in `none`, it says
    # "this record is not the inject transport's"). Evidence of each is a
    # constant the inject transport passes to `self._record` and a constant
    # scoring's `_inject_record` reads it by
    # (test_the_transport_markers_still_have_evidence_in_writer_and_reader).
    TRANSPORT_MARKERS = {"inject.task"}
    INJECT_TRANSPORT = "bench/kube_agents_bench/inject_transport.py"
    SCORING = "bench/kube_agents_bench/scoring.py"

    # Trajectory entries the harness writes itself after a stopped ask's
    # opening turn (bench/kube_agents_bench/stop_hook.py): what it did, not a
    # tool the agent called. A spec names them in `tool_called` with
    # `require_success`. Evidence of each is a constant in the stop hook that
    # the harness puts on an entry's `name`
    # (test_the_harness_entries_still_have_evidence_in_the_hook_and_harness).
    HARNESS_ENTRIES = {"harness_stop_sent", "harness_cards_stopped"}
    STOP_HOOK = "bench/kube_agents_bench/stop_hook.py"
    HARNESS = "bench/kube_agents_bench/harness.py"

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

    def _remote_mcp_aliases(self):
        """Aliases whose server is the remote proxy, from every agent config."""
        yaml = _yaml()
        aliases = set()
        configs = list((REPO_ROOT / "agents").glob("*/config.yaml"))
        configs.append(REPO_ROOT / "deploy" / "shared" / "defaults" / "config.yaml")
        for config_path in configs:
            if not config_path.exists():
                continue
            document = yaml.safe_load(config_path.read_text()) or {}
            for alias, spec in (document.get("mcp_servers") or {}).items():
                args = (spec or {}).get("args") or []
                if any(str(arg).endswith("proxy.js") for arg in args):
                    aliases.add(alias)
        return aliases

    def _remote_mcp_tools(self):
        """REMOTE_MCP_TOOLS in the two namespaced spellings a trajectory carries."""
        names = set()
        for alias, tool in self.REMOTE_MCP_TOOLS:
            names.add(f"mcp_{alias}_{tool}")
            names.add(f"mcp__{alias}__{tool}")
        return names

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
        registry = (
            self._registered_mcp_tools()
            | self._remote_mcp_tools()
            | self.HERMES_BUILTIN_TOOLS
            | self.HERMES_FILE_TOOLS
            | self.HERMES_CORE_TOOLS
            | self.SESSION_HARNESS_TOOLS
            | self.TRANSPORT_MARKERS
            | self.HARNESS_ENTRIES
        )
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

    @staticmethod
    def _names_builtin(name, corpus):
        """Whether the corpus names the tool as a call or a quoted name.

        A bare substring is not enough: `skill_manage` is part of the patch
        module's own file name, which every file of that family imports. A
        kanban tool also counts by its quoted action (`"create"`), the form
        the kanban patches dispatch on.
        """
        root = name.removeprefix("kanban_")
        forms = [f"{name}(", f"'{name}'", f'"{name}"']
        if root != name:
            forms += [f"'{root}'", f'"{root}"']
        return any(form in corpus for form in forms)

    def test_the_builtin_allowlist_still_has_evidence_in_the_image_patches(self):
        patches = REPO_ROOT / "deploy" / "docker" / "patches"
        for pattern, names in sorted(self.HERMES_BUILTIN_EVIDENCE.items()):
            corpus = "\n".join(p.read_text(errors="replace") for p in patches.glob(pattern))
            for name in sorted(names):
                self.assertTrue(
                    self._names_builtin(name, corpus),
                    f"{name} is allowlisted as a hermes builtin but the {pattern} "
                    "image patches carry no evidence of it — stale allowlist entry",
                )

    def test_builtin_evidence_is_a_call_or_a_quoted_name(self):
        self.assertFalse(self._names_builtin("skill_manage", "from tools.skill_manage_image_owned import x"))
        self.assertTrue(self._names_builtin("skill_manage", "raw = smt.skill_manage(**kwargs)"))
        self.assertTrue(self._names_builtin("kanban_create", 'if action == "create":'))
        self.assertFalse(self._names_builtin("skill_manage", 'if action == "create":'))


    def test_the_file_tools_still_have_evidence_in_the_configs(self):
        yaml = _yaml()
        doc = (REPO_ROOT / self.HERMES_FILE_TOOL_DOC).read_text(errors="replace")
        toolset_line = next(
            (line for line in doc.splitlines() if re.match(rf"\s*-\s*{self.HERMES_FILE_TOOLSET}\s*#", line)),
            "",
        )
        for name in sorted(self.HERMES_FILE_TOOLS):
            self.assertRegex(
                toolset_line,
                rf"\b{name}\b",
                f"{name} is allowlisted as a `{self.HERMES_FILE_TOOLSET}` toolset tool but "
                f"{self.HERMES_FILE_TOOL_DOC} no longer names it beside that toolset — stale allowlist entry",
            )
        platform = yaml.safe_load((REPO_ROOT / self.PLATFORM_CONFIG).read_text())
        worker = platform["platform_toolsets"][self.PLATFORM_WORKER_TOOLSET_KEY]
        self.assertIn(
            self.PLATFORM_WORKER_BUNDLE,
            worker,
            f"{self.PLATFORM_CONFIG} no longer gives its kanban workers {self.PLATFORM_WORKER_BUNDLE}, "
            "so the file tools are not a platform worker's — stale allowlist entry",
        )
        self.assertNotIn(
            self.HERMES_FILE_TOOLSET,
            (platform.get("agent") or {}).get("disabled_toolsets") or [],
            f"{self.PLATFORM_CONFIG} disables the `{self.HERMES_FILE_TOOLSET}` toolset, so no "
            "platform worker can call the file tools — stale allowlist entry",
        )

    def test_the_core_allowlist_still_has_evidence_in_the_agent_text(self):
        corpus = "\n".join(
            (REPO_ROOT / rel).read_text(errors="replace") for rel in self.CORE_TOOL_EVIDENCE
        )
        for name in sorted(self.HERMES_CORE_TOOLS):
            self.assertTrue(
                f"`{name}`" in corpus or f"{name}(" in corpus,
                f"{name} is allowlisted as a hermes core tool but the agent text "
                "carries no evidence of it — stale allowlist entry",
            )

    def test_the_remote_allowlist_still_has_evidence_in_the_agent_text(self):
        remote = self._remote_mcp_aliases()
        corpus = "\n".join(
            (REPO_ROOT / rel).read_text(errors="replace") for rel in self.REMOTE_TOOL_EVIDENCE
        )
        for alias, tool in sorted(self.REMOTE_MCP_TOOLS):
            self.assertIn(
                alias,
                remote,
                f"{alias} is allowlisted as a remote MCP server but no agent config "
                "launches it through the remote proxy — stale allowlist entry",
            )
            self.assertTrue(
                f"{alias}__{tool}" in corpus or f"`{tool}`" in corpus,
                f"{alias}/{tool} is allowlisted as a remote MCP tool but the personas "
                "and skill references carry no evidence of it — stale allowlist entry",
            )

    @staticmethod
    def _go_string_const(source, name):
        """The value of a Go `name = "literal"` constant, or None."""
        found = re.search(rf'^\s*{name}\s*=\s*"([^"]*)"\s*$', source, re.M)
        return found.group(1) if found else None

    def _session_harness_evidence(self):
        """Every harness tool name the worker source gives evidence of."""
        main = (REPO_ROOT / self.WORKER_ADAPTER_MAIN).read_text(errors="replace")
        mcp = (REPO_ROOT / self.WORKER_ADAPTER_MCP).read_text(errors="replace")
        evidenced = set()

        allowed = self._go_string_const(main, "defaultAllowedTools")
        self.assertTrue(allowed, f"{self.WORKER_ADAPTER_MAIN} no longer defines defaultAllowedTools as a literal")
        evidenced.update(t.strip() for t in allowed.split(",") if t.strip())
        view = re.search(r'^\s*clusterViewAllowedTools\s*=\s*defaultAllowedTools\s*\+\s*"([^"]*)"\s*$', main, re.M)
        if view:
            # `Bash(kubectl:*)` is the Bash tool with a permission rule.
            evidenced.update(t.strip().split("(")[0] for t in view.group(1).split(",") if t.strip())

        server = self._go_string_const(mcp, "DelegateMCPServer")
        tool = self._go_string_const(mcp, "DelegateToolName")
        self.assertTrue(server and tool, f"{self.WORKER_ADAPTER_MCP} no longer names the delegate server and tool")
        composed = re.search(
            r'delegateToolID\s*=\s*"mcp__"\s*\+\s*workeradapter\.DelegateMCPServer\s*\+\s*"__"'
            r"\s*\+\s*workeradapter\.DelegateToolName",
            main,
        )
        if composed:
            evidenced.add(f"mcp__{server}__{tool}")

        corpora = [(REPO_ROOT / rel).read_text(errors="replace") for rel in self.SKILL_TOOL_EVIDENCE]
        if all("the Skill tool" in corpus for corpus in corpora):
            evidenced.add("Skill")
        return evidenced

    def test_the_session_harness_tools_still_have_evidence_in_the_worker(self):
        evidenced = self._session_harness_evidence()
        # Read alone is on the allowlist today; an extraction that went empty
        # would leave only the Skill/delegate branches, which this catches.
        self.assertIn("Read", evidenced, "the defaultAllowedTools extraction found nothing")
        for name in sorted(self.SESSION_HARNESS_TOOLS):
            self.assertIn(
                name,
                evidenced,
                f"{name} is registered as a session harness tool but the worker adapter, "
                "its delegate server and the session persona carry no evidence of it — "
                "stale registry entry",
            )

    def test_the_transport_markers_still_have_evidence_in_writer_and_reader(self):
        transport = (REPO_ROOT / self.INJECT_TRANSPORT).read_text(errors="replace")
        scoring = (REPO_ROOT / self.SCORING).read_text(errors="replace")
        reader = re.search(r"^def _inject_record\(.*?(?=^\S)", scoring, re.M | re.S)
        self.assertIsNotNone(reader, f"{self.SCORING} no longer defines _inject_record")
        for marker in sorted(self.TRANSPORT_MARKERS):
            literal = re.escape(json.dumps(marker))
            writers = re.findall(rf"^(\w+)\s*=\s*{literal}\s*$", transport, re.M)
            self.assertTrue(
                any(f"self._record({const}," in transport for const in writers),
                f"{marker} is registered as a transport marker but {self.INJECT_TRANSPORT} "
                "no longer writes it — stale registry entry",
            )
            readers = re.findall(rf"^(\w+)\s*=\s*{literal}\s*$", scoring, re.M)
            self.assertTrue(
                any(re.search(rf"\b{const}\b", reader.group(0)) for const in readers),
                f"{marker} is registered as a transport marker but scoring's _inject_record "
                "no longer reads it — stale registry entry",
            )

    def test_the_harness_entries_still_have_evidence_in_the_hook_and_harness(self):
        hook = (REPO_ROOT / self.STOP_HOOK).read_text(errors="replace")
        harness = (REPO_ROOT / self.HARNESS).read_text(errors="replace")
        for entry in sorted(self.HARNESS_ENTRIES):
            literal = re.escape(json.dumps(entry))
            consts = re.findall(rf"^(\w+)\s*=\s*{literal}\s*$", hook, re.M)
            self.assertTrue(
                any(f'"name": stop_hook.{const}' in harness for const in consts),
                f"{entry} is registered as a harness entry but {self.HARNESS} no longer "
                f"writes it from a {self.STOP_HOOK} constant — stale registry entry",
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

    def test_the_flaky_check_notifier_watches_every_pull_request_check_or_says_why_not(self):
        """A pull-request check missing from the watch list fails silently:
        re-runs of it record nothing and nothing says so. Every workflow with
        a pull_request or pull_request_target trigger is therefore either in
        the list or in FLAKY_CHECK_EXCLUDED_WORKFLOWS with its reason, and a
        new check has to choose. The reverse holds too: an exclusion that no
        longer names a workflow is stale."""
        documents = self._workflows()
        watched = set(_flaky_check_reporter().WATCHED_WORKFLOWS)
        pull_request_checks = set()
        for filename, document in documents.items():
            if filename == FLAKY_CHECK_NOTIFIER:
                continue
            on = document.get("on") or document.get(True) or {}
            # `on:` may be a mapping, a list, or a bare scalar (`on: pull_request`);
            # iterating the scalar would walk its characters.
            if isinstance(on, str):
                on = {on: None}
            elif not isinstance(on, dict):
                on = {key: None for key in on}
            if "pull_request" in on or "pull_request_target" in on:
                pull_request_checks.add(document.get("name") or filename)
        excluded = set(FLAKY_CHECK_EXCLUDED_WORKFLOWS)
        unaccounted = sorted(pull_request_checks - watched - excluded)
        self.assertEqual(
            [],
            unaccounted,
            f"{FLAKY_CHECK_NOTIFIER} neither watches nor excludes these "
            "pull-request checks, so a re-run of any of them files no issue: "
            + ", ".join(unaccounted),
        )
        both = sorted(watched & excluded)
        self.assertEqual([], both, "watched and excluded at once: " + ", ".join(both))
        stale = sorted(excluded - pull_request_checks)
        self.assertEqual([], stale, "excluded but no longer a pull-request check: " + ", ".join(stale))

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

    def test_the_release_stage_notifier_watches_every_release_pipeline(self):
        """A release pipeline missing from the watch list fails silently: its
        red run is a row in the Actions tab, the next scheduler tick skips,
        and nobody is told (gke-labs/kube-agents#1978). The notifier is a
        sweep rather than a `workflow_run` consumer, so the list lives in the
        script alone; this holds it to the roster above and to the workflows
        that exist, since a rename would otherwise red every sweep."""
        watched = tuple(_release_stage_notifier().WATCHED_WORKFLOWS)
        self.assertEqual(
            RELEASE_STAGE_WATCHED_WORKFLOWS,
            watched,
            f"{RELEASE_STAGE_NOTIFIER}'s script no longer watches the release pipelines named here",
        )
        documents = self._workflows()
        self.assertIn(RELEASE_STAGE_NOTIFIER, documents)
        display_names = {document.get("name") for document in documents.values() if document.get("name")}
        missing = sorted(set(watched) - display_names)
        self.assertEqual(
            [],
            missing,
            "watched release workflows that match no workflow name (renamed?): " + ", ".join(missing),
        )

    def test_the_release_stage_notifier_finds_the_eval_wait_and_its_withdrawal(self):
        """The notifier reads a promotion night red on the eval's RED verdict
        alone as green, by job name: the eval wait failed and the withdrawal
        was skipped, which the withdrawal's `if:` makes mean a settled
        verdict. A rename of either job, or a withdrawal that no longer keys
        on `settled`, would leave every RED night filing an issue again, with
        every unit test green on the recorded names."""
        notifier = _release_stage_notifier()
        documents = self._workflows()
        promotion = [document for document in documents.values() if document.get("name") == notifier.EVAL_PIPELINE]
        self.assertEqual(len(promotion), 1, f"no workflow named {notifier.EVAL_PIPELINE!r}")
        jobs = (promotion[0].get("jobs") or {}).values()
        eval_waits = [job for job in jobs if notifier.EVAL_WAIT_JOB_MARKER in (job.get("name") or "")]
        withdrawals = [job for job in jobs if notifier.WITHDRAWAL_JOB_MARKER in (job.get("name") or "")]
        self.assertEqual(len(eval_waits), 1, "EVAL_WAIT_JOB_MARKER selects no job, or more than one, in the promotion pipeline")
        self.assertEqual(len(withdrawals), 1, "WITHDRAWAL_JOB_MARKER selects no job, or more than one, in the promotion pipeline")
        self.assertIn("settled != 'true'", str(withdrawals[0].get("if", "")), "the withdrawal no longer keys on the verdict having settled")

    def test_the_release_stage_notifier_knows_every_compensation_job(self):
        """The jobs API carries neither `if:` nor `continue-on-error`, so the
        notifier tells a compensation job (a withdrawal that runs because the
        eval did not settle, a teardown, a rollback leg, a non-gating next-mode
        leg) from a gating stage by its name. The markers have to select
        exactly the jobs named above, and every job the workflow files mark as
        running after a failure or without failing the run has to be among
        them, or a renamed leg is named as the stage and holds its issue open
        for good."""
        markers = tuple(_release_stage_notifier().COMPENSATION_JOB_MARKERS)
        documents = self._workflows()
        watched = [document for document in documents.values() if document.get("name") in RELEASE_STAGE_WATCHED_WORKFLOWS]
        self.assertEqual(len(watched), len(RELEASE_STAGE_WATCHED_WORKFLOWS))
        selected, always_run = set(), set()
        for document in watched:
            for job in (document.get("jobs") or {}).values():
                name = (job or {}).get("name") or ""
                if any(marker in name for marker in markers):
                    selected.add(name)
                condition = str((job or {}).get("if", ""))
                if (job or {}).get("continue-on-error") is True or any(each in condition for each in RELEASE_STAGE_ALWAYS_RUN_CONDITIONS):
                    always_run.add(name)
        self.assertEqual(
            set(RELEASE_STAGE_COMPENSATION_JOBS),
            selected,
            "COMPENSATION_JOB_MARKERS selects a different set of jobs than RELEASE_STAGE_COMPENSATION_JOBS names",
        )
        unmarked = sorted(always_run - selected)
        self.assertEqual([], unmarked, "jobs that run after a failure or without failing the run, and no marker selects: " + ", ".join(unmarked))
        unused = [marker for marker in markers if not any(marker in name for name in selected)]
        self.assertEqual([], unused, "markers that select no job: " + ", ".join(unused))

    def test_the_required_python_job_runs_the_suite_in_strict_mode(self):
        """The gate is one flag on one line, and losing it fails open.

        `make coverage` tolerates a failing test directory by design -- it is a
        meter, and one red directory must not hide the number for the rest. The
        job that reports the required `Run Python Unit Tests` context joins the
        shards' results with `make coverage-merge`, so COVERAGE_STRICT=1 there
        is what makes a red file in any shard a red check. Drop it in a reformat and CI reports success on failing tests:
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

    SHARD_RESULT = "${{needs.test-shard.result}}"
    BENCH_RESULT = "${{needs.bench-tests.result}}"
    NOT_SUCCESS = ("failure", "cancelled", "skipped")

    @staticmethod
    def _expr(value):
        """A GitHub expression with its whitespace removed, so `${{ x }}` and `${{x}}` compare equal."""
        return "".join(str(value).split())

    def assert_join_fails_closed(self, workflow):
        """The required check is one join job that runs on every outcome, and its
        guard step exits non-zero whenever a shard or the bench job did not
        succeed. The guard is found by what it reads (both job results in its
        env) and judged by running its script as the runner does, not by the
        spelling of its comparisons."""
        self.assertNotIn("shell", (workflow.get("defaults") or {}).get("run") or {}, "the workflow sets a default shell")
        jobs = workflow["jobs"]
        named = [key for key, job in jobs.items() if job.get("name") == "Run Python Unit Tests"]
        self.assertEqual(named, ["test"], "the required check's name is not on exactly the join job")
        join = jobs["test"]
        needs = join.get("needs")
        needs = [needs] if isinstance(needs, str) else list(needs or [])
        self.assertTrue({"test-shard", "bench-tests"} <= set(needs), f"the join does not need both jobs: {needs}")
        self.assertEqual(self._expr(join.get("if", "")), "${{always()}}")
        self.assertNotIn("continue-on-error", join, "the join job is continue-on-error")
        self.assertNotIn("shell", (join.get("defaults") or {}).get("run") or {}, "the join sets a default shell")
        guards = [step for step in join["steps"] if self._result_names(step)]
        self.assertEqual(len(guards), 1, "the join has no single step that reads both job results")
        guard = guards[0]
        shards_name, bench_name = self._result_names(guard)
        self.assertEqual(self._expr(guard.get("if", "")), "${{always()}}")
        # The key's absence, not its value: `continue-on-error: ${{ true }}` is
        # a string to the YAML reader and true to GitHub. A step-level shell
        # would run the script through a template of its own choosing.
        self.assertNotIn("continue-on-error", guard, "the guard step is continue-on-error, so its red does not fail the check")
        self.assertNotIn("shell", guard, "the guard step sets its own shell")
        script = guard.get("run", "")
        self.assertEqual(self._guard_exit_status(script, {shards_name: "success", bench_name: "success"}), 0,
                         "the guard fails when every job succeeded")
        for outcome in self.NOT_SUCCESS:
            for env in ({shards_name: outcome, bench_name: "success"}, {shards_name: "success", bench_name: outcome}):
                self.assertNotEqual(self._guard_exit_status(script, env), 0, f"the guard passes on {env}")
        runs = " ".join(step.get("run", "") for step in join["steps"])
        self.assertIn("coverage-merge", runs)

    def _result_names(self, step):
        """The env names under which a step reads the shards' and the bench job's
        results, or None when it does not read exactly one of each."""
        env = step.get("env") or {}
        shards = [name for name, value in env.items() if self._expr(value) == self.SHARD_RESULT]
        bench = [name for name, value in env.items() if self._expr(value) == self.BENCH_RESULT]
        if len(shards) == 1 and len(bench) == 1:
            return shards[0], bench[0]
        return None

    @staticmethod
    def _guard_exit_status(script, env):
        """Runs the guard's script the way the runner runs a `run:` step that
        names no shell: written to a file and given to `bash -e` (the runner
        adds pipefail only when `shell: bash` is spelled out; `--noprofile
        --norc` keep this machine's startup files out). The body is an `echo`
        and an `exit`, so running it here is safe."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guard.sh"
            path.write_text(script)
            done = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", str(path)],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
                capture_output=True, text=True, timeout=30,
            )
        return done.returncode

    def assert_bench_job_is_hard(self, bench):
        """The bench job the join needs must be able to go red: nothing on it or
        its steps is continue-on-error, and one ungated step with the default
        shell runs exactly `make test-bench`, so a `|| true`, a soft step or a
        gated step cannot report success without the suite going green."""
        self.assertNotIn("continue-on-error", bench, "the bench job is continue-on-error")
        for step in bench["steps"]:
            self.assertNotIn("continue-on-error", step, f"a bench step is continue-on-error: {step.get('name')}")
        run_steps = [step for step in bench["steps"] if str(step.get("run", "")).strip() == "make test-bench"]
        self.assertEqual(len(run_steps), 1, f"no single bench step runs exactly `make test-bench`: {[str(s.get('run', '')).strip() for s in bench['steps']]}")
        self.assertNotIn("shell", run_steps[0], "the bench run step sets its own shell")
        self.assertNotIn("if", run_steps[0], "the bench run step is gated by an if, so the job can pass without running the suite")

    def test_the_required_python_check_fails_closed_on_its_shards(self):
        """The required check is a join of three shards and the bench job, and a skip is a pass.

        A job skipped because a job it needs failed reports success to a
        required check. So the join must run whatever the shards and the bench
        job did, a cancelled run included (`always()`), and must itself fail
        unless every shard and the bench job succeeded. The bench job is in the
        join because the merge queue re-tests the combined tree, and a bench red
        there held no merge while the job sat outside the required context.
        Each shard runs strict as well, so its own red is visible on its row,
        and the shard count is written once in the matrix and must match
        everywhere else it appears, or files drop out of every shard with the
        check still green.
        """
        yaml = _yaml()
        workflow = yaml.safe_load((WORKFLOWS / "python-tests.yml").read_text())
        jobs = workflow["jobs"]
        join, shards = jobs["test"], jobs["test-shard"]
        self.assert_join_fails_closed(workflow)
        # The pin must refuse a neutralised gate, not only a missing one. Two
        # spelling-free mutants, judged by the same executed helper: the soft
        # flag written as an expression, and a guard that always passes.
        guard_index = next(i for i, step in enumerate(join["steps"]) if self._result_names(step))
        for label, key, value in (("a guard soft by expression", "continue-on-error", "${{ true }}"), ("a guard that always passes", "run", "true")):
            mutant = copy.deepcopy(workflow)
            mutant["jobs"]["test"]["steps"][guard_index][key] = value
            with self.subTest(mutant=label), self.assertRaises(AssertionError):
                self.assert_join_fails_closed(mutant)
        # The bench job itself must stay able to go red, or the join's bench
        # branch guards nothing.
        self.assert_bench_job_is_hard(jobs["bench-tests"])
        shard_runs = " ".join(step.get("run", "") for step in shards["steps"])
        self.assertIn("COVERAGE_STRICT=1", shard_runs)
        # In the environment, not on make's command line, where it would also
        # travel in MAKEFLAGS into every make the tests start.
        self.assertNotIn("PYTHON_TEST_SHARD", shard_runs)
        count = len(shards["strategy"]["matrix"]["shard"])
        self.assertEqual(shards["strategy"]["matrix"]["shard"], list(range(1, count + 1)))
        shard_env = " ".join(str((step.get("env") or {}).get("PYTHON_TEST_SHARD", "")) for step in shards["steps"])
        self.assertIn(f"/{count}", shard_env, "a shard divides the sweep by another count than the matrix has")
        join_runs = " ".join(step.get("run", "") for step in join["steps"])
        self.assertIn(f"COVERAGE_SHARD_COUNT={count}", join_runs, "the join expects another shard count than the matrix has")
        self.assertFalse(shards["strategy"].get("fail-fast", True), "one red shard would cancel the others")

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


class PoolPressureJobOwnersContractTest(unittest.TestCase):
    """pool_pressure.py's BOSKOS_JOB_OWNERS copies the fixed Boskos owners the
    hack/ scripts lease under. A rename there and not here would move that
    job's leases under "no build ID in the owner" with nothing red."""

    @staticmethod
    def _script(name):
        import importlib.util

        hack = REPO_SCRIPTS.parent / "hack"
        spec = importlib.util.spec_from_file_location(f"job_owner_{name}", hack / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        # The scripts import their hack/ siblings by bare name.
        sys.path.insert(0, str(hack))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(hack))
        return module

    def test_the_fixed_job_owners_are_the_scripts_defaults(self):
        # Discovered, not enumerated: every hack/ script that leases through
        # boskos_pool and names a default owner is in the tuple, so the next
        # such script goes red here rather than into "no build ID in the owner".
        sys.path.insert(0, str(REPO_SCRIPTS))
        import pool_pressure

        owners = set()
        for path in sorted((REPO_SCRIPTS.parent / "hack").glob("*.py")):
            if not re.search(r"^\s*(?:import|from)\s+boskos_pool\b", path.read_text(encoding="utf-8"), re.M):
                continue
            script = self._script(path.stem)
            for name in ("DEFAULT_OWNER", "DEFAULT_BOSKOS_OWNER"):
                if hasattr(script, name):
                    owners.add(getattr(script, name))
        self.assertEqual(
            {"fleet-reconcile", "ci-kube-agents-compute-sweep", "ci-kube-agents-pull-sweep"},
            owners,
            "the scripts this test expects to find; a new one is added to both sets",
        )
        self.assertEqual(owners, set(pool_pressure.BOSKOS_JOB_OWNERS))


class PoolPressureArtifactContractTest(unittest.TestCase):
    """The CI-health bot's rule-8 note against the artifact it reads (#1607).

    Nothing imports pool_pressure.py: health.py takes its verdict, cause and
    numbers as given, post_health.py copies its cause labels. A rename there
    raises nothing here, so this replays the periodic over its captured breach
    day and joins the two sides on the result.
    """

    def _modules(self):
        sys.path.insert(0, str(REPO_SCRIPTS))
        import pool_pressure
        from eval_dashboard import health, post_health

        return pool_pressure, health, post_health

    def _artifact(self, pool_pressure):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            pool_pressure.measure(
                from_dir=str(POOL_PRESSURE_FIXTURE),
                as_of=POOL_PRESSURE_AS_OF,
                window_days=POOL_PRESSURE_WINDOW_DAYS,
                as_json=True,
            )
        return json.loads(buffer.getvalue())

    def test_the_cause_labels_post_health_branches_on_are_the_periodics(self):
        pool_pressure, _, post_health = self._modules()
        self.assertEqual(
            (pool_pressure.CAUSE_CAPACITY, pool_pressure.CAUSE_CONCURRENCY_CAP, pool_pressure.CAUSE_CONTROL_PLANE),
            (post_health.CAUSE_CAPACITY, post_health.CAUSE_CONCURRENCY_CAP, post_health.CAUSE_CONTROL_PLANE),
            "the copy has drifted: the message names no remedy for a cause the periodic still emits",
        )
        # UNKNOWN is not copied, nor is a label added later; both must reach
        # the fallback message rather than one of the three remedies.
        for cause in (pool_pressure.CAUSE_UNKNOWN, "SOMETHING_NEW"):
            self.assertIn("cause unclear", post_health.pool_cause_text({"cause": cause}), cause)

    def test_the_verdicts_health_reads_are_the_periodics(self):
        pool_pressure, health, _ = self._modules()
        self.assertEqual(pool_pressure.VERDICT_BREACH, health.POOL_BREACH)
        self.assertEqual(pool_pressure.VERDICT_UNMEASURED, health.POOL_UNMEASURED)
        # STALE is health.py's own word: an artifact cannot report that it
        # stopped being written, so the note overrides the verdict it carries.
        # A periodic emitting STALE would make that override look like agreement.
        self.assertNotIn(
            health.POOL_STALE,
            (pool_pressure.VERDICT_OK, pool_pressure.VERDICT_BREACH, pool_pressure.VERDICT_UNMEASURED),
        )

    def test_the_note_quotes_the_periodics_own_numbers(self):
        """Every figure in the note, against the field it came from.

        The fixture breached on p95 alone and had two runs queued at the time,
        so it covers both halves of the breach rule. Its window is one day
        long, so the day's row and the seven-day aggregate hold the same
        numbers here: which of the two the note quotes is pinned in
        test_eval_dashboard_health.py, not here.
        """
        pool_pressure, health, _ = self._modules()
        doc = self._artifact(pool_pressure)
        self.assertEqual(pool_pressure.VERDICT_BREACH, doc["verdict"], "the fixture is the captured breach day")
        day, = doc["trend"]["days"]
        note = health.pool_note(doc, POOL_PRESSURE_AS_OF, None)
        self.assertEqual(
            {
                "verdict": doc["verdict"],
                "cause": doc["cause"],
                "day": day["day"],
                # The capture predates the `recent` block, which is also what
                # the first tick after a deploy reads: the note falls back to
                # the worst day rather than losing its numbers.
                "window_hours": None,
                "p50_s": int(day["p50_minutes"] * 60),
                "p95_s": int(day["p95_minutes"] * 60),
                "breach_seen": True,
                # The captured queue's longest wait: 150 minutes, the run that
                # also makes over_threshold non-zero.
                "waiting_longest_s": int(max(r["minutes"] for r in doc["queue"]["waiting_runs"]) * 60),
                # That 150 minutes against the captured 15 minute p50 limit,
                # and the same 150 minutes back from the reading dates the jam.
                "waiting_now": True,
                "waiting_since": health.iso(
                    health.parse_iso(doc["window_end"]) - timedelta(minutes=max(r["minutes"] for r in doc["queue"]["waiting_runs"]))
                ),
                "over_threshold": doc["queue"]["over_threshold"],
                "threshold_p50_s": int(doc["thresholds"]["p50_minutes"] * 60),
                "threshold_p95_s": int(doc["thresholds"]["p95_minutes"] * 60),
                "free": doc["pool"]["free"],
                "total": doc["pool"]["total"],
                "lease_failures": doc["recent"]["lease_failures"],
                "lease_failures_hours": doc["recent"]["hours"],
                "no_build_id": sum(doc["no_build_id"].values()),
                "max_concurrency": doc["max_concurrency"],
            },
            {key: note[key] for key in note if key not in ("since", "measured_at")},
        )
        self.assertEqual(health.parse_iso(doc["window_end"]), health.parse_iso(note["measured_at"]))
        # And once the periodic writes the block, the note quotes it instead:
        # the same keys, read from `recent` rather than the day's row.
        doc["recent"] = {"hours": pool_pressure.RECENT_WINDOW_HOURS, "runs": 31, "judged": True,
                         "p50_minutes": 18.0, "p95_minutes": 52.0, "worst_minutes": 61.0}
        fresh = health.pool_note(doc, POOL_PRESSURE_AS_OF, None)
        self.assertEqual(fresh["window_hours"], pool_pressure.RECENT_WINDOW_HOURS)
        self.assertIsNone(fresh["day"])
        self.assertEqual(fresh["p50_s"], 1080)
        # The digest reads the newest day's row, not the seven-day aggregate.
        self.assertEqual(int(day["p50_minutes"] * 60), health.pool_wait_p50_s(doc, POOL_PRESSURE_AS_OF))

    def test_a_field_the_periodic_stops_writing_is_not_quietly_zero(self):
        """A renamed field reads as absent, and absent must not print as a
        number: "0 of 30 projects free" is a sentence the reader believes."""
        pool_pressure, health, post_health = self._modules()
        doc = self._artifact(pool_pressure)
        doc["pool"].pop("free")
        doc["queue"].pop("over_threshold")
        note = health.pool_note(doc, POOL_PRESSURE_AS_OF, None)
        self.assertIsNone(note["free"])
        self.assertEqual(0, note["over_threshold"], "no live queue is a real reading of zero")
        # The count only reaches a message under CAUSE_CONTROL_PLANE; the other
        # three never print it, so the fixture's own cause proves nothing.
        note["cause"] = post_health.CAUSE_CONTROL_PLANE
        rendered = post_health.render_pool({"pool": note})
        self.assertIn("? of", rendered)
        self.assertNotIn("0 of", rendered)


if __name__ == "__main__":
    unittest.main()

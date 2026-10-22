"""Pins that the mirrored gke-* skills #2996 and #2998 swept teach no write path.

    python3 -m unittest tests/test_skill_read_only_text.py

Each profile's config offers the agent only the `gke` MCP server's read tools
(#2955), and the broker refuses `kubectl` and `gcloud` writes, so a change goes
out as a pull request. `make skills-check` holds each shipped skill equal to its
upstream copy plus overlay, which stays green on any text once the overlay is
regenerated, so the next `make skills-sync` could bring a write tool or a write
command back from `google/skills` (written for a user with write access) and
ship it. This reads the shipped text instead:

- every tool named on a `**MCP Tools:**` line is in the `gke` server's
  `tools.include` in deploy/shared/defaults/config.yaml, parsed rather than
  copied, so a tool the list drops is caught here too (any `server:` prefix,
  as `gke-observability` writes `gke:` and upstream writes `k8s:`, is stripped
  first);
- no file calls an MCP write tool (`patch_k8s_resource(`, `create_cluster(`);
- no shell or unlabelled fence runs one of the `kubectl`, `gcloud` and `helm`
  writes `WRITE_COMMAND_RE` names (a denylist: a verb or spelling it does not
  list passes, so widen it when a sweep removes a new one; see #3483).

One write is allowed: `gke-upgrades` writes a node-pool version change as the
exact `gcloud container clusters upgrade --node-pool` command for the platform
operator to run (an operation, not desired state, so it has no pull request).
A fence counts as that step only when the paragraph right before it says it is
"a step for the platform operator", and then only a `clusters upgrade` with
`--node-pool` on the same command passes; its comment lines are skipped, and any
other write in it, a control-plane upgrade included, still fails.

The skills are the eleven #2996 swept and the ten #2998 swept.
`gke-cluster-creation` is #2937's and joins SWEPT_SKILLS when its sweep lands.
"""

import re
import unittest
from pathlib import Path

import yaml
from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS = REPO_ROOT / "agents/platform/skills"
CONFIG = REPO_ROOT / "deploy/shared/defaults/config.yaml"

SWEPT_SKILLS = (
    "gke-app-onboarding",
    "gke-basics",
    "gke-batch-hpc",
    "gke-cost-optimization",
    "gke-golden-path",
    "gke-inference",
    "gke-multitenancy",
    "gke-networking",
    "gke-platform-security",
    "gke-reliability",
    "gke-storage",
    # #2998
    "gke-backup-dr",
    "gke-cluster-autoscaler",
    "gke-compute-classes",
    "gke-cost-analysis",
    "gke-observability",
    "gke-tpu-dynamic-slices-monitoring",
    "gke-upgrades",
    "gke-workload-identity",
    "gke-workload-scaling",
    "gke-workload-security",
)

# A `> **MCP Tools:**` line and its blockquote continuation, up to the first
# empty quote line or the end of the quote.
MCP_TOOLS_RE = re.compile(
    r"^> \*\*MCP Tools:\*\*(.*(?:\n>[^\n]*\S[^\n]*)*)", re.MULTILINE
)
# Every backticked token is taken whole and its `server:` prefix stripped
# afterwards, so a tool behind any prefix (upstream writes
# `k8s:apply_k8s_manifest`), or one in a shape no pattern expects, still reaches
# the comparison with the list and fails there rather than being skipped.
BACKTICKED_RE = re.compile(r"`([^`\s]+)`")


def tool_names(text: str) -> set[str]:
    return {token.rpartition(":")[2] for token in BACKTICKED_RE.findall(text)}


WRITE_TOOL_CALL_RE = re.compile(
    r"\b(?:create|update|delete|patch|apply|scale)_[a-z0-9_]+\("
)
WRITE_COMMAND_RE = re.compile(
    r"\bkubectl\s+(?:apply|patch|scale|delete|edit|create|replace|set|label|annotate|drain|run)\b"
    r"|\bgcloud\s+container\s+(?:clusters|node-pools)\s+(?:create|create-auto|update|delete|resize|upgrade)\b"
    r"|\bgcloud\s+projects\s+add-iam-policy-binding\b"
    r"|\bhelm\s+install\b"
)
SHELL_FENCES = {"", "bash", "sh", "shell", "console", "zsh"}
# The gke-upgrades marker: the paragraph right before the fence says so.
OPERATOR_STEP_MARKER = "a step for the platform operator"
OPERATOR_STEP_WRITE_RE = re.compile(r"\bgcloud\s+container\s+clusters\s+upgrade\b")


def read_tools() -> set[str]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return set(config["mcp_servers"]["gke"]["tools"]["include"])


def fence_writes(content: str, preceding: str) -> list[str]:
    """The writes a shell fence runs, less a marked platform operator step."""
    if OPERATOR_STEP_MARKER not in preceding.lower():
        return [m.group(0) for m in WRITE_COMMAND_RE.finditer(content)]
    commands = "\n".join(
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    ).replace("\\\n", " ")
    writes = []
    for m in WRITE_COMMAND_RE.finditer(commands):
        # Only a node-pool upgrade is the operator's step; a control-plane
        # upgrade is a pull request on spec.minMasterVersion.
        end = commands.find("\n", m.end())
        rest = commands[m.end() : end if end != -1 else len(commands)]
        if OPERATOR_STEP_WRITE_RE.fullmatch(m.group(0)) and "--node-pool" in rest:
            continue
        writes.append(m.group(0))
    return writes


def shell_fences(text: str):
    """Yield (token, preceding paragraph text) for each shell fence."""
    tokens = MarkdownIt().parse(text)
    for i, token in enumerate(tokens):
        if (
            token.type != "fence"
            or token.info.strip().split(" ")[0] not in SHELL_FENCES
        ):
            continue
        preceding = ""
        if i >= 2 and tokens[i - 1].type == "paragraph_close":
            preceding = tokens[i - 2].content
        yield token, preceding


def skill_files():
    for skill in SWEPT_SKILLS:
        for path in sorted((SKILLS / skill).rglob("*.md")):
            yield path, path.read_text(encoding="utf-8")


class SweptSkillsTeachNoWritesTest(unittest.TestCase):
    def test_the_swept_skills_exist(self):
        for skill in SWEPT_SKILLS:
            self.assertTrue((SKILLS / skill / "SKILL.md").is_file(), skill)

    def test_mcp_tools_lines_name_only_offered_tools(self):
        offered = read_tools()
        self.assertIn("get_k8s_resource", offered)  # the parse found the list
        lines = 0
        for path, text in skill_files():
            for match in MCP_TOOLS_RE.finditer(text):
                lines += 1
                names = tool_names(match.group(1)) - {"gke"}
                with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                    self.assertTrue(names, "an MCP Tools line with no tool names")
                    self.assertLessEqual(
                        names,
                        offered,
                        f"not in the gke server's tools.include: {sorted(names - offered)}",
                    )
        # Eleven swept SKILL.md files and core-concepts.md carry one; a regex
        # that stopped matching would otherwise pass on nothing.
        self.assertGreaterEqual(lines, 12)

    def test_no_mcp_write_tool_is_called(self):
        for path, text in skill_files():
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                self.assertEqual(WRITE_TOOL_CALL_RE.findall(text), [])

    def test_no_shell_fence_runs_a_write(self):
        fences = operator_steps = 0
        for path, text in skill_files():
            for token, preceding in shell_fences(text):
                fences += 1
                if OPERATOR_STEP_MARKER in preceding.lower():
                    operator_steps += 1
                with self.subTest(
                    path=str(path.relative_to(REPO_ROOT)), line=token.map[0] + 1
                ):
                    self.assertEqual(
                        fence_writes(token.content, preceding), [], token.content
                    )
        self.assertGreater(fences, 0)
        # gke-upgrades' three node-pool version steps; the allowance is no
        # wider than they need.
        self.assertEqual(operator_steps, 3)


class FenceWritesTest(unittest.TestCase):
    """Each pattern and allowance, on fixtures rather than shipped skills."""

    OPERATOR = "A step for the platform operator, not a pull request."
    UPGRADE = (
        "# Platform operator step\n"
        "gcloud container clusters upgrade CLUSTER_NAME \\\n"
        "  --node-pool NODE_POOL_NAME --cluster-version VERSION\n"
    )

    def test_mcp_tools_line_accepts_the_gke_prefix(self):
        text = "> **MCP Tools:** `gke:get_cluster`, `gke:list_k8s_events`\n"
        names = tool_names(MCP_TOOLS_RE.search(text).group(1))
        self.assertEqual(names, {"get_cluster", "list_k8s_events"})
        self.assertLessEqual(names, read_tools())

    def test_mcp_tools_line_prefix_does_not_hide_a_write_tool(self):
        text = "> **MCP Tools:** `gke:get_cluster`, `gke:patch_k8s_resource`\n"
        names = tool_names(MCP_TOOLS_RE.search(text).group(1))
        self.assertEqual(names - read_tools(), {"patch_k8s_resource"})

    def test_mcp_tools_line_any_prefix_does_not_hide_a_write_tool(self):
        text = "> **MCP Tools:** `gke:get_cluster`, `k8s:apply_k8s_manifest`\n"
        names = tool_names(MCP_TOOLS_RE.search(text).group(1))
        self.assertEqual(names - read_tools(), {"apply_k8s_manifest"})

    def test_mcp_tools_line_unparseable_token_fails_rather_than_vanishing(self):
        text = "> **MCP Tools:** `gke:get_cluster`, `gke:Patch_K8s_Resource`\n"
        names = tool_names(MCP_TOOLS_RE.search(text).group(1))
        self.assertEqual(names - read_tools(), {"Patch_K8s_Resource"})

    def test_marked_operator_step_allows_clusters_upgrade(self):
        self.assertEqual(fence_writes(self.UPGRADE, self.OPERATOR), [])

    def test_unmarked_fence_still_refuses_clusters_upgrade(self):
        self.assertEqual(
            fence_writes(self.UPGRADE, ""), ["gcloud container clusters upgrade"]
        )

    def test_marked_operator_step_refuses_a_control_plane_upgrade(self):
        # Beside a node-pool upgrade, so the --node-pool search must stop at the
        # end of its own command.
        fence = (
            self.UPGRADE + "gcloud container clusters upgrade CLUSTER_NAME --master\n"
        )
        self.assertEqual(
            fence_writes(fence, self.OPERATOR), ["gcloud container clusters upgrade"]
        )

    def test_marked_operator_step_still_refuses_clusters_update(self):
        fence = self.UPGRADE + "gcloud container clusters update CLUSTER_NAME\n"
        self.assertEqual(
            fence_writes(fence, self.OPERATOR), ["gcloud container clusters update"]
        )

    def test_widened_writes(self):
        for command in (
            "gcloud projects add-iam-policy-binding PROJECT_ID --member=M --role=R",
            "kubectl drain NODE --ignore-daemonsets",
            "kubectl run debug --image=busybox -- sleep 3600",
            "helm install RELEASE CHART -n NAMESPACE",
        ):
            with self.subTest(command=command):
                self.assertNotEqual(fence_writes(command + "\n", ""), [])

    def test_neighbouring_reads_pass(self):
        for command in (
            "gcloud projects get-iam-policy PROJECT_ID",
            "kubectl describe node NODE",
            "kubectl get pods --field-selector=status.phase=Running",
            "helm list -n NAMESPACE",
        ):
            with self.subTest(command=command):
                self.assertEqual(fence_writes(command + "\n", ""), [])


if __name__ == "__main__":
    unittest.main()

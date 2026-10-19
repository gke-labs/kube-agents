"""Pins that the mirrored gke-* skills #2996 swept teach no write path.

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
  copied, so a tool the list drops is caught here too;
- no file calls an MCP write tool (`patch_k8s_resource(`, `create_cluster(`);
- no shell or unlabelled fence runs a `kubectl` or `gcloud container` write.

The skills are the eleven #2996 swept. `gke-cluster-creation` is #2937's and
the other mirrored skills are #2998's; each joins SWEPT_SKILLS when its sweep
lands.
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
)

# A `> **MCP Tools:**` line and its blockquote continuation, up to the first
# empty quote line or the end of the quote.
MCP_TOOLS_RE = re.compile(
    r"^> \*\*MCP Tools:\*\*(.*(?:\n>[^\n]*\S[^\n]*)*)", re.MULTILINE
)
BACKTICKED_RE = re.compile(r"`([a-z][a-z0-9_]*)`")
WRITE_TOOL_CALL_RE = re.compile(
    r"\b(?:create|update|delete|patch|apply|scale)_[a-z0-9_]+\("
)
WRITE_COMMAND_RE = re.compile(
    r"\bkubectl\s+(?:apply|patch|scale|delete|edit|create|replace|set|label|annotate)\b"
    r"|\bgcloud\s+container\s+(?:clusters|node-pools)\s+(?:create|create-auto|update|delete|resize|upgrade)\b"
)
SHELL_FENCES = {"", "bash", "sh", "shell", "console", "zsh"}


def read_tools() -> set[str]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return set(config["mcp_servers"]["gke"]["tools"]["include"])


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
                names = set(BACKTICKED_RE.findall(match.group(1))) - {"gke"}
                with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                    self.assertTrue(names, "an MCP Tools line with no tool names")
                    self.assertLessEqual(
                        names,
                        offered,
                        f"not in the gke server's tools.include: {sorted(names - offered)}",
                    )
        # Ten swept SKILL.md files and core-concepts.md carry one; a regex that
        # stopped matching would otherwise pass on nothing.
        self.assertGreaterEqual(lines, 11)

    def test_no_mcp_write_tool_is_called(self):
        for path, text in skill_files():
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                self.assertEqual(WRITE_TOOL_CALL_RE.findall(text), [])

    def test_no_shell_fence_runs_a_write(self):
        md = MarkdownIt()
        fences = 0
        for path, text in skill_files():
            for token in md.parse(text):
                if (
                    token.type != "fence"
                    or token.info.strip().split(" ")[0] not in SHELL_FENCES
                ):
                    continue
                fences += 1
                with self.subTest(
                    path=str(path.relative_to(REPO_ROOT)), line=token.map[0] + 1
                ):
                    self.assertIsNone(
                        WRITE_COMMAND_RE.search(token.content), token.content
                    )
        self.assertGreater(fences, 0)


if __name__ == "__main__":
    unittest.main()

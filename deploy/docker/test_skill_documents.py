"""Tests that the skill size and command checks read the same skill documents.

Run: python3 -m unittest discover -s deploy/docker -p 'test_skill_documents.py'
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import check_skill_commands
from skill_documents import shipped_skill_documents, skill_documents

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts import check_skill_size  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "test_skill_inline_commands", REPO_ROOT / "tests" / "test_skill_inline_commands.py"
)
inline_commands = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inline_commands)

# A shell block for the Tirith check and a refused inline span for the inline test.
SKILL_TEXT = "```bash\nls\n```\n\nRun `$G add x`.\n"


class SkillDocumentsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKILL_TEXT, encoding="utf-8")
        return path

    def test_a_skill_holds_every_markdown_file_but_its_scripts(self):
        tree = self.repo / "agents/platform/skills"
        documents = [
            self.write("agents/platform/skills/s/SKILL.md"),
            self.write("agents/platform/skills/s/assets/template.md"),
            self.write("agents/platform/skills/s/references/SKILL.md"),
            self.write("agents/platform/skills/s/references/step.md"),
        ]
        self.write("agents/platform/skills/s/scripts/README.md")
        self.write("agents/platform/skills/s/scripts/fixtures/case.md")
        self.write("agents/platform/skills/notes/loose.md")
        self.assertEqual(sorted(documents), skill_documents(tree))

    def test_a_skill_under_a_category_directory_counts(self):
        skill = self.write("agents/platform/skills/category/s/SKILL.md")
        self.assertEqual([skill], skill_documents(self.repo / "agents/platform/skills"))

    def test_a_skill_md_under_scripts_is_neither_a_skill_nor_a_document(self):
        skill = self.write("agents/platform/skills/s/SKILL.md")
        self.write("agents/platform/skills/s/scripts/x/SKILL.md")
        self.write("agents/platform/skills/s/scripts/x/references/step.md")
        self.assertEqual([skill], skill_documents(self.repo / "agents/platform/skills"))

    def test_every_shipped_root_is_read_and_others_are_not(self):
        shipped = [
            self.write("agents/cluster/skills/a/SKILL.md"),
            self.write("a2a/persona/platform/skills/b/SKILL.md"),
            self.write("agentplugins/p/files/skills/c/SKILL.md"),
        ]
        self.write(".agents/skills/d/SKILL.md")
        self.write("third_party/google-skills/e/SKILL.md")
        self.assertEqual(sorted(shipped), sorted(shipped_skill_documents(self.repo)))

    def test_every_check_reads_the_shared_documents(self):
        for rel in (
            "agents/platform/skills/s/SKILL.md",
            "agents/platform/skills/s/assets/template.md",
            "agents/platform/skills/s/references/step.md",
            "agents/platform/skills/category/t/SKILL.md",
            "agents/platform/skills/s/scripts/README.md",
            "agentplugins/p/files/skills/c/SKILL.md",
        ):
            self.write(rel)
        expected = shipped_skill_documents(self.repo)
        tree = self.repo / "agents/platform/skills"
        tirith = [Path(block.path) for block in check_skill_commands.skill_blocks(str(tree), tree)]
        self.assertEqual(skill_documents(tree), tirith)
        self.assertEqual(expected, [doc for doc, _ in check_skill_size.oversized(self.repo, limit=0)])
        self.assertEqual(expected, [doc for doc, _, _ in inline_commands.refused_spans(self.repo)])


if __name__ == "__main__":
    unittest.main()

"""The frontmatter of every shipped skill loads with a YAML parser.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest plus PyYAML, matching the other suites in this directory.

An agent chooses a skill by the `name` and `description` in the frontmatter
of its `SKILL.md`. Hermes reads that block with `parse_frontmatter`
(`agent/skill_utils.py` at the pinned tag): it parses the block as YAML, and
when the parser raises it splits each line at its first colon. That second
reader takes a one-line `description` whole, so a description with an unquoted
`: ` in it, which YAML rejects, loaded all the same (#5867).

The second reader is not a parser. A folded or nested value does not survive
it, and a later loader or a repository tool need not have a second reader at
all. So every shipped skill is held to the parser here: the block is YAML, a
mapping, and its `name` and `description` are text.

The skills are the ones `deploy/docker/skill_documents.py` calls shipped, the
same set the size and command checks read. The repository's own skills under
`.agents/skills/` have this check in `test_skill_discovery.py`.
"""

import pathlib
import re
import sys
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "deploy" / "docker"))

from skill_documents import SKILL_FILE, shipped_trees, skill_dirs  # noqa: E402

#: The fenced YAML a `SKILL.md` opens with.
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\s*?\n", re.S)

#: The fields an agent chooses a skill by.
REQUIRED_TEXT_FIELDS = ("name", "description")


def frontmatter_problem(text):
    """Why the frontmatter of a `SKILL.md` with this text does not load; "" when it loads."""
    match = FRONTMATTER.match(text)
    if not match:
        return "no `---` frontmatter block at the top of the file"
    try:
        block = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return f"frontmatter is not valid YAML: {exc}"
    if not isinstance(block, dict):
        return f"frontmatter parses to {type(block).__name__}, not a mapping"
    for field in REQUIRED_TEXT_FIELDS:
        value = block.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"frontmatter has no text for `{field}`"
    return ""


def shipped_skill_files():
    """The `SKILL.md` of every shipped skill."""
    return [skill / SKILL_FILE for tree in shipped_trees(REPO_ROOT) for skill in skill_dirs(tree)]


class ShippedSkillFrontmatterTest(unittest.TestCase):
    def test_every_shipped_skill_has_frontmatter_a_yaml_parser_loads(self):
        for skill_md in shipped_skill_files():
            with self.subTest(skill=str(skill_md.relative_to(REPO_ROOT))):
                self.assertEqual(frontmatter_problem(skill_md.read_text(encoding="utf-8")), "")

    def test_every_shipped_tree_holds_a_skill(self):
        # A glob that stops matching would pass the test above with its skills unread.
        trees = shipped_trees(REPO_ROOT)
        self.assertTrue(trees)
        for tree in trees:
            with self.subTest(tree=str(tree.relative_to(REPO_ROOT))):
                self.assertTrue(skill_dirs(tree))


class FrontmatterProblemTest(unittest.TestCase):
    def _problem(self, block):
        return frontmatter_problem(f"---\n{block}\n---\n\n# body\n")

    def test_a_block_that_loads_has_no_problem(self):
        self.assertEqual(self._problem("name: x\ndescription: Reads a report."), "")
        self.assertEqual(self._problem("name: x\ndescription: >-\n  Never for an audit: a run collects."), "")
        self.assertEqual(self._problem('name: x\ndescription: "Never for an audit: a run collects."'), "")

    def test_an_unquoted_colon_and_space_in_a_description_is_a_problem(self):
        self.assertIn("not valid YAML", self._problem("name: x\ndescription: Never for an audit: a run collects."))

    def test_a_block_that_is_no_mapping_is_a_problem(self):
        self.assertIn("not a mapping", self._problem("- name: x"))

    def test_a_missing_or_empty_field_is_a_problem(self):
        self.assertIn("`description`", self._problem("name: x"))
        self.assertIn("`description`", self._problem("name: x\ndescription:"))
        self.assertIn("`name`", self._problem("description: Reads a report."))
        self.assertIn("`description`", self._problem("name: x\ndescription:\n  - a list"))

    def test_a_file_with_no_block_is_a_problem(self):
        self.assertIn("no `---` frontmatter", frontmatter_problem("# body\n"))


if __name__ == "__main__":
    unittest.main()

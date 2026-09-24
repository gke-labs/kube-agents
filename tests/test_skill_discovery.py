"""The repository's own skills are discoverable by the harness a reader opens it in.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest plus PyYAML, matching the other suites in this directory.

A user who asks an assistant "install kube-agents into my GCP project" gets one
of two things. Either the harness finds `.agents/skills/install-kube-agents/`
and follows a procedure pinned to a release tag, or the model answers from
whatever it absorbed about open-source installers and invents a plausible URL.
#1332 is the second outcome, observed: a fabricated
`gke-labs.github.io/.../install.sh`, the namespace `agent-system`, and a model
version this project has never shipped. That session had no checkout open, so
nothing here would have saved it; what this file covers is the case where the
checkout *is* there and the skills are still not found.

Two mechanisms stand between those outcomes, and nothing checked either one.

The first is where the skills sit. Claude Code reads `.claude/skills/`, and
#1471 made that a symlink to `.agents/` rather than a second copy. A symlink is
one dropped `.gitignore` negation away from being absent, one `git mv` away from
being an ordinary directory that drifts, or an absolute path that resolves only
on the machine that wrote it, and each failure is invisible: the repository
still contains every skill, so nothing looks wrong until an assistant in a fresh
clone reports it has none. That is the half of the premise this repository
controls and the only half asserted below. The
other half is that `.agents/skills/` is itself a project skill path -- Codex
and Gemini CLI both document it, and Jetski reads it directly -- which is a
property of those harnesses, cannot be checked from this tree, and is stated
here so a reader knows it rests on their documentation rather than on a test.

The second mechanism is the frontmatter. Every harness above loads skill names
and descriptions at session start and the full `SKILL.md` only once one of
them matches the request -- the body cannot be consulted to decide whether to
consult the body. Two ways that fails silently:

* the block stops being loadable. A description holding `: ` or opening with
  `{`, `[`, `*` or `&` is a YAML error, and a harness that cannot parse the
  frontmatter has no skill -- while a line-scanning reader of the same file
  sees a perfectly good description. So the block is parsed here the way a
  harness parses it, with `yaml.safe_load`, and not with a regex that would
  wave the broken case through;
* the words stop matching the request. `Discovers and removes provisioned
  kube-agents GCP/GKE infrastructure` was the uninstall skill's description
  until the commit that added this file: an accurate sentence about the skill
  that never says "uninstall".

  How much that second one costs is worth stating precisely, because it was
  measured and the answer was "not as much as you would think". Asked to "tear
  kube-agents out of that project", a fresh session picked the uninstall skill
  from that description anyway -- the directory name is `uninstall-kube-agents`
  and is loaded beside it, and the name carried the match. The same held for
  all three lifecycle skills under both wordings. So the floor below is not
  what makes these skills reachable today, and this file would be dishonest to
  claim otherwise. What it holds is the pair: a name and a description that
  degrade together leave nothing, and the name is the half most likely to be
  kept while a description is rewritten. It is a floor and not a phrasebook --
  everything after the first word is free -- because a test that pins whole
  sentences is a test that gets deleted the first time someone improves one.

Scope is this repository's own skills, under `.agents/skills/`. The skills
baked into the agent images (`agents/<profile>/skills/`) are loaded by Hermes
from a profile home, not discovered from a checkout, and
`scripts/check_prompt_assets.py` already holds their manifests -- though with
a line-scanning reader rather than a YAML one, so the loadability check below
has no counterpart on the skills that ship. Closing that is a change to a gate
covering fifty-odd bundles and turns on whether Hermes rejects the same block,
which needs a running image to answer; it is not this suite's to make.
Nothing reads the frontmatter here: `scripts/generate_docs.py` builds the
published skill catalogue from `agents/platform/skills` and
`agents/cluster/skills` alone, so these blocks reach no generated page and
this suite is their only reader inside the repository.
"""

import os
import pathlib
import re
import tempfile
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / ".agents" / "skills"

#: A frontmatter block: the fenced YAML a `SKILL.md` opens with.
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\s*?\n", re.S)

#: Directories a harness reads, and the `.agents/` directory each must resolve
#: to. Claude Code is the one that needs them; the harnesses that read
#: `.agents/skills/` as a project skill path need nothing here.
CLAUDE_LINKS = {
    ".claude/skills": ".agents/skills",
    ".claude/rules": ".agents/rules",
}

#: The action verb each lifecycle skill's description has to open with, matched
#: as a prefix of the first word. Held at the opening rather than as a
#: substring anywhere, for two reasons: a description that leads with the
#: action is the one that matches how a request is phrased, and "install" is a
#: substring of "uninstall", so the looser check lets either skill stand in for
#: the other.
LIFECYCLE_ACTIONS = {
    "install-kube-agents": "install",
    "uninstall-kube-agents": "uninstall",
    "upgrade-kube-agents": "upgrade",
}

#: The product as a user names it. "the Kubernetes Agentic Harness" is the same
#: thing and does not match a request that says kube-agents.
PRODUCT = "kube-agents"


def _skill_dirs():
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def _frontmatter(skill_md):
    """The parsed frontmatter mapping, or a reason it is not one."""
    match = FRONTMATTER.match(skill_md.read_text(encoding="utf-8"))
    if not match:
        return None, "no `---` frontmatter block at the top of the file"
    try:
        block = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, f"frontmatter is not valid YAML, so no harness can load it: {exc}"
    if not isinstance(block, dict):
        return None, f"frontmatter parses to {type(block).__name__}, not a mapping"
    return block, ""


def _description(skill_md):
    """The description, and why there is none when there is none.

    The reason travels with the text because the classes below sort after
    `SkillFrontmatterTest` only by accident of naming: without it, a skill
    whose frontmatter does not parse is first reported as a description
    opening with the wrong word, which points the reader at the wrong line.

    `or ""` rather than a default, because a present key holding nothing is
    not a missing key. `description:` with nothing after it -- or its `~` and
    `null` spellings, the shape a skill carries while someone is still
    drafting it -- puts `None` in the mapping, so `get(…, "")` returns `None`
    and `str(None)` is the four truthy characters `None`. The skill then has
    no description and passes the check that exists to say so.
    """
    block, why = _frontmatter(skill_md)
    if block is None:
        return "", why
    return str(block.get("description") or ""), ""


class DescriptionReadingTest(unittest.TestCase):
    """The reader the guards are only as good as.

    Every assertion below this class asks `_description` what a skill says,
    so a shape it mis-reads is a shape they all wave through. `description:`
    holding nothing did exactly that: it arrived as the string `None`, which
    is truthy, and `test_every_skill_declares_a_description` passed on a
    skill that has none.
    """

    def _description_of(self, frontmatter):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "SKILL.md"
            path.write_text(f"---\n{frontmatter}---\n\n# body\n", encoding="utf-8")
            return _description(path)[0]

    def test_a_description_with_no_value_reads_as_absent(self):
        # The three spellings YAML gives a null, all of which a half-written
        # skill carries and none of which is a description.
        for frontmatter in (
            "name: draft\ndescription:\n",
            "name: draft\ndescription: ~\n",
            "name: draft\ndescription: null\n",
        ):
            with self.subTest(frontmatter=frontmatter):
                self.assertEqual(self._description_of(frontmatter), "")

    def test_a_missing_description_key_reads_as_absent(self):
        self.assertEqual(self._description_of("name: draft\n"), "")

    def test_a_real_description_is_returned_unchanged(self):
        self.assertEqual(
            self._description_of("name: draft\ndescription: Installs things.\n"),
            "Installs things.",
        )


class ClaudeSkillLinksTest(unittest.TestCase):
    def _check_link(self, root, link, target):
        path = root / link
        self.assertTrue(
            path.exists() or path.is_symlink(),
            f"{link} is missing; Claude Code scans only .claude/ and "
            "would find no skills or rules in this checkout",
        )
        self.assertTrue(
            path.is_symlink(),
            f"{link} is not a symlink; Claude Code would read a second "
            f"copy of the skills that drifts from {target}",
        )
        self.assertFalse(
            os.path.isabs(os.readlink(path)),
            f"{link} points at an absolute path, so it resolves only "
            "in the checkout it was made in",
        )
        self.assertEqual(
            path.resolve(),
            (root / target).resolve(),
            f"{link} resolves somewhere other than {target}",
        )

    def test_claude_directories_are_relative_symlinks_into_dot_agents(self):
        # One check method rather than three separate tests over REPO_ROOT:
        # `os.readlink` raises on a path that is not a link, so a separate
        # relative-path test would report a missing path or plain directory as
        # an error with a traceback instead of the failure written above.
        for link, target in CLAUDE_LINKS.items():
            with self.subTest(link=link):
                self._check_link(REPO_ROOT, link, target)

    def test_a_missing_link_is_reported_as_missing_not_as_a_drifting_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".agents" / "skills").mkdir(parents=True)
            with self.assertRaises(AssertionError) as ctx:
                self._check_link(root, ".claude/skills", ".agents/skills")
            self.assertIn(".claude/skills is missing", str(ctx.exception))
            self.assertNotIn("second copy", str(ctx.exception))

    def test_a_plain_directory_is_reported_as_a_drifting_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".agents" / "skills").mkdir(parents=True)
            (root / ".claude" / "skills").mkdir(parents=True)
            with self.assertRaises(AssertionError) as ctx:
                self._check_link(root, ".claude/skills", ".agents/skills")
            self.assertIn("is not a symlink", str(ctx.exception))


class SkillFrontmatterTest(unittest.TestCase):
    def test_every_skill_has_frontmatter_a_harness_can_load(self):
        for skill in _skill_dirs():
            with self.subTest(skill=skill.name):
                block, why = _frontmatter(skill / "SKILL.md")
                self.assertIsNotNone(block, why)

    def test_every_skill_declares_a_name_matching_its_directory(self):
        for skill in _skill_dirs():
            with self.subTest(skill=skill.name):
                block, why = _frontmatter(skill / "SKILL.md")
                self.assertIsNotNone(block, why)
                self.assertEqual(
                    block.get("name"),
                    skill.name,
                    "a harness loads a skill by directory and cites it by "
                    "frontmatter name; the two disagreeing means the name in "
                    "the catalogue cannot be loaded",
                )

    def test_every_skill_declares_a_description(self):
        for skill in _skill_dirs():
            with self.subTest(skill=skill.name):
                description, why = _description(skill / "SKILL.md")
                self.assertTrue(
                    description.strip(),
                    why
                    or "the description is the only part of a skill loaded "
                    "before it is chosen; without one the skill is present "
                    "and unreachable",
                )


class LifecycleSkillDiscoverabilityTest(unittest.TestCase):
    def test_the_lifecycle_skills_are_present(self):
        self.assertLessEqual(
            set(LIFECYCLE_ACTIONS),
            {skill.name for skill in _skill_dirs()},
            "a lifecycle skill moved or was renamed; the floor below no longer "
            "guards anything",
        )

    def test_lifecycle_descriptions_open_with_the_action_a_user_asks_for(self):
        for name, action in LIFECYCLE_ACTIONS.items():
            with self.subTest(skill=name):
                description, why = _description(SKILLS_DIR / name / "SKILL.md")
                opening = description.split(" ", 1)[0].strip(",.:;").lower()
                # A prefix, so the third-person form the rest of this
                # directory is written in ("Installs kube-agents…") passes.
                # It still separates the pair that motivated the check:
                # neither "install" nor "installs" is a prefix of "uninstall",
                # and "uninstall" is not a prefix of "install".
                self.assertTrue(
                    opening.startswith(action),
                    why
                    or f"{name}'s description opens with {opening!r} rather "
                    f"than {action!r}; a request to {action} kube-agents is "
                    "matched against this sentence and nothing else",
                )

    def test_lifecycle_descriptions_name_the_product(self):
        for name in LIFECYCLE_ACTIONS:
            with self.subTest(skill=name):
                description, why = _description(SKILLS_DIR / name / "SKILL.md")
                self.assertIn(
                    PRODUCT,
                    description.lower(),
                    why
                    or f"{name}'s description does not name {PRODUCT}, so a "
                    "request that calls the product by that name has nothing "
                    "to match",
                )


if __name__ == "__main__":
    unittest.main()

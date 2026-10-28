# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Which files make up a shipped skill, for the skill size and command checks.

A skill is a directory holding ``SKILL.md``, at any depth under a skill tree,
so a skill filed under a category directory counts. Its documents are every
Markdown file in that directory except under its ``scripts/``: an agent can
open any of them with ``skill_view``. A ``SKILL.md`` inside another skill, such
as ``references/SKILL.md``, is a document of that skill rather than a skill.
Hermes does not list one under a skill's support directories (``scripts/``
among them) as a skill either; ``skill_view`` opens it only as a file of the
skill that holds it.

``scripts/check_skill_size.py``, ``tests/test_skill_inline_commands.py`` and
``deploy/docker/check_skill_commands.py`` all read skills through this module,
and ``deploy/docker/test_skill_documents.py`` holds them to it, so a document
kind one check reads cannot fall out of another's view. It sits beside
``check_skill_commands.py`` because the image build copies the two together.
Standard library only.
"""

from __future__ import annotations

from pathlib import Path

# The skill trees that ship in an image or a plugin, relative to the repository.
# `.agents/skills` and `third_party/google-skills` are not opened through
# `skill_view` by a deployed agent: the first is for coding agents working on
# this repository, the second is the upstream copy the shipped `gke-*` skills are
# generated from.
SKILL_ROOT_GLOBS = (
    "agents/*/skills",
    "a2a/persona/*/skills",
    "agentplugins/*/files/skills",
)
SKILL_FILE = "SKILL.md"
DOCUMENT_SUFFIX = ".md"
SCRIPTS_DIR = "scripts"


def skill_dirs(tree: Path) -> list[Path]:
    """Every skill directory under ``tree``."""
    found = sorted(skill_md.parent for skill_md in tree.rglob(SKILL_FILE))
    return [d for d in found if not any(other in d.parents for other in found)]


def skill_documents(tree: Path) -> list[Path]:
    """Every document of every skill under ``tree``, sorted by path."""
    skills = skill_dirs(tree)
    return sorted(
        path
        for skill in skills
        for path in skill.rglob(f"*{DOCUMENT_SUFFIX}")
        if path.is_file() and SCRIPTS_DIR not in path.relative_to(skill).parts
    )


def shipped_trees(repo: Path) -> list[Path]:
    """Every shipped skill tree in the repository at ``repo``."""
    return [tree for pattern in SKILL_ROOT_GLOBS for tree in sorted(repo.glob(pattern))]


def shipped_skill_documents(repo: Path) -> list[Path]:
    """Every document of every shipped skill in the repository at ``repo``."""
    return [doc for tree in shipped_trees(repo) for doc in skill_documents(tree)]

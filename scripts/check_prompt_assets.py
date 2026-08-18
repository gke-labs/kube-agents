#!/usr/bin/env python3
"""Verify that the agent's instructions point at assets that exist.

The Platform Agent is steered by Markdown and one JSON file: a persona, a set
of governance SOPs, thirty-odd skill bundles, and the cron prompts that start
the autonomous watchdogs. Those documents name other files -- ``read the SOP at
'governance/compliance_audit_sop.md'``, ``run ``scripts/audit_run.py```, ``defer
to the `gke-compute-classes` skill`` -- and nothing has ever checked that the
things they name are there.

That matters more here than a broken link in a document does, because nobody is
reading these at review time. A skill renamed in one directory and cited under
its old name somewhere else compiles, lints, formats and merges clean; the cost
lands at 06:20 the next morning, inside an agent that quietly cannot find what
it was told to open and carries on without it. The failure is a *worse* answer,
not an error, so it does not page anyone -- it just makes the fleet report
slightly less true, indefinitely.

Four checks, all offline and standard library only:

``asset-path``
    Every path reference in an instruction file resolves to a real file.
``skill-ref``
    Every skill named in prose or in a cron job's ``skills`` array is a real
    skill directory with a ``SKILL.md``.
``skill-manifest``
    A ``SKILL.md``'s frontmatter ``name`` matches the directory it lives in --
    the agent loads skills by directory, so a mismatch means the prose name and
    the loadable name disagree.
``cron-asset``
    Every cron prompt sends the worker to an SOP that exists.

Scope is deliberately narrow, because a lint the team switches off protects
nothing:

* only the files the *agent* reads are checked -- personas, SOPs, skills, agent
  docs and cron prompts. Plugin and directory ``README``s are developer
  documentation about Hermes internals that do not ship in the profile, and
  their relative links are already covered by ``check_docs_links.py``;
* a token is treated as a path only if it has an asset extension *and* its
  first segment names a directory that actually exists under one of the roots
  that file resolves against. That is what keeps ``roles/container.admin``,
  ``kubernetes.io/ingress.global-static-ip-name`` and the illustrative
  ``manifests/vendor/x.yaml`` out of the report without an allowlist anyone has
  to maintain;
* ``/opt/data/**`` is runtime state that exists only in a live pod, and is
  skipped. So is any reference holding a shell variable or an obvious
  placeholder;
* nothing resolves against the repository root. A checkout is not mounted into
  any agent, so ``agents/platform/skills/x/y.md`` -- which names a real file
  here, and survives every review a human gives it -- is a finding: that tree
  is COPYed to ``/opt/platform-template/skills``, so the profile home holds
  ``skills/``, never ``agents/``.

What this does *not* check is the SOP *geography* in the cron prompts -- the
line counts and section ranges. That is already covered, and better, by
``test_cron_prompts_cite_the_real_sop_geography`` in
``agents/platform/skills/fleet-audit/scripts/test_audit_report.py``, which
re-derives the numbers from the SOP headings.

Findings print to stderr as ``path:line: [rule] message``, and under GitHub
Actions each one is also emitted as an ``::error`` annotation so it lands on the
offending line in the pull request's Files Changed view.

Usage::

    python3 scripts/check_prompt_assets.py
    python3 scripts/check_prompt_assets.py --annotate   # render CI's output
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The profile source trees. `chat` is the Chat Agent (the `default` profile),
# `cluster` is the per-cluster template the Platform Agent scaffolds at runtime.
PROFILES = ("platform", "chat", "cluster")

# Extensions an asset reference can end in. Anything else -- `.admin`,
# `.global-static-ip-name`, `.conf` -- is a GCP role, an annotation key or an
# upstream file, not something this repository ships.
ASSET_SUFFIXES = frozenset({".md", ".py", ".sh", ".json", ".yaml", ".yml"})

# What `/opt/defaults` holds, in the order the image layers it, from the COPY
# lines in deploy/docker/Dockerfile.
#
# It reaches ONE profile home. docker-entrypoint.sh:253 does
# `cp -ru /opt/defaults/. "$TARGET_DIR/"`, and $TARGET_DIR is the *default*
# profile's home -- specialist profiles live at $TARGET_DIR/profiles/<name>,
# which that copy never touches. See PROFILE_HOME_ITEMS below for what does
# populate those.
#
# Each entry maps a path under /opt/defaults to the repository path that
# provides it. An empty prefix means the source directory is unpacked at the
# root. `test_opt_defaults_matches_the_dockerfile` re-derives this from the
# Dockerfile, so a new COPY that this table misses fails there rather than
# turning into false "missing asset" reports here.
OPT_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("", "deploy/shared/defaults"),
    ("docs/glossary.md", "agents/platform/docs/glossary.md"),
    ("docs/gcp-console-links.md", "agents/platform/docs/gcp-console-links.md"),
    ("", "agents/chat/defaults"),
    ("SOUL.md", "agents/chat/SOUL.md"),
    ("AGENTS.md", "agents/chat/AGENTS.md"),
    ("scripts", "agents/chat/scripts"),
    ("scripts", "agents/platform/scripts"),
    ("scripts/profile_overlay.py", "deploy/shared/profile_overlay.py"),
    ("scripts/profile_plugins.py", "deploy/shared/profile_plugins.py"),
    ("scripts/otel_config.py", "deploy/shared/otel_config.py"),
)

# What a specialist profile home actually contains, which is not the whole of
# its source tree and is not /opt/defaults either. Each is built from its
# template by an explicit item list: `profile_scaffold.py --items` in
# docker-entrypoint.sh for platform, OVERLAY_ITEMS in cluster_agent_profile.py
# for a cluster. A path whose first segment is outside that list is absent at
# runtime however plainly it exists in the checkout.
#
# agents/platform/docs is the live case: the Dockerfile copies it to
# /opt/defaults/docs and nowhere else, so it reaches the default profile and no
# specialist. `scripts` is here because the entrypoint makes it the one
# exception, symlinking $TARGET_DIR/scripts into profiles/platform (line 538)
# -- a link it would not need if that layer reached the profile on its own.
#
# `chat` is deliberately absent: it *is* the default profile, so its home is
# $TARGET_DIR and the /opt/defaults layer resolves there in full.
# `test_profile_home_items_match_the_entrypoint` re-derives both lists.
PROFILE_HOME_ITEMS: dict[str, frozenset[str]] = {
    "platform": frozenset(
        {
            "config.yaml",
            "SOUL.md",
            "AGENTS.md",
            "CAPABILITIES.md",
            "cron",
            "skills",
            "governance",
            "hindsight",
            "scripts",
        }
    ),
    "cluster": frozenset(
        {"SOUL.md", "AGENTS.md", "CAPABILITIES.md", "config.yaml", "skills"}
    ),
}

# The profile templates the entrypoint scaffolds from. Unlike /opt/defaults
# these are plain directory copies, so the mapping is one line each.
OPT_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("/opt/platform-template/", "agents/platform"),
    ("/opt/cluster-template/", "agents/cluster"),
    ("/opt/chat-template/", "agents/chat"),
)

# Top-level repository directories. These make a token count as a *path* --
# they are not a place a path resolves. Nothing mounts the checkout into an
# agent, so a reference beginning with one of these is reported, including the
# `agents/<profile>/...` form that names a real file here and no file there.
REPO_ROOTS = frozenset(
    {
        "agents",
        "bench",
        "charts",
        "deploy",
        "docs",
        "examples",
        "hack",
        "k8s-operator",
        "scripts",
        "terraform",
    }
)

# A backticked token, which is how every reference in these documents is
# written. Paths in prose without backticks are not searched for: the false
# positive rate on bare prose is high enough to drown the real findings.
TOKEN = re.compile(r"`([^`\n]+)`")

# `the fleet-audit skill`, `` the `gke-compute-classes` skill ``. Two segments
# minimum, so ordinary prose ("this skill", "a skill") does not match.
SKILL_IN_PROSE = re.compile(r"`?\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`?\s+skill\b")

# A quoted path inside a cron prompt. Any depth, not just `dir/file.ext`: the
# single-segment-then-file form left `agents/platform/governance/x_sop.md`
# unchecked, and that spelling is the one the asset-path rule reports as always
# wrong at runtime -- so the SOP was protected against it and the cron prompt
# that sends a worker to the SOP was not.
CRON_PROMPT_REF = re.compile(
    r"['\"`]([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.(?:md|json|py|sh))['\"`]"
)

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
FRONTMATTER_NAME = re.compile(r"^name:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
# `description:` may carry its value on the same line or as an indented block
# beneath it -- prettier reflows the long ones into the second form, so a
# same-line-only pattern reports the tidiest manifests as the broken ones.
#
# The block indicators are called out rather than left to `\S.*`, which matched
# `>-` itself and so accepted `description: >-` with the folded body deleted --
# the realistic slip, since most bundles here are in that form. An empty
# description loads a skill the agent will never select.
FRONTMATTER_DESCRIPTION = re.compile(
    r"^description:[ \t]*"
    r"(?:(?![>|][-+]?[ \t]*$)\S.*"
    r"|[>|][-+]?[ \t]*\n(?:[ \t]+\S.*\n?)+"
    r"|\n(?:[ \t]+\S.*\n?)+)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Finding:
    rule: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: [{self.rule}] {self.message}"

    def annotation(self) -> str:
        """This finding as a GitHub Actions workflow command.

        Printing these puts each finding on the line that holds it in the pull
        request's Files Changed view. Without them a broken reference is a red
        check and a collapsed log, which for a layer nobody reads at review
        time is most of the way to no check at all.

        `where` is `path:line` for the rules that can name a line and a bare
        path for the rest; `line=` is simply omitted for the latter, which
        anchors the annotation to the file.
        """
        path, _, line = self.where.partition(":")
        position = f",line={line}" if line.isdigit() else ""
        # A literal \n, %0A here, would start a second annotation, and a bare :
        # or , inside the message would be read as another property.
        escaped = (
            self.message.replace("%", "%25")
            .replace("\r", "%0D")
            .replace("\n", "%0A")
        )
        return f"::error file={path}{position},title=prompt {self.rule}::{escaped}"


def _is_placeholder(ref: str) -> bool:
    """True for references that name no fixed file and cannot be resolved.

    Runtime state under `/opt/data` is created by a running pod and is correctly
    absent from a checkout. The rest are references written as templates --
    `$HERMES_HOME/...`, `memory/YYYY-MM-DD.md`, `<cluster>/kustomization.yaml`.
    """
    if ref.startswith("/opt/data/"):
        return True
    if any(ch in ref for ch in "$<>{}*?"):
        return True
    if "..." in ref or "YYYY" in ref:
        return True
    # Absolute paths that are neither image layout nor repository content:
    # /var/run/docker.sock, /run/containerd/containerd.sock, /etc/...
    return ref.startswith("/") and not ref.startswith("/opt/")


def _resolve_opt(rel: str) -> Path | None:
    """Resolve a path under /opt/defaults back to the file that provides it."""
    for prefix, source in OPT_DEFAULTS:
        if not prefix:
            candidate = REPO / source / rel
        elif rel == prefix:
            candidate = REPO / source
        elif rel.startswith(prefix + "/"):
            candidate = REPO / source / rel[len(prefix) + 1 :]
        else:
            continue
        if candidate.exists():
            return candidate
    return None


def _profile_of(path: Path) -> str | None:
    parts = path.relative_to(REPO).parts
    if len(parts) >= 2 and parts[0] == "agents" and parts[1] in PROFILES:
        return parts[1]
    return None


def _skill_dir_of(path: Path) -> Path | None:
    """The skill bundle a file belongs to, if any: agents/<p>/skills/<name>/."""
    parts = path.relative_to(REPO).parts
    if len(parts) >= 4 and parts[0] == "agents" and parts[2] == "skills":
        return REPO / Path(*parts[:4])
    return None


def _roots_for(path: Path) -> list[Path]:
    """Where a reference in `path` is resolved from, in precedence order.

    A skill's own bundle first -- `references/` and `assets/` mean the skill's
    own, and two skills can hold a `references/` of the same name. Then the
    profile home, which is what `governance/...` and `docs/...` are relative to
    once the profile is unpacked on the PVC.
    """
    roots: list[Path] = []
    skill = _skill_dir_of(path)
    if skill is not None:
        roots.append(skill)
    profile = _profile_of(path)
    if profile is not None:
        roots.append(REPO / "agents" / profile)
    return roots


@lru_cache(maxsize=None)
def _known_first_segments(repo: Path, roots: tuple[Path, ...]) -> frozenset[str]:
    """Directory names that exist directly under any of `roots`.

    This is what separates a path from a string that merely contains a slash,
    and it is derived rather than listed so that a new asset directory is
    covered the day it is added. `manifests/vendor/x.yaml` in a sentence about
    a user's GitOps repository is not a reference to anything here, and no
    profile has a `manifests/`, so it is not treated as one.

    `repo` is passed rather than read off the module so that it is part of the
    cache key: the tests point REPO at a synthetic tree, and a cache keyed only
    on the roots would serve them this repository's answer.
    """
    segments: set[str] = set()
    for root in roots:
        if root.is_dir():
            segments |= {child.name for child in root.iterdir() if child.is_dir()}
    for prefix, source in OPT_DEFAULTS:
        if prefix:
            segments.add(prefix.split("/")[0])
        elif (repo / source).is_dir():
            segments |= {
                child.name for child in (repo / source).iterdir() if child.is_dir()
            }
    return frozenset(segments)


def _path_hint(ref: str, path: Path, roots: list[Path]) -> str:
    """Name the in-profile spelling when the reference is a checkout path.

    `agents/platform/skills/x/references/y.md` is the most useful thing this
    can say, because it is a file that plainly exists -- the author checked --
    and the reason it fails is a prefix the profile does not have. Saying only
    "does not resolve" invites the reader to conclude the checker is wrong.

    The rewrite is offered only for the citing file's *own* profile. Stripping
    `agents/chat/` off a path in a platform document and suggesting the
    remainder would name the platform file of that name, which is a different
    document -- a hint that turns a broken reference into a wrong one.
    """
    profile = _profile_of(path)
    if (
        profile in PROFILE_HOME_ITEMS
        and not ref.startswith(("/", "./", "../"))
        and _resolve_opt(ref) is not None
    ):
        return (
            f"; /opt/defaults/{ref} is copied to the default profile's home "
            f"and to no other, so the {profile} profile has no {ref.split('/')[0]!r} "
            f"-- write '/opt/defaults/{ref}'"
        )
    if not (REPO / ref).exists():
        return ""
    parts = ref.split("/")
    if len(parts) > 2 and parts[0] == "agents" and parts[1] in PROFILES:
        if parts[1] != _profile_of(path):
            return (
                f"; it belongs to the {parts[1]} profile, which is a separate "
                "home this agent cannot read"
            )
        inside = "/".join(parts[2:])
        if any((root / inside).exists() for root in roots):
            return (
                f"; that is its path in the checkout, not in the profile -- "
                f"write {inside!r}"
            )
    return (
        "; it exists in the checkout, but nothing copies it into this profile "
        "(see the COPY list in deploy/docker/Dockerfile)"
    )


def _looks_like_reference(ref: str, roots: list[Path]) -> bool:
    if "/" not in ref or ref.endswith("/"):
        return False
    if Path(ref).suffix not in ASSET_SUFFIXES:
        return False
    if ref.startswith(("./", "../", "/opt/")):
        return True
    head = ref.split("/", 1)[0]
    return head in REPO_ROOTS or head in _known_first_segments(REPO, tuple(roots))


def _resolves(ref: str, path: Path, roots: list[Path]) -> bool:
    if ref.startswith("/opt/defaults/"):
        return _resolve_opt(ref[len("/opt/defaults/") :]) is not None
    for prefix, source in OPT_TEMPLATES:
        if ref.startswith(prefix):
            return (REPO / source / ref[len(prefix) :]).exists()
    if ref.startswith("/opt/"):
        # /opt/hermes and friends belong to the upstream framework image.
        return True
    if ref.startswith("../"):
        return (path.parent / ref).exists()
    if ref.startswith("./"):
        # `./` in these documents means "from where you are working", which is
        # the profile home for a persona or an SOP and the bundle for a skill --
        # not the directory the sentence happens to be written in. A skill's
        # reference page says `./assets/x.sh` for an asset one level up from
        # itself, and the fleet-audit SKILL.md says
        # `./skills/fleet-audit/scripts/audit_report.py` for its own script.
        # Both are right, and both are wrong if `./` is read literally.
        bare = ref[2:]
        if (path.parent / bare).exists():
            return True
        return _resolves_in_home(bare, path)
    return _resolves_in_home(ref, path)


def _resolves_in_home(ref: str, path: Path) -> bool:
    """A profile-relative path, against the home that profile is actually given.

    Two things are deliberately not roots here.

    The repository root: a checkout is not mounted into any agent, so
    `agents/platform/skills/x/y.md` names a real file here and no file there --
    that tree is COPYed to /opt/platform-template/skills, so the home holds
    `skills/`, never `agents/`. REPO_ROOTS still makes such a token count as a
    path, so it is reported rather than falling out of scope.

    And the whole of the profile's source tree, for a specialist. The home is
    built from a fixed item list (PROFILE_HOME_ITEMS); `docs/` is in neither
    list and in neither template, so a platform document citing
    `docs/glossary.md` is citing the copy at /opt/defaults/docs, which reaches
    the default profile alone. The absolute path is the spelling that works.
    """
    skill = _skill_dir_of(path)
    if skill is not None and (skill / ref).exists():
        return True
    profile = _profile_of(path)
    if profile is None:
        return False
    items = PROFILE_HOME_ITEMS.get(profile)
    if items is not None:
        if ref.split("/", 1)[0] not in items:
            return False
        if (REPO / "agents" / profile / ref).exists():
            return True
        # `scripts` is the symlinked exception, and its contents come from the
        # shared layer rather than this profile's own tree.
        return ref.startswith("scripts/") and _resolve_opt(ref) is not None
    # The default profile's home IS $TARGET_DIR, so the /opt/defaults layer the
    # entrypoint lays down there is a legitimate source for it.
    if (REPO / "agents" / profile / ref).exists():
        return True
    return _resolve_opt(ref) is not None


def instruction_files() -> list[Path]:
    """The Markdown the agent itself is given. Not developer documentation.

    Plugin and directory READMEs are excluded on purpose: they describe Hermes
    internals and this repository's own tooling, they are not copied into a
    profile, and no agent reads them at runtime. The design docs that sit in
    `agents/<profile>/docs/` alongside the baked runtime references are
    excluded for the same reason -- see the comment on the `docs` entries
    below for why the directory cannot simply be globbed.
    """
    found: list[Path] = []
    for profile in PROFILES:
        home = REPO / "agents" / profile
        if not home.is_dir():
            continue
        for name in ("SOUL.md", "AGENTS.md", "CAPABILITIES.md"):
            if (home / name).is_file():
                found.append(home / name)
        found.extend(sorted((home / "governance").glob("*.md")))
        # `docs` is not a profile-home item and the layer does not take the
        # directory: the Dockerfile bakes named files out of it into
        # /opt/defaults/docs and leaves the rest. Those others are design docs
        # -- docs/README.md calls them "not baked into the image despite its
        # location" -- whose only reader is a human in a checkout, where their
        # `agents/<profile>/...` citations resolve exactly as written. Globbing
        # the directory swept them in and demanded the in-profile spelling for
        # a profile they never reach, which rewrites a working citation into
        # one that resolves nowhere. OPT_DEFAULTS lists the baked ones and
        # `test_opt_defaults_matches_the_dockerfile` holds it to the COPYs, so
        # a third one starts being checked on its own.
        found.extend(
            REPO / src
            for _, src in OPT_DEFAULTS
            if src.startswith(f"agents/{profile}/docs/") and (REPO / src).is_file()
        )
        found.extend(
            md
            for md in sorted((home / "skills").rglob("*.md"))
            if md.name != "README.md"
        )
    return found


def cron_rosters() -> list[tuple[Path, list[dict]]]:
    """Every cron roster in the tree, found rather than listed.

    This was two hardcoded paths behind an `is_file()` guard, which is the
    silent-scope failure the bundle discovery already had: move a roster and
    both cron rules become no-ops while the run still prints OK. Globbing means
    a roster that moves within agents/ is still checked, and the count in the
    success line means one that moves out of it is visible in the log.
    """
    rosters = []
    for path in sorted(REPO.glob("agents/*/cron/jobs.json")) + sorted(
        REPO.glob("agents/*/defaults/cron/jobs.json")
    ):
        rosters.append((path, json.loads(path.read_text(encoding="utf-8"))["jobs"]))
    return rosters


def skill_directories() -> dict[str, dict[str, Path]]:
    """Every skill bundle, grouped by the profile that owns it.

    Keyed by profile and *then* by name, not by name alone. Six names exist in
    both agents/platform/skills and agents/cluster/skills -- gke-storage,
    gke-reliability and four more -- and a flat dict silently kept whichever
    profile was iterated last, dropping six bundles out of every rule that walks
    it. That is the same silent-skip failure this checker exists to catch.

    Grouping is also the correct model, not merely a fix for the collision. The
    Dockerfile gives each profile its own skills directory -- agents/platform to
    /opt/platform-template, agents/cluster to /opt/cluster-template -- and no
    skill travels through the /opt/defaults layer (`make validate` fails a skill
    placed under agents/*/defaults/skills). So a Cluster Agent document naming a
    platform-only skill is naming something that profile will never receive, and
    a flat namespace cannot see it.
    """
    skills: dict[str, dict[str, Path]] = {}
    for profile in PROFILES:
        home = REPO / "agents" / profile / "skills"
        if not home.is_dir():
            continue
        skills[profile] = {
            child.name: child for child in sorted(home.iterdir()) if child.is_dir()
        }
    return skills


def check_asset_paths(files: list[Path]) -> list[Finding]:
    findings = []
    for path in files:
        roots = _roots_for(path)
        rel = path.relative_to(REPO)
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            for match in TOKEN.finditer(line):
                ref = match.group(1).strip()
                if _is_placeholder(ref) or not _looks_like_reference(ref, roots):
                    continue
                if not _resolves(ref, path, roots):
                    findings.append(
                        Finding(
                            "asset-path",
                            f"{rel}:{number}",
                            f"{ref!r} does not resolve to a file the agent will "
                            f"have{_path_hint(ref, path, roots)}",
                        )
                    )
    return findings


def check_skill_refs(
    files: list[Path], skills: dict[str, dict[str, Path]]
) -> list[Finding]:
    """A skill named in a document has to exist in *that document's* profile.

    Not in some merged namespace: a bundle under agents/platform/skills is never
    delivered to a Cluster Agent, so a cluster document citing one is citing
    something that profile will not have -- indistinguishable at runtime from
    citing a skill that does not exist anywhere.
    """
    findings = []
    for path in files:
        rel = path.relative_to(REPO)
        profile = _profile_of(path)
        owned = skills.get(profile or "", {})
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            for match in SKILL_IN_PROSE.finditer(line):
                name = match.group(1)
                if name in owned:
                    continue
                findings.append(
                    Finding(
                        "skill-ref",
                        f"{rel}:{number}",
                        f"names the {name!r} skill, which the {profile} profile "
                        f"does not have{_hint(name, owned, skills)}",
                    )
                )
    for path, jobs in cron_rosters():
        rel = path.relative_to(REPO)
        profile = _profile_of(path)
        owned = skills.get(profile or "", {})
        for job in jobs:
            for name in job.get("skills", []):
                if name not in owned:
                    findings.append(
                        Finding(
                            "skill-ref",
                            f"{rel}",
                            f"job {job['id']!r} declares skill {name!r}, which the "
                            f"{profile} profile does not have"
                            f"{_hint(name, owned, skills)}",
                        )
                    )
    return findings


def check_skill_manifests(skills: dict[str, dict[str, Path]]) -> list[Finding]:
    """Every bundle in every profile, including same-named siblings.

    The pairs matter: agents/platform/skills/gke-storage and
    agents/cluster/skills/gke-storage are different bundles that drift
    independently -- as of writing one carries a one-line description and the
    other a folded block -- so both have to be opened.
    """
    findings = []
    for name, directory in (
        (name, directory)
        for profile in sorted(skills)
        for name, directory in sorted(skills[profile].items())
    ):
        manifest = directory / "SKILL.md"
        rel = manifest.relative_to(REPO)
        if not manifest.is_file():
            findings.append(
                Finding("skill-manifest", str(directory.relative_to(REPO)), "has no SKILL.md")
            )
            continue
        matter = FRONTMATTER.match(manifest.read_text(encoding="utf-8"))
        if matter is None:
            findings.append(Finding("skill-manifest", str(rel), "has no YAML frontmatter"))
            continue
        declared = FRONTMATTER_NAME.search(matter.group(1))
        if declared is None:
            findings.append(Finding("skill-manifest", str(rel), "frontmatter has no 'name'"))
        elif declared.group(1) != name:
            findings.append(
                Finding(
                    "skill-manifest",
                    str(rel),
                    f"frontmatter name is {declared.group(1)!r} but the "
                    f"directory the agent loads it by is {name!r}",
                )
            )
        if FRONTMATTER_DESCRIPTION.search(matter.group(1)) is None:
            findings.append(
                Finding("skill-manifest", str(rel), "frontmatter has no 'description'")
            )
    return findings


def check_cron_assets() -> list[Finding]:
    """Every SOP a cron prompt sends its worker to has to be there.

    The geography of those SOPs -- how long they are, which lines hold the
    checks -- is verified by the fleet-audit suite, which can re-derive it. This
    only asks the cheaper question that suite does not ask of every roster: does
    the file exist at all.
    """
    findings = []
    for path, jobs in cron_rosters():
        rel = path.relative_to(REPO)
        for job in jobs:
            for ref in set(CRON_PROMPT_REF.findall(job.get("prompt", ""))):
                # The same profile-home model the asset-path rule uses. Resolving
                # against the whole of agents/<profile>/ instead was how this rule
                # came to accept `docs/glossary.md` from the platform roster --
                # the one class the message it prints claims to be about.
                if _resolves_in_home(ref, path):
                    continue
                findings.append(
                    Finding(
                        "cron-asset",
                        str(rel),
                        f"job {job['id']!r} sends the worker to {ref!r}, "
                        "which is not in its profile",
                    )
                )
    return findings


def _nearest(name: str, owned: dict[str, Path]) -> str | None:
    """The closest real skill name, for the common single-character slips."""
    matches = difflib.get_close_matches(name, sorted(owned), n=1, cutoff=0.85)
    return matches[0] if matches else None


def _hint(
    name: str, owned: dict[str, Path], skills: dict[str, dict[str, Path]]
) -> str:
    """Why this name failed, when the answer is knowable.

    Two different mistakes reach here and they want different fixes. A slip of
    the keyboard wants the nearest real name in the same profile. A skill that
    exists, but in another profile, wants saying so outright -- otherwise the
    author reads "does not have" and goes looking for a bundle that is sitting
    right there in the tree, and concludes the checker is wrong.
    """
    elsewhere = sorted(other for other, bundles in skills.items() if name in bundles)
    if elsewhere:
        return (
            f"; it exists in {' and '.join(elsewhere)}, which this profile "
            "does not receive"
        )
    near = _nearest(name, owned)
    return f"; did you mean {near!r}?" if near else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    annotate = parser.add_mutually_exclusive_group()
    # Default is to annotate whenever GitHub sets GITHUB_ACTIONS, so the CI job
    # is a plain `make prompt-check` with nothing to keep in sync. The flags are
    # for forcing either way -- --annotate to see what CI will render, --no-
    # annotate to keep a local run readable if the variable is already set.
    annotate.add_argument(
        "--annotate",
        dest="annotate",
        action="store_true",
        default=os.environ.get("GITHUB_ACTIONS") == "true",
        help="emit GitHub Actions error annotations (default: only under CI)",
    )
    annotate.add_argument(
        "--no-annotate", dest="annotate", action="store_false",
        help="suppress annotations even under CI",
    )
    options = parser.parse_args(argv)

    files = instruction_files()
    skills = skill_directories()
    findings = (
        check_asset_paths(files)
        + check_skill_refs(files, skills)
        + check_skill_manifests(skills)
        + check_cron_assets()
    )
    if findings:
        print(
            f"{len(findings)} broken reference(s) in the agent's instructions:\n",
            file=sys.stderr,
        )
        for finding in sorted(findings, key=lambda f: (f.rule, f.where)):
            print(f"  {finding}", file=sys.stderr)
        if options.annotate:
            # stdout, not stderr: GitHub reads workflow commands from both, but
            # interleaving them with the human-readable block above makes the
            # raw log unreadable.
            for finding in sorted(findings, key=lambda f: (f.rule, f.where)):
                print(finding.annotation())
        print(
            "\nThe agent follows these at runtime with no one watching. A "
            "reference it cannot open is silently skipped work.",
            file=sys.stderr,
        )
        return 1
    # The counts are here so that a scope which quietly narrows is visible in
    # the log. A rule that runs over nothing passes, and "OK" over an empty
    # roster list reads exactly like "OK" over a full one.
    rosters = cron_rosters()
    print(
        f"Prompt assets OK: {len(files)} instruction files, "
        f"{sum(len(b) for b in skills.values())} skill bundles "
        f"({', '.join(f'{p} {len(b)}' for p, b in sorted(skills.items()))}), "
        f"{sum(len(jobs) for _, jobs in rosters)} cron jobs in {len(rosters)} "
        "rosters, all references resolve."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

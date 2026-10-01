"""Refuse ``skill_manage`` writes to the skills the image ships.

Installed into the image at ``/opt/hermes/tools/skill_manage_image_owned.py``
and wired into ``tools/skill_manager_tool.py::skill_manage`` by
``deploy/docker/Dockerfile`` via ``apply_skill_manage_image_owned.py``.

Upstream's ``skill_manage`` is the agent's one in-process write path to its
skills directory: ``create``, ``patch`` (and the legacy ``edit``), ``delete``,
``write_file`` and ``remove_file``, flat or as an atomic ``operations`` batch,
all of it against ``$HERMES_HOME/skills`` (``_skills_dir()``) or wherever an
existing skill lives (``get_all_skills_dirs()``). Nothing upstream
distinguishes a skill the image ships from one the agent authored.

This image already treats the shipped ones as not the agent's to change, in
three places that agree:

- the ownership block after the skill ``COPY``s in ``deploy/docker/Dockerfile``
  makes the template trees root-owned so that the agent's own instructions
  are "writable by nobody it can reach";
- ``deploy/shared/docker-entrypoint.sh`` step 2.6a replaces every specialist
  profile's ``skills/`` from the image template on every gateway start,
  because "skills are wholly image-owned";
- ``agents/platform/scripts/verify_skills_provenance.py`` checks the trees
  against a SHA-256 manifest at boot, because a ``SKILL.md`` is prompt
  material and a skill's scripts run with the agent's credentials.

What none of them reached was the copy on the data volume between two
restarts. In the 18 September 2026 leaderboard run recorded in
gke-labs/kube-agents#1848 a platform worker patched ``submit-suggestion``'s
``SKILL.md`` twice through this tool, mid-run and unasked, and every later
session in that profile would have read the edited instructions until the
next restart. The run scored; the harness under test had rewritten itself.

The gate below answers one question at the top of ``skill_manage``: is the
named skill shipped in the image for the profile this process runs as? If
so, every write action is refused with a message that says where a gap in
the skill belongs (the worker's result), and the call never reaches the
approval gate, the ledger, or a handler. Names the image does not ship are
untouched, so a skill the agent authors under a new name stays as writable as
upstream makes it. ``skills_list`` and ``skill_view`` are not touched at all.

Upstream's own system prompt is part of why prose alone did not hold: the
Skills section it emits tells the model "If a skill has issues, fix it with
skill_manage(action='patch')" and to update a skill that was missing steps
before finishing (``agent/prompt_builder.py`` at the pinned version). On this
image that instruction is wrong for every shipped skill, and a rule in
``AGENTS.md`` competes with it on every turn; the gate is what settles it,
and its message says what to do instead.

Which template feeds which profile is read from ``HERMES_HOME``'s shape, the
way the entrypoint and the kanban dispatcher already agree on it: the default
profile is homed at ``$HERMES_HOME`` itself and a specialist at
``$HERMES_HOME/profiles/<name>``. ``platform`` is fed by
``/opt/platform-template/skills`` (plus the one-skill ``/opt/a2a-template``
overlay, which exists in the image whether or not the install's mode applies
it), every ``cluster-*`` profile by ``/opt/cluster-template/skills``, and the
default profile by nothing this image ships (its config disables the toolset
anyway). The answer is a ``stat`` on the read-only image tree, never on the
profile's own copy: the copy is what is being protected.

The file tools are the second in-process writer, and they are gated here too.
``write_file``, ``patch``, ``delete_file`` and ``move_file`` run their target
through ``agent/file_safety.py`` before any backend sees it; this module
answers, for that check, whether the target sits under a Hermes home's
``skills/`` or ``scripts/`` tree, and the write is refused with a message that
names the right tool. That matters because the file tools write to the shell
sandbox's copy of the tree (the sandbox carries the same paths on its own
volume), which is exactly where a worker refused by ``skill_manage`` went
next when this change was first run against the bench case.

What this does not close: writers that run in the sandbox. A shell command,
or ``execute_code``, can still ``sed -i`` the sandbox's copy for as long as
that sandbox pod runs, and a symlink made in the sandbox is invisible to the
file guard's ``realpath``, which runs on the gateway. That edit never reaches
the gateway (the writeback channel is closed at the sandbox end, see
``docs/designs/agent-shell-sandboxing.md``) and is replaced from the image
when the sandbox restarts. The bench case's safeguard counts only a
successful ``skill_manage`` write, so it does not observe these routes;
closing them needs the sandbox's copy to be read-only, a change to the
sandbox (gke-labs/kube-agents#2096).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

#: The Hermes home the entrypoint gives the default profile, and the fallback
#: when Hermes itself is not importable (the unit tests).
DEFAULT_HERMES_HOME = "/opt/data"
#: The directory under a home that holds the specialist profiles.
PROFILES_DIR = "profiles"
#: The file whose presence makes a directory a skill.
SKILL_FILE = "SKILL.md"
#: The profile names this gate knows: the entrypoint homes the platform profile
#: at ``profiles/platform``, every Cluster Agent at ``profiles/cluster-<name>``,
#: and the default (Planning Agent) profile at the home itself.
PLATFORM_PROFILE = "platform"
CLUSTER_PROFILE_PREFIX = "cluster-"
DEFAULT_PROFILE = "default"
#: The flag ``python3 -m tools.skill_manage_image_owned`` takes in the
#: ``platform`` build stage; ``deploy/docker/Dockerfile`` spells it too.
CHECK_ROOTS_FLAG = "--check-roots"

#: Template trees the image ships, by the profile whose ``skills/`` they feed.
#: The paths are the Dockerfile's ``COPY ... /opt/<x>-template/skills/``
#: destinations; entrypoint step 2.6a replaces each profile's ``skills/`` from
#: the same trees at every start, so "shipped for this profile" and "present
#: under these roots" are one question.
PLATFORM_ROOTS: tuple[str, ...] = ("/opt/platform-template/skills", "/opt/a2a-template/skills")
CLUSTER_ROOTS: tuple[str, ...] = ("/opt/cluster-template/skills",)

#: Every action upstream's ``_ACTION_HANDLERS`` knows. An action outside this
#: set is left to upstream's own "Unknown action" error.
WRITE_ACTIONS = frozenset({"create", "edit", "patch", "delete", "write_file", "remove_file"})

#: Subdirectories of a Hermes home that the image manages and the entrypoint
#: replaces at every start. The file tools (``write_file``, ``patch``,
#: ``delete_file``, ``move_file``) refuse to write under them: ``skill_manage``
#: is the tool for a skill, and it has its own gate above. Without this, the
#: refusal from ``skill_manage`` was answered by a ``patch`` of the same
#: ``SKILL.md`` path, which the file tools write to the shell sandbox's copy of
#: the tree (observed on the first green attempt of the case this ships with).
IMAGE_MANAGED_SUBPATHS: tuple[str, ...] = ("skills", "scripts")


def profile_for_home(home: Path) -> str:
    """``platform``, ``cluster-<name>`` or ``default``, from where ``home`` sits.

    The entrypoint homes the default profile at ``$HERMES_HOME`` and every
    specialist at ``$HERMES_HOME/profiles/<name>``; the kanban dispatcher
    re-points a worker's ``HERMES_HOME`` at the latter. That layout is the
    only per-process record of the profile, so it is what is read here.
    """
    return home.name if home.parent.name == PROFILES_DIR else DEFAULT_PROFILE


def image_skill_roots(profile: str) -> tuple[str, ...]:
    """The template trees that feed ``profile``; empty for a profile the image
    ships no skills for."""
    if profile == PLATFORM_PROFILE:
        return PLATFORM_ROOTS
    if profile.startswith(CLUSTER_PROFILE_PREFIX):
        return CLUSTER_ROOTS
    return ()


def _components(reference: str) -> list[str]:
    """The path components of a skill reference, both separators accepted."""
    parts = [part.strip() for part in reference.replace("\\", "/").split("/")]
    return [part for part in parts if part not in ("", ".", "..")]


def skill_basename(name: str) -> str:
    """The directory name a skill reference names: the last component of
    ``gke-basics`` or of the categorized ``ops/gke-basics``."""
    parts = _components(name or "")
    return parts[-1] if parts else ""


def image_shipped_skill(name: str, profile: str) -> Optional[str]:
    """The template tree that ships ``name`` for ``profile``, or ``None``.

    Answered by a ``stat`` on the read-only image tree, never on the profile's
    own ``skills/`` copy: that copy is uid-writable and is the thing this gate
    protects, and its manifest is not reliable either (an overlay can replace
    it).
    """
    base = skill_basename(name)
    if not base:
        return None
    for root in image_skill_roots(profile):
        if (Path(root) / base / SKILL_FILE).is_file():
            return root
    return None


def _skills_root() -> Optional[Path]:
    """The profile's own ``skills/`` root, from the tool module when it is
    loaded; ``None`` outside Hermes."""
    try:
        from tools import skill_manager_tool as smt

        return Path(smt._skills_dir())
    except Exception:  # noqa: BLE001 - no Hermes on the path
        return None


def _locate(name: str) -> Optional[Path]:
    """Where an existing skill lives, through upstream's own lookup; ``None``
    when it does not exist or Hermes is not on the path."""
    try:
        from tools import skill_manager_tool as smt

        hit = smt._find_skill(name)
        return Path(hit["path"]) if hit else None
    except Exception:  # noqa: BLE001 - no Hermes on the path
        return None


def touched_skill_names(
    name: str,
    category: Optional[str] = None,
    *,
    locate: Callable[[str], Optional[Path]] = _locate,
    skills_root: Callable[[], Optional[Path]] = _skills_root,
) -> set[str]:
    """Every top-level skill directory a write to ``name`` could reach.

    Three routes lead into a shipped skill's directory and all three are
    named here. The bare or categorized ``name`` itself. A ``category``, which
    ``_resolve_skill_dir`` joins UNDER the skills root, so
    ``create(name="scripts", category="fleet-audit")`` writes inside the
    shipped ``fleet-audit`` tree. And the directory an existing skill already
    lives in: ``_find_skill`` walks the tree by directory name, so a skill
    nested under a shipped one is found by its bare name and a ``delete`` of
    it would remove part of the shipped tree. The answer is the set of first
    components, plus the basename, so a check against the template trees
    catches whichever of them the image ships.
    """
    names: set[str] = set()
    parts = _components(name or "")
    if parts:
        names.add(parts[-1])
        names.add(parts[0])
    category_parts = _components(category or "")
    if category_parts:
        names.add(category_parts[0])
    # The lookups are upstream's and may raise for reasons of their own; a
    # failed lookup degrades to the name-based answer rather than failing the
    # tool call, so the gate can never be the reason skill_manage stops working.
    try:
        located = locate(name) if name else None
    except Exception:  # noqa: BLE001
        located = None
    if located is not None:
        names.add(located.name)
        try:
            root = skills_root()
            relative = located.resolve().relative_to(root.resolve()) if root is not None else None
        except Exception:  # noqa: BLE001
            relative = None
        if relative is not None and relative.parts:
            names.add(relative.parts[0])
    return names


def _hermes_home() -> Path:
    """The active Hermes home at call time.

    Through ``hermes_constants.get_hermes_home`` when it is importable, the
    same call ``_skills_dir()`` makes, so a multi-profile runtime that rebinds
    the home per session is answered for the session, not the launch. The
    environment fallback is for the unit tests, which run without Hermes.
    """
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - no Hermes on the path
        return Path(os.environ.get("HERMES_HOME", "").strip() or DEFAULT_HERMES_HOME)


def image_owned_refusal(
    action: str,
    name: str,
    *,
    category: Optional[str] = None,
    hermes_home: Path | None = None,
    locate: Callable[[str], Optional[Path]] = _locate,
    skills_root: Callable[[], Optional[Path]] = _skills_root,
) -> Optional[str]:
    """The refusal for a flat ``skill_manage`` call, or ``None`` to let it through.

    ``None`` for an action this module does not know (upstream reports it),
    for a profile the image ships no skills for, and for a write that touches
    no shipped skill's directory. The message names the profile and says
    where a gap in the skill belongs, because the refusal is read by a model
    that was about to fix something and needs the next move, not only a "no".
    """
    if action not in WRITE_ACTIONS:
        return None
    home = hermes_home if hermes_home is not None else _hermes_home()
    profile = profile_for_home(home)
    if not image_skill_roots(profile):
        return None
    shipped = sorted(
        candidate
        for candidate in touched_skill_names(name, category, locate=locate, skills_root=skills_root)
        if image_shipped_skill(candidate, profile) is not None
    )
    if not shipped:
        return None
    return (
        f"skill_manage refused: '{name}' would write to {', '.join(repr(n) for n in shipped)}, "
        f"which the image ships for the {profile} profile, and shipped skills are "
        "image-owned. This profile's skills/ is replaced from the image at every start "
        "and the image's copy is provenance-checked, so an edit here would not last and "
        "is not yours to make. If the skill has a gap, say so in your result, naming the "
        "skill and what it should have said, so the person who asked can carry it to the "
        "repository. Skills under names the image does not ship can still be created and "
        "edited."
    )


def image_owned_batch_refusal(
    operations: Any,
    default_name: Optional[str],
    *,
    hermes_home: Path | None = None,
    locate: Callable[[str], Optional[Path]] = _locate,
    skills_root: Callable[[], Optional[Path]] = _skills_root,
) -> Optional[str]:
    """The refusal for an ``operations`` batch, or ``None`` to let it through.

    Asked before ``_skill_manage_batch`` runs, so a batch naming a shipped
    skill is refused before the batch snapshots that skill's directory for
    its rollback: refusing only at the per-operation re-entry would still
    keep the shipped directory out of harm's way, but through a rename-aside
    and re-copy of it on every refused batch. A malformed batch is left to
    upstream's own validation.
    """
    if not isinstance(operations, list):
        return None
    for op in operations:
        if not isinstance(op, dict):
            continue
        name = op.get("name") or default_name or ""
        refusal = image_owned_refusal(
            str(op.get("action") or ""), str(name), category=op.get("category"),
            hermes_home=hermes_home, locate=locate, skills_root=skills_root,
        )
        if refusal is not None:
            return refusal
    return None


def image_managed_tree(resolved: str, bases: Iterable[Path]) -> Optional[str]:
    """The image-managed tree ``resolved`` sits under, as ``<base>/<sub>``, or ``None``.

    ``bases`` are the Hermes directories the file guard already checks (the
    active home and the root); ``resolved`` is the guard's realpath of the
    target. Realpath on both sides, so a symlink the gateway can see is
    followed; one that exists only on the sandbox's filesystem is not (the
    guard runs in the gateway), which is part of the sandbox route above.
    """
    target = os.path.realpath(str(resolved))
    for base in bases:
        for sub in IMAGE_MANAGED_SUBPATHS:
            root = os.path.realpath(os.path.join(str(base), sub))
            if target == root or target.startswith(root + os.sep):
                return root
    return None


def image_managed_write_refusal(path: str, tree: str, *, verb: str = "Write") -> str:
    """The message the file tools return for a write under an image-managed tree."""
    return (
        f"{verb} denied: '{path}' is under {tree}, a tree the image manages: it is "
        "replaced from the image at every start and its skills are image-owned. A skill "
        "you authored is edited with skill_manage; a gap in a shipped skill or script "
        "belongs in your result, naming it and what it should have said."
    )


def check_roots(argv: Optional[list[str]] = None) -> int:
    """Prove the configured roots name the image's real template trees.

    Run by ``deploy/docker/Dockerfile`` in the ``platform`` stage (``python3 -m
    tools.skill_manage_image_owned --check-roots``), the stage that copies the
    trees, so a Dockerfile change that moves a template destination fails the
    build instead of silently making every shipped skill writable. Each
    configured platform and cluster root must exist and hold at least one
    skill, that skill must answer as shipped for its own profile, and a
    platform skill must not answer as shipped for the default profile.
    """
    if argv is None or CHECK_ROOTS_FLAG not in argv:
        print(f"usage: python3 -m tools.skill_manage_image_owned {CHECK_ROOTS_FLAG}")
        return 2
    problems: list[str] = []
    for profile, roots in ((PLATFORM_PROFILE, PLATFORM_ROOTS), (CLUSTER_PROFILE_PREFIX + "x", CLUSTER_ROOTS)):
        for root in roots:
            skills = sorted(md.parent.name for md in Path(root).glob(f"*/{SKILL_FILE}"))
            if not skills:
                problems.append(f"{root}: no skill found (expected the image's template tree)")
                continue
            if image_shipped_skill(skills[0], profile) != root:
                problems.append(f"{root}: {skills[0]} does not answer as shipped for {profile}")
            if profile == PLATFORM_PROFILE and image_shipped_skill(skills[0], DEFAULT_PROFILE) is not None:
                problems.append(f"{root}: {skills[0]} answers as shipped for the default profile")
            print(f"  ok   {root}: {len(skills)} skill(s), {skills[0]} shipped for {profile}")
    if problems:
        print("CHECK FAILED:\n  " + "\n  ".join(problems))
        return 1
    print("CHECK OK: skill_manage image-owned gate roots name the shipped trees")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(check_roots(sys.argv[1:]))

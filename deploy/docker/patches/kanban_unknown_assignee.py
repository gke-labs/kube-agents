"""Refuse a card whose assignee is not a profile the dispatcher can run.

Installed into the image at ``/opt/hermes/tools/kanban_unknown_assignee.py``
and wired in by ``deploy/docker/patches/apply_kanban_unknown_assignee.py`` at
one place: ``kanban_create`` in ``tools/kanban_tools.py`` consults
:func:`refuse_unknown_assignee` before it writes anything.

Why
---
The dispatcher starts a worker only for an assignee that names a profile. A
card for any other name is never claimed: it stays in ``ready``, and the card
that created it waits for it. On a dev install (2026-10-22, case
``cost-rightsizing-names-dependencies``) a Platform Agent card filed a card
for ``seeded-a-cluster-agent``. The real profile was
``cluster-<project>-seeded-a-us-central1-a``, which other cards of the same
run used. Nothing claimed the card, the parent ended ``blocked``, and the
harness waited out its 1,800 s delegation timeout (issue #3786).

The Platform persona tells the agent to resolve the name with
``get_cluster_profile_name`` first, and that tool's docstring says what
happens otherwise. A model that skips the step got no signal.

What this changes
-----------------
``kanban_create`` returns :func:`refusal_text` instead of creating a card
whose assignee is not dispatchable. Dispatchable means:

- the gateway's own profile (``default``);
- a Cluster Agent profile (a name that starts with ``cluster-``) whose folder
  under ``<data root>/profiles`` Hermes registered (``profile.yaml``) and whose
  scaffold finished (``USER.md``, written last). That is the test
  ``cluster_agent_profile.is_ready_profile`` makes, the one behind
  ``get_cluster_profile_name``'s ``exists``;
- any other profile that Hermes itself says exists (the dispatcher's own
  question), or whose folder holds ``profile.yaml`` or, for a profile made
  before Hermes wrote that file, ``config.yaml``.

A name that is no profile is never claimed by a worker. An unfinished Cluster
Agent profile is registered, so its worker starts, and stops at its preflight.
Either way the card is not worked. The refusal lists the profiles that can
take a card, the ones whose names are nearest to the refused one first, so the
model can pick the right one in its next call.

What it leaves alone
--------------------
A card with no assignee. A name in another case: ``create_task`` lower-cases
the assignee, so ``Platform`` lands as ``platform`` and is accepted as one.
And any case where the profiles folder cannot be read: the refusal is for a
name that is known to be wrong, never for a lookup that failed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

# Where Hermes keeps named profiles under the data root, and the variable that
# names the home when hermes_constants cannot be imported.
PROFILES_DIR = "profiles"
HOME_ENV = "HERMES_HOME"
# The gateway's own profile. It is the data root itself, not a folder under
# PROFILES_DIR, and the dispatcher can run it.
ROOT_PROFILE = "default"
# What `hermes profile create` writes when it registers a profile
# (profile_scaffold.PROFILE_MARKER), what a profile made before that file
# existed still holds (profile_plugins and the entrypoint read it as a real
# profile), what a Cluster Agent scaffold writes last
# (cluster_agent_profile.USER_MD_NAME), and how a Cluster Agent profile's name
# starts (agent_roster.CLUSTER_PROFILE_PREFIX).
PROFILE_MARKER = "profile.yaml"
LEGACY_PROFILE_MARKER = "config.yaml"
USER_MD_NAME = "USER.md"
CLUSTER_PROFILE_PREFIX = "cluster-"
# Folders under PROFILES_DIR that are not profiles start with this (`.deleted`).
HIDDEN_PREFIX = "."
# How many profile names the refusal lists before it counts the rest.
LISTED_LIMIT = 20
# What separates the words of a profile name, and the words every Cluster
# Agent name or a model's guess at one carries, which say nothing about which
# cluster was meant.
NAME_WORD_RE = re.compile(r"[^a-z0-9]+")
COMMON_NAME_WORDS = frozenset({"cluster", "agent", "profile"})


def _hermes_home() -> Optional[Path]:
    """The home of the running process, a profile's home inside a worker; None when unknown."""
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home() or "")
    except Exception:  # noqa: BLE001 - outside Hermes, the variable is all there is
        home = os.environ.get(HOME_ENV, "")
    return Path(home) if home else None


def _hermes_profile_exists(name: str) -> bool:
    """Whether Hermes says ``name`` is a profile: the dispatcher's question. False when it cannot be asked."""
    try:
        from hermes_cli import profiles

        return bool(profiles.profile_exists(name))
    except Exception:  # noqa: BLE001 - no answer is not a yes
        return False


def profiles_dir_for(home: Path) -> Path:
    """The profiles folder as seen from ``home``: a worker's home is one of its entries."""
    own = home / PROFILES_DIR
    if not own.is_dir() and home.parent.name == PROFILES_DIR:
        return home.parent
    return own


def read_profiles(profiles_dir: Path) -> Optional[Tuple[List[str], List[str]]]:
    """(profiles that can take a card, Cluster Agent profiles whose scaffold did not finish).

    None when the folder is absent or cannot be listed. An entry that cannot be
    read counts as able to take a card: it is not known to be wrong.
    """
    try:
        if not profiles_dir.is_dir():
            return None
        entries = sorted(profiles_dir.iterdir())
    except OSError:
        return None
    ready: List[str] = []
    unfinished: List[str] = []
    for entry in entries:
        name = entry.name
        if name.startswith(HIDDEN_PREFIX):
            continue
        try:
            if not entry.is_dir():
                continue
            registered = (entry / PROFILE_MARKER).is_file()
            if name.startswith(CLUSTER_PROFILE_PREFIX):
                if not registered:
                    continue
                if not (entry / USER_MD_NAME).is_file():
                    unfinished.append(name)
                    continue
            elif not registered and not (entry / LEGACY_PROFILE_MARKER).is_file():
                continue
        except OSError:
            pass
        ready.append(name)
    return ready, unfinished


def _name_words(name: str) -> frozenset:
    return frozenset(NAME_WORD_RE.split(name.lower())) - COMMON_NAME_WORDS - {""}


def refusal_text(name: str, ready: List[str], *, unfinished: bool) -> str:
    # The specialists first, then the Cluster Agents, the ones that share most
    # words with the refused name first: on a fleet larger than the list, the
    # profile the model meant must still be in view.
    words = _name_words(name)
    ordered = sorted(
        ready,
        key=lambda n: (n.startswith(CLUSTER_PROFILE_PREFIX), -len(words & _name_words(n)), n),
    )
    listed = ", ".join(f"`{n}`" for n in ordered[:LISTED_LIMIT]) or "none"
    more = len(ordered) - LISTED_LIMIT
    if more > 0:
        listed += f", and {more} more that are not shown"
    if unfinished:
        why = (
            f"`{name}` is a Cluster Agent profile whose scaffold did not finish. Its worker "
            f"would stop at its preflight, so the card would not be worked"
        )
    else:
        why = (
            f"`{name}` is not a profile on this install. No worker picks up a card for it: "
            f"the card would stay in `ready` and your card would wait for it"
        )
    return (
        f"kanban_create refused: {why} (#3786). Profiles that can take a card: {listed}. "
        f"Write a profile's name exactly as it is listed, and never compose one. If the "
        f"profile you need is not shown, look it up with your roster or your profile lookup "
        f"tool, if you have one; if the cluster has no ready profile, follow your "
        f"instructions for a cluster with no agent."
    )


def refuse_unknown_assignee(
    assignee: Any,
    profiles_dir: Optional[Path] = None,
    profile_exists: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """The refusal for an assignee that is not dispatchable, else None.

    ``assignee`` is the new card's, as ``kanban_create`` got it. ``profiles_dir``
    and ``profile_exists`` are for the tests; the handler leaves them out, the
    folder is found from the process's Hermes home, and Hermes is asked.
    """
    # create_task lower-cases the assignee (``normalize_profile_name``), so the
    # comparison is on the name the card would carry.
    name = str(assignee or "").strip().lower()
    if not name or name == ROOT_PROFILE:
        return None
    if profiles_dir is None:
        home = _hermes_home()
        if home is None:
            return None
        profiles_dir = profiles_dir_for(home)
    found = read_profiles(Path(profiles_dir))
    if found is None:
        return None
    ready, unfinished = found
    if name in ready:
        return None
    # For a profile that is not a Cluster Agent, Hermes' own answer is enough:
    # the dispatcher starts a worker for whatever it says exists.
    if not name.startswith(CLUSTER_PROFILE_PREFIX) and (profile_exists or _hermes_profile_exists)(name):
        return None
    return refusal_text(name, ready, unfinished=name in unfinished)

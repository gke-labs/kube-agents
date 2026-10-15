"""Refuse a Platform Agent worker's card for its own profile that would run
alongside it.

Installed into the image at ``/opt/hermes/tools/kanban_self_copy.py`` and
wired in by ``deploy/docker/patches/apply_kanban_self_copy.py`` at one place:
``kanban_create`` in ``tools/kanban_tools.py`` consults
:func:`refuse_self_copy` once it has read the caller's own card, before it
writes anything.

Why
---
On ``ci-kube-agents-eval-next`` run 2110536180175998976 (2026-10-15),
``pdb-remediation-pr`` repetition 2, platform card ``t_cd092e7d`` called
``kanban_create(assignee="platform", title="Propose PDB manifest for
checkout-gateway")`` and then, without waiting for it, ran the
``submit-suggestion`` skill itself. The new card's worker did the same job in
parallel. The person got two pull requests for one change, 30 seconds apart:
gke-agentic/kube-agents-evals-4-infra#276 from the parent and #277 from the
child (issue #3164).

A platform card assigned to ``platform`` and started at once is a second
Platform Agent worker on the same request. It starts cold, without the first
worker's context, and redoes the job. The platform persona already forbids it
for per-cluster cards, but nothing covered a general one.

One self-assignment is sanctioned. Platform SOUL section 0 says
``parents=[<your own card id>]`` "is the correct way to queue follow-up work
you are handing off": that card waits until the creator finishes, so the two
never overlap. The refusal is therefore narrow: the caller is a dispatcher
worker on a ``platform`` card, the new card is for ``platform``, and the
caller's card is not in its ``parents``.

What this changes
-----------------
``kanban_create`` returns :func:`refusal_text` instead of creating such a
card. The text says to do the work on the caller's own card and does not
mention ``parents``: offered that way out, a model that wanted to delegate
would queue the same job after itself and do it twice in sequence. A
person's card, a chat session's card, a Cluster Agent's card and a platform
worker's card for any other profile are untouched. The image's scheduling
repair (``kanban_scheduling.find_deadlocked_children``) never releases a
``platform`` card queued after a ``platform`` card, so a dependency block
can't turn a follow-up into a concurrent copy either.

What it doesn't stop: a worker that queues the job it is doing as a
follow-up of its own card, unprompted, still gets it done twice, one after
the other. Telling that apart from different follow-up work needs to know
what the cards are for.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

# The Platform Agent's Hermes profile, as cards name their assignee.
PLATFORM_PROFILE = "platform"


def refusal_text(self_tid: str) -> str:
    # No hint at `parents`: a model told how to queue the card after its own
    # would do the job, complete, and let the queued copy do it again.
    return (
        f"kanban_create refused: this card would go to `{PLATFORM_PROFILE}`, your own "
        f"profile, while you are still working {self_tid}. A card for `{PLATFORM_PROFILE}` "
        f"is a second you: its worker starts cold and redoes the job, which is how one "
        f"request got two pull requests (#3164). Do this work on your own card, and "
        f"delegate only to a Cluster Agent or another specialist."
    )


def refuse_self_copy(self_task: Any, assignee: Any, parents: Sequence[str]) -> Optional[str]:
    """The refusal for a concurrent self-copy, else None.

    ``self_task`` is the caller's own card as ``kanban_create`` read it (None
    unless the caller is a dispatcher worker), ``assignee`` the new card's
    and ``parents`` its parent ids.
    """
    if self_task is None:
        return None
    own = str(getattr(self_task, "assignee", None) or "").strip().lower()
    self_tid = getattr(self_task, "id", None)
    if own != PLATFORM_PROFILE or not self_tid:
        return None
    # create_task lower-cases the assignee (``normalize_profile_name``), so
    # "Platform" lands as `platform` and must be refused as one.
    if str(assignee or "").strip().lower() != PLATFORM_PROFILE:
        return None
    if self_tid in tuple(parents or ()):
        return None
    return refusal_text(self_tid)

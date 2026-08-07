"""GKE Stockout Investigator plugin.

Hermes imports this package and calls ``register(ctx)``. Registering the skill here is
what makes it resolvable — a SKILL.md sitting in the mounted plugin directory is not
discovered on its own, because plugin skills never enter the flat ``~/.hermes/skills/``
tree that the skill index scans.
"""

from pathlib import Path

SKILL_NAME = "gke-stockout-investigator"


def register(ctx):
    """Register the stockout skill as ``<plugin name>:gke-stockout-investigator``."""
    skill_path = Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"
    ctx.register_skill(
        SKILL_NAME,
        skill_path,
        description="Diagnose GKE scale-up stockout failures and propose a GitOps remediation.",
    )

"""Microsoft Teams platform plugin for Hermes' gateway and standalone sender."""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    import teams_relay_patch

    teams_relay_patch.register_plugin(ctx)

"""The one place agent-side code asks which stack mode this install runs.

The operator pins KUBEAGENTS_MODE into the managed .env, which
load_hermes_dotenv applies last with override=True and save_env_value refuses
to overwrite -- so the value here is the operator's answer, not the agent's.
No component reads the variable itself; it asks is_next(). The mode spec
(docs/designs/spec-mode-switch.md) holds the reader/writer pair to exactly
this module and the operator's managed-env builder, so the delivery mechanism
can change later without touching call sites.

Fail-closed on purpose: absent or unrecognized reads as "today", the same rule
the operator's renderMode applies, so the dark stack stays dark from both
sides of the boundary.
"""

import os

_ENV_KEY = "KUBEAGENTS_MODE"
_TODAY = "today"
_NEXT = "next"


def mode() -> str:
    """The resolved mode: "today" or "next", never anything else."""
    value = os.environ.get(_ENV_KEY, _TODAY)
    return value if value in (_TODAY, _NEXT) else _TODAY


def is_next() -> bool:
    """Whether this install runs the next stack (NATS + A2A gateway)."""
    return mode() == _NEXT

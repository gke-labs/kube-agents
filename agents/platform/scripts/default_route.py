"""The install's default route, as the operator rendered it.

The operator renders spec.harness.tuning.defaultRoute as A2A_DEFAULT_ADDRESSEE
on the gateway and on this container from one resolver
(resolveA2ADefaultRoute), and pins it in the managed .env, so the gateway and
the producers here read the same value. A producer that moves its work to a
`cluster` profile task under the session route (#6528) asks routes_to_sessions()
and keeps today's path otherwise.

Only the CRD's two values are routes. Anything else, unset included, reads as
`platform`, the gateway's own default, so a producer never leaves today's path
on a value the operator would not have rendered.
"""

from __future__ import annotations

import os

DEFAULT_ADDRESSEE_ENV = "A2A_DEFAULT_ADDRESSEE"
ROUTE_SESSION = "session"
ROUTE_PLATFORM = "platform"


def default_route() -> str:
    """ROUTE_SESSION when the rendered route is the session route, else ROUTE_PLATFORM."""
    if os.environ.get(DEFAULT_ADDRESSEE_ENV) == ROUTE_SESSION:
        return ROUTE_SESSION
    return ROUTE_PLATFORM


def routes_to_sessions() -> bool:
    return default_route() == ROUTE_SESSION

"""default_route: the install's rendered default route, as the platform
agent's producers read it.

Run: python3 -m unittest discover -s agents/platform/scripts -p 'test_default_route.py' -v
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import default_route  # noqa: E402


class DefaultRouteTest(unittest.TestCase):
    def route_with(self, value):
        env = {k: v for k, v in os.environ.items() if k != default_route.DEFAULT_ADDRESSEE_ENV}
        if value is not None:
            env[default_route.DEFAULT_ADDRESSEE_ENV] = value
        with mock.patch.dict(os.environ, env, clear=True):
            return default_route.default_route()

    def test_the_session_route_reads_as_session(self):
        self.assertEqual(self.route_with("session"), default_route.ROUTE_SESSION)

    def test_the_platform_route_reads_as_platform(self):
        self.assertEqual(self.route_with("platform"), default_route.ROUTE_PLATFORM)

    def test_an_unset_route_reads_as_platform(self):
        # The gateway's own default (config.go envOr) and the operator's
        # fallback for an unset field: today's path.
        self.assertEqual(self.route_with(None), "platform")
        self.assertEqual(self.route_with(""), "platform")

    def test_anything_else_reads_as_platform(self):
        # Only the CRD's two values are routes, as resolveA2ADefaultRoute has it:
        # a near-miss keeps today's path rather than moving a producer.
        for value in ("Session", " session", "session ", "sessions", "cluster", "true"):
            with self.subTest(value=value):
                self.assertEqual(self.route_with(value), "platform")

    def test_routes_to_sessions_is_the_session_route_alone(self):
        with mock.patch.dict(os.environ, {default_route.DEFAULT_ADDRESSEE_ENV: "session"}):
            self.assertTrue(default_route.routes_to_sessions())
        with mock.patch.dict(os.environ, {default_route.DEFAULT_ADDRESSEE_ENV: "platform"}):
            self.assertFalse(default_route.routes_to_sessions())


if __name__ == "__main__":
    unittest.main()

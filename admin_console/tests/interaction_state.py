"""Gives a test its own interaction state file instead of the one under $HOME."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from admin_console.chat.store import INTERACTION_STATE_ENV


def isolate_interaction_state(test: unittest.TestCase) -> None:
    """Point create_app's default store at a temporary file for this test.

    Test files run in parallel, so a file under $HOME would be shared between
    processes, and a test run would leave it behind on the developer's machine.
    """
    state = tempfile.TemporaryDirectory()
    test.addCleanup(state.cleanup)
    path = os.path.join(state.name, "interactions.db")
    env = patch.dict(os.environ, {INTERACTION_STATE_ENV: path})
    env.start()
    test.addCleanup(env.stop)

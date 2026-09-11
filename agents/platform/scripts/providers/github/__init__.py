#!/usr/bin/env python3
"""GitHub, as one directory.

Everything only GitHub knows is under here: which hostnames are its, how it
spells a repository, which paths its API serves, what its JSON means, and the
one status whose shared reading is wrong for it. Nothing outside imports any of
it except `providers.registry`, which imports the class and nothing else.
"""

from .forge import GitHubForge

__all__ = ["GitHubForge"]

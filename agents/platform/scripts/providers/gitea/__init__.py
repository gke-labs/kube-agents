#!/usr/bin/env python3
"""Gitea, as one directory.

Everything only Gitea knows is under here: how an administrator declares an
instance, how it spells a repository, which paths its API serves, what its
JSON means, and the statuses whose shared reading is wrong for it. Nothing
outside imports any of it except `providers.registry`, which imports the class
and nothing else.
"""

from .forge import GiteaForge

__all__ = ["GiteaForge"]

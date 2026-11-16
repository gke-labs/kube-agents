#!/usr/bin/env python3
"""Bitbucket Data Center: one package, one instance per declared host.

Everything that only Bitbucket Data Center knows is in this directory.
Bitbucket Cloud is a different API and has its own package, `../bitbucket/`.
The registry imports the class and nothing else here; see `../registry.py`.
"""

from .forge import BitbucketDataCenterForge

__all__ = ["BitbucketDataCenterForge"]

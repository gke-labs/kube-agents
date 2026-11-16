#!/usr/bin/env python3
"""Bitbucket Cloud, at bitbucket.org: one package, one instance per install.

Everything that only Bitbucket Cloud knows is in this directory. Bitbucket
Data Center is a different API and has its own package,
`../bitbucket_datacenter/`. The registry imports the class and nothing else
here; see `../registry.py`.
"""

from .forge import BitbucketCloudForge

__all__ = ["BitbucketCloudForge"]

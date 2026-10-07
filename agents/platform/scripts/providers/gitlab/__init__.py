#!/usr/bin/env python3
"""GitLab, gitlab.com and self-managed: one package, one instance per configured host.

Everything GitLab-specific in this broker is in this directory. The registry
imports the class and nothing else here; see `../registry.py`.
"""

from .forge import GitLabForge

__all__ = ["GitLabForge"]

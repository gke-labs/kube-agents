#!/usr/bin/env python3
"""Take python-telegram-bot off Hermes' lazy-install allowlist.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``.

The package is LGPL-licensed, and the image build leaves it out of the copy of
the Hermes image (the ``hermes-pruned`` stage). That alone does not keep it out
of an install: ``tools/lazy_deps.py`` holds an allowlist of packages Hermes
installs at run time, the image's ``HERMES_LAZY_INSTALL_TARGET`` points those
installs at the data volume, and the Telegram adapter asks for
``platform.telegram`` when someone configures that platform. One anchored edit
removes the entry, so ``ensure("platform.telegram")`` raises
``FeatureUnavailable`` and the adapter reports the dependency as missing.

Usage::

    python3 apply_no_telegram_lazy_install.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

LAZY_DEPS_FILE = "tools/lazy_deps.py"

# Derived against v2026.9.14. The feature key is the anchor, with its opening
# quote and the indent of a LAZY_DEPS entry, so a mention in a comment or a
# docstring does not count. The pinned version is part of the anchor on
# purpose: a bump that changes the spec is a reason to read this again.
TELEGRAM_ENTRY = '    "platform.telegram": ("python-telegram-bot[webhooks]==22.8",),\n'

PATCHED_MARKER = "kube-agents patch: platform.telegram is not lazy-installable"

TELEGRAM_ENTRY_PATCHED = (
    f"    # {PATCHED_MARKER}: its package is\n"
    "    # LGPL-licensed and this image does not carry or fetch it.\n"
)


def apply(root: Path) -> None:
    """Remove the entry under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, LAZY_DEPS_FILE, prefix="no_telegram_lazy_install")
    patch.refuse_if_patched(PATCHED_MARKER)
    patch.substitute(TELEGRAM_ENTRY, TELEGRAM_ENTRY_PATCHED, expected=1)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))

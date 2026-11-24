"""Where the agent's profiles are on the data volume.

The chat metrics listener runs in the gateway process and reads two kinds of
per-profile file at scrape time: the audit files (``skill_invocations.py``) and
the cron execution ledgers (``cron_runs.py``). Both start from the volume's
Hermes home and the profile homes under it.
"""

from __future__ import annotations

import os
from pathlib import Path

from common import audit_sink


def hermes_root() -> Path:
    """The data volume's Hermes home: the front door's home, with every named profile under ``profiles/``.

    Not ``HERMES_HOME`` as it stands: a gateway run under a named profile (the
    operator's ``platformFrontDoor``) has it set to ``<root>/profiles/<name>``,
    and the other profiles' files are beside that home, not under it. Hermes'
    own resolution is asked first; without it the profile home is mapped back
    to the root the same way.
    """
    try:
        from hermes_constants import get_default_hermes_root  # the running Hermes' own

        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001 - outside Hermes, or a Hermes without it: the environment is the resolution
        pass
    home = Path(os.environ.get(audit_sink.HERMES_HOME_ENV) or audit_sink.DEFAULT_HERMES_HOME)
    if home.parent.name == audit_sink.PROFILES_DIR_NAME:
        return home.parent.parent
    return home


def profile_homes(root: Path) -> list[Path]:
    """The front door's home and each named profile's under ``root``, the front door first.

    Raises OSError when the profiles directory is there and cannot be listed:
    a caller must tell "no profiles" from "could not look".
    """
    homes = [root]
    try:
        with os.scandir(root / audit_sink.PROFILES_DIR_NAME) as entries:
            homes += sorted(Path(entry.path) for entry in entries if entry.is_dir())
    except FileNotFoundError:
        pass
    return homes


def home_gone(home: Path) -> bool:
    """Whether a profile's home has been removed; False when that cannot be told."""
    try:
        os.stat(home)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False

#!/usr/bin/env python3
"""Convert the pod's SQLite databases out of WAL once the operator pins DELETE.

Hermes reads `database.journal_mode` from its config and creates a fresh database in
that mode, but it never downgrades one whose header already reads WAL:
apply_wal_with_fallback (hermes_state.py) returns an on-disk WAL database before it
consults the setting, because a live downgrade under a concurrent opener would destroy
committed-but-uncheckpointed frames. So on a volume that ran in WAL before the pin —
every install this was written for (#610) — the pin alone changes nothing, and the
databases keep corrupting on the gofer mount the pin exists to protect them from.

This is the one conversion that pin needs. docker-entrypoint.sh runs it as step 1.7, in
the container that owns the shared state, after the bootstrap lock and before anything
below opens a database. For each governed database whose header reads WAL it
checkpoints with TRUNCATE, switches to DELETE, confirms SQLite reports the switch, and
removes the `-wal`/`-shm` files a switch can leave behind. Nothing is touched unless the
managed config says `delete`, and nothing is touched while another connection holds the
database: the checkpoint reports busy, or the switch raises `database is locked`, and the
file is left exactly as it was for the next start to retry.

The governed set is every database the pinned base opens through
apply_wal_with_fallback, which is the only reader of the pin. Eight of them are resolved
from get_hermes_home() and so exist at the agent home and again under each
`profiles/<name>/`: `state.db`, `cron/executions.db`, `cron/notepad.db`, `projects.db`,
`verification_evidence.db`, `response_store.db`, `memory_store.db` and
`gateway/discord_message_recovery.db`. The ninth, `kanban.db`, is shared across profiles
(kanban_db.kanban_home anchors at the root) and lives at the agent home and under each
`kanban/boards/<slug>/`. Databases whose opener sets its own journal mode, such as the
session KV store this repository runs beside Hermes, are not this script's: the pin never
reaches them, so a conversion here would be undone at their next open.

`--check` is the non-owner's side of the same step: exit 1 while the managed config says
`delete` and any governed header still reads WAL, exit 0 otherwise, so a sidecar can wait
for the conversion instead of opening a database in the middle of it.

Usage:
    sqlite_journal_migrate.py --agent-home DIR --managed-config FILE [--check]
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

# The journal mode this script converts TO, and the only value of
# `database.journal_mode` that makes it act. Matches hermes_state.resolve_journal_mode.
JOURNAL_MODE_DELETE = "delete"
# Dotted path of the pin in the managed config.
CONFIG_SECTION = "database"
CONFIG_KEY = "journal_mode"

# SQLite file header: 100 bytes, with the write and read format versions at
# offsets 18 and 19. Both read 2 for a WAL database and 1 for a rollback-journal
# one (https://www.sqlite.org/fileformat.html#the_database_header).
HEADER_LENGTH = 100
HEADER_WRITE_VERSION_OFFSET = 18
HEADER_READ_VERSION_OFFSET = 19
HEADER_VERSION_WAL = 2
HEADER_VERSION_ROLLBACK = 1
HEADER_MODE_WAL = "wal"
HEADER_MODE_ROLLBACK = "rollback"

# The governed files and where Hermes keeps them. HOME_DATABASES are relative to a
# Hermes home (the agent home, and each profiles/<name>/ under it); each is opened
# through apply_wal_with_fallback by the module named beside it, at the pinned base.
HOME_DATABASES = (
    "state.db",  # hermes_state.SessionDB
    "cron/executions.db",  # cron/executions.py
    "cron/notepad.db",  # cron/notepad.py
    "projects.db",  # hermes_cli/projects_db.py
    "verification_evidence.db",  # agent/verification_evidence.py
    "response_store.db",  # gateway/platforms/api_server.py
    "memory_store.db",  # plugins/memory/holographic/store.py
    "gateway/discord_message_recovery.db",  # plugins/platforms/discord/recovery.py
)
# The board is shared across profiles (hermes_cli/kanban_db.py), so it is looked for
# at the agent home and under each named board only, never under profiles/.
KANBAN_DB_NAME = "kanban.db"
PROFILES_DIR = "profiles"
BOARDS_DIR = pathlib.Path("kanban") / "boards"
# The files WAL mode keeps beside a database. A clean switch removes them; an
# unclean earlier shutdown can leave a stale pair that a DELETE-mode opener ignores.
WAL_SIDECAR_SUFFIXES = ("-wal", "-shm")

# No waiting on a lock: a database another connection holds open is not ours to
# convert, and sqlite3.connect's timeout is what turns that into an immediate error.
LOCK_WAIT_SECONDS = 0.0
# `PRAGMA wal_checkpoint` returns (busy, log_frames, checkpointed_frames).
CHECKPOINT_BUSY_COLUMN = 0
CHECKPOINT_FRAMES_COLUMN = 2

LOG_PREFIX = "[sqlite-journal]"

EXIT_OK = 0
EXIT_WAL_REMAINS = 1


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", file=sys.stderr, flush=True)


def configured_journal_mode(managed_config: pathlib.Path) -> str | None:
    """The managed config's `database.journal_mode`, lower-cased, or None.

    None covers every way of not having a pin: no file, a file that does not parse,
    no `database` mapping, no key, a non-string value, or no YAML parser to read it
    with. All of them mean "do nothing", which is the safe answer for a script that
    rewrites databases.
    """
    try:
        import yaml
    except ImportError:
        log(f"WARN: no YAML parser available to read {managed_config}; leaving every database as it is")
        return None
    try:
        with managed_config.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError) as exc:
        log(f"WARN: cannot read {managed_config}: {exc}; leaving every database as it is")
        return None
    if not isinstance(config, dict):
        return None
    section = config.get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return None
    mode = section.get(CONFIG_KEY)
    if not isinstance(mode, str):
        return None
    return mode.strip().lower()


def governed_databases(agent_home: pathlib.Path) -> list[pathlib.Path]:
    """Every governed database that exists under `agent_home`, in a stable order."""
    homes = [agent_home] + sorted(
        path for path in (agent_home / PROFILES_DIR).glob("*") if path.is_dir()
    )
    candidates = [home / relative for home in homes for relative in HOME_DATABASES]
    candidates.append(agent_home / KANBAN_DB_NAME)
    candidates.extend(sorted((agent_home / BOARDS_DIR).glob(f"*/{KANBAN_DB_NAME}")))
    return [path for path in candidates if path.is_file()]


def header_journal_mode(path: pathlib.Path) -> str | None:
    """`wal`, `rollback`, or None when the header is absent or not one of the two.

    Read from the file, not through SQLite: opening a connection to ask would take
    the shared lock this script must not take on a database it is only inspecting,
    and the header is what SQLite itself consults to decide the mode on open.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(HEADER_LENGTH)
    except OSError as exc:
        log(f"WARN: cannot read {path}: {exc}")
        return None
    if len(header) < HEADER_LENGTH:
        return None
    versions = (header[HEADER_WRITE_VERSION_OFFSET], header[HEADER_READ_VERSION_OFFSET])
    if versions == (HEADER_VERSION_WAL, HEADER_VERSION_WAL):
        return HEADER_MODE_WAL
    if versions == (HEADER_VERSION_ROLLBACK, HEADER_VERSION_ROLLBACK):
        return HEADER_MODE_ROLLBACK
    return None


def convert(path: pathlib.Path) -> tuple[bool, str]:
    """Move one WAL database to DELETE. Returns (converted, detail).

    Every failure leaves the file as it was: the checkpoint is a no-op on failure, and
    `PRAGMA journal_mode=DELETE` either takes effect or raises. A busy checkpoint and a
    locked switch both mean another connection has the database open, which is the one
    case the script must not act in.
    """
    try:
        conn = sqlite3.connect(path, timeout=LOCK_WAIT_SECONDS, isolation_level=None)
    except sqlite3.Error as exc:
        return False, f"cannot open: {exc}"
    try:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[CHECKPOINT_BUSY_COLUMN]:
            return False, "checkpoint reported busy (another connection holds it open); left in WAL"
        row = conn.execute(f"PRAGMA journal_mode={JOURNAL_MODE_DELETE.upper()}").fetchone()
        mode = str(row[0]).strip().lower() if row and row[0] is not None else ""
        if mode != JOURNAL_MODE_DELETE:
            return False, f"journal_mode={JOURNAL_MODE_DELETE.upper()} returned {mode or 'no result'!r}; left as it was"
        frames = checkpoint[CHECKPOINT_FRAMES_COLUMN]
    except sqlite3.Error as exc:
        return False, f"{exc}; left in WAL"
    finally:
        conn.close()
    for suffix in WAL_SIDECAR_SUFFIXES:
        sidecar = path.with_name(path.name + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log(f"WARN: {sidecar}: could not remove: {exc}")
    return True, f"converted to {JOURNAL_MODE_DELETE} ({frames} WAL frames checkpointed)"


def wal_databases(agent_home: pathlib.Path) -> list[pathlib.Path]:
    return [path for path in governed_databases(agent_home) if header_journal_mode(path) == HEADER_MODE_WAL]


def migrate(agent_home: pathlib.Path, managed_config: pathlib.Path) -> int:
    """Convert every governed WAL database when the managed config pins DELETE."""
    configured = configured_journal_mode(managed_config)
    if configured != JOURNAL_MODE_DELETE:
        log(f"managed config pins journal_mode={configured or 'nothing'}; leaving every database as it is")
        return EXIT_OK
    pending = wal_databases(agent_home)
    if not pending:
        log(f"managed config pins journal_mode={JOURNAL_MODE_DELETE}; no governed database under {agent_home} reads WAL")
        return EXIT_OK
    for path in pending:
        converted, detail = convert(path)
        log(f"{'' if converted else 'WARN: '}{path}: {detail}")
    return EXIT_OK


def check(agent_home: pathlib.Path, managed_config: pathlib.Path) -> int:
    """Exit 1 while a conversion is still owed, 0 otherwise."""
    if configured_journal_mode(managed_config) != JOURNAL_MODE_DELETE:
        return EXIT_OK
    pending = wal_databases(agent_home)
    if not pending:
        return EXIT_OK
    for path in pending:
        log(f"{path} still reads WAL")
    return EXIT_WAL_REMAINS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agent-home", required=True, type=pathlib.Path, help="the volume Hermes runs from ($HERMES_HOME)")
    parser.add_argument("--managed-config", required=True, type=pathlib.Path, help="the managed scope's config.yaml")
    parser.add_argument("--check", action="store_true", help="report whether a conversion is still owed instead of running one")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.agent_home, args.managed_config)
    return migrate(args.agent_home, args.managed_config)


if __name__ == "__main__":
    sys.exit(main())

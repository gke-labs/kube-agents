#!/usr/bin/env python3
"""Remove finished kanban scratch workspaces, on the agent pod and in the sandbox.

Hermes deletes a card's scratch workspace from exactly one place:
``kanban_db._cleanup_workspace``, called by ``complete_task`` after the
transaction commits, best-effort with the exception swallowed. Nothing else in
``hermes_cli`` removes one and there is no periodic sweep, so two classes of
workspace accumulate forever.

The first is on the agent pod and has nothing to do with the sandbox. A card
that reaches a terminal state by any route other than ``kanban_complete`` --
the dashboard API, ``kanban move``, a CLI edit -- never reaches that call site,
and neither ``cancelled`` nor ``failed`` reaches it at all. Measured on a
month-old install: 33 scratch directories on disk, of which 20 were ``done``
and 2 ``cancelled``. Two thirds of the terminal ones had been missed, and all
but one of the ``done`` ones had no children, so the deliberate
active-children deferral is not the explanation.

The second is the sandbox's copy, and it is a consequence of the same
host-operates-on-a-guest-path shape as the rest of Hermes' SSH backend:
``_cleanup_workspace`` calls ``shutil.rmtree`` in the gateway process, on the
gateway's path. Under ``terminal.backend: ssh`` the directory the worker
actually wrote to is on the sandbox's own ReadWriteOnce volume, which that call
cannot see, so the sandbox side is never cleaned even on the one path that
works. ``docs/designs/agent-shell-sandboxing.md`` has the full account.

A sweep rather than a ``kanban_task_completed`` plugin hook, deliberately. The
hook fires on precisely the path Hermes already handles, and the leak is in the
paths that have no hook; a reconciler is also self-healing after any missed
event, where a hook is one more thing that can miss one. It would fire in the
worker process too, so it would need the same SSH call regardless.

Authority is the board DB, never a directory listing. The removable set is
derived entirely from task rows, and the sandbox's listing is used only to
narrow it -- so the model, which owns the account that listing comes from,
can at worst hide a directory from the sweep or name one that does not exist.
It cannot name a path into the delete set. That also keeps the sweep away from
the task-shaped directories other code paths leave outside the kanban roots:
a live sandbox has ``/opt/data/tmp/t_384aaaba`` and ``/opt/data/gitops/
t_dc3f1647``, and a ``find -name 't_*'`` would have eaten both.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sandbox_exec  # noqa: E402

# The statuses Hermes itself treats as terminal, from the child query in
# ``_cleanup_workspace``. Used for both halves of the decision: a task is a
# candidate when its own status is here, and it is deferred while any child's
# is not. ``blocked`` is deliberately absent -- a blocked card is still live and
# its workspace is still the thing it will resume into.
TERMINAL = ("done", "archived", "failed", "cancelled")

# Dispatcher-minted workspace names. Anchored, and the only shape either side
# of the sweep will act on.
TASK_DIR = re.compile(r"^t_[0-9a-f]+$")

# `ls` and `rm` by absolute path, so the sandbox command cannot be redirected by
# the model's ~/.bashrc. Bash sources it even for `ssh host cmd`, and this is
# the one call in the repository that authenticates as the model's own account
# (see `_sandbox_names`), but a bash function name cannot contain a slash and a
# non-interactive shell does not expand aliases, so neither of these two can be
# shadowed by anything that file does.
REMOTE_LS = "/bin/ls"
REMOTE_RM = "/bin/rm"

# How long each of those may take. Listing one directory is fast on any install
# and slow only on one that is unreachable, which the connect timeout already
# catches; removing is generous because one call unlinks every workspace the
# sweep decided on, and a run that gave up half way would leave the board and
# the disk disagreeing until the next one.
LS_TIMEOUT_SECONDS = 60
RM_TIMEOUT_SECONDS = 120

# How much of a failed `rm`'s stderr reaches the warning. Enough for the first
# few paths it could not unlink; the rest is the same message repeated once per
# workspace, and the sweep's summary is read in a chat message.
STDERR_EXCERPT_CHARS = 400


class Sweep:
    """What one run found, and what went wrong doing it."""

    def __init__(self) -> None:
        self.local_removed: list[str] = []
        self.remote_removed: list[str] = []
        self.warnings: list[str] = []


def _removable(db: Path, root: Path) -> list[Path]:
    """Workspace paths for terminal scratch tasks on one board.

    Mirrors ``_cleanup_workspace``: only ``workspace_kind='scratch'``, and
    deferred while any child is non-terminal so a handoff artifact the child
    still needs is not pulled out from under it.

    Read-only, and on purpose. The dispatcher writes this DB continuously and a
    housekeeping job has no business taking its write lock; nothing here needs
    to record that the directory is gone, because the next run rediscovers the
    same rows and finds nothing left to delete.
    """
    paths: list[Path] = []
    uri = f"file:{db}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, workspace_path FROM tasks "
            f"WHERE workspace_kind = 'scratch' AND workspace_path IS NOT NULL "
            f"AND status IN ({','.join('?' * len(TERMINAL))})",
            TERMINAL,
        ).fetchall()
        for row in rows:
            active_child = conn.execute(
                "SELECT 1 FROM task_links l JOIN tasks t ON t.id = l.child_id "
                f"WHERE l.parent_id = ? AND t.status NOT IN "
                f"({','.join('?' * len(TERMINAL))}) LIMIT 1",
                (row["id"], *TERMINAL),
            ).fetchone()
            if active_child:
                continue
            candidate = Path(row["workspace_path"])
            # Containment, in the same terms as Hermes' own guard: a strict
            # child of this board's workspaces root, one level down, with the
            # name the dispatcher mints. A board's `default_workdir` can pair
            # `workspace_kind='scratch'` with a path pointing at a real source
            # tree (#28818), and this is what keeps that out of the delete set.
            if candidate.parent != root or not TASK_DIR.match(candidate.name):
                continue
            paths.append(candidate)
    return paths


def _sandbox_names(root: Path, sweep: Sweep) -> set[str] | None:
    """Workspace directory names present in the sandbox under *root*.

    Used only to narrow the DB-derived set, so that a run does not re-issue a
    remove for every terminal task the board has ever held -- 371 rows on the
    install this was written against, against a command line that has a length
    limit. None means the question could not be answered and the remote half of
    this root should be skipped rather than guessed at.

    Connects as the model's own account rather than as ``hermes``. That is the
    reverse of `sandbox_exec`'s default and the reason is a permission one:
    the workspaces are ``agent:agent 755`` all the way down, so uid 1001 cannot
    unlink inside them. It is safe here for a reason that does not generalise
    to the module's other callers -- this call consumes no output as a fact
    about the cluster, and the worst a hijacked ``rm`` could do is what uid 1000
    can already do to its own files.
    """
    try:
        result = sandbox_exec.run(
            [REMOTE_LS, "-1", "--", str(root)],
            principal=sandbox_exec.TERMINAL_PRINCIPAL,
            timeout=LS_TIMEOUT_SECONDS,
        )
    except sandbox_exec.SandboxUnavailable as exc:
        sweep.warnings.append(f"could not reach the shell sandbox: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - a sweep must not die on one root
        sweep.warnings.append(f"listing {root} in the sandbox failed: {exc}")
        return None
    if result.returncode != 0:
        # A root that does not exist there yet is the normal case for a board
        # no card has ever run on, and is not worth reporting.
        return set()
    return {
        name for name in result.stdout.split("\n") if TASK_DIR.match(name.strip())
    }


def _remove_remote(root: Path, names: list[str], sweep: Sweep) -> None:
    """Remove *names* under *root* in the sandbox, in one call."""
    try:
        result = sandbox_exec.run(
            [REMOTE_RM, "-rf", "--", *[str(root / name) for name in names]],
            principal=sandbox_exec.TERMINAL_PRINCIPAL,
            timeout=RM_TIMEOUT_SECONDS,
        )
    except sandbox_exec.SandboxUnavailable as exc:
        sweep.warnings.append(f"could not reach the shell sandbox: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        sweep.warnings.append(f"removing workspaces under {root} failed: {exc}")
        return
    if result.returncode != 0:
        sweep.warnings.append(
            f"sandbox rm under {root} exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:STDERR_EXCERPT_CHARS]}"
        )
        return
    sweep.remote_removed.extend(names)


def _remove_local(path: Path, sweep: Sweep) -> None:
    """Remove one workspace on the agent pod.

    `is_symlink` before `is_dir`, because `is_dir` follows one and `rmtree`
    refuses a symlink outright. Unlinking the link is the whole job in that
    case, and it is not the shape the dispatcher creates.
    """
    try:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            return
    except OSError as exc:
        sweep.warnings.append(f"could not remove {path}: {exc}")
        return
    sweep.local_removed.append(path.name)


def sweep_boards(kanban) -> Sweep:  # noqa: ANN001 - the hermes_cli module
    """Run the reconciliation across every board on disk."""
    sweep = Sweep()
    remote = sandbox_exec.sandbox_enabled()
    for board in kanban.list_boards():
        slug = board.get("slug") or board.get("id")
        if not slug:
            continue
        try:
            db = kanban.kanban_db_path(slug)
            root = kanban.workspaces_root(slug)
        except Exception as exc:  # noqa: BLE001
            sweep.warnings.append(f"board {slug!r}: could not resolve paths: {exc}")
            continue
        if not db.is_file():
            continue
        try:
            candidates = _removable(db, root)
        except sqlite3.Error as exc:
            sweep.warnings.append(f"board {slug!r}: reading {db} failed: {exc}")
            continue
        if not candidates:
            continue

        for path in candidates:
            _remove_local(path, sweep)

        if not remote:
            continue
        present = _sandbox_names(root, sweep)
        if not present:
            continue
        names = sorted({p.name for p in candidates} & present)
        if names:
            _remove_remote(root, names, sweep)
    return sweep


def main() -> int:
    try:
        from hermes_cli import kanban_db  # noqa: PLC0415 - not importable in tests
    except ImportError as exc:
        print(
            f"⚠️ **Kanban workspace GC:** hermes_cli.kanban_db is not importable "
            f"({exc}); no workspaces were reconciled this tick."
        )
        return 0

    sweep = sweep_boards(kanban_db)

    # Stdout is the delivery channel, so a clean sweep says nothing at all. What
    # it removed goes to stderr, which the scheduler logs and does not deliver:
    # a job that announced its own housekeeping every night would train the room
    # to ignore it, and the failure it must not hide is the one below.
    if sweep.local_removed or sweep.remote_removed:
        sys.stderr.write(
            f"kanban_workspace_gc: removed {len(sweep.local_removed)} on the agent "
            f"pod, {len(sweep.remote_removed)} in the sandbox\n"
        )
    if sweep.warnings:
        print(
            "⚠️ **Kanban workspace GC could not finish:**\n"
            + "\n".join(f"- {warning}" for warning in sweep.warnings)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

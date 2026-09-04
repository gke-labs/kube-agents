#!/usr/bin/env python3
"""Reclaims and archives stale Kanban tasks for a given route name.

Executed inside the platform-agent gateway pod by both the shell scenario harness
(scenarios/lib/common.sh) and the E2E pytest suite (tests/e2e/test_stockout_investigation.py).
"""

import json
import os
import subprocess
import sys
import time

_MAX_ATTEMPTS = 3
_RETRY_SLEEP_SECONDS = 0.5
_ACTIVE_STATUSES = ("running", "claimed", "ready", "blocked", "todo")
_CLAIMED_STATUSES = ("running", "claimed")
_DEFAULT_HERMES_HOME = "/opt/data"
_FALLBACK_TMP_HOME = "/tmp"


def clean_stale_tasks(route_name: str) -> int:
    cmd_env = dict(os.environ)
    cmd_env["HOME"] = _FALLBACK_TMP_HOME
    cmd_env.setdefault("HERMES_HOME", _DEFAULT_HERMES_HOME)

    archived_count = 0

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            res = subprocess.run(
                ["hermes", "kanban", "ls", "--json"],
                env=cmd_env,
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(res.stdout)
        except Exception as exc:
            sys.stderr.write(f"kanban ls failed: {exc}\n")
            return -1

        tasks = data.get("tasks") if isinstance(data, dict) else data
        matching = [
            t
            for t in (tasks or [])
            if t.get("id")
            and str(t.get("title", "")).startswith(route_name)
            and t.get("status") in _ACTIVE_STATUSES
        ]

        if not matching:
            break

        errors = []
        for t in matching:
            tid = t["id"]
            status = t.get("status")
            if status in _CLAIMED_STATUSES:
                rec = subprocess.run(
                    ["hermes", "kanban", "reclaim", tid],
                    env=cmd_env,
                    capture_output=True,
                    text=True,
                )
                if rec.returncode != 0:
                    errors.append(f"reclaim {tid}: {rec.stderr.strip()}")
            arc = subprocess.run(
                ["hermes", "kanban", "archive", tid],
                env=cmd_env,
                capture_output=True,
                text=True,
            )
            if arc.returncode != 0:
                errors.append(f"archive {tid}: {arc.stderr.strip()}")
            else:
                archived_count += 1

        if not errors:
            break

        if attempt < _MAX_ATTEMPTS:
            time.sleep(_RETRY_SLEEP_SECONDS)
        else:
            sys.stderr.write(f"kanban archive errors: {'; '.join(errors)}\n")
            return -1

    # Post-cleanup verification pass: report lingering tasks as a diagnostic warning.
    # Non-fatal: an alert arriving during verification is indistinguishable from a stale card,
    # so a warning preserves visibility without turning a race into a failed test.
    verify = subprocess.run(
        ["hermes", "kanban", "ls", "--json"],
        env=cmd_env,
        capture_output=True,
        text=True,
    )
    if verify.returncode == 0:
        try:
            vdata = json.loads(verify.stdout)
            vtasks = vdata.get("tasks") if isinstance(vdata, dict) else vdata
            lingering = [
                t.get("id")
                for t in (vtasks or [])
                if str(t.get("title", "")).startswith(route_name)
                and t.get("status") in _ACTIVE_STATUSES
            ]
            if lingering:
                sys.stderr.write(
                    f"warning: {len(lingering)} task(s) still active for route '{route_name}': {lingering}\n"
                )
        except Exception:
            pass

    return archived_count


def main() -> None:
    route_name = sys.argv[1] if len(sys.argv) > 1 else ""
    archived = clean_stale_tasks(route_name)
    if archived < 0:
        sys.exit(1)
    print(archived)


if __name__ == "__main__":
    main()

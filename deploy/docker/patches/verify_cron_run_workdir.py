#!/usr/bin/env python3
"""Build-time behaviour gate for the cron run-workdir patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after ``apply_cron_run_workdir.py``. The applier proves its anchor matched;
this constructs the shipped ``_CronRunScope`` and asserts on what it records:

- a job with no ``workdir`` gets ``<home>/cron/runs/<job>_<execution>`` as the
  terminal's cwd for its task id, and the directory exists;
- the session cwd passed to ``set_session_vars`` stays empty, so context-file
  loading is unchanged;
- a job with a configured ``workdir`` still gets exactly that, and no run
  directory is made;
- a home where the directory cannot be created leaves the run with no recorded
  cwd, as before the patch, instead of failing it.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

JOB_ID = "compliance-audit"
EXECUTION_ID = "ex-0001"


def main(root: Path) -> None:
    sys.path.insert(0, str(root))
    import cron.scheduler as scheduler
    import gateway.session_context as session_context
    from tools.terminal_tool import get_session_cwd

    passed_cwds = []
    real_set = session_context.set_session_vars

    def recording_set(*args, **kwargs):
        passed_cwds.append(kwargs.get("cwd", ""))
        return real_set(*args, **kwargs)

    session_context.set_session_vars = recording_set
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "profiles" / "platform"
        home.mkdir(parents=True)
        scheduler._hermes_home = home

        scope = scheduler._CronRunScope({"id": JOB_ID, "execution_id": EXECUTION_ID}, JOB_ID, EXECUTION_ID)
        want = home / "cron" / "runs" / f"{JOB_ID}_{EXECUTION_ID}"
        got = get_session_cwd(scope.task_id)
        if got != str(want):
            problems.append(f"a job with no workdir recorded {got!r}, not {str(want)!r}")
        if not want.is_dir():
            problems.append(f"{want} was not created")
        if passed_cwds[-1:] != [""]:
            problems.append(f"the session cwd changed to {passed_cwds[-1:]!r}; context-file loading would follow it")

        configured = Path(tmp) / "configured"
        configured.mkdir()
        job = {"id": "with-workdir", "execution_id": EXECUTION_ID, "workdir": str(configured)}
        scope = scheduler._CronRunScope(job, "with-workdir", EXECUTION_ID)
        if get_session_cwd(scope.task_id) != str(configured):
            problems.append(f"a configured workdir recorded {get_session_cwd(scope.task_id)!r}")
        if (home / "cron" / "runs" / f"with-workdir_{EXECUTION_ID}").exists():
            problems.append("a run directory was made for a job with a configured workdir")

        blocked = Path(tmp) / "blocked"
        blocked.mkdir()
        (blocked / "cron").write_text("not a directory", encoding="utf-8")
        scheduler._hermes_home = blocked
        scope = scheduler._CronRunScope({"id": "blocked", "execution_id": EXECUTION_ID}, "blocked", EXECUTION_ID)
        if get_session_cwd(scope.task_id) is not None:
            problems.append("a run directory that could not be made still left a recorded cwd")

    if problems:
        raise SystemExit("verify_cron_run_workdir:\n" + "\n".join(problems))
    print("verify_cron_run_workdir: ok (run dir recorded and made, session cwd unchanged, "
          "configured workdir kept, unmakeable dir falls back)")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))

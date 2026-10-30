#!/usr/bin/env python3
"""Build-time behaviour gate for the per-job turn-budget patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after ``apply_cron_job_max_turns.py``. The applier proves its anchors
matched; this imports the shipped scheduler and asserts on what the helper
resolves:

- a job carrying ``max_turns`` above the profile's runs under it;
- a job value below the profile's does not lower the budget: the larger of
  the two wins, so an operator's bigger ``maxTurns`` still reaches every job;
- a job without one keeps the profile's ``agent.max_turns``;
- a roster value written as a string still resolves, as the profile's does;
- a roster value Hermes would read as unlimited (0, negative, bool, a
  non-numeric string) is ignored and the profile's value stands;
- the setup path calls the helper, so the cap reaches ``AIAgent``.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

JOB_CAP = 250
PROFILE_CAP = 90
PROFILE = {"agent": {"max_turns": PROFILE_CAP}}


def main(root: Path) -> None:
    sys.path.insert(0, str(root))
    import cron.scheduler as scheduler

    resolve = scheduler._resolve_cron_max_iterations
    problems = []
    if resolve({"max_turns": JOB_CAP}, PROFILE) != JOB_CAP:
        problems.append("a job's own max_turns did not bound its run")
    if resolve({}, PROFILE) != PROFILE_CAP:
        problems.append("a job without max_turns did not keep the profile's budget")
    if resolve({"max_turns": str(JOB_CAP)}, PROFILE) != JOB_CAP:
        problems.append("a string max_turns on the job did not resolve")
    if resolve({"max_turns": None}, PROFILE) != PROFILE_CAP:
        problems.append("a null max_turns on the job did not fall back to the profile")
    if resolve({"max_turns": PROFILE_CAP - 1}, PROFILE) != PROFILE_CAP:
        problems.append("a job max_turns below the profile's lowered the budget; the profile's larger value must win")
    if resolve({"max_turns": JOB_CAP}, {"agent": {"max_turns": JOB_CAP + 50}}) != JOB_CAP + 50:
        problems.append("a profile above the job's value did not win; an operator's larger maxTurns must reach every job")
    for lifting in (0, -1, True, "unlimited", "abc"):
        if resolve({"max_turns": lifting}, PROFILE) != PROFILE_CAP:
            problems.append(f"a job max_turns of {lifting!r} did not fall back to the profile; it would lift the cap")
    if "_resolve_cron_max_iterations(job, _cfg)" not in inspect.getsource(
        scheduler._resolve_cron_agent_setup
    ):
        problems.append("_resolve_cron_agent_setup does not call the helper")

    if problems:
        raise SystemExit("verify_cron_job_max_turns:\n" + "\n".join(problems))
    print(
        "verify_cron_job_max_turns: ok (job cap wins, profile cap is the fallback, "
        "string and null job values handled, lifting values ignored, the larger of job and profile wins, setup calls the helper)"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))

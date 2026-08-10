"""Build-time behaviour gate for the cron Tirith-scan patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_cron_tirith_scan.py``. The applier proves the anchor
matched and ``tools/approval.py`` still parses; this proves the patched gate
actually refuses a command it used to run.

That distinction is the whole reason this file exists. Every way this patch can
fail is silent: a moved anchor, a scan that never reaches the wire, a block
result in the wrong shape, an import cycle that makes ``cron_tirith_block``
raise and take the terminal tool down with it. None of them announce themselves
— they look exactly like a cron run that had nothing to flag. So the checks
below drive the real ``check_all_command_guards`` and assert on its return
value, rather than grepping for the text the applier just inserted.

The unit suite in ``test_cron_tirith_scan.py`` covers ``cron_tirith_scan.py`` in
isolation and cannot cover any of this, because the edit lives inside Hermes'
own module.

What is faked, and why that is still a real test
------------------------------------------------
Tirith is not in the image at build time — it downloads to ``$HERMES_HOME/bin``
on first use at runtime — so ``check_command_security`` is replaced with a stub
that returns a fixed verdict. Everything between that stub and the caller is
the shipped code: the real ``check_all_command_guards``, the real patched cron
arm, the real ``cron_tirith_block``, the real ``_format_tirith_description``,
the real config read. The session predicates are pinned so a build host's
environment cannot decide which branch is under test.
"""

from __future__ import annotations

import os
import sys
import types

# Freeze this before tools.approval is imported: _YOLO_MODE_FROZEN is read at
# module import time precisely so nothing can flip it later, so a build host
# exporting it would silently turn every check below into a pass.
os.environ.pop("HERMES_YOLO_MODE", None)
os.environ.pop("HERMES_EXEC_ASK", None)

# A command with a hostile-looking URL and no dangerous *pattern* at all. That
# is the case the patch exists for: DANGEROUS_PATTERNS has nothing to say about
# it, so if the block does not come from the content scan it does not come at
# all.
COMMAND = "kubectl apply -f https://raw.githubusercontent.example/x/manifest.yaml"

FINDING_TITLE = "Lookalike TLD"
FINDING_DESC = "the host uses a TLD easily confused with a legitimate one"
BLOCK_RESULT = {
    "action": "block",
    "findings": [
        {
            "rule_id": "lookalike_tld",
            "severity": "HIGH",
            "title": FINDING_TITLE,
            "description": FINDING_DESC,
        }
    ],
    "summary": "lookalike tld",
}
ALLOW_RESULT = {"action": "allow", "findings": [], "summary": ""}

failures: list[str] = []
scan_calls: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok  {label}")


def install_scanner(behaviour) -> None:
    """Replace tools.tirith_security with a stub both import sites will find.

    Both the patched arm (via ``cron_tirith_block``) and upstream's deny arm
    import ``check_command_security`` lazily from inside a function, so seeding
    ``sys.modules`` ahead of the first call is enough to intercept both — which
    is the point: the deny arm must go on using the scanner exactly as before.
    """
    import tools

    stub = types.ModuleType("tools.tirith_security")

    def check_command_security(command: str) -> dict:
        scan_calls.append(command)
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour

    stub.check_command_security = check_command_security
    sys.modules["tools.tirith_security"] = stub
    tools.tirith_security = stub


def main() -> int:
    import tools.approval as ap
    import tools.cron_tirith_scan as cts

    # The patch must be reachable by the name the wiring imports it under. A
    # COPY to the wrong path fails here rather than at 06:20 on a Sunday.
    if not callable(getattr(cts, "cron_tirith_block", None)):
        failures.append("tools.cron_tirith_scan.cron_tirith_block is missing")
        print("\nVERIFY FAILED:\n  " + failures[0])
        return 1

    state = {"cron": True, "mode": "approve"}

    # Pin the session predicates. Each is upstream's own and is not what this
    # patch changes; leaving them live would let a build host's environment
    # pick the branch under test.
    ap._is_interactive_cli = lambda: False
    ap._is_gateway_approval_context = lambda: False
    ap._is_cron_approval_context = lambda: state["cron"]
    ap._get_cron_approval_mode = lambda: state["mode"]
    # approvals.mode: "off" is a global bypass that returns before the cron arm
    # is reached. Pin it away from "off" so the checks below test the cron arm
    # and not the bypass.
    ap._get_approval_mode = lambda: "smart"
    ap._command_matches_permanent_allowlist = lambda command: False
    ap._match_user_deny_rule = lambda command: None

    # The premise of COMMAND: no pattern matches it, so any refusal below is
    # attributable to the content scan and to nothing else. Assert it rather
    # than assume it — a future DANGEROUS_PATTERNS entry could quietly make
    # every check pass for the wrong reason.
    is_dangerous, _key, _desc = ap.detect_dangerous_command(COMMAND)
    check("the probe command matches no dangerous pattern", is_dangerous, False)
    check("and is not hardline", ap.detect_hardline_command(COMMAND)[0], False)

    # --- cron_mode: approve, scanner objects → blocked ----------------------
    # This is the finding. Before the patch this returned approved with the
    # scanner never consulted.
    install_scanner(BLOCK_RESULT)
    del scan_calls[:]
    result = ap.check_all_command_guards(COMMAND, "local")
    check("cron approve: scanner was consulted", len(scan_calls), 1)
    check("cron approve: command refused", result.get("approved"), False)
    message = result.get("message") or ""
    check("refusal names the finding", FINDING_TITLE in message, True)
    # The wiring passes Hermes' own _format_tirith_description in. That renderer
    # includes the finding's description; cron_tirith_scan's local fallback does
    # not. So this assertion is what proves the real one is wired, rather than
    # the stand-in the unit suite uses.
    check("refusal uses Hermes' own finding renderer", FINDING_DESC in message, True)
    check("refusal says not to retry it", "Do not retry" in message, True)

    # --- cron_mode: approve, scanner clean → runs ---------------------------
    # The other half of the requirement: a watchdog that is not doing anything
    # objectionable must not be slowed down or stopped.
    install_scanner(ALLOW_RESULT)
    del scan_calls[:]
    result = ap.check_all_command_guards(COMMAND, "local")
    check("cron approve: clean command scanned", len(scan_calls), 1)
    check("cron approve: clean command allowed", result.get("approved"), True)
    check("clean command carries no message", result.get("message"), None)

    # --- cron_mode: deny is untouched ---------------------------------------
    # Upstream already scans on this arm. The patch must not scan twice (a scan
    # is a subprocess spawn) and must leave upstream's message alone.
    state["mode"] = "deny"
    install_scanner(BLOCK_RESULT)
    del scan_calls[:]
    result = ap.check_all_command_guards(COMMAND, "local")
    check("cron deny: scanned exactly once", len(scan_calls), 1)
    check("cron deny: still refused", result.get("approved"), False)
    check(
        "cron deny: still upstream's message",
        "cron jobs run without a user present" in (result.get("message") or ""),
        True,
    )
    state["mode"] = "approve"

    # --- non-cron non-interactive is untouched ------------------------------
    # The patch widens the scan to the cron approve path and to nothing else. A
    # kanban worker's headless session keeps upstream's fall-through, for better
    # or worse; changing that is a separate decision with a separate blast
    # radius.
    state["cron"] = False
    install_scanner(BLOCK_RESULT)
    del scan_calls[:]
    result = ap.check_all_command_guards(COMMAND, "local")
    check("non-cron headless: not scanned", len(scan_calls), 0)
    check("non-cron headless: allowed as before", result.get("approved"), True)
    state["cron"] = True

    # --- a missing scanner honours security.tirith_fail_open ----------------
    # Tirith genuinely is absent from the image at build time, so this is the
    # state a first cron run hits before the background install lands. It must
    # not brick the roster. The default (fail open) is checked against the real
    # config read; the fail-closed variant pins the loader because the build
    # image has no config.yaml to set it in.
    install_scanner(ImportError("No module named 'tirith'"))
    del scan_calls[:]
    result = ap.check_all_command_guards(COMMAND, "local")
    check("scanner absent, fail-open default: allowed", result.get("approved"), True)

    real_loader = cts._load_config_readonly
    try:
        cts._load_config_readonly = lambda: {"security": {"tirith_fail_open": False}}
        result = ap.check_all_command_guards(COMMAND, "local")
        check("scanner absent, fail-closed: refused", result.get("approved"), False)
        check(
            "fail-closed refusal names the flag",
            "tirith_fail_open" in (result.get("message") or ""),
            True,
        )

        # --- the documented opt-out actually opts out -----------------------
        # approvals.cron_scan: false is the escape hatch for a watchdog that
        # trips the scanner for a reason the operator has examined. If it did
        # not work, the only remaining lever would be turning Tirith off for
        # interactive sessions too.
        cts._load_config_readonly = lambda: {"approvals": {"cron_scan": False}}
        install_scanner(BLOCK_RESULT)
        del scan_calls[:]
        result = ap.check_all_command_guards(COMMAND, "local")
        check("cron_scan: false — not scanned", len(scan_calls), 0)
        check("cron_scan: false — allowed", result.get("approved"), True)
    finally:
        cts._load_config_readonly = real_loader

    if failures:
        print("\nVERIFY FAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print("\ncron_tirith_scan verify OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

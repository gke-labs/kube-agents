"""Unit tests for the cron Tirith scan installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py'

These cover the decision in isolation — is this command refused, and on whose
authority. They cannot cover the wiring, because the edit lands inside Hermes'
own ``tools/approval.py``; ``verify_cron_tirith_scan.py`` drives that inside the
image at build time. The applier tests at the bottom cover the third failure
mode, an anchor that has drifted out from under the patch.
"""

import ast
import pathlib
import tempfile
import unittest

import apply_cron_tirith_scan as applier
from cron_tirith_scan import (
    CRON_SCAN_KEY,
    cron_scan_enabled,
    cron_tirith_block,
    tirith_fail_open,
)

COMMAND = "kubectl apply -f https://raw.githubusercontent.example/x/manifest.yaml"

FINDING = {
    "rule_id": "lookalike_tld",
    "severity": "HIGH",
    "title": "Lookalike TLD",
    "description": "the host uses a TLD easily confused with a legitimate one",
}
BLOCK = {"action": "block", "findings": [FINDING], "summary": "lookalike tld"}
WARN = {"action": "warn", "findings": [FINDING], "summary": "lookalike tld"}
ALLOW = {"action": "allow", "findings": [], "summary": ""}


def scanner(result):
    """A stand-in for check_command_security that records what it saw."""
    seen = []

    def scan(command):
        seen.append(command)
        if isinstance(result, BaseException):
            raise result
        return result

    scan.seen = seen
    return scan


def config(**sections):
    return dict(sections)


class VerdictTest(unittest.TestCase):
    """A verdict from the scanner refuses the command; nothing else does."""

    def test_a_clean_command_runs(self):
        scan = scanner(ALLOW)
        self.assertIsNone(
            cron_tirith_block(COMMAND, scan=scan, load_config=dict)
        )
        self.assertEqual([COMMAND], scan.seen)

    def test_a_block_verdict_refuses(self):
        out = cron_tirith_block(COMMAND, scan=scanner(BLOCK), load_config=dict)
        self.assertIsNotNone(out)
        self.assertFalse(out["approved"])

    def test_a_warn_verdict_also_refuses(self):
        """warn and block are both verdicts, and no one is here to weigh them.

        Interactively, warn means "show the user and let them decide". On an
        unattended run there is no one to decide, so the only two options are
        refuse or run it anyway — and running it anyway is the behaviour this
        patch exists to remove. This matches the cron deny arm, which has always
        treated ("block", "warn") as one case.
        """
        out = cron_tirith_block(COMMAND, scan=scanner(WARN), load_config=dict)
        self.assertIsNotNone(out)
        self.assertFalse(out["approved"])

    def test_an_unknown_action_is_not_a_verdict(self):
        """Anything that is not block/warn runs. Fail towards upstream."""
        weird = {"action": "quarantine", "findings": [FINDING]}
        self.assertIsNone(
            cron_tirith_block(COMMAND, scan=scanner(weird), load_config=dict)
        )

    def test_an_empty_result_is_not_a_verdict(self):
        """A scanner returning None must not become a refusal by accident."""
        self.assertIsNone(
            cron_tirith_block(COMMAND, scan=scanner(None), load_config=dict)
        )

    def test_the_refusal_has_the_shape_the_caller_branches_on(self):
        """check_all_command_guards returns exactly these two keys."""
        out = cron_tirith_block(COMMAND, scan=scanner(BLOCK), load_config=dict)
        self.assertEqual({"approved", "message"}, set(out))
        self.assertIsInstance(out["message"], str)

    def test_the_refusal_carries_the_finding_and_says_not_to_retry(self):
        out = cron_tirith_block(COMMAND, scan=scanner(BLOCK), load_config=dict)
        self.assertIn(FINDING["title"], out["message"])
        self.assertIn("Do not retry", out["message"])

    def test_the_injected_renderer_is_what_writes_the_message(self):
        """The wiring passes Hermes' own formatter; honour it.

        Without this the model would read a second, subtly different rendering
        of the same finding depending on whether a human was present.
        """
        out = cron_tirith_block(
            COMMAND,
            scan=scanner(BLOCK),
            load_config=dict,
            describe=lambda result: "RENDERED BY HERMES",
        )
        self.assertIn("RENDERED BY HERMES", out["message"])


class ScannerUnavailableTest(unittest.TestCase):
    """Tirith downloads at runtime, so "not installed yet" is a real state."""

    IMPORT_ERROR = ImportError("No module named 'tirith'")

    def test_the_default_is_to_let_the_command_through(self):
        """Matches security.tirith_fail_open's default and the deny arm's.

        A first cron run can fire before the background install lands. Refusing
        every command until it does would take the roster down for a reason
        that has nothing to do with the command.
        """
        self.assertIsNone(
            cron_tirith_block(
                COMMAND, scan=scanner(self.IMPORT_ERROR), load_config=dict
            )
        )

    def test_fail_closed_refuses_and_names_the_flag(self):
        out = cron_tirith_block(
            COMMAND,
            scan=scanner(self.IMPORT_ERROR),
            load_config=lambda: config(security={"tirith_fail_open": False}),
        )
        self.assertFalse(out["approved"])
        self.assertIn("tirith_fail_open", out["message"])

    def test_tirith_disabled_outranks_fail_closed(self):
        """An operator who switched the scanner off is not then blocked by its
        absence. Same ordering as the deny arm: fail_open is only consulted
        when tirith_enabled is true."""
        self.assertIsNone(
            cron_tirith_block(
                COMMAND,
                scan=scanner(self.IMPORT_ERROR),
                load_config=lambda: config(
                    security={"tirith_enabled": False, "tirith_fail_open": False}
                ),
            )
        )

    def test_only_ImportError_is_absorbed(self):
        """Anything else is a bug, and a swallowed bug here is an unscanned run.

        check_command_security is documented to handle its own expected
        failures — spawn errors, timeouts, odd exit codes — internally. An
        exception escaping it means something unforeseen, which must surface as
        a tool error rather than be quietly reinterpreted as "allow".
        """
        with self.assertRaises(RuntimeError):
            cron_tirith_block(
                COMMAND,
                scan=scanner(RuntimeError("tirith wrapper bug")),
                load_config=dict,
            )


class OptOutTest(unittest.TestCase):
    """approvals.cron_scan is the one supported way to turn this off."""

    def test_false_skips_the_scan_entirely(self):
        scan = scanner(BLOCK)
        self.assertIsNone(
            cron_tirith_block(
                COMMAND,
                scan=scan,
                load_config=lambda: config(approvals={CRON_SCAN_KEY: False}),
            )
        )
        self.assertEqual([], scan.seen, "opting out must not still pay for a spawn")

    def test_absent_means_on(self):
        self.assertTrue(cron_scan_enabled({}))
        self.assertTrue(cron_scan_enabled({"approvals": {}}))
        self.assertTrue(cron_scan_enabled(None))

    def test_a_malformed_approvals_block_is_not_an_opt_out(self):
        """Someone who cannot write the key has not decided anything."""
        self.assertTrue(cron_scan_enabled({"approvals": "cron_scan: false"}))
        self.assertTrue(cron_scan_enabled({"approvals": ["cron_scan"]}))

    def test_explicit_true_is_on(self):
        self.assertTrue(cron_scan_enabled({"approvals": {CRON_SCAN_KEY: True}}))


class ConfigReadTest(unittest.TestCase):
    """An unreadable config must not silently decide security policy."""

    def test_a_raising_loader_falls_back_to_scanning(self):
        def boom():
            raise OSError("config.yaml is a directory")

        scan = scanner(BLOCK)
        out = cron_tirith_block(COMMAND, scan=scan, load_config=boom)
        self.assertEqual([COMMAND], scan.seen)
        self.assertFalse(out["approved"])

    def test_a_none_config_falls_back_to_scanning(self):
        scan = scanner(BLOCK)
        out = cron_tirith_block(COMMAND, scan=scan, load_config=lambda: None)
        self.assertEqual([COMMAND], scan.seen)
        self.assertFalse(out["approved"])

    def test_fail_open_defaults(self):
        self.assertTrue(tirith_fail_open(None))
        self.assertTrue(tirith_fail_open({}))
        self.assertTrue(tirith_fail_open({"security": {}}))
        self.assertTrue(tirith_fail_open({"security": "nope"}))
        self.assertFalse(tirith_fail_open({"security": {"tirith_fail_open": False}}))


# The shape of the upstream cron arm the applier rewrites, reduced to something
# that parses on its own. Byte-identical to /opt/hermes/tools/approval.py for the
# four anchored lines — if upstream moves, the image build fails at the applier,
# not here.
UPSTREAM = '''\
def check_all_command_guards(command, env_type):
    is_cli = _is_interactive_cli()
    is_gateway = _is_gateway_approval_context()
    is_ask = False
    if not is_cli and not is_gateway and not is_ask:
        # Cron sessions: respect cron_mode config
        if _is_cron_approval_context():
            if _get_cron_approval_mode() == "deny":
                return {"approved": False, "message": "denied"}
        return {"approved": True, "message": None}
    return {"approved": True, "message": None}
'''


class ApplierTest(unittest.TestCase):
    """The applier must land the edit or fail the build. Never in between."""

    def write(self, source):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: [p.unlink() for p in sorted(root.rglob("*")) if p.is_file()]
        )
        target = root / "tools" / "approval.py"
        target.parent.mkdir(parents=True)
        target.write_text(source)
        return root, target

    def test_the_edit_lands_and_still_parses(self):
        root, target = self.write(UPSTREAM)
        applier.apply(root)
        patched = target.read_text()
        ast.parse(patched)
        self.assertIn("from tools.cron_tirith_scan import cron_tirith_block", patched)
        self.assertIn("describe=_format_tirith_description", patched)

    def test_the_deny_arm_survives(self):
        """The patch adds an arm; it must not consume the one already there."""
        root, target = self.write(UPSTREAM)
        applier.apply(root)
        patched = target.read_text()
        self.assertIn('if _cron_mode == "deny":', patched)
        self.assertIn('"message": "denied"', patched)

    def test_the_scan_is_skipped_on_the_deny_arm(self):
        """Upstream already scans there; scanning twice is a second subprocess."""
        root, target = self.write(UPSTREAM)
        applier.apply(root)
        self.assertIn('if _cron_mode != "deny":', target.read_text())

    def test_the_mode_is_read_once(self):
        """Two reads are two config loads, and can straddle a config reload."""
        root, target = self.write(UPSTREAM)
        applier.apply(root)
        patched = target.read_text()
        self.assertEqual(1, patched.count("_get_cron_approval_mode()"))

    def test_a_missing_anchor_fails_the_build(self):
        root, _ = self.write(UPSTREAM.replace("is_ask", "is_asked"))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("found 0", str(caught.exception))

    def test_a_duplicated_anchor_fails_the_build(self):
        """Ambiguity is a failure, not a coin toss: replace() would hit both."""
        root, _ = self.write(UPSTREAM + "\n\n" + UPSTREAM.replace(
            "def check_all_command_guards", "def check_all_command_guards_two"
        ))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("found 2", str(caught.exception))

    def test_a_missing_file_fails_the_build(self):
        root = pathlib.Path(tempfile.mkdtemp())
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("does not exist", str(caught.exception))

    def test_the_patch_is_not_applied_twice(self):
        """Re-running must fail rather than nest a second copy of the arm."""
        root, _ = self.write(UPSTREAM)
        applier.apply(root)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("found 0", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the per-job turn-budget patch (gke-labs/kube-agents#1947).

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

``UPSTREAM`` is the genuine region of ``cron/scheduler.py`` at the pinned
Hermes (v2026.9.14), copied verbatim, so both anchors are exercised against
the text they were derived from. The patched helper and setup function are
then run with a stub for ``hermes_cli.config.resolve_turn_limit`` and for the
setup's other collaborators. ``verify_cron_job_max_turns.py`` runs the real
scheduler in the image build.
"""

import contextlib
import io
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from apply_cron_job_max_turns import BUILD_MARKER, HELPER_NAME, RELATIVE, apply

UPSTREAM = r'''@dataclass
class _CronAgentSetup:
    """Everything ``AIAgent(...)`` needs that is resolved from job + config (or a preflight block)."""
    blocked: Optional[_RunResult] = None
    model: str = ""
    runtime: dict = None
    prefill_messages: Any = None
    max_iterations: Any = None
    reasoning_config: Any = None
    fallback_model: Any = None
    credential_pool: Any = None


def _resolve_cron_agent_setup(job: dict, job_id: str, job_name: str, jc) -> _CronAgentSetup:
    """Resolve model/runtime/reasoning/pool for the run, in the original gate order: exfil guard ->
    preflight (may block) -> runtime (+ fallback chain) -> credential pool -> MCP."""
    _cfg = jc.cfg
    setup = _CronAgentSetup(model=jc.model)
    setup.prefill_messages = _load_prefill_messages(_cfg, job_id)

    # resolve_turn_limit() honors none/unlimited (sys.maxsize) and explicit 0 / null.
    from hermes_cli.config import resolve_turn_limit as _resolve_turn_limit
    _mt = _cfg.get("agent", {}).get("max_turns")
    if _mt is None:
        _mt = _cfg.get("max_turns")
    setup.max_iterations = _resolve_turn_limit(_mt)

    # Runtime backstop (CWE-200/522): fail closed BEFORE resolution on a provider/base_url pair
    # that would ship a stored credential off-host; hand-written jobs bypass create-time checks.
    _guard_job_credential_exfil(job)

    setup.blocked = _preflight_or_block(job, job_id, job_name, _cfg)
    if setup.blocked is not None:
        return setup
    return setup

'''

UNLIMITED = 10**9


def _resolve_turn_limit(raw, default=UNLIMITED):
    """The shape of hermes_cli.config.resolve_turn_limit: None, a bool and an
    unparseable string give the default; a number or numeric string parses;
    zero and below are unlimited."""
    if raw is None or isinstance(raw, bool):
        return default
    try:
        n = int(float(str(raw).strip().lower()))
    except ValueError:
        return UNLIMITED if str(raw).strip().lower() in {"none", "unlimited", "inf"} else default
    return UNLIMITED if n <= 0 else n


class CronJobMaxTurnsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "hermes"
        target = self.root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM, encoding="utf-8")

    def apply_quietly(self):
        with contextlib.redirect_stdout(io.StringIO()):
            apply(self.root)

    def namespace(self):
        """The (patched or not) module text, run with stubs for what it imports and calls."""
        config = types.ModuleType("hermes_cli.config")
        config.resolve_turn_limit = _resolve_turn_limit
        config.TURN_LIMIT_UNLIMITED = UNLIMITED
        modules = {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.config": config}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(sys.modules, modules))
        namespace = {
            "dataclass": dataclass, "Optional": Optional, "Any": Any, "_RunResult": object,
            "_load_prefill_messages": lambda cfg, job_id: None,
            "_guard_job_credential_exfil": lambda job: None,
            "_preflight_or_block": lambda job, job_id, job_name, cfg: None,
        }
        exec(compile((self.root / RELATIVE).read_text(encoding="utf-8"), RELATIVE, "exec"), namespace)
        return namespace

    def setup_for(self, job, profile):
        jc = types.SimpleNamespace(cfg=profile, model="m")
        return self.namespace()["_resolve_cron_agent_setup"](job, job.get("id", "j"), "job", jc)

    def test_unpatched_upstream_ignores_a_jobs_own_budget(self):
        setup = self.setup_for({"max_turns": 250}, {"agent": {"max_turns": 90}})
        self.assertEqual(setup.max_iterations, 90)

    def test_a_job_with_max_turns_runs_under_it(self):
        self.apply_quietly()
        setup = self.setup_for({"max_turns": 250}, {"agent": {"max_turns": 90}})
        self.assertEqual(setup.max_iterations, 250)

    def test_a_job_without_max_turns_keeps_the_profiles_budget(self):
        self.apply_quietly()
        setup = self.setup_for({}, {"agent": {"max_turns": 90}})
        self.assertEqual(setup.max_iterations, 90)

    def test_a_null_job_value_falls_back_to_the_profile(self):
        self.apply_quietly()
        setup = self.setup_for({"max_turns": None}, {"agent": {"max_turns": 90}})
        self.assertEqual(setup.max_iterations, 90)

    def test_the_legacy_root_key_is_still_the_last_fallback(self):
        self.apply_quietly()
        setup = self.setup_for({}, {"max_turns": 70})
        self.assertEqual(setup.max_iterations, 70)

    def test_no_budget_anywhere_is_the_resolvers_default_as_before(self):
        self.apply_quietly()
        setup = self.setup_for({}, {})
        self.assertEqual(setup.max_iterations, UNLIMITED)

    def test_a_job_value_that_would_lift_the_cap_is_ignored(self):
        # 0, a negative number, a bool and a non-numeric string all resolve to
        # unlimited in Hermes; the profile's value stands instead.
        self.apply_quietly()
        helper = self.namespace()[HELPER_NAME]
        for lifting in (0, -1, True, "unlimited", "abc"):
            with self.subTest(value=lifting):
                self.assertEqual(helper({"max_turns": lifting}, {"agent": {"max_turns": 90}}), 90)

    def test_a_job_value_below_the_profiles_does_not_lower_the_budget(self):
        # The entry raises its job above the profile, never under it: an operator's
        # larger tuning.platform.maxTurns still reaches every job.
        self.apply_quietly()
        helper = self.namespace()[HELPER_NAME]
        self.assertEqual(helper({"max_turns": 100}, {"agent": {"max_turns": 150}}), 150)
        self.assertEqual(helper({"max_turns": 250}, {"agent": {"max_turns": 300}}), 300)
        self.assertEqual(helper({"max_turns": 250}, {"agent": {"max_turns": 150}}), 250)

    def test_a_profile_with_no_budget_is_still_unlimited_when_the_job_names_none(self):
        self.apply_quietly()
        helper = self.namespace()[HELPER_NAME]
        self.assertEqual(helper({"max_turns": 0}, {}), UNLIMITED)

    def test_the_helper_resolves_a_string_from_the_roster(self):
        self.apply_quietly()
        helper = self.namespace()[HELPER_NAME]
        self.assertEqual(helper({"max_turns": "250"}, {"agent": {"max_turns": 90}}), 250)

    def test_a_second_run_is_refused(self):
        self.apply_quietly()
        with self.assertRaises(SystemExit) as caught:
            apply(self.root)
        self.assertIn(BUILD_MARKER, str(caught.exception))

    def test_a_moved_resolution_anchor_fails_the_build(self):
        target = self.root / RELATIVE
        target.write_text(
            UPSTREAM.replace('    _mt = _cfg.get("agent", {}).get("max_turns")\n',
                             '    _mt = (_cfg.get("agent") or {}).get("max_turns")\n'),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            apply(self.root)

    def test_a_moved_setup_def_anchor_fails_the_build(self):
        target = self.root / RELATIVE
        target.write_text(
            UPSTREAM.replace("def _resolve_cron_agent_setup(job: dict, job_id: str, job_name: str, jc)",
                             "def _resolve_cron_agent_setup(job, job_id, job_name, jc)"),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            apply(self.root)


if __name__ == "__main__":
    unittest.main()

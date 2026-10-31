"""Unit tests for the cron run-workdir patch (gke-labs/kube-agents#2989).

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

``UPSTREAM`` is the genuine ``_CronRunScope`` region of ``cron/scheduler.py``
at the pinned Hermes (v2026.9.14), copied verbatim, so the anchor is exercised
against the text it was derived from. The patched class is then run with stub
modules for its two imports. ``verify_cron_run_workdir.py`` runs the real one
in the image build.
"""

import contextlib
import io
import logging
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from apply_cron_run_workdir import BUILD_MARKER, RELATIVE, RUNS_PARTS, apply

UPSTREAM = r'''class _CronRunScope:
    """Per-run ContextVar / tool-cwd scope for ``run_job`` (ContextVars, not os.environ, so
    parallel jobs don't clobber each other). Construct before the try, ``enter()`` as its first
    statement, ``exit()`` in the finally — every setter here has a matching reset there.

    HERMES_SESSION_* are deliberately NOT seeded from job["origin"]: it is delivery metadata, not
    a sender, and terminal/tts/skills/send_message tools would act as if the origin user were
    driving the agent. Delivery reads job["origin"] / HERMES_CRON_AUTO_DELIVER_* directly.
    """

    def __init__(self, job: dict, job_id: str, execution_id: Optional[str]):
        from gateway.session_context import set_session_vars, _VAR_MAP
        from tools.terminal_tool import record_session_cwd

        self._var_map = _VAR_MAP
        # Resolve workdir BEFORE set_session_vars so it owns the _SESSION_CWD set/clear.
        self.workdir = _resolve_job_workdir(job, job_id)
        self._ctx_tokens = set_session_vars(
            platform="",
            chat_id="",
            chat_name="",
            # Cron can't receive completions after its turn; async delegation output could
            # otherwise route to an unrelated chat via the ambient session key => inline delegation.
            # We clear the HERMES_SESSION_* routing keys just below, so an async delegation's completion
            # event carries session_key="" — _enrich_async_delegation_routing cannot resolve it and
            # _inject_watch_notification drops it ("no routing metadata"). And by the time a child finishes,
            # run_job has already shipped the job's final response via _deliver_result; there is no turn
            # left to re-enter. (Worse, get_current_session_key() can fall back to the ambient os.environ
            # HERMES_SESSION_KEY, which risks routing a cron subagent's output into an unrelated user chat.)
            # Declaring the channel stateless routes delegate_task to its existing inline/synchronous path,
            # so results return within the job's own turn. See declare_stateless_channel(). Upstream:
            # #53027, #63142.
            async_delivery=False,
            cwd=self.workdir or "",
        )
        for name in _CRON_DELIVERY_VARS:
            _VAR_MAP[name].set("")
        # Workdir binds to the per-run task id (tool-layer cwd authority) instead of mutating
        # global TERMINAL_CWD; _SESSION_CWD above remains the prompt/context-file authority.
        self.task_id = f"cron:{job_id}:{execution_id or job.get('execution_id') or uuid.uuid4().hex}"
        if self.workdir:
            record_session_cwd(self.task_id, self.workdir)
        self._cron_session_var = _VAR_MAP["HERMES_CRON_SESSION"]
        self._cron_session_token = None
        self._non_dispatcher_token = None

'''


class CronRunWorkdirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "hermes"
        target = self.root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM, encoding="utf-8")
        self.home = self.tmp / "profiles" / "platform"
        self.home.mkdir(parents=True)

    def apply_quietly(self):
        with contextlib.redirect_stdout(io.StringIO()):
            apply(self.root)

    def scope_class(self):
        """The (patched or not) class, with stubs for what it imports and reads."""
        self.recorded, self.session_cwds = {}, []
        session = types.ModuleType("gateway.session_context")
        var = types.SimpleNamespace(set=lambda value: None)
        session._VAR_MAP = {"HERMES_CRON_SESSION": var, "X": var}
        session.set_session_vars = lambda **kw: self.session_cwds.append(kw.get("cwd")) or []
        terminal = types.ModuleType("tools.terminal_tool")
        terminal.record_session_cwd = lambda key, cwd: self.recorded.__setitem__(key, cwd)
        modules = {"gateway": types.ModuleType("gateway"), "gateway.session_context": session,
                   "tools": types.ModuleType("tools"), "tools.terminal_tool": terminal}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(sys.modules, modules))
        namespace = {
            "Optional": Optional, "uuid": uuid, "logger": logging.getLogger("cron-test"),
            "_CRON_DELIVERY_VARS": ("X",), "_get_hermes_home": lambda: self.home,
            "_resolve_job_workdir": lambda job, job_id: (job.get("workdir") or "").strip() or None,
        }
        exec(compile((self.root / RELATIVE).read_text(encoding="utf-8"), RELATIVE, "exec"), namespace)
        return namespace["_CronRunScope"]

    def test_unpatched_upstream_records_no_cwd_for_a_job_without_one(self):
        scope = self.scope_class()({"id": "compliance-audit"}, "compliance-audit", "ex1")
        self.assertNotIn(scope.task_id, self.recorded)

    def test_a_job_without_a_workdir_gets_a_run_directory_of_its_own(self):
        self.apply_quietly()
        scope = self.scope_class()({"id": "compliance-audit"}, "compliance-audit", "ex1")
        run_dir = self.home.joinpath(*RUNS_PARTS, "compliance-audit_ex1")
        self.assertEqual(self.recorded[scope.task_id], str(run_dir))
        self.assertTrue(run_dir.is_dir())
        # The session cwd, and so context-file loading, is untouched.
        self.assertEqual(self.session_cwds, [""])

    def test_two_runs_of_one_job_get_different_directories(self):
        self.apply_quietly()
        cls = self.scope_class()
        first = cls({"id": "compliance-audit"}, "compliance-audit", "ex1")
        second = cls({"id": "compliance-audit"}, "compliance-audit", "ex2")
        self.assertNotEqual(self.recorded[first.task_id], self.recorded[second.task_id])

    def test_a_job_id_cannot_steer_the_directory_out_of_the_runs_root(self):
        self.apply_quietly()
        scope = self.scope_class()({"id": "x"}, "../../escape", "ex1")
        recorded = Path(self.recorded[scope.task_id])
        self.assertEqual(recorded.parent, self.home.joinpath(*RUNS_PARTS))

    def test_the_directory_name_is_what_the_prune_accepts(self):
        # kanban_workspace_gc.CRON_RUN_NAME is ASCII; a name it refuses is never swept.
        import re
        self.apply_quietly()
        scope = self.scope_class()({"id": "x"}, "審査 audit/1", "ex1")
        name = Path(self.recorded[scope.task_id]).name
        self.assertRegex(name, r"^[A-Za-z0-9._-]+$")

    def test_a_configured_workdir_is_kept(self):
        self.apply_quietly()
        configured = self.tmp / "configured"
        configured.mkdir()
        scope = self.scope_class()({"id": "j", "workdir": str(configured)}, "j", "ex1")
        self.assertEqual(self.recorded[scope.task_id], str(configured))
        self.assertFalse(self.home.joinpath(*RUNS_PARTS).exists())

    def test_a_directory_that_cannot_be_made_leaves_the_run_as_before(self):
        self.apply_quietly()
        (self.home / RUNS_PARTS[0]).write_text("not a directory", encoding="utf-8")
        with self.assertLogs("cron-test", level="WARNING"):
            scope = self.scope_class()({"id": "j"}, "j", "ex1")
        self.assertNotIn(scope.task_id, self.recorded)

    def test_a_second_run_is_refused(self):
        self.apply_quietly()
        with self.assertRaises(SystemExit) as caught:
            apply(self.root)
        self.assertIn(BUILD_MARKER, str(caught.exception))

    def test_a_moved_anchor_fails_the_build(self):
        target = self.root / RELATIVE
        target.write_text(UPSTREAM.replace("        if self.workdir:\n            record_session_cwd(",
                                           "        if self.workdir is not None:\n            record_session_cwd("),
                          encoding="utf-8")
        with self.assertRaises(SystemExit):
            apply(self.root)


if __name__ == "__main__":
    unittest.main()

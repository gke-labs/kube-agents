#!/usr/bin/env python3
"""Tests for scripts/check_bounded_concurrency.py."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_bounded_concurrency import (  # noqa: E402
    ALLOWLIST,
    BOUND_CALLED_FROM_MAIN,
    BOUND_FIXED_SIZE,
    BOUND_GO_LIMIT,
    BOUND_SINGLETON,
    BOUND_STARTUP,
    EXIT_FAIL,
    EXIT_OK,
    check,
    main,
    scan_go,
    scan_python,
)

# A FastAPI module with every Python shape the check distinguishes.
PYTHON_FIXTURE = textwrap.dedent(
    '''
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler

    app = object()
    _lock = threading.Lock()
    _digest_thread = None
    _POOL = ThreadPoolExecutor(max_workers=4)


    @app.post("/unbounded")
    def post_unbounded(payload):
        threading.Thread(target=_work, args=(payload,), daemon=True).start()
        return {}


    @app.post("/pooled")
    def post_pooled(payload):
        _POOL.submit(_work, payload)
        return {}


    @app.post("/guarded")
    def post_guarded(payload):
        _arm_digest()
        return {}


    def _arm_digest():
        global _digest_thread
        with _lock:
            if _digest_thread is not None and _digest_thread.is_alive():
                return
            _digest_thread = threading.Thread(target=_work, daemon=True)
            _digest_thread.start()


    @app.get("/through-helper")
    def get_through_helper():
        return _lookup("x")


    def _lookup(url):
        def _view():
            pass
        threading.Thread(target=_view, name="lookup", daemon=True).start()
        return None


    class _Workers:
        def __init__(self, workers):
            self.threads = [threading.Thread(target=self._work, daemon=True) for _ in range(workers)]

        def _work(self):
            pass


    class Door(BaseHTTPRequestHandler):
        def do_POST(self):
            self._proxy()

        def _proxy(self):
            threading.Thread(target=_work, daemon=True).start()


    def _work(*args):
        pass


    def _background():
        threading.Thread(target=_work, daemon=True).start()


    def main():
        threading.Thread(target=_background, daemon=True).start()
        _Workers(3)
    '''
)

GO_FIXTURE = textwrap.dedent(
    """
    package callout

    import "golang.org/x/sync/errgroup"

    func handle(msg string) {
    \tgo func() {
    \t\t_ = msg
    \t}()
    }

    func pooled(msgs []string) {
    \tvar g errgroup.Group
    \tg.SetLimit(4)
    \tfor _, m := range msgs {
    \t\tgo work(m)
    \t}
    }

    func gated(msgs []string) {
    \tsem := make(chan struct{}, 8)
    \tfor _, m := range msgs {
    \t\tsem <- struct{}{}
    \t\tgo work(m)
    \t}
    }

    func work(m string) {}

    func main() {
    \tgo serve()
    \tif err := run(); err != nil {
    \t\tpanic(err)
    \t}
    }

    func run() error {
    \tgo func() {}()
    \treturn nil
    }

    func serve() {}
    """
)

GO_TEST_FIXTURE = textwrap.dedent(
    """
    package callout

    func TestSpawn(t *testing.T) {
    \tgo func() {}()
    }
    """
)

PYTHON_TARGET = "svc/server.py"
GO_DIR = "svc/go"
ALLOWLIST_NAME = "allow.txt"


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _run(root: Path, allowlist_text: str | None = None) -> tuple[int, str, str]:
    allowlist = root / ALLOWLIST_NAME
    if allowlist_text is not None:
        allowlist.write_text(allowlist_text, encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    code = check(root, (PYTHON_TARGET,), (GO_DIR,), allowlist, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.py = _write(self.root, PYTHON_TARGET, PYTHON_FIXTURE)
        _write(self.root, f"{GO_DIR}/callout.go", GO_FIXTURE)
        _write(self.root, f"{GO_DIR}/callout_test.go", GO_TEST_FIXTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def _python_spawns(self):
        return {(s.function, s.line): s for s in scan_python(self.py, self.root)}

    def _flagged_python(self):
        return sorted(s.function for s in scan_python(self.py, self.root) if s.flagged)

    def test_a_route_starting_a_thread_per_request_is_flagged(self):
        flagged = self._flagged_python()
        self.assertIn("post_unbounded", flagged)
        spawn = next(s for s in scan_python(self.py, self.root) if s.function == "post_unbounded")
        self.assertEqual(("post_unbounded",), spawn.reachable_from)
        self.assertIsNone(spawn.bounded_by)

    def test_a_spawn_reached_through_a_helper_names_the_route(self):
        spawn = next(s for s in scan_python(self.py, self.root) if s.function == "_lookup")
        self.assertTrue(spawn.flagged)
        self.assertEqual(("get_through_helper",), spawn.reachable_from)

    def test_a_request_handler_method_is_an_entry_point(self):
        spawn = next(s for s in scan_python(self.py, self.root) if s.function == "Door._proxy")
        self.assertTrue(spawn.flagged)
        self.assertEqual(("Door._proxy", "Door.do_POST"), spawn.reachable_from)

    def test_an_executor_is_not_a_spawn(self):
        self.assertNotIn("post_pooled", [s.function for s in scan_python(self.py, self.root)])

    def test_the_is_alive_guard_is_bounded(self):
        spawn = next(s for s in scan_python(self.py, self.root) if s.function == "_arm_digest")
        self.assertEqual(("post_guarded",), spawn.reachable_from)
        self.assertEqual(BOUND_SINGLETON, spawn.bounded_by)
        self.assertFalse(spawn.flagged)

    def test_a_fixed_size_pool_is_bounded(self):
        spawn = next(s for s in scan_python(self.py, self.root) if s.function == "_Workers.__init__")
        self.assertEqual(BOUND_FIXED_SIZE, spawn.bounded_by)
        self.assertFalse(spawn.flagged)

    def test_main_and_unreached_functions_are_not_flagged(self):
        by_function = {s.function: s for s in scan_python(self.py, self.root)}
        self.assertEqual(BOUND_STARTUP, by_function["main"].bounded_by)
        self.assertEqual((), by_function["_background"].reachable_from)
        self.assertFalse(by_function["_background"].flagged)
        self.assertEqual(["Door._proxy", "_lookup", "post_unbounded"], self._flagged_python())

    def test_a_bare_goroutine_in_a_go_function_is_flagged(self):
        spawns = {(s.function, s.line): s for s in scan_go(self.root / GO_DIR / "callout.go", self.root)}
        by_function = {function: s for (function, _), s in spawns.items()}
        self.assertTrue(by_function["handle"].flagged)
        self.assertEqual(BOUND_GO_LIMIT, by_function["pooled"].bounded_by)
        self.assertEqual(BOUND_GO_LIMIT, by_function["gated"].bounded_by)
        self.assertEqual(BOUND_STARTUP, by_function["main"].bounded_by)
        self.assertEqual(BOUND_CALLED_FROM_MAIN, by_function["run"].bounded_by)
        self.assertEqual(["handle"], sorted(f for (f, _), s in spawns.items() if s.flagged))

    def test_go_test_files_are_not_read(self):
        code, _, err = _run(self.root, "")
        self.assertEqual(EXIT_FAIL, code)
        self.assertNotIn("callout_test.go", err)
        self.assertIn("callout.go:7: handle starts a goroutine", err)

    def test_every_finding_pinned_exactly_passes(self):
        code, out, err = _run(
            self.root,
            "# fixture\n"
            f"{PYTHON_TARGET} post_unbounded 1\n"
            f"{PYTHON_TARGET} _lookup 1\n"
            f"{PYTHON_TARGET} Door._proxy 1\n"
            f"{GO_DIR}/callout.go handle 1\n",
        )
        self.assertEqual(EXIT_OK, code, err)
        self.assertIn("none unbounded beyond the 4 allowlist", out)

    def test_an_unpinned_finding_fails_and_names_the_handler(self):
        code, _, err = _run(
            self.root,
            f"{PYTHON_TARGET} _lookup 1\n{PYTHON_TARGET} Door._proxy 1\n{GO_DIR}/callout.go handle 1\n",
        )
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("server.py:14: post_unbounded starts a thread per request, reachable from post_unbounded", err)

    def test_a_rise_above_the_entry_fails(self):
        self.py.write_text(
            PYTHON_FIXTURE.replace(
                "    threading.Thread(target=_work, args=(payload,), daemon=True).start()\n",
                "    threading.Thread(target=_work, args=(payload,), daemon=True).start()\n"
                "    threading.Thread(target=_work, args=(payload,), daemon=True).start()\n",
            ),
            encoding="utf-8",
        )
        code, _, err = _run(
            self.root,
            f"{PYTHON_TARGET} post_unbounded 1\n{PYTHON_TARGET} _lookup 1\n"
            f"{PYTHON_TARGET} Door._proxy 1\n{GO_DIR}/callout.go handle 1\n",
        )
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("post_unbounded has 2 unbounded per-request spawn(s), the entry at allow.txt:1 allows 1", err)

    def test_a_drop_below_the_entry_fails_until_lowered(self):
        code, _, err = _run(
            self.root,
            f"{PYTHON_TARGET} post_unbounded 2\n{PYTHON_TARGET} _lookup 1\n"
            f"{PYTHON_TARGET} Door._proxy 1\n{GO_DIR}/callout.go handle 1\n",
        )
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("allows 2; lower the entry to 1", err)

    def test_a_stale_entry_fails(self):
        code, _, err = _run(
            self.root,
            f"{PYTHON_TARGET} post_unbounded 1\n{PYTHON_TARGET} _lookup 1\n{PYTHON_TARGET} Door._proxy 1\n"
            f"{PYTHON_TARGET} post_pooled 1\n{GO_DIR}/callout.go handle 1\n",
        )
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("allow.txt:4: svc/server.py post_pooled has no unbounded per-request spawn left; remove the entry", err)

    def test_an_entry_for_an_unread_file_fails(self):
        code, _, err = _run(
            self.root,
            f"{PYTHON_TARGET} post_unbounded 1\n{PYTHON_TARGET} _lookup 1\n{PYTHON_TARGET} Door._proxy 1\n"
            f"{GO_DIR}/callout.go handle 1\nsvc/other.py anything 1\n",
        )
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("allow.txt:5: svc/other.py is not a file this check reads; remove the entry", err)

    def test_a_malformed_or_duplicate_line_fails(self):
        code, _, err = _run(
            self.root,
            f"{PYTHON_TARGET} post_unbounded 1\n{PYTHON_TARGET} _lookup 1\n{PYTHON_TARGET} Door._proxy 1\n"
            f"{GO_DIR}/callout.go handle 1\n{PYTHON_TARGET} post_unbounded\n{PYTHON_TARGET} _lookup 0\n"
            f"{PYTHON_TARGET} _lookup 1\n",
        )
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("allow.txt:5: expected `<file> <function> <count>` with a count above zero", err)
        self.assertIn("allow.txt:6: expected `<file> <function> <count>` with a count above zero", err)
        self.assertIn("allow.txt:7: svc/server.py _lookup already has an entry at line 2", err)

    def test_a_missing_allowlist_holds_every_function_at_zero(self):
        code, _, err = _run(self.root)
        self.assertEqual(EXIT_FAIL, code)
        self.assertEqual(4, err.count("bound it or pin it in"))


class CheckoutTest(unittest.TestCase):
    def test_the_checkout_passes_with_its_allowlist(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([])
        self.assertEqual(EXIT_OK, code, err.getvalue())
        self.assertTrue(ALLOWLIST.is_file())


if __name__ == "__main__":
    unittest.main()

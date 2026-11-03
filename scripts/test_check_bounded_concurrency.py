#!/usr/bin/env python3
"""Tests for scripts/check_bounded_concurrency.py."""

from __future__ import annotations

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
    BOUND_GO_SEMAPHORE,
    BOUND_GO_STARTUP,
    BOUND_SINGLETON,
    BOUND_STARTUP,
    EXIT_FAIL,
    EXIT_OK,
    GO_TARGET_DIRS,
    PYTHON_TARGETS,
    REPO_ROOT,
    check,
    go_files,
    main,
    scan,
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
    POOL_SIZE = 4


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


    @app.post("/guarded-inside")
    def post_guarded_inside(payload):
        global _digest_thread
        if _digest_thread is None or not _digest_thread.is_alive():
            _digest_thread = threading.Thread(target=_work, daemon=True)
            _digest_thread.start()
        return {}


    @app.post("/alive-elsewhere")
    def post_alive_elsewhere(payload):
        if _digest_thread is not None and _digest_thread.is_alive():
            pass
        threading.Thread(target=_work, daemon=True).start()
        return {}


    @app.post("/fanout")
    def post_fanout(payload):
        for _ in range(len(payload["targets"])):
            threading.Thread(target=_work, daemon=True).start()
        return {}


    @app.post("/fixed-fanout")
    def post_fixed_fanout(payload):
        for _ in range(POOL_SIZE):
            threading.Thread(target=_work, daemon=True).start()
        return {}


    @app.post("/deferred")
    def post_deferred(payload, background_tasks):
        background_tasks.add_task(_deferred, payload)
        return {}


    def _deferred(payload):
        threading.Thread(target=_work, args=(payload,), daemon=True).start()


    @app.post("/submitted")
    def post_submitted(payload):
        _POOL.submit(_submitted, payload)
        return {}


    def _submitted(payload):
        threading.Timer(30, _work).start()


    @app.post("/chained")
    def post_chained(payload):
        threading.Thread(target=_chained, daemon=True).start()
        return {}


    def _chained():
        threading.Thread(target=_work, daemon=True).start()


    @app.post("/timer")
    def post_timer(payload):
        threading.Timer(30, _work).start()
        return {}


    @app.post("/pool-per-request")
    def post_pool_per_request(payload):
        executor = ThreadPoolExecutor(max_workers=1)
        executor.submit(_work, payload)
        executor.shutdown(wait=False)
        return {}


    class Worker(threading.Thread):
        def run(self):
            pass


    @app.post("/subclass")
    def post_subclass(payload):
        Worker().start()
        return {}


    @app.get("/through-helper")
    def get_through_helper():
        return _lookup("x")


    def _lookup(url):
        def _view():
            pass
        threading.Thread(target=_view, name="lookup", daemon=True).start()
        return None


    class _Workers:
        def __init__(self):
            self.threads = [threading.Thread(target=self._work, daemon=True) for _ in range(POOL_SIZE)]

        def _work(self):
            pass


    class _SizedWorkers:
        def __init__(self, workers):
            self.threads = [threading.Thread(target=_work, daemon=True) for _ in range(workers)]


    @app.post("/sized-pool-per-request")
    def post_sized_pool_per_request(payload):
        _SizedWorkers(len(payload["targets"]))
        return {}


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
        _Workers()
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

    func limitElsewhere(msgs []string) {
    \tvar g errgroup.Group
    \tg.SetLimit(4)
    \tfor _, m := range msgs {
    \t\tgo work(m)
    \t}
    }

    func grouped(msgs []string) error {
    \tvar g errgroup.Group
    \tg.SetLimit(4)
    \tfor _, m := range msgs {
    \t\tg.Go(func() error { work(m); return nil })
    \t}
    \treturn g.Wait()
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
    \t// Handle(msg) is named here and in the string below, and neither is a call.
    \tlog("Handle(")
    \tgo func() {}()
    \t_ = New()
    \tstatusHandler()
    \treturn nil
    }

    func serve() {}

    func log(s string) {}

    func statusHandler() {
    \tmux.HandleFunc("/readyz", func(w, r) {
    \t\tgo func() { _ = w }()
    \t})
    }
    """
)

# A second file of the same package: a constructor `run` reaches, and a method
# nothing on the startup path calls.
GO_SERVICE_FIXTURE = textwrap.dedent(
    """
    package callout

    type S struct{}

    func New() *S {
    \ts := &S{}
    \tgo s.watch()
    \treturn s
    }

    func (s *S) watch() {}

    func (s *S) Handle(msg string) {
    \tgo func() { _ = msg }()
    }
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
GO_FILE = f"{GO_DIR}/callout.go"
GO_SERVICE_FILE = f"{GO_DIR}/service.go"
ALLOWLIST_NAME = "allow.txt"

# Every finding the fixtures carry, as the allowlist that pins them exactly.
FLAGGED_PYTHON = (
    "Door._proxy",
    "_SizedWorkers.__init__",
    "_chained",
    "_deferred",
    "_lookup",
    "_submitted",
    "post_alive_elsewhere",
    "post_chained",
    "post_fanout",
    "post_fixed_fanout",
    "post_pool_per_request",
    "post_subclass",
    "post_timer",
    "post_unbounded",
)
FLAGGED_GO = (
    ("callout.go", "handle"),
    ("callout.go", "limitElsewhere"),
    ("callout.go", "statusHandler"),
    ("service.go", "Handle"),
)
FULL_ALLOWLIST = "# fixture\n" + "".join(f"{PYTHON_TARGET} {name} 1\n" for name in FLAGGED_PYTHON) + "".join(
    f"{GO_DIR}/{file} {name} 1\n" for file, name in FLAGGED_GO
)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _run(root: Path, allowlist_text: str | None = None, python_targets=(PYTHON_TARGET,), go_dirs=(GO_DIR,)):
    allowlist = root / ALLOWLIST_NAME
    if allowlist_text is not None:
        allowlist.write_text(allowlist_text, encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    code = check(root, python_targets, go_dirs, allowlist, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.py = _write(self.root, PYTHON_TARGET, PYTHON_FIXTURE)
        _write(self.root, GO_FILE, GO_FIXTURE)
        _write(self.root, GO_SERVICE_FILE, GO_SERVICE_FIXTURE)
        _write(self.root, f"{GO_DIR}/callout_test.go", GO_TEST_FIXTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def _python(self, function):
        return next(s for s in scan_python(self.py, self.root) if s.function == function)

    def _go(self):
        return {s.function: s for s in scan_go(go_files(self.root / GO_DIR), self.root)}

    def test_a_route_starting_a_thread_per_request_is_flagged(self):
        spawn = self._python("post_unbounded")
        self.assertEqual(("post_unbounded",), spawn.reachable_from)
        self.assertIsNone(spawn.bounded_by)
        self.assertTrue(spawn.flagged)

    def test_a_spawn_reached_through_a_helper_names_the_route(self):
        spawn = self._python("_lookup")
        self.assertTrue(spawn.flagged)
        self.assertEqual(("get_through_helper",), spawn.reachable_from)

    def test_a_request_handler_method_is_an_entry_point(self):
        spawn = self._python("Door._proxy")
        self.assertTrue(spawn.flagged)
        self.assertEqual(("Door._proxy", "Door.do_POST"), spawn.reachable_from)

    def test_a_module_level_executor_is_not_a_spawn(self):
        self.assertNotIn("post_pooled", [s.function for s in scan_python(self.py, self.root)])

    def test_a_timer_an_executor_and_a_thread_subclass_are_spawns(self):
        for function in ("post_timer", "post_pool_per_request", "post_subclass"):
            with self.subTest(function=function):
                self.assertTrue(self._python(function).flagged)

    def test_the_is_alive_guard_is_bounded_only_where_it_guards(self):
        early_exit = self._python("_arm_digest")
        self.assertEqual(("post_guarded",), early_exit.reachable_from)
        self.assertEqual(BOUND_SINGLETON, early_exit.bounded_by)
        self.assertEqual(BOUND_SINGLETON, self._python("post_guarded_inside").bounded_by)
        elsewhere = self._python("post_alive_elsewhere")
        self.assertIsNone(elsewhere.bounded_by)
        self.assertTrue(elsewhere.flagged)

    def test_a_loop_over_range_is_not_a_bound_whatever_it_ranges_over(self):
        # A pool main builds once is not flagged because nothing a handler reaches builds it.
        startup_pool = self._python("_Workers.__init__")
        self.assertIsNone(startup_pool.bounded_by)
        self.assertEqual((), startup_pool.reachable_from)
        self.assertFalse(startup_pool.flagged)
        # The same loop in a route starts POOL_SIZE threads per request.
        fixed = self._python("post_fixed_fanout")
        self.assertIsNone(fixed.bounded_by)
        self.assertTrue(fixed.flagged)
        self.assertTrue(self._python("post_fanout").flagged)
        sized = self._python("_SizedWorkers.__init__")
        self.assertEqual(("post_sized_pool_per_request",), sized.reachable_from)
        self.assertTrue(sized.flagged)

    def test_a_function_handed_to_add_task_submit_or_a_spawn_is_followed(self):
        for function, route in (("_deferred", "post_deferred"), ("_submitted", "post_submitted"), ("_chained", "post_chained")):
            with self.subTest(function=function):
                spawn = self._python(function)
                self.assertEqual((route,), spawn.reachable_from)
                self.assertTrue(spawn.flagged)

    def test_main_and_unreached_functions_are_not_flagged(self):
        by_function = {s.function: s for s in scan_python(self.py, self.root)}
        self.assertEqual(BOUND_STARTUP, by_function["main"].bounded_by)
        self.assertEqual((), by_function["_background"].reachable_from)
        self.assertFalse(by_function["_background"].flagged)
        self.assertEqual(list(FLAGGED_PYTHON), sorted(s.function for s in by_function.values() if s.flagged))

    def test_a_bare_goroutine_is_flagged_and_a_semaphore_or_startup_bounds_it(self):
        by_function = self._go()
        self.assertTrue(by_function["handle"].flagged)
        self.assertEqual(BOUND_GO_SEMAPHORE, by_function["gated"].bounded_by)
        self.assertEqual(BOUND_STARTUP, by_function["main"].bounded_by)
        self.assertEqual(BOUND_GO_STARTUP, by_function["run"].bounded_by)
        self.assertNotIn("grouped", by_function)

    def test_set_limit_does_not_bound_a_go_statement(self):
        spawn = self._go()["limitElsewhere"]
        self.assertIsNone(spawn.bounded_by)
        self.assertTrue(spawn.flagged)

    def test_the_startup_set_crosses_files_and_stops_at_what_main_reaches(self):
        by_function = self._go()
        self.assertEqual(BOUND_GO_STARTUP, by_function["New"].bounded_by)
        # `Handle(` appears in run's comment and string; neither puts Handle on the startup path.
        self.assertTrue(by_function["Handle"].flagged)
        self.assertEqual(
            sorted(name for _, name in FLAGGED_GO),
            sorted(s.function for s in by_function.values() if s.flagged),
        )

    def test_a_closure_built_on_the_startup_path_is_not_startup(self):
        by_function = self._go()
        self.assertEqual(BOUND_GO_STARTUP, by_function["run"].bounded_by)
        closure = by_function["statusHandler"]
        self.assertIsNone(closure.bounded_by)
        self.assertTrue(closure.flagged)

    def test_go_test_files_are_not_read(self):
        code, _, err = _run(self.root, "")
        self.assertEqual(EXIT_FAIL, code)
        self.assertNotIn("callout_test.go", err)
        self.assertIn("callout.go:7: handle starts a goroutine", err)

    def test_every_finding_pinned_exactly_passes(self):
        code, out, err = _run(self.root, FULL_ALLOWLIST)
        self.assertEqual(EXIT_OK, code, err)
        self.assertIn(f"none unbounded beyond the {len(FLAGGED_PYTHON) + len(FLAGGED_GO)} allowlist", out)

    def test_an_unpinned_finding_fails_and_names_the_handler(self):
        code, _, err = _run(self.root, FULL_ALLOWLIST.replace(f"{PYTHON_TARGET} post_unbounded 1\n", ""))
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("server.py:15: post_unbounded starts a thread per request, reachable from post_unbounded", err)

    def test_a_rise_above_the_entry_fails(self):
        spawn = "    threading.Thread(target=_work, args=(payload,), daemon=True).start()\n"
        self.py.write_text(PYTHON_FIXTURE.replace(spawn, spawn + spawn), encoding="utf-8")
        code, _, err = _run(self.root, FULL_ALLOWLIST)
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("post_unbounded has 2 unbounded per-request spawn(s), the entry at allow.txt:15 allows 1", err)

    def test_a_drop_below_the_entry_fails_until_lowered(self):
        code, _, err = _run(self.root, FULL_ALLOWLIST.replace("post_unbounded 1", "post_unbounded 2"))
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("allows 2; lower the entry to 1", err)

    def test_a_stale_entry_fails(self):
        code, _, err = _run(self.root, FULL_ALLOWLIST + f"{PYTHON_TARGET} post_pooled 1\n")
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("allow.txt:20: svc/server.py post_pooled has no unbounded per-request spawn left; remove the entry", err)

    def test_an_entry_for_an_unread_file_fails(self):
        code, _, err = _run(self.root, FULL_ALLOWLIST + "svc/other.py anything 1\n")
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("allow.txt:20: svc/other.py is not a file this check reads; remove the entry", err)

    def test_a_malformed_or_duplicate_line_fails(self):
        code, _, err = _run(
            self.root,
            FULL_ALLOWLIST + f"{PYTHON_TARGET} post_unbounded\n{PYTHON_TARGET} _lookup 0\n{PYTHON_TARGET} _lookup 1\n",
        )
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("allow.txt:20: expected `<file> <function> <count>` with a count above zero", err)
        self.assertIn("allow.txt:21: expected `<file> <function> <count>` with a count above zero", err)
        self.assertIn("allow.txt:22: svc/server.py _lookup already has an entry at line 6", err)

    def test_a_missing_allowlist_holds_every_function_at_zero(self):
        code, _, err = _run(self.root)
        self.assertEqual(EXIT_FAIL, code)
        self.assertEqual(len(FLAGGED_PYTHON) + len(FLAGGED_GO), err.count("bound it or pin it in"))

    def test_a_missing_python_target_fails(self):
        code, _, err = _run(self.root, FULL_ALLOWLIST, python_targets=(PYTHON_TARGET, "svc/moved.py"))
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("svc/moved.py: a configured target this check reads is missing", err)

    def test_a_missing_or_empty_go_directory_fails(self):
        (self.root / "svc" / "empty").mkdir()
        code, _, err = _run(self.root, FULL_ALLOWLIST, go_dirs=(GO_DIR, "svc/empty", "svc/gone"))
        self.assertEqual(EXIT_FAIL, code)
        self.assertIn("svc/empty: a configured Go directory holds no non-test .go file", err)
        self.assertIn("svc/gone: a configured Go directory holds no non-test .go file", err)


class CheckoutTest(unittest.TestCase):
    def test_the_checkout_passes_with_its_allowlist(self):
        out, err = io.StringIO(), io.StringIO()
        code = main([], out=out, err=err)
        self.assertEqual(EXIT_OK, code, err.getvalue())
        self.assertTrue(ALLOWLIST.is_file())

    def test_the_checkout_scan_reads_every_configured_target(self):
        _, files, problems = scan(REPO_ROOT, PYTHON_TARGETS, GO_TARGET_DIRS)
        self.assertEqual([], problems)
        for target in PYTHON_TARGETS:
            self.assertIn(target, files)
        for go_dir in GO_TARGET_DIRS:
            self.assertTrue(any(f.startswith(f"{go_dir}/") for f in files), go_dir)


if __name__ == "__main__":
    unittest.main()

"""Tests for kubectl-admit.py: how a command is keyed and charged, and when it runs, waits or is refused."""

import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load(name: str, filename: str):
    loader = importlib.machinery.SourceFileLoader(name, str(Path(__file__).with_name(filename)))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


admit = load("kubectl_admit", "kubectl-admit.py")
guard = load("shell_memory_guard_for_admit", "shell-memory-guard.py")

MIB = admit.MIB
GIB = 1024 * MIB
TABLE_PATH = Path(__file__).with_name("kubectl-costs.json")
TABLE = {"tiers": {
    "default": {"commands": {"get pods cluster json": {"peak_mib": 80}}, "other": {"peak_mib": 256}},
    "fleet": {"commands": {
        "get cronjobs,daemonsets,deployments,pods,statefulsets cluster json": {"peak_mib": 15309},
        "get pods cluster json": {"peak_mib": 14307},
        "get pods cluster table": {"peak_mib": 4371},
        "get replicasets cluster json": {"peak_mib": 205},
        "get services cluster json": {"peak_mib": 101},
        "get nodes cluster json": {"peak_mib": 255},
    }, "other": {"peak_mib": 256}},
}}


def parsed(*argv):
    return admit.parse(list(argv))


def key(*argv):
    return admit.command_key(parsed(*argv))


class KeyTest(unittest.TestCase):
    def test_keys_by_kinds_scope_and_format(self):
        cases = [
            (("get", "deploy,sts,ds,cronjob,pod", "-A", "-o", "json"),
             "get cronjobs,daemonsets,deployments,pods,statefulsets cluster json"),
            (("--context", "c", "get", "po", "--all-namespaces"), "get pods cluster table"),
            (("get", "pods", "-A", "-o", "name"), "get pods cluster json"),       # kubectl decodes full objects
            (("get", "pods", "-A", "-ojsonpath={.items}"), "get pods cluster json"),
            (("get", "nodes"), "get nodes cluster table"),                         # cluster-scoped kind
            (("get", "pods", "-n", "x"), "get pods namespace table"),
            (("get", "pods", "-n", "x", "-A"), "get pods cluster table"),          # -A wins, as in kubectl
            (("get", "pod", "p1", "-n", "x", "-o", "json"), "get pods object json"),
            (("get", "pod/p1", "-o", "json"), "get pods object json"),
            (("get", "pods.v1", "-A"), "get pods cluster table"),                  # a group or version suffix
            (("get", "deployments.apps", "-A", "-o", "json"), "get deployments cluster json"),
            (("get", "widgets.example.com", "-A", "-o", "json"), "get widgets.example.com cluster json"),
            (("get", "pods", "--all-namespaces=false"), "get pods namespace table"),
            (("get", "pods", "-A", "--watch=false"), "get pods cluster table"),    # not a watch
            (("get", "pods", "-A=false", "-nteam"), "get pods namespace table"),   # glued -n, -A off
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(key(*argv), expected)

    def test_get_all_expands_to_its_kinds(self):
        self.assertEqual(key("get", "all", "-A", "-o", "json"),
                         "get cronjobs,daemonsets,deployments,jobs,pods,replicasets,services,statefulsets cluster json")

    def test_non_lists_are_not_keyed(self):
        for argv in [("logs", "x"), ("describe", "pod", "x"), ("get", "--raw", "/api/v1/pods"), ("get", "pods", "-w"),
                     ("get", "-k", "dir"), ("get", "-f", "x.yaml"), ("get",), ()]:
            with self.subTest(argv=argv):
                self.assertIsNone(key(*argv))

    def test_a_glued_selector_is_a_selector(self):
        self.assertTrue(parsed("get", "pods", "-A", "-lapp=x")["selector"])

    def test_value_flags_before_the_verb_do_not_move_it(self):
        self.assertEqual(key("--vmodule", "x=2", "--kuberc", "f", "get", "pods", "-A"), "get pods cluster table")


class CostTest(unittest.TestCase):
    def charge(self, tier, *argv):
        return admit.cost(TABLE, tier, parsed(*argv))

    def test_a_listed_key_is_charged_its_peak(self):
        self.assertEqual(self.charge("fleet", "get", "pods", "-A"), (4371 * MIB, True))

    def test_an_unlisted_key_is_charged_the_sum_of_its_listed_kinds(self):
        # get all: pods, replicasets and services are listed; the others add nothing.
        self.assertEqual(self.charge("fleet", "get", "all", "-A", "-o", "json"), ((14307 + 205 + 101) * MIB, True))
        self.assertEqual(self.charge("fleet", "get", "po,svc", "-A", "-o", "json"), ((14307 + 101) * MIB, True))
        self.assertEqual(self.charge("fleet", "get", "pods.v1", "-A", "-o", "json"), (14307 * MIB, True))

    def test_a_key_with_no_listed_kind_is_charged_other(self):
        self.assertEqual(self.charge("fleet", "get", "widgets.example.com", "-A", "-o", "json"), (256 * MIB, False))

    def test_an_unknown_tier_falls_back_to_default(self):
        self.assertEqual(self.charge("nosuch", "get", "pods", "-A", "-o", "json"), (80 * MIB, True))

    def test_the_shipped_table(self):
        table = json.loads(TABLE_PATH.read_text())
        self.assertIn("default", table["tiers"])
        for tier, column in table["tiers"].items():
            with self.subTest(tier=tier):
                self.assertGreater(column["other"]["peak_mib"], 0)
                for name, entry in column["commands"].items():
                    self.assertTrue(name.startswith("get ") and " cluster " in name, name)  # only cluster reads are charged
                    self.assertGreater(entry["peak_mib"], 0)
                    self.assertEqual(key(*entry["example"].split()[1:]), name)  # the example keys to its entry


class CollectionTest(unittest.TestCase):
    def test_the_count_path_of_the_first_kind(self):
        cases = [(["pods"], "/api/v1/pods"), (["deployments", "pods"], "/apis/apps/v1/deployments"),
                 (["cronjobs"], "/apis/batch/v1/cronjobs"), (["widgets.example.com"], admit.UNKNOWN_COLLECTION)]
        for kinds, expected in cases:
            with self.subTest(kinds=kinds):
                self.assertEqual(admit.collection(kinds), expected)


class BudgetTest(unittest.TestCase):
    def test_every_reservation_counts_at_full_size(self):
        high = 1792 * MIB
        free = admit.free_budget(high, 600 * MIB, [{"child": 7, "bytes": 1000 * MIB}], rss_of=lambda pid: 100 * MIB)
        self.assertEqual(free, high - 500 * MIB - 1000 * MIB)

    def test_a_reservation_without_a_child_yet_counts_in_full(self):
        free = admit.free_budget(1792 * MIB, 300 * MIB, [{"bytes": 500 * MIB}], rss_of=lambda pid: 0)
        self.assertEqual(free, (1792 - 300 - 500) * MIB)


class ReservationsTest(unittest.TestCase):
    def test_dead_and_temp_entries_are_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            admit.write_shared(os.path.join(tmp, str(os.getpid())), {"bytes": 7})
            Path(tmp, "999999999").write_text('{"bytes": 9}')       # no such process
            Path(tmp, f"{os.getpid()}.tmp").write_text('{"bytes": 5}')  # a write in flight
            self.assertEqual(admit.live_reservations(tmp), [{"bytes": 7}])


class StateTest(unittest.TestCase):
    def test_a_shared_write_is_atomic_and_readable_by_every_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "123")
            old = os.umask(0o077)  # a session's umask
            try:
                admit.write_shared(path, {"bytes": 5})
            finally:
                os.umask(old)
            self.assertEqual(json.loads(Path(path).read_text()), {"bytes": 5})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), admit.SHARED_FILE_MODE)
            self.assertEqual(os.listdir(tmp), ["123"])  # no temp file left behind

    def test_the_event_log_holds_no_command_line_and_stops_at_its_cap(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(admit, "EVENTS_LOG", os.path.join(tmp, "e")), \
                mock.patch.object(admit, "EVENTS_LOG_MAX_BYTES", 200):
            for _ in range(10):
                admit.log({"event": "other", "key": "get x cluster json"})
            text = Path(tmp, "e").read_text()
        self.assertLess(len(text), 400)
        self.assertNotIn("argv", text)


class AdmitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        (state / "reservations").mkdir()
        (state / "slots").mkdir()
        (state / "waiting").mkdir()
        for name, value in [("LEDGER_DIR", str(state / "reservations")), ("SLOT_DIR", str(state / "slots")),
                            ("WAIT_DIR", str(state / "waiting")), ("LEDGER_LOCK", str(state / "ledger.lock")),
                            ("EVENTS_LOG", str(state / "events.log")), ("ADMIT_WAIT_SECONDS", 0.0),
                            ("ADMIT_POLL_SECONDS", 0.0)]:
            patcher = mock.patch.object(admit, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events = state / "events.log"
        self.used = 200 * MIB
        self.guard = mock.Mock(memory_in_use=lambda: self.used, high_watermark=guard.high_watermark,
                               effective_watermark=guard.effective_watermark, on_gvisor=lambda: False,
                               process_count=lambda: 0)
        self.stderr = io.StringIO()
        for target, attr, value in [(admit.sys, "stderr", self.stderr),
                                    (admit, "run", mock.Mock(return_value=0)),
                                    (admit, "pass_through", mock.Mock(side_effect=SystemExit("pass_through")))]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def admit(self, *argv, tier="fleet", limit=2 * GIB):
        return admit.admit(list(argv), self.guard, TABLE, tier, limit)

    def booked(self):
        return admit.run.call_args.args[1]["bytes"]

    def logged(self):
        return [json.loads(l)["event"] for l in self.events.read_text().splitlines()] if self.events.exists() else []

    def test_a_read_that_fits_runs_under_a_reservation(self):
        self.assertEqual(self.admit("get", "nodes", "-o", "json"), 0)
        self.assertEqual(self.booked(), 255 * MIB)
        self.assertEqual(self.logged(), ["admitted"])

    def test_a_read_larger_than_one_read_may_take_is_refused_at_once(self):
        self.assertEqual(self.admit("get", "deploy,sts,ds,cronjob,pod", "-A", "-o", "json"), admit.REFUSED_NOW)
        admit.run.assert_not_called()
        reason = self.stderr.getvalue()
        self.assertTrue(reason.startswith("capacity: `kubectl get deploy,sts,ds,cronjob,pod` across this fleet"))
        self.assertIn("the install cannot take this read", reason)
        self.assertIn("kubectl get --raw '/apis/batch/v1/cronjobs?limit=1'", reason)
        self.assertIn("the total is that 1 plus `metadata.remainingItemCount`", reason)
        self.assertIn("kubectl get deploy,sts,ds,cronjob,pod -n <namespace>", reason)
        self.assertEqual(self.logged(), ["refused-size"])

    def test_get_all_is_refused_by_the_sum_of_its_kinds(self):
        self.assertEqual(self.admit("get", "all", "-A", "-o", "json"), admit.REFUSED_NOW)

    def test_the_same_read_on_a_small_fleet_runs(self):
        self.assertEqual(self.admit("get", "pods", "-A", "-o", "json", tier="default"), 0)
        self.assertEqual(self.booked(), 80 * MIB)

    def test_a_read_that_does_not_fit_now_waits_then_is_refused_busy(self):
        self.used = 1700 * MIB
        self.assertEqual(self.admit("get", "nodes", "-o", "json"), admit.REFUSED_BUSY)
        self.assertIn("Retry in a minute", self.stderr.getvalue())
        self.assertEqual(self.logged(), ["refused-busy"])

    def test_a_running_reservation_is_seen_by_the_next_read(self):
        admit.write_shared(os.path.join(admit.LEDGER_DIR, str(os.getpid())), {"bytes": 1400 * MIB})
        self.assertEqual(self.admit("get", "nodes", "-o", "json"), admit.REFUSED_BUSY)

    def test_a_selector_read_is_not_refused_for_size_and_books_what_is_free(self):
        self.used = 900 * MIB  # more than the shell floor in use
        self.assertEqual(self.admit("get", "pods", "-A", "-o", "json", "-l", "app=x"), 0)
        self.assertEqual(self.booked(), guard.high_watermark(2 * GIB) - 900 * MIB)

    def test_a_selector_read_waits_while_nothing_is_free(self):
        self.used = 1900 * MIB  # over the watermark: nothing is free
        self.assertEqual(self.admit("get", "pods", "-A", "-o", "json", "-l", "app=x"), admit.REFUSED_BUSY)

    def test_an_unlisted_read_is_charged_other_and_recorded(self):
        self.assertEqual(self.admit("get", "widgets.example.com", "-A", "-o", "json"), 0)
        self.assertEqual(self.booked(), 256 * MIB)
        self.assertEqual(self.logged(), ["other", "admitted"])

    def test_namespace_and_object_reads_and_other_verbs_pass_through(self):
        for argv in [("get", "pods", "-n", "x", "-o", "json"), ("get", "pod", "p1"), ("logs", "x")]:
            with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, "pass_through"):
                self.admit(*argv)
        self.assertEqual(self.logged(), [])

    def test_a_command_past_the_slot_cap_waits_then_is_refused(self):
        slots = admit.max_slots(guard.high_watermark(2 * GIB))
        self.assertEqual(slots, 13)  # (1,792 - 256) MiB in pairs of 96 + 16 MiB
        for pid in range(slots):
            admit.write_shared(os.path.join(admit.SLOT_DIR, str(10000 + pid)), {"pid": 10000 + pid})
        with mock.patch.object(admit, "alive", return_value=True):
            for argv in [("logs", "x"), ("get", "nodes", "-o", "json")]:
                with self.subTest(argv=argv):
                    self.assertEqual(self.admit(*argv), admit.REFUSED_BUSY)
                    reason = self.stderr.getvalue()
                    self.assertTrue(reason.startswith("capacity: the shell is already running 13 kubectl commands"))
                    self.assertIn("Options: ", reason)
                    self.stderr.truncate(0), self.stderr.seek(0)
        admit.run.assert_not_called()
        self.assertEqual(self.logged(), ["refused-slots", "refused-slots"])

    def test_a_passed_through_command_holds_its_slot_and_an_admitted_read_frees_its_own(self):
        mine = os.path.join(admit.SLOT_DIR, str(os.getpid()))
        with self.assertRaisesRegex(SystemExit, "pass_through"):
            self.admit("logs", "x")
        self.assertTrue(os.path.exists(mine))  # held across the exec, by the same pid
        os.unlink(mine)
        self.assertEqual(self.admit("get", "nodes", "-o", "json"), 0)
        self.assertFalse(os.path.exists(mine))

    def test_a_read_that_cannot_be_spawned_is_refused_and_leaves_no_slot_or_reservation(self):
        with mock.patch.object(admit, "run", side_effect=OSError("EAGAIN")):
            self.assertEqual(self.admit("get", "nodes", "-o", "json"), admit.REFUSED_BUSY)
        self.assertEqual(os.listdir(admit.SLOT_DIR) + os.listdir(admit.LEDGER_DIR), [])
        self.assertEqual(self.logged(), ["admitted", "refused-spawn"])

    def test_local_verbs_watches_and_streams_take_no_slot(self):
        for argv in [("config", "view"), ("version",), ("get", "pods", "-A", "-w"), ("port-forward", "svc/x", "80"),
                     ("logs", "-f", "x"), ("logs", "x", "--follow"), ("exec", "-it", "x", "--", "sh"),
                     ("rollout", "status", "deploy/x"), ("wait", "--for=condition=Ready", "pod/x")]:
            with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, "pass_through"):
                self.admit(*argv)
        self.assertEqual(os.listdir(admit.SLOT_DIR), [])

    def test_no_readable_memory_passes_through(self):
        self.used = None
        with self.assertRaisesRegex(SystemExit, "pass_through"):
            self.admit("get", "pods", "-A", "-o", "json")

    def test_a_lock_it_cannot_take_is_refused_at_once_and_never_runs_unslotted(self):
        with mock.patch.object(admit, "take_lock", return_value=False):
            for argv in [("get", "nodes", "-o", "json"), ("logs", "x")]:
                with self.subTest(argv=argv):
                    self.assertEqual(self.admit(*argv), admit.REFUSED_BUSY)
        admit.run.assert_not_called()
        self.assertTrue(self.stderr.getvalue().startswith("capacity: the shell is too busy"))
        self.assertEqual(self.logged(), ["refused-lock"] * 2)

    def fill(self, directory, count, base):
        for pid in range(base, base + count):
            admit.write_shared(os.path.join(directory, str(pid)), {"pid": pid})

    def test_a_flood_is_refused_at_once_without_the_lock(self):
        slots = admit.max_slots(guard.high_watermark(2 * GIB))
        self.fill(admit.SLOT_DIR, slots, 10000)
        self.fill(admit.WAIT_DIR, slots, 20000)
        with mock.patch.object(admit, "alive", return_value=True), \
                mock.patch.object(admit, "take_lock", side_effect=AssertionError("took the lock")), \
                mock.patch.object(admit.time, "sleep", side_effect=AssertionError("waited")):
            self.assertEqual(self.admit("logs", "x"), admit.REFUSED_BUSY)
        self.assertTrue(self.stderr.getvalue().startswith(
            f"capacity: the shell is already running {slots} kubectl commands and {slots} more are waiting"))
        self.assertIn("Options: ", self.stderr.getvalue())
        self.assertEqual(self.logged(), ["refused-flood"])

    def test_a_waiter_holds_a_place_in_the_queue_only_while_it_waits(self):
        slots = admit.max_slots(guard.high_watermark(2 * GIB))
        self.fill(admit.SLOT_DIR, slots, 10000)
        seen = []
        with mock.patch.object(admit, "alive", side_effect=lambda pid: pid != 99999), \
                mock.patch.object(admit.time, "sleep", side_effect=lambda _s: seen.append(os.listdir(admit.WAIT_DIR))), \
                mock.patch.object(admit, "ADMIT_WAIT_SECONDS", 0.05):
            self.assertEqual(self.admit("logs", "x"), admit.REFUSED_BUSY)
        self.assertIn([str(os.getpid())], seen)
        self.assertEqual(os.listdir(admit.WAIT_DIR), [])
        self.assertEqual(self.logged(), ["refused-slots"])

    def test_dead_entries_do_not_count_toward_a_flood(self):
        slots = admit.max_slots(guard.high_watermark(2 * GIB))
        self.fill(admit.WAIT_DIR, slots, 20000)  # dead pids: nothing waits
        self.assertEqual(admit.count_alive(admit.WAIT_DIR), 0)
        self.assertEqual(self.admit("get", "nodes", "-o", "json"), 0)


class LockTest(unittest.TestCase):
    def test_waits_in_the_kernel_and_gives_up_at_its_deadline(self):
        with tempfile.NamedTemporaryFile() as held, open(held.name, "rb") as other, \
                mock.patch.object(admit, "LOCK_WAIT_SECONDS", 0.2):
            admit.fcntl.flock(held, admit.fcntl.LOCK_EX)
            self.assertFalse(admit.take_lock(other))
            admit.fcntl.flock(held, admit.fcntl.LOCK_UN)
            self.assertTrue(admit.take_lock(other))
        self.assertEqual(admit.signal.getitimer(admit.signal.ITIMER_REAL), (0.0, 0.0))

    def test_an_alarm_after_the_lock_is_taken_reads_as_a_timeout_and_restores_the_handler(self):
        before = admit.signal.getsignal(admit.signal.SIGALRM)
        with tempfile.NamedTemporaryFile() as held, \
                mock.patch.object(admit.fcntl, "flock", side_effect=lambda *_: admit._lock_timeout(0, None)):
            self.assertFalse(admit.take_lock(held))
        self.assertIs(admit.signal.getsignal(admit.signal.SIGALRM), before)


class PassThroughTest(unittest.TestCase):
    def test_restores_sigpipe_before_exec(self):
        with mock.patch.object(admit.signal, "signal") as sig, mock.patch.object(admit.os, "execv") as execv:
            admit.pass_through(["logs", "x"])
        sig.assert_called_once_with(admit.signal.SIGPIPE, admit.signal.SIG_DFL)
        execv.assert_called_once_with(admit.KUBECTL_NEXT, [admit.KUBECTL_NEXT, "logs", "x"])

    def test_an_admission_error_runs_the_command_unchanged(self):
        stderr = io.StringIO()
        with mock.patch.object(admit, "load_guard", side_effect=FileNotFoundError("no guard")), \
                mock.patch.object(admit, "pass_through", side_effect=SystemExit("pass_through")) as through, \
                mock.patch.object(admit.sys, "stderr", stderr):
            with self.assertRaisesRegex(SystemExit, "pass_through"):
                admit.main(["get", "pods", "-A"])
        through.assert_called_once_with(["get", "pods", "-A"])
        self.assertIn("kubectl admission skipped", stderr.getvalue())


class RunTest(unittest.TestCase):
    def run_child(self, rc, ledger=None):
        child = mock.Mock(pid=4242)
        child.wait.return_value = rc
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(admit, "LEDGER_DIR", ledger or tmp), \
                mock.patch.object(admit.subprocess, "Popen", return_value=child) as popen, \
                mock.patch.object(admit.signal, "signal"), mock.patch.object(admit.sys, "stderr", stderr):
            out = admit.run(["get", "pods", "-A"], {"bytes": 1}, parsed("get", "pods", "-A"))
        return out, popen, stderr.getvalue()

    def test_a_ledger_write_error_after_the_spawn_does_not_run_the_command_again(self):
        out, popen, _ = self.run_child(0, ledger="/nonexistent/ledger")
        self.assertEqual(out, 0)
        popen.assert_called_once()

    def test_a_child_stopped_by_sigkill_explains_why(self):
        out, _, err = self.run_child(-admit.signal.SIGKILL)
        self.assertEqual(out, 128 + admit.signal.SIGKILL)
        self.assertTrue(err.startswith("capacity: this read was stopped (SIGKILL)"))

    def test_a_signal_exit_is_reported_as_the_shell_does(self):
        self.assertEqual(self.run_child(-admit.signal.SIGTERM)[0], 128 + admit.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()

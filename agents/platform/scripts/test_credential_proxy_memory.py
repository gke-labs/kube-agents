"""The broker's memory telemetry (#4315): what the container holds, read from
the kernel rather than estimated from the budget's constants.

Three readers and a sampler. `MemoryReader` reads the cgroup v2 files and
/proc; `ChildPeakTracker` reads the children's ru_maxrss after each reap and
attributes a rise to the tool just reaped; `MemorySampler` writes a log line
at start, at each step of the container's high-water, and as a warning when
the limit was reached; `ProxyMetrics` renders the snapshot as series. Every
file is faked under a temporary directory, so the tests say what each figure
is read from and what an unreadable one does: absent, never zero.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import credential_proxy
from credential_proxy import (
    ChildPeakTracker,
    CommandExecutor,
    MemoryReader,
    MemorySampler,
    MemorySnapshot,
    ProxyMetrics,
    _capture_output,
    _reap,
)

_MIB = 1024 * 1024
_KIB = 1024
# The gkedemos2 proxy container on 2026-10-29, 2Gi limit, a near-empty scope.
_CURRENT = 251711488
_PEAK = 569466880
_LIMIT = 2147483648
_ANON = 116756480
_FILE = 111996928
_BROKER_RSS_KB = 108748
_BROKER_PEAK_KB = 152900
_ENVOY_RSS_KB = 69916
_ENVOY_PEAK_KB = 69916
_ENVOY_PID = "11"
_OTHER_PID = "1"
_SERIES = re.compile(r"^(?P<name>[a-z_]+)(?:\{(?P<labels>[^}]*)\})? (?P<value>-?[0-9.]+(?:e[+-]?[0-9]+)?)$")
_LABEL_PAIR = re.compile(r'([a-z_]+)="((?:[^"\\]|\\.)*)"')


def _families(exposition: str) -> dict[str, dict[frozenset, float]]:
    families: dict[str, dict[frozenset, float]] = {}
    for line in exposition.splitlines():
        if line.startswith("#"):
            continue
        match = _SERIES.match(line)
        assert match, f"not a Prometheus text line: {line!r}"
        labels = frozenset(_LABEL_PAIR.findall(match.group("labels") or ""))
        families.setdefault(match.group("name"), {})[labels] = float(match.group("value"))
    return families


def _series(families, name, **labels):
    return families[name][frozenset(labels.items())]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_cgroup(root: Path, *, limit: str = str(_LIMIT), events: str = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n") -> Path:
    cgroup = root / "cgroup"
    _write(cgroup / "memory.current", f"{_CURRENT}\n")
    _write(cgroup / "memory.peak", f"{_PEAK}\n")
    _write(cgroup / "memory.max", f"{limit}\n")
    _write(cgroup / "memory.stat", f"anon {_ANON}\nfile {_FILE}\nkernel 22712320\nkernel_stack 458752\n")
    _write(cgroup / "memory.events", events)
    return cgroup


def _fake_proc(root: Path) -> Path:
    proc = root / "proc"
    _write(proc / "self" / "status", f"Name:\tpython3\nVmPeak:\t 643156 kB\nVmHWM:\t  {_BROKER_PEAK_KB} kB\nVmRSS:\t  {_BROKER_RSS_KB} kB\n")
    _write(proc / _ENVOY_PID / "comm", "envoy\n")
    _write(proc / _ENVOY_PID / "status", f"Name:\tenvoy\nVmHWM:\t   {_ENVOY_PEAK_KB} kB\nVmRSS:\t   {_ENVOY_RSS_KB} kB\n")
    _write(proc / _OTHER_PID / "comm", "bash\n")
    _write(proc / _OTHER_PID / "status", "Name:\tbash\nVmHWM:\t 3336 kB\nVmRSS:\t 3336 kB\n")
    return proc


class MemoryReaderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tracker = ChildPeakTracker()

    def reader(self, *, cgroup_dir: Path | None = None, proc_root: Path | None = None) -> MemoryReader:
        return MemoryReader(
            self.tracker,
            cgroup_dir=cgroup_dir if cgroup_dir is not None else _fake_cgroup(self.root),
            proc_root=proc_root if proc_root is not None else _fake_proc(self.root),
        )

    def test_the_cgroup_figures_are_read_by_kind(self):
        snapshot = self.reader().read()
        self.assertEqual(
            {"current": _CURRENT, "peak": _PEAK, "limit": _LIMIT, "anon": _ANON, "file": _FILE},
            snapshot.cgroup,
        )
        self.assertEqual({"high": 0, "max": 0, "oom": 0, "oom_kill": 0}, snapshot.events)

    def test_a_limit_of_max_and_an_unreadable_file_leave_the_kind_absent(self):
        cgroup = _fake_cgroup(self.root, limit="max")
        (cgroup / "memory.peak").unlink()
        (cgroup / "memory.events").write_text("not a counter line\n")
        snapshot = self.reader(cgroup_dir=cgroup).read()
        self.assertNotIn("limit", snapshot.cgroup)
        self.assertNotIn("peak", snapshot.cgroup)
        self.assertEqual(_CURRENT, snapshot.cgroup["current"])
        self.assertEqual({}, snapshot.events)

    def test_the_broker_and_envoy_are_read_from_proc_in_bytes(self):
        snapshot = self.reader().read()
        self.assertEqual(
            {
                "broker": {"rss": _BROKER_RSS_KB * _KIB, "peak": _BROKER_PEAK_KB * _KIB},
                "envoy": {"rss": _ENVOY_RSS_KB * _KIB, "peak": _ENVOY_PEAK_KB * _KIB},
            },
            snapshot.processes,
        )

    def test_envoy_is_found_by_comm_and_found_again_when_its_pid_changes(self):
        proc = _fake_proc(self.root)
        reader = self.reader(proc_root=proc)
        self.assertIn("envoy", reader.read().processes)
        # The pid is kept between reads, and a pid that now names another
        # process is dropped rather than read as Envoy.
        (proc / _ENVOY_PID / "comm").write_text("sleep\n")
        self.assertNotIn("envoy", reader.read().processes)
        _write(proc / "13" / "comm", "envoy\n")
        _write(proc / "13" / "status", "VmHWM:\t 70000 kB\nVmRSS:\t 70000 kB\n")
        self.assertEqual(70000 * _KIB, reader.read().processes["envoy"]["rss"])

    def test_a_container_without_envoy_has_no_envoy_series(self):
        proc = _fake_proc(self.root)
        (proc / _ENVOY_PID / "comm").unlink()
        self.assertEqual(["broker"], sorted(self.reader(proc_root=proc).read().processes))

    def test_the_children_come_from_the_tracker(self):
        tracker = ChildPeakTracker()
        tracker.record("gcloud", 120 * _MIB)
        tracker.record("kubectl", 600 * _MIB)
        reader = MemoryReader(tracker, cgroup_dir=_fake_cgroup(self.root), proc_root=_fake_proc(self.root))
        snapshot = reader.read()
        self.assertEqual({"gcloud": 120 * _MIB, "kubectl": 600 * _MIB}, snapshot.children)
        self.assertEqual(("kubectl", 600 * _MIB), snapshot.largest_child)


class ChildPeakTrackerTest(unittest.TestCase):
    def test_each_tools_figure_is_its_own_and_the_largest_is_logged(self):
        tracker = ChildPeakTracker()
        with self.assertLogs(credential_proxy.LOGGER, level=logging.INFO) as logs:
            self.assertTrue(tracker.record("gcloud", 100 * _MIB))
            # A smaller child moves its own tool's figure only when larger
            # than that tool's, and never the overall; no line for it.
            self.assertFalse(tracker.record("kubectl", 60 * _MIB))
            self.assertTrue(tracker.record("kubectl", 432 * _MIB))
            self.assertFalse(tracker.record("gcloud", 90 * _MIB))
        self.assertEqual({"gcloud": 100 * _MIB, "kubectl": 432 * _MIB}, tracker.snapshot()[0])
        self.assertEqual(("kubectl", 432 * _MIB), tracker.snapshot()[1])
        self.assertEqual(
            ["largest child so far tool=gcloud peak_rss=100MiB", "largest child so far tool=kubectl peak_rss=432MiB"],
            [record.getMessage() for record in logs.records],
        )

    def test_a_reap_without_a_peak_records_nothing(self):
        tracker = ChildPeakTracker()
        self.assertFalse(tracker.record("kubectl", None))
        self.assertFalse(tracker.record("kubectl", 0))
        self.assertEqual(({}, None), tracker.snapshot())

    def test_the_bootstrap_command_is_recorded_under_its_own_label(self):
        # The bootstrap is the process's first child, so the children's
        # high-water after it is its tree's; it is recorded as `bootstrap`
        # and the first brokered command does not inherit it.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        executor = CommandExecutor(timeout_seconds=5, max_output_bytes=1024, state_dir=temp.name, scoped_pool=None)
        executor.child_peaks = ChildPeakTracker()
        executor.bootstrap("true")
        by_tool, largest = executor.child_peaks.snapshot()
        self.assertEqual(["bootstrap"], sorted(by_tool))
        self.assertGreater(by_tool["bootstrap"], 0)
        self.assertEqual("bootstrap", largest[0])

    def test_an_empty_bootstrap_records_nothing(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        executor = CommandExecutor(timeout_seconds=5, max_output_bytes=1024, state_dir=temp.name, scoped_pool=None)
        executor.child_peaks = ChildPeakTracker()
        executor.bootstrap("")
        self.assertEqual(({}, None), executor.child_peaks.snapshot())

    def test_the_executor_records_each_reaped_child_under_the_tools_label(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        stub.write_text("#!/bin/sh\necho ok\n")
        stub.chmod(0o755)
        executor = CommandExecutor(timeout_seconds=5, max_output_bytes=1024, state_dir=temp.name, scoped_pool=None)
        executor.executables["kubectl"] = str(stub)
        with mock.patch.object(executor, "child_peaks") as peaks:
            result = executor.execute(["kubectl", "get", "pods"])
        self.assertEqual(0, result.exit_code, result.stderr)
        peaks.record.assert_called_once()
        tool, peak = peaks.record.call_args.args
        self.assertEqual("kubectl", tool)
        self.assertGreater(peak, 0)


class ReapTest(unittest.TestCase):
    """`_reap` is the one place a brokered child is waited for: wait4, so the
    child's own rusage comes back, and Popen's view set from it."""

    def test_a_blocking_reap_returns_the_childs_own_peak_and_sets_the_returncode(self):
        process = subprocess.Popen(["sh", "-c", "exit 3"])
        reaped, peak = _reap(process, None)
        self.assertTrue(reaped)
        self.assertGreater(peak, 0)
        self.assertEqual(3, process.returncode)
        # Popen agrees and does not wait again.
        self.assertEqual(3, process.wait())
        self.assertEqual(3, process.poll())

    def test_a_signal_is_the_negative_number_as_popen_reports_it(self):
        process = subprocess.Popen(["sleep", "30"])
        process.kill()
        reaped, peak = _reap(process, None)
        self.assertTrue(reaped)
        self.assertEqual(-9, process.returncode)
        self.assertIsNotNone(peak)

    def test_a_timed_reap_gives_up_on_a_running_child_and_takes_it_once_it_exits(self):
        process = subprocess.Popen(["sleep", "30"])
        try:
            started = time.monotonic()
            self.assertEqual((False, None), _reap(process, 0))
            self.assertEqual((False, None), _reap(process, 0.05))
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsNone(process.returncode)
        finally:
            process.kill()
        reaped, peak = _reap(process, 1)
        self.assertTrue(reaped)
        self.assertEqual(-9, process.returncode)

    def test_a_child_already_reaped_is_taken_from_popens_view_with_no_peak(self):
        process = subprocess.Popen(["sh", "-c", "exit 2"])
        self.assertEqual(2, process.wait())
        self.assertEqual((True, None), _reap(process, None))

    def test_capture_output_carries_the_peak_on_the_ordinary_path(self):
        process = subprocess.Popen(["sh", "-c", "echo out; echo err >&2"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        captured = _capture_output(process, stdin=None, limit=4096, timeout=5)
        self.assertEqual((b"out\n", b"err\n", 0), (captured.stdout, captured.stderr, process.returncode))
        self.assertGreater(captured.peak_rss_bytes, 0)

    def test_capture_output_carries_the_peak_when_the_child_closed_its_pipes_early(self):
        # The pipes close but the command runs on: the stepped poll path.
        process = subprocess.Popen(["sh", "-c", "exec >/dev/null 2>&1; sleep 0.3; exit 0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        captured = _capture_output(process, stdin=None, limit=4096, timeout=5)
        self.assertEqual(0, process.returncode)
        self.assertFalse(captured.timed_out)
        self.assertGreater(captured.peak_rss_bytes, 0)

    def test_capture_output_carries_the_peak_of_a_timed_out_child(self):
        process = subprocess.Popen(["sh", "-c", "sleep 30"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        captured = _capture_output(process, stdin=None, limit=4096, timeout=0.2)
        self.assertTrue(captured.timed_out)
        self.assertIsNotNone(process.returncode)
        self.assertGreater(captured.peak_rss_bytes, 0)


class _Readings:
    def __init__(self, *snapshots: MemorySnapshot) -> None:
        self._snapshots = list(snapshots)

    def read(self) -> MemorySnapshot:
        return self._snapshots.pop(0) if len(self._snapshots) > 1 else self._snapshots[0]


def _snapshot(peak: int, *, events: dict[str, int] | None = None, children=None) -> MemorySnapshot:
    return MemorySnapshot(
        cgroup={"current": _CURRENT, "peak": peak, "limit": _LIMIT, "anon": _ANON, "file": _FILE},
        events=events if events is not None else {"high": 0, "max": 0, "oom": 0, "oom_kill": 0},
        processes={"broker": {"rss": 110 * _MIB, "peak": 150 * _MIB}, "envoy": {"rss": 68 * _MIB, "peak": 68 * _MIB}},
        children=children or {"kubectl": 432 * _MIB},
        largest_child=("kubectl", 432 * _MIB),
    )


class MemorySamplerTest(unittest.TestCase):
    def test_the_first_tick_logs_the_breakdown_and_a_steady_broker_logs_nothing_more(self):
        sampler = MemorySampler(_Readings(_snapshot(543 * _MIB)), step_bytes=64 * _MIB)
        with self.assertLogs(credential_proxy.LOGGER, level=logging.INFO) as logs:
            line = sampler.tick()
        self.assertEqual(
            "memory at start: cgroup_peak=543MiB current=240MiB limit=2048MiB anon=111MiB file=106MiB "
            "broker_rss=110MiB broker_peak=150MiB envoy_rss=68MiB envoy_peak=68MiB largest_child=kubectl:432MiB "
            "events high=0 max=0 oom=0 oom_kill=0",
            line,
        )
        self.assertEqual(logging.INFO, logs.records[0].levelno)
        self.assertIsNone(sampler.tick())

    def test_a_high_water_a_step_up_logs_and_one_under_the_step_does_not(self):
        sampler = MemorySampler(_Readings(_snapshot(543 * _MIB), _snapshot(600 * _MIB), _snapshot(610 * _MIB)), step_bytes=64 * _MIB)
        with self.assertLogs(credential_proxy.LOGGER, level=logging.INFO):
            sampler.tick()
        self.assertIsNone(sampler.tick())
        with self.assertLogs(credential_proxy.LOGGER, level=logging.INFO):
            line = sampler.tick()
        self.assertTrue(line.startswith("memory high-water: cgroup_peak=610MiB"), line)

    def test_an_event_counter_moving_is_a_warning(self):
        sampler = MemorySampler(
            _Readings(_snapshot(543 * _MIB), _snapshot(2048 * _MIB, events={"high": 0, "max": 3, "oom": 0, "oom_kill": 0})),
        )
        with self.assertLogs(credential_proxy.LOGGER, level=logging.INFO):
            sampler.tick()
        with self.assertLogs(credential_proxy.LOGGER, level=logging.WARNING) as logs:
            line = sampler.tick()
        self.assertTrue(line.startswith("memory limit reached: cgroup_peak=2048MiB"), line)
        self.assertIn("events high=0 max=3 oom=0 oom_kill=0", line)
        self.assertEqual(1, len(logs.records))
        # The same count again is not news.
        self.assertIsNone(sampler.tick())

    def test_without_memory_peak_the_sampler_steps_on_its_own_high_water_of_current(self):
        # A kernel before 5.19 has no memory.peak: the sampler then keeps the
        # largest `current` it has seen and writes the high-water line as that
        # climbs, instead of falling silent after the first line.
        def without_peak(current):
            snapshot = _snapshot(0)
            cgroup = dict(snapshot.cgroup)
            del cgroup["peak"]
            cgroup["current"] = current
            return snapshot._replace(cgroup=cgroup)

        sampler = MemorySampler(
            _Readings(without_peak(300 * _MIB), without_peak(320 * _MIB), without_peak(400 * _MIB), without_peak(200 * _MIB)),
            step_bytes=64 * _MIB,
        )
        with self.assertLogs(credential_proxy.LOGGER, level=logging.INFO) as logs:
            self.assertTrue(sampler.tick().startswith("memory at start: cgroup_peak=? current=300MiB"))
        self.assertIsNone(sampler.tick())
        with self.assertLogs(credential_proxy.LOGGER, level=logging.INFO):
            self.assertTrue(sampler.tick().startswith("memory high-water: cgroup_peak=? current=400MiB"))
        self.assertIsNone(sampler.tick())

    def test_a_reader_that_fails_does_not_end_the_thread(self):
        reader = mock.Mock()
        reader.read.side_effect = [OSError("gone"), _snapshot(543 * _MIB)]
        stop = mock.Mock()
        stop.is_set.side_effect = [False, False, True]
        sampler = MemorySampler(reader, interval_s=0, stop=stop)
        with self.assertLogs(credential_proxy.LOGGER, level=logging.INFO) as logs:
            sampler.run()
        self.assertEqual(["memory sample failed type=OSError"], [r.getMessage() for r in logs.records if "failed" in r.getMessage()])
        self.assertEqual(2, reader.read.call_count)

    def test_a_snapshot_with_nothing_readable_still_describes_itself(self):
        empty = MemorySnapshot(cgroup={}, events={}, processes={}, children={}, largest_child=None)
        self.assertEqual("cgroup_peak=? current=? limit=?", MemorySampler.describe(empty))


class MemorySeriesTest(unittest.TestCase):
    def test_the_series_render_from_the_reader_at_scrape_time(self):
        metrics = ProxyMetrics()
        snapshot = _snapshot(543 * _MIB, events={"max": 2, "oom_kill": 0}, children={"kubectl": 432 * _MIB, "gcloud": 102 * _MIB})
        metrics.set_memory_reader(lambda: snapshot)
        families = _families(metrics.render())
        self.assertEqual(_LIMIT, _series(families, "kubeagents_credential_proxy_cgroup_memory_bytes", kind="limit"))
        self.assertEqual(543 * _MIB, _series(families, "kubeagents_credential_proxy_cgroup_memory_bytes", kind="peak"))
        self.assertEqual(_ANON, _series(families, "kubeagents_credential_proxy_cgroup_memory_bytes", kind="anon"))
        self.assertEqual(2, _series(families, "kubeagents_credential_proxy_cgroup_memory_events_total", event="max"))
        self.assertEqual(150 * _MIB, _series(families, "kubeagents_credential_proxy_process_memory_bytes", process="broker", kind="peak"))
        self.assertEqual(68 * _MIB, _series(families, "kubeagents_credential_proxy_process_memory_bytes", process="envoy", kind="rss"))
        self.assertEqual(432 * _MIB, _series(families, "kubeagents_credential_proxy_child_peak_rss_bytes", tool="kubectl"))
        self.assertEqual(102 * _MIB, _series(families, "kubeagents_credential_proxy_child_peak_rss_bytes", tool="gcloud"))
        self.assertIn("# TYPE kubeagents_credential_proxy_cgroup_memory_events_total counter", metrics.render())

    def test_a_registry_without_a_reader_renders_no_memory_series(self):
        text = ProxyMetrics().render()
        for name in (
            "kubeagents_credential_proxy_cgroup_memory_bytes",
            "kubeagents_credential_proxy_cgroup_memory_events_total",
            "kubeagents_credential_proxy_process_memory_bytes",
            "kubeagents_credential_proxy_child_peak_rss_bytes",
        ):
            self.assertNotIn(name, text)

    def test_the_label_values_are_the_closed_vocabularies(self):
        metrics = ProxyMetrics()
        metrics.set_memory_reader(lambda: _snapshot(543 * _MIB))
        for line in metrics.render().splitlines():
            if line.startswith("kubeagents_credential_proxy_cgroup_memory_bytes"):
                kind = dict(_LABEL_PAIR.findall(line))["kind"]
                self.assertIn(kind, {"current", "peak", "limit", "anon", "file"})


if __name__ == "__main__":
    unittest.main()

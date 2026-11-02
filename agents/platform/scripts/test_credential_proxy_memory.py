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
import os
import re
import subprocess
import tempfile
import threading
import time
import types
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



# -- the resource watermark gate (#4333) ------------------------------------

_GIB = 1024 * _MIB
_STATE_LIMIT = 5 * _GIB
_TMP_LIMIT = 2 * _GIB


def _fake_memory(root: Path, *, current: int, limit: str | None = str(_LIMIT), inactive_file: int = 0) -> Path:
    cgroup = root / "cgroup"
    _write(cgroup / "memory.current", f"{current}\n")
    _write(cgroup / "memory.stat", f"anon {current - inactive_file}\nfile {inactive_file}\ninactive_file {inactive_file}\n")
    if limit is not None:
        _write(cgroup / "memory.max", f"{limit}\n")
    return cgroup


def _percent(limit: int, percent: float) -> int:
    return int(limit * percent / 100)


class _Retry:
    """A Retry-After draw that records the range it was asked for."""

    def __init__(self, value: int = 17) -> None:
        self.value = value
        self.ranges: list[tuple[int, int]] = []

    def __call__(self, low: int, high: int) -> int:
        self.ranges.append((low, high))
        return self.value


class ResourceWatermarkGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sizes = {"state": 0, "tmp": 0}
        self.reclaimed: list[str] = []
        self.relieved = 0
        self.retry = _Retry()

    def _relieve(self) -> bool:
        self.relieved += 1
        return True

    def gate(self, *, memory_percent=50.0, limit=str(_LIMIT), state_limit=_STATE_LIMIT, tmp_limit=_TMP_LIMIT, statvfs=None, thresholds=None):
        cgroup = _fake_memory(self.root, current=_percent(_LIMIT, memory_percent), limit=limit)
        volumes = (
            credential_proxy.WatchedVolume("state", self.root / "state", state_limit),
            credential_proxy.WatchedVolume("tmp", self.root / "tmp", tmp_limit),
        )
        return credential_proxy.ResourceWatermarkGate(
            thresholds or credential_proxy.watermark_thresholds_from_env({}),
            volumes,
            cgroup_dir=cgroup,
            tree_bytes=lambda path: self.sizes[path.name],
            statvfs=statvfs or (lambda path: self.fail(f"statvfs asked for {path} with a cap known")),
            reclaimers=(lambda: self.reclaimed.append("workspaces") or 2, lambda: self.reclaimed.append("vcs") or 1),
            relieve=self._relieve,
            retry_after=self.retry,
        )

    def set_memory(self, gate, percent):
        (self.root / "cgroup" / "memory.current").write_text(f"{_percent(_LIMIT, percent)}\n")

    def test_below_the_soft_watermark_everyone_is_admitted_and_nothing_clamps(self):
        gate = self.gate(memory_percent=50)
        self.sizes["state"] = _percent(_STATE_LIMIT, 60)
        self.sizes["tmp"] = _percent(_TMP_LIMIT, 10)
        self.assertEqual("normal", gate.tick())
        for role in ("shell", "session", "profile", "scope", "watcher", None):
            with gate.admit(role):
                pass
        gate.check("watcher")
        self.assertFalse(gate.clamps_concurrency)
        self.assertEqual([], self.reclaimed)
        self.assertEqual(0, self.relieved)
        snapshot = gate.snapshot()
        self.assertEqual("normal", snapshot.level)
        self.assertAlmostEqual(0.5, snapshot.ratios["memory"], places=3)
        self.assertAlmostEqual(0.6, snapshot.ratios["state"], places=3)
        self.assertAlmostEqual(0.1, snapshot.ratios["tmp"], places=3)
        self.assertEqual([], self.retry.ranges)

    def test_at_76_percent_memory_the_tick_clamps_and_relieves_once_per_episode(self):
        gate = self.gate(memory_percent=76)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            self.assertEqual("soft", gate.tick())
        self.assertIn("resource watermark normal -> soft (memory=76% state=0% tmp=0%)", "\n".join(logs.output))
        self.assertIn("resource watermark relief: reclaimed=2,1 trimmed=True", "\n".join(logs.output))
        self.assertTrue(gate.clamps_concurrency)
        self.assertEqual(["workspaces", "vcs"], self.reclaimed)
        self.assertEqual(1, self.relieved)
        # Still soft: no second relief; a shell caller is still admitted.
        self.assertEqual("soft", gate.tick())
        self.assertEqual(1, self.relieved)
        with gate.admit("shell"):
            pass
        # Back under, then over again: the step is armed once more.
        self.set_memory(gate, 50)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            self.assertEqual("normal", gate.tick())
        self.assertIn("resource watermark soft -> normal", "\n".join(logs.output))
        self.assertFalse(gate.clamps_concurrency)
        self.set_memory(gate, 76)
        gate.tick()
        self.assertEqual(2, self.relieved)
        self.assertEqual(["workspaces", "vcs", "workspaces", "vcs"], self.reclaimed)

    def test_an_episode_that_ends_and_begins_on_the_request_path_arms_the_next_tick(self):
        # The level moves on every request's memory read, not only on the
        # tick: an episode can end and a new one begin between two ticks,
        # and the second owes the tick its relief too.
        gate = self.gate(memory_percent=76)
        self.assertEqual("soft", gate.tick())
        self.assertEqual(1, self.relieved)
        self.set_memory(gate, 50)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            gate.check("shell")
        self.assertIn("resource watermark soft -> normal", "\n".join(logs.output))
        self.set_memory(gate, 77)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            with gate.admit("shell"):
                pass
        self.assertIn("resource watermark normal -> soft", "\n".join(logs.output))
        self.assertEqual(1, self.relieved)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            self.assertEqual("soft", gate.tick())
        self.assertIn("resource watermark relief: reclaimed=2,1 trimmed=True", "\n".join(logs.output))
        self.assertEqual(2, self.relieved)
        self.assertEqual("soft", gate.tick())
        self.assertEqual(2, self.relieved)

    def test_at_71_percent_disk_on_either_volume_the_tick_clamps(self):
        gate = self.gate(memory_percent=10)
        self.sizes["tmp"] = _percent(_TMP_LIMIT, 71)
        self.assertEqual("soft", gate.tick())
        self.assertTrue(gate.clamps_concurrency)
        self.assertEqual(1, self.relieved)
        self.sizes["tmp"] = 0
        self.sizes["state"] = _percent(_STATE_LIMIT, 71)
        self.assertEqual("soft", gate.tick())
        self.sizes["state"] = _percent(_STATE_LIMIT, 69)
        self.assertEqual("normal", gate.tick())

    def test_at_86_percent_memory_a_background_caller_is_refused_at_once_and_one_session_is_admitted(self):
        gate = self.gate(memory_percent=86)
        for role in ("shell", "profile", "scope", "watcher", None):
            with self.assertRaises(credential_proxy.ResourcePressure) as raised:
                with gate.admit(role):
                    self.fail(f"{role!r} was admitted at the hard watermark")
            self.assertEqual(17, raised.exception.retry_after)
            self.assertEqual("pressure", raised.exception.bound)
            self.assertIn("hard resource watermark (memory=86%", str(raised.exception))
            self.assertIn("interactive sessions only; retry in 17 s", str(raised.exception))
        self.assertEqual([(5, 30)] * 5, self.retry.ranges)
        # The one reserved slot: a session is admitted, a second refused while
        # the first holds it, and admitted once it is released.
        with gate.admit("session"):
            with self.assertRaises(credential_proxy.ResourcePressure) as raised:
                with gate.admit("session"):
                    self.fail("a second session was admitted into the one reserved slot")
            self.assertIn("1 interactive slot(s) are in use", str(raised.exception))
        with gate.admit("session"):
            pass
        self.assertTrue(gate.clamps_concurrency)

    def test_the_memory_ratio_is_the_working_set_not_the_page_cache(self):
        # 90% of the limit in memory.current, 60% of it inactive file pages the
        # kernel drops first: the gate reads 30%, as the kubelet would, and a
        # broker at rest after a clone is not "hard". Without memory.stat the
        # cache is counted.
        cgroup = _fake_memory(self.root, current=_percent(_LIMIT, 90), inactive_file=_percent(_LIMIT, 60))
        gate = self.gate(memory_percent=90)
        (cgroup / "memory.stat").write_text(f"anon 1\nfile 1\ninactive_file {_percent(_LIMIT, 60)}\n")
        self.assertAlmostEqual(0.3, gate.read_memory_ratio(), places=3)
        with gate.admit("shell"):
            pass
        (cgroup / "memory.stat").unlink()
        self.assertAlmostEqual(0.9, gate.read_memory_ratio(), places=3)

    def test_the_memory_read_is_per_admission_not_per_tick(self):
        gate = self.gate(memory_percent=50)
        gate.tick()
        self.set_memory(gate, 86)
        with self.assertRaises(credential_proxy.ResourcePressure):
            with gate.admit("shell"):
                pass
        self.assertEqual("hard", gate.level)

    def test_past_the_ceiling_the_session_is_refused_too(self):
        gate = self.gate(memory_percent=93)
        with self.assertRaises(credential_proxy.ResourcePressure) as raised:
            with gate.admit("session"):
                pass
        self.assertIn("interactive resource ceiling", str(raised.exception))
        gate = self.gate(memory_percent=10)
        self.sizes["tmp"] = _percent(_TMP_LIMIT, 91)
        self.assertEqual("ceiling", gate.tick())
        with self.assertRaises(credential_proxy.ResourcePressure):
            with gate.admit("session"):
                pass
        # Under the disk ceiling but over its hard watermark, the reserve holds.
        self.sizes["tmp"] = _percent(_TMP_LIMIT, 85)
        self.assertEqual("hard", gate.tick())
        with gate.admit("session"):
            pass

    def test_the_hard_check_alone_refuses_a_read_without_holding_the_reserve(self):
        gate = self.gate(memory_percent=86)
        with self.assertRaises(credential_proxy.ResourcePressure):
            gate.check("watcher")
        gate.check("session")
        gate.check("session")
        with gate.admit("session"):
            pass

    def test_a_missing_or_unlimited_memory_max_turns_the_memory_half_off(self):
        for limit in (None, "max"):
            with self.subTest(limit=limit):
                with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                    gate = self.gate(memory_percent=99, limit=limit)
                self.assertIn("memory half off", "\n".join(logs.output))
                self.assertIsNone(gate.memory_limit_bytes)
                self.assertEqual("normal", gate.tick())
                self.assertNotIn("memory", gate.snapshot().ratios)
                with gate.admit("shell"):
                    pass
                # The disk half still gates.
                self.sizes["tmp"] = _percent(_TMP_LIMIT, 86)
                self.assertEqual("hard", gate.tick())
                with self.assertRaises(credential_proxy.ResourcePressure):
                    with gate.admit("shell"):
                        pass
                self.sizes["tmp"] = 0

    def test_a_volume_with_no_cap_is_measured_with_statvfs(self):
        stats = {"state": (100, 15), "tmp": (0, 0)}

        def statvfs(path):
            blocks, available = stats[Path(path).name]
            return types.SimpleNamespace(f_blocks=blocks, f_bavail=available)

        gate = self.gate(memory_percent=10, state_limit=None, tmp_limit=None, statvfs=statvfs)
        self.assertEqual("hard", gate.tick())
        snapshot = gate.snapshot()
        self.assertAlmostEqual(0.85, snapshot.ratios["state"], places=3)
        self.assertNotIn("tmp", snapshot.ratios)
        self.assertIn("state=", gate.describe_limits())
        self.assertIn(":statvfs", gate.describe_limits())

    def test_the_executor_runs_one_command_at_a_time_from_the_soft_watermark(self):
        gate = self.gate(memory_percent=76)
        gate.tick()
        executor = CommandExecutor(timeout_seconds=5, max_output_bytes=4096, state_dir=str(self.root / "exec"), scoped_pool=None, max_concurrent_commands=4)
        executor.watermark_gate = gate
        self.assertEqual(1, executor._effective_cap())
        held = threading.Event()
        release = threading.Event()

        def hold():
            with executor.request_slot():
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        self.assertTrue(held.wait(5))
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with executor.request_slot():
                    self.fail("a second command ran under the clamp")
        self.assertIn("limit of 1 concurrent commands (clamped under resource pressure)", str(raised.exception))
        self.assertEqual("slot", raised.exception.bound)
        # The reserve is a slot here too: an interactive request is admitted
        # past the clamp while the holder runs, and a second interactive one
        # waits behind it; the background caller stays held.
        with executor.request_slot(interactive=True):
            self.assertEqual(2, executor.slots_in_use)
            with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
                with self.assertRaises(credential_proxy.CommandSlotUnavailable) as second:
                    with executor.request_slot(interactive=True):
                        self.fail("a third command ran under the clamp")
                with self.assertRaises(credential_proxy.CommandSlotUnavailable):
                    with executor.request_slot():
                        self.fail("a background command ran in the reserved slot")
        self.assertIn("limit of 2 concurrent commands (clamped under resource pressure)", str(second.exception))
        # The clamp lifts with the level, and a waiter sees it on its next poll.
        self.set_memory(gate, 50)
        gate.tick()
        self.assertEqual(4, executor._effective_cap())
        with executor.request_slot():
            self.assertEqual(2, executor.slots_in_use)
        release.set()
        thread.join(5)

    def test_a_session_takes_a_slot_above_the_commands_admitted_before_the_clamp(self):
        # The clamp arrives mid-load: the sweep's commands admitted before it
        # keep their slots. The reserve is one slot above them, bounded by
        # the configured cap, not a fixed count that would hold the session
        # until all but one of them had finished.
        gate = self.gate(memory_percent=50)
        gate.tick()
        executor = CommandExecutor(timeout_seconds=5, max_output_bytes=4096, state_dir=str(self.root / "exec"), scoped_pool=None, max_concurrent_commands=4)
        executor.watermark_gate = gate
        held = [threading.Event() for _ in range(3)]
        release = [threading.Event() for _ in range(3)]

        def hold(index):
            with executor.request_slot():
                held[index].set()
                release[index].wait(5)

        threads = [threading.Thread(target=hold, args=(index,), daemon=True) for index in range(3)]
        for thread in threads:
            thread.start()
        for event in held:
            self.assertTrue(event.wait(5))
        self.set_memory(gate, 76)
        self.assertEqual("soft", gate.tick())
        self.assertEqual(1, executor._effective_cap())
        self.assertEqual(4, executor._effective_cap(interactive=True))
        # Admitted at once, in the one slot above the three background holders.
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with executor.request_slot(interactive=True):
                self.assertEqual(4, executor.slots_in_use)
                # The reserve is one slot: a second session is held, by the
                # configured cap now and by the reserve once a holder exits.
                with self.assertRaises(credential_proxy.CommandSlotUnavailable) as second:
                    with executor.request_slot(interactive=True):
                        self.fail("a second session ran past the configured cap")
                self.assertIn("limit of 4 concurrent commands", str(second.exception))
                release[0].set()
                threads[0].join(5)
                self.assertEqual(3, executor.slots_in_use)
                self.assertEqual(3, executor._effective_cap(interactive=True))
                with self.assertRaises(credential_proxy.CommandSlotUnavailable) as third:
                    with executor.request_slot(interactive=True):
                        self.fail("a second session ran in the one reserved slot")
                self.assertIn("limit of 3 concurrent commands (clamped under resource pressure)", str(third.exception))
                with self.assertRaises(credential_proxy.CommandSlotUnavailable):
                    with executor.request_slot():
                        self.fail("a background command ran under the clamp")
            # The session gone, the reserve is free again above the two holders.
            with executor.request_slot(interactive=True):
                self.assertEqual(3, executor.slots_in_use)
        for event in release:
            event.set()
        for thread in threads:
            thread.join(5)

    def test_the_sampler_ticks_the_gate(self):
        gate = self.gate(memory_percent=76)
        stop = threading.Event()
        reader = MemoryReader(ChildPeakTracker(), cgroup_dir=_fake_cgroup(self.root / "sampler"), proc_root=_fake_proc(self.root / "sampler"))
        sampler = MemorySampler(reader, interval_s=0.01, stop=stop, gate=gate)
        thread = threading.Thread(target=sampler.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while gate.level != "soft" and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(5)
        self.assertEqual("soft", gate.level)
        self.assertEqual(1, self.relieved)

    def test_reclaimers_wired_during_an_episode_are_run_on_the_next_tick(self):
        # serve() starts the sampler before the stores exist: an episode
        # under way at boot has its first relief with no reclaimers, and
        # wiring them owes the episode one more, once.
        gate = self.gate(memory_percent=76)
        gate.set_reclaimers(())
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            self.assertEqual("soft", gate.tick())
        self.assertIn("relief: reclaimed=- trimmed=True", "\n".join(logs.output))
        self.assertEqual(1, self.relieved)
        gate.set_reclaimers([lambda: self.reclaimed.append("late") or 4])
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            self.assertEqual("soft", gate.tick())
        self.assertIn("relief: reclaimed=4 trimmed=True", "\n".join(logs.output))
        self.assertEqual(["late"], self.reclaimed)
        self.assertEqual(2, self.relieved)
        self.assertEqual("soft", gate.tick())
        self.assertEqual(2, self.relieved)

    def test_a_reclaimer_that_fails_does_not_stop_the_relief(self):
        gate = self.gate(memory_percent=76)

        def broken():
            raise RuntimeError("no")

        gate.set_reclaimers([broken, lambda: 3])
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            gate.tick()
        self.assertIn("resource watermark reclaim failed type=RuntimeError", "\n".join(logs.output))
        self.assertIn("reclaimed=0,3", "\n".join(logs.output))
        self.assertEqual(1, self.relieved)

    def test_the_gauge_and_the_refusal_counter_render_the_gate(self):
        gate = self.gate(memory_percent=86)
        self.sizes["tmp"] = _percent(_TMP_LIMIT, 25)
        gate.tick()
        metrics = ProxyMetrics()
        metrics.set_watermark_reader(gate.snapshot)
        metrics.record_refusal("pressure")
        families = _families(metrics.render())
        self.assertAlmostEqual(0.86, _series(families, "kubeagents_credential_proxy_resource_watermark_ratio", resource="memory"), places=3)
        self.assertAlmostEqual(0.25, _series(families, "kubeagents_credential_proxy_resource_watermark_ratio", resource="tmp"), places=3)
        self.assertEqual(0.0, _series(families, "kubeagents_credential_proxy_resource_watermark_ratio", resource="state"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_refusals_total", bound="pressure"))
        self.assertIn("pressure", credential_proxy.ADMISSION_BOUNDS)
        self.assertNotIn("kubeagents_credential_proxy_resource_watermark_ratio", ProxyMetrics().render())


class WatermarkEnvTest(unittest.TestCase):
    def test_the_defaults(self):
        self.assertEqual((75, 85, 70, 80), tuple(credential_proxy.watermark_thresholds_from_env({})))

    def test_a_valid_pair_is_read_and_the_other_pair_keeps_its_defaults(self):
        thresholds = credential_proxy.watermark_thresholds_from_env(
            {"CREDENTIAL_PROXY_DISK_SOFT_PCT": "60", "CREDENTIAL_PROXY_DISK_HARD_PCT": "70"}
        )
        self.assertEqual((75, 85, 60, 70), tuple(thresholds))
        self.assertEqual((75, 90, 70, 80), tuple(credential_proxy.watermark_thresholds_from_env({"CREDENTIAL_PROXY_MEM_HARD_PCT": "90"})))

    def test_a_pair_out_of_order_past_the_ceiling_or_not_a_number_falls_back_with_a_warning(self):
        for env in (
            {"CREDENTIAL_PROXY_MEM_SOFT_PCT": "90", "CREDENTIAL_PROXY_MEM_HARD_PCT": "80"},
            {"CREDENTIAL_PROXY_MEM_HARD_PCT": "95"},
            {"CREDENTIAL_PROXY_MEM_SOFT_PCT": "lots"},
            {"CREDENTIAL_PROXY_MEM_SOFT_PCT": "0"},
            {"CREDENTIAL_PROXY_MEM_HARD_PCT": "101"},
        ):
            with self.subTest(env=env):
                with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                    thresholds = credential_proxy.watermark_thresholds_from_env(env)
                self.assertEqual((75, 85, 70, 80), tuple(thresholds))
                self.assertIn("using 75/85", "\n".join(logs.output))

    def test_a_volume_cap_is_a_positive_byte_count_or_statvfs(self):
        self.assertIsNone(credential_proxy.volume_limit_from_env("CREDENTIAL_PROXY_TMP_LIMIT_BYTES", {}))
        self.assertEqual(2147483648, credential_proxy.volume_limit_from_env("CREDENTIAL_PROXY_TMP_LIMIT_BYTES", {"CREDENTIAL_PROXY_TMP_LIMIT_BYTES": "2147483648"}))
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            self.assertIsNone(credential_proxy.volume_limit_from_env("CREDENTIAL_PROXY_TMP_LIMIT_BYTES", {"CREDENTIAL_PROXY_TMP_LIMIT_BYTES": "2Gi"}))
        self.assertIn("measuring that volume with statvfs", "\n".join(logs.output))


class ReliefStepTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_tree_allocated_bytes_counts_blocks_and_does_not_follow_symlinks(self):
        big = self.root / "outside.bin"
        big.write_bytes(b"x" * _MIB)
        tree = self.root / "tree"
        (tree / "a").mkdir(parents=True)
        (tree / "a" / "file").write_bytes(b"y" * (256 * _KIB))
        (tree / "link").symlink_to(big)
        counted = credential_proxy.tree_allocated_bytes(tree)
        self.assertGreaterEqual(counted, 256 * _KIB)
        self.assertLess(counted, _MIB)
        self.assertEqual(0, credential_proxy.tree_allocated_bytes(self.root / "absent"))

    def test_vcs_scratch_a_dead_process_left_is_swept_at_start_and_a_live_tree_is_kept_at_relief(self):
        import vcs_broker

        root = self.root / "vcs"
        root.mkdir()
        # A restarted container's broker gets the dead one's pid, so an
        # orphan can carry this very pid; the start sweep takes it anyway.
        for name in ("clone-1-3", f"clone-{os.getpid()}-7", "publish-2-9"):
            (root / name).mkdir()
            (root / name / "object").write_text("x")
        (root / "notes").mkdir()
        (root / "branch-4-4").symlink_to(root / "notes")
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            broker = vcs_broker.VcsBroker(root, git_runner=lambda *args, **kwargs: None)
        self.assertIn("removed 3 orphaned scratch tree(s)", "\n".join(logs.output))
        self.assertEqual({"notes", "branch-4-4"}, {entry.name for entry in root.iterdir()})
        live = broker._scratch("clone")
        self.assertTrue(vcs_broker.SCRATCH_NAME_RE.match(live.name))
        # A tree this process minted and no request holds any more.
        stale = root / vcs_broker.SCRATCH_NAME_FORMAT.format(kind="snapshot", pid=os.getpid(), sequence=0)
        stale.mkdir()
        self.assertEqual(1, broker.reclaim_orphan_scratch())
        self.assertTrue(live.exists())
        self.assertFalse(stale.exists())
        broker._discard_scratch(live)
        self.assertFalse(live.exists())
        self.assertEqual(0, broker.reclaim_orphan_scratch())

    def test_vcs_scratch_minted_while_the_reclaimer_lists_the_root_is_kept(self):
        import vcs_broker

        root = self.root / "vcs"
        root.mkdir()
        broker = vcs_broker.VcsBroker(root, git_runner=lambda *args, **kwargs: None)
        minted: list[Path] = []
        listing = broker._scratch_names

        def mint_then_list():
            # A request's `_scratch` lands between the reclaimer's entry and
            # its listing of the root: the tree is in the live set already,
            # and a sequence snapshot taken at entry would not know it.
            minted.append(broker._scratch("publish"))
            return listing()

        with mock.patch.object(broker, "_scratch_names", mint_then_list):
            self.assertEqual(0, broker.reclaim_orphan_scratch())
        self.assertTrue(minted[0].exists())
        broker._discard_scratch(minted[0])
        self.assertFalse(minted[0].exists())

    def test_vcs_scratch_stays_in_flight_until_its_tree_is_gone(self):
        import vcs_broker

        root = self.root / "vcs"
        root.mkdir()
        broker = vcs_broker.VcsBroker(root, git_runner=lambda *args, **kwargs: None)
        live = broker._scratch("clone")
        (live / "object").write_text("x")
        in_flight_during_removal: list[bool] = []
        remove = vcs_broker._remove_tree

        def observing_remove(path):
            # The reclaimer on the sampler thread asks this while the tree is
            # still on disk; a path already let go would be removed as an
            # orphan and counted as relief.
            in_flight_during_removal.append(broker._in_flight(path))
            remove(path)

        with mock.patch.object(vcs_broker, "_remove_tree", observing_remove):
            broker._discard_scratch(live)
        self.assertEqual([True], in_flight_during_removal)
        self.assertFalse(live.exists())
        self.assertFalse(broker._in_flight(live))

    def test_release_process_memory_collects_and_trims_where_libc_has_it(self):
        self.assertIsInstance(credential_proxy.release_process_memory(), bool)


if __name__ == "__main__":
    unittest.main()

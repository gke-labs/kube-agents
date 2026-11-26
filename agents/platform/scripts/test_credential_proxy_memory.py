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

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
import http.client
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import credential_proxy
from credential_proxy import (
    ChildPeakTracker,
    CommandExecutor,
    CredentialProxyHandler,
    MemoryReader,
    MemorySampler,
    MemorySnapshot,
    ProxyMetrics,
    _JSON_STREAM_SLICE_CHARS,
    _LARGE_OUTPUT_THRESHOLD_BYTES,
    _MALLOC_ARENA_SETTINGS,
    _bounded_text,
    _capture_output,
    _configure_malloc_arena,
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

    def gate(self, *, memory_percent=50.0, limit=str(_LIMIT), state_limit=_STATE_LIMIT, tmp_limit=_TMP_LIMIT, thresholds=None):
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

    def test_a_reading_that_hovers_at_a_watermark_moves_the_level_once(self):
        # A working set sitting within a reading's swing of the soft watermark
        # while a sweep's children come and go: the level is entered at 75%
        # and left only under 72% (the leave band), so the requests between
        # neither log a transition nor arm the relief step again, and the
        # tick that lands in the band relieves nothing a second time.
        gate = self.gate(memory_percent=76)
        self.assertEqual("soft", gate.tick())
        self.assertEqual(1, self.relieved)
        for percent in (74, 72.5, 74.5):
            self.set_memory(gate, percent)
            with self.assertNoLogs(credential_proxy.LOGGER, level="INFO"):
                gate.check("shell")
            self.assertEqual("soft", gate.level, percent)
        with self.assertNoLogs(credential_proxy.LOGGER, level="INFO"):
            self.assertEqual("soft", gate.tick())
        self.assertEqual(1, self.relieved)
        # Under the band the episode ends; back inside it, and up to the
        # watermark itself, nothing is entered until the watermark is reached.
        self.set_memory(gate, 71)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            gate.check("shell")
        self.assertIn("resource watermark soft -> normal (memory=71%", "\n".join(logs.output))
        self.set_memory(gate, 74)
        with self.assertNoLogs(credential_proxy.LOGGER, level="INFO"):
            gate.check("shell")
        self.assertEqual("normal", gate.level)
        self.set_memory(gate, 75)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            gate.check("shell")
        self.assertIn("resource watermark normal -> soft (memory=75%", "\n".join(logs.output))
        gate.tick()
        self.assertEqual(2, self.relieved)
        # The band holds between every pair of levels: from hard, 83% is
        # still hard and 82% is soft, not normal; from the ceiling, 89.5%.
        self.set_memory(gate, 86)
        with gate.admit("session"):
            pass
        self.assertEqual("hard", gate.level)
        self.set_memory(gate, 83)
        gate.check("session")
        self.assertEqual("hard", gate.level)
        self.set_memory(gate, 82)
        gate.check("session")
        self.assertEqual("soft", gate.level)
        self.set_memory(gate, 93)
        self.assertEqual("ceiling", gate.tick())
        self.set_memory(gate, 89.5)
        self.assertEqual("ceiling", gate.tick())
        self.set_memory(gate, 88)
        self.assertEqual("hard", gate.tick())
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
        # 69% is inside the leave band under 70%; 66% is out of it.
        self.sizes["state"] = _percent(_STATE_LIMIT, 69)
        self.assertEqual("soft", gate.tick())
        self.sizes["state"] = _percent(_STATE_LIMIT, 66)
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

    def test_a_volume_with_no_cap_is_off_and_said_so_once_at_start(self):
        # An emptyDir sits on the node's filesystem, which the kubelet's image
        # GC keeps at 80-85% on a busy node; statvfs there against the volume
        # watermarks would hold a healthy node's broker at hard for good. A
        # volume with no cap is not read at all: its tree is not walked (a
        # walk here raises KeyError, which is not an OSError the reader
        # swallows), it has no ratio, and the other volume still counts.
        del self.sizes["state"]
        self.sizes["tmp"] = _percent(_TMP_LIMIT, 10)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            gate = self.gate(memory_percent=10, state_limit=None)
        self.assertIn("state volume off", "\n".join(logs.output))
        with self.assertNoLogs(credential_proxy.LOGGER, level="INFO"):
            self.assertEqual("normal", gate.tick())
        self.assertNotIn("state", gate.snapshot().ratios)
        self.sizes["tmp"] = _percent(_TMP_LIMIT, 85)
        self.assertEqual("hard", gate.tick())
        self.assertEqual({"memory", "tmp"}, set(gate.snapshot().ratios))
        self.assertIn("state=", gate.describe_limits())
        self.assertIn(":off", gate.describe_limits())

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

    def test_a_session_under_the_clamp_is_seated_above_the_interactive_class_cap(self):
        # The class caps (§4.2) and the clamp together. The shell's sweep is
        # interactive to the class caps, so it can hold the whole interactive
        # cap when the clamp arrives: here three of four admissions, the cap
        # with one kept for background work. The session's reserve stands
        # above that cap under the clamp, because the clamp admits background
        # work one at a time whatever the kept admission says; a profile
        # request is still held by the clamp, and once the clamp lifts the
        # interactive cap holds a shell request until the count drains.
        gate = self.gate(memory_percent=50)
        gate.tick()
        executor = CommandExecutor(timeout_seconds=5, max_output_bytes=4096, state_dir=str(self.root / "exec"), scoped_pool=None, max_concurrent_commands=4)
        executor.watermark_gate = gate
        self.assertEqual(3, executor.class_cap(credential_proxy.CLASS_INTERACTIVE))
        held = [threading.Event() for _ in range(3)]
        release = [threading.Event() for _ in range(3)]

        def hold(index):
            with executor.request_slot(klass=credential_proxy.CLASS_INTERACTIVE):
                held[index].set()
                release[index].wait(5)

        threads = [threading.Thread(target=hold, args=(index,), daemon=True) for index in range(3)]
        for thread in threads:
            thread.start()
        for event in held:
            self.assertTrue(event.wait(5))
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            # Before the clamp the interactive cap holds a fourth interactive
            # request, and says so.
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as before:
                with executor.request_slot(klass=credential_proxy.CLASS_INTERACTIVE, interactive=True):
                    self.fail("a fourth interactive request ran past the interactive cap")
            self.assertIn("keeps one of its 4 admissions for background work", str(before.exception))
            self.set_memory(gate, 76)
            self.assertEqual("soft", gate.tick())
            # Under the clamp the session is seated at once above the three.
            with executor.request_slot(klass=credential_proxy.CLASS_INTERACTIVE, interactive=True):
                self.assertEqual(4, executor.slots_in_use)
                self.assertEqual(4, executor._slots_by_class[credential_proxy.CLASS_INTERACTIVE])
                with self.assertRaises(credential_proxy.CommandSlotUnavailable) as profile:
                    with executor.request_slot(klass=credential_proxy.CLASS_BACKGROUND):
                        self.fail("a profile command ran under the clamp")
                self.assertIn("limit of 1 concurrent commands (clamped under resource pressure)", str(profile.exception))
                self.assertEqual(credential_proxy.ADMISSION_BOUND_SLOT, profile.exception.bound)
            # The clamp lifts with three still held: the interactive cap is
            # back in force for a shell request, and a profile request has
            # the admission the cap kept for it.
            self.set_memory(gate, 50)
            gate.tick()
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as after:
                with executor.request_slot(klass=credential_proxy.CLASS_INTERACTIVE):
                    self.fail("a shell request ran past the interactive cap once the clamp lifted")
            self.assertIn("keeps one of its 4 admissions for background work", str(after.exception))
            with executor.request_slot(klass=credential_proxy.CLASS_BACKGROUND):
                self.assertEqual(4, executor.slots_in_use)
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

    def test_an_exec_pods_soft_watermark_reclaims_kubectls_cache(self):
        # The exec role's reclaimer: at the soft mark the cache is renamed
        # and removed, a kubectl reading it keeps its open file, and the next
        # command builds a new cache beside the kubeconfig, which stays.
        with tempfile.TemporaryDirectory() as state:
            executor = credential_proxy.CommandExecutor(
                timeout_seconds=5, max_output_bytes=1024, state_dir=state, scoped_pool=None)
            kube = executor.home_dir / ".kube"
            entry = kube / "cache" / "discovery" / "cluster" / "servergroups.json"
            entry.parent.mkdir(parents=True)
            entry.write_text("cached")
            (kube / "config").write_text("kubeconfig")
            leftover = kube / (credential_proxy.KUBECTL_CACHE_RECLAIMED_PREFIX + "old")
            leftover.mkdir()
            gate = self.gate(memory_percent=50)
            gate.set_reclaimers([executor.reclaim_kubectl_cache])
            self.sizes["state"] = _percent(_STATE_LIMIT, 75)
            self.sizes["tmp"] = _percent(_TMP_LIMIT, 10)
            with entry.open() as reader:
                with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                    self.assertEqual("soft", gate.tick())
                self.assertEqual("cached", reader.read())
            self.assertIn("relief: reclaimed=2 trimmed=True", "\n".join(logs.output))
            self.assertFalse((kube / "cache").exists())
            self.assertEqual([], list(kube.glob(credential_proxy.KUBECTL_CACHE_RECLAIMED_PREFIX + "*")))
            self.assertEqual("kubeconfig", (kube / "config").read_text())
            rebuilt = kube / "cache" / "discovery" / "cluster" / "servergroups.json"
            rebuilt.parent.mkdir(parents=True)
            rebuilt.write_text("fresh")
            self.assertEqual("fresh", rebuilt.read_text())
            self.assertEqual(1, executor.reclaim_kubectl_cache())

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
        metrics.record_refusal("pressure", credential_proxy.CLASS_BACKGROUND)
        families = _families(metrics.render())
        self.assertAlmostEqual(0.86, _series(families, "kubeagents_credential_proxy_resource_watermark_ratio", resource="memory"), places=3)
        self.assertAlmostEqual(0.25, _series(families, "kubeagents_credential_proxy_resource_watermark_ratio", resource="tmp"), places=3)
        self.assertEqual(0.0, _series(families, "kubeagents_credential_proxy_resource_watermark_ratio", resource="state"))
        self.assertEqual(
            1, _series(families, "kubeagents_credential_proxy_admission_refusals_total", bound="pressure", **{"class": "background"})
        )
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

    def test_a_volume_cap_is_a_positive_byte_count_or_the_volume_is_off(self):
        self.assertIsNone(credential_proxy.volume_limit_from_env("CREDENTIAL_PROXY_TMP_LIMIT_BYTES", {}))
        self.assertEqual(2147483648, credential_proxy.volume_limit_from_env("CREDENTIAL_PROXY_TMP_LIMIT_BYTES", {"CREDENTIAL_PROXY_TMP_LIMIT_BYTES": "2147483648"}))
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            self.assertIsNone(credential_proxy.volume_limit_from_env("CREDENTIAL_PROXY_TMP_LIMIT_BYTES", {"CREDENTIAL_PROXY_TMP_LIMIT_BYTES": "2Gi"}))
        self.assertIn("that volume's half of the resource watermark gate is off", "\n".join(logs.output))


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

    def test_vcs_scratch_a_request_lets_go_after_the_listing_is_not_counted_as_relief(self):
        import vcs_broker

        root = self.root / "vcs"
        root.mkdir()
        broker = vcs_broker.VcsBroker(root, git_runner=lambda *args, **kwargs: None)
        stale = root / vcs_broker.SCRATCH_NAME_FORMAT.format(kind="snapshot", pid=os.getpid(), sequence=0)
        stale.mkdir()
        live = broker._scratch("clone")
        listing = broker._scratch_names

        def list_then_finish():
            # The request ends between the reclaimer's listing and its tree's
            # turn in the loop: its own cleanup removes the tree and lets the
            # path go, so the reclaimer finds neither a live path nor a tree.
            listed = listing()
            broker._discard_scratch(live)
            return listed

        with mock.patch.object(broker, "_scratch_names", list_then_finish):
            self.assertEqual(1, broker.reclaim_orphan_scratch())
        self.assertFalse(stale.exists())
        self.assertFalse(live.exists())

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


class MallocArenaConfigurationTest(unittest.TestCase):
    def test_configure_malloc_arena_applies_settings_and_returns_bool(self):
        self.assertIsInstance(credential_proxy._MALLOC_ARENA_CONFIGURED, bool)
        self.assertEqual(
            (
                (credential_proxy._M_ARENA_MAX, 1),
                (credential_proxy._M_MMAP_THRESHOLD, 524_288),
                (credential_proxy._M_TRIM_THRESHOLD, 131_072),
            ),
            _MALLOC_ARENA_SETTINGS,
        )
        fake_libc = mock.MagicMock()
        fake_libc.mallopt.return_value = 1
        cleared_env = {name: "" for name in credential_proxy._MALLOC_ENV_OVERRIDES.values()}
        with (
            mock.patch.dict(os.environ, cleared_env),
            mock.patch.object(credential_proxy.ctypes, "CDLL", return_value=fake_libc) as cdll,
        ):
            self.assertTrue(_configure_malloc_arena())
        cdll.assert_called_once_with(credential_proxy.LIBC_SONAME)
        self.assertEqual(
            [mock.call(param, value) for param, value in _MALLOC_ARENA_SETTINGS],
            fake_libc.mallopt.call_args_list,
        )

    def test_configure_malloc_arena_respects_operator_env_overrides(self):
        fake_libc = mock.MagicMock()
        fake_libc.mallopt.return_value = 1
        env = {
            "MALLOC_ARENA_MAX": "2",
            "MALLOC_MMAP_THRESHOLD_": "",
            "MALLOC_TRIM_THRESHOLD_": "65536",
        }
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(credential_proxy.ctypes, "CDLL", return_value=fake_libc),
        ):
            self.assertTrue(_configure_malloc_arena())
        self.assertEqual(
            [mock.call(credential_proxy._M_MMAP_THRESHOLD, 524_288)],
            fake_libc.mallopt.call_args_list,
        )

    def test_configure_malloc_arena_handles_missing_or_failing_mallopt_and_oserror(self):
        cleared_env = {name: "" for name in credential_proxy._MALLOC_ENV_OVERRIDES.values()}
        with mock.patch.dict(os.environ, cleared_env):
            with mock.patch.object(credential_proxy.ctypes, "CDLL", side_effect=OSError("no libc")):
                self.assertFalse(_configure_malloc_arena())

            libc_without_mallopt = object()
            with mock.patch.object(credential_proxy.ctypes, "CDLL", return_value=libc_without_mallopt):
                self.assertFalse(_configure_malloc_arena())

            failing_libc = mock.MagicMock()
            failing_libc.mallopt.return_value = 0
            with mock.patch.object(credential_proxy.ctypes, "CDLL", return_value=failing_libc):
                self.assertFalse(_configure_malloc_arena())

            partial_libc = mock.MagicMock()
            partial_libc.mallopt.side_effect = [1, 0, 1]
            with mock.patch.object(credential_proxy.ctypes, "CDLL", return_value=partial_libc):
                self.assertFalse(_configure_malloc_arena())
            self.assertEqual(len(_MALLOC_ARENA_SETTINGS), partial_libc.mallopt.call_count)


class BoundedTextBytearrayTest(unittest.TestCase):
    def test_bounded_text_decodes_bytearray_below_and_at_limit(self):
        text, truncated = _bounded_text(bytearray(b"hello world\n"), 64)
        self.assertEqual("hello world\n", text)
        self.assertFalse(truncated)

        utf8_raw = bytearray("café€".encode("utf-8"))
        text_exact, truncated_exact = _bounded_text(utf8_raw, len(utf8_raw))
        self.assertEqual("café€", text_exact)
        self.assertFalse(truncated_exact)

    def test_bounded_text_handles_zero_and_negative_limits(self):
        self.assertEqual(("", False), _bounded_text(bytearray(), 0))
        self.assertEqual(("", False), _bounded_text(bytearray(), -5))
        self.assertEqual(("", True), _bounded_text(bytearray(b"non-empty"), 0))
        self.assertEqual(("", True), _bounded_text(bytearray(b"non-empty"), -1))

    def test_bounded_text_truncates_bytearray_above_limit_on_utf8_character_boundary(self):
        # "a" (1B) + "é" (2B) + "€" (3B) + "𐍈" (4B) = 10 bytes in UTF-8.
        raw = bytearray("aé€𐍈".encode("utf-8"))
        for limit, expected in (
            (1, "a"),
            (2, "a"),
            (3, "aé"),
            (4, "aé"),
            (5, "aé"),
            (6, "aé€"),
            (9, "aé€"),
        ):
            with self.subTest(limit=limit):
                text, truncated = _bounded_text(raw, limit)
                self.assertEqual(expected, text)
                self.assertTrue(truncated)
                self.assertLessEqual(len(text.encode("utf-8")), limit)

        # Invalid UTF-8 bytes expand to 3-byte U+FFFD replacement characters
        # and are still cut so the UTF-8 byte length stays within `limit`.
        invalid = bytearray(b"\xff" * 10)
        text_inv, truncated_inv = _bounded_text(invalid, 10)
        self.assertEqual("\ufffd" * 3, text_inv)
        self.assertTrue(truncated_inv)
        self.assertLessEqual(len(text_inv.encode("utf-8")), 10)

    def test_bounded_text_multi_chunk_bytearray_straddling_64kib_boundary_and_clears_large_buffer(self):
        chunk_bytes = credential_proxy.OUTPUT_READ_CHUNK_BYTES
        # Place a 4-byte UTF-8 character ("𐍈") straddling the 65536-byte chunk boundary,
        # and size the buffer above _LARGE_OUTPUT_THRESHOLD_BYTES so the incremental
        # path clears the bytearray before joining.
        prefix = b"a" * (chunk_bytes - 2)
        straddle = "𐍈".encode("utf-8")
        suffix = b"b" * (_LARGE_OUTPUT_THRESHOLD_BYTES - len(prefix) - len(straddle) + 4096)
        raw = bytearray(prefix + straddle + suffix)
        total_len = len(raw)

        # Cut inside the straddling 4-byte character (only 1 byte after `prefix` fits):
        text_cut, truncated_cut = _bounded_text(raw, len(prefix) + 1)
        self.assertTrue(truncated_cut)
        self.assertEqual("a" * (chunk_bytes - 2), text_cut)
        self.assertEqual(0, len(raw))

        # Cut right after the straddling 4-byte character:
        raw2 = bytearray(prefix + straddle + suffix)
        text_inc, truncated_inc = _bounded_text(raw2, len(prefix) + len(straddle))
        self.assertTrue(truncated_inc)
        self.assertEqual(("a" * (chunk_bytes - 2)) + "𐍈", text_inc)
        self.assertEqual(0, len(raw2))

        # Invalid byte forcing incremental decode across the full multi-chunk buffer:
        raw3 = bytearray(prefix + straddle + b"\xff" + suffix)
        text_full, truncated_full = _bounded_text(raw3, total_len + 16)
        self.assertFalse(truncated_full)
        self.assertIn("𐍈\ufffd", text_full)
        self.assertEqual(0, len(raw3))


class _CountingLock:
    def __init__(self, wrapped: threading.Lock) -> None:
        self._wrapped = wrapped
        self.entries = 0

    def __enter__(self) -> None:
        self._wrapped.acquire()
        self.entries += 1

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._wrapped.release()


class CaptureOutputBytearrayTest(unittest.TestCase):
    def test_capture_output_raw_bytearray_returns_mutable_bytearray_buffers(self):
        process = subprocess.Popen(
            ["sh", "-c", "printf 'out-bytes'; printf 'err-bytes' >&2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        captured = _capture_output(process, stdin=None, limit=4096, timeout=5, _raw_bytearray=True)
        self.assertIsInstance(captured.stdout, bytearray)
        self.assertIsInstance(captured.stderr, bytearray)
        self.assertEqual(bytearray(b"out-bytes"), captured.stdout)
        self.assertEqual(bytearray(b"err-bytes"), captured.stderr)
        self.assertFalse(captured.truncated)
        self.assertFalse(captured.stdout_truncated)
        self.assertFalse(captured.stopped)

    def test_capture_output_default_returns_bytes_and_serializes_large_payloads(self):
        size = _LARGE_OUTPUT_THRESHOLD_BYTES + 16384
        counting_lock = _CountingLock(credential_proxy._LARGE_OUTPUT_LOCK)

        process = subprocess.Popen(
            ["python3", "-c", f"import sys; sys.stdout.write('A' * {size}); sys.stderr.write('B' * 32)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Also verify that combined stdout + stderr >= threshold acquires the lock
        # even when each stream alone is below the threshold.
        half = (_LARGE_OUTPUT_THRESHOLD_BYTES // 2) + 4096
        process_combined = subprocess.Popen(
            ["python3", "-c", f"import sys; sys.stdout.write('C' * {half}); sys.stderr.write('D' * {half})"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with mock.patch.object(credential_proxy, "_LARGE_OUTPUT_LOCK", counting_lock):
            captured = _capture_output(process, stdin=None, limit=size + 1024, timeout=10)
            captured_combined = _capture_output(process_combined, stdin=None, limit=size + 1024, timeout=10)
        self.assertIsInstance(captured.stdout, bytes)
        self.assertIsInstance(captured.stderr, bytes)
        self.assertEqual(b"A" * size, captured.stdout)
        self.assertEqual(b"B" * 32, captured.stderr)
        self.assertEqual(b"C" * half, captured_combined.stdout)
        self.assertEqual(b"D" * half, captured_combined.stderr)
        self.assertEqual(2, counting_lock.entries)


class StreamingJsonResponseTest(unittest.TestCase):
    def _make_handler(self, *, body_unread: bool = False):
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        handler.connection = ours
        handler.client_address = ("127.0.0.1", 0)
        handler.close_connection = False
        handler.status_sent = None
        handler.headers_sent = []
        handler.end_headers_called = 0
        handler.writes = []

        def send_response(status):
            handler.status_sent = status

        def send_header(key, value):
            handler.headers_sent.append((key, value))
            if key.lower() == "connection" and value.lower() == "close":
                handler.close_connection = True

        def end_headers():
            handler.end_headers_called += 1

        handler.send_response = send_response
        handler.send_header = send_header
        handler.end_headers = end_headers
        handler._body_unread = lambda: body_unread
        handler.wfile = mock.MagicMock()
        handler.wfile.write.side_effect = lambda data: handler.writes.append(bytes(data))
        return handler

    def test_json_clears_payload_and_streams_large_ascii_in_slices(self):
        for payload_chars in (_LARGE_OUTPUT_THRESHOLD_BYTES + 50_000, 3 * _JSON_STREAM_SLICE_CHARS + 12345):
            with self.subTest(payload_chars=payload_chars):
                handler = self._make_handler()
                expected_stdout = "x" * payload_chars
                payload = {
                    "status": "completed",
                    "exitCode": 0,
                    "stdout": expected_stdout,
                    "stderr": "",
                    "durationMs": 12,
                    "truncated": False,
                    "timedOut": False,
                }
                expected_copy = dict(payload)

                handler._json(HTTPStatus.OK, payload, clear_payload=True)

                self.assertEqual({}, payload)
                self.assertEqual(HTTPStatus.OK, handler.status_sent)
                self.assertEqual(1, handler.end_headers_called)
                self.assertGreater(len(handler.writes), 1)
                for chunk in handler.writes:
                    self.assertLessEqual(len(chunk), _JSON_STREAM_SLICE_CHARS)
                raw_body = b"".join(handler.writes)
                headers_dict = dict(handler.headers_sent)
                self.assertEqual(str(len(raw_body)), headers_dict["Content-Length"])
                self.assertEqual("application/json", headers_dict["Content-Type"])
                self.assertEqual(expected_copy, json.loads(raw_body.decode("utf-8")))

    def test_json_preserves_extra_headers_and_keepalive_end_headers(self):
        # Small payload with extra header (e.g. Retry-After):
        small_handler = self._make_handler(body_unread=False)
        small_handler._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "busy"}, headers={"Retry-After": "12"})
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, small_handler.status_sent)
        self.assertEqual(1, small_handler.end_headers_called)
        self.assertEqual("12", dict(small_handler.headers_sent)["Retry-After"])
        self.assertNotIn("Connection", dict(small_handler.headers_sent))

        # Large streamed payload with extra header and unread request body closes connection via _end_headers:
        large_handler = self._make_handler(body_unread=True)
        payload = {"status": "completed", "stdout": "y" * (_LARGE_OUTPUT_THRESHOLD_BYTES + 1024)}
        expected_copy = dict(payload)
        large_handler._json(HTTPStatus.OK, payload, headers={"Retry-After": "5"}, clear_payload=True)
        self.assertEqual({}, payload)
        self.assertEqual(1, large_handler.end_headers_called)
        headers_dict = dict(large_handler.headers_sent)
        self.assertEqual("5", headers_dict["Retry-After"])
        self.assertEqual("close", headers_dict["Connection"])
        self.assertTrue(large_handler.close_connection)
        raw_body = b"".join(large_handler.writes)
        self.assertEqual(str(len(raw_body)), headers_dict["Content-Length"])
        self.assertEqual(expected_copy, json.loads(raw_body.decode("utf-8")))

    def test_json_non_ascii_large_payload_streams_in_slices_with_exact_content_length(self):
        for marker in ("é\ufffd", "𐍈✓"):
            with self.subTest(marker=marker):
                handler = self._make_handler()
                expected_stdout = (marker * (_LARGE_OUTPUT_THRESHOLD_BYTES // 2)) + '"\n\\tail'
                payload = {
                    "status": "completed",
                    "exitCode": 0,
                    "stdout": expected_stdout,
                    "stderr": "",
                }
                expected_copy = dict(payload)

                handler._json(HTTPStatus.OK, payload, clear_payload=True)

                self.assertEqual({}, payload)
                self.assertGreater(len(handler.writes), 1)
                for chunk in handler.writes:
                    self.assertLessEqual(len(chunk), _JSON_STREAM_SLICE_CHARS)
                raw_body = b"".join(handler.writes)
                headers_dict = dict(handler.headers_sent)
                self.assertEqual(str(len(raw_body)), headers_dict["Content-Length"])
                self.assertEqual(expected_copy, json.loads(raw_body.decode("utf-8")))

    def test_json_combined_and_nested_large_payloads_acquire_lock_and_round_trip(self):
        half = (_LARGE_OUTPUT_THRESHOLD_BYTES // 2) + 8192
        counting_lock = _CountingLock(credential_proxy._LARGE_OUTPUT_LOCK)
        handler = self._make_handler()
        payload = {
            "status": "completed",
            "stdout": "s" * half,
            "stderr": "e" * half,
            "nested": [{"content": "n" * 1024, "ok": True, "Null": None}],
        }
        expected_copy = json.loads(json.dumps(payload))
        with mock.patch.object(credential_proxy, "_LARGE_OUTPUT_LOCK", counting_lock):
            handler._json(HTTPStatus.OK, payload, clear_payload=True)
        self.assertEqual(1, counting_lock.entries)
        self.assertEqual({}, payload)
        self.assertGreater(len(handler.writes), 1)
        raw_body = b"".join(handler.writes)
        self.assertEqual(expected_copy, json.loads(raw_body.decode("utf-8")))

    def test_json_mid_stream_oserror_marks_close_connection_and_logs_warning(self):
        handler = self._make_handler()
        payload = {
            "status": "completed",
            "stdout": "x" * (2 * _JSON_STREAM_SLICE_CHARS + 4096),
            "stderr": "",
        }
        calls = 0

        def fail_on_second_chunk(data):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise BrokenPipeError("peer closed")
            handler.writes.append(bytes(data))

        handler.wfile.write.side_effect = fail_on_second_chunk
        with self.assertLogs(credential_proxy.LOGGER, level=logging.WARNING) as logs:
            handler._json(HTTPStatus.OK, payload, clear_payload=True)
        self.assertTrue(handler.close_connection)
        self.assertEqual({}, payload)
        self.assertEqual(1, len(handler.writes))
        self.assertTrue(
            any("response not delivered status=200" in rec.getMessage() and "type=BrokenPipeError" in rec.getMessage() for rec in logs.records),
            [rec.getMessage() for rec in logs.records],
        )

    def test_do_post_exec_passes_clear_payload_only_to_default_json(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        stub.write_text("#!/bin/sh\nprintf 'pod-a\\n'\n", encoding="utf-8")
        stub.chmod(0o755)
        executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=temp.name,
            scoped_pool=None,
        )
        executor.executables["kubectl"] = str(stub)

        req_bytes = json.dumps({"requestId": "req-1", "argv": ["kubectl", "get", "pods"]}).encode("utf-8")

        def setup_exec_handler():
            h = self._make_handler()
            h.path = "/v1/exec"
            h.executor = executor
            h.metrics = ProxyMetrics()
            h.authenticator = credential_proxy.NullAuthenticator()
            h.policy = credential_proxy.Policy(rules=[], blocked_message="blocked")
            h.max_request_bytes = 65536
            h.enforce_read_only = False
            h.headers = {"Content-Length": str(len(req_bytes))}
            h.rfile = credential_proxy.io.BytesIO(req_bytes)
            return h

        # 1. With default _json, do_POST passes clear_payload=True.
        handler_default = setup_exec_handler()
        seen_clear_flags: list[bool] = []
        orig_encode = CredentialProxyHandler._encode_json_chunks

        def spy_encode(payload, *, clear_payload):
            seen_clear_flags.append(clear_payload)
            return orig_encode(payload, clear_payload=clear_payload)

        with (
            mock.patch.object(CredentialProxyHandler, "_payload_has_large_text", return_value=True),
            mock.patch.object(CredentialProxyHandler, "_encode_json_chunks", staticmethod(spy_encode)),
        ):
            handler_default.do_POST()
        self.assertEqual([True], seen_clear_flags)
        self.assertEqual(HTTPStatus.OK, handler_default.status_sent)
        parsed = json.loads(b"".join(handler_default.writes).decode("utf-8"))
        self.assertEqual("pod-a\n", parsed["stdout"])
        self.assertEqual(len("pod-a\n"), parsed["bytesWritten"])

        # 2. When _json is overridden on the instance with a 2-arg callable, do_POST
        # calls it without clear_payload keyword argument.
        handler_custom = setup_exec_handler()
        recorded: list[tuple[HTTPStatus, dict]] = []
        handler_custom._json = lambda status, body: recorded.append((status, dict(body)))
        handler_custom.do_POST()
        self.assertEqual(1, len(recorded))
        self.assertEqual(HTTPStatus.OK, recorded[0][0])
        self.assertEqual("pod-a\n", recorded[0][1]["stdout"])

    def test_payload_has_large_text_guards_cycles_allows_shared_refs_and_counts_utf8_bytes(self):
        cyclic_list: list = []
        cyclic_list.append(cyclic_list)
        with self.assertRaisesRegex(ValueError, "Circular reference"):
            CredentialProxyHandler._payload_has_large_text({"items": cyclic_list})

        cyclic_dict: dict = {}
        cyclic_dict["self"] = cyclic_dict
        with self.assertRaisesRegex(ValueError, "Circular reference"):
            CredentialProxyHandler._payload_has_large_text(cyclic_dict)

        # Even if a large string triggers chunked encoding before a cycle is reached,
        # _encode_json_chunks also rejects the circular reference.
        cyclic_with_large: dict = {"stdout": "a" * (_LARGE_OUTPUT_THRESHOLD_BYTES + 16)}
        cyclic_with_large["loop"] = cyclic_with_large
        with self.assertRaisesRegex(ValueError, "Circular reference"):
            CredentialProxyHandler._encode_json_chunks(cyclic_with_large, clear_payload=False)

        # Shared acyclic references are valid JSON and must not raise.
        shared = ["ok", "data"]
        self.assertFalse(CredentialProxyHandler._payload_has_large_text({"a": shared, "b": shared}))

        # Astral UTF-8 string (4 bytes/char in UTF-8 & UCS-4) triggers the byte threshold
        # even when codepoint count is below _LARGE_OUTPUT_THRESHOLD_BYTES.
        self.assertTrue(
            CredentialProxyHandler._payload_has_large_text({"stdout": "\U00010348" * 100_000})
        )

    def test_encode_json_chunks_handles_non_string_keys_heavy_escapes_del_and_clear_payload_false(self):
        heavy_escapes = ("\x00" * 80_000) + ('"\\' * 80_000) + ("\x7f" * 1024) + "café𐍈"
        payload = {
            2: "int-key",
            True: "bool-key",
            None: "none-key",
            "stdout": heavy_escapes,
            "nested": (1, False, None, {"k": "v"}),
        }
        expected_bytes = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8", errors="replace")

        # clear_payload=False preserves caller dict while matching json.dumps byte-for-byte.
        chunks_keep, total_keep = CredentialProxyHandler._encode_json_chunks(
            payload, clear_payload=False
        )
        self.assertEqual(5, len(payload))
        self.assertEqual(len(expected_bytes), total_keep)
        for chunk in chunks_keep:
            self.assertGreater(len(chunk), 0)
            self.assertLessEqual(len(chunk), _JSON_STREAM_SLICE_CHARS)
        self.assertEqual(expected_bytes, b"".join(chunks_keep))

        # clear_payload=True clears top-level dict and produces identical bytes.
        chunks_clear, total_clear = CredentialProxyHandler._encode_json_chunks(
            payload, clear_payload=True
        )
        self.assertEqual({}, payload)
        self.assertEqual(len(expected_bytes), total_clear)
        for chunk in chunks_clear:
            self.assertGreater(len(chunk), 0)
            self.assertLessEqual(len(chunk), _JSON_STREAM_SLICE_CHARS)
        self.assertEqual(expected_bytes, b"".join(chunks_clear))

    def test_do_post_records_scope_policy_before_clearing_large_response(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        large_out = "p" * (_LARGE_OUTPUT_THRESHOLD_BYTES + 16_384)
        stub = stub_dir / "gcloud"
        stub.write_text(
            f"#!/usr/bin/env python3\nimport sys\nsys.stdout.write({large_out!r})\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        executor = CommandExecutor(
            timeout_seconds=10,
            max_output_bytes=len(large_out) + 4096,
            state_dir=temp.name,
            scoped_pool=None,
        )
        executor.executables["gcloud"] = str(stub)

        req_bytes = json.dumps({"requestId": "scope-1", "argv": ["gcloud", "projects", "list", "--format=json"]}).encode("utf-8")
        handler = self._make_handler()
        handler.path = "/v1/exec"
        handler.executor = executor
        handler.metrics = ProxyMetrics()
        handler.authenticator = mock.MagicMock()
        handler.authenticator.authenticate.return_value = credential_proxy.Principal(
            workload="system:serviceaccount:ns:scope",
            role=credential_proxy.CALLER_ROLE_SCOPE,
        )
        handler.policy = credential_proxy.Policy(rules=[], blocked_message="blocked")
        handler.max_request_bytes = 65536
        handler.enforce_read_only = False
        handler.headers = {"Content-Length": str(len(req_bytes))}
        handler.rfile = credential_proxy.io.BytesIO(req_bytes)

        recorded_stdouts: list[str] = []

        def fake_record(admitted, stdout_text, **kwargs):
            recorded_stdouts.append(stdout_text)

        with (
            mock.patch.object(credential_proxy.scope_policy, "check", return_value=(None, object())),
            mock.patch.object(credential_proxy.scope_policy, "record", side_effect=fake_record),
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.OK, handler.status_sent)
        self.assertEqual([large_out], recorded_stdouts)
        parsed = json.loads(b"".join(handler.writes).decode("utf-8"))
        self.assertEqual(large_out, parsed["stdout"])

    def test_live_http11_keepalive_reuses_connection_after_large_streamed_response(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        large_text = ("café𐍈" * 70_000) + "\n"
        payload_file = Path(temp.name) / "out.txt"
        payload_file.write_text(large_text, encoding="utf-8")
        stub = stub_dir / "kubectl"
        stub.write_text(f"#!/bin/sh\nexec cat '{payload_file}'\n", encoding="utf-8")
        stub.chmod(0o755)

        executor = CommandExecutor(
            timeout_seconds=15,
            max_output_bytes=len(large_text.encode("utf-8")) + 4096,
            state_dir=temp.name,
            scoped_pool=None,
        )
        executor.executables["kubectl"] = str(stub)

        class LiveHandler(CredentialProxyHandler):
            pass

        LiveHandler.policy = credential_proxy.Policy(rules=[], blocked_message="blocked")
        LiveHandler.executor = executor
        LiveHandler.authenticator = credential_proxy.NullAuthenticator()
        LiveHandler.metrics = ProxyMetrics()
        LiveHandler.max_request_bytes = 65536
        LiveHandler.enforce_read_only = False
        LiveHandler.log_message = lambda self, fmt, *args: None

        server = credential_proxy.ThreadingTCPHTTPServer(("127.0.0.1", 0), LiveHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
        self.addCleanup(conn.close)
        req_body = json.dumps({"requestId": "ka-1", "argv": ["kubectl", "get", "pods"]}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(req_body))}

        conn.request("POST", "/v1/exec", body=req_body, headers=headers)
        first_resp = conn.getresponse()
        first_body = json.loads(first_resp.read().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, first_resp.status)
        self.assertEqual(large_text, first_body["stdout"])
        first_sock = conn.sock
        self.assertIsNotNone(first_sock)

        # Second request on the exact same HTTP/1.1 keep-alive socket must succeed.
        conn.request("POST", "/v1/exec", body=req_body, headers=headers)
        second_resp = conn.getresponse()
        second_body = json.loads(second_resp.read().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, second_resp.status)
        self.assertEqual(large_text, second_body["stdout"])
        self.assertIs(first_sock, conn.sock)


class ConcurrentLargeOutputExecuteTest(unittest.TestCase):
    def test_concurrent_large_output_executes_serialize_under_lock_and_clear_bytearrays(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        size = _LARGE_OUTPUT_THRESHOLD_BYTES + 32768
        stub = stub_dir / "kubectl"
        stub.write_text(
            f"#!/usr/bin/env python3\nimport sys\nsys.stdout.write('P' * {size})\nsys.stderr.write('E' * {size})\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        executor = CommandExecutor(
            timeout_seconds=15,
            max_output_bytes=size + 65536,
            state_dir=temp.name,
            scoped_pool=None,
            max_concurrent_commands=4,
        )
        executor.executables["kubectl"] = str(stub)

        real_bounded_text = credential_proxy._bounded_text
        state_lock = threading.Lock()
        active_in_bounded_text = 0
        max_concurrent_in_bounded_text = 0
        seen_bytearrays: list[bytearray] = []

        def tracking_bounded_text(raw, limit):
            nonlocal active_in_bounded_text, max_concurrent_in_bounded_text
            with state_lock:
                active_in_bounded_text += 1
                if active_in_bounded_text > max_concurrent_in_bounded_text:
                    max_concurrent_in_bounded_text = active_in_bounded_text
                if isinstance(raw, bytearray):
                    seen_bytearrays.append(raw)
            try:
                time.sleep(0.01)
                return real_bounded_text(raw, limit)
            finally:
                with state_lock:
                    active_in_bounded_text -= 1

        with mock.patch.object(credential_proxy, "_bounded_text", tracking_bounded_text):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(executor.execute, ["kubectl", "get", "pods"]) for _ in range(4)]
                results = [f.result() for f in futures]

        self.assertEqual(1, max_concurrent_in_bounded_text)
        self.assertEqual(8, len(seen_bytearrays))
        for buf in seen_bytearrays:
            self.assertEqual(0, len(buf))
        for res in results:
            self.assertEqual(0, res.exit_code)
            self.assertEqual("P" * size, res.stdout)
            self.assertEqual("E" * size, res.stderr)
            self.assertEqual(size, res.stdout_bytes)
            self.assertFalse(res.truncated)

    def test_execute_preserves_stdout_bytes_for_large_cut_redecode_after_clearing_bytearray(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        limit = _LARGE_OUTPUT_THRESHOLD_BYTES + 100
        # Invalid UTF-8 bytes expand 3x to U+FFFD, forcing stdout_cut=True while
        # raw bytearray exceeds _LARGE_OUTPUT_THRESHOLD_BYTES and gets cleared in place.
        stub = stub_dir / "kubectl"
        stub.write_text(
            f"#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(b'\\xff' * {limit})\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        executor = CommandExecutor(
            timeout_seconds=15,
            max_output_bytes=limit,
            state_dir=temp.name,
            scoped_pool=None,
        )
        executor.executables["kubectl"] = str(stub)

        result = executor.execute(["kubectl", "get", "pods"])
        self.assertTrue(result.truncated)
        self.assertTrue(result.stdout_truncated)
        self.assertLessEqual(result.stdout_bytes, limit)
        self.assertEqual(len(result.stdout.encode("utf-8")), result.stdout_bytes)
        self.assertGreater(result.stdout_bytes, _LARGE_OUTPUT_THRESHOLD_BYTES)

    def test_execute_decodes_and_appends_drained_note_when_stopped_by_drain(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=temp.name,
            scoped_pool=None,
        )
        executor.executables["kubectl"] = str(stub)

        drained_out = bytearray(b"partial-stdout\n")
        drained_err = bytearray(b"partial-stderr\n")
        drained_captured = credential_proxy._CapturedOutput(
            stdout=drained_out,
            stderr=drained_err,
            truncated=False,
            timed_out=False,
            abandoned=True,
            peak_rss_bytes=1024,
            stopped=True,
        )

        def drained_capture_cleanup(process, *args, **kwargs):
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            process.wait()
            # Simulate drain starting while the command was running.
            executor.end_commands.set()
            return drained_captured

        with mock.patch.object(credential_proxy, "_capture_output", side_effect=drained_capture_cleanup):
            result = executor.execute(["kubectl", "get", "pods"])
        self.assertFalse(result.abandoned)
        self.assertEqual(credential_proxy.DRAINED_COMMAND_EXIT_CODE, result.exit_code)
        self.assertEqual("partial-stdout\n", result.stdout)
        self.assertEqual(len("partial-stdout\n"), result.stdout_bytes)
        self.assertEqual("partial-stderr\n" + credential_proxy.DRAINED_COMMAND_NOTE, result.stderr)
        self.assertEqual(0, len(drained_out))
        self.assertEqual(0, len(drained_err))

    def test_execute_preserves_bytes_in_reused_mock_captured_output_and_skips_decode_when_abandoned(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=temp.name,
            scoped_pool=None,
        )
        executor.executables["kubectl"] = str(stub)

        fake_captured = credential_proxy._CapturedOutput(
            stdout=b"reusable-stdout\n",
            stderr=b"reusable-stderr\n",
            truncated=False,
            timed_out=False,
            abandoned=False,
            peak_rss_bytes=1024,
        )

        def fake_capture_cleanup(process, *args, **kwargs):
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            process.wait()
            return fake_captured

        with mock.patch.object(credential_proxy, "_capture_output", side_effect=fake_capture_cleanup):
            first = executor.execute(["kubectl", "get", "pods"])
            second = executor.execute(["kubectl", "get", "pods"])
        self.assertEqual("reusable-stdout\n", first.stdout)
        self.assertEqual("reusable-stderr\n", first.stderr)
        self.assertEqual(len(b"reusable-stdout\n"), first.stdout_bytes)
        self.assertEqual("reusable-stdout\n", second.stdout)
        self.assertEqual("reusable-stderr\n", second.stderr)
        self.assertEqual(len(b"reusable-stdout\n"), second.stdout_bytes)

        abandoned_out = bytearray(b"X" * (_LARGE_OUTPUT_THRESHOLD_BYTES + 1024))
        abandoned_err = bytearray(b"Y" * (_LARGE_OUTPUT_THRESHOLD_BYTES + 1024))
        abandoned_captured = credential_proxy._CapturedOutput(
            stdout=abandoned_out,
            stderr=abandoned_err,
            truncated=False,
            timed_out=False,
            abandoned=True,
            peak_rss_bytes=1024,
        )

        def abandoned_capture_cleanup(process, *args, **kwargs):
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            process.wait()
            return abandoned_captured

        with (
            mock.patch.object(credential_proxy, "_capture_output", side_effect=abandoned_capture_cleanup),
            mock.patch.object(credential_proxy, "_bounded_text") as bounded_spy,
        ):
            abandoned_res = executor.execute(["kubectl", "get", "pods"])
        self.assertTrue(abandoned_res.abandoned)
        self.assertEqual("", abandoned_res.stdout)
        self.assertEqual("", abandoned_res.stderr)
        self.assertEqual(0, len(abandoned_out))
        self.assertEqual(0, len(abandoned_err))
        bounded_spy.assert_not_called()

    def test_execute_stderr_only_truncation_preserves_stdout_truncated_false(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        stub_dir = Path(temp.name) / "bin"
        stub_dir.mkdir()
        limit = 4096
        stub = stub_dir / "kubectl"
        stub.write_text(
            f"#!/usr/bin/env python3\nimport sys\nsys.stdout.write('ok\\n')\nsys.stderr.write('E' * {limit * 4})\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        executor = CommandExecutor(
            timeout_seconds=10,
            max_output_bytes=limit,
            state_dir=temp.name,
            scoped_pool=None,
        )
        executor.executables["kubectl"] = str(stub)

        result = executor.execute(["kubectl", "get", "pods"])
        self.assertEqual("ok\n", result.stdout)
        self.assertEqual(len("ok\n"), result.stdout_bytes)
        self.assertEqual("E" * limit, result.stderr)
        self.assertTrue(result.truncated)
        self.assertFalse(result.stdout_truncated)


if __name__ == "__main__":
    unittest.main()

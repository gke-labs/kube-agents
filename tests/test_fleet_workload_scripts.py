# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The two workload scripts that the seeded fleet plants on seeded-a for the
cost audit (bench/tf/fleet/defects-a.tf): `cycle.py` of the
long-name-replaced-pod role and `loop.py` of the cpu-burst-and-flat role.

Each test runs the shipped script, read out of its ConfigMap block, never a
copy of it. No test waits or calls an API: the clock, the sleep, the busy
period and the HTTP call are replaced, so a test of a loop ends after a fixed
number of steps. The tests hold the schedule, the order of the steps, the
delete call, and the numbers that the cost audit's check 3.1 reads.
"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import re
import signal
import tempfile
import textwrap
import types
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
_STACK = _REPO / "bench" / "tf" / "fleet" / "defects-a.tf"
_COLLECTOR = _REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "fleet_waste.py"


def _stack() -> str:
    return _STACK.read_text(encoding="utf-8")


def _script_text(key: str) -> str:
    """The text of the script that the stack ships under `key` in a ConfigMap.
    OpenTofu's `<<-PY` removes the common indent of the block, as dedent does."""
    match = re.search(rf'^\s*"{re.escape(key)}" = <<-PY\n(.*?)^\s*PY$', _stack(), re.S | re.M)
    assert match, f"no ConfigMap block ships {key!r} in {_STACK}"
    return textwrap.dedent(match.group(1))


def _script(key: str) -> types.ModuleType:
    """The shipped script as a module. Its `__main__` block does not run."""
    module = types.ModuleType(key.removesuffix(".py"))
    exec(compile(_script_text(key), f"{_STACK}:{key}", "exec"), module.__dict__)  # noqa: S102 - the shipped script
    return module


def _resource_block(kind: str, name: str) -> str:
    """The text of one resource block of the stack."""
    match = re.search(rf'^resource "{kind}" "{name}" \{{\n(.*?)^\}}$', _stack(), re.S | re.M)
    assert match, f"no resource {kind}.{name} in {_STACK}"
    return match.group(1)


class _StopLoop(Exception):
    """Ends a script loop that has no end of its own."""


class _SpinClock:
    """A stand-in for the `time` module of a script. Each read of the clock
    moves it by one second, as a busy loop does. `sleep` moves it by the sleep
    and records (the time that the script read last, the sleep). After `steps`
    reads the clock raises _StopLoop."""

    def __init__(self, start: float, steps: int):
        self.now = start
        self.steps = steps
        self.reads = []
        self.sleeps = []

    def time(self) -> float:
        if len(self.reads) >= self.steps:
            raise _StopLoop
        self.reads.append(self.now)
        self.now += 1
        return self.reads[-1]

    monotonic = time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append((self.reads[-1] if self.reads else self.now, seconds))
        self.now += seconds


def _collector_constant(name: str) -> float:
    match = re.search(rf"^{name} = ([0-9.]+)$", _COLLECTOR.read_text(encoding="utf-8"), re.M)
    assert match, f"{name} is not a plain number in {_COLLECTOR}"
    return float(match.group(1))


def _locals_value(name: str) -> str:
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"$', _stack(), re.M)
    assert match, f"no local {name!r} in {_STACK}"
    return match.group(1)


def _long_name() -> str:
    """The Deployment name of the long-name-replaced-pod role, from the stack."""
    match = re.search(r'^\s*long_name_workload = \{\s*name = "([^"]+)"\s*\}', _stack(), re.M)
    assert match, f"no local long_name_workload in {_STACK}"
    return match.group(1)


def _millicores(value: str) -> float:
    return float(value.removesuffix("m")) if value.endswith("m") else float(value) * 1000


def _mebibytes(value: str) -> float:
    assert value.endswith("Mi"), value
    return float(value.removesuffix("Mi"))


class LongNameCycleScriptTest(unittest.TestCase):
    """cycle.py: idle until one fixed UTC hour, then busy for some minutes,
    then the pod deletes itself. The pod of the day before is then a replaced
    pod with a peak above check 3.1's bar, and the live pod is idle."""

    @classmethod
    def setUpClass(cls):
        cls.cycle = _script("cycle.py")

    def test_the_next_busy_period_is_the_next_start_of_the_hour_after_now(self):
        hour = self.cycle.BURST_HOUR_UTC
        day = datetime(2026, 11, 24, tzinfo=timezone.utc)
        start = day.replace(hour=hour)
        for now, expected in (
            (start - timedelta(seconds=1), start),
            (start - timedelta(hours=5), start),
            # A pod that starts at the hour or inside the busy minutes waits
            # for the next day: the pod that replaces a busy pod must be idle.
            (start, start + timedelta(days=1)),
            (start + timedelta(seconds=1), start + timedelta(days=1)),
            (start + timedelta(seconds=self.cycle.BURST_SECONDS), start + timedelta(days=1)),
            (start + timedelta(hours=11), start + timedelta(days=1)),
        ):
            with self.subTest(now=now.isoformat()):
                self.assertEqual(self.cycle.next_burst(now), expected)

    def test_the_busy_hour_is_after_the_nightly_window_and_ends_inside_its_day(self):
        # The nightly lanes start at 00:00 UTC with a limit of 840 minutes, so
        # they can run until 14:00 UTC. The Deployment has no verdict from the
        # busy hour to about 71 minutes after it; that window must not reach
        # the start of the next nightly.
        nightly_end_hour = 14
        no_verdict_hours = 2
        self.assertGreaterEqual(self.cycle.BURST_HOUR_UTC, nightly_end_hour)
        self.assertLessEqual(self.cycle.BURST_HOUR_UTC + no_verdict_hours, 24)
        self.assertLessEqual(self.cycle.BURST_SECONDS + 3600, no_verdict_hours * 3600)

    def test_the_busy_period_fills_one_whole_bucket_of_the_cost_audits_read(self):
        # Check 3.1 reads the CPU rate in buckets. A busy period of two bucket
        # lengths or more holds one whole bucket at each position of its start.
        # The audit's CPU bucket is CPU_USAGE_ALIGNMENT_S; an audit of an
        # earlier release reads the CPU in the longer USAGE_ALIGNMENT_S, and
        # the fixture must hold for both, so the longer one decides.
        bucket_seconds = max(_collector_constant("CPU_USAGE_ALIGNMENT_S"), _collector_constant("USAGE_ALIGNMENT_S"))
        self.assertGreaterEqual(self.cycle.BURST_SECONDS, 2 * bucket_seconds)

    def test_the_busy_memory_is_above_the_bar_and_under_the_limit(self):
        bar = _collector_constant("IDLE_WORKLOAD_UTILISATION")
        request_bytes = 128 * 1024 * 1024
        limit_bytes = 256 * 1024 * 1024
        self.assertEqual(_locals_value("long_name_memory_request"), "128Mi")
        self.assertEqual(_locals_value("long_name_memory_limit"), "256Mi")
        self.assertGreater(self.cycle.BUSY_MEMORY_BYTES, 2 * bar * request_bytes)
        self.assertLess(self.cycle.BUSY_MEMORY_BYTES, limit_bytes / 2)

    def test_the_busy_cpu_is_above_the_bar(self):
        # The busy pod uses the CPU up to its limit. That peak must be above
        # the bar, as a part of the request, with a margin of two times.
        bar = _collector_constant("IDLE_WORKLOAD_UTILISATION")
        request = _millicores(_locals_value("long_name_cpu_request"))
        limit = _millicores(_locals_value("long_name_cpu_limit"))
        self.assertGreater(limit, 2 * bar * request)

    def test_the_busy_period_writes_each_page_and_spins_for_its_seconds(self):
        writes = []

        class Recorded(bytearray):
            def __setitem__(self, offset, value):
                writes.append(offset)
                super().__setitem__(offset, value)

        seconds = 7
        clock = _SpinClock(start=0, steps=1000)
        with mock.patch.object(self.cycle, "bytearray", Recorded, create=True), mock.patch.object(self.cycle, "time", clock):
            held = self.cycle.busy(seconds)
        # Memory that no write touched is not resident: the audit reads the
        # memory that the pod uses, so each page gets one write.
        self.assertEqual(held, self.cycle.BUSY_MEMORY_BYTES)
        self.assertEqual(writes, list(range(0, self.cycle.BUSY_MEMORY_BYTES, self.cycle.PAGE_BYTES)))
        # A loop on the clock, with no sleep: the pod uses the CPU.
        self.assertEqual(clock.sleeps, [])
        self.assertGreaterEqual(clock.now, seconds)
        self.assertLess(clock.now, seconds + 3)

    def test_the_deployment_name_is_long_enough_that_kubernetes_cuts_its_pod_names(self):
        # A pod name is `<replicaset name>-` cut to 58 characters, plus 5. A
        # ReplicaSet name is the Deployment name, a hyphen and a hash, so a
        # Deployment name of 47 characters or more is cut, and one of 58 or
        # more loses the hyphen before the hash too.
        name = _long_name()
        self.assertEqual(len(name), 58, name)
        self.assertIsNone(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", name))
        # The fleet has the Deployment `ledger-writer`, which cases name by the
        # word "ledger"; this name must not match those checks.
        self.assertNotIn("ledger", name)

    def test_the_pod_path_names_the_namespace_and_the_pod(self):
        self.assertEqual(
            self.cycle.pod_path("seeded-long-name", "a-pod-name-0abcd"),
            "/api/v1/namespaces/seeded-long-name/pods/a-pod-name-0abcd",
        )

    # ---- the delete call

    def _delete(self, answer, environment=None):
        """Run delete_own_pod with made-up account files and a replaced HTTP
        call. `answer` is the HTTP status, or an exception that the call
        raises. Returns (result, the request, the keyword arguments)."""
        calls = []

        def urlopen(request, **keywords):
            calls.append((request, keywords))
            if isinstance(answer, BaseException):
                raise answer
            response = mock.MagicMock()
            response.__enter__.return_value.status = answer
            return response

        with tempfile.TemporaryDirectory() as folder:
            token, namespace = pathlib.Path(folder) / "token", pathlib.Path(folder) / "namespace"
            token.write_text("a-made-up-value\n", encoding="utf-8")
            namespace.write_text("seeded-long-name\n", encoding="utf-8")
            variables = {"KUBERNETES_SERVICE_HOST": "10.20.0.1", "KUBERNETES_SERVICE_PORT": "8443", "POD_NAME": "a-long-pod-nameabcde"}
            variables.update(environment or {})
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.cycle, "TOKEN_PATH", str(token)))
                stack.enter_context(mock.patch.object(self.cycle, "NAMESPACE_PATH", str(namespace)))
                stack.enter_context(mock.patch.dict(os.environ, variables))
                stack.enter_context(mock.patch("urllib.request.urlopen", urlopen))
                context = stack.enter_context(mock.patch("ssl.create_default_context"))
                result = self.cycle.delete_own_pod()
        self.assertEqual(len(calls), 1)
        self.assertEqual(context.call_args.kwargs, {"cafile": self.cycle.CA_PATH})
        self.assertIs(calls[0][1]["context"], context.return_value)
        return result, calls[0][0], calls[0][1]

    def test_the_delete_call_is_one_delete_of_this_pod_with_the_accounts_bearer(self):
        result, request, keywords = self._delete(200)
        path = "/api/v1/namespaces/seeded-long-name/pods/a-long-pod-nameabcde"
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(request.full_url, "https://10.20.0.1:8443" + path)
        self.assertEqual(request.get_header("Authorization"), "Bearer a-made-up-value")
        self.assertEqual(request.get_header("User-agent"), self.cycle.USER_AGENT)
        self.assertIsNone(request.data)
        self.assertEqual(keywords["timeout"], self.cycle.REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(result, (200, path))

    def test_the_delete_call_uses_the_default_port_when_the_environment_has_none(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("KUBERNETES_SERVICE_PORT", None)
            calls = []

            def urlopen(request, **keywords):
                calls.append(request)
                raise OSError("no network in a test")

            with tempfile.TemporaryDirectory() as folder:
                for name in ("token", "namespace"):
                    (pathlib.Path(folder) / name).write_text("made-up\n", encoding="utf-8")
                with (
                    mock.patch.object(self.cycle, "TOKEN_PATH", folder + "/token"),
                    mock.patch.object(self.cycle, "NAMESPACE_PATH", folder + "/namespace"),
                    mock.patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.20.0.1", "POD_NAME": "a-pod"}),
                    mock.patch("urllib.request.urlopen", urlopen),
                    mock.patch("ssl.create_default_context"),
                ):
                    self.cycle.delete_own_pod()
        self.assertEqual(calls[0].full_url, "https://10.20.0.1:443/api/v1/namespaces/made-up/pods/a-pod")

    def test_a_refused_delete_and_a_call_with_no_answer_are_not_a_success(self):
        refused = urllib.error.HTTPError("https://made-up", 403, "Forbidden", None, None)
        for answer, status in ((refused, 403), (OSError("the API server is away"), self.cycle.HTTP_NO_ANSWER), (TimeoutError("no answer"), self.cycle.HTTP_NO_ANSWER)):
            with self.subTest(answer=repr(answer)):
                result, _, _ = self._delete(answer)
                self.assertEqual(result[0], status)
                self.assertNotIn(status, (self.cycle.HTTP_OK, self.cycle.HTTP_ACCEPTED))

    # ---- the order of the steps

    def _main(self, started: datetime, delete_answers):
        """Run main() with a made-up clock. Returns (exit code, the steps, the
        text that main printed). A step is ("sleep", seconds), ("busy",
        seconds) or ("delete",)."""
        steps = []
        now = [started]

        class Clock:
            @staticmethod
            def sleep(seconds):
                steps.append(("sleep", seconds))
                now[0] += timedelta(seconds=seconds)

        class Calendar:
            @staticmethod
            def now(zone):
                return now[0]

        def busy(seconds):
            steps.append(("busy", seconds))
            now[0] += timedelta(seconds=seconds)

        answers = list(delete_answers)

        def delete_own_pod():
            steps.append(("delete",))
            return answers.pop(0), "/a/made-up/path"

        printed = io.StringIO()
        with (
            mock.patch.object(self.cycle, "time", Clock),
            mock.patch.object(self.cycle, "datetime", Calendar),
            mock.patch.object(self.cycle, "busy", busy),
            mock.patch.object(self.cycle, "delete_own_pod", delete_own_pod),
            mock.patch.object(self.cycle, "signal") as signals,
            contextlib.redirect_stdout(printed),
        ):
            signals.SIGTERM = signal.SIGTERM
            code = self.cycle.main()
        self.handler_calls = signals.signal.call_args_list
        return code, steps, printed.getvalue()

    def test_main_is_idle_until_the_hour_then_busy_then_deletes_the_pod(self):
        cycle = self.cycle
        start = datetime(2026, 11, 24, cycle.BURST_HOUR_UTC, tzinfo=timezone.utc)
        code, steps, printed = self._main(start - timedelta(minutes=10, seconds=7), [cycle.HTTP_OK])
        idle = steps[: steps.index(("busy", cycle.BURST_SECONDS))]
        self.assertTrue(idle and all(step[0] == "sleep" and 0 < step[1] <= cycle.IDLE_STEP_SECONDS for step in idle), idle)
        self.assertEqual(sum(step[1] for step in idle), 607)
        self.assertEqual(steps[len(idle) :], [("busy", cycle.BURST_SECONDS), ("delete",), ("sleep", cycle.DELETE_WAIT_SECONDS)])
        self.assertIn("idle until " + start.isoformat(), printed)
        self.assertIn("deleted /a/made-up/path; this pod ends now", printed)
        # The pod must be gone by now. If it is still there, the script says
        # so and ends with 1, and the container starts again.
        self.assertEqual(code, 1)
        self.assertIn("BROKEN: this pod is still here", printed)

    def test_main_tries_the_delete_again_and_stops_at_the_first_success(self):
        cycle = self.cycle
        start = datetime(2026, 11, 24, cycle.BURST_HOUR_UTC, tzinfo=timezone.utc)
        _, steps, printed = self._main(start - timedelta(seconds=5), [cycle.HTTP_NO_ANSWER, 503, cycle.HTTP_ACCEPTED])
        after_busy = steps[steps.index(("busy", cycle.BURST_SECONDS)) + 1 :]
        retry = ("sleep", cycle.DELETE_RETRY_SECONDS)
        self.assertEqual(after_busy, [("delete",), retry, ("delete",), retry, ("delete",), ("sleep", cycle.DELETE_WAIT_SECONDS)])
        self.assertIn("returned 503 (try 2 of", printed)

    def test_a_delete_that_fails_on_each_try_is_not_a_success_and_ends_with_1(self):
        cycle = self.cycle
        start = datetime(2026, 11, 24, cycle.BURST_HOUR_UTC, tzinfo=timezone.utc)
        code, steps, printed = self._main(start - timedelta(seconds=5), [403] * cycle.DELETE_ATTEMPTS)
        self.assertEqual(code, 1)
        self.assertEqual(steps.count(("delete",)), cycle.DELETE_ATTEMPTS)
        self.assertEqual(steps.count(("sleep", cycle.DELETE_RETRY_SECONDS)), cycle.DELETE_ATTEMPTS - 1)
        self.assertNotIn(("sleep", cycle.DELETE_WAIT_SECONDS), steps)
        self.assertIn("BROKEN: each DELETE call failed; this pod stays", printed)
        self.assertNotIn("this pod ends now", printed)
        # The tries cover an API server that is away for some minutes.
        self.assertGreaterEqual((cycle.DELETE_ATTEMPTS - 1) * cycle.DELETE_RETRY_SECONDS, 300)

    def test_main_ends_with_0_on_a_term(self):
        # The script is process 1 of its container: with no handler a TERM
        # does nothing, and the pod ends only after its grace period.
        cycle = self.cycle
        start = datetime(2026, 11, 24, cycle.BURST_HOUR_UTC, tzinfo=timezone.utc)
        self._main(start - timedelta(seconds=5), [cycle.HTTP_OK])
        self.assertEqual(len(self.handler_calls), 1)
        number, handler = self.handler_calls[0].args
        self.assertEqual(number, signal.SIGTERM)
        with self.assertRaises(SystemExit) as stopped:
            handler(signal.SIGTERM, None)
        self.assertEqual(stopped.exception.code, 0)

    def test_the_container_runs_the_script_that_the_configmap_ships(self):
        block = _resource_block("kubernetes_deployment_v1", "long_name_replaced_pod")
        self.assertIn('command = ["python3", "/app/cycle.py"]', block)
        self.assertIn('mount_path = "/app"', block)
        self.assertIn("name = kubernetes_config_map_v1.long_name_cycle.metadata[0].name", block)
        _script_text("cycle.py")


class CpuCycleScriptTest(unittest.TestCase):
    """loop.py: `flat` uses the CPU all the time; `burst` uses it for 45
    seconds at the start of each 10 minutes of the clock."""

    @classmethod
    def setUpClass(cls):
        cls.loop = _script("loop.py")

    def test_the_busy_seconds_are_the_first_seconds_of_each_cycle(self):
        cycle, busy = self.loop.CYCLE_SECONDS, self.loop.BURST_SECONDS
        self.assertEqual((cycle, busy), (600, 45))
        for second, expected in ((0, True), (busy - 1, True), (busy, False), (cycle - 1, False), (cycle, True), (7 * cycle + busy - 0.5, True), (7 * cycle + busy, False)):
            with self.subTest(second=second):
                self.assertEqual(self.loop.in_burst(second), expected)

    def test_the_idle_wait_ends_at_the_next_cycle(self):
        cycle = self.loop.CYCLE_SECONDS
        self.assertEqual(self.loop.seconds_to_next_cycle(cycle - 10), 10)
        self.assertEqual(self.loop.seconds_to_next_cycle(3 * cycle + 45), cycle - 45)

    def test_the_burst_reads_under_the_bar_in_the_long_bucket_and_above_it_in_the_audits_cpu_bucket(self):
        # The rate of a bucket is limit x busy seconds / bucket seconds. In
        # the long bucket (USAGE_ALIGNMENT_S: the memory read's bucket, and the
        # CPU bucket of an audit of an earlier release) the burst must read at
        # most 15% of the request (a margin under the 20% bar). In the audit's
        # CPU bucket (CPU_USAGE_ALIGNMENT_S) it must read 30% or more, also
        # when the burst falls on two such buckets. So a longer CPU bucket
        # that puts the burst under the bar again fails here, with no run.
        long_bucket_seconds = _collector_constant("USAGE_ALIGNMENT_S")
        bar = _collector_constant("IDLE_WORKLOAD_UTILISATION")
        request = _millicores(_locals_value("cpu_burst_request"))
        limit = _millicores(_locals_value("cpu_burst_limit"))
        busy = self.loop.BURST_SECONDS
        self.assertLessEqual(limit * busy / long_bucket_seconds, 0.15 * request)
        self.assertLess(0.15, bar)
        cpu_bucket_seconds = _collector_constant("CPU_USAGE_ALIGNMENT_S")
        self.assertGreaterEqual(limit * (busy / 2) / cpu_bucket_seconds, 0.30 * request)

    def test_each_cpu_request_is_on_or_above_the_floor_of_the_check(self):
        floor_millicores = _collector_constant("OVERREQUEST_FLOOR_VCPU") * 1000
        for name in ("cpu_burst_request", "cpu_flat_request"):
            with self.subTest(name=name):
                self.assertGreaterEqual(_millicores(_locals_value(name)), floor_millicores)

    def test_the_memory_request_is_under_the_floor_of_the_check_and_under_the_limit(self):
        # Under the memory floor, the check reads the Deployments on CPU only.
        # A limit above the request keeps each pod out of the Guaranteed class.
        floor_mebibytes = _collector_constant("OVERREQUEST_FLOOR_GIB") * 1024
        request = _mebibytes(_locals_value("cpu_cycle_memory_request"))
        self.assertLess(request, floor_mebibytes)
        self.assertGreater(_mebibytes(_locals_value("cpu_cycle_memory_limit")), request)

    def test_an_unknown_mode_is_refused(self):
        self.assertEqual(self.loop.MODES, ("flat", "burst"))
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            self.loop.run("sometimes")

    def _run(self, mode: str, start: float, steps: int) -> _SpinClock:
        """Run the loop with a made-up clock for `steps` reads of the clock."""
        clock = _SpinClock(start=start, steps=steps)
        with mock.patch.object(self.loop, "time", clock), self.assertRaises(_StopLoop):
            self.loop.run(mode)
        return clock

    def test_flat_never_sleeps(self):
        clock = self._run("flat", start=0, steps=2000)
        self.assertEqual(clock.sleeps, [])

    def test_burst_spins_in_its_seconds_and_sleeps_for_the_rest_of_each_cycle(self):
        cycle, busy = self.loop.CYCLE_SECONDS, self.loop.BURST_SECONDS
        clock = self._run("burst", start=3 * cycle, steps=4000)
        self.assertTrue(clock.sleeps)
        # No sleep starts inside the busy seconds, and no sleep passes the
        # start of the next cycle.
        for at, seconds in clock.sleeps:
            self.assertGreaterEqual(at % cycle, busy, (at, seconds))
            self.assertLessEqual(at % cycle + seconds, cycle, (at, seconds))
            self.assertGreater(seconds, 0)
        # In the busy seconds of each of the first three cycles the loop reads
        # the clock each second and never sleeps: it uses the CPU.
        slept = sum(seconds for _, seconds in clock.sleeps)
        self.assertGreaterEqual(clock.now, 6 * cycle)
        for number in (3, 4, 5):
            in_busy = [at for at in clock.reads if number * cycle <= at < number * cycle + busy]
            self.assertGreaterEqual(len(in_busy), busy - 1, number)
        # Outside the busy seconds the loop sleeps and does not spin: the
        # sleeps cover the idle part of each whole cycle.
        self.assertGreaterEqual(slept, 3 * (cycle - busy) - 3 * self.loop.IDLE_STEP_SECONDS)

    def test_the_script_ends_with_0_on_a_term(self):
        # The `__main__` block of the shipped script, with the mode of no
        # Deployment: it puts the handler in place and then refuses the mode.
        with (
            mock.patch("signal.signal") as registered,
            mock.patch.dict(os.environ, {self.loop.MODE_ENV: "sometimes"}),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as refused,
        ):
            exec(compile(_script_text("loop.py"), f"{_STACK}:loop.py", "exec"), {"__name__": "__main__"})  # noqa: S102 - the shipped script
        self.assertEqual(refused.exception.code, self.loop.EXIT_USAGE)
        self.assertEqual(len(registered.call_args_list), 1)
        number, handler = registered.call_args_list[0].args
        self.assertEqual(number, signal.SIGTERM)
        with self.assertRaises(SystemExit) as stopped:
            handler(signal.SIGTERM, None)
        self.assertEqual(stopped.exception.code, 0)

    def test_each_container_runs_the_script_with_its_own_mode(self):
        block = _resource_block("kubernetes_deployment_v1", "cpu_cycle")
        self.assertIn('command = ["python3", "/app/loop.py"]', block)
        self.assertIn('mount_path = "/app"', block)
        self.assertIn("name = kubernetes_config_map_v1.cpu_cycle.metadata[0].name", block)
        # The mode of each Deployment is its key in the map of the two
        # workloads, and the keys are the script's two modes.
        self.assertRegex(block, r'name\s+= "CPU_MODE"\n\s+value = each\.key')
        self.assertEqual(self.loop.MODE_ENV, "CPU_MODE")
        keys = re.findall(r"^\s+(\w+)\s+= \{ name = \"cpu-", _stack(), re.M)
        self.assertEqual(sorted(keys), sorted(self.loop.MODES))
        self.assertRegex(_stack(), r'flat\s+= \{ name = "cpu-flat"')
        self.assertRegex(_stack(), r'burst\s+= \{ name = "cpu-burst"')

    def test_a_change_of_the_script_changes_the_pod_template(self):
        # A running loop does not read its file again. The pod template holds
        # the hash of the script, so a script change is a rollout.
        block = _resource_block("kubernetes_deployment_v1", "cpu_cycle")
        self.assertIn('(local.cpu_cycle_script_annotation) = sha256(kubernetes_config_map_v1.cpu_cycle.data["loop.py"])', block)


if __name__ == "__main__":
    unittest.main()

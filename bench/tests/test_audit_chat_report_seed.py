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

"""bench/tf/prebuilt/audit-chat-report/seed.py: a seed read the broker does not
answer is retried, then fails the plant; it is never taken as seeded (#6110).
With the file store it seeds nothing, and with any other switch value it stops."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "audit-chat-report"
AUDIT = "compliance-audit"
STREAM_REPO = "acme/fleet"

sys.path[:0] = [
    str(REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"),
    str(REPO / "agents" / "platform" / "scripts"),
]
import audit_report  # noqa: E402
import report_status  # noqa: E402

_spec = importlib.util.spec_from_file_location("audit_chat_report_seed", STACK / "seed.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


class Client:
    """Stands in for the broker store client: `list_runs` fails `failures` times,
    then answers and, like the real seed, leaves the marker when `marks`."""

    def __init__(self, stream: Path, failures: int, marks: bool = True):
        self.stream = stream
        self.failures = failures
        self.marks = marks
        self.calls = 0

    def list_runs(self, audit_id, repo):
        self.calls += 1
        if self.calls <= self.failures:
            raise report_status.ReportStoreUnavailable("the broker is not answering")
        if self.marks:
            (self.stream / report_status.SEED_MARKER_NAME).write_text("{}", encoding="utf-8")
        return []

    def read_latest(self, audit_id, repo):
        raise report_status.ReportStoreUnavailable("the broker is not answering")


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "reports" / AUDIT
    (root / "acme" / "fleet").mkdir(parents=True)
    (root / "acme" / "fleet" / report_status.LATEST_NAME).write_text("{}", encoding="utf-8")
    return root


def _run(monkeypatch, store, client):
    monkeypatch.setattr(audit_report, "_report_store_client", lambda: client)
    sleeps = []
    status = seed.seed_streams(audit_report, report_status, AUDIT, store, sleep=sleeps.append)
    return status, sleeps


def test_the_store_answers_exists_for_a_broker_it_cannot_read(store, monkeypatch):
    # Why the seed reads with ring(): exists() would take an outage as seeded.
    client = Client(store / "acme" / "fleet", failures=99)
    monkeypatch.setattr(audit_report, "_report_store_client", lambda: client)
    assert audit_report.report_store_dir(AUDIT, STREAM_REPO).exists() is True


def test_a_failed_seed_read_is_retried_then_goes_on(store, monkeypatch, capsys):
    client = Client(store / "acme" / "fleet", failures=2)
    status, sleeps = _run(monkeypatch, store, client)
    assert status == 0
    assert client.calls == 3
    assert sleeps == [1.0, 2.0]
    assert "try 1 of 4" in capsys.readouterr().err


def test_a_seed_read_that_never_succeeds_fails_the_plant(store, monkeypatch, capsys):
    client = Client(store / "acme" / "fleet", failures=99)
    status, sleeps = _run(monkeypatch, store, client)
    assert status == 1
    assert client.calls == seed.SEED_READ_ATTEMPTS
    assert len(sleeps) == seed.SEED_READ_ATTEMPTS - 1
    assert not (store / "acme" / "fleet" / report_status.SEED_MARKER_NAME).exists()
    assert "did not answer the seed read for acme/fleet" in capsys.readouterr().err


def test_a_read_that_leaves_no_marker_fails_the_plant(store, monkeypatch, capsys):
    client = Client(store / "acme" / "fleet", failures=0, marks=False)
    status, sleeps = _run(monkeypatch, store, client)
    assert status == 1
    assert sleeps == []
    assert "no seed marker after a broker read for acme/fleet" in capsys.readouterr().err


def test_a_seeded_stream_goes_on_at_once(store, monkeypatch):
    client = Client(store / "acme" / "fleet", failures=0)
    status, sleeps = _run(monkeypatch, store, client)
    assert status == 0
    assert (client.calls, sleeps) == (1, [])


def _main(monkeypatch, store, value):
    monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", value)
    monkeypatch.setattr(sys, "argv", ["seed.py", AUDIT, str(store)])
    monkeypatch.setattr(seed, "seed_streams", lambda *args, **kwargs: pytest.fail("seeded"))
    return seed.main()


@pytest.mark.parametrize("value", ["", "file"])
def test_the_file_store_has_nothing_to_seed(store, monkeypatch, capsys, value):
    assert _main(monkeypatch, store, value) == 0
    assert "nothing to seed" in capsys.readouterr().out


def test_an_unknown_switch_stops_the_seed(store, monkeypatch):
    with pytest.raises(SystemExit, match=re.escape("FLEET_AUDIT_REPORT_STORE='Broker'")):
        _main(monkeypatch, store, "Broker")


def test_the_broker_store_seeds_each_local_stream(store, monkeypatch):
    monkeypatch.setenv("FLEET_AUDIT_REPORT_STORE", "broker")
    monkeypatch.setattr(sys, "argv", ["seed.py", AUDIT, str(store)])
    seen = []
    monkeypatch.setattr(seed, "seed_streams", lambda audit_report, report_status, audit, root: seen.append((audit, root)) or 0)
    assert seed.main() == 0
    assert seen == [(AUDIT, store)]


@pytest.mark.parametrize("contents", [[], ["runs/"]], ids=["empty", "empty-runs"])
def test_a_local_stream_with_no_run_and_no_latest_is_nothing_to_seed(tmp_path, monkeypatch, contents):
    root = tmp_path / "reports" / AUDIT
    stream = root / "acme" / "bare"
    stream.mkdir(parents=True)
    for name in contents:
        (stream / name).mkdir()
    client = Client(stream, failures=0, marks=False)
    status, sleeps = _run(monkeypatch, root, client)
    assert (status, client.calls, sleeps) == (0, 0, [])


def test_a_local_stream_with_only_a_run_is_seeded(tmp_path, monkeypatch):
    root = tmp_path / "reports" / AUDIT
    runs = root / "acme" / "ring" / report_status.RUNS_DIR
    runs.mkdir(parents=True)
    (runs / "20261101T000000Z.json").write_text("{}", encoding="utf-8")
    client = Client(runs.parent, failures=0)
    status, _ = _run(monkeypatch, root, client)
    assert (status, client.calls) == (0, 1)

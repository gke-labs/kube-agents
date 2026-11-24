"""bench/tools/child_queue_time.py: queue-time stats, the never-confirmed
path, fan-out, what binds, and per-level attribution, over small synthetic
gateway and dispatcher logs."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "child_queue_time.py"
_spec = importlib.util.spec_from_file_location("child_queue_time", TOOL)
assert _spec is not None and _spec.loader is not None
cqt = importlib.util.module_from_spec(_spec)
sys.modules["child_queue_time"] = cqt
_spec.loader.exec_module(cqt)

GW = "[pod/platform-agent-a2a-gateway-abc/gateway] "
DP = "[pod/platform-agent-a2a-dispatcher-abc/dispatcher] "


def at(seconds: int) -> str:
    """A gateway-style timestamp, nanoseconds and all, ``seconds`` past a fixed minute."""
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"2026-11-24T{1 + hours:02d}:{minutes:02d}:{secs:02d}.123456789Z"


def line(prefix: str, **fields: Any) -> str:
    return prefix + json.dumps(fields)


def ingress(task: str, t: int, conversation: str, addressee: str = "cluster") -> str:
    return line(GW, time=at(t), msg="ingress", taskId=task, conversation=conversation, addressee=addressee)


def minted(parent: str, child: str, t: int, conversation: str, addressee: str = "cluster") -> str:
    return line(GW, time=at(t), msg="delegation minted", task=parent, conversation=conversation, parent=parent, child=child, addressee=addressee)


def disp(task: str, t: int, msg: str, kind: str = "child", profile: str = "cluster") -> str:
    return line(DP, time=at(t), msg=msg, profile=profile, tier="read", kind=kind, task=task)


def health(t: int, waiting: int, oldest_ns: int) -> str:
    durable = "profile-cluster-read-child"
    return line(DP, time=at(t), msg="dispatcher health", running={"cluster": 1}, waiting={durable: waiting}, noWorker={durable: waiting}, oldestWait={durable: oldest_ns})


CONV_A = "inject:run/session-burst-cluster-reads/1-L1-0"
CONV_B = "inject:run/session-burst-cluster-reads/1-L3-0"

GATEWAY_LOG = [
    "not json at all",
    ingress("p1", 0, CONV_A, addressee="chat-heron"),
    ingress("c1", 10, CONV_A),
    minted("p1", "c1", 10, CONV_A),
    ingress("c2", 10, CONV_A),
    minted("p1", "c2", 10, CONV_A),
    ingress("p2", 100, CONV_B, addressee="chat-lynx"),
    # c3's ingress names no conversation: attributed through its parent.
    line(GW, time=at(110), msg="ingress", taskId="c3", addressee="cluster"),
    minted("p2", "c3", 110, CONV_B),
    ingress("c4", 110, CONV_B),
    line(GW, time=at(900), msg="fan-out child ended", child="c4", parent="p2", conversation=CONV_B, state="canceled"),
    ingress("c5", 120, "inject:someone-else", addressee="platform"),
    minted("p3", "c5", 120, "inject:someone-else", addressee="platform"),
    ingress("t1", 0, "inject:top"),
]

DISPATCHER_LOG = [
    disp("c1", 10, "no worker took the task; the submission stays queued"),
    disp("c1", 20, "task confirmed to a worker"),
    disp("c2", 10, "no worker took the task; the submission stays queued"),
    disp("c2", 400, "task confirmed to a worker"),
    disp("c3", 110, "no worker took the task; the submission stays queued"),
    disp("c3", 1000, "task confirmed to a worker"),
    disp("c4", 110, "no worker took the task; the submission stays queued"),
    disp("c4", 900, "stopped before it was handed out; canceling it"),
    # Another profile's child is not this report's.
    disp("c5", 121, "task confirmed to a worker", profile="platform"),
    # A top-level task that waited 30s.
    disp("t1", 30, "task confirmed to a worker", kind="interactive", profile="cluster"),
    health(60, 1, 50_000_000_000),
    health(120, 2, 650_500_000_000),
    line(DP, time=at(130), msg="held back at admission", reason="profile concurrency", task="c4", kind="child", profile="cluster"),
]


def _report(levels=None):
    gateway, dispatcher = cqt.Gateway(), cqt.Dispatcher()
    gateway.read(cqt.log_records(GATEWAY_LOG))
    dispatcher.read(cqt.log_records(DISPATCHER_LOG))
    return cqt.analyse(gateway, dispatcher, "cluster", levels)


def test_timestamps_truncate_nanoseconds():
    got = cqt.parse_time("2026-11-24T01:02:38.041243440Z")
    assert got.microsecond == 41243 and got.utcoffset().total_seconds() == 0


def test_queue_time_is_ingress_to_confirm():
    q = _report()["queue_time_seconds"]
    # c1 10s, c2 390s, c3 890s.
    assert q["n"] == 3
    assert (q["p50"], q["p90"], q["p95"], q["max"]) == (390.0, 890.0, 890.0, 890.0)
    assert q["over_600s"] == 1


def test_stats_on_nothing():
    assert cqt.stats([]) == {"n": 0, "p50": None, "p90": None, "p95": None, "max": None, "over_600s": 0}


def test_a_child_never_confirmed_carries_its_last_message():
    never = _report()["never_confirmed"]
    assert never["count"] == 1
    assert never["tasks"] == [{"task": "c4", "last_msg": "held back at admission"}]


def test_fan_out_counts_children_per_parent():
    fan = _report()["fan_out"]
    assert fan["all"] == {"parents": 3, "children_per_parent": {"1": 1, "2": 2}}
    assert fan["cluster"] == {"parents": 2, "children_per_parent": {"2": 2}}


def test_what_binds():
    binds = _report()["binds"]
    assert binds["max_waiting_total"] == 2
    assert binds["max_oldest_wait_seconds"] == {"profile-cluster-read-child": 650.5}
    assert binds["cap_reason_lines"]["profile concurrency"] == 1
    assert binds["cap_reason_lines"]["install ceiling"] == 0
    assert (binds["top_level_confirmed"], binds["top_level_delayed"]) == (1, 1)


def _results(conversations_by_level: dict[int, list[str]], *, where: str = "trajectory") -> list[dict[str, Any]]:
    args = {
        "planned_levels": sorted(conversations_by_level),
        "levels": [
            {"level": lv, "conversations": [{"conversation": c} for c in convs]}
            for lv, convs in sorted(conversations_by_level.items())
        ],
    }
    if where == "trajectory":
        return [{"trajectory": [{"name": "inject.task", "args": {}}, {"name": cqt.BURST_ENTRY, "args": args}]}]
    return [{"trajectory": [], "metadata": {"burst_reads": args}}]


@pytest.mark.parametrize("where", ["trajectory", "metadata"])
def test_per_level_attribution(where):
    # The record names the unprefixed key on one level and the prefixed one on the other.
    levels = cqt.burst_levels(_results({1: [CONV_A.removeprefix("inject:")], 3: [CONV_B]}, where=where))
    assert levels == {CONV_A.removeprefix("inject:"): 1, CONV_B.removeprefix("inject:"): 3}
    report = _report(levels)
    per = report["per_level"]
    assert per["1"]["n"] == 2 and per["1"]["max"] == 390.0 and per["1"]["never_confirmed"] == 0
    # c3 through its parent's conversation; c4 never confirmed.
    assert per["3"]["n"] == 1 and per["3"]["max"] == 890.0 and per["3"]["never_confirmed"] == 1
    assert report["per_level_unattributed"] == 0
    text = cqt.render(report)
    assert "per level:" in text and "390s" in text


def test_no_record_means_no_per_level_block():
    assert "per_level" not in _report({})
    assert "per level" not in cqt.render(_report())


def test_the_cli_reads_a_directory(tmp_path, capsys):
    (tmp_path / "a2a-gateway-x.log").write_text("\n".join(GATEWAY_LOG) + "\n")
    (tmp_path / "a2a-dispatcher-x.log").write_text("\n".join(DISPATCHER_LOG) + "\n")
    (tmp_path / "results_session-burst-cluster-reads_rep1.json").write_text(json.dumps(_results({1: [CONV_A], 3: [CONV_B]})))
    assert cqt.main([str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "n=3 p50=390s p90=890s p95=890s max=890s over600=1 never=1" in text
    assert cqt.main([str(tmp_path), "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["per_level"]["3"]["never_confirmed"] == 1


def test_the_cli_needs_both_logs(tmp_path):
    (tmp_path / "a2a-gateway-x.log").write_text("")
    with pytest.raises(SystemExit):
        cqt.main([str(tmp_path)])

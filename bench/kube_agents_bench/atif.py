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

"""Transcripts as Harbor's Agent Trajectory Interchange Format (ATIF).

The file the agent writes and the scorer reads. Router tool calls are the root
trajectory's; each worker profile is a subagent trajectory named for it. What
ATIF has no field for is kept under ``extra.kube_agents``; a trajectory from
another agent (Claude Code, Gemini CLI) lacks that key, and the transcript is
derived from the standard fields instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kube_agents_bench.transcript import TranscriptSnapshot

SCHEMA_VERSION = "ATIF-v1.8"
AGENT_NAME = "kube-agents"
EXTRA_KEY = "kube_agents"

# Shell tools by agent: Hermes, Claude Code, Gemini CLI, Codex.
SHELL_TOOLS = {"terminal", "Bash", "run_shell_command", "shell", "exec_command"}


def from_snapshot(snap: TranscriptSnapshot, prompt: str = "") -> dict[str, Any]:
    router = [e for e in snap.trajectory if not e.get("agent")]
    workers: dict[str, list[dict[str, Any]]] = {}
    for e in snap.trajectory:
        if e.get("agent"):
            workers.setdefault(str(e["agent"]), []).append(e)

    steps = [{"step_id": 1, "source": "user", "message": prompt}]
    if snap.started_at:
        steps[0]["timestamp"] = datetime.fromtimestamp(snap.started_at, tz=timezone.utc).isoformat()
    steps.append({"step_id": 2, "source": "agent", "message": snap.output, **_calls(router, "r")})

    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "agent": {"name": AGENT_NAME, "version": "unknown"},
        "steps": steps,
        "extra": {
            EXTRA_KEY: {
                "final_message": snap.final_message,
                "worker_commands": snap.worker_commands,
                "worker_capture_gaps": snap.worker_capture_gaps,
            }
        },
    }
    if workers:
        doc["subagent_trajectories"] = [
            {
                "schema_version": SCHEMA_VERSION,
                "trajectory_id": profile,
                "agent": {"name": profile, "version": "unknown"},
                "steps": [{"step_id": 1, "source": "agent", "message": "", **_calls(calls, profile)}],
            }
            for profile, calls in workers.items()
        ]
    return doc


def _calls(entries: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    if not entries:
        return {}
    calls, results = [], []
    for i, e in enumerate(entries):
        call_id = f"{prefix}-{i}"
        args = e.get("args")
        calls.append(
            {
                "tool_call_id": call_id,
                "function_name": str(e.get("name") or ""),
                "arguments": args if isinstance(args, dict) else {"input": args},
                "extra": {k: e[k] for k in ("status", "task", "session", "at") if e.get(k) is not None},
            }
        )
        if e.get("result") is not None:
            results.append({"source_call_id": call_id, "content": _text(e["result"])})
    out: dict[str, Any] = {"tool_calls": calls}
    if results:
        out["observation"] = {"results": results}
    return out


def to_snapshot(doc: dict[str, Any]) -> TranscriptSnapshot:
    steps = doc.get("steps") or []
    agent_messages = [_text(s.get("message")) for s in steps if s.get("source") == "agent"]
    agent_messages = [m for m in agent_messages if m]
    prompt = next((_text(s.get("message")) for s in steps if s.get("source") == "user"), "")

    trajectory = _entries(doc, agent="")
    for sub in doc.get("subagent_trajectories") or []:
        trajectory += _entries(sub, agent=str((sub.get("agent") or {}).get("name") or ""))

    extra = (doc.get("extra") or {}).get(EXTRA_KEY)
    if extra is not None:
        worker_commands = extra.get("worker_commands")
        gaps = extra.get("worker_capture_gaps")
        final_message = extra.get("final_message") or ""
    else:
        worker_commands = [
            {"task": e.get("task", ""), "command": _command(e["args"])}
            for e in trajectory
            if e["name"] in SHELL_TOOLS and _command(e["args"])
        ]
        gaps = None
        final_message = agent_messages[-1] if agent_messages else ""

    started = next((s["timestamp"] for s in steps if s.get("timestamp")), None)
    return TranscriptSnapshot(
        output="\n\n".join(agent_messages),
        trajectory=trajectory,
        prompt_head=prompt[:64],
        started_at=datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() if started else 0.0,
        final_message=final_message,
        worker_commands=worker_commands,
        worker_capture_gaps=gaps,
    )


def _entries(traj: dict[str, Any], agent: str) -> list[dict[str, Any]]:
    out = []
    for step in traj.get("steps") or []:
        results = {
            r.get("source_call_id"): r.get("content")
            for r in ((step.get("observation") or {}).get("results") or [])
        }
        for call in step.get("tool_calls") or []:
            extra = call.get("extra") or {}
            args = call.get("arguments") or {}
            entry = {
                "name": call.get("function_name", ""),
                "args": args["input"] if set(args) == {"input"} else args,
                "result": results.get(call.get("tool_call_id")),
                "status": extra.get("status", "completed"),
                **{k: extra[k] for k in ("task", "session", "at") if k in extra},
            }
            if agent:
                entry["agent"] = agent
            out.append(entry)
    return out


def _command(args: Any) -> str:
    cmd = args.get("command") if isinstance(args, dict) else None
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    return str(cmd or "")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(p.get("text") or "") for p in value if isinstance(p, dict))
    return str(value)

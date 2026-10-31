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

"""Transcripts in the OpenAI Responses format.

The file the agent writes and the scorer reads: a response whose ``input`` and
``output`` are Responses items. What the format has no field for goes under
keys namespaced with ``kubeagents.x-k8s.io/``, such as the worker profile that
made a call. A trajectory from another agent lacks them, and the transcript is
derived from the standard fields instead.
"""

from __future__ import annotations

import json
from typing import Any

from kube_agents_bench.transcript import TranscriptSnapshot

NS = "kubeagents.x-k8s.io/"
ENTRY_KEYS = ("agent", "status", "task", "session", "at")

# Shell tools by agent: Hermes, Claude Code, Gemini CLI, Codex.
SHELL_TOOLS = {"terminal", "Bash", "run_shell_command", "shell", "exec_command"}


def from_snapshot(snap: TranscriptSnapshot, prompt: str = "") -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    for i, e in enumerate(snap.trajectory):
        call_id = f"call-{i}"
        output.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": str(e.get("name") or ""),
                "arguments": json.dumps(e.get("args")),
                **{NS + k: e[k] for k in ENTRY_KEYS if e.get(k) is not None},
            }
        )
        if e.get("result") is not None:
            output.append({"type": "function_call_output", "call_id": call_id, "output": _text(e["result"])})
    output.append(_message("assistant", snap.output))

    doc: dict[str, Any] = {
        "input": [_message("user", prompt)],
        "output": output,
        NS + "final_message": snap.final_message,
        NS + "worker_commands": snap.worker_commands,
        NS + "worker_capture_gaps": snap.worker_capture_gaps,
    }
    if snap.started_at:
        doc["created_at"] = snap.started_at
    return doc


def to_snapshot(doc: dict[str, Any]) -> TranscriptSnapshot:
    items = (doc.get("input") or []) + (doc.get("output") or [])
    prompt = next((_text(i.get("content")) for i in items if _is_message(i, "user")), "")
    replies = [t for t in (_text(i.get("content")) for i in items if _is_message(i, "assistant")) if t]

    results = {i.get("call_id"): i.get("output") for i in items if i.get("type") == "function_call_output"}
    trajectory = [
        {
            "name": i.get("name", ""),
            "args": _args(i.get("arguments")),
            "result": results.get(i.get("call_id")),
            "status": i.get(NS + "status", "completed"),
            **{k: i[NS + k] for k in ENTRY_KEYS if k != "status" and NS + k in i},
        }
        for i in items
        if i.get("type") == "function_call"
    ]

    if NS + "worker_commands" in doc:
        worker_commands = doc[NS + "worker_commands"]
        final_message = doc.get(NS + "final_message") or ""
    else:
        worker_commands = [
            {"task": e.get("task", ""), "command": _command(e["args"])}
            for e in trajectory
            if e["name"] in SHELL_TOOLS and _command(e["args"])
        ]
        final_message = replies[-1] if replies else ""

    return TranscriptSnapshot(
        output="\n\n".join(replies),
        trajectory=trajectory,
        prompt_head=prompt[:64],
        started_at=float(doc.get("created_at") or 0.0),
        final_message=final_message,
        worker_commands=worker_commands,
        worker_capture_gaps=doc.get(NS + "worker_capture_gaps"),
    )


def _message(role: str, text: str) -> dict[str, Any]:
    part = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": part, "text": text}]}


def _is_message(item: dict[str, Any], role: str) -> bool:
    return item.get("type", "message") == "message" and item.get("role") == role


def _args(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


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

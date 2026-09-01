"""Append-only detailed run logging shared by live CUJ scenarios."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class EvidenceLog:
    root: Path
    filename: str = "interactions.jsonl"
    _sequence: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.root / self.filename

    @classmethod
    def temporary(
        cls,
        prefix: str,
        *,
        directory: Path = Path("/tmp"),
    ) -> EvidenceLog:
        return cls(Path(tempfile.mkdtemp(prefix=prefix, dir=directory)))

    def write_transcript(self, request: str, interaction: dict[str, Any]) -> Path:
        """Write the conversation on its own, in the order a person reads it.

        The JSONL beside it is the machine record — every projection the
        portal returned. This is the exchange itself: what was asked, what
        came back, and the typed evidence and artifacts the answer rests on,
        so a reader does not have to reassemble a conversation out of
        repeated API payloads.
        """

        lines = [
            "=" * 78,
            f"USER -> {interaction.get('agentId', 'agent')}"
            f"  (session {interaction.get('sessionId', '')})",
            "=" * 78,
            request.strip(),
            "",
            "=" * 78,
            f"AGENT -> USER  (status: {interaction.get('status')})",
            "=" * 78,
            str(interaction.get("output") or "(no output)").strip(),
            "",
        ]
        for task in interaction.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            lines += [
                "=" * 78,
                f"DELEGATED TASK {task.get('taskId')}"
                f"  [{task.get('status')}, runs={task.get('runCount')}]",
                "=" * 78,
                f"title:   {task.get('title')}",
                f"summary: {task.get('summary')}",
                "",
            ]
            for item in task.get("evidence") or []:
                details = item.get("details") or {}
                lines.append(
                    f"  evidence {item.get('type')} [{item.get('status')}]"
                    f" via {details.get('apiMethod') or 'unknown'}"
                )
                lines.append(
                    "    " + json.dumps(details.get("analysis") or {}, indent=4)[:1500]
                )
            for item in task.get("artifacts") or []:
                manifest = item.get("manifest") or {}
                lines.append(f"  artifact {item.get('type')}")
                lines.append("    " + json.dumps(manifest, indent=4)[:1500])
            lines.append("")
        path = self.root / "conversation.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def record(self, event: str, data: Any) -> None:
        self._sequence += 1
        entry = {
            "sequence": self._sequence,
            "recordedAt": datetime.now(UTC).isoformat(),
            "event": event,
            "data": data,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True, default=str) + "\n")

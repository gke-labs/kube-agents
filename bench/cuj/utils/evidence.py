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
        """Write the run as a conversation, the way the portal's Chat tab shows it.

        The Chat tab renders a turn per message — the operator's prompt, then
        the agent's reply — and lists the delegated work beneath it as
        `assignee · status` cards. A reader comparing a run against the UI
        should not have to translate, so this file uses the same two-part
        shape. The JSONL beside it stays the machine record.
        """

        def block(role: str, subtitle: str, body: str) -> list[str]:
            head = f"{role}" + (f"  ({subtitle})" if subtitle else "")
            return [
                f"┌─ {head} " + "─" * max(0, 74 - len(head)),
                *[f"│ {line}" for line in (body or "(empty)").splitlines()],
                "└" + "─" * 77,
                "",
            ]

        agent = str(interaction.get("agentId") or "agent")
        profile = str(interaction.get("profile") or "")
        status = str(interaction.get("status") or "unknown")
        lines = [
            "CHAT",
            "=" * 78,
            "",
            *block("USER", str(interaction.get("sessionId") or ""), request.strip()),
            *block(
                f"ASSISTANT · {agent}",
                f"{profile} · {status}",
                str(interaction.get("output") or "").strip()
                or "Completed without a text response.",
            ),
        ]

        tasks = [t for t in interaction.get("tasks") or [] if isinstance(t, dict)]
        if tasks:
            lines += ["AGENT WORK", "=" * 78, ""]
        for task in tasks:
            lines.append(
                f"  {task.get('assignee', 'unassigned')} · {task.get('status')}"
                f"  ({task.get('taskId')}, run {task.get('runCount')})"
            )
            lines.append(f"    {task.get('title') or ''}")
            if task.get("summary"):
                lines.append(f"    ✓ {task['summary']}")
            elif task.get("error"):
                lines.append(f"    ✗ {task['error']}")
            for item in task.get("evidence") or []:
                details = item.get("details") or {}
                lines.append(
                    f"    · evidence {item.get('type')} [{item.get('status')}]"
                    f" — {details.get('apiMethod') or 'unknown method'}"
                )
                for label, value in (
                    ("request", details.get("request")),
                    ("analysis", details.get("analysis")),
                ):
                    rendered = json.dumps(value or {}, indent=2, sort_keys=True)
                    lines += [
                        f"        {label}:",
                        *[f"        {row}" for row in rendered.splitlines()[:40]],
                    ]
            for item in task.get("artifacts") or []:
                manifest = item.get("manifest") or {}
                rendered = json.dumps(manifest, indent=2, sort_keys=True)
                lines.append(f"    · artifact {item.get('type')}")
                lines += [f"        {row}" for row in rendered.splitlines()[:60]]
            if task.get("result"):
                lines += [
                    "    · report delivered to the user (also folded into the "
                    "reply above)",
                ]
            lines.append("")

        path = self.root / "conversation.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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

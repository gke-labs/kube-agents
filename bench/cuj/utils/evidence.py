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

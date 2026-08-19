"""Structured evidence output shared by live CUJ scenarios."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvidenceWriter:
    root: Path

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def temporary(
        cls,
        prefix: str,
        *,
        directory: Path = Path("/tmp"),
    ) -> EvidenceWriter:
        return cls(Path(tempfile.mkdtemp(prefix=prefix, dir=directory)))

    def write(self, relative_path: str | Path, value: Any) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("evidence path must stay beneath the evidence root")
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            rendered = value.rstrip() + "\n"
        else:
            rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
        path.write_text(rendered, encoding="utf-8")
        return path

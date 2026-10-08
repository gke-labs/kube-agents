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

"""The audit streams each task declares, for ``hack/ci-eval-pr.sh``.

A case whose stack starts real audit runs without grading their ledger lists
them in a top-level ``audit_streams:``; the runner holds each one's stream lock
for the case's units. Read with the YAML parser ``scripts/validate_bench_cases.py``
uses on the same key, once per run before the fan-out, so the runner and the
lint cannot read one file two ways.

Prints ``<case> [<stream>...]`` per task, in the order given. Exits non-zero,
naming the file, when a task cannot be read or its ``audit_streams:`` is not a
list of bare job ids.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

KEY = "audit_streams"
# A Platform Agent job id: what the runner splices into a lock directory's name.
BARE_ID = re.compile(r"\A[A-Za-z0-9_.-]+\Z")


class AuditStreamsError(Exception):
    pass


def declared_streams(task_yaml: Path) -> list[str]:
    try:
        doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AuditStreamsError(f"{task_yaml}: {exc}") from exc
    if not isinstance(doc, dict):
        raise AuditStreamsError(f"{task_yaml}: expected a YAML mapping at the top level")
    streams = doc.get(KEY, [])
    if not isinstance(streams, list) or not all(isinstance(s, str) and BARE_ID.match(s) for s in streams):
        raise AuditStreamsError(f"{task_yaml}: '{KEY}:' must be a list of audit job ids, got {streams!r}")
    return streams


def main(argv: list[str] | None = None) -> int:
    tasks = sys.argv[1:] if argv is None else argv
    try:
        lines = [" ".join([Path(task).parent.name, *declared_streams(Path(task))]) for task in tasks]
    except AuditStreamsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

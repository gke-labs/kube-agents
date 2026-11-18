#!/usr/bin/env python3
"""Validate Open Knowledge Format (OKF) v0.2 knowledge/ directories.

Checks every ``knowledge/`` tree in the repository (or the ``knowledge/``
directories passed on the command line) against the Open Knowledge Format v0.2
specification and the ``kube-agents`` contract in
``docs/architecture/06-api-and-data-contracts.md`` §5:

* Root ``index.md`` (navigation) and ``log.md`` (history) are reserved at the
  ``knowledge/`` root: they do not require YAML frontmatter, and if they carry
  frontmatter it must be a valid YAML mapping without a ``type`` key.
* Entry files (every other ``.md`` file under ``knowledge/``) must not be named
  ``index.md`` or ``log.md``, must carry valid YAML frontmatter that parses to
  a mapping, and must declare a non-empty string ``type`` that is not ``index``
  or ``log``.
* Recommended OKF v0.2 trust and lifecycle fields (``sources``, ``generated``,
  ``verified``, ``status``, ``stale_after``) and tolerated legacy ``timestamp``
  are optional, and are shape-checked when present:
  - ``sources``: non-empty list of non-empty strings or source mappings;
  - ``generated``: mapping with non-empty string ``by`` and ISO-8601 date or
    timestamp ``at``;
  - ``verified``: mapping (or non-empty list of mappings) with non-empty string
    ``by`` and ISO-8601 date or timestamp ``at``;
  - ``status``: ``proposed``, ``active``, ``rejected``, ``superseded``, or
    ``retired`` on ``decision`` and ``lesson`` entries, and ``draft``,
    ``stable``, or ``deprecated`` on all other OKF types;
  - ``stale_after`` and legacy ``timestamp``: ISO-8601 date (``YYYY-MM-DD``) or
    timestamp (``YYYY-MM-DDTHH:MM:SS...`` with timezone).
* Relative Markdown links and ``#fragment`` anchors inside ``knowledge/`` files
  must resolve.

Usage::

    python3 scripts/check_okf_knowledge.py
    python3 scripts/check_okf_knowledge.py examples/gitops-repo/knowledge
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import re
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

from check_docs_links import (  # noqa: E402
    AnchorCache,
    FRAGMENT_MARK,
    MARKDOWN_SUFFIXES,
    REPO_BLOB_URL_PREFIXES,
    SKIP_PREFIXES,
    fragment_part,
    link_target,
    markdown_links,
    page_anchors,
)

REPO = SCRIPT_DIR.parent

UTF8_ENCODING = "utf-8"
UTF8_BOM = "\ufeff"
CRLF = "\r\n"
CR = "\r"
LF = "\n"
NUL_SEPARATOR = "\0"
LIST_JOIN_SEPARATOR = ", "

GIT_LS_FILES_CMD = ("git", "ls-files", "-z")
CLI_DESCRIPTION = "Validate OKF v0.2 knowledge/ files."
CLI_ROOTS_ARG = "roots"
CLI_ZERO_OR_MORE = "*"
CLI_ROOTS_HELP = (
    "Optional knowledge/ root directories to validate (defaults to git-tracked knowledge/ roots)."
)

KNOWLEDGE_DIR_NAME = "knowledge"
MARKDOWN_GLOB = "*.md"
GIT_KNOWLEDGE_GLOBS = ("knowledge/*.md", "knowledge/**/*.md", "**/knowledge/*.md", "**/knowledge/**/*.md")

FRONTMATTER_OPEN = "---"
FRONTMATTER_CLOSERS = frozenset({"---", "..."})

RESERVED_ROOT_FILENAMES = frozenset({"index.md", "log.md"})
RESERVED_TYPES = frozenset({"index", "log"})

KEY_TYPE = "type"
KEY_SOURCES = "sources"
KEY_GENERATED = "generated"
KEY_VERIFIED = "verified"
KEY_STATUS = "status"
KEY_STALE_AFTER = "stale_after"
KEY_TIMESTAMP = "timestamp"
KEY_BY = "by"
KEY_AT = "at"

DECISION_LESSON_TYPES = frozenset({"decision", "lesson"})
DECISION_LESSON_STATUSES = frozenset({"proposed", "active", "rejected", "superseded", "retired"})
STANDARD_OKF_STATUSES = frozenset({"draft", "stable", "deprecated"})

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
UTC_SUFFIX = "Z"
UTC_OFFSET = "+00:00"

FRONTMATTER_NONE = "none"
FRONTMATTER_UNCLOSED = "unclosed"
FRONTMATTER_CLOSED = "closed"

MSG_NO_KNOWLEDGE_FILES = "no knowledge/ Markdown files found to validate"
MSG_UNCLOSED_FRONTMATTER = "frontmatter opens with '---' on line 1 but is never closed by '---' or '...'"
MSG_MISSING_FRONTMATTER = "missing YAML frontmatter (must open with '---' on line 1 and close with '---')"
MSG_INVALID_YAML_PREFIX = "frontmatter is not valid YAML"
MSG_FRONTMATTER_NOT_MAPPING = "frontmatter must be a YAML mapping"
MSG_ROOT_HAS_TYPE = "reserved root file must not declare 'type' in frontmatter"
MSG_RESERVED_ENTRY_FILENAME = "filename is reserved for the knowledge/ root and must not be used for an entry"
MSG_MISSING_TYPE = "frontmatter is missing required key 'type'"
MSG_EMPTY_TYPE = "frontmatter 'type' must be a non-empty string"
MSG_RESERVED_TYPE = "frontmatter 'type' must not use reserved value"
MSG_INVALID_SOURCES = (
    "frontmatter 'sources' must be a non-empty list of non-empty strings or source mappings"
)
MSG_INVALID_GENERATED = (
    "frontmatter 'generated' must be a mapping with non-empty string 'by' and ISO-8601 date/timestamp 'at'"
)
MSG_INVALID_VERIFIED = (
    "frontmatter 'verified' must be a mapping (or non-empty list of mappings) with non-empty string 'by' and ISO-8601 date/timestamp 'at'"
)
MSG_INVALID_STALE_AFTER = (
    "frontmatter 'stale_after' must be an ISO-8601 date (YYYY-MM-DD) or timestamp"
)
MSG_INVALID_TIMESTAMP = (
    "frontmatter 'timestamp' must be an ISO-8601 date (YYYY-MM-DD) or timestamp"
)
MSG_INVALID_STATUS_TYPE = "frontmatter 'status' must be a non-empty string"
MSG_BROKEN_LINK_PREFIX = "broken relative link"
MSG_UNTRACKED_LINK_PREFIX = "relative link points to an untracked path"
MSG_BROKEN_ANCHOR_PREFIX = "broken anchor"


@dataclass(frozen=True)
class SplitFrontmatter:
    state: str
    frontmatter: str
    body: str


def split_frontmatter(text: str) -> SplitFrontmatter:
    """Split leading YAML frontmatter from ``text``."""
    normalised = text.replace(CRLF, LF).replace(CR, LF).lstrip(UTF8_BOM)
    lines = normalised.split(LF)
    if not lines or lines[0].rstrip() != FRONTMATTER_OPEN:
        return SplitFrontmatter(FRONTMATTER_NONE, "", normalised)
    for index in range(1, len(lines)):
        if lines[index].rstrip() in FRONTMATTER_CLOSERS:
            return SplitFrontmatter(
                FRONTMATTER_CLOSED,
                LF.join(lines[1:index]),
                LF.join(lines[index + 1 :]),
            )
    return SplitFrontmatter(FRONTMATTER_UNCLOSED, "", normalised)


def is_valid_date_or_timestamp(value: object) -> bool:
    """True when ``value`` is a valid ISO-8601 date (YYYY-MM-DD) or timestamp."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (date, datetime)):
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    if ISO_DATE_RE.match(text):
        try:
            date.fromisoformat(text)
            return True
        except ValueError:
            return False
    if ISO_TIMESTAMP_RE.match(text):
        iso_text = text[:-1] + UTC_OFFSET if text.endswith(UTC_SUFFIX) else text
        try:
            datetime.fromisoformat(iso_text)
            return True
        except ValueError:
            return False
    return False


def is_valid_source_item(item: object) -> bool:
    """True when one element of ``sources`` is a non-empty string or valid source mapping."""
    if isinstance(item, str):
        return bool(item.strip())
    if isinstance(item, dict) and item:
        has_non_empty_string = False
        for key, val in item.items():
            if not isinstance(key, str) or not key.strip():
                return False
            if val is None:
                return False
            if isinstance(val, str):
                if not val.strip():
                    return False
                has_non_empty_string = True
        return has_non_empty_string
    return False


def is_valid_provenance_stamp(mapping: object) -> bool:
    """True when ``mapping`` is a dict carrying non-empty string ``by`` and valid ``at``."""
    if not isinstance(mapping, dict):
        return False
    by_val = mapping.get(KEY_BY)
    at_val = mapping.get(KEY_AT)
    if not isinstance(by_val, str) or not by_val.strip():
        return False
    return is_valid_date_or_timestamp(at_val)


def validate_optional_fields(front: dict[str, object], entry_type: str) -> list[str]:
    """Validate OKF v0.2 recommended fields and legacy ``timestamp`` when present."""
    errors: list[str] = []

    if KEY_SOURCES in front:
        sources = front[KEY_SOURCES]
        if (
            not isinstance(sources, list)
            or not sources
            or not all(is_valid_source_item(item) for item in sources)
        ):
            errors.append(MSG_INVALID_SOURCES)

    if KEY_GENERATED in front:
        if not is_valid_provenance_stamp(front[KEY_GENERATED]):
            errors.append(MSG_INVALID_GENERATED)

    if KEY_VERIFIED in front:
        verified = front[KEY_VERIFIED]
        if isinstance(verified, list):
            if not verified or not all(is_valid_provenance_stamp(item) for item in verified):
                errors.append(MSG_INVALID_VERIFIED)
        elif not is_valid_provenance_stamp(verified):
            errors.append(MSG_INVALID_VERIFIED)

    if KEY_STALE_AFTER in front:
        if not is_valid_date_or_timestamp(front[KEY_STALE_AFTER]):
            errors.append(MSG_INVALID_STALE_AFTER)

    if KEY_TIMESTAMP in front:
        if not is_valid_date_or_timestamp(front[KEY_TIMESTAMP]):
            errors.append(MSG_INVALID_TIMESTAMP)

    if KEY_STATUS in front:
        status = front[KEY_STATUS]
        if not isinstance(status, str) or not status.strip():
            errors.append(MSG_INVALID_STATUS_TYPE)
        else:
            allowed = (
                DECISION_LESSON_STATUSES
                if entry_type in DECISION_LESSON_TYPES
                else STANDARD_OKF_STATUSES
            )
            if status not in allowed:
                allowed_list = LIST_JOIN_SEPARATOR.join(sorted(allowed))
                errors.append(
                    f"frontmatter 'status' value '{status}' is not allowed for type '{entry_type}' "
                    f"(expected one of: {allowed_list})"
                )

    return errors


def validate_file_links(
    path: Path,
    *,
    tracked: set[Path] | None,
    anchors: AnchorCache,
) -> list[str]:
    """Validate navigable relative links and ``#fragment`` anchors in ``path``."""
    errors: list[str] = []
    for lineno, target in markdown_links(path):
        fragment = fragment_part(target)
        if target.startswith(FRAGMENT_MARK):
            if fragment and fragment not in page_anchors(path, anchors):
                errors.append(f"line {lineno}: {MSG_BROKEN_ANCHOR_PREFIX} '{target}'")
            continue
        if target.startswith(SKIP_PREFIXES) or target.startswith(REPO_BLOB_URL_PREFIXES):
            continue
        candidate = link_target(path, target)
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if not resolved.exists():
            errors.append(f"line {lineno}: {MSG_BROKEN_LINK_PREFIX} '{target}'")
            continue
        if tracked is not None:
            is_tracked = resolved in tracked or (
                resolved.is_dir()
                and any(
                     resolved == parent
                    for tracked_file in tracked
                    for parent in tracked_file.parents
                )
            )
            if not is_tracked:
                errors.append(f"line {lineno}: {MSG_UNTRACKED_LINK_PREFIX} '{target}'")
                continue
        if fragment and resolved.is_file() and resolved.suffix in MARKDOWN_SUFFIXES:
            if fragment not in page_anchors(resolved, anchors):
                errors.append(f"line {lineno}: {MSG_BROKEN_ANCHOR_PREFIX} '{target}'")
    return errors


def validate_knowledge_file(
    path: Path,
    knowledge_root: Path,
    *,
    tracked: set[Path] | None = None,
    anchors: AnchorCache | None = None,
) -> list[str]:
    """Validate one Markdown file inside ``knowledge_root``."""
    anchor_cache: AnchorCache = {} if anchors is None else anchors
    rel = path.resolve().relative_to(knowledge_root.resolve())
    is_root_file = len(rel.parts) == 1
    is_reserved_name = path.name in RESERVED_ROOT_FILENAMES

    text = path.read_text(encoding=UTF8_ENCODING)
    split = split_frontmatter(text)
    errors: list[str] = []

    if is_root_file and is_reserved_name:
        if split.state == FRONTMATTER_UNCLOSED:
            errors.append(MSG_UNCLOSED_FRONTMATTER)
        elif split.state == FRONTMATTER_CLOSED:
            try:
                front = yaml.safe_load(split.frontmatter)
            except (yaml.YAMLError, ValueError, RecursionError) as exc:
                errors.append(f"{MSG_INVALID_YAML_PREFIX}: {type(exc).__name__}")
                front = None
            if front is not None:
                if not isinstance(front, dict):
                    errors.append(MSG_FRONTMATTER_NOT_MAPPING)
                elif KEY_TYPE in front:
                    errors.append(MSG_ROOT_HAS_TYPE)
        errors.extend(validate_file_links(path, tracked=tracked, anchors=anchor_cache))
        return errors

    if is_reserved_name:
        errors.append(f"{MSG_RESERVED_ENTRY_FILENAME}: '{path.name}'")

    if split.state == FRONTMATTER_NONE:
        errors.append(MSG_MISSING_FRONTMATTER)
        errors.extend(validate_file_links(path, tracked=tracked, anchors=anchor_cache))
        return errors

    if split.state == FRONTMATTER_UNCLOSED:
        errors.append(MSG_UNCLOSED_FRONTMATTER)
        errors.extend(validate_file_links(path, tracked=tracked, anchors=anchor_cache))
        return errors

    try:
        front = yaml.safe_load(split.frontmatter)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        errors.append(f"{MSG_INVALID_YAML_PREFIX}: {type(exc).__name__}")
        errors.extend(validate_file_links(path, tracked=tracked, anchors=anchor_cache))
        return errors

    if not isinstance(front, dict):
        errors.append(MSG_FRONTMATTER_NOT_MAPPING)
        errors.extend(validate_file_links(path, tracked=tracked, anchors=anchor_cache))
        return errors

    if KEY_TYPE not in front:
        errors.append(MSG_MISSING_TYPE)
        errors.extend(validate_file_links(path, tracked=tracked, anchors=anchor_cache))
        return errors

    raw_type = front[KEY_TYPE]
    if not isinstance(raw_type, str) or not raw_type.strip():
        errors.append(MSG_EMPTY_TYPE)
        errors.extend(validate_file_links(path, tracked=tracked, anchors=anchor_cache))
        return errors

    entry_type = raw_type.strip()
    if entry_type.lower() in RESERVED_TYPES:
        errors.append(f"{MSG_RESERVED_TYPE} '{entry_type}'")
    else:
        errors.extend(validate_optional_fields(front, entry_type))

    errors.extend(validate_file_links(path, tracked=tracked, anchors=anchor_cache))
    return errors


def validate_knowledge_root(
    knowledge_root: Path,
    *,
    display_root: Path | None = None,
    tracked: set[Path] | None = None,
) -> tuple[int, list[str]]:
    """Validate every ``.md`` file under ``knowledge_root``.

    Returns ``(file_count, formatted_errors)``.
    """
    resolved_root = knowledge_root.resolve()
    base = resolved_root.parent if display_root is None else display_root.resolve()
    if not resolved_root.is_dir():
        return 0, [f"{knowledge_root}: {MSG_NO_KNOWLEDGE_FILES}"]

    files = sorted(
        path
        for path in resolved_root.rglob(MARKDOWN_GLOB)
        if path.is_file() and (tracked is None or path.resolve() in tracked)
    )
    if not files:
        return 0, [f"{knowledge_root}: {MSG_NO_KNOWLEDGE_FILES}"]

    anchors: AnchorCache = {}
    all_errors: list[str] = []
    for path in files:
        try:
            label = path.resolve().relative_to(base).as_posix()
        except ValueError:
            label = path.as_posix()
        for err in validate_knowledge_file(
            path,
            resolved_root,
            tracked=tracked,
            anchors=anchors,
        ):
            all_errors.append(f"{label}: {err}")
    return len(files), all_errors


def tracked_paths(repo: Path = REPO) -> set[Path]:
    """Return every git-tracked file path under ``repo``."""
    out = subprocess.run(
        [*GIT_LS_FILES_CMD],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split(NUL_SEPARATOR)
    return {(repo / rel).resolve() for rel in out if rel and (repo / rel).is_file()}


def discover_knowledge_roots(repo: Path = REPO) -> list[Path]:
    """Discover every git-tracked ``knowledge/`` root directory under ``repo``."""
    out = subprocess.run(
        [*GIT_LS_FILES_CMD, *GIT_KNOWLEDGE_GLOBS],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split(NUL_SEPARATOR)
    roots: set[Path] = set()
    for rel in out:
        if not rel:
            continue
        path = Path(rel)
        for index, part in enumerate(path.parts):
            if part == KNOWLEDGE_DIR_NAME:
                roots.add((repo / Path(*path.parts[: index + 1])).resolve())
                break
    return sorted(roots)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=CLI_DESCRIPTION)
    parser.add_argument(
        CLI_ROOTS_ARG,
        nargs=CLI_ZERO_OR_MORE,
        type=Path,
        help=CLI_ROOTS_HELP,
    )
    args = parser.parse_args(argv)

    if args.roots:
        roots = [root.resolve() for root in args.roots]
        tracked = None
        display_root = None
    else:
        roots = discover_knowledge_roots(REPO)
        tracked = tracked_paths(REPO)
        display_root = REPO

    if not roots:
        print(f"FAIL: {MSG_NO_KNOWLEDGE_FILES}")
        return 1

    total_files = 0
    errors: list[str] = []
    for root in roots:
        count, root_errors = validate_knowledge_root(
            root,
            display_root=display_root,
            tracked=tracked,
        )
        total_files += count
        errors.extend(root_errors)

    if errors:
        print(f"FAIL: {len(errors)} OKF knowledge error(s):\n")
        for err in errors:
            print(f"  {err}")
        return 1

    print(
        f"Checked {total_files} OKF knowledge file(s) across {len(roots)} knowledge/ root(s) -- all valid."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

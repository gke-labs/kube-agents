#!/usr/bin/env python3
"""Unit tests for scripts/check_okf_knowledge.py.

Run:
    python3 -m unittest discover -s scripts -p 'test_check_okf_knowledge.py' -v
    pytest scripts/test_check_okf_knowledge.py
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_okf_knowledge  # noqa: E402


class KnowledgeFixture(unittest.TestCase):
    """Temporary knowledge/ directory helper for OKF validator tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir()

    def write(self, rel_path: str, content: str) -> Path:
        target = self.knowledge / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return target

    def validate(self, *, tracked: set[Path] | None = None) -> tuple[int, list[str]]:
        return check_okf_knowledge.validate_knowledge_root(
            self.knowledge,
            display_root=self.root,
            tracked=tracked,
        )


class CheckOkfKnowledgeTest(KnowledgeFixture):
    def test_committed_repository_knowledge_trees_pass(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check_okf_knowledge.main([])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("all valid", buf.getvalue())

    def test_valid_knowledge_tree_with_recommended_fields_passes(self) -> None:
        self.write(
            "index.md",
            """
            # Knowledge Index

            - [Standard GKE](cluster-blueprint/standard-gke.md#baseline)
            - [Checkout memory lesson](lesson/LES-2026-001.md)
            """,
        )
        self.write(
            "log.md",
            """
            ---
            title: Knowledge change log
            ---

            # History

            - Added [Standard GKE](cluster-blueprint/standard-gke.md).
            """,
        )
        self.write(
            "STATE_OF_BEING.md",
            """
            # State of Being

            Baseline summary linking [Standard GKE](cluster-blueprint/standard-gke.md).
            """,
        )
        self.write(
            "cluster-blueprint/standard-gke.md",
            """
            ---
            type: cluster-blueprint
            title: Standard GKE cluster blueprint
            status: stable
            sources:
              - https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md
              - uri: clusters/prod/provisioning/cluster.yaml
                kind: manifest
            generated:
              by: human
              at: 2026-07-23T00:00:00Z
            verified:
              by: platform-team
              at: 2026-07-24
            stale_after: 2027-01-01
            ---

            # Standard GKE cluster blueprint

            ## Baseline

            Back to [index](../index.md).
            """,
        )
        self.write(
            "lesson/LES-2026-001.md",
            """
            ---
            type: lesson
            id: LES-2026-001
            title: Keep checkout memory above 256Mi
            status: active
            verified:
              - by: fleet-audit
                at: "2026-09-11T02:00:00+00:00"
            timestamp: 2026-09-10T00:00:00Z
            ---

            # Checkout lesson

            See [Standard GKE](../cluster-blueprint/standard-gke.md#baseline).
            """,
        )

        count, errors = self.validate()
        self.assertEqual(count, 5)
        self.assertEqual(errors, [])

    def test_root_index_and_log_reject_type_and_malformed_frontmatter(self) -> None:
        self.write(
            "index.md",
            """
            ---
            type: index
            title: Bad index
            ---

            # Index
            """,
        )
        self.write(
            "log.md",
            """
            ---
            type: log
            ---

            # Log
            """,
        )
        _, errors = self.validate()
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(check_okf_knowledge.MSG_ROOT_HAS_TYPE in err for err in errors))

        self.write(
            "index.md",
            """
            ---
            ---

            # Empty frontmatter
            """,
        )
        self.write(
            "log.md",
            """
            ---
            null
            ---

            # Null frontmatter
            """,
        )
        _, empty_errors = self.validate()
        self.assertEqual(len(empty_errors), 2, empty_errors)
        self.assertTrue(
            all(check_okf_knowledge.MSG_FRONTMATTER_NOT_MAPPING in err for err in empty_errors),
            empty_errors,
        )

        self.write(
            "index.md",
            """
            ---
            title: Unclosed frontmatter
            # No closing delimiter
            """,
        )
        _, unclosed_errors = self.validate()
        self.assertTrue(
            any(check_okf_knowledge.MSG_UNCLOSED_FRONTMATTER in err for err in unclosed_errors)
        )

    def test_entry_rejects_reserved_filenames_and_reserved_types(self) -> None:
        self.write(
            "runbook/index.md",
            """
            ---
            type: runbook
            title: Reserved filename
            ---

            # Runbook
            """,
        )
        self.write(
            "runbook/Log.md",
            """
            ---
            type: runbook
            title: Reserved log filename
            ---

            # Runbook log
            """,
        )
        self.write(
            "runbook/bad-type-index.md",
            """
            ---
            type: index
            title: Reserved type index
            ---

            # Bad type
            """,
        )
        self.write(
            "runbook/bad-type-log.md",
            """
            ---
            type: LOG
            title: Reserved type log
            ---

            # Bad type
            """,
        )
        _, errors = self.validate()
        self.assertTrue(
            any(
                "runbook/index.md" in err and check_okf_knowledge.MSG_ROOT_HAS_TYPE in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "runbook/Log.md" in err and check_okf_knowledge.MSG_ROOT_HAS_TYPE in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "bad-type-index.md" in err and check_okf_knowledge.MSG_RESERVED_TYPE in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "bad-type-log.md" in err and check_okf_knowledge.MSG_RESERVED_TYPE in err
                for err in errors
            ),
            errors,
        )

        # Untyped index.md and log.md are allowed at any directory level, whereas
        # STATE_OF_BEING.md and README.md remain root-only.
        self.write(
            "runbook/index.md",
            """
            # Runbooks
            """,
        )
        self.write(
            "runbook/Log.md",
            """
            # Runbook history
            """,
        )
        self.write(
            "runbook/STATE_OF_BEING.md",
            """
            # Subdirectory state of being without frontmatter
            """,
        )
        _, subdir_errors = self.validate()
        self.assertFalse(
            any("runbook/index.md" in err or "runbook/Log.md" in err for err in subdir_errors),
            subdir_errors,
        )
        self.assertTrue(
            any(
                "runbook/STATE_OF_BEING.md" in err
                and check_okf_knowledge.MSG_MISSING_FRONTMATTER in err
                for err in subdir_errors
            ),
            subdir_errors,
        )

    def test_missing_malformed_or_typeless_frontmatter_is_rejected(self) -> None:
        self.write(
            "runbook/no-frontmatter.md",
            """
            # Runbook without frontmatter
            """,
        )
        self.write(
            "runbook/unclosed.md",
            """
            ---
            type: runbook
            # never closed
            """,
        )
        self.write(
            "runbook/bad-yaml.md",
            """
            ---
            type: [unclosed
            ---

            # Bad YAML
            """,
        )
        self.write(
            "runbook/not-mapping.md",
            """
            ---
            - just
            - a
            - list
            ---

            # Not a mapping
            """,
        )
        self.write(
            "runbook/missing-type.md",
            """
            ---
            title: Missing type
            ---

            # Missing type
            """,
        )
        self.write(
            "runbook/empty-type.md",
            """
            ---
            type: "   "
            ---

            # Empty type
            """,
        )
        _, errors = self.validate()
        self.assertTrue(
            any(
                "no-frontmatter.md" in err and check_okf_knowledge.MSG_MISSING_FRONTMATTER in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "unclosed.md" in err and check_okf_knowledge.MSG_UNCLOSED_FRONTMATTER in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "bad-yaml.md" in err and check_okf_knowledge.MSG_INVALID_YAML_PREFIX in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "not-mapping.md" in err and check_okf_knowledge.MSG_FRONTMATTER_NOT_MAPPING in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "missing-type.md" in err and check_okf_knowledge.MSG_MISSING_TYPE in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                "empty-type.md" in err and check_okf_knowledge.MSG_EMPTY_TYPE in err
                for err in errors
            ),
            errors,
        )

    def test_invalid_recommended_fields_and_legacy_timestamp_are_rejected(self) -> None:
        self.write(
            "runbook/bad-fields.md",
            """
            ---
            type: runbook
            sources: []
            generated:
              by: ""
              at: not-a-date
            verified: "alice"
            stale_after: "2026-02-30"
            timestamp: 12345
            ---

            # Bad fields
            """,
        )
        _, errors = self.validate()
        self.assertTrue(any(check_okf_knowledge.MSG_INVALID_SOURCES in err for err in errors), errors)
        self.assertTrue(any(check_okf_knowledge.MSG_INVALID_GENERATED in err for err in errors), errors)
        self.assertTrue(any(check_okf_knowledge.MSG_INVALID_VERIFIED in err for err in errors), errors)
        self.assertTrue(any(check_okf_knowledge.MSG_INVALID_STALE_AFTER in err for err in errors), errors)
        self.assertTrue(any(check_okf_knowledge.MSG_INVALID_TIMESTAMP in err for err in errors), errors)

        self.write(
            "runbook/bad-sources-item.md",
            """
            ---
            type: runbook
            sources:
              - ""
            verified:
              - by: reviewer
                at: "not-a-timestamp"
            stale_after: true
            ---

            # Bad sources item
            """,
        )
        _, item_errors = self.validate()
        self.assertTrue(
            any(
                "bad-sources-item.md" in err and check_okf_knowledge.MSG_INVALID_SOURCES in err
                for err in item_errors
            ),
            item_errors,
        )
        self.assertTrue(
            any(
                "bad-sources-item.md" in err and check_okf_knowledge.MSG_INVALID_VERIFIED in err
                for err in item_errors
            ),
            item_errors,
        )
        self.assertTrue(
            any(
                "bad-sources-item.md" in err and check_okf_knowledge.MSG_INVALID_STALE_AFTER in err
                for err in item_errors
            ),
            item_errors,
        )

        self.write(
            "runbook/naive-timestamps.md",
            """
            ---
            type: runbook
            generated:
              by: human
              at: 2026-07-23T00:00:00
            stale_after: 2026-07-23 00:00:00
            timestamp: 2026-07-23T12:30:00
            ---

            # Naive unquoted timestamps without timezone
            """,
        )
        _, naive_errors = self.validate()
        self.assertTrue(
            any(
                "naive-timestamps.md" in err and check_okf_knowledge.MSG_INVALID_GENERATED in err
                for err in naive_errors
            ),
            naive_errors,
        )
        self.assertTrue(
            any(
                "naive-timestamps.md" in err and check_okf_knowledge.MSG_INVALID_STALE_AFTER in err
                for err in naive_errors
            ),
            naive_errors,
        )
        self.assertTrue(
            any(
                "naive-timestamps.md" in err and check_okf_knowledge.MSG_INVALID_TIMESTAMP in err
                for err in naive_errors
            ),
            naive_errors,
        )

    def test_status_vocabulary_for_decision_lesson_vs_other_types(self) -> None:
        for status in ("proposed", "active", "rejected", "superseded", "retired"):
            self.write(
                f"decision/dec-{status}.md",
                f"""
                ---
                type: Decision
                status: " {status} "
                ---

                # Decision {status}
                """,
            )
            self.write(
                f"lesson/les-{status}.md",
                f"""
                ---
                type: LESSON
                status: {status}
                ---

                # Lesson {status}
                """,
            )
        for status in ("draft", "stable", "deprecated"):
            self.write(
                f"runbook/run-{status}.md",
                f"""
                ---
                type: runbook
                status: {status}
                ---

                # Runbook {status}
                """,
            )
        _, valid_errors = self.validate()
        self.assertEqual(valid_errors, [])

        self.write(
            "decision/bad-status.md",
            """
            ---
            type: decision
            status: stable
            ---

            # Decision with standard OKF status
            """,
        )
        self.write(
            "runbook/bad-status.md",
            """
            ---
            type: runbook
            status: active
            ---

            # Runbook with decision status
            """,
        )
        self.write(
            "observation/non-string-status.md",
            """
            ---
            type: observation
            status: 123
            ---

            # Non-string status
            """,
        )
        _, invalid_errors = self.validate()
        self.assertTrue(
            any(
                "decision/bad-status.md" in err
                and "value 'stable' is not allowed for type 'decision'" in err
                for err in invalid_errors
            ),
            invalid_errors,
        )
        self.assertTrue(
            any(
                "runbook/bad-status.md" in err
                and "value 'active' is not allowed for type 'runbook'" in err
                for err in invalid_errors
            ),
            invalid_errors,
        )
        self.assertTrue(
            any(
                "observation/non-string-status.md" in err
                and check_okf_knowledge.MSG_INVALID_STATUS_TYPE in err
                for err in invalid_errors
            ),
            invalid_errors,
        )

    def test_broken_relative_links_and_anchors_are_rejected(self) -> None:
        target = self.write(
            "runbook/target.md",
            """
            ---
            type: runbook
            ---

            # Target Runbook

            ## Existing Section
            """,
        )
        broken = self.write(
            "runbook/links.md",
            """
            ---
            type: runbook
            ---

            # Links Runbook

            - [Missing file](does-not-exist.md)
            - [Missing anchor](target.md#no-such-section)
            - [Missing self anchor](#missing-local)
            - `inline [ignored](ignored.md)`
            """,
        )
        _, errors = self.validate()
        self.assertEqual(len(errors), 3, errors)
        self.assertTrue(
            any(
                check_okf_knowledge.MSG_BROKEN_LINK_PREFIX in err and "does-not-exist.md" in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                check_okf_knowledge.MSG_BROKEN_ANCHOR_PREFIX in err
                and "target.md#no-such-section" in err
                for err in errors
            ),
            errors,
        )
        self.assertTrue(
            any(
                check_okf_knowledge.MSG_BROKEN_ANCHOR_PREFIX in err and "#missing-local" in err
                for err in errors
            ),
            errors,
        )

        # Untracked link target is rejected when `tracked` is enforced.
        untracked_peer = self.write(
            "runbook/untracked.md",
            """
            ---
            type: runbook
            ---

            # Untracked
            """,
        )
        linker = self.write(
            "runbook/links.md",
            """
            ---
            type: runbook
            ---

            # Links Runbook

            - [Valid tracked](target.md#existing-section)
            - [Untracked file](untracked.md)
            """,
        )
        _, tracked_errors = self.validate(tracked={target.resolve(), linker.resolve()})
        self.assertEqual(len(tracked_errors), 1, tracked_errors)
        self.assertIn(check_okf_knowledge.MSG_UNTRACKED_LINK_PREFIX, tracked_errors[0])
        self.assertNotEqual(untracked_peer, linker)

    def test_frontmatter_regex_and_ellipsis_closer_do_not_break_link_or_anchor_checks(self) -> None:
        self.write(
            "runbook/target-ellipsis.md",
            """
            ---
            type: runbook
            # frontmatter comment that must not become a heading anchor
            ...

            # Target Runbook

            ---

            ## Details
            """,
        )
        self.write(
            "decision/DEC-2026-001.md",
            """
            ---
            type: decision
            status: active
            guard:
              rule: Finding IDs match pattern
              expect:
                file: findings.yaml
                path: $.id
                op: regex
                value: "^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$"
            notes: |
              [not-a-ref]: missing-frontmatter-target.md
            ---

            # Decision with Regex Guard

            See [Target Runbook](../runbook/target-ellipsis.md#target-runbook) and
            [Details](../runbook/target-ellipsis.md#details).
            """,
        )
        _, errors = self.validate()
        self.assertEqual(errors, [])

    def test_symlinks_inside_knowledge_root_are_rejected(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "note.md").write_text(
            "---\ntype: runbook\n---\n\n# Note\n",
            encoding="utf-8",
        )
        empty_elsewhere = self.root / "empty-elsewhere"
        empty_elsewhere.mkdir()

        index_file = self.write("index.md", "# Index\n")
        runbook_dir = self.knowledge / "runbook"
        runbook_dir.mkdir(parents=True, exist_ok=True)
        (runbook_dir / "linked.md").symlink_to(outside)
        (runbook_dir / "index.md").symlink_to(index_file)
        (runbook_dir / "broken.md").symlink_to(self.root / "does-not-exist.md")
        (self.knowledge / "symlinked-dir").symlink_to(elsewhere)
        (self.knowledge / "empty-dir-link").symlink_to(empty_elsewhere)
        (self.knowledge / "cycle-link").symlink_to(self.root)

        _, errors = self.validate()
        self.assertEqual(len(errors), 6, errors)
        for expected_path in (
            "knowledge/runbook/linked.md",
            "knowledge/runbook/index.md",
            "knowledge/runbook/broken.md",
            "knowledge/symlinked-dir/note.md",
            "knowledge/empty-dir-link",
            "knowledge/cycle-link",
        ):
            self.assertTrue(
                any(
                    expected_path in err
                    and check_okf_knowledge.MSG_SYMLINK_NOT_ALLOWED in err
                    for err in errors
                ),
                f"missing symlink error for {expected_path}: {errors}",
            )

    def test_git_tracked_broken_symlinks_symlinked_dirs_and_symlinked_roots_are_rejected(
        self,
    ) -> None:
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)

        # 1. Tracked broken .md symlink and tracked symlinked subdirectory inside knowledge/.
        elsewhere = self.root / "elsewhere-target"
        elsewhere.mkdir()
        (elsewhere / "note.md").write_text(
            "---\ntype: runbook\n---\n\n# External note\n",
            encoding="utf-8",
        )
        self.write("index.md", "# Index\n")
        runbook_dir = self.knowledge / "runbook"
        runbook_dir.mkdir(parents=True, exist_ok=True)
        (runbook_dir / "broken.md").symlink_to(self.root / "missing-target.md")
        (self.knowledge / "linked-subdir").symlink_to(elsewhere)

        # 2. Tracked symlinked knowledge/ root pointing to a directory without "knowledge" in its path.
        pkg_dir = self.root / "examples" / "pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        symlink_root = pkg_dir / "knowledge"
        symlink_root.symlink_to(elsewhere)

        subprocess.run(
            [
                "git",
                "add",
                "knowledge/index.md",
                "knowledge/runbook/broken.md",
                "knowledge/linked-subdir",
                "examples/pkg/knowledge",
                "elsewhere-target/note.md",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

        tracked = check_okf_knowledge.git_tracked_paths(self.root)
        roots = check_okf_knowledge.discover_knowledge_roots(self.root, tracked=tracked)
        self.assertEqual(
            roots,
            sorted([self.knowledge, symlink_root]),
            roots,
        )

        all_errors: list[str] = []
        for root in roots:
            _, root_errors = check_okf_knowledge.validate_knowledge_root(
                root,
                display_root=self.root,
                tracked=tracked,
            )
            all_errors.extend(root_errors)

        for expected_path in (
            "knowledge/runbook/broken.md",
            "knowledge/linked-subdir/note.md",
            "examples/pkg/knowledge",
        ):
            self.assertTrue(
                any(
                    expected_path in err
                    and check_okf_knowledge.MSG_SYMLINK_NOT_ALLOWED in err
                    for err in all_errors
                ),
                f"missing git-tracked symlink error for {expected_path}: {all_errors}",
            )

    def test_empty_knowledge_root_fails_loudly(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check_okf_knowledge.main([str(self.knowledge)])
        self.assertEqual(rc, 1)
        self.assertIn(check_okf_knowledge.MSG_NO_KNOWLEDGE_FILES, buf.getvalue())


if __name__ == "__main__":
    unittest.main()

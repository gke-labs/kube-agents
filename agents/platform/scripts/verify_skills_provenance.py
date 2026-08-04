#!/usr/bin/env python3
"""
Lightweight verification script for skill provenance and integrity.
Verifies SHA-256 checksums of all skill files against a build-time manifest.
"""

import argparse
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Set

logger = logging.getLogger("hermes.skills.provenance")


def compute_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def parse_manifest(manifest_path: str) -> Dict[str, str]:
    if not os.path.exists(manifest_path):
        raise RuntimeError(f"CRITICAL SECURITY ALERT: Manifest file not found at {manifest_path}")

    manifest_map: Dict[str, str] = {}
    with open(manifest_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                raise RuntimeError(
                    f"CRITICAL SECURITY ALERT: Malformed line {line_num} in manifest {manifest_path}: {line}"
                )
            expected_hash = parts[0].strip().lower()
            rel_path = parts[1].strip()
            # Strip leading asterisks or ./ from sha256sum output
            if rel_path.startswith("*"):
                rel_path = rel_path[1:]
            if rel_path.startswith("./"):
                rel_path = rel_path[2:]
            manifest_map[rel_path] = expected_hash
    return manifest_map


def verify_provenance(manifest_path: str, target_dir: str) -> bool:
    if not os.path.exists(manifest_path):
        raise RuntimeError(f"CRITICAL SECURITY ALERT: Manifest file not found at {manifest_path}")
    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        raise RuntimeError(f"CRITICAL SECURITY ALERT: Skills directory not found at {target_dir}")

    manifest_map = parse_manifest(manifest_path)
    found_files: Set[str] = set()

    for root, _, files in os.walk(target_dir):
        for filename in files:
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, target_dir)
            if rel_path.startswith("./"):
                rel_path = rel_path[2:]

            # Exclude the manifest file itself if located inside target_dir
            if os.path.abspath(full_path) == os.path.abspath(manifest_path) or filename == "skills_manifest.sha256":
                continue

            found_files.add(rel_path)

            if rel_path not in manifest_map:
                raise RuntimeError(
                    f"CRITICAL SECURITY ALERT: Untracked or unauthorized file detected in skills directory: {rel_path}"
                )

            actual_hash = compute_sha256(full_path)
            expected_hash = manifest_map[rel_path]
            if actual_hash != expected_hash:
                raise RuntimeError(
                    f"CRITICAL SECURITY ALERT: Checksum mismatch for file '{rel_path}' (expected {expected_hash}, got {actual_hash})"
                )

    missing_files = set(manifest_map.keys()) - found_files
    if missing_files:
        missing_list = ", ".join(sorted(missing_files))
        raise RuntimeError(
            f"CRITICAL SECURITY ALERT: Manifest file missing from directory: {missing_list}"
        )

    logger.info("Skill provenance verification successful for %s", target_dir)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Verify skills provenance against SHA-256 manifest.")
    parser.add_argument("--manifest", required=True, help="Path to SHA-256 manifest file.")
    parser.add_argument("--dir", required=True, help="Path to skills directory to verify.")
    args = parser.parse_args()

    try:
        verify_provenance(args.manifest, args.dir)
        print(f"Skill provenance verification passed for {args.dir}")
        return 0
    except Exception as exc:
        sys.stderr.write(f"{exc}\n")
        logger.critical("Skill provenance verification failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

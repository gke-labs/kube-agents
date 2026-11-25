"""CLI entrypoint for deterministic offline PR risk classification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .classifier import classify_changes
from .git_loader import classify_git_range, load_path_pair
from .types import CONCRETE_RISK_ORDER


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m risk_classifier",
        description="Deterministic offline PR risk classifier for Kubernetes GitOps changes.",
    )
    p.add_argument("--repo", default="", help="Path to a local or bare git repository")
    p.add_argument("--base", default="", help="Base git revision (used with --repo)")
    p.add_argument("--head", default="HEAD", help="Head git revision (used with --repo)")
    p.add_argument("--before", default="", help="Before manifest file or directory (or /dev/null)")
    p.add_argument("--after", default="", help="After manifest file or directory (or /dev/null)")
    p.add_argument("--path", default="", help="Optional logical repo-relative path for --before/--after")
    p.add_argument("--format", choices=("json", "summary"), default="json")
    p.add_argument(
        "--max-risk",
        choices=("low", "medium", "high"),
        default="",
        help="Exit 2 if risk_level exceeds this threshold (unknown always exits 2)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repo:
        if not args.base:
            print("error: --base is required with --repo", file=sys.stderr)
            return 1
        result, _ = classify_git_range(Path(args.repo), args.base, args.head)
    elif args.before or args.after:
        changes = load_path_pair(args.before, args.after, logical_path=args.path)
        result = classify_changes(changes)
    else:
        print("error: specify either --repo/--base or --before/--after", file=sys.stderr)
        return 1

    if args.format == "summary":
        print(
            f"risk_level={result.risk_level} blast_radius={result.blast_radius} "
            f"revertability={result.revertability} destructiveness={result.destructiveness} "
            f"supported={result.supported} label={result.risk_label}"
        )
        for r in result.reasons:
            loc = f" ({r.file})" if r.file else ""
            print(f"  - [{r.severity}] {r.code}{loc}: {r.message}")
    else:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))

    if args.max_risk:
        cur = CONCRETE_RISK_ORDER.get(result.risk_level)
        lim = CONCRETE_RISK_ORDER[args.max_risk]
        if cur is None or cur > lim:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

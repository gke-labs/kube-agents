"""CLI entrypoint for deterministic offline PR risk classification."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from .classifier import classify_changes
from .git_loader import classify_git_range, load_path_pair
from .types import CONCRETE_RISK_ORDER, Reason


class _CLIArgumentParser(argparse.ArgumentParser):
    """ArgumentParser exiting 1 on usage errors so exit 2 is reserved for --max-risk."""

    def error(self, message: str) -> NoReturn:
        self.exit(1, f"error: {message}\n")


def _safe_term_str(text: str) -> str:
    """Escape control characters before printing human-readable terminal summaries."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _format_reason_loc(reason: Reason) -> str:
    parts: list[str] = []
    if reason.file:
        parts.append(_safe_term_str(reason.file))
    if reason.resource is not None:
        ref = reason.resource
        target = f"{ref.namespace}/{ref.name}" if ref.namespace else ref.name
        parts.append(_safe_term_str(f"{ref.gvk_label()} {target}".strip()))
    if reason.field_path:
        parts.append(_safe_term_str(f"field={reason.field_path}"))
    return f" ({'; '.join(parts)})" if parts else ""


def _format_cli_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        raw_err = exc.stderr
        if isinstance(raw_err, bytes):
            detail = raw_err.decode("utf-8", errors="replace").strip()
        else:
            detail = str(raw_err or "").strip()
        first_line = detail.splitlines()[0].strip() if detail else f"exit status {exc.returncode}"
        return f"git command failed: {_safe_term_str(first_line)}"
    if isinstance(exc, FileNotFoundError):
        target = exc.filename or str(exc)
        return f"path not found: {_safe_term_str(str(target))}"
    return _safe_term_str(str(exc) or type(exc).__name__)


def build_parser() -> argparse.ArgumentParser:
    p = _CLIArgumentParser(
        prog="python3 -m risk_classifier",
        description="Deterministic offline PR risk classifier for Kubernetes GitOps changes.",
    )
    p.add_argument("--repo", default="", help="Path to a local or bare git repository")
    p.add_argument("--base", default="", help="Base git revision (used with --repo)")
    p.add_argument("--head", default="HEAD", help="Head git revision (used with --repo)")
    p.add_argument(
        "--disable-gitattributes",
        action="store_true",
        help="Temporarily neutralize .gitattributes export-ignore/export-subst during git archive reads",
    )
    p.add_argument("--before", default="", help="Before manifest file or directory (or /dev/null)")
    p.add_argument("--after", default="", help="After manifest file or directory (or /dev/null)")
    p.add_argument("--path", default="", help="Optional logical repo-relative path for --before/--after")
    p.add_argument("--format", choices=("json", "summary"), default="json")
    p.add_argument(
        "--max-risk",
        choices=("low", "medium", "high", "critical"),
        default="",
        help="Exit 2 if risk_level exceeds this threshold (unknown always exits 2)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (1 if code else 0)
    if args.repo and (args.before or args.after):
        print("error: cannot combine --repo with --before/--after", file=sys.stderr)
        return 1
    if not args.repo and args.base:
        print("error: --base requires --repo", file=sys.stderr)
        return 1

    try:
        if args.repo:
            if not args.base:
                print("error: --base is required with --repo", file=sys.stderr)
                return 1
            repo_path = Path(args.repo)
            if not repo_path.exists():
                print(f"error: repository path not found: {_safe_term_str(args.repo)}", file=sys.stderr)
                return 1
            result, _ = classify_git_range(
                repo_path,
                args.base,
                args.head,
                disable_gitattributes=bool(args.disable_gitattributes),
            )
        elif args.before or args.after:
            changes = load_path_pair(args.before, args.after, logical_path=args.path)
            result = classify_changes(changes)
        else:
            print("error: specify either --repo/--base or --before/--after", file=sys.stderr)
            return 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {_format_cli_error(exc)}", file=sys.stderr)
        return 1

    if args.format == "summary":
        st = result.stats
        print(
            f"risk_level={result.risk_level} blast_radius={result.blast_radius} "
            f"revertability={result.revertability} disruptiveness={result.disruptiveness} "
            f"supported={result.supported} effective_changes={result.effective_changes} "
            f"label={result.risk_label}"
        )
        print(
            f"stats: files={st.files_changed} "
            f"resources=+{st.resources_added}/~{st.resources_modified}/-{st.resources_deleted} "
            f"namespaces={st.namespaces_touched} clusters={st.clusters_touched} "
            f"unsupported_files={st.unsupported_files} parse_errors={st.parse_errors}"
        )
        for r in result.reasons:
            loc = _format_reason_loc(r)
            print(f"  - [{r.severity}] {_safe_term_str(r.code)}{loc}: {_safe_term_str(r.message)}")
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

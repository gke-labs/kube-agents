#!/usr/bin/env python3
"""Decision memory: durable infrastructure decisions and lessons, learned from outcomes.

Prototype for docs/designs/memory.md. Harness-neutral by construction:
it imports nothing from Hermes or the A2A stack, talks to Hindsight over its
REST API only, and reads and writes git. Every subcommand prints JSON on stdout
so any harness (a Hermes skill, a mode-next worker, a Kubernetes CronJob, a CI
step) can drive it.

System of record: OKF records in the GitOps repository,
``knowledge/decisions/*.md`` and ``knowledge/lessons/*.md`` (markdown + YAML
frontmatter, ``docs/architecture/06-api-and-data-contracts.md`` §5). The
Hindsight bank is a derived index of them and can be rebuilt from git at any
time.

Subcommands:
  validate  check every record's frontmatter
  index     upsert records into the Hindsight bank (one document per record)
  check     match a change (diff or file list) against recorded decisions and lessons
  why       explain the recorded rationale (or the history) behind a path
  from-outcome  draft a lesson record from a merged fix PR / resolved finding
  dream     nightly pass: verify, detect reverts/supersession/recurrence, reconcile index
  bootstrap harvest historical PRs, issues, and revert commits into initial records
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

# --------------------------------------------------------------------------
# Constants. The bank is separate from the conversational bank on purpose: it is
# a derived index (wipe-and-rebuild from git is a valid recovery), it must not be
# paraphrased by consolidation (identifiers are the point), and it carries a
# different config (no observations).
# --------------------------------------------------------------------------

DEFAULT_BANK_ID = "kube-agents-decisions"
SHARED_TAG = "scope:shared"
RECORD_DIRS = ("knowledge/decisions", "knowledge/lessons")
RECORD_TYPES = ("decision", "lesson")
STATUSES = ("proposed", "active", "superseded", "retired")
SEVERITIES = ("block", "warn", "info")
VALID_OPS = ("==", "!=", ">=", ">", "<=", "<", "absent", "present", "regex")
RECORD_STRATEGY = "record"
DOC_PREFIX = "okf:"
DEFAULT_HTTP_TIMEOUT_SECONDS = 60.0
DEFAULT_PAGE_SIZE = 100
DEFAULT_RECALL_MAX_TOKENS = 2048
MAX_CONTEXT_LENGTH = 200
MAX_SEMANTIC_TEXT_SNIPPET = 160
MAX_COMMIT_BODY_SNIPPET = 600
MAX_SLUG_LENGTH = 60
COMMIT_SHORT_SHA_LENGTH = 12
DEFAULT_VCS_ROOT = "/opt/data/scratch/vcs"
DEFAULT_SCRIPTS_DIRS = ("/opt/defaults/scripts", "/opt/data/scripts")
WITHDRAWN_EVAL_PROBE_TITLE = "[withdrawn-eval-probe]"
DEFAULT_QUOTA_PATH = "spec.hard.requests\\.memory"
DEFAULT_QUOTA_CEILING = "32Gi"
DEFAULT_LLM_MODEL = "model-default"
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0

BANK_CONFIG = {
    # Records are retained verbatim: `chunks` runs no LLM, so an identifier, a
    # threshold or a PR link can never be paraphrased away.
    "retain_strategies": {RECORD_STRATEGY: {"retain_extraction_mode": "chunks"}},
    "retain_default_strategy": RECORD_STRATEGY,
    # No observation layer: synthesis is the dream's job, done explicitly and
    # with provenance, not by background consolidation.
    "enable_observations": False,
    "reflect_mission": (
        "The decision register of a Kubernetes platform team. Each memory is a "
        "recorded infrastructure decision or a lesson learned from a fixed "
        "incident, with its identifier, status and pull request. Answer by citing "
        "record identifiers and pull requests; prefer active records over "
        "superseded ones and say so when only a superseded record matches."
    ),
}


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class Record:
    path: str               # repo-relative path of the markdown file
    meta: Dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def type(self) -> str:
        return str(self.meta.get("type", ""))

    @property
    def status(self) -> str:
        return str(self.meta.get("status", "active"))

    @property
    def title(self) -> str:
        return str(self.meta.get("title", ""))

    @property
    def scope(self) -> Dict[str, List[str]]:
        s = self.meta.get("scope") or {}
        return {k: [str(x) for x in (s.get(k) or [])] for k in ("paths", "resources", "clusters", "checks")}

    @property
    def guard(self) -> Dict[str, Any]:
        return self.meta.get("guard") or {}

    @property
    def evidence(self) -> Dict[str, Any]:
        return self.meta.get("evidence") or {}

    def document_id(self, repo_id: str) -> str:
        return f"{DOC_PREFIX}{repo_id}:{self.path}"

    def tags(self) -> List[str]:
        tags = [SHARED_TAG, f"kind:{self.type}", f"status:{self.status}", f"id:{self.id}"]
        s = self.scope
        tags += [f"cluster:{c}" for c in s["clusters"]]
        tags += [f"check:{c}" for c in s["checks"]]
        tags += [f"res:{r}" for r in s["resources"]]
        if self.guard.get("severity"):
            tags.append(f"guard:{self.guard['severity']}")
        return sorted(set(tags))

    def render_for_index(self) -> str:
        """The text the index stores: self-describing, identifiers in the prose."""
        ev = self.evidence
        lines = [
            f"{self.id} ({self.type}, {self.status}, {self.meta.get('date', '')}): {self.title}",
        ]
        s = self.scope
        if s["paths"] or s["resources"]:
            lines.append("Governs: " + ", ".join(s["resources"] + s["paths"]))
        if s["clusters"]:
            lines.append("Clusters: " + ", ".join(s["clusters"]))
        if self.guard.get("rule"):
            lines.append(f"Guard ({self.guard.get('severity', 'warn')}): {self.guard['rule']}")
        if ev.get("pull_request"):
            lines.append(f"Pull request: {ev['pull_request']}")
        if ev.get("rejected_proposals"):
            lines.append("Rejected proposals: " + ", ".join(str(x) for x in ev["rejected_proposals"]))
        if self.meta.get("supersedes"):
            lines.append("Supersedes: " + ", ".join(self.meta["supersedes"]))
        if self.meta.get("superseded_by"):
            lines.append(f"Superseded by: {self.meta['superseded_by']}")
        lines.append("")
        lines.append(self.body.strip())
        return "\n".join(lines)

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "status": self.status, "title": self.title,
            "path": self.path, "guard": self.guard or None,
            "pull_request": self.evidence.get("pull_request"),
            "breaking_pr": self.evidence.get("breaking_pr"),
            "incident": self.evidence.get("incident"),
            "rejected_proposals": self.evidence.get("rejected_proposals"),
            "supersedes": self.meta.get("supersedes"),
            "superseded_by": self.meta.get("superseded_by"),
            "body": self.body.strip(),
        }


FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def parse_record(text: str, path: str) -> Record:
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path}: no YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter is not a mapping")
    return Record(path=path, meta=meta, body=m.group(2))


def render_record(meta: Dict[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n" + body.strip() + "\n"


def validate_record(rec: Record) -> List[str]:
    if rec.meta.get("_parse_error"):
        return [str(rec.meta["_parse_error"])]
    errs = []
    if rec.type not in RECORD_TYPES:
        errs.append(f"type must be one of {RECORD_TYPES}")
    if not re.fullmatch(r"(DEC|LES)-\d{4}-\d{3,}", rec.id):
        errs.append("id must look like DEC-2026-001 or LES-2026-001")
    if rec.status not in STATUSES:
        errs.append(f"status must be one of {STATUSES}")
    if not rec.title:
        errs.append("title is required")
    s = rec.scope
    if not (s["paths"] or s["resources"] or s["checks"]):
        errs.append("scope needs at least one of paths, resources, checks — an unscoped record can never be matched")
    g = rec.guard
    if g:
        if g.get("severity") and g["severity"] not in SEVERITIES:
            errs.append(f"guard.severity must be one of {SEVERITIES}")
        exp = g.get("expect")
        if exp:
            if not all(k in exp for k in ("file", "path", "op", "value")):
                errs.append("guard.expect needs file, path, op, value")
            else:
                if exp.get("op") not in VALID_OPS:
                    errs.append(f"guard.expect.op must be one of {VALID_OPS}")
                if exp.get("path") != "$text":
                    try:
                        _path_tokens(str(exp["path"]))
                    except ValueError as exc:
                        errs.append(f"guard.expect.path is invalid: {exc}")
    if rec.type == "lesson" and not rec.evidence.get("pull_request"):
        errs.append("a lesson needs evidence.pull_request — it is learned from a merged fix")
    if rec.status == "superseded" and not rec.meta.get("superseded_by"):
        errs.append("a superseded record must name superseded_by")
    return errs


def load_records(repo: Path) -> List[Record]:
    out = []
    for d in RECORD_DIRS:
        base = repo / d
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.md")):
            if p.name.lower() in ("index.md", "readme.md"):
                continue
            rel = str(p.relative_to(repo))
            try:
                out.append(parse_record(p.read_text(), rel))
            except (ValueError, yaml.YAMLError) as exc:
                out.append(Record(path=rel, meta={"_parse_error": str(exc)}, body=""))
    return out


# --------------------------------------------------------------------------
# Guards: a machine-checkable assertion on the GitOps tree.
#   expect: {file: <glob>, path: spec.template.spec.containers[name=app].resources.requests.memory,
#            op: ">=", value: "256Mi"}
# --------------------------------------------------------------------------

_QTY = re.compile(r"^([0-9.]+)\s*([A-Za-z]*)$")
_UNITS = {"": 1, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
          "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


def quantity(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = _QTY.match(str(v).strip())
    if not m or m.group(2) not in _UNITS:
        return None
    return float(m.group(1)) * _UNITS[m.group(2)]


def _path_tokens(path: str) -> List[Tuple[str, Optional[Tuple[str, str]]]]:
    """Split on unescaped dots; `\\.` is a literal dot (ResourceQuota keys such as
    `requests.memory` contain one)."""
    toks = []
    for part in re.split(r"(?<!\\)\.", path):
        part = part.replace("\\.", ".")
        m = re.fullmatch(r"([^\[]+)(?:\[([^=\]]+)=([^\]]+)\])?", part)
        if not m:
            raise ValueError(f"bad guard path segment {part!r}")
        toks.append((m.group(1), (m.group(2), m.group(3)) if m.group(2) else None))
    return toks


def yaml_get(doc: Any, path: str) -> Any:
    cur = doc
    for key, sel in _path_tokens(path):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
        if sel:
            k, v = sel
            if not isinstance(cur, list):
                return None
            cur = next((x for x in cur if isinstance(x, dict) and str(x.get(k)) == v), None)
            if cur is None:
                return None
    return cur


def compare(actual: Any, op: str, expected: Any) -> bool:
    if op == "absent":
        return actual is None
    if op == "present":
        return actual is not None
    if actual is None:
        return False
    if op == "regex":
        return bool(re.search(str(expected), str(actual), re.M | re.S))
    if op in ("==", "!="):
        eq = str(actual) == str(expected)
        return eq if op == "==" else not eq
    a, e = quantity(actual), quantity(expected)
    if a is None or e is None:
        return False
    ops = {">=": a >= e, ">": a > e, "<=": a <= e, "<": a < e}
    return ops.get(op, False)


def _doc_resource_id(doc: Any) -> Optional[str]:
    if isinstance(doc, dict) and doc.get("kind") and isinstance(doc.get("metadata"), dict):
        md = doc["metadata"]
        return f"{doc['kind']}/{md.get('namespace', '')}/{md.get('name', '')}"
    return None


def evaluate_guard(expect: Dict[str, Any], files: Dict[str, str],
                   scope_resources: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Evaluate a guard over {path: content}. One result per matching document."""
    results = []
    op = expect["op"]
    for fpath, content in sorted(files.items()):
        if not fnmatch.fnmatch(fpath, expect["file"]):
            continue
        if expect["path"] == "$text":
            results.append({"file": fpath, "actual": "<text>",
                            "ok": compare(content, op, expect["value"])})
            continue
        try:
            docs = [d for d in yaml.safe_load_all(content) if d is not None]
        except yaml.YAMLError:
            results.append({"file": fpath, "actual": None, "ok": False})
            continue
        if not docs:
            results.append({"file": fpath, "actual": None, "ok": op == "absent"})
            continue
        if len(docs) > 1 and scope_resources:
            scoped = [
                d for d in docs
                if _doc_resource_id(d) and any(fnmatch.fnmatch(_doc_resource_id(d) or "", g) for g in scope_resources)
            ]
            if scoped:
                docs = scoped
        if len(docs) > 1:
            non_null = [d for d in docs if yaml_get(d, expect["path"]) is not None]
            if non_null:
                docs = non_null
        for doc in docs:
            actual = yaml_get(doc, expect["path"])
            if actual is None and op not in ("absent", "present"):
                results.append({"file": fpath, "actual": None, "ok": False})
                continue
            results.append({"file": fpath, "actual": actual,
                            "ok": compare(actual, op, expect["value"])})
    return results


# --------------------------------------------------------------------------
# Matching a change against records (deterministic; Hindsight optional)
# --------------------------------------------------------------------------

def _resources_in(content: str) -> List[str]:
    out = []
    try:
        for d in yaml.safe_load_all(content):
            rid = _doc_resource_id(d)
            if rid:
                out.append(rid)
    except yaml.YAMLError:
        pass
    return out


def _select_precedence_winners(records: List[Record], target_files: Iterable[str]) -> List[Record]:
    """Apply Precedence Rules 3 and 4 when multiple active records guard the same YAML path on a file."""
    grouped: Dict[Tuple[str, str], List[Record]] = {}
    ungrouped: List[Record] = []
    tfiles = list(target_files)
    for rec in records:
        exp = rec.guard.get("expect")
        if not exp:
            ungrouped.append(rec)
            continue
        matched_targets = [f for f in tfiles if fnmatch.fnmatch(f, exp["file"])]
        if not matched_targets:
            ungrouped.append(rec)
            continue
        for mf in matched_targets:
            grouped.setdefault((mf, exp["path"]), []).append(rec)
    suppressed_ids = set()
    for (_mf, _path), group in grouped.items():
        if len(group) <= 1:
            continue
        # Rule 4: narrower cluster scope (non-empty clusters) outranks fleet-wide (empty clusters).
        has_narrow = any(bool(r.scope["clusters"]) for r in group)
        has_broad = any(not r.scope["clusters"] for r in group)
        if has_narrow and has_broad:
            for r in group:
                if not r.scope["clusters"]:
                    suppressed_ids.add(r.id)
    return [r for r in records if r.id not in suppressed_ids]


def match_change(records: Iterable[Record], changed: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    """changed: {path: new content, or None if deleted}. Returns findings."""
    rec_list = [r for r in records if not r.meta.get("_parse_error")]
    by_id = {r.id: r for r in rec_list}
    active_candidates = [r for r in rec_list if r.status in ("active", "proposed")]
    effective_records = _select_precedence_winners(active_candidates, changed.keys())
    findings = []
    new_resources = {p: _resources_in(c) for p, c in changed.items() if c}
    for rec in effective_records:
        s = rec.scope
        hits = set()
        for p in changed:
            if any(fnmatch.fnmatch(p, g) for g in s["paths"]):
                hits.add(p)
            for r in new_resources.get(p, []):
                if any(fnmatch.fnmatch(r, g) for g in s["resources"]):
                    hits.add(p)
        if not hits:
            continue
        verdict = "related"
        detail = []
        exp = rec.guard.get("expect")
        if exp:
            files = {p: c for p, c in changed.items() if c is not None and p in hits}
            detail = evaluate_guard(exp, files, s["resources"])
            deleted = [p for p in hits if changed.get(p) is None and fnmatch.fnmatch(p, exp["file"])]
            if any(not d["ok"] for d in detail) or deleted:
                verdict = "violates"
                detail += [{"file": p, "actual": None, "ok": False, "deleted": True} for p in deleted]
            elif detail:
                verdict = "consistent"
        severity = rec.guard.get("severity", "warn") if verdict == "violates" else "info"
        superseded_records = [by_id[oid].summary() for oid in (rec.meta.get("supersedes") or []) if oid in by_id]
        findings.append({**rec.summary(), "verdict": verdict, "severity": severity,
                         "files": sorted(hits), "guard_results": detail,
                         "superseded_records": superseded_records})
    order = {"block": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["id"]))
    return findings


def render_check_markdown(findings: List[Dict[str, Any]], semantic: List[Dict[str, Any]]) -> str:
    if not findings and not semantic:
        return "No recorded decision or lesson governs the files this change touches."
    lines = ["### Prior decisions and lessons this change touches", ""]
    icon = {"violates": "⛔", "consistent": "✅", "related": "ℹ️"}
    for f in findings:
        ev_parts = []
        if f.get("breaking_pr"):
            ev_parts.append(f"breaking PR: {f['breaking_pr']}")
        if f.get("incident"):
            ev_parts.append(f"incident: {f['incident']}")
        if f.get("pull_request"):
            ev_parts.append(f"learned in: {f['pull_request']}")
        if f.get("rejected_proposals"):
            ev_parts.append("rejected proposals: " + ", ".join(str(x) for x in f["rejected_proposals"]))
        ev_str = f" ({', '.join(ev_parts)})" if ev_parts else ""
        lines.append(f"- {icon[f['verdict']]} **{f['id']}** ({f['type']}, status={f['status']}): {f['title']}{ev_str}")
        if f.get("guard") and f["guard"].get("rule"):
            lines.append(f"  - Guard ({f['guard'].get('severity', 'warn')}): {f['guard']['rule']}")
        for g in f["guard_results"]:
            if not g["ok"]:
                what = "deleted" if g.get("deleted") else f"is now `{g['actual']}`"
                lines.append(f"  - `{g['file']}` {what}")
        if f.get("superseded_records"):
            for sr in f["superseded_records"]:
                lines.append(f"  - Supersedes **{sr['id']}** ({sr['title']}, {sr.get('pull_request') or ''}, incident {sr.get('incident') or ''})")
        if f.get("body"):
            for bline in f["body"].splitlines():
                lines.append(f"  > {bline}")
    if semantic:
        lines += ["", "Possibly related (semantic match, not path-scoped — verify before relying on it):"]
        for s in semantic:
            lines.append(f"- {s.get('id') or '?'}: {s['text'][:160]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------

def _git_bin() -> Optional[str]:
    env_git = os.environ.get("KUBE_AGENTS_LOCAL_GIT")
    if env_git and os.path.isfile(env_git):
        return env_git
    for candidate in ("/opt/vcs/libexec/git", "/opt/vcs/bin/git", "/usr/bin/git"):
        if os.path.isfile(candidate):
            return candidate
    return None


def _ensure_scripts_on_path() -> None:
    resolved = Path(__file__).resolve()
    candidates = [Path("/opt/defaults/scripts"), Path("/opt/data/scripts")]
    if len(resolved.parents) > 3:
        candidates.insert(0, resolved.parents[3] / "scripts")
    for candidate in candidates:
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.append(str(candidate))


def _resolve_sandbox_git_repo(repo: Path) -> Path:
    vcs_root = Path(os.environ.get("KUBE_AGENTS_VCS_ROOT", DEFAULT_VCS_ROOT))
    if str(repo).startswith(str(vcs_root)):
        return repo
    try:
        _ensure_scripts_on_path()
        import sandbox_exec  # type: ignore
        res = sandbox_exec.run(
            ["/bin/sh", "-c", f"ls -d {vcs_root}/* 2>/dev/null | head -n 1"],
            check=False,
        )
        cand = (res.stdout or "").strip()
        if cand:
            return Path(cand)
    except Exception:
        pass
    return repo


def git(repo: Path, *args: str) -> str:
    local_bin = _git_bin()
    if local_bin:
        return subprocess.run(
            [local_bin, "-c", "safe.directory=*", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    _ensure_scripts_on_path()
    import sandbox_exec  # type: ignore
    srepo = _resolve_sandbox_git_repo(repo)
    res = sandbox_exec.run(
        ["/opt/vcs/libexec/git", "-c", "safe.directory=*", "-C", str(srepo), *args],
        check=True,
    )
    return res.stdout


def changed_files(repo: Path, base: str, head: str = "HEAD") -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for line in git(repo, "diff", "--name-status", f"{base}...{head}").splitlines():
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if status.startswith("D"):
            out[path] = None
        else:
            out[path] = git(repo, "show", f"{head}:{path}")
    return out


def _file_exists_on_head(repo: Path, rel_path: str, tracked: Optional[set] = None) -> bool:
    if (repo / rel_path).exists():
        return True
    if tracked is not None:
        return rel_path in tracked
    try:
        return bool(git(repo, "ls-files", "--error-unmatch", rel_path).strip())
    except subprocess.CalledProcessError:
        return False


def _list_repo_files(repo: Path) -> List[str]:
    if (repo / ".git").exists():
        try:
            return git(repo, "ls-files").splitlines()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return [
        p.relative_to(repo).as_posix()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    ]


def worktree_files(repo: Path, globs: Iterable[str]) -> Dict[str, str]:
    files = {}
    for p in _list_repo_files(repo):
        if any(fnmatch.fnmatch(p, g) for g in globs):
            if (repo / p).exists():
                files[p] = (repo / p).read_text()
            elif (repo / ".git").exists():
                try:
                    files[p] = git(repo, "show", f"HEAD:{p}")
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pass
    return files


PR_REF = re.compile(
    r"\([#!]\s*(\d+)\)"
    r"|Merge pull request #(\d+)"
    r"|See merge request [\w./-]*!(\d+)"
    r"|Merged in [^\n(]*\(pull request #(\d+)\)",
    re.I,
)


def _match_pr_num(text: str) -> Optional[str]:
    m = PR_REF.search(text)
    if not m:
        return None
    return next((g for g in m.groups() if g), None)


def history(repo: Path, path: str, limit: int = 10) -> List[Dict[str, Any]]:
    if not (repo / ".git").exists():
        return []
    fmt = "%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"
    try:
        raw = git(repo, "log", f"-n{limit}", f"--format={fmt}", "--", path)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    out = []
    for entry in raw.split("\x1e"):
        entry = entry.strip("\n")
        if not entry:
            continue
        sha, author, date, subject, body = (entry.split("\x1f") + [""] * 5)[:5]
        out.append({"commit": sha[:12], "author": author, "date": date, "subject": subject,
                    "pull_request": _match_pr_num(subject + "\n" + body),
                    "body": body.strip()[:600]})
    return out


# --------------------------------------------------------------------------
# Hindsight REST client (stdlib only)
# --------------------------------------------------------------------------

class Hindsight:
    def __init__(self, url: str, bank: str = DEFAULT_BANK_ID, timeout: float = 60.0):
        self.base = url.rstrip("/") + "/v1/default/banks/" + urllib.parse.quote(bank, safe="")
        self.bank = bank
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Any = None, query: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def ensure_bank(self) -> None:
        if self.bank == DEFAULT_BANK_ID:
            self._req("PUT", "", {"mission": BANK_CONFIG["reflect_mission"]})
            self._req("PATCH", "/config", {"updates": BANK_CONFIG})
        else:
            cur = self._req("GET", "/config") or {}
            cfg = cur.get("config") if isinstance(cur, dict) and isinstance(cur.get("config"), dict) else cur
            strats = dict((cfg.get("retain_strategies") if isinstance(cfg, dict) else None) or {})
            strats.update(BANK_CONFIG["retain_strategies"])
            self._req("PATCH", "/config", {"updates": {"retain_strategies": strats}})

    def retain(self, rec: Record, repo_id: str) -> None:
        text = rec.render_for_index()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        item = {
            "content": text,
            "document_id": rec.document_id(repo_id),
            "update_mode": "replace",
            "context": f"{rec.id}: {rec.title}"[:200],
            "tags": rec.tags(),
            "metadata": {"id": rec.id, "type": rec.type, "status": rec.status, "path": rec.path,
                         "pull_request": str(rec.evidence.get("pull_request") or ""),
                         "sha256": sha},
            "strategy": RECORD_STRATEGY,
        }
        if rec.meta.get("date"):
            item["timestamp"] = f"{rec.meta['date']}T00:00:00Z"
        self._req("POST", "/memories", {"items": [item], "async": False})

    def documents(self) -> List[Dict[str, Any]]:
        out, offset = [], 0
        while True:
            page = self._req("GET", "/documents", query={"limit": 100, "offset": offset,
                                                         "tags": SHARED_TAG, "tags_match": "any_strict"})
            items = page.get("items", []) if isinstance(page, dict) else page
            out += items
            if len(items) < 100:
                return out
            offset += 100

    def delete_document(self, doc_id: str) -> None:
        self._req("DELETE", "/documents/" + urllib.parse.quote(doc_id, safe=""))

    def recall(self, query: str, tag_groups: Optional[List[Dict[str, Any]]] = None,
               budget: str = "mid", max_tokens: int = 2048) -> List[Dict[str, Any]]:
        if os.environ.get("NATS_URL"):
            curator_bin = shutil.which("curator") or (
                "/usr/local/bin/curator" if os.path.exists("/usr/local/bin/curator") else None
            )
            if curator_bin:
                flat_tags: List[str] = []
                if tag_groups:
                    for g in tag_groups:
                        for t in g.get("tags") or []:
                            if t not in flat_tags:
                                flat_tags.append(t)
                else:
                    flat_tags = [SHARED_TAG]
                try:
                    proc = subprocess.run(
                        [curator_bin, "recall", "--bank", self.bank, "--tags", ",".join(flat_tags), query],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        payload = json.loads(proc.stdout)
                        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
                            return list(payload["results"])
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass

        body: Dict[str, Any] = {"query": query, "budget": budget, "max_tokens": max_tokens,
                                "types": ["world", "experience"]}
        if tag_groups:
            body["tag_groups"] = tag_groups
        else:
            body.update({"tags": [SHARED_TAG], "tags_match": "any_strict"})
        resp = self._req("POST", "/memories/recall", body)
        return list((resp or {}).get("results") or [])


def _id_from_tags(tags: Iterable[str]) -> Optional[str]:
    for t in tags or []:
        if t.startswith("id:"):
            return t[3:]
    return None


def semantic_related(hs: Hindsight, query: str, exclude: Iterable[str]) -> List[Dict[str, Any]]:
    """Active records related by meaning, for paths no record scopes yet."""
    groups = [{"tags": [SHARED_TAG], "match": "all_strict"},
              {"tags": ["status:active"], "match": "all_strict"}]
    seen, out = set(exclude), []
    for r in hs.recall(query, tag_groups=groups, budget="low"):
        rid = _id_from_tags(r.get("tags") or [])
        if rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "text": r.get("text", ""), "document_id": r.get("document_id")})
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _parse_remote_url_identity(url: str) -> Optional[str]:
    url = url.strip()
    if not url:
        return None
    if "://" in url:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.strip("/")
    elif ":" in url:
        m = re.match(r"^(?:[^@]+@)?[^:/]+:(.+)$", url)
        path = m.group(1).strip("/") if m else ""
    else:
        path = url.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if path.startswith("scm/"):
        path = path[4:]
    return path if "/" in path else None


def repo_identity(repo: Path) -> str:
    resolved = repo.resolve()
    vcs_root = Path(os.environ.get("KUBE_AGENTS_VCS_ROOT", "/opt/data/scratch/vcs"))
    sessions_dir = vcs_root / ".sessions"
    if sessions_dir.is_dir():
        for sf in sessions_dir.glob("*.json"):
            try:
                sdata = json.loads(sf.read_text())
                if Path(sdata.get("path", "")).resolve() == resolved:
                    raw_ident = str(sdata.get("repo") or sdata.get("spec") or "").strip()
                    ident = _parse_remote_url_identity(raw_ident) or raw_ident
                    if ident:
                        return str(ident)
            except Exception:
                continue
    try:
        url = git(repo, "remote", "get-url", "origin").strip()
        ident = _parse_remote_url_identity(url)
        if ident:
            return ident
    except subprocess.CalledProcessError:
        pass
    return resolved.name


def _has_knowledge_dir(p: Path) -> bool:
    return p.is_dir() and any((p / d).is_dir() for d in RECORD_DIRS)


def _resolve_repo_dir(repo_arg: str) -> Path:
    cand = Path(repo_arg)
    if _has_knowledge_dir(cand):
        return cand
    vcs_root = Path(os.environ.get("KUBE_AGENTS_VCS_ROOT", "/opt/data/scratch/vcs"))
    ident = _parse_remote_url_identity(repo_arg)
    if not ident and cand.is_dir() and (cand / ".git").exists():
        try:
            ident = _parse_remote_url_identity(git(cand, "remote", "get-url", "origin").strip())
        except Exception:
            ident = None
    if ident and "/" in ident and vcs_root.is_dir():
        owner, name = ident.split("/", 1)
        for match in sorted(vcs_root.glob(f"*__{owner}__{name}__*")):
            if _has_knowledge_dir(match):
                return match
    vcs_script = Path("/opt/data/profiles/platform/skills/version-control/scripts/vcs.py")
    if ident and "/" in ident and vcs_script.is_file():
        try:
            res = subprocess.run(
                [sys.executable, str(vcs_script), "clone", f"https://github.com/{ident}", "--force"],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                cloned = Path(data.get("path", ""))
                if _has_knowledge_dir(cloned):
                    return cloned
        except Exception:
            pass
    if vcs_root.is_dir() and (not cand.is_dir() or repo_arg in (".", "/opt/data")):
        for match in sorted(vcs_root.glob("*__*__*__*")):
            if _has_knowledge_dir(match):
                return match
    return cand


def cmd_validate(a) -> Dict[str, Any]:
    repo = _resolve_repo_dir(a.repo)
    problems, ids = {}, {}
    records = load_records(repo)
    for rec in records:
        errs = validate_record(rec)
        if rec.id in ids:
            errs.append(f"duplicate id, also in {ids[rec.id]}")
        ids[rec.id] = rec.path
        if errs:
            problems[rec.path] = errs
    known = set(ids)
    for rec in records:
        for ref in list(rec.meta.get("supersedes") or []) + ([rec.meta["superseded_by"]] if rec.meta.get("superseded_by") else []):
            if ref not in known:
                problems.setdefault(rec.path, []).append(f"references unknown record {ref}")
    return {"records": len(records), "ok": not problems, "problems": problems}


def cmd_index(a) -> Dict[str, Any]:
    repo = _resolve_repo_dir(a.repo)
    repo_id = a.repo_id or repo_identity(repo)
    hs = Hindsight(a.hindsight_url, a.bank)
    hs.ensure_bank()
    records = [r for r in load_records(repo) if not validate_record(r)]
    wanted = {r.document_id(repo_id): r for r in records}
    existing = {d["id"]: d for d in hs.documents() if str(d.get("id", "")).startswith(f"{DOC_PREFIX}{repo_id}:")}
    upserted, unchanged, deleted = [], [], []
    for doc_id, rec in wanted.items():
        cur = existing.get(doc_id)
        if cur and sorted(cur.get("tags") or []) == rec.tags() and not a.force:
            text = rec.render_for_index()
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            meta = cur.get("document_metadata") or {}
            stored_sha = meta.get("sha256")
            content_matches = (stored_sha == sha) if stored_sha else ((cur.get("text_length") or 0) == len(text))
            if meta.get("status") == rec.status and content_matches:
                unchanged.append(rec.id)
                continue
        hs.retain(rec, repo_id)
        upserted.append(rec.id)
    for doc_id in existing:
        if doc_id not in wanted:
            hs.delete_document(doc_id)
            deleted.append(doc_id)
    return {"bank": a.bank, "repo": repo_id, "upserted": upserted, "unchanged": unchanged, "deleted": deleted}


def _load_changed(a, repo: Path) -> Dict[str, Optional[str]]:
    if a.base:
        return changed_files(repo, a.base, a.head)
    changed: Dict[str, Optional[str]] = {}
    for p in a.files or []:
        fp = repo / p
        changed[p] = fp.read_text() if fp.exists() else None
    return changed


def cmd_check(a) -> Dict[str, Any]:
    repo = _resolve_repo_dir(a.knowledge_repo or a.repo)
    records = [r for r in load_records(repo) if not validate_record(r)]
    changed = _load_changed(a, _resolve_repo_dir(a.repo))
    findings = match_change(records, changed)
    semantic: List[Dict[str, Any]] = []
    if a.hindsight_url and a.semantic:
        query = a.semantic if isinstance(a.semantic, str) and a.semantic != "auto" else \
            "Changes to: " + ", ".join(sorted(changed))
        try:
            semantic = semantic_related(Hindsight(a.hindsight_url, a.bank), query,
                                        exclude=[f["id"] for f in findings])[: a.semantic_limit]
        except (urllib.error.URLError, OSError) as e:
            semantic = [{"id": None, "text": f"semantic search unreachable: {e}", "document_id": None}]
    verdict = "block" if any(f["severity"] == "block" for f in findings) else \
        "warn" if any(f["severity"] == "warn" for f in findings) else "ok"
    status = "found" if (findings or semantic) else "no_match"
    return {"status": status, "verdict": verdict, "changed": sorted(changed), "findings": findings, "semantic": semantic,
            "markdown": render_check_markdown(findings, semantic)}


def _format_citation(rec_summary: Dict[str, Any]) -> str:
    parts = [f"{rec_summary['id']} ({rec_summary.get('path', '')})"]
    if rec_summary.get("pull_request"):
        parts.append(f"PR: {rec_summary['pull_request']}")
    if rec_summary.get("rejected_proposals"):
        rej = ", ".join(str(x) for x in rec_summary["rejected_proposals"])
        parts.append(f"rejected PRs: {rej}")
    return " — ".join(parts)


def _why_single_path(repo: Path, target: str, records: List[Record], limit: int) -> Dict[str, Any]:
    content = (repo / target).read_text() if (repo / target).exists() else None
    resources = _resources_in(content) if content else []
    governing = []
    for rec in records:
        s = rec.scope
        if any(fnmatch.fnmatch(target, g) for g in s["paths"]) or \
                any(fnmatch.fnmatch(r, g) for r in resources for g in s["resources"]):
            governing.append(rec.summary() | {"date": str(rec.meta.get("date", ""))})
    governing.sort(key=lambda r: (r["status"] != "active", r["date"]), reverse=False)
    hist = history(repo, target, limit)
    uncovered = [h for h in hist if h["pull_request"] and not any(
        bool(re.search(rf"(?:[#!/]|^){re.escape(h['pull_request'])}(?:\b|\)|$)", str(g.get("pull_request") or "")))
        for g in governing)]
    return {"status": "found" if governing else "no_match",
            "path": target, "resources": resources,
            "citations": [_format_citation(g) for g in governing],
            "records": governing, "history": hist,
            "history_without_record": uncovered,
            "note": ("Recorded rationale exists; you MUST cite the record IDs (e.g. LES-..., DEC-...) and their PR / rejected_proposals links in your answer and kanban_complete result." if governing else
                     "No recorded decision governs this path. The history is evidence of what changed, "
                     "not of why — say so, and offer to draft a decision record from it.")}


def cmd_why(a) -> Dict[str, Any]:
    repo = _resolve_repo_dir(a.repo)
    raw_paths = list(getattr(a, "path", None) or []) + list(getattr(a, "flag_paths", None) or [])
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    targets = [str(p).strip() for p in raw_paths if str(p).strip()]
    if not targets:
        raise ValueError("why requires at least one path")
    records = [r for r in load_records(repo) if not validate_record(r)]
    limit = getattr(a, "limit", 10)
    if len(targets) == 1:
        return _why_single_path(repo, targets[0], records, limit)
    per_path = [_why_single_path(repo, t, records, limit) for t in targets]
    combined_records = []
    seen_ids = set()
    for item in per_path:
        for rec in item["records"]:
            if rec["id"] not in seen_ids:
                seen_ids.add(rec["id"])
                combined_records.append(rec)
    any_found = any(item["status"] == "found" for item in per_path)
    return {
        "status": "found" if any_found else "no_match",
        "paths": targets,
        "citations": [_format_citation(g) for g in combined_records],
        "records": combined_records,
        "results": per_path,
        "note": (
            "Recorded rationale exists; you MUST cite the record IDs (e.g. LES-..., DEC-...) and their PR / rejected_proposals links in your answer and kanban_complete result."
            if any_found
            else "No recorded decision governs these paths."
        ),
    }


def _next_id(repo: Path, kind: str, year: int) -> str:
    prefix = "LES" if kind == "lesson" else "DEC"
    n = 0
    for rec in load_records(repo):
        m = re.fullmatch(rf"{prefix}-{year}-(\d+)", rec.id)
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}-{year}-{n + 1:03d}"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _resolve_llm_config(a: Any) -> Tuple[Optional[str], str, str]:
    """Resolve LiteLLM gateway base URL, model, and API key for OKF synthesis."""
    if getattr(a, "no_llm", False):
        return None, "", ""
    base = (
        getattr(a, "llm_url", None)
        or os.environ.get("DECISION_MEMORY_LLM_URL")
        or os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip()
    model = (
        getattr(a, "llm_model", None)
        or os.environ.get("DECISION_MEMORY_LLM_MODEL")
        or os.environ.get("LITELLM_MODEL")
        or os.environ.get("API_SERVER_MODEL_NAME")
        or ""
    ).strip()
    api_key = (
        os.environ.get("DECISION_MEMORY_LLM_API_KEY")
        or os.environ.get("LITELLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not base or not model:
        for cfg_dir in filter(None, [os.environ.get("HERMES_HOME"), "/opt/data", "/opt/defaults"]):
            cfg_path = Path(cfg_dir) / "config.yaml"
            if cfg_path.is_file():
                try:
                    cfg = yaml.safe_load(cfg_path.read_text()) or {}
                    m_cfg = cfg.get("model") or {}
                    if not base and m_cfg.get("base_url"):
                        base = str(m_cfg["base_url"]).strip()
                    if not model and (m_cfg.get("model") or m_cfg.get("default")):
                        model = str(m_cfg.get("model") or m_cfg.get("default")).strip()
                    if not api_key and m_cfg.get("api_key"):
                        api_key = str(m_cfg["api_key"]).strip()
                    break
                except Exception:
                    pass
    if not base and os.environ.get("INFERENCE_GATEWAY_SERVICE_HOST"):
        base = "http://inference-gateway/v1"
    return (base.rstrip("/") if base else None), (model or DEFAULT_LLM_MODEL), (api_key or "none")


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _guard_passes_on_head(repo: Path, guard: Dict[str, Any], resources: List[str]) -> bool:
    """Verify that a candidate guard passes against the current repository worktree."""
    exp = (guard or {}).get("expect")
    if not isinstance(exp, dict):
        return False
    if not all(k in exp for k in ("file", "path", "op", "value")):
        return False
    if exp.get("op") not in VALID_OPS:
        return False
    if exp.get("path") != "$text":
        try:
            _path_tokens(str(exp["path"]))
        except ValueError:
            return False
    matched_files = worktree_files(repo, [str(exp["file"])])
    if not matched_files:
        return False
    results = evaluate_guard(exp, matched_files, resources)
    return bool(results) and all(r["ok"] for r in results)


def _llm_synthesize_record(
    llm_url: Optional[str],
    llm_model: str,
    llm_api_key: str,
    record_type: str,
    context_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Call an OpenAI-compatible chat completions endpoint to synthesize an OKF record."""
    if not llm_url:
        return None
    if record_type == "decision":
        schema_hint = (
            '{"title": "<concise decision title>", '
            '"context": "<why the decision was needed>", '
            '"decision": "<the architectural rule and trade-offs>", '
            '"consequences": "<how future changes must comply>", '
            '"guard": {"rule": "<1-line invariant>", "severity": "block", '
            '"expect": {"file": "<path>", "path": "<yaml_path_or_$text>", "op": "<op>", "value": "<val>"}}}'
        )
    else:
        schema_hint = (
            '{"title": "<concise lesson title>", '
            '"root_cause": "<what happened and why it failed>", '
            '"fix": "<what change resolved it>", '
            '"verification": "<how resolution was verified>", '
            '"prevention": "<rule to prevent repeat outage>", '
            '"guard": {"rule": "<1-line invariant>", "severity": "block", '
            '"expect": {"file": "<path>", "path": "<yaml_path_or_$text>", "op": "<op>", "value": "<val>"}}}'
        )
    prompt = (
        "You are synthesizing a Kubernetes GitOps Operational Knowledge Format (OKF) "
        f"{record_type} record from pull request and incident history.\n"
        "Preserve all exact identifiers, PR/issue numbers, file paths, and resource quantities.\n"
        "Return ONLY valid JSON matching this schema (omit 'guard' if no machine-checkable invariant applies):\n"
        f"{schema_hint}\n\n"
        f"Input evidence:\n{json.dumps(context_payload, indent=2, default=str)}"
    )
    body = json.dumps({
        "model": llm_model,
        "messages": [
            {"role": "system", "content": "Respond with a single JSON object only."},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{llm_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm_api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_LLM_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
        return _extract_json_object(str(content))
    except Exception:
        return None


def cmd_from_outcome(a) -> Dict[str, Any]:
    """Draft a lesson from a merged fix and its verified resolution.

    Input JSON (from the outcome recorder; harness-neutral):
      {pull_request, title, merged_at, merged_by, commit, files[], cluster, check, finding_id,
       root_cause, fix, prevention, guard{rule, severity, expect{...}}, resolution{verified_by, at}}
    """
    o = json.loads(Path(a.outcome).read_text())
    repo = Path(a.repo)
    llm_url, llm_model, llm_key = _resolve_llm_config(a)
    merged = str(o.get("merged_at") or _dt.date.today().isoformat())[:10]
    rid = _next_id(repo, "lesson", int(merged[:4]))
    scope = {"paths": sorted(set(o.get("files") or []))}
    if o.get("resources"):
        scope["resources"] = o["resources"]
    if o.get("cluster"):
        scope["clusters"] = [o["cluster"]]
    if o.get("check"):
        scope["checks"] = [o["check"]]

    synth = _llm_synthesize_record(llm_url, llm_model, llm_key, "lesson", o) if llm_url else None
    title = str((synth or {}).get("title") or o.get("title") or "").strip()
    root_cause = str((synth or {}).get("root_cause") or o.get("root_cause") or o.get("pr_body") or "").strip()
    fix_text = str((synth or {}).get("fix") or o.get("fix") or "").strip()
    ver_text = str(
        (synth or {}).get("verification")
        or (o.get("resolution") or {}).get("summary")
        or "The finding did not recur after the merge."
    ).strip()
    prev_text = str((synth or {}).get("prevention") or o.get("prevention") or "").strip()

    meta: Dict[str, Any] = {
        "type": "lesson", "id": rid, "title": title, "status": "active", "date": merged,
        "scope": scope,
    }
    guard = o.get("guard")
    if not guard and isinstance((synth or {}).get("guard"), dict):
        cand_guard = synth["guard"]
        if _guard_passes_on_head(repo, cand_guard, scope.get("resources") or []):
            guard = cand_guard
    if guard:
        meta["guard"] = guard
    meta["evidence"] = {k: v for k, v in {
        "pull_request": o.get("pull_request"), "commit": o.get("commit"),
        "merged_by": o.get("merged_by"), "finding_id": o.get("finding_id"),
        "resolution": o.get("resolution"), "incident": o.get("incident"),
    }.items() if v}
    body = "\n".join([
        "## What happened", "", root_cause, "",
        "## What fixed it", "", fix_text, "",
        "## How we know it worked", "", ver_text, "",
        "## Prevent it next time", "", prev_text,
    ])
    rel = f"knowledge/lessons/{rid}-{slug(title)}.md"
    text = render_record(meta, body)
    errs = validate_record(parse_record(text, rel))
    if a.write and not errs:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    return {"id": rid, "path": rel, "valid": not errs, "problems": errs, "written": bool(a.write and not errs),
            "llm_synthesized": bool(synth), "content": text}


def _reverted(repo: Path, commit: str) -> Optional[Dict[str, str]]:
    if not commit:
        return None
    try:
        out = git(repo, "log", "--format=%H%x1f%s", f"--grep=This reverts commit {commit}", "-n1")
    except subprocess.CalledProcessError:
        return None
    if out.strip():
        sha, subject = out.strip().split("\x1f", 1)
        return {"commit": sha[:12], "subject": subject}
    return None


def cmd_dream(a) -> Dict[str, Any]:
    """The nightly pass. Deterministic checks first; nothing here asks a model.

    1. Verify every active record against the repository as it stands:
       - its scoped paths still exist (else: obsolete candidate)
       - its guard still holds on the tree (else: at-risk — the lesson is being unlearned)
       - its fix commit was not reverted
    2. Supersession hygiene: a record superseded by another must say so on both sides.
    3. Recurrence: lessons sharing a check or a guarded field across clusters
       are promotion candidates (a policy, not another lesson).
    4. Coverage: recent changes to governed infrastructure paths with no record (harvest queue).
    5. Reconcile the Hindsight index with git (optional, with --hindsight-url).
    Output is a report plus proposed record edits; edits land through a PR, never directly.
    """
    repo = Path(a.repo)
    records = load_records(repo)
    by_id = {r.id: r for r in records}
    all_files = _list_repo_files(repo)
    report: Dict[str, Any] = {"date": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                              "verified": [], "at_risk": [], "obsolete": [], "reverted": [],
                              "supersession": [], "conflicts": [], "recurring": [], "uncovered_changes": [],
                              "proposed_edits": []}
    for rec in records:
        if rec.status != "active" or validate_record(rec):
            continue
        s = rec.scope
        if s["paths"] and not any(fnmatch.fnmatch(f, g) for f in all_files for g in s["paths"]):
            report["obsolete"].append({"id": rec.id, "reason": "no file matches its scope any more"})
            report["proposed_edits"].append({"id": rec.id, "path": rec.path, "set": {"status": "retired"},
                                             "why": "governed paths are gone"})
            continue
        exp = rec.guard.get("expect")
        if exp:
            results = evaluate_guard(exp, worktree_files(repo, [exp["file"]]), rec.scope["resources"])
            bad = [r for r in results if not r["ok"]]
            if bad:
                report["at_risk"].append({"id": rec.id, "title": rec.title, "violations": bad,
                                          "guard": rec.guard.get("rule"),
                                          "introduced_by": [history(repo, b["file"], 1)[0] for b in bad
                                                            if history(repo, b["file"], 1)]})
                continue
        rv = _reverted(repo, str(rec.evidence.get("commit") or ""))
        if rv:
            report["reverted"].append({"id": rec.id, "revert": rv})
            continue
        report["verified"].append(rec.id)
    for rec in records:
        for old in rec.meta.get("supersedes") or []:
            o = by_id.get(old)
            if o and (o.status != "superseded" or o.meta.get("superseded_by") != rec.id):
                report["supersession"].append({"id": old, "superseded_by": rec.id, "current_status": o.status})
                report["proposed_edits"].append({"id": old, "path": o.path,
                                                 "set": {"status": "superseded", "superseded_by": rec.id},
                                                 "why": f"{rec.id} declares it supersedes {old}"})
    # Conflicts: two active records guarding the same field with different
    # expectations, neither superseding the other. Recall ranking must never be
    # what decides which one is "the fact" — precedence has to be written down.
    guarded: Dict[Tuple[str, str], List[Record]] = {}
    for rec in records:
        exp = rec.guard.get("expect")
        if rec.status == "active" and exp:
            guarded.setdefault((exp["file"], exp["path"]), []).append(rec)
    for (fglob, fpath), recs in sorted(guarded.items()):
        exps = {(r.guard["expect"]["op"], str(r.guard["expect"]["value"])) for r in recs}
        if len(recs) > 1 and len(exps) > 1:
            linked = any(o.id in (r.meta.get("supersedes") or []) for r in recs for o in recs)
            if not linked:
                newest = max(recs, key=lambda r: str(r.meta.get("date", "")))
                report["conflicts"].append({
                    "file": fglob, "path": fpath,
                    "records": [{"id": r.id, "date": str(r.meta.get("date", "")),
                                 "expect": f"{r.guard['expect']['op']} {r.guard['expect']['value']}"}
                                for r in recs],
                    "proposal": f"newest is {newest.id}; propose it supersede the others, or narrow their scopes"})
    groups: Dict[str, List[Record]] = {}
    for rec in records:
        if rec.type != "lesson":
            continue
        keys = [f"check:{c}" for c in rec.scope["checks"]]
        exp = rec.guard.get("expect")
        if exp:
            keys.append(f"field:{exp['path']}")
        for k in keys:
            groups.setdefault(k, []).append(rec)
    for k, recs in sorted(groups.items()):
        clusters = sorted({c for r in recs for c in r.scope["clusters"]})
        if len(recs) >= a.recurrence_threshold:
            report["recurring"].append({"key": k, "lessons": [r.id for r in recs], "clusters": clusters,
                                        "proposal": "promote to a fleet-wide decision with a policy check "
                                                    "(policy/), instead of one lesson per cluster"})
    governed = [g for r in records for g in r.scope["paths"]]
    since = (_dt.date.today() - _dt.timedelta(days=a.since_days)).isoformat()
    known_prs = {str(r.evidence.get("pull_request") or "").rstrip("/").rsplit("/", 1)[-1] for r in records}
    for line in git(repo, "log", f"--since={since}", "--name-only", "--format=@@%H%x1f%s").split("@@"):
        if not line.strip():
            continue
        head, *files = line.strip().split("\n")
        sha, subject = head.split("\x1f", 1)
        if any(f.startswith("knowledge/") for f in files):
            continue  # the change carried its own record
        infra = [f for f in files if f and any(fnmatch.fnmatch(f, g) for g in a.infra_glob)
                 and not f.startswith("knowledge/")]
        if not infra:
            continue
        pr = _match_pr_num(subject)
        if pr and pr in known_prs:
            continue
        if any(fnmatch.fnmatch(f, g) for f in infra for g in governed) or a.include_ungoverned:
            report["uncovered_changes"].append({"commit": sha[:12], "subject": subject, "pull_request": pr,
                                                "files": infra})
    if a.hindsight_url:
        try:
            ns = argparse.Namespace(repo=a.repo, repo_id=a.repo_id, hindsight_url=a.hindsight_url,
                                    bank=a.bank, force=False)
            report["index"] = cmd_index(ns)
        except (urllib.error.URLError, OSError) as e:
            report["index"] = {"error": f"unreachable: {e}"}
    report["silent"] = not any(report[k] for k in ("at_risk", "obsolete", "reverted", "supersession",
                                                   "conflicts", "recurring", "uncovered_changes"))
    return report


def apply_edits(repo: Path, edits: List[Dict[str, Any]]) -> List[str]:
    """Apply a dream's proposed edits to the working tree (to be committed in a PR)."""
    touched = []
    for e in edits:
        p = repo / e["path"]
        rec = parse_record(p.read_text(), e["path"])
        rec.meta.update(e["set"])
        p.write_text(render_record(rec.meta, rec.body))
        touched.append(e["path"])
    return touched


# --------------------------------------------------------------------------
# First Two Hours Bootstrap Harvester
# --------------------------------------------------------------------------

def _flatten_yaml_leaves(obj: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            safe_k = str(k).replace(".", "\\.")
            sub = f"{prefix}.{safe_k}" if prefix else safe_k
            if isinstance(v, (dict, list)):
                out.update(_flatten_yaml_leaves(v, sub))
            else:
                out[sub] = v
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            if isinstance(item, dict) and "name" in item:
                sel = f"{prefix}[name={item['name']}]"
                for k, v in item.items():
                    if k == "name":
                        continue
                    safe_k = str(k).replace(".", "\\.")
                    sub = f"{sel}.{safe_k}"
                    if isinstance(v, (dict, list)):
                        out.update(_flatten_yaml_leaves(v, sub))
                    else:
                        out[sub] = v
            elif not isinstance(item, (dict, list)):
                out[f"{prefix}[{idx}]"] = item
    return out


def _infer_guard_from_commit(repo: Path, commit_sha: str, target_file: str) -> Optional[Dict[str, Any]]:
    try:
        old_raw = git(repo, "show", f"{commit_sha}^:{target_file}")
        new_raw = git(repo, "show", f"{commit_sha}:{target_file}")
    except subprocess.CalledProcessError:
        return None

    if target_file.endswith(".tf"):
        m_named = re.search(r'"([A-Za-z0-9_-]+)"\s*\{[\s\S]*?\b(max_surge|max_unavailable|node_count|min_node_count|max_node_count)\s*=\s*(\d+)', new_raw)
        if m_named:
            res_name, key_name, val = m_named.group(1), m_named.group(2), m_named.group(3)
            return {
                "rule": f"Keep {res_name} {key_name} = {val} in {target_file}.",
                "severity": "block",
                "expect": {
                    "file": target_file,
                    "path": "$text",
                    "op": "regex",
                    "value": rf"{re.escape(res_name)}[\s\S]*?{re.escape(key_name)}\s*=\s*{val}\b",
                },
            }
        m = re.search(r"\b(max_surge|max_unavailable|node_count)\s*=\s*(\d+)", new_raw)
        if m:
            key_name, val = m.group(1), m.group(2)
            return {
                "rule": f"Keep {key_name} = {val} in {target_file}.",
                "severity": "block",
                "expect": {
                    "file": target_file,
                    "path": "$text",
                    "op": "regex",
                    "value": rf"{re.escape(key_name)}\s*=\s*{val}\b",
                },
            }
        return None

    try:
        old_docs = [d for d in yaml.safe_load_all(old_raw) if isinstance(d, dict)]
        new_docs = [d for d in yaml.safe_load_all(new_raw) if isinstance(d, dict)]
    except yaml.YAMLError:
        return None
    if not old_docs or not new_docs:
        return None

    old_leaves: Dict[str, Any] = {}
    new_leaves: Dict[str, Any] = {}
    for d in old_docs:
        old_leaves.update(_flatten_yaml_leaves(d))
    for d in new_docs:
        new_leaves.update(_flatten_yaml_leaves(d))

    # Check if a key was removed in the fix (e.g. limits.cpu removed)
    removed = [k for k in old_leaves if k not in new_leaves and "tolerations" not in k]
    for rk in removed:
        if "limits.cpu" in rk:
            return {
                "rule": f"Do not set {rk} in {target_file} (must remain absent to prevent CFS throttling).",
                "severity": "block",
                "expect": {"file": target_file, "path": rk, "op": "absent", "value": True},
            }

        # Check changed or added leaves (skip multiline blobs such as ConfigMap data payloads)
    changed = [k for k in new_leaves if old_leaves.get(k) != new_leaves[k]]
    for ck in changed:
        nval = new_leaves[ck]
        oval = old_leaves.get(ck)
        if isinstance(nval, str) and ("\n" in nval or len(nval) > 200):
            continue
        nq, oq = quantity(nval), quantity(oval) if oval is not None else None
        if nq is not None and oq is not None:
            op = ">=" if nq > oq else "<="
            return {
                "rule": f"Keep {ck} {op} {nval} in {target_file} (was {oval} in breaking change).",
                "severity": "block",
                "expect": {"file": target_file, "path": ck, "op": op, "value": nval},
            }
        if oval is None and (".env[" in ck or ck.startswith("env[")):
            return {
                "rule": f"Keep {ck} present in {target_file}.",
                "severity": "block",
                "expect": {"file": target_file, "path": ck, "op": "present", "value": True},
            }
        return {
            "rule": f"Keep {ck} == {nval!r} in {target_file}.",
            "severity": "block",
            "expect": {"file": target_file, "path": ck, "op": "==", "value": nval},
        }
    return None


def _git_commit_index(repo: Path) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Index local git commits by proposal number (#N or !N) and commit SHA."""
    by_pr: Dict[int, Dict[str, Any]] = {}
    by_sha: Dict[str, Dict[str, Any]] = {}
    try:
        raw = git(repo, "log", "-m", "--first-parent", "--format=%x1e%H%x1f%s%x1f%b%x1f", "--name-only")
    except subprocess.CalledProcessError:
        return by_pr, by_sha
    for entry in raw.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x1f", 3)
        if len(parts) < 4:
            continue
        sha, subject, body, files_blob = parts
        files = [f.strip() for f in files_blob.splitlines() if f.strip()]
        info = {"sha": sha.strip(), "files": files}
        by_sha[sha.strip()] = info
        pr_str = _match_pr_num(subject + "\n" + body)
        if pr_str and pr_str.isdigit():
            by_pr.setdefault(int(pr_str), info)
    return by_pr, by_sha


def _files_from_diff(diff_text: str) -> List[str]:
    files = []
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff_text or "", re.M):
        p = m.group(1).strip()
        if p and p != "/dev/null" and p not in files:
            files.append(p)
    return files


def _normalize_proposal(raw: Dict[str, Any], by_pr: Dict[int, Dict[str, Any]],
                        by_sha: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    p = dict(raw)
    num = int(p["number"])
    if not p.get("merged_at") and p.get("state") == "merged":
        p["merged_at"] = p.get("closed") or p.get("updated") or p.get("created") or _dt.date.today().isoformat()
    if not p.get("created_at") and p.get("created"):
        p["created_at"] = p["created"]
    if not p.get("html_url") and p.get("url"):
        p["html_url"] = p["url"]
    cinfo = by_pr.get(num) or by_sha.get(str(p.get("merge_commit_sha") or p.get("sourceRevision") or ""))
    if cinfo:
        if not p.get("merge_commit_sha"):
            p["merge_commit_sha"] = cinfo["sha"]
        if not p.get("_files") and cinfo.get("files"):
            p["_files"] = cinfo["files"]
    if not p.get("_files") and p.get("diff"):
        p["_files"] = _files_from_diff(str(p.get("diff") or ""))
    if not p.get("_comments") and p.get("comments"):
        p["_comments"] = [{"user": c.get("author") or c.get("user"), "body": c.get("body", "")}
                          for c in (p.get("comments") or [])]
    return p


def _normalize_issue(raw: Dict[str, Any]) -> Dict[str, Any]:
    i = dict(raw)
    if not i.get("created_at") and i.get("created"):
        i["created_at"] = i["created"]
    if not i.get("html_url") and i.get("url"):
        i["html_url"] = i["url"]
    return i


def _load_vcs_client() -> Any:
    _ensure_scripts_on_path()
    import vcs_client  # type: ignore
    return vcs_client


def _vcs_fetch_all(repo: Path, repo_spec: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch proposals and issues across GitHub, GitLab, and Bitbucket via vcs_client."""
    vc = _load_vcs_client()
    prs: List[Dict[str, Any]] = []
    seen_nums = set()
    page = 1
    while page <= 20:
        resp = vc.forge("proposal-list", {"state": "all", "limit": 100, "page": page}, repository=repo_spec)
        batch = resp.get("proposals") or []
        new_items = [b for b in batch if b.get("number") not in seen_nums]
        if not new_items:
            break
        for b in new_items:
            seen_nums.add(b.get("number"))
        prs.extend(new_items)
        if not resp.get("truncated"):
            break
        page += 1

    issues_resp = vc.forge("issue-list", {"state": "all", "limit": 100}, repository=repo_spec)
    issues = [_normalize_issue(i) for i in (issues_resp.get("issues") or [])]

    re_rejected = re.compile(r"rejection of\s+(?:PR|MR|pull request|merge request)?\s*[#!]\s*(\d+)", re.I)
    referenced_rejected = {
        int(m.group(1))
        for raw_pr in prs
        for m in re_rejected.finditer(str(raw_pr.get("body") or ""))
    }

    by_pr, by_sha = _git_commit_index(repo)
    re_eval_noise = re.compile(r"eval-spend|probe:|\[withdrawn-eval-probe\]", re.I)
    normalized_prs: List[Dict[str, Any]] = []
    for raw_pr in prs:
        pr = _normalize_proposal(raw_pr, by_pr, by_sha)
        is_closed_unmerged = not pr.get("merged_at") and pr.get("state") == "closed"
        is_eval_noise = bool(re_eval_noise.search(f"{pr.get('title', '')} {pr.get('body', '')}"))
        if is_closed_unmerged and (int(pr["number"]) in referenced_rejected or not is_eval_noise):
            try:
                view = vc.forge(
                    "proposal-view",
                    {"number": int(pr["number"]), "comments": True, "diff": True, "limit": 100},
                    repository=repo_spec,
                )
                pr["_comments"] = view.get("comments") or []
                if not pr.get("_files") and view.get("diff"):
                    pr["_files"] = _files_from_diff(view["diff"])
            except Exception:
                pass
        normalized_prs.append(pr)
    return normalized_prs, issues


def _format_proposal_ref(pr_obj: Dict[str, Any], num: int, repo_id: str) -> str:
    url = pr_obj.get("html_url") or pr_obj.get("url")
    sigil = "!" if (url and "/-/merge_requests/" in str(url)) else "#"
    if url:
        return f"{url} ({sigil}{num})"
    return f"https://github.com/{repo_id}/pull/{num} (#{num})"


def _format_issue_ref(inc_obj: Dict[str, Any], num: int, repo_id: str) -> str:
    url = inc_obj.get("html_url") or inc_obj.get("url")
    if url:
        return f"{url} (#{num})"
    return f"https://github.com/{repo_id}/issues/{num} (#{num})"


def _parse_since_cutoff(since_spec: Optional[str]) -> Optional[str]:
    if not since_spec:
        return None
    s = str(since_spec).strip()
    m = re.fullmatch(r"(\d+)\s*([ydm])", s, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = n * 365 if unit == "y" else (n * 30 if unit == "m" else n)
        return (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return None


def _infer_quota_ceiling(repo: Path, target_file: str, comments_and_body: str) -> Tuple[str, str]:
    """Infer ResourceQuota ceiling field and value from the target YAML or reviewer comments."""
    if target_file and (repo / target_file).is_file():
        try:
            for doc in yaml.safe_load_all((repo / target_file).read_text()):
                if isinstance(doc, dict) and doc.get("kind") == "ResourceQuota":
                    hard = (doc.get("spec") or {}).get("hard") or {}
                    if "requests.memory" in hard:
                        return "spec.hard.requests\\.memory", str(hard["requests.memory"])
                    for k, v in hard.items():
                        safe_k = str(k).replace(".", "\\.")
                        return f"spec.hard.{safe_k}", str(v)
        except Exception:
            pass
    m_qty = re.search(r"\b(\d+(?:\.\d+)?(?:Ki|Mi|Gi|Ti|m|k|M|G|T))\b", comments_and_body)
    return "spec.hard.requests\\.memory", (m_qty.group(1) if m_qty else "32Gi")


def cmd_bootstrap(a) -> Dict[str, Any]:
    """First Two Hours Bootstrap: harvest historical PRs/MRs, reverts, rejected proposals, and issues."""
    import time
    t_start = time.monotonic()
    budget_sec = float(getattr(a, "budget_minutes", 120) or 120) * 60.0
    cutoff_date = _parse_since_cutoff(getattr(a, "since", None))

    repo = Path(a.repo)
    gh_repo = a.github_repo or repo_identity(repo)
    by_pr, by_sha = _git_commit_index(repo)
    if a.prs_json and a.issues_json:
        prs = [_normalize_proposal(p, by_pr, by_sha) for p in json.loads(Path(a.prs_json).read_text())]
        issues = [_normalize_issue(i) for i in json.loads(Path(a.issues_json).read_text())]
    else:
        prs, issues = _vcs_fetch_all(repo, gh_repo)

    prs = [p for p in prs if p.get("title") != "[withdrawn-eval-probe]"]
    if cutoff_date:
        prs = [
            p for p in prs
            if not (p.get("merged_at") or p.get("created_at"))
            or str(p.get("merged_at") or p.get("created_at"))[:10] >= cutoff_date
        ]
    pr_by_num = {int(p["number"]): p for p in prs}
    issue_by_num = {int(i["number"]): i for i in issues}

    re_reverts = re.compile(
        r"Reverts\s+(?:PR|MR|pull request|merge request)?\s*(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)?[#!]\s*(\d+)"
        r"|^Revert\s+PR\s+[#!]\s*(\d+)",
        re.I | re.M,
    )
    re_fixes = re.compile(
        r"(?:fixes|resolves|mitigates|closes)\s+(?:SEV-\d+\s+Incident\s+|Incident\s+|issue\s+)?"
        r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)?[#!]\s*(\d+)",
        re.I,
    )
    re_rejected = re.compile(r"rejection of\s+(?:PR|MR|pull request|merge request)?\s*[#!]\s*(\d+)", re.I)
    re_supersedes = re.compile(
        r"Supersedes Freeze from Incident\s+[#!]\s*(\d+)\s*/\s*Revert\s+(?:PR|MR|pull request|merge request)?\s*[#!]\s*(\d+)"
        r"|supersedes\b[^\n#!]*(?:PR|MR|pull request|merge request)?\s*[#!]\s*(\d+)",
        re.I,
    )
    re_high_signal_fix = re.compile(
        r"^(?:fix|perf)\([^)]+\):|^docs(?:\(conventions\))?:.*(?:conventions|naming|require|policy)",
        re.I,
    )
    re_eval_noise = re.compile(r"eval-spend|probe:|\[withdrawn-eval-probe\]", re.I)

    written: List[str] = []
    triage_counts = {"revert-lesson": 0, "decision": 0, "superseded": 0, "routine": 0}
    llm_synthesized_count = 0
    llm_url, llm_model, llm_key = _resolve_llm_config(a)
    try:
        tracked_files = set(git(repo, "ls-files").splitlines())
    except subprocess.CalledProcessError:
        tracked_files = set()
    pr_to_record_id: Dict[int, str] = {}
    allocated_ids: Dict[Tuple[str, int], int] = {}

    def _alloc_id(kind: str, yr: int) -> str:
        prefix = "LES" if kind == "lesson" else "DEC"
        key = (prefix, yr)
        if key not in allocated_ids:
            existing_id = _next_id(repo, kind, yr)
            allocated_ids[key] = int(existing_id.rsplit("-", 1)[-1]) - 1
        allocated_ids[key] += 1
        return f"{prefix}-{yr}-{allocated_ids[key]:03d}"

    # Sort PRs chronologically by number
    for pr in sorted(prs, key=lambda x: int(x["number"])):
        if (time.monotonic() - t_start) > budget_sec:
            break
        num = int(pr["number"])
        title = pr.get("title") or ""
        body = pr.get("body") or ""
        merged = pr.get("merged_at")
        files = pr.get("_files") or []
        date_str = str(merged or pr.get("created_at") or _dt.date.today().isoformat())[:10]
        year = int(date_str[:4])

        # Case 1: Superseding PR (e.g. BB-10 PR #33 or PR #254 superseding #253)
        m_sup = re_supersedes.search(body)
        if merged and m_sup:
            if m_sup.group(1) and m_sup.group(2):
                inc_num, old_rev_pr = int(m_sup.group(1)), int(m_sup.group(2))
            else:
                inc_num, old_rev_pr = 0, int(m_sup.group(3))
            old_rec_id = pr_to_record_id.get(old_rev_pr)
            inc = issue_by_num.get(inc_num, {}) if inc_num else {}
            old_pr_obj = pr_by_num.get(old_rev_pr, {})
            scoped_files = sorted(set(files + (old_pr_obj.get("_files") or []))) or ["CONVENTIONS.md"]
            rid = _alloc_id("decision", year)
            synth = _llm_synthesize_record(
                llm_url, llm_model, llm_key, "decision",
                {"pr_number": num, "title": title, "body": body, "files": scoped_files,
                 "supersedes_pr": old_rev_pr, "incident": inc},
            ) if llm_url else None
            if synth:
                llm_synthesized_count += 1
            rec_title = str((synth or {}).get("title") or title).strip()
            evidence: Dict[str, Any] = {
                "pull_request": _format_proposal_ref(pr, num, gh_repo),
                "breaking_pr": f"#{old_rev_pr}",
            }
            if inc_num:
                evidence["incident"] = _format_issue_ref(inc, inc_num, gh_repo)
            meta: Dict[str, Any] = {
                "type": "decision",
                "id": rid,
                "title": rec_title,
                "status": "active",
                "date": date_str,
                "scope": {"paths": scoped_files},
                "evidence": evidence,
            }
            if old_rec_id:
                meta["supersedes"] = [old_rec_id]
            rec_body_lines = [
                "## Context",
                "",
                str((synth or {}).get("context") or body).strip(),
                "",
            ]
            if (synth or {}).get("decision"):
                rec_body_lines.extend(["## Decision", "", str(synth["decision"]).strip(), ""])
            if (synth or {}).get("consequences"):
                rec_body_lines.extend(["## Consequences", "", str(synth["consequences"]).strip(), ""])
            if inc_num:
                rec_body_lines.extend([
                    "## Superseded Incident & Freeze",
                    "",
                    f"Supersedes earlier revert PR #{old_rev_pr} and Incident #{inc_num} ({inc.get('title', '')}).",
                    "The prerequisite upgrade has landed and the temporary freeze is lifted.",
                ])
            else:
                rec_body_lines.extend([
                    "## Superseded Policy",
                    "",
                    f"Supersedes earlier PR #{old_rev_pr} ({old_pr_obj.get('title', '')}).",
                ])
            rec_body = "\n".join(rec_body_lines)
            rel = f"knowledge/decisions/{rid}-{slug(rec_title)}.md"
            if a.write:
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(render_record(meta, rec_body))
                written.append(rel)
                # Reconcile superseded status on the old record
                if old_rec_id:
                    for rec in load_records(repo):
                        if rec.id == old_rec_id:
                            rec.meta["status"] = "superseded"
                            rec.meta["superseded_by"] = rid
                            (repo / rec.path).write_text(render_record(rec.meta, rec.body))
            pr_to_record_id[num] = rid
            triage_counts["superseded"] += 1
            continue

        # Case 2: Revert / Fix PR resolving an Incident after a Rejected PR (e.g. BB-02)
        m_rej = re_rejected.search(body)
        m_fix = re_fixes.search(body + " " + title)
        if merged and m_rej and m_fix:
            rej_num = int(m_rej.group(1))
            inc_num = int(m_fix.group(1))
            rej_pr = pr_by_num.get(rej_num, {})
            inc = issue_by_num.get(inc_num, {})
            rej_files = rej_pr.get("_files") or []
            target_file = rej_files[0] if rej_files else (files[0] if files else "")
            comments = "\n\n".join(c.get("body", "") for c in (rej_pr.get("_comments") or []))
            rid = _alloc_id("decision", year)
            q_path, q_val = _infer_quota_ceiling(repo, target_file, comments + "\n" + body)
            guard = {
                "rule": f"Do not raise {target_file} above {q_val}; right-size offending workloads instead (rejected in PR #{rej_num}, fixed in PR #{num}).",
                "severity": "block",
                "expect": {
                    "file": target_file,
                    "path": q_path,
                    "op": "<=",
                    "value": q_val,
                },
            }
            synth = _llm_synthesize_record(
                llm_url, llm_model, llm_key, "decision",
                {"pr_number": num, "title": title, "body": body, "rejected_pr": rej_num,
                 "rejected_comments": comments, "incident": inc, "files": sorted(set(rej_files + files))},
            ) if llm_url else None
            if synth:
                llm_synthesized_count += 1
                if isinstance(synth.get("guard"), dict) and _guard_passes_on_head(repo, synth["guard"], []):
                    guard = synth["guard"]
            rec_title = str(
                (synth or {}).get("title")
                or f"Hard multi-tenant quota ceiling on {target_file} (rejected PR #{rej_num})"
            ).strip()
            meta = {
                "type": "decision",
                "id": rid,
                "title": rec_title,
                "status": "active",
                "date": date_str,
                "scope": {"paths": sorted(set(rej_files + files))},
                "guard": guard,
                "evidence": {
                    "pull_request": _format_proposal_ref(pr, num, gh_repo),
                    "breaking_pr": _format_proposal_ref(rej_pr, rej_num, gh_repo),
                    "incident": _format_issue_ref(inc, inc_num, gh_repo),
                },
            }
            rec_body_lines = [
                "## Architectural Decision",
                "",
                str((synth or {}).get("decision") or comments or body).strip(),
                "",
            ]
            if (synth or {}).get("consequences"):
                rec_body_lines.extend(["## Consequences", "", str(synth["consequences"]).strip(), ""])
            rec_body_lines.extend([
                "## Incident & Workload Fix",
                "",
                f"Incident #{inc_num}: {inc.get('title', '')}",
                "",
                inc.get("body", "").strip(),
                "",
                f"Rejected quota increase in PR #{rej_num}; resolved at workload level in PR #{num}:",
                body.strip(),
            ])
            rec_body = "\n".join(rec_body_lines)
            rel = f"knowledge/decisions/{rid}-{slug(meta['title'])}.md"
            if a.write:
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(render_record(meta, rec_body))
                written.append(rel)
            pr_to_record_id[num] = rid
            triage_counts["decision"] += 1
            continue

        # Case 3: Revert / Fix PR reverting a Breaking PR (e.g. BB-01..10 or PR #259 reverting #258)
        m_rev = re_reverts.search(body + "\n" + title)
        if merged and m_rev and (m_fix or llm_url):
            brk_num = int(m_rev.group(1) or m_rev.group(2))
            inc_num = int(m_fix.group(1)) if m_fix else 0
            brk_pr = pr_by_num.get(brk_num, {})
            inc = issue_by_num.get(inc_num, {}) if inc_num else {}
            scoped_files = sorted(set(files + (brk_pr.get("_files") or [])))
            target_file = scoped_files[0] if scoped_files else ""
            commit_sha = pr.get("merge_commit_sha") or ""
            guard = _infer_guard_from_commit(repo, commit_sha, target_file) if (commit_sha and target_file) else None
            synth = _llm_synthesize_record(
                llm_url, llm_model, llm_key, "lesson",
                {"pr_number": num, "title": title, "body": body, "breaking_pr": brk_num,
                 "breaking_title": brk_pr.get("title", ""), "breaking_body": brk_pr.get("body", ""),
                 "incident": inc, "files": scoped_files},
            ) if llm_url else None
            if synth:
                llm_synthesized_count += 1
                if not guard and isinstance(synth.get("guard"), dict) and _guard_passes_on_head(repo, synth["guard"], []):
                    guard = synth["guard"]
            rid = _alloc_id("lesson", year)
            rec_title = str((synth or {}).get("title") or inc.get("title") or title).strip()
            evidence = {
                "pull_request": _format_proposal_ref(pr, num, gh_repo),
                "breaking_pr": _format_proposal_ref(brk_pr, brk_num, gh_repo),
                "commit": commit_sha[:12] if commit_sha else "",
            }
            if inc_num:
                evidence["incident"] = _format_issue_ref(inc, inc_num, gh_repo)
            meta = {
                "type": "lesson",
                "id": rid,
                "title": rec_title,
                "status": "active",
                "date": date_str,
                "scope": {"paths": scoped_files or ["gcp/**"]},
                "evidence": evidence,
            }
            if guard:
                meta["guard"] = guard
            header_line = (
                f"Breaking PR #{brk_num} ({brk_pr.get('title', '')}) triggered Incident #{inc_num}:"
                if inc_num else f"Breaking PR #{brk_num} ({brk_pr.get('title', '')}) required revert in PR #{num}:"
            )
            rec_body_lines = [
                "## What happened",
                "",
                header_line,
                "",
                str((synth or {}).get("root_cause") or inc.get("body") or body).strip(),
                "",
                "## What fixed it",
                "",
                f"Revert/Fix PR #{num} ({title}):",
                str((synth or {}).get("fix") or body).strip(),
            ]
            if (synth or {}).get("prevention"):
                rec_body_lines.extend(["", "## Prevent it next time", "", str(synth["prevention"]).strip()])
            rec_body = "\n".join(rec_body_lines)
            rel = f"knowledge/lessons/{rid}-{slug(title)}.md"
            if a.write:
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(render_record(meta, rec_body))
                written.append(rel)
            pr_to_record_id[num] = rid
            triage_counts["revert-lesson"] += 1
            continue

        # Case 4: High-signal incident fix or governance/convention PR synthesized via LiteLLM
        live_files = [
            f for f in files
            if _file_exists_on_head(repo, f, tracked_files) and not f.startswith("knowledge/")
        ]
        if (
            llm_url
            and merged
            and live_files
            and not re_eval_noise.search(title + " " + body)
            and (m_fix or re_high_signal_fix.search(title))
        ):
            is_doc_policy = title.lower().startswith("docs") or any(f.endswith("CONVENTIONS.md") for f in live_files)
            rec_kind = "decision" if is_doc_policy else "lesson"
            inc_num = int(m_fix.group(1)) if m_fix else 0
            inc = issue_by_num.get(inc_num, {}) if inc_num else {}
            target_file = live_files[0]
            commit_sha = pr.get("merge_commit_sha") or ""
            guard = _infer_guard_from_commit(repo, commit_sha, target_file) if (commit_sha and target_file) else None
            if guard and not _guard_passes_on_head(repo, guard, []):
                guard = None
            synth = _llm_synthesize_record(
                llm_url, llm_model, llm_key, rec_kind,
                {"pr_number": num, "title": title, "body": body, "incident": inc, "files": live_files},
            )
            if synth:
                llm_synthesized_count += 1
                if not guard and isinstance(synth.get("guard"), dict) and _guard_passes_on_head(repo, synth["guard"], []):
                    guard = synth["guard"]
                rid = _alloc_id(rec_kind, year)
                rec_title = str(synth.get("title") or inc.get("title") or title).strip()
                evidence = {
                    "pull_request": _format_proposal_ref(pr, num, gh_repo),
                    "commit": commit_sha[:12] if commit_sha else "",
                }
                if inc_num:
                    evidence["incident"] = _format_issue_ref(inc, inc_num, gh_repo)
                meta = {
                    "type": rec_kind,
                    "id": rid,
                    "title": rec_title,
                    "status": "active",
                    "date": date_str,
                    "scope": {"paths": live_files},
                    "evidence": {k: v for k, v in evidence.items() if v},
                }
                if guard:
                    meta["guard"] = guard
                if rec_kind == "decision":
                    rec_body = "\n".join([
                        "## Context",
                        "",
                        str(synth.get("context") or body).strip(),
                        "",
                        "## Decision",
                        "",
                        str(synth.get("decision") or body).strip(),
                        "",
                        "## Consequences",
                        "",
                        str(synth.get("consequences") or "").strip(),
                    ])
                    rel = f"knowledge/decisions/{rid}-{slug(rec_title)}.md"
                    triage_counts["decision"] += 1
                else:
                    rec_body = "\n".join([
                        "## What happened",
                        "",
                        str(synth.get("root_cause") or inc.get("body") or body).strip(),
                        "",
                        "## What fixed it",
                        "",
                        f"Fix PR #{num} ({title}):",
                        str(synth.get("fix") or body).strip(),
                        "",
                        "## Prevent it next time",
                        "",
                        str(synth.get("prevention") or "").strip(),
                    ])
                    rel = f"knowledge/lessons/{rid}-{slug(rec_title)}.md"
                    triage_counts["revert-lesson"] += 1
                if a.write:
                    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                    (repo / rel).write_text(render_record(meta, rec_body))
                    written.append(rel)
                pr_to_record_id[num] = rid
                continue

        # Case 5: Closed-unmerged (rejected) PR with reviewer/rejection comments
        if not merged and pr.get("state") == "closed" and not re_eval_noise.search(title + " " + body):
            comments_text = "\n\n".join(str(c.get("body") or "") for c in (pr.get("_comments") or [])).strip()
            cited_ids = sorted(set(re.findall(r"\b(?:DEC|LES)-\d{4}-\d{3,}\b", comments_text + "\n" + body)))
            rej_ref = _format_proposal_ref(pr, num, gh_repo)
            if cited_ids and a.write:
                matched_existing = False
                for rec in load_records(repo):
                    if rec.id in cited_ids:
                        ev = dict(rec.meta.get("evidence") or {})
                        rej_list = [str(x) for x in (ev.get("rejected_proposals") or [])]
                        if rej_ref not in rej_list:
                            rej_list.append(rej_ref)
                        ev["rejected_proposals"] = rej_list
                        rec.meta["evidence"] = ev
                        (repo / rec.path).write_text(render_record(rec.meta, rec.body))
                        if rec.path not in written:
                            written.append(rec.path)
                        matched_existing = True
                if matched_existing:
                    triage_counts["revert-lesson"] += 1
                    continue
            if llm_url and comments_text and live_files and re.search(r"\b(?:reject|violat|do not|must not|cannot|blocked)\b", comments_text, re.I):
                synth = _llm_synthesize_record(
                    llm_url, llm_model, llm_key, "lesson",
                    {"rejected_pr": num, "title": title, "body": body, "rejection_comments": comments_text, "files": live_files},
                )
                if synth:
                    llm_synthesized_count += 1
                    guard = synth.get("guard") if (isinstance(synth.get("guard"), dict) and _guard_passes_on_head(repo, synth["guard"], [])) else None
                    rid = _alloc_id("lesson", year)
                    rec_title = str(synth.get("title") or f"Rejected proposal #{num}: {title}").strip()
                    meta = {
                        "type": "lesson",
                        "id": rid,
                        "title": rec_title,
                        "status": "active",
                        "date": date_str,
                        "scope": {"paths": live_files},
                        "evidence": {
                            "pull_request": rej_ref,
                            "rejected_proposals": [rej_ref],
                        },
                    }
                    if guard:
                        meta["guard"] = guard
                    rec_body = "\n".join([
                        "## What happened",
                        "",
                        str(synth.get("root_cause") or comments_text).strip(),
                        "",
                        "## What fixed it",
                        "",
                        f"Rejected PR #{num} ({title}):",
                        str(synth.get("fix") or comments_text).strip(),
                        "",
                        "## Prevent it next time",
                        "",
                        str(synth.get("prevention") or "").strip(),
                    ])
                    rel = f"knowledge/lessons/{rid}-{slug(rec_title)}.md"
                    if a.write:
                        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                        (repo / rel).write_text(render_record(meta, rec_body))
                        written.append(rel)
                    pr_to_record_id[num] = rid
                    triage_counts["revert-lesson"] += 1
                    continue

        triage_counts["routine"] += 1

    # Write STATE_OF_BEING.md
    if a.write:
        records = load_records(repo)
        sob_lines = [
            f"# State of Being — `{gh_repo}`",
            "",
            "Generated by the Decision Memory First-Two-Hours Bootstrap Harvester.",
            "",
            "## Active Decisions and Lessons",
            "",
        ]
        for r in records:
            if r.status == "active":
                sob_lines.append(
                    f"- **{r.id}** ({r.type}): {r.title} — paths: `{', '.join(r.scope['paths'])}` "
                    f"(fix: {r.evidence.get('pull_request', '')}, incident: {r.evidence.get('incident', '')}, "
                    f"breaking: {r.evidence.get('breaking_pr', '')})"
                )
        sob_lines.extend(["", "## Superseded Records", ""])
        for r in records:
            if r.status == "superseded":
                sob_lines.append(
                    f"- ~~**{r.id}**~~ ({r.type}): {r.title} — **Superseded by {r.meta.get('superseded_by')}**"
                )
        sob_path = repo / "knowledge/STATE_OF_BEING.md"
        sob_path.parent.mkdir(parents=True, exist_ok=True)
        sob_path.write_text("\n".join(sob_lines) + "\n")
        written.append("knowledge/STATE_OF_BEING.md")

    idx_res = None
    if a.hindsight_url and a.write:
        ns = argparse.Namespace(repo=str(repo), repo_id=gh_repo, hindsight_url=a.hindsight_url,
                                bank=a.bank, force=True)
        idx_res = cmd_index(ns)

    return {
        "github_repo": gh_repo,
        "prs_scanned": len(prs),
        "issues_scanned": len(issues),
        "triage": triage_counts,
        "llm_synthesized": llm_synthesized_count,
        "written": written,
        "index": idx_res,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="GitOps repository checkout")
    ap.add_argument("--hindsight-url", default=os.environ.get("DECISION_MEMORY_HINDSIGHT_URL")
                    or os.environ.get("HINDSIGHT_API_URL"))
    ap.add_argument("--bank", default=os.environ.get("DECISION_MEMORY_BANK", DEFAULT_BANK_ID))
    ap.add_argument("--repo-id", default=None)
    ap.add_argument("--llm-url", default=None, help="OpenAI-compatible LLM base URL for OKF synthesis")
    ap.add_argument("--llm-model", default=None, help="Model name for OKF synthesis (default: model-default)")
    ap.add_argument("--no-llm", action="store_true", help="Disable LLM synthesis and use deterministic extraction")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    p = sub.add_parser("index")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--base")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--files", nargs="*")
    p.add_argument("--knowledge-repo", help="where knowledge/ lives, if not the changed repo")
    p.add_argument("--semantic", nargs="?", const="auto", default=None)
    p.add_argument("--semantic-limit", type=int, default=5)
    p.add_argument("--markdown", action="store_true", help="print only the markdown section")
    p = sub.add_parser("why")
    p.add_argument("path", nargs="*", help="One or more repository-relative file paths")
    p.add_argument("--path", "--file", dest="flag_paths", action="append", default=[],
                   help="Repository-relative file path (may be repeated)")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("from-outcome")
    p.add_argument("outcome", help="outcome JSON file")
    p.add_argument("--write", action="store_true")
    p.add_argument("--llm-url", default=None, help="OpenAI-compatible LLM base URL for OKF synthesis")
    p.add_argument("--llm-model", default=None, help="Model name for OKF synthesis")
    p.add_argument("--no-llm", action="store_true", help="Disable LLM synthesis")
    p = sub.add_parser("dream")
    p.add_argument("--recurrence-threshold", type=int, default=2)
    p.add_argument("--since-days", type=int, default=7)
    p.add_argument("--infra-glob", nargs="*", default=["clusters/**", "fleet/**", "policy/**"])
    p.add_argument("--include-ungoverned", action="store_true")
    p.add_argument("--apply", action="store_true", help="write proposed edits into the working tree")
    p = sub.add_parser("bootstrap")
    p.add_argument("--github-repo", default=None, help="owner/repo on GitHub")
    p.add_argument("--prs-json", default=None, help="optional local JSON file of PRs")
    p.add_argument("--issues-json", default=None, help="optional local JSON file of Issues")
    p.add_argument("--since", default="2y", help="history window to harvest (for example, 2y or 180d)")
    p.add_argument("--budget-minutes", type=int, default=120, help="wall-clock budget in minutes for bootstrap")
    p.add_argument("--write", action="store_true", help="write harvested records into knowledge/")
    p.add_argument("--llm-url", default=None, help="OpenAI-compatible LLM base URL for OKF synthesis")
    p.add_argument("--llm-model", default=None, help="Model name for OKF synthesis")
    p.add_argument("--no-llm", action="store_true", help="Disable LLM synthesis")
    a = ap.parse_args(argv)
    handlers = {"validate": cmd_validate, "index": cmd_index, "check": cmd_check, "why": cmd_why,
                "from-outcome": cmd_from_outcome, "dream": cmd_dream, "bootstrap": cmd_bootstrap}
    result = handlers[a.cmd](a)
    if a.cmd == "dream" and a.apply:
        result["applied"] = apply_edits(Path(a.repo), result["proposed_edits"])
    if a.cmd == "check" and a.markdown:
        print(result["markdown"])
    else:
        print(json.dumps(result, indent=2, default=str))
    if a.cmd == "check":
        return 2 if result["verdict"] == "block" else 0
    if a.cmd == "validate":
        return 0 if result["ok"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

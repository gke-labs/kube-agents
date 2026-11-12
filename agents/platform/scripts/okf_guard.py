#!/usr/bin/env python3
"""Deterministic Operational Knowledge Framework (OKF) guard evaluator for VCS publish."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

RECORD_DIRS = (
    "knowledge/decision",
    "knowledge/lesson",
    "knowledge/decisions",
    "knowledge/lessons",
)
RECORD_TYPES = (
    "decision",
    "lesson",
    "runbook",
    "cluster-blueprint",
    "tenancy-model",
    "metric-definition",
    "escalation",
    "observation",
)
STATUSES = ("proposed", "active", "rejected", "superseded", "retired")
SEVERITIES = ("block", "warn", "info")
VALID_OPS = ("==", "!=", ">=", ">", "<=", "<", "absent", "present", "regex")
SKIP_FILENAMES = frozenset({"index.md", "readme.md", "template.md", "state_of_being.md"})
RECORD_ID_RE = re.compile(r"(DEC|LES)-\d{4}-\d{3,}")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
QTY_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)$")
PATH_SEG_RE = re.compile(r"([^\[]+)(?:\[([^=\]]+)=([^\]]+)\])?")
POD_SPEC_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"})
UNITS = {
    "": 1.0,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "Ki": 1024.0,
    "Mi": float(1024**2),
    "Gi": float(1024**3),
    "Ti": float(1024**4),
}


def match_glob(pattern: str, target: str) -> bool:
    """Match repository-relative slash paths with `**` multi-segment wildcards."""
    pat = pattern.strip("/")
    tgt = target.strip("/")
    if pat in ("**", "**/*"):
        return bool(tgt)
    if pat == tgt:
        return True
    pat_segs = [s for s in pat.split("/") if s]
    tgt_segs = [s for s in tgt.split("/") if s]

    def _rec(pi: int, ti: int) -> bool:
        while pi < len(pat_segs):
            if pat_segs[pi] == "**":
                while pi + 1 < len(pat_segs) and pat_segs[pi + 1] == "**":
                    pi += 1
                if pi == len(pat_segs) - 1:
                    return True
                for k in range(ti, len(tgt_segs) + 1):
                    if _rec(pi + 1, k):
                        return True
                return False
            if ti >= len(tgt_segs) or not fnmatch.fnmatchcase(tgt_segs[ti], pat_segs[pi]):
                return False
            pi += 1
            ti += 1
        return ti == len(tgt_segs)

    return _rec(0, 0)


def quantity(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    matched = QTY_RE.match(str(val).strip())
    if not matched or matched.group(2) not in UNITS:
        return None
    return float(matched.group(1)) * UNITS[matched.group(2)]


def path_tokens(path_expr: str) -> List[Tuple[str, Optional[Tuple[str, str]]]]:
    tokens: List[Tuple[str, Optional[Tuple[str, str]]]] = []
    for part in re.split(r"(?<!\\)\.", path_expr):
        unescaped = part.replace("\\.", ".")
        matched = PATH_SEG_RE.fullmatch(unescaped)
        if not matched:
            raise ValueError(f"bad guard path segment {unescaped!r}")
        sel = (matched.group(2), matched.group(3)) if matched.group(2) else None
        tokens.append((matched.group(1), sel))
    return tokens


def yaml_get(doc: Any, path_expr: str) -> Any:
    cur = doc
    for key, sel in path_tokens(path_expr):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
        if sel:
            sk, sv = sel
            if not isinstance(cur, list):
                return None
            cur = next((x for x in cur if isinstance(x, dict) and str(x.get(sk)) == sv), None)
            if cur is None:
                return None
    return cur


def compare_values(actual: Any, op: str, expected: Any) -> bool:
    if op == "absent":
        return actual is None
    if op == "present":
        return actual is not None
    if actual is None:
        return False
    if op == "regex":
        return bool(re.search(str(expected), str(actual), re.M | re.S))
    aq, eq = quantity(actual), quantity(expected)
    if op in ("==", "!="):
        same = (aq == eq) if (aq is not None and eq is not None) else (str(actual) == str(expected))
        return same if op == "==" else not same
    if aq is None or eq is None:
        return False
    return {">=": aq >= eq, ">": aq > eq, "<=": aq <= eq, "<": aq < eq}.get(op, False)


def _doc_resource_id(doc: Any) -> Optional[str]:
    if isinstance(doc, dict) and doc.get("kind") and isinstance(doc.get("metadata"), dict):
        md = doc["metadata"]
        return f"{doc['kind']}/{md.get('namespace', '')}/{md.get('name', '')}"
    return None


def _doc_applicable_to_path(doc: Any, path_expr: str) -> bool:
    if not isinstance(doc, dict):
        return False
    kind = str(doc.get("kind") or "").strip()
    if not kind:
        return True
    if path_expr.startswith("spec.template.spec."):
        return kind in POD_SPEC_WORKLOAD_KINDS
    if path_expr.startswith("spec.jobTemplate.spec."):
        return kind == "CronJob"
    return True


def evaluate_guard(
    expect: Dict[str, Any],
    files: Dict[str, str],
    scope_resources: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    op = str(expect["op"])
    path_expr = str(expect["path"])
    file_glob = str(expect["file"])
    for fpath, content in sorted(files.items()):
        if not match_glob(file_glob, fpath):
            continue
        if path_expr == "$text":
            results.append({
                "file": fpath,
                "actual": "<text>",
                "ok": compare_values(content, op, expect.get("value")),
            })
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
                d
                for d in docs
                if _doc_resource_id(d)
                and any(match_glob(g, _doc_resource_id(d) or "") for g in scope_resources)
            ]
            if scoped:
                docs = scoped
        elif len(docs) > 1:
            applicable = [d for d in docs if _doc_applicable_to_path(d, path_expr)]
            if applicable:
                docs = applicable
        for doc in docs:
            actual = yaml_get(doc, path_expr)
            if actual is None and op not in ("absent", "present"):
                results.append({"file": fpath, "actual": None, "ok": False})
                continue
            results.append({
                "file": fpath,
                "actual": actual,
                "ok": compare_values(actual, op, expect.get("value")),
            })
    return results


def _normalize_declares(meta: Dict[str, Any]) -> None:
    declares = meta.get("declares")
    if not isinstance(declares, list) or not declares:
        return
    scope = dict(meta.get("scope") or {})
    paths = [str(x) for x in (scope.get("paths") or [])]
    resources = [str(x) for x in (scope.get("resources") or [])]
    clusters = [str(x) for x in (scope.get("clusters") or [])]
    checks = [str(x) for x in (scope.get("checks") or [])]
    for item in declares:
        if not isinstance(item, dict):
            continue
        for key, bucket in (("check", checks), ("cluster", clusters), ("file", paths)):
            val = str(item.get(key) or "").strip()
            if val and val not in bucket:
                bucket.append(val)
        obj = str(item.get("object") or "").strip()
        if obj:
            ns = str(item.get("namespace") or "").strip()
            if obj.count("/") == 1 and ns:
                kind, name = obj.split("/", 1)
                obj = f"{kind}/{ns}/{name}"
            if obj not in resources:
                resources.append(obj)
        fl = str(item.get("file") or "").strip()
        if fl and item.get("path") and item.get("op") and not meta.get("guard"):
            meta["guard"] = {
                "rule": str(item.get("rule") or "").strip(),
                "severity": str(item.get("severity") or "block").strip() or "block",
                "expect": {
                    "file": fl,
                    "path": str(item["path"]).strip(),
                    "op": str(item["op"]).strip(),
                    "value": item.get("value"),
                },
            }
    scope.update({"paths": paths, "resources": resources, "clusters": clusters, "checks": checks})
    meta["scope"] = scope


def intent_record_dirs(repo: Path) -> List[str]:
    dirs: List[str] = []
    intent_file = repo / ".kube-agents" / "intent.yaml"
    if intent_file.is_file():
        try:
            loaded = yaml.safe_load(intent_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, dict) and isinstance(loaded.get("paths"), list):
                for raw_p in loaded["paths"]:
                    clean = str(raw_p or "").strip().strip("/")
                    if clean and ".." not in clean.split("/"):
                        if clean not in dirs:
                            dirs.append(clean)
                        for leaf in ("decision", "lesson", "decisions", "lessons"):
                            cand = f"{clean}/{leaf}"
                            if cand not in dirs:
                                dirs.append(cand)
        except Exception:  # noqa: BLE001
            pass
    for d in RECORD_DIRS:
        if d not in dirs:
            dirs.append(d)
    return dirs


def load_okf_records(repo: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    records: List[Dict[str, Any]] = []
    active_errors: List[str] = []
    seen = set()
    for sub in intent_record_dirs(repo):
        base = repo / sub
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            if path.name.lower() in SKIP_FILENAMES:
                continue
            rel = path.relative_to(repo).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            text = path.read_text(encoding="utf-8", errors="replace")
            matched = FRONTMATTER_RE.match(text)
            if not matched:
                continue
            try:
                meta = yaml.safe_load(matched.group(1)) or {}
            except yaml.YAMLError as exc:
                active_errors.append(f"{rel}: {exc}")
                continue
            if not isinstance(meta, dict):
                continue
            rec_id = str(meta.get("id") or "").strip()
            rec_type = str(meta.get("type") or "").strip()
            if not rec_id and not rec_type:
                continue
            if rec_type not in ("decision", "lesson") and not RECORD_ID_RE.fullmatch(rec_id):
                continue
            _normalize_declares(meta)
            status = str(meta.get("status") or "active").strip()
            errs = _validate_meta(meta)
            if errs:
                if status == "active":
                    active_errors.append(f"{rec_id or rel} ({rel}): {'; '.join(errs)}")
                continue
            records.append({"path": rel, "meta": meta, "body": matched.group(2).strip()})
    return records, active_errors


def _validate_meta(meta: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    rtype = str(meta.get("type") or "")
    rid = str(meta.get("id") or "")
    status = str(meta.get("status") or "active")
    if rtype not in RECORD_TYPES:
        errs.append(f"invalid type {rtype!r}")
    if not RECORD_ID_RE.fullmatch(rid):
        errs.append(f"invalid id {rid!r}")
    if status not in STATUSES:
        errs.append(f"invalid status {status!r}")
    if not str(meta.get("title") or "").strip():
        errs.append("title is required")
    sc = meta.get("scope") or {}
    if not any(sc.get(k) for k in ("paths", "resources", "checks")):
        errs.append("scope is empty")
    guard = meta.get("guard") or {}
    if guard:
        if guard.get("severity") and guard["severity"] not in SEVERITIES:
            errs.append(f"invalid guard.severity {guard['severity']!r}")
        exp = guard.get("expect")
        if exp:
            req_keys = ("file", "path", "op") if exp.get("op") in ("absent", "present") else ("file", "path", "op", "value")
            if not all(k in exp for k in req_keys):
                errs.append("incomplete guard.expect")
            elif exp.get("op") not in VALID_OPS:
                errs.append(f"invalid op {exp.get('op')!r}")
            elif exp.get("path") != "$text":
                try:
                    path_tokens(str(exp["path"]))
                except ValueError as exc:
                    errs.append(str(exc))
    if rtype == "lesson" and status == "active" and not (meta.get("evidence") or {}).get("pull_request"):
        errs.append("active lesson requires evidence.pull_request")
    if status == "superseded" and not meta.get("superseded_by"):
        errs.append("superseded record requires superseded_by")
    return errs


def _is_fleet_wide(clusters: List[str]) -> bool:
    return not clusters or clusters == ["*"]


def _suppressed_files(records: List[Dict[str, Any]], target_files: List[str]) -> Dict[str, set]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for rec in records:
        if str(rec["meta"].get("status") or "active") != "active":
            continue
        exp = (rec["meta"].get("guard") or {}).get("expect")
        if not exp:
            continue
        for mf in target_files:
            if match_glob(str(exp["file"]), mf):
                grouped.setdefault((mf, str(exp["path"])), []).append(rec)
    suppressed: Dict[str, set] = {}
    for (mf, _yp), group in grouped.items():
        if len(group) <= 1:
            continue
        clusters_of = lambda r: [str(x) for x in ((r["meta"].get("scope") or {}).get("clusters") or [])]
        if any(not _is_fleet_wide(clusters_of(r)) for r in group) and any(_is_fleet_wide(clusters_of(r)) for r in group):
            for r in group:
                if _is_fleet_wide(clusters_of(r)):
                    suppressed.setdefault(str(r["meta"]["id"]), set()).add(mf)
    return suppressed


def render_markdown(findings: List[Dict[str, Any]]) -> str:
    if not findings:
        return "No recorded decision or lesson governs the files this change touches."
    lines = ["### Prior decisions and lessons this change touches", ""]
    icons = {"violates": "⛔", "consistent": "✅", "related": "ℹ️"}
    for f in findings:
        ev = []
        if f.get("breaking_pr"):
            ev.append(f"breaking PR: {f['breaking_pr']}")
        if f.get("incident"):
            ev.append(f"incident: {f['incident']}")
        if f.get("pull_request"):
            ev.append(f"learned in: {f['pull_request']}")
        if f.get("rejected_proposals"):
            ev.append("rejected proposals: " + ", ".join(str(x) for x in f["rejected_proposals"]))
        ev_str = f" ({', '.join(ev)})" if ev else ""
        lines.append(f"- {icons[f['verdict']]} **{f['id']}** ({f['type']}, status={f['status']}): {f['title']}{ev_str}")
        guard = f.get("guard") or {}
        if guard.get("rule"):
            lines.append(f"  - Guard ({guard.get('severity', 'warn')}): {guard['rule']}")
        for g in f.get("guard_results") or []:
            if not g["ok"]:
                what = "deleted" if g.get("deleted") else f"is now `{g['actual']}`"
                lines.append(f"  - `{g['file']}` {what}")
        if f.get("body"):
            for bline in str(f["body"]).splitlines():
                lines.append(f"  > {bline}")
    return "\n".join(lines)


def check_changed_files(knowledge_root: Path, changed: Dict[str, Optional[str]]) -> Dict[str, Any]:
    records, active_errors = load_okf_records(knowledge_root)
    if active_errors:
        raise ValueError("; ".join(active_errors))
    candidates = [r for r in records if str(r["meta"].get("status") or "active") in ("active", "proposed")]
    suppressed = _suppressed_files(candidates, sorted(changed))
    new_resources: Dict[str, List[str]] = {}
    for fpath, content in changed.items():
        if content:
            try:
                new_resources[fpath] = [
                    rid for d in yaml.safe_load_all(content) if (rid := _doc_resource_id(d))
                ]
            except yaml.YAMLError:
                new_resources[fpath] = []

    findings: List[Dict[str, Any]] = []
    for rec in candidates:
        meta = rec["meta"]
        rid = str(meta["id"])
        status = str(meta.get("status") or "active")
        sc = meta.get("scope") or {}
        s_paths = [str(x) for x in (sc.get("paths") or [])]
        s_res = [str(x) for x in (sc.get("resources") or [])]
        sup_for_rec = suppressed.get(rid, set())
        hits = set()
        for fpath in changed:
            if fpath in sup_for_rec:
                continue
            if any(match_glob(g, fpath) for g in s_paths):
                hits.add(fpath)
            for res_id in new_resources.get(fpath, []):
                if any(match_glob(g, res_id) for g in s_res):
                    hits.add(fpath)
        if not hits:
            continue
        verdict = "related"
        detail: List[Dict[str, Any]] = []
        guard = meta.get("guard") or {}
        exp = guard.get("expect")
        if exp:
            present_files = {p: c for p, c in changed.items() if c is not None and p in hits}
            detail = evaluate_guard(exp, present_files, s_res)
            deleted = [p for p in sorted(hits) if changed.get(p) is None and match_glob(str(exp["file"]), p)]
            if any(not d["ok"] for d in detail) or deleted:
                verdict = "violates"
                detail += [{"file": p, "actual": None, "ok": False, "deleted": True} for p in deleted]
            elif detail:
                verdict = "consistent"
        if verdict == "violates":
            raw_sev = str(guard.get("severity") or "warn")
            severity = "warn" if (raw_sev == "block" and status == "proposed") else raw_sev
        else:
            severity = "info"
        ev = meta.get("evidence") or {}
        findings.append({
            "id": rid,
            "type": str(meta.get("type") or ""),
            "status": status,
            "title": str(meta.get("title") or ""),
            "path": rec["path"],
            "verdict": verdict,
            "severity": severity,
            "files": sorted(hits),
            "guard": guard or None,
            "guard_results": detail,
            "pull_request": ev.get("pull_request"),
            "breaking_pr": ev.get("breaking_pr"),
            "incident": ev.get("incident"),
            "rejected_proposals": ev.get("rejected_proposals"),
            "supersedes": meta.get("supersedes"),
            "superseded_by": meta.get("superseded_by"),
            "body": rec["body"],
        })
    order = {"block": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["id"]))
    verdict = (
        "block"
        if any(f["severity"] == "block" for f in findings)
        else "warn"
        if any(f["severity"] == "warn" for f in findings)
        else "ok"
    )
    return {
        "status": "found" if findings else "no_match",
        "verdict": verdict,
        "changed": sorted(changed),
        "findings": findings,
        "markdown": render_markdown(findings),
    }

#!/usr/bin/env python3
"""Deterministic Open Knowledge Format (OKF) guard evaluator for VCS publish."""

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
SKIP_FILENAMES = frozenset({"index.md", "log.md", "readme.md", "template.md", "state_of_being.md"})
RECORD_ID_RE = re.compile(r"(DEC|LES)-\d{4}-\d{3,}")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
QTY_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)$")
PATH_SEG_RE = re.compile(r"([^\[]+)(?:\[([^=\]]+)=([^\]]+)\])?")
POD_SPEC_WORKLOAD_KINDS = frozenset({
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "ReplicaSet",
    "ReplicationController",
})
REPLICA_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "ReplicaSet"})
OVERSIZE_WILDCARD_ALL_GLOBS = frozenset({
    "*",
    "**",
    "**/*",
    "*.yaml",
    "*.yml",
    "**/*.yaml",
    "**/*.yml",
})
LOCKFILE_NAMES = frozenset({
    "pnpm-lock.yaml",
    "pnpm-lock.yml",
    "yarn.lock.yaml",
    "package-lock.yaml",
    "Chart.lock",
    "flake.lock",
})
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
    pat_segs = pat.split("/") if pat else []
    tgt_segs = tgt.split("/") if tgt else []

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


def _doc_resource_ids(doc: Any) -> List[str]:
    if not isinstance(doc, dict):
        return []
    kind = str(doc.get("kind") or "").strip()
    md = doc.get("metadata")
    if not kind or not isinstance(md, dict):
        return []
    name = str(md.get("name") or "").strip()
    if not name:
        return []
    ns = str(md.get("namespace") or "").strip()
    with_ns = f"{kind}/{ns}/{name}"
    if not ns:
        return [with_ns, f"{kind}/{name}"]
    return [with_ns]


def _doc_resource_id(doc: Any) -> Optional[str]:
    ids = _doc_resource_ids(doc)
    return ids[0] if ids else None


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
    if path_expr == "spec.replicas" or path_expr.startswith((
        "spec.replicas.",
        "spec.strategy.",
        "spec.minReadySeconds",
        "spec.revisionHistoryLimit",
        "spec.progressDeadlineSeconds",
    )):
        return kind in REPLICA_WORKLOAD_KINDS
    if path_expr.startswith(("spec.serviceName", "spec.volumeClaimTemplates", "spec.podManagementPolicy")):
        return kind == "StatefulSet"
    if path_expr.startswith(("spec.schedule", "spec.concurrencyPolicy", "spec.startingDeadlineSeconds")):
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
                if any(
                    match_glob(g, rid)
                    for rid in _doc_resource_ids(d)
                    for g in scope_resources
                )
            ]
            if scoped:
                docs = scoped
        if len(docs) > 1:
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
    raw_scope = meta.get("scope")
    scope = dict(raw_scope) if isinstance(raw_scope, dict) else {}
    paths = [str(x) for x in (scope.get("paths") if isinstance(scope.get("paths"), list) else [])]
    resources = [str(x) for x in (scope.get("resources") if isinstance(scope.get("resources"), list) else [])]
    clusters = [str(x) for x in (scope.get("clusters") if isinstance(scope.get("clusters"), list) else [])]
    checks = [str(x) for x in (scope.get("checks") if isinstance(scope.get("checks"), list) else [])]
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
    raw_sc = meta.get("scope")
    if raw_sc is not None and not isinstance(raw_sc, dict):
        errs.append("scope must be a mapping")
        sc: Dict[str, Any] = {}
    else:
        sc = raw_sc or {}
    if not any(isinstance(sc.get(k), list) and sc.get(k) for k in ("paths", "resources", "checks")):
        errs.append("scope is empty")
    raw_guard = meta.get("guard")
    if raw_guard is not None and not isinstance(raw_guard, dict):
        errs.append("guard must be a mapping")
        guard: Dict[str, Any] = {}
    else:
        guard = raw_guard or {}
    if guard:
        if guard.get("severity") and guard["severity"] not in SEVERITIES:
            errs.append(f"invalid guard.severity {guard['severity']!r}")
        exp = guard.get("expect")
        if exp is not None and not isinstance(exp, dict):
            errs.append("guard.expect must be a mapping")
        elif exp:
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
    raw_ev = meta.get("evidence")
    if raw_ev is not None and not isinstance(raw_ev, dict):
        errs.append("evidence must be a mapping")
        ev_dict: Dict[str, Any] = {}
    else:
        ev_dict = raw_ev or {}
    if rtype == "lesson" and status == "active" and not ev_dict.get("pull_request"):
        errs.append("active lesson requires evidence.pull_request")
    raw_sup = meta.get("supersedes")
    if raw_sup is not None and not isinstance(raw_sup, list):
        errs.append("supersedes must be a list")
    if status == "superseded" and not meta.get("superseded_by"):
        errs.append("superseded record requires superseded_by")
    return errs


def _is_fleet_wide(clusters: List[str]) -> bool:
    return not clusters or clusters == ["*"]


def _target_file_matches_narrow_cluster(target_file: str, clusters: List[str]) -> bool:
    if _is_fleet_wide(clusters):
        return True
    segs = [s for s in target_file.strip("/").split("/") if s]
    for raw_c in clusters:
        c = str(raw_c or "").strip()
        if not c or c == "*":
            continue
        for seg in segs:
            if seg == c or c.endswith("-" + seg):
                return True
    return False


def _precedence_suppression(
    records: List[Dict[str, Any]], target_files: List[str]
) -> Tuple[Dict[str, set], Dict[str, Dict[str, List[str]]]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    clusters_of = lambda r: [str(x) for x in ((r["meta"].get("scope") or {}).get("clusters") or [])]
    resources_of = lambda r: [str(x) for x in ((r["meta"].get("scope") or {}).get("resources") or [])]
    for rec in records:
        exp = (rec["meta"].get("guard") or {}).get("expect")
        if not isinstance(exp, dict) or not exp.get("path") or not exp.get("file"):
            continue
        rec_clusters = clusters_of(rec)
        for mf in target_files:
            if not match_glob(str(exp["file"]), mf):
                continue
            if not _is_fleet_wide(rec_clusters) and not _target_file_matches_narrow_cluster(mf, rec_clusters):
                continue
            grouped.setdefault((mf, str(exp["path"])), []).append(rec)
    whole_file: Dict[str, set] = {}
    excluded_resources: Dict[str, Dict[str, List[str]]] = {}
    for (mf, _yp), group in grouped.items():
        if len(group) <= 1:
            continue
        has_unscoped_active_narrow = False
        scoped_narrow_resources: List[str] = []
        has_broad = False
        for r in group:
            r_clusters = clusters_of(r)
            if _is_fleet_wide(r_clusters):
                has_broad = True
            elif str(r["meta"].get("status") or "active") == "active":
                r_res = resources_of(r)
                if not r_res:
                    has_unscoped_active_narrow = True
                else:
                    scoped_narrow_resources.extend(r_res)
        if not has_broad or (not has_unscoped_active_narrow and not scoped_narrow_resources):
            continue
        for r in group:
            if not _is_fleet_wide(clusters_of(r)):
                continue
            rid = str(r["meta"]["id"])
            if has_unscoped_active_narrow:
                whole_file.setdefault(rid, set()).add(mf)
            else:
                excluded_resources.setdefault(rid, {}).setdefault(mf, []).extend(scoped_narrow_resources)
    return whole_file, excluded_resources


def _suppressed_files(records: List[Dict[str, Any]], target_files: List[str]) -> Dict[str, set]:
    return _precedence_suppression(records, target_files)[0]


def _filter_yaml_content_excluding_resources(
    content: str, excluded_globs: List[str]
) -> Tuple[str, bool]:
    if not excluded_globs:
        return content, True
    try:
        docs = [d for d in yaml.safe_load_all(content) if d is not None]
    except yaml.YAMLError:
        return content, True
    if not docs:
        return content, True
    kept: List[str] = []
    for d in docs:
        covered = any(
            match_glob(g, rid)
            for rid in _doc_resource_ids(d)
            for g in excluded_globs
        )
        if not covered:
            kept.append(yaml.safe_dump(d, sort_keys=False, allow_unicode=True).strip())
    if not kept:
        return "", False
    return "\n---\n".join(kept) + "\n", True


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


def check_changed_files(
    knowledge_root: Path,
    changed: Dict[str, Optional[str]],
    oversize_files: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    records, active_errors = load_okf_records(knowledge_root)
    if active_errors:
        raise ValueError("; ".join(active_errors))
    oversize_set = set(oversize_files or ())
    candidates = [r for r in records if str(r["meta"].get("status") or "active") in ("active", "proposed")]
    whole_suppressed, excluded_resources = _precedence_suppression(
        candidates, [p for p in sorted(changed) if p not in oversize_set]
    )
    new_resources: Dict[str, List[str]] = {}
    for fpath, content in changed.items():
        if content:
            try:
                rids: List[str] = []
                for d in yaml.safe_load_all(content):
                    rids.extend(_doc_resource_ids(d))
                new_resources[fpath] = rids
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
        s_clusters = [str(x) for x in (sc.get("clusters") or [])]
        guard = meta.get("guard") or {}
        exp = guard.get("expect") if isinstance(guard, dict) else None
        sup_for_rec = whole_suppressed.get(rid, set())
        excl_for_rec = excluded_resources.get(rid, {})
        filtered_content_by_file: Dict[str, str] = {}
        hits = set()
        for fpath in changed:
            if fpath in oversize_set:
                if (
                    not _is_fleet_wide(s_clusters)
                    and fpath.strip("/").startswith("clusters/")
                    and not _target_file_matches_narrow_cluster(fpath, s_clusters)
                ):
                    continue
                exp_file = str(exp.get("file") or "").strip() if isinstance(exp, dict) else ""
                if exp_file and not match_glob(exp_file, fpath):
                    continue
                bname = Path(fpath).name
                path_matched = any(match_glob(g, fpath) for g in s_paths)
                exp_file_specific_matched = bool(
                    exp_file and exp_file not in OVERSIZE_WILDCARD_ALL_GLOBS and match_glob(exp_file, fpath)
                )
                is_lockfile = (
                    bname in LOCKFILE_NAMES
                    and not fpath.strip("/").startswith("clusters/")
                    and not path_matched
                    and not exp_file_specific_matched
                )
                res_yaml_matched = bool(
                    s_res
                    and fpath.lower().endswith((".yaml", ".yml"))
                    and not is_lockfile
                    and (_is_fleet_wide(s_clusters) or _target_file_matches_narrow_cluster(fpath, s_clusters))
                )
                if path_matched or exp_file_specific_matched or res_yaml_matched:
                    hits.add(fpath)
                continue
            if fpath in sup_for_rec:
                continue
            excl_globs = excl_for_rec.get(fpath, [])
            file_rids = new_resources.get(fpath, [])
            if excl_globs and changed.get(fpath) is not None:
                filt, rem = _filter_yaml_content_excluding_resources(str(changed[fpath]), excl_globs)
                if not rem:
                    continue
                filtered_content_by_file[fpath] = filt
                try:
                    file_rids = []
                    for d in yaml.safe_load_all(filt):
                        file_rids.extend(_doc_resource_ids(d))
                except yaml.YAMLError:
                    file_rids = []
            if (
                not _is_fleet_wide(s_clusters)
                and s_paths
                and fpath.strip("/").startswith("clusters/")
                and not _target_file_matches_narrow_cluster(fpath, s_clusters)
            ):
                pass
            elif any(match_glob(g, fpath) for g in s_paths):
                hits.add(fpath)
            for res_id in file_rids:
                if any(match_glob(g, res_id) for g in s_res):
                    hits.add(fpath)
        if not hits:
            continue
        verdict = "related"
        detail: List[Dict[str, Any]] = []
        oversize_hits = [p for p in sorted(hits) if p in oversize_set]
        if exp:
            present_files = {
                p: filtered_content_by_file.get(p, str(c))
                for p, c in changed.items()
                if c is not None and p in hits and p not in oversize_set
            }
            detail = evaluate_guard(exp, present_files, s_res)
            deleted = [
                p
                for p in sorted(hits)
                if p not in oversize_set and changed.get(p) is None and match_glob(str(exp["file"]), p)
            ]
            if any(not d["ok"] for d in detail) or deleted or oversize_hits:
                verdict = "violates"
                detail += [{"file": p, "actual": None, "ok": False, "deleted": True} for p in deleted]
                detail += [{"file": p, "actual": "<oversize>", "ok": False, "oversize": True} for p in oversize_hits]
            elif detail:
                verdict = "consistent"
        elif oversize_hits and guard:
            verdict = "violates"
            detail += [{"file": p, "actual": "<oversize>", "ok": False, "oversize": True} for p in oversize_hits]
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

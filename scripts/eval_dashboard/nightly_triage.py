#!/usr/bin/env python3
"""Triage a nightly-red issue with a model before a person gets it.

On 2026-11-09 the bug filer (``nightly_bugs.py``) opened 12 issues, 10 of
them for one grader change (#4973, fixed by #5179), and sent them to five
case owners, each of whom diagnosed the same cause on their own. The filer
routes by who owns the case, which is the wrong person whenever the case is
not at fault. This step reads what a triager would read and routes by the
cause instead.

Armed (the filer runs with ``--triage-pending``, docs/ci-health.md "Nightly
triage"), the filer files a domain or group bug unassigned, mentions no one,
and leaves a marker naming who its own rule would assign
(``nightly_bugs.TRIAGE_PENDING_MARKER``). This module then works in two
phases, each its own workflow step because each needs a different identity:

``prepare`` (as the nightly-log reader) gathers the evidence for each pending
issue into a JSON file on the runner's disk: every failing repetition's
grading line on the case's red nights and the tail of a few transcripts
(the private logs the issue links), the last green and first red nightly
commits (``head_sha`` in data.json), the commits and pull requests between
them (``git log`` on a full-history checkout, the titles and authors from
GitHub), and the pull requests that may already fix it: open ones touching
the same files, and ones merged since the first red night.

``route`` (as the publisher, which may call Vertex AI) asks the model for a
classification, a suspected pull request, an open or merged fix, a priority
and a confidence (VERDICT_SCHEMA), checks its answer against the evidence,
and routes (``route``):

    a suspected pull request from the window, confidence not low, class
    product bug or grader/harness   -> that pull request's author
    grader/harness, no such PR      -> ``infra_owner`` in domains.yaml
    infra                           -> ``infra_owner``
    a case problem, low confidence,
    or anything that went wrong     -> the filer's rule (the marker's logins)

It assigns the first login GitHub accepts (the marker's chain behind it),
posts one comment with the verdict and the evidence and @-mentions the people
the filer would have, then marks the issue done (``triage:agent`` label,
TRIAGE_DONE_MARKER in the body). The comment goes to the issues repository
only (KUBE_AGENTS_ISSUES_REPO, private), so it may name cases and quote
grading lines; nothing this module prints to the job log does.

**Fail safe.** A model call that fails, times out, runs past its token cap
or answers something unparseable routes by the filer's rule, and the comment
says so in one line. An issue the prepare phase did not reach is retried next
tick, and one still pending PENDING_MAX_AGE after it was filed is routed by
the rule without a model call. At most MAX_ISSUES_PER_RUN issues are triaged
per run and each prompt is capped at MAX_INPUT_TOKENS (transcripts are cut
first, then the oldest commits, then grading lines).

**Dry run is the default.** Unarmed, the workflow runs both phases on the
issues the filer filed that tick (``--issue``), calls the model, and prints
one line per issue -- the classification, the pull request numbers, the
priority, the confidence and who it would assign -- with no case name and no
grading text: the job log is public. ``--no-model`` routes every pending
issue by the rule without calling the model; the workflow uses it, live,
to drain issues left pending after triage is switched off.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

try:
    from eval_dashboard import classify, gate_issue, ghcli, nightly_bugs
except ImportError:  # run as a script: scripts/eval_dashboard/nightly_triage.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import classify, gate_issue, ghcli, nightly_bugs

REPO_ROOT = nightly_bugs.REPO_ROOT

# --- The model. Triage handles a few issues a day, so the strongest model
# the pool runs, not the cheapest; NIGHTLY_TRIAGE_MODEL (a repository
# variable the workflow passes through) overrides it.
DEFAULT_MODEL = "gemini-4-argon"
MODEL_ENV = "NIGHTLY_TRIAGE_MODEL"
# The project the call is billed to, which must grant the publisher
# roles/aiplatform.user (docs/ci-health.md, "Nightly triage").
DEFAULT_PROJECT = "kube-agents-prow"
PROJECT_ENV = "NIGHTLY_TRIAGE_PROJECT"
LOCATION = "global"
VERTEX_URL = "https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent"
TOKEN_COMMAND = ("gcloud", "auth", "print-access-token")
MODEL_TIMEOUT_S = 180

# --- The caps.
MAX_ISSUES_PER_RUN = 5
# The prompt's budget, at CHARS_PER_TOKEN characters a token (an estimate
# that runs high for English and code, so the real count stays under).
MAX_INPUT_TOKENS = 60_000
CHARS_PER_TOKEN = 4
# The answer's ceiling, thinking included: the verdict is a few hundred
# tokens; the rest is room to reason before it.
MAX_OUTPUT_TOKENS = 8_192
MAX_CASES = 12
MAX_COMMITS = 80
MAX_LATER_COMMITS = 30
MAX_FILES = 200
MAX_TRANSCRIPTS = 3
TRANSCRIPT_TAIL_BYTES = 16_000
OPEN_PRS_READ = 100
# An issue still pending this long after it was filed is routed by the rule
# without a model call: the step has failed on it every tick since.
PENDING_MAX_AGE = datetime.timedelta(hours=2)

# --- The issue.
PENDING_LABEL = nightly_bugs.TRIAGE_PENDING_LABEL
AGENT_LABEL = "triage:agent"
PENDING_MARKER_RE = re.compile(r"<!-- kube-agents-bot:triage-pending assign=(\S+) cc=(\S+) -->")
TRIAGE_DONE_MARKER = "<!-- kube-agents-bot:triage-done assign={assign} cc={cc} -->"
TRIAGED_ADDRESSEE = "**Triaged:** the agent's comment below says what this is and who has it."
COMMENT_MARKER = "<!-- kube-agents-bot:nightly-triage assignee={assignee} -->"
COMMENT_MARKER_RE = re.compile(r"<!-- kube-agents-bot:nightly-triage assignee=(\S+) -->")
NOBODY = "-"
ISSUE_PATH = "issues/{number}"
ASSIGNEES_PATH = "issues/{number}/assignees"
OPEN_RED_PATH = "issues?labels={label}&state=open&per_page=100"
PR_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

CLASSES = {"product": "product bug", "grader_or_harness": "grader or harness", "infra": "infra", "case_rework": "case needs rework"}
CONFIDENCES = ("high", "medium", "low")
PRIORITIES = ("P0", "P1", "P2", "P3")
FIX_STATES = ("open", "merged", "none")
VERDICT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "classification": {"type": "STRING", "enum": list(CLASSES)},
        "suspected_pr": {"type": "INTEGER", "nullable": True},
        "fix_pr": {"type": "INTEGER", "nullable": True},
        "fix_state": {"type": "STRING", "enum": list(FIX_STATES)},
        "priority": {"type": "STRING", "enum": list(PRIORITIES)},
        "confidence": {"type": "STRING", "enum": list(CONFIDENCES)},
        "evidence": {"type": "STRING"},
    },
    "required": ["classification", "suspected_pr", "fix_pr", "fix_state", "priority", "confidence", "evidence"],
}
MAX_EVIDENCE_CHARS = 2_000

INSTRUCTIONS = """\
You triage a failure of the nightly eval run of gke-labs/kube-agents, a Kubernetes agent harness. \
The bench (bench/) runs each eval case against the agent on `main` every night and grades it; a case \
that failed every graded repetition on three graded nights in a row was filed as the issue below. \
Several cases in one issue turned red on the same night with a shared failing check.

Decide what the bug really is, from the evidence only:
- classification: "product" when the agent or the product code regressed and the case is right; \
"grader_or_harness" when the grading code, the bench harness or the eval scripts misjudge or break \
a run that is otherwise fine; "infra" when pool projects, quota, clusters or Prow kept the run from \
working; "case_rework" when the case's own expectations are wrong, stale or flaky.
- suspected_pr: the number of the pull request in the commit window (between the last green and the \
first red nightly commit) that most likely caused it, or null. Pick only from the window's pull requests.
- fix_pr and fix_state: a pull request from the candidate fixes (open ones touching the same files, or \
merged since the first red night) that already fixes it, with "open" or "merged"; null and "none" otherwise.
- priority: P0 blocks a release or breaks most cases; P1 a real regression across several cases; \
P2 one case; P3 cosmetic.
- confidence: "high" when the grading lines and one pull request's change explain each other; \
"medium" when that is probable; "low" when you are guessing.
- evidence: at most five sentences a maintainer can check: what the grading lines say, and what in the \
suspected pull request explains it.

Answer with the JSON object only."""


def log(message: str) -> None:
    print(message, file=sys.stderr)


class ModelError(RuntimeError):
    """The model call failed, timed out, or answered nothing usable."""


# --------------------------------------------------------------------------
# the model


class VertexModel:
    """Gemini on Vertex AI, with the job's own credential (the publisher's
    Workload Identity, through ``gcloud auth print-access-token``): no key."""

    def __init__(self, project: str, model: str, runner=subprocess.run, opener=urllib.request.urlopen):
        self.project, self.model, self.runner, self.opener = project, model, runner, opener

    def token(self) -> str:
        try:
            proc = self.runner(list(TOKEN_COMMAND), capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ModelError(f"no access token ({type(exc).__name__})") from exc
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            raise ModelError("no access token (gcloud auth print-access-token failed)")
        return proc.stdout.strip()

    def __call__(self, prompt: str, max_output_tokens: int) -> str:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": VERDICT_SCHEMA,
                                 "maxOutputTokens": max_output_tokens, "temperature": 0},
        }
        request = urllib.request.Request(
            VERTEX_URL.format(project=self.project, location=LOCATION, model=self.model),
            data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=MODEL_TIMEOUT_S) as response:
                answer = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise ModelError(f"the model call failed (HTTP {exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelError(f"the model call failed ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise ModelError("the model's response was not JSON") from exc
        candidate = ((answer.get("candidates") or [{}])[0]) if isinstance(answer, dict) else {}
        if candidate.get("finishReason") not in (None, "STOP"):
            raise ModelError(f"the model stopped early ({candidate.get('finishReason')})")
        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict) and not p.get("thought"))
        if not text.strip():
            raise ModelError("the model answered nothing")
        return text


# --------------------------------------------------------------------------
# prepare: the evidence


def run_git(repo_root: pathlib.Path, args: list[str]) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def gcs_tail(path: str, nbytes: int = TRANSCRIPT_TAIL_BYTES) -> str | None:
    """The last ``nbytes`` of a gs:// object, as the active account."""
    try:
        proc = subprocess.run(["gcloud", "storage", "cat", f"--range=-{nbytes}", path], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


def last_red_block(verdicts: list[dict]) -> tuple[list[dict], dict | None]:
    """``(red nights, last green before them)``: the newest run of failing
    nights in the window (current or since recovered), oldest first, and the
    newest passing night before it, None when the window holds none."""
    last = max((i for i, v in enumerate(verdicts) if v["outcome"] == classify.OUTCOME_FAILED), default=None)
    if last is None:
        return [], None
    first = last
    while first > 0 and verdicts[first - 1]["outcome"] == classify.OUTCOME_FAILED:
        first -= 1
    green = next((v for v in reversed(verdicts[:first]) if v["outcome"] == classify.OUTCOME_PASSED), None)
    return verdicts[first:last + 1], green


def window(cases: list[dict]) -> tuple[dict | None, dict | None]:
    """``(green, red)`` nights bounding the commits that broke ``cases``: the
    culprit landed after each case's last green and before each case's first
    red, so the newest green and the oldest first red; when those disagree
    (a case went red before another's last green), the widest span."""
    greens = [c["last_green"] for c in cases if c.get("last_green") and c["last_green"].get("head_sha")]
    reds = [c["red_nights"][0] for c in cases if c.get("red_nights") and c["red_nights"][0].get("head_sha")]
    if not reds:
        return None, None
    green = max(greens, key=lambda v: v["date"], default=None)
    red = min(reds, key=lambda v: v["date"])
    if green and green["date"] >= red["date"]:
        green, red = min(greens, key=lambda v: v["date"]), max(reds, key=lambda v: v["date"])
    return green, red


def parse_log(text: str | None) -> list[dict]:
    """``git log --format=%x1e%h%x1f%an%x1f%s --name-only`` into
    ``[{sha, author_name, subject, pr, files}]``."""
    out = []
    for record in (text or "").split("\x1e"):
        if not record.strip():
            continue
        head, _, rest = record.partition("\n")
        fields = head.split("\x1f")
        if len(fields) != 3:
            continue
        sha, author, subject = fields
        pr = PR_SUBJECT_RE.search(subject)
        out.append({"sha": sha, "author_name": author, "subject": subject, "pr": int(pr.group(1)) if pr else None,
                    "files": [f for f in rest.splitlines() if f.strip()]})
    return out


LOG_FORMAT = "--format=%x1e%h%x1f%an%x1f%s"
PRS_QUERY = "query { repository(owner: %(owner)s, name: %(name)s) { %(fields)s } }"
PR_FIELD = "p%(n)d: pullRequest(number: %(n)d) { number title author { login } mergedAt }"
OPEN_PRS_QUERY = """\
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: FIRST, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number title author { login } files(first: 100) { nodes { path } } }
    }
  }
}""".replace("FIRST", str(OPEN_PRS_READ))


class Preparer:
    """The evidence for one issue (module docstring, ``prepare``)."""

    def __init__(self, data: dict, source_gh: ghcli.Gh, repo_root: pathlib.Path = REPO_ROOT, git=None, gcs=gcs_tail):
        self.nights = nightly_bugs.graded_nights(data)
        self.source_gh = source_gh
        self.repo_root = repo_root
        self.git = git or (lambda args: run_git(repo_root, args))
        self.gcs = gcs
        self._open_prs = None

    def bundle(self, issue: dict) -> dict:
        notes: list[str] = []
        names = nightly_bugs.group_issue_cases(issue)
        if len(names) > MAX_CASES:
            notes.append(f"{len(names)} cases; the first {MAX_CASES} are shown")
        cases = []
        for name in names[:MAX_CASES]:
            red, green = last_red_block(self.nights.get(name) or [])
            if not red:
                notes.append(f"{name}: no failing night in the window")
                continue
            cases.append({"case": name, "last_green": self.night(green) if green else None, "red_nights": [self.night(v) for v in red]})
        transcripts = self.transcripts(names, notes)
        green, red = window(cases)
        commits, later = self.commits(green, red, notes)
        numbers = sorted({c["pr"] for c in commits + later if c["pr"]})
        prs = self.pull_requests(numbers, notes)
        files = sorted({f for c in commits for f in c["files"]})[:MAX_FILES]
        return {
            "issue": issue.get("number"), "title": issue.get("title"), "cases": cases, "transcripts": transcripts,
            "green": green, "red": red, "commits": commits, "later": later, "prs": prs,
            "open_prs": self.open_prs_touching(files, notes), "notes": notes,
        }

    @staticmethod
    def night(v: dict) -> dict:
        return {"date": v["date"], "head_sha": v.get("head_sha"), "run_url": v.get("run_url"),
                "fails": [f for f in v.get("fails") or [] if isinstance(f, dict)]}

    def transcripts(self, names: list[str], notes: list[str]) -> list[dict]:
        """The tail of the first failing repetition's transcript on each of
        the first MAX_TRANSCRIPTS cases' first red night."""
        out = []
        for name in names:
            if len(out) >= MAX_TRANSCRIPTS:
                break
            red, _ = last_red_block(self.nights.get(name) or [])
            night = red[0] if red else None
            fails = (night or {}).get("fails") or []
            base = (night or {}).get("gcs_base")
            if not night or not base or not fails or not isinstance(fails[0].get("n"), int):
                continue
            path = f"{base.rstrip('/')}/artifacts/eval_{name}_rep{fails[0]['n']}.log"
            text = self.gcs(path)
            if text is None:
                notes.append(f"{name}: transcript not readable")
                continue
            out.append({"case": name, "night": night["date"], "rep": fails[0]["n"], "tail": text})
        return out

    def commits(self, green: dict | None, red: dict | None, notes: list[str]) -> tuple[list[dict], list[dict]]:
        """The commits in the window, newest first, and the commits merged
        since the first red night that touch the same files."""
        red_sha = (red or {}).get("head_sha")
        if not red_sha or not SHA_RE.match(red_sha):
            notes.append("no first-red commit on record")
            return [], []
        green_sha = (green or {}).get("head_sha")
        span = f"{green_sha}..{red_sha}" if green_sha and SHA_RE.match(green_sha) else red_sha
        if span == red_sha:
            notes.append(f"no green night in the window: the {MAX_COMMITS} commits up to the first red one")
        commits = parse_log(self.git(["log", "--no-merges", LOG_FORMAT, "--name-only", f"-n{MAX_COMMITS}", span]))
        if not commits:
            notes.append(f"git log {span} read nothing")
            return [], []
        files = sorted({f for c in commits for f in c["files"]})[:MAX_FILES]
        later = parse_log(self.git(["log", "--no-merges", LOG_FORMAT, "--name-only", f"-n{MAX_LATER_COMMITS}", f"{red_sha}..HEAD", "--", *files])) if files else []
        return commits, later

    def pull_requests(self, numbers: list[int], notes: list[str]) -> dict:
        """``{number: {title, author, merged_at}}`` from one GraphQL read."""
        if not numbers:
            return {}
        owner, _, name = ghcli.SOURCE_REPO.partition("/")
        query = PRS_QUERY % {"owner": json.dumps(owner), "name": json.dumps(name),
                             "fields": " ".join(PR_FIELD % {"n": n} for n in numbers)}
        found = self.source_gh.call("POST", "graphql", {"query": query}, read=True)
        repo = ((found or {}).get("data") or {}).get("repository") if isinstance(found, dict) else None
        if not isinstance(repo, dict):
            notes.append("the pull requests' titles and authors could not be read")
            return {}
        out = {}
        for node in repo.values():
            if isinstance(node, dict) and isinstance(node.get("number"), int):
                out[str(node["number"])] = {"title": node.get("title"), "author": (node.get("author") or {}).get("login"), "merged_at": node.get("mergedAt")}
        return out

    def open_prs_touching(self, files: list[str], notes: list[str]) -> list[dict]:
        if not files:
            return []
        if self._open_prs is None:
            owner, _, name = ghcli.SOURCE_REPO.partition("/")
            found = self.source_gh.call("POST", "graphql", {"query": OPEN_PRS_QUERY, "variables": {"owner": owner, "name": name}}, read=True)
            nodes = ((((found or {}).get("data") or {}).get("repository") or {}).get("pullRequests") or {}).get("nodes") if isinstance(found, dict) else None
            self._open_prs = nodes if isinstance(nodes, list) else []
            if not isinstance(nodes, list):
                notes.append("the open pull requests could not be read")
        wanted = set(files)
        out = []
        for pr in self._open_prs:
            paths = [n.get("path") for n in ((pr.get("files") or {}).get("nodes") or []) if isinstance(n, dict)]
            shared = sorted(wanted.intersection(paths))
            if shared:
                out.append({"number": pr.get("number"), "title": pr.get("title"), "author": (pr.get("author") or {}).get("login"), "files": shared})
        return out


# --------------------------------------------------------------------------
# route: the verdict, the routing, the writes


def build_prompt(bundle: dict, max_chars: int = MAX_INPUT_TOKENS * CHARS_PER_TOKEN) -> str | None:
    """The prompt for ``bundle``, cut to ``max_chars``: transcripts first
    (halved, then dropped), then the oldest commits, then grading lines;
    None when even the bare evidence does not fit."""
    transcripts = [dict(t) for t in bundle.get("transcripts") or []]
    commits = list(bundle.get("commits") or [])
    fails_cap = None
    while True:
        prompt = render_prompt(bundle, transcripts, commits, fails_cap)
        if len(prompt) <= max_chars:
            return prompt
        if any(len(t["tail"]) > 2_000 for t in transcripts):
            for t in transcripts:
                t["tail"] = t["tail"][-max(2_000, len(t["tail"]) // 2):]
        elif transcripts:
            transcripts.pop()
        elif len(commits) > 10:
            commits = commits[:len(commits) // 2]
        elif fails_cap is None or fails_cap > 1:
            fails_cap = 3 if fails_cap is None else fails_cap - 1
        else:
            return None


def render_prompt(bundle: dict, transcripts: list[dict], commits: list[dict], fails_cap: int | None) -> str:
    prs = bundle.get("prs") or {}
    lines = [INSTRUCTIONS, "", f"## Issue: {bundle.get('title') or ''}", ""]
    for case in bundle.get("cases") or []:
        green = case.get("last_green")
        lines.append(f"### Case {case['case']}: last green {green['date'] + ' at ' + str(green['head_sha']) if green else 'not in the window'}")
        for night in case["red_nights"]:
            lines.append(f"- red {night['date']} at {night['head_sha']}:")
            seen = []
            for fail in night["fails"]:
                reason = fail.get("reason") or "(no grading line)"
                if reason not in seen:
                    seen.append(reason)
            for reason in seen[:fails_cap]:
                lines.append(f"  - {reason}")
    green, red = bundle.get("green"), bundle.get("red")
    lines += ["", (f"## Commit window: last green {green['head_sha'] if green else 'unknown'} ({green['date'] if green else '-'})"
                   f" .. first red {red['head_sha'] if red else 'unknown'} ({red['date'] if red else '-'}), newest first"), ""]

    def commit_line(c):
        pr = prs.get(str(c["pr"])) if c["pr"] else None
        who = f" by @{pr['author']}" if pr and pr.get("author") else ""
        files = ", ".join(c["files"][:12]) + (f" (+{len(c['files']) - 12} more)" if len(c["files"]) > 12 else "")
        return f"- {c['sha']} {'PR #' + str(c['pr']) if c['pr'] else 'no PR'}{who}: {c['subject']} [{files}]"
    lines += [commit_line(c) for c in commits] or ["- (none read)"]
    lines += ["", "## Candidate fixes: merged since the first red night, touching the same files", ""]
    lines += [commit_line(c) for c in bundle.get("later") or []] or ["- (none)"]
    lines += ["", "## Candidate fixes: open pull requests touching the same files", ""]
    lines += [f"- PR #{p['number']} by @{p['author']}: {p['title']} [{', '.join(p['files'][:12])}]" for p in bundle.get("open_prs") or []] or ["- (none)"]
    for t in transcripts:
        lines += ["", f"## Transcript tail: {t['case']}, {t['night']}, repetition {t['rep']}", "", t["tail"]]
    if bundle.get("notes"):
        lines += ["", "## Gaps in this evidence", ""] + [f"- {n}" for n in bundle["notes"]]
    return "\n".join(lines)


def window_prs(bundle: dict) -> set[int]:
    return {c["pr"] for c in bundle.get("commits") or [] if c.get("pr")}


def fix_prs(bundle: dict) -> dict[int, str]:
    """The candidate fixes the model may name, with their state."""
    out = {c["pr"]: "merged" for c in bundle.get("later") or [] if c.get("pr")}
    out.update({p["number"]: "open" for p in bundle.get("open_prs") or [] if isinstance(p.get("number"), int)})
    return out


def parse_verdict(text: str, bundle: dict) -> dict:
    """The model's answer checked against ``bundle``; raises ModelError when
    it is not the schema's shape. A suspected pull request outside the
    window, or a fix outside the candidates, is dropped, not trusted."""
    try:
        answer = json.loads(text)
    except ValueError as exc:
        raise ModelError("the model's answer was not JSON") from exc
    if not isinstance(answer, dict):
        raise ModelError("the model's answer was not an object")
    verdict = {}
    for key, allowed in (("classification", CLASSES), ("confidence", CONFIDENCES), ("priority", PRIORITIES)):
        if answer.get(key) not in allowed:
            raise ModelError(f"the model's answer has no valid {key}")
        verdict[key] = answer[key]
    evidence = answer.get("evidence")
    verdict["evidence"] = evidence.strip()[:MAX_EVIDENCE_CHARS] if isinstance(evidence, str) else ""
    suspect = answer.get("suspected_pr")
    verdict["suspected_pr"] = suspect if isinstance(suspect, int) and not isinstance(suspect, bool) and suspect in window_prs(bundle) else None
    verdict["dropped_suspect"] = suspect if isinstance(suspect, int) and verdict["suspected_pr"] is None else None
    fixes = fix_prs(bundle)
    fix = answer.get("fix_pr")
    verdict["fix_pr"] = fix if isinstance(fix, int) and not isinstance(fix, bool) and fix in fixes else None
    verdict["fix_state"] = fixes[verdict["fix_pr"]] if verdict["fix_pr"] else "none"
    return verdict


def decide(bundle: dict, model) -> tuple[dict | None, str | None]:
    """``(verdict, None)``, or ``(None, why)`` when triage could not decide."""
    prompt = build_prompt(bundle)
    if prompt is None:
        return None, f"the evidence is over the {MAX_INPUT_TOKENS}-token cap even cut down"
    try:
        return parse_verdict(model(prompt, MAX_OUTPUT_TOKENS), bundle), None
    except ModelError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 -- a triage bug must not stop routing
        return None, f"triage raised {type(exc).__name__}"


WHY_AUTHOR = "author of {pr}, the suspected change"
WHY_INFRA_OWNER = "the infra owner in `docs/designs/domains.yaml`, who takes {what} no one pull request explains"
WHY_RULE = "the filing rule's assignee: {why}"
WHY_RULE_REFUSED = "the filing rule's next choice: GitHub would not assign `{refused}`"
RULE_WHY = {
    "case_rework": "triage found a case problem",
    "low": "triage's confidence is low",
    "no_pr": "triage named no pull request from the commit window",
    "author_bot": "the suspected pull request's author is a bot",
}


def route(verdict: dict | None, failure: str | None, bundle: dict | None, chain: list[str], infra_owner: str | None) -> list[tuple[str, str]]:
    """The ``(login, why)`` candidates to assign, in order (module docstring):
    the verdict's choice first, then the filer's ``chain``."""
    first: tuple[str, str] | None = None
    if verdict is None:
        reason = f"triage could not decide ({failure})"
    elif verdict["confidence"] == "low":
        reason = RULE_WHY["low"]
    elif verdict["classification"] == "case_rework":
        reason = RULE_WHY["case_rework"]
    elif verdict["classification"] in ("product", "grader_or_harness") and verdict["suspected_pr"]:
        author = ((bundle or {}).get("prs") or {}).get(str(verdict["suspected_pr"]), {}).get("author")
        if nightly_bugs.case_owner.is_person_login(author):
            first = (author, WHY_AUTHOR.format(pr=ghcli.pr_url(ghcli.SOURCE_REPO, verdict["suspected_pr"])))
            reason = None
        else:
            reason = RULE_WHY["author_bot"] if author else RULE_WHY["no_pr"]
    elif verdict["classification"] in ("infra", "grader_or_harness") and infra_owner:
        what = "an infrastructure failure" if verdict["classification"] == "infra" else "a grading or harness fault"
        first, reason = (infra_owner, WHY_INFRA_OWNER.format(what=what)), None
    else:
        reason = RULE_WHY["no_pr"]
    out = [first] if first else []
    for i, login in enumerate(chain):
        if any(login.lower() == o.lower() for o, _ in out):
            continue
        why = WHY_RULE.format(why=reason) if reason and i == 0 and not out else WHY_RULE_REFUSED.format(refused=out[-1][0] if out else chain[0])
        out.append((login, why))
    return out


COMMENT = """\
{marker}
**Agent triage** (`{model}`): **{classification}** · suggested priority **{priority}** · confidence **{confidence}**

**Suspected change:** {suspect}
**Fix:** {fix}

{evidence}

**Routed to** {routed}.{cc}

Read: {inputs}. Posted by `scripts/eval_dashboard/nightly_triage.py` (docs/ci-health.md, "Nightly triage"). A wrong call: reassign the issue and say why here.
"""
FAILED_COMMENT = """\
{marker}
**Agent triage did not run:** {why}. Routed by the filing rule to {routed}.{cc}
"""
SUSPECT_LINE = "{url} by {author}: {title}"
SUSPECT_NONE = "none named from the commit window"
SUSPECT_DROPPED = "none: the model named #{n}, which is not in the commit window, so it was set aside"
FIX_LINE = "{url} is {state}"
FIX_NONE = "none found among open pull requests touching the same files or merged since the first red night"


def render_comment(verdict: dict | None, failure: str | None, bundle: dict | None, assignee: str | None, why: str, cc: list[str], model: str) -> str:
    marker = COMMENT_MARKER.format(assignee=assignee or NOBODY)
    routed = f"@{assignee} ({why})" if assignee else f"no one ({why})"
    mentions = [c for c in cc if c.lower() != (assignee or "").lower()]
    cc_line = f" cc {', '.join('@' + c for c in mentions)}" if mentions else ""
    if verdict is None:
        return FAILED_COMMENT.format(marker=marker, why=failure, routed=routed, cc=cc_line)
    prs = (bundle or {}).get("prs") or {}
    if verdict["suspected_pr"]:
        pr = prs.get(str(verdict["suspected_pr"])) or {}
        author = pr.get("author")
        shown = (f"@{author}" if author and author.lower() == (assignee or "").lower() else f"`{author}`") if author else "an unknown author"
        suspect = SUSPECT_LINE.format(url=ghcli.pr_url(ghcli.SOURCE_REPO, verdict["suspected_pr"]), author=shown, title=pr.get("title") or "")
    elif verdict.get("dropped_suspect"):
        suspect = SUSPECT_DROPPED.format(n=verdict["dropped_suspect"])
    else:
        suspect = SUSPECT_NONE
    fix = FIX_LINE.format(url=ghcli.pr_url(ghcli.SOURCE_REPO, verdict["fix_pr"]), state=verdict["fix_state"]) if verdict["fix_pr"] else FIX_NONE
    green, red = (bundle or {}).get("green"), (bundle or {}).get("red")
    inputs = (f"{sum(len(n['fails']) for c in (bundle or {}).get('cases') or [] for n in c['red_nights'])} failing repetitions' grading lines, "
              f"{len((bundle or {}).get('transcripts') or [])} transcripts, {len((bundle or {}).get('commits') or [])} commits from "
              f"{green['head_sha'] + ' (' + green['date'] + ')' if green else 'an unknown green night'} to "
              f"{red['head_sha'] + ' (' + red['date'] + ')' if red else 'an unknown red night'}, "
              f"{len(fix_prs(bundle or {}))} candidate fixes")
    return COMMENT.format(marker=marker, model=model, classification=CLASSES[verdict["classification"]], priority=verdict["priority"],
                          confidence=verdict["confidence"], suspect=suspect, fix=fix, evidence=verdict["evidence"] or "(no evidence given)",
                          routed=routed, cc=cc_line, inputs=inputs)


def logins(field: str) -> list[str]:
    return [] if field == NOBODY else [x for x in field.split(",") if nightly_bugs.case_owner.is_person_login(x)]


def pending(issue: dict) -> re.Match | None:
    return PENDING_MARKER_RE.search(issue.get("body") or "") if isinstance(issue, dict) else None


def label_names(issue: dict) -> list[str]:
    return [lbl.get("name") if isinstance(lbl, dict) else lbl for lbl in issue.get("labels") or [] if lbl]


class Router:
    """The route phase for one run (module docstring)."""

    def __init__(self, gh: ghcli.Gh, model, model_name: str, repo_root: pathlib.Path = REPO_ROOT, live: bool = False, now=None):
        self.gh, self.model, self.model_name, self.live = gh, model, model_name, live
        self.infra_owner, _ = gate_issue.infra_addressees(repo_root / nightly_bugs.case_owner.DOMAINS_PATH)
        self.now = now or datetime.datetime.now(datetime.timezone.utc)

    def triage(self, issue: dict, bundle: dict | None) -> dict:
        """One issue: ``{issue, verdict, failure, assignee, why, ok}``."""
        number = issue["number"]
        marker = pending(issue)
        # Not filed for triage (a dry run over an issue the filer assigned):
        # its current assignees are the rule's answer.
        chain = logins(marker.group(1)) if marker else [a.get("login") for a in issue.get("assignees") or [] if isinstance(a, dict) and a.get("login")]
        cc = logins(marker.group(2)) if marker else []
        opened = nightly_bugs.nightly.parse_iso(issue.get("created_at"))
        verdict, failure = None, None
        if self.model is None:
            failure = "triage is switched off (NIGHTLY_TRIAGE_LIVE is not true)"
        elif bundle is None and opened and self.now - opened > PENDING_MAX_AGE:
            failure = f"no evidence was gathered in the {int(PENDING_MAX_AGE.total_seconds() // 3600)} hours since filing"
        elif bundle is None:
            return {"issue": number, "skipped": "no evidence gathered yet; retried next tick"}
        elif not bundle.get("cases"):
            failure = "no case of this issue failed in the nightly window"
        else:
            verdict, failure = decide(bundle, self.model)
        candidates = route(verdict, failure, bundle, chain, self.infra_owner)
        # The person the rule would have assigned is mentioned when someone
        # else takes it (the case owner, for a pull request's author).
        cc = ([chain[0]] if chain else []) + cc
        entry = {"issue": number, "verdict": verdict, "failure": failure, "candidates": candidates}
        if not self.live:
            entry.update(assignee=candidates[0][0] if candidates else None, why=candidates[0][1] if candidates else "no candidate", ok=True)
            return entry
        return self.write(issue, entry, candidates, cc, bundle)

    def write(self, issue: dict, entry: dict, candidates: list[tuple[str, str]], cc: list[str], bundle: dict | None) -> dict:
        """Assign, comment, mark done; stop at the first failed call (the
        issue stays pending and the next tick picks it up where it stopped:
        an assignee already set is kept, a comment already posted is not
        posted again)."""
        number = issue["number"]
        comments = self.gh.call("GET", self.gh.path(nightly_bugs.COMMENTS_READ_PATH.format(number=number)), paginate=True)
        if comments is None:
            return dict(entry, ok=False, why="the comments could not be read")
        posted = next((COMMENT_MARKER_RE.search(c.get("body") or "") for c in comments if isinstance(c, dict) and COMMENT_MARKER_RE.search(c.get("body") or "")), None)
        current = [a.get("login") for a in issue.get("assignees") or [] if isinstance(a, dict) and a.get("login")]
        assignee, why = (current[0], "assigned before this run") if current else (None, None)
        if posted:
            assignee = None if posted.group(1) == NOBODY else posted.group(1)
            why = "routed by an earlier run"
        elif not current:
            assignee, why = self.assign(number, candidates)
            if why is None:
                return dict(entry, ok=False, why="the assignment call failed")
        if not posted:
            body = render_comment(entry["verdict"], entry["failure"], bundle, assignee, why, cc, self.model_name)
            if self.gh.call("POST", self.gh.path(nightly_bugs.COMMENTS_PATH.format(number=number)), {"body": body}) is None:
                return dict(entry, assignee=assignee, ok=False, why="the comment could not be posted")
        marker = pending(issue)
        text = issue.get("body") or ""
        if marker:
            text = text.replace(marker.group(0), TRIAGE_DONE_MARKER.format(assign=marker.group(1), cc=marker.group(2)), 1)
        text = re.sub(re.escape(nightly_bugs.TRIAGE_PENDING_ADDRESSEE.split("{fallback}")[0]) + r".*", TRIAGED_ADDRESSEE, text, count=1)
        labels = [n for n in label_names(issue) if n != PENDING_LABEL] + [AGENT_LABEL]
        if self.gh.call("PATCH", self.gh.path(ISSUE_PATH.format(number=number)), {"body": text, "labels": labels}) is None:
            return dict(entry, assignee=assignee, ok=False, why="the issue could not be marked triaged")
        return dict(entry, assignee=assignee, why=why, ok=True)

    def assign(self, number: int, candidates: list[tuple[str, str]]) -> tuple[str | None, str | None]:
        """The first candidate GitHub assigns (it answers 201 and leaves out
        a login it will not take), ``(None, why)`` when it takes none, and
        ``(None, None)`` when a call failed."""
        refused = []
        for login, why in candidates:
            answer = self.gh.call("POST", self.gh.path(ASSIGNEES_PATH.format(number=number)), {"assignees": [login]})
            if answer is None and not self.gh.refused(nightly_bugs.ASSIGNEES_FIELD):
                return None, None
            taken = [a.get("login", "").lower() for a in (answer or {}).get("assignees") or [] if isinstance(a, dict)]
            if login.lower() in taken:
                return login, why
            refused.append(f"`{login}`")
        return None, f"GitHub would not assign {', '.join(refused)}" if refused else "no one to assign"


def dry_line(entry: dict) -> str:
    """One public line: numbers and logins, never a case or a grading line."""
    if entry.get("skipped"):
        return f"  #{entry['issue']}: skipped ({entry['skipped']})"
    v = entry.get("verdict")
    who = f"@{entry['assignee']}" if entry.get("assignee") else "no one"
    if v is None:
        head = "rule (triage did not decide)"
    else:
        head = (f"{v['classification']}, suspect {'#' + str(v['suspected_pr']) if v['suspected_pr'] else 'none'}, "
                f"fix {'#' + str(v['fix_pr']) + ' ' + v['fix_state'] if v['fix_pr'] else 'none'}, {v['priority']}, confidence {v['confidence']}")
    status = "" if entry.get("ok", True) else " -- NOT DONE, retried next tick"
    return f"  #{entry['issue']}: {head} -> {who}{status}"


# --------------------------------------------------------------------------
# entry points


def pending_issues(gh: ghcli.Gh) -> list[dict]:
    """The open nightly-red issues still carrying the pending marker,
    oldest first. Keyed on the body marker, not the label, so a label
    GitHub dropped cannot hide an issue from triage."""
    issues = gh.call("GET", gh.path(OPEN_RED_PATH.format(label=nightly_bugs.LABEL)), paginate=True) or []
    found = [i for i in issues if isinstance(i, dict) and not i.get("pull_request") and i.get("number") and pending(i)]
    return sorted(found, key=lambda i: i.get("created_at") or "")


def chosen_issues(gh: ghcli.Gh, numbers: list[int]) -> list[dict]:
    """``numbers`` when given (each read on its own; only an open issue that
    names a case, so not an infra issue), else the pending ones; capped at
    MAX_ISSUES_PER_RUN."""
    if numbers:
        issues = [gh.call("GET", gh.path(ISSUE_PATH.format(number=n))) for n in numbers]
        issues = [i for i in issues if isinstance(i, dict) and i.get("number") and i.get("state") == "open" and nightly_bugs.group_issue_cases(i)]
    else:
        issues = pending_issues(gh)
    if len(issues) > MAX_ISSUES_PER_RUN:
        log(f"{len(issues)} issues to triage; the oldest {MAX_ISSUES_PER_RUN} this run, the rest next tick")
    return issues[:MAX_ISSUES_PER_RUN]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="phase", required=True)
    prep = sub.add_parser("prepare", help="gather the evidence for each issue (no writes)")
    prep.add_argument("--data", required=True, type=pathlib.Path)
    prep.add_argument("--out", required=True, type=pathlib.Path)
    rt = sub.add_parser("route", help="ask the model, then assign, comment and mark done")
    rt.add_argument("--bundles", type=pathlib.Path, help="prepare's output; an issue missing from it waits for the next tick")
    rt.add_argument("--model", default=os.environ.get(MODEL_ENV) or DEFAULT_MODEL)
    rt.add_argument("--project", default=os.environ.get(PROJECT_ENV) or DEFAULT_PROJECT)
    rt.add_argument("--no-model", action="store_true", help="route by the filing rule without a model call")
    mode = rt.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    mode.add_argument("--live", dest="dry_run", action="store_false")
    for p in (prep, rt):
        p.add_argument("--repo", default=ghcli.DEFAULT_REPO, help="the issues repository")
        p.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT)
        p.add_argument("--issue", type=int, action="append", default=[], help="these issues instead of the pending ones")
    return parser.parse_args(argv)


def main(argv=None, gh_runner=None, source_runner=None, model=None, git=None, gcs=None, now=None) -> int:
    args = parse_args(argv)
    gh = ghcli.Gh(args.repo, gh_runner) if gh_runner else ghcli.Gh(args.repo)
    issues = chosen_issues(gh, args.issue)
    if args.phase == "prepare":
        try:
            data = json.loads(args.data.read_text())
        except (OSError, ValueError) as exc:
            log(f"ERROR: {args.data}: {exc}")
            return 1
        source = ghcli.Gh(ghcli.SOURCE_REPO, source_runner) if source_runner else ghcli.Gh(ghcli.SOURCE_REPO)
        preparer = Preparer(data if isinstance(data, dict) else {}, source, args.repo_root, git=git, gcs=gcs or gcs_tail)
        bundles = {}
        for issue in issues:
            try:
                bundles[str(issue["number"])] = preparer.bundle(issue)
            except Exception as exc:  # noqa: BLE001 -- one issue's bug must not cost the others theirs
                log(f"warning: #{issue['number']}: gathering the evidence raised {type(exc).__name__}; it is routed without it")
        args.out.write_text(json.dumps(bundles))
        print(f"nightly triage: gathered the evidence for {len(bundles)} of {len(issues)} issues")
        return 0
    bundles = {}
    if args.bundles:
        try:
            bundles = json.loads(args.bundles.read_text())
        except (OSError, ValueError) as exc:
            log(f"warning: {args.bundles}: {exc}; every issue waits for the next tick, or is routed by the rule once it is old")
    if args.no_model:
        client = None
    else:
        client = model or VertexModel(args.project, args.model)
    router = Router(gh, client, args.model, args.repo_root, live=not args.dry_run, now=now)
    lines = [f"nightly triage ({'live' if not args.dry_run else 'dry run'}, {'no model' if client is None else args.model}): {len(issues)} issues"]
    for issue in issues:
        lines.append(dry_line(router.triage(issue, bundles.get(str(issue["number"])))))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Turn the self-improvement loop on, and prove it is on before an hour goes by.

The chart renders every Kubernetes object the loop needs. What it cannot render
is the half that lives outside the cluster: a fork to push to, a personal access
token carrying the right two scopes, the labels the pull request will wear, and
the agreement between four names that Helm and Terraform each hold one half of.
Nothing in the install compares those halves.

That matters because every way they can disagree fails the same way. The CronJob
fires on schedule, the investigation runs and finds things, the ledger fills up
-- and the filing turn writes SKIPPED. A token missing `read:org`, a base branch
that does not contain the revision stamped into the image, a fork that is not a
fork of the source, a label the robot account lacks the permission to create:
each is invisible for the hour it takes the next run to reproduce it, and none
of them says which one it was.

So this checks the outside half before the first fire, installs the two pieces
the chart deliberately leaves out, and then checks the assembled install again
against what is actually running.

    selfimprove_enable.py preflight --mode upstream \\
        --upstream-repo gke-labs/kube-agents \\
        --fork-repo robot/kube-agents --token-file ~/.pat
    selfimprove_enable.py secret --token-file ~/.pat
    selfimprove_enable.py labels --mode upstream --upstream-repo ... --token-file ~/.pat
    selfimprove_enable.py values --mode upstream ... > selfimprove.values.yaml
    selfimprove_enable.py verify

What it does not do is install anything the chart installs. `terraform` and
`helm` own the install (AGENTS.md, "The install has one engine"), so `values`
emits the settings for them to apply rather than applying a manifest itself. The
two exceptions are the two things neither engine can own: a Secret whose content
must never reach a state file or a shell history, and labels that live on
GitHub.

The token is read from a file, from stdin, or from $SELFIMPROVE_PAT -- never
from a command-line argument, where it would sit in the process table for every
other pod on the node, and never printed back. Everything here reports on it by
scope and by the answer GitHub gives, not by value.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULT_NAMESPACE = "kubeagents-system"
DEFAULT_CRONJOB = "kube-agents-selfimprove"
DEFAULT_LEDGER_CONFIGMAP = "kube-agents-selfimprove-ledger"
DEFAULT_NETWORKPOLICY = "kube-agents-selfimprove-policy"
DEFAULT_AGENT_DEPLOYMENT = "platform-agent-gateway"
DEFAULT_PAT_SECRET = "kube-agents-selfimprove-pat"
DEFAULT_PAT_SECRET_KEY = "token"
DEFAULT_KSA = "kubeagents-selfimprove"
DEFAULT_GSA = "kubeagents-selfimprove"
DEFAULT_BASE_BRANCH = "main"
DEFAULT_PR_LABEL = "self-improvement"
DEFAULT_SEVERITY_PREFIX = "severity:"
DEFAULT_UPSTREAM_REPO = "gke-labs/kube-agents"

MODES = ("report-only", "fork", "upstream")

#: The severities `selfimprove_ledger` assigns, worst first. The loop attaches
#: `<prefix><severity>` to a pull request and, per the filing code, only if the
#: label already exists -- creating one needs write access the robot account is
#: not necessarily given. Read from the module below where it imports, so a
#: severity added there does not silently stop getting a label here.
SEVERITIES = ("critical", "high", "medium", "low")

#: Mirrors `selfimprove_ledger.MIN_CORROBORATING_RUNS`. A finding is never
#: promoted on the strength of a single run's sighting however low
#: `minOccurrencesPerDay` is set, so a gate configured at 1 still needs two
#: runs. Read from the module when it imports, so the two cannot drift.
MIN_CORROBORATING_RUNS = 2

#: The two scopes a *classic* token needs. `repo` alone is not enough: the
#: filing turn shells out to `gh`, and `gh auth login --with-token` rejects a
#: token whose scope set lacks `read:org`. `public_repo` does not substitute for
#: `repo` even on a public repository, because the fork the loop pushes to may
#: not be public and `gh` checks the scope, not the repository.
REQUIRED_CLASSIC_SCOPES = ("repo", "read:org")

GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

#: Where a client-side `kubectl apply` stores the manifest it submitted, which
#: for a Secret written from `stringData` means the value in cleartext. See the
#: apply in `cmd_secret`.
LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"

#: The CronJob volume carrying the GitHub token, named by
#: `charts/kube-agents/templates/self-improvement.yaml`. `verify` reads the
#: Secret's name and key off it rather than off its own flags.
PAT_VOLUME_NAME = "selfimprove-github-pat"

sys.path.insert(0, str(REPO_ROOT / "agents" / "selfimprove" / "scripts"))
try:  # pragma: no cover - the failure branch needs the module absent
    import selfimprove_ledger as ledger_mod

    MIN_CORROBORATING_RUNS = getattr(
        ledger_mod, "MIN_CORROBORATING_RUNS", MIN_CORROBORATING_RUNS
    )
    SEVERITIES = tuple(getattr(ledger_mod, "SEVERITIES", SEVERITIES))
except Exception:  # noqa: BLE001 - a checkout without the loop still gets the checks
    ledger_mod = None


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_MARKS = {OK: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}
_COLOURS = {OK: "\x1b[32m", WARN: "\x1b[33m", FAIL: "\x1b[31m", SKIP: "\x1b[2m"}


class Report:
    """An ordered list of checks, each with a verdict and a sentence.

    Checks accumulate rather than raising, because the whole point is to say
    everything that is wrong in one pass. Finding out about the missing label
    only after fixing the token is two round trips through a fifteen-minute
    Terraform apply.
    """

    def __init__(self, colour: bool = True) -> None:
        self.rows: List[Tuple[str, str, str, str]] = []
        self.colour = colour

    def add(self, status: str, name: str, detail: str, fix: str = "") -> str:
        self.rows.append((status, name, detail, fix))
        return status

    def ok(self, name: str, detail: str) -> str:
        return self.add(OK, name, detail)

    def warn(self, name: str, detail: str, fix: str = "") -> str:
        return self.add(WARN, name, detail, fix)

    def fail(self, name: str, detail: str, fix: str = "") -> str:
        return self.add(FAIL, name, detail, fix)

    def skip(self, name: str, detail: str) -> str:
        return self.add(SKIP, name, detail)

    @property
    def failed(self) -> bool:
        return any(r[0] == FAIL for r in self.rows)

    def _mark(self, status: str) -> str:
        text = _MARKS[status]
        if not self.colour:
            return text
        return "%s%s\x1b[0m" % (_COLOURS[status], text)

    def render(self) -> List[str]:
        out: List[str] = []
        for status, name, detail, fix in self.rows:
            out.append("  %s  %-34s %s" % (self._mark(status), name, detail))
            if fix:
                for line in fix.splitlines():
                    out.append("        %s" % line)
        counts = {k: 0 for k in _MARKS}
        for row in self.rows:
            counts[row[0]] += 1
        out.append("")
        out.append(
            "  %d passed, %d warnings, %d failed, %d skipped"
            % (counts[OK], counts[WARN], counts[FAIL], counts[SKIP])
        )
        return out

    def to_json(self) -> List[Dict[str, str]]:
        return [
            {"status": s, "check": n, "detail": d, "fix": f} for s, n, d, f in self.rows
        ]


def emit_json(rep: Report) -> None:
    """Write a report as the whole of stdout.

    Every subcommand that takes `--json` emits this one shape, so a caller can
    parse any of them with the same reader and branch on `failed` rather than
    on which command it ran. Nothing else goes to stdout in that mode: an agent
    should not have to strip a banner before `json.loads`.
    """
    print(json.dumps({"checks": rep.to_json(), "failed": rep.failed}, indent=2))


# --------------------------------------------------------------------------
# kubectl
# --------------------------------------------------------------------------


class KubeError(RuntimeError):
    pass


def kubectl(
    args: Sequence[str],
    namespace: Optional[str] = None,
    context: Optional[str] = None,
    stdin: Optional[str] = None,
    check: bool = True,
) -> str:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    if namespace:
        cmd += ["-n", namespace]
    cmd += list(args)
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise KubeError("kubectl is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise KubeError("kubectl timed out: %s" % " ".join(cmd)) from exc
    if check and proc.returncode != 0:
        raise KubeError((proc.stderr or proc.stdout).strip() or "kubectl failed")
    return proc.stdout


def kube_json(
    args: Sequence[str],
    namespace: Optional[str] = None,
    context: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """`kubectl get -o json`, or None when the object is not there.

    A missing object is an ordinary answer for most of these checks -- the
    CronJob does not exist yet, the Secret has not been created -- so it comes
    back as None rather than an exception. Any other kubectl failure still
    raises, because "cannot reach the cluster" and "the Secret is absent" are
    different findings and reporting the first as the second sends the reader
    to fix the wrong thing.
    """
    cmd = list(args) + ["-o", "json"]
    try:
        raw = kubectl(cmd, namespace=namespace, context=context)
    except KubeError as exc:
        text = str(exc).lower()
        if "notfound" in text.replace(" ", "") or "not found" in text:
            return None
        raise
    return json.loads(raw) if raw.strip() else None


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


class GitHubResponse:
    def __init__(self, status: int, body: Any, headers: Dict[str, str]) -> None:
        self.status = status
        self.body = body
        self.headers = headers


def github(
    path: str,
    token: Optional[str],
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> GitHubResponse:
    """One GitHub REST call, returning the status rather than raising on 4xx.

    Every caller here treats 403 and 404 as answers -- "the token cannot see
    this" is the finding, not an error condition -- so the HTTPError branch
    collapses back into the same object as the success branch.
    """
    url = path if path.startswith("http") else "%s%s" % (GITHUB_API, path)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "kube-agents-selfimprove-enable")
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = json.loads(raw) if raw else None
            return GitHubResponse(resp.status, body, headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = {"message": raw.decode("utf-8", "replace")[:200]}
        return GitHubResponse(exc.code, body, headers)
    except urllib.error.URLError as exc:
        return GitHubResponse(0, {"message": str(exc.reason)}, {})


def parse_scopes(header: Optional[str]) -> List[str]:
    """The `X-OAuth-Scopes` header as a list.

    Absent means a fine-grained or App token, which reports no scopes at all;
    present-but-empty means a classic token with none. The two are different
    findings, so the caller distinguishes them by testing the header for None
    rather than testing this for emptiness.
    """
    if not header:
        return []
    return [s.strip() for s in header.split(",") if s.strip()]


def missing_scopes(scopes: Sequence[str]) -> List[str]:
    """Which of the required classic scopes are absent.

    `repo` implies its children, so a token holding `repo` satisfies
    `public_repo` -- but not the other way round, which is the mistake worth
    catching: a `public_repo` token looks adequate against a public repository
    right up to the point `gh auth login` reads the scope list and refuses.
    """
    have = set(scopes)
    if "repo" in have:
        have.add("public_repo")
    if "admin:org" in have:
        have.add("read:org")
    if "write:org" in have:
        have.add("read:org")
    return [s for s in REQUIRED_CLASSIC_SCOPES if s not in have]


def repo_slug(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if value.startswith("https://github.com/"):
        value = value[len("https://github.com/") :]
    if value.endswith(".git"):
        value = value[: -len(".git")]
    return value


def valid_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value or ""))


def same_repo(a: str, b: str) -> bool:
    """Case-insensitive slug comparison, matching the chart's own guard.

    GitHub treats owner and repository names case-insensitively, so
    `Robot/Kube-Agents` and `robot/kube-agents` are one repository. The chart
    refuses a fork equal to the upstream on the same basis; disagreeing with it
    here would let a configuration pass preflight and fail `helm upgrade`.
    """
    return repo_slug(a).lower() == repo_slug(b).lower()


# --------------------------------------------------------------------------
# Pure planning helpers
# --------------------------------------------------------------------------


def pr_base_repo(mode: str, upstream: str, fork: str) -> str:
    """Where the pull request is opened, which is not always the upstream.

    Under `fork` mode the loop opens the pull request against the fork itself,
    so the labels have to exist there and the base branch has to exist there.
    Under `upstream` it is the upstream repository. Under `report-only` nothing
    is opened at all.
    """
    if mode == "fork":
        return repo_slug(fork)
    if mode == "upstream":
        return repo_slug(upstream)
    return ""


def source_repo(upstream: str) -> str:
    """Where the run reads its own source, in every mode: the upstream.

    The revision under investigation is the one stamped into the running image,
    and that is an upstream revision. `SELFIMPROVE_SOURCE_REPO` exists precisely
    so that fork mode does not investigate whatever state the fork happens to
    be in.
    """
    return repo_slug(upstream)


def gate_reachable(schedule_hours: int, gate: Dict[str, Any]) -> List[str]:
    """Complaints about a gate that cannot fire against a given run rate.

    Two ways a gate is unreachable and looks fine. `minOccurrencesPerDay` above
    the number of runs a day means a finding can be seen on every single run and
    still never promote. And any value below MIN_CORROBORATING_RUNS is a number
    the code will not honour -- the floor applies regardless -- so a gate set to
    1 promises same-run promotion and delivers next-run promotion, which is the
    difference between "the loop is broken" and "the loop is working" to
    somebody watching the first hour.
    """
    problems: List[str] = []
    if schedule_hours <= 0:
        return problems
    runs_per_day = 24 // schedule_hours
    for rule in gate.get("rules") or []:
        need = rule.get("minOccurrencesPerDay")
        sev = rule.get("severity", "?")
        if not isinstance(need, int):
            continue
        if need > runs_per_day:
            problems.append(
                "severity=%s needs %d sightings a day but the schedule allows at most %d"
                % (sev, need, runs_per_day)
            )
        elif need < MIN_CORROBORATING_RUNS:
            problems.append(
                "severity=%s asks for %d but %d runs is the floor, so promotion is one run later than configured"
                % (sev, need, MIN_CORROBORATING_RUNS)
            )
    return problems


def schedule_period_hours(schedule: str) -> int:
    """Hours between fires for the cron expressions the chart can produce.

    Only the shapes worth checking: `0 * * * *` and `0 */N * * *`. Anything else
    returns 0, which every caller reads as "do not draw a conclusion".
    """
    parts = (schedule or "").split()
    if len(parts) != 5:
        return 0
    hour = parts[1]
    if hour == "*":
        return 1
    m = re.fullmatch(r"\*/(\d+)", hour)
    if m:
        step = int(m.group(1))
        return step if step > 0 else 0
    if re.fullmatch(r"\d+", hour):
        return 24
    return 0


def build_values(args: argparse.Namespace) -> Dict[str, Any]:
    """The `selfImprovement` values a working install needs, as a plain dict.

    Only the keys an operator has to decide. Everything else -- the schedule,
    the gate, the timeouts, the signal list -- has a chart default that is the
    right answer for a first install, and restating a default in a values file
    is how it stops tracking the chart.
    """
    values: Dict[str, Any] = {
        "enabled": True,
        "mode": args.mode,
        "github": {
            "upstreamRepo": repo_slug(args.upstream_repo),
            "baseBranch": args.base_branch,
            "ksaName": args.ksa_name,
            "gsaName": args.gsa_name,
        },
    }
    gh = values["github"]
    if args.mode != "report-only":
        gh["forkRepo"] = repo_slug(args.fork_repo)
        gh["patSecret"] = args.pat_secret
        gh["patSecretKey"] = args.pat_secret_key
        gh["prLabel"] = args.pr_label
        gh["severityLabelPrefix"] = args.severity_label_prefix
    if getattr(args, "api_server_cidrs", None):
        values["apiServerCIDRs"] = list(args.api_server_cidrs)
    if getattr(args, "dns_cidrs", None):
        values["dnsCIDRs"] = list(args.dns_cidrs)
    return values


# --------------------------------------------------------------------------
# Emitters
# --------------------------------------------------------------------------


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`,]", text) or text != text.strip():
        return json.dumps(text)
    return text


def emit_yaml(value: Any, indent: int = 0) -> List[str]:
    """A YAML dump for the small, closed shape this tool produces.

    Deliberately not PyYAML. Everything else in `scripts/` runs on the standard
    library so it can be run from a laptop with no virtualenv, and the value
    tree here is dicts, lists, strings, ints and bools -- nothing that needs a
    parser's worth of code to serialise.
    """
    pad = "  " * indent
    out: List[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(sub, dict) and sub:
                out.append("%s%s:" % (pad, key))
                out.extend(emit_yaml(sub, indent + 1))
            elif isinstance(sub, list):
                if not sub:
                    out.append("%s%s: []" % (pad, key))
                else:
                    out.append("%s%s:" % (pad, key))
                    for item in sub:
                        out.append("%s  - %s" % (pad, _yaml_scalar(item)))
            elif isinstance(sub, dict):
                out.append("%s%s: {}" % (pad, key))
            else:
                out.append("%s%s: %s" % (pad, key, _yaml_scalar(sub)))
    else:
        out.append("%s%s" % (pad, _yaml_scalar(value)))
    return out


def emit_hcl(value: Any, indent: int = 0) -> List[str]:
    """The same tree as an HCL object, for `extra_helm_values`.

    `terraform/examples/full-install` does not expose `selfImprovement` as a
    variable of its own; `extra_helm_values` (type `any`) is merged in as a
    second values document, and is the supported route for any chart key the
    composition has not lifted. Emitting both forms means the same tool serves a
    Helm-only install and a Terraform-managed one without either reader having
    to translate.
    """
    pad = "  " * indent
    out: List[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(sub, dict):
                out.append("%s%s = {" % (pad, key))
                out.extend(emit_hcl(sub, indent + 1))
                out.append("%s}" % pad)
            elif isinstance(sub, list):
                items = ", ".join(json.dumps(str(i)) for i in sub)
                out.append("%s%s = [%s]" % (pad, key, items))
            elif isinstance(sub, bool):
                out.append("%s%s = %s" % (pad, key, "true" if sub else "false"))
            elif isinstance(sub, (int, float)):
                out.append("%s%s = %s" % (pad, key, sub))
            else:
                out.append("%s%s = %s" % (pad, key, json.dumps(str(sub))))
    return out


def secret_manifest(name: str, namespace: str, key: str, token: str) -> str:
    """The PAT Secret as YAML, for `kubectl apply -f -`.

    `kubectl create secret --from-literal` would put the token in argv, where
    it is readable by anything that can list processes on the machine and is
    kept by the shell's history file. Piping a manifest keeps it on a pipe.
    `stringData` rather than `data` so nothing here has to base64 it, and so a
    reader of this function can see there is no encoding step to get wrong.

    That choice is what makes the apply mode part of this contract rather than
    a detail of the call site: a client-side apply would copy this manifest,
    cleartext `stringData` and all, into the Secret's
    last-applied-configuration annotation. `cmd_secret` applies server-side for
    that reason -- do not simplify the flag away.
    """
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": "kube-agents"},
        },
        "type": "Opaque",
        "stringData": {key: token},
    }
    return json.dumps(body)


# --------------------------------------------------------------------------
# Token input
# --------------------------------------------------------------------------


def read_token(args: argparse.Namespace, required: bool = True) -> Optional[str]:
    """The PAT, from a file, from stdin, or from the environment.

    Never from an argument. A `--token` flag would be the obvious ergonomics
    win and is exactly the thing that leaks: argv is world-readable on Linux,
    and the value survives in shell history on the operator's laptop long after
    the install is done.
    """
    token: Optional[str] = None
    if args.token_file:
        path = pathlib.Path(os.path.expanduser(args.token_file))
        if not path.is_file():
            raise SystemExit("token file not found: %s" % path)
        token = path.read_text(encoding="utf-8").strip()
    elif args.token_stdin:
        token = sys.stdin.read().strip()
    else:
        token = (os.environ.get("SELFIMPROVE_PAT") or "").strip() or None
    if not token and required:
        raise SystemExit(
            "no token. Pass --token-file PATH, pipe it with --token-stdin, "
            "or export SELFIMPROVE_PAT. There is deliberately no --token flag."
        )
    return token or None


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_github(rep: Report, args: argparse.Namespace, token: Optional[str]) -> None:
    """Everything about the outside half that can be answered from GitHub."""
    upstream = repo_slug(args.upstream_repo)
    fork = repo_slug(args.fork_repo) if args.fork_repo else ""
    base_repo = pr_base_repo(args.mode, upstream, fork)

    if not valid_slug(upstream):
        rep.fail("upstream repo", "%r is not owner/name" % args.upstream_repo)
        return
    if args.mode != "report-only":
        if not valid_slug(fork):
            rep.fail(
                "fork repo",
                "%r is not owner/name, and %s mode needs one" % (args.fork_repo, args.mode),
                "The loop pushes its branch to the fork and opens the pull request from it.",
            )
            return
        if same_repo(fork, upstream):
            rep.fail(
                "fork repo",
                "fork and upstream are the same repository (%s)" % fork,
                "The chart refuses this too. The loop must push somewhere it is not "
                "also proposing to merge into.",
            )
            return

    if not token:
        rep.skip("github token", "no token given, skipping every GitHub check")
        return

    who = github("/user", token)
    if who.status == 401:
        rep.fail("github token", "GitHub rejected it (401)", "The token is wrong, revoked, or expired.")
        return
    if who.status != 200:
        rep.fail("github token", "GET /user returned %s" % who.status)
        return
    login = (who.body or {}).get("login", "?")
    rep.ok("github token", "authenticates as %s" % login)

    scope_header = who.headers.get("x-oauth-scopes")
    scopes = parse_scopes(scope_header)
    if scope_header is None:
        rep.warn(
            "token scopes",
            "no X-OAuth-Scopes header, so this is a fine-grained or App token",
            "Scopes cannot be read from here. Confirm by hand that it grants Contents "
            "read/write, Pull requests read/write, and Metadata read on both repositories.\n"
            "Note `gh auth login --with-token` accepts a fine-grained token, but the loop's\n"
            "filing turn has only been exercised with a classic one.",
        )
    else:
        gap = missing_scopes(scopes)
        if gap:
            rep.fail(
                "token scopes",
                "missing %s (has: %s)" % (", ".join(gap), ", ".join(scopes) or "none"),
                "`gh auth login --with-token` refuses a token without read:org and the\n"
                "credential proxy's sidecar then never comes up. The failure surfaces an\n"
                "hour later as a SKIPPED filing turn, not at startup.",
            )
        else:
            rep.ok("token scopes", "has %s" % ", ".join(REQUIRED_CLASSIC_SCOPES))

    src = source_repo(upstream)
    r = github("/repos/%s" % src, token)
    if r.status != 200:
        rep.fail(
            "source repo readable",
            "GET /repos/%s returned %s" % (src, r.status),
            "This is the repository the run fetches its own source from. Without it the\n"
            "investigation has nothing to read.",
        )
    else:
        rep.ok("source repo readable", "%s" % src)

    if fork:
        r = github("/repos/%s" % fork, token)
        if r.status != 200:
            rep.fail("fork readable", "GET /repos/%s returned %s" % (fork, r.status))
        else:
            body = r.body or {}
            perms = body.get("permissions") or {}
            if perms.get("push"):
                rep.ok("fork writable", "%s grants push to %s" % (fork, login))
            else:
                rep.fail(
                    "fork writable",
                    "%s does not grant push to %s (permissions=%s)"
                    % (fork, login, json.dumps(perms, sort_keys=True)),
                    "The loop pushes a branch there on every promoted finding.",
                )
            parent = ((body.get("parent") or {}).get("full_name") or "")
            network = ((body.get("source") or {}).get("full_name") or "")
            if not body.get("fork"):
                rep.warn(
                    "fork network",
                    "%s is not a fork of anything" % fork,
                    "A standalone repository can still receive a push, but GitHub will not\n"
                    "offer a cross-repository pull request into %s from it." % (base_repo or upstream),
                )
            elif same_repo(network or parent, src) or same_repo(parent, src):
                rep.ok("fork network", "%s forks %s" % (fork, network or parent))
            else:
                rep.warn(
                    "fork network",
                    "%s forks %s, not %s" % (fork, network or parent, src),
                    "Cross-repository pull requests only work inside one fork network.",
                )

    if base_repo:
        branch = github("/repos/%s/branches/%s" % (base_repo, args.base_branch), token)
        if branch.status != 200:
            rep.fail(
                "base branch",
                "%s has no branch %s (%s)" % (base_repo, args.base_branch, branch.status),
                "This is the pull request's base. The filing turn checks it out per finding,\n"
                "and returns SKIPPED rather than falling back to anything else.",
            )
        else:
            rep.ok("base branch", "%s@%s" % (base_repo, args.base_branch))
    else:
        rep.skip("base branch", "report-only opens no pull request")

    if base_repo and args.mode != "report-only":
        want = [args.pr_label] if args.pr_label else []
        want += ["%s%s" % (args.severity_label_prefix, s) for s in SEVERITIES]
        missing = []
        for label in want:
            resp = github(
                "/repos/%s/labels/%s" % (base_repo, urllib.request.quote(label)), token
            )
            if resp.status != 200:
                missing.append(label)
        if not want:
            rep.skip("pull request labels", "prLabel is empty, so nothing is attached")
        elif missing:
            rep.warn(
                "pull request labels",
                "%s lacks %s" % (base_repo, ", ".join(missing)),
                "The loop attaches only labels that already exist, so a missing one is a\n"
                "pull request without it rather than a failure. `%s labels` creates them,\n"
                "if the token has write access -- TRIAGE is not enough."
                % pathlib.Path(sys.argv[0]).name,
            )
        else:
            rep.ok("pull request labels", "%d present on %s" % (len(want), base_repo))


def stamped_revision(
    namespace: str, deployment: str, context: Optional[str]
) -> Tuple[Optional[str], str]:
    """The git revision baked into the running agent image.

    Read from the pod rather than from a label, because that file is what the
    runner itself reads to decide what it is investigating. A pod that cannot be
    exec'd into -- a locked-down cluster, a sandbox that refuses -- gives back
    None and a reason, and the caller degrades that check to a warning rather
    than blocking an enablement on it.
    """
    try:
        pods = kube_json(
            [
                "get",
                "pods",
                "-l",
                "app=%s" % deployment,
                "--field-selector=status.phase=Running",
            ],
            namespace=namespace,
            context=context,
        )
    except KubeError as exc:
        return None, str(exc)
    items = (pods or {}).get("items") or []
    if not items:
        try:
            pods = kube_json(
                ["get", "pods", "--field-selector=status.phase=Running"],
                namespace=namespace,
                context=context,
            )
        except KubeError as exc:
            return None, str(exc)
        items = [
            p
            for p in ((pods or {}).get("items") or [])
            if deployment in (p.get("metadata", {}).get("name") or "")
        ]
    if not items:
        return None, "no Running pod for deployment %s" % deployment
    pod = items[0]["metadata"]["name"]
    # No `-c`. The agent pod's container names are the operator's business and
    # have changed -- `platform-agent`, not the `hermes` an earlier draft of this
    # assumed -- so naming one here is a guess that fails with a message about
    # containers rather than about revisions. kubectl picks the first, which is
    # the agent in every layout the operator renders.
    try:
        raw = kubectl(
            ["exec", pod, "--", "cat", "/opt/build-info.json"],
            namespace=namespace,
            context=context,
        )
    except KubeError as exc:
        return None, "exec into %s failed: %s" % (pod, str(exc).splitlines()[0][:120])
    try:
        info = json.loads(raw)
    except ValueError:
        return None, "/opt/build-info.json in %s is not JSON" % pod
    rev = info.get("revision") or info.get("git_revision") or info.get("sha")
    if not rev:
        return None, "/opt/build-info.json has no revision key"
    return str(rev), pod


def check_revision(
    rep: Report,
    args: argparse.Namespace,
    token: Optional[str],
    revision: Optional[str],
    why: str,
) -> None:
    """Does the source repository serve the revision, and does the base contain it.

    The second is the one that surprises people. The filing turn branches from
    the base branch tip, so a base that does not already contain the stamped
    revision produces a pull request whose diff is every commit between the two
    -- forty-nine files, in the case that taught us this -- rather than the one
    fix. It looks like a credentials problem and is not.
    """
    if not revision:
        rep.warn("stamped revision", "could not read it: %s" % why)
        return
    rep.ok("stamped revision", "%s (from %s)" % (revision[:12], why))
    if not token:
        return
    # `source_repo` is set by `cmd_verify` from the CronJob's
    # `SELFIMPROVE_SOURCE_REPO`, which is the repository the runner clones. The
    # fallback carries the `preflight` path, which has no CronJob to read and
    # whose `--upstream-repo` is the upstream by definition.
    src = source_repo(getattr(args, "source_repo", "") or args.upstream_repo)
    resp = github("/repos/%s/commits/%s" % (src, revision), token)
    if resp.status != 200:
        rep.fail(
            "revision in source repo",
            "%s does not serve %s (%s)" % (src, revision[:12], resp.status),
            "The investigation fetches its own source at this revision. Push the commit,\n"
            "or point SELFIMPROVE_SOURCE_REPO at a repository in the same fork network.",
        )
        return
    rep.ok("revision in source repo", "%s serves %s" % (src, revision[:12]))

    base_repo = pr_base_repo(args.mode, args.upstream_repo, args.fork_repo)
    if not base_repo:
        return
    cmp_resp = github(
        "/repos/%s/compare/%s...%s" % (base_repo, args.base_branch, revision), token
    )
    if cmp_resp.status != 200:
        rep.warn(
            "base contains the revision",
            "compare %s...%s returned %s" % (args.base_branch, revision[:12], cmp_resp.status),
        )
        return
    status = (cmp_resp.body or {}).get("status")
    behind = (cmp_resp.body or {}).get("behind_by", 0)
    if status in ("identical", "behind"):
        rep.ok(
            "base contains the revision",
            "%s@%s is at or ahead of %s" % (base_repo, args.base_branch, revision[:12]),
        )
    else:
        rep.fail(
            "base contains the revision",
            "%s@%s does not contain %s (status=%s, %d commits behind it)"
            % (base_repo, args.base_branch, revision[:12], status, behind),
            "Every pull request will carry the difference between the two as its diff.\n"
            "Merge the running revision into the base branch first, or run against a base\n"
            "that already has it.",
        )


def check_values_shape(rep: Report, args: argparse.Namespace) -> None:
    """The constraints `self-improvement.yaml` enforces with `fail`, checked here first.

    Every one of these is already caught at render time, loudly and with a good
    message. The reason to repeat them is where the render happens: inside a
    `terraform apply` that has spent ten minutes on the GCP half before Helm
    ever runs, and that leaves the composition part-applied when it stops. Names
    are free to check before any of that starts.
    """
    if not re.fullmatch(r"[-._a-zA-Z0-9]+", args.pat_secret_key or ""):
        rep.fail(
            "patSecretKey shape",
            "%r is not a usable Secret key" % args.pat_secret_key,
            "Kubernetes allows [-._a-zA-Z0-9]+, and the chart interpolates this value into\n"
            "the credential proxy's bootstrap shell command.",
        )
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.gsa_name or ""):
        rep.fail(
            "gsaName shape",
            "%r is not a usable GCP service account id" % args.gsa_name,
            "6 to 30 characters, starting with a lowercase letter, ending alphanumeric,\n"
            "lowercase letters digits and hyphens only.",
        )


def check_kubernetes_version(rep: Report, args: argparse.Namespace) -> None:
    """1.29 or later, because the credential proxy is a native sidecar.

    `restartPolicy: Always` on an initContainer is GA from 1.29. On 1.28 the
    field is silently dropped rather than rejected, so the proxy becomes an
    ordinary initContainer that never exits and the Job never starts its runner.
    """
    try:
        raw = kubectl(["version", "-o", "json"], context=args.context)
        server = (json.loads(raw).get("serverVersion") or {})
    except (KubeError, ValueError):
        rep.skip("kubernetes version", "could not read it")
        return
    major = re.sub(r"\D", "", str(server.get("major", "")))
    minor = re.sub(r"\D", "", str(server.get("minor", "")))
    if not major or not minor:
        rep.skip("kubernetes version", "server reported %r" % server.get("gitVersion"))
        return
    if (int(major), int(minor)) >= (1, 29):
        rep.ok("kubernetes version", server.get("gitVersion", "%s.%s" % (major, minor)))
    else:
        rep.fail(
            "kubernetes version",
            "%s.%s, and the chart requires 1.29" % (major, minor),
            "Native sidecars are GA from 1.29. Below it the credential proxy never exits\n"
            "and the Job never completes, so concurrencyPolicy: Forbid blocks every later run.",
        )


def check_cluster(rep: Report, args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """The namespace, the agent Deployment, and the GSA binding on the KSA."""
    ns = args.namespace
    try:
        if kube_json(["get", "namespace", ns], context=args.context) is None:
            rep.fail("namespace", "%s does not exist" % ns)
            return None
    except KubeError as exc:
        rep.fail("cluster reachable", str(exc).splitlines()[0][:160])
        return None
    rep.ok("namespace", ns)

    dep = kube_json(
        ["get", "deployment", args.agent_deployment], namespace=ns, context=args.context
    )
    if dep is None:
        rep.fail(
            "agent deployment",
            "%s not found in %s" % (args.agent_deployment, ns),
            "The runner reads this Deployment's image to check it is investigating the\n"
            "code it is running. A wrong name is a refusal on every run.",
        )
    else:
        images = [
            c.get("image", "")
            for c in dep["spec"]["template"]["spec"].get("containers", [])
        ]
        rep.ok("agent deployment", "%s runs %s" % (args.agent_deployment, ", ".join(images)))
    return dep


def check_ksa(rep: Report, args: argparse.Namespace) -> None:
    ns = args.namespace
    ksa = kube_json(
        ["get", "serviceaccount", args.ksa_name], namespace=ns, context=args.context
    )
    if ksa is None:
        rep.skip(
            "workload identity",
            "%s not created yet -- the chart makes it when selfImprovement.enabled is true"
            % args.ksa_name,
        )
        return
    ann = (ksa.get("metadata", {}).get("annotations") or {}).get(
        "iam.gke.io/gcp-service-account", ""
    )
    if not ann:
        rep.warn(
            "workload identity",
            "%s has no iam.gke.io/gcp-service-account annotation" % args.ksa_name,
            "The investigation reads Cloud Logging and Cloud Monitoring through this.",
        )
        return
    project = getattr(args, "gcp_project", "")
    want = (
        "%s@%s.iam.gserviceaccount.com" % (args.gsa_name, project)
        if project
        else "%s@" % args.gsa_name
    )
    if ann == want or (not project and ann.startswith(want)):
        rep.ok("workload identity", "%s -> %s" % (args.ksa_name, ann))
    else:
        rep.warn(
            "workload identity",
            "%s -> %s, expected %s" % (args.ksa_name, ann, want),
            "The chart's gsaName and the Terraform module's must name the same account,\n"
            "and nothing compares them at apply time. A mismatch is not a startup failure:\n"
            "the pod runs and every Cloud Logging read inside it 403s an hour later.",
        )


def check_pat_secret(rep: Report, args: argparse.Namespace) -> None:
    """The Secret exists and has the key the chart will mount.

    By name and key only. Nothing here reads the value: a tool that decodes a
    Secret to check it is a tool that can print one, and the check it would buy
    -- "is this the right token" -- is already answered better by asking GitHub.
    """
    if args.mode == "report-only":
        rep.skip("pat secret", "report-only mounts no credential")
        return
    sec = kube_json(
        ["get", "secret", args.pat_secret], namespace=args.namespace, context=args.context
    )
    if sec is None:
        rep.fail(
            "pat secret",
            "%s/%s does not exist" % (args.namespace, args.pat_secret),
            "`%s secret --token-file PATH` creates it."
            % pathlib.Path(sys.argv[0]).name,
        )
        return
    keys = sorted((sec.get("data") or {}).keys())
    if args.pat_secret_key in keys:
        rep.ok("pat secret", "%s has key %s" % (args.pat_secret, args.pat_secret_key))
    else:
        rep.fail(
            "pat secret",
            "%s has %s, not %s" % (args.pat_secret, keys or "no keys", args.pat_secret_key),
            "patSecretKey in the chart values must name a key that exists.",
        )


def discovered_endpoints(args: argparse.Namespace) -> Dict[str, List[str]]:
    """The API server and DNS addresses this cluster actually uses.

    From the `kubernetes` Endpoints object and the kube-dns Service, so it works
    on any cluster without gcloud and without knowing the provider. These are
    the addresses the loop's NetworkPolicy has to allow: it is a default-deny
    policy, and a runner that cannot reach the API server cannot write the
    ledger row that would tell you so.
    """
    out: Dict[str, List[str]] = {"apiserver": [], "dns": []}
    try:
        ep = kube_json(
            ["get", "endpoints", "kubernetes"], namespace="default", context=args.context
        )
    except KubeError:
        ep = None
    for subset in ((ep or {}).get("subsets") or []):
        for addr in subset.get("addresses") or []:
            ip = addr.get("ip")
            if ip:
                out["apiserver"].append(ip)
    try:
        svcs = kube_json(
            ["get", "svc", "-l", "k8s-app=kube-dns"], namespace="kube-system", context=args.context
        )
    except KubeError:
        svcs = None
    for item in ((svcs or {}).get("items") or []):
        ip = (item.get("spec") or {}).get("clusterIP")
        if ip and ip != "None":
            out["dns"].append(ip)
    return out


def covered(address: str, blocks: Sequence[Any]) -> bool:
    """Is an address inside any of these ipBlocks and outside every `except`?

    A block is a NetworkPolicy `ipBlock` mapping, or a bare CIDR string for the
    callers that have nothing to except.

    Honouring `except` is what makes this answer anything. The chart's egress
    rule for 443 is `0.0.0.0/0` with the five private ranges excepted, so a
    reader that took `cidr` alone would find every address on earth covered and
    could never report one as blocked -- the check would pass by construction.
    Rules are additive, so an address excepted by the wide rule is still
    covered if some narrower rule names it: that is exactly the shape of a
    correct install, where the discovered API-server /32s sit alongside it.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    for block in blocks:
        if isinstance(block, str):
            block = {"cidr": block}
        try:
            if ip not in ipaddress.ip_network(block.get("cidr") or "", strict=False):
                continue
        except ValueError:
            continue
        excepted = False
        for hole in block.get("except") or []:
            try:
                if ip in ipaddress.ip_network(hole, strict=False):
                    excepted = True
                    break
            except ValueError:
                # A malformed `except` cannot be read as "excepts nothing":
                # that is the direction that turns an unreadable policy into a
                # clean report. Treat the whole block as not covering.
                excepted = True
                break
        if not excepted:
            return True
    return False


def report_promotion_gap(
    rep: Report, ledger: Dict[str, Any], promoted: int, filed: int, note: str
) -> None:
    """Say what `promoted > filed` means on this particular ledger.

    The arithmetic alone does not distinguish the two cases, and they want
    opposite responses. A finding the filing turn refused is the loop working:
    it decided it could not write a safe fix and said so, permanently. A
    promotion with no refusal and no pull request is the GitHub path failing
    silently, which is the thing this whole tool exists to surface. So read the
    findings for what could explain the gap, and warn only on what is left over
    once they have.
    """
    findings = ledger.get("findings") or {}
    entries = list(findings.values()) if isinstance(findings, dict) else list(findings)
    refused = [e for e in entries if isinstance(e.get("refused"), dict)]
    carrying = [
        e
        for e in entries
        if not e.get("refused")
        and [p for p in (e.get("promotions") or []) if p and p.get("url")]
    ]
    # Measured against the gap, not against the ledger. A finding the gate is
    # still holding carries the empty `promotions` list `record_finding` gave
    # it and was never promoted at all, so reading every such row as a
    # promotion nobody filed warned on any ledger holding more findings than it
    # has filed -- which is every working one. The run promoted `promoted` and
    # filed `filed`; the rest have to be findings that already carry a pull
    # request or that the filing turn refused, and a gap those two cannot cover
    # is the GitHub path failing silently.
    #
    # The explainers are counted over the whole ledger rather than attributed
    # to this run, because no record maps a run to the fingerprints it
    # promoted. So a genuinely lost promotion can hide behind an older finding
    # that already carries a pull request: the check fires when the gap exceeds
    # what the ledger can explain, not on every one. Exact attribution wants
    # `record_run` to store the promoted fingerprints.
    unexplained = (promoted - filed) - len(refused) - len(carrying)
    detail = "promoted %d, filed %d" % (promoted, filed)
    if note:
        detail += " -- %s" % note.splitlines()[0][:80]
    if unexplained <= 0:
        # No unexplained finding is the healthy case, and it is also the common
        # one: a finding that already carries an open pull request is promoted
        # again on every later run and deliberately not re-filed, so
        # `promoted > filed` holds forever on a working install. Requiring a
        # refusal to reach this arm turned that into a standing warning whose
        # own fix text said nothing was wrong.
        why = []
        if refused:
            why.append("%d carry a recorded refusal" % len(refused))
        if carrying:
            why.append("%d already carry a pull request" % len(carrying))
        rep.ok(
            "promotion gap explained",
            "%s; every promoted finding is accounted for%s"
            % (detail, " (%s)" % ", ".join(why) if why else ""),
        )
        return
    rep.warn(
        "last run filed nothing new",
        detail,
        "Not necessarily a fault: a finding already carrying an open pull request is\n"
        "promoted again and deliberately not re-filed, and so is one the filing turn\n"
        "refused. It is a fault when neither holds. %d promotion(s) from this run are\n"
        "covered by neither -- `make selfimprove-ledger` shows what each finding\n"
        "carries." % unexplained,
    )


def has_kube_dns_selector(policy: Dict[str, Any]) -> bool:
    """Whether any port-53 rule reaches kube-dns by label rather than by address.

    The chart's DNS rule does both, and the label half is the one that works:
    a NetworkPolicy matches the destination pod, so the kube-dns Service's
    ClusterIP appearing in no ipBlock is not a gap.
    """
    for rule in (policy.get("spec", {}).get("egress") or []):
        if 53 not in [p.get("port") for p in (rule.get("ports") or [])]:
            continue
        for peer in rule.get("to") or []:
            labels = (peer.get("podSelector") or {}).get("matchLabels") or {}
            if labels.get("k8s-app") in ("kube-dns", "node-local-dns"):
                return True
    return False


def policy_ip_blocks(policy: Dict[str, Any], port: int) -> List[Dict[str, Any]]:
    """Every ipBlock in the egress rules that open a given port.

    Whole blocks, not their `cidr` strings: `except` is half of what an ipBlock
    says, and dropping it here is what made `covered` unable to report anything
    as blocked. See `covered`.
    """
    found: List[Dict[str, Any]] = []
    for rule in (policy.get("spec", {}).get("egress") or []):
        ports = [p.get("port") for p in (rule.get("ports") or [])]
        if port not in ports:
            continue
        for peer in rule.get("to") or []:
            block = peer.get("ipBlock") or {}
            if block.get("cidr"):
                found.append(block)
    return found


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace) -> int:
    rep = Report(colour=args.colour)
    token = read_token(args, required=False)

    check_values_shape(rep, args)
    check_github(rep, args, token)
    # Before `check_cluster`, and not inside its `dep is not None` arm. The 1.29
    # floor is a property of the cluster, not of the agent Deployment, and
    # `check_cluster` returns None when that Deployment is merely absent -- the
    # state a first preflight is run in. Gating the check there skipped it
    # exactly when it was worth having. It reads `kubectl version` and reports a
    # skip of its own when it cannot, so it is safe to run against nothing.
    check_kubernetes_version(rep, args)
    dep = check_cluster(rep, args)
    if dep is not None:
        revision, why = stamped_revision(args.namespace, args.agent_deployment, args.context)
        check_revision(rep, args, token, revision, why)
        check_ksa(rep, args)
        check_pat_secret(rep, args)
        eps = discovered_endpoints(args)
        if eps["apiserver"]:
            rep.ok(
                "api server address",
                "%s -- put it in selfImprovement.apiServerCIDRs if the install is rendered"
                % ", ".join("%s/32" % a for a in eps["apiserver"]),
            )
        else:
            rep.warn("api server address", "could not read the kubernetes Endpoints object")

    if args.json:
        emit_json(rep)
    else:
        print("\npreflight -- %s mode, namespace %s\n" % (args.mode, args.namespace))
        for line in rep.render():
            print(line)
        print("")
    return 1 if rep.failed else 0


def cmd_secret(args: argparse.Namespace) -> int:
    # Two audiences, one set of verdicts. Every branch records what it decided
    # in `rep`; `--json` then emits that and suppresses the prose, so stdout is
    # parseable whole. The refusals keep going to stderr in prose mode -- they
    # are the reason a shell pipeline saw nothing on stdout, and moving them
    # onto stdout would hide that from a script that only reads one stream.
    rep = Report(colour=args.colour)
    quiet = args.json

    def say(*parts: object, **kw: object) -> None:
        if not quiet:
            print(*parts, **kw)

    def done() -> int:
        if quiet:
            emit_json(rep)
        return 1 if rep.failed else 0

    token = read_token(args, required=True)
    assert token
    if args.check_token:
        who = github("/user", token)
        if who.status != 200:
            rep.fail(
                "token",
                "GitHub returned %s for /user" % who.status,
                "The token is expired, revoked, or mistyped. Mint a new one and re-run.",
            )
            say("refusing to store it: GitHub returned %s for /user" % who.status, file=sys.stderr)
            return done()
        gap = missing_scopes(parse_scopes(who.headers.get("x-oauth-scopes")))
        header_absent = who.headers.get("x-oauth-scopes") is None
        if gap and not header_absent:
            rep.fail(
                "token scopes",
                "missing %s" % ", ".join(gap),
                "Re-mint the token with those scopes, or pass --no-check-token to store it anyway.",
            )
            say(
                "refusing to store it: missing scope(s) %s. Pass --no-check-token to store anyway."
                % ", ".join(gap),
                file=sys.stderr,
            )
            return done()
        rep.ok("token", "authenticates as %s" % (who.body or {}).get("login", "?"))
        say("token authenticates as %s" % (who.body or {}).get("login", "?"))
    else:
        rep.skip("token", "--no-check-token, so it was stored without asking GitHub whether it works")

    manifest = secret_manifest(args.pat_secret, args.namespace, args.pat_secret_key, token)
    if args.dry_run:
        rep.ok(
            "secret",
            "would apply %s/%s with key %s (%d bytes of token, not shown)"
            % (args.namespace, args.pat_secret, args.pat_secret_key, len(token)),
        )
        say(
            "would apply Secret %s/%s with key %s (%d bytes of token, not shown)"
            % (args.namespace, args.pat_secret, args.pat_secret_key, len(token))
        )
        return done()
    # --server-side, and the named field manager with it. A client-side apply
    # copies the manifest it submitted into
    # kubectl.kubernetes.io/last-applied-configuration, so the token would come
    # to rest twice: base64 in `data`, where it belongs, and in cleartext in
    # metadata, where every tool that redacts `data` and not annotations prints
    # it -- `kubectl diff` masks only the top-level `data` map, and an operator
    # scrubbing a base64 blob out of pasted output will not think to scrub an
    # annotation. Rotating `data` by any other route then leaves the previous
    # token there indefinitely, outliving its revocation. That is the one thing
    # this module is written not to do: `secret_manifest` refuses
    # --from-literal because argv is world-readable, `read_token` refuses a
    # --token flag for the same reason, and `check_pat_secret` will not decode
    # the Secret to check it.
    #
    # The field manager is load-bearing rather than cosmetic. Under the default
    # `kubectl` manager, apply deliberately preserves an annotation that is
    # already there; under a named one it migrates ownership and the applied
    # config, which omits the annotation, removes it. So this also cleans up an
    # install created by an earlier version of this tool. The explicit
    # `annotate ...-` below is the belt to that braces: it does not depend on
    # kubectl's migration path, which upstream describes as transitional.
    #
    # Preflight requires 1.29, so server-side apply is GA on every cluster this
    # runs against.
    try:
        kubectl(
            ["apply", "--server-side", "--field-manager=selfimprove-enable", "-f", "-"],
            namespace=args.namespace,
            context=args.context,
            stdin=manifest,
        )
    except KubeError as exc:
        if "conflict" not in str(exc).lower():
            raise
        say(
            "refusing to overwrite %s/%s: another field manager owns it (a chart, an\n"
            "ExternalSecret, or an earlier run under a different name). Delete the\n"
            "Secret and re-run, or reconcile it where it is managed."
            % (args.namespace, args.pat_secret),
            file=sys.stderr,
        )
        rep.fail(
            "secret",
            "%s/%s is managed by something else" % (args.namespace, args.pat_secret),
            str(exc),
        )
        return done()
    # Non-fatal: an install that never had the annotation returns "not found"
    # from `annotate`, and there is nothing to report in that case.
    kubectl(
        ["annotate", "secret", args.pat_secret, LAST_APPLIED_ANNOTATION + "-"],
        namespace=args.namespace,
        context=args.context,
        check=False,
    )
    rep.ok(
        "secret",
        "applied %s/%s, key %s" % (args.namespace, args.pat_secret, args.pat_secret_key),
    )
    rep.ok(
        "chart values",
        "set selfImprovement.github.patSecret=%s and patSecretKey=%s"
        % (args.pat_secret, args.pat_secret_key),
    )
    say(
        "applied Secret %s/%s, key %s"
        % (args.namespace, args.pat_secret, args.pat_secret_key)
    )
    say(
        "set selfImprovement.github.patSecret=%s and patSecretKey=%s"
        % (args.pat_secret, args.pat_secret_key)
    )
    return done()


def cmd_labels(args: argparse.Namespace) -> int:
    rep = Report(colour=args.colour)
    quiet = args.json

    def say(*parts: object, **kw: object) -> None:
        if not quiet:
            print(*parts, **kw)

    def done() -> int:
        if quiet:
            emit_json(rep)
        return 1 if rep.failed else 0

    token = read_token(args, required=True)
    base_repo = pr_base_repo(args.mode, args.upstream_repo, args.fork_repo)
    if not base_repo:
        rep.skip("labels", "report-only opens no pull request, so there is nothing to label")
        say("report-only opens no pull request, so there is nothing to label")
        return done()
    want: List[Tuple[str, str, str]] = []
    if args.pr_label:
        want.append((args.pr_label, "0e8a16", "Opened by the kube-agents self-improvement loop"))
    palette = {"critical": "b60205", "high": "d93f0b", "medium": "fbca04", "low": "c2e0c6"}
    for sev in SEVERITIES:
        want.append(
            (
                "%s%s" % (args.severity_label_prefix, sev),
                palette.get(sev, "ededed"),
                "Self-improvement finding severity: %s" % sev,
            )
        )

    for name, colour, desc in want:
        existing = github("/repos/%s/labels/%s" % (base_repo, urllib.request.quote(name)), token)
        if existing.status == 200:
            rep.ok("label %s" % name, "exists on %s" % base_repo)
            say("  exists  %s" % name)
            continue
        if args.dry_run:
            rep.warn(
                "label %s" % name,
                "absent from %s; would create it (#%s)" % (base_repo, colour),
                "Re-run without --dry-run.",
            )
            say("  would create  %s (#%s)" % (name, colour))
            continue
        resp = github(
            "/repos/%s/labels" % base_repo,
            token,
            method="POST",
            payload={"name": name, "color": colour, "description": desc},
        )
        if resp.status in (200, 201):
            rep.ok("label %s" % name, "created on %s" % base_repo)
            say("  created %s" % name)
        elif resp.status == 422:
            rep.ok("label %s" % name, "exists on %s" % base_repo)
            say("  exists  %s" % name)
        elif resp.status in (403, 404):
            # TRIAGE can attach a label and cannot create one, and the API
            # answers a permission failure on a repository the token can read
            # with 403 -- or 404, when it cannot see the repository at all.
            rep.fail(
                "label %s" % name,
                "the token cannot create labels on %s" % base_repo,
                "Ask someone with write access to create it; the loop attaches only "
                "labels that already exist.",
            )
            say(
                "  DENIED  %s -- the token cannot create labels on %s. Ask someone with "
                "write access to create it; the loop attaches only labels that exist."
                % (name, base_repo)
            )
        else:
            rep.fail(
                "label %s" % name,
                "%s %s" % (resp.status, (resp.body or {}).get("message", "")),
            )
            say("  ERROR   %s -- %s %s" % (name, resp.status, (resp.body or {}).get("message", "")))
    # Prose only. Which severities the gate can actually reach is a real check
    # in its own right -- `check_gate_reachability`, which preflight and verify
    # both run -- so a JSON reader gets it there rather than as an untyped note
    # bolted onto this command's rows.
    say("")
    say(
        "A severity with no rule in selfImprovement.gate never reaches a pull request, so"
    )
    say(
        "its label sits unused until a rule is added. The chart's default gate rules"
    )
    say("cover critical, high and medium.")
    return done()


def cmd_values(args: argparse.Namespace) -> int:
    values = build_values(args)
    if args.format == "hcl":
        print("# Merge into terraform/examples/full-install's extra_helm_values.")
        print("# The composition does not expose selfImprovement as a variable of its own;")
        print("# extra_helm_values is applied as a second Helm values document.")
        print("extra_helm_values = {")
        for line in emit_hcl({"selfImprovement": values}, 1):
            print(line)
        print("}")
    elif args.format == "json":
        print(json.dumps({"selfImprovement": values}, indent=2))
    else:
        print("# helm upgrade kube-agents charts/kube-agents -f this-file")
        print("# Only the keys an operator has to decide. The schedule, gate, timeouts")
        print("# and signal list keep their chart defaults on purpose.")
        for line in emit_yaml({"selfImprovement": values}):
            print(line)
    if args.format != "json":
        print("")
        print("# Two values outside this block are required when selfImprovement.enabled,")
        print("# and the chart fails the render without them:")
        print("#   platformAgent.harness.projectId    addresses the investigator GSA")
        print("#   platformAgent.harness.clusterName  scopes its metric queries to this")
        print("#                                      cluster, and an empty value is no")
        print("#                                      filter at all")
        print("# There is no selfImprovement.github.projectId.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check the assembled install, reading intent out of the CronJob itself.

    Distinct from preflight in where it gets its answers: preflight is told what
    you mean to configure, verify reads what is configured. Running it after a
    `helm upgrade` and before the next hour catches the settings that apply
    cleanly and then do nothing.
    """
    rep = Report(colour=args.colour)
    token = read_token(args, required=False)

    cron = kube_json(
        ["get", "cronjob", args.cronjob], namespace=args.namespace, context=args.context
    )
    if cron is None:
        rep.fail(
            "cronjob",
            "%s/%s does not exist" % (args.namespace, args.cronjob),
            "selfImprovement.enabled is false, or the upgrade has not been applied.",
        )
        # Same two branches as the command's tail. This is the one early return
        # in `verify`, and it is the state a caller polls `--json` for while it
        # waits for the upgrade to land -- printing the table here handed
        # `json.loads` a document it cannot parse.
        if args.json:
            emit_json(rep)
        else:
            print("\n".join(rep.render()))
        return 1

    spec = cron.get("spec", {})
    schedule = spec.get("schedule", "")
    if spec.get("suspend"):
        rep.fail("cronjob", "%s exists but is suspended" % args.cronjob)
    else:
        rep.ok("cronjob", "%s, schedule %s" % (args.cronjob, schedule))

    pod_spec = spec["jobTemplate"]["spec"]["template"]["spec"]
    containers = pod_spec.get("containers", [])
    runner = next((c for c in containers if c.get("name") == "runner"), None)
    if runner is None and containers:
        runner = containers[0]
    env = {
        e["name"]: e.get("value", "")
        for e in ((runner or {}).get("env") or [])
        if "value" in e
    }
    mode = env.get("SELFIMPROVE_MODE", "")
    if mode in MODES:
        rep.ok("mode", mode)
    else:
        # Echoing it back was not a check. Everything below branches on this
        # value -- which repository is the base, whether a credential is
        # mounted, which rows are skipped -- so a CronJob carrying no
        # `SELFIMPROVE_MODE`, or one carrying a typo, silently took the
        # report-only path through every one of them while the row read OK.
        rep.fail(
            "mode",
            "SELFIMPROVE_MODE is %r, not one of %s" % (mode, ", ".join(MODES)),
            "The runner reads this to decide whether it may file at all. Every check\n"
            "below branches on it too, so the rest of this report is about a mode the\n"
            "install does not have.",
        )

    dep = kube_json(
        ["get", "deployment", env.get("SELFIMPROVE_AGENT_DEPLOYMENT", args.agent_deployment)],
        namespace=args.namespace,
        context=args.context,
    )
    if dep is None:
        rep.fail(
            "agent deployment",
            "SELFIMPROVE_AGENT_DEPLOYMENT=%s does not resolve"
            % env.get("SELFIMPROVE_AGENT_DEPLOYMENT", "?"),
        )
    else:
        dep_images = {
            c.get("image", "") for c in dep["spec"]["template"]["spec"].get("containers", [])
        }
        runner_image = (runner or {}).get("image", "")
        if runner_image in dep_images:
            rep.ok("image agreement", "runner and agent both run %s" % runner_image)
        else:
            rep.fail(
                "image agreement",
                "runner %s, agent %s" % (runner_image, ", ".join(sorted(dep_images)) or "none"),
                "The runner refuses to investigate a revision other than the one the agent\n"
                "is running, and records the refusal instead of a finding. Roll both, or set\n"
                "selfImprovement.allowUnstampedImage only if you know why.",
            )

    for name in ("SELFIMPROVE_SOURCE_REPO", "SELFIMPROVE_UPSTREAM_REPO", "SELFIMPROVE_FORK_REPO",
                 "SELFIMPROVE_BASE_BRANCH"):
        value = env.get(name)
        if value:
            rep.ok(name.lower().replace("selfimprove_", ""), value)
        elif name == "SELFIMPROVE_FORK_REPO" and mode != "fork":
            rep.skip(name.lower().replace("selfimprove_", ""), "unset")
        elif mode == "report-only":
            rep.skip(name.lower().replace("selfimprove_", ""), "unset under report-only")
        else:
            rep.warn(name.lower().replace("selfimprove_", ""), "unset")

    args.mode = mode
    # Two variables, two questions, and reading the wrong one is silent. The
    # chart renders `SELFIMPROVE_UPSTREAM_REPO` as the pull request's base --
    # the fork, under fork mode; see the comment above `$prTarget` in
    # self-improvement.yaml -- while the repository the run fetches its own
    # source from is `SELFIMPROVE_SOURCE_REPO`, always the upstream. Deriving
    # both from the first asked a fork to serve an upstream commit, so
    # "revision in source repo" FAILed against a correctly configured fork-mode
    # install and the repository the runner actually clones went unchecked.
    # Bound before the overwrite below, so the fallback is the CLI's
    # `--upstream-repo` rather than the base this line is about to install.
    args.source_repo = env.get("SELFIMPROVE_SOURCE_REPO") or args.upstream_repo
    args.upstream_repo = env.get("SELFIMPROVE_UPSTREAM_REPO", args.upstream_repo)
    args.fork_repo = env.get("SELFIMPROVE_FORK_REPO", args.fork_repo)
    args.base_branch = env.get("SELFIMPROVE_BASE_BRANCH", args.base_branch)
    args.pr_label = env.get("SELFIMPROVE_PR_LABEL", args.pr_label)
    args.severity_label_prefix = env.get(
        "SELFIMPROVE_SEVERITY_LABEL_PREFIX", args.severity_label_prefix
    )

    revision, why = stamped_revision(
        args.namespace, env.get("SELFIMPROVE_AGENT_DEPLOYMENT", args.agent_deployment), args.context
    )
    check_revision(rep, args, token, revision, why)

    try:
        gate = json.loads(env.get("SELFIMPROVE_GATE", "{}"))
    except ValueError:
        gate = {}
        rep.warn("gate", "SELFIMPROVE_GATE is not JSON")
    if gate:
        problems = gate_reachable(schedule_period_hours(schedule), gate)
        if problems:
            # One row, however many rules are affected. Four identical warnings
            # differing only in a severity name push the rest of the report off
            # the screen and read as four problems.
            rep.warn("gate reachability", problems[0], "\n".join(problems[1:]))
        else:
            rep.ok(
                "gate reachability",
                "every rule can fire at %s" % (schedule or "this schedule"),
            )

    # The Secret the CronJob mounts, not the one the CLI defaults to. `verify`
    # reports on a live install, so an operator who set `patSecret` to anything
    # other than the default got a check that read a Secret nothing mounts --
    # green, while the pod that needs the real one sits in
    # CreateContainerConfigError. The volume carries the key too, in `items`,
    # so both halves of the question come from the same place the kubelet
    # reads. Falling back to the flags keeps a CronJob whose volume this does
    # not recognise checkable rather than skipped.
    pat_volume = next(
        (
            v
            for v in pod_spec.get("volumes") or []
            if v.get("name") == PAT_VOLUME_NAME and (v.get("secret") or {}).get("secretName")
        ),
        None,
    )
    if pat_volume:
        secret_source = pat_volume["secret"]
        args.pat_secret = secret_source["secretName"]
        items = secret_source.get("items") or []
        if items and items[0].get("key"):
            args.pat_secret_key = items[0]["key"]
    check_pat_secret(rep, args)

    policy = kube_json(
        ["get", "networkpolicy", args.networkpolicy],
        namespace=args.namespace,
        context=args.context,
    )
    if policy is None:
        rep.skip("network policy", "%s absent, so egress is unrestricted" % args.networkpolicy)
    else:
        eps = discovered_endpoints(args)
        allowed = policy_ip_blocks(policy, 443) + policy_ip_blocks(policy, 6443)
        blocked = [a for a in eps["apiserver"] if not covered(a, allowed)]
        if not eps["apiserver"]:
            rep.warn("api server egress", "could not read the kubernetes Endpoints object")
        elif blocked:
            rep.fail(
                "api server egress",
                "%s not covered by the policy" % ", ".join(blocked),
                "The runner cannot write the ledger, so the run leaves no record of failing.\n"
                "Add them to selfImprovement.apiServerCIDRs as /32s. The chart discovers this\n"
                "with `lookup`, which returns nothing under `helm template` -- a rendered\n"
                "install has to be told.",
            )
        else:
            rep.ok("api server egress", "%s allowed" % ", ".join(eps["apiserver"]))
        dns_blocked = [a for a in eps["dns"] if not covered(a, policy_ip_blocks(policy, 53))]
        if not eps["dns"]:
            # The same guard the api-server branch above has, for the same
            # reason. `discovered_endpoints` turns a failed read into an empty
            # list, and an empty list makes `dns_blocked` empty too -- so
            # without this the row reads OK having checked nothing.
            rep.warn(
                "dns egress",
                "no Service matches k8s-app=kube-dns in kube-system, so the DNS rule was "
                "not checked",
            )
        elif not dns_blocked:
            rep.ok("dns egress", "kube-dns reachable by address")
        elif has_kube_dns_selector(policy):
            # The ClusterIP is not in an ipBlock and does not need to be: the
            # chart's DNS rule selects the kube-dns pods by label, and a
            # NetworkPolicy is evaluated against the backing pod's address
            # rather than the Service's virtual one.
            rep.ok(
                "dns egress",
                "kube-dns reachable by podSelector (ClusterIP %s is not in an ipBlock, "
                "which is fine)" % ", ".join(dns_blocked),
            )
        else:
            rep.warn(
                "dns egress",
                "%s is in no ipBlock and no rule selects k8s-app=kube-dns"
                % ", ".join(dns_blocked),
                "Add it to selfImprovement.dnsCIDRs. Without DNS the runner resolves\n"
                "neither GitHub nor the model endpoint.",
            )

    ledger = kube_json(
        ["get", "configmap", env.get("SELFIMPROVE_LEDGER_CONFIGMAP", DEFAULT_LEDGER_CONFIGMAP)],
        namespace=args.namespace,
        context=args.context,
    )
    if ledger is None:
        rep.warn("ledger", "the ConfigMap does not exist yet; the first run creates it")
    else:
        try:
            doc = json.loads((ledger.get("data") or {}).get("ledger.json") or "{}")
        except ValueError:
            doc = {}
            rep.warn("ledger", "ledger.json is not JSON")
        runs = doc.get("runs") or []
        if not runs:
            rep.warn("ledger", "no runs recorded yet")
        else:
            last = runs[-1]
            rep.ok(
                "ledger",
                "%d runs, last at %s outcome=%s findings=%s promoted=%s filed=%s"
                % (
                    len(runs),
                    last.get("at", "?"),
                    last.get("outcome", "?"),
                    last.get("findings", "?"),
                    last.get("promoted", "?"),
                    last.get("filed", "?"),
                ),
            )
            promoted, filed = last.get("promoted"), last.get("filed")
            # Not under report-only, where the loop files nothing by design:
            # `promoted > filed` is that mode's steady state, and the check
            # exists to catch a GitHub write path failing silently, which is a
            # path that mode does not have. Reporting it there is a warning on
            # the chart's own default.
            if (
                mode != "report-only"
                and isinstance(promoted, int)
                and isinstance(filed, int)
                and promoted > filed
            ):
                report_promotion_gap(rep, doc, promoted, filed, last.get("note", ""))

    check_ksa(rep, args)

    if args.json:
        emit_json(rep)
    else:
        print("\nverify -- %s/%s\n" % (args.namespace, args.cronjob))
        for line in rep.render():
            print(line)
        print("")
        print("  `make selfimprove-ledger` renders the run history in full.")
        print("")
    return 1 if rep.failed else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", "-n", default=DEFAULT_NAMESPACE)
    parser.add_argument("--context", default=None, help="kubectl context")
    parser.add_argument(
        "--colour",
        "--color",
        dest="colour",
        action="store_true",
        default=sys.stdout.isatty(),
    )
    parser.add_argument("--no-colour", "--no-color", dest="colour", action="store_false")


def add_token(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "token",
        "The PAT, never as an argument. $SELFIMPROVE_PAT is used when neither flag is given.",
    )
    group.add_argument("--token-file", default=None, metavar="PATH")
    group.add_argument("--token-stdin", action="store_true", help="read it from stdin")


def add_repos(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=MODES, default="report-only")
    parser.add_argument("--upstream-repo", default=DEFAULT_UPSTREAM_REPO, metavar="OWNER/NAME")
    parser.add_argument("--fork-repo", default="", metavar="OWNER/NAME")
    parser.add_argument("--base-branch", default=DEFAULT_BASE_BRANCH)
    parser.add_argument("--pr-label", default=DEFAULT_PR_LABEL)
    parser.add_argument("--severity-label-prefix", default=DEFAULT_SEVERITY_PREFIX)


def add_names(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pat-secret", default=DEFAULT_PAT_SECRET)
    parser.add_argument("--pat-secret-key", default=DEFAULT_PAT_SECRET_KEY)
    parser.add_argument("--ksa-name", default=DEFAULT_KSA)
    parser.add_argument("--gsa-name", default=DEFAULT_GSA)
    parser.add_argument(
        "--gcp-project",
        default="",
        help="the project the investigator GSA lives in, for checking the KSA annotation. "
        "The chart takes it from platformAgent.harness.projectId, not from a "
        "selfImprovement key.",
    )
    parser.add_argument("--agent-deployment", default=DEFAULT_AGENT_DEPLOYMENT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selfimprove_enable.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The order that works: preflight, then secret and labels, then values,\n"
            "then apply with helm or terraform, then verify."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preflight", help="check everything before enabling")
    add_common(p)
    add_token(p)
    add_repos(p)
    add_names(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("secret", help="install the PAT as a Secret")
    add_common(p)
    add_token(p)
    p.add_argument("--mode", choices=MODES, default="upstream", help=argparse.SUPPRESS)
    add_names(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--no-check-token",
        dest="check_token",
        action="store_false",
        default=True,
        help="store it without asking GitHub whether it works",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_secret)

    p = sub.add_parser("labels", help="create the labels the loop attaches")
    add_common(p)
    add_token(p)
    add_repos(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_labels)

    p = sub.add_parser("values", help="emit the chart values, as YAML or HCL")
    add_common(p)
    add_repos(p)
    add_names(p)
    p.add_argument("--format", choices=("yaml", "hcl", "json"), default="yaml")
    p.add_argument(
        "--api-server-cidrs",
        nargs="*",
        default=[],
        metavar="CIDR",
        help="additive to what the chart discovers; required for a template-rendered install",
    )
    p.add_argument("--dns-cidrs", nargs="*", default=[], metavar="CIDR")
    p.set_defaults(func=cmd_values)

    p = sub.add_parser("verify", help="check a live install against what it is running")
    add_common(p)
    add_token(p)
    add_repos(p)
    add_names(p)
    p.add_argument("--cronjob", default=DEFAULT_CRONJOB)
    p.add_argument("--networkpolicy", default=DEFAULT_NETWORKPOLICY)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KubeError as exc:
        print("kubectl: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Security gates for cron runs: read-only policy, code execution refuse, and
content checks.

Installed into the image at ``/opt/hermes/tools/cron_risk_gate.py`` and wired
into ``tools/approval.py`` by ``deploy/docker/Dockerfile``.

Addresses Issue #993 (THREAT-002, SKILL-002):
1. Terminal escape and control character injection (_ESC pattern).
2. Pure-ASCII lookalike TLD / domain evasion (e.g. kubernetes.io.evil-cdn.co).
3. Unconditional block on execute_code in autonomous cron runs.
4. Per-job risk tiers: 'high' applies a fail-closed read-only command policy
   (allowlist of inspection verbs); 'low' retains the denylist-only floor.

Gates 1-3 are unconditional on every cron run. ``approvals.cron_scan`` opts out
of the Tirith content scan only (see cron_tirith_scan.py), never these.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Callable, Optional

logger = logging.getLogger(__name__)

RISK_LOW = "low"
RISK_HIGH = "high"

CRON_SCAN_KEY = "cron_scan"
APPROVALS_KEY = "approvals"

MAX_LOG_COMMAND_LEN = 200
DEFAULT_MAX_GCLOUD_COMMAND_LEN = 5

MSG_EXECUTE_CODE_REFUSED = (
    "BLOCKED: execute_code is refused during autonomous cron runs "
    "(THREAT-002). Autonomous watchdogs may not execute raw code."
)
MSG_ESC_REFUSED = (
    "BLOCKED: command contains raw terminal escape or control characters "
    "(THREAT-002). Terminal escape injection is refused during cron runs."
)
MSG_LOOKALIKE_TEMPLATE = (
    "BLOCKED: command contains lookalike domain '{host}' mimicking trusted apex '{apex}' "
    "(THREAT-002). Lookalike domain evasion is refused during cron runs."
)
MSG_MUTATION_REFUSED = (
    "BLOCKED: command is not a recognized read-only inspection command and this "
    "cron job is classified read-only (risk=high, SKILL-002). Only allowlisted "
    "read commands run; the audit continues."
)

#: Characters that alter terminal state or conceal command strings:
#: C0 control characters (excluding newline \n, tab \t, carriage return \r),
#: DEL (\x7f), and C1 control characters (\x80-\x9f, including 8-bit CSI \x9b).
_ESC = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")

#: Apex domains trusted for Kubernetes and GKE platform operations.
TRUSTED_APEX = (
    "kubernetes.io",
    "googleapis.com",
    "github.com",
    "githubusercontent.com",
    "k8s.io",
    "x-k8s.io",
    "google.com",
    "gke.io",
)

#: Extracts hostname candidates from URLs (any URI scheme), CLI flags (--server=...),
#: @hosts, headers/colons, quotes, or tokens. Uses fixed-width lookbehinds so chained
#: delimiters (e.g. comma, semicolon, pipes, brackets, colon) are recognized without
#: prematurely consuming the separator.
_HOST_TOKEN = re.compile(
    r"(?:(?:[a-z][a-z0-9+.-]*:)?//|--[a-z0-9_-]+=|(?<=^)|(?<=[\s@'\"=,;|([{`:]))([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)",
    re.IGNORECASE,
)

#: Executables that are read-only in every invocation (text/inspection utils).
_READ_ONLY_TOOLS = frozenset({
    "grep", "egrep", "fgrep", "jq", "cut", "head",
    "tail", "wc", "cat", "tr", "column", "nl", "comm", "join", "paste", "fold",
    "rev", "echo", "printf", "date", "hostname", "pwd", "true", "test",
})

#: Wrappers / interpreters whose presence makes a segment unanalyzable -> refuse.
_INDIRECTION = frozenset({
    "sh", "bash", "zsh", "ash", "dash", "ksh", "python", "python3", "perl",
    "ruby", "node", "eval", "exec", "env", "xargs", "watch", "timeout", "nohup",
    "nice", "ssh", "sudo", "su", "find", "flock", "setsid", "stdbuf", "script",
    "awk", "gawk", "mawk", "nawk", "yq",
})

#: Subcommand tools: any mutating verb refuses; else a read verb is required.
_TOOL_MUTATE_VERBS = {
    "kubectl": {
        "create", "apply", "delete", "patch", "edit", "replace", "scale",
        "autoscale", "annotate", "label", "set", "rollout", "drain", "cordon",
        "uncordon", "taint", "exec", "cp", "attach", "port-forward", "proxy",
        "run", "expose", "rollback", "wait", "debug", "reconcile", "set-context",
        "set-cluster", "set-credentials", "use-context", "delete-context",
        "delete-cluster", "delete-user", "unset", "rename-context", "dump",
    },
    "oc": {"create", "apply", "delete", "patch", "edit", "replace", "scale",
           "rollout", "set", "adm"},
    "gcloud": {"create", "delete", "update", "set", "add", "remove", "enable",
               "disable", "reset", "resize", "patch", "import", "deploy",
               "rollback", "restart", "attach", "detach", "clear", "replace",
               "abandon", "cancel", "start", "stop", "suspend", "resume",
               "write", "publish", "cp", "rm", "mv", "call", "untag",
               "add-metadata", "add-iam-policy-binding", "get-credentials"},
    "gsutil": {"cp", "mv", "rm", "rsync", "mb", "rb", "setmeta", "acl", "iam"},
    "gh": {"create", "delete", "edit", "close", "merge", "comment", "clone"},
    "helm": {"install", "upgrade", "uninstall", "rollback", "delete"},
    "bq": {"insert", "mk", "rm", "update", "cp", "load"},
}
#: Standalone read commands permitted for kubectl without subcommand qualification.
_KUBECTL_STANDALONE_READ_VERBS = frozenset({
    "get", "describe", "logs", "top", "explain", "version",
    "api-resources", "api-versions", "cluster-info", "events", "diff",
})

#: Subcommand-qualified read commands permitted for kubectl.
_KUBECTL_SUBCOMMAND_READ_VERBS = {
    "auth": frozenset({"can-i", "whoami"}),
    "config": frozenset({"view", "get-contexts", "get-clusters", "get-users", "current-context"}),
    "rollout": frozenset({"status", "history"}),
}

#: Subcommand combinations that are explicitly refused even if parent verb is in read list.
_KUBECTL_REFUSED_SUBCOMMANDS = frozenset({("cluster-info", "dump")})

#: Flags known to consume the subsequent token as an argument value.
_TOOL_FLAGS_WITH_VALUE = {
    "kubectl": frozenset({
        "-n", "--namespace",
        "--context", "--kubeconfig",
        "-s", "--server",
        "--cluster", "--user",
        "-l", "--selector",
        "-o", "--output",
        "-f", "--filename",
        "-k", "--kustomize",
        "-c", "--container",
        "--as", "--as-group", "--as-uid", "--as-user-extra",
        "--certificate-authority", "--client-certificate", "--client-key",
        "--token", "--tls-server-name",
        "--request-timeout", "--cache-dir",
        "--field-selector",
        "-v", "--v", "--vmodule",
        "--sort-by", "--chunk-size",
        "--template",
        "--since", "--since-time", "--tail",
        "--timeout",
        "--profile", "--profile-output",
        "--password", "--username",
        "--log-flush-frequency", "--kuberc",
        "--raw", "--field-manager", "--cascade", "--resource-version",
    }),
    "oc": frozenset({
        "-n", "--namespace",
        "--context", "--kubeconfig",
        "-s", "--server",
        "--cluster", "--user",
        "-l", "--selector",
        "-o", "--output",
        "-f", "--filename",
        "-c", "--container",
        "--as", "--as-group", "--as-uid", "--as-user-extra",
        "--certificate-authority", "--client-certificate", "--client-key",
        "--token", "--tls-server-name",
        "--request-timeout", "--cache-dir",
        "--profile", "--profile-output",
        "--password", "--username",
        "--log-flush-frequency", "--kuberc",
        "--raw", "--field-manager", "--cascade", "--resource-version",
    }),
    "gcloud": frozenset({
        "--project", "--account", "--billing-project",
        "--configuration", "--format", "--filter",
        "--verbosity", "--zone", "--region",
        "--cluster", "--location", "--limit", "--sort-by",
        "--billing-account", "--order", "--start-time", "--end-time",
        "--zones", "--page-size",
    }),
    "gh": frozenset({
        "-R", "--repo",
        "-L", "--limit",
        "-X", "--method",
        "-q", "--jq",
        "-t", "--template",
        "-f", "-F", "--field", "--raw-field",
        "--input",
        "-H", "--header",
        "-s", "--state",
        "-a", "--assignee",
        "-A", "--author",
        "-l", "--label",
        "-m", "--milestone",
        "-S", "--search",
        "--order",
        "--json",
    }),
    "helm": frozenset({
        "-n", "--namespace",
        "--kube-context", "--kubeconfig",
        "--kube-apiserver", "--kube-token",
        "--kube-as-user", "--kube-as-group",
        "--kube-ca-file",
        "--registry-config", "--repository-cache", "--repository-config",
        "--post-renderer",
        "-o", "--output",
        "-f", "--values",
        "--revision",
    }),
    "gsutil": frozenset({
        "-o", "-h", "-b",
    }),
    "bq": frozenset({
        "--project_id", "--dataset_id", "--location",
        "--format",
        "--application_default_credential_file",
        "--api", "--job_id", "--fingerprint_job_id",
        "--max_rows", "-n",
    }),
}

#: Long and short flags known to take no argument (boolean flags), preventing
#: ambiguity when determining whether the subsequent token is a flag value.
_KNOWN_BOOLEAN_FLAGS = frozenset({
    "-h", "--help",
    "--version",
    "-A", "--all-namespaces",
    "--all",
    "--show-labels",
    "-w", "--watch",
    "-R", "--recursive",
    "--ignore-not-found",
    "--disable-compression",
    "--insecure-skip-tls-verify",
    "--match-server-version",
    "--warnings-as-errors",
    "--no-headers",
    "-q", "--quiet",
    "--paginate",
    "--web",
    "--debug",
    "--force",
    "--previous",
    "--timestamps",
    "--all-containers",
    "--prefix",
    "--containers",
    "--show-events",
    "--show-kind",
    "--show-managed-fields",
    "--uri",
    "--server-print",
    "--allow-missing-template-keys",
})

#: Nouns accepted as the primary command target for gh CLI before verb inspection.
_GH_NOUNS = frozenset({
    "pr", "issue", "repo", "release", "run", "workflow", "cache",
    "ruleset", "secret", "variable",
})

#: Pure mutating verbs that must never appear as a resource type in kubectl read commands.
_STANDALONE_READ_MUTATE_TYPE_BLOCK = frozenset({
    "delete", "patch", "apply", "create", "edit", "replace", "scale",
    "drain", "cordon", "taint", "dump",
})

_TOOL_READ_VERBS = {
    "kubectl": _KUBECTL_STANDALONE_READ_VERBS,
    "oc": {"get", "describe", "logs", "status", "whoami"},
    "gcloud": {
        "list", "describe", "info", "version", "get-iam-policy", "search", "read",
        "list-usable", "get-nat-mapping-info", "get-server-config",
    },
    "gsutil": {"ls", "stat", "cat", "du", "hash", "ver", "version"},
    "gh": {"view", "list", "status", "diff"},
    "helm": {"list", "get", "status", "history", "show", "search", "version"},
    # 'query' omitted deliberately: `bq query` executes DML (DELETE/UPDATE/MERGE).
    "bq": {"ls", "show", "head"},
}

try:
    from tools.command_policy import GCLOUD_READ_COMMANDS
except ImportError:
    try:
        from command_policy import GCLOUD_READ_COMMANDS
    except ImportError:
        import sys
        from pathlib import Path
        for _p in (
            Path(__file__).resolve().parents[3] / "agents" / "platform" / "scripts",
            Path("/opt/defaults/scripts"),
            Path("/opt/hermes/tools"),
        ):
            if _p.is_dir() and str(_p) not in sys.path:
                sys.path.insert(0, str(_p))
        try:
            from command_policy import GCLOUD_READ_COMMANDS
        except ImportError:
            GCLOUD_READ_COMMANDS = frozenset()

# Include compute instances reads if not already present in GCLOUD_READ_COMMANDS
_CRON_GCLOUD_READ_COMMANDS = GCLOUD_READ_COMMANDS | frozenset({
    ("compute", "instances", "list"),
    ("compute", "instances", "describe"),
})
_LONGEST_GCLOUD_COMMAND = max((len(cmd) for cmd in _CRON_GCLOUD_READ_COMMANDS), default=DEFAULT_MAX_GCLOUD_COMMAND_LEN)

#: Common command aliases normalized before verb classification.
_ALIAS = {"k": "kubectl", "kubectl.exe": "kubectl", "gcloud.cmd": "gcloud"}

#: Read-only local writes tolerated in a read-only run.
_REDIR_OK_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})

#: Command / process substitution — refused wholesale (executes even inside "double quotes").
_SUBST = re.compile(r"\$\(|`|<\(|>\(")

#: Punctuation characters recognized by shlex. Emits shell control operators as discrete tokens.
_SHLEX_PUNCTUATION_CHARS = "();<>|&\n"

#: Multi-character punctuation operators recognized in shell syntax.
_VALID_MULTI_PUNCT = frozenset({"&&", "||", ";;", ">>", ">&", "&>", "&>>", "<<", "<&"})

#: Decomposition regex to separate glued punctuation tokens emitted by shlex.
_PUNCT_SPLIT_RE = re.compile(r"&&|\|\||;;|&>>|>>|>&|&>|<<|<&|[();<>|&\n]")

#: Operators that end a segment or start a new command / pipeline.
_BREAK_OPERATORS = frozenset({";", "|", "&", "\n", "&&", "||", ";;"})

#: Shell redirection operators.
_REDIRECT_OPERATORS = frozenset({">", ">>", "<", "<<", ">&", "<&", "&>", "&>>"})
_FD_REDIRECT_RE = re.compile(r"^\d+(?:>>?|>&|<&)$")

#: Mutating operations that must never be bypassed by dry-run flags.
_NEVER_DRY_RUN_VERBS = frozenset({"exec", "cp", "attach", "port-forward", "proxy"})

#: Permitted dry-run flag values/prefixes for declarative kubectl commands.
_DRY_RUN_PREFIX = "--dry-run="
_DRY_RUN_VALID_PREFIXES = ("--dry-run=c", "--dry-run=s")
_DOUBLE_DASH = "--"


def _split_punct_token(tok: str) -> list[str]:
    """Decompose mixed punctuation tokens (e.g. ';(' -> [';', '(']) into distinct operators."""
    if tok and all(c in _SHLEX_PUNCTUATION_CHARS for c in tok):
        if tok in _VALID_MULTI_PUNCT or len(tok) == 1:
            return [tok]
        return _PUNCT_SPLIT_RE.findall(tok)
    return [tok]


#: A pure control-operator token (pipe, background, sequence, newline) ends a segment.
def _is_break(tok: str) -> bool:
    return tok in _BREAK_OPERATORS


#: A redirect operator token (e.g. '>', '>>', '2>', '>&', '&>').
def _is_redirect(tok: str) -> bool:
    return tok in _REDIRECT_OPERATORS or bool(_FD_REDIRECT_RE.match(tok))


def _load_config_readonly() -> dict:
    """Read config.yaml without taking a write lock, or ``{}``.

    Deferred import so importing approval.py at startup does not load configuration early.
    """
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:
        return {}


def cron_scan_enabled(config: Optional[dict]) -> bool:
    """Whether ``approvals.cron_scan`` leaves the scan on. Default: yes.

    Anything other than an explicit false-y value keeps the scan, matching the
    opt-out contract in cron_tirith_scan.py.
    """
    approvals = (config or {}).get(APPROVALS_KEY)
    if not isinstance(approvals, dict):
        return True
    return bool(approvals.get(CRON_SCAN_KEY, True))


def _lex_segments(command: str) -> Optional[list[list[str]]]:
    """Tokenize a command into segments, honoring quotes and shell operators.

    Uses a single ``shlex`` pass with ``punctuation_chars`` so pipes, ``&``,
    ``&&``, ``;`` and newlines are emitted as their own tokens (and so cannot
    hide a second command), while ``|``/``;`` inside quotes stay part of the
    argument they belong to. Mixed punctuation tokens (e.g. ';(') are decomposed
    so break operators cannot be masked. Returns None if unlexable.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=_SHLEX_PUNCTUATION_CHARS)
    lex.whitespace_split = True
    lex.commenters = ""                       # '#' is data here, never a comment
    lex.whitespace = " \t\r"                  # newline is an operator, not whitespace
    try:
        raw_tokens = list(lex)
    except ValueError:
        return None
    tokens: list[str] = []
    for t in raw_tokens:
        tokens.extend(_split_punct_token(t))
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if _is_break(tok):
            segments.append(current)
            current = []
        else:
            current.append(tok)
    segments.append(current)
    return segments


def _extract_command_and_subcommand(
    tokens: list[str],
    flags_with_value: frozenset[str],
    known_boolean_flags: frozenset[str] = _KNOWN_BOOLEAN_FLAGS,
) -> tuple[str, str, list[str], list[str], Optional[str]]:
    """Extract (command, subcommand, flags, positionals, ambiguous_flag)."""
    dashdash_idx = tokens.index(_DOUBLE_DASH) if _DOUBLE_DASH in tokens else len(tokens)
    pre_dash = tokens[:dashdash_idx]

    flags: list[str] = []
    positionals: list[str] = []
    ambiguous_flag: Optional[str] = None
    i = 0
    while i < len(pre_dash):
        tok = pre_dash[i]
        if tok.startswith("-"):
            flags.append(tok)
            if "=" in tok:
                i += 1
            elif tok in flags_with_value and i + 1 < len(pre_dash):
                i += 2
            elif tok in known_boolean_flags:
                i += 1
            elif tok.startswith("--"):
                # Unrecognised long flag without '=' followed by another token:
                # Ambiguous whether the next token is consumed as a flag value or is an
                # independent token (subcommand, argument, or subsequent flag like --dry-run).
                if i + 1 < len(pre_dash):
                    if ambiguous_flag is None:
                        ambiguous_flag = tok
                i += 1
            else:
                i += 1
        else:
            positionals.append(tok)
            i += 1

    cmd = positionals[0] if positionals else ""
    subcmd = positionals[1] if len(positionals) > 1 else ""
    return cmd, subcmd, flags, positionals, ambiguous_flag


def _evaluate_command_tokens(
    exe: str,
    cmd: str,
    subcmd: str,
    flags: list[str],
    positionals: list[str],
) -> bool:
    """Classify extracted command, subcommand, flags, positionals for executable."""
    pos_lower = [p.lower() for p in positionals]
    read = _TOOL_READ_VERBS.get(exe)
    mutate = _TOOL_MUTATE_VERBS.get(exe)
    if read is None:
        return False

    if exe in ("kubectl", "oc"):
        if not cmd:
            return False

        if (cmd, subcmd) in _KUBECTL_REFUSED_SUBCOMMANDS:
            return False

        # Subcommand-gated read tools (e.g. 'kubectl auth can-i', 'kubectl config view')
        if cmd in _KUBECTL_SUBCOMMAND_READ_VERBS:
            return subcmd in _KUBECTL_SUBCOMMAND_READ_VERBS[cmd]

        # Standalone read commands (e.g. 'kubectl get pods', 'kubectl describe ns')
        if cmd in _KUBECTL_STANDALONE_READ_VERBS:
            if cmd in ("get", "describe", "explain") and len(positionals) > 1 and positionals[1].lower() in _STANDALONE_READ_MUTATE_TYPE_BLOCK:
                return False
            return True

        if exe == "oc" and cmd in _TOOL_READ_VERBS["oc"]:
            return True

        # Mutating commands with valid dry-run (e.g. 'kubectl delete ns foo --dry-run=client')
        # _NEVER_DRY_RUN_VERBS must never be approved via dry-run flags.
        if cmd in mutate and cmd not in _NEVER_DRY_RUN_VERBS:
            dry_run_flags = [t for t in flags if t.startswith(_DRY_RUN_PREFIX)]
            if dry_run_flags:
                last_dry_run = dry_run_flags[-1]
                if last_dry_run.startswith(_DRY_RUN_VALID_PREFIXES):
                    return True

        return False

    if exe == "bq":
        if not cmd:
            return False
        cmd_lower = cmd.lower()
        if cmd_lower in _TOOL_READ_VERBS["bq"]:
            if any(p in _TOOL_MUTATE_VERBS["bq"] or p == "query" for p in pos_lower):
                return False
            return True
        return False

    if exe == "helm":
        if not cmd:
            return False
        cmd_lower = cmd.lower()
        if cmd_lower in _TOOL_READ_VERBS["helm"]:
            if any(p in _TOOL_MUTATE_VERBS["helm"] for p in pos_lower):
                return False
            return True
        return False

    if exe == "gsutil":
        if not cmd:
            return False
        cmd_lower = cmd.lower()
        if cmd_lower in _TOOL_READ_VERBS["gsutil"]:
            if any(p in _TOOL_MUTATE_VERBS["gsutil"] for p in pos_lower):
                return False
            return True
        return False

    if exe == "gh":
        if not cmd:
            return False
        cmd_lower = cmd.lower()
        subcmd_lower = subcmd.lower()
        if cmd_lower in _GH_NOUNS and subcmd_lower in _TOOL_READ_VERBS["gh"]:
            if any(p in _TOOL_MUTATE_VERBS["gh"] for p in pos_lower):
                return False
            return True
        if cmd_lower == "search":
            if any(p in _TOOL_MUTATE_VERBS["gh"] for p in pos_lower):
                return False
            return True
        return False

    if exe == "gcloud":
        if not pos_lower:
            return False
        if any(p in _TOOL_MUTATE_VERBS["gcloud"] for p in pos_lower):
            return False
        longest = min(len(pos_lower), _LONGEST_GCLOUD_COMMAND)
        matched_path = None
        for length in range(1, longest + 1):
            cand = tuple(pos_lower[:length])
            if cand in _CRON_GCLOUD_READ_COMMANDS:
                matched_path = cand
                break
        if matched_path is None:
            return False
        if matched_path[-1] not in _TOOL_READ_VERBS["gcloud"]:
            return False
        return True

    return False


def _segment_is_read_only(tokens: list[str]) -> bool:
    """Classify one already-tokenized segment. Unknown/unanalyzable -> False."""
    cleaned: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Consume leading fd digit if adjacent to redirect (e.g. '2', '>', '/dev/null')
        if tok.isdigit() and i + 1 < len(tokens) and _is_redirect(tokens[i + 1]):
            tok = tokens[i + 1]
            i += 1
        if _is_redirect(tok):
            # fd dup: >&2, 2>&1, <&0, >&- (target is digit or '-')
            if (tok.endswith(">&") or tok.endswith("<&")) and i + 1 < len(tokens) and (tokens[i + 1].isdigit() or tokens[i + 1] == "-"):
                i += 2
                continue
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if target not in _REDIR_OK_TARGETS:
                return False
            i += 2
            continue
        cleaned.append(tok)
        i += 1

    if not cleaned:
        return False
    # Refuse any leading VAR=value assignment (e.g. LD_PRELOAD=..., PATH=..., KUBECONFIG=...)
    if "=" in cleaned[0] and not cleaned[0].startswith(("-", "/")):
        return False

    exe = cleaned[0].rsplit("/", 1)[-1]
    exe = _ALIAS.get(exe, exe)
    if "$" in exe or exe.startswith("-"):
        return False
    if exe in _INDIRECTION:
        return False
    if exe in _READ_ONLY_TOOLS:
        return True

    read = _TOOL_READ_VERBS.get(exe)
    if read is None:
        return False

    rest = cleaned[1:]
    flags_with_val = _TOOL_FLAGS_WITH_VALUE.get(exe, frozenset())
    cmd, subcmd, flags, positionals, amb_flag = _extract_command_and_subcommand(rest, flags_with_val)

    if amb_flag is None:
        return _evaluate_command_tokens(exe, cmd, subcmd, flags, positionals)

    # Dual-hypothesis evaluation: test ambiguous flag as value-taking and as boolean
    cmd_v, subcmd_v, flags_v, pos_v, amb_v = _extract_command_and_subcommand(
        rest, flags_with_val | {amb_flag}
    )
    cmd_b, subcmd_b, flags_b, pos_b, amb_b = _extract_command_and_subcommand(
        rest, flags_with_val, _KNOWN_BOOLEAN_FLAGS | {amb_flag}
    )
    if amb_v is not None or amb_b is not None:
        # Multiple cascading ambiguous flags: fail closed
        return False

    res_value = _evaluate_command_tokens(exe, cmd_v, subcmd_v, flags_v, pos_v)
    res_bool = _evaluate_command_tokens(exe, cmd_b, subcmd_b, flags_b, pos_b)
    # Refuse unless both hypotheses agree that the command is read-only
    return res_value and res_bool


def cron_command_policy_block(command: str, risk: str | None) -> Optional[dict]:
    """Refuse anything not provably read-only when the job is 'high' risk.

    'low' keeps the denylist-only floor (returns None). 'high' (and the
    fail-closed unannotated default) requires every shell segment to be an
    allowlisted read-only command; unknown, mutating, or unanalyzable commands
    are refused while the run continues.
    """
    if (risk or RISK_HIGH).strip().lower() == RISK_LOW:
        return None
    if not command or not isinstance(command, str):
        return None

    segments = None if _SUBST.search(command) else _lex_segments(command)
    refused = segments is None or not all(
        _segment_is_read_only(seg) for seg in segments if seg
    )
    if refused:
        logger.warning(
            "Cron risk gate block [read-only]: refused non-read command (command: %s)",
            command[:MAX_LOG_COMMAND_LEN],
        )
        return {"approved": False, "message": MSG_MUTATION_REFUSED}
    return None


def cron_execute_code_block() -> Optional[dict]:
    """Refuse execute_code unconditionally during autonomous cron runs.

    Autonomous watchdogs have no human operator present and must perform their
    actions using declared tools and read-only commands rather than running
    arbitrary embedded scripts.
    """
    logger.warning("Cron risk gate block [execute_code]: %s", MSG_EXECUTE_CODE_REFUSED)
    return {
        "approved": False,
        "message": MSG_EXECUTE_CODE_REFUSED,
    }


def find_lookalike_domain(command: str) -> Optional[tuple[str, str]]:
    """Detect whether any host token in the command mimics a trusted apex domain.

    Returns (detected_host, matched_apex) if a lookalike is detected, else None.
    Legitimate exact matches (e.g. 'k8s.io') and proper subdomains (e.g.
    'storage.googleapis.com', 'raw.githubusercontent.com', 'kubeagents.x-k8s.io')
    pass cleanly.
    """
    if not command or not isinstance(command, str):
        return None

    for match in _HOST_TOKEN.finditer(command):
        raw = match.group(1).lower().rstrip(".:/'\"")
        for apex in TRUSTED_APEX:
            # Legitimate apex or proper subdomain of apex
            if raw == apex or raw.endswith("." + apex):
                continue
            # Lookalike evasion: token contains the apex at a dot/label boundary
            # (e.g. 'kubernetes.io.evil-cdn.co' or 'sub.kubernetes.io.attacker.com')
            # rather than terminating at the apex.
            if raw.startswith(apex + ".") or ("." + apex + ".") in raw:
                return raw, apex
    return None


def cron_content_block(
    command: str,
    *,
    load_config: Optional[Callable[[], dict]] = None,
) -> Optional[dict]:
    """Refuse escape/control-char injection and lookalike-domain evasion.

    Unconditional on every cron run: ``approvals.cron_scan`` gates the Tirith
    content scan only, not these THREAT-002 evasion classes. ``load_config`` is
    accepted for backward-compatible call sites and ignored.
    """
    if not command or not isinstance(command, str):
        return None

    if _ESC.search(command):
        logger.warning(
            "Cron risk gate block [escape]: command contains raw control/escape characters (command: %s)",
            command[:MAX_LOG_COMMAND_LEN],
        )
        return {
            "approved": False,
            "message": MSG_ESC_REFUSED,
        }

    lookalike = find_lookalike_domain(command)
    if lookalike is not None:
        host, apex = lookalike
        logger.warning(
            "Cron risk gate block [lookalike]: command contains lookalike domain '%s' mimicking apex '%s' (command: %s)",
            host,
            apex,
            command[:MAX_LOG_COMMAND_LEN],
        )
        return {
            "approved": False,
            "message": MSG_LOOKALIKE_TEMPLATE.format(host=host, apex=apex),
        }

    return None

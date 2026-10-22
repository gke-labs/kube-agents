"""
readiness_rules — the per-entry rules `upgrade_readiness.EXTRA_RULES` runs after the PDB,
maintenance and skew rules of `fleet_upgrade_report.py --readiness`.

One module per entry of docs/designs/upgrade-failure-catalogue.md. Each module exposes:

- `RULE_ID`, the slug the report keys the rule's counts under and stamps on its findings;
  `ENTRY`, the catalogue entry number; `CAN_BLOCK`, whether a finding of the rule can ever
  block the upgrade. A rule with `CAN_BLOCK = False` files risks and notes; an `unknown` it
  files is reported in the note and never moves the member's verdict, since the finding it
  could not make would not have blocked either, and a blocker it files is demoted to a risk
  with a note saying so.
- `evaluate(cluster, member, read, target, context) -> dict`, a pure function over what the
  report has already read, returning `blocking`, `risks`, `unknown` and `notes`, each a list
  (`new_result()` is the empty one; a key left out is an empty list, a key outside the four
  is refused, so a misnamed list is the rule's `unknown` rather than a clean rule): `cluster`
  is the `gcloud container clusters list` record; `member` the graded version row; `read` the
  member's kubectl reads (below); `target` the parsed target version tuple, None without
  one; `context` the per-member read context (below). Each argument is the rule's own copy
  (the fleet records in the context too), so a rule's writes to them reach no other rule
  and not the report; the audit-log cache is the one shared thing, through `audit_log.read`,
  which hands out copies.
- `describe(finding) -> str`, one blocking or risk finding as a table cell.

`read` is the dict `fleet_upgrade_report.read_cluster_objects` returns. `items` is every
object the reads returned, None when `get-credentials` or the PDB read failed, in which case
the reads after the webhook read were skipped and `error` says why. `read_errors` is keyed by
kind as the reads after the webhook read name them (READ_KINDS), each None or the error of the
read that carried the kind:
the namespaced kinds (NAMESPACED_READ_KINDS) come in one read, the Pods in one of their own,
the Nodes in one through a jsonpath template, the StorageClasses and PersistentVolumes in
one. A read the credential proxy cut at its output cap is that kind's read error, naming the
cap. A rule that needs a kind asks `read_error(read, kind)` and files one `unknown` naming the
reason when the kind was not read, grading what it has; a refused or cut read never costs a
rule that does not need it. A rule that grades the PDB read's Deployments and StatefulSets
and the namespaced read's DaemonSets and CronJobs files `workloads_unread_detail(read, kinds)`
when one of its reads failed: the text names the kinds not read, the PDB read's included when
`items` is None, and the kinds graded, or says nothing was.

`context` carries `project`, `location` and `cluster_name` (for a log filter), `run_cmd`
(the report's runner) with `timeout_seconds` (the per-call cap a rule passes to it), `at`
(the aware instant the report evaluates at), `cache` (a dict a rule fills so another rule
reads the same thing once; the audit-log reads live under `audit_log.CACHE_KEY`), `clusters`
(every cluster record the run listed, for a rule that compares a member with its peers; the
whole listing of the projects even when `--cluster` narrows which members are graded, since
narrowing selects whom to grade, not whom a rule may see),
`pools` (the member's pools: `name`, `version`, `parsed`, the record's `config` and
`autoscaling`), `target_text`, `master` (the parsed control-plane version) and `autopilot`;
tests may add `clock`, a monotonic clock for a read's time budget.

A finding is a dict the rule shapes; the fold copies it before it stamps it, so a rule may
return the same object twice or cache what it returns. The fold in
`upgrade_readiness.evaluate_extra_rules` stamps `rule` and `entry` on every finding and
`text` (the rule's `describe`, collapsed to one line) on every blocking and risk finding, and
turns each `unknown` entry (a string, or a dict carrying `reason` or `detail`) into a dict
with `reason`, `can_block` and the `text` the note column prints, the reason collapsed to one
line of at most NOTE_EXCERPT_CHARS. A finding is written to the JSON as it is, so it holds
JSON values only; a rule that raises (`SystemExit` included: `sys.exit()` in a rule is its
defect, not the run's end; `KeyboardInterrupt` and `GeneratorExit` alone pass through),
returns a shape the contract forbids, or returns a value JSON cannot carry becomes one
`unknown` finding, never a crash, and a module whose
`RULE_ID` is not a string, whose `ENTRY` is not a JSON value or whose `CAN_BLOCK` is not a
bool is not evaluated: one `unknown` under its module name. A second module that carries a
registered `RULE_ID` is not evaluated either: it is one `unknown` under that id with that
rule's `CAN_BLOCK`, so the per-rule record and its entries never disagree. A rule reads; it
runs no command of its own except through `context["run_cmd"]`.

This file holds what the modules share: the result shape, the read kinds and context keys,
the quantity parser, a pod's owner and requests (CPU and memory, and the extended resources
such as `nvidia.com/gpu` by name), node helpers, pod templates with their
selectors and node-pool pins, image tags, the cgroup transition, `shell_scripts(command)`,
the scripts a container's command hands a shell through `-c`, `shell_words(script)`, the
programs such a script names, and `shell_forks(script)`,
whether a `sh -c` script puts a command in the background (True or False; None when the
script cannot be read: an unterminated quote, heredoc or expansion, which a rule reports as
unknown).
`finding.py` holds the tiered-finding helpers, `audit_log.py` the Cloud Logging reads.
"""

import math
import re

import upgrade_shape_tables as tables

RESULT_KEYS = ("blocking", "risks", "unknown", "notes")
# A table cell is one line. Text that reaches a cell (a finding's `text`, an unknown's
# reason, a note, a read's error) is collapsed to one line here, and a reason or an error is
# cut to this many characters; the JSON keeps the whole text.
NOTE_EXCERPT_CHARS = 160
# How much of an exception's text a guard keeps: a rule may raise with a tenant-sized payload
# in its arguments, and the reason goes to the JSON whole.
ERROR_EXCERPT_CHARS = 300
WHITESPACE_JOIN = " "
# What a guard around a rule's code lets through: the interpreter's own signals, which no
# rule raises as a verdict. Everything else a rule raises is the rule's defect, `SystemExit`
# included: `sys.exit()` in a module ported from a script is a BaseException, and a guard
# that caught `Exception` alone let it end the run after every member was read, with no
# table, no `--output` and no rollout record.
NOT_A_RULE_DEFECT = (KeyboardInterrupt, GeneratorExit)
# What the credential proxy prints on stderr when it cut a brokered command's stdout at its
# output cap, with the command's own exit code; every read through the shims (the report's
# kubectl reads, the audit-log reader's gcloud reads) checks it before parsing.
PROXY_TRUNCATION_MARKER = "credential proxy output truncated"
GRADE_BLOCKING = "blocking"
GRADE_RISK = "risk"
GRADE_UNKNOWN = "unknown"
LIST_SEPARATOR = ", "

# The kinds `fleet_upgrade_report` reads per member, as `kubectl get` names them. The PDB
# read is the one on main; the reads after it fail on their own, and `read_errors` is keyed
# by the kinds below. Pods are a read of their own: the pod list is the one most likely to
# exceed the credential proxy's output cap, and a cut read must cost only the rules that
# need pods, never the five smaller kinds.
PDB_READ_KINDS = ("pdb", "deploy", "statefulset")
NAMESPACED_READ_KINDS = ("daemonset", "cronjob", "pvc", "networkpolicy", "namespace")
POD_READ_KIND = "pods"
NODE_READ_KIND = "node"
CLUSTER_READ_KINDS = ("storageclass", "pv")
READ_KINDS = NAMESPACED_READ_KINDS + (POD_READ_KIND, NODE_READ_KIND) + CLUSTER_READ_KINDS
# The reason `read_errors` carries for every kind when the reads after the webhook read did not
# run: the kubeconfig was never written (the directory or `get-credentials` failed, so no read
# ran), or the PDB read failed, which says nothing of the webhook read, made or skipped on its
# own terms and graded beside these kinds.
NO_READ_RAN_REASON = "the member's kubeconfig was not written, so no read ran"
LATER_READS_SKIPPED_REASON = "the PDB read failed, so the reads after the webhook read were skipped"
# What a rule that grades the PDB read's workloads and some of the namespaced read's kinds says
# when a read it needs failed: the kinds not read, then the kinds graded. The PDB read's
# workload kinds are unread exactly when `items` is None, and then nothing was graded.
PDB_WORKLOAD_KINDS = ("deploy", "statefulset")
READ_KIND_NAMES = {"deploy": "Deployments", "statefulset": "StatefulSets", "daemonset": "DaemonSets", "cronjob": "CronJobs"}
WORKLOADS_UNREAD_FORMAT = "{unread} not read ({reason}); {graded}"
WORKLOADS_GRADED_FORMAT = "{kinds} graded"
WORKLOADS_NOTHING_GRADED = "nothing graded"
KIND_LIST_LAST_SEPARATOR = " and "

# The per-member context keys (the module docstring says what each holds).
CONTEXT_PROJECT = "project"
CONTEXT_LOCATION = "location"
CONTEXT_CLUSTER_NAME = "cluster_name"
CONTEXT_RUN_CMD = "run_cmd"
CONTEXT_TIMEOUT_SECONDS = "timeout_seconds"
CONTEXT_AT = "at"
CONTEXT_CACHE = "cache"
CONTEXT_CLOCK = "clock"
CONTEXT_CLUSTERS = "clusters"
CONTEXT_POOLS = "pools"
CONTEXT_TARGET_TEXT = "target_text"
CONTEXT_MASTER = "master"
CONTEXT_AUTOPILOT = "autopilot"
CONTEXT_KEYS = (
    CONTEXT_PROJECT,
    CONTEXT_LOCATION,
    CONTEXT_CLUSTER_NAME,
    CONTEXT_RUN_CMD,
    CONTEXT_TIMEOUT_SECONDS,
    CONTEXT_AT,
    CONTEXT_CACHE,
    CONTEXT_CLUSTERS,
    CONTEXT_POOLS,
    CONTEXT_TARGET_TEXT,
    CONTEXT_MASTER,
    CONTEXT_AUTOPILOT,
)

# The kinds the reads carry, as `kind` spells them.
KIND_POD = "Pod"
KIND_NODE = "Node"
KIND_DEPLOYMENT = "Deployment"
KIND_STATEFULSET = "StatefulSet"
KIND_DAEMONSET = "DaemonSet"
KIND_REPLICASET = "ReplicaSet"
KIND_CRONJOB = "CronJob"
KIND_PERSISTENT_VOLUME = "PersistentVolume"
KIND_PERSISTENT_VOLUME_CLAIM = "PersistentVolumeClaim"
KIND_STORAGE_CLASS = "StorageClass"
KIND_NETWORK_POLICY = "NetworkPolicy"
KIND_NAMESPACE = "Namespace"
KIND_POD_DISRUPTION_BUDGET = "PodDisruptionBudget"
# Where a workload's pod template sits, per kind that carries one. Bare Pods and Jobs are
# not templates; a CronJob's sits under its jobTemplate.
TEMPLATE_PATHS = {
    KIND_DEPLOYMENT: ("spec", "template"),
    KIND_STATEFULSET: ("spec", "template"),
    KIND_DAEMONSET: ("spec", "template"),
    KIND_CRONJOB: ("spec", "jobTemplate", "spec", "template"),
}
# A pod's owner and how a finding names it. A ReplicaSet is named for its Deployment:
# the ReplicaSet's name is the Deployment's plus `-<pod-template-hash>`.
POD_TEMPLATE_HASH_LABEL = "pod-template-hash"
OWNER_FORMAT = "{kind} {namespace}/{name}"
OBJECT_FORMAT = "{namespace}/{name}"
DESCRIBE_FORMAT = "{kind} {object}: {detail}"
RULE_LEVEL_FORMAT = "{rule}: {detail}"
NAME_SEPARATOR = "-"
# A static pod's mirror; the kubelet recreates it on the rebuilt node, so a drain never
# has to find it room.
MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"
# Pod phases that hold no node capacity.
TERMINAL_PHASES = ("Succeeded", "Failed")
NODE_READY_CONDITION = "Ready"
CONDITION_TRUE = "True"
# Kubernetes resource quantities: a decimal number with an optional SI or binary suffix.
QUANTITY_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)$")
BINARY_SUFFIXES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6}
DECIMAL_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
MILLIS_PER_CORE = 1000
BYTES_PER_MI = 1024**2
CPU_FORMAT = "{millis}m"
MEMORY_FORMAT = "{mebibytes}Mi"
CPU_KEY = "cpu"
MEMORY_KEY = "memory"
# An extended resource (`nvidia.com/gpu`, `google.com/tpu`, a device plugin's) carries a
# domain in its name; the scheduler counts it as it counts CPU and memory, per pod against
# the node's allocatable, so a placement that ignores it lands a GPU pod on a node whose GPU
# is taken.
EXTENDED_RESOURCE_SEPARATOR = "/"
# A restartable init container (a native sidecar) runs alongside the app containers and
# every init container started after it; `overhead` is what the RuntimeClass reserves on top.
RESTART_POLICY_FIELD = "restartPolicy"
RESTART_POLICY_ALWAYS = "Always"
OVERHEAD_FIELD = "overhead"
# Node affinity, as the PodSpec spells it. Only required terms are read: a preferred term
# never keeps a pod off a node. The operators are the NodeSelectorRequirement's; `In` and
# `Exists` select nodes carrying the label, the others select nodes without it or by value.
AFFINITY_REQUIRED = "requiredDuringSchedulingIgnoredDuringExecution"
OP_IN = "In"
OP_NOT_IN = "NotIn"
OP_EXISTS = "Exists"
OP_DOES_NOT_EXIST = "DoesNotExist"
POSITIVE_OPERATORS = (OP_IN, OP_EXISTS)
WHERE_NODE_SELECTOR = "nodeSelector"
WHERE_NODE_AFFINITY = "nodeAffinity"
# What `cgroup_transition` answers for a pool against the target.
CGROUP_MOVES = "moves"
CGROUP_STAYS_V1 = "stays-v1"
CGROUP_ALREADY_V2 = "already-v2"
CGROUP_UNKNOWN = "unknown"
CGROUP_REASON_MIGRATION = "GKE migrates cgroup v1 pools to v2 from 1.{minor}"
CGROUP_REASON_PIN_ENDS = "the pool is pinned to cgroup v1 and GKE removes cgroup v1 at 1.{minor}"
CGROUP_REASON_STAYS = "the pool stays on cgroup v1 below 1.{minor}"
CGROUP_REASON_ALREADY = "already on cgroup v2"
CGROUP_REASON_NO_TARGET = "no target to decide whether the pool moves to cgroup v2"
CGROUP_REASON_UNREAD = "the pool record carries no effectiveCgroupMode"
IMAGE_DIGEST_SEPARATOR = "@"
IMAGE_TAG_SEPARATOR = ":"
# A `sh -c` script's background jobs, for the rules that grade a container's process count.
# The quoting pass is a character scan rather than shlex: shlex in POSIX mode strips the
# quotes, so a quoted `'&'` comes back as the same token as the operator. A run of operator
# characters is split by longest match, so `)&` is a subshell then a fork and `2>&1` is
# `>&`, never `>` then a fork.
SHELL_OPERATOR_CHARS = "&|;()<>"
SHELL_WORD_BREAKS = SHELL_OPERATOR_CHARS + " \t\n"
SHELL_OPERATORS = ("<<<", "&>>", ";;&", "<<-", "&&", "||", ";;", ";&", "|&", ">&", "<&", "&>", ">>", "<<", ">|", "<>", "&", "|", ";", "(", ")", "<", ">")
SHELL_FORK_OPERATOR = "&"
SHELL_HEREDOC_OPERATOR = "<<"
SHELL_HERESTRING_OPERATOR = "<<<"
SHELL_HEREDOC_STRIP_TABS = "-"
SHELL_SINGLE_QUOTE = "'"
SHELL_DOUBLE_QUOTE = '"'
SHELL_QUOTES = SHELL_SINGLE_QUOTE + SHELL_DOUBLE_QUOTE
SHELL_ESCAPE = "\\"
SHELL_ESCAPED_PAIR = 2  # a backslash and the character it escapes
SHELL_COMMENT = "#"
SHELL_NEWLINE = "\n"
SHELL_TAB = "\t"
SHELL_BLANKS = " \t"
SHELL_NEUTRAL = "_"  # what a quoted or escaped character becomes: a word character, so words keep their shape
SHELL_OPERATOR_RUN_RE = re.compile("[" + re.escape(SHELL_OPERATOR_CHARS) + "]+")
# The expansions: `$((…))` and `((…))` (arithmetic, where `&` and `<<` are operators on
# numbers) and `${…}` (a parameter, where `&` is text) are words to the scanner, their
# contents neutral to the matching close; `$(…)` and backticks are command lists and are
# scanned, inside double quotes too, since a lone `&` in them is a fork in that subshell.
SHELL_ARITHMETIC_EXPANSION = "$(("
SHELL_ARITHMETIC_COMMAND = "(("
SHELL_ARITHMETIC_DEPTH = 2  # `((` opens two parentheses
SHELL_PARAMETER_EXPANSION = "${"
SHELL_BRACKET_ARITHMETIC = "$["  # bash's older arithmetic expansion, deprecated and still parsed by bash 5; a word, as `$((…))` is
SHELL_OPEN_BRACKET = "["
SHELL_CLOSE_BRACKET = "]"
SHELL_EXPANSION_OPENERS = (SHELL_ARITHMETIC_EXPANSION, SHELL_PARAMETER_EXPANSION, SHELL_BRACKET_ARITHMETIC)
SHELL_COMMAND_SUBSTITUTION = "$("
SHELL_BACKTICK = "`"
SHELL_OPEN_PAREN = "("
SHELL_CLOSE_PAREN = ")"
SHELL_OPEN_BRACE = "{"
SHELL_CLOSE_BRACE = "}"
SHELL_ARITHMETIC_CLOSE = "))"  # what closes arithmetic; `((` closed by a lone `)` is two parentheses, as bash reads it
SHELL_ANSI_QUOTE = "$'"  # bash's ANSI-C quoting, a span to the first `'` not escaped
SHELL_CASE_WORD = "case"  # a `case … esac` inside a substitution: its patterns' `)` close nothing
SHELL_ESAC_WORD = "esac"
# Reserved words are words only at command position (the start of a command list, after a
# separator, a parenthesis, a newline, or one of the words below, after which a command
# follows); `grep case file` names a file. `coproc` and a process substitution (`<(…)`,
# `>(…)`) run their command list asynchronously, so each is a fork, emitted as one.
SHELL_COPROC_WORD = "coproc"
SHELL_COMMAND_PREFIX_WORDS = ("if", "then", "else", "elif", "while", "until", "do", "!", "time")
# A `{` that stands as a word opens a group wherever it stands (`{ a; b; }`, `f() { … }`,
# `function f { … }`, `coproc f { … }`): a command follows, so `case` inside it is read.
SHELL_GROUP_OPEN = "{"
SHELL_COMMAND_SEPARATORS = ";&|"
SHELL_REDIRECTION_CHARS = "<>"
SHELL_PROCESS_SUBSTITUTIONS = ("<(", ">(")
SHELL_FORK_MARK = " & "
# Substitutions and expansions open at once; deeper is unread. With the end of each
# arithmetic span memoised per call, the scan is polynomial in the depth, and the cap keeps
# a crafted container command from costing the report more than a bounded walk.
SHELL_NESTING_CAP = 64
PATH_SEPARATOR = "/"
# A shell reads options until its first non-option word; `-c` (or `-ec`, `-lc`: any short
# flag group carrying `c`, SHELL_COMMAND_FLAG_RE) makes the word after the options the
# script, so `["bash", "-o", "pipefail", "-c", "a && b"]` and `["sh", "-e", "-c", "a && b"]`
# carry a script too. An option word starts with `-` or `+` (bash's `+O`); the ones that take
# the next word as their argument (`-o pipefail`, `+O extglob`, `--rcfile FILE`, and a short
# group ending in `o` or `O`, `-eo pipefail`) are stepped over with it. A script's words, for a
# rule that asks which programs it runs, are what is left between whitespace, the operator
# characters, the quotes and the backtick (a command substitution's delimiter, which the
# scanner below reads as a span), each word's last path component, so a quoted path names its
# program too.
SHELL_OPTION_PREFIXES = ("-", "+")
SHELL_OPTIONS_WITH_ARGUMENT = ("-o", "+o", "-O", "+O", "--rcfile", "--init-file")
SHELL_OPTION_ARGUMENT_GROUP_RE = re.compile(r"^[-+][a-zA-Z]*[oO]$")
SHELL_WORD_SPLIT_RE = re.compile("[" + re.escape(SHELL_OPERATOR_CHARS + SHELL_QUOTES + SHELL_BACKTICK) + r"\s]+")


# -------------------------------------------------------------------- results and reads


def new_result() -> dict:
    """An empty rule result, every key present."""
    return {key: [] for key in RESULT_KEYS}


def exception_text(error: BaseException, limit: int = ERROR_EXCERPT_CHARS) -> str:
    """`repr(error)` on one line, cut to `limit`, for a guard that names what a rule raised:
    when the repr itself raises (an argument with a broken `__repr__`), the exception's type
    name, so the guard's own rendering is never what escapes it."""
    try:
        text = repr(error)
    except NOT_A_RULE_DEFECT:
        raise
    except BaseException:  # noqa: BLE001 - whatever the argument's __repr__ raised, SystemExit included, the guard still names the type
        text = type(error).__name__
    return one_line(text, limit)


def one_line(text, limit: int | None = None) -> str:
    """`text` with every run of whitespace, line breaks included, as one space, cut to
    `limit` characters when one is given: the shape a table cell can carry."""
    flat = WHITESPACE_JOIN.join(str(text if text is not None else "").split())
    return flat[:limit] if limit else flat


def new_finding(rule: str, entry: int, grade: str, obj: dict, detail: str, **extra) -> dict:
    """One finding on `obj` (a dict from `templates`), with the rule's own fields after."""
    return {"rule": rule, "entry": entry, "grade": grade, "kind": obj["kind"], "namespace": obj["namespace"], "name": obj["name"], "object": obj["object"], "detail": detail, **extra}


def rule_unknown(rule: str, entry, detail: str) -> dict:
    """The whole rule, or one of its reads, could not run; `object` is None."""
    return {"rule": rule, "entry": entry, "grade": GRADE_UNKNOWN, "kind": None, "namespace": None, "name": None, "object": None, "detail": detail}


def describe(entry: dict) -> str:
    """`Deployment ns/name: detail` for a table cell; `rule: detail` for a rule-level entry."""
    if entry.get("object") is None:
        return RULE_LEVEL_FORMAT.format(rule=entry["rule"], detail=entry["detail"])
    return DESCRIBE_FORMAT.format(kind=entry["kind"], object=entry["object"], detail=entry["detail"])


def read_items(read) -> list:
    """Every object the member's reads returned; empty when the PDB read failed."""
    return ((read or {}).get("items")) or []


def read_error(read, kind: str) -> str | None:
    """Why `kind` was not read, or None when it was: the credentials or PDB read's failure when
    `items` is None (the reads after the webhook read were skipped), else the error of the read
    after it that carried the kind."""
    if not read or read.get("items") is None:
        return (read or {}).get("error") or LATER_READS_SKIPPED_REASON
    return (read.get("read_errors") or {}).get(kind)


def unread_kinds(read, kinds) -> dict:
    """{kind: error} for the kinds among `kinds` that were not read."""
    return {kind: error for kind in kinds if (error := read_error(read, kind))}


def kind_list(names: list) -> str:
    """`Deployments, StatefulSets and DaemonSets`."""
    if len(names) <= 1:
        return "".join(names)
    return LIST_SEPARATOR.join(names[:-1]) + KIND_LIST_LAST_SEPARATOR + names[-1]


def workloads_unread_detail(read, kinds) -> str | None:
    """For a rule that grades the PDB read's Deployments and StatefulSets and the namespaced
    read's `kinds`: None when every one was read, else which kinds were not read, with the
    first such read's error, and which were graded (`DaemonSets and CronJobs not read (...);
    Deployments and StatefulSets graded`), or that nothing was, when `items` is None and the
    PDB read's workloads are unread too."""
    unread = unread_kinds(read, kinds)
    if not unread:
        return None
    pdb_unread = not read or read.get("items") is None
    unread_names = [READ_KIND_NAMES.get(kind, kind) for kind in (PDB_WORKLOAD_KINDS if pdb_unread else ()) + tuple(unread)]
    graded_names = [READ_KIND_NAMES.get(kind, kind) for kind in (() if pdb_unread else PDB_WORKLOAD_KINDS) + tuple(kind for kind in kinds if kind not in unread)]
    graded = WORKLOADS_GRADED_FORMAT.format(kinds=kind_list(graded_names)) if graded_names else WORKLOADS_NOTHING_GRADED
    return WORKLOADS_UNREAD_FORMAT.format(unread=kind_list(unread_names), reason=next(iter(unread.values())), graded=graded)


def items_of_kind(items, kind: str) -> list[dict]:
    return [item for item in items or [] if isinstance(item, dict) and item.get("kind") == kind]


# ------------------------------------------------------------------------- quantities


def parse_quantity(text) -> float | None:
    """`"250m"` -> 0.25, `"256Mi"` -> 268435456.0, `2` -> 2.0; None when it does not parse or
    is wider than a float (`"1e400"`, `"1e300E"`), which `float()` turns into `inf` rather than raising."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        number, scale = text, 1.0
    elif isinstance(text, str):
        m = QUANTITY_RE.match(text.strip())
        if not m:
            return None
        number, suffix = m.groups()
        if suffix == "":
            scale = 1.0
        elif suffix in BINARY_SUFFIXES:
            scale = BINARY_SUFFIXES[suffix]
        elif suffix in DECIMAL_SUFFIXES:
            scale = DECIMAL_SUFFIXES[suffix]
        else:
            return None
    else:
        return None
    try:
        value = float(number) * scale
    except OverflowError:  # an int wider than a float, as `json.loads` returns for a 400-digit number
        return None
    return value if math.isfinite(value) else None


def cpu_millis(text) -> int | None:
    """Millicores, or None when the quantity does not parse or, a thousand times over, does
    not fit a float (`1e306` parses; its millicores do not)."""
    value = parse_quantity(text)
    if value is None:
        return None
    millis = value * MILLIS_PER_CORE
    return round(millis) if math.isfinite(millis) else None


def memory_bytes(text) -> int | None:
    value = parse_quantity(text)
    return None if value is None else round(value)


def format_cpu(millis: int) -> str:
    return CPU_FORMAT.format(millis=millis)


def format_memory(nbytes: int) -> str:
    return MEMORY_FORMAT.format(mebibytes=round(nbytes / BYTES_PER_MI))


# ------------------------------------------------------------------- pods and owners


def pod_owner(pod: dict) -> dict:
    """{kind, namespace, name} of what a finding should name for a pod."""
    meta = pod.get("metadata") or {}
    namespace = meta.get("namespace", "")
    refs = [ref for ref in meta.get("ownerReferences") or [] if isinstance(ref, dict)]
    if not refs:
        return {"kind": KIND_POD, "namespace": namespace, "name": meta.get("name", "")}
    ref = refs[0]
    kind, name = ref.get("kind", ""), ref.get("name", "")
    if kind == KIND_REPLICASET:
        template_hash = (meta.get("labels") or {}).get(POD_TEMPLATE_HASH_LABEL)
        suffix = NAME_SEPARATOR + str(template_hash) if template_hash else ""
        if suffix and name.endswith(suffix):
            return {"kind": KIND_DEPLOYMENT, "namespace": namespace, "name": name[: -len(suffix)]}
    return {"kind": kind, "namespace": namespace, "name": name}


def owner_label(owner: dict) -> str:
    return OWNER_FORMAT.format(kind=owner.get("kind"), namespace=owner.get("namespace", ""), name=owner.get("name", ""))


def active_pods(items) -> list[dict]:
    """Pods that hold node capacity: not Succeeded or Failed."""
    return [pod for pod in items_of_kind(items, KIND_POD) if ((pod.get("status") or {}).get("phase")) not in TERMINAL_PHASES]


def is_mirror_pod(pod: dict) -> bool:
    return MIRROR_POD_ANNOTATION in ((pod.get("metadata") or {}).get("annotations") or {})


def is_daemonset_pod(pod: dict) -> bool:
    return pod_owner(pod)["kind"] == KIND_DAEMONSET


def _container_requests(container: dict) -> tuple[int | None, int | None]:
    requests = ((container.get("resources") or {}).get("requests")) or {}
    return _quantities(requests)


def _quantities(requests: dict) -> tuple[int | None, int | None]:
    return cpu_millis(requests.get(CPU_KEY, 0)), memory_bytes(requests.get(MEMORY_KEY, 0))


def _slots_add(a: tuple, b: tuple) -> tuple:
    return tuple(None if x is None or y is None else x + y for x, y in zip(a, b))


def _slots_max(a: tuple, b: tuple) -> tuple:
    return tuple(None if x is None or y is None else max(x, y) for x, y in zip(a, b))


def _extended(requests: dict) -> dict | None:
    """{resource: quantity} for the extended resources among `requests`; None when one does not parse."""
    out = {}
    for key, value in (requests or {}).items():
        if EXTENDED_RESOURCE_SEPARATOR not in str(key):
            continue
        quantity = parse_quantity(value)
        if quantity is None:
            return None
        out[str(key)] = quantity
    return out


def _extended_add(a: dict, b: dict) -> dict:
    return {key: a.get(key, 0) + b.get(key, 0) for key in set(a) | set(b)}


def _extended_max(a: dict, b: dict) -> dict:
    return {key: max(a.get(key, 0), b.get(key, 0)) for key in set(a) | set(b)}


def pod_extended_requests(spec: dict) -> dict | None:
    """{resource: quantity} of the extended resources a pod spec asks for, counted as
    `pod_requests` counts CPU and memory (containers plus native sidecars, or an init
    container's peak, plus the overhead); None when a quantity does not parse."""
    app: dict = {}
    for container in spec.get("containers") or []:
        requests = _extended(((container.get("resources") or {}).get("requests")) or {})
        if requests is None:
            return None
        app = _extended_add(app, requests)
    sidecars: dict = {}
    init_peak: dict = {}
    for container in spec.get("initContainers") or []:
        requests = _extended(((container.get("resources") or {}).get("requests")) or {})
        if requests is None:
            return None
        if container.get(RESTART_POLICY_FIELD) == RESTART_POLICY_ALWAYS:
            sidecars = _extended_add(sidecars, requests)
        else:
            init_peak = _extended_max(init_peak, _extended_add(requests, sidecars))
    overhead = _extended(spec.get(OVERHEAD_FIELD) or {})
    if overhead is None:
        return None
    return _extended_add(_extended_max(_extended_add(app, sidecars), init_peak), overhead)


def pod_requests(spec: dict) -> tuple[int | None, int | None]:
    """(cpu millicores, memory bytes) a pod spec asks for, as the scheduler counts it: the
    containers' sum plus the restartable init containers (native sidecars, `restartPolicy:
    Always`, which keep running), or, where larger, an init container's request plus the
    sidecars started before it (init containers run one at a time, in order), plus the pod's
    `overhead`. None in a slot whose quantities do not all parse."""
    app = (0, 0)
    for container in spec.get("containers") or []:
        app = _slots_add(app, _container_requests(container))
    sidecars, init_peak = (0, 0), (0, 0)
    for container in spec.get("initContainers") or []:
        requests = _container_requests(container)
        if container.get(RESTART_POLICY_FIELD) == RESTART_POLICY_ALWAYS:
            sidecars = _slots_add(sidecars, requests)
        else:
            init_peak = _slots_max(init_peak, _slots_add(requests, sidecars))
    overhead = _quantities(spec.get(OVERHEAD_FIELD) or {})
    return _slots_add(_slots_max(_slots_add(app, sidecars), init_peak), overhead)


def containers(spec: dict) -> list[dict]:
    return [c for c in (spec.get("containers") or []) + (spec.get("initContainers") or []) if isinstance(c, dict)]


def hostpath_volumes(spec: dict) -> list[str]:
    """The host paths a pod spec mounts through `hostPath` volumes."""
    paths = []
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        host_path = volume.get("hostPath")
        if isinstance(host_path, dict) and host_path.get("path"):
            paths.append(str(host_path["path"]))
    return paths


def claim_names(spec: dict) -> list[str]:
    """The PersistentVolumeClaims a pod spec mounts by name."""
    names = []
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        claim = volume.get("persistentVolumeClaim")
        if isinstance(claim, dict) and claim.get("claimName"):
            names.append(str(claim["claimName"]))
    return names


# -------------------------------------------------------------------------- templates


def is_system_namespace(namespace: str) -> bool:
    """A namespace GKE or one of its add-ons occupies."""
    return namespace in tables.SYSTEM_NAMESPACES or namespace.startswith(tables.SYSTEM_NAMESPACE_PREFIXES)


def pod_template_spec(workload) -> dict | None:
    """The PodSpec a workload's template carries, or None for a kind without a template."""
    if not isinstance(workload, dict):
        return None
    path = TEMPLATE_PATHS.get(workload.get("kind"))
    if path is None:
        return None
    node = workload
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    spec = (node or {}).get("spec") if isinstance(node, dict) else None
    return spec if isinstance(spec, dict) else None


def templates(items, kinds: tuple = tables.TEMPLATE_KINDS, include_system: bool = False) -> list[tuple[dict, dict, dict]]:
    """(obj, pod_spec, workload) for every template of the kinds asked; the namespaces GKE
    and its add-ons occupy are skipped unless `include_system`."""
    out = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in kinds:
            continue
        meta = item.get("metadata") or {}
        namespace = meta.get("namespace", "") or ""
        if not include_system and is_system_namespace(namespace):
            continue
        spec = pod_template_spec(item)
        if not isinstance(spec, dict):
            continue
        name = meta.get("name", "") or ""
        obj = {"kind": item["kind"], "namespace": namespace, "name": name, "object": OBJECT_FORMAT.format(namespace=namespace, name=name)}
        out.append((obj, spec, item))
    return out


def template_specs(items, kinds: tuple = tables.TEMPLATE_KINDS) -> list[tuple[dict, dict]]:
    """(owner, pod spec) for every template read, system namespaces included: the view a
    rule takes when GKE's own agents count too (a socket client in kube-system)."""
    return [(obj, spec) for obj, spec, _ in templates(items, kinds, include_system=True)]


def selectors(spec: dict) -> list[dict]:
    """Every node label a template requires: `key`, `operator`, `values` (a list, empty for
    `Exists` and `DoesNotExist`) and `where` it was found. A `nodeSelector` entry is `In` one
    value; preferred affinity terms and `matchFields` are not read."""
    out = []
    for key, value in (spec.get("nodeSelector") or {}).items():
        out.append({"key": key, "operator": OP_IN, "values": [value], "where": WHERE_NODE_SELECTOR})
    node_affinity = (spec.get("affinity") or {}).get("nodeAffinity") or {}
    for term in (node_affinity.get(AFFINITY_REQUIRED) or {}).get("nodeSelectorTerms") or []:
        if not isinstance(term, dict):
            continue
        for expr in term.get("matchExpressions") or []:
            if isinstance(expr, dict) and expr.get("key") and expr.get("operator"):
                out.append({"key": expr["key"], "operator": expr["operator"], "values": [v for v in expr.get("values") or [] if v is not None], "where": WHERE_NODE_AFFINITY})
    return out


def selected_values(spec: dict, key: str) -> list[str]:
    """The values a template requires for one label through `In` terms and its nodeSelector."""
    values = []
    for selector in selectors(spec):
        if selector["key"] == key and selector["operator"] == OP_IN:
            values.extend(selector["values"])
    return values


# ------------------------------------------------------------------------------ nodes


def node_allocatable(node: dict) -> tuple[int | None, int | None]:
    allocatable = (node.get("status") or {}).get("allocatable") or {}
    return cpu_millis(allocatable.get("cpu")), memory_bytes(allocatable.get("memory"))


def node_extended_allocatable(node: dict) -> dict | None:
    """{resource: quantity} of the extended resources a node offers; None when one does not parse."""
    return _extended((node.get("status") or {}).get("allocatable") or {})


def node_pool(node: dict) -> str:
    return ((node.get("metadata") or {}).get("labels") or {}).get(tables.NODEPOOL_LABEL, "")


def node_schedulable(node: dict) -> bool:
    """Ready and not cordoned: a node a displaced pod could land on."""
    if (node.get("spec") or {}).get("unschedulable"):
        return False
    for condition in (node.get("status") or {}).get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == NODE_READY_CONDITION:
            return condition.get("status") == CONDITION_TRUE
    return False


# ------------------------------------------------------------------------------ pools


def pools_for_template(spec: dict, pools: list[dict]) -> list[dict]:
    """The pools a template can land on: the ones its gke-nodepool selector names, else all."""
    named = selected_values(spec, tables.NODEPOOL_LABEL)
    if named:
        return [p for p in pools if p.get("name") in named]
    return list(pools)


def pool_config(pool: dict) -> dict:
    config = pool.get("config")
    return config if isinstance(config, dict) else {}


def pool_scales_from_zero(pool: dict) -> bool:
    """An autoscaled pool whose minimum is zero nodes: empty today is not stranded."""
    autoscaling = pool.get("autoscaling")
    if not isinstance(autoscaling, dict) or not autoscaling.get("enabled"):
        return False
    return not (autoscaling.get("minNodeCount") or 0) and not (autoscaling.get("totalMinNodeCount") or 0)


def minor_text(version: tuple | None) -> str | None:
    return tables.format_minor(version) if version else None


def pool_below_target(pool: dict, target: tuple | None) -> bool:
    parsed = pool.get("parsed")
    return target is not None and parsed is not None and parsed < target


def pools_below_target(pools: list[dict], target: tuple | None) -> list[str]:
    return [p.get("name", "") for p in pools if pool_below_target(p, target)]


def cgroup_transition(pool: dict, target: tuple | None) -> tuple[str, str]:
    """Whether the upgrade to `target` moves `pool` to cgroup v2.

    `effectiveCgroupMode` is what the pool runs today; a `linuxNodeConfig.cgroupMode` pin
    to v1 holds until GKE removes v1 (1.35), an unpinned v1 pool is migrated from 1.33.
    """
    config = pool_config(pool)
    mode = config.get("effectiveCgroupMode")
    pin = (config.get("linuxNodeConfig") or {}).get("cgroupMode")
    if mode == tables.CGROUP_V2_MODE or pin == tables.CGROUP_MODE_V2_PIN:
        return CGROUP_ALREADY_V2, CGROUP_REASON_ALREADY
    if mode == tables.CGROUP_V1_MODE or pin == tables.CGROUP_MODE_V1_PIN:
        if target is None:
            return CGROUP_UNKNOWN, CGROUP_REASON_NO_TARGET
        pinned = pin == tables.CGROUP_MODE_V1_PIN
        threshold = tables.CGROUP_V1_REMOVAL_MINOR if pinned else tables.CGROUP_V2_MIGRATION_MINOR
        if target[1] >= threshold:
            reason = CGROUP_REASON_PIN_ENDS if pinned else CGROUP_REASON_MIGRATION
            return CGROUP_MOVES, reason.format(minor=threshold)
        return CGROUP_STAYS_V1, CGROUP_REASON_STAYS.format(minor=threshold)
    return CGROUP_UNKNOWN, CGROUP_REASON_UNREAD


# ----------------------------------------------------------------------------- images


def image_repository_and_tag(image: str) -> tuple[str, str]:
    """`docker.io/library/eclipse-temurin:8u302-b08-jre` -> (`eclipse-temurin`, `8u302-b08-jre`):
    the repository is the last path component, the tag what follows the colon."""
    name = image.partition(IMAGE_DIGEST_SEPARATOR)[0]
    path, sep, tag = name.rpartition(IMAGE_TAG_SEPARATOR)
    if not sep or PATH_SEPARATOR in tag:
        path, tag = name, ""
    return path.rsplit(PATH_SEPARATOR, 1)[-1], tag


# ------------------------------------------------------------------------ shell scripts


def shell_scripts(command: list) -> list[str]:
    """The scripts a command list hands a shell: after every shell program in it (SHELL_PROGRAMS,
    by last path component) the option words are walked, before and after the flag group
    carrying `c` alike (`-e`, `-o pipefail`, `--norc`, the fused `-ec`, each argument-taking
    option with its argument), and the first word after them is the script when a group
    carried `c` (SHELL_COMMAND_FLAG_RE). `["tini", "--", "bash", "-o", "pipefail", "-c",
    "nginx & php-fpm"]` and `["bash", "-c", "-o", "pipefail", "nginx & php-fpm"]` ->
    `["nginx & php-fpm"]`; a shell with no `c` flag hands back none."""
    words = [str(w) for w in command or []]
    scripts = []
    for index, word in enumerate(words):
        if word.rsplit(PATH_SEPARATOR, 1)[-1] not in tables.SHELL_PROGRAMS:
            continue
        j, carried = index + 1, False
        while j < len(words) and words[j].startswith(SHELL_OPTION_PREFIXES):
            option = words[j]
            j += 1
            if tables.SHELL_COMMAND_FLAG_RE.match(option):
                carried = True
            if option in SHELL_OPTIONS_WITH_ARGUMENT or SHELL_OPTION_ARGUMENT_GROUP_RE.match(option):
                j += 1
        if carried and j < len(words):
            scripts.append(words[j])
    return scripts


def shell_words(script) -> list[str]:
    """The programs and arguments a `-c` script names, by last path component: the words between
    whitespace, the shell's operator characters, its quotes and its backticks, so `'/sbin/modprobe' x`,
    `exec "$BIN/modprobe" x`, a nested `bash -c 'modprobe x'` and ``MOD=`which modprobe` `` all
    name `modprobe`.
    `"/sbin/modprobe br_netfilter && exec /agent"` -> `["modprobe", "br_netfilter", "exec",
    "agent"]`. Quoting is read no further: a program named inside a quoted string counts as
    named."""
    if not isinstance(script, str):
        return []
    return [word.rsplit(PATH_SEPARATOR, 1)[-1] for word in SHELL_WORD_SPLIT_RE.split(script) if word]


def shell_forks(script) -> bool | None:
    """Whether a `sh -c` script puts a command in the background: a lone `&` among its
    operators once the quoted spans, escaped characters, `#` comments, heredoc bodies and
    the expansions that are words to the shell are gone, so `&&`, `|&`, `>&`, `<&`, `&>`,
    `;&`, a subshell's `)&` and `2>&1` read as what they are and `'&'`, `$((mode & 4))`,
    `((x & 1))`, `$((1<<2))`, bash's older `$[mode & 4]` and `${URL:-a&b}` are words. A command list the script embeds
    is read with the same rules wherever it stands: `$(…)` and backticks in a word, in
    double quotes, in `${…}`, in `$((…))` and `$[…]` and in an unquoted heredoc body, so
    `"$(sleep 9 &)"`, `${SIDECAR:-$(sleep 9 &)}` and a heredoc line `$(start-agent &)` fork;
    a quoted heredoc body is text, and a delimiter is read with its quotes removed across
    the whole word (`E"O"F` is `EOF`, quoted). A process substitution (`>(…)`, `<(…)`) and
    `coproc` run their command list asynchronously and are forks.

    The scanner reads the script as bash does, which is also how zsh and a busybox ash with
    the bash extensions read it: `((…))` closed by `))` is arithmetic and otherwise two
    subshells (so `((sleep 10; kill 1) &)` forks), `&>` is a redirection, `$'…'` is a quoted
    string, and `case`, `esac` and `coproc` are words only at command position (`grep case
    file` names a file). `sh` on a Debian image is dash, which reads `((x & 1))` as two
    subshells with a fork inside and `cmd &>f` as `cmd &` then `>f`, two forks the scanner
    does not report; a rule that grades an `sh -c` script with those two spellings grades it
    as bash would. A `case … esac` is read as one wherever it stands, inside a substitution,
    a subshell or a `function NAME { … }` / `NAME() { … }` body (a `{` that stands as a word
    opens a command list wherever it stands): a pattern's `)` closes none of them.

    None when the script cannot be read this way: an unterminated quote, heredoc, command
    substitution or expansion, a trailing backslash, or substitutions and expansions nested
    more than SHELL_NESTING_CAP deep, which a rule reports as unknown, never as clean."""
    if not isinstance(script, str):
        return None
    state = _ShellScan()
    if _shell_scan(script, 0, state, None) is None or state.pending:
        return None
    return any(SHELL_FORK_OPERATOR in _shell_operators(run) for run in SHELL_OPERATOR_RUN_RE.findall("".join(state.out)))


class _ShellScan:
    """What one `shell_forks` call carries through the scan: the cleaned text (`out`), the
    heredoc delimiters whose bodies start at the next newline (`pending`: word, strip tabs,
    quoted), where each arithmetic span was found to end (`arithmetic_ends`, so a nested
    span answers that question once, not once per level above it), and how many
    substitutions and expansions are open (`nesting`, capped at SHELL_NESTING_CAP)."""

    __slots__ = ("out", "pending", "arithmetic_ends", "nesting")

    def __init__(self, out=None, pending=None, arithmetic_ends=None, nesting: int = 0):
        self.out: list[str] = [] if out is None else out
        self.pending: list[tuple[str, bool, bool]] = [] if pending is None else pending
        self.arithmetic_ends: dict[int, int | None] = {} if arithmetic_ends is None else arithmetic_ends
        self.nesting = nesting

    def scratch(self) -> "_ShellScan":
        """A state that shares the memo and the depth and discards what it emits."""
        return _ShellScan([], [], self.arithmetic_ends, self.nesting)


def _shell_operators(run: str) -> list[str]:
    """A run of operator characters as the shell's operators, longest match first:
    `")&"` -> `[")", "&"]`, `">&"` -> `[">&"]`, `"|&"` -> `["|&"]`."""
    out = []
    while run:
        operator = next(candidate for candidate in SHELL_OPERATORS if run.startswith(candidate))
        out.append(operator)
        run = run[len(operator) :]
    return out


def _shell_scan(script: str, i: int, state: _ShellScan, closer: str | None) -> int | None:
    """Scans the command list at `i` to the end of `script`, or to the `closer` that ends a
    command substitution (`)` at its own depth, or a backtick), appending to `state.out`
    every character outside a quoted span or an expansion, those as SHELL_NEUTRAL, with
    comments and heredoc bodies left out, and to `state.pending` each heredoc delimiter
    whose body starts at the next newline. Returns the index after the closer (the end, at
    the top level); None when a quote, expansion or heredoc is unterminated, a backslash
    ends the script, or the nesting cap is passed."""
    n = len(script)
    out = state.out
    depth = 0  # subshell parentheses open in this command list
    case_bases: list[int] = []  # the depth each open `case` sits at: a `)` at that depth is a pattern's and closes nothing
    command = True  # at command position, where a reserved word is a word
    while i < n:
        c = script[i]
        if closer == SHELL_BACKTICK and c == SHELL_BACKTICK:
            return i + 1
        if c == SHELL_ESCAPE:
            if i + 1 >= n:
                return None
            if script[i + 1] != SHELL_NEWLINE:  # a line continuation is nothing; an escaped character is a word character
                out.append(SHELL_NEUTRAL)
                command = False
            i += SHELL_ESCAPED_PAIR
        elif script.startswith(SHELL_ANSI_QUOTE, i):
            i = _shell_ansi_quoted(script, i + len(SHELL_ANSI_QUOTE))
            if i is None:
                return None
            out.append(SHELL_NEUTRAL)
            command = False
        elif c == SHELL_SINGLE_QUOTE:
            end = script.find(SHELL_SINGLE_QUOTE, i + 1)
            if end < 0:
                return None
            out.append(SHELL_NEUTRAL)
            i = end + 1
            command = False
        elif c == SHELL_DOUBLE_QUOTE:
            i = _shell_double_quoted(script, i + 1, state)
            if i is None:
                return None
            command = False
        elif c == SHELL_COMMENT and (not out or out[-1] in SHELL_WORD_BREAKS):
            end = script.find(SHELL_NEWLINE, i)
            i = n if end < 0 else end
        elif script.startswith(SHELL_EXPANSION_OPENERS, i):
            i = _shell_expansion(script, i, state)
            if i is None:
                return None
            command = False
        elif script.startswith(SHELL_ARITHMETIC_COMMAND, i):
            start = i + len(SHELL_ARITHMETIC_COMMAND)
            arithmetic = _shell_closes_arithmetic(script, start, state)
            if arithmetic is None:
                return None
            if arithmetic:
                i = _shell_arithmetic_span(script, start, state)
                if i is None:
                    return None
                command = False
            else:  # `((` closed by a lone `)`: a subshell in a subshell, as bash reads it; the second `(` is read next
                depth += 1
                out.append(SHELL_OPEN_PAREN)
                i += 1
                command = True
        elif script.startswith(SHELL_COMMAND_SUBSTITUTION, i) or c == SHELL_BACKTICK:
            i = _shell_substitution(script, i, state)
            if i is None:
                return None
            command = False
        elif script.startswith(SHELL_PROCESS_SUBSTITUTIONS, i):  # run asynchronously: a fork, then its command list
            out.extend(SHELL_FORK_MARK)
            i = _shell_nested_list(script, i + len(SHELL_PROCESS_SUBSTITUTIONS[0]), SHELL_CLOSE_PAREN, state)
            if i is None:
                return None
            command = False
        elif script.startswith(SHELL_HERESTRING_OPERATOR, i):  # a here-string's word is an ordinary word
            out.extend(SHELL_HERESTRING_OPERATOR)
            i += len(SHELL_HERESTRING_OPERATOR)
            command = False
        elif script.startswith(SHELL_HEREDOC_OPERATOR, i):
            out.extend(SHELL_HEREDOC_OPERATOR)
            i += len(SHELL_HEREDOC_OPERATOR)
            strip_tabs = script.startswith(SHELL_HEREDOC_STRIP_TABS, i)
            if strip_tabs:
                i += 1
            while i < n and script[i] in SHELL_BLANKS:
                i += 1
            word, i, quoted = _shell_heredoc_word(script, i)
            if word is None:
                return None
            out.append(SHELL_NEUTRAL)
            state.pending.append((word, strip_tabs, quoted))
            command = False
        elif c == SHELL_NEWLINE:
            out.append(c)
            i += 1
            while state.pending:
                word, strip_tabs, quoted = state.pending.pop(0)
                body = _shell_heredoc_end(script, i, word, strip_tabs)
                if body is None:
                    return None
                body_end, resume = body
                if not quoted and _shell_heredoc_body(script[i:body_end], state) is None:
                    return None
                i = resume
            command = True
        elif command and _shell_word_at(script, i, SHELL_CASE_WORD, out):
            case_bases.append(depth)
            out.extend(SHELL_CASE_WORD)
            i += len(SHELL_CASE_WORD)
            command = False
        elif command and _shell_word_at(script, i, SHELL_ESAC_WORD, out):
            if case_bases:
                case_bases.pop()
            out.extend(SHELL_ESAC_WORD)
            i += len(SHELL_ESAC_WORD)
            command = False
        elif _shell_word_at(script, i, SHELL_GROUP_OPEN, out):  # a group opens: a command follows, at any position
            out.extend(SHELL_GROUP_OPEN)
            i += len(SHELL_GROUP_OPEN)
            command = True
        elif command and _shell_word_at(script, i, SHELL_COPROC_WORD, out):  # runs its command asynchronously: a fork
            out.extend(SHELL_FORK_MARK)
            out.extend(SHELL_COPROC_WORD)
            i += len(SHELL_COPROC_WORD)
        elif command and (prefix := _shell_prefix_word_at(script, i, out)) is not None:  # a command follows the word
            out.extend(prefix)
            i += len(prefix)
        elif c == SHELL_OPEN_PAREN:
            depth += 1
            out.append(c)
            i += 1
            command = True
        elif c == SHELL_CLOSE_PAREN:
            if case_bases and depth == case_bases[-1]:  # a pattern's `)`, at the depth its `case` sits at: closes nothing
                pass
            elif closer == SHELL_CLOSE_PAREN and depth == 0:
                return i + 1
            elif depth:
                depth -= 1
            out.append(c)
            i += 1
            command = True
        elif c in SHELL_COMMAND_SEPARATORS:
            out.append(c)
            i += 1
            command = True
        elif c in SHELL_REDIRECTION_CHARS:  # a file name follows, not a command
            out.append(c)
            i += 1
            command = False
        elif c in SHELL_BLANKS:
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
            command = False
    return None if closer is not None else n


def _shell_word_at(script: str, i: int, word: str, out: list[str]) -> bool:
    """Whether `word` stands on its own at `i`: a word break (or the start) before it in
    what was scanned, and one (or the end) after it."""
    end = i + len(word)
    return script.startswith(word, i) and (not out or out[-1] in SHELL_WORD_BREAKS) and (end >= len(script) or script[end] in SHELL_WORD_BREAKS)


def _shell_prefix_word_at(script: str, i: int, out: list[str]) -> str | None:
    """The reserved word at `i` after which a command follows, or None."""
    for word in SHELL_COMMAND_PREFIX_WORDS:
        if _shell_word_at(script, i, word, out):
            return word
    return None


def _shell_ansi_quoted(script: str, i: int) -> int | None:
    """The index after the `'` that closes the `$'…'` span open before `i`, where a
    backslash escapes the next character; None when it never closes."""
    n = len(script)
    while i < n:
        if script[i] == SHELL_ESCAPE:
            i += SHELL_ESCAPED_PAIR
        elif script[i] == SHELL_SINGLE_QUOTE:
            return i + 1
        else:
            i += 1
    return None


def _shell_double_quoted(script: str, i: int, state: _ShellScan) -> int | None:
    """The index after the double quote that closes the span open before `i`, the span one
    neutral character in `state.out` except the command lists in it, which are scanned;
    None when the quote never closes."""
    n = len(script)
    while i < n:
        c = script[i]
        if c == SHELL_DOUBLE_QUOTE:
            state.out.append(SHELL_NEUTRAL)
            return i + 1
        if c == SHELL_ESCAPE:
            i += SHELL_ESCAPED_PAIR
        elif script.startswith(SHELL_EXPANSION_OPENERS, i):
            i = _shell_expansion(script, i, state)
            if i is None:
                return None
        elif script.startswith(SHELL_COMMAND_SUBSTITUTION, i) or c == SHELL_BACKTICK:
            i = _shell_substitution(script, i, state)
            if i is None:
                return None
        else:
            i += 1
    return None


def _shell_closes_arithmetic(script: str, start: int, state: _ShellScan) -> bool | None:
    """Whether the `((` whose contents start at `start` is closed by `))`, which makes it
    arithmetic, rather than by a lone `)`, which makes it two parentheses; None when it
    never closes. Where the span ends is memoised in the state, so a span nested in others
    answers once however many levels ask."""
    if start not in state.arithmetic_ends:
        state.arithmetic_ends[start] = _shell_balanced(script, start, SHELL_OPEN_PAREN, SHELL_CLOSE_PAREN, SHELL_ARITHMETIC_DEPTH, state.scratch())
    end = state.arithmetic_ends[start]
    if end is None:
        return None
    return script[end - len(SHELL_ARITHMETIC_CLOSE) : end] == SHELL_ARITHMETIC_CLOSE


def _shell_arithmetic_span(script: str, start: int, state: _ShellScan) -> int | None:
    """The index after the arithmetic span whose contents start at `start`, the span one
    neutral character in `state.out` with the command lists inside it scanned."""
    end = _shell_balanced(script, start, SHELL_OPEN_PAREN, SHELL_CLOSE_PAREN, SHELL_ARITHMETIC_DEPTH, state)
    if end is None:
        return None
    state.out.append(SHELL_NEUTRAL)
    return end


def _shell_expansion(script: str, i: int, state: _ShellScan) -> int | None:
    """The index after the parameter expansion `${…}`, the arithmetic expansion `$((…))` or
    bash's older arithmetic `$[…]` at `i`, one neutral character in `state.out` with the
    command lists inside it scanned; `$((` not closed by `))` is `$(` opening a subshell,
    as bash reads it. None when it never closes."""
    if script.startswith(SHELL_PARAMETER_EXPANSION, i):
        end = _shell_balanced(script, i + len(SHELL_PARAMETER_EXPANSION), SHELL_OPEN_BRACE, SHELL_CLOSE_BRACE, 1, state)
        if end is None:
            return None
        state.out.append(SHELL_NEUTRAL)
        return end
    if script.startswith(SHELL_BRACKET_ARITHMETIC, i):
        end = _shell_balanced(script, i + len(SHELL_BRACKET_ARITHMETIC), SHELL_OPEN_BRACKET, SHELL_CLOSE_BRACKET, 1, state)
        if end is None:
            return None
        state.out.append(SHELL_NEUTRAL)
        return end
    start = i + len(SHELL_ARITHMETIC_EXPANSION)
    arithmetic = _shell_closes_arithmetic(script, start, state)
    if arithmetic is None:
        return None
    if not arithmetic:
        return _shell_substitution(script, i, state)
    return _shell_arithmetic_span(script, start, state)


def _shell_balanced(script: str, i: int, opener: str, closer: str, depth: int, state: _ShellScan) -> int | None:
    """The index after the `closer` that brings the span open at `i` (`depth` already open)
    to zero, skipping its text as a word while scanning the command lists in it (`$(…)`,
    backticks, those inside double quotes or nested expansions) into `state.out`; None
    when it never closes or the nesting cap is passed."""
    state.nesting += 1
    try:
        if state.nesting > SHELL_NESTING_CAP:
            return None
        n = len(script)
        while i < n:
            c = script[i]
            if c == SHELL_ESCAPE:
                i += SHELL_ESCAPED_PAIR
            elif script.startswith(SHELL_ANSI_QUOTE, i):
                i = _shell_ansi_quoted(script, i + len(SHELL_ANSI_QUOTE))
                if i is None:
                    return None
            elif c == SHELL_SINGLE_QUOTE:
                end = script.find(SHELL_SINGLE_QUOTE, i + 1)
                if end < 0:
                    return None
                i = end + 1
            elif c == SHELL_DOUBLE_QUOTE:
                i = _shell_double_quoted(script, i + 1, state)
                if i is None:
                    return None
            elif script.startswith(SHELL_EXPANSION_OPENERS, i):
                i = _shell_expansion(script, i, state)
                if i is None:
                    return None
            elif script.startswith(SHELL_COMMAND_SUBSTITUTION, i) or c == SHELL_BACKTICK:
                i = _shell_substitution(script, i, state)
                if i is None:
                    return None
            elif c == opener:
                depth += 1
                i += 1
            elif c == closer:
                depth -= 1
                i += 1
                if depth == 0:
                    return i
            else:
                i += 1
        return None
    finally:
        state.nesting -= 1


def _shell_substitution(script: str, i: int, state: _ShellScan) -> int | None:
    """The index after the command substitution at `i` (`$(…)` or backticks), its command
    list scanned; None when it never closes."""
    if script.startswith(SHELL_COMMAND_SUBSTITUTION, i):
        return _shell_nested_list(script, i + len(SHELL_COMMAND_SUBSTITUTION), SHELL_CLOSE_PAREN, state)
    return _shell_nested_list(script, i + len(SHELL_BACKTICK), SHELL_BACKTICK, state)


def _shell_nested_list(script: str, start: int, closer: str, state: _ShellScan) -> int | None:
    """The index after the embedded command list starting at `start` and ending at
    `closer`, scanned into `state.out` between an opening parenthesis (so a comment may
    start it) and a neutral character (so its last operator does not run into the next);
    None when it never closes or the nesting cap is passed."""
    state.nesting += 1
    try:
        if state.nesting > SHELL_NESTING_CAP:
            return None
        state.out.append(SHELL_OPEN_PAREN)
        end = _shell_scan(script, start, state, closer)
        if end is None:
            return None
        state.out.append(SHELL_NEUTRAL)
        return end
    finally:
        state.nesting -= 1


def _shell_heredoc_word(script: str, i: int) -> tuple[str | None, int, bool]:
    """The heredoc delimiter at `i` as the shell reads it, its quotes removed across the
    whole word (`E"O"F` is `EOF`), where it ends, and whether any part was quoted (quotes
    or a backslash), which makes the body text rather than a subject of expansion; the word
    is None for an empty or unterminated one."""
    n = len(script)
    parts: list[str] = []
    quoted = False
    while i < n and script[i] not in SHELL_WORD_BREAKS:
        c = script[i]
        if c in SHELL_QUOTES:
            end = script.find(c, i + 1)
            if end < 0 or SHELL_NEWLINE in script[i + 1 : end]:
                return None, i, True
            parts.append(script[i + 1 : end])
            quoted = True
            i = end + 1
        elif c == SHELL_ESCAPE:
            if i + 1 >= n:
                return None, i, True
            parts.append(script[i + 1])
            quoted = True
            i += SHELL_ESCAPED_PAIR
        else:
            parts.append(c)
            i += 1
    return "".join(parts) or None, i, quoted


def _shell_heredoc_end(script: str, i: int, word: str, strip_tabs: bool) -> tuple[int, int] | None:
    """Where the heredoc body starting at `i` ends (the start of the line that closes it)
    and the index after that line; None when no line closes it."""
    while True:
        end = script.find(SHELL_NEWLINE, i)
        line = script[i:] if end < 0 else script[i:end]
        if (line.lstrip(SHELL_TAB) if strip_tabs else line) == word:
            return i, (len(script) if end < 0 else end + 1)
        if end < 0:
            return None
        i = end + 1


def _shell_heredoc_body(body: str, state: _ShellScan) -> int | None:
    """An unquoted heredoc body: text, except the command lists it embeds (`$(…)`,
    backticks, those inside `${…}` and `$((…))`), which the shell runs and which are
    scanned into `state.out`; a backslash escapes the next character. None when a command
    list in it never closes."""
    inner = _ShellScan(state.out, [], {}, state.nesting)  # the body is another string: its own memo, no heredocs of its own
    j, n = 0, len(body)
    while j < n:
        c = body[j]
        if c == SHELL_ESCAPE:
            j += SHELL_ESCAPED_PAIR
        elif body.startswith(SHELL_EXPANSION_OPENERS, j):
            j = _shell_expansion(body, j, inner)
        elif body.startswith(SHELL_COMMAND_SUBSTITUTION, j) or c == SHELL_BACKTICK:
            j = _shell_substitution(body, j, inner)
        else:
            j += 1
        if j is None:
            return None
    return None if inner.pending else n

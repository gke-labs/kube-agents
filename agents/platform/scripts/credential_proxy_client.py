#!/usr/bin/env python3
"""Submit a supported CLI argv vector to the paired credential proxy."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import os
import random
import re
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Every brokered call starts this script as a new process, so its imports are
# on the caller's clock (#5578). `argparse` is imported in `parse_args`, and
# `ClusterTarget` is a plain class, not a dataclass: `dataclasses` pulls in
# `inspect`. A call through the `gcloud` or `kubectl` name uses neither.
# (`hashlib` and `tempfile` stay: `urllib.request` imports them anyway.)


# What this client may be invoked *as*, which is a different list from what the
# broker may run. `CommandExecutor.ALLOWED_EXECUTABLES` is derived from the
# forges an install configured, so a forge CLI appears there and only there:
# the broker is where that CLI runs. The overlap is not an invariant and these
# two lists are not to be folded together.
#
# `gh` and `git` are not here. The forge is reached through the version-control
# verbs, and a forwarded git ran in the broker's filesystem rather than the
# caller's, so a shim on either name answered a command about a tree the agent
# had never seen. docs/designs/version-control-support.md, "One git in the
# sandbox".
SUPPORTED_EXECUTABLES = ("kubectl", "gcloud")

# How long to wait to reach the broker. Bounds the connect only — see
# BrokerConnection.
BROKER_CONNECT_TIMEOUT_SECONDS = 10.0

# Exit code returned when the credential proxy blocks a command under its
# security policy. 77 is EX_NOPERM from sysexits.h ("permission denied").
# Returning 77 rather than the shell's 126 ("not executable") ensures the
# runtime does not attach an execution/chmod hint that misleads the agent
# into seeking local file workarounds for a policy boundary (#2179).
EXIT_SECURITY_POLICY_BLOCKED = getattr(os, "EX_NOPERM", 77)
# Exit code returned when the broker cut the command's output at its per-stream
# cap. 74 is EX_IOERR from sysexits.h ("input/output error"): the command may
# well have exited 0, but what the caller holds is a partial payload, and a
# partial `-o json` dump read as success is the false all-clear this code
# exists to prevent. Scripts that already gate on the stderr marker below see
# the same marker; the exit code is what the model's own kubectl sees.
EXIT_OUTPUT_TRUNCATED = getattr(os, "EX_IOERR", 74)
# The stderr line a cut command gets. Its prefix is what every reader matches
# (`PROXY_TRUNCATED_MARKER` in the collectors); the counts after it say how
# much of stdout was kept and the cap it was held to, when the broker said.
OUTPUT_TRUNCATED_MARKER = "credential proxy output truncated"
OUTPUT_TRUNCATED_COUNTS = " at {limit} bytes (kept {kept} bytes of stdout; exit {code})"
# The cut fell on stderr alone: stdout is whole, so the exit code is the
# command's own and only the diagnostics are short.
OUTPUT_TRUNCATED_STDERR_ONLY = " (stderr only; stdout is complete)"
# The `policy verb:` line is read by the agent runtime one line at a time, so a
# verb word (an argv token) must not carry a line break or a quote.
_VERB_WORD_SAFE_RE = re.compile(r"[^\w./:@-]")
_VERB_WORD_MAX = 64

# `\Z`, not `$`. `$` also matches immediately before a trailing newline, so
# `re.match` on "nowhere\n" succeeds -- and that value goes on to build the
# project key in a log line and a filename in the broker's state dir. `fullmatch`
# at the call site says the same thing twice on purpose: whichever a later
# reader changes, the other still holds.
_GKE_CONTEXT_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")

# Enough for any real kubeconfig; the point is that the file is read into
# memory before anything is known about it.
MAX_KUBECONFIG_BYTES = 1 << 20

# The broker's read-only Cloud API relay: `GET {broker}/v1/gcp/<host>/<path>`
# forwards a permitted Google API read on the broker's credential (a `POST`
# for the one read whose arguments are a JSON body). Declared
# here rather than in the broker because both sides spell it and this is the
# module both import; `credential_proxy` re-exports it. `ApiSession` below is
# the client; `api_policy.API_READ_ROUTES` on the broker is what it may reach.
API_RELAY_PREFIX = "/v1/gcp/"

# Google Cloud universe domain (`googleapis.com` in commercial GCP, or a
# partner/sovereign domain such as Google Cloud Dedicated). Kept in this module
# because `gke_cluster_reads.py` and the sandbox collectors import it from the
# root-owned `/opt/vcs/libexec/platform` closure.
DEFAULT_UNIVERSE_DOMAIN = "googleapis.com"
GOOGLE_CLOUD_UNIVERSE_DOMAIN_ENV = "GOOGLE_CLOUD_UNIVERSE_DOMAIN"
CLOUDSDK_UNIVERSE_DOMAIN_ENV = "CLOUDSDK_CORE_UNIVERSE_DOMAIN"
MAX_UNIVERSE_DOMAIN_LENGTH = 253
_GOOGLE_API_SUFFIX = "." + DEFAULT_UNIVERSE_DOMAIN
_UNIVERSE_DOMAIN_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)

# The exec tier's shared names, here for the same reason as the relay prefix:
# the broker and the shim both spell them, and this is the module both import.
# docs/designs/credential-broker-exec-tier.md §12 is the contract, and
# test_exec_tier_contract.py checks that these match it.
EXEC_ROLE = "exec"
EXEC_URL_ENV = "CREDENTIAL_PROXY_EXEC_URL"
EXEC_REPLICAS_FILE_ENV = "CREDENTIAL_PROXY_EXEC_REPLICAS_FILE"
EXEC_REPLICAS_FILE = "/etc/credential-proxy-exec-tier/replicas"
# The keys the admission route adds for the whole tier when the replicas file
# holds a count (§3.5); `broker_admission` sizes a sweep by them.
ADMISSION_REPLICAS_KEY = "replicas"
ADMISSION_TIER_ADMITTED_KEY = "tier_admitted"
ADMISSION_TIER_INTERACTIVE_CAP_KEY = "tier_interactive_cap"
BUSY_BOUND_KEY = "bound"
ADMISSION_BOUND_DRAINING = "draining"
ADMISSION_BOUND_BACKGROUND = "background"
# A busy answer under one of these bounds is retried, each attempt on a new
# connection, so another replica can admit it. The session cap is a caller's
# own limit, and the others name no replica-local shortage.
RETRYABLE_BUSY_BOUNDS = frozenset(
    {"slot", "budget", ADMISSION_BOUND_BACKGROUND, ADMISSION_BOUND_DRAINING, "profile"}
)

# The broker's admission refusals, which it raises before a command starts, so
# a retry never runs one twice. `BUSY_CODE` is the 503 the broker answers at a
# cap or its memory budget. `RESOURCE_PRESSURE_CODE` is the 429 the broker's
# resource watermark gate answers at once past its hard watermark, with the
# interval to wait in the Retry-After header and the body's `retry_after`.
BUSY_CODE = "CREDENTIAL_PROXY_BUSY"
# credential_proxy.BUSY_BEFORE_ADMISSION_KEY: a busy answer the broker gave
# before admitting the request, when nothing it asked for has run.
BUSY_BEFORE_ADMISSION_KEY = "before_admission"
RESOURCE_PRESSURE_CODE = "CREDENTIAL_PROXY_RESOURCE_PRESSURE"
# Envoy's mark on a 503 it answered itself, under its own overload manager,
# without forwarding the request. Any other non-JSON 503 may be an upstream
# that reset after the request was forwarded, so the command may have run.
ENVOY_OVERLOADED_HEADER = "x-envoy-overloaded"
RETRY_AFTER_HEADER = "Retry-After"
# How many times a refused request is sent again: four retries, five attempts.
CREDENTIAL_PROXY_CLIENT_MAX_RETRIES = 4
# The wait before retry `k` (from 0) is uniform in [base/2, base]: full jitter
# over half the window, so shims refused together do not return together. The
# base is the reply's `Retry-After` when it names a number of seconds, held
# between RETRY_BACKOFF_BASE_SECONDS and RETRY_AFTER_CAP_SECONDS per wait (a
# header of 0 or less, which HTTP allows, would otherwise make the retries a
# burst with no wait and no jitter, the load this schedule exists to avoid);
# otherwise RETRY_BACKOFF_BASE_SECONDS grown RETRY_BACKOFF_GROWTH-fold per
# retry (2, 4, 8, 16 s), capped at RETRY_BACKOFF_CAP_SECONDS. The sleeps add
# up to about 30 s on the exponential schedule and to four times the per-wait
# cap when every reply names one.
RETRY_AFTER_CAP_SECONDS = 30.0
RETRY_BACKOFF_BASE_SECONDS = 2.0
RETRY_BACKOFF_GROWTH = 2
RETRY_BACKOFF_CAP_SECONDS = 16.0
RETRY_JITTER_FLOOR = 0.5
# The broker refuses a request at the slot cap, the memory budget or the
# background cap only after holding it the full admission wait
# (credential_proxy.COMMAND_SLOT_WAIT_SECONDS; test_credential_proxy_client
# holds the two equal), so each such attempt costs a minute before its
# refusal. The terminal tool kills a command at 180 s (Hermes
# TERMINAL_TOOL_TIMEOUT_SECONDS, which cluster_preflight.sh mirrors), and a
# shim killed there leaves the model a timeout instead of the broker's reason.
# So a retry is started only when its wait, plus a full admission wait, would
# still end inside RETRY_DEADLINE_SECONDS of the first send: two waited
# attempts (about 2 min) fit, a third does not, and the throttled line reaches
# the caller inside the tool's cap. Refusals the broker answers at once are not
# held back by it. The rule reserves the retry's admission wait and not the
# command's run after it: a retry the broker admits in the last seconds of
# its queue, whose command then uses most of its own bound (60 s for a
# one-shot kubectl), ends past the tool's kill, which closes the shim's
# connection and has the broker kill the command (CommandExecutor, on a
# caller that has gone); the model then reads a timeout, not output. The
# window is a few seconds wide (admission in the last seconds of the queue,
# a run within seconds of its bound), and reserving the run as well would
# leave no retry at all for a waited refusal under 170 s, so the window is
# the price of retrying one; §4.3 of the exec-tier design states it. A caller
# that kills the shim sooner than the tool does names its own deadline, in
# seconds from the first send, in RETRY_DEADLINE_ENV, less what this rule
# does not reserve: the Cluster Agent preflight's `timeout` on one brokered
# call, 125 s, has room for one admission wait and one command run, so it
# passes the cap less its margin and the run bound, 60 s, under which no
# retry fits, and the shim gives up at the first refusal with the broker's
# reason instead of being killed with that reason unread. The variable only
# shortens the deadline: a longer one would put the throttled line past the
# tool's kill again. Unset, empty, or anything but a finite non-negative
# number keeps the default.
BROKER_ADMISSION_WAIT_SECONDS = 60.0
RETRY_DEADLINE_SECONDS = 170.0
RETRY_DEADLINE_ENV = "CREDENTIAL_PROXY_RETRY_DEADLINE_SECONDS"
# What an answer with no JSON body is reported as, a refusal or a 2xx; the
# broker's own text is used when a refusal sent one.
NON_JSON_RESPONSE_TEXT = "non-JSON response"
# How deep a `/v1/vcs/*` body can nest lists and objects. `vcs_call` refuses a
# 2xx answer over the bound and takes the parts that are too deep out of a
# refusal. A broker answer is a few levels deep. Python 3.14 parses a body of
# 100,000 levels, and `json.dumps` and `repr` then raise RecursionError on the
# result, in whatever caller uses it. The bound is under the limit of each of
# them, and it is the bound of the version-control skill's print step. The
# shim holds a `/v1/exec` refusal and a relayed API refusal to the same bound
# (`_refusal_payload`, `ApiSession._json_body`), with the same trim.
MAX_VCS_BODY_DEPTH = 500
# The message of the error for a 2xx body over that bound. It names no value:
# `vcs_client` gives the caller a text of its own.
_VCS_ANSWER_TOO_DEEP_TEXT = "the broker's answer is JSON nested too deep to read"
# A refusal over the bound keeps its keys, and each value that is too deep
# becomes this text. Its `error`, when that is one of them, becomes the status,
# as for a body that is no refusal document.
_REFUSAL_PART_TOO_DEEP_TEXT = "left out: nested too deep to read"
_REFUSAL_ERROR_FIELD = "error"
_REFUSAL_STATUS_TEXT = "HTTP {status}"
CLASS_INTERACTIVE = "interactive"
CLASS_BACKGROUND = "background"
CLASS_LABEL = "class"
BACKGROUND_NICE = 10
BROKER_TIER_EXEC = "exec"
BROKER_TIER_STATEFUL = "stateful"
AUDIT_BROKER_TIER = "broker_tier"
AUDIT_CLASS = "class"
AUDIT_BROKER_POD = "broker_pod"

# The shim's own names for exec-tier routing; not part of the contract above.
# The stateful broker's URL, which an exec-role call falls back on when the exec
# tier cannot be reached, and every other call uses. The exec URL is plain http,
# as the broker's Service is: only `BrokerConnection` bounds the connect and
# tells "nothing was sent" apart.
BROKER_URL_ENV = "CREDENTIAL_PROXY_URL"
# The one line a fallback writes. No "credential proxy" prefix and no low-level
# cause: kubectl's failure classifiers read the whole stderr, and either would
# make a real cluster connection failure after the fallback look like the
# shim's own (cluster_agent_profile.NOT_A_CONNECTION_FAILURE_RE) or a broker
# refusal look like the cluster's (CONNECTIVITY_FAILURE_RE).
EXEC_FALLBACK_NOTICE = "exec tier not reachable; this command went to the stateful broker"
# The broker route `execute` sends a command to, under the broker's URL.
EXEC_ROUTE = "/v1/exec"
# The exec body's session key (the broker's SESSION_ID_FIELD): the card or
# session id this process runs under, `default_caller_label`, so the broker
# can count a run's commands and break its retry loops. Omitted when there is
# none; a request without one is uncounted.
SESSION_ID_FIELD = "sessionId"
# The refusal codes the shim prints as a policy block (the message, then
# `policy rule: <rule>`) and exits EXIT_SECURITY_POLICY_BLOCKED on: the policy's
# own, and the broker's two per-session bounds. The exit code keeps the runtime
# from reading the refusal as a missing executable; the runtime's refusal stop
# (deploy/docker/patches/permission_refusal_stop.py) halts on the read-only
# rule's write refusals only, so for the two session bounds the message is
# what tells the model to stop.
SECURITY_POLICY_BLOCKED_CODE = "SECURITY_POLICY_BLOCKED"
SESSION_BUDGET_EXCEEDED_CODE = "BROKER_SESSION_BUDGET_EXCEEDED"
LOOP_BREAKER_CODE = "BROKER_LOOP_BREAKER"
POLICY_BLOCKED_CODES = frozenset({SECURITY_POLICY_BLOCKED_CODE, SESSION_BUDGET_EXCEEDED_CODE, LOOP_BREAKER_CODE})
# A profile worker's out-of-band record of a definitive write refusal. The
# broker marks such a refusal's body (credential_proxy.WRITE_REFUSAL_KEY): a
# policy rule (the read-only rule, or a profile caller's flag rule) refusing a
# mutating kubectl verb. Version-control refusals are never marked or recorded.
# On a 403
# carrying that mark, the client appends one JSON line to the file the worker
# adapter names in REFUSAL_FILE_ENV (a file on the task's tmpfs, beside the
# task token), and the adapter stops the task at the first record
# (a2a/worker-adapter/refusal.go). The record carries the task (REFUSAL_TASK_ENV,
# when set), the rule, the verb, the route and the time, all from the broker's
# answer and this call, never from a command's output. The file is opened
# append-only, without following a symlink and without creating it: the adapter
# makes it for each task, so a missing or unwritable file is a line on stderr
# and the call's result is unchanged.
WRITE_REFUSAL_KEY = "write_refusal"
REFUSAL_FILE_ENV = "CREDENTIAL_PROXY_REFUSAL_FILE"
REFUSAL_TASK_ENV = "CREDENTIAL_PROXY_REFUSAL_TASK"
# The write-refusal record's fields and its timestamp form.
REFUSAL_RECORD_RULE = "rule"
REFUSAL_RECORD_VERB = "verb"
REFUSAL_RECORD_ROUTE = "route"
REFUSAL_RECORD_TIME = "time"
REFUSAL_RECORD_TASK = "task"
REFUSAL_RECORD_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# A session pod's kubectl write attempt (record_kubectl_write_attempt): its rule,
# its route, and the env naming the kube-proxy mode the gateway passes the pod.
WRITE_ATTEMPT_RULE = "kube-proxy.write-attempt"
KUBE_PROXY_ROUTE = "kube-proxy"
KUBE_PROXY_MODE_ENV = "KUBE_PROXY_MODE"
_REFUSAL_RULE_RE = re.compile(r"\A[a-z0-9][a-z0-9.-]{0,63}\Z")
_REFUSAL_VERB_WORDS = 3
# The line a malformed exec URL writes, once per process, when the stateful
# broker takes its place. No "credential proxy" prefix, for the same reason as
# EXEC_FALLBACK_NOTICE; the reason names the shape, never a network cause.
EXEC_URL_IGNORED_NOTICE = "{name} ignored ({reason}); commands go to the stateful broker"
_exec_url_notice_written = False
# A connection that closed once the request was sent: the command may have run,
# so it is never sent again. The `credential proxy error` prefix marks the line
# as the shim's own for kubectl's failure classifiers.
_DROPPED_LINE = "credential proxy error: the connection dropped {cause}; the command may have run"
# A 2xx answer on `/v1/exec` that cannot be read. The broker ran the command
# before it answered, so the line ends as the dropped connection's does, and
# the command is never sent again. `{what}` names the shape: the body can be
# a command's output and is never printed.
_UNREADABLE_ANSWER_LINE = "credential proxy error (HTTP {status}): {what}; the command may have run"
_NOT_AN_OBJECT_TEXT = "the response is JSON of type {type}, not an object"
# The fields of that answer that `execute` writes or returns. A field of another
# type makes the whole answer unreadable: the line names the field and the
# type, never a value, and nothing of the answer is printed.
_EXEC_OUTPUT_FIELDS = ("stdout", "stderr")
_EXEC_KUBECONFIG_FIELD = "kubeconfig"
_EXEC_EXIT_CODE_FIELD = "exitCode"
_FIELD_NOT_A_STRING_TEXT = "the response's {field} is of type {type}, not a string"
_EXIT_CODE_NOT_AN_INTEGER_TEXT = "the response's exitCode is of type {type}, not an integer"
_NO_EXIT_CODE_TEXT = "the response has no exitCode"
# The broker sends the command's return code as `exitCode`. A process status is
# 0 to 255. A command that a signal ended has the signal's negative number
# there, and a shell reports that death as 128 plus the number, so the least
# value with a status is 128 - 255. A value outside the two bounds is no
# command's result, and `sys.exit` would wrap it: 256 would read as success.
_MAX_EXIT_STATUS = 255
_SIGNAL_EXIT_BASE = 128
_MIN_EXIT_CODE = _SIGNAL_EXIT_BASE - _MAX_EXIT_STATUS
_EXIT_CODE_OUT_OF_RANGE_TEXT = f"the response's exitCode is outside {_MIN_EXIT_CODE} to {_MAX_EXIT_STATUS}"
# The least read time a relay fallback is worth sending with; less, and the
# caller's budget is already spent, so the error is raised instead.
RELAY_FALLBACK_MIN_SECONDS = 1.0

# Where a `get-credentials` that named no destination leaves its file:
# `$HERMES_HOME/.kubeconfigs/kubeconfig_<project>_<cluster>_<location>.yaml`,
# the convention agents/platform/AGENTS.md ("Cluster Credentials") teaches and
# `platform_mcp_server._thread_kubeconfig_path` owns. The default home is the
# one that convention spells as `${HERMES_HOME:-/opt/data}`.
DEFAULT_HERMES_HOME = "/opt/data"
KUBECONFIG_DIR_NAME = ".kubeconfigs"
DEFAULT_KUBE_PROXY_CA_FILE = "/var/run/secrets/kubeagents/kube-proxy-ca/ca.crt"
# A `get-credentials` the shim answers itself when the kube-api proxy is in
# use (`answer_get_credentials_locally`): the broker would run gcloud only for
# the shim to replace its kubeconfig with a kube-proxy one built from the
# context name, about half of the gcloud slot time the fleet audits put on the
# broker (#5444). The flags it understands; any other sends the call to the
# broker as before. Valued flags take the next token or `=value`. The endpoint
# flags (`--internal-ip`, `--dns-endpoint`) are accepted and have no effect:
# the kubeconfig names kube-proxy, which picks the cluster's endpoint itself.
GET_CREDENTIALS_LOCAL_VALUED_FLAGS = frozenset({"--location", "--zone", "-z", "--region", "--project"})
GET_CREDENTIALS_LOCAL_LOCATION_FLAGS = frozenset({"--location", "--zone", "-z", "--region"})
GET_CREDENTIALS_LOCAL_BARE_FLAGS = frozenset({"--internal-ip", "--dns-endpoint", "--quiet", "-q"})
# The one relay read that tells a typo from a cluster: the route the Cluster
# Agent reconcile already uses to ask whether a cluster still exists. Its
# answer is projected to name, location, status and id. A relay read takes
# well under a second; past this bound the broker answers instead.
GET_CREDENTIALS_CHECK_URL = "https://container.googleapis.com/v1/projects/{project}/locations/{location}/clusters/{cluster}"
GET_CREDENTIALS_CHECK_TIMEOUT_SECONDS = 5
# Sent on that read so the broker refuses it for a cluster the install's
# spec.scope.exclude.clusters names (credential_proxy.GET_CREDENTIALS_CHECK_HEADER,
# held equal by a test). It only narrows: the refusal sends the call to the
# broker's get-credentials, which refuses an excluded cluster itself (#5305);
# the sandbox has no scope file to check against.
GET_CREDENTIALS_CHECK_HEADER = "X-Kube-Agents-Get-Credentials-Check"
GET_CREDENTIALS_RUNNING_STATUS = "RUNNING"
# What gcloud prints on stderr for a get-credentials of a running cluster; the
# shim prints the same, so a caller reads the same lines.
GET_CREDENTIALS_FETCHING_LINE = "Fetching cluster endpoint and auth data."
GET_CREDENTIALS_GENERATED_LINE = "kubeconfig entry generated for {cluster}."
DEFAULT_KUBE_PROXY_TOKEN_FILE = "/var/run/secrets/kubeagents/credential-proxy/token"
KUBE_PROXY_USER_NAME = "kube-proxy-caller"
# Beside each kubeconfig the merge below checks, one line per context it found current or wrote:
# `<context> <sha256 of the file> <sha256 of record version, proxy URL, CA file and token file>`. The
# sandbox's kubectl wrapper (deploy/sandbox/kubectl_context_registered.sh) skips Python only on an
# exact line, so any later edit to the file, or a new proxy URL, CA file or token file, runs the
# merge once more.
REGISTERED_MARKER_NAME = ".{name}.contexts-registered"
REGISTERED_MARKER_ENV_SEPARATOR = "\n"
# The record sits on a volume that outlives the image. Increase this, here and as RECORD_VERSION in
# the wrapper's helper, when the merge changes what it accepts as a context's current entries: a
# record from an older client then matches nothing, and the merge runs once more for that context.
REGISTERED_MARKER_VERSION = "1"
# The merge's lock beside a kubeconfig `<dir>/<name>`. sandbox-entrypoint removes the default
# kubeconfig's at pod start (KUBECONFIG_LOCK_NAME in deploy/sandbox/entrypoint.sh).
KUBECONFIG_LOCK_NAME = ".{name}.lock"
KUBECONFIG_LOCK_MODE = 0o600
# What the kubeconfig's owner is told when the lock cannot be opened. The merge needs the lock, so
# the file is not read: a context that is missing from it stays missing, and kubectl then says the
# context does not exist. The owner note is left out when the lock is not there to ask.
KUBECONFIG_LOCK_UNOPENABLE_NOTICE = (
    "credential proxy: cannot open the kubeconfig lock {lock}{owner}: {reason}; "
    "context {context} was not checked or registered in {kubeconfig}"
)
KUBECONFIG_LOCK_OWNER_NOTE = " (owned by uid {uid})"
ROOT_UID = 0
# What a caller is told when `.kubeconfigs` is not its own. The per-target file and the shell's pin
# are written only by the folder's owner.
NOT_THE_CALLERS_FOLDER_NOTICE = "{folder} belongs to uid {owner}, not to this caller (uid {caller})"
# The way to the cluster for a caller that the rule refuses: a kubeconfig that it names itself.
OWN_KUBECONFIG_ADVICE_LINES = (
    "    KUBECONFIG=<a file of your own> gcloud container clusters get-credentials ...",
    "    KUBECONFIG=<the same file> kubectl ...",
)
SANDBOX_HERMES_UID = 1001
SANDBOX_HERMES_KUBECONFIG_PATH = Path("/home/hermes/.kube/config")
SANDBOX_WORKER_UID = 1000
SANDBOX_WORKER_KUBECONFIG_PATH = Path("/home/node/.kube/config")
SANDBOX_SHARED_KUBECONFIG_PATH = Path("/opt/data/.kubeconfig")
# What an exec request sends as its `sessionId`, so the broker can count the
# run's commands: the kanban card first, then the scheduled run, then the
# session. Where each comes from matters, because no process environment
# crosses the SSH hop into the shell sandbox: there the ForceCommand
# (deploy/sandbox/session-command.sh) recovers the card and the cron run from
# the command's cwd, and a chat turn, which runs from neither, sends no key.
# HERMES_SESSION_ID reaches the shim only where the terminal runs in the
# shim's own pod, the profile and session pods. The grammar mirrors the
# broker's SESSION_ID_RE; a value outside it is dropped rather than sent,
# because the key serves the broker's count and must never be what fails a
# command.
CALLER_LABEL_ENV = ("HERMES_KANBAN_TASK", "HERMES_CRON_RUN", "HERMES_SESSION_ID")
CALLER_LABEL_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")
PER_TARGET_KUBECONFIG_NAME = "kubeconfig_{project}_{cluster}_{location}.yaml"

# A shell's context pin: which cluster the last context-less `get-credentials`
# in this command line fetched, so a context-less `kubectl` later in the same
# line reaches it -- `get-credentials seeded-a && kubectl get ...`, the shape
# #1968's workers ran. Kept under the kubeconfig directory because
# sandbox_mirror already excludes that from the profile mirror, and holding a
# context name and nothing else.
#
# Keyed on the nearest shell above the `get-credentials` -- stepping over a
# `timeout`, `xargs` or helper -- by its pid and its
# start time from /proc, so a recycled pid is a different key. Hermes runs
# every terminal command in a fresh `bash -c`, so the pin lasts exactly one
# command line: the next command, a resumed card and another worker all have a
# different shell and see none of it. A backgrounded `( ... ) &` subshell of
# two or more commands is a shell of its own and pins alone; bash execs a
# one-command `( get-credentials x ) &`, so that fetch pins the line's shell,
# the same as without the parentheses.
SHELL_CONTEXT_DIR_NAME = "shells"
SHELL_CONTEXT_SUFFIX = ".context"
# A GKE context name is under 200 characters; anything longer is not one.
MAX_SHELL_CONTEXT_BYTES = 512
PROC_ROOT = Path("/proc")
# /proc/<pid>/stat fields, counted from the one after the `)` that closes the
# command name (which may itself hold spaces and parentheses): the parent pid
# is field 4 and the start time field 22, so 1 and 19 here.
_STAT_PPID_INDEX = 1
_STAT_STARTTIME_INDEX = 19
# How far up a kubectl looks for a pin. `$(kubectl ...)`, a pipeline, a loop
# or `timeout`/`xargs` puts one or two processes between the shell and
# kubectl; this is a bound on a walk, not a depth anything reaches.
MAX_SHELL_ANCESTORS = 16
# The process names a pin may be keyed on: the shells a command line runs in.
# Only these are keys: the walk steps over any other process without keying it.
SHELL_COMMAND_NAMES = frozenset({"bash", "sh", "dash", "zsh", "ksh", "mksh", "ash"})
_SHELL_CONTEXT_FILE = re.compile(r"^([0-9]+)-([0-9]+)\.context\Z")

# kubectl stops reading its own flags here; what follows belongs to the
# command `kubectl exec` or `kubectl debug` runs.
END_OF_FLAGS = "--"
CONTEXT_FLAG = "--context"
KUBECONFIG_FLAG = "--kubeconfig"


def normalize_universe_domain(raw: str | None) -> str:
    """Validate and canonicalise a Google Cloud universe domain, defaulting to `googleapis.com` when unset/blank.

    Refuses a domain under `googleapis.com`: such a value names hosts of the commercial universe.
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        return DEFAULT_UNIVERSE_DOMAIN
    cleaned = trimmed.removesuffix(".").lower()
    if (
        not cleaned
        or len(cleaned) > MAX_UNIVERSE_DOMAIN_LENGTH
        or not _UNIVERSE_DOMAIN_RE.fullmatch(cleaned)
    ):
        raise ValueError(
            f"universe domain {raw!r} must be a lowercase DNS domain with at least two labels"
        )
    if cleaned.endswith(_GOOGLE_API_SUFFIX):
        # `<service>.mtls.googleapis.com` and the like are hosts of the commercial
        # universe. As a universe domain such a value would pass the rule that a
        # custom universe refuses the commercial hosts, and stay in the commercial one.
        raise ValueError(
            f"universe domain {raw!r} is under {DEFAULT_UNIVERSE_DOMAIN}, which is the "
            f"commercial universe and not another one; use {DEFAULT_UNIVERSE_DOMAIN}"
        )
    return cleaned


def google_universe_domain(environ=os.environ) -> str:
    """Return the active Google Cloud universe domain from the process environment."""
    for name in (GOOGLE_CLOUD_UNIVERSE_DOMAIN_ENV, CLOUDSDK_UNIVERSE_DOMAIN_ENV):
        raw = environ.get(name, "").strip()
        if raw:
            return normalize_universe_domain(raw)
    return DEFAULT_UNIVERSE_DOMAIN


def google_api_host(service: str, universe_domain: str | None = None) -> str:
    """Return `<service>.<universe_domain>` for the active or explicit universe domain."""
    domain = (
        normalize_universe_domain(universe_domain)
        if universe_domain is not None
        else google_universe_domain()
    )
    return f"{service}.{domain}"


def rewrite_google_api_host(host: str, universe_domain: str | None = None) -> str:
    """Rewrite `<service>.googleapis.com` to `<service>.<universe_domain>` when custom."""
    domain = (
        normalize_universe_domain(universe_domain)
        if universe_domain is not None
        else google_universe_domain()
    )
    lowered = host.lower()
    if domain != DEFAULT_UNIVERSE_DOMAIN and lowered.endswith(_GOOGLE_API_SUFFIX):
        return lowered[: -len(_GOOGLE_API_SUFFIX)] + "." + domain
    return host


class BrokerConnection(http.client.HTTPConnection):
    """Bound how long we wait to reach the broker, not how long it works.

    A plain ``urlopen(request, timeout=N)`` sets one socket timeout for the
    whole exchange, which would put a ceiling on the command as well as on the
    connect. That ceiling must not exist: Envoy routes /v1/exec with
    ``timeout: 0s`` deliberately, because a proxied ``gcloud container clusters
    get-credentials`` or a large ``git clone`` legitimately runs for minutes.

    Before the split there was no need for either — the broker was on the Pod's
    own loopback, so a connect either succeeded or was refused at once. Now the
    call crosses a Service. A Pending broker still fails fast, with
    ``[Errno 111] Connection refused`` from a Service that has no endpoints;
    what hangs is a SYN that is dropped rather than rejected, which is exactly
    what a default-deny egress policy does. So: a timeout while connecting, and
    none once connected.
    """

    def connect(self) -> None:
        self.timeout = BROKER_CONNECT_TIMEOUT_SECONDS
        try:
            super().connect()
        except (OSError, UnicodeError) as exc:
            # The one point where "nothing was sent" is certain. urllib wraps
            # this in a URLError as before, so a caller that reads only that
            # sees no change. UnicodeError is the resolver's IDNA step refusing
            # the host (an empty or over-long label), before any lookup.
            raise BrokerUnreachable(str(exc)) from exc
        self.sock.settimeout(None)


class BrokerUnreachable(OSError):
    """No connection to the broker could be made, so no request reached it."""


def exec_endpoints() -> tuple[str | None, str | None]:
    """The broker an exec-role call goes to, and the one it falls back on.

    The exec tier when the operator set a well-formed URL, with the stateful
    broker as the fallback; else the stateful broker alone, as before the exec
    tier. A malformed URL is ignored with one stderr line (`_exec_url_fault`).
    `execute` falls back only on a failure before a byte was sent (no
    connection to the exec tier, or a URL it could not build a request from):
    then no request reached a broker, and nothing ran. `ApiSession` also falls
    back after a dropped connection, because its relay calls are GETs, which
    are safe to send again.
    """
    # Spelled out, not BROKER_URL_ENV: tests/conformance/test_C_enforcement.py
    # (C1) pins that the shim reads this name, across the module boundary to
    # the operator and the gateway that set it.
    stateful = os.getenv("CREDENTIAL_PROXY_URL")
    exec_url = os.getenv(EXEC_URL_ENV, "").strip()
    if exec_url:
        reason = _exec_url_fault(exec_url)
        if reason is None or not stateful:
            # With no stateful broker to use instead, a malformed URL is
            # reported when the call is made, as before this check.
            return exec_url, stateful or None
        global _exec_url_notice_written
        if not _exec_url_notice_written:
            print(EXEC_URL_IGNORED_NOTICE.format(name=EXEC_URL_ENV, reason=reason), file=sys.stderr)
            _exec_url_notice_written = True
        return stateful, None
    return stateful, None


def _exec_url_fault(url: str) -> str | None:
    """Why the exec URL can't be used, or None.

    The broker opener's connect bound and its `BrokerUnreachable` signal are on
    plain http only, so an https URL would get neither the bound nor the
    fallback. A bad port or a missing scheme fails before any socket opens.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        # A bracketed host that is not an IPv6 address, such as `http://[::1`.
        return "not a valid URL"
    if parts.scheme != "http":
        return "not an http:// URL"
    if not parts.hostname:
        return "no host"
    try:
        parts.port
    except ValueError:
        return "the port is not a number from 0 to 65535"
    if "?" in url or "#" in url:
        # The route is appended to the URL, so it would land in the query or
        # be cut off with the fragment.
        return "it has a query or a fragment"
    try:
        # What execute() builds from the raw string, short of a socket: the
        # resolver's IDNA step, the connection's host check and the request
        # line's path check. urlsplit's view drops a tab or newline and keeps
        # a %-escape, so it can't answer for them.
        parts.hostname.encode("idna")
        request = urllib.request.Request(url.rstrip("/") + EXEC_ROUTE)
        connection = http.client.HTTPConnection(request.host)
        # urllib sends its own Host header, which http.client encodes as latin-1.
        connection.putrequest("POST", request.selector, skip_host=True)
        connection.putheader("Host", request.host)
    except (ValueError, http.client.InvalidURL):
        return "not a valid URL"
    return None


class _BrokerHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(BrokerConnection, req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect out of the broker.

    urllib re-sends the Authorization header across a cross-host redirect, so
    a 302 in a broker response would hand the projected token to wherever the
    Location points. Only reachable by something that already controls the
    broker's responses, but the header is the one thing worth not leaking on
    the way out.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


# A private opener rather than urllib.request.install_opener: this module is
# imported by github_token_refresh and the two relay patches, and a global
# opener would strip the total timeouts their own urlopen calls rely on.
# No proxy from the environment: the broker is an in-cluster Service, and a
# proxy in between would make every connect succeed, so a broker that cannot
# be reached could not be told apart from one that dropped the request.
_BROKER_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _BrokerHTTPHandler, _NoRedirect()
)


def open_broker_request(request: urllib.request.Request):
    """Send `request` to the broker with a bounded connect."""
    return _BROKER_OPENER.open(request)


class TokenUnavailable(Exception):
    """The configured caller token could not be read."""


def authorization_headers() -> dict[str, str]:
    """Return the credential that identifies this caller to the broker.

    The operator projects a ServiceAccount token with the broker's audience into
    every container that may call, and points CREDENTIAL_PROXY_TOKEN_FILE at it.
    Empty when that variable is unset, which is a misconfiguration rather than a
    layout: the broker is on a ClusterIP and authenticates every caller by
    TokenReview, so a request with no header earns a 401 that says so.

    Read on every invocation, never cached: the kubelet rewrites a projected
    token in place as it approaches expiry, and this process is short-lived
    enough that re-reading costs nothing.
    """
    token_file = os.environ.get("CREDENTIAL_PROXY_TOKEN_FILE", "").strip()
    if not token_file:
        return {}
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenUnavailable(f"{token_file}: {exc.strerror or exc}") from exc
    if not token:
        raise TokenUnavailable(f"{token_file} is empty")
    return {"Authorization": f"Bearer {token}"}

# A profile worker's task credential (#3889): the dispatcher's proxyToken, which
# the worker adapter writes to a tmpfs file on the task's confirm and removes at
# the task's end. Sent in this header on every exec and version-control call
# (a report credential only on the audit-report routes), re-read on every call,
# so a warm pod's next task presents its own. The broker confines an exec to the
# cluster the credential names, and admits a version-control write only under
# its permit (credential_proxy.TASK_CREDENTIAL_HEADER).
TASK_TOKEN_FILE_ENV = "CREDENTIAL_PROXY_TASK_TOKEN_FILE"
TASK_CREDENTIAL_HEADER = "X-Kube-Agents-Task-Credential"
# The broker's own ceiling on the value (task_credential.MAX_TOKEN_BYTES).
MAX_TASK_CREDENTIAL_BYTES = 8192
_TASK_CREDENTIAL_SHAPE = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
# gcloud settings that would choose the project, account, identity, endpoint,
# location or configuration of a call if gcloud ran here. It doesn't: the shim
# forwards no environment, and the broker's gcloud reads the broker's. In task
# mode the shim names any that are set rather than leaving the model to believe
# one took. Named rather than matched on the CLOUDSDK_ prefix: images and
# workstations set plenty of CLOUDSDK_* that redirect nothing (CLOUDSDK_PYTHON,
# the context-aware access settings), and a notice on every call that lists
# them would be noise the model learns to skip.
CLOUDSDK_REDIRECTING_ENV = frozenset({
    "CLOUDSDK_CORE_PROJECT",
    "CLOUDSDK_CORE_ACCOUNT",
    "CLOUDSDK_CORE_BILLING_QUOTA_PROJECT",
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "CLOUDSDK_ACTIVE_CONFIG_NAME",
    "CLOUDSDK_CONFIG",
    "CLOUDSDK_CONTAINER_CLUSTER",
    "CLOUDSDK_COMPUTE_ZONE",
    "CLOUDSDK_COMPUTE_REGION",
})
CLOUDSDK_ENDPOINT_OVERRIDE_PREFIX = "CLOUDSDK_API_ENDPOINT_OVERRIDES_"


def task_mode() -> bool:
    """Whether this shim runs for a profile task (CREDENTIAL_PROXY_TASK_TOKEN_FILE set)."""
    return bool(os.environ.get(TASK_TOKEN_FILE_ENV, "").strip())


# The broker routes a report credential (one naming no target cluster:
# task_credential.REPORT_TIER) is admitted on, and refused on every other,
# /v1/exec included (credential_proxy.REPORT_CREDENTIAL_ROUTES).
AUDIT_REPORTS_ROUTE = "/v1/audit-reports"


def _is_report_credential(token: str) -> bool:
    """Whether a task credential names no target cluster, read unverified.

    This only decides whether to send it; the broker verifies whatever is
    sent. A token whose claims can't be read is not called a report
    credential, so it is sent as before and the broker refuses it loudly.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return False
    segment = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, RecursionError):
        return False
    nats = claims.get("nats") if isinstance(claims, dict) else None
    return isinstance(nats, dict) and "target" not in nats


def _audit_reports_path(path: str) -> bool:
    path = urllib.parse.urlsplit(path).path
    return path == AUDIT_REPORTS_ROUTE or path.startswith(AUDIT_REPORTS_ROUTE + "/")


def task_credential_headers(path: str) -> dict[str, str]:
    """The task credential header for a broker request to `path`, or nothing.

    A report credential is sent only on the audit-report routes, the only
    ones the broker admits it on: on any other call the task goes without
    it and keeps the local-only commands a task with no credential gets.
    A credential that names a target is sent on every call.

    Read per call. A missing file is no task, not an error: the adapter
    removes it when the task ends, and the broker then runs local-only
    commands and nothing else, which is the answer to give. Any other read
    failure, or a value that isn't one compact JWT, is TokenUnavailable, for
    authorization_headers' reason: a request sent without it would earn a
    refusal that hides the fault.
    """
    token_file = os.environ.get(TASK_TOKEN_FILE_ENV, "").strip()
    if not token_file:
        return {}
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenUnavailable(f"{token_file}: {getattr(exc, 'strerror', None) or exc}") from exc
    if not token:
        return {}
    if len(token) > MAX_TASK_CREDENTIAL_BYTES or not _TASK_CREDENTIAL_SHAPE.match(token):
        raise TokenUnavailable(f"{token_file} does not hold a task credential")
    if _is_report_credential(token) and not _audit_reports_path(path):
        return {}
    return {TASK_CREDENTIAL_HEADER: token}


def ignored_task_environment() -> list[str]:
    """The environment a profile task's shim does not honour, by name.

    KUBECONFIG (and with it the shell's own get-credentials pin) would choose
    the cluster; the CLOUDSDK_* settings in CLOUDSDK_REDIRECTING_ENV would
    choose a project, account or endpoint if gcloud ran here. In task mode the cluster and project are the
    task credential's, which the broker enforces whatever is sent, so none of
    these is read, and naming them keeps the model from believing one took.
    """
    names = []
    if os.environ.get("KUBECONFIG", "").strip():
        names.append("KUBECONFIG")
    names.extend(
        sorted(
            name
            for name, value in os.environ.items()
            if (name in CLOUDSDK_REDIRECTING_ENV or name.startswith(CLOUDSDK_ENDPOINT_OVERRIDE_PREFIX))
            and value.strip()
        )
    )
    return names


# Only these read KUBECONFIG: kubectl to pick a context, gcloud to write one in
# `container clusters get-credentials`. Today that is every executable this
# client runs; it stays a separate list because an unreadable kubeconfig is a
# hard failure, and a later executable that ignores the variable should not be
# refused over one.
KUBECONFIG_AWARE = frozenset({"kubectl", "gcloud"})

# Flags whose value may be `-`, meaning "read the document from stdin". This is
# the whole list the shipped skills use: kubectl's `-f`/`--filename` and
# `--patch-file`. When a new call site needs a document from stdin, add its long
# flag; a short one is too easily a different flag on another CLI.
STDIN_FILE_FLAGS = frozenset({"-f", "--filename", "--patch-file"})


def reads_stdin(argv: list[str]) -> bool:
    """Whether this argv asks, explicitly, to read a document from stdin.

    The shim has never forwarded stdin, and the comment in `__main__` gives the
    reason: an MCP or other stdio-based parent may have a protocol stream on
    fd 0, and consuming it would break the parent rather than the command. That
    reason is sound and this does not overrule it -- it narrows it. Reading fd 0
    only when a flag in `STDIN_FILE_FLAGS` is followed by a bare `-` means the
    read happens when the caller wrote `kubectl apply -f -` and at no other
    time, and no MCP server is invoked that way.

    The consequence of getting this wrong is asymmetric and the narrow form errs
    the safe way: reading when we should not corrupts a parent's protocol
    stream, while not reading when we should leaves the command receiving an
    empty document -- which is exactly the behaviour today.
    """
    for index, token in enumerate(argv):
        if token in STDIN_FILE_FLAGS and index + 1 < len(argv) and argv[index + 1] == "-":
            return True
        if "=" in token:
            flag, _, value = token.partition("=")
            if flag in STDIN_FILE_FLAGS and value == "-":
                return True
    return False


class ClusterTarget:
    """A GKE cluster identified well enough to re-fetch credentials for it.

    Immutable, compared and hashed by its three fields, as the frozen dataclass
    it replaces (see the note at the imports).
    """

    __slots__ = ("project", "location", "cluster")

    def __init__(self, project: str, location: str, cluster: str) -> None:
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "location", location)
        object.__setattr__(self, "cluster", cluster)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"cannot delete field {name!r}")

    def _fields(self) -> tuple[str, str, str]:
        return (self.project, self.location, self.cluster)

    def __eq__(self, other: object) -> bool:
        if other.__class__ is self.__class__:
            return self._fields() == other._fields()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._fields())

    def __repr__(self) -> str:
        return f"ClusterTarget(project={self.project!r}, location={self.location!r}, cluster={self.cluster!r})"

    def __reduce__(self):
        return (self.__class__, self._fields())

    @property
    def context_name(self) -> str:
        return f"gke_{self.project}_{self.location}_{self.cluster}"


def parse_gke_context(context: str) -> ClusterTarget | None:
    """Recover the cluster triple from a `gke_<project>_<location>_<cluster>` name.

    This is the same convention the operator builds in `buildCredentialProxyEnv`
    and the Cluster Agent preflight compares against, and it is what makes the
    broker's regeneration possible: the context name alone says which cluster to
    ask Google for. Underscores are the separator and none of the three
    components may contain one, so a 4-way split is unambiguous.

    Each component is held to the GKE naming rules, which is also what keeps the
    value safe to use in a filename — no separators, no dots, no traversal, and
    no newline, which `$` would have let through and `context_name` would then
    have carried into a path and a log record.
    """
    parts = context.split("_", 3)
    if len(parts) != 4 or parts[0] != "gke":
        return None
    project, location, cluster = parts[1], parts[2], parts[3]
    if not all(
        _GKE_CONTEXT_COMPONENT.fullmatch(part) for part in (project, location, cluster)
    ):
        return None
    return ClusterTarget(project=project, location=location, cluster=cluster)


def read_current_context(text: str) -> str | None:
    """Read `current-context` out of a kubeconfig the way kubectl would.

    `yaml.safe_load`, deliberately, and never `yaml.CSafeLoader`. The C loader
    recurses in C, so a deeply nested document exits on SIGSEGV where the
    pure-Python loader raises a catchable `RecursionError`. `safe_load` picks
    the Python loader on its own; the point of saying so is that switching it
    would be a denial of service rather than an optimisation.

    Alias expansion is not a concern here. PyYAML resolves every reference to an
    anchor to the same node and caches the object built from it, so a
    billion-laughs document costs memory proportional to its own size rather
    than to its nominal expansion.

    Anything else — a syntax error, several documents, a top level that is not a
    mapping, a non-string `current-context` — reads as absent, and the caller
    turns that into a rejection. So does a scalar that PyYAML cannot build. For
    that it raises a plain exception, not a `yaml.YAMLError`: a `ValueError`
    for a date that does not exist or an `!!int` tag on text, a `KeyError` for
    an `!!bool` tag on text, an `IndexError` for an `!!int` tag on nothing, an
    `AttributeError` for an `!!timestamp` tag on text.
    """
    import yaml  # lazy: keeps the module importable without pyyaml, as elsewhere in this directory

    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, LookupError, AttributeError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    context = document.get("current-context")
    if not isinstance(context, str):
        return None
    return context.strip() or None


class KubeconfigUnreadable(RuntimeError):
    """A kubeconfig was named, and no cluster name could be taken from it."""


def kubeconfig_context(kubeconfig: str) -> str:
    """The GKE context a local kubeconfig pins, to send in the file's place.

    The broker is in another pod and cannot open this file. It also should not:
    a kubeconfig is not passive data — `users[].user.exec.command` names a
    program to run, `clusters[].cluster.server` and `proxy-url` choose where the
    access token is sent, and `users[].user.tokenFile` reads a file of the
    author's choosing and sends it as the bearer token. The agent writes this
    file, so parsing it beside the credentials was the thing worth avoiding, and
    the pod boundary now makes it impossible rather than merely discouraged.

    What crosses instead is the one string the broker ever took from it. The
    broker holds that string to `parse_gke_context` and regenerates every other
    field with `gcloud container clusters get-credentials`, so naming a cluster
    is all the authority this hands the caller — and `get-credentials` is bound
    by the same IAM the broker already runs under.

    Raises rather than returning None. A kubeconfig that cannot be read is a
    request whose target cluster is unknown, and running it anyway means running
    it against the broker's own default cluster: the wrong cluster, silently.
    """
    entries = [entry.strip() for entry in kubeconfig.split(os.pathsep) if entry.strip()]
    if not entries:
        raise KubeconfigUnreadable("KUBECONFIG is set but empty")
    if len(entries) > 1:
        raise KubeconfigUnreadable(
            "KUBECONFIG must name a single file; merged lists are not supported"
        )
    candidate = Path(entries[0])
    try:
        if candidate.stat().st_size > MAX_KUBECONFIG_BYTES:
            raise KubeconfigUnreadable(f"kubeconfig is implausibly large: {candidate}")
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise KubeconfigUnreadable(f"kubeconfig is unreadable: {candidate}: {exc}") from exc
    context = read_current_context(text)
    if not context:
        raise KubeconfigUnreadable(f"kubeconfig names no current-context: {candidate}")
    if parse_gke_context(context) is None:
        raise KubeconfigUnreadable(
            f"current-context {context!r} is not a GKE context name"
            " (expected gke_<project>_<location>_<cluster>)"
        )
    return context


def resolve_kubeconfig_flags(argv: list[str]) -> list[str]:
    """Rewrite any `--kubeconfig <path>` in argv to the context that path pins.

    The flag is the second door into cluster selection and beats the environment
    in kubectl's own precedence, so it has to be translated here for the same
    reason the environment is: the path names a file only this pod has. The
    broker resolves whichever it receives through the same regeneration.
    Stops at `--`, as `kubectl_context_flag` does: a `--kubeconfig` after it
    belongs to the command `kubectl exec` runs, names no file of this pod's,
    and is passed through untouched.
    """
    rewritten = list(argv)
    for index, argument in enumerate(rewritten):
        if argument == END_OF_FLAGS:
            break
        if argument == KUBECONFIG_FLAG and index + 1 < len(rewritten):
            rewritten[index + 1] = kubeconfig_context(rewritten[index + 1])
        elif argument.startswith(f"{KUBECONFIG_FLAG}="):
            _, _, value = argument.partition("=")
            rewritten[index] = f"{KUBECONFIG_FLAG}={kubeconfig_context(value)}"
    return rewritten


def names_kubeconfig_flag(argv: list[str]) -> bool:
    """Whether argv carries a `--kubeconfig` of its own, in either spelling."""
    for argument in argv:
        if argument == END_OF_FLAGS:
            return False
        if argument == KUBECONFIG_FLAG or argument.startswith(f"{KUBECONFIG_FLAG}="):
            return True
    return False


def kubectl_context_flag(argv: list[str]) -> str | None:
    """The `--context` a kubectl argv names, the last one winning as in kubectl.

    Stops at `--`: after it the words are the remote command's, and a
    `kubectl exec pod -- tool --context x` names no context of kubectl's.
    """
    context: str | None = None
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == END_OF_FLAGS:
            break
        if argument == CONTEXT_FLAG and index + 1 < len(argv):
            context = argv[index + 1]
            index += 2
            continue
        if argument.startswith(f"{CONTEXT_FLAG}="):
            context = argument.partition("=")[2]
        index += 1
    return context


def kubeconfig_dir() -> Path:
    """`$HERMES_HOME/.kubeconfigs`, read at call time so the session decides."""
    home = os.environ.get("HERMES_HOME", "").strip() or DEFAULT_HERMES_HOME
    return Path(home) / KUBECONFIG_DIR_NAME


def per_target_kubeconfig_path(target: ClusterTarget) -> Path:
    """The per-target file AGENTS.md tells the agent to export.

    Built from a `ClusterTarget`, so every component has already passed
    `parse_gke_context` and none can steer the path.
    """
    return kubeconfig_dir() / PER_TARGET_KUBECONFIG_NAME.format(
        project=target.project, cluster=target.cluster, location=target.location
    )


def _process_stat(pid: int) -> tuple[int, str, str] | None:
    """`(parent pid, start time, command name)` for `pid`, or None once it has exited."""
    try:
        raw = (PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = raw.rfind(")")
    comm = raw[raw.find("(") + 1 : close]
    fields = raw[close + 1 :].split()
    try:
        return int(fields[_STAT_PPID_INDEX]), fields[_STAT_STARTTIME_INDEX], comm
    except (IndexError, ValueError):
        return None


def _shell_keys() -> list[str]:
    """`<pid>-<start time>` for the shells this process runs under, nearest first.

    The walk starts at the parent and steps over anything that is not a shell
    -- `timeout`, `xargs`, a helper script -- without keying it, so recording
    and reading agree: a `timeout 60 gcloud ... get-credentials` records under
    the line's shell, replacing whatever an earlier fetch in the line recorded,
    and a `timeout 30 kubectl` under that shell reads it.

    Only shells are keys. sshd, which outlives every command line on a Hermes
    ControlMaster connection, is never one, so a pin file named for it is never
    read, and a default cannot outlast the line the way #1799's did. The cost:
    a helper that bash exec'd as the last command of a bare `bash -c` keeps the
    shell's pid but not its name, so a kubectl it starts reads the host. The
    Hermes command wrapper runs the command inside an `eval` that is not its
    last line, so bash does not exec it there.
    """
    keys: list[str] = []
    pid = os.getppid()
    for _ in range(MAX_SHELL_ANCESTORS):
        if pid <= 1:
            break
        stat = _process_stat(pid)
        if stat is None:
            break
        if stat[2] in SHELL_COMMAND_NAMES:
            keys.append(f"{pid}-{stat[1]}")
        pid = stat[0]
    return keys


def _own_key() -> str | None:
    """This process's own key, which is its shell's when the shell exec'd it.

    bash replaces itself with the last command of a `-c` string, an `eval` or a
    subshell instead of forking it, so in `get-credentials x ; kubectl ...` the
    kubectl *is* the shell that ran the get-credentials: same pid, same start
    time, and a parent above the command line.
    """
    pid = os.getpid()
    stat = _process_stat(pid)
    return f"{pid}-{stat[1]}" if stat is not None else None


def _shell_context_dir() -> Path:
    return kubeconfig_dir() / SHELL_CONTEXT_DIR_NAME


def _prune_shell_contexts(directory: Path, keep: str) -> None:
    """Remove the pins of shells that have exited, except `keep`. Best-effort.

    A pin is only ever read by its own shell's descendants, so once that shell
    is gone the file is dead weight; clearing it here, on the next write, keeps
    the directory to roughly the shells alive at once without a sweep.
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        match = _SHELL_CONTEXT_FILE.fullmatch(entry.name)
        if match is None or entry.name == keep:
            continue
        stat = _process_stat(int(match.group(1)))
        if stat is not None and stat[1] == match.group(2):
            continue
        try:
            entry.unlink()
        except OSError:
            pass


def _replace_file(path: Path, text: str) -> None:
    """Write `text` to `path` so a concurrent reader sees the old file or the new.

    A plain truncate-and-write leaves a window where the file is empty: a
    kubectl reading the shell's pin in it falls back to the host with exit 0, and
    one exporting the per-target kubeconfig, or a Cluster Agent's pinned
    `--kubeconfig`/`KUBECONFIG` destination, fails as unreadable. Both are the
    failures this module exists to prevent, so the file is staged beside its
    destination and renamed over it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def record_shell_context(context: str) -> bool:
    """Make `context` this command line's default for a kubectl naming no cluster.

    What gcloud does on a workstation -- the cluster you just fetched is
    current -- for the rest of the shell that fetched it and nothing else, so
    it never becomes the pod's default: the drift #1799 reported, which #1852
    closed by pinning the broker to the host cluster. The next command line, a
    resumed card or a multi-command parallel subshell has another shell, and a
    context-less kubectl there reads the host cluster exactly as #1852 left it.

    Returns whether the pin was recorded. Failing to record it costs the
    convenience, not the command, so it is reported and not raised. The pin is
    recorded only in a folder that the caller owns (`_claim_callers_folder`).
    """
    keys = _shell_keys()
    if not keys:
        return False
    directory = _shell_context_dir()
    path = directory / f"{keys[0]}{SHELL_CONTEXT_SUFFIX}"
    try:
        # `.kubeconfigs` first: root as the owner counts only for the folder above it.
        _claim_callers_folder(kubeconfig_dir())
        _claim_callers_folder(directory)
        _replace_file(path, context)
    except OSError as exc:
        print(f"credential proxy: could not record the shell's context in {path}: {exc}", file=sys.stderr)
        # An earlier fetch's pin in this shell would otherwise stay in force
        # while the caller says the line reads the host.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    _prune_shell_contexts(directory, keep=path.name)
    return True


def shell_context() -> str | None:
    """The context the nearest enclosing shell's context-less get-credentials fetched.

    The file is the agent's to write, so what it holds is caller input like any
    other: it crosses only as a name, and only a name `parse_gke_context`
    accepts. Naming a cluster is what `KUBECONFIG` already lets a caller do.

    Only a pin this uid wrote counts. The hermes principal runs with
    HERMES_HOME=/opt/data, a directory the sandboxed agent owns; a pin the
    agent planted there under a hermes shell's key would otherwise steer a
    trusted kubectl. The open refuses a symlink and the file must be a regular
    file with one link, owned by this uid, which a file the agent made is not.
    """
    directory = _shell_context_dir()
    own = _own_key()
    uid = os.geteuid()
    for key in ([own] if own else []) + _shell_keys():
        try:
            # O_NONBLOCK: a FIFO planted under the name would otherwise hold
            # open() until a writer appears, before fstat can refuse it.
            fd = os.open(
                directory / f"{key}{SHELL_CONTEXT_SUFFIX}",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except OSError:
            continue
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1:
                return None
            raw = stream.read(MAX_SHELL_CONTEXT_BYTES + 1)
        if len(raw) > MAX_SHELL_CONTEXT_BYTES:
            return None
        context = raw.decode("utf-8", errors="replace").strip()
        return context if parse_gke_context(context) is not None else None
    return None


def implicit_kubectl_context(argv: list[str]) -> str:
    """The cluster a kubectl that names no kubeconfig should reach, or "".

    Only reached when neither `--kubeconfig` nor `KUBECONFIG` is set: those
    pin a cluster already and keep precedence, so a Cluster Agent's pinned
    file is never overridden from here.

    `--context` first. The broker runs a context-less request against the host
    cluster's file, which holds the host context alone, so a `--context` naming
    any other cluster used to end in "context was not found". Sending the name
    instead has the broker generate that cluster's file, whose one context is
    the one the flag names. A `--context` that is not a GKE name is left to
    kubectl, as before.

    Then the shell's pin, when there is no `--context` at all. Empty otherwise,
    which is the broker's host default.
    """
    flag = kubectl_context_flag(argv)
    if flag is not None:
        return flag if parse_gke_context(flag) is not None else ""
    return shell_context() or ""


def build_kube_proxy_kubeconfig(
    context: str,
    proxy_url: str | None = None,
    ca_file: str | None = None,
    token_file: str | None = None,
) -> str:
    """Build a kubeconfig document that routes `context` through `k8s-api-proxy`."""
    import yaml

    resolved_url = (proxy_url or os.environ.get("KUBE_PROXY_URL", "")).strip().rstrip("/")
    resolved_ca = (
        ca_file
        or os.environ.get("KUBE_PROXY_CA_FILE", "")
        or DEFAULT_KUBE_PROXY_CA_FILE
    ).strip()
    resolved_token = (
        token_file
        or os.environ.get("CREDENTIAL_PROXY_TOKEN_FILE", "")
        or DEFAULT_KUBE_PROXY_TOKEN_FILE
    ).strip()
    doc = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": context,
                "cluster": {
                    "server": f"{resolved_url}/clusters/{context}",
                    "certificate-authority": resolved_ca,
                },
            }
        ],
        "users": [
            {
                "name": KUBE_PROXY_USER_NAME,
                "user": {
                    "tokenFile": resolved_token,
                },
            }
        ],
        "contexts": [
            {
                "name": context,
                "context": {
                    "cluster": context,
                    "user": KUBE_PROXY_USER_NAME,
                },
            }
        ],
        "current-context": context,
    }
    return yaml.safe_dump(doc, sort_keys=False)


def _caller_default_kubeconfig_path() -> Path:
    """Return the default kubeconfig path for the active sandbox or worker user."""
    uid = os.geteuid()
    if uid == SANDBOX_HERMES_UID:
        return SANDBOX_HERMES_KUBECONFIG_PATH
    if uid == SANDBOX_WORKER_UID and SANDBOX_WORKER_KUBECONFIG_PATH.parent.parent.is_dir():
        return SANDBOX_WORKER_KUBECONFIG_PATH
    return SANDBOX_SHARED_KUBECONFIG_PATH


def _default_kubeconfig_candidates() -> list[Path]:
    """Return the default kubeconfig files to update when a cluster context is registered."""
    return [_caller_default_kubeconfig_path(), SANDBOX_SHARED_KUBECONFIG_PATH]


def _is_default_sandbox_kubeconfig(path: str | Path) -> bool:
    """Return True when `path` is one of the sandbox default multi-context kubeconfigs."""
    return Path(path) in _default_kubeconfig_candidates()


def _caller_owns_folder(folder: Path) -> bool:
    """Whether the caller owns `folder`. A default kubeconfig is created only in a folder that its
    caller owns. Only a kubeconfig's owner registers a context in it, so the agent can never add a
    cluster to a file that root made in the agent's folder. The question is about the folder that
    holds the file, not the one above it. A worker's home can be a mount that root owns, and the
    worker makes `.kube` in it."""
    return folder.stat().st_uid == os.geteuid()


class NotTheCallersFolder(PermissionError):
    """The folder for a per-target kubeconfig or a shell's pin is not the caller's."""


def _claim_callers_folder(folder: Path) -> None:
    """Make `folder` when it is missing, and raise `NotTheCallersFolder` unless the caller owns it.

    The per-target kubeconfig and the shell's pin are written only in a folder that the caller
    owns. Root in the shell pod has the agent's home as its default: a `.kubeconfigs` that root
    made there was one the agent could not write its file in.

    A missing folder is made only below a folder that belongs to the caller or to root. A session
    pod's home can be a mount that root made for the pod's user, and that user makes
    `.kubeconfigs` in it. Another user's home gets no new folder.
    """
    caller = os.geteuid()
    existing = folder
    while not existing.exists():
        existing = existing.parent
    if existing != folder:
        owner = existing.stat().st_uid
        if owner not in (caller, ROOT_UID):
            raise NotTheCallersFolder(
                NOT_THE_CALLERS_FOLDER_NOTICE.format(folder=existing, owner=owner, caller=caller)
            )
        folder.mkdir(parents=True, exist_ok=True)
    if not _caller_owns_folder(folder):
        raise NotTheCallersFolder(
            NOT_THE_CALLERS_FOLDER_NOTICE.format(folder=folder, owner=folder.stat().st_uid, caller=caller)
        )


def _ensure_host_kubeconfig_initialized(
    proxy_url: str,
    ca_file: str,
    token_file: str,
) -> None:
    """Create the caller's default kubeconfig for `KUBE_PROXY_HOST_CONTEXT` when absent, in a
    folder that the caller owns."""
    host_context = os.environ.get("KUBE_PROXY_HOST_CONTEXT", "").strip()
    if not host_context:
        return
    path = _caller_default_kubeconfig_path()
    if path.exists() or path.is_symlink():
        return
    if not path.parent.parent.is_dir():
        return
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not _caller_owns_folder(path.parent):
            return
        content = build_kube_proxy_kubeconfig(
            host_context,
            proxy_url=proxy_url,
            ca_file=ca_file,
            token_file=token_file,
        )
        _replace_file(path, content)
    except OSError:
        pass


def _record_registered_context(
    path: Path,
    content: bytes,
    context: str,
    proxy_url: str,
    ca_file: str,
    token_file: str,
) -> None:
    """Record that `content`, the bytes of `path` the merge checked or wrote, holds `context`'s current
    entries. Called under the merge's lock; the digest is of those bytes, never of a later read, so a
    writer that does not take the lock can only make the line stale, never vouch for unchecked bytes."""
    marker = path.with_name(REGISTERED_MARKER_NAME.format(name=path.name))
    file_digest = hashlib.sha256(content).hexdigest()
    env_digest = hashlib.sha256(
        REGISTERED_MARKER_ENV_SEPARATOR.join(
            (REGISTERED_MARKER_VERSION, proxy_url, ca_file, token_file)
        ).encode("utf-8")
    ).hexdigest()
    kept = []
    if marker.is_file() and not marker.is_symlink():
        kept = [line for line in marker.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.split(" ", 1)[0] != context]
    _replace_file(marker, "\n".join([*kept, f"{context} {file_digest} {env_digest}"]) + "\n")


def _report_unopenable_lock(lock_path: Path, kubeconfig: Path, context: str, reason: OSError) -> None:
    """Say that the kubeconfig's owner could not open its lock. Before the owner check came first
    in the merge, a root `kubectl --context` left a lock the agent user could not open, and the
    merge returned without a word."""
    try:
        owner = KUBECONFIG_LOCK_OWNER_NOTE.format(uid=lock_path.lstat().st_uid)
    except OSError:
        owner = ""
    print(
        KUBECONFIG_LOCK_UNOPENABLE_NOTICE.format(
            lock=lock_path, owner=owner, reason=reason.strerror or reason, context=context, kubeconfig=kubeconfig
        ),
        file=sys.stderr,
    )


def _merge_context_into_kubeconfig_file(
    path: Path,
    context: str,
    proxy_url: str,
    ca_file: str,
    token_file: str,
) -> None:
    import fcntl
    import yaml

    try:
        if not path.parent.is_dir():
            if path == _caller_default_kubeconfig_path() and path.parent.parent.is_dir():
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                return
        if not path.exists() and not path.is_symlink() and path == _caller_default_kubeconfig_path():
            if not _caller_owns_folder(path.parent):
                return
            _replace_file(
                path,
                build_kube_proxy_kubeconfig(
                    context,
                    proxy_url=proxy_url,
                    ca_file=ca_file,
                    token_file=token_file,
                ),
            )
            return
        # Only the kubeconfig's owner can change it (the check under the lock, below, says so
        # again), so only the owner makes the lock: one that root made is one the owner cannot open.
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return
    except OSError:
        return
    lock_path = path.with_name(KUBECONFIG_LOCK_NAME.format(name=path.name))
    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, lock_flags, KUBECONFIG_LOCK_MODE)
    except OSError as exc:
        _report_unopenable_lock(lock_path, path, context, exc)
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if path.is_symlink() or not path.is_file():
                return
            st = path.stat()
            if st.st_uid != os.geteuid() or st.st_nlink != 1:
                return
            content = path.read_bytes()
            doc = yaml.safe_load(content.decode("utf-8", errors="replace"))
        except (OSError, yaml.YAMLError, ValueError, LookupError, AttributeError, RecursionError):
            # The plain exceptions are PyYAML's for a scalar that it cannot
            # build (`read_current_context` lists them). The file stays as it is.
            return
        if not isinstance(doc, dict):
            return
        expected_server = f"{proxy_url}/clusters/{context}"
        existing_clusters = list(doc.get("clusters") or [])
        existing_contexts = list(doc.get("contexts") or [])
        existing_users = list(doc.get("users") or [])
        has_cluster = any(
            isinstance(c, dict)
            and c.get("name") == context
            and isinstance(c.get("cluster"), dict)
            and c["cluster"].get("server") == expected_server
            and c["cluster"].get("certificate-authority") == ca_file
            for c in existing_clusters
        )
        has_context = any(
            isinstance(c, dict)
            and c.get("name") == context
            and isinstance(c.get("context"), dict)
            and c["context"].get("cluster") == context
            and c["context"].get("user") == KUBE_PROXY_USER_NAME
            for c in existing_contexts
        )
        has_user = any(
            isinstance(u, dict)
            and u.get("name") == KUBE_PROXY_USER_NAME
            and u.get("user") == {"tokenFile": token_file}
            for u in existing_users
        )
        if has_cluster and has_context and has_user:
            try:
                _record_registered_context(path, content, context, proxy_url, ca_file, token_file)
            except OSError:
                pass
            return
        clusters = [
            c for c in existing_clusters if isinstance(c, dict) and c.get("name") != context
        ]
        clusters.append(
            {
                "name": context,
                "cluster": {
                    "server": expected_server,
                    "certificate-authority": ca_file,
                },
            }
        )
        doc["clusters"] = clusters
        users = [
            u
            for u in existing_users
            if isinstance(u, dict) and u.get("name") != KUBE_PROXY_USER_NAME
        ]
        users.append(
            {
                "name": KUBE_PROXY_USER_NAME,
                "user": {"tokenFile": token_file},
            }
        )
        doc["users"] = users
        contexts = [
            c for c in existing_contexts if isinstance(c, dict) and c.get("name") != context
        ]
        contexts.append(
            {
                "name": context,
                "context": {
                    "cluster": context,
                    "user": KUBE_PROXY_USER_NAME,
                },
            }
        )
        doc["contexts"] = contexts
        try:
            written = yaml.safe_dump(doc, sort_keys=False)
            _replace_file(path, written)
            _record_registered_context(path, written.encode("utf-8"), context, proxy_url, ca_file, token_file)
        except OSError:
            pass
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def merge_context_into_default_kubeconfig(context: str) -> None:
    """Register `context` in the default kubeconfig while preserving `current-context`."""
    proxy_url = os.environ.get("KUBE_PROXY_URL", "").strip().rstrip("/")
    if not proxy_url:
        return
    ca_file = (
        os.environ.get("KUBE_PROXY_CA_FILE", "") or DEFAULT_KUBE_PROXY_CA_FILE
    ).strip()
    token_file = (
        os.environ.get("CREDENTIAL_PROXY_TOKEN_FILE", "")
        or DEFAULT_KUBE_PROXY_TOKEN_FILE
    ).strip()
    _ensure_host_kubeconfig_initialized(proxy_url, ca_file, token_file)
    seen: set[Path] = set()
    for path in _default_kubeconfig_candidates():
        if path in seen:
            continue
        seen.add(path)
        _merge_context_into_kubeconfig_file(
            path, context, proxy_url, ca_file, token_file
        )


def ensure_kube_proxy_context_for_argv(argv: list[str]) -> None:
    """Register a `--context=gke_...` flag's cluster in the active kubeconfig before `kubectl` runs."""
    proxy_url = os.environ.get("KUBE_PROXY_URL", "").strip().rstrip("/")
    if not proxy_url:
        return
    ca_file = (
        os.environ.get("KUBE_PROXY_CA_FILE", "") or DEFAULT_KUBE_PROXY_CA_FILE
    ).strip()
    token_file = (
        os.environ.get("CREDENTIAL_PROXY_TOKEN_FILE", "")
        or DEFAULT_KUBE_PROXY_TOKEN_FILE
    ).strip()
    normalized = argv if (argv and argv[0] == "kubectl") else ["kubectl", *argv]
    _ensure_host_kubeconfig_initialized(proxy_url, ca_file, token_file)
    if names_kubeconfig_flag(normalized):
        return
    flag = kubectl_context_flag(normalized)
    target = parse_gke_context(flag) if flag else None
    if target is None:
        return
    merge_context_into_default_kubeconfig(target.context_name)
    env_kc = os.environ.get("KUBECONFIG", "").strip()
    if env_kc and os.pathsep not in env_kc:
        candidate = Path(env_kc)
        if candidate not in _default_kubeconfig_candidates():
            _merge_context_into_kubeconfig_file(
                candidate, target.context_name, proxy_url, ca_file, token_file
            )


def rewrite_kubeconfig_for_kube_proxy(generated: str) -> str:
    """Rewrite a `get-credentials` kubeconfig to route through `k8s-api-proxy`."""
    proxy_url = os.environ.get("KUBE_PROXY_URL", "").strip()
    if not proxy_url:
        return generated
    context = read_current_context(generated)
    target = parse_gke_context(context) if context else None
    if target is None:
        return generated
    merge_context_into_default_kubeconfig(target.context_name)
    return build_kube_proxy_kubeconfig(target.context_name, proxy_url=proxy_url)


def land_implicit_kubeconfig(generated: str) -> None:
    """File a context-less get-credentials' kubeconfig, and say how to use it.

    gcloud on a workstation would have written the default kubeconfig and made
    the fetched cluster current. Here that default is the broker's, pinned to
    the host cluster (#1852), so without this the file vanished into the
    broker's pod and the agent's next bare `kubectl` read the host -- exit 0,
    an empty answer, and a fixture reported absent that was there.

    The context comes from what the broker returned, not from argv: gcloud
    resolves an omitted `--project` or a `--zone`/`--region` spelling itself,
    and the file is the only place the answer is written down.

    The file is written only in a `.kubeconfigs` that the caller owns
    (`_claim_callers_folder`). A caller that this rule refuses is told to name
    a file of its own on both commands.

    Never changes the exit code: gcloud succeeded.
    """
    context = read_current_context(generated)
    target = parse_gke_context(context) if context else None
    if target is None:
        return
    path = per_target_kubeconfig_path(target)
    written = True
    refused = False
    try:
        _claim_callers_folder(path.parent)
        _replace_file(path, generated)
    except OSError as exc:
        written = False
        refused = isinstance(exc, NotTheCallersFolder)
        print(f"credential proxy: could not write {path}: {exc}", file=sys.stderr)
    pinned = False
    if not os.environ.get("KUBE_PROXY_URL", "").strip():
        pinned = record_shell_context(target.context_name)
    lines = ["credential proxy: no --kubeconfig or KUBECONFIG was given."]
    if written:
        lines.append(f"  Credentials for {target.context_name} are in {path}.")
    if pinned:
        lines.append(
            "  For the rest of this command line, a kubectl that names no cluster"
            f" reads {target.context_name}. Later commands read the host cluster;"
            f" to reach {target.context_name} from them:"
        )
    else:
        lines.append(
            "  A kubectl that names no cluster still reads the host cluster."
            f" To reach {target.context_name}:"
        )
    if written:
        lines.append(f"    export KUBECONFIG={path}")
    if refused:
        lines.extend(OWN_KUBECONFIG_ADVICE_LINES)
    else:
        lines.append(f"    kubectl --context={target.context_name} ...")
    print("\n".join(lines), file=sys.stderr)


def report_task_kubeconfig(generated: str) -> None:
    """Say, in one line, what a get-credentials in a profile task did.

    Nothing is filed and no shell context is recorded: in a task neither is
    read (the broker holds every kubectl and gcloud call to the task's
    cluster and project), and `land_implicit_kubeconfig`'s advice -- the
    host cluster for later commands, `export KUBECONFIG` -- would be false.
    """
    context = read_current_context(generated)
    target = parse_gke_context(context) if context else None
    if target is None:
        return
    print(
        f"credential proxy: credentials for {target.context_name} fetched; this profile task's kubectl and "
        "gcloud calls already target its cluster, so there is nothing to export.",
        file=sys.stderr,
    )


_GET_CREDENTIALS_WORDS = ("container", "clusters", "get-credentials")

# gcloud's short flags that take a value: the single-dash members of
# `command_policy._GCLOUD_FLAGS_WITH_VALUE`, which the shim can't import. A
# test holds the two equal, so a short valued flag added there fails it.
_GCLOUD_SHORT_FLAGS_WITH_VALUE = frozenset({"-z"})
_GCLOUD_RELEASE_TRACKS = frozenset({"alpha", "beta"})


def is_get_credentials(argv: list[str]) -> bool:
    """Is this the one command that legitimately authors a kubeconfig?

    Read the way the broker reads it: the command words past gcloud's flags, so
    a flag between the words (`container clusters --location l get-credentials`)
    is the same command. The shim can't import the broker's policy, so this walk
    doesn't know every flag's arity. A short valued flag (`-z`) always takes the
    next token. A bare `--flag` takes the next token as its value unless that
    token is one of the command words, and none of the three is a plausible
    flag value.
    """
    if not argv or argv[0] != "gcloud":
        return False
    words: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        if token in _GCLOUD_SHORT_FLAGS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            # Known and harmless: a bare `--flag` never takes one of the three
            # command words as its value, so `clusters --format get-credentials
            # list` is get-credentials here but not to the broker, which knows
            # `--format` takes a value. The shim then asks for a file the broker
            # doesn't send and nothing lands. The parity test lists it.
            if (
                token.startswith("--")
                and "=" not in token
                and index + 1 < len(argv)
                and not argv[index + 1].startswith("-")
                and argv[index + 1] not in _GET_CREDENTIALS_WORDS
            ):
                index += 1
            index += 1
            continue
        words.append(token)
        index += 1
    if words[:1] and words[0] in _GCLOUD_RELEASE_TRACKS:
        words = words[1:]
    return words[:3] == list(_GET_CREDENTIALS_WORDS)


def get_credentials_local_target(argv: list[str]) -> ClusterTarget | None:
    """The cluster a get-credentials names in full, or None when the shim should not answer it.

    Read from `argv` with `--kubeconfig` already taken off: the command words,
    one positional cluster name, exactly one location (as `--location`,
    `--zone`, `-z` or `--region`; `--zone` and `--location` for the same zone
    name the same context) and exactly one `--project`. Any other flag, a second
    positional, a missing part, or a second location or project flag is None,
    and the broker answers the call as before (gcloud refuses two location
    flags as a usage error).
    """
    if not is_get_credentials(argv):
        return None
    values: dict[str, list[str]] = {}
    positionals: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return None
        if token.startswith("-"):
            name, eq, value = token.partition("=")
            if name in GET_CREDENTIALS_LOCAL_BARE_FLAGS and not eq:
                index += 1
                continue
            if name not in GET_CREDENTIALS_LOCAL_VALUED_FLAGS:
                return None
            if not eq:
                if index + 1 >= len(argv):
                    return None
                value = argv[index + 1]
                index += 1
            key = "location" if name in GET_CREDENTIALS_LOCAL_LOCATION_FLAGS else name
            values.setdefault(key, []).append(value)
            index += 1
            continue
        positionals.append(token)
        index += 1
    if positionals[:1] and positionals[0] in _GCLOUD_RELEASE_TRACKS:
        positionals = positionals[1:]
    if positionals[:3] != list(_GET_CREDENTIALS_WORDS) or len(positionals) != len(_GET_CREDENTIALS_WORDS) + 1:
        return None
    locations = values.get("location", [])
    projects = values.get("--project", [])
    if len(locations) != 1 or len(projects) != 1:
        return None
    target = ClusterTarget(project=projects[0], location=locations[0], cluster=positionals[-1])
    if not all(_GKE_CONTEXT_COMPONENT.fullmatch(part) for part in (target.project, target.location, target.cluster)):
        return None
    return target


def answer_get_credentials_locally(
    argv: list[str], destination: Path | None, implicit_destination: bool, endpoint: str
) -> int | None:
    """Answer a get-credentials without the broker when the kube-api proxy is in use.

    The kubeconfig the shim writes after a broker call is built from the
    context name alone (`rewrite_kubeconfig_for_kube_proxy`), so with
    `KUBE_PROXY_URL` set the broker's gcloud run adds nothing but a check that
    the cluster exists and is running. That check is one relay read here
    instead. Returns 0 with the kubeconfig written as after a broker call.
    Returns None, and the caller sends the command to the broker as before,
    when the kube-api proxy is not in use, the argv does not name the cluster
    in full (`get_credentials_local_target`), or the relay answers anything but
    a running cluster or cannot be reached. A 404 is one of those: gcloud's own
    error names the cluster and, for a wrong location, the right one. So is
    the broker's refusal of an excluded cluster (GET_CREDENTIALS_CHECK_HEADER):
    the broker's get-credentials then refuses it with its own message.

    The check is one attempt at `endpoint`, the broker `execute` sends the
    command to: no retry of a busy or draining refusal and no fallback of its
    own, so it costs at most GET_CREDENTIALS_CHECK_TIMEOUT_SECONDS before
    `execute`'s own retries and fallback take the call.
    """
    if not os.environ.get("KUBE_PROXY_URL", "").strip():
        return None
    target = get_credentials_local_target(argv)
    if target is None:
        return None
    url = GET_CREDENTIALS_CHECK_URL.format(project=target.project, location=target.location, cluster=target.cluster)
    try:
        response = ApiSession(endpoint)._get_once(
            url, timeout=GET_CREDENTIALS_CHECK_TIMEOUT_SECONDS, headers={GET_CREDENTIALS_CHECK_HEADER: "1"}
        )
        if response.status_code != http.HTTPStatus.OK:
            return None
        cluster = response.json()
    except Exception:  # noqa: BLE001 - any failure here is the broker's call to make
        return None
    # gcloud warns about a cluster that is not running, and fails on one with
    # no endpoint yet; the broker's gcloud prints those.
    if not isinstance(cluster, dict) or cluster.get("status") != GET_CREDENTIALS_RUNNING_STATUS:
        return None
    merge_context_into_default_kubeconfig(target.context_name)
    generated = build_kube_proxy_kubeconfig(target.context_name)
    print(GET_CREDENTIALS_FETCHING_LINE, file=sys.stderr)
    print(GET_CREDENTIALS_GENERATED_LINE.format(cluster=target.cluster), file=sys.stderr)
    if implicit_destination:
        land_implicit_kubeconfig(generated)
    if destination is not None:
        try:
            _replace_file(destination, generated)
        except OSError as exc:
            print(f"credential proxy: could not write {destination}: {exc}", file=sys.stderr)
            return 1
    return 0


def get_credentials_destination(argv: list[str]) -> tuple[list[str], Path | None]:
    """Take the `--kubeconfig` off a get-credentials argv, keeping where it pointed.

    This one command writes a kubeconfig rather than reading one, so the flag
    cannot be resolved to a context name — the file does not exist yet, and the
    cluster it will name is in the rest of the argv. The path also cannot be
    forwarded: it is a path in this pod, and gcloud runs in the broker's. So the
    flag comes off, the broker returns the file it generated, and `execute`
    writes it here.

    Falls back to `$KUBECONFIG`, which is where a Cluster Agent profile's pin
    lives when the caller did not name one; not in task mode, where KUBECONFIG
    is not honoured (`ignored_task_environment`).
    """
    stripped: list[str] = []
    destination: str | None = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--kubeconfig" and index + 1 < len(argv):
            destination = argv[index + 1]
            index += 2
            continue
        if argument.startswith("--kubeconfig="):
            destination = argument.split("=", 1)[1]
            index += 1
            continue
        stripped.append(argument)
        index += 1
    if destination is None and not task_mode():
        env_kc = os.environ.get("KUBECONFIG", "").strip()
        if env_kc and not (
            os.environ.get("KUBE_PROXY_URL", "").strip()
            and _is_default_sandbox_kubeconfig(env_kc)
        ):
            destination = env_kc
    return stripped, Path(destination) if destination else None


def _refusal_payload(exc: urllib.error.HTTPError) -> dict | None:
    """The JSON body of a refusal, or None when it has none.

    The proxy's own errors are JSON, but the error can also come from whatever
    sits between shim and proxy: an Envoy restarting mid-request answers 503
    with an HTML body. The body is read once and kept on the error, because
    the retry decision and the message printed after it both need it; a body
    that is not a JSON object counts as none. `exc.shim_dropped` says the
    connection closed while the body was read. The error is closed here, on
    every way out: nothing reads the response again, and the status and the
    headers stay on it.

    A top-level value that puts the refusal over MAX_VCS_BODY_DEPTH gets a text
    in its place (`_refusal_without_deep_parts`). The parser takes a deeper body
    than `str` and `print` do, and each reader of a refusal prints its values.
    """
    exc.shim_dropped = False
    exc.shim_payload = None
    try:
        payload = json.load(exc)
    except (ValueError, TypeError, RecursionError):
        # RecursionError: a body nested deeper than the parser goes.
        return None
    except (http.client.HTTPException, OSError):
        # The connection closed while the refusal's body was read.
        exc.shim_dropped = True
        return None
    finally:
        exc.close()
    exc.shim_payload = _refusal_without_deep_parts(payload, exc.code) if isinstance(payload, dict) else None
    return exc.shim_payload


def _unusable_exec_answer(payload: dict) -> str | None:
    """What is wrong with the fields of a 2xx answer from `/v1/exec`, or None.

    `stdout` and `stderr` are strings when present, and `kubeconfig` is a string
    or null. `exitCode` is an integer and is not optional: without it the
    command's result is not known. A boolean is not an integer here, and a
    number in a string is not one either. An integer that is no command's
    result, above the greatest status or below the least signal's number, is
    not usable either.
    """
    for field in _EXEC_OUTPUT_FIELDS:
        if field in payload and not isinstance(payload[field], str):
            return _FIELD_NOT_A_STRING_TEXT.format(field=field, type=type(payload[field]).__name__)
    kubeconfig = payload.get(_EXEC_KUBECONFIG_FIELD)
    if kubeconfig is not None and not isinstance(kubeconfig, str):
        return _FIELD_NOT_A_STRING_TEXT.format(field=_EXEC_KUBECONFIG_FIELD, type=type(kubeconfig).__name__)
    if _EXEC_EXIT_CODE_FIELD not in payload:
        return _NO_EXIT_CODE_TEXT
    exit_code = payload[_EXEC_EXIT_CODE_FIELD]
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return _EXIT_CODE_NOT_AN_INTEGER_TEXT.format(type=type(exit_code).__name__)
    if not _MIN_EXIT_CODE <= exit_code <= _MAX_EXIT_STATUS:
        return _EXIT_CODE_OUT_OF_RANGE_TEXT
    return None


def _exit_status(exit_code: int) -> int:
    """The status to exit with for a command's return code from the broker.

    A negative code is the number of the signal that ended the command; the
    status is 128 plus that number, as a shell reports it.
    """
    return _SIGNAL_EXIT_BASE - exit_code if exit_code < 0 else exit_code


def record_write_refusal(status: int, payload, route: str, verb_words) -> None:
    """Append the worker's record of a definitive write refusal, when this answer is one.

    Only a 403 whose body carries the broker's ``write_refusal`` mark as true
    is recorded; every other answer, and a call with REFUSAL_FILE_ENV unset,
    writes nothing. ``verb_words`` is the verb the broker resolved (an exec
    refusal's ``verb``). Never
    raises: the record is the worker's signal, not the call's outcome.
    """
    if status != 403 or not isinstance(payload, dict) or payload.get(WRITE_REFUSAL_KEY) is not True:
        return
    rule = payload.get(REFUSAL_RECORD_RULE)
    if not isinstance(rule, str) or not _REFUSAL_RULE_RE.match(rule):
        return
    _append_refusal_record(rule, verb_words, route)


def record_kubectl_write_attempt(argv: list[str], exit_code: int) -> None:
    """Record a session pod's kubectl call that tried a change and failed.

    A session pod's kubectl reaches the cluster through kube-proxy, not the
    broker, so no broker answer marks its refusal. The kubectl wrapper calls
    this after a non-zero exit. The record is written when KUBE_PROXY_MODE_ENV,
    which the gateway sets from the mode the operator gives kube-proxy, is a
    read-only mode and the argv tries a change that mode refuses
    (command_policy.kubectl_write_attempt). It reads the argv and the exit
    code, never what kubectl printed, and a failure before kubectl reached the
    proxy records the same: a change was attempted and not made. Never raises.
    """
    if exit_code == 0 or not os.environ.get(REFUSAL_FILE_ENV, "").strip():
        return
    try:
        import command_policy  # shipped beside this module in the worker image
    except ImportError:
        return
    verb = command_policy.kubectl_write_attempt(argv, os.environ.get(KUBE_PROXY_MODE_ENV, "").strip())
    if verb is None:
        return
    _append_refusal_record(WRITE_ATTEMPT_RULE, list(verb), KUBE_PROXY_ROUTE)


def _append_refusal_record(rule: str, verb_words, route: str) -> None:
    """Append one write-refusal record to the file REFUSAL_FILE_ENV names, if set."""
    target = os.environ.get(REFUSAL_FILE_ENV, "").strip()
    if not target:
        return
    words = verb_words if isinstance(verb_words, list) else []
    verb = " ".join(_VERB_WORD_SAFE_RE.sub("_", str(w))[:_VERB_WORD_MAX] for w in words[:_REFUSAL_VERB_WORDS] if str(w))
    record = {
        WRITE_REFUSAL_KEY: True,
        REFUSAL_RECORD_RULE: rule,
        REFUSAL_RECORD_VERB: verb,
        REFUSAL_RECORD_ROUTE: _VERB_WORD_SAFE_RE.sub("_", route)[:_VERB_WORD_MAX],
        REFUSAL_RECORD_TIME: time.strftime(REFUSAL_RECORD_TIME_FORMAT, time.gmtime()),
    }
    task = os.environ.get(REFUSAL_TASK_ENV, "").strip()
    if task and CALLER_LABEL_RE.match(task):
        record[REFUSAL_RECORD_TASK] = task
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    # No O_CREAT: the adapter makes the file. O_NONBLOCK so a FIFO put in its
    # place cannot hold the call; the fstat below then refuses anything but a
    # regular file.
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        print(f"credential proxy: the write refusal was not recorded ({exc.strerror or type(exc).__name__})", file=sys.stderr)
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            print("credential proxy: the write refusal was not recorded (not a regular file)", file=sys.stderr)
            return
        # One write of one short line: an O_APPEND write lands whole.
        os.write(fd, line)
    except OSError as exc:
        print(f"credential proxy: the write refusal was not recorded ({exc.strerror or type(exc).__name__})", file=sys.stderr)
    finally:
        os.close(fd)


def _exec_refused(exc: urllib.error.HTTPError, payload: dict | None) -> int:
    """Print a refusal from `/v1/exec` the way the agent reads it, and its exit code."""
    if payload is None:
        # A traceback here would turn a transient sidecar blip into a shim
        # crash the agent cannot read.
        print(
            f"credential proxy error (HTTP {exc.code}): {NON_JSON_RESPONSE_TEXT}",
            file=sys.stderr,
        )
        return 1
    # The far side composes the payload: a `code` that is not a string (a
    # list, an object) is not a refusal code, and must not raise out of here.
    if isinstance(payload.get("code"), str) and payload["code"] in POLICY_BLOCKED_CODES:
        record_write_refusal(exc.code, payload, EXEC_ROUTE, payload.get("verb"))
        print(
            payload.get("message", "Command blocked for security reasons."),
            file=sys.stderr,
        )
        print(f"policy rule: {payload.get('rule', 'unknown')}", file=sys.stderr)
        # The verb the policy resolved and whether this was a local render:
        # the agent runtime's refusal stop reads these two lines.
        if isinstance(payload.get("verb"), list) and payload["verb"]:
            # Each word is an argv token the agent chose; keep it to one line
            # and to the characters a verb or resource name is made of. A
            # `verb` that is not a list has no words (a text would give its
            # letters, one for each word).
            words = (_VERB_WORD_SAFE_RE.sub("_", str(w))[:_VERB_WORD_MAX] for w in payload["verb"])
            print(f"policy verb: {' '.join(words)}", file=sys.stderr)
        if payload.get("local_render"):
            print("policy render: local", file=sys.stderr)
        return EXIT_SECURITY_POLICY_BLOCKED
    print(payload.get("error", str(exc)), file=sys.stderr)
    return 1


def retry_delay_seconds(attempt: int, retry_after: str | None = None, rng=random) -> float:
    """How long to wait before retry number `attempt` (counted from 0).

    `retry_after` is the reply's `Retry-After` header when it sent one; a
    numeric value (seconds) is the base, held between RETRY_BACKOFF_BASE_SECONDS
    (so a header of 0 or less still waits, and jittered) and
    RETRY_AFTER_CAP_SECONDS, and anything else (an HTTP date, garbage, nothing)
    falls back to the
    exponential base, RETRY_BACKOFF_BASE_SECONDS grown RETRY_BACKOFF_GROWTH-fold
    per retry and capped at RETRY_BACKOFF_CAP_SECONDS. The wait is uniform in
    [base/2, base]. `rng` is anything with `uniform(a, b)`, injectable so a
    test can pin the draw.
    """
    base = None
    if retry_after is not None:
        try:
            value = float(retry_after.strip())
        except (ValueError, AttributeError):
            value = None
        if value is not None and math.isfinite(value):
            base = min(max(value, RETRY_BACKOFF_BASE_SECONDS), RETRY_AFTER_CAP_SECONDS)
    if base is None:
        base = min(
            RETRY_BACKOFF_BASE_SECONDS * (RETRY_BACKOFF_GROWTH ** attempt), RETRY_BACKOFF_CAP_SECONDS
        )
    return rng.uniform(RETRY_JITTER_FLOOR * base, base)


def retry_deadline_seconds(environ=os.environ) -> float:
    """The deadline the retries keep to: the caller's, when it named a shorter one.

    RETRY_DEADLINE_ENV carries the caller's bound in seconds from the first
    send. It can only shorten RETRY_DEADLINE_SECONDS, and a value that is not
    a finite non-negative number is ignored.
    """
    raw = environ.get(RETRY_DEADLINE_ENV, "").strip()
    if not raw:
        return RETRY_DEADLINE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return RETRY_DEADLINE_SECONDS
    if not math.isfinite(value) or value < 0:
        return RETRY_DEADLINE_SECONDS
    return min(value, RETRY_DEADLINE_SECONDS)


def retry_wait(
    attempts_made: int,
    started: float,
    retry_after: str | None = None,
    rng=random,
    clock=time.monotonic,
    deadline: float = RETRY_DEADLINE_SECONDS,
) -> float | None:
    """The wait before one more attempt, or None when the retries are spent.

    `attempts_made` counts the attempts already refused (from 1); `started` is
    the clock reading at the first send. Spent means either the count
    (CREDENTIAL_PROXY_CLIENT_MAX_RETRIES retries) or the deadline: a retry
    whose wait, plus the admission wait the broker may hold it for, would end
    past `deadline` seconds from the first send is not started, so the
    throttled line, not a tool timeout, is what the caller reads. `deadline`
    is RETRY_DEADLINE_SECONDS, or what `retry_deadline_seconds` read from the
    caller.
    """
    if attempts_made > CREDENTIAL_PROXY_CLIENT_MAX_RETRIES:
        return None
    delay = retry_delay_seconds(attempts_made - 1, retry_after, rng)
    if clock() - started + delay + BROKER_ADMISSION_WAIT_SECONDS > deadline:
        return None
    return delay


def busy_reply_retryable(payload) -> bool:
    """Whether a JSON refusal from the broker is an admission refusal worth retrying.

    Admission is decided before the command starts, so a retry never runs it
    twice. A busy reply under a retryable bound (RETRYABLE_BUSY_BOUNDS), or
    with no bound at all (a broker that predates the field), is; one at the
    session or scope cap is the caller's own limit and is reported instead,
    and so is a bound of any shape but a string (a list, a number): the shim
    retries only what it understands. The 429 under memory pressure is
    retried whatever it carries.
    """
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if code == RESOURCE_PRESSURE_CODE:
        return True
    if code != BUSY_CODE:
        return False
    bound = payload.get(BUSY_BOUND_KEY)
    return bound is None or (isinstance(bound, str) and bound in RETRYABLE_BUSY_BOUNDS)


def exec_refusal_retryable(status: int, headers, payload) -> bool:
    """Whether `/v1/exec` answered a refusal made before the command ran.

    Any 429 is one. A 503 is one when its JSON body is a retryable busy reply
    (`busy_reply_retryable`), or when it has no JSON body and Envoy marked it
    as answered under overload, so nothing was forwarded. A bare non-JSON 503
    is not: Envoy also answers 503 when the upstream reset after the request
    was forwarded, and the command may have run.
    """
    if status == http.HTTPStatus.TOO_MANY_REQUESTS:
        return True
    if status != http.HTTPStatus.SERVICE_UNAVAILABLE:
        return False
    if payload is not None:
        return busy_reply_retryable(payload)
    return headers is not None and ENVOY_OVERLOADED_HEADER in headers


def vcs_refusal_retryable(status: int, headers, payload) -> bool:
    """Whether a `/v1/vcs/` refusal ran nothing, so `vcs_call` may send it again.

    Stricter than `exec_refusal_retryable`, because a vcs verb can write to a
    forge and a busy answer can come from inside its route: only a broker
    admission refusal that says it came before admission
    (BUSY_BEFORE_ADMISSION_KEY) and is otherwise one the shim retries
    (`busy_reply_retryable`), or a 503 Envoy answered itself under overload
    without forwarding. Not a forge's own 429 or 503, which the broker relays
    after the forge was called, and not a busy answer from an older broker
    that does not say when it refused.
    """
    if payload is None:
        return (
            status == http.HTTPStatus.SERVICE_UNAVAILABLE
            and headers is not None
            and ENVOY_OVERLOADED_HEADER in headers
        )
    if status not in (http.HTTPStatus.TOO_MANY_REQUESTS, http.HTTPStatus.SERVICE_UNAVAILABLE):
        return False
    return (
        isinstance(payload, dict)
        and payload.get(BUSY_BEFORE_ADMISSION_KEY) is True
        and busy_reply_retryable(payload)
    )


def refusal_text(payload) -> str:
    """The broker's own words for a refusal, for the stderr line."""
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("message") or NON_JSON_RESPONSE_TEXT)
    return NON_JSON_RESPONSE_TEXT


def report_retry(status: int, text: str, attempts_made: int, delay: float) -> None:
    """One stderr line per wait, so the agent sees the shim is waiting, not hung.

    The attempt is numbered without a total: the deadline, not the count, ends
    a run of waited refusals, so "of 5" would promise attempts that cannot
    start.
    """
    next_attempt = attempts_made + 1
    print(
        f"credential proxy busy (HTTP {status}): {text}; retrying in {delay:.1f}s (attempt {next_attempt})",
        file=sys.stderr,
    )


def report_throttled(status: int, text: str, attempts_made: int) -> None:
    """The line the agent reads when the retries are spent, by count or by deadline."""
    noun = "attempt" if attempts_made == 1 else "attempts"
    print(
        f"credential proxy throttled: gave up after {attempts_made} {noun} (HTTP {status}): {text}",
        file=sys.stderr,
    )


def execute(
    endpoint: str,
    argv: list[str],
    stdin: str | None = None,
    *,
    fallback: str | None = None,
    sleep_fn=time.sleep,
    rng=random,
    clock=time.monotonic,
) -> int:
    # No `cwd`. The broker resolves a path against its own filesystem, and it
    # has no view of this one — so a directory sent from here would name either
    # nothing or, worse, a same-named directory of the broker's. Every command
    # runs at the broker's own workspace root instead.
    #
    # KUBECONFIG is the one thing an agent legitimately steers with a path:
    # Cluster Agent profiles pin themselves to a target cluster through it (see
    # agents/cluster/config.yaml). It survives the pod boundary by being
    # resolved to a context name here rather than forwarded as a path.
    # Whitespace is stripped because profile .env files routinely carry a
    # trailing newline.
    #
    # With neither set, a get-credentials still asks for its file back and a
    # kubectl may still name a cluster: see `land_implicit_kubeconfig` and
    # `implicit_kubectl_context` for why, and for what keeps #1852's host
    # default for everything that names nothing.
    #
    # A profile task (`task_mode`) honours none of that environment: the
    # cluster and project are the task credential's, and the broker holds the
    # call to them. Only an explicit `--kubeconfig` in argv is still resolved,
    # and the broker refuses it unless it names the task's cluster.
    destination: Path | None = None
    implicit_destination = False
    in_task = task_mode()
    if in_task:
        ignored = ignored_task_environment()
        if ignored:
            print(
                f"credential proxy: {', '.join(ignored)} not honoured for a profile task; "
                "the task's cluster and project are used",
                file=sys.stderr,
            )
    get_credentials = False
    try:
        context = ""
        if is_get_credentials(argv):
            get_credentials = True
            argv, destination = get_credentials_destination(argv)
            implicit_destination = destination is None
        elif argv and argv[0] in KUBECONFIG_AWARE:
            flag_pinned = names_kubeconfig_flag(argv)
            argv = resolve_kubeconfig_flags(argv)
            kubeconfig = "" if in_task else os.environ.get("KUBECONFIG", "").strip()
            if kubeconfig and not (
                os.environ.get("KUBE_PROXY_URL", "").strip()
                and _is_default_sandbox_kubeconfig(kubeconfig)
            ):
                context = kubeconfig_context(kubeconfig)
            elif argv[0] == "kubectl" and not flag_pinned and not in_task:
                context = implicit_kubectl_context(argv)
    except KubeconfigUnreadable as exc:
        print(f"credential proxy: {exc}", file=sys.stderr)
        return 1
    # With the kube-api proxy in use, a get-credentials that names its cluster
    # in full needs no broker call; a profile task's stays with the broker,
    # which holds the task's cluster.
    if get_credentials and not in_task:
        answered = answer_get_credentials_locally(argv, destination, implicit_destination, endpoint)
        if answered is not None:
            return answered

    request_payload = {
        "requestId": str(uuid.uuid4()),
        "argv": argv,
    }
    if context:
        request_payload["kubeconfigContext"] = context
    session_label = default_caller_label()
    if session_label:
        request_payload[SESSION_ID_FIELD] = session_label
    if destination is not None or implicit_destination:
        request_payload["wantsKubeconfig"] = True
    if stdin is not None:
        request_payload["stdin"] = stdin
    body = json.dumps(
        request_payload,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        headers.update(authorization_headers())
        headers.update(task_credential_headers(EXEC_ROUTE))
    except TokenUnavailable as exc:
        # Sending the request anyway would earn an undifferentiated 401 and
        # hide the real fault, which is a broken token projection.
        print(f"credential proxy token unavailable: {exc}", file=sys.stderr)
        return 1
    targets = [endpoint] + ([fallback] if fallback and fallback != endpoint else [])
    # The body, and the requestId in it, is built once: a fallback or a retry
    # re-sends the same request, so the broker's audit shows one request
    # refused and then admitted rather than two. The Request object is new per
    # attempt, and so is the connection under it, which is what lets another
    # replica admit it. `attempts_made` counts the refusals the schedule has
    # answered; a fallback is not one of them, since nothing reached a broker.
    attempts_made = 0
    started = clock()
    deadline = retry_deadline_seconds()
    while True:
        try:
            request = urllib.request.Request(
                targets[0].rstrip("/") + EXEC_ROUTE,
                data=body,
                headers=headers,
                method="POST",
            )
        except ValueError as exc:
            # A URL with no scheme, such as a bare Service name: nothing was
            # sent, so a fallback is safe.
            if len(targets) > 1:
                print(EXEC_FALLBACK_NOTICE, file=sys.stderr)
                targets = targets[1:]
                continue
            print(f"credential proxy unavailable: {exc}", file=sys.stderr)
            return 1
        try:
            with open_broker_request(request) as response:
                # Only the parse and the answer's shape are guarded: a read
                # that breaks is a dropped connection, below. RecursionError:
                # a body nested deeper than the parser goes.
                try:
                    payload = json.load(response)
                except (ValueError, RecursionError):
                    print(
                        _UNREADABLE_ANSWER_LINE.format(status=response.status, what=NON_JSON_RESPONSE_TEXT),
                        file=sys.stderr,
                    )
                    return 1
                if not isinstance(payload, dict):
                    shape = _NOT_AN_OBJECT_TEXT.format(type=type(payload).__name__)
                    print(_UNREADABLE_ANSWER_LINE.format(status=response.status, what=shape), file=sys.stderr)
                    return 1
                # Before anything of the answer is written.
                unusable = _unusable_exec_answer(payload)
                if unusable is not None:
                    print(_UNREADABLE_ANSWER_LINE.format(status=response.status, what=unusable), file=sys.stderr)
                    return 1
            break
        except urllib.error.HTTPError as exc:
            payload = _refusal_payload(exc)
            if exc.shim_dropped:
                print(_DROPPED_LINE.format(cause="while the refusal was read"), file=sys.stderr)
                return 1
            if exec_refusal_retryable(exc.code, exc.headers, payload):
                # Refused before the command ran, so sending it again runs
                # nothing twice. The wait is what lets the broker that refused
                # it admit it, and the new connection is what lets another
                # replica; so the schedule runs whether or not exec routing is
                # on. Every disconnect after the send, and every other status,
                # falls through: the command may have run.
                attempts_made += 1
                text = refusal_text(payload)
                delay = retry_wait(
                    attempts_made, started, exc.headers.get(RETRY_AFTER_HEADER), rng, clock, deadline=deadline
                )
                if delay is None:
                    report_throttled(exc.code, text, attempts_made)
                    return 1
                report_retry(exc.code, text, attempts_made, delay)
                sleep_fn(delay)
                continue
            return _exec_refused(exc, payload)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, BrokerUnreachable) and len(targets) > 1:
                print(EXEC_FALLBACK_NOTICE, file=sys.stderr)
                targets = targets[1:]
                continue
            print(f"credential proxy unavailable: {exc.reason}", file=sys.stderr)
            return 1
        except (http.client.InvalidURL, UnicodeEncodeError) as exc:
            # Raised while the connection, the request line or the Host header
            # is built, before a socket opens: nothing was sent, so a fallback
            # is safe. A reply is decoded, never encoded, so a UnicodeEncodeError
            # here is the request's own.
            if len(targets) > 1:
                print(EXEC_FALLBACK_NOTICE, file=sys.stderr)
                targets = targets[1:]
                continue
            print(f"credential proxy unavailable: {exc}", file=sys.stderr)
            return 1
        except (http.client.HTTPException, OSError) as exc:
            # The request was sent and the connection dropped: the command may
            # have run, so it is never sent again.
            print(_DROPPED_LINE.format(cause=f"after the request was sent ({exc!r})"), file=sys.stderr)
            return 1

    sys.stdout.write(payload.get("stdout", ""))
    sys.stderr.write(payload.get("stderr", ""))
    truncated = bool(payload.get("truncated"))
    # The exit code is for a cut payload. A broker older than the field says
    # only that a stream was cut, and that is read as the payload.
    stdout_cut = bool(payload.get("stdoutTruncated", truncated))
    if truncated:
        print(_truncated_marker(payload, stdout_cut), file=sys.stderr)
    generated = payload.get("kubeconfig")
    if generated and os.environ.get("KUBE_PROXY_URL", "").strip():
        generated = rewrite_kubeconfig_for_kube_proxy(generated)
    if implicit_destination and generated:
        if in_task:
            report_task_kubeconfig(generated)
        else:
            land_implicit_kubeconfig(generated)
    if destination is not None and generated:
        # gcloud's own output, written where gcloud would have written it had it
        # run here. The agent never runs a command against this file — a later
        # kubectl names the cluster and the broker regenerates from that name —
        # so this is the visible pin `cluster_agent_profile.py` records and the
        # Cluster Agent preflight stats, and nothing more.
        try:
            _replace_file(destination, generated)
        except OSError as exc:
            print(f"credential proxy: could not write {destination}: {exc}", file=sys.stderr)
            return 1
    if stdout_cut:
        # Whatever the command itself exited: a cut payload is not the
        # command's answer, and exit 0 on one is what a caller reads as success.
        return EXIT_OUTPUT_TRUNCATED
    return _exit_status(payload[_EXEC_EXIT_CODE_FIELD])


def _truncated_marker(payload: dict, stdout_cut: bool) -> str:
    """The stderr line for a cut reply: the bare marker when the broker gave no
    counts (one older than the fields), the counts after it otherwise, and the
    stderr-only form when the payload is whole and the cut fell on stderr."""
    if not stdout_cut:
        return OUTPUT_TRUNCATED_MARKER + OUTPUT_TRUNCATED_STDERR_ONLY
    kept, limit = payload.get("bytesWritten"), payload.get("limit")
    if isinstance(kept, int) and isinstance(limit, int) and not isinstance(kept, bool) and not isinstance(limit, bool):
        return OUTPUT_TRUNCATED_MARKER + OUTPUT_TRUNCATED_COUNTS.format(limit=limit, kept=kept, code=EXIT_OUTPUT_TRUNCATED)
    return OUTPUT_TRUNCATED_MARKER


class WorkspaceUnavailable(RuntimeError):
    """The broker has no version control built (`VCS_UNAVAILABLE`)."""


class BrokerDisconnected(RuntimeError):
    """The connection broke after the request was on the wire.

    Distinct from `urllib.error.URLError`, which `urllib` raises for a send
    that never landed. Only `h.request(...)` is wrapped in it; `getresponse()`
    and the body read after it are not, and `BrokerConnection.connect` clears
    the socket timeout once connected — so a broker evicted or rolled
    mid-request raises `http.client.RemoteDisconnected`, `IncompleteRead` or a
    bare `ConnectionResetError` out of the read, none of them a `URLError`.

    Translated here rather than caught by the caller because this module owns
    the transport: `vcs_client` must import no network client of its own, and
    `test_vcs.test_the_only_network_client_is_the_broker` holds it to that.

    The two are worth telling apart downstream. A send that never landed
    changed nothing; a connection that broke mid-answer may have been acted on
    at the far end, and a retry has to be written knowing that.
    """


class VcsAnswerTooDeep(ValueError):
    """A 2xx `/v1/vcs/*` body that nests deeper than MAX_VCS_BODY_DEPTH, or deeper than the parser goes.

    The request reached the broker, so the verb can have taken effect. A
    ValueError, as for a body that is not JSON, so a caller with an arm for
    that one still takes this one.
    """


def nests_deeper_than(document: object, bound: int) -> bool:
    """Whether `document` nests lists and objects more than `bound` levels. A loop, with no recursion."""
    layer = [document]
    for _ in range(bound):
        layer = [
            child
            for node in layer
            if isinstance(node, (dict, list))
            for child in (node.values() if isinstance(node, dict) else node)
        ]
        if not layer:
            return False
    return any(isinstance(node, (dict, list)) for node in layer)


class WorkspaceRequestError(RuntimeError):
    """The broker refused. `status` and `payload` carry its answer verbatim."""

    def __init__(self, status: int, payload: dict) -> None:
        # Two spellings of the same field: the vcs routes answer `{error,
        # code}`, and a broker policy refusal answers `{status, code,
        # message}`. Reading
        # only one of them would render half the broker's refusals as the
        # generic fallback below, which is the sentence that tells a caller
        # nothing.
        super().__init__(
            payload.get("message")
            or payload.get("error")
            or f"broker request failed ({status})"
        )
        self.status = status
        self.payload = payload
        # What `vcs_call` decides a retry on: the reply's headers (Retry-After,
        # Envoy's overload mark) and its JSON body, or None when it sent none.
        self.headers: object = {}
        self.json_body: object = None


def default_caller_label() -> str:
    """The card, scheduled run or session id this process runs under, or ""
    when it has none.

    Read at call time, like `session_lease` in the skill, because the agent
    runs each command in a fresh process and the identity lives in its
    environment. A value the broker's grammar would refuse is dropped here:
    the key exists so the broker can count a run's commands, and it must
    never be the reason a command fails.
    """
    for name in CALLER_LABEL_ENV:
        value = os.environ.get(name, "").strip()
        if value and CALLER_LABEL_RE.match(value):
            return value
    return ""


def vcs_call(
    endpoint: str,
    verb: str,
    payload: dict,
    *,
    sleep_fn=time.sleep,
    clock=time.monotonic,
    rng=random,
) -> dict:
    """One `POST /v1/vcs/<verb>`, sent again after an admission refusal.

    Every `/v1/vcs/*` call stands alone and holds nothing across a session.
    Public, because the version-control skill is the caller and it lives
    outside this module.

    A refusal the broker makes before it admits the request -- a 503 at a slot,
    budget, background or profile cap, or a 429 under memory pressure, each
    marked as given before admission -- or an Envoy overload 503 ran nothing,
    so it is sent again on the exec shim's schedule (`vcs_refusal_retryable`,
    `retry_wait`) with a line on stderr per wait. A refusal after the request
    reached the forge is never sent again. Without it, a profile worker at its slot
    share could publish a branch and then fail the `proposal-create` that
    follows at once, leaving a branch with no proposal (#6247). Every other
    refusal, and the last admission refusal once the retries are spent, is
    raised as before.
    """
    started = clock()
    deadline = retry_deadline_seconds()
    attempts_made = 0
    while True:
        try:
            return _vcs_call_once(endpoint, verb, payload)
        except WorkspaceRequestError as exc:
            if not vcs_refusal_retryable(exc.status, exc.headers, exc.json_body):
                raise
            attempts_made += 1
            text = refusal_text(exc.payload)
            delay = retry_wait(
                attempts_made, started, exc.headers.get(RETRY_AFTER_HEADER), rng, clock, deadline=deadline
            )
            if delay is None:
                report_throttled(exc.status, text, attempts_made)
                raise
            report_retry(exc.status, text, attempts_made, delay)
            sleep_fn(delay)


def _vcs_call_once(endpoint: str, verb: str, payload: dict) -> dict:
    """One attempt of `vcs_call`."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    # Same credential and same opener as `execute`: these routes spend the
    # broker's forge credential, so a cross-Pod call without the header earns a
    # 401, and the stock opener would put a total socket timeout on a clone
    # that legitimately runs for minutes.
    headers.update(authorization_headers())
    # A profile worker's task credential, as `execute` sends it: the broker
    # refuses a profile caller's version-control verbs without one, and its
    # permit is what admits the writes (#3993). Nothing outside task mode.
    route = f"/v1/vcs/{verb}"
    headers.update(task_credential_headers(route))
    request = urllib.request.Request(
        endpoint.rstrip("/") + route,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with open_broker_request(request) as response:
            try:
                answer = json.load(response)
            except RecursionError as exc:
                # A body nested deeper than the parser goes. A RuntimeError by
                # type, so no clause for a body that is not JSON sees it.
                raise VcsAnswerTooDeep(_VCS_ANSWER_TOO_DEEP_TEXT) from exc
            if nests_deeper_than(answer, MAX_VCS_BODY_DEPTH):
                # The parser took it; an encoder or `repr` of a caller may not.
                raise VcsAnswerTooDeep(_VCS_ANSWER_TOO_DEEP_TEXT)
            if not isinstance(answer, dict):
                # Valid JSON, but not an object, and every consumer reads the
                # answer as one. A ValueError, as for a body that is not JSON:
                # `vcs_client.call` has an arm for it. The type's name only,
                # never the body.
                raise ValueError(f"the broker's answer is JSON of type {type(answer).__name__}, not an object")
            return answer
    except urllib.error.HTTPError as exc:
        try:
            answer = json.load(exc)
        except (ValueError, TypeError, RecursionError):  # RecursionError: a body nested too deep to parse
            # The status arrived but the body is not a refusal document. The
            # status is the whole answer.
            raise _vcs_refusal(exc, {"error": f"HTTP {exc.code}"}, None) from exc
        except (http.client.HTTPException, OSError) as broke:
            # The body ran out mid-read instead: an `IncompleteRead`, or a
            # `RemoteDisconnected`, or a reset socket. None of those is a
            # `ValueError`, so the clause above does not see them, and the
            # identical clause at the foot of this `try` cannot either --
            # Python does not offer an exception raised inside an `except`
            # block to that block's siblings. Without this arm it leaves
            # `vcs_call` as a raw `http.client` type, which `vcs_client.call`
            # has no arm for and every consumer is built on it having one.
            raise BrokerDisconnected(f"{type(broke).__name__}: {broke}") from broke
        finally:
            # The body is read, or lost: nothing reads the response again. The
            # status stays on the error.
            exc.close()
        if not isinstance(answer, dict):
            # Valid JSON, but not an object, so no refusal document either.
            # The status is the whole answer, as above.
            raise _vcs_refusal(exc, {"error": f"HTTP {exc.code}"}, None) from exc
        if nests_deeper_than(answer, MAX_VCS_BODY_DEPTH):
            answer = _refusal_without_deep_parts(answer, exc.code)
        if answer.get("code") == "VCS_UNAVAILABLE":
            # A broker too old to have these routes at all. Distinguished from
            # every other refusal because it is the one a caller cannot fix by
            # asking differently.
            raise WorkspaceUnavailable(answer.get("error", "not available")) from exc
        # No write-refusal record: a version-control refusal never stops the
        # task. The permit refusal's answer is the patch the version-control
        # skill tells the agent to give.
        raise _vcs_refusal(exc, answer, answer) from exc
    except urllib.error.URLError:
        # Out untouched: the send never landed, and `vcs_client.call` has an
        # arm that says so. Named before the clause below only because
        # `URLError` is itself an `OSError` and would otherwise be swallowed by
        # it and reported as a connection that broke mid-answer.
        raise
    except (http.client.HTTPException, OSError) as exc:
        raise BrokerDisconnected(f"{type(exc).__name__}: {exc}") from exc


def _refusal_without_deep_parts(refusal: dict, status: int) -> dict:
    """`refusal` with a text in place of each top-level value that puts it over MAX_VCS_BODY_DEPTH.

    The other values stay, so a caller still reads the refusal's `code` and its
    text. The key of a value that is too deep stays too: a reader then sees a
    mark that it does not know, and not a mark that is absent, which can have a
    meaning of its own (a busy refusal with no `bound` is sent again). An
    `error` that is too deep becomes the status.
    """
    trimmed = {}
    for name, value in refusal.items():
        # The refusal object is one level; a value has the rest of the bound.
        if not nests_deeper_than(value, MAX_VCS_BODY_DEPTH - 1):
            trimmed[name] = value
        elif name == _REFUSAL_ERROR_FIELD:
            trimmed[name] = _REFUSAL_STATUS_TEXT.format(status=status)
        else:
            trimmed[name] = _REFUSAL_PART_TOO_DEEP_TEXT
    return trimmed


def _vcs_refusal(exc: urllib.error.HTTPError, payload: dict, json_body: object) -> WorkspaceRequestError:
    """The error `vcs_call` raises for a refusal, with what its retry reads."""
    error = WorkspaceRequestError(exc.code, payload)
    error.headers = exc.headers if exc.headers is not None else {}
    error.json_body = json_body
    return error


class ApiSession:
    """A requests-shaped session whose reads of a Google API go through the broker.

    The sandbox holds no Google credential, so a script that needs a Cloud
    Monitoring read cannot call `monitoring.googleapis.com` itself. This
    rewrites `https://<host>/<path>` onto the broker's `/v1/gcp/<host>/<path>`
    route and attaches the caller token; the broker checks the read against
    `api_policy.API_READ_ROUTES`, forwards it on its own credential, and hands
    back the upstream status, `Content-Type` and body unchanged (or, on a route
    with a projection, a 2xx body cut down to the fields it names). A refusal
    arrives as a 403 whose JSON body carries `rule` and `message`, the same
    shape `/v1/exec` uses.

    The URL a caller writes stays as written, so a collector's evidence label
    still names the Google endpoint it reads, in its commercial form: when the
    caller's environment names a custom universe domain, `relay_url` puts the
    host in that domain. `.get(url, params=, timeout=)` is what a
    `requests.Session` offers, so code written against one runs against this
    unchanged. `requests` is imported here and not at module scope: it is in
    the sandbox image, where this class runs, and nowhere else this module is
    imported needs it. `http` is injectable for tests and for a caller that
    already holds a configured session.
    """

    def __init__(
        self, endpoint: str | None = None, http=None, *, sleep_fn=time.sleep, rng=random, clock=time.monotonic
    ) -> None:
        # A named endpoint is used as given. Otherwise the relay is an exec-tier
        # route, with the stateful broker as the fallback (`exec_endpoints`).
        fallback = None
        if not endpoint:
            endpoint, fallback = exec_endpoints()
        if not endpoint:
            raise RuntimeError(
                f"{BROKER_URL_ENV} is not set; the credential broker is the only "
                "path from here to a Google API"
            )
        self._endpoint = endpoint.rstrip("/")
        self._fallback = fallback.rstrip("/") if fallback else None
        # Fleet collectors share one session across a thread pool.
        self._lock = threading.Lock()
        if http is None:
            import requests

            http = requests.Session()
            # No proxy from the environment, as for the broker opener: through
            # a proxy every connect succeeds, and the fallback would never fire.
            http.trust_env = False
        self._http = http
        self._sleep = sleep_fn
        self._rng = rng
        self._clock = clock

    def relay_url(self, url: str, endpoint: str | None = None) -> str:
        """The broker URL that stands for `url`; the query is kept where it was."""
        parsed = urllib.parse.urlsplit(url)
        host = rewrite_google_api_host(parsed.netloc)
        relayed = f"{endpoint or self._endpoint}{API_RELAY_PREFIX}{host}{parsed.path}"
        if parsed.query:
            relayed += f"?{parsed.query}"
        return relayed

    def get(self, url: str, *, params=None, timeout=None):
        """One relayed GET, sent again on the shim's schedule if the broker itself refuses it.

        Only the broker's own refusal is retried (`busy_reply_retryable`: a
        body whose top-level `code` is its busy or resource-pressure code). The
        relay route takes no admission slot today and answers neither, so this
        loop is dormant until a broker refuses the relay under pressure; it is
        here so the collectors need no change when one does. Each send goes
        through `_get_once`, so a retry falls back with the session. A 429 or 503 the
        upstream Google API returned comes back with its body unchanged, whose
        `code` is nested under `error`, and is handed to the caller as it is:
        the collectors that make hundreds of these calls own their own pacing
        for a quota refusal.
        """
        return self._send(url, params=params, timeout=timeout)

    def post(self, url: str, *, json, timeout=None):
        """One relayed POST of a JSON body, on the same schedule as `get`.

        For a read whose arguments travel in a body, which the broker's table
        names with a body check (Logging `entries:list`). The broker reads the
        body, holds it to that check, and forwards its own copy of it.
        """
        return self._send(url, timeout=timeout, json_body=json)

    def _send(self, url: str, **kwargs):
        attempts_made = 0
        started = self._clock()
        deadline = retry_deadline_seconds()
        while True:
            response = self._get_once(url, **kwargs)
            attempts_made += 1
            if not self._refused_admission(response):
                return response
            retry_after = getattr(response, "headers", {}).get(RETRY_AFTER_HEADER)
            delay = retry_wait(attempts_made, started, retry_after, self._rng, self._clock, deadline=deadline)
            if delay is None:
                return response
            report_retry(response.status_code, refusal_text(self._json_body(response)), attempts_made, delay)
            self._sleep(delay)
            if (self._json_body(response) or {}).get(BUSY_BOUND_KEY) == ADMISSION_BOUND_DRAINING:
                # A draining exec pod keeps answering this on the pooled
                # keep-alive connection to its Envoy, so the retry would go back
                # to it. A new connection reaches a ready replica, or fails over
                # to the stateful broker.
                self._drop_pooled_connections()

    def _drop_pooled_connections(self) -> None:
        close = getattr(self._http, "close", None)
        if close is not None:
            with self._lock:
                close()

    @staticmethod
    def _json_body(response):
        try:
            body = response.json()
        except (ValueError, AttributeError, RecursionError):
            # RecursionError: a body nested deeper than the parser goes.
            return None
        if isinstance(body, dict):
            # The retry line prints the refusal's text: as for a `/v1/exec`
            # refusal, a value that is too deep gets a text in its place.
            return _refusal_without_deep_parts(body, getattr(response, "status_code", None))
        return body

    def _refused_admission(self, response) -> bool:
        status = getattr(response, "status_code", None)
        if status not in (http.HTTPStatus.TOO_MANY_REQUESTS, http.HTTPStatus.SERVICE_UNAVAILABLE):
            return False
        return busy_reply_retryable(self._json_body(response))

    def _get_once(self, url: str, *, params=None, timeout=None, headers=None, json_body=None):
        """One relayed GET, moved to the stateful broker when the exec tier cannot be reached.

        `headers` are sent beside the caller's credential (the get-credentials
        check's GET_CREDENTIALS_CHECK_HEADER). With `json_body` the request is
        a POST of that body, for `post`."""
        caller_timeout = timeout
        started = time.monotonic()
        with self._lock:
            endpoint, exec_attempt = self._endpoint, self._fallback is not None
        if exec_attempt:
            # The exec tier gets the shim's connect bound, as `BrokerConnection`
            # gives it, whatever the caller's timeout; the read keeps the
            # caller's. A missing egress peer hangs the dial rather than
            # refusing it.
            timeout = _exec_connect_bound(timeout)
        try:
            if json_body is not None:
                return self._http.post(
                    self.relay_url(url, endpoint),
                    json=json_body,
                    headers={**authorization_headers(), **(headers or {})},
                    timeout=timeout,
                )
            return self._http.get(
                self.relay_url(url, endpoint),
                params=params,
                headers={**authorization_headers(), **(headers or {})},
                timeout=timeout,
            )
        except _connection_errors() as exc:
            with self._lock:
                if self._endpoint == endpoint and self._fallback is not None:
                    # The first reader to fail moves the session. Every relay
                    # call is a read, so sending it again is safe even when the
                    # error came after the request left, and the rest of the
                    # session stays on the stateful broker: a missing exec
                    # Service would otherwise cost each read its connect bound.
                    self._endpoint, self._fallback = self._fallback, None
                    print(f"{EXEC_FALLBACK_NOTICE} for the Cloud API relay ({exc})", file=sys.stderr)
                moved = self._endpoint != endpoint
            if not moved:
                raise
            # The caller sized its timeout for one attempt; the fallback gets
            # what the exec attempt left of it.
            left = _time_left(caller_timeout, time.monotonic() - started)
            if left is False:
                raise
            return self._get_once(url, params=params, timeout=left, headers=headers, json_body=json_body)


def _exec_connect_bound(timeout):
    """A `requests` timeout whose connect part is at most the shim's connect bound."""
    if isinstance(timeout, tuple):
        connect, read = timeout
    else:
        connect, read = timeout, timeout
    bound = BROKER_CONNECT_TIMEOUT_SECONDS if connect is None else min(connect, BROKER_CONNECT_TIMEOUT_SECONDS)
    return (bound, read)


def _time_left(timeout, spent: float):
    """`timeout` less `spent` seconds of read time, or False when too little is left."""
    if timeout is None:
        return None
    if isinstance(timeout, tuple):
        connect, read = timeout
        if read is None:
            return timeout
        left = read - spent
        return (connect, left) if left >= RELAY_FALLBACK_MIN_SECONDS else False
    left = timeout - spent
    return left if left >= RELAY_FALLBACK_MIN_SECONDS else False


def _connection_errors() -> tuple[type[BaseException], ...]:
    """The errors a `requests` session raises when the relay cannot be reached."""
    try:
        import requests
    except ImportError:
        return (ConnectionError,)
    # The URL errors are raised while the request is prepared, before a byte is
    # sent, so moving to the fallback is as safe as for a connection error.
    return (
        requests.exceptions.ConnectionError,
        requests.exceptions.InvalidURL,
        requests.exceptions.MissingSchema,
        requests.exceptions.InvalidSchema,
        ConnectionError,
    )


def read_stdin_if_requested(argv: list[str]) -> str | None:
    """fd 0, but only for an argv that named `-` as an input file.

    Still `None` when fd 0 is a terminal: an interactive `kubectl apply -f -`
    with nothing piped in would otherwise hang the shim on a read that never
    returns, which reads to the agent as the proxy being down.
    """
    if not reads_stdin(argv):
        return None
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        return sys.stdin.read()
    except (OSError, ValueError):
        return None


def parse_args():
    import argparse

    parser = argparse.ArgumentParser()
    # No default: a named endpoint is used as given, with no exec routing and
    # no fallback, and an unnamed one is resolved by `exec_endpoints`. Read from
    # the environment here, not through `exec_endpoints`, which would write its
    # malformed-URL line even when an endpoint is named.
    parser.add_argument(
        "--endpoint",
        default=None,
        required=not os.getenv(EXEC_URL_ENV, "").strip() and os.getenv(BROKER_URL_ENV) is None,
    )
    parser.add_argument(
        "executable",
        choices=SUPPORTED_EXECUTABLES,
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main():
    """The command line: the names the shim is linked as, the explicit
    form, and `--ensure-kubectl-context`. Ends with SystemExit. The
    images' launcher imports this module, so its precompiled bytecode
    is used, and calls this."""
    if len(sys.argv) >= 3 and sys.argv[1] == "--record-kubectl-write-attempt":
        # The session kubectl wrapper: <exit code> then kubectl's own arguments.
        try:
            code = int(sys.argv[2])
        except ValueError:
            raise SystemExit(0) from None
        record_kubectl_write_attempt(["kubectl", *sys.argv[3:]], code)
        raise SystemExit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--ensure-kubectl-context":
        try:
            ensure_kube_proxy_context_for_argv(["kubectl", *sys.argv[2:]])
        except TokenUnavailable as exc:
            print(f"credential proxy: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        except Exception as exc:
            if hasattr(exc, "message") and hasattr(exc, "rule"):
                print(exc.message, file=sys.stderr)
                raise SystemExit(1) from None
            raise
        raise SystemExit(0)
    invoked_as = os.path.basename(sys.argv[0])
    fallback = None
    if invoked_as in set(SUPPORTED_EXECUTABLES):
        endpoint, fallback = exec_endpoints()
        if endpoint is None:
            print(f"{BROKER_URL_ENV} is not configured", file=sys.stderr)
            raise SystemExit(1)
        argv = [invoked_as, *sys.argv[1:]]
        stdin = read_stdin_if_requested(argv)
    else:
        args = parse_args()
        endpoint = args.endpoint
        if endpoint is None:
            endpoint, fallback = exec_endpoints()
        argv = [args.executable, *args.arguments]
        stdin = read_stdin_if_requested(argv)
    raise SystemExit(
        execute(
            endpoint,
            argv,
            stdin=stdin,
            fallback=fallback,
        )
    )


if __name__ == "__main__":
    main()

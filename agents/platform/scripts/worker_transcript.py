"""The rules that read a worker's Hermes transcript back out of its session store.

Two readers walk the same transcript: the session-kv feed
(`session_kv_server.py`, `GET /v1/tasks/{id}` and `GET /v1/sessions/{id}/tasks`)
serves a card's live worker steps to the chat surfaces, and the bench's in-pod
trajectory read (`bench/kube_agents_bench/worker_trajectory.py`) appends the
worker's tool calls to a run record. Each scrubs the text with the install's
`AuditRedactor` and then with a supplement for the shapes the redactor leaves:
block scalars under `data:`/`stringData:`, a `data`/`stringData` object in
JSON text or in a parsed manifest (by shape, whatever the kind: a ConfigMap's
is blanked as a Secret's, and kubectl's key-sorted JSON cut short before its
`kind` is text), URL userinfo,
`NAME: value` and `NAME=value` lines, the CLI credential flags (`-u user:pass`,
`-p pass`, `--password pass`), the bearer-scheme pair, and the secret-read
command detector that withholds a result instead of scrubbing it. Each reader
used to keep its own copy of the supplement and of the card-to-session mapping
(`task_runs.metadata.worker_session_id`, the dispatcher's prompt), and a fix
landed in one of them: `curl -u admin:hunter2` was masked in the feed and kept
in a bench trajectory. This module is the one copy, as `credential_shapes.py`
is for the credential proxy and the feedback broker. A new shape lands here
and nowhere else.

The module is stdlib only and never loads the redactor itself: every rule
takes it as an argument, so the server passes the one it loaded lazily and
the bench's in-pod script passes the one it loaded by path. The agent image
copies the scripts tree whole, so it ships at
`/opt/defaults/scripts/worker_transcript.py`; the server imports it as a
sibling module and the in-pod script loads it from that path.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

# The markers the redactor leaves, spelled once so a reader grepping a
# transcript finds one marker whichever rule masked the value.
REDACTED_SECRET_MARKER = "[REDACTED_SECRET]"
REDACTED_PRIVATE_KEY_MARKER = "[REDACTED_PRIVATE_KEY]"
# Every marker the redactor and this module write starts with this; a block
# whose every line already carries one is not masked again.
REDACTED_MARKER_PREFIX = "[REDACTED_"
# A marker as a value token: the `NAME: value` rules read it so that a line
# the redactor already masked the first word of is masked to its end.
REDACTED_MARKER_TOKEN_RE_SRC = r"\[REDACTED_[A-Z_]+\]"
# The step kinds whose text takes a rule of its own: a `tool_call` carries a
# shell command the continuation fold applies to, a `tool_result` the
# inline-object rule.
STEP_KIND_TOOL_CALL = "tool_call"
STEP_KIND_TOOL_RESULT = "tool_result"
# The first character of a string that may be a JSON document.
JSON_STRUCTURE_OPENERS = "{["
# The manifest keys whose mapping is blanked wholesale, and the keys that
# mark a document as a Kubernetes manifest. A `data` mapping is blanked by
# shape under any manifest kind (a ConfigMap's as a Secret's): in a parsed
# document when the object holds a marker, in text when the text does
# (kubectl's JSON is key-sorted, so `apiVersion` precedes a `data` the
# output was cut short inside). A `data` map of base64 strings is blanked
# with or without a marker: a Secret filtered to `{"data": {...}}` by `jq`.
# An API response's `data` (`{"status": "success", "data": {"resultType":
# "vector"}}`) and a JSON log line's are neither, and are kept.
MANIFEST_DATA_KEY = "data"
MANIFEST_STRING_DATA_KEY = "stringData"
MANIFEST_MARKER_KEYS = ("apiVersion", "kind")
JSON_MANIFEST_MARKER_RE = re.compile('"(?:' + "|".join(re.escape(k) for k in MANIFEST_MARKER_KEYS) + r')"\s*:')
JSON_STRING_DATA_KEY_QUOTED = '"' + MANIFEST_STRING_DATA_KEY + '"'
# How far the unquoted value after a credential-named key reaches, by what is
# being read. `shell`: a command's word ends at whitespace (`--set
# db.password=x --namespace prod` keeps its flags) and the marker the redactor
# left is not a scheme that takes the next word. `line`: a text line's value
# is the rest of the line (`DB_PASSWORD:  my secret phrase` leaves no word).
# `token`: prose keeps the first-token mask (`Secret: db-credentials is
# missing the key 'password'` is a diagnosis). `scrub_step_text` picks by
# step kind and argument key: `shell` for the first line of a tool call's
# command (every later line, a heredoc body among them, is `line`), `line`
# for a tool result and for a tool call's file-content arguments
# (`TEXT_ARG_KEYS`), `token` for a reply and for a tool call's other
# arguments (a worker reports through `kanban_complete(result=...)`, which
# is prose).
VALUE_SPAN_SHELL = "shell"
VALUE_SPAN_LINE = "line"
VALUE_SPAN_TOKEN = "token"
TEXT_ARG_KEYS = ("content",)
# A backslash-escaped space continues a shell word (`password=my\ secret\
# phrase` is one value). `scrub_step_text` folds each to a character no rule
# reads as whitespace before the redactor runs, so the redactor and the
# supplement see one token, and the supplement unfolds what survives.
SHELL_ESCAPED_SPACE = "\\ "
SHELL_ESCAPED_SPACE_FOLD = "\ue000"
# A line that opens a JSON document; the manifest marker that licenses the
# text-form `data` rule is looked for from the nearest such line before the
# object, so a `"kind":` on an earlier log line says nothing about a later one.
JSON_DOCUMENT_OPENER_LINE_RE = re.compile(r"(?m)^[ \t]*\{")
ENV_NAME_KEY = "name"
ENV_VALUE_KEY = "value"
# The redactor's own list of key suffixes that name no credential (`name`,
# `path`, ...), read from the module it was loaded from under this attribute;
# a redactor that predates the list falls back to the same words.
REDACTOR_NON_CREDENTIAL_SUFFIXES_ATTR = "NON_CREDENTIAL_KEY_SUFFIXES"
NON_CREDENTIAL_KEY_SUFFIXES_FALLBACK = frozenset({"name", "names", "path", "file", "mode", "ref", "type", "kind"})
# A flat base64 map (a Secret's `data` with the envelope cut off) is masked
# when one value is at least this long or sits under a credential-named key.
FLAT_BASE64_LONG_VALUE_MIN_CHARS = 16
BASE64_SECRET_VALUE_MIN_CHARS = 4
BASE64_QUANTUM_CHARS = 4
BASE64_PADDING_CHAR = "="
BASE64_DECODED_TEXT_WHITESPACE = "\n\r\t"
BASE64_DECODED_TEXT_ENCODING = "utf-8"
# The separator a `NAME=value` token is rescanned on, the character the
# shell continuation folds to, and the whitespace an indent or a line's tail
# is made of.
EQUALS_SEPARATOR = "="
SHELL_CONTINUATION_FOLD = " "
INDENT_CHAR = " "
INLINE_WHITESPACE = " \t"
LINE_BREAK = "\n"
LINE_TAIL_WHITESPACE = " \t\r"
# The one text line the rest-of-line mask keeps whole: kubectl's `describe
# pod` env row names where a value comes from and holds none of it, `<set to
# the key 'password' in secret 'db'>  Optional: false` (a ConfigMap-backed one
# reads `of config map`). The redactor runs first and has masked `<set` by
# then, so the marker is accepted in its place; the row is kept as it stands,
# in every rule that would read it.
KUBECTL_ENV_SOURCE_REF_RE = re.compile(
    r"(?:<set|" + REDACTED_MARKER_TOKEN_RE_SRC + r") to the key '[^'\r\n]*' (?:in|of) (?:secret|config ?map) '[^'\r\n]*'>"
    r"(?:[ \t]+Optional:[ \t]+(?:true|false))?[ \t]*(?=\r?$|\n)",
    re.MULTILINE,
)
# A CLI value that starts with this is the next flag, not a credential.
CLI_FLAG_LEAD_CHAR = "-"
CLI_FLAG_WORD_SEPARATOR = "_"
CLI_FLAG_NORMALISED_SEPARATOR = "-"
# The user prompt the kanban dispatcher starts a worker session with
# (`hermes -p <profile> chat -q "work kanban task <id>"`). Both readers map a
# card to its worker session through it when `task_runs.metadata` does not
# record `worker_session_id`.
WORKER_PROMPT_PREFIX = "work kanban task "
# The id ends where the prompt's does: a card whose id merely starts with
# another's is not that card.
WORKER_PROMPT_TASK_ID_RE = r"([\w-]+)"
WORKER_PROMPT_TASK_ID_BOUNDARY_RE = r"(?![\w-])"
WORKER_PROMPT_TASK_RE = re.compile(re.escape(WORKER_PROMPT_PREFIX) + WORKER_PROMPT_TASK_ID_RE + WORKER_PROMPT_TASK_ID_BOUNDARY_RE)
# Where a profile's Hermes session store sits under the data root: the
# default profile's in the root itself, every other profile's under
# `profiles/<name>/`.
DEFAULT_PROFILE = "default"
PROFILES_DIR = "profiles"
HERMES_STATE_DB_FILENAME = "state.db"
# The `task_runs` columns the stamped-session walk reads, and the metadata
# key Hermes writes when the worker ran with `HERMES_SESSION_ID` set.
TASK_RUN_METADATA_FIELD = "metadata"
TASK_RUN_PROFILE_FIELD = "profile"
WORKER_SESSION_ID_METADATA_KEY = "worker_session_id"
# The block rules below run only on text that can hold a `data:` block;
# `stringData` carries its only "data" capitalised.
SECRET_BLOCK_FAST_PATH_WORDS = ("data", "Data")
JSON_OBJECT_OPEN = "{"
JSON_OBJECT_CLOSE = "}"
YAML_SECRET_BLOCK_RE = re.compile(r"^(\s*)(data|stringData)\s*:\s*$")
YAML_PAIR_RE = re.compile(r"^(\s*)([^\s:]+)\s*:\s*(.*)$")
JSON_SECRET_BLOCK_RE = re.compile(r'("(?:data|stringData)"\s*:\s*\{)([^{}]*)(\}|\Z)')
JSON_PAIR_RE = re.compile(r'("(?:[^"\\]|\\.)*"\s*:\s*")((?:[^"\\]|\\.)*)("|\Z)')
URL_USERINFO_RE = re.compile(r"(://[^\s:/?#@\[\]\"'<>{}\\|^`,;]*:)[^\s/?#\[\]\"'<>{}\\|^`,;]+(?=@)")
UNTERMINATED_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY(?:\s+BLOCK)?-----[ \t]*\r?\n[A-Za-z0-9+/=\s]*\Z",
    re.IGNORECASE,
)
CLI_LONG_FLAG_RE = re.compile(
    r"(?P<prefix>(?:^|\s)--)(?P<flag>[A-Za-z0-9][A-Za-z0-9_-]*)(?P<sep>[ \t=]+)"
    r"(?P<val>\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|\S+)"
)
CLI_EXTRA_SECRET_FLAGS = frozenset(
    {
        "docker-password",
        "admin-password",
        "auth-token",
        "authtoken",
        "bearer-token",
        "passphrase",
    }
)
CLI_NON_SECRET_FLAGS = frozenset({"with-token"})
CLI_USERPASS_FLAG_RE = re.compile(
    r"\b(?:curl|wget|mysql|mysqldump|mariadb|mysqladmin|(?:docker|helm(?:\s+registry)?)\s+login)\b[^\r\n;|&]*"
)
CLI_USERPASS_ARG_RE = re.compile(
    r"(?P<prefix>\s(?:-u|--user)[ \t=]*)"
    r"(?:(?P<quote>[\"'])(?P<quser>[^:\s\"']+:)(?P<qval>(?:(?!(?P=quote))[^\r\n])+)(?P=quote)"
    r"|(?P<user>[^:\s\"']+:)(?P<val>\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|[^\s\"']+))"
)
CLI_SHORT_P_RE = re.compile(
    r"(?P<prefix>\b(?:mysql|mysqldump|mariadb|mysqladmin|sshpass|mongosh|mongo|redis-cli|az\s+login|(?:docker|helm(?:\s+registry)?)\s+login)\b"
    r"[^\r\n;|&]{0,512}?\s-(?:p|a)[ \t=]*)"
    r"(?P<val>\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|[^\s\"']+)"
)
# The two rules above end a command's segment at `;`, `|` or `&`, but a
# shell operator inside a quoted argument (`?query=x&type=dash-db`,
# `-e "select 1 | 2"`) is data. `sub_shell_segments` therefore finds the
# segment on a copy of the text whose quoted interiors are blanked to spaces
# (same length, so every offset holds) and reads the flag and its value out
# of the original at that span. A quote that does not close on its own line
# is left as written, which is how a shell would reject it.
SHELL_QUOTE_CHARS = ("'", '"')
SHELL_QUOTE_BLANK_CHAR = " "
SHELL_QUOTED_SPAN_RE = re.compile(r"'[^'\r\n]*'|\"(?:[^\"\\\r\n]|\\[^\r\n])*\"")
# A scheme-led value (`Bearer <token>`) after any credential-named key, with
# `:` or `=` as the separator: the redactor's own bearer rule needs twelve
# token characters from its alphabet, so a short or `~`-bearing token after
# `auth:`, `token:` or `AUTH_HEADER=` would otherwise outlive the marker.
# The redactor runs first and, for a key its own key rule knows (`token:`),
# masks the scheme word as the value; the marker it leaves there counts as
# the scheme so the token after it is still taken. A shell operator is not a
# token: `GH_TOKEN=[REDACTED_SECRET] && gh auth status` keeps its tail.
AUTH_SCHEME_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?\b(?P<key>[\w.\-]+)[\"']?[ \t]*[:=][ \t]*"
    r"(?P<scheme>bearer|basic|token|apikey|api-key|digest|negotiate|aws4-hmac-sha256|mutual|"
    + re.escape(REDACTED_SECRET_MARKER)
    + r")[ \t]+)"
    r"(?P<val>[^\s\"',}{\[\]\\&|;]+)"
)
TEXT_COLON_KV_RE = re.compile(
    r"(?m)^(?P<prefix>[ \t]*(?:-[ \t]+)?[\"']?)(?P<key>[\w.\-]+)(?P<sep>[\"']?[ \t]*:[ \t]*)"
    r"(?P<val>" + REDACTED_MARKER_TOKEN_RE_SRC + r"|\"(?:[^\"\\\r\n]|\\.)*(?:\"|\Z)|'[^'\r\n]*(?:'|\Z)|[^\s\"',}{\[\]]+)"
)
QUOTED_COLON_KV_RE = re.compile(
    r"(?P<prefix>[\"'])(?P<key>[\w.\-]+)(?P<sep>(?P=prefix)[ \t]*:[ \t]*)"
    r"(?P<val>\"(?:[^\"\\\r\n]|\\.)*(?:\"|\Z)|'[^'\r\n]*(?:'|\Z)|[^\s\"',}{\[\]]+)"
)
TEXT_EQUALS_KV_RE = re.compile(
    r"(?<![\w.\-/\"])(?P<prefix>)(?P<key>[\w.\-]+)(?P<sep>[ \t]*=[ \t]*)"
    r"(?P<val>" + REDACTED_MARKER_TOKEN_RE_SRC + r"|\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|[^\s\"',}{\[\]]+)"
)
SUPPLEMENT_BLOCK_SCALAR_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?:-[ \t]+)?[\"']?(?P<key>[\w.\-]+)[\"']?[ \t]*:[ \t]*[|>][+-]?[0-9]?[+-]?[ \t]*\r?\n"
    r"(?P<body>(?:(?:[ \t]*\r?\n)*(?P=indent)[ \t]+[^\r\n]*(?:\r?\n|$))+)"
)
SUPPLEMENT_ENV_BLOCK_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?:-[ \t]+)?name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)"
    r"[ \t]*\r?\n(?P=indent)(?P<keyindent>[ \t]*)value:[ \t]*[|>][+-]?[0-9]?[+-]?[ \t]*\r?\n"
    r"(?P<body>(?:(?:[ \t]*\r?\n)*(?P=indent)(?P=keyindent)[ \t]+[^\r\n]*(?:\r?\n|$))+)"
)
SUPPLEMENT_ENV_YAML_PATTERNS = (
    re.compile(
        r"(?m)^(?P<indent>[ \t]*)(?:-[ \t]+)?name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)"
        r"[ \t]*\r?\n(?P=indent)[ \t]*value:[ \t]*(?P<value>(?:\S[^\r\n]*)?)(?=\r?$)"
    ),
    re.compile(
        r"(?m)^(?P<indent>[ \t]*)(?:-[ \t]+)?value:[ \t]*(?P<value>(?:\S[^\r\n]*)?)\r?\n"
        r"(?P=indent)[ \t]*name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)[ \t]*(?=\r?$)"
    ),
)
KEY_DATA_WORDS = ({"key", "data"}, {"certificate", "data"})
# A key that is a file name (`tls.crt`, `cert.pem`) opens ordinary diagnostic
# lines: `kubectl describe secret` rows (`tls.crt:  1679 bytes`), `openssl
# verify` (`cert.pem: OK`), `file` (`cert.pem: PEM certificate`), `grep -n`
# (`cert.pem:1:-----BEGIN`, `cert.pem:28:-----END`). The value there is a
# byte count followed by the word `bytes`, a line number with at most a PEM
# marker after it, or one of the words those tools print, never key
# material, so the name alone does not mask it. A `grep -n` body line
# (`tls.key:2:MIIEvQ...`), a bare number (`api.key: 1234567890123456`) and a
# describe row of a non-file key (`token:  1234 bytes`) stay masked: the
# first two can be a credential, the third is over-masking that is safe.
FILE_NAME_KEY_SUFFIXES = (".key", ".pem", ".crt", ".p12", ".pfx")
FILE_KEY_LINE_NUMBER_RE = re.compile(r"^\d+:(?:-----[A-Za-z]*)?$")
FILE_KEY_BYTE_COUNT_RE = re.compile(r"^\d+$")
FILE_KEY_BYTES_WORD_RE = re.compile(r"^[ \t]+bytes\b")
FILE_KEY_BENIGN_WORDS = frozenset(
    {"ok", "pem", "openssh", "data", "ascii", "certificate", "verification", "error", "cannot", "unable", "bytes"}
)
SENSITIVE_KEY_SUFFIXES = (
    *FILE_NAME_KEY_SUFFIXES,
    ".dockercfg",
    ".dockerconfigjson",
    "privatekey",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
)
BASE64_SECRET_VALUE_RE = re.compile(r"^[A-Za-z0-9+/]{4,}={0,2}$")
INLINE_JSON_OBJECT_RE = re.compile(r"\{[^{}]+\}")
# A `kubectl get` of a secret whose output is shaped (`-o`/`--output`/
# `--template`, in any spelling: `-o json`, `-o=go-template=...`,
# `--output=yaml`) or piped anywhere (`| jq`, `| yq`, `| base64`) can print a
# value; the formatter is not enumerated because the agent writes the command
# and a form the list does not name would land as the value alone, which no
# key rule recognises. kubectl takes its flags in any position, so the two
# conditions are lookaheads from `kubectl`, not a token order: `get` with
# `secret` somewhere after it on the same unpiped segment, and a flag or a
# pipe anywhere on the line. A whitespace-led `-o` counts whatever follows
# it (`-ojson`, `-o'go-template=...'`, `-o"..."`, `-o$FMT`): kubectl reads
# the attached text as the format, and a quote or a variable is the shell's
# to resolve. A bare list (`kubectl get secrets -n ns`) prints names and ages
# only; a ConfigMap whose name contains `secret` is withheld too, which is
# the safe direction. gcloud and aws take global flags and a release track
# before the subcommand (`gcloud --project=p beta secrets versions access`,
# `aws --region r secretsmanager get-secret-value`), so those two match the
# subcommand anywhere on the same unpiped segment after the binary.
# A shell joins a backslash-newline before it parses, so the detector folds
# the continuation (and the indentation after it) to one space first; every
# alternative below is line-bounded and would otherwise see two commands.
SHELL_CONTINUATION_RE = re.compile(r"\\\r?\n[ \t]*")
# The tool-call argument keys that carry a shell command, in the order
# `is_secret_read_tool_call` and `scrub_step_text` read them.
COMMAND_ARG_KEYS = ("command", "cmd")
SECRET_READ_COMMAND_RE = re.compile(
    r"(?i)(?:"
    r"\bkubectl\b(?=[^\r\n]*\bget\b[^\r\n|]*\bsecrets?\b)"
    r"(?=[^\r\n]*(?:(?:^|\s)(?:-o|--output(?:[=\s]|$)|--template(?:[=\s]|$))|\|))"
    r"|\bjsonpath\s*=\s*['\"]?[^'\"]*\.(?:data|stringData)\b"
    r"|\bgcloud\b[^\r\n|;&]*\bsecrets\s+versions\s+access\b"
    r"|\baws\b[^\r\n|;&]*\bsecretsmanager\s+get-secret-value\b"
    r"|\baws\b[^\r\n|;&]*\bssm\s+get-parameters?\b"
    r"|\baz\b[^\r\n|;&]*\bkeyvault\s+secret\s+show\b"
    r"|\bvault\s+(?:kv\s+get|read)\b"
    r"|\bsops\s+(?:-d|--decrypt|decrypt)\b"
    # A file read (also after `kubectl exec ... --`) under a well-known secret
    # mount, or under a directory component that is, or ends in `-`,
    # `secret(s)`, `creds` or `credentials` (`/etc/db-creds/password`).
    r"|\b(?:cat|head|tail|less|more|base64)\b[^\r\n]*/"
    r"(?:var/run/secrets|etc/kubernetes/pki|etc/ssl/private|\.ssh/id_|(?:[^\s/]*-)?(?:secrets?|creds|credentials)/)[^\r\n]*"
    r")"
)


def scrub_secret_blocks(redactor: Any, text: str) -> str:
    # `stringData` carries its only "data" capitalised; the fast path must
    # let it through to the block rules below.
    if not any(word in text for word in SECRET_BLOCK_FAST_PATH_WORDS):
        return text
    lines = text.split(LINE_BREAK)
    block: Optional[int] = None
    for i, line in enumerate(lines):
        opener = YAML_SECRET_BLOCK_RE.match(line)
        if opener:
            block = len(opener.group(1))
            continue
        if block is None or not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= block:
            block = None
            continue
        pair = YAML_PAIR_RE.match(line)
        if pair is None:
            lines[i] = INDENT_CHAR * indent + REDACTED_SECRET_MARKER
        elif pair.group(3):
            lines[i] = f"{pair.group(1)}{pair.group(2)}: {REDACTED_SECRET_MARKER}"
    joined = LINE_BREAK.join(lines)

    # By shape under a manifest marker in the object's own document (from the
    # nearest line that opens one), not by kind: a ConfigMap's `data` is
    # blanked as the YAML line scan blanks it (the redactor's safe direction),
    # and kubectl's key-sorted JSON cut short inside `data` still carries
    # `apiVersion`. A `stringData` object is a Secret's wherever it sits. A
    # log line's or an API envelope's `data` is kept, whatever an earlier
    # line said.
    def _mask_json_block(m: "re.Match[str]") -> str:
        if JSON_STRING_DATA_KEY_QUOTED not in m.group(1):
            document_start = 0
            for opener in JSON_DOCUMENT_OPENER_LINE_RE.finditer(joined, 0, m.start() + 1):
                document_start = opener.start()
            if not JSON_MANIFEST_MARKER_RE.search(joined, document_start, m.start()):
                return m.group(0)
        return (
            m.group(1)
            + JSON_PAIR_RE.sub(lambda p: p.group(1) + REDACTED_SECRET_MARKER + p.group(3), m.group(2))
            + m.group(3)
        )

    return JSON_SECRET_BLOCK_RE.sub(_mask_json_block, joined)


def is_non_credential_key_suffix(redactor: Any, name: str) -> bool:
    last_word = redactor._last_key_word(name)
    # The list lives beside the class in the module it was loaded from,
    # under whatever name the caller registered that module.
    non_cred_suffixes = getattr(
        sys.modules.get(getattr(redactor, "__module__", "")),
        REDACTOR_NON_CREDENTIAL_SUFFIXES_ATTR,
        NON_CREDENTIAL_KEY_SUFFIXES_FALLBACK,
    )
    return last_word in non_cred_suffixes


def cli_flag_is_sensitive(redactor: Any, flag: str) -> bool:
    norm_flag = flag.lower().replace(CLI_FLAG_WORD_SEPARATOR, CLI_FLAG_NORMALISED_SEPARATOR)
    if norm_flag in CLI_NON_SECRET_FLAGS or is_non_credential_key_suffix(redactor, norm_flag):
        return False
    return norm_flag in CLI_EXTRA_SECRET_FLAGS or bool(redactor.CREDENTIAL_NAME_PATTERN.match(norm_flag))


def dict_key_needs_supplement(redactor: Any, name: str) -> bool:
    """Supplemental dictionary key check for keys `AuditRedactor.SENSITIVE_KEYS`
    does not cover (`client-key-data`, `client-certificate-data`, and `.key`/`.crt`
    file-name keys)."""
    if is_non_credential_key_suffix(redactor, name):
        return False
    words = redactor._get_key_words(name)
    return (
        any(w <= words for w in KEY_DATA_WORDS)
        or name.lower().endswith(SENSITIVE_KEY_SUFFIXES)
    )


def named_value_is_sensitive(redactor: Any, name: str) -> bool:
    if is_non_credential_key_suffix(redactor, name):
        return False
    words = redactor._get_key_words(name)
    return (
        bool(words & redactor.SENSITIVE_KEYS)
        or any(w <= words for w in KEY_DATA_WORDS)
        or name.lower().endswith(SENSITIVE_KEY_SUFFIXES)
    )


def blank_quoted_spans(text: str) -> str:
    """Return `text` with the interior of every single- or double-quoted span
    replaced by spaces, keeping the quotes and the length."""
    if not any(q in text for q in SHELL_QUOTE_CHARS):
        return text
    return SHELL_QUOTED_SPAN_RE.sub(
        lambda m: m.group(0)[0] + SHELL_QUOTE_BLANK_CHAR * (len(m.group(0)) - 2) + m.group(0)[-1], text
    )


def sub_shell_segments(
    segment_re: "re.Pattern[str]",
    text: str,
    mask: "Callable[[re.Match[str], str], str]",
) -> str:
    """Find each `segment_re` match on the quote-blanked copy of `text` and
    replace that span of the original with `mask(match, text)`."""
    blanked = blank_quoted_spans(text)
    out: list[str] = []
    pos = 0
    for match in segment_re.finditer(blanked):
        out.append(text[pos : match.start()])
        out.append(mask(match, text))
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


def unquote_token(raw: str) -> str:
    stripped = raw.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in SHELL_QUOTE_CHARS:
        return stripped[1:-1]
    if len(stripped) >= 1 and stripped[0] in SHELL_QUOTE_CHARS:
        return stripped[1:]
    return stripped


def scrub_supplement_env_pairs(redactor: Any, text: str) -> str:
    if ENV_NAME_KEY not in text or ENV_VALUE_KEY not in text:
        return text

    def _mask_env_block(match: "re.Match[str]") -> str:
        if not named_value_is_sensitive(redactor, match.group("name")):
            return match.group(0)
        body = match.group("body")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if lines and all(line.startswith(REDACTED_MARKER_PREFIX) for line in lines):
            return match.group(0)
        body_indent = body[: len(body) - len(body.lstrip(INLINE_WHITESPACE))]
        ending = LINE_BREAK if body.endswith(LINE_BREAK) else ""
        return f"{match.group(0)[: match.start('body') - match.start()]}{body_indent}{REDACTED_SECRET_MARKER}{ending}"

    def _mask_env_span(match: "re.Match[str]", start: int, end: int) -> str:
        value = match.string[start:end]
        if not redactor._is_credential_value(value) or not named_value_is_sensitive(
            redactor, match.group("name")
        ):
            return match.group(0)
        whole = match.group(0)
        return f"{whole[:start - match.start()]}{REDACTED_SECRET_MARKER}{whole[end - match.start():]}"

    def _mask_env_yaml(match: "re.Match[str]") -> str:
        raw = match.group("value").rstrip()
        start = match.start("value")
        if len(raw) > 1 and raw[0] in SHELL_QUOTE_CHARS and raw[-1] == raw[0]:
            return _mask_env_span(match, start + 1, start + len(raw) - 1)
        return _mask_env_span(match, start, start + len(raw))

    def _mask_env_json(match: "re.Match[str]") -> str:
        return _mask_env_span(match, match.start("value"), match.end("value"))

    text = SUPPLEMENT_ENV_BLOCK_RE.sub(_mask_env_block, text)
    for pattern in SUPPLEMENT_ENV_YAML_PATTERNS:
        text = pattern.sub(_mask_env_yaml, text)
    for pattern in getattr(redactor, "ENV_PAIR_JSON_PATTERNS", ()):
        text = pattern.sub(_mask_env_json, text)
    return text


def is_quoted_token(raw: str) -> bool:
    """Whether `raw` is one quoted span: a value its quotes delimit."""
    stripped = raw.strip()
    return len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in SHELL_QUOTE_CHARS


def line_tail_end(text: str, pos: int) -> int:
    """The offset where the line holding `pos` ends, trailing whitespace excluded."""
    end = text.find(LINE_BREAK, pos)
    if end < 0:
        end = len(text)
    while end > pos and text[end - 1] in LINE_TAIL_WHITESPACE:
        end -= 1
    return end


def is_env_source_ref(text: str, pos: int) -> bool:
    """Whether the value at `pos` is kubectl's env-source row (`KUBECTL_ENV_SOURCE_REF_RE`)."""
    return KUBECTL_ENV_SOURCE_REF_RE.match(text, pos) is not None


def sub_named_pairs(
    pattern: "re.Pattern[str]",
    text: str,
    mask: "Callable[[re.Match[str]], tuple[str, bool]]",
    extend: bool,
) -> str:
    """`pattern.sub` whose `mask` returns `(replacement, to_line_end)`: when
    `extend` and the second are true the replacement also stands for the rest
    of the line (`line_tail_end`, trailing whitespace excluded), and no later
    match on that line is read.

    With `extend` (a text line, `VALUE_SPAN_LINE`) an unquoted value after a
    credential-named key is the rest of the line (`DB_PASSWORD:  my secret
    phrase`), not its first word; without it (a shell command, prose) the
    value is its token. A quoted value ends at its quote either way.
    """
    out: list[str] = []
    pos = 0
    for match in pattern.finditer(text):
        if match.start() < pos:
            continue
        replacement, to_line_end = mask(match)
        out.append(text[pos : match.start()])
        out.append(replacement)
        pos = max(match.end(), line_tail_end(text, match.end())) if extend and to_line_end else match.end()
    out.append(text[pos:])
    return "".join(out)


def file_key_value_is_benign(value: str, tail: str) -> bool:
    """True for what a file-named key holds on a diagnostic line and never as
    key material: a line number, a byte count that the word `bytes` follows
    (tail is the text after the value), or one of the words the tools print."""
    if FILE_KEY_LINE_NUMBER_RE.match(value):
        return True
    if FILE_KEY_BYTE_COUNT_RE.match(value):
        return bool(FILE_KEY_BYTES_WORD_RE.match(tail))
    return value.lower() in FILE_KEY_BENIGN_WORDS


def blank_named_values(redactor: Any, text: str, value_span: str = VALUE_SPAN_TOKEN) -> str:
    """The `NAME: value` / `NAME=value` / CLI / bearer rules on `text`; `value_span`
    says how far an unquoted value reaches (`VALUE_SPAN_SHELL`, `_LINE`, `_TOKEN`).

    A shell command is shell on its first line only (a folded continuation is
    part of it); every later line, a heredoc body among them, is text.
    """
    if value_span == VALUE_SPAN_SHELL and LINE_BREAK in text:
        command_line, body = text.split(LINE_BREAK, 1)
        return (
            blank_named_values(redactor, command_line, VALUE_SPAN_SHELL)
            + LINE_BREAK
            + blank_named_values(redactor, body, VALUE_SPAN_LINE)
        )
    extend = value_span == VALUE_SPAN_LINE

    def _mask_block(match: "re.Match[str]") -> str:
        if not named_value_is_sensitive(redactor, match.group("key")):
            return match.group(0)
        body = match.group("body")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if lines and all(line.startswith(REDACTED_MARKER_PREFIX) for line in lines):
            return match.group(0)
        body_indent = body[: len(body) - len(body.lstrip(INLINE_WHITESPACE))]
        ending = LINE_BREAK if body.endswith(LINE_BREAK) else ""
        head = match.group(0)[: match.start("body") - match.start()]
        return f"{head}{body_indent}{REDACTED_SECRET_MARKER}{ending}"

    def _mask_colon_kv(match: "re.Match[str]") -> tuple[str, bool]:
        key = match.group("key")
        raw_val = match.group("val")
        unquoted = unquote_token(raw_val)
        # A file-name key opens diagnostic lines; its value is masked by the
        # token, never to the line's end.
        is_file_key = key.lower().endswith(FILE_NAME_KEY_SUFFIXES)
        if is_file_key and file_key_value_is_benign(unquoted, match.string[match.end() :]):
            return match.group(0), False
        if not named_value_is_sensitive(redactor, key) or is_env_source_ref(match.string, match.start("val")):
            return match.group(0), False
        to_line_end = not is_file_key and not is_quoted_token(raw_val)
        if unquoted.startswith(REDACTED_MARKER_PREFIX):
            # The redactor masked the first word, quotes and marker as it
            # wrote them; the rest of the value goes with it.
            return match.group(0), to_line_end
        if redactor._is_credential_value(unquoted):
            return f"{match.group('prefix')}{key}{match.group('sep')}{REDACTED_SECRET_MARKER}", to_line_end
        return match.group(0), False

    def _mask_equals_kv(match: "re.Match[str]") -> tuple[str, bool]:
        key = match.group("key")
        raw_val = match.group("val")
        unquoted = unquote_token(raw_val)
        if named_value_is_sensitive(redactor, key) and not is_env_source_ref(match.string, match.start("val")):
            to_line_end = not is_quoted_token(raw_val)
            if unquoted.startswith(REDACTED_MARKER_PREFIX):
                return match.group(0), to_line_end
            if redactor._is_credential_value(unquoted):
                return f"{match.group('prefix')}{key}{match.group('sep')}{REDACTED_SECRET_MARKER}", to_line_end
        if EQUALS_SEPARATOR in unquoted:
            # A token that holds pairs of its own (`--from-literal=password=x`)
            # is rescanned as a token: a pair inside it ends where it does.
            rescanned = sub_named_pairs(TEXT_EQUALS_KV_RE, unquoted, _mask_equals_token, False)
            if rescanned != unquoted:
                if is_quoted_token(raw_val):
                    return f"{match.group('prefix')}{key}{match.group('sep')}{raw_val[0]}{rescanned}{raw_val[0]}", False
                return f"{match.group('prefix')}{key}{match.group('sep')}{rescanned}", False
        return match.group(0), False

    def _mask_equals_token(match: "re.Match[str]") -> tuple[str, bool]:
        return _mask_equals_kv(match)[0], False

    def _mask_quoted_colon_kv(match: "re.Match[str]") -> str:
        # Inline pairs (`"password": "x", "user": "y"`): one token each.
        return _mask_colon_kv(match)[0]

    def _mask_long_flag(match: "re.Match[str]") -> str:
        flag = match.group("flag")
        raw_val = match.group("val")
        unquoted = unquote_token(raw_val)
        if unquoted.startswith(CLI_FLAG_LEAD_CHAR):
            return match.group(0)
        if cli_flag_is_sensitive(redactor, flag) and redactor._is_credential_value(unquoted):
            return f"{match.group('prefix')}{flag}{match.group('sep')}{REDACTED_SECRET_MARKER}"
        return match.group(0)

    def _mask_short_p(match: "re.Match[str]", source: str) -> str:
        # `match` comes from the quote-blanked copy; the value is read from `source`.
        unquoted = unquote_token(source[match.start("val") : match.end("val")])
        if unquoted.startswith(CLI_FLAG_LEAD_CHAR) or not redactor._is_credential_value(unquoted):
            return source[match.start() : match.end()]
        return f"{source[match.start() : match.start('val')]}{REDACTED_SECRET_MARKER}"

    def _mask_userpass_segment(match: "re.Match[str]", source: str) -> str:
        return CLI_USERPASS_ARG_RE.sub(_mask_userpass, source[match.start() : match.end()])

    def _mask_userpass(match: "re.Match[str]") -> str:
        quote = match.group("quote") or ""
        user = match.group("quser") if quote else match.group("user")
        val = match.group("qval") if quote else unquote_token(match.group("val"))
        if val.startswith(CLI_FLAG_LEAD_CHAR) or not redactor._is_credential_value(val):
            return match.group(0)
        return f"{match.group('prefix')}{quote}{user}{REDACTED_SECRET_MARKER}{quote}"

    def _mask_auth_scheme(match: "re.Match[str]") -> str:
        val = unquote_token(match.group("val"))
        if (
            not named_value_is_sensitive(redactor, match.group("key"))
            or not redactor._is_credential_value(val)
            or is_env_source_ref(match.string, match.start("scheme"))
            # In a shell command the word after a masked assignment is the
            # next argument (`DB_PASSWORD=[REDACTED_SECRET] LOG_LEVEL=debug`),
            # not a token after a scheme.
            or (value_span == VALUE_SPAN_SHELL and match.group("scheme").startswith(REDACTED_MARKER_PREFIX))
        ):
            return match.group(0)
        return f"{match.group('prefix')}{REDACTED_SECRET_MARKER}"

    text = scrub_supplement_env_pairs(redactor, text)
    text = SUPPLEMENT_BLOCK_SCALAR_RE.sub(_mask_block, text)
    text = AUTH_SCHEME_HEADER_RE.sub(_mask_auth_scheme, text)
    text = CLI_LONG_FLAG_RE.sub(_mask_long_flag, text)
    text = sub_shell_segments(CLI_USERPASS_FLAG_RE, text, _mask_userpass_segment)
    text = sub_shell_segments(CLI_SHORT_P_RE, text, _mask_short_p)
    text = sub_named_pairs(TEXT_COLON_KV_RE, text, _mask_colon_kv, extend)
    text = QUOTED_COLON_KV_RE.sub(_mask_quoted_colon_kv, text)
    text = sub_named_pairs(TEXT_EQUALS_KV_RE, text, _mask_equals_kv, extend)
    return text.replace(SHELL_ESCAPED_SPACE_FOLD, SHELL_ESCAPED_SPACE)


def as_json_structure(text: str) -> Any:
    if text.lstrip()[:1] not in JSON_STRUCTURE_OPENERS:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def is_base64_encoded_secret_value(v: str) -> bool:
    if len(v) < BASE64_SECRET_VALUE_MIN_CHARS or len(v) % BASE64_QUANTUM_CHARS != 0 or BASE64_SECRET_VALUE_RE.fullmatch(v) is None:
        return False
    try:
        raw = base64.b64decode(v, validate=True)
    except ValueError:
        return False
    if not raw:
        return False
    try:
        decoded_text = raw.decode(BASE64_DECODED_TEXT_ENCODING)
    except UnicodeDecodeError:
        return v.endswith(BASE64_PADDING_CHAR)
    return all(ch.isprintable() or ch in BASE64_DECODED_TEXT_WHITESPACE for ch in decoded_text)


def is_flat_base64_secret_map(redactor: Any, value: Dict[Any, Any]) -> bool:
    if not value:
        return False
    has_long_or_sensitive = False
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return False
        if not is_base64_encoded_secret_value(v):
            return False
        if len(v) >= FLAT_BASE64_LONG_VALUE_MIN_CHARS or named_value_is_sensitive(redactor, k):
            has_long_or_sensitive = True
    return has_long_or_sensitive


def supplement_redact(
    redactor: Any,
    value: Any,
    key: Optional[str] = None,
    *,
    is_tool_result: bool = False,
    parent_is_manifest: bool = False,
    value_span: str = VALUE_SPAN_TOKEN,
    shell_keys: Sequence[str] = (),
    line_keys: Sequence[str] = (),
) -> Any:
    """The supplement over a value: text rules on a string, by-shape blanking on a
    mapping, recursion through the rest. `value_span` is how far an unquoted
    value reaches in a string (`VALUE_SPAN_*`); a string under a key in
    `shell_keys` is a shell command and one under a key in `line_keys` is a
    text file, whatever `value_span` says."""
    if isinstance(value, str):
        if key in shell_keys:
            span = VALUE_SPAN_SHELL
        elif key in line_keys:
            span = VALUE_SPAN_LINE
        else:
            span = value_span
        nested = as_json_structure(value)
        if nested is not None:
            return json.dumps(
                supplement_redact(
                    redactor,
                    nested,
                    key=None,
                    is_tool_result=is_tool_result,
                    value_span=span,
                    shell_keys=shell_keys,
                    line_keys=line_keys,
                )
            )
        if is_tool_result and JSON_OBJECT_OPEN in value and JSON_OBJECT_CLOSE in value:

            def _scrub_inline_obj(m: "re.Match[str]") -> str:
                obj = as_json_structure(m.group(0))
                if isinstance(obj, dict) and is_flat_base64_secret_map(redactor, obj):
                    return json.dumps(
                        {
                            k: REDACTED_SECRET_MARKER if isinstance(v, str) else v
                            for k, v in obj.items()
                        }
                    )
                return m.group(0)

            value = INLINE_JSON_OBJECT_RE.sub(_scrub_inline_obj, value)
        text = redactor.PRIVATE_KEY_PATTERN.sub(REDACTED_PRIVATE_KEY_MARKER, value)
        text = UNTERMINATED_PRIVATE_KEY_RE.sub(REDACTED_PRIVATE_KEY_MARKER, text)
        text = URL_USERINFO_RE.sub(lambda m: m.group(1) + REDACTED_SECRET_MARKER, text)
        return blank_named_values(redactor, scrub_secret_blocks(redactor, text), span)
    if isinstance(value, dict):
        # A manifest's `data`/`stringData` mapping is blanked by shape,
        # whatever its kind, as the text forms are; so is a `data` map of
        # base64 strings with no manifest round it.
        if (
            key == MANIFEST_STRING_DATA_KEY
            or (key == MANIFEST_DATA_KEY and parent_is_manifest)
            or (key in (MANIFEST_DATA_KEY, MANIFEST_STRING_DATA_KEY) and is_flat_base64_secret_map(redactor, value))
            or (is_tool_result and key is None and is_flat_base64_secret_map(redactor, value))
        ):
            return {
                k: REDACTED_SECRET_MARKER
                if isinstance(v, str)
                else supplement_redact(
                    redactor,
                    v,
                    str(k),
                    is_tool_result=is_tool_result,
                    value_span=value_span,
                    shell_keys=shell_keys,
                    line_keys=line_keys,
                )
                for k, v in value.items()
            }
        is_manifest = any(marker in value for marker in MANIFEST_MARKER_KEYS)
        env_name = value.get(ENV_NAME_KEY)
        is_cred_env = isinstance(env_name, str) and named_value_is_sensitive(redactor, env_name)
        return {
            k: (
                REDACTED_SECRET_MARKER
                if isinstance(v, str)
                and ((is_cred_env and k == ENV_VALUE_KEY) or dict_key_needs_supplement(redactor, str(k)))
                and redactor._is_credential_value(v.strip())
                else supplement_redact(
                    redactor,
                    v,
                    str(k),
                    is_tool_result=is_tool_result,
                    parent_is_manifest=is_manifest,
                    value_span=value_span,
                    shell_keys=shell_keys,
                    line_keys=line_keys,
                )
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            supplement_redact(
                redactor,
                v,
                is_tool_result=is_tool_result,
                value_span=value_span,
                shell_keys=shell_keys,
                line_keys=line_keys,
            )
            for v in value
        ]
    return value


def fold_shell_command(command: str) -> str:
    """A command as the shell would read it: a backslash-newline continuation
    joined to its line, and an escaped space folded to one token character
    (`SHELL_ESCAPED_SPACE_FOLD`, unfolded by `blank_named_values`)."""
    return SHELL_CONTINUATION_RE.sub(SHELL_CONTINUATION_FOLD, command).replace(
        SHELL_ESCAPED_SPACE, SHELL_ESCAPED_SPACE_FOLD
    )


def scrub_step_text(redactor: Any, kind: str, text: str, withheld: str) -> str:
    """Scrub one worker step's text: `redactor.redact` first, then the supplement.

    `kind` is the step kind (`thinking`, `tool_call`, `tool_result`, `reply`);
    a `tool_call` has its command's shell continuations and escaped spaces
    folded first and the command read as a shell line (`VALUE_SPAN_SHELL`,
    its later lines as text), its file-content arguments (`TEXT_ARG_KEYS`)
    read as text and its other arguments as prose; a `tool_result` takes the
    inline-object rule and the rest-of-line mask (`VALUE_SPAN_LINE`); a reply
    keeps the first-token mask. Returns `withheld` for every kind when
    `redactor` is `None` or the structure is deeper than Python recurses, so
    an unscrubbable step is never served as written.
    """
    if redactor is None:
        return withheld
    is_call = kind == STEP_KIND_TOOL_CALL
    is_result = kind == STEP_KIND_TOOL_RESULT
    value_span = VALUE_SPAN_LINE if is_result else VALUE_SPAN_TOKEN
    shell_keys = COMMAND_ARG_KEYS if is_call else ()
    line_keys = TEXT_ARG_KEYS if is_call else ()
    try:
        parsed = as_json_structure(text)
        if is_call:
            # Fold a shell line continuation and an escaped space once, here,
            # so every line-bounded CLI rule below (`-u user:pass`, `-p pass`,
            # `--password pass`) reads the command the shell would run and an
            # escaped-space value is one token; `as_json_structure` returns a
            # fresh object, so the fold does not touch the row.
            if isinstance(parsed, dict):
                for arg_key in COMMAND_ARG_KEYS:
                    if isinstance(parsed.get(arg_key), str):
                        parsed[arg_key] = fold_shell_command(parsed[arg_key])
            elif parsed is None:
                text = fold_shell_command(text)
        if parsed is not None:
            return json.dumps(
                supplement_redact(
                    redactor,
                    redactor.redact(parsed),
                    is_tool_result=is_result,
                    value_span=value_span,
                    shell_keys=shell_keys,
                    line_keys=line_keys,
                )
            )
        # A bare-string tool call is the shell line itself.
        return str(
            supplement_redact(
                redactor,
                redactor.redact(text),
                is_tool_result=is_result,
                value_span=VALUE_SPAN_SHELL if is_call else value_span,
            )
        )
    except RecursionError:
        return withheld

def is_secret_read_tool_call(raw_args: Any) -> bool:
    """Whether a tool call's arguments run a command that prints a secret's value.

    `raw_args` is the arguments as Hermes stores them: a JSON string or an
    object whose `command`/`cmd` carries the shell line, or the bare line.
    """
    parsed = as_json_structure(raw_args) if isinstance(raw_args, str) else raw_args
    if isinstance(parsed, dict):
        cmd = next((parsed.get(k) for k in COMMAND_ARG_KEYS if parsed.get(k)), "")
        if isinstance(cmd, str) and SECRET_READ_COMMAND_RE.search(SHELL_CONTINUATION_RE.sub(SHELL_CONTINUATION_FOLD, cmd)):
            return True
    elif isinstance(raw_args, str) and SECRET_READ_COMMAND_RE.search(SHELL_CONTINUATION_RE.sub(SHELL_CONTINUATION_FOLD, raw_args)):
        return True
    return False


def worker_prompt_exact_re(task_id: str) -> "re.Pattern[str]":
    """The pattern that matches the dispatcher's prompt for `task_id` and no longer id.

    A `LIKE '%work kanban task <id>%'` scan also takes a card whose id merely
    starts with this one; the id has to end where the prompt's does.
    """
    return re.compile(re.escape(WORKER_PROMPT_PREFIX + task_id) + WORKER_PROMPT_TASK_ID_BOUNDARY_RE)


def profile_state_db_path(data_root: Path, profile: Optional[str]) -> Path:
    """Where `profile`'s Hermes session store sits under `data_root`.

    The default profile (or none) keeps its store in the root; every other
    profile's is under `profiles/<name>/`. The layout only: the server
    validates the profile name before it opens the path.
    """
    if not profile or profile == DEFAULT_PROFILE:
        return Path(data_root) / HERMES_STATE_DB_FILENAME
    return Path(data_root) / PROFILES_DIR / profile / HERMES_STATE_DB_FILENAME


def _run_field(run: Any, field: str) -> Any:
    # A `task_runs` row as `sqlite3.Row` (the bench) or as a dict (the feed):
    # both index by column name, and neither has every column on every read.
    try:
        return run[field]
    except (KeyError, IndexError, TypeError):
        return None


def run_worker_session_id(run: Any) -> Optional[str]:
    """The session id a `task_runs` row's metadata stamps, or `None`."""
    raw_meta = _run_field(run, TASK_RUN_METADATA_FIELD)
    try:
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) and raw_meta else {}
    except ValueError:
        meta = {}
    sid = meta.get(WORKER_SESSION_ID_METADATA_KEY) if isinstance(meta, dict) else None
    return sid if isinstance(sid, str) and sid else None


def stamped_worker_sessions(
    runs: Sequence[Any], assignee: Optional[str]
) -> tuple[list[tuple[str, str]], bool]:
    """The `(session_id, profile)` pairs `task_runs.metadata.worker_session_id`
    stamps, and whether any run carried no stamp.

    In run order, each session once; the profile is the run's, or the card's
    assignee when the run records none. One walk: the second value is what
    sends the feed to its prompt index for the runs the first does not cover.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    unstamped = False
    for run in runs:
        sid = run_worker_session_id(run)
        if sid is None:
            unstamped = True
            continue
        if sid in seen:
            continue
        seen.add(sid)
        found.append((sid, str(_run_field(run, TASK_RUN_PROFILE_FIELD) or assignee or "")))
    return found, unstamped

#!/usr/bin/env python3
"""Build gate for the credential-redaction patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_credential_redaction.py``. The applier proves the anchors matched;
this proves the behaviour the issue asked for, against the patched tree:

* a synthetic GCP access token is masked by ``redact_sensitive_text``, by the
  ``RedactingFormatter`` that ``agent.log`` uses, and in the ``💻 $`` line
  ``get_cute_tool_message`` renders for a ``terminal`` call;
* every shape the issue named as the floor masks too, rather than being
  assumed to (they are upstream's, but the gate is what says so);
* a real child process with ``HERMES_KANBAN_TASK`` set and stdout on a file
  -- the dispatcher's arrangement -- leaves no token in that file, whichever
  of the worker's print paths carried it, including prompt_toolkit's;
* a child without the variable keeps ``sys.stdout`` unwrapped, and a child
  with the operator opt-out set is still redacted;
* with ``KUBE_AGENTS_REDACTION_CONFIG`` naming a rule file, ``redact_sensitive_text``
  is the composed form and applies the operator's rules, a session-store write
  lands redacted in ``state.db`` and a kanban card's title, body, result,
  summary, a comment's body and a block's reason land redacted on the board;
  with the profile opt-out (``HERMES_REDACT_SECRETS=false``) set as well, the
  store still masks the token, a Hermes-only shape in the tool-call arguments,
  and the operator's identifiers; without the variable the store and the board
  are written as before (issue #1985). The runtime module path the image uses instead of the variable is
  asserted later in the build, once ``/opt/defaults`` exists.

Usage::

    cd /opt/hermes && python3 verify_credential_redaction.py
"""

from __future__ import annotations

import ast
import importlib.util
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []

#: Long enough that a 6-head/4-tail mask is unmistakable from the token, and
#: the length the reproduction on the live-test install measured, near enough.
TOKEN_BODY_LENGTH = 1024
TOKEN = "ya29." + ("a0AfB_" + "x" * 26) * (TOKEN_BODY_LENGTH // 32)
#: What upstream's ``_mask_token`` leaves of a long token: 6 head, 4 tail.
TOKEN_MASK = TOKEN[:6] + "..." + TOKEN[-4:]
#: Any run of the token's alphabet this long after the prefix is a leak; the
#: mask stub is shorter than this by construction.
LEAK_RE = re.compile(r"ya29\.[0-9A-Za-z\-_.]{20,}")

#: The shapes the issue names as the minimum. Each is a plausible-length
#: sample of the real format; the assertion is only that it does not survive.
FLOOR_SHAPES = {
    "GitHub app installation token (ghs_)": "ghs_" + "A1b2C3d4" * 5,
    "GitHub PAT (ghp_)": "ghp_" + "Z9y8X7w6" * 5,
    "GitHub fine-grained PAT (github_pat_)": "github_pat_" + "Q1w2E3r4" * 6,
    "Bearer header": "Authorization: Bearer " + "opaque" * 8,
    "Google API key (AIza)": "AIza" + "Sy" * 18 + "Q",
    "Slack bot token (xoxb-)": "xoxb-1234567890-" + "abcdefghij" * 2,
}

#: The worker's own command line, and the transcript line it produces.
CURL_COMMAND = f"curl -sS -H 'Authorization: Bearer {TOKEN}' https://example.invalid/v1/x"
WORKER_TASK_ID = "t_verify"
CLUSTER_NAME = "prod-eu-1"
VERIFY_SALT = "verify-salt"
#: A shape Hermes' prefix list masks (``hf_``) and the shared module's credential
#: list does not, so masking it in a tool-call argument proves the forced Hermes
#: layer ran there; the premise is asserted before the checks that rest on it.
HERMES_ONLY_TOKEN = "hf_" + "AbCdEfGhIj" * 3
POD_IP = "10.1.2.3"
RULES_YAML = (
    "ip:\n  action: pseudonym\nrules:\n"
    f"  - name: cluster-name\n    literal: {CLUSTER_NAME}\n    action: mask\n"
)
CHILD_TIMEOUT_SECONDS = 60
#: The duration a rendered completion line is given; any value renders.
CUTE_DURATION_SECONDS = 0.4
#: How much of an offending line, or of a transcript tail, a failure prints.
DETAIL_CHARS = 120
TAIL_CHARS = 200

#: Every print path a worker writes the transcript through, exercised by the
#: child below: ``print``, ``sys.stderr.write``, a logging StreamHandler on
#: stderr, ``sys.stdout.buffer``, and prompt_toolkit's ``print_formatted_text``
#: -- the one ``cli._cprint`` uses for the ``💻 $`` line, which caches its
#: output object and writes through ``buffer`` when the stream has one.
CHILD_SCRIPT = r"""
import logging, os, sys
from agent.credential_redaction import install_or_report
installed = install_or_report()
token = sys.argv[1]
print("stdout-print " + token)
sys.stderr.write("stderr-write " + token + "\n")
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(message)s"))
log = logging.getLogger("verify-child")
log.addHandler(handler)
log.setLevel(logging.INFO)
log.info("stderr-logging %s", token)
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI
print_formatted_text(ANSI("\x1b[2m┊ 💻 $         curl -H 'Authorization: Bearer " + token + "'\x1b[0m"))
sys.stdout.buffer.write(("buffer-write " + token + "\n").encode("utf-8"))
sys.stdout.write("split-a " + token[:9])
sys.stdout.write(token[9:] + " split-b\n")
sys.stdout.write("plain line, no secret, Bearer as a word\n")
sys.stdout.write("unterminated " + token)
print("installed=%r type=%s" % (installed, type(sys.stdout).__name__), file=sys.stderr)
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
"""

CHILD_UNWRAPPED_SCRIPT = r"""
import sys
from agent.credential_redaction import install_or_report
print("installed=%r type=%s" % (install_or_report(), type(sys.stdout).__name__))
"""


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


def leaked(text: str) -> bool:
    return LEAK_RE.search(text) is not None


# --- 1. The wiring resolved --------------------------------------------------
print("import wiring:")
import agent.redact as R  # noqa: E402
import agent.credential_redaction as C  # noqa: E402

check(
    "agent.redact registered the kube-agents pattern at import",
    C.GCP_OAUTH_TOKEN_PATTERN in R._plugin_patterns(),
    f"plugin patterns: {R._plugin_patterns()!r}",
)
check(
    "the pattern is attributed to kube-agents",
    C.GCP_OAUTH_TOKEN_PATTERN in R._PLUGIN_PREFIX_PATTERNS.get(C.PATTERN_SOURCE, []),
)
check(
    "the pre-screen knows the ya29 prefix",
    "ya29" in R._PREFIX_SUBSTRINGS,
)

main_source = Path("hermes_cli/main.py").read_text()
main_def = [
    node
    for node in ast.parse(main_source).body
    if isinstance(node, ast.FunctionDef) and node.name == "main"
]
first_statement = main_def[0].body[1] if main_def and len(main_def[0].body) > 1 else None
check(
    "the transcript installer is main()'s first statement after the docstring",
    first_statement is not None
    and isinstance(first_statement, ast.Try)
    and "install_or_report" in ast.unparse(first_statement),
    ast.unparse(first_statement)[:DETAIL_CHARS] if first_statement is not None else "no main()",
)

# --- 2. The token is masked at every in-process sink -------------------------
print("in-process masking:")
masked = R.redact_sensitive_text(TOKEN)
check("redact_sensitive_text masks a bare token", not leaked(masked), masked[:DETAIL_CHARS])
check("the mask keeps 6 head and 4 tail characters", masked == TOKEN_MASK, masked[:DETAIL_CHARS])
check(
    "an exported token is masked",
    not leaked(R.redact_sensitive_text(f"export TOKEN={TOKEN}")),
)
check(
    "a token in a URL is masked",
    not leaked(R.redact_sensitive_text(f"https://example.invalid/?access_token={TOKEN}")),
)
check(
    "the opt-out does not reopen a forced call",
    not leaked(R.redact_sensitive_text(TOKEN, force=True)),
)

formatter = R.RedactingFormatter("%(levelname)s %(message)s")
record = logging.LogRecord("verify", logging.INFO, __file__, 0, "token %s", (TOKEN,), None)
check("RedactingFormatter masks the token in agent.log", not leaked(formatter.format(record)))

from agent.display import (  # noqa: E402
    build_tool_preview,
    get_cute_tool_message,
    redact_tool_args_for_display,
)

cute = get_cute_tool_message("terminal", {"command": CURL_COMMAND}, CUTE_DURATION_SECONDS)
check("the terminal completion line masks the bearer token", not leaked(cute), cute[:DETAIL_CHARS])
check("the completion line still names the command", "curl -sS -H" in cute, cute[:DETAIL_CHARS])
check(
    "the completion line still shows the redacted header",
    "Authorization: Bearer" in cute,
    cute[:DETAIL_CHARS],
)
preview = build_tool_preview("terminal", {"command": CURL_COMMAND})
check("the tool preview masks the bearer token", preview is not None and not leaked(preview))
code_args = redact_tool_args_for_display("execute_code", {"code": f"TOKEN = '{TOKEN}'"})
check("execute_code source is masked", not leaked(code_args["code"]))
plain = {"command": "kubectl get pods -A"}
check(
    "a command without a secret is returned as it was",
    redact_tool_args_for_display("terminal", plain) == plain,
)
typed = redact_tool_args_for_display("browser_type", {"text": TOKEN})
check("the upstream browser_type branch still masks", not leaked(typed["text"]))

for label, sample in FLOOR_SHAPES.items():
    out = R.redact_sensitive_text(sample)
    check(f"floor shape masks: {label}", out != sample, out)

# --- 3. A real worker-shaped child -------------------------------------------
print("worker transcript:")


def run_child(script: str, *args: str, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    env.update(env_extra or {})
    with tempfile.NamedTemporaryFile("rb", suffix=".log", delete=False) as log_f:
        log_path = Path(log_f.name)
    with open(log_path, "ab") as log_f:
        proc = subprocess.run(
            [sys.executable, "-c", script, *args],
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=os.getcwd(),
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    check(f"child exited 0 (rc={proc.returncode})", proc.returncode == 0)
    text = log_path.read_text(errors="replace")
    log_path.unlink()
    return text


transcript = run_child(CHILD_SCRIPT, TOKEN, env_extra={"HERMES_KANBAN_TASK": WORKER_TASK_ID})
check("the worker installed the wrapper", "installed=True" in transcript, transcript[-TAIL_CHARS:])
check(
    "the transcript holds no 20+ character ya29 run",
    not leaked(transcript),
    next((line[:DETAIL_CHARS] for line in transcript.splitlines() if leaked(line)), ""),
)
lines = transcript.splitlines()
for path in (
    "stdout-print",
    "stderr-write",
    "stderr-logging",
    "buffer-write",
    "split-a",
    "unterminated",
):
    matching = [line for line in lines if path in line]
    check(
        f"the {path} line carries the mask, not the token",
        len(matching) == 1 and TOKEN_MASK in matching[0],
        matching[0][:DETAIL_CHARS] if matching else "line absent",
    )
# The prefix mask leaves a 13-character stub, which the Authorization-header
# rule then treats as a short value and masks whole: the header line reads
# ``Bearer ***`` rather than carrying the 6-head/4-tail stub.
header_lines = [line for line in lines if "💻 $" in line]
check(
    "the 💻 $ line reached the transcript through prompt_toolkit, redacted",
    len(header_lines) == 1 and "Bearer ***" in header_lines[0],
    header_lines[0][:DETAIL_CHARS] if header_lines else "line absent",
)
check(
    "a plain line is untouched, Bearer as a word included",
    "plain line, no secret, Bearer as a word\n" in transcript,
)

opted_out = run_child(
    CHILD_SCRIPT,
    TOKEN,
    env_extra={"HERMES_KANBAN_TASK": WORKER_TASK_ID, "HERMES_REDACT_SECRETS": "false"},
)
check("security.redact_secrets: false does not reopen the transcript", not leaked(opted_out))

bare_env = {key: value for key, value in os.environ.items() if key != "HERMES_KANBAN_TASK"}
with tempfile.NamedTemporaryFile("w+", suffix=".log") as out_f:
    proc = subprocess.run(
        [sys.executable, "-c", CHILD_UNWRAPPED_SCRIPT],
        stdout=out_f,
        stderr=subprocess.STDOUT,
        env=bare_env,
        cwd=os.getcwd(),
        timeout=CHILD_TIMEOUT_SECONDS,
    )
    out_f.seek(0)
    unwrapped = out_f.read()
check(
    "a process without HERMES_KANBAN_TASK keeps sys.stdout unwrapped",
    proc.returncode == 0 and "installed=False type=TextIOWrapper" in unwrapped,
    unwrapped[-TAIL_CHARS:],
)

# --- 4. The module itself needs no Hermes to import --------------------------
print("standalone import:")
buffer = io.StringIO()
stream = C.RedactingTextStream(buffer, lambda text: text.replace("secret", "***"))
stream.write("a secret split ")
stream.write("across writes\nand a partial")
check("a line is written once its newline arrives", buffer.getvalue() == "a *** split across writes\n")
stream.flush()
check("flush carries the partial line", buffer.getvalue().endswith("and a partial"))

print()
# --- 5. The operator's rules on disk (issue #1985) ---------------------------
print("operator rules:")
RULES_CHILD_SCRIPT = r"""
import json, os, sqlite3, sys
from pathlib import Path
from agent.redact import redact_sensitive_text
token, cluster, ip = sys.argv[1], sys.argv[2], sys.argv[3]
print("wrapped=" + str(bool(getattr(redact_sensitive_text, "_kube_agents_operator_rules", False))))
print("text=" + redact_sensitive_text(f"deploy {token} to {cluster} at {ip}"))
from hermes_state import SessionDB
home = Path(os.environ["HERMES_HOME"])
db = SessionDB(home / "state.db")
db.create_session("s_verify", "cli")
hermes_only = sys.argv[4]
calls = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps(
    {"command": f"curl -H 'Authorization: Bearer {token}' -H 'X-Hermes: {hermes_only}' https://{ip}/"})}}]
db.append_message("s_verify", "assistant", content=f"token {token} on {cluster}", tool_calls=calls)
db.append_message("s_verify", "tool", content=[{"type": "text", "text": f"{cluster} {ip}"}],
                  tool_call_id="c1", tool_name="terminal")
rows = sqlite3.connect(home / "state.db").execute("SELECT content, tool_calls FROM messages").fetchall()
print("state=" + json.dumps(rows))
from hermes_cli import kanban_db
from hermes_cli.kanban_db_connect import connect, init_db
board = home / "kanban" / "kanban.db"
init_db(board)
conn = connect(board)
task_id = kanban_db.create_task(conn, title=f"ask about {cluster}", body=f"token {token}", created_by="verify")
kanban_db.complete_task(conn, task_id, result=f"done on {cluster} at {ip} with {token}",
                        summary=f"summary {token}", fire_lifecycle_hook=False)
row = conn.execute("SELECT title, body, result FROM tasks WHERE id = ?", (task_id,)).fetchone()
summary = [list(r) for r in conn.execute("SELECT summary FROM task_runs WHERE task_id = ?", (task_id,)).fetchall()]
comment_id = kanban_db.add_comment(conn, task_id, "verify", f"comment {token} on {cluster} at {ip}")
comment = conn.execute("SELECT body FROM task_comments WHERE id = ?", (comment_id,)).fetchone()[0]
blocked_id = kanban_db.create_task(conn, title="second card", body="plain", created_by="verify")
kanban_db.block_task(conn, blocked_id, reason=f"blocked on {cluster} ({ip}) with {token}")
blocked = [list(r) for r in conn.execute("SELECT summary FROM task_runs WHERE task_id = ?", (blocked_id,)).fetchall()]
events = [str(tuple(r)) for r in conn.execute("SELECT * FROM task_events WHERE task_id = ?", (blocked_id,)).fetchall()]
print("kanban=" + json.dumps([list(row), summary, comment, blocked, events]))
"""
REDACTOR_MODULE = os.environ.get("KUBE_AGENTS_REDACTOR_MODULE") or C.DEFAULT_REDACTOR_MODULE
check("the shared redactor module is present (KUBE_AGENTS_REDACTOR_MODULE, else the image's path)", Path(REDACTOR_MODULE).is_file(), REDACTOR_MODULE)
# The premise of the forced-leaves checks: a shape Hermes' prefix list masks and the
# shared module does not, so masking it in a tool-call argument proves the Hermes layer ran.
_spec = importlib.util.spec_from_file_location("verify_redactor", REDACTOR_MODULE)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
check("the shared module leaves the Hermes-only shape alone (premise)", HERMES_ONLY_TOKEN in _module.AuditRedactor.redact_text(f"x {HERMES_ONLY_TOKEN} y"), HERMES_ONLY_TOKEN)
check("the Hermes layer masks the Hermes-only shape (premise)", HERMES_ONLY_TOKEN not in R.redact_sensitive_text(f"x {HERMES_ONLY_TOKEN} y", force=True), HERMES_ONLY_TOKEN)


def run_rules_child(rule_file: str, home: str, opt_out: bool = False) -> str:
    env_extra = {"HERMES_HOME": home, "SESSION_KV_SALT": VERIFY_SALT, "KUBE_AGENTS_REDACTOR_MODULE": REDACTOR_MODULE}
    if rule_file:
        env_extra["KUBE_AGENTS_REDACTION_CONFIG"] = rule_file
    if opt_out:
        env_extra["HERMES_REDACT_SECRETS"] = "false"
    return run_child(RULES_CHILD_SCRIPT, TOKEN, CLUSTER_NAME, POD_IP, HERMES_ONLY_TOKEN, env_extra=env_extra)


with tempfile.TemporaryDirectory() as rules_home:
    rule_file = Path(rules_home) / "redaction.yaml"
    rule_file.write_text(RULES_YAML)
    on = run_rules_child(str(rule_file), rules_home)
check("the module-level redactor is the composed form", "wrapped=True" in on, on[-TAIL_CHARS:])
text_line = next((line for line in on.splitlines() if line.startswith("text=")), "")
check("the operator's literal rule masks the cluster name", "[REDACTED_CLUSTER_NAME]" in text_line, text_line)
check("the IP rule pseudonymises the address", POD_IP not in text_line and "[ip:" in text_line, text_line)
check("the credential masking still runs first", not leaked(text_line) and TOKEN_MASK in text_line, text_line)
state_line = next((line for line in on.splitlines() if line.startswith("state=")), "")
check("state.db holds no token, cluster name or address", state_line and not leaked(state_line)
      and CLUSTER_NAME not in state_line and POD_IP not in state_line, state_line[:DETAIL_CHARS])
check("the Hermes-only token in the tool-call arguments is masked by the forced Hermes layer", HERMES_ONLY_TOKEN not in state_line, state_line[:DETAIL_CHARS])
check("state.db still holds the tool call's shape", '\\"name\\": \\"terminal\\"' in state_line, state_line[:DETAIL_CHARS])
kanban_line = next((line for line in on.splitlines() if line.startswith("kanban=")), "")
check("the board holds no token, cluster name or address", kanban_line and not leaked(kanban_line)
      and CLUSTER_NAME not in kanban_line and POD_IP not in kanban_line, kanban_line[:DETAIL_CHARS])
check("the board's result and summary were written", "done on" in kanban_line and "summary" in kanban_line, kanban_line[:DETAIL_CHARS])
check("a comment body and a block reason were written, redacted", "comment" in kanban_line and "blocked on" in kanban_line, kanban_line[:DETAIL_CHARS])

with tempfile.TemporaryDirectory() as opted_out_home:
    opted_out_file = Path(opted_out_home) / "redaction.yaml"
    opted_out_file.write_text(RULES_YAML)
    opted = run_rules_child(str(opted_out_file), opted_out_home, opt_out=True)
opted_state = next((line for line in opted.splitlines() if line.startswith("state=")), "")
check("security.redact_secrets: false does not reopen state.db: no token", opted_state and not leaked(opted_state), opted_state[:DETAIL_CHARS])
check("security.redact_secrets: false does not reopen the argument leaves: no Hermes-only token", opted_state and HERMES_ONLY_TOKEN not in opted_state, opted_state[:DETAIL_CHARS])
check("security.redact_secrets: false does not reopen the operator's rules", CLUSTER_NAME not in opted_state and POD_IP not in opted_state, opted_state[:DETAIL_CHARS])

with tempfile.TemporaryDirectory() as plain_home:
    off = run_rules_child("", plain_home)
check("without the variable the redactor is upstream's", "wrapped=False" in off, off[-TAIL_CHARS:])
off_text = next((line for line in off.splitlines() if line.startswith("text=")), "")
check("without the variable the cluster name and address pass", CLUSTER_NAME in off_text and POD_IP in off_text, off_text)
off_state = next((line for line in off.splitlines() if line.startswith("state=")), "")
check("without the variable state.db is written as before", leaked(off_state) and CLUSTER_NAME in off_state, off_state[:DETAIL_CHARS])
off_kanban = next((line for line in off.splitlines() if line.startswith("kanban=")), "")
check("without the variable the board is written as before", leaked(off_kanban) and CLUSTER_NAME in off_kanban, off_kanban[:DETAIL_CHARS])

with tempfile.TemporaryDirectory() as bad_home:
    bad_file = Path(bad_home) / "redaction.yaml"
    bad_file.write_text("rules:\n  - name: bad\n    literal: ''\n")
    bad_env = {k: v for k, v in os.environ.items()}
    bad_env.update({"HERMES_HOME": bad_home, "KUBE_AGENTS_REDACTION_CONFIG": str(bad_file), "KUBE_AGENTS_REDACTOR_MODULE": REDACTOR_MODULE})
    bad = subprocess.run([sys.executable, "-c", "import agent.redact"], capture_output=True, text=True, env=bad_env, cwd=os.getcwd(), timeout=CHILD_TIMEOUT_SECONDS)
check("a rule file that does not parse stops the import", bad.returncode != 0 and "KUBE_AGENTS_REDACTION_CONFIG" in bad.stderr, bad.stderr[-TAIL_CHARS:])
print()

if FAILURES:
    print(f"verify_credential_redaction: {len(FAILURES)} FAILED")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("verify_credential_redaction: all checks passed")

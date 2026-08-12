#!/usr/bin/env python3
"""Small HTTP resolver for platform session metadata."""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict
from contextlib import closing

import logging

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from agent_common_server import _run_env, CONFIG_PATH, DOTENV_PATH

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("session_kv_server")

try:
    import dotenv
    dotenv.load_dotenv(DOTENV_PATH)
except Exception:
    pass

# The schema is not published: this server has exactly three known callers, all
# of them inside this pod, and an interactive /docs page on a port that carries
# chat identifiers is a browsable index of them.
app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

SESSION_KV_DB_PATH = os.getenv("SESSION_KV_DB_PATH", "/var/lib/kube-agents/session/session_kv.db")
CLEANUP_TTL_DAYS = int(os.getenv("SESSION_KV_CLEANUP_TTL_DAYS", "14"))

# Deliberately not API_SERVER_KEY. That value is the loopback sentinel
# `cluster-internal-trusted` — a marker, not a secret — so reusing it here would
# authenticate nothing. See docs/credential-isolation-design.md.
SESSION_KV_API_KEY_ENV = "SESSION_KV_API_KEY"


def _expected_api_key() -> str:
    # Read per request rather than at import: the value arrives from the pod
    # environment, and tests set it around individual calls.
    return (os.getenv(SESSION_KV_API_KEY_ENV) or "").strip()


def _presented_api_key(authorization: str, x_api_key: str) -> str:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    return (x_api_key or "").strip()


def verify_api_key(
    authorization: str = Header(default=""),
    x_api_key: str = Header(default=""),
) -> None:
    """Reject callers that cannot present the pod's session-KV key.

    Fails closed when the key is unset. Every caller — the event watcher, the
    MCP server, the incident_context plugin — gets the value from the same pod
    secret, so an empty variable means the deployment is misconfigured, and
    serving chat identifiers to an unauthenticated caller is the worse of the
    two outcomes.
    """
    expected = _expected_api_key()
    if not expected:
        logger.error(
            "%s is not set — refusing every authenticated request. "
            "Re-run provisioning so the pod secret carries a session KV key.",
            SESSION_KV_API_KEY_ENV,
        )
        raise HTTPException(status_code=503, detail="session KV authentication is not configured")

    # Compared as bytes: Starlette decodes header values as latin-1, so any byte
    # in 0x80–0xFF arrives as a non-ASCII `str` and `compare_digest` raises
    # TypeError on those — escaping the dependency as a 500 with a traceback
    # instead of the 401 this route is specified to return.
    presented = _presented_api_key(authorization, x_api_key)
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# Identity fields that predate pseudonymisation. `user_id` is only plaintext on
# Google Chat, where it *is* the address, so it is matched on content rather
# than dropped outright — a Slack member id is opaque and stays.
_PLAINTEXT_IDENTITY_KEYS = ("user_email",)


def _purge_plaintext_identities(conn: sqlite3.Connection) -> None:
    """Strip plaintext identities left in rows written before this change.

    Stripping rather than deleting: the row also carries `chat_id`/`thread_id`,
    and dropping it would break threaded replies for conversations that are
    still open.

    The hash is not recomputed, and the reason is not container topology: this
    server runs in the sandbox container, which does carry `SESSION_KV_SALT`.
    It is that the *fallback* instance — the one `start_session_kv_server()` in
    platform_mcp_server.py spawns — inherits the stdio MCP allowlist in
    agents/platform/config.yaml, which names `SESSION_KV_API_KEY` and not the
    salt. Rehashing on that path would write a digest under some other salt,
    stored permanently and uncorrelated with every hash the Chat Agent plugins
    produce — worse than an absent value, because dropping the field costs one
    message's worth of identity and no more: the plugins rewrite the hash on
    the user's next turn.
    """
    try:
        rows = conn.execute("SELECT session_id, metadata FROM session_metadata").fetchall()
    except sqlite3.Error as exc:
        logger.error(f"Failed to scan session metadata for plaintext identities: {exc}")
        return

    purged = 0
    for session_id, raw in rows:
        try:
            metadata = json.loads(raw)
        except Exception:
            continue
        if not isinstance(metadata, dict):
            continue

        changed = False
        for key in _PLAINTEXT_IDENTITY_KEYS:
            if metadata.pop(key, None) is not None:
                changed = True
        if "@" in str(metadata.get("user_id") or ""):
            metadata.pop("user_id", None)
            changed = True
        if not changed:
            continue

        try:
            conn.execute(
                "UPDATE session_metadata SET metadata = ? WHERE session_id = ?",
                (json.dumps(metadata, sort_keys=True), session_id),
            )
            purged += 1
        except sqlite3.Error as exc:
            logger.error(f"Failed to purge plaintext identity from session {session_id}: {exc}")

    if purged:
        logger.info(f"Purged plaintext identity fields from {purged} session metadata row(s)")


def init_db() -> None:
    db_dir = os.path.dirname(SESSION_KV_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        with conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_metadata (
                    session_id TEXT PRIMARY KEY,
                    metadata TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    chat_id   TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    report    TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            _purge_plaintext_identities(conn)






def cleanup_old_records(conn: sqlite3.Connection) -> None:
    try:
        # Delete incident reports and session metadata older than CLEANUP_TTL_DAYS
        param = f"-{CLEANUP_TTL_DAYS} days"
        conn.execute("DELETE FROM incidents WHERE created_at < datetime('now', ?)", (param,))
        conn.execute("DELETE FROM session_metadata WHERE updated_at < datetime('now', ?)", (param,))
    except Exception as exc:
        logger.error(f"Failed to clean up old DB records: {exc}")


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    """Unauthenticated on purpose: it returns no data and gates the others."""
    return {"status": "ok"}


@app.post("/sessions", status_code=201, dependencies=[Depends(verify_api_key)])
def create_session() -> Dict[str, str]:
    """Create a new session ID for the incoming incident."""
    session_id = f"k8s-evt-{uuid.uuid4().hex[:8]}"
    
    # Save the session to the local metadata DB
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                (session_id, json.dumps({"platform": "k8s-watcher", "created_at": datetime.now(timezone.utc).isoformat()}))
            )
            cleanup_old_records(conn)
    return {"sessionID": session_id}


def clean_workload_name(kind: str, name: str) -> str:
    if kind.lower() == "pod":
        # Match pattern of deployment replica (e.g. -6cfdb6b98b-zwv24)
        m = re.match(r"^(.*?)-[a-f0-9]{8,10}-[a-z0-9]{5}$", name)
        if m:
            return m.group(1)
        # Match pattern of statefulset/job/pod replica (e.g. -0 or -abcde)
        m = re.match(r"^(.*?)-[a-z0-9]{5}$", name)
        if m:
            return m.group(1)
    return name


def clean_reason_label(reason: str) -> str:
    # E.g. FailedToDrainNode -> Failed to drain node
    s = re.sub(r'(?<!^)(?=[A-Z])', ' ', reason).lower()
    return s.capitalize()


def clean_event_message(message: str) -> str:
    msg = message.replace("PodDisruptionBudget", "PDB")
    # Simplify PDB eviction violation message. The namespace segment excludes
    # whitespace so it cannot overlap the preceding `\s+`: two adjacent
    # quantifiers that can match the same characters make the engine try every
    # split point, which is quadratic on hostile input (CodeQL py/polynomial-redos).
    m = re.search(r"cannot be evicted:\s*would violate PDB\s+(?:[^\s/]+/)?([a-zA-Z0-9_-]+)", msg)
    if m:
        clean_pdb = m.group(1)
        return f"Eviction would violate PDB {clean_pdb}"
    return msg


def get_severity_details(event_type: str, reason: str) -> tuple[str, str]:
    event_lower = event_type.lower()
    reason_lower = reason.lower()
    
    # Blocker if it blocks drain, eviction, or scheduling
    is_blocker = (
        event_lower == "warning" and 
        any(x in reason_lower for x in ("drain", "evict", "schedul", "capacity", "oomkilled", "crashloopbackoff", "failedmount"))
    )
    
    if is_blocker:
        return "🔴", "Critical"
    elif event_lower == "warning":
        return "🟡", "Warning"
    else:
        return "🔵", "Info"



def get_active_platform() -> str:
    try:
        import yaml
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        platforms = cfg.get("platforms", {})
        if platforms.get("slack", {}).get("enabled"):
            return "slack"
        if platforms.get("google_chat", {}).get("enabled"):
            return "google_chat"
    except Exception as exc:
        logger.error(f"Failed to parse config.yaml for active platform: {exc}")
    if os.environ.get("SLACK_BOT_TOKEN"):
        return "slack"
    return "google_chat"


def _post_initial_alert(active_platform: str, alert_msg: str) -> str | None:
    """Send initial warning alert via hermes CLI and return the thread/message ID."""
    try:
        res = subprocess.run(
            ["hermes", "send", "--json", "--to", active_platform, alert_msg],
            check=True,
            capture_output=True,
            text=True,
            env=_run_env()
        )
        resp = json.loads(res.stdout)
        msg_id = resp.get("message_id", "")
        if msg_id:
            # Google Chat message IDs contain space and message parts; we extract the thread key.
            if active_platform == "google_chat" and "/messages/" in msg_id:
                space_part, msg_part = msg_id.split("/messages/", 1)
                thread_key = msg_part.split(".")[0]
                return f"{space_part}/threads/{thread_key}"
            return msg_id
    except subprocess.CalledProcessError as exc:
        logger.error(f"Failed to post warning alert. Stdout: {exc.stdout}. Stderr: {exc.stderr}. Exc: {exc}")
    except Exception as exc:
        logger.error(f"Failed to post warning alert or parse message_id response: {exc}")
    return None


def _register_session_routing(session_id: str, platform: str, thread_id: str) -> None:
    """Save thread configurations in session_metadata SQLite table."""
    try:
        with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
            with conn:
                row = conn.execute(
                    "SELECT metadata FROM session_metadata WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
                if row:
                    meta = json.loads(row[0])
                    meta["thread_id"] = thread_id
                    if platform == "slack":
                        meta["chat_id"] = os.environ.get("SLACK_HOME_CHANNEL", "")
                    else:
                        meta["chat_id"] = thread_id.split("/threads/")[0]
                    
                    # Update SQLite metadata table
                    conn.execute(
                        "UPDATE session_metadata SET metadata = ? WHERE session_id = ?",
                        (json.dumps(meta), session_id)
                    )
    except Exception as exc:
        logger.error(f"Failed to update session metadata with thread_id: {exc}")


def _create_gateway_session(api_url: str, session_id: str, headers: Dict[str, str]) -> bool:
    """POST request to local gateway API to initialize the troubleshooting session ID."""
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions",
            data=json.dumps({"session_id": session_id, "title": f"Triage {session_id}"}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:  # 409 Conflict means it already exists, which is acceptable
            return True
        logger.error(f"Failed to create gateway API session (code {exc.code}): {exc.read().decode()}")
    except Exception as exc:
        logger.error(f"Failed to connect to gateway API server: {exc}")
    return False


def _build_agent_query(session_id: str, payload: Dict[str, Any]) -> str:
    """Format a detailed Markdown diagnostic query for the Platform Agent.

    The report template below is STANDARD markdown, and must stay that way.
    Every chat platform's adapter translates the agent's markdown on the way
    out; on Slack that is ``SlackAdapter.format_message``, which rewrites
    ``**bold**`` to ``*bold*`` and ``[label](url)`` to ``<url|label>``. Writing
    the template in the destination's own syntax does not skip that pass, it
    feeds it: a pre-authored ``*Issue:*`` matches format_message's single-
    asterisk ITALIC rule and every heading in the delivered report came out
    italic instead of bold. Authoring in markdown also lets the Block Kit
    renderer (``platforms.slack.extra.rich_blocks`` in agents/chat/config.yaml)
    see the structure and emit real header, list and table blocks.
    """
    event_reason = payload.get("reason") or "Unknown"
    namespace = payload.get("namespace") or "default"
    object_kind = payload.get("kind_of_object") or payload.get("kindOfObject") or "Pod"
    object_name = payload.get("name") or ""
    message = payload.get("message") or ""
    cluster_name = payload.get("cluster") or os.environ.get("GKE_CLUSTER_NAME", "platform-agent-host")
    gcp_project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GCP_PROJECT") or ""
    workloads_project_query = f"?project={gcp_project}" if gcp_project else ""
    logs_project_query = f";project={gcp_project}" if gcp_project else ""

    return (
        f"Analyze the following Kubernetes event warning on GKE cluster '{cluster_name}' "
        f"for the active session '{session_id}'.\n\n"
        f"**Event Details:**\n"
        f"- **Resource:** {namespace}/{object_kind}/{object_name}\n"
        f"- **Event Reason:** {event_reason}\n"
        f"- **Warning Message:** {message}\n\n"
        f"When calling your send_notification tool to report findings, you MUST pass this exact session ID: '{session_id}' as the session_id argument so it routes as a threaded reply to the warning alert.\n\n"
        f"Propose as many GitOps remediation options as the root cause genuinely warrants — one is fine if there is only one sound fix; do not invent filler alternatives to pad the list. "
        f"Label them 'Option A', 'Option B', ... in order. When you propose more than one, mark exactly one of them '✅ **Recommended: Option <letter>**' — the safest, most durable fix for the root cause "
        f"(favor correctness and least blast radius over quick mitigations). When there is only one option, omit the Recommended line and drop the 'apply Option <letter>' override from the call-to-action, since a bare 'apply' is unambiguous.\n\n"
        f"The template below shows two Option lines as an example of the shape — repeat or drop that line to match the number of options you actually propose, and name those same letters in the call-to-action. "
        f"Every <...> in the template is a placeholder: fill each one in. The posted report must never contain a literal '<letter>'.\n\n"
        f"When done, post your final diagnostic report to the chat platform (using your notification tool) formatted exactly like this:\n\n"
        f"📋 **Incident Triage**\n\n"
        f"- **Issue:** <Short 1-sentence description of the problem>\n"
        f"- **Root Cause:** <Key constraint mismatch or log finding in 1-2 sentences>\n\n"
        f"🛠️ **Proposed Fixes (GitOps):**\n\n"
        f"- **Option A (<Action Title>):** <1-sentence description of Option A GitOps fix>.\n"
        f"- **Option B (<Action Title>):** <1-sentence description of Option B GitOps fix>.\n\n"
        f"✅ **Recommended: Option <letter>** — <1-sentence why this is the safer/better choice>.\n\n"
        f"🔗 [GKE Workloads](https://console.cloud.google.com/kubernetes/workload/overview{workloads_project_query}) | "
        f"[Cloud Logs](https://console.cloud.google.com/logs/query;query=resource.type%3D%22k8s_container%22{logs_project_query})\n\n"
        f"👉 **Reply 'apply' to open a GitOps Pull Request with the recommended fix, or name one directly with 'apply Option A' / 'apply Option B'.**\n\n"
        f"---"
        f"\n\n**GitOps PR Instructions (For subsequent turns if the user replies):**\n"
        f"If the user replies to the thread with 'apply' or 'apply Option <letter>':\n"
        f"1. A bare 'apply' (or 'apply recommended') means apply the option you marked '✅ **Recommended: Option <letter>**', or the only option you proposed if there was just one. You are explicitly authorized to create a new branch, modify the resource manifests in the local checkout, commit, push, and open a GitHub Pull Request matching the selected option.\n"
        f"2. Post a threaded response confirming the PR was created and include the clickable PR link.\n"
        f"3. Do not execute any write mutations (kubectl scale, patch, or apply) directly on the live cluster."
    )


def _start_agent_turn(api_url: str, session_id: str, query: str, headers: Dict[str, str]) -> None:
    """Post the agent query request to execute the diagnostic reasoning loop."""
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions/{session_id}/chat",
            data=json.dumps({"message": query}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=300.0) as resp:
            if resp.status != 200:
                logger.error(f"Gateway API chat execution failed (status {resp.status})")
    except Exception as exc:
        logger.error(f"Failed to call gateway API chat execution: {exc}")


def trigger_agent_troubleshooter(session_id: str, alert_msg: str, payload: Dict[str, Any]) -> None:
    """Post warning alert to Chat, configure thread mapping, and trigger the agent loop in background."""
    active_platform = get_active_platform()
    
    # 1. Post initial warning notification to Google Chat or Slack
    thread_id = _post_initial_alert(active_platform, alert_msg)
    
    # 2. Register thread-to-session mappings for two-way chat routing
    if thread_id:
        _register_session_routing(session_id, active_platform, thread_id)

    # 3. Configure HTTP authentication headers for Hermes REST gateway
    api_url = os.environ.get("PLATFORM_API_URL", "http://127.0.0.1:8642")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("API_SERVER_KEY", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # 4. Instantiate the session in Platform Gateway
    session_created = _create_gateway_session(api_url, session_id, headers)
    if not session_created:
        logger.error(f"Aborting troubleshooting trigger: session creation failed for {session_id}")
        return

    # 5. Formulate instructions query and execute the agent turn
    agent_query = _build_agent_query(session_id, payload)
    _start_agent_turn(api_url, session_id, agent_query, headers)


@app.post("/sessions/{session_id}/inject", dependencies=[Depends(verify_api_key)])
def inject_message(session_id: str, request_data: Dict[str, Any], background_tasks: BackgroundTasks) -> Dict[str, str]:
    """Receive the event payload and notify the Platform Agent via Google Chat."""
    raw_message = request_data.get("message", "")
    if not raw_message:
        raise HTTPException(status_code=400, detail="message field is required")
        
    try:
        payload = json.loads(raw_message)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse inner payload JSON: {exc}")
        
    event_reason = payload.get("reason") or "Unknown"
    namespace = payload.get("namespace") or "default"
    object_kind = payload.get("kind_of_object") or payload.get("kindOfObject") or "Pod"
    object_name = payload.get("name") or ""
    message = payload.get("message") or ""
    count = payload.get("count") if payload.get("count") is not None else 1
    event_type = payload.get("type") or "Warning"

    severity_emoji, severity_label = get_severity_details(event_type, event_reason)
    clean_name = clean_workload_name(object_kind, object_name)
    clean_reason = clean_reason_label(event_reason)
    clean_msg = clean_event_message(message)

    # Construct a pretty notification alert. Standard markdown, not Slack
    # mrkdwn: SlackAdapter.format_message runs over everything on its way out,
    # and it reads a single `*...*` as ITALIC. A label written `*Critical:*`
    # therefore arrives italic, which is the opposite of the emphasis intended.
    # `**Critical:**` is what becomes bold. (`_..._` is italic in both, so the
    # second line needs no change.)
    alert_msg = (
        f"{severity_emoji} **{severity_label}:** {clean_reason} `{namespace}/{clean_name}` — {clean_msg}\n"
        f"🌱 _Digging down to the root cause..._"
    )
    
    # Delegate the heavy REST API call to FastAPI BackgroundTasks to keep response times sub-millisecond
    background_tasks.add_task(trigger_agent_troubleshooter, session_id, alert_msg, payload)
    
    return {"status": "injected"}


@app.get("/v1/sessions/{session_id}/metadata", dependencies=[Depends(verify_api_key)])
def get_metadata(session_id: str) -> Dict[str, Any]:
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        row = conn.execute(
            "SELECT metadata FROM session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session metadata not found")

    try:
        return json.loads(row[0])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Data decoding failure: {exc}")


@app.get("/v1/sessions", dependencies=[Depends(verify_api_key)])
def list_sessions(limit: int = 100) -> Dict[str, Any]:
    limit = max(1, min(limit, 1000))
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        rows = conn.execute(
            """
            SELECT session_id, metadata, updated_at
            FROM session_metadata
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    sessions = []
    for session_id, metadata, updated_at in rows:
        try:
            parsed = json.loads(metadata)
        except Exception:
            parsed = {}
        sessions.append(
            {
                "session_id": session_id,
                "metadata": parsed,
                "updated_at": updated_at,
            }
        )
    return {"sessions": sessions}


@app.post("/v1/incidents", dependencies=[Depends(verify_api_key)])
def store_incident(body: Dict[str, Any]) -> Dict[str, str]:
    chat_id, thread_id, report = body.get("chat_id"), body.get("thread_id"), body.get("report")
    if not (chat_id and thread_id and report):
        raise HTTPException(status_code=400, detail="chat_id, thread_id, report required")
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        with conn:
            # keep the FIRST report per thread (the one carrying the options)
            conn.execute(
                "INSERT OR IGNORE INTO incidents (chat_id, thread_id, report) VALUES (?, ?, ?)",
                (chat_id, thread_id, report),
            )
            cleanup_old_records(conn)
    return {"status": "stored"}


@app.get("/v1/incidents/by-thread", dependencies=[Depends(verify_api_key)])
def get_incident(chat_id: str, thread_id: str) -> Dict[str, str]:
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        row = conn.execute(
            "SELECT report FROM incidents WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="no incident for thread")
    return {"chat_id": chat_id, "thread_id": thread_id, "report": row[0]}


init_db()

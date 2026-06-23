import os
import re
import json
import sqlite3
import hashlib
import time
import logging
import string
from typing import Dict, Any, List, Set, Tuple

logger = logging.getLogger("hermes.plugin.client_redactor")

DB_PATH = "/opt/data/redaction_cache.db"
CLEANUP_INTERVAL = 7 * 24 * 3600  # 7 days in seconds

# IP address regex pattern
IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Domain/Hostname patterns (googlers.com, cluster.local, etc.)
HOSTNAME_PATTERN = re.compile(
    r"\b[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\.(?:googlers\.com|internal|local|cluster)\b"
)

# A static salt component to obfuscate hashes
HASH_SALT_PREFIX = "kube_agents_salt_"

# --- Spellcheck & Dictionary Redaction Configuration ---
DICTIONARY_PATH = "/usr/share/dict/words"
ENGLISH_WORDS: Set[str] = set()

# Pre-load the system dictionary on module initialization
if os.path.exists(DICTIONARY_PATH):
    try:
        with open(DICTIONARY_PATH, "r", encoding="utf-8") as f:
            ENGLISH_WORDS = {line.strip().lower() for line in f}
        logger.info(f"Loaded {len(ENGLISH_WORDS)} words from system dictionary for spellcheck redaction.")
    except Exception as e:
        logger.error(f"Failed to load system dictionary: {e}")
else:
    logger.warning(f"System dictionary not found at '{DICTIONARY_PATH}' — spellcheck redaction disabled.")

# Technical allowlist of Unix/Kubernetes commands, terms, and metadata attributes that should NEVER be redacted
TECHNICAL_ALLOWLIST: Set[str] = {
    # Unix & command line tools
    "kubectl", "gcloud", "helm", "docker", "git", "cat", "grep", "sed", "awk", "ls", "cd", "echo",
    "mkdir", "rm", "cp", "mv", "chmod", "chown", "systemctl", "journalctl", "apt", "apt-get", "python",
    "python3", "pip", "pip3", "curl", "wget", "bash", "sh", "sudo", "tar", "gzip", "gunzip", "zip", "unzip",
    # Kubernetes resource kinds & shortnames
    "pod", "pods", "service", "services", "svc", "deployment", "deployments", "deploy", "replicaset", "replicasets",
    "statefulset", "statefulsets", "daemonset", "daemonsets", "namespace", "namespaces", "node", "nodes",
    "configmap", "configmaps", "secret", "secrets", "pvc", "pv", "persistentvolume", "persistentvolumeclaim",
    "persistentvolumes", "persistentvolumeclaims", "ingress", "ingresses", "job", "jobs", "cronjob", "cronjobs",
    "clusterrole", "clusterrolebinding", "role", "rolebinding", "serviceaccount", "sa", "volume", "volumemount",
    "kubelet", "kube-system", "kube-public", "kube-node-lease", "agent-system", "env", "envfrom",
    # Kubernetes API / Manifest keywords
    "get", "describe", "apply", "delete", "logs", "exec", "rollout", "restart", "status", "port-forward",
    "create", "run", "image", "metadata", "spec", "labels", "annotations", "selector", "template",
    "containers", "resources", "limits", "requests", "cpu", "memory", "ephemeral-storage", "mountpath", "readonly",
    "volumeclaimtemplate", "volumeclaimtemplates", "storageclassname", "accessmodes", "resources", "requests",
    "securitycontext", "allowprivilegeescalation", "capabilities", "drop", "add", "runasuser", "runasgroup",
    "fsgroup", "seccompprofile", "type", "runtimedefault", "hostpath", "emptydir", "configmapref", "secretref",
    # Project specifics
    "hermes", "litellm", "otel", "fluent-bit", "yolo", "devteam", "operator", "platform", "hermes_otel",
    "compliance_critic", "session_resolver", "tool_overrides", "session_store", "delegate_workload"
}

PLACEHOLDER_PATTERN = re.compile(r"^GC[a-fA-F0-9]{6}$")
EMBEDDED_PLACEHOLDER_PATTERN = re.compile(r"GC[a-fA-F0-9]{6}")
WORD_TOKEN_PATTERN = re.compile(r"\b[a-zA-Z0-9_-]+\b")


def is_placeholder(word: str) -> bool:
    """Check if a word is already a generated platform-agent placeholder."""
    return bool(PLACEHOLDER_PATTERN.match(word))


def is_dictionary_word(word: str) -> bool:
    """Check if a word is a standard English dictionary word or a whitelisted technical keyword."""
    cleaned = word.strip(string.punctuation + string.digits).lower()
    if not cleaned:
        return True
    if len(cleaned) < 4:
        return True
    if cleaned in TECHNICAL_ALLOWLIST:
        return True
    return cleaned in ENGLISH_WORDS



def init_db() -> sqlite3.Connection:
    """Initialize the SQLite redaction cache database and create the table."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # Enable WAL mode for concurrency
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS redaction_map (
            placeholder TEXT PRIMARY KEY,
            real_value TEXT UNIQUE,
            last_used_time INTEGER
        );
        """
    )
    conn.commit()
    return conn


def cleanup_db(conn: sqlite3.Connection):
    """Delete database entries that haven't been used in a long time."""
    try:
        cutoff = int(time.time()) - CLEANUP_INTERVAL
        cursor = conn.cursor()
        cursor.execute("DELETE FROM redaction_map WHERE last_used_time < ?", (cutoff,))
        deleted_count = cursor.rowcount
        conn.commit()
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} expired redaction mappings.")
    except Exception as e:
        logger.error(f"Error cleaning up redaction database: {e}")


def load_registry_vocabulary() -> Set[str]:
    """Dynamically load known cluster, namespace, and agent names from registry files."""
    vocab: Set[str] = set()

    # Files to scan for registrations
    registry_files = [
        "/opt/data/devteam_agents.jsonl",
        "/opt/data/operator_agents.jsonl",
    ]

    for path in registry_files:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            # Extract sensitive identifiers
                            for key in (
                                "name",
                                "cluster",
                                "region",
                                "namespace",
                                "endpoint",
                                "agent_id",
                                "cluster_name",
                                "location",
                            ):
                                val = data.get(key)
                                if isinstance(val, str) and len(val) >= 4:
                                    vocab.add(val)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning(f"Failed to load registry file '{path}': {e}")

    # Also capture common cluster names / context variables from the environment
    for env_var in ("KUBERNETES_SERVICE_HOST", "GCP_PROJECT", "GKE_CLUSTER"):
        val = os.environ.get(env_var)
        if val and len(val) >= 4:
            vocab.add(val)

    return vocab


def generate_placeholder(real_val: str, salt: str) -> str:
    """Generate a stable, deterministic 6-character hex placeholder for a given real value."""
    hasher = hashlib.sha256()
    hasher.update((HASH_SALT_PREFIX + salt + real_val).encode("utf-8"))
    hash_hex = hasher.hexdigest()[:6]
    return f"GC{hash_hex}"


def get_or_create_placeholder(
    conn: sqlite3.Connection, real_value: str, session_id: str
) -> str:
    """Get the placeholder for a value, creating a new one if it doesn't exist."""
    cursor = conn.cursor()
    # Check if this real value already has a mapping
    cursor.execute("SELECT placeholder FROM redaction_map WHERE real_value = ?", (real_value,))
    row = cursor.fetchone()

    current_time = int(time.time())

    if row:
        placeholder = row[0]
        # Update last used time
        cursor.execute(
            "UPDATE redaction_map SET last_used_time = ? WHERE placeholder = ?",
            (current_time, placeholder),
        )
        conn.commit()
        return placeholder

    # Generate a new placeholder candidate
    salt = session_id
    candidate = generate_placeholder(real_value, salt)
    collision_counter = 0

    # Handle potential hash collisions (extremely rare but possible)
    while True:
        cursor.execute("SELECT real_value FROM redaction_map WHERE placeholder = ?", (candidate,))
        collision_row = cursor.fetchone()
        if not collision_row:
            # Unique placeholder found!
            break
        if collision_row[0] == real_value:
            # We already have it mapped (should not happen due to top select, but for safety)
            break
        # Collision: modify salt to generate a different hash
        collision_counter += 1
        salt = f"{session_id}_col{collision_counter}"
        candidate = generate_placeholder(real_value, salt)

    # Insert new mapping
    try:
        cursor.execute(
            "INSERT INTO redaction_map (placeholder, real_value, last_used_time) VALUES (?, ?, ?)",
            (candidate, real_value, current_time),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Re-fetch in case of concurrent insert race condition
        cursor.execute("SELECT placeholder FROM redaction_map WHERE real_value = ?", (real_value,))
        row = cursor.fetchone()
        if row:
            candidate = row[0]

    return candidate


def redact_text(text: str, vocab: Set[str], session_id: str, conn: sqlite3.Connection) -> str:
    """Scan and redact IP addresses, hostnames, and registered vocab elements in the text."""
    if not text:
        return text

    orig_text = text
    matched_terms = []

    # Helper to redact a single matched term
    def redact_match(match: re.Match) -> str:
        matched_str = match.group(0)
        placeholder = get_or_create_placeholder(conn, matched_str, session_id)
        matched_terms.append(f"{matched_str} -> {placeholder}")
        return placeholder

    # 1. Redact IP addresses
    text = IP_PATTERN.sub(redact_match, text)

    # 2. Redact domain names/hostnames
    text = HOSTNAME_PATTERN.sub(redact_match, text)

    # 3. Redact vocabulary entries (clusters, namespaces, subagent names)
    if vocab:
        # Sort vocabulary by length descending to match longest matches first
        sorted_vocab = sorted(vocab, key=len, reverse=True)
        # Build regex matching any of the vocabulary terms as whole words using custom boundary lookarounds
        # to prevent standard \b (which treats - as non-word) from matching inside hyphenated strings
        vocab_regex = re.compile(
            r"(?<![a-zA-Z0-9_-])(" + "|".join(re.escape(word) for word in sorted_vocab) + r")(?![a-zA-Z0-9_-])"
        )
        text = vocab_regex.sub(redact_match, text)

    # 4. Redact non-dictionary tokens (generic spellcheck fallback)
    def redact_non_dict_match(match: re.Match) -> str:
        matched_str = match.group(0)
        # If the token contains any existing placeholder, skip redacting it further to avoid double-redaction
        if EMBEDDED_PLACEHOLDER_PATTERN.search(matched_str):
            return matched_str
        if is_dictionary_word(matched_str):
            return matched_str
        # Generate placeholder for non-dictionary word
        placeholder = get_or_create_placeholder(conn, matched_str, session_id)
        matched_terms.append(f"{matched_str} -> {placeholder}")
        return placeholder

    text = WORD_TOKEN_PATTERN.sub(redact_non_dict_match, text)

    if matched_terms:
        logger.info(f"redact_text matches found: {', '.join(matched_terms)}")
        logger.debug(f"Original text: {orig_text}")
        logger.debug(f"Redacted text: {text}")

    return text


def restore_text(text: str, session_id: str, conn: sqlite3.Connection) -> str:
    """Restore all placeholders in the text with their real values from the database."""
    if not text:
        return text

    # Fetch all mappings into a dictionary
    cursor = conn.cursor()
    cursor.execute("SELECT placeholder, real_value FROM redaction_map")
    mappings = {p: r for p, r in cursor.fetchall()}

    current_time = int(time.time())
    restored_terms = []
    orig_text = text

    # Recursively resolve placeholders to handle nested/composite mappings
    iterations = 0
    while iterations < 5:
        placeholders_found = EMBEDDED_PLACEHOLDER_PATTERN.findall(text)
        if not placeholders_found:
            break

        replaced_any = False
        for p in set(placeholders_found):
            if p in mappings:
                real_value = mappings[p]
                text = text.replace(p, real_value)
                restored_terms.append(f"{p} -> {real_value}")
                replaced_any = True
                # Update last used time to keep it from being pruned
                try:
                    cursor.execute(
                        "UPDATE redaction_map SET last_used_time = ? WHERE placeholder = ?",
                        (current_time, p),
                    )
                except Exception as e:
                    logger.warning(f"Failed to update last_used_time for {p}: {e}")

        if not replaced_any:
            break
        iterations += 1

    conn.commit()

    if restored_terms:
        logger.info(f"restore_text replacements: {', '.join(restored_terms)}")
        logger.debug(f"Original text: {orig_text}")
        logger.debug(f"Restored text: {text}")

    return text


def redact_object(
    obj: Any, vocab: Set[str], session_id: str, conn: sqlite3.Connection
) -> Any:
    """Recursively traverse and redact values in any python object/request structure, decoding JSON strings."""
    if isinstance(obj, str):
        try:
            data = json.loads(obj)
            if isinstance(data, (dict, list)):
                redacted_data = redact_object(data, vocab, session_id, conn)
                return json.dumps(redacted_data)
        except (json.JSONDecodeError, TypeError):
            pass
        return redact_text(obj, vocab, session_id, conn)
    elif isinstance(obj, dict):
        return {k: redact_object(v, vocab, session_id, conn) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [redact_object(v, vocab, session_id, conn) for v in obj]
    return obj


def unredact_object(obj: Any, session_id: str, conn: sqlite3.Connection) -> Any:
    """Recursively traverse and restore placeholders in any python object/response structure, decoding JSON strings."""
    if isinstance(obj, str):
        try:
            data = json.loads(obj)
            if isinstance(data, (dict, list)):
                unredacted_data = unredact_object(data, session_id, conn)
                return json.dumps(unredacted_data)
        except (json.JSONDecodeError, TypeError):
            pass
        return restore_text(obj, session_id, conn)
    elif isinstance(obj, dict):
        return {k: unredact_object(v, session_id, conn) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [unredact_object(v, session_id, conn) for v in obj]
    elif hasattr(obj, "__dict__"):
        for k, v in list(obj.__dict__.items()):
            # Avoid traversing internal system fields or lock descriptors
            if not k.startswith("__"):
                try:
                    setattr(obj, k, unredact_object(v, session_id, conn))
                except AttributeError:
                    # In case of read-only properties
                    continue
    return obj


# --- Plugin Registration Entry Point ---


def register(ctx):
    """Register client_redactor pre-LLM and post-LLM middleware wrappers."""
    # Ensure opt-in database structure is set up
    conn = init_db()
    cleanup_db(conn)
    conn.close()

    # Pre-LLM payload rewriting (llm_request middleware)
    def redact_llm_request(request: Dict[str, Any], session_id: str, **kwargs: Any) -> Dict[str, Any]:
        conn = None
        try:
            logger.info("Running client-side redaction sweep...")
            conn = init_db()
            vocab = load_registry_vocabulary()

            # Redact system prompt parameter if it exists
            if "system" in request and isinstance(request["system"], str):
                request["system"] = redact_object(request["system"], vocab, session_id, conn)

            # Redact all messages in the prompt history
            messages = request.get("messages", [])
            for msg in messages:
                content = msg.get("content")
                if content is not None:
                    msg["content"] = redact_object(content, vocab, session_id, conn)

            logger.info("Client-side redaction sweep complete.")
        except Exception as e:
            logger.error(f"Error in client_redactor request middleware: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()

        return {"request": request}

    # Post-LLM response restoration (llm_execution middleware)
    def unredact_llm_response(
        request: Dict[str, Any], next_call: Any, session_id: str, **kwargs: Any
    ) -> Any:
        # 1. Run downstream execution chain (the actual LLM API call)
        response = next_call(request)

        # 2. De-redact the response object inline
        conn = None
        try:
            logger.info("Running client-side un-redaction sweep...")
            conn = init_db()
            response = unredact_object(response, session_id, conn)
            logger.info("Client-side un-redaction sweep complete.")
        except Exception as e:
            logger.error(f"Error in client_redactor execution middleware: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()

        return response

    # Register both middleware handlers
    ctx.register_middleware("llm_request", redact_llm_request)
    ctx.register_middleware("llm_execution", unredact_llm_response)
    logger.info("Successfully registered client_redactor middleware handlers.")


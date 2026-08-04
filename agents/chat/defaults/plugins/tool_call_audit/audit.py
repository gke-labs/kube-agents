import fnmatch
import json
import logging
import os
import pathlib
import shlex
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None

logger = logging.getLogger("hermes.plugin.tool_call_audit")

_PAYLOAD_LOG_LIMIT = 2000

_BOUNDS_CACHE: Dict[str, Dict[str, Any]] = {}


def clear_bounds_cache() -> None:
    """Clear the module-level execution bounds cache (for unit testing)."""
    _BOUNDS_CACHE.clear()


def _load_execution_bounds(config_path: Optional[str] = None) -> Dict[str, Any]:
    cache_key = (
        str(config_path)
        if config_path
        else f"default:{os.environ.get('HERMES_HOME', '/opt/data')}:{os.environ.get('HERMES_PROFILE', '')}:{os.environ.get('HERMES_PROFILE_DIR', '')}:{os.getcwd()}"
    )
    if cache_key in _BOUNDS_CACHE:
        return _BOUNDS_CACHE[cache_key]

    paths_to_check = []
    if config_path:
        paths_to_check.append(pathlib.Path(config_path))

    # 1. Check environment overrides for active profile (Significant 2)
    if os.environ.get("HERMES_PROFILE_DIR"):
        p = pathlib.Path(os.environ["HERMES_PROFILE_DIR"]) / "config.yaml"
        if p not in paths_to_check:
            paths_to_check.append(p)

    hermes_home = pathlib.Path(os.environ.get("HERMES_HOME", "/opt/data"))
    if os.environ.get("HERMES_PROFILE"):
        p = hermes_home / "profiles" / os.environ["HERMES_PROFILE"] / "config.yaml"
        if p not in paths_to_check:
            paths_to_check.append(p)

    # 2. Check if current working directory is inside a profile directory under profiles/<name>
    try:
        cwd_path = pathlib.Path(os.getcwd()).resolve()
        for parent in [cwd_path] + list(cwd_path.parents):
            if parent.parent.name == "profiles" and (parent / "config.yaml").exists():
                if (parent / "config.yaml") not in paths_to_check:
                    paths_to_check.append(parent / "config.yaml")
                break
    except Exception:
        pass

    # 3. Fallback check for standard platform and cluster paths
    for candidate in (
        hermes_home / "profiles" / "platform" / "config.yaml",
        pathlib.Path("/opt/data/profiles/platform/config.yaml"),
        pathlib.Path("/opt/platform-template/config.yaml"),
        pathlib.Path("/opt/cluster-template/config.yaml"),
    ):
        if candidate not in paths_to_check:
            paths_to_check.append(candidate)

    # 4. Check relative profile config from __file__ parents
    try:
        curr = pathlib.Path(__file__).resolve()
        for p in curr.parents:
            candidate_profile = p / "config.yaml"
            if candidate_profile.exists() and candidate_profile not in paths_to_check:
                paths_to_check.append(candidate_profile)
    except Exception:
        pass

    try:
        curr = pathlib.Path(__file__).resolve()
        for p in curr.parents:
            candidate_repo = p / "agents" / "platform" / "config.yaml"
            if candidate_repo.exists() and candidate_repo not in paths_to_check:
                paths_to_check.append(candidate_repo)
    except Exception:
        pass

    if pathlib.Path("/opt/defaults/config.yaml") not in paths_to_check:
        paths_to_check.append(pathlib.Path("/opt/defaults/config.yaml"))

    for path in paths_to_check:
        if path.exists() and yaml is not None:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    bounds = data.get("execution_bounds", {}).get("hermes_cli", {})
                    if bounds:
                        _BOUNDS_CACHE[cache_key] = bounds
                        return bounds
            except Exception as exc:
                logger.warning("Failed to load execution bounds from %s: %s", path, exc)

    # Significant 1: Keep one source of truth and fail closed when the config cannot be read
    raise PermissionError(
        "Execution bounds policy could not be loaded from config.yaml (no valid configuration found or PyYAML is missing). Failing closed."
    )


def _is_shell_tool(tool_name: str, args: Optional[Dict[str, Any]] = None) -> bool:
    if not tool_name or not isinstance(tool_name, str):
        return False
    name_lower = tool_name.lower()
    shell_tools = {
        "hermes-cli",
        "hermes_cli",
        "shell",
        "bash",
        "run_command",
        "cli",
        "terminal",
        "execute_command",
        "exec_command",
        "command",
        "run_shell",
        "run_shell_command",
        "exec",
        "execute",
        "cmd",
        "terminal_command",
        "command_execution",
        "run",
        "mcp-hermes_run_command",
        "mcp_hermes_run_command",
    }
    if name_lower in shell_tools:
        return True
    shell_keywords = (
        "run_command",
        "execute_command",
        "shell_command",
        "terminal_command",
        "run_shell",
        "bash_command",
    )
    if any(kw in name_lower for kw in shell_keywords):
        return True

    # Significant 4: Default to deny for unknown tools that carry shell command payloads
    known_non_shell = {
        "mcp-agent_common",
        "mcp_agent_common",
        "mcp-gke",
        "mcp_gke",
        "mcp-developer_knowledge",
        "mcp_developer_knowledge",
        "read_file",
        "write_file",
        "list_directory",
        "search_files",
    }
    if (
        name_lower in known_non_shell
        or any(name_lower.startswith(k) for k in ("kanban_", "mcp-agent_common_", "mcp_agent_common_"))
    ) and not any(w in name_lower for w in ("run_command", "execute", "exec", "shell", "bash")):
        return False

    if args and isinstance(args, dict):
        if any(key in args for key in ("command", "cmd", "command_line", "CommandLine")):
            return True
    return False


def _split_subcommands(cmd: str) -> List[str]:
    """Split a shell command string into individual subcommands across pipelines,
    logical operators, unescaped newlines, semicolons, and command substitutions.
    """
    if not cmd:
        return []

    subcommands = []
    idx = 0
    while idx < len(cmd):
        if cmd[idx : idx + 2] == "$(":
            depth = 1
            start = idx + 2
            curr = start
            while curr < len(cmd) and depth > 0:
                if cmd[curr : curr + 2] == "$(":
                    depth += 1
                    curr += 2
                    continue
                elif cmd[curr] == ")":
                    depth -= 1
                    if depth == 0:
                        subcommands.extend(_split_subcommands(cmd[start:curr]))
                        break
                curr += 1
            idx = curr + 1
        elif cmd[idx] == "`":
            start = idx + 1
            curr = start
            while curr < len(cmd) and cmd[curr] != "`":
                curr += 1
            if curr < len(cmd):
                subcommands.extend(_split_subcommands(cmd[start:curr]))
            idx = curr + 1
        else:
            idx += 1

    normalized = cmd.replace("\\\n", " ").replace("\\\r\n", " ")

    current_token = []
    in_single_quote = False
    in_double_quote = False
    escape_next = False

    i = 0
    while i < len(normalized):
        ch = normalized[i]
        if escape_next:
            current_token.append(ch)
            escape_next = False
            i += 1
            continue
        if ch == "\\":
            escape_next = True
            current_token.append(ch)
            i += 1
            continue
        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current_token.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current_token.append(ch)
            i += 1
            continue

        if not in_single_quote and not in_double_quote:
            if normalized[i : i + 2] in ("&&", "||"):
                sub_str = "".join(current_token).strip()
                if sub_str:
                    subcommands.append(sub_str)
                current_token = []
                i += 2
                continue
            elif ch in (";", "|", "\n", "\r"):
                sub_str = "".join(current_token).strip()
                if sub_str:
                    subcommands.append(sub_str)
                current_token = []
                i += 1
                continue

        current_token.append(ch)
        i += 1

    sub_str = "".join(current_token).strip()
    if sub_str:
        subcommands.append(sub_str)

    return [s for s in subcommands if s]


def _validate_kubectl_exec(subcmd: str) -> None:
    """Enforce read-only non-interactive diagnostics in kubeagents-system for kubectl exec."""
    tokens = subcmd.split()
    forbidden_flags = {"-i", "-t", "-it", "-ti", "--stdin", "--tty"}
    if any(t in forbidden_flags for t in tokens):
        raise PermissionError(
            f"Command '{subcmd}' is blocked by execution bounds: interactive 'kubectl exec' flags (-i/-t) are forbidden."
        )

    has_namespace = False
    for idx, t in enumerate(tokens):
        if t in ("-n", "--namespace"):
            if idx + 1 < len(tokens) and tokens[idx + 1] == "kubeagents-system":
                has_namespace = True
        elif t.startswith("--namespace=") and t.split("=", 1)[1] == "kubeagents-system":
            has_namespace = True
    if not has_namespace:
        raise PermissionError(
            f"Command '{subcmd}' is blocked by execution bounds: 'kubectl exec' is restricted to namespace kubeagents-system."
        )

    if "--" not in tokens:
        raise PermissionError(
            f"Command '{subcmd}' is blocked by execution bounds: 'kubectl exec' must specify '--' before inner command."
        )

    dash_idx = tokens.index("--")
    inner_tokens = tokens[dash_idx + 1 :]
    if not inner_tokens:
        raise PermissionError(
            f"Command '{subcmd}' is blocked by execution bounds: missing command after '--' in 'kubectl exec'."
        )

    forbidden_exec_bins = {"sh", "bash", "/bin/sh", "/bin/bash", "python", "python3", "perl", "ruby", "eval"}
    if inner_tokens[0] in forbidden_exec_bins:
        raise PermissionError(
            f"Command '{subcmd}' is blocked by execution bounds: arbitrary shell execution via 'kubectl exec' is forbidden."
        )


def _validate_gh_api(subcmd: str) -> None:
    """Enforce read-only HTTP methods for gh api calls."""
    tokens = subcmd.split()
    mutating_methods = {"DELETE", "PUT", "POST", "PATCH"}
    for idx, t in enumerate(tokens):
        upper_t = t.upper()
        if upper_t in ("-XDELETE", "-XPUT", "-XPOST", "-XPATCH"):
            raise PermissionError(
                f"Command '{subcmd}' is blocked by execution bounds: 'gh api' with mutating HTTP method is forbidden without HITL."
            )
        if t in ("-X", "--method") and idx + 1 < len(tokens):
            if tokens[idx + 1].upper() in mutating_methods:
                raise PermissionError(
                    f"Command '{subcmd}' is blocked by execution bounds: 'gh api' with mutating HTTP method is forbidden without HITL."
                )
        if t.startswith("--method=") and t.split("=", 1)[1].upper() in mutating_methods:
            raise PermissionError(
                f"Command '{subcmd}' is blocked by execution bounds: 'gh api' with mutating HTTP method is forbidden without HITL."
            )


def verify_execution_bounds(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    config_path: Optional[str] = None,
) -> None:
    if not _is_shell_tool(tool_name, args):
        return
    if not args or not isinstance(args, dict):
        return

    cmd = args.get("command") or args.get("cmd") or args.get("command_line") or args.get("CommandLine") or ""
    if isinstance(args.get("args"), str) and not cmd:
        cmd = args.get("args")
    elif isinstance(args.get("args"), (list, tuple)) and not cmd:
        cmd = " ".join(str(x) for x in args.get("args"))
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(x) for x in cmd)

    if not cmd or not isinstance(cmd, str):
        return

    cmd_stripped = cmd.strip()
    bounds = _load_execution_bounds(config_path)

    # Significant 7: Enforce command_timeout_seconds limit
    timeout_limit = bounds.get("command_timeout_seconds", 60)
    if timeout_limit and isinstance(args, dict):
        current_timeout = args.get("timeout") or args.get("timeout_seconds") or args.get("command_timeout")
        if current_timeout and isinstance(current_timeout, (int, float)):
            if current_timeout > timeout_limit:
                raise PermissionError(
                    f"Command timeout {current_timeout}s exceeds execution bounds limit of {timeout_limit}s."
                )
        else:
            args["timeout_seconds"] = timeout_limit

    # 0. Reject shell metacharacters that allow backgrounding and process substitution
    always_blocked_metas = ["&", "`", "<(", ">("]
    for meta in always_blocked_metas:
        if meta in cmd_stripped:
            raise PermissionError(
                f"Command '{cmd_stripped}' is blocked by execution bounds: contains forbidden shell metacharacter '{meta}'."
            )

    # Parse command into individual subcommands across pipelines, logical operators, etc. (Blocking 4)
    subcommands = _split_subcommands(cmd_stripped)
    if not subcommands:
        subcommands = [cmd_stripped]

    blocked_patterns = bounds.get("blocked_command_patterns", [])
    read_only_paths = bounds.get("read_only_paths", [])
    writable_paths = bounds.get("writable_paths", [])
    mutating_tokens = {"rm", "mv", "cp", "touch", "chmod", "chown", "mkdir", "rmdir", "sed", "tee", "vi", "nano", ">", ">>"}

    ro_list = []
    for ro in read_only_paths:
        ro_list.append(ro.rstrip("/"))
        ro_list.append(os.path.abspath(os.path.expanduser(ro)).rstrip("/"))
        ro_list.append(os.path.realpath(os.path.expanduser(ro)).rstrip("/"))

    w_list = []
    for w in writable_paths:
        w_list.append(w.rstrip("/"))
        w_list.append(os.path.abspath(os.path.expanduser(w)).rstrip("/"))
        w_list.append(os.path.realpath(os.path.expanduser(w)).rstrip("/"))

    SAFE_PIPE_FILTERS = {
        "grep",
        "egrep",
        "fgrep",
        "tail",
        "head",
        "awk",
        "sed",
        "cat",
        "sort",
        "uniq",
        "wc",
        "jq",
        "tr",
        "cut",
        "column",
        "xargs",
        "date",
    }

    for subcmd in subcommands:
        # 1. Check blocked command patterns (Minor 3: anchored to command tokens)
        for pattern in blocked_patterns:
            if not pattern:
                continue
            if "*" in pattern:
                glob_pat = pattern if pattern.startswith("*") else f"*{pattern}"
                glob_pat = glob_pat if glob_pat.endswith("*") else f"{glob_pat}*"
                if fnmatch.fnmatch(subcmd, glob_pat) or fnmatch.fnmatch(cmd_stripped, glob_pat):
                    raise PermissionError(
                        f"Command '{cmd_stripped}' is blocked by execution bounds: matches blocked pattern '{pattern}'."
                    )
            else:
                pat_clean = pattern.strip()
                tokens_in_sub = subcmd.split()
                if tokens_in_sub and tokens_in_sub[0] == pat_clean:
                    raise PermissionError(
                        f"Command '{cmd_stripped}' is blocked by execution bounds: matches blocked command '{pat_clean}'."
                    )
                elif subcmd == pat_clean or subcmd.startswith(pat_clean + " "):
                    raise PermissionError(
                        f"Command '{cmd_stripped}' is blocked by execution bounds: matches blocked pattern '{pattern}'."
                    )

        # 2. Check filesystem write confinement and read-only paths
        tokens = subcmd.split()
        is_mutating = any(t in mutating_tokens for t in tokens)
        path_candidates = []
        for idx, token in enumerate(tokens):
            if token in mutating_tokens or token.startswith("-"):
                continue
            if (
                token.startswith("/")
                or token.startswith(".")
                or token.startswith("~")
                or "/" in token
                or (idx > 0 and tokens[idx - 1] in {">", ">>"})
                or (is_mutating and idx > 0 and tokens[0] in mutating_tokens)
            ):
                path_candidates.append(token)

        for token in path_candidates:
            norm_paths = {
                token,
                os.path.normpath(token),
                os.path.abspath(os.path.expanduser(token)),
                os.path.realpath(os.path.expanduser(token)),
            }
            if is_mutating and any("/profiles/" in p and "/skills" in p for p in norm_paths):
                raise PermissionError(
                    f"Command '{cmd_stripped}' is blocked by execution bounds: write access to runtime profile skills directory is forbidden."
                )
            for p in norm_paths:
                p_clean = p.rstrip("/")
                for ro_path in ro_list:
                    if p_clean == ro_path or p_clean.startswith(ro_path + "/"):
                        if is_mutating:
                            raise PermissionError(
                                f"Command '{cmd_stripped}' is blocked by execution bounds: write access to read-only path '{ro_path}' is forbidden."
                            )
                if is_mutating:
                    is_writable = any(
                        p_clean == w_path or p_clean.startswith(w_path + "/")
                        for w_path in w_list
                    )
                    if not is_writable:
                        raise PermissionError(
                            f"Command '{cmd_stripped}' is blocked by execution bounds: write access to path '{token}' outside writable paths is restricted."
                        )

        # 3. Check allowlist prefixes if sandbox mode is enforced
        if bounds.get("sandbox_mode", "").lower() == "enforced":
            allowed_prefixes = bounds.get("allowed_binary_prefixes", [])
            if allowed_prefixes:
                matched = False
                tokens_in_sub = subcmd.split()
                if tokens_in_sub and tokens_in_sub[0] in SAFE_PIPE_FILTERS and not is_mutating:
                    matched = True
                else:
                    for pref in allowed_prefixes:
                        if subcmd == pref:
                            matched = True
                            break
                        if pref.endswith("/") and subcmd.startswith(pref):
                            matched = True
                            break
                        if subcmd.startswith(pref + " "):
                            matched = True
                            break
                if not matched:
                    raise PermissionError(
                        f"Command '{subcmd}' is blocked by execution bounds: command does not match any allowed binary prefix."
                    )

                if subcmd.startswith("kubectl exec"):
                    _validate_kubectl_exec(subcmd)
                if subcmd.startswith("gh api"):
                    _validate_gh_api(subcmd)


def _serialize(value: Any) -> str:
    if isinstance(value, str):
        if len(value) > _PAYLOAD_LOG_LIMIT:
            return value[:_PAYLOAD_LOG_LIMIT] + "...(truncated)"
        return value
    try:
        serialized = json.dumps(value, default=str, sort_keys=True)
    except Exception:
        serialized = str(value)
    if len(serialized) > _PAYLOAD_LOG_LIMIT:
        return serialized[:_PAYLOAD_LOG_LIMIT] + "...(truncated)"
    return serialized


def _emit(event: str, fields: Dict[str, Any]) -> None:
    record = {"audit_event": event, **fields}
    logger.info(json.dumps(record, default=str, sort_keys=True))


def log_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    **kwargs: Any,
) -> None:
    try:
        verify_execution_bounds(tool_name, args)
    except PermissionError as exc:
        _emit(
            "tool_call_denied",
            {
                "tool_name": tool_name,
                "task_id": task_id,
                "args": _serialize(args or {}),
                "reason": str(exc),
            },
        )
        raise
    try:
        _emit(
            "tool_call_start",
            {"tool_name": tool_name, "task_id": task_id, "args": _serialize(args or {})},
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_tool_call hook: %s", exc, exc_info=True)



def log_post_tool_call(
    tool_name: str = "",
    result: Any = None,
    duration_ms: Optional[float] = None,
    task_id: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "tool_call_end",
            {
                "tool_name": tool_name,
                "task_id": task_id,
                "duration_ms": duration_ms,
                "result": _serialize(result),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit post_tool_call hook: %s", exc, exc_info=True)


def log_pre_approval_request(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    surface: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "approval_request",
            {
                "surface": surface,
                "pattern_key": pattern_key,
                "description": description,
                "command": _serialize(command),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_approval_request hook: %s", exc, exc_info=True)


def log_post_approval_response(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    surface: str = "",
    choice: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "approval_response",
            {
                "surface": surface,
                "pattern_key": pattern_key,
                "choice": choice,
                "description": description,
                "command": _serialize(command),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit post_approval_response hook: %s", exc, exc_info=True)


def log_pre_gateway_dispatch(
    event: Any,
    gateway: Any = None,
    session_store: Any = None,
    **kwargs: Any,
) -> None:
    try:
        source = getattr(event, "source", None)
        session_id = ""
        if source is not None and session_store is not None:
            try:
                session_entry = session_store.get_or_create_session(source)
                session_id = getattr(session_entry, "session_id", "") or ""
            except Exception:
                pass

        text = getattr(event, "text", "") or ""
        platform = ""
        user_id = ""
        if source is not None:
            platform_obj = getattr(source, "platform", "") or ""
            platform = getattr(platform_obj, "value", None) or str(platform_obj)
            user_id = getattr(source, "user_id", "") or ""

        _emit(
            "gateway_dispatch",
            {
                "session_id": session_id,
                "platform": platform,
                "user_id": user_id,
                "text": _serialize(text),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_gateway_dispatch hook: %s", exc, exc_info=True)


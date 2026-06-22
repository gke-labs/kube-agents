import logging
from typing import Any, Dict, List, Optional

from tools.terminal_tool import terminal_tool, check_terminal_requirements
from tools.cronjob_tools import cronjob, check_cronjob_requirements
from tools.delegate_tool import delegate_task, check_delegate_requirements
from tools.session_search_tool import session_search, check_session_search_requirements
from tools.skill_manager_tool import skill_manage

logger = logging.getLogger(__name__)

# ─── 1. Terminal Override ───────────────────────────────────────────────────
def terminal_handler(args: Dict[str, Any], **kwargs) -> str:
    return terminal_tool(
        command=args.get("command"),
        background=args.get("background", False),
        timeout=args.get("timeout"),
        task_id=kwargs.get("task_id"),
        workdir=args.get("workdir"),
        pty=args.get("pty", False),
        notify_on_complete=args.get("notify_on_complete", False),
        watch_patterns=args.get("watch_patterns"),
    )

TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": "Execute a shell command in the container terminal.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute."
            },
            "background": {
                "type": "boolean",
                "description": "Set true to run command in the background (recommended for builds/tests)."
            },
            "workdir": {
                "type": "string",
                "description": "Optional absolute path to the working directory."
            },
            "notify_on_complete": {
                "type": "boolean",
                "description": "Set true to get a notification when the background command finishes."
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait before timing out (default: 180)."
            }
        },
        "required": ["command"]
    }
}

# ─── 2. Cronjob Override ────────────────────────────────────────────────────
def cronjob_handler(args: Dict[str, Any], **kwargs) -> str:
    model_obj = args.get("model")
    model_name = None
    provider_name = None
    if isinstance(model_obj, dict):
        model_name = model_obj.get("model")
        provider_name = model_obj.get("provider")

    return cronjob(
        action=args.get("action", ""),
        job_id=args.get("job_id"),
        prompt=args.get("prompt"),
        schedule=args.get("schedule"),
        name=args.get("name"),
        repeat=args.get("repeat"),
        deliver=args.get("deliver"),
        include_disabled=args.get("include_disabled", True),
        skill=args.get("skill"),
        skills=args.get("skills"),
        model=model_name,
        provider=provider_name or args.get("provider"),
        base_url=args.get("base_url"),
        reason=args.get("reason"),
        script=args.get("script"),
        context_from=args.get("context_from"),
        enabled_toolsets=args.get("enabled_toolsets"),
        workdir=args.get("workdir"),
        no_agent=args.get("no_agent"),
        task_id=kwargs.get("task_id"),
    )

CRONJOB_SCHEMA = {
    "name": "cronjob",
    "description": "Manage scheduled background jobs (create, list, pause, delete, trigger).",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "update", "pause", "resume", "delete", "trigger"],
                "description": "The action to perform."
            },
            "job_id": {
                "type": "string",
                "description": "Target job ID (required for update, pause, resume, delete, trigger)."
            },
            "prompt": {
                "type": "string",
                "description": "The instructions or prompt the agent runs at each scheduled interval."
            },
            "schedule": {
                "type": "string",
                "description": "Standard cron expression (e.g. '*/15 * * * *' for every 15 mins)."
            },
            "name": {
                "type": "string",
                "description": "A descriptive label for the job."
            },
            "script": {
                "type": "string",
                "description": "Optional script path to run. If no_agent=true, this runs directly without LLM."
            },
            "no_agent": {
                "type": "boolean",
                "description": "Set true to execute the script directly and deliver stdout without LLM reasoning."
            },
            "enabled_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of toolsets (e.g. ['web', 'terminal']) to limit the cron agent's capabilities."
            },
            "workdir": {
                "type": "string",
                "description": "Optional absolute path to run the job from (injects local workspace contexts)."
            }
        },
        "required": ["action"]
    }
}

# ─── 3. Delegate Task Override ──────────────────────────────────────────────
def delegate_handler(args: Dict[str, Any], **kwargs) -> str:
    return delegate_task(
        tasks=args.get("tasks"),
        role=args.get("role"),
        toolsets=args.get("toolsets"),
        background=args.get("background", False),
        task_id=kwargs.get("task_id"),
    )

DELEGATE_SCHEMA = {
    "name": "delegate_task",
    "description": "Spawn subagents with isolated context to solve subtasks concurrently or in background.",
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "The prompt/task instructions for the subagent."}
                    },
                    "required": ["goal"]
                },
                "description": "Array of goals/tasks to run in parallel (up to 3)."
            },
            "role": {
                "type": "string",
                "description": "Short role description for the subagent (e.g. 'Code Researcher')."
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Toolsets to enable (default: inherits parent agent's toolsets)."
            },
            "background": {
                "type": "boolean",
                "description": "Set true to run asynchronously in background. Retrieve results later."
            }
        },
        "required": ["tasks"]
    }
}

# ─── 4. Session Search Override ─────────────────────────────────────────────
def session_search_handler(args: Dict[str, Any], **kwargs) -> str:
    return session_search(
        query=args.get("query"),
        limit=args.get("limit", 5),
        sort=args.get("sort", "rank"),
        role_filter=args.get("role_filter"),
        session_id=args.get("session_id"),
        profile=args.get("profile"),
        task_id=kwargs.get("task_id"),
    )

SESSION_SEARCH_SCHEMA = {
    "name": "session_search",
    "description": "Search past conversation sessions and history.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "FTS5 query search keywords or phrases."
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default: 5)."
            },
            "role_filter": {
                "type": "string",
                "description": "Optional comma-separated list of roles to filter (e.g. 'user,assistant')."
            }
        },
        "required": ["query"]
    }
}

# ─── 5. Skill Manage Override ───────────────────────────────────────────────
def skill_manage_handler(args: Dict[str, Any], **kwargs) -> str:
    return skill_manage(
        action=args.get("action", ""),
        name=args.get("name", ""),
        content=args.get("content"),
        absorbed_into=args.get("absorbed_into"),
        file_path=args.get("file_path"),
        file_content=args.get("file_content"),
        task_id=kwargs.get("task_id"),
    )

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": "Create, edit, delete, or view skill documentation.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "edit", "patch", "delete", "read", "write_file", "remove_file"],
                "description": "The action to perform."
            },
            "name": {
                "type": "string",
                "description": "The name of the skill (e.g. 'gke-troubleshooting')."
            },
            "content": {
                "type": "string",
                "description": "The markdown content of the SKILL.md document (for create/edit)."
            },
            "file_path": {
                "type": "string",
                "description": "Optional path for helper files inside the skill directory."
            },
            "file_content": {
                "type": "string",
                "description": "Optional content of helper files."
            },
            "absorbed_into": {
                "type": "string",
                "description": "For delete action: name of the skill that absorbs this one."
            }
        },
        "required": ["action", "name"]
    }
}

# ─── 6. Plugin Registration ─────────────────────────────────────────────────
def register(ctx: Any) -> None:
    logger.info("Registering custom token-optimized Hermes Core Tool Overrides...")

    # Override terminal
    ctx.register_tool(
        name="terminal",
        toolset="terminal",
        schema=TERMINAL_SCHEMA,
        handler=terminal_handler,
        check_fn=check_terminal_requirements,
        emoji="💻",
        override=True
    )

    # Override cronjob
    ctx.register_tool(
        name="cronjob",
        toolset="cronjob",
        schema=CRONJOB_SCHEMA,
        handler=cronjob_handler,
        check_fn=check_cronjob_requirements,
        emoji="⏰",
        override=True
    )

    # Override delegate_task
    ctx.register_tool(
        name="delegate_task",
        toolset="delegation",
        schema=DELEGATE_SCHEMA,
        handler=delegate_handler,
        check_fn=check_delegate_requirements,
        emoji="🤝",
        override=True
    )

    # Override session_search
    ctx.register_tool(
        name="session_search",
        toolset="session_search",
        schema=SESSION_SEARCH_SCHEMA,
        handler=session_search_handler,
        check_fn=check_session_search_requirements,
        emoji="🔍",
        override=True
    )

    # Override skill_manage
    ctx.register_tool(
        name="skill_manage",
        toolset="skills",
        schema=SKILL_MANAGE_SCHEMA,
        handler=skill_manage_handler,
        emoji="💡",
        override=True
    )

    logger.info("Hermes Core Tool Overrides registered successfully!")

import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The roster logic is shared with the `list_agents` MCP tool
# (agents/chat/scripts/router_server.py) so the injected block and the refresh
# tool can never describe the fleet differently. It is a loose script rather
# than an importable package: the entrypoint copies /opt/defaults/scripts into
# $HERMES_HOME/scripts, and the MCP server is launched by absolute path. Load
# it by path for the same reason.
_MODULE_NAME = "_kube_agents_agent_roster"
_SCRIPT_NAME = "agent_roster.py"
_FALLBACK_SCRIPTS_DIR = Path("/opt/defaults/scripts")

_HEADER = "[SPECIALIST AGENTS AVAILABLE NOW]"
_FOOTER = (
    "Use one of the names above verbatim as the `assignee` of `kanban_create`. "
    "This list is current as of this turn; call `list_agents` only if an agent "
    "you expect is missing."
)

# Cached across turns in one process. Not a TTL cache: the module is a few
# hundred lines of stdlib and re-executing it per turn buys nothing, whereas
# the roster it produces IS re-read every turn — a cluster agent created a
# moment ago has to show up on the next message, which is the whole reason the
# front door was calling the tool in the first place.
_roster_module: Optional[ModuleType] = None


def _scripts_dirs() -> list[Path]:
    data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    return [data_dir / "scripts", _FALLBACK_SCRIPTS_DIR]


def _load_roster_module() -> Optional[ModuleType]:
    global _roster_module
    if _roster_module is not None:
        return _roster_module
    for base in _scripts_dirs():
        path = base / _SCRIPT_NAME
        try:
            # is_file() is inside the try on purpose: the scripts directory is on
            # the shared PVC, and pathlib only swallows ENOENT/ENOTDIR/EBADF/ELOOP
            # — a stat() that fails with EACCES or EIO raises for real, and this
            # runs ahead of every turn on the front door.
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            logger.warning("Could not load the agent roster from %s: %s", path, e)
            continue
        _roster_module = module
        return module
    logger.warning("No %s found under %s; roster injection disabled.",
                   _SCRIPT_NAME, [str(b) for b in _scripts_dirs()])
    return None


def handle_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Put the current specialist roster in front of the model on every turn.

    The Chat Agent must name an `assignee` to delegate anything, and the set of
    specialists is dynamic — per-cluster agents appear and disappear as the
    fleet changes — so it was told to call `list_agents` before every routing
    decision. The tool itself takes about 0.1s, but invoking it costs a whole
    LLM roundtrip: measured on the live deployment, roughly 6s of a ~17s
    acknowledgement went to fetching what is, underneath, a directory listing.
    Injecting it here makes that roundtrip unnecessary in the common case; the
    tool remains as the refresh path.

    Fails soft in every direction. This hook runs ahead of every single user
    turn on the front door, so a raise here is a chat agent that cannot answer
    at all — strictly worse than one that has to look the roster up.
    """
    module = _load_roster_module()
    if module is None:
        return None
    try:
        data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))
        roster = module.render(data_dir / "profiles")
    except Exception as e:
        logger.warning("Could not render the agent roster: %s", e)
        return None
    # render() returns None when discovery itself failed. Inject nothing: the
    # model then falls back to `list_agents`, which is the pre-injection
    # behaviour. Injecting "no specialist agents are available" instead would
    # state a fault as a fact and stop the front door routing at all.
    if not roster:
        return None
    # An empty fleet is a fact worth stating, but the footer must not ride
    # along with it: "use one of the names above" against a list with no names
    # in it, on a persona forbidden from doing the work itself, invites an
    # invented assignee. getattr rather than a direct read, because every other
    # module access in this hook is already defensive.
    if roster == getattr(module, "EMPTY_ROSTER", None):
        return {"context": f"\n\n{_HEADER}\n{roster}\n"}
    return {"context": f"\n\n{_HEADER}\n{roster}\n\n{_FOOTER}\n"}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", handle_pre_llm_call)

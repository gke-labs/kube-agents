"""Hermes runtime stubs for testing memory package in isolated unit test environments."""

import inspect
import json
import sys
import types

HAS_REAL_HERMES = False


def ensure_hermes_stubs():
    global HAS_REAL_HERMES
    real_agent = False
    real_plugins = False

    # 1. tools.registry
    try:
        import tools.registry
        if not hasattr(tools.registry, "tool_error"):
            def tool_error(message: str, **kwargs) -> str:
                data = {"error": message}
                data.update(kwargs)
                return json.dumps(data)
            tools.registry.tool_error = tool_error
    except ImportError:
        if "tools" not in sys.modules:
            tools = types.ModuleType("tools")
            sys.modules["tools"] = tools
        else:
            tools = sys.modules["tools"]

        if "tools.registry" not in sys.modules:
            tools_registry = types.ModuleType("tools.registry")
            sys.modules["tools.registry"] = tools_registry
            tools.registry = tools_registry
        else:
            tools_registry = sys.modules["tools.registry"]

        if not hasattr(tools_registry, "tool_error"):
            def tool_error(message: str, **kwargs) -> str:
                data = {"error": message}
                data.update(kwargs)
                return json.dumps(data)
            tools_registry.tool_error = tool_error

    # 2. agent, agent.memory_provider, agent.memory_manager
    try:
        import agent.memory_provider
        import agent.memory_manager
        real_agent = True
    except ImportError:
        if "agent" not in sys.modules:
            agent = types.ModuleType("agent")
            sys.modules["agent"] = agent
        else:
            agent = sys.modules["agent"]

        if "agent.memory_provider" not in sys.modules:
            agent_mem_prov = types.ModuleType("agent.memory_provider")
            sys.modules["agent.memory_provider"] = agent_mem_prov
            agent.memory_provider = agent_mem_prov
        else:
            agent_mem_prov = sys.modules["agent.memory_provider"]

        if not hasattr(agent_mem_prov, "MemoryProvider"):
            class MemoryProvider:
                def shutdown(self): pass
                def initialize(self, session_id="", **kwargs): pass
                def is_available(self): return True
                def queue_prefetch(self, query: str, *, session_id: str = ""): pass
                def prefetch(self, query: str, *, session_id: str = ""): pass
                def on_turn_start(self, turn_number: int, user_message: str, *, session_id: str = "", messages: list = None): pass
                def on_session_switch(self, session_id: str, *, user_id: str = ""): pass
                def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = ""): pass
                def on_session_end(self, messages: list = None): pass
            agent_mem_prov.MemoryProvider = MemoryProvider

        if "agent.memory_manager" not in sys.modules:
            agent_mem_mgr = types.ModuleType("agent.memory_manager")
            sys.modules["agent.memory_manager"] = agent_mem_mgr
            agent.memory_manager = agent_mem_mgr
        else:
            agent_mem_mgr = sys.modules["agent.memory_manager"]

        if not hasattr(agent_mem_mgr, "MemoryManager"):
            class MemoryManager:
                def __init__(self, *args, **kwargs):
                    self.providers = []

                @staticmethod
                def _provider_sync_accepts_messages(provider):
                    try:
                        sig = inspect.signature(provider.sync_turn)
                        for p in sig.parameters.values():
                            if p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "messages":
                                return True
                        return False
                    except Exception:
                        return False

                def add_provider(self, p):
                    self.providers.append(p)

                def _submit_background(self, fn, **kwargs):
                    fn()

                def sync_all(self, user_content, assistant_content, session_id="", messages=None):
                    for p in self.providers:
                        kw = {"session_id": session_id}
                        if self._provider_sync_accepts_messages(p):
                            kw["messages"] = messages
                        self._submit_background(lambda p=p, kw=kw: p.sync_turn(user_content, assistant_content, **kw))
            agent_mem_mgr.MemoryManager = MemoryManager

    # 3. plugins, plugins.memory, plugins.memory.hindsight
    try:
        import plugins.memory.hindsight
        if not hasattr(sys.modules.get("plugins.memory", object()), "load_memory_provider"):
            def load_memory_provider(name, config=None):
                if name == "hindsight":
                    from plugins.memory.hindsight import HindsightMemoryProvider
                    return HindsightMemoryProvider()
                return None
            sys.modules["plugins.memory"].load_memory_provider = load_memory_provider
        real_plugins = True
    except ImportError:
        if "plugins" not in sys.modules:
            plugins = types.ModuleType("plugins")
            sys.modules["plugins"] = plugins
        else:
            plugins = sys.modules["plugins"]

        if "plugins.memory" not in sys.modules:
            plugins_mem = types.ModuleType("plugins.memory")
            sys.modules["plugins.memory"] = plugins_mem
            plugins.memory = plugins_mem
        else:
            plugins_mem = sys.modules["plugins.memory"]

        if not hasattr(plugins_mem, "load_memory_provider"):
            def load_memory_provider(name, config=None):
                if name == "hindsight":
                    from plugins.memory.hindsight import HindsightMemoryProvider
                    return HindsightMemoryProvider()
                return None
            plugins_mem.load_memory_provider = load_memory_provider

        if "plugins.memory.hindsight" not in sys.modules:
            plugins_hindsight = types.ModuleType("plugins.memory.hindsight")
            sys.modules["plugins.memory.hindsight"] = plugins_hindsight
            plugins_mem.hindsight = plugins_hindsight
        else:
            plugins_hindsight = sys.modules["plugins.memory.hindsight"]

        if not hasattr(plugins_hindsight, "HindsightMemoryProvider"):
            base = sys.modules.get("agent.memory_provider", types.SimpleNamespace(MemoryProvider=object)).MemoryProvider
            class HindsightMemoryProvider(base):
                def shutdown(self): pass
                def initialize(self, session_id="", **kwargs): pass
                def is_available(self): return True
                def _get_client(self): return None
                def queue_prefetch(self, query: str, *, session_id: str = ""): pass
                def prefetch(self, query: str, *, session_id: str = ""): pass
                def on_turn_start(self, turn_number: int, user_message: str, *, session_id: str = "", messages: list = None): pass
                def on_session_switch(self, session_id: str, *, user_id: str = ""): pass
                def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = ""): pass
                def on_session_end(self, messages: list = None): pass
            plugins_hindsight.HindsightMemoryProvider = HindsightMemoryProvider

    # 4. hermes_cli, hermes_cli.config
    try:
        import hermes_cli.config
    except ImportError:
        if "hermes_cli" not in sys.modules:
            hermes_cli = types.ModuleType("hermes_cli")
            sys.modules["hermes_cli"] = hermes_cli
        else:
            hermes_cli = sys.modules["hermes_cli"]

        if "hermes_cli.config" not in sys.modules:
            hermes_cli_config = types.ModuleType("hermes_cli.config")
            sys.modules["hermes_cli.config"] = hermes_cli_config
            hermes_cli.config = hermes_cli_config
        else:
            hermes_cli_config = sys.modules["hermes_cli.config"]

        if not hasattr(hermes_cli_config, "load_config"):
            def load_config():
                return {}
            hermes_cli_config.load_config = load_config

    HAS_REAL_HERMES = real_agent and real_plugins


ensure_hermes_stubs()

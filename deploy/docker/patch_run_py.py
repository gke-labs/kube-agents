import sys

path = "/opt/hermes/gateway/run.py"
content = open(path).read()

target = "    async def _handle_active_session_busy_message(self, event: MessageEvent, session_key: str) -> bool:"
patch = """
        # Fire pre_gateway_dispatch hook for busy messages to allow interception (e.g. tool approvals)
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _hook_results = _invoke_hook(
                "pre_gateway_dispatch",
                event=event,
                gateway=self,
                session_store=self.session_store,
            )
            for _result in _hook_results:
                if isinstance(_result, dict) and _result.get("action") == "skip":
                    import logging
                    logging.getLogger("gateway.run").info("Busy message skipped by pre_gateway_dispatch hook: %s", _result.get("reason"))
                    return True
        except Exception as _hook_exc:
            import logging
            logging.getLogger("gateway.run").warning("Failed to invoke pre_gateway_dispatch hook for busy message: %s", _hook_exc)
"""

if target in content:
    new_content = content.replace(target, target + patch, 1)
    open(path, "w").write(new_content)
    print("Successfully patched run.py")
else:
    print("Error: target signature not found in run.py!")
    sys.exit(1)

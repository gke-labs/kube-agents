"""LiteLLM pre-call hook that redacts every outbound request body at the gateway.

The chart mounts this file, `redactor.py` beside it and the rendered
`redaction.yaml` into the stock LiteLLM image next to `/app/config.yaml`, and
names `litellm_redaction_callback.proxy_handler_instance` in
`litellm_settings.callbacks`. LiteLLM resolves that string to a file in the
config's directory, so the three files have to share one; `proxy_handler_instance`
is the attribute it reads.

What runs: `AuditRedactor`'s credential patterns, then the operator's rules
from the file `KUBE_AGENTS_REDACTION_CONFIG` names, over every string the
request carries to the provider -- `messages[].content` as a string or as
`text` content parts, the `input` of an embeddings request and the `prompt` of
a text completion. Responses are not touched; the limitation is documented on
the site's inference-gateway page.

Two failure modes are deliberate. A rule that does not load raises at import,
which stops the gateway pod at startup rather than letting it forward
unredacted; and an exception inside the hook propagates, which fails the one
request rather than forwarding it unredacted. That is the opposite of the
audit-hook copy's fail-open stance, because here the alternative to an error
is the payload leaving the estate.

The log line per request carries the count of substitutions by rule name and
never the payload.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from litellm.integrations.custom_logger import CustomLogger

# `redactor.py` is mounted beside this file, and LiteLLM loads this module by
# path rather than as a package, so its directory is not on sys.path unless
# this puts it there.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from redactor import AuditRedactor, RedactionRule  # noqa: E402

logger = logging.getLogger("kube_agents.litellm_redaction")

# The rendered redaction.yaml; the chart sets this on the gateway container.
CONFIG_PATH_ENV_VAR = "KUBE_AGENTS_REDACTION_CONFIG"
# Request fields the redactor walks. `messages` is the chat shape the agents
# send; `input` is embeddings; `prompt` is the legacy text-completion shape.
MESSAGES_KEY = "messages"
CONTENT_KEY = "content"
TEXT_PART_TYPE = "text"
TEXT_KEY = "text"
PART_TYPE_KEY = "type"
SCALAR_INPUT_KEYS = ("input", "prompt")


def load_rules(config_path: str) -> List[RedactionRule]:
    """Read the rendered config and build the rule list; raise on any defect."""
    with open(config_path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise ValueError(f"{config_path} must hold a mapping, not {type(config).__name__}")
    return AuditRedactor.rules_from_config(config)


def _config_path_from_env() -> str:
    path = os.environ.get(CONFIG_PATH_ENV_VAR, "").strip()
    if not path:
        raise RuntimeError(
            f"{CONFIG_PATH_ENV_VAR} is not set; the gateway redaction callback "
            f"refuses to start without its rule file"
        )
    return path


class KubeAgentsRedactionCallback(CustomLogger):
    """Redact request bodies before LiteLLM forwards them to the provider."""

    def __init__(self, rules: List[RedactionRule]) -> None:
        super().__init__()
        self._rules = list(rules)

    @property
    def rules(self) -> List[RedactionRule]:
        return list(self._rules)

    def _redact_text(self, text: str, counts: Dict[str, int]) -> str:
        redacted, made = AuditRedactor.redact_text_counted(text, self._rules)
        for name, count in made.items():
            counts[name] = counts.get(name, 0) + count
        return redacted

    def _redact_value(self, value: Any, counts: Dict[str, int]) -> Any:
        """Strings in place; lists element-wise; `text` content parts by their text."""
        if isinstance(value, str):
            return self._redact_text(value, counts)
        if isinstance(value, list):
            return [self._redact_value(item, counts) for item in value]
        if isinstance(value, dict):
            if value.get(PART_TYPE_KEY) == TEXT_PART_TYPE and isinstance(value.get(TEXT_KEY), str):
                value[TEXT_KEY] = self._redact_text(value[TEXT_KEY], counts)
            return value
        return value

    def redact_request(self, data: Dict[str, Any]) -> Dict[str, int]:
        """Redact `data` in place and return the substitution counts by rule name."""
        counts: Dict[str, int] = {}
        messages = data.get(MESSAGES_KEY)
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and CONTENT_KEY in message:
                    message[CONTENT_KEY] = self._redact_value(message[CONTENT_KEY], counts)
        for key in SCALAR_INPUT_KEYS:
            if key in data:
                data[key] = self._redact_value(data[key], counts)
        return counts

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        counts = self.redact_request(data)
        if counts:
            logger.info(
                "redacted %s request: %s",
                call_type,
                ", ".join(f"{name}={count}" for name, count in sorted(counts.items())),
            )
        return data


# Built at import so a bad rule file stops the pod at startup. The name is the
# one the chart's callback string ends in.
proxy_handler_instance = KubeAgentsRedactionCallback(load_rules(_config_path_from_env()))

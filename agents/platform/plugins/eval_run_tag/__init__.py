"""Tag every model request with the eval run, case and repetition it serves.

An ``llm_request`` middleware, registered only while the install has
``KUBEAGENTS_EVAL_RUN_TAGS`` on (the chart's
``platformAgent.deployment.evalRunTags``, which only the eval deploy sets).
Off, ``register`` adds nothing, so Hermes' turn loop takes the path it takes
with no middleware at all.

On, each request Hermes' turn loop sends gets two HTTP headers through
``extra_headers``: ``X-Kube-Agents-Eval-Run`` with the tag of the card this
worker runs or, outside a worker, the tag recorded for the turn's session; and
``X-Kube-Agents-Eval-Agent`` naming the profile and where the tag came from.
The replay proxy in front of LiteLLM logs both and forwards neither. The body,
which the model reads and the proxy keys on, is not touched. Everything lives
in ``hermes_cli/eval_run_tag.py`` (``deploy/docker/patches/eval_run_tag.py``);
a failure there leaves the request as it was.

Enabled in the default, platform, cluster and tse profiles' ``plugins.enabled``.
Auxiliary calls (titles, compression summaries) do not pass through the turn
loop's middleware and stay untagged.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Where the image installs the module.
TAG_MODULES = ("hermes_cli.eval_run_tag",)
LLM_REQUEST = "llm_request"


def _tag_module():
    import importlib

    for name in TAG_MODULES:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def register(ctx):
    module = _tag_module()
    if module is None:
        logger.warning("eval_run_tag: hermes_cli.eval_run_tag is not installed; nothing is tagged")
        return
    if not module.enabled():
        return
    ctx.register_middleware(LLM_REQUEST, module.on_llm_request)

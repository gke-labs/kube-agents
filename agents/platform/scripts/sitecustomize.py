"""Runtime patches for the platform agent image.

Every patch here exists to reach inside the running gateway -- the two relay
patches monkeypatch ``gateway.platform_registry``, ``sandbox_artifact_patch``
wraps ``gateway.kanban_watchers`` -- so all of them are dead weight in any
process that never runs a gateway. That used to be a purely theoretical
distinction. The operator sets ``PYTHONPATH=/opt/defaults/scripts``
on the agent container (``platformagent_manifests.go``), which means this module
runs in *every* Python the pod starts, and calling ``install()`` eagerly pulled
in ``gateway`` -> ``slack_bolt`` -> ``aiohttp``/``requests`` before the process
reached its own work: 747 modules, 2.2-6.1s under gVisor, to set up a hook only
the long-lived gateway can ever fire. Kanban spawns a fresh subprocess per card
and Hermes' environment probe spawns four more interpreters per session, so that
import was paid several times over on every card. Deferring it took the probe
from 9.6-10.3s to 0.39-0.44s.

Deferring, rather than gating on an "am I the gateway?" environment variable, is
deliberate. The credential proxy holds the only real bot token, and a gateway
that skipped the patch would connect Slack with a placeholder and be dropped
from Hermes' reconnect queue for the life of the pod (see the note on
``DEFAULT_RELAY_READY_TIMEOUT`` in ``slack_relay_patch``). Keying on the import
itself means the patch lands exactly when the thing it patches appears,
regardless of which process imports it or what it is called.
"""

import os
import sys

# The import every patch here waits on. The two relay patches replace something
# in it; ``sandbox_artifact_patch`` does not, and uses it only as the signal that
# this process is a gateway rather than one of the pod's short-lived
# interpreters. Nothing else the gateway imports is as reliable a marker, and
# waiting on ``gateway.kanban_watchers`` instead would fire inside its own import.
TRIGGER_MODULE = "gateway.platform_registry"

# (environment variable that enables the relay, module providing ``install()``)
RELAY_PATCHES = (
    ("GOOGLE_CHAT_RELAY_URL", "google_chat_relay_patch"),
    ("SLACK_RELAY_URL", "slack_relay_patch"),
)

# Patches deferred the same way but applied whatever the chat wiring is, because
# what they fix is not a relay's doing. ``sandbox_artifact_patch`` restores
# kanban artifact delivery once the agent's files live in the shell sandbox; the
# gateway drops them for every adapter, relayed or not, so gating this on a
# relay URL would leave a direct-token install with the same silent loss. It
# decides for itself whether the sandbox is on, on the first card it delivers
# rather than here -- reading the terminal config in every interpreter the pod
# starts is the cost this whole module exists to avoid, and the answer is
# memoised from then on.
UNCONDITIONAL_PATCHES = ("sandbox_artifact_patch",)


def _install(module_name):
    """Import ``module_name`` and run its ``install()``."""
    import importlib

    try:
        importlib.import_module(module_name).install()
    except ModuleNotFoundError as exc:
        # Credential-free wrapper scripts use the system Python, which can see
        # this directory through PYTHONPATH but not Hermes' gateway packages.
        # The long-lived Hermes process uses its venv and applies the patch.
        if exc.name not in {"gateway", "plugins"}:
            raise


class PatchOnImport:
    """Run the ``install()`` hooks when the gateway registry is imported.

    A ``sys.meta_path`` finder that claims exactly one module name, hands the
    real import back to the rest of the meta path, and wraps the resulting
    loader so the patches run immediately after the registry module executes.
    By then the registry is already in ``sys.modules``, so each ``install()``'s
    own ``from gateway.platform_registry import PlatformRegistry`` resolves
    without re-entering the import machinery.
    """

    def __init__(self, module_names):
        self._module_names = tuple(module_names)
        # Latched rather than removing ourselves from ``sys.meta_path``:
        # ``importlib._bootstrap._find_spec`` iterates that list live, so
        # mutating it mid-import can make a concurrent import skip a finder.
        self._fired = False

    def find_spec(self, fullname, path=None, target=None):
        if self._fired or fullname != TRIGGER_MODULE:
            return None
        # Latch before recursing, so the nested lookup below falls through to
        # the real finders instead of back into this one.
        self._fired = True

        import importlib.util

        spec = importlib.util.find_spec(fullname)
        if spec is None or spec.loader is None:
            return spec

        exec_module = spec.loader.exec_module
        module_names = self._module_names

        def exec_and_patch(module):
            exec_module(module)
            for module_name in module_names:
                _install(module_name)

        spec.loader.exec_module = exec_and_patch
        return spec


def install_hook(meta_path=None, environ=None):
    """Register the deferred patches: the configured relays', and the rest.

    Returns the finder that was registered. That used to be ``None`` on an
    install with no relay, when the relays were all there was to defer;
    ``UNCONDITIONAL_PATCHES`` means there is now always something. Registering
    the finder costs a list insert -- the imports it defers are what cost, and
    those still wait for the trigger module.
    """
    if meta_path is None:
        meta_path = sys.meta_path
    if environ is None:
        environ = os.environ

    enabled = [name for variable, name in RELAY_PATCHES if environ.get(variable)]
    enabled.extend(UNCONDITIONAL_PATCHES)

    finder = PatchOnImport(enabled)
    meta_path.insert(0, finder)
    return finder


install_hook()

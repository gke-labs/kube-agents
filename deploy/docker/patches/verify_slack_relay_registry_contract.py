"""Build-time gate for the relay patches' grip on Hermes' platform registry.

Run by ``deploy/docker/Dockerfile`` in the ``platform`` stage, against the real
``/opt/hermes`` tree and the scripts that will be on the runtime PYTHONPATH.
A failure here fails the image build.

The unit suite in ``agents/platform/scripts/test_slack_relay_patch.py`` covers
the shim against fakes, and fakes are written by the same hand as the shim.
That is not a hypothetical weakness. ``slack_relay_patch.install()`` replaces
``PlatformRegistry.register`` on the class, and until v2026.8.13 both the shim
and both test fakes declared ``register(self, entry)``. The base-image bump in
#718 gave upstream a keyword-only ``scope`` and had
``PluginContext.register_platform`` pass it on every registration. The fakes
agreed with the shim, CI was green, and the built image raised

    TypeError: register() got an unexpected keyword argument 'scope'

for *every* platform plugin -- Google Chat and Discord included, because the
shim sits on the class the whole gateway registers through. The gateway came up
with no chat adapters and nothing in the symptom pointed at a Slack file.

So this checks the shim against the thing it actually wraps:

1. the signatures and parameter defaults of every upstream callable the runtime
   patches replace, reimplement or call into are still the signatures and
   defaults they were written against -- the two relay patches, and
   ``sandbox_artifact_patch``, which wraps the kanban notifier's artifact
   delivery and reuses upstream's media-delivery denylist;
2. a plugin-sourced entry registers through the shim exactly as
   ``hermes_cli/plugins.py`` registers one, scope and all, and comes back
   relay-backed and findable;
3. a non-Slack entry registers untouched -- the blast radius, not the symptom;
4. the install-time sweep still finds an entry that was registered before the
   patch loaded, which since v2026.8.13 means finding it in ``_scoped_entries``
   rather than ``_entries``;
5. all of that again in a child process wired the way the pod is -- relay URL
   in the environment, scripts on ``PYTHONPATH`` -- so ``sitecustomize``'s
   import hook is what installs the patch, not this file.

Check 1 is a pin covering both callable signatures (`UPSTREAM_SIGNATURES`) and
critical parameter defaults (`PINNED_DEFAULTS`) on reimplemented methods. When
either fails, read the upstream method, decide whether the shim needs to change,
and then update the expectation deliberately -- do not refresh it to whatever
the new image says.

Checks 1-4 run in a process where ``SLACK_RELAY_URL`` is *unset at startup*,
because ``sitecustomize.install_hook()`` reads the environment as it is
imported: with the variable set, the hook is armed before this file gets a
word in and ``PlatformRegistry.register`` is already wrapped by the time the
signatures are read, which would pin the shim against itself. The wired
process is check 5's job, and it asserts the hook fired rather than installing
anything by hand.

``sandbox_artifact_patch`` cannot be held off the same way -- ``sitecustomize``
installs it unconditionally, with no variable to unset -- so the one signature
it wraps is read through ``inspect.unwrap`` instead. See
``upstream_callables()``.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys

from typing import Any

# Never dialled: every check here stops short of a request. It only has to look
# like a configured relay to ``relay_without_token()``.
RELAY_URL = "http://127.0.0.1:8765"

# Set by the parent on the wired child, and the only thing that tells the two
# runs apart.
WIRED = "SLACK_RELAY_CONTRACT_WIRED"

failures: list[str] = []


def relay_patch_variables() -> tuple[tuple[str, str], ...]:
    """``sitecustomize``'s own list of (env var, patch module) pairs.

    Read from the module rather than restated, because the whole point of this
    file is that a copy of upstream's shape drifts from it. An import error is
    left to propagate: the scripts are on PYTHONPATH by the time this runs, so
    a failure here means the build is wrong about that, and a fallback list
    would hide it.
    """
    import sitecustomize

    return tuple(sitecustomize.RELAY_PATCHES)


def check(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        fail(label, f"expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok    {label}")


def fail(label: str, why: str) -> None:
    failures.append(f"{label}: {why}")
    print(f"  FAIL  {label}")


def shape(func: object) -> str:
    """Parameter names and kinds, without annotations or default values.

    Annotations churn for cosmetic reasons (``Optional[str]`` becoming
    ``str | None`` breaks nothing); an added, renamed or reordered parameter is
    what breaks a shim.
    """
    rendered = []
    for param in inspect.signature(func).parameters.values():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            rendered.append(f"*{param.name}")
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            rendered.append(f"**{param.name}")
        else:
            if param.kind is inspect.Parameter.KEYWORD_ONLY and "*" not in "".join(
                rendered
            ):
                rendered.append("*")
            rendered.append(
                param.name + ("=..." if param.default is not param.empty else "")
            )
    return ", ".join(rendered)


# The signatures the two relay patches were written against. Keyed by a label
# that names the shim that depends on each one.
UPSTREAM_SIGNATURES = {
    "PlatformRegistry.register": "self, entry, *, scope=...",
    "PlatformRegistry.create_adapter": "self, name, config",
    "SlackAdapter.connect": "self, *, is_reconnect=...",
    "SlackAdapter.disconnect": "self",
    "SlackAdapter._start_socket_mode_handler": "self",
    "SlackAdapter._stop_socket_mode_handler": "self",
    "SlackAdapter._ensure_socket_watchdog": "self",
    "SlackAdapter._download_slack_file": "self, url, ext, audio=..., team_id=...",
    "SlackAdapter._download_slack_file_bytes": "self, url, team_id=...",
    "SlackAdapter.format_message": "self, content",
    "slack._standalone_send": (
        "pconfig, chat_id, message, *, thread_id=..., media_files=..., "
        "force_document=..., caption=..."
    ),
    "GoogleChatAdapter.connect": "self, *, is_reconnect=...",
    "GoogleChatAdapter.disconnect": "self",
    "GoogleChatAdapter._new_authed_http": "self",
    "GoogleChatAdapter._handle_setup_files_command": (
        "self, chat_id, thread_id, raw_text, sender_email=..."
    ),
    "GoogleChatAdapter._post_attachment_fallback": (
        "self, chat_id, path, filename, caption, thread_id"
    ),
    "GatewayKanbanWatchersMixin._deliver_kanban_artifacts": (
        "self, *, adapter, chat_id, metadata, event_payload, task"
    ),
    "BasePlatformAdapter.filter_local_delivery_paths": "file_paths",
    "base._media_delivery_strict_mode": "",
    "base._path_under_denied_prefix": "resolved",
}

# Default values the relay patches depend on. Keyed by (callable label, parameter name).
# Only reimplemented methods/functions where a changed upstream default creates silent
# drift are pinned here; delegating wrappers (PlatformRegistry.register,
# PlatformRegistry.create_adapter) pass through *args/**kwargs unchanged and are exempt
# from default pinning.
PINNED_DEFAULTS: dict[tuple[str, str], object] = {
    ("SlackAdapter.connect", "is_reconnect"): False,
    ("GoogleChatAdapter.connect", "is_reconnect"): False,
    ("SlackAdapter._download_slack_file", "audio"): False,
    ("SlackAdapter._download_slack_file", "team_id"): "",
    ("SlackAdapter._download_slack_file_bytes", "team_id"): "",
    ("slack._standalone_send", "thread_id"): None,
    ("slack._standalone_send", "media_files"): None,
    ("slack._standalone_send", "force_document"): False,
    ("slack._standalone_send", "caption"): None,
    ("GoogleChatAdapter._handle_setup_files_command", "sender_email"): None,
}


def upstream_callables() -> dict[str, Any]:
    """Resolve every pinned callable out of the image's own Hermes tree.

    ``_deliver_kanban_artifacts`` needs unwrapping first. This runs with
    ``/opt/defaults/scripts`` on ``PYTHONPATH``, so ``sitecustomize`` executes in
    this interpreter too and arms a finder that installs
    ``sandbox_artifact_patch`` as ``gateway.platform_registry`` loads -- and that
    patch replaces the method. Reading the attribute afterwards pins the shim
    against itself and checks nothing. Import order cannot get ahead of it,
    because everything below reaches the registry one way or another, so the
    patch applies ``functools.wraps`` and ``inspect.unwrap`` recovers the real
    method here. The identity check is what keeps that honest.
    """
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    import sandbox_artifact_patch

    kanban_deliver = GatewayKanbanWatchersMixin._deliver_kanban_artifacts
    if getattr(GatewayKanbanWatchersMixin, sandbox_artifact_patch.PATCH_FLAG, False):
        kanban_deliver = inspect.unwrap(kanban_deliver)
        if kanban_deliver is GatewayKanbanWatchersMixin._deliver_kanban_artifacts:
            fail(
                "GatewayKanbanWatchersMixin._deliver_kanban_artifacts",
                "sandbox_artifact_patch wrapped it without __wrapped__, so the "
                "pin would compare the shim against itself",
            )

    from gateway.platform_registry import PlatformRegistry
    from gateway.platforms import base as platforms_base
    from gateway.platforms.base import BasePlatformAdapter
    from plugins.platforms.google_chat import adapter as google_chat_adapter
    from plugins.platforms.slack import adapter as slack_adapter

    slack = slack_adapter.SlackAdapter
    google_chat = google_chat_adapter.GoogleChatAdapter
    return {
        "GatewayKanbanWatchersMixin._deliver_kanban_artifacts": kanban_deliver,
        "BasePlatformAdapter.filter_local_delivery_paths": (
            BasePlatformAdapter.filter_local_delivery_paths
        ),
        "base._media_delivery_strict_mode": platforms_base._media_delivery_strict_mode,
        "base._path_under_denied_prefix": platforms_base._path_under_denied_prefix,
        "PlatformRegistry.register": PlatformRegistry.register,
        "PlatformRegistry.create_adapter": PlatformRegistry.create_adapter,
        "SlackAdapter.connect": slack.connect,
        "SlackAdapter.disconnect": slack.disconnect,
        "SlackAdapter._start_socket_mode_handler": slack._start_socket_mode_handler,
        "SlackAdapter._stop_socket_mode_handler": slack._stop_socket_mode_handler,
        "SlackAdapter._ensure_socket_watchdog": slack._ensure_socket_watchdog,
        "SlackAdapter._download_slack_file": slack._download_slack_file,
        "SlackAdapter._download_slack_file_bytes": slack._download_slack_file_bytes,
        "SlackAdapter.format_message": slack.format_message,
        "slack._standalone_send": slack_adapter._standalone_send,
        "GoogleChatAdapter.connect": google_chat.connect,
        "GoogleChatAdapter.disconnect": google_chat.disconnect,
        "GoogleChatAdapter._new_authed_http": google_chat._new_authed_http,
        "GoogleChatAdapter._handle_setup_files_command": (
            google_chat._handle_setup_files_command
        ),
        "GoogleChatAdapter._post_attachment_fallback": (
            google_chat._post_attachment_fallback
        ),
    }


async def upstream_sender(_pconfig, _chat_id, _message, **_kwargs):
    """Stand-in for the plugin's own ``_standalone_send``."""
    return {"success": True, "sender": "upstream"}


def make_entry(name: str):
    """A ``source="plugin"`` entry shaped the way ``register_platform`` builds one."""
    from gateway.platform_registry import PlatformEntry

    return PlatformEntry(
        name=name,
        label=name.title(),
        adapter_factory=lambda config: None,
        check_fn=lambda: True,
        source="plugin",
        standalone_sender_fn=upstream_sender,
    )


def register_scoped(label: str, entry, scope: str) -> bool:
    """Register the way ``PluginContext.register_platform`` does, and say so.

    ``hermes_cli/plugins.py`` builds a ``source="plugin"`` entry and calls
    ``register(entry, scope=...)``. The scope is never ``None`` there --
    ``PluginManager`` defaults it to ``hermes_home_key()`` -- so the keyword
    the shim used to reject is on every real registration.

    Returns whether it landed, because every check after it reads an entry
    that a failed registration never touched: without this the regression
    reports itself as a stack trace out of some later assertion instead of as
    the one thing that went wrong.
    """
    from gateway.platform_registry import platform_registry

    try:
        platform_registry.register(entry, scope=scope)
    except TypeError as exc:
        fail(label, f"register(entry, scope=...) raised {exc}")
        return False
    print(f"  ok    {label}")
    return True


def relay_backed(entry) -> bool:
    return getattr(
        entry.standalone_sender_fn, "_credential_proxy_relay_patched", False
    )


def registry_checks(scope: str) -> None:
    """The behaviour both the hand-installed and the wired process must show."""
    from gateway.platform_registry import platform_registry

    slack_entry = make_entry("slack")
    slack_label = "a scoped Slack registration survives the shim"
    if register_scoped(slack_label, slack_entry, scope):
        check(
            "the Slack entry relays its standalone sends",
            relay_backed(slack_entry),
            True,
        )
        check(
            "the Slack entry reports itself reachable without a token",
            # ``is_connected`` is an optional field, left None by the entry the
            # plugin builds, so an unpatched entry has to read as a failed
            # check rather than as a TypeError from calling None.
            bool(slack_entry.is_connected and slack_entry.is_connected(None)),
            True,
        )
        check(
            "the Slack entry reached the registry",
            platform_registry.get("slack"),
            slack_entry,
        )

    # The blast radius, not the symptom: the shim sits on the class, so this is
    # the registration the v2026.8.13 TypeError actually broke most of.
    other = make_entry("google_chat")
    if register_scoped("a non-Slack platform still registers", other, scope):
        check(
            "a non-Slack platform is left alone",
            other.standalone_sender_fn,
            upstream_sender,
        )
        check(
            "the non-Slack entry reached the registry",
            platform_registry.get("google_chat"),
            other,
        )


def unwired() -> int:
    """Pin the signatures, then drive the shim with an explicit ``install()``."""
    from gateway.platform_registry import PlatformRegistry, platform_registry
    from hermes_constants import hermes_home_key

    # Read before anything installs, so a shim that replaced a method cannot be
    # mistaken for upstream declaring it. The guard in main() strips the relay
    # variables that would have armed one; this asserts the result rather than
    # trusting it, because both patches leave the same marker and a pin read
    # off a shim is a check that has quietly stopped checking anything.
    for sentinel in (
        "_slack_credential_proxy_relay_patched",
        "_slack_standalone_relay_patched",
        "_credential_proxy_relay_patched",
    ):
        check(
            f"no relay patch installed before the pin is read ({sentinel})",
            getattr(PlatformRegistry, sentinel, False),
            False,
        )

    for label, func in upstream_callables().items():
        check(f"upstream signature: {label}", shape(func), UPSTREAM_SIGNATURES[label])

    for (label, param_name), expected_default in PINNED_DEFAULTS.items():
        func = upstream_callables()[label]
        sig = inspect.signature(func)
        param = sig.parameters.get(param_name)
        if param is None:
            fail(
                f"upstream default: {label}({param_name})",
                "parameter missing from signature",
            )
        else:
            check(
                f"upstream default: {label}({param_name})",
                param.default,
                expected_default,
            )

    # --- the Google Chat message cap the inline paste is budgeted against ---
    # google_chat_relay_patch splits a deliverable into messages sized to fit
    # under the adapter's own ceiling, and it holds that ceiling as its own
    # constant. A signature pin does not cover a number, and the failure is
    # quiet in the same way: the unit suite compares chunks against the copy,
    # so a base image that lowers the real cap keeps every test green and ships
    # an image whose every fenced multi-part paste is re-split by send() with
    # the fence cut in half. verify_kanban_progress_lines.py guards MAX_RENDER
    # against the same constant for the same reason.
    import google_chat_relay_patch

    from plugins.platforms.google_chat import adapter as google_chat_adapter

    upstream_cap = getattr(google_chat_adapter, "_MAX_TEXT_LENGTH", None)
    check(
        "the adapter's text cap was located",
        isinstance(upstream_cap, int),
        True,
    )
    if isinstance(upstream_cap, int):
        check(
            "the relay patch's copy of the cap matches the adapter's",
            google_chat_relay_patch.MESSAGE_CHAR_CAP,
            upstream_cap,
        )
        check(
            "a decorated chunk still fits under the cap",
            google_chat_relay_patch._payload_budget(fenced=True)
            + google_chat_relay_patch.HEADER_RESERVE_CHARS
            + google_chat_relay_patch.FENCE_RESERVE_CHARS
            <= upstream_cap,
            True,
        )

    os.environ["SLACK_RELAY_URL"] = RELAY_URL
    # No token anywhere: the credential proxy holds the only one, and that is
    # the deployment these patches exist for.
    os.environ.pop("SLACK_BOT_TOKEN", None)

    import slack_relay_patch

    stock_register = PlatformRegistry.register
    slack_relay_patch.install()
    check(
        "install() replaced PlatformRegistry.register",
        PlatformRegistry.register is not stock_register,
        True,
    )

    scope = hermes_home_key()
    registry_checks(scope)

    # --- the ordering the register() wrapper cannot cover -------------------
    # An entry already in place when install() runs is reached only by the
    # sweep over the registry's private entry maps. Since v2026.8.13 a scoped
    # registration lands in _scoped_entries, so a sweep that reads only
    # _entries finds nothing, patches nothing, and warns about nothing.
    #
    # This ordering does not occur in the pod: sitecustomize's hook calls
    # install() as gateway.platform_registry finishes executing, before
    # anything can register, and nothing else calls install(). The sweep is
    # cover for install() gaining a later caller, and this check is what keeps
    # it honest against a base image that moves the maps under it -- it has to
    # be staged deliberately, as below, because nothing produces it on its own.
    platform_registry.unregister("slack", scope=scope)
    PlatformRegistry.register = stock_register
    for sentinel in (
        "_slack_credential_proxy_relay_patched",
        "_slack_standalone_relay_patched",
    ):
        if hasattr(PlatformRegistry, sentinel):
            delattr(PlatformRegistry, sentinel)
    pre_registered = make_entry("slack")
    platform_registry.register(pre_registered, scope=scope)
    slack_relay_patch.install()
    check(
        "the sweep finds a Slack entry registered before the patch loaded",
        relay_backed(pre_registered),
        True,
    )

    return report("unwired")


def wired() -> int:
    """Assert the pod's own wiring installed the patch, and that it works.

    Nothing here calls ``install()``. Importing the registry is what fires
    ``sitecustomize``'s hook, exactly as the gateway process does it, so a
    ``sitecustomize`` that stopped arming the hook fails this and passes
    everything above.
    """
    from gateway.platform_registry import PlatformRegistry
    from hermes_constants import hermes_home_key

    if not getattr(PlatformRegistry, "_slack_standalone_relay_patched", False):
        # Everything below asks what the patch did, so with no patch installed
        # they answer a question nobody asked. Report the one real fault.
        fail(
            "importing the registry installed the Slack relay patch",
            "PlatformRegistry._slack_standalone_relay_patched is unset -- "
            "sitecustomize did not arm the hook for SLACK_RELAY_URL",
        )
        return report("wired")
    print("  ok    importing the registry installed the Slack relay patch")

    registry_checks(hermes_home_key())
    return report("wired")


def report(phase: str) -> int:
    if failures:
        print(f"\nverify_slack_relay_registry_contract [{phase}] FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nverify_slack_relay_registry_contract [{phase}]: all checks passed")
    return 0


def main() -> int:
    if os.environ.get(WIRED):
        return wired()

    # Every variable ``sitecustomize`` arms a patch on, not just Slack's. The
    # operator sets GOOGLE_CHAT_RELAY_URL on the agent container, and
    # google_chat_relay_patch.install() replaces create_adapter too -- so
    # running this inside a pod with only SLACK_RELAY_URL stripped reads the
    # Google Chat shim and reports create_adapter as drifted when it has not.
    # That misfire is worse than a missed check: the docstring above tells the
    # reader to update the pin deliberately, and doing so here would pin the
    # shim against itself and delete the only guard on that method.
    inherited = [variable for variable, _ in relay_patch_variables() if os.environ.get(variable)]
    if inherited:
        clean_env = dict(os.environ)
        for variable in inherited:
            clean_env.pop(variable)
        return subprocess.run(
            [sys.executable, os.path.abspath(__file__)], env=clean_env, check=False
        ).returncode

    status = unwired()

    print("\n-- again, wired the way the pod wires it --")
    child_env = dict(os.environ, **{WIRED: "1", "SLACK_RELAY_URL": RELAY_URL})
    child_env.pop("SLACK_BOT_TOKEN", None)
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__)], env=child_env, check=False
    )
    return status or child.returncode


if __name__ == "__main__":
    sys.exit(main())

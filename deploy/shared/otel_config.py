#!/usr/bin/env python3
"""Point the hermes_otel plugin at the collector the operator resolved.

The agent does not export spans through OTEL_EXPORTER_OTLP_ENDPOINT. It exports through
the `hermes_otel` plugin, whose backend endpoint is baked into the image
(deploy/docker/Dockerfile, "Install and enable hermes-otel plugin"). Setting the env var
alone therefore changes nothing — this module is what makes the resolved endpoint real.

Why every copy, not just one
----------------------------
Each Hermes profile carries its own plugins/hermes_otel/config.yaml:
profile_scaffold.overlay_template and cluster_agent_profile both copytree
/opt/defaults/plugins into the new profile. So `apply_all` sweeps the root home and every
profiles/*/ under it. Cluster profiles are created at onboarding time, long after this
runs at start-up, which is why cluster_agent_profile calls `apply` again for its own copy.

Why source_path
---------------
The config lives on the data PVC and start-up reruns this every time. Deriving the result
from the pristine /opt/defaults copy rather than editing the PVC copy in place means (a) a
newer baked endpoint in a fresh image actually lands — docker-entrypoint's `cp -ru` cannot
refresh a file this script has already touched — and (b) clearing the override reverts to
the baked default instead of pinning a stale customer endpoint on the PVC forever.

Nothing here is fatal. Telemetry misconfiguration must not stop an agent from starting, so
every failure warns on stderr and returns False.

Usage:
    otel_config.py --hermes-home DIR [--service-name NAME] [--endpoint URL]
                   [--defaults-plugins DIR]
"""

from __future__ import annotations

import argparse
import copy
import pathlib
import sys
from urllib.parse import urlsplit, urlunsplit

import yaml

DEFAULT_DEFAULTS_PLUGINS = "/opt/defaults/plugins"

# The per-signal path hermes_otel POSTs traces to. The OTLP spec defines the endpoint as a
# base URL with the signal path appended, so the operator and the Helm chart carry base
# URLs and this is the only place that knows the path.
TRACES_PATH = "v1/traces"


def log(msg: str) -> None:
    print(f"[OTEL-CONFIG] {msg}", file=sys.stderr)


def traces_url(base: str) -> str:
    """Return the traces URL for an OTLP base URL.

    Idempotent: a base that already ends in the traces path is returned unchanged, because
    this runs on every start against a file that persists on the PVC — and because
    operators do paste the full traces URL into the endpoint field.

    A bare host:port is assumed to be http, matching the scheme-less collector addresses
    that turn up in values files.
    """
    base = base.strip()
    if not base:
        return ""

    if "://" not in base:
        base = f"http://{base}"

    scheme, netloc, path, query, fragment = urlsplit(base)
    path = path.rstrip("/")
    if not path.endswith(f"/{TRACES_PATH}"):
        # A non-root path is a prefix the collector is mounted under, so the signal path
        # goes after it rather than replacing it.
        path = f"{path}/{TRACES_PATH}"
    return urlunsplit((scheme, netloc, path, query, fragment))


def _load(path: pathlib.Path) -> dict:
    """Read a YAML mapping, or raise."""
    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"expected a mapping, got {type(loaded).__name__}")
    return loaded


def apply(
    config_path: str | pathlib.Path,
    service_name: str | None = None,
    endpoint: str | None = None,
    source_path: str | pathlib.Path | None = None,
) -> bool:
    """Write service.name and, when given, the collector endpoint into one plugin config.

    An unset endpoint leaves `backends` alone, so the baked default stands and an install
    that never configures telemetry keeps exporting exactly where it always did.

    Note that leaving `backends` alone is not the same as leaving the file alone. With
    source_path given, the result is re-derived from the pristine copy on every start, so
    a hand-edit made to the PVC copy of this one file does not survive a restart — that is
    the point of source_path (see the module docstring), not an accident, but it does mean
    the plugin config is operator-owned rather than editable in place.

    Returns True when the file was written or was already correct.
    """
    config_path = pathlib.Path(config_path)
    origin = pathlib.Path(source_path) if source_path else config_path

    if not origin.exists():
        return False

    try:
        current = _load(config_path) if config_path.exists() else {}
        config = _load(origin) if origin != config_path else copy.deepcopy(current)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log(f"WARN: cannot read {origin}: {exc}; leaving the plugin config alone")
        return False

    attrs = config.setdefault("resource_attributes", {})
    if not isinstance(attrs, dict):
        log(f"WARN: resource_attributes in {origin} is not a mapping; replacing it")
        attrs = config["resource_attributes"] = {}
    if service_name:
        attrs["service.name"] = service_name
    else:
        attrs.pop("service.name", None)

    if endpoint:
        url = traces_url(endpoint)
        backends = config.get("backends")
        if not isinstance(backends, list) or not backends:
            backends = config["backends"] = [{"name": "otlp", "type": "otlp"}]
        first = backends[0]
        if not isinstance(first, dict):
            log(f"WARN: backends[0] in {origin} is not a mapping; replacing it")
            first = backends[0] = {"name": "otlp", "type": "otlp"}
        previous = first.get("endpoint")
        if previous != url:
            # Only the endpoint moves. name/type/headers are whatever the image baked or
            # an operator edited, and the name in particular may be keyed on elsewhere —
            # so it keeps its baked value even when repointed, which is worth saying out
            # loud in the log rather than leaving to be discovered.
            log(f"Repointing backend '{first.get('name')}' from {previous} to {url}")
            first["endpoint"] = url

    if config == current:
        return True

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(config))
    except OSError as exc:
        log(f"WARN: cannot write {config_path}: {exc}; the agent keeps its previous telemetry config")
        return False
    return True


def apply_all(
    hermes_home: str | pathlib.Path,
    service_name: str | None = None,
    endpoint: str | None = None,
    defaults_plugins: str | pathlib.Path | None = DEFAULT_DEFAULTS_PLUGINS,
) -> dict[str, bool]:
    """Apply to the root home's plugin config and to every profile's copy.

    Returns a {path: written} map, so a caller can see which copies were reached without
    having to care that a miss is not an error.
    """
    hermes_home = pathlib.Path(hermes_home)
    defaults = pathlib.Path(defaults_plugins) if defaults_plugins else None
    source = defaults / "hermes_otel" / "config.yaml" if defaults else None
    if source is not None and not source.exists():
        source = None

    targets = [hermes_home / "plugins" / "hermes_otel" / "config.yaml"]
    profiles = hermes_home / "profiles"
    if profiles.is_dir():
        targets += sorted(p / "plugins" / "hermes_otel" / "config.yaml" for p in profiles.iterdir() if p.is_dir())

    results: dict[str, bool] = {}
    for target in targets:
        # Only sweep copies that already exist: a profile without the plugin never had it,
        # and creating one here would enable telemetry Hermes was not asked for.
        if not target.exists():
            continue
        results[str(target)] = apply(target, service_name, endpoint, source_path=source)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hermes-home", required=True, help="Hermes home to sweep")
    parser.add_argument("--service-name", default=None, help="OTEL service.name to record")
    parser.add_argument("--endpoint", default=None, help="OTLP base URL; unset keeps the baked default")
    parser.add_argument(
        "--defaults-plugins",
        default=DEFAULT_DEFAULTS_PLUGINS,
        help="Pristine plugin directory to derive each config from",
    )
    args = parser.parse_args(argv)

    results = apply_all(args.hermes_home, args.service_name, args.endpoint, args.defaults_plugins)
    for path, ok in results.items():
        if not ok:
            log(f"WARN: could not update {path}")
    # Always succeed: telemetry wiring is not worth failing a container start over.
    return 0


if __name__ == "__main__":
    sys.exit(main())

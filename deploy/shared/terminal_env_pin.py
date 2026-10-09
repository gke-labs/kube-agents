#!/usr/bin/env python3
"""Copy the operator's managed terminal settings into each Hermes profile's .env.

The operator pins `terminal:` in the managed config ($HERMES_MANAGED_DIR/config.yaml): the
ssh backend into the shell sandbox. Hermes applies that overlay when it loads a config, but
the per-profile terminal scope a scheduled run executes under
(tools/terminal_scope.build_profile_terminal_scope) is built from the profile's own .env
and config.yaml and never reads the managed scope. Without this, a cron job's terminal
falls back to Hermes' default `local` backend instead of the sandbox.

The scope does read TERMINAL_* from the profile .env, which is where
`hermes config set terminal.*` mirrors terminal settings, so this writes the managed values
there, then asks Hermes for the scope and fails unless it resolves to them. config.yaml is
not an option: save_config strips managed leaves from it. For the same reason, a key in the
profile's config.yaml that outranks the copy is deleted.

Only the variables the managed block maps to are replaced. Any other TERMINAL_* line is a
setting of the profile's own (`hermes config set terminal.timeout` writes one) and stays.

A failure is fatal to the caller: a profile whose scheduled runs would not use the
managed terminal is not one to start or scaffold. The start-up sweep makes one exception, below.

Usage:
    terminal_env_pin.py --hermes-home DIR [--sweep]
    terminal_env_pin.py --build-check
"""

from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import re
import stat
import sys
import tempfile

import yaml

MANAGED_DIR_ENV = "HERMES_MANAGED_DIR"
HERMES_HOME_ENV = "HERMES_HOME"
CONFIG_NAME = "config.yaml"
TERMINAL_SECTION = "terminal"
BACKEND_KEY = "backend"
REQUIRED_BACKEND = "ssh"
BACKEND_ENV = "TERMINAL_ENV"
ENV_FILE_NAME = ".env"
PROFILES_DIR_NAME = "profiles"
DEFAULT_PROFILE_NAME = "default"
TEMP_PREFIX = ".env_"
TEMP_SUFFIX = ".tmp"
NEWLINE = "\n"

# The shapes agent.secret_scope.load_env_file accepts; see defined_key.
EXPORT_PREFIX = "export "
COMMENT_PREFIX = "#"
UTF8_BOM = b"\xef\xbb\xbf"
PRIMARY_CODEC = "utf-8"
FALLBACK_CODEC = "latin-1"

# A .env this creates is group read/write like the ones the install already has: the
# containers sharing the data volume run as different users in one fsGroup.
NEW_ENV_MODE = 0o660

# Every value the operator renders is a backend name, host, user, port, path or number. A
# value outside this set would need quoting in .env, and a newline would add a line of its own.
PLAIN_VALUE = re.compile(r"[A-Za-z0-9_./:@-]+")

# The operator's terminal block (managedTerminalConfig in platformagent_manifests.go), used
# only by --build-check against a throwaway managed dir.
BUILD_CHECK_TERMINAL = {
    "backend": REQUIRED_BACKEND,
    "ssh_host": "platform-agent-shell-0.platform-agent-shell.kubeagents-system.svc.cluster.local",
    "ssh_user": "agent",
    "ssh_port": 2222,
    "ssh_key": "/etc/sandbox-ssh/id_ed25519",
    "lifetime_seconds": 2592000,
    "workspace_root": "/opt/data",
}
BUILD_CHECK_OTHER_LINE = "KUBECONFIG=/opt/data/kube/config\n"
# A profile config.yaml from before the terminal pin, whose backend the copy has to delete.
BUILD_CHECK_KEPT_LINE = "# unrelated settings survive the delete\n"
BUILD_CHECK_KEPT_SETTING = "  timeout: 60\n"
BUILD_CHECK_PROFILE_CONFIG = BUILD_CHECK_KEPT_LINE + "terminal:\n  backend: local\n" + BUILD_CHECK_KEPT_SETTING
# A named profile the sweep has to find. profile_homes imports private Hermes names that no
# other build step touches, and a pod whose sweep cannot import them does not start.
BUILD_CHECK_PROFILE_NAME = "check"
# The Cluster Agent preflight (cluster_preflight.sh) budgets its brokered calls
# under the terminal tool's default call timeout, which is Hermes' and out of
# that script's reach; it carries a mirror, and the build check below reads
# the default out of the Hermes this image ships and holds the mirror to it.
# Beside this module in the image, under agents/platform/scripts in a checkout.
PREFLIGHT_SCRIPT_NAME = "cluster_preflight.sh"
PREFLIGHT_SCRIPT_DIRS = (
    pathlib.Path(__file__).resolve().parent,
    pathlib.Path(__file__).resolve().parents[2] / "agents" / "platform" / "scripts",
)
PREFLIGHT_TERMINAL_MIRROR_RE = re.compile(r"^readonly TERMINAL_TOOL_TIMEOUT_SECONDS=(\d+)$", re.MULTILINE)
TERMINAL_TOOL_FILE = pathlib.Path("tools") / "terminal_tool.py"
TERMINAL_TOOL_DEFAULT_RE = re.compile(r'_parse_env_var\("TERMINAL_TIMEOUT",\s*"(\d+)"\)')


class PinError(Exception):
    """A profile's scheduled runs would not use the managed terminal."""


class ScopeUnavailable(PinError):
    """Hermes cannot read or parse the profile's .env or config.yaml, so its scheduled runs fail.

    pin_profile raises it only once the copy is written, which leaves the config.yaml case.
    """


def log(msg: str) -> None:
    print(f"[TERMINAL-ENV-PIN] {msg}", file=sys.stderr)


def describe(exc: BaseException) -> str:
    """A read or parse error by type and position, without its text.

    A YAML error's text quotes the lines around the fault, and the files read here hold more
    than terminal settings. PyYAML and ruamel.yaml marks both carry line and column.
    """
    for err in (exc, exc.__cause__):
        mark = getattr(err, "problem_mark", None)
        if mark is not None:
            return f"{type(err).__name__} at line {mark.line + 1}, column {mark.column + 1}"
        if isinstance(err, OSError):
            return str(err)
    return type(exc.__cause__ or exc).__name__


def managed_terminal_block(managed: pathlib.Path | None, override: str) -> dict | None:
    """The managed terminal: block, or None when there is nothing to pin.

    override is HERMES_MANAGED_DIR, the entrypoint's marker of an operator-managed pod. The
    operator always renders the ssh terminal, so with it set a missing directory, an
    unreadable file, a missing block or another backend is an error. Without it Hermes still
    falls back to /etc/hermes, which every image creates with no config.yaml, and ignores a
    file it cannot read or parse; so does this.
    """
    if managed is None:
        if override:
            raise PinError(f"{MANAGED_DIR_ENV}={override} is not a directory")
        return None
    path = managed / CONFIG_NAME
    try:
        raw = yaml.safe_load(path.read_text(encoding=PRIMARY_CODEC))
    except Exception as exc:  # Hermes' managed read ignores any failure, not only a YAML one
        if override:
            raise PinError(f"cannot read {path}: {describe(exc)}") from exc
        if not isinstance(exc, FileNotFoundError):
            log(f"WARN: ignoring {path}, as Hermes does: {describe(exc)}")
        return None
    terminal = raw.get(TERMINAL_SECTION) if isinstance(raw, dict) else None
    if not override:
        return terminal if isinstance(terminal, dict) else None
    if not isinstance(terminal, dict) or terminal.get(BACKEND_KEY) != REQUIRED_BACKEND:
        raise PinError(f"{path} does not pin terminal.{BACKEND_KEY}: {REQUIRED_BACKEND}")
    return terminal


def managed_terminal_env() -> dict[str, str]:
    """The TERMINAL_* variables the managed terminal block sets, by Hermes' own mapping.

    Empty when there is nothing to pin. workspace_root maps to no variable and is skipped:
    sandbox_exec reads it from the managed file itself. Outside an operator pod a value
    .env cannot hold as a plain line is left out rather than stopping the container.
    """
    from hermes_cli.config import (
        TERMINAL_CONFIG_ENV_MAP,
        _terminal_config_value_is_bridgeable,
        _terminal_env_value,
    )
    from hermes_cli.managed_scope import get_managed_dir

    override = os.environ.get(MANAGED_DIR_ENV, "").strip()
    managed = get_managed_dir()
    terminal = managed_terminal_block(managed, override)
    if terminal is None:
        return {}
    pinned: dict[str, str] = {}
    for key, value in terminal.items():
        env_var = TERMINAL_CONFIG_ENV_MAP.get(key)
        if env_var is None or value is None or not _terminal_config_value_is_bridgeable(key, value):
            continue
        text = _terminal_env_value(value)
        if not PLAIN_VALUE.fullmatch(text):
            if override:
                raise PinError(f"{managed / CONFIG_NAME}: terminal.{key} is not a plain value")
            log(f"WARN: not copying terminal.{key} from {managed / CONFIG_NAME}: not a plain value")
            continue
        pinned[env_var] = text
    if terminal.get(BACKEND_KEY) is not None and BACKEND_ENV not in pinned:
        # Every later check compares only what was copied, so a lost backend would pass them.
        raise PinError(f"this Hermes does not map terminal.{BACKEND_KEY} to {BACKEND_ENV}; the copy cannot pin it")
    return pinned


def defined_key(line: str) -> str | None:
    """The key a .env line assigns, by the rules of agent.secret_scope.load_env_file."""
    text = line.strip()
    if not text or text.startswith(COMMENT_PREFIX):
        return None
    if text.startswith(EXPORT_PREFIX):
        text = text[len(EXPORT_PREFIX) :].lstrip()
    key, sep, _ = text.partition("=")
    key = key.strip()
    return key if sep and key else None


def render_env(text: str, pinned: dict[str, str]) -> str:
    """Set each pinned variable once, where it first appears; leave every other line as it was."""
    out: list[str] = []
    written: set[str] = set()
    for line in text.splitlines(keepends=True):
        key = defined_key(line)
        if key not in pinned:
            out.append(line)
            continue
        if key in written:
            continue
        ending = line[len(line.splitlines()[0]) :] or NEWLINE
        out.append(f"{key}={pinned[key]}{ending}")
        written.add(key)
    missing = [key for key in pinned if key not in written]
    if missing:
        if out and out[-1] == out[-1].splitlines()[0]:
            out.append(NEWLINE)
        out.extend(f"{key}={pinned[key]}{NEWLINE}" for key in missing)
    return "".join(out)


def _decode(raw: bytes) -> tuple[bytes, str, str]:
    """Split off a BOM and decode the way load_env_file does, so re-encoding is lossless."""
    bom = UTF8_BOM if raw.startswith(UTF8_BOM) else b""
    body = raw[len(bom) :]
    try:
        return bom, body.decode(PRIMARY_CODEC), PRIMARY_CODEC
    except UnicodeDecodeError:
        return bom, body.decode(FALLBACK_CODEC), FALLBACK_CODEC


def write_env(env_path: pathlib.Path, pinned: dict[str, str]) -> bool:
    """Apply pinned to env_path atomically, keeping its owner and mode. True if it changed.

    A symlinked .env is written through, as Hermes' own writer does, so the link survives.
    """
    target = pathlib.Path(os.path.realpath(env_path)) if env_path.is_symlink() else env_path
    try:
        before: os.stat_result | None = target.stat()
        raw = target.read_bytes()
    except FileNotFoundError:
        before, raw = None, b""
    bom, text, codec = _decode(raw)
    data = bom + render_env(text, pinned).encode(codec)
    if before is not None and data == raw:
        return False
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if before is None:
                os.fchmod(handle.fileno(), NEW_ENV_MODE)
            else:
                made = os.fstat(handle.fileno())
                if (made.st_uid, made.st_gid) != (before.st_uid, before.st_gid):
                    try:
                        os.fchown(handle.fileno(), before.st_uid, before.st_gid)
                    except PermissionError:
                        # Only root gives a file to another user, and a CR sidecar with its
                        # own runAsUser can own one. The group is how the other containers
                        # on the volume read it, so keep that rather than stop the pod.
                        os.fchown(handle.fileno(), -1, before.st_gid)
                # After the chown, which may clear mode bits.
                os.fchmod(handle.fileno(), stat.S_IMODE(before.st_mode))
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return True


def resolved_scope(home: pathlib.Path) -> dict[str, str]:
    """The TERMINAL_* policy Hermes gives a scheduled run in this profile."""
    from tools.terminal_scope import TerminalPolicyUnavailable, build_profile_terminal_scope

    try:
        return build_profile_terminal_scope(home)
    except TerminalPolicyUnavailable as exc:
        raise ScopeUnavailable(f"Hermes cannot build the terminal scope for {home}: {describe(exc)}") from exc


def config_overrides(home: pathlib.Path, env_vars: list[str]) -> list[str]:
    """The keys in home's config.yaml terminal block that set one of env_vars.

    The profile config.yaml outranks .env in the scope, so a key there defeats the copy.
    """
    try:
        raw = yaml.safe_load((home / CONFIG_NAME).read_text(encoding=PRIMARY_CODEC))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    terminal = raw.get(TERMINAL_SECTION) if isinstance(raw, dict) else None
    if not isinstance(terminal, dict):
        return []
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

    return sorted(f"{TERMINAL_SECTION}.{key}" for key in terminal if TERMINAL_CONFIG_ENV_MAP.get(key) in env_vars)


def strip_config_keys(path: pathlib.Path, keys: list[str]) -> None:
    """Delete keys from a profile config.yaml, keeping its comments, order, owner and mode.

    The managed config already sets them everywhere a scheduled run is not, and Hermes' own
    save_config drops every managed key from this file the same way.
    """
    from utils import atomic_roundtrip_yaml_update

    try:
        for key in keys:
            atomic_roundtrip_yaml_update(path, key, None)
    except Exception as exc:
        raise PinError(f"cannot delete {', '.join(keys)} from {path}: {describe(exc)}; delete them by hand") from exc
    log(f"deleted {', '.join(keys)} from {path}: the managed config sets them")


def pin_profile(home: pathlib.Path, pinned: dict[str, str]) -> bool:
    """Write pinned into home's .env and confirm Hermes resolves it. True if a file changed.

    A profile config.yaml key that outranks the copy is deleted first.
    """
    home = pathlib.Path(home)
    env_path = home / ENV_FILE_NAME
    try:
        changed = write_env(env_path, pinned)
    except OSError as exc:
        # Fatal even when Hermes cannot read the profile or already resolves the values:
        # the sweep's skip of an unreadable profile is safe only once .env holds the copy.
        raise PinError(f"cannot rewrite {env_path}: {exc}") from exc
    scope = resolved_scope(home)
    wrong = sorted(key for key, value in pinned.items() if scope.get(key) != value)
    overrides = config_overrides(home, wrong) if wrong else []
    if overrides:
        strip_config_keys(home / CONFIG_NAME, overrides)
        scope = resolved_scope(home)
        wrong = sorted(key for key, value in pinned.items() if scope.get(key) != value)
    if wrong:
        raise PinError(
            f"scheduled runs in {home} would not use the managed terminal; Hermes resolves another {', '.join(wrong)}"
        )
    return changed or bool(overrides)


def pin(home: pathlib.Path) -> bool:
    """Pin one profile home. False, and no change, when there is nothing to pin."""
    pinned = managed_terminal_env()
    return bool(pinned) and pin_profile(home, pinned)


def profile_homes(root: pathlib.Path) -> list[pathlib.Path]:
    """The default home and every live named profile under it, as Hermes enumerates them."""
    from hermes_cli.profiles import _PROFILE_ID_RE, named_profile_is_deleted

    homes = [root]
    profiles = root / PROFILES_DIR_NAME
    if profiles.is_dir():
        homes += [
            entry
            for entry in sorted(profiles.iterdir())
            if entry.is_dir()
            and entry.name != DEFAULT_PROFILE_NAME
            and _PROFILE_ID_RE.match(entry.name)
            and not named_profile_is_deleted(entry)
        ]
    return homes


def sweep(root: pathlib.Path) -> int:
    """Pin every profile under root. 1 if, in an operator pod, the managed block is unusable,
    or if any profile's scheduled runs would not use the managed terminal.

    A profile whose .env took the copy but whose config.yaml Hermes cannot read or parse is
    reported and skipped: its scheduled runs fail until the file is fixed, which is safe, and
    stopping the pod for it would take every other profile down with it.
    """
    try:
        pinned = managed_terminal_env()
    except PinError as exc:
        log(f"ERROR: {exc}")
        return 1
    if not pinned:
        log("no managed terminal settings to copy; nothing to pin")
        return 0
    failed = []
    for home in profile_homes(root):
        try:
            if pin_profile(home, pinned):
                log(f"pinned the managed terminal in {home / ENV_FILE_NAME}")
        except ScopeUnavailable as exc:
            log(f"WARN: {exc}; this profile's scheduled runs fail until it is fixed")
        except PinError as exc:
            log(f"ERROR: {exc}")
            failed.append(str(home))
    if failed:
        log(f"ERROR: scheduled runs would not use the managed terminal in: {', '.join(failed)}")
        return 1
    return 0


def hermes_root() -> pathlib.Path:
    """The checkout or install the Hermes on the path comes from."""
    import hermes_cli

    return pathlib.Path(hermes_cli.__file__).resolve().parents[1]


def hermes_terminal_default_seconds(root: pathlib.Path | None = None) -> int:
    """The terminal tool's default call timeout, read from the Hermes at `root`
    (the one on the path when None)."""
    tool = (hermes_root() if root is None else root) / TERMINAL_TOOL_FILE
    match = TERMINAL_TOOL_DEFAULT_RE.search(tool.read_text(encoding=PRIMARY_CODEC))
    if match is None:
        raise PinError(f"{tool} no longer declares the TERMINAL_TIMEOUT default the way this check reads it")
    return int(match.group(1))


def preflight_terminal_mirror_seconds() -> int:
    """The preflight's mirror of that default, from the script beside this one or in the checkout."""
    for directory in PREFLIGHT_SCRIPT_DIRS:
        script = directory / PREFLIGHT_SCRIPT_NAME
        if script.is_file():
            match = PREFLIGHT_TERMINAL_MIRROR_RE.search(script.read_text(encoding=PRIMARY_CODEC))
            if match is None:
                raise PinError(f"{script} no longer declares TERMINAL_TOOL_TIMEOUT_SECONDS")
            return int(match.group(1))
    raise PinError(f"{PREFLIGHT_SCRIPT_NAME} not found in {[str(d) for d in PREFLIGHT_SCRIPT_DIRS]}")


def terminal_default_mismatch(root: pathlib.Path | None = None) -> str | None:
    """The error to log when the preflight's mirror disagrees with the Hermes at `root`
    (the one on the path when None), else None."""
    hermes, mirror = hermes_terminal_default_seconds(root), preflight_terminal_mirror_seconds()
    if hermes == mirror:
        return None
    return (
        f"the terminal tool's default call timeout is {hermes}s on this Hermes, but "
        f"{PREFLIGHT_SCRIPT_NAME} mirrors it as {mirror}s (TERMINAL_TOOL_TIMEOUT_SECONDS); "
        "move the mirror, and the budget it derives, with the Hermes bump"
    )


def build_check() -> int:
    """Against a throwaway managed dir and profile: the copy must make Hermes resolve ssh,
    including over a backend the profile's config.yaml still sets, and the sweep must find a
    named profile.

    Also asks Hermes before the copy. If it already resolves ssh, a Hermes release has
    started applying the managed scope to the profile terminal scope itself, and this module
    may no longer be needed. That warns rather than fails: the copy still agrees with it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        managed, home = root / "managed", root / "profile"
        managed.mkdir()
        home.mkdir()
        (managed / CONFIG_NAME).write_text(yaml.safe_dump({TERMINAL_SECTION: BUILD_CHECK_TERMINAL}))
        (home / ENV_FILE_NAME).write_text(BUILD_CHECK_OTHER_LINE)
        os.environ[MANAGED_DIR_ENV] = str(managed)
        os.environ[HERMES_HOME_ENV] = str(home)
        try:
            if resolved_scope(home).get(BACKEND_ENV) == REQUIRED_BACKEND:
                log(
                    "WARNING: Hermes now resolves the managed terminal backend for a profile "
                    "without the .env copy. Check that scheduled runs use ssh on this Hermes, "
                    "then remove terminal_env_pin.py and its callers."
                )
            (home / CONFIG_NAME).write_text(BUILD_CHECK_PROFILE_CONFIG)
            pin_profile(home, managed_terminal_env())
        except PinError as exc:
            log(f"ERROR: {exc}")
            return 1
        if BUILD_CHECK_OTHER_LINE not in (home / ENV_FILE_NAME).read_text():
            log(f"ERROR: the copy dropped an unrelated line from {home / ENV_FILE_NAME}")
            return 1
        kept = (home / CONFIG_NAME).read_text()
        if BUILD_CHECK_KEPT_LINE not in kept or BUILD_CHECK_KEPT_SETTING not in kept:
            log(f"ERROR: the delete dropped an unrelated line from {home / CONFIG_NAME}")
            return 1
        named = home / PROFILES_DIR_NAME / BUILD_CHECK_PROFILE_NAME
        named.mkdir(parents=True)
        try:
            homes = profile_homes(home)
        except ImportError as exc:
            log(f"ERROR: the sweep cannot list profiles on this Hermes: {exc}")
            return 1
        if homes != [home, named]:
            log(f"ERROR: the sweep would not pin the named profile at {named}")
            return 1
    try:
        mismatch = terminal_default_mismatch()
    except (PinError, ImportError, OSError) as exc:
        log(f"ERROR: could not compare the preflight's terminal-timeout mirror with this Hermes: {exc}")
        return 1
    if mismatch is not None:
        log(f"ERROR: {mismatch}")
        return 1
    log("build check passed: the .env copy makes Hermes resolve the managed ssh terminal")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--hermes-home", type=pathlib.Path, help="profile home to pin")
    mode.add_argument("--build-check", action="store_true", help="check this Hermes against a throwaway profile")
    parser.add_argument("--sweep", action="store_true", help="also pin every named profile under --hermes-home")
    args = parser.parse_args(argv)
    if args.build_check:
        return build_check()
    if args.sweep:
        return sweep(args.hermes_home)
    try:
        pin(args.hermes_home)
    except PinError as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

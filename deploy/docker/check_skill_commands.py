#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build-time check that the shell commands shipped skills teach pass Tirith.

A kanban worker runs as ``hermes chat -q``, where Hermes refuses any command
Tirith rates ``block`` or ``warn``, and the refusal is final. A cron run
refuses the same two verdicts through
``deploy/docker/patches/cron_tirith_scan.py``. A command a skill teaches in a
refused form therefore fails every time an agent copies it, and nothing short
of a live run would say so.

This reads every ``bash``, ``sh``, ``shell`` and ``zsh`` fenced block in each
``SKILL.md`` under the skill trees named on the command line, replaces each
``<placeholder>`` with its bare name, and passes the block whole to Hermes' own
``tools.tirith_security.check_command_security``, the call the approval gate
makes on a terminal command of any length. The block is not split into
commands: Tirith parses the shell itself, heredocs and quoting included, and a
hand-written splitter would only approximate it. Tirith has a fixed work budget,
so a block too long for it is refused as ``analysis_incomplete`` whatever it
holds, as a terminal command that long would be; the report prints Tirith's
finding titles so that reads as "split the block". Unlabelled blocks and inline
code are not read: in the skills they hold tool calls, report templates and
program output as often as commands. ``tests/test_skill_inline_commands.py``
checks inline code for the forms the skills used to teach: a variable as the
program, a script path starting with a variable, and a program's path put in
a variable. The Dockerfile names the agent image's three trees; a plugin's
skills ship in its own image and are not read.
A tree with no shell block fails the check, and so do two trees where one path
is, or holds, the other: an empty copy, or a path repeated from another tree in
the Dockerfile, would otherwise pass a tree with nothing read.

Tirith is not in the image, so ``main`` downloads the release ``TIRITH_VERSION``
names into a temporary directory, checks the archive against the digest pinned
here, and points Hermes at that binary. A pod instead installs Tirith's latest
release on first use, so the pin can lag the runtime: a rule a newer release
adds is refused in production before this check sees it. Raising the pin is a
change of its own, with the digests copied from that release's
``checksums.txt``; pinning is what keeps a Tirith release from failing pull
requests that did not touch a skill. Two probe commands with a known verdict
run before and after the scan, and fail-open is off, so a binary that does not
run fails the build rather than passing every block.

A finding that cannot be fixed in the skill text yet goes in
``KNOWN_FINDINGS`` with the rules Tirith cites and its reason, keyed on the
whole block, so an edit anywhere in that block, or a rule a Tirith release adds
to it, brings it back for review. An entry under a tree the run reads that no
longer matches a refused block fails the check too, so the list cannot outlive
what it excuses.

A shell block whose raw text and parsed Markdown disagree, such as a fence left
unclosed inside a list item, fails the check as well: its commands cannot be
read, and ``KNOWN_FINDINGS`` cannot excuse it. The fix is in the Markdown.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import os
import platform
import re
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

SKILL_FILE_NAME = "SKILL.md"
SHELL_LANGUAGES = frozenset({"bash", "sh", "shell", "zsh"})
REFUSED_ACTIONS = frozenset({"block", "warn"})

TIRITH_VERSION = "v0.4.2"
TIRITH_ARCHIVE_URL = (
    "https://github.com/sheeki03/tirith/releases/download/{version}/tirith-{target}.tar.gz"
)
TIRITH_ARCHIVE_SHA256 = {
    "x86_64-unknown-linux-gnu": "efa6bf414a83dba385d4f13137e8677f850ced9102fe74ebb14c72f31df0dc77",
    "aarch64-unknown-linux-gnu": "c550b1bfb0c8c872ab3421cd6ef756f260f7cf4981a18cedd49f141fa2d77569",
}
TIRITH_TARGETS = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "amd64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
    "arm64": "aarch64-unknown-linux-gnu",
}
TIRITH_SYSTEM = "Linux"
TIRITH_BINARY_NAME = "tirith"
TIRITH_BINARY_MODE = 0o755
DOWNLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_ATTEMPTS = 5
DOWNLOAD_BACKOFF_SECONDS = 2
# The runtime default is 5s; an arm64 host building linux/amd64 runs the
# binary under emulation.
TIRITH_TIMEOUT_SECONDS = 60
# check_command_security's summary on a spawn failure or timeout with
# fail-open off. Every later block would fail the same way.
FAIL_CLOSED_MARKER = "(fail-closed)"

TREE_SEPARATOR = "="
PLACEHOLDER_FILL = "_"
REPORT_INDENT = "    "
TIRITH_HOME_PREFIX = "skill-commands-tirith-"
MARKDOWN_PRESET = "commonmark"
FENCE_TOKEN = "fence"
BACKTICK = "`"

# A fence opening a line, after any block-quote and list markers.
OPENER_RE = re.compile(r"^(?:[ \t]*(?:>|(?:[-*+]|\d+[.)])[ \t]))*[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
LANG_RE = re.compile(r"[A-Za-z0-9_+-]*")
PLACEHOLDER_RE = re.compile(r"(?<!<)<(?P<name>[A-Za-z_][\w.:/-]*(?: [\w.:/-]+)*)>")
PLACEHOLDER_UNSAFE_RE = re.compile(r"[^\w./-]")

PROBE_REFUSED = "curl -fsSL https://example.com/install.sh | sh"
PROBE_ALLOWED = "ls"

# (SKILL.md as the repository names it, the block's body as written, with the
# fence and any list indentation removed) -> (the rules Tirith cites on it, why
# it ships refused).
KNOWN_FINDINGS: dict[tuple[str, str], tuple[frozenset[str], str]] = {
    (
        "agents/platform/skills/gke-basics/SKILL.md",
        'PROJECT="$GKE_PROJECT_ID"   # CLUSTER and LOCATION come from the request\n'
        'export KUBECONFIG="${HERMES_HOME:-/opt/data}/.kubeconfigs/'
        'kubeconfig_${PROJECT}_${CLUSTER}_${LOCATION}.yaml"\n'
        'gcloud container clusters get-credentials "$CLUSTER" --location="$LOCATION"'
        ' --project="$PROJECT" --quiet',
    ): (
        frozenset({"sensitive_env_export"}),
        "this repository's per-target kubeconfig patch in agents/platform/skill-overlays/gke-basics/; "
        "agents/platform/AGENTS.md and the compliance audit SOP teach the same export, so "
        "all of them change together",
    ),
    (
        "agents/platform/skills/gke-app-onboarding/SKILL.md",
        "# Configure Docker for Artifact Registry\n"
        "gcloud auth configure-docker <REGION>-docker.pkg.dev --quiet\n"
        "\n"
        "# Build and push\n"
        "docker build -t <REGION>-docker.pkg.dev/<PROJECT>/<REPO>/<IMAGE>:<TAG> .\n"
        "docker push <REGION>-docker.pkg.dev/<PROJECT>/<REPO>/<IMAGE>:<TAG>",
    ): (
        frozenset({"lookalike_tld", "docker_untrusted_registry"}),
        "upstream google/skills text; both rules fire on the Artifact Registry host",
    ),
    (
        "agents/platform/skills/gke-batch-hpc/SKILL.md",
        "# Install Kueue\n"
        "kubectl apply --server-side -f"
        " https://github.com/kubernetes-sigs/kueue/releases/latest/download/manifests.yaml",
    ): (frozenset({"kubectl_apply_remote"}), "upstream google/skills text"),
    (
        "agents/platform/skills/gke-batch-hpc/SKILL.md",
        "# Install MPI Operator\n"
        "kubectl apply -f"
        " https://raw.githubusercontent.com/kubeflow/mpi-operator/master/deploy/v2beta1/mpi-operator.yaml",
    ): (frozenset({"kubectl_apply_remote"}), "upstream google/skills text"),
    (
        "agents/platform/skills/gke-workload-identity/SKILL.md",
        "# Token endpoint via the hardcoded metadata IP (a healthy path returns a token).\n"
        "kubectl exec {pod_name} -n {namespace} -- \\\n"
        "  curl -sS -H 'Metadata-Flavor: Google' \\\n"
        "  'http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token'",
    ): (
        frozenset({"raw_ip_url"}),
        "upstream google/skills text; the block tests the metadata IP path apart from DNS, "
        "so the raw IP is what it checks",
    ),
}

Scan = Callable[[str], dict]


class ScannerUnavailable(RuntimeError):
    """Tirith did not give a real verdict, so no result can be trusted."""


class UnreadableFence(ValueError):
    """A shell block that reads differently as raw text than as parsed Markdown."""

    def __init__(self, line: int, reason: str, path: str = ""):
        super().__init__(f"{path}:{line}: {reason}")
        self.line = line
        self.reason = reason
        self.path = path


@dataclass(frozen=True)
class Block:
    path: str
    line: int
    text: str

    @property
    def key(self) -> tuple[str, str]:
        return self.path, self.text

    @property
    def scanned(self) -> str:
        return substitute_placeholders(self.text)


@dataclass(frozen=True)
class Finding:
    block: Block
    action: str
    rules: tuple[str, ...]
    titles: tuple[str, ...] = ()


def code_blocks(text: str) -> Iterator[tuple[int, str, list[str]]]:
    """Yield ``(line of the first body line, language, body lines)`` per fenced block.

    A CommonMark parser finds the blocks, so a fence in a list item or a block
    quote reads as it renders, with its container's indentation and ``>``
    markers removed from the body. An agent reads the raw file instead, so a
    shell block the two read differently raises ``UnreadableFence`` rather than
    going unscanned: a line that opens one where the parser finds no fence, or
    one the parser ends before its own closing fence.
    """
    # The trailing newline ends the last content line of an unclosed fence
    # too, so every body line is counted.
    fences = [t for t in MarkdownIt(MARKDOWN_PRESET).parse(text + "\n") if t.type == FENCE_TOKEN]
    starts = {token.map[0] for token in fences}
    problems = [
        (index + 1, "this line opens a shell block, but Markdown parses no code block here")
        for index, line in enumerate(text.split("\n"))
        if (fence := _shell_opener(line)) and index not in starts
        and not _nested(fence, index, fences)
    ]
    blocks = []
    for token in fences:
        lang = _language(token.info)
        opening_line = token.map[0] + 1
        body = token.content.split("\n")[:-1]
        if lang in SHELL_LANGUAGES and not _closed(token):
            problems.append((opening_line, "this shell block ends before a closing fence of its own"))
        blocks.append((opening_line + 1, lang, body))
    if problems:
        raise UnreadableFence(*min(problems))
    yield from blocks


def _language(info: str) -> str:
    return LANG_RE.match(info.strip()).group().lower()


def _shell_opener(line: str) -> str | None:
    """The fence that opens ``line`` if a reader would take it for a shell block."""
    match = OPENER_RE.match(line)
    if not match:
        return None
    fence, info = match.group("fence", "info")
    # CommonMark: a backtick fence's info string holds no backtick, so
    # ```ls``` on one line is inline code.
    if fence.startswith(BACKTICK) and BACKTICK in info:
        return None
    return fence if _language(info) in SHELL_LANGUAGES else None


def _closed(token) -> bool:
    """Whether the parser ended a fence at a closing fence of its own."""
    return token.map[0] + token.content.count("\n") + 1 < token.map[1]


def _nested(fence: str, index: int, fences: list) -> bool:
    """Whether line ``index`` is in the body of a closed block ``fence`` could not close.

    That is a deliberate example, as in a longer or tilde fence around one.
    """
    return any(
        t.map[0] < index < t.map[1]
        and _closed(t)
        and (t.markup[0] != fence[0] or len(t.markup) > len(fence))
        for t in fences
    )


def substitute_placeholders(command: str) -> str:
    """Replace each ``<placeholder>`` with its name, as an agent fills in a value.

    Left in, the angle brackets parse as redirections, and Tirith would rate a
    command no agent runs.
    """
    return PLACEHOLDER_RE.sub(
        lambda match: PLACEHOLDER_UNSAFE_RE.sub(PLACEHOLDER_FILL, match.group("name")),
        command,
    )


def skill_blocks(repo_dir: str, skills_dir: Path) -> list[Block]:
    """Every shell block in the ``SKILL.md`` files under ``skills_dir``.

    ``repo_dir`` is where the repository keeps that tree, so a finding names the
    file to edit rather than its copy in the image.
    """
    shell_blocks = []
    for skill_file in sorted(skills_dir.rglob(SKILL_FILE_NAME)):
        path = f"{repo_dir}/{skill_file.relative_to(skills_dir).as_posix()}"
        try:
            blocks = list(code_blocks(skill_file.read_text(encoding="utf-8")))
        except UnreadableFence as exc:
            raise UnreadableFence(exc.line, exc.reason, path) from None
        shell_blocks.extend(
            Block(path, first_line, "\n".join(body))
            for first_line, lang, body in blocks
            if lang in SHELL_LANGUAGES
        )
    return shell_blocks


def check_scanner(scan: Scan) -> None:
    refused = scan(PROBE_REFUSED)
    allowed = scan(PROBE_ALLOWED)
    if refused.get("action") != "block" or allowed.get("action") != "allow":
        raise ScannerUnavailable(
            f"probe verdicts were {refused.get('action')!r} ({refused.get('summary')!r}) for "
            f"{PROBE_REFUSED!r} and {allowed.get('action')!r} ({allowed.get('summary')!r}) for "
            f"{PROBE_ALLOWED!r}; expected 'block' and 'allow'"
        )


def scan_blocks(blocks: Iterable[Block], scan: Scan) -> list[Finding]:
    findings = []
    for block in blocks:
        verdict = scan(block.scanned)
        summary = verdict.get("summary") or ""
        if FAIL_CLOSED_MARKER in summary:
            raise ScannerUnavailable(f"{summary}, scanning {block.path}:{block.line}")
        if verdict.get("action") in REFUSED_ACTIONS:
            cited = [f for f in verdict.get("findings") or [] if isinstance(f, dict)]
            # Tirith can cite one rule several times in a verdict.
            rules = tuple(dict.fromkeys(str(f.get("rule_id", "?")) for f in cited))
            titles = tuple(dict.fromkeys(str(f["title"]) for f in cited if f.get("title")))
            findings.append(Finding(block, verdict["action"], rules, titles))
    return findings


def triage(
    findings: Iterable[Finding],
    known: dict[tuple[str, str], tuple[frozenset[str], str]],
    repo_dirs: Iterable[str],
) -> tuple[list[Finding], list[tuple[str, str]]]:
    """Split into findings ``known`` does not excuse and entries that excuse nothing.

    An entry excuses its block only while Tirith cites exactly the rules it
    lists: the key is the whole block, so a new rule on another of its lines
    would otherwise pass unseen. Only an entry under one of ``repo_dirs`` can be
    stale, since a run that did not read a tree cannot say its blocks pass.
    """
    findings = list(findings)
    refused = {finding.block.key for finding in findings}
    new = [
        finding for finding in findings
        if finding.block.key not in known or set(finding.rules) != known[finding.block.key][0]
    ]
    read = tuple(f"{repo_dir}/" for repo_dir in repo_dirs)
    stale = sorted(key for key in known if key[0].startswith(read) and key not in refused)
    return new, stale


def tirith_target(system: str, machine: str) -> str:
    target = TIRITH_TARGETS.get(machine.lower()) if system == TIRITH_SYSTEM else None
    if target is None:
        raise ScannerUnavailable(
            f"no pinned Tirith build for {system} {machine}; pass --tirith-bin"
        )
    return target


def install_tirith(directory: Path, target: str) -> Path:
    """Download the pinned Tirith release for ``target`` into ``directory``."""
    url = TIRITH_ARCHIVE_URL.format(version=TIRITH_VERSION, target=target)
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                archive = response.read()
            break
        except (OSError, http.client.HTTPException) as exc:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise ScannerUnavailable(f"downloading {url}: {exc}") from exc
            time.sleep(DOWNLOAD_BACKOFF_SECONDS * attempt)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != TIRITH_ARCHIVE_SHA256[target]:
        raise ScannerUnavailable(
            f"{url} has sha256 {digest}; the pin is {TIRITH_ARCHIVE_SHA256[target]}"
        )
    binary = directory / TIRITH_BINARY_NAME
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            member = tar.extractfile(TIRITH_BINARY_NAME)
            if member is None:
                raise KeyError(TIRITH_BINARY_NAME)
            binary.write_bytes(member.read())
    except (tarfile.TarError, KeyError) as exc:
        raise ScannerUnavailable(f"{url} holds no {TIRITH_BINARY_NAME!r}: {exc}") from exc
    binary.chmod(TIRITH_BINARY_MODE)
    return binary


def _tree(value: str) -> tuple[str, Path]:
    repo_dir, separator, skills_dir = value.partition(TREE_SEPARATOR)
    if not separator or not repo_dir or not skills_dir:
        raise argparse.ArgumentTypeError(f"expected REPO_DIR=SKILLS_DIR, got {value!r}")
    if not Path(skills_dir).is_dir():
        raise argparse.ArgumentTypeError(f"{skills_dir} is not a directory")
    return repo_dir.rstrip("/"), Path(skills_dir)


def _overlap(first: Path, second: Path) -> bool:
    first, second = first.resolve(), second.resolve()
    return first == second or first in second.parents or second in first.parents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "trees",
        nargs="+",
        type=_tree,
        metavar="REPO_DIR=SKILLS_DIR",
        help="a skill tree in the image, and where the repository keeps it",
    )
    parser.add_argument(
        "--tirith-bin",
        type=Path,
        help=f"a Tirith binary to use instead of downloading {TIRITH_VERSION}",
    )
    args = parser.parse_args(argv)
    overlapping = [
        (first, second)
        for index, first in enumerate(args.trees)
        for second in args.trees[index + 1 :]
        if _overlap(first[1], second[1])
    ]
    for (first_repo, first_dir), (second_repo, second_dir) in overlapping:
        print(
            f"{first_dir} ({first_repo}) and {second_dir} ({second_repo}) overlap, so one "
            "tree would be read twice and the other not at all. Check each tree's path in "
            "the Dockerfile.",
            file=sys.stderr,
        )
    if overlapping:
        return 1
    try:
        trees = [
            (repo_dir, skills_dir, skill_blocks(repo_dir, skills_dir))
            for repo_dir, skills_dir in args.trees
        ]
    except UnreadableFence as exc:
        print(
            f"{exc}. An agent reads this block as shell commands, but the check cannot read "
            "it. Close each fence at the indentation it opened with, indent a heredoc body "
            "with its block, and leave a blank line between an HTML tag and a fence.",
            file=sys.stderr,
        )
        return 1
    empty = [(repo_dir, skills_dir) for repo_dir, skills_dir, found in trees if not found]
    for repo_dir, skills_dir in empty:
        print(
            f"{skills_dir} holds no shell block in any {SKILL_FILE_NAME}, so {repo_dir} "
            "would pass unread. Check the Dockerfile copies that tree to this path, or, if "
            "its skills no longer teach shell commands, drop the tree from the invocation.",
            file=sys.stderr,
        )
    if empty:
        return 1
    blocks = [block for _, _, found in trees for block in found]

    with tempfile.TemporaryDirectory(prefix=TIRITH_HOME_PREFIX) as home:
        try:
            binary = args.tirith_bin or install_tirith(
                Path(home), tirith_target(platform.system(), platform.machine())
            )
            # An explicit TIRITH_BIN is never replaced by Hermes' own download
            # of the latest release.
            os.environ.update(
                HERMES_HOME=home,
                TIRITH_BIN=str(binary.resolve()),
                TIRITH_ENABLED="true",
                TIRITH_FAIL_OPEN="false",
                TIRITH_TIMEOUT=str(TIRITH_TIMEOUT_SECONDS),
            )
            from tools.tirith_security import check_command_security

            check_scanner(check_command_security)
            findings = scan_blocks(blocks, check_command_security)
            check_scanner(check_command_security)
        except ScannerUnavailable as exc:
            print(f"SKILL COMMAND CHECK COULD NOT RUN: {exc}", file=sys.stderr)
            return 1

    new, stale = triage(findings, KNOWN_FINDINGS, [repo_dir for repo_dir, _ in args.trees])
    for finding in new:
        block = finding.block
        print(
            f"{block.path}:{block.line}: Tirith rates this block {finding.action} "
            f"[{', '.join(finding.rules)}] ({'; '.join(finding.titles)}):\n"
            f"{textwrap.indent(block.text, REPORT_INDENT)}",
            file=sys.stderr,
        )
    if new:
        print(
            "Hermes refuses a command in each of these blocks in kanban workers and cron runs, "
            "so an agent that copies it from the skill is refused every time. Rewrite the "
            "command each block's rules point at (call a program by its path, not through a "
            "shell variable), or add the block and its rules to KNOWN_FINDINGS in "
            "deploy/docker/check_skill_commands.py with the reason it has to ship. A title "
            "saying the analysis exceeded its work budget means the block is too long for "
            "Tirith to finish, whatever it holds: split it into shorter blocks.",
            file=sys.stderr,
        )
    for path, text in stale:
        print(
            f"KNOWN_FINDINGS entry matches no refused block; remove it: ({path!r}, {text!r})",
            file=sys.stderr,
        )
    if new or stale:
        return 1
    print(
        f"{len(blocks)} skill shell blocks pass Tirith "
        f"({len(findings)} refused, all in KNOWN_FINDINGS)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""What an image ships under the helper-script root is root-owned and not group- or world-writable (#5622).

A profile's ``spec.scripts`` names helper scripts by path under one fixed
root in the worker image, ``/opt/kube-agents/scripts/``
(``AgentProfileScriptRoot`` in ``k8s-operator/api/v1alpha1/agentprofile_types.go``).
The worker's script mode refuses, at exec time, a file there that isn't owned
by root or is writable by anyone else. That holds only while the image puts
the files there that way, so this reads every Dockerfile in the tree and
refuses:

- a ``COPY`` or ``ADD`` into the root with a ``--chown`` to anyone but root;
- a ``COPY`` or ``ADD`` into the root without a ``--chmod``, since without one
  the mode is the build context's, which a checkout can leave group-writable;
- a ``--chmod`` with a group or world write bit, or a setuid, setgid or
  sticky bit;
- a ``COPY`` or ``ADD`` whose destination holds a ``$``, which this can't
  resolve;
- a ``RUN`` that changes an owner or a mode (``chown``, ``chmod``, ``chgrp``,
  ``install``, ``setfacl``) and names the root, a path under it, or one of
  its parents (``chown -R 1000 /opt/kube-agents`` reaches it), which this
  can't read the outcome of;
- any ``RUN`` while the stage's ``USER`` isn't root that names the root or a
  path under it, or runs with its ``WORKDIR`` there: what it writes is owned
  by that user (``USER node`` then ``RUN cp x.py /opt/kube-agents/scripts/``).
  ``COPY`` and ``ADD`` aren't affected: they create files owned by root
  whatever the ``USER``, unless a ``--chown`` names someone else, which the
  first rule refuses.

A relative destination, and a relative path a ``RUN`` names before any
``cd``, is read against the stage's ``WORKDIR``; ``FROM`` resets a stage's
``WORKDIR`` to ``/`` and its ``USER`` to root; ``//``, ``.`` and ``..`` are
resolved; the keyword may be followed by a tab; ``install`` counts as a
command by name or by path, after ``RUN`` flags, but not as an argument
(``apt-get install``); and an ``ONBUILD`` instruction is read as the one it
wraps.

It reads instructions, not built images. Not seen: a file reaching the root
another way (a ``COPY`` of a parent directory, a ``RUN`` that writes there
without naming it, a ``WORKDIR`` or ``RUN`` path built from a variable, a
relative path after a ``cd`` in the same ``RUN``), and the body of a ``RUN``
heredoc. The worker's exec-time check is the control; this keeps a
Dockerfile change from quietly breaking what that check expects.

Run: python3 deploy/docker/test_script_root_ownership.py
"""

from __future__ import annotations

import posixpath
import re
import shlex
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT_ROOT = "/opt/kube-agents/scripts"
ROOT_OWNERS = {"root", "0", "root:root", "0:0", "root:0", "0:root"}
# A RUN that names the root and does one of these is refused: an owner or a
# mode set there is one this can't check.
# install only as a command (apt-get install and pip install aren't).
RUN_MODE_CHANGE = re.compile(r"\b(chown|chmod|chgrp|setfacl)\b")
# The commands whose relative arguments are read against WORKDIR.
MODE_COMMANDS = {"chown", "chmod", "chgrp", "setfacl", "install"}
# A path a RUN names: from a slash to the next space, quote or shell operator.
RUN_PATH = re.compile(r"/[^\s;&|'\"()<>]*")
SKIP_DIRS = {".git", "node_modules", "vendor"}


def instructions(text: str) -> list[tuple[str, str]]:
    """The Dockerfile's instructions as (keyword, arguments), continuation lines joined and comments dropped."""
    out, current = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if current and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        keyword, args = _split_keyword(current)
        out.append((keyword.upper(), args.strip()))
        current = ""
    if current.strip():
        keyword, args = _split_keyword(current)
        out.append((keyword.upper(), args.strip()))
    return out


def _split_keyword(line: str) -> tuple[str, str]:
    """An instruction's keyword, upper-cased, and its arguments, split at the first whitespace (a tab too)."""
    parts = line.strip().split(None, 1)
    return (parts[0].upper() if parts else ""), (parts[1].strip() if len(parts) > 1 else "")


def _run_changes_modes(args: str) -> bool:
    """Whether a RUN changes an owner or a mode: chown, chmod, chgrp or setfacl anywhere, or install as a
    command (after a RUN flag, an environment assignment or sudo, by name or by path), never as an argument
    (apt-get install, pip install)."""
    if RUN_MODE_CHANGE.search(args):
        return True
    for segment in re.split(r"[;&|()]+", args):
        words = segment.split()
        while words and (words[0].startswith("--") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] == "sudo"):
            words = words[1:]
        if words and posixpath.basename(words[0]) == "install":
            return True
    return False


def _norm(path: str) -> str:
    """path with repeated slashes, `.` and `..` segments resolved, and no trailing slash."""
    return posixpath.normpath(re.sub(r"/+", "/", path)) if path.startswith("/") else path


def _under_root(dest: str) -> bool:
    dest = _norm(dest)
    return dest == SCRIPT_ROOT or dest.startswith(SCRIPT_ROOT + "/")


def _reaches_root(path: str) -> bool:
    """Whether a change to *path* can reach the root: it is the root, under it, or one of its parents."""
    path = _norm(path)
    return _under_root(path) or path == "/" or SCRIPT_ROOT.startswith(path + "/")


def _is_root_user(user: str) -> bool:
    """Whether a USER instruction's argument (``node``, ``1000:1000``, ``root:root``) runs as root."""
    return user.strip("\"'").split(":", 1)[0] in {"root", "0"}


def problems(text: str) -> list[str]:
    """Each instruction in *text* that could leave a file under the root not root-owned, or writable by others."""
    found = []
    workdir = "/"
    user = "root"
    for keyword, args in instructions(text):
        if keyword == "ONBUILD":
            keyword, args = _split_keyword(args)
        if keyword == "FROM":
            workdir = "/"
            user = "root"
        elif keyword == "USER":
            user = args.split()[0] if args.split() else "root"
        elif keyword == "WORKDIR":
            workdir = posixpath.join(workdir, args.strip("\"'"))
        elif keyword in ("COPY", "ADD"):
            if re.sub(r"^(--\S+\s+)*", "", args).startswith("["):
                found.append(f"{keyword} {args}: the JSON form isn't read here; use the shell form")
                continue
            words = shlex.split(args)
            flags = {}
            paths = []
            for word in words:
                if word.startswith("--"):
                    name, _, value = word[2:].partition("=")
                    flags[name] = value
                else:
                    paths.append(word)
            if not paths:
                continue
            if "$" in paths[-1]:
                found.append(f"{keyword} {args}: a destination with a variable, which this can't resolve")
                continue
            if not _under_root(posixpath.join(workdir, paths[-1])):
                continue
            chown = flags.get("chown")
            if chown is not None and chown not in ROOT_OWNERS:
                found.append(f"{keyword} {args}: --chown={chown} into {SCRIPT_ROOT}; only root may own a helper script")
            chmod = flags.get("chmod")
            if chmod is None:
                found.append(f"{keyword} {args}: no --chmod, so the mode is the build context's")
            elif not re.fullmatch(r"0?[0-7]{3,4}", chmod):
                found.append(f"{keyword} {args}: --chmod={chmod} isn't an octal mode this can check")
            elif int(chmod, 8) & 0o7022:
                found.append(f"{keyword} {args}: --chmod={chmod} is group- or world-writable, or sets a setuid, setgid or sticky bit")
        elif keyword == "RUN" and _run_changes_modes(args) and _run_reaches_root(args, workdir):
            found.append(f"RUN {args[:120]}: sets an owner or a mode that can reach {SCRIPT_ROOT}, which this can't check")
        elif keyword == "RUN" and not _is_root_user(user) and (
            _under_root(workdir) or any(_under_root(p) for p in RUN_PATH.findall(args))
        ):
            found.append(f"RUN {args[:120]}: runs as USER {user} and writes into {SCRIPT_ROOT}, so what it writes is owned by {user}")
    return found


def _run_reaches_root(args: str, workdir: str) -> bool:
    """Whether a RUN names the root, a path under it or a parent of it: an absolute path, or a relative one
    (`.`, `scripts/a.py`) read against the stage's WORKDIR."""
    if any(_reaches_root(p) for p in RUN_PATH.findall(args)):
        return True
    # A relative path is read against WORKDIR only as an argument of a
    # command that changes an owner or a mode (`chown -R 1000 .`), and only
    # until the shell changes directory: after a `cd` it is relative to a
    # directory this can't know (that part isn't seen).
    for segment in re.split(r"[;&|()]+", args):
        words = segment.split()
        while words and (words[0].startswith("--") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] == "sudo"):
            words = words[1:]
        if not words:
            continue
        if words[0] == "cd":
            break
        if posixpath.basename(words[0]) not in MODE_COMMANDS:
            continue
        for word in words[1:]:
            word = word.strip("\"'")
            if not word or word.startswith(("-", "/", "$")) or "=" in word:
                continue
            if _reaches_root(posixpath.join(workdir, word)):
                return True
    return False


def dockerfiles() -> list[Path]:
    files = []
    for path in sorted(REPO.rglob("*Dockerfile*")):
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(REPO).parts):
            files.append(path)
    return files


class ScriptRootOwnershipTest(unittest.TestCase):
    def test_no_dockerfile_breaks_the_script_roots_ownership(self):
        files = dockerfiles()
        self.assertTrue(any(p.name == "Dockerfile.worker" for p in files), "found no worker Dockerfile; the glob is stale")
        for path in files:
            with self.subTest(dockerfile=str(path.relative_to(REPO))):
                self.assertEqual(problems(path.read_text(encoding="utf-8", errors="replace")), [])

    def test_the_root_is_the_operators(self):
        types = (REPO / "k8s-operator/api/v1alpha1/agentprofile_types.go").read_text(encoding="utf-8")
        self.assertIn(f'AgentProfileScriptRoot = "{SCRIPT_ROOT}/"', types)

    def test_a_root_owned_read_only_copy_passes(self):
        for line in (
            f"COPY --chmod=0555 helpers/stall_watch.py {SCRIPT_ROOT}/stall_watch.py",
            f"COPY --chmod=755 --chown=root:root helpers/ {SCRIPT_ROOT}/",
            f"ADD --chmod=0444 a.py {SCRIPT_ROOT}/a.py",
            "COPY --chown=1000:1000 app.py /opt/app/app.py",  # not under the root
            f"RUN mkdir -p {SCRIPT_ROOT}",
            "RUN chmod 0755 /opt/kube-agents/scriptsx/a.py",  # the root's sibling
            "RUN chmod -R 0755 /opt/tools",  # a sibling of a parent
            f"WORKDIR {SCRIPT_ROOT}\nFROM scratch\nCOPY a.py a.py",  # FROM resets the stage's WORKDIR
            f"WORKDIR /opt\nWORKDIR kube-agents/scripts\nCOPY --chmod=0555 a.py a.py",
            "RUN apt-get install -y curl && pip install x",  # install as an argument
            "WORKDIR /opt/kube-agents\nRUN chown root:root /opt/other && find /opt/other | grep .",  # . is grep's
            f"WORKDIR {SCRIPT_ROOT}\nRUN cd /tmp && find . -type f",  # no mode change
            "USER node\nRUN mkdir -p /home/node/work",  # not the root
            f"USER node\nCOPY --chmod=0555 a.py {SCRIPT_ROOT}/a.py",  # COPY is root-owned whatever the USER
            f"USER node\nFROM scratch\nRUN mkdir -p {SCRIPT_ROOT}",  # FROM resets the stage's USER
            f"USER node\nUSER root\nRUN mkdir -p {SCRIPT_ROOT}",
            f"USER 0\nRUN mkdir -p {SCRIPT_ROOT}",
            f"USER root:root\nRUN cp /tmp/x.py {SCRIPT_ROOT}/",
        ):
            with self.subTest(line=line):
                self.assertEqual(problems(f"FROM scratch\n{line}\n"), [])

    def test_each_way_to_break_it_is_refused(self):
        for line in (
            f"COPY --chown=1000 a.py {SCRIPT_ROOT}/a.py",
            f"COPY --chown=worker:worker --chmod=0555 a.py {SCRIPT_ROOT}/a.py",
            f"COPY a.py {SCRIPT_ROOT}/a.py",
            f"COPY a.py {SCRIPT_ROOT}/",
            f"COPY a.py {SCRIPT_ROOT}",
            f"COPY --chmod=0775 a.py {SCRIPT_ROOT}/a.py",
            f"COPY --chmod=0757 a.py {SCRIPT_ROOT}/a.py",
            f"COPY --chmod=u+x a.py {SCRIPT_ROOT}/a.py",
            f'COPY ["a.py", "{SCRIPT_ROOT}/a.py"]',
            f"ADD --chmod=0666 a.py {SCRIPT_ROOT}/a.py",
            f"RUN chown 1000 {SCRIPT_ROOT}/a.py",
            f"RUN chmod g+w {SCRIPT_ROOT}/a.py",
            f"RUN install -m 0777 a.py {SCRIPT_ROOT}/a.py",
            f"RUN install -o worker a.py {SCRIPT_ROOT}/a.py",
            f"RUN true && \\\n    chown -R 1000 {SCRIPT_ROOT}",
            f"RUN install -Dm777 a.py {SCRIPT_ROOT}/a.py",
            f"RUN install -m0777 a.py {SCRIPT_ROOT}/a.py",
            f"RUN install --mode=0777 a.py {SCRIPT_ROOT}/a.py",
            f"RUN install --owner=1000 a.py {SCRIPT_ROOT}/a.py",
            "RUN chown -R 1000 /opt/kube-agents",
            "RUN chmod -R a+w /opt/kube-agents/",
            "RUN chown -R 1000 /opt",
            "RUN chmod -R 777 /",
            "RUN chmod -R a+w /opt/kube-agents//scripts",
            f"WORKDIR {SCRIPT_ROOT}\nCOPY --chown=1000 a.py .",
            "WORKDIR /opt/kube-agents\nCOPY a.py scripts/a.py",
            f"ARG D={SCRIPT_ROOT}\nCOPY --chmod=0555 a.py ${{D}}/a.py",
            "COPY a.py /opt/kube-agents//scripts/a.py",
            f"COPY --chmod=4755 a.py {SCRIPT_ROOT}/a.py",
            f"ONBUILD COPY --chown=1000 --chmod=0555 a.py {SCRIPT_ROOT}/a.py",
            f"COPY --chmod=0555 \\\n\n    --chown=1000 a.py {SCRIPT_ROOT}/a.py",
            f'COPY --chown=1000 ["a.py", "{SCRIPT_ROOT}"]',
            f'COPY --chown=1000 ["a.py","{SCRIPT_ROOT}/a.py"]',
            f"COPY\t--chown=1000 --chmod=0555 a.py {SCRIPT_ROOT}/a.py",
            f"RUN --mount=type=bind,target=/y install -m 0777 a.py {SCRIPT_ROOT}/a.py",
            f"RUN /usr/bin/install -m 0777 a.py {SCRIPT_ROOT}/a.py",
            "WORKDIR /opt/kube-agents\nCOPY a.py ./scripts/a.py",
            "COPY a.py /opt/kube-agents/other/../scripts/a.py",
            "RUN chown 1000 /opt/kube-agents/./scripts/a.py",
            f"WORKDIR {SCRIPT_ROOT}\nRUN chmod 0777 a.py",
            "WORKDIR /opt/kube-agents\nRUN chown -R 1000 .",
            "WORKDIR /opt/kube-agents\nRUN chown -R 1000 scripts/a.py",
            f"USER node\nRUN mkdir -p {SCRIPT_ROOT} && cp /tmp/x.py {SCRIPT_ROOT}/",  # #6297's case
            f"USER 1000:1000\nRUN cp /tmp/x.py {SCRIPT_ROOT}/x.py",
            f"USER node:node\nRUN tee {SCRIPT_ROOT}/a.py < /tmp/a.py",
            f"USER node\nWORKDIR {SCRIPT_ROOT}\nRUN touch a.py",  # a write into WORKDIR
            f"USER \"node\"\nRUN cp /tmp/x.py {SCRIPT_ROOT}/",
            f"FROM scratch AS base\nUSER node\nRUN cp /tmp/x.py {SCRIPT_ROOT}/",
        ):
            with self.subTest(line=line):
                self.assertNotEqual(problems(f"FROM scratch\n{line}\n"), [])

    def test_a_continued_instruction_is_read_whole(self):
        text = f"FROM scratch\nCOPY --chmod=0555 \\\n    # a comment inside\n    a.py \\\n    {SCRIPT_ROOT}/a.py\n"
        keyword, args = instructions(text)[-1]
        self.assertEqual((keyword, args.split()), ("COPY", ["--chmod=0555", "a.py", f"{SCRIPT_ROOT}/a.py"]))
        self.assertEqual(problems(text), [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Stands in for a script that cannot run in the shell sandbox.

The sandbox holds the agent's shell, and some of what a SKILL.md tells the model
to run is not a shell command in any useful sense: it needs the `hermes` binary,
the profiles tree on the agent pod's PVC, or Hermes' own Python namespace. None
of those crossed the boundary and none of them should — the point of #737 is
that code the model runs cannot reach them.

Leaving the file out entirely was the other option. It reads worse: the model
gets `No such file or directory`, concludes the image is broken or the path is
wrong, and spends a turn or two proving it. This says what is actually true and
exits non-zero, so the failure carries its own explanation.

For cluster_agent_profile.py, read-only discovery (`list` and `name`) is served
in the sandbox from the mirrored profile roster (/opt/data/profiles/), while
profile mutation (`create` and `delete`) remains agent-pod-only.

Copied to each such path by deploy/sandbox/Dockerfile, which is also where the
list of them lives.
"""

import os
import sys
from pathlib import Path

NAME = os.path.basename(sys.argv[0])

if NAME == "cluster_agent_profile.py":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        # In the sandbox, the agent pod PVC is not mounted, but sandbox_mirror
        # pushes the profile skeleton and each cluster profile's identity file (USER.md)
        # into /opt/data/profiles. Enumerate valid, fully scaffolded cluster profiles from there.
        #
        # In the sandbox, session-command.sh narrows HERMES_HOME to the profile home
        # (e.g. /opt/data/profiles/platform) when cwd is inside a profile, but
        # PLATFORM_AGENT_HOME remains the static sandbox data root (/opt/data).
        # Resolve the profiles directory from PLATFORM_AGENT_HOME, falling back
        # to parent traversal if HERMES_HOME points directly into a profile home.
        data_root = Path(os.environ.get("PLATFORM_AGENT_HOME") or "/opt/data")
        profiles_dir = data_root / "profiles"
        if not profiles_dir.is_dir():
            hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
            if (hermes_home / "profiles").is_dir():
                profiles_dir = hermes_home / "profiles"
            elif hermes_home.parent.name == "profiles" and hermes_home.parent.is_dir():
                profiles_dir = hermes_home.parent
            elif (hermes_home.parent / "profiles").is_dir():
                profiles_dir = hermes_home.parent / "profiles"
        names = []
        if profiles_dir.is_dir():
            for p in profiles_dir.iterdir():
                if (
                    p.is_dir()
                    and p.name.startswith("cluster-")
                    and (p / "USER.md").is_file()
                ):
                    names.append(p.name)
        for n in sorted(names):
            print(n)
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "name":
        import argparse
        import hashlib
        import re

        parser = argparse.ArgumentParser()
        parser.add_argument("--project", required=True)
        parser.add_argument("--cluster", required=True)
        parser.add_argument("--location", required=True)
        try:
            args = parser.parse_args(sys.argv[2:])
        except SystemExit as e:
            sys.exit(e.code)
        raw = f"cluster-{args.project}-{args.cluster}-{args.location}".lower()
        cname = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", raw)).strip("-")
        if len(cname) > 63:
            digest = hashlib.sha1(cname.encode("utf-8")).hexdigest()[:8]
            cname = f"{cname[:54]}-{digest}"
        print(cname)
        sys.exit(0)

print(
    f"{NAME} does not run in the shell sandbox.\n"
    "\n"
    "This is the sandbox — the container the agent's terminal, file and\n"
    "code-execution tools run in, reached over SSH from the agent pod. It has\n"
    "no `hermes` binary, no profiles tree, and no access to the agent pod's\n"
    "data volume. Profile creation and teardown happen on the agent pod.\n"
    "\n"
    "To enumerate active cluster profiles in the sandbox, inspect the mirrored\n"
    "roster under /opt/data/profiles/cluster-*/USER.md or run:\n"
    "  python3 /opt/data/scripts/cluster_agent_profile.py list\n"
    "\n"
    "There is no way to create or delete profiles from here, and no argument\n"
    "to this command that changes that. Report the request as blocked on work\n"
    "that has to happen in the agent pod, and say which script it was.\n"
    "\n"
    "/opt/data/.sandbox describes which side of the boundary this is.",
    file=sys.stderr,
)
sys.exit(1)

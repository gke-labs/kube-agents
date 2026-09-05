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

Copied to each such path by deploy/sandbox/Dockerfile, which is also where the
list of them lives.
"""

import os
import sys

NAME = os.path.basename(sys.argv[0])

print(
    f"{NAME} does not run in the shell sandbox.\n"
    "\n"
    "This is the sandbox — the container the agent's terminal, file and\n"
    "code-execution tools run in, reached over SSH from the agent pod. It has\n"
    "no `hermes` binary, no profiles tree, and no access to the agent pod's\n"
    "data volume. The script this file stands in for needs all three.\n"
    "\n"
    "There is no way to run it from here, and no argument to this command that\n"
    "changes that. Report the request as blocked on work that has to happen in\n"
    "the agent pod, and say which script it was.\n"
    "\n"
    "/opt/data/.sandbox describes which side of the boundary this is.",
    file=sys.stderr,
)
sys.exit(1)

#!/usr/bin/env python3
"""Build gate for the unknown-assignee refusal.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_unknown_assignee.py``. The applier only proves the anchor
matched. This replays the #3786 shape through the real patched
``kanban_create`` on a real board, with a throwaway ``HERMES_HOME`` that holds
four folders under ``profiles/``: the Platform Agent, a Cluster Agent whose
scaffold finished, one whose scaffold stopped before ``USER.md``, and a folder
Hermes never registered.

- a card for a name that is no profile is refused, the refusal lists the
  profiles that can take a card, and no card is written;
- a card for the unfinished Cluster Agent profile and one for the unregistered
  folder are refused too;
- a card for the Platform Agent and one for the finished Cluster Agent are
  created, also when the name is written in upper case (``create_task``
  lower-cases it);
- a card for ``default``, the gateway's own profile, is created;
- the same refusal and the same acceptance hold when the process's home is a
  profile's folder, as it is in a worker, which is where #3786 was seen.

Before the patch the first call creates a card that no worker ever claims.

Usage::

    cd /opt/hermes && python3 verify_kanban_unknown_assignee.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []

# The #3786 shape: the name the model composed, and the profiles on the install.
COMPOSED = "seeded-a-cluster-agent"
PLATFORM = "platform"
CLUSTER = "cluster-kube-agents-evals-4-seeded-a-us-central1-a"
UNFINISHED = "cluster-kube-agents-evals-4-seeded-b-us-central1-a"
UNREGISTERED = "cluster-kube-agents-evals-4-seeded-c-us-central1-a"
ROOT_PROFILE = "default"
# kanban_unknown_assignee.PROFILES_DIR, PROFILE_MARKER and USER_MD_NAME.
PROFILES_DIR = "profiles"
PROFILE_MARKER = "profile.yaml"
USER_MD_NAME = "USER.md"


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


TMP = Path(tempfile.mkdtemp())
HOME = TMP / "home"
DB = TMP / "kanban.db"
for name, files in (
    (PLATFORM, (PROFILE_MARKER,)),
    (CLUSTER, (PROFILE_MARKER, USER_MD_NAME)),
    (UNFINISHED, (PROFILE_MARKER,)),
    (UNREGISTERED, ()),
):
    (HOME / PROFILES_DIR / name).mkdir(parents=True)
    for file in files:
        (HOME / PROFILES_DIR / name / file).write_text("x\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_KANBAN_DB"] = str(DB)
for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_KEY"):
    os.environ.pop(var, None)

from hermes_cli import kanban_db_connect as KC  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402

conn = KC.connect(DB)


def create(**args):
    return json.loads(kt._handle_create(args))


def cards():
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


print("wiring:")
check("the create handler resolved the import", hasattr(kt, "_kanban_refuse_unknown_assignee"))

print("the #3786 replay:")
before = cards()
made_up = create(assignee=COMPOSED, title="Right-size the seeded-a workloads")
err = made_up.get("error", "")
check("a card for a name that is no profile is refused", not made_up.get("ok") and err, str(made_up))
check("the refusal names the wrong assignee and lists the profiles that can take a card",
      f"`{COMPOSED}` is not a profile" in err and f"`{PLATFORM}`" in err and f"`{CLUSTER}`" in err, err)
check("and it does not list a profile that cannot take one",
      f"`{UNFINISHED}`" not in err and f"`{UNREGISTERED}`" not in err, err)
check("and no card was written", cards() == before, f"{before} -> {cards()}")

unfinished = create(assignee=UNFINISHED, title="seeded-b")
check("a Cluster Agent profile whose scaffold did not finish is refused",
      not unfinished.get("ok") and "scaffold did not finish" in unfinished.get("error", ""), str(unfinished))
unregistered = create(assignee=UNREGISTERED, title="seeded-c")
check("a folder Hermes never registered is refused", not unregistered.get("ok"), str(unregistered))
check("and still no card was written", cards() == before, f"{before} -> {cards()}")

print("what it leaves alone:")
for label, assignee in (
    ("a card for the Platform Agent is created", PLATFORM),
    ("a card for a finished Cluster Agent is created", CLUSTER),
    ("an upper-case name of a real profile is created (create_task lower-cases it)", CLUSTER.upper()),
    ("a card for the gateway's own profile is created", ROOT_PROFILE),
):
    made = create(assignee=assignee, title=label)
    check(label, made.get("ok"), str(made))

print("from a worker's home:")
# A worker runs with its profile's folder as its home. Whether Hermes reads the
# variable now or kept the gateway's home from its first read, the profiles
# must still be found.
os.environ["HERMES_HOME"] = str(HOME / PROFILES_DIR / PLATFORM)
before = cards()
from_worker = create(assignee=COMPOSED, title="Right-size the seeded-a workloads")
check("a card for a name that is no profile is refused", not from_worker.get("ok"), str(from_worker))
check("and no card was written", cards() == before, f"{before} -> {cards()}")
made = create(assignee=CLUSTER, title="seeded-a from a worker")
check("a card for a finished Cluster Agent is created", made.get("ok"), str(made))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")

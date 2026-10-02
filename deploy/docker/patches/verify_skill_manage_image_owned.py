"""Build-time behaviour gate for the image-owned skills patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_skill_manage_image_owned.py``, in the ``agent-base``
stage. Proves, through the patched ``skill_manage`` itself and against
temporary template trees, that:

1. every write action on a shipped name is refused for the platform and
   cluster profiles before any handler runs, with a message that says the
   skill is image-owned -- flat, and as an ``operations`` batch, the one
   call shape the tool advertises;
2. the two other routes into a shipped skill's directory are refused too: a
   ``category`` that names a shipped skill, and a categorized name under one;
3. a name the image does not ship, and any name in the default profile, is
   not refused by this gate (whatever upstream then does with it);
4. an action the gate does not know is left to upstream's own error;
5. the file tools' write guard (``agent/file_safety.py``) refuses a target
   under the active home's ``skills/`` or ``scripts/`` tree with a message
   that names ``skill_manage``, and still allows the home's ``scratch/``.

The image's real template trees do not exist in ``agent-base`` (the
``platform`` stage copies them), so the check that the module's roots name
the Dockerfile's actual destinations runs there, in the same RUN that writes
the trees' manifests. Temporary trees are what make this script the unit
test's oracle in a checkout that has no ``/opt``. A failure here fails the
image build.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

failures: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")
        print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
    else:
        print(f"  ok   {label}")


def _make_template(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: shipped fixture.\n---\n# {name}\n"
        )


VALID_CONTENT = "---\nname: zz-local-note\ndescription: a note the agent keeps for itself.\n---\n# note\n"


def main() -> int:
    import tools.skill_manage_image_owned as gate
    import tools.skill_manager_tool as smt

    print("through the patched skill_manage, against temporary templates")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        p_root = tmp_path / "platform-template" / "skills"
        c_root = tmp_path / "cluster-template" / "skills"
        _make_template(p_root, "submit-suggestion", "gke-basics")
        _make_template(c_root, "gke-stall-detection")
        gate.PLATFORM_ROOTS = (str(p_root),)
        gate.CLUSTER_ROOTS = (str(c_root),)

        def run(home: Path, **kwargs) -> dict:
            home.mkdir(parents=True, exist_ok=True)
            (home / "skills").mkdir(exist_ok=True)
            os.environ["HERMES_HOME"] = str(home)
            raw = smt.skill_manage(**kwargs)
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return {"raw": raw}

        platform_home = tmp_path / "data" / "profiles" / "platform"
        cluster_home = tmp_path / "data" / "profiles" / "cluster-x"
        default_home = tmp_path / "data"

        for action, extra in (
            ("patch", {"old_string": "a", "new_string": "b"}),
            ("edit", {"content": VALID_CONTENT}),
            ("create", {"content": VALID_CONTENT}),
            ("delete", {}),
            ("write_file", {"file_path": "scripts/x.py", "file_content": "print()"}),
            ("remove_file", {"file_path": "scripts/x.py"}),
        ):
            out = run(platform_home, action=action, name="submit-suggestion", **extra)
            check(f"platform {action} submit-suggestion refused", out.get("success"), False)
            check(f"platform {action} names image-owned", "image-owned" in str(out.get("error", "")), True)

        out = run(platform_home, action="patch", name="ops/gke-basics", old_string="a", new_string="b")
        check("platform categorized shipped name refused", "image-owned" in str(out.get("error", "")), True)

        out = run(platform_home, action="create", name="scripts", category="gke-basics", content=VALID_CONTENT)
        check("platform create under a shipped skill's category refused",
              "image-owned" in str(out.get("error", "")), True)

        out = run(platform_home, action="patch", name="gke-basics/scripts", old_string="a", new_string="b")
        check("platform name nested under a shipped skill refused",
              "image-owned" in str(out.get("error", "")), True)

        out = run(platform_home, operations=[
            {"action": "create", "name": "zz-local-note", "content": VALID_CONTENT},
            {"action": "patch", "name": "submit-suggestion", "old_string": "a", "new_string": "b"},
        ], action="", name="")
        check("platform batch naming a shipped skill refused whole",
              "image-owned" in str(out.get("error", "")), True)
        check("the refusal names the shipped skill", "submit-suggestion" in str(out.get("error", "")), True)

        out = run(platform_home, operations=[
            {"action": "create", "name": "zz-local-note", "content": VALID_CONTENT},
        ], action="", name="")
        check("platform batch of a new name is not refused by the gate",
              "image-owned" in str(out.get("error", "")), False)

        out = run(cluster_home, action="delete", name="gke-stall-detection")
        check("cluster-x delete of a cluster skill refused", "image-owned" in str(out.get("error", "")), True)

        out = run(cluster_home, action="patch", name="submit-suggestion", old_string="a", new_string="b")
        check("cluster-x is not gated by the platform template", "image-owned" in str(out.get("error", "")), False)

        out = run(platform_home, action="create", name="zz-local-note", content=VALID_CONTENT)
        check("platform create of a new name is not refused by the gate",
              "image-owned" in str(out.get("error", "")), False)

        out = run(default_home, action="patch", name="submit-suggestion", old_string="a", new_string="b")
        check("default profile is not gated", "image-owned" in str(out.get("error", "")), False)

        out = run(platform_home, action="frobnicate", name="submit-suggestion")
        check("unknown action is left to upstream", "image-owned" in str(out.get("error", "")), False)

        print("5. the file tools' write guard")
        import agent.file_safety as fs
        os.environ["HERMES_HOME"] = str(platform_home)
        os.environ.pop("HERMES_WRITE_SAFE_ROOT", None)
        for sub in ("skills", "scripts", "scratch"):
            (platform_home / sub).mkdir(exist_ok=True)
        skill_md = platform_home / "skills" / "gke-basics" / "SKILL.md"
        skill_md.parent.mkdir(parents=True, exist_ok=True); skill_md.write_text("x\n")
        for label, target, expect in (
            ("a shipped skill's SKILL.md", str(skill_md), True),
            ("a new skill under the profile's skills/", str(platform_home / "skills" / "zz-local-note" / "SKILL.md"), True),
            ("the skills directory itself", str(platform_home / "skills"), True),
            ("a profile script", str(platform_home / "scripts" / "helper.py"), True),
            ("the profile's scratch", str(platform_home / "scratch" / "notes.md"), False),
        ):
            denial = fs.get_write_denied_error(target) or ""
            check(f"file guard refuses {label}", "image manages" in denial, expect)
            if expect:
                check(f"file guard names skill_manage for {label}", "skill_manage" in denial, True)
        check("file guard verb is carried", (fs.get_write_denied_error(str(skill_md), verb="Delete") or "").startswith("Delete denied"), True)

    if failures:
        print("\nVERIFY FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("\nVERIFY OK: skill_manage refuses writes to image-shipped skills")
    return 0


if __name__ == "__main__":
    sys.exit(main())

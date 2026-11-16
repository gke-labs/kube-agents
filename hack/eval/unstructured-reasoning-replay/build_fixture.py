"""Build the #3206 replay fixture from this repository and the pinned Hermes source only.

usage: python3 -I build_fixture.py <kube-agents checkout> <hermes-agent v2026.9.14 checkout> <out.json>

Writes an OpenAI chat/completions body (model, messages, tools) shaped like a
fleet-consistency-drift cron run: the platform agent's system prompt, the cron
prompt, then a clean tool call and its result, then a reply whose content opens
with `thought` (Gemini's reasoning written as content) and its tool result. The
leaked reply is the latest assistant turn before a tool result, which is the
shape that makes Gemini imitate it (see README.md). Nothing in it comes from a
cluster. Hermes source is parsed with ast: constants are literal-evaluated, and
f-string or concatenation expressions over them are evaluated with no builtins.
Nothing from it is imported.

The system prompt approximates the agent's, it does not reproduce it. The
skills index takes each description from a one-line frontmatter read, so a
description written as a YAML block scalar renders as `>-` or not at all, and
the memory block, MCP tools and kanban tools are left out. The recorded red and
green were made on this prompt. The planted reply drives the result: swapping
the system prompt, the user turn or the tool results for other text kept it red.
"""
import ast
import json
import re
import sys
from pathlib import Path

REPO, HERMES, OUT = (Path(a) for a in sys.argv[1:4])


PLACEHOLDERS = {}


class _Placeholders(dict):
    """Locals for the restricted eval: an unresolvable name renders as <NAME> and is recorded."""

    def __init__(self, known):
        super().__init__(known)
        self.missed = set()

    def __missing__(self, key):
        self.missed.add(key)
        return f"<{key}>"


def hermes_constants(rel, names):
    # Module-level constants only: literal_eval, else an f-string/concat over constants already seen,
    # evaluated with no builtins (names only resolve to earlier constants).
    tree = ast.parse((HERMES / rel).read_text())
    known, found = {"FOREGROUND_MAX_TIMEOUT": 600, "DEFAULT_READ_LIMIT": 2000}, {}  # env default; file_operations_common
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            try:
                known[name] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                if all(isinstance(n, (ast.Constant, ast.Name, ast.JoinedStr, ast.FormattedValue, ast.Dict,
                                      ast.Tuple, ast.List, ast.BinOp, ast.Add, ast.Load, ast.expr_context))
                       or isinstance(n, ast.operator) for n in ast.walk(node.value)):
                    try:
                        ns = _Placeholders(known)
                        known[name] = eval(compile(ast.Expression(node.value), rel, "eval"),
                                           {"__builtins__": {}}, ns)
                        if ns.missed:
                            PLACEHOLDERS.setdefault(f"{rel}:{name}", sorted(ns.missed))
                    except Exception:
                        pass
            if name in names and name in known:
                found[name] = known[name]
    missing = set(names) - set(found)
    if missing:
        raise SystemExit(f"could not literal-eval {sorted(missing)} from {rel}")
    return found


def frontmatter(text):
    # One-line `key: value` reads only; see the module docstring for what that drops.
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


pb = hermes_constants("agent/prompt_builder.py", [
    "TOOL_USE_ENFORCEMENT_GUIDANCE", "GOOGLE_MODEL_OPERATIONAL_GUIDANCE",
    "TASK_COMPLETION_GUIDANCE", "PARALLEL_TOOL_CALL_GUIDANCE", "STEER_CHANNEL_NOTE", "PLATFORM_HINTS"])
schemas = {}
for rel, name in [("tools/terminal_tool.py", "TERMINAL_SCHEMA"), ("tools/file_tools.py", "READ_FILE_SCHEMA"),
                  ("tools/file_tools.py", "SEARCH_FILES_SCHEMA"), ("tools/file_tools.py", "WRITE_FILE_SCHEMA"),
                  ("tools/skills_tool.py", "SKILL_VIEW_SCHEMA"), ("tools/skills_tool.py", "SKILLS_LIST_SCHEMA")]:
    schemas.update(hermes_constants(rel, [name]))
cron_hint_src = (HERMES / "cron/scheduler_prompt.py").read_text()
CRON_HINT = ast.literal_eval(re.search(r"_CRON_HINT = (\(.*?\n\))\n", cron_hint_src, re.S).group(1))
SINGLE_SKILL = hermes_constants("agent/skill_commands.py", ["_SINGLE_SKILL_INSTRUCTION"])["_SINGLE_SKILL_INSTRUCTION"]

# --- system prompt: SOUL.md, then the guidance blocks Hermes adds for a gemini model, the skills
# index, and the cron platform hint (agent/system_prompt.py order, approximately).
soul = (REPO / "agents/platform/SOUL.md").read_text().strip()
skills = []
for skill_md in sorted((REPO / "agents/platform/skills").glob("*/SKILL.md")):
    fm = frontmatter(skill_md.read_text())
    skills.append((fm.get("name", skill_md.parent.name), fm.get("description", "")))
index = "\n".join(f"    - {n}: {d}" if d else f"    - {n}" for n, d in skills)
skills_block = (
    "## Skills\n"
    "Before replying, scan the skills below. If a skill matches or is even partially relevant to your task, "
    "you MUST load it with skill_view(name) and follow its instructions.\n\n"
    "<available_skills>\n  platform:\n" + index + "\n</available_skills>\n\n"
    "Only proceed without loading a skill if genuinely none are relevant to the task.")
system = "\n\n".join([
    soul, pb["TOOL_USE_ENFORCEMENT_GUIDANCE"], pb["GOOGLE_MODEL_OPERATIONAL_GUIDANCE"],
    pb["TASK_COMPLETION_GUIDANCE"], pb["PARALLEL_TOOL_CALL_GUIDANCE"], pb["STEER_CHANNEL_NOTE"],
    skills_block, pb["PLATFORM_HINTS"]["cron"]])

# --- user turn: the cron job prompt as cron/scheduler_prompt.py assembles it with skills attached.
jobs = json.loads((REPO / "agents/platform/cron/jobs.json").read_text())
jobs = jobs["jobs"] if isinstance(jobs, dict) else jobs
job = next(j for j in jobs if j["id"] == "fleet-consistency-drift")
skill_text = (REPO / "agents/platform/skills/fleet-audit/SKILL.md").read_text().strip()
user = "\n".join([
    '[IMPORTANT: The user has invoked the "fleet-audit" skill, indicating they want you to follow its '
    'instructions. The full skill content is loaded below.]',
    "", skill_text, "", SINGLE_SKILL + CRON_HINT + job["prompt"]])

# --- the synthetic leaked reply, its tool call, and synthetic tool results.
SOP_PATH = "/opt/data/profiles/platform/governance/fleet_consistency_drift_sop.md"
LEAK = (
    "thought\n"
    "The user wants the weekly fleet consistency drift audit. The prompt is explicit about the order: read "
    "the SOP at /opt/data/profiles/platform/governance/fleet_consistency_drift_sop.md in full first (339 "
    "lines; section 4 is lines 121-263 and holds the twenty checks), then run the collector "
    "fleet_drift.py into /opt/data/scratch/manifest_fleet-consistency-drift.json, then compare facets the "
    "way section 4 says, and close the run with the fleet-audit skill's finish step, passing the same "
    "manifest file.\n\n"
    "Plan:\n"
    "1. Read the whole SOP with read_file (limit high enough to get all 339 lines).\n"
    "2. Open the audit run per the fleet-audit skill.\n"
    "3. Run the collector and redirect stdout to the manifest path.\n"
    "4. Walk the twenty checks against the manifest; the baseline comes from the live fleet, not a "
    "blueprint.\n"
    "5. Finish with --manifest-file.\n\n"
    "Let me start by reading the SOP.")
sop = (REPO / "agents/platform/governance/fleet_consistency_drift_sop.md").read_text()
sop_lines = sop[:-1].split("\n") if sop.endswith("\n") else sop.split("\n")
read_result = json.dumps({
    "content": "\n".join(f"{i}|{line}" for i, line in enumerate(sop_lines, 1)),
    "total_lines": len(sop_lines), "file_size": len(sop.encode()), "truncated": False}, ensure_ascii=False)
contexts = "seeded-a\nseeded-b\nseeded-c\n"
term_result = json.dumps({"output": contexts, "exit_code": 0, "error": None})
call1 = {"id": "call_synthetic_read_sop", "type": "function",
         "function": {"name": "read_file", "arguments": json.dumps({"path": SOP_PATH, "offset": 1, "limit": 400})}}
call2 = {"id": "call_synthetic_contexts", "type": "function",
         "function": {"name": "terminal", "arguments": json.dumps({"command": "kubectl config get-contexts -o name"})}}

# The clean turn first, the leaked reply last: Gemini copies a leaked reply when it is the
# latest assistant turn before a tool result, and not when a clean turn follows it.
messages = [
    {"role": "system", "content": system},
    {"role": "user", "content": user},
    {"role": "assistant", "content": "", "tool_calls": [call2]},
    {"role": "tool", "tool_call_id": call2["id"], "content": term_result},
    {"role": "assistant", "content": LEAK, "tool_calls": [call1]},
    {"role": "tool", "tool_call_id": call1["id"], "content": read_result},
]
tools = [{"type": "function", "function": schemas[k]} for k in
         ["TERMINAL_SCHEMA", "READ_FILE_SCHEMA", "SEARCH_FILES_SCHEMA", "WRITE_FILE_SCHEMA",
          "SKILL_VIEW_SCHEMA", "SKILLS_LIST_SCHEMA"]]
body = {"model": "model-default", "messages": messages, "tools": tools}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n")
if PLACEHOLDERS:
    print("unresolved names rendered as placeholders:", json.dumps(PLACEHOLDERS))
print(f"wrote {OUT}: system={len(system)} chars, user={len(user)} chars, "
      f"tools={[t['function']['name'] for t in tools]}")

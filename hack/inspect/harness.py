from pathlib import Path

from inspect_ai.agent import Agent, AgentState, agent, sandbox_agent_bridge
from inspect_ai.model import get_model
from inspect_ai.util import sandbox
from inspect_swe import claude_code, gemini_cli

SKILLS = [Path(__file__).parent / "skills/fleet-inventory"]
WORKDIR = "/app"
# Where the platform image's entrypoint scaffolds the profiles.
HERMES_HOME = "/opt/data"

# Pinned so a comparison never straddles a harness release.
GEMINI_CLI_VERSION = "0.61.0"
CLAUDE_CODE_VERSION = "2.1.277"


def harness_sandbox(environment: Path, harness: str) -> tuple[str, str]:
    """Hermes runs in the platform image (hermes.yaml); the others in sandbox.yaml."""
    return ("k8s-pod", str(environment / ("hermes.yaml" if harness == "hermes" else "sandbox.yaml")))


@agent
def hermes(skills: list[Path] | None) -> Agent:
    """The Platform Agent from the shipped image, one-shot, with any extra skills added to its profile."""

    async def execute(state: AgentState) -> AgentState:
        for skill in skills or []:
            for file in (f for f in skill.rglob("*") if f.is_file()):
                dest = f"{HERMES_HOME}/profiles/platform/skills/{skill.name}/{file.relative_to(skill)}"
                await sandbox().write_file(dest, file.read_bytes())
        # hermes.yaml asks for "gemini": Hermes replays Gemini's thought signatures only to a
        # model so named, and the bridge serves the eval's model under it.
        async with sandbox_agent_bridge(state, model_aliases={"gemini": get_model()}) as bridge:
            prompt = "\n\n".join(m.text for m in state.messages if m.role == "user")
            result = await sandbox().exec(
                ["hermes", "-p", "platform", "chat", "-Q", "--yolo", "--query-file", "-"],
                input=prompt,
                cwd=WORKDIR,
                env={"HERMES_HOME": HERMES_HOME},
                concurrency=False,
            )
            if not result.success:
                raise RuntimeError(f"hermes exited {result.returncode}: {result.stderr}{result.stdout}")
        return bridge.state

    return execute


def harness_agent(harness: str, skills: bool, identity: str | None) -> Agent:
    """The harness under test: Gemini CLI, Claude Code or Hermes, with or without the skills."""
    agent_skills = SKILLS if skills else None
    if harness == "gemini":
        return gemini_cli(skills=agent_skills, cwd=WORKDIR, version=GEMINI_CLI_VERSION)
    if harness == "claude":
        # identity (e.g. identity=claude-sonnet-5): the Claude model Claude Code
        # presents itself as, needed when the bridged model is not an Anthropic one.
        return claude_code(
            skills=agent_skills, cwd=WORKDIR, version=CLAUDE_CODE_VERSION, model_config=identity
        )
    if harness == "hermes":
        return hermes(agent_skills)
    raise ValueError(f"unknown harness {harness!r}: use gemini, claude or hermes")

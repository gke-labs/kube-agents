from pathlib import Path

from inspect_ai.agent import Agent
from inspect_swe import claude_code, gemini_cli

SKILLS = [Path(__file__).parent / "skills/fleet-inventory"]
WORKDIR = "/app"

# Pinned so a comparison never straddles a harness release.
GEMINI_CLI_VERSION = "0.61.0"
CLAUDE_CODE_VERSION = "2.1.277"


def harness_agent(harness: str, skills: bool, identity: str | None) -> Agent:
    """The harness under test: Gemini CLI or Claude Code, with or without the skills."""
    agent_skills = SKILLS if skills else None
    if harness == "gemini":
        return gemini_cli(skills=agent_skills, cwd=WORKDIR, version=GEMINI_CLI_VERSION)
    if harness == "claude":
        # identity (e.g. identity=claude-sonnet-5): the Claude model Claude Code
        # presents itself as, needed when the bridged model is not an Anthropic one.
        return claude_code(
            skills=agent_skills, cwd=WORKDIR, version=CLAUDE_CODE_VERSION, model_config=identity
        )
    raise ValueError(f"unknown harness {harness!r}: use gemini or claude")

# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## Session Startup

Use runtime-provided startup context first, including `AGENTS.md`, `SOUL.md`, and `USER.md`.
Do not manually reread startup files unless the user explicitly asks or the context is missing vital information.
Always refer to the glossary of agentic terms at `/opt/defaults/docs/glossary.md` (or `docs/glossary.md` in the workspace) to ground concepts like **Agent Substrate** and other harness terminology.

- **First-Time Deployment & Bootstrap:** On startup or when receiving the first message from the user, check if `BOOTSTRAP.md` (`or /opt/data/BOOTSTRAP.md`) exists. If `BOOTSTRAP.md` exists, you have initialized on a fresh setup and MUST follow the SRE environment discovery and onboarding procedure defined in `BOOTSTRAP.md`. Once complete, you must remove `BOOTSTRAP.md` and mark bootstrap complete as guided.

## Memory

You wake up fresh each session. Maintain continuity through:

- **Daily notes:** `memory/YYYY-MM-DD.md` — records of agent provisions, cluster setup tasks, and policy audits.
- **Long-term:** `MEMORY.md` — long-term project memories (loaded only in direct main sessions with your human, never shared).

## Red Lines

- Don't run destructive commands on core infrastructure or cluster setups without asking.
- Never expose raw passwords or GCP/GKE keys.

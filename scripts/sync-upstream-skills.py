#!/usr/bin/env python3
"""Syncs GKE agent skills from the upstream google/skills repository (skills/cloud)."""

import os
import shutil
import subprocess
import sys
import tempfile

UPSTREAM_REPO = "https://github.com/google/skills.git"
UPSTREAM_SKILLS_PATH = os.path.join("skills", "cloud")
SKILL_PREFIX = "gke-"

# Target agents where upstream GKE skills should be synced.
DEFAULT_TARGET_AGENTS = ["platform"]
SKILL_AGENT_OVERRIDES = {
    # Per-skill target agent overrides if specific skills should go to multiple agents
    # e.g., "gke-observability": ["platform", "cluster"],
}

# Marker that identifies our auto-injected footer, so injection is idempotent and
# the footer can be recognized/stripped later if needed.
FOOTER_MARKER = "<!-- kube-agents: cluster-agent coupling (auto-injected by sync-upstream-skills.py) -->"

# Upstream skills are copied over verbatim on every sync (the local dir is rmtree'd first), so any
# local edits are wiped. The GKE create/lifecycle skills must, however, keep pointing at this repo's
# Cluster Agent profile lifecycle (which upstream knows nothing about). These footers are the single
# source of truth for that coupling and are re-appended after each sync. See
# agents/platform/skills/cluster-agent-lifecycle/SKILL.md for the mechanics they reference.
SKILL_FOOTERS = {
    "gke-cluster-creation": f"""{FOOTER_MARKER}

## Required final step: provision the Cluster Agent profile

Creating a cluster is **not complete** until it has a Cluster Agent. A managed cluster and its
Cluster Agent profile are **created together** — never leave a newly created cluster without a
profile. Immediately after `create_cluster` succeeds and the cluster is reachable, create its
dedicated **Cluster Agent** profile (this is what makes the cluster delegable for runtime
debugging). Use the [cluster-agent-lifecycle](../cluster-agent-lifecycle/SKILL.md) skill:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py create \\
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

The command is idempotent, so it is safe to re-run. This gives the new cluster an agent
immediately. (The `cluster-agent-reconcile` cron would also pick it up on its next run — it
manages every cluster in the project except the management cluster — so no labeling is required.)

## Cluster Agent Profile Teardown

A managed cluster and its Cluster Agent profile are **deleted together**. When a cluster is
decommissioned/deleted, also remove its dedicated **Cluster Agent** profile (created at onboarding).
Use the [cluster-agent-lifecycle](../cluster-agent-lifecycle/SKILL.md) skill:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py delete \\
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

Do not delete a Cluster Agent profile while its cluster still exists.

Deleting the profile here is the immediate, preferred path. As a backstop, the hourly
`cluster-agent-reconcile` job auto-prunes any profile whose cluster is definitively gone, so a
profile missed during teardown is cleaned up on the next reconcile cycle.
""",
}


def inject_footer(dest_path, skill_name):
    """Append the Cluster Agent coupling footer to a freshly-synced skill's SKILL.md.

    Idempotent: does nothing if the skill has no footer configured or the footer marker is
    already present. Returns True if a footer was written, else False.
    """
    footer = SKILL_FOOTERS.get(skill_name)
    if footer is None:
        return False

    skill_md = os.path.join(dest_path, "SKILL.md")
    if not os.path.isfile(skill_md):
        print(f"Warning: {skill_md} not found; cannot inject Cluster Agent footer.", file=sys.stderr)
        return False

    with open(skill_md, "r", encoding="utf-8") as f:
        existing = f.read()
    if FOOTER_MARKER in existing:
        return False

    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    with open(skill_md, "a", encoding="utf-8") as f:
        f.write(separator + footer)
    return True


def run_cmd(cmd, cwd=None):
    """Runs a shell command and returns the result, raising an exception on failure."""
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error running command: {' '.join(cmd)}", file=sys.stderr)
        print(f"Stdout:\n{res.stdout}", file=sys.stderr)
        print(f"Stderr:\n{res.stderr}", file=sys.stderr)
        res.check_returncode()
    return res

def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    
    try:
        print("Creating temporary directory for shallow clone...")
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"Cloning upstream repository (depth 1): {UPSTREAM_REPO}...")
            run_cmd([
                "git", "clone", "--depth", "1",
                UPSTREAM_REPO, tmpdir
            ])
            
            upstream_skills_dir = os.path.join(tmpdir, UPSTREAM_SKILLS_PATH)
            if not os.path.isdir(upstream_skills_dir):
                print(f"Error: upstream skills directory not found in clone: {upstream_skills_dir}", file=sys.stderr)
                sys.exit(1)
                
            # Discover all skills that start with the prefix (e.g. 'gke-')
            discovered_skills = sorted([
                name for name in os.listdir(upstream_skills_dir)
                if name.startswith(SKILL_PREFIX) and os.path.isdir(os.path.join(upstream_skills_dir, name))
            ])
            
            if not discovered_skills:
                print(f"Warning: No skills found matching prefix '{SKILL_PREFIX}' in {upstream_skills_dir}", file=sys.stderr)
                return
                
            print(f"\nDiscovered {len(discovered_skills)} skills matching prefix '{SKILL_PREFIX}':")
            for name in discovered_skills:
                print(f"  - {name}")

            # Prune obsolete local skill directories that were renamed/removed upstream
            target_agents = set(DEFAULT_TARGET_AGENTS + [a for agents in SKILL_AGENT_OVERRIDES.values() for a in agents])
            for agent in target_agents:
                agent_skills_dir = os.path.join(repo_root, "agents", agent, "skills")
                if os.path.isdir(agent_skills_dir):
                    for local_name in sorted(os.listdir(agent_skills_dir)):
                        if local_name.startswith(SKILL_PREFIX) and local_name not in discovered_skills:
                            stale_path = os.path.join(agent_skills_dir, local_name)
                            print(f"Removing obsolete upstream skill: agents/{agent}/skills/{local_name}...")
                            shutil.rmtree(stale_path)
                
            print("\nSyncing skills...")
            for skill_name in discovered_skills:
                src_skill_path = os.path.join(upstream_skills_dir, skill_name)
                agents = SKILL_AGENT_OVERRIDES.get(skill_name, DEFAULT_TARGET_AGENTS)
                
                for agent in agents:
                    dest_path = os.path.join(repo_root, "agents", agent, "skills", skill_name)
                    print(f"Syncing '{skill_name}' to agents/{agent}/skills/{skill_name}...")
                    
                    # Delete existing destination directory to remove stale files
                    if os.path.exists(dest_path):
                        shutil.rmtree(dest_path)
                        
                    # Re-create destination parent directories if needed
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    
                    # Copy from upstream src to dest
                    shutil.copytree(src_skill_path, dest_path)

                    # Re-inject the Cluster Agent coupling footer (wiped by the copy above).
                    if inject_footer(dest_path, skill_name):
                        print(f"  Injected Cluster Agent coupling footer into {skill_name}/SKILL.md")

            print("\nSynchronization complete!")
    except subprocess.CalledProcessError:
        print("\nError: Synchronization failed due to command error. Details above.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nError: An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

# Persona content for the A2A bus

What lives here: the platform agent's `a2a-topics` skill, which is how a
running agent reads and writes the topic blackboard. It sits under `a2a/`
rather than in `agents/platform/skills/` on purpose — a skill copied into the
shipped persona tree would appear in the agent's skill list on every install,
including the ones where the bus does not exist. "A normal install cannot tell
this feature exists" is the mode switch's promise
(`docs/designs/spec-mode-switch.md`), and a skill file is the easiest way to
break it.

This directory is the SOURCE the agent image builds from: the Dockerfile
copies `platform/skills/` to `/opt/a2a-template/skills/`, deliberately outside
`/opt/platform-template`, and the entrypoint overlays it into the platform
profile only on a `next` install (below).

## The three pieces, and where each one lives

| Piece                                      | Home                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The `a2a` client binary                    | built from `a2a/cmd/a2a` in the image's `a2a-builder` stage and `COPY`d to `/usr/local/bin/a2a` — the `k8s-event-watcher` pattern. Ungated: a binary is inert until something invokes it, and the thing that invokes it is what ships dark                                                                                      |
| `SKILL.md`                                 | shipped at `/opt/a2a-template/skills/`, overlaid into the profile by `docker-entrypoint.sh` step 2.6a-bis only when `runtime_mode.is_next()`. The overlay rides step 2.6a's staged swap, which is what makes it stick — and what cleans it off on the first boot after a flip back to `today`                                   |
| `NATS_URL` / `NATS_USER` / `NATS_PASSWORD` | rendered by the operator into the agent container's env under `next`, as the `worker` user, from the `<agent>-a2a-nats-creds` Secret the gateway already reads. A bridge sidecar declared in `spec.deployment.sidecars` shares the pod and declares the same three against the same Secret, so this is one Secret seam, not two |

## The skill copy does not survive a restart, and that is by design

The entrypoint's step 2.6a (`sync_profile_skills` in
`deploy/shared/docker-entrypoint.sh`) rebuilds each profile's `skills/` from
the image into a staging tree and renames it over the live directory on
**every** pod start. It is a replace, not a merge — deliberately, so that an
image roll cannot leave a profile running stale skills. A skill that exists
only on the PVC is therefore deleted by the next restart, silently.

So there is no supported way to add a skill to a running agent without going
through the image. That constraint is why the mode-gated overlay exists: it is
not a nicety, it is the only route — and it is also what makes the gate
honest, since the same replace that would delete a hand-copied skill is what
removes the overlaid one the first boot after the mode leaves `next`.

## The shell sandbox does not carry the client, on purpose

Since #913 every command the agent runs executes in the shell sandbox pod,
which ships no `a2a` binary and holds no credential — deliberately, and the
bus credential must not become the exception (see
`docs/designs/agent-shell-sandboxing.md`). Until the topics reader moves off
the shell (an MCP tool running in the agent process, where the operator's env
already is), the skill's shell invocation reports the client missing and the
skill's own instructions tell the agent to say so rather than guess. Do not
"fix" this by adding the binary or `NATS_*` env to the sandbox image: that
moves a credential into the one container built never to hold one.

## Nothing to install by hand

On a `next` install the operator and the image do all three placements; a
fresh pod roll is the whole procedure. If you find yourself copying anything
onto the PVC to make the reader work, the install is not actually running
`mode: next`, and that is the thing to fix.

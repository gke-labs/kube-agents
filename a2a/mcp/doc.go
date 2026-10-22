// Package mcp is the kube-agents Model Context Protocol server: a stdio
// JSON-RPC server that lets a local coding agent drive a kube-agents
// deployment through its A2A JetStream bus - discover and connect to
// deployments, send a message and stream the turn's steps back as it runs,
// inspect and steer tasks, read blackboard topics, open the PRs an agent
// filed, and receive push notifications when work finishes out of turn.
//
// It is a bus client and a renderer, not an executor: everything it shows is
// folded off the TASKS, DIRECTORY, and TOPICS-* streams and the console
// subjects as the A2A payload spec defines them, and the step list it renders
// is read structurally from the executor's kube-agents.step/v1 records
// (a2a/lib/stepmsg) rather than parsed out of prose. It also projects the
// live A2A JetStream fleet (DIRECTORY agent cards, specialist profiles,
// subagent-bound /session worker pods, TASKS delegations/Kanban cards, and
// TOPICS-STATE snapshots) into Antigravity's native Swarm UI directory
// (.system_generated/swarm/) via SwarmSink and LineageResolver. See
// a2a/docs/mcp-server.md for the module layout, the step contract, how a
// long turn streams to an MCP client, and how the on-disk state stays
// coherent across the processes that share it.
package mcp

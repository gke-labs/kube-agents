package mcp

import (
	"context"
	"encoding/json"
	"fmt"
)

func (s *Server) readResource(ctx context.Context, uri string) (string, error) {
	switch uri {
	case resourceURIStatus:
		return s.handleConnectionStatus(ctx)
	case resourceURIDeployments:
		return s.handleDiscoverDeployments(ctx, nil)
	case resourceURITopics:
		return s.handleReadTopics(ctx, nil)
	case resourceURITasks:
		txt, _, _, err := s.handleTaskControl(ctx, nil)
		return txt, err
	case resourceURIEvents:
		raw, _ := json.Marshal(map[string]any{"peek_only": true})
		return s.handlePollEvents(ctx, raw)
	default:
		return "", fmt.Errorf("unknown resource URI %q", uri)
	}
}

func (s *Server) resourceDefinitions() []MCPResource {
	return []MCPResource{
		{
			URI:         resourceURIStatus,
			Name:        "Kube-Agents Bus & Connection Status",
			Description: "Live connection health, JetStream stream usage, durable consumer liveness, and agent directory cards.",
			MIMEType:    mimeTypeMarkdown,
		},
		{
			URI:         resourceURIDeployments,
			Name:        "Discovered Kube-Agents Deployments",
			Description: "All discovered and saved kube-agents deployments across Kubernetes contexts.",
			MIMEType:    mimeTypeMarkdown,
		},
		{
			URI:         resourceURITopics,
			Name:        "Kube-Agents Blackboard Topics",
			Description: "Standing fleet and cluster state on TOPICS-STATE and TOPICS-JOURNAL.",
			MIMEType:    mimeTypeMarkdown,
		},
		{
			URI:         resourceURITasks,
			Name:        "Recent Kube-Agents Tasks",
			Description: "Recent and in-flight tasks on the TASKS JetStream stream.",
			MIMEType:    mimeTypeMarkdown,
		},
		{
			URI:         resourceURIEvents,
			Name:        "Pending Push Events",
			Description: "Unread asynchronous push notifications and task events from kube-agents.",
			MIMEType:    mimeTypeMarkdown,
		},
	}
}

func (s *Server) toolDefinitions() []MCPToolDefinition {
	return []MCPToolDefinition{
		{
			Name:        toolDiscoverDeployments,
			Description: "Discover remote kube-agents deployments across Kubernetes contexts in ~/.kube/config and list saved deployment profiles.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"kube_context": map[string]any{
						"type":        "string",
						"description": "Optional specific kubectl context to probe. Omit to probe all contexts in parallel.",
					},
					"include_credentials": map[string]any{
						"type":        "boolean",
						"description": "Whether to pre-fetch and cache NATS bus credentials during discovery.",
					},
				},
			},
		},
		{
			Name:        toolConnectDeployment,
			Description: "Connect or switch to a remote kube-agents deployment (by name, cluster name, kubectl context, or direct NATS URL). Automatically manages port-forwarding, credentials, JetStream stream bootstrap, and real-time push subscriptions.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"deployment": map[string]any{
						"type":        "string",
						"description": "Deployment name, cluster name, or kubectl context to connect to. Omit to connect to the default/active deployment.",
					},
					"kube_context": map[string]any{
						"type":        "string",
						"description": "Optional kubectl context override.",
					},
					"namespace": map[string]any{
						"type":        "string",
						"description": "Optional Kubernetes namespace (default: kubeagents-system).",
					},
					"direct_nats_url": map[string]any{
						"type":        "string",
						"description": "Optional direct NATS URL (e.g. nats://127.0.0.1:4222) if connecting without kubectl port-forward.",
					},
					"bus_user": map[string]any{
						"type":        "string",
						"description": "Optional NATS bus user (default: console).",
					},
					"bus_auth_value": map[string]any{
						"type":        "string",
						"description": "Optional NATS bus password when using direct_nats_url.",
					},
					"agy_conversation_id": map[string]any{
						"type":        "string",
						"description": "Optional active Antigravity conversation UUID to bind for real-time agentapi push notifications.",
					},
				},
			},
		},
		{
			Name:        toolConnectionStatus,
			Description: "Inspect the current kube-agents connection, A2A v2 JetStream stream capacities (TASKS, DIRECTORY, TOPICS-STATE, TOPICS-JOURNAL), durable consumer health, registered agent cards, and active session/push state.",
			InputSchema: map[string]any{
				"type":       "object",
				"properties": map[string]any{},
			},
		},
		{
			Name:        toolSendMessage,
			Description: "Send a prompt, question, task, or command to kube-agents over the A2A v2 JetStream bus and stream back thinking, tool activity timelines, progress plans, delegations (session -> platform -> wake), notices, PR links, and structured kube-agents.chat/v1 results.",
			InputSchema: map[string]any{
				"type":     "object",
				"required": []string{"message"},
				"properties": map[string]any{
					"message": map[string]any{
						"type":        "string",
						"description": "The prompt, question, or command (/session, /session off, delegate: ..., status, stop) to send to kube-agents.",
					},
					"mode": map[string]any{
						"type":        "string",
						"enum":        []string{"auto", "platform", "session", "delegate"},
						"description": "Routing mode: 'auto' (use current conversation mode), 'platform' (route to Hermes platform agent), 'session' (ensure /session persistent Claude worker pod is active), or 'delegate' (delegate from session worker to platform agent and wait for wake synthesis).",
					},
					"conversation": map[string]any{
						"type":        "string",
						"description": "Optional conversation alias or console token to send on (defaults to active conversation).",
					},
					"wait_timeout_seconds": map[string]any{
						"type":        "integer",
						"description": "Seconds to wait for task completion while live-streaming thinking & tool progress before returning a snapshot (default: 150). Background push remains armed if the task continues running.",
					},
					"agy_conversation_id": map[string]any{
						"type":        "string",
						"description": "Optional active Antigravity conversation UUID to bind for real-time agentapi push notifications.",
					},
					"debug": map[string]any{
						"type":        "boolean",
						"description": "Optional debug mode (default: false). Set to true ONLY when the user explicitly asks to debug kube-agents itself (shows tool call tables, timings, and task metadata).",
					},
				},
			},
		},
		{
			Name:        toolManageSession,
			Description: "Manage kube-agents conversations and persistent /session worker pods: list conversations, create a new conversation, switch conversations, toggle /session worker mode on/off, delete a conversation/session, clear all killed/terminated sessions, toggle debug mode on/off, or view the conversation transcript.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"action": map[string]any{
						"type":        "string",
						"enum":        []string{"list", "new", "switch", "session_on", "session_off", "delete", "clear_killed", "debug_on", "debug_off", "transcript"},
						"description": "Session management action to perform.",
					},
					"alias": map[string]any{
						"type":        "string",
						"description": "Conversation alias (e.g. 'upgrade-triage', 'default') for new/switch/session_on/session_off/delete/transcript.",
					},
					"description": map[string]any{
						"type":        "string",
						"description": "Optional human-readable description when creating or switching a conversation.",
					},
					"debug": map[string]any{
						"type":        "boolean",
						"description": "Optional debug flag (default: false) to include tool call tables and timings when viewing a transcript.",
					},
				},
			},
		},
		{
			Name:        toolTaskControl,
			Description: "Inspect or control tasks on the TASKS JetStream stream: get a task's full folded state (thinking, tool activities, progress, delegation chain, result), list recent/active tasks, trigger gateway status replay, steer an in-flight task, or stop/cancel a running task.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"action": map[string]any{
						"type":        "string",
						"enum":        []string{"get", "list", "status", "steer", "stop"},
						"description": "Task control action to perform.",
					},
					"task_id": map[string]any{
						"type":        "string",
						"description": "Task ID to inspect when action is 'get'.",
					},
					"conversation": map[string]any{
						"type":        "string",
						"description": "Optional conversation alias or token filter.",
					},
					"message": map[string]any{
						"type":        "string",
						"description": "Steering instruction text when action is 'steer'.",
					},
					"only_active": map[string]any{
						"type":        "boolean",
						"description": "When action is 'list', only return non-terminal in-flight tasks.",
					},
					"limit": map[string]any{
						"type":        "integer",
						"description": "Maximum number of tasks to return when action is 'list' (default: 20).",
					},
					"wait_timeout_seconds": map[string]any{
						"type":        "integer",
						"description": "Optional seconds to wait (with live streaming progress) when action is 'get' or 'steer' on an in-flight task.",
					},
					"debug": map[string]any{
						"type":        "boolean",
						"description": "Optional debug mode (default: false). Set to true ONLY when the user explicitly asks to debug kube-agents itself (shows tool call tables, timings, and task metadata).",
					},
				},
			},
		},
		{
			Name:        toolReadTopics,
			Description: "Read standing blackboard topics from TOPICS-STATE and TOPICS-JOURNAL (e.g. upgrade-readiness, blueprint, annotations) or DIRECTORY agent cards.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"action": map[string]any{
						"type":        "string",
						"enum":        []string{"list", "read", "cards"},
						"description": "Action: 'list' all topics, 'read' a specific topic's latest envelope and JSON data, or 'cards' for DIRECTORY agent cards.",
					},
					"topic": map[string]any{
						"type":        "string",
						"description": "Topic name (e.g. 'upgrade-readiness', 'blueprint', 'annotations') or full subject when action is 'read'.",
					},
				},
			},
		},
		{
			Name:        toolInspectPR,
			Description: "Fetch a GitHub Pull Request opened or referenced by kube-agents (metadata, changed files, unified diff) and save local .diff and per-file .patch files so Antigravity can open the PR and diffs natively in its viewer.",
			InputSchema: map[string]any{
				"type":     "object",
				"required": []string{"pr"},
				"properties": map[string]any{
					"pr": map[string]any{
						"type":        "string",
						"description": "GitHub PR URL (https://github.com/owner/repo/pull/123), shorthand (owner/repo#123), or PR number (#123, resolved against the deployment's configured githubRepo).",
					},
					"repo": map[string]any{
						"type":        "string",
						"description": "Optional owner/repo override if passing a bare PR number.",
					},
					"output_dir": map[string]any{
						"type":        "string",
						"description": "Optional local directory to write .diff and .patch files into.",
					},
				},
			},
		},
		{
			Name:        toolPollEvents,
			Description: "Drain pending asynchronous push events received from kube-agents (proactive chat.notify.* alerts/cron/kanban cards, completed background/delegated tasks, PR notices) and/or configure real-time reactive push wakeups via agentapi.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"agy_conversation_id": map[string]any{
						"type":        "string",
						"description": "Active Antigravity conversation UUID to register for reactive agentapi push notifications.",
					},
					"enable_push": map[string]any{
						"type":        "boolean",
						"description": "Enable or disable reactive background wakeup via agentapi send-message.",
					},
					"peek_only": map[string]any{
						"type":        "boolean",
						"description": "If true, view queued events without marking them drained.",
					},
				},
			},
		},
	}
}

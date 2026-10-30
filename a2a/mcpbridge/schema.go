package mcpbridge

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
		// Gated with task_control list (upstreamTaskListAndSteer): refused, not forwarded.
		txt, _, _, err := s.handleTaskControl(ctx, json.RawMessage(`{"action":"list"}`))
		return txt, err
	case resourceURIEvents:
		if s.host != nil {
			raw, _ := json.Marshal(map[string]any{"peek_only": true})
			return s.host.HandlePollEvents(ctx, s, raw)
		}
		return "", fmt.Errorf("unknown resource URI %q", uri)
	default:
		return "", fmt.Errorf("unknown resource URI %q", uri)
	}
}

func (s *Server) resourceDefinitions() []Resource {
	defs := []Resource{
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
	}
	if upstreamTaskListAndSteer {
		defs = append(defs, Resource{
			URI:         resourceURITasks,
			Name:        "Recent Kube-Agents Tasks",
			Description: "Recent and in-flight tasks on the TASKS JetStream stream.",
			MIMEType:    mimeTypeMarkdown,
		})
	}
	if s.host != nil {
		defs = append(defs, Resource{
			URI:         resourceURIEvents,
			Name:        "Pending Push Events",
			Description: "Unread asynchronous push notifications and task events from kube-agents.",
			MIMEType:    mimeTypeMarkdown,
		})
	}
	return defs
}

func (s *Server) toolDefinitions() []ToolDefinition {
	defs := []ToolDefinition{
		{
			Name:        ToolDiscoverDeployments,
			Description: "Discover remote kube-agents deployments across Kubernetes contexts in ~/.kube/config and list saved deployment profiles.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"kube_context": map[string]any{
						"type":        "string",
						"description": "Optional specific kubeconfig context to probe. Omit to probe all contexts in parallel.",
					},
				},
			},
		},
		{
			Name:        ToolConnectDeployment,
			Description: "Connect or switch to a remote kube-agents deployment (by name, cluster name, or kubeconfig context). Automatically manages the in-process port-forward to the in-cluster a2a-mcp listener and bearer token authentication.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"deployment": map[string]any{
						"type":        "string",
						"description": "Deployment name, cluster name, or kubeconfig context to connect to. Omit to connect to the default/active deployment.",
					},
					"kube_context": map[string]any{
						"type":        "string",
						"description": "Optional kubeconfig context override: an exact context name, or a case-insensitive part of exactly one. An override applies to this connection only: it is not saved, the active deployment does not change, and reconnects keep it until the next connect_deployment.",
					},
					"namespace": map[string]any{
						"type":        "string",
						"description": "Optional Kubernetes namespace override (default: the deployment's saved namespace). An override applies to this connection only: it is not saved, the active deployment does not change, and reconnects keep it until the next connect_deployment.",
					},
				},
			},
		},
		{
			Name:        ToolConnectionStatus,
			Description: "Inspect the current kube-agents connection, A2A v2 JetStream stream capacities (TASKS, DIRECTORY, TOPICS-STATE, TOPICS-JOURNAL), durable consumer health, registered agent cards, and active session state.",
			InputSchema: map[string]any{
				"type":       "object",
				"properties": map[string]any{},
			},
		},
		{
			Name:        ToolSendMessage,
			Description: "Send a prompt, question, task, or command to kube-agents over the A2A v2 JetStream bus and wait for the answer. A turn still running when the wait ends returns its task, to read later with task_control. The in-cluster server sends no progress while a turn runs yet: the answer shows when the turn ends.",
			InputSchema: map[string]any{
				"type":     "object",
				"required": []string{"message"},
				"properties": map[string]any{
					"message": map[string]any{
						"type":        "string",
						"description": "The prompt, question, or command (/session, /session off, delegate: ..., status, stop) to send to kube-agents.",
					},
					"mode": sendModeProperty(),
					"conversation": map[string]any{
						"type":        "string",
						"description": "Optional conversation alias or console token to send on (defaults to active conversation).",
					},
					"wait_timeout_seconds": map[string]any{
						"type":        "integer",
						"description": fmt.Sprintf("Seconds to wait for the answer before returning the still-running task to read later with task_control (default: %d, at most %d; larger values are clamped to %d).", defaultSendWaitSeconds, maxSendWaitSeconds, maxSendWaitSeconds),
					},
					"debug": map[string]any{
						"type":        "boolean",
						"description": "Optional debug mode (default: false). Set to true ONLY when the user explicitly asks to debug kube-agents itself (shows tool call tables, timings, and task metadata).",
					},
				},
			},
		},
		manageSessionDefinition(),
		taskControlDefinition(),
		{
			Name:        ToolReadTopics,
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
			Name:        ToolInspectPR,
			Description: "Fetch a GitHub Pull Request opened or referenced by kube-agents (metadata, changed files, unified diff) and save local .diff and per-file .patch files so the PR and diffs can be opened in a local viewer.",
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
	}
	if s.host != nil {
		defs = s.host.AugmentToolSchemas(defs)
	}
	return defs
}

// manageSessionDefinition is manage_session's schema. transcript and clear_killed, and the
// debug argument only transcript takes, are advertised only once the in-cluster server offers
// them (upstreamTranscriptAndClearKilled).
func manageSessionDefinition() ToolDefinition {
	description := "Manage kube-agents conversations and persistent /session worker pods: list conversations, create a new conversation, switch conversations, toggle /session worker mode on/off, delete a conversation/session, or toggle debug mode on/off."
	actions := []string{"list", "new", "switch", "session_on", "session_off", "delete", "debug_on", "debug_off"}
	aliasFor := "new/switch/session_on/session_off/delete"
	props := map[string]any{
		"description": map[string]any{
			"type":        "string",
			"description": "Optional human-readable description when creating or switching a conversation.",
		},
	}
	if upstreamTranscriptAndClearKilled {
		description = "Manage kube-agents conversations and persistent /session worker pods: list conversations, create a new conversation, switch conversations, toggle /session worker mode on/off, delete a conversation/session, clear all killed/terminated sessions, toggle debug mode on/off, or view the conversation transcript."
		actions = []string{"list", "new", "switch", "session_on", "session_off", "delete", "clear_killed", "debug_on", "debug_off", "transcript"}
		aliasFor = "new/switch/session_on/session_off/delete/transcript"
		props["debug"] = map[string]any{
			"type":        "boolean",
			"description": "Optional debug flag (default: false) to include tool call tables and timings when viewing a transcript.",
		}
	}
	props["action"] = map[string]any{
		"type":        "string",
		"enum":        actions,
		"description": "Session management action to perform.",
	}
	props["alias"] = map[string]any{
		"type":        "string",
		"description": "Conversation alias (e.g. 'upgrade-triage', 'default') for " + aliasFor + ".",
	}
	return ToolDefinition{
		Name:        ToolManageSession,
		Description: description,
		InputSchema: map[string]any{
			"type":       "object",
			"properties": props,
		},
	}
}

// sendModeProperty is send_message's mode argument. Only auto is advertised until the in-cluster
// server offers the other modes (upstreamSendModes).
func sendModeProperty() map[string]any {
	if upstreamSendModes {
		return map[string]any{
			"type":        "string",
			"enum":        []string{sendModeAuto, "platform", "session", "delegate"},
			"description": "Routing mode: 'auto' (use current conversation mode), 'platform' (route to Hermes platform agent), 'session' (ensure /session persistent Claude worker pod is active), or 'delegate' (delegate from session worker to platform agent and wait for wake synthesis).",
		}
	}
	return map[string]any{
		"type":        "string",
		"enum":        []string{sendModeAuto},
		"description": "Routing mode: 'auto' (the default) sends the turn on the conversation as it stands. Start a /session worker with manage_session session_on.",
	}
}

// taskControlDefinition is task_control's schema. The list, status and steer actions, and the
// arguments only they take, are advertised only once the in-cluster server offers them
// (upstreamTaskListAndSteer).
func taskControlDefinition() ToolDefinition {
	actions := []string{taskActionGet, taskActionStop}
	description := "Inspect or control tasks on the TASKS JetStream stream: get a task's state and answer (optionally waiting for it to finish), or stop/cancel a conversation's running task."
	props := map[string]any{
		"action": map[string]any{
			"type":        "string",
			"enum":        actions,
			"description": "Task control action to perform.",
		},
		"task_id": map[string]any{
			"type":        "string",
			"description": "Task ID to inspect when action is 'get'. With 'stop' and no conversation, the stop goes to the conversation that started this task (refused if this bridge is not tracking it).",
		},
		"conversation": map[string]any{
			"type":        "string",
			"description": "Conversation alias or token whose running task to stop when action is 'stop' (defaults to task_id's conversation when task_id is given, else the active conversation).",
		},
		"wait_timeout_seconds": map[string]any{
			"type":        "integer",
			"description": "Optional seconds to wait for an in-flight task to finish when action is 'get'.",
		},
		"debug": map[string]any{
			"type":        "boolean",
			"description": "Optional debug mode (default: false). Set to true ONLY when the user explicitly asks to debug kube-agents itself (shows tool call tables, timings, and task metadata).",
		},
	}
	if upstreamTaskListAndSteer {
		actions = []string{taskActionGet, actionList, taskActionStatus, taskActionSteer, taskActionStop}
		props["action"].(map[string]any)["enum"] = actions
		description = "Inspect or control tasks on the TASKS JetStream stream: get a task's full folded state (thinking, tool activities, progress, delegation chain, result), list recent/active tasks, trigger gateway status replay, steer an in-flight task, or stop/cancel a running task."
		props["message"] = map[string]any{
			"type":        "string",
			"description": "Steering instruction text when action is 'steer'.",
		}
		props["only_active"] = map[string]any{
			"type":        "boolean",
			"description": "When action is 'list', only return non-terminal in-flight tasks.",
		}
		props["limit"] = map[string]any{
			"type":        "integer",
			"description": "Maximum number of tasks to return when action is 'list' (default: 20).",
		}
	}
	return ToolDefinition{
		Name:        ToolTaskControl,
		Description: description,
		InputSchema: map[string]any{
			"type":       "object",
			"properties": props,
		},
	}
}

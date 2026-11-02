package mcpbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// upstreamTaskListAndSteer gates the task_control actions the in-cluster server (a2a/mcp) does
// not offer yet: list (and the kube-agents://tasks resource that sends it), status and steer.
// Its task_control answers get and stop only. While false, those actions are neither advertised
// nor forwarded, and a call to one is refused with errTaskActionNotSupported.
const upstreamTaskListAndSteer = false

// taskActionsAwaitingServer are the task_control actions upstreamTaskListAndSteer gates.
var taskActionsAwaitingServer = map[string]bool{actionList: true, taskActionStatus: true, taskActionSteer: true}

const (
	taskActionGet    = "get"
	taskActionStop   = "stop"
	taskActionCancel = "cancel"
	taskActionStatus = "status"
	taskActionSteer  = "steer"
)

// upstreamSendModes gates send_message's routing modes other than auto (platform, session and
// delegate). The in-cluster server's send_message has no mode: every turn goes on the
// conversation as it stands. While false, only auto is advertised, and a call naming another
// mode is refused with errSendModeNotSupported before the bridge connects.
const upstreamSendModes = false

const sendModeAuto = "auto"

// sendModesAwaitingServer are the send_message modes upstreamSendModes gates.
var sendModesAwaitingServer = map[string]bool{"platform": true, "session": true, "delegate": true}

func sendModeAwaitsServer(mode string) bool {
	return !upstreamSendModes && sendModesAwaitingServer[mode]
}

func errSendModeNotSupported(mode string) error {
	return fmt.Errorf("send_message mode %q is not supported by the in-cluster server yet; omit mode (or use auto), and start a /session worker with manage_session session_on", mode)
}

// upstreamTranscriptAndClearKilled gates the manage_session actions the in-cluster server
// refuses (a2a/mcp droppedActions): transcript, and clear_killed (prune). While false, they are
// neither advertised nor forwarded, and a call to one is refused with
// errSessionActionNotSupported.
const upstreamTranscriptAndClearKilled = false

// sessionActionsAwaitingServer are the manage_session actions upstreamTranscriptAndClearKilled
// gates.
var sessionActionsAwaitingServer = map[string]bool{actionTranscript: true, actionClearKilled: true, actionPruneAlias: true}

// upstreamDebug gates the debug argument of send_message and task_control, and manage_session's
// debug_on and debug_off: nothing reads them yet. The in-cluster server has no debug argument
// (a2a/mcp sendMessageArgs, taskControlArgs) and refuses debug_on/debug_off, and no bridge or
// HostIntegration code reads the flag debug_on would store. While false, none of them is
// advertised, a debug argument is dropped before forwarding, and debug_on/debug_off are refused
// with errSessionActionNotSupported. A host that reads the flag can advertise it again through
// AugmentToolSchemas and handle the actions in HandleManageSessionAction.
const upstreamDebug = false

// debugSessionActions are the manage_session actions upstreamDebug gates.
var debugSessionActions = map[string]bool{actionDebugOn: true, actionDebugOff: true}

func sessionActionAwaitsServer(action string) bool {
	return (!upstreamTranscriptAndClearKilled && sessionActionsAwaitingServer[action]) ||
		(!upstreamDebug && debugSessionActions[action])
}

func errSessionActionNotSupported(action string) error {
	usable := "list, new, switch, session_on, session_off or delete"
	if upstreamDebug {
		usable = "list, new, switch, session_on, session_off, delete, debug_on or debug_off"
	}
	return fmt.Errorf("manage_session %s is not supported by the in-cluster server yet; use %s, and read a task with task_control get", action, usable)
}

func taskActionAwaitsServer(action string) bool {
	return !upstreamTaskListAndSteer && taskActionsAwaitingServer[action]
}

func errTaskActionNotSupported(action string) error {
	return fmt.Errorf("task_control %s is not supported by the in-cluster server yet; use get (with task_id) or stop", action)
}

// moveArg renames the bridge's argument from to the in-cluster server's name to. The bridge's
// own name wins when a call carries both.
func moveArg(args map[string]any, from, to string) {
	v, ok := args[from]
	if !ok {
		return
	}
	delete(args, from)
	args[to] = v
}

// The bridge's send_message wait: its schema's default, and the in-cluster server's ceiling
// (a2a/mcp maxWaitSeconds). The server's own default (60s) never applies to a bridge call: the
// bridge always sends wait_seconds.
const (
	defaultSendWaitSeconds = 150
	maxSendWaitSeconds     = 300
)

// serverSendMessageArgs translates the bridge's send_message arguments to the in-cluster
// server's (a2a/mcp sendMessageArgs): message is text, wait_timeout_seconds is wait_seconds.
// An omitted or non-positive wait is the bridge's default, and one above the server's ceiling
// is that ceiling; a numeric string is read as its number, and any other value is an error
// rather than a silent default. mode is dropped (auto is the only mode the server has). The
// rest (conversation) keep their names; debug, which the server lacks, is dropped (upstreamDebug).
func serverSendMessageArgs(args map[string]any) (map[string]any, error) {
	moveArg(args, "message", "text")
	delete(args, "mode")
	if !upstreamDebug {
		delete(args, "debug")
	}
	moveArg(args, "wait_timeout_seconds", "wait_seconds")
	wait, err := parseWaitSeconds(args["wait_seconds"])
	if err != nil {
		return nil, err
	}
	switch {
	case wait <= 0:
		wait = defaultSendWaitSeconds
	case wait > maxSendWaitSeconds:
		wait = maxSendWaitSeconds
	}
	args["wait_seconds"] = int(wait)
	return args, nil
}

// parseWaitSeconds reads a wait_timeout_seconds value as send_message and task_control both
// take it: a JSON number, or a string holding one (some hosts quote the schema's integer);
// absent is 0. Any other value is an error naming the bridge's argument, not the server's.
func parseWaitSeconds(v any) (float64, error) {
	switch v := v.(type) {
	case nil:
		return 0, nil
	case float64:
		return v, nil
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil {
			return 0, fmt.Errorf("invalid wait_timeout_seconds %q: want a number of seconds", v)
		}
		return f, nil
	default:
		return 0, fmt.Errorf("invalid wait_timeout_seconds %v: want a number of seconds", v)
	}
}

// serverTaskControlArgs translates the bridge's task_control arguments to the in-cluster
// server's (a2a/mcp taskControlArgs): task_id is task, wait_timeout_seconds is wait_seconds,
// and the cancel action is stop. stopConversation, when set, names the conversation a stop
// is for, resolved from the bridge's alias or console token to the alias the server knows.
// The wait is read as send_message's is (parseWaitSeconds) and sent as whole seconds; a value
// that is not a number is an error.
func serverTaskControlArgs(raw json.RawMessage, stopConversation string) (json.RawMessage, error) {
	args := map[string]any{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &args)
	}
	if args == nil {
		args = map[string]any{}
	}
	moveArg(args, "task_id", "task")
	moveArg(args, "wait_timeout_seconds", "wait_seconds")
	if v, ok := args["wait_seconds"]; ok {
		wait, err := parseWaitSeconds(v)
		if err != nil {
			return nil, err
		}
		args["wait_seconds"] = int(wait)
	}
	if !upstreamDebug {
		delete(args, "debug")
	}
	act, _ := args["action"].(string)
	act = strings.ToLower(strings.TrimSpace(act))
	if act == taskActionCancel {
		act = taskActionStop
	}
	if act != "" {
		args["action"] = act
	}
	if act == taskActionStop && stopConversation != "" {
		args["conversation"] = stopConversation
	}
	return json.Marshal(args)
}

// upstreamArgs gates and translates a call to one of the in-cluster server's tools made in the
// bridge's argument names (CallRemoteTool): a mode or action the server does not offer yet is
// refused (send_message modes, task_control and manage_session actions), and the arguments are
// renamed as send_message and task_control rename theirs. A stop
// names the conversation alias the bridge resolves, as task_control's does.
func (s *Server) upstreamArgs(ctx context.Context, toolName string, rawArgs json.RawMessage) (json.RawMessage, error) {
	switch toolName {
	case ToolSendMessage:
		args := map[string]any{}
		if len(rawArgs) > 0 {
			_ = json.Unmarshal(rawArgs, &args)
		}
		if args == nil {
			args = map[string]any{}
		}
		mode, _ := args["mode"].(string)
		if mode = strings.ToLower(strings.TrimSpace(mode)); sendModeAwaitsServer(mode) {
			return nil, errSendModeNotSupported(mode)
		}
		fwd, err := serverSendMessageArgs(args)
		if err != nil {
			return nil, err
		}
		return json.Marshal(fwd)
	case ToolTaskControl:
		var args struct {
			Action       string `json:"action"`
			TaskID       string `json:"task_id"`
			Conversation string `json:"conversation"`
		}
		if len(rawArgs) > 0 {
			_ = json.Unmarshal(rawArgs, &args)
		}
		act := strings.ToLower(strings.TrimSpace(args.Action))
		if taskActionAwaitsServer(act) {
			return nil, errTaskActionNotSupported(act)
		}
		stopConversation := ""
		if act == taskActionStop || act == taskActionCancel {
			var err error
			if stopConversation, err = s.stopConversationAlias(ctx, args.Conversation, strings.TrimSpace(args.TaskID)); err != nil {
				return nil, err
			}
		}
		return serverTaskControlArgs(rawArgs, stopConversation)
	case ToolManageSession:
		var args struct {
			Action string `json:"action"`
		}
		if len(rawArgs) > 0 {
			_ = json.Unmarshal(rawArgs, &args)
		}
		if act := strings.ToLower(strings.TrimSpace(args.Action)); sessionActionAwaitsServer(act) {
			return nil, errSessionActionNotSupported(act)
		}
	}
	return rawArgs, nil
}

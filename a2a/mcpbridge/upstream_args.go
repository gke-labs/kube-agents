package mcpbridge

import (
	"encoding/json"
	"fmt"
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
// is that ceiling. mode is dropped (auto is the only mode the server has). The rest
// (conversation) keep their names; debug has no server counterpart and is ignored there.
func serverSendMessageArgs(args map[string]any) map[string]any {
	moveArg(args, "message", "text")
	delete(args, "mode")
	moveArg(args, "wait_timeout_seconds", "wait_seconds")
	wait, _ := args["wait_seconds"].(float64)
	switch {
	case wait <= 0:
		wait = defaultSendWaitSeconds
	case wait > maxSendWaitSeconds:
		wait = maxSendWaitSeconds
	}
	args["wait_seconds"] = int(wait)
	return args
}

// serverTaskControlArgs translates the bridge's task_control arguments to the in-cluster
// server's (a2a/mcp taskControlArgs): task_id is task, wait_timeout_seconds is wait_seconds,
// and the cancel action is stop. stopConversation, when set, names the conversation a stop
// is for, resolved from the bridge's alias or console token to the alias the server knows.
func serverTaskControlArgs(raw json.RawMessage, stopConversation string) json.RawMessage {
	args := map[string]any{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &args)
	}
	if args == nil {
		args = map[string]any{}
	}
	moveArg(args, "task_id", "task")
	moveArg(args, "wait_timeout_seconds", "wait_seconds")
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
	out, _ := json.Marshal(args)
	return out
}

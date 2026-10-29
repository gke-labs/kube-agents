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

// serverSendMessageArgs translates the bridge's send_message arguments to the in-cluster
// server's (a2a/mcp sendMessageArgs): message is text, wait_timeout_seconds is wait_seconds.
// The rest (conversation) keep their names; mode and debug have no server counterpart and
// are ignored there.
func serverSendMessageArgs(args map[string]any) map[string]any {
	moveArg(args, "message", "text")
	moveArg(args, "wait_timeout_seconds", "wait_seconds")
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

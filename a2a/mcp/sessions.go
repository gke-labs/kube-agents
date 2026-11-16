package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"
)

// ToolManageSession names, lists and forgets this server's conversations, and
// sends the console's control turns on one.
const ToolManageSession = "manage_session"

const (
	actionList       = "list"
	actionNew        = "new"
	actionDelete     = "delete"
	actionSessionOn  = "session_on"
	actionSessionOff = "session_off"
	actionStatus     = "status"
	actionHelp       = "help"

	// The console texts the control actions send, as a person would type
	// them. The gateway settles /session and /session off with a notice
	// (turnFate). help is a message to the agent, sent as any turn is: while
	// the conversation's turn is still running it waits for it to end
	// (SendTurn). sendAndWait takes a notice or a task either way.
	//
	// status is not offered (droppedActions): sent the same way it would
	// wait out a running turn rather than report its progress, and once
	// offered, clients' prompts would learn it. It comes back when it can
	// report progress (#3895).
	sessionOnText  = "/session"
	sessionOffText = "/session off"
	statusText     = "status"
	helpText       = "help"

	// newAliasFormat is new's alias when the caller names none: the Unix
	// second, with a -<n> suffix if another new took that second already.
	newAliasFormat      = "session-%d"
	newAliasSuffix      = "%s-%d"
	maxNewAliasAttempts = 100

	// maxDescriptionRunes caps a description; a longer one is refused, not
	// cut, so the caller knows what was kept.
	maxDescriptionRunes = 200
	// maxConversationsShown caps list's table; the rest are counted.
	maxConversationsShown = 50
	// controlWait is how long a control action waits for a task it opens:
	// control turns answer quickly, and a slow one is still readable with
	// task_control.
	controlWait = 20 * time.Second

	lastUsedLayout = time.RFC3339
)

// controlTexts maps each control action to the console text it sends.
var controlTexts = map[string]string{
	actionSessionOn:  sessionOnText,
	actionSessionOff: sessionOffText,
	actionHelp:       helpText,
}

// actionSynonyms are the original client's other spellings, accepted so its
// prompts keep working; they are not offered in the schema.
var actionSynonyms = map[string]string{
	"start":  actionSessionOn,
	"end":    actionSessionOff,
	"remove": actionDelete,
	"prune":  "clear_killed",
}

// droppedActions are the original client's manage_session actions this server
// does not offer. They are kept out of the schema but answered by name, since
// that client's prompts may still call them.
var droppedActions = map[string]string{
	"switch":       "not available in the in-cluster server: there is no active conversation; name it with `conversation` on each call",
	"debug_on":     "not available in the in-cluster server yet: per-conversation formatting comes with a later change",
	"debug_off":    "not available in the in-cluster server yet: per-conversation formatting comes with a later change",
	"transcript":   "not available in the in-cluster server yet: reading a conversation's history needs a cheaper bus read than a scan of every task; use task_control get for a task",
	"clear_killed": "not available in the in-cluster server: it cannot see session pods",
	actionStatus:   "not available in the in-cluster server yet: it would wait for the running turn to end rather than report its progress; read a task's state and progress with task_control get, by the task send_message reported",
}

type manageSessionArgs struct {
	Action            string `json:"action"`
	Conversation      string `json:"conversation"`
	ConversationToken string `json:"conversation_token"`
	Description       string `json:"description"`
	// Alias is the original client's name for conversation.
	Alias string `json:"alias"`
}

// ManageSessionTool lists, creates and forgets conversation aliases, and sends
// /session, /session off and help on a conversation.
func ManageSessionTool(bus TurnBus, convs *Conversations) Tool {
	return Tool{
		Definition: ToolDefinition{
			Name: ToolManageSession,
			Description: "Manage this server's named conversations: list them, create one (new), forget one (delete), " +
				"or send a control turn on one: start a /session worker (session_on), end it (session_off), " +
				"or send the agent the message help (like any message, it waits for the conversation's running turn to end first). " +
				"Names are this server's memory only; " +
				"a conversation resumes by its token after a restart.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"action": map[string]any{"type": "string", "description": "Default: " + actionList + ".", "enum": []string{
						actionList, actionNew, actionDelete, actionSessionOn, actionSessionOff, actionHelp,
					}},
					"conversation": map[string]any{"type": "string", "description": "The conversation's name. new: the name to create " +
						"(default session-<unix time>); delete: the name to forget (or its conversation's token); the control actions: the conversation to send on " +
						"(default: " + DefaultConversation + ")."},
					"conversation_token": map[string]any{"type": "string", "description": "The control actions and delete: the conversation, by token, instead of by name."},
					"description":        map[string]any{"type": "string", "description": fmt.Sprintf("new only: what the conversation is for, %d characters at most.", maxDescriptionRunes)},
				},
			},
		},
		Call: func(ctx context.Context, raw json.RawMessage) (string, error) {
			ctx, cancel := context.WithTimeout(ctx, MaxTurnWait)
			defer cancel()
			var a manageSessionArgs
			if err := json.Unmarshal(orEmptyObject(raw), &a); err != nil {
				return "", fmt.Errorf("arguments: %w", err)
			}
			// The original client's alias wins over conversation, as it did
			// there.
			if a.Alias != "" {
				a.Conversation = a.Alias
			}
			// As the original client had it: no action lists, and its other
			// spellings mean what they meant there.
			a.Action = strings.ToLower(strings.TrimSpace(a.Action))
			if a.Action == "" {
				a.Action = actionList
			}
			if canonical, ok := actionSynonyms[a.Action]; ok {
				a.Action = canonical
			}
			if text, ok := controlTexts[a.Action]; ok {
				return sendAndWait(ctx, bus, convs, a.Conversation, a.ConversationToken, text, controlWait)
			}
			if msg, ok := droppedActions[a.Action]; ok {
				return "", fmt.Errorf("%s: %s", a.Action, msg)
			}
			switch a.Action {
			case actionList:
				return formatConversations(convs.List()), nil
			case actionNew:
				return newConversation(convs, a)
			case actionDelete:
				return deleteConversation(convs, a)
			default:
				return "", fmt.Errorf("unknown action %s; use %s, %s, %s, %s, %s or %s", cell(a.Action),
					actionList, actionNew, actionDelete, actionSessionOn, actionSessionOff, actionHelp)
			}
		},
	}
}

func newConversation(convs *Conversations, a manageSessionArgs) (string, error) {
	if a.ConversationToken != "" {
		return "", errors.New("new mints its own token; to continue a conversation by its token, pass conversation_token to send_message")
	}
	if n := utf8.RuneCountInString(a.Description); n > maxDescriptionRunes {
		return "", fmt.Errorf("description is %d characters; at most %d", n, maxDescriptionRunes)
	}
	alias, token := strings.TrimSpace(a.Conversation), ""
	var err error
	if alias != "" {
		token, err = convs.Create(alias, a.Description)
	} else {
		base := fmt.Sprintf(newAliasFormat, convs.now().Unix())
		for i := 1; i <= maxNewAliasAttempts; i++ {
			alias = base
			if i > 1 {
				alias = fmt.Sprintf(newAliasSuffix, base, i)
			}
			if token, err = convs.Create(alias, a.Description); !errors.Is(err, ErrConversationExists) {
				break
			}
		}
	}
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("Created conversation %s (token `%s`). Name it with `conversation` on later calls, "+
		"or resume it by the token after this server restarts.\n", cell(alias), token), nil
}

func deleteConversation(convs *Conversations, a manageSessionArgs) (string, error) {
	// Trimmed as new trims it, so the name new made is the name forgotten.
	a.Conversation = strings.TrimSpace(a.Conversation)
	if a.Conversation == "" && a.ConversationToken == "" {
		return "", errors.New("delete needs the conversation's name, or its token")
	}
	name, ok := convs.Forget(a.Conversation, a.ConversationToken)
	if !ok {
		if a.ConversationToken != "" {
			return "", fmt.Errorf("no name here holds the conversation with token %s; delete forgets a name, and there is none to forget", cell(a.ConversationToken))
		}
		return "", fmt.Errorf("no conversation named %s", cell(a.Conversation))
	}
	return fmt.Sprintf("Forgot the name %s. This only forgets the name: the conversation's history stays on the bus, "+
		"and a /session worker for it idles out by itself (to end one now, stop its running task and send session_off first). "+
		"A later call with this name starts a new conversation.\n", cell(name)), nil
}

// formatConversations renders list's table. Names and descriptions are the
// callers' own, so they are shown as inert cells.
func formatConversations(convs []ConversationInfo) string {
	var sb strings.Builder
	sb.WriteString("## Conversations\n\n")
	sb.WriteString("These are this server's memory only: a restart forgets the names, and a conversation resumes by its token " +
		"(pass it as `conversation_token`).\n\n")
	if len(convs) == 0 {
		sb.WriteString("_No conversations yet._\n")
		return sb.String()
	}
	shown := convs
	if len(shown) > maxConversationsShown {
		shown = shown[:maxConversationsShown]
	}
	sb.WriteString("| Name | Token | Description | Last used |\n| :--- | :--- | :--- | :--- |\n")
	for _, c := range shown {
		fmt.Fprintf(&sb, "| %s | `%s%s` | %s | %s |\n", cell(c.Alias), consoleConversationPrefix, c.Token,
			cell(c.Description), c.LastUsed.UTC().Format(lastUsedLayout))
	}
	if omitted := len(convs) - len(shown); omitted > 0 {
		fmt.Fprintf(&sb, "\n(+%d older omitted)\n", omitted)
	}
	return sb.String()
}

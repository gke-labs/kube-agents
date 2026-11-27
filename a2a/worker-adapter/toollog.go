package workeradapter

// The per-tool log line of a profile task: one line on the adapter's log when
// a tool call starts and one when its result is in, with the task, the
// harness's id for the call, the tool's name, the status and, on the second,
// the time between the two as the adapter measured it. A call of the
// harness's skill tool also names its skill. The lines hold no tool input
// and no result, and they are not audit records: they are what the harness
// reported, which a process in the worker can omit or forge
// (docs/designs/profile-worker-hooks.md, "The per-tool record").
//
// A call does not always leave both lines. A call with no result event (the
// harness was stopped, canceled or timed out) leaves the first only. Events
// the adapter drains without handling, after an abandon or after it stops
// the harness at a write refusal, leave none.

import (
	"log/slog"
	"time"
)

const (
	// The two lines' messages. A reader finds the lines by these.
	toolCallStartedMsg = "tool call started"
	toolCallEndedMsg   = "tool call ended"

	// The values of the status key: the first line's, then the second
	// line's, from the result's error flag and never from its content.
	toolStatusStarted = "started"
	toolStatusOK      = "ok"
	toolStatusError   = "error"

	// The lines' keys. The task key is the one the adapter's other lines
	// use, and the call key the one the refused-call line uses
	// (refusal_log.go).
	toolLineTaskKey     = "task"
	toolLineCallKey     = "call"
	toolLineToolKey     = "tool"
	toolLineStatusKey   = "status"
	toolLineDurationKey = "durationMs"
	toolLineSkillKey    = "skill"

	// toolLineOtherSkill is the skill key's value for a skill call that
	// names no skill of the task's persona, so the key holds no free text.
	toolLineOtherSkill = "other"
	// toolLineFieldMax is the byte budget of the call id and of the tool's
	// name on a line: both are the harness's text, and a stdout line can be
	// megabytes long. A longer one is cut on a rune boundary inside the
	// budget and marked (truncate).
	toolLineFieldMax = 200
	// toolLogOpenMax bounds the calls waiting for a result. A call past it
	// still gets both lines; its second line has no duration.
	toolLogOpenMax = 1024
)

// ToolLines turns on the per-tool log line for a task (Config.ToolLines).
type ToolLines struct {
	// Skills are the names of the skills of the task's persona: the only
	// names the skill key carries.
	Skills []string
}

// toolLog writes one task's per-tool lines. A nil toolLog writes nothing.
// It is used from the adapter's event loop only.
type toolLog struct {
	log    *slog.Logger
	task   string
	skills map[string]bool
	// open is the calls whose result is not in yet, by call id.
	open map[string]openToolCall
}

type openToolCall struct {
	tool    string
	skill   string // the skill key's value, or "" for a tool that is not the skill tool
	started time.Time
}

// newToolLog is the per-tool log of task, or nil when lines is nil.
func newToolLog(log *slog.Logger, task string, lines *ToolLines) *toolLog {
	if lines == nil {
		return nil
	}
	skills := make(map[string]bool, len(lines.Skills))
	for _, name := range lines.Skills {
		skills[name] = true
	}
	return &toolLog{log: log, task: task, skills: skills, open: map[string]openToolCall{}}
}

// started writes the first line of a tool call (EventActivity) and keeps the
// call for its result. Only an id that fits the line uncut is kept: two
// longer ids can share the part that is left.
func (l *toolLog) started(ev Event) {
	if l == nil {
		return
	}
	call := openToolCall{tool: truncate(ev.Tool, toolLineFieldMax), started: ev.At}
	if ev.SkillCall {
		call.skill = toolLineOtherSkill
		if l.skills[ev.Skill] {
			call.skill = ev.Skill
		}
	}
	id := truncate(ev.CallID, toolLineFieldMax)
	if id != "" && id == ev.CallID && len(l.open) < toolLogOpenMax {
		l.open[id] = call
	}
	l.log.Info(toolCallStartedMsg, l.attrs(id, call, toolStatusStarted)...)
}

// ended writes the second line of a tool call (EventToolResult). A result
// whose call was not kept (no id, a cut id, an id that never started, a call
// past toolLogOpenMax) gets its line with no duration and with the tool's
// name only where the driver puts it on the result. A duration needs the
// read time of both events (Event.At).
func (l *toolLog) ended(ev Event) {
	if l == nil {
		return
	}
	// By the whole id: a kept id was never cut (started).
	call, known := l.open[ev.CallID]
	delete(l.open, ev.CallID)
	if !known {
		call = openToolCall{tool: truncate(ev.Tool, toolLineFieldMax)}
	}
	status := toolStatusOK
	if ev.Failed {
		status = toolStatusError
	}
	attrs := l.attrs(truncate(ev.CallID, toolLineFieldMax), call, status)
	if known && !call.started.IsZero() && !ev.At.IsZero() {
		attrs = append(attrs, toolLineDurationKey, ev.At.Sub(call.started).Milliseconds())
	}
	l.log.Info(toolCallEndedMsg, attrs...)
}

func (l *toolLog) attrs(id string, call openToolCall, status string) []any {
	attrs := []any{toolLineTaskKey, l.task, toolLineCallKey, id, toolLineToolKey, call.tool, toolLineStatusKey, status}
	if call.skill != "" {
		attrs = append(attrs, toolLineSkillKey, call.skill)
	}
	return attrs
}

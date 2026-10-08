package gateway

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// syncBuffer is a bytes.Buffer safe for the gateway's goroutines to write
// while the test reads it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// logRecords parses the JSON log (the handler cmd/gateway uses) into one map
// per line, keeping those whose msg is msg. A record that is not one line of
// valid JSON fails the test: that is the log-injection shape.
func logRecords(t *testing.T, logs *syncBuffer, msg string) []map[string]any {
	t.Helper()
	var out []map[string]any
	sc := bufio.NewScanner(strings.NewReader(logs.String()))
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		var rec map[string]any
		if err := json.Unmarshal(sc.Bytes(), &rec); err != nil {
			t.Fatalf("log line is not one JSON record: %v: %q", err, sc.Text())
		}
		if rec["msg"] == msg {
			out = append(out, rec)
		}
	}
	return out
}

// publishTerminal publishes a final status with message text on subject, from
// party: the executor's own on the events subject, the gateway's on the
// supervisor subject (an executor is not its own supervisor).
func publishTerminal(t *testing.T, r *rig, origin *lib.Envelope, party lib.Party, subject string, state lib.TaskState, text string) {
	t.Helper()
	update := lib.StatusUpdate{
		TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: state},
		Final:  true,
	}
	if text != "" {
		update.Status.Message = &lib.Message{Role: "agent", MessageID: "msg-terminal",
			Parts: []lib.Part{{Kind: "text", Text: text}}}
	}
	payload, err := json.Marshal(update)
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(party, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), subject, env); err != nil {
		t.Fatal(err)
	}
}

// Each terminal the relay delivers logs one "task terminal" line that joins
// its "ingress" line on taskId and names how the task ended: state, whose
// word (executor or supervisor) and the executor's reason token. Before this
// line the gateway logged a task going out and nothing about how it ended
// (#2406).
func TestTaskTerminalLogsOneLinePerTerminal(t *testing.T) {
	cases := []struct {
		name       string
		state      lib.TaskState
		supervisor bool
		message    string
		wantReason string
	}{
		{name: "completed", state: lib.StateCompleted, wantReason: ""},
		{name: "failed", state: lib.StateFailed,
			message: "reason: hermes-exited-nonzero - exit status 1; stdout tail: ; stderr tail: boom", wantReason: "hermes-exited-nonzero"},
		{name: "canceled", state: lib.StateCanceled, message: "reason: canceled-by-request", wantReason: "canceled-by-request"},
		{name: "rejected", state: lib.StateRejected,
			message: "reason: capability-refused - the verifier could not be reached", wantReason: "capability-refused"},
		{name: "failed-by-supervisor", state: lib.StateFailed, supervisor: true,
			message: "reason: bridge-died-without-terminal-event", wantReason: "bridge-died-without-terminal-event"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			logs := &syncBuffer{}
			r := startRigWithLogger(t, nil, slog.New(slog.NewJSONHandler(logs, nil)))
			conv := "discord:g1/thread-terminal-log-" + tc.name
			r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tl-1", Text: "start"}
			origin := r.awaitTask(t, "platform")
			exec := r.execFor(t, origin, "platform")
			if err := exec.PublishStatus(context.Background(), lib.StateSubmitted, false); err != nil {
				t.Fatal(err)
			}
			if tc.state == lib.StateCompleted {
				if err := exec.PublishArtifact(context.Background(), lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "done"}}}); err != nil {
					t.Fatal(err)
				}
			}
			party, subject, wantSource := lib.Party{Session: "platform"}, lib.TaskEventsSubject("platform", origin.TaskID), TerminalFromExecutor
			if tc.supervisor {
				party, subject, wantSource = gatewayParty, lib.TaskSupervisorSubject("platform", origin.TaskID), TerminalFromSupervisor
			}
			publishTerminal(t, r, origin, party, subject, tc.state, tc.message)

			waitFor(t, "the terminal log line", func() bool { return len(logRecords(t, logs, "task terminal")) > 0 })
			holdsAt(t, "task terminal lines", 1, func() int { return len(logRecords(t, logs, "task terminal")) })

			got := logRecords(t, logs, "task terminal")
			ingress := logRecords(t, logs, "ingress")
			if len(ingress) != 1 {
				t.Fatalf("want one ingress line, got %d", len(ingress))
			}
			line := got[0]
			want := map[string]any{
				"level":        "INFO",
				"taskId":       origin.TaskID,
				"conversation": conv,
				"addressee":    "platform",
				"state":        string(tc.state),
				"source":       string(wantSource),
				"reason":       tc.wantReason,
			}
			for k, v := range want {
				if line[k] != v {
					t.Errorf("%s = %v, want %v (line %v)", k, line[k], v, line)
				}
			}
			// The pairing the issue asks for: the same keys as ingress.
			for _, k := range []string{"taskId", "conversation", "addressee"} {
				if line[k] != ingress[0][k] {
					t.Errorf("%s does not join ingress: %v vs %v", k, line[k], ingress[0][k])
				}
			}
		})
	}
}

// The reason is the executor's, and the bus carries whatever a publisher
// wrote: a forged token with a newline must not start a second log record,
// and the detail (the bridge puts subprocess output there) must not reach
// the log at all.
func TestTaskTerminalLogKeepsOnlyASafeReasonToken(t *testing.T) {
	logs := &syncBuffer{}
	r := startRigWithLogger(t, nil, slog.New(slog.NewJSONHandler(logs, nil)))
	conv := "discord:g1/thread-terminal-log-forged"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tl-2", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	const forged = "reason: spawn-failed\n{\"level\":\"INFO\",\"msg\":\"task terminal\",\"state\":\"completed\"} - stderr tail: API_TOKEN=sk-live-0123456789"
	publishTerminal(t, r, origin, lib.Party{Session: "platform"}, lib.TaskEventsSubject("platform", origin.TaskID), lib.StateFailed, forged)

	waitFor(t, "the forged failure posts to chat", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "sk-live") {
				return true
			}
		}
		return false
	})
	waitFor(t, "the terminal log line", func() bool { return len(logRecords(t, logs, "task terminal")) > 0 })
	holdsAt(t, "task terminal lines", 1, func() int { return len(logRecords(t, logs, "task terminal")) })

	got := logRecords(t, logs, "task terminal")
	if got[0]["reason"] != "spawn-failed" || got[0]["state"] != string(lib.StateFailed) {
		t.Fatalf("line = %v, want reason spawn-failed and state failed", got[0])
	}
	if strings.Contains(logs.String(), "sk-live") {
		t.Fatalf("the reason's detail reached the log:\n%s", logs.String())
	}
}

func TestReasonToken(t *testing.T) {
	cases := map[string]string{
		"":                                      "",
		"no prefix at all":                      "",
		"Reason: spawn-failed":                  "",
		"reason: spawn-failed":                  "spawn-failed",
		"reason: spawn-failed - exec: boom":     "spawn-failed",
		"reason: bus-publish-failed at working": "bus-publish-failed",
		// The worker adapter's no-text-parts refusal, written as the bridge
		// writes it; it was prose once and logged as `no`.
		"reason: no-text-parts - the submission message carries nothing to execute": "no-text-parts",
		"reason: spawn-failed\nforged line":                                         "spawn-failed",
		"reason: spawn-failed\tx":                                                   "spawn-failed",
		"reason: ":                                                                  reasonTokenMalformed,
		"reason:  leading-space":                                                    reasonTokenMalformed,
		"reason: bad\"quote":                                                        reasonTokenMalformed,
		"reason: ünïcode":                                                           reasonTokenMalformed,
		"reason: " + strings.Repeat("a", reasonTokenCap):                            strings.Repeat("a", reasonTokenCap),
		"reason: " + strings.Repeat("a", reasonTokenCap+1):                          reasonTokenMalformed,
	}
	for in, want := range cases {
		if got := reasonToken(in); got != want {
			t.Errorf("reasonToken(%q) = %q, want %q", in, got, want)
		}
	}
}

// The logged addressee is the one the task was published to, which is what
// ingress logged, not the record's current one: after a Delegate re-home the
// record points somewhere else while the delegated task is still running, and
// its terminal must still join its ingress line.
func TestTaskTerminalLogNamesTheTasksOwnAddressee(t *testing.T) {
	logs := &syncBuffer{}
	r := startRigWithLogger(t, nil, slog.New(slog.NewJSONHandler(logs, nil)))
	conv := "discord:g1/thread-terminal-log-rehomed"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tl-3", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}

	// The re-home, as the record sees it: the standing addressee moves on
	// while the task's own ref keeps the addressee it was published to.
	// Under the session lock, so a relay batch still applying the submitted
	// status cannot write back the record it read before this edit.
	waitFor(t, "the submitted status applied", func() bool {
		rec, err := r.g.reg.Get(ctx, conv)
		return err == nil && rec != nil && rec.ActiveTask != nil && rec.ActiveTask.TaskID == origin.TaskID
	})
	l := r.g.lockSession(conv)
	l.Lock()
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		l.Unlock()
		t.Fatalf("no record after the ask: %+v err=%v", rec, err)
	}
	rec.Addressee = "chat-rehomed"
	err = r.g.reg.Put(ctx, rec)
	l.Unlock()
	if err != nil {
		t.Fatal(err)
	}

	publishTerminal(t, r, origin, lib.Party{Session: "platform"}, lib.TaskEventsSubject("platform", origin.TaskID), lib.StateFailed, "reason: spawn-failed")
	waitFor(t, "the terminal log line", func() bool { return len(logRecords(t, logs, "task terminal")) > 0 })

	got := logRecords(t, logs, "task terminal")[0]
	ingress := logRecords(t, logs, "ingress")[0]
	if got["addressee"] != "platform" || got["addressee"] != ingress["addressee"] {
		t.Fatalf("addressee = %v, want platform (ingress logged %v)", got["addressee"], ingress["addressee"])
	}
}

// terminalSettle is how long a count of "task terminal" lines must hold
// before a test calls it exact. Waiting on anything else first does not
// order the count: the relay retires the task route before it logs the
// line, and writes the session record back only after.
const terminalSettle = 300 * time.Millisecond

// holdsAt fails unless count() returns want on every poll for terminalSettle,
// so a second line written just after the first still fails the test.
func holdsAt(t *testing.T, what string, want int, count func() int) {
	t.Helper()
	deadline := time.Now().Add(terminalSettle)
	for {
		if n := count(); n != want {
			t.Fatalf("%s: got %d, want %d", what, n, want)
		}
		if time.Now().After(deadline) {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// terminalLinesFor is the "task terminal" lines that name taskID.
func terminalLinesFor(t *testing.T, logs *syncBuffer, taskID string) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range logRecords(t, logs, "task terminal") {
		if line["taskId"] == taskID {
			out = append(out, line)
		}
	}
	return out
}

// assertTerminalLine checks every field the line carries, so a dropped key
// fails as loudly as a wrong one.
func assertTerminalLine(t *testing.T, line map[string]any, want map[string]any) {
	t.Helper()
	for _, k := range []string{"level", "taskId", "conversation", "addressee", "state", "source", "reason"} {
		if _, ok := line[k]; !ok {
			t.Errorf("task terminal line has no %q key: %v", k, line)
		}
	}
	for k, v := range want {
		if line[k] != v {
			t.Errorf("%s = %v, want %v (line %v)", k, line[k], v, line)
		}
	}
}

// The terminals that never reach the relay log the same line: the heal of a
// terminal the relay missed, the heal of a task no executor took, and a
// submission that never reached the bus. Each of those paths calls
// logTaskTerminal, as the relay does, so an operator joining "ingress" to
// "task terminal" on taskId finds an outcome for each of these too (#2406,
// #2410 review).

// The stale-task heal: the relay missed a final the stream holds, and the
// next turn finds it and delivers it. One line, with the fold's state,
// whose word it was, and the reason token from the final message.
func TestTaskTerminalLogCoversTheStaleTaskHeal(t *testing.T) {
	logs := &syncBuffer{}
	r := startRigWithLogger(t, nil, slog.New(slog.NewJSONHandler(logs, nil)))
	conv := "discord:g1/thread-terminal-log-stale-heal"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "th-1", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	publishTerminal(t, r, origin, lib.Party{Session: "platform"}, lib.TaskEventsSubject("platform", origin.TaskID),
		lib.StateFailed, "reason: hermes-exited-nonzero - exit status 1")
	waitFor(t, "the relayed terminal's line", func() bool { return len(terminalLinesFor(t, logs, origin.TaskID)) >= 1 })
	// The relay writes the record back after it logs; the edit below must
	// land after that write, or the relay's would undo it.
	waitFor(t, "the relayed terminal written back", func() bool {
		rec, err := r.g.reg.Get(ctx, conv)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
	holdsAt(t, "the relayed terminal's lines", 1, func() int { return len(terminalLinesFor(t, logs, origin.TaskID)) })

	// The relay having missed the terminal: the active task restored, the
	// state a transient KV failure on the final event leaves.
	l := r.g.lockSession(conv)
	l.Lock()
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		l.Unlock()
		t.Fatalf("no record: %+v err=%v", rec, err)
	}
	rec.ActiveTask = &ActiveTask{TaskID: origin.TaskID, CorrelationID: origin.CorrelationID}
	err = r.g.reg.Put(ctx, rec)
	l.Unlock()
	if err != nil {
		t.Fatal(err)
	}

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "th-2", Text: "next"}
	waitFor(t, "the heal's terminal line", func() bool { return len(terminalLinesFor(t, logs, origin.TaskID)) >= 2 })
	waitFor(t, "the next turn's ingress", func() bool { return len(logRecords(t, logs, "ingress")) == 2 })

	got := terminalLinesFor(t, logs, origin.TaskID)
	if len(got) != 2 {
		t.Fatalf("want one line from the relay and one from the heal, got %d: %v", len(got), got)
	}
	assertTerminalLine(t, got[1], map[string]any{
		"level": "INFO", "taskId": origin.TaskID, "conversation": conv, "addressee": "platform",
		"state": string(lib.StateFailed), "source": string(TerminalFromExecutor), "reason": "hermes-exited-nonzero",
	})
}

// The never-started heal: a task older than the grace with nothing on its
// stream is released as the install's failure, and logged as one.
func TestTaskTerminalLogCoversTheNeverStartedHeal(t *testing.T) {
	logs := &syncBuffer{}
	r := startRigWithLogger(t, nil, slog.New(slog.NewJSONHandler(logs, nil)))
	conv := "discord:g1/thread-terminal-log-never-started"
	seedTasklessDelegate(t, r, conv, defaultFirstEventGrace+time.Minute)

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tn-1", Text: "anyone there?"}
	waitFor(t, "the heal's terminal line", func() bool { return len(terminalLinesFor(t, logs, "task-never")) > 0 })
	waitFor(t, "the next turn's ingress", func() bool { return len(logRecords(t, logs, "ingress")) == 1 })

	got := terminalLinesFor(t, logs, "task-never")
	if len(got) != 1 {
		t.Fatalf("want one task terminal line, got %d: %v", len(got), got)
	}
	assertTerminalLine(t, got[0], map[string]any{
		"level": "INFO", "taskId": "task-never", "conversation": conv, "addressee": "chat-otter-dead",
		"state": string(lib.StateFailed), "source": string(TerminalNeverStarted), "reason": "",
	})
}

// The publish failure: the task was announced and never reached the bus.
// Its ingress line is already written, so the line is its only outcome.
func TestTaskTerminalLogCoversThePublishFailure(t *testing.T) {
	logs := &syncBuffer{}
	r := startRigWithLogger(t, nil, slog.New(slog.NewJSONHandler(logs, nil)))
	deleteTasksStream(t, r.url)
	conv := "discord:g1/thread-terminal-log-no-bus"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tp-1", Text: "start"}

	waitFor(t, "the terminal log line", func() bool { return len(logRecords(t, logs, "task terminal")) > 0 })
	waitFor(t, "the failure edit", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "could not reach the bus") {
				return true
			}
		}
		return false
	})

	got := logRecords(t, logs, "task terminal")
	ingress := logRecords(t, logs, "ingress")
	if len(got) != 1 || len(ingress) != 1 {
		t.Fatalf("want one ingress and one task terminal line, got %d and %d: %v", len(ingress), len(got), got)
	}
	assertTerminalLine(t, got[0], map[string]any{
		"level": "INFO", "taskId": ingress[0]["taskId"], "conversation": conv, "addressee": "platform",
		"state": string(lib.StateFailed), "source": string(TerminalFromGateway), "reason": "",
	})
}

// textLogLines is the lines of a rig's text log whose msg is msg and that
// carry every key=value in kv.
func textLogLines(logs *lockedBuffer, msg string, kv ...string) []string {
	var out []string
	for _, line := range strings.Split(logs.String(), "\n") {
		if !strings.Contains(line, "msg="+msg+" ") && !strings.Contains(line, `msg="`+msg+`" `) {
			continue
		}
		fields := strings.Fields(line)
		all := true
		for _, want := range kv {
			found := false
			for _, f := range fields {
				if f == want {
					found = true
					break
				}
			}
			if !found {
				all = false
				break
			}
		}
		if all {
			out = append(out, line)
		}
	}
	return out
}

// A delegation chain is three tasks, each with its own ingress line: the
// turn that delegated, the child minted to platform, and the wake. Each
// logs its own terminal by its own id and addressee, though the observers
// hear of the chain only by its root (observedAs), so a join of "ingress"
// to "task terminal" on taskId finds an outcome for every one.
func TestTaskTerminalLogCoversEachTaskOfADelegationChain(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/t-terminal-log-chain"
	origin, session, child := delegated(t, r, spawn, conv, "")
	publishFinal(t, r, child, targetPlatform, lib.StateFailed, "reason: quota - exceeded")
	waitFor(t, "wake spawn", func() bool { return len(spawn.calls()) == 2 })
	wakeSession := spawn.calls()[1].Session
	wake := r.awaitTask(t, wakeSession)
	exec := r.execFor(t, wake, wakeSession)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	completeTask(t, exec, "the fleet could not be read")
	waitFor(t, "the wake's terminal line", func() bool {
		return len(textLogLines(r.logs, "task terminal", "taskId="+wake.TaskID)) > 0
	})

	for _, tc := range []struct {
		name string
		id   string
		kv   []string
	}{
		{"the delegating turn", origin.TaskID, []string{"addressee=" + session, "state=completed", "source=executor"}},
		{"the child", child.TaskID, []string{"addressee=" + targetPlatform, "state=failed", "source=executor", "reason=quota"}},
		{"the wake", wake.TaskID, []string{"addressee=" + wakeSession, "state=completed", "source=executor"}},
	} {
		if n := len(textLogLines(r.logs, "ingress", "taskId="+tc.id)); n != 1 {
			t.Errorf("%s: %d ingress lines, want 1", tc.name, n)
		}
		all := textLogLines(r.logs, "task terminal", "taskId="+tc.id)
		if len(all) != 1 {
			t.Errorf("%s: %d task terminal lines, want 1: %v", tc.name, len(all), all)
			continue
		}
		if got := textLogLines(r.logs, "task terminal", append([]string{"taskId=" + tc.id, "conversation=" + conv}, tc.kv...)...); len(got) != 1 {
			t.Errorf("%s: terminal line %q, want %v", tc.name, all[0], tc.kv)
		}
	}
}

// A session turn whose delegate request minted no child ends `completed` on
// its hand-off line, and toward the adapter and the read route it ends failed
// with the delegation reason (SessionRecord.handOffEnd). Its "task terminal"
// line reports that same end, not the raw `completed`, on the relay's path
// and on the stale heal's, or a count of terminals by state calls every
// refused turn a success (#2410 review).
func TestTaskTerminalLogReportsAHandOffsEnd(t *testing.T) {
	refuse := func(t *testing.T) func(*Config) {
		return func(c *Config) {
			armInjectMap(t, c)
			c.TargetAllowedUsers = map[string]map[string][]string{targetPlatform: {injectBackend: {"someone-else"}}}
		}
	}
	for _, tc := range []struct {
		name, conv, text, token string
		tweak                   func(t *testing.T) func(*Config)
		heal                    bool
	}{
		{"refused, relayed", injectKeyPrefix + "tl-handoff-refused", "x", reasonDelegationRefused, refuse, false},
		{"not started, relayed", injectKeyPrefix + "tl-handoff-ignored", " ", reasonDelegationNotStarted,
			func(t *testing.T) func(*Config) { return func(c *Config) { armInjectMap(t, c) } }, false},
		{"refused, healed", injectKeyPrefix + "tl-handoff-healed", "x", reasonDelegationRefused, refuse, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, spawn, obs := startObservedRig(t, tc.tweak(t))
			exec, origin, session := sessionTurnVia(t, r, spawn, tc.conv, injectBackend, "do a thing")
			if err := exec.PublishArtifact(context.Background(), delegateArtifact(t, targetPlatform, tc.text)); err != nil {
				t.Fatal(err)
			}
			waitFor(t, "the request handled", loggedContaining(r, "delegation", origin.TaskID))
			completeTask(t, exec, "delegated to platform")
			waitFor(t, "the turn's terminal", func() bool { _, ok := obs.terminalFor(origin.TaskID); return ok })
			end, _ := obs.terminalFor(origin.TaskID)
			if end.state != lib.StateFailed || reasonToken(end.text) != tc.token {
				t.Fatalf("observer's end = %+v, want failed with %s", end, tc.token)
			}
			want := []string{"taskId=" + origin.TaskID, "conversation=" + tc.conv, "addressee=" + session,
				"state=failed", "source=executor", "reason=" + tc.token}
			lines := 1
			if tc.heal {
				waitFor(t, "the turn released", func() bool {
					rec, _ := r.g.reg.Get(context.Background(), tc.conv)
					return rec != nil && rec.ActiveTask == nil
				})
				// The relay having missed the terminal, as in the stale
				// heal above: the next turn finds it on the stream.
				putRecord(t, r, tc.conv, func(rec *SessionRecord) {
					rec.ActiveTask = &ActiveTask{TaskID: origin.TaskID, CorrelationID: origin.CorrelationID}
				})
				r.adapter.inbox <- InboundMessage{Conversation: tc.conv, Kind: "group", AuthorID: "1001",
					MessageID: "tl-handoff-next", Text: "next", Backend: injectBackend}
				waitFor(t, "the heal", loggedContaining(r, "healing stale active task", origin.TaskID))
				lines = 2
			}
			waitFor(t, "the turn's terminal lines", func() bool {
				return len(textLogLines(r.logs, "task terminal", "taskId="+origin.TaskID)) >= lines
			})
			all := textLogLines(r.logs, "task terminal", "taskId="+origin.TaskID)
			if len(all) != lines {
				t.Fatalf("%d task terminal lines for the turn, want %d: %v", len(all), lines, all)
			}
			for _, line := range all {
				for _, kv := range want {
					if !strings.Contains(" "+line+" ", " "+kv+" ") {
						t.Errorf("task terminal line %q lacks %s: the adapter was told %+v", line, kv, end)
					}
				}
			}
		})
	}
}

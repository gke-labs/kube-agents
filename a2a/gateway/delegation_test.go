package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// delegateArtifact is the artifact the worker adapter publishes for a
// delegate tool call: the reserved name, one data part.
func delegateArtifact(t *testing.T, addressee, text string) lib.Artifact {
	t.Helper()
	data, err := json.Marshal(lib.DelegateRequest{Addressee: addressee, Text: text})
	if err != nil {
		t.Fatal(err)
	}
	return lib.Artifact{ArtifactID: "artifact-x-" + lib.ArtifactDelegate, Name: lib.ArtifactDelegate, Parts: []lib.Part{{Kind: "data", Data: data}}}
}

// sessionTurn opens a session-routed turn on conv and returns the spawned
// incarnation's executor, the turn's submission and the bus session.
func sessionTurn(t *testing.T, r *rig, spawn *fakeSpawner, conv, text string) (*lib.TaskExecution, *lib.Envelope, string) {
	t.Helper()
	before := len(spawn.calls())
	sessionRigTurn(r, conv, fmt.Sprintf("%s-%d", conv, before), "/session "+text)
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) > before })
	session := spawn.calls()[before].Session
	origin := r.awaitTask(t, session)
	exec := r.execFor(t, origin, session)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	return exec, origin, session
}

// loggedContaining waits on the rig's captured log for every needle on one
// line, which is how an ignored delegation is observable: it posts nothing
// and mints nothing.
func loggedContaining(r *rig, needles ...string) func() bool {
	return func() bool {
		for _, line := range strings.Split(r.logs.String(), "\n") {
			all := true
			for _, n := range needles {
				if !strings.Contains(line, n) {
					all = false
					break
				}
			}
			if all {
				return true
			}
		}
		return false
	}
}

// putRecord edits the stored record between turns, for the states a test
// cannot reach through the bus in one step (a released active task, an
// incarnation that moved on, a requester the ask bound cleared).
func putRecord(t *testing.T, r *rig, conv string, edit func(*SessionRecord)) {
	t.Helper()
	ctx := context.Background()
	l := r.g.lockSession(conv)
	l.Lock()
	defer l.Unlock()
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatalf("record: %v %v", rec, err)
	}
	edit(rec)
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
}

func platformSubmissions(t *testing.T, r *rig) int {
	t.Helper()
	n := 0
	for _, e := range inSubjectEnvelopes(t, r.url, targetPlatform) {
		if e.Kind == lib.KindMessage {
			n++
		}
	}
	return n
}

// TestADelegateArtifactMintsAChildToPlatform is the spec's first gateway
// test, with no allowlist configured (no list allows): the child carries the
// parent's correlationId, contextId and attribution plus a via, its root
// capability resolves for platform, it is the active task, the chain is on
// the record, and the parent's own terminal does not disturb it.
func TestADelegateArtifactMintsAChildToPlatform(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/thread-del"
	exec, origin, session := sessionTurn(t, r, spawn, conv, "how is the fleet?")
	before, _ := r.g.reg.Get(ctx, conv)
	parentLine := before.ActiveTask.StatusMsgID
	if parentLine == "" {
		t.Fatal("the parent turn has no rolling line")
	}
	if err := exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "report fleet health")); err != nil {
		t.Fatal(err)
	}
	child := r.awaitTask(t, targetPlatform)
	if child.TaskID == origin.TaskID {
		t.Fatal("the child reused the parent's taskId")
	}
	if child.ContextID != origin.ContextID || child.CorrelationID != origin.CorrelationID {
		t.Fatalf("child context/correlation = %s/%s, want %s/%s", child.ContextID, child.CorrelationID, origin.ContextID, origin.CorrelationID)
	}
	var m lib.Message
	if err := json.Unmarshal(child.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if got := joinTextParts(m.Parts); got != "report fleet health" {
		t.Fatalf("child text = %q", got)
	}
	var auth, parent Authority
	if err := json.Unmarshal(child.Authority, &auth); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(origin.Authority, &parent); err != nil {
		t.Fatal(err)
	}
	if auth.Requester != parent.Requester || auth.Audience.Conversation != parent.Audience.Conversation {
		t.Fatalf("child attribution %+v != parent %+v", auth, parent)
	}
	if auth.Via == nil || auth.Via.TaskID != origin.TaskID || auth.Via.Session != session {
		t.Fatalf("via = %+v, want task %s session %s", auth.Via, origin.TaskID, session)
	}
	if parent.Via != nil {
		t.Fatalf("the human turn carries a via: %+v", parent.Via)
	}
	assertRootCapability(t, r, auth, child.TaskID, targetPlatform)

	waitFor(t, "chain on the record", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		pref, _ := rec.TaskRefFor(origin.TaskID)
		return len(pref.Children) == 1
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != child.TaskID || rec.Addressee != targetPlatform || rec.BusSession != session {
		t.Fatalf("active=%+v addressee=%s busSession=%s", rec.ActiveTask, rec.Addressee, rec.BusSession)
	}
	pref, _ := rec.TaskRefFor(origin.TaskID)
	cref, _ := rec.TaskRefFor(child.TaskID)
	if pref.Children[0] != child.TaskID || cref.ParentTaskID != origin.TaskID || cref.Role != taskRoleChild || cref.Depth != 1 || cref.Addressee != targetPlatform {
		t.Fatalf("parent=%+v child=%+v", pref, cref)
	}
	if cref.Requester == nil || pref.Requester == nil || *cref.Requester != *pref.Requester {
		t.Fatalf("child requester %+v, want the parent's %+v", cref.Requester, pref.Requester)
	}
	if !r.g.targetAllows(targetPlatform, cref.Requester.Backend, cref.Requester.Subject) {
		t.Fatal("the inherited requester is not the one the check passed")
	}
	if !loggedContaining(r, "delegation requested", origin.TaskID, session, "addressee=platform", "depth=0")() {
		t.Fatalf("no receipt line for the request:\n%s", r.logs.String())
	}
	if !loggedContaining(r, "delegation minted", origin.TaskID, child.TaskID)() {
		t.Fatalf("no minted line:\n%s", r.logs.String())
	}

	// The child's rolling line says whose it is; a human turn's does not.
	if !postedContaining(r, "submitted… (delegated to platform)")() {
		t.Fatalf("the child's placeholder does not name the delegation: %v", r.adapter.postTexts())
	}
	cexec := r.execFor(t, child, targetPlatform)
	if err := cexec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the child's working line names the delegation", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "working") && strings.HasSuffix(e, "(delegated to platform)") {
				return true
			}
		}
		return false
	})

	// The parent's own terminal does not disturb the child as active task,
	// and it is task activity for the session.
	stamp := time.Now().UTC()
	_ = exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "a-r", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "delegated to platform"}}})
	_ = exec.PublishStatus(ctx, lib.StateCompleted, true)
	waitFor(t, "parent result relayed", func() bool {
		for _, p := range r.adapter.postTexts() {
			if p == "delegated to platform" {
				return true
			}
		}
		return false
	})
	waitFor(t, "parent terminal folded", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return !rec.LastTaskActivity.Before(stamp)
	})
	// The parent's own line closes on its terminal like any turn's, and the
	// child's is left alone.
	waitFor(t, "the parent's line reaches its completed line", func() bool {
		for _, e := range r.adapter.editsOf(parentLine) {
			if e == terminalLine(lib.StateCompleted, "") {
				return true
			}
		}
		return false
	})
	rec, _ = r.g.reg.Get(ctx, conv)
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != child.TaskID {
		t.Fatalf("parent terminal cleared the child: %+v", rec.ActiveTask)
	}
	for _, e := range r.adapter.editsOf(rec.ActiveTask.StatusMsgID) {
		if strings.Contains(e, "completed") {
			t.Fatalf("the parent's terminal edited the child's line: %q", e)
		}
	}
	if n := platformSubmissions(t, r); n != 1 {
		t.Fatalf("platform received %d submissions, want 1", n)
	}
}

// TestDelegateAllowlist: the check against the platform agent's list for the
// requester's backend. The rig's backend is discord and its author 1001.
func TestDelegateAllowlist(t *testing.T) {
	for _, tc := range []struct {
		name  string
		lists map[string][]string
		mint  bool
	}{
		{"no list for the backend allows", map[string][]string{gchatBackend: {"alice@example.com"}}, true},
		{"on the list mints", map[string][]string{"discord": {"1002", "1001"}}, true},
		{"off the list is refused", map[string][]string{"discord": {"1002"}}, false},
		{"a blank list is nobody", map[string][]string{"discord": {}}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, spawn := startRigWithSpawnerCap(t, "platform", 0, func(c *Config) {
				c.TargetAllowedUsers = map[string]map[string][]string{targetPlatform: tc.lists}
			})
			exec, origin, _ := sessionTurn(t, r, spawn, "discord:g1/t-list", "do a thing")
			if err := exec.PublishArtifact(context.Background(), delegateArtifact(t, "platform", "x")); err != nil {
				t.Fatal(err)
			}
			if tc.mint {
				r.awaitTask(t, targetPlatform)
				return
			}
			// The notice follows the delegating turn's terminal.
			waitFor(t, "refusal line", loggedContaining(r, "delegation refused", "rule=delegation.allowed-users"))
			_ = exec.PublishStatus(context.Background(), lib.StateCompleted, true)
			waitFor(t, "refusal", postedContaining(r, "🚫 not allowed to reach platform from here"))
			rec, _ := r.g.reg.Get(context.Background(), "discord:g1/t-list")
			pref, _ := rec.TaskRefFor(origin.TaskID)
			if !loggedContaining(r, "delegation refused", "rule=delegation.allowed-users", "backend=discord", "requester="+pref.Requester.Subject)() {
				t.Fatalf("no audit line with rule, backend and hashed requester:\n%s", r.logs.String())
			}
			if strings.Contains(r.logs.String(), "requester=1001") {
				t.Fatal("the audit line carries the plaintext author id")
			}
			if n := platformSubmissions(t, r); n != 0 {
				t.Fatalf("a child was minted for a refused requester: %d", n)
			}
		})
	}
}

// TestDelegateRefusalsWithANotice: the refusals the conversation is told
// about. Each mints nothing.
func TestDelegateRefusalsWithANotice(t *testing.T) {
	for _, tc := range []struct {
		name      string
		addressee string
		edit      func(rec *SessionRecord, parent string)
		notice    string
		rule      string
	}{
		{name: "addressee other than platform", addressee: "chat-other-1",
			notice: "⚠️ delegation refused: only platform can be delegated to today", rule: ruleDelegationTarget},
		{name: "depth at the bound", addressee: "platform",
			edit: func(rec *SessionRecord, parent string) {
				for i := range rec.Tasks {
					if rec.Tasks[i].ID == parent {
						rec.Tasks[i].Depth = defaultDelegationDepthMax
					}
				}
			},
			notice: "⚠️ delegation refused: this conversation has delegated as deep as it may (3)", rule: ruleDelegationDepth},
		{name: "no requester on record", addressee: "platform",
			edit: func(rec *SessionRecord, parent string) {
				for i := range rec.Tasks {
					if rec.Tasks[i].ID == parent {
						rec.Tasks[i].Requester, rec.Tasks[i].Attribution = nil, nil
					}
				}
			},
			notice: "⚠️ delegation refused: this turn's requester is no longer on record; ask again", rule: ruleDelegationNoRequester},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, spawn := startRigWithSpawner(t)
			conv := "discord:g1/t-refuse"
			exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
			if tc.edit != nil {
				putRecord(t, r, conv, func(rec *SessionRecord) { tc.edit(rec, origin.TaskID) })
			}
			if err := exec.PublishArtifact(context.Background(), delegateArtifact(t, tc.addressee, "x")); err != nil {
				t.Fatal(err)
			}
			waitFor(t, "refusal line", loggedContaining(r, "delegation refused", "rule="+tc.rule, origin.TaskID))
			_ = exec.PublishStatus(context.Background(), lib.StateCompleted, true)
			waitFor(t, "notice", postedContaining(r, tc.notice))
			if n := platformSubmissions(t, r); n != 0 {
				t.Fatalf("a refused request minted %d children", n)
			}
		})
	}
}

// TestADepthUnderTheBoundMints: the bound refuses at the bound, not one
// short of it.
func TestADepthUnderTheBoundMints(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/t-depth-ok"
	exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
	putRecord(t, r, conv, func(rec *SessionRecord) {
		for i := range rec.Tasks {
			if rec.Tasks[i].ID == origin.TaskID {
				rec.Tasks[i].Depth = defaultDelegationDepthMax - 1
			}
		}
	})
	_ = exec.PublishArtifact(context.Background(), delegateArtifact(t, "platform", "x"))
	child := r.awaitTask(t, targetPlatform)
	waitFor(t, "child depth", func() bool {
		rec, _ := r.g.reg.Get(context.Background(), conv)
		cref, ok := rec.TaskRefFor(child.TaskID)
		return ok && cref.Depth == defaultDelegationDepthMax
	})
}

// TestADelegateIsIgnoredAndLogged: the cases that mint nothing and tell the
// conversation nothing, each observable as its warning line.
func TestADelegateIsIgnoredAndLogged(t *testing.T) {
	for _, tc := range []struct {
		name string
		// edit runs on the stored record before the artifact is published.
		edit func(rec *SessionRecord, parent string)
		art  func(t *testing.T) lib.Artifact
		// from, when set, publishes as another session on its own subject.
		from string
		rule string
	}{
		{name: "not the active task",
			edit: func(rec *SessionRecord, _ string) { rec.ActiveTask = nil },
			rule: ruleDelegationStale},
		{name: "from a retired incarnation",
			edit: func(rec *SessionRecord, _ string) {
				rec.BusSession = "chat-moved-on-0000"
				rec.Addressee = rec.BusSession
			},
			rule: ruleDelegationStale},
		{name: "on another session's subject", from: "chat-imposter-0000", rule: ruleDelegationStale},
		{name: "over the text cap",
			art: func(t *testing.T) lib.Artifact {
				return delegateArtifact(t, "platform", strings.Repeat("x", lib.DelegateTextCap+1))
			},
			rule: ruleDelegationMalformed},
		{name: "blank text",
			art:  func(t *testing.T) lib.Artifact { return delegateArtifact(t, "platform", "  ") },
			rule: ruleDelegationMalformed},
		{name: "two parts",
			art: func(t *testing.T) lib.Artifact {
				a := delegateArtifact(t, "platform", "x")
				a.Parts = append(a.Parts, lib.Part{Kind: "text", Text: "y"})
				return a
			},
			rule: ruleDelegationMalformed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, spawn := startRigWithSpawner(t)
			conv := "discord:g1/t-ignore"
			exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
			if tc.edit != nil {
				putRecord(t, r, conv, func(rec *SessionRecord) { tc.edit(rec, origin.TaskID) })
			}
			art := delegateArtifact(t, "platform", "x")
			if tc.art != nil {
				art = tc.art(t)
			}
			posts := len(r.adapter.postTexts())
			if tc.from != "" {
				// The lib will not build an execution for a task addressed
				// elsewhere, so the envelope is put together by hand.
				payload, _ := json.Marshal(lib.ArtifactUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID, Artifact: art})
				env, err := lib.NewArtifactUpdateEnvelope(lib.Party{Session: tc.from, AgentType: "test-executor"}, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
				if err != nil {
					t.Fatal(err)
				}
				if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject(tc.from, origin.TaskID), env); err != nil {
					t.Fatal(err)
				}
			} else if err := exec.PublishArtifact(context.Background(), art); err != nil {
				t.Fatal(err)
			}
			waitFor(t, "ignore line", loggedContaining(r, "delegation ignored", "rule="+tc.rule, origin.TaskID, "backend=discord", "requester=hmac:"))
			if n := platformSubmissions(t, r); n != 0 {
				t.Fatalf("an ignored request minted %d children", n)
			}
			if got := r.adapter.postTexts()[posts:]; len(got) != 0 {
				t.Fatalf("an ignored request posted %v", got)
			}
		})
	}
}

// TestARepeatDelegateMintsNoSecondChild: one child at a time, and a repeat
// from the parent is ignored and logged, not refused with a notice.
func TestARepeatDelegateMintsNoSecondChild(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/t-busy"
	exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "first"))
	child := r.awaitTask(t, targetPlatform)
	posts := len(r.adapter.postTexts())
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "second"))
	waitFor(t, "busy line", loggedContaining(r, "delegation ignored", "rule="+ruleDelegationBusy, origin.TaskID, "child="+child.TaskID))
	if n := platformSubmissions(t, r); n != 1 {
		t.Fatalf("platform received %d submissions, want 1", n)
	}
	for _, p := range r.adapter.postTexts()[posts:] {
		if strings.Contains(p, "delegat") {
			t.Fatalf("a repeat posted a notice: %q", p)
		}
	}
}

// TestADelegateAfterTheTerminalMintsNothing: an artifact published after
// the parent's terminal finds the task's route retired and mints nothing.
// The next turn's working line is the barrier: the relay renders a
// conversation's events in order, so by the time it lands the late artifact
// has been dealt with.
func TestADelegateAfterTheTerminalMintsNothing(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/t-late"
	exec, _, _ := sessionTurn(t, r, spawn, conv, "x")
	_ = exec.PublishStatus(ctx, lib.StateCompleted, true)
	waitFor(t, "terminal", func() bool { rec, _ := r.g.reg.Get(ctx, conv); return rec != nil && rec.ActiveTask == nil })
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "late"))

	exec2, _, _ := sessionTurn(t, r, spawn, conv, "next")
	_ = exec2.PublishArtifact(ctx, lib.Artifact{ArtifactID: "p", Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "barrier"}}})
	_ = exec2.PublishStatus(ctx, lib.StateCompleted, true)
	waitFor(t, "barrier turn done", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return rec != nil && rec.ActiveTask == nil && len(rec.Tasks) == 2
	})
	if n := platformSubmissions(t, r); n != 0 {
		t.Fatalf("a late request minted %d children", n)
	}
}

// TestADelegateFromAFixedRoutePlatformTaskIsIgnored: platform may not
// delegate to itself; only the conversation's own session may ask.
func TestADelegateFromAFixedRoutePlatformTaskIsIgnored(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/t-fixed"
	sessionRigTurn(r, conv, "m", "hi")
	origin := r.awaitTask(t, targetPlatform)
	exec := r.execFor(t, origin, targetPlatform)
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "loop"))
	waitFor(t, "ignore line", loggedContaining(r, "delegation ignored", "rule="+ruleDelegationStale, origin.TaskID))
	if n := platformSubmissions(t, r); n != 1 {
		t.Fatalf("platform minted a child of itself: %d submissions", n)
	}
}

// TestTheSessionCannotDelegateForItsChild: the session's grants cover its own
// subjects for any task id, so it can publish on the child's id there. The
// child was addressed to platform, not to the session, so that is not a
// request from the conversation's session turn and mints nothing.
func TestTheSessionCannotDelegateForItsChild(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/t-forchild"
	exec, _, session := sessionTurn(t, r, spawn, conv, "x")
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "first"))
	child := r.awaitTask(t, targetPlatform)
	payload, _ := json.Marshal(lib.ArtifactUpdate{TaskID: child.TaskID, ContextID: child.ContextID, Artifact: delegateArtifact(t, "platform", "again")})
	env, err := lib.NewArtifactUpdateEnvelope(lib.Party{Session: session, AgentType: "test-executor"}, child.TaskID, child.ContextID, child.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(ctx, lib.TaskEventsSubject(session, child.TaskID), env); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "ignore line", loggedContaining(r, "delegation ignored", "rule="+ruleDelegationStale, child.TaskID))
	if n := platformSubmissions(t, r); n != 1 {
		t.Fatalf("platform received %d submissions, want 1", n)
	}
}

// editsOf is every edit the adapter received for one message, in order.
func (a *fakeAdapter) editsOf(messageID string) []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	var out []string
	for _, e := range a.edits {
		if e.MessageID == messageID {
			out = append(out, e.Text)
		}
	}
	return out
}

// postIndex is the position of the first post containing needle, or -1.
func postIndex(r *rig, needle string) int {
	for i, p := range r.adapter.postTexts() {
		if strings.Contains(p, needle) {
			return i
		}
	}
	return -1
}

// TestARefusalFollowsTheDelegatingTurnsAnswer: spec §3 - the human sees the
// session's "delegated to platform" and then the refusal, so the notice waits
// for the delegating turn's terminal, from the executor or the supervisor.
func TestARefusalFollowsTheDelegatingTurnsAnswer(t *testing.T) {
	const notice = "only platform can be delegated to today"
	for _, supervisor := range []bool{false, true} {
		name := "executor terminal"
		if supervisor {
			name = "supervisor terminal"
		}
		t.Run(name, func(t *testing.T) {
			r, spawn := startRigWithSpawner(t)
			ctx := context.Background()
			conv := "discord:g1/t-order"
			exec, origin, session := sessionTurn(t, r, spawn, conv, "x")
			_ = exec.PublishArtifact(ctx, delegateArtifact(t, "chat-other-1", "x"))
			waitFor(t, "refusal line", loggedContaining(r, "delegation refused", "rule="+ruleDelegationTarget))
			if i := postIndex(r, notice); i >= 0 {
				t.Fatalf("the notice was posted before the turn's answer: %v", r.adapter.postTexts())
			}
			if supervisor {
				if err := r.g.publishSupervisorTerminal(ctx, session, origin.TaskID, origin.ContextID, origin.CorrelationID, lib.StateFailed, "the pod died"); err != nil {
					t.Fatal(err)
				}
				waitFor(t, "notice", postedContaining(r, notice))
				if i, j := postIndex(r, "failed"), postIndex(r, notice); i < 0 || j < i {
					t.Fatalf("posts %v: want the failure, then the notice", r.adapter.postTexts())
				}
				return
			}
			_ = exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "a-r", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "delegated to platform"}}})
			_ = exec.PublishStatus(ctx, lib.StateCompleted, true)
			waitFor(t, "notice", postedContaining(r, notice))
			if i, j := postIndex(r, "delegated to platform"), postIndex(r, notice); i < 0 || j < i {
				t.Fatalf("posts %v: want the answer, then the notice", r.adapter.postTexts())
			}
		})
	}
}

// narrowTasksStream takes the platform agent's in subject off TASKS while
// leaving the events and supervisor subjects (the relay's) and the named
// session's in subject on it, so a submission to platform fails for real.
func narrowTasksStream(t *testing.T, url, session string) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := js.Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	cfg := stream.CachedInfo().Config
	cfg.Subjects = []string{"a2a.tasks.*.*.events", "a2a.tasks.*.*.supervisor", "a2a.tasks." + session + ".*.in"}
	if _, err := js.UpdateStream(ctx, cfg); err != nil {
		t.Fatal(err)
	}
}

// TestAChildThatCannotReachTheBusLeavesNoChain: a failed child publish
// leaves the parent the conversation's task with no child, no child entry in
// the history and no index entry for the child.
func TestAChildThatCannotReachTheBusLeavesNoChain(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/t-nobus"
	exec, origin, session := sessionTurn(t, r, spawn, conv, "x")
	narrowTasksStream(t, r.url, session)
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "x"))
	waitFor(t, "publish failure", loggedContaining(r, "task publish failed"))
	var childID string
	for _, line := range strings.Split(r.logs.String(), "\n") {
		if strings.Contains(line, "task publish failed") {
			for _, f := range strings.Fields(line) {
				if v, ok := strings.CutPrefix(f, "taskId="); ok {
					childID = v
				}
			}
		}
	}
	if childID == "" || childID == origin.TaskID {
		t.Fatalf("could not read the failed child's id: %q", childID)
	}
	waitFor(t, "record written", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		_, has := rec.TaskRefFor(childID)
		return !has
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID || rec.Addressee != session {
		t.Fatalf("active=%+v addressee=%s, want the parent on its session", rec.ActiveTask, rec.Addressee)
	}
	pref, _ := rec.TaskRefFor(origin.TaskID)
	if len(pref.Children) != 0 {
		t.Fatalf("the parent records a child that never reached the bus: %v", pref.Children)
	}
	for _, ref := range rec.Tasks {
		if ref.Role == taskRoleChild || ref.ParentTaskID != "" {
			t.Fatalf("a child entry survived the failed publish: %+v", ref)
		}
	}
	if key, err := r.g.reg.SessionForTask(ctx, childID); err != nil || key != "" {
		t.Fatalf("the failed child is still indexed: %q %v", key, err)
	}
}

// TestOneLiveChildPerConversation: decision 3 is per conversation. A stop
// detaches the child without ending it; a later turn's request is refused,
// after that turn's answer, naming the running child.
func TestOneLiveChildPerConversation(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	ctx := context.Background()
	conv := "discord:g1/t-onelive"
	exec, _, _ := sessionTurn(t, r, spawn, conv, "x")
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, "platform", "first"))
	child := r.awaitTask(t, targetPlatform)
	_ = exec.PublishStatus(ctx, lib.StateCompleted, true)
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "child detached", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == child.TaskID && rec.ActiveTask.Detached
	})

	exec2, origin2, _ := sessionTurn(t, r, spawn, conv, "again")
	_ = exec2.PublishArtifact(ctx, delegateArtifact(t, "platform", "second"))
	waitFor(t, "busy refusal", loggedContaining(r, "delegation refused", "rule="+ruleDelegationBusy, origin2.TaskID, "child="+child.TaskID))
	_ = exec2.PublishStatus(ctx, lib.StateCompleted, true)
	waitFor(t, "notice", postedContaining(r, "⚠️ delegation refused: a delegated task is still running (task "+child.TaskID+")"))
	if n := platformSubmissions(t, r); n != 1 {
		t.Fatalf("platform received %d submissions, want 1", n)
	}
	// Retiring the delegating turn's pod is not the child's end: the child
	// ran on platform, not in that pod, so no supervisor terminal is owed.
	if key, _ := r.g.reg.SessionForTask(ctx, child.TaskID); key != conv {
		t.Fatalf("the detached child was retired with the pod: index=%q", key)
	}
}

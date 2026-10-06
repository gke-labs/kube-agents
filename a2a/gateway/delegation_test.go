package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

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
	rec, _ = r.g.reg.Get(ctx, conv)
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != child.TaskID {
		t.Fatalf("parent terminal cleared the child: %+v", rec.ActiveTask)
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
			waitFor(t, "notice", postedContaining(r, tc.notice))
			if !loggedContaining(r, "delegation refused", "rule="+tc.rule, origin.TaskID)() {
				t.Fatalf("no refusal line for %s:\n%s", tc.rule, r.logs.String())
			}
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

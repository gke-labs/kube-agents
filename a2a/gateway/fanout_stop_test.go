package gateway

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// The tree-wide stop (#5682 PR 3): one stop cancels every live task of the
// conversation's tree, a stopped fan-out releases the conversation, and
// nothing from a canceled task reaches the room afterwards.

// cancelSentTo reports whether a cancel for taskID is on addressee's .in.
func cancelSentTo(t *testing.T, r *rig, addressee, taskID string) bool {
	t.Helper()
	for _, e := range inSubjectEnvelopes(t, r.url, addressee) {
		if e.Kind == lib.KindCancel && e.TaskID == taskID {
			return true
		}
	}
	return false
}

// countPosts counts the room's posts containing needle.
func countPosts(r *rig, needle string) int {
	n := 0
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, needle) {
			n++
		}
	}
	return n
}

// publishNote publishes a non-final working status carrying note, as an
// executor writes a status note, from the executor's party.
func publishNote(t *testing.T, r *rig, origin *lib.Envelope, addressee, note string) {
	t.Helper()
	payload, err := json.Marshal(lib.StatusUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateWorking,
			Message: &lib.Message{Role: "agent", MessageID: "msg-n-" + origin.TaskID, Parts: []lib.Part{{Kind: "text", Text: note}}}}})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: addressee, AgentType: "test-executor"}, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject(addressee, origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
}

// startStopRig is a fan-out rig at width 3 with the cluster profile, tweak
// applied after.
func startStopRig(t *testing.T, tweak func(*Config)) (*rig, *fakeSpawner, *recordingObserver) {
	t.Helper()
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r, spawn, obs := startObservedRig(t, func(c *Config) {
		c.DelegationWidthMax = 3
		c.ScopeFile = scope
		c.Namespace = "kubeagents-system"
		if tweak != nil {
			tweak(c)
		}
	})
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams})
	return r, spawn, obs
}

// TestAStopCancelsTheWholeFanOut: a stop while a fan-out of three profile
// tasks runs, one of them already ended, sends each live child its cancel on
// its own addressee's .in and its stop record, and marks it canceled; the
// ended child gets neither. The room reads one "cancel sent". The chain
// doesn't end until the stopped children have; then it ends once, canceled,
// and nothing wakes though a child completed.
func TestAStopCancelsTheWholeFanOut(t *testing.T) {
	t.Parallel()
	r, spawn, obs := startStopRig(t, nil)
	ctx := context.Background()
	conv := "discord:g1/t-stop-tree"
	f := fanOut(t, r, spawn, conv, clusterTask("web-1", "a"), clusterTask("web-2", "b"), clusterTask("web-3", "c"))
	completeTask(t, r.execFor(t, f.envs[0], "cluster"), "web-1 is green")
	childEndedLogged(t, r, f.kids[0].ID)

	sessionRigTurn(r, conv, "stop-1", "stop")
	for _, k := range f.kids[1:] {
		waitFor(t, "the cancel for "+k.ID, func() bool { return cancelSentTo(t, r, "cluster", k.ID) })
		if _, ok := dispatchStopFor(t, r.url, "cluster", k.ID); !ok {
			t.Fatalf("no stop record for %s", k.ID)
		}
	}
	waitFor(t, "every live child canceled on the record", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		for _, k := range f.kids[1:] {
			if ref, _ := rec.TaskRefFor(k.ID); !ref.Canceled {
				return false
			}
		}
		return true
	})
	if cancelSentTo(t, r, "cluster", f.kids[0].ID) {
		t.Fatal("the ended child was sent a cancel")
	}
	if _, ok := dispatchStopFor(t, r.url, "cluster", f.kids[0].ID); ok {
		t.Fatal("the ended child got a stop record")
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if ref, _ := rec.TaskRefFor(f.kids[0].ID); ref.Canceled {
		t.Fatal("the ended child is marked canceled")
	}
	if _, ok := obs.terminalFor(f.origin.TaskID); ok {
		t.Fatal("the chain ended before its stopped children did")
	}
	for _, i := range []int{1, 2} {
		_ = r.execFor(t, f.envs[i], "cluster").PublishStatus(ctx, lib.StateCanceled, true)
	}
	waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(f.origin.TaskID); return ok })
	if end, _ := obs.terminalFor(f.origin.TaskID); end.state != lib.StateCanceled {
		t.Fatalf("root terminal = %+v, want canceled", end)
	}
	if n := countPosts(r, "cancel sent"); n != 1 {
		t.Fatalf("%d cancel-sent posts, want 1: %q", n, r.adapter.postTexts())
	}
	if len(spawn.calls()) != 1 || wakeEntries(t, r, conv) != 0 {
		t.Fatal("a stopped fan-out woke the session")
	}
}

// tasksSubjects reads the TASKS stream's subjects, so a test that narrows
// them (narrowTasksStream) can put them back (setTasksSubjects).
func tasksSubjects(t *testing.T, url string) []string {
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
	return stream.CachedInfo().Config.Subjects
}

// setTasksSubjects sets the TASKS stream's subjects.
func setTasksSubjects(t *testing.T, url string, subjects []string) {
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
	cfg.Subjects = subjects
	if _, err := js.UpdateStream(ctx, cfg); err != nil {
		t.Fatal(err)
	}
}

// TestAStopWhoseChildCancelFailsHoldsUntilResent: a stop whose cancels to the
// children can't be published marks them canceled (their writes are
// dropped) but not as stopped: they may still run. So the batch doesn't end,
// the chain doesn't end, and a new delegation is refused naming one of them.
// A second stop re-sends the cancels; once they go out, the batch ends, the
// chain ends once, canceled, and the next delegation mints.
func TestAStopWhoseChildCancelFailsHoldsUntilResent(t *testing.T) {
	t.Parallel()
	r, spawn, obs := startStopRig(t, nil)
	ctx := context.Background()
	conv := "discord:g1/t-stop-tree-nocancel"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"))
	// platform's .in refuses, so the children's cancels fail; the parent's,
	// on its session's .in, goes.
	wide := tasksSubjects(t, r.url)
	narrowTasksStream(t, r.url, f.session)
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "the parent's cancel", postedContaining(r, "cancel sent"))
	waitFor(t, "the children marked canceled, their cancels unsent", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		for _, k := range f.kids {
			if ref, _ := rec.TaskRefFor(k.ID); !ref.Canceled || !ref.CancelUnsent || ref.BatchEnd != nil {
				return false
			}
		}
		return true
	})
	if _, ok := obs.terminalFor(f.origin.TaskID); ok {
		t.Fatalf("the chain ended while children whose cancel never went out may run: %v", obs.kinds())
	}

	// Each still counts as the conversation's live delegation, which is the
	// check a new delegation is refused on.
	rec, _ := r.g.reg.Get(ctx, conv)
	if live := r.g.liveChild(ctx, rec); live != f.kids[0].ID && live != f.kids[1].ID {
		t.Fatalf("liveChild = %q, want a child whose cancel never went out", live)
	}

	setTasksSubjects(t, r.url, wide)
	sessionRigTurn(r, conv, "stop-2", "stop")
	for _, k := range f.kids {
		waitFor(t, "the re-sent cancel for "+k.ID, func() bool { return cancelSentTo(t, r, targetPlatform, k.ID) })
	}
	for i := range f.kids {
		_ = r.execFor(t, f.envs[i], targetPlatform).PublishStatus(ctx, lib.StateCanceled, true)
	}
	waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(f.origin.TaskID); return ok })
	if end, _ := obs.terminalFor(f.origin.TaskID); end.state != lib.StateCanceled {
		t.Fatalf("root terminal = %+v, want canceled", end)
	}
	exec3, origin3, _ := sessionTurn(t, r, spawn, conv, "and again")
	_ = exec3.PublishArtifact(ctx, delegateArtifact(t, "platform", "next"))
	waitFor(t, "the next child on the record", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		pref, _ := rec.TaskRefFor(origin3.TaskID)
		return len(pref.Children) == 1
	})
	terminals := 0
	for _, e := range obs.events() {
		if e.task == f.origin.TaskID && e.kind == "terminal" {
			terminals++
		}
	}
	if terminals != 1 || wakeEntries(t, r, conv) != 0 {
		t.Fatalf("root terminals = %d, wakes = %d; want 1 and 0", terminals, wakeEntries(t, r, conv))
	}
}

// TestAStoppedFanOutReleasesTheConversation: a fan-out of three is stopped,
// two children confirm and one never starts. The one that never started
// holds the batch until the heal ends it past FirstEventGrace; then the
// chain ends once, canceled, nothing wakes, and the next delegation mints.
func TestAStoppedFanOutReleasesTheConversation(t *testing.T) {
	t.Parallel()
	r, spawn, obs := startStopRig(t, nil)
	ctx := context.Background()
	conv := "discord:g1/t-stop-release"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"), platformTask("three"))
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "the cancels", func() bool {
		return cancelSentTo(t, r, targetPlatform, f.kids[0].ID) && cancelSentTo(t, r, targetPlatform, f.kids[2].ID)
	})
	for _, i := range []int{0, 1} {
		_ = r.execFor(t, f.envs[i], targetPlatform).PublishStatus(ctx, lib.StateCanceled, true)
		childEndedLogged(t, r, f.kids[i].ID)
	}
	if _, ok := obs.terminalFor(f.origin.TaskID); ok {
		t.Fatal("the chain ended while a stopped child was still on the bus")
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if live := r.g.liveChild(ctx, rec); live != f.kids[2].ID {
		t.Fatalf("liveChild = %q, want the child that never started", live)
	}
	putRecord(t, r, conv, func(rec *SessionRecord) {
		for i := range rec.Tasks {
			if rec.Tasks[i].ID == f.kids[2].ID {
				rec.Tasks[i].StartedAt = time.Now().Add(-r.g.cfg.FirstEventGrace - time.Minute)
			}
		}
	})

	exec2, origin2, _ := sessionTurn(t, r, spawn, conv, "again")
	waitFor(t, "the root's terminal", func() bool { _, ok := obs.terminalFor(f.origin.TaskID); return ok })
	if end, _ := obs.terminalFor(f.origin.TaskID); end.state != lib.StateCanceled {
		t.Fatalf("root terminal = %+v, want canceled", end)
	}
	_ = exec2.PublishArtifact(ctx, delegateArtifact(t, "platform", "next"))
	next := awaitSubmission(t, r, targetPlatform, 3)
	waitFor(t, "the next child on the record", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		pref, _ := rec.TaskRefFor(origin2.TaskID)
		return len(pref.Children) == 1 && pref.Children[0] == next.TaskID
	})
	if loggedContaining(r, "delegation refused", "rule="+ruleDelegationBusy)() {
		t.Fatalf("the stopped fan-out refused the next delegation:\n%s", r.logs.String())
	}
	terminals := 0
	for _, e := range obs.events() {
		if e.task == f.origin.TaskID && e.kind == "terminal" {
			terminals++
		}
	}
	if terminals != 1 {
		t.Fatalf("root terminals = %d, want 1: %v", terminals, obs.kinds())
	}
	if wakeEntries(t, r, conv) != 0 {
		t.Fatal("a stopped fan-out woke the session")
	}
}

// TestTheHealEndsAStoppedChildThatNeverStarted: a stopped child with nothing
// on its stream past FirstEventGrace is ended by the next turn's heal: its
// route retired and its end logged, with nothing posted.
func TestTheHealEndsAStoppedChildThatNeverStarted(t *testing.T) {
	t.Parallel()
	r, spawn, _ := startStopRig(t, nil)
	ctx := context.Background()
	conv := "discord:g1/t-stop-heal"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"))
	_ = r.execFor(t, f.envs[0], targetPlatform).PublishStatus(ctx, lib.StateWorking, false)
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "the children canceled", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		ref, _ := rec.TaskRefFor(f.kids[1].ID)
		return ref.Canceled
	})
	putRecord(t, r, conv, func(rec *SessionRecord) {
		for i := range rec.Tasks {
			if rec.Tasks[i].ID == f.kids[1].ID {
				rec.Tasks[i].StartedAt = time.Now().Add(-r.g.cfg.FirstEventGrace - time.Minute)
			}
		}
	})
	posts := len(r.adapter.postTexts())
	sessionRigTurn(r, conv, "next-1", "status")
	waitFor(t, "the never-started child retired", func() bool {
		key, err := r.g.reg.SessionForTask(ctx, f.kids[1].ID)
		return err == nil && key == ""
	})
	waitFor(t, "its end logged", loggedContaining(r, "a stopped task never started", f.kids[1].ID))
	if key, _ := r.g.reg.SessionForTask(ctx, f.kids[0].ID); key != conv {
		t.Fatalf("the started child was retired: %q", key)
	}
	for _, p := range r.adapter.postTexts()[posts:] {
		if strings.Contains(p, f.kids[1].ID) {
			t.Fatalf("the heal posted about a stopped child: %q", p)
		}
	}
}

// lateWrites has a canceled task write everything a task can: a status note,
// a progress line, a notice, a question, and then a completed result.
func lateWrites(t *testing.T, r *rig, origin *lib.Envelope, addressee string) {
	t.Helper()
	ctx := context.Background()
	exec := r.execFor(t, origin, addressee)
	publishNote(t, r, origin, addressee, "late note")
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "p-1", Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "late progress"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "n-1", Name: lib.ArtifactNotice, Parts: []lib.Part{{Kind: "text", Text: "late notice"}}}); err != nil {
		t.Fatal(err)
	}
	publishQuestion(t, r, origin, addressee, "late question?")
	completeTask(t, exec, "late result")
}

// assertNothingLate fails on any post or line edit since posts and edits
// carrying a late write.
func assertNothingLate(t *testing.T, r *rig, posts, edits int) {
	t.Helper()
	for _, p := range r.adapter.postTexts()[posts:] {
		t.Fatalf("the room got a post after the stop: %q", p)
	}
	for _, e := range r.adapter.editTexts()[edits:] {
		for _, late := range []string{"late progress", "late note", "late question", "late result"} {
			if strings.Contains(e, late) {
				t.Fatalf("a line edit carries a late write: %q", e)
			}
		}
	}
}

// TestAStoppedChildWritesNothingLate: a single delegation stopped mid-run
// whose child then writes a note, a progress line, a notice, a question and
// a result: the room gets nothing after the stop's own "cancel sent", and
// the drops are logged.
func TestAStoppedChildWritesNothingLate(t *testing.T) {
	t.Parallel()
	r, spawn, _ := startObservedRig(t, nil)
	conv := "discord:g1/t-stop-late-single"
	_, _, child := delegated(t, r, spawn, conv, "")
	_ = r.execFor(t, child, targetPlatform).PublishStatus(context.Background(), lib.StateWorking, false)
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "cancel sent", postedContaining(r, "cancel sent"))
	posts, edits := len(r.adapter.postTexts()), len(r.adapter.editTexts())
	lateWrites(t, r, child, targetPlatform)
	waitFor(t, "the result dropped", loggedContaining(r, "dropped a canceled task's write", child.TaskID, "what=result"))
	assertNothingLate(t, r, posts, edits)
	for _, what := range []string{"status note", "progress", "notice", "question"} {
		if !loggedContaining(r, "dropped a canceled task's write", child.TaskID, "what=\""+what+"\"")() &&
			!loggedContaining(r, "dropped a canceled task's write", child.TaskID, "what="+what)() {
			t.Fatalf("no drop logged for the %s:\n%s", what, r.logs.String())
		}
	}
}

// TestAStoppedFanOutChildWritesNothingLate: the same for a fan-out child
// after the stop, and its result wakes nothing.
func TestAStoppedFanOutChildWritesNothingLate(t *testing.T) {
	t.Parallel()
	r, spawn, _ := startStopRig(t, nil)
	conv := "discord:g1/t-stop-late-fanout"
	f := fanOut(t, r, spawn, conv, platformTask("one"), platformTask("two"))
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "cancel sent", postedContaining(r, "cancel sent"))
	waitFor(t, "the cancels", func() bool {
		return cancelSentTo(t, r, targetPlatform, f.kids[0].ID) && cancelSentTo(t, r, targetPlatform, f.kids[1].ID)
	})
	posts, edits := len(r.adapter.postTexts()), len(r.adapter.editTexts())
	lateWrites(t, r, f.envs[0], targetPlatform)
	waitFor(t, "the result dropped", loggedContaining(r, "dropped a canceled task's write", f.kids[0].ID, "what=result"))
	publishFinal(t, r, f.envs[1], targetPlatform, lib.StateFailed, "late failure")
	waitFor(t, "the failure dropped", loggedContaining(r, "dropped a canceled task's write", f.kids[1].ID))
	assertNothingLate(t, r, posts, edits)
	if len(spawn.calls()) != 1 || wakeEntries(t, r, conv) != 0 {
		t.Fatal("a stopped fan-out child woke the session")
	}
}

package mcp

import (
	"context"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The gate-2 names (#5301): a session-shaped token a profile is named, and a
// profile-shaped token, with a live card, that a session task is addressed to.
const (
	cutoffSessionShaped = "cto-otter-1a2b"
	cutoffProfileShaped = "fleet-audit"
)

// dialCutoff is dial with the install's profile supervisor cutoff.
func dialCutoff(t *testing.T, url string, cutoff time.Time) *NATSBus {
	t.Helper()
	b, err := DialBus(context.Background(), BusConfig{
		URL: url, User: testUser, Password: testPass,
		Deployment:              Deployment{Agent: "platform-agent", Namespace: "kubeagents-system"},
		ProfileSupervisorCutoff: cutoff,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(b.Close)
	return b
}

// publishGatewayTerminal is a five-token supervisor terminal in the gateway's
// name: what a session task's supervisor writes, and what the cutoff must
// stop a profile task's reader from taking.
func publishGatewayTerminal(t *testing.T, js jetstream.JetStream, addressee, taskID string) {
	t.Helper()
	p := mustJSON(t, lib.StatusUpdate{TaskID: taskID, ContextID: "ctx-" + taskID,
		Status: lib.TaskStatus{State: lib.StateFailed}, Final: true})
	env, err := lib.NewStatusUpdateEnvelope(fakeGateway, taskID, "ctx-"+taskID, "corr-"+taskID, p)
	if err != nil {
		t.Fatal(err)
	}
	pub(t, js, lib.TaskSupervisorSubject(addressee, taskID), env)
}

// A noticed profile task's read doesn't end on a five-token supervisor
// terminal stored after the cutoff; an .in-submitted session task's does,
// under the same token. Each token is tried with both kinds, and the
// profile-shaped one has a live card, so it is how the task was learned that
// decides, not the name.
func TestAFiveTokenTerminalAfterTheCutoffDoesNotEndANoticedProfileTask(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, cutoffProfileShaped, map[string]any{"name": cutoffProfileShaped})
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	ctx := context.Background()
	for _, addressee := range []string{cutoffSessionShaped, cutoffProfileShaped} {
		profTask, sessTask := "task-prof", "task-sess"
		publishRun(t, js, addressee, profTask, "", false)
		publishGatewayTerminal(t, js, addressee, profTask)
		publishSubmission(t, js, addressee, sessTask, "console:x", "q", "")
		publishRun(t, js, addressee, sessTask, "", false)
		publishGatewayTerminal(t, js, addressee, sessTask)

		prof := TaskRef{Addressee: addressee, TaskID: profTask}
		b.kinds.note(prof, TaskKindProfile)
		task, err := b.ReadTask(ctx, prof)
		if err != nil {
			t.Fatal(err)
		}
		if task.Final || task.State != lib.StateWorking {
			t.Errorf("%s: the profile task ended on a five-token terminal after the cutoff: %s final=%v", prof, task.State, task.Final)
		}
		// A wait doesn't end on it either: it runs out with the task working.
		task, err = b.WaitForTask(ctx, prof, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		if task.Final {
			t.Errorf("%s: a wait ended on the five-token terminal", prof)
		}

		sess := TaskRef{Addressee: addressee, TaskID: sessTask}
		b.kinds.note(sess, TaskKindSession)
		task, err = b.WaitForTask(ctx, sess, 5*time.Second)
		if err != nil {
			t.Fatal(err)
		}
		if !task.Final || task.State != lib.StateFailed {
			t.Errorf("%s: the session task's supervisor terminal was not read: %s final=%v", sess, task.State, task.Final)
		}
	}
	if b.client.ProfileSupervisorDrops() == 0 {
		t.Error("no drop counted")
	}
}

// steerOrSubmission is an .in message for taskID under addressee in the
// gateway's name, as a new session task's submission and a steer into a
// running task both are (steerTask), via viaTask when it is set.
func steerOrSubmission(t *testing.T, addressee, taskID, conversation, text, viaTask, corr string) *nats.Msg {
	t.Helper()
	msg := mustJSON(t, lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: text}}, MessageID: "m-" + taskID})
	env, err := lib.NewMessageEnvelope(fakeGateway, taskID, "ctx-"+taskID, corr, msg, lib.WithAuthority(authorityFor(t, conversation, viaTask)))
	if err != nil {
		t.Fatal(err)
	}
	return &nats.Msg{Subject: lib.TaskInSubject(addressee, taskID), Data: mustJSON(t, env)}
}

// How the MCP server learns a task sets its kind, kept beside the reference:
// the gateway's status line names only a profile task, and a task delegated
// in the chain and submitted on .in is a session's or the platform agent's.
// A turn's own submission says nothing, since it can be a steer into a
// profile task, and neither does a typed reference.
func TestHowATaskIsLearnedSetsItsKind(t *testing.T) {
	kinds := &taskKinds{}
	const conv = "console:x"
	w := &turnWatch{addressed: true, messageID: "frame-1", conversation: conv, texts: []string{"q"},
		known: map[string]bool{}, subs: make(chan *nats.Msg, 1), stop: make(chan struct{}), kinds: kinds}
	noticed, ok := w.noticedTask(&nats.Msg{Data: mustJSON(t, ConsoleOutFrame{Text: "⏳ submitted", TaskID: "task-n",
		Addressee: cutoffSessionShaped, InReplyTo: "frame-1"})})
	if !ok || kinds.of(noticed) != TaskKindProfile {
		t.Errorf("noticed task = %+v, %v, kind %d; want a profile task", noticed, ok, kinds.of(noticed))
	}
	own, ok := w.ownSubmission(steerOrSubmission(t, cutoffProfileShaped, "task-s", conv, "q", "", "corr-turn"))
	if !ok || kinds.of(own) != TaskKindUnknown || own.Addressee != cutoffProfileShaped {
		t.Errorf("own submission = %+v, %v, kind %d; want its kind unknown", own, ok, kinds.of(own))
	}
	done := make(chan struct{})
	go func() { w.follow(); close(done) }()
	w.subs <- steerOrSubmission(t, cutoffProfileShaped, "task-child", conv, "go", "task-s", "corr-turn")
	deadline := time.Now().Add(2 * time.Second)
	for len(w.followedTasks()) < 1 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	close(w.stop)
	<-done
	if got := w.followedTasks(); len(got) != 1 || kinds.of(got[0]) != TaskKindSession {
		t.Errorf("followed %v; want task-child as a session task", got)
	}
	if typed, err := ParseTaskRef(cutoffProfileShaped+"/task-t", ""); err != nil || kinds.of(typed) != TaskKindUnknown {
		t.Errorf("a typed reference = %+v, %v; want its kind unknown", typed, err)
	}
}

// A reference names the same task however it was learned: a typed reference
// to the turn's root is the step state's root, on an unlatched install with
// the root's kind known, so the root's watch and branches are the same.
func TestATypedReferenceMatchesTheStoredStepReference(t *testing.T) {
	kinds := &taskKinds{}
	w := &turnWatch{addressed: true, messageID: "frame-1", known: map[string]bool{}, stop: make(chan struct{}), kinds: kinds}
	root, ok := w.noticedTask(&nats.Msg{Data: mustJSON(t, ConsoleOutFrame{Text: "⏳ submitted", TaskID: "task-root",
		Addressee: cutoffProfileShaped, InReplyTo: "frame-1"})})
	if !ok || kinds.of(root) != TaskKindProfile {
		t.Fatalf("noticed task = %+v, %v", root, ok)
	}
	typed, err := ParseTaskRef(root.String(), "")
	if err != nil {
		t.Fatal(err)
	}
	if typed != root {
		t.Errorf("typed %+v != stored %+v", typed, root)
	}
	convs := NewConversations()
	st := convs.initTaskStepState(root, "tok", DefaultConversation, false, nil, nil)
	st.mu.Lock()
	defer st.mu.Unlock()
	if !st.isUnsubmittedRootLocked(typed) {
		t.Error("a typed reference to the root is not the state's unsubmitted root")
	}
	st.followedRefs = []TaskRef{root}
	if !slices.Contains(st.syncFollowedTasksLocked(), typed) {
		t.Error("a typed reference does not match the followed reference")
	}
}

// A message the gateway steers into a running profile task arrives on the
// task's .in like a new session task's submission, and a turn takes it as its
// own. Under a profile with a session-shaped name and a live card, that task
// is read as the profile's after the cutoff: its five-token terminal is not
// taken.
func TestASteerIntoAProfileTaskDoesNotTakeAFiveTokenTerminal(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, cutoffSessionShaped, map[string]any{"name": cutoffSessionShaped})
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	const conv, taskID = "console:x", "task-steered"
	w := &turnWatch{conversation: conv, texts: []string{"and also this"}, known: map[string]bool{}, kinds: &b.kinds}
	ref, ok := w.ownSubmission(steerOrSubmission(t, cutoffSessionShaped, taskID, conv, "and also this", "", "corr-"+taskID))
	if !ok {
		t.Fatal("the turn did not take the steer as its own")
	}
	publishRun(t, js, cutoffSessionShaped, taskID, "", false)
	publishGatewayTerminal(t, js, cutoffSessionShaped, taskID)
	task, err := b.ReadTask(context.Background(), ref)
	if err != nil {
		t.Fatal(err)
	}
	if task.Final || task.State != lib.StateWorking {
		t.Errorf("the steered profile task ended on a five-token terminal after the cutoff: %s final=%v", task.State, task.Final)
	}
}

// A watch settles its task's kind on the ctx of the call that made it, so a
// watch kept past that call (a step poll's) reads a session task's terminal,
// not a cancelled directory read's "profile".
func TestAWatchKeptPastItsCallReadsASessionTasksTerminal(t *testing.T) {
	url, js := testBus(t)
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	const taskID = "task-kept"
	publishRun(t, js, cutoffSessionShaped, taskID, "", false)
	publishGatewayTerminal(t, js, cutoffSessionShaped, taskID)
	ref := TaskRef{Addressee: cutoffSessionShaped, TaskID: taskID}
	made, cancel := context.WithCancel(context.Background())
	w, err := b.watchTask(made, ref)
	cancel()
	if err != nil {
		t.Fatal(err)
	}
	defer w.close()
	ctx, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	for w.pending > 0 {
		if err := w.next(ctx); err != nil {
			t.Fatal(err)
		}
	}
	task, err := w.fold()
	if err != nil {
		t.Fatal(err)
	}
	if !task.Final || task.State != lib.StateFailed {
		t.Errorf("the session task read through a kept watch: %s final=%v, want its supervisor's terminal", task.State, task.Final)
	}
}

// A directory read that fails decides nothing: the five-token terminal is held
// unread, and once the directory answers, the same watch's next read places
// it, so a session task's terminal is read.
func TestAFailedDirectoryReadIsNotKept(t *testing.T) {
	url, js := testBus(t)
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	ctx, stop := context.WithTimeout(context.Background(), 10*time.Second)
	defer stop()
	dir, err := js.Stream(ctx, streamDirectory)
	if err != nil {
		t.Fatal(err)
	}
	cfg := dir.CachedInfo().Config
	if err := js.DeleteStream(ctx, streamDirectory); err != nil {
		t.Fatal(err)
	}
	const taskID = "task-doubted"
	publishRun(t, js, cutoffSessionShaped, taskID, "", false)
	publishGatewayTerminal(t, js, cutoffSessionShaped, taskID)
	ref := TaskRef{Addressee: cutoffSessionShaped, TaskID: taskID}

	if _, err := b.ReadTask(ctx, ref); err != nil {
		t.Fatal(err)
	}
	w, err := b.watchTask(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	defer w.close()
	for w.pending > 0 {
		if err := w.next(ctx); err != nil {
			t.Fatal(err)
		}
	}
	if task, err := w.fold(); err != nil || task.Final {
		t.Fatalf("with no directory, the read = %+v, %v; want the terminal held unread", task, err)
	}
	if len(w.doubted) != 1 || w.dropped != 0 {
		t.Fatalf("held %d, dropped %d; want the terminal held, not dropped", len(w.doubted), w.dropped)
	}

	if _, err := js.CreateStream(ctx, cfg); err != nil {
		t.Fatal(err)
	}
	if err := w.nextWait(ctx, minFetchWait); err != nil && ctx.Err() != nil {
		t.Fatal(err)
	}
	task, err := w.fold()
	if err != nil {
		t.Fatal(err)
	}
	if !task.Final || task.State != lib.StateFailed {
		t.Errorf("once the directory answered: %s final=%v, want the session task's terminal", task.State, task.Final)
	}
	if b.client.ProfileSupervisorDrops() != 0 {
		t.Errorf("%d drops counted for a session task", b.client.ProfileSupervisorDrops())
	}
}

// Planner call A (#5301): a reference typed into task_control get, to a task
// this server learned nothing of, has no kind, so an addressee with a live
// agent card is read as a profile. The
// residue, pinned here: an old session task whose name a profile has since
// taken reads as unfinished once the cutoff is set and its gateway terminal
// is stored after it. Without the card it reads as ended, as before.
func TestAnOldSessionTaskNamedLikeANewProfileReadsAsUnfinishedInGet(t *testing.T) {
	url, js := testBus(t)
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	convs := NewConversations()
	s := mustServer(t, TaskControlTool(b, convs))
	for _, addressee := range []string{cutoffProfileShaped, cutoffSessionShaped} {
		const taskID = "task-old"
		publishSubmission(t, js, addressee, taskID, "console:x", "q", "")
		publishRun(t, js, addressee, taskID, "", false)
		publishGatewayTerminal(t, js, addressee, taskID)
	}
	publishCard(t, js, cutoffProfileShaped, map[string]any{"name": cutoffProfileShaped})

	got := callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task": cutoffProfileShaped + "/task-old"})
	if got.IsError || !strings.Contains(got.Content[0].Text, "**working** (still running") {
		t.Errorf("get of the old session task under a profile's name = %+v, want it unfinished", got)
	}
	got = callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task": cutoffSessionShaped + "/task-old"})
	if got.IsError || !strings.Contains(got.Content[0].Text, "**failed**") || strings.Contains(got.Content[0].Text, "still running") {
		t.Errorf("get of a session task with no card under its name = %+v, want it ended", got)
	}
	// The session-kind row: a task this server learned as a session's (a
	// turn's delegated task) reads as one when typed, card or not.
	b.kinds.note(TaskRef{Addressee: cutoffProfileShaped, TaskID: "task-old"}, TaskKindSession)
	got = callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task": cutoffProfileShaped + "/task-old"})
	if got.IsError || !strings.Contains(got.Content[0].Text, "**failed**") || strings.Contains(got.Content[0].Text, "still running") {
		t.Errorf("get of a typed reference to a task learned as a session's = %+v, want it ended", got)
	}
}

// The swarm read applies the same rule: a task it knows only from the stream
// is a profile's when its addressee has a live card, and its five-token
// terminal after the cutoff is not folded.
func TestTheSwarmReadDropsAFiveTokenTerminalAfterTheCutoffForAProfile(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, cutoffProfileShaped, map[string]any{"name": cutoffProfileShaped})
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	for _, addressee := range []string{cutoffProfileShaped, cutoffSessionShaped} {
		publishSubmission(t, js, addressee, "task-sw", "console:x", "q", "")
		publishRun(t, js, addressee, "task-sw", "", false)
		publishGatewayTerminal(t, js, addressee, "task-sw")
	}
	read, err := b.swarmTasks(context.Background(), time.Now().Add(-time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	final := map[string]bool{}
	for _, task := range read.tasks {
		if task.Task != nil {
			final[task.Addressee] = task.Task.Final
		}
	}
	if f, ok := final[cutoffProfileShaped]; !ok || f {
		t.Errorf("the profile's task folded final=%v (seen %v), want unfinished", f, ok)
	}
	if f, ok := final[cutoffSessionShaped]; !ok || !f {
		t.Errorf("the session's task folded final=%v (seen %v), want ended", f, ok)
	}
	// The session row drops nothing: the one drop is the profile's.
	if n := b.client.ProfileSupervisorDrops(); n != 1 {
		t.Errorf("the swarm read counted %d drops, want the profile's one", n)
	}
}

// forget drops every kind recorded, for a test that tries one reference
// under each kind.
func (k *taskKinds) forget() {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.kinds, k.order = nil, nil
}

// Unlatched, every read is what it was before the cutoff existed: the same
// fold whatever kind the reference carries and whether or not the addressee
// has a card, with no directory read and nothing counted.
func TestAnUnlatchedServerReadsBothFormsAsBefore(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, cutoffProfileShaped, map[string]any{"name": cutoffProfileShaped})
	b := dial(t, url)
	ctx := context.Background()
	for _, addressee := range []string{cutoffProfileShaped, cutoffSessionShaped} {
		publishSubmission(t, js, addressee, "task-u", "console:x", "q", "")
		publishRun(t, js, addressee, "task-u", "", false)
		publishGatewayTerminal(t, js, addressee, "task-u")
		var first *lib.Task
		for _, kind := range []TaskKind{TaskKindUnknown, TaskKindProfile, TaskKindSession} {
			ref := TaskRef{Addressee: addressee, TaskID: "task-u"}
			b.kinds.forget()
			b.kinds.note(ref, kind)
			task, err := b.ReadTask(ctx, ref)
			if err != nil {
				t.Fatal(err)
			}
			if !task.Final || task.State != lib.StateFailed {
				t.Errorf("%s kind %d: %s final=%v, want the supervisor's terminal", addressee, kind, task.State, task.Final)
			}
			if first == nil {
				first = task
			} else if !reflect.DeepEqual(task, first) {
				t.Errorf("%s kind %d: the fold differs by kind:\n got %+v\nwant %+v", addressee, kind, task, first)
			}
		}
	}
	if n := directoryConsumers(t, js); n != 0 {
		t.Errorf("an unlatched read opened %d DIRECTORY consumers", n)
	}
	if b.client.ProfileSupervisorDrops() != 0 {
		t.Errorf("an unlatched server counted %d drops", b.client.ProfileSupervisorDrops())
	}
}

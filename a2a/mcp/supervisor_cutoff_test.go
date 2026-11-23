package mcp

import (
	"context"
	"reflect"
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

		prof := TaskRef{Addressee: addressee, TaskID: profTask, Kind: TaskKindProfile}
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

		sess := TaskRef{Addressee: addressee, TaskID: sessTask, Kind: TaskKindSession}
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

// How the MCP server learns a task sets its kind: the gateway's status line
// names only a profile task, and a submission on .in is a session's or the
// platform agent's.
func TestHowATaskIsLearnedSetsItsKind(t *testing.T) {
	w := &turnWatch{addressed: true, messageID: "frame-1", known: map[string]bool{}}
	noticed, ok := w.noticedTask(&nats.Msg{Data: mustJSON(t, ConsoleOutFrame{Text: "⏳ submitted", TaskID: "task-n",
		Addressee: cutoffSessionShaped, InReplyTo: "frame-1"})})
	if !ok || noticed.Kind != TaskKindProfile {
		t.Errorf("noticed task = %+v, %v; want a profile task", noticed, ok)
	}
	msg := mustJSON(t, lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "q"}}, MessageID: "m-task-s"})
	env, err := lib.NewMessageEnvelope(fakeGateway, "task-s", "ctx-task-s", "corr-task-s", msg, lib.WithAuthority(authorityFor(t, "console:x", "")))
	if err != nil {
		t.Fatal(err)
	}
	submitted, _, _, ok := submission(&nats.Msg{Subject: lib.TaskInSubject(cutoffProfileShaped, "task-s"), Data: mustJSON(t, env)})
	if !ok || submitted.Kind != TaskKindSession || submitted.Addressee != cutoffProfileShaped {
		t.Errorf("submitted task = %+v, %v; want a session task", submitted, ok)
	}
	if typed, err := ParseTaskRef(cutoffProfileShaped+"/task-t", ""); err != nil || typed.Kind != TaskKindUnknown {
		t.Errorf("a typed reference = %+v, %v; want its kind unknown", typed, err)
	}
}

// Planner call A (#5301): a reference typed into task_control get carries no
// kind, so an addressee with a live agent card is read as a profile. The
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
			task, err := b.ReadTask(ctx, TaskRef{Addressee: addressee, TaskID: "task-u", Kind: kind})
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

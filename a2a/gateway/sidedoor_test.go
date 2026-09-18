package gateway

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The side door beside a real backend. The guard lets the two coexist
// (config_test.go); these are about what happens when they do, which is that
// every reply has to go back out the ingress it came in through. A post that
// crossed would put an eval's answer in a customer's chat room, or a
// customer's answer in a transcript the harness grades.

const sideDoorTestToken = "side-door-token"

// newSideDoorRig pairs a fake chat backend with a real inject door, and hands
// back both halves plus the composite the gateway would drive.
func newSideDoorRig(t *testing.T) (*fakeAdapter, *InjectAdapter, Adapter) {
	t.Helper()
	primary := newFakeAdapter()
	door, err := NewInjectAdapter("127.0.0.1:0", sideDoorTestToken, time.Minute, nil)
	if err != nil {
		t.Fatal(err)
	}
	return primary, door, WithSideDoor(primary, door, nil)
}

// TestSideDoorRoutesEveryReplyByItsConversation: the key is what decides, and
// backend-qualified keys are what make that safe -- a synthetic conversation
// is spelled inject:... and a real one is not.
func TestSideDoorRoutesEveryReplyByItsConversation(t *testing.T) {
	primary, door, composite := newSideDoorRig(t)

	chatKey := "discord:g1/thread-1"
	doorKey := injectKeyPrefix + "case-1"

	chatID, err := composite.Post(chatKey, "for the room")
	if err != nil {
		t.Fatal(err)
	}
	doorID, err := composite.Post(doorKey, "for the harness")
	if err != nil {
		t.Fatal(err)
	}
	if err := composite.Edit(chatKey, chatID, "rolling line, room"); err != nil {
		t.Fatal(err)
	}
	if err := composite.Edit(doorKey, doorID, "rolling line, harness"); err != nil {
		t.Fatal(err)
	}

	// The chat backend saw its own and nothing else.
	posts := primary.postTexts()
	if len(posts) != 1 || posts[0] != "for the room" {
		t.Fatalf("the chat backend received %v; an eval's answer must never reach a chat room", posts)
	}

	// And the door's transcript holds its own and nothing else.
	entries, _, _ := door.snapshot(doorKey, 0, "")
	var doorTexts []string
	for _, entry := range entries {
		doorTexts = append(doorTexts, entry.Text)
	}
	joined := strings.Join(doorTexts, "|")
	if !strings.Contains(joined, "for the harness") || !strings.Contains(joined, "rolling line, harness") {
		t.Fatalf("the door's transcript is missing its own traffic: %q", joined)
	}
	if strings.Contains(joined, "room") {
		t.Fatalf("a chat room's traffic landed in the door's transcript, which the harness grades: %q", joined)
	}
	if chatEntries, _, _ := door.snapshot(chatKey, 0, ""); len(chatEntries) != 0 {
		t.Fatalf("the door kept %d entries for a chat conversation", len(chatEntries))
	}
}

// TestSideDoorRosterAsksTheRightBackend: the roster rides into the authority
// block's audience -- "who could have read this" -- so answering it from the
// wrong backend would describe the wrong people.
func TestSideDoorRosterAsksTheRightBackend(t *testing.T) {
	primary, door, composite := newSideDoorRig(t)
	primary.roster = []string{"1001", "1002"}
	primary.complete = true

	doorKey := injectKeyPrefix + "case-roster"
	if _, err := door.Post(doorKey, "seed"); err != nil {
		t.Fatal(err)
	}
	door.noteRequester(doorKey, "devops-bench")

	ids, complete, err := composite.Roster(doorKey)
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 1 || ids[0] != "devops-bench" || !complete {
		t.Fatalf("the door's roster = %v (complete %v), want the requester alone", ids, complete)
	}

	ids, complete, err = composite.Roster("discord:g1/thread-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 2 || !complete {
		t.Fatalf("the chat roster = %v (complete %v), want the backend's own answer", ids, complete)
	}
}

// TestSideDoorObservesOnlyItsOwnTasks: TaskObserver exists for the door, and
// a chat conversation's task must not land in a transcript the harness reads
// as its own. The composite implements the interface whatever the primary is,
// so the gateway's type assertion cannot depend on which backend is paired.
func TestSideDoorObservesOnlyItsOwnTasks(t *testing.T) {
	_, door, composite := newSideDoorRig(t)

	observer, ok := composite.(TaskObserver)
	if !ok {
		t.Fatal("the composite does not implement TaskObserver, so POST /inject can never return a task id")
	}
	dropped, ok := composite.(InboundObserver)
	if !ok {
		t.Fatal("the composite does not implement InboundObserver, so a second drop at the door is a silence")
	}

	doorKey := injectKeyPrefix + "case-observe"
	observer.TaskStarted(doorKey, "task-mine")
	observer.TaskAccepted(doorKey, "task-mine")
	observer.TaskTerminal(doorKey, "task-mine", lib.StateCompleted, TerminalFromExecutor, "")
	dropped.MessageDropped(doorKey, "9999")
	observer.TaskStarted("discord:g1/thread-1", "task-theirs")
	observer.TaskAccepted("discord:g1/thread-1", "task-theirs")
	observer.TaskTerminal("discord:g1/thread-1", "task-theirs", lib.StateFailed, TerminalFromExecutor, "")
	dropped.MessageDropped("discord:g1/thread-1", "9999")

	entries, _, terminal := door.snapshot(doorKey, 0, "task-mine")
	if terminal != string(lib.StateCompleted) {
		t.Fatalf("the door did not record its own task's terminal: %q", terminal)
	}
	for _, entry := range entries {
		if entry.TaskID == "task-theirs" {
			t.Fatal("a chat conversation's task reached the door's transcript")
		}
	}
	if chatEntries, _, _ := door.snapshot("discord:g1/thread-1", 0, ""); len(chatEntries) != 0 {
		t.Fatalf("the door minted a transcript for a chat conversation: %d entries", len(chatEntries))
	}
	if counts := door.counts(doorKey); counts.accepted != 1 || counts.drops != 1 {
		t.Fatalf("the door recorded %+v for its own conversation, want one accept and one drop", counts)
	}
	if counts := door.counts("discord:g1/thread-1"); counts.accepted != 0 || counts.drops != 0 {
		t.Fatalf("a chat conversation's accept or drop reached the door: %+v", counts)
	}
}

// TestSideDoorStopsWhenEitherHalfDoes: a gateway that kept running with its
// chat backend dead would look healthy while consuming nothing, which is the
// failure the one-backend guard exists to prevent; one that kept running with
// a dead door would hang every eval on a listener nothing answers. Either
// half returning ends Run, so the Deployment restarts both.
func TestSideDoorStopsWhenEitherHalfDoes(t *testing.T) {
	primary, door, composite := newSideDoorRig(t)
	// A door that cannot bind fails Run at once, which is the half this case
	// stops. The chat half keeps running, and the composite must not.
	door.listen = "127.0.0.1:-1"

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- composite.Run(ctx, func(InboundMessage) {}) }()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("the composite reported a clean stop for a door that never came up")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the composite kept running after its inject door failed to bind")
	}
	// And the chat half was told to stop too, rather than being left serving
	// into a gateway that has exited.
	select {
	case <-primary.stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("the chat backend was left running after the composite returned")
	}
}

// TestSideDoorHandsTheProbeToTheDoor: the gateway offers its probe to
// whatever adapter it was built with, and with a side door that is the
// composite -- which has to pass it on to the one half whose caller reads
// records, or the read route would answer "could not look" on exactly the
// install stage 2 runs.
func TestSideDoorHandsTheProbeToTheDoor(t *testing.T) {
	_, door, composite := newSideDoorRig(t)
	sink, ok := composite.(ProbeSink)
	if !ok {
		t.Fatal("the composite does not implement ProbeSink; the gateway would offer the door no probe")
	}
	want := ConversationState{Active: true, TaskID: "task-1", ExecutorState: lib.StateWorking, Backend: gchatBackend}
	sink.SetProbe(func(context.Context, string) (ConversationState, error) { return want, nil })
	report := door.runProbe(context.Background(), injectKeyPrefix+"any")
	if !report.Active || report.TaskID != want.TaskID || report.ExecutorState != string(want.ExecutorState) ||
		report.Backend != gchatBackend || report.InjectOnly {
		t.Fatalf("the door answered %+v, want the probe the composite was handed", report)
	}
}

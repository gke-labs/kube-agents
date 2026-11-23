package mcp

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The gate-2 names (#5301): a session-shaped token that is a profile's name
// in the operator's profile names, and a profile-shaped token, with a live
// card, that is not. Profile-ness is the operator's names and nothing else:
// not the name's shape, not the directory, not how this server learned the
// task.
const (
	cutoffSessionShaped = "cto-otter-1a2b" // a profile, by the names
	cutoffProfileShaped = "fleet-audit"    // not a profile, card or not
)

// dialCutoff is dial with the install's profile supervisor cutoff and the
// operator's profile names, which name cutoffSessionShaped alone.
func dialCutoff(t *testing.T, url string, cutoff time.Time) *NATSBus {
	t.Helper()
	return dialGate(t, url, lib.ProfileSupervisorGate{
		ProfileSupervisorCutoffSetting: lib.ProfileSupervisorCutoffSetting{Cutoff: cutoff, Source: lib.CutoffSourceEnv},
		Names:                          lib.NewProfileNames(cutoffSessionShaped),
	})
}

func dialGate(t *testing.T, url string, gate lib.ProfileSupervisorGate) *NATSBus {
	t.Helper()
	b, err := DialBus(context.Background(), BusConfig{
		URL: url, User: testUser, Password: testPass,
		Deployment:            Deployment{Agent: "platform-agent", Namespace: "kubeagents-system"},
		ProfileSupervisorGate: gate,
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

// A profile's task doesn't end on a five-token supervisor terminal stored
// after the cutoff; a task under a name that is no profile does. The profile
// is named like a session and has no card; the other name is profile-shaped
// and has one; so neither the name's shape nor the directory decides.
func TestAFiveTokenTerminalAfterTheCutoffDoesNotEndAProfileTask(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, cutoffProfileShaped, map[string]any{"name": cutoffProfileShaped})
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	ctx := context.Background()

	prof := TaskRef{Addressee: cutoffSessionShaped, TaskID: "task-prof"}
	publishRun(t, js, prof.Addressee, prof.TaskID, "", false)
	publishGatewayTerminal(t, js, prof.Addressee, prof.TaskID)
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

	sess := TaskRef{Addressee: cutoffProfileShaped, TaskID: "task-sess"}
	publishSubmission(t, js, sess.Addressee, sess.TaskID, "console:x", "q", "")
	publishRun(t, js, sess.Addressee, sess.TaskID, "", false)
	publishGatewayTerminal(t, js, sess.Addressee, sess.TaskID)
	task, err = b.WaitForTask(ctx, sess, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if !task.Final || task.State != lib.StateFailed {
		t.Errorf("%s: the session task's supervisor terminal was not read: %s final=%v", sess, task.State, task.Final)
	}
	if b.client.ProfileSupervisorDrops() == 0 {
		t.Error("no drop counted")
	}
	if n := directoryConsumers(t, js); n != 0 {
		t.Errorf("a latched read opened %d DIRECTORY consumers; profile-ness is not the directory's", n)
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

// Nothing the gateway writes moves a task across the gate. The gateway
// writes .in, the console's status lines and the profile directory's
// readers' inputs: a turn that takes an .in message on a profile's task as
// its own, a chain that follows a delegated .in submission there, and a
// status line naming the task, all leave a five-token terminal after the
// cutoff unread on it, and none of them makes a task under another name read
// as a profile's.
func TestNothingTheGatewayWritesMovesATaskAcrossTheGate(t *testing.T) {
	url, js := testBus(t)
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	ctx := context.Background()
	const conv = "console:x"
	w := &turnWatch{addressed: true, messageID: "frame-1", conversation: conv, texts: []string{"q"},
		known: map[string]bool{}, subs: make(chan *nats.Msg, 1), stop: make(chan struct{})}

	// A turn's own .in message, a delegated .in submission with a via, and
	// a status line, each on a profile's task and on a session's.
	for _, addressee := range []string{cutoffSessionShaped, cutoffProfileShaped} {
		own, ok := w.ownSubmission(steerOrSubmission(t, addressee, "task-own", conv, "q", "", "corr-turn"))
		if !ok {
			t.Fatalf("%s: the turn did not take its .in message", addressee)
		}
		pub(t, js, lib.TaskInSubject(addressee, "task-child"), mustJSONEnv(t, steerOrSubmission(t, addressee, "task-child", conv, "go", "task-own", "corr-turn")))
		noticed, ok := w.noticedTask(&nats.Msg{Data: mustJSON(t, ConsoleOutFrame{Text: "⏳ submitted", TaskID: "task-noticed",
			Addressee: addressee, InReplyTo: "frame-1"})})
		if !ok {
			t.Fatalf("%s: the status line was not taken", addressee)
		}
		child := TaskRef{Addressee: addressee, TaskID: "task-child"}
		for _, ref := range []TaskRef{own, child, noticed} {
			publishRun(t, js, ref.Addressee, ref.TaskID, "", false)
			publishGatewayTerminal(t, js, ref.Addressee, ref.TaskID)
			task, err := b.ReadTask(ctx, ref)
			if err != nil {
				t.Fatal(err)
			}
			wantFinal := addressee != cutoffSessionShaped
			if task.Final != wantFinal {
				t.Errorf("%s: final = %v, want %v: profile-ness follows the operator's names alone", ref, task.Final, wantFinal)
			}
		}
	}
}

// mustJSONEnv is the envelope a steerOrSubmission message carries.
func mustJSONEnv(t *testing.T, m *nats.Msg) *lib.Envelope {
	t.Helper()
	var env lib.Envelope
	if err := json.Unmarshal(m.Data, &env); err != nil {
		t.Fatal(err)
	}
	return &env
}

// A profile whose card was withdrawn, or whose directory can't be read at
// all, is still a profile: the read decides at once from the operator's names
// and the stored time. A task whose only message the cutoff refused is not
// found at once; a session task's supervisor terminal is read at once; and a
// profile's task with a working event and a refused terminal reads working.
// Before the names, the directory decided, and with it unreadable a held
// message waited for it.
func TestAnUnreadableDirectoryDecidesNothing(t *testing.T) {
	url, js := testBus(t)
	ctx, stop := context.WithTimeout(context.Background(), 20*time.Second)
	defer stop()
	if err := js.DeleteStream(ctx, streamDirectory); err != nil {
		t.Fatal(err)
	}
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))

	refused := TaskRef{Addressee: cutoffSessionShaped, TaskID: "task-only-refused"}
	publishGatewayTerminal(t, js, refused.Addressee, refused.TaskID)
	start := time.Now()
	if _, err := b.WaitForTask(ctx, refused, 10*time.Second); err != ErrTaskNotFound {
		t.Errorf("a wait on a task with only a refused terminal = %v, want ErrTaskNotFound", err)
	}
	if took := time.Since(start); took > 3*time.Second {
		t.Errorf("the wait took %s; it should answer at once", took)
	}

	sess := TaskRef{Addressee: cutoffProfileShaped, TaskID: "task-sess"}
	publishGatewayTerminal(t, js, sess.Addressee, sess.TaskID)
	start = time.Now()
	task, err := b.WaitForTask(ctx, sess, 10*time.Second)
	if err != nil || !task.Final || task.State != lib.StateFailed {
		t.Errorf("a session task's terminal with no directory = %+v, %v; want it read", task, err)
	}
	if took := time.Since(start); took > 3*time.Second {
		t.Errorf("the session read took %s; it should answer at once", took)
	}

	prof := TaskRef{Addressee: cutoffSessionShaped, TaskID: "task-prof"}
	publishRun(t, js, prof.Addressee, prof.TaskID, "", false)
	publishGatewayTerminal(t, js, prof.Addressee, prof.TaskID)
	task, err = b.ReadTask(ctx, prof)
	if err != nil || task.Final || task.State != lib.StateWorking {
		t.Errorf("a profile's task with no directory = %+v, %v; want it working", task, err)
	}
}

// A message held for an undecided kind used to read as "no such task" when
// the directory was unreadable (the review's point 4). Nothing is held now:
// a wait on a submitted task whose first event has not landed still waits
// for it rather than answering not found, and the refused-only answer is
// given only when the stream has nothing else.
func TestAWaitOnASubmittedTaskStillWaitsForItsFirstEvent(t *testing.T) {
	url, js := testBus(t)
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	ref := TaskRef{Addressee: cutoffSessionShaped, TaskID: "task-late"}
	publishGatewayTerminal(t, js, ref.Addressee, ref.TaskID)
	go func() {
		time.Sleep(500 * time.Millisecond)
		publishRun(t, js, ref.Addressee, ref.TaskID, "the answer", true)
	}()
	task, err := b.waitForSubmittedTask(context.Background(), ref, 10*time.Second)
	if err != nil || !task.Final || task.State != lib.StateCompleted {
		t.Errorf("a submitted task's wait = %+v, %v; want its executor's terminal", task, err)
	}
}

// Planner call A (#5301) as the names have it: a session task whose
// addressee is in the operator's profile names reads as a profile's, so its
// gateway terminal stored after the cutoff is not read, and get shows it
// unfinished. The bus refuses the gateway that write on a profile's name
// already (nats.conf's per-profile deny, from the same names), so the reading
// and the permission agree. Under a name the names don't hold it reads as
// ended, card or not.
func TestASessionTaskUnderAProfilesNameReadsAsUnfinishedInGet(t *testing.T) {
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

	got := callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task": cutoffSessionShaped + "/task-old"})
	if got.IsError || !strings.Contains(got.Content[0].Text, "**working** (still running") {
		t.Errorf("get of a session task under a profile's name = %+v, want it unfinished", got)
	}
	got = callTool(t, s, ToolTaskControl, map[string]any{"action": "get", "task": cutoffProfileShaped + "/task-old"})
	if got.IsError || !strings.Contains(got.Content[0].Text, "**failed**") || strings.Contains(got.Content[0].Text, "still running") {
		t.Errorf("get of a session task under a name with a card and not in the names = %+v, want it ended", got)
	}
}

// The swarm read applies the same rule: a task it knows only from the stream
// is a profile's when its addressee is in the operator's names, and its
// five-token terminal after the cutoff is not folded.
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
	if f, ok := final[cutoffSessionShaped]; !ok || f {
		t.Errorf("the profile's task folded final=%v (seen %v), want unfinished", f, ok)
	}
	if f, ok := final[cutoffProfileShaped]; !ok || !f {
		t.Errorf("the session's task folded final=%v (seen %v), want ended", f, ok)
	}
	if n := b.client.ProfileSupervisorDrops(); n != 1 {
		t.Errorf("the swarm read counted %d drops, want the profile's one", n)
	}
}

// Unlatched, every read is what it was before the cutoff existed: the same
// fold whatever the profile names hold, with no directory read and nothing
// counted.
func TestAnUnlatchedServerReadsBothFormsAsBefore(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, cutoffProfileShaped, map[string]any{"name": cutoffProfileShaped})
	plain := dial(t, url)
	named := dialGate(t, url, lib.ProfileSupervisorGate{Names: lib.NewProfileNames(cutoffSessionShaped, cutoffProfileShaped)})
	unloaded := dialGate(t, url, lib.ProfileSupervisorGate{Names: &lib.ProfileNames{}})
	ctx := context.Background()
	for _, addressee := range []string{cutoffProfileShaped, cutoffSessionShaped} {
		publishSubmission(t, js, addressee, "task-u", "console:x", "q", "")
		publishRun(t, js, addressee, "task-u", "", false)
		publishGatewayTerminal(t, js, addressee, "task-u")
		ref := TaskRef{Addressee: addressee, TaskID: "task-u"}
		want, err := plain.ReadTask(ctx, ref)
		if err != nil {
			t.Fatal(err)
		}
		if !want.Final || want.State != lib.StateFailed {
			t.Errorf("%s: %s final=%v, want the supervisor's terminal", addressee, want.State, want.Final)
		}
		for label, b := range map[string]*NATSBus{"names": named, "names never loaded": unloaded} {
			got, err := b.ReadTask(ctx, ref)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("%s, %s: the fold differs:\n got %+v\nwant %+v", addressee, label, got, want)
			}
		}
	}
	if n := directoryConsumers(t, js); n != 0 {
		t.Errorf("an unlatched read opened %d DIRECTORY consumers", n)
	}
	for _, b := range []*NATSBus{plain, named, unloaded} {
		if b.client.ProfileSupervisorDrops() != 0 {
			t.Errorf("an unlatched server counted %d drops", b.client.ProfileSupervisorDrops())
		}
	}
}

// A wait on a profile task whose only message the cutoff refuses answers not
// found at once, as ReadTask does, rather than holding a reader for the whole
// wait to say the same.
func TestAWaitOnATaskWithOnlyARefusedTerminalIsNotFoundAtOnce(t *testing.T) {
	url, js := testBus(t)
	b := dialCutoff(t, url, time.Now().Add(-time.Minute))
	ref := TaskRef{Addressee: cutoffSessionShaped, TaskID: "task-only-refused"}
	publishGatewayTerminal(t, js, ref.Addressee, ref.TaskID)
	ctx := context.Background()
	if _, err := b.ReadTask(ctx, ref); err != ErrTaskNotFound {
		t.Fatalf("ReadTask = %v, want ErrTaskNotFound", err)
	}
	start := time.Now()
	_, err := b.WaitForTask(ctx, ref, 10*time.Second)
	if err != ErrTaskNotFound {
		t.Fatalf("WaitForTask = %v, want ErrTaskNotFound", err)
	}
	if took := time.Since(start); took > 3*time.Second {
		t.Errorf("WaitForTask took %s; it should answer at once, not wait out the 10s", took)
	}
}

// The health probe says which gate the server runs with, so a malformed
// cutoff or names that never loaded show outside the log: the cutoff, its
// source, whether the names are loaded, and the drops.
func TestTheHealthProbeShowsTheProfileSupervisorGate(t *testing.T) {
	url, _ := testBus(t)
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "token")
	if err := os.WriteFile(tokenFile, []byte("tok\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		value string
		names *lib.ProfileNames
		want  ProfileSupervisorHealth
	}{
		{"", nil, ProfileSupervisorHealth{Source: lib.CutoffSourceUnset}},
		{"2026-11-23T12:10:00Z", lib.NewProfileNames("fleet-audit"), ProfileSupervisorHealth{Cutoff: "2026-11-23T12:10:00Z", Source: lib.CutoffSourceEnv, NamesLoaded: true}},
		{"garbled", &lib.ProfileNames{}, ProfileSupervisorHealth{Cutoff: "1970-01-01T00:00:00Z", Source: lib.CutoffSourceMalformed}},
	} {
		setting := lib.ProfileSupervisorCutoffFromEnv(func(string) string { return c.value }, nil)
		b := dialGate(t, url, lib.ProfileSupervisorGate{ProfileSupervisorCutoffSetting: setting, Names: c.names})
		srv := mustServer(t)
		h := Handler(srv, tokenFile, nil, WithProfileSupervisorHealth(b.ProfileSupervisorHealth))
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, HealthPath, nil))
		var body struct {
			Status string                  `json:"status"`
			Gate   ProfileSupervisorHealth `json:"profileSupervisorGate"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("%q: %v (%s)", c.value, err, rec.Body.String())
		}
		// When the names were last read, which only loaded names have.
		if at := c.names.LoadedAt(); !at.IsZero() {
			c.want.NamesLoadedAt = at.UTC().Format(time.RFC3339)
		}
		if body.Status != "ok" || body.Gate != c.want {
			t.Errorf("%q: health = %+v, want gate %+v", c.value, body, c.want)
		}
	}
}

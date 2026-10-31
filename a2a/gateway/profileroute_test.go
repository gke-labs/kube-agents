package gateway

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

type subjectEnvelope struct {
	subject string
	env     *lib.Envelope
}

// streamEnvelopes is every envelope under filter, oldest first: on
// PROFILE_SUBMITS for a submission filter, on TASKS otherwise.
func streamEnvelopes(t *testing.T, url, filter string) []subjectEnvelope {
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
	stream := lib.TasksStream
	if strings.HasPrefix(filter, lib.SubmitSubjectPrefix) {
		stream = lib.ProfileSubmitStream
	}
	cons, err := js.OrderedConsumer(ctx, stream, jetstream.OrderedConsumerConfig{FilterSubjects: []string{filter}})
	if err != nil {
		t.Fatal(err)
	}
	it, err := cons.Messages()
	if err != nil {
		t.Fatal(err)
	}
	defer it.Stop()
	var out []subjectEnvelope
	for {
		fctx, fcancel := context.WithTimeout(ctx, 300*time.Millisecond)
		msg, err := fetchNext(fctx, it)
		fcancel()
		if err != nil {
			break
		}
		if env, err := lib.ParseEnvelope(msg.Data()); err == nil {
			out = append(out, subjectEnvelope{subject: msg.Subject(), env: env})
		}
	}
	return out
}

// mintedEntry reads the grant ref names from the cap bucket.
func mintedEntry(t *testing.T, url string, ref *capability.Ref) capability.Entry {
	t.Helper()
	if ref == nil {
		t.Fatal("no capability on the task")
	}
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
	kv, err := js.KeyValue(ctx, capability.Bucket)
	if err != nil {
		t.Fatal(err)
	}
	e, err := kv.GetRevision(ctx, ref.Key, ref.Revision)
	if err != nil {
		t.Fatal(err)
	}
	var entry capability.Entry
	if err := json.Unmarshal(e.Value(), &entry); err != nil {
		t.Fatal(err)
	}
	return entry
}

// startProfileRig is a rig whose gateway knows a generic cluster profile and
// one pinned to p1/us-east1/prod, under a scope that lists project p1.
func startProfileRig(t *testing.T) *rig {
	t.Helper()
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r := startRigWith(t, func(c *Config) { c.ScopeFile = scope; c.Namespace = "kubeagents-system" })
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams})
	r.g.profiles.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	return r
}

func profileTurn(conversation, profile string, params map[string]string) InboundMessage {
	return InboundMessage{Conversation: conversation, Kind: "dm", AuthorID: "1001", MessageID: "m-" + conversation,
		Text: "what's wrong with it", Profile: profile, Parameters: params}
}

func TestProfileTurn_IsMintedForTheResolvedProfileAndSubmittedOnSubmit(t *testing.T) {
	r := startProfileRig(t)
	r.adapter.inbox <- profileTurn("discord:1/1", "cluster", targetParams("p1", "us-east1", "prod"))

	var got []subjectEnvelope
	waitFor(t, "a submission on the pinned profile's .submit", func() bool {
		got = streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"prod-east.>")
		return len(got) == 1
	})
	profile, tier, kind, taskID, ok := lib.ParseSubmitSubject(got[0].subject)
	if !ok || profile != "prod-east" || tier != "read" || kind != lib.SubmitKindTop {
		t.Fatalf("subject = %q, want prod-east's read top .submit", got[0].subject)
	}
	params, found, err := lib.TaskParametersOf(messageParts(t, got[0].env))
	if err != nil || !found {
		t.Fatalf("taskParameters part: found=%v err=%v", found, err)
	}
	if want := targetParams("p1", "us-east1", "prod"); !mapsEqual(params.Parameters, want) {
		t.Fatalf("parameters = %v, want %v", params.Parameters, want)
	}

	rec := waitForRecord(t, r, "discord:1/1", "the task on record", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(taskID)
		return ok && ref.Tier == "read"
	})
	if rec.Addressee != "prod-east" {
		t.Fatalf("addressee = %q, want the resolved profile", rec.Addressee)
	}
	ref, _ := rec.TaskRefFor(taskID)
	entry := mintedEntry(t, r.url, ref.Capability)
	if entry.Tier != capability.TierRead || entry.Profile != "prod-east" || entry.Delegate != profileGrantDelegate ||
		entry.Scope != "project/p1/location/us-east1/cluster/prod" {
		t.Fatalf("grant = %+v, want read for prod-east at its cluster, delegated to the dispatcher", entry)
	}
	// The grant answers the dispatcher's question for this task and no other.
	if err := capability.PermitsProfile(entry, "prod-east", capability.TierRead, "project/p1/location/us-east1/cluster/prod"); err != nil {
		t.Fatalf("PermitsProfile for the minted target: %v", err)
	}
	if err := capability.PermitsProfile(entry, "prod-east", capability.TierRead, "project/p1/location/us-east1/cluster/dev"); err == nil {
		t.Fatal("the grant permits a cluster it wasn't minted for")
	}
	if err := capability.PermitsProfile(entry, "cluster", capability.TierRead, "project/p1/location/us-east1/cluster/prod"); err == nil {
		t.Fatal("the grant permits a profile it wasn't minted for")
	}
}

func TestProfileTurn_ARefusalPostsWhyAndSubmitsNothing(t *testing.T) {
	cases := []struct {
		name, profile string
		params        map[string]string
		want          string
	}{
		{"out of scope", "cluster", targetParams("p9", "us-east1", "prod"), "outside this install's scope"},
		{"pinned for another cluster", "prod-east", targetParams("p1", "us-east1", "dev"), "works only on cluster"},
		{"unknown profile", "nope", nil, `no agent profile named "nope"`},
	}
	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := startProfileRig(t)
			conv := "discord:2/" + string(rune('a'+i))
			r.adapter.inbox <- profileTurn(conv, tc.profile, tc.params)
			waitFor(t, "the refusal", func() bool {
				for _, p := range r.adapter.postTexts() {
					if strings.Contains(p, tc.want) {
						return true
					}
				}
				return false
			})
			got := append(streamEnvelopes(t, r.url, "a2a.tasks.>"), streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">")...)
			if len(got) != 0 {
				t.Fatalf("published %d message(s) for a refused turn: %s", len(got), got[0].subject)
			}
		})
	}
}

func TestDeliverChildResult_ReachesAProfileParentOnItsIn(t *testing.T) {
	r := startProfileRig(t)
	rec := &SessionRecord{Key: "discord:3/3", ContextID: "ctx-3", Tasks: []TaskRef{
		{ID: "task-parent", Addressee: "cluster", Tier: "read", CorrelationID: "corr-p",
			Attribution: json.RawMessage(`{"requester":{"backend":"discord","subject":"hmac:00"}}`)},
		{ID: "task-child", Addressee: "prod-east", Role: taskRoleChild, ParentTaskID: "task-parent"},
	}}
	child, _ := rec.TaskRefFor("task-child")
	if !r.g.deliverChildResult(context.Background(), rec, child, lib.StateCompleted, "the pod is OOMKilled", "") {
		t.Fatal("not delivered")
	}
	var got []subjectEnvelope
	waitFor(t, "the child result on the parent's .in", func() bool {
		got = streamEnvelopes(t, r.url, lib.TaskInSubject("cluster", "task-parent"))
		return len(got) == 1
	})
	c, ok := lib.ChildResultOf(messageParts(t, got[0].env))
	if !ok || c.ChildTaskID != "task-child" || c.State != lib.StateCompleted || c.Text != "the pod is OOMKilled" || c.Addressee != "prod-east" {
		t.Fatalf("child result = %+v, %v", c, ok)
	}
}

func TestDeliverChildResult_LeavesASessionParentToTheWake(t *testing.T) {
	r := startProfileRig(t)
	rec := &SessionRecord{Key: "discord:4/4", ContextID: "ctx-4", Tasks: []TaskRef{
		{ID: "task-parent", Addressee: "chat-otter-1a2b", CorrelationID: "corr-p",
			Attribution: json.RawMessage(`{"requester":{"backend":"discord","subject":"hmac:00"}}`)},
		{ID: "task-child", Addressee: "prod-east", Role: taskRoleChild, ParentTaskID: "task-parent"},
	}}
	child, _ := rec.TaskRefFor("task-child")
	if r.g.deliverChildResult(context.Background(), rec, child, lib.StateCompleted, "done", "") {
		t.Fatal("a session parent's child was delivered as a child result")
	}
}

func messageParts(t *testing.T, env *lib.Envelope) []lib.Part {
	t.Helper()
	var m lib.Message
	if err := json.Unmarshal(env.Payload, &m); err != nil {
		t.Fatal(err)
	}
	return m.Parts
}

func mapsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// submissionOn waits for the one submission on profile's queue and returns it.
func submissionOn(t *testing.T, r *rig, profile string) *lib.Envelope {
	t.Helper()
	var got []subjectEnvelope
	waitFor(t, "a submission on "+profile, func() bool {
		got = nil
		for _, e := range streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+profile+".>") {
			{
				got = append(got, e)
			}
		}
		return len(got) == 1
	})
	return got[0].env
}

// TestAProfileParentDelegatesWaitsAndEndsItsChain drives a top-level profile
// task through the relay: its delegation to platform is refused, its
// delegation to a profile is submitted at its own tier, the child's end
// reaches it as a childResult and gives it the conversation back, a stop
// while it waits cancels it, and its own end is the chain's end.
func TestAProfileParentDelegatesWaitsAndEndsItsChain(t *testing.T) {
	r := startProfileRig(t)
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	ctx := context.Background()
	// A console conversation: the one door whose observer is told each
	// chain's end as a terminal frame.
	conv := "console:tab-5"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	// To platform: refused, nothing minted.
	if err := parent.PublishArtifact(ctx, delegateArtifact(t, "platform", "do it with your tier")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the platform refusal", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && strings.Contains(ref.DelegationEnd, noticeProfileParentToPlatform)
	})
	if n := platformSubmissions(t, r); n != 0 {
		t.Fatalf("a profile parent minted %d platform child(ren)", n)
	}

	// To a profile: a child on the pinned profile's child queue, at the
	// parent's tier. The refusal above recorded a DelegationEnd, so clear
	// it as a fresh request would find it.
	putRecord(t, r, conv, func(rec *SessionRecord) {
		for i := range rec.Tasks {
			if rec.Tasks[i].ID == origin.TaskID {
				rec.Tasks[i].DelegationEnd = ""
			}
		}
	})
	data, err := json.Marshal(lib.DelegateRequest{Addressee: "prod-east", Text: "look at prod"})
	if err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishArtifact(ctx, lib.Artifact{ArtifactID: "artifact-y-" + lib.ArtifactDelegate, Name: lib.ArtifactDelegate,
		Parts: []lib.Part{{Kind: "data", Data: data}}}); err != nil {
		t.Fatal(err)
	}
	childEnv := submissionOn(t, r, "prod-east")
	childSubject := ""
	for _, e := range streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"prod-east.>") {
		childSubject = e.subject
	}
	if _, tier, kind, _, ok := lib.ParseSubmitSubject(childSubject); !ok || tier != "read" || kind != lib.SubmitKindChild {
		t.Fatalf("child subject = %q, want a read child submission", childSubject)
	}

	// The child ends: its result reaches the parent's .in, and the parent
	// holds the conversation again.
	child := r.execFor(t, childEnv, "prod-east")
	if err := child.PublishArtifact(ctx, lib.Artifact{ArtifactID: "a-r", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "prod is fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := child.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the child result on the parent's .in", func() bool {
		for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject("chat", origin.TaskID)) {
			if c, ok := lib.ChildResultOf(messageParts(t, e.env)); ok && c.ChildTaskID == childEnv.TaskID && c.Text == "prod is fine" {
				return true
			}
		}
		return false
	})
	waitForRecord(t, r, conv, "the parent active again", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == origin.TaskID && rec.Addressee == "chat"
	})

	// The parent's own end is the chain's: an observer is told.
	r.adapter.setTakesTerminals()
	if err := parent.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the chain's end told to the observer", func() bool {
		for _, tp := range r.adapter.terminalPosts() {
			if tp.TaskID == origin.TaskID && tp.State == lib.StateCompleted {
				return true
			}
		}
		return false
	})
}

// TestAStopWhileAProfileChildRunsCancelsTheWaitingParent: the child holds
// the conversation while it runs, and a stop cancels it and the profile task
// waiting on it.
func TestAStopWhileAProfileChildRunsCancelsTheWaitingParent(t *testing.T) {
	r := startProfileRig(t)
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	ctx := context.Background()
	conv := "discord:6/6"
	r.adapter.inbox <- profileTurn(conv, "chat", nil)
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	_ = parent.PublishStatus(ctx, lib.StateWorking, false)
	data, _ := json.Marshal(lib.DelegateRequest{Addressee: "prod-east", Text: "look at prod"})
	if err := parent.PublishArtifact(ctx, lib.Artifact{ArtifactID: "artifact-z-" + lib.ArtifactDelegate, Name: lib.ArtifactDelegate,
		Parts: []lib.Part{{Kind: "data", Data: data}}}); err != nil {
		t.Fatal(err)
	}
	childEnv := submissionOn(t, r, "prod-east")
	waitForRecord(t, r, conv, "the child active", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == childEnv.TaskID
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "stop-6", Text: "stop"}
	cancelled := func(addressee, taskID string) bool {
		for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject(addressee, taskID)) {
			if e.env.Kind == lib.KindCancel {
				return true
			}
		}
		return false
	}
	waitFor(t, "the child's cancel", func() bool { return cancelled("prod-east", childEnv.TaskID) })
	waitFor(t, "the waiting parent's cancel", func() bool { return cancelled("chat", origin.TaskID) })
	// Each profile task in the tree gets its stop record too, for the
	// dispatcher, which has no read on `…in`.
	for _, task := range []struct{ profile, id string }{{"prod-east", childEnv.TaskID}, {"chat", origin.TaskID}} {
		waitFor(t, "the stop record for "+task.profile, func() bool {
			stop, ok := dispatchStopFor(t, r.url, task.profile, task.id)
			return ok && stop.V == 1 && stop.TaskID == task.id
		})
	}
}

// TestAStopOfAPlatformTaskWritesNoStopRecord: the record is the dispatcher's,
// and a task addressed to platform never went through it.
func TestAStopOfAPlatformTaskWritesNoStopRecord(t *testing.T) {
	r := startProfileRig(t)
	conv := "discord:8/8"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-8a", Text: "how is the fleet?"}
	origin := r.awaitTask(t, "platform")
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "stop-8", Text: "stop"}
	waitFor(t, "the cancel", func() bool {
		for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject("platform", origin.TaskID)) {
			if e.env.Kind == lib.KindCancel {
				return true
			}
		}
		return false
	})
	if n := dispatchStopCount(t, r.url); n != 0 {
		t.Fatalf("a platform task's stop wrote %d stop records", n)
	}
}

// dispatchStopFor reads a task's stop record the way the dispatcher does: a
// direct get of the last message on its subject.
func dispatchStopFor(t *testing.T, url, profile, taskID string) (*lib.DispatchStop, bool) {
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
	stream, err := js.Stream(ctx, lib.DispatchStopStream)
	if err != nil {
		t.Fatal(err)
	}
	msg, err := stream.GetLastMsgForSubject(ctx, lib.DispatchStopSubject(profile, taskID))
	if err != nil {
		return nil, false
	}
	var stop lib.DispatchStop
	if err := json.Unmarshal(msg.Data, &stop); err != nil {
		t.Fatalf("stop record %s: %v", msg.Subject, err)
	}
	return &stop, true
}

// dispatchStopCount is how many stop records the stream holds.
func dispatchStopCount(t *testing.T, url string) uint64 {
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
	stream, err := js.Stream(ctx, lib.DispatchStopStream)
	if err != nil {
		t.Fatal(err)
	}
	info, err := stream.Info(ctx)
	if err != nil {
		t.Fatal(err)
	}
	return info.State.Msgs
}

func TestAProfileTurnWhileATaskRunsIsRefusedNotSteered(t *testing.T) {
	r := startProfileRig(t)
	conv := "discord:7/7"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-7a", Text: "how is the fleet?"}
	origin := r.awaitTask(t, "platform")
	r.adapter.inbox <- profileTurn(conv, "cluster", targetParams("p1", "us-east1", "prod"))
	waitFor(t, "the refusal", postedContaining(r, noticeProfileWhileRunning))
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		if e.TaskID == origin.TaskID && e.EnvelopeID != origin.EnvelopeID && e.Kind == lib.KindMessage {
			t.Fatal("the profile turn was steered into the running platform task")
		}
	}
	if got := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"prod-east.>"); len(got) != 0 {
		t.Fatalf("a profile task was submitted while another ran: %s", got[0].subject)
	}
}

func TestDirectoryFeed_AReReadForgetsAProfileWhoseTombstoneWasMissed(t *testing.T) {
	f := &directoryFeed{dir: newProfileDirectory(), log: slog.New(slog.NewTextHandler(io.Discard, nil)), live: map[string]bool{}}
	f.dir.put("gone", &lib.AgentCard{Name: "gone"})
	f.dir.put("kept", &lib.AgentCard{Name: "kept"})
	f.beginRead()
	// A card the live feed applies during the read stays, found or not.
	f.apply(lib.AgentSubject("fresh"), cardEnvelope(t, lib.AgentCard{Name: "fresh"}), true)
	f.endRead(map[string]bool{"kept": true})
	if _, ok := f.dir.lookup("gone"); ok {
		t.Fatal("a profile the re-read no longer found is still addressable")
	}
	for _, name := range []string{"kept", "fresh"} {
		if _, ok := f.dir.lookup(name); !ok {
			t.Fatalf("%s was forgotten", name)
		}
	}
}

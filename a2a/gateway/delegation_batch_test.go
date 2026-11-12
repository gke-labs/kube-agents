package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// delegateBatchArtifact is the artifact the worker adapter publishes for a
// delegate call carrying a batch.
func delegateBatchArtifact(t *testing.T, tasks ...lib.DelegateTask) lib.Artifact {
	t.Helper()
	data, err := json.Marshal(lib.DelegateRequest{Tasks: tasks})
	if err != nil {
		t.Fatal(err)
	}
	return lib.Artifact{ArtifactID: "artifact-x-" + lib.ArtifactDelegate, Name: lib.ArtifactDelegate, Parts: []lib.Part{{Kind: "data", Data: data}}}
}

// platformTask is a batch entry for platform.
func platformTask(text string) lib.DelegateTask {
	return lib.DelegateTask{Addressee: targetPlatform, Text: text}
}

// startBatchRig is a session rig whose gateway takes batches up to width.
func startBatchRig(t *testing.T, width int) (*rig, *fakeSpawner) {
	t.Helper()
	return startRigWithSpawnerCap(t, targetPlatform, 0, func(c *Config) { c.DelegationWidthMax = width })
}

// childrenOf is the record's child entries naming parent.
func childrenOf(rec *SessionRecord, parent string) []TaskRef {
	var out []TaskRef
	for _, ref := range rec.Tasks {
		if ref.Role == taskRoleChild && ref.ParentTaskID == parent {
			out = append(out, ref)
		}
	}
	return out
}

// TestABatchMintsEveryChildBesideTheParent: a batch of three mints three
// children to platform, each under the parent's chain and a via naming it,
// each linked from the parent; the parent keeps the active slot and the
// record's addressee stays its session.
func TestABatchMintsEveryChildBesideTheParent(t *testing.T) {
	r, spawn := startBatchRig(t, 3)
	ctx := context.Background()
	conv := "discord:g1/t-batch"
	exec, origin, session := sessionTurn(t, r, spawn, conv, "check three things")
	if err := exec.PublishArtifact(ctx, delegateBatchArtifact(t, platformTask("one"), platformTask("two"), platformTask("three"))); err != nil {
		t.Fatal(err)
	}
	var texts []string
	for i := range 3 {
		child := awaitSubmission(t, r, targetPlatform, i)
		texts = append(texts, envText(t, child))
		if child.CorrelationID != origin.CorrelationID || child.ContextID != origin.ContextID {
			t.Fatalf("child %d: correlation/context %s/%s, want the parent's", i, child.CorrelationID, child.ContextID)
		}
		var auth Authority
		if err := json.Unmarshal(child.Authority, &auth); err != nil {
			t.Fatal(err)
		}
		if auth.Via == nil || auth.Via.TaskID != origin.TaskID || auth.Via.Session != session {
			t.Fatalf("child %d: via = %+v", i, auth.Via)
		}
	}
	if strings.Join(texts, ",") != "one,two,three" {
		t.Fatalf("children's texts = %v, want the batch's in order", texts)
	}
	waitFor(t, "three children on the record", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		pref, _ := rec.TaskRefFor(origin.TaskID)
		return len(pref.Children) == 3
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID {
		t.Fatalf("active = %+v, want the parent %s", rec.ActiveTask, origin.TaskID)
	}
	if rec.Addressee != session || rec.BusSession != session {
		t.Fatalf("addressee = %s busSession = %s, want the parent's session %s", rec.Addressee, rec.BusSession, session)
	}
	pref, _ := rec.TaskRefFor(origin.TaskID)
	kids := childrenOf(rec, origin.TaskID)
	if len(kids) != 3 {
		t.Fatalf("child entries = %+v", kids)
	}
	for i, c := range kids {
		if pref.Children[i] != c.ID || c.Depth != 1 || c.Addressee != targetPlatform || c.RootTaskID != origin.TaskID {
			t.Fatalf("child %d = %+v, parent children %v", i, c, pref.Children)
		}
		if c.StatusMsgID == "" {
			t.Fatalf("child %d keeps no rolling line on its entry", i)
		}
		if key, _ := r.g.reg.SessionForTask(ctx, c.ID); key != conv {
			t.Fatalf("child %d not indexed: %q", i, key)
		}
	}
	if n := strings.Count(r.logs.String(), "delegation minted"); n != 3 {
		t.Fatalf("%d minted lines, want 3", n)
	}
}

// TestABatchOfOneIsASingleDelegation: one entry behaves as the single shape
// does today, the child taking the active slot and the addressee.
func TestABatchOfOneIsASingleDelegation(t *testing.T) {
	r, spawn := startBatchRig(t, 3)
	ctx := context.Background()
	conv := "discord:g1/t-batch-one"
	exec, origin, session := sessionTurn(t, r, spawn, conv, "x")
	if err := exec.PublishArtifact(ctx, delegateBatchArtifact(t, platformTask("only"))); err != nil {
		t.Fatal(err)
	}
	child := r.awaitTask(t, targetPlatform)
	waitFor(t, "the child active", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == child.TaskID
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.Addressee != targetPlatform || rec.BusSession != session {
		t.Fatalf("addressee = %s busSession = %s", rec.Addressee, rec.BusSession)
	}
	pref, _ := rec.TaskRefFor(origin.TaskID)
	if len(pref.Children) != 1 || pref.Children[0] != child.TaskID || pref.StatusMsgID == "" {
		t.Fatalf("parent = %+v", pref)
	}
}

// TestABatchOverTheWidthIsRefusedWhole: a batch wider than the setting is
// refused with a notice naming the cap, and nothing is minted.
func TestABatchOverTheWidthIsRefusedWhole(t *testing.T) {
	for _, width := range []int{1, 2} {
		r, spawn := startBatchRig(t, width)
		ctx := context.Background()
		conv := "discord:g1/t-batch-wide"
		exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
		if err := exec.PublishArtifact(ctx, delegateBatchArtifact(t, platformTask("a"), platformTask("b"), platformTask("c"))); err != nil {
			t.Fatal(err)
		}
		waitFor(t, "width refusal", loggedContaining(r, "delegation refused", "rule="+ruleDelegationWidth, origin.TaskID))
		_ = exec.PublishStatus(ctx, lib.StateCompleted, true)
		want := "⚠️ delegation refused: 3 tasks in one delegation, and this conversation may hand out at most " + string(rune('0'+width))
		waitFor(t, "notice naming the cap", postedContaining(r, want))
		if n := platformSubmissions(t, r); n != 0 {
			t.Fatalf("width %d: a refused batch minted %d children", width, n)
		}
	}
}

// TestOneBadEntryRefusesTheWholeBatch: every entry meets the single child's
// checks before any child is minted, so one that fails refuses the batch
// with nothing minted, whichever place it holds.
func TestOneBadEntryRefusesTheWholeBatch(t *testing.T) {
	for _, tc := range []struct {
		name   string
		tasks  []lib.DelegateTask
		notice string
		rule   string
	}{
		{name: "no such profile, last", tasks: []lib.DelegateTask{platformTask("a"), platformTask("b"), {Addressee: "chat-other-1", Text: "c"}},
			notice: `⚠️ delegation refused: there is no agent profile named "chat-other-1"`, rule: ruleDelegationTarget},
		{name: "parameters to platform, first", tasks: []lib.DelegateTask{{Addressee: targetPlatform, Text: "a", Parameters: map[string]string{"cluster": "web-1"}}, platformTask("b")},
			notice: "⚠️ delegation refused: " + noticePlatformTakesNoParameters, rule: ruleDelegationTarget},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, spawn := startBatchRig(t, 3)
			ctx := context.Background()
			conv := "discord:g1/t-batch-bad"
			exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
			if err := exec.PublishArtifact(ctx, delegateBatchArtifact(t, tc.tasks...)); err != nil {
				t.Fatal(err)
			}
			waitFor(t, "refusal line", loggedContaining(r, "delegation refused", "rule="+tc.rule, origin.TaskID))
			_ = exec.PublishStatus(ctx, lib.StateCompleted, true)
			waitFor(t, "notice", postedContaining(r, tc.notice))
			if n := platformSubmissions(t, r); n != 0 {
				t.Fatalf("a refused batch minted %d children", n)
			}
			rec, _ := r.g.reg.Get(ctx, conv)
			if kids := childrenOf(rec, origin.TaskID); len(kids) != 0 {
				t.Fatalf("a refused batch left child entries: %+v", kids)
			}
		})
	}
}

// TestADelegateBatchWithAnEntryOverTheCapIsIgnored: an entry the adapter
// would have refused (blank, or over the text cap) means the batch reached
// the bus some other way: ignored as malformed, like the single shape.
func TestADelegateBatchWithAnEntryOverTheCapIsIgnored(t *testing.T) {
	r, spawn := startBatchRig(t, 3)
	ctx := context.Background()
	conv := "discord:g1/t-batch-cap"
	exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
	art := delegateBatchArtifact(t, platformTask("a"), platformTask(strings.Repeat("x", lib.DelegateTextCap+1)))
	if err := exec.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "ignore line", loggedContaining(r, "delegation ignored", "rule="+ruleDelegationMalformed, origin.TaskID))
	if n := platformSubmissions(t, r); n != 0 {
		t.Fatalf("a malformed batch minted %d children", n)
	}
}

// TestARedeliveredBatchMintsNothing: the artifact delivered again (a relay
// replay, a straggler) finds the parent's children and is ignored.
func TestARedeliveredBatchMintsNothing(t *testing.T) {
	r, spawn := startBatchRig(t, 2)
	ctx := context.Background()
	conv := "discord:g1/t-batch-again"
	exec, origin, _ := sessionTurn(t, r, spawn, conv, "x")
	art := delegateBatchArtifact(t, platformTask("a"), platformTask("b"))
	if err := exec.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	awaitSubmission(t, r, targetPlatform, 1)
	if err := exec.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the repeat ignored", loggedContaining(r, "delegation ignored", "rule="+ruleDelegationBusy, origin.TaskID))
	if n := platformSubmissions(t, r); n != 2 {
		t.Fatalf("platform received %d submissions, want 2", n)
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if pref, _ := rec.TaskRefFor(origin.TaskID); len(pref.Children) != 2 {
		t.Fatalf("parent children = %v", pref.Children)
	}
}

// TestABatchIsRefusedWhileAnEarlierChildIsLive: one live delegation per
// conversation still holds; a batch while an earlier turn's child runs,
// released from the active task but not stopped, is refused, naming that
// child.
func TestABatchIsRefusedWhileAnEarlierChildIsLive(t *testing.T) {
	r, spawn := startBatchRig(t, 3)
	ctx := context.Background()
	conv := "discord:g1/t-batch-live"
	exec, _, _ := sessionTurn(t, r, spawn, conv, "x")
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, targetPlatform, "first"))
	child := r.awaitTask(t, targetPlatform)
	_ = exec.PublishStatus(ctx, lib.StateCompleted, true)
	waitFor(t, "child active", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == child.TaskID
	})
	putRecord(t, r, conv, func(rec *SessionRecord) { rec.ActiveTask = nil })

	exec2, origin2, _ := sessionTurn(t, r, spawn, conv, "again")
	_ = exec2.PublishArtifact(ctx, delegateBatchArtifact(t, platformTask("a"), platformTask("b")))
	waitFor(t, "busy refusal", loggedContaining(r, "delegation refused", "rule="+ruleDelegationBusy, origin2.TaskID, "child="+child.TaskID))
	_ = exec2.PublishStatus(ctx, lib.StateCompleted, true)
	waitFor(t, "notice", postedContaining(r, "⚠️ delegation refused: a delegated task is still running (task "+child.TaskID+")"))
	if n := platformSubmissions(t, r); n != 1 {
		t.Fatalf("platform received %d submissions, want 1", n)
	}
}

// TestABatchThatFailsPartwayLeavesNoChain: the first child reaches the bus
// and the second can't; the first is canceled and dropped with it, so the
// record is as it was before the request: the parent active on its session,
// no child entries, no links and no index entries.
func TestABatchThatFailsPartwayLeavesNoChain(t *testing.T) {
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r, spawn := startRigWithSpawnerCap(t, targetPlatform, 0, func(c *Config) {
		c.DelegationWidthMax = 2
		c.ScopeFile = scope
		c.Namespace = "kubeagents-system"
	})
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams})
	ctx := context.Background()
	conv := "discord:g1/t-batch-partway"
	exec, origin, session := sessionTurn(t, r, spawn, conv, "x")
	closeProfileSubmissions(t, r.url)

	art := delegateBatchArtifact(t, platformTask("first"),
		lib.DelegateTask{Addressee: "cluster", Text: "second", Parameters: targetParams("p1", "us-east1", "dev")})
	if err := exec.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	first := awaitSubmission(t, r, targetPlatform, 0)
	waitFor(t, "the second child's publish failure", loggedContaining(r, "task publish failed"))
	waitFor(t, "the first child dropped", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		_, has := rec.TaskRefFor(first.TaskID)
		return !has
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID || rec.Addressee != session {
		t.Fatalf("active=%+v addressee=%s, want the parent on its session", rec.ActiveTask, rec.Addressee)
	}
	pref, _ := rec.TaskRefFor(origin.TaskID)
	if len(pref.Children) != 0 {
		t.Fatalf("the parent links children of a failed batch: %v", pref.Children)
	}
	if !strings.Contains(pref.DelegationEnd, reasonDelegationNotStarted) {
		t.Fatalf("the parent's DelegationEnd = %q", pref.DelegationEnd)
	}
	for _, ref := range rec.Tasks {
		if ref.Role == taskRoleChild || ref.ParentTaskID != "" {
			t.Fatalf("a child entry survived the failed batch: %+v", ref)
		}
	}
	if key, err := r.g.reg.SessionForTask(ctx, first.TaskID); err != nil || key != "" {
		t.Fatalf("the withdrawn child is still indexed: %q %v", key, err)
	}
	// It wrote an ingress line, so it gets a terminal line too.
	waitFor(t, "the withdrawn child's terminal line", loggedContaining(r, "task terminal", "taskId="+first.TaskID, "state=canceled", "reason="+reasonBatchWithdrawn))
	// The child that did reach the bus is told to stop, so nothing runs
	// for a request the record no longer holds.
	waitFor(t, "a cancel for the first child", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, targetPlatform) {
			if e.Kind == lib.KindCancel && e.TaskID == first.TaskID {
				return true
			}
		}
		return false
	})
}

// closeProfileSubmissions moves the profile submission stream off the
// subjects the gateway submits on, so a profile task's publish fails while
// platform's still lands.
func closeProfileSubmissions(t *testing.T, url string) {
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
	stream, err := js.Stream(ctx, lib.ProfileSubmitStream)
	if err != nil {
		t.Fatal(err)
	}
	cfg := stream.CachedInfo().Config
	cfg.Subjects = []string{lib.SubmitSubjectPrefix + "closed.>"}
	if _, err := js.UpdateStream(ctx, cfg); err != nil {
		t.Fatal(err)
	}
}

// startBatchProfileRig is a session rig taking batches up to width whose
// gateway also knows the generic cluster profile and a profile that takes no
// target, under a scope that lists project p1. tweak, when set, edits the
// Config further.
func startBatchProfileRig(t *testing.T, width int, tweak func(*Config)) (*rig, *fakeSpawner) {
	t.Helper()
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r, spawn := startRigWithSpawnerCap(t, targetPlatform, 0, func(c *Config) {
		c.DelegationWidthMax = width
		c.ScopeFile = scope
		c.Namespace = "kubeagents-system"
		if tweak != nil {
			tweak(c)
		}
	})
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams})
	r.g.profiles.put("triage", &lib.AgentCard{Name: "triage"})
	return r, spawn
}

// TestOnlyTheSecondEntryFailingRefusesTheBatch: every entry meets the
// target's checks, not only the first. The first entry here (platform, no
// list) passes all of them, and the second (triage) fails one: the batch is
// refused under that rule, naming triage, with nothing minted. The steer
// over-cap refusal isn't here: it doesn't depend on the target, so the first
// entry meets it the same way.
func TestOnlyTheSecondEntryFailingRefusesTheBatch(t *testing.T) {
	off := func(r *rig) TaskRequester {
		return TaskRequester{Backend: "discord", Subject: requesterSubject(r.g.ps, "discord", "1002")}
	}
	for _, tc := range []struct {
		name    string
		backend string
		tweak   func(c *Config)
		edit    func(r *rig, rec *SessionRecord, parent string)
		rule    string
		field   string
	}{
		{name: "the target's list", rule: ruleDelegationAllowedUsers,
			tweak: func(c *Config) {
				c.TargetAllowedUsers = map[string]map[string][]string{"triage": {"discord": {"1002"}}}
			}},
		{name: "the door's own list", backend: a2aBackend, rule: ruleDelegationDoorUnlisted,
			tweak: func(c *Config) {
				armDoorMap(t, c)
				c.TargetAllowedUsers = map[string]map[string][]string{targetPlatform: {a2aBackend: {"1001"}}}
			}},
		{name: "a steer author", rule: ruleDelegationSteerAuthor, field: "steerBackend=discord",
			tweak: func(c *Config) {
				c.TargetAllowedUsers = map[string]map[string][]string{"triage": {"discord": {"1001"}}}
			},
			edit: func(r *rig, rec *SessionRecord, parent string) {
				for i := range rec.Tasks {
					if rec.Tasks[i].ID == parent {
						rec.Tasks[i].SteerAuthors = append(rec.Tasks[i].SteerAuthors, off(r))
					}
				}
			}},
		{name: "an incarnation-set member", rule: ruleDelegationSessionAuthor, field: "sessionBackend=discord",
			tweak: func(c *Config) {
				c.TargetAllowedUsers = map[string]map[string][]string{"triage": {"discord": {"1001"}}}
			},
			edit: func(r *rig, rec *SessionRecord, _ string) { rec.addSessionAuthor(off(r)) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, spawn := startBatchProfileRig(t, 2, tc.tweak)
			ctx := context.Background()
			conv := "discord:g1/t-batch-second"
			if tc.backend == a2aBackend {
				conv = "a2a:agent-1001/ctx-batch-second"
			}
			exec, origin, _ := sessionTurnVia(t, r, spawn, conv, tc.backend, "x")
			if tc.edit != nil {
				putRecord(t, r, conv, func(rec *SessionRecord) { tc.edit(r, rec, origin.TaskID) })
			}
			art := delegateBatchArtifact(t, platformTask("first"), lib.DelegateTask{Addressee: "triage", Text: "second"})
			if err := exec.PublishArtifact(ctx, art); err != nil {
				t.Fatal(err)
			}
			waitFor(t, "refusal line", loggedContaining(r, "delegation refused", "rule="+tc.rule, origin.TaskID, tc.field))
			completeTask(t, exec, "delegated 2 tasks to platform, triage")
			waitFor(t, "the notice names the second target", postedContaining(r, fmt.Sprintf(noticeDelegationNotAllowed, "triage")))
			if n := platformSubmissions(t, r); n != 0 {
				t.Fatalf("a refused batch minted %d platform children", n)
			}
			if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"triage.>"); len(envs) != 0 {
				t.Fatalf("a refused batch submitted %d triage children", len(envs))
			}
		})
	}
}

// TestAProfileParentsBatchIsIgnored: a profile task waits open for its child,
// and nothing yet lets it take several child results, so a batch from one is
// ignored under its own rule (delegation.profile-batch), with nothing
// minted. This rig captures no log, so the record's end is the observation.
func TestAProfileParentsBatchIsIgnored(t *testing.T) {
	r := startProfileRig(t)
	r.g.cfg.DelegationWidthMax = 3
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	ctx := context.Background()
	conv := "console:tab-profile-batch"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	art := delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
		lib.DelegateTask{Addressee: "cluster", Text: "b", Parameters: targetParams("p1", "us-east1", "b")})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the request recorded as minting nothing", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && strings.Contains(ref.DelegationEnd, whyProfileBatch)
	})
	if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>"); len(envs) != 0 {
		t.Fatalf("a profile parent's batch submitted %d children", len(envs))
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if pref, _ := rec.TaskRefFor(origin.TaskID); len(pref.Children) != 0 {
		t.Fatalf("parent children = %v", pref.Children)
	}
}

// TestAWithdrawnChildWhoseCancelFailedStaysOnRecord: when the second child
// can't be submitted and the first one's cancel can't be published either,
// the first may still run, so its entry stays, marked canceled, linked from
// the parent and indexed, where a later stop and the reaper can find it. Its
// stop record is still written for the dispatcher.
func TestAWithdrawnChildWhoseCancelFailedStaysOnRecord(t *testing.T) {
	r, spawn := startBatchProfileRig(t, 2, nil)
	ctx := context.Background()
	conv := "discord:g1/t-batch-cancel-fails"
	exec, origin, session := sessionTurn(t, r, spawn, conv, "x")
	// TASKS keeps only the session's own .in: platform's submission and
	// the cluster child's cancel both fail, its submission doesn't.
	narrowTasksStream(t, r.url, session)
	art := delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "cluster", Text: "first", Parameters: targetParams("p1", "us-east1", "dev")},
		platformTask("second"))
	if err := exec.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	first := submissionOn(t, r, "cluster")
	waitFor(t, "the failed cancel", loggedContaining(r, "cancel publish failed for a withdrawn batch child", first.TaskID))
	waitFor(t, "the second child's entry dropped", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return len(childrenOf(rec, origin.TaskID)) == 1
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	kept := childrenOf(rec, origin.TaskID)
	if kept[0].ID != first.TaskID || !kept[0].Canceled {
		t.Fatalf("kept child = %+v, want %s marked canceled", kept[0], first.TaskID)
	}
	if pref, _ := rec.TaskRefFor(origin.TaskID); len(pref.Children) != 1 || pref.Children[0] != first.TaskID {
		t.Fatalf("parent children = %v, want the kept child", pref.Children)
	}
	if key, _ := r.g.reg.SessionForTask(ctx, first.TaskID); key != conv {
		t.Fatalf("the kept child's index = %q, want %q", key, conv)
	}
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID || rec.Addressee != session {
		t.Fatalf("active=%+v addressee=%s, want the parent on its session", rec.ActiveTask, rec.Addressee)
	}
	if _, ok := dispatchStopFor(t, r.url, "cluster", first.TaskID); !ok {
		t.Fatal("no stop record for the kept child")
	}
}

// TestABatchChildsFinishedLineKeepsItsNote: a batch child's rolling line
// carries "(delegated to <addressee>)" from its placeholder through its
// terminal edit, so closed sibling lines can still be told apart.
func TestABatchChildsFinishedLineKeepsItsNote(t *testing.T) {
	r, spawn := startBatchRig(t, 2)
	ctx := context.Background()
	conv := "discord:g1/t-batch-note"
	exec, _, _ := sessionTurn(t, r, spawn, conv, "check two things")
	if err := exec.PublishArtifact(ctx, delegateBatchArtifact(t, platformTask("one"), platformTask("two"))); err != nil {
		t.Fatal(err)
	}
	first := awaitSubmission(t, r, targetPlatform, 0)
	awaitSubmission(t, r, targetPlatform, 1)
	note := fmt.Sprintf(delegatedProfileLineNote, targetPlatform)
	if targetPlatform == "platform" {
		note = delegatedLineNote
	}
	child := r.execFor(t, first, targetPlatform)
	if err := child.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the child's line closed with its note", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.HasPrefix(e, terminalLine(lib.StateCompleted, "")) && strings.HasSuffix(e, " "+note) {
				return true
			}
		}
		return false
	})
}

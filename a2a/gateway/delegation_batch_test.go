package gateway

import (
	"context"
	"encoding/json"
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
// conversation still holds; a batch while an earlier turn's child runs is
// refused, naming that child.
func TestABatchIsRefusedWhileAnEarlierChildIsLive(t *testing.T) {
	r, spawn := startBatchRig(t, 3)
	ctx := context.Background()
	conv := "discord:g1/t-batch-live"
	exec, _, _ := sessionTurn(t, r, spawn, conv, "x")
	_ = exec.PublishArtifact(ctx, delegateArtifact(t, targetPlatform, "first"))
	child := r.awaitTask(t, targetPlatform)
	_ = exec.PublishStatus(ctx, lib.StateCompleted, true)
	sessionRigTurn(r, conv, "stop-1", "stop")
	waitFor(t, "child detached", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == child.TaskID && rec.ActiveTask.Detached
	})

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

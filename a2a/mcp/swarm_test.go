package mcp

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
	"unicode"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

var updateSwarmGolden = flag.Bool("update-swarm-golden", false, "rewrite a2a/mcp/testdata/swarm_*.golden.json")

var swarmNow = time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)

func textArtifact(name, text string) lib.Artifact {
	return lib.Artifact{Name: name, Parts: []lib.Part{{Kind: "text", Text: text}}}
}

func activityArtifact(tools ...string) lib.Artifact {
	a := lib.Artifact{Name: lib.ArtifactActivity}
	for _, tool := range tools {
		a.Parts = append(a.Parts, lib.Part{Kind: "data", Data: json.RawMessage(fmt.Sprintf(`{"tool":%q}`, tool))})
	}
	return a
}

func runningTask(id string, arts ...lib.Artifact) *lib.Task {
	return &lib.Task{ID: id, State: lib.StateWorking, Artifacts: arts}
}

func finishedTask(id string, state lib.TaskState, result string) *lib.Task {
	t := &lib.Task{ID: id, State: state, Final: true}
	if result != "" {
		t.Artifacts = []lib.Artifact{textArtifact(lib.ArtifactResult, result)}
	}
	return t
}

// fixtureInput is the golden input: two conversations (one a child of the
// other), a running session turn, a completed platform turn, a delegation off
// it, an input-required turn and a failed one.
func fixtureInput() SwarmInput {
	return SwarmInput{
		Title: "kube-agents · demo",
		Conversations: []SwarmConversation{
			{Alias: "triage", Token: "tok-triage-0001", Description: "Cluster triage", LastUsed: swarmNow.Add(-5 * time.Minute)},
			{Alias: "triage-db", Token: "tok-db-0002", Description: "Database deep dive", Parent: "triage", LastUsed: swarmNow.Add(-2 * time.Minute)},
			{Alias: "stale", Token: "tok-stale-0003", Description: "Old work", LastUsed: swarmNow.Add(-48 * time.Hour)},
		},
		Tasks: []SwarmTask{
			{Addressee: "platform", TaskID: "t-100", Conversation: "console:tok-triage-0001", Submitted: swarmNow.Add(-30 * time.Minute),
				Prompt: "Why are the checkout pods restarting?\nPlease check events.",
				Task:   finishedTask("t-100", lib.StateCompleted, "## Summary\nAll nodes Ready.\nmore detail")},
			{Addressee: "db-specialist", TaskID: "t-101", Conversation: "console:tok-triage-0001", ViaAddressee: "platform", ViaTaskID: "t-100",
				Submitted: swarmNow.Add(-29 * time.Minute), Prompt: "Check replica lag on orders-db", Task: finishedTask("t-101", lib.StateCompleted, "Replica lag 2s.")},
			{Addressee: "session-db-pod", TaskID: "t-102", Conversation: "console:tok-db-0002", Submitted: swarmNow.Add(-10 * time.Minute),
				Task: runningTask("t-102", activityArtifact("kubectl_get", "psql_query"))},
			{Addressee: "platform", TaskID: "t-103", Conversation: "console:tok-triage-0001", Submitted: swarmNow.Add(-8 * time.Minute),
				Task: &lib.Task{ID: "t-103", State: lib.StateInputRequired, StatusHistory: []lib.TaskState{lib.StateSubmitted, lib.StateWorking, lib.StateInputRequired},
					Artifacts: []lib.Artifact{textArtifact(lib.ArtifactProgress, "Checking rollout history")}},
				Question: &lib.Message{Parts: []lib.Part{{Kind: "text", Text: "Approve the restart?"}}}},
			{Addressee: "platform", TaskID: "t-104", Conversation: "console:tok-triage-0001", Submitted: swarmNow.Add(-6 * time.Minute),
				Task: &lib.Task{ID: "t-104", State: lib.StateFailed, Final: true, FinalMessage: &lib.Message{Parts: []lib.Part{{Kind: "text", Text: "reason: quota - out of tokens"}}}}},
		},
		Cards: map[string]AgentCard{
			"platform":      {Name: "Platform Agent"},
			"db-specialist": {Name: "DB Specialist"},
		},
		Now: swarmNow,
	}
}

func swarmJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(b, '\n')
}

func freshLedger() BoardLedger { return BoardLedger{} }

func TestBuildSwarmStateEmpty(t *testing.T) {
	snap, ledger := BuildSwarmState(SwarmInput{Now: swarmNow}, freshLedger())
	if snap.Title != swarmDefaultTitle {
		t.Errorf("title = %q", snap.Title)
	}
	if len(snap.Members) != 3 {
		t.Errorf("members = %v, want coordinator, narrator, platform", snap.Members)
	}
	if len(snap.NewMessages) != 1 || !strings.HasPrefix(snap.NewMessages[0].Text, "Connected @platform") {
		t.Errorf("new messages = %+v, want the one connect post", snap.NewMessages)
	}
	if snap.ViewHTML != "" {
		t.Error("ViewHTML set; the bridge renders the view")
	}
	if ledger.NextSeq != 2 || ledger.SchemaVersion != swarmLedgerSchema || len(ledger.EventSeqs) != 1 {
		t.Errorf("ledger = %+v", ledger)
	}
	if snap.View.Attention == nil || snap.View.Risks == nil || len(snap.View.Workstreams) != 1 || len(snap.View.Milestones) != 1 {
		t.Errorf("view = %+v", snap.View)
	}
	// The zero Now is still deterministic: no clock is read.
	a, _ := BuildSwarmState(SwarmInput{}, freshLedger())
	b, _ := BuildSwarmState(SwarmInput{}, freshLedger())
	if !bytes.Equal(swarmJSON(t, a), swarmJSON(t, b)) {
		t.Error("zero Now is not deterministic")
	}
}

func TestBuildSwarmStateRunningAndCompleted(t *testing.T) {
	in := SwarmInput{
		Title:         "demo",
		Conversations: []SwarmConversation{{Alias: "ops", Token: "tok-ops", Description: "Ops", LastUsed: swarmNow.Add(-time.Minute)}},
		Tasks: []SwarmTask{
			{Addressee: "session-ops-pod", TaskID: "run-1", Conversation: "console:tok-ops", Submitted: swarmNow.Add(-3 * time.Minute),
				Task: runningTask("run-1", activityArtifact("kubectl_logs"))},
			{Addressee: "platform", TaskID: "done-1", Conversation: "console:tok-ops", Submitted: swarmNow.Add(-4 * time.Minute),
				Task: finishedTask("done-1", lib.StateCompleted, "Rolled out v2")},
		},
		Now: swarmNow,
	}
	snap, _ := BuildSwarmState(in, freshLedger())
	m, ok := snap.Members["session-ops"]
	if !ok {
		t.Fatalf("no session-ops member: %v", snap.Members)
	}
	if m.WakeMode != swarmWakeWorking || !strings.Contains(m.Model, "kubectl_logs") {
		t.Errorf("ops member = %+v, want working on kubectl_logs", m)
	}
	if snap.Members[swarmMemberPlatform].WakeMode != swarmWakeMentions {
		t.Error("platform is working with no running platform task")
	}
	texts := boardTexts(snap)
	for _, want := range []string{
		"@session-ops joined from console conversation `ops` — `Ops`.",
		"@platform [task `done-1`] submitted to `platform`.",
		"@coordinator `completed` on #",
		"`Rolled out v2`",
		"@session-ops [task `run-1`] submitted to `session-ops-pod`.",
	} {
		if !strings.Contains(texts, want) {
			t.Errorf("board lacks %q:\n%s", want, texts)
		}
	}
	if snap.View.Phase.Label != "Executing 1 Task(s)" {
		t.Errorf("phase = %+v", snap.View.Phase)
	}
	if len(snap.View.Milestones) != 1 || !strings.Contains(snap.View.Milestones[0].Text, "done-1") {
		t.Errorf("milestones = %+v", snap.View.Milestones)
	}
	// Board order follows submission, with each completion after its own submission.
	seqOf := func(sub string) int {
		for _, m := range snap.NewMessages {
			if strings.Contains(m.Text, sub) {
				return m.Seq
			}
		}
		t.Fatalf("no post containing %q", sub)
		return 0
	}
	if !(seqOf("[task `done-1`]") < seqOf("[task `run-1`]") && seqOf("[task `done-1`]") < seqOf("`completed`")) {
		t.Errorf("board out of order:\n%s", texts)
	}
}

func TestBuildSwarmStateDelegated(t *testing.T) {
	in := fixtureInput()
	snap, _ := BuildSwarmState(in, freshLedger())
	var in101, done101, in100 *SwarmBoardMessage
	for i := range snap.NewMessages {
		m := &snap.NewMessages[i]
		switch {
		case strings.Contains(m.Text, "[task `t-100`]"):
			in100 = m
		case strings.Contains(m.Text, "delegated task `t-101`"):
			in101 = m
		case strings.Contains(m.Text, "(`t-101`)"):
			done101 = m
		}
	}
	if in100 == nil || in101 == nil || done101 == nil {
		t.Fatalf("missing posts:\n%s", boardTexts(snap))
	}
	// The delegation is posted by the parent task's owner and cites it.
	if in101.Agent != swarmMemberPlatform || !strings.Contains(in101.Text, fmt.Sprintf("(#%d)", in100.Seq)) {
		t.Errorf("delegation post = %+v, want from platform citing #%d", in101, in100.Seq)
	}
	if !strings.Contains(in101.Text, "`db-specialist` (`DB Specialist`)") {
		t.Errorf("delegation post lacks the card name: %q", in101.Text)
	}
	// The completion answers the delegating parent's owner.
	if !strings.HasPrefix(done101.Text, "@platform `completed` on #") {
		t.Errorf("delegated completion = %q", done101.Text)
	}
}

func TestBuildSwarmStateLedgerDedupe(t *testing.T) {
	in := fixtureInput()
	snap1, l1 := BuildSwarmState(in, freshLedger())
	if len(snap1.NewMessages) == 0 {
		t.Fatal("first call emitted nothing")
	}
	// Round-trip the ledger through JSON, as the bridge does.
	var back BoardLedger
	if err := json.Unmarshal(swarmJSON(t, l1), &back); err != nil {
		t.Fatal(err)
	}
	snap2, l2 := BuildSwarmState(in, back)
	if len(snap2.NewMessages) != 0 {
		t.Errorf("second call re-emitted:\n%s", boardTexts(snap2))
	}
	if !bytes.Equal(swarmJSON(t, l1), swarmJSON(t, l2)) {
		t.Error("ledger changed on an identical second call")
	}
	// A task finishing later posts exactly its completion, citing its submission.
	in.Tasks[2].Task = finishedTask("t-102", lib.StateCompleted, "query plan fixed")
	snap3, _ := BuildSwarmState(in, l2)
	if len(snap3.NewMessages) != 1 || snap3.NewMessages[0].Seq != l2.NextSeq ||
		!strings.Contains(snap3.NewMessages[0].Text, fmt.Sprintf("on #%d", l2.EventSeqs["task-in:t-102"])) {
		t.Errorf("third call = %+v", snap3.NewMessages)
	}
}

func TestBuildSwarmStateCaps(t *testing.T) {
	in := SwarmInput{Title: "big", Now: swarmNow}
	for i := 0; i < 800; i++ {
		tok := fmt.Sprintf("tok-%04d", i)
		in.Conversations = append(in.Conversations, SwarmConversation{
			Alias: fmt.Sprintf("conv-%04d", i), Token: tok, Description: "d", LastUsed: swarmNow.Add(-time.Duration(i) * time.Second),
		})
		in.Tasks = append(in.Tasks, SwarmTask{
			Addressee: "platform", TaskID: fmt.Sprintf("task-%04d", i), Conversation: "console:" + tok,
			Submitted: swarmNow.Add(-time.Duration(i) * time.Second), Task: finishedTask("x", lib.StateFailed, "boom"),
		})
	}
	huge := BoardLedger{NextSeq: 1 << 20, EventSeqs: map[string]int{}, Authors: map[string]bool{}}
	for i := 0; i < 50000; i++ {
		huge.EventSeqs[fmt.Sprintf("old:%d", i)] = i + 1
		huge.Authors[fmt.Sprintf("author-%d", i)] = true
	}
	snap, l := BuildSwarmState(in, huge)
	if len(snap.Members) > maxSwarmMembers {
		t.Errorf("members = %d > %d", len(snap.Members), maxSwarmMembers)
	}
	if len(snap.NewMessages) != maxNewBoardMessages {
		t.Errorf("new messages = %d, want %d", len(snap.NewMessages), maxNewBoardMessages)
	}
	if len(l.EventSeqs) > maxLedgerEvents || len(l.Authors) > maxLedgerAuthors {
		t.Errorf("ledger out: %d events, %d authors", len(l.EventSeqs), len(l.Authors))
	}
	v := snap.View
	if len(v.Attention) > maxAttentionItemsInView || len(v.Workstreams) > maxWorkstreamsInView || len(v.Milestones) > maxMilestonesInView {
		t.Errorf("view lists: %d attention, %d workstreams, %d milestones", len(v.Attention), len(v.Workstreams), len(v.Milestones))
	}
	// The backlog drains over later calls without re-emitting anything.
	seen := map[int]bool{}
	for _, m := range snap.NewMessages {
		seen[m.Seq] = true
	}
	for call := 0; call < 20; call++ {
		snap, l = BuildSwarmState(in, l)
		for _, m := range snap.NewMessages {
			if seen[m.Seq] {
				t.Fatalf("seq %d emitted twice", m.Seq)
			}
			seen[m.Seq] = true
		}
		if len(l.EventSeqs) > maxLedgerEvents {
			t.Fatalf("ledger grew to %d", len(l.EventSeqs))
		}
		if len(snap.NewMessages) == 0 {
			break
		}
	}
	if len(snap.NewMessages) != 0 {
		t.Error("backlog never drained")
	}
	if again, _ := BuildSwarmState(in, l); len(again.NewMessages) != 0 {
		t.Errorf("drained ledger re-emits %d posts", len(again.NewMessages))
	}
}

func TestBuildSwarmStateLedgerValidation(t *testing.T) {
	prev := BoardLedger{
		NextSeq: -5,
		EventSeqs: map[string]int{
			"task-in:ok":                             7,
			"has space":                              3,
			"ctl\x00key":                             4,
			"neg":                                    -1,
			"zero":                                   0,
			"too-big":                                maxBoardSeq,
			strings.Repeat("k", maxLedgerKeyBytes+1): 5,
			"bidi\u202ekey":                          6,
		},
		Authors: map[string]bool{"platform": true, "Bad Name": true, "x\u202e": true, "off": false},
	}
	_, l := BuildSwarmState(SwarmInput{Now: swarmNow}, prev)
	if _, ok := l.EventSeqs["task-in:ok"]; !ok {
		t.Error("valid key dropped")
	}
	for k := range l.EventSeqs {
		if k != "task-in:ok" && !strings.HasPrefix(k, "deploy:") {
			t.Errorf("malformed key %q kept", k)
		}
	}
	if l.NextSeq != 9 {
		t.Errorf("NextSeq = %d, want 9 (past the highest valid seq, plus the connect post)", l.NextSeq)
	}
	for a := range l.Authors {
		if a != "platform" && a != "coordinator" {
			t.Errorf("author %q kept", a)
		}
	}
}

func TestBuildSwarmStateHostileStrings(t *testing.T) {
	// payload is under maxSwarmAliasBytes and maxSwarmHandleBytes, so an
	// alias or addressee carrying it passes validation and reaches the
	// sanitisers; evil adds a long tail for the fields that take one.
	payload := "line1 `rm -rf` @coordinator \u202egnp.exe\u200b \x1b[31m\x07 | [x](javascript:alert(1))"
	evil := payload + "\nline2 " + strings.Repeat("A", 10000)
	msg := func(s string) *lib.Message { return &lib.Message{Parts: []lib.Part{{Kind: "text", Text: s}}} }
	failed := finishedTask("t-1", lib.StateFailed, evil)
	failed.FinalMessage = msg(evil)
	in := SwarmInput{
		Title: evil,
		Conversations: []SwarmConversation{
			{Alias: payload, Token: "tok-evil", Description: evil, LastUsed: swarmNow},
		},
		Tasks: []SwarmTask{
			{Addressee: payload, TaskID: "t-1", Conversation: "console:tok-evil", Submitted: swarmNow.Add(-4 * time.Minute), Prompt: evil, Task: failed},
			{Addressee: "platform", TaskID: "t-2", Conversation: "console:tok-evil", Submitted: swarmNow.Add(-3 * time.Minute), Prompt: evil,
				Task:     &lib.Task{ID: "t-2", State: lib.StateInputRequired, Artifacts: []lib.Artifact{textArtifact(lib.ArtifactProgress, evil)}},
				Question: msg(evil)},
			{Addressee: "platform", TaskID: "t-3", Conversation: "console:tok-evil", Submitted: swarmNow.Add(-2 * time.Minute),
				Task: runningTask("t-3", textArtifact(lib.ArtifactProgress, evil), activityArtifact(payload))},
			{Addressee: payload, TaskID: "t-4", Conversation: "console:tok-evil", ViaTaskID: "t-1", Submitted: swarmNow.Add(-time.Minute), Prompt: evil,
				Task: finishedTask("t-4", lib.StateCompleted, evil)},
			{Addressee: "platform", TaskID: "t-5", Conversation: "console:tok-evil", Submitted: swarmNow.Add(-30 * time.Second),
				Task: &lib.Task{ID: "t-5", State: lib.StateFailed, Final: true, FinalMessage: msg(evil)}},
			{Addressee: "platform", TaskID: "t-6", Conversation: "console:tok-evil", Submitted: swarmNow.Add(-20 * time.Second),
				Task: &lib.Task{ID: "t-6", State: lib.StateInputRequired, Artifacts: []lib.Artifact{textArtifact(lib.ArtifactProgress, evil)}}},
			{Addressee: "platform", TaskID: "bad id `x`", Submitted: swarmNow},
			{Addressee: "platform", TaskID: strings.Repeat("t", 500), Submitted: swarmNow},
		},
		Cards: map[string]AgentCard{"platform": {Name: evil}, payload: {Name: evil}},
		Now:   swarmNow,
	}
	snap, l := BuildSwarmState(in, freshLedger())
	check := func(where, s string, maxRunes int) {
		t.Helper()
		for _, r := range s {
			if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
				t.Errorf("%s carries %U: %q", where, r, s)
				return
			}
		}
		if n := len([]rune(s)); n > maxRunes {
			t.Errorf("%s is %d runes > %d", where, n, maxRunes)
		}
	}
	// The fixture must reach the board and the view, or the checks below
	// run over nothing.
	texts := boardTexts(snap)
	for _, want := range []string{"joined from console conversation", "[task `t-1`]", "`failed`", "input required", "delegated task `t-4`", "`completed`"} {
		if !strings.Contains(texts, want) {
			t.Fatalf("hostile fixture did not reach the board (%q missing):\n%s", want, texts)
		}
	}
	if len(snap.View.Attention) < 2 || len(snap.View.Milestones) < 2 {
		t.Fatalf("hostile fixture did not reach the view: %+v", snap.View)
	}
	check("title", snap.Title, maxSwarmTitleChars)
	for name, m := range snap.Members {
		if !validMemberName(name) || name != m.Name {
			t.Errorf("member name %q", name)
		}
		check("model "+name, m.Model, 400)
	}
	for _, m := range snap.NewMessages {
		check("board", m.Text, 1200)
		if strings.Count(m.Text, "@") > 1 {
			t.Errorf("board post mentions more than its addressee: %q", m.Text)
		}
		if strings.Count(m.Text, "`")%2 != 0 {
			t.Errorf("board post has an unbalanced code span: %q", m.Text)
		}
		if strings.Contains(m.Text, "bad id") || strings.Contains(m.Text, "ttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttt") {
			t.Errorf("invalid task id reached the board: %q", m.Text)
		}
	}
	v := snap.View
	check("headline", v.Headline, maxViewHeadlineChars)
	for _, a := range v.Attention {
		check("attention title", a.Title, maxViewTitleChars)
		check("attention why", a.Why, maxViewWhyChars)
	}
	for _, w := range v.Workstreams {
		check("workstream title", w.Title, maxViewTitleChars)
		check("workstream note", w.Note, maxViewTextItemChars)
	}
	for _, ms := range v.Milestones {
		check("milestone", ms.Text, maxViewTextItemChars)
		if strings.Contains(ms.Text, "`") {
			t.Errorf("milestone carries a backtick: %q", ms.Text)
		}
	}
	for k := range l.EventSeqs {
		if !validLedgerKey(k) {
			t.Errorf("ledger key %q", k)
		}
	}
}

func TestBuildSwarmStateLineage(t *testing.T) {
	conv := func(alias, parent string) SwarmConversation {
		return SwarmConversation{Alias: alias, Token: "tok-" + alias, Parent: parent, LastUsed: swarmNow}
	}
	in := SwarmInput{Now: swarmNow, Conversations: []SwarmConversation{
		conv("orphan", "nobody"), // missing parent
		conv("selfish", "selfish"),
		conv("ping", "pong"), conv("pong", "ping"), // 2-cycle
		conv("root", ""), conv("kid", "root"),
	}}
	// A chain deeper than the cap: c00 <- c01 <- ... <- c11.
	for i := 0; i <= maxLineageDepth+3; i++ {
		parent := ""
		if i > 0 {
			parent = fmt.Sprintf("c%02d", i-1)
		}
		in.Conversations = append(in.Conversations, conv(fmt.Sprintf("c%02d", i), parent))
	}
	convs := normalizeConversations(in.Conversations, nil, swarmNow)
	resolveLineage(convs)
	got := map[string][2]string{}
	for _, c := range convs {
		got[c.alias] = [2]string{c.parent, c.root}
	}
	want := map[string][2]string{
		"orphan":                                {"", "orphan"},
		"selfish":                               {"", "selfish"},
		"ping":                                  {"", "ping"},
		"pong":                                  {"", "pong"},
		"root":                                  {"", "root"},
		"kid":                                   {"root", "root"},
		"c01":                                   {"c00", "c00"},
		fmt.Sprintf("c%02d", maxLineageDepth):   {fmt.Sprintf("c%02d", maxLineageDepth-1), "c00"},
		fmt.Sprintf("c%02d", maxLineageDepth+1): {"", fmt.Sprintf("c%02d", maxLineageDepth+1)},
	}
	for alias, w := range want {
		if got[alias] != w {
			t.Errorf("%s: parent,root = %v, want %v", alias, got[alias], w)
		}
	}
	snap, _ := BuildSwarmState(in, freshLedger())
	var rootWS *SwarmWorkstream
	for i := range snap.View.Workstreams {
		if snap.View.Workstreams[i].Title == "Conversation: root" {
			rootWS = &snap.View.Workstreams[i]
		}
	}
	if rootWS == nil || strings.Join(rootWS.Owners, ",") != "session-root,session-kid" {
		t.Errorf("root workstream = %+v", rootWS)
	}
	// A child whose alias yields no slug is named for its parent.
	in.Conversations = append(in.Conversations, conv("???", "root"))
	snap, _ = BuildSwarmState(in, freshLedger())
	if _, ok := snap.Members["session-root-sub"]; !ok {
		t.Errorf("no session-root-sub member: %v", memberNames(snap))
	}
}

func TestBuildSwarmStateDeterministic(t *testing.T) {
	in := fixtureInput()
	for i := 0; i < 40; i++ {
		in.Conversations = append(in.Conversations, SwarmConversation{
			Alias: fmt.Sprintf("dup-%d", i%7), Token: fmt.Sprintf("tok-%d", i), LastUsed: swarmNow.Add(-time.Duration(i%3) * time.Minute),
		})
		in.Tasks = append(in.Tasks, SwarmTask{
			Addressee: "platform", TaskID: fmt.Sprintf("d-%d", i%25), Conversation: fmt.Sprintf("console:tok-%d", i),
			Submitted: swarmNow.Add(-time.Duration(i%5) * time.Minute), Task: finishedTask("d", lib.StateCompleted, fmt.Sprint(i)),
		})
	}
	wantSnap, wantLedger := BuildSwarmState(in, freshLedger())
	want := append(swarmJSON(t, wantSnap), swarmJSON(t, wantLedger)...)
	r := rand.New(rand.NewPCG(1, 2))
	for i := 0; i < 20; i++ {
		shuf := in
		shuf.Conversations = append([]SwarmConversation(nil), in.Conversations...)
		shuf.Tasks = append([]SwarmTask(nil), in.Tasks...)
		r.Shuffle(len(shuf.Conversations), func(a, b int) {
			shuf.Conversations[a], shuf.Conversations[b] = shuf.Conversations[b], shuf.Conversations[a]
		})
		r.Shuffle(len(shuf.Tasks), func(a, b int) { shuf.Tasks[a], shuf.Tasks[b] = shuf.Tasks[b], shuf.Tasks[a] })
		s, l := BuildSwarmState(shuf, freshLedger())
		if got := append(swarmJSON(t, s), swarmJSON(t, l)...); !bytes.Equal(got, want) {
			t.Fatalf("shuffle %d changed the output", i)
		}
	}
}

func TestSwarmSnapshotGolden(t *testing.T) {
	snap, ledger := BuildSwarmState(fixtureInput(), freshLedger())
	for name, v := range map[string]any{"swarm_snapshot.golden.json": snap, "swarm_ledger.golden.json": ledger} {
		path := filepath.Join("testdata", name)
		got := swarmJSON(t, v)
		if *updateSwarmGolden {
			if err := os.WriteFile(path, got, 0o600); err != nil {
				t.Fatal(err)
			}
			continue
		}
		want, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("%v (run with -update-swarm-golden)", err)
		}
		if !bytes.Equal(got, want) {
			t.Errorf("%s differs; got:\n%s", name, got)
		}
	}
}

// TestSwarmGoldenBridgeCompatible decodes the goldens into the bridge's own
// types, refusing any field it does not know, and re-encodes them to the same
// bytes: what the gateway sends is exactly what #3689's bridge reads.
func TestSwarmGoldenBridgeCompatible(t *testing.T) {
	for name, target := range map[string]any{
		"swarm_snapshot.golden.json": &bridgeSwarmSnapshot{},
		"swarm_ledger.golden.json":   &bridgeBoardLedger{},
	} {
		raw, err := os.ReadFile(filepath.Join("testdata", name))
		if err != nil {
			t.Fatal(err)
		}
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.DisallowUnknownFields()
		if err := dec.Decode(target); err != nil {
			t.Fatalf("%s: bridge decode: %v", name, err)
		}
		if got := swarmJSON(t, target); !bytes.Equal(got, raw) {
			t.Errorf("%s: bridge re-encode differs:\n%s", name, got)
		}
	}
}

func boardTexts(s SwarmSnapshot) string {
	var b strings.Builder
	for _, m := range s.NewMessages {
		fmt.Fprintf(&b, "#%d %s: %s\n", m.Seq, m.Agent, m.Text)
	}
	return b.String()
}

func memberNames(s SwarmSnapshot) []string {
	var out []string
	for n := range s.Members {
		out = append(out, n)
	}
	return out
}

// The types below are copied verbatim from the workstation bridge
// (refs/remotes/pr/3689: a2a/mcpbridge/swarm_sink.go and swarm_overview.go),
// renamed with a bridge prefix so they can sit beside this package's own.

type bridgeSwarmMember struct {
	Name               string `json:"name"`
	Role               string `json:"role"`
	Model              string `json:"model,omitempty"`
	ConversationID     string `json:"conversation_id"`
	WakeMode           string `json:"wake_mode"`
	WaitSince          int    `json:"wait_since"`
	LastReadSeq        int    `json:"last_read_seq"`
	LastNudgedSeq      int    `json:"last_nudged_seq"`
	LastParkBlockedSeq int    `json:"last_park_blocked_seq"`
	JoinedAt           string `json:"joined_at,omitempty"`
	Killed             bool   `json:"killed"`
	ManagedBy          string `json:"managed_by,omitempty"`
}

type bridgeSwarmBoardMessage struct {
	Seq   int    `json:"seq"`
	Agent string `json:"agent"`
	Text  string `json:"text"`
	Time  string `json:"time"`
}

type bridgeSwarmRef struct {
	Seq    int    `json:"seq,omitempty"`
	Member string `json:"member,omitempty"`
}

type bridgeSwarmAction struct {
	Type   string `json:"type"`
	Target any    `json:"target"`
	Label  string `json:"label"`
}

type bridgeSwarmPhase struct {
	Label string `json:"label"`
	Index int    `json:"index"`
	Total int    `json:"total"`
}

type bridgeSwarmAttentionItem struct {
	ID       string              `json:"id"`
	Kind     string              `json:"kind"`
	Severity string              `json:"severity"`
	Title    string              `json:"title"`
	Why      string              `json:"why,omitempty"`
	Refs     []bridgeSwarmRef    `json:"refs"`
	Actions  []bridgeSwarmAction `json:"actions,omitempty"`
}

type bridgeSwarmWorkstream struct {
	ID      string              `json:"id"`
	Title   string              `json:"title"`
	Owners  []string            `json:"owners"`
	State   string              `json:"state"`
	Note    string              `json:"note,omitempty"`
	Refs    []bridgeSwarmRef    `json:"refs"`
	Actions []bridgeSwarmAction `json:"actions,omitempty"`
}

type bridgeSwarmTextItem struct {
	ID      string              `json:"id"`
	Text    string              `json:"text"`
	Refs    []bridgeSwarmRef    `json:"refs"`
	Actions []bridgeSwarmAction `json:"actions,omitempty"`
}

type bridgeSwarmView struct {
	Version     int                            `json:"version"`
	AsOfSeq     int                            `json:"as_of_seq"`
	UpdatedAt   string                         `json:"updated_at"`
	Headline    string                         `json:"headline"`
	Phase       *bridgeSwarmPhase              `json:"phase,omitempty"`
	Attention   []bridgeSwarmAttentionItem     `json:"attention"`
	Workstreams []bridgeSwarmWorkstream        `json:"workstreams"`
	Milestones  []bridgeSwarmTextItem          `json:"milestones"`
	Risks       []bridgeSwarmTextItem          `json:"risks"`
	KubeAgents  *bridgeKubeAgentsOverviewState `json:"kube_agents,omitempty"`
}

type bridgeBoardLedger struct {
	SchemaVersion int             `json:"schemaVersion,omitempty"`
	NextSeq       int             `json:"nextSeq"`
	EventSeqs     map[string]int  `json:"eventSeqs"`
	Authors       map[string]bool `json:"authors,omitempty"`
}

type bridgeSwarmSnapshot struct {
	Title       string                       `json:"title"`
	Members     map[string]bridgeSwarmMember `json:"members"`
	NewMessages []bridgeSwarmBoardMessage    `json:"newMessages,omitempty"`
	View        bridgeSwarmView              `json:"view"`
	ViewHTML    string                       `json:"viewHtml,omitempty"`
}

type bridgeKubeAgentsClusterCandidate struct {
	Name              string `json:"name"`
	ProjectID         string `json:"project_id,omitempty"`
	Location          string `json:"location,omitempty"`
	ClusterName       string `json:"cluster_name,omitempty"`
	KubeContext       string `json:"kube_context,omitempty"`
	Namespace         string `json:"namespace,omitempty"`
	PlatformAgentName string `json:"platform_agent_name,omitempty"`
	Mode              string `json:"mode,omitempty"`
	Version           string `json:"version,omitempty"`
	GitHubRepo        string `json:"github_repo,omitempty"`
	Ready             bool   `json:"ready"`
	StatusMessage     string `json:"status_message,omitempty"`
}

type bridgeKubeAgentsOverviewState struct {
	Connected                bool                               `json:"connected"`
	UpdatedAt                string                             `json:"updated_at"`
	RootConversationID       string                             `json:"root_conversation_id,omitempty"`
	ProjectID                string                             `json:"project_id,omitempty"`
	Location                 string                             `json:"location,omitempty"`
	ClusterName              string                             `json:"cluster_name,omitempty"`
	KubeContext              string                             `json:"kube_context,omitempty"`
	Namespace                string                             `json:"namespace,omitempty"`
	PlatformAgentName        string                             `json:"platform_agent_name,omitempty"`
	Mode                     string                             `json:"mode,omitempty"`
	Ready                    bool                               `json:"ready"`
	StatusMessage            string                             `json:"status_message,omitempty"`
	GitHubRepo               string                             `json:"github_repo,omitempty"`
	GitHubRepoURL            string                             `json:"github_repo_url,omitempty"`
	ChatPlatform             string                             `json:"chat_platform,omitempty"`
	NATSService              string                             `json:"nats_service,omitempty"`
	NATSPort                 int                                `json:"nats_port,omitempty"`
	BusUser                  string                             `json:"bus_user,omitempty"`
	BusEndpoint              string                             `json:"bus_endpoint,omitempty"`
	ActiveConversationAlias  string                             `json:"active_conversation_alias,omitempty"`
	ActiveConsoleToken       string                             `json:"active_console_token,omitempty"`
	PushArmed                bool                               `json:"push_armed"`
	SwarmTitle               string                             `json:"swarm_title,omitempty"`
	SwarmMemberNames         []string                           `json:"swarm_member_names,omitempty"`
	AgentProfiles            []string                           `json:"agent_profiles,omitempty"`
	ActiveSessionPodCount    int                                `json:"active_session_pod_count"`
	ClustersMonitored        int                                `json:"clusters_monitored,omitempty"`
	ToolExecutionsTotal      int                                `json:"tool_executions_total,omitempty"`
	RemediationsProposed     int                                `json:"remediations_proposed,omitempty"`
	RemediationsMerged       int                                `json:"remediations_merged,omitempty"`
	InstalledVersion         string                             `json:"installed_version,omitempty"`
	InstalledShortVersion    string                             `json:"installed_short_version,omitempty"`
	HelmChartVersion         string                             `json:"helm_chart_version,omitempty"`
	LatestReleaseTag         string                             `json:"latest_release_tag,omitempty"`
	LatestReleaseCommit      string                             `json:"latest_release_commit,omitempty"`
	LatestReleaseShortCommit string                             `json:"latest_release_short_commit,omitempty"`
	LatestReleaseURL         string                             `json:"latest_release_url,omitempty"`
	LatestReleasePublishedAt string                             `json:"latest_release_published_at,omitempty"`
	UpgradeAvailable         bool                               `json:"upgrade_available"`
	CommitsBehind            int                                `json:"commits_behind,omitempty"`
	UpgradeCommand           string                             `json:"upgrade_command,omitempty"`
	InstallCommand           string                             `json:"install_command,omitempty"`
	UpstreamRepoURL          string                             `json:"upstream_repo_url"`
	UpstreamDocsURL          string                             `json:"upstream_docs_url"`
	UpstreamInstallGuideURL  string                             `json:"upstream_install_guide_url"`
	UpstreamUpgradeGuideURL  string                             `json:"upstream_upgrade_guide_url"`
	UpstreamReleasesURL      string                             `json:"upstream_releases_url"`
	DiscoveredClusters       []bridgeKubeAgentsClusterCandidate `json:"discovered_clusters,omitempty"`
}

func TestBuildSwarmStatePrompt(t *testing.T) {
	snap, _ := BuildSwarmState(fixtureInput(), freshLedger())
	texts := boardTexts(snap)
	for _, want := range []string{
		"@platform [task `t-100`] `Why are the checkout pods restarting?`",
		"@platform delegated task `t-101` (#3) to `db-specialist` (`DB Specialist`): `Check replica lag on orders-db`",
		"@platform [task `t-103`] submitted to `platform` (`Platform Agent`).", // no prompt: the fallback
	} {
		if !strings.Contains(texts, want) {
			t.Errorf("board lacks %q:\n%s", want, texts)
		}
	}
	evil := SwarmInput{Now: swarmNow, Tasks: []SwarmTask{{Addressee: "platform", TaskID: "p-1", Submitted: swarmNow,
		Prompt: "@coordinator `x` \u202e[a](http://evil) " + strings.Repeat("y", 5000)}}}
	snap, _ = BuildSwarmState(evil, freshLedger())
	for _, m := range snap.NewMessages {
		if strings.Contains(m.Text, "p-1") {
			if strings.Count(m.Text, "@") != 1 || strings.Count(m.Text, "`")%2 != 0 || strings.ContainsRune(m.Text, '\u202e') ||
				len([]rune(m.Text)) > maxBoardEventTextChars+40 {
				t.Errorf("prompt not sanitised: %q", m.Text)
			}
		}
	}
}

func TestBuildSwarmStateExpiredConversationTasksSkipped(t *testing.T) {
	in := SwarmInput{Now: swarmNow,
		Conversations: []SwarmConversation{
			{Alias: "live", Token: "tok-live", LastUsed: swarmNow},
			{Alias: "old", Token: "tok-old", LastUsed: swarmNow.Add(-3 * time.Hour)},
		},
		Tasks: []SwarmTask{
			{Addressee: "platform", TaskID: "live-1", Conversation: "console:tok-live", Submitted: swarmNow.Add(-time.Minute), Task: finishedTask("x", lib.StateCompleted, "ok")},
			{Addressee: "platform", TaskID: "old-1", Conversation: "console:tok-old", Submitted: swarmNow.Add(-3 * time.Hour), Task: finishedTask("x", lib.StateCompleted, "ok")},
			{Addressee: "spec", TaskID: "old-2", ViaTaskID: "old-1", Submitted: swarmNow.Add(-time.Minute)}, // off-console child of a skipped task
			{Addressee: "platform", TaskID: "stranger-1", Conversation: "console:tok-unknown", Submitted: swarmNow.Add(-time.Minute)},
			{Addressee: "platform", TaskID: "slack-1", Submitted: swarmNow.Add(-time.Minute)},
		},
	}
	snap, _ := BuildSwarmState(in, freshLedger())
	texts := boardTexts(snap)
	for _, gone := range []string{"old-1", "old-2", "stranger-1", "session-old"} {
		if strings.Contains(texts, gone) {
			t.Errorf("board shows %s:\n%s", gone, texts)
		}
	}
	for _, kept := range []string{"live-1", "slack-1"} {
		if !strings.Contains(texts, kept) {
			t.Errorf("board lacks %s:\n%s", kept, texts)
		}
	}
}

func TestBuildSwarmStateViewIDsUnique(t *testing.T) {
	in := SwarmInput{Now: swarmNow}
	prefix := "task-0123456789-abcdefghij-0123456789-"
	for i, alias := range []string{"investigate-payment-service-latency", "investigate-payment-service-errors", "investigate-payment-service-latency-2"} {
		tok := fmt.Sprintf("tok-%d", i)
		in.Conversations = append(in.Conversations, SwarmConversation{Alias: alias, Token: tok, LastUsed: swarmNow})
	}
	for i := 0; i < 30; i++ {
		in.Tasks = append(in.Tasks,
			SwarmTask{Addressee: "platform", TaskID: fmt.Sprintf("%s%02d", prefix, i), Submitted: swarmNow.Add(-time.Duration(i) * time.Second),
				Task: finishedTask("x", []lib.TaskState{lib.StateFailed, lib.StateCompleted}[i%2], "r")},
			SwarmTask{Addressee: "platform", TaskID: fmt.Sprintf("%sin%02d", prefix, i), Submitted: swarmNow.Add(-time.Duration(i) * time.Second),
				Task: &lib.Task{State: lib.StateInputRequired}})
	}
	snap, _ := BuildSwarmState(in, freshLedger())
	seen := map[string]bool{}
	var ids []string
	for _, a := range snap.View.Attention {
		ids = append(ids, a.ID)
	}
	for _, w := range snap.View.Workstreams {
		ids = append(ids, w.ID)
	}
	for _, m := range snap.View.Milestones {
		ids = append(ids, m.ID)
	}
	if len(ids) < 30 {
		t.Fatalf("only %d view ids", len(ids))
	}
	for _, id := range ids {
		if seen[id] {
			t.Errorf("duplicate view id %q", id)
		}
		if len(id) > maxItemIDBytes {
			t.Errorf("view id %q longer than %d", id, maxItemIDBytes)
		}
		seen[id] = true
	}
}

func TestBuildSwarmStateViewTextNeutralised(t *testing.T) {
	evil := "x <img src=y onerror=alert(1)> [click](https://evil) ![i](https://evil/p.png)"
	in := SwarmInput{Title: evil, Now: swarmNow,
		Conversations: []SwarmConversation{{Alias: evil, Token: "tok", Description: evil, LastUsed: swarmNow}},
		Tasks: []SwarmTask{{Addressee: "platform", TaskID: "v-1", Conversation: "console:tok", Submitted: swarmNow, Prompt: evil,
			Task: finishedTask("v-1", lib.StateFailed, evil)}}}
	snap, _ := BuildSwarmState(in, freshLedger())
	raw, _ := json.Marshal(snap)
	var back SwarmSnapshot
	_ = json.Unmarshal(raw, &back)
	// Encode without HTML escaping: json.Marshal turns < and > into \u003c
	// and \u003e, which would hide them from the check below.
	var viewBuf bytes.Buffer
	enc := json.NewEncoder(&viewBuf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(back.View); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(viewBuf.String(), "‹img") {
		t.Fatalf("view never carried the hostile text, so the check is vacuous:\n%s", viewBuf.String())
	}
	all := viewBuf.String() + back.Title
	for _, m := range back.Members {
		all += m.Model
	}
	for _, m := range back.NewMessages {
		all += m.Text
	}
	for _, bad := range []string{"<", ">", "](", "<img"} {
		if strings.Contains(all, bad) {
			t.Errorf("output carries %q", bad)
		}
	}
}

func TestBuildSwarmStateLedgerClampsNextSeq(t *testing.T) {
	_, l := BuildSwarmState(SwarmInput{Now: swarmNow}, BoardLedger{NextSeq: maxBoardSeq + 5, EventSeqs: map[string]int{"a": 10}})
	if l.NextSeq != maxBoardSeq {
		t.Errorf("NextSeq = %d, want clamped to %d (never reset to 1)", l.NextSeq, maxBoardSeq)
	}
	snap, l := BuildSwarmState(SwarmInput{Now: swarmNow}, BoardLedger{NextSeq: maxBoardSeq - 1})
	if len(snap.NewMessages) != 1 || snap.NewMessages[0].Seq != maxBoardSeq-1 || l.NextSeq != maxBoardSeq {
		t.Errorf("at the cap: %+v, NextSeq %d", snap.NewMessages, l.NextSeq)
	}
}

func TestBuildSwarmStateInputNormalisation(t *testing.T) {
	// LastUsed in the future is clamped to Now.
	convs := normalizeConversations([]SwarmConversation{{Alias: "a", LastUsed: swarmNow.Add(24 * time.Hour)}}, nil, swarmNow)
	if !convs[0].lastUsed.Equal(swarmNow) {
		t.Errorf("lastUsed = %v", convs[0].lastUsed)
	}
	// Final with a non-terminal state is non-final.
	ts := normalizeTasks([]SwarmTask{{TaskID: "f", Submitted: swarmNow, Task: &lib.Task{State: lib.StateWorking, Final: true}}}, swarmNow)
	if ts[0].terminal {
		t.Error("final+working treated as terminal")
	}
	// Duplicate ids resolve by a total order, not input order.
	a := SwarmTask{Addressee: "platform", TaskID: "dup", Submitted: swarmNow, Prompt: "alpha"}
	b := SwarmTask{Addressee: "platform", TaskID: "dup", Submitted: swarmNow, Prompt: "beta"}
	x := normalizeTasks([]SwarmTask{a, b}, swarmNow)
	y := normalizeTasks([]SwarmTask{b, a}, swarmNow)
	if len(x) != 1 || len(y) != 1 || x[0].prompt != y[0].prompt {
		t.Errorf("duplicate pick depends on input order: %q vs %q", x[0].prompt, y[0].prompt)
	}
	// The task cap keeps pending input-required tasks over newer finished ones.
	var many []SwarmTask
	many = append(many, SwarmTask{Addressee: "platform", TaskID: "needs-me", Submitted: swarmNow.Add(-48 * time.Hour), Task: &lib.Task{State: lib.StateInputRequired}})
	for i := 0; i < maxSwarmInputTasks+10; i++ {
		many = append(many, SwarmTask{Addressee: "platform", TaskID: fmt.Sprintf("f-%d", i), Submitted: swarmNow.Add(-time.Duration(i) * time.Minute), Task: finishedTask("x", lib.StateCompleted, "")})
	}
	got := normalizeTasks(many, swarmNow)
	if len(got) != maxSwarmInputTasks || got[0].id != "needs-me" {
		t.Errorf("cap kept %d tasks, first %q", len(got), got[0].id)
	}
}

// TestSwarmWireTypesMatchBridge compares field sets both ways, recursively:
// a field missing on either side fails, as does a changed Go type or tag.
// The one documented omission is SwarmView.KubeAgents (omitempty).
func TestSwarmWireTypesMatchBridge(t *testing.T) {
	allowedMissing := map[string]bool{"SwarmView.KubeAgents": true}
	var cmp func(path string, ours, theirs reflect.Type)
	cmp = func(path string, ours, theirs reflect.Type) {
		if ours.Kind() != theirs.Kind() {
			t.Errorf("%s: kind %s vs bridge %s", path, ours.Kind(), theirs.Kind())
			return
		}
		switch ours.Kind() {
		case reflect.Pointer, reflect.Slice:
			cmp(path+"[]", ours.Elem(), theirs.Elem())
		case reflect.Map:
			cmp(path+"{key}", ours.Key(), theirs.Key())
			cmp(path+"{}", ours.Elem(), theirs.Elem())
		case reflect.Struct:
			name := ours.Name()
			theirFields := map[string]reflect.StructField{}
			for i := 0; i < theirs.NumField(); i++ {
				theirFields[theirs.Field(i).Name] = theirs.Field(i)
			}
			for i := 0; i < ours.NumField(); i++ {
				f := ours.Field(i)
				tf, ok := theirFields[f.Name]
				if !ok {
					t.Errorf("%s.%s: not in the bridge type", name, f.Name)
					continue
				}
				delete(theirFields, f.Name)
				if f.Tag != tf.Tag {
					t.Errorf("%s.%s: tag %q vs bridge %q", name, f.Name, f.Tag, tf.Tag)
				}
				cmp(name+"."+f.Name, f.Type, tf.Type)
			}
			for n := range theirFields {
				if !allowedMissing[name+"."+n] {
					t.Errorf("%s.%s: bridge field missing here", name, n)
				}
			}
		default:
			if ours.Kind() == reflect.Interface && ours.NumMethod() != theirs.NumMethod() {
				t.Errorf("%s: interface method sets differ", path)
			}
		}
	}
	cmp("SwarmSnapshot", reflect.TypeOf(SwarmSnapshot{}), reflect.TypeOf(bridgeSwarmSnapshot{}))
	cmp("BoardLedger", reflect.TypeOf(BoardLedger{}), reflect.TypeOf(bridgeBoardLedger{}))
}

func TestBuildSwarmStateMemberCarriesTheClientConversationID(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	in := SwarmInput{Title: "t", Now: now}
	ids := map[string]string{
		"good":     "3f2b9c1e-7a4d-4c1b-9e0f-2a6d8b5c4e11",
		"slash":    "../../etc/passwd",
		"back":     `a\b`,
		"dots":     "a..b",
		"ctrl":     "abc\x07def",
		"bidi":     "abc‮def",
		"space":    "a b",
		"long":     strings.Repeat("x", maxConversationIDBytes+1),
		"atlimit":  strings.Repeat("y", maxConversationIDBytes),
		"unicode":  "会话-1",
		"empty":    "",
		"badutf8":  "a\xffb",
		"zerowide": "a​b",
		"padded":   "  conv-7  ",
	}
	want := map[string]string{"good": ids["good"], "atlimit": ids["atlimit"], "padded": "conv-7"}
	for alias, id := range ids {
		in.Conversations = append(in.Conversations, SwarmConversation{
			Alias: alias, Token: "tok-" + alias, AGYConversationID: id, LastUsed: now.Add(-time.Minute),
		})
	}
	snap, _ := BuildSwarmState(in, freshLedger())
	found := 0
	for _, m := range snap.Members {
		if m.Role != swarmRoleWorker {
			continue
		}
		alias := strings.TrimPrefix(m.Name, "session-")
		if _, ok := ids[alias]; !ok {
			continue
		}
		found++
		if m.ConversationID != want[alias] {
			t.Errorf("member %s conversation_id = %q, want %q", m.Name, m.ConversationID, want[alias])
		}
	}
	if found != len(ids) {
		t.Fatalf("matched %d session members, want one per conversation (%d); members: %+v", found, len(ids), snap.Members)
	}
}

// A delegated child that sorts before its skipped parent (same submission
// time, smaller id; or a parent stamped Now for a missing time) is skipped
// all the same: propagation does not depend on visit order.
// A delegated child whose parent is not in the input (past the task cap or
// TASKS retention) is posted from, and its completion answers, the owner its
// ViaAddressee names; with no ViaAddressee it keeps the conversation-member /
// coordinator fallback.
func TestBuildSwarmStateDelegatedParentAbsent(t *testing.T) {
	for _, tc := range []struct{ via, wantFrom, wantTo string }{
		{"platform", swarmMemberPlatform, swarmMemberPlatform},
		{"", "session-triage", swarmMemberCoordinator},
		{"no-such-member", "session-triage", swarmMemberCoordinator},
	} {
		in := SwarmInput{Now: swarmNow,
			Conversations: []SwarmConversation{{Alias: "triage", Token: "tok-triage", LastUsed: swarmNow.Add(-time.Minute)}},
			Tasks: []SwarmTask{
				{Addressee: "session-triage-pod", TaskID: "turn-1", Conversation: "console:tok-triage", Submitted: swarmNow.Add(-5 * time.Minute),
					Task: runningTask("turn-1")},
				{Addressee: "db-specialist", TaskID: "child-1", Conversation: "console:tok-triage", ViaAddressee: tc.via, ViaTaskID: "gone-parent",
					Submitted: swarmNow.Add(-4 * time.Minute), Prompt: "Check lag", Task: finishedTask("child-1", lib.StateCompleted, "Lag 2s.")},
			},
		}
		snap, _ := BuildSwarmState(in, freshLedger())
		if _, ok := snap.Members["session-triage"]; !ok {
			t.Fatalf("via %q: no session-triage member: %v", tc.via, snap.Members)
		}
		var del, done *SwarmBoardMessage
		for i := range snap.NewMessages {
			m := &snap.NewMessages[i]
			switch {
			case strings.Contains(m.Text, "delegated task `child-1`"):
				del = m
			case strings.Contains(m.Text, "(`child-1`)"):
				done = m
			}
		}
		if del == nil || done == nil {
			t.Fatalf("via %q: missing posts:\n%s", tc.via, boardTexts(snap))
		}
		if del.Agent != tc.wantFrom {
			t.Errorf("via %q: delegation posted by %q, want %q", tc.via, del.Agent, tc.wantFrom)
		}
		if !strings.HasPrefix(done.Text, "@"+tc.wantTo+" `completed`") {
			t.Errorf("via %q: completion = %q, want it to mention @%s", tc.via, done.Text, tc.wantTo)
		}
	}
}

func TestBuildSwarmStateSkippedParentSortedAfterChild(t *testing.T) {
	in := SwarmInput{Now: swarmNow,
		Conversations: []SwarmConversation{{Alias: "old", Token: "tok-old", LastUsed: swarmNow.Add(-3 * time.Hour)}},
		Tasks: []SwarmTask{
			{Addressee: "platform", TaskID: "z-parent", Conversation: "console:tok-old", Submitted: swarmNow.Add(-3 * time.Hour),
				Task: finishedTask("z-parent", lib.StateCompleted, "ok")},
			{Addressee: "spec", TaskID: "a-child", ViaTaskID: "z-parent", Submitted: swarmNow.Add(-3 * time.Hour)},
			// grandchild before both, parent with no submission time (stamped Now)
			{Addressee: "spec", TaskID: "a-grandchild", ViaTaskID: "a-child", Submitted: swarmNow.Add(-4 * time.Hour)},
			{Addressee: "platform", TaskID: "late-parent", Conversation: "console:tok-unknown"},
			{Addressee: "spec", TaskID: "early-child", ViaTaskID: "late-parent", Submitted: swarmNow.Add(-time.Hour)},
			{Addressee: "platform", TaskID: "keep-me", Submitted: swarmNow.Add(-time.Minute)},
		},
	}
	tasks := normalizeTasks(in.Tasks, swarmNow)
	idx := map[string]int{}
	for i, task := range tasks {
		idx[task.id] = i
	}
	if idx["a-child"] > idx["z-parent"] || idx["early-child"] > idx["late-parent"] {
		t.Fatalf("fixture does not sort the children first: %v", idx)
	}
	snap, _ := BuildSwarmState(in, freshLedger())
	texts := boardTexts(snap)
	for _, gone := range []string{"z-parent", "a-child", "a-grandchild", "late-parent", "early-child"} {
		if strings.Contains(texts, gone) {
			t.Errorf("board shows %s:\n%s", gone, texts)
		}
	}
	if !strings.Contains(texts, "keep-me") {
		t.Errorf("board lacks keep-me:\n%s", texts)
	}
}

func inputRequiredTask(id string, history ...lib.TaskState) *lib.Task {
	return &lib.Task{ID: id, State: lib.StateInputRequired, StatusHistory: history,
		Artifacts: []lib.Artifact{textArtifact(lib.ArtifactProgress, "Reading the rollout history")}}
}

// The input-required post and attention item carry the question from the
// status message, not the progress line; without one they fall back.
func TestBuildSwarmStateInputRequiredQuestion(t *testing.T) {
	in := SwarmInput{Now: swarmNow, Tasks: []SwarmTask{
		{Addressee: "platform", TaskID: "q-1", Submitted: swarmNow.Add(-time.Minute), Task: inputRequiredTask("q-1", lib.StateInputRequired),
			Question: &lib.Message{Parts: []lib.Part{{Kind: "text", Text: "Restart `checkout` @narrator <now>?\nmore detail " + strings.Repeat("z", 5000)}}}},
		{Addressee: "platform", TaskID: "q-2", Submitted: swarmNow.Add(-2 * time.Minute), Task: inputRequiredTask("q-2", lib.StateInputRequired)},
	}}
	snap, _ := BuildSwarmState(in, freshLedger())
	var post1, post2 string
	for _, m := range snap.NewMessages {
		switch {
		case strings.Contains(m.Text, "input required") && strings.Contains(m.Text, "`q-1`"):
			post1 = m.Text
		case strings.Contains(m.Text, "input required") && strings.Contains(m.Text, "`q-2`"):
			post2 = m.Text
		}
	}
	if want := "`Restart 'checkout' ＠narrator ‹now›?`"; !strings.HasSuffix(post1, want) {
		t.Errorf("q-1 post = %q, want the sanitised question %s", post1, want)
	}
	if !strings.HasSuffix(post2, "`Reading the rollout history`") {
		t.Errorf("q-2 post = %q, want the progress fallback", post2)
	}
	why := map[string]string{}
	for _, a := range snap.View.Attention {
		why[a.ID] = a.Why
	}
	if why["input-q-1"] != "Restart 'checkout' ＠narrator ‹now›?" || why["input-q-2"] != "Reading the rollout history" {
		t.Errorf("attention why = %v", why)
	}
}

// A task that asks, is answered (working) and asks again posts twice; an
// unchanged ask stays deduped.
func TestBuildSwarmStateInputRequiredAskedAgain(t *testing.T) {
	task := func(lt *lib.Task) SwarmInput {
		return SwarmInput{Now: swarmNow, Tasks: []SwarmTask{{Addressee: "platform", TaskID: "r-1", Submitted: swarmNow.Add(-time.Minute), Task: lt}}}
	}
	countAsks := func(s SwarmSnapshot) int {
		n := 0
		for _, m := range s.NewMessages {
			if strings.Contains(m.Text, "input required") {
				n++
			}
		}
		return n
	}
	first := inputRequiredTask("r-1", lib.StateSubmitted, lib.StateWorking, lib.StateInputRequired)
	s1, l := BuildSwarmState(task(first), freshLedger())
	s2, l := BuildSwarmState(task(first), l)
	answered := &lib.Task{ID: "r-1", State: lib.StateWorking, StatusHistory: []lib.TaskState{lib.StateSubmitted, lib.StateWorking, lib.StateInputRequired, lib.StateWorking}}
	s3, l := BuildSwarmState(task(answered), l)
	again := inputRequiredTask("r-1", lib.StateSubmitted, lib.StateWorking, lib.StateInputRequired, lib.StateWorking, lib.StateInputRequired)
	s4, l := BuildSwarmState(task(again), l)
	s5, _ := BuildSwarmState(task(again), l)
	if got := []int{countAsks(s1), countAsks(s2), countAsks(s3), countAsks(s4), countAsks(s5)}; !reflect.DeepEqual(got, []int{1, 0, 0, 1, 0}) {
		t.Errorf("input-required posts per call = %v, want [1 0 0 1 0]", got)
	}
	if len(s4.NewMessages) != 1 || !strings.HasPrefix(s4.NewMessages[0].Text, "@coordinator input required on #") {
		t.Errorf("second ask = %+v, want one @coordinator post citing the submission", s4.NewMessages)
	}
}

// At the conversation input cap, a conversation running a task is kept over
// newer idle ones, so its work stays on the board.
func TestBuildSwarmStateConversationCapKeepsRunning(t *testing.T) {
	in := SwarmInput{Now: swarmNow}
	for i := 0; i < maxSwarmInputConvs+20; i++ {
		in.Conversations = append(in.Conversations, SwarmConversation{
			Alias: fmt.Sprintf("idle-%04d", i), Token: fmt.Sprintf("tok-idle-%04d", i), LastUsed: swarmNow.Add(-time.Duration(i) * time.Second),
		})
	}
	in.Conversations = append(in.Conversations,
		SwarmConversation{Alias: "long-runner", Token: "tok-long", LastUsed: swarmNow.Add(-6 * time.Hour)},
		SwarmConversation{Alias: "long-done", Token: "tok-done", LastUsed: swarmNow.Add(-6 * time.Hour)})
	in.Tasks = []SwarmTask{
		{Addressee: "session-long-pod", TaskID: "long-1", Conversation: "console:tok-long", Submitted: swarmNow.Add(-5 * time.Hour),
			Task: runningTask("long-1", activityArtifact("kubectl_wait"))},
		{Addressee: "platform", TaskID: "done-1", Conversation: "console:tok-done", Submitted: swarmNow.Add(-5 * time.Hour),
			Task: finishedTask("done-1", lib.StateCompleted, "ok")},
	}
	snap, _ := BuildSwarmState(in, freshLedger())
	m, ok := snap.Members["session-long-runner"]
	if !ok || m.WakeMode != swarmWakeWorking {
		t.Errorf("running conversation cut by the input cap: member %+v (present %v)", m, ok)
	}
	texts := boardTexts(snap)
	if !strings.Contains(texts, "[task `long-1`]") {
		t.Errorf("board lacks the running task:\n%s", texts)
	}
	if strings.Contains(texts, "done-1") {
		t.Errorf("a stale finished conversation survived the cap:\n%s", texts)
	}
}

// safeConversationID is an allowlist: [A-Za-z0-9._-], at most 128 bytes, no
// leading '.', no "..", no Windows reserved device name; surrounding space is
// trimmed.
func TestSafeConversationID(t *testing.T) {
	for id, want := range map[string]string{
		"3f2b9c1e-7a4d":          "3f2b9c1e-7a4d",
		"A.b_c-9":                "A.b_c-9",
		"  trimmed\t":            "trimmed",
		strings.Repeat("y", 128): strings.Repeat("y", 128),
		strings.Repeat("y", 129): "",
		"会话-1":                   "",
		".":                      "",
		"..":                     "",
		".hidden":                "",
		"a..b":                   "",
		"a:b":                    "",
		"a%2fb":                  "",
		"a*b":                    "",
		"a?b":                    "",
		"a|b":                    "",
		`a"b`:                    "",
		"a/b":                    "",
		`a\b`:                    "",
		"a b":                    "",
		"CON":                    "",
		"con":                    "",
		"Nul.txt":                "",
		"com1":                   "",
		"LPT9.log":               "",
		"console":                "console",
		"com10":                  "com10",
		"conx.y":                 "conx.y",
		"":                       "",
	} {
		if got := safeConversationID(id); got != want {
			t.Errorf("safeConversationID(%q) = %q, want %q", id, got, want)
		}
	}
}

// Two copies of a task that differ only in the question, or only in the
// question's sequence, dedupe to the same one whatever the input order.
func TestNormalizeTasksQuestionTiebreak(t *testing.T) {
	base := func(q string, seq uint64) SwarmTask {
		return SwarmTask{Addressee: "platform", TaskID: "dup", Submitted: swarmNow, Task: &lib.Task{State: lib.StateInputRequired},
			Question: &lib.Message{Parts: []lib.Part{{Kind: "text", Text: q}}}, QuestionSeq: seq}
	}
	for _, pair := range [][2]SwarmTask{{base("alpha?", 7), base("beta?", 7)}, {base("same?", 7), base("same?", 9)}} {
		x := normalizeTasks([]SwarmTask{pair[0], pair[1]}, swarmNow)
		y := normalizeTasks([]SwarmTask{pair[1], pair[0]}, swarmNow)
		if len(x) != 1 || len(y) != 1 || x[0].question != y[0].question || x[0].questionSeq != y[0].questionSeq {
			t.Errorf("duplicate pick depends on input order: %q@%d vs %q@%d", x[0].question, x[0].questionSeq, y[0].question, y[0].questionSeq)
		}
	}
}

// With QuestionSeq the input post is keyed on the ask's stream sequence: the
// same seq never re-posts, a new seq re-posts even when the history count is
// unchanged (TASKS dropped the oldest events).
func TestBuildSwarmStateInputRequiredQuestionSeq(t *testing.T) {
	call := func(seq uint64, history ...lib.TaskState) SwarmInput {
		return SwarmInput{Now: swarmNow, Tasks: []SwarmTask{{Addressee: "platform", TaskID: "s-1", Submitted: swarmNow.Add(-time.Minute),
			Task: inputRequiredTask("s-1", history...), QuestionSeq: seq,
			Question: &lib.Message{Parts: []lib.Part{{Kind: "text", Text: fmt.Sprintf("Ask %d?", seq)}}}}}}
	}
	posts := func(s SwarmSnapshot) int {
		n := 0
		for _, m := range s.NewMessages {
			if strings.Contains(m.Text, "input required") {
				n++
			}
		}
		return n
	}
	ir := lib.StateInputRequired
	s1, l := BuildSwarmState(call(40, ir, lib.StateWorking, ir), freshLedger())
	s2, l := BuildSwarmState(call(40, ir, lib.StateWorking, ir), l)
	s3, l := BuildSwarmState(call(90, ir, lib.StateWorking, ir), l) // same count, new ask
	if got := []int{posts(s1), posts(s2), posts(s3)}; !reflect.DeepEqual(got, []int{1, 0, 1}) {
		t.Errorf("input-required posts per call = %v, want [1 0 1]", got)
	}
	if _, ok := l.EventSeqs["task-input:s-1@90"]; !ok {
		t.Errorf("ledger lacks the seq key: %v", l.EventSeqs)
	}
	if !strings.HasSuffix(s3.NewMessages[0].Text, "`Ask 90?`") {
		t.Errorf("second ask = %q", s3.NewMessages[0].Text)
	}
}

// An input-required member's step line and workstream note show the
// question, not the progress line.
func TestBuildSwarmStateInputRequiredMemberShowsQuestion(t *testing.T) {
	in := SwarmInput{Now: swarmNow,
		Conversations: []SwarmConversation{{Alias: "ops", Token: "tok-ops", LastUsed: swarmNow}},
		Tasks: []SwarmTask{{Addressee: "session-ops-pod", TaskID: "m-1", Conversation: "console:tok-ops", Submitted: swarmNow.Add(-time.Minute),
			Task:     inputRequiredTask("m-1", lib.StateInputRequired),
			Question: &lib.Message{Parts: []lib.Part{{Kind: "text", Text: "Scale `orders` to <3>?"}}}}},
	}
	snap, _ := BuildSwarmState(in, freshLedger())
	if m := snap.Members["session-ops"].Model; !strings.HasSuffix(m, "⚡ Scale 'orders' to ‹3›?") {
		t.Errorf("member model = %q, want the question", m)
	}
	var note string
	for _, w := range snap.View.Workstreams {
		if w.Title == "Conversation: ops" {
			note = w.Note
		}
	}
	if !strings.HasPrefix(note, "⚡ Scale 'orders' to ‹3›? — ") {
		t.Errorf("workstream note = %q, want the question", note)
	}
}

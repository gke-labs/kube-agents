package gateway

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	testTeamsTenant    = "759d68f0-65ed-44cd-8487-ce6221b72b2a"
	testTeamsUser      = "05e62547-5ee7-41be-9639-155d9212be8f"
	testTeamsBot       = "28:6736b7e8-31ec-41b1-816f-8abb333b26cb"
	testTeamsChannel   = "19:11fb65f1e6c941b382a410dd0e383724@thread.tacv2;messageid=1794603936403"
	testTeamsDM        = "a:1UxxASmI_WSV5PQj3K0Nd3K292l62BfKLXmPpH6cQlZN7Ogxu7L_EoFVo9EhRcJ4ybzI"
	testTeamsServiceCA = "https://smba.trafficmanager.net/ca/" + testTeamsTenant + "/"
)

// testTeamsActivity is a channel message that mentions the bot.
func testTeamsActivity(id, text string) map[string]any {
	return map[string]any{
		"type": "message", "id": id, "text": text, "serviceUrl": testTeamsServiceCA,
		"conversation": map[string]any{"id": testTeamsChannel, "conversationType": "channel", "tenantId": testTeamsTenant},
		"from":         map[string]any{"id": "29:1abc", "aadObjectId": testTeamsUser, "name": "hangdng"},
		"recipient":    map[string]any{"id": testTeamsBot, "name": "Kage"},
		"entities": []map[string]any{{
			"type": "mention", "text": "<at>Kage</at>", "mentioned": map[string]any{"id": testTeamsBot, "name": "Kage"},
		}},
		"channelData": map[string]any{"tenant": map[string]any{"id": testTeamsTenant}},
	}
}

func decodeTeamsActivity(t *testing.T, m map[string]any) *teamsActivity {
	t.Helper()
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var act teamsActivity
	if err := json.Unmarshal(raw, &act); err != nil {
		t.Fatal(err)
	}
	return &act
}

func newTestTeamsAdapter(t *testing.T, relayURL, tenant string) *TeamsAdapter {
	t.Helper()
	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("projected-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := NewTeamsAdapter(relayURL, tokenPath, tenant, true, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func TestTeamsConversationIDRoundTrip(t *testing.T) {
	key := teamsConversationID(testTeamsChannel)
	if !strings.HasPrefix(key, "teams:") {
		t.Fatalf("key %q lacks the backend prefix", key)
	}
	if got := teamsConversationFromID(key); got != testTeamsChannel {
		t.Fatalf("round trip: got %q", got)
	}
	if got := teamsConversationFromID("gchat:space/x"); got != "" {
		t.Fatalf("another backend's key resolved to %q", got)
	}
}

func TestTeamsServiceURLAllowed(t *testing.T) {
	for url, want := range map[string]bool{
		testTeamsServiceCA:                              true,
		"https://smba.trafficmanager.net/teams/":        true,
		"https://europe.api.botframework.com/":          true,
		"http://smba.trafficmanager.net/teams/":         false,
		"https://evil.example/smba.trafficmanager.net/": false,
		"https://smba.trafficmanager.net/?x=1":          false,
		"":                                              false,
	} {
		if got := teamsServiceURLAllowed(url); got != want {
			t.Errorf("%q: got %v, want %v", url, got, want)
		}
	}
}

func TestTeamsStripBotMentions(t *testing.T) {
	act := decodeTeamsActivity(t, testTeamsActivity("f:1", "<at>Kage</at> what clusters do you manage?"))
	if got := teamsStripBotMentions(act); got != "what clusters do you manage?" {
		t.Fatalf("got %q", got)
	}
	// A mention of someone else stays.
	other := testTeamsActivity("f:2", "<at>Kage</at> ask <at>Dana</at> too")
	other["entities"] = append(other["entities"].([]map[string]any), map[string]any{
		"type": "mention", "text": "<at>Dana</at>", "mentioned": map[string]any{"id": "29:dana"},
	})
	if got := teamsStripBotMentions(decodeTeamsActivity(t, other)); got != "ask <at>Dana</at> too" {
		t.Fatalf("got %q", got)
	}
	// The rest of the text keeps its shape: a pasted manifest arrives with
	// its line breaks and indentation.
	pasted := "<at>Kage</at> apply this:\n```yaml\nkind: Pod\nmetadata:\n  name: x\n```"
	if got := teamsStripBotMentions(decodeTeamsActivity(t, testTeamsActivity("f:3", pasted))); got != "apply this:\n```yaml\nkind: Pod\nmetadata:\n  name: x\n```" {
		t.Fatalf("got %q", got)
	}
}

func TestTeamsClassify(t *testing.T) {
	a := newTestTeamsAdapter(t, "http://relay.invalid", testTeamsTenant)
	msg, reason := a.classify(decodeTeamsActivity(t, testTeamsActivity("f:1", "<at>Kage</at> hello")))
	if reason != "" {
		t.Fatalf("refused: %s", reason)
	}
	if msg.Conversation != "teams:"+testTeamsChannel || msg.Kind != sessionKindGroup || msg.AuthorID != testTeamsUser ||
		msg.MessageID != "f:1" || msg.Text != "hello" {
		t.Fatalf("message: %+v", msg)
	}
	if got := a.serviceURLFor(testTeamsChannel); got != testTeamsServiceCA {
		t.Fatalf("serviceUrl not remembered: %q", got)
	}
	// The same activity again is a duplicate.
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsActivity("f:1", "<at>Kage</at> hello"))); !strings.HasPrefix(reason, "duplicate") {
		t.Fatalf("duplicate not refused: %q", reason)
	}
	// A personal chat is a DM.
	dm := testTeamsActivity("f:2", "hello")
	dm["conversation"] = map[string]any{"id": testTeamsDM, "conversationType": "personal", "tenantId": testTeamsTenant}
	dm["entities"] = nil
	msg, reason = a.classify(decodeTeamsActivity(t, dm))
	if reason != "" || msg.Kind != sessionKindDM || msg.Conversation != "teams:"+testTeamsDM {
		t.Fatalf("dm: %+v %q", msg, reason)
	}
	// Another tenant, a bare mention, a non-message and an untrusted
	// serviceUrl are not turns.
	foreign := testTeamsActivity("f:3", "<at>Kage</at> hi")
	foreign["channelData"] = map[string]any{"tenant": map[string]any{"id": "other"}}
	foreign["conversation"].(map[string]any)["tenantId"] = "other"
	for name, m := range map[string]map[string]any{
		"tenant": foreign,
		"bare":   testTeamsActivity("f:4", "<at>Kage</at>"),
		"typing": {"type": "typing", "conversation": map[string]any{"id": testTeamsChannel}},
		"serviceUrl": func() map[string]any {
			m := testTeamsActivity("f:5", "x")
			m["serviceUrl"] = "https://evil.example/"
			return m
		}(),
	} {
		if _, reason := a.classify(decodeTeamsActivity(t, m)); reason == "" {
			t.Errorf("%s: accepted as a turn", name)
		}
	}
	// With no tenant configured, any tenant is served.
	open := newTestTeamsAdapter(t, "http://relay.invalid", "")
	if _, reason := open.classify(decodeTeamsActivity(t, foreign)); reason != "" {
		t.Fatalf("open tenant refused: %s", reason)
	}
}

// TestTeamsMarkdownTableRules: the table scanner follows GitHub Markdown. A
// table starts only where a header row is followed by a delimiter row, so a
// lone "|x| + |y|" line is text; a delimiter cell may be one dash; an escaped
// pipe stays inside its cell; an empty edge cell keeps its column.
func TestTeamsMarkdownTableRules(t *testing.T) {
	body := func(text string) []map[string]any { return teamsMarkdownToCard(text)["body"].([]map[string]any) }
	if b := body("|x| + |y| = 1"); len(b) != 1 || b[0]["type"] != teamsCardTextBlock || b[0]["text"] != "|x| + |y| = 1" {
		t.Fatalf("a lone pipe line became %v", b)
	}
	if b := body("| a | b |\n| - | - |\n| 1 | 2 |"); len(b) != 1 || b[0]["type"] != teamsCardTable || len(b[0]["rows"].([]map[string]any)) != 2 {
		t.Fatalf("one-dash delimiter: %v", b)
	}
	if got := teamsTableCells(`| kubectl get pods \| grep x | c |`); len(got) != 2 || got[0] != "kubectl get pods | grep x" || got[1] != "c" {
		t.Fatalf("escaped pipe split: %q", got)
	}
	if got := teamsTableCells("|| node-1 | Running |"); len(got) != 3 || got[0] != "" || got[1] != "node-1" {
		t.Fatalf("empty first cell: %q", got)
	}
	if got := teamsTableCells(`| a\b | c |`); len(got) != 2 || got[0] != `a\b` {
		t.Fatalf("a backslash not before a pipe must stay: %q", got)
	}
	if got := teamsTableCells(`| a | b \|`); len(got) != 2 || got[1] != "b |" {
		t.Fatalf("a trailing escaped pipe is content: %q", got)
	}
	// A header with no delimiter row after it is text, line by line; so is
	// a pipe-led line followed by a thematic break; a delimiter row with a
	// different cell count opens no table.
	if b := body("| a | b |\n| 1 | 2 |"); len(b) != 2 || b[0]["type"] != teamsCardTextBlock {
		t.Fatalf("no delimiter row: %v", b)
	}
	if b := body("| a | b |\n---\nrule"); len(b) != 3 || b[0]["type"] != teamsCardTextBlock {
		t.Fatalf("thematic break read as a table: %v", b)
	}
	// Prose with an escaped pipe, or a one-cell pipe line, followed by a
	// rule: text, not a one-column table.
	if b := body(`Run cmd_a \| cmd_b` + "\n---\nnext"); len(b) != 3 || b[0]["type"] != teamsCardTextBlock {
		t.Fatalf("escaped pipe + rule read as a table: %v", b)
	}
	if b := body("| note |\n---"); b[0]["type"] != teamsCardTextBlock {
		t.Fatalf("one-cell line + rule read as a table: %v", b)
	}
	// A fence is a line of backticks with no backtick after it: an inline
	// triple-backtick span at the start of a line is text, and a fence
	// whose info string carries a pipe, right after a table, is a fence.
	if b := body("```kubectl get pods``` lists the pods\nnext line"); len(b) != 2 || b[0]["type"] != teamsCardTextBlock || b[0]["text"] != "```kubectl get pods``` lists the pods" {
		t.Fatalf("inline triple-backtick span opened a block: %v", b)
	}
	if b := body("| a | b |\n|--|--|\n| 1 | 2 |\n```sh | head\nls\n```\nafter"); len(b) != 3 || b[0]["type"] != teamsCardTable || b[1]["fontType"] != teamsCardMonospace || b[1]["text"] != "ls" || b[2]["text"] != "after" {
		t.Fatalf("fence after a table: %v", b)
	}
	if b := body("| a | b |\n| - |\n| 1 | 2 |"); b[0]["type"] != teamsCardTextBlock {
		t.Fatalf("cell-count mismatch read as a table: %v", b)
	}
	// GitHub needs no border pipes: a table the gateway's chunker carries
	// across a cut arrives as "a | b" rows.
	if b := body("a | b\n--|--\n1 | 2"); len(b) != 1 || b[0]["type"] != teamsCardTable || len(b[0]["rows"].([]map[string]any)) != 2 {
		t.Fatalf("borderless table: %v", b)
	}
}

// TestTeamsEditNotifyMessageEditsInPlace: a keyed home post (an alert its
// triage updates) is one activity edited through the relay's PUT, in the
// thread when one is named and in home otherwise, never a new post.
func TestTeamsEditNotifyMessageEditsInPlace(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	var editor notifyChatEditor = a
	home := "19:11fb65f1e6c941b382a410dd0e383724@thread.tacv2"
	if err := editor.EditNotifyMessage(home, home+";messageid=1794603936403", "act-9", OutMessage{Text: "triage: resolved"}); err != nil {
		t.Fatalf("edit in thread: %v", err)
	}
	if err := editor.EditNotifyMessage(home, "", "act-7", OutMessage{Text: "still looking"}); err != nil {
		t.Fatalf("edit in home: %v", err)
	}
	if err := editor.EditNotifyMessage("spaces/AAA", "", "act-1", OutMessage{Text: "x"}); err == nil {
		t.Fatal("a non-Teams home accepted")
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if len(relay.apiCall) != 2 || relay.apiCall[0]["method"] != http.MethodPut || relay.apiCall[0]["endpoint"] != "v3/conversations/"+home+";messageid=1794603936403/activities/act-9" || relay.apiCall[1]["endpoint"] != "v3/conversations/"+home+"/activities/act-7" {
		t.Fatalf("api calls %v", relay.apiCall)
	}
}

// TestTeamsAnswersOnItsLine: with no reaction and no Working… status the
// task's line is the ask's one receipt, so the adapter answers on it as
// Chat does (LineAnswerer).
func TestTeamsAnswersOnItsLine(t *testing.T) {
	var a Adapter = newTestTeamsAdapter(t, "http://relay.invalid", testTeamsTenant)
	answerer, ok := a.(LineAnswerer)
	if !ok || !answerer.AnswersOnLine("teams:"+testTeamsChannel) {
		t.Fatal("the Teams adapter must answer on its line")
	}
}

func TestTeamsMarkdownToCard(t *testing.T) {
	text := "# Title\nSome text\n- one\n- two\n```\ncode here\n```\n| Name | Count |\n|---|---|\n| a | 1 |"
	card := teamsMarkdownToCard(text)
	if card["version"] != "1.5" {
		t.Fatalf("version %v", card["version"])
	}
	if width := card["msteams"].(map[string]any)["width"]; width != "Full" {
		t.Fatalf("card width %v, want Full", width)
	}
	body := card["body"].([]map[string]any)
	types := []string{}
	for _, b := range body {
		types = append(types, b["type"].(string))
	}
	want := []string{"TextBlock", "TextBlock", "TextBlock", "TextBlock", "TextBlock", "Table"}
	if strings.Join(types, ",") != strings.Join(want, ",") {
		t.Fatalf("blocks %v", types)
	}
	if body[0]["weight"] != "Bolder" || body[0]["size"] != "Large" {
		t.Fatalf("heading %v", body[0])
	}
	if !strings.HasPrefix(body[2]["text"].(string), "• ") {
		t.Fatalf("bullet %v", body[2])
	}
	if body[4]["fontType"] != "Monospace" {
		t.Fatalf("code %v", body[4])
	}
	table := body[5]
	rows := table["rows"].([]map[string]any)
	if table["firstRowAsHeader"] != true || len(rows) != 2 {
		t.Fatalf("table %v", table)
	}
	header := rows[0]["cells"].([]map[string]any)[0]["items"].([]map[string]any)[0]
	if header["text"] != "Name" || header["weight"] != "Bolder" {
		t.Fatalf("header %v", header)
	}
}

func TestTeamsFitActivity(t *testing.T) {
	short := teamsFitActivity("hello", true, teamsMaxActivityBytes)
	if short["summary"] != "hello" || short["attachments"] == nil {
		t.Fatalf("short: %v", short)
	}
	plain := teamsFitActivity("hello", false, teamsMaxActivityBytes)
	if plain["text"] != "hello" || plain["attachments"] != nil {
		t.Fatalf("plain: %v", plain)
	}
	// Many short lines inflate a card past a small cap; the text still fits.
	lines := strings.Repeat("x\n", 200)
	fitted := teamsFitActivity(lines, true, 1500)
	if fitted["attachments"] != nil || fitted["text"] != lines {
		t.Fatalf("card over the cap should post as text: %v", fitted)
	}
	// Text over the cap is cut with the marker and measured under it.
	long := strings.Repeat("y", 5000)
	cut := teamsFitActivity(long, false, 1000)
	raw, _ := json.Marshal(cut)
	if len(raw) > 1000 || !strings.HasSuffix(cut["text"].(string), teamsTruncationMarker) {
		t.Fatalf("truncation: %d bytes, text ends %q", len(raw), cut["text"].(string)[len(cut["text"].(string))-20:])
	}
}

func TestTeamsActivitySummary(t *testing.T) {
	if got := teamsActivitySummary("| **Cluster** | `x` |\n|---|---|"); got != "Cluster x" {
		t.Fatalf("got %q", got)
	}
	// Nothing but marks: the preview is the text's first line as written,
	// never a sentence the renderer composed.
	if got := teamsActivitySummary("|---|"); got != "|---|" {
		t.Fatalf("got %q", got)
	}
	if got := teamsActivitySummary("pod_name is **ready**"); got != "pod_name is ready" {
		t.Fatalf("an underscore is part of a name: %q", got)
	}
	if got := teamsActivitySummary(strings.Repeat("word ", 100)); len([]rune(got)) > teamsSummaryMax {
		t.Fatalf("summary not bounded: %d", len([]rune(got)))
	}
}

func TestTeamsRelayError(t *testing.T) {
	err := teamsRelayError("teams: relay answered 502", []byte(`{"error":"Teams operation failed","detail":"401 invalid_client AADSTS7000215: Invalid client secret provided."}`))
	if !strings.Contains(err.Error(), "Teams operation failed") || !strings.Contains(err.Error(), "AADSTS7000215") {
		t.Fatalf("the broker's error and detail missing: %v", err)
	}
	err = teamsRelayError("teams: relay answered 403", []byte(`{"error":"caller role not admitted"}`))
	if !strings.Contains(err.Error(), "caller role not admitted") {
		t.Fatalf("broker's error missing: %v", err)
	}
	if err := teamsRelayError("teams: relay answered 500", nil); err.Error() != "teams: relay answered 500" {
		t.Fatalf("bare: %v", err)
	}
}

// fakeTeamsRelay speaks the credential proxy's Teams relay: one event is
// handed out on the first pull, every later pull is empty, and every ack,
// nack and api call is recorded.
type fakeTeamsRelay struct {
	srv *httptest.Server
	mu  sync.Mutex

	event   map[string]any
	pulled  bool
	acks    []string
	apiCall []map[string]any
	tokens  []string
}

func newFakeTeamsRelay(t *testing.T, event map[string]any) *fakeTeamsRelay {
	f := &fakeTeamsRelay{event: event}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.tokens = append(f.tokens, strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		var body map[string]any
		if r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(&body)
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case teamsRelayEventsPath:
			if f.pulled || f.event == nil {
				_ = json.NewEncoder(w).Encode(map[string]any{"event": nil})
				return
			}
			f.pulled = true
			_ = json.NewEncoder(w).Encode(map[string]any{"event": map[string]any{"receipt": "r-1", "event": f.event}})
		case teamsRelayAckPath:
			f.acks = append(f.acks, body["receipt"].(string))
			_ = json.NewEncoder(w).Encode(map[string]any{"settled": true})
		case teamsRelayAPIPath:
			f.apiCall = append(f.apiCall, body)
			_ = json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"id": "1794603936403-reply"}})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func TestTeamsRunPullsAcksAndHandsOver(t *testing.T) {
	relay := newFakeTeamsRelay(t, testTeamsActivity("f:9", "<at>Kage</at> how many nodes?"))
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	ctx, cancel := context.WithCancel(context.Background())
	got := make(chan InboundMessage, 1)
	done := make(chan error, 1)
	go func() { done <- a.Run(ctx, func(m InboundMessage) { got <- m }) }()
	select {
	case m := <-got:
		if m.Text != "how many nodes?" || m.AuthorID != testTeamsUser || m.Conversation != "teams:"+testTeamsChannel {
			t.Fatalf("handed over: %+v", m)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no turn handed over")
	}
	cancel()
	if err := <-done; err == nil {
		t.Fatal("Run returned nil after cancel")
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if len(relay.acks) != 1 || relay.acks[0] != "r-1" {
		t.Fatalf("acks %v", relay.acks)
	}
	for _, tok := range relay.tokens {
		if tok != "projected-token" {
			t.Fatalf("relay saw token %q", tok)
		}
	}
}

func TestTeamsPostRepliesThroughTheRelay(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	// The conversation's regional serviceUrl, learned from an inbound
	// activity, is where the reply goes; an unknown conversation uses the
	// global default.
	a.classify(decodeTeamsActivity(t, testTeamsActivity("f:1", "<at>Kage</at> hi")))
	id, err := a.Post("teams:"+testTeamsChannel, "| a | b |\n|---|---|\n| 1 | 2 |")
	if err != nil || id != "1794603936403-reply" {
		t.Fatalf("post: %v %q", err, id)
	}
	if _, err := a.Post("teams:"+testTeamsDM, "hello"); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Post("slack:C1", "hello"); err == nil {
		t.Fatal("another backend's key accepted")
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if len(relay.apiCall) != 2 {
		t.Fatalf("api calls %d", len(relay.apiCall))
	}
	first := relay.apiCall[0]
	if first["serviceUrl"] != testTeamsServiceCA || first["method"] != "POST" ||
		first["endpoint"] != "v3/conversations/"+testTeamsChannel+"/activities" {
		t.Fatalf("first call %v", first)
	}
	payload := first["payload"].(map[string]any)
	if payload["attachments"] == nil || payload["summary"] == nil {
		t.Fatalf("not a card activity: %v", payload)
	}
	if relay.apiCall[1]["serviceUrl"] != teamsDefaultServiceURL {
		t.Fatalf("unknown conversation did not use the default serviceUrl: %v", relay.apiCall[1]["serviceUrl"])
	}
}

func TestTeamsEditUpdatesTheActivityThroughTheRelay(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	if err := a.Edit("teams:"+testTeamsChannel, "1794603936403-reply", "⏳ **running** step 2 of 3"); err != nil {
		t.Fatal(err)
	}
	if err := a.Edit("teams:"+testTeamsChannel, "", "x"); err == nil {
		t.Fatal("an edit naming no activity was sent")
	}
	if err := a.Edit("slack:C1", "m", "x"); err == nil {
		t.Fatal("another backend's key accepted")
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if len(relay.apiCall) != 1 {
		t.Fatalf("api calls %d", len(relay.apiCall))
	}
	call := relay.apiCall[0]
	if call["method"] != "PUT" || call["endpoint"] != "v3/conversations/"+testTeamsChannel+"/activities/1794603936403-reply" {
		t.Fatalf("update call %v", call)
	}
	if payload := call["payload"].(map[string]any); payload["id"] != "1794603936403-reply" || payload["attachments"] == nil {
		t.Fatalf("update payload %v", payload)
	}
}

func TestTeamsUnservedOperations(t *testing.T) {
	a := newTestTeamsAdapter(t, "http://relay.invalid", "")
	if _, err := a.OpenDirect("u"); err == nil {
		t.Fatal("OpenDirect served")
	}
	ids, complete, err := a.Roster("teams:x")
	if err != nil || complete || len(ids) != 0 {
		t.Fatalf("roster: %v %v %v", ids, complete, err)
	}
	if a.React("c", "m", "ok") != nil || a.RemoveReaction("c", "m", "ok") != nil {
		t.Fatal("reactions are a nil no-op")
	}
}

func TestTeamsBackendIsArmedByTheRelayURL(t *testing.T) {
	cfg := &Config{TeamsRelayURL: "http://relay"}
	backends := cfg.Backends()
	if len(backends) != 1 || backends[0] != BackendTeams {
		t.Fatalf("backends %v", backends)
	}
	if !knownBackendName(teamsBackend) {
		t.Fatal("teams is not a known backend name")
	}
	if metricsBackendPlatforms[teamsBackend] != platformTeams {
		t.Fatal("teams has no platform label")
	}
}

func TestTeamsAllowlistAdmitsEitherID(t *testing.T) {
	a := newTestTeamsAdapter(t, "http://relay.invalid", testTeamsTenant)
	act := func(id string) *teamsActivity {
		return decodeTeamsActivity(t, testTeamsActivity(id, "<at>Kage</at> hi"))
	}
	// No list: the object id is the author (the gateway refuses it).
	if msg, _ := a.classify(act("f:1")); msg.AuthorID != testTeamsUser {
		t.Fatalf("no list: author %q", msg.AuthorID)
	}
	// Listed by the object id, in another case: the object id.
	a.SetAllowedUsers([]string{strings.ToUpper(testTeamsUser)}, false)
	if msg, _ := a.classify(act("f:2")); msg.AuthorID != testTeamsUser {
		t.Fatalf("aad listed: author %q", msg.AuthorID)
	}
	// Listed by the Bot Framework id only: that id is handed over, so the
	// gateway's check against the same list admits the sender.
	a.SetAllowedUsers([]string{"29:1abc"}, false)
	if msg, _ := a.classify(act("f:3")); msg.AuthorID != "29:1abc" {
		t.Fatalf("bf id listed: author %q", msg.AuthorID)
	}
	// Allow-all: the object id.
	a.SetAllowedUsers(nil, true)
	if msg, _ := a.classify(act("f:4")); msg.AuthorID != testTeamsUser {
		t.Fatalf("allow-all: author %q", msg.AuthorID)
	}
	// A sender with no object id is keyed on the Bot Framework id.
	m := testTeamsActivity("f:5", "<at>Kage</at> hi")
	m["from"] = map[string]any{"id": "29:guest"}
	if msg, _ := a.classify(decodeTeamsActivity(t, m)); msg.AuthorID != "29:guest" {
		t.Fatalf("no aad: author %q", msg.AuthorID)
	}
}

// TestTeamsPrincipalFollowsTheAllowlist: the gateway's verification of a
// Teams sender (resolvePrincipal) is the allowlist alone, compared
// lowercased, with allow-all admitting anyone; a sender off the list has no
// principal and is refused before any session is touched.
func TestTeamsPrincipalFollowsTheAllowlist(t *testing.T) {
	build := func(allowed []string, allowAll bool) *Gateway {
		t.Helper()
		g, err := New(Options{
			Client:  &lib.Client{},
			Adapter: newFakeAdapter(),
			Backend: teamsBackend,
			Config: &Config{
				NATSURL:            "nats://127.0.0.1:4222",
				TeamsRelayURL:      "http://relay.invalid",
				TeamsAllowedUsers:  allowed,
				TeamsAllowAllUsers: allowAll,
				DefaultAddressee:   "platform",
				AttributionSalt:    []byte("test-salt"),
			},
		})
		if err != nil {
			t.Fatalf("New: %v", err)
		}
		return g
	}
	g := build([]string{strings.ToUpper(testTeamsUser), " 29:1abc "}, false)
	if got := g.resolvePrincipal(teamsBackend, testTeamsUser); got != testTeamsUser {
		t.Fatalf("listed object id resolved to %q", got)
	}
	if got := g.resolvePrincipal(teamsBackend, "29:1ABC"); got != "29:1ABC" {
		t.Fatalf("listed Bot Framework id resolved to %q", got)
	}
	if got := g.resolvePrincipal(teamsBackend, "29:guest"); got != "" {
		t.Fatalf("an unlisted sender resolved to %q", got)
	}
	if got := build(nil, false).resolvePrincipal(teamsBackend, testTeamsUser); got != "" {
		t.Fatalf("an empty list with allow-all off resolved %q", got)
	}
	if got := build(nil, true).resolvePrincipal(teamsBackend, "29:guest"); got != "29:guest" {
		t.Fatalf("allow-all resolved %q", got)
	}
}

func TestTeamsNotifyRoutePostsIntoTheConversation(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	home := "19:11fb65f1e6c941b382a410dd0e383724@thread.tacv2"
	n, err := NewTeamsNotifier(a, home, nil)
	if err != nil {
		t.Fatal(err)
	}
	if n.subject != "chat.notify.teams" || n.convPrefix != "teams:" {
		t.Fatalf("notifier %q %q", n.subject, n.convPrefix)
	}
	if _, err := NewTeamsNotifier(a, "not a conversation id", nil); err == nil {
		t.Fatal("a home with whitespace accepted")
	}
	// The home must be a Teams conversation id: another backend's key or a
	// URL is refused at start, not at every post.
	for _, bad := range []string{"spaces/AAA", "C0123ABCD", "https://teams.microsoft.com/l/channel/x", "gchat:spaces/AAA", "19:", "a:", "19:abc@thread.tacv2/General?groupId=1", "19:a\u00a0b@thread.tacv2", `19:abc\x@thread.tacv2`, home + ";messageid=1794603936403"} {
		if _, err := NewTeamsNotifier(a, bad, nil); err == nil {
			t.Fatalf("home %q accepted", bad)
		}
	}
	for _, good := range []string{home, "19:abc@thread.v2", testTeamsDM} {
		if _, err := NewTeamsNotifier(a, good, nil); err != nil {
			t.Fatalf("home %q refused: %v", good, err)
		}
	}
	if !teamsThreadInHome(home+";messageid=1794603936403", home) || teamsThreadInHome("a:other", home) || !teamsThreadInHome(home, home) {
		t.Fatal("thread-in-home rule")
	}
	// A top-level home post in a channel lands in the thread it starts, so
	// later chunks and a person's reply key that thread; a thread post lands
	// in its thread.
	message, landed, err := a.PostNotify(home, "", "fleet audit done")
	if err != nil || message == "" || landed != home+";messageid="+message {
		t.Fatalf("home post: %v %q %q", err, message, landed)
	}
	if !teamsThreadInHome(landed, home) {
		t.Fatalf("the notifier would refuse where the home post landed: %q", landed)
	}
	// A personal chat is not threaded: the post lands in the chat itself.
	_, landed, err = a.PostNotify(testTeamsDM, "", "a word in the chat")
	if err != nil || landed != testTeamsDM {
		t.Fatalf("dm post: %v %q", err, landed)
	}
	_, landed, err = a.PostNotify(home, home+";messageid=1794603936403", "and the thread")
	if err != nil || landed != home+";messageid=1794603936403" {
		t.Fatalf("thread post: %v %q", err, landed)
	}
	// A home that is itself a thread, and a group chat (not threaded): the
	// post lands where it was sent, with no second suffix.
	_, landed, err = a.PostNotify(home+";messageid=1794603936403", "", "into the thread home")
	if err != nil || landed != home+";messageid=1794603936403" {
		t.Fatalf("thread-home post: %v %q", err, landed)
	}
	_, landed, err = a.PostNotify("19:groupchat@thread.v2", "", "in the group chat")
	if err != nil || landed != "19:groupchat@thread.v2" {
		t.Fatalf("group chat post: %v %q", err, landed)
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if len(relay.apiCall) != 5 || relay.apiCall[2]["endpoint"] != "v3/conversations/"+home+";messageid=1794603936403/activities" {
		t.Fatalf("api calls %v", relay.apiCall)
	}
}

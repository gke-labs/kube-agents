package mcp

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The bridge's conversation and lineage types (#3689 at 5a706327:
// a2a/mcpbridge/config.go ConversationEntry, swarm_lineage.go
// ConversationLineage), copied so the field sets can be held to ours.
type bridgeConversationEntry struct {
	Alias                   string    `json:"alias"`
	Token                   string    `json:"token"`
	SessionMode             bool      `json:"sessionMode"`
	SessionTerminated       bool      `json:"sessionTerminated,omitempty"`
	Description             string    `json:"description,omitempty"`
	AGYConversationID       string    `json:"agyConversationId,omitempty"`
	ParentAGYConversationID string    `json:"parentAgyConversationId,omitempty"`
	SubagentRole            string    `json:"subagentRole,omitempty"`
	SessionWorkerID         string    `json:"sessionWorkerId,omitempty"`
	AutoBound               bool      `json:"autoBound,omitempty"`
	DebugMode               bool      `json:"debugMode,omitempty"`
	Disconnected            bool      `json:"disconnected,omitempty"`
	CreatedAt               time.Time `json:"createdAt"`
	LastUsedAt              time.Time `json:"lastUsedAt"`
}

type bridgeConversationLineage struct {
	ConversationID       string `json:"conversationId"`
	ParentConversationID string `json:"parentConversationId,omitempty"`
	RootConversationID   string `json:"rootConversationId"`
	SubagentType         string `json:"subagentType,omitempty"`
	SubagentRole         string `json:"subagentRole,omitempty"`
	SubagentState        string `json:"subagentState,omitempty"`
	IsSubagent           bool   `json:"isSubagent"`
}

// bridgeRemoteSwarmResponse is the bridge's RemoteSwarmResponse, with the
// deployment and pending events, which this server never fills, held as raw
// JSON, and the three fields the server adds and the bridge ignores.
type bridgeRemoteSwarmResponse struct {
	Connected          bool                               `json:"connected"`
	MigratedSchema     bool                               `json:"migratedSchema,omitempty"`
	Deployment         json.RawMessage                    `json:"deployment"`
	Snapshot           bridgeSwarmSnapshot                `json:"snapshot"`
	Ledger             bridgeBoardLedger                  `json:"ledger"`
	Conversations      map[string]bridgeConversationEntry `json:"conversations,omitempty"`
	ActiveConversation string                             `json:"activeConversation,omitempty"`
	PendingEvents      []json.RawMessage                  `json:"pendingEvents,omitempty"`

	DeploymentIgnored   bool     `json:"deploymentIgnored"`
	PendingEventsServed bool     `json:"pendingEventsServed"`
	Notes               []string `json:"notes"`
}

const (
	convTriage  = "convtriage01"
	convUnknown = "convunknown9"
)

// swarmServer is a handler serving the swarm route from a fresh bus, with the
// TASKS window and drain limit restored after the test.
func swarmServer(t *testing.T) (http.Handler, jetstream.JetStream, *Conversations) {
	t.Helper()
	url, js := testBus(t)
	convs := NewConversations()
	oldWindow, oldLimit := swarmTaskWindow, swarmTaskLimit
	t.Cleanup(func() { swarmTaskWindow, swarmTaskLimit = oldWindow, oldLimit })
	return Handler(mustServer(t), writeToken(t, testToken), quietLog(), WithSwarm(dial(t, url), convs)), js, convs
}

func postSwarm(h http.Handler, header http.Header, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, SwarmPath, strings.NewReader(body))
	for k, vs := range header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

var swarmAuth = http.Header{headerAuthorization: {"Bearer " + testToken}}

// askSwarm posts body and decodes a 200 answer.
func askSwarm(t *testing.T, h http.Handler, body any) (swarmResponse, []byte) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	rec := postSwarm(h, swarmAuth, string(raw))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	var resp swarmResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	return resp, rec.Body.Bytes()
}

func clientConv(alias, conv string, lastUsed time.Time) map[string]any {
	return map[string]any{"alias": alias, "token": conv, "lastUsedAt": lastUsed}
}

func hasNote(resp swarmResponse, sub string) bool {
	for _, n := range resp.Notes {
		if strings.Contains(n, sub) {
			return true
		}
	}
	return false
}

// The swarm route is behind exactly the /mcp checks: each of the /mcp cases
// gets the same answer on both routes.
func TestTheSwarmRouteRefusesWhatMCPRefuses(t *testing.T) {
	url, _ := testBus(t)
	bus := dial(t, url)
	for _, tc := range authCases() {
		t.Run(tc.name, func(t *testing.T) {
			h := Handler(mustServer(t), tc.tokenFile(t), quietLog(), WithSwarm(bus, NewConversations()))
			if rec := post(h, tc.header, pingFrame); rec.Code != tc.want {
				t.Errorf("%s: status = %d, want %d", Path, rec.Code, tc.want)
			}
			if rec := postSwarm(h, tc.header, `{}`); rec.Code != tc.want {
				t.Errorf("%s: status = %d, want %d (body %q)", SwarmPath, rec.Code, tc.want, rec.Body.String())
			}
		})
	}
}

func TestTheSwarmRouteBoundsItsBody(t *testing.T) {
	h, _, _ := swarmServer(t)
	// Past its own limit and well under /mcp's, so the limit is its own.
	if rec := postSwarm(h, swarmAuth, `{"x":"`+strings.Repeat("a", maxSwarmBodyBytes)+`"}`); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body = %d, want 413", rec.Code)
	}
	for _, body := range []string{`[]`, `{"conversations":5}`, `{"ledger":"x"}`, `not json`, `{} {}`} {
		if rec := postSwarm(h, swarmAuth, body); rec.Code != http.StatusBadRequest {
			t.Errorf("body %s = %d, want 400", body, rec.Code)
		}
	}
	if rec := postSwarm(h, swarmAuth, `{"rootConversationId":"r","someFutureField":{"a":1}}`); rec.Code != http.StatusOK {
		t.Errorf("an unknown field = %d, want 200: unknown fields are ignored", rec.Code)
	}
	req := httptest.NewRequest(http.MethodGet, SwarmPath, nil)
	req.Header.Set(headerAuthorization, "Bearer "+testToken)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET %s = %d, want 405", SwarmPath, rec.Code)
	}
}

// Without WithSwarm there is no swarm route.
func TestTheSwarmRouteIsOptIn(t *testing.T) {
	h := Handler(mustServer(t), writeToken(t, testToken), quietLog())
	if rec := postSwarm(h, swarmAuth, `{}`); rec.Code != http.StatusNotFound {
		t.Errorf("status = %d, want 404", rec.Code)
	}
}

// A deployment, credentials included, is never read: nothing of it comes
// back, and nothing is dialled because of it.
func TestTheSwarmRouteIgnoresTheDeployment(t *testing.T) {
	h, _, _ := swarmServer(t)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	var dialled atomic.Int32
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			dialled.Add(1)
			_ = c.Close()
		}
	}()
	addr := ln.Addr().String()
	const marker = "dep-marker-7f3a"
	body := fmt.Sprintf(`{"rootConversationId":"root-1","deployment":{"name":%[1]q,"namespace":%[1]q,
		"directNatsUrl":"nats://%[2]s","natsService":%[2]q,"busUser":%[1]q,"busAuthValue":%[1]q,
		"observerAuthValue":%[1]q,"kubeContext":%[1]q,"credsObjectName":%[1]q},
		"conversations":{"triage":{"alias":"triage","token":%[3]q}}}`, marker, addr, convTriage)
	rec := postSwarm(h, swarmAuth, body)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	raw := rec.Body.String()
	if strings.Contains(raw, marker) || strings.Contains(raw, addr) {
		t.Errorf("the deployment leaked into the response: %s", raw)
	}
	var resp swarmResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if !resp.DeploymentIgnored || !hasNote(resp, "never takes a deployment") || resp.PendingEventsServed || resp.PendingEvents == nil {
		t.Errorf("response = ignored %v, served %v, pending %v, notes %q", resp.DeploymentIgnored, resp.PendingEventsServed, resp.PendingEvents, resp.Notes)
	}
	time.Sleep(200 * time.Millisecond)
	if n := dialled.Load(); n != 0 {
		t.Errorf("the deployment's address was dialled %d times", n)
	}
}

// End to end: a console conversation's task and its delegated child are
// projected; a task in another conversation, one off the console that
// nothing delegated, and one older than the window are not. A second call
// with the returned ledger posts nothing again.
func TestTheSwarmRouteProjectsTheConversationsTasks(t *testing.T) {
	h, js, _ := swarmServer(t)
	publishSubmission(t, js, "s-triage", "t-old", consoleConversationPrefix+convTriage, "old work", "")
	publishRun(t, js, "s-triage", "t-old", "old answer", true)
	time.Sleep(100 * time.Millisecond)
	boundary := time.Now()
	time.Sleep(100 * time.Millisecond)

	publishSubmission(t, js, "s-triage", "t-root", consoleConversationPrefix+convTriage, "check the pods", "")
	publishRun(t, js, "s-triage", "t-root", "pods are fine", true)
	publishSubmission(t, js, "db-specialist", "t-child", "slack:C0DELEGATE", "look at the db", "t-root")
	publishRun(t, js, "db-specialist", "t-child", "", false)
	publishSubmission(t, js, "s-other", "t-other", consoleConversationPrefix+convUnknown, "not ours", "")
	publishRun(t, js, "s-other", "t-other", "", false)
	publishSubmission(t, js, "platform", "t-slack", "slack:C0STRANGER", "not ours either", "")
	publishRun(t, js, "platform", "t-slack", "", false)

	req := map[string]any{
		"rootConversationId": "agy-root",
		"conversations":      map[string]any{"triage": clientConv("triage", convTriage, time.Now())},
	}
	swarmTaskWindow = time.Since(boundary)
	resp, _ := askSwarm(t, h, req)
	if !resp.Connected || len(resp.Notes) != 1 {
		t.Fatalf("connected %v, notes %q", resp.Connected, resp.Notes)
	}
	texts := boardTexts(resp.Snapshot)
	for _, want := range []string{"[task `t-root`]", "delegated task `t-child`", "`pods are fine`"} {
		if !strings.Contains(texts, want) {
			t.Errorf("board lacks %q:\n%s", want, texts)
		}
	}
	all := string(swarmJSON(t, resp.Snapshot))
	for _, not := range []string{"t-old", "t-other", "t-slack", "not ours"} {
		if strings.Contains(all, not) {
			t.Errorf("snapshot holds %q:\n%s", not, texts)
		}
	}
	if _, ok := resp.Snapshot.Members["session-triage"]; !ok {
		t.Errorf("no member for the conversation: %v", memberNames(resp.Snapshot))
	}
	if e := resp.Conversations["triage"]; e.Token != convTriage {
		t.Errorf("conversations = %+v", resp.Conversations)
	}

	req["ledger"] = resp.Ledger
	swarmTaskWindow = time.Since(boundary)
	again, _ := askSwarm(t, h, req)
	if len(again.Snapshot.NewMessages) != 0 {
		t.Errorf("a second call with the ledger posted again:\n%s", boardTexts(again.Snapshot))
	}
	if again.Ledger.NextSeq != resp.Ledger.NextSeq {
		t.Errorf("ledger moved from %d to %d with nothing new", resp.Ledger.NextSeq, again.Ledger.NextSeq)
	}
}

// The server's own conversations are projected and returned too, and a
// client entry for one of them takes the server's alias.
func TestTheSwarmRouteMergesTheServersConversations(t *testing.T) {
	h, js, convs := swarmServer(t)
	token, err := convs.Create("from-server", "made here")
	if err != nil {
		t.Fatal(err)
	}
	publishSubmission(t, js, "s-srv", "t-srv", consoleConversationPrefix+token, "server turn", "")
	publishRun(t, js, "s-srv", "t-srv", "", false)
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{
		"client-name": map[string]any{"alias": "client-name", "token": token, "sessionMode": true, "agyConversationId": "agy-1"},
	}})
	e, ok := resp.Conversations["from-server"]
	if !ok || len(resp.Conversations) != 1 || e.Description != "made here" || !e.SessionMode || e.AGYConversationID != "agy-1" {
		t.Errorf("conversations = %+v", resp.Conversations)
	}
	if !strings.Contains(boardTexts(resp.Snapshot), "[task `t-srv`]") {
		t.Errorf("the server conversation's task is missing:\n%s", boardTexts(resp.Snapshot))
	}
}

func TestTheSwarmRouteRefusesTooManyConversations(t *testing.T) {
	h, _, _ := swarmServer(t)
	for _, field := range []string{"conversations", "subagents"} {
		many := map[string]any{}
		for i := 0; i <= maxSwarmInputConvs; i++ {
			many[fmt.Sprintf("c%d", i)] = map[string]any{"alias": fmt.Sprintf("c%d", i), "token": fmt.Sprintf("tok%d", i)}
		}
		raw, _ := json.Marshal(map[string]any{field: many})
		if rec := postSwarm(h, swarmAuth, string(raw)); rec.Code != http.StatusBadRequest {
			t.Errorf("%d %s = %d, want 400", len(many), field, rec.Code)
		}
		delete(many, "c0")
		raw, _ = json.Marshal(map[string]any{field: many})
		if rec := postSwarm(h, swarmAuth, string(raw)); rec.Code != http.StatusOK {
			t.Errorf("%d %s = %d, want 200", len(many), field, rec.Code)
		}
	}
}

// An entry with an invalid token is dropped, not the request; hostile text
// reaches the snapshot inert.
func TestTheSwarmRouteTakesClientInputAsUntrusted(t *testing.T) {
	h, js, _ := swarmServer(t)
	publishSubmission(t, js, "s-x", "t-hostile", consoleConversationPrefix+convTriage, "@coordinator <img src=x> [x](http://evil)", "")
	publishRun(t, js, "s-x", "t-hostile", "", false)
	now := time.Now()
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{
		"@narrator <b>hi</b>": clientConv("@narrator <b>hi</b>", "console:"+convTriage, now),
		"bad-token":           clientConv("bad-token", "Not A.Token", now),
		"too-long-token":      clientConv("too-long-token", strings.Repeat("a", maxSwarmHandleBytes+1), now),
	}})
	if _, ok := resp.Conversations["bad-token"]; ok {
		t.Error("an entry with an invalid token was kept")
	}
	if _, ok := resp.Conversations["too-long-token"]; ok {
		t.Error("an entry with an over-long token was kept")
	}
	if e := resp.Conversations["@narrator <b>hi</b>"]; e.Token != convTriage {
		t.Errorf("a console:-prefixed token was not taken bare: %+v", resp.Conversations)
	}
	var texts []string
	for _, m := range resp.Snapshot.NewMessages {
		texts = append(texts, m.Text)
	}
	for _, m := range resp.Snapshot.Members {
		texts = append(texts, m.Model)
	}
	texts = append(texts, string(swarmJSON(t, resp.Snapshot.View)))
	all := strings.Join(texts, "\n")
	for _, bad := range []string{"<img", "<b>", "@coordinator <", "@narrator <", "](http"} {
		if strings.Contains(all, bad) {
			t.Errorf("snapshot holds %q:\n%s", bad, all)
		}
	}
	if !strings.Contains(all, "t-hostile") {
		t.Errorf("the task is missing, so the test checked nothing:\n%s", all)
	}
}

func TestSwarmConversationParents(t *testing.T) {
	entries := []swarmConversationEntry{
		{Alias: "root", Token: "r", AGYConversationID: "agy-root"},
		{Alias: "by-parent-id", Token: "a", AGYConversationID: "agy-a", ParentAGYConversationID: "agy-root"},
		{Alias: "by-lineage", Token: "b", AGYConversationID: "agy-b"},
		{Alias: "unresolvable", Token: "c", AGYConversationID: "agy-c", ParentAGYConversationID: "agy-nowhere"},
		{Alias: "self", Token: "d", AGYConversationID: "agy-d", ParentAGYConversationID: "agy-d"},
	}
	subagents := map[string]swarmLineage{
		"agy-b": {ConversationID: "agy-b", ParentConversationID: "agy-a", IsSubagent: true},
		"agy-c": {ConversationID: "agy-c", ParentConversationID: "agy-elsewhere"},
	}
	want := map[string]string{"root": "", "by-parent-id": "root", "by-lineage": "by-parent-id", "unresolvable": "", "self": ""}
	for _, c := range swarmConversations(entries, subagents) {
		if c.Parent != want[c.Alias] {
			t.Errorf("%s: parent %q, want %q", c.Alias, c.Parent, want[c.Alias])
		}
	}
}

func TestMergeSwarmConversations(t *testing.T) {
	t0 := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	server := []ConversationInfo{{Alias: "srv", Token: "tok-shared", Description: "server's", LastUsed: t0}}
	client := map[string]swarmConversationEntry{
		"cli-shared": {Token: "console:tok-shared", Description: "client's", LastUsedAt: t0.Add(time.Minute), DebugMode: true},
		"older":      {Token: "tok-dup", LastUsedAt: t0},
		"newer":      {Token: "tok-dup", LastUsedAt: t0.Add(time.Hour)},
		"srv":        {Token: "tok-collides", LastUsedAt: t0.Add(time.Hour)},
		"bad":        {Token: "UPPER"},
		" ":          {Token: "tok-blank"},
	}
	got := map[string]swarmConversationEntry{}
	for _, e := range mergeSwarmConversations(server, client) {
		got[e.Alias] = e
	}
	if len(got) != 2 {
		t.Fatalf("merged = %+v", got)
	}
	if e := got["srv"]; e.Token != "tok-shared" || e.Description != "server's" || !e.LastUsedAt.Equal(t0.Add(time.Minute)) || !e.DebugMode {
		t.Errorf("shared token = %+v", e)
	}
	if e := got["newer"]; e.Token != "tok-dup" {
		t.Errorf("duplicate token = %+v", got)
	}
}

// A TASKS read that stops at its limit is reported, not served as complete.
func TestTheSwarmRouteReportsAPartialRead(t *testing.T) {
	h, js, _ := swarmServer(t)
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "one", "")
	publishRun(t, js, "s-triage", "t-1", "done", true)
	swarmTaskLimit = 2
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{"triage": clientConv("triage", convTriage, time.Now())}})
	if !hasNote(resp, "TASKS read was partial") {
		t.Errorf("notes = %q, want the partial read reported", resp.Notes)
	}
}

func TestTheSwarmRouteSetsTheRootConversation(t *testing.T) {
	h, _, _ := swarmServer(t)
	for _, tc := range []struct{ root, want string }{
		{"agy-root-1", "agy-root-1"},
		{"", ""},
		{"bad\x01id", ""},
		{strings.Repeat("é", maxSwarmConversationIDRunes+1), ""},
		{strings.Repeat("é", maxSwarmConversationIDRunes), strings.Repeat("é", maxSwarmConversationIDRunes)},
	} {
		resp, _ := askSwarm(t, h, map[string]any{"rootConversationId": tc.root})
		for _, name := range []string{swarmMemberCoordinator, swarmMemberPlatform} {
			if got := resp.Snapshot.Members[name].ConversationID; got != tc.want {
				t.Errorf("root %q: %s conversation_id = %q, want %q", tc.root, name, got, tc.want)
			}
		}
		if got := resp.Snapshot.Members[swarmMemberNarrator].ConversationID; got != "" {
			t.Errorf("root %q: the narrator took it too", tc.root)
		}
	}
}

// The answer decodes into the bridge's response, refusing any field it does
// not know beyond the three this server adds; the request and conversation
// types hold the bridge's field sets.
func TestTheSwarmResponseIsTheBridges(t *testing.T) {
	compareWireTypes(t, "swarmConversationEntry", reflect.TypeOf(swarmConversationEntry{}), reflect.TypeOf(bridgeConversationEntry{}), nil)
	compareWireTypes(t, "swarmLineage", reflect.TypeOf(swarmLineage{}), reflect.TypeOf(bridgeConversationLineage{}), nil)

	h, js, _ := swarmServer(t)
	publishSubmission(t, js, "s-triage", "t-root", consoleConversationPrefix+convTriage, "check", "")
	publishRun(t, js, "s-triage", "t-root", "fine", true)
	_, raw := askSwarm(t, h, map[string]any{
		"rootConversationId": "agy-root",
		"conversations": map[string]any{"triage": map[string]any{
			"alias": "triage", "token": convTriage, "sessionMode": true, "subagentRole": "reviewer", "lastUsedAt": time.Now(),
		}},
	})
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var got bridgeRemoteSwarmResponse
	if err := dec.Decode(&got); err != nil {
		t.Fatalf("bridge decode: %v\n%s", err, raw)
	}
	if !got.Connected || got.Snapshot.Title != swarmDefaultTitle || got.Conversations["triage"].SubagentRole != "reviewer" ||
		!got.Conversations["triage"].SessionMode || got.Deployment != nil || got.ActiveConversation != "" || got.PendingEvents == nil {
		t.Errorf("bridge view = %+v", got)
	}
}

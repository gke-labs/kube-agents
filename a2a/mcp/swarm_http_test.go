package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/iotest"
	"time"
	"unicode/utf8"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The bridge's conversation entry (#3689 at 5eb4af7e:
// a2a/mcpbridge/config.go ConversationEntry) and lineage (at 5a706327:
// swarm_lineage.go ConversationLineage, since moved out to the host's own
// integration), copied so the field sets can be held to ours.
type bridgeConversationEntry struct {
	Alias                    string    `json:"alias"`
	Token                    string    `json:"token"`
	SessionMode              bool      `json:"sessionMode"`
	SessionTerminated        bool      `json:"sessionTerminated,omitempty"`
	Description              string    `json:"description,omitempty"`
	HostConversationID       string    `json:"hostConversationId,omitempty"`
	ParentHostConversationID string    `json:"parentHostConversationId,omitempty"`
	SubagentRole             string    `json:"subagentRole,omitempty"`
	SessionWorkerID          string    `json:"sessionWorkerId,omitempty"`
	AutoBound                bool      `json:"autoBound,omitempty"`
	DebugMode                bool      `json:"debugMode,omitempty"`
	Disconnected             bool      `json:"disconnected,omitempty"`
	CreatedAt                time.Time `json:"createdAt"`
	LastUsedAt               time.Time `json:"lastUsedAt"`
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
	req := httptest.NewRequest(http.MethodPost, SwarmPath, iotest.ErrReader(errors.New("connection reset")))
	req.Header.Set(headerAuthorization, "Bearer "+testToken)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("a body that fails to read = %d, want 400: only an oversized body is 413", rec.Code)
	}
	for _, body := range []string{`[]`, `{"conversations":5}`, `{"ledger":"x"}`, `not json`, `{} {}`, `null`, ` null `} {
		if rec := postSwarm(h, swarmAuth, body); rec.Code != http.StatusBadRequest {
			t.Errorf("body %s = %d, want 400", body, rec.Code)
		}
	}
	if rec := postSwarm(h, swarmAuth, `{"rootConversationId":"r","someFutureField":{"a":1}}`); rec.Code != http.StatusOK {
		t.Errorf("an unknown field = %d, want 200: unknown fields are ignored", rec.Code)
	}
	req = httptest.NewRequest(http.MethodGet, SwarmPath, nil)
	req.Header.Set(headerAuthorization, "Bearer "+testToken)
	rec = httptest.NewRecorder()
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
	// And it is not decoded at all: no field of the request takes it, so
	// nothing can log, store or forward it. Decoding is the check, since
	// encoding/json also matches untagged fields and names in any case.
	for _, key := range []string{"deployment", "Deployment", "DEPLOYMENT"} {
		var got swarmRequest
		if err := json.Unmarshal([]byte(`{"`+key+`":{"name":"x","busAuthValue":"secret-marker"}}`), &got); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, swarmRequest{}) {
			t.Errorf("a request holding only %q decoded into %+v; want nothing taken", key, got)
		}
	}
}

// End to end: a console conversation's task and its delegated child are
// projected; a task in another conversation, one off the console that
// nothing delegated, and one older than the window are not. A second call
// with the returned ledger posts nothing again.
func TestTheSwarmRouteProjectsTheConversationsTasks(t *testing.T) {
	sh, _, js := swarmBus(t)
	h := Handler(mustServer(t), writeToken(t, testToken), quietLog(), func(c *handlerConfig) { c.swarm = sh })
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
	// The read's clock is fixed, so the window starts at the boundary
	// exactly, however long the call takes to reach the read.
	now := time.Now()
	sh.now = func() time.Time { return now }
	swarmTaskWindow = now.Sub(boundary)
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
	again, _ := askSwarm(t, h, req)
	if len(again.Snapshot.NewMessages) != 0 {
		t.Errorf("a second call with the ledger posted again:\n%s", boardTexts(again.Snapshot))
	}
	if again.Ledger.NextSeq != resp.Ledger.NextSeq {
		t.Errorf("ledger moved from %d to %d with nothing new", resp.Ledger.NextSeq, again.Ledger.NextSeq)
	}
}

// A request entry whose alias the server's table holds is the server's
// conversation: the bridge sends turns by alias, so the server's token is
// where its turns went. The entry keeps the client's alias and every one of
// its own fields. The bridge replaces its entry with the returned one when any
// of token, sessionMode, sessionTerminated, sessionWorkerId, the Antigravity
// ids, subagentRole or debugMode differ (#3689 at 5a706327:
// a2a/mcpbridge/config.go:1724-1740), so each of those must come back as sent,
// the token aside.
func TestTheSwarmRouteResolvesARequestAliasOnTheServer(t *testing.T) {
	h, js, convs := swarmServer(t)
	serverToken, err := convs.Resolve("", "") // what send_message without a conversation does
	if err != nil {
		t.Fatal(err)
	}
	publishSubmission(t, js, "s-def", "t-def", consoleConversationPrefix+serverToken, "server turn", "")
	publishRun(t, js, "s-def", "t-def", "", false)
	created := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	sent := swarmConversationEntry{
		Alias: DefaultConversation, Token: "kabridgelocal01", SessionMode: true, SessionTerminated: true,
		Description: "the bridge's", HostConversationID: "agy-root", ParentHostConversationID: "agy-parent",
		SubagentRole: "reviewer", SessionWorkerID: "worker-1", AutoBound: true, DebugMode: true, Disconnected: true,
		CreatedAt: created, LastUsedAt: created,
	}
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{DefaultConversation: sent}})
	got, ok := resp.Conversations[DefaultConversation]
	if !ok || len(resp.Conversations) != 1 {
		t.Fatalf("conversations = %+v", resp.Conversations)
	}
	if got.Token != serverToken {
		t.Errorf("token = %q, want the server's %q", got.Token, serverToken)
	}
	if !got.LastUsedAt.After(created) {
		t.Errorf("last use = %v, want the server's, the later", got.LastUsedAt)
	}
	want := sent
	want.Token, want.LastUsedAt = got.Token, got.LastUsedAt
	if got != want {
		t.Errorf("the client's fields did not come back as sent:\n got %+v\nwant %+v", got, want)
	}
	if !strings.Contains(boardTexts(resp.Snapshot), "[task `t-def`]") {
		t.Errorf("the server conversation's task is missing:\n%s", boardTexts(resp.Snapshot))
	}

	// Without a description of its own, the entry takes the server's.
	if _, err := convs.Create("described", "made here"); err != nil {
		t.Fatal(err)
	}
	resp, _ = askSwarm(t, h, map[string]any{"conversations": map[string]any{"described": map[string]any{"alias": "described", "token": "anything"}}})
	if e := resp.Conversations["described"]; e.Description != "made here" {
		t.Errorf("described = %+v, want the server's description", e)
	}
}

// The server's table only resolves the request's names: a conversation the
// request does not name, another caller's, never reaches the board, the
// members or the returned table, nor does its task.
func TestTheSwarmRouteProjectsOnlyTheRequestsConversations(t *testing.T) {
	h, js, convs := swarmServer(t)
	other, err := convs.Create("someone-elses", "")
	if err != nil {
		t.Fatal(err)
	}
	publishSubmission(t, js, "s-x", "t-secret", consoleConversationPrefix+other, "PRIVATE PROMPT of another caller", "")
	publishRun(t, js, "s-x", "t-secret", "", false)
	publishSubmission(t, js, "s-mine", "t-mine", consoleConversationPrefix+"kamine01", "my turn", "")
	publishRun(t, js, "s-mine", "t-mine", "", false)
	resp, raw := askSwarm(t, h, map[string]any{"rootConversationId": "agy-mine", "conversations": map[string]any{
		"mine": map[string]any{"alias": "mine", "token": "kamine01", "hostConversationId": "agy-sub",
			"parentHostConversationId": "agy-mine", "sessionMode": true, "lastUsedAt": time.Now()},
	}})
	for _, not := range []string{"PRIVATE PROMPT", "t-secret", "someone-elses", other} {
		if strings.Contains(string(raw), not) {
			t.Errorf("the response holds %q:\n%s", not, raw)
		}
	}
	if len(resp.Conversations) != 1 {
		t.Errorf("conversations = %+v", resp.Conversations)
	}
	if !strings.Contains(boardTexts(resp.Snapshot), "[task `t-mine`]") {
		t.Errorf("the request's own task is missing, so the test checked nothing:\n%s", boardTexts(resp.Snapshot))
	}
}

// Two of the bridge's entries on one token are its own state: both come back,
// each under its own alias.
func TestTheSwarmRouteKeepsTwoAliasesOnOneToken(t *testing.T) {
	h, _, convs := swarmServer(t)
	if _, err := convs.Create("srv-name", ""); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{
		"first":  clientConv("first", convTriage, now),
		"second": clientConv("second", "console:"+convTriage, now.Add(-time.Hour)),
	}})
	if len(resp.Conversations) != 2 || resp.Conversations["first"].Token != convTriage || resp.Conversations["second"].Token != convTriage ||
		resp.Conversations["first"].Alias != "first" || resp.Conversations["second"].Alias != "second" {
		t.Errorf("conversations = %+v", resp.Conversations)
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
		{Alias: "root", Token: "r", HostConversationID: "agy-root"},
		{Alias: "by-parent-id", Token: "a", HostConversationID: "agy-a", ParentHostConversationID: "agy-root"},
		{Alias: "by-lineage", Token: "b", HostConversationID: "agy-b"},
		{Alias: "unresolvable", Token: "c", HostConversationID: "agy-c", ParentHostConversationID: "agy-nowhere"},
		{Alias: "self", Token: "d", HostConversationID: "agy-d", ParentHostConversationID: "agy-d"},
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
	server := []ConversationInfo{
		{Alias: "srv", Token: "tok-srv", Description: "server's", LastUsed: t0},
		{Alias: "server-only", Token: "tok-only", LastUsed: t0},
	}
	client := map[string]swarmConversationEntry{
		"srv":     {Token: "Not A Token", Description: "client's", LastUsedAt: t0.Add(-time.Minute), DebugMode: true},
		"older":   {Token: "tok-dup", LastUsedAt: t0},
		"newer":   {Token: "console:tok-dup", LastUsedAt: t0.Add(time.Hour)},
		"bad":     {Token: "UPPER CASE"},
		" ":       {Token: "tok-blank"},
		" padded": {Token: "tok-padded"},
	}
	merged, dropped := mergeSwarmConversations(server, client)
	var aliases []string
	got := map[string]swarmConversationEntry{}
	for _, e := range merged {
		aliases = append(aliases, e.Alias)
		got[e.Alias] = e
	}
	if !slices.Equal(aliases, []string{"newer", "older", "srv"}) || dropped != 3 {
		t.Fatalf("merged %q, dropped %d", aliases, dropped)
	}
	if e := got["srv"]; e.Token != "tok-srv" || e.Description != "client's" || !e.LastUsedAt.Equal(t0) || !e.DebugMode {
		t.Errorf("server alias = %+v", e)
	}
	if got["older"].Token != "tok-dup" || got["newer"].Token != "tok-dup" {
		t.Errorf("shared token = %+v", got)
	}
}

// Past its limit, the read takes the newest messages and says the oldest
// were dropped.
func TestTheSwarmRouteReadsTheNewestTasks(t *testing.T) {
	h, js, _ := swarmServer(t)
	for i := 0; i < 6; i++ {
		id := fmt.Sprintf("t-%d", i)
		publishSubmission(t, js, "s-triage", id, consoleConversationPrefix+convTriage, "turn "+id, "")
		publishRun(t, js, "s-triage", id, "", false)
	}
	swarmTaskLimit = 9 // three messages a task: the newest three fit
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{"triage": clientConv("triage", convTriage, time.Now())}})
	all := string(swarmJSON(t, resp.Snapshot))
	for i := 0; i < 6; i++ {
		id := fmt.Sprintf("`t-%d`", i)
		if shown := strings.Contains(all, id); shown != (i >= 3) {
			t.Errorf("%s shown = %v, want %v:\n%s", id, shown, i >= 3, boardTexts(resp.Snapshot))
		}
	}
	if !hasNote(resp, "the oldest were not read") || hasNote(resp, "partial") {
		t.Errorf("notes = %q, want the oldest reported dropped", resp.Notes)
	}
}

// A window holding more than the bus reader's byte budget is read oldest
// first and stops at the budget, so the answer says the newest may be
// missing, and the cut read is not cached for the next caller.
func TestASwarmReadPastTheBudgetSaysTheNewestMayBeMissing(t *testing.T) {
	h, _, js := swarmBus(t)
	// The test bus caps each stream at 1 MiB; this one must hold more than
	// the reader's budget.
	cfg := jetstream.StreamConfig{Name: lib.TasksStream, Subjects: []string{"a2a.tasks.>"}, MaxBytes: 4 * readMaxBytes}
	if _, err := js.UpdateStream(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	big := strings.Repeat("x", 600<<10)
	for i := 0; i < 8; i++ {
		id := fmt.Sprintf("t-old%d", i)
		publishSubmission(t, js, "s-triage", id, consoleConversationPrefix+convTriage, "turn "+id, "")
		publishArtifact(t, js, "s-triage", id, lib.ArtifactResult, big)
	}
	publishSubmission(t, js, "s-triage", "t-new", consoleConversationPrefix+convTriage, "the newest turn", "")

	set := h.taskSet(context.Background())
	if !slices.ContainsFunc(set.notes, func(n string) bool { return strings.Contains(n, "the newest tasks and states may be missing") }) {
		t.Errorf("notes = %q, want the budget cut reported", set.notes)
	}
	if !h.cachedAt.IsZero() {
		t.Error("a read cut by the byte budget was cached")
	}
}

func TestTheSwarmRouteSetsTheRootConversation(t *testing.T) {
	h, _, _ := swarmServer(t)
	for _, tc := range []struct{ root, want string }{
		{"agy-root-1", "agy-root-1"},
		{"", ""},
		{"bad\x01id", ""},
		{"  agy-root-2  ", "agy-root-2"},
		{"../../x", ""},
		{"a/b", ""},
		{".hidden", ""},
		{"CON", ""},
		{"a b", ""},
		{"é", ""},
		{strings.Repeat("r", maxConversationIDBytes+1), ""},
		{strings.Repeat("r", maxConversationIDBytes), strings.Repeat("r", maxConversationIDBytes)},
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

// swarmBus is a fresh bus, its connection, and a swarm handler on them with
// the TASKS window and drain limit restored after the test.
func swarmBus(t *testing.T) (*swarmHandler, *NATSBus, jetstream.JetStream) {
	t.Helper()
	url, js := testBus(t)
	bus := dial(t, url)
	oldWindow, oldLimit := swarmTaskWindow, swarmTaskLimit
	t.Cleanup(func() { swarmTaskWindow, swarmTaskLimit = oldWindow, oldLimit })
	return &swarmHandler{bus: bus, convs: NewConversations()}, bus, js
}

// Concurrent and rapid swarm calls share one TASKS read, so they hold one
// reader slot between them, and a turn's watch alongside them still gets one.
func TestSwarmCallsShareOneReader(t *testing.T) {
	sh, bus, js := swarmBus(t)
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "one", "")
	publishRun(t, js, "s-triage", "t-1", "", false)
	h := Handler(mustServer(t), writeToken(t, testToken), quietLog(), func(c *handlerConfig) { c.swarm = sh })
	body := map[string]any{"conversations": map[string]any{"triage": clientConv("triage", convTriage, time.Now())}}
	var wg sync.WaitGroup
	for i := 0; i < 2*maxTaskReaders; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			raw, _ := json.Marshal(body)
			if rec := postSwarm(h, swarmAuth, string(raw)); rec.Code != http.StatusOK {
				t.Errorf("status %d", rec.Code)
			}
		}()
	}
	wg.Wait()
	for i := 0; i < maxTaskReaders; i++ {
		resp, _ := askSwarm(t, h, body)
		if hasNote(resp, "readers were busy") || !strings.Contains(boardTexts(resp.Snapshot), "t-1") {
			t.Fatalf("call %d: notes %q\n%s", i, resp.Notes, boardTexts(resp.Snapshot))
		}
	}
	if held := len(bus.readers); held > 1 {
		t.Errorf("swarm calls hold %d reader slots, want at most 1", held)
	}
	w, err := bus.watchTask(context.Background(), TaskRef{Addressee: "s-triage", TaskID: "t-1"})
	if err != nil {
		t.Fatalf("a turn's watch beside the swarm calls: %v", err)
	}
	w.close()
}

// One read answers every call for swarmReadTTL; then the next call reads
// again.
func TestTheSwarmReadIsCachedBriefly(t *testing.T) {
	sh, _, js := swarmBus(t)
	clock := time.Now()
	sh.now = func() time.Time { return clock }
	req := swarmRequest{Conversations: map[string]swarmConversationEntry{"triage": {Token: convTriage, LastUsedAt: clock}}}
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "one", "")
	sh.answer(context.Background(), req, clock)
	publishSubmission(t, js, "s-triage", "t-2", consoleConversationPrefix+convTriage, "two", "")
	clock = clock.Add(swarmReadTTL - time.Millisecond)
	if b := boardTexts(sh.answer(context.Background(), req, clock).Snapshot); strings.Contains(b, "t-2") {
		t.Errorf("a call inside the TTL read again:\n%s", b)
	}
	clock = clock.Add(time.Millisecond)
	if b := boardTexts(sh.answer(context.Background(), req, clock).Snapshot); !strings.Contains(b, "t-2") {
		t.Errorf("a call past the TTL did not read again:\n%s", b)
	}
}

// With every reader busy the read is skipped, said so, and not cached.
func TestTheSwarmRouteSaysWhenTheReadersAreBusy(t *testing.T) {
	sh, bus, js := swarmBus(t)
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "one", "")
	req := swarmRequest{Conversations: map[string]swarmConversationEntry{"triage": {Token: convTriage, LastUsedAt: time.Now()}}}
	var releases []func(bool)
	for i := 0; i < maxTaskReaders; i++ {
		release, err := bus.readerSlot()
		if err != nil {
			t.Fatal(err)
		}
		releases = append(releases, release)
	}
	resp := sh.answer(context.Background(), req, time.Now())
	if !hasNote(resp, "skipped because this server's bus readers were busy") || hasNote(resp, "partial") {
		t.Errorf("notes = %q", resp.Notes)
	}
	for _, release := range releases {
		release(false)
	}
	resp = sh.answer(context.Background(), req, time.Now())
	if hasNote(resp, "busy") || !strings.Contains(boardTexts(resp.Snapshot), "t-1") {
		t.Errorf("the skipped read was cached: notes %q\n%s", resp.Notes, boardTexts(resp.Snapshot))
	}
}

// A task submitted before the window, with a steer inside it, is live and
// shown, but the steer is not its submission: no prompt, and the time is the
// submission's, which the read saw before the window.
func TestASteerIsNotTheSubmission(t *testing.T) {
	sh, bus, js := swarmBus(t)
	publishSubmission(t, js, "s-triage", "t-straddle", consoleConversationPrefix+convTriage, "original prompt", "")
	time.Sleep(150 * time.Millisecond)
	boundary := time.Now()
	time.Sleep(50 * time.Millisecond)
	publishSubmission(t, js, "s-triage", "t-straddle", consoleConversationPrefix+convTriage, "STEER TEXT", "")
	publishRun(t, js, "s-triage", "t-straddle", "", false)
	read, err := bus.swarmTasks(context.Background(), boundary)
	if err != nil || len(read.tasks) != 1 {
		t.Fatalf("read %+v, %v", read, err)
	}
	if got := read.tasks[0]; got.Prompt != "" || !got.Submitted.Before(boundary) || got.Conversation != consoleConversationPrefix+convTriage {
		t.Errorf("task = prompt %q, submitted %v (window from %v), conversation %q", got.Prompt, got.Submitted, boundary, got.Conversation)
	}
	now := time.Now()
	sh.now = func() time.Time { return now }
	swarmTaskWindow = now.Sub(boundary)
	resp := sh.answer(context.Background(), swarmRequest{Conversations: map[string]swarmConversationEntry{"triage": {Token: convTriage, LastUsedAt: time.Now()}}}, time.Now())
	all := string(swarmJSON(t, resp.Snapshot))
	if !strings.Contains(all, "t-straddle") || strings.Contains(all, "STEER") {
		t.Errorf("want the task shown without the steer:\n%s", all)
	}
}

// When the read starts after a task's submission (its oldest messages past
// the limit), an .in after the task's events is a steer, not a submission.
func TestAnInAfterTheTasksEventsIsNotItsPrompt(t *testing.T) {
	_, bus, js := swarmBus(t)
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "original prompt", "")
	publishRun(t, js, "s-triage", "t-1", "", false)
	time.Sleep(50 * time.Millisecond)
	steerAt := time.Now()
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "STEER TEXT", "")
	swarmTaskLimit = 3 // the two events and the steer
	read, err := bus.swarmTasks(context.Background(), time.Now().Add(-time.Hour))
	if err != nil || len(read.tasks) != 1 || !read.oldestDropped {
		t.Fatalf("read %+v, %v", read, err)
	}
	if p := read.tasks[0].Prompt; p != "" {
		t.Errorf("prompt = %q, want none", p)
	}
	// Nor is the steer's time the task's start: its first event is earlier.
	if s := read.tasks[0].Submitted; !s.Before(steerAt) {
		t.Errorf("submitted %v, want no later than the first event, before the steer at %v", s, steerAt)
	}
	// And with the submission in the read, it is the prompt.
	swarmTaskLimit = 4
	read, err = bus.swarmTasks(context.Background(), time.Now().Add(-time.Hour))
	if err != nil || len(read.tasks) != 1 || read.oldestDropped {
		t.Fatalf("read %+v, %v", read, err)
	}
	if p := read.tasks[0].Prompt; p != "original prompt" {
		t.Errorf("prompt = %q, want the submission's", p)
	}
}

// A terminal status on the supervisor subject from anyone but the gateway is
// a forgery the agreement check drops: it is not folded.
func TestTheSwarmReadDropsAForgedEvent(t *testing.T) {
	_, bus, js := swarmBus(t)
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "one", "")
	publishRun(t, js, "s-triage", "t-1", "", false)
	p := mustJSON(t, lib.StatusUpdate{TaskID: "t-1", ContextID: "ctx-t-1", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true})
	env, err := lib.NewStatusUpdateEnvelope(executor(otherSession), "t-1", "ctx-t-1", "corr-t-1", p)
	if err != nil {
		t.Fatal(err)
	}
	pub(t, js, lib.TaskSupervisorSubject("s-triage", "t-1"), env)
	read, err := bus.swarmTasks(context.Background(), time.Now().Add(-time.Hour))
	if err != nil || len(read.tasks) != 1 || read.tasks[0].Task == nil {
		t.Fatalf("read %+v, %v", read, err)
	}
	if got := read.tasks[0].Task; got.State != lib.StateWorking || got.Final {
		t.Errorf("the forged status was folded: state %s, final %v", got.State, got.Final)
	}
}

// A client entry's strings past their limits come back empty, not
// shortened; an Antigravity id that isn't one is dropped.
func TestTheSwarmRouteCapsClientFields(t *testing.T) {
	h, _, _ := swarmServer(t)
	long := strings.Repeat("a", maxSwarmHandleBytes+1)
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{
		"capped": map[string]any{"alias": "capped", "token": convTriage, "subagentRole": long, "sessionWorkerId": long,
			"description": strings.Repeat("d", swarmScanBytes+1), "hostConversationId": "agy\x01bad",
			"parentHostConversationId": strings.Repeat("p", maxSwarmConversationIDRunes+1)},
		"kept": map[string]any{"alias": "kept", "token": convUnknown, "subagentRole": "reviewer", "sessionWorkerId": "w-1",
			"description": "fine", "hostConversationId": "agy-ok", "parentHostConversationId": "agy-parent"},
	}})
	if e := resp.Conversations["capped"]; e.SubagentRole != "" || e.SessionWorkerID != "" || e.Description != "" ||
		e.HostConversationID != "" || e.ParentHostConversationID != "" || e.Token != convTriage {
		t.Errorf("capped = %+v", e)
	}
	if e := resp.Conversations["kept"]; e.SubagentRole != "reviewer" || e.SessionWorkerID != "w-1" || e.Description != "fine" ||
		e.HostConversationID != "agy-ok" || e.ParentHostConversationID != "agy-parent" {
		t.Errorf("kept = %+v", e)
	}
}

// Dropped request entries are counted in notes, not named.
func TestTheSwarmRouteCountsDroppedEntries(t *testing.T) {
	h, _, _ := swarmServer(t)
	now := time.Now()
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{
		"secret-name-1": clientConv("secret-name-1", "Not A.Token", now),
		"secret-name-2": clientConv("secret-name-2", "", now),
		"fine":          clientConv("fine", convTriage, now),
	}})
	if !hasNote(resp, "2 conversation entries in the request were dropped") || hasNote(resp, "secret-name") {
		t.Errorf("notes = %q", resp.Notes)
	}
}

// A task delegated from a delegated task is kept too, even when the read
// reaches it before its parent, so a second hop is needed to keep it.
func TestTheSwarmRouteKeepsAGrandchild(t *testing.T) {
	h, js, _ := swarmServer(t)
	publishSubmission(t, js, "s-triage", "t-root", consoleConversationPrefix+convTriage, "root", "")
	publishSubmission(t, js, "net-specialist", "t-grandchild", "slack:C0DELEGATE", "grandchild", "t-child")
	publishSubmission(t, js, "db-specialist", "t-child", "slack:C0DELEGATE", "child", "t-root")
	resp, _ := askSwarm(t, h, map[string]any{"conversations": map[string]any{"triage": clientConv("triage", convTriage, time.Now())}})
	if all := string(swarmJSON(t, resp.Snapshot)); !strings.Contains(all, "t-grandchild") {
		t.Errorf("the grandchild is missing:\n%s", boardTexts(resp.Snapshot))
	}
}

// Without the agent cards the answer says so.
func TestTheSwarmRouteSaysWhenTheCardsAreUnavailable(t *testing.T) {
	h, js, _ := swarmServer(t)
	if err := js.DeleteStream(context.Background(), streamDirectory); err != nil {
		t.Fatal(err)
	}
	if resp, _ := askSwarm(t, h, map[string]any{}); !hasNote(resp, noteCardsUnavailable) {
		t.Errorf("notes = %q", resp.Notes)
	}
}

// Off the bus, the answer says so and reads nothing.
func TestTheSwarmRouteSaysWhenItIsNotConnected(t *testing.T) {
	sh, bus, _ := swarmBus(t)
	bus.Close()
	resp := sh.answer(context.Background(), swarmRequest{}, time.Now())
	if resp.Connected || !hasNote(resp, noteNotConnected) {
		t.Errorf("connected %v, notes %q", resp.Connected, resp.Notes)
	}
}

// A read its caller cut short answers that caller only: the next call reads
// again rather than taking the cut-short read from the cache.
func TestACutShortSwarmReadIsNotCached(t *testing.T) {
	sh, _, js := swarmBus(t)
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "one", "")
	req := swarmRequest{Conversations: map[string]swarmConversationEntry{"triage": {Token: convTriage, LastUsedAt: time.Now()}}}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if first := sh.answer(ctx, req, time.Now()); !hasNote(first, "the TASKS read was partial") {
		t.Errorf("a cut-short read was not reported in notes: %q", first.Notes)
	}
	if b := boardTexts(sh.answer(context.Background(), req, time.Now()).Snapshot); !strings.Contains(b, "t-1") {
		t.Errorf("the cut-short read was cached:\n%s", b)
	}
}

// A session member carries its conversation's Antigravity id, from the
// request entry, as the bridge's local fallback does.
func TestTheSwarmRouteGivesSessionMembersTheirConversationID(t *testing.T) {
	h, js, _ := swarmServer(t)
	publishSubmission(t, js, "s-triage", "t-1", consoleConversationPrefix+convTriage, "turn", "")
	publishRun(t, js, "s-triage", "t-1", "", false)
	entry := clientConv("triage", convTriage, time.Now())
	entry["hostConversationId"] = "agy-sub-1"
	resp, _ := askSwarm(t, h, map[string]any{"rootConversationId": "agy-root", "conversations": map[string]any{"triage": entry}})
	found := false
	for name, m := range resp.Snapshot.Members {
		if m.ConversationID == "agy-sub-1" {
			found = true
			if name == swarmMemberCoordinator || name == swarmMemberPlatform {
				t.Errorf("the session's id landed on %s", name)
			}
		}
	}
	if !found {
		t.Errorf("no member carries the session's conversation id: %+v", resp.Snapshot.Members)
	}
}

// A task's record is bounded too: a prompt is capped at maxSwarmPromptRunes,
// and an over-long conversation reference is not kept.
func TestTheSwarmReadBoundsWhatATaskKeeps(t *testing.T) {
	url, js := testBus(t)
	publishSubmission(t, js, "s-triage", "t-long", consoleConversationPrefix+convTriage, strings.Repeat("p", maxSwarmPromptRunes+1000), "")
	publishSubmission(t, js, "s-triage", "t-longref", consoleConversationPrefix+strings.Repeat("c", maxSwarmRefBytes), "q", "")
	read, err := dial(t, url).swarmTasks(context.Background(), time.Now().Add(-time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	for _, task := range read.tasks {
		if n := utf8.RuneCountInString(task.Prompt); n > maxSwarmPromptRunes {
			t.Errorf("%s kept a %d-rune prompt; the cap is %d", task.TaskID, n, maxSwarmPromptRunes)
		}
		if task.TaskID == "t-longref" && task.Conversation != "" {
			t.Errorf("an over-long conversation reference was kept (%d bytes)", len(task.Conversation))
		}
	}
}

// A task waiting on input carries its question and the question's stream
// sequence into the projection: the latest input-required message, text
// only and capped.
func TestTheSwarmReadCarriesATasksQuestion(t *testing.T) {
	url, js := testBus(t)
	publishSubmission(t, js, "s-triage", "t-ask", consoleConversationPrefix+convTriage, "turn", "")
	ask := func(text string) {
		msg := &lib.Message{Role: "agent", MessageID: "m-" + text[:3], Parts: []lib.Part{{Kind: "text", Text: text}, {Kind: "data", Data: json.RawMessage(`{"x":1}`)}}}
		p := mustJSON(t, lib.StatusUpdate{TaskID: "t-ask", ContextID: "ctx", Status: lib.TaskStatus{State: lib.StateInputRequired, Message: msg}})
		env, err := lib.NewStatusUpdateEnvelope(executor("s-triage"), "t-ask", "ctx", "corr", p)
		if err != nil {
			t.Fatal(err)
		}
		pub(t, js, lib.TaskEventsSubject("s-triage", "t-ask"), env)
	}
	ask("Which namespace first?")
	ask("Delete the pod " + strings.Repeat("x", maxSwarmQuestionRunes))
	read, err := dial(t, url).swarmTasks(context.Background(), time.Now().Add(-time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if len(read.tasks) != 1 || read.tasks[0].Question == nil {
		t.Fatalf("tasks %+v; want one carrying its question", read.tasks)
	}
	q := read.tasks[0]
	if len(q.Question.Parts) != 1 || !strings.HasPrefix(q.Question.Parts[0].Text, "Delete the pod") {
		t.Errorf("question = %+v; want the latest one, text parts only", q.Question.Parts)
	}
	if n := utf8.RuneCountInString(q.Question.Parts[0].Text); n > maxSwarmQuestionRunes+8 {
		t.Errorf("question kept %d runes, cap %d", n, maxSwarmQuestionRunes)
	}
	if q.QuestionSeq == 0 {
		t.Error("the question's stream sequence was not carried")
	}
}

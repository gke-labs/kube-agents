package mcp

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// ConfigStore: persistence, conversation bindings, and cross-process coherence.

func TestConfigStorePersistenceAndConversations(t *testing.T) {
	cfgFile := filepath.Join(t.TempDir(), "mcp-config.json")
	store, err := NewConfigStore(cfgFile)
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}

	err = store.UpsertDeployments([]DeploymentProfile{
		{
			Name:              testClusterName,
			KubeContext:       testKubeContext,
			Namespace:         testNamespace,
			PlatformAgentName: testPAName,
			ClusterName:       testClusterName,
			GitHubRepo:        testGitHubRepo,
			Ready:             true,
		},
	})
	if err != nil {
		t.Fatalf("UpsertDeployments: %v", err)
	}

	active, ok := store.ActiveDeployment()
	if !ok || active.Name != testClusterName {
		t.Fatalf("ActiveDeployment = %+v, ok=%v; want %s", active, ok, testClusterName)
	}

	conv, err := store.EnsureConversation("incident-42", true, "Investigating node upgrade")
	if err != nil {
		t.Fatalf("EnsureConversation: %v", err)
	}
	if conv.Alias != "incident-42" || !strings.HasPrefix(conv.Token, defaultConvTokenPrefix) {
		t.Fatalf("unexpected conversation entry: %+v", conv)
	}
	if _, err := store.SetConversationSessionMode("incident-42", true); err != nil {
		t.Fatalf("SetConversationSessionMode: %v", err)
	}

	reloaded, err := NewConfigStore(cfgFile)
	if err != nil {
		t.Fatalf("reloading ConfigStore: %v", err)
	}
	if reloaded.ActiveConversation().Alias != "incident-42" || !reloaded.ActiveConversation().SessionMode {
		t.Fatalf("reloaded conversation state mismatch: %+v", reloaded.ActiveConversation())
	}

	ev := PushEvent{
		ID:        "ev-1",
		Kind:      eventKindProactiveNotify,
		Title:     "Upgrade Check",
		Summary:   "Cluster is ready for 1.32",
		Timestamp: time.Now().UTC(),
	}
	if err := reloaded.AppendPendingEvents(ev); err != nil {
		t.Fatalf("AppendPendingEvents: %v", err)
	}
	if err := reloaded.AppendPendingEvents(ev); err != nil {
		t.Fatalf("duplicate AppendPendingEvents: %v", err)
	}
	peeked := reloaded.PeekPendingEvents()
	if len(peeked) != 1 {
		t.Fatalf("PeekPendingEvents len = %d, want 1", len(peeked))
	}
	drained, err := reloaded.DrainPendingEvents()
	if err != nil || len(drained) != 1 {
		t.Fatalf("DrainPendingEvents = %+v, err=%v", drained, err)
	}
	if after := reloaded.PeekPendingEvents(); len(after) != 0 {
		t.Fatalf("expected 0 events after drain, got %d", len(after))
	}
}

// TestConcurrentAGYConversationsGetIsolatedConsoleConversations reproduces soak round 1:
// Antigravity conversation A ("connect", then "when was the last fleet-audit run?") and Antigravity
// conversation B (`conversation: "soak-r1b"`, long fleet health check) share one MCP server
// process. B's explicit alias became the global active conversation, so A's next send_message
// (no `conversation` arg) landed on B's console token, was absorbed as a steer by B's in-flight
// task, and A streamed B's task for 12 minutes.
func TestConcurrentAGYConversationsGetIsolatedConsoleConversations(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{store: store}
	ctxA := withCallMeta(context.Background(), rpcCallToolMeta{ConversationID: "aaaaaaaa-1111-4111-8111-111111111111"})
	ctxB := withCallMeta(context.Background(), rpcCallToolMeta{ConversationID: "bbbbbbbb-2222-4222-8222-222222222222"})
	enabled := true
	// HandleJSONRPC records the push binding for every _meta-carrying call before dispatch; replay
	// that pre-step so the test covers the real request path (it used to steal the active
	// conversation for the newest Antigravity id, which is exactly how round 2 re-hijacked).
	preStep := func(ctx context.Context) {
		_, _ = store.UpdatePushConfig(callMetaFromContext(ctx).ConversationID, &enabled, "")
	}

	// A connects: binds the default conversation.
	preStep(ctxA)
	a1 := s.conversationFor(ctxA, "", "")
	if a1.Alias != defaultConvAlias {
		t.Fatalf("A should bind the default conversation, got %q", a1.Alias)
	}
	// B explicitly uses its own alias (becomes the global active conversation).
	preStep(ctxB)
	b1 := s.conversationFor(ctxB, "soak-r1b", "")
	if b1.Alias != "soak-r1b" || store.ActiveConversation().Alias != "soak-r1b" {
		t.Fatalf("B should use soak-r1b and make it active, got %q / active %q", b1.Alias, store.ActiveConversation().Alias)
	}
	// A's next message with no conversation arg must stay on A's conversation, not B's.
	preStep(ctxA)
	a2 := s.conversationFor(ctxA, "", "")
	if a2.Alias != a1.Alias || a2.Token != a1.Token {
		t.Fatalf("A hijacked onto %q (token %s); want %q (token %s)", a2.Alias, a2.Token, a1.Alias, a1.Token)
	}
	// And B keeps its own.
	preStep(ctxB)
	b2 := s.conversationFor(ctxB, "", "")
	if b2.Alias != "soak-r1b" {
		t.Fatalf("B lost its conversation: %q", b2.Alias)
	}
	// A third Antigravity conversation arriving while the active conversation is bound elsewhere gets a
	// fresh auto alias rather than stealing.
	ctxC := withCallMeta(context.Background(), rpcCallToolMeta{ConversationID: "cccccccc-3333-4333-8333-333333333333"})
	preStep(ctxC)
	c1 := s.conversationFor(ctxC, "", "")
	if c1.Alias != agyAliasPrefix+"cccccccc" || c1.Token == a1.Token || c1.Token == b1.Token {
		t.Fatalf("C should get an isolated auto-bound conversation, got %q (token %s)", c1.Alias, c1.Token)
	}
	// Push routing for events on A's console token resolves back to Antigravity conversation A.
	if got := store.ResolveAGYConversationIDForEvent("console:" + a1.Token); got != "aaaaaaaa-1111-4111-8111-111111111111" {
		t.Fatalf("event routing for A's token resolved to %q", got)
	}
	// Non-Antigravity callers (no _meta) still use the active conversation.
	plain := s.conversationFor(context.Background(), "", "")
	if plain.Alias != store.ActiveConversation().Alias {
		t.Fatalf("plain caller should get the active conversation, got %q", plain.Alias)
	}
	// Switching A explicitly moves A's binding (one console conversation per Antigravity conversation).
	s.conversationFor(ctxA, "fleet-health", "")
	if got := s.conversationFor(ctxA, "", "").Alias; got != "fleet-health" {
		t.Fatalf("after explicit switch A should resolve to fleet-health, got %q", got)
	}
	if got := store.Snapshot().Conversations[defaultConvAlias].AGYConversationID; got != "" {
		t.Fatalf("old binding on %q should have been released, still %q", a1.Alias, got)
	}
}

// Two stores on one file stand in for two processes (an MCP server and a `--hook` run): a
// mutation through one must not clobber what the other wrote in between, and both must read
// the other's writes.
func TestConfigStoreCrossProcessCoherence(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mcp-config.json")
	server, err := NewConfigStore(path)
	if err != nil {
		t.Fatal(err)
	}
	hook, err := NewConfigStore(path)
	if err != nil {
		t.Fatal(err)
	}

	// The hook binds the Antigravity conversation and the server, holding a stale in-memory copy,
	// then records a deployment: a whole-file write from the stale copy would lose the binding.
	if _, err := hook.UpdatePushConfig("agy-conv-1", nil, ""); err != nil {
		t.Fatal(err)
	}
	if err := server.UpsertDeployments([]DeploymentProfile{{Name: "host", KubeContext: "ctx-host", Ready: true}}); err != nil {
		t.Fatal(err)
	}
	if got := hook.Snapshot(); got.Push.AGYConversationID != "agy-conv-1" || got.Deployments["host"].KubeContext != "ctx-host" {
		t.Fatalf("hook sees push=%q deployments=%v; want both writes", got.Push.AGYConversationID, got.Deployments)
	}
	if got := server.Snapshot(); got.Push.AGYConversationID != "agy-conv-1" || !got.Push.Enabled {
		t.Fatalf("server lost the hook's push binding: %+v", got.Push)
	}

	// A second Antigravity conversation bound through the hook's store is visible to the server's
	// conversation lookup without a restart.
	hook.ConversationForAGY("agy-conv-2")
	if entry := server.ConversationForAGY("agy-conv-2"); entry.AGYConversationID != "agy-conv-2" {
		t.Fatalf("server minted a second conversation for agy-conv-2 instead of reusing the hook's: %+v", entry)
	}

	// Concurrent appends from many stores keep every event: the queue is a read-modify-write
	// that the file lock serializes.
	const writers, perWriter = 8, 20
	var wg sync.WaitGroup
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			st, err := NewConfigStore(path)
			if err != nil {
				t.Error(err)
				return
			}
			for i := 0; i < perWriter; i++ {
				if err := st.AppendPendingEvents(PushEvent{ID: fmt.Sprintf("ev-%d-%d", w, i), Kind: eventKindTaskCompleted}); err != nil {
					t.Error(err)
					return
				}
			}
		}(w)
	}
	wg.Wait()
	events, err := hook.DrainPendingEvents()
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != writers*perWriter {
		t.Fatalf("drained %d events, want %d (lost updates across concurrent writers)", len(events), writers*perWriter)
	}
	if again, _ := server.DrainPendingEvents(); len(again) != 0 {
		t.Fatalf("a second drain returned %d events; the first should have emptied the queue for every process", len(again))
	}
}

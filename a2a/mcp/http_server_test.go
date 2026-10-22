package mcp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestHTTPHandler_JSONRPCAndSwarmAndAuth(t *testing.T) {
	ns, _, _ := startEmbeddedJetStream(t)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	srv, err := NewInClusterServer(ctx, InClusterOptions{
		NATSURL:           ns.ClientURL(),
		Namespace:         "kubeagents-system",
		PlatformAgentName: "platform-agent",
		ClusterName:       "test-cluster",
		GitHubRepo:        "example-org/kube-agents",
		ConfigPath:        t.TempDir() + "/mcp-config.json",
	})
	if err != nil {
		t.Fatalf("NewInClusterServer: %v", err)
	}
	defer srv.Close()

	httpSrv := httptest.NewServer(srv.HTTPHandler("secret-token"))
	defer httpSrv.Close()

	// 1. /healthz should be unauthenticated and return 200 OK.
	resp, err := http.Get(httpSrv.URL + MCPHealthPath)
	if err != nil {
		t.Fatalf("GET /healthz: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET /healthz status = %d, want 200", resp.StatusCode)
	}

	// 2. Unauthenticated POST /mcp should return 401 Unauthorized.
	unauthReq, _ := http.NewRequestWithContext(ctx, http.MethodPost, httpSrv.URL+MCPPath, strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"ping"}`))
	unauthResp, err := http.DefaultClient.Do(unauthReq)
	if err != nil {
		t.Fatalf("POST /mcp unauth: %v", err)
	}
	unauthResp.Body.Close()
	if unauthResp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("POST /mcp unauth status = %d, want 401", unauthResp.StatusCode)
	}

	// 3. Authenticated tools/list over POST /mcp.
	listReq, _ := http.NewRequestWithContext(ctx, http.MethodPost, httpSrv.URL+MCPPath, strings.NewReader(`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`))
	listReq.Header.Set(headerAuthorization, "Bearer secret-token")
	listReq.Header.Set(headerContentType, contentTypeJSON)
	listResp, err := http.DefaultClient.Do(listReq)
	if err != nil {
		t.Fatalf("POST /mcp tools/list: %v", err)
	}
	var rpcOut struct {
		Result struct {
			Tools []MCPToolDefinition `json:"tools"`
		} `json:"result"`
	}
	if err := json.NewDecoder(listResp.Body).Decode(&rpcOut); err != nil {
		listResp.Body.Close()
		t.Fatalf("decoding tools/list response: %v", err)
	}
	listResp.Body.Close()
	if len(rpcOut.Result.Tools) != 9 {
		t.Fatalf("tools/list returned %d tools, want 9", len(rpcOut.Result.Tools))
	}

	// 4. Authenticated NDJSON streaming POST /mcp (connection_status).
	callBody := `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"connection_status","arguments":{},"_meta":{"progressToken":"tok-1"}}}`
	ndReq, _ := http.NewRequestWithContext(ctx, http.MethodPost, httpSrv.URL+MCPPath, strings.NewReader(callBody))
	ndReq.Header.Set(headerAuthorization, "Bearer secret-token")
	ndReq.Header.Set(headerAccept, contentTypeNDJSON)
	ndResp, err := http.DefaultClient.Do(ndReq)
	if err != nil {
		t.Fatalf("POST /mcp ndjson: %v", err)
	}
	scanner := bufio.NewScanner(ndResp.Body)
	var frames []string
	for scanner.Scan() {
		if line := strings.TrimSpace(scanner.Text()); line != "" {
			frames = append(frames, line)
		}
	}
	ndResp.Body.Close()
	if len(frames) == 0 {
		t.Fatal("expected at least 1 NDJSON frame from POST /mcp")
	}

	// 5. Authenticated POST /mcp/swarm computes SwarmSnapshot and merges conversations.
	swarmReqPayload := RemoteSwarmRequest{
		RootConversationID: "11111111-2222-3333-4444-555555555555",
		Conversations: map[string]ConversationEntry{
			"session-alpha": {
				Alias:             "session-alpha",
				Token:             "agy-alpha1",
				SessionMode:       true,
				AGYConversationID: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
				CreatedAt:         time.Now().UTC(),
				LastUsedAt:        time.Now().UTC(),
			},
		},
		ActiveConversation: "session-alpha",
	}
	rawSwarmReq, _ := json.Marshal(swarmReqPayload)
	swarmReq, _ := http.NewRequestWithContext(ctx, http.MethodPost, httpSrv.URL+MCPSwarmPath, bytes.NewReader(rawSwarmReq))
	swarmReq.Header.Set(headerAuthorization, "Bearer secret-token")
	swarmResp, err := http.DefaultClient.Do(swarmReq)
	if err != nil {
		t.Fatalf("POST /mcp/swarm: %v", err)
	}
	var swarmOut RemoteSwarmResponse
	if err := json.NewDecoder(swarmResp.Body).Decode(&swarmOut); err != nil {
		swarmResp.Body.Close()
		t.Fatalf("decoding POST /mcp/swarm: %v", err)
	}
	swarmResp.Body.Close()
	if !swarmOut.Connected {
		t.Fatal("expected RemoteSwarmResponse.Connected = true")
	}
	if _, ok := swarmOut.Snapshot.Members["coordinator"]; !ok {
		t.Fatalf("expected coordinator in SwarmSnapshot.Members, got %+v", swarmOut.Snapshot.Members)
	}
	if _, ok := swarmOut.Snapshot.Members["platform"]; !ok {
		t.Fatalf("expected platform in SwarmSnapshot.Members, got %+v", swarmOut.Snapshot.Members)
	}
	if _, ok := swarmOut.Conversations["session-alpha"]; !ok {
		t.Fatalf("expected session-alpha in merged Conversations, got %+v", swarmOut.Conversations)
	}

	// 6. Authenticated GET /mcp/events streams live PushEvent items as SSE `event: push`.
	eventsCtx, cancelEvents := context.WithTimeout(ctx, 5*time.Second)
	defer cancelEvents()
	eventsReq, _ := http.NewRequestWithContext(eventsCtx, http.MethodGet, httpSrv.URL+MCPEventsPath, nil)
	eventsReq.Header.Set(headerAuthorization, "Bearer secret-token")
	eventsResp, err := http.DefaultClient.Do(eventsReq)
	if err != nil {
		t.Fatalf("GET /mcp/events: %v", err)
	}
	defer eventsResp.Body.Close()

	go func() {
		time.Sleep(50 * time.Millisecond)
		srv.enqueuePushEvent(PushEvent{
			ID:           "ev-sse-1",
			Kind:         "notification",
			Conversation: "console:agy-alpha1",
			Title:        "SSE push test",
			Summary:      "live push event over SSE",
			Timestamp:    time.Now().UTC(),
		})
	}()

	sseScanner := bufio.NewScanner(eventsResp.Body)
	var gotEventType string
	var gotDataLine string
	for sseScanner.Scan() {
		line := strings.TrimSpace(sseScanner.Text())
		if strings.HasPrefix(line, "event: ") {
			gotEventType = strings.TrimPrefix(line, "event: ")
		} else if strings.HasPrefix(line, "data: ") && gotEventType == "push" {
			gotDataLine = strings.TrimPrefix(line, "data: ")
			break
		}
	}
	if gotEventType != "push" || !strings.Contains(gotDataLine, `"id":"ev-sse-1"`) {
		t.Fatalf("expected SSE event: push with id ev-sse-1, got event=%q data=%q", gotEventType, gotDataLine)
	}
}

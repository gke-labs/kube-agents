package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// startMultiTenantGatewayAndSessionFleet simulates a multi-tenant kube-agents A2A v2
// gateway, platform agent, and dynamic /session worker fleet on a shared NATS bus.
// Every console:<token> gets its own isolated session state and unique task IDs.
func startMultiTenantGatewayAndSessionFleet(t *testing.T, nc *nats.Conn) {
	t.Helper()

	var (
		mu             sync.Mutex
		sessionWorkers = make(map[string]string) // token -> worker addressee
		respawnCounts  = make(map[string]int)
		taskSeq        atomic.Int64
		envSeq         atomic.Int64
	)

	nextTaskID := func(prefix string) string {
		return fmt.Sprintf("%s-%d", prefix, taskSeq.Add(1))
	}
	nextEnvID := func(prefix string) string {
		return fmt.Sprintf("%s-%d", prefix, envSeq.Add(1))
	}

	sub, err := nc.Subscribe("chat.console.*.in", func(msg *nats.Msg) {
		token := strings.TrimPrefix(msg.Subject, subjectConsoleInPrefix)
		token = strings.TrimSuffix(token, subjectConsoleInSuffix)
		var frame ConsoleInFrame
		if json.Unmarshal(msg.Data, &frame) != nil {
			return
		}
		text := strings.TrimSpace(frame.Text)
		shortTok := token
		if len(shortTok) > 10 {
			shortTok = shortTok[len(shortTok)-10:]
		}
		shortTok = sanitizeMemberName(shortTok)

		switch text {
		case cmdSessionOn:
			mu.Lock()
			respawnCounts[token]++
			workerID := fmt.Sprintf("chat-worker-%s-%d", shortTok, respawnCounts[token])
			sessionWorkers[token] = workerID
			mu.Unlock()
			out, _ := json.Marshal(ConsoleOutFrame{
				MessageID: nextEnvID("msg-on"),
				Text:      fmt.Sprintf("session started — `%s`", workerID),
			})
			_ = nc.Publish(subjectConsoleOutPrefix+token+subjectConsoleOutSuffix, out)
			return

		case cmdSessionOff:
			mu.Lock()
			prev := sessionWorkers[token]
			delete(sessionWorkers, token)
			mu.Unlock()
			out, _ := json.Marshal(ConsoleOutFrame{
				MessageID: nextEnvID("msg-off"),
				Text:      fmt.Sprintf("session ended (`%s`)", firstNonEmptyStr(prev, "none")),
			})
			_ = nc.Publish(subjectConsoleOutPrefix+token+subjectConsoleOutSuffix, out)
			return

		case cmdStop:
			out, _ := json.Marshal(ConsoleOutFrame{
				MessageID: nextEnvID("msg-stop"),
				Text:      "🛑 stopped active task",
			})
			_ = nc.Publish(subjectConsoleOutPrefix+token+subjectConsoleOutSuffix, out)
			return
		}

		// Process each turn concurrently so multiple tenants run in parallel on the shared bus.
		go func(tok, prompt string) {
			mu.Lock()
			workerID, isSession := sessionWorkers[tok]
			mu.Unlock()

			conv := consoleConvPrefix + tok
			authRaw := mustJSON(map[string]any{
				"audience": map[string]any{"conversation": conv},
			})
			now := time.Now().UTC()
			ctxID := "ctx-" + tok

			if !isSession {
				tID := nextTaskID("t-plat-" + shortTok)
				inSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", testPlatformAddressee, tID)
				outSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", testPlatformAddressee, tID)

				publishTaskEnv(t, nc, inSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now,
					TaskID:     tID,
					ContextID:  ctxID,
					Kind:       lib.KindMessage,
					From:       lib.Party{Session: "gateway"},
					Authority:  authRaw,
					Payload:    mustJSON(lib.Message{Role: "user", MessageID: nextEnvID("m"), Parts: []lib.Part{{Kind: "text", Text: prompt}}}),
				})
				publishTaskEnv(t, nc, outSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(5 * time.Millisecond),
					TaskID:     tID,
					ContextID:  ctxID,
					Kind:       lib.KindStatusUpdate,
					From:       lib.Party{Session: testPlatformAddressee},
					Payload:    mustJSON(lib.StatusUpdate{TaskID: tID, ContextID: ctxID, Status: lib.TaskStatus{State: lib.StateWorking}}),
				})
				publishTaskEnv(t, nc, outSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(10 * time.Millisecond),
					TaskID:     tID,
					ContextID:  ctxID,
					Kind:       lib.KindArtifactUpdate,
					From:       lib.Party{Session: testPlatformAddressee},
					Payload: mustJSON(lib.ArtifactUpdate{
						TaskID:    tID,
						ContextID: ctxID,
						Append:    true,
						Artifact:  stepsArtifact(planStep("Executing platform check for "+tok, "Running isolated tenant check.")),
					}),
				})
				publishTaskEnv(t, nc, outSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(15 * time.Millisecond),
					TaskID:     tID,
					ContextID:  ctxID,
					Kind:       lib.KindArtifactUpdate,
					From:       lib.Party{Session: testPlatformAddressee},
					Payload: mustJSON(lib.ArtifactUpdate{
						TaskID:    tID,
						ContextID: ctxID,
						Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "PLATFORM_RESULT[" + tok + "]: " + prompt}}},
					}),
				})
				publishTaskEnv(t, nc, outSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(20 * time.Millisecond),
					TaskID:     tID,
					ContextID:  ctxID,
					Kind:       lib.KindStatusUpdate,
					From:       lib.Party{Session: testPlatformAddressee},
					Payload:    mustJSON(lib.StatusUpdate{TaskID: tID, ContextID: ctxID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
				})
				return
			}

			// Session worker turn (with optional platform delegation + wake synthesis).
			pTaskID := nextTaskID("t-sess-" + shortTok)
			inSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", workerID, pTaskID)
			outSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", workerID, pTaskID)

			publishTaskEnv(t, nc, inSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: nextEnvID("env"),
				TS:         now,
				TaskID:     pTaskID,
				ContextID:  ctxID,
				Kind:       lib.KindMessage,
				From:       lib.Party{Session: "gateway"},
				Authority:  authRaw,
				Payload:    mustJSON(lib.Message{Role: "user", MessageID: nextEnvID("m"), Parts: []lib.Part{{Kind: "text", Text: prompt}}}),
			})
			publishTaskEnv(t, nc, outSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: nextEnvID("env"),
				TS:         now.Add(5 * time.Millisecond),
				TaskID:     pTaskID,
				ContextID:  ctxID,
				Kind:       lib.KindStatusUpdate,
				From:       lib.Party{Session: workerID},
				Payload:    mustJSON(lib.StatusUpdate{TaskID: pTaskID, ContextID: ctxID, Status: lib.TaskStatus{State: lib.StateWorking}}),
			})

			if strings.Contains(strings.ToLower(prompt), "delegate") {
				publishTaskEnv(t, nc, outSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(10 * time.Millisecond),
					TaskID:     pTaskID,
					ContextID:  ctxID,
					Kind:       lib.KindArtifactUpdate,
					From:       lib.Party{Session: workerID},
					Payload: mustJSON(lib.ArtifactUpdate{
						TaskID:    pTaskID,
						ContextID: ctxID,
						Artifact:  lib.Artifact{Name: lib.ArtifactDelegate, Parts: []lib.Part{{Kind: "data", Data: mustJSON(map[string]string{"addressee": testPlatformAddressee, "text": "Delegated from " + workerID + ": " + prompt})}}},
					}),
				})
				publishTaskEnv(t, nc, outSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(15 * time.Millisecond),
					TaskID:     pTaskID,
					ContextID:  ctxID,
					Kind:       lib.KindStatusUpdate,
					From:       lib.Party{Session: workerID},
					Payload:    mustJSON(lib.StatusUpdate{TaskID: pTaskID, ContextID: ctxID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
				})

				cTaskID := nextTaskID("t-child-" + shortTok)
				cInSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", testPlatformAddressee, cTaskID)
				cOutSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", testPlatformAddressee, cTaskID)
				childAuth := mustJSON(map[string]any{
					"audience":      map[string]any{"conversation": conv},
					"delegatedFrom": map[string]any{"taskId": pTaskID, "contextId": ctxID, "agent": workerID},
				})
				publishTaskEnv(t, nc, cInSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(20 * time.Millisecond),
					TaskID:     cTaskID,
					ContextID:  ctxID,
					Kind:       lib.KindMessage,
					From:       lib.Party{Session: "gateway"},
					Authority:  childAuth,
					Payload:    mustJSON(lib.Message{Role: "user", MessageID: nextEnvID("m"), Parts: []lib.Part{{Kind: "text", Text: "Delegated from " + workerID + ": " + prompt}}}),
				})
				publishTaskEnv(t, nc, cOutSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(25 * time.Millisecond),
					TaskID:     cTaskID,
					ContextID:  ctxID,
					Kind:       lib.KindArtifactUpdate,
					From:       lib.Party{Session: testPlatformAddressee},
					Payload: mustJSON(lib.ArtifactUpdate{
						TaskID:    cTaskID,
						ContextID: ctxID,
						Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "CHILD_RESULT[" + tok + "]: " + prompt}}},
					}),
				})
				publishTaskEnv(t, nc, cOutSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(30 * time.Millisecond),
					TaskID:     cTaskID,
					ContextID:  ctxID,
					Kind:       lib.KindStatusUpdate,
					From:       lib.Party{Session: testPlatformAddressee},
					Payload:    mustJSON(lib.StatusUpdate{TaskID: cTaskID, ContextID: ctxID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
				})

				wTaskID := nextTaskID("t-wake-" + shortTok)
				wInSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", workerID, wTaskID)
				wOutSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", workerID, wTaskID)
				publishTaskEnv(t, nc, wInSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(35 * time.Millisecond),
					TaskID:     wTaskID,
					ContextID:  ctxID,
					Kind:       lib.KindMessage,
					From:       lib.Party{Session: "gateway"},
					Authority:  authRaw,
					Payload:    mustJSON(lib.Message{Role: "user", MessageID: nextEnvID("m"), Parts: []lib.Part{{Kind: "text", Text: "[delegated task " + cTaskID + " completed]\n\nSynthesize for " + tok}}}),
				})
				publishTaskEnv(t, nc, wOutSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(40 * time.Millisecond),
					TaskID:     wTaskID,
					ContextID:  ctxID,
					Kind:       lib.KindArtifactUpdate,
					From:       lib.Party{Session: workerID},
					Payload: mustJSON(lib.ArtifactUpdate{
						TaskID:    wTaskID,
						ContextID: ctxID,
						Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "SESSION_WAKE_RESULT[" + workerID + "]: " + prompt}}},
					}),
				})
				publishTaskEnv(t, nc, wOutSubj, lib.Envelope{
					Protocol:   lib.Protocol,
					EnvelopeID: nextEnvID("env"),
					TS:         now.Add(45 * time.Millisecond),
					TaskID:     wTaskID,
					ContextID:  ctxID,
					Kind:       lib.KindStatusUpdate,
					From:       lib.Party{Session: workerID},
					Payload:    mustJSON(lib.StatusUpdate{TaskID: wTaskID, ContextID: ctxID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
				})
				return
			}

			publishTaskEnv(t, nc, outSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: nextEnvID("env"),
				TS:         now.Add(15 * time.Millisecond),
				TaskID:     pTaskID,
				ContextID:  ctxID,
				Kind:       lib.KindArtifactUpdate,
				From:       lib.Party{Session: workerID},
				Payload: mustJSON(lib.ArtifactUpdate{
					TaskID:    pTaskID,
					ContextID: ctxID,
					Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "SESSION_DIRECT_RESULT[" + workerID + "]: " + prompt}}},
				}),
			})
			publishTaskEnv(t, nc, outSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: nextEnvID("env"),
				TS:         now.Add(20 * time.Millisecond),
				TaskID:     pTaskID,
				ContextID:  ctxID,
				Kind:       lib.KindStatusUpdate,
				From:       lib.Party{Session: workerID},
				Payload:    mustJSON(lib.StatusUpdate{TaskID: pTaskID, ContextID: ctxID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
			})
		}(token, text)
	})
	if err != nil {
		t.Fatalf("nc.Subscribe: %v", err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
}

func callJSONRPCToolWithMeta(t *testing.T, srv *Server, brainDir, convID, toolName string, args map[string]any) string {
	t.Helper()
	req := map[string]any{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "tools/call",
		"params": map[string]any{
			"name":      toolName,
			"arguments": args,
			"_meta": map[string]any{
				"antigravity.google/conversation_id": convID,
				"antigravity.google/artifacts_dir":   filepath.Join(brainDir, convID),
			},
		},
	}
	raw, _ := json.Marshal(req)
	respRaw := srv.HandleJSONRPC(context.Background(), raw)
	var rpcResp struct {
		Result MCPCallToolResult `json:"result"`
		Error  *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(respRaw, &rpcResp); err != nil {
		t.Fatalf("unmarshal JSON-RPC response for %s: %v\nraw: %s", toolName, err, string(respRaw))
	}
	if rpcResp.Error != nil {
		t.Fatalf("JSON-RPC error on %s: %s", toolName, rpcResp.Error.Message)
	}
	if rpcResp.Result.IsError {
		t.Fatalf("MCP tool %s returned IsError=true: %s", toolName, rpcResp.Result.Content[0].Text)
	}
	if len(rpcResp.Result.Content) == 0 {
		return ""
	}
	text := rpcResp.Result.Content[0].Text
	var customEnv struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		MD     string `json:"markdown"`
		Result string `json:"result"`
	}
	if json.Unmarshal([]byte(text), &customEnv) == nil && customEnv.Custom {
		return customEnv.Title + "\n" + customEnv.MD + "\n" + customEnv.Result
	}
	return text
}

// TestMultiUserSharedClusterMultitenancy simulates 3 engineers (Alice, Bob, Charlie), each
// running their own Antigravity instance + kube-agents-mcp server, connecting simultaneously to
// the same remote kube-agents deployment over a shared NATS JetStream bus.
func TestMultiUserSharedClusterMultitenancy(t *testing.T) {
	natsSrv, nc, _ := startEmbeddedJetStream(t)
	seedBusTopicsAndCard(t, nc)
	startMultiTenantGatewayAndSessionFleet(t, nc)

	type tenantFixture struct {
		name           string
		rootID         string
		subID          string
		subRole        string
		wantSubMember  string
		platSecret     string
		sessSecret     string
		delegateSecret string
		brainDir       string
		store          *ConfigStore
		srv            *Server
	}

	tenants := []*tenantFixture{
		{
			name:           "alice",
			rootID:         "aaaaaaaa-1111-4111-8111-111111111111",
			subID:          "aaaaaaaa-2222-4222-8222-222222222222",
			subRole:        "Session Alice SRE",
			wantSubMember:  "session-alice-sre",
			platSecret:     "ALICE_PRIVATE_PLATFORM_AUDIT_101",
			sessSecret:     "ALICE_PRIVATE_SESSION_CHECK_102",
			delegateSecret: "ALICE_PRIVATE_DELEGATE_UPGRADE_103",
		},
		{
			name:           "bob",
			rootID:         "bbbbbbbb-1111-4111-8111-111111111111",
			subID:          "bbbbbbbb-2222-4222-8222-222222222222",
			subRole:        "Session Bob Network",
			wantSubMember:  "session-bob-network",
			platSecret:     "BOB_PRIVATE_PLATFORM_AUDIT_201",
			sessSecret:     "BOB_PRIVATE_SESSION_CHECK_202",
			delegateSecret: "BOB_PRIVATE_DELEGATE_UPGRADE_203",
		},
		{
			name:           "charlie",
			rootID:         "cccccccc-1111-4111-8111-111111111111",
			subID:          "cccccccc-2222-4222-8222-222222222222",
			subRole:        "Session Charlie Security",
			wantSubMember:  "session-charlie-security",
			platSecret:     "CHARLIE_PRIVATE_PLATFORM_AUDIT_301",
			sessSecret:     "CHARLIE_PRIVATE_SESSION_CHECK_302",
			delegateSecret: "CHARLIE_PRIVATE_DELEGATE_UPGRADE_303",
		},
	}

	noopRunner := func(_ context.Context, _ string, _ ...string) ([]byte, error) {
		return nil, fmt.Errorf("agentapi disabled in test")
	}

	for _, tf := range tenants {
		tf.brainDir = t.TempDir()

		var err error
		tf.store, err = NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
		if err != nil {
			t.Fatalf("NewConfigStore(%s): %v", tf.name, err)
		}
		_, err = tf.store.EnsureConversationForSubagent(ConversationLineage{
			ConversationID:       tf.subID,
			ParentConversationID: tf.rootID,
			RootConversationID:   tf.rootID,
			SubagentType:         "kube-session",
			SubagentRole:         tf.subRole,
			SubagentState:        "SUBAGENT_STATE_RUNNING",
			IsSubagent:           true,
		}, "", false, "")
		if err != nil {
			t.Fatalf("EnsureConversationForSubagent(%s): %v", tf.name, err)
		}
		tf.srv, err = NewServer(ServerOptions{
			Store:  tf.store,
			Runner: noopRunner,
		})
		if err != nil {
			t.Fatalf("NewServer(%s): %v", tf.name, err)
		}
		t.Cleanup(tf.srv.Close)
	}

	// 1. All 3 tenants concurrently connect to the same NATS JetStream cluster, run platform turns,
	// spawn /session workers, run direct /session turns, and run delegated /session -> platform -> wake turns.
	var wg sync.WaitGroup
	for _, tf := range tenants {
		tf := tf
		wg.Add(1)
		go func() {
			defer wg.Done()

			// Coordinator connects to shared cluster.
			callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.rootID, toolConnectDeployment, map[string]any{
				"deployment":      testClusterName,
				"namespace":       testNamespace,
				"direct_nats_url": natsSrv.ClientURL(),
			})

			// Coordinator sends a platform message and Subagent runs /session concurrently!
			var innerWG sync.WaitGroup
			innerWG.Add(2)

			go func() {
				defer innerWG.Done()
				platOut := callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.rootID, toolSendMessage, map[string]any{
					"message":      tf.platSecret,
					"mode":         modePlatform,
					"stream_steps": false,
				})
				if !strings.Contains(platOut, tf.platSecret) {
					t.Errorf("%s platform send_message output missing own secret %q:\n%s", tf.name, tf.platSecret, platOut)
				}
				for _, other := range tenants {
					if other.name != tf.name && (strings.Contains(platOut, other.platSecret) || strings.Contains(platOut, other.sessSecret)) {
						t.Errorf("%s platform send_message leaked %s's secret:\n%s", tf.name, other.name, platOut)
					}
				}
			}()

			go func() {
				defer innerWG.Done()
				// Start /session pod.
				onOut := callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.subID, toolManageSession, map[string]any{
					"action": actionSessionOn,
				})
				if !strings.Contains(onOut, "session started") {
					t.Errorf("%s session_on failed:\n%s", tf.name, onOut)
				}

				// Direct /session turn.
				sessOut := callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.subID, toolSendMessage, map[string]any{
					"message":      tf.sessSecret,
					"mode":         modeSession,
					"stream_steps": false,
				})
				if !strings.Contains(sessOut, tf.sessSecret) {
					t.Errorf("%s session send_message missing %q:\n%s", tf.name, tf.sessSecret, sessOut)
				}

				// Delegated /session -> @platform -> wake turn.
				delOut := callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.subID, toolSendMessage, map[string]any{
					"message":      "delegate: " + tf.delegateSecret,
					"mode":         modeSession,
					"stream_steps": false,
				})
				if !strings.Contains(delOut, tf.delegateSecret) {
					t.Errorf("%s delegated session turn missing %q:\n%s", tf.name, tf.delegateSecret, delOut)
				}
				for _, other := range tenants {
					if other.name != tf.name && (strings.Contains(delOut, other.delegateSecret) || strings.Contains(delOut, other.platSecret)) {
						t.Errorf("%s delegated session turn leaked %s's secret:\n%s", tf.name, other.name, delOut)
					}
				}
			}()

			innerWG.Wait()
		}()
	}
	wg.Wait()

	// 2. Publish an asynchronous background task completion on Bob's console token while all 3 are idle,
	// and verify ONLY Bob queues a push event — Alice and Charlie must NOT receive Bob's private task event!
	bobRootConv := tenants[1].store.ConversationForAGY(tenants[1].rootID)
	bobAsyncTaskID := "t-bob-async-bg-999"
	bobAsyncSubjIn := fmt.Sprintf("a2a.tasks.%s.%s.in", testPlatformAddressee, bobAsyncTaskID)
	bobAsyncSubjOut := fmt.Sprintf("a2a.tasks.%s.%s.out", testPlatformAddressee, bobAsyncTaskID)
	now := time.Now().UTC()

	publishTaskEnv(t, nc, bobAsyncSubjIn, lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-bob-async-1",
		TS:         now,
		TaskID:     bobAsyncTaskID,
		ContextID:  "ctx-bob-async",
		Kind:       lib.KindMessage,
		From:       lib.Party{Session: "gateway"},
		Authority:  mustJSON(map[string]any{"audience": map[string]any{"conversation": "console:" + bobRootConv.Token}}),
		Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-bob-async", Parts: []lib.Part{{Kind: "text", Text: "BOB_ASYNC_BACKGROUND_SECRET_777"}}}),
	})
	publishTaskEnv(t, nc, bobAsyncSubjOut, lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-bob-async-2",
		TS:         now.Add(10 * time.Millisecond),
		TaskID:     bobAsyncTaskID,
		ContextID:  "ctx-bob-async",
		Kind:       lib.KindArtifactUpdate,
		From:       lib.Party{Session: testPlatformAddressee},
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID:    bobAsyncTaskID,
			ContextID: "ctx-bob-async",
			Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "BOB_ASYNC_BACKGROUND_RESULT_777"}}},
		}),
	})
	publishTaskEnv(t, nc, bobAsyncSubjOut, lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-bob-async-3",
		TS:         now.Add(20 * time.Millisecond),
		TaskID:     bobAsyncTaskID,
		ContextID:  "ctx-bob-async",
		Kind:       lib.KindStatusUpdate,
		From:       lib.Party{Session: testPlatformAddressee},
		Payload:    mustJSON(lib.StatusUpdate{TaskID: bobAsyncTaskID, ContextID: "ctx-bob-async", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	})
	_ = nc.Flush()

	deadline := time.Now().Add(3 * time.Second)
	for len(tenants[1].store.PeekPendingEvents()) == 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if len(tenants[1].store.PeekPendingEvents()) == 0 {
		t.Fatal("expected Bob to receive his own async background task completion push event")
	}
	if gotAliceEvents := tenants[0].store.PeekPendingEvents(); len(gotAliceEvents) != 0 {
		t.Fatalf("Alice must NOT receive Bob's private console push event, got: %+v", gotAliceEvents)
	}
	if gotCharlieEvents := tenants[2].store.PeekPendingEvents(); len(gotCharlieEvents) != 0 {
		t.Fatalf("Charlie must NOT receive Bob's private console push event, got: %+v", gotCharlieEvents)
	}

	// 3. Verify transcripts, task_control(list) alias filtering, and Swarm board isolation for all 3 tenants.
	for _, tf := range tenants {
		// Force a fresh swarm sync after all bus activity has settled.
		_ = callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.rootID, toolConnectionStatus, map[string]any{})

		// Check coordinator transcript contains ONLY this tenant's platform secret.
		rootTranscript := callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.rootID, toolManageSession, map[string]any{
			"action": actionTranscript,
		})
		if !strings.Contains(rootTranscript, tf.platSecret) {
			t.Errorf("%s coordinator transcript missing %q:\n%s", tf.name, tf.platSecret, rootTranscript)
		}

		// Check subagent transcript contains ONLY this tenant's session & delegate secrets.
		subTranscript := callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.subID, toolManageSession, map[string]any{
			"action": actionTranscript,
		})
		for _, want := range []string{tf.sessSecret, tf.delegateSecret} {
			if !strings.Contains(subTranscript, want) {
				t.Errorf("%s subagent transcript missing %q:\n%s", tf.name, want, subTranscript)
			}
		}

		// Check task_control(action: "list", conversation: <subMemberAlias>) resolves alias -> token and isolates tasks.
		taskListOut := callJSONRPCToolWithMeta(t, tf.srv, tf.brainDir, tf.rootID, toolTaskControl, map[string]any{
			"action":       actionList,
			"conversation": tf.wantSubMember,
		})
		if !strings.Contains(taskListOut, tf.sessSecret) {
			t.Errorf("%s task_control(list, conversation=%s) missing %q:\n%s", tf.name, tf.wantSubMember, tf.sessSecret, taskListOut)
		}

		for _, other := range tenants {
			if other.name == tf.name {
				continue
			}
			for _, forbidden := range []string{other.platSecret, other.sessSecret, other.delegateSecret} {
				if strings.Contains(rootTranscript, forbidden) {
					t.Errorf("%s coordinator transcript leaked %s's %q:\n%s", tf.name, other.name, forbidden, rootTranscript)
				}
				if strings.Contains(subTranscript, forbidden) {
					t.Errorf("%s subagent transcript leaked %s's %q:\n%s", tf.name, other.name, forbidden, subTranscript)
				}
				if strings.Contains(taskListOut, forbidden) {
					t.Errorf("%s task_control(list) leaked %s's %q:\n%s", tf.name, other.name, forbidden, taskListOut)
				}
			}
		}

		// Check Swarm projection isolation via ComputeRemoteSwarmState.
		swarmResp := tf.srv.ComputeRemoteSwarmState(context.Background(), RemoteSwarmRequest{
			RootConversationID: tf.rootID,
			Subagents: map[string]ConversationLineage{
				tf.subID: {
					ConversationID:       tf.subID,
					ParentConversationID: tf.rootID,
					RootConversationID:   tf.rootID,
					SubagentType:         "kube-session",
					SubagentRole:         tf.subRole,
					SubagentState:        "SUBAGENT_STATE_RUNNING",
					IsSubagent:           true,
				},
			},
		})
		roster := SwarmRosterFile{Title: swarmResp.Snapshot.Title, Members: swarmResp.Snapshot.Members}
		if roster.Members["coordinator"].ConversationID != tf.rootID {
			t.Errorf("%s coordinator conversation_id = %q, want %q", tf.name, roster.Members["coordinator"].ConversationID, tf.rootID)
		}
		subMem, ok := roster.Members[tf.wantSubMember]
		if !ok || subMem.ConversationID != tf.subID {
			t.Errorf("%s missing or misbound %s in members: %+v", tf.name, tf.wantSubMember, roster.Members)
		}

		rawBoard, _ := json.Marshal(swarmResp.Snapshot.NewMessages)
		boardStr := string(rawBoard)
		for _, wantOnBoard := range []string{tf.platSecret, tf.sessSecret, tf.delegateSecret} {
			if !strings.Contains(boardStr, wantOnBoard) {
				t.Errorf("%s board missing own activity %q:\n%s", tf.name, wantOnBoard, boardStr)
			}
		}
		for _, other := range tenants {
			if other.name == tf.name {
				continue
			}
			if _, leakedMem := roster.Members[other.wantSubMember]; leakedMem {
				t.Errorf("%s members leaked %s's subagent %q: %+v", tf.name, other.name, other.wantSubMember, roster.Members)
			}
			for mName := range roster.Members {
				if strings.Contains(mName, other.name) {
					t.Errorf("%s members leaked %s's worker %q: %+v", tf.name, other.name, mName, roster.Members)
				}
			}
			for _, forbidden := range []string{other.platSecret, other.sessSecret, other.delegateSecret, "BOB_ASYNC_BACKGROUND"} {
				if other.name == "bob" || forbidden != "BOB_ASYNC_BACKGROUND" {
					if tf.name != "bob" && strings.Contains(boardStr, forbidden) {
						t.Errorf("%s board leaked %s's %q:\n%s", tf.name, other.name, forbidden, boardStr)
					}
				}
			}
		}
	}
}

// TestSameInstanceMultiConversationAndSubagentMultitenancy verifies that when a single user
// has multiple concurrent Antigravity conversations/tabs AND multiple /session subagents sharing
// the same MCP Server and ConfigStore:
//  1. Secondary/background Antigravity conversations get their own isolated ConversationEntry and
//     console token without hijacking the primary coordinator's ActiveConversation,
//     Push.AGYConversationID, or /session subagents.
//  2. The primary coordinator's Swarm board contains ONLY its own and its subagents' turns.
//  3. Per-conversation debug_on/debug_off toggles and transcripts stay strictly isolated.
func TestSameInstanceMultiConversationAndSubagentMultitenancy(t *testing.T) {
	natsSrv, nc, _ := startEmbeddedJetStream(t)
	seedBusTopicsAndCard(t, nc)
	startMultiTenantGatewayAndSessionFleet(t, nc)

	brainDir := t.TempDir()
	coordA := "11111111-aaaa-4aaa-8aaa-111111111111"
	subPoet := "22222222-bbbb-4bbb-8bbb-222222222222"
	subExplorer := "33333333-cccc-4ccc-8ccc-333333333333"
	windowB := "44444444-dddd-4ddd-8ddd-444444444444"
	windowC := "55555555-eeee-4eee-8eee-555555555555"

	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}
	subPoetLin := ConversationLineage{
		ConversationID:       subPoet,
		ParentConversationID: coordA,
		RootConversationID:   coordA,
		SubagentType:         "kube-session",
		SubagentRole:         "Session Poet",
		SubagentState:        "SUBAGENT_STATE_RUNNING",
		IsSubagent:           true,
	}
	subExplorerLin := ConversationLineage{
		ConversationID:       subExplorer,
		ParentConversationID: coordA,
		RootConversationID:   coordA,
		SubagentType:         "kube-session",
		SubagentRole:         "Session Cluster Explorer",
		SubagentState:        "SUBAGENT_STATE_RUNNING",
		IsSubagent:           true,
	}
	_, _ = store.EnsureConversationForSubagent(subPoetLin, "", false, "")
	_, _ = store.EnsureConversationForSubagent(subExplorerLin, "", false, "")

	srv, err := NewServer(ServerOptions{
		Store: store,
		Runner: func(_ context.Context, _ string, _ ...string) ([]byte, error) {
			return nil, fmt.Errorf("agentapi disabled in test")
		},
	})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	t.Cleanup(srv.Close)

	// 1. Primary Coordinator A connects to the deployment.
	callJSONRPCToolWithMeta(t, srv, brainDir, coordA, toolConnectDeployment, map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": natsSrv.ClientURL(),
	})

	// 2. Run concurrent turns across Coordinator A, Subagent Poet, Subagent Explorer, Window B, and Window C!
	var wg sync.WaitGroup
	wg.Add(5)

	go func() {
		defer wg.Done()
		out := callJSONRPCToolWithMeta(t, srv, brainDir, coordA, toolSendMessage, map[string]any{
			"message":      "COORDA_MAIN_FLEET_CHECK",
			"mode":         modePlatform,
			"stream_steps": false,
		})
		if !strings.Contains(out, "COORDA_MAIN_FLEET_CHECK") {
			t.Errorf("coordA send_message missing expected text:\n%s", out)
		}
	}()

	go func() {
		defer wg.Done()
		callJSONRPCToolWithMeta(t, srv, brainDir, subPoet, toolManageSession, map[string]any{
			"action": actionSessionOn,
		})
		out := callJSONRPCToolWithMeta(t, srv, brainDir, subPoet, toolSendMessage, map[string]any{
			"message":      "SUBPOET_HAIKU_ABOUT_ETCD",
			"mode":         modeSession,
			"stream_steps": false,
		})
		if !strings.Contains(out, "SUBPOET_HAIKU_ABOUT_ETCD") {
			t.Errorf("subPoet send_message missing expected text:\n%s", out)
		}
	}()

	go func() {
		defer wg.Done()
		callJSONRPCToolWithMeta(t, srv, brainDir, subExplorer, toolManageSession, map[string]any{
			"action": actionSessionOn,
		})
		out := callJSONRPCToolWithMeta(t, srv, brainDir, subExplorer, toolSendMessage, map[string]any{
			"message":      "delegate: SUBEXPLORER_DELEGATE_POD_CHECK",
			"mode":         modeSession,
			"stream_steps": false,
		})
		if !strings.Contains(out, "SUBEXPLORER_DELEGATE_POD_CHECK") {
			t.Errorf("subExplorer send_message missing expected text:\n%s", out)
		}
	}()

	go func() {
		defer wg.Done()
		// Secondary Window B toggles debug_on for itself and sends a platform message without specifying conversation.
		callJSONRPCToolWithMeta(t, srv, brainDir, windowB, toolManageSession, map[string]any{
			"action": actionDebugOn,
		})
		out := callJSONRPCToolWithMeta(t, srv, brainDir, windowB, toolSendMessage, map[string]any{
			"message":      "WINDOWB_ADHOC_QUERY_888",
			"mode":         modePlatform,
			"stream_steps": false,
		})
		if !strings.Contains(out, "WINDOWB_ADHOC_QUERY_888") {
			t.Errorf("windowB send_message missing expected text:\n%s", out)
		}
	}()

	go func() {
		defer wg.Done()
		// Tertiary Window C sends a platform message, checks status, and lists sessions.
		out := callJSONRPCToolWithMeta(t, srv, brainDir, windowC, toolSendMessage, map[string]any{
			"message":      "WINDOWC_BACKGROUND_SOAK_999",
			"mode":         modePlatform,
			"stream_steps": false,
		})
		if !strings.Contains(out, "WINDOWC_BACKGROUND_SOAK_999") {
			t.Errorf("windowC send_message missing expected text:\n%s", out)
		}
		_ = callJSONRPCToolWithMeta(t, srv, brainDir, windowC, toolConnectionStatus, map[string]any{})
		_ = callJSONRPCToolWithMeta(t, srv, brainDir, windowC, toolManageSession, map[string]any{"action": actionList})
	}()

	wg.Wait()

	// 3. Verify all 5 callers have distinct console tokens in ConfigStore, and coordA is still the active coordinator.
	snap := store.Snapshot()
	if snap.Push.AGYConversationID != coordA {
		t.Fatalf("Push.AGYConversationID = %q, want primary coordinator %q", snap.Push.AGYConversationID, coordA)
	}
	activeEntry := snap.Conversations[snap.ActiveConversation]
	if activeEntry.AGYConversationID != coordA {
		t.Fatalf("ActiveConversation %q bound to %q, want primary coordinator %q", snap.ActiveConversation, activeEntry.AGYConversationID, coordA)
	}

	tokensSeen := make(map[string]string)
	for _, cid := range []string{coordA, subPoet, subExplorer, windowB, windowC} {
		conv, ok := store.LookupConversation(cid)
		if !ok {
			t.Fatalf("expected conversation bound for %s in ConfigStore", cid)
		}
		if prevCID, dup := tokensSeen[conv.Token]; dup {
			t.Fatalf("conversations %s and %s share console token %q!", prevCID, cid, conv.Token)
		}
		tokensSeen[conv.Token] = cid
	}

	// Verify Window B's debug_on did NOT leak to coordA, subPoet, subExplorer, or windowC.
	winBConv, _ := store.LookupConversation(windowB)
	if !store.DebugModeFor(winBConv.Alias) {
		t.Errorf("expected debug mode enabled for windowB (%s)", winBConv.Alias)
	}
	for _, cid := range []string{coordA, subPoet, subExplorer, windowC} {
		c, _ := store.LookupConversation(cid)
		if store.DebugModeFor(c.Alias) {
			t.Errorf("debug_on from windowB leaked to %s (%s)", cid, c.Alias)
		}
	}

	// 4. Verify coordA's Swarm state has coordA, session-poet, and session-cluster-explorer,
	// and does NOT contain windowB or windowC tasks!
	swarmResp := srv.ComputeRemoteSwarmState(context.Background(), RemoteSwarmRequest{
		RootConversationID: coordA,
		Subagents: map[string]ConversationLineage{
			subPoet:     subPoetLin,
			subExplorer: subExplorerLin,
		},
	})
	roster := SwarmRosterFile{Title: swarmResp.Snapshot.Title, Members: swarmResp.Snapshot.Members}
	if roster.Members["coordinator"].ConversationID != coordA {
		t.Errorf("coordinator conversation_id = %q, want %q", roster.Members["coordinator"].ConversationID, coordA)
	}
	if roster.Members["session-poet"].ConversationID != subPoet {
		t.Errorf("session-poet conversation_id = %q, want %q", roster.Members["session-poet"].ConversationID, subPoet)
	}
	if roster.Members["session-cluster-explorer"].ConversationID != subExplorer {
		t.Errorf("session-cluster-explorer conversation_id = %q, want %q", roster.Members["session-cluster-explorer"].ConversationID, subExplorer)
	}

	rawBoard, _ := json.Marshal(swarmResp.Snapshot.NewMessages)
	boardStr := string(rawBoard)
	for _, want := range []string{
		"COORDA_MAIN_FLEET_CHECK",
		"SUBPOET_HAIKU_ABOUT_ETCD",
		"SUBEXPLORER_DELEGATE_POD_CHECK",
	} {
		if !strings.Contains(boardStr, want) {
			t.Errorf("coordA board missing %q:\n%s", want, boardStr)
		}
	}
	for _, unwanted := range []string{
		"WINDOWB_ADHOC_QUERY_888",
		"WINDOWC_BACKGROUND_SOAK_999",
	} {
		if strings.Contains(boardStr, unwanted) {
			t.Errorf("coordA board was polluted by secondary window turn %q:\n%s", unwanted, boardStr)
		}
	}
}

// TestPerConversationPushPiggybackAndAgentAPIRoutingIsolation verifies that push events
// queued for specific console tokens are routed only to their bound Antigravity conversation
// (via piggyback drain or poll_events), while foreign console tokens are rejected at Enqueue.
func TestPerConversationPushPiggybackAndAgentAPIRoutingIsolation(t *testing.T) {
	tmpDir := t.TempDir()
	store, err := NewConfigStore(filepath.Join(tmpDir, "mcp-config.json"))
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}

	coordID := "11111111-1111-4111-8111-111111111111"
	subPoetID := "22222222-2222-4222-8222-222222222222"
	subExplorerID := "33333333-3333-4333-8333-333333333333"

	coordConv := store.ConversationForAGYWithLineage(ConversationLineage{
		ConversationID:     coordID,
		RootConversationID: coordID,
	})
	poetConv := store.ConversationForAGYWithLineage(ConversationLineage{
		ConversationID:       subPoetID,
		ParentConversationID: coordID,
		RootConversationID:   coordID,
		SubagentType:         "kube-session",
		SubagentRole:         "Session Poet",
		IsSubagent:           true,
	})
	explorerConv := store.ConversationForAGYWithLineage(ConversationLineage{
		ConversationID:       subExplorerID,
		ParentConversationID: coordID,
		RootConversationID:   coordID,
		SubagentType:         "kube-session",
		SubagentRole:         "Session Cluster Explorer",
		IsSubagent:           true,
	})

	disabled := false
	_, _ = store.UpdatePushConfig(coordID, &disabled, "")

	srv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	t.Cleanup(srv.Close)

	// 1. Enqueue events for poet, explorer, coordinator, cluster-wide (""), and a foreign user token.
	srv.enqueuePushEvent(PushEvent{
		ID:           "ev-foreign",
		Kind:         eventKindTaskCompleted,
		Title:        "Foreign User Task",
		Summary:      "FOREIGN_TENANT_SECRET_999",
		Conversation: "console:agy-foreign999",
		Timestamp:    time.Now().UTC(),
	})
	srv.enqueuePushEvent(PushEvent{
		ID:           "ev-poet",
		Kind:         eventKindTaskCompleted,
		Title:        "Poet Task Completed",
		Summary:      "POET_EXCLUSIVE_EVENT_111",
		Conversation: "console:" + poetConv.Token,
		Timestamp:    time.Now().UTC(),
	})
	srv.enqueuePushEvent(PushEvent{
		ID:           "ev-explorer",
		Kind:         eventKindInputRequired,
		Title:        "Explorer Input Needed",
		Summary:      "EXPLORER_EXCLUSIVE_EVENT_222",
		Conversation: "console:" + explorerConv.Token,
		Timestamp:    time.Now().UTC(),
	})
	srv.enqueuePushEvent(PushEvent{
		ID:           "ev-coord",
		Kind:         eventKindNoticePR,
		Title:        "Coordinator PR Opened",
		Summary:      "COORD_EXCLUSIVE_EVENT_333",
		Conversation: "console:" + coordConv.Token,
		Timestamp:    time.Now().UTC(),
	})
	srv.enqueuePushEvent(PushEvent{
		ID:           "ev-cluster-broadcast",
		Kind:         eventKindProactiveNotify,
		Title:        "Cluster Broadcast",
		Summary:      "CLUSTER_WIDE_ALERT_444",
		Conversation: "",
		Timestamp:    time.Now().UTC(),
	})

	// Foreign event must never be stored.
	allQueued := store.PeekPendingEvents()
	if len(allQueued) != 4 {
		t.Fatalf("expected 4 queued events (foreign dropped), got %d: %+v", len(allQueued), allQueued)
	}
	for _, ev := range allQueued {
		if strings.Contains(ev.Summary, "FOREIGN_TENANT_SECRET_999") {
			t.Fatalf("foreign tenant event was queued: %+v", ev)
		}
	}

	// 2. Subagent Poet piggyback drains ONLY Poet's event!
	poetPiggyback := srv.drainAndFormatPiggybackFor(subPoetID)
	if !strings.Contains(poetPiggyback, "POET_EXCLUSIVE_EVENT_111") {
		t.Fatalf("expected Poet piggyback to contain POET_EXCLUSIVE_EVENT_111, got:\n%s", poetPiggyback)
	}
	for _, forbidden := range []string{"EXPLORER_EXCLUSIVE_EVENT_222", "COORD_EXCLUSIVE_EVENT_333", "CLUSTER_WIDE_ALERT_444"} {
		if strings.Contains(poetPiggyback, forbidden) {
			t.Fatalf("Poet piggyback drained %q meant for another conversation:\n%s", forbidden, poetPiggyback)
		}
	}

	// 3. Subagent Explorer piggyback drains ONLY Explorer's event!
	explorerPiggyback := srv.drainAndFormatPiggybackFor(subExplorerID)
	if !strings.Contains(explorerPiggyback, "EXPLORER_EXCLUSIVE_EVENT_222") {
		t.Fatalf("expected Explorer piggyback to inject EXPLORER_EXCLUSIVE_EVENT_222, got:\n%s", explorerPiggyback)
	}
	for _, forbidden := range []string{"COORD_EXCLUSIVE_EVENT_333", "CLUSTER_WIDE_ALERT_444"} {
		if strings.Contains(explorerPiggyback, forbidden) {
			t.Fatalf("Explorer piggyback drained %q meant for coordinator:\n%s", forbidden, explorerPiggyback)
		}
	}

	// 4. Coordinator drains both its own console event AND the cluster-wide broadcast!
	coordPiggyback := srv.drainAndFormatPiggybackFor(coordID)
	for _, want := range []string{"COORD_EXCLUSIVE_EVENT_333", "CLUSTER_WIDE_ALERT_444"} {
		if !strings.Contains(coordPiggyback, want) {
			t.Fatalf("Coordinator piggyback missing %q:\n%s", want, coordPiggyback)
		}
	}
	if rem := store.PeekPendingEvents(); len(rem) != 0 {
		t.Fatalf("expected 0 remaining events after all tenants drained, got %+v", rem)
	}
}

func TestConcurrentEnsureConnectedSingleflightAndReconnectCatchUp(t *testing.T) {
	natsSrv, nc, _ := startEmbeddedJetStream(t)
	seedBusTopicsAndCard(t, nc)
	tmpDir := t.TempDir()
	brainDir := filepath.Join(tmpDir, "brain")
	coordID := "11111111-4444-4444-8444-111111111111"

	store, err := NewConfigStore(filepath.Join(tmpDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	_ = store.UpsertDeployments([]DeploymentProfile{{
		Name:              "shared-host",
		Namespace:         "kubeagents-system",
		PlatformAgentName: "platform-agent",
		DirectNATSURL:     natsSrv.ClientURL(),
		Ready:             true,
	}})
	_, _ = store.SetActiveDeployment("shared-host")
	disabled := false
	_, _ = store.UpdatePushConfig(coordID, &disabled, filepath.Join(tmpDir, "no-agentapi"))

	srv, err := NewServer(ServerOptions{
		Store: store,
		Runner: func(_ context.Context, name string, args ...string) ([]byte, error) {
			return nil, fmt.Errorf("disabled runner: %s %v", name, args)
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)

	// 1. Fire 8 concurrent connection_status calls on a cold server (srv.session == nil)
	// and verify all 8 succeed and share the exact same singleflight *BusSession instance.
	const numCallers = 8
	sessions := make([]*BusSession, numCallers)
	var wg sync.WaitGroup
	for i := 0; i < numCallers; i++ {
		i := i
		wg.Add(1)
		go func() {
			defer wg.Done()
			out := callJSONRPCToolWithMeta(t, srv, brainDir, coordID, toolConnectionStatus, map[string]any{})
			if !strings.Contains(out, "shared-host") {
				t.Errorf("caller %d missing shared-host in status:\n%s", i, out)
			}
			srv.mu.Lock()
			sessions[i] = srv.session
			srv.mu.Unlock()
		}()
	}
	wg.Wait()

	firstSess := sessions[0]
	if firstSess == nil {
		t.Fatal("expected non-nil BusSession after concurrent connection_status calls")
	}
	for i, s := range sessions {
		if s != firstSess {
			t.Fatalf("caller %d got different *BusSession (%p vs %p); expected singleflight ensureConnected", i, s, firstSess)
		}
	}

	// 2. Simulate a NATS reconnect gap: temporarily unsubscribe the live core NATS subscription,
	// publish a task completion to JetStream TASKS, and invoke catchUpAfterReconnect().
	// Verify the missed task is replayed from streamBootSeq+1 with live=true, folded into tasks,
	// and enqueues a push event without duplicating on a second catchUpAfterReconnect().
	coordConv := store.ConversationForAGY(coordID)
	for _, sub := range firstSess.subs {
		_ = sub.Unsubscribe()
	}
	firstSess.subs = nil

	gapTaskID := "t-reconnect-gap-42"
	gapSubjIn := fmt.Sprintf("a2a.tasks.%s.%s.in", testPlatformAddressee, gapTaskID)
	gapSubjOut := fmt.Sprintf("a2a.tasks.%s.%s.out", testPlatformAddressee, gapTaskID)
	now := time.Now().UTC()

	publishTaskEnv(t, nc, gapSubjIn, lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-gap-1",
		TS:         now,
		TaskID:     gapTaskID,
		ContextID:  "ctx-gap",
		Kind:       lib.KindMessage,
		From:       lib.Party{Session: "gateway"},
		Authority:  mustJSON(map[string]any{"audience": map[string]any{"conversation": "console:" + coordConv.Token}}),
		Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-gap-1", Parts: []lib.Part{{Kind: "text", Text: "GAP_PROMPT_42"}}}),
	})
	publishTaskEnv(t, nc, gapSubjOut, lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-gap-2",
		TS:         now.Add(10 * time.Millisecond),
		TaskID:     gapTaskID,
		ContextID:  "ctx-gap",
		Kind:       lib.KindArtifactUpdate,
		From:       lib.Party{Session: testPlatformAddressee},
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID:    gapTaskID,
			ContextID: "ctx-gap",
			Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "GAP_RESULT_42"}}},
		}),
	})
	publishTaskEnv(t, nc, gapSubjOut, lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-gap-3",
		TS:         now.Add(20 * time.Millisecond),
		TaskID:     gapTaskID,
		ContextID:  "ctx-gap",
		Kind:       lib.KindStatusUpdate,
		From:       lib.Party{Session: testPlatformAddressee},
		Payload: mustJSON(lib.StatusUpdate{
			TaskID:    gapTaskID,
			ContextID: "ctx-gap",
			Final:     true,
			Status: lib.TaskStatus{
				State:   lib.StateCompleted,
				Message: &lib.Message{Role: "agent", MessageID: "m-gap-done", Parts: []lib.Part{{Kind: "text", Text: "GAP_RESULT_42"}}},
			},
		}),
	})
	_ = nc.Flush()

	firstSess.catchUpAfterReconnect()
	firstSess.catchUpAfterReconnect() // idempotent second replay

	rec := firstSess.GetTask(gapTaskID)
	if rec == nil || rec.State != lib.StateCompleted || !strings.Contains(rec.ResultText, "GAP_RESULT_42") {
		t.Fatalf("expected catchUpAfterReconnect to fold %s to completed with GAP_RESULT_42, got rec=%+v", gapTaskID, rec)
	}
	events := store.PeekPendingEventsForAGY(coordID)
	if len(events) != 1 || !strings.Contains(events[0].Summary, "GAP_RESULT_42") {
		t.Fatalf("expected 1 push event from catchUpAfterReconnect, got %+v", events)
	}
}

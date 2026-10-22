package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

// TestMain isolates the package tests from the Antigravity host environment: when the
// suite itself runs inside an Antigravity conversation, the host exports
// ANTIGRAVITY_CONVERSATION_ID, which would otherwise leak into every conversation
// resolution fallback and make the auto-binding assertions depend on the caller.
func TestMain(m *testing.M) {
	os.Unsetenv(envAGYConversationID)
	os.Exit(m.Run())
}

// Shared fixtures for the package tests: an embedded JetStream server, a simulated
// gateway and worker pair, and envelope/step builders.

// stepsArtifact is the "steps" artifact an executor publishes: one kube-agents.step/v1 data
// part per step record.
func stepsArtifact(steps ...stepmsg.Step) lib.Artifact {
	parts := make([]lib.Part, 0, len(steps))
	for _, st := range steps {
		parts = append(parts, st.Part())
	}
	return lib.Artifact{Name: stepmsg.ArtifactName, Parts: parts}
}

func planStep(title, thought string, callIDs ...string) stepmsg.Step {
	return stepmsg.Step{Kind: stepmsg.KindPlan, Title: title, Thought: thought, CallIDs: callIDs}
}

func stepRecords(steps ...stepmsg.Step) []StepRecord {
	out := make([]StepRecord, 0, len(steps))
	base := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	for i, st := range steps {
		out = append(out, StepRecord{Step: st, At: base.Add(time.Duration(i) * time.Second)})
	}
	return out
}

const (
	testHostLoopback      = "127.0.0.1"
	testReadyTimeout      = 15 * time.Second
	testClusterName       = "kube-agents-host"
	testKubeContext       = "gke_example-project_us-east4_kube-agents-host"
	testUnreachableCtx    = "gke_unreachable_context"
	testNamespace         = "kubeagents-system"
	testPAName            = "platform-agent"
	testProjectID         = "example-project"
	testLocation          = "us-east4"
	testGitHubRepo        = "example-org/example-infra"
	testConsoleAuthVal    = "console-pass-val"
	testGatewayAuthVal    = "gateway-pass-val"
	testAGYConvID         = "00000000-0000-0000-0000-000000000001"
	testPRNumber          = 42
	testPRURL             = "https://github.com/example-org/example-infra/pull/42"
	testParentTaskID      = "t-parent-101"
	testChildTaskID       = "t-child-102"
	testWakeTaskID        = "t-wake-103"
	testBgTaskID          = "t-bg-999"
	testContextID         = "c-ctx-100"
	testSessionAddressee  = "s-worker01"
	testPlatformAddressee = "platform"
)

func startEmbeddedJetStream(t *testing.T) (*natsserver.Server, *nats.Conn, jetstream.JetStream) {
	t.Helper()
	opts := &natsserver.Options{
		Host:      testHostLoopback,
		Port:      -1,
		NoLog:     true,
		NoSigs:    true,
		JetStream: true,
		StoreDir:  t.TempDir(),
	}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("natsserver.NewServer: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(testReadyTimeout) {
		t.Fatal("embedded nats-server did not become ready")
	}
	t.Cleanup(srv.Shutdown)

	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("nats.Connect: %v", err)
	}
	t.Cleanup(nc.Close)

	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream.New: %v", err)
	}

	ctx := context.Background()
	streamDefs := []jetstream.StreamConfig{
		{Name: streamTasks, Subjects: []string{"a2a.tasks.*.*.in", "a2a.tasks.*.*.out"}},
		{Name: streamDirectory, Subjects: []string{"a2a.agents.*", "a2a.agents.*.card"}},
		{Name: streamTopicsState, Subjects: []string{"a2a.topics.state.>"}},
		{Name: streamTopicsJournal, Subjects: []string{"a2a.topics.journal.>"}},
	}
	for _, sc := range streamDefs {
		if _, err := js.CreateStream(ctx, sc); err != nil {
			t.Fatalf("CreateStream(%s): %v", sc.Name, err)
		}
	}
	return srv, nc, js
}

func seedBusTopicsAndCard(t *testing.T, nc *nats.Conn) {
	t.Helper()
	cardEnv := lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "card-1",
		TS:         time.Now().UTC(),
		Kind:       lib.KindAgentCard,
		From:       lib.Party{Session: testPlatformAddressee},
		Payload:    mustJSON(map[string]any{"name": testPlatformAddressee, "version": "0.4.0", "description": "GKE Fleet Platform Agent"}),
	}
	publishTaskEnv(t, nc, "a2a.agents.platform.card", cardEnv)

	topicArt, err := lib.NewTopicArtifact("upgrade-readiness", "Cluster is ready for 1.32", map[string]any{"ready": true, "targetVersion": "1.32.1"})
	if err != nil {
		t.Fatal(err)
	}
	topicEnv := lib.Envelope{
		Protocol:      lib.Protocol,
		EnvelopeID:    "topic-1",
		TS:            time.Now().UTC(),
		CorrelationID: "corr-1",
		Kind:          lib.KindTopicUpdate,
		From:          lib.Party{Session: testPlatformAddressee},
		Payload:       mustJSON(topicArt),
	}
	publishTaskEnv(t, nc, "a2a.topics.state.agent.platform.upgrade-readiness", topicEnv)
	_ = nc.Flush()
}

func startSimulatedGatewayAndWorkers(t *testing.T, nc *nats.Conn) {
	t.Helper()
	sub, err := nc.Subscribe("chat.console.*.in", func(msg *nats.Msg) {
		token := strings.TrimPrefix(msg.Subject, subjectConsoleInPrefix)
		token = strings.TrimSuffix(token, subjectConsoleInSuffix)
		var frame ConsoleInFrame
		if json.Unmarshal(msg.Data, &frame) != nil {
			return
		}
		if frame.Text == cmdSessionOn {
			out, _ := json.Marshal(ConsoleOutFrame{
				MessageID: "out-1",
				Text:      "session started — `" + testSessionAddressee + "`",
			})
			_ = nc.Publish(subjectConsoleOutPrefix+token+subjectConsoleOutSuffix, out)
			return
		}

		conv := consoleConvPrefix + token
		authRaw := mustJSON(map[string]any{
			"audience": map[string]any{"conversation": conv},
		})
		now := time.Now().UTC()

		// 1. Parent session worker task.
		inSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", testSessionAddressee, testParentTaskID)
		outSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", testSessionAddressee, testParentTaskID)

		publishTaskEnv(t, nc, inSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-1",
			TS:         now,
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindMessage,
			From:       lib.Party{Session: "gateway"},
			Authority:  authRaw,
			Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-p-1", Parts: []lib.Part{{Kind: "text", Text: frame.Text}}}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-2",
			TS:         now.Add(10 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: testParentTaskID, ContextID: testContextID, Status: lib.TaskStatus{State: lib.StateWorking}}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-3",
			TS:         now.Add(20 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testParentTaskID,
				ContextID: testContextID,
				Append:    true,
				Artifact:  lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Analyzing cluster upgrade readiness and delegating to platform agent."}}},
			}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-4",
			TS:         now.Add(30 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testParentTaskID,
				ContextID: testContextID,
				Artifact:  lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"run_kubectl","input":"get nodes","call_id":"c1","status":"ok","duration_ms":85}`)}}},
			}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-5",
			TS:         now.Add(40 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testParentTaskID,
				ContextID: testContextID,
				Artifact:  lib.Artifact{Name: lib.ArtifactDelegate, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"addressee":"platform","text":"verify upgrade readiness and open PR"}`)}}},
			}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-6",
			TS:         now.Add(50 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: testParentTaskID, ContextID: testContextID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
		})

		// 2. Delegated child task on platform. As the real gateway does, the child's authority
		// names the delegating session turn in `via` (addressed to the platform agent).
		childAuth := mustJSON(map[string]any{
			"audience": map[string]any{"conversation": conv},
			"via":      map[string]any{"taskId": testParentTaskID, "session": testSessionAddressee},
		})
		cInSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", testPlatformAddressee, testChildTaskID)
		cOutSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", testPlatformAddressee, testChildTaskID)
		publishTaskEnv(t, nc, cInSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-c-1",
			TS:         now.Add(60 * time.Millisecond),
			TaskID:     testChildTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindMessage,
			From:       lib.Party{Session: "gateway"},
			Authority:  childAuth,
			Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-c-1", Parts: []lib.Part{{Kind: "text", Text: "verify upgrade readiness and open PR"}}}),
		})
		publishTaskEnv(t, nc, cOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-c-2",
			TS:         now.Add(70 * time.Millisecond),
			TaskID:     testChildTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testPlatformAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testChildTaskID,
				ContextID: testContextID,
				Artifact:  lib.Artifact{Name: lib.ArtifactNotice, Parts: []lib.Part{{Kind: "text", Text: "Opened PR " + testPRURL}}},
			}),
		})
		publishTaskEnv(t, nc, cOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-c-3",
			TS:         now.Add(80 * time.Millisecond),
			TaskID:     testChildTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testPlatformAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testChildTaskID,
				ContextID: testContextID,
				Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "PR #42 opened for 1.32 upgrade."}}},
			}),
		})
		publishTaskEnv(t, nc, cOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-c-4",
			TS:         now.Add(90 * time.Millisecond),
			TaskID:     testChildTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: testPlatformAddressee},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: testChildTaskID, ContextID: testContextID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
		})

		// 3. Session wake task synthesizing the child's result. As the real gateway does, the
		// wake's authority names the finished child in `via` and is addressed to the session
		// worker; its prompt is the gateway's templated wake header, never parsed for linkage.
		wakeAuth := mustJSON(map[string]any{
			"audience": map[string]any{"conversation": conv},
			"via":      map[string]any{"taskId": testChildTaskID, "session": testSessionAddressee},
		})
		wInSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", testSessionAddressee, testWakeTaskID)
		wOutSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", testSessionAddressee, testWakeTaskID)
		publishTaskEnv(t, nc, wInSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-w-1",
			TS:         now.Add(100 * time.Millisecond),
			TaskID:     testWakeTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindMessage,
			From:       lib.Party{Session: "gateway"},
			Authority:  wakeAuth,
			Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-w-1", Parts: []lib.Part{{Kind: "text", Text: "The task you delegated to platform (task " + testChildTaskID + ") completed.\nPR #42 opened."}}}),
		})
		chatPayload := mustJSON(chatmsg.Message{
			Schema:   chatmsg.Schema,
			Headline: "Upgrade Readiness Confirmed & PR Opened",
			Tone:     chatmsg.ToneSuccess,
			Note:     "GKE 1.32.1",
			Detail:   "All pre-flight checks passed and PR #42 has been created.",
			Rows: []chatmsg.Row{
				{Text: "Control Plane 1.32.1-gke.200", Severity: severityGood, Detail: "Compatible"},
			},
			Plan: &chatmsg.Plan{
				Title: "Upgrade Rollout",
				Items: []chatmsg.PlanItem{
					{ID: "step-1", Title: "Verify deprecations", Status: chatmsg.StatusComplete},
					{ID: "step-2", Title: "Open GitOps PR #42", Status: chatmsg.StatusComplete},
				},
			},
			Fold: &chatmsg.Fold{
				Title: "Node Pool Breakdown",
				Table: &chatmsg.Table{
					Columns: []string{"Pool", "Nodes", "Status"},
					Rows:    [][]string{{"default-pool", "3", "Ready"}},
				},
			},
			Choices: []chatmsg.Choice{
				{Text: "Merge PR #42", Reply: "merge PR #42"},
			},
			Links: []chatmsg.Link{
				{Text: "PR #42", URL: testPRURL},
			},
		})
		publishTaskEnv(t, nc, wOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-w-2",
			TS:         now.Add(110 * time.Millisecond),
			TaskID:     testWakeTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testWakeTaskID,
				ContextID: testContextID,
				Artifact: lib.Artifact{
					Name: lib.ArtifactResult,
					Parts: []lib.Part{
						{Kind: "text", Text: "Upgrade Readiness Confirmed & PR Opened"},
						{Kind: "data", Data: chatPayload},
					},
				},
			}),
		})
		publishTaskEnv(t, nc, wOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-w-3",
			TS:         now.Add(120 * time.Millisecond),
			TaskID:     testWakeTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: testWakeTaskID, ContextID: testContextID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
		})
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
}

func publishTaskEnv(t *testing.T, nc *nats.Conn, subject string, env lib.Envelope) {
	t.Helper()
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Publish(subject, raw); err != nil {
		t.Fatal(err)
	}
}

func mustJSON(v any) json.RawMessage {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		panic(err)
	}
	return json.RawMessage(bytes.TrimSpace(buf.Bytes()))
}

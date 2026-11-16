package workeradapter

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// submitWithParts is submit with extra parts on the message, beside the text.
func submitWithParts(t *testing.T, c *lib.Client, addressee, taskID, text string, extra ...lib.Part) *lib.Envelope {
	t.Helper()
	parts := append([]lib.Part{{Kind: "text", Text: text}}, extra...)
	payload, err := json.Marshal(lib.Message{
		Role: "user", Parts: parts, MessageID: "msg-" + taskID,
		TaskID: taskID, ContextID: "ctx-" + taskID,
	})
	if err != nil {
		t.Fatalf("marshal message: %v", err)
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, "ctx-"+taskID, "corr-"+taskID, payload,
		lib.WithTo(lib.Party{Session: addressee}),
		lib.WithAuthority(authorityFor(t, mintFor(t, c, taskID, addressee))))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Publish(ctx, lib.TaskInSubject(addressee, taskID), env); err != nil {
		t.Fatalf("publish submission: %v", err)
	}
	return env
}

// mcpServerEnv is the delegate server's env in a harness run's recorded argv
// (its --mcp-config argument), or nil when the run had none.
func mcpServerEnv(t *testing.T, argv string) map[string]string {
	t.Helper()
	i := strings.Index(argv, `{"`+MCPServersKey)
	if i < 0 {
		return nil
	}
	dec := json.NewDecoder(strings.NewReader(argv[i:]))
	var cfg map[string]map[string]struct {
		Env map[string]string `json:"env"`
	}
	if err := dec.Decode(&cfg); err != nil {
		t.Fatalf("the run's --mcp-config doesn't parse: %v\nargv: %s", err, argv)
	}
	return cfg[MCPServersKey][DelegateMCPServer].Env
}

// Through the adapter's own run (not the helper alone): a reused session
// pod's turn that carries a delegateTargets part hands it to that turn's
// delegate server, in the --mcp-config the harness is started with; the next
// turn, with no part, gets no list, so it reads as platform only rather than
// keeping the earlier turn's agents.
func TestEachReusedTurnHandsItsOwnDelegateTargetsToItsHarness(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-otter-t5u6"
	dir := t.TempDir()
	sock := delegateSock(t)
	mcpCfg := `{"` + MCPServersKey + `":{"` + DelegateMCPServer + `":{"command":"worker-adapter","args":["mcp"]}}}`
	harness := append(recordingStub(t, dir, ""), MCPConfigFlag, mcpCfg)

	targets := lib.DelegateTargets{Targets: []lib.DelegateTarget{
		{Name: "platform", Description: "the platform agent"},
		{Name: "e2e5", Description: "reads one cluster", Parameters: []lib.ProfileParameter{{Name: "cluster", Required: true}}},
	}}
	part, err := lib.DelegateTargetsPart(targets)
	if err != nil {
		t.Fatal(err)
	}
	submitWithParts(t, c, session, "task-d-1", "use e2e5", part)
	cfg := reuseConfig(url, "task-d-1", session, harness)
	cfg.DelegateSocket = sock
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)
	waitState(t, c, session, "task-d-1", lib.StateCompleted)

	env := mcpServerEnv(t, readRun(t, dir, "argv", 1))
	raw, ok := env[EnvDelegateTargets]
	if !ok {
		t.Fatalf("the first turn's delegate server env %v has no %s", env, EnvDelegateTargets)
	}
	t.Setenv(EnvDelegateTargets, raw)
	got := DelegateTargetsFromEnv(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if got == nil || len(got.Targets) != 2 || got.Targets[1].Name != "e2e5" || len(got.Targets[1].Parameters) != 1 {
		t.Fatalf("the first turn's server would read %+v, want the turn's two targets", got)
	}

	submit(t, c, session, "task-d-2", "and now?")
	waitRun(t, dir, "argv", 2)
	waitState(t, c, session, "task-d-2", lib.StateCompleted)
	if env := mcpServerEnv(t, readRun(t, dir, "argv", 2)); env[EnvDelegateTargets] != "" {
		t.Fatalf("a turn with no part kept a list: %s", env[EnvDelegateTargets])
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

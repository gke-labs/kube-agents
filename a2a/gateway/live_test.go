package gateway

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// liveTestRelayDurable keeps the in-process gateway off the deployed
// gateway's durable: bound to one durable, the two would split event
// deliveries and starve each other probabilistically (observed live, 9/3).
// One fixed name, reused across runs, so the install's max_consumers
// budget pays for exactly one extra consumer rather than one per run.
const liveTestRelayDurable = "gateway-relay-livetest"

// TestLiveAgainstInstallNATS runs the gateway (fake chat adapter, real bus
// client) against a real deployment's NATS — the W6 install via
// port-forward — under the REAL gateway user's deny-by-default grants, with
// a stand-in executor on the worker user. This is the half of the DoD unit
// tests cannot prove: that every JetStream interaction the gateway performs
// (durable consumer on TASKS, ordered replay for tasks/get, KV on
// session-state, acks, inbox traffic) survives the permission lists.
//
// Skipped unless the env is set:
//
//	A2A_LIVE_NATS_URL=nats://127.0.0.1:4222 \
//	A2A_LIVE_GATEWAY_PASSWORD=... A2A_LIVE_WORKER_PASSWORD=... \
//	go test ./gateway -run TestLive -v -count=1
func TestLiveAgainstInstallNATS(t *testing.T) {
	url := os.Getenv("A2A_LIVE_NATS_URL")
	gwPass := os.Getenv("A2A_LIVE_GATEWAY_PASSWORD")
	wkPass := os.Getenv("A2A_LIVE_WORKER_PASSWORD")
	if url == "" || gwPass == "" || wkPass == "" {
		t.Skip("live NATS env not set; see comment")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	client, err := lib.Connect(ctx, url,
		lib.WithName("a2a-gateway-livetest"),
		lib.WithNATSOptions(
			nats.UserInfo("gateway", gwPass),
			nats.CustomInboxPrefix("_INBOX.gateway"),
		))
	if err != nil {
		t.Fatalf("gateway connect: %v", err)
	}
	defer client.Close()

	worker, err := lib.Connect(ctx, url,
		lib.WithName("a2a-worker-livetest"),
		lib.WithNATSOptions(
			nats.UserInfo("worker", wkPass),
			nats.CustomInboxPrefix("_INBOX.worker"),
		))
	if err != nil {
		t.Fatalf("worker connect: %v", err)
	}
	defer worker.Close()

	mapFile := t.TempDir() + "/principal-map"
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	adapter := newFakeAdapter()
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("live-test-salt"),
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "discord", RelayDurable: liveTestRelayDurable})
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = g.Run(ctx) }()
	time.Sleep(2 * time.Second) // let the durable bind before traffic

	// Beat 1 shape: a chat message becomes a task addressed to platform; the
	// executor answers over the bus; the reply relays back.
	marker := "live grants check " + time.Now().UTC().Format(time.RFC3339)
	adapter.inbox <- InboundMessage{
		Conversation: "discord:live/thread-livetest", Kind: "group",
		AuthorID: "1001", MessageID: "live-1", Text: marker,
	}

	// The stand-in executor finds the task the way W7's bridge will: from
	// the stream, under the worker user. Match on the marker so a stale task
	// from an earlier run can never satisfy this.
	var origin *lib.Envelope
	waitFor(t, "task on the real TASKS stream", func() bool {
		task, err := findLatestLiveTask("worker", wkPass, url)
		if err != nil || task == nil {
			return false
		}
		var m lib.Message
		if json.Unmarshal(task.Payload, &m) != nil || joinTextParts(m.Parts) != marker {
			return false
		}
		origin = task
		return true
	})

	exec, err := worker.NewTaskExecution(origin, lib.Party{Session: "platform", AgentType: "livetest-executor"}, "platform")
	if err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatalf("submitted under worker grants: %v", err)
	}
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "live step 1"}}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "rolling line edit through real grants", func() bool {
		for _, e := range adapter.editTexts() {
			if strings.Contains(e, "live step 1") {
				return true
			}
		}
		return false
	})

	// Beat 2 shape: "what is it doing" answered by replay under the gateway
	// user's grants (ordered consumer + stream msg-get on TASKS).
	adapter.inbox <- InboundMessage{
		Conversation: "discord:live/thread-livetest", Kind: "group",
		AuthorID: "1001", MessageID: "live-2", Text: "what is it doing",
	}
	waitFor(t, "status by replay", func() bool {
		for _, p := range adapter.postTexts() {
			if strings.Contains(p, "replay") && strings.Contains(p, "working") {
				return true
			}
		}
		return false
	})

	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "live result: grants hold"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "result relayed", func() bool {
		for _, p := range adapter.postTexts() {
			if p == "live result: grants hold" {
				return true
			}
		}
		return false
	})
	t.Log("live DoD (bus half) held: submit under gateway grants, execute under worker grants, relay + replay under gateway grants")
}

// findLatestLiveTask fetches the newest message on the platform in subjects
// as the named bus user (stream msg-get by last_by_subj; wildcards are
// legal there — both worker and gateway hold it).
func findLatestLiveTask(user, pass, url string) (*lib.Envelope, error) {
	nc, err := nats.Connect(url, nats.UserInfo(user, pass), nats.CustomInboxPrefix("_INBOX."+user))
	if err != nil {
		return nil, err
	}
	defer nc.Close()
	msg, err := nc.Request("$JS.API.STREAM.MSG.GET.TASKS", []byte(`{"last_by_subj":"a2a.tasks.platform.*.in"}`), 5*time.Second)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Message struct {
			Data []byte `json:"data"`
		} `json:"message"`
		Error *struct {
			Description string `json:"description"`
		} `json:"error"`
	}
	if err := json.Unmarshal(msg.Data, &resp); err != nil {
		return nil, err
	}
	if resp.Error != nil {
		return nil, nil
	}
	return lib.ParseEnvelope(resp.Message.Data)
}

// TestLiveEndToEndThroughBridge is the W3 DoD's bus path with no stand-ins:
// the gateway (fake chat adapter in Discord's place) submits to platform on
// the real install, W7's bridge drives the real platform agent, and the
// real answer relays back — plus "what is it doing" answered by replay
// while the task runs. Requires the same env as TestLiveAgainstInstallNATS
// (worker password unused here but kept for the shared gate).
func TestLiveEndToEndThroughBridge(t *testing.T) {
	url := os.Getenv("A2A_LIVE_NATS_URL")
	gwPass := os.Getenv("A2A_LIVE_GATEWAY_PASSWORD")
	if url == "" || gwPass == "" || os.Getenv("A2A_LIVE_BRIDGE") != "true" {
		t.Skip("live bridge env not set (A2A_LIVE_BRIDGE=true)")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	client, err := lib.Connect(ctx, url,
		lib.WithName("a2a-gateway-livee2e"),
		lib.WithNATSOptions(nats.UserInfo("gateway", gwPass), nats.CustomInboxPrefix("_INBOX.gateway")))
	if err != nil {
		t.Fatalf("gateway connect: %v", err)
	}
	defer client.Close()

	mapFile := t.TempDir() + "/principal-map"
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	adapter := newFakeAdapter()
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("live-test-salt"),
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "discord", RelayDurable: liveTestRelayDurable})
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = g.Run(ctx) }()
	time.Sleep(2 * time.Second)

	conv := "discord:live/e2e-" + time.Now().UTC().Format("150405")
	adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "e2e-1",
		Text: "What is the upgrade readiness of the fleet? Answer from the upgrade-readiness topic.",
	}

	// Beat 2 while it runs: status by replay, never forwarded to the bridge.
	time.Sleep(8 * time.Second)
	adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "e2e-2",
		Text: "what is it doing",
	}
	waitFor(t, "status answered by replay", func() bool {
		for _, p := range adapter.postTexts() {
			if strings.Contains(p, "replay") {
				return true
			}
		}
		return false
	})

	// Beat 1: the real platform agent's answer, relayed back. Hermes takes
	// its time; give it the full window.
	deadline := time.Now().Add(4 * time.Minute)
	for time.Now().Before(deadline) {
		for _, p := range adapter.postTexts() {
			low := strings.ToLower(p)
			if strings.Contains(low, "readiness") && !strings.Contains(p, "replay") && !strings.HasPrefix(p, "⏳") {
				t.Logf("real answer relayed (%d chars): %.200s", len(p), p)
				return
			}
		}
		time.Sleep(3 * time.Second)
	}
	t.Fatalf("no real answer relayed; posts so far: %q", adapter.postTexts())
}

// TestLiveSessionCapOnInstall proves S8's DoD against the real install: with
// the cap set to 2, two concurrent Delegates spawn real worker pods and a
// third is refused with the honest message while the first two keep running
// — then both complete on the real bus, so the path being capped still works
// end to end. The gateway here is in-process (fake chat adapter in Discord's
// place, the W3 convention) with the REAL pod spawner pointed at the install
// through a pinned kube context. It never calls Run, so the deployed gateway
// keeps the shared relay durable and Discord delivery keeps working - though
// that gateway will drain this test's task events and log routing errors for
// the synthetic conversations (noise, not damage; the records also age out
// through its reaper). Completion is asserted by replay (TasksGet), which
// rides an ephemeral ordered consumer of its own.
//
//	kubectl --context "$CTX" -n kubeagents-system port-forward svc/platform-agent-a2a-nats 14222:4222 &
//	A2A_LIVE_NATS_URL=nats://127.0.0.1:14222 \
//	A2A_LIVE_GATEWAY_PASSWORD=... \
//	A2A_LIVE_KUBECONTEXT=gke_bnaylor-kagents-dev_northamerica-northeast1_a2a-next-dev \
//	go test ./gateway -run TestLiveSessionCapOnInstall -v -count=1 -timeout 15m
func TestLiveSessionCapOnInstall(t *testing.T) {
	url := os.Getenv("A2A_LIVE_NATS_URL")
	gwPass := os.Getenv("A2A_LIVE_GATEWAY_PASSWORD")
	kubeCtx := os.Getenv("A2A_LIVE_KUBECONTEXT")
	if url == "" || gwPass == "" || kubeCtx == "" {
		t.Skip("live cap env not set; see comment")
	}
	ns := envOr("A2A_LIVE_NAMESPACE", "kubeagents-system")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	client, err := lib.Connect(ctx, url,
		lib.WithName("a2a-gateway-captest"),
		lib.WithNATSOptions(
			nats.UserInfo("gateway", gwPass),
			nats.CustomInboxPrefix("_INBOX.gateway"),
		))
	if err != nil {
		t.Fatalf("gateway connect: %v", err)
	}
	defer client.Close()

	rc, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		clientcmd.NewDefaultClientConfigLoadingRules(),
		&clientcmd.ConfigOverrides{CurrentContext: kubeCtx},
	).ClientConfig()
	if err != nil {
		t.Fatalf("kubeconfig for context %q: %v", kubeCtx, err)
	}
	cs, err := kubernetes.NewForConfig(rc)
	if err != nil {
		t.Fatal(err)
	}

	mapFile := t.TempDir() + "/principal-map"
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := &Config{
		// The spawned pods dial the bus in-cluster; only this process rides
		// the port-forward.
		NATSURL:          envOr("A2A_LIVE_INCLUSTER_NATS_URL", "nats://platform-agent-a2a-nats."+ns+".svc:4222"),
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		MaxSessions:      2,
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("live-test-salt"),
		Namespace:        ns,
		WorkerImage:      envOr("A2A_LIVE_WORKER_IMAGE", "northamerica-northeast1-docker.pkg.dev/bnaylor-kagents-dev/a2a-demo/worker-next:latest"),
		NATSCredsSecret:  envOr("A2A_NATS_CREDS_SECRET", "platform-agent-a2a-nats-creds"),
	}
	adapter := newFakeAdapter()
	sp := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "discord", Spawner: sp, RelayDurable: liveTestRelayDurable})
	if err != nil {
		t.Fatal(err)
	}

	if live, err := sp.LiveSessions(ctx); err != nil {
		t.Fatalf("live count against the install: %v", err)
	} else if live != 0 {
		t.Skipf("install busy: %d live session pods; rerun when quiet", live)
	}

	convA, convB, convC := "discord:livecap/s8-a", "discord:livecap/s8-b", "discord:livecap/s8-c"
	turn := func(conv, id, text string) {
		t.Helper()
		g.handleInbound(InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: id, Text: text})
	}
	// A failed run must not leave Running workers holding the bus
	// credential; a clean one retires its task indexes. (Session records
	// themselves age out through the deployed gateway's reaper.)
	t.Cleanup(func() {
		cctx, ccancel := context.WithTimeout(context.Background(), time.Minute)
		defer ccancel()
		for _, conv := range []string{convA, convB, convC} {
			rec, err := g.reg.Get(cctx, conv)
			if err != nil || rec == nil {
				continue
			}
			for _, tr := range rec.Tasks {
				_ = g.reg.DropTask(cctx, tr.ID)
			}
			if rec.PodName != "" {
				_ = sp.Delete(cctx, rec.PodName)
			}
		}
	})

	// Task identity comes from rec.Tasks, captured right after the turn:
	// the deployed gateway drains this test's events off the shared relay
	// durable and retires ActiveTask in the shared KV as terminals land, so
	// ActiveTask is not ours to read later - the Tasks history is.
	type liveTask struct{ id, addressee string }
	taskFor := func(conv string) liveTask {
		t.Helper()
		rec, err := g.reg.Get(ctx, conv)
		if err != nil || rec == nil || len(rec.Tasks) == 0 {
			t.Fatalf("no task recorded for %s: %+v (err=%v)", conv, rec, err)
		}
		tr := rec.Tasks[len(rec.Tasks)-1]
		return liveTask{id: tr.ID, addressee: tr.Addressee}
	}

	turn(convA, "s8-live-1", "Delegate: write a haiku about pod quotas")
	taskA := taskFor(convA)
	turn(convB, "s8-live-2", "Delegate: write a haiku about resource limits")
	taskB := taskFor(convB)
	waitLive(t, "two live session pods on the install", 2*time.Minute, func() bool {
		n, err := sp.LiveSessions(ctx)
		return err == nil && n == 2
	})

	turn(convC, "s8-live-3", "Delegate: write a haiku about refusals")
	refused := false
	for _, p := range adapter.postTexts() {
		if strings.Contains(p, "2 session workers") && strings.Contains(p, "(cap 2)") {
			refused = true
		}
	}
	if !refused {
		t.Fatalf("no honest refusal at the cap; posts: %q", adapter.postTexts())
	}
	if n, err := sp.LiveSessions(ctx); err != nil || n != 2 {
		t.Fatalf("third pod appeared past the cap (n=%d, err=%v)", n, err)
	}
	// A refused turn changes nothing the turn did: first contact Creates
	// the bare identity record (contextId minting is create-only, per the
	// gateway design), so conversation C has a record — but no task, no
	// pod, and no route mutation.
	if rec, err := g.reg.Get(ctx, convC); err != nil || rec == nil ||
		rec.ActiveTask != nil || rec.PodName != "" || rec.BusSession != "" || len(rec.Tasks) != 0 {
		t.Fatalf("refused conversation kept turn state: %+v (err=%v)", rec, err)
	}

	// The first two keep running to completion on the real bus — the path
	// being capped still works end to end (worker pod -> LiteLLM -> result).
	for conv, lt := range map[string]liveTask{convA: taskA, convB: taskB} {
		waitLive(t, conv+" completes", 5*time.Minute, func() bool {
			task, err := g.client.TasksGet(ctx, lt.addressee, lt.id)
			return err == nil && task.Final && task.State == lib.StateCompleted
		})
	}
}

// TestLiveSaltJoinsAcrossSurfaces proves the salt half of S9's DoD on the
// real bus: a gateway configured with the install's SESSION_KV_SALT (passed
// in as A2A_LIVE_SALT, read from platform-agent-secrets by the harness)
// publishes an authority block whose requester principal is exactly
// HMAC-SHA256(salt, principal) — the digest the shipped attribution path
// produces for the same human with the same Secret, so the cross-surface
// join resolves by digest prefix (the gateway truncates to 32 hex chars
// behind the "hmac:" tag; session metadata carries the full digest).
func TestLiveSaltJoinsAcrossSurfaces(t *testing.T) {
	url := os.Getenv("A2A_LIVE_NATS_URL")
	gwPass := os.Getenv("A2A_LIVE_GATEWAY_PASSWORD")
	salt := os.Getenv("A2A_LIVE_SALT")
	if url == "" || gwPass == "" || salt == "" {
		t.Skip("live salt env not set (A2A_LIVE_NATS_URL, A2A_LIVE_GATEWAY_PASSWORD, A2A_LIVE_SALT)")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	client, err := lib.Connect(ctx, url,
		lib.WithName("a2a-gateway-salttest"),
		lib.WithNATSOptions(nats.UserInfo("gateway", gwPass), nats.CustomInboxPrefix("_INBOX.gateway")))
	if err != nil {
		t.Fatalf("gateway connect: %v", err)
	}
	defer client.Close()

	mapFile := t.TempDir() + "/principal-map"
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	adapter := newFakeAdapter()
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		// Trimmed the way FromEnv trims it, so this gateway is configured
		// exactly as the deployed one.
		AttributionSalt: []byte(strings.TrimSpace(salt)),
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "discord", RelayDurable: liveTestRelayDurable})
	if err != nil {
		t.Fatal(err)
	}

	conv := "discord:livesalt/s9-" + time.Now().UTC().Format("150405")
	marker := "salt join check " + time.Now().UTC().Format(time.RFC3339)
	g.handleInbound(InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "salt-1", Text: marker})
	t.Cleanup(func() {
		cctx, ccancel := context.WithTimeout(context.Background(), time.Minute)
		defer ccancel()
		if rec, err := g.reg.Get(cctx, conv); err == nil && rec != nil {
			for _, tr := range rec.Tasks {
				_ = g.reg.DropTask(cctx, tr.ID)
			}
		}
	})

	var env *lib.Envelope
	waitLive(t, "submission with authority on the real stream", 30*time.Second, func() bool {
		e, err := findLatestLiveTask("gateway", gwPass, url)
		if err != nil || e == nil {
			return false
		}
		var m lib.Message
		if json.Unmarshal(e.Payload, &m) != nil || joinTextParts(m.Parts) != marker {
			return false
		}
		env = e
		return true
	})

	var a Authority
	if err := json.Unmarshal(env.Authority, &a); err != nil {
		t.Fatalf("authority block: %v", err)
	}
	// The expectation is constructed independently of the Pseudonymizer —
	// raw HMAC over the stripped salt, the way the shipped redactor
	// computes it (hmac.new(salt.strip(), value, sha256).hexdigest()) — so
	// a construction divergence between the two surfaces fails here
	// instead of passing a gateway-equals-gateway comparison.
	mac := hmac.New(sha256.New, []byte(strings.TrimSpace(salt)))
	mac.Write([]byte("test:bnaylor"))
	want := "hmac:" + hex.EncodeToString(mac.Sum(nil))[:32]
	if a.Requester.Principal != want {
		t.Fatalf("principal on the bus = %s, want %s (HMAC under the install salt)", a.Requester.Principal, want)
	}
	t.Logf("principal on the real stream: %s — joins the shipped attribution digest by prefix", a.Requester.Principal)
}

// TestLiveDetachedDelegateSupervisorTerminal proves S9's DoD against the
// real install: a Delegate over a DETACHED task publishes that task's
// terminal `canceled` on the real stream BEFORE deleting the pod — under
// the real gateway user's grants, against the real k8s API — and the new
// delegation then completes end to end. The detached state is seeded the
// way a wedged worker leaves it (a real Running pod, a cancel already
// recorded, no terminal), because a healthy worker confirms a cancel too
// fast to hold the window open. Same env and conventions as
// TestLiveSessionCapOnInstall.
func TestLiveDetachedDelegateSupervisorTerminal(t *testing.T) {
	url := os.Getenv("A2A_LIVE_NATS_URL")
	gwPass := os.Getenv("A2A_LIVE_GATEWAY_PASSWORD")
	kubeCtx := os.Getenv("A2A_LIVE_KUBECONTEXT")
	if url == "" || gwPass == "" || kubeCtx == "" {
		t.Skip("live env not set; see TestLiveSessionCapOnInstall")
	}
	ns := envOr("A2A_LIVE_NAMESPACE", "kubeagents-system")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	client, err := lib.Connect(ctx, url,
		lib.WithName("a2a-gateway-suptest"),
		lib.WithNATSOptions(
			nats.UserInfo("gateway", gwPass),
			nats.CustomInboxPrefix("_INBOX.gateway"),
		))
	if err != nil {
		t.Fatalf("gateway connect: %v", err)
	}
	defer client.Close()

	rc, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		clientcmd.NewDefaultClientConfigLoadingRules(),
		&clientcmd.ConfigOverrides{CurrentContext: kubeCtx},
	).ClientConfig()
	if err != nil {
		t.Fatalf("kubeconfig for context %q: %v", kubeCtx, err)
	}
	cs, err := kubernetes.NewForConfig(rc)
	if err != nil {
		t.Fatal(err)
	}

	mapFile := t.TempDir() + "/principal-map"
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := &Config{
		NATSURL:          envOr("A2A_LIVE_INCLUSTER_NATS_URL", "nats://platform-agent-a2a-nats."+ns+".svc:4222"),
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("live-test-salt"),
		Namespace:        ns,
		WorkerImage:      envOr("A2A_LIVE_WORKER_IMAGE", "northamerica-northeast1-docker.pkg.dev/bnaylor-kagents-dev/a2a-demo/worker-next:latest"),
		NATSCredsSecret:  envOr("A2A_NATS_CREDS_SECRET", "platform-agent-a2a-nats-creds"),
	}
	// With A2A_LIVE_OWNER_DEPLOYMENT set (the deployed gateway's own
	// Deployment), the spawner resolves the real owner and every pod this
	// test creates carries the ownerReference — asserted below against the
	// real API, alongside the pod deadline.
	cfg.OwnerDeployment = os.Getenv("A2A_LIVE_OWNER_DEPLOYMENT")
	adapter := newFakeAdapter()
	sp := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
	if err := sp.resolveOwner(ctx); err != nil {
		t.Fatalf("resolving owner on the install: %v", err)
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "discord", Spawner: sp, RelayDurable: liveTestRelayDurable})
	if err != nil {
		t.Fatal(err)
	}

	conv := "discord:livesup/s9-a"
	// Seed the detached shape: a real Running worker pod (its synthetic
	// task never gets a submission, so the adapter idles and publishes
	// nothing), a session record whose active task is detached with the
	// cancel recorded — what a stop against a wedged worker leaves behind.
	seedTask := "task-s9sup-" + time.Now().UTC().Format("150405")
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-s9sup", Kind: "group",
		Addressee: "chat-s9sup-seed", BusSession: "chat-s9sup-seed",
		LastActivity: time.Now().UTC(),
		ActiveTask:   &ActiveTask{TaskID: seedTask, CorrelationID: "corr-s9sup", Detached: true},
		Tasks:        []TaskRef{{ID: seedTask, Addressee: "chat-s9sup-seed", Canceled: true}},
	}
	podName, err := sp.Spawn(ctx, rec, seedTask, "")
	if err != nil {
		t.Fatalf("seed pod spawn on the install: %v", err)
	}
	rec.PodName = podName
	if err := g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	// The spawned pod's spec, read back from the real API: the pod-level
	// deadline exists (a wedged adapter has an owner), and when the owner
	// is configured, the ownerReference points at the real Deployment.
	seedPod, err := cs.CoreV1().Pods(ns).Get(ctx, podName, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if seedPod.Spec.ActiveDeadlineSeconds == nil {
		t.Fatal("live spawned pod has no activeDeadlineSeconds")
	}
	t.Logf("live pod activeDeadlineSeconds=%d", *seedPod.Spec.ActiveDeadlineSeconds)
	if cfg.OwnerDeployment != "" {
		if len(seedPod.OwnerReferences) != 1 || seedPod.OwnerReferences[0].Name != cfg.OwnerDeployment ||
			seedPod.OwnerReferences[0].Kind != "Deployment" {
			t.Fatalf("live pod ownerReferences = %+v", seedPod.OwnerReferences)
		}
		t.Logf("live pod owned by Deployment/%s uid=%s", seedPod.OwnerReferences[0].Name, seedPod.OwnerReferences[0].UID)
	}
	t.Cleanup(func() {
		cctx, ccancel := context.WithTimeout(context.Background(), time.Minute)
		defer ccancel()
		if fresh, err := g.reg.Get(cctx, conv); err == nil && fresh != nil {
			for _, tr := range fresh.Tasks {
				_ = g.reg.DropTask(cctx, tr.ID)
			}
			if fresh.PodName != "" {
				_ = sp.Delete(cctx, fresh.PodName)
			}
		}
		_ = sp.Delete(cctx, podName)
	})

	// The Delegate over the detached task: terminal first, then the delete,
	// then the new incarnation.
	g.handleInbound(InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "s9-live-1", Text: "Delegate: write one sentence about supervisors"})

	// The detached task's terminal is on the real stream, state canceled —
	// replay through the real grants is the proof (assertion 13's
	// enumeration for a cancel).
	task, err := g.client.TasksGet(ctx, "chat-s9sup-seed", seedTask)
	if err != nil {
		t.Fatalf("replaying the detached task: %v", err)
	}
	if !task.Final || task.State != lib.StateCanceled {
		t.Fatalf("detached task state = %s (final=%v), want terminal canceled before the delete", task.State, task.Final)
	}
	// The seeded pod is gone (or terminating) on the real API.
	waitLive(t, "seed pod deleted", 2*time.Minute, func() bool {
		p, err := cs.CoreV1().Pods(ns).Get(ctx, podName, metav1.GetOptions{})
		return err != nil || p.DeletionTimestamp != nil
	})
	// And the delegation it made way for completes end to end.
	fresh, err := g.reg.Get(ctx, conv)
	if err != nil || fresh == nil || len(fresh.Tasks) < 2 {
		t.Fatalf("no delegated task recorded: %+v (err=%v)", fresh, err)
	}
	newRef := fresh.Tasks[len(fresh.Tasks)-1]
	waitLive(t, "delegated task completes", 5*time.Minute, func() bool {
		task, err := g.client.TasksGet(ctx, newRef.Addressee, newRef.ID)
		return err == nil && task.Final && task.State == lib.StateCompleted
	})
}

// waitLive is waitFor with a live-install clock: pod pulls and model calls
// take longer than the in-memory suite's 10s budget.
func waitLive(t *testing.T, what string, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Second)
	}
	t.Fatalf("timed out waiting for %s", what)
}

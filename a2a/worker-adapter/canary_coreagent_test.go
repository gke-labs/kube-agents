package workeradapter

// The harness-isolation canary, core-agent half (#6710): the real
// core-agent harness binary, driven by the adapter with the core-agent
// driver, against a fake Gemini endpoint. In turn one the model's shell
// call plants a process in its own session that keeps writing a file, saves
// the environment a tool child gets, and tries to read the adapter's
// environ and fds through /proc. A steer held during turn one starts turn
// two on a fresh process; when turn two's first model request arrives, the
// canary checks whether the plant is still writing.
//
// Two legs:
//   - control: the namespace-wide kill and the boundary check off, the
//     adapter dumpable. Every detector must fire (the plant outlives turn
//     one, /proc/1/environ is readable), so a detector that sees nothing
//     fails rather than passes;
//   - guarded: as shipped. The adapter must be the container's init (pid 1)
//     and non-dumpable: the plant is gone before turn two, /proc/1 is
//     closed to the tool, and the tool child's environment holds no
//     credential.
//
// In both legs the tool child's environment must lack the model key and an
// unlisted secret, and must hold an allowlisted CREDENTIAL_PROXY_* value,
// which shows the saved environment is the child's own.
//
// Skipped unless A2A_HARNESS_CANARY_COREAGENT names a core-agent-harness
// binary. The guarded leg needs this test process to be pid 1 (run it as a
// container's entrypoint, as a non-root user) and fails if it isn't. Nothing
// in CI sets the variable.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

const (
	canaryCoreAgentEnv = "A2A_HARNESS_CANARY_COREAGENT"
	canaryModelKey     = "canary-model-key-must-not-reach-a-tool"
	canarySecret       = "canary-unlisted-secret-must-not-reach-a-tool"
	canaryVisible      = "canary-allowlisted-value"
	canaryTurnTwo      = "CANARY-TURN-TWO: say done"
	// canaryAdapterMark is set in this process's environment at exec (as
	// CANARY_ADAPTER_MARK) by whatever starts it as pid 1: a tool that can
	// read /proc/1/environ, which shows the exec-time environment, finds it.
	canaryAdapterMark = "canary-adapter-environ-mark"
)

// canaryGemini is a fake Gemini endpoint. Turn one's first request gets a
// shell call (plant); a request answering a tool call gets text; turn two's
// first request (it carries canaryTurnTwo) records whether the plant is
// still writing, then gets text.
type canaryGemini struct {
	plant    string // the shell command turn one runs
	aliveAt  string // the file the plant keeps writing
	mu       sync.Mutex
	requests int
	checked  bool
	stillOn  bool
	answers  []string // each tool result the model was sent, for a failure message
	// steered is closed once the test has published turn two's steer; turn
	// one's answer to its tool result waits for it, so the steer is held
	// during turn one.
	steered chan struct{}
	held    bool
}

func (g *canaryGemini) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b, _ := io.ReadAll(r.Body)
	var req struct {
		Contents []struct {
			Parts []struct {
				Text             string          `json:"text"`
				FunctionResponse json.RawMessage `json:"functionResponse"`
			} `json:"parts"`
		} `json:"contents"`
	}
	_ = json.Unmarshal(b, &req)
	g.mu.Lock()
	g.requests++
	g.mu.Unlock()
	answersTool, turnTwo := false, false
	if n := len(req.Contents); n > 0 {
		for _, p := range req.Contents[n-1].Parts {
			if len(p.FunctionResponse) > 0 {
				answersTool = true
				g.mu.Lock()
				g.answers = append(g.answers, string(p.FunctionResponse))
				g.mu.Unlock()
			}
			if strings.Contains(p.Text, canaryTurnTwo) {
				turnTwo = true
			}
		}
	}
	var part map[string]any
	switch {
	case answersTool:
		g.mu.Lock()
		first := !g.held
		g.held = true
		g.mu.Unlock()
		if first {
			select {
			case <-g.steered:
			case <-time.After(30 * time.Second):
			}
		}
		part = map[string]any{"text": "turn done"}
	case turnTwo:
		g.mu.Lock()
		first := !g.checked
		g.checked = true
		g.mu.Unlock()
		if first {
			a, _ := os.ReadFile(g.aliveAt)
			time.Sleep(700 * time.Millisecond)
			b, _ := os.ReadFile(g.aliveAt)
			g.mu.Lock()
			g.stillOn = len(a) > 0 && string(a) != string(b)
			g.mu.Unlock()
		}
		part = map[string]any{"text": "turn two done"}
	default:
		part = map[string]any{"functionCall": map[string]any{"name": "bash", "args": map[string]any{"command": g.plant}}}
	}
	resp := map[string]any{
		"candidates":    []any{map[string]any{"content": map[string]any{"role": "model", "parts": []any{part}}, "finishReason": "STOP", "index": 0}},
		"usageMetadata": map[string]any{"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
	}
	out, _ := json.Marshal(resp)
	if strings.Contains(r.URL.Path, "streamGenerateContent") {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprintf(w, "data: %s\r\n\r\n", out)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(out)
}

// canaryLeg is one leg's findings.
type canaryLeg struct {
	state      lib.TaskState
	runs       int
	checked    bool
	plantAlive bool   // still writing when turn two began
	env        string // the tool child's environment
	environ    string // what reading /proc/1/environ gave the tool
	fds        string // what listing /proc/1/fd gave the tool
}

func runCanaryLeg(t *testing.T, bin string, guarded bool) canaryLeg {
	t.Helper()
	w := t.TempDir()
	// The plant is a script the model runs as one simple command, which the
	// policy allows by its exact text.
	script := filepath.Join(w, "plant.sh")
	body := fmt.Sprintf(`perl -MPOSIX -e 'POSIX::setsid() or die "setsid: $!"; exec @ARGV' sh -c 'echo $$ > %[1]s/plant.pid; i=0; while :; do i=$((i+1)); echo $i > %[1]s/alive; sleep 0.1; done' >/dev/null 2>&1 </dev/null &
env > %[1]s/env.txt
cat /proc/1/environ > %[1]s/environ.txt 2>&1
ls /proc/1/fd > %[1]s/fd.txt 2>&1
echo planted
`, w)
	if err := os.WriteFile(script, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	plant := "sh " + script
	g := &canaryGemini{plant: plant, aliveAt: filepath.Join(w, "alive"), steered: make(chan struct{})}
	srv := httptest.NewServer(g)
	defer srv.Close()

	url := startServer(t)
	c := testClient(t, url)
	leg := "control"
	if guarded {
		leg = "guarded"
	}
	session, taskID := "chat-canary-"+leg, "task-canary-"+leg
	origin := submit(t, c, session, taskID, "plant something")

	runs := 0
	var mu sync.Mutex
	counting := countingDriver{coreAgentDriver: newCoreAgentDriver(), guarded: guarded, onStart: func() { mu.Lock(); runs++; mu.Unlock() }}
	counting.Policy = coreagentwire.Policy{Tools: []string{"bash"}, Allow: []string{"bash:" + plant}}
	cfg := adapterConfig(url, taskID, session, []string{bin, "--model", "canary-model"})
	cfg.Driver = counting
	cfg.TaskDeadline = 90 * time.Second
	cfg.HarnessEnv = append(os.Environ(),
		"GOOGLE_GEMINI_BASE_URL="+srv.URL,
		"GEMINI_API_KEY="+canaryModelKey,
		"CANARY_UNLISTED_SECRET="+canarySecret,
		"CREDENTIAL_PROXY_CANARY="+canaryVisible,
	)
	if !guarded {
		prevKill := killEveryoneButMe
		killEveryoneButMe = func() {}
		t.Cleanup(func() { killEveryoneButMe = prevKill })
	}
	done := runAdapter(context.Background(), cfg)
	for deadline := time.Now().Add(60 * time.Second); ; time.Sleep(50 * time.Millisecond) {
		if b, err := os.ReadFile(filepath.Join(w, "env.txt")); err == nil && len(b) > 0 {
			break
		}
		select {
		case o := <-done:
			g.mu.Lock()
			n := g.requests
			g.mu.Unlock()
			t.Fatalf("the task ended before the plant ran: state=%q err=%v, %d model requests, tool results %q", o.res.State, o.err, n, g.answers)
		default:
		}
		if time.Now().After(deadline) {
			g.mu.Lock()
			n := g.requests
			g.mu.Unlock()
			t.Fatalf("the plant never ran; %d model requests", n)
		}
	}
	publishSteerFor(t, c, origin, session, canaryTurnTwo)
	time.Sleep(300 * time.Millisecond) // the steer reaches the adapter and is held
	close(g.steered)
	out := waitOutcome(t, done, 120*time.Second)

	read := func(name string) string { b, _ := os.ReadFile(filepath.Join(w, name)); return string(b) }
	g.mu.Lock()
	defer g.mu.Unlock()
	mu.Lock()
	defer mu.Unlock()
	res := canaryLeg{state: out.res.State, runs: runs, checked: g.checked, plantAlive: g.stillOn,
		env: read("env.txt"), environ: read("environ.txt"), fds: read("fd.txt")}
	// End the plant whatever the leg found, so the next leg starts clean.
	if pid := strings.TrimSpace(read("plant.pid")); pid != "" {
		var p int
		if _, err := fmt.Sscanf(pid, "%d", &p); err == nil && p > 0 {
			_ = syscall.Kill(p, syscall.SIGKILL)
		}
	}
	reapKilledChildren(2 * time.Second)
	return res
}

// countingDriver is the core-agent driver, counting the processes it
// prepares; in the control leg its turn-boundary check is off.
type countingDriver struct {
	coreAgentDriver
	guarded bool
	onStart func()
}

func (d countingDriver) PrepareTurn(ctx context.Context, dirs TurnDirs) error {
	d.onStart()
	if !d.guarded {
		return nil
	}
	return d.coreAgentDriver.PrepareTurn(ctx, dirs)
}

func TestTheCoreAgentHarnessIsolationCanary(t *testing.T) {
	bin := os.Getenv(canaryCoreAgentEnv)
	if bin == "" {
		t.Skipf("%s is not set", canaryCoreAgentEnv)
	}
	if os.Getpid() == containerInitPID && os.Getenv("CANARY_ADAPTER_MARK") != canaryAdapterMark {
		t.Fatalf("as pid 1 the canary needs CANARY_ADAPTER_MARK=%s in its exec environment", canaryAdapterMark)
	}
	common := func(t *testing.T, l canaryLeg) {
		t.Helper()
		if l.state != lib.StateCompleted {
			t.Errorf("state=%q; want completed", l.state)
		}
		if l.runs != 2 || !l.checked {
			t.Errorf("%d processes ran and turn two checked=%v; want 2 and true", l.runs, l.checked)
		}
		if l.env == "" {
			t.Fatal("the tool child's environment was never saved")
		}
		for _, s := range []string{canaryModelKey, canarySecret} {
			if strings.Contains(l.env, s) {
				t.Errorf("the tool child's environment holds %q", s)
			}
		}
		if !strings.Contains(l.env, "CREDENTIAL_PROXY_CANARY="+canaryVisible) {
			t.Error("the saved environment lacks the allowlisted value, so it isn't the tool child's")
		}
	}

	t.Run("control", func(t *testing.T) {
		l := runCanaryLeg(t, bin, false)
		common(t, l)
		if !l.plantAlive {
			t.Error("control: the plant was not seen alive at turn two; the leftover detector sees nothing")
		}
		if os.Getpid() == containerInitPID && !strings.Contains(l.environ, canaryAdapterMark) {
			t.Errorf("control: the tool could not read the dumpable adapter's environ (%q); the /proc detector sees nothing", firstLine(l.environ))
		}
	})

	t.Run("guarded", func(t *testing.T) {
		if os.Getpid() != containerInitPID {
			t.Fatalf("the guarded leg needs this process to be pid 1, the container's init (it is %d)", os.Getpid())
		}
		if err := ProtectProcess(); err != nil {
			t.Fatal(err)
		}
		l := runCanaryLeg(t, bin, true)
		common(t, l)
		if l.plantAlive {
			t.Error("guarded: the plant from turn one was still running when turn two began")
		}
		if strings.Contains(l.environ, canaryAdapterMark) {
			t.Error("guarded: the tool read the adapter's environ")
		}
		if !strings.Contains(strings.ToLower(l.fds), "permission denied") {
			t.Errorf("guarded: listing the adapter's fds gave %q; want permission denied", firstLine(l.fds))
		}
	})
}

func firstLine(s string) string {
	s, _, _ = strings.Cut(s, "\n")
	if len(s) > 120 {
		s = s[:120]
	}
	return s
}

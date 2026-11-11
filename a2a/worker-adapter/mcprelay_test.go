package workeradapter

import (
	"bytes"
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
)

// A relay URL that isn't under the broker's own /v1/mcp/ route is refused
// before anything is sent: the bridge attaches the pod's credentials, so a
// model that rewrites its MCP config must not be able to aim them elsewhere.
func TestARelayURLOutsideTheBrokersMCPRouteIsRefused(t *testing.T) {
	const base = "http://credential-proxy.ns.svc:8765"
	for _, raw := range []string{
		"http://attacker.example:8765/v1/mcp/developer_knowledge",         // other host
		"http://credential-proxy.ns.svc:9999/v1/mcp/developer_knowledge",  // other port
		"https://credential-proxy.ns.svc:8765/v1/mcp/developer_knowledge", // other scheme
		"http://credential-proxy.ns.svc:8765/v1/exec",                     // other path
		"http://credential-proxy.ns.svc:8765/v1/mcpx/developer_knowledge",
		"http://credential-proxy.ns.svc:8765/v1/mcp/../exec",
		"http://credential-proxy.ns.svc:8765/v1/mcp/%2e%2e/exec",
		"http://credential-proxy.ns.svc:8765/v1/mcp/",
		"http://credential-proxy.ns.svc:8765/v1/mcp/a/b",
		"http://credential-proxy.ns.svc:8765/v1/mcp/developer_knowledge?x=1",
		"http://user@credential-proxy.ns.svc:8765/v1/mcp/developer_knowledge",
		"//credential-proxy.ns.svc:8765/v1/mcp/developer_knowledge",
	} {
		if _, err := RelayTarget(base, raw); err == nil {
			t.Errorf("RelayTarget(%q) was accepted", raw)
		}
	}
	if _, err := RelayTarget(base, base+"/v1/mcp/developer_knowledge"); err != nil {
		t.Errorf("the broker's own relay URL was refused: %v", err)
	}
	if _, err := RelayTarget("", base+"/v1/mcp/developer_knowledge"); err == nil {
		t.Error("a relay URL was accepted with no broker configured")
	}
}

type relayBroker struct {
	*httptest.Server
	mu      sync.Mutex
	headers []http.Header
	bodies  []string
	reply   func(w http.ResponseWriter, body []byte)
}

func newRelayBroker(t *testing.T) *relayBroker {
	b := &relayBroker{}
	b.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		b.mu.Lock()
		b.headers = append(b.headers, r.Header.Clone())
		b.bodies = append(b.bodies, r.URL.Path+" "+string(body))
		b.mu.Unlock()
		b.reply(w, body)
	}))
	t.Cleanup(b.Close)
	return b
}

// relayEnv points the bridge at broker and writes the two credential files.
func relayEnv(t *testing.T, brokerURL, task string) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "token")
	if err := os.WriteFile(tokenFile, []byte("sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("KUBE_AGENTS_VERSION", "")
	t.Setenv(EnvCredentialProxyURL, brokerURL)
	t.Setenv(EnvCredentialProxyTokenFile, tokenFile)
	taskFile := filepath.Join(dir, "task")
	if task != "" {
		if err := os.WriteFile(taskFile, []byte(task), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv(EnvCredentialProxyTaskToken, taskFile)
}

func runRelay(t *testing.T, target string, input string) (string, error) {
	var out bytes.Buffer
	err := ServeMCPRelay(context.Background(), target, strings.NewReader(input), &out,
		slog.New(slog.NewTextHandler(io.Discard, nil)))
	return out.String(), err
}

// Each message is POSTed to the broker's relay with the pod's two
// credentials, and the replies come back one per line: a JSON body, an
// event-stream body, and nothing for an accepted notification.
func TestTheRelayCarriesEachMessageWithThePodsCredentials(t *testing.T) {
	b := newRelayBroker(t)
	b.reply = func(w http.ResponseWriter, body []byte) {
		switch {
		case bytes.Contains(body, []byte(`"notifications/initialized"`)):
			w.WriteHeader(http.StatusAccepted)
		case bytes.Contains(body, []byte(`"tools/list"`)):
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\n\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[]}}\n\n")
		default:
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"ok":true}}`)
		}
	}
	relayEnv(t, b.URL, "a.b.c")
	out, err := runRelay(t, b.URL+"/v1/mcp/developer_knowledge",
		`{"jsonrpc":"2.0","id":1,"method":"initialize"}`+"\n"+
			`{"jsonrpc":"2.0","method":"notifications/initialized"}`+"\n"+
			`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`+"\n")
	if err != nil {
		t.Fatal(err)
	}
	want := `{"jsonrpc":"2.0","id":1,"result":{"ok":true}}` + "\n" + `{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}` + "\n"
	if out != want {
		t.Fatalf("out = %q, want %q", out, want)
	}
	if len(b.headers) != 3 {
		t.Fatalf("broker got %d requests, want 3", len(b.headers))
	}
	for _, h := range b.headers {
		if h.Get("Authorization") != "Bearer sa-token" || h.Get(taskCredentialHeader) != "a.b.c" {
			t.Errorf("credentials sent = %q / %q", h.Get("Authorization"), h.Get(taskCredentialHeader))
		}
		if h.Get("User-Agent") != "kube-agents/dev (daemon)" {
			t.Errorf("User-Agent = %q", h.Get("User-Agent"))
		}
	}
	if !strings.HasPrefix(b.bodies[0], "/v1/mcp/developer_knowledge ") {
		t.Errorf("sent to %q", b.bodies[0])
	}
}

// No task file is no task: the bridge sends the pod token alone and the
// broker decides. A task file that doesn't hold a credential stops the
// request, answered with an error, rather than sending it without.
func TestTheTaskCredentialIsReadPerRequest(t *testing.T) {
	b := newRelayBroker(t)
	b.reply = func(w http.ResponseWriter, _ []byte) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{}}`)
	}
	relayEnv(t, b.URL, "")
	if _, err := runRelay(t, b.URL+"/v1/mcp/developer_knowledge", `{"jsonrpc":"2.0","id":1,"method":"ping"}`+"\n"); err != nil {
		t.Fatal(err)
	}
	if got := b.headers[0].Get(taskCredentialHeader); got != "" {
		t.Errorf("a task credential was sent with no task file: %q", got)
	}
	if err := os.WriteFile(os.Getenv(EnvCredentialProxyTaskToken), []byte("not a jwt!"), 0o600); err != nil {
		t.Fatal(err)
	}
	out, err := runRelay(t, b.URL+"/v1/mcp/developer_knowledge", `{"jsonrpc":"2.0","id":7,"method":"ping"}`+"\n")
	if err != nil {
		t.Fatal(err)
	}
	if len(b.headers) != 1 {
		t.Errorf("a request went out with a malformed task credential")
	}
	assertRelayError(t, out, 7, "task credential")
}

// A redirect from the broker is never followed, so the credentials never
// reach the address it names; the request is answered with an error.
func TestTheRelayFollowsNoRedirect(t *testing.T) {
	var elsewhere int
	attacker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		elsewhere++
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(attacker.Close)
	b := newRelayBroker(t)
	b.reply = func(w http.ResponseWriter, _ []byte) {
		w.Header().Set("Location", attacker.URL+"/steal")
		w.WriteHeader(http.StatusTemporaryRedirect)
	}
	relayEnv(t, b.URL, "a.b.c")
	out, err := runRelay(t, b.URL+"/v1/mcp/developer_knowledge", `{"jsonrpc":"2.0","id":3,"method":"ping"}`+"\n")
	if err != nil {
		t.Fatal(err)
	}
	if elsewhere != 0 {
		t.Fatalf("the redirect was followed: the other server got %d request(s)", elsewhere)
	}
	assertRelayError(t, out, 3, "redirect")
}

// A target that isn't the broker's relay stops the bridge before it reads a
// message or a credential.
func TestTheRelayRefusesToStartForAnotherHost(t *testing.T) {
	b := newRelayBroker(t)
	b.reply = func(w http.ResponseWriter, _ []byte) { w.WriteHeader(http.StatusOK) }
	relayEnv(t, b.URL, "a.b.c")
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("a request reached a host that isn't the broker: %s", r.URL)
	}))
	t.Cleanup(other.Close)
	if _, err := runRelay(t, other.URL+"/v1/mcp/developer_knowledge", `{"jsonrpc":"2.0","id":1,"method":"ping"}`+"\n"); err == nil {
		t.Fatal("the bridge started for another host")
	}
}

// A broker error answers the request with its id; a notification gets nothing.
func TestABrokerErrorIsAnsweredWithTheRequestsID(t *testing.T) {
	b := newRelayBroker(t)
	b.reply = func(w http.ResponseWriter, _ []byte) { w.WriteHeader(http.StatusForbidden) }
	relayEnv(t, b.URL, "a.b.c")
	out, err := runRelay(t, b.URL+"/v1/mcp/developer_knowledge",
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`+"\n"+`{"jsonrpc":"2.0","id":"x","method":"ping"}`+"\n")
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) != 1 {
		t.Fatalf("got %d replies, want one for the request alone: %q", len(lines), out)
	}
	var reply struct {
		ID    string `json:"id"`
		Error struct{ Message string }
	}
	if err := json.Unmarshal([]byte(lines[0]), &reply); err != nil || reply.ID != "x" || !strings.Contains(reply.Error.Message, "HTTP 403") {
		t.Fatalf("reply = %s (%v)", lines[0], err)
	}
}

func assertRelayError(t *testing.T, out string, id int, want string) {
	t.Helper()
	var reply struct {
		ID    int `json:"id"`
		Error struct {
			Code    int
			Message string
		}
	}
	if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &reply); err != nil {
		t.Fatalf("reply %q: %v", out, err)
	}
	if reply.ID != id || reply.Error.Code != jsonRPCServerError || !strings.Contains(reply.Error.Message, want) {
		t.Fatalf("reply = %+v, want an error for id %d naming %q", reply, id, want)
	}
}

// A profile worker renders its persona's servers through the bridge, with the
// broker's address and the credential files passed explicitly; with no
// broker it renders none.
func TestAProfileWorkerRendersItsServersThroughTheBridge(t *testing.T) {
	env := map[string]string{
		EnvCredentialProxyURL:       "http://credential-proxy.ns.svc:8765/",
		EnvCredentialProxyTokenFile: "/var/run/broker/token",
		EnvCredentialProxyTaskToken: "/run/task/token",
	}
	r := ProfileRelay("/usr/local/bin/worker-adapter", func(k string) string { return env[k] })
	if r.URL != "http://credential-proxy.ns.svc:8765/v1/mcp" || r.Command != "/usr/local/bin/worker-adapter" ||
		strings.Join(r.Args, " ") != "mcp-relay {url}" {
		t.Fatalf("relay = %+v", r)
	}
	if r.Env[EnvCredentialProxyURL] != "http://credential-proxy.ns.svc:8765" || r.Env[EnvCredentialProxyTokenFile] != "/var/run/broker/token" ||
		r.Env[EnvCredentialProxyTaskToken] != "/run/task/token" {
		t.Errorf("relay env = %v", r.Env)
	}
	if got := ProfileRelay("/usr/local/bin/worker-adapter", func(string) string { return "" }); got.URL != "" || got.Command != "" {
		t.Errorf("a relay was built with no broker: %+v", got)
	}
}

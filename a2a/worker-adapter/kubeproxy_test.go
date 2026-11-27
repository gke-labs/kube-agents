package workeradapter

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// testWait bounds every wait on a channel in these tests: long enough that
// only a hang reaches it.
const testWait = 20 * time.Second

// kubeUpstream is a TLS server standing in for kube-proxy. It records each
// request with the connection it came on, and tells when a connection closes.
type kubeUpstream struct {
	srv *httptest.Server

	mu     sync.Mutex
	calls  []kubeCall
	closed map[string]chan struct{} // by the connection's remote address

	// started receives a path each time a held request (hang, watch, exec)
	// reaches the upstream; next releases a watch's second event.
	started chan string
	next    chan struct{}
	// gone receives a path when a held request's connection has ended.
	gone chan string
}

type kubeCall struct {
	method, path, query, host, conn string
	hdr                             http.Header
	body                            []byte
}

const (
	kubeTestCluster   = "/clusters/gke_proj_us-central1_alpha"
	kubeTestHangPath  = kubeTestCluster + "/hang"
	kubeTestWatchPath = kubeTestCluster + "/api/v1/pods"
	kubeTestExecPath  = kubeTestCluster + "/api/v1/namespaces/default/pods/p/exec"
	kubeTestEchoPath  = kubeTestCluster + "/echo"
	kubeTestProtocol  = "test-stream"
	kubeTestSeenCred  = "X-Test-Seen-Credential"
)

func startKubeUpstream(t *testing.T) *kubeUpstream {
	t.Helper()
	u := &kubeUpstream{
		closed:  map[string]chan struct{}{},
		started: make(chan string, 64),
		next:    make(chan struct{}),
		gone:    make(chan string, 64),
	}
	u.srv = httptest.NewUnstartedServer(http.HandlerFunc(u.handle))
	u.srv.Config.ConnState = func(c net.Conn, state http.ConnState) {
		addr := c.RemoteAddr().String()
		u.mu.Lock()
		defer u.mu.Unlock()
		switch state {
		case http.StateNew:
			u.closed[addr] = make(chan struct{})
		case http.StateClosed:
			if ch, ok := u.closed[addr]; ok {
				select {
				case <-ch:
				default:
					close(ch)
				}
			}
		}
	}
	u.srv.StartTLS()
	t.Cleanup(u.srv.Close)
	return u
}

func (u *kubeUpstream) handle(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	u.mu.Lock()
	u.calls = append(u.calls, kubeCall{r.Method, r.URL.Path, r.URL.RawQuery, r.Host, r.RemoteAddr, r.Header.Clone(), body})
	u.mu.Unlock()
	switch r.URL.Path {
	case kubeTestHangPath:
		// Never answers: holds the request until its connection ends.
		u.started <- r.URL.Path
		<-r.Context().Done()
		u.gone <- r.URL.Path
	case kubeTestWatchPath:
		// One event at once, a second when the test says, then open until
		// the connection ends.
		fmt.Fprintln(w, "event-1")
		w.(http.Flusher).Flush()
		u.started <- r.URL.Path
		select {
		case <-u.next:
			fmt.Fprintln(w, "event-2")
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		case <-r.Context().Done():
		}
		u.gone <- r.URL.Path
	case kubeTestExecPath:
		conn, rw, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer conn.Close()
		fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: %s\r\n\r\n", kubeTestProtocol)
		_ = rw.Flush()
		u.started <- r.URL.Path
		// Echo lines until the connection ends.
		for {
			line, err := rw.ReadString('\n')
			if err != nil {
				break
			}
			_, _ = rw.WriteString(line)
			_ = rw.Flush()
		}
		u.gone <- r.URL.Path
	default:
		w.Header().Set(kubeTestSeenCred, r.Header.Get(taskCredentialHeader))
		_, _ = io.WriteString(w, "ok")
	}
}

func (u *kubeUpstream) seen() []kubeCall {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]kubeCall(nil), u.calls...)
}

// connClosed is closed when the upstream has seen the connection end.
func (u *kubeUpstream) connClosed(t *testing.T, addr string) <-chan struct{} {
	t.Helper()
	u.mu.Lock()
	defer u.mu.Unlock()
	ch, ok := u.closed[addr]
	if !ok {
		t.Fatalf("the upstream never saw a connection from %s", addr)
	}
	return ch
}

// caFileFor writes the upstream's certificate as a CA file.
func (u *kubeUpstream) caFileFor(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "ca.crt")
	data := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: u.srv.Certificate().Raw})
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func await[T any](t *testing.T, ch <-chan T, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(testWait):
		t.Fatalf("timed out waiting for %s", what)
		panic("unreachable")
	}
}

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// clusterCredential is a value shaped like a task credential whose claims
// name a cluster. Nothing here verifies it.
func clusterCredential(cluster string) string {
	claims := fmt.Sprintf(`{"nats":{"target":{"project":"proj","location":"us-central1","cluster":%q}}}`, cluster)
	return "hdr." + base64.RawURLEncoding.EncodeToString([]byte(claims)) + ".sig"
}

// reportCredential is shaped like a task credential whose claims name no
// cluster.
func reportCredential() string {
	return "hdr." + base64.RawURLEncoding.EncodeToString([]byte(`{"nats":{"tier":"unattended"}}`)) + ".sig"
}

type kubeLoopbackFixture struct {
	up        *kubeUpstream
	p         *KubeProxyLoopback
	tokenFile string
	client    *http.Client
}

func startKubeLoopback(t *testing.T) *kubeLoopbackFixture {
	t.Helper()
	up := startKubeUpstream(t)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("pod-token-1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	p, err := StartKubeProxyLoopback(KubeProxyLoopbackConfig{Upstream: up.srv.URL, CAFile: up.caFileFor(t), PodTokenFile: tokenFile}, discardLogger())
	if err != nil {
		t.Fatal(err)
	}
	// Registered after the upstream's, so it runs first: the loopback's
	// connections end before the upstream waits for its own.
	t.Cleanup(func() { _ = p.Close() })
	transport := &http.Transport{}
	t.Cleanup(transport.CloseIdleConnections)
	return &kubeLoopbackFixture{up: up, p: p, tokenFile: tokenFile, client: &http.Client{Transport: transport}}
}

// get is one GET through the loopback, its status and body.
func (f *kubeLoopbackFixture) get(t *testing.T, path string) (int, string, http.Header) {
	t.Helper()
	resp, err := f.client.Get(f.p.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body), resp.Header
}

// TestTheKubeProxyLoopbackListensOnLoopbackOnly: the listener is 127.0.0.1
// on a port of the kernel's choosing.
func TestTheKubeProxyLoopbackListensOnLoopbackOnly(t *testing.T) {
	f := startKubeLoopback(t)
	host, port, err := net.SplitHostPort(strings.TrimPrefix(f.p.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	if host != "127.0.0.1" || port == "0" || port == "" {
		t.Errorf("the loopback listens at %s, want 127.0.0.1 on an ephemeral port", f.p.URL)
	}
}

// TestTheKubeProxyLoopbackReplacesWhoIsCalling: whatever the caller says
// about who it is never reaches kube-proxy; the pod's token and the active
// task's credential do, once each; the method, path, query and body pass as
// they came; and a rotated pod token is the one the next request carries.
func TestTheKubeProxyLoopbackReplacesWhoIsCalling(t *testing.T) {
	f := startKubeLoopback(t)
	credential := clusterCredential("alpha")
	end := f.p.BeginTask("task-a", credential)
	defer end()

	const query = "labelSelector=app%3Dweb&limit=5"
	req, err := http.NewRequest(http.MethodPost, f.p.URL+kubeTestEchoPath+"?"+query, strings.NewReader(`{"kind":"Pod"}`))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer the-agents-own")
	req.Header.Add(taskCredentialHeader, clusterCredential("forged"))
	req.Header.Add(taskCredentialHeader, clusterCredential("forged-again"))
	req.Header.Set("Impersonate-User", "system:admin")
	req.Header.Set("Impersonate-Group", "system:masters")
	req.Header.Set("Impersonate-Extra-Scopes", "all")
	req.Header.Set("X-Forwarded-For", "10.0.0.1")
	req.Header.Set("X-Forwarded-Host", "elsewhere")
	req.Header.Set("X-Forwarded-Client-Cert", "cert")
	req.Header.Set("Forwarded", "for=10.0.0.1")
	req.Header.Set("Accept", "application/json;as=Table")
	resp, err := f.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d, want 200", resp.StatusCode)
	}
	calls := f.up.seen()
	if len(calls) != 1 {
		t.Fatalf("the upstream saw %d requests, want 1", len(calls))
	}
	c := calls[0]
	if c.method != http.MethodPost || c.path != kubeTestEchoPath || c.query != query || string(c.body) != `{"kind":"Pod"}` {
		t.Errorf("the upstream saw %s %s?%s with body %q, want the request as it came", c.method, c.path, c.query, c.body)
	}
	if got := c.hdr.Values("Authorization"); !slices.Equal(got, []string{"Bearer pod-token-1"}) {
		t.Errorf("Authorization upstream = %q, want the pod's token alone", got)
	}
	if got := c.hdr.Values(taskCredentialHeader); !slices.Equal(got, []string{credential}) {
		t.Errorf("the task credential upstream = %q, want the active task's alone", got)
	}
	for name := range c.hdr {
		if strings.HasPrefix(name, "Impersonate-") || strings.HasPrefix(name, "X-Forwarded-") || name == "Forwarded" {
			t.Errorf("%s reached the upstream", name)
		}
	}
	if got := c.hdr.Get("Accept"); got != "application/json;as=Table" {
		t.Errorf("Accept upstream = %q, want it passed through", got)
	}

	// The kubelet rotates the token; the next request carries the new one.
	if err := os.WriteFile(f.tokenFile, []byte("pod-token-2\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if status, _, _ := f.get(t, kubeTestEchoPath); status != http.StatusOK {
		t.Fatalf("status %d after the rotation, want 200", status)
	}
	if got := f.up.seen()[1].hdr.Get("Authorization"); got != "Bearer pod-token-2" {
		t.Errorf("Authorization upstream after the rotation = %q, want the rotated token", got)
	}
}

// TestTheKubeProxyLoopbackRefusesWithoutATaskOrACluster: with no task, with
// a task that holds no cluster credential, and with one whose credential
// names no cluster, the answer is a local 403 kubectl can print, and nothing
// is sent upstream. A path outside /clusters/ is a local 404 even with a
// task.
func TestTheKubeProxyLoopbackRefusesWithoutATaskOrACluster(t *testing.T) {
	f := startKubeLoopback(t)
	expectRefusal := func(when string, wantStatus int, wantMessage, path string) {
		t.Helper()
		status, body, hdr := f.get(t, path)
		if status != wantStatus {
			t.Errorf("%s: status %d, want %d", when, status, wantStatus)
		}
		if !strings.Contains(body, `"kind":"Status"`) || !strings.Contains(body, wantMessage) || hdr.Get("Content-Type") != "application/json" {
			t.Errorf("%s: body %q (%s), want a Status carrying %q", when, body, hdr.Get("Content-Type"), wantMessage)
		}
	}
	expectRefusal("no task", http.StatusForbidden, kubeProxyNoTaskMessage, kubeTestEchoPath)

	end := f.p.BeginTask("task-none", "")
	expectRefusal("a task with no credential", http.StatusForbidden, kubeProxyNoCredentialMessage, kubeTestEchoPath)
	end()

	end = f.p.BeginTask("task-report", reportCredential())
	expectRefusal("a report credential", http.StatusForbidden, kubeProxyNoCredentialMessage, kubeTestEchoPath)
	end()

	end = f.p.BeginTask("task-misshapen", "not a credential")
	expectRefusal("a misshapen credential", http.StatusForbidden, kubeProxyNoCredentialMessage, kubeTestEchoPath)
	end()

	end = f.p.BeginTask("task-a", clusterCredential("alpha"))
	defer end()
	for _, path := range []string{"/", "/api/v1/pods", "/clusters", "/version"} {
		expectRefusal("the path "+path, http.StatusNotFound, kubeProxyNotFoundMessage, path)
	}
	if calls := f.up.seen(); len(calls) != 0 {
		t.Errorf("the upstream saw %d requests, want none", len(calls))
	}
}

// TestATaskSwitchChangesTheCredentialAndTheConnections: a request under task
// A carries A's credential; once A has ended and B begun, a request carries
// B's, on a connection the upstream had not seen before, and the one A left
// idle is closed.
func TestATaskSwitchChangesTheCredentialAndTheConnections(t *testing.T) {
	f := startKubeLoopback(t)
	credA, credB := clusterCredential("alpha"), clusterCredential("beta")

	endA := f.p.BeginTask("task-a", credA)
	for range 2 {
		if status, _, _ := f.get(t, kubeTestEchoPath); status != http.StatusOK {
			t.Fatalf("status %d under task A, want 200", status)
		}
	}
	calls := f.up.seen()
	if calls[0].conn != calls[1].conn {
		t.Fatalf("task A's two requests came on %s and %s, want one kept-alive connection", calls[0].conn, calls[1].conn)
	}
	connA := calls[0].conn
	endA()
	await(t, f.up.connClosed(t, connA), "task A's idle connection to close")

	if status, _, _ := f.get(t, kubeTestEchoPath); status != http.StatusForbidden {
		t.Errorf("status %d between tasks, want 403", status)
	}

	endB := f.p.BeginTask("task-b", credB)
	defer endB()
	if status, _, _ := f.get(t, kubeTestEchoPath); status != http.StatusOK {
		t.Fatalf("status %d under task B, want 200", status)
	}
	calls = f.up.seen()
	if len(calls) != 3 {
		t.Fatalf("the upstream saw %d requests, want 3", len(calls))
	}
	for i, want := range []string{credA, credA, credB} {
		if got := calls[i].hdr.Values(taskCredentialHeader); !slices.Equal(got, []string{want}) {
			t.Errorf("request %d carried %q, want %q", i, got, want)
		}
	}
	if calls[2].conn == connA {
		t.Errorf("task B's request reused task A's connection %s", connA)
	}
}

// TestARequestInFlightEndsWithItsTask: a request kube-proxy has not answered
// when its task ends is aborted, on both sides, before the task's end
// returns.
func TestARequestInFlightEndsWithItsTask(t *testing.T) {
	f := startKubeLoopback(t)
	end := f.p.BeginTask("task-a", clusterCredential("alpha"))
	type result struct {
		status int
		err    error
	}
	done := make(chan result, 1)
	go func() {
		resp, err := f.client.Get(f.p.URL + kubeTestHangPath)
		if err != nil {
			done <- result{err: err}
			return
		}
		resp.Body.Close()
		done <- result{status: resp.StatusCode}
	}()
	await(t, f.up.started, "the request to reach the upstream")
	end()
	if got := await(t, done, "the caller's request to end"); got.err == nil && got.status != http.StatusBadGateway {
		t.Errorf("the request in flight ended with status %d, want it aborted", got.status)
	}
	await(t, f.up.gone, "the upstream to see the request's connection end")
}

// TestAWatchStreamsAndEndsWithItsTask: each event reaches the caller as the
// upstream flushes it, while the response is still open, and when the task
// ends the stream is cut rather than ended cleanly.
func TestAWatchStreamsAndEndsWithItsTask(t *testing.T) {
	f := startKubeLoopback(t)
	end := f.p.BeginTask("task-a", clusterCredential("alpha"))
	resp, err := f.client.Get(f.p.URL + kubeTestWatchPath + "?watch=true")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	br := bufio.NewReader(resp.Body)
	readLine := func() (string, error) {
		type line struct {
			text string
			err  error
		}
		got := make(chan line, 1)
		go func() {
			text, err := br.ReadString('\n')
			got <- line{text, err}
		}()
		l := await(t, got, "a line of the watch")
		return l.text, l.err
	}
	// The upstream is holding the response open, waiting for the test,
	// while the first event is read: nothing waited for the body to end.
	if text, err := readLine(); err != nil || text != "event-1\n" {
		t.Fatalf("the first event = %q, %v", text, err)
	}
	await(t, f.up.started, "the watch to reach the upstream")
	f.up.next <- struct{}{}
	if text, err := readLine(); err != nil || text != "event-2\n" {
		t.Fatalf("the second event = %q, %v", text, err)
	}
	end()
	if text, err := readLine(); err == nil || errors.Is(err, io.EOF) {
		t.Errorf("after the task ended the watch read %q, %v; want the stream cut", text, err)
	}
	await(t, f.up.gone, "the upstream to see the watch's connection end")
}

// TestAnUpgradedConnectionEndsWithItsTask: a protocol upgrade (exec,
// port-forward) passes through with the proxy's own credentials, carries
// bytes both ways, and is closed on both sides when the task ends.
func TestAnUpgradedConnectionEndsWithItsTask(t *testing.T) {
	f := startKubeLoopback(t)
	credential := clusterCredential("alpha")
	end := f.p.BeginTask("task-a", credential)
	addr := strings.TrimPrefix(f.p.URL, "http://")
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(testWait)); err != nil {
		t.Fatal(err)
	}
	fmt.Fprintf(conn, "POST %s?command=sh HTTP/1.1\r\nHost: %s\r\nConnection: Upgrade\r\nUpgrade: %s\r\nAuthorization: Bearer the-agents-own\r\n\r\n", kubeTestExecPath, addr, kubeTestProtocol)
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("status %d, want 101", resp.StatusCode)
	}
	await(t, f.up.started, "the upgrade to reach the upstream")
	if _, err := io.WriteString(conn, "ping\n"); err != nil {
		t.Fatal(err)
	}
	if line, err := br.ReadString('\n'); err != nil || line != "ping\n" {
		t.Fatalf("the upgraded connection echoed %q, %v", line, err)
	}
	c := f.up.seen()[0]
	if got := c.hdr.Values("Authorization"); !slices.Equal(got, []string{"Bearer pod-token-1"}) {
		t.Errorf("Authorization on the upgrade = %q, want the pod's token alone", got)
	}
	if got := c.hdr.Values(taskCredentialHeader); !slices.Equal(got, []string{credential}) {
		t.Errorf("the task credential on the upgrade = %q, want the active task's alone", got)
	}

	end()
	await(t, f.up.gone, "the upstream to see the upgraded connection end")
	if line, err := br.ReadString('\n'); err == nil {
		t.Errorf("the upgraded connection still read %q after its task ended", line)
	} else if ne, ok := err.(net.Error); ok && ne.Timeout() {
		t.Errorf("the upgraded connection was left open after its task ended")
	}
}

// TestNoRequestCarriesAnotherTasksCredential hammers the loopback while
// tasks begin and end. Every answered request names the credential it was
// sent with; that task must have begun before the request returned and must
// not have finished ending before the request was made.
func TestNoRequestCarriesAnotherTasksCredential(t *testing.T) {
	const (
		tasks           = 40
		callers         = 8
		requestsPerTask = 6
	)
	f := startKubeLoopback(t)
	index := map[string]int64{}
	credentials := make([]string, tasks+1)
	for i := 1; i <= tasks; i++ {
		credentials[i] = clusterCredential(fmt.Sprintf("c%d", i))
		index[credentials[i]] = int64(i)
	}
	// begun is the number of tasks whose begin has been called; ended the
	// number whose end has returned.
	var begun, ended atomic.Int64
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	completed := make(chan struct{}, callers)
	var wg sync.WaitGroup
	for range callers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for ctx.Err() == nil {
				endedBefore := ended.Load()
				req, _ := http.NewRequestWithContext(ctx, http.MethodGet, f.p.URL+kubeTestEchoPath, nil)
				// A credential of the caller's own, which must never count.
				req.Header.Set(taskCredentialHeader, credentials[1])
				resp, err := f.client.Do(req)
				if err != nil {
					continue
				}
				_, _ = io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				begunAfter := begun.Load()
				switch resp.StatusCode {
				case http.StatusOK:
					task, known := index[resp.Header.Get(kubeTestSeenCred)]
					switch {
					case !known:
						t.Errorf("a request carried %q, which is no task's credential", resp.Header.Get(kubeTestSeenCred))
					case task > begunAfter:
						t.Errorf("a request carried task %d's credential before that task began (%d begun)", task, begunAfter)
					case task <= endedBefore:
						t.Errorf("a request carried task %d's credential after that task ended (%d ended before it was made)", task, endedBefore)
					}
				case http.StatusForbidden, http.StatusBadGateway:
					// Between tasks, or cut by a task's end.
				default:
					t.Errorf("status %d", resp.StatusCode)
				}
				select {
				case completed <- struct{}{}:
				case <-ctx.Done():
				}
			}
		}()
	}
	for i := 1; i <= tasks; i++ {
		begun.Add(1)
		end := f.p.BeginTask(fmt.Sprintf("task-%d", i), credentials[i])
		for range requestsPerTask {
			await(t, completed, "a caller's request to return")
		}
		end()
		ended.Add(1)
	}
	cancel()
	wg.Wait()
	// The upstream's own record: every request it saw carried exactly one
	// credential, a task's.
	for _, c := range f.up.seen() {
		got := c.hdr.Values(taskCredentialHeader)
		if len(got) != 1 || index[got[0]] == 0 {
			t.Errorf("the upstream saw the task credential %q, want one task's", got)
		}
	}
}

// TestWithoutAKubeProxyNothingStarts: a pod that names no kube-proxy gets no
// loopback, and a task's environment is left exactly as it was.
func TestWithoutAKubeProxyNothingStarts(t *testing.T) {
	for _, cfg := range []KubeProxyLoopbackConfig{
		{},
		{CAFile: "/var/run/ca.crt", PodTokenFile: "/var/run/token"},
		{Upstream: "  "},
	} {
		p, err := startKubeProxyLoopback(cfg, discardLogger())
		if p != nil || err != nil {
			t.Errorf("startKubeProxyLoopback(%+v) = %v, %v; want nothing started", cfg, p, err)
		}
	}
	w := &profileWorker{}
	env := []string{"PATH=/bin", "CREDENTIAL_PROXY_URL=http://broker", "KUBE_PROXY_HOST_CONTEXT=gke_p_l_c"}
	cfg := Config{TaskID: "task-a", HarnessEnv: slices.Clone(env)}
	end := w.beginKubeProxyTask(&cfg, &held{proxyToken: clusterCredential("alpha")})
	end()
	if !slices.Equal(cfg.HarnessEnv, env) {
		t.Errorf("the task's environment = %q, want it unchanged: %q", cfg.HarnessEnv, env)
	}
}

// TestAnUnusableKubeProxyStopsTheWorker: a kube-proxy the pod names but the
// loopback can't use is an error at start that says which value is wrong.
func TestAnUnusableKubeProxyStopsTheWorker(t *testing.T) {
	up := startKubeUpstream(t)
	ca := up.caFileFor(t)
	dir := t.TempDir()
	token := filepath.Join(dir, "token")
	garbage := filepath.Join(dir, "garbage.crt")
	for _, path := range []string{token, garbage} {
		if err := os.WriteFile(path, []byte("not a certificate\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for name, tc := range map[string]struct {
		cfg  KubeProxyLoopbackConfig
		want string
	}{
		"plain http":        {KubeProxyLoopbackConfig{Upstream: "http://kube-proxy:8443", CAFile: ca, PodTokenFile: token}, EnvKubeProxyURL},
		"no scheme":         {KubeProxyLoopbackConfig{Upstream: "kube-proxy:8443", CAFile: ca, PodTokenFile: token}, EnvKubeProxyURL},
		"credentials":       {KubeProxyLoopbackConfig{Upstream: "https://u:p@kube-proxy:8443", CAFile: ca, PodTokenFile: token}, EnvKubeProxyURL},
		"a query":           {KubeProxyLoopbackConfig{Upstream: "https://kube-proxy:8443/?x=1", CAFile: ca, PodTokenFile: token}, EnvKubeProxyURL},
		"no CA file named":  {KubeProxyLoopbackConfig{Upstream: up.srv.URL, PodTokenFile: token}, EnvKubeProxyCAFile},
		"a missing CA file": {KubeProxyLoopbackConfig{Upstream: up.srv.URL, CAFile: filepath.Join(dir, "absent.crt"), PodTokenFile: token}, EnvKubeProxyCAFile},
		"a CA file of junk": {KubeProxyLoopbackConfig{Upstream: up.srv.URL, CAFile: garbage, PodTokenFile: token}, EnvKubeProxyCAFile},
		"no token file":     {KubeProxyLoopbackConfig{Upstream: up.srv.URL, CAFile: ca}, EnvCredentialProxyTokenFile},
	} {
		p, err := startKubeProxyLoopback(tc.cfg, discardLogger())
		if p != nil {
			_ = p.Close()
		}
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: startKubeProxyLoopback = %v, want an error naming %s", name, err, tc.want)
		}
	}

	// The worker itself refuses to start, before it reaches the bus.
	cfg := profileWorkerConfig(t, startServer(t), stub(t, "exit 0"))
	err := RunProfileWorker(context.Background(), cfg, ProfileWorkerConfig{
		Tier:      pwTier,
		KubeProxy: KubeProxyLoopbackConfig{Upstream: "http://kube-proxy:8443", CAFile: ca, PodTokenFile: token},
	})
	if err == nil || !strings.Contains(err.Error(), EnvKubeProxyURL) {
		t.Errorf("a worker with a plain-http kube-proxy = %v, want refused at start", err)
	}
}

// TestATasksHarnessIsPointedAtTheLoopback: with a loopback, a task's harness
// and its tool children get the loopback's URL where the pod's kube-proxy
// was, no CA file, and the task's target cluster as the context a bare
// kubectl reads; the upstream URL is nowhere in that environment; and the
// task's end is the loopback's too.
func TestATasksHarnessIsPointedAtTheLoopback(t *testing.T) {
	f := startKubeLoopback(t)
	upstream := f.up.srv.URL
	w := &profileWorker{kubeProxy: f.p}
	w.cfg.HarnessEnv = []string{
		"PATH=/bin",
		EnvKubeProxyURL + "=" + upstream,
		EnvKubeProxyCAFile + "=/var/run/secrets/ca.crt",
		envKubeProxyHostContext + "=gke_proj_us-central1_host",
	}
	cfg := Config{TaskID: "task-a", HarnessEnv: slices.Clone(w.cfg.HarnessEnv)}
	end := w.beginKubeProxyTask(&cfg, &held{proxyToken: clusterCredential("alpha")})
	want := []string{"PATH=/bin", EnvKubeProxyURL + "=" + f.p.URL, envKubeProxyHostContext + "=gke_proj_us-central1_alpha"}
	if !slices.Equal(cfg.HarnessEnv, want) {
		t.Errorf("the task's environment = %q, want %q", cfg.HarnessEnv, want)
	}
	if !slices.Equal(toolChildEnv(cfg.HarnessEnv), want) {
		t.Errorf("the tool children's environment = %q, want %q", toolChildEnv(cfg.HarnessEnv), want)
	}
	for _, kv := range cfg.HarnessEnv {
		if strings.Contains(kv, upstream) {
			t.Errorf("%q hands the task the upstream URL", kv)
		}
	}
	if status, _, _ := f.get(t, kubeTestEchoPath); status != http.StatusOK {
		t.Errorf("status %d during the task, want 200", status)
	}
	end()
	if status, _, _ := f.get(t, kubeTestEchoPath); status != http.StatusForbidden {
		t.Errorf("status %d after the task, want 403", status)
	}

	// A task with no target gets no context, the pod's own included.
	cfg = Config{TaskID: "task-b", HarnessEnv: slices.Clone(w.cfg.HarnessEnv)}
	end = w.beginKubeProxyTask(&cfg, &held{})
	defer end()
	if want := []string{"PATH=/bin", EnvKubeProxyURL + "=" + f.p.URL}; !slices.Equal(cfg.HarnessEnv, want) {
		t.Errorf("a targetless task's environment = %q, want %q", cfg.HarnessEnv, want)
	}
}

// TestTheTargetContextComesFromTheCredentialsClaims: the context is built
// from a credential's target, and only from parts a GKE context can hold.
func TestTheTargetContextComesFromTheCredentialsClaims(t *testing.T) {
	encode := func(claims string) string {
		return "hdr." + base64.RawURLEncoding.EncodeToString([]byte(claims)) + ".sig"
	}
	for credential, want := range map[string]string{
		clusterCredential("alpha"): "gke_proj_us-central1_alpha",
		reportCredential():         "",
		"":                         "",
		"one.two":                  "",
		"hdr.!!!.sig":              "",
		encode(`{"nats":{"target":{"project":"proj","location":"us-central1","cluster":"a_b"}}}`):  "",
		encode(`{"nats":{"target":{"project":"proj","location":"us-central1","cluster":"a\nb"}}}`): "",
		encode(`{"nats":{"target":{"project":"","location":"us-central1","cluster":"alpha"}}}`):    "",
	} {
		if got := taskTargetContext(credential); got != want {
			t.Errorf("taskTargetContext(%q) = %q, want %q", credential, got, want)
		}
	}
}

// TestARotatedCAIsReadAgain: the CA file is read again when it changes, and
// a file caught unreadable keeps the last certificates that loaded.
func TestARotatedCAIsReadAgain(t *testing.T) {
	up := startKubeUpstream(t)
	path := up.caFileFor(t)
	good, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	ca := &caFile{path: path}
	first, err := ca.pool()
	if err != nil {
		t.Fatal(err)
	}
	if again, err := ca.pool(); err != nil || again != first {
		t.Errorf("an unchanged file was read again: %v", err)
	}
	// Half-written: the last pool stands.
	if err := os.WriteFile(path, []byte("junk"), 0o600); err != nil {
		t.Fatal(err)
	}
	if kept, err := ca.pool(); err != nil || kept != first {
		t.Errorf("a file of junk gave %v, want the last certificates kept", err)
	}
	// Rotated: a new pool.
	if err := os.WriteFile(path, append(slices.Clone(good), '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	if rotated, err := ca.pool(); err != nil || rotated == first {
		t.Errorf("a rotated file was not read again: %v", err)
	}
}

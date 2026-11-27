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

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// testWait bounds every wait on a channel in these tests: long enough that
// only a hang reaches it.
const testWait = 20 * time.Second

const (
	// kubeTestStallAfter is how long the flood's upstream lets one write
	// wait before it takes the response as backed up.
	kubeTestStallAfter = time.Second
	// kubeTestShortDrain stands in for kubeProxyDrainTimeout where a test
	// leaves a handler stuck.
	kubeTestShortDrain = 50 * time.Millisecond
	// kubeTestOrderProbe is how long a task's end is held to see whether its
	// outcome was reported first.
	kubeTestOrderProbe = time.Second
)

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
	hdr, trailer                    http.Header
	body                            []byte
}

const (
	kubeTestCluster   = "/clusters/gke_proj_us-central1_alpha"
	kubeTestFloodPath = kubeTestCluster + "/flood"
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
	u.calls = append(u.calls, kubeCall{r.Method, r.URL.Path, r.URL.RawQuery, r.Host, r.RemoteAddr, r.Header.Clone(), r.Trailer.Clone(), body})
	u.mu.Unlock()
	switch r.URL.Path {
	case kubeTestHangPath:
		// Never answers: holds the request until its connection ends.
		u.started <- r.URL.Path
		<-r.Context().Done()
		u.gone <- r.URL.Path
	case kubeTestFloodPath:
		// Writes without end. A write that waits kubeTestStallAfter means
		// nothing downstream is reading; then it holds the request open.
		rc := http.NewResponseController(w)
		chunk := make([]byte, 64<<10)
		for {
			_ = rc.SetWriteDeadline(time.Now().Add(kubeTestStallAfter))
			if _, err := w.Write(chunk); err != nil {
				break
			}
			if err := rc.Flush(); err != nil {
				break
			}
		}
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
	return taskClusterCredential(cluster, "task")
}

// taskClusterCredential is clusterCredential for a named task, so two tasks
// on one cluster hold different credentials.
func taskClusterCredential(cluster, task string) string {
	claims := fmt.Sprintf(`{"nats":{"task":%q,"target":{"project":"proj","location":"us-central1","cluster":%q}}}`, task, cluster)
	return "hdr." + base64.RawURLEncoding.EncodeToString([]byte(claims)) + ".sig"
}

// rawRequest writes request to the loopback as it is, and returns the
// status and body of the answer.
func rawRequest(t *testing.T, addr, request string) (int, string) {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(testWait)); err != nil {
		t.Fatal(err)
	}
	if _, err := io.WriteString(conn, request); err != nil {
		t.Fatal(err)
	}
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
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
	// addr is the loopback's host:port; logs is what it logged.
	addr string
	logs *lockedBuffer
}

func startKubeLoopback(t *testing.T) *kubeLoopbackFixture {
	t.Helper()
	up := startKubeUpstream(t)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("pod-token-1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	logs := &lockedBuffer{}
	p, err := StartKubeProxyLoopback(KubeProxyLoopbackConfig{Upstream: up.srv.URL, CAFile: up.caFileFor(t), PodTokenFile: tokenFile}, slog.New(slog.NewTextHandler(logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	// Registered after the upstream's, so it runs first: the loopback's
	// connections end before the upstream waits for its own.
	t.Cleanup(func() { _ = p.Close() })
	transport := &http.Transport{}
	t.Cleanup(transport.CloseIdleConnections)
	return &kubeLoopbackFixture{up: up, p: p, tokenFile: tokenFile, client: &http.Client{Transport: transport}, addr: strings.TrimPrefix(p.URL, "http://"), logs: logs}
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
	_, end := f.p.BeginTask("task-a", credential)
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

	_, end := f.p.BeginTask("task-none", "")
	expectRefusal("a task with no credential", http.StatusForbidden, kubeProxyNoCredentialMessage, kubeTestEchoPath)
	end()

	_, end = f.p.BeginTask("task-report", reportCredential())
	expectRefusal("a report credential", http.StatusForbidden, kubeProxyNoCredentialMessage, kubeTestEchoPath)
	end()

	_, end = f.p.BeginTask("task-misshapen", "not a credential")
	expectRefusal("a misshapen credential", http.StatusForbidden, kubeProxyUnaddressedMessage, kubeTestEchoPath)
	end()

	_, end = f.p.BeginTask("task-a", clusterCredential("alpha"))
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
	credA, credB := taskClusterCredential("alpha", "task-a"), taskClusterCredential("alpha", "task-b")

	_, endA := f.p.BeginTask("task-a", credA)
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

	_, endB := f.p.BeginTask("task-b", credB)
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
	_, end := f.p.BeginTask("task-a", clusterCredential("alpha"))
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
	_, end := f.p.BeginTask("task-a", clusterCredential("alpha"))
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
	_, end := f.p.BeginTask("task-a", credential)
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
		credentials[i] = taskClusterCredential("alpha", fmt.Sprintf("task-%d", i))
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
		_, end := f.p.BeginTask(fmt.Sprintf("task-%d", i), credentials[i])
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

	// A refused value is never printed whole: it can carry credentials.
	for _, upstream := range []string{
		"https://user:s3cr3t-in-userinfo@kube-proxy:8443",
		"http://user:s3cr3t-in-userinfo@kube-proxy:8443",
		"https://kube-proxy:8443/?token=s3cr3t-in-query",
		"https://user:s3cr3t-in-userinfo@kube-proxy:8443/%zz",
		"https://user:s3cr3t-in-userinfo@kube proxy:8443",
	} {
		_, err := startKubeProxyLoopback(KubeProxyLoopbackConfig{Upstream: upstream, CAFile: ca, PodTokenFile: token}, discardLogger())
		if err == nil {
			t.Errorf("startKubeProxyLoopback(%q) started", upstream)
			continue
		}
		for _, leaked := range []string{"s3cr3t", "user:", "token="} {
			if strings.Contains(err.Error(), leaked) {
				t.Errorf("the error for a refused URL prints %q: %v", leaked, err)
			}
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

// claimsCredential is a value shaped like a task credential with the given
// claims.
func claimsCredential(claims string) string {
	return "hdr." + base64.RawURLEncoding.EncodeToString([]byte(claims)) + ".sig"
}

// TestTheTargetContextComesFromTheCredentialsClaims: the context is built
// from a credential's target, and only from parts a GKE context can hold. A
// credential that isn't known to be without a cluster counts as having one.
func TestTheTargetContextComesFromTheCredentialsClaims(t *testing.T) {
	type reading struct {
		context  string
		targeted bool
	}
	for credential, want := range map[string]reading{
		clusterCredential("alpha"): {"gke_proj_us-central1_alpha", true},
		reportCredential():         {"", false},
		"":                         {"", true},
		"one.two":                  {"", true},
		"hdr.!!!.sig":              {"", true},
		claimsCredential(`{"nats":{"target":{"project":"proj","location":"us-central1","cluster":"a_b"}}}`):           {"", true},
		claimsCredential(`{"nats":{"target":{"project":"proj","location":"us-central1","cluster":"a\nb"}}}`):          {"", true},
		claimsCredential(`{"nats":{"target":{"project":"","location":"us-central1","cluster":"alpha"}}}`):             {"", true},
		claimsCredential(`{"nats":{"target":{"project":"example.com:proj","location":"us-central1","cluster":"a"}}}`): {"", true},
	} {
		if context, targeted := taskTargetContext(credential); context != want.context || targeted != want.targeted {
			t.Errorf("taskTargetContext(%q) = %q, %v; want %q, %v", credential, context, targeted, want.context, want.targeted)
		}
	}
}

// TestAClusterThisPathCantAddressIsSaidSo: a credential that names a cluster
// whose name doesn't fit a context (a domain-scoped project) leaves the task
// refused here, with a line in the log naming the task and not the
// credential, and an answer that doesn't claim the task has no cluster. A
// report credential, which has none, is told that, and logs nothing.
func TestAClusterThisPathCantAddressIsSaidSo(t *testing.T) {
	f := startKubeLoopback(t)
	const logged = "can't address"
	credential := claimsCredential(`{"nats":{"target":{"project":"example.com:proj","location":"us-central1","cluster":"alpha"}}}`)
	target, end := f.p.BeginTask("task-domain-scoped", credential)
	if target != "" {
		t.Errorf("the task is held to %q, want no context", target)
	}
	status, body, _ := f.get(t, kubeTestEchoPath)
	if status != http.StatusForbidden || !strings.Contains(body, kubeProxyUnaddressedMessage) || strings.Contains(body, kubeProxyNoCredentialMessage) {
		t.Errorf("status %d, body %q; want a 403 saying the cluster can't be addressed", status, body)
	}
	end()
	logs := f.logs.String()
	if strings.Count(logs, logged) != 1 || !strings.Contains(logs, "task-domain-scoped") {
		t.Errorf("want one line naming the task:\n%s", logs)
	}
	if strings.Contains(logs, credential) || strings.Contains(body, credential) {
		t.Errorf("the credential is in the log or the answer:\n%s\n%s", logs, body)
	}

	target, end = f.p.BeginTask("task-report", reportCredential())
	defer end()
	if target != "" {
		t.Errorf("a report task is held to %q, want no context", target)
	}
	if status, body, _ := f.get(t, kubeTestEchoPath); status != http.StatusForbidden || !strings.Contains(body, kubeProxyNoCredentialMessage) {
		t.Errorf("a report task: status %d, body %q; want a 403 saying it has no cluster", status, body)
	}
	if got := strings.Count(f.logs.String(), logged); got != 1 {
		t.Errorf("a report credential was logged as unaddressable:\n%s", f.logs)
	}
	if calls := f.up.seen(); len(calls) != 0 {
		t.Errorf("the upstream saw %d requests, want none", len(calls))
	}
}

// TestTheQueryPassesByteForByte: a query the reverse proxy's cleaner would
// rewrite (a ";" separator, a bad escape) reaches the upstream as sent.
func TestTheQueryPassesByteForByte(t *testing.T) {
	f := startKubeLoopback(t)
	_, end := f.p.BeginTask("task-a", clusterCredential("alpha"))
	defer end()
	for _, query := range []string{
		"watch=true;timeoutSeconds=5&labelSelector=app%3Dweb",
		"a=1;b=2",
		"fieldSelector=metadata.name%3Dx&bad=%zz&limit=5",
		"labelSelector=a+in+(b,c)&x",
	} {
		status, _ := rawRequest(t, f.addr, fmt.Sprintf("GET %s?%s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", kubeTestEchoPath, query, f.addr))
		if status != http.StatusOK {
			t.Fatalf("%s: status %d, want 200", query, status)
		}
		calls := f.up.seen()
		if got := calls[len(calls)-1].query; got != query {
			t.Errorf("the upstream saw the query %q, want %q", got, query)
		}
	}
}

// TestTheHarnessGetsTheContextTheLoopbackSettledOn: a credential that
// decodes to a target but is refused for its size gives the harness no host
// context, as the loopback holds the task to none.
func TestTheHarnessGetsTheContextTheLoopbackSettledOn(t *testing.T) {
	f := startKubeLoopback(t)
	oversize := taskClusterCredential("alpha", strings.Repeat("x", maxTaskCredentialBytes))
	if context, _ := taskTargetContext(oversize); context != "gke_proj_us-central1_alpha" {
		t.Fatalf("the oversize credential decodes to %q; the test needs one that names a cluster", context)
	}
	w := &profileWorker{kubeProxy: f.p}
	w.cfg.HarnessEnv = []string{"PATH=/bin", envKubeProxyHostContext + "=gke_proj_us-central1_host"}
	cfg := Config{TaskID: "task-oversize", HarnessEnv: slices.Clone(w.cfg.HarnessEnv)}
	end := w.beginKubeProxyTask(&cfg, &held{proxyToken: oversize})
	defer end()
	if want := []string{"PATH=/bin", EnvKubeProxyURL + "=" + f.p.URL}; !slices.Equal(cfg.HarnessEnv, want) {
		t.Errorf("the task's environment = %q, want no host context: %q", cfg.HarnessEnv, want)
	}
	if status, body, _ := f.get(t, kubeTestEchoPath); status != http.StatusForbidden || !strings.Contains(body, kubeProxyUnaddressedMessage) {
		t.Errorf("status %d, body %q; want the task refused here", status, body)
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
	logs := &lockedBuffer{}
	ca := &caFile{path: path, log: slog.New(slog.NewTextHandler(logs, nil))}
	const failed, recovered = "doesn't load", "loads again"
	logged := func(what string) int { return strings.Count(logs.String(), what) }
	first, err := ca.pool()
	if err != nil {
		t.Fatal(err)
	}
	if again, err := ca.pool(); err != nil || again != first {
		t.Errorf("an unchanged file was read again: %v", err)
	}
	// Half-written: the last pool stands, and it is said once however many
	// connections ask.
	if err := os.WriteFile(path, []byte("junk"), 0o600); err != nil {
		t.Fatal(err)
	}
	for range 3 {
		if kept, err := ca.pool(); err != nil || kept != first {
			t.Errorf("a file of junk gave %v, want the last certificates kept", err)
		}
	}
	if got := logged(failed); got != 1 {
		t.Errorf("%d lines for one failing version of the file, want 1:\n%s", got, logs)
	}
	if !strings.Contains(logs.String(), path) {
		t.Errorf("the line doesn't name the file:\n%s", logs)
	}
	// Another failing version is said once more.
	if err := os.WriteFile(path, []byte("more junk"), 0o600); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if kept, err := ca.pool(); err != nil || kept != first {
			t.Errorf("a second file of junk gave %v, want the last certificates kept", err)
		}
	}
	if got := logged(failed); got != 2 {
		t.Errorf("%d lines for two failing versions of the file, want 2:\n%s", got, logs)
	}
	if got := logged(recovered); got != 0 {
		t.Errorf("a recovery was logged while the file still fails:\n%s", logs)
	}
	// Rotated: a new pool, and the recovery is said once.
	if err := os.WriteFile(path, append(slices.Clone(good), '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	rotated, err := ca.pool()
	if err != nil || rotated == first {
		t.Errorf("a rotated file was not read again: %v", err)
	}
	if again, err := ca.pool(); err != nil || again != rotated {
		t.Errorf("the rotated file was read twice: %v", err)
	}
	if got := logged(recovered); got != 1 {
		t.Errorf("%d recovery lines, want 1:\n%s", got, logs)
	}
}

// TestATaskIsHeldToItsOwnCluster: a path that names any cluster but the
// task's target is answered 403 here, naming neither credential, and nothing
// is sent; the task's own cluster is forwarded.
func TestATaskIsHeldToItsOwnCluster(t *testing.T) {
	f := startKubeLoopback(t)
	credential := clusterCredential("alpha")
	_, end := f.p.BeginTask("task-a", credential)
	defer end()
	for _, path := range []string{
		"/clusters/gke_proj_us-central1_beta/api/v1/pods",
		"/clusters/gke_other_us-central1_alpha/api/v1/pods",
		"/clusters/gke_proj_us-central1_alpha-2/api/v1/pods",
		"/clusters/gke_proj_us-central1_alph/api/v1/pods",
		"/clusters/GKE_proj_us-central1_alpha/api/v1/pods",
		"/clusters/alpha/api/v1/pods",
	} {
		status, body, _ := f.get(t, path)
		if status != http.StatusForbidden || !strings.Contains(body, kubeProxyOtherClusterMessage) {
			t.Errorf("%s: status %d, body %q; want a local 403", path, status, body)
		}
		if strings.Contains(body, credential) || strings.Contains(body, "pod-token-1") {
			t.Errorf("%s: the refusal carries a credential: %q", path, body)
		}
	}
	if calls := f.up.seen(); len(calls) != 0 {
		t.Fatalf("the upstream saw %d requests for other clusters, want none", len(calls))
	}
	for _, path := range []string{kubeTestEchoPath, kubeTestCluster, kubeTestCluster + "/"} {
		if status, _, _ := f.get(t, path); status != http.StatusOK {
			t.Errorf("%s: status %d, want the task's own cluster forwarded", path, status)
		}
	}
	if calls := f.up.seen(); len(calls) != 3 {
		t.Errorf("the upstream saw %d requests for the task's cluster, want 3", len(calls))
	}
}

// kubeTestBentPaths are paths that climb, stand still, leave the cluster
// empty or spell it in an encoding, with the dots or the slash encoded too.
var kubeTestBentPaths = []string{
	"/clusters/../healthz",
	"/clusters/gke_proj_us-central1_alpha/../../x",
	"/clusters/gke_proj_us-central1_alpha/..",
	"/clusters/gke_proj_us-central1_alpha/./api",
	"/clusters/gke_proj_us-central1_alpha/api/v1/../../../../x",
	"//clusters/gke_proj_us-central1_alpha/echo",
	"/clusters//gke_proj_us-central1_alpha/echo",
	"/clusters//echo",
	"/clusters/%2e%2e/healthz",
	"/clusters/%2E%2E/healthz",
	"/clusters/.%2e/healthz",
	"/clusters/gke_proj_us-central1_alpha/%2e%2e/%2e%2e/x",
	"/clusters/gke_proj_us-central1_alpha/%2e/x",
	"/clusters/gke_proj_us-central1_alpha/a%2f..%2f..%2fx",
	"/clusters/gke_proj_us-central1_alpha/..%2fx",
	"/clusters/gke_proj_us-central1_alph%61/echo",
	"/clusters/gke_proj_us-central1_alpha%2fecho",
	"/clusters/gke_proj_us-central1_alpha%2f..%2fx",
	"/clusters/%67ke_proj_us-central1_alpha/echo",
}

// TestThePathGateReadsThePathAsItWasSent: the cluster is read from the
// escaped path, and a bent path names none.
func TestThePathGateReadsThePathAsItWasSent(t *testing.T) {
	for _, path := range kubeTestBentPaths {
		if cluster, ok := kubeProxyPathCluster(path); ok {
			t.Errorf("kubeProxyPathCluster(%q) = %q, want it refused", path, cluster)
		}
	}
	for path, want := range map[string]string{
		kubeTestEchoPath:                         "gke_proj_us-central1_alpha",
		kubeTestCluster:                          "gke_proj_us-central1_alpha",
		kubeTestCluster + "/":                    "gke_proj_us-central1_alpha",
		kubeTestCluster + "/api/v1/pods/a%20b":   "gke_proj_us-central1_alpha",
		kubeTestCluster + "/apis/x/v1/..hidden":  "gke_proj_us-central1_alpha",
		kubeTestCluster + "/api/v1/pods/a%2Fb/c": "gke_proj_us-central1_alpha",
		"/clusters/gke_other_europe-west1_beta":  "gke_other_europe-west1_beta",
	} {
		if cluster, ok := kubeProxyPathCluster(path); !ok || cluster != want {
			t.Errorf("kubeProxyPathCluster(%q) = %q, %v; want %q", path, cluster, ok, want)
		}
	}
}

// TestABentPathIsRefusedHere: each of those paths, sent as written, is a
// local 404 with a task active, and nothing reaches the upstream.
func TestABentPathIsRefusedHere(t *testing.T) {
	f := startKubeLoopback(t)
	_, end := f.p.BeginTask("task-a", clusterCredential("alpha"))
	defer end()
	for _, path := range kubeTestBentPaths {
		status, body := rawRequest(t, f.addr, fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", path, f.addr))
		if status != http.StatusNotFound || !strings.Contains(body, kubeProxyNotFoundMessage) {
			t.Errorf("%s: status %d, body %q; want a local 404", path, status, body)
		}
	}
	if calls := f.up.seen(); len(calls) != 0 {
		t.Errorf("the upstream saw %d requests, want none: %+v", len(calls), calls)
	}
}

// TestTheLoopbackIsNotAProxyForOtherHosts: a request for another Host, or in
// absolute form, is refused here even with a task active.
func TestTheLoopbackIsNotAProxyForOtherHosts(t *testing.T) {
	f := startKubeLoopback(t)
	_, end := f.p.BeginTask("task-a", clusterCredential("alpha"))
	defer end()
	for name, request := range map[string]string{
		"another Host":          fmt.Sprintf("GET %s HTTP/1.1\r\nHost: kube-proxy.example:8443\r\nConnection: close\r\n\r\n", kubeTestEchoPath),
		"the upstream's Host":   fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", kubeTestEchoPath, strings.TrimPrefix(f.up.srv.URL, "https://")),
		"absolute form":         fmt.Sprintf("GET http://elsewhere.example%s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", kubeTestEchoPath, f.addr),
		"absolute form to self": fmt.Sprintf("GET http://%s%s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", f.addr, kubeTestEchoPath, f.addr),
	} {
		status, body := rawRequest(t, f.addr, request)
		if status != http.StatusForbidden || !strings.Contains(body, kubeProxyNotProxyMessage) {
			t.Errorf("%s: status %d, body %q; want a local 403", name, status, body)
		}
	}
	if calls := f.up.seen(); len(calls) != 0 {
		t.Errorf("the upstream saw %d requests, want none", len(calls))
	}
}

// TestWithoutAPodTokenNothingIsSent: a pod token that can't be read, or is
// empty, is a local 503; no request goes upstream with an empty bearer.
func TestWithoutAPodTokenNothingIsSent(t *testing.T) {
	f := startKubeLoopback(t)
	_, end := f.p.BeginTask("task-a", clusterCredential("alpha"))
	defer end()
	expect503 := func(when string) {
		t.Helper()
		status, body, _ := f.get(t, kubeTestEchoPath)
		if status != http.StatusServiceUnavailable || !strings.Contains(body, kubeProxyNoPodTokenMessage) {
			t.Errorf("%s: status %d, body %q; want a local 503", when, status, body)
		}
	}
	for when, content := range map[string]string{"an empty token file": "", "a token file of whitespace": " \n\t\n"} {
		if err := os.WriteFile(f.tokenFile, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		expect503(when)
	}
	if err := os.Remove(f.tokenFile); err != nil {
		t.Fatal(err)
	}
	expect503("no token file")
	if calls := f.up.seen(); len(calls) != 0 {
		t.Errorf("the upstream saw %d requests, want none: %+v", len(calls), calls)
	}
	// The token back, the next request goes, with it.
	if err := os.WriteFile(f.tokenFile, []byte("pod-token-3\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if status, _, _ := f.get(t, kubeTestEchoPath); status != http.StatusOK {
		t.Fatalf("status %d with the token back, want 200", status)
	}
	for _, c := range f.up.seen() {
		if got := c.hdr.Values("Authorization"); !slices.Equal(got, []string{"Bearer pod-token-3"}) {
			t.Errorf("Authorization upstream = %q, want the pod's token", got)
		}
	}
}

// TestTrailersAndNominatedHeadersCantCarryACredential: a caller that sends
// Authorization or the task credential as a trailer, or names them in
// Connection so a proxy would drop them, still reaches the upstream with the
// proxy's own two headers and neither trailer.
func TestTrailersAndNominatedHeadersCantCarryACredential(t *testing.T) {
	f := startKubeLoopback(t)
	credential := clusterCredential("alpha")
	_, end := f.p.BeginTask("task-a", credential)
	defer end()
	request := "POST " + kubeTestEchoPath + " HTTP/1.1\r\n" +
		"Host: " + f.addr + "\r\n" +
		"Transfer-Encoding: chunked\r\n" +
		"Trailer: Authorization, " + taskCredentialHeader + ", Impersonate-User, X-Checksum\r\n" +
		"Connection: Authorization, " + taskCredentialHeader + ", close\r\n" +
		"Authorization: Bearer the-agents-own\r\n" +
		taskCredentialHeader + ": " + clusterCredential("forged") + "\r\n" +
		"\r\n" +
		"2\r\nhi\r\n0\r\n" +
		"Authorization: Bearer in-a-trailer\r\n" +
		taskCredentialHeader + ": " + clusterCredential("forged-trailer") + "\r\n" +
		"Impersonate-User: system:admin\r\n" +
		"X-Checksum: abc\r\n" +
		"\r\n"
	if status, body := rawRequest(t, f.addr, request); status != http.StatusOK {
		t.Fatalf("status %d, body %q; want 200", status, body)
	}
	calls := f.up.seen()
	if len(calls) != 1 {
		t.Fatalf("the upstream saw %d requests, want 1", len(calls))
	}
	c := calls[0]
	if string(c.body) != "hi" {
		t.Errorf("the body upstream = %q, want it as sent", c.body)
	}
	if got := c.hdr.Values("Authorization"); !slices.Equal(got, []string{"Bearer pod-token-1"}) {
		t.Errorf("Authorization upstream = %q, want the pod's token alone", got)
	}
	if got := c.hdr.Values(taskCredentialHeader); !slices.Equal(got, []string{credential}) {
		t.Errorf("the task credential upstream = %q, want the active task's alone", got)
	}
	for _, fields := range []http.Header{c.hdr, c.trailer} {
		for name, values := range fields {
			if strings.HasPrefix(name, "Impersonate-") {
				t.Errorf("%s reached the upstream", name)
			}
			for _, v := range values {
				if strings.Contains(v, "the-agents-own") || strings.Contains(v, "in-a-trailer") || v == clusterCredential("forged") || v == clusterCredential("forged-trailer") {
					t.Errorf("the caller's %s reached the upstream: %q", name, v)
				}
			}
		}
	}
	for _, name := range []string{"Authorization", taskCredentialHeader} {
		if _, sent := c.trailer[name]; sent {
			t.Errorf("the upstream was sent a %s trailer", name)
		}
	}
}

// TestACallerThatStopsReadingIsLeftBehindAtTheBound: a handler stuck writing
// to a caller that reads nothing doesn't hold its task's end past the drain
// bound, and its connection to the upstream is closed all the same.
func TestACallerThatStopsReadingIsLeftBehindAtTheBound(t *testing.T) {
	f := startKubeLoopback(t)
	f.p.drainTimeout = kubeTestShortDrain
	_, end := f.p.BeginTask("task-a", clusterCredential("alpha"))
	conn, err := net.Dial("tcp", f.addr)
	if err != nil {
		t.Fatal(err)
	}
	// Closed before the loopback's cleanup, which frees the stuck handler.
	defer conn.Close()
	// The request, and then nothing is read.
	fmt.Fprintf(conn, "GET %s HTTP/1.1\r\nHost: %s\r\n\r\n", kubeTestFloodPath, f.addr)
	await(t, f.up.started, "the response to back up")
	ended := make(chan struct{})
	go func() {
		end()
		close(ended)
	}()
	await(t, ended, "the task's end to give up on the stuck handler")
	if !strings.Contains(f.logs.String(), "still returning") {
		t.Errorf("the task ended without leaving a handler behind; the test didn't reach the bound:\n%s", f.logs)
	}
	await(t, f.up.gone, "the upstream to see the stuck request's connection end")
	if status, _, _ := f.get(t, kubeTestEchoPath); status != http.StatusForbidden {
		t.Errorf("status %d after the task, want 403", status)
	}
}

// TestATasksConnectionsEndBeforeItsOutcomeIsReported runs a task through the
// worker. The loopback's end of the task is held open for a moment, and no
// final renewal, which follows the outcome, may arrive meanwhile: the
// connections go before anything says the task is over.
func TestATasksConnectionsEndBeforeItsOutcomeIsReported(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	up := startKubeUpstream(t)
	podToken := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(podToken, []byte("pod-token-1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	const task = "task-order"
	reportedFirst := make(chan bool, 1)
	hook := func(id string) {
		if id != task {
			return
		}
		timer := time.NewTimer(kubeTestOrderProbe)
		defer timer.Stop()
		for {
			select {
			case r := <-d.leases:
				if r.TaskID == task && r.Final {
					reportedFirst <- true
					return
				}
			case <-timer.C:
				reportedFirst <- false
				return
			}
		}
	}
	harness := stub(t, `
read first || exit 1
printf '{"type":"result","subtype":"success","result":"done"}\n'
`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorkerWith(t, ctx, profileWorkerConfig(t, url, harness), ProfileWorkerConfig{
		Tier:                 pwTier,
		KubeProxy:            KubeProxyLoopbackConfig{Upstream: up.srv.URL, CAFile: up.caFileFor(t), PodTokenFile: podToken},
		kubeProxyTaskEndHook: hook,
	})
	d.offer(task, profileSubmission(t, task, "read the cluster", nil), 5)
	if a := d.nextAccept(5 * time.Second); a.TaskID != task || !a.Accepted {
		t.Fatalf("answer = %+v, want an accept", a)
	}
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	if await(t, reportedFirst, "the task's end at the loopback") {
		t.Fatal("the final renewal arrived while the task's connections were still open: the outcome was reported first")
	}
	if _, final := drainLeases(d, task, testWait); !final {
		t.Fatal("no final renewal after the task ended")
	}
}

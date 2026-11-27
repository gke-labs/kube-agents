package workeradapter

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

// The kube-proxy loopback carries a profile worker's kubectl from loopback
// to the install's kube-proxy. kube-proxy asks a worker for two things on
// every request: the pod's projected ServiceAccount token, and the cluster
// credential of the task the pod is running. A worker pod runs one task after
// another, so what attaches the second has to know which task is running.
// The adapter does, so it stands a reverse proxy on 127.0.0.1 between kubectl
// and kube-proxy:
//
//   - it sets both headers itself on every request and deletes whatever the
//     caller sent in their place, so a tool child can't supply a credential;
//   - the task credential comes from the adapter's memory (the dispatcher's
//     confirm), never from the task-token file, which the agent can write;
//   - a request is admitted under the task active at that moment and is sent
//     with that task's credential on that task's connections, and when the
//     task ends everything it opened is closed before the outcome is
//     reported: requests in flight, watches, upgraded connections and idle
//     keep-alives;
//   - with no task, or a task that holds no cluster credential, it answers
//     403 itself and sends nothing;
//   - it holds a task to its target cluster itself: a path that names any
//     other cluster is answered 403 here.
//
// It streams: nothing is buffered, capped or inspected, since a watch or a
// log follow runs for as long as the task does. It forwards only paths under
// kubeProxyPathPrefix in their plain form, to the one kube-proxy it was
// given, and nothing it logs comes from a header or a body.
//
// It starts only on a worker pod whose environment names a kube-proxy
// (EnvKubeProxyURL); without that, nothing listens and a task's environment
// is left as it is. A worker pod must be given that variable only once the
// kube-proxy it names admits the profile audience and requires the task
// credential: that check, of the credential's signature, pod and cluster, is
// what the path rests on, and the loopback's own is in front of it, not in
// its place. Until then kube-proxy refuses a worker's token.
const (
	// EnvKubeProxyURL and EnvKubeProxyCAFile are the pod's kube-proxy and the
	// CA that signed its certificate. A task's harness gets the loopback's
	// URL under the first and nothing under the second.
	EnvKubeProxyURL    = "KUBE_PROXY_URL"
	EnvKubeProxyCAFile = "KUBE_PROXY_CA_FILE"
	// envKubeProxyHostContext is the context the kubectl wrapper's broker
	// client makes current in a default kubeconfig it creates
	// (credential_proxy_client._ensure_host_kubeconfig_initialized). A task's
	// harness gets its target cluster's context here, so a kubectl that names
	// no context reads the task's cluster.
	envKubeProxyHostContext = "KUBE_PROXY_HOST_CONTEXT"

	// kubeProxyUpstreamScheme is the only scheme kube-proxy is reached over:
	// the request carries the pod's token.
	kubeProxyUpstreamScheme = "https"
	// kubeProxyPathPrefix starts every path kube-proxy serves:
	// /clusters/<context>/<the Kubernetes API path>.
	kubeProxyPathPrefix = "/clusters/"
	// kubeProxyListenAddr is loopback on a port the kernel picks.
	kubeProxyListenAddr    = "127.0.0.1:0"
	kubeProxyListenNetwork = "tcp"
	kubeProxyURLFormat     = "http://%s"

	kubeProxyAuthorizationHeader = "Authorization"
	kubeProxyBearerPrefix        = "Bearer "
	// Deleted from every request, on top of the two the proxy sets: a caller
	// names no one to act as, and nothing about the hop it came over.
	kubeProxyImpersonatePrefix = "Impersonate-"
	kubeProxyForwardedPrefix   = "X-Forwarded-"
	kubeProxyForwardedHeader   = "Forwarded"

	// kubeProxyDialTimeout bounds the TCP connect to kube-proxy, and again
	// the TLS handshake on it.
	kubeProxyDialTimeout = 10 * time.Second
	// kubeProxyResponseHeaderTimeout bounds the wait for kube-proxy's
	// response headers. A watch or a follow sends its headers at once and
	// then streams without a bound; a large list sends them when the API
	// server has the answer, which its own request timeout holds to a minute.
	kubeProxyResponseHeaderTimeout = 2 * time.Minute
	// kubeProxyHeaderTimeout bounds how long the proxy waits for a caller's
	// request headers. There is deliberately no server ReadTimeout or
	// WriteTimeout and no deadline on a request: either would cut a watch.
	kubeProxyHeaderTimeout = 10 * time.Second
	// kubeProxyIdleTimeout bounds a kept-alive connection between requests,
	// from a caller and to kube-proxy alike.
	kubeProxyIdleTimeout = time.Minute
	// kubeProxyIdleConns is how many kept-alive connections to kube-proxy a
	// task holds between requests. Each watch owns a connection while it
	// runs; these serve the short reads between.
	kubeProxyIdleConns = 8
	// kubeProxyMaxHeaderBytes bounds a request's headers.
	kubeProxyMaxHeaderBytes = 64 << 10
	// kubeProxyDrainTimeout bounds the wait, at a task's end, for its
	// requests to return once their connections are closed. A handler still
	// writing to a caller that has stopped reading is left behind after it:
	// its connection to kube-proxy is already closed.
	kubeProxyDrainTimeout = 5 * time.Second
	// kubeProxyShutdownTimeout bounds Close's wait for requests in flight.
	kubeProxyShutdownTimeout = 5 * time.Second

	// The local answers, each a Kubernetes Status so kubectl prints the
	// message.
	kubeProxyNoTaskMessage       = "worker-adapter: this pod is running no task, so there is no cluster to read. kubectl works only while a task runs."
	kubeProxyNoCredentialMessage = "worker-adapter: this task was given no cluster, so kubectl has nothing to read. This is the task's scope, not an error to work around."
	kubeProxyNotProxyMessage     = "worker-adapter: this listener serves kubectl on this pod only."
	kubeProxyNotFoundMessage     = "worker-adapter: only paths under " + kubeProxyPathPrefix + " are served, in their plain form: a cluster with no percent-encoding, and no . or .. segment."
	kubeProxyOtherClusterMessage = "worker-adapter: this task reads one cluster, and the request names another. This is the task's scope, not an error to work around."
	kubeProxyNoPodTokenMessage   = "worker-adapter: the pod's own token can't be read, so kube-proxy can't tell who is calling."
	kubeProxyUpstreamMessage     = "worker-adapter: the request to kube-proxy failed."

	kubeProxyStatusKind        = "Status"
	kubeProxyStatusAPIVersion  = "v1"
	kubeProxyStatusFailure     = "Failure"
	kubeProxyContentTypeHeader = "Content-Type"
	kubeProxyContentType       = "application/json"

	// kubeProxyPathSeparator separates a path's segments, and the two
	// segments below are the ones that climb or stand still.
	kubeProxyPathSeparator = "/"
	kubeProxyDotSegment    = "."
	kubeProxyDotDotSegment = ".."
	// kubeProxyEscape starts a percent-encoded byte.
	kubeProxyEscape = "%"

	// caFailureFormat names one failing version of the CA file: its
	// modification time, its size and the error.
	caFailureFormat = "%d/%d: %v"

	// gkeContextFormat is a GKE cluster's kubeconfig context name, from its
	// project, location and cluster (credential_proxy_client.ClusterTarget).
	gkeContextFormat = "gke_%s_%s_%s"
)

// kubeProxyStatusReasons are the Status reasons for the codes the proxy
// answers with itself.
var kubeProxyStatusReasons = map[int]string{
	http.StatusForbidden:          "Forbidden",
	http.StatusNotFound:           "NotFound",
	http.StatusBadGateway:         "InternalError",
	http.StatusServiceUnavailable: "ServiceUnavailable",
}

// gkeContextPart is one part of a GKE context name, as
// credential_proxy_client.parse_gke_context reads it.
var gkeContextPart = regexp.MustCompile(`\A[a-z0-9][a-z0-9-]*\z`)

// KubeProxyLoopbackConfig is what the loopback needs from the pod's
// environment.
type KubeProxyLoopbackConfig struct {
	// Upstream is the pod's kube-proxy (EnvKubeProxyURL). Empty leaves the
	// loopback off.
	Upstream string
	// CAFile is the CA kube-proxy's certificate is verified against
	// (EnvKubeProxyCAFile).
	CAFile string
	// PodTokenFile is the pod's projected ServiceAccount token
	// (EnvCredentialProxyTokenFile), read again for every request.
	PodTokenFile string
}

// KubeProxyLoopback is a running loopback. URL is what a task's harness is
// told (`http://127.0.0.1:<port>`).
type KubeProxyLoopback struct {
	URL string

	upstream     *url.URL
	podTokenFile string
	ca           *caFile
	log          *slog.Logger
	srv          *http.Server
	ln           net.Listener
	self         string
	rp           *httputil.ReverseProxy

	// drainTimeout is kubeProxyDrainTimeout, shorter in tests.
	drainTimeout time.Duration
	// taskEndHook, when set, runs as a task's end begins. Tests use it to
	// see what has and hasn't happened by then.
	taskEndHook func(task string)

	// mu guards task, and with it admission: a request takes its task and
	// joins that task's in-flight count under it, and a task's end clears
	// task under it, so no request joins a task that has begun to end.
	mu   sync.Mutex
	task *kubeProxyTask
}

// kubeProxyTask is one task's use of the loopback: its credential, and
// everything opened under it.
type kubeProxyTask struct {
	id string
	// credential is the task's cluster credential, "" when it holds none.
	credential string
	// target is the kubeconfig context of the cluster the credential names:
	// the only cluster the task's requests may name.
	target string
	// ctx ends with the task; every request admitted under it runs inside.
	ctx       context.Context
	cancel    context.CancelFunc
	transport *http.Transport
	// inflight counts the requests admitted under the task.
	inflight sync.WaitGroup
	end      sync.Once

	// mu guards conns and closed: every connection to kube-proxy dialed for
	// the task, upgraded ones included, until it is closed.
	mu     sync.Mutex
	conns  map[*kubeProxyConn]struct{}
	closed bool
}

// kubeProxyAdmission is what a request was admitted with, carried in its
// context to the rewrite and the transport.
type kubeProxyAdmission struct {
	task *kubeProxyTask
	// podToken is the pod's token as read for this request.
	podToken string
}

type kubeProxyAdmissionKey struct{}

// kubeProxyConn is a connection to kube-proxy that leaves its task's set
// when it closes.
type kubeProxyConn struct {
	net.Conn
	task *kubeProxyTask
	once sync.Once
}

func (c *kubeProxyConn) Close() error {
	c.once.Do(func() {
		c.task.mu.Lock()
		delete(c.task.conns, c)
		c.task.mu.Unlock()
	})
	return c.Conn.Close()
}

// track adds a new connection to the task's set, or closes it when the task
// has ended.
func (t *kubeProxyTask) track(raw net.Conn) (*kubeProxyConn, error) {
	c := &kubeProxyConn{Conn: raw, task: t}
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		_ = raw.Close()
		return nil, errKubeProxyTaskEnded
	}
	t.conns[c] = struct{}{}
	t.mu.Unlock()
	return c, nil
}

var errKubeProxyTaskEnded = errors.New("the task this request ran under has ended")

// caFile is the CA file's certificates, read again when the file's
// modification time or size changes, so a rotated CA is used by the next
// connection.
type caFile struct {
	path string
	// log is where a failed reload and its recovery are said; nil is the
	// default logger.
	log *slog.Logger

	mu      sync.Mutex
	modTime time.Time
	size    int64
	roots   *x509.CertPool
	// failure describes the reload that last failed, "" when the last one
	// worked: the file's version and the error, so each failing version is
	// logged once rather than once per connection.
	failure string
}

// pool is the file's certificates. A file that can't be read or holds no
// certificate keeps the last pool that loaded, when there is one: a rotation
// can be seen half done. That is logged once for each failing version of the
// file, and once more when a reload works again.
func (c *caFile) pool() (*x509.CertPool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	roots, err := c.load()
	if err == nil {
		if c.failure != "" {
			c.failure = ""
			c.logger().Info("kube-proxy loopback: the CA file loads again", "file", c.path)
		}
		return roots, nil
	}
	if c.roots == nil {
		return nil, err
	}
	failure := err.Error()
	if info, statErr := os.Stat(c.path); statErr == nil {
		failure = fmt.Sprintf(caFailureFormat, info.ModTime().UnixNano(), info.Size(), err)
	}
	if failure != c.failure {
		c.failure = failure
		c.logger().Warn("kube-proxy loopback: the CA file doesn't load; new connections keep the last certificates that did", "file", c.path, "err", err)
	}
	return c.roots, nil
}

func (c *caFile) logger() *slog.Logger {
	if c.log != nil {
		return c.log
	}
	return slog.Default()
}

func (c *caFile) load() (*x509.CertPool, error) {
	info, err := os.Stat(c.path)
	if err != nil {
		return nil, err
	}
	if c.roots != nil && info.ModTime().Equal(c.modTime) && info.Size() == c.size {
		return c.roots, nil
	}
	pem, err := os.ReadFile(c.path) // #nosec G304 -- the pod's own CA mount, from KUBE_PROXY_CA_FILE
	if err != nil {
		return nil, err
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("%s holds no PEM certificate", c.path)
	}
	c.roots, c.modTime, c.size = roots, info.ModTime(), info.Size()
	return roots, nil
}

// Enabled reports whether the pod names a kube-proxy at all.
func (c KubeProxyLoopbackConfig) Enabled() bool {
	return strings.TrimSpace(c.Upstream) != ""
}

// startKubeProxyLoopback is the loopback for a pod that names a kube-proxy,
// and nil for one that doesn't.
func startKubeProxyLoopback(cfg KubeProxyLoopbackConfig, log *slog.Logger) (*KubeProxyLoopback, error) {
	if !cfg.Enabled() {
		return nil, nil
	}
	return StartKubeProxyLoopback(cfg, log)
}

// StartKubeProxyLoopback listens on an ephemeral loopback port and forwards
// to cfg.Upstream. It refuses an upstream that isn't https, a CA file that
// doesn't load and a pod with no token file named. Close stops it.
func StartKubeProxyLoopback(cfg KubeProxyLoopbackConfig, log *slog.Logger) (*KubeProxyLoopback, error) {
	// The value is never printed: it can carry credentials (a userinfo, a
	// query), and url.Parse's error quotes it whole.
	upstream, err := url.Parse(strings.TrimSpace(cfg.Upstream))
	if err != nil {
		return nil, fmt.Errorf("%s does not parse as a URL: want https://host[:port][/path]", EnvKubeProxyURL)
	}
	if upstream.Scheme != kubeProxyUpstreamScheme || upstream.Host == "" || upstream.User != nil || upstream.RawQuery != "" || upstream.Fragment != "" {
		return nil, fmt.Errorf("%s (scheme %q, host %q): want https://host[:port][/path], with no credentials, query or fragment", EnvKubeProxyURL, upstream.Scheme, upstream.Host)
	}
	caPath := strings.TrimSpace(cfg.CAFile)
	if caPath == "" {
		return nil, fmt.Errorf("%s is set without %s: kube-proxy's certificate has nothing to be verified against", EnvKubeProxyURL, EnvKubeProxyCAFile)
	}
	if log == nil {
		log = slog.Default()
	}
	ca := &caFile{path: caPath, log: log}
	if _, err := ca.pool(); err != nil {
		return nil, fmt.Errorf("%s: %w", EnvKubeProxyCAFile, err)
	}
	podTokenFile := strings.TrimSpace(cfg.PodTokenFile)
	if podTokenFile == "" {
		return nil, fmt.Errorf("%s is set without %s: kube-proxy couldn't tell who is calling", EnvKubeProxyURL, EnvCredentialProxyTokenFile)
	}
	ln, err := net.Listen(kubeProxyListenNetwork, kubeProxyListenAddr)
	if err != nil {
		return nil, fmt.Errorf("kube-proxy loopback listen: %w", err)
	}
	p := &KubeProxyLoopback{
		URL:          fmt.Sprintf(kubeProxyURLFormat, ln.Addr().String()),
		upstream:     upstream,
		podTokenFile: podTokenFile,
		ca:           ca,
		log:          log,
		ln:           ln,
		self:         ln.Addr().String(),
		drainTimeout: kubeProxyDrainTimeout,
	}
	p.rp = &httputil.ReverseProxy{
		Rewrite:       p.rewrite,
		Transport:     kubeProxyRoundTripper{},
		FlushInterval: -1, // every chunk as it comes: a watch is a stream of events
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			// The path names a cluster and a resource; the transport's error
			// names the connection. No header and no body is logged.
			// A request cut by its task's end or its caller's leaving is
			// not a failure of kube-proxy's.
			level := slog.LevelWarn
			if errors.Is(err, context.Canceled) {
				level = slog.LevelDebug
			}
			log.Log(r.Context(), level, "kube-proxy loopback: the request to kube-proxy failed", "path", r.URL.Path, "err", err)
			writeKubeProxyStatus(w, http.StatusBadGateway, kubeProxyUpstreamMessage)
		},
		ErrorLog: slog.NewLogLogger(log.Handler(), slog.LevelWarn),
	}
	p.srv = &http.Server{
		Handler:           http.HandlerFunc(p.serve),
		ReadHeaderTimeout: kubeProxyHeaderTimeout,
		IdleTimeout:       kubeProxyIdleTimeout,
		MaxHeaderBytes:    kubeProxyMaxHeaderBytes,
		ErrorLog:          slog.NewLogLogger(log.Handler(), slog.LevelWarn),
	}
	go func() {
		if err := p.srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Warn("kube-proxy loopback stopped", "err", err)
		}
	}()
	log.Info("kube-proxy loopback listening", "url", p.URL, "upstream", upstream.Host)
	return p, nil
}

// serve admits one request under the active task and forwards it.
func (p *KubeProxyLoopback) serve(w http.ResponseWriter, r *http.Request) {
	// Only origin-form requests for this listener: an absolute-form request
	// or another Host is someone using it as a proxy, which it is not.
	if r.URL.IsAbs() || r.Host != p.self {
		writeKubeProxyStatus(w, http.StatusForbidden, kubeProxyNotProxyMessage)
		return
	}
	// 404, as for any path this listener doesn't serve: a path that climbs,
	// or spells its cluster in an encoding, is not one kubectl sends, and
	// not one whose cluster can be read here.
	cluster, ok := kubeProxyPathCluster(r.URL.EscapedPath())
	if !ok {
		writeKubeProxyStatus(w, http.StatusNotFound, kubeProxyNotFoundMessage)
		return
	}
	task, refusal := p.admit()
	if task == nil {
		writeKubeProxyStatus(w, http.StatusForbidden, refusal)
		return
	}
	defer task.inflight.Done()
	// The task's own cluster, byte for byte, or nothing is sent. kube-proxy
	// checks the same against the credential; this is in front of that.
	if cluster != task.target {
		writeKubeProxyStatus(w, http.StatusForbidden, kubeProxyOtherClusterMessage)
		return
	}
	podToken, err := p.podToken()
	if err != nil {
		p.log.Warn("kube-proxy loopback: the pod's token can't be read", "file", p.podTokenFile, "err", err)
		writeKubeProxyStatus(w, http.StatusServiceUnavailable, kubeProxyNoPodTokenMessage)
		return
	}
	// The request ends with its caller or with its task, whichever is first.
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	stop := context.AfterFunc(task.ctx, cancel)
	defer stop()
	ctx = context.WithValue(ctx, kubeProxyAdmissionKey{}, &kubeProxyAdmission{task: task, podToken: podToken})
	p.rp.ServeHTTP(w, r.WithContext(ctx))
}

// kubeProxyPathCluster is the cluster a request path names, the segment
// after kubeProxyPathPrefix, read from the path as it was sent (escaped). It
// refuses a path outside the prefix, an empty cluster, a cluster with a
// percent-encoded byte, and any "." or ".." segment, including one hidden by
// encoding its dots or the slash beside it.
func kubeProxyPathCluster(escaped string) (string, bool) {
	rest, ok := strings.CutPrefix(escaped, kubeProxyPathPrefix)
	if !ok {
		return "", false
	}
	segments := strings.Split(rest, kubeProxyPathSeparator)
	cluster := segments[0]
	if cluster == "" || strings.Contains(cluster, kubeProxyEscape) {
		return "", false
	}
	for _, segment := range segments {
		plain, err := url.PathUnescape(segment)
		if err != nil {
			return "", false
		}
		for _, part := range strings.Split(plain, kubeProxyPathSeparator) {
			if part == kubeProxyDotSegment || part == kubeProxyDotDotSegment {
				return "", false
			}
		}
	}
	return cluster, true
}

// admit is the active task with one more request in flight, or why there is
// none to run under.
func (p *KubeProxyLoopback) admit() (*kubeProxyTask, string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	switch {
	case p.task == nil:
		return nil, kubeProxyNoTaskMessage
	case p.task.credential == "":
		return nil, kubeProxyNoCredentialMessage
	}
	p.task.inflight.Add(1)
	return p.task, ""
}

// podToken reads the pod's projected token, which the kubelet rotates.
func (p *KubeProxyLoopback) podToken() (string, error) {
	raw, err := os.ReadFile(p.podTokenFile) // #nosec G304 -- the pod's own projected token mount, from CREDENTIAL_PROXY_TOKEN_FILE
	if err != nil {
		return "", err
	}
	token := strings.TrimSpace(string(raw))
	if token == "" {
		return "", errors.New("the token file is empty")
	}
	return token, nil
}

// rewrite points the request at kube-proxy and replaces every header that
// says who is calling. Method, path, query and body pass as they came.
func (p *KubeProxyLoopback) rewrite(r *httputil.ProxyRequest) {
	r.SetURL(p.upstream)
	r.Out.Host = p.upstream.Host
	// Headers and declared trailers alike: a trailer is a header sent late.
	for _, fields := range []http.Header{r.Out.Header, r.Out.Trailer} {
		for name := range fields {
			if kubeProxySaysWhoIsCalling(name) {
				delete(fields, name)
			}
		}
	}
	admitted, _ := r.In.Context().Value(kubeProxyAdmissionKey{}).(*kubeProxyAdmission)
	if admitted == nil {
		// serve admits every request; one that reaches here without is sent
		// with neither header, and kube-proxy refuses it.
		return
	}
	// Set, never Add, after the delete above: exactly one of each reaches
	// kube-proxy.
	r.Out.Header.Set(kubeProxyAuthorizationHeader, kubeProxyBearerPrefix+admitted.podToken)
	r.Out.Header.Set(taskCredentialHeader, admitted.task.credential)
}

// kubeProxySaysWhoIsCalling is whether a header or trailer name is one the
// proxy alone may set, or one no caller may send at all.
func kubeProxySaysWhoIsCalling(name string) bool {
	canonical := http.CanonicalHeaderKey(name)
	return canonical == kubeProxyAuthorizationHeader ||
		canonical == http.CanonicalHeaderKey(taskCredentialHeader) ||
		canonical == kubeProxyForwardedHeader ||
		strings.HasPrefix(canonical, kubeProxyImpersonatePrefix) ||
		strings.HasPrefix(canonical, kubeProxyForwardedPrefix)
}

// kubeProxyRoundTripper sends a request on the connections of the task it
// was admitted under.
type kubeProxyRoundTripper struct{}

func (kubeProxyRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) {
	admitted, _ := r.Context().Value(kubeProxyAdmissionKey{}).(*kubeProxyAdmission)
	if admitted == nil {
		return nil, errors.New("kube-proxy loopback: a request with no task")
	}
	return admitted.task.transport.RoundTrip(r)
}

// newTask is a task's state, with a transport of its own: no connection is
// shared between two tasks.
func (p *KubeProxyLoopback) newTask(id, credential, target string) *kubeProxyTask {
	ctx, cancel := context.WithCancel(context.Background())
	t := &kubeProxyTask{id: id, credential: credential, target: target, ctx: ctx, cancel: cancel, conns: map[*kubeProxyConn]struct{}{}}
	dialer := &net.Dialer{Timeout: kubeProxyDialTimeout}
	t.transport = &http.Transport{
		Proxy: nil, // straight to kube-proxy, whatever the pod's proxy env says
		DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			roots, err := p.ca.pool()
			if err != nil {
				return nil, fmt.Errorf("the kube-proxy CA: %w", err)
			}
			raw, err := dialer.DialContext(ctx, network, addr)
			if err != nil {
				return nil, err
			}
			conn, err := t.track(raw)
			if err != nil {
				return nil, err
			}
			// No ALPN offer, so the connection speaks HTTP/1.1.
			secured := tls.Client(conn, &tls.Config{RootCAs: roots, ServerName: p.upstream.Hostname(), MinVersion: tls.VersionTLS12})
			handshake, cancel := context.WithTimeout(ctx, kubeProxyDialTimeout)
			defer cancel()
			if err := secured.HandshakeContext(handshake); err != nil {
				_ = conn.Close()
				return nil, err
			}
			return secured, nil
		},
		// HTTP/1.1, so each watch owns a connection and closing a task's
		// connections ends exactly that task's streams.
		ForceAttemptHTTP2:     false,
		ResponseHeaderTimeout: kubeProxyResponseHeaderTimeout,
		MaxIdleConnsPerHost:   kubeProxyIdleConns,
		IdleConnTimeout:       kubeProxyIdleTimeout,
		// The response passes as kube-proxy encoded it.
		DisableCompression: true,
	}
	return t
}

// BeginTask makes a task the active one and returns what ends it. credential
// is the task's cluster credential as the dispatcher's confirm carried it;
// one that is empty, isn't shaped like a credential or names no cluster the
// loopback can read (a report credential, or claims that don't parse) leaves
// the task with none, and its requests are answered 403 here. The returned function closes everything the task opened
// and waits for its requests to return; it is safe to call more than once.
func (p *KubeProxyLoopback) BeginTask(id, credential string) func() {
	switch {
	case credential == "":
	case len(credential) > maxTaskCredentialBytes || !taskCredentialShape.MatchString(credential):
		p.log.Warn("kube-proxy loopback: the task's cluster credential isn't shaped like one; the task's kubectl will be refused", "task", id)
		credential = ""
	}
	// The cluster the credential names, which is all the task may ask for.
	// With none to hold it to, nothing is sent.
	target := taskTargetContext(credential)
	if target == "" {
		credential = ""
	}
	t := p.newTask(id, credential, target)
	p.mu.Lock()
	previous := p.task
	p.task = t
	p.mu.Unlock()
	if previous != nil {
		// The worker ends each task before the next begins; one still here
		// is ended now rather than left running beside its successor.
		p.log.Warn("kube-proxy loopback: a task began while another was active; ending the earlier one", "task", id, "earlier", previous.id)
		p.shut(previous)
	}
	return func() { p.endTask(t) }
}

// endTask clears t as the active task and closes everything opened under it.
func (p *KubeProxyLoopback) endTask(t *kubeProxyTask) {
	if p.taskEndHook != nil {
		p.taskEndHook(t.id)
	}
	p.mu.Lock()
	if p.task == t {
		p.task = nil
	}
	p.mu.Unlock()
	p.shut(t)
}

// shut ends a task that is no longer the active one, so nothing more is
// admitted under it: its context is cancelled, which aborts requests in
// flight and upgraded connections; every connection dialed for it is
// closed, which ends a stream being copied; its idle connections are dropped;
// and its handlers are waited for.
func (p *KubeProxyLoopback) shut(t *kubeProxyTask) {
	t.end.Do(func() {
		t.cancel()
		t.mu.Lock()
		t.closed = true
		conns := make([]*kubeProxyConn, 0, len(t.conns))
		for c := range t.conns {
			conns = append(conns, c)
		}
		t.mu.Unlock()
		for _, c := range conns {
			_ = c.Close()
		}
		t.transport.CloseIdleConnections()
		drained := make(chan struct{})
		go func() {
			t.inflight.Wait()
			close(drained)
		}()
		timer := time.NewTimer(p.drainTimeout)
		defer timer.Stop()
		select {
		case <-drained:
		case <-timer.C:
			p.log.Warn("kube-proxy loopback: a task's requests were still returning when the wait ended; their connections to kube-proxy are closed", "task", t.id)
		}
	})
}

// Close ends the active task, if any, and stops the listener.
func (p *KubeProxyLoopback) Close() error {
	p.mu.Lock()
	t := p.task
	p.task = nil
	p.mu.Unlock()
	if t != nil {
		p.shut(t)
	}
	ctx, cancel := context.WithTimeout(context.Background(), kubeProxyShutdownTimeout)
	defer cancel()
	err := p.srv.Shutdown(ctx)
	if err != nil {
		err = p.srv.Close()
	}
	// Serve may not have taken the listener yet (GatewayProxy.Close).
	_ = p.ln.Close()
	return err
}

// kubeProxyStatus is the part of a Kubernetes Status kubectl reads.
type kubeProxyStatus struct {
	Kind       string            `json:"kind"`
	APIVersion string            `json:"apiVersion"`
	Metadata   map[string]string `json:"metadata"`
	Status     string            `json:"status"`
	Message    string            `json:"message"`
	Reason     string            `json:"reason"`
	Code       int               `json:"code"`
}

// writeKubeProxyStatus answers with a Status, so kubectl prints message as
// an error from the server.
func writeKubeProxyStatus(w http.ResponseWriter, code int, message string) {
	body, _ := json.Marshal(kubeProxyStatus{
		Kind:       kubeProxyStatusKind,
		APIVersion: kubeProxyStatusAPIVersion,
		Metadata:   map[string]string{},
		Status:     kubeProxyStatusFailure,
		Message:    message,
		Reason:     kubeProxyStatusReasons[code],
		Code:       code,
	})
	w.Header().Set(kubeProxyContentTypeHeader, kubeProxyContentType)
	w.WriteHeader(code)
	_, _ = w.Write(append(body, '\n'))
}

// taskTargetContext is the kubeconfig context of the cluster a task's
// credential names, read without verifying it, or "" when it names none or
// can't be read. It chooses which cluster a kubectl that names none asks
// for, and which cluster the loopback lets the task's requests name. It
// grants nothing: the credential comes from the dispatcher's confirm, not
// from the agent, and the path may be enabled only against a kube-proxy that
// verifies the credential and holds each request to the cluster it names.
func taskTargetContext(credential string) string {
	parts := strings.Split(credential, ".")
	if len(parts) != 3 {
		return ""
	}
	payload, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return ""
	}
	var claims struct {
		NATS struct {
			Target *struct {
				Project  string `json:"project"`
				Location string `json:"location"`
				Cluster  string `json:"cluster"`
			} `json:"target"`
		} `json:"nats"`
	}
	if err := json.Unmarshal(payload, &claims); err != nil || claims.NATS.Target == nil {
		return ""
	}
	t := claims.NATS.Target
	for _, part := range []string{t.Project, t.Location, t.Cluster} {
		if !gkeContextPart.MatchString(part) {
			return ""
		}
	}
	return fmt.Sprintf(gkeContextFormat, t.Project, t.Location, t.Cluster)
}

// kubeProxyTaskEnv is a task's harness environment with the pod's kube-proxy
// replaced by the loopback: EnvKubeProxyURL names the loopback, the CA file
// goes (the loopback is plain http, and holds the CA itself), and the host
// context is the task's target, or absent when it has none.
func kubeProxyTaskEnv(env []string, loopbackURL, targetContext string) []string {
	out := make([]string, 0, len(env)+2)
	for _, kv := range env {
		name, _, _ := strings.Cut(kv, "=")
		if name == EnvKubeProxyURL || name == EnvKubeProxyCAFile || name == envKubeProxyHostContext {
			continue
		}
		out = append(out, kv)
	}
	out = append(out, EnvKubeProxyURL+"="+loopbackURL)
	if targetContext != "" {
		out = append(out, envKubeProxyHostContext+"="+targetContext)
	}
	return out
}

package kube

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/util/httpstream"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/portforward"
	"k8s.io/client-go/transport/spdy"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"
)

const (
	portForwardReadyTimeout  = 15 * time.Second
	portForwardRestartMinGap = 3 * time.Second
	// streamTunnelBaseURL is the base URL the bridge's requests carry. Its host is a placeholder
	// that is never resolved: every connection is dialled by DialContext as a port-forward
	// stream through the kube API server.
	streamTunnelBaseURL = "http://a2a-mcp"
	streamTunnelNetwork = "portforward"
)

var errTunnelClosed = errors.New("port-forward tunnel is closed")

// streamConnector opens one SPDY port-forward connection to the pod behind the MCP Service and
// returns it with the pod port to forward to.
type streamConnector func(ctx context.Context) (httpstream.Connection, int, error)

// PortForwardTunnel carries the bridge's connections to the deployment's <agent>-a2a-mcp
// Service as port-forward streams over one SPDY connection to the kube API server, opened
// in-process with the kubeconfig's credentials. Nothing listens on a local port: each HTTP
// connection the bridge makes is a fresh data stream (with its paired error stream), so the
// bearer token the bridge sends never crosses a local socket another process could hold, and
// the path is authenticated end to end by the API server's TLS. A dropped connection is
// re-opened, with the pod resolved again, on the next dial.
type PortForwardTunnel struct {
	connect streamConnector

	// dialSem serialises (re)connecting: a one-slot semaphore rather than a mutex, so a caller
	// waiting behind another's dial gives up when its own context ends.
	dialSem     chan struct{}
	mu          sync.Mutex
	conn        httpstream.Connection
	port        int
	nextID      int
	lastRestart time.Time
	closed      bool
}

// StartPortForward opens the port-forward connection for the MCP service named by prof and
// returns the tunnel once the API server has accepted it, or the reason it did not.
func StartPortForward(ctx context.Context, prof mcpbridge.DeploymentProfile) (*PortForwardTunnel, error) {
	remotePort := prof.MCPPort
	if remotePort <= 0 {
		remotePort = mcpbridge.DefaultMCPPort
	}
	svc := prof.MCPService
	if svc == "" && prof.PlatformAgentName != "" {
		svc = prof.PlatformAgentName + mcpbridge.MCPServiceSuffix
	}
	t := newPortForwardTunnel(podConnector(prof.KubeContext, prof.Namespace, svc, remotePort))
	waitCtx, cancel := context.WithTimeout(ctx, portForwardReadyTimeout)
	defer cancel()
	if _, _, err := t.connection(waitCtx); err != nil {
		t.Close()
		return nil, fmt.Errorf("port-forward to %s/%s: %w", prof.Namespace, svc, err)
	}
	return t, nil
}

func newPortForwardTunnel(connect streamConnector) *PortForwardTunnel {
	return &PortForwardTunnel{connect: connect, dialSem: make(chan struct{}, 1)}
}

// MCPURL returns the base URL for requests through the tunnel; see streamTunnelBaseURL.
func (t *PortForwardTunnel) MCPURL() string {
	return streamTunnelBaseURL
}

// StartGatewayPortForward opens the port-forward to the deployment's <agent>-a2a-mcp HTTP
// listener (default port 8089).
func StartGatewayPortForward(ctx context.Context, prof mcpbridge.DeploymentProfile) (*PortForwardTunnel, error) {
	mcpProf := prof
	if mcpProf.MCPService == "" && mcpProf.PlatformAgentName != "" {
		mcpProf.MCPService = mcpProf.PlatformAgentName + mcpbridge.MCPServiceSuffix
	}
	if mcpProf.MCPPort <= 0 {
		mcpProf.MCPPort = mcpbridge.DefaultMCPPort
	}
	return StartPortForward(ctx, mcpProf)
}

// DialContext opens one connection to the MCP server as a new port-forward stream pair. The
// network and address are ignored: the tunnel has one destination.
func (t *PortForwardTunnel) DialContext(ctx context.Context, _, _ string) (net.Conn, error) {
	conn, port, err := t.connection(ctx)
	if err != nil {
		return nil, err
	}
	t.mu.Lock()
	t.nextID++
	id := t.nextID
	t.mu.Unlock()

	headers := http.Header{}
	headers.Set(corev1.StreamType, corev1.StreamTypeError)
	headers.Set(corev1.PortHeader, strconv.Itoa(port))
	headers.Set(corev1.PortForwardRequestIDHeader, strconv.Itoa(id))
	errStream, err := conn.CreateStream(headers)
	if err != nil {
		t.dropIfClosed(conn)
		return nil, fmt.Errorf("creating port-forward error stream: %w", err)
	}
	// Nothing is written to the error stream; the server writes to it and closes it.
	_ = errStream.Close()
	headers.Set(corev1.StreamType, corev1.StreamTypeData)
	dataStream, err := conn.CreateStream(headers)
	if err != nil {
		_ = errStream.Reset()
		conn.RemoveStreams(errStream)
		t.dropIfClosed(conn)
		return nil, fmt.Errorf("creating port-forward data stream: %w", err)
	}
	sc := &streamConn{conn: conn, data: dataStream, errStream: errStream}
	go sc.watchErrors()
	return sc, nil
}

// connection returns the open SPDY connection, opening one if there is none or the last one
// closed. A caller waiting for another's dial gives up on its own ctx. The dial itself is
// bounded by portForwardReadyTimeout and detached from the ctx of the caller that happens to
// start it: the connection is shared, so that caller's cancellation must not abort it, and a
// long-lived request ctx must not leave a hung dial blocking every other caller.
func (t *PortForwardTunnel) connection(ctx context.Context) (httpstream.Connection, int, error) {
	select {
	case t.dialSem <- struct{}{}:
	case <-ctx.Done():
		return nil, 0, ctx.Err()
	}
	defer func() { <-t.dialSem }()
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return nil, 0, errTunnelClosed
	}
	if t.conn != nil {
		select {
		case <-t.conn.CloseChan():
			t.conn = nil
		default:
			conn, port := t.conn, t.port
			t.mu.Unlock()
			return conn, port, nil
		}
	}
	t.mu.Unlock()

	dialCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), portForwardReadyTimeout)
	defer cancel()
	conn, port, err := t.connect(dialCtx)
	if err != nil {
		return nil, 0, err
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		_ = conn.Close()
		return nil, 0, errTunnelClosed
	}
	t.conn, t.port = conn, port
	return conn, port, nil
}

// drop closes conn if it is still the tunnel's connection, so the next dial opens a new one.
func (t *PortForwardTunnel) drop(conn httpstream.Connection) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.conn == conn {
		t.conn = nil
	}
	_ = conn.Close()
}

// dropIfClosed is drop for a connection that has closed. A stream that failed on a connection
// still open does not tear down the other streams sharing it; a caller that sees the dial
// error restarts the tunnel (rate-limited) if the connection is unhealthy.
func (t *PortForwardTunnel) dropIfClosed(conn httpstream.Connection) {
	select {
	case <-conn.CloseChan():
		t.drop(conn)
	default:
	}
}

// Restart closes the current connection, at most once per portForwardRestartMinGap, so the
// next dial opens a fresh one to the pod then behind the Service.
func (t *PortForwardTunnel) Restart() {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed || t.conn == nil || time.Since(t.lastRestart) < portForwardRestartMinGap {
		return
	}
	t.lastRestart = time.Now()
	_ = t.conn.Close()
	t.conn = nil
}

// Close closes the connection and refuses further dials.
func (t *PortForwardTunnel) Close() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return
	}
	t.closed = true
	if t.conn != nil {
		_ = t.conn.Close()
		t.conn = nil
	}
}

// streamConn is one port-forward data stream as a net.Conn. A message on the paired error
// stream (the pod refused the port, the container is gone) ends the stream and is returned by
// the next Read or Write.
type streamConn struct {
	conn      httpstream.Connection
	data      httpstream.Stream
	errStream httpstream.Stream

	mu        sync.Mutex
	remoteErr error
	closeOnce sync.Once
}

func (c *streamConn) watchErrors() {
	msg, err := io.ReadAll(c.errStream)
	var remote error
	switch {
	case err != nil:
		remote = fmt.Errorf("reading port-forward error stream: %w", err)
	case len(msg) > 0:
		remote = fmt.Errorf("port-forward: %s", strings.TrimSpace(string(msg)))
	}
	if remote == nil {
		return
	}
	c.mu.Lock()
	c.remoteErr = remote
	c.mu.Unlock()
	_ = c.data.Reset()
}

func (c *streamConn) err(ioErr error) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.remoteErr != nil {
		return c.remoteErr
	}
	return ioErr
}

func (c *streamConn) Read(p []byte) (int, error) {
	n, err := c.data.Read(p)
	if err != nil && err != io.EOF {
		err = c.err(err)
	} else if err == io.EOF {
		if rErr := c.err(nil); rErr != nil {
			err = rErr
		}
	}
	return n, err
}

func (c *streamConn) Write(p []byte) (int, error) {
	n, err := c.data.Write(p)
	if err != nil {
		err = c.err(err)
	}
	return n, err
}

func (c *streamConn) Close() error {
	c.closeOnce.Do(func() {
		_ = c.data.Reset()
		_ = c.errStream.Reset()
		c.conn.RemoveStreams(c.data, c.errStream)
	})
	return nil
}

// Deadlines are not supported by the stream; the HTTP client bounds calls by context instead.
func (c *streamConn) SetDeadline(time.Time) error      { return nil }
func (c *streamConn) SetReadDeadline(time.Time) error  { return nil }
func (c *streamConn) SetWriteDeadline(time.Time) error { return nil }
func (c *streamConn) LocalAddr() net.Addr              { return tunnelAddr{} }
func (c *streamConn) RemoteAddr() net.Addr             { return tunnelAddr{} }

type tunnelAddr struct{}

func (tunnelAddr) Network() string { return streamTunnelNetwork }
func (tunnelAddr) String() string  { return strings.TrimPrefix(streamTunnelBaseURL, "http://") }

// podConnector opens the SPDY port-forward connection to the pod behind namespace/service.
func podConnector(kubeContext, namespace, service string, remotePort int) streamConnector {
	return func(ctx context.Context) (httpstream.Connection, int, error) {
		restCfg, err := restConfigForKubeContext(kubeContext)
		if err != nil {
			return nil, 0, err
		}
		clientset, err := kubernetes.NewForConfig(restCfg)
		if err != nil {
			return nil, 0, err
		}
		podName, targetPort, err := resolveServiceTargetPod(ctx, clientset, namespace, service, remotePort)
		if err != nil {
			return nil, 0, err
		}
		req := clientset.CoreV1().RESTClient().Post().
			Resource("pods").
			Namespace(namespace).
			Name(podName).
			SubResource("portforward")
		transport, upgrader, err := spdy.RoundTripperFor(restCfg)
		if err != nil {
			return nil, 0, err
		}
		dialer := spdy.NewDialer(upgrader, &http.Client{Transport: transport}, http.MethodPost, req.URL())

		// Dial takes no context: bound it here, and close a connection that arrives late.
		type result struct {
			conn httpstream.Connection
			err  error
		}
		done := make(chan result, 1)
		go func() {
			conn, _, err := dialer.Dial(portforward.PortForwardProtocolV1Name)
			done <- result{conn, err}
		}()
		select {
		case r := <-done:
			if r.err != nil {
				return nil, 0, fmt.Errorf("opening port-forward to pod %s/%s: %w", namespace, podName, r.err)
			}
			return r.conn, targetPort, nil
		case <-ctx.Done():
			go func() {
				if r := <-done; r.conn != nil {
					_ = r.conn.Close()
				}
			}()
			return nil, 0, fmt.Errorf("opening port-forward to pod %s/%s: %w", namespace, podName, ctx.Err())
		}
	}
}

// resolveServiceTargetPod finds a running pod behind namespace/service, preferring a ready
// one, and the container port the Service's remotePort targets on it. It returns an error
// naming what failed rather than guessing a pod name.
func resolveServiceTargetPod(ctx context.Context, clientset kubernetes.Interface, namespace, service string, remotePort int) (string, int, error) {
	dialCtx, cancel := context.WithTimeout(ctx, contextProbeTimeout)
	defer cancel()

	targetPort := remotePort
	var targetPortName string
	labelSelector := "app=" + service
	svc, svcErr := clientset.CoreV1().Services(namespace).Get(dialCtx, service, metav1.GetOptions{})
	if svcErr == nil && svc != nil {
		if len(svc.Spec.Selector) > 0 {
			labelSelector = labels.SelectorFromSet(svc.Spec.Selector).String()
		}
		for _, p := range svc.Spec.Ports {
			if int(p.Port) == remotePort {
				if p.TargetPort.IntVal > 0 {
					targetPort = int(p.TargetPort.IntVal)
				} else if p.TargetPort.StrVal != "" {
					targetPortName = p.TargetPort.StrVal
				}
				break
			}
		}
	}

	resolvePodPort := func(pod *corev1.Pod) int {
		if targetPortName != "" && pod != nil {
			for _, c := range pod.Spec.Containers {
				for _, cp := range c.Ports {
					if cp.Name == targetPortName && cp.ContainerPort > 0 {
						return int(cp.ContainerPort)
					}
				}
			}
		}
		return targetPort
	}

	pods, podsErr := clientset.CoreV1().Pods(namespace).List(dialCtx, metav1.ListOptions{LabelSelector: labelSelector})
	if podsErr == nil && len(pods.Items) > 0 {
		for i := range pods.Items {
			pod := &pods.Items[i]
			if pod.Status.Phase == corev1.PodRunning && pod.DeletionTimestamp == nil && isPodReady(pod) {
				return pod.Name, resolvePodPort(pod), nil
			}
		}
		for i := range pods.Items {
			pod := &pods.Items[i]
			if pod.Status.Phase == corev1.PodRunning && pod.DeletionTimestamp == nil {
				return pod.Name, resolvePodPort(pod), nil
			}
		}
		// None can serve (Pending, Failed, or terminating, as in a Recreate rollout or after
		// an eviction): refuse rather than dial one, so the next dial resolves again.
		states := make([]string, 0, len(pods.Items))
		for i := range pods.Items {
			pod := &pods.Items[i]
			state := string(pod.Status.Phase)
			if pod.DeletionTimestamp != nil {
				state += ", terminating"
			}
			states = append(states, fmt.Sprintf("%s (%s)", pod.Name, state))
		}
		return "", 0, fmt.Errorf("finding the pod behind service %s/%s: none of the %d pod(s) matching %s is running and not terminating: %s; retry once the replacement pod is running",
			namespace, service, len(pods.Items), labelSelector, strings.Join(states, ", "))
	}
	var problems []string
	if svcErr != nil {
		problems = append(problems, fmt.Sprintf("get service: %v", svcErr))
	}
	if podsErr != nil {
		problems = append(problems, fmt.Sprintf("list pods (%s): %v", labelSelector, podsErr))
	} else {
		problems = append(problems, fmt.Sprintf("no pods match %s", labelSelector))
	}
	return "", 0, fmt.Errorf("finding the pod behind service %s/%s: %s", namespace, service, strings.Join(problems, "; "))
}

func isPodReady(pod *corev1.Pod) bool {
	if pod == nil {
		return false
	}
	for _, cond := range pod.Status.Conditions {
		if cond.Type == corev1.PodReady && cond.Status == corev1.ConditionTrue {
			return true
		}
	}
	return false
}

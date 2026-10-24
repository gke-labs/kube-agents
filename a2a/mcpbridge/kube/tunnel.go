package kube

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/portforward"
	"k8s.io/client-go/transport/spdy"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"
)

const (
	portForwardReadyTimeout  = 15 * time.Second
	portForwardMinBackoff    = 250 * time.Millisecond
	portForwardMaxBackoff    = 5 * time.Second
	portForwardHealthyReset  = 5 * time.Second
	portForwardRestartMinGap = 3 * time.Second
	loopbackHost             = "127.0.0.1"
	httpSchemePrefix         = "http://"
	tcpNetwork               = "tcp"
	ephemeralPortSuffix      = ":0"
)

var (
	errPortForwardTimedOut = errors.New("timed out waiting for port-forward to bind local port")
)

// PortForwardTunnel manages a persistent in-process SPDY port-forward to the deployment's
// <agent>-a2a-mcp Service, automatically restarting the tunnel on the same local port if
// the connection drops.
type PortForwardTunnel struct {
	kubeContext string
	namespace   string
	service     string
	remotePort  int
	localPort   int

	mu           sync.Mutex
	activeStopCh chan struct{}
	restartCh    chan struct{}
	lastRestart  time.Time
	cancel       context.CancelFunc
	closed       bool
	doneCh       chan struct{}
	readyCh      chan struct{}
}

// StartPortForward starts a supervised in-process SPDY port-forward to the MCP service named by prof
// and waits until the local TCP port is accepting connections.
func StartPortForward(ctx context.Context, prof mcpbridge.DeploymentProfile) (*PortForwardTunnel, error) {
	remotePort := prof.MCPPort
	if remotePort <= 0 {
		remotePort = mcpbridge.DefaultMCPPort
	}
	svc := prof.MCPService
	if svc == "" && prof.PlatformAgentName != "" {
		svc = prof.PlatformAgentName + mcpbridge.MCPServiceSuffix
	}
	localPort, err := allocateFreeLocalPort()
	if err != nil {
		return nil, fmt.Errorf("allocating local port for port-forward: %w", err)
	}

	supCtx, supCancel := context.WithCancel(context.Background())
	t := &PortForwardTunnel{
		kubeContext: prof.KubeContext,
		namespace:   prof.Namespace,
		service:     svc,
		remotePort:  remotePort,
		localPort:   localPort,
		restartCh:   make(chan struct{}, 1),
		cancel:      supCancel,
		doneCh:      make(chan struct{}),
		readyCh:     make(chan struct{}),
	}
	go t.supervise(supCtx)

	waitCtx, waitCancel := context.WithTimeout(ctx, portForwardReadyTimeout)
	defer waitCancel()
	select {
	case <-t.readyCh:
		return t, nil
	case <-waitCtx.Done():
		t.Close()
		return nil, fmt.Errorf("%w (%s/%s)", errPortForwardTimedOut, prof.Namespace, svc)
	}
}

// MCPURL returns the local http:// URL for the port-forwarded a2a-mcp tunnel.
func (t *PortForwardTunnel) MCPURL() string {
	return fmt.Sprintf("%s%s:%d", httpSchemePrefix, loopbackHost, t.localPort)
}

// StartGatewayPortForward starts a supervised in-process SPDY port-forward to the deployment's
// <agent>-a2a-mcp HTTP listener (default port 8089) and waits until the local TCP port is ready.
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

// Close stops the port-forward supervisor and closes any active tunnel stream.
func (t *PortForwardTunnel) Close() {
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return
	}
	t.closed = true
	if t.activeStopCh != nil {
		close(t.activeStopCh)
		t.activeStopCh = nil
	}
	t.cancel()
	t.mu.Unlock()
	<-t.doneCh
}

// Restart terminates the current port-forward stream so the supervisor loop immediately
// opens a fresh SPDY tunnel on the same local port.
func (t *PortForwardTunnel) Restart() {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed || t.activeStopCh == nil {
		return
	}
	if time.Since(t.lastRestart) < portForwardRestartMinGap {
		return
	}
	t.lastRestart = time.Now()
	close(t.activeStopCh)
	t.activeStopCh = nil
	if t.restartCh != nil {
		select {
		case t.restartCh <- struct{}{}:
		default:
		}
	}
}

func (t *PortForwardTunnel) supervise(ctx context.Context) {
	defer close(t.doneCh)
	var readyOnce sync.Once
	backoff := portForwardMinBackoff
	for ctx.Err() == nil {
		startedAt := time.Now()
		_ = t.runInProcessPortForward(ctx, &readyOnce)
		if ctx.Err() != nil {
			return
		}
		if time.Since(startedAt) >= portForwardHealthyReset {
			backoff = portForwardMinBackoff
		}
		select {
		case <-ctx.Done():
			return
		case <-t.restartCh:
			backoff = portForwardMinBackoff
			continue
		default:
		}
		select {
		case <-ctx.Done():
			return
		case <-t.restartCh:
			backoff = portForwardMinBackoff
		case <-time.After(backoff):
			backoff *= 2
			if backoff > portForwardMaxBackoff {
				backoff = portForwardMaxBackoff
			}
		}
	}
}

func (t *PortForwardTunnel) runInProcessPortForward(ctx context.Context, readyOnce *sync.Once) error {
	restCfg, err := restConfigForKubeContext(t.kubeContext)
	if err != nil {
		return err
	}
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return err
	}

	podName, targetPort := t.resolveServiceTargetPod(ctx, clientset)
	req := clientset.CoreV1().RESTClient().Post().
		Resource("pods").
		Namespace(t.namespace).
		Name(podName).
		SubResource("portforward")

	transport, upgrader, err := spdy.RoundTripperFor(restCfg)
	if err != nil {
		return err
	}
	if closeIdle, ok := transport.(interface{ CloseIdleConnections() }); ok {
		defer closeIdle.CloseIdleConnections()
	}
	dialer := spdy.NewDialer(upgrader, &http.Client{Transport: transport}, http.MethodPost, req.URL())

	stopCh := make(chan struct{})
	pfReadyCh := make(chan struct{})

	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		close(stopCh)
		return nil
	}
	t.activeStopCh = stopCh
	t.mu.Unlock()

	defer func() {
		t.mu.Lock()
		if t.activeStopCh == stopCh {
			close(stopCh)
			t.activeStopCh = nil
		}
		t.mu.Unlock()
	}()

	fw, err := portforward.New(
		dialer,
		[]string{fmt.Sprintf("%d:%d", t.localPort, targetPort)},
		stopCh,
		pfReadyCh,
		io.Discard,
		io.Discard,
	)
	if err != nil {
		return err
	}

	go func() {
		select {
		case <-pfReadyCh:
			readyOnce.Do(func() { close(t.readyCh) })
		case <-stopCh:
		case <-ctx.Done():
		}
	}()

	return fw.ForwardPorts()
}

func (t *PortForwardTunnel) resolveServiceTargetPod(ctx context.Context, clientset *kubernetes.Clientset) (string, int) {
	dialCtx, cancel := context.WithTimeout(ctx, contextProbeTimeout)
	defer cancel()

	targetPort := t.remotePort
	var targetPortName string
	labelSelector := "app=" + t.service
	if svc, err := clientset.CoreV1().Services(t.namespace).Get(dialCtx, t.service, metav1.GetOptions{}); err == nil && svc != nil {
		if len(svc.Spec.Selector) > 0 {
			labelSelector = labels.SelectorFromSet(svc.Spec.Selector).String()
		}
		for _, p := range svc.Spec.Ports {
			if int(p.Port) == t.remotePort {
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

	if pods, err := clientset.CoreV1().Pods(t.namespace).List(dialCtx, metav1.ListOptions{LabelSelector: labelSelector}); err == nil && len(pods.Items) > 0 {
		for i := range pods.Items {
			pod := &pods.Items[i]
			if pod.Status.Phase == corev1.PodRunning && pod.DeletionTimestamp == nil && isPodReady(pod) {
				return pod.Name, resolvePodPort(pod)
			}
		}
		for i := range pods.Items {
			pod := &pods.Items[i]
			if pod.Status.Phase == corev1.PodRunning && pod.DeletionTimestamp == nil {
				return pod.Name, resolvePodPort(pod)
			}
		}
		return pods.Items[0].Name, resolvePodPort(&pods.Items[0])
	}
	return t.service + "-0", targetPort
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

func allocateFreeLocalPort() (int, error) {
	ln, err := net.Listen(tcpNetwork, loopbackHost+ephemeralPortSuffix)
	if err != nil {
		return 0, err
	}
	defer func() { _ = ln.Close() }()
	addr, ok := ln.Addr().(*net.TCPAddr)
	if !ok {
		return 0, errors.New("unexpected listener address type")
	}
	return addr.Port, nil
}

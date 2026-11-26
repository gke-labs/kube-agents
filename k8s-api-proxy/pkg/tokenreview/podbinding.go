package tokenreview

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/netip"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/authentication/serviceaccount"
	"k8s.io/apiserver/pkg/authentication/user"
	corev1client "k8s.io/client-go/kubernetes/typed/core/v1"

	"golang.org/x/sync/singleflight"
)

const (
	// The user.extra keys a TokenReview fills for a projected ServiceAccount token: the pod
	// the kubelet requested it for.
	extraPodName = "authentication.kubernetes.io/pod-name"
	extraPodUID  = "authentication.kubernetes.io/pod-uid"

	// maxPodLookupTTL caps how long a pod's addresses are reused, whatever the TokenReview
	// cache's TTL; the TTL used is the smaller of the two.
	maxPodLookupTTL = 30 * time.Second
	// podLookupTimeout bounds one pod GET; no longer than the TokenReview's own timeout.
	podLookupTimeout = tokenReviewTimeout
	// forbiddenLogInterval is the least time between two log lines for one namespace whose
	// pods the proxy may not read.
	forbiddenLogInterval = time.Minute
	// podCacheSweepSize is the cache size at which an insert first drops expired entries.
	podCacheSweepSize = 1024
	// maxLookupInterval is the most a pod's token waits between two GETs of its pod. A token
	// refused on a GET (its pod gone, recreated or stopped) can then cost at most one GET an
	// interval however often it is sent, and leaves the client's rate budget to other pods
	// and to TokenReviews. A matched pod is cached for longer than this, so it never waits.
	maxLookupInterval = time.Second
)

var (
	errNoPodIdentity = errors.New("the caller's token names no pod")
	errPeerNotPod    = errors.New("the request does not come from the pod its token was issued to")
	errLookupTooSoon = errors.New("the caller's pod was looked up less than an interval ago and not matched")
)

// podGetFunc reads one pod; the core/v1 client's Get in production.
type podGetFunc func(ctx context.Context, namespace, name string) (*corev1.Pod, error)

// podBinder holds a request authenticated with a pod's token to that pod: the request's
// direct peer has to be one of the addresses of the pod the TokenReview names, read from the
// API server and matched by UID. Every failure to show that is a refusal; none is a 503,
// so a caller that cannot be matched is never let through on a retry.
type podBinder struct {
	get     podGetFunc
	ttl     time.Duration
	timeout time.Duration
	// interval is the least time between two GETs for one pod and UID.
	interval time.Duration
	now      func() time.Time
	logger   *slog.Logger

	group singleflight.Group

	mu    sync.Mutex
	cache map[string]podAddrEntry
	// lastLookup is when each pod and UID was last read.
	lastLookup map[string]time.Time
	// forbiddenLogged is when each namespace's missing pod read was last logged.
	forbiddenLogged map[string]time.Time
}

type podAddrEntry struct {
	addrs   []netip.Addr
	expires time.Time
}

func newPodBinder(pods corev1client.PodsGetter, tokenCacheTTL time.Duration, logger *slog.Logger) *podBinder {
	get := func(ctx context.Context, namespace, name string) (*corev1.Pod, error) {
		return pods.Pods(namespace).Get(ctx, name, metav1.GetOptions{})
	}
	return newPodBinderWithGetter(get, tokenCacheTTL, logger)
}

func newPodBinderWithGetter(get podGetFunc, tokenCacheTTL time.Duration, logger *slog.Logger) *podBinder {
	if logger == nil {
		logger = slog.Default()
	}
	ttl := min(tokenCacheTTL, maxPodLookupTTL)
	return &podBinder{
		get:             get,
		ttl:             ttl,
		timeout:         podLookupTimeout,
		interval:        min(ttl, maxLookupInterval),
		lastLookup:      make(map[string]time.Time),
		now:             time.Now,
		logger:          logger,
		cache:           make(map[string]podAddrEntry),
		forbiddenLogged: make(map[string]time.Time),
	}
}

// check returns nil when remoteAddr, the request's direct peer, is an address of the pod
// the TokenReview's user's token was issued to. The peer is the connection's own address and
// never a header: nothing fronts kube-proxy, so it is the calling pod's.
func (b *podBinder) check(ctx context.Context, remoteAddr string, u user.Info) error {
	extra := u.GetExtra()
	name, uid := soleValue(extra[extraPodName]), soleValue(extra[extraPodUID])
	if name == "" || uid == "" {
		return errNoPodIdentity
	}
	// A pod's projected token is issued for a ServiceAccount in the pod's own namespace.
	namespace, _, err := serviceaccount.SplitUsername(u.GetName())
	if err != nil {
		return fmt.Errorf("the caller %q is not a ServiceAccount: %w", u.GetName(), err)
	}
	peer, err := peerAddr(remoteAddr)
	if err != nil {
		return err
	}
	addrs, err := b.podAddrs(ctx, namespace, name, uid)
	if err != nil {
		return err
	}
	for _, addr := range addrs {
		if addr == peer {
			return nil
		}
	}
	return fmt.Errorf("%w: peer %s, pod %s/%s", errPeerNotPod, peer, namespace, name)
}

// podAddrs returns the addresses of the pod namespace/name whose UID is uid, from the cache
// while its entry lasts. Concurrent lookups of one pod share a GET. Only a pod that matched
// is cached; a refusal is looked up again on a request an interval later, and refused
// without a GET before then.
func (b *podBinder) podAddrs(ctx context.Context, namespace, name, uid string) ([]netip.Addr, error) {
	key := namespace + "/" + name + "/" + uid
	b.mu.Lock()
	entry, ok := b.cache[key]
	b.mu.Unlock()
	if ok && b.now().Before(entry.expires) {
		return entry.addrs, nil
	}
	v, err, _ := b.group.Do(key, func() (any, error) {
		// The GET is shared by every request waiting on it, so one caller leaving does not
		// fail the others; the timeout still bounds it.
		if !b.startLookup(key) {
			return nil, fmt.Errorf("%w: %s/%s", errLookupTooSoon, namespace, name)
		}
		lookupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), b.timeout)
		defer cancel()
		addrs, err := b.lookup(lookupCtx, namespace, name, uid)
		if err != nil {
			return nil, err
		}
		b.store(key, addrs)
		return addrs, nil
	})
	if err != nil {
		return nil, err
	}
	return v.([]netip.Addr), nil
}

func (b *podBinder) lookup(ctx context.Context, namespace, name, uid string) ([]netip.Addr, error) {
	pod, err := b.get(ctx, namespace, name)
	if err != nil {
		if apierrors.IsForbidden(err) {
			b.logForbidden(namespace)
		}
		return nil, fmt.Errorf("reading the caller's pod %s/%s: %w", namespace, name, err)
	}
	if string(pod.UID) != uid {
		return nil, fmt.Errorf("the caller's pod %s/%s is not the one its token was issued to", namespace, name)
	}
	if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
		return nil, fmt.Errorf("the caller's pod %s/%s has stopped (%s)", namespace, name, pod.Status.Phase)
	}
	addrs := podAddresses(pod)
	if len(addrs) == 0 {
		return nil, fmt.Errorf("the caller's pod %s/%s has no address", namespace, name)
	}
	return addrs, nil
}

// startLookup records a GET for key, or reports false when the last one was less than an
// interval ago.
func (b *podBinder) startLookup(key string) bool {
	now := b.now()
	b.mu.Lock()
	defer b.mu.Unlock()
	if last, ok := b.lastLookup[key]; ok && now.Sub(last) < b.interval {
		return false
	}
	if len(b.lastLookup) >= podCacheSweepSize {
		for k, last := range b.lastLookup {
			if now.Sub(last) >= b.interval {
				delete(b.lastLookup, k)
			}
		}
	}
	b.lastLookup[key] = now
	return true
}

func (b *podBinder) store(key string, addrs []netip.Addr) {
	now := b.now()
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.cache) >= podCacheSweepSize {
		for k, e := range b.cache {
			if !now.Before(e.expires) {
				delete(b.cache, k)
			}
		}
	}
	b.cache[key] = podAddrEntry{addrs: addrs, expires: now.Add(b.ttl)}
}

// logForbidden says, at most once a forbiddenLogInterval for each namespace, that the proxy
// lacks the permission the check needs there; every request it refuses for it is already a
// 401 in the audit record.
func (b *podBinder) logForbidden(namespace string) {
	now := b.now()
	b.mu.Lock()
	last, seen := b.forbiddenLogged[namespace]
	if seen && now.Sub(last) < forbiddenLogInterval {
		b.mu.Unlock()
		return
	}
	b.forbiddenLogged[namespace] = now
	b.mu.Unlock()
	b.logger.Error(
		"kube-proxy may not get pods in a caller's namespace, so it refuses every pod-token request from there; grant get on pods to its ServiceAccount in that namespace",
		"permission", "get pods",
		"namespace", namespace,
	)
}

// podAddresses returns the pod's status.podIPs, or its status.podIP when it lists none,
// each with an IPv4-mapped IPv6 address unmapped.
func podAddresses(pod *corev1.Pod) []netip.Addr {
	raw := make([]string, 0, len(pod.Status.PodIPs)+1)
	for _, ip := range pod.Status.PodIPs {
		raw = append(raw, ip.IP)
	}
	if len(raw) == 0 && pod.Status.PodIP != "" {
		raw = append(raw, pod.Status.PodIP)
	}
	var addrs []netip.Addr
	for _, s := range raw {
		if addr, err := netip.ParseAddr(s); err == nil {
			addrs = append(addrs, addr.Unmap())
		}
	}
	return addrs
}

// peerAddr parses the host part of http.Request.RemoteAddr, unmapping an IPv4-mapped IPv6
// address so it compares equal to the pod's IPv4 address.
func peerAddr(remoteAddr string) (netip.Addr, error) {
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		return netip.Addr{}, fmt.Errorf("the request's peer %q: %w", remoteAddr, err)
	}
	addr, err := netip.ParseAddr(host)
	if err != nil {
		return netip.Addr{}, fmt.Errorf("the request's peer %q: %w", remoteAddr, err)
	}
	return addr.Unmap().WithZone(""), nil
}

// soleValue is the one value of a user.extra key, or "" for none or several.
func soleValue(values []string) string {
	if len(values) != 1 {
		return ""
	}
	return values[0]
}

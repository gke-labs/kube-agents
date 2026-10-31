package dispatch

import (
	"context"
	"log/slog"
	"sort"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// PodPool is the binary's Pool: the warm worker pods in the profile
// namespace, watched through the API on the worker component label
// (lib.ProfileComponentLabel), grouped by their profile and tier labels.
// Pods returns the Ready ones that aren't being deleted. Whether a pod is
// busy is the dispatcher's business (its holdings and the lease bucket),
// not the pool's.
type PodPool struct {
	store cache.Store
	run   func(stop <-chan struct{})
	ready func() bool
}

// NewPodPool builds the watch on namespace. Run starts it.
func NewPodPool(ctx context.Context, client kubernetes.Interface, namespace string, log *slog.Logger) *PodPool {
	selector := lib.ProfileComponentLabel + "=" + lib.ProfileWorkerComponent
	lw := &cache.ListWatch{
		ListFunc: func(opts metav1.ListOptions) (runtime.Object, error) {
			opts.LabelSelector = selector
			return client.CoreV1().Pods(namespace).List(ctx, opts)
		},
		WatchFunc: func(opts metav1.ListOptions) (watch.Interface, error) {
			opts.LabelSelector = selector
			return client.CoreV1().Pods(namespace).Watch(ctx, opts)
		},
	}
	store, informer := cache.NewInformerWithOptions(cache.InformerOptions{
		ListerWatcher: lw,
		ObjectType:    &corev1.Pod{},
		Handler: cache.ResourceEventHandlerFuncs{
			AddFunc: func(obj any) {
				if p, ok := obj.(*corev1.Pod); ok {
					log.Debug("worker pod seen", "pod", p.Name, "ready", podReady(p))
				}
			},
		},
	})
	return &PodPool{store: store, run: informer.Run, ready: informer.HasSynced}
}

// Run watches until ctx ends.
func (p *PodPool) Run(ctx context.Context) { p.run(ctx.Done()) }

// Synced reports whether the first list has landed.
func (p *PodPool) Synced() bool { return p.ready() }

// Pods implements Pool: the Ready pods of one (profile, tier), sorted.
func (p *PodPool) Pods(profile, tier string) []string {
	var out []string
	for _, obj := range p.store.List() {
		pod, ok := obj.(*corev1.Pod)
		if !ok || pod.Labels[lib.ProfileNameLabel] != profile || pod.Labels[lib.ProfileTierLabel] != tier {
			continue
		}
		if pod.DeletionTimestamp == nil && podReady(pod) && lib.ValidSubjectToken(pod.Name) {
			out = append(out, pod.Name)
		}
	}
	sort.Strings(out)
	return out
}

func podReady(p *corev1.Pod) bool {
	if p.Status.Phase != corev1.PodRunning {
		return false
	}
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}

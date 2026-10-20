package controller

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// The manager's cache keeps no managedFields. Every informer here is
// cluster-wide, so each entry is paid for once per object in the cluster, and
// managedFields is the largest part of an object nothing in this process
// reads: the only managedFields consumer in the repository is the drift
// detector, a separate binary with its own live client.
func TestTheManagerCacheKeepsNoManagedFields(t *testing.T) {
	transform := ManagerCacheOptions().DefaultTransform
	if transform == nil {
		t.Fatal("the manager's cache has no DefaultTransform, so every cached object carries its managedFields")
	}

	deployment := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "platform-agent-gateway",
			Namespace:   "kubeagents-system",
			Annotations: map[string]string{"kubectl.kubernetes.io/last-applied-configuration": "{}"},
			ManagedFields: []metav1.ManagedFieldsEntry{
				{Manager: "kube-agents-operator", Operation: metav1.ManagedFieldsOperationApply},
				{Manager: "kube-controller-manager", Operation: metav1.ManagedFieldsOperationUpdate},
			},
		},
	}

	out, err := transform(deployment)
	if err != nil {
		t.Fatalf("transforming a Deployment on its way into the cache: %v", err)
	}
	cached, ok := out.(*appsv1.Deployment)
	if !ok {
		t.Fatalf("the transform returned %T, want *appsv1.Deployment", out)
	}
	if len(cached.ManagedFields) != 0 {
		t.Errorf("the cached Deployment kept %d managedFields entries, want none", len(cached.ManagedFields))
	}
	// The annotation stays. It is the other bulk field, but a read-modify-write
	// built from a cached copy that had lost it would delete it from the live
	// object; managedFields is omitempty, so a nil one is simply absent from
	// the request and the server's own entries stand.
	if _, ok := cached.Annotations["kubectl.kubernetes.io/last-applied-configuration"]; !ok {
		t.Error("the transform dropped last-applied-configuration; an Update built from a cached copy would then remove it from the live object")
	}
	if cached.Name != deployment.Name || cached.Namespace != deployment.Namespace {
		t.Errorf("the transform changed the object's identity: %s/%s", cached.Namespace, cached.Name)
	}
}

// Pods are out of the cache by configuration, not by every reader
// remembering to ask for a live one. The four readers that exist are tested
// individually (pod_reads_are_live_test.go); this is what covers the fifth,
// written by someone who never read those tests.
func TestTheManagerClientNeverServesAPodFromTheCache(t *testing.T) {
	opts := ManagerClientOptions()
	if opts.Cache == nil {
		t.Fatal("the manager's client has no CacheOptions, so every kind is served from the cache")
	}
	for _, obj := range opts.Cache.DisableFor {
		if _, ok := obj.(*corev1.Pod); ok {
			return
		}
	}
	t.Errorf("Pods are not in the client's DisableFor (%T), so a cached Pod read starts a cluster-wide Pod informer", opts.Cache.DisableFor)
}

/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"os"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The callout's Role and the callout's informer are one design, and neither
// half is checkable from the other's file.
//
// The Role scopes the ConfigMap read to a single object by resourceNames. The
// received wisdom is that this cannot authorize list or watch, because a
// collection request names no resource — and if that were still true here, the
// callout would start, fail to sync, never serve a map, and refuse every
// connection to the bus. It is not true: with selector-aware authorization the
// API server matches resourceNames against a metadata.name field selector, so a
// narrowed LIST is allowed where a bare one is refused, and Store.WatchConfigMap
// narrows exactly that way.
//
// So this asserts the pairing against a real API server, in both directions:
// the narrowed reads the informer actually issues are permitted, and the bare
// LIST that dropping the field selector would produce is not. Either half
// changing alone breaks the callout in a way no unit test would see.
func TestTheCalloutRoleAuthorizesTheInformerItIsPairedWith(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS is unset; run through `make test`, which installs the envtest binaries")
	}

	env := &envtest.Environment{}
	cfg, err := env.Start()
	if err != nil {
		t.Fatalf("starting envtest: %v", err)
	}
	t.Cleanup(func() { _ = env.Stop() })

	admin, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		t.Fatalf("admin client: %v", err)
	}
	ctx := context.Background()

	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "agent", Namespace: "kubeagents-system"},
	}
	ns := agent.Namespace

	if _, err := admin.CoreV1().Namespaces().Create(ctx,
		&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}, metav1.CreateOptions{}); err != nil {
		t.Fatalf("namespace: %v", err)
	}
	if _, err := admin.CoreV1().ServiceAccounts(ns).Create(ctx,
		buildA2ACalloutServiceAccount(agent), metav1.CreateOptions{}); err != nil {
		t.Fatalf("serviceaccount: %v", err)
	}
	authMap, _, err := buildA2AAuthMapConfigMap(agent)
	if err != nil {
		t.Fatalf("rendering the identity map: %v", err)
	}
	if _, err := admin.CoreV1().ConfigMaps(ns).Create(ctx, authMap, metav1.CreateOptions{}); err != nil {
		t.Fatalf("configmap: %v", err)
	}
	// A second ConfigMap the callout must not be able to read, so "scoped to
	// one object" is asserted rather than assumed from the rule's shape.
	if _, err := admin.CoreV1().ConfigMaps(ns).Create(ctx, &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "someone-elses-config", Namespace: ns},
		Data:       map[string]string{"secret": "not for the callout"},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatalf("second configmap: %v", err)
	}

	// The real rendered Role and RoleBinding, not a hand-written stand-in.
	if _, err := admin.RbacV1().Roles(ns).Create(ctx, buildA2ACalloutRole(agent), metav1.CreateOptions{}); err != nil {
		t.Fatalf("role: %v", err)
	}
	if _, err := admin.RbacV1().RoleBindings(ns).Create(ctx, buildA2ACalloutRoleBinding(agent), metav1.CreateOptions{}); err != nil {
		t.Fatalf("rolebinding: %v", err)
	}

	impersonated := rest.CopyConfig(cfg)
	impersonated.Impersonate = rest.ImpersonationConfig{
		UserName: a2aServiceAccountName(ns, a2aCalloutName(agent)),
	}
	as, err := kubernetes.NewForConfig(impersonated)
	if err != nil {
		t.Fatalf("impersonated client: %v", err)
	}

	byName := "metadata.name=" + a2aAuthMapName(agent)

	t.Run("the narrowed LIST the informer issues is authorized", func(t *testing.T) {
		list, err := as.CoreV1().ConfigMaps(ns).List(ctx, metav1.ListOptions{FieldSelector: byName})
		if err != nil {
			t.Fatalf("the callout cannot list its own identity map: %v\n"+
				"The Role and Store.WatchConfigMap's field selector are one design; if the selector was dropped, or this cluster predates selector-aware authorization, the informer never syncs and the callout refuses every connection.", err)
		}
		if len(list.Items) != 1 {
			t.Errorf("narrowed list returned %d items, want 1", len(list.Items))
		}
	})

	t.Run("the narrowed WATCH the informer issues is authorized", func(t *testing.T) {
		wctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		w, err := as.CoreV1().ConfigMaps(ns).Watch(wctx, metav1.ListOptions{FieldSelector: byName})
		if err != nil {
			t.Fatalf("the callout cannot watch its own identity map: %v", err)
		}
		w.Stop()
	})

	t.Run("a bare LIST is refused", func(t *testing.T) {
		// This is what dropping the field selector would produce. It must
		// stay refused, or the callout's token becomes a read over every
		// ConfigMap in the namespace.
		if _, err := as.CoreV1().ConfigMaps(ns).List(ctx, metav1.ListOptions{}); err == nil {
			t.Error("the callout can list every ConfigMap in the namespace; the Role is not scoped")
		}
	})

	t.Run("another ConfigMap is unreadable", func(t *testing.T) {
		if _, err := as.CoreV1().ConfigMaps(ns).Get(ctx, "someone-elses-config", metav1.GetOptions{}); err == nil {
			t.Error("the callout read a ConfigMap that is not its identity map")
		}
	})

	t.Run("the identity map itself is readable by name", func(t *testing.T) {
		if _, err := as.CoreV1().ConfigMaps(ns).Get(ctx, a2aAuthMapName(agent), metav1.GetOptions{}); err != nil {
			t.Errorf("the callout cannot get its own identity map: %v", err)
		}
	})
}

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
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The policy source the chart's copy is generated from, relative to this package.
var webConsoleCRPolicyFile = filepath.Join("..", "..", "config", "admission", "web-console-cr-policy.yaml")

const (
	webConsoleCRPolicyNamespace = "web-console-cr-policy"
	webConsoleCRPolicyAgent     = "platform-agent"
	// The chart names the console's ServiceAccount `<release>-web-console`.
	webConsoleCRPolicyConsole = "kube-agents-web-console"
	// How long the API server may take to start enforcing a policy it just stored.
	webConsoleCRPolicyActivation = 30 * time.Second
)

var platformAgentGVK = schema.GroupVersionKind{Group: "kubeagents.x-k8s.io", Version: "v1alpha1", Kind: "PlatformAgent"}

// TestTheWebConsoleCRPolicyHoldsTheConsoleToItsOwnFields runs the shipped policy
// in a real API server. The console's ServiceAccount, with the chart's Role on
// the CR, may write its own annotations and spec.integration the way
// a2a/cmd/web-console does (JSON merge patches); any other change it makes is
// refused by the rule that names it. Other requesters are not selected.
func TestTheWebConsoleCRPolicyHoldsTheConsoleToItsOwnFields(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	admin, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	applyAdmissionPolicyFile(ctx, t, admin, webConsoleCRPolicyFile)

	ns := webConsoleCRPolicyNamespace
	mustCreate(ctx, t, admin, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})
	for _, sa := range []string{webConsoleCRPolicyConsole, "other-controller"} {
		mustCreate(ctx, t, admin, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: sa, Namespace: ns}})
	}
	// The chart's Role for the console (templates/web-console.yaml), granted to
	// both identities so only the policy tells them apart.
	mustCreate(ctx, t, admin, &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Name: "cr-patch", Namespace: ns},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{platformAgentGVK.Group}, Resources: []string{"platformagents"},
			ResourceNames: []string{webConsoleCRPolicyAgent}, Verbs: []string{"get", "patch"},
		}},
	})
	mustCreate(ctx, t, admin, &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "cr-patch", Namespace: ns},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: "cr-patch"},
		Subjects: []rbacv1.Subject{
			{Kind: rbacv1.ServiceAccountKind, Name: webConsoleCRPolicyConsole, Namespace: ns},
			{Kind: rbacv1.ServiceAccountKind, Name: "other-controller", Namespace: ns},
		},
	})
	mustCreate(ctx, t, admin, &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name: webConsoleCRPolicyAgent, Namespace: ns,
			Labels:      map[string]string{"team": "platform"},
			Annotations: map[string]string{"example.com/owner": "ops", "kubeagents.x-k8s.io/active-model": "gemini"},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
			},
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				IntegrationSpec: agentv1alpha1.IntegrationSpec{GitHub: &agentv1alpha1.GitHubSpec{GitRepo: "gke-labs/kube-agents"}},
			},
		},
	})
	console := impersonatedClient(t, cfg, ns, webConsoleCRPolicyConsole)
	patch := func(c client.Client, body string) error {
		pa := &unstructured.Unstructured{}
		pa.SetGroupVersionKind(platformAgentGVK)
		pa.SetNamespace(ns)
		pa.SetName(webConsoleCRPolicyAgent)
		return c.Patch(ctx, pa, client.RawPatch(types.MergePatchType, []byte(body)))
	}

	// The policy is stored before it is enforced; wait until the console's
	// change to spec.deployment.env is refused.
	const envPatch = `{"spec":{"deployment":{"env":[{"name":"WATCHER_ALERT_EXCLUDE","value":"*"}]}}}`
	deadline := time.Now().Add(webConsoleCRPolicyActivation)
	for {
		err := patch(console, envPatch)
		if apierrors.IsForbidden(err) || apierrors.IsInvalid(err) {
			break
		}
		if err == nil {
			if err := patch(admin, `{"spec":{"deployment":null}}`); err != nil {
				t.Fatal(err)
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("the policy was not enforced within %s (last patch: %v)", webConsoleCRPolicyActivation, err)
		}
		time.Sleep(time.Second)
	}

	// What a2a/cmd/web-console writes is admitted.
	for name, body := range map[string]string{
		"the active model (models.go)":    `{"metadata":{"annotations":{"kubeagents.x-k8s.io/active-model":"claude"}}}`,
		"the model target (models.go)":    `{"metadata":{"annotations":{"kubeagents.x-k8s.io/active-model-target":"vertex"}}}`,
		"the reasoning level (models.go)": `{"metadata":{"annotations":{"kubeagents.x-k8s.io/reasoning-level":"high"}}}`,
		"the applied PRs (patches.go)":    `{"metadata":{"annotations":{"kubeagents.x-k8s.io/applied-prs":"[1,2]"}}}`,
		"a console annotation removed":    `{"metadata":{"annotations":{"kubeagents.x-k8s.io/reasoning-level":null}}}`,
		"the repositories page (repositories.go), from the github alias": `{"metadata":{"annotations":{` +
			`"kubeagents.x-k8s.io/repository-topology":"single","kubeagents.x-k8s.io/repository-metadata":"{}"}},` +
			`"spec":{"integration":{"forges":[{"name":"github","namespace":"gke-labs"}],` +
			`"repositories":[{"forge":"github","repository":"gke-labs/kube-agents","role":"gitops"}],"github":null}}}`,
		"a repository added": `{"spec":{"integration":{"repositories":[` +
			`{"forge":"github","repository":"gke-labs/kube-agents","role":"gitops"},` +
			`{"forge":"github","repository":"kubernetes/kubernetes","role":"context"}]}}}`,
	} {
		if err := patch(console, body); err != nil {
			t.Errorf("%s: refused, want admitted: %v", name, err)
		}
	}

	const (
		specRule   = "may change only spec.integration"
		annotRule  = "may change only its own annotations"
		labelsRule = "labels, finalizers or owner references"
	)
	refused := map[string]struct{ body, rule string }{
		"spec.deployment.env":               {envPatch, specRule},
		"an image tag":                      {`{"spec":{"deployment":{"tag":"evil"}}}`, specRule},
		"spec.harness.eventWatcher.enabled": {`{"spec":{"harness":{"eventWatcher":{"enabled":false}}}}`, specRule},
		"spec.scope":                        {`{"spec":{"scope":{"projects":["another-project"]}}}`, specRule},
		"a foreign annotation changed":      {`{"metadata":{"annotations":{"example.com/owner":"me"}}}`, annotRule},
		"a foreign annotation added":        {`{"metadata":{"annotations":{"example.com/new":"x"}}}`, annotRule},
		"a foreign annotation removed":      {`{"metadata":{"annotations":{"example.com/owner":null}}}`, annotRule},
		"a label":                           {`{"metadata":{"labels":{"team":"other"}}}`, labelsRule},
		"a finalizer":                       {`{"metadata":{"finalizers":["example.com/hold"]}}`, labelsRule},
	}
	for name, tc := range refused {
		err := patch(console, tc.body)
		if !apierrors.IsForbidden(err) && !apierrors.IsInvalid(err) {
			t.Errorf("%s: %v, want refused", name, err)
			continue
		}
		if !strings.Contains(err.Error(), tc.rule) {
			t.Errorf("%s: refused with %q, want the rule %q", name, err, tc.rule)
		}
	}

	// Another identity holding the same Role is not selected, and neither is
	// the admin; both make the change the console may not.
	other := impersonatedClient(t, cfg, ns, "other-controller")
	if err := patch(other, `{"spec":{"deployment":{"tag":"v2"}}}`); err != nil {
		t.Errorf("another ServiceAccount: refused, want admitted: %v", err)
	}
	if err := patch(admin, `{"metadata":{"labels":{"team":"other"}}}`); err != nil {
		t.Errorf("the admin: refused, want admitted: %v", err)
	}
	got := &agentv1alpha1.PlatformAgent{}
	if err := admin.Get(ctx, client.ObjectKey{Namespace: ns, Name: webConsoleCRPolicyAgent}, got); err != nil {
		t.Fatal(err)
	}
	if ptr.Deref(got.Spec.Deployment.Tag, "") != "v2" || got.Labels["team"] != "other" {
		t.Errorf("the other identities' changes did not land: tag %v, labels %v", got.Spec.Deployment.Tag, got.Labels)
	}
}

package controller

import (
	"context"
	"reflect"
	"testing"

	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The broker reads the pod a verified token names, to hold the request to it
// (PodAddressBinding in credential_proxy.py). This grant is `get` on pods and
// nothing else, as a Role in each namespace whose pods call the broker, bound
// to the broker's own ServiceAccount. It is not the broker's only pod read
// (the minimal ClusterRole has one); it is the one the check keeps if that
// one is narrowed.
func TestTheBrokerPodReadRoleIsGetOnPodsAlone(t *testing.T) {
	agent := a2aTestAgent()
	for _, namespace := range []string{agent.Namespace, profileNamespaceName(agent)} {
		role := buildCredentialBrokerPodReadRole(agent, namespace)
		want := []rbacv1.PolicyRule{{APIGroups: []string{""}, Resources: []string{"pods"}, Verbs: []string{"get"}}}
		if !reflect.DeepEqual(role.Rules, want) {
			t.Errorf("%s: the broker's pod-read Role grants %+v, want %+v", namespace, role.Rules, want)
		}
		if role.Namespace != namespace || role.Kind != "Role" {
			t.Errorf("%s: rendered as %s in %q", namespace, role.Kind, role.Namespace)
		}
		binding := buildCredentialBrokerPodReadRoleBinding(agent, namespace)
		if binding.Namespace != namespace || binding.RoleRef.Kind != "Role" || binding.RoleRef.Name != role.Name {
			t.Errorf("%s: the binding refers to %+v in %q, want Role %s", namespace, binding.RoleRef, binding.Namespace, role.Name)
		}
		wantSubject := []rbacv1.Subject{{Kind: "ServiceAccount", Name: agentServiceAccountName(agent), Namespace: agent.Namespace}}
		if !reflect.DeepEqual(binding.Subjects, wantSubject) {
			t.Errorf("%s: the binding's subjects are %+v, want only the broker's ServiceAccount %+v", namespace, binding.Subjects, wantSubject)
		}
	}
	// The broker's ServiceAccount follows spec.security.serviceAccountName.
	agent.Spec.Security = &agentv1alpha1.SecuritySpec{ServiceAccountName: "custom-broker"}
	if got := buildCredentialBrokerPodReadRoleBinding(agent, agent.Namespace).Subjects[0].Name; got != "custom-broker" {
		t.Errorf("the binding names %q, want the CR's broker ServiceAccount custom-broker", got)
	}
}

// Rendered in the agent's namespace on every reconcile, owned by the agent, and
// in the profile namespace once it is fenced, marked by the agent's labels.
func TestTheBrokerPodReadRBACRendersInTheCallersNamespaces(t *testing.T) {
	ctx := context.Background()
	for _, fenced := range []bool{false, true} {
		scheme := setupScheme()
		agent := a2aTestAgent()
		agent.UID = "agent-uid"
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, profileNamespaceObject(agent)).
			WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
		r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
		if err := r.reconcileCredentialBrokerPodReadRBAC(ctx, agent); err != nil {
			t.Fatal(err)
		}
		if err := r.reconcileCredentialBrokerProfilePodRead(ctx, agent, fenced); err != nil {
			t.Fatal(err)
		}
		name := credentialBrokerPodReadName(agent)
		for _, obj := range []client.Object{&rbacv1.Role{}, &rbacv1.RoleBinding{}} {
			if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: name}, obj); err != nil {
				t.Fatalf("the agent namespace's %T: %v", obj, err)
			}
			if !metav1.IsControlledBy(obj, agent) {
				t.Errorf("the agent namespace's %T is not owned by the agent", obj)
			}
			err := cl.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: name}, obj)
			switch {
			case fenced && err != nil:
				t.Errorf("the profile namespace's %T is missing once fenced: %v", obj, err)
			case fenced && !agentLabelsMarkComponent(obj, agent, credentialBrokerPodReadComponent):
				t.Errorf("the profile namespace's %T lacks the agent's labels: %v", obj, obj.GetLabels())
			case !fenced && !apierrors.IsNotFound(err):
				t.Errorf("the profile namespace's %T rendered before the namespace is fenced: %v", obj, err)
			}
		}
		// The pod-read steps add no cluster-scoped grant of their own.
		var clusterRoles rbacv1.ClusterRoleList
		if err := cl.List(ctx, &clusterRoles); err != nil {
			t.Fatal(err)
		}
		if len(clusterRoles.Items) != 0 {
			t.Errorf("the pod-read steps rendered ClusterRoles: %+v", clusterRoles.Items)
		}
	}
}

// The agent namespace's RBAC sweep reaps kubeagents-prefixed RoleBindings
// bound to the broker's ServiceAccount; an agent named with that prefix must
// not lose its pod-read binding to it on every pass.
func TestTheRBACSweepSparesTheBrokerPodReadBinding(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.Name = "kubeagents-platform"
	binding := buildCredentialBrokerPodReadRoleBinding(agent, agent.Namespace)
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, binding).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.cleanupAgentRBAC(ctx, agent, false); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(binding), &rbacv1.RoleBinding{}); err != nil {
		t.Errorf("the sweep deleted the broker's pod-read binding: %v", err)
	}
}

// Deleting the PlatformAgent removes the broker's pod-read Role and binding in
// the profile namespace, which nothing owns; a pair under that name without
// this agent's labels is someone else's, and stays.
func TestHandleDeletionRemovesTheBrokerProfilePodRead(t *testing.T) {
	for _, own := range []bool{true, false} {
		scheme := setupScheme()
		agent := a2aTestAgent()
		agent.Finalizers = []string{platformAgentFinalizer}
		now := metav1.Now()
		agent.DeletionTimestamp = &now
		namespace := profileNamespaceName(agent)
		role, binding := buildCredentialBrokerPodReadRole(agent, namespace), buildCredentialBrokerPodReadRoleBinding(agent, namespace)
		if !own {
			role.Labels = map[string]string{labelInstance: "foreign-agent"}
			binding.Labels = map[string]string{labelInstance: "foreign-agent"}
		}
		cl := fake.NewClientBuilder().WithScheme(scheme).
			WithObjects(agent.DeepCopy(), role, binding).
			WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
		r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
		ctx := context.Background()
		current := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, current); err != nil {
			t.Fatal(err)
		}
		if _, err := r.handleDeletion(ctx, current); err != nil {
			t.Fatalf("handleDeletion (own=%v): %v", own, err)
		}
		key := types.NamespacedName{Namespace: namespace, Name: credentialBrokerPodReadName(agent)}
		for _, obj := range []client.Object{&rbacv1.Role{}, &rbacv1.RoleBinding{}} {
			err := cl.Get(ctx, key, obj)
			switch {
			case own && !apierrors.IsNotFound(err):
				t.Errorf("this agent's broker pod-read %T survived deletion: %v", obj, err)
			case !own && err != nil:
				t.Errorf("a foreign %T under the broker pod-read name was deleted: %v", obj, err)
			}
		}
	}
}

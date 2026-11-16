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
	"fmt"
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/go-logr/logr/funcr"
	corev1 "k8s.io/api/core/v1"
	storagev1 "k8s.io/api/storage/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
)

// loggingContext returns a context whose logger appends every message to lines.
func loggingContext(lines *[]string) context.Context {
	logger := funcr.New(func(prefix, args string) { *lines = append(*lines, args) }, funcr.Options{})
	return logf.IntoContext(context.Background(), logger)
}

// existingClaim is a claim the operator created on a previous install, at
// `size`: it carries the controller reference reconcilePersistentVolumeClaim
// sets before Create.
func existingClaim(agent *agentv1alpha1.PlatformAgent, name, size string) *corev1.PersistentVolumeClaim {
	pvc := unownedClaim(name, agent.Namespace, size)
	pvc.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: agentv1alpha1.GroupVersion.String(),
		Kind:       "PlatformAgent",
		Name:       agent.Name,
		UID:        agent.UID,
		Controller: ptr.To(true),
	}}
	return pvc
}

// testStorageClassName is the class every test claim names. growDataClaim
// reads it before a patch, so a test that expects a patch seeds
// storageClass(testStorageClassName, true).
const testStorageClassName = "test-class"

// storageClass is a StorageClass named name whose allowVolumeExpansion is
// expandable.
func storageClass(name string, expandable bool) *storagev1.StorageClass {
	return &storagev1.StorageClass{
		ObjectMeta:           metav1.ObjectMeta{Name: name},
		Provisioner:          "test.csi.example.com",
		AllowVolumeExpansion: ptr.To(expandable),
	}
}

// unownedClaim is a claim nobody's controller reference points at: one an
// administrator made by hand, or one a StatefulSet template created.
func unownedClaim(name, namespace, size string) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: corev1.PersistentVolumeClaimSpec{
			StorageClassName: ptr.To(testStorageClassName),
			AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse(size)},
			},
		},
	}
}

func claimSize(t *testing.T, cl client.Client, name, namespace string) string {
	t.Helper()
	got := &corev1.PersistentVolumeClaim{}
	if err := cl.Get(context.Background(), client.ObjectKey{Name: name, Namespace: namespace}, got); err != nil {
		t.Fatalf("reading claim %s: %v", name, err)
	}
	q := got.Spec.Resources.Requests[corev1.ResourceStorage]
	return q.String()
}

// TestReconcilePVCGrowsAnExistingDataClaim pins that the agent's data claim
// follows agentDataStorageSize on an existing install, not only on a new one:
// the claim is create-only otherwise, and a size raised to stop the volume
// filling would reach no install that already has the problem.
func TestReconcilePVCGrowsAnExistingDataClaim(t *testing.T) {
	agent := brokerPodAgent()
	dataClaim := buildPVC(agent)
	systemClaim := buildSystemPVC(agent)
	cases := []struct {
		name     string
		have     string
		wantSize string
	}{
		{"a smaller claim is widened to the constant", "10Gi", agentDataStorageSize},
		{"a claim already at the constant is left alone", agentDataStorageSize, agentDataStorageSize},
		{"a larger claim is never shrunk", "80Gi", "80Gi"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent,
					storageClass(testStorageClassName, true),
					existingClaim(agent, dataClaim.Name, tc.have),
					existingClaim(agent, systemClaim.Name, "1Gi")).
				Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

			if err := r.reconcilePVC(context.Background(), agent); err != nil {
				t.Fatalf("reconcilePVC: %v", err)
			}
			if got := claimSize(t, cl, dataClaim.Name, agent.Namespace); got != tc.wantSize {
				t.Errorf("data claim is %s, want %s", got, tc.wantSize)
			}
			if got := claimSize(t, cl, systemClaim.Name, agent.Namespace); got != "1Gi" {
				t.Errorf("the system claim moved to %s; only the data claim follows the constant", got)
			}
		})
	}
}

// TestReconcilePVCCreatesTheDataClaimAtTheConstant is the new-install half:
// no claim yet, so the reconcile creates one at the constant and the grow
// has nothing to do.
func TestReconcilePVCCreatesTheDataClaimAtTheConstant(t *testing.T) {
	agent := brokerPodAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcilePVC(context.Background(), agent); err != nil {
		t.Fatalf("reconcilePVC: %v", err)
	}
	if got := claimSize(t, cl, buildPVC(agent).Name, agent.Namespace); got != agentDataStorageSize {
		t.Errorf("new data claim is %s, want %s", got, agentDataStorageSize)
	}
}

// TestReconcilePVCSurvivesARefusedExpansion pins the best-effort contract: the
// class allows expansion, but the API server refuses the patch anyway (a
// namespace storage quota, say). The claim keeps its size, and the reconcile
// still succeeds rather than taking the agent down over a volume that is
// merely smaller than wanted.
func TestReconcilePVCSurvivesARefusedExpansion(t *testing.T) {
	agent := brokerPodAgent()
	dataClaim := buildPVC(agent)
	refusals := 0
	refuse := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if _, isPVC := obj.(*corev1.PersistentVolumeClaim); !isPVC {
				return cl.Patch(ctx, obj, patch, opts...)
			}
			refusals++
			return errors.NewForbidden(
				schema.GroupResource{Resource: "persistentvolumeclaims"}, obj.GetName(),
				fmt.Errorf("exceeded quota: storage, requested: requests.storage=40Gi"))
		},
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, storageClass(testStorageClassName, true), existingClaim(agent, dataClaim.Name, "10Gi")).
		WithInterceptorFuncs(refuse).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	var lines []string
	if err := r.reconcilePVC(loggingContext(&lines), agent); err != nil {
		t.Fatalf("a refused expansion must not fail the reconcile: %v", err)
	}
	if refusals == 0 {
		t.Fatal("the fake never refused a patch, so this exercised the accepting path and proves nothing")
	}
	if got := claimSize(t, cl, dataClaim.Name, agent.Namespace); got != "10Gi" {
		t.Errorf("data claim is %s after a refused patch, want 10Gi", got)
	}
	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, "refused the data claim's growth") {
		t.Errorf("an admission refusal must be reported as one, with its reason; logged:\n%s", joined)
	}
	if strings.Contains(joined, "could not grow the data claim") {
		t.Errorf("an admission refusal that repeats identically every reconcile was logged as an error:\n%s", joined)
	}
	if msg, _ := r.dataVolumeExpansionMessage(agent); msg != "" {
		t.Errorf("a refusal from a class that allows expansion must not set %s; message: %s", dataVolumeExpansionConditionType, msg)
	}
}

// TestReconcilePVCLeavesAClaimItDoesNotOwnAlone pins the ownership gate: a
// `<name>-data` claim an administrator made by hand, smaller on purpose, is
// adopted by reconcilePersistentVolumeClaim and must not be resized by the
// grow that follows it.
func TestReconcilePVCLeavesAClaimItDoesNotOwnAlone(t *testing.T) {
	agent := brokerPodAgent()
	dataClaim := buildPVC(agent)
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, unownedClaim(dataClaim.Name, agent.Namespace, "20Gi")).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcilePVC(context.Background(), agent); err != nil {
		t.Fatalf("reconcilePVC: %v", err)
	}
	if got := claimSize(t, cl, dataClaim.Name, agent.Namespace); got != "20Gi" {
		t.Errorf("a claim the operator did not create was resized to %s; it must stay at 20Gi", got)
	}
}

// TestReconcilePVCDoesNotRepatchAClaimWhoseResizeHasNotLanded pins the state a
// post-admission resize failure leaves behind: the spec already says the wanted
// size, the capacity still says the old one. Nothing to patch; the reconcile
// succeeds and leaves the claim for the resizer, the claim's events and whoever
// reads them.
func TestReconcilePVCDoesNotRepatchAClaimWhoseResizeHasNotLanded(t *testing.T) {
	agent := brokerPodAgent()
	dataClaim := buildPVC(agent)
	patches := 0
	count := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if _, isPVC := obj.(*corev1.PersistentVolumeClaim); isPVC {
				patches++
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	}
	stuck := existingClaim(agent, dataClaim.Name, agentDataStorageSize)
	stuck.Status.Phase = corev1.ClaimBound
	stuck.Status.Capacity = corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("10Gi")}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, stuck).
		WithStatusSubresource(stuck).
		WithInterceptorFuncs(count).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	var lines []string
	if err := r.reconcilePVC(loggingContext(&lines), agent); err != nil {
		t.Fatalf("a resize that has not landed must not fail the reconcile: %v", err)
	}
	if patches != 0 {
		t.Errorf("the claim's spec already asks for %s; %d patch(es) were issued for nothing", agentDataStorageSize, patches)
	}
	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, "has not landed") {
		t.Errorf("a spec at %s over a capacity of 10Gi must be reported every reconcile; logged:\n%s", agentDataStorageSize, joined)
	}
}

// TestReconcilePVCReportsAnAcceptedPatchAsARequest pins the wording: the API
// server accepting the patch is not the volume having grown, and the log must
// not say it is.
func TestReconcilePVCReportsAnAcceptedPatchAsARequest(t *testing.T) {
	agent := brokerPodAgent()
	dataClaim := buildPVC(agent)
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, storageClass(testStorageClassName, true), existingClaim(agent, dataClaim.Name, "10Gi")).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	var lines []string
	if err := r.reconcilePVC(loggingContext(&lines), agent); err != nil {
		t.Fatalf("reconcilePVC: %v", err)
	}
	joined := strings.Join(lines, "\n")
	if strings.Contains(joined, "grew the data claim") {
		t.Errorf("an accepted patch was logged as growth:\n%s", joined)
	}
	if !strings.Contains(joined, "requested growth of the data claim") {
		t.Errorf("an accepted patch must be logged as a request; logged:\n%s", joined)
	}
}

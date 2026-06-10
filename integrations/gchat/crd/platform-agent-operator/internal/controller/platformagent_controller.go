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
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"reflect"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-agentic/platform-agent-operator/api/v1alpha1"
)

const platformAgentFinalizer = "agent.platform.io/finalizer"

// PlatformAgentReconciler reconciles a PlatformAgent object
type PlatformAgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=agent.platform.io,resources=platformagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agent.platform.io,resources=platformagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=agent.platform.io,resources=platformagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts;persistentvolumeclaims;configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=iam.cnrm.cloud.google.com,resources=iamserviceaccounts;iampolicymembers,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=pubsub.cnrm.cloud.google.com,resources=pubsubtopics;pubsubsubscriptions,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=clusterroles;clusterrolebindings,verbs=get;list;watch;create;update;patch;delete;bind;escalate

func (r *PlatformAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// 1. Fetch the PlatformAgent instance
	instance := &agentv1alpha1.PlatformAgent{}
	err := r.Get(ctx, req.NamespacedName, instance)
	if err != nil {
		if errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// 1.5. Handle finalizer registration and deletion lifecycle hooks
	// Check if the instance is marked for deletion (DeletionTimestamp is set)
	if !instance.ObjectMeta.DeletionTimestamp.IsZero() {
		if containsString(instance.ObjectMeta.Finalizers, platformAgentFinalizer) {
			// Run custom cleanup logic for cluster-scoped resources (ClusterRoleBinding)
			if err := r.deleteExternalResources(ctx, instance); err != nil {
				return ctrl.Result{}, err
			}

			// Remove finalizer string from GMeta list
			instance.ObjectMeta.Finalizers = removeString(instance.ObjectMeta.Finalizers, platformAgentFinalizer)
			if err := r.Update(ctx, instance); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{}, nil
	}

	// Register finalizer if not already present in metadata
	if !containsString(instance.ObjectMeta.Finalizers, platformAgentFinalizer) {
		instance.ObjectMeta.Finalizers = append(instance.ObjectMeta.Finalizers, platformAgentFinalizer)
		if err := r.Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{}, nil
	}

	log.Info("Reconciling PlatformAgent", "name", instance.Name)

	// Update status phase to Provisioning if empty
	if instance.Status.Phase == "" {
		instance.Status.Phase = "Provisioning"
		if err := r.Status().Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{}, nil
	}

	// 2. Reconcile KCC GCP Identity (GSA), Topic, Subscription, and IAM bindings
	if os.Getenv("SKIP_KCC_RECONCILE") != "true" {
		requeue, err := r.reconcileGSA(ctx, instance)
		if err != nil {
			log.Error(err, "Failed to reconcile GSA")
			return ctrl.Result{}, err
		}
		if requeue {
			log.Info("Reconciliation requires requeue (GSA)")
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}

		// 3. Reconcile Pub/Sub Topic
		requeue, err = r.reconcileTopic(ctx, instance)
		if err != nil {
			log.Error(err, "Failed to reconcile Pub/Sub Topic")
			return ctrl.Result{}, err
		}
		if requeue {
			log.Info("Reconciliation requires requeue (Topic)")
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}

		// 4. Reconcile Pub/Sub Subscription
		requeue, err = r.reconcileSubscription(ctx, instance)
		if err != nil {
			log.Error(err, "Failed to reconcile Pub/Sub Subscription")
			return ctrl.Result{}, err
		}
		if requeue {
			log.Info("Reconciliation requires requeue (Subscription)")
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}

		// 5. Reconcile IAM Policy Bindings
		requeue, err = r.reconcileIAMBindings(ctx, instance)
		if err != nil {
			log.Error(err, "Failed to reconcile IAM Bindings")
			return ctrl.Result{}, err
		}
		if requeue {
			log.Info("Reconciliation requires requeue (IAM Bindings)")
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
	}

	// 6. Reconcile Kubernetes Service Account (KSA)
	if err := r.reconcileKSA(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile KSA")
		return ctrl.Result{}, err
	}

	// 6.5. Reconcile ClusterRoleBinding for GKE cluster-wide viewer capabilities
	if err := r.reconcileClusterRoleBinding(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile ClusterRoleBinding")
		return ctrl.Result{}, err
	}

	// Reconcile Platform custom ClusterRole
	if err := r.reconcilePlatformExplorerRole(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile Platform ClusterRole")
		return ctrl.Result{}, err
	}

	// Reconcile Platform custom ClusterRoleBinding
	if err := r.reconcilePlatformExplorerRoleBinding(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile Platform ClusterRoleBinding")
		return ctrl.Result{}, err
	}

	// 7. Reconcile PVC
	if err := r.reconcilePVC(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile PVC")
		return ctrl.Result{}, err
	}

	// 8. Reconcile ConfigMap
	if err := r.reconcileConfigMap(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile ConfigMap")
		return ctrl.Result{}, err
	}

	// 9. Reconcile Deployment
	if err := r.reconcileDeployment(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile Deployment")
		return ctrl.Result{}, err
	}

	// Update status phase to Ready
	if instance.Status.Phase != "Ready" {
		instance.Status.Phase = "Ready"
		if err := r.Status().Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		log.Info("PlatformAgent is Ready", "name", instance.Name)
	}

	return ctrl.Result{}, nil
}

// Intelligent diff check helper: returns true if desired map is a subset of found map.
func isMapSubset(desired, found map[string]any) bool {
	for k, desiredVal := range desired {
		foundVal, exists := found[k]
		if !exists {
			logf.Log.V(1).Info("isMapSubset diff: Key missing in found", "key", k)
			return false
		}
		desiredMap, desiredIsMap := desiredVal.(map[string]any)
		foundMap, foundIsMap := foundVal.(map[string]any)

		if desiredIsMap && foundIsMap {
			if !isMapSubset(desiredMap, foundMap) {
				return false
			}
		} else {
			desiredStr := fmt.Sprintf("%v", desiredVal)
			foundStr := fmt.Sprintf("%v", foundVal)
			if desiredStr != foundStr {
				logf.Log.V(1).Info("isMapSubset diff: Values differ", "key", k, "desired", desiredStr, "found", foundStr)
				return false
			}
		}
	}
	return true
}

// Helper to calculate the SHA256 hash of ConfigMap Data for rolling restarts.
func getConfigMapHash(configMap *corev1.ConfigMap) (string, error) {
	if configMap == nil {
		return "", nil
	}
	dataBytes, err := json.Marshal(configMap.Data)
	if err != nil {
		return "", err
	}
	hash := sha256.Sum256(dataBytes)
	return fmt.Sprintf("%x", hash), nil
}

// Custom Deployment spec comparison helper to avoid APIServer defaults trap.
func isDeploymentSpecEqual(found, desired *appsv1.Deployment) bool {
	// 1. Compare ConfigMap hashes (for rolling restarts) safely avoiding nil annotations map panic
	foundHash := ""
	if found.Spec.Template.Annotations != nil {
		foundHash = found.Spec.Template.Annotations["config-hash"]
	}
	desiredHash := ""
	if desired.Spec.Template.Annotations != nil {
		desiredHash = desired.Spec.Template.Annotations["config-hash"]
	}
	if foundHash != desiredHash {
		return false
	}

	// 2. Compare replicas count
	if found.Spec.Replicas == nil || desired.Spec.Replicas == nil || *found.Spec.Replicas != *desired.Spec.Replicas {
		return false
	}

	// 3. Compare critical container spec fields
	if len(found.Spec.Template.Spec.Containers) == 0 || len(desired.Spec.Template.Spec.Containers) == 0 {
		return false
	}
	foundContainer := found.Spec.Template.Spec.Containers[0]
	desiredContainer := desired.Spec.Template.Spec.Containers[0]

	// Compare Image URI
	if foundContainer.Image != desiredContainer.Image {
		return false
	}

	// Compare entire Env array (captures both 'Value' and 'ValueFrom' secret sources!)
	if !reflect.DeepEqual(foundContainer.Env, desiredContainer.Env) {
		return false
	}

	// Compare Volume Mounts (checks where folders are mounted inside the container)
	if !reflect.DeepEqual(foundContainer.VolumeMounts, desiredContainer.VolumeMounts) {
		return false
	}

	// Compare Resource Limits (CPU/Memory requests & limits)
	if !reflect.DeepEqual(foundContainer.Resources, desiredContainer.Resources) {
		return false
	}

	// 4. Compare Volumes specification (detects changes in backing ConfigMaps/PVC sources)
	if !reflect.DeepEqual(found.Spec.Template.Spec.Volumes, desired.Spec.Template.Spec.Volumes) {
		return false
	}

	return true
}

// Robust merge-based update for unstructured objects.
// Implements Delete-and-Recreate with Requeue signal (returns requeue=true) for immutable resources.
func (r *PlatformAgentReconciler) createOrUpdateUnstructured(ctx context.Context, obj *unstructured.Unstructured) (bool, error) {
	log := logf.FromContext(ctx)
	found := &unstructured.Unstructured{}
	found.SetGroupVersionKind(obj.GroupVersionKind())
	err := r.Get(ctx, client.ObjectKey{Name: obj.GetName(), Namespace: obj.GetNamespace()}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return false, r.Create(ctx, obj)
		}
		return false, err
	}

	// 1. Check if Spec has changed
	desiredSpec, desiredSpecExists, _ := unstructured.NestedMap(obj.Object, "spec")
	foundSpec, foundSpecExists, _ := unstructured.NestedMap(found.Object, "spec")

	specChanged := false
	if desiredSpecExists && foundSpecExists {
		if !isMapSubset(desiredSpec, foundSpec) {
			specChanged = true
			// Spec differs! For immutable IAMPolicyMember, we MUST delete and request requeue.
			if obj.GetKind() == "IAMPolicyMember" {
				log.Info("Spec of immutable IAMPolicyMember changed. Deleting and requesting requeue...", "name", obj.GetName())
				if err := r.Delete(ctx, found); err != nil {
					return false, err
				}
				return true, nil
			}
		}
	}

	// If the spec did NOT change and this is an IAMPolicyMember, we MUST skip to avoid GKE webhook denials!
	if !specChanged && obj.GetKind() == "IAMPolicyMember" {
		return false, nil
	}

	desiredLabels := obj.GetLabels()
	foundLabels := found.GetLabels()
	labelsChanged := false
	for k, v := range desiredLabels {
		if foundLabels == nil || foundLabels[k] != v {
			labelsChanged = true
			break
		}
	}

	desiredAnnotations := obj.GetAnnotations()
	foundAnnotations := found.GetAnnotations()
	annotationsChanged := false
	for k, v := range desiredAnnotations {
		if foundAnnotations == nil || foundAnnotations[k] != v {
			annotationsChanged = true
			break
		}
	}

	if !specChanged && !labelsChanged && !annotationsChanged {
		return false, nil
	}

	// 2. Merge Spec (for mutable resources)
	if desiredSpecExists {
		if !foundSpecExists {
			foundSpec = make(map[string]any)
		}
		maps.Copy(foundSpec, desiredSpec)
		err = unstructured.SetNestedMap(found.Object, foundSpec, "spec")
		if err != nil {
			return false, err
		}
	}

	// 3. Merge Labels
	if foundLabels == nil {
		foundLabels = make(map[string]string)
	}
	maps.Copy(foundLabels, desiredLabels)
	found.SetLabels(foundLabels)

	// 4. Merge Annotations
	if foundAnnotations == nil {
		foundAnnotations = make(map[string]string)
	}
	maps.Copy(foundAnnotations, desiredAnnotations)
	found.SetAnnotations(foundAnnotations)

	return false, r.Update(ctx, found)
}

func (r *PlatformAgentReconciler) reconcileGSA(ctx context.Context, instance *agentv1alpha1.PlatformAgent) (bool, error) {
	gsa := &unstructured.Unstructured{}
	gsa.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "iam.cnrm.cloud.google.com",
		Version: "v1beta1",
		Kind:    "IAMServiceAccount",
	})
	gsa.SetName(instance.Spec.GSAName)
	gsa.SetNamespace(instance.Namespace)
	gsa.UnstructuredContent()["spec"] = map[string]any{
		"displayName": "PlatformAgent GSA for GChat: " + instance.Name,
	}

	if err := ctrl.SetControllerReference(instance, gsa, r.Scheme); err != nil {
		return false, err
	}

	return r.createOrUpdateUnstructured(ctx, gsa)
}

func (r *PlatformAgentReconciler) reconcileTopic(ctx context.Context, instance *agentv1alpha1.PlatformAgent) (bool, error) {
	topic := &unstructured.Unstructured{}
	topic.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "pubsub.cnrm.cloud.google.com",
		Version: "v1beta1",
		Kind:    "PubSubTopic",
	})
	topic.SetName(instance.Spec.ChatTopicName)
	topic.SetNamespace(instance.Namespace)

	if err := ctrl.SetControllerReference(instance, topic, r.Scheme); err != nil {
		return false, err
	}

	return r.createOrUpdateUnstructured(ctx, topic)
}

func (r *PlatformAgentReconciler) reconcileSubscription(ctx context.Context, instance *agentv1alpha1.PlatformAgent) (bool, error) {
	sub := &unstructured.Unstructured{}
	sub.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "pubsub.cnrm.cloud.google.com",
		Version: "v1beta1",
		Kind:    "PubSubSubscription",
	})
	sub.SetName(instance.Spec.ChatSubName)
	sub.SetNamespace(instance.Namespace)
	sub.UnstructuredContent()["spec"] = map[string]any{
		"topicRef": map[string]any{
			"name": instance.Spec.ChatTopicName,
		},
		"ackDeadlineSeconds": int64(60),
	}

	if err := ctrl.SetControllerReference(instance, sub, r.Scheme); err != nil {
		return false, err
	}

	return r.createOrUpdateUnstructured(ctx, sub)
}

func (r *PlatformAgentReconciler) reconcileIAMPolicyMember(ctx context.Context, instance *agentv1alpha1.PlatformAgent, name string, resourceRef map[string]any, role string, member string) (bool, error) {
	iam := &unstructured.Unstructured{}
	iam.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "iam.cnrm.cloud.google.com",
		Version: "v1beta1",
		Kind:    "IAMPolicyMember",
	})
	iam.SetName(name)
	iam.SetNamespace(instance.Namespace)
	iam.UnstructuredContent()["spec"] = map[string]any{
		"resourceRef": resourceRef,
		"role":        role,
		"member":      member,
	}

	if err := ctrl.SetControllerReference(instance, iam, r.Scheme); err != nil {
		return false, err
	}

	return r.createOrUpdateUnstructured(ctx, iam)
}

func (r *PlatformAgentReconciler) reconcileIAMBindings(ctx context.Context, instance *agentv1alpha1.PlatformAgent) (bool, error) {
	gsaEmail := fmt.Sprintf("%s@%s.iam.gserviceaccount.com", instance.Spec.GSAName, instance.Spec.ProjectID)

	// 1. GSA Subscriber on Subscription
	requeue, err := r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-sub-subscriber", map[string]any{
		"apiVersion": "pubsub.cnrm.cloud.google.com/v1beta1",
		"kind":       "PubSubSubscription",
		"name":       instance.Spec.ChatSubName,
	}, "roles/pubsub.subscriber", "serviceAccount:"+gsaEmail)
	if err != nil {
		return false, err
	}
	if requeue {
		return true, nil
	}

	// 2. GSA Viewer on Subscription
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-sub-viewer", map[string]any{
		"apiVersion": "pubsub.cnrm.cloud.google.com/v1beta1",
		"kind":       "PubSubSubscription",
		"name":       instance.Spec.ChatSubName,
	}, "roles/pubsub.viewer", "serviceAccount:"+gsaEmail)
	if err != nil {
		return false, err
	}
	if requeue {
		return true, nil
	}

	// 3. GSA AI Platform User on Project (uses 'external' project mapping)
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-aiplatform-user", map[string]any{
		"kind":     "Project",
		"external": instance.Spec.ProjectID,
	}, "roles/aiplatform.user", "serviceAccount:"+gsaEmail)
	if err != nil {
		return false, err
	}
	if requeue {
		return true, nil
	}

	// 3.5. GSA Cluster Viewer on Project
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-cluster-viewer", map[string]any{
		"kind":     "Project",
		"external": instance.Spec.ProjectID,
	}, "roles/container.clusterViewer", "serviceAccount:"+gsaEmail)
	if err != nil {
		return false, err
	}
	if requeue {
		return true, nil
	}

	// 4. GChat System SA Publisher on Topic
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-chat-publisher", map[string]any{
		"apiVersion": "pubsub.cnrm.cloud.google.com/v1beta1",
		"kind":       "PubSubTopic",
		"name":       instance.Spec.ChatTopicName,
	}, "roles/pubsub.publisher", "serviceAccount:chat-api-push@system.gserviceaccount.com")
	if err != nil {
		return false, err
	}
	if requeue {
		return true, nil
	}

	// 5. GSuite Add-ons SA Publisher on Topic
	gsuiteSA := fmt.Sprintf("service-%s@gcp-sa-gsuiteaddons.iam.gserviceaccount.com", strings.TrimSpace(instance.Spec.NumericProjectID))
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-gsuite-publisher", map[string]any{
		"apiVersion": "pubsub.cnrm.cloud.google.com/v1beta1",
		"kind":       "PubSubTopic",
		"name":       instance.Spec.ChatTopicName,
	}, "roles/pubsub.publisher", "serviceAccount:"+gsuiteSA)
	if err != nil {
		return false, err
	}
	if requeue {
		return true, nil
	}

	// 6. Workload Identity Binding (GSA -> KSA)
	wiMember := fmt.Sprintf("serviceAccount:%s.svc.id.goog[%s/%s]", instance.Spec.ProjectID, instance.Namespace, instance.Spec.KSAName)
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-workload-identity", map[string]any{
		"apiVersion": "iam.cnrm.cloud.google.com/v1beta1",
		"kind":       "IAMServiceAccount",
		"name":       instance.Spec.GSAName,
	}, "roles/iam.workloadIdentityUser", wiMember)
	if err != nil {
		return false, err
	}
	if requeue {
		return true, nil
	}

	return false, nil
}

func (r *PlatformAgentReconciler) reconcileKSA(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	gsaEmail := fmt.Sprintf("%s@%s.iam.gserviceaccount.com", instance.Spec.GSAName, instance.Spec.ProjectID)
	ksa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{
			Name:      instance.Spec.KSAName,
			Namespace: instance.Namespace,
			Annotations: map[string]string{
				"iam.gke.io/gcp-service-account": gsaEmail,
			},
		},
	}

	if err := ctrl.SetControllerReference(instance, ksa, r.Scheme); err != nil {
		return err
	}

	found := &corev1.ServiceAccount{}
	err := r.Get(ctx, client.ObjectKey{Name: ksa.Name, Namespace: ksa.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, ksa)
		}
		return err
	}

	if found.Annotations == nil {
		found.Annotations = make(map[string]string)
	}
	if found.Annotations["iam.gke.io/gcp-service-account"] != gsaEmail {
		found.Annotations["iam.gke.io/gcp-service-account"] = gsaEmail
		return r.Update(ctx, found)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcilePVC(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Name:      instance.Name + "-data",
			Namespace: instance.Namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("10Gi"),
				},
			},
		},
	}

	if err := ctrl.SetControllerReference(instance, pvc, r.Scheme); err != nil {
		return err
	}

	found := &corev1.PersistentVolumeClaim{}
	err := r.Get(ctx, client.ObjectKey{Name: pvc.Name, Namespace: pvc.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, pvc)
		}
		return err
	}

	return nil
}

func (r *PlatformAgentReconciler) reconcileConfigMap(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	configMap := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      instance.Name + "-config",
			Namespace: instance.Namespace,
		},
		Data: map[string]string{
			"config.yaml": fmt.Sprintf(`model:
  default: %s
  provider: %s
terminal:
  backend: "local"
  cwd: "/opt/data"
platforms:
  google_chat:
    enabled: true
`, marshalString(instance.Spec.Model.Default), marshalString(instance.Spec.Model.Provider)),
		},
	}

	if err := ctrl.SetControllerReference(instance, configMap, r.Scheme); err != nil {
		return err
	}

	found := &corev1.ConfigMap{}
	err := r.Get(ctx, client.ObjectKey{Name: configMap.Name, Namespace: configMap.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, configMap)
		}
		return err
	}

	if found.Data == nil || found.Data["config.yaml"] != configMap.Data["config.yaml"] {
		found.Data = configMap.Data
		return r.Update(ctx, found)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcileDeployment(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	log := logf.FromContext(ctx)
	replicas := int32(1)
	fsGroup := int64(10000)

	// 1. Fetch the active ConfigMap to calculate the hash annotation
	configMap := &corev1.ConfigMap{}
	configMapHash := ""
	err := r.Get(ctx, client.ObjectKey{Name: instance.Name + "-config", Namespace: instance.Namespace}, configMap)
	if err != nil {
		if !errors.IsNotFound(err) {
			return err
		}
		log.Info("ConfigMap not found, skipping hash calculation until next reconcile cycle")
	} else {
		hash, hashErr := getConfigMapHash(configMap)
		if hashErr != nil {
			return hashErr
		}
		configMapHash = hash
	}

	deploy := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      instance.Name + "-gateway",
			Namespace: instance.Namespace,
			Labels: map[string]string{
				"app": instance.Name + "-gateway",
			},
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RecreateDeploymentStrategyType,
			},
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app": instance.Name + "-gateway",
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{
						"app": instance.Name + "-gateway",
					},
					Annotations: map[string]string{
						"config-hash": configMapHash,
					},
				},
				Spec: corev1.PodSpec{
					ServiceAccountName: instance.Spec.KSAName,
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup:        &fsGroup,
						RunAsUser:      func(i int64) *int64 { return &i }(1000),
						RunAsNonRoot:   func(b bool) *bool { return &b }(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{
						{
							Name:            "platform-agent",
							Image:           instance.Spec.ImageURI,
							ImagePullPolicy: corev1.PullAlways,
							Command:         []string{"hermes"},
							Args:            []string{"gateway", "run"},
							Ports: []corev1.ContainerPort{
								{
									Name:          "dashboard",
									ContainerPort: 9119,
								},
								{
									Name:          "api",
									ContainerPort: 8642,
								},
							},
							Env: []corev1.EnvVar{
								{
									Name:  "PLATFORM_AGENT_HOME",
									Value: "/opt/data",
								},
								{
									Name:  "PLATFORM_AGENT_DASHBOARD",
									Value: "1",
								},
								{
									Name:  "PLATFORM_AGENT_PLUGINS_DEBUG",
									Value: "1",
								},
								{
									Name: "API_SERVER_KEY",
									ValueFrom: &corev1.EnvVarSource{
										SecretKeyRef: &corev1.SecretKeySelector{
											LocalObjectReference: corev1.LocalObjectReference{
												Name: "platform-agent-secrets",
											},
											Key:      "API_SERVER_KEY",
											Optional: func(b bool) *bool { return &b }(true),
										},
									},
								},
								{
									Name:  "GKE_CLUSTER_NAME",
									Value: instance.Spec.ClusterName,
								},
								{
									Name:  "GKE_LOCATION",
									Value: instance.Spec.Location,
								},
								{
									Name:  "GOOGLE_CHAT_PROJECT_ID",
									Value: instance.Spec.ProjectID,
								},
								{
									Name:  "GOOGLE_CHAT_SUBSCRIPTION_NAME",
									Value: fmt.Sprintf("projects/%s/subscriptions/%s", instance.Spec.ProjectID, instance.Spec.ChatSubName),
								},
								{
									Name:  "GOOGLE_CHAT_ALLOWED_USERS",
									Value: instance.Spec.GoogleChatAllowedUsers,
								},
								{
									Name:  "GOOGLE_CHAT_HOME_CHANNEL",
									Value: instance.Spec.GoogleChatHomeChannel,
								},
								{
									Name: "GEMINI_API_KEY",
									ValueFrom: &corev1.EnvVarSource{
										SecretKeyRef: &corev1.SecretKeySelector{
											LocalObjectReference: corev1.LocalObjectReference{
												Name: "platform-agent-secrets",
											},
											Key:      "GEMINI_API_KEY",
											Optional: func(b bool) *bool { return &b }(true),
										},
									},
								},
								{
									Name:  "OTEL_SERVICE_NAME",
									Value: instance.Name + "-gateway",
								},
							},
							Resources: corev1.ResourceRequirements{
								Requests: corev1.ResourceList{
									corev1.ResourceCPU:    resource.MustParse("500m"),
									corev1.ResourceMemory: resource.MustParse("2Gi"),
								},
								Limits: corev1.ResourceList{
									corev1.ResourceCPU:    resource.MustParse("2"),
									corev1.ResourceMemory: resource.MustParse("4Gi"),
								},
							},
							VolumeMounts: []corev1.VolumeMount{
								{
									Name:      "platform-agent-data-vol",
									MountPath: "/opt/data",
								},
								{
									Name:      "platform-agent-config-vol",
									MountPath: "/opt/data/config.yaml",
									SubPath:   "config.yaml",
								},
							},
							SecurityContext: &corev1.SecurityContext{
								AllowPrivilegeEscalation: func(b bool) *bool { return &b }(false),
								Capabilities: &corev1.Capabilities{
									Drop: []corev1.Capability{"ALL"},
								},
							},
						},
					},
					Volumes: []corev1.Volume{
						{
							Name: "platform-agent-data-vol",
							VolumeSource: corev1.VolumeSource{
								PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
									ClaimName: instance.Name + "-data",
								},
							},
						},
						{
							Name: "platform-agent-config-vol",
							VolumeSource: corev1.VolumeSource{
								ConfigMap: &corev1.ConfigMapVolumeSource{
									LocalObjectReference: corev1.LocalObjectReference{
										Name: instance.Name + "-config",
									},
									DefaultMode: func(i int32) *int32 { return &i }(0755),
								},
							},
						},
					},
				},
			},
		},
	}

	if err := ctrl.SetControllerReference(instance, deploy, r.Scheme); err != nil {
		return err
	}

	found := &appsv1.Deployment{}
	err = r.Get(ctx, client.ObjectKey{Name: deploy.Name, Namespace: deploy.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			createErr := r.Create(ctx, deploy)
			if createErr != nil && !errors.IsAlreadyExists(createErr) {
				return createErr
			}
			return nil
		}
		return err
	}

	// Use custom deployment spec comparison to avoid APIServer defaults trap
	if !isDeploymentSpecEqual(found, deploy) || !reflect.DeepEqual(found.Labels, deploy.Labels) {
		found.Spec = deploy.Spec
		found.Labels = deploy.Labels
		return r.Update(ctx, found)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcileClusterRoleBinding(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	crb := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: instance.Namespace + "-" + instance.Name + "-cluster-viewer",
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      instance.Spec.KSAName,
				Namespace: instance.Namespace,
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "ClusterRole",
			Name:     "view",
		},
	}

	found := &rbacv1.ClusterRoleBinding{}
	err := r.Get(ctx, client.ObjectKey{Name: crb.Name}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, crb)
		}
		return err
	}

	if !reflect.DeepEqual(found.RoleRef, crb.RoleRef) {
		log := logf.FromContext(ctx)
		log.Info("RoleRef of ClusterRoleBinding changed. Deleting and recreating...", "name", crb.Name)
		if err := r.Delete(ctx, found); err != nil {
			return err
		}
		return r.Create(ctx, crb)
	}

	if !reflect.DeepEqual(found.Subjects, crb.Subjects) {
		found.Subjects = crb.Subjects
		return r.Update(ctx, found)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcilePlatformExplorerRole(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	cr := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{
			Name: instance.Namespace + "-" + instance.Name + "-platform-explorer",
		},
		Rules: []rbacv1.PolicyRule{
			{
				APIGroups: []string{""},
				Resources: []string{"nodes", "pods", "namespaces"},
				Verbs:     []string{"get", "list"},
			},
		},
	}

	found := &rbacv1.ClusterRole{}
	err := r.Get(ctx, client.ObjectKey{Name: cr.Name}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, cr)
		}
		return err
	}

	if !reflect.DeepEqual(found.Rules, cr.Rules) {
		found.Rules = cr.Rules
		return r.Update(ctx, found)
	}
	return nil
}

func (r *PlatformAgentReconciler) reconcilePlatformExplorerRoleBinding(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	crbName := instance.Namespace + "-" + instance.Name + "-platform-explorer-binding"
	roleName := instance.Namespace + "-" + instance.Name + "-platform-explorer"
	crb := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: crbName,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      instance.Spec.KSAName,
				Namespace: instance.Namespace,
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "ClusterRole",
			Name:     roleName,
		},
	}

	found := &rbacv1.ClusterRoleBinding{}
	err := r.Get(ctx, client.ObjectKey{Name: crb.Name}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, crb)
		}
		return err
	}

	if !reflect.DeepEqual(found.RoleRef, crb.RoleRef) {
		log := logf.FromContext(ctx)
		log.Info("RoleRef of ClusterRoleBinding changed. Deleting and recreating...", "name", crb.Name)
		if err := r.Delete(ctx, found); err != nil {
			return err
		}
		return r.Create(ctx, crb)
	}

	if !reflect.DeepEqual(found.Subjects, crb.Subjects) {
		found.Subjects = crb.Subjects
		return r.Update(ctx, found)
	}
	return nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *PlatformAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.PlatformAgent{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ConfigMap{}).
		Named("platformagent").
		Complete(r)
}

func (r *PlatformAgentReconciler) deleteExternalResources(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	log := logf.FromContext(ctx)
	crbName := instance.Namespace + "-" + instance.Name + "-cluster-viewer"

	crb := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: crbName,
		},
	}

	log.Info("Deleting associated ClusterRoleBinding during finalizer cleanup", "name", crbName)
	err := r.Delete(ctx, crb)
	if err != nil && !errors.IsNotFound(err) {
		return err
	}

	platformExplorerCrbName := instance.Namespace + "-" + instance.Name + "-platform-explorer-binding"
	platformExplorerCrb := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{
			Name: platformExplorerCrbName,
		},
	}
	log.Info("Deleting associated Platform ClusterRoleBinding during finalizer cleanup", "name", platformExplorerCrbName)
	err = r.Delete(ctx, platformExplorerCrb)
	if err != nil && !errors.IsNotFound(err) {
		return err
	}

	platformExplorerCrName := instance.Namespace + "-" + instance.Name + "-platform-explorer"
	platformExplorerCr := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{
			Name: platformExplorerCrName,
		},
	}
	log.Info("Deleting associated Platform ClusterRole during finalizer cleanup", "name", platformExplorerCrName)
	err = r.Delete(ctx, platformExplorerCr)
	if err != nil && !errors.IsNotFound(err) {
		return err
	}

	return nil
}

func containsString(slice []string, s string) bool {
	for _, item := range slice {
		if item == s {
			return true
		}
	}
	return false
}

func removeString(slice []string, s string) []string {
	var result []string
	for _, item := range slice {
		if item == s {
			continue
		}
		result = append(result, item)
	}
	return result
}

func marshalString(v string) string {
	b, _ := json.Marshal(v)
	return string(b)
}

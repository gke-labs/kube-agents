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
	"os"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-agentic/hermes-operator/api/v1alpha1"
)

// HermesAgentReconciler reconciles a HermesAgent object
type HermesAgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=agent.hermes.io,resources=hermesagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agent.hermes.io,resources=hermesagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=agent.hermes.io,resources=hermesagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts;persistentvolumeclaims;configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=iam.cnrm.cloud.google.com,resources=iamserviceaccounts;iampolicymembers,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=pubsub.cnrm.cloud.google.com,resources=pubsubtopics;pubsubsubscriptions,verbs=get;list;watch;create;update;patch;delete

func (r *HermesAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// 1. Fetch the HermesAgent instance
	instance := &agentv1alpha1.HermesAgent{}
	err := r.Get(ctx, req.NamespacedName, instance)
	if err != nil {
		if errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	log.Info("Reconciling HermesAgent", "name", instance.Name)

	// Update status phase to Provisioning if empty
	if instance.Status.Phase == "" {
		instance.Status.Phase = "Provisioning"
		if err := r.Status().Update(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
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
		log.Info("HermesAgent is Ready", "name", instance.Name)
	}

	return ctrl.Result{}, nil
}

// Intelligent diff check helper: returns true if desired map is a subset of found map.
func isMapSubset(desired, found map[string]interface{}) bool {
	for k, desiredVal := range desired {
		foundVal, exists := found[k]
		if !exists {
			logf.Log.V(1).Info("isMapSubset diff: Key missing in found", "key", k)
			return false
		}
		desiredMap, desiredIsMap := desiredVal.(map[string]interface{})
		foundMap, foundIsMap := foundVal.(map[string]interface{})

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

// Robust merge-based update for unstructured objects.
// Implements Delete-and-Recreate with Requeue signal (returns requeue=true) for immutable resources.
func (r *HermesAgentReconciler) createOrUpdateUnstructured(ctx context.Context, obj *unstructured.Unstructured) (bool, error) {
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

	// If the spec did NOT change and this is an IAMPolicyMember, we MUST skip Update to avoid GKE webhook denials!
	if !specChanged && obj.GetKind() == "IAMPolicyMember" {
		return false, nil
	}

	// 2. Merge Spec (for mutable resources)
	if desiredSpecExists {
		if !foundSpecExists {
			foundSpec = make(map[string]interface{})
		}
		for k, v := range desiredSpec {
			foundSpec[k] = v
		}
		err = unstructured.SetNestedMap(found.Object, foundSpec, "spec")
		if err != nil {
			return false, err
		}
	}

	// 3. Merge Labels
	desiredLabels := obj.GetLabels()
	foundLabels := found.GetLabels()
	if foundLabels == nil {
		foundLabels = make(map[string]string)
	}
	for k, v := range desiredLabels {
		foundLabels[k] = v
	}
	found.SetLabels(foundLabels)

	// 4. Merge Annotations
	desiredAnnotations := obj.GetAnnotations()
	foundAnnotations := found.GetAnnotations()
	if foundAnnotations == nil {
		foundAnnotations = make(map[string]string)
	}
	for k, v := range desiredAnnotations {
		foundAnnotations[k] = v
	}
	found.SetAnnotations(foundAnnotations)

	return false, r.Update(ctx, found)
}

func (r *HermesAgentReconciler) reconcileGSA(ctx context.Context, instance *agentv1alpha1.HermesAgent) (bool, error) {
	gsa := &unstructured.Unstructured{}
	gsa.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "iam.cnrm.cloud.google.com",
		Version: "v1beta1",
		Kind:    "IAMServiceAccount",
	})
	gsa.SetName(instance.Spec.GSAName)
	gsa.SetNamespace(instance.Namespace)
	gsa.UnstructuredContent()["spec"] = map[string]interface{}{
		"displayName": "Hermes GSA for GChat: " + instance.Name,
	}

	if err := ctrl.SetControllerReference(instance, gsa, r.Scheme); err != nil {
		return false, err
	}

	return r.createOrUpdateUnstructured(ctx, gsa)
}

func (r *HermesAgentReconciler) reconcileTopic(ctx context.Context, instance *agentv1alpha1.HermesAgent) (bool, error) {
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

func (r *HermesAgentReconciler) reconcileSubscription(ctx context.Context, instance *agentv1alpha1.HermesAgent) (bool, error) {
	sub := &unstructured.Unstructured{}
	sub.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "pubsub.cnrm.cloud.google.com",
		Version: "v1beta1",
		Kind:    "PubSubSubscription",
	})
	sub.SetName(instance.Spec.ChatSubName)
	sub.SetNamespace(instance.Namespace)
	sub.UnstructuredContent()["spec"] = map[string]interface{}{
		"topicRef": map[string]interface{}{
			"name": instance.Spec.ChatTopicName,
		},
		"ackDeadlineSeconds": int64(60),
	}

	if err := ctrl.SetControllerReference(instance, sub, r.Scheme); err != nil {
		return false, err
	}

	return r.createOrUpdateUnstructured(ctx, sub)
}

func (r *HermesAgentReconciler) reconcileIAMPolicyMember(ctx context.Context, instance *agentv1alpha1.HermesAgent, name string, resourceRef map[string]interface{}, role string, member string) (bool, error) {
	iam := &unstructured.Unstructured{}
	iam.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "iam.cnrm.cloud.google.com",
		Version: "v1beta1",
		Kind:    "IAMPolicyMember",
	})
	iam.SetName(name)
	iam.SetNamespace(instance.Namespace)
	iam.UnstructuredContent()["spec"] = map[string]interface{}{
		"resourceRef": resourceRef,
		"role":        role,
		"member":      member,
	}

	if err := ctrl.SetControllerReference(instance, iam, r.Scheme); err != nil {
		return false, err
	}

	return r.createOrUpdateUnstructured(ctx, iam)
}

func (r *HermesAgentReconciler) reconcileIAMBindings(ctx context.Context, instance *agentv1alpha1.HermesAgent) (bool, error) {
	gsaEmail := fmt.Sprintf("%s@%s.iam.gserviceaccount.com", instance.Spec.GSAName, instance.Spec.ProjectID)

	// 1. GSA Subscriber on Subscription
	requeue, err := r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-sub-subscriber", map[string]interface{}{
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
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-sub-viewer", map[string]interface{}{
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
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-aiplatform-user", map[string]interface{}{
		"kind":     "Project",
		"external": instance.Spec.ProjectID,
	}, "roles/aiplatform.user", "serviceAccount:"+gsaEmail)
	if err != nil {
		return false, err
	}
	if requeue {
		return true, nil
	}

	// 4. GChat System SA Publisher on Topic
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-chat-publisher", map[string]interface{}{
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
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-gsuite-publisher", map[string]interface{}{
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
	requeue, err = r.reconcileIAMPolicyMember(ctx, instance, instance.Name+"-workload-identity", map[string]interface{}{
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

func (r *HermesAgentReconciler) reconcileKSA(ctx context.Context, instance *agentv1alpha1.HermesAgent) error {
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
	found.Annotations["iam.gke.io/gcp-service-account"] = gsaEmail
	return r.Update(ctx, found)
}

func (r *HermesAgentReconciler) reconcilePVC(ctx context.Context, instance *agentv1alpha1.HermesAgent) error {
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

func (r *HermesAgentReconciler) reconcileConfigMap(ctx context.Context, instance *agentv1alpha1.HermesAgent) error {
	configMap := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      instance.Name + "-config",
			Namespace: instance.Namespace,
		},
		Data: map[string]string{
			"config.yaml": `model:
  default: "gemini-3.1-flash-lite"
  provider: "gemini"
terminal:
  backend: "local"
  cwd: "/opt/data"
platforms:
  google_chat:
    enabled: true
`,
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

	found.Data = configMap.Data
	return r.Update(ctx, found)
}

func (r *HermesAgentReconciler) reconcileDeployment(ctx context.Context, instance *agentv1alpha1.HermesAgent) error {
	replicas := int32(1)
	fsGroup := int64(10000)

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
				},
				Spec: corev1.PodSpec{
					ServiceAccountName: instance.Spec.KSAName,
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup: &fsGroup,
					},
					Containers: []corev1.Container{
						{
							Name:            "hermes",
							Image:           instance.Spec.ImageURI,
							ImagePullPolicy: corev1.PullAlways,
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
									Name:  "HERMES_HOME",
									Value: "/opt/data",
								},
								{
									Name:  "HERMES_DASHBOARD",
									Value: "1",
								},
								{
									Name:  "HERMES_PLUGINS_DEBUG",
									Value: "1",
								},
								{
									Name:  "API_SERVER_KEY",
									Value: "hermes_gke_internal_api_key_392859",
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
												Name: "hermes-secrets",
											},
											Key:      "GEMINI_API_KEY",
											Optional: func(b bool) *bool { return &b }(true),
										},
									},
								},
								{
									Name: "GCP_API_KEY",
									ValueFrom: &corev1.EnvVarSource{
										SecretKeyRef: &corev1.SecretKeySelector{
											LocalObjectReference: corev1.LocalObjectReference{
												Name: "hermes-secrets",
											},
											Key:      "GCP_API_KEY",
											Optional: func(b bool) *bool { return &b }(true),
										},
									},
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
									Name:      "hermes-data-vol",
									MountPath: "/opt/data",
								},
								{
									Name:      "hermes-config-vol",
									MountPath: "/opt/data/config.yaml",
									SubPath:   "config.yaml",
								},
							},
						},
					},
					Volumes: []corev1.Volume{
						{
							Name: "hermes-data-vol",
							VolumeSource: corev1.VolumeSource{
								PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
									ClaimName: instance.Name + "-data",
								},
							},
						},
						{
							Name: "hermes-config-vol",
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
	err := r.Get(ctx, client.ObjectKey{Name: deploy.Name, Namespace: deploy.Namespace}, found)
	if err != nil {
		if errors.IsNotFound(err) {
			return r.Create(ctx, deploy)
		}
		return err
	}

	found.Spec = deploy.Spec
	found.Labels = deploy.Labels
	return r.Update(ctx, found)
}



// SetupWithManager sets up the controller with the Manager.
func (r *HermesAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.HermesAgent{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ConfigMap{}).
		Named("hermesagent").
		Complete(r)
}

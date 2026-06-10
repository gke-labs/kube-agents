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
	"reflect"
	"strings"
	"time"

	cloudiam "cloud.google.com/go/iam"
	iampb "cloud.google.com/go/iam/apiv1/iampb"
	pubsub "cloud.google.com/go/pubsub"
	resourcemanager "cloud.google.com/go/resourcemanager/apiv3"
	iam "google.golang.org/api/iam/v1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-agentic/platform-agent-operator/api/v1alpha1"
)

const platformAgentFinalizer = "agent.platform.io/finalizer"

// PlatformAgentReconciler reconciles a PlatformAgent object
type PlatformAgentReconciler struct {
	client.Client
	Scheme                *runtime.Scheme
	ProjectID             string
	PubSubClient          *pubsub.Client
	IAMService            *iam.Service
	ResourceManagerClient *resourcemanager.ProjectsClient
}

// +kubebuilder:rbac:groups=agent.platform.io,resources=platformagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agent.platform.io,resources=platformagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=agent.platform.io,resources=platformagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts;persistentvolumeclaims;configmaps,verbs=get;list;watch;create;update;patch;delete
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
	if !instance.ObjectMeta.DeletionTimestamp.IsZero() {
		if containsString(instance.ObjectMeta.Finalizers, platformAgentFinalizer) {
			// Run custom cleanup logic for cluster-scoped and cloud-scoped resources
			if err := r.deleteExternalResources(ctx, instance); err != nil {
				return ctrl.Result{}, err
			}

			// Remove finalizer string from metadata list
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

	// 2. Reconcile GCP GSA Service Account
	if err := r.reconcileGSA(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile GSA")
		return ctrl.Result{}, err
	}

	// 3. Reconcile GCP Pub/Sub Topic
	if err := r.reconcileTopic(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile Pub/Sub Topic")
		return ctrl.Result{}, err
	}

	// 4. Reconcile GCP Pub/Sub Subscription
	if err := r.reconcileSubscription(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile Pub/Sub Subscription")
		return ctrl.Result{}, err
	}

	// 5. Reconcile GCP IAM Policy Bindings
	if err := r.reconcileIAMBindings(ctx, instance); err != nil {
		log.Error(err, "Failed to reconcile IAM Bindings")
		return ctrl.Result{}, err
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

func (r *PlatformAgentReconciler) reconcileGSA(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	gsaEmail := fmt.Sprintf("%s@%s.iam.gserviceaccount.com", instance.Spec.GSAName, r.ProjectID)
	name := fmt.Sprintf("projects/%s/serviceAccounts/%s", r.ProjectID, gsaEmail)

	_, err := r.IAMService.Projects.ServiceAccounts.Get(name).Context(ctx).Do()
	if err == nil {
		return nil
	}

	request := &iam.CreateServiceAccountRequest{
		AccountId: instance.Spec.GSAName,
		ServiceAccount: &iam.ServiceAccount{
			DisplayName: "PlatformAgent GSA for GChat: " + instance.Name,
		},
	}
	_, err = r.IAMService.Projects.ServiceAccounts.Create("projects/"+r.ProjectID, request).Context(ctx).Do()
	if err != nil {
		return fmt.Errorf("failed to create service account %s: %w", name, err)
	}

	return nil
}

func (r *PlatformAgentReconciler) reconcileTopic(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	topicID := instance.Spec.ChatTopicName
	topic := r.PubSubClient.Topic(topicID)

	exists, err := topic.Exists(ctx)
	if err != nil {
		return fmt.Errorf("failed to check pubsub topic %s: %w", topicID, err)
	}
	if exists {
		return nil
	}

	_, err = r.PubSubClient.CreateTopic(ctx, topicID)
	if err != nil {
		return fmt.Errorf("failed to create pubsub topic %s: %w", topicID, err)
	}

	return nil
}

func (r *PlatformAgentReconciler) reconcileSubscription(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	subID := instance.Spec.ChatSubName
	sub := r.PubSubClient.Subscription(subID)

	exists, err := sub.Exists(ctx)
	if err != nil {
		return fmt.Errorf("failed to check pubsub subscription %s: %w", subID, err)
	}
	if exists {
		return nil
	}

	topic := r.PubSubClient.Topic(instance.Spec.ChatTopicName)
	_, err = r.PubSubClient.CreateSubscription(ctx, subID, pubsub.SubscriptionConfig{
		Topic:       topic,
		AckDeadline: 60 * time.Second,
	})
	if err != nil {
		return fmt.Errorf("failed to create pubsub subscription %s: %w", subID, err)
	}

	return nil
}

func (r *PlatformAgentReconciler) reconcileIAMBindings(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	gsaEmail := fmt.Sprintf("%s@%s.iam.gserviceaccount.com", instance.Spec.GSAName, r.ProjectID)

	// 1. GSA Subscriber on Subscription
	err := r.addPubSubIAMBinding(ctx, "subscription", instance.Spec.ChatSubName, "roles/pubsub.subscriber", "serviceAccount:"+gsaEmail)
	if err != nil {
		return err
	}

	// 2. GSA Viewer on Subscription
	err = r.addPubSubIAMBinding(ctx, "subscription", instance.Spec.ChatSubName, "roles/pubsub.viewer", "serviceAccount:"+gsaEmail)
	if err != nil {
		return err
	}

	// 3. GSA AI Platform User on Project
	err = r.addProjectIAMBinding(ctx, r.ProjectID, "roles/aiplatform.user", "serviceAccount:"+gsaEmail)
	if err != nil {
		return err
	}

	// 4. GChat System SA Publisher on Topic
	err = r.addPubSubIAMBinding(ctx, "topic", instance.Spec.ChatTopicName, "roles/pubsub.publisher", "serviceAccount:chat-api-push@system.gserviceaccount.com")
	if err != nil {
		return err
	}

	// 5. GSuite Add-ons SA Publisher on Topic
	gsuiteSA := fmt.Sprintf("service-%s@gcp-sa-gsuiteaddons.iam.gserviceaccount.com", strings.TrimSpace(instance.Spec.NumericProjectID))
	err = r.addPubSubIAMBinding(ctx, "topic", instance.Spec.ChatTopicName, "roles/pubsub.publisher", "serviceAccount:"+gsuiteSA)
	if err != nil {
		return err
	}

	// 6. Workload Identity Binding (GSA -> KSA)
	wiMember := fmt.Sprintf("serviceAccount:%s.svc.id.goog[%s/%s]", r.ProjectID, instance.Namespace, instance.Spec.KSAName)
	err = r.addGSABinding(ctx, r.ProjectID, gsaEmail, "roles/iam.workloadIdentityUser", wiMember)
	if err != nil {
		return err
	}

	return nil
}

func (r *PlatformAgentReconciler) addPubSubIAMBinding(ctx context.Context, resourceType string, resourceName string, role string, member string) error {
	var handle *cloudiam.Handle
	if resourceType == "topic" {
		handle = r.PubSubClient.Topic(resourceName).IAM()
	} else {
		handle = r.PubSubClient.Subscription(resourceName).IAM()
	}

	policy, err := handle.Policy(ctx)
	if err != nil {
		return fmt.Errorf("failed to get IAM policy for %s %s: %w", resourceType, resourceName, err)
	}

	if policy.HasRole(member, cloudiam.RoleName(role)) {
		return nil
	}

	policy.Add(member, cloudiam.RoleName(role))
	err = handle.SetPolicy(ctx, policy)
	if err != nil {
		return fmt.Errorf("failed to set IAM policy for %s %s: %w", resourceType, resourceName, err)
	}

	return nil
}

func (r *PlatformAgentReconciler) addProjectIAMBinding(ctx context.Context, projectID string, role string, member string) error {
	req := &iampb.GetIamPolicyRequest{
		Resource: "projects/" + projectID,
	}
	policy, err := r.ResourceManagerClient.GetIamPolicy(ctx, req)
	if err != nil {
		return fmt.Errorf("failed to get IAM policy for project %s: %w", projectID, err)
	}

	foundBinding := false
	for _, binding := range policy.Bindings {
		if binding.Role == role {
			for _, m := range binding.Members {
				if m == member {
					foundBinding = true
					break
				}
			}
			if !foundBinding {
				binding.Members = append(binding.Members, member)
				foundBinding = true
			}
			break
		}
	}

	if !foundBinding {
		policy.Bindings = append(policy.Bindings, &iampb.Binding{
			Role:    role,
			Members: []string{member},
		})
	}

	setReq := &iampb.SetIamPolicyRequest{
		Resource: "projects/" + projectID,
		Policy:   policy,
	}
	_, err = r.ResourceManagerClient.SetIamPolicy(ctx, setReq)
	if err != nil {
		return fmt.Errorf("failed to set project IAM policy for project %s: %w", projectID, err)
	}

	return nil
}

func (r *PlatformAgentReconciler) removeProjectIAMBinding(ctx context.Context, projectID string, role string, member string) error {
	req := &iampb.GetIamPolicyRequest{
		Resource: "projects/" + projectID,
	}
	policy, err := r.ResourceManagerClient.GetIamPolicy(ctx, req)
	if err != nil {
		return fmt.Errorf("failed to get IAM policy for project %s: %w", projectID, err)
	}

	for _, binding := range policy.Bindings {
		if binding.Role == role {
			for i, m := range binding.Members {
				if m == member {
					binding.Members = append(binding.Members[:i], binding.Members[i+1:]...)
					break
				}
			}
			break
		}
	}

	setReq := &iampb.SetIamPolicyRequest{
		Resource: "projects/" + projectID,
		Policy:   policy,
	}
	_, err = r.ResourceManagerClient.SetIamPolicy(ctx, setReq)
	if err != nil {
		return fmt.Errorf("failed to set project IAM policy for project %s: %w", projectID, err)
	}

	return nil
}

func (r *PlatformAgentReconciler) addGSABinding(ctx context.Context, projectID string, gsaEmail string, role string, member string) error {
	resource := fmt.Sprintf("projects/%s/serviceAccounts/%s", projectID, gsaEmail)
	policy, err := r.IAMService.Projects.ServiceAccounts.GetIamPolicy(resource).Context(ctx).Do()
	if err != nil {
		return fmt.Errorf("failed to get IAM policy for GSA %s: %w", gsaEmail, err)
	}

	found := false
	for _, binding := range policy.Bindings {
		if binding.Role == role {
			for _, m := range binding.Members {
				if m == member {
					found = true
					break
				}
			}
			if !found {
				binding.Members = append(binding.Members, member)
				found = true
			}
			break
		}
	}

	if !found {
		policy.Bindings = append(policy.Bindings, &iam.Binding{
			Role:    role,
			Members: []string{member},
		})
	}

	request := &iam.SetIamPolicyRequest{
		Policy: policy,
	}
	_, err = r.IAMService.Projects.ServiceAccounts.SetIamPolicy(resource, request).Context(ctx).Do()
	if err != nil {
		return fmt.Errorf("failed to set IAM policy for GSA %s: %w", gsaEmail, err)
	}

	return nil
}

func (r *PlatformAgentReconciler) reconcileKSA(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	gsaEmail := fmt.Sprintf("%s@%s.iam.gserviceaccount.com", instance.Spec.GSAName, r.ProjectID)
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
									Value: r.ProjectID,
								},
								{
									Name:  "GOOGLE_CHAT_SUBSCRIPTION_NAME",
									Value: fmt.Sprintf("projects/%s/subscriptions/%s", r.ProjectID, instance.Spec.ChatSubName),
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

	found.Subjects = crb.Subjects
	found.RoleRef = crb.RoleRef
	return r.Update(ctx, found)
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

func (r *PlatformAgentReconciler) deleteExternalResources(ctx context.Context, instance *agentv1alpha1.PlatformAgent) error {
	log := logf.FromContext(ctx)

	// 1. Tear down Pub/Sub Subscription
	log.Info("Cleaning up cloud subscription", "id", instance.Spec.ChatSubName)
	sub := r.PubSubClient.Subscription(instance.Spec.ChatSubName)
	if exists, _ := sub.Exists(ctx); exists {
		if err := sub.Delete(ctx); err != nil {
			return fmt.Errorf("failed to delete subscription: %w", err)
		}
	}

	// 2. Tear down Pub/Sub Topic
	log.Info("Cleaning up cloud topic", "id", instance.Spec.ChatTopicName)
	topic := r.PubSubClient.Topic(instance.Spec.ChatTopicName)
	if exists, _ := topic.Exists(ctx); exists {
		if err := topic.Delete(ctx); err != nil {
			return fmt.Errorf("failed to delete topic: %w", err)
		}
	}

	// 3. Tear down GSA
	gsaEmail := fmt.Sprintf("%s@%s.iam.gserviceaccount.com", instance.Spec.GSAName, r.ProjectID)
	log.Info("Removing project-level IAM bindings from GSA", "email", gsaEmail)
	if err := r.removeProjectIAMBinding(ctx, r.ProjectID, "roles/aiplatform.user", "serviceAccount:"+gsaEmail); err != nil {
		log.Error(err, "Failed to remove project-level IAM binding for GSA during cleanup")
	}

	gsaResourceName := fmt.Sprintf("projects/%s/serviceAccounts/%s", r.ProjectID, gsaEmail)
	log.Info("Cleaning up IAM Service Account", "resource", gsaResourceName)

	_, err := r.IAMService.Projects.ServiceAccounts.Delete(gsaResourceName).Context(ctx).Do()
	if err != nil && !strings.Contains(err.Error(), "notFound") {
		return fmt.Errorf("failed to delete GSA: %w", err)
	}

	// 4. Clean up K8s ClusterRoleBinding
	crbName := instance.Namespace + "-" + instance.Name + "-cluster-viewer"
	crb := &rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{Name: crbName}}
	if err := r.Delete(ctx, crb); err != nil && !errors.IsNotFound(err) {
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

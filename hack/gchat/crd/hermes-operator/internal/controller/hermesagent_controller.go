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
	"time"

	"cloud.google.com/go/iam"
	iamadmin "cloud.google.com/go/iam/admin/apiv1"
	"cloud.google.com/go/iam/admin/apiv1/adminpb"
	"cloud.google.com/go/pubsub"
	resourcemanager "cloud.google.com/go/resourcemanager/apiv3"
	"cloud.google.com/go/resourcemanager/apiv3/resourcemanagerpb"
	secretmanager "cloud.google.com/go/secretmanager/apiv1"
	"cloud.google.com/go/secretmanager/apiv1/secretmanagerpb"
	agentv1alpha1 "github.com/gke-agentic/hermes-operator/api/v1alpha1"
	iamv1 "google.golang.org/genproto/googleapis/iam/v1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

const hermesFinalizer = "agent.hermes.io/finalizer"

func ptr[T any](v T) *T { return &v }

// HermesAgentReconciler reconciles a HermesAgent object
type HermesAgentReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// RBAC Permissions the Operator needs against the Kubernetes API
// +kubebuilder:rbac:groups=agent.hermes.io,resources=hermesagents,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agent.hermes.io,resources=hermesagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=persistentvolumeclaims;configmaps;secrets;serviceaccounts,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=agent.hermes.io,resources=hermesagents/finalizers,verbs=update

// Reconcile manages the lifecycle of the HermesAgent. It ensures GCP infrastructure,
// Kubernetes resources, and secret synchronization match the desired state defined by the user.
//
// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.23.3/pkg/reconcile
func (r *HermesAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	// 1. Fetch the HermesAgent Custom Resource
	var agent agentv1alpha1.HermesAgent
	if err := r.Get(ctx, req.NamespacedName, &agent); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// 2. Handle Deletion and Finalizer execution
	if agent.GetDeletionTimestamp() != nil {
		if controllerutil.ContainsFinalizer(&agent, hermesFinalizer) {
			logger.Info("Executing GCP Teardown...")
			if err := r.teardownGCP(ctx, &agent); err != nil {
				return ctrl.Result{}, err
			}
			controllerutil.RemoveFinalizer(&agent, hermesFinalizer)
			if err := r.Update(ctx, &agent); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{}, nil
	}

	// 3. Add Finalizer for newly created resources
	if !controllerutil.ContainsFinalizer(&agent, hermesFinalizer) {
		controllerutil.AddFinalizer(&agent, hermesFinalizer)
		if err := r.Update(ctx, &agent); err != nil {
			return ctrl.Result{}, err
		}
	}

	// 4. Provision required GCP Infrastructure (Pub/Sub, IAM)
	logger.Info("Provisioning GCP Infrastructure...")
	if err := r.provisionGCP(ctx, &agent); err != nil {
		return ctrl.Result{RequeueAfter: time.Minute}, err
	}

	// 5. Sync API Keys and other secrets from GCP Secret Manager
	logger.Info("Resolving GCP Secrets...")
	if err := r.syncSecrets(ctx, &agent); err != nil {
		return ctrl.Result{RequeueAfter: time.Minute}, err
	}

	// 6. Ensure Kubernetes Resources (Deployments, PVCs, ConfigMaps)
	logger.Info("Provisioning Kubernetes Resources...")
	if err := r.ensureK8sResources(ctx, &agent); err != nil {
		return ctrl.Result{}, err
	}

	// 7. Update Status ONLY if it has changed to prevent infinite reconciliation loops
	if agent.Status.Phase != "Ready" {
		// Fetch the absolute latest version of the resource to avoid "Object Modified" conflicts
		latestAgent := &agentv1alpha1.HermesAgent{}
		if err := r.Get(ctx, req.NamespacedName, latestAgent); err == nil {
			latestAgent.Status.Phase = "Ready"
			if err := r.Status().Update(ctx, latestAgent); err != nil {
				logger.Error(err, "Failed to update HermesAgent status")
				return ctrl.Result{}, err
			}
		} else {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{}, nil
}

// =====================================================================
// GCP SDK OPERATIONS
// =====================================================================

// provisionGCP ensures the existence of all external cloud resources required
// by the Hermes Gateway, including Pub/Sub topics/subscriptions, Service Accounts,
// and required IAM bindings.
func (r *HermesAgentReconciler) provisionGCP(ctx context.Context, agent *agentv1alpha1.HermesAgent) error {
	// 1. Setup Pub/Sub Topic & Subscription
	pubsubClient, err := pubsub.NewClient(ctx, agent.Spec.ProjectID)
	if err != nil {
		return err
	}
	defer pubsubClient.Close()

	topic := pubsubClient.Topic(agent.Spec.ChatTopicName)
	if exists, _ := topic.Exists(ctx); !exists {
		// Reassign err but not topic to prevent overwriting 'topic' with nil if it already exists
		_, err = pubsubClient.CreateTopic(ctx, agent.Spec.ChatTopicName)
		if err != nil && !strings.Contains(err.Error(), "AlreadyExists") {
			return err
		}
	}

	sub := pubsubClient.Subscription(agent.Spec.ChatSubName)
	if exists, _ := sub.Exists(ctx); !exists {
		_, err = pubsubClient.CreateSubscription(ctx, agent.Spec.ChatSubName, pubsub.SubscriptionConfig{
			Topic: topic, AckDeadline: 60 * time.Second, RetentionDuration: 7 * 24 * time.Hour,
		})
		// Safely ignore AlreadyExists errors to maintain idempotency
		if err != nil && !strings.Contains(err.Error(), "AlreadyExists") {
			return err
		}
	}

	// 2. Setup IAM Service Account
	iamClient, err := iamadmin.NewIamClient(ctx)
	if err != nil {
		return err
	}
	defer iamClient.Close()

	saEmail := fmt.Sprintf("%s@%s.iam.gserviceaccount.com", agent.Spec.GSAName, agent.Spec.ProjectID)
	saName := fmt.Sprintf("projects/%s/serviceAccounts/%s", agent.Spec.ProjectID, saEmail)

	if _, err := iamClient.GetServiceAccount(ctx, &adminpb.GetServiceAccountRequest{Name: saName}); err != nil {
		_, err = iamClient.CreateServiceAccount(ctx, &adminpb.CreateServiceAccountRequest{
			Name: "projects/" + agent.Spec.ProjectID, AccountId: agent.Spec.GSAName,
			ServiceAccount: &adminpb.ServiceAccount{DisplayName: "Hermes Chat Bot"},
		})
		// Safely ignore AlreadyExists errors to maintain idempotency
		if err != nil && !strings.Contains(err.Error(), "AlreadyExists") {
			return err
		}
	}

	// 3. Create the Workload Identity Bridge (IAM Binding)
	workloadIdentityMember := fmt.Sprintf("serviceAccount:%s.svc.id.goog[%s/%s]", agent.Spec.ProjectID, agent.Namespace, agent.Spec.KSAName)

	// Note: GetIamPolicy uses the iamv1 Request, but returns a high-level *iam.Policy wrapper
	policy, err := iamClient.GetIamPolicy(ctx, &iamv1.GetIamPolicyRequest{
		Resource: saName,
	})

	if err == nil {
		role := iam.RoleName("roles/iam.workloadIdentityUser")

		// Check if the member already has the role using the wrapper's helper method
		if !policy.HasRole(workloadIdentityMember, role) {
			// Use the built-in Add() method instead of modifying bindings manually for safety
			policy.Add(workloadIdentityMember, role)

			// Note: SetIamPolicyRequest is located in the iamadmin wrapper package, not adminpb
			if _, err := iamClient.SetIamPolicy(ctx, &iamadmin.SetIamPolicyRequest{
				Resource: saName,
				Policy:   policy,
			}); err != nil {
				return err
			}
		}
	} else if !strings.Contains(err.Error(), "NotFound") {
		return err
	}

	// ===================================================================
	// 4. Provision Missing Pub/Sub and Resource Manager IAM Policies
	// ===================================================================

	// A. Fetch Project via Resource Manager to get the numeric Project Number
	rmClient, err := resourcemanager.NewProjectsClient(ctx)
	if err != nil {
		return err
	}
	defer rmClient.Close()

	proj, err := rmClient.GetProject(ctx, &resourcemanagerpb.GetProjectRequest{
		Name: "projects/" + agent.Spec.ProjectID,
	})
	if err != nil {
		return err
	}
	projectNumber := strings.TrimPrefix(proj.Name, "projects/")
	botSA := fmt.Sprintf("serviceAccount:%s@%s.iam.gserviceaccount.com", agent.Spec.GSAName, agent.Spec.ProjectID)

	// B. Pub/Sub Subscription Bindings (Grants your Bot permission to Pull messages)
	subPolicy, err := sub.IAM().Policy(ctx)
	if err == nil {
		subPolicy.Add(botSA, "roles/pubsub.subscriber")
		subPolicy.Add(botSA, "roles/pubsub.viewer")
		_ = sub.IAM().SetPolicy(ctx, subPolicy)
	}

	// C. Pub/Sub Topic Bindings (Grants Google Chat backend permission to Push messages)
	topicPolicy, err := topic.IAM().Policy(ctx)
	if err == nil {
		topicPolicy.Add("serviceAccount:chat-api-push@system.gserviceaccount.com", "roles/pubsub.publisher")
		chatSystemSA := fmt.Sprintf("serviceAccount:service-%s@gcp-sa-gsuiteaddons.iam.gserviceaccount.com", projectNumber)
		topicPolicy.Add(chatSystemSA, "roles/pubsub.publisher")
		_ = topic.IAM().SetPolicy(ctx, topicPolicy)
	}

	// D. Project Bindings for AI Platform (Allows Bot to use Vertex AI/Gemini natively)
	projPolicy, err := rmClient.GetIamPolicy(ctx, &iamv1.GetIamPolicyRequest{Resource: proj.Name})
	if err == nil {
		roleFound := false
		for _, b := range projPolicy.Bindings {
			if b.Role == "roles/aiplatform.user" {
				for _, m := range b.Members {
					if m == botSA {
						roleFound = true
						break
					}
				}
				if !roleFound {
					b.Members = append(b.Members, botSA)
					roleFound = true
				}
				break
			}
		}
		if !roleFound {
			projPolicy.Bindings = append(projPolicy.Bindings, &iamv1.Binding{
				Role:    "roles/aiplatform.user",
				Members: []string{botSA},
			})
		}
		_, _ = rmClient.SetIamPolicy(ctx, &iamv1.SetIamPolicyRequest{
			Resource: proj.Name,
			Policy:   projPolicy,
		})
	}

	return nil
}

// teardownGCP cleans up external cloud resources when the Custom Resource is deleted.
func (r *HermesAgentReconciler) teardownGCP(ctx context.Context, agent *agentv1alpha1.HermesAgent) error {
	if pubsubClient, err := pubsub.NewClient(ctx, agent.Spec.ProjectID); err == nil {
		defer pubsubClient.Close()
		_ = pubsubClient.Subscription(agent.Spec.ChatSubName).Delete(ctx)
		_ = pubsubClient.Topic(agent.Spec.ChatTopicName).Delete(ctx)
	}
	if iamClient, err := iamadmin.NewIamClient(ctx); err == nil {
		defer iamClient.Close()
		saName := fmt.Sprintf("projects/%s/serviceAccounts/%s@%s.iam.gserviceaccount.com", agent.Spec.ProjectID, agent.Spec.GSAName, agent.Spec.ProjectID)
		_ = iamClient.DeleteServiceAccount(ctx, &adminpb.DeleteServiceAccountRequest{Name: saName})
	}
	return nil
}

// syncSecrets retrieves secure payloads from Google Secret Manager and hydrates
// them into a local Kubernetes Secret to be mounted by the deployment pods.
func (r *HermesAgentReconciler) syncSecrets(ctx context.Context, agent *agentv1alpha1.HermesAgent) error {
	smClient, err := secretmanager.NewClient(ctx)
	if err != nil {
		return err
	}
	defer smClient.Close()

	getSecret := func(name string) []byte {
		req := &secretmanagerpb.AccessSecretVersionRequest{
			Name: fmt.Sprintf("projects/%s/secrets/%s/versions/latest", agent.Spec.ProjectID, name),
		}
		res, err := smClient.AccessSecretVersion(ctx, req)
		if err != nil {
			return nil
		}
		return res.Payload.Data
	}

	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "hermes-secrets", Namespace: agent.Namespace}}
	_, err = controllerutil.CreateOrUpdate(ctx, r.Client, secret, func() error {
		if secret.Data == nil {
			secret.Data = make(map[string][]byte)
		}
		if gcpKey := getSecret("GCP_API_KEY"); gcpKey != nil {
			secret.Data["GCP_API_KEY"] = gcpKey
		}
		if gemKey := getSecret("GEMINI_API_KEY"); gemKey != nil {
			secret.Data["GEMINI_API_KEY"] = gemKey
		}
		return controllerutil.SetControllerReference(agent, secret, r.Scheme)
	})
	return err
}

// =====================================================================
// KUBERNETES OPERATIONS
// =====================================================================

// ensureK8sResources provisions the standard workloads and configurations
// needed to run the Hermes agent within the cluster.
func (r *HermesAgentReconciler) ensureK8sResources(ctx context.Context, agent *agentv1alpha1.HermesAgent) error {
	// 1. Workload Identity Service Account
	sa := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: agent.Spec.KSAName, Namespace: agent.Namespace}}
	_, _ = controllerutil.CreateOrUpdate(ctx, r.Client, sa, func() error {
		if sa.Annotations == nil {
			sa.Annotations = make(map[string]string)
		}
		sa.Annotations["iam.gke.io/gcp-service-account"] = fmt.Sprintf("%s@%s.iam.gserviceaccount.com", agent.Spec.GSAName, agent.Spec.ProjectID)
		return controllerutil.SetControllerReference(agent, sa, r.Scheme)
	})

	// 2. ConfigMap
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "hermes-config", Namespace: agent.Namespace}}
	_, _ = controllerutil.CreateOrUpdate(ctx, r.Client, cm, func() error {
		cm.Data = map[string]string{"config.yaml": `model:
  default: "gemini-3.1-flash-lite"
  provider: "gemini"
terminal:
  backend: "local"
  cwd: "/opt/data"
gateway:
  platforms:
    google_chat:
      enabled: true`}
		return controllerutil.SetControllerReference(agent, cm, r.Scheme)
	})

	// 3. Persistent Volume Claim
	pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: "hermes-data", Namespace: agent.Namespace}}
	_, _ = controllerutil.CreateOrUpdate(ctx, r.Client, pvc, func() error {
		if pvc.CreationTimestamp.IsZero() {
			pvc.Spec.AccessModes = []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}
			pvc.Spec.Resources = corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("10Gi")}}
		}
		return controllerutil.SetControllerReference(agent, pvc, r.Scheme)
	})

	// 4. Deployment
	deploy := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "hermes-gateway", Namespace: agent.Namespace}}
	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, deploy, func() error {
		deploy.Spec.Replicas = ptr[int32](1)
		deploy.Spec.Strategy.Type = appsv1.RecreateDeploymentStrategyType
		deploy.Spec.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{"app": "hermes-gateway"}}

		deploy.Spec.Template = corev1.PodTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "hermes-gateway"}},
			Spec: corev1.PodSpec{
				ServiceAccountName: agent.Spec.KSAName,
				SecurityContext:    &corev1.PodSecurityContext{FSGroup: ptr[int64](10000)},
				// Use an init container to copy the ConfigMap into the persistent volume,
				// ensuring the hermes runtime has write access to its configuration files.
				InitContainers: []corev1.Container{{
					Name:    "init-config",
					Image:   "busybox:latest",
					Command: []string{"sh", "-c", "cp /tmp/config.yaml /opt/data/config.yaml && chown 10000:10000 /opt/data/config.yaml"},
					VolumeMounts: []corev1.VolumeMount{
						{Name: "hermes-data-vol", MountPath: "/opt/data"},
						{Name: "hermes-config-vol", MountPath: "/tmp/config.yaml", SubPath: "config.yaml"},
					},
				}},
				Containers: []corev1.Container{{
					Name: "hermes", Image: agent.Spec.ImageURI, ImagePullPolicy: corev1.PullAlways,
					Args:  []string{"hermes", "gateway", "run"},
					Ports: []corev1.ContainerPort{{ContainerPort: 9119}},
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
					Env: []corev1.EnvVar{
						{Name: "S6_KEEP_ENV", Value: "1"},
						{Name: "HERMES_HOME", Value: "/opt/data"},
						{Name: "HERMES_DASHBOARD", Value: "1"},
						{Name: "GATEWAY_ALLOW_ALL_USERS", Value: "true"},
						{Name: "GOOGLE_CHAT_ENABLED", Value: "true"},
						{Name: "GOOGLE_CHAT_PROJECT_ID", Value: agent.Spec.ProjectID},
						{Name: "GOOGLE_CHAT_SUBSCRIPTION_NAME", Value: fmt.Sprintf("projects/%s/subscriptions/%s", agent.Spec.ProjectID, agent.Spec.ChatSubName)},
						{Name: "GOOGLE_CHAT_ALLOWED_USERS", Value: agent.Spec.GoogleChatAllowedUsers},
						{Name: "GOOGLE_CHAT_HOME_CHANNEL", Value: agent.Spec.GoogleChatHomeChannel},
						{Name: "GEMINI_API_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "hermes-secrets"}, Key: "GEMINI_API_KEY", Optional: ptr(true)}}},
						{Name: "GCP_API_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "hermes-secrets"}, Key: "GCP_API_KEY", Optional: ptr(true)}}},
					},
					VolumeMounts: []corev1.VolumeMount{
						{Name: "hermes-data-vol", MountPath: "/opt/data"},
					},
				}},
				Volumes: []corev1.Volume{
					{Name: "hermes-data-vol", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "hermes-data"}}},
					{Name: "hermes-config-vol", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: "hermes-config"}}}},
				},
			},
		}
		return controllerutil.SetControllerReference(agent, deploy, r.Scheme)
	})
	return err
}

// SetupWithManager sets up the controller with the Manager.
func (r *HermesAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.HermesAgent{}).
		Named("hermesagent").
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Secret{}).
		Complete(r)
}

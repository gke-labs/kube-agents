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

package webhook

import (
	"context"
	"fmt"
	"slices"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/controller"
)

// PreventDeletionAnnotation blocks deletion when set to "true".
// Note that this serves as an accidental-deletion guardrail rather than an authorization control,
// as a principal with update permissions can remove the annotation before deleting.
const PreventDeletionAnnotation = "kubeagents.x-k8s.io/prevent-deletion"

// DefaultPort is the port the webhook server binds to unless --webhook-port says otherwise.
//
// It is 10250 rather than controller-runtime's 9443 because GKE's automatic
// control-plane-to-node firewall rule permits only tcp:443 and tcp:10250. On a private
// cluster the API server dials the endpoint pod IP on the Service's targetPort, so a
// webhook on any other port is unreachable until someone adds a VPC firewall rule per
// cluster — and with failurePolicy=Fail an unreachable webhook blocks every PlatformAgent
// create, update, and delete. 10250 is the kubelet's port, but the kubelet binds it on the
// node IP in a separate network namespace, so a pod listening on 10250 does not collide.
//
// The manifests must agree with this value: config/manager/manager.yaml sets it as the
// container port and config/webhook/service.yaml as the Service targetPort. A mismatch
// reproduces exactly the outage described above, so TestWebhookPortsMatchDefault guards it.
const DefaultPort = 10250

// The two refusals validateBusCredentialSource makes, one per route in
// agentv1alpha1.BusCredentialRoutes. Each names the thing matched, so the
// author reading the field error knows which line to change and why.
const (
	busTokenAudienceForbiddenFmt = "volume %q projects a serviceAccountToken for audience %q, which is the A2A bus token audience; the operator projects that token for the platform-agent container alone" // #nosec G101 -- Error message format, not a credential
	busCredsSecretForbiddenFmt   = "volume %q mounts Secret %q, which the operator renders with A2A bus credentials for its own workloads; it may not be mounted by the CR"                                 // #nosec G101 -- Error message format, not a credential
	forgeCredsSecretForbiddenFmt = "volume %q mounts Secret %q, which is declared as a forge credentialsRef Secret for the credential proxy alone; it may not be mounted by the CR"                         // #nosec G101 -- Error message format, not a credential
	forgeCredsEnvForbiddenFmt    = "container %q references Secret %q, which is declared as a forge credentialsRef Secret for the credential proxy alone; it may not be referenced by the CR"               // #nosec G101 -- Error message format, not a credential
)

// The data volume size checks on spec.storage.size. A PersistentVolumeClaim
// cannot shrink, so a lower size is refused. Removing the field is allowed with
// a warning, because the operator never shrinks a claim and an upgrade can drop
// a key the target chart does not declare.
const (
	storageSizeNotPositiveMsg = "the data volume size must be greater than zero"
	storageSizeTooLargeFmt    = "the data volume size must be at most %s, the largest persistent disk size"
	storageSizeShrinkFmt      = "cannot lower the data volume size from %[1]s to %[2]s: a PersistentVolumeClaim cannot shrink. Keep %[1]s or raise it."
	storageSizeRemovedWarnFmt = "spec.storage.size was removed, so the data volume size goes from %s to the operator default %s. Existing data claims keep their current size, because a PersistentVolumeClaim cannot shrink; only new claims get the default."
)

// maxStorageSize is the largest spec.storage.size: 64TiB is the largest
// persistent disk. A larger value is a typo (Ei for Gi, say) that would patch
// both claims to a size no disk can reach, and a claim cannot shrink back.
var maxStorageSize = resource.MustParse("64Ti")

// restrictedServiceAccounts is the set of high-privilege service account names forbidden in PlatformAgent spec.
var restrictedServiceAccounts = map[string]struct{}{
	"cluster-admin": {},
	"system:admin":  {},
}

// log is for logging in this package.
var platformagentlog = logf.Log.WithName("platformagent-resource")

// SetupPlatformAgentWebhookWithManager registers the webhook for PlatformAgent in the manager.
func SetupPlatformAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr, &agentv1alpha1.PlatformAgent{}).
		WithDefaulter(&PlatformAgentCustomDefaulter{}).
		WithValidator(&PlatformAgentCustomValidator{
			Client: mgr.GetAPIReader(),
		}).
		Complete()
}

// +kubebuilder:webhook:path=/mutate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=true,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update,versions=v1alpha1,name=mplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomDefaulter struct to implement admission.Defaulter.
type PlatformAgentCustomDefaulter struct{}

var _ admission.Defaulter[*agentv1alpha1.PlatformAgent] = &PlatformAgentCustomDefaulter{}

// Default implements admission.Defaulter so a webhook will be registered for the type PlatformAgent.
func (d *PlatformAgentCustomDefaulter) Default(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) error {
	platformagentlog.Info("defaulting PlatformAgent", "name", platformAgent.Name)

	if platformAgent.Spec.Deployment != nil {
		// Tag is deliberately not defaulted: persisting "latest" would be
		// misleading when Image is omitted and the operator falls back to its
		// default platform-agent version (see resolveAgentImage).
		if platformAgent.Spec.Deployment.ImagePullPolicy == nil || *platformAgent.Spec.Deployment.ImagePullPolicy == "" {
			platformAgent.Spec.Deployment.ImagePullPolicy = ptr.To(corev1.PullIfNotPresent)
		}
	}
	if platformAgent.Spec.Harness != nil {
		if platformAgent.Spec.Harness.Memory == nil {
			platformAgent.Spec.Harness.Memory = &agentv1alpha1.MemorySpec{}
		}
		if platformAgent.Spec.Harness.Memory.UserProfileEnabled == nil {
			platformAgent.Spec.Harness.Memory.UserProfileEnabled = ptr.To(false)
		}
	}

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update;delete,versions=v1alpha1,name=vplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomValidator struct to implement admission.Validator.
type PlatformAgentCustomValidator struct {
	Client client.Reader
}

var _ admission.Validator[*agentv1alpha1.PlatformAgent] = &PlatformAgentCustomValidator{}

// ValidateCreate implements admission.Validator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateCreate(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	platformagentlog.Info("validating PlatformAgent creation", "name", platformAgent.Name)

	return v.validatePlatformAgent(ctx, nil, platformAgent)
}

// ValidateUpdate implements admission.Validator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateUpdate(ctx context.Context, oldObj, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	platformagentlog.Info("validating PlatformAgent update", "name", platformAgent.Name)

	return v.validatePlatformAgent(ctx, oldObj, platformAgent)
}

// validatePlatformAgent checks platformAgent. oldObj is the stored object on an
// update and nil on a create; only the checks that compare the two read it.
func (v *PlatformAgentCustomValidator) validatePlatformAgent(ctx context.Context, oldObj, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	// Skip validation for terminating agents to avoid deadlocks during deletion (e.g. finalizer removal)
	if platformAgent.DeletionTimestamp != nil {
		return nil, nil
	}

	var allErrs field.ErrorList
	var warnings admission.Warnings

	// 1. Enforce 1 PlatformAgent per cluster limit (enforced at cluster level on the Hub/Management cluster)
	if v.Client != nil {
		var list agentv1alpha1.PlatformAgentList
		if err := v.Client.List(ctx, &list); err != nil {
			return nil, err
		}
		for _, item := range list.Items {
			// Skip terminating agents to prevent deadlocking new platformagent deployment
			if item.DeletionTimestamp != nil {
				continue
			}
			if item.Name != platformAgent.Name || item.Namespace != platformAgent.Namespace {
				allErrs = append(allErrs, field.Forbidden(field.NewPath(""), "only one PlatformAgent is allowed per cluster"))
				break
			}
		}
	}

	// 2. Validate Deployment Security Constraints
	if platformAgent.Spec.Deployment != nil {
		depPath := field.NewPath("spec", "deployment")

		// 2a. Validate sensitive environment variable overrides and forge credential Secret references
		allErrs = append(allErrs, validateForgeCredentialEnvVars("platform-agent", platformAgent.Spec.Deployment.Env, platformAgent.Spec.Integration, depPath.Child("env"))...)
		for i, env := range platformAgent.Spec.Deployment.Env {
			if _, isSensitive := agentv1alpha1.SensitiveEnvVars[env.Name]; isSensitive {
				allErrs = append(allErrs, field.Forbidden(
					depPath.Child("env").Index(i).Child("name"),
					fmt.Sprintf("overriding sensitive environment variable %q is forbidden", env.Name),
				))
			}
		}

		// 2b. Validate InitContainers security context
		for i := range platformAgent.Spec.Deployment.InitContainers {
			initPath := depPath.Child("initContainers").Index(i)
			allErrs = append(allErrs, validateContainerSecurity(platformAgent.Spec.Deployment.InitContainers[i].SecurityContext, initPath)...)
			allErrs = append(allErrs, validateReservedVolumeMounts(platformAgent.Spec.Deployment.InitContainers[i].VolumeMounts, initPath.Child("volumeMounts"))...)
			allErrs = append(allErrs, validateForgeCredentialContainerEnv(platformAgent.Spec.Deployment.InitContainers[i], platformAgent.Spec.Integration, initPath)...)
		}

		// 2c. Validate Sidecars security context
		for i := range platformAgent.Spec.Deployment.Sidecars {
			sidecarPath := depPath.Child("sidecars").Index(i)
			allErrs = append(allErrs, validateContainerSecurity(platformAgent.Spec.Deployment.Sidecars[i].SecurityContext, sidecarPath)...)
			allErrs = append(allErrs, validateReservedVolumeMounts(platformAgent.Spec.Deployment.Sidecars[i].VolumeMounts, sidecarPath.Child("volumeMounts"))...)
			allErrs = append(allErrs, validateForgeCredentialContainerEnv(platformAgent.Spec.Deployment.Sidecars[i], platformAgent.Spec.Integration, sidecarPath)...)
		}

		// 2d. Validate ExtraVolumes & SidecarVolumes (hostPath forbidden)
		for i, vol := range platformAgent.Spec.Deployment.ExtraVolumes {
			if vol.HostPath != nil {
				allErrs = append(allErrs, field.Forbidden(
					depPath.Child("extraVolumes").Index(i).Child("hostPath"),
					"hostPath volumes are forbidden for security reasons",
				))
			}
			allErrs = append(allErrs, validateReservedVolumeName(vol.Name, depPath.Child("extraVolumes").Index(i).Child("name"))...)
			allErrs = append(allErrs, validateBusCredentialSource(vol, platformAgent.Name, depPath.Child("extraVolumes").Index(i))...)
			allErrs = append(allErrs, validateForgeCredentialSource(vol, platformAgent.Spec.Integration, depPath.Child("extraVolumes").Index(i))...)
		}
		for i, vol := range platformAgent.Spec.Deployment.SidecarVolumes {
			if vol.HostPath != nil {
				allErrs = append(allErrs, field.Forbidden(
					depPath.Child("sidecarVolumes").Index(i).Child("hostPath"),
					"hostPath volumes are forbidden for security reasons",
				))
			}
			allErrs = append(allErrs, validateReservedVolumeName(vol.Name, depPath.Child("sidecarVolumes").Index(i).Child("name"))...)
			allErrs = append(allErrs, validateBusCredentialSource(vol, platformAgent.Name, depPath.Child("sidecarVolumes").Index(i))...)
			allErrs = append(allErrs, validateForgeCredentialSource(vol, platformAgent.Spec.Integration, depPath.Child("sidecarVolumes").Index(i))...)
		}

		// 2da. The fifth user-authored mount surface. Unlike the four above it
		// names no container of its own: buildBaseContainers appends this list
		// verbatim to the platform-agent container AND to
		// platform-agent-dashboard, so a reserved name here reaches a second
		// container without the CR ever mentioning one.
		allErrs = append(allErrs, validateReservedVolumeMounts(platformAgent.Spec.Deployment.ExtraVolumeMounts, depPath.Child("extraVolumeMounts"))...)

		// 2e. Validate ImagePullSecrets name a Secret, each of them exactly once.
		// Neither shape is caught anywhere below: corev1.LocalObjectReference
		// makes Name optional, so the CRD schema admits `- {}` and `- name: ""`,
		// and core PodSpec validation does not reliably reject them either — on
		// GKE 1.35.6 an empty name is a warning rather than an error. The kubelet
		// then looks for a Secret named "", fails, and pulls anonymously, so the
		// agent lands in ImagePullBackOff against a CR that looks like it
		// configured a pull identity. A repeat fails further away still:
		// PodSpec.imagePullSecrets is a server-side-apply list-map keyed on name,
		// so two identical entries make every apply of the generated Deployment
		// fail with `duplicate entries for key` — a reconcile error on an object
		// the author never wrote. The controller normalizes both away for installs
		// running without this webhook (resolveImagePullSecrets, which the chart's
		// default leaves as the only line of defence); rejecting here puts the
		// error on the object that has the typo.
		seenPullSecrets := make(map[string]struct{}, len(platformAgent.Spec.Deployment.ImagePullSecrets))
		for i, ref := range platformAgent.Spec.Deployment.ImagePullSecrets {
			namePath := depPath.Child("imagePullSecrets").Index(i).Child("name")
			name := strings.TrimSpace(ref.Name)
			if name == "" {
				allErrs = append(allErrs, field.Required(
					namePath,
					"an imagePullSecrets entry must name a Secret in the agent's namespace",
				))
				continue
			}
			if _, dup := seenPullSecrets[name]; dup {
				allErrs = append(allErrs, field.Duplicate(namePath, name))
				continue
			}
			seenPullSecrets[name] = struct{}{}
		}

		// 2f. The credential-proxy container's requests and limits, checked as
		// the operator will render them rather than as written: the override is
		// merged over the defaults per key, so a CR that raises only
		// requests.memory collides with a limit it never wrote. The reconciler
		// runs the same function before it writes the proxy Deployment, so this
		// refuses at apply what the operator refuses at reconcile anyway.
		proxyErrs, proxyWarnings := controller.ValidateCredentialProxyResources(platformAgent.Spec.Deployment, depPath.Child("credentialProxy", "resources"))
		allErrs = append(allErrs, proxyErrs...)
		warnings = append(warnings, proxyWarnings...)

		// 2g. The agent-api-auth sidecar's requests and limits, checked the same
		// way: merged over the defaults per key, so the reconciler refuses at
		// reconcile what this refuses at apply.
		apiAuthErrs, apiAuthWarnings := controller.ValidateAgentAPIAuthResources(platformAgent.Spec.Deployment, depPath.Child("agentAPIAuth", "resources"))
		allErrs = append(allErrs, apiAuthErrs...)
		warnings = append(warnings, apiAuthWarnings...)

		// 2h. The exec tier's objects are named after the agent, so a name too
		// long for them is refused while the tier is on.
		allErrs = append(allErrs, controller.ValidateCredentialProxyExec(platformAgent, field.NewPath("metadata", "name"))...)
	}

	// 3. Validate Security ServiceAccountName
	// Note: This check serves as a name-based tripwire against obvious misconfigurations
	// (e.g., binding to literal names like "cluster-admin" or "system:admin"). It is NOT
	// full security enforcement against privileged ServiceAccounts. Genuine RBAC enforcement
	// requires inspecting RoleBinding / ClusterRoleBinding resources, which is controller
	// and admission-policy territory to avoid time-of-check to time-of-use (TOCTOU) issues
	// at webhook admission time.
	if platformAgent.Spec.Security != nil && platformAgent.Spec.Security.ServiceAccountName != "" {
		sa := platformAgent.Spec.Security.ServiceAccountName
		if _, isRestricted := restrictedServiceAccounts[sa]; isRestricted {
			allErrs = append(allErrs, field.Forbidden(
				field.NewPath("spec", "security", "serviceAccountName"),
				fmt.Sprintf("binding to privileged service account %q is forbidden", sa),
			))
		}
		// The name the broker and its Workload Identity binding run as must not
		// be one the operator mints for another workload: the gateway, the A2A
		// session pods and the rest would then share the broker's cloud
		// identity, and two reconcilers would fight over one ServiceAccount.
		if msg := controller.ReservedServiceAccountOverride(platformAgent); msg != "" {
			allErrs = append(allErrs, field.Forbidden(field.NewPath("spec", "security", "serviceAccountName"), msg))
		}
	}

	// 4. Validate the forge integration. Each forge and each repository on it is
	// checked by the declared provider's own rules rather than by GitHub's
	// applied to everyone. `spec.integration.github` is a deprecated alias that
	// resolves to the same declaration, so the field paths below are rendered in
	// whichever spelling was written.
	gitErrs, gitWarnings := validateGitIntegration(platformAgent.Spec.Integration)
	allErrs = append(allErrs, gitErrs...)
	warnings = append(warnings, gitWarnings...)

	// 5. The data volume size: above zero, at most maxStorageSize, and on an
	// update never lower than the size the stored object resolves to.
	storageErrs, storageWarnings := validateStorageSize(oldObj, platformAgent)
	allErrs = append(allErrs, storageErrs...)
	warnings = append(warnings, storageWarnings...)

	if len(allErrs) > 0 {
		return warnings, apierrors.NewInvalid(
			schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
			platformAgent.Name,
			allErrs,
		)
	}

	return warnings, nil
}

// validateStorageSize refuses a spec.storage.size of zero or less or above
// maxStorageSize, and on an update (oldObj not nil) an explicit size lower
// than the one the stored object resolves to. Both sides go through controller.ResolveDataStorageSize, so an
// absent field compares as the operator default. A removed field is allowed
// with a warning when the default is lower than the stored size: the operator
// never shrinks a claim, so the claims keep their size.
func validateStorageSize(oldObj, platformAgent *agentv1alpha1.PlatformAgent) (field.ErrorList, admission.Warnings) {
	sizePath := field.NewPath("spec", "storage", "size")
	newSize := storageSize(platformAgent)
	// A value the stored object already carries passes the range checks, so a
	// CR written while the webhook was off stays open to every other update.
	unchanged := oldObj != nil && sameStorageSize(storageSize(oldObj), newSize)
	if newSize != nil && !unchanged && newSize.Sign() <= 0 {
		return field.ErrorList{field.Invalid(sizePath, newSize.String(), storageSizeNotPositiveMsg)}, nil
	}
	if newSize != nil && !unchanged && newSize.Cmp(maxStorageSize) > 0 {
		return field.ErrorList{field.Invalid(sizePath, newSize.String(), fmt.Sprintf(storageSizeTooLargeFmt, maxStorageSize.String()))}, nil
	}
	if oldObj == nil {
		return nil, nil
	}
	oldResolved := controller.ResolveDataStorageSize(oldObj)
	// A stored size above the limit compares as the limit, so lowering it to a
	// size the webhook admits is not refused as a shrink.
	if oldResolved.Cmp(maxStorageSize) > 0 {
		oldResolved = maxStorageSize.DeepCopy()
	}
	newResolved := controller.ResolveDataStorageSize(platformAgent)
	if newResolved.Cmp(oldResolved) >= 0 {
		return nil, nil
	}
	if newSize == nil {
		return nil, admission.Warnings{fmt.Sprintf(storageSizeRemovedWarnFmt, oldResolved.String(), newResolved.String())}
	}
	return field.ErrorList{field.Forbidden(sizePath, fmt.Sprintf(storageSizeShrinkFmt, oldResolved.String(), newResolved.String()))}, nil
}

// sameStorageSize reports whether a and b are both absent or the same quantity.
func sameStorageSize(a, b *resource.Quantity) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return a.Cmp(*b) == 0
}

// storageSize is spec.storage.size as written, or nil when it is absent.
func storageSize(platformAgent *agentv1alpha1.PlatformAgent) *resource.Quantity {
	if platformAgent.Spec.Storage == nil {
		return nil
	}
	return platformAgent.Spec.Storage.Size
}

// validateReservedVolumeMounts refuses a user-authored container that mounts a
// volume the operator renders for one specific container of its own: the
// projected bus token and the watchers' broker token. agentv1alpha1.ReservedVolumeNames
// says what that buys and why the render strips them as well as this rejecting
// them.
// path is the mount LIST's own path, not the container's: the three callers
// name three different fields (a container's volumeMounts under initContainers
// or sidecars, and spec.deployment.extraVolumeMounts, which hangs off the
// deployment directly), and a helper that appended "volumeMounts" itself would
// have reported the third one at a field that does not exist.
func validateReservedVolumeMounts(mounts []corev1.VolumeMount, path *field.Path) field.ErrorList {
	var errs field.ErrorList
	for i, m := range mounts {
		_, reserved := agentv1alpha1.ReservedVolumeNames[m.Name]
		if !reserved && !agentv1alpha1.IsForgeCredentialVolumeName(m.Name) {
			continue
		}
		errs = append(errs, field.Forbidden(
			path.Index(i).Child("name"),
			fmt.Sprintf("volume %q is rendered by the operator for a single container and may not be mounted here", m.Name),
		))
	}
	return errs
}

// validateReservedVolumeName refuses a user-supplied volume that shadows one of
// those names. Two volumes with one name is a Deployment server-side apply
// rejects outright, so this is a wedged-reconcile guard as much as a credential
// one.
func validateReservedVolumeName(name string, path *field.Path) field.ErrorList {
	_, reserved := agentv1alpha1.ReservedVolumeNames[name]
	if !reserved && !agentv1alpha1.IsForgeCredentialVolumeName(name) {
		return nil
	}
	return field.ErrorList{field.Forbidden(
		path, fmt.Sprintf("volume name %q is reserved by the operator", name),
	)}
}

func validateForgeCredentialSource(vol corev1.Volume, integration *agentv1alpha1.PlatformAgentIntegrationSpec, path *field.Path) field.ErrorList {
	if integration == nil {
		return nil
	}
	var errs field.ErrorList
	for _, route := range integration.ForgeCredentialRoutes(vol) {
		at := path.Child("secret", "secretName")
		if route.Source != agentv1alpha1.BusCredentialRouteVolumeSource {
			at = path.Child("projected", "sources").Index(route.Source).Child("secret", "name")
		}
		errs = append(errs, field.Forbidden(at, fmt.Sprintf(forgeCredsSecretForbiddenFmt, vol.Name, route.Secret)))
	}
	return errs
}

func validateForgeCredentialEnvVars(containerName string, envVars []corev1.EnvVar, integration *agentv1alpha1.PlatformAgentIntegrationSpec, path *field.Path) field.ErrorList {
	if integration == nil {
		return nil
	}
	forgeSecrets := integration.ForgeCredentialSecretNames()
	if len(forgeSecrets) == 0 {
		return nil
	}
	var errs field.ErrorList
	for i, e := range envVars {
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && slices.Contains(forgeSecrets, e.ValueFrom.SecretKeyRef.Name) {
			errs = append(errs, field.Forbidden(
				path.Index(i).Child("valueFrom", "secretKeyRef", "name"),
				fmt.Sprintf(forgeCredsEnvForbiddenFmt, containerName, e.ValueFrom.SecretKeyRef.Name),
			))
		}
	}
	return errs
}

func validateForgeCredentialContainerEnv(container corev1.Container, integration *agentv1alpha1.PlatformAgentIntegrationSpec, path *field.Path) field.ErrorList {
	if integration == nil {
		return nil
	}
	errs := validateForgeCredentialEnvVars(container.Name, container.Env, integration, path.Child("env"))
	forgeSecrets := integration.ForgeCredentialSecretNames()
	if len(forgeSecrets) == 0 {
		return errs
	}
	for i, ef := range container.EnvFrom {
		if ef.SecretRef != nil && slices.Contains(forgeSecrets, ef.SecretRef.Name) {
			errs = append(errs, field.Forbidden(
				path.Child("envFrom").Index(i).Child("secretRef", "name"),
				fmt.Sprintf(forgeCredsEnvForbiddenFmt, container.Name, ef.SecretRef.Name),
			))
		}
	}
	return errs
}

// validateBusCredentialSource refuses a user-supplied volume whose SOURCE
// would deliver the A2A bus credential, whatever the volume is called: a
// projected serviceAccountToken for the bus audience, or one of the Secrets
// the operator renders with bus credentials in them, as a `secret` volume or
// a projected `secret` source. Volumes only; env is not checked, and
// BusCredentialRoutes says why. validateReservedVolumeName above is the name half of the same
// reservation; agentv1alpha1.BusCredentialRoutes is the source half, and the
// render strips what this refuses, because the chart's default failurePolicy
// is Ignore. The error lands on the field that matched, not on the volume, so
// the author is told which line and why.
//
// A guard against a misconfiguration by the CR's author, not a boundary
// against a hostile sidecar: KSA tokens are pod-scoped and the callout cannot
// tell which container presented one.
// path is the volume's own path (the list element).
func validateBusCredentialSource(vol corev1.Volume, agentName string, path *field.Path) field.ErrorList {
	var errs field.ErrorList
	for _, route := range agentv1alpha1.BusCredentialRoutes(vol, agentName) {
		switch route.Kind {
		case agentv1alpha1.BusCredentialRouteAudience:
			errs = append(errs, field.Forbidden(
				path.Child("projected", "sources").Index(route.Source).Child("serviceAccountToken", "audience"),
				fmt.Sprintf(busTokenAudienceForbiddenFmt, vol.Name, agentv1alpha1.A2ABusTokenAudience),
			))
		case agentv1alpha1.BusCredentialRouteSecret:
			at := path.Child("secret", "secretName")
			if route.Source != agentv1alpha1.BusCredentialRouteVolumeSource {
				at = path.Child("projected", "sources").Index(route.Source).Child("secret", "name")
			}
			errs = append(errs, field.Forbidden(at, fmt.Sprintf(busCredsSecretForbiddenFmt, vol.Name, route.Secret)))
		}
	}
	return errs
}

// integrationFieldRoot is the spec path the forge declaration hangs off.
var integrationFieldRoot = field.NewPath("spec", "integration")

// validateGitIntegration checks the forge declaration against the rules of the
// providers it names, one error per field rather than stopping at the first,
// and returns the declarations that are valid but do nothing as warnings.
func validateGitIntegration(integration *agentv1alpha1.PlatformAgentIntegrationSpec) (field.ErrorList, admission.Warnings) {
	var errs field.ErrorList
	if integration == nil {
		return errs, nil
	}
	resolved, err := integration.ResolveGit()
	if err != nil {
		// Both spellings set. Neither field is at fault on its own, so the error
		// hangs off the integration itself.
		return append(errs, field.Invalid(integrationFieldRoot, "", err.Error())), nil
	}
	for _, p := range resolved.Problems() {
		errs = append(errs, field.Invalid(integrationPath(p.Path), p.Value, p.Err.Error()))
	}
	return errs, resolved.Warnings()
}

// integrationPath renders an IntegrationFieldPath under spec.integration.
func integrationPath(p agentv1alpha1.IntegrationFieldPath) *field.Path {
	path := integrationFieldRoot.Child(p.List)
	if p.Index >= 0 {
		path = path.Index(p.Index)
	}
	if p.Field != "" {
		path = path.Child(p.Field)
	}
	return path
}

func validateContainerSecurity(sc *corev1.SecurityContext, path *field.Path) field.ErrorList {
	var errs field.ErrorList
	if sc == nil {
		return errs
	}
	if sc.Privileged != nil && *sc.Privileged {
		errs = append(errs, field.Forbidden(
			path.Child("securityContext", "privileged"),
			"privileged containers are forbidden",
		))
	}
	if sc.AllowPrivilegeEscalation != nil && *sc.AllowPrivilegeEscalation {
		errs = append(errs, field.Forbidden(
			path.Child("securityContext", "allowPrivilegeEscalation"),
			"allowPrivilegeEscalation must be false",
		))
	}
	if sc.RunAsUser != nil && *sc.RunAsUser == 0 {
		errs = append(errs, field.Forbidden(
			path.Child("securityContext", "runAsUser"),
			"running containers as root (runAsUser=0) is forbidden",
		))
	}
	if sc.Capabilities != nil && len(sc.Capabilities.Add) > 0 {
		errs = append(errs, field.Forbidden(
			path.Child("securityContext", "capabilities", "add"),
			"adding capabilities is forbidden",
		))
	}

	return errs
}

// ValidateDelete implements admission.Validator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateDelete(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	platformagentlog.Info("validating PlatformAgent deletion", "name", platformAgent.Name)

	if platformAgent.Annotations != nil && platformAgent.Annotations[PreventDeletionAnnotation] == "true" {
		return nil, apierrors.NewForbidden(
			schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "platformagents"},
			platformAgent.Name,
			fmt.Errorf("deletion is blocked by annotation %s=true", PreventDeletionAnnotation),
		)
	}

	return nil, nil
}

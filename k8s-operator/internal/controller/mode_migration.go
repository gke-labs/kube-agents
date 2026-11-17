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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// spec.mode is deprecated: "today" named a stack no release renders any more,
// and the operator renders the A2A stack whatever the field says. This file is
// the only reader of PlatformAgentSpec.Mode in the operator. It moves a stored
// "today" to "next" so the object agrees with what runs: the admission webhook
// rewrites the value on the next write, and the CRD defaults an absent field,
// but neither reaches an object nobody writes again.
const (
	// modeMigratedReason is the Event reason for the one-time rewrite.
	modeMigratedReason = "ModeMigrated"
	// modeMigratedMessageFmt names the value the CR carried.
	modeMigratedMessageFmt = "spec.mode %q is retired; set to %q. Every install runs the A2A stack."
	// modeMigrationPatch is the merge patch that sets the field.
	modeMigrationPatch = `{"spec":{"mode":"` + agentv1alpha1.PlatformAgentModeNext + `"}}`
	// modeMigrationRefusedReason is the Warning Event reason when admission
	// refuses the patch; the object reconciles as it stands.
	modeMigrationRefusedReason = "ModeMigrationRefused"
	// modeMigrationRefusedMessageFmt names the stored value and the refusal.
	modeMigrationRefusedMessageFmt = "spec.mode %q is retired but could not be set to %q: %s. The install reconciles as it stands; the next accepted write sets it."
)

// migrateRetiredMode patches a stored spec.mode other than "next" to "next".
// Returns true when it patched, in which case the caller returns and lets the
// update event drive the next pass on the fresh object.
func (r *PlatformAgentReconciler) migrateRetiredMode(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	if agent.Spec.Mode == nil || *agent.Spec.Mode == agentv1alpha1.PlatformAgentModeNext {
		return false, nil
	}
	previous := *agent.Spec.Mode
	if err := r.Patch(ctx, agent, client.RawPatch(types.MergePatchType, []byte(modeMigrationPatch))); err != nil {
		// The patch goes through the validating webhook, which validates the
		// whole object. A stored object admission now refuses stays as it is and
		// reconciles as it stands (the controller warns about such an object
		// elsewhere); the migration is not a reason to stop rendering it.
		if apierrors.IsInvalid(err) || apierrors.IsForbidden(err) {
			logf.FromContext(ctx).Info("the retired spec.mode was not migrated: admission refused the patch", "from", previous, "error", err.Error())
			if r.Recorder != nil {
				r.Recorder.Eventf(agent, corev1.EventTypeWarning, modeMigrationRefusedReason, modeMigrationRefusedMessageFmt, previous, agentv1alpha1.PlatformAgentModeNext, err.Error())
			}
			return false, nil
		}
		return false, err
	}
	logf.FromContext(ctx).Info("migrated the retired spec.mode", "from", previous, "to", agentv1alpha1.PlatformAgentModeNext)
	if r.Recorder != nil {
		r.Recorder.Eventf(agent, corev1.EventTypeNormal, modeMigratedReason, modeMigratedMessageFmt, previous, agentv1alpha1.PlatformAgentModeNext)
	}
	return true, nil
}

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
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// A ConfigMap edit rolls the gateway, because getConfigMapHash stamps the
// rendered content onto the pod template and a changed annotation is a changed
// template. Secret material had no equivalent. A container's environment is
// fixed for the life of the pod, and nothing recomputed anything when the
// Secret changed, so rotating a key left the running agent on the old one
// indefinitely, with no sign in the CR or the Deployment that it had happened
// (#971, finding 5).
//
// Two of the operator's pods read credentials that way, and both are stamped:
//
//   - the gateway, for the API_SERVER_KEY and SESSION_KV_API_KEY keys of
//     platform-agent-secrets. Its own API_SERVER_KEY env var is a non-secret
//     loopback sentinel; API_SERVER_EXTERNAL_KEY on the auth sidecar is the one
//     that carries the real key.
//   - the credential proxy, which is where the Slack and Teams tokens and the
//     model-provider keys are read (buildCredentialProxyEnv). It is a separate
//     Deployment, so stamping the gateway alone would have left the credentials
//     most likely to be rotated reaching nothing.
//
// The mode-next A2A callout and gateway pods have the same shape and are not
// stamped — see the note at the foot of this comment.
//
// This file is the missing half: a digest of exactly the Secret material a pod
// consumes as environment, stamped on its template so a rotation rolls it the
// way a ConfigMap edit already does.
//
// # Why a tick and not a watch
//
// The trigger is a bounded re-read on the reconcile tick, not an informer.
// Three places in platformagent_controller.go argue against watching Secrets —
// the grant withholds list/watch so a cached read cannot be added by accident,
// a cached Get of an unwatched type starts a cluster-wide Secret informer that
// holds every Secret in the cluster in memory and blocks WaitForCacheSync
// wherever the grant is trimmed, and enqueueing on Secret writes would wake
// every reconcile on every Secret write in the namespace. Beyond those,
// tests/test_operator_secrets_grant.py pins the verb set to exactly
// {get, create, update, patch, delete} and asserts separately that the operator
// can never enumerate Secrets; that test stands in for a Trivy KSV-0041
// exemption which cannot be scoped to one rule. A watch would have to delete
// it.
//
// Re-reading by name costs one Get per referenced Secret per reconcile and
// needs no verb the operator does not already hold. Where a watch on a trimmed
// grant hangs the single reconcile worker in WaitForCacheSync, an unreadable
// Secret here costs only the digest: stampSecretEnvHash carries the previous
// one forward and the reconcile continues, because everything below it in the
// pass — the Service, the NetworkPolicies, the status — must not be withheld
// over a credential this operator only wanted to hash (#964). What the tick
// buys is a bound rather than an instant: a rotation reaches the pod within
// secretEnvReprobeInterval.
//
// # What the digest covers, and what it does not
//
// Only Secret material that arrives as environment — SecretKeyRef, and
// envFrom.secretRef on a CR-supplied sidecar. Mounted Secrets are deliberately
// excluded: the kubelet refreshes a mounted Secret file in place, so hashing
// one would roll a pod over a change it was going to see anyway. The gateway
// has exactly one such mount, the SANDBOX_SSH_PRIVATE_KEY item of
// platform-agent-secrets (buildShellSandboxClientKeyVolumes), and rotating that
// key still needs a restart because an init container copies it to an emptyDir
// at pod start — unchanged by this file. The shell sandbox pod is not involved:
// it mounts its own <agent>-shell-authorized-keys and deliberately never names
// platform-agent-secrets.
//
// Reading the refs off the rendered pod spec rather than naming
// platform-agent-secrets also covers the case where the CR supplies its own
// SecretKeyRef: that key lives in a different Secret, and anything keyed to the
// default name would silently miss it.
//
// # Why the digest is keyed
//
// The annotation sits on the pod template, which anyone who can read pods or
// Deployments can read. An unkeyed SHA-256 over the referenced values — the
// construction Helm's `checksum/secret` annotation uses — would let such a
// reader verify guesses offline: the pod spec names every Secret and key in the
// digest, and a CR-supplied SecretKeyRef can point at a value with far less
// entropy than a generated API key (code-scanning alert #37,
// go/weak-sensitive-data-hashing). So the digest is an HMAC-SHA256 whose key
// is built from the UID of each Secret the values are read from. A UID is 122 random bits
// minted by the API server, and reading it takes the same `get` on the Secret
// that reads the values — so a reader who can recover the key needs no digest
// to learn the values. The other places a UID can surface are Events and owner
// references that point at the Secret, and this operator emits no Events on
// Secrets and owns nothing under one; a third-party controller in the
// namespace that does would widen who can read the key, not what it protects.
// The key is stable for the life of the Secret, so an in-place edit, `kubectl
// apply`, or a patch keeps it and the digest moves only when a value does;
// deleting and recreating the Secret mints a new UID and rolls the pod once,
// which a recreate deserves anyway.
//
// Two keys that would have done less were rejected. The PlatformAgent's own UID
// is readable by the same audience as the annotation. A dedicated random
// Secret would need a create path outside `mode: next`, the only place the
// operator writes Secrets today, and one more object for a rotation to go wrong
// on. Hashing resourceVersion instead of the values was rejected too: a
// metadata-only write to the Secret bumps it and would roll the gateway for a
// change no container can see.
const (
	// secretEnvHashAnnotation carries the digest on the pod template. Named for
	// environment specifically, because a mounted Secret is not in it.
	secretEnvHashAnnotation = "kubeagents.x-k8s.io/secret-env-hash" // #nosec G101 -- Annotation name, not a credential
	// secretEnvReprobeInterval bounds how long a rotated key can take to reach
	// the pod: the reconcile that re-reads the Secret is the only thing that
	// notices, so this is the requeue a healthy pass asks for. It is the one
	// thing this change costs an install that rotates nothing — a render and an
	// apply per agent per interval, where a healthy agent previously sat idle
	// between events.
	//
	// Equal to otelRediscoverAfter on purpose. A healthy pass returns the sooner
	// of the two, so any shorter value would quietly speed the telemetry
	// re-probe up as well and make "re-probes every 15 minutes" false wherever
	// it is documented. Matching it leaves that cadence exactly as it was on the
	// installs that had one, and gives one to the installs that did not.
	// Distinct from rbacReprobeInterval, which is a different reason to come
	// back and whose own test reads the requeue to tell that it has stopped.
	secretEnvReprobeInterval = 15 * time.Minute
	// secretAbsentDigest and secretKeyAbsentDigest stand in for a value that is
	// not there, so that the Secret appearing later changes the digest and rolls
	// the pod. Both open with a character outside the base64 alphabet, which no
	// encoded value can begin with, so neither can collide with a real one.
	secretAbsentDigest    = "!secret-absent"
	secretKeyAbsentDigest = "!key-absent"
)

// secretEnvRef is one piece of Secret material a pod consumes as environment.
// An empty key means every key in the Secret — envFrom.secretRef, which a
// CR-supplied sidecar can carry even though nothing the operator renders does.
type secretEnvRef struct {
	name string
	key  string
}

// podSpecSecretEnvRefs returns the Secret material this pod spec consumes as
// environment, deduplicated and ordered, so the digest over it is stable across
// reconciles. Init containers are walked as well as containers: the agent-api
// auth sidecar is a native sidecar, which is an init container with a restart
// policy.
func podSpecSecretEnvRefs(spec *corev1.PodSpec) []secretEnvRef {
	if spec == nil {
		return nil
	}
	var refs []secretEnvRef
	add := func(ref secretEnvRef) {
		if ref.name == "" || slices.Contains(refs, ref) {
			return
		}
		refs = append(refs, ref)
	}
	for _, containers := range [][]corev1.Container{spec.InitContainers, spec.Containers} {
		for _, container := range containers {
			for _, env := range container.Env {
				if env.ValueFrom != nil && env.ValueFrom.SecretKeyRef != nil {
					add(secretEnvRef{name: env.ValueFrom.SecretKeyRef.Name, key: env.ValueFrom.SecretKeyRef.Key})
				}
			}
			for _, source := range container.EnvFrom {
				if source.SecretRef != nil {
					add(secretEnvRef{name: source.SecretRef.Name})
				}
			}
		}
	}
	slices.SortFunc(refs, func(a, b secretEnvRef) int {
		if a.name != b.name {
			return strings.Compare(a.name, b.name)
		}
		return strings.Compare(a.key, b.key)
	})
	return refs
}

// secretEnvHash digests the Secret material the pod spec consumes as
// environment. It returns "" when the pod consumes none, and when there is no
// reader to consult — a render with no cluster behind it stamps no annotation
// rather than a digest of nothing, which would be indistinguishable from a real
// one.
//
// A Secret or key that does not exist digests to a marker rather than failing:
// an install whose Slack credentials are not created yet is a running install,
// and it has to roll when they appear. Any other read error is returned, and
// the caller abandons the pass before applying anything. Treating a blip as
// absence would drop the annotation and roll the gateway on an API hiccup,
// which is the failure this change exists to avoid causing.
//
// The read goes through r.APIReader for the reason checkShellSandboxKeys gives:
// a cached Get of a type the manager does not watch starts a cluster-wide
// Secret informer and blocks in WaitForCacheSync behind a LIST this operator's
// RBAC forbids.
func (r *PlatformAgentReconciler) secretEnvHash(ctx context.Context, agent *agentv1alpha1.PlatformAgent, spec *corev1.PodSpec) (string, error) {
	refs := podSpecSecretEnvRefs(spec)
	if len(refs) == 0 {
		return "", nil
	}
	var reader client.Reader = r.APIReader
	if reader == nil {
		reader = r.Client
	}
	if reader == nil {
		return "", nil
	}

	// One Get per distinct Secret, however many keys are read out of it.
	loaded := map[string]*corev1.Secret{}
	material := map[string]string{}
	// The HMAC key: the UID of every Secret that was there to read. An absent
	// Secret has no UID and contributes only its marker to the material.
	keyMaterial := map[string]string{}
	for _, ref := range refs {
		secret, seen := loaded[ref.name]
		if !seen {
			fetched := &corev1.Secret{}
			err := reader.Get(ctx, types.NamespacedName{Name: ref.name, Namespace: agent.Namespace}, fetched)
			switch {
			case err == nil:
				secret = fetched
				keyMaterial[ref.name] = string(fetched.UID)
			case apierrors.IsNotFound(err):
				secret = nil
			default:
				return "", fmt.Errorf("failed to read Secret %s/%s for the pod-template digest: %w", agent.Namespace, ref.name, err)
			}
			loaded[ref.name] = secret
		}

		switch {
		case secret == nil:
			material[ref.name+"/"+ref.key] = secretAbsentDigest
		case ref.key == "":
			// envFrom takes the whole Secret, so every key is material.
			for key, value := range secret.Data {
				material[ref.name+"/"+key] = base64.StdEncoding.EncodeToString(value)
			}
		default:
			value, ok := secret.Data[ref.key]
			if !ok {
				material[ref.name+"/"+ref.key] = secretKeyAbsentDigest
				continue
			}
			material[ref.name+"/"+ref.key] = base64.StdEncoding.EncodeToString(value)
		}
	}

	// encoding/json sorts map keys, so neither the digest nor its key depends
	// on Go's randomised map iteration order — the same property
	// getConfigMapHash relies on, and the reason this marshals maps rather than
	// ranging over them.
	encoded, err := json.Marshal(material)
	if err != nil {
		return "", err
	}
	key, err := json.Marshal(keyMaterial)
	if err != nil {
		return "", err
	}
	// Keyed by the Secrets' UIDs rather than an unkeyed SHA-256, so the
	// annotation cannot be used to verify guesses at the values; see the file
	// comment. hash.Hash.Write never returns an error.
	mac := hmac.New(sha256.New, key)
	mac.Write(encoded)
	return hex.EncodeToString(mac.Sum(nil)), nil
}

// stampSecretEnvHash annotates the pod template with the digest of the Secret
// material it consumes as environment, so a rotated key changes the template
// and the workload rolls.
//
// Called after the template is built rather than threaded through the builders:
// the refs are read off the rendered spec, which is what makes CR-supplied
// SecretKeyRefs and sidecar envFrom count without any of that logic being
// duplicated here.
//
// workload is the object the template belongs to, and is only read when the
// digest cannot be computed — see carryForwardSecretEnvHash. A nil workload
// skips that recovery, which is what a render with no cluster behind it wants.
func (r *PlatformAgentReconciler) stampSecretEnvHash(ctx context.Context, agent *agentv1alpha1.PlatformAgent, workload client.Object, template *corev1.PodTemplateSpec) error {
	if template == nil {
		return nil
	}
	hash, err := r.secretEnvHash(ctx, agent, &template.Spec)
	if err != nil {
		return r.carryForwardSecretEnvHash(ctx, workload, template, err)
	}
	if hash == "" {
		return nil
	}
	setSecretEnvHash(template, hash)
	return nil
}

// carryForwardSecretEnvHash copies the digest already on the live workload onto
// the template this pass is about to apply, and reports the read failure that
// made that necessary.
//
// Leaving the annotation off is not the safe option it looks like. The apply is
// server-side and the operator owns this field, so a template rendered without
// it deletes it — which changes the template, which rolls the pod: an API blip
// would restart the agent, the exact outage this file exists to keep rotations
// from causing silently. Carrying the previous digest forward renders the
// template the last good pass rendered, so nothing moves.
//
// Failing the reconcile instead is worse still. Everything after the workload
// in the pass — the Service, both NetworkPolicies, the status — would be
// withheld on every pass for as long as the Secret stays unreadable, which on a
// cluster that has trimmed the Secret grant is forever, and #964 is the standing
// argument against a bail-out that withholds guardrails. checkShellSandboxKeys
// swallows its own read errors for the same reason.
//
// Nothing to carry forward — the first apply, or the live read failing too —
// leaves the template unannotated, which deletes nothing and rolls nothing. The
// digest appears on the first pass that can read the Secret.
func (r *PlatformAgentReconciler) carryForwardSecretEnvHash(ctx context.Context, workload client.Object, template *corev1.PodTemplateSpec, cause error) error {
	log := logf.FromContext(ctx)
	if workload == nil || r.Client == nil {
		log.Info("WARNING: could not digest the Secret material this pod reads as environment; a rotated key will not roll it until this clears", "cause", cause.Error())
		return nil
	}
	live, isObject := workload.DeepCopyObject().(client.Object)
	if !isObject {
		return nil
	}
	previous := ""
	if err := r.Get(ctx, client.ObjectKeyFromObject(workload), live); err == nil {
		if liveTemplate := podTemplateOf(live); liveTemplate != nil {
			previous = liveTemplate.Annotations[secretEnvHashAnnotation]
		}
	}
	log.Info("WARNING: could not digest the Secret material this pod reads as environment; keeping the previous digest so the pod is not rolled by the failure",
		"workload", workload.GetName(),
		"namespace", workload.GetNamespace(),
		"keptPreviousDigest", previous != "",
		"cause", cause.Error())
	if previous == "" {
		return nil
	}
	setSecretEnvHash(template, previous)
	return nil
}

// podTemplateOf returns the pod template of the workload kinds this file
// stamps, or nil for anything else.
func podTemplateOf(workload client.Object) *corev1.PodTemplateSpec {
	switch typed := workload.(type) {
	case *appsv1.Deployment:
		return &typed.Spec.Template
	case *appsv1.StatefulSet:
		return &typed.Spec.Template
	default:
		return nil
	}
}

func setSecretEnvHash(template *corev1.PodTemplateSpec, hash string) {
	if template.Annotations == nil {
		template.Annotations = map[string]string{}
	}
	template.Annotations[secretEnvHashAnnotation] = hash
}

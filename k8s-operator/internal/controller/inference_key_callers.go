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
	"errors"
	"fmt"
	"slices"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/types"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Which callers send their inference gateway key (inference_keys.go mints
// them). A caller is given its class's key only once every Ready gateway pod
// has admitted a map holding that key: the pass that finds
// InferenceKeysServed True marks each class's key Secret with the digest of
// the key it holds (inferenceKeyServedAnnotation), and a later pass renders
// a caller with a secretKeyRef to its class's Secret only while that mark
// matches the key the Secret holds now. Until then, and after anything
// rewrites the key, the caller renders as it did before keys existed and
// sends the placeholder the gateway admits as keyless.
//
// The mark, not the condition, is what a render reads, because the condition
// goes False whenever the map changes (a new tier's key, a probe key
// rotation) and Unknown while the gateway pods roll, and a key already in a
// map every pod has loaded stays in every later map while it is unchanged:
// reading the condition would take every caller off its key and roll it, and
// roll it back a minute later, on each of those. A key that changes loses its
// mark with it, since the mark is the old key's digest.
//
// The set is read once, at the start of a pass, before the agent's managed
// config renders, so the config and the agent pod's env never disagree within
// a pass; a class marked during the pass is sent from the next one, which the
// marking pass asks for (inferenceKeysMarkedRecheck).
//
// The A2A session class is marked like the others and sent by no caller yet.
const (
	// inferenceKeyServedAnnotation is the mark: the digest, as the map
	// indexes it, of the key the Secret held when a Served pass marked it.
	inferenceKeyServedAnnotation = "kubeagents.x-k8s.io/inference-key-served-sha256"

	// agentInferenceKeyEnv is the agent container's variable for the agent
	// class's key. Hermes expands ${...} in the managed config against the
	// process environment, and the pod's other in-process callers (decision
	// memory, the curator's LLM configuration) already read this name.
	agentInferenceKeyEnv = "LITELLM_API_KEY"
	// agentInferenceKeyConfigRef is the managed config's model.api_key while
	// the agent sends its key. A pod that has the new config but not yet the
	// env (the kubelet updates a mounted ConfigMap in place, ahead of the
	// roll) sends this literal unexpanded; the gateway's caller-auth hook
	// admits it as keyless.
	agentInferenceKeyConfigRef = "${" + agentInferenceKeyEnv + "}"
	// agentPlaceholderAPIKey is the managed config's model.api_key otherwise:
	// a placeholder the gateway admits as keyless.
	agentPlaceholderAPIKey = "none"
	// curatorInferenceKeyEnv is the curator's variable for its key: the
	// first one memory.ResolveLLMConfig reads that the curator doesn't set
	// otherwise.
	curatorInferenceKeyEnv = "LITELLM_API_KEY"
	// workerInferenceKeyEnv is a profile worker's variable for its tier's
	// key, the harness's own. worker-adapter adds the placeholder only when
	// it is unset, and its default base URL only when this and the base URL
	// are both unset, so the worker sets ANTHROPIC_BASE_URL itself
	// (buildProfileWorkerPodSpec).
	workerInferenceKeyEnv = "ANTHROPIC_API_KEY"

	// inferenceKeysMarkedRecheck is how soon the pass after one that marked a
	// class runs, to render the callers that now get their key: nothing the
	// operator watches changes when a Secret's annotation does.
	inferenceKeysMarkedRecheck = 2 * time.Second
)

// inferenceKeyCallers is the classes whose callers this pass gives their key,
// and where each key's Secret is. The zero value gives none, which is the
// render of an install without keys.
type inferenceKeyCallers struct {
	secrets map[string]types.NamespacedName
}

// sends reports whether class's callers get their key on this pass.
func (c inferenceKeyCallers) sends(class string) bool {
	_, ok := c.secrets[class]
	return ok
}

// envVar is the variable name carrying class's key from its Secret, and
// whether class is sent. Not optional: the Secret exists before any caller
// references it, and a pod whose Secret has gone should fail to start, not
// start as another class or as keyless.
func (c inferenceKeyCallers) envVar(class, name string) (corev1.EnvVar, bool) {
	secret, ok := c.secrets[class]
	if !ok {
		return corev1.EnvVar{}, false
	}
	return corev1.EnvVar{Name: name, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: secret.Name},
		Key:                  inferenceKeyDataKey,
	}}}, true
}

// inferenceKeyCallersKey carries a pass's inferenceKeyCallers on its context:
// one read at the top of Reconcile reaches the render sites in the A2A stack
// and the profile pools without a new parameter on every builder and every
// test that calls one. The builders stay pure; only the reconcile steps that
// call them read it.
type inferenceKeyCallersKey struct{}

func withInferenceKeyCallers(ctx context.Context, c inferenceKeyCallers) context.Context {
	return context.WithValue(ctx, inferenceKeyCallersKey{}, c)
}

// inferenceKeyCallersFrom is the pass's set; none on a context without one.
func inferenceKeyCallersFrom(ctx context.Context) inferenceKeyCallers {
	c, _ := ctx.Value(inferenceKeyCallersKey{}).(inferenceKeyCallers)
	return c
}

// inferenceKeyMarked reports whether s, which must be the class's recorded
// Secret, carries a well-formed key and the mark for that key.
func inferenceKeyMarked(s *corev1.Secret) bool {
	apiKey := string(s.Data[inferenceKeyDataKey])
	return wellFormedInferenceKey(apiKey) && s.Annotations[inferenceKeyServedAnnotation] == inferenceKeyDigest(apiKey)
}

// servedInferenceKeyCallers reads which classes' keys are marked: each
// recorded key Secret, by name and uncached, used only when it is the
// recorded one (inferenceKeySecretIsOurs). An install with no record, or
// whose gateway has gone (the condition is removed with it), sends none.
//
// A failed read in the agent's namespace fails the pass: rendering it as
// unmarked would take the callers off their keys and roll them, and back on
// the next pass. A failed read in the profile namespace is confined there,
// like every other failure in it, and that tier's callers send no key on
// this pass.
func (r *PlatformAgentReconciler) servedInferenceKeyCallers(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (inferenceKeyCallers, error) {
	recorded := recordedSecretUIDs(agent)
	if len(recorded) == 0 || meta.FindStatusCondition(agent.Status.Conditions, inferenceKeysServedCondition) == nil {
		return inferenceKeyCallers{}, nil
	}
	classes := make([]inferenceKeyClass, 0, len(installInferenceKeyClasses))
	for _, class := range installInferenceKeyClasses {
		classes = append(classes, installInferenceKeyClass(agent, class))
	}
	for _, tier := range recordedTiers(agent, recorded) {
		classes = append(classes, tierInferenceKeyClass(agent, tier))
	}
	out := inferenceKeyCallers{secrets: map[string]types.NamespacedName{}}
	for _, c := range classes {
		uid := recorded[c.secret.Name]
		if uid == "" {
			continue
		}
		s := &corev1.Secret{}
		if err := r.a2aReader().Get(ctx, c.secret, s); err != nil {
			switch {
			case apierrors.IsNotFound(err):
				continue
			case c.inProfileNamespace:
				r.profileNamespaceFailure(ctx, agent, "read the "+c.name+" inference gateway key", errors.New(apiFailure(err)))
				continue
			default:
				return inferenceKeyCallers{}, fmt.Errorf("read the %s inference gateway key's Secret %s: %w", c.name, c.secret.String(), err)
			}
		}
		if s.DeletionTimestamp == nil && inferenceKeySecretIsOurs(s, agent, c, uid) && inferenceKeyMarked(s) {
			out.secrets[c.name] = c.secret
		}
	}
	return out, nil
}

// markInferenceKeysServed marks every class in served whose Secret isn't
// marked for the key the pass read, after a pass found InferenceKeysServed
// True for a map holding those keys, and reports how many it marked. Only the
// recorded Secret holding that same key is marked; a write that fails is
// tried again on the next Served pass, and a profile namespace failure stays
// there.
func (r *PlatformAgentReconciler) markInferenceKeysServed(ctx context.Context, agent *agentv1alpha1.PlatformAgent, served []inferenceKeyClass, keys map[string]string) int {
	already := inferenceKeyCallersFrom(ctx)
	// The record as this pass left it: a key created on the pass is on it.
	recorded := recordedSecretUIDs(agent)
	marked := 0
	for _, c := range served {
		if already.sends(c.name) {
			continue
		}
		apiKey, ok := keys[c.name]
		if !ok {
			continue
		}
		s := &corev1.Secret{}
		err := r.a2aReader().Get(ctx, c.secret, s)
		if err == nil {
			if !inferenceKeySecretIsOurs(s, agent, c, recorded[c.secret.Name]) || string(s.Data[inferenceKeyDataKey]) != apiKey || inferenceKeyMarked(s) {
				continue
			}
			if s.Annotations == nil {
				s.Annotations = map[string]string{}
			}
			s.Annotations[inferenceKeyServedAnnotation] = inferenceKeyDigest(apiKey)
			err = r.Update(ctx, s)
		}
		switch {
		case err == nil:
			marked++
		case apierrors.IsNotFound(err) || apierrors.IsConflict(err):
			// Gone or changed since the pass read it: the next pass decides.
		case c.inProfileNamespace:
			r.profileNamespaceFailure(ctx, agent, "mark the "+c.name+" inference gateway key served", errors.New(apiFailure(err)))
		default:
			logf.FromContext(ctx).Info("could not mark an inference gateway key served; the next Served pass tries again", "class", c.name, "secret", c.secret.String(), "failure", apiFailure(err))
		}
	}
	return marked
}

// withInferenceKeyEnv adds the env carrying class's key to the container
// named container in spec, when the pass sends it. An entry of that name
// already there (a plugin's or an override's) is replaced in place, so the
// list never holds the name twice, which server-side apply refuses.
func withInferenceKeyEnv(spec *corev1.PodSpec, container string, callers inferenceKeyCallers, class, name string) {
	env, ok := callers.envVar(class, name)
	if !ok {
		return
	}
	for i := range spec.Containers {
		ctr := &spec.Containers[i]
		if ctr.Name != container {
			continue
		}
		if at := slices.IndexFunc(ctr.Env, func(e corev1.EnvVar) bool { return e.Name == name }); at >= 0 {
			ctr.Env[at] = env
		} else {
			ctr.Env = append(ctr.Env, env)
		}
	}
}

// tierInferenceKeyClassName is a profile tier's class.
func tierInferenceKeyClassName(tier string) string {
	return inferenceKeyTierClassPrefix + tier
}

// withTierInferenceKeys gives every Job template its tier's key, when the
// pass sends it: the template the dispatch config carries and the one the
// dispatcher Job policy's param holds are this same object, so the two stay
// equal and the policy admits a Job carrying the reference.
func withTierInferenceKeys(templates map[string]profileJobTemplates, callers inferenceKeyCallers) {
	for _, set := range templates {
		for _, tmpl := range set {
			tier := tmpl.Labels[agentProfileTierLabel]
			if tier == "" {
				continue
			}
			withInferenceKeyEnv(&tmpl.Spec, profileWorkerContainer, callers, tierInferenceKeyClassName(tier), workerInferenceKeyEnv)
		}
	}
}

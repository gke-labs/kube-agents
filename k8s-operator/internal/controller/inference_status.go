package controller

import (
	"context"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/prometheus/client_golang/prometheus"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// status.inference: the Platform Agent's default model as the install's LiteLLM config
// states it. The value is the litellm_params.model of the model_list entry named
// agentModelName, split at its first separator into a provider and a name, both
// non-empty. A resource reference keeps its resource id only, so a project id, a region
// or an account id never reaches the status.
const (
	// litellmConfigMapName is the LiteLLM config ConfigMap the chart writes.
	litellmConfigMapName = "litellm-config"
	// litellmConfigMapHashedPrefix prefixes the kustomize install's hashed ConfigMap name.
	litellmConfigMapHashedPrefix = "litellm-config-"
	// litellmConfigKey is the ConfigMap key that holds LiteLLM's config file.
	litellmConfigKey = "config.yaml"
	// inferenceProviderSeparator splits a LiteLLM model into its provider and its name.
	inferenceProviderSeparator = "/"
	// inferenceResourcePathPrefix starts a Vertex AI endpoint path after the provider.
	inferenceResourcePathPrefix = "projects/"
	// inferenceARNPrefix starts the name segment that holds a Bedrock ARN.
	inferenceARNPrefix = "arn:"
	// inferenceARNFieldSeparator separates an ARN's fields; the colon-only form ends in its id.
	inferenceARNFieldSeparator = ":"
	// inferenceResourcePathMinSegments is the shortest full Vertex AI resource path:
	// projects/<p>/locations/<l>/<kind>/<id>.
	inferenceResourcePathMinSegments = 6
	// inferenceResourcePathLocationsIndex is where a full path's "locations" segment sits.
	inferenceResourcePathLocationsIndex = 2
	// inferenceResourcePathLocations is the segment a full path carries at that index.
	inferenceResourcePathLocations = "locations"
	// inferenceARNFields is the field count of a full ARN:
	// arn:partition:service:region:account:resource.
	inferenceARNFields = 6
	// inferenceNewerField names status.inference's pruning record in prunedNewerFields.
	inferenceNewerField = "status.inference"
)

// The info gauge: one sample at 1 per PlatformAgent whose status.inference holds both
// fields, labelled with the agent (the usage gauges' two labels) and the pair, so
// sum by (provider, model) over a shared project is the headcount.
const (
	inferenceGaugeName          = "kubeagents_platformagent_model"
	inferenceGaugeProviderLabel = "provider"
	inferenceGaugeModelLabel    = "model"
	// inferenceGaugeValue is an info gauge's constant sample value.
	inferenceGaugeValue = 1
)

// inferenceRead is the outcome of reading the install's default model.
type inferenceRead int

const (
	// inferenceReadFound: the config states a default model; write the pair.
	inferenceReadFound inferenceRead = iota
	// inferenceReadNegative: the config does not state one, or there is no config;
	// clear both fields.
	inferenceReadNegative
	// inferenceReadFailed: an API call failed; keep the pair the status carries.
	inferenceReadFailed
)

// litellmConfig is the part of LiteLLM's config file the reader needs.
type litellmConfig struct {
	ModelList []struct {
		ModelName     string `json:"model_name"`
		LitellmParams struct {
			Model string `json:"model"`
		} `json:"litellm_params"`
	} `json:"model_list"`
}

// inferenceModel is the provider and model pair status.inference carries.
type inferenceModel struct{ Provider, Model string }

// parseLiteLLMDefaultModel reads the default model out of a LiteLLM config file. The
// entry named agentModelName is the answer when it exists, and a malformed model there
// (no separator, a separator at either end, or a provider side that is itself a
// resource reference) is a negative with no fallback. Only when no entry carries that
// name does the first well-formed provider-qualified entry answer. ok false is a
// definite negative: the config does not state a default model.
func parseLiteLLMDefaultModel(configYAML string) (inferenceModel, bool) {
	var config litellmConfig
	if err := yaml.Unmarshal([]byte(configYAML), &config); err != nil {
		return inferenceModel{}, false
	}
	for _, entry := range config.ModelList {
		if entry.ModelName == agentModelName {
			return splitInferenceModel(entry.LitellmParams.Model)
		}
	}
	for _, entry := range config.ModelList {
		if model, ok := splitInferenceModel(entry.LitellmParams.Model); ok {
			return model, true
		}
	}
	return inferenceModel{}, false
}

// splitInferenceModel splits a LiteLLM model at its first separator. Both sides must be
// non-empty, the name after trimInferenceModelName included; a value that ends with the
// separator, or whose provider side is a bare resource reference, is malformed. Whitespace
// on either side is malformed too: a status value, a gauge label and a summary line must
// each stay on one line.
func splitInferenceModel(litellmModel string) (inferenceModel, bool) {
	if strings.HasSuffix(litellmModel, inferenceProviderSeparator) {
		return inferenceModel{}, false
	}
	provider, name, found := strings.Cut(litellmModel, inferenceProviderSeparator)
	if !found || !validInferenceProvider(provider) {
		return inferenceModel{}, false
	}
	name = trimInferenceModelName(name)
	if name == "" || strings.ContainsFunc(name, unicode.IsSpace) {
		return inferenceModel{}, false
	}
	return inferenceModel{Provider: provider, Model: name}, true
}

// validInferenceProvider reports whether provider is a provider slug. An empty side, or a
// bare resource reference (an ARN, or a Vertex AI path with no provider before it), is
// malformed: it would put a project id, a region or an account id in the status.
func validInferenceProvider(provider string) bool {
	return provider != "" &&
		!strings.ContainsFunc(provider, unicode.IsSpace) &&
		!strings.HasPrefix(provider, inferenceARNPrefix) &&
		!strings.Contains(provider, inferenceARNFieldSeparator) &&
		provider != strings.TrimSuffix(inferenceResourcePathPrefix, inferenceProviderSeparator)
}

// trimInferenceModelName applies the resource-reference rule to the name after the
// provider. A reference must have its full shape, or the name is malformed and "" comes
// back. A Vertex AI path needs projects/<p>/locations/<l>/<kind>/<id> at least and keeps
// the text after its last separator. A name with a segment that starts with an ARN needs
// six colon fields from there (arn:partition:service:region:account:resource) with a
// non-empty resource part, and keeps the text after the resource part's last separator,
// or, without one, the text after its last field separator. Every other name is kept as
// typed.
func trimInferenceModelName(name string) string {
	if strings.HasPrefix(name, inferenceResourcePathPrefix) {
		segments := strings.Split(name, inferenceProviderSeparator)
		if len(segments) < inferenceResourcePathMinSegments ||
			segments[inferenceResourcePathLocationsIndex] != inferenceResourcePathLocations {
			return ""
		}
		return segments[len(segments)-1]
	}
	segments := strings.Split(name, inferenceProviderSeparator)
	for i, segment := range segments {
		if !strings.HasPrefix(segment, inferenceARNPrefix) {
			continue
		}
		arn := strings.Join(segments[i:], inferenceProviderSeparator)
		fields := strings.SplitN(arn, inferenceARNFieldSeparator, inferenceARNFields)
		if len(fields) < inferenceARNFields || fields[inferenceARNFields-1] == "" {
			return ""
		}
		resource := fields[inferenceARNFields-1]
		if strings.Contains(resource, inferenceProviderSeparator) {
			return afterLast(resource, inferenceProviderSeparator)
		}
		return afterLast(resource, inferenceARNFieldSeparator)
	}
	return name
}

// afterLast returns the text after the last sep in s, or s when sep does not occur.
func afterLast(s, sep string) string {
	i := strings.LastIndex(s, sep)
	if i < 0 {
		return s
	}
	return s[i+len(sep):]
}

// isLiteLLMConfigMapName reports whether name is the LiteLLM config ConfigMap the chart
// writes or the kustomize install's hashed one.
func isLiteLLMConfigMapName(name string) bool {
	return name == litellmConfigMapName ||
		(strings.HasPrefix(name, litellmConfigMapHashedPrefix) && len(name) > len(litellmConfigMapHashedPrefix))
}

// liteLLMConfigMapRequests maps a LiteLLM config ConfigMap to the PlatformAgents in its
// namespace, so an edited config reaches status.inference within a reconcile. Any other
// ConfigMap maps to nothing.
func liteLLMConfigMapRequests(ctx context.Context, obj client.Object, enqueue func(context.Context, string) []reconcile.Request) []reconcile.Request {
	if !isLiteLLMConfigMapName(obj.GetName()) {
		return nil
	}
	return enqueue(ctx, obj.GetNamespace())
}

// readInferenceModel reads the default model out of the install's LiteLLM config. The
// litellm Deployment's config volumes name the ConfigMap: the first, in volume order,
// whose data carries litellmConfigKey. Only when the Deployment is missing does the
// newest candidate in the namespace that carries litellmConfigKey answer (ties by
// name). A Deployment none of whose mounted LiteLLM config ConfigMaps carries the key,
// or a config that states no default model, is a negative; any other API error is a
// failed read.
func (r *PlatformAgentReconciler) readInferenceModel(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (inferenceModel, inferenceRead) {
	log := logf.FromContext(ctx)
	cm, deployed, err := r.mountedLiteLLMConfigMap(ctx, agent.Namespace)
	if err != nil {
		log.Info("could not read the LiteLLM Deployment or its config ConfigMap for status.inference; keeping the last written pair", "error", err)
		return inferenceModel{}, inferenceReadFailed
	}
	if !deployed {
		cm, err = r.newestLiteLLMConfigMap(ctx, agent.Namespace)
		if err != nil {
			log.Info("could not list the LiteLLM config ConfigMaps for status.inference; keeping the last written pair", "error", err)
			return inferenceModel{}, inferenceReadFailed
		}
	}
	// With the Deployment present, a leftover ConfigMap it does not mount is not what
	// LiteLLM serves, so a nil here is a negative with no list fallback.
	if cm == nil {
		return inferenceModel{}, inferenceReadNegative
	}
	model, ok := parseLiteLLMDefaultModel(cm.Data[litellmConfigKey])
	if !ok {
		return inferenceModel{}, inferenceReadNegative
	}
	return model, inferenceReadFound
}

// mountedLiteLLMConfigMap returns the first LiteLLM config ConfigMap, in volume order,
// that the litellm Deployment mounts and whose data carries litellmConfigKey. A mounted
// ConfigMap that is missing or lacks the key is passed over. deployed is false when
// there is no Deployment; a Deployment with no such ConfigMap returns nil with deployed
// true.
func (r *PlatformAgentReconciler) mountedLiteLLMConfigMap(ctx context.Context, namespace string) (cm *corev1.ConfigMap, deployed bool, err error) {
	dep := &appsv1.Deployment{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: litellmDeploymentName}, dep); err != nil {
		if errors.IsNotFound(err) {
			return nil, false, nil
		}
		return nil, false, err
	}
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.ConfigMap == nil || !isLiteLLMConfigMapName(v.ConfigMap.Name) {
			continue
		}
		mounted := &corev1.ConfigMap{}
		if err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: v.ConfigMap.Name}, mounted); err != nil {
			if errors.IsNotFound(err) {
				continue
			}
			return nil, true, err
		}
		if _, ok := mounted.Data[litellmConfigKey]; ok {
			return mounted, true, nil
		}
	}
	return nil, true, nil
}

// newestLiteLLMConfigMap returns the newest LiteLLM config ConfigMap in the namespace
// that carries litellmConfigKey, by creation time and then name, or nil when none does.
func (r *PlatformAgentReconciler) newestLiteLLMConfigMap(ctx context.Context, namespace string) (*corev1.ConfigMap, error) {
	list := &corev1.ConfigMapList{}
	if err := r.List(ctx, list, client.InNamespace(namespace)); err != nil {
		return nil, err
	}
	var candidates []*corev1.ConfigMap
	for i := range list.Items {
		cm := &list.Items[i]
		if _, ok := cm.Data[litellmConfigKey]; ok && isLiteLLMConfigMapName(cm.Name) {
			candidates = append(candidates, cm)
		}
	}
	if len(candidates) == 0 {
		return nil, nil
	}
	sort.Slice(candidates, func(i, j int) bool {
		ti, tj := candidates[i].CreationTimestamp, candidates[j].CreationTimestamp
		if !ti.Equal(&tj) {
			return tj.Before(&ti)
		}
		return candidates[i].Name < candidates[j].Name
	})
	return candidates[0], nil
}

// inferencePruned reports whether the served CRD has been seen to drop status.inference
// for this CR within the last usageStatusReprobeInterval. A stale record reads false,
// which is what makes the next pass probe.
func (r *PlatformAgentReconciler) inferencePruned(agent *agentv1alpha1.PlatformAgent) bool {
	r.prunedNewerFieldsMu.Lock()
	defer r.prunedNewerFieldsMu.Unlock()
	recorded, ok := r.prunedNewerFields[usagePollKey(agent)][inferenceNewerField]
	return ok && time.Since(recorded) < usageStatusReprobeInterval
}

// noteInferenceEcho reads status.inference back after the Ready writer's write. A
// non-empty pair written that comes back absent or with both fields empty is the served
// CRD pruning it: recorded, and logged once per record. An echo that carries the pair
// clears the record. An empty pair written says nothing either way.
func (r *PlatformAgentReconciler) noteInferenceEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent, wrote inferenceModel) {
	if wrote == (inferenceModel{}) {
		return
	}
	echoed := agent.Status.Inference
	key := usagePollKey(agent)
	r.prunedNewerFieldsMu.Lock()
	defer r.prunedNewerFieldsMu.Unlock()
	if echoed != nil && echoed.Provider == wrote.Provider && echoed.Model == wrote.Model {
		delete(r.prunedNewerFields[key], inferenceNewerField)
		return
	}
	if echoed != nil && *echoed != (agentv1alpha1.InferenceStatus{}) {
		return
	}
	if r.prunedNewerFields == nil {
		r.prunedNewerFields = map[string]map[string]time.Time{}
	}
	if r.prunedNewerFields[key] == nil {
		r.prunedNewerFields[key] = map[string]time.Time{}
	}
	recorded, held := r.prunedNewerFields[key][inferenceNewerField]
	fresh := held && time.Since(recorded) < usageStatusReprobeInterval
	r.prunedNewerFields[key][inferenceNewerField] = time.Now()
	if !fresh {
		logf.FromContext(ctx).Info("the served CRD does not serve a status field this operator writes; apply this release's CRD to get it, which is probed again after the interval",
			"platformagent", key, "field", inferenceNewerField, "reprobeAfter", usageStatusReprobeInterval.String())
	}
}

// inferenceStatusEqual reports whether two status.inference values serialise the same:
// both absent, or both present with the same pair.
func inferenceStatusEqual(a, b *agentv1alpha1.InferenceStatus) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

// inferenceGauges serves status.inference as an info gauge.
type inferenceGauges struct {
	model *prometheus.GaugeVec
	mu    sync.Mutex
}

func newInferenceGauges(reg prometheus.Registerer) *inferenceGauges {
	g := &inferenceGauges{
		model: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: inferenceGaugeName, Help: "A PlatformAgent's default model from status.inference, at 1; absent while the status holds no pair."},
			[]string{usageGaugeNamespaceLabel, usageGaugeNameLabel, inferenceGaugeProviderLabel, inferenceGaugeModelLabel}),
	}
	reg.MustRegister(g.model)
	return g
}

// inferenceMetrics is the process's info gauge, served beside usageMetrics.
var inferenceMetrics = newInferenceGauges(metrics.Registry)

// publish sets the agent's one sample from status.inference, deleting its previous
// pair first so a collector never sees two models for one agent. No sample while
// either field is empty.
func (g *inferenceGauges) publish(agent *agentv1alpha1.PlatformAgent) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.deleteAgent(agent.Namespace, agent.Name)
	inference := agent.Status.Inference
	if inference == nil || inference.Provider == "" || inference.Model == "" {
		return
	}
	g.model.WithLabelValues(agent.Namespace, agent.Name, inference.Provider, inference.Model).Set(inferenceGaugeValue)
}

// forget drops the agent's sample when the CR is deleted.
func (g *inferenceGauges) forget(namespace, name string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.deleteAgent(namespace, name)
}

func (g *inferenceGauges) deleteAgent(namespace, name string) {
	g.model.DeletePartialMatch(prometheus.Labels{usageGaugeNamespaceLabel: namespace, usageGaugeNameLabel: name})
}

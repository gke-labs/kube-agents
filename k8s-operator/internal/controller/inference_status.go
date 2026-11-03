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
// provider. A Vertex AI endpoint path keeps the text after its last separator. A name
// with a segment that starts with an ARN keeps the text after its last separator, or,
// when the ARN segment is the last one, the text after the ARN's last field separator.
// Every other name is kept as typed.
func trimInferenceModelName(name string) string {
	if strings.HasPrefix(name, inferenceResourcePathPrefix) {
		return afterLast(name, inferenceProviderSeparator)
	}
	segments := strings.Split(name, inferenceProviderSeparator)
	for i, segment := range segments {
		if !strings.HasPrefix(segment, inferenceARNPrefix) {
			continue
		}
		if i < len(segments)-1 {
			return afterLast(name, inferenceProviderSeparator)
		}
		return afterLast(segment, inferenceARNFieldSeparator)
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
// litellm Deployment's config volume names the ConfigMap; only when the Deployment is
// missing does the newest candidate in the namespace that carries litellmConfigKey
// answer (ties by name). A Deployment that mounts no LiteLLM config ConfigMap, a missing
// ConfigMap or key, or a config that states no default model, is a negative; any other
// API error is a failed read.
func (r *PlatformAgentReconciler) readInferenceModel(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (inferenceModel, inferenceRead) {
	log := logf.FromContext(ctx)
	var cm *corev1.ConfigMap
	name, deployed, err := r.mountedLiteLLMConfigMapName(ctx, agent.Namespace)
	switch {
	case err != nil:
		log.Info("could not read the LiteLLM Deployment for status.inference; keeping the last written pair", "error", err)
		return inferenceModel{}, inferenceReadFailed
	case deployed && name == "":
		// A leftover ConfigMap is not what LiteLLM serves.
		return inferenceModel{}, inferenceReadNegative
	case deployed:
		cm = &corev1.ConfigMap{}
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: name}, cm); err != nil {
			if errors.IsNotFound(err) {
				return inferenceModel{}, inferenceReadNegative
			}
			log.Info("could not read the LiteLLM config ConfigMap for status.inference; keeping the last written pair", "error", err)
			return inferenceModel{}, inferenceReadFailed
		}
	default:
		cm, err = r.newestLiteLLMConfigMap(ctx, agent.Namespace)
		if err != nil {
			log.Info("could not list the LiteLLM config ConfigMaps for status.inference; keeping the last written pair", "error", err)
			return inferenceModel{}, inferenceReadFailed
		}
		if cm == nil {
			return inferenceModel{}, inferenceReadNegative
		}
	}
	config, ok := cm.Data[litellmConfigKey]
	if !ok {
		return inferenceModel{}, inferenceReadNegative
	}
	model, ok := parseLiteLLMDefaultModel(config)
	if !ok {
		return inferenceModel{}, inferenceReadNegative
	}
	return model, inferenceReadFound
}

// mountedLiteLLMConfigMapName returns the LiteLLM config ConfigMap the litellm
// Deployment mounts. deployed is false when there is no Deployment; a Deployment that
// mounts none returns "" with deployed true.
func (r *PlatformAgentReconciler) mountedLiteLLMConfigMapName(ctx context.Context, namespace string) (name string, deployed bool, err error) {
	dep := &appsv1.Deployment{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: litellmDeploymentName}, dep); err != nil {
		if errors.IsNotFound(err) {
			return "", false, nil
		}
		return "", false, err
	}
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.ConfigMap != nil && isLiteLLMConfigMapName(v.ConfigMap.Name) {
			return v.ConfigMap.Name, true, nil
		}
	}
	return "", true, nil
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
// non-empty pair written that comes back with both fields empty is the served CRD
// pruning it: recorded, and logged once per record. An echo that carries the pair
// clears the record. An empty pair written says nothing either way.
func (r *PlatformAgentReconciler) noteInferenceEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent, wrote inferenceModel) {
	if wrote == (inferenceModel{}) {
		return
	}
	echoed := agent.Status.Inference
	key := usagePollKey(agent)
	r.prunedNewerFieldsMu.Lock()
	defer r.prunedNewerFieldsMu.Unlock()
	if echoed.Provider == wrote.Provider && echoed.Model == wrote.Model {
		delete(r.prunedNewerFields[key], inferenceNewerField)
		return
	}
	if echoed != (agentv1alpha1.InferenceStatus{}) {
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
	if inference.Provider == "" || inference.Model == "" {
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

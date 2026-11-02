package controller

import (
	"strings"

	"sigs.k8s.io/yaml"
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
// (no separator, or an empty side) is a negative with no fallback. Only when no entry
// carries that name does the first provider-qualified entry answer. ok false is a
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
// separator, or whose provider side is a bare resource reference, is malformed.
func splitInferenceModel(litellmModel string) (inferenceModel, bool) {
	if strings.HasSuffix(litellmModel, inferenceProviderSeparator) {
		return inferenceModel{}, false
	}
	provider, name, found := strings.Cut(litellmModel, inferenceProviderSeparator)
	if !found || !validInferenceProvider(provider) {
		return inferenceModel{}, false
	}
	name = trimInferenceModelName(name)
	if name == "" {
		return inferenceModel{}, false
	}
	return inferenceModel{Provider: provider, Model: name}, true
}

// validInferenceProvider reports whether provider is a provider slug. An empty side, or a
// bare resource reference (an ARN, or a Vertex AI path with no provider before it), is
// malformed: it would put a project id, a region or an account id in the status.
func validInferenceProvider(provider string) bool {
	return provider != "" &&
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

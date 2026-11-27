package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	// defaultGeminiModel is the chart's default for the gemini and vertex_ai
	// providers (DEFAULT_MODEL_GEMINI in install.defaults.env), the model the
	// console names when the install names none.
	defaultGeminiModel = "gemini-3.8-flash"

	activeModelAnnotation    = "kubeagents.x-k8s.io/active-model"
	activeTargetAnnotation   = "kubeagents.x-k8s.io/active-model-target"
	reasoningLevelAnnotation = "kubeagents.x-k8s.io/reasoning-level"
	modelsTimeout            = 15 * time.Second

	// liteLLMModelInfoPath lists the models the inference gateway names.
	liteLLMModelInfoPath = "/model/info"
	// ModelsResponse.ModelsSource: where the selector's list came from.
	modelsSourceGateway = "gateway"
	modelsSourcePresets = "presets"
	// servedModelDescription describes a served model the preset catalog
	// does not know.
	servedModelDescription = "Served by the inference gateway"
	// modelTestFailedCode is the error code of a pick the gateway did not
	// answer for.
	modelTestFailedCode = "model_test_failed"
	// liteLLMWildcard marks a gateway route that matches models by pattern.
	liteLLMWildcard = "*"
	// geminiModelMarker is in the name of every Gemini model, which all
	// take a reasoning level.
	geminiModelMarker = "gemini"
	// The chart routes litellm.modelProvider openai_compatible through
	// LiteLLM's hosted_vllm handler, so the gateway names the provider
	// hosted_vllm where the chart's value is openai_compatible.
	chartOpenAICompatibleProvider = "openai_compatible"
	liteLLMHostedVLLMProvider     = "hosted_vllm"
)

var errNoServedModels = errors.New("LiteLLM reported no models")

var validModelTokenPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,127}$`)

type ModelOption struct {
	ID                string `json:"id"`
	Name              string `json:"name"`
	Provider          string `json:"provider"`
	ModelTarget       string `json:"model_target"`
	SupportsReasoning bool   `json:"supports_reasoning"`
	ReasoningLevel    string `json:"reasoning_level,omitempty"`
	Description       string `json:"description,omitempty"`
	Verified          bool   `json:"verified"`
}

type ModelsResponse struct {
	// ActiveModel is the preferred model: the one last picked in the
	// selector unless the gateway's list rules it out, else the model in use.
	ActiveModel    string `json:"active_model"`
	ActiveProvider string `json:"active_provider"`
	ActiveTarget   string `json:"active_target"`
	// ModelInUse is the model the gateway's default alias routes to, the
	// one the agent's requests reach; empty when the console cannot tell.
	ModelInUse     string        `json:"model_in_use,omitempty"`
	ReasoningLevel string        `json:"reasoning_level,omitempty"`
	HelmValuesYAML string        `json:"helm_values_yaml,omitempty"`
	ModelsSource   string        `json:"models_source"`
	Models         []ModelOption `json:"models"`
}

// modelTestFailedResponse answers a pick the gateway did not answer for:
// nothing was saved, and HelmValuesYAML is what would make the gateway
// serve the model.
type modelTestFailedResponse struct {
	Error  string `json:"error"`
	Detail string `json:"detail"`
	Model  string `json:"model"`
	// Target is the model target the test request named, and Reason the
	// gateway's answer to it.
	Target         string `json:"target"`
	Reason         string `json:"reason"`
	HelmValuesYAML string `json:"helm_values_yaml"`
}

// servedModels is what the inference gateway names in /model/info.
type servedModels struct {
	// options holds each served model once, however many aliases route to it.
	options []ModelOption
	// names holds every alias, model name and target the gateway lists.
	names map[string]bool
	// patterns is true when the gateway also routes models by pattern: it
	// then serves models names does not hold.
	patterns bool
	// inUse is the model the default alias routes to; its ID is empty when
	// the gateway lists no default alias.
	inUse ModelOption
}

// serves reports whether the gateway lists the model under its name or
// target. A gateway that routes by pattern may serve any model.
func (m servedModels) serves(model, target string) bool {
	return m.patterns || (model != "" && m.names[model]) || (target != "" && m.names[target])
}

type ModelSelectRequest struct {
	Model          string `json:"model"`
	Provider       string `json:"provider,omitempty"`
	ModelTarget    string `json:"model_target,omitempty"`
	ReasoningLevel string `json:"reasoning_level,omitempty"`
}

var allowedReasoningLevels = map[string]bool{
	"dynamic": true,
	"high":    true,
	"medium":  true,
	"low":     true,
	"off":     true,
}

var candidateModelCatalog = []ModelOption{
	{
		ID:                "gemini-3.5-flash",
		Name:              "Gemini 3.5 Flash",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/gemini-3.5-flash",
		SupportsReasoning: true,
		ReasoningLevel:    "dynamic",
		Description:       "Frontier multimodal model with adaptive thinking",
	},
	{
		ID:                "gemini-3.5-pro",
		Name:              "Gemini 3.5 Pro",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/gemini-3.5-pro",
		SupportsReasoning: true,
		ReasoningLevel:    "dynamic",
		Description:       "Frontier reasoning model for deep architectural investigations",
	},
	{
		ID:                "gemini-3.5-ultra",
		Name:              "Gemini 3.5 Ultra",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/gemini-3.5-ultra",
		SupportsReasoning: true,
		ReasoningLevel:    "high",
		Description:       "Maximum capability frontier reasoning model",
	},
	{
		ID:                "gemini-3.8-flash",
		Name:              "Gemini 3.8 Flash",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/gemini-3.8-flash",
		SupportsReasoning: true,
		ReasoningLevel:    "dynamic",
		Description:       "Next-generation high-speed multimodal reasoning model (Recommended)",
	},
	{
		ID:                "gemini-3.8-pro",
		Name:              "Gemini 3.8 Pro",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/gemini-3.8-pro",
		SupportsReasoning: true,
		ReasoningLevel:    "dynamic",
		Description:       "Advanced high-throughput frontier reasoning model",
	},
	{
		ID:                "gemini-4.0-argon",
		Name:              "Gemini 4.0 Argon",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/gemini-4.0-argon",
		SupportsReasoning: true,
		ReasoningLevel:    "high",
		Description:       "Frontier Argon architecture with extreme cognitive reasoning",
	},
	{
		ID:                "gemini-4.0-argon-pro",
		Name:              "Gemini 4.0 Argon Pro",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/gemini-4.0-argon-pro",
		SupportsReasoning: true,
		ReasoningLevel:    "high",
		Description:       "High-capacity Argon architecture for autonomous engineering tasks",
	},
	{
		ID:                "claude-opus-5",
		Name:              "Claude Opus 5",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/claude-opus-5",
		SupportsReasoning: false,
		Description:       "Anthropic frontier flagship model on Vertex AI Model Garden",
	},
	{
		ID:                "claude-3-5-sonnet",
		Name:              "Claude 3.5 Sonnet",
		Provider:          "vertex_ai",
		ModelTarget:       "vertex_ai/claude-3-5-sonnet",
		SupportsReasoning: false,
		Description:       "Anthropic Claude on Vertex AI Model Garden",
	},
	{
		ID:                "gpt-5.4",
		Name:              "GPT-5.4",
		Provider:          "openai",
		ModelTarget:       "openai/gpt-5.4",
		SupportsReasoning: false,
		Description:       "OpenAI frontier flagship model",
	},
	{
		ID:                "gpt-5-mini",
		Name:              "GPT-5 Mini",
		Provider:          "openai",
		ModelTarget:       "openai/gpt-5-mini",
		SupportsReasoning: false,
		Description:       "OpenAI lightweight cost-efficient frontier model",
	},
}

type modelProbeCache struct {
	mu      sync.RWMutex
	tested  map[string]bool
	lastRun time.Time
}

var probeCache = modelProbeCache{
	tested: make(map[string]bool),
}

// testModel tests whether an LLM model target responds to chat completions
// through the inference gateway before adding it or switching to it.
func (s *server) testModel(ctx context.Context, modelID, target string) error {
	if s.cfg.LiteLLMURL == "" {
		return errors.New("no inference gateway (LITELLM_URL) configured")
	}
	primary := target
	fallback := modelID
	if primary == "" {
		primary = modelID
		fallback = ""
	} else if fallback == primary || fallback == defaultAssistModel || fallback == "hermes-agent" {
		fallback = ""
	}
	payload := map[string]any{
		"model": primary,
		"messages": []map[string]string{
			{"role": "user", "content": "ping"},
		},
		"max_tokens": 1,
	}
	b, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(400 * time.Millisecond):
			}
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.LiteLLMURL+"/v1/chat/completions", bytes.NewReader(b))
		if err != nil {
			return err
		}
		// No Authorization: the agent API key is the agent's own, not a gateway
		// credential. The console calls the gateway keyless, as assist.go and
		// insights.go do.
		req.Header.Set("Content-Type", contentTypeJSON)
		resp, err := s.client.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("could not reach inference gateway: %w", err)
			continue
		}
		statusCode := resp.StatusCode
		detail := upstreamErrorDetail(resp)
		drainAndClose(resp)
		if statusCode == http.StatusOK {
			return nil
		}
		// If explicit target was not mapped directly in LiteLLM, try the modelID alias
		if fallback != "" {
			payload["model"] = fallback
			b2, _ := json.Marshal(payload)
			req2, reqErr := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.LiteLLMURL+"/v1/chat/completions", bytes.NewReader(b2))
			if reqErr == nil {
				req2.Header.Set("Content-Type", contentTypeJSON)
				resp2, err2 := s.client.Do(req2)
				if err2 == nil {
					status2 := resp2.StatusCode
					detail2 := upstreamErrorDetail(resp2)
					drainAndClose(resp2)
					if status2 == http.StatusOK {
						return nil
					}
					detail = detail2
				}
			}
			payload["model"] = primary
		}
		if statusCode == http.StatusNotFound || statusCode == http.StatusBadRequest {
			return fmt.Errorf("HTTP %d: %s", statusCode, detail)
		}
		lastErr = fmt.Errorf("HTTP %d: %s", statusCode, detail)
	}
	return lastErr
}

// catalogModelsWithStatus returns the candidates, probing all of them only
// when probeAll is true. Without probeAll, candidates carry Verified from
// prior probes (or when matching the active model on a cluster with LiteLLM
// configured), avoiding a request per candidate on cold GETs.
func (s *server) catalogModelsWithStatus(ctx context.Context, candidates []ModelOption, activeModel, activeTarget string, probeAll bool) []ModelOption {
	if probeAll && s.cfg.LiteLLMURL != "" {
		probeCache.mu.RLock()
		cacheValid := time.Since(probeCache.lastRun) < 5*time.Minute && len(probeCache.tested) > 0
		probeCache.mu.RUnlock()

		if !cacheValid {
			var wg sync.WaitGroup
			results := make(map[string]bool)
			var resultsMu sync.Mutex

			for _, opt := range candidates {
				wg.Add(1)
				go func(m ModelOption) {
					defer wg.Done()
					probeCtx, cancel := context.WithTimeout(ctx, 3500*time.Millisecond)
					defer cancel()
					err := s.testModel(probeCtx, m.ID, m.ModelTarget)
					resultsMu.Lock()
					results[m.ModelTarget] = (err == nil)
					if err != nil {
						log.Printf(logPrefix+"model probe failed for %s (%s): %v", m.ID, m.ModelTarget, err)
					} else {
						log.Printf(logPrefix+"model probe succeeded for %s (%s)", m.ID, m.ModelTarget)
					}
					resultsMu.Unlock()
				}(opt)
			}
			wg.Wait()

			probeCache.mu.Lock()
			probeCache.tested = results
			probeCache.lastRun = time.Now()
			probeCache.mu.Unlock()
		}
	}

	probeCache.mu.RLock()
	cachedResults := make(map[string]bool, len(probeCache.tested))
	for k, v := range probeCache.tested {
		cachedResults[k] = v
	}
	probeCache.mu.RUnlock()

	out := make([]ModelOption, 0, len(candidates))
	for _, opt := range candidates {
		isActive := (activeModel != "" && opt.ID == activeModel) || (activeTarget != "" && opt.ModelTarget == activeTarget)
		if cachedResults[opt.ModelTarget] || (s.cfg.LiteLLMURL != "" && isActive) {
			opt.Verified = true
		}
		if probeAll && s.cfg.LiteLLMURL != "" && !opt.Verified && !isActive {
			continue
		}
		out = append(out, opt)
	}
	return out
}

func generateModelHelmValuesYAML(modelID, provider, target, reasoningLevel string) string {
	if modelID == "" {
		return ""
	}
	if provider == "" {
		provider = "vertex_ai"
	}
	if target == "" {
		target = provider + "/" + modelID
	}
	var sb strings.Builder
	sb.WriteString("litellm:\n")
	sb.WriteString(fmt.Sprintf("  modelProvider: %s\n", provider))
	sb.WriteString(fmt.Sprintf("  modelDefaultName: %s\n", modelID))
	switch reasoningLevel {
	case "low", "medium", "high":
		sb.WriteString(fmt.Sprintf("  reasoningEffort: %s\n", reasoningLevel))
	case "none":
		sb.WriteString("  reasoningEffort: \"\"\n")
	}
	sb.WriteString("platformAgent:\n  annotations:\n")
	sb.WriteString(fmt.Sprintf("    %s: %s\n", activeModelAnnotation, modelID))
	sb.WriteString(fmt.Sprintf("    %s: %s\n", activeTargetAnnotation, target))
	if reasoningLevel != "" {
		sb.WriteString(fmt.Sprintf("    %s: %s\n", reasoningLevelAnnotation, reasoningLevel))
	}
	return sb.String()
}

// parseServedModels reads LiteLLM's /model/info into the models it names.
// A model's ID is its target without the provider, whole: the chart routes
// the default alias to modelProvider/modelDefaultName, so the ID of
// openrouter/anthropic/claude is anthropic/claude, the modelDefaultName
// that selects it.
func parseServedModels(body []byte) (servedModels, error) {
	var payload struct {
		Data []struct {
			ModelName     string `json:"model_name"`
			LitellmParams struct {
				Model string `json:"model"`
			} `json:"litellm_params"`
			ModelInfo struct {
				Key                    string  `json:"key"`
				LitellmProvider        string  `json:"litellm_provider"`
				SupportsReasoning      bool    `json:"supports_reasoning"`
				DefaultReasoningEffort *string `json:"default_reasoning_effort"`
			} `json:"model_info"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return servedModels{}, err
	}
	out := servedModels{names: map[string]bool{}}
	seen := map[string]bool{}
	for _, item := range payload.Data {
		alias := strings.TrimSpace(item.ModelName)
		raw := strings.TrimSpace(item.LitellmParams.Model)
		if raw == "" {
			raw = strings.TrimSpace(item.ModelInfo.Key)
		}
		if raw == "" {
			raw = alias
		}
		if raw == "" {
			continue
		}
		if strings.Contains(alias, liteLLMWildcard) || strings.Contains(raw, liteLLMWildcard) {
			out.patterns = true
			continue
		}
		provider := strings.TrimSpace(item.ModelInfo.LitellmProvider)
		id := raw
		if p, m, found := strings.Cut(raw, "/"); found {
			if provider == "" {
				provider = p
			}
			id = m
		}
		if id == "" {
			continue
		}
		target := raw
		if !strings.Contains(raw, "/") && provider != "" {
			target = provider + "/" + raw
		}
		opt := ModelOption{
			ID:                id,
			Name:              id,
			Provider:          provider,
			ModelTarget:       target,
			SupportsReasoning: item.ModelInfo.SupportsReasoning || strings.Contains(strings.ToLower(id), geminiModelMarker),
			Description:       servedModelDescription,
		}
		if catOpt, found := findCatalogOption(id, target); found {
			opt.Name = catOpt.Name
			opt.Description = catOpt.Description
			opt.SupportsReasoning = catOpt.SupportsReasoning
			opt.ReasoningLevel = catOpt.ReasoningLevel
		}
		if item.ModelInfo.DefaultReasoningEffort != nil && *item.ModelInfo.DefaultReasoningEffort != "" {
			opt.ReasoningLevel = *item.ModelInfo.DefaultReasoningEffort
		} else if opt.ReasoningLevel == "" && opt.SupportsReasoning {
			opt.ReasoningLevel = "dynamic"
		}
		for _, name := range []string{alias, id, target} {
			if name != "" {
				out.names[name] = true
			}
		}
		if alias == defaultAssistModel {
			out.inUse = opt
		}
		if !seen[target] {
			seen[target] = true
			out.options = append(out.options, opt)
		}
	}
	if len(out.options) == 0 {
		return servedModels{}, errNoServedModels
	}
	return out, nil
}

// chartProvider returns the litellm.modelProvider value that selects the
// provider the gateway names: the same name, except on an install whose
// MODEL_PROVIDER is openai_compatible, where the gateway's hosted_vllm is
// that value. Helm values naming hosted_vllm would render, and drop the
// endpoint, credentials and egress the chart sets for openai_compatible.
func (s *server) chartProvider(gatewayProvider string) string {
	if gatewayProvider == liteLLMHostedVLLMProvider && s.cfg.ModelProvider == chartOpenAICompatibleProvider {
		return chartOpenAICompatibleProvider
	}
	return gatewayProvider
}

// gatewayProvider is chartProvider's inverse: the provider name the gateway
// routes a chart provider under, which is what a model target starts with.
func (s *server) gatewayProvider(chartProvider string) string {
	if chartProvider == chartOpenAICompatibleProvider && s.cfg.ModelProvider == chartOpenAICompatibleProvider {
		return liteLLMHostedVLLMProvider
	}
	return chartProvider
}

// readServedModels asks the inference gateway which models it serves. It
// reports false when no gateway is configured or it did not answer with a
// list naming a model, in which case nothing can be said about any model.
func (s *server) readServedModels(ctx context.Context) (servedModels, bool) {
	if s.cfg.LiteLLMURL == "" {
		return servedModels{}, false
	}
	ctx, cancel := context.WithTimeout(ctx, modelFetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.cfg.LiteLLMURL+liteLLMModelInfoPath, nil)
	if err != nil {
		return servedModels{}, false
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return servedModels{}, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return servedModels{}, false
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxMetricsBodyBytes))
	if err != nil {
		return servedModels{}, false
	}
	served, err := parseServedModels(body)
	if err != nil {
		return servedModels{}, false
	}
	for i := range served.options {
		served.options[i].Provider = s.chartProvider(served.options[i].Provider)
	}
	served.inUse.Provider = s.chartProvider(served.inUse.Provider)
	return served, true
}

// getActiveModelFromCluster returns the model, target and reasoning level
// recorded on the PlatformAgent, without a model the gateway no longer serves.
func (s *server) getActiveModelFromCluster(ctx context.Context) (string, string, string) {
	if s.kube == nil || s.cfg.PodNamespace == "" {
		return "", "", ""
	}
	served, servedOK := s.readServedModels(ctx)
	return s.recordedModelAmong(ctx, served, servedOK)
}

// recordedModelAmong reads the model, target and reasoning level recorded on
// the PlatformAgent. A recorded model the gateway no longer serves, as after
// a Helm change of the default model, is returned empty so the caller shows
// the model in use; when the gateway could not be asked, or routes models
// by pattern, the record stands.
func (s *server) recordedModelAmong(ctx context.Context, served servedModels, servedOK bool) (string, string, string) {
	if s.kube == nil || s.cfg.PodNamespace == "" {
		return "", "", ""
	}
	var pa struct {
		Metadata struct {
			Annotations map[string]string `json:"annotations"`
		} `json:"metadata"`
	}
	path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
	if err := s.kube.get(ctx, path, &pa); err == nil {
		model := pa.Metadata.Annotations[activeModelAnnotation]
		target := pa.Metadata.Annotations[activeTargetAnnotation]
		reasoning := pa.Metadata.Annotations[reasoningLevelAnnotation]
		if servedOK && !served.serves(model, target) {
			model, target = "", ""
		}
		return model, target, reasoning
	}
	return "", "", ""
}

func findCatalogOption(id, target string) (ModelOption, bool) {
	for _, opt := range candidateModelCatalog {
		if opt.ID == id || opt.ModelTarget == target {
			return opt, true
		}
	}
	return ModelOption{}, false
}

func (s *server) getModelsResponse(ctx context.Context, probeAll ...bool) ModelsResponse {
	served, servedOK := s.readServedModels(ctx)
	activeModel, activeTarget, clusterReasoning := s.recordedModelAmong(ctx, served, servedOK)
	activeProvider := ""
	reasoningLevel := clusterReasoning

	modelInUse := s.cfg.ModelName
	candidates := candidateModelCatalog
	modelsSource := modelsSourcePresets
	if servedOK {
		candidates = served.options
		modelsSource = modelsSourceGateway
		if served.inUse.ID != "" {
			modelInUse = served.inUse.ID
		}
	}

	s.model.mu.Lock()
	cachedModel := s.model.value
	s.model.mu.Unlock()
	if servedOK && !served.serves(cachedModel.Name, "") {
		cachedModel = modelInfo{}
	}
	if activeModel == "" && (s.kube != nil || cachedModel.Name == "") && servedOK && served.inUse.ID != "" {
		// Nothing recorded: the preferred model is the model in use, under
		// the same ID the selector lists it by. With a cluster to record
		// on, the name held in memory is no record: the Insights read
		// refreshes it under its display name, which for a nested target
		// is shorter than the ID.
		activeModel = served.inUse.ID
		activeTarget = served.inUse.ModelTarget
		activeProvider = served.inUse.Provider
	}
	if activeModel == "" && cachedModel.Name != "" {
		activeModel = cachedModel.Name
		activeProvider = cachedModel.Provider
		if reasoningLevel == "" {
			reasoningLevel = cachedModel.ReasoningLevel
		}
	}

	// Read live model info from LiteLLM / cached insights model
	liveInfo, _ := s.readModel(ctx)
	if activeModel == "" {
		if liveInfo.Name != "" {
			activeModel = liveInfo.Name
			activeProvider = liveInfo.Provider
			if reasoningLevel == "" {
				reasoningLevel = liveInfo.ReasoningLevel
			}
		} else if s.cfg.AssistModel != "" && s.cfg.AssistModel != defaultAssistModel {
			activeModel = s.cfg.AssistModel
		} else if s.cfg.ModelName != "" {
			activeModel = s.cfg.ModelName
		} else {
			activeModel = defaultGeminiModel
		}
	}
	if activeProvider == "" {
		activeProvider = liveInfo.Provider
		if activeProvider == "" {
			activeProvider = s.cfg.ModelProvider
			if activeProvider == "" {
				activeProvider = "vertex_ai"
			}
		}
	}
	activeProvider = s.chartProvider(activeProvider)
	if reasoningLevel == "" {
		reasoningLevel = liveInfo.ReasoningLevel
	}

	shouldProbe := len(probeAll) > 0 && probeAll[0]
	verifiedModels := s.catalogModelsWithStatus(ctx, candidates, activeModel, activeTarget, shouldProbe)

	models := make([]ModelOption, len(verifiedModels))
	copy(models, verifiedModels)

	foundActive := false
	for i, m := range models {
		if m.ID == activeModel || (activeTarget != "" && m.ModelTarget == activeTarget) {
			foundActive = true
			if s.cfg.LiteLLMURL != "" {
				models[i].Verified = true
			}
			if activeTarget == "" {
				activeTarget = m.ModelTarget
			}
			if activeProvider == "" {
				activeProvider = m.Provider
			}
			if reasoningLevel == "" {
				reasoningLevel = m.ReasoningLevel
			}
			break
		}
	}

	if !foundActive && activeModel != "" {
		if activeTarget == "" {
			activeTarget = s.gatewayProvider(activeProvider) + "/" + activeModel
		}
		catOpt, found := findCatalogOption(activeModel, activeTarget)
		name := activeModel
		desc := "Active custom model"
		supports := strings.Contains(strings.ToLower(activeModel), "gemini")
		catReasoning := reasoningLevel
		if found {
			name = catOpt.Name
			desc = catOpt.Description
			supports = catOpt.SupportsReasoning
			if catReasoning == "" {
				catReasoning = catOpt.ReasoningLevel
			}
		}
		if catReasoning == "" && supports {
			catReasoning = "dynamic"
		}
		probeCache.mu.RLock()
		customVerified := probeCache.tested[activeTarget] || s.cfg.LiteLLMURL != ""
		probeCache.mu.RUnlock()
		customOpt := ModelOption{
			ID:                activeModel,
			Name:              name,
			Provider:          activeProvider,
			ModelTarget:       activeTarget,
			SupportsReasoning: supports,
			ReasoningLevel:    catReasoning,
			Verified:          customVerified,
			Description:       desc,
		}
		models = append([]ModelOption{customOpt}, models...)
		if reasoningLevel == "" {
			reasoningLevel = catReasoning
		}
	}

	if activeTarget == "" {
		activeTarget = s.gatewayProvider(activeProvider) + "/" + activeModel
	}
	if reasoningLevel == "" {
		reasoningLevel = "dynamic"
	}

	return ModelsResponse{
		ActiveModel:    activeModel,
		ActiveProvider: activeProvider,
		ActiveTarget:   activeTarget,
		ModelInUse:     modelInUse,
		ReasoningLevel: reasoningLevel,
		HelmValuesYAML: generateModelHelmValuesYAML(activeModel, activeProvider, activeTarget, reasoningLevel),
		ModelsSource:   modelsSource,
		Models:         models,
	}
}

func (s *server) handleModelsGet(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), modelsTimeout)
	defer cancel()

	probeAll := r.URL.Query().Get("probe") == "1" || strings.EqualFold(r.URL.Query().Get("probe"), "true")
	resp := s.getModelsResponse(ctx, probeAll)
	writeJSON(w, http.StatusOK, resp)
}

func (s *server) handleModelsTest(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req ModelSelectRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}
	modelID := strings.TrimSpace(req.Model)
	target := strings.TrimSpace(req.ModelTarget)
	if modelID == "" && target == "" {
		writeError(w, http.StatusBadRequest, "missing_target", "Model name or model_target is required.")
		return
	}
	if modelID != "" && !validModelTokenPattern.MatchString(modelID) {
		writeError(w, http.StatusBadRequest, "invalid_model", "Model name contains invalid characters.")
		return
	}
	if target != "" && !validModelTokenPattern.MatchString(target) {
		writeError(w, http.StatusBadRequest, "invalid_target", "Model target contains invalid characters.")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), modelsTimeout)
	defer cancel()
	if err := s.testModel(ctx, modelID, target); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{
			"model":    modelID,
			"target":   target,
			"verified": false,
			"error":    err.Error(),
		})
		return
	}
	if target != "" {
		probeCache.mu.Lock()
		probeCache.tested[target] = true
		probeCache.mu.Unlock()
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"model":    modelID,
		"target":   target,
		"verified": true,
	})
}

func (s *server) handleModelsPost(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req ModelSelectRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return
	}

	reqReasoning := strings.ToLower(strings.TrimSpace(req.ReasoningLevel))
	if reqReasoning != "" && !allowedReasoningLevels[reqReasoning] {
		writeError(w, http.StatusBadRequest, "invalid_reasoning_level",
			"Invalid reasoning level. Allowed values: dynamic, high, medium, low, off.")
		return
	}

	modelID := strings.TrimSpace(req.Model)
	if modelID == "" && reqReasoning == "" {
		writeError(w, http.StatusBadRequest, "invalid_request", "Either model or reasoning_level must be specified.")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), modelsTimeout)
	defer cancel()

	// Resolve current active model
	activeModel, _, _ := s.getActiveModelFromCluster(ctx)
	if activeModel == "" {
		s.model.mu.Lock()
		activeModel = s.model.value.Name
		s.model.mu.Unlock()
	}
	if activeModel == "" {
		activeModel = s.cfg.AssistModel
		if activeModel == "" || activeModel == defaultAssistModel {
			activeModel = s.cfg.ModelName
		}
		if activeModel == "" {
			activeModel = defaultGeminiModel
		}
	}

	// If model is omitted or is unchanged and reasoning level is specified, update reasoning level in-place
	if (modelID == "" || modelID == activeModel) && reqReasoning != "" {
		modelID = activeModel

		if s.kube != nil && s.cfg.PodNamespace != "" {
			patch := map[string]any{
				"metadata": map[string]any{
					"annotations": map[string]string{
						reasoningLevelAnnotation: reqReasoning,
					},
				},
			}
			path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
			if _, err := s.kube.doJSON(ctx, http.MethodPatch, path, patch, nil); err != nil {
				log.Printf(logPrefix+"could not persist %s on PlatformAgent: %v", reasoningLevelAnnotation, err)
				writeError(w, http.StatusInternalServerError, "persist_failed", fmt.Sprintf("Failed to update PlatformAgent annotations: %v", err))
				return
			}
		}

		s.model.mu.Lock()
		s.model.value.ReasoningLevel = reqReasoning
		s.model.readAt = time.Now()
		s.model.mu.Unlock()

		resp := s.getModelsResponse(ctx)
		resp.ReasoningLevel = reqReasoning
		resp.HelmValuesYAML = generateModelHelmValuesYAML(resp.ActiveModel, resp.ActiveProvider, resp.ActiveTarget, reqReasoning)
		writeJSON(w, http.StatusOK, resp)
		return
	}

	if !validModelTokenPattern.MatchString(modelID) {
		writeError(w, http.StatusBadRequest, "invalid_model", "Model name contains invalid characters.")
		return
	}

	provider := strings.TrimSpace(req.Provider)
	if provider != "" && !validModelTokenPattern.MatchString(provider) {
		writeError(w, http.StatusBadRequest, "invalid_provider", "Provider name contains invalid characters.")
		return
	}
	target := strings.TrimSpace(req.ModelTarget)
	if target != "" && !validModelTokenPattern.MatchString(target) {
		writeError(w, http.StatusBadRequest, "invalid_target", "Model target contains invalid characters.")
		return
	}
	reasoningLevel := reqReasoning
	supportsReasoning := false

	// Resolve against candidate catalog if not fully specified
	for _, opt := range candidateModelCatalog {
		if opt.ID == modelID || opt.ModelTarget == target {
			if provider == "" {
				provider = opt.Provider
			}
			if target == "" {
				target = opt.ModelTarget
			}
			supportsReasoning = opt.SupportsReasoning
			if reasoningLevel == "" {
				reasoningLevel = opt.ReasoningLevel
			}
			break
		}
	}

	if provider == "" {
		if strings.Contains(modelID, "/") {
			parts := strings.SplitN(modelID, "/", 2)
			provider = parts[0]
			modelID = parts[1]
			target = provider + "/" + modelID
		} else {
			provider = "vertex_ai"
		}
	}
	if target == "" {
		target = s.gatewayProvider(provider) + "/" + modelID
	}
	provider = s.chartProvider(provider)
	if !validModelTokenPattern.MatchString(modelID) || !validModelTokenPattern.MatchString(provider) || !validModelTokenPattern.MatchString(target) {
		writeError(w, http.StatusBadRequest, "invalid_model", "Model, provider, or target contains invalid characters.")
		return
	}
	if supportsReasoning && reasoningLevel == "" {
		reasoningLevel = "dynamic"
	}

	// Pre-flight check when an inference gateway is configured
	if s.cfg.LiteLLMURL != "" {
		if err := s.testModel(ctx, modelID, target); err != nil {
			log.Printf(logPrefix+"model pre-flight test failed for %s (%s): %v", modelID, target, err)
			writeJSON(w, http.StatusUnprocessableEntity, modelTestFailedResponse{
				Error:          modelTestFailedCode,
				Detail:         fmt.Sprintf("Pre-flight check failed: model %s (%s) did not respond successfully in this environment: %v. Active model was not changed.", modelID, target, err),
				Model:          modelID,
				Target:         target,
				Reason:         err.Error(),
				HelmValuesYAML: generateModelHelmValuesYAML(modelID, provider, target, reasoningLevel),
			})
			return
		}
		probeCache.mu.Lock()
		probeCache.tested[target] = true
		probeCache.mu.Unlock()
	}

	// Record model selection on PlatformAgent annotations
	if s.kube != nil && s.cfg.PodNamespace != "" {
		annotations := map[string]string{
			activeModelAnnotation:  modelID,
			activeTargetAnnotation: target,
		}
		if reasoningLevel != "" {
			annotations[reasoningLevelAnnotation] = reasoningLevel
		}
		patch := map[string]any{
			"metadata": map[string]any{
				"annotations": annotations,
			},
		}
		path := fmt.Sprintf(platformAgentsAPIPath, s.cfg.PodNamespace, s.cfg.PlatformAgentName)
		if _, err := s.kube.doJSON(ctx, http.MethodPatch, path, patch, nil); err != nil {
			log.Printf(logPrefix+"could not persist annotations on PlatformAgent: %v", err)
			writeError(w, http.StatusInternalServerError, "persist_failed", fmt.Sprintf("Failed to update PlatformAgent annotations: %v", err))
			return
		}
	}

	s.model.mu.Lock()
	s.model.value = modelInfo{
		Name:           modelID,
		Provider:       provider,
		ReasoningLevel: reasoningLevel,
	}
	s.model.readAt = time.Now()
	s.model.mu.Unlock()

	current := s.getModelsResponse(ctx)
	writeJSON(w, http.StatusOK, ModelsResponse{
		ActiveModel:    modelID,
		ActiveProvider: provider,
		ActiveTarget:   target,
		ModelInUse:     current.ModelInUse,
		ReasoningLevel: reasoningLevel,
		HelmValuesYAML: generateModelHelmValuesYAML(modelID, provider, target, reasoningLevel),
		ModelsSource:   current.ModelsSource,
		Models:         current.Models,
	})
}

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	activeModelAnnotation    = "kubeagents.x-k8s.io/active-model"
	activeTargetAnnotation   = "kubeagents.x-k8s.io/active-model-target"
	reasoningLevelAnnotation = "kubeagents.x-k8s.io/reasoning-level"
	modelsTimeout            = 15 * time.Second
)

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
	ActiveModel    string        `json:"active_model"`
	ActiveProvider string        `json:"active_provider"`
	ActiveTarget   string        `json:"active_target"`
	ReasoningLevel string        `json:"reasoning_level,omitempty"`
	HelmValuesYAML string        `json:"helm_values_yaml,omitempty"`
	Models         []ModelOption `json:"models"`
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
		Description:       "Frontier multimodal model with adaptive thinking (Recommended)",
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
		Description:       "Next-generation high-speed multimodal reasoning model",
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
		req.Header.Set("Content-Type", contentTypeJSON)
		if s.cfg.APIServerKey != "" {
			req.Header.Set("Authorization", bearerPrefix+s.cfg.APIServerKey)
		}
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
				if s.cfg.APIServerKey != "" {
					req2.Header.Set("Authorization", bearerPrefix+s.cfg.APIServerKey)
				}
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

// catalogModelsWithStatus returns the candidate model catalog, probing all
// candidates only when probeAll is true. Without probeAll, candidates carry
// Verified from prior probes (or when matching the active model on a cluster
// with LiteLLM configured), avoiding an 11-request fan-out on cold GETs.
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

func (s *server) getActiveModelFromCluster(ctx context.Context) (string, string, string) {
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
	activeModel, activeTarget, clusterReasoning := s.getActiveModelFromCluster(ctx)
	activeProvider := ""
	reasoningLevel := clusterReasoning

	s.model.mu.Lock()
	cachedModel := s.model.value
	s.model.mu.Unlock()
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
			activeModel = "gemini-3.5-flash"
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
	if reasoningLevel == "" {
		reasoningLevel = liveInfo.ReasoningLevel
	}

	shouldProbe := len(probeAll) > 0 && probeAll[0]
	verifiedModels := s.catalogModelsWithStatus(ctx, candidateModelCatalog, activeModel, activeTarget, shouldProbe)

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
			activeTarget = activeProvider + "/" + activeModel
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
		activeTarget = activeProvider + "/" + activeModel
	}
	if reasoningLevel == "" {
		reasoningLevel = "dynamic"
	}

	return ModelsResponse{
		ActiveModel:    activeModel,
		ActiveProvider: activeProvider,
		ActiveTarget:   activeTarget,
		ReasoningLevel: reasoningLevel,
		HelmValuesYAML: generateModelHelmValuesYAML(activeModel, activeProvider, activeTarget, reasoningLevel),
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
			activeModel = "gemini-3.5-flash"
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
		target = provider + "/" + modelID
	}
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
			writeError(w, http.StatusUnprocessableEntity, "model_test_failed",
				fmt.Sprintf("Pre-flight check failed: model %s (%s) did not respond successfully in this environment: %v. Active model was not changed.", modelID, target, err))
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

	writeJSON(w, http.StatusOK, ModelsResponse{
		ActiveModel:    modelID,
		ActiveProvider: provider,
		ActiveTarget:   target,
		ReasoningLevel: reasoningLevel,
		HelmValuesYAML: generateModelHelmValuesYAML(modelID, provider, target, reasoningLevel),
		Models:         s.getModelsResponse(ctx).Models,
	})
}

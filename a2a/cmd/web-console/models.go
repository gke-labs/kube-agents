package main

import (
	"bytes"
	"context"
	"encoding/json"
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
	litellmConfigMapName     = "litellm-config"
	modelsTimeout            = 15 * time.Second
)

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
		return nil
	}
	modelQuery := modelID
	if modelQuery == "" {
		modelQuery = target
	}
	payload := map[string]any{
		"model": modelQuery,
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
		defer drainAndClose(resp)
		if resp.StatusCode == http.StatusOK {
			return nil
		}
		detail := upstreamErrorDetail(resp)
		// If model alias failed and target is different, try target directly
		if target != "" && target != modelQuery {
			payload["model"] = target
			b2, _ := json.Marshal(payload)
			req2, _ := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.LiteLLMURL+"/v1/chat/completions", bytes.NewReader(b2))
			req2.Header.Set("Content-Type", contentTypeJSON)
			if s.cfg.APIServerKey != "" {
				req2.Header.Set("Authorization", bearerPrefix+s.cfg.APIServerKey)
			}
			resp2, err2 := s.client.Do(req2)
			if err2 == nil {
				defer drainAndClose(resp2)
				if resp2.StatusCode == http.StatusOK {
					return nil
				}
				detail = upstreamErrorDetail(resp2)
			}
		}
		// If 404 or 400 (model definitely not found/invalid in publisher), no need to retry
		if resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusBadRequest {
			return fmt.Errorf("HTTP %d: %s", resp.StatusCode, detail)
		}
		lastErr = fmt.Errorf("HTTP %d: %s", resp.StatusCode, detail)
	}
	return lastErr
}

// probeCandidateModels tests the candidate models against the environment
// and returns only those verified to work in the current cluster.
func (s *server) probeCandidateModels(ctx context.Context, candidates []ModelOption) []ModelOption {
	if s.cfg.LiteLLMURL == "" {
		out := make([]ModelOption, len(candidates))
		copy(out, candidates)
		for i := range out {
			out[i].Verified = true
		}
		return out
	}

	probeCache.mu.RLock()
	cacheValid := time.Since(probeCache.lastRun) < 5*time.Minute && len(probeCache.tested) > 0
	cachedResults := make(map[string]bool, len(probeCache.tested))
	for k, v := range probeCache.tested {
		cachedResults[k] = v
	}
	probeCache.mu.RUnlock()

	activeModel, activeTarget, _ := s.getActiveModelFromCluster(ctx)
	if activeTarget != "" {
		cachedResults[activeTarget] = true
	}
	if activeModel != "" {
		for _, opt := range candidates {
			if opt.ID == activeModel {
				cachedResults[opt.ModelTarget] = true
			}
		}
	}

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
		cachedResults = results
		probeCache.mu.Unlock()
	}

	verified := make([]ModelOption, 0, len(candidates))
	for _, opt := range candidates {
		if cachedResults[opt.ModelTarget] {
			opt.Verified = true
			verified = append(verified, opt)
		}
	}
	return verified
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

func (s *server) getModelsResponse(ctx context.Context) ModelsResponse {
	activeModel, activeTarget, clusterReasoning := s.getActiveModelFromCluster(ctx)
	activeProvider := ""
	reasoningLevel := clusterReasoning

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

	// Probe candidate models to only include models verified to work in this environment
	verifiedModels := s.probeCandidateModels(ctx, candidateModelCatalog)

	models := make([]ModelOption, len(verifiedModels))
	copy(models, verifiedModels)

	// Ensure active model is in the catalog and marked verified
	foundActive := false
	for i, m := range models {
		if m.ID == activeModel || m.ModelTarget == activeTarget {
			foundActive = true
			models[i].Verified = true
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
		customOpt := ModelOption{
			ID:                activeModel,
			Name:              name,
			Provider:          activeProvider,
			ModelTarget:       activeTarget,
			SupportsReasoning: supports,
			ReasoningLevel:    catReasoning,
			Verified:          true,
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
		Models:         models,
	}
}

func (s *server) handleModelsGet(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), modelsTimeout)
	defer cancel()

	resp := s.getModelsResponse(ctx)
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
			activeModel = "gemini-3.5-flash"
		}
	}

	// If model is omitted or is unchanged and reasoning level is specified, update reasoning level in-place
	if (modelID == "" || modelID == activeModel) && reqReasoning != "" {
		modelID = activeModel

		// 1. Update PlatformAgent annotation for reasoning level
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
			}
		}

		// 2. Update in-memory server state
		s.model.mu.Lock()
		s.model.value.ReasoningLevel = reqReasoning
		s.model.readAt = time.Now()
		s.model.mu.Unlock()

		resp := s.getModelsResponse(ctx)
		resp.ReasoningLevel = reqReasoning
		writeJSON(w, http.StatusOK, resp)
		return
	}

	if len(modelID) > 128 || strings.ContainsAny(modelID, " \t\r\n'\"`") {
		writeError(w, http.StatusBadRequest, "invalid_model", "Model name contains invalid characters.")
		return
	}

	provider := strings.TrimSpace(req.Provider)
	target := strings.TrimSpace(req.ModelTarget)
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
	if supportsReasoning && reasoningLevel == "" {
		reasoningLevel = "dynamic"
	}

	// Pre-flight check: test that the model works prior to changing cluster state!
	if err := s.testModel(ctx, modelID, target); err != nil {
		log.Printf(logPrefix+"model pre-flight test failed for %s (%s): %v", modelID, target, err)
		writeError(w, http.StatusUnprocessableEntity, "model_test_failed",
			fmt.Sprintf("Pre-flight check failed: model %s (%s) did not respond successfully in this environment: %v. Active model was not changed.", modelID, target, err))
		return
	}

	// 1. Update PlatformAgent annotations
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
		}
	}

	// 2. Update ConfigMap litellm-config
	if s.kube != nil && s.cfg.PodNamespace != "" {
		cmPath := fmt.Sprintf("/api/v1/namespaces/%s/configmaps/%s", s.cfg.PodNamespace, litellmConfigMapName)
		var cm struct {
			Data map[string]string `json:"data"`
		}
		if err := s.kube.get(ctx, cmPath, &cm); err == nil && cm.Data != nil {
			rawConfig := cm.Data["config.yaml"]
			if rawConfig != "" {
				updatedConfig := updateConfigYAMLWithModel(rawConfig, modelID, target)
				patchCM := map[string]any{
					"data": map[string]string{
						"config.yaml": updatedConfig,
					},
				}
				if _, err := s.kube.doJSON(ctx, http.MethodPatch, cmPath, patchCM, nil); err != nil {
					log.Printf(logPrefix+"could not patch %s ConfigMap: %v", litellmConfigMapName, err)
				} else {
					log.Printf(logPrefix+"updated %s with model %s (%s)", litellmConfigMapName, modelID, target)
					// Delete litellm pods to trigger immediate reload
					s.restartLiteLLMPods(ctx)
				}
			}
		}
	}

	// 3. Update in-memory server state
	s.cfg.AssistModel = modelID
	s.model.mu.Lock()
	s.model.value = modelInfo{
		Name:           modelID,
		Provider:       provider,
		ReasoningLevel: reasoningLevel,
	}
	s.model.readAt = time.Now()
	s.model.mu.Unlock()

	// Invalidate probe cache so active model is fresh
	probeCache.mu.Lock()
	probeCache.tested[target] = true
	probeCache.mu.Unlock()

	writeJSON(w, http.StatusOK, ModelsResponse{
		ActiveModel:    modelID,
		ActiveProvider: provider,
		ActiveTarget:   target,
		ReasoningLevel: reasoningLevel,
		Models:         s.getModelsResponse(ctx).Models,
	})
}

func (s *server) restartLiteLLMPods(ctx context.Context) {
	if s.kube == nil || s.cfg.PodNamespace == "" {
		return
	}
	deployPath := fmt.Sprintf("/apis/apps/v1/namespaces/%s/deployments/litellm", s.cfg.PodNamespace)
	patch := map[string]any{
		"spec": map[string]any{
			"template": map[string]any{
				"metadata": map[string]any{
					"annotations": map[string]string{
						"kubectl.kubernetes.io/restartedAt": time.Now().UTC().Format(time.RFC3339),
					},
				},
			},
		},
	}
	if _, err := s.kube.doJSON(ctx, http.MethodPatch, deployPath, patch, nil); err != nil {
		log.Printf(logPrefix+"could not trigger rolling update of litellm: %v", err)
	} else {
		log.Printf(logPrefix + "triggered zero-downtime rolling update of litellm deployment")
	}
}

// updateConfigYAMLWithModel updates model-default and hermes-agent targets in
// the LiteLLM config.yaml while preserving all other configuration and indentation.
func updateConfigYAMLWithModel(rawYAML, modelName, modelTarget string) string {
	lines := strings.Split(rawYAML, "\n")
	out := make([]string, 0, len(lines)+4)
	inModelDefault := false
	inHermesAgent := false
	hasAlias := false

	modelNamePattern := regexp.MustCompile(`^\s*-\s*model_name:\s*(.+)$`)
	aliasExactPattern := regexp.MustCompile(`^\s*-\s*model_name:\s*` + regexp.QuoteMeta(modelName) + `\s*$`)

	for _, line := range lines {
		trimmed := strings.TrimSpace(line)

		if aliasExactPattern.MatchString(trimmed) {
			hasAlias = true
		}

		if m := modelNamePattern.FindStringSubmatch(trimmed); len(m) > 1 {
			name := strings.Trim(m[1], `"' `)
			inModelDefault = (name == "model-default")
			inHermesAgent = (name == "hermes-agent")
		}

		if (inModelDefault || inHermesAgent) && strings.HasPrefix(trimmed, "model:") {
			indent := line[:strings.Index(line, "model:")]
			out = append(out, indent+"model: "+modelTarget)
			inModelDefault = false
			inHermesAgent = false
			continue
		}

		out = append(out, line)
	}

	result := strings.Join(out, "\n")

	// If the newly selected model alias isn't in model_list, add it before litellm_settings with 2-space indent
	if !hasAlias && !strings.Contains(result, "model_name: "+modelName) {
		newEntry := fmt.Sprintf("  - model_name: %s\n    litellm_params:\n      model: %s\n", modelName, modelTarget)
		if idx := strings.Index(result, "litellm_settings:"); idx >= 0 {
			result = result[:idx] + newEntry + result[idx:]
		} else {
			result += "\n" + newEntry
		}
	}

	return result
}

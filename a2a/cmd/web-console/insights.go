/*
Copyright 2026 Google LLC

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

// GET /api/insights: the banner's model, token usage and cost, the agent's
// identity, and the cluster.
//
// Usage comes from LiteLLM's Prometheus endpoint. This install's LiteLLM has
// no database, so its spend API is not there; the prometheus callback is. Its
// counters live in each replica's memory, so the console reads every replica
// and adds them up. The total covers the time since each replica last
// restarted, and the page says so. Replicas are found by a DNS lookup of a
// headless Service the chart renders for the console. That needs no
// Kubernetes API access and no RBAC.
//
// Default model and identity values are passed in by the chart at install
// time; preferred model and reasoning overrides are read from the
// PlatformAgent annotations when available. The console gets no credentials to
// read IAM, so a role granted later does not show here until the next install.

package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// litellmMetricsPort and litellmMetricsPath are where each LiteLLM pod
	// serves its Prometheus metrics: the proxy's own port, which the
	// litellm-policy NetworkPolicy admits from every pod in the namespace.
	litellmMetricsPort = "8080"
	litellmMetricsPath = "/metrics"

	// The LiteLLM counters the banner adds up. Each has one series per
	// model, key and team label set; every series is summed.
	metricInputTokens  = "litellm_input_tokens_metric_total"
	metricOutputTokens = "litellm_output_tokens_metric_total"
	metricTotalTokens  = "litellm_total_tokens_metric_total"
	metricSpendUSD     = "litellm_spend_metric_total"

	// insightsTimeout bounds one /api/insights request, the DNS lookup and
	// every replica fetch included.
	insightsTimeout = 5 * time.Second
	// peerFetchTimeout bounds one replica's /metrics read. A replica that
	// takes longer is counted as not read.
	peerFetchTimeout = 2 * time.Second
	// maxMetricsBodyBytes bounds one replica's /metrics body. LiteLLM's
	// exposition grows with the number of models, keys and teams; a few MiB
	// is far above what one install produces.
	maxMetricsBodyBytes = 4 << 20
	// maxLiteLLMPeers caps how many replicas one read fetches.
	maxLiteLLMPeers = 32
	// usageCacheTTL is how long one usage read is reused. The page refreshes
	// every 30 seconds; this keeps two tabs from doubling the fetches.
	usageCacheTTL = 15 * time.Second
	// modelCacheTTL is how long one model read is reused. Short enough that
	// screen reloads and drawer opens query LiteLLM live without doubling fetches.
	modelCacheTTL = 10 * time.Second
	// modelFetchTimeout bounds one /model/info fetch.
	modelFetchTimeout = 2 * time.Second

	errUsageDisabled  = "LiteLLM is not enabled on this install, so the console has no usage to read."
	errNoReplicaReady = "No LiteLLM replica is ready."
)

// hostResolver is the part of net.Resolver the console uses.
type hostResolver interface {
	LookupHost(ctx context.Context, host string) ([]string, error)
}

// metricsFetcher reads one replica's metrics body.
type metricsFetcher func(ctx context.Context, addr string) ([]byte, error)

type modelInfo struct {
	Name           string `json:"name"`
	Provider       string `json:"provider"`
	ReasoningLevel string `json:"reasoning_level,omitempty"`
}

// usageInfo is token usage and estimated spend summed across the LiteLLM
// replicas that answered. Error explains a missing or partial read.
type usageInfo struct {
	InputTokens   int64   `json:"input_tokens"`
	OutputTokens  int64   `json:"output_tokens"`
	TotalTokens   int64   `json:"total_tokens"`
	SpendUSD      float64 `json:"spend_usd"`
	ReplicasRead  int     `json:"replicas_read"`
	ReplicasFound int     `json:"replicas_found"`
	Error         string  `json:"error"`
}

type identityInfo struct {
	KubernetesServiceAccount string   `json:"kubernetes_service_account"`
	GCPServiceAccount        string   `json:"gcp_service_account"`
	Roles                    []string `json:"roles"`
}

type clusterInfo struct {
	Name     string `json:"name"`
	Project  string `json:"project"`
	Location string `json:"location"`
}

// repositoryInfo is one repository the agent may open pull requests on or
// read, as declared at install (spec.integration.repositories).
type repositoryInfo struct {
	Forge      string `json:"forge"`
	Provider   string `json:"provider"`
	Host       string `json:"host,omitempty"`
	Repository string `json:"repository"`
	Role       string `json:"role"`
	URL        string `json:"url,omitempty"`
	BaseBranch string `json:"base_branch,omitempty"`
	Category   string `json:"category,omitempty"`
}

type insightsResponse struct {
	Model        modelInfo        `json:"model"`
	Usage        usageInfo        `json:"usage"`
	Identity     identityInfo     `json:"identity"`
	Cluster      clusterInfo      `json:"cluster"`
	Repositories []repositoryInfo `json:"repositories"`
	Topology     string           `json:"topology,omitempty"`
}

// usageCache holds the last usage read. refresh serializes reads, so
// concurrent requests wait for one fetch instead of each starting their own.
type usageCache struct {
	refresh sync.Mutex
	mu      sync.Mutex
	value   usageInfo
	readAt  time.Time
}

// modelCache holds the last model read from the inference gateway.
type modelCache struct {
	refresh sync.Mutex
	mu      sync.Mutex
	value   modelInfo
	readAt  time.Time
}

func (s *server) handleInsights(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), insightsTimeout)
	defer cancel()
	roles := s.cfg.AgentRoles
	if roles == nil {
		roles = []string{}
	}
	repos := s.currentRepositories(ctx)
	if repos == nil {
		repos = []repositoryInfo{}
	}
	s.reposMu.RLock()
	topology := s.cachedTopology
	s.reposMu.RUnlock()
	writeJSON(w, http.StatusOK, insightsResponse{
		Model:        s.currentModel(ctx),
		Usage:        s.currentUsage(ctx),
		Repositories: repos,
		Topology:     topology,
		Identity: identityInfo{
			KubernetesServiceAccount: s.cfg.AgentKSA,
			GCPServiceAccount:        s.cfg.AgentGSA,
			Roles:                    roles,
		},
		Cluster: clusterInfo{Name: s.cfg.ClusterName, Project: s.cfg.ProjectID, Location: s.cfg.Location},
	})
}

// currentModel returns the live model from LiteLLM, cached briefly so screen
// loads and tab polls see live updates without doubling queries.
func (s *server) currentModel(ctx context.Context) modelInfo {
	s.model.mu.Lock()
	if !s.model.readAt.IsZero() && time.Since(s.model.readAt) < modelCacheTTL {
		v := s.model.value
		s.model.mu.Unlock()
		return v
	}
	s.model.mu.Unlock()

	fallback := modelInfo{Name: s.cfg.ModelName, Provider: s.cfg.ModelProvider}
	if s.cfg.LiteLLMURL == "" && s.fetchModel == nil && s.kube == nil {
		return fallback
	}

	s.model.refresh.Lock()
	defer s.model.refresh.Unlock()
	s.model.mu.Lock()
	if !s.model.readAt.IsZero() && time.Since(s.model.readAt) < modelCacheTTL {
		v := s.model.value
		s.model.mu.Unlock()
		return v
	}
	s.model.mu.Unlock()

	activeModel, _, clusterReasoning := s.getActiveModelFromCluster(ctx)
	v, err := s.readModel(ctx)
	if err != nil {
		s.model.mu.Lock()
		v = s.model.value
		s.model.mu.Unlock()
		if v.Name != "" {
			if activeModel != "" {
				v.Name = activeModel
			}
			if clusterReasoning != "" {
				v.ReasoningLevel = clusterReasoning
			}
			s.model.mu.Lock()
			s.model.value, s.model.readAt = v, time.Now()
			s.model.mu.Unlock()
			return v
		}
		if activeModel != "" {
			fallback.Name = activeModel
		}
		if clusterReasoning != "" {
			fallback.ReasoningLevel = clusterReasoning
		}
		s.model.mu.Lock()
		s.model.value, s.model.readAt = fallback, time.Now()
		s.model.mu.Unlock()
		return fallback
	}
	if activeModel != "" {
		v.Name = activeModel
	}
	if clusterReasoning != "" {
		v.ReasoningLevel = clusterReasoning
	}
	s.model.mu.Lock()
	s.model.value, s.model.readAt = v, time.Now()
	s.model.mu.Unlock()
	return v
}

func (s *server) readModel(ctx context.Context) (modelInfo, error) {
	fetch := s.fetchModel
	if fetch == nil {
		fetch = s.fetchLiveModel
	}
	ctx, cancel := context.WithTimeout(ctx, modelFetchTimeout)
	defer cancel()
	return fetch(ctx, s.cfg.LiteLLMURL)
}

func (s *server) fetchLiveModel(ctx context.Context, baseURL string) (modelInfo, error) {
	if baseURL == "" {
		return modelInfo{}, errors.New("no LiteLLM URL configured")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/model/info", nil)
	if err != nil {
		return modelInfo{}, err
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return modelInfo{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return modelInfo{}, fmt.Errorf("LiteLLM /model/info answered HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxMetricsBodyBytes))
	if err != nil {
		return modelInfo{}, err
	}
	preferred := s.cfg.AssistModel
	s.model.mu.Lock()
	if s.model.value.Name != "" {
		preferred = s.model.value.Name
	}
	s.model.mu.Unlock()
	return parseLiteLLMModelInfo(body, preferred, s.cfg.ModelName, s.cfg.ModelProvider)
}

func parseLiteLLMModelInfo(body []byte, preferredModel, defaultName, defaultProvider string) (modelInfo, error) {
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
		return modelInfo{}, err
	}
	if len(payload.Data) == 0 {
		return modelInfo{}, errors.New("LiteLLM reported no models")
	}

	idx := -1
	targets := []string{preferredModel, defaultAssistModel, "hermes-agent"}
	for _, target := range targets {
		if target == "" {
			continue
		}
		for i, item := range payload.Data {
			if item.ModelName == target {
				idx = i
				break
			}
		}
		if idx >= 0 {
			break
		}
	}
	if idx < 0 {
		for i, item := range payload.Data {
			if item.LitellmParams.Model != "" || item.ModelInfo.Key != "" {
				idx = i
				break
			}
		}
	}
	if idx < 0 {
		idx = 0
	}

	item := payload.Data[idx]
	raw := strings.TrimSpace(item.LitellmParams.Model)
	if raw == "" {
		raw = strings.TrimSpace(item.ModelInfo.Key)
	}
	if raw == "" {
		raw = strings.TrimSpace(item.ModelName)
	}

	provider := strings.TrimSpace(item.ModelInfo.LitellmProvider)
	if strings.Contains(raw, "/") {
		p, m, _ := strings.Cut(raw, "/")
		if provider == "" {
			provider = p
		}
		raw = m
	}
	if lastSlash := strings.LastIndex(raw, "/"); lastSlash != -1 {
		raw = raw[lastSlash+1:]
	}
	name := strings.TrimSpace(raw)
	if name == "" {
		name = defaultName
	}
	if provider == "" {
		provider = defaultProvider
	}
	reasoning := ""
	if item.ModelInfo.DefaultReasoningEffort != nil && *item.ModelInfo.DefaultReasoningEffort != "" {
		reasoning = *item.ModelInfo.DefaultReasoningEffort
	} else if item.ModelInfo.SupportsReasoning || strings.Contains(strings.ToLower(name), "gemini") {
		reasoning = "dynamic"
	}
	return modelInfo{Name: name, Provider: provider, ReasoningLevel: reasoning}, nil
}

// currentUsage returns the cached usage while it is fresh, and reads the
// replicas again otherwise.
func (s *server) currentUsage(ctx context.Context) usageInfo {
	if s.cfg.LiteLLMPeersHost == "" {
		return usageInfo{Error: errUsageDisabled}
	}
	s.usage.mu.Lock()
	if !s.usage.readAt.IsZero() && time.Since(s.usage.readAt) < usageCacheTTL {
		v := s.usage.value
		s.usage.mu.Unlock()
		return v
	}
	s.usage.mu.Unlock()

	s.usage.refresh.Lock()
	defer s.usage.refresh.Unlock()
	s.usage.mu.Lock()
	if !s.usage.readAt.IsZero() && time.Since(s.usage.readAt) < usageCacheTTL {
		v := s.usage.value
		s.usage.mu.Unlock()
		return v
	}
	s.usage.mu.Unlock()

	v := s.readUsage(ctx)
	if v.ReplicasRead > 0 && ctx.Err() == nil {
		s.usage.mu.Lock()
		s.usage.value, s.usage.readAt = v, time.Now()
		s.usage.mu.Unlock()
	}
	return v
}

// readUsage finds every LiteLLM replica and sums their counters. A replica
// that fails is left out and named in Error; the rest are still summed.
func (s *server) readUsage(ctx context.Context) usageInfo {
	addrs, err := s.resolver.LookupHost(ctx, s.cfg.LiteLLMPeersHost)
	if err != nil {
		var dnsErr *net.DNSError
		if errors.As(err, &dnsErr) && dnsErr.IsNotFound {
			return usageInfo{Error: errNoReplicaReady}
		}
		return usageInfo{Error: "Could not find the LiteLLM replicas: " + err.Error()}
	}
	out := usageInfo{ReplicasFound: len(addrs)}
	if len(addrs) > maxLiteLLMPeers {
		addrs = addrs[:maxLiteLLMPeers]
	}
	if len(addrs) == 0 {
		out.Error = errNoReplicaReady
		return out
	}

	type result struct {
		addr   string
		totals map[string]float64
		err    error
	}
	results := make([]result, len(addrs))
	var wg sync.WaitGroup
	for i, addr := range addrs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			peerCtx, cancel := context.WithTimeout(ctx, peerFetchTimeout)
			defer cancel()
			body, err := s.fetchMetrics(peerCtx, addr)
			if err != nil {
				results[i] = result{addr: addr, err: err}
				return
			}
			totals, err := sumMetrics(bytes.NewReader(body))
			results[i] = result{addr: addr, totals: totals, err: err}
		}()
	}
	wg.Wait()

	var sums = map[string]float64{}
	var failures []string
	for _, res := range results {
		if res.err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", res.addr, res.err))
			continue
		}
		out.ReplicasRead++
		for name, v := range res.totals {
			sums[name] += v
		}
	}
	out.InputTokens = int64(math.Round(sums[metricInputTokens]))
	out.OutputTokens = int64(math.Round(sums[metricOutputTokens]))
	out.TotalTokens = int64(math.Round(sums[metricTotalTokens]))
	out.SpendUSD = sums[metricSpendUSD]
	switch {
	case len(failures) > 0 && out.ReplicasFound > len(addrs):
		out.Error = fmt.Sprintf("%d of %d LiteLLM replicas could not be read (%d over the %d-replica cap were skipped): %s",
			len(failures), out.ReplicasFound, out.ReplicasFound-len(addrs), maxLiteLLMPeers, strings.Join(failures, "; "))
	case len(failures) > 0:
		out.Error = fmt.Sprintf("%d of %d LiteLLM replicas could not be read: %s",
			len(failures), out.ReplicasFound, strings.Join(failures, "; "))
	case out.ReplicasFound > len(addrs):
		out.Error = fmt.Sprintf("Read the first %d of %d LiteLLM replicas (%d over the %d-replica cap were skipped).",
			len(addrs), out.ReplicasFound, out.ReplicasFound-len(addrs), maxLiteLLMPeers)
	}
	return out
}

// fetchPeerMetrics reads one replica's /metrics body, up to
// maxMetricsBodyBytes. A larger body is an error, not a silent truncation
// that would undercount.
func (s *server) fetchPeerMetrics(ctx context.Context, addr string) ([]byte, error) {
	target := "http://" + net.JoinHostPort(addr, s.peerPort) + litellmMetricsPath
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer drainAndClose(resp)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("answered HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxMetricsBodyBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxMetricsBodyBytes {
		return nil, errors.New("metrics body is larger than the console reads")
	}
	return body, nil
}

// usageMetrics are the series names sumMetrics keeps. The matching
// _created series and every other metric are skipped.
var usageMetrics = map[string]bool{
	metricInputTokens: true, metricOutputTokens: true, metricTotalTokens: true, metricSpendUSD: true,
}

// sumMetrics adds up every series of each usage metric in a Prometheus text
// exposition. Comment lines and other metrics are skipped. A sample whose
// value does not parse is an error, so a garbled body is not read as zero.
func sumMetrics(r io.Reader) (map[string]float64, error) {
	totals := map[string]float64{}
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, bufio.MaxScanTokenSize), maxMetricsBodyBytes)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, rest := line, ""
		if i := strings.IndexAny(line, "{ \t"); i >= 0 {
			name, rest = line[:i], line[i:]
		}
		if !usageMetrics[name] {
			continue
		}
		// Labels may hold spaces inside quoted values; the value follows the
		// last closing brace.
		if strings.HasPrefix(rest, "{") {
			end := strings.LastIndex(rest, "}")
			if end < 0 {
				return nil, fmt.Errorf("series %s has no closing brace", name)
			}
			rest = rest[end+1:]
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			return nil, fmt.Errorf("series %s has no value", name)
		}
		v, err := strconv.ParseFloat(fields[0], 64)
		if err != nil {
			return nil, fmt.Errorf("series %s has a value that is not a number", name)
		}
		if math.IsNaN(v) || math.IsInf(v, 0) {
			continue
		}
		totals[name] += v
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return totals, nil
}

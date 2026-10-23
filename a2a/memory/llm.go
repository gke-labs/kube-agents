package memory

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"sigs.k8s.io/yaml"
)

const (
	defaultLLMTimeout = 30 * time.Second
	defaultLLMModel   = "model-default"
)

// LLMConfig configures single-shot LiteLLM completions for OKF synthesis.
type LLMConfig struct {
	BaseURL string
	Model   string
	APIKey  string
	Client  *http.Client
}

// SynthesizedRecord holds the structured fields returned by a single-shot LiteLLM synthesis call.
type SynthesizedRecord struct {
	Title        string       `json:"title,omitempty"`
	Context      string       `json:"context,omitempty"`
	Decision     string       `json:"decision,omitempty"`
	Consequences string       `json:"consequences,omitempty"`
	RootCause    string       `json:"root_cause,omitempty"`
	Fix          string       `json:"fix,omitempty"`
	Verification string       `json:"verification,omitempty"`
	Prevention   string       `json:"prevention,omitempty"`
	Guard        *RecordGuard `json:"guard,omitempty"`
}

// ResolveLLMConfig discovers the LiteLLM gateway URL, model, and API key from
// environment variables or Hermes config.yaml.
func ResolveLLMConfig() LLMConfig {
	base := firstNonEmpty(
		os.Getenv("DECISION_MEMORY_LLM_URL"),
		os.Getenv("LITELLM_BASE_URL"),
		os.Getenv("OPENAI_BASE_URL"),
	)
	model := firstNonEmpty(
		os.Getenv("DECISION_MEMORY_LLM_MODEL"),
		os.Getenv("LITELLM_MODEL"),
		os.Getenv("API_SERVER_MODEL_NAME"),
	)
	apiKey := firstNonEmpty(
		os.Getenv("DECISION_MEMORY_LLM_API_KEY"),
		os.Getenv("LITELLM_API_KEY"),
		os.Getenv("OPENAI_API_KEY"),
	)

	if base == "" || model == "" {
		for _, dir := range []string{os.Getenv("HERMES_HOME"), "/opt/data", "/opt/defaults"} {
			if dir == "" {
				continue
			}
			raw, err := os.ReadFile(filepath.Join(dir, "config.yaml"))
			if err != nil {
				continue
			}
			var cfg struct {
				Model struct {
					BaseURL string `yaml:"base_url"`
					Model   string `yaml:"model"`
					Default string `yaml:"default"`
					APIKey  string `yaml:"api_key"`
				} `yaml:"model"`
			}
			if yaml.Unmarshal(raw, &cfg) == nil {
				if base == "" && cfg.Model.BaseURL != "" {
					base = strings.TrimSpace(cfg.Model.BaseURL)
				}
				if model == "" {
					if cfg.Model.Model != "" {
						model = strings.TrimSpace(cfg.Model.Model)
					} else if cfg.Model.Default != "" {
						model = strings.TrimSpace(cfg.Model.Default)
					}
				}
				if apiKey == "" && cfg.Model.APIKey != "" {
					apiKey = strings.TrimSpace(cfg.Model.APIKey)
				}
				break
			}
		}
	}
	if base == "" && os.Getenv("INFERENCE_GATEWAY_SERVICE_HOST") != "" {
		base = "http://inference-gateway/v1"
	}
	if model == "" {
		model = defaultLLMModel
	}
	if apiKey == "" {
		apiKey = "none"
	}
	return LLMConfig{
		BaseURL: strings.TrimRight(base, "/"),
		Model:   model,
		APIKey:  apiKey,
		Client:  &http.Client{Timeout: defaultLLMTimeout},
	}
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if s := strings.TrimSpace(v); s != "" {
			return s
		}
	}
	return ""
}

var reJSONCodeFence = regexp.MustCompile("(?s)```(?:json)?\\s*(\\{.*?\\})\\s*```")

func extractJSONObject(raw string) (*SynthesizedRecord, error) {
	text := strings.TrimSpace(raw)
	if m := reJSONCodeFence.FindStringSubmatch(text); m != nil {
		text = m[1]
	} else {
		start := strings.Index(text, "{")
		end := strings.LastIndex(text, "}")
		if start >= 0 && end > start {
			text = text[start : end+1]
		}
	}
	var out SynthesizedRecord
	if err := json.Unmarshal([]byte(text), &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// Synthesize calls the OpenAI-compatible chat/completions endpoint once to draft
// an OKF record from an OutcomeEvent or recurrence candidate. Returns nil if the
// endpoint is unconfigured or fails, allowing deterministic fallback.
func (cfg LLMConfig) Synthesize(ctx context.Context, rtype RecordType, payload any) *SynthesizedRecord {
	if cfg.BaseURL == "" {
		return nil
	}
	client := cfg.Client
	if client == nil {
		client = &http.Client{Timeout: defaultLLMTimeout}
	}
	var schemaHint string
	if rtype == RecordTypeDecision {
		schemaHint = `{"title": "<concise decision title>", "context": "<why the decision was needed>", "decision": "<the architectural rule and trade-offs>", "consequences": "<how future changes must comply>", "guard": {"rule": "<1-line invariant>", "severity": "block", "expect": {"file": "<path>", "path": "<yaml_path_or_$text>", "op": "<op>", "value": "<val>"}}}`
	} else {
		schemaHint = `{"title": "<concise lesson title>", "root_cause": "<what happened and why it failed>", "fix": "<what change resolved it>", "verification": "<how resolution was verified>", "prevention": "<rule to prevent repeat outage>", "guard": {"rule": "<1-line invariant>", "severity": "block", "expect": {"file": "<path>", "path": "<yaml_path_or_$text>", "op": "<op>", "value": "<val>"}}}`
	}
	evidenceJSON, _ := json.MarshalIndent(payload, "", "  ")
	prompt := fmt.Sprintf(
		"You are synthesizing a Kubernetes GitOps Operational Knowledge Format (OKF) %s record from verified pull request and incident history.\n"+
			"Preserve all exact identifiers, PR/issue numbers, file paths, and resource quantities.\n"+
			"Return ONLY valid JSON matching this schema (omit 'guard' if no machine-checkable invariant applies):\n%s\n\nInput evidence:\n%s",
		rtype, schemaHint, string(evidenceJSON),
	)
	reqPayload := map[string]any{
		"model": cfg.Model,
		"messages": []map[string]string{
			{"role": "system", "content": "Respond with a single JSON object only."},
			{"role": "user", "content": prompt},
		},
	}
	rawReq, err := json.Marshal(reqPayload)
	if err != nil {
		return nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, cfg.BaseURL+"/chat/completions", bytes.NewReader(rawReq))
	if err != nil {
		return nil
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)

	resp, err := client.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil
	}
	rawResp, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil
	}
	var chatResp struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(rawResp, &chatResp); err != nil || len(chatResp.Choices) == 0 {
		return nil
	}
	synth, err := extractJSONObject(chatResp.Choices[0].Message.Content)
	if err != nil {
		return nil
	}
	return synth
}

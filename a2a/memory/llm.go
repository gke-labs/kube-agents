package memory

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"

	"sigs.k8s.io/yaml"
)

const (
	defaultLLMTimeout          = 30 * time.Second
	defaultLLMModel            = "model-default"
	defaultInferenceGatewayURL = "http://inference-gateway/v1"
	defaultPlaceholderAPIKey   = "none"
	defaultConfigFilename      = "config.yaml"
	maxJudgeDiffRunes          = 12000
	maxLLMResponseBytes        = 4 << 20
	defaultAgentDataDir        = "/opt/data"
	defaultAgentDefaultsDir    = "/opt/defaults"
)

var reJSONCodeFence = regexp.MustCompile("(?s)```(?:json)?\\s*(\\{.*?\\})\\s*```")

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

// OverseerFinding represents a semantic policy violation identified by the LLM Overseer.
type OverseerFinding struct {
	ID       string `json:"id"`
	Severity string `json:"severity"`
	Rule     string `json:"rule"`
	Reason   string `json:"reason"`
}

func readScopedConfigYAML(dir string) ([]byte, error) {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	return root.ReadFile(defaultConfigFilename)
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

	if base == "" || model == "" || apiKey == "" {
		for _, dir := range []string{os.Getenv("HERMES_HOME"), defaultAgentDataDir, defaultAgentDefaultsDir} {
			if strings.TrimSpace(dir) == "" {
				continue
			}
			raw, err := readScopedConfigYAML(dir)
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
				if base != "" && model != "" && apiKey != "" {
					break
				}
			}
		}
	}
	if base == "" && os.Getenv("INFERENCE_GATEWAY_SERVICE_HOST") != "" {
		base = defaultInferenceGatewayURL
	}
	if model == "" {
		model = defaultLLMModel
	}
	if apiKey == "" {
		apiKey = defaultPlaceholderAPIKey
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

func unwrapJSONObjectString(raw string) string {
	text := strings.TrimSpace(raw)
	if m := reJSONCodeFence.FindStringSubmatch(text); m != nil {
		return m[1]
	}
	if start, end := strings.Index(text, "{"), strings.LastIndex(text, "}"); start >= 0 && end > start {
		return text[start : end+1]
	}
	return text
}

func (cfg LLMConfig) chatCompletionJSON(ctx context.Context, prompt string) (string, bool) {
	if cfg.BaseURL == "" {
		return "", false
	}
	client := cfg.Client
	if client == nil {
		client = &http.Client{Timeout: defaultLLMTimeout}
	}
	reqPayload := map[string]any{
		"model": cfg.Model,
		"messages": []map[string]string{
			{"role": "system", "content": "Respond with a single JSON object only."},
			{"role": "user", "content": prompt},
		},
	}
	rawReq, err := json.Marshal(reqPayload)
	if err != nil {
		return "", false
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, cfg.BaseURL+"/chat/completions", bytes.NewReader(rawReq))
	if err != nil {
		return "", false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)

	resp, err := client.Do(req)
	if err != nil {
		return "", false
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", false
	}
	rawResp, err := io.ReadAll(io.LimitReader(resp.Body, maxLLMResponseBytes))
	if err != nil {
		return "", false
	}
	var chatResp struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(rawResp, &chatResp); err != nil || len(chatResp.Choices) == 0 {
		return "", false
	}
	return unwrapJSONObjectString(chatResp.Choices[0].Message.Content), true
}

// Synthesize calls the OpenAI-compatible chat/completions endpoint once to draft
// an OKF record from an OutcomeEvent or recurrence candidate. Returns nil if the
// endpoint is unconfigured or fails, allowing deterministic fallback.
func (cfg LLMConfig) Synthesize(ctx context.Context, rtype RecordType, payload any) *SynthesizedRecord {
	var schemaHint string
	if rtype == RecordTypeDecision {
		schemaHint = `{"title": "<concise decision title>", "context": "<why the decision was needed>", "decision": "<the architectural rule and trade-offs>", "consequences": "<how future changes must comply>", "guard": {"rule": "<1-line invariant>", "severity": "<warn_or_block>", "expect": {"file": "<path>", "path": "<yaml_path_or_$text>", "op": "<op>", "value": "<val>"}}}`
	} else {
		schemaHint = `{"title": "<concise lesson title>", "root_cause": "<what happened and why it failed>", "fix": "<what change resolved it>", "verification": "<how resolution was verified>", "prevention": "<rule to prevent repeat outage>", "guard": {"rule": "<1-line invariant>", "severity": "<warn_or_block>", "expect": {"file": "<path>", "path": "<yaml_path_or_$text>", "op": "<op>", "value": "<val>"}}}`
	}
	evidenceJSON, _ := json.MarshalIndent(payload, "", "  ")
	prompt := fmt.Sprintf(
		"You are synthesizing a Kubernetes GitOps Operational Knowledge Framework (OKF) %s record from verified pull request and incident history.\n"+
			"Preserve all exact identifiers, PR/issue numbers, file paths, and resource quantities.\n"+
			"Set guard.severity to \"warn\" by default (for reviewer comments, edit-before-merge adjustments, naming/quota conventions, and general operational guidance).\n"+
			"Set guard.severity to \"block\" ONLY when the pull request resolved a verified crash, outage, or availability failure with a concrete root cause (such as OOMKilled, CrashLoopBackOff, quorum loss, or pod eviction deadlock).\n"+
			"Return ONLY valid JSON matching this schema (omit 'guard' if no machine-checkable invariant applies):\n%s\n\nInput evidence:\n%s",
		rtype, schemaHint, string(evidenceJSON),
	)
	jsonText, ok := cfg.chatCompletionJSON(ctx, prompt)
	if !ok {
		return nil
	}
	var out SynthesizedRecord
	if err := json.Unmarshal([]byte(jsonText), &out); err != nil {
		return nil
	}
	return &out
}

func truncateRunes(s string, maxRunes int) string {
	runes := []rune(s)
	if len(runes) <= maxRunes {
		return s
	}
	return string(runes[:maxRunes])
}

// JudgeDiff evaluates a proposed Git diff against active natural-language OKF records
// that do not already have a deterministic guard.expect assertion. Returns nil when the
// LLM endpoint is unconfigured or when all records are satisfied.
func (cfg LLMConfig) JudgeDiff(ctx context.Context, records []Record, diffText string) []OverseerFinding {
	if cfg.BaseURL == "" || strings.TrimSpace(diffText) == "" {
		return nil
	}
	activeByID := map[string]Record{}
	var proseRecords []map[string]any
	for _, r := range records {
		if r.Meta.Status != StatusActive {
			continue
		}
		if r.Meta.Guard != nil && r.Meta.Guard.Expect != nil {
			continue
		}
		activeByID[r.Meta.ID] = r
		proseRecords = append(proseRecords, map[string]any{
			"id":    r.Meta.ID,
			"type":  r.Meta.Type,
			"title": r.Meta.Title,
			"scope": r.Meta.Scope,
			"guard": r.Meta.Guard,
			"body":  r.Body,
		})
	}
	if len(proseRecords) == 0 {
		return nil
	}
	recJSON, _ := json.MarshalIndent(proseRecords, "", "  ")
	clippedDiff := truncateRunes(diffText, maxJudgeDiffRunes)
	prompt := fmt.Sprintf(
		"You are the pre-push Overseer for a Kubernetes GitOps repository.\n"+
			"Evaluate whether the proposed git diff violates any of the following active architectural decisions or lessons.\n"+
			"Return ONLY a JSON object of the form {\"violations\": [{\"id\": \"<DEC-or-LES-ID>\", \"severity\": \"<warn_or_block>\", \"rule\": \"<violated rule>\", \"reason\": \"<specific explanation>\"}]}.\n"+
			"If no active policy is violated, return {\"violations\": []}.\n\nActive records:\n%s\n\nProposed diff:\n%s",
		string(recJSON), clippedDiff,
	)
	jsonText, ok := cfg.chatCompletionJSON(ctx, prompt)
	if !ok {
		return nil
	}
	var parsed struct {
		Violations []OverseerFinding `json:"violations"`
	}
	if json.Unmarshal([]byte(jsonText), &parsed) != nil {
		return nil
	}
	var verified []OverseerFinding
	for _, v := range parsed.Violations {
		rec, exists := activeByID[strings.TrimSpace(v.ID)]
		if !exists {
			continue
		}
		sev := "warn"
		if rec.Meta.Guard != nil && rec.Meta.Guard.Severity != "" {
			sev = rec.Meta.Guard.Severity
		}
		ruleText := strings.TrimSpace(v.Rule)
		if ruleText == "" && rec.Meta.Guard != nil && rec.Meta.Guard.Rule != "" {
			ruleText = rec.Meta.Guard.Rule
		}
		if ruleText == "" {
			ruleText = rec.Meta.Title
		}
		verified = append(verified, OverseerFinding{
			ID:       rec.Meta.ID,
			Severity: sev,
			Rule:     ruleText,
			Reason:   strings.TrimSpace(v.Reason),
		})
	}
	return verified
}

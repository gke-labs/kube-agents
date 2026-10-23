package memory

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	defaultHindsightTimeout = 45 * time.Second

	bankReflectMission = "The decision register of a Kubernetes platform team. " +
		"Each memory is a recorded infrastructure decision or a lesson learned from a fixed incident, " +
		"with its identifier, status (proposed, active, rejected, superseded), and pull request. " +
		"Answer by citing record identifiers and pull requests; prefer active records over " +
		"superseded or rejected ones and warn when a record is still proposed."
)

// HindsightClient manages the kube-agents-decisions bank over Hindsight's REST API.
type HindsightClient struct {
	baseURL    string
	bankID     string
	httpClient *http.Client
}

// HindsightRecallResult represents one retrieved memory chunk from Hindsight.
type HindsightRecallResult struct {
	ID         string   `json:"id"`
	DocumentID string   `json:"document_id"`
	Text       string   `json:"text"`
	Tags       []string `json:"tags"`
}

// HindsightDocument represents one document summary from GET /documents.
type HindsightDocument struct {
	ID               string            `json:"id"`
	Tags             []string          `json:"tags"`
	TextLength       int               `json:"text_length"`
	DocumentMetadata map[string]string `json:"document_metadata"`
}

// NewHindsightClient constructs a client for the given Hindsight API URL and bank ID.
func NewHindsightClient(apiURL, bankID string) *HindsightClient {
	if bankID == "" {
		bankID = DefaultBankID
	}
	return &HindsightClient{
		baseURL: strings.TrimRight(apiURL, "/") + "/v1/default/banks/" + url.PathEscape(bankID),
		bankID:  bankID,
		httpClient: &http.Client{
			Timeout: defaultHindsightTimeout,
		},
	}
}

func (h *HindsightClient) doJSON(ctx context.Context, method, path string, reqBody, respBody any) error {
	var bodyReader io.Reader
	if reqBody != nil {
		raw, err := json.Marshal(reqBody)
		if err != nil {
			return fmt.Errorf("marshal request: %w", err)
		}
		bodyReader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, h.baseURL+path, bodyReader)
	if err != nil {
		return err
	}
	if reqBody != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := h.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("hindsight %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	rawResp, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read hindsight response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("hindsight %s %s returned %d: %s", method, path, resp.StatusCode, strings.TrimSpace(string(rawResp)))
	}
	if respBody != nil && len(rawResp) > 0 {
		if err := json.Unmarshal(rawResp, respBody); err != nil {
			return fmt.Errorf("unmarshal hindsight response: %w", err)
		}
	}
	return nil
}

// EnsureBank idempotently creates and configures the Hindsight bank with
// retain_extraction_mode: chunks and enable_observations: false so OKF record
// identifiers and YAML guards are stored verbatim without LLM paraphrasing.
func (h *HindsightClient) EnsureBank(ctx context.Context) error {
	if err := h.doJSON(ctx, http.MethodPut, "", map[string]any{
		"mission": bankReflectMission,
	}, nil); err != nil {
		return err
	}
	updates := map[string]any{
		"retain_strategies": map[string]any{
			RecordStrategy: map[string]any{
				"retain_extraction_mode": "chunks",
			},
		},
		"retain_default_strategy": RecordStrategy,
		"enable_observations":     false,
		"reflect_mission":         bankReflectMission,
	}
	return h.doJSON(ctx, http.MethodPatch, "/config", map[string]any{
		"updates": updates,
	}, nil)
}

// RetainRecord upserts one OKF record into Hindsight with document_id="okf:<repo>:<id>"
// and update_mode="replace".
func (h *HindsightClient) RetainRecord(ctx context.Context, repoID string, rec Record) error {
	text := rec.RenderForIndex()
	ctxTitle := fmt.Sprintf("%s: %s", rec.Meta.ID, rec.Meta.Title)
	if len(ctxTitle) > 200 {
		ctxTitle = ctxTitle[:200]
	}
	item := map[string]any{
		"content":     text,
		"document_id": rec.DocumentID(repoID),
		"update_mode": "replace",
		"context":     ctxTitle,
		"tags":        rec.Tags(),
		"metadata": map[string]string{
			"id":           rec.Meta.ID,
			"type":         string(rec.Meta.Type),
			"status":       string(rec.Meta.Status),
			"path":         rec.Path,
			"pull_request": rec.Meta.Evidence.PullRequest,
			"sha256":       rec.ContentSHA256(),
		},
		"strategy": RecordStrategy,
	}
	if rec.Meta.Date != "" && len(rec.Meta.Date) >= 10 {
		item["timestamp"] = rec.Meta.Date[:10] + "T00:00:00Z"
	}
	return h.doJSON(ctx, http.MethodPost, "/memories", map[string]any{
		"items": []any{item},
		"async": false,
	}, nil)
}

// ListDocuments returns all documents tagged scope:shared in the bank.
func (h *HindsightClient) ListDocuments(ctx context.Context) ([]HindsightDocument, error) {
	var all []HindsightDocument
	offset := 0
	for {
		path := fmt.Sprintf("/documents?limit=100&offset=%d&tags=%s&tags_match=any_strict",
			offset, url.QueryEscape(SharedTag))
		var page struct {
			Items []HindsightDocument `json:"items"`
		}
		if err := h.doJSON(ctx, http.MethodGet, path, nil, &page); err != nil {
			return nil, err
		}
		all = append(all, page.Items...)
		if len(page.Items) < 100 {
			break
		}
		offset += 100
	}
	return all, nil
}

// DeleteDocument deletes one document by document_id from the bank.
func (h *HindsightClient) DeleteDocument(ctx context.Context, docID string) error {
	return h.doJSON(ctx, http.MethodDelete, "/documents/"+url.PathEscape(docID), nil, nil)
}

// Recall queries Hindsight for records matching query and tag groups.
func (h *HindsightClient) Recall(ctx context.Context, query string, tags []string, tagsMatch string) ([]HindsightRecallResult, error) {
	if len(tags) == 0 {
		tags = []string{SharedTag}
	}
	if tagsMatch == "" {
		tagsMatch = "all_strict"
	}
	body := map[string]any{
		"query":      query,
		"budget":     "mid",
		"max_tokens": 2048,
		"types":      []string{"world", "experience"},
		"tags":       tags,
		"tags_match": tagsMatch,
	}
	var resp struct {
		Results []HindsightRecallResult `json:"results"`
	}
	if err := h.doJSON(ctx, http.MethodPost, "/memories/recall", body, &resp); err != nil {
		return nil, err
	}
	return resp.Results, nil
}

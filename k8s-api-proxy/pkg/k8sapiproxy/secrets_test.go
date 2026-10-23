package k8sapiproxy

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRewriteSecretRequestHeaders(t *testing.T) {
	factory := NewRequestInfoFactory()

	t.Run("get secret rewrites Accept to PartialObjectMetadata and deletes Accept-Encoding", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/my-secret", nil)
		req.Header.Set("Accept", "application/json")
		req.Header.Set("Accept-Encoding", "gzip")
		info, _ := factory.NewRequestInfo(req)
		callerPartial, acceptRewritten := RewriteSecretRequestHeadersWithStatus(req, info)
		if callerPartial {
			t.Errorf("expected callerPartial=false")
		}
		if !acceptRewritten {
			t.Errorf("expected acceptRewritten=true")
		}
		if got := req.Header.Get("Accept"); got != partialObjectMetadataAccept {
			t.Errorf("Accept = %q, want %q", got, partialObjectMetadataAccept)
		}
		if got := req.Header.Get("Accept-Encoding"); got != "" {
			t.Errorf("Accept-Encoding = %q, want empty (deleted)", got)
		}
	})

	t.Run("list secrets with as=Table preserves Table Accept and reports acceptRewritten=false", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets", nil)
		req.Header.Set("Accept", "application/json;as=Table;v=v1;g=meta.k8s.io,application/json")
		info, _ := factory.NewRequestInfo(req)
		_, acceptRewritten := RewriteSecretRequestHeadersWithStatus(req, info)
		if acceptRewritten {
			t.Errorf("expected acceptRewritten=false when Table Accept is preserved")
		}
		if got := req.Header.Get("Accept"); got != tableAccept {
			t.Errorf("Accept = %q, want %q", got, tableAccept)
		}
	})

	t.Run("create secret rewrites Accept to PartialObjectMetadata", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/secrets?dryRun=All", nil)
		req.Header.Set("Accept", "application/json")
		info, _ := factory.NewRequestInfo(req)
		_, acceptRewritten := RewriteSecretRequestHeadersWithStatus(req, info)
		if !acceptRewritten {
			t.Errorf("expected acceptRewritten=true for create")
		}
		if got := req.Header.Get("Accept"); got != partialObjectMetadataAccept {
			t.Errorf("Accept = %q, want %q", got, partialObjectMetadataAccept)
		}
	})
}

func TestScrubSecretResponse(t *testing.T) {
	factory := NewRequestInfoFactory()

	t.Run("scrubs data, stringData, and last-applied-configuration from Secret", func(t *testing.T) {
		rawSecret := `{
			"apiVersion": "v1",
			"kind": "Secret",
			"metadata": {
				"name": "db-creds",
				"namespace": "default",
				"annotations": {
					"kubectl.kubernetes.io/last-applied-configuration": "{\"data\":{\"password\":\"c2VjcmV0\"}}",
					"app.kubernetes.io/managed-by": "helm"
				}
			},
			"data": {
				"password": "c2VjcmV0"
			},
			"stringData": {
				"token": "plaintext-secret"
			}
		}`
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/db-creds", nil)
		info, _ := factory.NewRequestInfo(req)
		resp := &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(strings.NewReader(rawSecret)),
		}

		ScrubSecretResponse(resp, info, false)
		bodyBytes, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatalf("failed to read scrubbed body: %v", err)
		}
		bodyStr := string(bodyBytes)
		if strings.Contains(bodyStr, "c2VjcmV0") || strings.Contains(bodyStr, "plaintext-secret") || strings.Contains(bodyStr, "last-applied-configuration") {
			t.Fatalf("scrubbed body leaked secret content: %s", bodyStr)
		}

		var parsed map[string]any
		if err := json.Unmarshal(bodyBytes, &parsed); err != nil {
			t.Fatalf("scrubbed output is not valid JSON: %v", err)
		}
		dataMap, ok := parsed["data"].(map[string]any)
		if !ok {
			t.Fatalf("expected data key map to be preserved with REDACTED values, got %v", parsed["data"])
		}
		if dataMap["password"] != "REDACTED" || dataMap["token"] != "REDACTED" {
			t.Errorf("expected data keys to have REDACTED values, got %v", dataMap)
		}
		if _, hasStringData := parsed["stringData"]; hasStringData {
			t.Errorf("expected stringData field to be removed")
		}
		meta := parsed["metadata"].(map[string]any)
		annotations := meta["annotations"].(map[string]any)
		if annotations["app.kubernetes.io/managed-by"] != "helm" {
			t.Errorf("expected non-sensitive annotation to be preserved")
		}
		if annotations[redactedAnnotationKey] != redactedAnnotationValue {
			t.Errorf("expected redaction annotation %q=%q, got %v", redactedAnnotationKey, redactedAnnotationValue, annotations[redactedAnnotationKey])
		}
	})

	t.Run("normalizes PartialObjectMetadata to Secret and populates data keys from managedFields", func(t *testing.T) {
		rawPartial := `{
			"apiVersion": "meta.k8s.io/v1",
			"kind": "PartialObjectMetadata",
			"metadata": {
				"name": "tls-secret",
				"namespace": "default",
				"managedFields": [
					{
						"apiVersion": "v1",
						"fieldsType": "FieldsV1",
						"fieldsV1": {
							"f:data": {
								".": {},
								"f:ca.crt": {},
								"f:tls.crt": {},
								"f:tls.key": {}
							}
						}
					}
				]
			}
		}`
		scrubbed, err := ScrubSecretJSONBytes([]byte(rawPartial), false)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		var parsed map[string]any
		_ = json.Unmarshal(scrubbed, &parsed)
		if parsed["apiVersion"] != "v1" || parsed["kind"] != "Secret" {
			t.Errorf("got apiVersion=%v kind=%v, want v1/Secret", parsed["apiVersion"], parsed["kind"])
		}
		dataMap, ok := parsed["data"].(map[string]any)
		if !ok || len(dataMap) != 3 {
			t.Fatalf("expected 3 data keys from managedFields, got %v", parsed["data"])
		}
		for _, k := range []string{"ca.crt", "tls.crt", "tls.key"} {
			if val, exists := dataMap[k]; !exists || val != "REDACTED" {
				t.Errorf("data[%q] = %q (exists=%v), want REDACTED", k, val, exists)
			}
		}
	})

	t.Run("fails closed with 502 on malformed JSON", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/db-creds", nil)
		info, _ := factory.NewRequestInfo(req)
		resp := &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(bytes.NewBufferString("not-valid-json{")),
		}
		ScrubSecretResponse(resp, info, false)
		if resp.StatusCode != http.StatusBadGateway {
			t.Errorf("StatusCode = %d, want %d", resp.StatusCode, http.StatusBadGateway)
		}
	})

	t.Run("fails closed with 502 when upstream Secret payload exceeds 64 MiB limit", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets", nil)
		info, _ := factory.NewRequestInfo(req)
		oversized := io.MultiReader(
			strings.NewReader(`{"apiVersion":"v1","kind":"SecretList","items":[{"metadata":{"name":"big"},"data":{"blob":"`),
			io.LimitReader(zeroReader{}, maxSecretResponseBytes+10),
			strings.NewReader(`"}}]}`),
		)
		resp := &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(oversized),
		}
		ScrubSecretResponse(resp, info, false)
		if resp.StatusCode != http.StatusBadGateway {
			t.Fatalf("StatusCode = %d, want %d", resp.StatusCode, http.StatusBadGateway)
		}
		bodyBytes, _ := io.ReadAll(resp.Body)
		if !strings.Contains(string(bodyBytes), "exceeds 64 MiB limit") {
			t.Errorf("expected 64 MiB limit error message, got %s", string(bodyBytes))
		}
	})

	t.Run("scrubs streaming watch events", func(t *testing.T) {
		watchStream := `{"type":"ADDED","object":{"apiVersion":"v1","kind":"Secret","metadata":{"name":"s1"},"data":{"k":"dmFs"}}}` + "\n"
		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets?watch=true", nil)
		info, _ := factory.NewRequestInfo(req)
		resp := &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(strings.NewReader(watchStream)),
		}
		ScrubSecretResponse(resp, info, false)
		out, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatalf("unexpected watch read error: %v", err)
		}
		if strings.Contains(string(out), "dmFs") {
			t.Fatalf("watch event leaked secret data: %s", string(out))
		}
	})

	t.Run("decompresses gzip-encoded Secret response before scrubbing", func(t *testing.T) {
		rawSecret := `{"apiVersion":"v1","kind":"Secret","metadata":{"name":"gz-sec"},"data":{"pw":"c2VjcmV0"}}`
		var gzBuf bytes.Buffer
		gz := gzip.NewWriter(&gzBuf)
		_, _ = gz.Write([]byte(rawSecret))
		_ = gz.Close()

		req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/gz-sec", nil)
		info, _ := factory.NewRequestInfo(req)
		resp := &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Encoding": []string{"gzip"}},
			Body:       io.NopCloser(&gzBuf),
		}
		ScrubSecretResponse(resp, info, false)
		if got := resp.Header.Get("Content-Encoding"); got != "" {
			t.Errorf("expected Content-Encoding header to be removed after gunzip, got %q", got)
		}
		out, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatalf("read scrubbed body failed: %v", err)
		}
		if strings.Contains(string(out), "c2VjcmV0") {
			t.Fatalf("gzip secret response leaked secret value: %s", string(out))
		}
	})
}

type zeroReader struct{}

func (zeroReader) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = 'a'
	}
	return len(p), nil
}

package k8sapiproxy

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	lastAppliedConfigAnnotation = "kubectl.kubernetes.io/last-applied-configuration"
	partialObjectMetadataAccept = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
	partialObjectListAccept     = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
	tableAccept                 = "application/json;as=Table;g=meta.k8s.io;v=v1"
	redactedSecretValue         = "REDACTED"
	redactedAnnotationKey       = "kubeagents.x-k8s.io/redacted"
	redactedAnnotationValue     = "true (secret data values are redacted by kube-proxy)"
	maxSecretResponseBytes      = 64 << 20
)

// RewriteSecretRequestHeaders rewrites Accept and Accept-Encoding headers on core/v1 Secret requests
// so the upstream API server returns metadata instead of Secret payload bytes.
// It returns true if the caller originally requested PartialObjectMetadata directly.
func RewriteSecretRequestHeaders(r *http.Request, info *request.RequestInfo) bool {
	callerWantsPartial, _ := RewriteSecretRequestHeadersWithStatus(r, info)
	return callerWantsPartial
}

// RewriteSecretRequestHeadersWithStatus rewrites Accept and Accept-Encoding headers on core/v1 Secret
// requests and returns (callerWantsPartial, acceptRewritten).
func RewriteSecretRequestHeadersWithStatus(r *http.Request, info *request.RequestInfo) (bool, bool) {
	r.Header.Del("Accept-Encoding")

	origAccept := r.Header.Get("Accept")
	callerWantsPartial := strings.Contains(origAccept, "as=PartialObjectMetadata")

	if strings.Contains(origAccept, "as=Table") {
		r.Header.Set("Accept", tableAccept)
		return callerWantsPartial, false
	}

	verb := ""
	if info != nil {
		verb = info.Verb
	}
	switch verb {
	case "list":
		r.Header.Set("Accept", partialObjectListAccept)
	default:
		r.Header.Set("Accept", partialObjectMetadataAccept)
	}
	return callerWantsPartial, true
}

// ScrubSecretResponse redacts Secret payload fields (.data, .stringData, and last-applied-configuration)
// from an upstream HTTP response. If the upstream payload is not valid JSON, it fails closed and
// replaces the response with HTTP 502 Bad Gateway.
func ScrubSecretResponse(resp *http.Response, info *request.RequestInfo, callerWantsPartial bool) {
	if resp == nil || resp.Body == nil {
		return
	}

	bodyReader := io.ReadCloser(resp.Body)
	if strings.EqualFold(strings.TrimSpace(resp.Header.Get("Content-Encoding")), "gzip") {
		gzReader, err := gzip.NewReader(resp.Body)
		if err != nil {
			_ = resp.Body.Close()
			replaceWithBadGateway(resp, "kube-proxy failed to decompress gzip Secret response from upstream")
			return
		}
		resp.Header.Del("Content-Encoding")
		bodyReader = &gzipReadCloser{gz: gzReader, raw: resp.Body}
	}

	if info != nil && info.Verb == "watch" && resp.StatusCode == http.StatusOK {
		resp.Header.Del("Content-Length")
		resp.Body = newSecretWatchScrubber(bodyReader, callerWantsPartial)
		return
	}

	defer bodyReader.Close()
	raw, err := io.ReadAll(io.LimitReader(bodyReader, maxSecretResponseBytes+1))
	if err != nil {
		replaceWithBadGateway(resp, "kube-proxy failed to read Secret response body from upstream")
		return
	}
	if len(raw) > maxSecretResponseBytes {
		replaceWithBadGateway(resp, "kube-proxy refused Secret response: upstream Secret payload exceeds 64 MiB limit")
		return
	}

	scrubbed, err := ScrubSecretJSONBytes(raw, callerWantsPartial)
	if err != nil {
		replaceWithBadGateway(resp, "kube-proxy refused Secret response: upstream payload is not valid JSON")
		return
	}

	resp.Header.Set("Content-Type", "application/json")
	resp.Header.Set("Content-Length", strconv.Itoa(len(scrubbed)))
	resp.ContentLength = int64(len(scrubbed))
	resp.Body = io.NopCloser(bytes.NewReader(scrubbed))
}

type gzipReadCloser struct {
	gz  *gzip.Reader
	raw io.ReadCloser
}

func (g *gzipReadCloser) Read(p []byte) (int, error) {
	return g.gz.Read(p)
}

func (g *gzipReadCloser) Close() error {
	_ = g.gz.Close()
	return g.raw.Close()
}

// ScrubSecretJSONBytes parses a JSON payload and strips .data, .stringData, and last-applied-configuration
// from Secret objects, SecretLists, PartialObjectMetadata, Tables, and WatchEvents.
func ScrubSecretJSONBytes(raw []byte, callerWantsPartial bool) ([]byte, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return nil, errors.New("empty JSON payload")
	}

	var root map[string]any
	if err := json.Unmarshal(trimmed, &root); err != nil {
		return nil, err
	}

	scrubSecretObjectMap(root, callerWantsPartial)

	out, err := json.Marshal(root)
	if err != nil {
		return nil, err
	}
	return out, nil
}

func scrubSecretObjectMap(obj map[string]any, callerWantsPartial bool) {
	if obj == nil {
		return
	}

	dataKeys := collectSecretDataKeys(obj)
	delete(obj, "data")
	delete(obj, "stringData")

	kind, _ := obj["kind"].(string)
	apiVersion, _ := obj["apiVersion"].(string)

	isSecretObject := (apiVersion == "v1" && kind == "Secret")
	if !callerWantsPartial && apiVersion == "meta.k8s.io/v1" {
		switch kind {
		case "PartialObjectMetadata":
			obj["apiVersion"] = "v1"
			obj["kind"] = "Secret"
			isSecretObject = true
		case "PartialObjectMetadataList":
			obj["apiVersion"] = "v1"
			obj["kind"] = "SecretList"
		}
	}

	isSecretItem := isSecretObject || (kind == "" && obj["metadata"] != nil && obj["items"] == nil && obj["rows"] == nil)
	if !callerWantsPartial && isSecretItem && len(dataKeys) > 0 {
		redactedData := make(map[string]any, len(dataKeys))
		for _, k := range dataKeys {
			redactedData[k] = redactedSecretValue
		}
		obj["data"] = redactedData
	}

	if meta, ok := obj["metadata"].(map[string]any); ok {
		annotations, _ := meta["annotations"].(map[string]any)
		if annotations != nil {
			delete(annotations, lastAppliedConfigAnnotation)
		}
		if !callerWantsPartial && isSecretItem {
			if annotations == nil {
				annotations = make(map[string]any, 1)
			}
			annotations[redactedAnnotationKey] = redactedAnnotationValue
			meta["annotations"] = annotations
		} else if annotations != nil && len(annotations) == 0 {
			delete(meta, "annotations")
		}
	}

	if items, ok := obj["items"].([]any); ok {
		for _, item := range items {
			if itemMap, ok := item.(map[string]any); ok {
				if !callerWantsPartial {
					if _, hasKind := itemMap["kind"]; !hasKind {
						itemMap["apiVersion"] = "v1"
						itemMap["kind"] = "Secret"
					}
				}
				scrubSecretObjectMap(itemMap, callerWantsPartial)
			}
		}
	}

	if rows, ok := obj["rows"].([]any); ok {
		for _, row := range rows {
			if rowMap, ok := row.(map[string]any); ok {
				if rowObj, ok := rowMap["object"].(map[string]any); ok {
					scrubSecretObjectMap(rowObj, callerWantsPartial)
				}
			}
		}
	}

	if nestedObj, ok := obj["object"].(map[string]any); ok {
		scrubSecretObjectMap(nestedObj, callerWantsPartial)
	}
}

func collectSecretDataKeys(obj map[string]any) []string {
	seen := make(map[string]struct{})
	var keys []string
	addKey := func(k string) {
		if k == "" {
			return
		}
		if _, ok := seen[k]; !ok {
			seen[k] = struct{}{}
			keys = append(keys, k)
		}
	}

	for _, field := range []string{"data", "stringData"} {
		if m, ok := obj[field].(map[string]any); ok {
			for k := range m {
				addKey(k)
			}
		}
	}

	if meta, ok := obj["metadata"].(map[string]any); ok {
		if managedFields, ok := meta["managedFields"].([]any); ok {
			for _, entry := range managedFields {
				entryMap, ok := entry.(map[string]any)
				if !ok {
					continue
				}
				fieldsV1, ok := entryMap["fieldsV1"].(map[string]any)
				if !ok {
					continue
				}
				for _, fieldKey := range []string{"f:data", "f:stringData"} {
					if fData, ok := fieldsV1[fieldKey].(map[string]any); ok {
						for k := range fData {
							if strings.HasPrefix(k, "f:") {
								addKey(strings.TrimPrefix(k, "f:"))
							}
						}
					}
				}
			}
		}
	}

	return keys
}

func replaceWithBadGateway(resp *http.Response, message string) {
	status := map[string]any{
		"kind":       "Status",
		"apiVersion": "v1",
		"metadata":   map[string]any{},
		"status":     "Failure",
		"message":    message,
		"reason":     "BadGateway",
		"code":       http.StatusBadGateway,
	}
	body, _ := json.Marshal(status)
	resp.StatusCode = http.StatusBadGateway
	resp.Status = "502 Bad Gateway"
	resp.Header.Set("Content-Type", "application/json")
	resp.Header.Set("Content-Length", strconv.Itoa(len(body)))
	resp.ContentLength = int64(len(body))
	resp.Body = io.NopCloser(bytes.NewReader(body))
}

type secretWatchScrubber struct {
	src                io.ReadCloser
	dec                *json.Decoder
	callerWantsPartial bool
	buf                bytes.Buffer
	err                error
}

func newSecretWatchScrubber(src io.ReadCloser, callerWantsPartial bool) io.ReadCloser {
	return &secretWatchScrubber{
		src:                src,
		dec:                json.NewDecoder(src),
		callerWantsPartial: callerWantsPartial,
	}
}

func (s *secretWatchScrubber) Read(p []byte) (int, error) {
	if s.buf.Len() > 0 {
		return s.buf.Read(p)
	}
	if s.err != nil {
		return 0, s.err
	}

	var event map[string]any
	if err := s.dec.Decode(&event); err != nil {
		s.err = err
		return 0, err
	}

	scrubSecretObjectMap(event, s.callerWantsPartial)
	encoded, err := json.Marshal(event)
	if err != nil {
		s.err = err
		return 0, err
	}
	s.buf.Write(encoded)
	s.buf.WriteByte('\n')
	return s.buf.Read(p)
}

func (s *secretWatchScrubber) Close() error {
	return s.src.Close()
}

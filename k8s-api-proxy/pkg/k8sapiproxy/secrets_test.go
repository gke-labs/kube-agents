package k8sapiproxy

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// plaintext is the decoded value every fixture Secret holds; c2VjcmV0 is its base64.
const (
	plaintext = "secret"
	encoded   = "c2VjcmV0"
)

const secretJSON = `{"apiVersion":"v1","kind":"Secret","type":"Opaque",
	"metadata":{"name":"db","namespace":"default","annotations":{
		"kubectl.kubernetes.io/last-applied-configuration":"{\"data\":{\"password\":\"c2VjcmV0\"}}",
		"team":"a"}},
	"data":{"password":"c2VjcmV0"},"stringData":{"user":"secret"}}`

const partialJSON = `{"apiVersion":"meta.k8s.io/v1","kind":"PartialObjectMetadata",
	"metadata":{"name":"db","namespace":"default","annotations":{
		"kubectl.kubernetes.io/last-applied-configuration":"{\"data\":{\"password\":\"c2VjcmV0\"}}",
		"kapp.k14s.io/original":"{\"kind\":\"Secret\",\"stringData\":{\"user\":\"secret\"}}",
		"config":"{\"replicas\":2}"},
		"managedFields":[{"manager":"kubectl","fieldsV1":{"f:data":{".":{},"f:password":{}},"f:type":{}}}]}}`

func assertNoValue(t *testing.T, body []byte) {
	t.Helper()
	if bytes.Contains(body, []byte(encoded)) || bytes.Contains(body, []byte(`"`+plaintext+`"`)) {
		t.Errorf("a Secret value survived: %s", body)
	}
}

func decodeObject(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var obj map[string]any
	if err := json.Unmarshal(body, &obj); err != nil {
		t.Fatalf("not JSON: %v: %s", err, body)
	}
	return obj
}

func TestCallerSecretView(t *testing.T) {
	for accept, want := range map[string]secretView{
		"":                                    viewObject,
		"application/json":                    viewObject,
		"application/vnd.kubernetes.protobuf": viewObject,
		"application/json;as=Table;v=v1;g=meta.k8s.io,application/json;as=Table;v=v1beta1;g=meta.k8s.io,application/json": viewTable,
		"application/json; as=PartialObjectMetadata; g=meta.k8s.io; v=v1":                                                 viewPartial,
		"application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1":                                                viewPartial,
		"application/json;as=Table;g=meta.k8s.io;v=v1beta1":                                                               viewObject,
		"application/json;as=Table;g=example.com;v=v1":                                                                    viewObject,
		"application/json;as=Tables;g=meta.k8s.io;v=v1":                                                                   viewObject,
	} {
		if got := callerSecretView(accept); got != want {
			t.Errorf("callerSecretView(%q) = %d, want %d", accept, got, want)
		}
	}
}

func TestRewriteSecretRequest(t *testing.T) {
	tests := []struct {
		method, target, accept, want string
	}{
		{http.MethodGet, "/api/v1/namespaces/default/secrets/db", "application/json", acceptPartial},
		{http.MethodGet, "/api/v1/namespaces/default/secrets", "application/json", acceptPartialLst},
		{http.MethodGet, "/api/v1/secrets?watch=1", "application/json", acceptPartial},
		{http.MethodDelete, "/api/v1/namespaces/default/secrets", "", acceptPartialLst},
		{http.MethodPost, "/api/v1/namespaces/default/secrets?dryRun=All", "application/json", acceptPartial},
		{http.MethodGet, "/api/v1/namespaces/default/secrets", "application/json;as=Table;v=v1;g=meta.k8s.io", acceptTable},
	}
	factory := NewRequestInfoFactory()
	for _, tc := range tests {
		req := httptest.NewRequest(tc.method, tc.target, nil)
		req.Header.Set(headerAccept, tc.accept)
		req.Header.Set(headerAcceptEncoding, encodingGzip)
		info, err := factory.NewRequestInfo(req)
		if err != nil {
			t.Fatal(err)
		}
		rewriteSecretRequest(req.Header, info)
		if got := req.Header.Get(headerAccept); got != tc.want {
			t.Errorf("%s %s: Accept = %q, want %q", tc.method, tc.target, got, tc.want)
		}
		if req.Header.Get(headerAcceptEncoding) != "" {
			t.Errorf("%s %s: Accept-Encoding kept", tc.method, tc.target)
		}
	}
}

func TestRedactSecretJSON(t *testing.T) {
	t.Run("secret keeps keys and type", func(t *testing.T) {
		out, err := redactSecretJSON([]byte(secretJSON), viewObject)
		if err != nil {
			t.Fatal(err)
		}
		assertNoValue(t, out)
		obj := decodeObject(t, out)
		data, _ := obj[fieldData].(map[string]any)
		if data["password"] != redactedValue || data["user"] != redactedValue || len(data) != 2 {
			t.Errorf("data = %v", obj[fieldData])
		}
		if _, ok := obj[fieldStringData]; ok || obj["type"] != "Opaque" {
			t.Errorf("stringData kept or type lost: %s", out)
		}
		ann := obj[fieldMetadata].(map[string]any)[fieldAnnotations].(map[string]any)
		if _, ok := ann[lastAppliedAnnotation]; ok || ann["team"] != "a" || ann[redactedAnnotation] != redactedAnnotationValue {
			t.Errorf("annotations = %v", ann)
		}
	})

	t.Run("secret list", func(t *testing.T) {
		raw := `{"apiVersion":"v1","kind":"SecretList","items":[` + secretJSON + `,` + secretJSON + `]}`
		out, err := redactSecretJSON([]byte(raw), viewObject)
		if err != nil {
			t.Fatal(err)
		}
		assertNoValue(t, out)
	})

	t.Run("metadata becomes a secret with keys from managedFields", func(t *testing.T) {
		out, err := redactSecretJSON([]byte(partialJSON), viewObject)
		if err != nil {
			t.Fatal(err)
		}
		assertNoValue(t, out)
		obj := decodeObject(t, out)
		if obj[fieldKind] != kindSecret || obj[fieldAPIVersion] != coreVersion {
			t.Errorf("kind = %v %v", obj[fieldAPIVersion], obj[fieldKind])
		}
		if data, _ := obj[fieldData].(map[string]any); len(data) != 1 || data["password"] != redactedValue {
			t.Errorf("data = %v", obj[fieldData])
		}
	})

	t.Run("metadata stays metadata when asked for", func(t *testing.T) {
		out, err := redactSecretJSON([]byte(partialJSON), viewPartial)
		if err != nil {
			t.Fatal(err)
		}
		assertNoValue(t, out)
		obj := decodeObject(t, out)
		if obj[fieldKind] != asPartial || obj[fieldData] != nil {
			t.Errorf("got %s", out)
		}
		if ann := obj[fieldMetadata].(map[string]any)[fieldAnnotations].(map[string]any); len(ann) != 1 || ann["config"] == nil {
			t.Errorf("annotations = %v, want only the one without a Secret copy", ann)
		}
	})

	t.Run("metadata list becomes a secret list", func(t *testing.T) {
		raw := `{"apiVersion":"meta.k8s.io/v1","kind":"PartialObjectMetadataList","items":[` + partialJSON + `]}`
		out, err := redactSecretJSON([]byte(raw), viewObject)
		if err != nil {
			t.Fatal(err)
		}
		assertNoValue(t, out)
		obj := decodeObject(t, out)
		item := obj[fieldItems].([]any)[0].(map[string]any)
		if obj[fieldKind] != kindSecretList || item[fieldKind] != kindSecret {
			t.Errorf("got %s", out)
		}
	})

	t.Run("table row objects", func(t *testing.T) {
		raw := `{"apiVersion":"meta.k8s.io/v1","kind":"Table","columnDefinitions":[],
			"rows":[{"cells":["db","Opaque",1,"1d"],"object":` + secretJSON + `},{"cells":[],"object":` + partialJSON + `},{"cells":[]}]}`
		out, err := redactSecretJSON([]byte(raw), viewTable)
		if err != nil {
			t.Fatal(err)
		}
		assertNoValue(t, out)
	})

	t.Run("status passes", func(t *testing.T) {
		raw := `{"apiVersion":"v1","kind":"Status","status":"Failure","code":404,"message":"not found"}`
		out, err := redactSecretJSON([]byte(raw), viewObject)
		if err != nil || decodeObject(t, out)["message"] != "not found" {
			t.Errorf("got %s, %v", out, err)
		}
	})

	for name, raw := range map[string]string{
		"unknown kind":          `{"apiVersion":"v1","kind":"ConfigMap","data":{"k":"c2VjcmV0"}}`,
		"no kind":               `{"metadata":{},"data":{"k":"c2VjcmV0"}}`,
		"array":                 `[` + secretJSON + `]`,
		"not JSON":              `c2VjcmV0`,
		"items not an array":    `{"apiVersion":"v1","kind":"SecretList","items":{"a":` + secretJSON + `}}`,
		"item not an object":    `{"apiVersion":"v1","kind":"SecretList","items":["c2VjcmV0"]}`,
		"unknown row object":    `{"apiVersion":"meta.k8s.io/v1","kind":"Table","rows":[{"object":{"kind":"X","data":{"k":"c2VjcmV0"}}}]}`,
		"row object not object": `{"apiVersion":"meta.k8s.io/v1","kind":"Table","rows":[{"object":"c2VjcmV0"}]}`,
	} {
		t.Run("refuses "+name, func(t *testing.T) {
			if out, err := redactSecretJSON([]byte(raw), viewObject); err == nil {
				t.Errorf("redacted to %s, want an error", out)
			}
		})
	}
}

func jsonResponse(body []byte, header http.Header) *http.Response {
	if header == nil {
		header = http.Header{}
	}
	if header.Get(headerContentType) == "" {
		header.Set(headerContentType, contentTypeJSON)
	}
	return &http.Response{StatusCode: http.StatusOK, Header: header, Body: io.NopCloser(bytes.NewReader(body))}
}

func gzipped(t *testing.T, s string) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	if _, err := gz.Write([]byte(s)); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestRedactSecretResponse(t *testing.T) {
	get := &secretRedaction{view: viewObject, verb: verbGet}

	t.Run("gzip", func(t *testing.T) {
		resp := jsonResponse(gzipped(t, secretJSON), http.Header{headerContentEncoding: {encodingGzip}})
		redactSecretResponse(resp, get)
		body, _ := io.ReadAll(resp.Body)
		if resp.StatusCode != http.StatusOK || resp.Header.Get(headerContentEncoding) != "" {
			t.Fatalf("code %d, encoding %q: %s", resp.StatusCode, resp.Header.Get(headerContentEncoding), body)
		}
		assertNoValue(t, body)
		if resp.ContentLength != int64(len(body)) {
			t.Errorf("ContentLength = %d, body %d", resp.ContentLength, len(body))
		}
	})

	for name, resp := range map[string]*http.Response{
		"unknown encoding": jsonResponse([]byte(secretJSON), http.Header{headerContentEncoding: {"br"}}),
		"corrupt gzip":     jsonResponse([]byte(secretJSON), http.Header{headerContentEncoding: {encodingGzip}}),
		"protobuf":         jsonResponse([]byte(encoded), http.Header{headerContentType: {"application/vnd.kubernetes.protobuf"}}),
		"unknown kind":     jsonResponse([]byte(`{"apiVersion":"v1","kind":"ConfigMap","data":{"k":"c2VjcmV0"}}`), nil),
	} {
		t.Run("502 for "+name, func(t *testing.T) {
			redactSecretResponse(resp, get)
			body, _ := io.ReadAll(resp.Body)
			if resp.StatusCode != http.StatusBadGateway {
				t.Fatalf("code = %d: %s", resp.StatusCode, body)
			}
			assertNoValue(t, body)
			var s metav1.Status
			if err := json.Unmarshal(body, &s); err != nil || s.Code != http.StatusBadGateway || s.Kind != statusKind {
				t.Errorf("body is not a 502 Status: %s", body)
			}
		})
	}
}

// oneLine compacts a fixture to the single line the API server writes per watch event.
func oneLine(s string) string {
	var buf bytes.Buffer
	if err := json.Compact(&buf, []byte(s)); err != nil {
		panic(err)
	}
	return buf.String()
}

func readWatch(t *testing.T, src string, view secretView) []map[string]any {
	t.Helper()
	resp := jsonResponse([]byte(src), nil)
	redactSecretResponse(resp, &secretRedaction{view: view, verb: verbWatch})
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	assertNoValue(t, body)
	var events []map[string]any
	dec := json.NewDecoder(bytes.NewReader(body))
	for dec.More() {
		var e map[string]any
		if err := dec.Decode(&e); err != nil {
			t.Fatalf("output is not a watch stream: %v: %s", err, body)
		}
		events = append(events, e)
	}
	return events
}

func TestWatchRedactor(t *testing.T) {
	t.Run("sets a plain JSON content type", func(t *testing.T) {
		resp := jsonResponse([]byte(`{"type":"ADDED","object":`+oneLine(partialJSON)+"}\n"), http.Header{headerContentType: {acceptPartial}})
		redactSecretResponse(resp, &secretRedaction{view: viewObject, verb: verbWatch})
		if got := resp.Header.Get(headerContentType); got != contentTypeJSON {
			t.Errorf("Content-Type = %q", got)
		}
	})

	t.Run("ends with an error event on an oversized event", func(t *testing.T) {
		big := `{"type":"ADDED","object":{"apiVersion":"v1","kind":"Secret","metadata":{"name":"` +
			strings.Repeat("a", maxWatchEventBytes) + `"},"data":{"k":"c2VjcmV0"}}}`
		events := readWatch(t, big+"\n", viewObject)
		if len(events) != 1 || events[0][fieldType] != watchEventError {
			t.Fatalf("got %d events", len(events))
		}
	})

	t.Run("redacts each event", func(t *testing.T) {
		src := `{"type":"ADDED","object":` + oneLine(partialJSON) + "}\n" + `{"type":"MODIFIED","object":` + oneLine(secretJSON) + "}\n"
		events := readWatch(t, src, viewObject)
		if len(events) != 2 {
			t.Fatalf("events = %v", events)
		}
		for _, e := range events {
			if e[fieldObject].(map[string]any)[fieldKind] != kindSecret {
				t.Errorf("event = %v", e)
			}
		}
	})

	for name, bad := range map[string]string{
		"unknown kind": `{"type":"ADDED","object":{"apiVersion":"v1","kind":"ConfigMap","data":{"k":"c2VjcmV0"}}}`,
		"no object":    `{"type":"ADDED"}`,
		"not JSON":     `c2VjcmV0`,
	} {
		t.Run("ends with an error event on "+name, func(t *testing.T) {
			src := `{"type":"ADDED","object":` + oneLine(secretJSON) + "}\n" + bad + "\n" + `{"type":"ADDED","object":` + oneLine(secretJSON) + "}\n"
			events := readWatch(t, src, viewObject)
			if len(events) != 2 || events[1][fieldType] != watchEventError {
				t.Fatalf("events = %v", events)
			}
			if status, _ := events[1][fieldObject].(map[string]any); status[fieldKind] != statusKind {
				t.Errorf("error event object = %v", events[1][fieldObject])
			}
		})
	}
}

func TestHandlerRedactsSecrets(t *testing.T) {
	respond := func(*http.Request) *http.Response { return jsonResponse([]byte(secretJSON), nil) }

	upstream := &recordingTransport{response: respond}
	h, _ := newTestHandler(t, Config{Upstream: upstream})
	req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/db", nil)
	req.Header.Set(headerAccept, contentTypeJSON)
	rec := serve(h, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	assertNoValue(t, rec.Body.Bytes())
	if got := upstream.req.Header.Get(headerAccept); got != acceptPartial {
		t.Errorf("upstream Accept = %q, want %q", got, acceptPartial)
	}

	// A transport may answer with a request of its own, without the handler's context.
	upstream = &recordingTransport{response: func(*http.Request) *http.Response {
		resp := jsonResponse([]byte(secretJSON), nil)
		resp.Request = httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/db", nil).WithContext(context.Background())
		return resp
	}}
	h, _ = newTestHandler(t, Config{Upstream: upstream})
	rec = serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/db", nil))
	assertNoValue(t, rec.Body.Bytes())

	upstream = &recordingTransport{response: respond}
	h, _ = newTestHandler(t, Config{Upstream: upstream, Policy: Policy{Mode: ModeReadOnly}})
	req = httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/secrets/db", nil)
	req.Header.Set(headerAccept, contentTypeJSON)
	rec = serve(h, req)
	if !strings.Contains(rec.Body.String(), encoded) || upstream.req.Header.Get(headerAccept) != contentTypeJSON {
		t.Errorf("without RedactSecrets the response or Accept changed: Accept %q, body %s", upstream.req.Header.Get(headerAccept), rec.Body.String())
	}

	upstream = &recordingTransport{}
	h, _ = newTestHandler(t, Config{Upstream: upstream})
	req = httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/configmaps", nil)
	req.Header.Set(headerAccept, contentTypeJSON)
	if rec := serve(h, req); rec.Code != http.StatusOK || upstream.req.Header.Get(headerAccept) != contentTypeJSON {
		t.Errorf("a non-Secret request was rewritten: code %d, Accept %q", rec.Code, upstream.req.Header.Get(headerAccept))
	}
}

// The API server returns the deleted Secret, or the deleted list, even for a dry run.
func TestHandlerRedactsSecretDeletes(t *testing.T) {
	tests := []struct {
		name, target, body, wantAccept string
	}{
		{"delete", "/api/v1/namespaces/default/secrets/db?dryRun=All", secretJSON, acceptPartial},
		{"deletecollection", "/api/v1/namespaces/default/secrets?dryRun=All", `{"apiVersion":"v1","kind":"SecretList","items":[` + secretJSON + `]}`, acceptPartialLst},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			upstream := &recordingTransport{response: func(*http.Request) *http.Response { return jsonResponse([]byte(tc.body), nil) }}
			h, _ := newTestHandler(t, Config{Upstream: upstream, Policy: Policy{Mode: ModeReadWrite, RedactSecrets: true}})
			rec := serve(h, httptest.NewRequest(http.MethodDelete, tc.target, nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
			}
			assertNoValue(t, rec.Body.Bytes())
			if got := upstream.req.Header.Get(headerAccept); got != tc.wantAccept {
				t.Errorf("upstream Accept = %q, want %q", got, tc.wantAccept)
			}
		})
	}
}

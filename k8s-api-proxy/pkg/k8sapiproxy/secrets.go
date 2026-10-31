package k8sapiproxy

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strconv"
	"strings"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	headerAccept          = "Accept"
	headerAcceptEncoding  = "Accept-Encoding"
	headerContentEncoding = "Content-Encoding"
	headerContentLength   = "Content-Length"
	encodingGzip          = "gzip"
	encodingIdentity      = "identity"

	metaGroup        = "meta.k8s.io"
	metaVersion      = "v1"
	metaAPIVersion   = metaGroup + "/" + metaVersion
	mediaParamAs     = "as"
	mediaParamGroup  = "g"
	mediaParamVer    = "v"
	asTable          = "Table"
	asPartial        = "PartialObjectMetadata"
	asPartialList    = "PartialObjectMetadataList"
	acceptTable      = contentTypeJSON + ";as=" + asTable + ";g=" + metaGroup + ";v=" + metaVersion
	acceptPartial    = contentTypeJSON + ";as=" + asPartial + ";g=" + metaGroup + ";v=" + metaVersion
	acceptPartialLst = contentTypeJSON + ";as=" + asPartialList + ";g=" + metaGroup + ";v=" + metaVersion

	kindSecret     = "Secret"
	kindSecretList = "SecretList"

	fieldAPIVersion    = "apiVersion"
	fieldKind          = "kind"
	fieldMetadata      = "metadata"
	fieldAnnotations   = "annotations"
	fieldManagedFields = "managedFields"
	fieldFieldsV1      = "fieldsV1"
	fieldData          = "data"
	fieldStringData    = "stringData"
	fieldItems         = "items"
	fieldRows          = "rows"
	fieldObject        = "object"
	fieldType          = "type"
	managedFieldPrefix = "f:"
	watchEventError    = "ERROR"

	lastAppliedAnnotation = "kubectl.kubernetes.io/last-applied-configuration"
	// redactedValue is base64 of "REDACTED", so a decoded value reads as the word.
	redactedValue           = "UkVEQUNURUQ="
	redactedAnnotation      = "kubeagents.x-k8s.io/redacted"
	redactedAnnotationValue = "true (Secret values are redacted by kube-proxy)"

	// maxSecretResponseBytes bounds a buffered Secret response, and maxWatchEventBytes one
	// watch event. A decoded body costs several times its size in memory; kubectl pages
	// lists, so one page of metadata stays well under the bound.
	maxSecretResponseBytes = 16 << 20
	maxWatchEventBytes     = 4 << 20

	redactionFailed = "kube-proxy refused the Secret response: %s"
)

// secretView is the representation the caller asked for.
type secretView int

const (
	// viewObject is a Secret or SecretList, rebuilt from metadata with every value redacted.
	viewObject secretView = iota
	// viewPartial is PartialObjectMetadata or PartialObjectMetadataList, returned as sent.
	viewPartial
	// viewTable is a meta.k8s.io/v1 Table, as kubectl get asks for.
	viewTable
)

type secretRedaction struct {
	view secretView
	verb string
}

type secretRedactionKey struct{}

// callerSecretView reads the caller's Accept header. A Table or PartialObjectMetadata
// entry in the meta.k8s.io/v1 group selects that view; anything else, protobuf included, is
// answered as a JSON Secret.
func callerSecretView(accept string) secretView {
	for _, entry := range strings.Split(accept, ",") {
		_, params, err := mime.ParseMediaType(strings.TrimSpace(entry))
		if err != nil || params[mediaParamGroup] != metaGroup || params[mediaParamVer] != metaVersion {
			continue
		}
		switch params[mediaParamAs] {
		case asTable:
			return viewTable
		case asPartial, asPartialList:
			return viewPartial
		}
	}
	return viewObject
}

// rewriteSecretRequest asks the API server for a representation without Secret values:
// a Table, or PartialObjectMetadata, which carries no data. Response redaction is the second
// layer, for what the first does not cover, such as a Table row's embedded object.
func rewriteSecretRequest(h http.Header, info *request.RequestInfo) *secretRedaction {
	plan := &secretRedaction{view: callerSecretView(strings.Join(h.Values(headerAccept), ",")), verb: info.Verb}
	h.Del(headerAcceptEncoding)
	switch {
	case plan.view == viewTable:
		h.Set(headerAccept, acceptTable)
	case info.Verb == verbList || info.Verb == verbDeleteCollection:
		h.Set(headerAccept, acceptPartialLst)
	default:
		h.Set(headerAccept, acceptPartial)
	}
	return plan
}

// redactSecretResponse redacts resp in place. Anything it cannot redact, it replaces with a
// 502 Status, or on a watch ends the stream with an ERROR event: a body that is not JSON, an encoding other than gzip, an object of an
// unexpected kind, or a body over maxSecretResponseBytes.
func redactSecretResponse(resp *http.Response, plan *secretRedaction) {
	body, err := decodedBody(resp)
	if err != nil {
		replaceWithStatus(resp, err)
		return
	}
	if mt, _, err := mime.ParseMediaType(resp.Header.Get(headerContentType)); err != nil || mt != contentTypeJSON {
		_ = body.Close()
		replaceWithStatus(resp, fmt.Errorf("content type %q is not JSON", resp.Header.Get(headerContentType)))
		return
	}

	if plan.verb == verbWatch && resp.StatusCode == http.StatusOK {
		resp.Header.Set(headerContentType, contentTypeJSON)
		resp.Header.Del(headerContentLength)
		resp.ContentLength = -1
		resp.Body = newWatchRedactor(body, plan.view)
		return
	}

	defer body.Close()
	raw, err := io.ReadAll(io.LimitReader(body, maxSecretResponseBytes+1))
	if err != nil {
		replaceWithStatus(resp, fmt.Errorf("reading the body: %w", err))
		return
	}
	if len(raw) > maxSecretResponseBytes {
		replaceWithStatus(resp, fmt.Errorf("the body exceeds %d bytes", maxSecretResponseBytes))
		return
	}
	out, err := redactSecretJSON(raw, plan.view)
	if err != nil {
		replaceWithStatus(resp, err)
		return
	}
	setBody(resp, out)
}

// decodedBody returns resp's body without its content encoding.
func decodedBody(resp *http.Response) (io.ReadCloser, error) {
	switch enc := strings.ToLower(strings.TrimSpace(resp.Header.Get(headerContentEncoding))); enc {
	case "", encodingIdentity:
		return resp.Body, nil
	case encodingGzip:
		gz, err := gzip.NewReader(resp.Body)
		if err != nil {
			_ = resp.Body.Close()
			return nil, fmt.Errorf("decompressing the body: %w", err)
		}
		resp.Header.Del(headerContentEncoding)
		return &gzipReadCloser{gz: gz, raw: resp.Body}, nil
	default:
		_ = resp.Body.Close()
		return nil, fmt.Errorf("content encoding %q is not supported", enc)
	}
}

type gzipReadCloser struct {
	gz  *gzip.Reader
	raw io.ReadCloser
}

func (g *gzipReadCloser) Read(p []byte) (int, error) { return g.gz.Read(p) }

func (g *gzipReadCloser) Close() error {
	_ = g.gz.Close()
	return g.raw.Close()
}

func setBody(resp *http.Response, body []byte) {
	resp.Header.Set(headerContentType, contentTypeJSON)
	resp.Header.Set(headerContentLength, strconv.Itoa(len(body)))
	resp.ContentLength = int64(len(body))
	resp.Body = io.NopCloser(bytes.NewReader(body))
}

func replaceWithStatus(resp *http.Response, cause error) {
	code, body := statusBody(http.StatusBadGateway, metav1.StatusReasonInternalError, fmt.Sprintf(redactionFailed, cause))
	resp.StatusCode = code
	resp.Status = strconv.Itoa(code) + " " + http.StatusText(code)
	resp.Header.Del(headerContentEncoding)
	setBody(resp, body)
}

// redactSecretJSON redacts one JSON object of a Secret response.
func redactSecretJSON(raw []byte, view secretView) ([]byte, error) {
	obj, err := parseJSONObject(raw)
	if err != nil {
		return nil, errors.New("the body is not one JSON object")
	}
	if err := redactObject(obj, view); err != nil {
		return nil, err
	}
	return json.Marshal(obj)
}

// parseJSONObject decodes exactly one JSON object, keeping numbers as written.
func parseJSONObject(raw []byte) (map[string]any, error) {
	var obj map[string]any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&obj); err != nil {
		return nil, err
	}
	if dec.More() || obj == nil {
		return nil, errors.New("not one JSON object")
	}
	return obj, nil
}

// redactObject redacts obj by its kind. A kind the API server does not return for a Secret
// request is an error, so a shape the redaction does not know is refused rather than passed.
func redactObject(obj map[string]any, view secretView) error {
	apiVersion, _ := obj[fieldAPIVersion].(string)
	kind, _ := obj[fieldKind].(string)
	switch {
	case apiVersion == statusAPIVersion && kind == statusKind:
		return nil
	case apiVersion == coreVersion && kind == kindSecret:
		redactSecret(obj)
		return nil
	case apiVersion == coreVersion && kind == kindSecretList:
		return eachItem(obj, func(item map[string]any) error {
			redactSecret(item)
			return nil
		})
	case apiVersion == metaAPIVersion && kind == asPartial:
		stripLastApplied(obj)
		if view == viewObject {
			partialToSecret(obj)
		}
		return nil
	case apiVersion == metaAPIVersion && kind == asPartialList:
		if view == viewObject {
			obj[fieldAPIVersion], obj[fieldKind] = coreVersion, kindSecretList
		}
		return eachItem(obj, func(item map[string]any) error {
			stripLastApplied(item)
			if view == viewObject {
				partialToSecret(item)
			}
			return nil
		})
	case apiVersion == metaAPIVersion && kind == asTable:
		return redactTableRows(obj)
	default:
		return fmt.Errorf("unexpected kind %q in %q", kind, apiVersion)
	}
}

func eachItem(list map[string]any, fn func(map[string]any) error) error {
	raw, ok := list[fieldItems]
	if !ok || raw == nil {
		return nil
	}
	items, ok := raw.([]any)
	if !ok {
		return errors.New("list items are not an array")
	}
	for _, it := range items {
		item, ok := it.(map[string]any)
		if !ok {
			return errors.New("a list item is not an object")
		}
		if err := fn(item); err != nil {
			return err
		}
	}
	return nil
}

// redactTableRows redacts each row's embedded object, which is a full Secret when the
// caller asked for includeObject=Object.
func redactTableRows(table map[string]any) error {
	raw, ok := table[fieldRows]
	if !ok || raw == nil {
		return nil
	}
	rows, ok := raw.([]any)
	if !ok {
		return errors.New("table rows are not an array")
	}
	for _, r := range rows {
		row, ok := r.(map[string]any)
		if !ok {
			return errors.New("a table row is not an object")
		}
		embedded, ok := row[fieldObject]
		if !ok || embedded == nil {
			continue
		}
		obj, ok := embedded.(map[string]any)
		if !ok {
			return errors.New("a table row's object is not an object")
		}
		if err := redactObject(obj, viewPartial); err != nil {
			return err
		}
	}
	return nil
}

// redactSecret replaces every value of a Secret with redactedValue, keeping its keys and type.
func redactSecret(obj map[string]any) {
	keys := secretDataKeys(obj)
	delete(obj, fieldData)
	delete(obj, fieldStringData)
	setRedactedData(obj, keys)
}

// partialToSecret turns PartialObjectMetadata into the Secret the caller asked for. The API
// server does not send the type or values; the keys come from managedFields.
func partialToSecret(obj map[string]any) {
	obj[fieldAPIVersion], obj[fieldKind] = coreVersion, kindSecret
	setRedactedData(obj, secretDataKeys(obj))
}

func setRedactedData(obj map[string]any, keys []string) {
	stripLastApplied(obj)
	if len(keys) > 0 {
		data := make(map[string]any, len(keys))
		for _, k := range keys {
			data[k] = redactedValue
		}
		obj[fieldData] = data
	}
	meta, _ := obj[fieldMetadata].(map[string]any)
	if meta == nil {
		meta = map[string]any{}
		obj[fieldMetadata] = meta
	}
	annotations, _ := meta[fieldAnnotations].(map[string]any)
	if annotations == nil {
		annotations = map[string]any{}
		meta[fieldAnnotations] = annotations
	}
	annotations[redactedAnnotation] = redactedAnnotationValue
}

// stripLastApplied removes the annotations that hold a copy of the Secret: kubectl's
// last-applied-configuration, and any other whose value is a JSON object with data or
// stringData, as other deploy tools record the object they applied.
func stripLastApplied(obj map[string]any) {
	meta, _ := obj[fieldMetadata].(map[string]any)
	if meta == nil {
		return
	}
	annotations, _ := meta[fieldAnnotations].(map[string]any)
	if annotations == nil {
		return
	}
	for key, value := range annotations {
		if key == lastAppliedAnnotation || holdsSecretCopy(value) {
			delete(annotations, key)
		}
	}
	if len(annotations) == 0 {
		delete(meta, fieldAnnotations)
	}
}

func holdsSecretCopy(value any) bool {
	s, ok := value.(string)
	if !ok {
		return false
	}
	var copied map[string]any
	if json.Unmarshal([]byte(s), &copied) != nil {
		return false
	}
	_, hasData := copied[fieldData]
	_, hasStringData := copied[fieldStringData]
	return hasData || hasStringData
}

// secretDataKeys returns the keys of data and stringData, and the data keys managedFields
// records, which is where PartialObjectMetadata keeps them.
func secretDataKeys(obj map[string]any) []string {
	seen := map[string]bool{}
	var keys []string
	add := func(k string) {
		if k != "" && !seen[k] {
			seen[k] = true
			keys = append(keys, k)
		}
	}
	for _, field := range []string{fieldData, fieldStringData} {
		if m, ok := obj[field].(map[string]any); ok {
			for k := range m {
				add(k)
			}
		}
	}
	meta, _ := obj[fieldMetadata].(map[string]any)
	entries, _ := meta[fieldManagedFields].([]any)
	for _, e := range entries {
		entry, _ := e.(map[string]any)
		fields, _ := entry[fieldFieldsV1].(map[string]any)
		for _, field := range []string{managedFieldPrefix + fieldData, managedFieldPrefix + fieldStringData} {
			if m, ok := fields[field].(map[string]any); ok {
				for k := range m {
					if name, ok := strings.CutPrefix(k, managedFieldPrefix); ok {
						add(name)
					}
				}
			}
		}
	}
	return keys
}

// watchRedactor redacts a JSON watch stream one event at a time. The API server writes each
// event as one line of JSON. An event it cannot redact, or one longer than
// maxWatchEventBytes, ends the stream with an ERROR event carrying a Status, as the API server
// ends a failed watch.
type watchRedactor struct {
	src     io.ReadCloser
	scanner *bufio.Scanner
	view    secretView
	buf     bytes.Buffer
	err     error
}

func newWatchRedactor(src io.ReadCloser, view secretView) io.ReadCloser {
	scanner := bufio.NewScanner(src)
	scanner.Buffer(nil, maxWatchEventBytes)
	return &watchRedactor{src: src, scanner: scanner, view: view}
}

func (w *watchRedactor) Read(p []byte) (int, error) {
	for w.buf.Len() == 0 {
		if w.err != nil {
			return 0, w.err
		}
		w.next()
	}
	return w.buf.Read(p)
}

func (w *watchRedactor) next() {
	if !w.scanner.Scan() {
		switch err := w.scanner.Err(); {
		case err == nil:
			w.err = io.EOF
		case errors.Is(err, bufio.ErrTooLong):
			w.fail(fmt.Errorf("a watch event exceeds %d bytes", maxWatchEventBytes))
		default:
			// The upstream stream broke; the caller's watch ends as it would at the API server.
			w.err = err
		}
		return
	}
	line := bytes.TrimSpace(w.scanner.Bytes())
	if len(line) == 0 {
		return
	}
	event, err := parseJSONObject(line)
	if err != nil {
		w.fail(errors.New("a watch event is not one JSON object"))
		return
	}
	obj, ok := event[fieldObject].(map[string]any)
	if _, typed := event[fieldType].(string); !ok || !typed {
		w.fail(errors.New("a watch event has no type or object"))
		return
	}
	if err := redactObject(obj, w.view); err != nil {
		w.fail(err)
		return
	}
	w.write(event)
}

func (w *watchRedactor) fail(cause error) {
	_, status := statusBody(http.StatusBadGateway, metav1.StatusReasonInternalError, fmt.Sprintf(redactionFailed, cause))
	w.write(map[string]any{fieldType: watchEventError, fieldObject: json.RawMessage(status)})
	w.err = io.EOF
}

func (w *watchRedactor) write(event map[string]any) {
	// An event decoded from JSON marshals back without error.
	encoded, _ := json.Marshal(event)
	w.buf.Write(encoded)
	w.buf.WriteByte('\n')
}

func (w *watchRedactor) Close() error { return w.src.Close() }

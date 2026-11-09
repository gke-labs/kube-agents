package hermesbridge

import (
	"encoding/base64"
	"encoding/json"
	"regexp"
	"sort"
	"strings"
	"sync"
	"unicode/utf8"
)

// What of an answer reaches TASKS. The answer stays on the stream (the
// result artifact, each turn artifact, and the card reports a turn folds
// into its answer), because the gateway delivers from it, so the copy a
// person is shown and the copy the stream keeps are one message, scrubbed
// once before it is published (scrubAnswer). Two passes:
//
//   - the Kubernetes Secret values this task's tool results carried
//     (seenSecrets), exact-matched in their plain and base64 forms, for a
//     secret with no shape: a value decoded from a Secret's data and
//     repeated, in the answer or in a card's report;
//   - the shape rules (scrubInlineSecrets), for a secret written in a form
//     that says what it is.
//
// The set takes only the data and stringData values of objects whose kind
// is Secret, eight bytes or more, under any key but the connection
// settings seenSecretSkippedKeys names. It never takes what a shape rule
// catches in a result's text: a Secret's name, a byte count, an image
// digest or a phrase would then be redacted everywhere in the answer.
//
// The bridge sees a tool's result only through the activity door: hermes's
// post_tool_call delivery carries the tool's whole result (extra.result).
// Under the cli executor that is the bridge's own child; under the api
// executor the pod-wide hook, so the coordinator session and the worker
// sessions of the cards the task filed or tracks, whether or not the turn
// is waiting on them yet (cardWorkerRun). Kubernetes Secret data reaches a
// result when a tool prints the object: kubectl's -o json or -o yaml in
// the shell sandbox (a real kubectl there, talking to the k8s API proxy),
// or a file or MCP result holding the same manifest. A value printed bare
// (a jsonpath read piped through base64 -d) has no structure to read, so
// only the shape rules see it.
const (
	// seenSecretMinBytes is the shortest value tracked: below it an exact
	// match redacts ordinary words.
	seenSecretMinBytes = 8
	// seenSecretMaxValueBytes is the longest value tracked; a longer one
	// (a bundle, a kubeconfig) is left to the shape rules.
	seenSecretMaxValueBytes = 16 << 10
	// seenSecretMaxEntries and seenSecretMaxBytes cap the set: past either,
	// the oldest entry is evicted first, so a task that reads many Secrets
	// keeps the most recent ones.
	seenSecretMaxEntries = 512
	seenSecretMaxBytes   = 1 << 20
	// toolResultScanCap bounds how much of one result's text the text
	// readers (YAML, JSON cut short) scan for a Secret; JSON that parses
	// whole is not bounded by it.
	toolResultScanCap = 1 << 20
	// toolResultNestDepth bounds how deep JSON text inside a result's
	// strings is parsed (a terminal result is JSON whose output is
	// kubectl's JSON).
	toolResultNestDepth = 4
)

// seenSecretSkippedKeys are the Secret data keys whose values are a
// connection's settings, not credentials: a user or database name, a host,
// a port. An exact match on one would redact an ordinary word everywhere
// in the answer, so they are not tracked, like a service account token
// Secret's namespace. Matched on the whole key, in any case.
var seenSecretSkippedKeys = map[string]bool{
	"username": true, "user": true, "host": true, "port": true, "database": true,
}

// seenSecretKeySkipped says whether a Secret's value under key is not
// tracked.
func seenSecretKeySkipped(key string, saToken bool) bool {
	k := strings.ToLower(strings.TrimSpace(key))
	return seenSecretSkippedKeys[k] || (saToken && k == "namespace")
}

// seenSecrets is one task's set of Secret values seen in its tool results.
// It lives on the task's activityState and is cleared, and closed to
// further values, when the task finalizes. No value in it is logged or
// published: it is read only to redact (redact).
type seenSecrets struct {
	mu     sync.Mutex
	set    map[string]struct{}
	order  []string // insertion order, the oldest first, for eviction
	bytes  int
	closed bool // the task has finalized: a late delivery adds nothing
}

// add tracks a plain value (a stringData value) and its base64 form.
func (s *seenSecrets) add(v string) {
	v = strings.TrimSpace(v)
	if !seenSecretSized(v) {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.addLocked(v)
	s.addLocked(base64.StdEncoding.EncodeToString([]byte(v)))
}

// addEncoded tracks a Secret data value: the base64 form as given, and the
// decoded value when it decodes to text. A value that decodes to fewer
// than seenSecretMinBytes is not tracked in either form.
func (s *seenSecrets) addEncoded(b64 string) {
	b64 = strings.TrimSpace(b64)
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err == nil && len(strings.TrimSpace(string(raw))) < seenSecretMinBytes {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.addLocked(b64)
	if err != nil || !utf8.Valid(raw) {
		return
	}
	s.addLocked(strings.TrimSpace(string(raw)))
}

func seenSecretSized(v string) bool {
	return len(v) >= seenSecretMinBytes && len(v) <= seenSecretMaxValueBytes
}

func (s *seenSecrets) addLocked(v string) {
	if s.closed || !seenSecretSized(v) || strings.Contains(v, redactedValue) {
		return
	}
	if s.set == nil {
		s.set = make(map[string]struct{})
	}
	if _, ok := s.set[v]; ok {
		return
	}
	s.set[v] = struct{}{}
	s.order = append(s.order, v)
	s.bytes += len(v)
	for len(s.order) > seenSecretMaxEntries || s.bytes > seenSecretMaxBytes {
		old := s.order[0]
		s.order = s.order[1:]
		delete(s.set, old)
		s.bytes -= len(old)
	}
}

// size is how many values the set holds.
func (s *seenSecrets) size() int {
	if s == nil {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.order)
}

// has says whether v is tracked; for tests.
func (s *seenSecrets) has(v string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.set[v]
	return ok
}

// clear forgets every value and takes no more: the task has finalized, so
// a delivery still on its way past the door adds nothing.
func (s *seenSecrets) clear() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.set, s.order, s.bytes, s.closed = nil, nil, 0, true
}

// redact replaces every tracked value in text with the marker, the longest
// first where two overlap.
func (s *seenSecrets) redact(text string) string {
	if s == nil || text == "" {
		return text
	}
	s.mu.Lock()
	vals := append([]string(nil), s.order...)
	s.mu.Unlock()
	if len(vals) == 0 {
		return text
	}
	sort.SliceStable(vals, func(i, j int) bool { return len(vals[i]) > len(vals[j]) })
	pairs := make([]string, 0, 2*len(vals))
	for _, v := range vals {
		pairs = append(pairs, v, redactedValue)
	}
	return strings.NewReplacer(pairs...).Replace(text)
}

// scrubAnswerText is what of an answer goes on TASKS: the task's seen
// values exact-matched out, then the shape rules. seen may be nil (no
// door, or nothing seen).
func scrubAnswerText(text string, seen *seenSecrets) string {
	return scrubInlineSecrets(seen.redact(text))
}

// scrubAnswer is scrubAnswerText with the run's own seen values.
func scrubAnswer(run *taskRun, text string) string {
	var seen *seenSecrets
	if a := run.act.Load(); a != nil {
		seen = &a.secrets
	}
	return scrubAnswerText(text, seen)
}

var (
	// yamlSecretKindPattern finds a kind: Secret line at a scope's own top
	// level (column 0 once a list item is dedented), a trailing comment
	// allowed; yamlAnySecretKindPattern finds one at any depth, to decide
	// whether a text is worth reading for scopes at all.
	yamlSecretKindPattern    = regexp.MustCompile(`(?m)^kind:[ \t]*["']?Secret["']?[ \t]*(?:#.*)?\r?$`)
	yamlAnySecretKindPattern = regexp.MustCompile(`(?m)^[ \t]*(?:-[ \t]+)*kind:[ \t]*["']?Secret["']?[ \t]*(?:#.*)?\r?$`)
	yamlDocSeparatorPattern  = regexp.MustCompile(`(?m)^---[ \t]*(?:#.*)?\r?$`)
	yamlListItemPattern      = regexp.MustCompile(`^([ \t]*)-[ \t]+\S`)
	yamlTopBlockPattern      = regexp.MustCompile(`^(data|stringData)[ \t]*:[ \t]*(?:#.*)?$`)
	yamlDataEntryPattern     = regexp.MustCompile(`^[ \t]+["']?([-A-Za-z0-9_./]+)["']?[ \t]*:[ \t]*(.*?)[ \t]*$`)
	// jsonSecretBlockOpenPattern and jsonStringPairPattern read a Secret's
	// data out of JSON text that does not parse whole (a truncated result),
	// a block cut short included, up to its last complete pair.
	jsonSecretBlockOpenPattern = regexp.MustCompile(`"(data|stringData)"\s*:\s*\{`)
	jsonStringPairPattern      = regexp.MustCompile(`"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"`)
	jsonKindPattern            = regexp.MustCompile(`"kind"\s*:\s*"([^"\\]*)"`)
	jsonSATokenTypePattern     = regexp.MustCompile(`"type"\s*:\s*"kubernetes\.io/service-account-token"`)
)

// collectToolResult adds the Secret values one tool result carries: every
// value of a Kubernetes Secret's data (base64, decoded too) and stringData,
// whether the result holds the object as JSON (at any depth, in JSON text
// inside a string included, or cut short) or as YAML (a document, or an
// item of a list). Nothing else in the result is tracked.
func (s *seenSecrets) collectToolResult(raw json.RawMessage) {
	if s == nil || len(raw) == 0 {
		return
	}
	c := toolResultCollector{set: s}
	var v any
	if json.Unmarshal(raw, &v) != nil {
		c.text(string(raw), 0)
		return
	}
	c.value(v, 0)
}

type toolResultCollector struct {
	set *seenSecrets
}

func (c *toolResultCollector) value(v any, depth int) {
	switch t := v.(type) {
	case string:
		c.text(t, depth)
	case map[string]any:
		if kind, _ := t["kind"].(string); kind == "Secret" {
			c.secretObject(t)
		}
		for _, x := range t {
			c.value(x, depth)
		}
	case []any:
		for _, x := range t {
			c.value(x, depth)
		}
	}
}

// secretObject adds a decoded Secret object's values, but for the keys
// seenSecretKeySkipped names.
func (c *toolResultCollector) secretObject(m map[string]any) {
	saToken := m["type"] == "kubernetes.io/service-account-token"
	if data, ok := m["data"].(map[string]any); ok {
		for k, v := range data {
			if sv, ok := v.(string); ok && !seenSecretKeySkipped(k, saToken) {
				c.set.addEncoded(sv)
			}
		}
	}
	if sd, ok := m["stringData"].(map[string]any); ok {
		for k, v := range sd {
			if sv, ok := v.(string); ok && !seenSecretKeySkipped(k, saToken) {
				c.set.add(sv)
			}
		}
	}
}

func (c *toolResultCollector) text(t string, depth int) {
	if t == "" || !strings.Contains(t, "Secret") {
		return
	}
	t = capBytes(t, toolResultScanCap)
	if trimmed := strings.TrimSpace(t); depth < toolResultNestDepth && (strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[")) {
		var v any
		if json.Unmarshal([]byte(trimmed), &v) == nil {
			c.value(v, depth+1)
			return
		}
		c.truncatedJSONSecrets(t)
	}
	if yamlAnySecretKindPattern.MatchString(t) {
		c.yamlSecrets(t)
	}
}

// truncatedJSONSecrets reads the data and stringData blocks out of JSON
// text that does not parse whole (cut short), each up to its closing brace
// or its last complete pair. A block is read only when the text names kind
// Secret and no kind but Secret and List: in kubectl's output a block
// comes before its own object's kind, so a text holding other kinds could
// hand it a ConfigMap's data. A single Secret cut inside its data, before
// its kind is printed, names no kind and is not read.
func (c *toolResultCollector) truncatedJSONSecrets(t string) {
	sawSecret := false
	for _, k := range jsonKindPattern.FindAllStringSubmatch(t, -1) {
		switch k[1] {
		case "Secret":
			sawSecret = true
		case "List", "SecretList":
		default:
			return
		}
	}
	if !sawSecret {
		return
	}
	saToken := jsonSATokenTypePattern.MatchString(t)
	for _, m := range jsonSecretBlockOpenPattern.FindAllStringSubmatchIndex(t, -1) {
		block := t[m[1]:]
		if end := strings.IndexByte(block, '}'); end >= 0 {
			block = block[:end]
		}
		stringData := t[m[2]:m[3]] == "stringData"
		for _, kv := range jsonStringPairPattern.FindAllStringSubmatch(block, -1) {
			var k, v string
			if json.Unmarshal([]byte(`"`+kv[1]+`"`), &k) != nil || json.Unmarshal([]byte(`"`+kv[2]+`"`), &v) != nil {
				continue
			}
			if seenSecretKeySkipped(k, saToken) {
				continue
			}
			if stringData {
				c.set.add(v)
			} else {
				c.set.addEncoded(v)
			}
		}
	}
}

// yamlSecrets reads the data and stringData blocks of each YAML scope in t
// whose own kind is Secret: a document, and each list item at any depth
// (kubectl's -o yaml of several Secrets is a List whose items are
// indented), each item read only for its own lines.
func (c *toolResultCollector) yamlSecrets(t string) {
	for _, doc := range yamlDocSeparatorPattern.Split(t, -1) {
		if !yamlAnySecretKindPattern.MatchString(doc) {
			continue
		}
		lines := strings.Split(doc, "\n")
		for i := range lines {
			lines[i] = strings.TrimRight(lines[i], "\r")
		}
		c.yamlScope(lines)
		for i, line := range lines {
			m := yamlListItemPattern.FindStringSubmatchIndex(line)
			if m == nil {
				continue
			}
			col := m[1] - 1 // the item's own keys start here
			item := []string{line[col:]}
			for _, next := range lines[i+1:] {
				if strings.TrimSpace(next) == "" {
					item = append(item, "")
					continue
				}
				if yamlIndent(next) < col {
					break
				}
				item = append(item, next[col:])
			}
			c.yamlScope(item)
		}
	}
}

// yamlScope reads one scope's top-level data and stringData blocks when
// its own top-level kind is Secret. lines are dedented: the scope's keys
// start at column 0.
func (c *toolResultCollector) yamlScope(lines []string) {
	secret, saToken := false, false
	for _, l := range lines {
		if yamlSecretKindPattern.MatchString(l) {
			secret = true
		}
		if strings.HasPrefix(l, "type:") && strings.Contains(l, "kubernetes.io/service-account-token") {
			saToken = true
		}
	}
	if !secret {
		return
	}
	block := ""
	for _, l := range lines {
		if strings.TrimSpace(l) == "" || strings.HasPrefix(strings.TrimSpace(l), "#") {
			continue
		}
		if yamlIndent(l) == 0 {
			block = ""
			if m := yamlTopBlockPattern.FindStringSubmatch(l); m != nil {
				block = m[1]
			}
			continue
		}
		if block == "" {
			continue
		}
		em := yamlDataEntryPattern.FindStringSubmatch(l)
		if em == nil || seenSecretKeySkipped(em[1], saToken) {
			continue
		}
		v := yamlScalar(em[2])
		if block == "data" {
			c.set.addEncoded(v)
		} else {
			c.set.add(v)
		}
	}
}

// yamlScalar is an entry's value: a quoted one's contents, else the value
// with a trailing comment taken off.
func yamlScalar(v string) string {
	if len(v) >= 2 && (v[0] == '"' || v[0] == '\'') {
		if end := strings.IndexByte(v[1:], v[0]); end >= 0 {
			return v[1 : 1+end]
		}
	}
	if i := strings.Index(v, " #"); i >= 0 {
		v = v[:i]
	}
	return strings.TrimSpace(v)
}

func yamlIndent(l string) int {
	return len(l) - len(strings.TrimLeft(l, " \t"))
}

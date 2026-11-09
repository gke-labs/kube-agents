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
//   - the values this task's tool results carried that the bridge knows are
//     secrets (seenSecrets), exact-matched in their plain and base64 forms,
//     for a secret with no shape: a value decoded from a Secret's data and
//     repeated, or a token echoed verbatim in a card's report;
//   - the shape rules (scrubInlineSecrets), for a secret written in a form
//     that says what it is.
//
// The set takes every Secret data and stringData value of eight bytes or
// more, and every value a shape rule catches in a result's text that is
// not one plain word (shapeCaught).
//
// The bridge sees a tool's result only through the activity door: hermes's
// post_tool_call delivery carries the tool's whole result (extra.result).
// Under the cli executor that is the bridge's own child; under the api
// executor the pod-wide hook, so the coordinator session and the worker
// sessions of the cards the task tracks. Kubernetes Secret data reaches a
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
	// toolResultScanCap bounds how much of one result's text the shape
	// rules read for values; the structured read (a Secret object) is not
	// bounded by it.
	toolResultScanCap = 1 << 20
	// toolResultNestDepth bounds how deep JSON text inside a result's
	// strings is parsed (a terminal result is JSON whose output is
	// kubectl's JSON).
	toolResultNestDepth = 4
)

// seenSecrets is one task's set of secret values seen in its tool results.
// It lives on the task's activityState and is cleared when the task
// finalizes. No value in it is logged or published: it is read only to
// redact (redact).
type seenSecrets struct {
	mu    sync.Mutex
	set   map[string]struct{}
	order []string // insertion order, the oldest first, for eviction
	bytes int
}

// add tracks a plain value and its base64 form.
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
// decoded value (and its own base64 form) when it decodes to text. A value
// that decodes to fewer than seenSecretMinBytes is not tracked in either
// form.
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
	d := strings.TrimSpace(string(raw))
	s.addLocked(d)
	s.addLocked(base64.StdEncoding.EncodeToString([]byte(d)))
}

func seenSecretSized(v string) bool {
	return len(v) >= seenSecretMinBytes && len(v) <= seenSecretMaxValueBytes
}

func (s *seenSecrets) addLocked(v string) {
	if !seenSecretSized(v) || strings.Contains(v, redactedValue) {
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

// clear forgets every value: the task has finalized.
func (s *seenSecrets) clear() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.set, s.order, s.bytes = nil, nil, 0
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
	// yamlSecretKindPattern finds a Secret's kind line in a YAML document.
	yamlSecretKindPattern = regexp.MustCompile(`(?m)^kind:[ \t]*["']?Secret["']?[ \t]*\r?$`)
	// jsonSecretBlockPattern and jsonStringPairPattern read a Secret's data
	// out of JSON text that does not parse whole (a truncated result).
	jsonSecretBlockPattern = regexp.MustCompile(`"(data|stringData)"\s*:\s*\{([^{}]*)\}`)
	jsonStringPairPattern  = regexp.MustCompile(`"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"`)
	jsonSecretKindPattern  = regexp.MustCompile(`"kind"\s*:\s*"Secret"`)
)

// collectToolResult adds the secret values one tool result carries: every
// value of a Kubernetes Secret's data (base64, decoded too) and stringData,
// whether the result holds the object as JSON (at any depth, in JSON text
// inside a string included) or as YAML, and every value a shape rule would
// redact in the result's text.
func (s *seenSecrets) collectToolResult(raw json.RawMessage) {
	if s == nil || len(raw) == 0 {
		return
	}
	c := toolResultCollector{set: s, budget: toolResultScanCap}
	var v any
	if json.Unmarshal(raw, &v) != nil {
		c.text(string(raw), 0)
		return
	}
	c.value(v, 0)
}

type toolResultCollector struct {
	set    *seenSecrets
	budget int // bytes of text the shape rules may still read
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

// secretObject adds a decoded Secret object's values. The namespace a
// service account token Secret carries is a name, not a credential.
func (c *toolResultCollector) secretObject(m map[string]any) {
	saToken := m["type"] == "kubernetes.io/service-account-token"
	if data, ok := m["data"].(map[string]any); ok {
		for k, v := range data {
			if sv, ok := v.(string); ok && !(saToken && k == "namespace") {
				c.set.addEncoded(sv)
			}
		}
	}
	if sd, ok := m["stringData"].(map[string]any); ok {
		for _, v := range sd {
			if sv, ok := v.(string); ok {
				c.set.add(sv)
			}
		}
	}
}

func (c *toolResultCollector) text(t string, depth int) {
	if t == "" {
		return
	}
	if trimmed := strings.TrimSpace(t); depth < toolResultNestDepth && (strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[")) {
		var v any
		if json.Unmarshal([]byte(trimmed), &v) == nil {
			c.value(v, depth+1)
		} else if jsonSecretKindPattern.MatchString(t) {
			c.truncatedJSONSecrets(t)
		}
	}
	if yamlSecretKindPattern.MatchString(t) {
		c.yamlSecrets(t)
	}
	if c.budget <= 0 {
		return
	}
	scan := t
	if len(scan) > c.budget {
		scan = capBytes(scan, c.budget)
	}
	c.budget -= len(scan)
	scrubInlineSecretsTo(scan, c.shapeCaught)
}

// shapeCaught tracks a value a shape rule redacted in a result's text,
// unless it is one plain word. The shape rules fail closed after a
// credential key, which costs one word where it stands, but the set
// redacts its values everywhere in the answer, so a plain word a rule
// caught (a constant's value under a *_KEY name in a source file the agent
// read) would come out of every sentence that uses it.
func (c *toolResultCollector) shapeCaught(v string) {
	if isPlainWord(trimValueEdges(v)) {
		return
	}
	c.set.add(v)
}

// truncatedJSONSecrets reads Secret data blocks out of JSON text cut short.
func (c *toolResultCollector) truncatedJSONSecrets(t string) {
	for _, blk := range jsonSecretBlockPattern.FindAllStringSubmatch(t, -1) {
		for _, kv := range jsonStringPairPattern.FindAllStringSubmatch(blk[2], -1) {
			var v string
			if json.Unmarshal([]byte(`"`+kv[2]+`"`), &v) != nil {
				continue
			}
			if blk[1] == "data" {
				c.set.addEncoded(v)
			} else {
				c.set.add(v)
			}
		}
	}
}

// yamlSecrets reads the data and stringData blocks of each YAML document in
// t whose kind is Secret.
func (c *toolResultCollector) yamlSecrets(t string) {
	for _, doc := range strings.Split(t, "\n---") {
		if !yamlSecretKindPattern.MatchString(doc) {
			continue
		}
		block, blockIndent := "", 0
		for _, line := range strings.Split(doc, "\n") {
			line = strings.TrimRight(line, "\r")
			if strings.TrimSpace(line) == "" {
				continue
			}
			indent := len(line) - len(strings.TrimLeft(line, " \t"))
			if m := yamlSecretBlockHeaderPattern.FindStringSubmatch(line); m != nil {
				block, blockIndent = strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(line), ":")), len(m[1])
				continue
			}
			if block == "" {
				continue
			}
			if indent <= blockIndent {
				block = ""
				continue
			}
			em := yamlBlockEntryPattern.FindStringSubmatch(line)
			if em == nil {
				continue
			}
			v := strings.Trim(strings.TrimSpace(em[2]), `"'`)
			if strings.HasPrefix(block, "data") {
				c.set.addEncoded(v)
			} else {
				c.set.add(v)
			}
		}
	}
}

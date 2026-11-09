package hermesbridge

import (
	"encoding/base64"
	"encoding/json"
	"regexp"
	"slices"
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
// The set takes the data and stringData values of objects whose kind is
// Secret, under any key but the connection settings seenSecretSkippedKeys
// names, and the values a shape rule catches in a result's text where the
// key, flag or prose noun in front names a credential outright, or the
// rule's own shape is a credential's (secretSink), one word with no space.
// Values under eight bytes are not tracked. A Secret's name, a byte count,
// an image digest or a phrase is never tracked: the set redacts its values
// everywhere in the answer.
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
	// The set's two pools (seenSecrets) each have their own share, and past
	// either of its caps a pool evicts its own oldest entry: Secret object
	// data 384 entries and 768 KiB, shape-caught values 128 entries and
	// 256 KiB. seenSecretMaxEntries and seenSecretMaxBytes are the totals.
	seenSecretPoolEntries = 384
	seenSecretPoolBytes   = 768 << 10
	seenShapePoolEntries  = 128
	seenShapePoolBytes    = 256 << 10
	seenSecretMaxEntries  = seenSecretPoolEntries + seenShapePoolEntries
	seenSecretMaxBytes    = seenSecretPoolBytes + seenShapePoolBytes
	// seenSecretMaxPerResult is how many Secret object entries one tool
	// result may add, so a result holding a forged or huge Secret cannot
	// flush the Secret values earlier results carried.
	seenSecretMaxPerResult = 128
	// toolResultScanCap bounds how much of one result's text the regex
	// readers (the shape rules, the YAML reader) scan.
	toolResultScanCap = 1 << 20
	// toolResultParseCap bounds the text the JSON readers (a whole parse,
	// the scan of JSON cut short) read: the door's own body cap, so a
	// Secret list of any size the door accepts is read whole.
	toolResultParseCap = activityBodyCap
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

// seenSecrets is one task's set of secret values seen in its tool results,
// in two pools: values from Secret objects, and values the shape rules
// caught (secretSink). It lives on the task's activityState and is
// cleared, and closed to further values, when the task finalizes. No value
// in it is logged or published: it is read only to redact (redact).
//
// Each pool has a fixed share of the caps and evicts only its own oldest
// entry, so neither pool can flush the other: shape-caught values, however
// many a result holds (or is written to hold), never evict Secret data,
// and a large or forged Secret never evicts shape-caught values. One tool
// result adds at most seenSecretMaxPerResult Secret entries. A value both
// pools would hold is kept in the Secret pool.
type seenSecrets struct {
	mu     sync.Mutex
	set    map[string]bool // value -> held in the Secret pool
	secret seenPool
	shape  seenPool
	closed bool // the task has finalized: a late delivery adds nothing
}

// seenPool is one pool's entries in insertion order, the oldest first.
type seenPool struct {
	order []string
	bytes int
}

func (s *seenSecrets) pool(fromSecret bool) (*seenPool, int, int) {
	if fromSecret {
		return &s.secret, seenSecretPoolEntries, seenSecretPoolBytes
	}
	return &s.shape, seenShapePoolEntries, seenShapePoolBytes
}

// add tracks a Secret's plain value (a stringData value) and its base64
// form, and says how many entries it added.
func (s *seenSecrets) add(v string) int { return s.addPlain(v, true) }

// addShape tracks a value a shape rule caught, and its base64 form, in the
// shape-caught pool.
func (s *seenSecrets) addShape(v string) { s.addPlain(v, false) }

func (s *seenSecrets) addPlain(v string, fromSecret bool) int {
	v = strings.TrimSpace(v)
	if !seenSecretSized(v) {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.addLocked(v, fromSecret) + s.addLocked(base64.StdEncoding.EncodeToString([]byte(v)), fromSecret)
}

// addEncoded tracks a Secret data value: the base64 form as given, and the
// decoded value when it decodes to text, and says how many entries it
// added. A value that decodes to fewer than seenSecretMinBytes is not
// tracked in either form.
func (s *seenSecrets) addEncoded(b64 string) int {
	b64 = strings.TrimSpace(b64)
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err == nil && len(strings.TrimSpace(string(raw))) < seenSecretMinBytes {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	n := s.addLocked(b64, true)
	if err != nil || !utf8.Valid(raw) {
		return n
	}
	return n + s.addLocked(strings.TrimSpace(string(raw)), true)
}

func seenSecretSized(v string) bool {
	return len(v) >= seenSecretMinBytes && len(v) <= seenSecretMaxValueBytes
}

// addLocked tracks v in its pool and says whether that added an entry to
// the pool (1) or not (0), evicting the pool's own oldest past its caps.
func (s *seenSecrets) addLocked(v string, fromSecret bool) int {
	if s.closed || !seenSecretSized(v) || strings.Contains(v, redactedValue) {
		return 0
	}
	if s.set == nil {
		s.set = make(map[string]bool)
	}
	if inSecret, ok := s.set[v]; ok {
		if !fromSecret || inSecret {
			return 0
		}
		// A shape-caught value a Secret also holds moves to the Secret pool.
		s.shape.order = slices.DeleteFunc(s.shape.order, func(x string) bool { return x == v })
		s.shape.bytes -= len(v)
	}
	s.set[v] = fromSecret
	p, maxEntries, maxBytes := s.pool(fromSecret)
	p.order = append(p.order, v)
	p.bytes += len(v)
	for len(p.order) > maxEntries || p.bytes > maxBytes {
		old := p.order[0]
		p.order = p.order[1:]
		p.bytes -= len(old)
		delete(s.set, old)
	}
	return 1
}

// size is how many values the set holds.
func (s *seenSecrets) size() int {
	if s == nil {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.set)
}

// has says whether v is tracked; for tests.
func (s *seenSecrets) has(v string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.set[v]
	return ok
}

// values is every tracked value; for tests and redact.
func (s *seenSecrets) values() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append(append([]string(nil), s.secret.order...), s.shape.order...)
}

// clear forgets every value and takes no more: the task has finalized, so
// a delivery still on its way past the door adds nothing.
func (s *seenSecrets) clear() {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.set, s.secret, s.shape, s.closed = nil, seenPool{}, seenPool{}, true
}

// redact replaces every tracked value in text with the marker, the longest
// first where two overlap.
func (s *seenSecrets) redact(text string) string {
	if s == nil || text == "" {
		return text
	}
	vals := s.values()
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
)

// collectToolResult adds the secret values one tool result carries: every
// value of a Kubernetes Secret's data (base64, decoded too) and stringData,
// whether the result holds the object as JSON (at any depth, in JSON text
// inside a string included, or cut short) or as YAML (a document, or an
// item of a list), and the values secretSink describes: a string under a
// JSON key that names a credential outright, and what the shape rules hand
// on from the result's text.
func (s *seenSecrets) collectToolResult(raw json.RawMessage) {
	if s == nil || len(raw) == 0 {
		return
	}
	c := toolResultCollector{set: s, budget: toolResultScanCap, secretLeft: seenSecretMaxPerResult}
	var v any
	if json.Unmarshal(raw, &v) != nil {
		c.text(string(raw), 0)
		return
	}
	c.value(v, 0)
}

type toolResultCollector struct {
	set        *seenSecrets
	budget     int // bytes of text the shape rules may still read
	secretLeft int // Secret entries this result may still add
}

// addSecret and addSecretEncoded track a Secret's value within the
// result's allowance (seenSecretMaxPerResult).
func (c *toolResultCollector) addSecret(v string) {
	if c.secretLeft > 0 {
		c.secretLeft -= c.set.add(v)
	}
}

func (c *toolResultCollector) addSecretEncoded(b64 string) {
	if c.secretLeft > 0 {
		c.secretLeft -= c.set.addEncoded(b64)
	}
}

func (c *toolResultCollector) value(v any, depth int) {
	switch t := v.(type) {
	case string:
		c.text(t, depth)
	case map[string]any:
		if kind, _ := t["kind"].(string); kind == "Secret" {
			c.secretObject(t)
		}
		for k, x := range t {
			if sv, ok := x.(string); ok && keyClearlyNamesSecret(k) {
				c.shapeCaught(sv)
			}
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
				c.addSecretEncoded(sv)
			}
		}
	}
	if sd, ok := m["stringData"].(map[string]any); ok {
		for k, v := range sd {
			if sv, ok := v.(string); ok && !seenSecretKeySkipped(k, saToken) {
				c.addSecret(sv)
			}
		}
	}
}

// text reads one string of a result: as JSON when it is JSON text (read
// whole, or scanned when cut short), as YAML when it holds a kind: Secret
// line, and through the shape rules within the scan budget. JSON that
// parses is read as a structure only, so the shape rules see its strings
// unescaped.
func (c *toolResultCollector) text(t string, depth int) {
	if t == "" {
		return
	}
	if trimmed := strings.TrimSpace(capBytes(t, toolResultParseCap)); depth < toolResultNestDepth && (strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[")) {
		var v any
		if json.Unmarshal([]byte(trimmed), &v) == nil {
			c.value(v, depth+1)
			return
		}
		if strings.Contains(trimmed, "Secret") {
			c.jsonSecretScan(trimmed)
		}
	}
	scan := capBytes(t, toolResultScanCap)
	if strings.Contains(scan, "Secret") && yamlAnySecretKindPattern.MatchString(scan) {
		c.yamlSecrets(scan)
	}
	if c.budget <= 0 {
		return
	}
	if len(scan) > c.budget {
		scan = capBytes(scan, c.budget)
	}
	c.budget -= len(scan)
	scrubInlineSecretsTo(scan, c.shapeCaught)
}

// shapeCaught tracks a value secretSink hands on, its quotes and trailing
// punctuation taken off, in the shape-caught pool, unless it holds a space
// (a phrase, "14 bytes" or "denied for user ...", is not a credential to
// match everywhere).
func (c *toolResultCollector) shapeCaught(v string) {
	v = trimValueEdges(v)
	if strings.ContainsAny(v, " \t\r\n") {
		return
	}
	c.set.addShape(v)
}

// jsonSecretScan reads Secret data out of JSON text that does not parse
// whole (cut short): a scan that tracks each object's own kind, type and
// data and stringData blocks, so a block is read for the object it belongs
// to, whatever other kinds the text holds (an ownerReference, another item
// of a list). An object's pairs are taken when it closes, or at the cut
// when its kind was read before it; a pair cut short is not taken. In
// kubectl's output a block comes before its own object's kind, so an item
// cut inside its data is taken as a Secret when every item of its list
// read before it was one (kubectl get secrets -o json, cut short); a
// single Secret cut there, with no kind read at all, is not read.
func (c *toolResultCollector) jsonSecretScan(t string) {
	type pair struct {
		key, value string
		stringData bool
	}
	type frame struct {
		object     bool
		key        string
		haveKey    bool
		kind       string
		saToken    bool
		owner      *frame // a data block's object
		parent     *frame
		stringData bool
		pairs      []pair
		// an array's items read whole so far: Secrets, and anything else
		secretItems, otherItems int
	}
	var stack []*frame
	settle := func(f *frame, cut bool) {
		if !f.object || f.owner != nil {
			return
		}
		if p := f.parent; !cut && p != nil && !p.object {
			if f.kind == "Secret" {
				p.secretItems++
			} else {
				p.otherItems++
			}
		}
		kind := f.kind
		if p := f.parent; cut && kind == "" && p != nil && !p.object && p.secretItems > 0 && p.otherItems == 0 {
			kind = "Secret"
		}
		if kind != "Secret" {
			return
		}
		for _, p := range f.pairs {
			switch {
			case seenSecretKeySkipped(p.key, f.saToken):
			case p.stringData:
				c.addSecret(p.value)
			default:
				c.addSecretEncoded(p.value)
			}
		}
	}
	valueDone := func() {
		if n := len(stack); n > 0 {
			stack[n-1].haveKey = false
		}
	}
	for i := 0; i < len(t); {
		ch := t[i]
		switch ch {
		case '{', '[':
			f := &frame{object: ch == '{'}
			if n := len(stack); n > 0 {
				f.parent = stack[n-1]
			}
			if n := len(stack); n > 0 && ch == '{' {
				if p := stack[n-1]; p.object && p.haveKey && p.owner == nil && (p.key == "data" || p.key == "stringData") {
					f.owner, f.stringData = p, p.key == "stringData"
				}
			}
			stack = append(stack, f)
			i++
		case '}', ']':
			if n := len(stack); n > 0 {
				settle(stack[n-1], false)
				stack = stack[:n-1]
			}
			valueDone()
			i++
		case '"':
			end := i + 1
			for end < len(t) && t[end] != '"' {
				if t[end] == '\\' {
					end++
				}
				end++
			}
			if end >= len(t) {
				i = len(t) // a string cut short: nothing after it is read
				break
			}
			var str string
			ok := json.Unmarshal([]byte(t[i:end+1]), &str) == nil
			i = end + 1
			n := len(stack)
			if n == 0 || !stack[n-1].object {
				break
			}
			top := stack[n-1]
			if !top.haveKey {
				j := i
				for j < len(t) && (t[j] == ' ' || t[j] == '\t' || t[j] == '\r' || t[j] == '\n') {
					j++
				}
				if j < len(t) && t[j] == ':' {
					top.key, top.haveKey, i = str, true, j+1
				}
				break
			}
			if ok {
				switch {
				case top.owner != nil:
					top.owner.pairs = append(top.owner.pairs, pair{top.key, str, top.stringData})
				case top.key == "kind":
					top.kind = str
				case top.key == "type" && str == "kubernetes.io/service-account-token":
					top.saToken = true
				}
			}
			top.haveKey = false
		case ',':
			valueDone()
			i++
		default:
			i++
		}
	}
	for k := len(stack) - 1; k >= 0; k-- {
		settle(stack[k], true)
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
			c.addSecretEncoded(v)
		} else {
			c.addSecret(v)
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

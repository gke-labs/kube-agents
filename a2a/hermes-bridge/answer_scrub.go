package hermesbridge

import (
	"encoding/base64"
	"encoding/json"
	"maps"
	"regexp"
	"slices"
	"sort"
	"strconv"
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
	// The set's two pools (seenSecrets) each have a fixed reserve: Secret
	// object data 384 entries and 768 KiB, shape-caught values 128 entries
	// and 256 KiB. seenSecretMaxEntries and seenSecretMaxBytes are the
	// totals.
	seenSecretPoolEntries = 384
	seenSecretPoolBytes   = 768 << 10
	seenShapePoolEntries  = 128
	seenShapePoolBytes    = 256 << 10
	seenSecretMaxEntries  = seenSecretPoolEntries + seenShapePoolEntries
	seenSecretMaxBytes    = seenSecretPoolBytes + seenShapePoolBytes
	// One tool result's allowance in each pool, about a third of it,
	// charged with the entries it actually added.
	seenSecretResultEntries = 128
	seenSecretResultBytes   = 256 << 10
	seenShapeResultEntries  = 64
	seenShapeResultBytes    = 96 << 10
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
// Each pool has a fixed reserve, and each tool result is a generation with
// an allowance in each pool. A result may evict only its own earlier
// entries, never an earlier result's: past its allowance a new value
// replaces that result's own oldest entry, and when the pool is full of
// earlier results' entries the value is refused and counted (refused, a
// number only). So neither pool can flush the other, and no result, a
// forged or huge one included, can flush what earlier results carried; a
// task whose results hold more values than the pools keep tracks the
// earliest ones. A value both pools would hold is kept in the Secret pool
// when it can take the value, else in the shape pool: in exactly one.
type seenSecrets struct {
	mu      sync.Mutex
	set     map[string]bool // value -> held in the Secret pool
	secret  seenPool
	shape   seenPool
	charge  map[seenCharge]seenUse // each generation's use of each pool
	nextGen uint64
	refused int  // values refused because a pool was full of earlier results'
	closed  bool // the task has finalized: a late delivery adds nothing
}

// seenPool is one pool's entries in insertion order, the oldest first.
type seenPool struct {
	entries []seenEntry
	bytes   int
}

type seenEntry struct {
	v   string
	gen uint64
}

type seenCharge struct {
	gen    uint64
	secret bool
}

type seenUse struct{ entries, bytes int }

// newGen is a fresh generation, one per tool result.
func (s *seenSecrets) newGen() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.nextGen++
	return s.nextGen
}

func (s *seenSecrets) pool(fromSecret bool) (p *seenPool, maxEntries, maxBytes, allowEntries, allowBytes int) {
	if fromSecret {
		return &s.secret, seenSecretPoolEntries, seenSecretPoolBytes, seenSecretResultEntries, seenSecretResultBytes
	}
	return &s.shape, seenShapePoolEntries, seenShapePoolBytes, seenShapeResultEntries, seenShapeResultBytes
}

// add tracks a Secret's plain value (a stringData value) and its base64
// form, as a result of its own.
func (s *seenSecrets) add(v string) { s.addPlain(v, true, s.newGen()) }

// addShape tracks a value a shape rule caught, and its base64 form, in the
// shape-caught pool, as a result of its own.
func (s *seenSecrets) addShape(v string) { s.addPlain(v, false, s.newGen()) }

func (s *seenSecrets) addPlain(v string, fromSecret bool, gen uint64) {
	v = strings.TrimSpace(v)
	if !seenSecretSized(v) {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.addLocked(v, fromSecret, gen)
	s.addLocked(base64.StdEncoding.EncodeToString([]byte(v)), fromSecret, gen)
}

// addEncoded tracks a Secret data value: the base64 form as given, and the
// decoded value when it decodes to text, trimmed, with that trimmed value's
// own base64 when trimming changed it (a file's or echo's trailing newline
// gone, as a re-encoding of the value would have it). A value that decodes to fewer
// than seenSecretMinBytes is not tracked in either form.
func (s *seenSecrets) addEncoded(b64 string, gen uint64) {
	b64 = strings.TrimSpace(b64)
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err == nil && len(strings.TrimSpace(string(raw))) < seenSecretMinBytes {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.addLocked(b64, true, gen)
	if err != nil || !utf8.Valid(raw) {
		return
	}
	trimmed := strings.TrimSpace(string(raw))
	s.addLocked(trimmed, true, gen)
	if trimmed != string(raw) {
		s.addLocked(base64.StdEncoding.EncodeToString([]byte(trimmed)), true, gen)
	}
}

func seenSecretSized(v string) bool {
	return len(v) >= seenSecretMinBytes && len(v) <= seenSecretMaxValueBytes
}

// addLocked tracks v in its pool for generation gen, as seenSecrets says.
func (s *seenSecrets) addLocked(v string, fromSecret bool, gen uint64) {
	if s.closed || !seenSecretSized(v) || strings.Contains(v, redactedValue) {
		return
	}
	if s.set == nil {
		s.set = make(map[string]bool)
		s.charge = make(map[seenCharge]seenUse)
	}
	inSecret, held := s.set[v]
	if held && (!fromSecret || inSecret) {
		return
	}
	p, maxEntries, maxBytes, allowEntries, allowBytes := s.pool(fromSecret)
	key := seenCharge{gen, fromSecret}
	// Whether p can take v at all, reclaiming only gen's own entries, is
	// known before anything changes: a value p refuses evicts nothing, and
	// one the shape pool holds stays there.
	own := s.charge[key]
	if allowEntries < 1 || len(v) > allowBytes {
		return
	}
	if len(p.entries)-own.entries+1 > maxEntries || p.bytes-own.bytes+len(v) > maxBytes {
		s.refused++
		return
	}
	if held {
		// A shape-caught value a Secret also holds moves to the Secret pool.
		s.removeLocked(&s.shape, false, func(e seenEntry) bool { return e.v == v })
	}
	for u := s.charge[key]; u.entries+1 > allowEntries || u.bytes+len(v) > allowBytes; u = s.charge[key] {
		if !s.removeLocked(p, fromSecret, func(e seenEntry) bool { return e.gen == gen }) {
			return // nothing of its own left to replace: refused
		}
	}
	for len(p.entries)+1 > maxEntries || p.bytes+len(v) > maxBytes {
		if !s.removeLocked(p, fromSecret, func(e seenEntry) bool { return e.gen == gen }) {
			s.refused++
			return
		}
	}
	p.entries = append(p.entries, seenEntry{v, gen})
	p.bytes += len(v)
	s.set[v] = fromSecret
	u := s.charge[key]
	s.charge[key] = seenUse{u.entries + 1, u.bytes + len(v)}
}

// removeLocked drops the oldest entry of p that match says, and says
// whether there was one.
func (s *seenSecrets) removeLocked(p *seenPool, fromSecret bool, match func(seenEntry) bool) bool {
	for i, e := range p.entries {
		if !match(e) {
			continue
		}
		p.entries = slices.Delete(p.entries, i, i+1)
		p.bytes -= len(e.v)
		delete(s.set, e.v)
		key := seenCharge{e.gen, fromSecret}
		if u := s.charge[key]; u.entries <= 1 {
			delete(s.charge, key)
		} else {
			s.charge[key] = seenUse{u.entries - 1, u.bytes - len(e.v)}
		}
		return true
	}
	return false
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
	out := make([]string, 0, len(s.secret.entries)+len(s.shape.entries))
	for _, e := range append(append([]seenEntry(nil), s.secret.entries...), s.shape.entries...) {
		out = append(out, e.v)
	}
	return out
}

// clear forgets every value and takes no more: the task has finalized, so
// a delivery still on its way past the door adds nothing. It says how many
// values the set refused while it was open (a count, never a value).
func (s *seenSecrets) clear() int {
	if s == nil {
		return 0
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	n := s.refused
	s.set, s.secret, s.shape, s.charge, s.refused, s.closed = nil, seenPool{}, seenPool{}, nil, 0, true
	return n
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
	c := toolResultCollector{set: s, budget: toolResultScanCap, gen: s.newGen()}
	var v any
	if json.Unmarshal(raw, &v) != nil {
		c.text(string(raw), 0)
		return
	}
	c.value(v, 0)
}

type toolResultCollector struct {
	set    *seenSecrets
	budget int    // bytes of text the shape rules may still read
	gen    uint64 // this result's generation (seenSecrets)
}

func (c *toolResultCollector) addSecret(v string)          { c.set.addPlain(v, true, c.gen) }
func (c *toolResultCollector) addSecretEncoded(b64 string) { c.set.addEncoded(b64, c.gen) }

func (c *toolResultCollector) value(v any, depth int) {
	switch t := v.(type) {
	case string:
		c.text(t, depth)
	case map[string]any:
		secret := false
		kind, _ := t["kind"].(string)
		if items, ok := t["items"].([]any); ok && kind == "SecretList" {
			// The API server's SecretList leaves kind off its items: each
			// is a Secret.
			for _, it := range items {
				if m, ok := it.(map[string]any); ok && m["kind"] == nil {
					m["kind"] = "Secret"
				}
			}
		}
		if kind == "Secret" {
			c.secretObject(t)
			secret = true
		}
		for k, x := range t {
			if secret && (k == "data" || k == "stringData") {
				continue // read above
			}
			if sv, ok := x.(string); ok && keyClearlyNamesSecret(k) && !jsonValueNotASecret(sv) {
				c.exactCaught(sv)
			}
			c.value(x, depth)
		}
	case []any:
		for _, x := range t {
			c.value(x, depth)
		}
	}
}

// envVarReferencePattern is a value that is wholly an upper-case
// environment variable reference ($DB_PASSWORD, ${DB_PASSWORD}).
var envVarReferencePattern = regexp.MustCompile(`^\$\{?[A-Z_][A-Z0-9_]*\}?$`)

// jsonValueNotASecret says a decoded JSON string under a credential key is
// plainly not a value: the whole of it, as decoded, is a state word
// (credentialStateWords, credentialWholeValueWords: password: disabled) or
// an upper-case environment variable reference. A narrower test than the
// free-text valueNotASecret on purpose: the value's edges are exact, so it
// is not trimmed, and a value that only looks like a placeholder ($(...),
// <...>, a mixed-case $name) can be a credential and is tracked.
func jsonValueNotASecret(v string) bool {
	lower := strings.ToLower(v)
	return credentialStateWords[lower] || credentialWholeValueWords[lower] || envVarReferencePattern.MatchString(v)
}

// secretObject adds a decoded Secret object's values, but for the keys
// seenSecretKeySkipped names.
func (c *toolResultCollector) secretObject(m map[string]any) {
	saToken := m["type"] == "kubernetes.io/service-account-token"
	// In key order, so which values a result over its allowance keeps
	// (its last) does not depend on map order.
	if data, ok := m["data"].(map[string]any); ok {
		for _, k := range slices.Sorted(maps.Keys(data)) {
			if sv, ok := data[k].(string); ok && !seenSecretKeySkipped(k, saToken) {
				c.addSecretEncoded(sv)
			}
		}
	}
	if sd, ok := m["stringData"].(map[string]any); ok {
		for _, k := range slices.Sorted(maps.Keys(sd)) {
			if sv, ok := sd[k].(string); ok && !seenSecretKeySkipped(k, saToken) {
				c.addSecret(sv)
			}
		}
	}
}

// text reads one string of a result: as JSON when it is JSON text (read
// whole, or scanned when cut short), as YAML when it holds a kind: Secret
// line, and through the shape rules within the scan budget. JSON that
// parses is read as a structure, so the shape rules see each of its
// strings unescaped, and then as text too, so the rules that need a key
// and its value together see pairs that span fields or items (an env
// entry's name and value, a flag and its value in an args list, a header
// list's name and value).
func (c *toolResultCollector) text(t string, depth int) {
	if t == "" {
		return
	}
	scan := capBytes(t, toolResultScanCap)
	if trimmed := strings.TrimSpace(capBytes(t, toolResultParseCap)); depth < toolResultNestDepth && (strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[")) {
		var v any
		if json.Unmarshal([]byte(trimmed), &v) == nil {
			c.value(v, depth+1)
			c.shapeScan(blankMultilineJSONStrings(scan))
			return
		}
		if strings.Contains(trimmed, "Secret") {
			c.jsonSecretScan(trimmed)
		}
	}
	if strings.Contains(scan, "Secret") && yamlAnySecretKindPattern.MatchString(scan) {
		c.yamlSecrets(scan)
	}
	c.shapeScan(scan)
}

// blankMultilineJSONStrings is JSON text t for the shape rules that pair
// sibling fields (an env entry's name and value, a flag and its value in an
// args list): every string token holding an escaped line break is emptied
// in place, since c.value already reads it as text of its own and its \n
// would run one line's value into the next. Keys keep their order; a
// string cut short at the end of t is left as it is.
func blankMultilineJSONStrings(t string) string {
	if !strings.Contains(t, `\n`) && !strings.Contains(t, `\r`) {
		return t
	}
	var sb strings.Builder
	sb.Grow(len(t))
	for i := 0; i < len(t); {
		if t[i] != '"' {
			sb.WriteByte(t[i])
			i++
			continue
		}
		end, multiline := i+1, false
		for end < len(t) && t[end] != '"' {
			if t[end] == '\\' && end+1 < len(t) {
				if t[end+1] == 'n' || t[end+1] == 'r' {
					multiline = true
				}
				end++
			}
			end++
		}
		if end >= len(t) {
			sb.WriteString(t[i:])
			break
		}
		if multiline {
			sb.WriteString(`""`)
		} else {
			sb.WriteString(t[i : end+1])
		}
		i = end + 1
	}
	return sb.String()
}

// shapeScan runs the shape rules over scan, within what is left of the
// result's scan budget, handing what they catch to shapeCaught.
func (c *toolResultCollector) shapeScan(scan string) {
	if c.budget <= 0 {
		return
	}
	if len(scan) > c.budget {
		scan = capBytes(scan, c.budget)
	}
	c.budget -= len(scan)
	scrubInlineSecretsTo(scan, c.shapeCaught)
}

// shapeCaught tracks a value secretSink hands on, cut from free text, its
// quotes and trailing punctuation taken off (trimValueEdges), in the
// shape-caught pool (trackShape).
func (c *toolResultCollector) shapeCaught(v string) { c.trackShape(trimValueEdges(v)) }

// exactCaught tracks a value whose edges are already exact, a decoded JSON
// string, as it is: trailing punctuation in it is its own, not a
// sentence's (trackShape). Where trimValueEdges would change it, the
// trimmed form is tracked as well, unless it is a state word or an env
// var reference (jsonValueNotASecret), for an answer that drops the
// value's quotes or trailing punctuation; redact replaces the longest
// match first, so the exact form still covers its own punctuation.
func (c *toolResultCollector) exactCaught(v string) {
	c.trackShape(v)
	if tv := trimValueEdges(v); tv != v && !jsonValueNotASecret(tv) {
		c.trackShape(tv)
	}
}

// trackShape adds v to the shape-caught pool unless it holds a space (a
// phrase, "14 bytes" or "denied for user ...", is not a credential to
// match everywhere).
func (c *toolResultCollector) trackShape(v string) {
	if strings.ContainsAny(v, " \t\r\n") {
		return
	}
	c.set.addPlain(v, false, c.gen)
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
// single Secret cut there, with no kind read at all, is not read. An item
// of a SecretList's items is a Secret: the API server's list (kubectl get
// --raw, a curl through the API proxy) leaves kind off its items. When
// the list's own kind comes after its items (keys sorted: jq -S, a
// re-marshalled map), a kind-less item is held until the list settles. An
// object cut before its type was read may be a service-account token, so
// its namespace is not taken.
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
		typeSeen   bool
		owner      *frame // a data block's object
		parent     *frame
		stringData bool
		pairs      []pair
		// an array's items read whole so far: Secrets, and anything else
		secretItems, otherItems int
		// kind-less items of this object's items, held until its kind is known
		deferred []*frame
	}
	var stack []*frame
	take := func(f *frame, cut bool) {
		saToken := f.saToken || cut && !f.typeSeen
		for _, p := range f.pairs {
			switch {
			case seenSecretKeySkipped(p.key, saToken):
			case p.stringData:
				c.addSecret(p.value)
			default:
				c.addSecretEncoded(p.value)
			}
		}
	}
	settle := func(f *frame, cut bool) {
		if !f.object || f.owner != nil {
			return
		}
		if f.kind == "SecretList" {
			for _, d := range f.deferred {
				take(d, false)
			}
		}
		f.deferred = nil
		kind := f.kind
		if p := f.parent; !cut && kind == "" && p != nil && !p.object && p.parent != nil && p.parent.key == "items" && p.parent.kind == "" {
			p.parent.deferred = append(p.parent.deferred, f)
			return
		}
		if p := f.parent; kind == "" && p != nil && !p.object && p.parent != nil && p.parent.kind == "SecretList" && p.parent.key == "items" {
			kind = "Secret" // the API server's SecretList leaves kind off its items
		}
		if p := f.parent; !cut && p != nil && !p.object {
			if kind == "Secret" {
				p.secretItems++
			} else {
				p.otherItems++
			}
		}
		if p := f.parent; cut && kind == "" && p != nil && !p.object && p.secretItems > 0 && p.otherItems == 0 {
			kind = "Secret"
		}
		if kind != "Secret" {
			return
		}
		take(f, cut)
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
				case top.key == "type":
					top.typeSeen = true
					top.saToken = str == "kubernetes.io/service-account-token"
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
				// A blank or comment line, at any column, does not end it.
				if t := strings.TrimSpace(next); t == "" || strings.HasPrefix(t, "#") {
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
		// Both forms of a quoted value with escapes in it: an answer may
		// repeat the value or quote the manifest as written.
		v, raw := yamlScalar(em[2])
		for _, x := range slices.Compact([]string{v, raw}) {
			if block == "data" {
				c.addSecretEncoded(x)
			} else {
				c.addSecret(x)
			}
		}
	}
}

// yamlScalar is an entry's value: a quoted one's contents with its escapes
// read, and raw, the contents as written between the quotes (yamlQuoted),
// else the value with a trailing comment taken off (and an opening quote
// that never closes), raw the same.
func yamlScalar(v string) (value, raw string) {
	if len(v) >= 2 && (v[0] == '"' || v[0] == '\'') {
		if q, r, ok := yamlQuoted(v); ok {
			return q, r
		}
		v = v[1:]
	}
	if i := strings.Index(v, " #"); i >= 0 {
		v = v[:i]
	}
	v = strings.TrimSpace(v)
	return v, v
}

func yamlIndent(l string) int {
	return len(l) - len(strings.TrimLeft(l, " \t"))
}

// yamlEscapes are the one-character escapes of a double-quoted YAML scalar.
var yamlEscapes = map[byte]string{
	'0': "\x00", 'a': "\a", 'b': "\b", 't': "\t", '\t': "\t", 'n': "\n", 'v': "\v", 'f': "\f",
	'r': "\r", 'e': "\x1b", ' ': " ", '"': "\"", '/': "/", '\\': "\\", 'N': "\u0085",
	'_': "\u00a0", 'L': "\u2028", 'P': "\u2029",
}

// yamlQuoted reads the quoted scalar v opens with: raw is its contents as
// written, value the same with its escapes read (in double quotes the
// backslash escapes, in single quotes each doubled quote as one), or raw
// again when an escape is not YAML's. ok is false when the quote does not
// close.
func yamlQuoted(v string) (value, raw string, ok bool) {
	q := v[0]
	end := -1
	for i := 1; i < len(v) && end < 0; i++ {
		switch {
		case q == '"' && v[i] == '\\':
			i++
		case v[i] != q:
		case q == '\'' && i+1 < len(v) && v[i+1] == '\'':
			i++
		default:
			end = i
		}
	}
	if end < 0 {
		return "", "", false
	}
	raw = v[1:end]
	if q == '\'' {
		return strings.ReplaceAll(raw, "''", "'"), raw, true
	}
	if value, ok = yamlUnescape(raw); !ok {
		value = raw
	}
	return value, raw, true
}

// yamlUnescape reads the backslash escapes of a double-quoted scalar's
// contents; ok is false when one is not YAML's.
func yamlUnescape(raw string) (string, bool) {
	if !strings.Contains(raw, "\\") {
		return raw, true
	}
	var sb strings.Builder
	for i := 0; i < len(raw); i++ {
		if raw[i] != '\\' {
			sb.WriteByte(raw[i])
			continue
		}
		if i+1 >= len(raw) {
			return "", false
		}
		i++
		if e, ok := yamlEscapes[raw[i]]; ok {
			sb.WriteString(e)
			continue
		}
		n := map[byte]int{'x': 2, 'u': 4, 'U': 8}[raw[i]]
		if n == 0 || i+n >= len(raw) {
			return "", false
		}
		u, err := strconv.ParseUint(raw[i+1:i+1+n], 16, 32)
		// Bounded before the conversion, so rune(u) is in range.
		if err != nil || u > utf8.MaxRune {
			return "", false
		}
		r := rune(u)
		if !utf8.ValidRune(r) {
			return "", false
		}
		sb.WriteRune(r)
		i += n
	}
	return sb.String(), true
}

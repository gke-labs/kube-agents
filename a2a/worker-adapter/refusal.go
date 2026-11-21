package workeradapter

import (
	"bytes"
	"encoding/json"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// A profile task's harness is stopped at its first definitive write refusal
// (spec-subagent-profiles.md, "Write refusals"). classifyRefusal is the
// predicate deploy/docker/patches/permission_refusal_stop.py applies to a
// Hermes worker's tool results, ported line for line, and
// tests/conformance/fixtures/write-refusal-results.json holds the cases both
// must agree on. A refusal is definitive when the result carries the action
// boundary notice and a `policy rule: kubernetes.read-only` block whose
// `policy verb:` names a mutating kubectl verb and that has no
// `policy render: local` line. A gcp.read-only refusal, a refused read
// spelling, a local render and an API 403 are not; the worker lets the task
// go on past them.

// Refusal classes, as permission_refusal_stop.classify_refusal names them.
const (
	refusalPolicy   = "policy"
	refusalAdvisory = "advisory"
	refusalAPI      = "api"
)

// definitiveRule is the rule a definitive refusal carries.
const definitiveRule = "kubernetes.read-only"

// verbLinePrefix and ruleLinePrefix begin the shim's lines.
const (
	verbLinePrefix = "policy verb: "
	ruleLinePrefix = "policy rule: "
)

// The result's readers anchor on the shim's line breaks, which a JSON-encoded
// result carries as a literal `\n`.
const lineStart = `(?:^|\n|\\n)`

// Python's re is Unicode by default and Go's \w, \b, \d and \s are ASCII, so
// the patterns spell Python's classes out: a word character is a letter, a
// number or `_` (str.isalnum), and a \b beside a word character is a
// non-word character or an end of the text, consumed: no reader here uses
// where a boundary match ends, only whether it matched and where a rule line
// starts.
const (
	pyWord       = `\p{L}\p{N}_`
	pyNotWordL   = `(?:^|[^` + pyWord + `])`
	pyNotWordR   = `(?:[^` + pyWord + `]|$)`
	pyDigit      = `\p{Nd}`
	pyWhitespace = `[\t\n\v\f\r\x1c-\x1f\x85\p{Z}]`
)

var (
	policyActionRE = regexp.MustCompile(`refused however it is attempted`)
	ruleLineRE     = regexp.MustCompile(lineStart + `policy rule: ([` + pyWord + `.-]+)`)
	verbLineRE     = regexp.MustCompile(lineStart + `policy verb: ([^\n\\"]+)`)
	renderLineRE   = regexp.MustCompile(lineStart + `policy render: local` + pyNotWordR)
	// Kubernetes and Google Cloud API denials. 401 and Unauthenticated are absent on purpose.
	apiDeniedRE = regexp.MustCompile(`Error from server \(Forbidden\)|` + pyNotWordL + `is forbidden:|` +
		pyNotWordL + `PERMISSION_DENIED` + pyNotWordR + `|"code":` + pyWhitespace + `*403` + pyNotWordR + `|` +
		pyNotWordL + `HTTP(?:/[` + pyDigit + `.]+)? 403` + pyNotWordR)
	// Where one command ends and the next begins, and an argv literal's list punctuation.
	separatorRE = regexp.MustCompile(`\|\||&&|[;|&\n]`)
	listPunctRE = regexp.MustCompile(`[\[\],'"()]`)
)

// mutatingVerbs are the kubectl verbs that change state (debug starts a pod or container).
var mutatingVerbs = map[string]bool{
	"scale": true, "patch": true, "edit": true, "apply": true, "replace": true, "autoscale": true,
	"annotate": true, "label": true, "create": true, "delete": true, "set": true, "run": true,
	"expose": true, "cordon": true, "uncordon": true, "drain": true, "taint": true, "debug": true,
}

// mutatingSubverbs are the verbs whose second word decides.
var mutatingSubverbs = map[string]map[string]bool{
	"rollout":     {"restart": true, "undo": true, "pause": true, "resume": true},
	"auth":        {"reconcile": true},
	"certificate": {"approve": true, "deny": true},
}

// kubectl's flag tables, as agents/platform/scripts/command_policy.py reads an
// argv (_KUBECTL_FLAGS_WITH_VALUE, _KUBECTL_BOOLEAN_FLAGS, and local_render's sets).
var (
	kubectlFlagsWithValue = map[string]bool{
		"-n": true, "--namespace": true, "--context": true, "--cluster": true, "--kubeconfig": true,
		"-s": true, "--server": true, "--user": true, "--token": true, "--request-timeout": true,
		"--cache-dir": true, "--certificate-authority": true, "--client-certificate": true,
		"--client-key": true, "--tls-server-name": true, "--username": true, "--password": true,
		"--kuberc": true, "--profile": true, "--profile-output": true, "--log-flush-frequency": true,
		"-v": true, "--v": true, "--vmodule": true,
	}
	kubectlBooleanFlags = map[string]bool{
		"-h": true, "--help": true,
		"--insecure-skip-tls-verify": true, "--disable-compression": true,
		"--match-server-version": true, "--warnings-as-errors": true,
	}
	localRenderFlags     = map[string]bool{"--local": true, "--list": true, "-h": true, "--help": true}
	dryRunWriteValues    = map[string]bool{"none": true, "server": true, "false": true, "f": true, "0": true}
	booleanTrueSpellings = map[string]bool{"": true, "true": true, "t": true, "1": true}
)

const localRenderSubcommand = "view-last-applied"

// refusalBlock is one `policy rule:` block of a result.
type refusalBlock struct {
	rule   string
	verb   []string
	render bool
}

// verbIsMutating reports whether the verb the policy resolved changes state.
func verbIsMutating(words []string) bool {
	if len(words) == 0 {
		return false
	}
	if subs, ok := mutatingSubverbs[words[0]]; ok {
		return len(words) > 1 && subs[words[1]]
	}
	return mutatingVerbs[words[0]]
}

// refusalBlocks is every `policy rule:` block in result, in order.
func refusalBlocks(result string) []refusalBlock {
	rules := ruleLineRE.FindAllStringSubmatchIndex(result, -1)
	blocks := make([]refusalBlock, 0, len(rules))
	for i, m := range rules {
		end := len(result)
		if i+1 < len(rules) {
			end = rules[i+1][0]
		}
		block := result[m[0]:end]
		var verb []string
		if v := verbLineRE.FindStringSubmatch(block); v != nil {
			verb = pyFields(v[1])
		}
		blocks = append(blocks, refusalBlock{rule: result[m[2]:m[3]], verb: verb, render: renderLineRE.MatchString(block)})
	}
	return blocks
}

// definitiveBlocks is the verb words of each block that refused a write.
func definitiveBlocks(result string) [][]string {
	var out [][]string
	for _, b := range refusalBlocks(result) {
		if b.rule == definitiveRule && verbIsMutating(b.verb) && !b.render {
			out = append(out, b.verb)
		}
	}
	return out
}

// echoesRefusal reports whether the call's own text carries the result's
// refusal lines: a report being echoed, not a refusal.
func echoesRefusal(command, result string) bool {
	if command == "" {
		return false
	}
	var lines []string
	for _, words := range definitiveBlocks(result) {
		lines = append(lines, verbLinePrefix+strings.Join(words, " "))
	}
	if len(lines) == 0 {
		for _, b := range refusalBlocks(result) {
			if len(b.verb) > 0 {
				lines = append(lines, verbLinePrefix+strings.Join(b.verb, " "))
			} else {
				lines = append(lines, ruleLinePrefix+b.rule)
			}
		}
	}
	if len(lines) == 0 {
		return false
	}
	for _, line := range lines {
		if !strings.Contains(command, line) {
			return false
		}
	}
	return true
}

// classifyRefusal is refusalPolicy for a definitive refusal, refusalAdvisory
// or refusalAPI for one the task goes on past, and "" for anything else.
// failed is the call's failure flag; a call that exited 0 counts only when
// its own text runs a kubectl write and does not echo the result's lines.
func classifyRefusal(command, result string, failed bool) string {
	if result == "" {
		return ""
	}
	if policyActionRE.MatchString(result) {
		if !failed && echoesRefusal(command, result) {
			return ""
		}
		if len(definitiveBlocks(result)) > 0 && (failed || namesAWrite(command)) {
			return refusalPolicy
		}
		return refusalAdvisory
	}
	if apiDeniedRE.MatchString(result) {
		return refusalAPI
	}
	return ""
}

// namesAWrite reports whether the call's own text runs a kubectl write: some
// kubectl command in it whose first subcommand, as the policy reads it, is a
// mutating verb and that is not a local render or help read.
func namesAWrite(command string) bool {
	for _, verb := range kubectlVerbs(command) {
		if verbIsMutating(verb) {
			return true
		}
	}
	return false
}

// kubectlVerbs is the verb of each kubectl command in the text, read the way
// the policy reads an argv: each piece between separators is read from each
// of its kubectl words to the next, with quotes and list punctuation as
// spaces, and a newline inside square brackets is a space.
func kubectlVerbs(command string) [][]string {
	var verbs [][]string
	for _, piece := range separatorRE.Split(joinBracketedLines(command), -1) {
		starts := kubectlWordStarts(piece)
		for i, start := range starts {
			end := len(piece)
			if i+1 < len(starts) {
				end = starts[i+1]
			}
			argv := pyFields(listPunctRE.ReplaceAllString(piece[start:end], " "))
			if len(argv) == 0 {
				continue
			}
			if verb, _ := kubectlVerbAndFlag(argv); verb != nil && !localRender(argv) {
				verbs = append(verbs, verb)
			}
		}
	}
	return verbs
}

// kubectlWordStarts is the offset of each `kubectl` in piece that no word
// character or hyphen touches on either side.
func kubectlWordStarts(piece string) []int {
	const word = "kubectl"
	var starts []int
	for from := 0; ; {
		i := strings.Index(piece[from:], word)
		if i < 0 {
			return starts
		}
		at := from + i
		after := at + len(word)
		before, _ := utf8.DecodeLastRuneInString(piece[:at])
		next, _ := utf8.DecodeRuneInString(piece[after:])
		if (at == 0 || !isWordOrHyphen(before)) && (after == len(piece) || !isWordOrHyphen(next)) {
			starts = append(starts, at)
		}
		from = at + 1
	}
}

// isWordOrHyphen is Python's [\w-]: a letter, a number, `_` or `-`.
func isWordOrHyphen(r rune) bool {
	return r == '_' || r == '-' || unicode.IsLetter(r) || unicode.IsNumber(r)
}

// pyFields is str.split(): the words between runs of what str.isspace calls
// whitespace, which adds \x1c-\x1f to Go's unicode.IsSpace.
func pyFields(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool { return unicode.IsSpace(r) || (0x1c <= r && r <= 0x1f) })
}

// joinBracketedLines replaces each newline inside square brackets with a
// space, nesting counted.
func joinBracketedLines(text string) string {
	var b strings.Builder
	depth := 0
	for _, r := range text {
		switch {
		case r == '[':
			depth++
		case r == ']' && depth > 0:
			depth--
		}
		if r == '\n' && depth > 0 {
			r = ' '
		}
		b.WriteRune(r)
	}
	return b.String()
}

// kubectlVerbAndFlag is command_policy._kubectl_verb_and_flag: the verb
// sequence, or nil and the unknown global flag that hides it.
func kubectlVerbAndFlag(argv []string) ([]string, string) {
	index := 1
	var word1 string
	for ; index < len(argv); index++ {
		token := argv[index]
		if strings.HasPrefix(token, "-") {
			name, _, separated := strings.Cut(token, "=")
			if !kubectlFlagsWithValue[name] && !kubectlBooleanFlags[name] {
				return nil, name
			}
			if kubectlFlagsWithValue[name] && !separated {
				index++
			}
			continue
		}
		word1 = token
		index++
		break
	}
	if word1 == "" {
		return nil, ""
	}
	for index < len(argv) {
		token := argv[index]
		if strings.HasPrefix(token, "-") {
			name, _, separated := strings.Cut(token, "=")
			if !kubectlFlagsWithValue[name] && !kubectlBooleanFlags[name] {
				break
			}
			if kubectlFlagsWithValue[name] && !separated {
				index++
			}
			index++
			continue
		}
		return []string{word1, token}, ""
	}
	return []string{word1}, ""
}

// localRender is command_policy.local_render: whether a kubectl argv only
// renders or reads a manifest locally. The last --dry-run counts.
func localRender(argv []string) bool {
	own := argv
	for i, t := range argv {
		if t == "--" {
			own = argv[:i]
			break
		}
	}
	lastDryRun, sawDryRun := "", false
	for _, t := range own {
		if t == "--dry-run" || strings.HasPrefix(t, "--dry-run=") {
			lastDryRun, sawDryRun = t, true
		}
	}
	if sawDryRun {
		_, value, _ := strings.Cut(lastDryRun, "=")
		if !dryRunWriteValues[strings.ToLower(value)] {
			return true
		}
	}
	for _, t := range own {
		flag, value, _ := strings.Cut(t, "=")
		if localRenderFlags[flag] && booleanTrueSpellings[strings.ToLower(value)] {
			return true
		}
	}
	verb, _ := kubectlVerbAndFlag(argv)
	return len(verb) >= 2 && verb[0] == "apply" && verb[1] == localRenderSubcommand
}

// writeRefusal is the first definitive refusal a task met: the rule and the
// mutating verb it refused, and nothing else of the tool's output.
type writeRefusal struct {
	rule string
	verb string
}

// definitiveRefusal reads one tool result: the refusal that stops the task,
// or nil. The verb is cut to the words that make it mutating, so the
// terminal quotes only the policy's own vocabulary.
func definitiveRefusal(command, result string, failed bool) *writeRefusal {
	if classifyRefusal(command, result, failed) != refusalPolicy {
		return nil
	}
	blocks := definitiveBlocks(result)
	if len(blocks) == 0 {
		return nil
	}
	words := blocks[0]
	if _, sub := mutatingSubverbs[words[0]]; sub {
		words = words[:2]
	} else {
		words = words[:1]
	}
	return &writeRefusal{rule: definitiveRule, verb: strings.Join(words, " ")}
}

// toolCommandText is every string the call's input carries, in order, joined
// by newlines: the command the refusal predicate reads (permission_refusal_stop.command_text).
func toolCommandText(input json.RawMessage) string {
	dec := json.NewDecoder(bytes.NewReader(input))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return ""
	}
	var parts []string
	for dec.More() {
		if _, err := dec.Token(); err != nil { // the key
			return strings.Join(parts, "\n")
		}
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return strings.Join(parts, "\n")
		}
		var s string
		if json.Unmarshal(value, &s) == nil {
			parts = append(parts, s)
		}
	}
	return strings.Join(parts, "\n")
}

// toolResultText is a tool_result block's content: a string, or the text of
// each text block, joined by newlines.
func toolResultText(content json.RawMessage) string {
	var s string
	if json.Unmarshal(content, &s) == nil {
		return s
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(content, &blocks) != nil {
		return ""
	}
	var texts []string
	for _, b := range blocks {
		if b.Type == "text" {
			texts = append(texts, b.Text)
		}
	}
	return strings.Join(texts, "\n")
}

// writeRefusedReason is the terminal message of a task stopped at a write
// refusal, in the `reason: <token> - <detail>` shape.
func writeRefusedReason(r *writeRefusal) string {
	return "reason: write-refused" + reasonDetailSeparator +
		"the task stopped after a write refusal: the command policy's " + r.rule + " rule refused the verb " + r.verb +
		". The change was not made and was not retried another way; it can be proposed as a pull request."
}

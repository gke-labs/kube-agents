package memory

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"k8s.io/apimachinery/pkg/api/resource"
	"sigs.k8s.io/yaml"
)

const (
	millisPerUnit          = 1000
	defaultRecurrenceFloor = 2
)

var rePathSegment = regexp.MustCompile(`^([^\[]+)(?:\[([^=\]]+)=([^\]]+)\])?$`)

// GuardResult records the evaluation of one guard assertion against one file.
type GuardResult struct {
	File    string `json:"file"`
	Actual  any    `json:"actual"`
	OK      bool   `json:"ok"`
	Deleted bool   `json:"deleted,omitempty"`
}

// CheckFinding is one matched record returned by CheckChangedFiles.
type CheckFinding struct {
	ID           string        `json:"id"`
	Type         RecordType    `json:"type"`
	Status       RecordStatus  `json:"status"`
	Title        string        `json:"title"`
	Path         string        `json:"path"`
	Verdict      string        `json:"verdict"`
	Severity     string        `json:"severity"`
	Files        []string      `json:"files"`
	Guard        *RecordGuard  `json:"guard,omitempty"`
	GuardResults []GuardResult `json:"guard_results"`
	PullRequest  string        `json:"pull_request,omitempty"`
	BreakingPR   string        `json:"breaking_pr,omitempty"`
	Incident     string        `json:"incident,omitempty"`
	Supersedes   []string      `json:"supersedes,omitempty"`
	Body         string        `json:"body"`
}

// CheckReport is the complete result of evaluating records against a change set.
type CheckReport struct {
	Status   string         `json:"status"`
	Verdict  string         `json:"verdict"`
	Changed  []string       `json:"changed"`
	Findings []CheckFinding `json:"findings"`
	Markdown string         `json:"markdown"`
}

// RecurrenceCandidate describes >= 2 cluster lessons that share a check or guarded YAML path.
type RecurrenceCandidate struct {
	Key       string   `json:"key"`
	CheckID   string   `json:"check_id,omitempty"`
	FileGlob  string   `json:"file_glob,omitempty"`
	YAMLPath  string   `json:"yaml_path,omitempty"`
	Op        string   `json:"op,omitempty"`
	Value     any      `json:"value,omitempty"`
	Rule      string   `json:"rule,omitempty"`
	Lessons   []string `json:"lessons"`
	Clusters  []string `json:"clusters"`
	Paths     []string `json:"paths"`
	Resources []string `json:"resources,omitempty"`
}

type pathToken struct {
	Key      string
	SelField string
	SelValue string
}

func isValidOp(op string) bool {
	switch op {
	case "==", "!=", ">=", ">", "<=", "<", "absent", "present", "regex":
		return true
	}
	return false
}

// ManifestNamespacesIn extracts target Kubernetes namespaces from a YAML manifest or path convention.
func ManifestNamespacesIn(content, relPath string) []string {
	seen := map[string]bool{}
	docs, err := parseMultiYAMLDocs(content)
	if err == nil {
		for _, d := range docs {
			kind, _ := d["kind"].(string)
			md, _ := d["metadata"].(map[string]any)
			spec, _ := d["spec"].(map[string]any)
			if kind == "Namespace" && md != nil {
				if n, ok := md["name"].(string); ok && strings.TrimSpace(n) != "" {
					seen[strings.TrimSpace(n)] = true
				}
			} else if md != nil {
				if n, ok := md["namespace"].(string); ok && strings.TrimSpace(n) != "" {
					seen[strings.TrimSpace(n)] = true
				}
			}
			if kind == "Application" && spec != nil {
				if dest, ok := spec["destination"].(map[string]any); ok {
					if n, ok := dest["namespace"].(string); ok && strings.TrimSpace(n) != "" {
						seen[strings.TrimSpace(n)] = true
					}
				}
			} else if (kind == "Kustomization" || kind == "HelmRelease") && spec != nil {
				if n, ok := spec["targetNamespace"].(string); ok && strings.TrimSpace(n) != "" {
					seen[strings.TrimSpace(n)] = true
				}
			}
		}
	}
	parts := strings.Split(filepath.ToSlash(relPath), "/")
	for i := 0; i+1 < len(parts); i++ {
		if (parts[i] == "namespaces" || parts[i] == "apps") && parts[i+1] != "" &&
			!strings.HasSuffix(parts[i+1], ".yaml") && !strings.HasSuffix(parts[i+1], ".yml") {
			seen[parts[i+1]] = true
		}
	}
	var out []string
	for ns := range seen {
		out = append(out, ns)
	}
	sort.Strings(out)
	return out
}

// parsePathTokens splits a dot-separated YAML path, respecting escaped dots (\.)
// and list selectors like containers[name=checkout].
func parsePathTokens(rawPath string) ([]pathToken, error) {
	var parts []string
	var cur strings.Builder
	for i := 0; i < len(rawPath); i++ {
		if rawPath[i] == '\\' && i+1 < len(rawPath) && rawPath[i+1] == '.' {
			cur.WriteByte('.')
			i++
			continue
		}
		if rawPath[i] == '.' {
			parts = append(parts, cur.String())
			cur.Reset()
			continue
		}
		cur.WriteByte(rawPath[i])
	}
	parts = append(parts, cur.String())

	tokens := make([]pathToken, 0, len(parts))
	for _, p := range parts {
		m := rePathSegment.FindStringSubmatch(p)
		if m == nil {
			return nil, fmt.Errorf("bad guard path segment %q", p)
		}
		tokens = append(tokens, pathToken{
			Key:      m[1],
			SelField: m[2],
			SelValue: m[3],
		})
	}
	return tokens, nil
}

// YAMLGet walks a parsed YAML document map along rawPath.
func YAMLGet(doc any, rawPath string) any {
	tokens, err := parsePathTokens(rawPath)
	if err != nil {
		return nil
	}
	cur := doc
	for _, tok := range tokens {
		m, ok := cur.(map[string]any)
		if !ok {
			return nil
		}
		next, exists := m[tok.Key]
		if !exists {
			return nil
		}
		cur = next
		if tok.SelField != "" {
			list, ok := cur.([]any)
			if !ok {
				return nil
			}
			var matched any
			for _, item := range list {
				if im, ok := item.(map[string]any); ok {
					if fmt.Sprint(im[tok.SelField]) == tok.SelValue {
						matched = im
						break
					}
				}
			}
			if matched == nil {
				return nil
			}
			cur = matched
		}
	}
	return cur
}

// parseQuantityMillis converts a Kubernetes resource quantity or numeric value to milli-units.
func parseQuantityMillis(v any) (int64, bool) {
	if v == nil {
		return 0, false
	}
	switch n := v.(type) {
	case int:
		return int64(n) * millisPerUnit, true
	case int64:
		return n * millisPerUnit, true
	case float64:
		return int64(n * millisPerUnit), true
	}
	s := strings.TrimSpace(fmt.Sprint(v))
	if s == "" {
		return 0, false
	}
	if q, err := resource.ParseQuantity(s); err == nil {
		return q.MilliValue(), true
	}
	if f, err := strconv.ParseFloat(s, 64); err == nil {
		return int64(f * millisPerUnit), true
	}
	return 0, false
}

// CompareValue evaluates actual against expected using op.
func CompareValue(actual any, op string, expected any) bool {
	switch op {
	case "absent":
		return actual == nil
	case "present":
		return actual != nil
	}
	if actual == nil {
		return false
	}
	switch op {
	case "regex":
		pat := fmt.Sprint(expected)
		re, err := regexp.Compile("(?ms)" + pat)
		if err != nil {
			return false
		}
		return re.MatchString(fmt.Sprint(actual))
	case "==":
		if aq, aok := parseQuantityMillis(actual); aok {
			if eq, eok := parseQuantityMillis(expected); eok {
				return aq == eq
			}
		}
		return fmt.Sprint(actual) == fmt.Sprint(expected)
	case "!=":
		if aq, aok := parseQuantityMillis(actual); aok {
			if eq, eok := parseQuantityMillis(expected); eok {
				return aq != eq
			}
		}
		return fmt.Sprint(actual) != fmt.Sprint(expected)
	case ">=", ">", "<=", "<":
		aq, aok := parseQuantityMillis(actual)
		eq, eok := parseQuantityMillis(expected)
		if !aok || !eok {
			return false
		}
		switch op {
		case ">=":
			return aq >= eq
		case ">":
			return aq > eq
		case "<=":
			return aq <= eq
		case "<":
			return aq < eq
		}
	}
	return false
}

func matchSegments(patSegs, targetSegs []string) bool {
	for len(patSegs) > 0 {
		seg := patSegs[0]
		if seg == "**" {
			for len(patSegs) > 1 && patSegs[1] == "**" {
				patSegs = patSegs[1:]
			}
			if len(patSegs) == 1 {
				return true
			}
			for i := 0; i <= len(targetSegs); i++ {
				if matchSegments(patSegs[1:], targetSegs[i:]) {
					return true
				}
			}
			return false
		}
		if len(targetSegs) == 0 {
			return false
		}
		matched, err := path.Match(seg, targetSegs[0])
		if err != nil || !matched {
			return false
		}
		patSegs = patSegs[1:]
		targetSegs = targetSegs[1:]
	}
	return len(targetSegs) == 0
}

// MatchGlob matches a slash-separated path against a glob pattern, supporting "**".
func MatchGlob(pattern, target string) bool {
	pattern = strings.TrimPrefix(filepath.ToSlash(pattern), "./")
	target = strings.TrimPrefix(filepath.ToSlash(target), "./")
	if pattern == target || pattern == "**" {
		return true
	}
	if !strings.Contains(pattern, "/") && pattern == "*" {
		return !strings.Contains(target, "/")
	}
	if strings.Contains(pattern, "**") {
		return matchSegments(strings.Split(pattern, "/"), strings.Split(target, "/"))
	}
	matched, err := path.Match(pattern, target)
	return err == nil && matched
}

func splitYAMLDocuments(content string) []string {
	normalized := strings.ReplaceAll(content, "\r\n", "\n")
	lines := strings.Split(normalized, "\n")
	var chunks []string
	var cur []string
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "---" {
			if i > 0 && len(cur) > 0 {
				chunks = append(chunks, strings.Join(cur, "\n"))
				cur = nil
			}
			continue
		}
		if trimmed == "..." {
			if len(cur) > 0 {
				chunks = append(chunks, strings.Join(cur, "\n"))
				cur = nil
			}
			continue
		}
		cur = append(cur, line)
	}
	if len(cur) > 0 {
		chunks = append(chunks, strings.Join(cur, "\n"))
	}
	return chunks
}

func parseMultiYAMLDocs(content string) ([]map[string]any, error) {
	var docs []map[string]any
	for _, chunk := range splitYAMLDocuments(content) {
		if strings.TrimSpace(chunk) == "" {
			continue
		}
		var d map[string]any
		if err := yaml.Unmarshal([]byte(chunk), &d); err != nil {
			return nil, err
		}
		if len(d) > 0 {
			docs = append(docs, d)
		}
	}
	return docs, nil
}

func docResourceIDs(doc map[string]any) []string {
	kind, _ := doc["kind"].(string)
	md, _ := doc["metadata"].(map[string]any)
	if kind == "" || md == nil {
		return nil
	}
	ns, _ := md["namespace"].(string)
	name, _ := md["name"].(string)
	if name == "" {
		return nil
	}
	withNS := fmt.Sprintf("%s/%s/%s", kind, ns, name)
	if ns == "" {
		return []string{withNS, fmt.Sprintf("%s/%s", kind, name)}
	}
	return []string{withNS}
}

// EvaluateGuard evaluates a GuardExpect over a map of {filePath: content}.
func EvaluateGuard(exp GuardExpect, files map[string]string, scopeResources []string) []GuardResult {
	var results []GuardResult
	keys := make([]string, 0, len(files))
	for k := range files {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	for _, fpath := range keys {
		if !MatchGlob(exp.File, fpath) {
			continue
		}
		content := files[fpath]
		if exp.Path == "$text" {
			results = append(results, GuardResult{
				File:   fpath,
				Actual: "<text>",
				OK:     CompareValue(content, exp.Op, exp.Value),
			})
			continue
		}
		docs, err := parseMultiYAMLDocs(content)
		if err != nil {
			results = append(results, GuardResult{File: fpath, Actual: nil, OK: false})
			continue
		}
		if len(docs) == 0 {
			results = append(results, GuardResult{File: fpath, Actual: nil, OK: exp.Op == "absent"})
			continue
		}
		if len(docs) > 1 && len(scopeResources) > 0 {
			var scoped []map[string]any
			for _, d := range docs {
				matched := false
				for _, rid := range docResourceIDs(d) {
					for _, g := range scopeResources {
						if MatchGlob(g, rid) {
							matched = true
							break
						}
					}
					if matched {
						break
					}
				}
				if matched {
					scoped = append(scoped, d)
				}
			}
			if len(scoped) > 0 {
				docs = scoped
			}
		}
		// When a multi-document file mixes distinct Kinds (for example, Service + Deployment)
		// without explicit scope.resources, narrow only to Kinds that carry exp.Path while still
		// evaluating every document of those Kinds (so deleting the field from a second Deployment
		// in the same file is caught).
		if len(docs) > 1 && len(scopeResources) == 0 {
			matchingKinds := map[string]bool{}
			for _, d := range docs {
				if YAMLGet(d, exp.Path) != nil {
					if k, _ := d["kind"].(string); k != "" {
						matchingKinds[k] = true
					}
				}
			}
			if len(matchingKinds) > 0 {
				var kindFiltered []map[string]any
				for _, d := range docs {
					if k, _ := d["kind"].(string); matchingKinds[k] {
						kindFiltered = append(kindFiltered, d)
					}
				}
				if len(kindFiltered) > 0 {
					docs = kindFiltered
				}
			}
		}
		for _, doc := range docs {
			actual := YAMLGet(doc, exp.Path)
			if actual == nil && exp.Op != "absent" && exp.Op != "present" {
				results = append(results, GuardResult{File: fpath, Actual: nil, OK: false})
				continue
			}
			results = append(results, GuardResult{
				File:   fpath,
				Actual: actual,
				OK:     CompareValue(actual, exp.Op, exp.Value),
			})
		}
	}
	return results
}

// GuardPassesOnHead verifies that a candidate guard passes on the current worktree in repoDir.
func GuardPassesOnHead(repoDir string, g *RecordGuard, scopeResources []string) bool {
	if g == nil || g.Expect == nil {
		return false
	}
	exp := *g.Expect
	if exp.File == "" || exp.Path == "" || !isValidOp(exp.Op) {
		return false
	}
	root, err := os.OpenRoot(repoDir)
	if err != nil {
		return false
	}
	defer root.Close()

	matched := map[string]string{}
	_ = filepath.WalkDir(repoDir, func(p string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil
		}
		if d.IsDir() {
			if d.Name() == ".git" {
				return filepath.SkipDir
			}
			return nil
		}
		rel, relErr := filepath.Rel(repoDir, p)
		if relErr != nil || strings.HasPrefix(rel, "..") {
			return nil
		}
		slashRel := filepath.ToSlash(rel)
		if MatchGlob(exp.File, slashRel) {
			if raw, readErr := root.ReadFile(rel); readErr == nil {
				matched[slashRel] = string(raw)
			}
		}
		return nil
	})
	if len(matched) == 0 {
		return false
	}
	res := EvaluateGuard(exp, matched, scopeResources)
	if len(res) == 0 {
		return false
	}
	for _, r := range res {
		if !r.OK {
			return false
		}
	}
	return true
}

func isFleetWideClusterScope(clusters []string) bool {
	if len(clusters) == 0 {
		return true
	}
	return len(clusters) == 1 && clusters[0] == "*"
}

func targetFileMatchesNarrowCluster(targetFile string, clusters []string) bool {
	if isFleetWideClusterScope(clusters) {
		return true
	}
	segs := strings.Split(filepath.ToSlash(targetFile), "/")
	for _, c := range clusters {
		c = strings.TrimSpace(c)
		if c == "" || c == "*" {
			continue
		}
		for _, seg := range segs {
			if seg == c || strings.HasSuffix(c, "-"+seg) {
				return true
			}
		}
	}
	return false
}

type precedenceSuppression struct {
	wholeFile         map[string]map[string]bool
	excludedResources map[string]map[string][]string
}

func suppressedByPrecedence(records []Record, targetFiles []string) precedenceSuppression {
	type groupKey struct {
		file string
		path string
	}
	grouped := map[groupKey][]Record{}
	for _, rec := range records {
		if rec.Meta.Guard == nil || rec.Meta.Guard.Expect == nil {
			continue
		}
		exp := rec.Meta.Guard.Expect
		if exp.Path == "" {
			continue
		}
		for _, tf := range targetFiles {
			if !MatchGlob(exp.File, tf) {
				continue
			}
			if !isFleetWideClusterScope(rec.Meta.Scope.Clusters) && !targetFileMatchesNarrowCluster(tf, rec.Meta.Scope.Clusters) {
				continue
			}
			k := groupKey{file: tf, path: exp.Path}
			grouped[k] = append(grouped[k], rec)
		}
	}
	whole := map[string]map[string]bool{}
	excl := map[string]map[string][]string{}
	for k, grp := range grouped {
		if len(grp) <= 1 {
			continue
		}
		hasUnscopedActiveNarrow := false
		var scopedNarrowResources []string
		hasBroad := false
		for _, r := range grp {
			if isFleetWideClusterScope(r.Meta.Scope.Clusters) {
				hasBroad = true
			} else if r.Meta.Status == StatusActive {
				if len(r.Meta.Scope.Resources) == 0 {
					hasUnscopedActiveNarrow = true
				} else {
					scopedNarrowResources = append(scopedNarrowResources, r.Meta.Scope.Resources...)
				}
			}
		}
		if !hasBroad || (!hasUnscopedActiveNarrow && len(scopedNarrowResources) == 0) {
			continue
		}
		for _, r := range grp {
			if !isFleetWideClusterScope(r.Meta.Scope.Clusters) {
				continue
			}
			if hasUnscopedActiveNarrow {
				if whole[r.Meta.ID] == nil {
					whole[r.Meta.ID] = map[string]bool{}
				}
				whole[r.Meta.ID][k.file] = true
			} else {
				if excl[r.Meta.ID] == nil {
					excl[r.Meta.ID] = map[string][]string{}
				}
				excl[r.Meta.ID][k.file] = append(excl[r.Meta.ID][k.file], scopedNarrowResources...)
			}
		}
	}
	return precedenceSuppression{wholeFile: whole, excludedResources: excl}
}

func filterYAMLContentExcludingResources(content string, excludedGlobs []string) (filtered string, anyRemaining bool) {
	if len(excludedGlobs) == 0 {
		return content, true
	}
	docs, err := parseMultiYAMLDocs(content)
	if err != nil || len(docs) == 0 {
		return content, true
	}
	var kept []string
	for _, d := range docs {
		covered := false
		for _, rid := range docResourceIDs(d) {
			for _, g := range excludedGlobs {
				if MatchGlob(g, rid) {
					covered = true
					break
				}
			}
			if covered {
				break
			}
		}
		if !covered {
			if raw, mErr := yaml.Marshal(d); mErr == nil {
				kept = append(kept, strings.TrimSpace(string(raw)))
			}
		}
	}
	if len(kept) == 0 {
		return "", false
	}
	return strings.Join(kept, "\n---\n") + "\n", true
}

// CheckChangedFiles matches modified or deleted files against active and proposed records.
// Active records with failing block guards produce verdict="block" (exit code 2);
// proposed records produce advisory warnings ("warn") until approved.
func CheckChangedFiles(records []Record, changed map[string]*string) CheckReport {
	var candidates []Record
	for _, r := range records {
		if r.Meta.Status == StatusActive || r.Meta.Status == StatusProposed {
			candidates = append(candidates, r)
		}
	}
	changedPaths := make([]string, 0, len(changed))
	for p := range changed {
		changedPaths = append(changedPaths, p)
	}
	sort.Strings(changedPaths)

	supp := suppressedByPrecedence(candidates, changedPaths)
	fileResources := map[string][]string{}
	for p, content := range changed {
		if content == nil {
			continue
		}
		if docs, err := parseMultiYAMLDocs(*content); err == nil {
			for _, d := range docs {
				fileResources[p] = append(fileResources[p], docResourceIDs(d)...)
			}
		}
	}

	var findings []CheckFinding
	for _, rec := range candidates {
		hitSet := map[string]bool{}
		filteredContentByFile := map[string]string{}
		for _, p := range changedPaths {
			if supp.wholeFile[rec.Meta.ID][p] {
				continue
			}
			if exclGlobs := supp.excludedResources[rec.Meta.ID][p]; len(exclGlobs) > 0 && changed[p] != nil {
				filt, rem := filterYAMLContentExcludingResources(*changed[p], exclGlobs)
				if !rem {
					continue
				}
				filteredContentByFile[p] = filt
			}
			if !isFleetWideClusterScope(rec.Meta.Scope.Clusters) && len(rec.Meta.Scope.Paths) > 0 {
				// If a cluster-scoped record uses a wildcard file glob (such as clusters/**/app.yaml),
				// restrict it to files matching its declared cluster when the path sits under clusters/<name>/.
				if strings.HasPrefix(filepath.ToSlash(p), "clusters/") && !targetFileMatchesNarrowCluster(p, rec.Meta.Scope.Clusters) {
					continue
				}
			}
			for _, g := range rec.Meta.Scope.Paths {
				if MatchGlob(g, p) {
					hitSet[p] = true
				}
			}
			for _, rid := range fileResources[p] {
				for _, rg := range rec.Meta.Scope.Resources {
					if MatchGlob(rg, rid) {
						hitSet[p] = true
					}
				}
			}
		}
		if len(hitSet) == 0 {
			continue
		}
		var hits []string
		for p := range hitSet {
			hits = append(hits, p)
		}
		sort.Strings(hits)

		verdict := "related"
		var detail []GuardResult
		if rec.Meta.Guard != nil && rec.Meta.Guard.Expect != nil {
			exp := *rec.Meta.Guard.Expect
			liveFiles := map[string]string{}
			var deleted []string
			for _, p := range hits {
				if changed[p] != nil {
					if filt, ok := filteredContentByFile[p]; ok {
						liveFiles[p] = filt
					} else {
						liveFiles[p] = *changed[p]
					}
				} else if MatchGlob(exp.File, p) {
					deleted = append(deleted, p)
				}
			}
			detail = EvaluateGuard(exp, liveFiles, rec.Meta.Scope.Resources)
			anyFailed := len(deleted) > 0
			for _, d := range detail {
				if !d.OK {
					anyFailed = true
				}
			}
			for _, p := range deleted {
				detail = append(detail, GuardResult{File: p, Actual: nil, OK: false, Deleted: true})
			}
			if anyFailed {
				verdict = "violates"
			} else if len(detail) > 0 {
				verdict = "consistent"
			}
		}
		severity := "info"
		if verdict == "violates" {
			severity = "warn"
			if rec.Meta.Guard != nil && rec.Meta.Guard.Severity != "" {
				severity = rec.Meta.Guard.Severity
			}
			// A proposed record is always advisory (warn) until its PR merges and upgrades it to active.
			if rec.Meta.Status == StatusProposed && severity == "block" {
				severity = "warn"
			}
		}
		findings = append(findings, CheckFinding{
			ID:           rec.Meta.ID,
			Type:         rec.Meta.Type,
			Status:       rec.Meta.Status,
			Title:        rec.Meta.Title,
			Path:         rec.Path,
			Verdict:      verdict,
			Severity:     severity,
			Files:        hits,
			Guard:        rec.Meta.Guard,
			GuardResults: detail,
			PullRequest:  rec.Meta.Evidence.PullRequest,
			BreakingPR:   rec.Meta.Evidence.BreakingPR,
			Incident:     rec.Meta.Evidence.Incident,
			Supersedes:   rec.Meta.Supersedes,
			Body:         rec.Body,
		})
	}

	return BuildCheckReport(changedPaths, findings)
}

// BuildCheckReport sorts findings by severity and renders the final CheckReport.
func BuildCheckReport(changedPaths []string, findings []CheckFinding) CheckReport {
	sevOrder := map[string]int{"block": 0, "warn": 1, "info": 2}
	sort.Slice(findings, func(i, j int) bool {
		oi, oj := sevOrder[findings[i].Severity], sevOrder[findings[j].Severity]
		if oi != oj {
			return oi < oj
		}
		return findings[i].ID < findings[j].ID
	})

	overallVerdict := "ok"
	for _, f := range findings {
		if f.Severity == "block" {
			overallVerdict = "block"
			break
		}
		if f.Severity == "warn" {
			overallVerdict = "warn"
		}
	}
	status := "no_match"
	if len(findings) > 0 {
		status = "found"
	}
	return CheckReport{
		Status:   status,
		Verdict:  overallVerdict,
		Changed:  changedPaths,
		Findings: findings,
		Markdown: RenderCheckMarkdown(findings),
	}
}

// RenderCheckMarkdown formats CheckFindings into a PR-ready Markdown section.
func RenderCheckMarkdown(findings []CheckFinding) string {
	if len(findings) == 0 {
		return "No recorded decision or lesson governs the files this change touches."
	}
	var lines []string
	lines = append(lines, "### Prior decisions and lessons this change touches", "")
	for _, f := range findings {
		icon := "ℹ️"
		switch f.Verdict {
		case "violates":
			icon = "⛔"
		case "consistent":
			icon = "✅"
		}
		lines = append(lines, fmt.Sprintf("- %s **%s** (%s, status=%s): %s",
			icon, f.ID, f.Type, f.Status, f.Title))
		if f.Guard != nil && f.Guard.Rule != "" {
			lines = append(lines, fmt.Sprintf("  - Guard (%s): %s", f.Severity, f.Guard.Rule))
		}
		for _, g := range f.GuardResults {
			if !g.OK {
				if g.Deleted {
					lines = append(lines, fmt.Sprintf("  - `%s` deleted", g.File))
				} else {
					lines = append(lines, fmt.Sprintf("  - `%s` is now `%v`", g.File, g.Actual))
				}
			}
		}
	}
	return strings.Join(lines, "\n")
}

func generalizeClusterGlob(fileGlob string, clusters []string) string {
	slash := filepath.ToSlash(fileGlob)
	segs := strings.Split(slash, "/")
	if len(segs) >= 3 && segs[0] == "clusters" {
		for _, c := range clusters {
			if c != "" && (segs[1] == c || strings.HasSuffix(c, "-"+segs[1])) {
				segs[1] = "*"
				return strings.Join(segs, "/")
			}
		}
	}
	return slash
}

func workloadRecurrenceHint(r Record) string {
	if len(r.Meta.Scope.Checks) > 0 && r.Meta.Scope.Checks[0] != "" {
		return "check:" + r.Meta.Scope.Checks[0]
	}
	if len(r.Meta.Scope.Resources) > 0 && r.Meta.Scope.Resources[0] != "" {
		return "res:" + r.Meta.Scope.Resources[0]
	}
	if r.Meta.Guard != nil && r.Meta.Guard.Expect != nil && r.Meta.Guard.Expect.File != "" {
		gen := generalizeClusterGlob(r.Meta.Guard.Expect.File, r.Meta.Scope.Clusters)
		return "glob:" + gen
	}
	return ""
}

// DetectCrossClusterRecurrences finds LES-* records (active or verified-soak proposed)
// that share the same check ID or workload-scoped guarded YAML path across >= threshold distinct clusters.
func DetectCrossClusterRecurrences(records []Record, threshold int) []RecurrenceCandidate {
	if threshold < defaultRecurrenceFloor {
		threshold = defaultRecurrenceFloor
	}
	coveredLessons := map[string]bool{}
	for _, r := range records {
		if r.Meta.Type == RecordTypeDecision && (r.Meta.Status == StatusProposed || r.Meta.Status == StatusActive) {
			for _, sup := range r.Meta.Supersedes {
				coveredLessons[sup] = true
			}
		}
	}
	byKey := map[string][]Record{}
	for _, r := range records {
		if r.Meta.Type != RecordTypeLesson || coveredLessons[r.Meta.ID] {
			continue
		}
		if r.Meta.Status != StatusActive && !(r.Meta.Status == StatusProposed && r.Meta.Evidence.Outcome == string(OutcomeResolvedMerged)) {
			continue
		}
		if len(r.Meta.Scope.Clusters) == 0 || isFleetWideClusterScope(r.Meta.Scope.Clusters) {
			continue
		}
		var key string
		hint := workloadRecurrenceHint(r)
		if r.Meta.Guard != nil && r.Meta.Guard.Expect != nil && r.Meta.Guard.Expect.Path != "" {
			key = fmt.Sprintf("field:%s:%s:%s", hint, r.Meta.Guard.Expect.Path, r.Meta.Guard.Expect.Op)
		} else if len(r.Meta.Scope.Checks) > 0 {
			key = "check:" + r.Meta.Scope.Checks[0]
		}
		if key != "" {
			byKey[key] = append(byKey[key], r)
		}
	}

	keys := make([]string, 0, len(byKey))
	for k := range byKey {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var out []RecurrenceCandidate
	for _, k := range keys {
		recs := byKey[k]
		clusterSet := map[string]bool{}
		pathSet := map[string]bool{}
		resSet := map[string]bool{}
		var lessonIDs []string
		var checkID, fileGlob, yamlPath, op, rule string
		var val any
		for _, r := range recs {
			lessonIDs = append(lessonIDs, r.Meta.ID)
			for _, c := range r.Meta.Scope.Clusters {
				if c != "" && c != "*" {
					clusterSet[c] = true
				}
			}
			for _, p := range r.Meta.Scope.Paths {
				pathSet[generalizeClusterGlob(p, r.Meta.Scope.Clusters)] = true
			}
			for _, res := range r.Meta.Scope.Resources {
				resSet[res] = true
			}
			if checkID == "" && len(r.Meta.Scope.Checks) > 0 {
				checkID = r.Meta.Scope.Checks[0]
			}
			if r.Meta.Guard != nil {
				if rule == "" {
					rule = r.Meta.Guard.Rule
				}
				if r.Meta.Guard.Expect != nil && yamlPath == "" {
					fileGlob = generalizeClusterGlob(r.Meta.Guard.Expect.File, r.Meta.Scope.Clusters)
					yamlPath = r.Meta.Guard.Expect.Path
					op = r.Meta.Guard.Expect.Op
					val = r.Meta.Guard.Expect.Value
				}
			}
		}
		if len(clusterSet) < threshold {
			continue
		}
		var clusters, paths, resources []string
		for c := range clusterSet {
			clusters = append(clusters, c)
		}
		for p := range pathSet {
			paths = append(paths, p)
		}
		for res := range resSet {
			resources = append(resources, res)
		}
		sort.Strings(clusters)
		sort.Strings(paths)
		sort.Strings(resources)
		sort.Strings(lessonIDs)
		out = append(out, RecurrenceCandidate{
			Key:       k,
			CheckID:   checkID,
			FileGlob:  fileGlob,
			YAMLPath:  yamlPath,
			Op:        op,
			Value:     val,
			Rule:      rule,
			Lessons:   lessonIDs,
			Clusters:  clusters,
			Paths:     paths,
			Resources: resources,
		})
	}
	return out
}

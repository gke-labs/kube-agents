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

	yamlv3 "go.yaml.in/yaml/v3"
	"k8s.io/apimachinery/pkg/api/resource"
)

// GuardResult records the evaluation of one guard assertion against one file.
type GuardResult struct {
	File    string `json:"file"`
	Actual  any    `json:"actual"`
	OK      bool   `json:"ok"`
	Deleted bool   `json:"deleted,omitempty"`
}

// CheckFinding is one matched record returned by CheckChangedFiles.
type CheckFinding struct {
	ID            string        `json:"id"`
	Type          RecordType    `json:"type"`
	Status        RecordStatus  `json:"status"`
	Title         string        `json:"title"`
	Path          string        `json:"path"`
	Verdict       string        `json:"verdict"`
	Severity      string        `json:"severity"`
	Files         []string      `json:"files"`
	Guard         *RecordGuard  `json:"guard,omitempty"`
	GuardResults  []GuardResult `json:"guard_results"`
	PullRequest   string        `json:"pull_request,omitempty"`
	BreakingPR    string        `json:"breaking_pr,omitempty"`
	Incident      string        `json:"incident,omitempty"`
	Supersedes    []string      `json:"supersedes,omitempty"`
	SupersededIDs []string      `json:"superseded_ids,omitempty"`
	Body          string        `json:"body"`
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

var rePathSegment = regexp.MustCompile(`^([^\[]+)(?:\[([^=\]]+)=([^\]]+)\])?$`)

func isValidOp(op string) bool {
	switch op {
	case "==", "!=", ">=", ">", "<=", "<", "absent", "present", "regex":
		return true
	}
	return false
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
		return int64(n) * 1000, true
	case int64:
		return n * 1000, true
	case float64:
		return int64(n * 1000), true
	}
	s := strings.TrimSpace(fmt.Sprint(v))
	if s == "" {
		return 0, false
	}
	if q, err := resource.ParseQuantity(s); err == nil {
		return q.MilliValue(), true
	}
	if f, err := strconv.ParseFloat(s, 64); err == nil {
		return int64(f * 1000), true
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

// MatchGlob matches a slash-separated path against a glob pattern, supporting "**".
func MatchGlob(pattern, target string) bool {
	pattern = filepath.ToSlash(pattern)
	target = filepath.ToSlash(target)
	if pattern == target || pattern == "**" || pattern == "*" {
		return true
	}
	if strings.Contains(pattern, "**") {
		parts := strings.Split(pattern, "**")
		prefix := strings.TrimSuffix(parts[0], "/")
		suffix := strings.TrimPrefix(parts[len(parts)-1], "/")
		if prefix != "" && !strings.HasPrefix(target, prefix+"/") && target != prefix {
			return false
		}
		if suffix != "" {
			if matched, _ := path.Match(suffix, path.Base(target)); !matched && !strings.HasSuffix(target, suffix) {
				return false
			}
		}
		return true
	}
	matched, err := path.Match(pattern, target)
	return err == nil && matched
}

func parseMultiYAMLDocs(content string) ([]map[string]any, error) {
	dec := yamlv3.NewDecoder(strings.NewReader(content))
	var docs []map[string]any
	for {
		var d map[string]any
		err := dec.Decode(&d)
		if err != nil {
			if err.Error() == "EOF" {
				break
			}
			return nil, err
		}
		if len(d) > 0 {
			docs = append(docs, d)
		}
	}
	return docs, nil
}

func docResourceID(doc map[string]any) string {
	kind, _ := doc["kind"].(string)
	md, _ := doc["metadata"].(map[string]any)
	if kind == "" || md == nil {
		return ""
	}
	ns, _ := md["namespace"].(string)
	name, _ := md["name"].(string)
	return fmt.Sprintf("%s/%s/%s", kind, ns, name)
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
				rid := docResourceID(d)
				if rid == "" {
					continue
				}
				for _, g := range scopeResources {
					if MatchGlob(g, rid) {
						scoped = append(scoped, d)
						break
					}
				}
			}
			if len(scoped) > 0 {
				docs = scoped
			}
		}
		if len(docs) > 1 {
			var nonNull []map[string]any
			for _, d := range docs {
				if YAMLGet(d, exp.Path) != nil {
					nonNull = append(nonNull, d)
				}
			}
			if len(nonNull) > 0 {
				docs = nonNull
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
	matched := map[string]string{}
	_ = filepath.WalkDir(repoDir, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if d.Name() == ".git" {
				return filepath.SkipDir
			}
			return nil
		}
		rel, err := filepath.Rel(repoDir, p)
		if err != nil {
			return nil
		}
		rel = filepath.ToSlash(rel)
		if MatchGlob(exp.File, rel) {
			if raw, err := os.ReadFile(p); err == nil {
				matched[rel] = string(raw)
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

func selectPrecedenceWinners(records []Record, targetFiles []string) []Record {
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
		for _, tf := range targetFiles {
			if MatchGlob(exp.File, tf) {
				k := groupKey{file: tf, path: exp.Path}
				grouped[k] = append(grouped[k], rec)
			}
		}
	}
	suppressed := map[string]bool{}
	for _, grp := range grouped {
		if len(grp) <= 1 {
			continue
		}
		hasNarrow := false
		hasBroad := false
		for _, r := range grp {
			if isFleetWideClusterScope(r.Meta.Scope.Clusters) {
				hasBroad = true
			} else {
				hasNarrow = true
			}
		}
		if hasNarrow && hasBroad {
			for _, r := range grp {
				if isFleetWideClusterScope(r.Meta.Scope.Clusters) {
					suppressed[r.Meta.ID] = true
				}
			}
		}
	}
	var out []Record
	for _, r := range records {
		if !suppressed[r.Meta.ID] {
			out = append(out, r)
		}
	}
	return out
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

	effective := selectPrecedenceWinners(candidates, changedPaths)
	fileResources := map[string][]string{}
	for p, content := range changed {
		if content == nil {
			continue
		}
		if docs, err := parseMultiYAMLDocs(*content); err == nil {
			for _, d := range docs {
				if rid := docResourceID(d); rid != "" {
					fileResources[p] = append(fileResources[p], rid)
				}
			}
		}
	}

	var findings []CheckFinding
	for _, rec := range effective {
		hitSet := map[string]bool{}
		for _, p := range changedPaths {
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
					liveFiles[p] = *changed[p]
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
			ID:            rec.Meta.ID,
			Type:          rec.Meta.Type,
			Status:        rec.Meta.Status,
			Title:         rec.Meta.Title,
			Path:          rec.Path,
			Verdict:       verdict,
			Severity:      severity,
			Files:         hits,
			Guard:         rec.Meta.Guard,
			GuardResults:  detail,
			PullRequest:   rec.Meta.Evidence.PullRequest,
			BreakingPR:    rec.Meta.Evidence.BreakingPR,
			Incident:      rec.Meta.Evidence.Incident,
			Supersedes:    rec.Meta.Supersedes,
			SupersededIDs: rec.Meta.Supersedes,
			Body:          rec.Body,
		})
	}

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
		Markdown: renderCheckMarkdown(findings),
	}
}

func renderCheckMarkdown(findings []CheckFinding) string {
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

// DetectCrossClusterRecurrences finds active LES-* records that share the same
// check ID or guarded YAML path across >= threshold distinct clusters.
func DetectCrossClusterRecurrences(records []Record, threshold int) []RecurrenceCandidate {
	if threshold < 2 {
		threshold = 2
	}
	byKey := map[string][]Record{}
	for _, r := range records {
		if r.Meta.Type != RecordTypeLesson || r.Meta.Status != StatusActive {
			continue
		}
		if len(r.Meta.Scope.Clusters) == 0 || isFleetWideClusterScope(r.Meta.Scope.Clusters) {
			continue
		}
		var key string
		if r.Meta.Guard != nil && r.Meta.Guard.Expect != nil && r.Meta.Guard.Expect.Path != "" {
			key = fmt.Sprintf("field:%s:%s", r.Meta.Guard.Expect.Path, r.Meta.Guard.Expect.Op)
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
				pathSet[p] = true
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
					fileGlob = r.Meta.Guard.Expect.File
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

package persona

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"

	"sigs.k8s.io/yaml"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// maxDescription is the Agent Skills limit on a skill's description,
	// applied to the manifest's too.
	maxDescription = 1024
	// maxFileBytes and maxTotalBytes bound what Load reads into memory. A
	// persona is prose and a few scripts; anything near these is a mistake.
	maxFileBytes  = 1 << 20
	maxTotalBytes = 16 << 20
)

// skillFrontmatterKeys are the SKILL.md frontmatter keys Load accepts.
var skillFrontmatterKeys = []string{"name", "description", "license", "metadata"}

var (
	// nameRE is a DNS label: a persona's name ends up in image and object names.
	nameRE = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`)
	// skillNameRE is the Agent Skills name rule.
	skillNameRE = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,62}[a-z0-9])?$`)
	// serverRE keeps "__" out of server names: Claude Code spells an MCP
	// tool mcp__<server>__<tool>.
	serverRE  = regexp.MustCompile(`^[a-z0-9]([-a-z0-9_]{0,30}[a-z0-9])?$`)
	mcpToolRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	// paramRE is lib's, so a name a persona declares is one a submission
	// can carry (lib.TaskParameters).
	paramRE   = lib.TaskParameterNameRE
	harnessRE = regexp.MustCompile(`^[a-z][a-z0-9-]{0,30}$`)
	commandRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`)
	// importRE is anything a Claude Code @-import could be: an "@" anywhere,
	// through the next ASCII whitespace. Broader than the harness's own rule
	// on purpose (see importEscapes).
	importRE = regexp.MustCompile(`@(\S+)`)
	// wordImportRE is an "@" that starts a word: at the start of a line or
	// after anything but an ASCII letter or digit. The harness reads only
	// these as imports; "oncall@example.com" is not one.
	wordImportRE = regexp.MustCompile(`(?:^|[^A-Za-z0-9])@(\S+)`)
)

// Load reads and validates the persona directory dir. A validation failure
// is an *InvalidError listing every problem found; any other error is I/O.
func Load(dir string) (*Persona, error) {
	l := &loader{dir: dir}
	p := l.load()
	if l.err != nil {
		return nil, l.err
	}
	if len(l.problems) > 0 {
		return nil, &InvalidError{Dir: dir, Problems: l.problems}
	}
	return p, nil
}

type loader struct {
	dir      string
	imports  []pendingImport
	problems []Problem
	err      error
	total    int
}

func (l *loader) add(file, field, format string, args ...any) {
	l.problems = append(l.problems, Problem{File: file, Field: field, Msg: fmt.Sprintf(format, args...)})
}

func (l *loader) load() *Persona {
	entries, err := os.ReadDir(l.dir)
	if err != nil {
		l.err = err
		return nil
	}
	known := map[string]bool{ManifestFile: true, InstructionsFile: true, SkillsDir: true, MCPFile: true, ReadmeFile: true}
	for _, e := range entries {
		// README.md is never read, so its symlink check is here; the files
		// that are read get theirs at the open.
		if known[e.Name()] && e.Name() == ReadmeFile && e.Type()&fs.ModeSymlink != 0 {
			l.add(e.Name(), "", "is not a regular file (symlinks and devices are refused)")
		}
		if !known[e.Name()] {
			l.add(e.Name(), "", "not part of the persona format (%s, %s, %s/, %s or %s)",
				ManifestFile, InstructionsFile, SkillsDir, MCPFile, ReadmeFile)
		}
	}
	p := &Persona{}
	if data, ok := l.read(ManifestFile, true); ok {
		if err := yaml.UnmarshalStrict(data, &p.Manifest); err != nil {
			l.add(ManifestFile, "", "%v", err)
		}
	}
	if data, ok := l.read(MCPFile, false); ok {
		p.MCP = l.mcp(data)
	}
	if data, ok := l.read(InstructionsFile, true); ok {
		if len(bytes.TrimSpace(data)) == 0 {
			l.add(InstructionsFile, "", "is empty")
		}
		l.checkImports(InstructionsFile, data)
		p.Instructions = string(data)
	}
	p.Skills = l.skills()
	if l.err != nil {
		return nil
	}
	l.checkImportTargets(p)
	l.validateMCP(p.MCP)
	l.validateManifest(&p.Manifest, p.MCP)
	return p
}

// errSymlink is what openNoFollow returns for a symlink.
var errSymlink = errors.New("symlink")

// read returns a top-level file's content. A missing optional file is
// (nil, false) with no problem.
func (l *loader) read(name string, required bool) ([]byte, bool) {
	data, ok, missing := l.readFile(name, filepath.Join(l.dir, name))
	if missing && required {
		l.add(name, "", "is missing")
	}
	return data, ok
}

// readFile reads one file, refusing anything but a regular file: a symlink
// could name a file outside the persona, and the image that carries the
// persona would not carry its target. It opens without following a link and
// checks the open file, so nothing can be swapped in between.
func (l *loader) readFile(rel, full string) (data []byte, ok, missing bool) {
	f, err := openNoFollow(full)
	switch {
	case errors.Is(err, errSymlink):
		l.add(rel, "", "is not a regular file (symlinks and devices are refused)")
		return nil, false, false
	case errors.Is(err, fs.ErrNotExist):
		return nil, false, true
	case err != nil:
		l.err = err
		return nil, false, false
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		l.err = err
		return nil, false, false
	}
	if !info.Mode().IsRegular() {
		l.add(rel, "", "is not a regular file (symlinks and devices are refused)")
		return nil, false, false
	}
	if info.Size() > maxFileBytes {
		l.add(rel, "", "is %d bytes; the limit is %d", info.Size(), maxFileBytes)
		return nil, false, false
	}
	data, err = io.ReadAll(io.LimitReader(f, maxFileBytes+1))
	if err != nil {
		l.err = err
		return nil, false, false
	}
	if len(data) > maxFileBytes {
		l.add(rel, "", "is over %d bytes", maxFileBytes)
		return nil, false, false
	}
	l.total += len(data)
	if l.total > maxTotalBytes {
		l.add(rel, "", "the persona passes %d bytes in total", maxTotalBytes)
		return nil, false, false
	}
	return data, true, false
}

func (l *loader) skills() []Skill {
	root := filepath.Join(l.dir, SkillsDir)
	info, err := os.Lstat(root)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		l.err = err
		return nil
	}
	if !info.IsDir() {
		l.add(SkillsDir, "", "is not a directory")
		return nil
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		l.err = err
		return nil
	}
	var skills []Skill
	for _, e := range entries {
		rel := path.Join(SkillsDir, e.Name())
		if e.Type()&fs.ModeSymlink != 0 {
			l.add(rel, "", "is a symlink; a skill is a directory inside the persona")
			continue
		}
		if !e.IsDir() {
			l.add(rel, "", "a skill is a directory holding %s", SkillFile)
			continue
		}
		if s, ok := l.skill(rel, e.Name()); ok {
			skills = append(skills, s)
		}
	}
	return skills
}

func (l *loader) skill(rel, name string) (Skill, bool) {
	s := Skill{Name: name}
	if !skillNameRE.MatchString(name) {
		l.add(rel, "", "skill directory name %q is not lowercase letters, digits and hyphens", name)
	}
	err := filepath.WalkDir(filepath.Join(l.dir, rel), func(full string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		fileRel, _ := filepath.Rel(filepath.Join(l.dir, rel), full)
		fileRel = filepath.ToSlash(fileRel)
		// The adapter writes the file under this path, and the output
		// check refuses one LocalPath does (a backslash in a name).
		if !LocalPath(fileRel) {
			l.add(path.Join(rel, fileRel), "", "is not a plain relative path (a backslash in a name?)")
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		data, ok, _ := l.readFile(path.Join(rel, fileRel), full)
		if ok {
			if IsMarkdown(fileRel) {
				l.checkImports(path.Join(rel, fileRel), data)
			}
			s.Files = append(s.Files, File{Path: fileRel, Data: data, Executable: info.Mode()&0o111 != 0})
		}
		return nil
	})
	if err != nil {
		l.err = err
		return s, false
	}
	sort.Slice(s.Files, func(i, j int) bool { return s.Files[i].Path < s.Files[j].Path })
	i := slices.IndexFunc(s.Files, func(f File) bool { return f.Path == SkillFile })
	if i < 0 {
		l.add(rel, "", "has no %s", SkillFile)
		return s, false
	}
	front, ok := frontmatter(s.Files[i].Data)
	file := path.Join(rel, SkillFile)
	if !ok {
		l.add(file, "", "does not open with --- frontmatter")
		return s, false
	}
	var meta map[string]any
	if err := yaml.Unmarshal(front, &meta); err != nil {
		l.add(file, "frontmatter", "%v", err)
		return s, false
	}
	// The file ships verbatim, and Claude Code reads keys such as
	// allowed-tools and hooks from it: a skill could grant what the persona
	// withholds. Only inert keys pass.
	for _, key := range sortedKeys(meta) {
		if !slices.Contains(skillFrontmatterKeys, key) {
			l.add(file, key, "is not an allowed frontmatter key (%s); a skill cannot grant tools or run hooks",
				strings.Join(skillFrontmatterKeys, ", "))
		}
	}
	if m, ok := meta["metadata"]; ok {
		if _, isMap := m.(map[string]any); !isMap {
			l.add(file, "metadata", "must be a map")
		}
	}
	skillName, _ := meta["name"].(string)
	desc, _ := meta["description"].(string)
	if skillName != name {
		l.add(file, "name", "is %q; it must be the directory name %q", skillName, name)
	}
	switch {
	case strings.TrimSpace(desc) == "":
		l.add(file, "description", "is required")
	case len(desc) > maxDescription:
		l.add(file, "description", "is %d bytes; the limit is %d", len(desc), maxDescription)
	}
	s.Description = desc
	return s, true
}

// pendingImport is an @-import seen while reading, checked against the
// persona's files once they are all read.
type pendingImport struct {
	rel, target string
	line        int
}

// checkImports refuses @-imports that could leave the config directory, and
// records each word-initial one for checkImportTargets. Claude Code expands
// "@path" in CLAUDE.md and its imports, so such a line would read a file
// from the pod into the prompt. Fenced code blocks are checked too:
// mirroring the harness's fence rule is a parser to keep in step, and
// refusing an example costs only a rewording.
func (l *loader) checkImports(rel string, data []byte) {
	for i, line := range strings.Split(string(data), "\n") {
		for _, m := range importRE.FindAllStringSubmatch(line, -1) {
			if importEscapes(m[1]) {
				l.add(rel, fmt.Sprintf("line %d", i+1),
					"the import %q could read a file outside the persona; an import names a path inside it, with no \"..\", \"\\\\\", leading \"/\" or \"~\", or non-ASCII", "@"+m[1])
			}
		}
		for _, m := range wordImportRE.FindAllStringSubmatch(line, -1) {
			if !importEscapes(m[1]) {
				l.imports = append(l.imports, pendingImport{rel: rel, target: m[1], line: i + 1})
			}
		}
	}
}

// checkImportTargets refuses an import that names no file the persona ships.
// Staying inside the persona's own directory is not enough: the adapter
// writes the instructions into the harness's config directory, beside files
// the persona does not own (settings.json, .credentials.json). A target is
// resolved as the harness would, in the rendered layout: the instructions
// become CLAUDE.md at the root, and a skill's files keep their paths.
func (l *loader) checkImportTargets(p *Persona) {
	shipped := map[string]bool{renderedInstructions: true}
	for _, s := range p.Skills {
		for _, f := range s.Files {
			shipped[path.Join(SkillsDir, s.Name, f.Path)] = true
		}
	}
	for _, imp := range l.imports {
		if !shipped[path.Clean(path.Join(path.Dir(imp.rel), imp.target))] {
			l.add(imp.rel, fmt.Sprintf("line %d", imp.line),
				"the import %q names no file the persona ships; an import names %s or a file under %s/", "@"+imp.target, renderedInstructions, SkillsDir)
		}
	}
}

// renderedInstructions is the name instructions.md is written under in the
// config directory, which is where an import from it resolves.
const renderedInstructions = "CLAUDE.md"

// importEscapes fails closed rather than resolve the path the way the
// harness does: the harness's token rules (Unicode spaces, backslash-escaped
// spaces) are not ours to mirror, so any target that could climb, start at a
// root, or hide a separator is refused.
func importEscapes(target string) bool {
	if strings.HasPrefix(target, "/") || strings.HasPrefix(target, "~") ||
		strings.Contains(target, "..") || strings.Contains(target, "\\") {
		return true
	}
	for _, r := range target {
		if r < 0x21 || r > 0x7e {
			return true
		}
	}
	return false
}

// IsMarkdown reports whether a path names a markdown file, in any case.
func IsMarkdown(p string) bool { return strings.EqualFold(path.Ext(p), ".md") }

// frontmatter returns the YAML between a leading "---" line and the next.
func frontmatter(data []byte) ([]byte, bool) {
	text := strings.ReplaceAll(string(data), "\r\n", "\n")
	if !strings.HasPrefix(text, "---\n") {
		return nil, false
	}
	rest := text[len("---\n"):]
	end := strings.Index(rest, "\n---")
	if end < 0 {
		return nil, false
	}
	return []byte(rest[:end]), true
}

// mcp decodes mcp.yaml strictly: an unknown key, at any level, or a
// repeated one is refused. A server with a url gets its own problem rather
// than the decoder's "unknown field": v1 reaches MCP servers only through the
// broker, whose policy and audit a direct URL would bypass.
func (l *loader) mcp(data []byte) MCP {
	var urls []string
	var raw struct {
		Servers map[string]map[string]any `json:"servers"`
	}
	if yaml.Unmarshal(data, &raw) == nil {
		for _, name := range sortedKeys(raw.Servers) {
			if _, ok := raw.Servers[name]["url"]; ok {
				urls = append(urls, name)
				l.add(MCPFile, "servers."+name+".url",
					"direct URLs are not supported; a server names a broker MCP server (broker: <name>)")
			}
		}
	}
	var m MCP
	if err := yaml.UnmarshalStrict(data, &m); err != nil {
		// The decoder stops at the first unknown field; when that is a url
		// already reported above, decode again without it to find the rest.
		if len(urls) == 0 || !strings.Contains(err.Error(), `unknown field "url"`) {
			l.add(MCPFile, "", "%v", err)
			return m
		}
		for _, name := range urls {
			delete(raw.Servers[name], "url")
		}
		cleaned, _ := yaml.Marshal(raw)
		m = MCP{}
		if err := yaml.UnmarshalStrict(cleaned, &m); err != nil {
			l.add(MCPFile, "", "%v", err)
		}
	}
	return m
}

func (l *loader) validateMCP(m MCP) {
	for _, name := range sortedKeys(m.Servers) {
		s := m.Servers[name]
		field := "servers." + name
		if !serverRE.MatchString(name) {
			l.add(MCPFile, field, "server name is not lowercase letters, digits, '-' and single '_'")
		} else if strings.Contains(name, "__") {
			l.add(MCPFile, field, "server name must not contain \"__\"")
		} else if slices.Contains(BuiltinTools, name) {
			l.add(MCPFile, field, "server name is a built-in tool name (%s); a tools entry could not tell them apart", strings.Join(BuiltinTools, ", "))
		}
		switch {
		case s.Broker == "":
			l.add(MCPFile, field+".broker", "is required: a server names a broker MCP server")
		case !serverRE.MatchString(s.Broker):
			l.add(MCPFile, field+".broker", "%q is not a broker MCP server name", s.Broker)
		}
		seen := map[string]bool{}
		for i, t := range s.Include {
			f := fmt.Sprintf("%s.include[%d]", field, i)
			switch {
			case !mcpToolRE.MatchString(t):
				l.add(MCPFile, f, "%q is not an MCP tool name", t)
			case seen[t]:
				l.add(MCPFile, f, "%q repeats", t)
			}
			seen[t] = true
		}
	}
}

func (l *loader) validateManifest(m *Manifest, mcp MCP) {
	if m.APIVersion != APIVersion {
		l.add(ManifestFile, "apiVersion", "is %q; the supported version is %q", m.APIVersion, APIVersion)
	}
	if !nameRE.MatchString(m.Name) {
		l.add(ManifestFile, "name", "%q is not a DNS label", m.Name)
	}
	switch {
	case strings.TrimSpace(m.Description) == "":
		l.add(ManifestFile, "description", "is required")
	case len(m.Description) > maxDescription:
		l.add(ManifestFile, "description", "is %d bytes; the limit is %d", len(m.Description), maxDescription)
	}
	seen := map[string]bool{}
	for i, t := range m.Tools {
		field := fmt.Sprintf("tools[%d]", i)
		if seen[t] {
			l.add(ManifestFile, field, "%q repeats", t)
		}
		seen[t] = true
		if msg := checkTool(t, mcp); msg != "" {
			l.add(ManifestFile, field, "%s", msg)
		}
	}
	params := map[string]bool{}
	for i, p := range m.Parameters {
		field := fmt.Sprintf("parameters[%d]", i)
		switch {
		case !paramRE.MatchString(p.Name):
			l.add(ManifestFile, field+".name", "%q is not lowercase letters, digits and '_'", p.Name)
		case params[p.Name]:
			l.add(ManifestFile, field+".name", "%q repeats", p.Name)
		}
		params[p.Name] = true
	}
	for i, c := range m.Requires.Commands {
		if !commandRE.MatchString(c) {
			l.add(ManifestFile, fmt.Sprintf("requires.commands[%d]", i), "%q is not a command name", c)
		}
	}
	for _, h := range sortedKeys(m.Harness) {
		if !harnessRE.MatchString(h) {
			l.add(ManifestFile, "harness."+h, "is not a harness name")
		}
	}
}

// checkTool says what is wrong with one tools entry, or "".
func checkTool(t string, mcp MCP) string {
	if slices.Contains(BuiltinTools, t) {
		return ""
	}
	server, tool, scoped := strings.Cut(t, ".")
	s, ok := mcp.Servers[server]
	if !ok {
		return fmt.Sprintf("%q is neither a built-in tool (%s) nor a server in %s",
			t, strings.Join(BuiltinTools, ", "), MCPFile)
	}
	if scoped && len(s.Include) > 0 && !slices.Contains(s.Include, tool) {
		return fmt.Sprintf("%q names a tool outside servers.%s.include", t, server)
	}
	if scoped && !mcpToolRE.MatchString(tool) {
		return fmt.Sprintf("%q is not an MCP tool name", tool)
	}
	return ""
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// SortedServers returns the names of p's MCP servers in order.
func (p *Persona) SortedServers() []string { return sortedKeys(p.MCP.Servers) }

package persona

import (
	"bytes"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"

	"sigs.k8s.io/yaml"
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

var (
	// nameRE is a DNS label: a persona's name ends up in image and object names.
	nameRE = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`)
	// skillNameRE is the Agent Skills name rule.
	skillNameRE = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,62}[a-z0-9])?$`)
	// serverRE keeps "__" out of server names: Claude Code spells an MCP
	// tool mcp__<server>__<tool>.
	serverRE    = regexp.MustCompile(`^[a-z0-9]([-a-z0-9_]{0,30}[a-z0-9])?$`)
	mcpToolRE   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	paramRE     = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)
	harnessRE   = regexp.MustCompile(`^[a-z][a-z0-9-]{0,30}$`)
	commandRE   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`)
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
		p.Instructions = string(data)
	}
	p.Skills = l.skills()
	if l.err != nil {
		return nil
	}
	l.validateMCP(p.MCP)
	l.validateManifest(&p.Manifest, p.MCP)
	return p
}

// read returns a top-level file's content. A missing optional file is
// (nil, false) with no problem.
func (l *loader) read(name string, required bool) ([]byte, bool) {
	full := filepath.Join(l.dir, name)
	info, err := os.Lstat(full)
	if os.IsNotExist(err) {
		if required {
			l.add(name, "", "is missing")
		}
		return nil, false
	}
	if err != nil {
		l.err = err
		return nil, false
	}
	return l.readRegular(name, full, info)
}

// readRegular reads one file, refusing anything but a regular file: a
// symlink could name a file outside the persona, and the image that carries
// the persona would not carry its target.
func (l *loader) readRegular(rel, full string, info fs.FileInfo) ([]byte, bool) {
	if !info.Mode().IsRegular() {
		l.add(rel, "", "is not a regular file (symlinks and devices are refused)")
		return nil, false
	}
	if info.Size() > maxFileBytes {
		l.add(rel, "", "is %d bytes; the limit is %d", info.Size(), maxFileBytes)
		return nil, false
	}
	l.total += int(info.Size())
	if l.total > maxTotalBytes {
		l.add(rel, "", "the persona passes %d bytes in total", maxTotalBytes)
		return nil, false
	}
	data, err := os.ReadFile(full)
	if err != nil {
		l.err = err
		return nil, false
	}
	return data, true
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
		info, err := d.Info()
		if err != nil {
			return err
		}
		data, ok := l.readRegular(path.Join(rel, fileRel), full, info)
		if ok {
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
	var meta struct {
		Name        string `json:"name"`
		Description string `json:"description"`
	}
	// Not strict: Agent Skills allows more keys, and a harness that reads
	// them gets the file verbatim.
	if err := yaml.Unmarshal(front, &meta); err != nil {
		l.add(file, "frontmatter", "%v", err)
		return s, false
	}
	if meta.Name != name {
		l.add(file, "name", "is %q; it must be the directory name %q", meta.Name, name)
	}
	switch {
	case strings.TrimSpace(meta.Description) == "":
		l.add(file, "description", "is required")
	case len(meta.Description) > maxDescription:
		l.add(file, "description", "is %d characters; the limit is %d", len(meta.Description), maxDescription)
	}
	s.Description = meta.Description
	return s, true
}

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

// mcp decodes mcp.yaml. A server with a url gets its own problem rather than
// the strict decoder's "unknown field": v1 reaches MCP servers only through
// the broker, whose policy and audit a direct URL would bypass.
func (l *loader) mcp(data []byte) MCP {
	var raw struct {
		Servers map[string]map[string]any `json:"servers"`
	}
	if err := yaml.Unmarshal(data, &raw); err == nil {
		for _, name := range sortedKeys(raw.Servers) {
			if _, ok := raw.Servers[name]["url"]; ok {
				l.add(MCPFile, "servers."+name+".url",
					"direct URLs are not supported; a server names a broker MCP server (broker: <name>)")
				delete(raw.Servers[name], "url")
			}
		}
		if cleaned, err := yaml.Marshal(raw); err == nil {
			data = cleaned
		}
	}
	var m MCP
	if err := yaml.UnmarshalStrict(data, &m); err != nil {
		l.add(MCPFile, "", "%v", err)
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
		l.add(ManifestFile, "description", "is %d characters; the limit is %d", len(m.Description), maxDescription)
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

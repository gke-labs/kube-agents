// Package persona is the harness-neutral persona format of an AgentProfile
// (#3656): what an agent is told, which skills it has and which tools and MCP
// servers it may reach, written once and rendered for each harness by an
// adapter (claudecode, and Antigravity to follow). A persona is a directory:
//
//	persona.yaml     the manifest (Manifest)
//	instructions.md  the agent's instructions, AGENTS.md-compatible
//	skills/<name>/   Agent Skills: SKILL.md plus its files
//	mcp.yaml         optional: MCP servers on the broker's relay (MCP)
//	README.md        optional, never rendered
//
// The persona image carries this directory as its /files payload, and the
// profile pod mounts it at /persona (#3662, the profile pod contract).
// Nothing here reads the network, credentials or the environment: Load reads
// one directory, and an adapter maps what Load returned.
package persona

import (
	"errors"
	"fmt"
	"path"
	"strings"
)

// APIVersion is the one manifest version Load accepts.
const APIVersion = "persona.kube-agents.x-k8s.io/v1alpha1"

// The files and directories of a persona.
const (
	ManifestFile     = "persona.yaml"
	InstructionsFile = "instructions.md"
	SkillsDir        = "skills"
	SkillFile        = "SKILL.md"
	MCPFile          = "mcp.yaml"
	ReadmeFile       = "README.md"
)

// The neutral names of the harness built-in tools a manifest's tools list
// may grant. Every other entry names an MCP server from mcp.yaml, whole
// ("gke") or one of its tools ("gke.get_cluster").
const (
	ToolReadFile  = "read_file"
	ToolWebSearch = "web_search"
	ToolShell     = "shell"
)

// BuiltinTools are the neutral built-in tool names, for validation and for
// adapters that map them.
var BuiltinTools = []string{ToolReadFile, ToolWebSearch, ToolShell}

// Harness names a harness an adapter renders for: the value the profile
// worker is started with (#3662).
type Harness string

const (
	ClaudeCode  Harness = "claude-code"
	Antigravity Harness = "antigravity"
)

// Persona is a loaded, validated persona directory.
type Persona struct {
	Manifest     Manifest
	Instructions string
	// Skills are sorted by name.
	Skills []Skill
	// MCP is empty when the persona has no mcp.yaml.
	MCP MCP
}

// Manifest is persona.yaml.
type Manifest struct {
	APIVersion  string `json:"apiVersion"`
	Name        string `json:"name"`
	Description string `json:"description"`
	// Model is a hint; the worker decides whether to honour it.
	Model string `json:"model,omitempty"`
	// Tools is the allowlist: BuiltinTools and MCP server references.
	Tools []string `json:"tools,omitempty"`
	// Parameters are the values each task supplies in the dispatcher's
	// preamble (the target cluster, say). They are never stamped in.
	Parameters []Parameter `json:"parameters,omitempty"`
	Requires   Requires    `json:"requires,omitempty"`
	// Harness holds per-harness hints keyed by Harness. An adapter reads its
	// own key and ignores the others.
	Harness map[string]map[string]any `json:"harness,omitempty"`
}

// Parameter is one value a task supplies.
type Parameter struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	Required    bool   `json:"required,omitempty"`
}

// Requires lists what the persona expects to find in the pod. It is a hint
// for whoever composes the profile's tools images; nothing enforces it.
type Requires struct {
	Commands []string `json:"commands,omitempty"`
}

// Skill is one skills/<name> directory.
type Skill struct {
	Name        string
	Description string
	// Files are every file of the skill, SKILL.md included, by path
	// relative to the skill directory, sorted.
	Files []File
}

// File is one file to write, by slash-separated path relative to a root.
type File struct {
	Path       string
	Data       []byte
	Executable bool
}

// MCP is mcp.yaml.
type MCP struct {
	Servers map[string]Server `json:"servers"`
}

// Server is one MCP server on the credential broker's MCP relay, by its
// relay name. There is no direct-URL kind: a server reached around the broker
// would be outside its policy and its audit log. The relay authenticates the
// pod, so a server carries no credential.
type Server struct {
	Broker string `json:"broker"`
	// Include narrows the server to these tools; empty means all of them.
	Include []string `json:"include,omitempty"`
}

// Relay is where the credential broker's MCP relay is and how a harness
// reaches it, supplied by the worker that runs the adapter (the adapter
// itself reads no environment). A server's URL is URL + "/" + its broker
// name. Command and Args start a stdio proxy to it; "{url}" in Args
// is replaced with the server's URL. The zero Relay means no relay: every
// server is dropped with a warning.
type Relay struct {
	URL     string
	Command string
	Args    []string
	Env     map[string]string
}

// Target is what an adapter needs from the pod it renders for.
type Target struct {
	Relay Relay
}

// Output is one harness's rendering of a persona.
type Output struct {
	// Files go under the harness's per-task config directory. Every path is
	// relative and stays inside it.
	Files []File
	// Args are the flags the worker adds to the harness argv.
	Args []string
	// Allowed and Disallowed are the harness tool names the Args grant and
	// deny, for a worker that merges its own lists rather than parse Args.
	// A worker that adds a deny list of its own unions it with Disallowed.
	Allowed    []string
	Disallowed []string
	// Model is the manifest's model hint, for the worker to honour or not.
	Model string
	// Warnings name each part of the persona the harness cannot express.
	// Dropping something is never an error.
	Warnings []string
}

// Adapter renders a persona for one harness.
type Adapter interface {
	Render(p *Persona, t Target) (Output, error)
}

// Problem is one thing wrong with a persona directory.
type Problem struct {
	// File is the persona-relative path the problem is in.
	File string
	// Field locates it inside File, when it has a place there.
	Field string
	Msg   string
}

func (p Problem) Error() string {
	if p.Field == "" {
		return p.File + ": " + p.Msg
	}
	return p.File + ": " + p.Field + ": " + p.Msg
}

// InvalidError is every Problem Load found, so one pass reports them all.
type InvalidError struct {
	Dir      string
	Problems []Problem
}

func (e *InvalidError) Error() string {
	lines := make([]string, 0, len(e.Problems)+1)
	lines = append(lines, fmt.Sprintf("persona %s is invalid:", e.Dir))
	for _, p := range e.Problems {
		lines = append(lines, "  "+p.Error())
	}
	return strings.Join(lines, "\n")
}

// IsInvalid reports whether err is a persona validation failure, as opposed
// to an I/O error reading the directory.
func IsInvalid(err error) bool {
	var invalid *InvalidError
	return errors.As(err, &invalid)
}

// LocalPath reports whether p is a clean, relative, slash-separated path
// that stays inside its root.
func LocalPath(p string) bool {
	return p != "" && p != "." && path.Clean(p) == p && !path.IsAbs(p) &&
		p != ".." && !strings.HasPrefix(p, "../") && !strings.Contains(p, `\`)
}

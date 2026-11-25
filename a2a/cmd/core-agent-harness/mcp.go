package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	adktool "google.golang.org/adk/v2/tool"

	coremcp "github.com/go-steer/core-agent/v3/pkg/mcp"
	"github.com/go-steer/core-agent/v3/pkg/permissions"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// mcpStartBound is how long the turn's MCP servers get to start and list
// their tools before the turn fails.
var mcpStartBound = 60 * time.Second

// mcpLog is where a server's elicitation notices go.
var mcpLog = func(s string) { fmt.Fprintln(os.Stderr, "core-agent-harness: mcp:", s) }

// buildMCP starts the turn's MCP servers and returns their toolsets, gated,
// and a func that stops them. The process exits after the turn, so a
// server lives for one turn.
//
// Each server registers only the tools its entry names (core-agent's
// ServerSpec.Tools allowlist), as <server>_<tool>, the names the tool
// policy permits. The turn fails, with every server stopped, if an entry
// names no tools (an empty allowlist means the whole catalog to
// core-agent), if a server does not start, or if it does not list every
// tool its entry names.
//
// Servers come only from the Envelope: core-agent's builder reads them from
// an mcp.json in a fresh private directory, written and removed here, and
// reads no other config. No value is interpolated: a ${env:NAME} in an env
// value fails the turn rather than reading the harness's environment.
func buildMCP(ctx context.Context, servers []coreagentwire.MCPServer, gate *permissions.Gate) ([]adktool.Toolset, func(), error) {
	if len(servers) == 0 {
		return nil, func() {}, nil
	}
	specs, err := mcpSpecs(servers)
	if err != nil {
		return nil, func() {}, err
	}
	dir, err := os.MkdirTemp("", "core-agent-mcp-")
	if err != nil {
		return nil, func() {}, fmt.Errorf("mcp config dir: %w", err)
	}
	defer os.RemoveAll(dir)
	off := false
	cfg, err := json.Marshal(coremcp.Servers{Version: 1, Servers: specs, AgenticWrap: &off})
	if err != nil {
		return nil, func() {}, err
	}
	if err := os.WriteFile(filepath.Join(dir, coremcp.MCPFileName), cfg, 0o600); err != nil {
		return nil, func() {}, fmt.Errorf("mcp config: %w", err)
	}

	type built struct {
		srvs []*coremcp.Server
		sets []adktool.Toolset
		err  error
	}
	done := make(chan built, 1)
	go func() {
		srvs, sets, err := coremcp.Build(coremcp.WithTrustedAgentsDir(ctx, dir), dir, "", mcpLog, gate, nil, nil)
		done <- built{srvs, sets, err}
	}()
	var b built
	select {
	case b = <-done:
	case <-time.After(mcpStartBound):
		// The process exits on this error; the adapter ends what it left.
		return nil, func() {}, fmt.Errorf("the MCP servers did not start within %s", mcpStartBound)
	case <-ctx.Done():
		return nil, func() {}, ctx.Err()
	}
	closeAll := func() { coremcp.CloseAll(b.srvs) }
	if b.err != nil {
		closeAll()
		return nil, func() {}, b.err
	}
	if err := checkStarted(servers, b.srvs); err != nil {
		closeAll()
		return nil, func() {}, err
	}
	return b.sets, closeAll, nil
}

// mcpSpecs is the Envelope's servers as core-agent server specs, refusing
// an entry it could not run exactly as written.
func mcpSpecs(servers []coreagentwire.MCPServer) (map[string]coremcp.ServerSpec, error) {
	specs := make(map[string]coremcp.ServerSpec, len(servers))
	for _, s := range servers {
		if s.Name == "" {
			return nil, fmt.Errorf("an MCP server has no name")
		}
		if _, dup := specs[s.Name]; dup {
			return nil, fmt.Errorf("MCP server %q is named twice", s.Name)
		}
		if len(s.Tools) == 0 {
			return nil, fmt.Errorf("MCP server %q names no tools; a server is granted only the tools it names", s.Name)
		}
		for k, v := range s.Env {
			if strings.Contains(v, "${env:") {
				return nil, fmt.Errorf("MCP server %q: env %s asks for interpolation, which is not supported", s.Name, k)
			}
		}
		spec := coremcp.ServerSpec{Command: s.Command, Args: s.Args, Env: s.Env, URL: s.URL, Tools: s.Tools}
		switch {
		case s.Command != "" && s.URL == "":
			spec.Transport = "stdio"
		case s.URL != "" && s.Command == "":
			spec.Transport = "http"
		default:
			return nil, fmt.Errorf("MCP server %q needs exactly one of a command or a URL", s.Name)
		}
		if err := spec.Validate(s.Name); err != nil {
			return nil, err
		}
		specs[s.Name] = spec
	}
	return specs, nil
}

// checkStarted is the started-check: every granted server is up and lists
// every tool its entry names, and registers nothing else.
func checkStarted(granted []coreagentwire.MCPServer, srvs []*coremcp.Server) error {
	byName := make(map[string]*coremcp.Server, len(srvs))
	for _, s := range srvs {
		byName[s.Name] = s
	}
	for _, g := range granted {
		s := byName[g.Name]
		if s == nil {
			return fmt.Errorf("MCP server %q did not start", g.Name)
		}
		if s.Status != coremcp.StatusOK {
			return fmt.Errorf("MCP server %q did not start: %v", g.Name, s.Err)
		}
		want := make([]string, 0, len(g.Tools))
		for _, t := range g.Tools {
			want = append(want, g.Name+"_"+t)
		}
		var missing []string
		for _, w := range want {
			if !slices.Contains(s.Tools, w) {
				missing = append(missing, strings.TrimPrefix(w, g.Name+"_"))
			}
		}
		if len(missing) > 0 {
			return fmt.Errorf("MCP server %q does not list %s", g.Name, strings.Join(missing, ", "))
		}
		for _, have := range s.Tools {
			if !slices.Contains(want, have) {
				return fmt.Errorf("MCP server %q registered %s, which its entry does not name", g.Name, have)
			}
		}
	}
	return nil
}

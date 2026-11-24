package main

import (
	"fmt"
	"slices"

	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/plugin"
	adktool "google.golang.org/adk/v2/tool"

	"github.com/go-steer/core-agent/v3/pkg/permissions"
	"github.com/go-steer/core-agent/v3/pkg/tools"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/toolgrant"
)

// toolPolicyPluginName names the before-tool plugin, the first plugin the
// agent runs.
const toolPolicyPluginName = "kube-agents-tool-policy"

// policySetup turns the turn's tool policy into core-agent's, at three
// layers that each fail closed on their own:
//   - registration: only the built-ins the policy names, each one a tool a
//     grant may register (toolgrant's table; anything else is refused, so
//     an edit, delete, URL fetch, spawn or background agent is never
//     offered);
//   - the gate: allow mode with the policy's patterns, so a call no pattern
//     allows is refused without a prompt, and deny wins; file tools reach
//     only workdir, the turn's workspace (the path scope);
//   - the before-tool plugin, returned first: every call, MCP and skills
//     included, is checked against the turn's tools before it runs, and
//     one outside them is refused (the tool is skipped and the refusal is
//     its result).
//
// mcp carries each server's granted tools; core-agent names them
// <server>_<tool>.
func policySetup(p coreagentwire.Policy, mcp []coreagentwire.MCPServer, workdir string) (tools.BuiltinTools, permissions.Options, []*plugin.Plugin, error) {
	var b tools.BuiltinTools
	permitted := map[string]bool{}
	for _, name := range p.Tools {
		if err := enable(&b, name); err != nil {
			return tools.BuiltinTools{}, permissions.Options{}, nil, err
		}
		permitted[name] = true
	}
	for _, s := range mcp {
		for _, t := range s.Tools {
			permitted[s.Name+"_"+t] = true
		}
	}
	for _, t := range toolgrant.CoreAgentSkillTools {
		permitted[t] = true
	}
	policy, err := permissions.NewPolicy(p.Allow, p.Deny)
	if err != nil {
		return tools.BuiltinTools{}, permissions.Options{}, nil, fmt.Errorf("permission patterns: %w", err)
	}
	scope, err := permissions.NewPathScope(workdir, "", nil)
	if err != nil {
		return tools.BuiltinTools{}, permissions.Options{}, nil, fmt.Errorf("path scope: %w", err)
	}
	pl, err := toolPolicyPlugin(permitted)
	if err != nil {
		return tools.BuiltinTools{}, permissions.Options{}, nil, err
	}
	return b, permissions.Options{Mode: permissions.ModeAllow, Policy: policy, Scope: scope}, []*plugin.Plugin{pl}, nil
}

// toolPolicyPlugin refuses, before it runs, any tool call outside
// permitted. It doesn't rewrite arguments; a call with nil arguments is
// checked by name like any other.
func toolPolicyPlugin(permitted map[string]bool) (*plugin.Plugin, error) {
	return plugin.New(plugin.Config{
		Name: toolPolicyPluginName,
		BeforeToolCallback: func(_ agent.Context, t adktool.Tool, _ map[string]any) (map[string]any, error) {
			if t == nil {
				return nil, fmt.Errorf("a tool call without a tool")
			}
			if !permitted[t.Name()] {
				return nil, fmt.Errorf("tool %q is not in this turn's grant", t.Name())
			}
			return nil, nil
		},
	})
}

// enable turns on one built-in tool by name. A name toolgrant's table
// doesn't let a grant register is refused, so core-agent adding a built-in
// never offers it to a worker by default.
func enable(b *tools.BuiltinTools, name string) error {
	if !slices.Contains(tools.BuiltinToolNames(), name) {
		return fmt.Errorf("unknown built-in tool %q", name)
	}
	if !toolgrant.CoreAgentRegistrable(name) {
		return fmt.Errorf("built-in tool %q is not offered to workers", name)
	}
	f := builtinSwitch(b, name)
	if f == nil {
		// Known to core-agent but not to this map: refuse rather than
		// leave it off without a word.
		return fmt.Errorf("built-in tool %q has no switch in the harness", name)
	}
	*f = true
	return nil
}

// builtinSwitch is the BuiltinTools field for every core-agent built-in,
// whether or not a grant may turn it on: toolgrant's table, checked first,
// is the only thing that decides that.
func builtinSwitch(b *tools.BuiltinTools, name string) *bool {
	return map[string]*bool{
		"bash":              &b.Bash,
		"read_file":         &b.ReadFile,
		"read_many_files":   &b.ReadManyFiles,
		"view_file_outline": &b.ViewFileOutline,
		"write_file":        &b.WriteFile,
		"edit_file":         &b.EditFile,
		"delete_file":       &b.DeleteFile,
		"stat":              &b.Stat,
		"list_dir":          &b.ListDir,
		"glob":              &b.Glob,
		"grep":              &b.Grep,
		"json_query":        &b.JSONQuery,
		"fetch_url":         &b.FetchURL,
		"alert":             &b.Alert,
		"wait_and_verify":   &b.WaitAndVerify,
		"todo":              &b.Todo,
		"record_plan":       &b.RecordPlan,
		"sciontool_status":  &b.SciontoolStatus,
	}[name]
}

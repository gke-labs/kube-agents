package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"slices"
	"strconv"
	"strings"

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
	servers := map[string][]string{}
	for _, s := range mcp {
		if _, dup := servers[s.Name]; dup {
			return tools.BuiltinTools{}, permissions.Options{}, nil, fmt.Errorf("MCP server %q named twice", s.Name)
		}
		servers[s.Name] = s.Tools
	}
	// Checked again here, on what the envelope carries, so the plugin's set
	// doesn't rest on the adapter having checked it.
	if err := toolgrant.CheckMCPNames(servers); err != nil {
		return tools.BuiltinTools{}, permissions.Options{}, nil, err
	}
	for server, ts := range servers {
		for _, t := range ts {
			permitted[server+"_"+t] = true
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
	pl, err := toolPolicyPlugin(permitted, refusalLog)
	if err != nil {
		return tools.BuiltinTools{}, permissions.Options{}, nil, err
	}
	return b, permissions.Options{Mode: permissions.ModeAllow, Policy: policy, Scope: scope}, []*plugin.Plugin{pl}, nil
}

// refusalLog is where each refused tool call is logged, one line each
// (logRefusal); run points it at the process's stderr, which the worker
// writes to its pod log.
var refusalLog io.Writer = os.Stderr

// RefusalLogPrefix starts every refused-call line.
const RefusalLogPrefix = "tool refused: "

// toolPolicyPlugin refuses, before it runs, any tool call outside
// permitted, and logs every refused call to log, whichever layer refused
// it. It doesn't rewrite arguments; a call with nil arguments is checked by
// name like any other.
func toolPolicyPlugin(permitted map[string]bool, log io.Writer) (*plugin.Plugin, error) {
	return plugin.New(plugin.Config{
		Name: toolPolicyPluginName,
		// Every tool error passes through here, the plugin's own refusal
		// included. Returning nothing leaves the error as the tool's
		// result.
		OnToolErrorCallback: func(_ agent.Context, t adktool.Tool, _ map[string]any, err error) (map[string]any, error) {
			name := ""
			if t != nil {
				name = t.Name()
			}
			logRefusal(log, name, err)
			return nil, nil
		},
		BeforeToolCallback: func(_ agent.Context, t adktool.Tool, _ map[string]any) (map[string]any, error) {
			if t == nil {
				return nil, fmt.Errorf("a tool call without a tool")
			}
			if !permitted[t.Name()] {
				return nil, fmt.Errorf("tool %q %s", t.Name(), notInGrant)
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

// notInGrant ends the plugin's own refusal.
const notInGrant = "is not in this turn's grant"

// refusalReasons are the refusals logRefusal names, each by a phrase of
// its error: the plugin's, then core-agent's gate in allow mode. The
// phrases are core-agent's error texts; one it changes falls through to
// unlogged, never to a wrong reason.
var refusalReasons = []struct{ phrase, reason string }{
	{notInGrant, "not in the turn's grant"},
	{"requires an allowlist entry in 'allow' mode", "no allow rule matches"},
	{"denied by config policy", "a deny rule matches"},
	{"is outside scope", "outside the workspace"},
	{"bash refused", "the gate's command checks"},
	// ADK's, for a call to a tool that isn't registered.
	{"' not found.", "no such tool"},
}

// logRefusal writes one line for a refused call: the prefix, the tool's
// name and why, never its arguments (core-agent's error texts quote the
// command or path, so they aren't logged). An error that isn't a refusal,
// a missing file say, writes nothing.
func logRefusal(w io.Writer, tool string, err error) {
	if w == nil || err == nil {
		return
	}
	reason := ""
	switch {
	case errors.Is(err, permissions.ErrNoPrompter), errors.Is(err, permissions.ErrEscalationRefused):
		reason = "needs an approval no one can give"
	case errors.Is(err, permissions.ErrSandboxDenied), errors.Is(err, permissions.ErrCredentialFile):
		reason = "the sandbox"
	default:
		for _, r := range refusalReasons {
			if strings.Contains(err.Error(), r.phrase) {
				reason = r.reason
				break
			}
		}
	}
	if reason == "" {
		return
	}
	_, _ = fmt.Fprintf(w, "%s%s (%s)\n", RefusalLogPrefix, strconv.Quote(tool), reason)
}

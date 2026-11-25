package main

import (
	"fmt"

	"google.golang.org/adk/v2/plugin"

	"github.com/go-steer/core-agent/v3/pkg/permissions"
	"github.com/go-steer/core-agent/v3/pkg/tools"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// policySetup turns the turn's tool policy into core-agent's: the built-in
// tools the model is offered, the permission gate's options, and the plugins
// that see every tool call before it runs (allow, deny or rewrite its args).
//
// Still to come (#7158): the before-tool plugin and the mapping from the
// persona grant. What is here is the floor: only the named built-ins, in
// allow mode with the policy's patterns, so a call no pattern allows is
// refused without a prompt; file tools reach only workdir, the turn's
// workspace; background agents and spawns are never offered.
func policySetup(p coreagentwire.Policy, workdir string) (tools.BuiltinTools, permissions.Options, []*plugin.Plugin, error) {
	var b tools.BuiltinTools
	for _, name := range p.Tools {
		if err := enable(&b, name); err != nil {
			return b, permissions.Options{}, nil, err
		}
	}
	policy, err := permissions.NewPolicy(p.Allow, p.Deny)
	if err != nil {
		return b, permissions.Options{}, nil, fmt.Errorf("permission patterns: %w", err)
	}
	scope, err := permissions.NewPathScope(workdir, "", nil)
	if err != nil {
		return b, permissions.Options{}, nil, fmt.Errorf("path scope: %w", err)
	}
	return b, permissions.Options{Mode: permissions.ModeAllow, Policy: policy, Scope: scope}, nil, nil
}

// enable turns on one built-in tool by name, refusing a name core-agent
// doesn't have.
func enable(b *tools.BuiltinTools, name string) error {
	all := tools.Default()
	probe := all
	if err := probe.Disable(name); err != nil {
		return fmt.Errorf("unknown built-in tool %q", name)
	}
	// probe is all with name off: copy name's switch from all into b.
	switch name {
	case "bash":
		b.Bash = all.Bash
	case "read_file":
		b.ReadFile = all.ReadFile
	case "read_many_files":
		b.ReadManyFiles = all.ReadManyFiles
	case "write_file":
		b.WriteFile = all.WriteFile
	case "edit_file":
		b.EditFile = all.EditFile
	case "list_dir":
		b.ListDir = all.ListDir
	case "glob":
		b.Glob = all.Glob
	case "grep":
		b.Grep = all.Grep
	default:
		return fmt.Errorf("built-in tool %q is not offered to workers", name)
	}
	return nil
}

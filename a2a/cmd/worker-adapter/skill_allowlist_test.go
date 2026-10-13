package main

import (
	"strings"
	"testing"
)

// The session's skills (a2a/cmd/session-persona) load without the Skill tool
// on --allowedTools. The harness's Skill tool runs a skill on its own when the
// skill's definition carries no field that widens what it may do (no
// allowed-tools, no hooks), and the shipped skills carry only name and
// description. A bare "Skill" here would also run a skill that does widen it.
// The harness can Write a .claude/skills/<name>/SKILL.md under its working
// directory, and the harness discovers skills from directories it works in
// mid-run; one declaring `allowed-tools: Bash` would get a shell, and under the
// cluster view Bash is not on the disallowed list. So the allowlists name no
// Skill, and the Skill tool's own check is what decides.
func TestAllowlistsLeaveTheSkillToolToItsOwnCheck(t *testing.T) {
	for name, list := range map[string]string{
		"defaultAllowedTools":     defaultAllowedTools,
		"clusterViewAllowedTools": clusterViewAllowedTools,
	} {
		for _, tool := range strings.FieldsFunc(list, func(r rune) bool { return r == ',' || r == ' ' }) {
			if tool == "Skill" || strings.HasPrefix(tool, "Skill(") {
				t.Errorf("%s allows %q; skills must pass the Skill tool's own check", name, tool)
			}
		}
	}
}

package render

import (
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

func TestFor(t *testing.T) {
	if a, err := For(persona.ClaudeCode); err != nil || a == nil {
		t.Errorf("claude-code: %v, %v", a, err)
	}
	if _, err := For(persona.Antigravity); err == nil || !strings.Contains(err.Error(), "no persona adapter yet") {
		t.Errorf("antigravity: %v, want a not-yet error", err)
	}
	if _, err := For("hermes"); err == nil || !strings.Contains(err.Error(), "unknown harness") {
		t.Errorf("hermes: %v, want unknown", err)
	}
}

func TestCheckOutputRefusesAnEscape(t *testing.T) {
	for _, p := range []string{"../CLAUDE.md", "/etc/cron.d/x", "skills/../../x"} {
		if err := CheckOutput(persona.Output{Files: []persona.File{{Path: "CLAUDE.md"}, {Path: p}}}); err == nil {
			t.Errorf("CheckOutput accepted %q", p)
		}
	}
	if err := CheckOutput(persona.Output{Files: []persona.File{{Path: "skills/a/SKILL.md"}}}); err != nil {
		t.Error(err)
	}
}

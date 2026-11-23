package lib

import "testing"

func TestSessionHomeCovers(t *testing.T) {
	for rel, want := range map[string]bool{
		"CLAUDE.md": true, "skills": true, "skills/a/SKILL.md": true,
		"skillsx": false, "skillsx/a": false, "projects/state.json": false,
		"settings.json": false, ".credentials.json": false, "skills_manifest.sha256": false,
	} {
		if got := SessionHomeCovers(rel); got != want {
			t.Errorf("SessionHomeCovers(%q) = %v, want %v", rel, got, want)
		}
	}
}

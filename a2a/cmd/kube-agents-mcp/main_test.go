package main

import "testing"

func TestVersionStringNamesTheBuild(t *testing.T) {
	if got, want := versionString(), "kube-agents-mcp "+devVersion; got != want {
		t.Errorf("versionString() = %q, want %q for a build without the release ldflags", got, want)
	}
	defer func(old string) { version = old }(version)
	version = "0.34.0"
	if got, want := versionString(), "kube-agents-mcp 0.34.0"; got != want {
		t.Errorf("versionString() = %q, want %q", got, want)
	}
}

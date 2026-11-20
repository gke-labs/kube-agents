package main

import (
	"runtime/debug"
	"testing"
)

// withBuildInfo makes readBuildInfo report mainVersion (or nothing, when ok is false) for one test.
func withBuildInfo(t *testing.T, mainVersion string, ok bool) {
	t.Helper()
	old := readBuildInfo
	t.Cleanup(func() { readBuildInfo = old })
	readBuildInfo = func() (*debug.BuildInfo, bool) {
		if !ok {
			return nil, false
		}
		return &debug.BuildInfo{Main: debug.Module{Version: mainVersion}}, true
	}
}

func TestVersionStringNamesTheBuild(t *testing.T) {
	withBuildInfo(t, develModuleVersion, true)
	if got, want := versionString(), "kube-agents-mcp "+devVersion; got != want {
		t.Errorf("versionString() = %q, want %q for a build without the release ldflags", got, want)
	}
	defer func(old string) { version = old }(version)
	version = "0.34.0"
	if got, want := versionString(), "kube-agents-mcp 0.34.0"; got != want {
		t.Errorf("versionString() = %q, want %q", got, want)
	}
}

func TestVersionStringFallsBackToTheModuleVersion(t *testing.T) {
	const pseudo = "v0.0.0-20261119120000-1be9fe992abc"
	for _, tc := range []struct {
		name        string
		mainVersion string
		ok          bool
		want        string
	}{
		{"go install at a ref", pseudo, true, "kube-agents-mcp " + pseudo},
		{"go install at a tag", "v0.34.0", true, "kube-agents-mcp v0.34.0"},
		{"build from a tree without version control", develModuleVersion, true, "kube-agents-mcp " + devVersion},
		{"no module version", "", true, "kube-agents-mcp " + devVersion},
		{"no build info", "", false, "kube-agents-mcp " + devVersion},
	} {
		t.Run(tc.name, func(t *testing.T) {
			withBuildInfo(t, tc.mainVersion, tc.ok)
			if got := versionString(); got != tc.want {
				t.Errorf("versionString() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestStampedReleaseWinsOverTheModuleVersion(t *testing.T) {
	withBuildInfo(t, "v0.0.0-20261119120000-1be9fe992abc", true)
	defer func(old string) { version = old }(version)
	version = "0.34.0"
	if got, want := versionString(), "kube-agents-mcp 0.34.0"; got != want {
		t.Errorf("versionString() = %q, want %q", got, want)
	}
}

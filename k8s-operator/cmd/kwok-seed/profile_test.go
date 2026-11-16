// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func writeProfileFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "profile.json")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

const smokeJSON = `{"nodes":100,"pods":3000,"namespaces":20,"services":100,"events":5000}`

var smokeProfile = profile{Nodes: 100, Pods: 3000, Namespaces: 20, Services: 100, Events: 5000}

func TestLoadProfile(t *testing.T) {
	p, err := loadProfile(writeProfileFile(t, smokeJSON))
	if err != nil {
		t.Fatal(err)
	}
	if p != smokeProfile {
		t.Errorf("loaded %+v, want %+v", p, smokeProfile)
	}
}

func TestLoadProfileRejectsUnknownKey(t *testing.T) {
	_, err := loadProfile(writeProfileFile(t, `{"node":100,"pods":3000,"namespaces":20,"services":100,"events":5000}`))
	if err == nil || !strings.Contains(err.Error(), "unknown field") {
		t.Fatalf("err = %v, want an unknown field error", err)
	}
}

func TestLoadProfileMissingFile(t *testing.T) {
	if _, err := loadProfile(filepath.Join(t.TempDir(), "absent.json")); err == nil {
		t.Fatal("expected an error for a missing file")
	}
}

func TestValidate(t *testing.T) {
	cases := []struct {
		name    string
		p       profile
		wantErr string
	}{
		{"valid", smokeProfile, ""},
		{"zero nodes", profile{Pods: 10, Namespaces: 1, Services: 1, Events: 1}, "nodes must be greater than zero"},
		{"negative events", profile{Nodes: 1, Pods: 10, Namespaces: 1, Services: 1, Events: -1}, "events must be greater than zero"},
		{"zero namespaces", profile{Nodes: 1, Pods: 10, Services: 1, Events: 1}, "namespaces must be greater than zero"},
		{"fewer pods than nodes", profile{Nodes: 20, Pods: 10, Namespaces: 1, Services: 1, Events: 1}, "pods (10) must be at least nodes (20)"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := c.p.validate()
			if c.wantErr == "" {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), c.wantErr) {
				t.Fatalf("err = %v, want %q", err, c.wantErr)
			}
		})
	}
}

func TestWithOverrides(t *testing.T) {
	got := smokeProfile.withOverrides(map[string]int{"pods": 60000, "events": 150000})
	want := profile{Nodes: 100, Pods: 60000, Namespaces: 20, Services: 100, Events: 150000}
	if got != want {
		t.Errorf("withOverrides = %+v, want %+v", got, want)
	}
	if got := smokeProfile.withOverrides(nil); got != smokeProfile {
		t.Errorf("no overrides changed the profile: %+v", got)
	}
}

func TestResolveProfile(t *testing.T) {
	path := writeProfileFile(t, smokeJSON)
	all := map[string]int{"nodes": 10, "pods": 300, "namespaces": 5, "services": 10, "events": 500}

	t.Run("file with an override", func(t *testing.T) {
		got, err := resolveProfile(path, map[string]int{"nodes": 200})
		if err != nil {
			t.Fatal(err)
		}
		want := smokeProfile
		want.Nodes = 200
		if got != want {
			t.Errorf("got %+v, want %+v", got, want)
		}
	})
	t.Run("flags only", func(t *testing.T) {
		got, err := resolveProfile("", all)
		if err != nil {
			t.Fatal(err)
		}
		if got != (profile{Nodes: 10, Pods: 300, Namespaces: 5, Services: 10, Events: 500}) {
			t.Errorf("got %+v", got)
		}
	})
	t.Run("flags only, one missing", func(t *testing.T) {
		four := map[string]int{"nodes": 10, "pods": 300, "namespaces": 5, "services": 10}
		_, err := resolveProfile("", four)
		if err == nil || !strings.Contains(err.Error(), "--profile") {
			t.Fatalf("err = %v, want the --profile-or-all-flags error", err)
		}
	})
	t.Run("override makes the file invalid", func(t *testing.T) {
		_, err := resolveProfile(path, map[string]int{"pods": 50})
		if err == nil || !strings.Contains(err.Error(), "pods (50) must be at least nodes (100)") {
			t.Fatalf("err = %v", err)
		}
	})
	t.Run("missing file", func(t *testing.T) {
		if _, err := resolveProfile(filepath.Join(t.TempDir(), "none.json"), nil); err == nil {
			t.Fatal("expected an error")
		}
	})
}

func TestParseFlags(t *testing.T) {
	f, set, err := parseFlags([]string{"--kubeconfig", "/tmp/kc", "--pods", "300", "--events", "0"})
	if err != nil {
		t.Fatal(err)
	}
	if f.kubeconfig != "/tmp/kc" {
		t.Errorf("kubeconfig = %q", f.kubeconfig)
	}
	if want := map[string]int{"pods": 300, "events": 0}; !reflect.DeepEqual(set, want) {
		t.Errorf("set flags = %v, want %v", set, want)
	}
	if f.workers != 48 || f.seed != 0 || f.qps != 5000 || f.burst != 10000 || f.namespacePrefix != "team" ||
		!f.waitRunning || f.waitTimeout != 30*time.Minute || f.skipEvents || f.summaryOut != "" || f.kubeContext != "" {
		t.Errorf("defaults differ from the design: %+v", f)
	}

	if _, _, err := parseFlags([]string{"--pods", "300"}); err == nil || !strings.Contains(err.Error(), "--kubeconfig is required") {
		t.Errorf("missing kubeconfig: err = %v", err)
	}
	if _, _, err := parseFlags([]string{"--kubeconfig", "x", "--workers", "0"}); err == nil || !strings.Contains(err.Error(), "--workers") {
		t.Errorf("zero workers: err = %v", err)
	}
}

func TestPrintMarkdown(t *testing.T) {
	s := summary{Profile: smokeProfile}
	s.Phases.Pods = phaseResult{Created: 2990, Existed: 10, Failed: 0, Seconds: 15, PerSecond: 200}
	s.PodsRunningAfterSeconds = 3.5
	var buf bytes.Buffer
	printMarkdown(&buf, s)
	out := buf.String()
	for _, want := range []string{
		"| phase | created | existed | failed | seconds | per_second |",
		"| pods | 2990 | 10 | 0 | 15.00 | 200.0 |",
		"| events | 0 | 0 | 0 | 0.00 | 0.0 |",
		"pods_running_after_seconds: 3.50",
		"first_error: -",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q:\n%s", want, out)
		}
	}
}

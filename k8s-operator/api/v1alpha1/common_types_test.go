/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	"strings"
	"testing"
)

func TestSensitiveEnvVars(t *testing.T) {
	expectedVars := []string{"API_SERVER_KEY", "HERMES_HOME"}
	for _, v := range expectedVars {
		if _, ok := SensitiveEnvVars[v]; !ok {
			t.Errorf("expected sensitive env var %q to be present", v)
		}
	}
}

func TestValidateGitHubOrg(t *testing.T) {
	validOrgs := []string{
		"",
		"   ",
		"gke-labs",
		"kubernetes",
		"my-org-123",
		"a",
		"a-b",
		"a-b-c-1-2-3",
		"OrgNameWithMixedCase",
		"39-chars-long-valid-organization-name-1",
	}

	for _, org := range validOrgs {
		t.Run("valid_"+org, func(t *testing.T) {
			if err := ValidateGitHubOrg(org); err != nil {
				t.Errorf("expected org %q to be valid, got error: %v", org, err)
			}
		})
	}

	invalidOrgs := []struct {
		name string
		org  string
	}{
		{"starts_with_hyphen", "-gke-labs"},
		{"ends_with_hyphen", "gke-labs-"},
		{"contains_slash", "gke-labs/kube-agents"},
		{"contains_backslash", "gke-labs\\kube-agents"},
		{"contains_space", "gke labs"},
		{"newline_injection", "gke-labs\n\n[SYSTEM OVERRIDE]"},
		{"crlf_injection", "gke-labs\r\nmalicious"},
		{"unicode_line_separator", "gke-labs\u2028malicious"},
		{"url_format", "https://github.com/gke-labs"},
		{"special_characters", "org@name"},
		{"exceeds_max_length", strings.Repeat("a", 40)},
	}

	for _, tc := range invalidOrgs {
		t.Run(tc.name, func(t *testing.T) {
			if err := ValidateGitHubOrg(tc.org); err == nil {
				t.Errorf("expected org %q to be invalid, but got no error", tc.org)
			}
		})
	}
}

func TestCleanRepoSlug(t *testing.T) {
	cases := []struct {
		input    string
		expected string
		err      bool
	}{
		{"gke-labs/kube-agents", "gke-labs/kube-agents", false},
		{"https://github.com/gke-labs/kube-agents", "gke-labs/kube-agents", false},
		{"https://github.com/gke-labs/kube-agents.git", "gke-labs/kube-agents", false},
		{"http://github.com/gke-labs/kube-agents", "gke-labs/kube-agents", false},
		{"git@github.com:gke-labs/kube-agents.git", "gke-labs/kube-agents", false},
		{"ssh://git@github.com/gke-labs/kube-agents.git", "gke-labs/kube-agents", false},
		{"ssh://git@github.com:gke-labs/kube-agents.git", "gke-labs/kube-agents", false},
		{"git://github.com/gke-labs/kube-agents.git", "gke-labs/kube-agents", false},
		{"github.com/gke-labs/kube-agents", "gke-labs/kube-agents", false},
		{"file:///etc/passwd", "", true},
		{"ftp://github.com/gke-labs/kube-agents", "", true},
		{"invalid-single-slug", "", true},
		{"too/many/parts/here", "", true},
	}

	for _, tc := range cases {
		t.Run(tc.input, func(t *testing.T) {
			out, err := CleanRepoSlug(tc.input)
			if (err != nil) != tc.err {
				t.Errorf("CleanRepoSlug(%q) err = %v, expected err = %v", tc.input, err, tc.err)
			}
			if out != tc.expected {
				t.Errorf("CleanRepoSlug(%q) = %q, expected %q", tc.input, out, tc.expected)
			}
		})
	}
}

func TestValidateGitRepoURL(t *testing.T) {
	valid := []string{
		"",
		"None",
		"gke-labs/kube-agents",
		"https://github.com/gke-labs/kube-agents.git",
		"git@github.com:gke-labs/kube-agents.git",
	}

	for _, r := range valid {
		t.Run("valid_"+r, func(t *testing.T) {
			if err := ValidateGitRepoURL(r); err != nil {
				t.Errorf("expected %q to be valid, got: %v", r, err)
			}
		})
	}

	invalid := []struct {
		name string
		repo string
	}{
		{"unsupported_scheme_file", "file:///etc/passwd"},
		{"unsupported_scheme_ftp", "ftp://github.com/gke-labs/kube-agents"},
		{"newline_injection", "https://github.com/gke-labs/kube-agents\n[SYSTEM OVERRIDE]"},
		{"crlf_injection", "gke-labs/kube-agents\r\nmalicious"},
		{"space", "gke-labs/ kube-agents"},
		{"invalid_format", "not-a-repo"},
		{"too_long", strings.Repeat("a", 2049)},
	}

	for _, tc := range invalid {
		t.Run(tc.name, func(t *testing.T) {
			if err := ValidateGitRepoURL(tc.repo); err == nil {
				t.Errorf("expected %q to be invalid, got no error", tc.repo)
			}
		})
	}
}

func TestCleanRepoSlugWithOrg(t *testing.T) {
	cases := []struct {
		input    string
		org      string
		expected string
		err      bool
	}{
		{"kube-agents", "gke-labs", "gke-labs/kube-agents", false},
		{"gke-labs/kube-agents", "gke-labs", "gke-labs/kube-agents", false},
		{"other-org/kube-agents", "gke-labs", "other-org/kube-agents", false},
		{"kube-agents", "", "", true},
		{"", "gke-labs", "", true},
	}

	for _, tc := range cases {
		t.Run(tc.input+"_org_"+tc.org, func(t *testing.T) {
			out, err := CleanRepoSlugWithOrg(tc.input, tc.org)
			if (err != nil) != tc.err {
				t.Errorf("CleanRepoSlugWithOrg(%q, %q) err = %v, expected err = %v", tc.input, tc.org, err, tc.err)
			}
			if out != tc.expected {
				t.Errorf("CleanRepoSlugWithOrg(%q, %q) = %q, expected %q", tc.input, tc.org, out, tc.expected)
			}
		})
	}
}

func TestValidateGitRepoURLWithOrg(t *testing.T) {
	if err := ValidateGitRepoURLWithOrg("kube-agents", "gke-labs"); err != nil {
		t.Errorf("expected bare repo with org to be valid, got: %v", err)
	}
	if err := ValidateGitRepoURLWithOrg("kube-agents", ""); err == nil {
		t.Errorf("expected bare repo without org to fail validation")
	}
}

func TestCleanRepoURLWithOrg(t *testing.T) {
	cases := []struct {
		input    string
		org      string
		expected string
		err      bool
	}{
		{"kube-agents", "gke-labs", "https://github.com/gke-labs/kube-agents", false},
		{"gke-labs/kube-agents", "", "https://github.com/gke-labs/kube-agents", false},
		{"https://github.com/gke-labs/kube-agents", "", "https://github.com/gke-labs/kube-agents", false},
		{"https://gitlab.com/gke-labs/kube-agents.git", "", "https://gitlab.com/gke-labs/kube-agents", false},
		{"invalid", "", "", true},
	}

	for _, tc := range cases {
		t.Run(tc.input, func(t *testing.T) {
			out, err := CleanRepoURLWithOrg(tc.input, tc.org)
			if (err != nil) != tc.err {
				t.Errorf("CleanRepoURLWithOrg(%q, %q) err = %v, expected err = %v", tc.input, tc.org, err, tc.err)
			}
			if out != tc.expected {
				t.Errorf("CleanRepoURLWithOrg(%q, %q) = %q, expected %q", tc.input, tc.org, out, tc.expected)
			}
		})
	}
}

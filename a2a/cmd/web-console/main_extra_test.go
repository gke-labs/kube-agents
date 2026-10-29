/*
Copyright 2026 Google LLC

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

package main

import "testing"

// repositoryURL turns every repository spelling values.yaml admits into a
// link a browser can open.
func TestRepositoryURLHandlesEveryRemoteSpelling(t *testing.T) {
	gitea := "gitea-http.gitea.svc.cluster.local:3000"
	cases := []struct {
		in   repositoryInfo
		want string
	}{
		{repositoryInfo{Provider: "github", Repository: "acme/infra"}, "https://github.com/acme/infra"},
		{repositoryInfo{Provider: "gitlab", Repository: "acme/infra.git"}, "https://gitlab.com/acme/infra"},
		{repositoryInfo{Provider: "github", Repository: "https://github.com/acme/infra.git"}, "https://github.com/acme/infra"},
		{repositoryInfo{Provider: "github", Repository: "git@github.com:acme/infra.git"}, "https://github.com/acme/infra"},
		{repositoryInfo{Provider: "github", Repository: "ssh://git@github.com/acme/infra"}, "https://github.com/acme/infra"},
		{repositoryInfo{Provider: "github", Repository: "ssh://git@github.example.com:2222/acme/infra.git"}, "https://github.example.com/acme/infra"},
		{repositoryInfo{Provider: "github", Repository: "git://github.com/acme/infra.git"}, "https://github.com/acme/infra"},
		{repositoryInfo{Provider: "github", Repository: "github.com/acme/infra"}, "https://github.com/acme/infra"},
		{repositoryInfo{Provider: "gitea", Host: gitea, Repository: "ops/gitops-repo"}, "http://" + gitea + "/ops/gitops-repo"},
		{repositoryInfo{Provider: "gitea", Repository: gitea + "/ops/gitops-repo"}, "http://" + gitea + "/ops/gitops-repo"},
		{repositoryInfo{Provider: "gitea", Repository: "ops/gitops-repo"}, ""},
	}
	for _, c := range cases {
		if got := repositoryURL(c.in); got != c.want {
			t.Errorf("repositoryURL(%+v) = %q, want %q", c.in, got, c.want)
		}
	}
}

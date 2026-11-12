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
	bbdc := "bitbucket.corp.example"
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
		{repositoryInfo{Provider: "bitbucket", Repository: "acme/infra"}, "https://bitbucket.org/acme/infra"},
		{repositoryInfo{Provider: "bitbucket", Repository: "git@bitbucket.org:acme/infra.git"}, "https://bitbucket.org/acme/infra"},
		{repositoryInfo{Provider: "bitbucket-datacenter", Host: bbdc, Repository: "OPS/infra"}, "https://" + bbdc + "/projects/OPS/repos/infra"},
		{repositoryInfo{Provider: "bitbucket-datacenter", Host: bbdc, Repository: "ssh://git@" + bbdc + ":7999/OPS/infra.git"}, "https://" + bbdc + "/projects/OPS/repos/infra"},
		{repositoryInfo{Provider: "bitbucket-datacenter", Host: bbdc, Repository: "https://" + bbdc + "/OPS/infra.git"}, "https://" + bbdc + "/projects/OPS/repos/infra"},
		{repositoryInfo{Provider: "bitbucket-datacenter", Host: bbdc, Repository: "https://" + bbdc + ":7990/OPS/infra"}, "https://" + bbdc + ":7990/projects/OPS/repos/infra"},
		{repositoryInfo{Provider: "bitbucket", Repository: "https://bitbucket.org/acme/infra"}, "https://bitbucket.org/acme/infra"},
		{repositoryInfo{Provider: "bitbucket-datacenter", Repository: "OPS/infra"}, ""},
		{repositoryInfo{Provider: "bitbucket-datacenter", Host: bbdc, Repository: "OPS/sub/infra"}, ""},
		{repositoryInfo{Provider: "bitbucket-datacenter", Host: bbdc, Repository: "infra"}, ""},
		// A host links as the forge's operator reads it: lowercased, and a
		// provider's own spellings folded to its default host.
		{repositoryInfo{Provider: "github", Host: "www.github.com", Repository: "org/r"}, "https://github.com/org/r"},
		{repositoryInfo{Provider: "github", Host: "ssh.github.com", Repository: "org/r"}, "https://github.com/org/r"},
		{repositoryInfo{Host: "WWW.GitHub.com", Repository: "org/r"}, "https://github.com/org/r"},
		{repositoryInfo{Provider: "github", Host: "www.github.com", Repository: "github.com/org/r1"}, "https://github.com/org/r1"},
		{repositoryInfo{Provider: "github", Repository: "git@ssh.github.com:org/r.git"}, "https://github.com/org/r"},
		{repositoryInfo{Provider: "gitlab", Host: "www.gitlab.com", Repository: "grp/sub/r"}, "https://gitlab.com/grp/sub/r"},
		{repositoryInfo{Provider: "bitbucket", Host: "www.bitbucket.org", Repository: "acme/infra"}, "https://bitbucket.org/acme/infra"},
		{repositoryInfo{Provider: "gitea", Host: "Gitea.Example.com", Repository: "ops/app"}, "https://gitea.example.com/ops/app"},
		{repositoryInfo{Provider: "gitea", Host: "Gitea.Example.com", Repository: "gitea.example.com/ops/app"}, "https://gitea.example.com/ops/app"},
		{repositoryInfo{Provider: "bitbucket-datacenter", Host: "Bitbucket.Corp.Example", Repository: "OPS/infra"}, "https://bitbucket.corp.example/projects/OPS/repos/infra"},
	}
	for _, c := range cases {
		if got := repositoryURL(c.in); got != c.want {
			t.Errorf("repositoryURL(%+v) = %q, want %q", c.in, got, c.want)
		}
	}
}

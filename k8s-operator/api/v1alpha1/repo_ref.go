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

// repo_ref.go — one repository identity, parsed once, host first.
//
// This is the Go counterpart of `agents/platform/scripts/repo_ref.py`, and it
// exists for the same reason: `CleanRepoSlugWithOrg` used to discard the host
// and then count the slashes in what was left, so "exactly one slash" was
// standing in for a host check it never performed. That is why
// `git@gitlab.com:group/project` was admitted by the CRD, reduced to
// `group/project`, and written into the state ConfigMap as
// `https://github.com/group/project` — a different, valid repository on a forge
// the administrator did not name. `docs/designs/multi-forge-support.md` §3 has
// the census; #1085 is the admission half of the same defect.
//
// The two are counterparts, not a port: they agree on every shape an install
// produces, and knowingly differ on one. `ssh://git@github.com:owner/repo` — an
// scp path wearing a URL scheme — resolves here to `owner/repo` and in
// `repo_ref.py` to `repo`, because the Python side reads the non-numeric port
// slot as a port and drops it. Go's reading is git's; the Python side is the one
// to correct, and §9 step 3 is where it is touched.
//
// A `RepoRef` carries a host, possibly empty, and an opaque path of any depth.
// Depth is not checked here. "Exactly two segments" is a property of GitHub, so
// it belongs to a `GitProvider` (see gitprovider.go), which is what lets a
// GitLab `group/subgroup/project` parse without every caller learning about it.

import (
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"
)

const (
	// schemeSeparator introduces the authority in a URL, as opposed to the
	// scp-style `host:path` remote form that carries no scheme at all.
	schemeSeparator = "://"
	// pathSeparator separates repository path segments in every form parsed here.
	pathSeparator = "/"
	// gitSuffix is the optional suffix a clone URL carries and a repository name
	// does not.
	gitSuffix = ".git"
	// userInfoSeparator ends the `git@` part of a remote.
	userInfoSeparator = "@"
	// authoritySeparator divides host from port in a URL, and host from path in
	// the scp remote form — telling the two apart is what splitAuthority does.
	authoritySeparator = ":"
	// flagPrefix, leading a path segment, makes a CLI read the repository as an
	// option rather than an argument.
	flagPrefix = "-"
	// ipv6Open and ipv6Close bracket a literal address in a URL authority.
	ipv6Open  = "["
	ipv6Close = "]"
)

// repoSegmentRegex is one path segment. The class is the one every validator
// this file replaces already agreed on, matched with a single group so it
// cannot be driven into polynomial backtracking.
var repoSegmentRegex = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)

// repoPortRegex recognises a real port, which is how `https://github.com:443/o/r`
// is told apart from `ssh://git@github.com:owner/repo` — a scp path wearing a
// URL scheme, which git accepts and the CRD has always admitted.
var repoPortRegex = regexp.MustCompile(`^[0-9]*$`)

// allowedRepoSchemes is the set a repository URL may carry. `file://` and the
// rest are refused rather than ignored, because a scheme this list does not
// name is a value the administrator did not mean as a repository.
var allowedRepoSchemes = map[string]bool{
	"http":  true,
	"https": true,
	"git":   true,
	"ssh":   true,
}

// scpCapableSchemes are the schemes under which `host:path` is a remote git
// resolves rather than a malformed authority. http(s) is absent: a non-numeric
// port there is a typo, not a second syntax.
var scpCapableSchemes = map[string]bool{
	"git": true,
	"ssh": true,
}

// traversalSegments are filesystem instructions rather than names. The segment
// class permits "." and "-", so it matches ".." as happily as a real name.
var traversalSegments = map[string]bool{
	".":  true,
	"..": true,
}

// RepoRef is a parsed repository: a host, which is empty when the value stated
// none, and a path of any depth with no leading or trailing separator and no
// `.git` suffix.
//
// A parse result rather than a CRD field.
// +kubebuilder:object:generate=false
type RepoRef struct {
	Host string
	Path string
}

// Segments splits the path. A GitHub repository has exactly two; a GitLab
// project may have more.
func (r RepoRef) Segments() []string {
	return strings.Split(r.Path, pathSeparator)
}

// String renders the ref back as `host/path`, or as the bare path when the
// value named no host.
func (r RepoRef) String() string {
	if r.Host == "" {
		return r.Path
	}
	return r.Host + pathSeparator + r.Path
}

// URL renders the ref as an HTTPS clone URL. A hostless ref has nothing to
// build one from, so the caller resolves the host first — `GitProvider.Resolve`
// fills in the provider's default.
func (r RepoRef) URL() string {
	return "https://" + r.Host + pathSeparator + r.Path
}

// ParseRepoRef reads a repository identity out of a URL, an scp-style remote,
// or a bare path, and reports the host separately from the path.
//
// A schemeless slash-path is hostless, because inferring a host from it would
// read `my.org/repo` — a legal bare GitHub slug — as a host and a one-segment
// path. `GitProvider.Resolve` lifts a first segment that spells one of its own
// hosts; nothing else does.
func ParseRepoRef(value string) (RepoRef, error) {
	return parseRepoRef(value, nil)
}

func parseRepoRef(value string, schemelessHosts map[string]bool) (RepoRef, error) {
	text := strings.TrimSpace(value)
	if text == "" {
		return RepoRef{}, fmt.Errorf("empty repository")
	}
	if utf8.RuneCountInString(text) > MaxGitRepoURLLength {
		return RepoRef{}, fmt.Errorf("repository exceeds maximum length of %d characters", MaxGitRepoURLLength)
	}

	var host, path string
	if idx := strings.Index(text, schemeSeparator); idx != -1 {
		scheme := strings.ToLower(text[:idx])
		if !allowedRepoSchemes[scheme] {
			return RepoRef{}, fmt.Errorf("unsupported URL scheme %q; must be http, https, git, or ssh", scheme)
		}
		var err error
		if host, path, err = splitAuthority(text[idx+len(schemeSeparator):], scheme); err != nil {
			return RepoRef{}, err
		}
	} else if h, p, ok := splitSCPRemote(text); ok {
		host, path = h, p
	} else {
		path = text
	}

	path = trimRepoPath(path)
	if host == "" && len(schemelessHosts) > 0 {
		if first, rest, found := strings.Cut(path, pathSeparator); found && rest != "" &&
			schemelessHosts[strings.ToLower(first)] {
			host, path = first, rest
		}
	}

	if path == "" {
		return RepoRef{}, fmt.Errorf("empty repository")
	}
	for _, segment := range strings.Split(path, pathSeparator) {
		if !safeRepoSegment(segment) {
			return RepoRef{}, fmt.Errorf("invalid repository path segment %q", segment)
		}
	}
	return RepoRef{Host: strings.ToLower(host), Path: path}, nil
}

// splitAuthority separates host from path in everything after a URL's scheme.
//
// The awkward case is a port slot that is not a port. `ssh://git@github.com:owner/repo`
// is scp syntax carrying a scheme; git resolves it and the CRD has always
// admitted it, so under an scp-capable scheme the non-numeric "port" is put
// back on the front of the path rather than rejected. Under http(s) there is no
// such form — `https://github.com:evil/owner` is a malformed URL and nothing
// resolves it — so it is refused rather than silently read as a path segment.
func splitAuthority(rest, scheme string) (string, string, error) {
	authority, path, _ := strings.Cut(rest, pathSeparator)
	if path != "" {
		path = pathSeparator + path
	}
	if idx := strings.LastIndex(authority, userInfoSeparator); idx != -1 {
		authority = authority[idx+1:]
	}

	if strings.HasPrefix(authority, ipv6Open) {
		end := strings.Index(authority, ipv6Close)
		if end == -1 {
			return "", "", fmt.Errorf("malformed address literal in %q", authority)
		}
		return authority[:end+1], path, nil
	}

	host, port, found := strings.Cut(authority, authoritySeparator)
	if !found {
		return host, path, nil
	}
	if repoPortRegex.MatchString(port) {
		return host, path, nil
	}
	if !scpCapableSchemes[scheme] {
		return "", "", fmt.Errorf("invalid port %q in %q", port, authority)
	}
	return host, port + path, nil
}

// splitSCPRemote recognises the schemeless `[user@]host:path` remote form. A
// value with no colon, or whose colon falls after a slash, is an ordinary path.
func splitSCPRemote(text string) (string, string, bool) {
	colon := strings.Index(text, authoritySeparator)
	if colon == -1 {
		return "", "", false
	}
	if slash := strings.Index(text, pathSeparator); slash != -1 && slash < colon {
		return "", "", false
	}
	authority, path := text[:colon], text[colon+1:]
	if idx := strings.LastIndex(authority, userInfoSeparator); idx != -1 {
		authority = authority[idx+1:]
	}
	if authority == "" || path == "" {
		return "", "", false
	}
	return authority, path, true
}

// trimRepoPath drops surrounding separators and one trailing `.git`, in either
// order, so `/owner/repo.git/` and `owner/repo` come out the same.
func trimRepoPath(path string) string {
	path = strings.Trim(path, pathSeparator)
	path = strings.TrimSuffix(path, gitSuffix)
	return strings.Trim(path, pathSeparator)
}

// safeRepoSegment reports whether a segment is a name rather than an
// instruction to a filesystem or a CLI.
func safeRepoSegment(segment string) bool {
	return repoSegmentRegex.MatchString(segment) &&
		!traversalSegments[segment] &&
		!strings.HasPrefix(segment, flagPrefix)
}

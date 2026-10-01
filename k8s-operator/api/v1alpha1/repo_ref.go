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
// exists for the same reason: `CleanRepoSlugWithOrg` read a host only when it
// could see one, so "exactly one slash" was standing in for a host check it
// never performed. `gitlab.com/project` has one slash, so it was admitted by the
// CRD as owner `gitlab.com` and written into the state ConfigMap as
// `https://github.com/gitlab.com/project` — a path on a forge the administrator
// did not name. Parsing host first, and then holding the namespace to the
// provider's grammar, refuses it. "Repository identity" in
// `docs/designs/version-control-support.md` §2 has the census.
//
// The two are counterparts, not a port: they agree on every shape an install
// produces, and knowingly differ on one. A schemeless `www.github.com/o/r` or
// `ssh.github.com/o/r` is lifted here to a GitHub host, because the CRD has
// admitted it, while `repo_ref.py` lifts only `github.com` and reads the rest
// as a three-segment hostless path. The operator therefore never writes either
// spelling into the state ConfigMap, and does not count one written there by
// hand as the repository it seeds.
//
// They also read an scp remote's userinfo differently, as no install writes
// it. Here, as git does, the host starts after the authority's last `@`, and
// the authority ends at the first colon; `repo_ref.py` takes a user without an
// `@` and lets it run past a colon. So `a@b@github.com:o/r` is GitHub here and
// host `b@github.com` there, which the operator therefore does not count as
// the repository it seeds; and `user:token@github.com:o/r` is refused here and
// GitHub there, which leaves the agent reading the seeded entry beside it.
// An empty user is refused here — `@github.com:o/r`, and `@github.com/o/r` on a
// slash path — since `repo_ref.py` reads neither as GitHub.
//
// A `RepoRef` carries a host, possibly empty, and an opaque path of any depth.
// Depth is not checked here. "Exactly two segments" is a property of GitHub, so
// it belongs to a `GitProvider` (see gitprovider.go), which is what lets a
// GitLab `group/subgroup/project` parse without every caller learning about it.

import (
	"fmt"
	"regexp"
	"strconv"
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
	// authorityTerminators end a URL's authority for git, curl and Python's
	// urlsplit, so an `@` after one is not the end of userinfo:
	// `https://evil.com#@github.com/o/r` is a clone of evil.com.
	authorityTerminators = "#?"
	// authoritySeparator divides host from port in a URL, and host from path in
	// the scp remote form, which carries no scheme.
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

// lowerASCII lowers A-Z and nothing else. A host is compared ASCII-only:
// Unicode case mapping folds some non-ASCII runes onto ASCII letters (U+0130
// to `i`, U+212A to `k`), which would read `gİthub.com`, a different host on
// the wire, as github.com. repo_ref.py's str.lower() folds neither.
func lowerASCII(s string) string {
	return strings.Map(func(r rune) rune {
		if 'A' <= r && r <= 'Z' {
			return r + ('a' - 'A')
		}
		return r
	}, s)
}

// allowedRepoSchemes is the set a repository URL may carry. `file://` and the
// rest are refused rather than ignored, because a scheme this list does not
// name is a value the administrator did not mean as a repository.
var allowedRepoSchemes = map[string]bool{
	"http":  true,
	"https": true,
	"git":   true,
	"ssh":   true,
}

// traversalSegments are filesystem instructions rather than names. The segment
// class permits "." and "-", so it matches ".." as happily as a real name.
// `.git` addresses a clone's own git directory, and is no repository name.
var traversalSegments = map[string]bool{
	".":    true,
	"..":   true,
	".git": true,
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
// read `my.org/repo` — a namespace containing a dot, which some forges allow —
// as a host and a one-segment path. `GitProvider.Resolve` and
// `GitProvider.ParseRepoRef` lift a first segment that spells one of the
// provider's own hosts; nothing else does.
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
		scheme := lowerASCII(text[:idx])
		if !allowedRepoSchemes[scheme] {
			return RepoRef{}, fmt.Errorf("unsupported URL scheme %q; must be http, https, git, or ssh", scheme)
		}
		var err error
		if host, path, err = splitAuthority(text[idx+len(schemeSeparator):]); err != nil {
			return RepoRef{}, err
		}
	} else if h, p, ok := splitSCPRemote(text); ok {
		host, path = h, p
	} else {
		path = text
	}

	path = trimRepoPath(path)
	if host == "" && len(schemelessHosts) > 0 {
		// `git@github.com/owner/repo` is a user@host prefix on a slash path —
		// not scp syntax, since there is no colon — and the CRD has admitted
		// it since before provider dispatch. The user is dropped as it is for
		// a URL.
		// The rest is trimmed again because the path was trimmed as a whole:
		// `github.com//o/r` would otherwise keep an empty first segment that
		// `https://github.com//o/r` does not. repo_ref.py trims it too.
		first, rest, found := strings.Cut(path, pathSeparator)
		if rest = strings.Trim(rest, pathSeparator); found && rest != "" {
			// An empty user, or a colon ahead of the `@`, is no userinfo prefix
			// git would read either: `@github.com/o/r` and `:x@github.com/o/r`
			// are local paths to git and hostless to repo_ref.py, so neither
			// is lifted.
			if at := strings.LastIndex(first, userInfoSeparator); at > 0 && !strings.ContainsAny(first, authorityTerminators+authoritySeparator) {
				first = first[at+1:]
			}
			if schemelessHosts[lowerASCII(first)] {
				host, path = first, rest
			}
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
	return RepoRef{Host: lowerASCII(host), Path: path}, nil
}

// splitAuthority separates host from path in everything after a URL's scheme.
//
// The awkward case is a port slot that is not a port. Git splits a port off
// the host only when what follows the colon is one, 0 to 65535; anything else
// stays in the host, so `ssh://git@github.com:owner/repo` makes git connect to
// a host called `github.com:owner` and ask it for `/repo`. No scheme makes the
// scp form legal after `://`. Reading the slot as the start of the path would
// rewrite a URL git cannot clone into a repository nobody wrote, so it is
// refused, under every scheme.
//
// A `#` or `?` ends the authority before the first `/` does, so one in the
// authority slot is refused rather than read past: dropping everything up to
// the last `@` would turn `https://evil.com#@github.com/o/r`, a clone of
// evil.com, into github.com's `o/r`. repo_ref.py refuses it too.
func splitAuthority(rest string) (string, string, error) {
	authority, path, _ := strings.Cut(rest, pathSeparator)
	if path != "" {
		path = pathSeparator + path
	}
	if strings.ContainsAny(authority, authorityTerminators) {
		return "", "", fmt.Errorf("URL authority %q carries a %q or %q, which ends the authority for git before the host", authority, "#", "?")
	}
	// A bracket without its pair anywhere in the authority, the userinfo
	// included, is an invalid IPv6 URL to urlsplit, so repo_ref.py refuses
	// `https://[x@github.com/o/r`; reading its host would count an entry the
	// agent skips.
	if strings.Contains(authority, ipv6Open) != strings.Contains(authority, ipv6Close) {
		return "", "", fmt.Errorf("unpaired bracket in URL authority %q", authority)
	}
	if idx := strings.LastIndex(authority, userInfoSeparator); idx != -1 {
		// A paired bracket in the userinfo (`https://[TOKEN]@github.com/o/r`)
		// is refused too: urlsplit then requires the host after the `@` to be
		// an address literal, so the agent reads no repository from it.
		if strings.ContainsAny(authority[:idx], ipv6Open+ipv6Close) {
			return "", "", fmt.Errorf("bracket in URL userinfo of %q", authority)
		}
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
	// `https:///o/r` and `https://:443/o/r` stated an authority with no host
	// in it. Reading either as a value that stated none would hand it to the
	// install's default forge, the silent fallback this parser exists to
	// remove; repo_ref.py refuses both too.
	if host == "" {
		return "", "", fmt.Errorf("URL %q names no host", rest)
	}
	if !found || port == "" || isPortNumber(port) {
		return host, path, nil
	}
	return "", "", fmt.Errorf("invalid port %q in %q", port, authority)
}

// isPortNumber reports whether a run of digits fits in a TCP port.
func isPortNumber(digits string) bool {
	_, err := strconv.ParseUint(digits, 10, 16)
	return err == nil
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
	if idx := strings.LastIndex(authority, userInfoSeparator); idx == 0 {
		// `@github.com:o/r` names no user. repo_ref.py's user needs a
		// character, so it reads host `@github.com`; this is not a remote.
		return "", "", false
	} else if idx != -1 {
		authority = authority[idx+1:]
	}
	if authority == "" || path == "" {
		return "", "", false
	}
	return authority, path, true
}

// trimRepoPath drops surrounding separators and one trailing `.git`, in either
// order, so `/owner/repo.git/` and `owner/repo` come out the same.
//
// The suffix is dropped only from a name. A `.git` that is a whole segment
// stays for safeRepoSegment to refuse: dropping it would turn `owner/.git`
// into the one-segment `owner`, which a namespace then requalifies into
// `namespace/owner`, a repository nobody wrote.
func trimRepoPath(path string) string {
	path = strings.Trim(path, pathSeparator)
	if name := strings.TrimSuffix(path, gitSuffix); name != path && name != "" && !strings.HasSuffix(name, pathSeparator) {
		path = name
	}
	return strings.Trim(path, pathSeparator)
}

// safeRepoSegment reports whether a segment is a name rather than an
// instruction to a filesystem or a CLI.
func safeRepoSegment(segment string) bool {
	return repoSegmentRegex.MatchString(segment) &&
		!traversalSegments[segment] &&
		!strings.HasPrefix(segment, flagPrefix)
}

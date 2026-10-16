#!/usr/bin/env python3
"""The GitOps repository and the PlatformAgent, read as the operator reads them.

One copy of the rules run-branch.sh, agent-base-branch.sh and
run-gitops-pilot.sh share (gke-labs/kube-agents#1970), so the three cannot
disagree on which repository GITOPS_REPO is, which of the PlatformAgent's
spec.integration.repositories[] entries names it, or what the installed CRD
declares.

A repository resolves as the operator's GitProvider.Resolve resolves it for
the github provider (k8s-operator/api/v1alpha1/gitprovider.go over
repo_ref.go): a URL (http, https, git or ssh), an scp remote
(git@github.com:owner/name), or a schemeless path; github.com,
www.github.com and ssh.github.com are one host, and the only one accepted;
surrounding "/" and one trailing ".git" are dropped, in either order; a bare
name is qualified by the namespace given (the entry's, else its forge's); and
the result is owner/name, with GitHub's owner and name rules. A value the
operator refuses resolves to nothing. Two repositories are the same when their
owner/name match case-insensitively, as the operator compares the URLs it
renders.

Usage:
  gitops_repo.py slug <repository>
      Prints the repository's owner/name; exits 2, with the reason on
      stderr, when it is not a github.com repository.
  gitops_repo.py entry base|pin|unpin <repository> [<run branch>]
      Reads the PlatformAgent as JSON on stdin and finds its repositories[]
      entry with role gitops when that entry names <repository> on a github
      forge and the operator accepts it. Prints that entry's baseBranch (empty when unset); for pin
      and unpin, also the JSON patch that sets it to <run branch> or removes
      it, on a second line. Both patches test the entry's repository and role,
      so they never write to an entry that moved; pin's also tests the
      PlatformAgent's resourceVersion, and unpin's the value it removes. Exits
      NO_ENTRY when there is no such entry, and 2 when the input is not a
      PlatformAgent.
  gitops_repo.py crd-declares <path>...
      Reads the PlatformAgent CRD as JSON on stdin. Exits 0 when one served
      version declares every <path> (dotted, through an array's items:
      spec.integration.repositories.baseBranch), 1 when none does, and 2 when
      the input is not a CRD (what a failed kubectl leaves).
"""

import json
import re
import sys

NO_ENTRY = 3

# repo_ref.go's allowedRepoSchemes and gitprovider.go's githubHosts.
SCHEMES = ("http", "https", "git", "ssh")
GITHUB_HOSTS = ("github.com", "www.github.com", "ssh.github.com")
# common_types.go's githubOrgRegex, gitprovider.go's MaxGitHubRepoNameLength,
# and repo_ref.go's repoSegmentRegex.
GITHUB_NAMESPACE = re.compile(r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?")
GITHUB_NAME_MAX = 100
SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
GIT_SUFFIX = ".git"
# What repo_ref.go's splitAuthority refuses in an authority, and the largest
# port it reads as one.
AUTHORITY_TERMINATORS = "#?"
MAX_PORT = 65535
# The provider a forge names when it names none, and the role of the GitOps
# repository's entry.
GITHUB_PROVIDER = "github"
GITOPS_ROLE = "gitops"
# The other roles the operator accepts, and the deprecated alias's "no
# repository", which it refuses in a list (common_types.go).
OTHER_ROLES = ("managed", "context")
NO_REPOSITORY = "None"
REPOSITORY_POINTER = "/spec/integration/repositories/%d"


def ascii_lower(text):
    """Lowers A-Z only, as the operator compares hosts and schemes."""
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)


def safe_segment(segment):
    return bool(SEGMENT.fullmatch(segment)) and segment not in (".", "..", GIT_SUFFIX) and not segment.startswith("-")


def trim_repo_path(path):
    """Drops surrounding "/" and one trailing ".git" from a name, in either order."""
    path = path.strip("/")
    name = path[: -len(GIT_SUFFIX)] if path.endswith(GIT_SUFFIX) else path
    if name != path and name and not name.endswith("/"):
        path = name
    return path.strip("/")


def split_url(rest):
    """(host, path) after a URL's scheme, or None where the operator refuses the authority."""
    authority, _, path = rest.partition("/")
    if any(c in authority for c in AUTHORITY_TERMINATORS) or ("[" in authority) != ("]" in authority):
        return None
    user, at, host_port = authority.rpartition("@")
    if at:
        if "[" in user or "]" in user:
            return None
        authority = host_port
    if authority.startswith("["):
        # An address literal, which is no GitHub host.
        return None
    host, _, port = authority.partition(":")
    if not host or (port and not (re.fullmatch(r"[0-9]+", port) and int(port) <= MAX_PORT)):
        return None
    return host, path


def split_scp(text):
    """(host, path) of a [user@]host:path remote, or None for anything else."""
    colon, slash = text.find(":"), text.find("/")
    if colon == -1 or slash != -1 and slash < colon:
        return None
    authority, path = text[:colon], text[colon + 1 :]
    at = authority.rfind("@")
    if at == 0:
        return None
    if at != -1:
        authority = authority[at + 1 :]
    return (authority, path) if authority and path else None


def resolve(repository, namespace=""):
    """owner/name of a repository on github.com, or None where the operator refuses it."""
    text = repository.strip()
    if not text:
        return None
    host, path = "", text
    if "://" in text:
        scheme, _, rest = text.partition("://")
        parts = split_url(rest) if ascii_lower(scheme) in SCHEMES else None
        if parts is None:
            return None
        host, path = parts
    else:
        host, path = split_scp(text) or ("", text)
    path = trim_repo_path(path)
    if not host:
        # A schemeless path whose first segment spells a GitHub host (after a
        # user@ the operator would drop) names that host.
        first, sep, rest = path.partition("/")
        rest = rest.strip("/")
        if sep and rest:
            at = first.rfind("@")
            if at > 0 and not any(c in first for c in AUTHORITY_TERMINATORS + ":"):
                first = first[at + 1 :]
            if ascii_lower(first) in GITHUB_HOSTS:
                host, path = first, rest
    if not path or not all(safe_segment(s) for s in path.split("/")):
        return None
    if host and ascii_lower(host) not in GITHUB_HOSTS:
        return None
    if not host:
        if text.endswith("/") and ascii_lower(text.strip("/")) in GITHUB_HOSTS:
            # A host and no repository.
            return None
        first, sep, _ = path.partition("/")
        if sep and "." in first and not GITHUB_NAMESPACE.fullmatch(first):
            # Another forge's host.
            return None
        if not sep:
            namespace = namespace.strip()
            if not namespace:
                return None
            path = namespace.strip("/") + "/" + path
    segments = path.split("/")
    if len(segments) != 2 or not all(safe_segment(s) for s in segments):
        return None
    owner, name = segments
    if name.endswith(GIT_SUFFIX) or len(name) > GITHUB_NAME_MAX or not GITHUB_NAMESPACE.fullmatch(owner):
        return None
    return path


def github_forges(integration):
    """{name: namespace} of the forges the operator reads as valid github forges."""
    forges, seen = {}, set()
    for forge in integration.get("forges") or []:
        name = str(forge.get("name") or "").strip()
        if name in seen:
            continue
        seen.add(name)
        provider = ascii_lower(str(forge.get("provider") or "").strip()) or GITHUB_PROVIDER
        host = ascii_lower(str(forge.get("host") or "").strip())
        namespace = str(forge.get("namespace") or "").strip()
        if provider == GITHUB_PROVIDER and (not host or host in GITHUB_HOSTS) and (
                not namespace or GITHUB_NAMESPACE.fullmatch(namespace)):
            forges[name] = namespace
    return forges


def entry_repository(entry, forges):
    """owner/name of a repositories[] entry on a github forge, or None where the operator refuses its own fields."""
    forge = str(entry.get("forge") or "").strip()
    namespace = str(entry.get("namespace") or "").strip()
    repository = str(entry.get("repository") or "").strip()
    if forge not in forges or namespace and not GITHUB_NAMESPACE.fullmatch(namespace) or repository == NO_REPOSITORY:
        return None
    return resolve(repository, namespace or forges[forge])


def gitops_entry(integration, repository):
    """(index, entry) of the accepted repositories[] entry with role gitops when it names repository, or None.

    As ResolvedIntegration.check() reads them: only the first entry with role
    gitops can be accepted, and it is refused for its own fields, and for a
    repository an earlier accepted entry already declares.
    """
    slug = resolve(repository)
    if slug is None:
        return None
    forges = github_forges(integration)
    declared = set()
    for i, entry in enumerate(integration.get("repositories") or []):
        role = str(entry.get("role") or "").strip()
        name = entry_repository(entry, forges)
        if role == GITOPS_ROLE:
            if name is not None and name.lower() == slug.lower() and name.lower() not in declared:
                return i, entry
            return None
        if role in OTHER_ROLES and name is not None:
            declared.add(name.lower())
    return None


def entry_main(mode, repository, run):
    try:
        cr = json.load(sys.stdin)
        rv = cr["metadata"]["resourceVersion"]
        integration = cr["spec"].get("integration") or {}
    except (ValueError, KeyError, TypeError, AttributeError):
        return 2
    found = gitops_entry(integration, repository)
    if found is None:
        return NO_ENTRY
    i, entry = found
    at = REPOSITORY_POINTER % i
    guard = [{"op": "test", "path": at + "/repository", "value": entry["repository"]},
             {"op": "test", "path": at + "/role", "value": entry["role"]}]
    print(entry.get("baseBranch", ""))
    if mode == "pin":
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": rv}] + guard + [
            {"op": "add", "path": at + "/baseBranch", "value": run}]
    elif mode == "unpin":
        patch = guard + [{"op": "test", "path": at + "/baseBranch", "value": run},
                         {"op": "remove", "path": at + "/baseBranch"}]
    else:
        return 0
    print(json.dumps(patch, separators=(",", ":")))
    return 0


def declares(schema, path):
    node = schema
    for key in path.split("."):
        node = (node.get("items") or node).get("properties", {}).get(key)
        if not isinstance(node, dict):
            return False
    return True


def crd_declares_main(paths):
    try:
        versions = json.load(sys.stdin)["spec"]["versions"]
        schemas = [v.get("schema", {}).get("openAPIV3Schema", {}) for v in versions if v.get("served")]
    except (ValueError, KeyError, TypeError, AttributeError):
        return 2
    return 0 if any(all(declares(s, p) for p in paths) for s in schemas) else 1


def main(argv):
    if len(argv) == 2 and argv[0] == "slug":
        slug = resolve(argv[1])
        if slug is None:
            sys.stderr.write("gitops_repo: '%s' is not a github.com repository (owner/name) the operator accepts\n" % argv[1])
            return 2
        print(slug)
        return 0
    if len(argv) in (3, 4) and argv[0] == "entry" and argv[1] in ("base", "pin", "unpin"):
        return entry_main(argv[1], argv[2], argv[3] if len(argv) == 4 else "")
    if len(argv) >= 2 and argv[0] == "crd-declares":
        return crd_declares_main(argv[1:])
    sys.stderr.write("usage: gitops_repo.py slug <repository> | entry base|pin|unpin <repository> [<run branch>]"
                     " | crd-declares <path>...\n")
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

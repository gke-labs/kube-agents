#!/usr/bin/env python3
# Script to delete a package version by tag from GHCR.
# Requires GITHUB_TOKEN or CR_PAT with delete:packages scope.

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error

def get_headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }

def get_versions(url, token):
    req = urllib.request.Request(url, headers=get_headers(token))
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Error fetching versions: {e.code} {e.reason}", file=sys.stderr)
        try:
             print(e.read().decode(), file=sys.stderr)
        except:
             pass
        return None

def delete_version(url, token):
    req = urllib.request.Request(url, headers=get_headers(token), method="DELETE")
    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 204:
                return True
    except urllib.error.HTTPError as e:
        print(f"Error deleting version: {e.code} {e.reason}", file=sys.stderr)
        try:
             print(e.read().decode(), file=sys.stderr)
        except:
             pass
    return False

def main():
    parser = argparse.ArgumentParser(description="Delete a GHCR package version by tag.")
    parser.add_argument("--owner", required=True, help="GitHub owner (user or org)")
    parser.add_argument("--package", required=True, help="Package name (e.g., kube-agents/k8s-operator)")
    parser.add_argument("--tag", required=True, help="Tag to delete")
    parser.add_argument("--token", help="GitHub PAT (defaults to GITHUB_TOKEN or CR_PAT env var)")
    parser.add_argument("--org", action="store_true", help="Set this flag if the owner is an organization")

    args = parser.parse_args()

    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("CR_PAT")
    if not token:
        print("Error: GitHub token must be provided via --token or GITHUB_TOKEN/CR_PAT environment variables.", file=sys.stderr)
        sys.exit(1)

    # URL-encode package name (slashes become %2F)
    package_encoded = urllib.parse.quote(args.package, safe='')

    if args.org:
        list_url = f"https://api.github.com/orgs/{args.owner}/packages/container/{package_encoded}/versions"
    else:
        # Note: 'user' endpoint works for the authenticated user's packages
        list_url = f"https://api.github.com/user/packages/container/{package_encoded}/versions"

    print(f"Fetching versions for '{args.package}'...")
    versions = get_versions(list_url, token)
    
    if versions is None:
        print("Could not retrieve versions. Double check your token permissions and package name.", file=sys.stderr)
        if not args.org:
            print("If this package belongs to an organization, try adding the --org flag.", file=sys.stderr)
        sys.exit(1)

    target_version = None
    for v in versions:
        metadata = v.get("metadata", {})
        container = metadata.get("container", {})
        tags = container.get("tags", [])
        if args.tag in tags:
            target_version = v
            break

    if not target_version:
        print(f"Error: No version found with tag '{args.tag}'", file=sys.stderr)
        sys.exit(1)

    version_id = target_version["id"]
    print(f"Found version '{args.tag}' with ID: {version_id}")

    if args.org:
        delete_url = f"https://api.github.com/orgs/{args.owner}/packages/container/{package_encoded}/versions/{version_id}"
    else:
        delete_url = f"https://api.github.com/user/packages/container/{package_encoded}/versions/{version_id}"

    print(f"Deleting version {version_id}...")
    if delete_version(delete_url, token):
        print(f"Successfully deleted tag '{args.tag}' (ID: {version_id})")
    else:
        print(f"Failed to delete tag '{args.tag}'", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

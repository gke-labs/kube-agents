#!/usr/bin/env bash
# ==============================================================================
# Mirror every container image an install of kube-agents pulls into a registry
# of your own.
# ==============================================================================
# Environments that only permit images from an approved registry need a copy of
# each image in `images.json` before any of the install paths (provisioning
# scripts, Helm chart, Terraform) can be pointed at it. This script is that
# copy step; `images.json` is the source of truth for what has to be copied, so
# an image added there is mirrored here without editing this file.
#
# Destination naming is flat: "<prefix>/<the inventory entry's .name>".
#
#   quay.io/jetstack/cert-manager-controller:v1.21.1
#     -> ${MIRROR_THIRD_PARTY_PREFIX}/cert-manager-controller:v1.21.1
#
# The name, not the repository's trailing segment. For most entries they are
# the same word, which is why this is easy to state wrong; where they are not,
# the name wins — docker.io/pgvector/pgvector lands as "<prefix>/hindsight-
# postgresql", after the entry that describes what it is for rather than after
# the upstream image it happens to be.
#
# That flat layout is what the rest of the project assumes of a mirror — the
# operator derives the credential-proxy reference from the agent reference by
# swapping the last path element (resolveCredentialProxyImage in
# k8s-operator/internal/controller/platformagent_manifests.go), and the
# provisioning scripts build every default as "<prefix>/<name>" through
# third_party_image in scripts/installer/common.sh.
#
# The Helm chart is the one consumer that cannot read this file, so it rewrites
# onto the repository's trailing segment instead (kube-agents.imageRepository).
# The two agree only while every image the chart renders has .name equal to that
# segment, which check 3c in hack/check-image-inventory.sh now enforces rather
# than leaving to whoever adds the next entry.
#
# Usage:
#   MIRROR_PREFIX=registry.example.com/kube-agents ./scripts/mirror_images.sh
#   MIRROR_PREFIX=... ./scripts/mirror_images.sh --dry-run
#
# Environment:
#   MIRROR_PREFIX              Required. Destination prefix for the images
#                              built from this repo.
#   MIRROR_THIRD_PARTY_PREFIX  Destination prefix for images this project does
#                              not build. Defaults to MIRROR_PREFIX.
#   IMAGE_TAG                  Tag to copy for the first-party images, whose
#                              tag tracks the release. Defaults to "latest".
#   INCLUDE                    Comma-separated origins to copy. Defaults to
#                              "first-party,third-party" — what a running
#                              install pulls. Add "build-time" when you rebuild
#                              the images from source instead of copying them.
# ==============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGES_JSON="${IMAGES_JSON:-${REPO_ROOT}/images.json}"

DRY_RUN="${DRY_RUN:-0}"
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      # The banner is the help text. Stop at the first line that is not a
      # comment rather than at a line number, so editing the banner cannot
      # spill code into --help the way a stale range does.
      awk 'NR == 1 { next } !/^#/ { exit } { print }' "${BASH_SOURCE[0]}" |
        sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown argument: ${arg}" >&2
      exit 2
      ;;
  esac
done

MIRROR_PREFIX="${MIRROR_PREFIX:-}"
MIRROR_THIRD_PARTY_PREFIX="${MIRROR_THIRD_PARTY_PREFIX:-$MIRROR_PREFIX}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
INCLUDE="${INCLUDE:-first-party,third-party}"

if [ -z "$MIRROR_PREFIX" ]; then
  echo "❌ MIRROR_PREFIX is required, e.g. MIRROR_PREFIX=registry.example.com/kube-agents" >&2
  exit 1
fi
case "$MIRROR_PREFIX" in
  *"://"*)
    echo "❌ MIRROR_PREFIX must be a bare registry path without a scheme (got '${MIRROR_PREFIX}')." >&2
    exit 1
    ;;
esac
# The same rule for the third-party prefix, which is a separate input whenever
# it is not just defaulting to MIRROR_PREFIX: a scheme here would be caught by
# crane at copy time instead, halfway through the mirror.
case "$MIRROR_THIRD_PARTY_PREFIX" in
  *"://"*)
    echo "❌ MIRROR_THIRD_PARTY_PREFIX must be a bare registry path without a scheme (got '${MIRROR_THIRD_PARTY_PREFIX}')." >&2
    exit 1
    ;;
esac
MIRROR_PREFIX="${MIRROR_PREFIX%/}"
MIRROR_THIRD_PARTY_PREFIX="${MIRROR_THIRD_PARTY_PREFIX%/}"

command -v jq >/dev/null 2>&1 || {
  echo "❌ jq is required to read ${IMAGES_JSON}." >&2
  exit 1
}
[ -f "$IMAGES_JSON" ] || {
  echo "❌ ${IMAGES_JSON} not found." >&2
  exit 1
}

# ─── Copy backend ─────────────────────────────────────────────────────────────
# crane first: it copies the manifest list as-is, so a multi-arch image stays
# multi-arch. `docker pull` resolves to the host's single architecture, which
# silently produces an amd64-only mirror on an arm64 workstation — hence the
# warning rather than a quiet fallback.
COPY_TOOL=""
if command -v crane >/dev/null 2>&1; then
  COPY_TOOL="crane"
elif command -v skopeo >/dev/null 2>&1; then
  COPY_TOOL="skopeo"
elif command -v docker >/dev/null 2>&1; then
  COPY_TOOL="docker"
fi

if [ -z "$COPY_TOOL" ] && [ "$DRY_RUN" -ne 1 ]; then
  echo "❌ Need one of crane, skopeo, or docker on PATH to copy images." >&2
  echo "   crane: https://github.com/google/go-containerregistry/tree/main/cmd/crane" >&2
  exit 1
fi
if [ "$COPY_TOOL" = "docker" ] && [ "$DRY_RUN" -ne 1 ]; then
  echo "⚠️  Falling back to docker; multi-arch images will be flattened to this host's"
  echo "    architecture. That also changes their digests, so any reference pinned by"
  echo "    digest — HERMES_AGENT_TAG in tags.env is one — will not resolve against the"
  echo "    copy. Install crane or skopeo to mirror the manifest list byte-for-byte."
fi

# Drop the tag from a reference that carries both a tag and a digest, leaving
# "repo@sha256:...". Four inventory entries pin that way — both Hindsight
# images, busybox, and the Hermes base (through tags.env) — and skopeo's
# docker:// transport refuses the combined form outright ("Docker references
# with both a tag and digest are currently not supported"), so a skopeo host
# would fail exactly those copies and mirror everything else. The digest half
# is the one to keep: it names the same manifest the tag resolved to, whereas
# dropping the digest would copy whatever the tag points at today and silently
# unpin the image. crane accepts the combined form, so only the skopeo path
# needs this.
digest_only_ref() {
  local ref=$1
  case "$ref" in
    *@*)
      # The tag separator is the last colon before the "@": a registry port
      # ("reg:5000/foo:1.0@sha256:...") puts an earlier colon in the string.
      local tagged="${ref%@*}"
      echo "${tagged%:*}@${ref#*@}"
      ;;
    *) echo "$ref" ;;
  esac
}

copy_image() {
  local src=$1 dst=$2
  case "$COPY_TOOL" in
    crane) crane copy "$src" "$dst" ;;
    skopeo) skopeo copy --all "docker://$(digest_only_ref "$src")" "docker://${dst}" ;;
    docker)
      docker pull "$src" && docker tag "$src" "$dst" && docker push "$dst"
      ;;
  esac
}

# ─── Reference resolution ─────────────────────────────────────────────────────

# Resolve the tag for one entry: a literal `tag`, a `tagFrom` pointer into
# another file (the Hermes pin stays in tags.env, which CI already reads), or
# `tagPolicy: release`, which follows IMAGE_TAG.
resolve_tag() {
  local entry=$1
  local tag tag_file tag_key policy

  tag="$(jq -r '.tag // empty' <<<"$entry")"
  if [ -n "$tag" ]; then
    echo "$tag"
    return 0
  fi

  tag_file="$(jq -r '.tagFrom.file // empty' <<<"$entry")"
  if [ -n "$tag_file" ]; then
    tag_key="$(jq -r '.tagFrom.key' <<<"$entry")"
    tag="$(sed -n "s/^${tag_key}=//p" "${REPO_ROOT}/${tag_file}" | tail -n1)"
    if [ -z "$tag" ]; then
      echo "❌ ${tag_key} not found in ${tag_file}" >&2
      return 1
    fi
    echo "$tag"
    return 0
  fi

  policy="$(jq -r '.tagPolicy // empty' <<<"$entry")"
  if [ "$policy" = "release" ]; then
    echo "$IMAGE_TAG"
    return 0
  fi

  echo "❌ entry has no tag, tagFrom, or tagPolicy" >&2
  return 1
}

# ─── Copy plan ────────────────────────────────────────────────────────────────
print_plan_header() {
  echo ""
  echo "Mirroring kube-agents images"
  echo "  source of truth : ${IMAGES_JSON#"${REPO_ROOT}/"}"
  echo "  first-party  -> : ${MIRROR_PREFIX}"
  echo "  other images -> : ${MIRROR_THIRD_PARTY_PREFIX}"
  echo "  release tag     : ${IMAGE_TAG}"
  echo "  origins         : ${INCLUDE}"
  [ "$DRY_RUN" -eq 1 ] && echo "  mode            : dry run (nothing is copied)"
  [ "$DRY_RUN" -ne 1 ] && echo "  copy tool       : ${COPY_TOOL}"
  echo ""
}

failed=()
copied=0
skipped_origins=()

print_plan_header

while IFS= read -r entry; do
  name="$(jq -r '.name' <<<"$entry")"
  origin="$(jq -r '.origin' <<<"$entry")"
  repository="$(jq -r '.repository' <<<"$entry")"

  if [[ ",${INCLUDE}," != *",${origin},"* ]]; then
    skipped_origins+=("$origin")
    continue
  fi

  if ! tag="$(resolve_tag "$entry")"; then
    echo "❌ ${name}: could not resolve a tag" >&2
    failed+=("$name")
    continue
  fi

  src="${repository}:${tag}"

  # A tag pinned by digest ("v1.2.3@sha256:...") reads fine as a source but
  # cannot name a destination — you push to a tag, never to a digest. Keep the
  # human-readable half for the destination and let the digest pin the source.
  dst_tag="${tag%%@*}"

  if [ "$origin" = "first-party" ]; then
    dst="${MIRROR_PREFIX}/${name}:${dst_tag}"
  else
    dst="${MIRROR_THIRD_PARTY_PREFIX}/${name}:${dst_tag}"
  fi

  echo "  ${src}"
  echo "    -> ${dst}"

  if [ "$DRY_RUN" -eq 1 ]; then
    continue
  fi

  if copy_image "$src" "$dst"; then
    copied=$((copied + 1))
  else
    failed+=("$name")
  fi
done < <(jq -c '.images[]' "$IMAGES_JSON")

# ─── Result ───────────────────────────────────────────────────────────────────
echo ""
if [ ${#skipped_origins[@]} -gt 0 ]; then
  # Name what was left out. A mirror that is quietly missing the build-time
  # bases looks complete right up until someone tries to rebuild from source.
  echo "Skipped origins not in INCLUDE: $(printf '%s\n' "${skipped_origins[@]}" | sort -u | paste -sd, -)"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "✅ Dry run complete — nothing was copied."
  exit 0
fi

if [ ${#failed[@]} -gt 0 ]; then
  echo "❌ ${#failed[@]} image(s) failed to copy: ${failed[*]}" >&2
  echo "   The mirror is incomplete; the install will fall back to ImagePullBackOff." >&2
  exit 1
fi

echo "✅ Mirrored ${copied} image(s) successfully."

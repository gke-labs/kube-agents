#!/usr/bin/env bash
# Verifies that images.json — the inventory `make mirror-images` copies from —
# still describes the images an install actually pulls. Two ways it can be
# wrong, and both have happened:
#
#   1. A pin drifts. The chart sat on LiteLLM v1.92.0 for a release while the
#      kustomize base it claimed to mirror was on v1.95.0. A mirror populated
#      from the inventory then lacks the tag the install asks for.
#   2. A new image appears with no inventory entry. Nothing copies it, and the
#      air-gapped install fails at pull time on a registry nobody approved.
#
# And one that has not happened yet: the Go builder pin matches the inventory
# but no longer satisfies k8s-operator/go.mod, so the image build fails, or a
# base without GOTOOLCHAIN=local builds with a toolchain nobody pinned.
#
# Run via `make images-check`; CI runs it in validate.yml.
set -euo pipefail
cd "$(dirname "$0")/.."

INVENTORY=images.json
readonly GO_MOD=k8s-operator/go.mod
readonly GOLANG_IMAGE_ARG=GOLANG_IMAGE
readonly GOTOOLCHAIN_PIN='ENV GOTOOLCHAIN=local'
MIRROR=registry.example.invalid/mirror

# githubMinter.org and githubMinter.repo are required when the minter is
# enabled. Nothing the render produces depends on their values.
readonly MINTER_ORG=ci-org
readonly MINTER_REPO=ci-repo

# Which render a check-3 failure came from. Each is written to be the subject
# of the sentence the failure opens with, because "the chart renders X" is not
# actionable when four configurations render.
readonly LABEL_DEFAULT="a default install"
readonly LABEL_DEFAULT_MINTER="a default install with githubMinter enabled"
readonly LABEL_MIRRORED="a mirrored install"
readonly LABEL_MIRRORED_MINTER="a mirrored install with githubMinter enabled"

# The env vars whose values are image references, matched on the variable's
# name. Matching on the shape of the value instead — a quoted string with a
# slash and a colon in it — cannot tell an image from any other reference-like
# value: githubMinter's ISSUER_ALLOWLIST is two https:// URLs joined by a comma
# and matches that shape exactly, so it was reported as an image rendered
# outside the mirror (#1139). The chart emits three names the pattern below
# catches: PLATFORM_AGENT_IMAGE, AGENT_SANDBOX_IMAGE and FLUENT_BIT_IMAGE.
readonly IMAGE_ENV_NAME_RE='^[[:space:]]*-[[:space:]]+name:[[:space:]]*[A-Z0-9_]*_IMAGE[[:space:]]*$'
readonly VALUE_FIELD_RE='^[[:space:]]*value:[[:space:]]*'

status=0

fail() {
  echo "ERROR: $1" >&2
  status=1
}

for tool in jq helm; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "ERROR: $tool is required to check the image inventory." >&2
    exit 1
  }
done

# Docker Hub official images are pullable by bare name; the inventory spells
# out the registry so mirroring has an unambiguous source. Compare on the
# normalised form so both spellings agree.
normalise() {
  local ref=$1
  ref=${ref#docker.io/library/}
  ref=${ref#docker.io/}
  echo "$ref"
}

pin_of() {
  jq -r --arg n "$1" '.images[] | select(.name == $n) | .tag' "$INVENTORY"
}

# The pin for a repository, or empty when the inventory carries no fixed tag
# for it — first-party images are tagPolicy "release" and take the tag of
# whatever release is being installed, so there is nothing to compare against.
pin_of_repo() {
  jq -r --arg r "$1" '.images[] | select(.repository == $r) | .tag // empty' "$INVENTORY"
}

repo_of() {
  jq -r --arg n "$1" '.images[] | select(.name == $n) | .repository' "$INVENTORY"
}

# Every inventory name carried by a repository. Plural because nothing stops
# two entries sharing a repository, and a mirror populated from the inventory
# would then hold the image under both names.
names_of_repo() {
  jq -r --arg r "$1" '.images[] | select(.repository == $r) | .name' "$INVENTORY"
}

# A Dockerfile's `ARG FOO=bar` default, or empty if the arg has no default.
arg_default() {
  sed -n "s/^ARG $2=\(.*\)$/\1/p" "$1" | head -n1
}

# ---------------------------------------------------------------------------
# 0. Origins. Both consumers filter on this field and neither complains about a
#    value it does not recognise: mirror_images.sh folds an unknown origin into
#    its "skipped" line, and generate_docs.py's section loop drops it. A typo
#    like "third_party" therefore removes an image from the mirror AND from the
#    docs while everything still reports success — failure mode #2 above,
#    arriving quietly.
# ---------------------------------------------------------------------------
unknown_origins="$(jq -r '.images[].origin | select(. != "first-party" and . != "third-party" and . != "build-time")' "$INVENTORY" | sort -u)"
[ -z "$unknown_origins" ] ||
  fail "$INVENTORY has unrecognised origin(s): $(tr '\n' ' ' <<<"$unknown_origins")— must be first-party, third-party, or build-time, or the entry is silently neither mirrored nor documented."

# ---------------------------------------------------------------------------
# 1. Build-time base images: the ARG defaults are what a plain `docker build`
#    pulls, so they are the pins the inventory must mirror.
# ---------------------------------------------------------------------------
check_base_image() {
  local name=$1 dockerfile=$2 image_arg=$3 version_arg=$4
  local want_repo want_tag got_repo got_tag
  want_repo="$(normalise "$(repo_of "$name")")"
  want_tag="$(pin_of "$name")"
  got_repo="$(normalise "$(arg_default "$dockerfile" "$image_arg")")"
  got_tag="$(arg_default "$dockerfile" "$version_arg")"

  [ "$got_repo" = "$want_repo" ] ||
    fail "$dockerfile: ARG $image_arg defaults to '${got_repo:-<unset>}', but $INVENTORY has '$want_repo' for '$name'."
  [ "$got_tag" = "$want_tag" ] ||
    fail "$dockerfile: ARG $version_arg defaults to '${got_tag:-<unset>}', but $INVENTORY pins '$name' at '$want_tag'."
}

check_base_image envoy deploy/docker/Dockerfile ENVOY_IMAGE ENVOY_VERSION
check_base_image golang deploy/docker/Dockerfile GOLANG_IMAGE GOLANG_VERSION
check_base_image golang k8s-operator/Dockerfile GOLANG_IMAGE GOLANG_VERSION
check_base_image distroless-static k8s-operator/Dockerfile DISTROLESS_IMAGE DISTROLESS_VERSION
check_base_image python examples/inference-replay/replay-proxy/Dockerfile PYTHON_IMAGE PYTHON_VERSION
check_base_image python deploy/sandbox/Dockerfile PYTHON_IMAGE PYTHON_VERSION

# The Go builder and k8s-operator/go.mod's `go` directive must name the same
# major.minor: a builder behind the directive fails the image build (the
# official golang image sets GOTOOLCHAIN=local, and the Dockerfiles repeat it so
# a substituted base cannot quietly download a newer toolchain instead, #1138),
# and a builder ahead of it ships a compiler the directive does not name. A tag
# that names only a major.minor (`1.27-alpine`) pulls the newest patch from
# Docker Hub, so equality is the whole check; a mirror populated by
# `make mirror-images` freezes whichever patch it copied, and a patch-pinned tag
# (`1.27.3-alpine`) is the answer when the directive outruns that copy. A tag
# that names a patch or pre-release must also sit at or above the directive.
# The ENV line is checked too, between the FROM that opens the Go stage and its
# first RUN: it is the only guard left once a mirror has frozen the patch, and
# nothing else would notice a reshuffle moving it out of that stage or below the
# `go build` it has to precede.
check_go_directive() {
  local dockerfile=$1 version_arg=$2
  local builder_tag directive builder_ver builder_mm directive_mm
  awk -v img="\${$GOLANG_IMAGE_ARG}" '/^FROM / { in_stage = index($0, img) > 0; next } /^RUN / { in_stage = 0 } in_stage' "$dockerfile" |
    grep -qx "$GOTOOLCHAIN_PIN" ||
    fail "$dockerfile: no line exactly '$GOTOOLCHAIN_PIN' between the FROM \${$GOLANG_IMAGE_ARG} line and that stage's first RUN, so a substituted $GOLANG_IMAGE_ARG without that default downloads a toolchain the pin does not name instead of failing."
  builder_tag="$(arg_default "$dockerfile" "$version_arg")"
  directive="$(sed -n 's/^go[[:space:]][[:space:]]*\([0-9][0-9A-Za-z.]*\).*$/\1/p' "$GO_MOD" | head -n1)"
  [ -n "$directive" ] || {
    fail "$GO_MOD has no 'go' directive, so nothing pins the toolchain the operator builds with."
    return
  }
  builder_ver="$(sed -n 's/^\([0-9][0-9A-Za-z.]*\).*$/\1/p' <<<"$builder_tag")"
  builder_mm="$(sed -n 's/^\([0-9][0-9]*\.[0-9][0-9]*\).*$/\1/p' <<<"$builder_ver")"
  directive_mm="$(sed -n 's/^\([0-9][0-9]*\.[0-9][0-9]*\).*$/\1/p' <<<"$directive")"
  [ -n "$builder_mm" ] || {
    fail "$dockerfile: ARG $version_arg defaults to '${builder_tag:-<unset>}', which does not name a Go major.minor, so nothing ties the builder to the 'go $directive' directive in $GO_MOD."
    return
  }
  [ "$builder_mm" = "$directive_mm" ] || {
    fail "$dockerfile: ARG $version_arg defaults to '$builder_tag' (Go $builder_mm), but $GO_MOD says 'go $directive' (Go $directive_mm). Move both together: a go.mod ahead of the builder fails the image build, a builder ahead of go.mod ships a compiler the directive does not name."
    return
  }
  [ "$builder_ver" = "$builder_mm" ] ||
    [ "$(printf '%s\n%s\n' "$directive" "$builder_ver" | sort -V | head -n1)" = "$directive" ] ||
    fail "$dockerfile: ARG $version_arg defaults to '$builder_tag' (Go $builder_ver), below the 'go $directive' floor in $GO_MOD, so the image build fails under $GOTOOLCHAIN_PIN."
}

check_go_directive deploy/docker/Dockerfile GOLANG_VERSION
check_go_directive k8s-operator/Dockerfile GOLANG_VERSION

# hermes-agent is the one base image whose tag lives outside the Dockerfile —
# the release workflows read tags.env — so the inventory points at that file
# rather than copying the value. Check the pointer still resolves.
hermes_repo="$(normalise "$(arg_default deploy/docker/Dockerfile HERMES_AGENT_IMAGE)")"
[ "$hermes_repo" = "$(normalise "$(repo_of hermes-agent)")" ] ||
  fail "deploy/docker/Dockerfile: ARG HERMES_AGENT_IMAGE defaults to '$hermes_repo', but $INVENTORY has '$(repo_of hermes-agent)'."

jq -r '.images[] | select(.tagFrom) | "\(.name)\t\(.tagFrom.file)\t\(.tagFrom.key)"' "$INVENTORY" |
  while IFS=$'\t' read -r name file key; do
    [ -f "$file" ] || {
      echo "ERROR: $INVENTORY: '$name' takes its tag from '$file', which does not exist." >&2
      exit 1
    }
    grep -q "^${key}=" "$file" || {
      echo "ERROR: $INVENTORY: '$name' takes its tag from ${file}:${key}, which is not set there." >&2
      exit 1
    }
  done || status=1

# ---------------------------------------------------------------------------
# 2. The fluent-bit pin compiled into the operator. It is the only image the
#    operator falls back to without an env var, so a drift here mirrors the
#    wrong tag with nothing to catch it at render time.
# ---------------------------------------------------------------------------
want_fluent="$(normalise "$(repo_of fluent-bit)"):$(pin_of fluent-bit)"
got_fluent="$(sed -n 's/.*fallbackFluentBitImage = "\(.*\)".*/\1/p' k8s-operator/internal/controller/manifest_helpers.go)"
[ "$(normalise "$got_fluent")" = "$want_fluent" ] ||
  fail "k8s-operator/internal/controller/manifest_helpers.go: fallbackFluentBitImage is '$got_fluent', but $INVENTORY has '$want_fluent'."

# ---------------------------------------------------------------------------
# 3. The chart. Rendering it is the only way to see what it actually pulls:
#    the operator's image env vars are assembled from several values, and a
#    grep over values.yaml would miss exactly the composition bugs that matter.
# ---------------------------------------------------------------------------
REQUIRED_VALUES=(
  --set platformAgent.harness.clusterName=ci-cluster
  --set platformAgent.harness.location=us-central1
  --set platformAgent.harness.projectId=ci-project
)

# What turns the GitHub token minter on. It is off by default, so its image
# reached no render at all and its pin sat behind every check below — the gap
# any off-by-default toggle opens, and the one #1139 closes.
MINTER_VALUES=(
  --set githubMinter.enabled=true
  --set "githubMinter.org=$MINTER_ORG"
  --set "githubMinter.repo=$MINTER_REPO"
)

# The rendered manifests for one configuration. A render failure is fatal
# rather than an empty list: the checks below iterate what comes out of it, and
# "no images" reads exactly like "no images to object to".
render_chart() {
  helm template test-release charts/kube-agents "${REQUIRED_VALUES[@]}" "$@" || {
    echo "ERROR: 'helm template' failed for the chart${*:+ with $*} — see the error above." >&2
    return 1
  }
}

# The `image:` fields of a rendered manifest stream on stdin.
image_field_refs() {
  sed -n 's/^[[:space:]]*image:[[:space:]]*"\?\([^"]*\)"\?[[:space:]]*$/\1/p'
}

# The image references the *_IMAGE env vars carry, from the same stream. The
# pass pairs a name line with the value line under it rather than matching the
# value alone, because an env var's value is not an image by its shape, only by
# which variable holds it — IMAGE_ENV_NAME_RE is that test. Anything between
# the name and its value — a `valueFrom:`, the next list entry — drops the
# pairing.
image_env_refs() {
  awk -v name_re="$IMAGE_ENV_NAME_RE" -v value_re="$VALUE_FIELD_RE" '
    $0 ~ name_re { pending = 1; next }
    pending && match($0, value_re) {
      ref = substr($0, RLENGTH + 1)
      sub(/[[:space:]]+$/, "", ref)
      sub(/^"/, "", ref)
      sub(/"$/, "", ref)
      if (ref != "") print ref
      pending = 0
      next
    }
    { pending = 0 }
  '
}

# Every image a rendered manifest stream on stdin pulls: the `image:` fields,
# and the operator's *_IMAGE env vars — the latter are what the operator later
# stamps onto agent pods, so leaving them public half-mirrors the install.
image_refs() {
  local rendered
  rendered="$(cat)"
  {
    image_field_refs <<<"$rendered"
    image_env_refs <<<"$rendered"
  } | sort -u
}

# Split a reference into repository and tag. The digest, if any, goes first;
# the tag is then the part after a colon in the final path segment, so a
# registry port (host:5000/name) is not mistaken for one. ref_pin keeps the
# digest (tag@sha256:...), because that is the form images.json pins a tag and
# a digest together with and what the chart's default render must match byte for
# byte. Of the entries this function sees — the ones a chart render emits — that
# is hindsight-api and hindsight-postgresql; images.json pins two more the same
# way (busybox and, through tags.env, the Hermes base), but both are build-time
# and never appear in a render.
split_ref() {
  local ref=${1%%@*} digest=""
  case "$1" in
  *@*) digest="${1#*@}" ;;
  esac
  case "${ref##*/}" in
  *:*)
    ref_repo="${ref%:*}"
    ref_tag="${ref##*:}"
    ;;
  *)
    ref_repo="$ref"
    ref_tag=""
    ;;
  esac
  ref_pin="$ref_tag${digest:+@$digest}"
}

inventory_repos="$(jq -r '.images[].repository' "$INVENTORY" | sort -u)"
inventory_names="$(jq -r '.images[].name' "$INVENTORY" | sort -u)"

# 3a. Unmirrored render: every image in it must be in the inventory, at the
#     pin the inventory carries, so the mirror built from it is complete and
#     the tags it holds are the tags the install asks for. Matching on the
#     repository alone would pass through failure mode #1 — the chart on
#     LiteLLM v1.92.0 while the inventory says v1.95.0 is one entry, one
#     repository, and an ImagePullBackOff.
check_inventory_pins() {
  local label=$1 images=$2 image want_tag
  while read -r image; do
    [ -n "$image" ] || continue
    split_ref "$image"
    grep -qxF "$ref_repo" <<<"$inventory_repos" || {
      fail "$label: the chart renders '$image', which has no entry in $INVENTORY — 'make mirror-images' would not copy it."
      continue
    }
    want_tag="$(pin_of_repo "$ref_repo")"
    [ -z "$want_tag" ] || [ "$ref_pin" = "$want_tag" ] ||
      fail "$label: the chart renders '$image', but $INVENTORY pins '$ref_repo' at '$want_tag' — 'make mirror-images' would copy '$want_tag' and the install would ask for '$ref_pin'."
  done <<<"$images"
}

# 3b. Mirrored install: nothing may be left on a public registry. This is the
#     chart-side equivalent of TestNoPublicRegistryWhenMirrored in the
#     operator, and it covers the env vars a Go test cannot see.
check_mirror_prefix() {
  local label=$1 images=$2 image
  while read -r image; do
    [ -n "$image" ] || continue
    case "$image" in
    "$MIRROR"/*) ;;
    *) fail "$label: with global.imageRegistry set, the chart still renders '$image' outside the mirror." ;;
    esac
  done <<<"$images"
}

# 3c. Mirrored install, continued: the reference has to be in the mirror, not
#     merely under its prefix. scripts/mirror_images.sh names each destination
#     after the inventory entry's `.name`, but the chart cannot read
#     images.json at render time — kube-agents.imageRepository reproduces the
#     rule by taking the repository's trailing path segment, and
#     kube-agents.thirdPartyImage takes the real name explicitly for the
#     entries where the two differ (hindsight-postgresql is
#     docker.io/pgvector/pgvector). So the check reads the mirrored render
#     directly: every image under the mirror prefix must sit at a name the
#     inventory carries, or it points at a path 'make mirror-images' never
#     pushed to and the install fails at pull time on the one path this
#     feature exists for.
check_mirror_names() {
  local label=$1 images=$2 image segment
  while read -r image; do
    [ -n "$image" ] || continue
    case "$image" in
    "$MIRROR"/*) ;;
    *) continue ;; # not under the prefix is check 3b's finding, not this one
    esac
    split_ref "$image"
    segment="${ref_repo##*/}"
    grep -qxF "$segment" <<<"$inventory_names" ||
      fail "$label: with global.imageRegistry set, the chart renders '$image', but no $INVENTORY entry is named '${segment}' — 'make mirror-images' pushes each image to <prefix>/<name>, so nothing ever pushed there. Either rename the entry or pass the real name to kube-agents.thirdPartyImage."
  done <<<"$images"
}

# Four renders, not two: the default and mirrored pair says nothing about an
# image behind a chart toggle, because a toggle left off renders nothing to
# check. The minter pair is that gap closed for githubMinter; Hindsight's
# images, behind memory.provider, are still only covered by their values.yaml
# pins.
default_render="$(render_chart)" || exit 1
mirrored_render="$(render_chart --set "global.imageRegistry=$MIRROR")" || exit 1
minter_render="$(render_chart "${MINTER_VALUES[@]}")" || exit 1
minter_mirrored_render="$(render_chart "${MINTER_VALUES[@]}" --set "global.imageRegistry=$MIRROR")" || exit 1

default_images="$(image_refs <<<"$default_render")"
mirrored_images="$(image_refs <<<"$mirrored_render")"
minter_images="$(image_refs <<<"$minter_render")"
minter_mirrored_images="$(image_refs <<<"$minter_mirrored_render")"

[ -n "$default_images" ] || {
  echo "ERROR: the chart rendered no image references at all — the extraction patterns in image_refs no longer match the manifests, so checks 3a, 3b and 3c are inspecting nothing." >&2
  exit 1
}
[ -n "$(image_env_refs <<<"$default_render")" ] || {
  echo "ERROR: the chart rendered no *_IMAGE env var that image_env_refs recognises, so the images the operator stamps onto agent pods are unchecked — and the 'image:' fields keep the list above non-empty, which is why the guard above does not catch it. Either the chart stopped emitting them or IMAGE_ENV_NAME_RE no longer matches the shape it emits." >&2
  exit 1
}
minter_only="$(grep -Fxv -f <(printf '%s\n' "$default_images") <<<"$minter_images" || true)"
[ -n "$minter_only" ] || {
  echo "ERROR: enabling githubMinter added no image the default render already carried, so the minter renders exercise nothing the default and mirrored pair does not — the coverage gap #1139 closed is open again. Check that MINTER_VALUES still turns the minter on." >&2
  exit 1
}

check_inventory_pins "$LABEL_DEFAULT" "$default_images"
check_inventory_pins "$LABEL_DEFAULT_MINTER" "$minter_images"

check_mirror_prefix "$LABEL_MIRRORED" "$mirrored_images"
check_mirror_prefix "$LABEL_MIRRORED_MINTER" "$minter_mirrored_images"

check_mirror_names "$LABEL_MIRRORED" "$mirrored_images"
check_mirror_names "$LABEL_MIRRORED_MINTER" "$minter_mirrored_images"

# ---------------------------------------------------------------------------
# 4. The example manifests. They are applied by hand rather than rendered by
#    the chart, so nothing above sees them — and two of them hard-code the
#    LiteLLM tag. A pin raised in images.json and the chart but not here is
#    failure mode #1 again, in the copy people paste from. Images an example
#    brings along itself (its demo workload, a vLLM server) are not in the
#    inventory and are left alone.
# ---------------------------------------------------------------------------
example_refs="$(grep -rnE '^[[:space:]]*-?[[:space:]]*image:[[:space:]]*[^$"'"'"' ]+[[:space:]]*$' examples --include='*.yaml' --include='*.yml' |
  sed -E 's/^([^:]+):([0-9]+):[[:space:]]*-?[[:space:]]*image:[[:space:]]*/\1\t\2\t/' || true)"
[ -n "$example_refs" ] ||
  fail "no image references found under examples/ — the extraction pattern in check 4 no longer matches, so the example pins are unchecked."
while IFS=$'\t' read -r file line image; do
  [ -n "${image:-}" ] || continue
  split_ref "$image"
  want_tag="$(pin_of_repo "$ref_repo")"
  [ -n "$want_tag" ] || continue
  [ "$ref_tag" = "$want_tag" ] ||
    fail "${file}:${line} pins '$image', but $INVENTORY has '$want_tag' for '$ref_repo' — the mirror is populated from the inventory, so this example asks for a tag that was never copied."
done <<<"$example_refs"

# ---------------------------------------------------------------------------
# 5. The kustomize integrations. `make deploy-litellm`, `deploy-github`,
#    `deploy-hindsight` and `deploy-inference-replay` apply these directly, so
#    the chart render in check 3 never sees them and neither does check 4.
#    Every image here has to come from a variable the inventory owns: a literal
#    reference is un-mirrorable — no deploy target can redirect it and
#    `make mirror-images` was never told to copy it — so an approved-registry
#    install pulls it from a public registry after the install reported success.
#
#    This is failure mode #2, and it arrived exactly this way: Hindsight landed
#    with both of its images hard-coded, and every check above stayed green.
# ---------------------------------------------------------------------------
overrides="$(jq -r '.images[] | select(.override) | .override' "$INVENTORY" | sort -u)"
integration_refs="$(grep -rnE '^[[:space:]]*image:[[:space:]]*\S+' k8s-operator/config/integrations \
  --include='*.yaml' --include='*.yml' --include='*.yaml.template' |
  sed -E 's/^([^:]+):([0-9]+):[[:space:]]*image:[[:space:]]*/\1\t\2\t/' || true)"
[ -n "$integration_refs" ] ||
  fail "no image references found under k8s-operator/config/integrations — the extraction pattern in check 5 no longer matches, so the integration manifests are unchecked."
while IFS=$'\t' read -r file line image; do
  [ -n "${image:-}" ] || continue
  # The '${'*'}' pattern is deliberately unexpanded: it matches the literal
  # characters "${…}" as they appear in the manifest, which is the point of the
  # check. SC2016 reads that as an accident.
  # shellcheck disable=SC2016
  case "$image" in
  '${'*'}')
    var="${image#\$\{}"
    var="${var%\}}"
    grep -qxF "$var" <<<"$overrides" ||
      fail "${file}:${line} substitutes \$$var, which is not the 'override' of any entry in $INVENTORY — nothing resolves it from the inventory, so a mirrored install cannot redirect it."
    ;;
  *)
    fail "${file}:${line} hard-codes '$image'. Integration images must come from a \${VAR} that an $INVENTORY entry names in its 'override', or no deploy target can point them at a mirror."
    ;;
  esac
done <<<"$integration_refs"

if [ "$status" -eq 0 ]; then
  echo "Image inventory check passed: $INVENTORY matches every pin, the Go builder pin matches $GO_MOD, and the chart mirrors cleanly."
fi
exit "$status"

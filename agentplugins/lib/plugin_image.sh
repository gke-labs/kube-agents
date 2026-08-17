#!/usr/bin/env bash
# Image plumbing shared by the plugin installers. Source it; running it does nothing.
#
# Every plugin image in this directory is `FROM scratch` plus a COPY of one tree, so the
# entire build is a single tar layer. That is what makes a local build practical, and a
# local build is the point:
#
#   docker  builds the plugin's own Dockerfile and pushes it. Validates the Dockerfile.
#   crane   assembles the same layer and pushes it. No daemon, no Dockerfile.
#
# `gcloud builds submit` is deliberately not one of the options. Cloud Build rejects some
# corporate credentials for its own quota (`ACCESS_TOKEN_TYPE_UNSUPPORTED` on
# `serviceusage.services.use`), and it needs the API enabled and billing wired up before
# it will run at all — so an installer that can only build there fails on the machine
# where it is most often run. Both builders here run against the local filesystem and
# push straight to the registry.
#
# Everything that can fail before the first byte is pushed is settled by
# plugin_image_resolve, which the caller runs BEFORE it provisions anything: the project,
# the source tree, the builder, the Artifact Registry repository and the credential for
# it. A missing builder or an unauthenticated gcloud discovered at the end of the run
# leaves a half-provisioned project behind. Resolve proves the credential can be obtained
# but does not store one — see plugin_image_check_credential; the login that writes a
# token to disk happens around the push and nowhere else.
#
# What resolve cannot settle is authorization. Nothing short of a push tells you whether
# the credential may write to that repository, so a run that gets past resolve can still
# fail at the push — just not for any reason a person could have fixed up front.
# Publishing happens later:
#
#   . "${REPO_ROOT}/agentplugins/lib/plugin_image.sh"
#   plugin_image_resolve <plugin-name> <project> <plugin-dir> <source-dir> \
#       [<context> <namespace> <agent>]
#   … provision …
#   plugin_image_publish <plugin-dir> <source-dir>
#   helm upgrade --install … --set image="$PLUGIN_IMAGE_REF"
#
# <plugin-dir> is the chart directory, holding the Dockerfile and .dockerignore;
# <source-dir> is the tree the Dockerfile copies to /. Both functions take the pair, and
# for the same reason: what the image contains is <source-dir>, but how it is assembled
# is <plugin-dir>, and the tag has to cover both.

# ─── Where images are published ───────────────────────────────────────────────
# Artifact Registry, not Container Registry. gcr.io is deprecated and its hosts are being
# turned down, so an installer that pushed there would be standing up a new deployment on
# a registry with an end date.
#
# Which Artifact Registry is worked out at resolve time from the agent's own image, not
# assumed here — see plugin_image_discover_registry. Empty means "work it out"; set
# AR_LOCATION, AR_PROJECT or AR_REPOSITORY to pin one instead.
PLUGIN_AR_LOCATION="${AR_LOCATION:-}"
PLUGIN_AR_PROJECT="${AR_PROJECT:-}"
PLUGIN_AR_REPOSITORY="${AR_REPOSITORY:-}"
# Used only when there is no agent image to copy — a first install, or an agent running
# from somewhere that is not Artifact Registry. REGION and GCP_ARTIFACT_REGISTRY_REPO_NAME
# are the variables the provisioning scripts put the fleet's region and repository in, so
# they are better guesses than hardcoded ones. They are fallbacks and not pins: an
# exported GCP_ARTIFACT_REGISTRY_REPO_NAME left over from `dev_rebuild_agent.sh` must not
# quietly outrank the repository the agent is demonstrably being pulled from.
PLUGIN_AR_LOCATION_FALLBACK="${REGION:-us-central1}"
PLUGIN_AR_REPOSITORY_FALLBACK="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-kube-agents}"
# GKE nodes are linux/amd64. On an arm64 laptop a `docker build` without this produces an
# arm64 manifest, which the kubelet declines to mount rather than mounting wrongly.
PLUGIN_TARGET_PLATFORM="${TARGET_PLATFORM:-linux/amd64}"
PLUGIN_CRANE_BIN="${CRANE_BIN:-crane}"

# Bump when the way an image is produced changes, not just what goes into it.
#
# The tag digests the source tree, and the publish step skips a tag that already exists.
# Those two together mean a change to the BUILD is invisible: the source is untouched, so
# the tag is unchanged, so the existing image is left in place and the change never ships.
# That is not hypothetical — the platform stamp added to the crane path below could not
# reach any image published before it, which stayed on `os: ""` indefinitely. Mixing this
# into the digest makes such a change a new tag, exactly as an edited file would be.
#
#   1  initial: source tree only
#   2  crane images carry an explicit platform
#   3  the plugin's own Dockerfile and .dockerignore are part of the digest, and both
#      builders stage a world-readable copy instead of shipping the working tree's modes
#   4  the exclusion set is read from the plugin's .dockerignore rather than hardcoded
#      here, so the crane layer and `docker build` cannot ship different files
#
# Note that 3 makes this counter a backstop rather than the first line of defence: a
# per-plugin build change now moves that plugin's tag on its own. The counter still
# covers a change to THIS file, which no plugin's digest can see.
PLUGIN_IMAGE_RECIPE=4

# Set by plugin_image_resolve.
PLUGIN_IMAGE_REF=""
PLUGIN_IMAGE_PREBUILT=0
PLUGIN_IMAGE_TAG_PINNED=0
# Set once the repository is known to exist, so publish does not repeat the describe.
PLUGIN_IMAGE_REGISTRY_READY=0
# Set by plugin_image_crane_publish when the intermediate tag outlived the build.
PLUGIN_IMAGE_STAGING_LEFT=""
# Set by plugin_image_builder.
PLUGIN_IMAGE_BUILDER_RESOLVED=""

plugin_image_die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

plugin_image_sha256() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256
    else
        sha256sum
    fi
}

# ─── What the image does NOT ship ─────────────────────────────────────────────
# The exclusions come from the plugin's own .dockerignore and from nowhere else.
#
# They used to be spelled out three times — once for the content tag, once for the crane
# layer, and once in each .dockerignore — with only a comment claiming the three agreed.
# Nothing enforced it, and the divergence would have been silent in the worst possible
# way: add a line to .dockerignore, and the build digest moves, so a NEW tag is minted,
# so the publish is not skipped — but only `docker build` honours the new line. crane
# would ship the excluded file anyway, under a tag that says it did not. Whichever
# builder reached that tag first would then define it forever, because publish skips a
# tag that already exists. One reader, used by both builders and by the tag, cannot do
# that.
#
# Matching follows Docker's rules for the subset of patterns these plugins use, and
# plugin_image_ignore_load REFUSES anything outside it. Refusing is the point: a pattern
# this cannot match the way Docker matches it is exactly the silent divergence above.
#
#   #comment, blank      ignored
#   **/name              `name`, at any depth, file or directory
#   dir/file             anchored at the context root, `*` does not cross a `/`
#   trailing `/`         a directory; everything under it goes too
#   !negation            REFUSED
#   `**` anywhere else   REFUSED
#
# Newline-separated rather than an array: /bin/bash on macOS is 3.2, and `${arr[@]}`
# on an empty array is an unbound-variable error there under `set -u`. No pattern can
# contain a newline, so nothing is lost.
PLUGIN_IMAGE_IGNORE=""

plugin_image_ignore_load() {
    local plugin_dir="$1" raw line

    PLUGIN_IMAGE_IGNORE=""
    [ -f "${plugin_dir}/.dockerignore" ] || return 0

    # `|| [ -n "$raw" ]` so a final line with no trailing newline is still read.
    while IFS= read -r raw || [ -n "$raw" ]; do
        # Trimmed at both ends, because Docker trims each pattern before matching it and
        # anything this keeps that Docker drops is a pattern that excludes nothing here and
        # something there — a file set that differs between the two builders, silently.
        # A CRLF checkout is the common way in (`[:space:]` covers the carriage return),
        # an editor leaving a trailing space on a line is the other.
        line="${raw#"${raw%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        case "$line" in
            '' | '#'*) continue ;;
        esac
        # Docker anchors at the context root either way, so these spellings are the same
        # pattern; normalising here keeps the matcher from having to know that.
        line="${line#/}"
        line="${line#./}"
        line="${line%/}"
        case "$line" in
            '') continue ;;
            '!'*)
                plugin_image_die "${plugin_dir}/.dockerignore: negation ('${line}') is not supported. Both builders here must agree on the file set exactly, and a re-include is the one rule that cannot be checked one path at a time."
                ;;
            '**/'*)
                case "${line#\*\*/}" in
                    *'**'*)
                        plugin_image_die "${plugin_dir}/.dockerignore: '${line}' has more than one '**'. Only a leading '**/' is supported."
                        ;;
                esac
                ;;
            *'**'*)
                plugin_image_die "${plugin_dir}/.dockerignore: '${line}' uses '**' somewhere other than the start. Only a leading '**/' is supported."
                ;;
        esac
        PLUGIN_IMAGE_IGNORE="${PLUGIN_IMAGE_IGNORE}${line}"$'\n'
    done <"${plugin_dir}/.dockerignore"
}

# Match one path against one pattern, segment by segment.
#
# Segment by segment because `*` must not cross a `/` — bash's own `==` would let it, so
# `*.pyc` would match `skills/x.pyc` and exclude far more than Docker does. A segment
# holds no `/`, so a plain `case` glob over one is exactly Docker's rule for one.
plugin_image_glob_segments() {
    local pat="$1" path="$2" pseg fseg

    while :; do
        pseg="${pat%%/*}"
        fseg="${path%%/*}"
        case "$fseg" in
            $pseg) ;;
            *) return 1 ;;
        esac
        case "$pat" in
            */*) pat="${pat#*/}" ;;
            *) pat="" ;;
        esac
        case "$path" in
            */*) path="${path#*/}" ;;
            *) path="" ;;
        esac
        # Both have to run out together. A pattern with segments left over does not match
        # a shorter path, and a path with segments left over is only excluded because an
        # ANCESTOR of it matched — which is plugin_image_ignored's job, not this one's.
        if [ -z "$pat" ] && [ -z "$path" ]; then
            return 0
        fi
        if [ -z "$pat" ] || [ -z "$path" ]; then
            return 1
        fi
    done
}

plugin_image_ignore_match() {
    local path="$1" pat rest

    [ -n "$PLUGIN_IMAGE_IGNORE" ] || return 1
    while IFS= read -r pat; do
        [ -n "$pat" ] || continue
        case "$pat" in
            '**/'*)
                # Unanchored: the pattern matches any suffix of the path.
                rest="$path"
                while :; do
                    plugin_image_glob_segments "${pat#\*\*/}" "$rest" && return 0
                    case "$rest" in
                        */*) rest="${rest#*/}" ;;
                        *) break ;;
                    esac
                done
                ;;
            *)
                plugin_image_glob_segments "$pat" "$path" && return 0
                ;;
        esac
    done <<<"$PLUGIN_IMAGE_IGNORE"
    return 1
}

# True when a path is excluded. The path is relative to the DOCKER CONTEXT root — the
# plugin directory — because that is what .dockerignore patterns are relative to, and the
# crane layer is rooted somewhere else entirely (see plugin_image_src_prefix).
plugin_image_ignored() {
    local path="$1" prefix="" rest="$1"

    # Every ancestor, not just the path itself: an excluded directory takes its contents
    # with it, so `**/__pycache__` has to exclude `skills/__pycache__/x.pyc` too.
    while :; do
        plugin_image_ignore_match "${prefix}${rest%%/*}" && return 0
        case "$rest" in
            */*)
                prefix="${prefix}${rest%%/*}/"
                rest="${rest#*/}"
                ;;
            *) return 1 ;;
        esac
    done
}

# How to spell a path under src_dir as a path under plugin_dir.
#
# .dockerignore patterns are relative to the build context, which is the plugin
# directory; the crane layer and the content tag walk src_dir, which is a subtree of it.
# Without this prefix an anchored pattern would be tested against the wrong string and
# quietly never match — the same silent divergence the shared reader exists to prevent.
#
# Both paths are resolved before they are compared, so a caller mixing an absolute
# plugin_dir with a relative src_dir (or a `..` in either) still gets the right answer.
plugin_image_src_prefix() {
    local plugin_dir src_dir

    plugin_dir="$(cd "$1" 2>/dev/null && pwd)" || return 1
    src_dir="$(cd "$2" 2>/dev/null && pwd)" || return 1

    if [ "$src_dir" = "$plugin_dir" ]; then
        printf ''
        return 0
    fi
    case "$src_dir" in
        "${plugin_dir}/"*) printf '%s/' "${src_dir#"${plugin_dir}/"}" ;;
        # A Dockerfile can only COPY from inside its own context, so a src_dir outside
        # plugin_dir means the two arguments disagree about what is being built. Guessing
        # would put the image and the tag out of step.
        *) plugin_image_die "source directory '${src_dir}' is not inside plugin directory '${plugin_dir}'; the Dockerfile could not copy from it." ;;
    esac
}

# The files the image ships, relative to the current directory (which must be src_dir).
#
# One definition, used by both the content tag and the crane layer, so the two cannot
# drift: a file that changes the tag must be a file that reaches the image. `prefix` is
# what plugin_image_src_prefix returned, so the exclusions are applied to the same path
# `docker build` would apply them to.
plugin_image_source_files() {
    local prefix="$1" file

    find . -type f | while IFS= read -r file; do
        file="${file#./}"
        plugin_image_ignored "${prefix}${file}" || printf '%s\n' "$file"
    done
}

# Refuse a source tree holding anything that is not a regular file or a directory.
#
# plugin_image_source_files is the single definition of what the image ships, and its
# `find . -type f` does not see a symlink — but plugin_image_stage's `cp -R` preserves one,
# and the crane tar writes it out as a symlink entry. So a link would be IN the layer and
# absent from the digest: re-point it, and the content tag does not move, publish reports
# the image already published, and the edit never reaches a cluster on a deployment that
# reports healthy. That is precisely the `latest` failure the content tag exists to
# eliminate, arriving through a file type the digest cannot see. It compounds: whether
# `docker build`'s COPY preserves a symlink or dereferences it is builder-dependent, so the
# two builders could put different bytes under one tag — the divergence
# plugin_image_ignore_load's refusals are written to prevent, by a route they do not cover.
#
# Refused rather than hashed, for the same reason a negation is refused. Hashing the link
# target would work only once both builders were KNOWN to agree on what a symlink becomes,
# and neither this file nor anything around it establishes that. A fifo, a socket or a
# device node is invisible to `-type f` in exactly the same way and is caught here too; a
# plugin source tree is a directory of files, so anything else is a mistake to stop on
# rather than a case to interpret.
#
# Excluded paths are not checked: something the .dockerignore drops reaches neither builder,
# so a link inside a `.venv` is nobody's problem.
#
# Every path out of here EXITS, the way plugin_image_ignore_load does, and for the same
# reason: both call sites invoke this bare, and the comments there promise a refusal reports
# itself. A `return 1` would keep that promise only for as long as the caller happened to be
# running under `set -e` — and `set -e` is suspended for the whole of a call made in an `if`
# or on the left of an `&&`, so a caller that merely TESTED plugin_image_resolve for success
# would carry on past a tree this had already rejected, and fail further down over something
# else. The two directory checks are here rather than left to plugin_image_src_prefix's
# silent `return 1` for the same reason: publish, unlike resolve, never validated them, and
# an exit with nothing on stderr is the one way to fail that cannot be acted on.
plugin_image_check_file_types() {
    local plugin_dir="$1" src_dir="$2" prefix offenders

    [ -d "$plugin_dir" ] || plugin_image_die "plugin directory '${plugin_dir}' does not exist."
    [ -d "$src_dir" ] || plugin_image_die "plugin source directory '${src_dir}' does not exist."
    # With those two settled, the only way left for this to fail is a src_dir outside the
    # context — which plugin_image_src_prefix has already reported by the time we see it, so
    # this exits rather than saying the same thing again in different words.
    prefix="$(plugin_image_src_prefix "$plugin_dir" "$src_dir")" || exit 1
    plugin_image_ignore_load "$plugin_dir"

    offenders="$(
        # pipefail HERE, and not left to the caller. The `||` below reads the status of a
        # `find | while` pipeline, and a while loop exits 0 however badly find went — so a
        # walk that stopped on a permission error hands back only the offenders it managed
        # to reach, which for a tree whose symlink was in the part it never got to is an
        # empty list. The check would then pass, for the one reason that proves it did not
        # run. Both installers set pipefail themselves, so this changes nothing for them;
        # it is here because a guard that holds only while the caller opts in is not the
        # guard the comment above promises.
        set -o pipefail
        cd "$src_dir" || exit 1
        find . ! -type d ! -type f | while IFS= read -r entry; do
            entry="${entry#./}"
            plugin_image_ignored "${prefix}${entry}" || printf '    %s\n' "$entry"
        done
    )" || plugin_image_die "could not walk '${src_dir}' to check what it holds."

    [ -z "$offenders" ] || plugin_image_die "a plugin source tree may hold only regular files and directories, but '${src_dir}' holds:
${offenders}
  A symlink (or a fifo, or a socket) is copied into the image but is invisible to the
  content tag, so editing one would publish nothing — and the two builders do not agree on
  what it becomes. Replace it with the file it points at, or exclude it in
  ${plugin_dir}/.dockerignore."
}

# A digest of how the image is assembled, as opposed to what goes in it.
#
# Without this the Dockerfile is outside the tag: change its COPY path, or add a line to
# .dockerignore, and the source tree is untouched, so the tag is unchanged, so the publish
# is skipped and the edit never reaches a cluster. That is the same trap PLUGIN_IMAGE_RECIPE
# documents, one level down and per plugin, where a shared counter cannot see it.
#
# Absent files hash as nothing rather than as an error: a plugin built only by crane needs
# no Dockerfile, and its digest should stay stable rather than depend on one appearing.
plugin_image_build_digest() {
    local plugin_dir="$1" name
    {
        for name in Dockerfile .dockerignore; do
            [ -f "${plugin_dir}/${name}" ] || continue
            printf '%s ' "$name"
            plugin_image_sha256 <"${plugin_dir}/${name}"
        done
    } | plugin_image_sha256
}

# A tag derived from the contents of the source tree.
#
# Not `latest`, because `latest` is what makes a second install silently do nothing: the
# rendered AgentPlugin is byte-identical, so the operator sees no change, never rolls the
# pod, and the edited skill or adapter stays unpublished on a deployment that reports
# healthy. A content tag makes the CR change exactly when the files changed — and stay
# identical when they did not, so re-running the installer is still idempotent.
#
# Returns non-zero if there is nothing to hash. The caller cannot test the tag for that:
# `v` prefixed to nothing is still a non-empty string, and the sha256 of no input at all
# is a perfectly valid digest, so an unreadable or empty tree would otherwise produce a
# confident-looking tag for an image with no files in it.
plugin_image_content_tag() {
    local plugin_dir="$1" src_dir="$2" digest
    digest="$(
        # pipefail, for the reason plugin_image_check_file_types spells out: the walk below
        # is a `find | while`, and a while loop exits 0 however badly find went. A walk that
        # stopped on a permission error would hash the files it happened to reach and hand
        # back a perfectly well-formed tag for a SUBSET of the tree — and a stable one, so
        # every later install would find that tag published and skip the build. The
        # `[ -n "$files" ]` guard below cannot see this: it catches a walk that returned
        # nothing at all, not one that returned half. It also makes an unreadable file fatal
        # rather than a path contributed to the digest with no hash after it.
        set -o pipefail
        # All three before the cd: plugin_dir may be relative to where the caller started.
        build="$(plugin_image_build_digest "$plugin_dir")" || exit 1
        prefix="$(plugin_image_src_prefix "$plugin_dir" "$src_dir")" || exit 1
        plugin_image_ignore_load "$plugin_dir" || exit 1
        cd "$src_dir" || exit 1
        # `|| exit 1` rather than a bare assignment: pipefail is what makes this status
        # worth reading, and an assignment on its own discards it.
        files="$(plugin_image_source_files "$prefix" | LC_ALL=C sort)" || exit 1
        [ -n "$files" ] || exit 1
        {
            # How the image is built is part of what the image is. The platform belongs
            # here for the same reason as the recipe: an arm64 build of an unchanged tree
            # is a different image, and must not land on the tag the amd64 build holds.
            printf 'recipe %s\nplatform %s\nbuild %s\n' \
                "$PLUGIN_IMAGE_RECIPE" "$PLUGIN_TARGET_PLATFORM" "$build"
            # Each file contributes its path and its contents, so a rename is a new image
            # too.
            printf '%s\n' "$files" |
                while IFS= read -r file; do
                    printf '%s ' "$file"
                    plugin_image_sha256 <"$file"
                done
        } | plugin_image_sha256
    )" || return 1
    printf 'v%s' "$(printf '%s' "$digest" | cut -c1-12)"
}

# Point the registry defaults at the repository the agent images are already in.
#
# Not tidiness. Nothing in this repository grants artifactregistry.reader, so whether the
# kubelet can pull a plugin image depends entirely on how the fleet scoped that role onto
# its nodes — and a fleet that scoped it per repository cannot mount an image published
# to a repository of our own invention. The agent's own image is the one reference the
# kubelet is already known to pull, so that is the one copied: same host, same location,
# same project, same repository. Guessing instead is how a plugin ends up in a second,
# empty repository that nothing can read.
#
# The project is copied along with the rest, and that matters: a fleet whose agents run
# from a shared registry project would otherwise get location and repository from the
# agent but the project from wherever the plugin is being installed — a reference that
# satisfies none of the reasoning above, points at a repository that does not exist, and
# has plugin_image_ensure_repository create it with no reader binding on it. Set
# AR_PROJECT to publish somewhere else deliberately.
plugin_image_discover_registry() {
    local context="$1" namespace="$2" agent_ref="$3" agent_image host rest proj repo

    # All three pinned by the caller — nothing left to discover.
    if [ -n "$PLUGIN_AR_LOCATION" ] && [ -n "$PLUGIN_AR_PROJECT" ] &&
        [ -n "$PLUGIN_AR_REPOSITORY" ]; then
        return 0
    fi

    agent_image="$(kubectl --context="$context" -n "$namespace" \
        get platformagent "$agent_ref" -o jsonpath='{.spec.deployment.image}' 2>/dev/null || echo "")"

    host="${agent_image%%/*}"
    case "$host" in
        *-docker.pkg.dev) ;;
        # No agent deployed yet, or one running from somewhere else entirely (ghcr.io on
        # a mirrored install). Neither tells us anything; the fallbacks apply.
        *) return 0 ;;
    esac

    rest="${agent_image#*/}" # <project>/<repository>/<name>…
    proj="${rest%%/*}"
    rest="${rest#*/}" # <repository>/<name>…
    # A repository segment exists only if something follows it. An Artifact Registry
    # reference is host/project/repository/name, and anything shorter has no repository
    # to copy: `host/project/name:tag` would yield `name:tag` — a "repository" named
    # after the image, with a tag stuck to it — and `host/project` would yield the
    # project name. Both are inventions rather than discoveries, and both would build a
    # reference nothing can pull. Empty means the fallback applies, which is the honest
    # answer when the agent's own image does not say.
    repo=""
    case "$rest" in
        */*) repo="${rest%%/*}" ;;
    esac

    [ -n "$PLUGIN_AR_LOCATION" ] || PLUGIN_AR_LOCATION="${host%-docker.pkg.dev}"
    [ -n "$PLUGIN_AR_PROJECT" ] || [ -z "$proj" ] || PLUGIN_AR_PROJECT="$proj"
    [ -n "$PLUGIN_AR_REPOSITORY" ] || [ -z "$repo" ] || PLUGIN_AR_REPOSITORY="$repo"
}

# Decide how the image gets built. Sets PLUGIN_IMAGE_BUILDER_RESOLVED, once.
plugin_image_builder() {
    local choice="${IMAGE_BUILDER:-auto}"

    [ -z "$PLUGIN_IMAGE_BUILDER_RESOLVED" ] || return 0

    case "$choice" in
        docker)
            if ! { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }; then
                plugin_image_die "IMAGE_BUILDER=docker, but no Docker daemon is reachable. Start Docker, or unset IMAGE_BUILDER to fall back to crane."
            fi
            PLUGIN_IMAGE_BUILDER_RESOLVED="docker"
            return 0
            ;;
        crane)
            command -v "$PLUGIN_CRANE_BIN" >/dev/null 2>&1 ||
                plugin_image_die "IMAGE_BUILDER=crane, but '${PLUGIN_CRANE_BIN}' is not on PATH. Set CRANE_BIN, or install it: GOBIN=\"\$HOME/.local/bin\" go install github.com/google/go-containerregistry/cmd/crane@latest"
            PLUGIN_IMAGE_BUILDER_RESOLVED="crane"
            return 0
            ;;
        auto) ;;
        *) plugin_image_die "IMAGE_BUILDER must be auto, docker or crane; got '${choice}'." ;;
    esac

    # Docker first when a daemon answers: it builds the real Dockerfile, so it validates
    # the file crane bypasses. crane otherwise — the common case on a laptop.
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        PLUGIN_IMAGE_BUILDER_RESOLVED="docker"
        return 0
    fi
    if command -v "$PLUGIN_CRANE_BIN" >/dev/null 2>&1; then
        PLUGIN_IMAGE_BUILDER_RESOLVED="crane"
        return 0
    fi
    plugin_image_die "no image builder is available. Either start a Docker daemon, or install crane:
    brew install crane
    # or: GOBIN=\"\$HOME/.local/bin\" go install github.com/google/go-containerregistry/cmd/crane@latest
  Alternatively set PLUGIN_IMAGE=<ref> to install an image that has already been published."
}

# The reference to publish under. Sets PLUGIN_IMAGE_REF and PLUGIN_IMAGE_PREBUILT.
#
# The last three arguments are optional and only used to find the agent's own registry;
# pass the kubectl context, the namespace and the PlatformAgent name if the caller knows
# them by this point.
#
# Everything a person could have to go and fix fails here, before the caller provisions
# anything: the project, the source tree, the builder, the registry repository and the
# credential for it. Discovering at step 5 that no builder is installed, or that gcloud
# is not logged in, leaves behind the topic, the subscription, the log sink and the IAM
# bindings that steps 1 to 4 already created.
#
# The one thing left for the push to discover is authorization — see the header.
plugin_image_resolve() {
    local name="$1" project="$2" plugin_dir="$3" src_dir="$4"
    local context="${5:-}" namespace="${6:-}" agent_ref="${7:-}" tag

    if [ -n "${PLUGIN_IMAGE:-}" ]; then
        PLUGIN_IMAGE_REF="$PLUGIN_IMAGE"
        PLUGIN_IMAGE_PREBUILT=1
        echo "  Image: ${PLUGIN_IMAGE_REF} (prebuilt; PLUGIN_IMAGE is set)"
        return 0
    fi

    [ -n "$project" ] ||
        plugin_image_die "could not determine the GCP project to publish ${name} to. Set GCP_PROJECT_ID, or PLUGIN_IMAGE to an image that already exists."
    [ -d "$plugin_dir" ] || plugin_image_die "plugin directory '${plugin_dir}' does not exist."
    [ -d "$src_dir" ] || plugin_image_die "plugin source directory '${src_dir}' does not exist."

    [ -z "$context" ] || plugin_image_discover_registry "$context" "$namespace" "$agent_ref"
    : "${PLUGIN_AR_LOCATION:=$PLUGIN_AR_LOCATION_FALLBACK}"
    : "${PLUGIN_AR_REPOSITORY:=$PLUGIN_AR_REPOSITORY_FALLBACK}"
    : "${PLUGIN_AR_PROJECT:=$project}"

    # Both the .dockerignore and the shape of the source tree are checked HERE, before the
    # caller provisions anything, and neither check keeps its result — the loads that matter
    # happen where they are used, and plugin_image_content_tag below reads the file again
    # inside its own subshell. Reading a small file twice is cheaper than the invariant.
    #
    # A .dockerignore this cannot match the way Docker does is fatal, and so is a symlink in
    # the tree; both report it by exiting, so neither must first be reached by the publish
    # step. Down there the exit lands between plugin_image_login and plugin_image_logout,
    # past the mktemp, and `exit` is not `return`: the failure handler in
    # plugin_image_publish does not run, the staging tree stays on disk and crane's access
    # token stays in ~/.docker/config.json for its full hour.
    plugin_image_check_file_types "$plugin_dir" "$src_dir"

    if [ -n "${PLUGIN_IMAGE_TAG:-}" ]; then
        tag="$PLUGIN_IMAGE_TAG"
        PLUGIN_IMAGE_TAG_PINNED=1
    else
        tag="$(plugin_image_content_tag "$plugin_dir" "$src_dir")" ||
            plugin_image_die "could not compute a content tag for '${src_dir}': it is unreadable, or holds no file the image would ship."
    fi

    PLUGIN_IMAGE_REF="${PLUGIN_AR_LOCATION}-docker.pkg.dev/${PLUGIN_AR_PROJECT}/${PLUGIN_AR_REPOSITORY}/${name}:${tag}"
    PLUGIN_IMAGE_PREBUILT=0

    # Announced here rather than in each installer's banner. The reference is not known
    # until this function has run, and a banner printed before it would either have to
    # omit the one line a reader looks for or push everything below into the middle of
    # the install.
    echo "  Image: ${PLUGIN_IMAGE_REF}"
    # Said out loud rather than left to be noticed inside the reference. States the fact
    # and not where it came from, because both routes lead here: AR_PROJECT set by hand,
    # and an agent image discovered in a registry project of its own.
    if [ "$PLUGIN_AR_PROJECT" != "$project" ]; then
        printf '  NOTE: publishing %s to project %s, not to %s.\n        That needs push rights there, and the image outlives a teardown of %s.\n' \
            "$name" "$PLUGIN_AR_PROJECT" "$project" "$project"
    fi

    plugin_image_builder

    # The repository and the credential are settled here rather than at publish time, and
    # for the same reason as the builder: both are prerequisites of the push, and both
    # fail for reasons somebody has to leave the terminal to fix — an unauthenticated
    # gcloud, no rights to create a repository. Left until step 5 they are found after the
    # provisioning is done, which is the failure this ordering exists to prevent.
    plugin_image_ensure_repository "$PLUGIN_IMAGE_REF"
    plugin_image_check_credential "$PLUGIN_IMAGE_REF" "$PLUGIN_IMAGE_BUILDER_RESOLVED"
    PLUGIN_IMAGE_REGISTRY_READY=1
}

# Create the Artifact Registry repository the reference names, if it is missing.
plugin_image_ensure_repository() {
    local image="$1" host location rest repo_project repo_name

    host="${image%%/*}"
    case "$host" in
        *-docker.pkg.dev) ;;
        # Some other registry — the caller pointed at it deliberately, so nothing here
        # tries to create it.
        *) return 0 ;;
    esac

    location="${host%-docker.pkg.dev}"
    rest="${image#*/}"
    repo_project="${rest%%/*}"
    rest="${rest#*/}"
    repo_name="${rest%%/*}"

    if gcloud artifacts repositories describe "$repo_name" \
        --location="$location" --project="$repo_project" >/dev/null 2>&1; then
        return 0
    fi

    # One stream, in one call. Split across stdout and stderr — as this was — the four
    # lines interleave unpredictably the moment the installer's output is piped to a
    # file, which is exactly when somebody is reading them rather than watching them.
    printf '%s\n' \
        "  Creating Artifact Registry repository ${repo_name} in ${location}..." \
        "  NOTE: a new repository has no reader binding of its own. If the agent pod" \
        "        reports ImagePullBackOff on the plugin's image volume, grant the" \
        "        cluster's nodes roles/artifactregistry.reader on it."
    # Tolerated: the common case is that the API is already on, and an account that can
    # push images may still lack serviceusage rights. If it really is off, the create
    # below fails with a message that says so.
    gcloud services enable artifactregistry.googleapis.com --project="$repo_project" --quiet ||
        echo "  WARN: could not enable artifactregistry.googleapis.com; continuing in case it is already enabled." >&2
    gcloud artifacts repositories create "$repo_name" \
        --repository-format=docker \
        --location="$location" \
        --project="$repo_project" \
        --description="kube-agents plugin images" --quiet ||
        # A concurrent installer may have won the race; only fail if it is still absent.
        gcloud artifacts repositories describe "$repo_name" \
            --location="$location" --project="$repo_project" >/dev/null 2>&1 ||
        plugin_image_die "could not create Artifact Registry repository '${repo_name}' in ${location} (project ${repo_project})."
}

# Prove the builder will be able to authenticate, WITHOUT leaving a secret behind.
#
# Called from resolve, where nothing is pushed yet. The distinction from
# plugin_image_login matters because the two paths differ in what they persist:
# `gcloud auth configure-docker` writes a credential HELPER — a program name — which is
# safe to leave anywhere, whereas `crane auth login` base64s a live access token into
# ~/.docker/config.json and leaves it there for its full hour.
#
# So docker configures for real here, and crane only checks that a token CAN be minted
# and throws it away. That keeps the fail-early property this ordering exists for — an
# unauthenticated gcloud is still caught before the first topic is created — without a
# bearer token sitting on disk through the five provisioning steps that follow, any one
# of which can abort the run before plugin_image_publish would have removed it.
plugin_image_check_credential() {
    local image="$1" builder="$2"
    local host="${image%%/*}"

    case "$host" in
        *-docker.pkg.dev | gcr.io | *.gcr.io) ;;
        *) return 0 ;;
    esac

    if [ "$builder" = "crane" ]; then
        gcloud auth print-access-token >/dev/null 2>&1 ||
            plugin_image_die "gcloud auth print-access-token produced nothing, so crane will have no credential for ${host}. Run 'gcloud auth login'."
        return 0
    fi
    plugin_image_login "$image" "$builder"
}

# Authenticate the chosen builder against the registry host.
#
# For crane this WRITES A TOKEN to ~/.docker/config.json, so it is called as late as
# possible — immediately before the push — and plugin_image_logout removes it on every
# path out of plugin_image_publish. The window is the push itself; an interrupt during
# one still leaves the token, and `crane auth logout <host>` clears it by hand.
plugin_image_login() {
    # host on its own line, not folded into the declaration above. `local` is a builtin:
    # every one of its arguments is expanded BEFORE any of them is assigned, so
    # `local image="$1" host="${image%%/*}"` reads whatever `image` meant in the CALLER's
    # scope — nothing, or the caller's own variable of that name. It read the right value
    # only for as long as plugin_image_publish, which happens to have an `image` local,
    # was the only caller.
    local image="$1" builder="$2" token
    local host="${image%%/*}"

    case "$host" in
        *-docker.pkg.dev | gcr.io | *.gcr.io) ;;
        # Anything else is the caller's own registry: use whatever credentials the host
        # is already configured with rather than overwriting them with a GCP token.
        *) return 0 ;;
    esac

    case "$builder" in
        docker)
            gcloud auth configure-docker "$host" --quiet >/dev/null 2>&1 ||
                plugin_image_die "gcloud auth configure-docker ${host} failed; docker cannot push there."
            ;;
        crane)
            # A short-lived access token rather than the gcloud credential helper: crane
            # reads ~/.docker/config.json, and a credHelpers entry pointing at a
            # docker-credential-gcloud that is not installed fails at push time with an
            # authentication error that reads like a permissions problem.
            #
            # Checked before it is used, because an unauthenticated gcloud prints nothing
            # and exits non-zero inside a substitution that swallows both — crane would
            # then be handed an empty password and the failure would surface later, at
            # push time, looking like a registry permissions problem.
            token="$(gcloud auth print-access-token 2>/dev/null || echo "")"
            [ -n "$token" ] ||
                plugin_image_die "gcloud auth print-access-token produced nothing, so crane has no credential for ${host}. Run 'gcloud auth login'."
            # --password-stdin, not -p: a token on the command line is visible in `ps` to
            # every other user on the machine for as long as the push takes.
            printf '%s' "$token" |
                "$PLUGIN_CRANE_BIN" auth login "$host" -u oauth2accesstoken --password-stdin >/dev/null ||
                plugin_image_die "crane could not authenticate to ${host}. Check 'gcloud auth login', and that '${PLUGIN_CRANE_BIN}' is new enough to support --password-stdin."
            ;;
    esac
}

# Remove the credential plugin_image_login stored, once the push no longer needs it.
#
# Only crane's. `gcloud auth configure-docker` writes a credential HELPER — a program name,
# no secret — but `crane auth login` base64s a live access token into ~/.docker/config.json
# and leaves it there. An hour of validity is not the same as gone, and nothing else in
# this repository writes a bearer token to disk.
#
# Nothing is destroyed by this: `crane auth login` overwrote whatever entry the host had,
# so a pre-existing credential for it was already replaced by the time we get here.
plugin_image_logout() {
    # Two statements, for the reason given in plugin_image_login.
    local image="$1" builder="$2"
    local host="${image%%/*}"

    [ "$builder" = "crane" ] || return 0
    case "$host" in
        *-docker.pkg.dev | gcr.io | *.gcr.io) ;;
        *) return 0 ;;
    esac
    # Best effort, and silent: a token left behind is worth removing, but failing to
    # remove it is not worth failing an otherwise complete install over.
    "$PLUGIN_CRANE_BIN" auth logout "$host" >/dev/null 2>&1 || true
}

plugin_image_exists() {
    # Two statements, for the reason given in plugin_image_login.
    local image="$1" builder="$2"
    local host="${image%%/*}"

    case "$host" in
        *-docker.pkg.dev)
            gcloud artifacts docker images describe "$image" >/dev/null 2>&1
            return $?
            ;;
    esac
    if [ "$builder" = "crane" ]; then
        "$PLUGIN_CRANE_BIN" digest "$image" >/dev/null 2>&1
        return $?
    fi
    docker manifest inspect "$image" >/dev/null 2>&1
}

# Copy a tree into a staging directory with the modes the image needs, minus whatever
# the plugin's .dockerignore excludes.
#
# The image mounts read-only and is read as UID 10000, so every file in it has to be
# world-readable — and a chmod belongs in a copy, not in the working tree. This is not
# theoretical tidying: git creates files as 0666 masked by the invoking umask, so a
# checkout made under `umask 077` yields mode 0600 files throughout, and an image built
# straight from that tree loads nothing.
#
# Both builders stage, so both ship the same modes and the same files for the same
# content tag. `src` is what gets copied; `plugin_dir` is only where .dockerignore is
# read from and what its patterns are anchored to, which is why both are taken.
plugin_image_stage() {
    local plugin_dir="$1" src="$2" dest="$3" prefix entry rel
    # Beside the staging tree rather than inside it — `$dest.ignored` under the same
    # mktemp directory, so nothing here can end up in the layer.
    local doomed="${dest}.ignored"

    prefix="$(plugin_image_src_prefix "$plugin_dir" "$src")" || return 1
    plugin_image_ignore_load "$plugin_dir" || return 1

    mkdir -p "$dest" || return 1
    cp -R "${src}/." "${dest}/" || return 1

    # Listed first and deleted afterwards, not deleted as `find` walks: removing a
    # directory out from under a traversal that is still reading it is how a walk starts
    # skipping siblings it should have visited.
    : >"$doomed" || return 1
    # `if` rather than `cond && printf`: an AND-list whose first half is false leaves the
    # loop body — and so the whole pipeline — non-zero, which under the caller's pipefail
    # would read as a staging failure on the entirely normal case of nothing being
    # excluded.
    # In a SUBSHELL, purely to hold `set -o pipefail`: this function returns rather than
    # dies, so that plugin_image_publish's failure handler runs, and an option set in its
    # body would outlive it and change how every later pipeline in the installer behaves.
    # The status of a `find | while` is the while loop's, and that is 0 however badly find
    # went — so a walk that stopped on a permission error would simply leave the paths it
    # never reached out of $doomed. Those files are then not deleted, and an excluded file
    # ships in a layer under a tag computed as though it were absent: the divergence
    # between the two builders that one shared reader exists to prevent, reintroduced by a
    # walk that half worked. $doomed is a file, so the subshell's writes outlive it.
    (
        set -o pipefail
        find "$dest" -mindepth 1 | while IFS= read -r entry; do
            rel="${entry#"${dest}/"}"
            if plugin_image_ignored "${prefix}${rel}"; then
                printf '%s\n' "$entry" >>"$doomed"
            fi
        done
    ) || return 1
    while IFS= read -r entry; do
        [ -n "$entry" ] || continue
        # -rf, and no test for existence: a matched directory is listed alongside the
        # matched files inside it, and whichever goes first takes the others with it.
        rm -rf "$entry" || return 1
    done <"$doomed"
    rm -f "$doomed" || return 1

    chmod -R a+rX "$dest" || return 1
}

# Build the plugin's own Dockerfile against a staged context and push the result.
#
# The context is the staged copy rather than plugin_dir itself, so the modes in the image
# are the ones plugin_image_stage sets. The Dockerfile and .dockerignore are copied along
# with everything else, so every COPY path in the Dockerfile still resolves unchanged.
plugin_image_docker_publish() {
    local plugin_dir="$1" image="$2" ctx="$3/context"

    plugin_image_stage "$plugin_dir" "$plugin_dir" "$ctx" || return 1
    docker build --platform "$PLUGIN_TARGET_PLATFORM" \
        -t "$image" -f "${ctx}/Dockerfile" "$ctx" || return 1
    docker push "$image" || return 1
}

# Assemble the same single-layer image with crane and push it. No daemon, no Dockerfile.
plugin_image_crane_publish() {
    local plugin_dir="$1" src_dir="$2" image="$3"
    local root="$4/root" layer="$4/layer.tar" staging="$3.staging"

    plugin_image_stage "$plugin_dir" "$src_dir" "$root" || return 1

    # --numeric-owner --owner=0 --group=0 is required, not cosmetic: without it macOS tar
    # writes the invoking user's ids and aborts with "Numeric user ID too large".
    #
    # That spelling and no other. bsdtar also has --uid/--gid, and it is tempting to use
    # them on a Mac, but GNU tar has no such options and exits on the unrecognised flag —
    # which would confine the crane path to macOS, and crane is the DEFAULT builder
    # wherever no Docker daemon answers, Linux included. --owner/--group are understood by
    # both and produce the same header (uid 0, gid 0, empty uname/gname) on each.
    #
    # COPYFILE_DISABLE stops macOS tar adding ._* AppleDouble entries to the layer.
    #
    # The member list comes through -T with the leading "./" stripped, so entries are
    # named `plugin.yaml` the way `COPY … /` writes them. Taring `.` instead adds a "./"
    # root entry that some tools reject outright — `crane export` of the result fails
    # with `unsafe tar path "."`.
    (
        cd "$root" &&
            find . -mindepth 1 -maxdepth 1 | sed 's|^\./||' |
            COPYFILE_DISABLE=1 tar --numeric-owner --owner=0 --group=0 -cf "$layer" -T -
    ) || return 1

    # Published under a staging tag and moved onto the real one, never assembled in
    # place. `crane append` on an empty base writes a config with os and architecture set
    # to "" — not what `docker build --platform` produces for the same Dockerfile — and
    # the mutate below is what fixes that. Were the append to write the final tag
    # directly, a mutate that failed would leave a valid-looking image under the tag the
    # content digest names, and every later install would find it published, skip the
    # build, and deploy the broken platform forever. The final tag only ever appears
    # fully formed, so a failure here is simply retried by re-running the installer.
    "$PLUGIN_CRANE_BIN" append -f "$layer" -t "$staging" || return 1
    "$PLUGIN_CRANE_BIN" mutate "$staging" \
        --set-platform "$PLUGIN_TARGET_PLATFORM" -t "$image" >/dev/null || return 1
    # Best effort, and deliberately not a warning. Deleting a tag needs a permission that
    # pushing one does not, and some registries decline a delete-by-tag outright — so this
    # can fail on every single install, and a warning that always fires is one nobody
    # reads. A leftover costs nothing either: the staging tag is derived from the content
    # tag, so the next build of the same content overwrites it rather than adding another.
    # It is reported once, at the end, as part of what the run did.
    "$PLUGIN_CRANE_BIN" delete "$staging" >/dev/null 2>&1 ||
        PLUGIN_IMAGE_STAGING_LEFT="$staging"
}

# Build PLUGIN_IMAGE_REF from src_dir and push it, unless it is already published.
#
#   plugin_dir  the chart directory, holding the Dockerfile and .dockerignore
#   src_dir     the tree the Dockerfile copies to /, which crane tars directly
plugin_image_publish() {
    local plugin_dir="$1" src_dir="$2" image="$PLUGIN_IMAGE_REF" builder work

    [ -n "$image" ] || plugin_image_die "plugin_image_publish was called before plugin_image_resolve."

    if [ "$PLUGIN_IMAGE_PREBUILT" = "1" ]; then
        echo "  Using the prebuilt image ${image} (PLUGIN_IMAGE is set); skipping the build."
        return 0
    fi

    plugin_image_builder
    builder="$PLUGIN_IMAGE_BUILDER_RESOLVED"
    # Normally already done by plugin_image_resolve; repeated only for a caller that
    # publishes without having resolved through it.
    [ "$PLUGIN_IMAGE_REGISTRY_READY" = "1" ] || plugin_image_ensure_repository "$image"
    # BEFORE the login, and that is the whole reason these are here rather than left to the
    # staging step that actually wants the exclusions. A .dockerignore outside the supported
    # subset is fatal, and so is a symlink in the source tree; both report it by exiting —
    # which past this line means exiting between the login and the logout, so the token
    # crane just wrote to ~/.docker/config.json stays there. `exit` is not `return`: the
    # failure handler below is bypassed too, leaving the staging tree behind with it.
    # Resolve makes both checks for the same reason, one step earlier; this covers a caller
    # that set PLUGIN_IMAGE_REF itself and never went through it.
    plugin_image_check_file_types "$plugin_dir" "$src_dir"
    # Not conditional, unlike the repository: resolve's token may have expired while the
    # caller provisioned, and re-minting one is a second of work.
    plugin_image_login "$image" "$builder"

    # Only a content tag is evidence of what is already published. A tag the caller chose
    # says nothing about the source behind it, so skipping the build for one would
    # reintroduce the `latest` failure this whole scheme exists to prevent: publish once,
    # then silently install stale content forever, under a message claiming the source
    # had not changed. A pinned tag is therefore always rebuilt and overwritten.
    #
    # Rebuilding is as far as that goes, and it is worth being plain about how far that is
    # NOT. Overwriting a tag republishes the bytes; it does not deliver them. The reference
    # in the AgentPlugin is unchanged, so the operator sees no change and the pod does not
    # roll, and spec.imagePullPolicy defaults to IfNotPresent, so a pod that did roll would
    # mount the copy the node already holds under that tag. A pinned tag is only safe where
    # something else moves it — a per-build tag out of CI, or an imagePullPolicy of Always.
    # This is said out loud below rather than left in a comment: whoever set the variable is
    # the person who needs to know, and they are reading the install log, not this file.
    if [ "$PLUGIN_IMAGE_TAG_PINNED" = "1" ]; then
        printf '%s\n' \
            "  PLUGIN_IMAGE_TAG is set, so ${image} is rebuilt and overwritten — a chosen tag is no evidence of its contents." \
            "  NOTE: overwriting a tag republishes the image but does not roll the agent," \
            "        and imagePullPolicy defaults to IfNotPresent, so a running pod keeps" \
            "        the copy it already has. Move the tag per build, or unset" \
            "        PLUGIN_IMAGE_TAG to get a content tag that changes when the source does."
    elif plugin_image_exists "$image" "$builder"; then
        echo "  ${image} is already published — the source has not changed since it was built."
        plugin_image_logout "$image" "$builder"
        return 0
    fi

    echo "  Building ${image} with ${builder}..."
    work="$(mktemp -d)"
    # The staging tree is removed on both paths. `set -e` in the caller would otherwise
    # abort the run before the cleanup, and the helpers return rather than die so that
    # this failure branch is reachable at all.
    case "$builder" in
        docker) plugin_image_docker_publish "$plugin_dir" "$image" "$work" ;;
        crane) plugin_image_crane_publish "$plugin_dir" "$src_dir" "$image" "$work" ;;
    esac || {
        rm -rf "$work"
        plugin_image_logout "$image" "$builder"
        plugin_image_die "could not build and publish ${image} with ${builder}. Nothing was left under that tag, so re-running the installer retries cleanly."
    }
    rm -rf "$work"
    plugin_image_logout "$image" "$builder"
    echo "  Published ${image}."
    # Said here, plainly and once, rather than as a warning at the moment it happened.
    # The build succeeded; this is a note about what else is in the repository.
    [ -z "$PLUGIN_IMAGE_STAGING_LEFT" ] ||
        echo "  (the intermediate tag ${PLUGIN_IMAGE_STAGING_LEFT} could not be removed; it is harmless, and the next build of the same content overwrites it)"
}

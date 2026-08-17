#!/usr/bin/env python3
"""Fail when a built image is close to Docker's 128-layer ceiling.

Docker's overlay2 storage driver refuses to mount a stack deeper than 128
layers and reports ``max depth exceeded``. The longest chain in
``deploy/docker/Dockerfile`` -- ``agent-base`` -> ``platform`` ->
``credential-proxy`` -- runs close to that ceiling, and nothing in an ordinary
build tells you how close.

Why a check rather than a comment: the limit belongs to the classic Docker
daemon, not to the image format. BuildKit has no equivalent limit, so an image
that is over budget builds cleanly in ``docker-build.yml`` (buildx) and in the
GHCR publish (buildx), and fails only in Cloud Build (``gcr.io/cloud-builders/
docker``), on main, after the pull request has merged. That is how six
consecutive Artifact Registry publishes broke on 2026-08-12 at 132 layers, and
how build f9f1747c broke on 2026-08-07 before them (#658).

The budget is checked against a real image rather than by counting ``RUN`` and
``COPY`` lines, because the two disagree: BuildKit elides instructions that
produce no filesystem diff, and the base image contributes layers this file
cannot see (38 of them, as of ``nousresearch/hermes-agent:v2026.8.3``). The
count that matters is the one the daemon will have to stack, which is the one
recorded in the image.

The default ceiling is deliberately below 128. A build that has reached 128 is
already broken; the point of a gate is to fail while there is still room to
land a fix.

Usage::

    # after `make build-credential-proxy`, or any build that loads the image
    python3 scripts/check_image_layers.py
    python3 scripts/check_image_layers.py --image credential-proxy:latest --max 120

Standard library only, but it does shell out to ``docker image inspect``, so it
needs a Docker daemon and an image that has been loaded into it. A buildx build
with ``push: false`` and no ``load: true`` does NOT leave an image behind --
see the note in .github/workflows/docker-build.yml.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

# Docker's overlay2 driver, daemon/graphdriver/overlay2: `maxDepth = 128`.
OVERLAY2_MAX_DEPTH = 128

# Default budget. The gap to OVERLAY2_MAX_DEPTH is the room a fix needs: enough
# that a pull request tripping this can consolidate a few COPYs rather than
# having to restructure the file under a broken publish.
DEFAULT_MAX_LAYERS = 120

DEFAULT_IMAGE = "credential-proxy:latest"


def layer_count(image: str) -> int:
    """Return the number of layers in ``image``, per the local Docker daemon."""
    proc = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{json .RootFS.Layers}}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "(no stderr)"
        raise SystemExit(
            f"cannot inspect {image}: {stderr}\n"
            f"Build it first, e.g.:\n"
            f"  docker build --platform linux/amd64 -f deploy/docker/Dockerfile "
            f"--target credential-proxy -t {DEFAULT_IMAGE} ."
        )
    return len(json.loads(proc.stdout))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=f"image to inspect (default: {DEFAULT_IMAGE})",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_LAYERS,
        dest="max_layers",
        help=f"fail above this many layers (default: {DEFAULT_MAX_LAYERS})",
    )
    args = parser.parse_args()

    if shutil.which("docker") is None:
        raise SystemExit("docker not found on PATH")

    count = layer_count(args.image)
    headroom = args.max_layers - count

    if count > args.max_layers:
        print(
            f"FAIL: {args.image} has {count} layers, over the {args.max_layers} budget "
            f"({OVERLAY2_MAX_DEPTH} is where Docker's overlay2 driver stops mounting "
            f"and Cloud Build starts failing with 'max depth exceeded').\n"
            f"\n"
            f"Every RUN and COPY in the agent-base -> platform -> credential-proxy "
            f"chain is a layer. Consolidate rather than adding: files sharing a "
            f"destination go in one COPY, and patch appliers are staged into a "
            f"directory instead of copied one at a time. See the layer-budget note "
            f"at the top of deploy/docker/Dockerfile.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {args.image} has {count} layers, {headroom} under the {args.max_layers} budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())

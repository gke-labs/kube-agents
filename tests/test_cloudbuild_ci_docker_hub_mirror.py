"""The smoke's Cloud Build pulls every Docker Hub base image through mirror.gcr.io.

deploy/docker/cloudbuild-ci.yaml builds the presubmit's images with plain
`docker build` from Cloud Build's shared egress, which Docker Hub rate-limits:
a 429 on node:24-slim killed a smoke on 2026-11-24 (#7033). Each Dockerfile
keeps a Docker Hub name as its base-image ARG default, so a local build is
unchanged, and each build in the Cloud Build file overrides every such ARG of
the Dockerfile it builds with `$_DOCKER_HUB_MIRROR/...`. The check runs per
build invocation, so an override dropped from one step is caught even when
another step passes the same ARG.

Literal `FROM busybox:musl@sha256:...` lines (the persona and AgentPlugin
images) are not ARGs, and hack/check-image-inventory.sh pins them as literals,
so they are out of this test's reach.
"""

import pathlib
import re
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CLOUDBUILD = _REPO_ROOT / "deploy" / "docker" / "cloudbuild-ci.yaml"
_MIRROR = "mirror.gcr.io"
_MIRROR_SUBSTITUTION = "_DOCKER_HUB_MIRROR"
_A2A_TEMPLATE = "a2a/Dockerfile.$1"

# `ARG GOLANG_IMAGE=golang`: an image ARG and its default.
_IMAGE_ARG = re.compile(r"^ARG ([A-Z_]+_IMAGE)=(\S+)$", re.MULTILINE)
_BUILD_ARG = re.compile(r"--build-arg\s+\"?([A-Z_]+)=\"?([^\"\s]+)")
_DASH_F = re.compile(r"-f\s+\"?([^\"\s]+)\"?")
_A2A_CALL = re.compile(r"^\s*build_and_push ([\w-]+) ", re.MULTILINE)


def _is_docker_hub(default):
    first = default.split("/", 1)[0]
    return "." not in first and ":" not in first


def _docker_hub_args(dockerfile):
    text = (_REPO_ROOT / dockerfile).read_text()
    return {arg for arg, default in _IMAGE_ARG.findall(text) if _is_docker_hub(default)}


def _builds():
    """(step id, Dockerfile, {build-arg: value}) for every docker build in the file."""
    out = []
    for step in yaml.safe_load(_CLOUDBUILD.read_text())["steps"]:
        args = step.get("args") or []
        if args and args[0] == "build":
            dockerfile = args[args.index("-f") + 1]
            pairs = [args[i + 1] for i, a in enumerate(args) if a == "--build-arg"]
            out.append((step["id"], dockerfile, dict(p.split("=", 1) for p in pairs)))
            continue
        script = args[1] if len(args) > 1 and args[0] == "-c" else ""
        # Join continuation lines, then read each docker build command.
        joined = script.replace("\\\n", " ").replace("$$", "$")
        for line in joined.splitlines():
            if "docker build" not in line:
                continue
            match = _DASH_F.search(line)
            if not match:
                continue
            build_args = dict(_BUILD_ARG.findall(line))
            dockerfile = match.group(1)
            if dockerfile == _A2A_TEMPLATE:
                for suffix in _A2A_CALL.findall(joined):
                    out.append((step["id"], f"a2a/Dockerfile.{suffix}", build_args))
            elif (_REPO_ROOT / dockerfile).is_file():
                out.append((step["id"], dockerfile, build_args))
    return out


class CloudBuildDockerHubMirrorTest(unittest.TestCase):
    def test_the_substitution_points_at_the_mirror(self):
        substitutions = yaml.safe_load(_CLOUDBUILD.read_text())["substitutions"]
        self.assertEqual(substitutions[_MIRROR_SUBSTITUTION], _MIRROR)

    def test_the_parse_sees_every_build_step(self):
        steps = {step for step, _, _ in _builds()}
        for expected in ("platform", "credential-proxy", "sandbox", "operator", "kube-proxy", "a2a", "a2a-bridge"):
            self.assertIn(expected, steps)
        self.assertIn("a2a/Dockerfile.worker", {dockerfile for _, dockerfile, _ in _builds()})

    def test_each_build_overrides_every_docker_hub_base_of_its_dockerfile(self):
        missing = []
        for step, dockerfile, build_args in _builds():
            for arg in sorted(_docker_hub_args(dockerfile)):
                value = build_args.get(arg, "")
                if not value.startswith(f"${_MIRROR_SUBSTITUTION}/"):
                    missing.append(f"{step}: {dockerfile} {arg}")
        self.assertEqual(missing, [], "Docker Hub base images a smoke build pulls without the mirror")


if __name__ == "__main__":
    unittest.main()

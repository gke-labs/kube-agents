FROM alpine:3.19
WORKDIR /
# Plain COPY, not COPY --chmod: --chmod is a BuildKit-only flag that the classic builder
# rejects outright ("the --chmod option requires BuildKit"). Staying builder-agnostic also
# keeps this image byte-equivalent to the one IMAGE_BUILDER=crane assembles, which layers
# the same files onto the same base without a Dockerfile. The test chmods the build
# context on the host instead, and COPY preserves those bits.
COPY . /

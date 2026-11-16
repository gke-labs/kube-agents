# The GHES proxy image: an eval fixture, not a product image. See README.md.
#
# Build from this directory:
#   docker build --platform linux/amd64 -t <registry>/ghes-proxy:<tag> hack/ghes-proxy
#
# Bases come from images.json, the repository's image inventory, spelled as
# a2a/Dockerfile.verifier spells them, so `make images-check` covers them.
ARG GOLANG_IMAGE=golang
ARG GOLANG_VERSION=1.27-alpine
ARG DISTROLESS_IMAGE=gcr.io/distroless/static
ARG DISTROLESS_VERSION=nonroot

FROM ${GOLANG_IMAGE}:${GOLANG_VERSION} AS build
# Refuse to download a toolchain the pin above does not name (#1138).
ENV GOTOOLCHAIN=local
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/ghes-proxy .

# The binary writes nothing to disk and needs only the CA bundle of the base
# to reach github.com.
FROM ${DISTROLESS_IMAGE}:${DISTROLESS_VERSION}
COPY --from=build /out/ghes-proxy /ghes-proxy
USER nonroot
ENTRYPOINT ["/ghes-proxy"]

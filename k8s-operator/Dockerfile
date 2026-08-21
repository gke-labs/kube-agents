# Base images are build args so this can be rebuilt where the public
# registries are unreachable; see deploy/docker/Dockerfile for why each takes
# a full reference rather than a shared prefix. Defaults are upstream, so an
# ordinary build is unchanged.
ARG GOLANG_IMAGE=golang
ARG GOLANG_VERSION=1.26-alpine
ARG DISTROLESS_IMAGE=gcr.io/distroless/static
ARG DISTROLESS_VERSION=nonroot

# Build stage
FROM ${GOLANG_IMAGE}:${GOLANG_VERSION} AS builder

ARG TARGETOS
ARG TARGETARCH
ARG TARGETPLATFORM
# When set, skip compilation and use the pre-built binary instead (used by GoReleaser)
ARG PREBUILT_BINARY
ARG VERSION=latest

WORKDIR /workspace

# Copy dependency files first for caching
COPY go.mod go.sum ./
RUN if [ -z "$PREBUILT_BINARY" ]; then go mod download; fi

# Copy everything else (source code, and GoReleaser platform dirs if present)
COPY . .

# Use pre-built binary if available, otherwise build from source.
# GoReleaser (dockers_v2) places binaries under $TARGETPLATFORM/ (e.g., linux/amd64/manager).
RUN if [ -n "$PREBUILT_BINARY" ] && [ -f "${TARGETPLATFORM}/${PREBUILT_BINARY}" ]; then \
      cp "${TARGETPLATFORM}/${PREBUILT_BINARY}" manager; \
    else \
      CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-amd64} go build -a -ldflags "-s -w -X github.com/gke-labs/kube-agents/k8s-operator/internal/controller.DefaultPlatformAgentVersion=${VERSION}" -o manager cmd/main.go; \
    fi

# Runtime stage - use distroless for minimal attack surface
FROM ${DISTROLESS_IMAGE}:${DISTROLESS_VERSION}

WORKDIR /

COPY --from=builder /workspace/manager .

USER 65532:65532

ENTRYPOINT ["/manager"]
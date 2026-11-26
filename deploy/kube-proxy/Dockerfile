ARG GOLANG_IMAGE=golang
ARG GOLANG_VERSION=1.27-alpine
ARG DISTROLESS_IMAGE=gcr.io/distroless/static
ARG DISTROLESS_VERSION=nonroot

FROM --platform=$BUILDPLATFORM ${GOLANG_IMAGE}:${GOLANG_VERSION} AS builder

ENV GOTOOLCHAIN=local

ARG TARGETOS
ARG TARGETARCH
# The commit the image is built from, which `--version` and the startup log name;
# docker-publish-ghcr.yml passes the SHA the image is tagged with. A release
# retags that image without rebuilding, so an image names its commit, not X.Y.Z.
ARG KUBE_AGENTS_VERSION=dev

# The binary is kube-proxy/cmd/kube-proxy: the generic k8s-api-proxy server plus the task
# credential gate, which verifies with a2a/taskcred. kube-proxy/go.mod replaces both
# modules with ../k8s-api-proxy and ../a2a, so the three sit side by side as in the
# repository. Of a2a only the two packages the gate imports are copied, so a change
# elsewhere on the bus does not rebuild this image.
WORKDIR /workspace/kube-proxy

COPY k8s-api-proxy/go.mod k8s-api-proxy/go.sum /workspace/k8s-api-proxy/
COPY a2a/go.mod a2a/go.sum /workspace/a2a/
COPY kube-proxy/go.mod kube-proxy/go.sum ./
RUN go mod download

COPY k8s-api-proxy/ /workspace/k8s-api-proxy/
COPY a2a/lib/ /workspace/a2a/lib/
COPY a2a/taskcred/ /workspace/a2a/taskcred/
COPY kube-proxy/ ./
# The output keeps the name k8s-api-proxy, so the path the image's ENTRYPOINT runs is the
# one it has always been.
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-amd64} \
    go build -a -ldflags="-s -w -X main.version=${KUBE_AGENTS_VERSION:-dev}" -o /workspace/k8s-api-proxy ./cmd/kube-proxy/

FROM ${DISTROLESS_IMAGE}:${DISTROLESS_VERSION}

WORKDIR /
COPY --from=builder /workspace/k8s-api-proxy /usr/local/bin/k8s-api-proxy

USER 10000:10000

ENTRYPOINT ["/usr/local/bin/k8s-api-proxy"]

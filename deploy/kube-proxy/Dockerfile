ARG GOLANG_IMAGE=golang
ARG GOLANG_VERSION=1.27-alpine
ARG DISTROLESS_IMAGE=gcr.io/distroless/static
ARG DISTROLESS_VERSION=nonroot

FROM --platform=$BUILDPLATFORM ${GOLANG_IMAGE}:${GOLANG_VERSION} AS builder

ENV GOTOOLCHAIN=local

ARG TARGETOS
ARG TARGETARCH

WORKDIR /workspace

COPY k8s-api-proxy/go.mod k8s-api-proxy/go.sum ./
RUN go mod download

COPY k8s-api-proxy/ ./
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-amd64} \
    go build -a -ldflags="-s -w" -o /workspace/k8s-api-proxy ./cmd/k8s-api-proxy/

FROM ${DISTROLESS_IMAGE}:${DISTROLESS_VERSION}

WORKDIR /
COPY --from=builder /workspace/k8s-api-proxy /usr/local/bin/k8s-api-proxy

USER 10000:10000

ENTRYPOINT ["/usr/local/bin/k8s-api-proxy"]

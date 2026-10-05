# The eval harness and scorer: devops-bench, kube_agents_bench and the cases.
#   docker build -t kube-agents-bench bench/
#   docker run kube-agents-bench devops-bench tasks/<id> --agent-type kubeagents
ARG PYTHON_IMAGE=python
ARG PYTHON_VERSION=3.12-slim
FROM ${PYTHON_IMAGE}:${PYTHON_VERSION}

ARG KUBECTL_VERSION=v1.34.1
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && pip install --no-cache-dir uv

WORKDIR /bench
COPY . .
RUN uv sync --frozen --no-dev
ENV PATH=/bench/.venv/bin:$PATH

# check=skip=InvalidDefaultArgInFrom
FROM node:22-bookworm AS agent-base

USER root

# 1. Install all common system utilities and dependencies
RUN apt-get update && apt-get install -y \
    apt-transport-https \
    ca-certificates \
    curl \
    dnsutils \
    git \
    gnupg \
    iputils-ping \
    jq \
    nano \
    patch \
    vim \
    wget \
    python3 \
    python3-venv \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Create openclaw user and group (UID/GID 10000) and install global pnpm
RUN groupadd -g 10000 openclaw && \
    useradd -u 10000 -g openclaw -m -s /bin/bash openclaw && \
    npm install -g pnpm

# 2. Install google-cloud-cli and kubectl natively using the Google Cloud apt repository
RUN curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y \
    google-cloud-cli \
    google-cloud-cli-gke-gcloud-auth-plugin \
    kubectl \
    && rm -rf /var/lib/apt/lists/*

# 3. Install GitHub CLI (gh), yq, k9s, and helm
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fL -o /tmp/gh.deb "https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_${ARCH}.deb" \
    && dpkg -i /tmp/gh.deb \
    && rm /tmp/gh.deb && \
    curl -sSLf -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v4.53.3/yq_linux_${ARCH}" && \
    chmod +x /usr/local/bin/yq && \
    curl -sSLf -o /tmp/k9s.tar.gz "https://github.com/derailed/k9s/releases/download/v0.51.0/k9s_Linux_${ARCH}.tar.gz" && \
    tar -xzf /tmp/k9s.tar.gz -C /usr/local/bin k9s && \
    rm /tmp/k9s.tar.gz && \
    curl -sSLf -o /tmp/helm.tar.gz "https://get.helm.sh/helm-v3.15.2-linux-${ARCH}.tar.gz" && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-${ARCH}

# 4. Create python venv at /opt/openclaw/.venv and install packages
RUN python3 -m venv /opt/openclaw/.venv && \
    /opt/openclaw/.venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/openclaw/.venv/bin/pip install --no-cache-dir \
    ddgs \
    google-api-python-client \
    google-auth \
    google-cloud-pubsub \
    mcp \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-exporter-otlp-proto-http \
    fastapi \
    uvicorn

# 5. Clone and build mcp-remote proxy
RUN git clone https://github.com/bradhoekstra/mcp-remote.git /opt/mcp-remote && \
    cd /opt/mcp-remote && \
    git checkout 2ff8f16753d8c744e6982b99e6ed5ab62f44f311 && \
    npm install && \
    npm run build && \
    npm prune --production && \
    npm cache clean --force && \
    rm -rf .git

# 6. Create base defaults directory and copy docs
RUN mkdir -p /opt/defaults && chown openclaw:openclaw /opt/defaults
COPY --chown=openclaw:openclaw docs/glossary.md /opt/defaults/docs/

# 6.5. Copy custom agent startup script
COPY deploy/shared/docker-entrypoint.sh /usr/local/bin/agent-entrypoint
RUN chmod 755 /usr/local/bin/agent-entrypoint

# Expose ports shared by all agent gateways
EXPOSE 8642

# Execution lifecycle
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["node", "/opt/openclaw/openclaw.mjs", "gateway", "--port", "8642", "--bind", "lan"]


# --- OPENCLAW BUILDER STAGE ---
FROM node:22-bookworm-slim AS openclaw-builder
ARG OPENCLAW_COMMIT=86e9dcfc1b051b8b0993850e21a37359ff2626ac

# Install build dependencies
RUN apt-get update && apt-get install -y git python3 make g++ && rm -rf /var/lib/apt/lists/*

# Install pnpm and patch utility
RUN apt-get update && apt-get install -y patch && rm -rf /var/lib/apt/lists/* && \
    npm install -g pnpm

COPY deploy/docker/patches/openclaw-googlechat.patch /tmp/openclaw-googlechat.patch

# Clone and build openclaw from public repository
RUN git clone https://github.com/openclaw/openclaw.git /opt/openclaw && \
    cd /opt/openclaw && \
    git checkout ${OPENCLAW_COMMIT} && \
    patch -p1 < /tmp/openclaw-googlechat.patch && \
    rm /tmp/openclaw-googlechat.patch && \
    pnpm install && \
    pnpm run build && \
    pnpm ui:build && \
    mkdir -p /opt/openclaw/dist && \
    UI_DIR=$(find /opt/openclaw/node_modules -path "*/dist/control-ui" -type d | head -n 1) && if [ -n "$UI_DIR" ]; then cp -r "$UI_DIR" /opt/openclaw/dist/control-ui; else echo "Error: control-ui assets not found" && exit 1; fi && \
    rm -rf .git


# --- PLATFORM AGENT TARGET ---
FROM agent-base AS platform

# Setup platform specific defaults from consolidated configs
COPY agents/platform/defaults/ /opt/defaults/
COPY agents/platform/scripts/ /opt/defaults/scripts/
COPY agents/platform/governance/ /opt/defaults/governance/
COPY agents/platform/SOUL.md /opt/defaults/SOUL.md
COPY agents/platform/AGENTS.md /opt/defaults/AGENTS.md

# Copy platform skills
COPY agents/platform/skills/ /opt/openclaw/skills/

# Copy compiled OpenClaw gateway from builder stage
COPY --from=openclaw-builder --chown=openclaw:openclaw /opt/openclaw /opt/openclaw

# Construct the openclaw workspace configs dynamically by combining primary config and OpenClaw metadata
RUN mkdir -p /openclaw/agents/platform
COPY agents/platform/ /openclaw/agents/platform/
COPY openclaw/ /openclaw/

# Set env var for OpenClaw state directory
ENV OPENCLAW_STATE_DIR=/opt/data

# Normalize ownership and permissions at the end of build
RUN chown -R openclaw:openclaw /opt/defaults /opt/openclaw /openclaw && \
    find /opt/defaults -type d -exec chmod 755 {} + && \
    find /opt/defaults -type f -exec chmod 644 {} + && \
    find /opt/openclaw/skills -type d -exec chmod 755 {} + && \
    find /opt/openclaw/skills -type f -exec chmod 644 {} + && \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + 2>/dev/null && \
    find /opt/openclaw/skills -path '*/scripts/*' -type f -exec chmod 755 {} + 2>/dev/null || true && \
    find /openclaw -type d -exec chmod 755 {} + && \
    find /openclaw -type f -exec chmod 644 {} + && \
    find /openclaw/agents/platform/scripts -type f -exec chmod 755 {} + 2>/dev/null || true

# Hand off execution to openclaw gateway
CMD ["node", "/opt/openclaw/openclaw.mjs", "gateway", "--port", "8642", "--bind", "lan"]

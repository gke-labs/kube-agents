# check=skip=InvalidDefaultArgInFrom
ARG HERMES_AGENT_TAG
ARG OPENCLAW_COMMIT

# ==========================================
# STAGE 1: Hermes Base (nousresearch/hermes-agent)
# ==========================================
FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG} AS hermes-base

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
    && rm -rf /var/lib/apt/lists/*

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

RUN /usr/local/bin/uv pip install --python /opt/hermes/.venv/bin/python \
    ddgs \
    google-api-python-client \
    google-auth \
    google-cloud-pubsub \
    "hermes-agent[google_chat]" \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-exporter-otlp-proto-http

# Apply upstream PR #51567 patch to google_chat adapter to fix nested inline formatting leaks (GC0, GC1, etc.)
RUN sed -i 's/for key, value in placeholders.items():/for key, value in reversed(list(placeholders.items())):/' /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    grep -q "reversed(list(placeholders.items()))" /opt/hermes/plugins/platforms/google_chat/adapter.py

# 4. Clone and build mcp-remote proxy
RUN git clone https://github.com/bradhoekstra/mcp-remote.git /opt/mcp-remote && \
    cd /opt/mcp-remote && \
    git checkout 2ff8f16753d8c744e6982b99e6ed5ab62f44f311 && \
    npm install && \
    npm run build && \
    npm prune --production && \
    npm cache clean --force && \
    rm -rf .git

# 5. Create base defaults directory and copy shared defaults
RUN mkdir -p /opt/defaults && chown hermes:hermes /opt/defaults
COPY --chown=hermes:hermes deploy/shared/defaults/ /opt/defaults/
COPY --chown=hermes:hermes docs/glossary.md /opt/defaults/docs/


# 5.5. Install and enable hermes-otel plugin in /opt/defaults
RUN HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins install briancaffey/hermes-otel && \
    HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins enable hermes_otel && \
    /opt/hermes/.venv/bin/python3 -c "import yaml, pathlib; p = pathlib.Path('/opt/defaults/plugins/hermes_otel/config.yaml'); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; c['backends'] = [{'name': 'gke-managed-otel', 'type': 'otlp', 'endpoint': 'http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces'}]; p.write_text(yaml.safe_dump(c))"



# 5.8. Copy custom agent startup script
COPY deploy/shared/docker-entrypoint.sh /usr/local/bin/agent-entrypoint
RUN chmod 755 /usr/local/bin/agent-entrypoint

# 6. Expose ports shared by all agent gateways
EXPOSE 8642
EXPOSE 9119

# 7. Execution lifecycle
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["hermes", "gateway", "run"]


# ==========================================
# STAGE 2: OpenClaw Base (node:22-bookworm)
# ==========================================
FROM node:22-bookworm AS openclaw-base

USER root

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

RUN groupadd -g 10000 openclaw && \
    useradd -u 10000 -g openclaw -m -s /bin/bash openclaw && \
    npm install -g pnpm

RUN curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y \
    google-cloud-cli \
    google-cloud-cli-gke-gcloud-auth-plugin \
    kubectl \
    && rm -rf /var/lib/apt/lists/*

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

RUN git clone https://github.com/bradhoekstra/mcp-remote.git /opt/mcp-remote && \
    cd /opt/mcp-remote && \
    git checkout 2ff8f16753d8c744e6982b99e6ed5ab62f44f311 && \
    npm install && \
    npm run build && \
    npm prune --production && \
    npm cache clean --force && \
    rm -rf .git

RUN mkdir -p /opt/defaults && chown openclaw:openclaw /opt/defaults
COPY --chown=openclaw:openclaw docs/glossary.md /opt/defaults/docs/

COPY deploy/shared/docker-entrypoint.sh /usr/local/bin/agent-entrypoint
RUN chmod 755 /usr/local/bin/agent-entrypoint

EXPOSE 8642
EXPOSE 9119

ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["node", "/opt/openclaw/openclaw.mjs", "gateway", "--port", "8642", "--bind", "lan"]


# ==========================================
# STAGE 3: OpenClaw Builder Stage
# ==========================================
FROM node:22-bookworm-slim AS openclaw-builder
ARG OPENCLAW_COMMIT=86e9dcfc1b051b8b0993850e21a37359ff2626ac

RUN apt-get update && apt-get install -y git python3 make g++ patch && rm -rf /var/lib/apt/lists/* && \
    npm install -g pnpm

COPY deploy/docker/patches/openclaw-googlechat.patch /tmp/openclaw-googlechat.patch

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


# ==========================================
# STAGE 4: Platform Agent Hermes Target (platform / platform-hermes)
# ==========================================
# --- PLATFORM AGENT TARGET ---
FROM hermes-base AS platform-hermes

# Setup platform specific defaults from consolidated configs
COPY agents/platform/cron/ /opt/defaults/cron/
COPY --chown=hermes:hermes agents/platform/defaults/ /opt/defaults/
COPY agents/platform/scripts/ /opt/defaults/scripts/
COPY agents/platform/governance/ /opt/defaults/governance/
COPY agents/platform/SOUL.md /opt/defaults/SOUL.md
COPY agents/platform/AGENTS.md /opt/defaults/AGENTS.md

# Copy platform skills
COPY agents/platform/skills/ /opt/hermes/skills/

# Copy platform plugins
COPY agents/platform/plugins/ /opt/hermes/plugins/

# Copy platform defaults configuration as temp file
COPY agents/platform/config.yaml /tmp/platform-config.yaml

# Copy config merge helper script
COPY deploy/docker/merge_configs.py /tmp/merge_configs.py

# Dynamically merge config.yamls using helper script
RUN /opt/hermes/.venv/bin/python3 /tmp/merge_configs.py /opt/defaults/config.yaml /tmp/platform-config.yaml && \
    rm -f /tmp/merge_configs.py /tmp/platform-config.yaml

# Normalize ownership and permissions at the end of build
RUN chown -R hermes:hermes /opt/defaults /opt/hermes/skills /opt/hermes/plugins && \
    find /opt/defaults -type d -exec chmod 755 {} + && \
    find /opt/defaults -type f -exec chmod 644 {} + && \
    find /opt/hermes/skills -type d -exec chmod 755 {} + && \
    find /opt/hermes/skills -type f -exec chmod 644 {} + && \
    find /opt/hermes/plugins -type d -exec chmod 755 {} + && \
    find /opt/hermes/plugins -type f -exec chmod 644 {} + && \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + 2>/dev/null && \
    find /opt/hermes/skills -path '*/scripts/*' -type f -exec chmod 755 {} + 2>/dev/null || true

# Alias platform to platform-hermes as default
FROM platform-hermes AS platform


# ==========================================
# STAGE 5: Platform Agent OpenClaw Target (platform-openclaw)
# ==========================================
FROM openclaw-base AS platform-openclaw

COPY agents/platform/defaults/ /opt/defaults/
COPY agents/platform/scripts/ /opt/defaults/scripts/
COPY agents/platform/governance/ /opt/defaults/governance/
COPY agents/platform/SOUL.md /opt/defaults/SOUL.md
COPY agents/platform/AGENTS.md /opt/defaults/AGENTS.md

COPY --from=openclaw-builder --chown=openclaw:openclaw /opt/openclaw /opt/openclaw
COPY agents/platform/skills/ /opt/openclaw/skills/

RUN mkdir -p /openclaw/agents/platform
COPY agents/platform/ /openclaw/agents/platform/
COPY openclaw/ /openclaw/

ENV OPENCLAW_STATE_DIR=/opt/data

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

CMD ["node", "/opt/openclaw/openclaw.mjs", "gateway", "--port", "8642", "--bind", "lan"]

# check=skip=InvalidDefaultArgInFrom
ARG HERMES_AGENT_TAG
FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG} AS agent-base

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

# 4. Apply the common stage2-hook patch
COPY deploy/shared/stage2-hook.patch /tmp/stage2-hook.patch
RUN patch /opt/hermes/docker/stage2-hook.sh < /tmp/stage2-hook.patch && rm /tmp/stage2-hook.patch

# 5. Clone and build mcp-remote proxy
RUN git clone https://github.com/bradhoekstra/mcp-remote.git /opt/mcp-remote && \
    cd /opt/mcp-remote && \
    git checkout 2ff8f16753d8c744e6982b99e6ed5ab62f44f311 && \
    npm install && \
    npm run build && \
    npm prune --production && \
    npm cache clean --force && \
    rm -rf .git

# 6. Create base defaults directory and copy shared defaults
RUN mkdir -p /opt/defaults && chown hermes:hermes /opt/defaults
COPY --chown=hermes:hermes deploy/shared/defaults/ /opt/defaults/

# 6.5. Install and enable hermes-otel plugin in /opt/defaults
RUN HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins install briancaffey/hermes-otel && \
    HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins enable hermes_otel && \
    /opt/hermes/.venv/bin/python3 -c "import yaml, pathlib; p = pathlib.Path('/opt/defaults/plugins/hermes_otel/config.yaml'); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; c['backends'] = [{'name': 'gke-managed-otel', 'type': 'otlp', 'endpoint': 'http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces'}]; p.write_text(yaml.safe_dump(c))"

# Copy shared skills
COPY --chown=hermes:hermes deploy/shared/skills/ /opt/hermes/skills/


# 7. Expose ports shared by all agent gateways
EXPOSE 8642
EXPOSE 9119

# 8. Default command
CMD ["hermes", "gateway", "run"]



# --- DEVTEAM AGENT TARGET ---
FROM agent-base AS devteam

# Copy identity template and defaults from consolidated templates
COPY agents/devteam/SOUL.md /opt/defaults/SOUL.md
COPY agents/devteam/AGENTS.md /opt/defaults/AGENTS.md
COPY agents/devteam/IDENTITY.md /opt/defaults/IDENTITY.md
COPY agents/devteam/HEARTBEAT.md /opt/defaults/HEARTBEAT.md
COPY agents/devteam/cron/ /opt/defaults/cron/
COPY agents/devteam/procedures/ /opt/defaults/procedures/

# Copy devteam skills
COPY agents/devteam/skills/ /opt/hermes/skills/

# Normalize ownership and permissions
RUN chown -R hermes:hermes /opt/defaults /opt/hermes/skills && \
    find /opt/defaults -type d -exec chmod 755 {} + && \
    find /opt/defaults -type f -exec chmod 644 {} + && \
    find /opt/hermes/skills -type d -exec chmod 755 {} + && \
    find /opt/hermes/skills -type f -exec chmod 644 {} + && \
    find /opt/hermes/skills -path '*/scripts/*' -type f -exec chmod 755 {} + 2>/dev/null || true



# --- OPERATOR AGENT TARGET ---
FROM agent-base AS operator

# Copy identity template and defaults from consolidated templates
COPY agents/operator/SOUL.md /opt/defaults/SOUL.md
COPY agents/operator/AGENTS.md /opt/defaults/AGENTS.md
COPY agents/operator/IDENTITY.md /opt/defaults/IDENTITY.md
COPY agents/operator/HEARTBEAT.md /opt/defaults/HEARTBEAT.md
COPY agents/operator/cron/ /opt/defaults/cron/
COPY agents/operator/procedures/ /opt/defaults/procedures/

# Copy operator skills
COPY agents/operator/skills/ /opt/hermes/skills/

# Normalize ownership and permissions
RUN chown -R hermes:hermes /opt/defaults /opt/hermes/skills && \
    find /opt/defaults -type d -exec chmod 755 {} + && \
    find /opt/defaults -type f -exec chmod 644 {} + && \
    find /opt/hermes/skills -type d -exec chmod 755 {} + && \
    find /opt/hermes/skills -type f -exec chmod 644 {} + && \
    find /opt/hermes/skills -path '*/scripts/*' -type f -exec chmod 755 {} + 2>/dev/null || true



# --- PLATFORM AGENT TARGET ---
FROM agent-base AS platform

# Setup platform specific defaults from consolidated configs
COPY agents/platform/cron/ /opt/defaults/cron/
COPY agents/platform/scripts/ /opt/defaults/scripts/
COPY agents/platform/governance/ /opt/defaults/governance/
COPY agents/platform/SOUL.md /opt/defaults/SOUL.md
COPY agents/platform/AGENTS.md /opt/defaults/AGENTS.md
COPY agents/platform/ROUTING.md /opt/defaults/ROUTING.md

# Copy platform skills
COPY agents/platform/skills/ /opt/hermes/skills/

# Copy platform defaults configuration as temp file
COPY agents/platform/config.yaml /tmp/platform-config.yaml

# Copy devteam deployment template
COPY deploy/kustomize/devteam/deployment.yaml /opt/defaults/templates/devteam/deployment.yaml

# Copy operator deployment template
COPY deploy/kustomize/operator/deployment.yaml /opt/defaults/templates/operator/deployment.yaml

# Copy config merge helper script
COPY deploy/docker/merge_configs.py /tmp/merge_configs.py

# Dynamically merge config.yamls using helper script
RUN /opt/hermes/.venv/bin/python3 /tmp/merge_configs.py /opt/defaults/config.yaml /tmp/platform-config.yaml && \
    rm -f /tmp/merge_configs.py /tmp/platform-config.yaml

# Normalize ownership and permissions at the end of build
RUN chown -R hermes:hermes /opt/defaults /opt/hermes/skills && \
    find /opt/defaults -type d -exec chmod 755 {} + && \
    find /opt/defaults -type f -exec chmod 644 {} + && \
    find /opt/hermes/skills -type d -exec chmod 755 {} + && \
    find /opt/hermes/skills -type f -exec chmod 644 {} + && \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + 2>/dev/null && \
    find /opt/hermes/skills -path '*/scripts/*' -type f -exec chmod 755 {} + 2>/dev/null || true

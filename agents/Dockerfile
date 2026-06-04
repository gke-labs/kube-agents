# check=skip=InvalidDefaultArgInFrom
ARG HERMES_AGENT_TAG
FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG} AS agent-base

USER root

# 1. Install all common system utilities and dependencies
RUN apt-get update && apt-get install -y \
    apt-transport-https \
    ca-certificates \
    gnupg \
    curl \
    patch \
    git \
    && rm -rf /var/lib/apt/lists/*

# 2. Install google-cloud-cli and kubectl natively using the Google Cloud apt repository
RUN curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y \
    google-cloud-cli \
    google-cloud-cli-gke-gcloud-auth-plugin \
    kubectl \
    && rm -rf /var/lib/apt/lists/*

# 3. Install GitHub CLI (gh)
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fL -o /tmp/gh.deb "https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_${ARCH}.deb" \
    && dpkg -i /tmp/gh.deb \
    && rm /tmp/gh.deb

RUN /usr/local/bin/uv pip install --python /opt/hermes/.venv/bin/python \
    google-api-python-client \
    google-auth \
    google-cloud-pubsub \
    "hermes-agent[google_chat]"

# 4. Apply the common stage2-hook patch
COPY agents/shared/stage2-hook.patch /tmp/stage2-hook.patch
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
RUN mkdir -p /opt/defaults
COPY agents/shared/defaults/ /opt/defaults/

# Copy shared skills
COPY --chown=hermes:hermes agents/shared/skills/ /opt/hermes/skills/


# 7. Expose ports shared by all agent gateways
EXPOSE 8642
EXPOSE 9119

# 8. Default command
CMD ["hermes", "gateway", "run"]



# --- DEVTEAM AGENT TARGET ---
FROM agent-base AS devteam

# Copy identity template
COPY --chmod=644 workspace/agents/platform/templates/devteam/SOUL.md /opt/defaults/SOUL.md

# Setup devteam specific defaults (e.g. potentially overwriting SOUL.md if provided)
COPY --chmod=755 agents/devteam/defaults/ /opt/defaults/

# Copy devteam skills
COPY --chown=hermes:hermes agents/devteam/skills/ /opt/hermes/skills/



# --- OPERATOR AGENT TARGET ---
FROM agent-base AS operator

# Copy identity template
COPY --chmod=644 workspace/agents/platform/templates/operator/SOUL.md /opt/defaults/SOUL.md

# Setup operator specific defaults
COPY --chmod=755 agents/operator/defaults/ /opt/defaults/

# Copy operator skills
COPY --chown=hermes:hermes agents/operator/skills/ /opt/hermes/skills/



# --- PLATFORM AGENT TARGET ---
FROM agent-base AS platform

# Setup platform specific defaults
COPY --chmod=755 agents/platform/defaults/ /opt/defaults/
COPY --chmod=644 agents/platform/SOUL.md /opt/defaults/SOUL.md

# Copy platform skills
COPY --chown=hermes:hermes agents/platform/skills/ /opt/hermes/skills/

# Copy platform defaults configuration as temp file
COPY --chmod=644 agents/platform/config.yaml /tmp/platform-config.yaml

# Copy devteam deployment template
COPY --chmod=644 agents/devteam/deployment.yaml /opt/defaults/templates/devteam/deployment.yaml

# Copy operator deployment template
COPY --chmod=644 agents/operator/deployment.yaml /opt/defaults/templates/operator/deployment.yaml

# Dynamically merge config.yamls using virtualenv python
RUN /opt/hermes/.venv/bin/python3 -c "import pathlib, yaml; p1=pathlib.Path('/opt/defaults/config.yaml'); p2=pathlib.Path('/tmp/platform-config.yaml'); s=yaml.safe_load(p1.read_text()) if p1.exists() else {}; p=yaml.safe_load(p2.read_text()) if p2.exists() else {}; s.update(p); p1.write_text(yaml.safe_dump(s))" && rm -f /tmp/platform-config.yaml

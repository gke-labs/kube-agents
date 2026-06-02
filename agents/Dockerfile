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
RUN curl -fL -o /tmp/gh.deb https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_amd64.deb \
    && dpkg -i /tmp/gh.deb \
    && rm /tmp/gh.deb

# 4. Apply the common stage2-hook patch
COPY agents/shared/stage2-hook.patch /tmp/stage2-hook.patch
RUN patch /opt/hermes/docker/stage2-hook.sh < /tmp/stage2-hook.patch && rm /tmp/stage2-hook.patch

# 5. Create base defaults directory and copy shared defaults
RUN mkdir -p /opt/defaults
COPY agents/shared/defaults/ /opt/defaults/

# Copy shared skills
COPY --chown=hermes:hermes agents/shared/skills/ /opt/hermes/skills/


# 6. Expose ports shared by all agent gateways
EXPOSE 8642
EXPOSE 9119

# 7. Default command
CMD ["hermes", "gateway", "run"]


# --- DEVTEAM AGENT TARGET ---
FROM agent-base AS devteam

# Copy identity template
COPY workspace/agents/platform/templates/devteam/SOUL.md /opt/defaults/SOUL.md

# Setup devteam specific defaults (e.g. potentially overwriting SOUL.md if provided)
COPY agents/devteam/defaults/ /opt/defaults/

# Copy devteam skills
COPY --chown=hermes:hermes agents/devteam/skills/ /opt/hermes/skills/

RUN chmod -R a+rX /opt/defaults


# --- OPERATOR AGENT TARGET ---
FROM agent-base AS operator

# Copy identity template
COPY workspace/agents/platform/templates/operator/SOUL.md /opt/defaults/SOUL.md

# Setup operator specific defaults
COPY agents/operator/defaults/ /opt/defaults/

# Copy operator skills
COPY --chown=hermes:hermes agents/operator/skills/ /opt/hermes/skills/

RUN chmod -R a+rX /opt/defaults


# --- PLATFORM AGENT TARGET ---
FROM agent-base AS platform

# Setup platform specific defaults
COPY agents/platform/defaults/ /opt/defaults/
COPY agents/platform/SOUL.md /opt/defaults/SOUL.md

# Copy platform skills
COPY --chown=hermes:hermes agents/platform/skills/ /opt/hermes/skills/

# Copy platform defaults configuration as temp file
COPY agents/platform/config.yaml /tmp/platform-config.yaml

# Copy devteam deployment template
COPY agents/devteam/deployment.yaml /opt/defaults/templates/devteam/deployment.yaml

# Dynamically merge config.yamls using virtualenv python
RUN /opt/hermes/.venv/bin/python3 -c "import yaml; s=yaml.safe_load(open('/opt/defaults/config.yaml')) or {}; p=yaml.safe_load(open('/tmp/platform-config.yaml')) or {}; s.update(p); yaml.safe_dump(s, open('/opt/defaults/config.yaml', 'w'))" && rm /tmp/platform-config.yaml

RUN chmod -R a+rX /opt/defaults

# check=skip=InvalidDefaultArgInFrom
ARG HERMES_AGENT_TAG
FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG}

USER root

# Install system utilities, gcloud CLI, GKE auth plugin, patch, and kubectl natively!
RUN apt-get update && apt-get install -y apt-transport-https ca-certificates gnupg curl patch && \
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin kubectl && \
    rm -rf /var/lib/apt/lists/*

# Copy shared defaults
COPY agents/shared/defaults/ /opt/defaults/

# Copy agent defaults
COPY agents/platform/defaults/ /opt/defaults/

# Copy SOUL.md identity
COPY agents/platform/SOUL.md /opt/defaults/SOUL.md

# Copy platform defaults configuration as temp file (contains 'mcp_servers' block)
COPY agents/platform/config.yaml /tmp/platform-config.yaml

# Dynamically merge config.yamls using the virtual environment's python binary (which has PyYAML installed)
RUN /opt/hermes/.venv/bin/python3 -c "import yaml; s=yaml.safe_load(open('/opt/defaults/config.yaml')) or {}; p=yaml.safe_load(open('/tmp/platform-config.yaml')) or {}; s.update(p); yaml.safe_dump(s, open('/opt/defaults/config.yaml', 'w'))" && rm /tmp/platform-config.yaml

RUN chmod -R a+rX /opt/defaults

# Copy and apply patch
COPY agents/shared/entrypoint.patch /tmp/entrypoint.patch
RUN patch /opt/hermes/docker/entrypoint.sh < /tmp/entrypoint.patch && rm /tmp/entrypoint.patch

# Expose ports
EXPOSE 8642
EXPOSE 9119

# Run gateway
CMD ["hermes", "gateway", "run"]

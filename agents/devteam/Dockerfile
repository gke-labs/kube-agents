FROM nousresearch/hermes-agent

USER root

# Install dependencies, git, and curl
RUN apt-get update && apt-get install -y \
    patch \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (gh) via standalone .deb package
RUN curl -L -o /tmp/gh.deb https://github.com/cli/cli/releases/download/v2.49.0/gh_2.49.0_linux_amd64.deb \
    && dpkg -i /tmp/gh.deb \
    && rm /tmp/gh.deb

# Install kubectl via standalone binary
RUN curl -LO "https://dl.k8s.io/release/v1.30.0/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/kubectl

# Install google-cloud-cli (gcloud) via standalone tarball
RUN curl -sSL https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz | tar -xz -C /opt
ENV PATH="/opt/google-cloud-sdk/bin:${PATH}"

# Create defaults directory
RUN mkdir -p /opt/defaults

# Copy identity from templates to defaults
COPY workspace/agents/platform/templates/devteam/SOUL.md /opt/defaults/SOUL.md
RUN chmod -R 755 /opt/defaults

# Setup .env for API server in defaults
RUN echo "API_SERVER_ENABLED=true" > /opt/defaults/.env
RUN echo "HERMES_DASHBOARD=1" >> /opt/defaults/.env

# Copy and apply patch
COPY agents/entrypoint.patch /tmp/entrypoint.patch
RUN patch /opt/hermes/docker/entrypoint.sh < /tmp/entrypoint.patch && rm /tmp/entrypoint.patch

# Expose port
EXPOSE 8642
EXPOSE 9119

# Run gateway
CMD ["hermes", "gateway", "run"]

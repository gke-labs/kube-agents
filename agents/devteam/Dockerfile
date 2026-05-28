# check=skip=InvalidDefaultArgInFrom
ARG HERMES_AGENT_TAG
FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG}

USER root

# Install dependencies, git, and curl
RUN apt-get update && apt-get install -y \
    patch \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (gh) via standalone .deb package
RUN curl -L -o /tmp/gh.deb https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_amd64.deb \
    && dpkg -i /tmp/gh.deb \
    && rm /tmp/gh.deb

# Install kubectl via standalone binary
RUN curl -LO "https://dl.k8s.io/release/v1.36.1/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/kubectl

# Install google-cloud-cli (gcloud) via standalone tarball
RUN curl -sSL https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz | tar -xz -C /opt
ENV PATH="/opt/google-cloud-sdk/bin:${PATH}"

# Create defaults directory
RUN mkdir -p /opt/defaults

# Copy identity from templates to defaults
COPY workspace/agents/platform/templates/devteam/SOUL.md /opt/defaults/SOUL.md

# Setup defaults
COPY agents/shared/defaults/ /opt/defaults/
COPY agents/devteam/defaults/ /opt/defaults/

# Ensure the non-root user (hermes) can read defaults on first-boot initialization
RUN chmod -R a+rX /opt/defaults

# Copy and apply patch
COPY agents/shared/entrypoint.patch /tmp/entrypoint.patch
RUN patch /opt/hermes/docker/entrypoint.sh < /tmp/entrypoint.patch && rm /tmp/entrypoint.patch

# Expose port
EXPOSE 8642
EXPOSE 9119

# Run gateway
CMD ["hermes", "gateway", "run"]
